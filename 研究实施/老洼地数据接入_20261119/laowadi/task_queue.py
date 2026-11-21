"""Durable jobs, leases, event sequences and Worker heartbeats on the configured runtime store."""

from __future__ import annotations

from contextlib import closing

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sqlite3
import threading
import time
from typing import Any, Literal
import uuid

from laowadi.db.postgres import connect_runtime, runtime_postgres
from laowadi.outcomes import BUSINESS_STATUSES, classify_outcome
from laowadi.storage import ROOT


JOBS_DB = ROOT / "local" / "laowadi_jobs.sqlite3"
JobType = Literal["diagnose", "multi_agent", "powerflow"]
JobStatus = Literal["queued", "running", "completed", "failed", "cancelled", "dead_letter"]
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "dead_letter"}

# Audit #06: Pre-final-review model text MUST NEVER be persisted into public SSE logs or replayed.
PRE_VERIFICATION_REDACTED_FIELDS = (
    "preview",
    "text",
    "delta",
    "reasoning",
    "raw_content",
    "draft_report",
    "partial_judgment",
)
PRE_VERIFICATION_EVENT_TYPES = {"llm_delta", "llm_round", "reflection", "claim_audit"}


def sanitize_public_job_event(event_obj: dict[str, Any]) -> dict[str, Any]:
    """Strip unverified model text fragments from pre-final-review SSE events before storage or replay."""
    if not isinstance(event_obj, dict):
        return {"type": "event"}
    ev_type = str(event_obj.get("type") or "event")
    clean = dict(event_obj)
    if ev_type in PRE_VERIFICATION_EVENT_TYPES:
        redacted = False
        for field in PRE_VERIFICATION_REDACTED_FIELDS:
            if field in clean:
                clean.pop(field, None)
                redacted = True
        clean["visibility"] = "progress_only"
        clean["pre_final_review"] = True
        if redacted:
            clean["unverified_text_redacted"] = True
    elif ev_type == "done" and isinstance(clean.get("result"), dict):
        res = dict(clean["result"])
        if "raw_content" in res:
            res["raw_content_redacted"] = True
            res["raw_content_chars"] = len(str(res.pop("raw_content") or ""))
        clean["result"] = res
    return clean


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class DurableSQLiteBroker:
    """Cross-process durable task queue, SSE event store, worker registry & shared rate limiter."""

    backend_name = "sqlite_wal_durable_broker"

    def __init__(self, path: Path | str = JOBS_DB):
        self.path = Path(path)
        if runtime_postgres(self.path):
            self.backend_name = "postgresql_durable_broker"
        self._local_lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return connect_runtime(self.path, writable=True, wal=True, explicit=True)

    def _init_db(self) -> None:
        with self._local_lock, closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS job (
                    job_id TEXT PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    session_id TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL DEFAULT 'local-workbench',
                    role TEXT NOT NULL DEFAULT 'engineer',
                    idempotency_key TEXT UNIQUE,
                    worker_id TEXT,
                    lease_expires_at REAL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    business_status TEXT NOT NULL DEFAULT '',
                    business_trusted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    created_epoch REAL NOT NULL,
                    updated_epoch REAL NOT NULL
                )
                """
            )
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(job)").fetchall()}
            if "business_status" not in cols:
                conn.execute("ALTER TABLE job ADD COLUMN business_status TEXT NOT NULL DEFAULT ''")
            if "business_trusted" not in cols:
                conn.execute("ALTER TABLE job ADD COLUMN business_trusted INTEGER NOT NULL DEFAULT 0")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_job_status_created ON job(status, created_epoch)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_job_lease ON job(status, lease_expires_at)")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS job_event (
                    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    job_seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, job_seq)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_job_event_lookup ON job_event(job_id, job_seq)")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS worker_heartbeat (
                    worker_id TEXT PRIMARY KEY,
                    worker_kind TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    hostname TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    last_heartbeat_epoch REAL NOT NULL,
                    last_heartbeat_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    active_job_id TEXT,
                    processed_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS circuit_breaker_state (
                    name TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_threshold INTEGER NOT NULL DEFAULT 3,
                    cooldown_seconds REAL NOT NULL DEFAULT 30.0,
                    opened_epoch REAL DEFAULT 0,
                    cooldown_until_epoch REAL DEFAULT 0,
                    half_open_probe_inflight INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT DEFAULT '',
                    updated_at TEXT NOT NULL,
                    updated_by_pid INTEGER NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS shared_rate_limit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_key TEXT NOT NULL,
                    hit_epoch REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rate_limit_key_ts ON shared_rate_limit(client_key, hit_epoch)")
            conn.commit()

    # ── Job Enqueue & Status ───────────────────────────────────────────────────
    def enqueue(
        self,
        job_type: JobType,
        payload: dict[str, Any],
        *,
        session_id: str = "",
        actor: str = "local-workbench",
        role: str = "engineer",
        idempotency_key: str | None = None,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        now_ts = _utc_now()
        now_ep = time.time()
        idem = (idempotency_key or "").strip()[:128] or None

        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if idem:
                existing = conn.execute("SELECT * FROM job WHERE idempotency_key = ?", (idem,)).fetchone()
                if existing is not None:
                    conn.commit()
                    return self._row_to_job(existing)

            job_id = f"JOB-{uuid.uuid4().hex[:12].upper()}"
            conn.execute(
                """
                INSERT INTO job (
                    job_id, job_type, status, payload_json, session_id, actor, role,
                    idempotency_key, attempts, max_attempts, created_at, updated_at,
                    created_epoch, updated_epoch
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    job_type,
                    json.dumps(payload, ensure_ascii=False, allow_nan=False),
                    session_id,
                    actor,
                    role,
                    idem,
                    max(1, int(max_attempts)),
                    now_ts,
                    now_ts,
                    now_ep,
                    now_ep,
                ),
            )
            # Persist initial durable SSE event (job_seq = 1)
            self._append_event_tx(
                conn,
                job_id,
                "job_queued",
                {
                    "type": "job_queued",
                    "job_id": job_id,
                    "job_type": job_type,
                    "status": "queued",
                    "message": f"任务 {job_id} 已持久化入外部队列，等待工作进程认领…",
                },
            )
            row = conn.execute("SELECT * FROM job WHERE job_id = ?", (job_id,)).fetchone()
            conn.commit()
        return self._row_to_job(row)

    def _append_event_tx(self, conn: sqlite3.Connection, job_id: str, event_type: str, payload: dict[str, Any]) -> int:
        row = conn.execute("SELECT COALESCE(MAX(job_seq), 0) AS mx FROM job_event WHERE job_id = ?", (job_id,)).fetchone()
        next_seq = int(row["mx"]) + 1
        sanitized = sanitize_public_job_event(payload)
        enriched = {**sanitized, "job_id": job_id, "seq": next_seq}
        conn.execute(
            """
            INSERT INTO job_event (job_id, job_seq, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                job_id,
                next_seq,
                event_type,
                json.dumps(enriched, ensure_ascii=False, allow_nan=False),
                _utc_now(),
            ),
        )
        return next_seq

    def append_event(self, job_id: str, event_obj: dict[str, Any]) -> int:
        sanitized = sanitize_public_job_event(event_obj)
        ev_type = str(sanitized.get("type") or "event")
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            seq = self._append_event_tx(conn, job_id, ev_type, sanitized)
            conn.execute("UPDATE job SET updated_at = ?, updated_epoch = ? WHERE job_id = ?", (_utc_now(), time.time(), job_id))
            conn.commit()
        return seq

    def get_events_after(self, job_id: str, after_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn, conn:
            job_row = conn.execute(
                "SELECT status, business_status, business_trusted FROM job WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            rows = conn.execute(
                """
                SELECT job_seq, event_type, payload_json, created_at
                FROM job_event
                WHERE job_id = ? AND job_seq > ?
                ORDER BY job_seq ASC
                LIMIT ?
                """,
                (job_id, int(after_seq), max(1, min(int(limit), 500))),
            ).fetchall()
        suppress_pre_review_deltas = bool(
            job_row is not None
            and job_row["status"] in TERMINAL_STATUSES
            and int(job_row["business_trusted"] or 0) == 0
        )
        out = []
        for r in rows:
            if suppress_pre_review_deltas and r["event_type"] == "llm_delta":
                continue
            try:
                payload = sanitize_public_job_event(json.loads(r["payload_json"]))
            except Exception:
                payload = {"type": r["event_type"]}
            out.append({
                "seq": int(r["job_seq"]),
                "event_type": r["event_type"],
                "created_at": r["created_at"],
                "data": payload,
            })
        return out

    # ── Atomic Multi-Worker Lease Claiming & Orphan Reclamation ────────────────
    def claim_next_job(self, worker_id: str, *, lease_seconds: float = 20.0) -> dict[str, Any] | None:
        now_ep = time.time()
        now_ts = _utc_now()
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            # 1. Reclaim expired leases from crashed/SIGKILLed workers!
            orphans = conn.execute(
                """
                SELECT job_id, worker_id, attempts, max_attempts
                FROM job
                WHERE status = 'running' AND lease_expires_at > 0 AND lease_expires_at < ?
                """,
                (now_ep,),
            ).fetchall()
            for orph in orphans:
                oid = orph["job_id"]
                if int(orph["attempts"]) >= int(orph["max_attempts"]):
                    conn.execute(
                        """
                        UPDATE job
                        SET status = 'dead_letter', error = ?, updated_at = ?, updated_epoch = ?
                        WHERE job_id = ?
                        """,
                        ("工作进程多次失联且超过最大重试次数，任务已转入死信队列 (DLQ)。", now_ts, now_ep, oid),
                    )
                    self._append_event_tx(
                        conn,
                        oid,
                        "error",
                        {
                            "type": "error",
                            "error": "任务超过最大重试次数并转入死信队列 (dead_letter)。",
                            "status": "dead_letter",
                        },
                    )
                else:
                    conn.execute(
                        """
                        UPDATE job
                        SET status = 'queued', worker_id = NULL, lease_expires_at = 0,
                            updated_at = ?, updated_epoch = ?
                        WHERE job_id = ?
                        """,
                        (now_ts, now_ep, oid),
                    )
                    self._append_event_tx(
                        conn,
                        oid,
                        "job_reclaimed",
                        {
                            "type": "job_reclaimed",
                            "previous_worker": orph["worker_id"],
                            "attempt": int(orph["attempts"]),
                            "message": f"原工作进程 {orph['worker_id']} 租约超时失联，任务已重新入队等待其他 Worker 接管恢复…",
                        },
                    )

            # 2. Atomically claim oldest queued job
            cand = conn.execute(
                "SELECT job_id FROM job WHERE status = 'queued' ORDER BY created_epoch ASC LIMIT 1"
            ).fetchone()
            if cand is None:
                conn.commit()
                return None

            job_id = cand["job_id"]
            conn.execute(
                """
                UPDATE job
                SET status = 'running',
                    worker_id = ?,
                    lease_expires_at = ?,
                    attempts = attempts + 1,
                    updated_at = ?,
                    updated_epoch = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                (worker_id, now_ep + float(lease_seconds), now_ts, now_ep, job_id),
            )
            row = conn.execute("SELECT * FROM job WHERE job_id = ?", (job_id,)).fetchone()
            self._append_event_tx(
                conn,
                job_id,
                "job_claimed",
                {
                    "type": "job_claimed",
                    "worker_id": worker_id,
                    "worker_pid": os.getpid(),
                    "attempt": int(row["attempts"]),
                    "message": f"外部工作进程 {worker_id} (PID {os.getpid()}) 已认领任务（第 {row['attempts']} 次尝试）…",
                },
            )
            conn.commit()
        return self._row_to_job(row)

    def renew_lease(self, job_id: str, worker_id: str, *, lease_seconds: float = 20.0) -> bool:
        now_ep = time.time()
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE job
                SET lease_expires_at = ?, updated_at = ?, updated_epoch = ?
                WHERE job_id = ? AND worker_id = ? AND status = 'running'
                """,
                (now_ep + float(lease_seconds), _utc_now(), now_ep, job_id, worker_id),
            )
            conn.commit()
        return cur.rowcount > 0

    def release_job_on_shutdown(self, job_id: str, worker_id: str) -> bool:
        """Gracefully return an in-flight job to queued state when a worker receives SIGTERM."""
        now_ep = time.time()
        now_ts = _utc_now()
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE job
                SET status = 'queued', worker_id = NULL, lease_expires_at = 0,
                    updated_at = ?, updated_epoch = ?
                WHERE job_id = ? AND worker_id = ? AND status = 'running'
                """,
                (now_ts, now_ep, job_id, worker_id),
            )
            if cur.rowcount > 0:
                self._append_event_tx(
                    conn,
                    job_id,
                    "job_requeued",
                    {
                        "type": "job_requeued",
                        "message": f"工作进程 {worker_id} 正在优雅停机，已释放任务租约移交其他 Worker 继续执行…",
                    },
                )
            conn.commit()
        return cur.rowcount > 0

    def complete_job(self, job_id: str, worker_id: str, result: dict[str, Any]) -> None:
        now_ep = time.time()
        now_ts = _utc_now()
        outcome = classify_outcome(result, execution_status="completed")
        enriched_result = {
            **result,
            "outcome_layers": result.get("outcome_layers") or outcome,
            "business_status": outcome["business_status"],
            "business_trusted": outcome["business_trusted"],
        }
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE job
                SET status = 'completed',
                    business_status = ?,
                    business_trusted = ?,
                    result_json = ?,
                    lease_expires_at = 0,
                    updated_at = ?,
                    updated_epoch = ?
                WHERE job_id = ? AND worker_id = ?
                """,
                (
                    outcome["business_status"],
                    1 if outcome["business_trusted"] else 0,
                    json.dumps(enriched_result, ensure_ascii=False, allow_nan=False),
                    now_ts,
                    now_ep,
                    job_id,
                    worker_id,
                ),
            )
            self._append_event_tx(
                conn,
                job_id,
                "done",
                {
                    "type": "done",
                    "display_policy": "final_verdict_only",
                    "pre_review_events_superseded": not outcome["business_trusted"],
                    "business_status": outcome["business_status"],
                    "business_trusted": outcome["business_trusted"],
                    "result": enriched_result,
                },
            )
            conn.commit()

    def fail_job(self, job_id: str, worker_id: str, error: str, *, retryable: bool = False) -> None:
        now_ep = time.time()
        now_ts = _utc_now()
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT attempts, max_attempts FROM job WHERE job_id = ?", (job_id,)).fetchone()
            if retryable and row and int(row["attempts"]) < int(row["max_attempts"]):
                conn.execute(
                    """
                    UPDATE job
                    SET status = 'queued', worker_id = NULL, lease_expires_at = 0,
                        error = ?, updated_at = ?, updated_epoch = ?
                    WHERE job_id = ?
                    """,
                    (error, now_ts, now_ep, job_id),
                )
                self._append_event_tx(
                    conn,
                    job_id,
                    "job_retry",
                    {"type": "job_retry", "error": error, "message": f"执行遇到瞬态异常（{error[:80]}），已重新入队重试…"},
                )
            else:
                final_status = "dead_letter" if (row and int(row["attempts"]) >= int(row["max_attempts"]) and retryable) else "failed"
                conn.execute(
                    """
                    UPDATE job
                    SET status = ?, error = ?, lease_expires_at = 0,
                        updated_at = ?, updated_epoch = ?
                    WHERE job_id = ?
                    """,
                    (final_status, error, now_ts, now_ep, job_id),
                )
                self._append_event_tx(conn, job_id, "error", {"type": "error", "error": error, "status": final_status})
            conn.commit()

    def cancel_job(self, job_id: str) -> dict[str, Any] | None:
        now_ep = time.time()
        now_ts = _utc_now()
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE job
                SET status = 'cancelled', lease_expires_at = 0, updated_at = ?, updated_epoch = ?
                WHERE job_id = ? AND status IN ('queued', 'running')
                """,
                (now_ts, now_ep, job_id),
            )
            if cur.rowcount > 0:
                self._append_event_tx(conn, job_id, "cancelled", {"type": "cancelled", "message": "任务已取消。"})
            row = conn.execute("SELECT * FROM job WHERE job_id = ?", (job_id,)).fetchone()
            conn.commit()
        return self._row_to_job(row) if row else None

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT * FROM job WHERE job_id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def list_jobs(self, *, limit: int = 30, status: str | None = None) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit), 100))
        with closing(self._connect()) as conn, conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM job WHERE status = ? ORDER BY created_epoch DESC LIMIT ?",
                    (status, lim),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM job ORDER BY created_epoch DESC LIMIT ?", (lim,)).fetchall()
        return [self._row_to_job(r, include_result=False) for r in rows]

    def queue_stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = {"queued": 0, "running": 0, "completed": 0, "failed": 0, "cancelled": 0, "dead_letter": 0}
        by_biz = {k: 0 for k in BUSINESS_STATUSES}
        completed_trusted = 0
        completed_untrusted = 0
        with closing(self._connect()) as conn, conn:
            for r in conn.execute("SELECT status, COUNT(*) AS cnt FROM job GROUP BY status"):
                if r["status"] in stats:
                    stats[r["status"]] = int(r["cnt"])
            for r in conn.execute(
                "SELECT business_status, business_trusted, COUNT(*) AS cnt FROM job WHERE status = 'completed' GROUP BY business_status, business_trusted"
            ):
                cnt = int(r["cnt"])
                bst = str(r["business_status"] or "")
                if bst in by_biz:
                    by_biz[bst] += cnt
                if int(r["business_trusted"] or 0) == 1:
                    completed_trusted += cnt
                else:
                    completed_untrusted += cnt
        total = sum(stats[k] for k in ("queued", "running", "completed", "failed", "cancelled", "dead_letter"))
        terminal_total = stats["completed"] + stats["failed"] + stats["dead_letter"]
        stats["total"] = total
        stats["completed_trusted"] = completed_trusted
        stats["completed_untrusted"] = completed_untrusted
        stats["by_business_status"] = by_biz
        stats["execution_completion_rate_pct"] = round(stats["completed"] / terminal_total * 100.0, 2) if terminal_total else 0.0
        stats["business_trust_rate_pct"] = round(completed_trusted / terminal_total * 100.0, 2) if terminal_total else 0.0
        return stats

    # ── Worker Heartbeat Registry ──────────────────────────────────────────────
    def record_worker_heartbeat(
        self,
        worker_id: str,
        *,
        worker_kind: str = "external_worker",
        status: str = "idle",
        active_job_id: str | None = None,
        processed_count: int = 0,
    ) -> None:
        now_ep = time.time()
        now_ts = _utc_now()
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO worker_heartbeat (
                    worker_id, worker_kind, pid, hostname, started_at,
                    last_heartbeat_epoch, last_heartbeat_at, status, active_job_id, processed_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    pid = excluded.pid,
                    last_heartbeat_epoch = excluded.last_heartbeat_epoch,
                    last_heartbeat_at = excluded.last_heartbeat_at,
                    status = excluded.status,
                    active_job_id = excluded.active_job_id,
                    processed_count = excluded.processed_count
                """,
                (
                    worker_id,
                    worker_kind,
                    os.getpid(),
                    socket.gethostname(),
                    now_ts,
                    now_ep,
                    now_ts,
                    status,
                    active_job_id,
                    int(processed_count),
                ),
            )
            conn.commit()

    def mark_worker_stopped(self, worker_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE worker_heartbeat SET status = 'stopped', active_job_id = NULL WHERE worker_id = ?", (worker_id,))
            conn.commit()

    def list_workers(self, *, active_within_seconds: float = 30.0) -> list[dict[str, Any]]:
        now_ep = time.time()
        with closing(self._connect()) as conn, conn:
            rows = conn.execute("SELECT * FROM worker_heartbeat ORDER BY last_heartbeat_epoch DESC LIMIT 32").fetchall()
        out = []
        for r in rows:
            age = round(max(0.0, now_ep - float(r["last_heartbeat_epoch"])), 1)
            alive = r["status"] != "stopped" and age <= active_within_seconds
            out.append({
                "worker_id": r["worker_id"],
                "worker_kind": r["worker_kind"],
                "pid": int(r["pid"]),
                "hostname": r["hostname"],
                "status": r["status"] if alive else "stale_or_stopped",
                "alive": alive,
                "heartbeat_age_s": age,
                "active_job_id": r["active_job_id"],
                "processed_count": int(r["processed_count"]),
                "last_heartbeat_at": r["last_heartbeat_at"],
            })
        return out

    # ── Cross-Process Shared Sliding-Window Rate Limiter ───────────────────────
    def check_shared_rate_limit(self, client_key: str, *, max_requests: int, window_seconds: float = 60.0) -> tuple[bool, int, int]:
        now_ep = time.time()
        cutoff = now_ep - float(window_seconds)
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM shared_rate_limit WHERE hit_epoch <= ?", (cutoff,))
            rows = conn.execute(
                "SELECT hit_epoch FROM shared_rate_limit WHERE client_key = ? ORDER BY hit_epoch ASC",
                (client_key,),
            ).fetchall()
            if len(rows) >= max_requests:
                oldest = float(rows[0]["hit_epoch"])
                retry_after = max(1, int(window_seconds - (now_ep - oldest)) + 1)
                conn.commit()
                return False, 0, retry_after
            conn.execute("INSERT INTO shared_rate_limit (client_key, hit_epoch) VALUES (?, ?)", (client_key, now_ep))
            conn.commit()
            return True, max(0, max_requests - len(rows) - 1), 0

    @staticmethod
    def _row_to_job(row: sqlite3.Row, *, include_result: bool = True) -> dict[str, Any]:
        payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
        result = json.loads(row["result_json"]) if (include_result and row["result_json"]) else None
        row_keys = set(row.keys())
        biz_status = str(row["business_status"]) if "business_status" in row_keys else ""
        biz_trusted = bool(row["business_trusted"]) if "business_trusted" in row_keys else False
        if not biz_status and result is not None:
            out = classify_outcome(result, execution_status=row["status"])
            biz_status = out["business_status"]
            biz_trusted = out["business_trusted"]
        return {
            "job_id": row["job_id"],
            "job_type": row["job_type"],
            "status": row["status"],
            "execution_status": row["status"],
            "business_status": biz_status or ("pending" if row["status"] in ("queued", "running") else "error"),
            "business_trusted": biz_trusted,
            "session_id": row["session_id"],
            "actor": row["actor"],
            "role": row["role"],
            "worker_id": row["worker_id"],
            "attempts": int(row["attempts"]),
            "max_attempts": int(row["max_attempts"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "error": row["error"],
            "payload": payload,
            "result": result,
            "stream_url": f"/api/jobs/{row['job_id']}/stream",
            "status_url": f"/api/jobs/{row['job_id']}",
        }


def create_task_broker(path: Path | str | None = None) -> DurableSQLiteBroker:
    """Use the configured shared or offline store for the task queue."""
    target = Path(path) if path else Path(os.environ.get("LAOWADI_JOBS_DB", str(JOBS_DB)))
    return DurableSQLiteBroker(target)
