"""Real Newton-Raphson AC Power-Flow Solver (pandapower) driven by MineProfile + SQLite archives.

Strict academic integrity boundary:
- Every hourly boundary condition (35 kV bus voltage, transformer HV/LV power, feeder active/reactive
  loads) is fetched read-only from the archived SQLite database with exact Excel cell provenance.
- Every equivalent-circuit parameter not present in the archived reports (transformer Sn/Uk%/tap
  position, downhole cable length/R/X/ampacity) is explicitly disclosed under `model_assumptions`
  and can be overridden by the operator or LLM tool arguments.
"""

from __future__ import annotations

from collections import OrderedDict
import math
import threading
import time
from typing import Any, Literal

import pandapower as pp

from laowadi.mine_schema import MineProfile, load_mine_profile
from laowadi.storage import REPORT_STATION


ScenarioType = Literal["base_case", "tap_sensitivity", "n1_contingency", "reactive_opt"]
OutageMode = Literal["feeder_1009", "incomer_306", "transformer_t2"]

_DEFAULT_POWER_FACTOR = 0.85
_MIN_POWER_FACTOR = 0.20
_MAX_POWER_FACTOR = 0.9999
_EPS_DIVISOR = 1e-6


class _BoundedLRUCache:
    """Thread-safe bounded LRU cache with optional TTL eviction for power-flow memoization."""

    def __init__(self, maxsize: int = 32, ttl_seconds: float | None = 3600.0) -> None:
        self.maxsize = max(1, int(maxsize))
        self.ttl_seconds = ttl_seconds
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def _evict_expired_locked(self, now: float) -> None:
        if self.ttl_seconds is None:
            return
        expired = [k for k, (ts, _) in self._data.items() if now - ts > self.ttl_seconds]
        for k in expired:
            self._data.pop(k, None)

    def get(self, key: Any, default: Any = None) -> Any:
        now = time.monotonic()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self.misses += 1
                return default
            ts, val = item
            if self.ttl_seconds is not None and (now - ts) > self.ttl_seconds:
                self._data.pop(key, None)
                self.misses += 1
                return default
            self._data.move_to_end(key)
            self.hits += 1
            return val

    def set(self, key: Any, value: Any) -> None:
        now = time.monotonic()
        with self._lock:
            self._evict_expired_locked(now)
            self._data[key] = (now, value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def __contains__(self, key: Any) -> bool:
        now = time.monotonic()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return False
            if self.ttl_seconds is not None and (now - item[0]) > self.ttl_seconds:
                self._data.pop(key, None)
                return False
            return True

    def __getitem__(self, key: Any) -> Any:
        val = self.get(key, default=_MISSING)
        if val is _MISSING:
            raise KeyError(key)
        return val

    def __setitem__(self, key: Any, value: Any) -> None:
        self.set(key, value)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self.hits = 0
            self.misses = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "size": len(self._data),
                "maxsize": self.maxsize,
                "hits": self.hits,
                "misses": self.misses,
            }


_MISSING = object()


def _safe_q_mvar(p_kw: float | None, cos_val: float | None) -> float:
    if p_kw is None or p_kw <= 0.0:
        return 0.0
    if cos_val is None or cos_val <= 0.0:
        c = _DEFAULT_POWER_FACTOR
    else:
        c = max(_MIN_POWER_FACTOR, min(abs(float(cos_val)), _MAX_POWER_FACTOR))
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


def _solve_nr(net, *, warm_start: bool = False) -> bool:
    for init_mode in (("results", "auto") if warm_start else ("auto",)):
        try:
            pp.runpp(
                net,
                algorithm="nr",
                calculate_voltage_angles=True,
                init=init_mode,
                max_iteration=30,
                tolerance_mva=1e-8,
                numba=False,
            )
            if bool(net.converged):
                return True
        except Exception:
            continue
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


