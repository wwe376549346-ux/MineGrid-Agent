"""Evidence exchange and arbitration for coupled voltage/reactive power studies."""

from __future__ import annotations

import math
import re

from laowadi.verifier import bind_tool_fact, build_observations


PROTOCOL_VERSION = 1
PF_TARGET = 0.90
REVIEW_REQUIREMENTS = [
    "主变铭牌、分接开关类型与同期实际档位",
    "同期母联、双回路开关状态与投切日志",
    "电缆参数、计量变比与无功流向核验",
    "末端电容器位置、可投容量分组、谐波与保护配合资料",
    "轻载和重载电压以及调档过程的安全复核",
    "电压判据原文、适用测点及统计窗口",
]


def reactive_request(question, hints, profile):
    if not re.search(r"补偿|电容|分时投切|24\s*小时.*(?:调压|投切|优化)|(?:提高|提升|改善)功率因数|低功率因数.*过压", question):
        return None
    if re.search(r"N-1|转供", question, re.I) and not re.search(r"补偿|电容|分时投切", question):
        return None
    if hints["mentioned_c1535"] and not hints["mentioned_s35"]:
        return None
    feeders = [
        f for f in profile.feeders if f.station == profile.primary_station and f.downstream_bus_id
    ]
    targets = [f for f in feeders if f.cabinet in hints["mentioned_s35"]]
    if len(targets) != 1:
        if hints["mentioned_s35"] or not feeders:
            return {"target": None}
        targets = [next((f for f in feeders if f.cabinet == "1010"), feeders[0])]
    capacity = re.search(r"(?<![\d.\-])(\d+(?:\.\d+)?)\s*kvar\b", question, re.I)
    time = re.search(r"(?<!\d)\d{1,2}:(\d{2})(?::(\d{2}))?(?!\d)", question)
    non_hourly = bool(time and (time[1] != "00" or time[2] not in (None, "00")))
    want_schedule = bool(re.search(r"分时|24\s*小时|几点投|几点切|投切计划|动作次数", question))
    return {
        "target": targets[0].cabinet, "requested_qc_kvar": float(capacity[1]) if capacity else None,
        "non_hourly": non_hourly, "want_daily_schedule": want_schedule,
    }


def _message(id_, sender, recipient, kind, content, payload, *parents):
    return {
        "id": id_, "from": sender, "to": recipient, "type": kind, "content": content,
        "in_reply_to": [parent["id"] for parent in parents], "payload": payload,
    }


def _valid(reading):
    return bool(reading and reading.get("cell") and isinstance(reading.get("value"), (int, float))
                and math.isfinite(reading["value"]))


def telemetry_case(archive, profile, request, hints, scoped_call):
    """Choose one hour using archived, concurrent load and voltage measurements."""
    target = request["target"]
    feeder = profile.feeder_map().get(target)
    transformer = next((tr for tr in profile.transformers if feeder and tr.lv_bus == feeder.bus_id), None)
    if not transformer or not feeder:
        return _telemetry_message({"status": "missing_target"}, "待确认有末端等值模型的单一送电回路。"), None

    source_bus = profile.bus_map()[feeder.bus_id]
    voltage_cabinet = source_bus.reference_cabinet
    load_cabs = {tr.lv_cabinet for tr in profile.transformers}
    load_cabs.update(f.cabinet for f in profile.feeders if f.downstream_bus_id)
    bus_cabs = {
        b.reference_cabinet for b in profile.buses
        if b.station == profile.primary_station and b.nominal_kv < 20 and b.reference_cabinet
    }
    incomers = set(profile.incomer_cabinets())
    required = {(cab, metric) for cab in incomers for metric in ("Uab", "P", "Ia")}
    required.update((cab, "P") for cab in load_cabs)
    required.update((cab, "Uab") for cab in bus_cabs)
    days = [hints["day"]] if hints["day"] else archive.days()
    if request.get("non_hourly") or (hints["hour"] is not None and not 0 <= hints["hour"] <= 23):
        return _telemetry_message({"status": "missing_measurements"}, "请求时刻超出日报整点范围。"), None
    frames = []
    for day in days:
        hour = hints["hour"]
        start, end = ("00:00:00", "23:00:00") if hour is None else (f"{hour:02d}:00:00",) * 2
        window = scoped_call("get_window_snapshot", {
            "start_time": f"{day} {start}", "end_time": f"{day} {end}",
            "cabinets": sorted(incomers | load_cabs | bus_cabs), "metrics": ["P", "cos", "Uab", "Ia"],
        })
        frames.extend(window.get("frames", []))
    eligible = []
    limit = profile.voltage_limit_for_kv(source_bus.nominal_kv)
    for frame in frames:
        snap = frame["readings"]
        entries = [(cab, metric, snap.get(cab, {}).get(metric)) for cab, metric in required]
        if any(not _valid(entry) for _, _, entry in entries):
            continue
        if any(entry["value"] <= 0 for _, metric, entry in entries if metric == "Uab"):
            continue
        incomplete_load = any(
            snap[cab]["P"]["value"] < 0
            or (snap[cab]["P"]["value"] > 0 and (
                not _valid(snap[cab].get("cos")) or not 0 < snap[cab]["cos"]["value"] <= 1
            ))
            for cab in load_cabs
        )
        if incomplete_load or snap[target]["P"]["value"] <= 0:
            continue
        cos = snap[target]["cos"]["value"]
        voltage = snap[voltage_cabinet]["Uab"]["value"]
        eligible.append((not (voltage > limit.hi_kv and cos < PF_TARGET), cos, -voltage, frame["time"], frame))
    if not eligible:
        return _telemetry_message(
            {"status": "missing_measurements", "cabinet": target},
            "指定范围内缺少有正向负荷且模型边界齐全的同期整点读数。",
        ), None

    frame = min(eligible, key=lambda row: row[:4])[-1]
    snap = frame["readings"]
    payload = {
        "status": "ok", "at_time": frame["time"], "cabinet": target,
        "supply_bus": feeder.bus_id, "receiving_bus": feeder.downstream_bus_id,
        "transformer": transformer.id, "voltage_cabinet": voltage_cabinet,
        "power": snap[target]["P"], "power_factor": snap[target]["cos"],
        "voltage": snap[voltage_cabinet]["Uab"],
        "selection": "requested_hour" if hints["day"] and hints["hour"] is not None else
        "prefer_concurrent_high_voltage_and_low_pf_then_lowest_pf_highest_voltage_earliest_hour",
    }
    return _telemetry_message(
        payload, f'{frame["time"]} · {target} 柜 cos {payload["power_factor"]["value"]:.2f}，'
        f'{voltage_cabinet} 柜 {payload["voltage"]["value"]:.2f} kV。',
    ), frame


