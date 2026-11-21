"""Deterministic local diagnostic router, query parsers, and UI artifact hint inference.

Extracted from laowadi/diagnose.py to keep the core ReAct/Reflection orchestrator slim and modular.
Supports all 11 compound read-only diagnostic tools, including Tool 11 (`run_ac_power_flow`).
"""

from __future__ import annotations

import re

from laowadi.diag_tools import DiagnosticToolbox
from laowadi.storage import REPORT_STATION
from laowadi.report_evidence import capacity_fact, render_tool_facts
from laowadi.verifier import bind_tool_fact, build_observations


_JOINT_COND_RE = re.compile(
    r"(Uab|线电压|电压|Ia|电流|cos|功率因数|(?<![A-Za-z])P(?![A-Za-z])|有功)"
    r"\s*(>=|<=|!=|>|<|=|大于等于|小于等于|高于|大于|超过|低于|小于|等于)\s*"
    r"(-?\d+(?:\.\d+)?)",
    re.I,
)


def _parse_capacity_params(question: str) -> dict:
    """Extract optional engineering rating parameters from natural language questions."""
    params = {}
    m_tr = re.search(r"(\d{3,5}(?:\.\d+)?)\s*kVA", question, re.I)
    if m_tr:
        params["rated_tr1_kva"] = float(m_tr.group(1))
    m_derate = re.search(r"降容[^\d]{0,6}(0\.\d+|1(?:\.\d+)?)", question)
    if m_derate:
        params["derating_factor"] = float(m_derate.group(1))
    m_303 = re.search(r"303[^\d]{0,12}(\d{2,4}(?:\.\d+)?)\s*A\b", question, re.I)
    if m_303:
        params["rated_303_a"] = float(m_303.group(1))
    m_1009 = re.search(r"(?:1009|1010|下井电缆)[^\d]{0,12}(\d{2,4}(?:\.\d+)?)\s*A\b", question, re.I)
    if m_1009:
        params["rated_1009_a"] = float(m_1009.group(1))
    if any(k in question for k in ("母联合", "306检修", "计划停电", "倒闸确认", "合位")):
        params["switch_log_20261113"] = "用户会话内确认：35kV母联300开关合位，306柜检修退出"
    if any(k in question for k in ("1200联络", "热备用", "自动切换")):
        params["tie_switch_mode"] = "用户会话内确认：1535中央变电所1200联络开关热备用可自动切换"
    return params


def _infer_ui_artifact_hint(question: str, anchors: dict, trace: list[dict]) -> dict:
    """Compute a Generative UI hint so the Agent Workspace Right Canvas syncs automatically."""
    tool_names = [step.get("tool") for step in trace]
    cabs = anchors.get("cabinets") or []
    days = anchors.get("days") or []
    hours = anchors.get("hours") or []

    if "run_ac_power_flow" in tool_names or any(k in question for k in ("潮流", "pandapower", "牛顿拉夫逊", "分接头", "调压档位")):
        return {"canvas": "powerflow", "focus_cabinet": cabs[0] if cabs else "1006", "replay_index": 89 if "2026-11-13" in days else None}

    if "verify_conditional_capacity" in tool_names or any(k in question for k in ("补证", "状态图", "复算", "过载", "载流量", "kVA")):
        if any(k in question for k in ("kVA", "额定", "补证", "状态图", "复算", "降容")):
            return {"canvas": "graph", "focus_cabinet": cabs[0] if cabs else "303", "replay_index": 87 if "2026-11-13" in days else None}

    if any(k in question for k in ("评测", "消融", "RAG", "准确率", "幻觉", "连续流")):
        return {"canvas": "eval", "focus_cabinet": None, "replay_index": None}

    replay_index = None
    if "2026-11-13" in days and ("过压" in question or "11.01" in question or "17:00" in hours):
        replay_index = 89
    elif "2026-11-13" in days:
        replay_index = 87
    elif "2026-11-12" in days and ("12:00" in hours or "反向" in question or "312" in cabs or "1006" in cabs or "1027" in cabs):
        replay_index = 60
    elif "2026-11-10" in days and ("12:00" in hours or "LW-000010" in question):
        replay_index = 12
    elif "过压" in question or "11.01" in question:
        replay_index = 89
    elif "1018" in cabs:
        replay_index = 165

    if len(cabs) == 1 and not days and cabs[0] in ("1018", "1012", "1020", "1010", "1009", "1006", "1005", "311", "312", "1027"):
        return {"canvas": "cabinet", "focus_cabinet": cabs[0], "replay_index": replay_index}

    return {
        "canvas": "topology",
        "focus_cabinet": cabs[0] if cabs else None,
        "replay_index": replay_index,
    }


