"""Diagnostic compound read-only tools for the Laowadi electrical health agent."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
import math
import statistics

from laowadi.health import (
    BALANCE_THRESHOLD_KW,
    MAIN_CHAIN,
    POLLING_RAMP_KW,
    SPARE_CABINETS,
    evaluate_health,
)
from laowadi.monitor import HOUR, stamp
from laowadi.standards import search_standards
from laowadi.storage import METRICS, REPORT_STATION


DIAGNOSTIC_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_station_health_overview",
            "description": (
                "获取老洼地 35 kV 变电所全站 25 柜 × 4 项电气量（Uab/Ia/P/cos）的四维体检总览与分级健康发现清单。"
                "适合回答全站健康状况、某日异常汇总或快速定位高/中优先级问题。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "day": {
                        "type": "string",
                        "description": "可选，指定日期 YYYY-MM-DD（范围 2026-11-10 至 2026-11-16）；留空返回整周 7 天体检总览。",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["all", "high", "medium", "info"],
                        "description": "按严重级别过滤发现，默认 all。",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_window_snapshot",
            "description": (
                "一次获取指定时间窗口内多个柜号的多项电气量（Uab/Ia/P/cos）整点读数、首尾变化量与原表单元格出处。"
                "每次最多查询 24 小时，适合排查事件前后多柜联动、倒闸切换或负荷突变。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_time": {
                        "type": "string",
                        "description": "起始整点，格式 YYYY-MM-DD HH:00:00（例如 2026-11-13 06:00:00）。",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "结束整点，格式 YYYY-MM-DD HH:00:00（例如 2026-11-13 10:00:00）。",
                    },
                    "cabinets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要查询的柜号列表（如 ['303','306','311','312']）；留空默认查主供电链 6 柜（303/306/311/312/1005/1006）。",
                    },
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["Uab", "Ia", "P", "cos"]},
                        "description": "要查询的电气量列表；留空默认返回全部四项（Uab/Ia/P/cos）。",
                    },
                },
                "required": ["start_time", "end_time"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_power_balance",
            "description": (
                "对指定整点执行全站功率守恒与运行方式核验：计算 35 kV 进线与主变高压侧差值、主变高低压侧差值、"
                "10 kV 低压侧与已列日报出线总和差值，判断是否处于单进线带双主变、全链反向有功或 SCADA 整点轮询非同步截面。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "at_time": {
                        "type": "string",
                        "description": "整点时刻，格式 YYYY-MM-DD HH:00:00。",
                    },
                    "section_cabinet": {
                        "type": "string",
                        "description": "可选。按该出线柜所在的母线段汇总主变低压侧和非备用出线，并对照该柜有功反号后的差额。",
                    },
                },
                "required": ["at_time"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_cabinet_health_profile",
            "description": (
                "查询指定柜号的 7 天完整电气健康画像：四项电气量统计、10 kV 所属母线段（I/II段）、电压偏差超限小时数、"
                "带载功率因数分布、三相功率自洽比例 P/(√3·Uab·Ia·cos)、图示供电关系及该柜相关健康发现与原表出处。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cabinet": {
                        "type": "string",
                        "description": "柜号，如 303、306、1009、1010、1012、1018、1020、1101 等。",
                    }
                },
                "required": ["cabinet"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_diurnal_baseline",
            "description": (
                "查询老洼地 35 kV 全站总有功（303+306）的 7 天 00:00–23:00 分时段基线与跨日反常偏离点，"
                "用于区分常规避峰填谷（如每天 10:00→11:00 与 17:00→18:00 骤降）与真正的跨日异常负荷。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hour": {
                        "type": "string",
                        "description": "可选，指定整点小时如 '11:00' 或 '18:00'；留空返回全天 24 小时基线和全部反常点。",
                    },
                    "day": {
                        "type": "string",
                        "description": "可选，指定日期 YYYY-MM-DD 查看该日各整点相对 7 天中位基线的偏差。",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_coal_standards",
            "description": (
                "检索本地已归档的《煤矿安全规程》（2025 修订，电气与监控章节原文）以及已核对的 GB/T 50417-2017、"
                "GB/T 12325-2008 条款与判据参考，返回精确条号、PDF 页码与原文摘录。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "检索关键词，例如 '两回路电源 分列运行'、'电压偏差 电能质量'、'中性点接地'、'保护定值 台账'。",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回条数（1 至 6，默认 4）。",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retrieve_conditional_evidence",
            "description": (
                "条件化检索老洼地本矿归档证据卡（6张带原表单元格与SHA-256的领域证据卡）与68条规程/国标条款，"
                "并自动执行站点与柜号前置适用性核验（拦截井下 1535 中央所 1101~1207 柜小时读数越界查询并输出结构化缺证清单）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "诊断问题或检索关键词。"},
                    "cabinet": {"type": "string", "description": "可选，目标柜号（如 303、1018、1101）。"},
                    "day": {"type": "string", "description": "可选，目标日期 YYYY-MM-DD。"},
                    "station": {"type": "string", "enum": ["S35", "C1535"], "description": "可选，指定站点代号。"},
                    "top_k": {"type": "integer", "description": "返回条数（1-5，默认 3）。"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_conditional_capacity",
            "description": (
                "按老洼地 V01-V04 专业核验契约执行有条件载流裕度与过载局部复算（v1 基线 -> v2 复算）。"
                "当用户提供 303 额定电流、主变额定容量(kVA)、1009/1010 下井电缆额定电流、降容系数或倒闸日志时调用本工具，"
                "精确计算负载率与安全裕度；参数不全时严格保持待补证，绝不猜测。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rated_303_a": {"type": "number", "description": "35 kV 303 一回进线额定电流 (A)。"},
                    "rated_tr1_kva": {"type": "number", "description": "1# 主变压器额定容量 (kVA)。"},
                    "rated_1009_a": {"type": "number", "description": "10 kV 1009/1010 下井电缆额定载流量 (A)。"},
                    "derating_factor": {"type": "number", "description": "环境与敷设降容系数（0.5-1.2，如 0.90）。"},
                    "switch_log_20261113": {"type": "string", "description": "2026-11-13 35 kV 母联与 306 检修开关确认记录。"},
                    "tie_switch_mode": {"type": "string", "description": "1535 中央变电所 1200 联络柜运行方式。"},
                    "source_doc": {"type": "string", "description": "参数来源说明。"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_electrical_analytics",
            "description": (
                "对老洼地 35 kV 变电所 25 柜 × 168 小时只读 SQLite 电气量执行安全参数化即席统计、分组排行或多指标联合条件筛选，"
                "并返回精确的原表单元格出处。支持两类只读分析："
                "1) aggregate_rank：按 cabinet/day/hour 分组计算 sum（对有功 P 即近似累计电量 kWh）、avg、min、max、std（标准差/波动度）、range（极差）、count 并输出 Top-K 排行；"
                "2) joint_filter：在同一柜号同一整点上同时满足多项电气量阈值条件的联合筛选（如同时满足 Uab > 10.85 kV 且 cos < 0.70）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["aggregate_rank", "joint_filter"],
                        "description": "分析操作类型：aggregate_rank（分组统计排行）或 joint_filter（多指标联合条件筛选）。",
                    },
                    "metric": {
                        "type": "string",
                        "enum": ["Uab", "Ia", "P", "cos"],
                        "description": "aggregate_rank 时的目标电气量（默认 P）。",
                    },
                    "group_by": {
                        "type": "string",
                        "enum": ["cabinet", "day", "hour"],
                        "description": "aggregate_rank 时的分组维度（默认 cabinet）。",
                    },
                    "agg": {
                        "type": "string",
                        "enum": ["sum", "avg", "min", "max", "std", "range", "count"],
                        "description": "aggregate_rank 时的统计方式（默认 avg）。",
                    },
                    "order": {
                        "type": "string",
                        "enum": ["desc", "asc"],
                        "description": "排序方向（默认 desc）。",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回前 K 条记录（1 至 20，默认 5）。",
                    },
                    "cabinets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选，限定柜号列表（如 ['1009', '1010']）；留空统计地面站 25 柜。",
                    },
                    "day": {
                        "type": "string",
                        "description": "可选，限定单日 YYYY-MM-DD。",
                    },
                    "start_day": {
                        "type": "string",
                        "description": "可选，起始日期 YYYY-MM-DD。",
                    },
                    "end_day": {
                        "type": "string",
                        "description": "可选，结束日期 YYYY-MM-DD。",
                    },
                    "exclude_spares": {
                        "type": "boolean",
                        "description": "未显式指定柜号时是否排除备用柜 1030/1032，默认 true。",
                    },
                    "voltage_level": {
                        "type": "string",
                        "enum": ["all", "10kV", "35kV"],
                        "description": "可选，按电压等级过滤（例如按 10 kV 过压阈值 Uab > 10.85 kV 筛选时应传 '10kV'，避免 35 kV 柜 36 kV 读数混入；默认 all）。",
                    },
                    "conditions": {
                        "type": "array",
                        "description": "joint_filter 时的多指标联合条件列表，如 [{\"metric\":\"Uab\",\"op\":\">\",\"value\":10.85},{\"metric\":\"cos\",\"op\":\"<\",\"value\":0.70}]。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "metric": {"type": "string", "enum": ["Uab", "Ia", "P", "cos"]},
                                "op": {"type": "string", "enum": [">", ">=", "<", "<=", "=", "!="]},
                                "value": {"type": "number"},
                            },
                            "required": ["metric", "op", "value"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["operation"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "simulate_electrical_engineering",
            "description": (
                "基于老洼地归档实测电气量与标准电力系统物理公式执行确定性只读电气工程仿真计算（零参数编造，全数附原表单元格出处）。支持两类专业计算："
                "1) reactive_compensation：按 Qc = P·(tanφ1 - tanφ2) 测算低功率因数回路（如 1010/1020）提升至目标 cos（如 0.90/0.95）所需的峰值/P95/平均无功补偿容量(kVar)、电流下降率及线路铜损降损比例；"
                "2) n1_transfer：模拟任一回路停运（如 1009 退运转供至 1010、或 306 转供至 303）时的全周同步合成电流、合成视在功率(kVA)与最严苛整点；若提供额定载流量则同步输出条件负载率，未提供时严守缺证边界。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["reactive_compensation", "n1_transfer"],
                        "description": "仿真类型：reactive_compensation（无功补偿容量与降损测算）或 n1_transfer（N-1 负荷转供仿真）。",
                    },
                    "cabinet": {
                        "type": "string",
                        "description": "reactive_compensation 时的目标柜号（如 '1010'、'1020'）。",
                    },
                    "target_cos": {
                        "type": "number",
                        "description": "reactive_compensation 时的目标功率因数（0.75 至 0.99，默认 0.90）。",
                    },
                    "outage_cabinet": {
                        "type": "string",
                        "description": "n1_transfer 时的退出/故障回路柜号（如 '1009' 或 '306'）。",
                    },
                    "receiving_cabinet": {
                        "type": "string",
                        "description": "n1_transfer 时的受电转供回路柜号（如 '1010' 或 '303'）。",
                    },
                    "day": {
                        "type": "string",
                        "description": "可选，限定单日 YYYY-MM-DD；留空按全周 168 小时计算。",
                    },
                    "rated_current_a": {
                        "type": "number",
                        "description": "可选，受电回路额定载流量 (A)；提供时计算条件 N-1 负载率与安全裕度。",
                    },
                    "derating_factor": {
                        "type": "number",
                        "description": "可选，敷设与环境降容系数（0.5-1.2，默认 0.90）。",
                    },
                },
                "required": ["operation"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_ac_power_flow",
            "description": (
                "基于 MineProfile 拓扑与老洼地归档 SQLite 整点边界，调用真实 pandapower Newton-Raphson 交流潮流求解器执行物理校核"
                "（严格区分实测边界单元格出处与参数化等值电路假设）。支持 4 类工程仿真场景："
                "1) base_case：指定整点的全网交流潮流与 10 kV I/II 段实测电压拟合校核；"
                "2) tap_sensitivity：扫描 2# 主变高压侧分接头（-2 至 +2 档，步长 ±2.5%），量化计算消除 10 kV II 段过压（如 11-13 17:00）的精准调压档位；"
                "3) n1_contingency：N-1 故障潮流（1009 退运经 1200 联络由 1010 转供 / 306 退运 / 2#主变退运），计算收敛后的电缆负载率与井下末端压降；"
                "4) reactive_opt：在目标井下回路（1010/1020/1009）末端投切并联电容器前后全网交流潮流对比，量化降流率、压升与真实网损降低(kW)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scenario": {
                        "type": "string",
                        "enum": ["base_case", "tap_sensitivity", "n1_contingency", "reactive_opt"],
                        "description": "潮流计算场景：base_case、tap_sensitivity、n1_contingency 或 reactive_opt。",
                    },
                    "at_time": {
                        "type": "string",
                        "description": "可选，归档整点时刻 YYYY-MM-DD HH:00:00（留空按场景自动选用典型极值整点，如 2026-11-13 17:00:00 或 2026-11-10 06:00:00）。",
                    },
                    "transformer_sn_mva": {
                        "type": "number",
                        "description": "可选，主变额定容量等值假设（MVA，默认 6.3）。",
                    },
                    "transformer_vk_percent": {
                        "type": "number",
                        "description": "可选，主变短路电压百分比等值假设（%，默认 7.5）。",
                    },
                    "tr1_tap_pos": {
                        "type": "integer",
                        "description": "可选，1#主变高压侧分接头档位（-2 至 +2，默认 +2）。",
                    },
                    "tr2_tap_pos": {
                        "type": "integer",
                        "description": "可选，2#主变高压侧分接头档位（-2 至 +2，默认 +1）。",
                    },
                    "cable_length_km": {
                        "type": "number",
                        "description": "可选，1009/1010 下井电缆长度假设（km，默认 1.8）。",
                    },
                    "outage_mode": {
                        "type": "string",
                        "enum": ["feeder_1009", "incomer_306", "transformer_t2"],
                        "description": "n1_contingency 时的故障停运方式，默认 feeder_1009。",
                    },
                    "target_cabinet": {
                        "type": "string",
                        "description": "reactive_opt 时的补偿目标出线柜号（1010/1020/1009，默认 1010）。",
                    },
                    "qc_kvar": {
                        "type": "number",
                        "description": "reactive_opt 时的并联电容补偿容量（kVar，留空按目标 cos=0.92 自动测算）。",
                    },
                },
                "required": ["scenario"],
                "additionalProperties": False,
            },
        },
    },
]

SANDBOX_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_python_analysis",
        "description": (
            "在隔离的只读 Python/SQL/NumPy/SciPy/pandapower 沙盒中执行自定义分析脚本，用于回答固定工具未覆盖的开放式工程计算"
            "（如相关性/线性回归、负荷曲线聚类、多柜统计对比、假设性潮流敏感性试算与生成结构化表格/SVG 图表）。"
            "沙盒已预注入 query_sql(sql, params)、query_events(sql, params)、emit_table(title, columns, rows)、"
            "emit_chart(title, chart_type, x_label, y_label, series)、np、math、statistics、json、re、datetime、collections，"
            "并允许按需 import scipy / pandapower。严禁写文件、访问网络或修改数据库。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "要执行的只读 Python 分析脚本。禁止 import sqlite3 或 pandas，直接调用预注入 query_sql(sql) 返回 list[dict]，metric 严格区分大小写（'Uab','Ia','P','cos'）。表结构：reading(day,time,station,cabinet,metric,value,marker,source_row,source_col)、report(day,filename,sheet,sha256)、cabinet(station,number,label,aliases)、summary(...)。",
                },
                "timeout_s": {
                    "type": "number",
                    "description": "可选超时时间（秒，默认 6.0，最大 15.0）。",
                },
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    },
}

AUTONOMOUS_TOOL_SCHEMAS = [*DIAGNOSTIC_TOOL_SCHEMAS, SANDBOX_TOOL_SCHEMA]


class DiagnosticToolbox:
    """Read-only diagnostic toolbox bound to an Archive instance."""

    def __init__(self, archive, profile=None):
        from laowadi.mine_schema import load_mine_profile

        self.archive = archive
        self.profile = profile or load_mine_profile()
        self._health_cache = {}

    def _health(self, day=None):
        key = day or "__ALL__"
        if key not in self._health_cache:
            self._health_cache[key] = evaluate_health(self.archive, day=day)
        return self._health_cache[key]

    def execute(self, name: str, arguments: dict | None) -> dict:
        args = arguments or {}
        if not isinstance(args, dict):
            return {"status": "error", "error": "工具参数必须为 JSON 对象。"}
        dispatch = {
            "get_station_health_overview": self.get_station_health_overview,
            "get_window_snapshot": self.get_window_snapshot,
            "check_power_balance": self.check_power_balance,
            "get_cabinet_health_profile": self.get_cabinet_health_profile,
            "get_diurnal_baseline": self.get_diurnal_baseline,
            "search_coal_standards": self.search_coal_standards,
            "retrieve_conditional_evidence": self.retrieve_conditional_evidence,
            "verify_conditional_capacity": self.verify_conditional_capacity,
            "query_electrical_analytics": self.query_electrical_analytics,
            "simulate_electrical_engineering": self.simulate_electrical_engineering,
            "run_ac_power_flow": self.run_ac_power_flow,
            "run_python_analysis": self.run_python_analysis,
        }
        handler = dispatch.get(name)
        if not handler:
            return {"status": "error", "error": f"未知诊断工具：{name}"}
        try:
            return handler(**args)
        except (TypeError, ValueError) as exc:
            return {"status": "error", "tool": name, "error": str(exc)}

    def get_station_health_overview(self, day: str | None = None, severity: str = "all") -> dict:
        if day is not None:
            day = date.fromisoformat(day).isoformat()
        if severity not in {"all", "high", "medium", "info"}:
            raise ValueError("severity 须为 all、high、medium 或 info。")
        report = self._health(day=day)
        findings = [
            f for f in report["findings"]
            if severity == "all" or f["severity"] == severity
        ]
        compact_findings = [
            {
                "id": f["id"],
                "severity": f["severity"],
                "dimension": f["dimension"],
                "cabinet": f["cabinet"],
                "title": f["title"],
                "summary": f["summary"],
                "evidence": f["evidence"][:3],
                "action": f["action"],
            }
            for f in findings
        ]
        return {
            "kind": "station_health_overview",
            "status": "ok",
            "scope": {
                "days": report["scope"]["days"],
                "hours": report["scope"]["hours"],
                "cabinets": report["scope"]["cabinets"],
                "completeness": report["scope"]["completeness"],
            },
            "summary": report["summary"],
            "bus_segments": report["dimensions"]["power_quality"]["bus_segments"],
            "balance_summary": report["dimensions"]["system_balance"]["balance_summary"],
            "findings": compact_findings,
            "missing_field_evidence": report["missing_field_evidence"],
        }

    def get_window_snapshot(
        self,
        start_time: str,
        end_time: str,
        cabinets: list[str] | None = None,
        metrics: list[str] | None = None,
    ) -> dict:
        start = stamp(start_time)
        end = stamp(end_time)
        dt_start = datetime.fromisoformat(start)
        dt_end = datetime.fromisoformat(end)
        if dt_end < dt_start:
            raise ValueError("结束时刻不能早于起始时刻。")
        hours = int((dt_end - dt_start) // HOUR) + 1
        if hours > 24:
            raise ValueError("单次窗口快照最多查询 24 个整点，请缩小起止时间范围。")

        selected_cabs = list(dict.fromkeys(str(c) for c in (cabinets or self.profile.main_chain_cabinets())))
        if len(selected_cabs) > 12:
            raise ValueError("单次窗口快照最多查询 12 个柜号。")
        station = self.profile.primary_station
        valid_cabs = {num for st, num in self.archive.devices if st == station}
        unknown = [c for c in selected_cabs if c not in valid_cabs]
        if unknown:
            raise ValueError(f"{station} 日报台账中无以下柜号：{', '.join(unknown)}")

        selected_metrics = list(dict.fromkeys(metrics or list(METRICS)))
        bad_m = [m for m in selected_metrics if m not in METRICS]
        if bad_m:
            raise ValueError(f"不支持的电气量：{', '.join(bad_m)}")

        c_ph = ",".join("?" for _ in selected_cabs)
        m_ph = ",".join("?" for _ in selected_metrics)
        rows = self.archive.db.execute(
            f"""
            SELECT r.time, r.cabinet, r.metric, r.value, r.source_col || r.source_row AS cell,
                   rep.filename, rep.sheet, rep.sha256
            FROM reading r
            JOIN report rep ON rep.day = r.day
            WHERE r.station = ? AND r.time BETWEEN ? AND ?
              AND r.cabinet IN ({c_ph}) AND r.metric IN ({m_ph})
            ORDER BY r.time, r.cabinet, r.metric
            """,
            (station, start, end, *selected_cabs, *selected_metrics),
        ).fetchall()
        if not rows:
            return {"kind": "window_snapshot", "status": "missing", "start_time": start, "end_time": end, "frames": []}

        frames = defaultdict(lambda: defaultdict(dict))
        for r in rows:
            frames[r["time"]][r["cabinet"]][r["metric"]] = {
                "value": r["value"],
                "cell": f'{r["filename"]} / {r["sheet"]}!{r["cell"]}',
            }

        ordered_times = sorted(frames.keys())
        deltas = {}
        if len(ordered_times) >= 2:
            t0, t1 = ordered_times[0], ordered_times[-1]
            for cab in selected_cabs:
                cab_d = {}
                for m in selected_metrics:
                    v0 = frames[t0].get(cab, {}).get(m, {}).get("value")
                    v1 = frames[t1].get(cab, {}).get(m, {}).get("value")
                    if v0 is not None and v1 is not None:
                        cab_d[m] = round(v1 - v0, 3)
                if cab_d:
                    deltas[cab] = cab_d

        return {
            "kind": "window_snapshot",
            "status": "ok",
            "station": station,
            "start_time": start,
            "end_time": end,
            "cabinets": {c: self.archive.devices[(station, c)]["label"] for c in selected_cabs},
            "metrics": selected_metrics,
            "hour_count": len(ordered_times),
            "start_to_end_deltas": deltas,
            "frames": [{"time": t, "readings": dict(frames[t])} for t in ordered_times],
        }

    def check_power_balance(self, at_time: str, section_cabinet: str | None = None) -> dict:
        at = stamp(at_time)
        dt = datetime.fromisoformat(at)
        prev_t = (dt - HOUR).isoformat(sep=" ")
        next_t = (dt + HOUR).isoformat(sep=" ")
        station = self.profile.primary_station

        rows = self.archive.db.execute(
            """
            SELECT r.time, r.cabinet, r.metric, r.value, r.source_col || r.source_row AS cell,
                   rep.filename, rep.sheet
            FROM reading r
            JOIN report rep ON rep.day = r.day
            WHERE r.station = ? AND r.time IN (?, ?, ?) AND r.metric IN ('P', 'Ia', 'Uab', 'cos')
            """,
            (station, prev_t, at, next_t),
        ).fetchall()

        by_time = defaultdict(lambda: defaultdict(dict))
        for r in rows:
            by_time[r["time"]][r["cabinet"]][r["metric"]] = {
                "value": r["value"],
                "cell": f'{r["filename"]} / {r["sheet"]}!{r["cell"]}',
            }

        cur = by_time.get(at)
        if not cur:
            return {"kind": "power_balance", "status": "missing", "time": at, "message": "库内无该整点读数。"}

        def p_val(t_map, cab):
            v = t_map.get(cab, {}).get("P", {}).get("value")
            return float(v) if v is not None else 0.0

        def ia_val(t_map, cab):
            v = t_map.get(cab, {}).get("Ia", {}).get("value")
            return float(v) if v is not None else 0.0

        main_chain = self.profile.main_chain_cabinets()
        incomers = list(self.profile.incomer_cabinets())
        tr_hvs = [t.hv_cabinet for t in self.profile.transformers if t.station == station]
        tr_lvs = [t.lv_cabinet for t in self.profile.transformers if t.station == station]
        spare_set = self.profile.spare_cabinets()
        lv_feeder_cabs = [
            f.cabinet for f in self.profile.feeders
            if f.station == station and f.role in ("feeder", "capacitor") and f.cabinet not in spare_set and f.cabinet in cur
        ]

        p_in = sum(p_val(cur, c) for c in incomers)
        p_hv = sum(p_val(cur, c) for c in tr_hvs)
        p_lv = sum(p_val(cur, c) for c in tr_lvs)
        p_feeders = sum(p_val(cur, c) for c in lv_feeder_cabs)

        d35 = round(p_in - p_hv, 2)
        dtr = round(p_hv - p_lv, 2)
        d10 = round(p_lv - p_feeders, 2)

        # Auto-evaluate every topology-derived power conservation equation
        derived_eqs = []
        for eq in self.profile.derive_balance_equations():
            inputs = {
                cab: cur.get(cab, {}).get("P", {})
                for cab in dict.fromkeys([*eq.lhs_cabinets, *eq.rhs_cabinets])
            }
            complete = all(reading.get("value") is not None for reading in inputs.values())
            lhs_sum = round(sum(p_val(cur, c) for c in eq.lhs_cabinets), 2)
            rhs_sum = round(sum(p_val(cur, c) for c in eq.rhs_cabinets), 2)
            diff_kw = round(lhs_sum - rhs_sum, 2)
            derived_eqs.append({
                "id": eq.id,
                "name": eq.name,
                "lhs_cabinets": eq.lhs_cabinets,
                "rhs_cabinets": eq.rhs_cabinets,
                "lhs_kw": lhs_sum,
                "rhs_kw": rhs_sum,
                "diff_kw": diff_kw,
                "threshold_kw": eq.threshold_kw,
                "balanced": abs(diff_kw) <= eq.threshold_kw,
                "inputs_complete": complete,
                "input_cells": [value["cell"] for value in inputs.values() if value.get("value") is not None],
            })

        # Check adjacent ramps for polling skew classification
        ramps = []
        for adj_t in (prev_t, next_t):
            adj = by_time.get(adj_t)
            if adj and all(c in adj for c in incomers + tr_hvs):
                ramps.append(abs(sum(p_val(adj, c) for c in incomers) - p_in))
                ramps.append(abs(sum(p_val(adj, c) for c in tr_hvs) - p_hv))
        max_ramp = round(max(ramps), 2) if ramps else 0.0

        flags = []
        zero_incomers = [c for c in incomers if p_val(cur, c) == 0.0 and ia_val(cur, c) == 0.0]
        active_incomers = [c for c in incomers if p_val(cur, c) > 50.0]
        if len(zero_incomers) >= 1 and len(active_incomers) == 1 and len(tr_hvs) >= 2:
            if all(p_val(cur, c) > 50.0 for c in tr_hvs) and abs(p_val(cur, active_incomers[0]) - p_hv) <= self.profile.balance_threshold_kw:
                flags.append("single_incomer_dual_transformer_suspected")
        neg_main = [c for c in main_chain if p_val(cur, c) < 0.0]
        if neg_main:
            flags.append("main_chain_reverse_power")
        if abs(d35) > self.profile.balance_threshold_kw or abs(dtr) > self.profile.balance_threshold_kw:
            if max_ramp >= self.profile.polling_ramp_kw or int(at[11:13]) in {0, 10, 13, 17}:
                flags.append("suspected_scada_polling_skew")
            else:
                flags.append("steady_state_balance_deviation")

        p_in0 = p_val(cur, incomers[0]) if len(incomers) >= 1 else 0.0
        p_in1 = p_val(cur, incomers[1]) if len(incomers) >= 2 else 0.0
        p_hv0 = p_val(cur, tr_hvs[0]) if len(tr_hvs) >= 1 else 0.0
        p_hv1 = p_val(cur, tr_hvs[1]) if len(tr_hvs) >= 2 else 0.0

        result = {
            "kind": "power_balance",
            "status": "ok",
            "station": station,
            "mine_id": self.profile.mine_id,
            "time": at,
            "main_chain": {
                c: {
                    "P_kw": cur[c]["P"]["value"],
                    "Ia_a": cur[c]["Ia"]["value"],
                    "Uab_kv": cur[c]["Uab"]["value"],
                    "cos": cur[c]["cos"]["value"],
                    "P_cell": cur[c]["P"]["cell"],
                }
                for c in main_chain
                if c in cur and "P" in cur[c]
            },
            "equations": derived_eqs,
            "sums_kw": {
                "incomers_total": round(p_in, 2),
                "transformers_hv_total": round(p_hv, 2),
                "transformers_lv_total": round(p_lv, 2),
                "reported_lv_feeders_sum": round(p_feeders, 2),
                "incomers_303_306": round(p_in, 2),
                "transformers_hv_311_312": round(p_hv, 2),
                "transformers_lv_1005_1006": round(p_lv, 2),
                "reported_10kv_feeders_sum": round(p_feeders, 2),
            },
            "diffs_kw": {
                "d35_in_minus_hv": d35,
                "dtr_hv_minus_lv": dtr,
                "d10_lv_minus_reported_feeders": d10,
                "p303_minus_p311": round(p_in0 - p_hv0, 2),
                "p306_minus_p312": round(p_in1 - p_hv1, 2),
            },
            "adjacent_max_ramp_kw": max_ramp,
            "flags": flags,
            "negative_main_cabinets": neg_main,
        }
        if section_cabinet is not None:
            feeder = self.profile.feeder_map().get(section_cabinet)
            if feeder is None or feeder.station != station or feeder.role not in ("feeder", "capacitor"):
                raise ValueError("母线分段核算须指定本站的出线柜或补偿柜。")
            incomer_cabs = [t.lv_cabinet for t in self.profile.transformers
                            if t.station == station and t.lv_bus == feeder.bus_id]
            outgoing_cabs = [f.cabinet for f in self.profile.feeders if f.station == station
                             and f.bus_id == feeder.bus_id and f.role in ("feeder", "capacitor")]
            inputs = [cur.get(c, {}).get("P", {}) for c in [*incomer_cabs, *outgoing_cabs]]
            section = {
                "bus_id": feeder.bus_id,
                "bus_label": self.profile.bus_map()[feeder.bus_id].name,
                "incomer_cabinets": incomer_cabs,
                "outgoing_cabinets": outgoing_cabs,
                "compared_cabinet": section_cabinet,
                "inputs_complete": bool(incomer_cabs) and all(r.get("value") is not None for r in inputs),
                "input_cells": [r["cell"] for r in inputs if r.get("value") is not None],
            }
            if section["inputs_complete"]:
                incoming_kw = sum(p_val(cur, c) for c in incomer_cabs)
                outgoing_kw = sum(p_val(cur, c) for c in outgoing_cabs)
                feeder_kw = p_val(cur, section_cabinet)
                section.update(
                    incomer_kw=round(incoming_kw, 2), outgoing_kw=round(outgoing_kw, 2),
                    cabinet_kw=feeder_kw, signed_residual_kw=round(incoming_kw - outgoing_kw, 2),
                    flipped_residual_kw=round(incoming_kw - outgoing_kw + 2 * feeder_kw, 2),
                    residual_change_kw=round(2 * feeder_kw, 2),
                )
            result["section_balance"] = section
        return result

    def get_cabinet_health_profile(self, cabinet: str) -> dict:
        cab = str(cabinet).strip()
        matches = [(st, num) for st, num in self.archive.devices if num == cab]
        if not matches:
            raise ValueError(f"老洼地台账中未找到柜号 {cab}。")
        station, number = matches[0]
        device = self.archive.devices[(station, number)]

        up = self.archive.supply(station, number, "upstream")
        down = self.archive.supply(station, number, "downstream")

        if station != REPORT_STATION:
            return {
                "kind": "cabinet_health_profile",
                "status": "ok",
                "device": device,
                "has_hourly_readings": False,
                "note": "该柜属于 1535 中央变电所，当前仅归档监控截图与站间图示关系，无小时级运行日报读数。",
                "supply_links": {"upstream": up.get("links", []), "downstream": down.get("links", [])},
            }

        report = self._health()
        rows = self.archive.db.execute(
            """
            SELECT r.metric, r.value, r.time, r.source_col || r.source_row AS cell, rep.filename, rep.sheet
            FROM reading r JOIN report rep ON rep.day = r.day
            WHERE r.station = ? AND r.cabinet = ?
            ORDER BY r.time
            """,
            (REPORT_STATION, number),
        ).fetchall()
        by_m = defaultdict(list)
        for r in rows:
            if r["value"] is not None:
                by_m[r["metric"]].append(r)

        metric_stats = {}
        for m in ("Uab", "Ia", "P", "cos"):
            items = by_m.get(m, [])
            if not items:
                continue
            vals = [x["value"] for x in items]
            min_r = min(items, key=lambda x: x["value"])
            max_r = max(items, key=lambda x: x["value"])
            metric_stats[m] = {
                "unit": METRICS[m][1],
                "valid_hours": len(items),
                "min": round(min_r["value"], 3),
                "min_time": min_r["time"],
                "min_cell": f'{min_r["filename"]} / {min_r["sheet"]}!{min_r["cell"]}',
                "max": round(max_r["value"], 3),
                "max_time": max_r["time"],
                "max_cell": f'{max_r["filename"]} / {max_r["sheet"]}!{max_r["cell"]}',
                "avg": round(sum(vals) / len(vals), 3),
                "zero_hours": sum(1 for v in vals if v == 0),
                "negative_hours": sum(1 for v in vals if v < 0),
            }

        related_findings = [
            {
                "id": f["id"],
                "severity": f["severity"],
                "dimension": f["dimension"],
                "title": f["title"],
                "summary": f["summary"],
                "evidence": f["evidence"][:3],
                "action": f["action"],
            }
            for f in report["findings"]
            if number in str(f["cabinet"]).split("/") or number in str(f["label"])
        ]

        return {
            "kind": "cabinet_health_profile",
            "status": "ok",
            "device": device,
            "has_hourly_readings": True,
            "metrics_7d": metric_stats,
            "metering_check": report["dimensions"]["metering"]["per_cabinet"].get(number),
            "voltage_check": report["dimensions"]["power_quality"]["voltage"].get(number),
            "power_factor_check": report["dimensions"]["power_quality"]["power_factor"].get(number),
            "supply_links": {"upstream": up.get("links", []), "downstream": down.get("links", [])},
            "related_findings": related_findings,
        }

    def get_diurnal_baseline(self, hour: str | None = None, day: str | None = None) -> dict:
        report = self._health()
        diurnal = report["dimensions"]["diurnal_baseline"]
        baseline = diurnal["hourly_baseline"]
        outliers = diurnal["outlier_hours"]

        result = {
            "kind": "diurnal_baseline",
            "status": "ok",
            "hourly_baseline": baseline,
            "outlier_hours": outliers,
        }
        if hour is not None:
            norm_h = hour.strip()
            if len(norm_h) <= 2 and norm_h.isdigit():
                norm_h = f"{int(norm_h):02d}:00"
            if norm_h not in baseline:
                raise ValueError("小时格式应为 HH:00（00:00 至 23:00）。")
            result["hourly_baseline"] = {norm_h: baseline[norm_h]}
            result["outlier_hours"] = [o for o in outliers if o["hour"] == norm_h]

        if day is not None:
            iso_day = date.fromisoformat(day).isoformat()
            rows = self.archive.db.execute(
                """
                SELECT r.time, r.cabinet, r.value, r.source_col || r.source_row AS cell, rep.filename, rep.sheet
                FROM reading r JOIN report rep ON rep.day = r.day
                WHERE r.station = ? AND r.day = ? AND r.cabinet IN ('303', '306') AND r.metric = 'P'
                ORDER BY r.time
                """,
                (REPORT_STATION, iso_day),
            ).fetchall()
            if not rows:
                raise ValueError(f"库内无 {iso_day} 的日报数据。")
            by_t = defaultdict(dict)
            for r in rows:
                by_t[r["time"]][r["cabinet"]] = r
            day_series = []
            for t in sorted(by_t):
                hh = f"{t[11:13]}:00"
                if hour is not None and hh not in result["hourly_baseline"]:
                    continue
                p303, p306 = by_t[t].get("303"), by_t[t].get("306")
                if p303 is None or p306 is None or p303["value"] is None or p306["value"] is None:
                    continue
                med = baseline[hh]["median_kw"]
                if med is None:
                    continue
                tot = round(p303["value"] + p306["value"], 2)
                day_series.append({
                    "time": t,
                    "total_p_kw": tot,
                    "baseline_median_kw": med,
                    "deviation_kw": round(tot - med, 2),
                    "cells": [
                        f'{p303["filename"]} / {p303["sheet"]}!{p303["cell"]}',
                        f'{p306["filename"]} / {p306["sheet"]}!{p306["cell"]}',
                    ],
                })
            result["day_comparison"] = {"day": iso_day, "hours": day_series}

        return result

    def search_coal_standards(self, query: str, top_k: int = 4) -> dict:
        return search_standards(query, top_k=top_k)

    def retrieve_conditional_evidence(
        self,
        query: str,
        cabinet: str | None = None,
        day: str | None = None,
        station: str | None = None,
        top_k: int = 3,
    ) -> dict:
        from laowadi.rag import ConditionalRAGEngine

        engine = ConditionalRAGEngine(self.archive)
        res = engine.retrieve(query, station=station, cabinet=cabinet, day=day, top_k=max(1, min(int(top_k), 5)))
        return {"kind": "conditional_rag", "status": "ok", **res}

    def verify_conditional_capacity(
        self,
        rated_303_a: float | None = None,
        rated_tr1_kva: float | None = None,
        rated_1009_a: float | None = None,
        derating_factor: float | None = None,
        switch_log_20261113: str | None = None,
        tie_switch_mode: str | None = None,
        source_doc: str = "会话内用户补充参数",
    ) -> dict:
        from laowadi.supplement import apply_supplement, build_baseline_verification

        v1 = build_baseline_verification(self.archive)
        params = {}
        if rated_303_a is not None:
            params["303_rated_current_a"] = float(rated_303_a)
        if rated_tr1_kva is not None:
            params["main_transformer_rated_kva"] = float(rated_tr1_kva)
        if rated_1009_a is not None:
            params["1009_1010_rated_current_a"] = float(rated_1009_a)
        if derating_factor is not None:
            params["ambient_derating_factor"] = float(derating_factor)
        if switch_log_20261113:
            params["switch_log_20261113"] = str(switch_log_20261113)
        if tie_switch_mode:
            params["c1535_tie_switch_mode"] = str(tie_switch_mode)

        if not params:
            return {"kind": "conditional_capacity", "status": "ok", "version": 1, "baseline_v1": v1}

        packet = {"station": REPORT_STATION, "source_doc": source_doc, "parameters": params}
        v2 = apply_supplement(v1, packet)
        return {
            "kind": "conditional_capacity",
            "status": "ok",
            "version": 2,
            "parent_run_id": v2["parent_run_id"],
            "run_id": v2["run_id"],
            "summary": v2["summary"],
            "items": v2["items"],
        }

    def query_electrical_analytics(
        self,
        operation: str,
        metric: str = "P",
        group_by: str = "cabinet",
        agg: str = "avg",
        order: str = "desc",
        top_k: int = 5,
        cabinets: list[str] | None = None,
        day: str | None = None,
        start_day: str | None = None,
        end_day: str | None = None,
        exclude_spares: bool = True,
        voltage_level: str = "all",
        conditions: list[dict] | None = None,
        combine_cabinets_per_hour: bool = False,
    ) -> dict:
        if operation not in {"aggregate_rank", "joint_filter"}:
            raise ValueError("operation 须为 aggregate_rank 或 joint_filter。")
        if order not in {"desc", "asc"}:
            raise ValueError("order 须为 desc 或 asc。")
        if voltage_level not in {"all", "10kV", "35kV"}:
            raise ValueError("voltage_level 须为 all、10kV 或 35kV。")
        top_k = max(1, min(int(top_k), 20))

        valid_s35 = {num for st, num in self.archive.devices if st == REPORT_STATION}
        explicit_cabs = None
        if cabinets is not None:
            explicit_cabs = list(dict.fromkeys(str(c).strip() for c in cabinets if str(c).strip()))
            if not explicit_cabs:
                explicit_cabs = None
            else:
                unknown = [c for c in explicit_cabs if c not in valid_s35]
                if unknown:
                    raise ValueError(f"地面 35 kV 日报台账中无以下柜号（井下 C1535 柜无小时日报）：{', '.join(unknown)}")

        iso_day = date.fromisoformat(day).isoformat() if day else None
        iso_start = date.fromisoformat(start_day).isoformat() if start_day else None
        iso_end = date.fromisoformat(end_day).isoformat() if end_day else None
        if iso_start and iso_end and iso_end < iso_start:
            raise ValueError("end_day 不能早于 start_day。")
        effective_days = [
            archived_day for archived_day in self.archive.days()
            if (archived_day == iso_day if iso_day else
                (not iso_start or iso_start <= archived_day) and (not iso_end or archived_day <= iso_end))
        ]
        if not effective_days:
            return {"kind": "electrical_analytics", "status": "missing", "operation": operation, "rows": []}

        where_clauses = ["r.station = ?", "r.value IS NOT NULL"]
        params: list = [REPORT_STATION]
        if iso_day:
            where_clauses.append("r.day = ?")
            params.append(iso_day)
        else:
            if iso_start:
                where_clauses.append("r.day >= ?")
                params.append(iso_start)
            if iso_end:
                where_clauses.append("r.day <= ?")
                params.append(iso_end)

        if explicit_cabs:
            c_ph = ",".join("?" for _ in explicit_cabs)
            where_clauses.append(f"r.cabinet IN ({c_ph})")
            params.extend(explicit_cabs)
        else:
            if voltage_level == "10kV":
                where_clauses.append("CAST(r.cabinet AS INTEGER) >= 1000")
            elif voltage_level == "35kV":
                where_clauses.append("CAST(r.cabinet AS INTEGER) < 1000")
            if exclude_spares:
                sp = sorted(SPARE_CABINETS)
                s_ph = ",".join("?" for _ in sp)
                where_clauses.append(f"r.cabinet NOT IN ({s_ph})")
                params.extend(sp)

        if operation == "aggregate_rank":
            if metric not in METRICS:
                raise ValueError(f"不支持的电气量：{metric}")
            if group_by not in {"cabinet", "day", "hour"}:
                raise ValueError("group_by 须为 cabinet、day 或 hour。")
            if agg not in {"sum", "avg", "min", "max", "std", "range", "count"}:
                raise ValueError("agg 须为 sum、avg、min、max、std、range 或 count。")

            where_clauses.append("r.metric = ?")
            params.append(metric)
            sql = (
                "SELECT r.cabinet, r.day, r.time, r.value, r.source_col || r.source_row AS cell, "
                "rep.filename, rep.sheet "
                "FROM reading r JOIN report rep ON rep.day = r.day "
                f"WHERE {' AND '.join(where_clauses)} "
                "ORDER BY r.time, CAST(r.cabinet AS INTEGER)"
            )
            rows = self.archive.db.execute(sql, params).fetchall()
            if not rows:
                return {"kind": "electrical_analytics", "status": "missing", "operation": operation, "rows": []}

            combine_requested_cabinets = bool(combine_cabinets_per_hour and group_by in {"day", "hour"} and explicit_cabs)
            if combine_requested_cabinets:
                by_time = defaultdict(list)
                for r in rows:
                    by_time[r["time"]].append(r)
                req_cab_set = set(explicit_cabs)
                combined_rows = []
                for t_str, t_items in sorted(by_time.items()):
                    if {x["cabinet"] for x in t_items} >= req_cab_set:
                        tot_v = round(sum(float(x["value"]) for x in t_items), 3)
                        first = t_items[0]
                        combined_rows.append({
                            "cabinet": "+".join(explicit_cabs),
                            "day": first["day"],
                            "time": t_str,
                            "value": tot_v,
                            "cell": first["cell"],
                            "filename": first["filename"],
                            "sheet": first["sheet"],
                            "cells": [f'{x["filename"]} / {x["sheet"]}!{x["cell"]}' for x in t_items],
                        })
                rows_iter = combined_rows
            else:
                rows_iter = rows

            groups = defaultdict(list)
            for r in rows_iter:
                if group_by == "cabinet":
                    gkey = r["cabinet"]
                elif group_by == "day":
                    gkey = r["day"]
                else:
                    gkey = f"{r['time'][11:13]}:00"
                groups[gkey].append(r)

            ranked_all = []
            for gkey, items in groups.items():
                vals = [x["value"] for x in items]
                max_r = max(items, key=lambda x: x["value"])
                min_r = min(items, key=lambda x: x["value"])
                v_sum = round(sum(vals), 2)
                v_avg = round(sum(vals) / len(vals), 2 if combine_cabinets_per_hour else 3)
                v_min = round(min_r["value"], 3)
                v_max = round(max_r["value"], 3)
                v_std = round(statistics.pstdev(vals), 3) if len(vals) >= 2 else 0.0
                v_range = round(v_max - v_min, 3)
                stats_map = {
                    "sum": v_sum,
                    "avg": v_avg,
                    "min": v_min,
                    "max": v_max,
                    "std": v_std,
                    "range": v_range,
                    "count": len(vals),
                }
                entry = {
                    "group": gkey,
                    "label": (
                        self.archive.devices[(REPORT_STATION, gkey)]["label"]
                        if group_by == "cabinet"
                        else (f"{gkey} 同期合计({'+'.join(explicit_cabs)})" if combine_requested_cabinets else gkey)
                    ),
                    "score": stats_map[agg],
                    "stats": stats_map,
                    "max_time": max_r["time"],
                    "max_cell": f'{max_r["filename"]} / {max_r["sheet"]}!{max_r["cell"]}',
                    "min_time": min_r["time"],
                    "min_cell": f'{min_r["filename"]} / {min_r["sheet"]}!{min_r["cell"]}',
                }
                if isinstance(max_r, dict) and max_r.get("cells"):
                    entry["max_cells"] = max_r["cells"]
                if isinstance(min_r, dict) and min_r.get("cells"):
                    entry["min_cells"] = min_r["cells"]
                if group_by != "cabinet":
                    entry["max_cabinet"] = max_r["cabinet"]
                    entry["min_cabinet"] = min_r["cabinet"]
                ranked_all.append(entry)

            ranked_all.sort(key=lambda x: (x["score"], x["group"]), reverse=(order == "desc"))
            top_items = ranked_all[:top_k]
            for idx, item in enumerate(top_items, 1):
                item["rank"] = idx

            unit = METRICS[metric][1]
            agg_unit = "kWh (按小时整点有功矩形积分近似)" if (metric == "P" and agg == "sum") else unit
            if agg == "count":
                agg_unit = "点"
            comparison = None
            if len(top_items) == 2 and agg in {"sum", "avg", "std", "max", "min", "range"}:
                a, b = sorted(top_items, key=lambda row: row["score"], reverse=True)
                diff = round(a["score"] - b["score"], 3)
                ratio = round(a["score"] / b["score"], 3) if b["score"] not in (0, 0.0) else None
                comparison = {
                    "higher": a["group"],
                    "lower": b["group"],
                    "diff": diff,
                    "ratio": ratio,
                }

            return {
                "kind": "electrical_analytics",
                "status": "ok",
                "operation": "aggregate_rank",
                "query_spec": {
                    "metric": metric,
                    "unit": unit,
                    "agg": agg,
                    "agg_unit": agg_unit,
                    "group_by": group_by,
                    "order": order,
                    "day": iso_day,
                    "start_day": iso_start,
                    "end_day": iso_end,
                    "effective_date_range": [effective_days[0], effective_days[-1]],
                    "cabinets": explicit_cabs,
                    "combine_cabinets_per_hour": combine_requested_cabinets,
                    "voltage_level": voltage_level,
                    "exclude_spares": exclude_spares if not explicit_cabs else False,
                },
                "total_groups": len(ranked_all),
                "comparison": comparison,
                "results": top_items,
            }

        # operation == "joint_filter"
        if not conditions or not isinstance(conditions, list) or len(conditions) > 4:
            raise ValueError("joint_filter 须提供 1 至 4 条联合筛选条件。")

        ops = {
            ">": lambda a, b: a > b,
            ">=": lambda a, b: a >= b,
            "<": lambda a, b: a < b,
            "<=": lambda a, b: a <= b,
            "=": lambda a, b: math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6),
            "!=": lambda a, b: not math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6),
        }
        norm_conds = []
        needed_metrics = []
        for cond in conditions:
            if not isinstance(cond, dict):
                raise ValueError("条件项必须为对象。")
            cm = cond.get("metric")
            cop = cond.get("op")
            cval = cond.get("value")
            if cm not in METRICS:
                raise ValueError(f"条件包含不支持的电气量：{cm}")
            if cop not in ops:
                raise ValueError(f"不支持的比较运算符：{cop}")
            if not isinstance(cval, (int, float)) or isinstance(cval, bool):
                raise ValueError(f"条件阈值必须为数值：{cval}")
            norm_conds.append({"metric": cm, "op": cop, "value": float(cval)})
            if cm not in needed_metrics:
                needed_metrics.append(cm)

        m_ph = ",".join("?" for _ in needed_metrics)
        where_clauses.append(f"r.metric IN ({m_ph})")
        params.extend(needed_metrics)
        sql = (
            "SELECT r.cabinet, r.time, r.metric, r.value, r.source_col || r.source_row AS cell, "
            "rep.filename, rep.sheet "
            "FROM reading r JOIN report rep ON rep.day = r.day "
            f"WHERE {' AND '.join(where_clauses)} "
            "ORDER BY r.time, CAST(r.cabinet AS INTEGER)"
        )
        rows = self.archive.db.execute(sql, params).fetchall()
        frames = defaultdict(dict)
        for r in rows:
            frames[(r["cabinet"], r["time"])][r["metric"]] = {
                "value": r["value"],
                "cell": f'{r["filename"]} / {r["sheet"]}!{r["cell"]}',
            }

        matches = []
        by_cab_counts = defaultdict(int)
        for (cab, t_str), m_map in frames.items():
            ok = True
            for c in norm_conds:
                entry = m_map.get(c["metric"])
                if entry is None or not ops[c["op"]](entry["value"], c["value"]):
                    ok = False
                    break
            if ok:
                by_cab_counts[cab] += 1
                matches.append({
                    "cabinet": cab,
                    "label": self.archive.devices[(REPORT_STATION, cab)]["label"],
                    "time": t_str,
                    "readings": {m: m_map[m] for m in needed_metrics},
                    "cells": [m_map[m]["cell"] for m in needed_metrics],
                })

        # Sort by extremity of the primary condition so the most severe joint cases appear first
        first_c = norm_conds[0]
        sort_desc = first_c["op"] in {">", ">="} if order == "desc" else first_c["op"] in {"<", "<="}
        matches.sort(
            key=lambda item: (item["readings"][first_c["metric"]]["value"], item["time"]),
            reverse=sort_desc,
        )
        cab_summary = [
            {
                "cabinet": cab,
                "label": self.archive.devices[(REPORT_STATION, cab)]["label"],
                "matched_hours": cnt,
            }
            for cab, cnt in sorted(by_cab_counts.items(), key=lambda x: (-x[1], int(x[0])))
        ]
        return {
            "kind": "electrical_analytics",
            "status": "ok",
            "operation": "joint_filter",
            "query_spec": {
                "conditions": norm_conds,
                "day": iso_day,
                "start_day": iso_start,
                "end_day": iso_end,
                "effective_date_range": [effective_days[0], effective_days[-1]],
                "cabinets": explicit_cabs,
                "voltage_level": voltage_level,
                "exclude_spares": exclude_spares if not explicit_cabs else False,
            },
            "total_matched_rows": len(matches),
            "matched_cabinets_summary": cab_summary,
            "results": matches[:top_k],
        }

    def simulate_electrical_engineering(
        self,
        operation: str,
        cabinet: str | None = None,
        target_cos: float = 0.90,
        outage_cabinet: str | None = None,
        receiving_cabinet: str | None = None,
        day: str | None = None,
        rated_current_a: float | None = None,
        derating_factor: float = 0.90,
    ) -> dict:
        if operation not in {"reactive_compensation", "n1_transfer"}:
            raise ValueError("operation 须为 reactive_compensation 或 n1_transfer。")
        iso_day = date.fromisoformat(day).isoformat() if day else None
        valid_s35 = {num for st, num in self.archive.devices if st == REPORT_STATION}

        if operation == "reactive_compensation":
            cab = str(cabinet or "1010").strip()
            if cab not in valid_s35:
                raise ValueError(f"地面 35 kV 日报台账中无柜号 {cab}。")
            target_cos = float(target_cos)
            if not 0.75 <= target_cos <= 0.99:
                raise ValueError("target_cos 须在 0.75 至 0.99 之间。")

            where = "r.station = ? AND r.cabinet = ?" + (" AND r.day = ?" if iso_day else "")
            params = (REPORT_STATION, cab, iso_day) if iso_day else (REPORT_STATION, cab)
            rows = self.archive.db.execute(
                f"""
                SELECT r.time, r.metric, r.value, r.source_col || r.source_row AS cell, rep.filename, rep.sheet
                FROM reading r JOIN report rep ON rep.day = r.day
                WHERE {where} AND r.value IS NOT NULL
                ORDER BY r.time
                """,
                params,
            ).fetchall()
            frames = defaultdict(dict)
            for r in rows:
                frames[r["time"]][r["metric"]] = {
                    "value": r["value"],
                    "cell": f'{r["filename"]} / {r["sheet"]}!{r["cell"]}',
                }

            neg_hours = sum(1 for m in frames.values() if (m.get("P", {}).get("value") or 0) < 0)
            if neg_hours > len(frames) * 0.5:
                return {
                    "kind": "electrical_simulation",
                    "status": "warning",
                    "operation": "reactive_compensation",
                    "cabinet": cab,
                    "label": self.archive.devices[(REPORT_STATION, cab)]["label"],
                    "message": (
                        f"{cab} 柜存在 {neg_hours}/{len(frames)} 小时持续负有功/疑似 CT 极性反接，在二次接线未核实前拒绝直接按负读数设计无功补偿容量。"
                    ),
                }

            tan_target = math.tan(math.acos(target_cos))
            tan_095 = math.tan(math.acos(0.95))
            samples = []
            for t_str, m in sorted(frames.items()):
                p_e, cos_e, ia_e, u_e = m.get("P"), m.get("cos"), m.get("Ia"), m.get("Uab")
                if not (p_e and cos_e and ia_e and u_e):
                    continue
                p_v, c_v, ia_v, u_v = p_e["value"], cos_e["value"], ia_e["value"], u_e["value"]
                if p_v <= 10.0 or not (0.15 < c_v < 0.999):
                    continue
                tan1 = math.tan(math.acos(c_v))
                qc = max(0.0, p_v * (tan1 - tan_target))
                qc_095 = max(0.0, p_v * (tan1 - tan_095))
                i_drop = max(0.0, (1.0 - c_v / target_cos) * 100.0) if c_v < target_cos else 0.0
                loss_drop = max(0.0, (1.0 - (c_v / target_cos) ** 2) * 100.0) if c_v < target_cos else 0.0
                samples.append({
                    "time": t_str,
                    "P_kw": p_v,
                    "cos": c_v,
                    "Ia_a": ia_v,
                    "Uab_kv": u_v,
                    "Q1_kvar": round(p_v * tan1, 2),
                    "Qc_kvar": round(qc, 2),
                    "Qc_095_kvar": round(qc_095, 2),
                    "current_reduction_pct": round(i_drop, 2),
                    "loss_reduction_pct": round(loss_drop, 2),
                    "cells": [p_e["cell"], cos_e["cell"], ia_e["cell"]],
                })

            if not samples:
                return {
                    "kind": "electrical_simulation",
                    "status": "missing",
                    "operation": "reactive_compensation",
                    "cabinet": cab,
                    "message": f"{cab} 柜在所选时段无有效正向带载样本。",
                }

            below_samples = [s for s in samples if s["cos"] < target_cos]
            qc_vals = sorted(s["Qc_kvar"] for s in samples)
            p95_idx = min(len(qc_vals) - 1, int(math.ceil(0.95 * len(qc_vals))) - 1)
            peak_s = max(samples, key=lambda x: x["Qc_kvar"])
            min_cos_s = min(samples, key=lambda x: x["cos"])
            avg_qc = round(sum(s["Qc_kvar"] for s in samples) / len(samples), 2)
            avg_qc_095 = round(sum(s["Qc_095_kvar"] for s in samples) / len(samples), 2)
            avg_i_drop = round(sum(s["current_reduction_pct"] for s in below_samples) / max(len(below_samples), 1), 2)
            avg_loss_drop = round(sum(s["loss_reduction_pct"] for s in below_samples) / max(len(below_samples), 1), 2)
            avg_cos = round(sum(s["cos"] for s in samples) / len(samples), 3)

            return {
                "kind": "electrical_simulation",
                "status": "ok",
                "operation": "reactive_compensation",
                "formula": "Qc = P * (tan(arccos(cosφ1)) - tan(arccos(cosφ2))); ΔP_loss% = (1 - (cosφ1/cosφ2)^2) * 100%",
                "cabinet": cab,
                "label": self.archive.devices[(REPORT_STATION, cab)]["label"],
                "scope_day": iso_day or "2026-11-10 至 2026-11-16",
                "target_cos": target_cos,
                "loaded_hours": len(samples),
                "below_target_hours": len(below_samples),
                "avg_measured_cos": avg_cos,
                "required_compensation_kvar": {
                    "peak_kvar": peak_s["Qc_kvar"],
                    "p95_kvar": round(qc_vals[p95_idx], 2),
                    "avg_kvar": avg_qc,
                    "peak_at_target_095_kvar": peak_s["Qc_095_kvar"],
                    "avg_at_target_095_kvar": avg_qc_095,
                },
                "expected_benefits_on_below_hours": {
                    "avg_current_reduction_pct": avg_i_drop,
                    "avg_line_loss_reduction_pct": avg_loss_drop,
                },
                "peak_demand_hour": peak_s,
                "worst_cos_hour": min_cos_s,
                "missing_field_evidence": [
                    "现场现有 10 kV 电容器组/SVG 额定容量与分组级差",
                    "目标回路谐波畸变率（THD）实测数据（防范串联谐振与过补偿）",
                ],
            }

        # operation == "n1_transfer"
        c_out = str(outage_cabinet or "1009").strip()
        c_rec = str(receiving_cabinet or "1010").strip()
        for c in (c_out, c_rec):
            if c not in valid_s35:
                raise ValueError(f"地面 35 kV 日报台账中无柜号 {c}。")
        if c_out == c_rec:
            raise ValueError("退出回路与受电转供回路不能为同一柜号。")
        derating_factor = float(derating_factor)
        if not 0.5 <= derating_factor <= 1.2:
            raise ValueError("derating_factor 须在 0.5 至 1.2 之间。")

        where = "r.station = ? AND r.cabinet IN (?, ?)" + (" AND r.day = ?" if iso_day else "")
        params = (REPORT_STATION, c_out, c_rec, iso_day) if iso_day else (REPORT_STATION, c_out, c_rec)
        rows = self.archive.db.execute(
            f"""
            SELECT r.time, r.cabinet, r.metric, r.value, r.source_col || r.source_row AS cell, rep.filename, rep.sheet
            FROM reading r JOIN report rep ON rep.day = r.day
            WHERE {where} AND r.value IS NOT NULL
            ORDER BY r.time
            """,
            params,
        ).fetchall()
        by_t = defaultdict(lambda: defaultdict(dict))
        for r in rows:
            by_t[r["time"]][r["cabinet"]][r["metric"]] = {
                "value": r["value"],
                "cell": f'{r["filename"]} / {r["sheet"]}!{r["cell"]}',
            }

        sim_hours = []
        for t_str, c_map in sorted(by_t.items()):
            if c_out not in c_map or c_rec not in c_map:
                continue
            m1, m2 = c_map[c_out], c_map[c_rec]
            if not ({"P", "Ia", "Uab", "cos"} <= m1.keys() and {"P", "Ia", "Uab", "cos"} <= m2.keys()):
                continue
            p1, i1, u1, c1 = m1["P"]["value"], m1["Ia"]["value"], m1["Uab"]["value"], m1["cos"]["value"]
            p2, i2, u2, c2 = m2["P"]["value"], m2["Ia"]["value"], m2["Uab"]["value"], m2["cos"]["value"]
            c1_clamped = max(0.15, min(abs(c1), 1.0))
            c2_clamped = max(0.15, min(abs(c2), 1.0))
            q1 = abs(p1) * math.tan(math.acos(c1_clamped))
            q2 = abs(p2) * math.tan(math.acos(c2_clamped))
            p_comb = round(p1 + p2, 2)
            q_comb = round(q1 + q2, 2)
            s_comb = round(math.hypot(p_comb, q_comb), 2)
            i_alg = round(i1 + i2, 2)
            u_ref = u2 if u2 > 1.0 else (u1 if u1 > 1.0 else 10.0)
            i_phasor = round(s_comb / (math.sqrt(3.0) * u_ref), 2)
            sim_hours.append({
                "time": t_str,
                "outage_P_kw": p1,
                "outage_Ia_a": i1,
                "receiving_P_kw": p2,
                "receiving_Ia_a": i2,
                "combined_P_kw": p_comb,
                "combined_Q_kvar": q_comb,
                "combined_S_kva": s_comb,
                "combined_Ia_algebraic_a": i_alg,
                "combined_Ia_phasor_a": i_phasor,
                "receiving_Uab_kv": u2,
                "cells": [m1["Ia"]["cell"], m2["Ia"]["cell"], m1["P"]["cell"], m2["P"]["cell"]],
            })

        if not sim_hours:
            return {"kind": "electrical_simulation", "status": "missing", "operation": "n1_transfer", "message": "无同期双柜有效读数。"}

        worst_i = max(sim_hours, key=lambda x: x["combined_Ia_algebraic_a"])
        worst_p = max(sim_hours, key=lambda x: x["combined_P_kw"])
        avg_i_comb = round(sum(x["combined_Ia_algebraic_a"] for x in sim_hours) / len(sim_hours), 2)

        capacity_eval = None
        missing_params = []
        if rated_current_a is not None:
            rated_a = float(rated_current_a)
            eff_a = round(rated_a * derating_factor, 2)
            ratio = round(worst_i["combined_Ia_algebraic_a"] / eff_a * 100.0, 2)
            margin = round(100.0 - ratio, 2)
            capacity_eval = {
                "rated_current_a": rated_a,
                "derating_factor": derating_factor,
                "effective_limit_a": eff_a,
                "n1_load_ratio_pct": ratio,
                "n1_margin_pct": margin,
                "verdict": "conditionally_satisfied" if ratio <= 100.0 else "conditionally_violated",
            }
        else:
            missing_params = [
                f"{c_rec} 回路电缆/断路器长期允许载流量 (A)",
                "井下/站内联络开关倒闸方式与闭锁条件确认记录",
            ]

        return {
            "kind": "electrical_simulation",
            "status": "ok",
            "operation": "n1_transfer",
            "formula": "I_comb_upper = Ia_out + Ia_rec; S_comb = sqrt((P1+P2)^2 + (Q1+Q2)^2)",
            "outage_cabinet": {"number": c_out, "label": self.archive.devices[(REPORT_STATION, c_out)]["label"]},
            "receiving_cabinet": {"number": c_rec, "label": self.archive.devices[(REPORT_STATION, c_rec)]["label"]},
            "scope_day": iso_day or "2026-11-10 至 2026-11-16",
            "hours_simulated": len(sim_hours),
            "avg_combined_Ia_a": avg_i_comb,
            "worst_current_hour": worst_i,
            "worst_power_hour": worst_p,
            "capacity_evaluation": capacity_eval,
            "missing_field_evidence": missing_params,
        }

    def run_ac_power_flow(
        self,
        scenario: str = "base_case",
        at_time: str | None = None,
        transformer_sn_mva: float | None = None,
        transformer_vk_percent: float | None = None,
        tr1_tap_pos: int | None = None,
        tr2_tap_pos: int | None = None,
        cable_length_km: float | None = None,
        outage_mode: str = "feeder_1009",
        target_cabinet: str = "1010",
        qc_kvar: float | None = None,
    ) -> dict:
        try:
            from laowadi.powerflow import run_ac_power_flow as _solve_pf
        except ImportError as exc:
            return {
                "kind": "ac_power_flow",
                "status": "error",
                "error": f"当前 Python 运行环境未安装 pandapower（{exc}），请使用 .venv-power 环境启动工作台。",
            }
        return _solve_pf(
            self.archive,
            scenario=scenario,
            at_time=at_time,
            transformer_sn_mva=transformer_sn_mva,
            transformer_vk_percent=transformer_vk_percent,
            tr1_tap_pos=tr1_tap_pos,
            tr2_tap_pos=tr2_tap_pos,
            cable_length_km=cable_length_km,
            outage_mode=outage_mode,
            target_cabinet=target_cabinet,
            qc_kvar=qc_kvar,
        )

    def run_python_analysis(self, code: str, timeout_s: float = 6.0) -> dict:
        from laowadi.code_sandbox import run_python_analysis as _run_sandbox

        res = _run_sandbox(self.archive, code, timeout_s=timeout_s)
        return {"kind": "python_analysis", **res}
