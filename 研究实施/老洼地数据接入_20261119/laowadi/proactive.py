"""Evidence-backed inspection, shift tickets and transactional notification delivery."""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable
from urllib.request import Request, urlopen
import uuid

from laowadi.db.postgres import connect_runtime, watcher_slot
from laowadi.logging_config import get_logger
from laowadi.storage import ROOT

logger = get_logger("laowadi.proactive")

PROACTIVE_DB = ROOT / "local" / "laowadi_proactive.sqlite3"

TICKET_STATUSES = {
    "open": "待认领",
    "claimed": "已认领处理中",
    "supplemented": "已补证复算",
    "disputed": "人工否决/冲突保留",
    "escalated": "超时/高危升级",
    "handed_over": "已交接下一班",
    "closed": "已销项",
    "reopened": "已复开",
    "superseded": "数据更正已替代",
}

ACTION_TO_STATUS = {
    "claim": "claimed",
    "supplement": "supplemented",
    "dispute": "disputed",
    "escalate": "escalated",
    "handover": "handed_over",
    "close": "closed",
    "reopen": "reopened",
    "supersede": "superseded",
}

TICKET_ACTIONS = {
    "open": ("claim", "dispute", "escalate", "supersede"),
    "claimed": ("supplement", "dispute", "escalate", "handover", "close", "supersede"),
    "supplemented": ("supplement", "dispute", "escalate", "handover", "close", "supersede"),
    "disputed": ("claim", "supplement", "escalate", "handover", "close", "supersede"),
    "escalated": ("claim", "supplement", "dispute", "handover", "close", "supersede"),
    "handed_over": ("claim", "supplement", "dispute", "escalate", "close", "supersede"),
    "closed": ("reopen",),
    "reopened": ("claim", "dispute", "escalate", "supersede"),
    "superseded": (),
}
NOTE_REQUIRED_ACTIONS = ("close", "reopen", "supersede")

SUPPORTED_CHANNELS = ("in_app", "webhook", "wecom", "dingtalk", "cmms_export")
OUTBOX_LEASE_SECONDS = 60.0
OUTBOX_DUE = """
    attempts < max_attempts
    AND (
        (status IN ('pending', 'failed') AND (? OR next_retry_at = '' OR next_retry_at <= ?))
        OR (status IN ('claimed', 'sending') AND claim_expires_epoch <= ?)
    )
    AND EXISTS (
        SELECT 1 FROM incident_ticket
        WHERE incident_ticket.ticket_id = alert_outbox.ticket_id AND incident_ticket.status <> 'superseded'
    )
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS incident_ticket (
    ticket_id TEXT PRIMARY KEY,
    dedup_key TEXT UNIQUE,
    source_finding_id TEXT NOT NULL,
    cabinet TEXT NOT NULL DEFAULT '',
    at_time TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'open',
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    evidence_cells_json TEXT NOT NULL DEFAULT '[]',
    pre_diagnosis_json TEXT,
    assignee TEXT NOT NULL DEFAULT '',
    shift_name TEXT NOT NULL DEFAULT '当前班次',
    sla_deadline_at TEXT NOT NULL DEFAULT '',
    handover_note TEXT NOT NULL DEFAULT '',
    resolution_note TEXT NOT NULL DEFAULT '',
    superseded_reason TEXT NOT NULL DEFAULT '',
    archive_version TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    acknowledged_at TEXT,
    closed_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS incident_ticket_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    action TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    shift_name TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_outbox (
    outbox_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    event_id TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT 'in_app',
    target_url TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    last_error TEXT NOT NULL DEFAULT '',
    next_retry_at TEXT NOT NULL DEFAULT '',
    delivered_at TEXT,
    receipt_sha256 TEXT NOT NULL DEFAULT '',
    claim_token TEXT NOT NULL DEFAULT '',
    claim_expires_epoch REAL NOT NULL DEFAULT 0,
    withdrawn_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watcher_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_scan_at TEXT NOT NULL DEFAULT '',
    last_archive_version TEXT NOT NULL DEFAULT '',
    scans_total INTEGER NOT NULL DEFAULT 0,
    tickets_created_total INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_ticket_status ON incident_ticket(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ticket_cab_time ON incident_ticket(cabinet, at_time);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON alert_outbox(status, created_at ASC);
"""