def _telemetry_message(payload, content):
    return _message("reactive-measurement", "TelemetryMeteringAgent", "PhysicsPowerFlowAgent",
                    "measurement_claim", content, payload)


def standards_policy(profile, hits):
    reference = next((hit for hit in hits if hit["id"].startswith("GB/T 12325")), None)
    # This archive's reference card covers the 10 kV screening range, with formal applicability pending.
    limits = {
        b.id: [limit.lo_kv, limit.hi_kv]
        for b in profile.buses if b.nominal_kv < 20
        for limit in [profile.voltage_limit_for_kv(b.nominal_kv)]
    }
    return {
        "reference": {k: reference[k] for k in ("id", "pdf_page", "applicability")} if reference else None,
        "voltage_limits_kv": limits, "loading_limit_pct": 100.0, "pf_target": PF_TARGET,
        "review_requirements": REVIEW_REQUIREMENTS,
    }


def _state(profile, frame, case, tap, qc):
    from laowadi.powerflow import solve_reactive_study_state

    return solve_reactive_study_state(profile, frame["readings"], case["cabinet"], case["transformer"], tap, qc)


def propose(profile, measurement, frame, request):
    case = measurement["payload"]
    result = {"status": "ok", "case": case, "powerflow_states": []}
    if case["status"] != "ok":
        result["status"] = case["status"]
    elif case["power_factor"]["value"] >= PF_TARGET and request["requested_qc_kvar"] is None:
        result["status"] = "no_low_pf"
    else:
        transformer = next(tr for tr in profile.transformers if tr.id == case["transformer"])
        tap = transformer.calibrated_tap_pos
        p, cos = case["power"]["value"], case["power_factor"]["value"]
        requested = request["requested_qc_kvar"]
        # A nominal sizing seed. The solved feeder-head power factor is checked independently.
        qc = requested if requested is not None else round(
            p * max(0.0, math.tan(math.acos(cos)) - math.tan(math.acos(0.92))), 3
        )
        if not math.isfinite(qc) or qc <= 0:
            result["status"] = "invalid_capacity"
        else:
            try:
                base = _state(profile, frame, case, tap, 0.0)
                direct = _state(profile, frame, case, tap, qc)
            except ImportError:
                result["status"] = "solver_unavailable"
            else:
                result["powerflow_states"] = [base, direct]
                result["model_assumptions"] = profile.model_assumption_disclaimer
                result["status"] = "ok" if base["converged"] else "baseline_non_converged"
    message = _message(
        "reactive-proposal", "PhysicsPowerFlowAgent", "StandardsComplianceAgent", "proposal",
        "提交当前等值档位的基线与单投电容对照。" if result["powerflow_states"] else "当前无法提出单投电容的条件方案。",
        result, measurement,
    )
    return message


def _screen(state, baseline, frame, profile, policy, *, require_pf):
    if not state["converged"]:
        return {"violations": [{"code": "non_converged"}], "voltage_projection_kv": {}}
    violations = []
    projected = {}
    for bus_id, (lower, upper) in policy["voltage_limits_kv"].items():
        simulated = state["bus_kv"][bus_id]
        cabinet = profile.bus_map()[bus_id].reference_cabinet
        measurement = frame["readings"].get(cabinet, {}).get("Uab")
        if profile.bus_map()[bus_id].station != profile.primary_station:
            measurement = None
        if _valid(measurement):
            projected[bus_id] = measurement["value"] + simulated - baseline["bus_kv"][bus_id]
        for basis, value in (("modeled", simulated), ("measured_plus_model_delta", projected.get(bus_id))):
            if value is not None and not lower <= value <= upper:
                violations.append({"code": "voltage_outside_screen", "bus": bus_id, "basis": basis,
                                   "value_kv": value, "limit_kv": [lower, upper]})
    if state["max_loading_pct"] > policy["loading_limit_pct"]:
        violations.append({"code": "modeled_thermal_limit", "value_pct": state["max_loading_pct"]})
    if state["feeder_q_kvar"] < 0:
        violations.append({"code": "leading_feeder_q", "value_kvar": state["feeder_q_kvar"]})
    if require_pf and state["feeder_pf"] < policy["pf_target"]:
        violations.append({"code": "modeled_pf_below_target", "value": state["feeder_pf"]})
    return {"violations": violations, "voltage_projection_kv": projected}


def challenge(profile, frame, proposal, policy):
    result = proposal["payload"]
    payload = {"policy": policy, "status": result["status"], "requested_variants": []}
    content = "保留现场复核事项。"
    if not policy["reference"]:
        payload["status"] = "missing_voltage_reference"
        content = "缺少电压筛查依据，暂不筛选方案。"
    elif result["status"] == "ok":
        base, direct = result["powerflow_states"]
        baseline_screen = _screen(base, base, frame, profile, policy, require_pf=False)
        direct_screen = _screen(direct, base, frame, profile, policy, require_pf=True)
        supply_bus = result["case"]["supply_bus"]
        rise = direct["bus_kv"][supply_bus] - base["bus_kv"][supply_bus] if direct["converged"] else None
        voltage_conflict = bool(rise is not None and rise > 0 and any(
            v["code"] == "voltage_outside_screen" and v["bus"] == supply_bus
            and v["value_kv"] > v["limit_kv"][1]
            for v in direct_screen["violations"]
        ))
        payload.update(
            status="challenged" if direct_screen["violations"] else "direct_passes_screen",
            baseline_screen=baseline_screen, direct_screen=direct_screen,
            supply_bus_rise_kv=rise, voltage_conflict=voltage_conflict,
        )
        if direct_screen["violations"]:
            payload["requested_variants"] = ["tap_only", "tap_then_cap"]
            content = "单投候选触发筛查约束，要求对照仅调压与先调压后补偿，并检查中间稳态。"
        else:
            content = "该整点的单投候选通过模型筛查，提交仲裁复核。"
    return _message("reactive-challenge", "StandardsComplianceAgent", "PhysicsPowerFlowAgent",
                    "challenge" if payload["requested_variants"] else "constraint_review",
                    content, payload, proposal)