def _summarize_tool_result(tool_name: str, result: dict) -> str:
    """Return a concise one-line Chinese summary for live SSE execution cards."""
    if not isinstance(result, dict):
        return "已完成"
    if result.get("status") == "error":
        return f"参数提示：{str(result.get('error', ''))[:60]}"
    if result.get("status") == "duplicate_skipped":
        return "复用已查回执"
    if tool_name == "get_station_health_overview":
        n = len(result.get("findings") or [])
        return f"扫描 25 柜，返回 {n} 条分级健康发现"
    if tool_name == "get_window_snapshot":
        return f"提取 {result.get('hour_count', 0)} 个整点 × {len(result.get('cabinets') or {})} 柜读数"
    if tool_name == "check_power_balance":
        s = result.get("sums_kw") or {}
        return f"进线 {s.get('incomers_303_306', '-')} kW / 标记 {result.get('flags') or ['正常']}"
    if tool_name == "get_cabinet_health_profile":
        dev = result.get("device") or {}
        if not result.get("has_hourly_readings"):
            return f"{dev.get('number', '')} 柜无日报读数（井下站边界）"
        return f"完成 {dev.get('number', '')} 柜 168h 四维画像"
    if tool_name == "get_diurnal_baseline":
        return f"对照 24h 分时基线，命中 {len(result.get('outlier_hours') or [])} 个反常点"
    if tool_name == "search_coal_standards":
        return f"检索命中 {len(result.get('hits') or [])} 条归档规程/国标"
    if tool_name == "retrieve_conditional_evidence":
        warns = result.get("applicability_warnings") or []
        if warns:
            return "触发跨站适用性前置拦截（C1535）"
        return f"召回 {len(result.get('evidence_cards') or [])} 张证据卡与 {len(result.get('standard_clauses') or [])} 条规程"
    if tool_name == "verify_conditional_capacity":
        return f"v{result.get('version', 1)} 局部载流裕度复算完成"
    if tool_name == "query_electrical_analytics":
        op = result.get("operation")
        if op == "joint_filter":
            return f"多指标联合筛选命中 {result.get('total_matched_rows', 0)} 条整点记录"
        return f"即席聚合排行返回 Top-{len(result.get('results') or [])} 统计结果"
    if tool_name == "simulate_electrical_engineering":
        op = result.get("operation")
        if op == "reactive_compensation":
            req = (result.get("required_compensation_kvar") or {}).get("p95_kvar")
            return f"完成 {result.get('cabinet', '')} 柜无功补偿测算（P95 需补 {req} kVar）"
        wi = (result.get("worst_current_hour") or {}).get("combined_Ia_algebraic_a")
        return f"完成 N-1 转供仿真（最严苛合成电流 {wi} A）"
    if tool_name == "run_ac_power_flow":
        sc = result.get("scenario", "base_case")
        ns = result.get("network_summary") or {}
        if sc == "tap_sensitivity":
            rec = (result.get("tap_sensitivity") or {}).get("recommended_tap") or {}
            return f"pandapower 调压扫描收敛：推荐 {rec.get('tap_label', '+2 档')} → {rec.get('bus_10kv_ii_kv', '-')} kV"
        if sc == "n1_contingency":
            n1 = result.get("n1_contingency") or {}
            return f"pandapower N-1 潮流收敛（{n1.get('verdict', 'ok')}，网损 {ns.get('total_loss_kw', '-')} kW）"
        if sc == "reactive_opt":
            ro = (result.get("reactive_opt") or {}).get("delta") or {}
            return f"pandapower 补偿前后潮流收敛（降损 {ro.get('total_loss_reduction_kw', '-')} kW）"
        return f"pandapower 交流潮流收敛（10kV II段 {ns.get('bus_10kv_ii_kv', '-')} kV，网损 {ns.get('total_loss_kw', '-')} kW）"
    return "执行完成"