class TicketStateConflict(ValueError):
    """A submitted action conflicts with the current ticket revision or state."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _sha256_json(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def format_channel_payload(channel: str, ticket: dict[str, Any]) -> dict[str, Any]:
    """Build channel-specific outbound alert message (WeCom / DingTalk / Webhook / CMMS / In-App)."""
    sev_cn = {"high": "高危", "medium": "中危", "info": "提示"}.get(ticket.get("severity", "medium"), "关注")
    title = f"[{sev_cn}] {ticket.get('title', '配电网异常')}"
    cells = ", ".join((ticket.get("evidence_cells") or [])[:4]) or "详见工单"
    body_md = (
        f"### {title}\n"
        f"- **工单编号**: `{ticket['ticket_id']}`\n"
        f"- **设备/时刻**: `{ticket.get('cabinet') or '-'}` / `{ticket.get('at_time') or '-'}`\n"
        f"- **状态**: `{TICKET_STATUSES.get(ticket.get('status', 'open'), ticket.get('status'))}`\n"
        f"- **摘要**: {ticket.get('summary', '')}\n"
        f"- **原表单元格出处**: `{cells}`\n"
    )
    if channel == "wecom":
        return {"msgtype": "markdown", "markdown": {"content": body_md}}
    if channel == "dingtalk":
        return {"msgtype": "markdown", "markdown": {"title": title, "text": body_md}}
    if channel == "cmms_export":
        pre_diagnosis = ticket.get("pre_diagnosis") or {}
        legacy_report = pre_diagnosis.get("report") or {}
        return {
            "workorder_schema": "coal_power_cmms_v1",
            "ticket_id": ticket["ticket_id"],
            "priority": "P1" if ticket.get("severity") == "high" else "P2",
            "equipment": ticket.get("cabinet", ""),
            "occurred_at": ticket.get("at_time", ""),
            "title": ticket.get("title", ""),
            "description": ticket.get("summary", ""),
            "evidence_cells": ticket.get("evidence_cells") or [],
            "recommended_actions": pre_diagnosis.get("recommended_actions") or legacy_report.get("field_sop") or [],
            "missing_evidence": pre_diagnosis.get("missing_evidence", legacy_report.get("missing_evidence") or []),
        }
    return {
        "event": "laowadi_incident_alert",
        "ticket_id": ticket["ticket_id"],
        "severity": ticket.get("severity", "medium"),
        "cabinet": ticket.get("cabinet", ""),
        "at_time": ticket.get("at_time", ""),
        "title": ticket.get("title", ""),
        "summary": ticket.get("summary", ""),
        "evidence_cells": ticket.get("evidence_cells") or [],
        "status": ticket.get("status", "open"),
    }


def _default_channel_sender(
    channel: str, target_url: str, payload: dict[str, Any], *, notification_id: str,
) -> dict[str, Any]:
    """Deliver outbound notification to in_app/cmms locally or HTTP POST to webhook/wecom/dingtalk."""
    if channel in ("in_app", "cmms_export"):
        return {"ok": True, "mode": channel, "ack": "stored_locally"}
    if not target_url:
        raise RuntimeError(f"通道 {channel} 缺少目标 webhook URL")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(target_url, data=data, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8",
        "Idempotency-Key": notification_id,
    })
    with urlopen(req, timeout=8) as resp:
        code = getattr(resp, "status", 200)
        body = resp.read(512).decode("utf-8", errors="ignore")
        if code >= 400:
            raise RuntimeError(f"HTTP {code}: {body[:160]}")
        return {"ok": True, "http_status": code, "body": body[:160]}


class IncidentTicketStore:
    """Shift tickets and an outbox on the configured SQLite or PostgreSQL runtime."""

    def __init__(self, path: Path | str = PROACTIVE_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return connect_runtime(self.path, writable=True, wal=True)

    @contextmanager
    def _write(self):
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    def _init_db(self) -> None:
        with self._write() as conn:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            ticket_columns = {row["name"] for row in conn.execute("PRAGMA table_info(incident_ticket)")}
            if "revision" not in ticket_columns:
                conn.execute("ALTER TABLE incident_ticket ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
                conn.execute("UPDATE incident_ticket SET closed_at = NULL WHERE status NOT IN ('closed', 'superseded')")
            outbox_columns = {row["name"] for row in conn.execute("PRAGMA table_info(alert_outbox)")}
            if "withdrawn_at" not in outbox_columns:
                conn.execute("ALTER TABLE alert_outbox ADD COLUMN withdrawn_at TEXT")
                conn.execute("""
                    UPDATE alert_outbox
                    SET status = CASE WHEN status = 'delivered' THEN 'revoked' ELSE 'cancelled' END,
                        withdrawn_at = (SELECT updated_at FROM incident_ticket WHERE incident_ticket.ticket_id = alert_outbox.ticket_id),
                        next_retry_at = ''
                    WHERE EXISTS (
                        SELECT 1 FROM incident_ticket
                        WHERE incident_ticket.ticket_id = alert_outbox.ticket_id AND incident_ticket.status = 'superseded'
                    )
                """)
            if "claim_token" not in outbox_columns:
                conn.execute("ALTER TABLE alert_outbox ADD COLUMN claim_token TEXT NOT NULL DEFAULT ''")
            if "claim_expires_epoch" not in outbox_columns:
                conn.execute("ALTER TABLE alert_outbox ADD COLUMN claim_expires_epoch REAL NOT NULL DEFAULT 0")
            conn.execute(
                "INSERT OR IGNORE INTO watcher_state(id, last_scan_at, last_archive_version, scans_total, tickets_created_total, last_error) VALUES (1, '', '', 0, 0, '')"
            )

    @staticmethod
    def _row_to_ticket(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "ticket_id": row["ticket_id"],
            "dedup_key": row["dedup_key"],
            "source_finding_id": row["source_finding_id"],
            "cabinet": row["cabinet"],
            "at_time": row["at_time"],
            "severity": row["severity"],
            "status": row["status"],
            "status_label": TICKET_STATUSES.get(row["status"], row["status"]),
            "title": row["title"],
            "summary": row["summary"],
            "evidence_cells": json.loads(row["evidence_cells_json"] or "[]"),
            "pre_diagnosis": json.loads(row["pre_diagnosis_json"]) if row["pre_diagnosis_json"] else None,
            "assignee": row["assignee"],
            "shift_name": row["shift_name"],
            "sla_deadline_at": row["sla_deadline_at"],
            "handover_note": row["handover_note"],
            "resolution_note": row["resolution_note"],
            "superseded_reason": row["superseded_reason"],
            "archive_version": row["archive_version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "acknowledged_at": row["acknowledged_at"],
            "closed_at": row["closed_at"],
            "revision": row["revision"],
            "allowed_actions": list(TICKET_ACTIONS.get(row["status"], ())),
            "note_required_actions": [action for action in NOTE_REQUIRED_ACTIONS if action in TICKET_ACTIONS.get(row["status"], ())],
        }

    def create_ticket_with_outbox(
        self,
        *,
        source_finding_id: str,
        title: str,
        summary: str = "",
        cabinet: str = "",
        at_time: str = "",
        severity: str = "medium",
        evidence_cells: list[str] | None = None,
        pre_diagnosis: dict[str, Any] | None = None,
        shift_name: str = "当前值班班次",
        archive_version: str = "",
        dedup_key: str | None = None,
        channels: list[dict[str, str]] | None = None,
        sla_minutes: int = 30,
        actor: str = "proactive-watcher",
    ) -> tuple[dict[str, Any], bool]:
        """Atomically create a ticket (idempotent on dedup_key) AND enqueue outbox alerts in one transaction."""
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat(timespec="seconds").replace("+00:00", "Z")
        sla_deadline = (now_dt + timedelta(minutes=max(5, int(sla_minutes)))).isoformat(timespec="seconds").replace("+00:00", "Z")
        dkey = dedup_key or f"{source_finding_id}::{cabinet}::{at_time}"
        ev_cells = list(dict.fromkeys(str(c) for c in (evidence_cells or []) if c))
        eff_channels = channels if channels is not None else [{"channel": "in_app", "target_url": ""}]

        with self._write() as conn:
            existing = conn.execute("SELECT * FROM incident_ticket WHERE dedup_key = ?", (dkey,)).fetchone()
            if existing is not None:
                return self._row_to_ticket(existing), False

            tid = f"TKT-{uuid.uuid4().hex[:10].upper()}"
            conn.execute(
                """
                INSERT INTO incident_ticket(
                    ticket_id, dedup_key, source_finding_id, cabinet, at_time, severity, status,
                    title, summary, evidence_cells_json, pre_diagnosis_json, assignee, shift_name,
                    sla_deadline_at, handover_note, resolution_note, superseded_reason, archive_version,
                    created_at, updated_at, acknowledged_at, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, '', ?, ?, '', '', '', ?, ?, ?, NULL, NULL)
                """,
                (
                    tid,
                    dkey,
                    source_finding_id,
                    cabinet,
                    at_time,
                    severity,
                    title,
                    summary,
                    json.dumps(ev_cells, ensure_ascii=False),
                    json.dumps(pre_diagnosis, ensure_ascii=False, default=str) if pre_diagnosis is not None else None,
                    shift_name,
                    sla_deadline,
                    archive_version,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO incident_ticket_log(
                    ticket_id, action, from_status, to_status, actor, shift_name, note, payload_json, created_at
                ) VALUES (?, 'create', '', 'open', ?, ?, ?, '{}', ?)
                """,
                (tid, actor, shift_name, f"事件入库自动建单（来源 {source_finding_id}）", now),
            )
            row = conn.execute("SELECT * FROM incident_ticket WHERE ticket_id = ?", (tid,)).fetchone()
            ticket = self._row_to_ticket(row)

            destinations: set[tuple[str, str]] = set()
            for ch in eff_channels:
                ch_name = str(ch.get("channel") or "in_app").strip()
                if ch_name not in SUPPORTED_CHANNELS:
                    ch_name = "in_app"
                target_url = str(ch.get("target_url") or "").strip() if ch_name not in ("in_app", "cmms_export") else ""
                destination = (ch_name, target_url)
                if destination in destinations:
                    continue
                destinations.add(destination)
                oid = f"OUT-{uuid.uuid4().hex[:10].upper()}"
                payload = format_channel_payload(ch_name, ticket)
                conn.execute(
                    """
                    INSERT INTO alert_outbox(
                        outbox_id, ticket_id, event_id, channel, target_url, payload_json,
                        status, attempts, max_attempts, last_error, next_retry_at, delivered_at, receipt_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, 5, '', ?, NULL, '', ?)
                    """,
                    (
                        oid,
                        tid,
                        source_finding_id,
                        ch_name,
                        target_url,
                        json.dumps(payload, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            return ticket, True

    def refresh_pre_diagnosis(
        self, ticket_id: str, pre_diagnosis: dict[str, Any], *, expected_revision: int, archive_version: str, actor: str,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._write() as conn:
            row = conn.execute(
                """
                UPDATE incident_ticket SET pre_diagnosis_json = ?, updated_at = ?, revision = revision + 1
                WHERE ticket_id = ? AND revision = ? AND status NOT IN ('closed', 'superseded')
                RETURNING *
                """,
                (json.dumps(pre_diagnosis, ensure_ascii=False), now, ticket_id, expected_revision),
            ).fetchone()
            if row is None:
                raise TicketStateConflict("工单已更新或已结束，请核对最新状态。")
            conn.execute(
                """
                INSERT INTO incident_ticket_log(
                    ticket_id, action, from_status, to_status, actor, shift_name, note, payload_json, created_at
                ) VALUES (?, 'refresh_pre_diagnosis', ?, ?, ?, ?, ?, ?, ?)
                """,
                (ticket_id, row["status"], row["status"], actor, row["shift_name"], "重新核对归档并刷新初诊。",
                 json.dumps({"archive_version": archive_version, "kind": pre_diagnosis["kind"]}), now),
            )
            return self._row_to_ticket(row)

    def transition_ticket(
        self,
        ticket_id: str,
        action: str,
        *,
        actor: str = "值班员",
        shift_name: str = "",
        note: str = "",
        assignee: str | None = None,
        extra_payload: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        with self._write() as conn:
            row = conn.execute("SELECT * FROM incident_ticket WHERE ticket_id = ?", (ticket_id,)).fetchone()
            if row is None:
                raise ValueError(f"未找到工单：{ticket_id}")
            if expected_revision is not None and row["revision"] != expected_revision:
                raise TicketStateConflict("工单已被更新，请核对最新状态后重试。")
            return self._transition(conn, row, action, actor=actor, shift_name=shift_name, note=note,
                                    assignee=assignee, extra_payload=extra_payload)

    def _transition(self, conn, row, action, *, actor, shift_name="", note="", assignee=None, extra_payload=None):
        if action not in ACTION_TO_STATUS:
            raise ValueError(f"不支持的工单动作：{action}")
        previous = row["status"]
        if action not in TICKET_ACTIONS.get(previous, ()):
            raise TicketStateConflict(f"工单当前为「{TICKET_STATUSES.get(previous, previous)}」，无法执行该操作。")
        note = note.strip()
        if action in NOTE_REQUIRED_ACTIONS and not note:
            raise ValueError("请填写本次销项、复开或替代的依据。")

        now = _utc_now()
        status = ACTION_TO_STATUS[action]
        ticket_id = row["ticket_id"]
        shift = shift_name.strip() or row["shift_name"]
        owner = assignee if assignee is not None else (actor if action == "claim" else row["assignee"])
        acknowledged = row["acknowledged_at"]
        if acknowledged is None and action in ("claim", "supplement", "dispute", "close"):
            acknowledged = now
        closed = now if action == "close" else None
        handover = note if action == "handover" else row["handover_note"]
        resolution = note if action in ("close", "dispute", "supplement") else row["resolution_note"]
        log_payload = dict(extra_payload or {})
        if action == "reopen":
            owner = ""
            resolution = ""
            log_payload["previous_closed_at"] = row["closed_at"]
            log_payload["previous_resolution_note"] = row["resolution_note"]

        updated = conn.execute(
            """
            UPDATE incident_ticket SET status = ?, assignee = ?, shift_name = ?,
                handover_note = ?, resolution_note = ?, superseded_reason = ?,
                updated_at = ?, acknowledged_at = ?, closed_at = ?, revision = revision + 1
            WHERE ticket_id = ? AND revision = ?
            RETURNING *
            """,
            (status, owner, shift, handover, resolution, note if action == "supersede" else row["superseded_reason"],
             now, acknowledged, closed, ticket_id, row["revision"]),
        ).fetchone()
        if updated is None:
            raise TicketStateConflict("工单已被更新，请核对最新状态后重试。")
        conn.execute(
            """
            INSERT INTO incident_ticket_log(
                ticket_id, action, from_status, to_status, actor, shift_name, note, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ticket_id, action, previous, status, actor, shift, note,
             json.dumps(log_payload, ensure_ascii=False, default=str), now),
        )
        if action == "supersede":
            conn.execute("""
                UPDATE alert_outbox
                SET status = CASE WHEN status IN ('delivered', 'sending') THEN 'revoked' ELSE 'cancelled' END,
                    claim_token = CASE WHEN status = 'sending' THEN claim_token ELSE '' END,
                    claim_expires_epoch = CASE WHEN status = 'sending' THEN claim_expires_epoch ELSE 0 END,
                    withdrawn_at = ?, next_retry_at = ''
                WHERE ticket_id = ? AND status NOT IN ('cancelled', 'revoked')
            """, (now, ticket_id))
        return self._row_to_ticket(updated)

    def reconcile_correction(
        self,
        *,
        cabinet: str,
        at_time: str,
        prev_p_kw: float,
        curr_p_kw: float,
        threshold_kw: float = 1000.0,
        reason: str = "原始归档读数经更正后差值回落至门槛以下",
        actor: str = "correction-reconciler",
    ) -> dict[str, Any]:
        """Supersede stale active alerts/tickets when corrected readings eliminate the trigger (Astra #13)."""
        corrected_delta = round(float(curr_p_kw) - float(prev_p_kw), 2)
        still_triggered = abs(corrected_delta) >= float(threshold_kw)
        superseded_ids: list[str] = []

        if not still_triggered:
            with self._write() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM incident_ticket
                    WHERE cabinet = ? AND at_time = ? AND status NOT IN ('closed', 'superseded')
                    """,
                    (str(cabinet).strip(), str(at_time).strip()),
                ).fetchall()
                for row in rows:
                    self._transition(
                        conn, row, "supersede", actor=actor,
                        note=f"{reason}（更正后 ΔP={corrected_delta:+.2f} kW，绝对值低于 {threshold_kw} kW）",
                        extra_payload={"prev_p_kw": prev_p_kw, "curr_p_kw": curr_p_kw, "corrected_delta_kw": corrected_delta},
                    )
                    superseded_ids.append(row["ticket_id"])

        return {
            "cabinet": cabinet,
            "at_time": at_time,
            "corrected_delta_kw": corrected_delta,
            "threshold_kw": threshold_kw,
            "still_triggered": still_triggered,
            "superseded_ticket_ids": superseded_ids,
            "superseded_count": len(superseded_ids),
        }

    def _claim_outbox(self, outbox_id, token, force_retry_now):
        epoch = time.time()
        with self._write() as conn:
            return conn.execute(
                f"""
                UPDATE alert_outbox SET status = 'claimed', claim_token = ?, claim_expires_epoch = ?
                WHERE outbox_id = ? AND {OUTBOX_DUE}
                RETURNING outbox_id
                """,
                (token, epoch + OUTBOX_LEASE_SECONDS, outbox_id, force_retry_now, _utc_now(), epoch),
            ).fetchone()

    def _start_outbox(self, outbox_id, token):
        epoch = time.time()
        # Transition and correction share the same transaction lock. A reserved
        # notification cancelled before this point never enters the transport.
        with self._write() as conn:
            return conn.execute(
                """
                UPDATE alert_outbox SET status = 'sending', attempts = attempts + 1, claim_expires_epoch = ?
                WHERE outbox_id = ? AND status = 'claimed' AND claim_token = ? AND claim_expires_epoch > ?
                  AND EXISTS (
                    SELECT 1 FROM incident_ticket
                    WHERE incident_ticket.ticket_id = alert_outbox.ticket_id AND incident_ticket.status <> 'superseded'
                  )
                RETURNING *
                """,
                (epoch + OUTBOX_LEASE_SECONDS, outbox_id, token, epoch),
            ).fetchone()

    def _finish_outbox(self, row, token, ack, error):
        now = _utc_now()
        attempts = row["attempts"]
        status = "delivered" if error is None else "dead_letter" if attempts >= row["max_attempts"] else "failed"
        delay = min(300, 5 * (2 ** (attempts - 1)))
        retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds").replace("+00:00", "Z")
        outbox_id = row["outbox_id"]
        receipt = _sha256_json({"outbox_id": outbox_id, "delivered_at": now, "ack": ack})[:24] if error is None else ""
        with self._write() as conn:
            updated = conn.execute("""
                UPDATE alert_outbox
                SET status = CASE WHEN status = 'revoked' THEN status ELSE ? END,
                    last_error = ?, delivered_at = ?, receipt_sha256 = ?,
                    next_retry_at = CASE WHEN status = 'revoked' THEN '' ELSE ? END,
                    claim_token = '', claim_expires_epoch = 0
                WHERE outbox_id = ? AND claim_token = ? AND status IN ('sending', 'revoked')
                RETURNING status
            """, (status, error or "", now if error is None else None, receipt,
                  retry_at if status == "failed" else "", outbox_id, token)).fetchone()
        if updated is None:
            return None
        return {
            "outbox_id": outbox_id, "ticket_id": row["ticket_id"], "channel": row["channel"],
            "status": updated["status"], "attempts": attempts, "receipt_sha256": receipt, "error": error or "",
        }

    def dispatch_outbox(
        self,
        *,
        sender: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None = None,
        limit: int = 50,
        force_retry_now: bool = False,
    ) -> dict[str, Any]:
        """Claim, send, and settle each due notification without holding a transaction during I/O."""
        now = _utc_now()
        epoch = time.time()
        exhausted_reason = "最后一次投递租约已过期，送达情况待对账。"
        with self._write() as conn:
            exhausted = conn.execute("""
                UPDATE alert_outbox SET status = 'dead_letter', last_error = ?, next_retry_at = '',
                    claim_token = '', claim_expires_epoch = 0
                WHERE status = 'sending' AND claim_expires_epoch <= ? AND attempts >= max_attempts
                RETURNING outbox_id, ticket_id, channel, status, attempts
            """, (exhausted_reason, epoch)).fetchall()
            candidates = conn.execute(
                f"SELECT outbox_id FROM alert_outbox WHERE {OUTBOX_DUE} ORDER BY created_at, outbox_id LIMIT ?",
                (force_retry_now, now, epoch, max(1, min(int(limit), 200))),
            ).fetchall()

        results = [dict(row, error=exhausted_reason) for row in exhausted]
        for candidate in candidates:
            outbox_id = candidate["outbox_id"]
            token = uuid.uuid4().hex
            if not self._claim_outbox(outbox_id, token, force_retry_now):
                continue
            row = self._start_outbox(outbox_id, token)
            if row is None:
                continue

            ack, error = None, None
            try:
                payload = json.loads(row["payload_json"])
                if sender is None:
                    ack = _default_channel_sender(row["channel"], row["target_url"], payload, notification_id=outbox_id)
                else:
                    ack = sender(row["channel"], row["target_url"], {**payload, "notification_id": outbox_id})
                if isinstance(ack, dict) and ack.get("ok") is False:
                    raise RuntimeError("通知通道未确认接收。")
            except Exception as exc:
                error = str(exc)[:300]
            settled = self._finish_outbox(row, token, ack, error)
            if settled is not None:
                results.append(settled)

        return {
            "dispatched_at": now, "processed": len(results), "results": results,
            **{status: sum(item["status"] == status for item in results)
               for status in ("delivered", "failed", "dead_letter", "revoked")},
        }

    def list_tickets(self, *, status: str | None = None, severity: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if status and status != "all":
            if status == "active":
                clauses.append("status NOT IN ('closed', 'superseded')")
            else:
                clauses.append("status = ?")
                params.append(status)
        if severity and severity != "all":
            clauses.append("severity = ?")
            params.append(severity)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 200)))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM incident_ticket {where} ORDER BY updated_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [self._row_to_ticket(r) for r in rows]

    def get_ticket_detail(self, ticket_id: str) -> dict[str, Any] | None:
        with self._write() as conn:
            row = conn.execute("SELECT * FROM incident_ticket WHERE ticket_id = ?", (ticket_id,)).fetchone()
            if row is None:
                return None
            logs = [
                {
                    "seq": r["seq"],
                    "action": r["action"],
                    "from_status": r["from_status"],
                    "to_status": r["to_status"],
                    "actor": r["actor"],
                    "shift_name": r["shift_name"],
                    "note": r["note"],
                    "payload": json.loads(r["payload_json"] or "{}"),
                    "created_at": r["created_at"],
                }
                for r in conn.execute(
                    "SELECT * FROM incident_ticket_log WHERE ticket_id = ? ORDER BY seq ASC", (ticket_id,)
                ).fetchall()
            ]
            outbox = [
                {
                    "outbox_id": r["outbox_id"],
                    "channel": r["channel"],
                    "target_url": r["target_url"],
                    "status": r["status"],
                    "attempts": r["attempts"],
                    "max_attempts": r["max_attempts"],
                    "last_error": r["last_error"],
                    "delivered_at": r["delivered_at"],
                    "withdrawn_at": r["withdrawn_at"],
                    "receipt_sha256": r["receipt_sha256"],
                    "created_at": r["created_at"],
                }
                for r in conn.execute(
                    "SELECT * FROM alert_outbox WHERE ticket_id = ? ORDER BY created_at DESC", (ticket_id,)
                ).fetchall()
            ]
        ticket = self._row_to_ticket(row)
        return {
            **ticket,
            "logs": logs,
            "outbox": outbox,
            "cmms_workorder": format_channel_payload("cmms_export", ticket),
        }

    def sla_metrics(self) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            t_rows = conn.execute("SELECT status, created_at, acknowledged_at FROM incident_ticket").fetchall()
            o_rows = conn.execute("SELECT status, COUNT(*) AS cnt FROM alert_outbox GROUP BY status").fetchall()
            w_row = conn.execute("SELECT * FROM watcher_state WHERE id = 1").fetchone()

        status_counts: dict[str, int] = {k: 0 for k in TICKET_STATUSES}
        ack_lats: list[float] = []
        for r in t_rows:
            status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
            if r["acknowledged_at"]:
                dt_c = _parse_iso(r["created_at"])
                dt_a = _parse_iso(r["acknowledged_at"])
                if dt_c and dt_a:
                    ack_lats.append(max(0.0, (dt_a - dt_c).total_seconds()))

        outbox_counts = {r["status"]: int(r["cnt"]) for r in o_rows}
        avg_ack_s = round(sum(ack_lats) / len(ack_lats), 2) if ack_lats else 0.0
        active_count = sum(v for k, v in status_counts.items() if k not in ("closed", "superseded"))
        return {
            "tickets_total": len(t_rows),
            "tickets_active": active_count,
            "tickets_open": status_counts["open"],
            "tickets_claimed": status_counts["claimed"],
            "tickets_disputed": status_counts["disputed"],
            "tickets_escalated": status_counts["escalated"],
            "tickets_handed_over": status_counts["handed_over"],
            "tickets_closed": status_counts["closed"],
            "tickets_superseded": status_counts["superseded"],
            "avg_ack_latency_s": avg_ack_s,
            "unread_badge_count": status_counts["open"] + status_counts["escalated"],
            "outbox_pending": outbox_counts.get("pending", 0),
            "outbox_failed": outbox_counts.get("failed", 0),
            "outbox_delivered": outbox_counts.get("delivered", 0),
            "outbox_dead_letter": outbox_counts.get("dead_letter", 0),
            "outbox_in_flight": outbox_counts.get("claimed", 0) + outbox_counts.get("sending", 0),
            "outbox_cancelled": outbox_counts.get("cancelled", 0),
            "outbox_revoked": outbox_counts.get("revoked", 0),
            "watcher": dict(w_row) if w_row else {},
        }

    def record_watcher_scan(self, *, archive_version: str, created_count: int, error: str = "") -> None:
        now = _utc_now()
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE watcher_state SET
                        last_scan_at = ?,
                        last_archive_version = ?,
                        scans_total = scans_total + 1,
                        tickets_created_total = tickets_created_total + ?,
                        last_error = ?
                    WHERE id = 1
                    """,
                    (now, archive_version, int(created_count), error[:300]),
                )


_REVIEW_BOUNDARIES = {
    "voltage_over_limit": [
        ("tap_position", "2# 主变档位与电压偏差", "2# 主变铭牌、同期分接头档位和变更记录。"),
        ("reactive_state", "轻载及补偿状态与电压偏差", "II 段同期无功量测和电容器投切记录。"),
    ],
    "voltage_high_35kv": [
        ("upstream_voltage", "上级电源电压与档位影响", "上级站同期母线电压、1# 主变实际档位及变更记录。"),
    ],
    "low_power_factor": [
        ("load_reactive_demand", "带载工况及就地补偿状态", "同期负荷工况和就地无功补偿投入记录。"),
        ("metering_scale", "功率因数量测口径偏差", "表计 CT/PT 变比、接线图及同期无功量测。"),
    ],
    "capacitor_cos_anomaly": [
        ("metering_channel", "表计接线或采样通道偏差", "表计二次接线图和功率因数通道配置。"),
        ("capacitor_state", "补偿投入状态与读数不一致", "同期电容器投切记录。"),
    ],
    "systematic_scale_offset": [
        ("metering_scale", "表计倍率或接线折算口径偏差", "CT 变比配置、功率计算口径及二次接线图。"),
    ],
    "scada_polling_skew": [
        ("polling_skew", "整点截面存在采集时差", "各测点采集时间戳及同一冻结断面的量测。"),
    ],
    "diurnal_baseline_outlier": [
        ("operating_schedule", "生产安排或运行方式变化", "同期生产排班、避峰执行记录及主变切换记录。"),
    ],
}


def _eligible_findings(archive):
    from laowadi.health import evaluate_health

    for finding in evaluate_health(archive)["findings"]:
        if finding["severity"] in ("high", "medium"):
            evidence = finding.get("evidence") or []
            at_time = evidence[0].get("time", "") if evidence else ""
            dedup_key = f"{finding['id']}::{finding.get('cabinet', '')}::{at_time}"
            yield finding, at_time, dedup_key


def _finding_cells(finding):
    return [f"{e['file']} / {e['sheet']}!{e['cell']}" for e in finding.get("evidence", [])
            if {"file", "sheet", "cell"} <= e.keys()]


def _build_pre_diagnosis(archive, finding, at_time):
    from laowadi.diag_tools import DiagnosticToolbox
    from laowadi.hypothesis import FEEDER_POLARITY, REVERSE_POWER, ZERO_INCOMER, execute_hypotheses
    from laowadi.verifier import finalize_diagnosis, finalize_scoped_diagnosis

    category = finding["category"]
    cabinet = finding["cabinet"]
    at = at_time[:16]
    cells = _finding_cells(finding)
    reviews = _REVIEW_BOUNDARIES.get(category, [
        ("site_conditions", "同期运行工况与量测口径", "同期运行日志与相关测点的采集记录。"),
    ])
    pre = {
        "kind": category,
        "verified": "pending_site_review" if cells else "insufficient_data",
        "summary": finding["summary"] + " 初诊范围限于日报筛查，现场原因待核。",
        "refuted_hypotheses": [],
        "pending_hypotheses": [
            {"hypothesis": name, "title": title, "judgment": "待现场核对。"} for name, title, _ in reviews
        ],
        "missing_evidence": [evidence for _, _, evidence in reviews],
        "evidence_cells": list(dict.fromkeys(cells)),
        "followup_question": f"请核查老洼地 {at} 的 {cabinet} 测点及同期工况，复核这项发现：{finding['title']}。哪些原表读数支持筛查，最少还需补哪些现场记录？",
        "recommended_actions": [f"核对{evidence}" for _, _, evidence in reviews],
    }
    if category == "main_chain_reverse_power":
        scenario = REVERSE_POWER
        question = f"请查证老洼地 {at} 的 306/312/1006 柜反向有功，对照 1027 和前后整点。哪些假设可在该采样点排除，最少还需补哪些现场证据？"
    elif category == "persistent_negative_power" and cabinet in FEEDER_POLARITY.cabinets:
        scenario = FEEDER_POLARITY
        question = f"请查证老洼地 {at} 的 {cabinet} 柜持续负有功与负功率因数，区分极性整定和二采区倒送。哪些假设可排除，最少还需补哪些现场证据？"
    elif category == "single_incomer_dual_transformer":
        scenario = ZERO_INCOMER
        question = f"请查证老洼地 {at} 的 306 柜有功和 A 相电流归零，对照归零窗口前后量测。区分实际退出、失压与采集异常，需要哪些现场证据？"
    else:
        return pre

    pre.update(kind=scenario.kind, followup_question=question)
    if not at_time:
        pre.update(verified="insufficient_data", summary=scenario.missing_observation,
                   missing_evidence=[scenario.missing_observation], recommended_actions=[])
        return pre

    result = execute_hypotheses(
        archive, DiagnosticToolbox(archive), question, scenario, at_time, client=None,
        max_rounds=5, max_tool_calls=5, emit=lambda *_args, **_kwargs: None,
        fallback_to_local=False, summarize=lambda *_args: None,
    )
    finalize = finalize_scoped_diagnosis if result.get("task_alignment") else finalize_diagnosis
    report, verification = finalize(archive, result["report"], result["tool_trace"])
    pre.update(verified=verification["status"], summary=report["summary"])
    trace = report.get("hypothesis_trace") or {}
    if not trace or result["stop_reason"] == "missing_observation":
        pre.update(summary=trace.get("stop_message") or report["summary"],
                   verified="insufficient_data" if result["stop_reason"] == "missing_observation" else verification["status"],
                   missing_evidence=report.get("missing_evidence") or [scenario.missing_observation],
                   recommended_actions=[])
        return pre

    facts = report["confirmed_facts"]
    pre["pending_hypotheses"] = []
    for check in trace["checks"]:
        hypothesis = {key: check[key] for key in ("hypothesis", "title", "judgment")}
        refuted = check["outcome"] == "refuted_at_sample"
        pre["refuted_hypotheses" if refuted else "pending_hypotheses"].append(hypothesis)
        check_cells = [cell for index in check["fact_indices"] for cell in facts[index]["evidence"]]
        cells.extend(check_cells if refuted else check_cells[:6])
    pre["pending_hypotheses"].extend(
        {"hypothesis": check["hypothesis"], "title": check["title"], "judgment": check["criterion"]}
        for check in trace["pending"]
    )
    for index in trace["observation_fact_indices"]:
        cells.extend(facts[index]["evidence"])
    pre.update(
        missing_evidence=report["missing_evidence"],
        evidence_cells=list(dict.fromkeys(cells)),
        recommended_actions=report.get("field_sop") or [],
    )
    return pre


class ProactiveWatcherDaemon:
    """Event-driven background watcher that detects new anomalies and autonomously generates pre-diagnosis tickets."""

    def __init__(
        self,
        archive_factory: Callable[[], Any],
        store: IncidentTicketStore,
        *,
        archive_version_fn: Callable[[], str] | None = None,
        poll_interval_seconds: float = 30.0,
        channels: list[dict[str, str]] | None = None,
    ):
        self._archive_factory = archive_factory
        self.store = store
        self._archive_version_fn = archive_version_fn or (lambda: "v1")
        self.poll_interval_seconds = max(2.0, float(poll_interval_seconds))
        self.channels = channels or [{"channel": "in_app", "target_url": ""}]
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._running = False

    def _archive_version(self, archive) -> str:
        if archive.pg_url:
            publication = archive.db.execute("SELECT version FROM publication WHERE singleton=1").fetchone()
            if not publication:
                raise ValueError("主动巡检等待已发布的归档版本。")
            return publication[0]
        return str(self._archive_version_fn())

    def refresh_pre_diagnosis(self, ticket, *, expected_revision: int, actor: str):
        if ticket["revision"] != expected_revision or ticket["status"] in ("closed", "superseded"):
            raise TicketStateConflict("工单已更新或已结束，请核对最新状态。")
        with self._archive_factory() as archive:
            version = self._archive_version(archive)
            for finding, at_time, key in _eligible_findings(archive):
                if key == ticket["dedup_key"]:
                    pre = _build_pre_diagnosis(archive, finding, at_time)
                    break
            else:
                raise TicketStateConflict("当前归档未检出同一发现，无法刷新初诊。")
        return self.store.refresh_pre_diagnosis(
            ticket["ticket_id"], pre, expected_revision=expected_revision, archive_version=version, actor=actor,
        )

    def scan_once(self, *, max_new_tickets: int = 4, channels: list[dict[str, str]] | None = None) -> dict[str, Any]:
        """Pre-diagnose high and medium findings before creating tickets and notifications."""
        t0 = time.monotonic()
        ver = ""
        created_tickets: list[dict[str, Any]] = []
        skipped = 0
        batch_limit = max(1, int(max_new_tickets))
        eff_channels = channels or self.channels

        try:
            with self._archive_factory() as archive:
                ver = self._archive_version(archive)
                for f, at_time, dedup_key in _eligible_findings(archive):
                    if len(created_tickets) >= batch_limit:
                        break
                    # Check existence first so we don't redundantly re-run pre-diagnosis
                    with closing(self.store._connect()) as conn:
                        exists = conn.execute("SELECT 1 FROM incident_ticket WHERE dedup_key = ?", (dedup_key,)).fetchone()
                    if exists:
                        skipped += 1
                        continue

                    compact_pre = _build_pre_diagnosis(archive, f, at_time)
                    ticket, created = self.store.create_ticket_with_outbox(
                        source_finding_id=f["id"],
                        title=f["title"],
                        summary=f["summary"],
                        cabinet=str(f.get("cabinet", "")),
                        at_time=at_time,
                        severity=f["severity"],
                        evidence_cells=_finding_cells(f),
                        pre_diagnosis=compact_pre,
                        archive_version=ver,
                        dedup_key=dedup_key,
                        channels=eff_channels,
                        actor="proactive-watcher",
                    )
                    if created:
                        created_tickets.append(ticket)
                    else:
                        skipped += 1

            dispatch_res = self.store.dispatch_outbox()
            self.store.record_watcher_scan(archive_version=ver, created_count=len(created_tickets))
            elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)
            logger.info(
                f"Proactive scan finished: created={len(created_tickets)}, skipped={skipped}, delivered={dispatch_res['delivered']}",
                extra={
                    "event": "proactive_scan_completed",
                    "fields": {
                        "created": len(created_tickets),
                        "skipped": skipped,
                        "delivered": dispatch_res["delivered"],
                        "elapsed_ms": elapsed_ms,
                    },
                },
            )
            return {
                "status": "ok",
                "archive_version": ver,
                "created_count": len(created_tickets),
                "skipped_existing": skipped,
                "created_tickets": created_tickets,
                "dispatch": dispatch_res,
                "elapsed_ms": elapsed_ms,
            }
        except Exception as exc:
            self.store.record_watcher_scan(archive_version=ver, created_count=0, error=str(exc))
            raise

    def start(self) -> bool:
        with self._lock:
            if self._running and self._thread and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._running = True

            def _loop():
                while not self._stop_event.is_set():
                    try:
                        with watcher_slot(self.store.path) as acquired:
                            if acquired:
                                self.scan_once(max_new_tickets=4)
                    except Exception as exc:
                        logger.warning(f"Proactive watcher cycle warning: {exc}", extra={"event": "proactive_scan_error"})
                    self._stop_event.wait(self.poll_interval_seconds)

            self._thread = threading.Thread(target=_loop, name="laowadi-proactive-watcher", daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            self._running = False

    def status(self) -> dict[str, Any]:
        sla = self.store.sla_metrics()
        return {
            "daemon_running": bool(self._running and self._thread and self._thread.is_alive()),
            "poll_interval_seconds": self.poll_interval_seconds,
            "channels": self.channels,
            **sla,
        }