def revise(profile, frame, proposal, objection, request):
    result, review = proposal["payload"], objection["payload"]
    candidates = []
    if review["requested_variants"]:
        base, direct = result["powerflow_states"]
        case = result["case"]
        tr = next(tr for tr in profile.transformers if tr.id == case["transformer"])
        qc = direct["qc_kvar"]
        sizes = ([qc] if request["requested_qc_kvar"] is not None else
                 sorted({round(qc * scale, 3) for scale in (0.25, 0.5, 0.75, 1.0, 1.25, 1.5)} - {0.0}))
        supply = case["supply_bus"]
        for tap in range(tr.tap_min, tr.tap_max + 1):
            tap_only = base if tap == base["tap"] else _state(profile, frame, case, tap, 0.0)
            joint = []
            # An overvoltage objection requires a tap that physically lowers that bus.
            can_test = tap_only["converged"] and (
                not review["voltage_conflict"] or tap_only["bus_kv"][supply] < base["bus_kv"][supply]
            )
            if can_test:
                for size in sizes:
                    joint.append(
                        direct if tap == base["tap"] and size == qc else _state(profile, frame, case, tap, size)
                    )
            candidates.append({"tap_only": tap_only, "after_cap": joint})
    payload = {"trials": candidates, "at_time": result["case"].get("at_time")}
    return _message(
        "reactive-revision", "PhysicsPowerFlowAgent", "SkepticAuditorAgent", "counterproposal",
        f'按质疑复算 {sum(len(t["after_cap"]) for t in candidates)} 个投容终态及对应调档稳态。'
        if candidates else "无需追加调档与投容组合求解。",
        payload, proposal, objection,
    )


def arbitrate(profile, frame, proposal, objection, revision):
    result, review = proposal["payload"], objection["payload"]
    decision = {"status": review["status"], "case": result["case"], "selected": None,
                "field_status": "pending_evidence", "review_requirements": REVIEW_REQUIREMENTS}
    if review["status"] in ("challenged", "direct_passes_screen"):
        base, direct = result["powerflow_states"]
        policy = review["policy"]
        direct_screen = review["direct_screen"]
        candidates = [{"variant": "cap_only", "states": [direct], "screens": [direct_screen]}]
        for trial in revision["payload"]["trials"]:
            tap = trial["tap_only"]
            if tap["tap"] != base["tap"]:
                intermediate = _screen(tap, base, frame, profile, policy, require_pf=False)
                candidates.append({
                    "variant": "tap_only", "states": [tap],
                    "screens": [_screen(tap, base, frame, profile, policy, require_pf=True)],
                })
            for final in trial["after_cap"]:
                if final == direct:
                    continue
                stage = _screen(final, base, frame, profile, policy, require_pf=True)
                if tap["tap"] == base["tap"]:
                    candidates.append({"variant": "cap_only", "states": [final], "screens": [stage]})
                else:
                    candidates.append({
                        "variant": "tap_then_cap", "states": [tap, final], "screens": [intermediate, stage],
                    })
        eligible = [c for c in candidates if not any(s["violations"] for s in c["screens"])]
        # Prefer fewer changes, then less tap travel and smaller nominal capacitor rating.
        def priority(candidate):
            final = candidate["states"][-1]
            return (len(candidate["states"]), abs(final["tap"] - base["tap"]), final["qc_kvar"], final["loss_kw"])

        best = min(eligible, key=priority, default=None)
        decision.update(
            status="conditional_candidate" if best else "no_feasible_candidate", selected=best,
            baseline=base, direct=direct, candidates=candidates, voltage_conflict=review["voltage_conflict"],
            model_assumptions=result["model_assumptions"], screening_reference=policy["reference"],
            ranking=["number_of_stages", "tap_travel", "nominal_kvar", "loss_kw"],
        )
    return _message("reactive-decision", "SkepticAuditorAgent", "Consensus",
                    "arbitration", _decision_statement(decision), decision, objection, revision)


def _decision_statement(decision):
    case = decision["case"]
    selected = decision["selected"]
    if selected:
        final = selected["states"][-1]
        action = {
            "cap_only": f"维持模型档位，末端补偿额定 {final['qc_kvar']:g} kvar",
            "tap_only": f"仅将 {case['transformer']} 调至等值 {final['tap']:+d} 档",
            "tap_then_cap": f"{case['transformer']} 先到等值 {final['tap']:+d} 档，末端再补偿额定 {final['qc_kvar']:g} kvar",
        }[selected["variant"]]
        return (f'{case["at_time"]} 等值模型候选为{action}。'
                f'终态送电母线 {final["bus_kv"][case["supply_bus"]]:.4f} kV，'
                f'末端 {final["bus_kv"][case["receiving_bus"]]:.4f} kV，'
                f'馈线首端 cos {final["feeder_pf"]:.4f}。现场条件待补证。')
    if decision["status"] == "no_feasible_candidate":
        return "本整点已试候选均未通过模型筛查，待补证复算。"
    if decision["status"] == "no_low_pf":
        return "该整点功率因数达到项目筛查目标，未提出补偿候选。"
    return "本轮缺少有效求解或适用性证据，暂无法形成条件候选。"


def voltage_conflict_record(decision, objection):
    review = objection["payload"]
    if not review.get("voltage_conflict"):
        return []
    payload = decision["payload"]
    case, direct = payload["case"], payload["direct"]
    return [{
        "id": "CONFLICT-OVERVOLT-VS-REACTIVE",
        "severity": "high",
        "agents_involved": ["TelemetryMeteringAgent", "PhysicsPowerFlowAgent", "StandardsComplianceAgent"],
        "tension": (
            f'{case["at_time"]}，{case["voltage_cabinet"]} 柜实测 {case["voltage"]["value"]:.2f} kV，'
            f'{case["cabinet"]} 柜实测 cos {case["power_factor"]["value"]:.2f}。'
            f'单投额定 {direct["qc_kvar"]:g} kvar 将馈线首端模型 cos 从 {payload["baseline"]["feeder_pf"]:.4f}'
            f' 变为 {direct["feeder_pf"]:.4f}，同时抬升送电母线 {review["supply_bus_rise_kv"]:.4f} kV，'
            f'触发电压筛查。'
        ),
        "resolution": decision["content"],
        "decision_message_id": decision["id"],
    }]


def arbitration_evidence(archive, trace, decision):
    payload = decision["payload"]
    case = payload["case"]
    if case["status"] != "ok":
        return [], []
    refs = [case[key]["cell"] for key in ("power_factor", "voltage", "power")]
    facts = build_observations(archive, refs, trace)
    if not facts or [fact["source"]["value"] for fact in facts] != [
        case[key]["value"] for key in ("power_factor", "voltage", "power")
    ] or any(fact["source"]["time"] != case["at_time"] for fact in facts):
        return None, []
    if payload["status"] in ("conditional_candidate", "no_feasible_candidate"):
        step = {
            "round": 3, "agent": "auditor", "tool": "screen_reactive_study",
            "arguments": {"at_time": case["at_time"], "target_cabinet": case["cabinet"]},
            "result": {"status": "ok", "decision": payload},
        }
        facts.append(bind_tool_fact(
            {"statement": decision["content"], "evidence": refs}, step, "decision", kind="simulation",
        ))
        return facts, [step]
    return facts, []