def _parse_analytics_queries(
    question: str,
    mentioned_s35: list[str],
    day: str | None,
    *,
    days: list[str] | None = None,
    incomer_cabinets: list[str] | None = None,
) -> list[dict]:
    """Parse ad-hoc statistical ranking / cumulative kWh / multi-metric joint filter queries from Chinese text."""
    calls: list[dict] = []

    req_days = sorted(dict.fromkeys(days or ([day] if day else [])))
    is_range = len(req_days) >= 2 or bool(
        re.search(r"\d{1,2}[日号]?\s*(?:至|到|~|—|–|-)\s*(?:\d{1,2}[月-])?\d{1,2}[日号]", question)
    )
    target_day = None if is_range else (req_days[0] if req_days else day)
    start_day = req_days[0] if (is_range and len(req_days) >= 2) else None
    end_day = req_days[-1] if (is_range and len(req_days) >= 2) else None

    def _apply_date_scope(args: dict) -> None:
        if target_day:
            args["day"] = target_day
        elif start_day and end_day:
            args["start_day"] = start_day
            args["end_day"] = end_day

    metric_alias = {
        "uab": "Uab", "线电压": "Uab", "电压": "Uab",
        "ia": "Ia", "电流": "Ia",
        "cos": "cos", "功率因数": "cos",
        "p": "P", "有功": "P",
    }
    op_alias = {
        ">": ">", "大于": ">", "高于": ">", "超过": ">",
        ">=": ">=", "大于等于": ">=",
        "<": "<", "小于": "<", "低于": "<",
        "<=": "<=", "小于等于": "<=",
        "=": "=", "等于": "=",
        "!=": "!=",
    }
    raw_conds = []
    for m in _JOINT_COND_RE.finditer(question):
        m_key = metric_alias.get(m.group(1).lower())
        op_val = op_alias.get(m.group(2))
        num_val = float(m.group(3))
        if m_key and op_val:
            raw_conds.append({"metric": m_key, "op": op_val, "value": num_val})

    if len(raw_conds) >= 2 or (len(raw_conds) == 1 and any(k in question for k in ("筛选", "同时满足", "找出所有", "列出所有"))):
        jf_args: dict = {
            "operation": "joint_filter",
            "conditions": raw_conds[:4],
            "top_k": 10,
        }
        if not mentioned_s35:
            uab_conds = [c for c in raw_conds if c["metric"] == "Uab"]
            if uab_conds:
                uv = uab_conds[0]["value"]
                if 8.0 <= uv <= 15.0:
                    jf_args["voltage_level"] = "10kV"
                elif uv >= 25.0:
                    jf_args["voltage_level"] = "35kV"
        _apply_date_scope(jf_args)
        if mentioned_s35:
            jf_args["cabinets"] = mentioned_s35
        calls.append(jf_args)

    top_m = re.search(r"(?:前|Top\s*)(\d{1,2})", question, re.I)
    top_k = max(2, min(int(top_m.group(1)), 15)) if top_m else max(len(mentioned_s35), 5)

    wants_rank_or_stat = any(
        k in question
        for k in (
            "排行", "排名", "前5", "前3", "前10", "Top", "top", "极差",
            "哪几个小时", "哪个整点", "谁的负荷更重", "谁负荷更重", "负荷对比", "比较一下",
            "哪天", "按日", "按天", "日均", "平均有功", "平均负荷", "总有功",
        )
    )
    if any(k in question for k in ("标准差", "波动最大", "波动前", "波动排行", "最稳定")):
        metric = "Ia" if any(k in question for k in ("电流", "Ia")) else (
            "Uab" if any(k in question for k in ("电压", "Uab")) else (
                "cos" if any(k in question for k in ("功率因数", "cos")) else "P"
            )
        )
        order = "asc" if any(k in question for k in ("最小", "最稳", "最低")) else "desc"
        ar_args: dict = {
            "operation": "aggregate_rank",
            "metric": metric,
            "group_by": "day" if any(k in question for k in ("按日", "按天", "哪天")) else "cabinet",
            "agg": "std",
            "order": order,
            "top_k": top_k,
        }
        _apply_date_scope(ar_args)
        if mentioned_s35:
            ar_args["cabinets"] = mentioned_s35
        calls.append(ar_args)
    elif any(k in question for k in ("累计用电", "用电量", "总电量", "累计电量", "耗电量", "kWh", "kwh")):
        ar_args = {
            "operation": "aggregate_rank",
            "metric": "P",
            "group_by": "day" if any(k in question for k in ("按日", "按天", "每日", "哪天")) else "cabinet",
            "agg": "sum",
            "order": "asc" if any(k in question for k in ("最少", "最低")) else "desc",
            "top_k": top_k,
        }
        _apply_date_scope(ar_args)
        if mentioned_s35:
            ar_args["cabinets"] = mentioned_s35
        elif any(k in question for k in ("全站", "总负荷", "总有功")) and incomer_cabinets:
            ar_args["cabinets"] = incomer_cabinets
            ar_args["combine_cabinets_per_hour"] = True
        calls.append(ar_args)
    elif wants_rank_or_stat and not any(k in question for k in ("异常", "健康状况", "单进线", "反向", "轮询", "过压", "补证", "规程")):
        metric = "Ia" if any(k in question for k in ("电流", "Ia")) else (
            "Uab" if any(k in question for k in ("电压", "Uab")) else (
                "cos" if any(k in question for k in ("功率因数", "cos")) else "P"
            )
        )
        # Critical precedence (Astra #04): "平均...最高" -> agg="avg", order="desc" (NOT agg="max"!)
        if "极差" in question:
            agg = "range"
        elif any(k in question for k in ("平均", "均值", "日均")):
            agg = "avg"
        elif any(k in question for k in ("最大", "峰值", "最高")):
            agg = "max"
        elif any(k in question for k in ("最小", "最低")):
            agg = "min"
        else:
            agg = "avg"
        order = "asc" if any(k in question for k in ("最低", "最小", "最差", "最轻")) else "desc"
        group_by = "day" if any(k in question for k in ("按日", "按天", "每日", "哪天")) else (
            "hour" if any(k in question for k in ("按小时", "哪个整点", "哪几个小时", "分时")) else "cabinet"
        )
        alias_cabs = list(mentioned_s35)
        combine_hourly = False
        if not alias_cabs and ("1#主变" in question or "1号主变" in question or "一号主变" in question) and ("2#主变" in question or "2号主变" in question or "二号主变" in question):
            alias_cabs = ["311", "312"]
        elif not alias_cabs and group_by in {"day", "hour"} and metric == "P" and any(k in question for k in ("全站", "总有功", "总负荷")):
            alias_cabs = list(incomer_cabinets or ["303", "306"])
            combine_hourly = True
        ar_args = {
            "operation": "aggregate_rank",
            "metric": metric,
            "group_by": group_by,
            "agg": agg,
            "order": order,
            "top_k": top_k,
        }
        _apply_date_scope(ar_args)
        if alias_cabs:
            ar_args["cabinets"] = alias_cabs
        if combine_hourly:
            ar_args["combine_cabinets_per_hour"] = True
        calls.append(ar_args)

    return calls


