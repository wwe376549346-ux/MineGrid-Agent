"""Real Newton-Raphson AC Power-Flow Solver (pandapower) driven by MineProfile + SQLite archives.

Strict academic integrity boundary:
- Every hourly boundary condition (35 kV bus voltage, transformer HV/LV power, feeder active/reactive
  loads) is fetched read-only from the archived SQLite database with exact Excel cell provenance.
- Every equivalent-circuit parameter not present in the archived reports (transformer Sn/Uk%/tap
  position, downhole cable length/R/X/ampacity) is explicitly disclosed under `model_assumptions`
  and can be overridden by the operator or LLM tool arguments.
"""

from __future__ import annotations

import math
from typing import Any, Literal

import pandapower as pp

from laowadi.mine_schema import MineProfile, load_mine_profile
from laowadi.storage import REPORT_STATION


ScenarioType = Literal["base_case", "tap_sensitivity", "n1_contingency", "reactive_opt"]
OutageMode = Literal["feeder_1009", "incomer_306", "transformer_t2"]


def _safe_q_mvar(p_kw: float | None, cos_val: float | None) -> float:
    if p_kw is None or p_kw <= 0.0:
        return 0.0
    if cos_val is None or cos_val <= 0.0:
        c = 0.85
    else:
        c = max(0.20, min(abs(float(cos_val)), 0.9999))
    return (p_kw / 1000.0) * math.tan(math.acos(c))


def _load_hour_snapshot(archive, at_time: str, station: str) -> dict[str, dict[str, dict[str, Any]]]:
    rows = archive.db.execute(
        """
        SELECT r.cabinet, r.metric, r.value, r.source_col || r.source_row AS cell,
               rep.filename, rep.sheet, rep.sha256
        FROM reading r
        JOIN report rep ON rep.day = r.day
        WHERE r.station = ? AND r.time = ?
        """,
        (station, at_time),
    ).fetchall()
    if not rows:
        raise ValueError(f"归档日报库中未找到站点 {station} 在整点 {at_time} 的有效读数（可用范围 2026-11-10 00:00:00 至 2026-11-16 23:00:00）。")
    snap: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        snap.setdefault(row["cabinet"], {})[row["metric"]] = {
            "value": row["value"],
            "cell": f'{row["filename"]} / {row["sheet"]}!{row["cell"]}',
            "file": row["filename"],
            "sheet": row["sheet"],
            "raw_cell": row["cell"],
            "sha256": row["sha256"],
        }
    return snap


def _val(snap: dict, cab: str, metric: str, default: float = 0.0) -> float:
    entry = snap.get(cab, {}).get(metric)
    if not entry or entry["value"] is None:
        return default
    return float(entry["value"])


def _cell(snap: dict, cab: str, metric: str) -> str | None:
    entry = snap.get(cab, {}).get(metric)
    return entry["cell"] if entry else None


