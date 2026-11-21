"""Comprehensive verification for Enterprise Upgrade Workstreams 1 & 2:
1. Metadata-Driven Multi-Mine Architecture (MineProfile Pydantic v2 + zero-code multi-mine proof)
2. Monolith Modularization (verifier.py + local_router.py + slim diagnose.py)
3. Enterprise FastAPI + Pydantic v2 + OpenAPI 3.1 + Health Probes + Async SSE + CSRF Defense
4. Real pandapower Newton-Raphson AC Power-Flow Solver (all 4 scenarios + calibration + Agent Tool 11)
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
from pydantic import ValidationError

from laowadi.api import create_app
from laowadi.diagnose import diagnose_question, render_diagnosis_markdown, verify_diagnosis_groundedness
from laowadi.local_router import _local_diagnose, _parse_powerflow_request
from laowadi.mine_schema import MineProfile, load_mine_profile
from laowadi.monitor import EVENT_DB
from laowadi.powerflow import _solve_nr, build_pandapower_network, run_ac_power_flow
from laowadi.queries import Archive
from laowadi.storage import DB, ROOT, connect
from laowadi.verifier import verify_diagnosis_groundedness as verifier_fn
from laowadi.workbench import SOURCE_DIR, Workbench


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_multi_mine_metadata(archive: Archive) -> dict:
    # 1. Default Laowadi profile validates cleanly against SQLite 25/25 cabinets
    prof = load_mine_profile()
    check = prof.validate_against_sqlite(archive)
    assert check["consistent"] is True, f"Profile mismatch: {check}"
    assert check["db_cabinet_count"] == 25 and check["profile_cabinet_count"] == 25

    # 2. Invalid profile is strictly rejected by Pydantic v2
    bad_raw = prof.model_dump()
    bad_raw["voltage_limits"][0]["lo_kv"] = 99.0
    try:
        MineProfile.model_validate(bad_raw)
        raise AssertionError("Expected ValidationError for lo_kv >= hi_kv")
    except ValidationError:
        pass

    # 3. Zero-code-change multi-mine proof: construct a completely different coal mine profile
    synthetic_mine = {
        "schema_version": "2.0.0",
        "mine_id": "dongtan_demo",
        "mine_name": "东滩示范矿 35/10 kV 配电网（多矿适配验证）",
        "primary_station": "DT35",
        "polling_ramp_kw": 800.0,
        "balance_threshold_kw": 150.0,
        "model_assumption_disclaimer": "Synthetic unit-test mine profile for multi-mine architecture proof.",
        "stations": [
            {"code": "DT35", "name": "东滩地面 35 kV 变电所", "role": "surface_substation", "has_hourly_archive": True},
            {"code": "DT_UG", "name": "东滩井下中央变电所", "role": "underground_substation", "has_hourly_archive": False},
        ],
        "voltage_limits": [
            {"nominal_kv": 35.0, "lo_kv": 32.55, "hi_kv": 38.50, "operational_watch_hi_kv": 37.45, "standard_ref": "GB/T 12325"},
            {"nominal_kv": 10.0, "lo_kv": 9.30, "hi_kv": 10.70, "operational_watch_hi_kv": 10.70, "standard_ref": "GB/T 12325"},
        ],
        "buses": [
            {"id": "DT_35_I", "station": "DT35", "name": "东滩35kV母线", "nominal_kv": 35.0, "segment": "35kV", "reference_cabinet": "101"},
            {"id": "DT_10_I", "station": "DT35", "name": "东滩10kV母线", "nominal_kv": 10.5, "segment": "I段", "reference_cabinet": "201"},
            {"id": "DT_UG_10", "station": "DT_UG", "name": "东滩井下10kV母线", "nominal_kv": 10.5, "segment": "UG", "reference_cabinet": "901"},
        ],
        "transformers": [
            {
                "id": "DT_T1",
                "name": "东滩1#主变 (10.0 MVA)",
                "station": "DT35",
                "hv_cabinet": "102",
                "lv_cabinet": "201",
                "hv_bus": "DT_35_I",
                "lv_bus": "DT_10_I",
                "default_sn_mva": 10.0,
                "default_vn_hv_kv": 35.0,
                "default_vn_lv_kv": 10.5,
                "default_vk_percent": 7.5,
                "default_vkr_percent": 0.55,
                "calibrated_tap_pos": 1,
            }
        ],
        "feeders": [
            {"cabinet": "101", "station": "DT35", "label": "35kV进线", "bus_id": "DT_35_I", "role": "incomer", "nominal_kv": 35.0},
            {"cabinet": "102", "station": "DT35", "label": "1#主变高压", "bus_id": "DT_35_I", "role": "transformer_hv", "nominal_kv": 35.0},
            {"cabinet": "201", "station": "DT35", "label": "1#主变低压", "bus_id": "DT_10_I", "role": "transformer_lv", "nominal_kv": 10.0},
            {
                "cabinet": "202", "station": "DT35", "label": "井下主馈线", "bus_id": "DT_10_I", "role": "feeder", "nominal_kv": 10.0,
                "downstream_bus_id": "DT_UG_10", "default_cable_length_km": 2.5, "default_r_ohm_per_km": 0.16, "default_x_ohm_per_km": 0.08, "default_max_i_ka": 0.35,
            },
        ],
        "inter_station_links": [],
        "bus_ties": [],
        "balance_equations": [],
    }
    alt_prof = MineProfile.model_validate(synthetic_mine)
    alt_snap = {
        "101": {"Uab": {"value": 36.2, "cell": "demo"}, "P": {"value": 4500.0, "cell": "demo"}, "Ia": {"value": 76.0, "cell": "demo"}, "cos": {"value": 0.94, "cell": "demo"}},
        "201": {"Uab": {"value": 10.4, "cell": "demo"}, "P": {"value": 4460.0, "cell": "demo"}, "Ia": {"value": 260.0, "cell": "demo"}, "cos": {"value": 0.93, "cell": "demo"}},
        "202": {"Uab": {"value": 10.4, "cell": "demo"}, "P": {"value": 1800.0, "cell": "demo"}, "Ia": {"value": 118.0, "cell": "demo"}, "cos": {"value": 0.85, "cell": "demo"}},
    }
    alt_net, _ = build_pandapower_network(alt_prof, alt_snap)
    assert _solve_nr(alt_net) is True, "Synthetic second mine failed NR convergence!"
    ug_v = round(float(alt_net.res_bus.vm_pu.at[2]) * 10.5, 3)
    assert 9.3 <= ug_v <= 10.7, f"Unexpected synthetic underground voltage: {ug_v}"

    return {
        "laowadi_consistent": check["consistent"],
        "synthetic_mine_converged": True,
        "synthetic_mine_ug_kv": ug_v,
    }


def verify_modularization() -> dict:
    assert verify_diagnosis_groundedness is verifier_fn
    assert callable(_local_diagnose) and callable(_parse_powerflow_request)
    diag_lines = len((ROOT / "laowadi" / "diagnose.py").read_text(encoding="utf-8").splitlines())
    ver_lines = len((ROOT / "laowadi" / "verifier.py").read_text(encoding="utf-8").splitlines())
    router_lines = len((ROOT / "laowadi" / "local_router.py").read_text(encoding="utf-8").splitlines())
    assert diag_lines < 950, f"diagnose.py should remain modular (<950L vs 1815L before), got {diag_lines}"
    return {"diagnose_lines": diag_lines, "verifier_lines": ver_lines, "local_router_lines": router_lines}


def verify_pandapower_all_scenarios(archive: Archive) -> dict:
    # 1. base_case calibration at 2026-11-13 17:00
    r_base = run_ac_power_flow(archive, scenario="base_case", at_time="2026-11-13 17:00:00")
    assert r_base["status"] == "ok" and r_base["solver"]["converged"] is True
    b1 = next(b for b in r_base["buses"] if b["bus_id"] == "S35_10KV_I")
    b2 = next(b for b in r_base["buses"] if b["bus_id"] == "S35_10KV_II")
    assert abs(b1["calibration_diff_kv"]) <= 0.05, f"10kV I calibration residual too large: {b1}"
    assert abs(b2["calibration_diff_kv"]) <= 0.05, f"10kV II calibration residual too large: {b2}"
    assert "parameterized" in r_base["model_assumptions"]["parameter_nature"]

    # 2. tap_sensitivity strictly monotonic and solves 10kV II overvoltage
    r_tap = run_ac_power_flow(archive, scenario="tap_sensitivity", at_time="2026-11-13 17:00:00")
    sweep = r_tap["tap_sensitivity"]["sweep"]
    assert len(sweep) == 5
    volts = [s["bus_10kv_ii_kv"] for s in sweep]
    assert all(volts[i] > volts[i + 1] for i in range(len(volts) - 1)), f"Tap sweep not strictly monotonic: {volts}"
    rec = r_tap["tap_sensitivity"]["recommended_tap"]
    assert rec is not None and rec["tap_pos"] == 2 and rec["gb12325_compliant"] is True
    assert 9.30 <= rec["bus_10kv_ii_kv"] <= 10.70

    # 3. n1_contingency (feeder_1009 @ 2026-11-10 06:00)
    r_n1 = run_ac_power_flow(archive, scenario="n1_contingency", outage_mode="feeder_1009")
    n1 = r_n1["n1_contingency"]
    assert n1["converged"] is True and n1["verdict"] == "conditionally_passed"
    assert n1["receiving_1010_comparison"]["after_i_a"] > n1["receiving_1010_comparison"]["before_i_a"]
    assert n1["receiving_1010_comparison"]["min_underground_kv"] >= 9.30

    # 4. reactive_opt (1010 @ 2026-11-10 06:00)
    r_ro = run_ac_power_flow(archive, scenario="reactive_opt", target_cabinet="1010")
    d = r_ro["reactive_opt"]["delta"]
    assert r_ro["reactive_opt"]["converged"] is True
    assert d["receiving_u_boost_kv"] > 0.0
    assert d["current_reduction_pct"] > 10.0
    assert d["line_loss_reduction_pct"] > 20.0
    assert d["total_loss_reduction_kw"] > 3.0

    # 5. 24-hour discrete tap & capacitor switching DP schedule + 7-day weekly Pareto analysis
    from laowadi.powerflow import analyze_weekly_schedule_pareto, optimize_daily_tap_cap_schedule

    sched = optimize_daily_tap_cap_schedule(archive, "2026-11-13", target_cabinet="1010")
    assert sched["status"] == "ok" and len(sched["hours"]) == 24
    assert sched["all_hours_safe"] is True and sched["optimized_violations_count"] == 0
    assert sched["baseline_violations_count"] > 0
    assert sched["tap_changes"] <= sched["max_tap_changes"]
    assert sched["cap_changes"] <= sched["max_cap_changes"]
    assert sched["optimized_loss_kwh"] < sched["baseline_loss_kwh"]
    assert all(h["feeder_q_kvar"] >= -1e-6 for h in sched["hours"])

    weekly = analyze_weekly_schedule_pareto(archive, target_cabinet="1010", pareto_day="2026-11-13")
    assert weekly["status"] == "ok" and len(weekly["weekly_days"]) == 7
    ws = weekly["weekly_summary"]
    assert ws["total_hours"] == 168
    assert ws["optimized_violations_total"] < ws["baseline_violations_total"]
    assert ws["total_loss_saving_kwh"] > 0
    frontier = weekly["pareto_frontier"]
    assert len(frontier) == 6
    # Relaxing budget from (0/0) to (2/6) monotonically eliminates violations and reduces/keeps loss
    assert frontier[-1]["violations"] <= frontier[0]["violations"]
    assert frontier[-1]["all_hours_safe"] is True
    assert weekly["featured_day_schedule"]["day"] == "2026-11-13"

    # 6. Agent Tool 11 end-to-end groundedness check
    diag = diagnose_question(
        archive,
        "请用 pandapower 交流潮流评估 2026年11月13日17:00 2#主变分接头调几档可以消除 10 kV II段过压？",
    )
    assert diag["status"] == "ok"
    assert diag["verification"]["status"] == "verified", f"Verification failed: {diag['verification']}"
    assert diag["verification"]["verified_tool_results_count"] > 0
    assert diag["verification"]["verified_tool_results_count"] == diag["verification"]["tool_result_checks_total"]
    assert diag["ui_artifact_hint"]["canvas"] == "powerflow"
    assert any(t["tool"] == "run_ac_power_flow" for t in diag["tool_trace"])
    assert "pandapower" in render_diagnosis_markdown(diag)

    return {
        "calibration_diff_10kv_i_kv": b1["calibration_diff_kv"],
        "calibration_diff_10kv_ii_kv": b2["calibration_diff_kv"],
        "tap_recommended": rec["tap_label"],
        "tap_post_kv": rec["bus_10kv_ii_kv"],
        "n1_verdict": n1["verdict"],
        "reactive_loss_reduction_kw": d["total_loss_reduction_kw"],
        "daily_24h_loss_saving_kwh": sched["loss_saving_kwh"],
        "daily_24h_all_safe": sched["all_hours_safe"],
        "agent_tool11_verified": True,
    }


def verify_fastapi_enterprise() -> dict:
    with TemporaryDirectory() as tmp:
        wb = Workbench(
            DB,
            EVENT_DB,
            SOURCE_DIR,
            None,
            reviews_db=Path(tmp) / "reviews.sqlite3",
            sessions_db=Path(tmp) / "sessions.sqlite3",
            audit_db=Path(tmp) / "audit.sqlite3",
            jobs_db=Path(tmp) / "jobs.sqlite3",
        )
        app = create_app(wb)
        client = TestClient(app)

        # Probes & OpenAPI
        hz = client.get("/healthz")
        assert hz.status_code == 200 and hz.json()["engine"] == "fastapi+uvicorn"
        rz = client.get("/readyz")
        assert rz.status_code == 200 and rz.json()["status"] == "ready"
        oa = client.get("/openapi.json")
        assert oa.status_code == 200 and len(oa.json()["paths"]) >= 20
        assert client.get("/docs").status_code == 200

        # Mine profile & powerflow API
        mp = client.get("/api/mine-profile")
        assert mp.status_code == 200 and mp.json()["archive_validation"]["consistent"] is True

        pf_ok = client.post("/api/powerflow", json={"scenario": "tap_sensitivity"})
        assert pf_ok.status_code == 200 and pf_ok.json()["tap_sensitivity"]["recommended_tap"]["tap_pos"] == 2

        # Pydantic v2 strict validation error -> HTTP 400 with Chinese error envelope
        pf_bad = client.post("/api/powerflow", json={"scenario": "invalid_mode", "transformer_sn_mva": -5})
        assert pf_bad.status_code == 400 and "error" in pf_bad.json()

        # CSRF Origin protection -> HTTP 403
        csrf = client.get("/api/dates", headers={"Origin": "http://evil.example"})
        assert csrf.status_code == 403

        # Parity checks on existing workbench endpoints
        for route in ("/api/dates", "/api/health", "/api/cabinet?cabinet=1018", "/api/diagrams", "/api/timeline", "/api/supplement", "/api/reviews", "/api/sessions"):
            resp = client.get(route)
            assert resp.status_code == 200, f"Route {route} failed: {resp.status_code}"

        # True async SSE streaming check
        sse_events = []
        with client.stream(
            "POST",
            "/api/diagnose/stream",
            json={"question": "请用 pandapower 交流潮流分析2#主变分接头调压方案", "mode": "local", "sessionId": "fastapi-sse-test"},
        ) as stream_resp:
            assert stream_resp.status_code == 200
            assert "text/event-stream" in stream_resp.headers["content-type"]
            for line in stream_resp.iter_lines():
                if line.startswith("data: "):
                    sse_events.append(json.loads(line[6:]))
        ev_types = [e["type"] for e in sse_events]
        assert "plan" in ev_types and "tool_start" in ev_types and "tool_end" in ev_types and "done" in ev_types
        done_res = sse_events[-1]["result"]
        assert done_res["verification"]["status"] == "verified"
        assert done_res["ui_artifact_hint"]["canvas"] == "powerflow"

        return {
            "openapi_paths": len(oa.json()["paths"]),
            "readyz": rz.json()["status"],
            "pydantic_validation_400": True,
            "csrf_guard_403": True,
            "sse_event_count": len(sse_events),
        }


def main():
    before_db = sha256(DB)
    before_ev = sha256(EVENT_DB)

    with closing(connect(DB)) as db:
        archive = Archive(db, EVENT_DB)
        multi_mine = verify_multi_mine_metadata(archive)
        mod = verify_modularization()
        pf = verify_pandapower_all_scenarios(archive)
    api = verify_fastapi_enterprise()

    assert sha256(DB) == before_db and sha256(EVENT_DB) == before_ev, "Archive SQLite mutated!"
    print(
        json.dumps(
            {
                "status": "ok",
                "archives_unchanged": True,
                "workstream1_multi_mine": multi_mine,
                "workstream1_modularization": mod,
                "workstream1_fastapi": api,
                "workstream2_pandapower": pf,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
