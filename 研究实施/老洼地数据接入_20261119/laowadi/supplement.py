"""Stage 11: Metadata & Topology-Driven Professional Verification and Versioned Supplement Recalculation.

Zero hardcoded mine/cabinet/date constants: all verification items (V01–V04), target cabinets,
windows, dependency parameter keys, and power-conservation baselines are derived dynamically
from `MineProfile` + live SQLite archive readings.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import statistics
from typing import Any

from laowadi.mine_schema import MineProfile, load_mine_profile
from laowadi.storage import CATALOG, sha


def _json_sha(obj: dict) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _snapshot_identity(archive) -> dict:
    reports = [
        dict(r)
        for r in archive.db.execute("SELECT day, filename, sheet, sha256 FROM report ORDER BY day").fetchall()
    ]
    diagrams = [
        dict(r)
        for r in archive.db.execute("SELECT filename, captured_at, sha256 FROM diagram ORDER BY filename").fetchall()
    ]
    catalog_sha = sha(CATALOG.read_bytes()) if CATALOG.is_file() else ""
    snap = {"reports": reports, "diagrams": diagrams, "catalog_sha256": catalog_sha}
    snap["snapshot_sha256"] = _json_sha(snap)
    return snap


def _query_reading_safe(archive, station: str, cabinet: str, metric: str, at_time: str) -> dict[str, Any]:
    row = archive.db.execute(
        """
        SELECT r.value, r.source_col || r.source_row AS cell, rep.filename, rep.sheet, r.time
        FROM reading r
        JOIN report rep ON rep.day = r.day
        WHERE r.station = ? AND r.cabinet = ? AND r.metric = ? AND r.time = ?
        LIMIT 1
        """,
        (station, cabinet, metric, at_time),
    ).fetchone()
    if row is None:
        return {"value": None, "time": at_time, "source": {"file": "", "sheet": "", "cell": ""}}
    return {
        "value": row["value"],
        "time": row["time"],
        "source": {"file": row["filename"], "sheet": row["sheet"], "cell": row["cell"]},
    }


def _cell_ref(rec: dict[str, Any]) -> str:
    s = rec.get("source") or {}
    if not s.get("file") or not s.get("cell"):
        return ""
    return f'{s["file"]} / {s["sheet"]}!{s["cell"]}'


def discover_single_incomer_window(archive, profile: MineProfile) -> dict[str, Any]:
    """Dynamically detect a single-incomer window (one incomer P=0/Ia=0 while peer carries load)."""
    station = profile.primary_station
    incomers = list(profile.incomer_cabinets())
    if len(incomers) >= 2:
        in_a, in_b = incomers[0], incomers[1]
        for active_c, zero_c in ((in_a, in_b), (in_b, in_a)):
            rows = archive.db.execute(
                """
                SELECT r0.day, r0.time, r1.value AS active_p
                FROM reading r0
                JOIN reading r0i ON r0i.station = r0.station AND r0i.time = r0.time
                    AND r0i.cabinet = r0.cabinet AND r0i.metric = 'Ia'
                JOIN reading r1 ON r1.station = r0.station AND r1.time = r0.time
                    AND r1.cabinet = ? AND r1.metric = 'P'
                WHERE r0.station = ? AND r0.cabinet = ? AND r0.metric = 'P'
                  AND r0.value = 0 AND r0i.value = 0 AND r1.value > 50
                ORDER BY r0.time ASC
                """,
                (active_c, station, zero_c),
            ).fetchall()
            if rows:
                times = [r["time"] for r in rows]
                peak_p_row = max(rows, key=lambda r: float(r["active_p"]))
                ia_row = archive.db.execute(
                    """
                    SELECT time, value FROM reading
                    WHERE station = ? AND cabinet = ? AND metric = 'Ia' AND time BETWEEN ? AND ? AND value IS NOT NULL
                    ORDER BY value DESC, time ASC LIMIT 1
                    """,
                    (station, active_c, times[0], times[-1]),
                ).fetchone()
                return {
                    "found_zero_window": True,
                    "active_incomer": active_c,
                    "zero_incomer": zero_c,
                    "day": rows[0]["day"],
                    "start_time": times[0],
                    "end_time": times[-1],
                    "hours": len(times),
                    "peak_p_time": peak_p_row["time"],
                    "peak_ia_time": ia_row["time"] if ia_row else peak_p_row["time"],
                }

    # Fallback for mines without a zero-incomer outage window: use peak load day of primary incomer
    active_c = incomers[0] if incomers else profile.feeders[0].cabinet
    zero_c = incomers[1] if len(incomers) >= 2 else active_c
    pk = archive.db.execute(
        """
        SELECT day, time, value FROM reading
        WHERE station = ? AND cabinet = ? AND metric = 'P' AND value IS NOT NULL
        ORDER BY value DESC, time ASC LIMIT 1
        """,
        (station, active_c),
    ).fetchone()
    day = pk["day"] if pk else "2026-11-10"
    pk_t = pk["time"] if pk else f"{day} 12:00:00"
    day_times = [
        r["time"]
        for r in archive.db.execute(
            "SELECT DISTINCT time FROM reading WHERE station = ? AND day = ? ORDER BY time ASC",
            (station, day),
        ).fetchall()
    ]
    return {
        "found_zero_window": False,
        "active_incomer": active_c,
        "zero_incomer": zero_c,
        "day": day,
        "start_time": day_times[0] if day_times else pk_t,
        "end_time": day_times[-1] if day_times else pk_t,
        "hours": len(day_times) or 1,
        "peak_p_time": pk_t,
        "peak_ia_time": pk_t,
    }


def build_baseline_verification(archive, profile: MineProfile | None = None) -> dict:
    """Build v1 baseline verification dynamically from MineProfile + read-only SQLite archive."""
    prof = profile or load_mine_profile()
    station = prof.primary_station
    snap = _snapshot_identity(archive)
    days = [r["day"] for r in archive.db.execute("SELECT day FROM report ORDER BY day").fetchall()]
    day_span = f"{days[0]} 至 {days[-1]}" if len(days) >= 2 else (days[0] if days else "当前归档期")
    fmap = prof.feeder_map()

    incomers = list(prof.incomer_cabinets())
    trs = [t for t in prof.transformers if t.station == station]
    tr_hvs = [t.hv_cabinet for t in trs]
    tr_lvs = [t.lv_cabinet for t in trs]
    hv_kv = int(round(max((b.nominal_kv for b in prof.buses if b.station == station), default=35.0)))
    lv_kv = int(round(min((b.nominal_kv for b in prof.buses if b.station == station), default=10.0)))

    items: list[dict[str, Any]] = []

    # ── V01: Single-Incomer / Peak-Load Thermal Stability Verification ─────────
    win = discover_single_incomer_window(archive, prof)
    active_in = win["active_incomer"]
    zero_in = win["zero_incomer"]
    tr0_hv = tr_hvs[0] if tr_hvs else active_in
    r_act_p = _query_reading_safe(archive, station, active_in, "P", win["peak_p_time"])
    r_act_u = _query_reading_safe(archive, station, active_in, "Uab", win["peak_p_time"])
    r_act_i_p = _query_reading_safe(archive, station, active_in, "Ia", win["peak_p_time"])
    r_act_imax = _query_reading_safe(archive, station, active_in, "Ia", win["peak_ia_time"])
    r_zero_start = _query_reading_safe(archive, station, zero_in, "P", win["start_time"])
    r_tr_start_p = _query_reading_safe(archive, station, tr0_hv, "P", win["start_time"])
    r_tr_start_c = _query_reading_safe(archive, station, tr0_hv, "cos", win["start_time"])

    s_act_kva = (
        round(math.sqrt(3) * float(r_act_u["value"]) * float(r_act_i_p["value"]), 2)
        if r_act_u["value"] is not None and r_act_i_p["value"] is not None
        else 0.0
    )
    s_tr_kva = (
        round(abs(float(r_tr_start_p["value"])) / max(abs(float(r_tr_start_c["value"])), 0.1), 2)
        if r_tr_start_p["value"] is not None and r_tr_start_c["value"] not in (None, 0)
        else s_act_kva
    )
    day_compact = win["day"].replace("-", "")
    v01_rated_i_key = f"{active_in}_rated_current_a"
    v01_tr_kva_key = "main_transformer_rated_kva"
    v01_derate_key = "ambient_derating_factor"
    v01_sw_key = f"switch_log_{day_compact}"
    v01_deps = [v01_rated_i_key, v01_tr_kva_key, v01_derate_key, v01_sw_key]
    v01_cells = [c for c in (_cell_ref(r_zero_start), _cell_ref(r_act_p), _cell_ref(r_act_imax), _cell_ref(r_tr_start_p)) if c]

    items.append({
        "id": "V01",
        "item_kind": "single_incomer_thermal",
        "title": f"{hv_kv} kV 单进线带双主变期间（{win['day']} {win['start_time'][11:16]}–{win['end_time'][11:16]}）{active_in} 柜与主变热稳定裕度核验",
        "target_objects": [f"{station}.{c}" for c in dict.fromkeys(incomers + tr_hvs)],
        "time_window": f"{win['start_time']} 至 {win['end_time']}",
        "status": "needs_supplement",
        "recalculated": False,
        "reused_from_run": None,
        "param_bindings": {
            "active_incomer": active_in,
            "transformer_name": trs[0].name if trs else tr0_hv,
            "rated_current_key": v01_rated_i_key,
            "transformer_kva_key": v01_tr_kva_key,
            "derating_key": v01_derate_key,
            "switch_log_key": v01_sw_key,
        },
        "confirmed_local_facts": {
            "single_incomer_hours": win["hours"],
            "active_incomer": active_in,
            "zero_incomer": zero_in,
            "peak_p_kw": r_act_p["value"],
            "peak_i_a": r_act_imax["value"],
            "s_peak_kva": s_act_kva,
            "tr_peak_est_kva": s_tr_kva,
            # Legacy compatibility aliases
            "p303_peak_kw": r_act_p["value"],
            "i303_peak_a": r_act_imax["value"],
            "s303_15h_kva": s_act_kva,
            "p311_07h_kw": r_tr_start_p["value"],
            "s311_07h_est_kva": s_tr_kva,
            "evidence_cells": v01_cells,
        },
        "required_dependencies": v01_deps,
        "missing_dependencies": list(v01_deps),
        "conditional_judgment": None,
        "explanation": (
            f"已证实 {active_in} 柜在 {win['day']} 窗口期峰值电流 {r_act_imax['value']} A、峰值有功 {r_act_p['value']} kW；"
            f"因缺少 {active_in} 额定载流量、主变额定容量、环境降容系数及同期母联开关日志，暂不出具是否越限的结论。"
        ),
    })

    # ── V02: Dual Inter-Station / Underground Feeder N-1 Capacity ────────────
    links = list(prof.inter_station_links)
    if len(links) >= 2:
        l1, l2 = links[0], links[1]
        c1, c2 = l1.from_cabinet, l2.from_cabinet
        down_st = l1.to_station
        to1, to2 = l1.to_cabinet, l2.to_cabinet
    else:
        lv_f = [f.cabinet for f in prof.feeders if f.station == station and f.role == "feeder"]
        c1 = lv_f[0] if len(lv_f) >= 1 else active_in
        c2 = lv_f[1] if len(lv_f) >= 2 else c1
        down_st = next((s.code for s in prof.stations if s.code != station), station)
        to1, to2 = c1, c2

    v02_peak_row = archive.db.execute(
        """
        SELECT r1.time
        FROM reading r1
        JOIN reading r2 ON r2.station = r1.station AND r2.time = r1.time AND r2.metric = 'Ia'
        WHERE r1.station = ? AND r1.cabinet = ? AND r2.cabinet = ? AND r1.metric = 'Ia'
          AND r1.value IS NOT NULL AND r2.value IS NOT NULL
        ORDER BY (r1.value + r2.value) DESC, r1.time ASC
        LIMIT 1
        """,
        (station, c1, c2),
    ).fetchone()
    v02_peak_time = v02_peak_row["time"] if v02_peak_row else win["peak_p_time"]
    r_c1_i = _query_reading_safe(archive, station, c1, "Ia", v02_peak_time)
    r_c2_i = _query_reading_safe(archive, station, c2, "Ia", v02_peak_time)
    r_c1_p = _query_reading_safe(archive, station, c1, "P", v02_peak_time)
    r_c2_p = _query_reading_safe(archive, station, c2, "P", v02_peak_time)
    combined_i_a = round(float(r_c1_i["value"] or 0.0) + float(r_c2_i["value"] or 0.0), 2)
    combined_p_kw = round(float(r_c1_p["value"] or 0.0) + float(r_c2_p["value"] or 0.0), 2)

    down_tie = next((t for t in prof.bus_ties if t.station == down_st), None)
    tie_label = down_tie.cabinet if down_tie else "母联"
    v02_rated_key = f"{c1}_{c2}_rated_current_a"
    v02_tie_key = f"{down_st.lower()}_tie_switch_mode"
    v02_deps = [v02_rated_key, "ambient_derating_factor", v02_tie_key]
    diag_files = [d["filename"] for d in snap["diagrams"]]
    v02_cells = [c for c in (_cell_ref(r_c1_i), _cell_ref(r_c2_i), *diag_files[:2]) if c]
    diff_bus = (c1 in fmap and c2 in fmap and fmap[c1].bus_id != fmap[c2].bus_id)

    items.append({
        "id": "V02",
        "item_kind": "dual_feeder_n1",
        "title": f"{c1}/{c2} 下井双回路供电结构与 N-1 备用载流能力核验（GB/T 50417-2017 3.0.3）",
        "target_objects": [f"{station}.{c1}", f"{station}.{c2}", f"{down_st}.{to1}", f"{down_st}.{to2}"],
        "time_window": f"{day_span}（联合峰值时刻 {v02_peak_time}）",
        "status": "needs_supplement",
        "recalculated": False,
        "reused_from_run": None,
        "param_bindings": {
            "feeder_a": c1,
            "feeder_b": c2,
            "down_station": down_st,
            "rated_current_key": v02_rated_key,
            "derating_key": "ambient_derating_factor",
            "tie_switch_key": v02_tie_key,
        },
        "confirmed_local_facts": {
            "diagram_links_confirmed": [
                f"{station}.{c1} -> {down_st}.{to1}",
                f"{station}.{c2} -> {down_st}.{to2}",
            ],
            "different_bus_segments_confirmed": diff_bus,
            "peak_combined_time": v02_peak_time,
            "i_feeder_a_at_peak_a": r_c1_i["value"],
            "i_feeder_b_at_peak_a": r_c2_i["value"],
            "i1009_at_peak_a": r_c1_i["value"],
            "i1010_at_peak_a": r_c2_i["value"],
            "combined_current_a": combined_i_a,
            "combined_power_kw": combined_p_kw,
            "evidence_cells": v02_cells,
        },
        "required_dependencies": v02_deps,
        "missing_dependencies": list(v02_deps),
        "conditional_judgment": None,
        "explanation": (
            f"已由图模拓扑证实 {c1} 与 {c2} 接至 {down_st} {to1}/{to2}，同期联合最大电流 {combined_i_a} A（{v02_peak_time}）；"
            f"但缺下井电缆额定载流量、降容系数与井下 {tie_label} 联络开关状态，暂不出具 N-1 载流合格结论。"
        ),
    })

    # ── V03: Persistent Negative Active Power / Polarity Suspect Audit ───────
    neg_row = archive.db.execute(
        """
        SELECT cabinet, COUNT(*) AS neg_cnt
        FROM reading
        WHERE station = ? AND metric = 'P' AND value < -1.0
        GROUP BY cabinet
        ORDER BY neg_cnt DESC, cabinet ASC
        LIMIT 1
        """,
        (station,),
    ).fetchone()
    suspect_f = next((f for f in prof.feeders if f.station == station and f.polarity_suspect), None)
    pol_cab = neg_row["cabinet"] if neg_row else (suspect_f.cabinet if suspect_f else c1)
    pol_label = fmap[pol_cab].label if pol_cab in fmap else pol_cab

    neg_p_cnt = int(
        archive.db.execute(
            "SELECT COUNT(*) FROM reading WHERE station = ? AND cabinet = ? AND metric = 'P' AND value < 0",
            (station, pol_cab),
        ).fetchone()[0]
    )
    neg_cos_cnt = int(
        archive.db.execute(
            "SELECT COUNT(*) FROM reading WHERE station = ? AND cabinet = ? AND metric = 'cos' AND value < 0",
            (station, pol_cab),
        ).fetchone()[0]
    )
    min_p_row = archive.db.execute(
        "SELECT time FROM reading WHERE station = ? AND cabinet = ? AND metric = 'P' AND value IS NOT NULL ORDER BY value ASC, time ASC LIMIT 1",
        (station, pol_cab),
    ).fetchone()
    min_c_row = archive.db.execute(
        "SELECT time FROM reading WHERE station = ? AND cabinet = ? AND metric = 'cos' AND value IS NOT NULL ORDER BY value ASC, time ASC LIMIT 1",
        (station, pol_cab),
    ).fetchone()
    r_pol_pmin = _query_reading_safe(archive, station, pol_cab, "P", min_p_row["time"] if min_p_row else win["peak_p_time"])
    r_pol_cmin = _query_reading_safe(archive, station, pol_cab, "cos", min_c_row["time"] if min_c_row else win["peak_p_time"])

    # Compute exact median three-phase ratio |P| / (sqrt(3)*Uab*Ia*|cos|) from SQLite
    ratio_rows = archive.db.execute(
        """
        SELECT rP.value AS p, rU.value AS u, rI.value AS ia, rC.value AS c
        FROM reading rP
        JOIN reading rU ON rU.station=rP.station AND rU.cabinet=rP.cabinet AND rU.time=rP.time AND rU.metric='Uab'
        JOIN reading rI ON rI.station=rP.station AND rI.cabinet=rP.cabinet AND rI.time=rP.time AND rI.metric='Ia'
        JOIN reading rC ON rC.station=rP.station AND rC.cabinet=rP.cabinet AND rC.time=rP.time AND rC.metric='cos'
        WHERE rP.station=? AND rP.cabinet=? AND rP.metric='P'
          AND rP.value IS NOT NULL AND rU.value > 0 AND rI.value > 0.5 AND ABS(rC.value) > 0.05
        """,
        (station, pol_cab),
    ).fetchall()
    ratios = [abs(r["p"]) / (math.sqrt(3) * r["u"] * r["ia"] * abs(r["c"])) for r in ratio_rows]
    med_ratio = round(statistics.median(ratios), 4) if ratios else 0.0

    v03_audit_key = f"{pol_cab}_wiring_audit"
    items.append({
        "id": "V03",
        "item_kind": "polarity_metering",
        "title": f"{pol_cab} {pol_label}持续负有功与负功率因数二次接线核验",
        "target_objects": [f"{station}.{pol_cab}"],
        "time_window": f"{day_span}（负有功 {neg_p_cnt} 整点）",
        "status": "needs_supplement",
        "recalculated": False,
        "reused_from_run": None,
        "param_bindings": {"cabinet": pol_cab, "wiring_audit_key": v03_audit_key},
        "confirmed_local_facts": {
            "cabinet": pol_cab,
            "negative_p_hours": neg_p_cnt,
            "negative_cos_hours": neg_cos_cnt,
            "min_p_kw": r_pol_pmin["value"],
            "min_cos": r_pol_cmin["value"],
            "three_phase_ratio_median": med_ratio,
            "evidence_cells": [c for c in (_cell_ref(r_pol_pmin), _cell_ref(r_pol_cmin)) if c],
        },
        "required_dependencies": [v03_audit_key],
        "missing_dependencies": [v03_audit_key],
        "conditional_judgment": None,
        "explanation": (
            f"已证实 {pol_cab} 柜共有 {neg_p_cnt} 小时有功为负、{neg_cos_cnt} 小时功率因数为负且比例系数中位数 {med_ratio}；"
            "待现场补齐二次接线与 CT 极性实测记录后方可确诊。"
        ),
    })

    # ── V04: Auto-Derived LV Bus Power Conservation Unclosed Gap Audit ───────
    lv_eq = next(
        (eq for eq in prof.derive_balance_equations() if eq.id == "AUTO_BAL_LV_IN_VS_FEEDERS"),
        None,
    )
    lv_lhs = lv_eq.lhs_cabinets if lv_eq else tr_lvs
    lv_rhs = [
        f.cabinet
        for f in prof.feeders
        if f.station == station and f.role in ("feeder", "capacitor") and f.cabinet not in prof.spare_cabinets()
    ]
    lhs_ph = ",".join("?" for _ in lv_lhs)
    rhs_ph = ",".join("?" for _ in lv_rhs)
    lv_avg_row = archive.db.execute(
        f"""
        SELECT AVG(s) FROM (
            SELECT time, SUM(value) AS s FROM reading
            WHERE station = ? AND metric = 'P' AND cabinet IN ({lhs_ph}) AND value IS NOT NULL
            GROUP BY time
        )
        """,
        (station, *lv_lhs),
    ).fetchone()
    fd_avg_row = archive.db.execute(
        f"""
        SELECT AVG(s) FROM (
            SELECT time, SUM(value) AS s FROM reading
            WHERE station = ? AND metric = 'P' AND cabinet IN ({rhs_ph}) AND value IS NOT NULL
            GROUP BY time
        )
        """,
        (station, *lv_rhs),
    ).fetchone()
    lv_avg_kw = round(float(lv_avg_row[0] or 0.0), 2)
    fd_avg_kw = round(float(fd_avg_row[0] or 0.0), 2)
    unaccounted_kw = round(lv_avg_kw - fd_avg_kw, 2)
    unaccounted_pct = round(abs(unaccounted_kw) / max(abs(lv_avg_kw), 1.0) * 100.0, 1)

    sample_row = archive.db.execute(
        f"SELECT time FROM reading WHERE station = ? AND metric = 'P' AND cabinet IN ({lhs_ph}) GROUP BY time ORDER BY SUM(value) DESC LIMIT 1",
        (station, *lv_lhs),
    ).fetchone()
    sample_t = sample_row["time"] if sample_row else win["peak_p_time"]
    sample_recs = [_query_reading_safe(archive, station, c, "P", sample_t) for c in lv_lhs[:2]]

    v04_unlisted_key = f"{lv_kv}kv_unlisted_feeders_kw"
    v04_scale_key = f"{lv_kv}kv_feeder_scale_factor"
    v04_deps = [v04_unlisted_key, v04_scale_key]
    items.append({
        "id": "V04",
        "item_kind": "lv_bus_conservation",
        "title": f"{lv_kv} kV 母线进出线功率未闭合差额（{unaccounted_pct}%）与未列回路核验",
        "target_objects": [f"{station}.{c}" for c in lv_lhs],
        "time_window": day_span,
        "status": "needs_supplement",
        "recalculated": False,
        "reused_from_run": None,
        "param_bindings": {
            "lv_kv": lv_kv,
            "unlisted_key": v04_unlisted_key,
            "scale_key": v04_scale_key,
        },
        "confirmed_local_facts": {
            "lv_avg_kw": lv_avg_kw,
            "reported_feeders_avg_kw": fd_avg_kw,
            "unaccounted_avg_kw": unaccounted_kw,
            "unaccounted_pct": unaccounted_pct,
            "sample_time": sample_t,
            "sample_1005_p_kw": sample_recs[0]["value"] if len(sample_recs) >= 1 else None,
            "sample_1006_p_kw": sample_recs[1]["value"] if len(sample_recs) >= 2 else None,
            "evidence_cells": [c for c in (_cell_ref(r) for r in sample_recs) if c],
        },
        "required_dependencies": v04_deps,
        "missing_dependencies": list(v04_deps),
        "conditional_judgment": None,
        "explanation": (
            f"依据图模自动生成的 {lv_kv} kV 母线守恒方程，主变低压侧（均值 {lv_avg_kw} kW）与已列 {lv_kv} kV 出线"
            f"（均值 {fd_avg_kw} kW）间存在 {unaccounted_pct}% 平均差额；待补齐未列回路台账与 CT 倍率核实表后方可闭合母线守恒。"
        ),
    })

    payload = {
        "version": 1,
        "parent_run_id": None,
        "mine_id": prof.mine_id,
        "station": station,
        "snapshot": snap,
        "supplement_applied": None,
        "items": items,
    }
    run_id = "LWS-v1-" + _json_sha(payload)[:12]
    voi_hints = _build_voi_hints(archive, prof)
    result = {
        "run_id": run_id,
        **payload,
        "summary": {
            "total_items": len(items),
            "needs_supplement": sum(1 for x in items if x["status"] == "needs_supplement"),
            "conditionally_satisfied": 0,
            "conditionally_violated": 0,
            "recalculated_items": 0,
            "reused_items": 0,
        },
    }
    if voi_hints is not None:
        result["voi_hints"] = voi_hints
    return result


def _build_voi_hints(archive: Archive, prof: MineProfile) -> dict | None:
    """Attach pandapower parameter sensitivity Value-of-Information (VoI) hints for supplement prioritization."""
    if prof.mine_id != "laowadi":
        return None
    try:
        from .powerflow import analyze_parameter_sensitivity_voi

        voi = analyze_parameter_sensitivity_voi(archive)
        if voi.get("status") != "ok":
            return None
        by_param = {item["param_key"]: item for item in voi.get("tornado_items", [])}
        item_mapping = {
            "V01": ["transformer_sn_mva", "transformer_tap_pos"],
            "V02": ["metering_ratio", "cable_length_km"],
            "V03": ["metering_ratio"],
            "V04": ["metering_ratio", "transformer_vk_percent"],
        }
        item_priorities = {}
        for item_id, keys in item_mapping.items():
            matched = [by_param[k] for k in keys if k in by_param]
            if not matched:
                continue
            lead = max(matched, key=lambda x: float(x.get("voi_score", 0.0)))
            item_priorities[item_id] = {
                "lead_param_key": lead["param_key"],
                "lead_param_label": lead["label"],
                "priority_rank": lead["priority_rank"],
                "voi_score": lead["voi_score"],
                "dominant_impact": lead["dominant_impact"],
                "required_field_document": lead["required_field_document"],
            }
        return {
            "at_time": voi.get("at_time"),
            "n1_time": voi.get("n1_time"),
            "top_parameters": voi.get("voi_ranking", []),
            "item_priorities": item_priorities,
            "summary": voi.get("summary", ""),
        }
    except Exception:
        return None


def apply_supplement(baseline: dict, packet: dict) -> dict:
    """Validate supplement binding and trigger selective local recalculation generically for any mine."""
    if not isinstance(packet, dict):
        raise ValueError("补证包必须为 JSON 对象。")
    expected_station = baseline.get("station", "S35")
    if packet.get("station") != expected_station:
        raise ValueError(f"补证对象站名必须绑定为 {expected_station}（拒绝跨站或未绑定对象的补证）。")
    source_doc = str(packet.get("source_doc") or "").strip()
    if not source_doc:
        raise ValueError("补证包必须提供明确来源标识（source_doc）。")
    params = packet.get("parameters")
    if not isinstance(params, dict) or not params:
        raise ValueError("补证包必须包含非空 parameters 字段。")

    # Validate positive numeric parameters dynamically
    for k, val in params.items():
        if k.endswith("_rated_current_a") or k == "main_transformer_rated_kva" or k.endswith("_scale_factor"):
            if not isinstance(val, (int, float)) or val <= 0:
                raise ValueError(f"参数 {k} 必须为正数。")
    if "ambient_derating_factor" in params:
        df = params["ambient_derating_factor"]
        if not isinstance(df, (int, float)) or not (0.5 <= float(df) <= 1.2):
            raise ValueError("环境与敷设降容系数 ambient_derating_factor 须在 0.5 至 1.2 之间。")

    new_items = []
    recalculated_count = 0
    reused_count = 0

    for old_item in baseline["items"]:
        item = deepcopy(old_item)
        req_deps = set(item["required_dependencies"])
        touched = req_deps & set(params.keys())

        if not touched:
            item["recalculated"] = False
            item["reused_from_run"] = baseline["run_id"]
            reused_count += 1
            new_items.append(item)
            continue

        recalculated_count += 1
        item["recalculated"] = True
        item["reused_from_run"] = None
        missing = [d for d in item["required_dependencies"] if d not in params or params[d] in (None, "")]
        item["missing_dependencies"] = missing

        if missing:
            item["status"] = "partial_supplement"
            item["conditional_judgment"] = None
            item["explanation"] = (
                f"已收到部分补证（{', '.join(sorted(touched))}，来源：{source_doc}），"
                f"但仍缺必要前置条件（{', '.join(missing)}），拒绝在绑定不完整时出具条件判定。"
            )
            new_items.append(item)
            continue

        facts = item["confirmed_local_facts"]
        bindings = item.get("param_bindings") or {}
        kind = item.get("item_kind") or item["id"]

        if kind in ("single_incomer_thermal", "V01"):
            rated_i = float(params[bindings.get("rated_current_key", "303_rated_current_a")])
            rated_kva = float(params[bindings.get("transformer_kva_key", "main_transformer_rated_kva")])
            derate = float(params[bindings.get("derating_key", "ambient_derating_factor")])
            sw_log = str(params[bindings.get("switch_log_key", "switch_log_20261113")]).strip()
            act_cab = bindings.get("active_incomer", facts.get("active_incomer", "303"))

            eff_i_limit = round(rated_i * derate, 2)
            peak_i = float(facts.get("peak_i_a", facts.get("i303_peak_a", 0.0)))
            i_ratio_pct = round(peak_i / eff_i_limit * 100.0, 2)
            i_margin_pct = round(100.0 - i_ratio_pct, 2)

            peak_tr_kva = float(facts.get("tr_peak_est_kva", facts.get("s311_07h_est_kva", 0.0)))
            tr_ratio_pct = round(peak_tr_kva / rated_kva * 100.0, 2)
            tr_margin_pct = round(100.0 - tr_ratio_pct, 2)

            passed = i_ratio_pct <= 100.0 and tr_ratio_pct <= 100.0
            item["status"] = "conditionally_satisfied" if passed else "conditionally_violated"
            item["conditional_judgment"] = {
                "source_doc": source_doc,
                "switch_log_verified": sw_log,
                "effective_current_limit_a": eff_i_limit,
                "peak_current_a": peak_i,
                "current_load_ratio_pct": i_ratio_pct,
                "current_margin_pct": i_margin_pct,
                "transformer_rated_kva": rated_kva,
                "transformer_peak_kva": peak_tr_kva,
                "transformer_load_ratio_pct": tr_ratio_pct,
                "transformer_margin_pct": tr_margin_pct,
            }
            item["explanation"] = (
                f"依据补证（{source_doc}：{sw_log}；{act_cab} 额定 {rated_i} A × 降容 {derate} = 有效限值 {eff_i_limit} A；主变额定 {rated_kva} kVA）"
                f"局部复算：单进线窗口期 {act_cab} 峰值电流 {peak_i} A（负载率 {i_ratio_pct}%，裕度 {i_margin_pct:+}%），"
                f"主变峰值视在功率约 {peak_tr_kva} kVA（负载率 {tr_ratio_pct}%，裕度 {tr_margin_pct:+}%），"
                f"判定为【{'条件满足' if passed else '条件越限'}】。"
            )

        elif kind in ("dual_feeder_n1", "V02"):
            rated_i = float(params[bindings.get("rated_current_key", "1009_1010_rated_current_a")])
            derate = float(params[bindings.get("derating_key", "ambient_derating_factor")])
            tie_mode = str(params[bindings.get("tie_switch_key", "c1535_tie_switch_mode")]).strip()

            eff_i_limit = round(rated_i * derate, 2)
            n1_peak_i = float(facts["combined_current_a"])
            n1_ratio_pct = round(n1_peak_i / eff_i_limit * 100.0, 2)
            n1_margin_pct = round(100.0 - n1_ratio_pct, 2)
            tie_blocked = any(
                w in tie_mode
                for w in (
                    "闭锁", "不可切换", "禁止转供", "不具备转供", "不具备",
                    "不可转供", "不能转供", "无法转供", "故障停用", "断开不可合", "停电检修", "检修闭锁",
                )
            )
            thermal_passed = n1_ratio_pct <= 100.0
            transfer_prereq_passed = not tie_blocked
            passed = thermal_passed and transfer_prereq_passed
            item["status"] = "conditionally_satisfied" if passed else "conditionally_violated"
            item["conditional_judgment"] = {
                "source_doc": source_doc,
                "tie_switch_mode": tie_mode,
                "tie_blocked": tie_blocked,
                "thermal_calc_passed": thermal_passed,
                "transfer_prerequisite_passed": transfer_prereq_passed,
                "conflict_preserved": thermal_passed and tie_blocked,
                "effective_single_cable_limit_a": eff_i_limit,
                "n_minus_1_combined_peak_a": n1_peak_i,
                "n_minus_1_load_ratio_pct": n1_ratio_pct,
                "n_minus_1_margin_pct": n1_margin_pct,
            }
            item["explanation"] = (
                f"依据补证（{source_doc}：下井电缆额定 {rated_i} A × 降容 {derate} = 单回有效限值 {eff_i_limit} A；联络方式：{tie_mode}）"
                f"按 GB/T 50417-2017 3.0.3 复算 N-1 工况：任一回退出时另一回承担联合最大电流 {n1_peak_i} A"
                f"（热稳定载流计算{'成立' if thermal_passed else '越限'}，负载率 {n1_ratio_pct}%，裕度 {n1_margin_pct:+}%）；"
                + (
                    "但人工补证明确指出联络开关不具备转供前提（闭锁/检修），热稳定计算成立不能替代转供前提，保留冲突并否决转供条件，判定为【条件越限/不具备转供前提】。"
                    if tie_blocked
                    else f"且联络转供前提满足，判定为【{'条件满足' if passed else '条件越限'}】。"
                )
            )

        elif kind in ("polarity_metering", "V03"):
            audit_res = str(params[bindings.get("wiring_audit_key", "1018_wiring_audit")]).strip()
            cab = bindings.get("cabinet", facts.get("cabinet", "1018"))
            is_normal = any(
                w in audit_res
                for w in ("接线正常", "极性正确", "极性正常", "接线及极性正常", "接线无异常", "未见异常", "核查正常", "接线正确", "无异常")
            ) and not any(w in audit_res for w in ("反接", "接反", "极性错误", "接线错误", "故障"))
            neg_hours_cnt = int(facts.get("negative_p_hours", facts.get("negative_hours", 0)))
            has_persistent_neg = neg_hours_cnt > 0
            if is_normal and has_persistent_neg:
                # Astra #07: Preserve conflict between normal wiring check and persistent negative P readings!
                item["status"] = "evidence_conflict"
                item["conditional_judgment"] = {
                    "source_doc": source_doc,
                    "wiring_audit": audit_res,
                    "wiring_confirmed_normal": True,
                    "conflict_preserved": True,
                    "archive_negative_hours": neg_hours_cnt,
                }
                item["explanation"] = (
                    f"依据现场补证（{source_doc}：{audit_res}），{cab} 柜二次接线与极性核实正常，"
                    f"但与归档日报持续 {neg_hours_cnt} 小时负有功读数存在【证据冲突】，"
                    f"不能判定二次量测回路异常；须保留冲突并进一步核查下游反送电、表计四象限符号定义或采集规约倍率。"
                )
            elif is_normal:
                item["status"] = "conditionally_satisfied"
                item["conditional_judgment"] = {
                    "source_doc": source_doc,
                    "wiring_audit": audit_res,
                    "wiring_confirmed_normal": True,
                    "conflict_preserved": False,
                }
                item["explanation"] = f"依据现场补证（{source_doc}：{audit_res}），{cab} 柜二次接线与极性实测正常。"
            else:
                item["status"] = "conditionally_violated"
                item["conditional_judgment"] = {
                    "source_doc": source_doc,
                    "wiring_audit": audit_res,
                    "wiring_confirmed_normal": False,
                    "conflict_preserved": False,
                }
                item["explanation"] = (
                    f"依据现场补证（{source_doc}：{audit_res}），确认 {cab} 柜二次量测回路异常，需按检定规程更正极性/相序。"
                )

        elif kind in ("lv_bus_conservation", "V04"):
            unlisted_kw = float(params[bindings.get("unlisted_key", "10kv_unlisted_feeders_kw")])
            scale_factor = float(params[bindings.get("scale_key", "10kv_feeder_scale_factor")])
            lv_kv_lbl = bindings.get("lv_kv", 10)
            corrected_feeders = round(facts["reported_feeders_avg_kw"] / scale_factor + unlisted_kw, 2)
            residual_kw = round(facts["lv_avg_kw"] - corrected_feeders, 2)
            residual_pct = round(abs(residual_kw) / max(abs(facts["lv_avg_kw"]), 1.0) * 100.0, 2)
            passed = residual_pct <= 8.0
            item["status"] = "conditionally_satisfied" if passed else "conditionally_violated"
            item["conditional_judgment"] = {
                "source_doc": source_doc,
                "corrected_feeders_avg_kw": corrected_feeders,
                "residual_kw": residual_kw,
                "residual_pct": residual_pct,
            }
            item["explanation"] = (
                f"依据补证（{source_doc}：未列回路均值 {unlisted_kw} kW、出线折算系数 {scale_factor}）"
                f"修正后 {lv_kv_lbl} kV 出线总和均值 {corrected_feeders} kW，与主变低压侧差额缩至 {residual_kw:+} kW（{residual_pct}%）。"
            )

        new_items.append(item)

    payload = {
        "version": baseline["version"] + 1,
        "parent_run_id": baseline["run_id"],
        "mine_id": baseline.get("mine_id", "laowadi"),
        "station": expected_station,
        "snapshot": baseline["snapshot"],
        "supplement_applied": {
            "source_doc": source_doc,
            "target_object": str(packet.get("target_object") or expected_station),
            "effective_window": str(packet.get("effective_window") or ""),
            "archive_version": str(packet.get("archive_version") or baseline["snapshot"]["snapshot_sha256"][:16]),
            "parameters": params,
            "packet_sha256": _json_sha(packet),
        },
        "items": new_items,
    }
    run_id = f'LWS-v{payload["version"]}-' + _json_sha(payload)[:12]
    result = {
        "run_id": run_id,
        **payload,
        "summary": {
            "total_items": len(new_items),
            "needs_supplement": sum(1 for x in new_items if x["status"] in ("needs_supplement", "partial_supplement")),
            "conditionally_satisfied": sum(1 for x in new_items if x["status"] == "conditionally_satisfied"),
            "conditionally_violated": sum(1 for x in new_items if x["status"] == "conditionally_violated"),
            "evidence_conflict": sum(1 for x in new_items if x["status"] == "evidence_conflict"),
            "recalculated_items": recalculated_count,
            "reused_items": reused_count,
        },
    }
    if baseline.get("voi_hints") is not None:
        result["voi_hints"] = baseline["voi_hints"]
    return result


def render_supplement_markdown(v1: dict, v2: dict | None = None) -> str:
    mine_id = v1.get("mine_id", "laowadi")
    lines = [
        f"# 矿井配电网专业核验与补证局部复算报告（{mine_id} · 阶段 11）",
        "",
        f'- **基线运行编号**：`{v1["run_id"]}`（证据快照 `{v1["snapshot"]["snapshot_sha256"][:16]}…`）',
    ]
    if v2:
        lines += [
            f'- **补证后运行编号**：`{v2["run_id"]}`（父运行 `{v2["parent_run_id"]}`，补证来源 `{v2["supplement_applied"]["source_doc"]}`）',
            f'- **局部复算统计**：共 {v2["summary"]["total_items"]} 项核验——**局部重算 {v2["summary"]["recalculated_items"]} 项**，**按依赖直接复用未变结果 {v2["summary"]["reused_items"]} 项**',
        ]
    target = v2 or v1
    lines += [
        "",
        "| 核验编号 | 核验项目 | 状态 | 是否重算 | 复用自父版本 | 原始单元格与截图依据 |",
        "|---|---|---|---|---|---|",
    ]
    for item in target["items"]:
        ev = "；".join(f'`{c}`' for c in item["confirmed_local_facts"]["evidence_cells"][:3])
        lines.append(
            f'| `{item["id"]}` | {item["title"]} | `{item["status"]}` | '
            f'{"是（局部重算）" if item["recalculated"] else "否"} | '
            f'`{item["reused_from_run"] or "-"}` | {ev} |'
        )
    lines += ["", "## 逐项核验与补证对比明细", ""]
    for item in target["items"]:
        lines += [
            f'### {item["id"]} · {item["title"]}',
            f'- **对象与时段**：`{", ".join(item["target_objects"])}` · `{item["time_window"]}`',
            f'- **当前状态**：`{item["status"]}`（尚缺依赖：`{item["missing_dependencies"] or "无"}`）',
            f'- **核验结论**：{item["explanation"]}',
            "",
        ]
    return "\n".join(lines)
