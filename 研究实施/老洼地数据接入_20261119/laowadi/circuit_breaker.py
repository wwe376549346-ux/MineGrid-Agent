"""Cross-Process Shared Circuit Breaker (CLOSED / OPEN / HALF_OPEN) backed by SQLite WAL.

Every Uvicorn API worker process and every external job worker process shares the EXACT same
circuit-breaker state atomically (`BEGIN IMMEDIATE`). When upstream LLM calls fail `failure_threshold`
consecutive times across ANY workers, the breaker opens globally so all workers fast-fail in <1ms
and trigger automatic graceful degradation to the local deterministic 11-tool baseline.
"""

from __future__ import annotations

from contextlib import closing

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

from laowadi.db.postgres import connect_runtime
from laowadi.task_queue import JOBS_DB, DurableSQLiteBroker


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class SharedCircuitBreaker:
    """Process-safe 3-state circuit breaker persisted across multiple OS worker processes."""

    def __init__(
        self,
        name: str = "llm:deepseek",
        *,
        db_path: Path | str = JOBS_DB,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
    ):
        self.name = name
        self.db_path = Path(db_path)
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(1.0, float(cooldown_seconds))
        # Ensure schema exists via broker initialization
        DurableSQLiteBroker(self.db_path)
        self._ensure_row()

    def _connect(self) -> sqlite3.Connection:
        return connect_runtime(self.db_path, writable=True, wal=True, explicit=True)

    def _ensure_row(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR IGNORE INTO circuit_breaker_state (
                    name, state, failure_count, success_count, failure_threshold,
                    cooldown_seconds, opened_epoch, cooldown_until_epoch,
                    half_open_probe_inflight, last_error, updated_at, updated_by_pid
                ) VALUES (?, 'closed', 0, 0, ?, ?, 0, 0, 0, '', ?, ?)
                """,
                (self.name, self.failure_threshold, self.cooldown_seconds, _utc_now(), os.getpid()),
            )
            conn.commit()

    def allow_request(self) -> tuple[bool, dict[str, Any]]:
        """Atomically check & transition OPEN -> HALF_OPEN when cooldown expires."""
        now_ep = time.time()
        now_ts = _utc_now()
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM circuit_breaker_state WHERE name = ?", (self.name,)).fetchone()
            if row is None:
                conn.commit()
                self._ensure_row()
                return True, self.snapshot()

            state = row["state"]
            cooldown_until = float(row["cooldown_until_epoch"] or 0.0)
            probe_inflight = int(row["half_open_probe_inflight"] or 0)

            if state == "closed":
                conn.commit()
                return True, self._format(row, now_ep)

            if state == "open":
                if now_ep >= cooldown_until:
                    # Transition to HALF_OPEN and grant exactly ONE probe slot across all OS processes!
                    conn.execute(
                        """
                        UPDATE circuit_breaker_state
                        SET state = 'half_open', half_open_probe_inflight = 1,
                            updated_at = ?, updated_by_pid = ?
                        WHERE name = ?
                        """,
                        (now_ts, os.getpid(), self.name),
                    )
                    updated = conn.execute("SELECT * FROM circuit_breaker_state WHERE name = ?", (self.name,)).fetchone()
                    conn.commit()
                    return True, self._format(updated, now_ep)
                conn.commit()
                return False, self._format(row, now_ep)

            # state == "half_open"
            if probe_inflight == 0:
                conn.execute(
                    """
                    UPDATE circuit_breaker_state
                    SET half_open_probe_inflight = 1, updated_at = ?, updated_by_pid = ?
                    WHERE name = ?
                    """,
                    (now_ts, os.getpid(), self.name),
                )
                updated = conn.execute("SELECT * FROM circuit_breaker_state WHERE name = ?", (self.name,)).fetchone()
                conn.commit()
                return True, self._format(updated, now_ep)
            conn.commit()
            return False, self._format(row, now_ep)

    def record_success(self) -> dict[str, Any]:
        now_ep = time.time()
        now_ts = _utc_now()
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE circuit_breaker_state
                SET state = 'closed',
                    failure_count = 0,
                    success_count = success_count + 1,
                    opened_epoch = 0,
                    cooldown_until_epoch = 0,
                    half_open_probe_inflight = 0,
                    last_error = '',
                    updated_at = ?,
                    updated_by_pid = ?
                WHERE name = ?
                """,
                (now_ts, os.getpid(), self.name),
            )
            row = conn.execute("SELECT * FROM circuit_breaker_state WHERE name = ?", (self.name,)).fetchone()
            conn.commit()
        return self._format(row, now_ep)

    def record_failure(self, error_message: str = "") -> dict[str, Any]:
        now_ep = time.time()
        now_ts = _utc_now()
        err = str(error_message or "upstream error")[:240]
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM circuit_breaker_state WHERE name = ?", (self.name,)).fetchone()
            cur_state = row["state"] if row else "closed"
            fails = (int(row["failure_count"]) + 1) if row else 1
            thresh = int(row["failure_threshold"]) if row else self.failure_threshold
            cd = float(row["cooldown_seconds"]) if row else self.cooldown_seconds

            if cur_state == "half_open" or fails >= thresh:
                conn.execute(
                    """
                    UPDATE circuit_breaker_state
                    SET state = 'open',
                        failure_count = ?,
                        opened_epoch = ?,
                        cooldown_until_epoch = ?,
                        half_open_probe_inflight = 0,
                        last_error = ?,
                        updated_at = ?,
                        updated_by_pid = ?
                    WHERE name = ?
                    """,
                    (fails, now_ep, now_ep + cd, err, now_ts, os.getpid(), self.name),
                )
            else:
                conn.execute(
                    """
                    UPDATE circuit_breaker_state
                    SET failure_count = ?,
                        half_open_probe_inflight = 0,
                        last_error = ?,
                        updated_at = ?,
                        updated_by_pid = ?
                    WHERE name = ?
                    """,
                    (fails, err, now_ts, os.getpid(), self.name),
                )
            updated = conn.execute("SELECT * FROM circuit_breaker_state WHERE name = ?", (self.name,)).fetchone()
            conn.commit()
        return self._format(updated, now_ep)

    def force_open(self, reason: str = "手动演练触发跨进程熔断", *, cooldown_seconds: float | None = None) -> dict[str, Any]:
        now_ep = time.time()
        now_ts = _utc_now()
        cd = float(cooldown_seconds) if cooldown_seconds is not None else self.cooldown_seconds
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE circuit_breaker_state
                SET state = 'open',
                    failure_count = failure_threshold,
                    opened_epoch = ?,
                    cooldown_until_epoch = ?,
                    half_open_probe_inflight = 0,
                    last_error = ?,
                    updated_at = ?,
                    updated_by_pid = ?
                WHERE name = ?
                """,
                (now_ep, now_ep + cd, reason[:240], now_ts, os.getpid(), self.name),
            )
            row = conn.execute("SELECT * FROM circuit_breaker_state WHERE name = ?", (self.name,)).fetchone()
            conn.commit()
        return self._format(row, now_ep)

    def reset(self) -> dict[str, Any]:
        return self.record_success()

    def snapshot(self) -> dict[str, Any]:
        now_ep = time.time()
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT * FROM circuit_breaker_state WHERE name = ?", (self.name,)).fetchone()
        if row is None:
            self._ensure_row()
            return self.snapshot()
        return self._format(row, now_ep)

    @staticmethod
    def list_all(db_path: Path | str = JOBS_DB) -> list[dict[str, Any]]:
        DurableSQLiteBroker(db_path)
        now_ep = time.time()
        conn = connect_runtime(db_path, wal=True)
        try:
            rows = conn.execute("SELECT * FROM circuit_breaker_state ORDER BY name ASC").fetchall()
            return [SharedCircuitBreaker._format(r, now_ep) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _format(row: sqlite3.Row, now_ep: float) -> dict[str, Any]:
        cd_until = float(row["cooldown_until_epoch"] or 0.0)
        rem = round(max(0.0, cd_until - now_ep), 1) if row["state"] == "open" else 0.0
        return {
            "name": row["name"],
            "state": row["state"],
            "failure_count": int(row["failure_count"]),
            "success_count": int(row["success_count"]),
            "failure_threshold": int(row["failure_threshold"]),
            "cooldown_seconds": float(row["cooldown_seconds"]),
            "cooldown_remaining_s": rem,
            "half_open_probe_inflight": bool(row["half_open_probe_inflight"]),
            "last_error": row["last_error"] or "",
            "updated_at": row["updated_at"],
            "updated_by_pid": int(row["updated_by_pid"]),
        }