def evaluate_multi_hour_envelope(archive, profile, decision, policy):
    """Cross-hour robustness check of a selected tap/capacitor scheme across valley, peak, and switching hours."""
    from laowadi.diag_tools import DiagnosticToolbox

    payload = decision["payload"]
    selected = payload.get("selected")
    case = payload.get("case") or {}
    if not selected or case.get("status") != "ok" or not case.get("at_time"):
        return []

    day = case["at_time"][:10]
    target = case["cabinet"]
    voltage_cab = case["voltage_cabinet"]
    supply_bus = case["supply_bus"]
    final_state = selected["states"][-1]
    sel_tap = final_state["tap"]
    sel_qc = final_state["qc_kvar"]

    load_cabs = {tr.lv_cabinet for tr in profile.transformers}
    load_cabs.update(f.cabinet for f in profile.feeders if f.downstream_bus_id)
    bus_cabs = {
        b.reference_cabinet for b in profile.buses
        if b.station == profile.primary_station and b.nominal_kv < 20 and b.reference_cabinet
    }
    incomers = set(profile.incomer_cabinets())
    req_cabs = sorted(incomers | load_cabs | bus_cabs)
    required = {(cab, metric) for cab in incomers for metric in ("Uab", "P", "Ia")}
    required.update((cab, "P") for cab in load_cabs)
    required.update((cab, "Uab") for cab in bus_cabs)

    tb = DiagnosticToolbox(archive)
    args = {
        "start_time": f"{day} 00:00:00",
        "end_time": f"{day} 23:00:00",
        "cabinets": req_cabs,
        "metrics": ["P", "cos", "Uab", "Ia"],
    }
    win = tb.execute("get_window_snapshot", args)
    frames = win.get("frames") or []

    valid_frames = []
    for fr in frames:
        snap = fr.get("readings") or {}
        if any(not _valid((snap.get(cab) or {}).get(m)) for cab, m in required):
            continue
        if any(snap[cab]["Uab"]["value"] <= 0 for cab, m in required if m == "Uab"):
            continue
        bad_load = any(
            snap[cab]["P"]["value"] < 0
            or (snap[cab]["P"]["value"] > 0 and (
                not _valid(snap[cab].get("cos")) or not 0 < snap[cab]["cos"]["value"] <= 1
            ))
            for cab in load_cabs
        )
        if bad_load or snap[target]["P"]["value"] <= 0:
            continue
        valid_frames.append(fr)

    if not valid_frames:
        return []

    picked: list[tuple[str, dict]] = []
    seen_times: set[str] = set()

    def _add(label: str, fr: dict | None):
        if fr is None or fr["time"] in seen_times:
            return
        seen_times.add(fr["time"])
        picked.append((label, fr))

    valley = min(valid_frames, key=lambda fr: (fr["readings"][target]["P"]["value"], fr["time"]))
    peak_v = max(valid_frames, key=lambda fr: (fr["readings"][voltage_cab]["Uab"]["value"], fr["time"]))
    peak_p = max(valid_frames, key=lambda fr: (fr["readings"][target]["P"]["value"], fr["time"]))
    # Detect dual-incomer recovery hour (e.g. 306 returns from 0 to >0)
    recovery = None
    for idx in range(1, len(valid_frames)):
        prev_306 = (valid_frames[idx - 1]["readings"].get("306") or {}).get("P", {}).get("value")
        cur_306 = (valid_frames[idx]["readings"].get("306") or {}).get("P", {}).get("value")
        if prev_306 is not None and cur_306 is not None and prev_306 == 0 and cur_306 > 0:
            recovery = valid_frames[idx]
            break
    if recovery is None:
        # Also check pre-outage dual-incomer boundary (e.g. 06:00 before 07:00 outage)
        for idx in range(len(valid_frames) - 1):
            cur_306 = (valid_frames[idx]["readings"].get("306") or {}).get("P", {}).get("value")
            nxt_306 = (valid_frames[idx + 1]["readings"].get("306") or {}).get("P", {}).get("value")
            if cur_306 is not None and nxt_306 is not None and cur_306 > 0 and nxt_306 == 0:
                recovery = valid_frames[idx]
                break

    _add("轻载谷值断面", valley)
    _add("母线最高压断面", peak_v)
    _add("重载峰值断面", peak_p)
    _add("双进线方式切换断面", recovery)
    picked.sort(key=lambda item: item[1]["time"])

    transformer = next(tr for tr in profile.transformers if tr.id == case["transformer"])
    base_tap = transformer.calibrated_tap_pos

    envelope_rows = []
    for label, fr in picked:
        base_h = _state(profile, fr, case, base_tap, 0.0)
        cand_h = _state(profile, fr, case, sel_tap, sel_qc)
        screen_h = _screen(cand_h, base_h, fr, profile, policy, require_pf=False)
        snap = fr["readings"]
        cells = [
            snap[target]["P"]["cell"],
            snap[target]["cos"]["cell"],
            snap[voltage_cab]["Uab"]["cell"],
        ]
        violations = screen_h["violations"]
        envelope_rows.append({
            "hour": fr["time"],
            "label": label,
            "is_study_hour": fr["time"] == case["at_time"],
            "target_p_kw": round(float(snap[target]["P"]["value"]), 2),
            "measured_uab_kv": round(float(snap[voltage_cab]["Uab"]["value"]), 2),
            "measured_cos": round(float(snap[target]["cos"]["value"]), 2),
            "bus_kv": round(float(cand_h["bus_kv"][supply_bus]), 4) if cand_h.get("converged") else None,
            "projected_bus_kv": (
                round(float(screen_h["voltage_projection_kv"][supply_bus]), 4)
                if supply_bus in screen_h["voltage_projection_kv"] else None
            ),
            "feeder_pf": round(float(cand_h["feeder_pf"]), 4) if cand_h.get("converged") else None,
            "feeder_q_kvar": round(float(cand_h["feeder_q_kvar"]), 2) if cand_h.get("converged") else None,
            "pass": len(violations) == 0,
            "violations": [v["code"] for v in violations],
            "cells": cells,
        })

    failed = [row for row in envelope_rows if not row["pass"]]
    if failed:
        reasons = sorted({code for row in failed for code in row["violations"]})
        reason_zh = "、".join({
            "leading_feeder_q": "轻载无功倒送（过补）",
            "voltage_outside_screen": "母线电压越出筛查限值",
            "modeled_thermal_limit": "模型载流超限",
            "non_converged": "潮流未收敛",
        }.get(code, code) for code in reasons)
        hours_str = "、".join(f'{row["hour"][11:16]}（{row["label"]}）' for row in failed)
        switching_mode = "time_of_use_switching_required"
        verdict_text = (
            f'跨时段包络校核（同日 {len(envelope_rows)} 个代表断面）：固定维持该档位与投容在 {hours_str} '
            f'触发 {reason_zh}，不可全天固定投入，须随负荷/运行方式分时投切。'
        )
    else:
        switching_mode = "fixed_daily_safe"
        verdict_text = (
            f'跨时段包络校核（同日 {len(envelope_rows)} 个代表断面）：轻载谷值、最高压、重载峰值及方式切换断面均通过电压与无功防倒送筛查，'
            f'模型层支持当日固定档位/投容运行（现场实际投切仍须待铭牌与方式复核）。'
        )

    payload["multi_hour_envelope"] = envelope_rows
    payload["envelope_verdict"] = {
        "day": day,
        "checked_hours": len(envelope_rows),
        "passed_hours": len(envelope_rows) - len(failed),
        "failed_hours": len(failed),
        "switching_mode": switching_mode,
        "summary": verdict_text,
    }
    decision["content"] = decision["content"].rstrip("。") + "。" + verdict_text

    # When fixed daily operation triggers violations (or user explicitly asks for 24h switching),
    # solve the exact 24-hour discrete tap & capacitor schedule with equipment switching limits.
    if switching_mode == "time_of_use_switching_required" or payload.get("want_daily_schedule"):
        from laowadi.powerflow import optimize_daily_tap_cap_schedule

        schedule = optimize_daily_tap_cap_schedule(
            archive,
            day,
            target_cabinet=target,
            pf_target=float(policy.get("pf_target", PF_TARGET)),
            profile=profile,
        )
        if schedule.get("status") == "ok":
            payload["daily_schedule"] = schedule
            decision["content"] = decision["content"].rstrip("。") + "。" + schedule["summary"]

    return [{"round": 3, "agent": "physics", "tool": "get_window_snapshot", "arguments": args, "result": win}]