def _solve_hour_grid_inplace(
    profile: MineProfile,
    snap: dict[str, dict[str, dict[str, Any]]],
    cabinet: str,
    transformer_id: str,
    tap_grid: list[int],
    cap_grid: list[float],
    supply_bus: str,
    receiving_bus: str,
    voltage_cabinet: str,
    v_lo: float,
    v_hi: float,
    base_tap: int,
) -> tuple[dict[str, Any], dict[tuple[int, float], dict[str, Any]]]:
    """Evaluate all discrete (tap, qc) combinations for one hour by reusing a single pandapower net."""
    feeder = profile.feeder_map()[cabinet]
    max_cap = max(cap_grid) if cap_grid else 0.0
    net, meta = build_pandapower_network(
        profile,
        snap,
        shunt_target_bus=feeder.downstream_bus_id,
        shunt_qc_kvar=max(max_cap, 50.0),
    )
    trafo_pp = next(t["pp_idx"] for t in meta["trafo_meta"] if t["spec"].id == transformer_id)
    line_pp = next(l["pp_idx"] for l in meta["line_meta"] if l["spec"].cabinet == cabinet)
    supply_pp = meta["bus_idx"][supply_bus]
    recv_pp = meta["bus_idx"][receiving_bus]
    supply_vn = profile.bus_map()[supply_bus].nominal_kv
    recv_vn = profile.bus_map()[receiving_bus].nominal_kv
    shunt_idx = int(net.shunt.index[0]) if len(net.shunt) else None

    meas_u = snap.get(voltage_cabinet, {}).get("Uab", {}).get("value")
    meas_u = float(meas_u) if isinstance(meas_u, (int, float)) and math.isfinite(meas_u) else None
    p_meas = _val(snap, cabinet, "P", 0.0)
    cos_meas = _val(snap, cabinet, "cos", 0.85)
    q_load_est = _safe_q_mvar(p_meas, cos_meas) * 1000.0

    has_prev_res = False

    def _eval(tap: int, qc: float) -> dict[str, Any]:
        nonlocal has_prev_res
        net.trafo.at[trafo_pp, "tap_pos"] = tap
        if shunt_idx is not None:
            if qc > 0.0:
                net.shunt.at[shunt_idx, "in_service"] = True
                net.shunt.at[shunt_idx, "q_mvar"] = -qc / 1000.0
            else:
                net.shunt.at[shunt_idx, "in_service"] = False
                net.shunt.at[shunt_idx, "q_mvar"] = 0.0
        ok = _solve_nr(net, warm_start=has_prev_res)
        if not ok:
            has_prev_res = False
            return {"tap": tap, "qc_kvar": qc, "converged": False}
        has_prev_res = True
        br = net.res_line.loc[line_pp]
        p_kw = float(br.p_from_mw) * 1000.0
        q_kvar = float(br.q_from_mvar) * 1000.0
        u_sup = float(net.res_bus.at[supply_pp, "vm_pu"]) * supply_vn
        u_rec = float(net.res_bus.at[recv_pp, "vm_pu"]) * recv_vn
        loading = max(
            [float(v) for tbl in (net.res_line, net.res_trafo) for v in tbl.loading_percent],
            default=0.0,
        )
        loss_kw = float(net.res_line.pl_mw.sum() + net.res_trafo.pl_mw.sum()) * 1000.0
        pf = p_kw / math.hypot(p_kw, q_kvar) if p_kw > 0 else 1.0
        return {
            "tap": tap,
            "qc_kvar": qc,
            "converged": True,
            "bus_kv": u_sup,
            "recv_kv": u_rec,
            "feeder_pf": pf,
            "feeder_q_kvar": q_kvar,
            "max_loading_pct": loading,
            "loss_kw": loss_kw,
        }

    baseline = _eval(base_tap, 0.0)
    base_u = baseline["bus_kv"] if baseline["converged"] else None

    def _annotate(st: dict[str, Any]) -> dict[str, Any]:
        if not st["converged"]:
            st["projected_bus_kv"] = None
            st["violations"] = ["non_converged"]
            st["pass"] = False
            return st
        proj_u = (meas_u + st["bus_kv"] - base_u) if (meas_u is not None and base_u is not None) else st["bus_kv"]
        viols: list[str] = []
        if not (v_lo <= st["bus_kv"] <= v_hi):
            viols.append("modeled_bus_voltage")
        if not (v_lo <= proj_u <= v_hi):
            viols.append("projected_bus_voltage")
        if not (v_lo <= st["recv_kv"] <= v_hi):
            viols.append("receiving_bus_voltage")
        if st["max_loading_pct"] > 100.0:
            viols.append("thermal_limit")
        if st["feeder_q_kvar"] < -1e-6:
            viols.append("leading_feeder_q")
        st["projected_bus_kv"] = proj_u
        st["violations"] = viols
        st["pass"] = len(viols) == 0
        return st

    _annotate(baseline)
    grid: dict[tuple[int, float], dict[str, Any]] = {}
    for tap in tap_grid:
        for qc in cap_grid:
            # Fast prune capacitors grossly larger than reactive load (guaranteed reverse Q)
            if qc > 0.0 and qc > q_load_est + 95.0:
                continue
            if tap == base_tap and qc == 0.0:
                grid[(tap, qc)] = baseline
            else:
                grid[(tap, qc)] = _annotate(_eval(tap, qc))
    return baseline, grid


_DAILY_SCHEDULE_CACHE = _BoundedLRUCache(maxsize=64, ttl_seconds=3600.0)
_DAY_GRID_CACHE = _BoundedLRUCache(maxsize=16, ttl_seconds=3600.0)
_WEEKLY_PARETO_CACHE = _BoundedLRUCache(maxsize=16, ttl_seconds=3600.0)


def _get_day_hourly_grids(
    archive,
    prof: MineProfile,
    day: str,
    day_sha: str,
    target_cabinet: str,
    transformer_id: str,
    tap_grid: list[int],
    cap_grid: list[float],
    supply_bus: str,
    receiving_bus: str,
    voltage_cabinet: str,
    v_lo: float,
    v_hi: float,
    base_tap: int,
):
    grid_key = (prof.mine_id, day, day_sha, target_cabinet, tuple(tap_grid), tuple(cap_grid))
    if day_sha:
        cached_bundle = _DAY_GRID_CACHE.get(grid_key)
        if cached_bundle is not None:
            return cached_bundle

    hour_rows = archive.db.execute(
        """
        SELECT DISTINCT time FROM reading
        WHERE station = ? AND day = ?
        ORDER BY time ASC
        """,
        (prof.primary_station, day),
    ).fetchall()
    hours = [row["time"] for row in hour_rows]
    if not hours:
        return [], [], [], []

    hourly_baselines: list[dict[str, Any]] = []
    hourly_grids: list[dict[tuple[int, float], dict[str, Any]]] = []
    hourly_snaps: list[dict[str, dict[str, dict[str, Any]]]] = []
    for at_time in hours:
        snap = _load_hour_snapshot(archive, at_time, prof.primary_station)
        base_st, grid = _solve_hour_grid_inplace(
            prof, snap, target_cabinet, transformer_id,
            tap_grid, cap_grid, supply_bus, receiving_bus,
            voltage_cabinet, v_lo, v_hi, base_tap,
        )
        hourly_baselines.append(base_st)
        hourly_grids.append(grid)
        hourly_snaps.append(snap)

    bundle = (hours, hourly_baselines, hourly_grids, hourly_snaps)
    if day_sha:
        _DAY_GRID_CACHE.set(grid_key, bundle)
    return bundle


