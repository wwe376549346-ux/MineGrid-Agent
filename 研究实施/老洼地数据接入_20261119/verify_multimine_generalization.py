"""Verification Suite: True Multi-Mine Generalization (Zero Hardcoding in supplement, rag, graph, SLD & Power Balance).

Builds a completely independent synthetic second coal mine ("东坡煤矿", mine_id="dongpo", stations D35 & U900,
cabinets H101/H102/T1H/T2H/L101/L102/F201/F202/F203/C201/C202/S299 — ZERO Laowadi cabinet IDs or dates!)
and verifies WITHOUT modifying any code:
1. `MineProfile.derive_balance_equations()` automatically generates KCL power-conservation equations (even when `balance_equations=[]`).
2. `MineProfile.build_sld_layout(archive)` automatically computes HV bays, bus ties, transformers, LV sections,
   inter-station corridors & underground station layout for BOTH Laowadi AND Dongpo Mine.
3. `supplement.build_baseline_verification(archive, dongpo)` + `apply_supplement()` dynamically builds V01–V04,
   recalculates with Dongpo parameter bindings, and enforces V02 tie-blocked & V03 normal-wiring semantics.
4. `rag.build_mine_evidence_cards(archive, dongpo)` + `ConditionalRAGEngine(archive, dongpo)` generates Dongpo-only
   evidence cards (`EVC-DONGPO-*`) and intercepts cross-station U900 hourly read attempts.
5. `graph.LaowadiAuditGraph(archive, store, profile=dongpo)` dynamically plans Dongpo steps, suspends, and supports
   cumulative 2-round supplement merging without dropping round-1 parameters.
6. `diag_tools.DiagnosticToolbox(archive, profile=dongpo).check_power_balance()` evaluates auto-derived equations.
7. Static source check confirms `SingleLineDiagram.jsx` contains zero hardcoded Laowadi cabinet constants.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile

from laowadi.diag_tools import DiagnosticToolbox
from laowadi.graph import CheckpointStore, LaowadiAuditGraph
from laowadi.mine_schema import MineProfile, load_mine_profile
from laowadi.monitor import EVENT_DB
from laowadi.queries import Archive
from laowadi.rag import ConditionalRAGEngine, build_mine_evidence_cards
from laowadi.storage import DB, ROOT, connect
from laowadi.supplement import apply_supplement, build_baseline_verification


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_dongpo_profile() -> MineProfile:
    """Create a second coal mine with completely distinct station/bus/cabinet names & omitted balance_equations."""
    return MineProfile.model_validate({
        "schema_version": "2.0.0",
        "mine_id": "dongpo",
        "mine_name": "东坡煤矿 35/6 kV 配电网",
        "primary_station": "D35",
        "polling_ramp_kw": 800.0,
        "balance_threshold_kw": 150.0,
        "model_assumption_disclaimer": "东坡煤矿参数化测试模型。",
        "stations": [
            {"code": "D35", "name": "东坡地面 35 kV 变电站", "role": "surface_substation", "has_hourly_archive": True, "diagram_filename": "D35_sld.png"},
            {"code": "U900", "name": "井下 900 水平中央变电所", "role": "underground_substation", "has_hourly_archive": False, "diagram_filename": "U900_sld.png"},
        ],
        "voltage_limits": [
            {"nominal_kv": 35.0, "lo_kv": 32.55, "hi_kv": 38.50, "operational_watch_hi_kv": 37.45, "standard_ref": "GB/T 12325"},
            {"nominal_kv": 6.0, "lo_kv": 5.58, "hi_kv": 6.42, "operational_watch_hi_kv": 6.42, "standard_ref": "GB/T 12325 (±7%)"},
        ],
        "buses": [
            {"id": "D35_35_A", "station": "D35", "name": "35 kV A段母线", "nominal_kv": 35.0, "segment": "35A", "reference_cabinet": "H101"},
            {"id": "D35_35_B", "station": "D35", "name": "35 kV B段母线", "nominal_kv": 35.0, "segment": "35B", "reference_cabinet": "H102"},
            {"id": "D35_6_A", "station": "D35", "name": "6 kV A段母线", "nominal_kv": 6.0, "segment": "6A", "reference_cabinet": "L101"},
            {"id": "D35_6_B", "station": "D35", "name": "6 kV B段母线", "nominal_kv": 6.0, "segment": "6B", "reference_cabinet": "L102"},
            {"id": "U900_6_A", "station": "U900", "name": "900中央变 6 kV A段", "nominal_kv": 6.0, "segment": "U900_A", "reference_cabinet": "U101"},
            {"id": "U900_6_B", "station": "U900", "name": "900中央变 6 kV B段", "nominal_kv": 6.0, "segment": "U900_B", "reference_cabinet": "U201"},
        ],
        "transformers": [
            {
                "id": "TR_A", "name": "东坡1#主变", "station": "D35",
                "hv_cabinet": "T1H", "lv_cabinet": "L101", "hv_bus": "D35_35_A", "lv_bus": "D35_6_A",
                "default_sn_mva": 8.0, "default_vn_hv_kv": 35.0, "default_vn_lv_kv": 6.3,
                "default_vk_percent": 7.5, "default_vkr_percent": 0.6,
            },
            {
                "id": "TR_B", "name": "东坡2#主变", "station": "D35",
                "hv_cabinet": "T2H", "lv_cabinet": "L102", "hv_bus": "D35_35_B", "lv_bus": "D35_6_B",
                "default_sn_mva": 8.0, "default_vn_hv_kv": 35.0, "default_vn_lv_kv": 6.3,
                "default_vk_percent": 7.5, "default_vkr_percent": 0.6,
            },
        ],
        "feeders": [
            {"cabinet": "H101", "station": "D35", "label": "东坡一回进线", "bus_id": "D35_35_A", "role": "incomer", "nominal_kv": 35.0},
            {"cabinet": "H102", "station": "D35", "label": "东坡二回进线", "bus_id": "D35_35_B", "role": "incomer", "nominal_kv": 35.0},
            {"cabinet": "T1H", "station": "D35", "label": "1#主变高压", "bus_id": "D35_35_A", "role": "transformer_hv", "nominal_kv": 35.0},
            {"cabinet": "T2H", "station": "D35", "label": "2#主变高压", "bus_id": "D35_35_B", "role": "transformer_hv", "nominal_kv": 35.0},
            {"cabinet": "L101", "station": "D35", "label": "1#主变低压", "bus_id": "D35_6_A", "role": "transformer_lv", "nominal_kv": 6.0},
            {"cabinet": "L102", "station": "D35", "label": "2#主变低压", "bus_id": "D35_6_B", "role": "transformer_lv", "nominal_kv": 6.0},
            {"cabinet": "F201", "station": "D35", "label": "下井I回900", "bus_id": "D35_6_A", "role": "feeder", "nominal_kv": 6.0, "downstream_bus_id": "U900_6_A"},
            {"cabinet": "F202", "station": "D35", "label": "下井II回900", "bus_id": "D35_6_B", "role": "feeder", "nominal_kv": 6.0, "downstream_bus_id": "U900_6_B"},
            {"cabinet": "F203", "station": "D35", "label": "北翼采区回", "bus_id": "D35_6_B", "role": "feeder", "nominal_kv": 6.0, "polarity_suspect": True},
            {"cabinet": "C201", "station": "D35", "label": "电容补偿A", "bus_id": "D35_6_A", "role": "capacitor", "nominal_kv": 6.0},
            {"cabinet": "C202", "station": "D35", "label": "电容补偿B", "bus_id": "D35_6_B", "role": "capacitor", "nominal_kv": 6.0},
            {"cabinet": "S299", "station": "D35", "label": "6kV备用柜", "bus_id": "D35_6_B", "role": "spare", "nominal_kv": 6.0},
        ],
        "inter_station_links": [
            {"from_station": "D35", "from_cabinet": "F201", "to_station": "U900", "to_cabinet": "U101", "description": "下井I回900电缆", "default_cable_length_km": 1.5, "default_r_ohm_per_km": 0.2, "default_x_ohm_per_km": 0.08, "default_max_i_ka": 0.28},
            {"from_station": "D35", "from_cabinet": "F202", "to_station": "U900", "to_cabinet": "U201", "description": "下井II回900电缆", "default_cable_length_km": 1.5, "default_r_ohm_per_km": 0.2, "default_x_ohm_per_km": 0.08, "default_max_i_ka": 0.28},
        ],
        "bus_ties": [
            {"id": "TIE_D35", "station": "D35", "cabinet": "H100", "bus_a": "D35_35_A", "bus_b": "D35_35_B", "normal_closed": False, "description": "东坡35kV母联"},
            {"id": "TIE_U900", "station": "U900", "cabinet": "U900T", "bus_a": "U900_6_A", "bus_b": "U900_6_B", "normal_closed": False, "description": "900变电所母联"},
        ],
        # Intentionally OMIT balance_equations to verify 100% auto-derivation from topology!
        "balance_equations": [],
    })


def _create_dongpo_sqlite(db_path: Path, profile: MineProfile) -> None:
    """Create a realistic 24-hour SQLite archive for Dongpo Mine (2026-12-01)."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE station (code TEXT PRIMARY KEY, name TEXT, diagram TEXT);
        CREATE TABLE cabinet (station TEXT, number TEXT, label TEXT, aliases TEXT DEFAULT '[]', PRIMARY KEY (station, number));
        CREATE TABLE line (from_station TEXT, from_cabinet TEXT, to_station TEXT, to_cabinet TEXT, label TEXT);
        CREATE TABLE report (day TEXT PRIMARY KEY, filename TEXT, sheet TEXT, sha256 TEXT);
        CREATE TABLE diagram (filename TEXT PRIMARY KEY, captured_at TEXT, sha256 TEXT);
        CREATE TABLE reading (
            day TEXT, time TEXT, station TEXT, cabinet TEXT, metric TEXT,
            value REAL, marker TEXT, source_row INTEGER, source_col TEXT,
            PRIMARY KEY (time, station, cabinet, metric)
        );
        CREATE TABLE summary (
            day TEXT, station TEXT, cabinet TEXT, metric TEXT, statistic TEXT,
            value REAL, source_row INTEGER, source_col TEXT
        );
        """
    )
    for s in profile.stations:
        conn.execute("INSERT INTO station VALUES (?, ?, ?)", (s.code, s.name, s.diagram_filename or ""))
    for f in profile.feeders:
        conn.execute("INSERT INTO cabinet VALUES (?, ?, ?, '[]')", (f.station, f.cabinet, f.label))
    for num, lbl in [("U101", "900一回进线"), ("U102", "主水泵I"), ("U201", "900二回进线"), ("U202", "主水泵II"), ("U900T", "900联络柜")]:
        conn.execute("INSERT INTO cabinet VALUES (?, ?, ?, '[]')", ("U900", num, lbl))
    for ln in profile.inter_station_links:
        conn.execute("INSERT INTO line VALUES (?, ?, ?, ?, ?)", (ln.from_station, ln.from_cabinet, ln.to_station, ln.to_cabinet, ln.description))
    conn.execute("INSERT INTO report VALUES ('2026-12-01', 'dongpo_1201.xls', 'Sheet1', ?)", ("a" * 64,))
    conn.execute("INSERT INTO diagram VALUES ('D35_sld.png', '2026-12-01 08:00', ?)", ("b" * 64,))
    conn.execute("INSERT INTO diagram VALUES ('U900_sld.png', '2026-12-01 08:05', ?)", ("c" * 64,))

    for hr in range(24):
        ts = f"2026-12-01 {hr:02d}:00:00"
        single_in = 8 <= hr <= 15
        vals = {
            "H101": (36.4, 72.0 if single_in else 38.0, 4100.0 if single_in else 2100.0, 0.90),
            "H102": (36.2, 0.0 if single_in else 34.0, 0.0 if single_in else 1900.0, 0.0 if single_in else 0.89),
            "T1H": (36.4, 38.0, 2100.0, 0.90),
            "T2H": (36.2, 34.0, 1980.0 if single_in else 1900.0, 0.89),
            "L101": (6.20, 215.0, 2080.0, 0.90),
            "L102": (6.52 if hr >= 12 else 6.30, 195.0, 1890.0, 0.88),
            "F201": (6.18, 92.0 if hr == 14 else 60.0, 810.0 if hr == 14 else 520.0, 0.82),
            "F202": (6.50 if hr >= 12 else 6.28, 98.0 if hr == 14 else 65.0, 740.0 if hr == 14 else 490.0, 0.66),
            "F203": (6.48, 22.0, -145.0, -0.45),
            "C201": (6.19, 18.0, 1.1, 0.02),
            "C202": (6.49, 30.0, -5.0 if hr == 10 else 2.0, 0.72),
            "S299": (6.48, 0.0, 0.0, 0.0),
        }
        # Inject one SCADA polling skew at 17:00
        if hr == 17:
            vals["H101"] = (36.4, 52.0, 3200.0, 0.90)
        for ci, (cab, (u, ia, p, cos)) in enumerate(vals.items()):
            for mi, (m, v) in enumerate([("Uab", u), ("Ia", ia), ("P", p), ("cos", cos)]):
                col = chr(ord("B") + (ci * 4 + mi) % 24)
                conn.execute(
                    "INSERT INTO reading VALUES (?, ?, 'D35', ?, ?, ?, NULL, ?, ?)",
                    ("2026-12-01", ts, cab, m, v, hr + 4, col),
                )
    conn.commit()
    conn.close()


def main() -> None:
    before_db = _sha256(DB)
    before_ev = _sha256(EVENT_DB)

    # 1. Verify Laowadi auto-derived layout & balance equations
    lw_prof = load_mine_profile()
    with closing(connect(DB)) as lw_conn:
        lw_arch = Archive(lw_conn, EVENT_DB)
        lw_layout = lw_prof.build_sld_layout(lw_arch)
    assert len(lw_layout["primary_bays"]) == 2
    assert len(lw_layout["lv_sections"]) == 2
    assert len(lw_layout["inter_station_corridors"]) == 2
    assert len(lw_layout["downstream_stations"]) == 1 and lw_layout["downstream_stations"][0]["total_cabinets"] == 15
    assert len(lw_layout["auto_balance_equations"]) == 4
    print("[1/7] Laowadi MineProfile auto-layout & 4 auto-derived balance equations OK")

    # 2. Verify completely synthetic Dongpo Mine (zero Laowadi IDs!)
    dp_prof = _build_dongpo_profile()
    dp_eqs = dp_prof.effective_balance_equations()
    assert len(dp_eqs) == 4, f"Expected 4 auto-derived equations when balance_equations=[], got {len(dp_eqs)}"
    assert dp_eqs[0].lhs_cabinets == ["H101", "H102"] and dp_eqs[0].rhs_cabinets == ["T1H", "T2H"]

    with tempfile.TemporaryDirectory() as td:
        dp_db = Path(td) / "dongpo.sqlite3"
        dp_ev = Path(td) / "dongpo_events.sqlite3"
        _create_dongpo_sqlite(dp_db, dp_prof)

        with closing(connect(dp_db)) as conn:
            dp_arch = Archive(conn, dp_ev)
            dp_arch.devices = {
                (r["station"], r["number"]): {"station": r["station"], "number": r["number"], "label": r["label"], "station_name": r["station"]}
                for r in conn.execute("SELECT station, number, label FROM cabinet").fetchall()
            }

            # 2b. SLD Auto-Layout on Dongpo Mine
            dp_layout = dp_prof.build_sld_layout(dp_arch)
            assert dp_layout["mine_id"] == "dongpo"
            assert dp_layout["hv_nominal_kv"] == 35.0 and dp_layout["lv_nominal_kv"] == 6.0
            assert len(dp_layout["primary_bays"]) == 2
            assert dp_layout["downstream_stations"][0]["station"]["code"] == "U900"
            assert dp_layout["downstream_stations"][0]["total_cabinets"] == 5
            print("[2/7] Dongpo Mine (35/6 kV) auto-derived balance equations & SLD auto-layout OK")

            # 3. DiagnosticToolbox check_power_balance on Dongpo Mine
            tb = DiagnosticToolbox(dp_arch, profile=dp_prof)
            bal = tb.check_power_balance("2026-12-01 12:00:00")
            assert bal["status"] == "ok" and bal["station"] == "D35"
            assert "single_incomer_dual_transformer_suspected" in bal["flags"]
            assert len(bal["equations"]) == 4
            print("[3/7] DiagnosticToolbox.check_power_balance() auto-derived equations on Dongpo Mine OK")

            # 4. Supplement V01-V04 dynamic discovery & negative-evidence rules on Dongpo Mine
            v1 = build_baseline_verification(dp_arch, profile=dp_prof)
            assert v1["station"] == "D35" and v1["mine_id"] == "dongpo"
            assert v1["items"][0]["confirmed_local_facts"]["active_incomer"] == "H101"
            assert v1["items"][0]["confirmed_local_facts"]["single_incomer_hours"] == 8
            assert "H101_rated_current_a" in v1["items"][0]["required_dependencies"]
            assert "F201_F202_rated_current_a" in v1["items"][1]["required_dependencies"]
            assert "F203_wiring_audit" in v1["items"][2]["required_dependencies"]

            # Test V02 tie-switch blocked -> must be conditionally_violated!
            v2_blocked = apply_supplement(
                v1,
                {
                    "station": "D35",
                    "source_doc": "东坡机电科核实单",
                    "parameters": {
                        "F201_F202_rated_current_a": 280.0,
                        "ambient_derating_factor": 0.9,
                        "u900_tie_switch_mode": "U900T联络开关故障闭锁不可切换",
                        "F203_wiring_audit": "现场核查接线正常极性正确",
                    },
                },
            )
            v2_map = {x["id"]: x for x in v2_blocked["items"]}
            assert v2_map["V02"]["status"] == "conditionally_violated", "Blocked tie switch must violate V02!"
            assert v2_map["V03"]["status"] == "evidence_conflict" and v2_map["V03"]["conditional_judgment"]["wiring_confirmed_normal"] is True, "Normal wiring against persistent negative P must preserve evidence_conflict!"
            print("[4/7] supplement.py (V01-V04) dynamic Dongpo discovery & V02/V03 negative-evidence semantics OK")

            # 5. Dynamic RAG Evidence Cards & Cross-Station Interception on Dongpo Mine
            cards = build_mine_evidence_cards(dp_arch, profile=dp_prof)
            assert len(cards) == 8
            assert all(c["card_id"].startswith("EVC-DONGPO-") for c in cards)
            for c in cards:
                assert "老洼地" not in c["title"] and "303" not in c["card_id"]
            rag = ConditionalRAGEngine(dp_arch, profile=dp_prof)
            r_cross = rag.retrieve("请查井下 U101 柜 12:00 的有功读数最大值是多少")
            assert r_cross["applicability_warnings"], "Expected cross-station U900 warning on Dongpo Mine!"
            r_hit = rag.retrieve("H101 单进线 H102 零功率", top_k=2)
            top_card = r_hit["evidence_cards"][0]
            top_id = top_card["card"]["card_id"] if "card" in top_card else top_card["card_id"]
            assert top_id == "EVC-DONGPO-H101-H102-SINGLE", f"Unexpected top card: {top_id}"
            print("[5/7] rag.py dynamic 8 Dongpo evidence cards & U900 cross-station applicability guard OK")

            # 6. Dynamic Graph Planner & 2-Round Cumulative Supplement Merge on Dongpo Mine
            store = CheckpointStore(Path(td) / "dp_ckpt.sqlite3")
            g = LaowadiAuditGraph(dp_arch, store=store, profile=dp_prof)
            s_susp = g.run("DP-V01", {"task_type": "single_incomer_overload_audit", "question": "核验东坡单进线过载"})
            assert s_susp["terminal_status"] == "needs_supplement"
            assert "H101_rated_current_a" in s_susp["missing_requirements"]
            # Round 1 partial supplement
            s_part = g.run(
                "DP-V01",
                {"task_type": "single_incomer_overload_audit", "question": "核验东坡单进线过载"},
                supplement_packet={
                    "station": "D35",
                    "source_doc": "东坡补证第1批",
                    "parameters": {"H101_rated_current_a": 200.0, "main_transformer_rated_kva": 8000.0},
                },
            )
            assert s_part["terminal_status"] == "needs_supplement"
            # Round 2 completes remaining parameters -> must merge with Round 1 (not lose Round 1!)
            s_done = g.run(
                "DP-V01",
                {"task_type": "single_incomer_overload_audit", "question": "核验东坡单进线过载"},
                supplement_packet={
                    "station": "D35",
                    "source_doc": "东坡补证第2批",
                    "parameters": {"ambient_derating_factor": 0.9, "switch_log_20261201": "H100母联合位"},
                },
            )
            assert s_done["terminal_status"] == "completed", f"2-round cumulative merge failed: {s_done['supplement_result']}"
            assert s_done["executed_in_this_run"] == []
            print("[6/7] graph.py dynamic Dongpo step planning & 2-round cumulative supplement merge OK")

    # 7. Verify SingleLineDiagram.jsx has ZERO hardcoded Laowadi cabinet constants & source DB unchanged
    sld_src = (ROOT / "workbench" / "src" / "SingleLineDiagram.jsx").read_text(encoding="utf-8")
    for forbidden in ("C1535_BUS1", "C1535_BUS2", "BUS1_FEEDERS", "BUS2_FEEDERS", '"303"', '"306"', '"1009"', '"1010"', '"1101"', '"1201"'):
        assert forbidden not in sld_src, f"Hardcoded Laowadi symbol still in SingleLineDiagram.jsx: {forbidden}"
    assert "/api/mine-profile" in sld_src and "sld_layout" in sld_src and "auto_balance_equations" in sld_src
    assert _sha256(DB) == before_db and _sha256(EVENT_DB) == before_ev
    print("[7/7] SingleLineDiagram.jsx zero-hardcoding check & source archives SHA-256 unchanged OK")
    print("\nALL 7 MULTI-MINE GENERALIZATION CHECKS PASSED!")


if __name__ == "__main__":
    main()
