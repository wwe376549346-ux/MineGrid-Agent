#!/usr/bin/env python3
"""Regression & E2E verification suite for Operator Duty Closure & P0 Safety Guards:

  1. Autonomous verification rejects sign-flipped values (-16.88 vs +16.88 kW) and
     cell-to-value mismatches; does not trigger switching orders on plain history lookups.
  2. Action ticket drafts never fabricate +2 tap / 10.662 kV / gb12325_compliant=True
     when pandapower solver fails or returns invalid recommendation; approval stays blocked.
  3. Live telemetry drill filters custom semantic voltage thresholds strictly by scope_cabinet.
  4. Deep investigation distinguishes targeted single-cabinet/hour goals vs station-wide weekly audits.
  5. Golden E2E duty closure on 2026-11-13 306 zero-load event:
     Proactive scan -> ticket claim -> Strict Audit 303 15:00 query -> cross-mode switch to
     Autonomous "那 306 柜呢？" without losing 2026-11-13 15:00 -> Shift handover with missing
     evidence note -> Shift B resume & context continuity.

Source archives `local/laowadi.sqlite3` and `local/laowadi_events.sqlite3` stay 100% read-only.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from laowadi import action_tickets
from laowadi.action_tickets import (
    ChecklistIncompleteError,
    approve_action_ticket,
    draft_action_ticket,
    toggle_checklist_item,
)
from laowadi.api import create_app
from laowadi.autonomous_agent import _compute_verification_badge, _infer_recommended_action
from laowadi.deep_research import export_deep_report, run_deep_investigation
from laowadi.ingest_live import stream_live_telemetry
from laowadi.memory_store import create_memory
from laowadi.monitor import EVENT_DB
from laowadi.queries import Archive
from laowadi.storage import DB, REPORT_STATION, connect
from laowadi.workbench import SOURCE_DIR, Workbench


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decode_sse(body: str) -> list[dict]:
    events: list[dict] = []
    for frame in body.split("\n\n"):
        data = [ln[5:].lstrip() for ln in frame.splitlines() if ln.startswith("data:")]
        if data:
            events.append(json.loads("\n".join(data)))
    return events


def verify_01_autonomous_sign_and_action_guards(archive: Archive) -> dict:
    row = archive.db.execute(
        """
        SELECT r.value, rp.filename || ' / ' || rp.sheet || '!' || r.source_col || r.source_row AS cell
        FROM reading r
        JOIN report rp ON rp.day = r.day
        WHERE r.station = ? AND r.cabinet = '1018' AND r.metric = 'P' AND r.time = '2026-11-16 21:00:00'
        LIMIT 1
        """,
        (REPORT_STATION,),
    ).fetchone()
    assert row is not None and float(row["value"]) < 0, f"Expected negative 1018 reading: {row}"
    actual_val = float(row["value"])
    cell_ref = row["cell"]
    trace = [
        {
            "tool": "get_hourly_reading",
            "arguments": {"cabinet": "1018", "metric": "P", "at": "2026-11-16 21:00:00"},
            "result": {"status": "ok", "cabinet": "1018", "metric": "P", "value": actual_val, "cell": cell_ref},
        }
    ]

    bad_prose = f"2026-11-16 21:00 时 1018 柜有功为 +{abs(actual_val):.2f} kW（原表 `{cell_ref}`）。"
    bad_badge = _compute_verification_badge(archive, bad_prose, trace, [])
    assert bad_badge["status"] == "failed", bad_badge
    assert bad_badge["business_trusted"] is False, bad_badge
    assert bad_badge["sign_mismatches"] >= 1, bad_badge
    assert bad_badge["cell_value_mismatches"] >= 1, bad_badge

    good_prose = f"2026-11-16 21:00 时 1018 柜有功为 {actual_val:.2f} kW（原表 `{cell_ref}`）。"
    good_badge = _compute_verification_badge(archive, good_prose, trace, [])
    assert good_badge["status"] == "verified", good_badge
    assert good_badge["business_trusted"] is True, good_badge
    assert good_badge["sign_mismatches"] == 0 and good_badge["cell_value_mismatches"] == 0, good_badge

    plain_rec = _infer_recommended_action(
        "2026年11月10日08:00，1005柜线电压Uab是多少？",
        {"cabinets": ["1005"], "days": ["2026-11-10"], "hours": ["08:00"]},
        "1005 柜 2026-11-10 08:00 Uab 为 10.45 kV，低于 GB/T 12325 的 10.70 kV 上限。",
        tool_trace=[
            {
                "tool": "get_hourly_reading",
                "arguments": {"cabinet": "1005", "metric": "Uab"},
                "result": {"status": "ok", "cabinet": "1005", "metric": "Uab", "value": 10.45},
            }
        ],
    )
    assert plain_rec is None, f"Plain history lookup should NOT draft an action ticket: {plain_rec}"
    return {"sign_flip_blocked": True, "signed_verified": True, "plain_lookup_no_ticket": True}


def verify_02_solver_failure_ticket_guard(archive: Archive, tmp: Path) -> dict:
    act_db = tmp / "actions_guard.sqlite3"
    with patch.object(action_tickets, "run_ac_power_flow", side_effect=RuntimeError("pandapower did not converge")):
        tk = draft_action_ticket(
            archive,
            kind="switching_order",
            target_cabinet="1006",
            at_time="2026-11-13 17:00:00",
            actor="duty-tester",
            db_path=act_db,
        )
    basis = tk["technical_basis"]
    assert basis["solver_status"] == "failed", basis
    assert basis["needs_verification"] is True, basis
    assert basis["recommended_tap"] is None, basis
    assert basis["expected_bus_10kv_ii_kv"] is None, basis
    assert basis["gb12325_compliant"] is None, basis
    assert tk["checklist_progress"]["verification_blocked"] is True
    assert tk["checklist_progress"]["ready_to_approve"] is False

    for item in tk["checklist"]:
        if item["required"]:
            tk = toggle_checklist_item(tk["ticket_id"], item["id"], checked=True, actor="chief", db_path=act_db)
    assert tk["checklist_progress"]["ready_to_approve"] is False, "Verification-blocked ticket must never be ready_to_approve"
    try:
        approve_action_ticket(tk["ticket_id"], actor="chief", db_path=act_db)
        raise AssertionError("Expected ChecklistIncompleteError when solver failed!")
    except ChecklistIncompleteError:
        pass
    return {"ticket_id": tk["ticket_id"], "solver_failure_blocked_approval": True}


def verify_03_cabinet_scoped_memory_thresholds(archive: Archive, tmp: Path) -> dict:
    mem_db = tmp / "mem_scope.sqlite3"
    create_memory(
        category="semantic",
        title="1018 专用电压告警线 10.50 kV",
        content="仅对 1018 柜按 10.50 kV 预警，不得影响 1006 柜。",
        scope_cabinet="1018",
        metric_threshold={"metric": "Uab", "operator": ">", "value": 10.50, "unit": "kV"},
        db_path=mem_db,
    )
    st_unscoped = stream_live_telemetry(archive, day="2026-11-13", speed_ms=0, live_db_path=tmp / "live_scope.sqlite3", memory_db_path=mem_db)
    assert st_unscoped["threshold_kv"] == 10.70, f"1018 rule leaked onto 1006 drill: {st_unscoped}"

    create_memory(
        category="semantic",
        title="1006 专用红线 10.65 kV",
        content="1006 柜内部红线 10.65 kV。",
        scope_cabinet="1006",
        metric_threshold={"metric": "Uab", "operator": ">", "value": 10.65, "unit": "kV"},
        db_path=mem_db,
    )
    st_scoped = stream_live_telemetry(archive, day="2026-11-13", speed_ms=0, live_db_path=tmp / "live_scope.sqlite3", memory_db_path=mem_db)
    assert st_scoped["threshold_kv"] == 10.65, f"Expected 1006-scoped threshold 10.65: {st_scoped}"
    return {"isolated_1018_threshold": 10.70, "applied_1006_threshold": 10.65}


def verify_04_targeted_vs_weekly_deep_research(archive: Archive, tmp: Path) -> dict:
    rep_dir = tmp / "deep_reports_scope"
    mem_db = tmp / "deep_mem.sqlite3"
    r_1018 = run_deep_investigation(
        archive,
        "只查 2026-11-16 21:00 1018 柜有功读数",
        memory_db_path=mem_db,
        reports_dir=rep_dir,
    )
    r_303 = run_deep_investigation(
        archive,
        "只查 2026-11-13 15:00 303 柜电流",
        memory_db_path=mem_db,
        reports_dir=rep_dir,
    )
    assert r_1018["scope"] == "targeted" and len(r_1018["sections"]) == 4, r_1018
    assert r_303["scope"] == "targeted" and len(r_303["sections"]) == 4, r_303
    assert r_1018["sections"] != r_303["sections"]
    assert set(r_1018["evidence_cells"]) != set(r_303["evidence_cells"])
    assert r_1018["kpis"]["target_cabinets"] == "1018" and r_303["kpis"]["target_cabinets"] == "303"
    assert "-16.88" in r_1018["markdown"] or "1018" in r_1018["markdown"]
    md_txt, _, _ = export_deep_report(r_1018["report_id"], fmt="md", reports_dir=rep_dir)
    html_txt, _, _ = export_deep_report(r_303["report_id"], fmt="html", reports_dir=rep_dir)
    assert "定向专项核查" in md_txt and "定向专项核查报告" in html_txt

    r_weekly = run_deep_investigation(
        archive,
        "全面排查老洼地 7 天所有供电隐患",
        memory_db_path=mem_db,
        reports_dir=rep_dir,
    )
    assert r_weekly["scope"] == "station_weekly" and len(r_weekly["sections"]) == 6
    return {
        "r_1018_id": r_1018["report_id"],
        "r_303_id": r_303["report_id"],
        "weekly_id": r_weekly["report_id"],
    }


def verify_05_golden_306_e2e_closure(tmp: Path) -> dict:
    wb = Workbench(
        DB,
        EVENT_DB,
        SOURCE_DIR,
        None,
        reviews_db=tmp / "reviews.sqlite3",
        sessions_db=tmp / "sessions.sqlite3",
        audit_db=tmp / "audit.sqlite3",
        jobs_db=tmp / "jobs.sqlite3",
        dag_checkpoint_db=tmp / "dag.sqlite3",
    )
    app = create_app(wb, require_auth=False)
    sid = "duty-306-e2e"
    try:
        with TestClient(app) as client:
            # 1. Proactive scan detects 2026-11-13 306 zero-load ticket with 1113.xls evidence
            scan = client.post("/api/proactive/scan", json={"max_new_tickets": 12})
            assert scan.status_code == 200, scan.text
            listed = client.get("/api/tickets?status=all&limit=20").json()["tickets"]
            tk306 = next(
                (t for t in listed if t["source_finding_id"] == "HLTH-MODE-SINGLE-01"),
                None,
            )
            assert tk306 is not None, f"Missing 306 single-incomer ticket in {listed}"
            tid = tk306["ticket_id"]
            detail_306 = client.get(f"/api/tickets/{tid}").json()
            assert any("1113.xls" in c for c in (detail_306.get("evidence_cells") or [])), detail_306

            # 2. Shift A claims ticket
            cl = client.post(
                f"/api/tickets/{tid}/transition",
                json={"action": "claim", "actor": "白班值班长", "shift_name": "早班", "expected_revision": tk306["revision"]},
            )
            assert cl.status_code == 200 and cl.json()["ticket"]["status"] == "claimed", cl.text
            rev = cl.json()["ticket"]["revision"]

            # 3. Strict Audit turn anchored to the ticket + 2026-11-13 15:00 303柜有功
            diag = client.post(
                "/api/diagnose",
                json={
                    "sessionId": sid,
                    "mode": "local",
                    "question": "2026年11月13日15:00，303柜的有功功率是多少？请给出原表出处。",
                    "context": {
                        "ticket_id": tid,
                        "finding_id": tk306["source_finding_id"],
                        "ticket_title": tk306["title"],
                        "cabinet": tk306["cabinet"],
                        "at_time": tk306["at_time"],
                    },
                },
            )
            assert diag.status_code == 200, diag.text
            diag_res = diag.json()
            assert "5710.12" in json.dumps(diag_res, ensure_ascii=False)
            assert "1113.xls / New Sheet1!D19" in json.dumps(diag_res, ensure_ascii=False)
            assert diag_res["context_snapshot"]["active_anchors"]["ticket_id"] == tid

            # 4. Switch to Autonomous Mode in SAME session and ask "那 306 柜呢？"
            auto_resp = client.post(
                "/api/agent/stream",
                json={"sessionId": sid, "mode": "local", "question": "那 306 柜呢？"},
            )
            assert auto_resp.status_code == 200, auto_resp.text
            events = _decode_sse(auto_resp.text)
            done = next(ev["result"] for ev in events if ev.get("type") == "done")
            ans = done["answer_markdown"]
            assert ("0.0 kW" in ans or "0 kW" in ans) and "1113.xls" in ans, f"Did not stay on 2026-11-13 15:00 306: {ans}"
            assert "2026-11-12" not in ans and "反向有功" not in ans, f"Autonomous leaked to 11-12 reverse power: {ans}"
            assert done["verification_badge"]["status"] == "verified", done["verification_badge"]
            anchors = done["active_anchors"]
            assert anchors["days"] == ["2026-11-13"] and anchors["hours"] == ["15:00"], anchors
            assert anchors["cabinets"] == ["306"] and anchors["ticket_id"] == tid, anchors

            # 5. Shift A hands over with missing-evidence note
            ho_note = "待补 2026-11-13 306 开关操作票及 303 载流铭牌"
            ho = client.post(
                f"/api/tickets/{tid}/transition",
                json={"action": "handover", "actor": "白班值班长", "shift_name": "早班", "note": ho_note, "expected_revision": rev},
            )
            assert ho.status_code == 200 and ho.json()["ticket"]["status"] == "handed_over", ho.text
            rev = ho.json()["ticket"]["revision"]

            # 6. Shift B reloads ticket & session and resumes
            reloaded_tk = client.get(f"/api/tickets/{tid}").json()
            assert reloaded_tk["status"] == "handed_over" and ho_note in reloaded_tk["handover_note"]
            cl_b = client.post(
                f"/api/tickets/{tid}/transition",
                json={"action": "claim", "actor": "夜班值班长", "shift_name": "夜班", "note": "已接班跟进", "expected_revision": rev},
            )
            assert cl_b.status_code == 200 and cl_b.json()["ticket"]["status"] == "claimed"

            reloaded_sess = client.get(f"/api/sessions/{sid}").json()
            assert len(reloaded_sess["turns"]) == 2
            assert reloaded_sess["active_anchors"]["ticket_id"] == tid
            assert reloaded_sess["active_anchors"]["days"] == ["2026-11-13"]
            assert reloaded_sess["active_anchors"]["hours"] == ["15:00"]
            return {"ticket_id": tid, "session_id": sid, "turns": len(reloaded_sess["turns"]), "closure_verified": True}
    finally:
        wb.db_manager.dispose()


def main() -> int:
    before_db, before_ev = _sha256(DB), _sha256(EVENT_DB)
    with TemporaryDirectory(prefix="laowadi_duty_") as td:
        tmp = Path(td)
        with closing(connect(DB)) as conn:
            archive = Archive(conn, EVENT_DB)
            res = {
                "step1_sign_and_action_guards": verify_01_autonomous_sign_and_action_guards(archive),
                "step2_solver_failure_ticket_guard": verify_02_solver_failure_ticket_guard(archive, tmp),
                "step3_cabinet_scoped_memory": verify_03_cabinet_scoped_memory_thresholds(archive, tmp),
                "step4_targeted_vs_weekly_research": verify_04_targeted_vs_weekly_deep_research(archive, tmp),
                "step5_golden_306_e2e_closure": verify_05_golden_306_e2e_closure(tmp),
            }
    assert _sha256(DB) == before_db and _sha256(EVENT_DB) == before_ev, "Golden SQLite archives mutated!"
    print(json.dumps({"status": "ok", "archives_unchanged": True, **res}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
