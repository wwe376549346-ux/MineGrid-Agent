"""Workstream 3: 5-Role Hierarchical Multi-Agent Specialist Collaboration & Skeptic Arbitration.

Architecture:
1. SupervisorPlannerAgent   — Security firewall + query decomposition + scoped specialist dispatch.
2. TelemetryMeteringAgent   — Scoped SCADA health, metering ratio/polarity, balance & diurnal tools.
3. PhysicsPowerFlowAgent    — Scoped pandapower Newton-Raphson AC power flow & reactive/N-1 tools.
4. StandardsComplianceAgent — Scoped Hybrid BM25+Semantic RRF RAG & coal-mine safety standards.
5. SkepticAuditorAgent      — Evidence-based arbitration and two-level groundedness audit.

Each parallel specialist opens its own read-only SQLite connection to guarantee thread safety
without mutating the archived mine databases.
"""

from __future__ import annotations

from pathlib import Path
import re
import time
from typing import Any
import uuid

from laowadi.diag_tools import DiagnosticToolbox
from laowadi.diagnose import diagnose_question
from laowadi.llm import ChatClient
from laowadi.mine_schema import load_mine_profile
from laowadi.local_router import (
    _parse_capacity_params,
    _parse_physics_simulation,
    _parse_powerflow_request,
    _summarize_tool_result,
)
from laowadi.context import extract_query_anchors
from laowadi.outcomes import classify_outcome
from laowadi.queries import Archive
from laowadi import reactive_collaboration as reactive
from laowadi.report_evidence import render_tool_facts
from laowadi.security import inspect_prompt_security
from laowadi.storage import REPORT_STATION
from laowadi.verifier import (
    EVIDENCE_POLICY,
    bind_tool_fact,
    blocked_diagnosis_report,
    build_missing_date_refusal,
    build_observations,
    diagnostic_boundary_verdict,
    finalize_diagnosis,
)


AGENT_ROLES: dict[str, dict[str, Any]] = {
    "supervisor": {
        "id": "supervisor",
        "name": "SupervisorPlannerAgent",
        "title": "总控规划调度员",
        "allowed_tools": [],
    },
    "telemetry": {
        "id": "telemetry",
        "name": "TelemetryMeteringAgent",
        "title": "SCADA 量测与健康体检专家",
        "allowed_tools": [
            "get_station_health_overview",
            "get_window_snapshot",
            "check_power_balance",
            "get_cabinet_health_profile",
            "get_diurnal_baseline",
            "query_electrical_analytics",
        ],
    },
    "physics": {
        "id": "physics",
        "name": "PhysicsPowerFlowAgent",
        "title": "pandapower 交流潮流与物理仿真专家",
        "allowed_tools": [
            "run_ac_power_flow",
            "simulate_electrical_engineering",
            "verify_conditional_capacity",
            "solve_reactive_study_state",
        ],
    },
    "standards": {
        "id": "standards",
        "name": "StandardsComplianceAgent",
        "title": "煤矿安规与证据边界合规专家",
        "allowed_tools": [
            "retrieve_conditional_evidence",
            "search_coal_standards",
        ],
    },
    "auditor": {
        "id": "auditor",
        "name": "SkepticAuditorAgent",
        "title": "独立对抗审计与跨专业冲突仲裁员",
        "allowed_tools": [],
    },
}


def _parse_hints(archive, question: str) -> dict[str, Any]:
    day_m = re.search(r"(?:(\d{4})[年-])?(\d{1,2})[月-](\d{1,2})", question)
    day = f"{int(day_m.group(1) or 2026):04d}-{int(day_m.group(2)):02d}-{int(day_m.group(3)):02d}" if day_m else None
    scrubbed = re.sub(r"(?:(\d{4})[年-])?(\d{1,2})[月-](\d{1,2})[日号]?", " ", question)
    hour_m = re.search(r"(?<!\d)(\d{1,2})\s*(?:点|时|:00)", scrubbed)
    hour = int(hour_m.group(1)) if hour_m else None

    s35_cabs = {num for st, num in archive.devices if st == REPORT_STATION}
    c1535_cabs = {num for st, num in archive.devices if st == "C1535"}
    mentioned_s35 = [c for c in sorted(s35_cabs, key=int) if re.search(rf"(?<!\d){re.escape(c)}(?!\d)", question)]
    mentioned_c1535 = [c for c in sorted(c1535_cabs, key=int) if re.search(rf"(?<!\d){re.escape(c)}(?!\d)", question)]
    return {
        "day": day,
        "hour": hour,
        "mentioned_s35": mentioned_s35,
        "mentioned_c1535": mentioned_c1535,
        "cap_params": _parse_capacity_params(question),
    }


def _plan_consultation(question: str, hints: dict[str, Any]) -> dict[str, Any]:
    """Role 1: Supervisor decomposes the question and selects specialists + concrete scoped sub-tasks."""
    q = question
    day = hints["day"]
    hour = hints["hour"]
    cabs = hints["mentioned_s35"]
    c1535 = hints["mentioned_c1535"]

    # Always dispatch Telemetry & Standards; dispatch Physics whenever voltage/tap/reactive/N-1/overload/powerflow is involved
    needs_physics = bool(
        _parse_powerflow_request(q, cabs, day, hour)
        or _parse_physics_simulation(q, cabs, day, hints["cap_params"])
        or any(k in q for k in ("过压", "电压", "分接头", "调压", "过载", "裕度", "无功", "补偿", "功率因数", "N-1", "n-1", "转供", "潮流", "网损", "压降", "单进线"))
    )
    if c1535 and not cabs and not any(k in q for k in ("N-1", "n-1", "转供", "潮流")):
        needs_physics = False

    selected = ["telemetry", "standards"]
    if needs_physics:
        selected.insert(1, "physics")

    # Decide best AC power-flow scenario for PhysicsPowerFlowAgent
    pf_req = _parse_powerflow_request(q, cabs, day, hour)
    if pf_req is None and needs_physics:
        if any(k in q for k in ("无功", "补偿", "功率因数", "kVar", "降损")):
            pf_req = {
                "scenario": "reactive_opt",
                "target_cabinet": "1020" if "1020" in cabs else ("1009" if "1009" in cabs else "1010"),
            }
        elif any(k in q for k in ("N-1", "n-1", "转供", "1009", "1010")) and not any(k in q for k in ("303", "306")):
            pf_req = {"scenario": "n1_contingency", "outage_mode": "feeder_1009"}
        elif any(k in q for k in ("306", "303", "单进线")) and any(k in q for k in ("过压", "1006", "II段", "分接头")):
            pf_req = {"scenario": "tap_sensitivity", "at_time": "2026-11-13 17:00:00"}
        elif any(k in q for k in ("306", "303", "单进线")):
            pf_req = {"scenario": "n1_contingency", "outage_mode": "incomer_306", "at_time": "2026-11-13 15:00:00"}
        elif any(k in q for k in ("过压", "电压", "1006", "II段", "分接头", "调压")):
            pf_req = {"scenario": "tap_sensitivity", "at_time": "2026-11-13 17:00:00"}
        else:
            pf_req = {"scenario": "base_case"}
        if day and hour is not None and "at_time" not in pf_req:
            pf_req["at_time"] = f"{day} {hour:02d}:00:00"

    return {
        "selected_specialists": selected,
        "execution_mode": "parallel_threads",
        "hints": hints,
        "powerflow_spec": pf_req,
        "subtasks": {
            "telemetry": "检索归档 SQLite 25 柜体检、目标柜画像、功率守恒及极性/比例量测口径异常",
            "physics": f"执行 pandapower Newton-Raphson 交流潮流（{pf_req['scenario'] if pf_req else 'n/a'}）与物理仿真校核",
            "standards": "执行混合 BM25+语义 RRF 检索本矿证据卡、68 条安规国标条款与跨站缺证门控",
        },
    }


