"""Regression cases for inspection backlog, outbox ownership, withdrawal and ticket transitions."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from laowadi.monitor import EVENT_DB
from laowadi.proactive import IncidentTicketStore, ProactiveWatcherDaemon, TicketStateConflict
from laowadi.queries import Archive
from laowadi.storage import DB, connect


@contextmanager
def archive():
    with closing(connect(DB)) as conn:
        yield Archive(conn, EVENT_DB)


def add_ticket(store, finding_id="LW-000010", **kwargs):
    ticket, created = store.create_ticket_with_outbox(
        source_finding_id=finding_id, title="303 柜有功变化",
        cabinet="303", at_time="2026-11-10 12:00:00",
        evidence_cells=["1110.xls / New Sheet1!D15", "1110.xls / New Sheet1!D16"], **kwargs,
    )
    assert created
    return ticket


def correction(store):
    return store.reconcile_correction(
        cabinet="303", at_time="2026-11-10 12:00:00", prev_p_kw=1071.84, curr_p_kw=1071.84,
        reason="隔离验收中的模拟订正",
    )


def verify_backlog(store):
    watcher = ProactiveWatcherDaemon(archive, store)
    scans = [watcher.scan_once(max_new_tickets=4) for _ in range(5)]
    assert [scan["created_count"] for scan in scans] == [4, 4, 4, 2, 0], scans
    assert scans[-1]["skipped_existing"] == 14
    created = [ticket for scan in scans for ticket in scan["created_tickets"]]
    assert [ticket["severity"] for ticket in created] == ["high"] * 7 + ["medium"] * 7
    tickets = store.list_tickets()
    assert len(tickets) == len({ticket["dedup_key"] for ticket in tickets}) == 14
    assert store.sla_metrics()["outbox_delivered"] == 14


def verify_claim_and_retry(store):
    ticket = add_ticket(store, channels=[{"channel": "in_app"}, {"channel": "in_app"}])
    original = store.get_ticket_detail(ticket["ticket_id"])["outbox"]
    assert len(original) == 1
    outbox_id = original[0]["outbox_id"]
    entered, release = Event(), Event()
    keys = []

    def sender(_channel, _target_url, payload):
        keys.append(payload["notification_id"])
        entered.set()
        assert release.wait(timeout=5)
        return {"ok": True}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.dispatch_outbox, sender=sender)
        try:
            assert entered.wait(timeout=5)
            second = pool.submit(store.dispatch_outbox, sender=sender, force_retry_now=True)
            assert second.result(timeout=5)["processed"] == 0
        finally:
            release.set()
        assert first.result(timeout=5)["delivered"] == 1
    assert keys == [outbox_id]

    add_ticket(store, "fixture-retry")
    seen = []
    def unsuccessful(_channel, _target_url, payload):
        seen.append(payload["notification_id"])
        return {"ok": False}

    assert store.dispatch_outbox(sender=unsuccessful)["failed"] == 1
    assert store.dispatch_outbox(sender=unsuccessful)["processed"] == 0
    assert store.dispatch_outbox(sender=unsuccessful, force_retry_now=True)["failed"] == 1
    assert len(set(seen)) == 1 and len(seen) == 2

    unfinished = add_ticket(store, "fixture-expiry")
    lease_id = store.get_ticket_detail(unfinished["ticket_id"])["outbox"][0]["outbox_id"]
    assert store._claim_outbox(lease_id, "old-lease", False)
    old_attempt = store._start_outbox(lease_id, "old-lease")
    assert old_attempt
    with store._write() as conn:
        conn.execute("UPDATE alert_outbox SET claim_expires_epoch = 1 WHERE outbox_id = ?", (lease_id,))
    assert store.dispatch_outbox()["delivered"] == 1
    assert store._finish_outbox(old_attempt, "old-lease", None, "old worker resumed") is None
    settled = store.get_ticket_detail(unfinished["ticket_id"])["outbox"][0]
    assert settled["attempts"] == 2 and settled["status"] == "delivered"


def verify_withdrawal(store):
    pending = add_ticket(store, "fixture-pending")
    assert correction(store)["superseded_ticket_ids"] == [pending["ticket_id"]]
    assert store.dispatch_outbox(force_retry_now=True)["processed"] == 0
    assert store.get_ticket_detail(pending["ticket_id"])["outbox"][0]["status"] == "cancelled"

    reserved = add_ticket(store, "fixture-reserved")
    entered, release = Event(), Event()
    original_start = store._start_outbox
    def pause_before_send(*args):
        entered.set()
        assert release.wait(timeout=5)
        return original_start(*args)

    with patch.object(store, "_start_outbox", side_effect=pause_before_send), ThreadPoolExecutor() as pool:
        future = pool.submit(store.dispatch_outbox)
        try:
            assert entered.wait(timeout=5)
            assert correction(store)["superseded_ticket_ids"] == [reserved["ticket_id"]]
        finally:
            release.set()
        assert future.result(timeout=5)["processed"] == 0
    row = store.get_ticket_detail(reserved["ticket_id"])["outbox"][0]
    assert row["status"] == "cancelled" and row["attempts"] == 0

    delivered = add_ticket(store, "fixture-delivered")
    assert store.dispatch_outbox()["delivered"] == 1
    receipt = store.get_ticket_detail(delivered["ticket_id"])["outbox"][0]["receipt_sha256"]
    assert correction(store)["superseded_ticket_ids"] == [delivered["ticket_id"]]
    row = store.get_ticket_detail(delivered["ticket_id"])["outbox"][0]
    assert row["status"] == "revoked" and row["receipt_sha256"] == receipt and row["withdrawn_at"]

    sending = add_ticket(store, "fixture-in-flight")
    entered, release = Event(), Event()
    def late_receipt(*_args):
        entered.set()
        assert release.wait(timeout=5)
        return {"ok": True}

    with ThreadPoolExecutor() as pool:
        future = pool.submit(store.dispatch_outbox, sender=late_receipt)
        try:
            assert entered.wait(timeout=5)
            assert correction(store)["superseded_ticket_ids"] == [sending["ticket_id"]]
        finally:
            release.set()
        result = future.result(timeout=5)
        assert result["revoked"] == 1 and result["delivered"] == 0
    row = store.get_ticket_detail(sending["ticket_id"])["outbox"][0]
    assert row["status"] == "revoked" and row["delivered_at"] and row["receipt_sha256"]
    assert correction(store)["superseded_count"] == 0
    assert store.dispatch_outbox(force_retry_now=True)["processed"] == 0


def verify_transitions(store):
    ticket = add_ticket(store)
    ticket_id = ticket["ticket_id"]

    def claim(actor):
        try:
            return store.transition_ticket(ticket_id, "claim", actor=actor, expected_revision=ticket["revision"])
        except TicketStateConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = [result for result in pool.map(claim, ("早班", "中班")) if result]
    assert len(claimed) == 1
    assert claimed[0]["revision"] == ticket["revision"] + 1

    try:
        store.transition_ticket(ticket_id, "close", note=" ")
    except ValueError:
        pass
    else:
        raise AssertionError("Closed without resolution evidence")
    closed = store.transition_ticket(ticket_id, "close", note="隔离验收销项依据")
    assert closed["allowed_actions"] == ["reopen"]
    try:
        store.transition_ticket(ticket_id, "claim")
    except TicketStateConflict:
        pass
    else:
        raise AssertionError("Claimed a closed ticket")
    reopened = store.transition_ticket(ticket_id, "reopen", note="隔离验收复开依据", expected_revision=closed["revision"])
    assert not reopened["assignee"] and reopened["closed_at"] is None
    again = store.transition_ticket(ticket_id, "claim", actor="下一班", expected_revision=reopened["revision"])
    assert again["status"] == "claimed" and again["closed_at"] is None
    history = store.get_ticket_detail(ticket_id)["logs"]
    assert [log["action"] for log in history] == ["create", "claim", "close", "reopen", "claim"]
    assert history[3]["payload"]["previous_closed_at"] == closed["closed_at"]
    assert history[2]["note"] == "隔离验收销项依据"


def verify_legacy_upgrade(path):
    store = IncidentTicketStore(path)
    old = add_ticket(store, "fixture-previous-withdrawal")
    new = add_ticket(store, "fixture-previous-closure")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE incident_ticket SET status='superseded' WHERE ticket_id=?", (old["ticket_id"],))
        conn.execute("UPDATE incident_ticket SET status='claimed', closed_at=created_at WHERE ticket_id=?", (new["ticket_id"],))
        conn.execute("ALTER TABLE incident_ticket DROP COLUMN revision")
        for column in ("withdrawn_at", "claim_token", "claim_expires_epoch"):
            conn.execute(f"ALTER TABLE alert_outbox DROP COLUMN {column}")
    upgraded = IncidentTicketStore(path)
    assert upgraded.get_ticket_detail(old["ticket_id"])["outbox"][0]["status"] == "cancelled"
    assert upgraded.get_ticket_detail(new["ticket_id"])["closed_at"] is None
    assert upgraded.get_ticket_detail(new["ticket_id"])["revision"] == 1


def main():
    with TemporaryDirectory(prefix="laowadi-proactive-") as tmp:
        root = Path(tmp)
        for index, verify in enumerate((verify_backlog, verify_claim_and_retry, verify_withdrawal, verify_transitions), 23):
            verify(IncidentTicketStore(root / f"case-{index}.sqlite3"))
            print(f"PASS #{index}")
        verify_legacy_upgrade(root / "legacy.sqlite3")
        print("PASS legacy runtime upgrade")


if __name__ == "__main__":
    main()