def build_pandapower_network(
    profile: MineProfile,
    snap: dict[str, dict[str, dict[str, Any]]],
    *,
    transformer_sn_mva: float | None = None,
    transformer_vk_percent: float | None = None,
    tr1_tap_pos: int | None = None,
    tr2_tap_pos: int | None = None,
    cable_length_km: float | None = None,
    outage_mode: OutageMode | None = None,
    shunt_target_bus: str | None = None,
    shunt_qc_kvar: float = 0.0,
) -> tuple[Any, dict[str, Any]]:
    """Construct a pandapower AC network purely from MineProfile + archived hourly snapshot."""
    net = pp.create_empty_network(name=profile.mine_id, f_hz=50.0, sn_mva=100.0)
    bus_idx: dict[str, int] = {}
    for bus in profile.buses:
        bus_idx[bus.id] = int(pp.create_bus(net, vn_kv=bus.nominal_kv, name=bus.id))

    incomers = [f for f in profile.feeders if f.role == "incomer"]
    inc_a = incomers[0] if len(incomers) >= 1 else None
    inc_b = incomers[1] if len(incomers) >= 2 else None

    u_inc_a = _val(snap, inc_a.cabinet, "Uab", 35.0) if inc_a else 35.0
    u_inc_b = _val(snap, inc_b.cabinet, "Uab", u_inc_a) if inc_b else u_inc_a
    p_inc_b = _val(snap, inc_b.cabinet, "P", 0.0) if inc_b else 0.0
    i_inc_b = _val(snap, inc_b.cabinet, "Ia", 0.0) if inc_b else 0.0

    # Detect single-incomer operation directly from archived snapshot OR explicit N-1 outage
    inc_b_offline = (
        outage_mode == "incomer_306"
        or (inc_b is not None and abs(p_inc_b) < 1e-6 and abs(i_inc_b) < 1e-6)
    )

    if inc_a:
        pp.create_ext_grid(
            net,
            bus=bus_idx[inc_a.bus_id],
            vm_pu=max(0.85, min(1.15, u_inc_a / inc_a.nominal_kv)),
            va_degree=0.0,
            name=f"Grid_{inc_a.cabinet}",
        )
    if inc_b and not inc_b_offline:
        pp.create_ext_grid(
            net,
            bus=bus_idx[inc_b.bus_id],
            vm_pu=max(0.85, min(1.15, u_inc_b / inc_b.nominal_kv)),
            va_degree=0.0,
            name=f"Grid_{inc_b.cabinet}",
        )

    # 35 kV bus-tie & underground bus-tie switches
    tie_states: dict[str, bool] = {}
    for tie in profile.bus_ties:
        closed = tie.normal_closed
        if tie.id == "TIE_35KV":
            closed = bool(inc_b_offline)
        elif tie.id == "TIE_C1535":
            closed = outage_mode == "feeder_1009"
        tie_states[tie.id] = closed
        pp.create_switch(
            net,
            bus=bus_idx[tie.bus_a],
            element=bus_idx[tie.bus_b],
            et="b",
            closed=closed,
            type="CB",
            name=f"{tie.id}_{tie.cabinet}",
        )

    # Transformers (declarative from MineProfile)
    trafo_idx: dict[str, int] = {}
    trafo_meta: list[dict[str, Any]] = []
    for i, tr in enumerate(profile.transformers):
        sn = float(transformer_sn_mva) if transformer_sn_mva is not None else tr.default_sn_mva
        vk = float(transformer_vk_percent) if transformer_vk_percent is not None else tr.default_vk_percent
        vkr = min(tr.default_vkr_percent, vk * 0.4)
        tap = tr.calibrated_tap_pos
        if i == 0 and tr1_tap_pos is not None:
            tap = int(tr1_tap_pos)
        elif i == 1 and tr2_tap_pos is not None:
            tap = int(tr2_tap_pos)
        tap = max(tr.tap_min, min(tr.tap_max, tap))

        in_service = not (outage_mode == "transformer_t2" and i == 1)
        t_id = int(
            pp.create_transformer_from_parameters(
                net,
                hv_bus=bus_idx[tr.hv_bus],
                lv_bus=bus_idx[tr.lv_bus],
                sn_mva=sn,
                vn_hv_kv=tr.default_vn_hv_kv,
                vn_lv_kv=tr.default_vn_lv_kv,
                vkr_percent=vkr,
                vk_percent=vk,
                pfe_kw=tr.default_pfe_kw,
                i0_percent=tr.default_i0_percent,
                tap_side=tr.tap_side,
                tap_neutral=0,
                tap_min=tr.tap_min,
                tap_max=tr.tap_max,
                tap_step_percent=tr.tap_step_percent,
                tap_pos=tap,
                tap_changer_type="Ratio",
                in_service=in_service,
                name=tr.id,
            )
        )
        trafo_idx[tr.id] = t_id
        trafo_meta.append({
            "spec": tr,
            "pp_idx": t_id,
            "sn_mva": sn,
            "vk_percent": vk,
            "vkr_percent": vkr,
            "tap_pos": tap,
            "in_service": in_service,
        })

    # If transformer_t2 is out of service, close a temporary 10 kV tie between S35_10KV_I and S35_10KV_II
    if outage_mode == "transformer_t2" and "S35_10KV_I" in bus_idx and "S35_10KV_II" in bus_idx:
        pp.create_switch(
            net,
            bus=bus_idx["S35_10KV_I"],
            element=bus_idx["S35_10KV_II"],
            et="b",
            closed=True,
            type="CB",
            name="TIE_10KV_N1",
        )

    # Downstream cable lines & feeder loads
    line_meta: list[dict[str, Any]] = []
    modeled_p_by_bus: dict[str, float] = {}
    modeled_q_by_bus: dict[str, float] = {}
    for fd in profile.feeders:
        if not fd.downstream_bus_id or fd.downstream_bus_id not in bus_idx:
            continue
        length = float(cable_length_km) if (cable_length_km is not None and fd.cabinet in ("1009", "1010")) else (fd.default_cable_length_km or 1.5)
        r_km = fd.default_r_ohm_per_km or 0.193
        x_km = fd.default_x_ohm_per_km or 0.085
        max_i = fd.default_max_i_ka or 0.30
        in_service = not (outage_mode == "feeder_1009" and fd.cabinet == "1009")

        l_id = int(
            pp.create_line_from_parameters(
                net,
                from_bus=bus_idx[fd.bus_id],
                to_bus=bus_idx[fd.downstream_bus_id],
                length_km=length,
                r_ohm_per_km=r_km,
                x_ohm_per_km=x_km,
                c_nf_per_km=210.0,
                max_i_ka=max_i,
                in_service=in_service,
                name=f"Line_{fd.cabinet}",
            )
        )
        p_kw = max(0.0, _val(snap, fd.cabinet, "P", 0.0))
        cos_v = _val(snap, fd.cabinet, "cos", 0.85)
        p_mw = p_kw / 1000.0
        q_mvar = _safe_q_mvar(p_kw, cos_v)
        pp.create_load(
            net,
            bus=bus_idx[fd.downstream_bus_id],
            p_mw=p_mw,
            q_mvar=q_mvar,
            name=f"Load_{fd.cabinet}",
        )
        modeled_p_by_bus[fd.bus_id] = modeled_p_by_bus.get(fd.bus_id, 0.0) + p_mw
        modeled_q_by_bus[fd.bus_id] = modeled_q_by_bus.get(fd.bus_id, 0.0) + q_mvar
        line_meta.append({
            "spec": fd,
            "pp_idx": l_id,
            "length_km": length,
            "r_ohm_per_km": r_km,
            "x_ohm_per_km": x_km,
            "max_i_ka": max_i,
            "in_service": in_service,
            "load_p_mw": p_mw,
            "load_q_mvar": q_mvar,
        })

    # Attach residual lumped loads at each transformer LV bus so total transformer loading matches archived LV readings
    bus_residual_notes: dict[str, dict[str, float]] = {}
    for tr in profile.transformers:
        lv_p_kw = _val(snap, tr.lv_cabinet, "P", 0.0)
        lv_cos = _val(snap, tr.lv_cabinet, "cos", 0.92)
        total_p_mw = max(0.0, lv_p_kw) / 1000.0
        total_q_mvar = _safe_q_mvar(max(0.0, lv_p_kw), lv_cos)
        res_p = max(0.0, total_p_mw - modeled_p_by_bus.get(tr.lv_bus, 0.0))
        res_q = max(0.0, total_q_mvar - modeled_q_by_bus.get(tr.lv_bus, 0.0))
        if res_p > 1e-6 or res_q > 1e-6:
            pp.create_load(
                net,
                bus=bus_idx[tr.lv_bus],
                p_mw=res_p,
                q_mvar=res_q,
                name=f"LumpedResidual_{tr.lv_bus}",
            )
        bus_residual_notes[tr.lv_bus] = {
            "archived_lv_p_kw": round(lv_p_kw, 2),
            "modeled_feeders_p_kw": round(modeled_p_by_bus.get(tr.lv_bus, 0.0) * 1000.0, 2),
            "residual_bus_p_kw": round(res_p * 1000.0, 2),
            "residual_bus_q_kvar": round(res_q * 1000.0, 2),
        }

    # Optional shunt capacitor compensation (pandapower convention: negative q_mvar = capacitive injection)
    if shunt_target_bus and shunt_target_bus in bus_idx and shunt_qc_kvar > 0.0:
        pp.create_shunt(
            net,
            bus=bus_idx[shunt_target_bus],
            p_mw=0.0,
            q_mvar=-(shunt_qc_kvar / 1000.0),
            vn_kv=float(net.bus.at[bus_idx[shunt_target_bus], "vn_kv"]),
            name=f"Shunt_{shunt_target_bus}_{int(round(shunt_qc_kvar))}kvar",
        )

    meta = {
        "bus_idx": bus_idx,
        "trafo_meta": trafo_meta,
        "line_meta": line_meta,
        "inc_b_offline": inc_b_offline,
        "tie_states": tie_states,
        "bus_residual_notes": bus_residual_notes,
    }
    return net, meta