def optimize_daily_tap_cap_schedule(
    archive,
    day: str,
    *,
    target_cabinet: str = "1010",
    cap_step_kvar: float = 50.0,
    max_cap_kvar: float = 400.0,
    max_tap_changes: int = 2,
    max_cap_changes: int = 4,
    pf_target: float = 0.90,
    profile: MineProfile | None = None,
) -> dict[str, Any]:
    """24-hour discrete tap & capacitor switching optimization via exact finite-horizon DP on pandapower."""
    from copy import deepcopy

    prof = profile or load_mine_profile()
    day_row = archive.db.execute("SELECT sha256 FROM report WHERE day = ?", (day,)).fetchone()
    day_sha = str(day_row["sha256"]) if day_row else ""
    cache_key = (
        prof.mine_id, day, day_sha, target_cabinet,
        round(float(cap_step_kvar), 3), round(float(max_cap_kvar), 3),
        int(max_tap_changes), int(max_cap_changes), round(float(pf_target), 4),
    )
    if day_sha:
        cached_daily = _DAILY_SCHEDULE_CACHE.get(cache_key)
        if cached_daily is not None:
            return deepcopy(cached_daily)
    feeder = prof.feeder_map().get(target_cabinet)
    if not feeder or not feeder.downstream_bus_id:
        raise ValueError(f"目标柜 {target_cabinet} 缺少下游等值潮流支路模型。")
    transformer = next((tr for tr in prof.transformers if tr.lv_bus == feeder.bus_id), None)
    if not transformer:
        raise ValueError(f"未找到向母线 {feeder.bus_id} 供电的主变配置。")

    supply_bus = feeder.bus_id
    receiving_bus = feeder.downstream_bus_id
    voltage_cabinet = prof.bus_map()[supply_bus].reference_cabinet or transformer.lv_cabinet
    v_lim = prof.voltage_limit_for_kv(prof.bus_map()[supply_bus].nominal_kv)
    v_lo, v_hi = v_lim.lo_kv, v_lim.hi_kv

    base_tap = transformer.calibrated_tap_pos
    tap_grid = sorted({
        t for t in range(max(transformer.tap_min, base_tap - 1), min(transformer.tap_max, base_tap + 1) + 1)
    })
    steps_n = max(1, int(round(max_cap_kvar / max(cap_step_kvar, 10.0))))
    cap_grid = [round(i * cap_step_kvar, 1) for i in range(steps_n + 1)]

    hours, hourly_baselines, hourly_grids, hourly_snaps = _get_day_hourly_grids(
        archive, prof, day, day_sha, target_cabinet, transformer.id,
        tap_grid, cap_grid, supply_bus, receiving_bus, voltage_cabinet,
        v_lo, v_hi, base_tap,
    )
    if not hours:
        return {"status": "missing_day", "day": day, "summary": f"归档库中缺少 {day} 的小时级数据。"}

    # Exact DP over (tap, qc, tap_changes_used, cap_changes_used)
    INF = 1e18
    TAP_SW_PENALTY = 0.08
    CAP_SW_PENALTY = 0.02
    VIOL_PENALTY = 10000.0

    def _hour_cost(st: dict[str, Any]) -> float:
        if not st.get("converged"):
            return VIOL_PENALTY * 5.0
        cost = float(st["loss_kw"])
        if not st["pass"]:
            cost += VIOL_PENALTY * max(1, len(st["violations"]))
        if st["feeder_pf"] < pf_target:
            cost += 3.0 * (pf_target - st["feeder_pf"])
        return cost

    dp_prev: dict[tuple[int, float, int, int], float] = {}
    backptr: list[dict[tuple[int, float, int, int], tuple[int, float, int, int] | None]] = []

    bp0: dict[tuple[int, float, int, int], tuple[int, float, int, int] | None] = {}
    for (tap, qc), st in hourly_grids[0].items():
        # Starting at hour 0 from calibrated tap / 0 kvar avoids hidden pre-day tap jump if equal
        key = (tap, qc, 0, 0)
        init_bias = 0.01 * (abs(tap - base_tap) + (1 if qc > 0 else 0))
        dp_prev[key] = _hour_cost(st) + init_bias
        bp0[key] = None
    backptr.append(bp0)

    for h_idx in range(1, len(hours)):
        dp_cur: dict[tuple[int, float, int, int], float] = {}
        bp_h: dict[tuple[int, float, int, int], tuple[int, float, int, int] | None] = {}
        grid_h = hourly_grids[h_idx]
        # Precompute per-hour state cost once
        h_costs = {k: _hour_cost(st) for k, st in grid_h.items()}
        for (p_tap, p_qc, used_t, used_c), prev_cost in dp_prev.items():
            for (c_tap, c_qc), scost in h_costs.items():
                dt = 1 if c_tap != p_tap else 0
                dc = 1 if c_qc != p_qc else 0
                nt = used_t + dt
                nc = used_c + dc
                if nt > max_tap_changes or nc > max_cap_changes:
                    continue
                tot = prev_cost + scost + dt * TAP_SW_PENALTY + dc * CAP_SW_PENALTY
                nkey = (c_tap, c_qc, nt, nc)
                if tot < dp_cur.get(nkey, INF):
                    dp_cur[nkey] = tot
                    bp_h[nkey] = (p_tap, p_qc, used_t, used_c)
        dp_prev = dp_cur
        backptr.append(bp_h)

    best_end = min(dp_prev.items(), key=lambda item: item[1])[0]
    chosen_keys: list[tuple[int, float, int, int]] = [best_end]
    cur = best_end
    for h_idx in range(len(hours) - 1, 0, -1):
        prev = backptr[h_idx][cur]
        assert prev is not None
        chosen_keys.append(prev)
        cur = prev
    chosen_keys.reverse()

    schedule_hours: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    base_loss_kwh = 0.0
    opt_loss_kwh = 0.0
    base_viols = 0
    opt_viols = 0

    for idx, (at_time, key) in enumerate(zip(hours, chosen_keys)):
        tap, qc, _, _ = key
        base_st = hourly_baselines[idx]
        opt_st = hourly_grids[idx][(tap, qc)]
        snap = hourly_snaps[idx]
        base_loss_kwh += float(base_st.get("loss_kw") or 0.0)
        opt_loss_kwh += float(opt_st.get("loss_kw") or 0.0)
        if not base_st.get("pass"):
            base_viols += 1
        if not opt_st.get("pass"):
            opt_viols += 1

        if idx > 0:
            p_tap, p_qc, _, _ = chosen_keys[idx - 1]
            if tap != p_tap:
                actions.append({
                    "hour": at_time,
                    "type": "tap_change",
                    "from_val": f"{p_tap:+d} 档",
                    "to_val": f"{tap:+d} 档",
                    "reason": (
                        f"母线实测 {_val(snap, voltage_cabinet, 'Uab', 0.0):.2f} kV，"
                        f"调档后模型母线 {opt_st['bus_kv']:.4f} kV（投影 {opt_st['projected_bus_kv']:.4f} kV）"
                    ),
                })
            if qc != p_qc:
                actions.append({
                    "hour": at_time,
                    "type": "cap_switch",
                    "from_val": f"{p_qc:g} kvar",
                    "to_val": f"{qc:g} kvar",
                    "reason": (
                        f"{target_cabinet} 有功 {_val(snap, target_cabinet, 'P', 0.0):.1f} kW、"
                        f"实测 cos {_val(snap, target_cabinet, 'cos', 0.0):.2f}，"
                        f"投切后首端 cos {opt_st['feeder_pf']:.4f}、无功 {opt_st['feeder_q_kvar']:.1f} kvar"
                    ),
                })

        cells = [
            c for c in (
                _cell(snap, target_cabinet, "P"),
                _cell(snap, target_cabinet, "cos"),
                _cell(snap, voltage_cabinet, "Uab"),
            ) if c
        ]
        schedule_hours.append({
            "hour": at_time,
            "tap": tap,
            "qc_kvar": qc,
            "baseline_bus_kv": round(float(base_st["bus_kv"]), 4) if base_st.get("converged") else None,
            "optimized_bus_kv": round(float(opt_st["bus_kv"]), 4) if opt_st.get("converged") else None,
            "projected_bus_kv": round(float(opt_st["projected_bus_kv"]), 4) if opt_st.get("projected_bus_kv") is not None else None,
            "optimized_pf": round(float(opt_st["feeder_pf"]), 4) if opt_st.get("converged") else None,
            "feeder_q_kvar": round(float(opt_st["feeder_q_kvar"]), 2) if opt_st.get("converged") else None,
            "baseline_loss_kw": round(float(base_st["loss_kw"]), 3) if base_st.get("converged") else None,
            "optimized_loss_kw": round(float(opt_st["loss_kw"]), 3) if opt_st.get("converged") else None,
            "pass": bool(opt_st.get("pass")),
            "violations": list(opt_st.get("violations") or []),
            "cells": cells,
        })

    tap_changes_used = best_end[2]
    cap_changes_used = best_end[3]
    base_loss_kwh = round(base_loss_kwh, 3)
    opt_loss_kwh = round(opt_loss_kwh, 3)
    loss_saving_kwh = round(base_loss_kwh - opt_loss_kwh, 3)
    loss_saving_pct = round(loss_saving_kwh / max(base_loss_kwh, 1e-6) * 100.0, 2)
    all_safe = opt_viols == 0
    action_brief = (
        "；".join(f'{a["hour"][11:16]} {a["from_val"]}→{a["to_val"]}' for a in actions)
        if actions else f'全天维持 {chosen_keys[0][0]:+d} 档 + {chosen_keys[0][1]:g} kvar'
    )
    summary = (
        f'{day} 24 小时分时离散优化（{transformer.id} + {target_cabinet} 柜 {cap_step_kvar:g} kvar 分级，'
        f'约束：分接头 ≤{max_tap_changes} 次/日、电容 ≤{max_cap_changes} 次/日）：'
        f'违规整点由基线 {base_viols}/{len(hours)} 降至 {opt_viols}/{len(hours)}'
        f'（{"全天 24h 零越限、零无功倒送" if all_safe else "仍有约束冲突待现场补证"}），'
        f'实际动作分接头 {tap_changes_used} 次、电容 {cap_changes_used} 次（{action_brief}），'
        f'24h 等值总网损由 {base_loss_kwh:.2f} 降至 {opt_loss_kwh:.2f} kWh（节电 {loss_saving_kwh:+.2f} kWh / {loss_saving_pct:+.2f}%）。'
    )
    out = {
        "status": "ok",
        "day": day,
        "target_cabinet": target_cabinet,
        "transformer": transformer.id,
        "supply_bus": supply_bus,
        "receiving_bus": receiving_bus,
        "cap_step_kvar": cap_step_kvar,
        "max_tap_changes": max_tap_changes,
        "max_cap_changes": max_cap_changes,
        "tap_changes": tap_changes_used,
        "cap_changes": cap_changes_used,
        "initial_tap": chosen_keys[0][0],
        "initial_qc_kvar": chosen_keys[0][1],
        "all_hours_safe": all_safe,
        "baseline_violations_count": base_viols,
        "optimized_violations_count": opt_viols,
        "baseline_loss_kwh": base_loss_kwh,
        "optimized_loss_kwh": opt_loss_kwh,
        "loss_saving_kwh": loss_saving_kwh,
        "loss_saving_pct": loss_saving_pct,
        "actions": actions,
        "hours": schedule_hours,
        "summary": summary,
        "model_assumptions": prof.model_assumption_disclaimer,
    }
    if day_sha:
        _DAILY_SCHEDULE_CACHE.set(cache_key, deepcopy(out))
    return out