def _parse_powerflow_request(question: str, mentioned_s35: list[str], day: str | None, hour: int | None) -> dict | None:
    """Detect real AC Newton-Raphson power-flow (pandapower) requests."""
    pf_triggers = ("潮流", "pandapower", "牛顿拉夫逊", "Newton", "分接头", "调压档位", "调几档", "调档", "网损", "末端压降")
    if not any(k in question for k in pf_triggers):
        return None
    at_time = f"{day} {hour:02d}:00:00" if (day and hour is not None and 0 <= hour <= 23) else None
    args: dict = {}
    if at_time:
        args["at_time"] = at_time

    if any(k in question for k in ("分接头", "调压", "调档", "调几档", "灵敏度")):
        args["scenario"] = "tap_sensitivity"
    elif any(k in question for k in ("N-1", "n-1", "故障潮流", "断线", "退运", "转供")):
        args["scenario"] = "n1_contingency"
        if "306" in question or "单进线" in question:
            args["outage_mode"] = "incomer_306"
        elif "2#主变" in question or "2号主变" in question or "312" in question:
            args["outage_mode"] = "transformer_t2"
        else:
            args["outage_mode"] = "feeder_1009"
    elif any(k in question for k in ("无功", "补偿", "电容", "降损", "网损对比")):
        args["scenario"] = "reactive_opt"
        if "1020" in mentioned_s35:
            args["target_cabinet"] = "1020"
        elif "1009" in mentioned_s35:
            args["target_cabinet"] = "1009"
        else:
            args["target_cabinet"] = "1010"
        m_qc = re.search(r"(\d{2,4}(?:\.\d+)?)\s*kVar", question, re.I)
        if m_qc:
            args["qc_kvar"] = float(m_qc.group(1))
    else:
        args["scenario"] = "base_case"

    m_km = re.search(r"(\d+(?:\.\d+)?)\s*km", question, re.I)
    if m_km:
        args["cable_length_km"] = float(m_km.group(1))
    return args