DEFAULT_N1_SAFE_LIMIT_A = 210.0
DEFAULT_N1_THERMAL_LIMIT_A = 300.0


N1_REVIEW_REQUIREMENTS = [
    "1009 与 1010 下井电缆铭牌型号、截面（mm²）、长度及井筒敷设降容系数",
    "1010 柜电流互感器（CT）变比、两表法/三表法接线与有功计量比例系数核校记录",
    "井下 1535 中央变电所 1200 母联/联络开关热备用状态与闭锁逻辑操作票",
    "主变实测短路阻抗 Uk% 与同期分接头档位台账",
]


def n1_transfer_request(question, hints, profile):
    """Detect underground dual-circuit N-1 transfer study requests (e.g. 1009 -> 1010) and counterfactual limits."""
    if not re.search(r"N-1|n-1|转供|(?:1009.*退出|1009.*停运)", question):
        return None
    if re.search(r"补偿|电容", question):
        return None
    if any(k in question for k in ("303", "306", "单进线")) and not any(k in question for k in ("1009", "1010", "下井")):
        return None
    safe_m = re.search(
        r"(?:长期允许(?:安全)?载流(?:量)?|安全载流(?:量|限值|线)?|载流量(?:只有|为|按|限定)?|额定(?:电流|载流)?|电流限值)[^\d]{0,12}(\d{2,4}(?:\.\d+)?)\s*A\b",
        question,
        re.I,
    )
    thermal_m = re.search(
        r"(?:热稳定(?:限值|电流|上限)?|最大允许(?:电流|载流))[^\d]{0,12}(\d{2,4}(?:\.\d+)?)\s*A\b",
        question,
        re.I,
    )
    derate_m = re.search(r"降容(?:系数)?[^\d]{0,8}(0\.\d+|1(?:\.\d+)?)", question)
    time = re.search(r"(?<!\d)\d{1,2}:(\d{2})(?::(\d{2}))?(?!\d)", question)
    non_hourly = bool(time and (time[1] != "00" or time[2] not in (None, "00")))
    rated_a = float(safe_m.group(1)) if safe_m else None
    thermal_a = float(thermal_m.group(1)) if thermal_m else None
    derate = float(derate_m.group(1)) if derate_m else None
    return {
        "outage_cabinet": "1009",
        "receiving_cabinet": "1010",
        "outage_mode": "feeder_1009",
        "rated_current_a": rated_a,
        "thermal_limit_a": thermal_a,
        "derating_factor": derate,
        "counterfactual_limit_applied": bool(rated_a is not None or thermal_a is not None or derate is not None),
        "non_hourly": non_hourly,
    }