def _run_specialist_in_archive(
    role_id: str,
    question: str,
    plan: dict[str, Any],
    archive: Archive,
    reactive_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    t0 = time.monotonic()
    tb = DiagnosticToolbox(archive)
    allowed = set(AGENT_ROLES[role_id]["allowed_tools"])
    hints = plan["hints"]
    day = hints["day"]
    hour = hints["hour"]
    cabs = hints["mentioned_s35"]
    c1535 = hints["mentioned_c1535"]
    cap_params = hints["cap_params"]
    trace: list[dict[str, Any]] = []

    def call_scoped(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool_name not in allowed:
            raise PermissionError(f"{role_id} 无权调用工具 {tool_name}")
        res = tb.execute(tool_name, args)
        trace.append({"round": 1, "agent": role_id, "tool": tool_name, "arguments": args, "result": res})
        return res

    def sourced(fact, result, *path, kind="calculation"):
        step = next(step for step in reversed(trace) if step["result"] is result)
        return bind_tool_fact(fact, step, *path, kind=kind)

    findings: list[dict[str, Any]] = []
    evidence_cells: list[str] = []
    assumptions: list[str] = []
    signals: dict[str, Any] = {}
    messages: list[dict[str, Any]] = []
    case_frame = None

    if role_id == "telemetry":
        if plan.get("reactive_request"):
            message, case_frame = reactive.telemetry_case(archive, tb.profile, plan["reactive_request"], hints, call_scoped)
            messages.append(message)
            if case_frame:
                refs = [message["payload"][key]["cell"] for key in ("power_factor", "voltage", "power")]
                evidence_cells.extend(refs)
                findings.extend(build_observations(archive, refs, trace) or [])
        elif plan.get("n1_request"):
            message, case_frame = reactive.n1_telemetry_case(archive, tb.profile, plan["n1_request"], hints, call_scoped)
            messages.append(message)
            if case_frame:
                refs = [
                    message["payload"][key]["cell"]
                    for key in ("ia_outage", "ia_receiving", "p_outage", "p_receiving", "bus_voltage")
                    if isinstance(message["payload"].get(key), dict) and message["payload"][key].get("cell")
                ]
                evidence_cells.extend(refs)
                findings.extend(build_observations(archive, refs, trace) or [])
        ov = call_scoped("get_station_health_overview", {"day": day, "severity": "all"} if day else {"severity": "all"})
        if ov.get("status") != "ok":
            ov = call_scoped("get_station_health_overview", {"severity": "all"})
        all_findings = ov.get("findings") or []

        # Always inspect metering/voltage station-level signals for cross-agent conflict detection
        bus2_ov = next((f for f in all_findings if f.get("id") == "HLTH-PQ-VOLT-10KV-BUS2"), None)
        if bus2_ov:
            signals["bus2_overvoltage"] = True
            signals["bus2_overvoltage_summary"] = bus2_ov["summary"]
            ev = [f'{e["file"]} / {e["sheet"]}!{e["cell"]}' for e in bus2_ov.get("evidence", []) if isinstance(e, dict) and "cell" in e]
            evidence_cells.extend(ev)
            findings.append(sourced(
                {"statement": f'{bus2_ov["title"]}：{bus2_ov["summary"]}', "evidence": ev[:2]},
                ov, "findings", all_findings.index(bus2_ov), kind="screening",
            ))

        # Target cabinet profiles (up to 3)
        inspect_cabs = list(dict.fromkeys(cabs[:3]))
        if not inspect_cabs and any(k in question for k in ("无功", "补偿", "1010", "N-1", "n-1")):
            inspect_cabs = ["1010", "1009"]
        elif not inspect_cabs and any(k in question for k in ("过压", "分接头", "1006")):
            inspect_cabs = ["1006", "1005"]
        elif not inspect_cabs and any(k in question for k in ("单进线", "306", "303")):
            inspect_cabs = ["303", "306"]

        for cab in inspect_cabs[:3]:
            prof = call_scoped("get_cabinet_health_profile", {"cabinet": cab})
            if prof.get("status") == "ok" and prof.get("has_hourly_readings"):
                dev = prof["device"]
                m_p = prof["metrics_7d"].get("P", {})
                m_i = prof["metrics_7d"].get("Ia", {})
                m_c = prof["metrics_7d"].get("cos", {})
                m_chk = prof.get("metering_check") or {}
                ev = [c for c in (m_p.get("max_cell"), m_i.get("max_cell"), m_c.get("min_cell")) if c]
                evidence_cells.extend(ev)
                findings.append(sourced({
                    "statement": (
                        f'{dev["number"]} 柜（{dev["label"]}）量测画像：有功 {m_p.get("min")}~{m_p.get("max")} kW，'
                        f'电流峰值 {m_i.get("max")} A，cos 均值 {m_c.get("avg")}；量测校验：{m_chk.get("note", "正常")}'
                    ),
                    "evidence": ev[:3],
                }, prof, kind="screening"))
                if m_chk.get("status") == "persistent_negative_power":
                    signals["negative_polarity_cabinet"] = cab
                if m_chk.get("status") == "systematic_scale_offset":
                    signals.setdefault("ratio_offset_cabinets", {})[cab] = m_chk.get("ratio_median")

        # Power balance at specific hour
        if day and hour is not None and 0 <= hour <= 23:
            bal = call_scoped("check_power_balance", {"at_time": f"{day} {hour:02d}:00:00"})
            if bal.get("status") == "ok":
                result_facts = render_tool_facts(trace[-1])
                evidence_cells.extend(c for fact in result_facts for c in fact["evidence"])
                findings.extend(result_facts)
                signals["balance_flags"] = bal["flags"]

        # Reverse-flow falsification on 1027 if 2026-11-12 or reverse flow mentioned
        if (day == "2026-11-12") or any(k in question for k in ("反向", "倒送", "瓦斯", "1027")):
            snap = call_scoped(
                "get_window_snapshot",
                {
                    "start_time": f"{day or '2026-11-12'} 11:00:00",
                    "end_time": f"{day or '2026-11-12'} 13:00:00",
                    "cabinets": ["306", "312", "1006", "1027"],
                    "metrics": ["P", "Ia", "cos"],
                },
            )
            if snap.get("status") == "ok":
                fr12 = next((fr for fr in snap.get("frames", []) if fr["time"].endswith("12:00:00")), None)
                if fr12:
                    r1027 = fr12["readings"].get("1027") or {}
                    p1027 = (r1027.get("P") or {}).get("value")
                    ia1027 = (r1027.get("Ia") or {}).get("value")
                    ev_f = [r1027[m]["cell"] for m in ("P", "Ia") if (r1027.get(m) or {}).get("cell")]
                    observations = build_observations(tb.archive, ev_f, trace) or []
                    evidence_cells.extend(ev_f)
                    signals["falsified_1027_reverse"] = p1027 == 0 and ia1027 == 0
                    findings.extend(observations)


        # Handle C1535 underground inquiries explicitly
        for ug in c1535[:2]:
            prof = call_scoped("get_cabinet_health_profile", {"cabinet": ug})
            if prof.get("status") == "ok":
                signals["c1535_boundary"] = True
                findings.append(sourced({
                    "statement": f'井下 1535 中央变电所 {ug} 柜：{prof.get("note", "仅存在于一次系统图台账，无小时级运行日报读数。")}',
                    "evidence": [],
                }, prof, kind="coverage"))

    elif role_id == "physics" and reactive_inputs is not None and plan.get("reactive_request"):
        message = reactive.propose(tb.profile, reactive_inputs["messages"][0],
                                   reactive_inputs["case_frame"], plan["reactive_request"])
        messages.append(message)
        if message["payload"]["powerflow_states"]:
            trace.append({
                "round": 1, "agent": role_id, "tool": "solve_reactive_study_state",
                "arguments": plan["reactive_request"], "result": message["payload"],
            })
            assumptions.append(message["payload"]["model_assumptions"])

    elif role_id == "physics":
        if reactive_inputs is not None and plan.get("n1_request"):
            n1_msg = reactive.n1_propose(
                archive, tb.profile, reactive_inputs["messages"][0], reactive_inputs["case_frame"], plan["n1_request"],
            )
            messages.append(n1_msg)
        pf_spec = plan.get("powerflow_spec") or {"scenario": "base_case"}
        pf_res = call_scoped("run_ac_power_flow", pf_spec)
        if pf_res.get("status") == "ok":
            ns = pf_res["network_summary"]
            assump = pf_res["model_assumptions"]
            assumptions.append(
                f'等值主变 {assump["transformer_sn_mva"]} MVA / {assump["transformer_vn_kv"]} kV / Uk={assump["transformer_vk_percent"]}%，'
                f'校准分接头 T1={assump["tr1_tap_pos"]:+d} / T2={assump["tr2_tap_pos"]:+d} 档，下井电缆 {assump["downhole_1009_1010_cable_length_km"]} km（非现场铭牌实测）。'
            )
            result_facts = render_tool_facts(trace[-1])
            evidence_cells.extend(c for fact in result_facts for c in fact["evidence"])
            findings.extend(result_facts)
            signals["powerflow_scenario"] = pf_res["scenario"]
            signals["bus_10kv_ii_kv"] = ns["bus_10kv_ii_kv"]
            signals["total_loss_kw"] = ns["total_loss_kw"]
            if pf_res.get("n1_contingency"):
                signals["n1_powerflow"] = pf_res["n1_contingency"].get("receiving_1010_comparison") or {}
            if pf_res.get("reactive_opt"):
                signals["reactive_opt"] = pf_res["reactive_opt"]

        # Also run analytical Qc / N-1 simulation or conditional capacity when relevant
        phys_sim = _parse_physics_simulation(question, cabs, day, cap_params)
        if phys_sim:
            sim_res = call_scoped("simulate_electrical_engineering", phys_sim)
            if sim_res["status"] in ("ok", "warning"):
                result_facts = render_tool_facts(trace[-1])
                evidence_cells.extend(c for fact in result_facts for c in fact["evidence"])
                findings.extend(result_facts)
            if sim_res["status"] == "ok" and phys_sim["operation"] == "n1_transfer":
                signals["n1_metered_peak_a"] = sim_res["worst_current_hour"]["combined_Ia_algebraic_a"]

        if cap_params or any(k in question for k in ("过载", "裕度", "载流量")):
            cap_res = call_scoped("verify_conditional_capacity", cap_params)
            signals["capacity_version"] = cap_res.get("version", 1)

    elif role_id == "standards":
        rag_args: dict[str, Any] = {"query": question, "top_k": 3}
        if cabs or c1535:
            rag_args["cabinet"] = (cabs + c1535)[0]
        if day:
            rag_args["day"] = day
        rag_res = call_scoped("retrieve_conditional_evidence", rag_args)
        std_q = "电能质量 电压偏差 功率因数 无功补偿 两回路电源"
        if plan.get("reactive_request"):
            std_q = "GB/T 12325 供电电压偏差 10 kV"
        elif any(k in question for k in ("N-1", "n-1", "单进线", "306", "转供", "两回路")):
            std_q = "GB/T 50417-2017 两回路电源 分列运行 备用电源"
        elif any(k in question for k in ("电压", "过压", "分接头")):
            std_q = "GB/T 12325 电压偏差 10kV 35kV"
        std_res = call_scoped("search_coal_standards", {"query": std_q, "top_k": 3})

        cited = []
        for hit in (std_res.get("hits") or [])[:3]:
            cited.append({"id": hit["id"], "relevance": f'{hit["title"]}（{hit["pdf_page"]}）'})
        for hit in (rag_res.get("standard_clauses") or [])[:2]:
            if all(c["id"] != hit["id"] for c in cited):
                cited.append({"id": hit["id"], "relevance": f'{hit["title"]}（{hit["pdf_page"]}）'})

        signals["cited_standards"] = cited[:3]
        signals["applicability_warnings"] = rag_res.get("applicability_warnings") or []
        signals["missing_evidence"] = rag_res.get("missing_evidence") or []
        if plan.get("reactive_request") or plan.get("n1_request"):
            signals["reactive_policy"] = reactive.standards_policy(tb.profile, std_res.get("hits") or [])

        for index, warning in enumerate(signals["applicability_warnings"]):
            findings.append(sourced({"statement": warning, "evidence": []},
                                     rag_res, "applicability_warnings", index, kind="coverage"))

    elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)
    return {
        "role_id": role_id,
        "agent_name": AGENT_ROLES[role_id]["name"],
        "agent_title": AGENT_ROLES[role_id]["title"],
        "latency_ms": elapsed_ms,
        "tools_called": [t["tool"] for t in trace],
        "tool_summaries": [f'{t["tool"]}: {_summarize_tool_result(t["tool"], t["result"])}' for t in trace],
        "findings": findings,
        "evidence_cells": list(dict.fromkeys(evidence_cells)),
        "assumptions": assumptions,
        "signals": signals,
        "tool_trace": trace,
        "messages": messages,
        "case_frame": case_frame,
    }


def _detect_and_arbitrate_conflicts(briefs_by_role: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Role 5 (SkepticAuditorAgent): Surface & resolve cross-specialist engineering conflicts."""
    tel = briefs_by_role.get("telemetry", {}).get("signals") or {}
    phy = briefs_by_role.get("physics", {}).get("signals") or {}
    std = briefs_by_role.get("standards", {}).get("signals") or {}
    conflicts: list[dict[str, Any]] = []

    # Conflict 2: Metering ratio offset / polarity anomaly vs balanced 3-phase AC power flow!
    ratios = tel.get("ratio_offset_cabinets") or {}
    if ratios and (phy.get("n1_powerflow") or phy.get("n1_metered_peak_a")):
        ratio_desc = "、".join(f"{c}柜比例系数={r}" for c, r in ratios.items())
        n1_pf = phy.get("n1_powerflow") or {}
        conflicts.append({
            "id": "CONFLICT-METERING-RATIO-VS-POWERFLOW",
            "severity": "high",
            "agents_involved": ["TelemetryMeteringAgent", "PhysicsPowerFlowAgent"],
            "tension": (
                f'量测专家检出目标回路存在系统性计量口径偏离（{ratio_desc}），'
                f'表计合成峰值与平衡三相潮流估算值须按各自口径核对。'
                + (f'表计合成峰值 {peak} A。' if (peak := phy.get("n1_metered_peak_a")) is not None else "")
                + (f'平衡三相潮流估算 {current} A。' if (current := n1_pf.get("after_i_a")) is not None else "")
            ),
            "resolution": (
                "仲裁口径：热稳定安全校核须按较保守的表计实测电流上限与现场核校后的真实 CT 变比双口径复核，"
                "先排除两表法/CT 变比折算偏差后再终审电缆载流裕度，避免盲目乐观或误扩容。"
            ),
        })

    if tel.get("negative_polarity_cabinet"):
        cab = tel["negative_polarity_cabinet"]
        conflicts.append({
            "id": "CONFLICT-POLARITY-PROHIBITS-SIZING",
            "severity": "high",
            "agents_involved": ["TelemetryMeteringAgent", "PhysicsPowerFlowAgent"],
            "tension": f"{cab} 柜归档量测触发负有功筛查，原因及计量口径待核。",
            "resolution": f"仲裁结论：将 {cab} 柜标记为量测极性待核项，须现场相位仪校验 CT S1/S2 接线后方可纳入定量优化。",
        })

    # Conflict 3: Equivalent-circuit assumptions vs strict archive boundary
    phy_assumps = briefs_by_role.get("physics", {}).get("assumptions") or []
    if phy_assumps:
        conflicts.append({
            "id": "CONFLICT-ASSUMPTION-VS-ARCHIVE",
            "severity": "medium",
            "agents_involved": ["PhysicsPowerFlowAgent", "StandardsComplianceAgent"],
            "tension": "交流潮流求解需要主变额定容量、短路阻抗 Uk% 及下井电缆参数，但老洼地归档 7 份日报与 2 张截图未包含设备铭牌台账。",
            "resolution": "仲裁底线（严防参数编造）：所有潮流数值均明确标注为「实测整点边界 + 参数化等值模型假设」的条件校核解，正式停电调档或定值修改前必须由现场铭牌台账替换复算。",
        })

    # Conflict 4: Reverse-flow falsification consensus
    if tel.get("falsified_1027_reverse"):
        conflicts.append({
            "id": "CONSENSUS-FALSIFIED-1027-EXPORT",
            "severity": "info",
            "agents_involved": ["TelemetryMeteringAgent", "StandardsComplianceAgent"],
            "tension": "针对 2#主变（306/312/1006）全链反向有功，需区分「瓦斯发电并网倒送」与「轻载/倒闸环流或表计零漂」。",
            "resolution": "1027 柜在该归档整点的有功与电流均为零。并网状态、小时内反送及其他路径须核对同期日志和录波。",
        })

    if std.get("applicability_warnings"):
        conflicts.append({
            "id": "BOUNDARY-CROSS-STATION-C1535",
            "severity": "high",
            "agents_involved": ["StandardsComplianceAgent", "TelemetryMeteringAgent"],
            "tension": "用户查询涉及井下 1535 中央变电所（C1535）分柜读数，但归档日报仅覆盖地面 35/10 kV 变电所（S35）。",
            "resolution": "合规门控已拒绝编造井下分柜读数，仅提供图示上下游供电关系与上级地面送电柜实测边界，并输出井下监控报表补证清单。",
        })

    return conflicts


def run_multi_agent_consultation(
    archive: Archive,
    question: str,
    *,
    client: ChatClient | None = None,
    on_event=None,
    thread_id: str | None = None,
    checkpoint_db: Path | str | None = None,
    pause_before_node: str | None = None,
    pause_after_node: str | None = None,
    human_input: dict[str, Any] | None = None,
    resume_policy: str = "strict",
) -> dict[str, Any]:
    """Execute the 5-Role Multi-Agent Specialist Consultation as a dynamic parallel DAG on UnifiedDAGRuntime."""
    from laowadi.dag_engine import (
        DAGCheckpointStore,
        DAGGraph,
        DAGNode,
        DAGRunContext,
        NodeRestricted,
        UnifiedDAGRuntime,
        compute_snapshot,
    )

    t0 = time.monotonic()
    consult_id = thread_id or f"MAC-{uuid.uuid4().hex[:10].upper()}"
    persist_dag = bool(thread_id or pause_before_node or pause_after_node or human_input or checkpoint_db)

    def emit(ev_type: str, **payload):
        if on_event is not None:
            on_event({"type": ev_type, "ts": round(time.monotonic() - t0, 3), **payload})

    # Audit #08: Unified P0 Date-Coverage Guard matching primary diagnose_question
    known_cabs = {num for _, num in archive.devices}
    q_anchors = extract_query_anchors(question, known_cabs)
    available_days = archive.days()
    req_days = q_anchors.get("days") or []
    missing_req_days = [d for d in req_days if d not in set(available_days)]
    if req_days and len(missing_req_days) == len(req_days):
        base_ref = build_missing_date_refusal(
            question,
            question,
            missing_req_days,
            available_days,
            mode="local",
            model=None,
            elapsed_seconds=round(time.monotonic() - t0, 3),
            context_snapshot={"turn": 1, "raw_question": question, "resolved_question": question, "current_anchors": q_anchors, "active_anchors": q_anchors},
        )
        out_miss = {
            "kind": "multi_agent_consultation",
            "consultation_id": consult_id,
            "status": "insufficient_data",
            "stop_reason": "missing_requested_date",
            "missing_days": missing_req_days,
            "archive_date_range": base_ref["archive_date_range"],
            "question": question,
            "mode": "local",
            "model": None,
            "elapsed_seconds": round(time.monotonic() - t0, 3),
            "plan": {"selected_specialists": [], "execution_mode": "refused_missing_date"},
            "specialist_briefs": [],
            "inter_agent_messages": [],
            "conflicts_resolved": [],
            "parallel_speedup": {"wall_ms": 0.0, "sequential_sum_ms": 0.0, "speedup_ratio": 1.0},
            "report": base_ref["report"],
            "verification": base_ref["verification"],
            "tool_trace": [],
            "dag_execution": None,
        }
        outcome = classify_outcome(out_miss, execution_status="completed")
        out_miss["outcome_layers"] = outcome
        out_miss["business_status"] = outcome["business_status"]
        out_miss["business_trusted"] = outcome["business_trusted"]
        return out_miss

    hints = _parse_hints(archive, question)
    plan = _plan_consultation(question, hints)
    profile = load_mine_profile()
    reactive_req = reactive.reactive_request(question, hints, profile)
    n1_req = None if reactive_req else reactive.n1_transfer_request(question, hints, profile)
    exchange_active = bool(reactive_req or n1_req)
    if reactive_req:
        plan["reactive_request"] = reactive_req
        plan["selected_specialists"] = ["telemetry", "physics", "standards"]
        plan["execution_mode"] = "evidence_exchange"
        plan["subtasks"]["physics"] = "根据量测消息执行同点交流潮流，收到约束质疑后重算"
    elif n1_req:
        plan["n1_request"] = n1_req
        plan["selected_specialists"] = ["telemetry", "physics", "standards"]
        plan["execution_mode"] = "evidence_exchange"
        plan["subtasks"]["physics"] = "根据双回峰值量测执行 N-1 交流潮流，收到计量口径质疑后补算双口径安全包络"
    selected = plan["selected_specialists"]
    reader = archive.parallel_reader() if len(selected) > 1 else None
    use_parallel = reader is not None

    dag = DAGGraph("multi_agent_consultation")

    # Wave 0: Security Firewall Guard Node
    def _guard_node(_ctx: DAGRunContext) -> dict[str, Any]:
        sec = inspect_prompt_security(question)
        if not sec["allowed"]:
            raise NodeRestricted(sec["stop_reason"], sec["message"], payload={"security_block": sec})
        return {"allowed": True}

    dag.add_node(DAGNode(node_id="guard", kind="guard", title="总控安全围栏", handler=_guard_node))

    # Wave 1: Supervisor Planner Node
    def _plan_node(_ctx: DAGRunContext) -> dict[str, Any]:
        for role_id in selected:
            emit(
                "agent_spawn",
                agent=AGENT_ROLES[role_id]["name"],
                title=AGENT_ROLES[role_id]["title"],
                subtask=plan["subtasks"][role_id],
            )
        return {
            "selected_specialists": selected,
            "execution_mode": plan["execution_mode"],
            "subtasks": {k: plan["subtasks"][k] for k in selected},
        }

    dag.add_node(DAGNode(node_id="plan", kind="plan", title="总控任务拆解", handler=_plan_node, depends_on=["guard"]))

    # Wave 2: Parallel Specialist Fan-Out Nodes (each individually checkpointed & reusable on resume!)
    specialist_node_ids: list[str] = []
    for role_id in selected:
        nid = f"specialist:{role_id}"
        specialist_node_ids.append(nid)

        def _make_spec_handler(r_id: str):
            def _run_spec(ctx: DAGRunContext) -> dict[str, Any]:
                inputs = ctx.completed_outputs["specialist:telemetry"] if exchange_active and r_id == "physics" else None
                if reader:
                    with reader() as local_archive:
                        brief = _run_specialist_in_archive(r_id, question, plan, local_archive, inputs)
                else:
                    brief = _run_specialist_in_archive(r_id, question, plan, archive, inputs)
                emit(
                    "agent_brief",
                    agent=brief["agent_name"],
                    title=brief["agent_title"],
                    latency_ms=brief["latency_ms"],
                    tools=brief["tools_called"],
                    findings_count=len(brief["findings"]),
                )
                return brief

            return _run_spec

        metadata = {"role_id": role_id, "agent_name": AGENT_ROLES[role_id]["name"]}
        if exchange_active and role_id == "telemetry":
            # Cabinet profiles and an unpinned search may depend on the whole report span.
            metadata["data_scope"] = {"reads_all_days": True}
        dag.add_node(
            DAGNode(
                node_id=nid,
                kind="specialist",
                title=AGENT_ROLES[role_id]["title"],
                handler=_make_spec_handler(role_id),
                depends_on=["specialist:telemetry"] if exchange_active and role_id == "physics" else ["plan"],
                metadata=metadata,
            )
        )

    def _reactive_inputs(ctx):
        outputs = ctx.completed_outputs
        return outputs["specialist:telemetry"]["case_frame"], outputs["specialist:physics"]["messages"][0]

    def _challenge_node(ctx):
        frame, proposal = _reactive_inputs(ctx)
        policy = ctx.completed_outputs["specialist:standards"]["signals"]["reactive_policy"]
        return reactive.challenge(profile, frame, proposal, policy)

    def _revision_node(ctx):
        frame, proposal = _reactive_inputs(ctx)
        return reactive.revise(profile, frame, proposal, ctx.completed_outputs["reactive:challenge"], reactive_req)

    def _n1_challenge_node(ctx):
        _, proposal = _reactive_inputs(ctx)
        policy = ctx.completed_outputs["specialist:standards"]["signals"].get("reactive_policy") or {}
        return reactive.n1_challenge(profile, proposal, policy)

    def _n1_revision_node(ctx):
        _, proposal = _reactive_inputs(ctx)
        return reactive.n1_revise(profile, proposal, ctx.completed_outputs["n1:challenge"], n1_req)

    if reactive_req:
        dag.add_node(DAGNode(
            "reactive:challenge", "specialist", "规程专家质疑单投候选", _challenge_node,
            depends_on=["specialist:standards", "specialist:physics"],
        ))
        dag.add_node(DAGNode(
            "reactive:revise", "specialist", "物理专家同点复算中间稳态与终态", _revision_node,
            depends_on=["reactive:challenge"],
        ))
    elif n1_req:
        dag.add_node(DAGNode(
            "n1:challenge", "specialist", "规程专家质疑单一潮流电流口径", _n1_challenge_node,
            depends_on=["specialist:standards", "specialist:physics"],
        ))
        dag.add_node(DAGNode(
            "n1:revise", "specialist", "物理专家补算表计上限与比例修正双口径包络", _n1_revision_node,
            depends_on=["n1:challenge"],
        ))

    # Wave 3: SkepticAuditor Conflict Arbitration & Consensus Synthesis Node
    def _arbitrate_node(ctx: DAGRunContext) -> dict[str, Any]:
        briefs_by_role = {
            r_id: ctx.completed_outputs[f"specialist:{r_id}"]
            for r_id in selected
            if f"specialist:{r_id}" in ctx.completed_outputs
        }
        ordered_briefs = [briefs_by_role[r_id] for r_id in selected if r_id in briefs_by_role]
        conflicts = _detect_and_arbitrate_conflicts(briefs_by_role)
        decision = None
        arb_trace = []
        case_facts = []
        if reactive_req:
            frame, proposal = _reactive_inputs(ctx)
            objection = ctx.completed_outputs["reactive:challenge"]
            revision = ctx.completed_outputs["reactive:revise"]
            decision = reactive.arbitrate(profile, frame, proposal, objection, revision)
            env_trace = []
            if decision["payload"].get("selected"):
                env_trace = reactive.evaluate_multi_hour_envelope(
                    archive, profile, decision, objection["payload"]["policy"],
                )
            case_facts, arb_trace = reactive.arbitration_evidence(
                archive, [step for brief in ordered_briefs for step in brief["tool_trace"]] + env_trace, decision,
            )
            if case_facts is None:
                raise NodeRestricted("reactive_evidence_mismatch", "复算断面与当前日报证据版本不一致。")
            arb_trace = list(env_trace) + list(arb_trace)
            conflicts.extend(reactive.voltage_conflict_record(decision, objection))
        elif n1_req:
            _, proposal = _reactive_inputs(ctx)
            objection = ctx.completed_outputs["n1:challenge"]
            revision = ctx.completed_outputs["n1:revise"]
            decision = reactive.n1_arbitrate(profile, proposal, objection, revision)
            case_facts, arb_trace = reactive.n1_arbitration_evidence(
                archive, [step for brief in ordered_briefs for step in brief["tool_trace"]], decision,
            )
            if case_facts is None:
                case_facts = []
            conflicts.extend(reactive.n1_conflict_record(decision, objection))
        for conf in conflicts:
            emit("conflict_detected", conflict=conf)

        all_facts = list(case_facts)
        for b in ordered_briefs:
            for f in b["findings"][:6]:
                if f not in all_facts:
                    all_facts.append(f)

        missing_ev = list(dict.fromkeys(
            (briefs_by_role.get("standards", {}).get("signals", {}).get("missing_evidence") or [])
            + ([
                "1#/2# 主变铭牌容量/短路阻抗 Uk% 与分接头实际档位台账",
                "1009/1010/1020 下井电缆型号、长度与实测 CT 变比核校记录",
            ] if "physics" in briefs_by_role else [])
        ))[:5]

        cited_stds = (briefs_by_role.get("standards", {}).get("signals", {}).get("cited_standards") or [])[:3]
        if decision:
            missing_ev = list(dict.fromkeys(decision["payload"]["review_requirements"] + missing_ev))

        report = {
            "confirmed_facts": all_facts[:8],
            "missing_evidence": missing_ev or ["同期现场开关变位、保护 SOE 与设备铭牌台账"],
            "cited_standards": cited_stds,
        }

        if ctx.human_inputs.get("operator_note"):
            report["operator_note"] = str(ctx.human_inputs["operator_note"])[:1000]
        return {"conflicts": conflicts, "report": report, "decision": decision, "tool_trace": arb_trace}

    dag.add_node(
        DAGNode(
            node_id="arbitrate",
            kind="arbitrate",
            title="Skeptic 冲突仲裁与共识综合",
            handler=_arbitrate_node,
            depends_on=["reactive:revise"] if reactive_req else (["n1:revise"] if n1_req else specialist_node_ids),
        )
    )

    # Wave 4: Two-Level Cell + Numeric Groundedness Verification Node
    def _verify_node(ctx: DAGRunContext) -> dict[str, Any]:
        arb = ctx.completed_outputs["arbitrate"]
        merged_trace: list[dict[str, Any]] = []
        for r_id in selected:
            b = ctx.completed_outputs.get(f"specialist:{r_id}")
            if b:
                merged_trace.extend(b.get("tool_trace") or [])
        merged_trace.extend(arb.get("tool_trace") or [])
        report, ver = finalize_diagnosis(archive, arb["report"], merged_trace)
        emit(
            "auditor_verdict",
            status=ver["status"],
            verified_cells=len(ver["verified_cells"]),
            cited_cells_total=ver["cited_cells_total"],
            conflicts_count=len(arb["conflicts"]),
            llm_synthesis_used=False,
        )
        return {"report": report, "verification": ver}

    dag.add_node(
        DAGNode(
            node_id="verify",
            kind="verify",
            title="单元格与数值双层防幻觉校验",
            handler=_verify_node,
            depends_on=["arbitrate"],
        )
    )

    t_par_start = time.monotonic()
    runtime = UnifiedDAGRuntime(DAGCheckpointStore(checkpoint_db) if checkpoint_db else None)
    input_spec = {"mode": "multi_agent", "question": question, "evidence_policy": EVIDENCE_POLICY}
    if reactive_req:
        input_spec.update(reactive_protocol=reactive.PROTOCOL_VERSION, mine_profile=profile.model_dump(mode="json"))
    dag_state = runtime.execute(
        dag,
        thread_id=consult_id,
        mode="multi_agent",
        question=question,
        input_spec=input_spec,
        archive_snapshot=compute_snapshot(archive),
        pause_before_node=pause_before_node,
        pause_after_node=pause_after_node,
        human_input=human_input,
        resume_policy=resume_policy,
        persist=persist_dag,
        parallel=use_parallel,
        on_event=on_event,
    )
    par_wall_ms = round((time.monotonic() - t_par_start) * 1000.0, 2)

    dag_meta = {
        "engine": "UnifiedDAGRuntime",
        "thread_id": consult_id,
        "status": dag_state["status"],
        "suspended_at_node": dag_state.get("suspended_at_node"),
        "executed_nodes": dag_state.get("executed_nodes", []),
        "reused_nodes": dag_state.get("reused_nodes", []),
        "invalidated_nodes": dag_state.get("invalidated_nodes", []),
        "resume_policy": dag_state.get("resume_policy", resume_policy),
        "snapshot_drift": dag_state.get("snapshot_drift"),
        "review_mode": dag_state.get("review_mode"),
        "interrupt": dag_state.get("interrupt"),
        "topology": dag_state.get("dag_topology"),
    }

    if dag_state["status"] in ("restricted", "conflict"):
        sec = (dag_state.get("restricted_payload") or {}).get("security_block")
        refusal = blocked_diagnosis_report()
        refusal["summary"] = (
            sec["message"] if sec else (
                "该会诊线程正在被另一并发恢复请求执行，本次恢复已互斥拒绝。"
                if dag_state["status"] == "conflict" else
                "历史检查点与当前请求、资料或核验规则不一致，请发起新会诊。"
            )
        )
        ver = diagnostic_boundary_verdict("restricted")
        out_blk = {
            "kind": "multi_agent_consultation",
            "consultation_id": consult_id,
            "status": "conflict" if dag_state["status"] == "conflict" else "blocked",
            "stop_reason": dag_state["stop_reason"],
            "security_block": sec,
            "question": question,
            "mode": "local",
            "model": None,
            "elapsed_seconds": round(time.monotonic() - t0, 3),
            "plan": {"selected_specialists": [], "execution_mode": "blocked_by_firewall"},
            "specialist_briefs": [],
            "inter_agent_messages": [
                {"from": "SupervisorPlannerAgent", "to": "SecurityFirewall", "type": "block", "content": refusal["summary"]}
            ],
            "conflicts_resolved": [],
            "parallel_speedup": {"wall_ms": par_wall_ms, "sequential_sum_ms": 0.0, "speedup_ratio": 1.0},
            "report": refusal,
            "verification": ver,
            "tool_trace": [],
            "dag_execution": dag_meta,
        }
        outcome = classify_outcome(out_blk, execution_status="conflict" if dag_state["status"] == "conflict" else "completed")
        out_blk["outcome_layers"] = outcome
        out_blk["business_status"] = outcome["business_status"]
        out_blk["business_trusted"] = outcome["business_trusted"]
        return out_blk

    completed = dag_state.get("completed_nodes") or {}
    ordered_briefs = [
        completed[f"specialist:{r_id}"]["output"]
        for r_id in selected
        if f"specialist:{r_id}" in completed
    ]
    seq_sum_ms = round(sum(b["latency_ms"] for b in ordered_briefs), 2)
    speedup = round(seq_sum_ms / max(par_wall_ms, 0.1), 2)
    merged_trace: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = [
        {
            "from": "SupervisorPlannerAgent",
            "to": AGENT_ROLES[r_id]["name"],
            "type": "dispatch",
            "content": plan["subtasks"][r_id],
        }
        for r_id in selected
    ]
    for b in ordered_briefs:
        merged_trace.extend(b.get("tool_trace") or [])
        messages.append({
            "from": b["agent_name"],
            "to": "SkepticAuditorAgent",
            "type": "specialist_brief",
            "content": f'提交 {len(b["findings"])} 条结构化证据与 {len(b["evidence_cells"])} 处原表单元格（耗时 {b["latency_ms"]} ms）',
        })
        messages.extend(b.get("messages") or [])
    for node_id in ("reactive:challenge", "reactive:revise", "n1:challenge", "n1:revise"):
        if node_id in completed:
            messages.append(completed[node_id]["output"])
    public_briefs = [
        {k: v for k, v in b.items() if k not in ("tool_trace", "case_frame", "messages")} for b in ordered_briefs
    ]

    if dag_state["status"] == "suspended":
        out_s = {
            "kind": "multi_agent_consultation",
            "consultation_id": consult_id,
            "status": "suspended",
            "stop_reason": dag_state["stop_reason"],
            "question": question,
            "mode": "local",
            "model": None,
            "elapsed_seconds": round(time.monotonic() - t0, 3),
            "plan": {
                "selected_specialists": selected,
                "execution_mode": plan["execution_mode"],
                "subtasks": {k: plan["subtasks"][k] for k in selected},
            },
            "specialist_briefs": public_briefs,
            "inter_agent_messages": messages,
            "conflicts_resolved": [],
            "parallel_speedup": {"wall_ms": par_wall_ms, "sequential_sum_ms": seq_sum_ms, "speedup_ratio": speedup},
            "report": None,
            "verification": None,
            "tool_trace": merged_trace,
            "dag_execution": dag_meta,
        }
        outcome = classify_outcome(out_s, execution_status="suspended")
        out_s["outcome_layers"] = outcome
        out_s["business_status"] = outcome["business_status"]
        out_s["business_trusted"] = outcome["business_trusted"]
        return out_s

    arb = completed["arbitrate"]["output"]
    checked = completed["verify"]["output"]
    verification = checked["verification"]
    verified = verification["status"] == "verified"
    decision_message = arb.get("decision")
    decision = decision_message["payload"] if verified and decision_message else None
    if decision:
        messages.append(decision_message)
    merged_trace.extend(arb.get("tool_trace") or [])
    for conf in arb["conflicts"]:
        if conf["id"] == "CONFLICT-OVERVOLT-VS-REACTIVE":
            continue
        messages.append({
            "from": "SkepticAuditorAgent",
            "to": "Consensus",
            "type": "conflict_arbitration",
            "content": f'[{conf["id"]}] {conf["resolution"]}',
        })

    c1535_only = bool(hints.get("mentioned_c1535") and not hints.get("mentioned_s35"))
    out_done = {
        "kind": "multi_agent_consultation",
        "consultation_id": consult_id,
        "status": "insufficient_data" if (c1535_only and verified) else ("ok" if verified else "verification_warning"),
        "stop_reason": "requested_evidence_missing" if (c1535_only and verified) else ("consensus_verified" if verified else "verification_failed"),
        "question": question,
        "mode": "local",
        "model": None,
        "llm_synthesis_used": False,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
        "plan": {
            "selected_specialists": selected,
            "execution_mode": plan["execution_mode"],
            "subtasks": {k: plan["subtasks"][k] for k in selected},
        },
        "specialist_briefs": public_briefs,
        "inter_agent_messages": messages,
        "conflicts_resolved": arb["conflicts"],
        **({"reactive_arbitration": decision} if (decision and reactive_req) else {}),
        **({"n1_arbitration": decision} if (decision and n1_req) else {}),
        "parallel_speedup": {
            "wall_ms": par_wall_ms,
            "sequential_sum_ms": seq_sum_ms,
            "speedup_ratio": speedup,
        },
        "report": checked["report"],
        "verification": verification,
        "tool_trace": merged_trace,
        "llm_records": [],
        "dag_execution": dag_meta,
    }
    outcome = classify_outcome(out_done, execution_status="completed")
    out_done["outcome_layers"] = outcome
    out_done["business_status"] = outcome["business_status"]
    out_done["business_trusted"] = outcome["business_trusted"]
    return out_done


MULTI_AGENT_BENCHMARK_CASES: list[dict[str, Any]] = [
    {
        "id": "MA01",
        "title": "1010 低功率因数无功补偿 vs 10 kV II段过压冲突权衡",
        "question": "请对 1010 二回井下 1535 测算无功补偿容量与降损效果，并同时评估投切电容会不会加剧 10 kV II段过压，给出综合治理方案。",
        "expected_conflict": "CONFLICT-OVERVOLT-VS-REACTIVE",
    },
    {
        "id": "MA02",
        "title": "1009➔1010 N-1 转供：表计比例偏离 vs 三相潮流双口径仲裁",
        "question": "如果 1009 柜退出转供至 1010 柜，请结合 N-1 交流故障潮流与 1010 量测口径校核下井双回载流裕度与末端压降。",
        "expected_conflict": "CONFLICT-METERING-RATIO-VS-POWERFLOW",
    },
    {
        "id": "MA03",
        "title": "11-13 17:00 单进线带双变 + 2#主变分接头调压 + 铭牌边界审计",
        "question": "2026年11月13日17:00 306停运期间 10 kV II段过压达 10.90 kV，请用交流潮流评估 2#主变分接头调压档位并核查安规与铭牌边界。",
        "expected_conflict": "CONFLICT-ASSUMPTION-VS-ARCHIVE",
    },
    {
        "id": "MA04",
        "title": "11-12 12:00 2#主变全链反向有功跨专业证伪会诊",
        "question": "2026年11月12日12:00 2#主变306/312/1006全链反向有功，请核查 1027 瓦斯发电站I回证伪反送电假设并对照安规给出排查步骤。",
        "expected_conflict": "CONSENSUS-FALSIFIED-1027-EXPORT",
    },
]


def _coverage_score(tool_trace: list[dict], conflicts: list[dict]) -> dict[str, Any]:
    tools = {t.get("tool") for t in tool_trace}
    has_tel = bool(tools & {"get_station_health_overview", "get_cabinet_health_profile", "check_power_balance", "get_window_snapshot"})
    has_pf = bool(tools & {"run_ac_power_flow", "solve_reactive_study_state"})
    has_std = bool(tools & {"search_coal_standards", "retrieve_conditional_evidence"})
    has_conf = len(conflicts) > 0
    score = sum([has_tel, has_pf, has_std, has_conf])
    return {
        "telemetry": has_tel,
        "ac_powerflow": has_pf,
        "coal_standards": has_std,
        "conflict_arbitration": has_conf,
        "score_out_of_4": score,
    }


def evaluate_multi_agent_comparison(archive: Archive) -> dict[str, Any]:
    """Run Single-Agent vs 5-Role Multi-Agent thesis comparison across 4 complex cross-domain cases."""
    rows = []
    for case in MULTI_AGENT_BENCHMARK_CASES:
        s_res = diagnose_question(archive, case["question"])
        m_res = run_multi_agent_consultation(archive, case["question"])
        s_cov = _coverage_score(s_res["tool_trace"], [])
        m_cov = _coverage_score(m_res["tool_trace"], m_res["conflicts_resolved"])
        expected_hit = any(c["id"] == case["expected_conflict"] for c in m_res["conflicts_resolved"])
        rows.append({
            "id": case["id"],
            "title": case["title"],
            "expected_conflict": case["expected_conflict"],
            "single_agent": {
                "verified": s_res["verification"]["status"] == "verified",
                "tool_calls": s_res["tool_calls"],
                "elapsed_ms": round(s_res["elapsed_seconds"] * 1000.0, 1),
                "coverage": s_cov,
            },
            "multi_agent": {
                "verified": m_res["verification"]["status"] == "verified",
                "specialists_count": len(m_res["specialist_briefs"]),
                "tool_calls": len(m_res["tool_trace"]),
                "conflicts_count": len(m_res["conflicts_resolved"]),
                "expected_conflict_detected": expected_hit,
                "elapsed_ms": round(m_res["elapsed_seconds"] * 1000.0, 1),
                "speedup_ratio": m_res["parallel_speedup"]["speedup_ratio"],
                "coverage": m_cov,
            },
        })

    s_avg_cov = round(sum(r["single_agent"]["coverage"]["score_out_of_4"] for r in rows) / len(rows), 2)
    m_avg_cov = round(sum(r["multi_agent"]["coverage"]["score_out_of_4"] for r in rows) / len(rows), 2)
    return {
        "kind": "multi_agent_comparison",
        "cases_total": len(rows),
        "single_agent_avg_coverage_of_4": s_avg_cov,
        "multi_agent_avg_coverage_of_4": m_avg_cov,
        "coverage_gain": round(m_avg_cov - s_avg_cov, 2),
        "expected_conflicts_detected": sum(1 for r in rows if r["multi_agent"]["expected_conflict_detected"]),
        "all_verified": all(r["single_agent"]["verified"] and r["multi_agent"]["verified"] for r in rows),
        "cases": rows,
    }