def _parse_physics_simulation(question: str, mentioned_s35: list[str], day: str | None, cap_params: dict) -> dict | None:
    """Detect reactive compensation Qc sizing or N-1 load transfer simulation requests."""
    if any(k in question for k in ("无功补偿", "补偿容量", "kVar", "kvar", "降损", "提高功率因数", "提升功率因数", "补多少无功")):
        cab = mentioned_s35[0] if mentioned_s35 else ("1020" if "1020" in question else "1010")
        m_cos = re.search(r"(?:到|至|目标[^\d]{0,4})(0\.\d{2})", question)
        target_cos = float(m_cos.group(1)) if m_cos else 0.90
        args: dict = {"operation": "reactive_compensation", "cabinet": cab, "target_cos": target_cos}
        if day:
            args["day"] = day
        return args

    if any(k in question for k in ("转供", "转到", "负荷转移", "N-1", "n-1", "退出由另一回", "全部转给")):
        if len(mentioned_s35) >= 2:
            c_out, c_rec = mentioned_s35[0], mentioned_s35[1]
        elif "306" in question or "303" in question:
            c_out, c_rec = "306", "303"
        elif "312" in question or "311" in question:
            c_out, c_rec = "312", "311"
        else:
            c_out, c_rec = "1009", "1010"
        args = {"operation": "n1_transfer", "outage_cabinet": c_out, "receiving_cabinet": c_rec}
        if day:
            args["day"] = day
        if cap_params.get("rated_1009_a") and c_rec in ("1009", "1010"):
            args["rated_current_a"] = cap_params["rated_1009_a"]
        elif cap_params.get("rated_303_a") and c_rec == "303":
            args["rated_current_a"] = cap_params["rated_303_a"]
        if cap_params.get("derating_factor"):
            args["derating_factor"] = cap_params["derating_factor"]
        return args
    return None


def _is_briefing_followup(question: str, prior_turn: dict | None) -> bool:
    if prior_turn is None:
        return False
    return any(k in question for k in ("简报", "精简", "3句话", "三句话", "总结刚才", "归纳刚才", "交接话术", "一句话概括"))