def n1_telemetry_case(archive, profile, request, hints, scoped_call):
    """Locate the worst concurrent dual-circuit current hour and inspect metering scale offset."""
    out_cab = request["outage_cabinet"]
    rec_cab = request["receiving_cabinet"]
    if request.get("non_hourly") or (hints["hour"] is not None and not 0 <= hints["hour"] <= 23):
        return _message(
            "n1-measurement", "TelemetryMeteringAgent", "PhysicsPowerFlowAgent",
            "measurement_claim", "请求时刻超出日报整点范围。", {"status": "missing_measurements"},
        ), None

    prof_rec = scoped_call("get_cabinet_health_profile", {"cabinet": rec_cab})
    m_chk = (prof_rec.get("metering_check") or {}) if prof_rec.get("status") == "ok" else {}
    ratio_median = m_chk.get("ratio_median")
    scale_status = m_chk.get("status")

    days = [hints["day"]] if hints["day"] else archive.days()
    frames = []
    for day in days:
        hour = hints["hour"]
        start, end = ("00:00:00", "23:00:00") if hour is None else (f"{hour:02d}:00:00",) * 2
        win = scoped_call("get_window_snapshot", {
            "start_time": f"{day} {start}", "end_time": f"{day} {end}",
            "cabinets": ["303", "306", "1005", "1006", out_cab, rec_cab],
            "metrics": ["Ia", "P", "Uab", "cos"],
        })
        frames.extend(win.get("frames", []))

    candidates = []
    for fr in frames:
        rd = fr.get("readings") or {}
        ia_out = (rd.get(out_cab) or {}).get("Ia")
        ia_rec = (rd.get(rec_cab) or {}).get("Ia")
        p_out = (rd.get(out_cab) or {}).get("P")
        p_rec = (rd.get(rec_cab) or {}).get("P")
        u_rec = (rd.get("1006") or {}).get("Uab")
        if all(_valid(x) for x in (ia_out, ia_rec, p_out, p_rec, u_rec)):
            comb_ia = round(float(ia_out["value"]) + float(ia_rec["value"]), 2)
            comb_p = round(float(p_out["value"]) + float(p_rec["value"]), 2)
            candidates.append((comb_ia, fr["time"], fr, comb_p))

    if not candidates:
        return _message(
            "n1-measurement", "TelemetryMeteringAgent", "PhysicsPowerFlowAgent",
            "measurement_claim", "指定范围内缺少双回同期完整电流与有功读数。",
            {"status": "missing_measurements"},
        ), None

    comb_ia, best_time, best_frame, comb_p = max(candidates, key=lambda row: (row[0], row[1]))
    rd = best_frame["readings"]
    # Pick valley & median/second-peak representative hours for multi-hour N-1 envelope check
    sorted_by_ia = sorted(candidates, key=lambda row: (row[0], row[1]))
    rep_samples = []
    seen_rep = set()
    for rep_label, row in (
        ("轻载谷值断面", sorted_by_ia[0]),
        ("中位负荷断面", sorted_by_ia[len(sorted_by_ia) // 2]),
        ("重载峰值断面", sorted_by_ia[-1]),
    ):
        if row[1] in seen_rep:
            continue
        seen_rep.add(row[1])
        r_rd = row[2]["readings"]
        rep_samples.append({
            "hour": row[1],
            "label": rep_label,
            "combined_metered_ia_a": row[0],
            "combined_metered_p_kw": row[3],
            "bus_voltage_kv": round(float(r_rd["1006"]["Uab"]["value"]), 2),
            "cells": [
                r_rd[out_cab]["Ia"]["cell"],
                r_rd[rec_cab]["Ia"]["cell"],
                r_rd["1006"]["Uab"]["cell"],
            ],
        })
    rep_samples.sort(key=lambda item: item["hour"])

    payload = {
        "status": "ok",
        "at_time": best_time,
        "outage_cabinet": out_cab,
        "receiving_cabinet": rec_cab,
        "ia_outage": rd[out_cab]["Ia"],
        "ia_receiving": rd[rec_cab]["Ia"],
        "p_outage": rd[out_cab]["P"],
        "p_receiving": rd[rec_cab]["P"],
        "bus_voltage": rd["1006"]["Uab"],
        "combined_metered_ia_a": comb_ia,
        "combined_metered_p_kw": comb_p,
        "metering_scale_status": scale_status,
        "metering_ratio_median": ratio_median,
        "peak_1010": {"time": best_time, "ia_a": rd[rec_cab]["Ia"]["value"], "cell": rd[rec_cab]["Ia"]["cell"]},
        "peak_1009": {"time": best_time, "ia_a": rd[out_cab]["Ia"]["value"], "cell": rd[out_cab]["Ia"]["cell"]},
        "worst_mismatch": {"time": best_time, "ratio": ratio_median, "p_cell": rd[rec_cab]["P"]["cell"]},
        "representative_hours": rep_samples,
    }
    content = (
        f'{best_time} · {out_cab}+{rec_cab} 双回同期表计电流代数和峰值 {comb_ia:.2f} A'
        f'（{out_cab}={rd[out_cab]["Ia"]["value"]:.2f} A，{rec_cab}={rd[rec_cab]["Ia"]["value"]:.2f} A），'
        f'有功合计 {comb_p:.2f} kW；{rec_cab} 柜量测比例系数中位数={ratio_median}（{scale_status}）。'
    )
    return _message(
        "n1-measurement", "TelemetryMeteringAgent", "PhysicsPowerFlowAgent",
        "measurement_claim", content, payload,
    ), best_frame


def n1_propose(archive, profile, measurement, frame, request):
    """PhysicsPowerFlowAgent runs single-model balanced 3-phase pandapower N-1 contingency."""
    from laowadi.powerflow import run_ac_power_flow

    case = measurement["payload"]
    if case.get("status") != "ok":
        return _message(
            "n1-proposal", "PhysicsPowerFlowAgent", "StandardsComplianceAgent",
            "proposal", "缺少有效同期量测断面，无法执行 N-1 交流潮流提案。",
            {"status": case.get("status", "error"), "case": case}, measurement,
        )
    pf = run_ac_power_flow(
        archive,
        scenario="n1_contingency",
        outage_mode=request["outage_mode"],
        at_time=case["at_time"],
        profile=profile,
    )
    n1_block = pf.get("n1_contingency") or {}
    rec_comp = n1_block.get("receiving_1010_comparison") or {}
    sim_after_i = rec_comp.get("after_i_a")
    min_ug_kv = rec_comp.get("min_underground_kv")
    payload = {
        "status": "ok" if pf.get("status") == "ok" and n1_block.get("converged") else "non_converged",
        "case": case,
        "powerflow_n1": n1_block,
        "sim_receiving_i_a": sim_after_i,
        "min_underground_kv": min_ug_kv,
        "total_loss_kw": (pf.get("network_summary") or {}).get("total_loss_kw"),
        "model_assumptions": pf.get("model_assumptions"),
    }
    content = (
        f'{case["at_time"]} · pandapower N-1 交流潮流（{request["outage_cabinet"]} 退运由 {request["receiving_cabinet"]} 单回转供）收敛：'
        f'受电回路平衡三相仿真电流 {sim_after_i} A，井下最低仿真电压 {min_ug_kv} kV。'
        if payload["status"] == "ok" else "N-1 交流潮流未收敛。"
    )
    return _message(
        "n1-proposal", "PhysicsPowerFlowAgent", "StandardsComplianceAgent",
        "proposal", content, payload, measurement,
    )


def n1_challenge(profile, proposal, policy):
    """StandardsComplianceAgent challenges single-model N-1 current when metering ratio offset exists."""
    prop = proposal["payload"]
    case = prop.get("case") or {}
    if prop.get("status") != "ok":
        return _message(
            "n1-challenge", "StandardsComplianceAgent", "PhysicsPowerFlowAgent",
            "constraint_review", "N-1 潮流提案未收敛或缺少量测，保留缺证清单。",
            {"status": prop.get("status"), "challenged": False}, proposal,
        )
    metered_ia = float(case["combined_metered_ia_a"])
    sim_ia = float(prop["sim_receiving_i_a"] or 0.0)
    ratio = case.get("metering_ratio_median")
    gap_a = round(metered_ia - sim_ia, 2)
    challenged = bool(case.get("metering_scale_status") == "systematic_scale_offset" or abs(gap_a) >= 15.0)
    payload = {
        "status": "challenged" if challenged else "passes_single_basis",
        "challenged": challenged,
        "metered_algebraic_peak_a": metered_ia,
        "powerflow_balanced_i_a": sim_ia,
        "discrepancy_a": gap_a,
        "metering_ratio_median": ratio,
        "requested_revisions": ["dual_basis_thermal_envelope", "corrected_ct_power_projection"] if challenged else [],
        "policy": policy,
    }
    content = (
        f'合规质疑：{case["receiving_cabinet"]} 柜存在系统性量测比例偏离（P/(√3·U·I·cos) 中位数={ratio}），'
        f'导致按有功驱动的三相潮流仿真电流（{sim_ia:.2f} A）显著低于表计实测电流代数和上限（{metered_ia:.2f} A，相差 {gap_a:+.2f} A）。'
        f'依据《煤矿安全规程》双回路全负荷转供安全红线，禁止仅凭偏低的潮流电流判定不越限，要求补算「表计电流保守包络」与「按比例修正有功后的折算潮流」双口径对比！'
    )
    return _message(
        "n1-challenge", "StandardsComplianceAgent", "PhysicsPowerFlowAgent",
        "challenge" if challenged else "constraint_review", content, payload, proposal,
    )


def n1_revise(profile, proposal, objection, request):
    """PhysicsPowerFlowAgent computes the dual-basis thermal & voltage envelope in response to the challenge."""
    prop = proposal["payload"]
    case = prop.get("case") or {}
    metered_ia = float(case.get("combined_metered_ia_a") or 0.0)
    sim_ia = float(prop.get("sim_receiving_i_a") or 0.0)
    ratio = float(case.get("metering_ratio_median") or 0.73)
    u_kv = float((case.get("bus_voltage") or {}).get("value") or 10.6)
    min_ug_kv = prop.get("min_underground_kv")
    v_drop_pct = (
        round((u_kv - float(min_ug_kv)) / max(u_kv, 0.1) * 100.0, 2)
        if min_ug_kv is not None and u_kv > 0 else None
    )

    s_powerflow_kva = round(1.7320508 * u_kv * sim_ia, 2)
    s_metered_upper_kva = round(1.7320508 * u_kv * metered_ia, 2)
    scale_corrected_i_a = round(sim_ia / max(ratio, 0.5), 2) if ratio else metered_ia

    rated_a = request.get("rated_current_a")
    derate = request.get("derating_factor") or 1.0
    eff_limit_a = round(rated_a * derate, 2) if rated_a is not None else None
    safe_limit_a = eff_limit_a if eff_limit_a is not None else DEFAULT_N1_SAFE_LIMIT_A
    thermal_limit_a = (
        round(float(request["thermal_limit_a"]) * derate, 2)
        if request.get("thermal_limit_a") is not None
        else max(DEFAULT_N1_THERMAL_LIMIT_A, safe_limit_a)
    )
    counterfactual_applied = bool(request.get("counterfactual_limit_applied"))

    def _build_env(basis: str, label: str, current_a: float, s_kva: float, rec_kv, vd_pct):
        load_safe = round(current_a / max(safe_limit_a, 0.1) * 100.0, 2)
        load_max = round(current_a / max(thermal_limit_a, 0.1) * 100.0, 2)
        return {
            "basis": basis,
            "label": label,
            "current_a": current_a,
            "apparent_power_kva": s_kva,
            "min_underground_kv": rec_kv,
            "receiving_kv": rec_kv,
            "v_drop_pct": vd_pct,
            "loading_safe_pct": load_safe,
            "loading_max_pct": load_max,
            "safe_pass": current_a <= safe_limit_a,
            "thermal_pass": current_a <= thermal_limit_a,
            "load_ratio_pct": round(current_a / eff_limit_a * 100.0, 2) if eff_limit_a else None,
        }

    envelopes = [
        _build_env(
            "pandapower_active_power_driven",
            "口径 A：有功驱动平衡三相交流潮流",
            sim_ia, s_powerflow_kva, min_ug_kv, v_drop_pct,
        ),
        _build_env(
            "metered_current_conservative_upper",
            "口径 B：表计实测电流代数和保守上限",
            metered_ia, s_metered_upper_kva, min_ug_kv, v_drop_pct,
        ),
        _build_env(
            "scale_offset_corrected_projection",
            f"口径 C：计及 {case.get('receiving_cabinet')} 比例系数({ratio})修正折算",
            scale_corrected_i_a, round(1.7320508 * u_kv * scale_corrected_i_a, 2), min_ug_kv, v_drop_pct,
        ),
    ]
    binding_env = max(envelopes, key=lambda item: item["current_a"])
    multi_hour_rows = []
    for rep in case.get("representative_hours") or []:
        rep_ia = float(rep["combined_metered_ia_a"])
        multi_hour_rows.append({
            "hour": rep["hour"],
            "label": rep["label"],
            "bus_kv": rep["bus_voltage_kv"],
            "feeder_pf": None,
            "combined_metered_ia_a": rep_ia,
            "loading_safe_pct": round(rep_ia / max(safe_limit_a, 0.1) * 100.0, 2),
            "pass": rep_ia <= safe_limit_a,
            "cells": rep["cells"],
        })

    payload = {
        "at_time": case.get("at_time"),
        "envelopes": envelopes,
        "effective_limit_a": eff_limit_a,
        "safe_limit_a": safe_limit_a,
        "thermal_limit_a": thermal_limit_a,
        "counterfactual_limit_applied": counterfactual_applied,
        "conservative_governing_current_a": max(metered_ia, sim_ia),
        "strictest_envelope_current_a": binding_env["current_a"],
        "binding_envelope": binding_env,
        "multi_hour_envelope": multi_hour_rows,
    }
    limit_note = (
        f'（按用户给定反事实安全限值 {safe_limit_a:.1f} A 校核，最严口径 {binding_env["current_a"]:.2f} A 占比 {binding_env["loading_safe_pct"]:.2f}%）'
        if counterfactual_applied else ""
    )
    return _message(
        "n1-revision", "PhysicsPowerFlowAgent", "SkepticAuditorAgent",
        "counterproposal",
        f'已按质疑完成三口径复算：潮流口径 {sim_ia:.2f} A vs 表计实测代数和保守上限 {metered_ia:.2f} A vs 比例修正折算 {scale_corrected_i_a:.2f} A{limit_note}。',
        payload, proposal, objection,
    )


def n1_arbitrate(profile, proposal, objection, revision):
    """SkepticAuditorAgent arbitrates the dual-basis N-1 envelope and signs off conditional or counterfactual verdict."""
    prop = proposal["payload"]
    rev = objection["payload"]
    recomp = revision["payload"]
    case = prop.get("case") or {}
    envelopes = recomp.get("envelopes") or []
    gov_i = recomp.get("conservative_governing_current_a")
    strictest_i = recomp.get("strictest_envelope_current_a") or gov_i
    binding_env = recomp.get("binding_envelope") or (max(envelopes, key=lambda e: e["current_a"]) if envelopes else None)
    sim_i = prop.get("sim_receiving_i_a")
    min_ug = prop.get("min_underground_kv")
    eff_lim = recomp.get("effective_limit_a")
    safe_lim = recomp.get("safe_limit_a") or DEFAULT_N1_SAFE_LIMIT_A
    thermal_lim = recomp.get("thermal_limit_a") or DEFAULT_N1_THERMAL_LIMIT_A
    counterfactual = bool(recomp.get("counterfactual_limit_applied"))

    any_safe_exceeded = any(not env.get("safe_pass", True) for env in envelopes)
    any_thermal_exceeded = any(not env.get("thermal_pass", True) for env in envelopes)
    pf_passes = envelopes[0].get("safe_pass", True) if envelopes else True

    constraint_verdict = "pending_field_rating"
    status = "conditional_candidate" if prop.get("status") == "ok" else prop.get("status", "error")
    if counterfactual and eff_lim is not None:
        strictest_ratio = round(strictest_i / max(eff_lim, 0.1) * 100.0, 2)
        gov_ratio = round(gov_i / max(eff_lim, 0.1) * 100.0, 2)
        if any_safe_exceeded or any_thermal_exceeded:
            status = "limit_exceeded_under_constraint"
            constraint_verdict = "exceeded_require_load_shedding"
            pf_contrast = (
                f'单一潮流口径（{sim_i:.2f} A，{round(sim_i / eff_lim * 100.0, 2):.2f}%）虽未触发告警，'
                if pf_passes else ""
            )
            verdict_str = (
                f'按反事实输入有效安全限值 {eff_lim:.1f} A 校核，{pf_contrast}'
                f'最严包络口径（{binding_env["label"]}，{strictest_i:.2f} A）负载率已达 {strictest_ratio:.2f}%（超限，表计代数上限占比 {gov_ratio:.2f}%），'
                f'仲裁否决全负荷盲目单回转供，须先核校 1010 CT 变比并制定非保安负荷限荷/分流预案。'
            )
        else:
            constraint_verdict = "within_counterfactual_limit"
            verdict_str = (
                f'按反事实输入有效限值 {eff_lim:.1f} A 校核，表计保守包络占比 {gov_ratio:.2f}%、最严包络占比 {strictest_ratio:.2f}%（均在给定限值内，仍须核验 1200 联络开关与现场铭牌）。'
            )
    else:
        verdict_str = (
            f'因归档缺失下井电缆真实额定载流量与 1200 联络开关操作票，按参考连续安全线 {safe_lim:.0f} A 保留三口径包络区间'
            f'（最严 {strictest_i:.2f} A，占参考线 {round(strictest_i / safe_lim * 100.0, 2):.2f}%）待 HITL 补证终审。'
        )

    statement = (
        f'{case.get("at_time")} 下井双回（{case.get("outage_cabinet")}→{case.get("receiving_cabinet")}）N-1 转供会诊裁决：'
        f'采纳安规专家质疑，拒绝单一偏低潮流值，确立「三相潮流 {sim_i:.2f} A ～ 表计实测代数和上限 {gov_i:.2f} A ～ 比例修正 {strictest_i:.2f} A」三口径安全校核包络'
        f'（井下仿真最低母线电压 {min_ug:.3f} kV）。{verdict_str}'
    )
    decision = {
        "study_kind": "n1_dual_basis_arbitration",
        "status": status,
        "constraint_verdict": constraint_verdict,
        "counterfactual_limit_applied": counterfactual,
        "effective_limit_a": eff_lim,
        "safe_limit_a": safe_lim,
        "thermal_limit_a": thermal_lim,
        "case": case,
        "envelopes": envelopes,
        "selected": {"binding_envelope": binding_env} if binding_env else None,
        "multi_hour_envelope": recomp.get("multi_hour_envelope") or [],
        "governing_current_a": gov_i,
        "strictest_envelope_current_a": strictest_i,
        "powerflow_current_a": sim_i,
        "min_underground_kv": min_ug,
        "discrepancy_a": rev.get("discrepancy_a"),
        "field_status": "pending_evidence",
        "review_requirements": N1_REVIEW_REQUIREMENTS,
    }
    return _message(
        "n1-decision", "SkepticAuditorAgent", "Consensus",
        "arbitration", statement, decision, objection, revision,
    )


def n1_conflict_record(decision, objection):
    """Emit structured cross-agent conflict when counterfactual limit flips N-1 verdict across bases."""
    payload = decision["payload"]
    if not payload.get("counterfactual_limit_applied") or payload.get("status") != "limit_exceeded_under_constraint":
        return []
    eff_lim = payload.get("effective_limit_a")
    sim_i = payload.get("powerflow_current_a")
    strictest_i = payload.get("strictest_envelope_current_a")
    return [{
        "id": "CONFLICT-N1-COUNTERFACTUAL-LIMIT",
        "severity": "high",
        "agents_involved": ["TelemetryMeteringAgent", "PhysicsPowerFlowAgent", "StandardsComplianceAgent"],
        "tension": (
            f'在用户给定 {eff_lim:.1f} A 反事实安全载流限值下，平衡三相潮流口径（{sim_i:.2f} A）与'
            f'计及 1010 量测比例偏离的最严包络口径（{strictest_i:.2f} A）结论相反（前者通过、后者超限）。'
        ),
        "resolution": decision["content"],
        "decision_message_id": decision["id"],
    }]


def n1_arbitration_evidence(archive, trace, decision):
    """Bind exact archived Excel cell coordinates for the N-1 dual-basis arbitration decision."""
    payload = decision["payload"]
    case = payload.get("case") or {}
    if case.get("status") != "ok":
        return [], []
    refs = [
        case[key]["cell"]
        for key in ("ia_outage", "ia_receiving", "p_outage", "p_receiving", "bus_voltage")
        if isinstance(case.get(key), dict) and case[key].get("cell")
    ]
    facts = build_observations(archive, refs, trace)
    if not facts:
        return None, []
    step = {
        "round": 3,
        "agent": "auditor",
        "tool": "arbitrate_n1_dual_basis",
        "arguments": {
            "at_time": case["at_time"],
            "outage_cabinet": case["outage_cabinet"],
            "receiving_cabinet": case["receiving_cabinet"],
        },
        "result": {"status": "ok", "decision": payload},
    }
    facts.append(bind_tool_fact(
        {"statement": decision["content"], "evidence": refs[:6]},
        step, "decision", kind="simulation",
    ))
    return facts, [step]