def analyze_weekly_schedule_pareto(
    archive,
    *,
    target_cabinet: str = "1010",
    pareto_day: str = "2026-11-13",
    cap_step_kvar: float = 50.0,
    max_cap_kvar: float = 400.0,
    profile: MineProfile | None = None,
) -> dict[str, Any]:
    """Run 7-day (168h) discrete tap/cap optimization and switching-budget Pareto frontier on `pareto_day`."""
    from copy import deepcopy

    prof = profile or load_mine_profile()
    rep_rows = archive.db.execute("SELECT day, sha256 FROM report ORDER BY day ASC").fetchall()
    days = [row["day"] for row in rep_rows]
    archive_sig = tuple((row["day"], row["sha256"]) for row in rep_rows)
    if pareto_day not in days and days:
        pareto_day = days[0]

    wkey = (prof.mine_id, archive_sig, target_cabinet, pareto_day, round(float(cap_step_kvar), 3), round(float(max_cap_kvar), 3))
    cached_weekly = _WEEKLY_PARETO_CACHE.get(wkey)
    if cached_weekly is not None:
        return deepcopy(cached_weekly)

    weekly_days: list[dict[str, Any]] = []
    featured_schedule: dict[str, Any] | None = None
    total_hours = 0
    base_viols_tot = 0
    opt_viols_tot = 0
    base_loss_tot = 0.0
    opt_loss_tot = 0.0

    for d in days:
        sched = optimize_daily_tap_cap_schedule(
            archive,
            d,
            target_cabinet=target_cabinet,
            cap_step_kvar=cap_step_kvar,
            max_cap_kvar=max_cap_kvar,
            max_tap_changes=2,
            max_cap_changes=4,
            profile=prof,
        )
        if sched.get("status") != "ok":
            continue
        if d == pareto_day:
            featured_schedule = sched
        total_hours += len(sched["hours"])
        base_viols_tot += sched["baseline_violations_count"]
        opt_viols_tot += sched["optimized_violations_count"]
        base_loss_tot += sched["baseline_loss_kwh"]
        opt_loss_tot += sched["optimized_loss_kwh"]
        weekly_days.append({
            "day": d,
            "all_hours_safe": sched["all_hours_safe"],
            "baseline_violations_count": sched["baseline_violations_count"],
            "optimized_violations_count": sched["optimized_violations_count"],
            "tap_changes": sched["tap_changes"],
            "cap_changes": sched["cap_changes"],
            "baseline_loss_kwh": sched["baseline_loss_kwh"],
            "optimized_loss_kwh": sched["optimized_loss_kwh"],
            "loss_saving_kwh": sched["loss_saving_kwh"],
            "loss_saving_pct": sched["loss_saving_pct"],
            "actions_count": len(sched["actions"]),
        })

    base_loss_tot = round(base_loss_tot, 3)
    opt_loss_tot = round(opt_loss_tot, 3)
    saving_tot = round(base_loss_tot - opt_loss_tot, 3)
    saving_pct = round(saving_tot / max(base_loss_tot, 1e-6) * 100.0, 2)

    pareto_budgets = [
        ("固定运行 (0/0)", 0, 0),
        ("仅调档 1 次 (1/0)", 1, 0),
        ("低频分时 (1/2)", 1, 2),
        ("标准分时 (1/4)", 1, 4),
        ("均衡分时 (2/4)", 2, 4),
        ("高频分时 (2/6)", 2, 6),
    ]
    pareto_frontier: list[dict[str, Any]] = []
    if pareto_day in days:
        for label, mt, mc in pareto_budgets:
            p_sched = optimize_daily_tap_cap_schedule(
                archive,
                pareto_day,
                target_cabinet=target_cabinet,
                cap_step_kvar=cap_step_kvar,
                max_cap_kvar=max_cap_kvar,
                max_tap_changes=mt,
                max_cap_changes=mc,
                profile=prof,
            )
            pareto_frontier.append({
                "label": label,
                "max_tap_changes": mt,
                "max_cap_changes": mc,
                "tap_changes": p_sched["tap_changes"],
                "cap_changes": p_sched["cap_changes"],
                "violations": p_sched["optimized_violations_count"],
                "all_hours_safe": p_sched["all_hours_safe"],
                "loss_kwh": p_sched["optimized_loss_kwh"],
                "loss_saving_kwh": p_sched["loss_saving_kwh"],
                "loss_saving_pct": p_sched["loss_saving_pct"],
            })

    out = {
        "status": "ok",
        "target_cabinet": target_cabinet,
        "pareto_day": pareto_day,
        "weekly_days": weekly_days,
        "weekly_summary": {
            "total_days": len(weekly_days),
            "total_hours": total_hours,
            "baseline_violations_total": base_viols_tot,
            "optimized_violations_total": opt_viols_tot,
            "total_baseline_loss_kwh": base_loss_tot,
            "total_optimized_loss_kwh": opt_loss_tot,
            "total_loss_saving_kwh": saving_tot,
            "total_loss_saving_pct": saving_pct,
            "all_week_safe": opt_viols_tot == 0,
        },
        "pareto_frontier": pareto_frontier,
        "featured_day_schedule": featured_schedule,
        "model_assumptions": prof.model_assumption_disclaimer,
    }
    _WEEKLY_PARETO_CACHE.set(wkey, deepcopy(out))
    return out


