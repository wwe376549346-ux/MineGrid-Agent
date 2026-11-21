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
    if not re.search(r"补偿|电容|(?:提高|提升|改善)功率因数|低功率因数.*过压", question):
        return None
    if re.search(r"N-1|转供", question, re.I) and not re.search(r"补偿|电容", question):
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
    return {
        "target": targets[0].cabinet, "requested_qc_kvar": float(capacity[1]) if capacity else None,
        "non_hourly": non_hourly,
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
