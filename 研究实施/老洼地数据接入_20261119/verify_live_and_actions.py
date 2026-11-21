"""Verification suite for Sprint 3: Live Telemetry Drill Stream, Incremental CSV Upload & Action Ticket Approval."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from laowadi.action_tickets import (
    ChecklistIncompleteError,
    approve_action_ticket,
    dispatch_action_webhook,
    draft_action_ticket,
    get_action_ticket,
    list_action_tickets,
    reject_action_ticket,
    toggle_checklist_item,
)
from laowadi.ingest_live import ingest_csv_report, list_live_uploads, stream_live_telemetry
from laowadi.memory_store import create_memory
from laowadi.monitor import EVENT_DB
from laowadi.queries import Archive
from laowadi.storage import DB, connect


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_live_ingestion_and_stream(archive: Archive, tmp: Path) -> dict:
    live_db = tmp / "live.sqlite3"
    mem_db = tmp / "mem.sqlite3"

    # 1. Incremental CSV upload (wide format) into isolated sandbox DB
    csv_sample = "\n".join([
        "time,cabinet,Uab,Ia,P,cos",
        "2026-11-17 08:00:00,1006,10.88,120.0,1950.0,0.92",
        "2026-11-17 09:00:00,1006,10.96,115.0,-42.5,0.91",
        "2026-11-17 08:00:00,1010,10.85,92.0,980.0,0.64",
        "2026-11-17 09:00:00,303,36.2,92.5,5120.0,0.93",
        "2026-11-17 09:00:00,306,36.2,0.0,0.0,0.0",
    ])
    up = ingest_csv_report(csv_sample, filename="laowadi_20261117_new.csv", live_db_path=live_db)
    assert up["status"] == "ok" and up["day"] == "2026-11-17"
    assert up["row_count"] == 20 and up["cabinet_count"] == 4
    kinds = {f["kind"] for f in up["findings"]}
    assert {"overvoltage", "reverse_power", "low_power_factor"} <= kinds, f"Missing findings: {kinds}"
    assert len(list_live_uploads(live_db_path=live_db)) == 1

    # 2. Stream uploaded incremental day (fast drill speed_ms=0)
    ev_up: list[dict] = []
    st_up = stream_live_telemetry(archive, day="2026-11-17", speed_ms=0, live_db_path=live_db, memory_db_path=mem_db, on_event=ev_up.append)
    assert st_up["source"] == "live_upload" and st_up["ticks_emitted"] == 2
    assert st_up["alerts_count"] >= 2

    # 3. Stream archived day 2026-11-13 with custom semantic memory threshold 10.65 kV
    create_memory(
        category="semantic",
        title="1006 内部红线 10.65 kV",
        content="10 kV II 段内部严控红线 10.65 kV",
        scope_cabinet="1006",
        metric_threshold={"metric": "Uab", "operator": ">", "value": 10.65, "unit": "kV"},
        db_path=mem_db,
    )
    ev_arch: list[dict] = []
    st_arch = stream_live_telemetry(archive, day="2026-11-13", speed_ms=0, live_db_path=live_db, memory_db_path=mem_db, on_event=ev_arch.append)
    assert st_arch["source"] == "archive" and st_arch["ticks_emitted"] == 24
    assert st_arch["threshold_kv"] == 10.65
    assert any(a["kind"] == "overvoltage" and a["recommended_ticket"]["kind"] == "switching_order" for a in st_arch["alerts"])
    assert any(a["kind"] == "single_incomer_zero" for a in st_arch["alerts"])

    return {
        "upload_id": up["upload_id"],
        "upload_findings": len(up["findings"]),
        "archive_stream_ticks": st_arch["ticks_emitted"],
        "archive_stream_alerts": st_arch["alerts_count"],
        "custom_threshold_applied": st_arch["threshold_kv"],
    }


def verify_action_ticket_workflow(archive: Archive, tmp: Path) -> dict:
    act_db = tmp / "actions.sqlite3"

    # 1. Draft switching_order ticket backed by real pandapower & archive cells
    tk = draft_action_ticket(
        archive,
        kind="switching_order",
        target_cabinet="1006",
        at_time="2026-11-13 17:00:00",
        actor="shift-lead",
        db_path=act_db,
    )
    assert tk["status"] == "draft" and tk["risk_level"] == "high"
    assert tk["checklist_progress"]["ready_to_approve"] is False
    assert len(tk["evidence_cells"]) >= 1

    # 2. HARD SAFETY GATE: approving before checking all required items MUST raise ChecklistIncompleteError!
    try:
        approve_action_ticket(tk["ticket_id"], actor="chief-dispatcher", db_path=act_db)
        raise AssertionError("Expected ChecklistIncompleteError when checklist is incomplete!")
    except ChecklistIncompleteError:
        pass

    # 3. Dispatching before approval MUST also be blocked!
    try:
        dispatch_action_webhook(tk["ticket_id"], dry_run=True, actor="chief-dispatcher", db_path=act_db)
        raise AssertionError("Expected ChecklistIncompleteError when dispatching draft ticket!")
    except ChecklistIncompleteError:
        pass

    # 4. Check all required items one by one -> ready_to_approve becomes True -> approve -> dry-run dispatch
    for item in tk["checklist"]:
        if item["required"]:
            tk = toggle_checklist_item(tk["ticket_id"], item["id"], checked=True, actor="chief-dispatcher", db_path=act_db)
    assert tk["checklist_progress"]["ready_to_approve"] is True

    approved = approve_action_ticket(tk["ticket_id"], actor="chief-dispatcher", comment="现场复核无误准予签发", db_path=act_db)
    assert approved["status"] == "approved" and approved["approved_by"] == "chief-dispatcher"

    # 5. Locked after approval: toggling checklist must fail
    try:
        toggle_checklist_item(tk["ticket_id"], "chk_1", checked=False, actor="chief-dispatcher", db_path=act_db)
        raise AssertionError("Expected ChecklistIncompleteError when mutating approved ticket!")
    except ChecklistIncompleteError:
        pass

    dispatched = dispatch_action_webhook(tk["ticket_id"], dry_run=True, actor="chief-dispatcher", db_path=act_db)
    assert dispatched["ticket"]["status"] == "dispatched"
    assert dispatched["dispatch"]["status"] == "dry_run_recorded"

    # 6. Draft defect_notice & field_verification + reject flow
    tk2 = draft_action_ticket(archive, kind="defect_notice", target_cabinet="1018", at_time="2026-11-16 21:00:00", db_path=act_db)
    rej = reject_action_ticket(tk2["ticket_id"], actor="chief-dispatcher", reason="待补充相位表测试后再办票", db_path=act_db)
    assert rej["status"] == "rejected"

    tk3 = draft_action_ticket(archive, kind="field_verification", target_cabinet="1010", db_path=act_db)
    assert tk3["status"] == "draft" and len(list_action_tickets(db_path=act_db)) == 3
    assert get_action_ticket(tk3["ticket_id"], db_path=act_db) is not None

    return {
        "switching_order_id": tk["ticket_id"],
        "safety_gate_blocked_unapproved": True,
        "approved_and_dispatched": True,
        "total_tickets_tested": 3,
    }


def main():
    before_db = sha256(DB)
    before_ev = sha256(EVENT_DB)
    with TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        with closing(connect(DB)) as db:
            archive = Archive(db, EVENT_DB)
            live_res = verify_live_ingestion_and_stream(archive, tmp)
            act_res = verify_action_ticket_workflow(archive, tmp)
    assert sha256(DB) == before_db and sha256(EVENT_DB) == before_ev, "Golden archives mutated!"
    print(
        json.dumps(
            {
                "status": "ok",
                "archives_unchanged": True,
                "live_ingestion_and_stream": live_res,
                "action_tickets": act_res,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
