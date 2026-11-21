"""Tamper-Evident SHA-256 Hash-Chained Audit Ledger & Prometheus/OpenTelemetry-Style Observability.

Maintains a dedicated operational SQLite ledger (`local/laowadi_audit.sqlite3`) separate from
the strictly read-only mine archives (`laowadi.sqlite3` & `laowadi_events.sqlite3`).
Every record links cryptographically to `prev_hash`, enabling deterministic tamper verification.
"""

from __future__ import annotations

from contextlib import closing

from collections import defaultdict, deque
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any
import uuid

from laowadi.db.postgres import connect_runtime
from laowadi.storage import ROOT


AUDIT_DB = ROOT / "local" / "laowadi_audit.sqlite3"
GENESIS_HASH = "0" * 64


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _compute_entry_hash(
    prev_hash: str,
    seq: int,
    timestamp_utc: str,
    trace_id: str,
    actor: str,
    role: str,
    method: str,
    path: str,
    action: str,
    status_code: int,
    request_sha256: str,
    summary_json: str,
) -> str:
    canonical = "|".join([
        prev_hash,
        str(seq),
        timestamp_utc,
        trace_id,
        actor,
        role,
        method,
        path,
        action,
        str(status_code),
        request_sha256,
        summary_json,
    ])
    return _sha256_text(canonical)


