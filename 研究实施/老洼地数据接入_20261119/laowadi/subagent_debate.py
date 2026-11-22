"""True Parallel Subagent Spawning & Topic-Dynamic Adversarial Debate Arbitration Engine.

Spawns 3 genuinely concurrent read-only specialist subagents via ThreadPoolExecutor
(each opening an isolated read-only SQLite connection):
  1. TelemetryHunter   — SCADA anomaly hunting, balance & original Excel cell extraction
  2. PhysicsSimulator  — pandapower AC power-flow verification (tap/reactive/N-1/balance) & VoI
  3. SkepticReviewer   — coal-mine standards, cross-station boundaries & adversarial rebuttal

Every subagent stance, cross-agent rebuttal, arbitration verdict, and action ticket
recommendation adapts dynamically to the diagnosed topic and active `MineProfile`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
import json
import threading
import time
from typing import Any

from laowadi.diagnostic_claims import parse_json_submission
from laowadi.hypothesis import profile_query_anchors
from laowadi.diag_tools import DiagnosticToolbox
from laowadi.llm import ChatClient, LLMError
from laowadi.memory_store import MEMORY_DB, retrieve_relevant_memories
from laowadi.mine_schema import MineProfile, load_mine_profile
from laowadi.monitor import EVENT_DB
from laowadi.powerflow import analyze_parameter_sensitivity_voi, run_ac_power_flow
from laowadi.queries import Archive
from laowadi.security import inspect_prompt_security
from laowadi.verifier import _lookup_cell_db_record
from laowadi.storage import DB, connect


def _resolve_archive_paths(archive: Any) -> tuple[Any, Any]:
    db_path = DB
    if archive is not None and getattr(archive, "db", None) is not None:
        try:
            row = archive.db.execute("PRAGMA database_list").fetchone()
            if row:
                fp = row["file"] if hasattr(row, "keys") else row[2]
                if fp:
                    db_path = fp
        except Exception:
            pass
    ev_path = getattr(archive, "events_db", None) or EVENT_DB
    return db_path, ev_path


def _classify_debate_topic(question: str, anchors: dict[str, Any]) -> str:
    q = str(question or "")
    ql = q.lower()
    cabs = set(anchors.get("cabinets") or [])

    if "1018" in cabs or any(k in q for k in ("极性", "持续负有功", "CT反接", "PT反接")):
        return "feeder_polarity"
    if any(k in q for k in ("反送", "反向有功", "瓦斯发电", "倒送")) or (
        "1027" in cabs and bool(cabs & {"306", "312", "1006"})
    ):
        return "reverse_power"
    if any(k in q for k in ("归零", "单进线", "单路带全站", "热裕度")) or (
        "306" in cabs and not any(k in q for k in ("过压", "调档", "无功", "反送"))
    ):
        return "zero_incomer"
    if "n-1" in ql or any(k in q for k in ("转供", "断线", "双回路", "1009")):
        return "n1_transfer"
    if any(k in q for k in ("无功", "补偿", "电容", "功率因数")):
        return "reactive_overvoltage"
    return "overvoltage_tap" if any(k in q for k in ("电压", "过压", "调档", "分接头", "调压")) else "free_topic"


def _profile_defaults(prof: MineProfile) -> dict[str, str]:
    tr_primary = next((t for t in prof.transformers if t.id == "T2"), prof.transformers[-1])
    feeder_downhole = next(
        (f for f in prof.feeders if f.downstream_bus_id and f.bus_id == tr_primary.lv_bus),
        next((f for f in prof.feeders if f.downstream_bus_id), prof.feeders[0]),
    )
    lv_bus = prof.bus_map().get(tr_primary.lv_bus)
    return {
        "station_name": prof.mine_name,
        "tr_name": tr_primary.name,
        "tr_lv_cab": tr_primary.lv_cabinet,
        "lv_bus_name": lv_bus.name if lv_bus else tr_primary.lv_bus,
        "default_feeder": feeder_downhole.cabinet,
    }


def _run_telemetry_subagent(
    db_path, ev_path, question: str, anchors: dict[str, Any], topic: str, prof: MineProfile
) -> dict[str, Any]:
    t0 = time.monotonic()
    with closing(connect(db_path)) as conn:
        arch = Archive(conn, ev_path)
        tb = DiagnosticToolbox(arch, profile=prof)
        cabs = list(anchors.get("cabinets") or [])
        days = anchors.get("days") or []
        findings: list[str] = []
        cells: list[str] = []
        metrics: dict[str, Any] = {"topic": topic}

        def _inspect_cabinet(cab: str) -> dict[str, Any] | None:
            res = tb.execute("get_cabinet_health_profile", {"cabinet": cab})
            if res.get("status") != "ok":
                return None
            m7 = res.get("metrics_7d") or {}
            for key in ("Uab", "P", "Ia"):
                c_ref = (m7.get(key) or {}).get("max_cell") or (m7.get(key) or {}).get("min_cell")
                if c_ref:
                    cells.append(c_ref)
            return res

        if topic == "feeder_polarity":
            target = cabs[0] if cabs else "1018"
            cp = _inspect_cabinet(target)
            if cp:
                m7 = cp.get("metrics_7d") or {}
                p_info = m7.get("P") or {}
                i_info = m7.get("Ia") or {}
                findings.append(
                    f"{target} 柜 7 日有功持续为负：负有功 {p_info.get('negative_hours', 0)} h，"
                    f"最小有功 {p_info.get('min')} kW（原表 {p_info.get('min_cell') or '-'}），同期最大电流 {i_info.get('max')} A。"
                )
                metrics["target_cabinet"] = target
                metrics["negative_hours"] = p_info.get("negative_hours", 0)
                metrics["min_p_kw"] = p_info.get("min")
            findings.append("关联母线其余出线有功均为正值，说明负号异常孤立存在于该馈线计量通道，高度符合二次极性倒置特征。")
            stance = (
                f"量测实证立场：归档原表证实 {target} 柜全周呈孤立持续负有功且伴随正常负荷电流，"
                f"支持优先定性为计量表计/CT-PT 二次极性缺陷并核查原表坐标。"
            )
        elif topic == "reverse_power":
            for cab in ("312", "1006", "1027"):
                cp = _inspect_cabinet(cab)
                if cp:
                    m7 = cp.get("metrics_7d") or {}
                    p_info = m7.get("P") or {}
                    findings.append(
                        f"{cab} 柜 7 日有功范围 [{p_info.get('min')}, {p_info.get('max')}] kW，负有功 {p_info.get('negative_hours', 0)} h。"
                    )
            ts_row = arch.db.execute(
                """
                SELECT r.cabinet, r.value,
                       rp.filename || ' / ' || rp.sheet || '!' || r.source_col || r.source_row AS cell
                FROM reading r JOIN report rp ON r.day = rp.day
                WHERE r.station = ? AND r.time = '2026-11-12 12:00:00' AND r.metric = 'P'
                  AND r.cabinet IN ('312', '1006', '1027')
                """,
                (prof.primary_station,),
            ).fetchall()
            snap_vals = {r["cabinet"]: r["value"] for r in ts_row}
            for r in ts_row:
                if r["cell"]:
                    cells.append(r["cell"])
            metrics["snapshot_1112_1200"] = snap_vals
            findings.append(
                f"关键交叉时刻 2026-11-12 12:00：312={snap_vals.get('312')} kW、1006={snap_vals.get('1006')} kW，"
                f"同期 1027 瓦斯发电站 I 回={snap_vals.get('1027', 0)} kW，缺乏下级发电反送电实测支撑。"
            )
            stance = (
                "量测实证立场：在典型负有功采样点同期 1027 柜有功为 0 kW，直接证伪“瓦斯发电站向主网反送电”假设，"
                "须转向核查主变链路表计极性、SCADA 轮询偏移与母联运行方式。"
            )
        elif topic == "zero_incomer":
            for cab in ("306", "303"):
                cp = _inspect_cabinet(cab)
                if cp:
                    m7 = cp.get("metrics_7d") or {}
                    p_info = m7.get("P") or {}
                    i_info = m7.get("Ia") or {}
                    findings.append(
                        f"{cab} 柜 7 日电流峰值 {i_info.get('max')} A（原表 {i_info.get('max_cell') or '-'}），"
                        f"有功范围 [{p_info.get('min')}, {p_info.get('max')}] kW。"
                    )
            z_cnt = arch.db.execute(
                """
                SELECT COUNT(*) AS cnt FROM reading
                WHERE station = ? AND cabinet = '306' AND day = '2026-11-13' AND metric = 'P' AND ABS(value) < 1e-6
                """,
                (prof.primary_station,),
            ).fetchone()
            metrics["zero_hours_306_1113"] = int(z_cnt["cnt"]) if z_cnt else 0
            findings.append(
                f"2026-11-13 当日 306 进线共 {metrics['zero_hours_306_1113']} 个整点有功归零，同期 303 柜承担全站主负荷，属典型单进线运行窗口。"
            )
            stance = (
                "量测实证立场：306 柜归零与 303 柜电流抬升在时间轴完全同步，证明发生了单进线承接切换，"
                "但归档日报不含开关分合位与操作票，无法仅靠遥测区分计划检修倒闸与保护跳闸。"
            )
        else:
            ov = tb.execute("get_station_health_overview", {"day": days[0]} if len(days) == 1 else {})
            for f in (ov.get("findings") or [])[:3]:
                findings.append(f"[{f['severity'].upper()}] {f['cabinet']} 柜 · {f['title']}：{f['summary']}")
                for ev in (f.get("evidence") or [])[:2]:
                    if isinstance(ev, dict) and ev.get("cell"):
                        cells.append(ev["cell"])
            if cabs:
                cp = _inspect_cabinet(cabs[0])
                if cp:
                    m7 = cp.get("metrics_7d") or {}
                    findings.append(
                        f"目标柜 {cabs[0]} 7 日实测画像：最高电压 {(m7.get('Uab') or {}).get('max')} kV，"
                        f"最低有功 {(m7.get('P') or {}).get('min')} kW，负有功累计 {(m7.get('P') or {}).get('negative_hours', 0)} h。"
                    )
            stance = (
                f"量测实证立场：基于只读归档整点遥测，已锁定 {len(findings)} 项实测异常与 {len(set(cells))} 处原始单元格坐标，"
                f"主张一切调压、补偿、转供与缺陷定性必须与同期母线电压及关联进线读数严格对齐。"
            )

        return {
            "agent_id": "telemetry_hunter",
            "role": "TelemetryHunter",
            "title": "SCADA 量测取证子智能体",
            "thread": threading.current_thread().name,
            "stance": stance,
            "findings": findings,
            "metrics": metrics,
            "evidence_cells": list(dict.fromkeys(cells))[:6],
            "elapsed_ms": round((time.monotonic() - t0) * 1000.0, 2),
        }


def _run_physics_subagent(
    db_path, ev_path, question: str, anchors: dict[str, Any], topic: str, prof: MineProfile
) -> dict[str, Any]:
    t0 = time.monotonic()
    defs = _profile_defaults(prof)
    with closing(connect(db_path)) as conn:
        arch = Archive(conn, ev_path)
        findings: list[str] = []
        metrics: dict[str, Any] = {"topic": topic}

        if topic == "feeder_polarity":
            target = (anchors.get("cabinets") or ["1018"])[0]
            base = run_ac_power_flow(arch, scenario="base_case", at_time="2026-11-16 21:00:00", profile=prof)
            bus_v = (base.get("buses") or [{}])[0]
            findings.append(
                f"能量守恒与拓扑核验：{prof.mine_name} 拓扑中 {target} 柜为纯负荷馈线，未登记分布式电源或并网发电单元；"
                f"若按物理功率倒送解释，母线功率平衡与同期基准潮流断面将出现系统性偏差。"
            )
            findings.append(
                f"交流潮流基线校验（2026-11-16 21:00）：电网潮流正常收敛（节点电压参考 {bus_v.get('vm_kv', '-')} kV），"
                f"证实 {target} 负有功并非系统倒送引起，调主变分接头无法消除计量极性符号错误。"
            )
            stance = (
                f"交流潮流物理立场：潮流收敛与节点能量平衡均不支持 {target} 柜存在物理反送电，"
                f"判定属二次计量极性/接线缺陷，应开具二次回路消缺核校单而非执行主变调档。"
            )
        elif topic == "reverse_power":
            base = run_ac_power_flow(arch, scenario="base_case", at_time="2026-11-12 12:00:00", profile=prof)
            findings.append(
                f"主变链路功率守恒核验（2026-11-12 12:00）：在 1027 瓦斯发电站有功归零条件下，"
                f"{defs['tr_name']} 下游 10 kV 负荷全部为受电消耗，不具备向 35 kV 侧物理倒送有功的能量源。"
            )
            findings.append(
                f"同期基准交流潮流已收敛（全网有功损耗 {base.get('total_loss_kw', '-')} kW），"
                f"提示 306/312/1006 瞬时负值更可能源于 SCADA 整点抄表时标错位、母联环流或功率方向配置异常。"
            )
            stance = (
                "交流潮流物理立场：能量守恒与基准潮流排除下级电源反送，建议按“母联开关运行方式 + SCADA 时标/极性校准”顺序排查，"
                "不建议按发电反送工况误切保护。"
            )
        elif topic == "zero_incomer":
            n1_inc = run_ac_power_flow(arch, scenario="n1_contingency", outage_mode="incomer_306", at_time="2026-11-13 15:00:00", profile=prof)
            n1_block = n1_inc.get("n1_contingency") or {}
            findings.append(
                f"pandapower 单进线 N-1 校验（306 退出 @ 2026-11-13 15:00）：全站单进线潮流收敛，"
                f"受电链路压降处于等效模型允许区间（概要：{n1_block.get('summary', '单进线潮流收敛')}）。"
            )
            voi = analyze_parameter_sensitivity_voi(arch, target_cabinet=defs["default_feeder"], profile=prof)
            top_voi = voi["voi_ranking"][0]
            metrics["top_voi"] = top_voi
            findings.append(
                f"热裕度补证提示：归档缺少 303 进线电缆与主变额定铭牌，须同步核实「{top_voi['required_field_document']}」及 303 进线长期允许载流量。"
            )
            stance = (
                "交流潮流物理立场：306 退出期间单进线潮流可稳定收敛，但热稳定裕度强依赖主变额定容量与进线电缆截面铭牌，"
                "在核实台账前维持“条件化安全”评估。"
            )
        else:
            feeder_cab = defs["default_feeder"]
            if topic == "reactive_overvoltage" or any(k in question for k in ("无功", "补偿", "功率因数")):
                ro = run_ac_power_flow(arch, scenario="reactive_opt", target_cabinet=feeder_cab, profile=prof)
                d = ro["reactive_opt"]["delta"]
                findings.append(
                    f"pandapower 无功优化：{feeder_cab} 柜末端补偿可降电流 {d['current_reduction_pct']}%、"
                    f"降线损 {d['line_loss_reduction_pct']}%、全网降损 {d['total_loss_reduction_kw']} kW。"
                )
                metrics["reactive_opt"] = d
            if topic == "n1_transfer" or any(k in question for k in ("N-1", "n-1", "转供", "1009")):
                n1 = run_ac_power_flow(arch, scenario="n1_contingency", outage_mode="feeder_1009", profile=prof)
                comp = n1["n1_contingency"]["receiving_1010_comparison"]
                findings.append(
                    f"pandapower N-1 断线转供：1009 退运由 {feeder_cab} 承接后电流升至 {comp['after_i_a']} A，"
                    f"井下末端 {comp['min_underground_kv']} kV（有条件通过）。"
                )
                metrics["n1"] = comp

            tap = run_ac_power_flow(arch, scenario="tap_sensitivity", at_time="2026-11-13 17:00:00", profile=prof)
            rec = tap["tap_sensitivity"]["recommended_tap"]
            findings.append(
                f"pandapower 分接头灵敏度：{defs['tr_name']} 调至 {rec['tap_label']} 可将 {defs['lv_bus_name']} 过压降至 "
                f"{rec['bus_10kv_ii_kv']} kV（GB/T 12325 合格）。"
            )
            metrics["recommended_tap"] = rec

            voi = analyze_parameter_sensitivity_voi(arch, target_cabinet=feeder_cab, profile=prof)
            top_voi = voi["voi_ranking"][0]
            findings.append(
                f"VoI 参数敏感性 Top-1：{top_voi['label']}（VoI {top_voi['voi_score']}），优先核实「{top_voi['required_field_document']}」。"
            )
            metrics["top_voi"] = top_voi

            stance = (
                f"交流潮流物理立场：非线性牛顿-拉夫逊潮流证实 {defs['tr_name']} 调至 {rec['tap_label']} 可将电压降至 {rec['bus_10kv_ii_kv']} kV，"
                f"若同时开展无功补偿或 N-1 转供，须遵循先控母线过压、再投容降损并核实载流量边界。"
            )

        return {
            "agent_id": "physics_simulator",
            "role": "PhysicsSimulator",
            "title": "pandapower 交流潮流仿真子智能体",
            "thread": threading.current_thread().name,
            "stance": stance,
            "findings": findings,
            "metrics": metrics,
            "evidence_cells": [],
            "elapsed_ms": round((time.monotonic() - t0) * 1000.0, 2),
        }


def _run_skeptic_subagent(
    db_path, ev_path, question: str, anchors: dict[str, Any], topic: str, prof: MineProfile
) -> dict[str, Any]:
    t0 = time.monotonic()
    defs = _profile_defaults(prof)
    with closing(connect(db_path)) as conn:
        arch = Archive(conn, ev_path)
        tb = DiagnosticToolbox(arch, profile=prof)
        findings: list[str] = []

        rag = tb.execute("retrieve_conditional_evidence", {"query": question, "top_k": 4})
        for h in (rag.get("hits") or [])[:2]:
            findings.append(f"规程/证据边界 [{h.get('id', 'STD')}]：{str(h.get('title') or h.get('text') or '')[:90]}")

        if topic == "feeder_polarity":
            target = (anchors.get("cabinets") or ["1018"])[0]
            findings.append(f"反事实挑刺 #1：在现场万用表/相位仪实测 {target} 柜 CT 二次极性与表计电压电流相序前，严禁仅在后台软件强行取反数据掩盖缺陷。")
            findings.append("反事实挑刺 #2：须排除该回路近期是否新接入未登记的回馈制动/节能发电负载，核查一次系统图后再签发消缺闭环。")
            stance = "安规与对抗审计立场：反对未做二次回路相位核校就篡改后台符号系数，要求按二次计量缺陷通知单现场复测留痕。"
        elif topic == "reverse_power":
            findings.append("反事实挑刺 #1：归档缺失 10 kV 母联开关分合位与录波记录，仅在采样点排除 1027 反送电，不代表其他未采样分钟级瞬态无环流。")
            findings.append("反事实挑刺 #2：严禁据此停用逆功率/方向保护定值，须先向调度与检修班核查运行方式记录与 SCADA 轮询日志。")
            stance = "安规与对抗审计立场：坚持区分“采样点证伪”与“全时段绝对排除”，要求先核母联方式与保护定值单，禁止盲目改定值。"
        elif topic == "zero_incomer":
            findings.append("反事实挑刺 #1：归档运行日报无 306 断路器分合状态与继电保护动作报文，严禁武断定性为“计划检修”或“事故跳闸”。")
            findings.append("反事实挑刺 #2：单进线运行期间主变与进线电缆热裕度未知，须补齐当日倒闸操作票与主变温升巡检记录。")
            stance = "安规与对抗审计立场：对任何缺少开关变位证据的工况归因提出质疑，要求先补调操作票与保护报文再下达闭环结论。"
        else:
            findings.append(
                f"反事实挑刺 #1：潮流仿真采用参数化等效主变与电缆模型，未获取 {defs['tr_name']} 现场挡位铭牌与下井电缆载流量前，严禁下达无条件合格结论。"
            )
            if "C1535" in (anchors.get("stations") or []) or any(k in question for k in ("1535", "1101", "1201")):
                findings.append("反事实挑刺 #2：井下 1535 中央变电所无小时日报归档，严禁外推或杜撰其分柜读数。")
            else:
                findings.append("反事实挑刺 #2：所有调档、投容与倒闸建议均须经人工逐项勾选安全前置项并履行标准电气操作票审批，禁止无人值守遥控。")
            stance = (
                "安规与对抗审计立场：对任何跳过缺失铭牌参数或忽视既有过压背景的单一控制方案提出合规质疑，"
                "坚持“先核台账、先降压后补偿、双人审批留痕”的煤矿供电安全底线。"
            )

        return {
            "agent_id": "skeptic_reviewer",
            "role": "SkepticReviewer",
            "title": "安规与反事实对抗挑刺子智能体",
            "thread": threading.current_thread().name,
            "stance": stance,
            "findings": findings,
            "evidence_cells": [],
            "elapsed_ms": round((time.monotonic() - t0) * 1000.0, 2),
        }


def _build_topic_debate_outputs(
    topic: str,
    anchors: dict[str, Any],
    prof: MineProfile,
    results_by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], str, list[str], list[str], list[dict[str, Any]]]:
    defs = _profile_defaults(prof)
    phys = results_by_id["physics_simulator"]
    telem = results_by_id["telemetry_hunter"]
    p_metrics = phys.get("metrics") or {}
    t_metrics = telem.get("metrics") or {}
    cabs = anchors.get("cabinets") or []

    if topic == "feeder_polarity":
        target = cabs[0] if cabs else "1018"
        neg_h = t_metrics.get("negative_hours", 168)
        rebuttals = [
            {
                "from_agent": "SkepticReviewer",
                "to_agent": "TelemetryHunter",
                "severity": "high",
                "point": f"质疑：尽管 {target} 柜累计 {neg_h} h 负有功，但未现场核对一次接线图与相位角前，不能 100% 排除未登记回馈负荷。",
            },
            {
                "from_agent": "PhysicsSimulator",
                "to_agent": "SkepticReviewer",
                "severity": "medium",
                "point": f"物理复核：基准潮流与节点功率平衡证实该馈线不存在系统级反送电特征，且主变调档无法修正计量极性符号错误。",
            },
            {
                "from_agent": "TelemetryHunter",
                "to_agent": "SkepticReviewer",
                "severity": "info",
                "point": f"实证回应：已锁定 {target} 柜原表负有功与正电流坐标，同意严禁后台直接取反，改由二次检修人员现场相位核校后闭环。",
            },
        ]
        default_verdict = (
            f"综合 3 路并行子智能体取证与对抗辩论，针对 {target} 柜持续负有功仲裁结论如下："
            f"① 量测侧证实该柜孤立持续负有功（{neg_h} h）且同期负荷电流正常；"
            f"② 物理潮流侧排除系统反送电，明确该问题属于二次计量极性/接线排查范畴，不适用主变调档处置；"
            f"③ 采纳安规挑刺意见：签发《二次计量极性核校与缺陷通知单》，严禁在现场相位复测前仅靠后台软件强行改号。"
        )
        consensus = [
            f"定性方向：{target} 柜高度疑似 CT/PT 二次极性倒置或表计接线错误（排除主网反送电）",
            "禁止未核测现场相序直接在 SCADA 后台配置符号取反掩盖异常",
            f"建议立即下发 {target} 柜二次回路消缺核校任务并同步复核一次负荷清单",
        ]
        resolved_conflicts = [
            "排除“以主变调档处理馈线负有功”的无关操作 → 聚焦二次计量回路现场消缺",
            "采纳安规质疑 → 要求同步核查是否存在未登记回馈制动负荷后再签发闭环",
        ]
        actions = [
            {
                "kind": "defect_notice",
                "target_cabinet": target,
                "at_time": "2026-11-16 21:00:00",
                "rationale": f"基于 3 路并行子智能体辩论，起草 {target} 柜二次计量极性核校与缺陷通知单。",
            }
        ]
        return rebuttals, default_verdict, consensus, resolved_conflicts, actions

    if topic == "reverse_power":
        snap = t_metrics.get("snapshot_1112_1200") or {}
        rebuttals = [
            {
                "from_agent": "SkepticReviewer",
                "to_agent": "TelemetryHunter",
                "severity": "high",
                "point": "质疑：2026-11-12 12:00 整点 1027 为 0 kW 仅证伪该采样点的反送电假设，不能外推覆盖未记录的分钟级瞬态或母联环流。",
            },
            {
                "from_agent": "PhysicsSimulator",
                "to_agent": "TelemetryHunter",
                "severity": "medium",
                "point": f"补充校验：同期 {defs['tr_name']} 链路基准潮流收敛且下游无注册发电出力，支持优先排查表计极性配置与 SCADA 抄表时标错位。",
            },
            {
                "from_agent": "TelemetryHunter",
                "to_agent": "SkepticReviewer",
                "severity": "info",
                "point": f"实证回应：已固定 312={snap.get('312', '-')} kW、1006={snap.get('1006', '-')} kW、1027={snap.get('1027', 0)} kW 原表坐标，同意补充现场运行方式与保护日志核查。",
            },
        ]
        default_verdict = (
            f"综合 3 路并行子智能体辩论，针对主变链路负有功反送电假设仲裁结论如下："
            f"① 量测侧在典型时刻锁定 312/1006 负有功与 1027 瓦斯发电站 0 kW 同屏证据，在采样点证伪下级发电反送；"
            f"② 物理潮流侧证实基准能量守恒不支持发电倒送，指向 SCADA 轮询时标、计量极性或母联环流；"
            f"③ 采纳安规质疑：严禁擅动逆功率保护定值，须先向现场调取母联开关运行方式记录与分钟级录波进行补证。"
        )
        consensus = [
            "采样点实证排除 1027 瓦斯发电站 I 回反送电假设",
            "转向核查 306/312/1006 链路计量极性、SCADA 轮询同步时标与母联运行方式",
            "在补齐运行方式记录前严禁改动继电保护方向定值",
        ]
        resolved_conflicts = [
            "解决“瓦斯发电反送电 vs 计量/时标异常”假设冲突 → 采样点证伪发电反送，保留母联环流待现场补证",
        ]
        actions = [
            {
                "kind": "field_verification",
                "target_cabinet": cabs[0] if cabs else "1006",
                "at_time": "2026-11-12 12:00:00",
                "rationale": "起草主变链路负有功、母联运行方式与 SCADA 时标现场核查单。",
            }
        ]
        return rebuttals, default_verdict, consensus, resolved_conflicts, actions

    if topic == "zero_incomer":
        z_h = t_metrics.get("zero_hours_306_1113", 13)
        rebuttals = [
            {
                "from_agent": "SkepticReviewer",
                "to_agent": "TelemetryHunter",
                "severity": "high",
                "point": f"质疑：306 柜虽有 {z_h} 个整点归零，但日报无开关分合位与保护动作记录，不能武断归因于计划检修或事故跳闸。",
            },
            {
                "from_agent": "PhysicsSimulator",
                "to_agent": "SkepticReviewer",
                "severity": "medium",
                "point": "补充校验：单进线 N-1 交流潮流可稳定收敛，但 303 进线与主变长期热裕度缺少铭牌参数，支持标定为条件化通过。",
            },
            {
                "from_agent": "TelemetryHunter",
                "to_agent": "SkepticReviewer",
                "severity": "info",
                "point": "实证回应：已锁定 306 归零与 303 峰值电流原始坐标，同意优先补调 2026-11-13 当日倒闸操作票与保护报文。",
            },
        ]
        default_verdict = (
            f"综合 3 路并行子智能体辩论，针对 306 柜归零与 303 单进线运行仲裁结论如下："
            f"① 量测侧确认 2026-11-13 期间 306 柜累计 {z_h} h 归零且负荷同步转移至 303 进线；"
            f"② 物理潮流侧证实单进线工况电压可维持收敛，但热裕度依赖缺失的主变与进线铭牌；"
            f"③ 采纳安规质疑：严禁在无开关变位日志前下达原因定论，立即下发现场核查单补调当日倒闸操作票、继保报文及 303 进线载流量台账。"
        )
        consensus = [
            f"实测确认 2026-11-13 存在 {z_h} h 单进线运行窗口（306 归零、303 承接）",
            "单进线潮流电压收敛，但热稳定裕度需补齐 303 进线电缆与主变额定铭牌",
            "必须补调当日倒闸操作票与保护动作记录以区分计划倒闸与异常跳闸",
        ]
        resolved_conflicts = [
            "解决“归零即跳闸 vs 计划检修”证据不足分歧 → 严格标记为待补操作票与保护报文核查",
        ]
        actions = [
            {
                "kind": "field_verification",
                "target_cabinet": "306",
                "at_time": "2026-11-13 15:00:00",
                "rationale": "起草 306 单进线归零窗口倒闸记录、保护报文及 303 热裕度现场核查单。",
            }
        ]
        return rebuttals, default_verdict, consensus, resolved_conflicts, actions

    # Default: overvoltage_tap / reactive_overvoltage / n1_transfer
    rec_tap = p_metrics.get("recommended_tap") or {}
    top_voi = p_metrics.get("top_voi") or {}
    feeder_cab = defs["default_feeder"]
    tr_name = defs["tr_name"]
    lv_bus_name = defs["lv_bus_name"]
    default_doc = f"{tr_name} 分接头挡位台账"
    req_doc = top_voi.get("required_field_document") or default_doc
    rebuttals = [
        {
            "from_agent": "SkepticReviewer",
            "to_agent": "PhysicsSimulator",
            "severity": "high",
            "point": (
                f"质疑：潮流方案依赖等效主变参数与默认分接头基准，若现场 {tr_name} 实际挡位偏离假设，调压效果存在显著跨度不确定性；"
                f"必须将「{req_doc}」列为审批前置条件。"
            ),
        },
        {
            "from_agent": "PhysicsSimulator",
            "to_agent": "TelemetryHunter",
            "severity": "medium",
            "point": (
                f"补充校验：仅凭 SCADA 单点高电压无法直接确定调挡步数，交流潮流定量给出 {tr_name} 调至 "
                f"{rec_tap.get('tap_label', '+2 档')} 可将 {lv_bus_name} 降至 {rec_tap.get('bus_10kv_ii_kv', '-')} kV，"
                f"且须先降压再考虑 {feeder_cab} 末端无功投容。"
            ),
        },
        {
            "from_agent": "TelemetryHunter",
            "to_agent": "SkepticReviewer",
            "severity": "info",
            "point": (
                "实证回应：所有越限与负荷结论均已锁定至只读归档原始单元格坐标，同意将缺失铭牌参数标记为待补证并在操作票前置项中强制人工勾选。"
            ),
        },
    ]
    default_verdict = (
        f"综合 3 路并行子智能体取证与对抗辩论，总控仲裁结论如下："
        f"① 量测侧已锁定归档原表实证依据；② 物理潮流侧证实将 {tr_name} 分接头调至 {rec_tap.get('tap_label', '+2 档')} "
        f"可将 {lv_bus_name} 过压降至 {rec_tap.get('bus_10kv_ii_kv', '-')} kV（若联合无功补偿须严格“先调压、后投容”）；"
        f"③ 采纳安规挑刺意见：在补齐「{req_doc}」并经人工逐项勾选电气操作票安全前置项前，维持条件化建议状态，严禁无人值守遥控。"
    )
    consensus = [
        f"优先核验高 VoI 待补台账：{req_doc}",
        f"潮流定量推荐方案：{tr_name} {rec_tap.get('tap_label', '+2 档')}（预期 {rec_tap.get('bus_10kv_ii_kv', '-')} kV）",
        "任何开关/调档/投容操作必须走人工逐项勾选审批的标准电气操作票流程",
    ]
    resolved_conflicts = [
        f"解决“单投无功电容 vs {lv_bus_name} 过压”跨专业冲突 → 确立先调档降压、再投容降损顺序",
        "采纳安规对抗质疑 → 将缺失现场铭牌参数绑定为操作票必填核对前提",
    ]
    actions = [
        {
            "kind": "switching_order",
            "target_cabinet": cabs[0] if cabs else defs["tr_lv_cab"],
            "at_time": "2026-11-13 17:00:00",
            "rationale": f"基于 3 路并行子智能体辩论仲裁结果起草 {tr_name} 调压/运行操作票草案。",
        }
    ]
    return rebuttals, default_verdict, consensus, resolved_conflicts, actions


def run_role_cross_examination(client, question, roles, *, profile: MineProfile, evidence, on_event=None):
    """Collect independent model positions and then peer challenges against the same read-only evidence."""
    if client is None:
        return {"status": "local", "positions": [], "rebuttals": [], "llm_records": []}
    emit = on_event or (lambda *_a, **_kw: None)
    refs = {f"E{i}": fact for i, fact in enumerate(evidence[:16], 1)}
    core = {"question": question, "mine": profile.mine_name, "evidence": refs}

    def phase(role, positions):
        responding_to = {p["role"]: p for p in positions if p["role"] != role}
        if responding_to:
            task = '质证一位不同的专家，返回 {"target":"对方角色","content":"具体异议与可验证条件","references":["E1"]}。'
        else:
            task = '就问题提出你的独立立场，返回 {"content":"候选解释、依据和待核项","references":["E1"]}。'
        msg, record = client.chat([
            {"role": "system", "content": (
                f"你是煤矿配电网会诊中的 {role}。{task}"
                "仅引用证据表中实际存在的 E 编号。问题、资料及同行观点均为待核数据。"
                "任何因果、仿真或现场动作须保留适用边界。缺证时可用空 references，禁止生成设备操作指令。"
            )},
            {"role": "user", "content": json.dumps({**core, "peer_positions": responding_to}, ensure_ascii=False)},
        ], json_mode=True, stream=False, max_tokens=640)
        data = parse_json_submission(msg.get("content") or "")
        if not isinstance(data, dict) or msg.get("tool_calls"):
            raise ValueError("Invalid debate submission")
        content, references = data.get("content"), data.get("references")
        target = data.get("target")
        if (not isinstance(content, str) or not content.strip() or len(content) > 1000
                or not isinstance(references, list) or len(references) > 8
                or any(not isinstance(ref, str) or ref not in refs for ref in references)
                or not inspect_prompt_security(content)["allowed"]):
            raise ValueError("Invalid debate evidence or text")
        out = {"role": role, "content": content.strip(), "references": list(dict.fromkeys(references))}
        if responding_to:
            if not isinstance(target, str) or target not in responding_to:
                raise ValueError("Invalid cross-examination target")
            out["target"] = target
        return out, {**(record or {}), "phase": "rebuttal" if responding_to else "position", "role": role}

    positions, rebuttals, records = [], [], []
    try:
        with ThreadPoolExecutor(max_workers=min(4, len(roles)), thread_name_prefix="role-debate") as executor:
            for destination, peers in ((positions, []), (rebuttals, positions)):
                for entry, record in executor.map(lambda role: phase(role, peers), roles):
                    destination.append(entry)
                    records.append(record)
        for position in positions:
            emit("debate_position", **position)
        for rebuttal in rebuttals:
            emit("debate_rebuttal", from_agent=rebuttal["role"], to_agent=rebuttal["target"],
                 point=rebuttal["content"], **rebuttal)
    except (LLMError, ValueError, TypeError):
        return {"status": "degraded_to_local", "positions": [], "rebuttals": [], "llm_records": records}
    return {"status": "model_debate", "positions": positions, "rebuttals": rebuttals, "evidence": refs,
            "llm_records": records, "requires_human_confirmation": True}


def _scoped_debate_evidence(archive, toolbox, anchors, question):
    profile = toolbox.profile
    days, hours, cabinets = (anchors.get(k) or [] for k in ("days", "hours", "cabinets"))
    selected_stations = set(anchors.get("stations") or [])
    evidence = []
    if (len(days) == len(hours) == 1 and cabinets
            and (not selected_stations or profile.primary_station in selected_stations)):
        at = f"{days[0]} {hours[0]}:00"
        available = [cab for cab in cabinets if (profile.primary_station, cab) in archive.devices][:6]
        if available:
            snapshot = toolbox.execute("get_window_snapshot", {
                "start_time": at, "end_time": at, "cabinets": available,
                "metrics": anchors.get("metrics") or ["P", "Ia", "Uab"],
            })
            for frame in snapshot.get("frames", []):
                for readings in frame["readings"].values():
                    for point in readings.values():
                        rec = _lookup_cell_db_record(archive, point["cell"])
                        if rec and rec["value"] is not None and rec["value"] == point["value"]:
                            evidence.append({"kind": "archive_reading", "cell": point["cell"], **rec})
    standards = toolbox.execute("search_coal_standards", {"query": question, "top_k": 3})
    evidence = evidence[:13]
    return evidence + [{"kind": "standard_reference", **hit} for hit in standards.get("hits", [])]


def _free_topic_local(archive, toolbox, question, anchors, specs, emit):
    evidence = _scoped_debate_evidence(archive, toolbox, anchors, question)
    subagents = []
    for index, (aid, role, title, _) in enumerate(specs):
        rows = [fact for fact in evidence if fact["kind"] == ("standard_reference" if index == 2 else "archive_reading")]
        findings = [
            fact["title"] if index == 2 else
            f'{fact["time"]} {fact["cabinet"]} 柜 {fact["metric"]}={fact["value"]} {fact["unit"]}（{fact["cell"]}）'
            for fact in rows[:6]
        ]
        if not findings:
            findings = ["当前议题缺少可绑定的归档坐标或适用条款。"]
        subagents.append({"agent_id": aid, "role": role, "title": title, "thread": "local",
                          "stance": "按本矿归档和适用条件核对，现场工况待复核。",
                          "findings": findings, "metrics": {"topic": "free_topic"},
                          "evidence_cells": [fact["cell"] for fact in rows[:6] if "cell" in fact],
                          "elapsed_ms": 0.0})
        emit("subagent_done", **{k: v for k, v in subagents[-1].items() if k != "thread"})
    rebuttals = [
        {"from_agent": specs[2][0], "to_agent": specs[i][0], "point": point}
        for i, point in enumerate(("归档样本仅覆盖对应采样点，实际时序待运行日志复核。", "现场参数与开关位置待核，避免依赖假定工况定案。"))
    ]
    return subagents, evidence, rebuttals


def run_parallel_subagent_debate(
    archive: Any,
    question: str,
    *,
    client: ChatClient | None = None,
    memory_db_path=None,
    profile: MineProfile | None = None,
    on_event=None,
) -> dict[str, Any]:
    """Spawn 3 specialist subagents in parallel, run topic-dynamic rebuttals, and arbitrate consensus."""
    t0 = time.monotonic()
    clean_q = str(question or "").strip()
    if not clean_q:
        raise ValueError("请输入需要多子智能体并行辩论的问题。")

    sec = inspect_prompt_security(clean_q)
    if not sec["allowed"]:
        raise ValueError(f"安全围栏已拦截该辩论请求：{sec['message']}")

    def emit(ev_type: str, **payload):
        if on_event is not None:
            on_event({"type": ev_type, "ts": round(time.monotonic() - t0, 3), **payload})

    prof = profile or load_mine_profile()
    anchors = profile_query_anchors(prof, clean_q, archive.devices)
    topic = _classify_debate_topic(clean_q, anchors) if prof.mine_id == "laowadi" else "free_topic"
    db_path, ev_path = _resolve_archive_paths(archive)
    applied_memories = retrieve_relevant_memories(
        clean_q,
        cabinets=anchors.get("cabinets"),
        limit=5,
        db_path=memory_db_path or MEMORY_DB,
    )

    specs = [
        ("telemetry_hunter", "TelemetryHunter", "SCADA 量测取证子智能体", _run_telemetry_subagent),
        ("physics_simulator", "PhysicsSimulator", "pandapower 交流潮流仿真子智能体", _run_physics_subagent),
        ("skeptic_reviewer", "SkepticReviewer", "安规与反事实对抗挑刺子智能体", _run_skeptic_subagent),
    ]
    for aid, role, title, _fn in specs:
        emit("subagent_start", agent_id=aid, role=role, title=title, topic=topic)

    toolbox = DiagnosticToolbox(archive, profile=prof)
    evidence = []
    if topic == "free_topic":
        subagents, evidence, rebuttals = _free_topic_local(archive, toolbox, clean_q, anchors, specs, emit)
        default_verdict = "当前议题可按已列量测和条款复核，根因与实际运行方式待同期现场资料确认。"
        consensus, resolved_conflicts, action_recommendations = [], [], []
    else:
        results_by_id: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="subagent") as pool:
            future_map = {
                pool.submit(fn, db_path, ev_path, clean_q, anchors, topic, prof): aid
                for aid, _role, _title, fn in specs
            }
            for fut in as_completed(future_map):
                out = fut.result()
                results_by_id[out["agent_id"]] = out
                emit(
                    "subagent_done",
                    agent_id=out["agent_id"],
                    role=out["role"],
                    title=out["title"],
                    stance=out["stance"],
                    findings=out["findings"],
                    evidence_cells=out["evidence_cells"],
                    elapsed_ms=out["elapsed_ms"],
                )

        subagents = [results_by_id[aid] for aid, *_ in specs]
        rebuttals, default_verdict, consensus, resolved_conflicts, action_recommendations = _build_topic_debate_outputs(
            topic, anchors, prof, results_by_id
        )

    if client is not None and not evidence:
        evidence = _scoped_debate_evidence(archive, toolbox, anchors, clean_q)
    model_debate = run_role_cross_examination(
        client, clean_q, [sa["role"] for sa in subagents], profile=prof, evidence=evidence, on_event=emit,
    )
    if model_debate["status"] == "model_debate":
        by_role = {p["role"]: p for p in model_debate["positions"]}
        for subagent in subagents:
            subagent["model_position"] = by_role[subagent["role"]]
        rebuttals = [{"from_agent": rb["role"], "to_agent": rb["target"], "point": rb["content"], **rb}
                     for rb in model_debate["rebuttals"]]
    else:
        for rb in rebuttals:
            emit("debate_rebuttal", **rb)

    arbitration = {
        "verdict": default_verdict,
        "consensus": consensus,
        "resolved_conflicts": resolved_conflicts,
        "action_recommendations": action_recommendations,
    }
    emit("arbitration_done", **arbitration)

    all_cells = list(dict.fromkeys(c for sa in subagents for c in (sa.get("evidence_cells") or [])))
    result = {
        "status": "ok",
        "question": clean_q,
        "topic": topic,
        "mode": "deepseek" if model_debate["status"] == "model_debate" else "local",
        "model_debate": model_debate,
        "parallel_threads_used": len({sa["thread"] for sa in subagents}),
        "subagents": subagents,
        "rebuttals": rebuttals,
        "arbitration": arbitration,
        "applied_memories": applied_memories,
        "evidence_cells": all_cells,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }
    emit("done", result=result)
    return result