def _local_diagnose(
    toolbox: DiagnosticToolbox,
    question: str,
    *,
    prior_turn: dict | None = None,
    emit=None,
) -> tuple[dict, list[dict]]:
    """Deterministic local diagnostic baseline using all 11 compound read-only tools."""
    trace: list[dict] = []

    def run_tool(name: str, arguments: dict, round_no: int = 1) -> dict:
        step_no = len(trace) + 1
        if emit:
            emit("tool_start", round=round_no, step=step_no, tool=name, arguments=arguments)
        res = toolbox.execute(name, arguments)
        trace.append({"round": round_no, "tool": name, "arguments": arguments, "result": res})
        if emit:
            emit(
                "tool_end",
                round=round_no,
                step=step_no,
                tool=name,
                status=res.get("status", "ok"),
                summary=_summarize_tool_result(name, res),
            )
        return res

    def sourced(fact, result, *path, kind="calculation"):
        step = next(step for step in reversed(trace) if step["result"] is result)
        return bind_tool_fact(fact, step, *path, kind=kind)

    from laowadi.context import extract_query_anchors

    s35_cabs = {num for st, num in toolbox.archive.devices if st == REPORT_STATION}
    c1535_cabs = {num for st, num in toolbox.archive.devices if st == "C1535"}
    parsed_anchors = extract_query_anchors(question, s35_cabs | c1535_cabs)
    req_days = parsed_anchors.get("days") or []
    day = req_days[0] if req_days else None
    req_hours = parsed_anchors.get("hours") or []
    hour = int(req_hours[0][:2]) if req_hours else None
    mentioned_s35 = [c for c in sorted(s35_cabs, key=int) if re.search(rf"(?<!\d){re.escape(c)}(?!\d)", question)]
    mentioned_c1535 = [c for c in sorted(c1535_cabs, key=int) if re.search(rf"(?<!\d){re.escape(c)}(?!\d)", question)]
    mentioned_cabs = mentioned_s35 + mentioned_c1535
    incomer_cabs = list(toolbox.profile.incomer_cabinets()) if getattr(toolbox, "profile", None) else ["303", "306"]

    if _is_briefing_followup(question, prior_turn):
        previous_question = prior_turn.get("resolved_question") or prior_turn["question"]
        report, trace = _local_diagnose(toolbox, previous_question, emit=emit)
        report["evidence_question"] = previous_question
        report["confirmed_facts"] = report["confirmed_facts"][:3]
        return report, trace

    pf_call = _parse_powerflow_request(question, mentioned_s35, day, hour)
    sim_call = _parse_physics_simulation(question, mentioned_s35, day, _parse_capacity_params(question))
    if pf_call is not None or sim_call is not None:
        if pf_call is not None:
            result = run_tool("run_ac_power_flow", pf_call)
            std_query = "电能质量 电压偏差 10kV 35kV" if pf_call["scenario"] != "n1_contingency" else "两回路 备用电源"
        else:
            result = run_tool("simulate_electrical_engineering", sim_call)
            std_query = "电压偏差 功率因数 无功补偿" if sim_call["operation"] == "reactive_compensation" else "两回路 备用电源"
        if result["status"] in ("ok", "warning"):
            facts = render_tool_facts(trace[-1])
            standards = run_tool("search_coal_standards", {"query": std_query, "top_k": 2}, round_no=2)
            return {
                "confirmed_facts": facts,
                "missing_evidence": result.get("missing_field_evidence") or ["现场设备台账和同期运行记录。"],
                "cited_standards": [{"id": hit["id"]} for hit in standards.get("hits", [])],
            }, trace

    analytics_calls = _parse_analytics_queries(
        question, mentioned_s35, day, days=req_days, incomer_cabinets=incomer_cabs,
    )
    if analytics_calls:
        result_groups = []
        for index, arguments in enumerate(analytics_calls, 1):
            result = run_tool("query_electrical_analytics", arguments, round_no=min(index, 2))
            if result.get("status") == "ok":
                result_groups.append(render_tool_facts(trace[-1])[:6])
        # Interleave separate queries so a long ranking cannot crowd out a requested second operation.
        facts = [
            group[index] for index in range(6) for group in result_groups if index < len(group)
        ][:6]
        standards = run_tool("search_coal_standards", {"query": "电能质量 电压偏差 功率因数 无功补偿", "top_k": 2}, round_no=2)
        return {
            "confirmed_facts": facts,
            "missing_evidence": ["同期运行与检修记录。精确电量需核对电能表实抄。"],
            "cited_standards": [{"id": hit["id"]} for hit in standards.get("hits", [])],
        }, trace

    # 0b2. Concise Single-Point Lookup Fast-Path (Astra #05: parity with fast lookup & follow-up "同期")
    is_point_lookup = (
        bool(day)
        and hour is not None
        and bool(mentioned_s35)
        and len(req_days) == 1
        and any(k in question for k in ("是多少", "读数", "多少", "同期", "什么值", "有功", "电压", "电流", "功率因数"))
        and not any(k in question for k in ("异常", "健康", "单进线", "反向", "轮询", "过压", "补证", "规程", "基线", "突增", "原因", "为什么", "怎么回事", "故障", "是不是", "相差", "是否"))
    )
    if is_point_lookup:
        target_cabs = list(dict.fromkeys(mentioned_s35))
        ts = f"{day} {hour:02d}:00:00"
        snap = run_tool(
            "get_window_snapshot",
            {"start_time": ts, "end_time": ts, "cabinets": target_cabs},
            round_no=1,
        )
        frames = snap.get("frames") or []
        readings = frames[0]["readings"] if frames else {}
        cells = [
            reading["cell"] for cab in target_cabs for metric in ("P", "Ia", "Uab", "cos")
            if (reading := readings.get(cab, {}).get(metric)) and reading.get("value") is not None
        ]
        return {
            "confirmed_facts": build_observations(toolbox.archive, cells, trace) or [],
            "missing_evidence": ["如需核实运行方式，须查同期开关变位与保护 SOE。"],
            "cited_standards": [],
        }, trace

    # 0c. Condition-aware RAG & Applicability Gate
    rag_args = {"query": question, "top_k": 3}
    if mentioned_cabs:
        rag_args["cabinet"] = mentioned_cabs[0]
    if day:
        rag_args["day"] = day
    rag_res = run_tool("retrieve_conditional_evidence", rag_args)

    if mentioned_c1535 and not mentioned_s35:
        profiles = [run_tool("get_cabinet_health_profile", {"cabinet": cab}) for cab in mentioned_c1535[:3]]
        confirmed_facts = []
        for index, warn in enumerate(rag_res.get("applicability_warnings") or []):
            confirmed_facts.append(sourced({"statement": warn, "evidence": []},
                                          rag_res, "applicability_warnings", index, kind="coverage"))
        for prof in profiles:
            dev = prof.get("device") or {}
            links_up = (prof.get("supply_links") or {}).get("upstream") or []
            links_dn = (prof.get("supply_links") or {}).get("downstream") or []
            link_desc = "；".join(
                [f'上游来自 {l["from"]["station_name"]} {l["from"]["number"]} 柜（{l["from"]["label"]}）' for l in links_up]
                + [f'下游送至 {l["to"]["station_name"]} {l["to"]["number"]} 柜（{l["to"]["label"]}）' for l in links_dn]
            )
            confirmed_facts.append(sourced({
                "statement": f'{dev.get("station_name", "1535中央变电所")} {dev.get("number")} 柜（{dev.get("label")}）：{prof.get("note", "仅存在于一次系统图截图台账，无小时级运行日报读数。")} {link_desc}',
                "evidence": [],
            }, prof, kind="coverage"))
        for ug_cab, surf_cab in (("1101", "1009"), ("1201", "1010")):
            if ug_cab in mentioned_c1535:
                sprof = run_tool("get_cabinet_health_profile", {"cabinet": surf_cab})
                if sprof.get("status") == "ok" and sprof.get("has_hourly_readings"):
                    m_p = sprof["metrics_7d"].get("P", {})
                    m_c = sprof["metrics_7d"].get("cos", {})
                    ev_cells = [c for c in (m_p.get("max_cell"), m_c.get("min_cell")) if c]
                    confirmed_facts.append(sourced({
                        "statement": (
                            f'可回查的地面上级送电柜 {surf_cab}（{sprof["device"]["label"]} ➔ 井下 {ug_cab}）7 天量测：'
                            f'有功 {m_p.get("min")}~{m_p.get("max")} kW（均值 {m_p.get("avg")} kW），cos 均值 {m_c.get("avg")}。'
                        ),
                        "evidence": ev_cells,
                    }, sprof, "metrics_7d"))
        return {
            "confirmed_facts": confirmed_facts,
            "missing_evidence": rag_res.get("missing_evidence") or ["井下分柜小时级运行报表。"],
            "cited_standards": [{"id": hit["id"]} for hit in (rag_res.get("standard_clauses") or [])[:2]],
        }, trace

    # 1. Station health overview
    overview = run_tool("get_station_health_overview", {"day": day, "severity": "all"} if day else {"severity": "all"})
    if overview.get("status") != "ok":
        overview = run_tool("get_station_health_overview", {"severity": "all"})

    # 2. Cabinet profiles
    profiles = [run_tool("get_cabinet_health_profile", {"cabinet": cab}) for cab in mentioned_s35[:3]]

    # 3. Power balance
    balance = None
    if day and hour is not None and 0 <= hour <= 23:
        balance = run_tool("check_power_balance", {"at_time": f"{day} {hour:02d}:00:00"})

    # 3b. Round-2 Hypothesis Falsification (1027 vs 2# transformer reverse flow)
    falsification_snap = None
    if (
        ("1027" in mentioned_s35 and day)
        or (day == "2026-11-12" and (hour == 12 or any(k in question for k in ("反向", "倒送", "瓦斯", "312", "1006"))))
    ):
        snap_day = day or "2026-11-12"
        snap_h = hour if hour is not None else 12
        h0 = max(0, snap_h - 1)
        h1 = min(23, snap_h + 1)
        snap_cabs = list(dict.fromkeys(["306", "312", "1006", "1027", *mentioned_s35[:2]]))[:6]
        falsification_snap = run_tool(
            "get_window_snapshot",
            {
                "start_time": f"{snap_day} {h0:02d}:00:00",
                "end_time": f"{snap_day} {h1:02d}:00:00",
                "cabinets": snap_cabs,
                "metrics": ["P", "Ia", "cos"],
            },
            round_no=2,
        )

    # 4. Diurnal baseline
    diurnal = None
    if day or any(k in question for k in ("基线", "负荷", "异常", "单进线", "避峰", "波动")):
        diurnal = run_tool("get_diurnal_baseline", {"day": day} if day else {})

    # 5. Conditional Capacity Recalculation
    cap_params = _parse_capacity_params(question)
    cap_res = None
    if cap_params or any(k in question for k in ("过载", "裕度", "载流量", "N-1")):
        cap_res = run_tool("verify_conditional_capacity", cap_params, round_no=2)

    # 6. Coal standards
    std_query = "两回路电源 分列运行 电能质量 电压偏差"
    if any(k in question for k in ("电压", "过压", "母线")):
        std_query = "电能质量 电压偏差 10kV 35kV"
    elif any(k in question for k in ("1018", "1012", "量测", "接线", "定值", "台账")):
        std_query = "电气设备 台账 保护定值 检修 试验"
    elif any(k in question for k in ("单进线", "306", "倒闸", "双主变", "11月13")):
        std_query = "两回路电源 任一回路 分列运行 备用电源"
    std_res = run_tool("search_coal_standards", {"query": std_query, "top_k": 3}, round_no=2)

    confirmed_facts = []

    findings = overview.get("findings") or []
    dim_boost = set()
    if any(k in question for k in ("电压", "过压", "母线", "功率因数", "无功", "电能质量")):
        dim_boost.add("power_quality")
    if any(k in question for k in ("量测", "表计", "接线", "CT", "误差", "1018", "1012")):
        dim_boost.add("metering")
    if any(k in question for k in ("平衡", "守恒", "轮询", "单进线", "双主变", "反向", "倒送")):
        dim_boost.add("system_balance")
    if any(k in question for k in ("基线", "避峰", "突增", "波动")):
        dim_boost.add("diurnal_baseline")

    sev_rank = {"high": 0, "medium": 1, "info": 2}

    def _rank_finding(f):
        cab_hit = 0 if (mentioned_s35 and any(c in str(f["cabinet"]) for c in mentioned_s35)) else 1
        dim_hit = 0 if (dim_boost and f["dimension"] in dim_boost) else 1
        return (cab_hit, dim_hit, sev_rank.get(f["severity"], 2), f["id"])

    ranked_findings = sorted(findings, key=_rank_finding)
    if mentioned_s35:
        direct_cab_findings = [f for f in ranked_findings if any(c in str(f["cabinet"]) for c in mentioned_s35)]
        relevant_findings = direct_cab_findings[:5] if direct_cab_findings else [f for f in ranked_findings if f["severity"] in ("high", "medium")][:3]
    else:
        relevant_findings = [f for f in ranked_findings if f["severity"] in ("high", "medium")][:6] or ranked_findings[:6]

    for f in relevant_findings:
        ev_list = [f'{e["file"]} / {e["sheet"]}!{e["cell"]}' for e in f.get("evidence", []) if isinstance(e, dict) and "cell" in e]
        confirmed_facts.append(sourced(
            {"statement": f'{f["title"]}：{f["summary"]}', "evidence": ev_list},
            overview, "findings", findings.index(f), kind="screening",
        ))

    if cap_res and cap_res.get("version") == 2:
        step = next(step for step in reversed(trace) if step["result"] is cap_res)
        confirmed_facts[:0] = [
            capacity_fact(step, index) for index, item in enumerate(cap_res["items"])
            if isinstance(item.get("conditional_judgment"), dict)
        ]

    if falsification_snap and falsification_snap.get("status") == "ok":
        target_frame = None
        for fr in falsification_snap.get("frames") or []:
            if fr["time"].endswith(f"{hour if hour is not None else 12:02d}:00:00"):
                target_frame = fr
                break
        if target_frame:
            r1027 = target_frame["readings"].get("1027") or {}
            p1027 = (r1027.get("P") or {}).get("value")
            ia1027 = (r1027.get("Ia") or {}).get("value")
            ev_f = [r1027[m]["cell"] for m in ("P", "Ia") if (r1027.get(m) or {}).get("cell")]
            if p1027 is not None and ia1027 is not None:
                confirmed_facts[:0] = build_observations(toolbox.archive, ev_f, trace) or []

    if balance and balance.get("status") == "ok":
        step = next(step for step in reversed(trace) if step["result"] is balance)
        confirmed_facts.extend(render_tool_facts(step))

    if diurnal and diurnal.get("status") == "ok" and diurnal.get("day_comparison"):
        d_hours = diurnal["day_comparison"].get("hours") or []
        target_dh = None
        if hour is not None:
            target_dh = next((x for x in d_hours if x["time"].endswith(f"{hour:02d}:00:00")), None)
        if target_dh is None and d_hours:
            target_dh = max(d_hours, key=lambda x: abs(x.get("deviation_kw", 0)))
        if target_dh:
            confirmed_facts.append(sourced({
                "statement": (
                    f'【分时段基线对照 · {target_dh["time"]}】全站总有功（303+306）实测 {target_dh["total_p_kw"]} kW，'
                    f'7 天同整点中位基线 {target_dh["baseline_median_kw"]} kW，偏离基线 {target_dh["deviation_kw"]:+} kW。'
                ),
                "evidence": target_dh.get("cells") or [],
            }, diurnal, "day_comparison", "hours", d_hours.index(target_dh)))

    for prof in profiles:
        if prof.get("status") == "ok" and prof.get("has_hourly_readings"):
            dev = prof["device"]
            m_p = prof["metrics_7d"].get("P", {})
            m_c = prof["metrics_7d"].get("cos", {})
            ev_cells = [c for c in (m_p.get("min_cell"), m_p.get("max_cell"), m_c.get("min_cell")) if c]
            confirmed_facts.append(sourced({
                "statement": (
                    f'{dev["number"]} 柜（{dev["label"]}）归档周期统计：有功 {m_p.get("min")}~{m_p.get("max")} kW（均值 {m_p.get("avg")} kW），'
                    f'cos {m_c.get("min")}~{m_c.get("max")}（均值 {m_c.get("avg")}）；量测校验：{prof["metering_check"]["note"]}'
                ),
                "evidence": ev_cells[:3],
            }, prof, kind="screening"))

    cited_standards = [
        {"id": hit["id"], "relevance": f'{hit["title"]}（{hit["pdf_page"]}）'}
        for hit in (std_res.get("hits") or [])[:2]
    ]

    missing_merged = list(dict.fromkeys((rag_res.get("missing_evidence") or []) + (overview.get("missing_field_evidence") or [])))
    report = {
        "confirmed_facts": confirmed_facts,
        "missing_evidence": missing_merged[:6],
        "cited_standards": cited_standards,
    }
    return report, trace