class AuditLedger:
    """Append-only SQLite hash-chained audit trail with cryptographic tamper verification."""

    def __init__(self, path: Path | str = AUDIT_DB):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return connect_runtime(self.path, writable=True, wal=True)

    def _init_db(self) -> None:
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_event (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    trace_id TEXT NOT NULL,
                    timestamp_utc TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    latency_ms REAL NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_event(action)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit_event(trace_id)")
            conn.commit()

    def record(
        self,
        *,
        trace_id: str,
        actor: str,
        role: str,
        method: str,
        path: str,
        action: str,
        status_code: int,
        latency_ms: float,
        request_payload: Any = None,
        summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ts = _utc_now()
        event_id = f"AUD-{uuid.uuid4().hex[:12].upper()}"
        req_text = (
            request_payload
            if isinstance(request_payload, str)
            else json.dumps(request_payload or {}, ensure_ascii=False, sort_keys=True, default=str)
        )
        req_hash = _sha256_text(req_text)
        summary_json = json.dumps(summary or {}, ensure_ascii=False, sort_keys=True, default=str)

        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            tip = conn.execute("SELECT seq, entry_hash FROM audit_event ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = tip["entry_hash"] if tip else GENESIS_HASH
            next_seq = (int(tip["seq"]) + 1) if tip else 1
            entry_hash = _compute_entry_hash(
                prev_hash,
                next_seq,
                ts,
                trace_id,
                actor,
                role,
                method,
                path,
                action,
                int(status_code),
                req_hash,
                summary_json,
            )
            conn.execute(
                """
                INSERT INTO audit_event (
                    seq, event_id, trace_id, timestamp_utc, actor, role,
                    method, path, action, status_code, latency_ms,
                    request_sha256, summary_json, prev_hash, entry_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    next_seq,
                    event_id,
                    trace_id,
                    ts,
                    actor,
                    role,
                    method,
                    path,
                    action,
                    int(status_code),
                    round(float(latency_ms), 3),
                    req_hash,
                    summary_json,
                    prev_hash,
                    entry_hash,
                ),
            )
            conn.commit()
        return {
            "seq": next_seq,
            "event_id": event_id,
            "trace_id": trace_id,
            "timestamp_utc": ts,
            "entry_hash": entry_hash,
        }

    def list_events(self, *, limit: int = 50, action: str | None = None) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit), 200))
        with self._lock, closing(self._connect()) as conn, conn:
            if action:
                rows = conn.execute(
                    "SELECT * FROM audit_event WHERE action = ? ORDER BY seq DESC LIMIT ?",
                    (action, lim),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audit_event ORDER BY seq DESC LIMIT ?",
                    (lim,),
                ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["summary"] = json.loads(item.pop("summary_json"))
            except Exception:
                item["summary"] = {}
            out.append(item)
        return out

    def count(self) -> int:
        with self._lock, closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM audit_event").fetchone()
            return int(row["cnt"]) if row else 0

    def verify_chain(self) -> dict[str, Any]:
        """Recompute SHA-256 chain from genesis to head; detects any tampered or deleted row."""
        with self._lock, closing(self._connect()) as conn, conn:
            rows = conn.execute("SELECT * FROM audit_event ORDER BY seq ASC").fetchall()

        expected_prev = GENESIS_HASH
        expected_seq = 1
        head_hash = GENESIS_HASH
        for r in rows:
            seq = int(r["seq"])
            if seq != expected_seq:
                return {
                    "valid": False,
                    "checked_entries": expected_seq - 1,
                    "total_entries": len(rows),
                    "broken_at_seq": seq,
                    "reason": f"审计序号不连续：期望 seq={expected_seq}，实际 seq={seq}（疑似行被删除）。",
                    "head_hash": head_hash,
                }
            if r["prev_hash"] != expected_prev:
                return {
                    "valid": False,
                    "checked_entries": seq - 1,
                    "total_entries": len(rows),
                    "broken_at_seq": seq,
                    "reason": f"seq={seq} 的 prev_hash 断裂，与上一条记录哈希不匹配。",
                    "head_hash": head_hash,
                }
            recomputed = _compute_entry_hash(
                r["prev_hash"],
                seq,
                r["timestamp_utc"],
                r["trace_id"],
                r["actor"],
                r["role"],
                r["method"],
                r["path"],
                r["action"],
                int(r["status_code"]),
                r["request_sha256"],
                r["summary_json"],
            )
            if recomputed != r["entry_hash"]:
                return {
                    "valid": False,
                    "checked_entries": seq - 1,
                    "total_entries": len(rows),
                    "broken_at_seq": seq,
                    "reason": f"seq={seq}（{r['event_id']}）内容哈希校验失败（疑似被篡改）。",
                    "head_hash": head_hash,
                }
            expected_prev = recomputed
            head_hash = recomputed
            expected_seq += 1

        return {
            "valid": True,
            "checked_entries": len(rows),
            "total_entries": len(rows),
            "broken_at_seq": None,
            "reason": "全链 SHA-256 校验通过，未发现删改或篡改痕迹。",
            "head_hash": head_hash,
        }


class ObservabilityRegistry:
    """In-process telemetry collector emitting structured summaries and Prometheus text exposition."""

    def __init__(self):
        self._lock = threading.Lock()
        self.started_at = time.monotonic()
        self._route_counts: dict[tuple[str, str, int], int] = defaultdict(int)
        self._route_latencies: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=500))
        self._tool_counts: dict[str, int] = defaultdict(int)
        self._security_blocks: dict[str, int] = defaultdict(int)
        self._multi_agent_runs: int = 0
        self._recent_traces: deque[dict[str, Any]] = deque(maxlen=60)

    def record_http(
        self,
        *,
        trace_id: str,
        method: str,
        path: str,
        status_code: int,
        latency_ms: float,
        role: str,
    ) -> None:
        with self._lock:
            self._route_counts[(method, path, int(status_code))] += 1
            self._route_latencies[path].append(float(latency_ms))
            self._recent_traces.appendleft({
                "trace_id": trace_id,
                "timestamp_utc": _utc_now(),
                "method": method,
                "path": path,
                "status_code": int(status_code),
                "latency_ms": round(float(latency_ms), 2),
                "role": role,
            })

    def record_tool_calls(self, tool_names: list[str]) -> None:
        with self._lock:
            for name in tool_names:
                self._tool_counts[str(name)] += 1

    def record_security_block(self, category: str) -> None:
        with self._lock:
            self._security_blocks[str(category)] += 1

    def record_multi_agent_run(self) -> None:
        with self._lock:
            self._multi_agent_runs += 1

    @staticmethod
    def _percentile(values: list[float], p: float) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        idx = min(len(s) - 1, max(0, int(round((len(s) - 1) * p))))
        return round(s[idx], 2)

    def summary(self, audit_ledger: AuditLedger | None = None) -> dict[str, Any]:
        with self._lock:
            total_req = sum(self._route_counts.values())
            err_req = sum(c for (_, _, st), c in self._route_counts.items() if st >= 400)
            all_lats = [v for q in self._route_latencies.values() for v in q]
            per_route = []
            by_path_cnt: dict[str, int] = defaultdict(int)
            for (_, path, _), c in self._route_counts.items():
                by_path_cnt[path] += c
            for path, cnt in sorted(by_path_cnt.items(), key=lambda x: -x[1])[:12]:
                lats = list(self._route_latencies.get(path, ()))
                per_route.append({
                    "path": path,
                    "requests": cnt,
                    "p50_ms": self._percentile(lats, 0.50),
                    "p95_ms": self._percentile(lats, 0.95),
                })
            sec_blocks = dict(self._security_blocks)
            tools = dict(sorted(self._tool_counts.items(), key=lambda x: -x[1]))
            traces = list(self._recent_traces)[:20]
            ma_runs = self._multi_agent_runs

        chain_info = audit_ledger.verify_chain() if audit_ledger is not None else None
        return {
            "uptime_seconds": round(time.monotonic() - self.started_at, 2),
            "http_requests_total": total_req,
            "http_errors_total": err_req,
            "latency_p50_ms": self._percentile(all_lats, 0.50),
            "latency_p95_ms": self._percentile(all_lats, 0.95),
            "security_blocks_total": sum(sec_blocks.values()),
            "security_blocks_by_category": sec_blocks,
            "multi_agent_consultations_total": ma_runs,
            "tool_calls_by_name": tools,
            "top_routes": per_route,
            "recent_traces": traces,
            "audit_chain": chain_info,
        }

    def render_prometheus(
        self,
        audit_ledger: AuditLedger | None = None,
        *,
        queue_stats: dict[str, int] | None = None,
        workers_alive: int | None = None,
        circuit_breakers: list[dict[str, Any]] | None = None,
        db_pool: dict[str, Any] | None = None,
        vector_docs_count: int | None = None,
        llmops_lines: list[str] | None = None,
        duty_sla: dict[str, Any] | None = None,
    ) -> str:
        """Render standard Prometheus 0.0.4 text exposition format."""
        lines = [
            "# HELP laowadi_uptime_seconds Process uptime in seconds.",
            "# TYPE laowadi_uptime_seconds gauge",
            f"laowadi_uptime_seconds {time.monotonic() - self.started_at:.2f}",
            "# HELP laowadi_http_requests_total Total HTTP requests processed by route and status.",
            "# TYPE laowadi_http_requests_total counter",
        ]
        with self._lock:
            for (method, path, status), cnt in sorted(self._route_counts.items()):
                safe_path = path.replace('"', "")
                lines.append(f'laowadi_http_requests_total{{method="{method}",path="{safe_path}",status="{status}"}} {cnt}')
            lines += [
                "# HELP laowadi_security_blocks_total Total security firewall and RBAC blocks by category.",
                "# TYPE laowadi_security_blocks_total counter",
            ]
            for cat, cnt in sorted(self._security_blocks.items()):
                lines.append(f'laowadi_security_blocks_total{{category="{cat}"}} {cnt}')
            lines += [
                "# HELP laowadi_tool_calls_total Total read-only diagnostic tool executions.",
                "# TYPE laowadi_tool_calls_total counter",
            ]
            for tool, cnt in sorted(self._tool_counts.items()):
                lines.append(f'laowadi_tool_calls_total{{tool="{tool}"}} {cnt}')
            lines += [
                "# HELP laowadi_multi_agent_consultations_total Total multi-agent specialist consultations.",
                "# TYPE laowadi_multi_agent_consultations_total counter",
                f"laowadi_multi_agent_consultations_total {self._multi_agent_runs}",
            ]
        if audit_ledger is not None:
            chain = audit_ledger.verify_chain()
            lines += [
                "# HELP laowadi_audit_chain_entries Total records in SHA-256 hash-chained audit ledger.",
                "# TYPE laowadi_audit_chain_entries gauge",
                f"laowadi_audit_chain_entries {chain['total_entries']}",
                "# HELP laowadi_audit_chain_valid Cryptographic integrity status of the audit ledger (1=valid, 0=tampered).",
                "# TYPE laowadi_audit_chain_valid gauge",
                f"laowadi_audit_chain_valid {1 if chain['valid'] else 0}",
            ]
        if queue_stats is not None:
            lines += [
                "# HELP laowadi_task_queue_jobs Number of jobs in external durable queue by execution status.",
                "# TYPE laowadi_task_queue_jobs gauge",
            ]
            for st in ("queued", "running", "completed", "failed", "cancelled", "dead_letter"):
                lines.append(f'laowadi_task_queue_jobs{{status="{st}"}} {queue_stats.get(st, 0)}')
            lines += [
                "# HELP laowadi_task_queue_completed_trusted Completed jobs whose business output passed verification.",
                "# TYPE laowadi_task_queue_completed_trusted gauge",
                f"laowadi_task_queue_completed_trusted {int(queue_stats.get('completed_trusted', 0))}",
                "# HELP laowadi_task_queue_completed_untrusted Completed jobs whose business output is untrusted/warning/refused.",
                "# TYPE laowadi_task_queue_completed_untrusted gauge",
                f"laowadi_task_queue_completed_untrusted {int(queue_stats.get('completed_untrusted', 0))}",
            ]
            by_biz = queue_stats.get("by_business_status") or {}
            if by_biz:
                lines += [
                    "# HELP laowadi_task_queue_jobs_by_business_status Completed jobs partitioned by business outcome layer.",
                    "# TYPE laowadi_task_queue_jobs_by_business_status gauge",
                ]
                for bst, cnt in sorted(by_biz.items()):
                    lines.append(f'laowadi_task_queue_jobs_by_business_status{{business_status="{bst}"}} {int(cnt)}')
        if workers_alive is not None:
            lines += [
                "# HELP laowadi_workers_alive Active API and external job worker OS processes.",
                "# TYPE laowadi_workers_alive gauge",
                f"laowadi_workers_alive {workers_alive}",
            ]
        if circuit_breakers:
            lines += [
                "# HELP laowadi_circuit_breaker_open Cross-process circuit breaker state (1=open, 0=closed/half_open).",
                "# TYPE laowadi_circuit_breaker_open gauge",
            ]
            for cb in circuit_breakers:
                val = 1 if cb.get("state") == "open" else 0
                lines.append(f'laowadi_circuit_breaker_open{{name="{cb.get("name", "")}"}} {val}')
        if db_pool is not None:
            lines += [
                "# HELP laowadi_db_pool_size Configured SQLAlchemy 2.0 QueuePool size.",
                "# TYPE laowadi_db_pool_size gauge",
                f"laowadi_db_pool_size {int(db_pool.get('pool_size', 0))}",
                "# HELP laowadi_db_pool_checked_in Idle connections currently checked in to SQLAlchemy QueuePool.",
                "# TYPE laowadi_db_pool_checked_in gauge",
                f"laowadi_db_pool_checked_in {int(db_pool.get('checked_in', 0))}",
                "# HELP laowadi_db_pool_checked_out Active connections currently checked out from SQLAlchemy QueuePool.",
                "# TYPE laowadi_db_pool_checked_out gauge",
                f"laowadi_db_pool_checked_out {int(db_pool.get('checked_out', 0))}",
            ]
        if vector_docs_count is not None:
            lines += [
                "# HELP laowadi_vector_documents_total Total 128-dim semantic embeddings indexed in pgvector table.",
                "# TYPE laowadi_vector_documents_total gauge",
                f"laowadi_vector_documents_total {int(vector_docs_count)}",
            ]
        if llmops_lines:
            lines.extend(llmops_lines)
        if duty_sla is not None:
            lines += [
                "# HELP laowadi_tickets_open Currently open shift incident tickets awaiting acknowledgment.",
                "# TYPE laowadi_tickets_open gauge",
                f"laowadi_tickets_open {int(duty_sla.get('tickets_open', 0))}",
                "# HELP laowadi_tickets_active Currently active non-closed shift incident tickets.",
                "# TYPE laowadi_tickets_active gauge",
                f"laowadi_tickets_active {int(duty_sla.get('tickets_active', 0))}",
                "# HELP laowadi_tickets_ack_latency_avg_s Mean operator acknowledgment latency in seconds.",
                "# TYPE laowadi_tickets_ack_latency_avg_s gauge",
                f"laowadi_tickets_ack_latency_avg_s {float(duty_sla.get('avg_ack_latency_s', 0.0))}",
                "# HELP laowadi_outbox_pending Pending outbound alert notifications in transactional outbox.",
                "# TYPE laowadi_outbox_pending gauge",
                f"laowadi_outbox_pending {int(duty_sla.get('outbox_pending', 0))}",
                "# HELP laowadi_outbox_failed Failed outbound alert notifications awaiting retry.",
                "# TYPE laowadi_outbox_failed gauge",
                f"laowadi_outbox_failed {int(duty_sla.get('outbox_failed', 0))}",
                "# HELP laowadi_outbox_delivered Successfully delivered outbound alert notifications.",
                "# TYPE laowadi_outbox_delivered counter",
                f"laowadi_outbox_delivered {int(duty_sla.get('outbox_delivered', 0))}",
            ]
        lines.append("")
        return "\n".join(lines)