_SENSITIVITY_VOI_CACHE = _BoundedLRUCache(maxsize=16, ttl_seconds=3600.0)


def analyze_parameter_sensitivity_voi(
    archive,
    *,
    at_time: str = "2026-11-13 17:00:00",
    target_cabinet: str = "1010",
    n1_time: str = "2026-11-10 06:00:00",
    profile: MineProfile | None = None,
) -> dict[str, Any]:
    """Quantify impact of unverified equivalent parameters via pandapower perturbation & rank VoI."""
    from copy import deepcopy

    prof = profile or load_mine_profile()
    rep_rows = archive.db.execute("SELECT day, sha256 FROM report ORDER BY day ASC").fetchall()
    archive_sig = tuple((row["day"], row["sha256"]) for row in rep_rows)
    skey = (prof.mine_id, archive_sig, at_time, target_cabinet, n1_time)
    cached_voi = _SENSITIVITY_VOI_CACHE.get(skey)
    if cached_voi is not None:
        return deepcopy(cached_voi)

    tr2 = next((t for t in prof.transformers if t.id == "T2"), prof.transformers[-1])
    fd_target = prof.feeder_map().get(target_cabinet) or next(f for f in prof.feeders if f.downstream_bus_id)
    base_tap = int(tr2.calibrated_tap_pos)
    base_vk = float(tr2.default_vk_percent)
    base_sn = float(tr2.default_sn_mva)
    base_len = float(fd_target.default_cable_length_km or 1.8)

    # Fetch real metering scale ratio median for target cabinet from archived readings
    from laowadi.diag_tools import DiagnosticToolbox

    tb = DiagnosticToolbox(archive)
    prof_cab = tb.execute("get_cabinet_health_profile", {"cabinet": target_cabinet})
    m_chk = (prof_cab.get("metering_check") or {}) if prof_cab.get("status") == "ok" else {}
    obs_ratio = float(m_chk.get("ratio_median") or 0.6606)

    # Baseline runs at voltage-critical hour (`at_time`) and N-1 peak hour (`n1_time`)
    base_v = run_ac_power_flow(archive, scenario="base_case", at_time=at_time, profile=prof)
    base_n1 = run_ac_power_flow(
        archive, scenario="n1_contingency", outage_mode="feeder_1009", at_time=n1_time, profile=prof,
    )
    b_u_ii = float(base_v["network_summary"]["bus_10kv_ii_kv"])
    b_loss = float(base_v["network_summary"]["total_loss_kw"])
    b_n1_comp = (base_n1.get("n1_contingency") or {}).get("receiving_1010_comparison") or {}
    b_n1_i = float(b_n1_comp.get("after_i_a") or 0.0)
    b_n1_ug = float(b_n1_comp.get("min_underground_kv") or 0.0)

    def _eval_case(**kwargs) -> tuple[float, float, float, float]:
        rv = run_ac_power_flow(archive, scenario="base_case", at_time=at_time, profile=prof, **kwargs)
        rn = run_ac_power_flow(
            archive, scenario="n1_contingency", outage_mode="feeder_1009", at_time=n1_time, profile=prof, **kwargs,
        )
        u_ii = float(rv["network_summary"]["bus_10kv_ii_kv"])
        loss = float(rv["network_summary"]["total_loss_kw"])
        nc = (rn.get("n1_contingency") or {}).get("receiving_1010_comparison") or {}
        ni = float(nc.get("after_i_a") or b_n1_i)
        nug = float(nc.get("min_underground_kv") or b_n1_ug)
        return u_ii, ni, loss, nug

    def _eval_metering_scale(scale_factor: float) -> tuple[float, float, float, float]:
        # Re-solve pandapower with target cabinet load scaled by 1/scale_factor to reflect CT ratio uncertainty
        snap_v = _load_hour_snapshot(archive, at_time, prof.primary_station)
        snap_n = _load_hour_snapshot(archive, n1_time, prof.primary_station)
        mult = 1.0 / max(scale_factor, 0.4)

        def _scaled_snap(snap: dict) -> dict:
            out_s = {cab: {m: dict(v) for m, v in metrics.items()} for cab, metrics in snap.items()}
            if target_cabinet in out_s and "P" in out_s[target_cabinet] and out_s[target_cabinet]["P"].get("value") is not None:
                out_s[target_cabinet]["P"]["value"] = float(out_s[target_cabinet]["P"]["value"]) * mult
            if "1006" in out_s and "P" in out_s["1006"] and out_s["1006"]["P"].get("value") is not None:
                delta_p = float(snap.get(target_cabinet, {}).get("P", {}).get("value") or 0.0) * (mult - 1.0)
                out_s["1006"]["P"]["value"] = float(out_s["1006"]["P"]["value"]) + delta_p
            return out_s

        net_v, meta_v = build_pandapower_network(prof, _scaled_snap(snap_v))
        _solve_nr(net_v)
        sol_v = _extract_solution(net_v, meta_v, prof, snap_v)
        u_ii = float(sol_v["network_summary"]["bus_10kv_ii_kv"])
        loss = float(sol_v["network_summary"]["total_loss_kw"])

        net_n, meta_n = build_pandapower_network(prof, _scaled_snap(snap_n), outage_mode="feeder_1009")
        _solve_nr(net_n)
        sol_n = _extract_solution(net_n, meta_n, prof, snap_n)
        l_rec = next((l for l in sol_n["lines"] if l["cabinet"] == target_cabinet), None)
        ni = float(l_rec["i_from_a"]) if l_rec else b_n1_i
        nug = float(sol_n["network_summary"]["min_underground_kv"] or b_n1_ug)
        return u_ii, ni, loss, nug

    specs = [
        {
            "param_key": "transformer_tap_pos",
            "label": "2# 主变分接头实际档位",
            "unit": "档",
            "base_value": base_tap,
            "low_value": max(tr2.tap_min, base_tap - 1),
            "high_value": min(tr2.tap_max, base_tap + 1),
            "perturbation_label": f"{max(tr2.tap_min, base_tap - 1):+d} 档 ～ {min(tr2.tap_max, base_tap + 1):+d} 档（±1 档）",
            "low_eval": lambda: _eval_case(tr2_tap_pos=max(tr2.tap_min, base_tap - 1)),
            "high_eval": lambda: _eval_case(tr2_tap_pos=min(tr2.tap_max, base_tap + 1)),
            "required_field_document": "2# 主变铭牌及同期分接开关实际档位台账",
            "rationale": "直接决定 10 kV II 段母线电压是否处于 GB/T 12325 限值带内以及调压裕度。",
        },
        {
            "param_key": "metering_ratio",
            "label": f"{target_cabinet} 柜计量/CT 比例系数",
            "unit": "倍",
            "base_value": 1.0,
            "low_value": round(obs_ratio, 4),
            "high_value": 1.0,
            "perturbation_label": f"观测比例 {obs_ratio:.4f} ～ 标称 1.00",
            "low_eval": lambda: _eval_metering_scale(obs_ratio),
            "high_eval": lambda: (b_u_ii, b_n1_i, b_loss, b_n1_ug),
            "required_field_document": f"{target_cabinet} 柜 CT 变比、计量接线核校报告与试验记录",
            "rationale": "决定有功驱动潮流与实测电流包络之间的口径偏差，是下井双回 N-1 载流安全裁决的主导变量。",
        },
        {
            "param_key": "cable_length_km",
            "label": "1009/1010 下井电缆长度",
            "unit": "km",
            "base_value": round(base_len, 2),
            "low_value": round(base_len * 0.70, 2),
            "high_value": round(base_len * 1.30, 2),
            "perturbation_label": f"{base_len * 0.70:.2f} ～ {base_len * 1.30:.2f} km（±30%）",
            "low_eval": lambda: _eval_case(cable_length_km=round(base_len * 0.70, 3)),
            "high_eval": lambda: _eval_case(cable_length_km=round(base_len * 1.30, 3)),
            "required_field_document": "下井双回电缆型号、截面（mm²）、敷设长度及井筒降容台账",
            "rationale": "直接控制井下 1535 中央变电所受端压降与线路铜损测算精度。",
        },
        {
            "param_key": "transformer_vk_percent",
            "label": "1#/2# 主变短路阻抗 Uk%",
            "unit": "%",
            "base_value": round(base_vk, 2),
            "low_value": round(base_vk * 0.80, 2),
            "high_value": round(base_vk * 1.20, 2),
            "perturbation_label": f"{base_vk * 0.80:.1f}% ～ {base_vk * 1.20:.1f}%（±20%）",
            "low_eval": lambda: _eval_case(transformer_vk_percent=round(base_vk * 0.80, 3)),
            "high_eval": lambda: _eval_case(transformer_vk_percent=round(base_vk * 1.20, 3)),
            "required_field_document": "1#/2# 主变出厂试验报告（实测短路阻抗 Uk% 与空载/负载损耗）",
            "rationale": "影响重载及单进线带双主变工况下的主变内部阻抗压降。",
        },
        {
            "param_key": "transformer_sn_mva",
            "label": "1#/2# 主变额定容量 Sn",
            "unit": "MVA",
            "base_value": round(base_sn, 2),
            "low_value": round(base_sn * 0.75, 2),
            "high_value": round(base_sn * 1.25, 2),
            "perturbation_label": f"{base_sn * 0.75:.1f} ～ {base_sn * 1.25:.1f} MVA（±25%）",
            "low_eval": lambda: _eval_case(transformer_sn_mva=round(base_sn * 0.75, 3)),
            "high_eval": lambda: _eval_case(transformer_sn_mva=round(base_sn * 1.25, 3)),
            "required_field_document": "1#/2# 主变铭牌额定容量与冷却方式核定单",
            "rationale": "影响主变标幺阻抗及单进线/N-1 负载率热稳定判定基准。",
        },
    ]

    items: list[dict[str, Any]] = []
    for sp in specs:
        lu, li, ll, lnug = sp["low_eval"]()
        hu, hi, hl, hnug = sp["high_eval"]()
        d_u_low = round(lu - b_u_ii, 4)
        d_u_high = round(hu - b_u_ii, 4)
        v_span = round(max(abs(lu - hu), abs(lnug - hnug)), 4)

        d_i_low = round(li - b_n1_i, 2)
        d_i_high = round(hi - b_n1_i, 2)
        i_span = round(abs(li - hi), 2)

        d_l_low = round(ll - b_loss, 3)
        d_l_high = round(hl - b_loss, 3)
        l_span = round(abs(ll - hl), 3)

        v_norm = min(1.0, v_span / 0.50)
        i_norm = min(1.0, i_span / 45.0)
        l_norm = min(1.0, l_span / 15.0)
        voi_score = round((0.45 * v_norm + 0.40 * i_norm + 0.15 * l_norm) * 100.0, 1)

        if i_norm >= v_norm and i_norm >= l_norm and i_span > 1.0:
            dominant = f"N-1 受电电流跨度 {i_span:.2f} A"
        elif v_norm >= l_norm:
            dominant = f"母线/末端电压跨度 {v_span:.4f} kV"
        else:
            dominant = f"系统总网损跨度 {l_span:.3f} kW"

        items.append({
            "param_key": sp["param_key"],
            "label": sp["label"],
            "unit": sp["unit"],
            "base_value": sp["base_value"],
            "low_value": sp["low_value"],
            "high_value": sp["high_value"],
            "perturbation_label": sp["perturbation_label"],
            "low_delta_u_kv": d_u_low,
            "high_delta_u_kv": d_u_high,
            "voltage_span_kv": v_span,
            "low_delta_n1_i_a": d_i_low,
            "high_delta_n1_i_a": d_i_high,
            "n1_current_span_a": i_span,
            "low_delta_loss_kw": d_l_low,
            "high_delta_loss_kw": d_l_high,
            "loss_span_kw": l_span,
            "voi_score": voi_score,
            "dominant_impact": dominant,
            "required_field_document": sp["required_field_document"],
            "rationale": sp["rationale"],
        })

    items.sort(key=lambda r: (-r["voi_score"], -r["voltage_span_kv"], -r["n1_current_span_a"]))
    voi_ranking = []
    for rank, item in enumerate(items, 1):
        item["priority_rank"] = rank
        voi_ranking.append({
            "rank": rank,
            "param_key": item["param_key"],
            "label": item["label"],
            "voi_score": item["voi_score"],
            "dominant_impact": item["dominant_impact"],
            "required_field_document": item["required_field_document"],
            "rationale": item["rationale"],
        })

    top1, top2 = voi_ranking[0], voi_ranking[1]
    summary = (
        f"参数不确定性与信息价值（VoI）排序（电压断面 {at_time}、N-1 峰值断面 {n1_time}）："
        f"最优先核查「{top1['label']}」（VoI={top1['voi_score']:.1f}，{top1['dominant_impact']}），"
        f"其次为「{top2['label']}」（VoI={top2['voi_score']:.1f}，{top2['dominant_impact']}）；"
        f"建议按上述顺序向现场索要铭牌与核校台账以收窄安全边界。"
    )
    out = {
        "status": "ok",
        "at_time": at_time,
        "n1_time": n1_time,
        "target_cabinet": target_cabinet,
        "baseline": {
            "bus_10kv_ii_kv": round(b_u_ii, 4),
            "n1_min_ug_kv": round(b_n1_ug, 4),
            "n1_current_a": round(b_n1_i, 2),
            "total_loss_kw": round(b_loss, 3),
        },
        "tornado_items": items,
        "voi_ranking": voi_ranking,
        "summary": summary,
        "model_assumptions": prof.model_assumption_disclaimer,
    }
    _SENSITIVITY_VOI_CACHE.set(skey, deepcopy(out))
    return out