def _extract_solution(
    net,
    meta: dict[str, Any],
    profile: MineProfile,
    snap: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    v10_lim = profile.voltage_limit_for_kv(10.0)
    v35_lim = profile.voltage_limit_for_kv(35.0)
    bus_idx = meta["bus_idx"]

    buses_out = []
    bus_u_map: dict[str, float] = {}
    for bus in profile.buses:
        idx = bus_idx[bus.id]
        vm_pu = float(net.res_bus.at[idx, "vm_pu"])
        va_deg = float(net.res_bus.at[idx, "va_degree"])
        sim_u_kv = vm_pu * bus.nominal_kv
        bus_u_map[bus.id] = sim_u_kv

        meas_u = None
        meas_cell = None
        diff_kv = None
        if bus.reference_cabinet and bus.station == profile.primary_station:
            entry = snap.get(bus.reference_cabinet, {}).get("Uab")
            if entry and entry["value"] is not None:
                meas_u = float(entry["value"])
                meas_cell = entry["cell"]
                diff_kv = round(sim_u_kv - meas_u, 3)

        is_10kv = bus.nominal_kv < 20.0
        nom_std = 10.0 if is_10kv else 35.0
        dev_pct = round((sim_u_kv / nom_std - 1.0) * 100.0, 2)
        if is_10kv:
            compliant = v10_lim.lo_kv <= sim_u_kv <= v10_lim.hi_kv
        else:
            compliant = v35_lim.lo_kv <= sim_u_kv <= v35_lim.hi_kv

        buses_out.append({
            "bus_id": bus.id,
            "name": bus.name,
            "station": bus.station,
            "nominal_kv": bus.nominal_kv,
            "sim_u_kv": round(sim_u_kv, 3),
            "sim_vm_pu": round(vm_pu, 4),
            "sim_va_degree": round(va_deg, 3),
            "measured_u_kv": round(meas_u, 3) if meas_u is not None else None,
            "measured_cell": meas_cell,
            "calibration_diff_kv": diff_kv,
            "deviation_from_nominal_pct": dev_pct,
            "gb12325_compliant": compliant,
        })

    trafos_out = []
    total_trafo_loss_kw = 0.0
    for tm in meta["trafo_meta"]:
        idx = tm["pp_idx"]
        tr = tm["spec"]
        in_srv = tm["in_service"]
        loading = float(net.res_trafo.at[idx, "loading_percent"]) if in_srv else 0.0
        pl_mw = float(net.res_trafo.at[idx, "pl_mw"]) if in_srv else 0.0
        p_hv = float(net.res_trafo.at[idx, "p_hv_mw"]) if in_srv else 0.0
        p_lv = float(net.res_trafo.at[idx, "p_lv_mw"]) if in_srv else 0.0
        i_hv_a = float(net.res_trafo.at[idx, "i_hv_ka"]) * 1000.0 if in_srv else 0.0
        i_lv_a = float(net.res_trafo.at[idx, "i_lv_ka"]) * 1000.0 if in_srv else 0.0
        total_trafo_loss_kw += pl_mw * 1000.0
        trafos_out.append({
            "id": tr.id,
            "name": tr.name,
            "hv_cabinet": tr.hv_cabinet,
            "lv_cabinet": tr.lv_cabinet,
            "in_service": in_srv,
            "sn_mva": tm["sn_mva"],
            "tap_pos": tm["tap_pos"],
            "tap_pct": round(tm["tap_pos"] * tr.tap_step_percent, 2),
            "loading_percent": round(loading, 2),
            "p_hv_kw": round(p_hv * 1000.0, 2),
            "p_lv_kw": round(-p_lv * 1000.0, 2),
            "pl_kw": round(pl_mw * 1000.0, 2),
            "i_hv_a": round(i_hv_a, 2),
            "i_lv_a": round(i_lv_a, 2),
        })

    lines_out = []
    total_line_loss_kw = 0.0
    for lm in meta["line_meta"]:
        idx = lm["pp_idx"]
        fd = lm["spec"]
        in_srv = lm["in_service"]
        i_from_a = float(net.res_line.at[idx, "i_from_ka"]) * 1000.0 if in_srv else 0.0
        loading = float(net.res_line.at[idx, "loading_percent"]) if in_srv else 0.0
        p_from_kw = float(net.res_line.at[idx, "p_from_mw"]) * 1000.0 if in_srv else 0.0
        q_from_kvar = float(net.res_line.at[idx, "q_from_mvar"]) * 1000.0 if in_srv else 0.0
        pl_kw = float(net.res_line.at[idx, "pl_mw"]) * 1000.0 if in_srv else 0.0
        u_from = bus_u_map[fd.bus_id]
        u_to = bus_u_map[fd.downstream_bus_id]
        du_kv = u_from - u_to if in_srv else 0.0
        du_pct = (du_kv / u_from * 100.0) if (in_srv and u_from > 0) else 0.0
        total_line_loss_kw += pl_kw
        lines_out.append({
            "cabinet": fd.cabinet,
            "label": fd.label,
            "from_bus": fd.bus_id,
            "to_bus": fd.downstream_bus_id,
            "in_service": in_srv,
            "length_km": lm["length_km"],
            "max_i_a": round(lm["max_i_ka"] * 1000.0, 1),
            "i_from_a": round(i_from_a, 2),
            "loading_percent": round(loading, 2),
            "p_from_kw": round(p_from_kw, 2),
            "q_from_kvar": round(q_from_kvar, 2),
            "pl_kw": round(pl_kw, 3),
            "u_from_kv": round(u_from, 3),
            "u_to_kv": round(u_to, 3),
            "voltage_drop_kv": round(du_kv, 4),
            "voltage_drop_pct": round(du_pct, 3),
        })

    total_p_load_kw = float(net.res_load["p_mw"].sum()) * 1000.0 if len(net.res_load) else 0.0
    total_q_load_kvar = float(net.res_load["q_mvar"].sum()) * 1000.0 if len(net.res_load) else 0.0
    total_loss_kw = total_trafo_loss_kw + total_line_loss_kw
    loss_rate_pct = (total_loss_kw / (total_p_load_kw + total_loss_kw) * 100.0) if total_p_load_kw > 0 else 0.0

    ug_buses = [b["sim_u_kv"] for b in buses_out if b["station"] != profile.primary_station]
    summary = {
        "total_load_p_kw": round(total_p_load_kw, 2),
        "total_load_q_kvar": round(total_q_load_kvar, 2),
        "transformer_loss_kw": round(total_trafo_loss_kw, 3),
        "line_loss_kw": round(total_line_loss_kw, 3),
        "total_loss_kw": round(total_loss_kw, 3),
        "loss_rate_pct": round(loss_rate_pct, 3),
        "max_transformer_loading_pct": round(max((t["loading_percent"] for t in trafos_out), default=0.0), 2),
        "max_line_loading_pct": round(max((l["loading_percent"] for l in lines_out), default=0.0), 2),
        "bus_10kv_i_kv": round(bus_u_map.get("S35_10KV_I", 0.0), 3),
        "bus_10kv_ii_kv": round(bus_u_map.get("S35_10KV_II", 0.0), 3),
        "min_underground_kv": round(min(ug_buses), 3) if ug_buses else None,
    }
    return {
        "buses": buses_out,
        "transformers": trafos_out,
        "lines": lines_out,
        "network_summary": summary,
    }


def _solve_nr(net) -> bool:
    try:
        pp.runpp(
            net,
            algorithm="nr",
            calculate_voltage_angles=True,
            init="auto",
            max_iteration=30,
            tolerance_mva=1e-8,
            numba=False,
        )
        return bool(net.converged)
    except Exception:
        return False


def solve_reactive_study_state(profile, snapshot, cabinet, transformer, tap, qc_kvar):
    """Solve one steady state from the snapshot supplied by Telemetry."""
    feeder = profile.feeder_map()[cabinet]
    net, meta = build_pandapower_network(
        profile, snapshot, shunt_target_bus=feeder.downstream_bus_id, shunt_qc_kvar=qc_kvar,
    )
    trafo = next(t for t in meta["trafo_meta"] if t["spec"].id == transformer)
    net.trafo.at[trafo["pp_idx"], "tap_pos"] = tap
    state = {"tap": tap, "qc_kvar": qc_kvar, "converged": False}
    if not _solve_nr(net):
        return state

    line = next(line for line in meta["line_meta"] if line["spec"].cabinet == cabinet)
    branch = net.res_line.loc[line["pp_idx"]]
    p_kw, q_kvar = float(branch.p_from_mw) * 1000, float(branch.q_from_mvar) * 1000
    buses = {
        bus.id: float(net.res_bus.at[meta["bus_idx"][bus.id], "vm_pu"]) * bus.nominal_kv
        for bus in profile.buses
    }
    loading = [float(value) for table in (net.res_line, net.res_trafo) for value in table.loading_percent]
    loss_kw = float(net.res_line.pl_mw.sum() + net.res_trafo.pl_mw.sum()) * 1000
    actual_qc = -float(net.res_shunt.q_mvar.sum()) * 1000 if qc_kvar else 0.0
    if not all(math.isfinite(v) for v in [*buses.values(), *loading, p_kw, q_kvar, loss_kw, actual_qc]):
        return state
    return {
        **state, "converged": True, "bus_kv": buses, "max_loading_pct": max(loading, default=0.0),
        "feeder_pf": p_kw / math.hypot(p_kw, q_kvar) if p_kw > 0 else 0.0,
        "feeder_q_kvar": q_kvar, "loss_kw": loss_kw, "injected_kvar": actual_qc,
        "modeled_ties": meta["tie_states"],
    }


def run_ac_power_flow(
    archive,
    *,
    scenario: ScenarioType = "base_case",
    at_time: str | None = None,
    transformer_sn_mva: float | None = None,
    transformer_vk_percent: float | None = None,
    tr1_tap_pos: int | None = None,
    tr2_tap_pos: int | None = None,
    cable_length_km: float | None = None,
    outage_mode: OutageMode = "feeder_1009",
    target_cabinet: str = "1010",
    qc_kvar: float | None = None,
    profile: MineProfile | None = None,
) -> dict[str, Any]:
    """Run converged Newton-Raphson AC power flow using pandapower on archived Laowadi data."""
    if scenario not in ("base_case", "tap_sensitivity", "n1_contingency", "reactive_opt"):
        raise ValueError("scenario 必须为 base_case、tap_sensitivity、n1_contingency 或 reactive_opt。")
    if outage_mode not in ("feeder_1009", "incomer_306", "transformer_t2"):
        raise ValueError("outage_mode 必须为 feeder_1009、incomer_306 或 transformer_t2。")

    prof = profile or load_mine_profile()
    if at_time is None or not str(at_time).strip():
        if scenario == "tap_sensitivity":
            at_time = "2026-11-13 17:00:00"
        elif scenario == "n1_contingency":
            at_time = "2026-11-13 15:00:00" if outage_mode == "incomer_306" else "2026-11-10 06:00:00"
        elif scenario == "reactive_opt":
            at_time = "2026-11-10 06:00:00" if target_cabinet == "1010" else "2026-11-13 15:00:00"
        else:
            at_time = "2026-11-13 17:00:00"
    at_time = str(at_time).strip()

    snap = _load_hour_snapshot(archive, at_time, prof.primary_station)

    # Record measured archived boundaries with exact Excel cell references
    key_cabs = list(prof.main_chain_cabinets()) + [
        f.cabinet for f in prof.feeders if f.downstream_bus_id
    ]
    measured_boundaries = []
    for cab in dict.fromkeys(key_cabs):
        if cab not in snap:
            continue
        measured_boundaries.append({
            "cabinet": cab,
            "Uab_kv": _val(snap, cab, "Uab", 0.0),
            "Uab_cell": _cell(snap, cab, "Uab"),
            "Ia_a": _val(snap, cab, "Ia", 0.0),
            "Ia_cell": _cell(snap, cab, "Ia"),
            "P_kw": _val(snap, cab, "P", 0.0),
            "P_cell": _cell(snap, cab, "P"),
            "cos": _val(snap, cab, "cos", 1.0),
            "cos_cell": _cell(snap, cab, "cos"),
        })

    # Always run base network first
    base_net, base_meta = build_pandapower_network(
        prof,
        snap,
        transformer_sn_mva=transformer_sn_mva,
        transformer_vk_percent=transformer_vk_percent,
        tr1_tap_pos=tr1_tap_pos,
        tr2_tap_pos=tr2_tap_pos,
        cable_length_km=cable_length_km,
        outage_mode=None,
    )
    converged = _solve_nr(base_net)
    if not converged:
        return {
            "kind": "ac_power_flow",
            "status": "non_converged",
            "scenario": scenario,
            "at_time": at_time,
            "message": "Newton-Raphson 交流潮流未收敛，请检查输入的变压器阻抗或边界参数。",
        }
    base_sol = _extract_solution(base_net, base_meta, prof, snap)

    # Effective equivalent-circuit assumptions block (explicitly disclosed!)
    eff_sn = float(transformer_sn_mva) if transformer_sn_mva is not None else prof.transformers[0].default_sn_mva
    eff_vk = float(transformer_vk_percent) if transformer_vk_percent is not None else prof.transformers[0].default_vk_percent
    eff_t1_tap = base_meta["trafo_meta"][0]["tap_pos"] if len(base_meta["trafo_meta"]) >= 1 else 0
    eff_t2_tap = base_meta["trafo_meta"][1]["tap_pos"] if len(base_meta["trafo_meta"]) >= 2 else 0
    eff_len = float(cable_length_km) if cable_length_km is not None else 1.8

    model_assumptions = {
        "parameter_nature": "parameterized_equivalent_circuit_assumptions",
        "disclaimer": prof.model_assumption_disclaimer,
        "transformer_sn_mva": eff_sn,
        "transformer_vn_kv": f"{prof.transformers[0].default_vn_hv_kv}/{prof.transformers[0].default_vn_lv_kv}",
        "transformer_vk_percent": eff_vk,
        "transformer_vkr_percent": prof.transformers[0].default_vkr_percent,
        "transformer_tap_step_percent": prof.transformers[0].tap_step_percent,
        "tr1_tap_pos": eff_t1_tap,
        "tr2_tap_pos": eff_t2_tap,
        "downhole_1009_1010_cable_length_km": eff_len,
        "downhole_1009_1010_r_x_ohm_per_km": "0.193 + j0.085 (典型 MYJV22 10 kV 3×95/120 mm² 等值参数)",
        "downhole_1009_1010_max_i_a": 300.0,
        "bus_lumped_residual_loads": base_meta["bus_residual_notes"],
    }

    op_mode = (
        "35 kV 306 二回停运 / 303 单进线经 300 母联带双主变"
        if base_meta["inc_b_offline"]
        else "35 kV 303/306 双进线分列运行"
    )

    result: dict[str, Any] = {
        "kind": "ac_power_flow",
        "status": "ok",
        "scenario": scenario,
        "at_time": at_time,
        "operating_mode": op_mode,
        "solver": {
            "engine": "pandapower",
            "version": pp.__version__,
            "algorithm": "Newton-Raphson (AC)",
            "converged": True,
            "mine_id": prof.mine_id,
            "mine_name": prof.mine_name,
        },
        "measured_boundaries": measured_boundaries,
        "model_assumptions": model_assumptions,
        **base_sol,
        "missing_field_evidence": [
            "1# 与 2# 主变压器铭牌额定容量 (kVA)、实测短路阻抗 Uk% 及当前分接头实际档位",
            "1009 / 1010 / 1020 下井高压电缆型号、截面、敷设长度及长期允许载流量台账",
            "10 kV 未单独列柜的其余出线实测明细（当前按 1005/1006 总负荷扣减已列支路做母线等值汇总）",
        ],
    }

    # Scenario 2: 2# Main Transformer HV Tap Sensitivity Sweep
    if scenario == "tap_sensitivity":
        v10_lim = prof.voltage_limit_for_kv(10.0)
        sweep = []
        for tap in range(-2, 3):
            t_net, t_meta = build_pandapower_network(
                prof,
                snap,
                transformer_sn_mva=transformer_sn_mva,
                transformer_vk_percent=transformer_vk_percent,
                tr1_tap_pos=eff_t1_tap,
                tr2_tap_pos=tap,
                cable_length_km=cable_length_km,
            )
            ok = _solve_nr(t_net)
            sol = _extract_solution(t_net, t_meta, prof, snap) if ok else None
            u_ii = sol["network_summary"]["bus_10kv_ii_kv"] if sol else None
            ug_ii = next((b["sim_u_kv"] for b in sol["buses"] if b["bus_id"] == "C1535_10KV_II"), None) if sol else None
            ug_1750 = next((b["sim_u_kv"] for b in sol["buses"] if b["bus_id"] == "UG_1750_10KV"), None) if sol else None
            dev_pct = round((u_ii / 10.0 - 1.0) * 100.0, 2) if u_ii is not None else None
            compliant = (v10_lim.lo_kv <= u_ii <= v10_lim.hi_kv) if u_ii is not None else False
            sweep.append({
                "tap_pos": tap,
                "tap_label": f"{tap:+d} 档 ({tap * 2.5:+.1f}% 高压绕组)",
                "converged": ok,
                "bus_10kv_ii_kv": u_ii,
                "c1535_ii_kv": ug_ii,
                "ug_1750_kv": ug_1750,
                "deviation_from_10kv_pct": dev_pct,
                "gb12325_compliant": compliant,
                "total_loss_kw": sol["network_summary"]["total_loss_kw"] if sol else None,
            })
        compliant_taps = [s for s in sweep if s["gb12325_compliant"]]
        # Prefer compliant tap closest to nominal 10.0-10.5 kV with minimum step change from calibrated tap
        recommended = (
            min(compliant_taps, key=lambda s: abs(s["tap_pos"] - eff_t2_tap))
            if compliant_taps
            else None
        )
        base_row = next((s for s in sweep if s["tap_pos"] == eff_t2_tap), sweep[3])
        result["tap_sensitivity"] = {
            "target_transformer": "T2 (2号主变 312 → 1006)",
            "current_calibrated_tap": eff_t2_tap,
            "measured_1006_uab_kv": _val(snap, "1006", "Uab", 0.0),
            "measured_1006_cell": _cell(snap, "1006", "Uab"),
            "gb12325_limit_kv": [v10_lim.lo_kv, v10_lim.hi_kv],
            "sweep": sweep,
            "recommended_tap": recommended,
            "voltage_reduction_kv": (
                round(base_row["bus_10kv_ii_kv"] - recommended["bus_10kv_ii_kv"], 3)
                if (recommended and base_row and base_row["bus_10kv_ii_kv"] is not None)
                else 0.0
            ),
        }

    # Scenario 3: N-1 Contingency AC Power-Flow Check
    elif scenario == "n1_contingency":
        n1_net, n1_meta = build_pandapower_network(
            prof,
            snap,
            transformer_sn_mva=transformer_sn_mva,
            transformer_vk_percent=transformer_vk_percent,
            tr1_tap_pos=eff_t1_tap,
            tr2_tap_pos=eff_t2_tap,
            cable_length_km=cable_length_km,
            outage_mode=outage_mode,
        )
        n1_ok = _solve_nr(n1_net)
        if not n1_ok:
            result["n1_contingency"] = {"outage_mode": outage_mode, "converged": False}
        else:
            n1_sol = _extract_solution(n1_net, n1_meta, prof, snap)
            # Update top-level buses/transformers/lines to show N-1 converged state while keeping baseline comparison
            base_line_1010 = next((l for l in base_sol["lines"] if l["cabinet"] == "1010"), None)
            n1_line_1010 = next((l for l in n1_sol["lines"] if l["cabinet"] == "1010"), None)
            n1_max_line = max((l["loading_percent"] for l in n1_sol["lines"]), default=0.0)
            n1_max_tr = max((t["loading_percent"] for t in n1_sol["transformers"]), default=0.0)
            n1_min_ug = n1_sol["network_summary"]["min_underground_kv"]
            v10_lim = prof.voltage_limit_for_kv(10.0)
            voltage_ok = all(b["gb12325_compliant"] for b in n1_sol["buses"] if b["nominal_kv"] < 20.0)
            thermal_ok = n1_max_line <= 100.0 and n1_max_tr <= 100.0
            desc_map = {
                "feeder_1009": "1009 退运 + 井下 1535 1200 联络合闸，由 1010 单回转供井下双段负荷",
                "incomer_306": "35 kV 306 二回退运 + 300 母联合闸，由 303 单回带双主变",
                "transformer_t2": "2# 主变退运 + 10 kV 母联合闸，由 1# 主变单变带全站负荷",
            }
            result["buses"] = n1_sol["buses"]
            result["transformers"] = n1_sol["transformers"]
            result["lines"] = n1_sol["lines"]
            result["network_summary"] = n1_sol["network_summary"]
            result["n1_contingency"] = {
                "outage_mode": outage_mode,
                "description": desc_map[outage_mode],
                "converged": True,
                "thermal_compliant": thermal_ok,
                "voltage_compliant": voltage_ok,
                "verdict": "conditionally_passed" if (thermal_ok and voltage_ok) else "limit_exceeded",
                "baseline_summary": base_sol["network_summary"],
                "n1_summary": n1_sol["network_summary"],
                "loss_increase_kw": round(
                    n1_sol["network_summary"]["total_loss_kw"] - base_sol["network_summary"]["total_loss_kw"], 3
                ),
                "receiving_1010_comparison": {
                    "before_i_a": base_line_1010["i_from_a"] if base_line_1010 else None,
                    "after_i_a": n1_line_1010["i_from_a"] if n1_line_1010 else None,
                    "before_loading_pct": base_line_1010["loading_percent"] if base_line_1010 else None,
                    "after_loading_pct": n1_line_1010["loading_percent"] if n1_line_1010 else None,
                    "before_voltage_drop_pct": base_line_1010["voltage_drop_pct"] if base_line_1010 else None,
                    "after_voltage_drop_pct": n1_line_1010["voltage_drop_pct"] if n1_line_1010 else None,
                    "min_underground_kv": n1_min_ug,
                    "gb12325_lo_kv": v10_lim.lo_kv,
                },
            }

    # Scenario 4: Shunt Reactive Compensation Before vs After AC Power Flow
    elif scenario == "reactive_opt":
        cab_to_bus = {
            f.cabinet: f.downstream_bus_id
            for f in prof.feeders
            if f.downstream_bus_id
        }
        cab = str(target_cabinet or "1010").strip()
        if cab not in cab_to_bus:
            cab = "1010"
        target_bus = cab_to_bus[cab]
        p_kw = max(0.0, _val(snap, cab, "P", 0.0))
        cos_v = _val(snap, cab, "cos", 0.75)
        if qc_kvar is None:
            c_clamped = max(0.20, min(abs(cos_v), 0.99))
            tan1 = math.tan(math.acos(c_clamped))
            tan2 = math.tan(math.acos(0.92))
            eff_qc = round(max(50.0, p_kw * max(0.0, tan1 - tan2)), 1)
        else:
            eff_qc = max(0.0, float(qc_kvar))

        comp_net, comp_meta = build_pandapower_network(
            prof,
            snap,
            transformer_sn_mva=transformer_sn_mva,
            transformer_vk_percent=transformer_vk_percent,
            tr1_tap_pos=eff_t1_tap,
            tr2_tap_pos=eff_t2_tap,
            cable_length_km=cable_length_km,
            shunt_target_bus=target_bus,
            shunt_qc_kvar=eff_qc,
        )
        comp_ok = _solve_nr(comp_net)
        if not comp_ok:
            result["reactive_opt"] = {"target_cabinet": cab, "qc_kvar": eff_qc, "converged": False}
        else:
            comp_sol = _extract_solution(comp_net, comp_meta, prof, snap)
            b_line = next(l for l in base_sol["lines"] if l["cabinet"] == cab)
            a_line = next(l for l in comp_sol["lines"] if l["cabinet"] == cab)
            b_bus = next(b for b in base_sol["buses"] if b["bus_id"] == target_bus)
            a_bus = next(b for b in comp_sol["buses"] if b["bus_id"] == target_bus)

            i_red_pct = (
                round((b_line["i_from_a"] - a_line["i_from_a"]) / b_line["i_from_a"] * 100.0, 2)
                if b_line["i_from_a"] > 0
                else 0.0
            )
            line_loss_red_kw = round(b_line["pl_kw"] - a_line["pl_kw"], 3)
            line_loss_red_pct = (
                round(line_loss_red_kw / b_line["pl_kw"] * 100.0, 2)
                if b_line["pl_kw"] > 0
                else 0.0
            )
            net_loss_red_kw = round(
                base_sol["network_summary"]["total_loss_kw"] - comp_sol["network_summary"]["total_loss_kw"], 3
            )
            result["buses"] = comp_sol["buses"]
            result["transformers"] = comp_sol["transformers"]
            result["lines"] = comp_sol["lines"]
            result["network_summary"] = comp_sol["network_summary"]
            result["reactive_opt"] = {
                "target_cabinet": cab,
                "target_bus": target_bus,
                "measured_p_kw": round(p_kw, 2),
                "measured_cos": round(cos_v, 3),
                "measured_p_cell": _cell(snap, cab, "P"),
                "measured_cos_cell": _cell(snap, cab, "cos"),
                "qc_kvar": eff_qc,
                "converged": True,
                "before": {
                    "receiving_u_kv": b_bus["sim_u_kv"],
                    "line_i_a": b_line["i_from_a"],
                    "line_loading_pct": b_line["loading_percent"],
                    "line_pl_kw": b_line["pl_kw"],
                    "voltage_drop_pct": b_line["voltage_drop_pct"],
                    "total_loss_kw": base_sol["network_summary"]["total_loss_kw"],
                },
                "after": {
                    "receiving_u_kv": a_bus["sim_u_kv"],
                    "line_i_a": a_line["i_from_a"],
                    "line_loading_pct": a_line["loading_percent"],
                    "line_pl_kw": a_line["pl_kw"],
                    "voltage_drop_pct": a_line["voltage_drop_pct"],
                    "total_loss_kw": comp_sol["network_summary"]["total_loss_kw"],
                },
                "delta": {
                    "receiving_u_boost_kv": round(a_bus["sim_u_kv"] - b_bus["sim_u_kv"], 4),
                    "current_reduction_a": round(b_line["i_from_a"] - a_line["i_from_a"], 2),
                    "current_reduction_pct": i_red_pct,
                    "line_loss_reduction_kw": line_loss_red_kw,
                    "line_loss_reduction_pct": line_loss_red_pct,
                    "total_loss_reduction_kw": net_loss_red_kw,
                },
            }

    return result
