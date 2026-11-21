"""Unified Dynamic DAG Execution & Arbitrary-Node Checkpoint/Resume Runtime.

Unifies all three agent execution paths onto ONE shared execution kernel:
1. Primary Diagnosis (`laowadi/diagnose.py`)
2. 5-Role Parallel Multi-Agent Consultation (`laowadi/multi_agent.py`)
3. Auditable Checkpointed State-Graph (`laowadi/graph.py`)

Key Enterprise Capabilities:
- Dynamic DAG compilation for any free-form question with Kahn topological wave scheduling & cycle validation.
- Parallel wave execution (`ThreadPoolExecutor`) for independent sibling nodes (e.g., specialist fan-out).
- Arbitrary-node suspend & cross-process resume (`pause_before_node`, `pause_after_node`, or dynamic `NodeInterrupt`).
- Cryptographic SHA-256 receipt caching + archive snapshot/input binding so resumed threads skip 100% of
  already-completed nodes (0 duplicate executions) and cumulatively merge multi-round human inputs.
"""

from __future__ import annotations

from contextlib import closing

from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Callable
import uuid

from laowadi.db.postgres import connect_runtime
from laowadi.logging_config import get_logger, log_event
from laowadi.mine_schema import MineProfile, load_mine_profile
from laowadi.storage import ROOT


DAG_CHECKPOINT_DB = ROOT / "local" / "laowadi_checkpoints.sqlite3"
LOGGER = get_logger("dag_engine")
VALID_RESUME_POLICIES = ("strict", "pin_snapshot", "recompute_affected")
_DAY_IN_TEXT_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_XLS_DAY_RE = re.compile(r"\b(\d{2})(\d{2})\.xls\b", re.I)


def _hash_payload(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def compute_snapshot(archive, profile: MineProfile | None = None) -> dict[str, Any]:
    prof = profile or load_mine_profile()
    reports = {
        r["day"]: {"filename": r["filename"], "sha256": r["sha256"]}
        for r in archive.db.execute("SELECT day, filename, sha256 FROM report ORDER BY day").fetchall()
    }
    diagrams = {
        r["filename"]: r["sha256"]
        for r in archive.db.execute("SELECT filename, sha256 FROM diagram ORDER BY filename").fetchall()
    }
    digest = hashlib.sha256(
        json.dumps({"reports": reports, "diagrams": diagrams}, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {
        "mine_id": prof.mine_id,
        "station": prof.primary_station,
        "reports_count": len(reports),
        "diagrams_count": len(diagrams),
        "reports": reports,
        "diagrams": diagrams,
        "snapshot_sha256": digest,
    }


def compute_snapshot_drift(old_snap: dict[str, Any], new_snap: dict[str, Any]) -> dict[str, Any]:
    """Compute per-day and per-diagram drift between checkpoint snapshot and live archive snapshot."""
    old_reports = old_snap.get("reports") or {}
    new_reports = new_snap.get("reports") or {}
    old_diagrams = old_snap.get("diagrams") or {}
    new_diagrams = new_snap.get("diagrams") or {}
    old_days = set(old_reports.keys())
    new_days = set(new_reports.keys())
    added_days = sorted(new_days - old_days)
    removed_days = sorted(old_days - new_days)
    modified_days = sorted(
        d for d in (old_days & new_days)
        if (old_reports[d].get("sha256") if isinstance(old_reports[d], dict) else old_reports[d])
        != (new_reports[d].get("sha256") if isinstance(new_reports[d], dict) else new_reports[d])
    )
    all_diag_keys = set(old_diagrams.keys()) | set(new_diagrams.keys())
    modified_diagrams = sorted(k for k in all_diag_keys if old_diagrams.get(k) != new_diagrams.get(k))
    changed = old_snap.get("snapshot_sha256") != new_snap.get("snapshot_sha256")
    return {
        "changed": changed,
        "drift_detected": changed,
        "has_manifest": bool(old_reports or new_reports),
        "old_snapshot_sha256": old_snap.get("snapshot_sha256", ""),
        "new_snapshot_sha256": new_snap.get("snapshot_sha256", ""),
        "added_days": added_days,
        "removed_days": removed_days,
        "modified_days": modified_days,
        "modified_diagrams": modified_diagrams,
        "changed_days": sorted(set(added_days) | set(removed_days) | set(modified_days)),
    }


def _infer_node_data_scope(node: "DAGNode", output: Any) -> dict[str, Any]:
    """Infer which archive days/diagrams a completed DAG node depended on for impact-scoped recomputation."""
    if node.kind in ("guard", "plan"):
        return {"days": [], "reads_all_days": False, "reads_diagrams": False}
    explicit_scope = node.metadata.get("data_scope")
    if isinstance(explicit_scope, dict):
        return {
            "days": sorted(set(explicit_scope.get("days") or [])),
            "reads_all_days": bool(explicit_scope.get("reads_all_days", False)),
            "reads_diagrams": bool(explicit_scope.get("reads_diagrams", False)),
        }
    blob = json.dumps({"meta": node.metadata, "output": output}, ensure_ascii=False, default=str)
    days = set(_DAY_IN_TEXT_RE.findall(blob))
    for mm, dd in _XLS_DAY_RE.findall(blob):
        days.add(f"2026-{mm}-{dd}")
    tool_name = str(node.metadata.get("tool") or "")
    tool_args = node.metadata.get("arguments") if isinstance(node.metadata.get("arguments"), dict) else {}
    reads_all_days = False
    if tool_name in ("get_cabinet_health_profile", "verify_conditional_capacity"):
        reads_all_days = True
    elif tool_name in ("get_station_health_overview", "simulate_electrical_engineering") and not tool_args.get("day"):
        reads_all_days = True
    elif not days and node.kind in ("tool", "specialist"):
        reads_all_days = True
    reads_diagrams = bool("diagram" in blob.lower() or "supply_link" in blob.lower() or "1535" in blob)
    return {
        "days": sorted(days),
        "reads_all_days": reads_all_days,
        "reads_diagrams": reads_diagrams,
    }


class NodeInterrupt(Exception):
    """Raised by any DAG node to pause execution and persist a resumable HITL checkpoint."""

    def __init__(
        self,
        reason: str,
        *,
        missing_inputs: list[str] | None = None,
        prompt: str = "",
        payload: dict[str, Any] | None = None,
    ):
        super().__init__(reason)
        self.reason = reason
        self.missing_inputs = list(missing_inputs or [])
        self.prompt = prompt or reason
        self.payload = dict(payload or {})


class NodeRestricted(Exception):
    """Raised by a guard/policy node to terminate the DAG cleanly in `restricted` status."""

    def __init__(self, stop_reason: str, message: str, *, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.stop_reason = stop_reason
        self.message = message
        self.payload = dict(payload or {})


@dataclass
class DAGNode:
    """Single executable node in a dynamically compiled Agent DAG."""

    node_id: str
    kind: str  # guard | plan | tool | specialist | gate_hitl | arbitrate | synthesize | verify
    title: str
    handler: Callable[["DAGRunContext"], Any]
    depends_on: list[str] = field(default_factory=list)
    counts_toward_budget: bool = False
    interrupt_before: bool = False
    interrupt_after: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DAGRunContext:
    """Shared read-view passed to every node handler during DAG execution."""

    thread_id: str
    mode: str
    question: str
    node_id: str
    completed_outputs: dict[str, Any]
    human_inputs: dict[str, Any]
    emit: Callable[..., None]


class DAGGraph:
    """Validated Directed Acyclic Graph with Kahn topological wave partitioning."""

    def __init__(self, name: str, nodes: list[DAGNode] | None = None):
        self.name = name
        self._nodes: dict[str, DAGNode] = {}
        self._order: list[str] = []
        for n in (nodes or []):
            self.add_node(n)

    def add_node(self, node: DAGNode) -> "DAGGraph":
        if node.node_id in self._nodes:
            raise ValueError(f"Duplicate DAG node_id: {node.node_id}")
        self._nodes[node.node_id] = node
        self._order.append(node.node_id)
        return self

    def nodes(self) -> list[DAGNode]:
        return [self._nodes[nid] for nid in self._order]

    def edges(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for nid in self._order:
            for dep in self._nodes[nid].depends_on:
                out.append((dep, nid))
        return out

    def topological_waves(self) -> list[list[DAGNode]]:
        """Partition nodes into parallel-ready topological waves; raises ValueError on cycles or missing deps."""
        in_deg: dict[str, int] = {}
        dependents: dict[str, list[str]] = {nid: [] for nid in self._order}
        for nid in self._order:
            deps = self._nodes[nid].depends_on
            for dep in deps:
                if dep not in self._nodes:
                    raise ValueError(f"DAG node {nid!r} depends on unknown node {dep!r}")
                dependents[dep].append(nid)
            in_deg[nid] = len(deps)

        ready = deque([nid for nid in self._order if in_deg[nid] == 0])
        waves: list[list[DAGNode]] = []
        visited = 0
        while ready:
            wave_ids = list(ready)
            ready.clear()
            waves.append([self._nodes[nid] for nid in wave_ids])
            visited += len(wave_ids)
            for nid in wave_ids:
                for nxt in dependents[nid]:
                    in_deg[nxt] -= 1
                    if in_deg[nxt] == 0:
                        ready.append(nxt)

        if visited != len(self._order):
            raise ValueError(f"Cycle detected in DAG {self.name!r}")
        return waves

    def describe(
        self,
        *,
        completed_nodes: dict[str, Any] | None = None,
        reused_nodes: list[str] | None = None,
        suspended_at: str | None = None,
    ) -> dict[str, Any]:
        comp = completed_nodes or {}
        reused = set(reused_nodes or [])
        waves = self.topological_waves()
        node_specs = []
        for w_idx, wave in enumerate(waves):
            for n in wave:
                if n.node_id == suspended_at:
                    st = "suspended"
                elif n.node_id in reused:
                    st = "reused"
                elif n.node_id in comp:
                    st = "completed"
                else:
                    st = "pending"
                rec = comp.get(n.node_id) or {}
                node_specs.append({
                    "node_id": n.node_id,
                    "kind": n.kind,
                    "title": n.title,
                    "wave": w_idx,
                    "depends_on": list(n.depends_on),
                    "status": st,
                    "elapsed_ms": rec.get("elapsed_ms"),
                    "receipt_sha256": rec.get("receipt_sha256"),
                    "metadata": n.metadata,
                })
        return {
            "graph_name": self.name,
            "node_count": len(self._order),
            "wave_count": len(waves),
            "waves": [[n.node_id for n in w] for w in waves],
            "nodes": node_specs,
            "edges": [[s, t] for s, t in self.edges()],
        }


class DAGCheckpointStore:
    """SQLite WAL-backed checkpoint persistence for cross-process arbitrary-node DAG resume."""

    def __init__(self, path: Path | str = DAG_CHECKPOINT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return connect_runtime(self.path, writable=True, wal=True)

    def _init_db(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dag_checkpoint (
                    thread_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    question TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    suspended_at_node TEXT,
                    updated_at TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    lock_owner TEXT,
                    lock_expires_at REAL NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            try:
                cols = {r["name"] for r in conn.execute("PRAGMA table_info(dag_checkpoint)").fetchall()}
                if "lock_owner" not in cols:
                    conn.execute("ALTER TABLE dag_checkpoint ADD COLUMN lock_owner TEXT")
                if "lock_expires_at" not in cols:
                    conn.execute("ALTER TABLE dag_checkpoint ADD COLUMN lock_expires_at REAL NOT NULL DEFAULT 0")
                if "version" not in cols:
                    conn.execute("ALTER TABLE dag_checkpoint ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
            except Exception:
                pass
            conn.commit()

    def acquire_resume_lock(self, thread_id: str, owner_id: str, *, lease_seconds: float = 30.0) -> tuple[bool, dict[str, Any] | None]:
        """Atomically acquire exclusive resume lease on a checkpoint thread to prevent concurrent double-resume."""
        now_ep = time.time()
        with closing(self._connect()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except Exception:
                pass
            row = conn.execute(
                "SELECT lock_owner, lock_expires_at, version FROM dag_checkpoint WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if row is None:
                conn.commit()
                return True, None
            holder = row["lock_owner"]
            exp = float(row["lock_expires_at"] or 0.0)
            if holder and holder != owner_id and exp > now_ep:
                conn.commit()
                return False, {"lock_owner": holder, "lock_expires_at": exp, "version": int(row["version"] or 1)}
            conn.execute(
                "UPDATE dag_checkpoint SET lock_owner = ?, lock_expires_at = ? WHERE thread_id = ?",
                (owner_id, now_ep + max(1.0, float(lease_seconds)), thread_id),
            )
            conn.commit()
            return True, None

    def release_resume_lock(self, thread_id: str, owner_id: str) -> None:
        with closing(self._connect()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except Exception:
                pass
            conn.execute(
                "UPDATE dag_checkpoint SET lock_owner = NULL, lock_expires_at = 0 WHERE thread_id = ? AND lock_owner = ?",
                (thread_id, owner_id),
            )
            conn.commit()

    def save(self, state: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        state["updated_at"] = now
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO dag_checkpoint (
                    thread_id, mode, question, input_hash, snapshot_sha256,
                    status, suspended_at_node, updated_at, state_json, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(thread_id) DO UPDATE SET
                    mode = excluded.mode,
                    question = excluded.question,
                    input_hash = excluded.input_hash,
                    snapshot_sha256 = excluded.snapshot_sha256,
                    status = excluded.status,
                    suspended_at_node = excluded.suspended_at_node,
                    updated_at = excluded.updated_at,
                    state_json = excluded.state_json,
                    version = dag_checkpoint.version + 1
                """,
                (
                    state["thread_id"],
                    state["mode"],
                    state["question"],
                    state["input_hash"],
                    state["snapshot"]["snapshot_sha256"],
                    state["status"],
                    state.get("suspended_at_node"),
                    now,
                    json.dumps(state, ensure_ascii=False, default=str),
                ),
            )
            conn.commit()

    def load(self, thread_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT state_json FROM dag_checkpoint WHERE thread_id = ?", (thread_id,)).fetchone()
        return json.loads(row["state_json"]) if row else None

    def list_threads(self, limit: int = 30) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit), 100))
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                """
                SELECT thread_id, mode, question, status, suspended_at_node, updated_at, state_json
                FROM dag_checkpoint
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
        out = []
        for r in rows:
            st = json.loads(r["state_json"])
            out.append({
                "thread_id": r["thread_id"],
                "mode": r["mode"],
                "question": r["question"],
                "status": r["status"],
                "suspended_at_node": r["suspended_at_node"],
                "updated_at": r["updated_at"],
                "resume_count": st.get("resume_count", 0),
                "completed_count": len(st.get("completed_nodes") or {}),
                "node_count": (st.get("dag_topology") or {}).get("node_count", 0),
                "interrupt": st.get("interrupt"),
            })
        return out


class UnifiedDAGRuntime:
    """Single shared DAG runtime powering diagnose, multi-agent, and checkpointed audit graphs."""

    def __init__(self, store: DAGCheckpointStore | None = None):
        self.store = store or DAGCheckpointStore()

    def execute(
        self,
        graph: DAGGraph,
        *,
        thread_id: str | None = None,
        mode: str = "diagnose",
        question: str = "",
        input_spec: dict[str, Any] | None = None,
        archive_snapshot: dict[str, Any] | None = None,
        max_budget: int | None = None,
        pause_before_node: str | None = None,
        pause_after_node: str | None = None,
        human_input: dict[str, Any] | None = None,
        resume_policy: str = "strict",
        lock_lease_seconds: float = 30.0,
        persist: bool = True,
        parallel: bool = False,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if resume_policy not in VALID_RESUME_POLICIES:
            raise ValueError(f"无效的恢复策略：{resume_policy}（支持 {', '.join(VALID_RESUME_POLICIES)}）")
        t0 = time.monotonic()
        tid = thread_id or f"DAG-{mode[:4].upper()}-{uuid.uuid4().hex[:10].upper()}"
        snap = archive_snapshot or {"snapshot_sha256": "no-snapshot"}
        spec = input_spec if input_spec is not None else {"mode": mode, "question": question}
        input_hash = _hash_payload(spec)

        def emit(ev_type: str, **payload: Any) -> None:
            if on_event is not None:
                on_event({"type": ev_type, "thread_id": tid, "ts": round(time.monotonic() - t0, 3), **payload})

        waves = graph.topological_waves()
        lock_owner = f"dag-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        lock_acquired = False
        if persist:
            ok, holder = self.store.acquire_resume_lock(tid, lock_owner, lease_seconds=lock_lease_seconds)
            if not ok:
                existing_locked = self.store.load(tid) or {"thread_id": tid, "mode": mode, "question": question, "snapshot": snap}
                return {
                    **existing_locked,
                    "status": "conflict",
                    "stop_reason": "concurrent_resume_in_progress",
                    "concurrent_conflict": True,
                    "lock_holder": holder,
                    "resume_policy": resume_policy,
                    "executed_nodes": [],
                    "reused_nodes": [],
                    "invalidated_nodes": [],
                    "node_trace": list(existing_locked.get("node_trace") or []) + ["dag:conflict:concurrent_resume_in_progress"],
                }
            lock_acquired = True

        try:
            existing = self.store.load(tid) if persist else None
            executed_nodes: list[str] = []
            reused_nodes: list[str] = []
            invalidated_nodes: list[str] = []
            node_trace: list[str] = []
            snapshot_drift: dict[str, Any] | None = None
            review_mode = "live_snapshot"

            if existing is not None:
                node_trace.append("dag:resume_from_checkpoint")
                if existing["input_hash"] != input_hash:
                    return {
                        **existing,
                        "status": "restricted",
                        "stop_reason": "input_binding_mismatch",
                        "resume_policy": resume_policy,
                        "executed_nodes": [],
                        "reused_nodes": [],
                        "invalidated_nodes": [],
                        "node_trace": existing.get("node_trace", []) + node_trace + ["dag:restricted:input_binding_mismatch"],
                    }
                if existing["snapshot"]["snapshot_sha256"] != snap["snapshot_sha256"]:
                    snapshot_drift = compute_snapshot_drift(existing["snapshot"], snap)
                    if resume_policy == "strict":
                        return {
                            **existing,
                            "status": "restricted",
                            "stop_reason": "snapshot_version_mismatch",
                            "resume_policy": resume_policy,
                            "snapshot_drift": snapshot_drift,
                            "executed_nodes": [],
                            "reused_nodes": [],
                            "invalidated_nodes": [],
                            "node_trace": existing.get("node_trace", []) + node_trace + ["dag:restricted:snapshot_version_mismatch"],
                        }
                    if resume_policy == "pin_snapshot":
                        review_mode = "pinned_historical_snapshot"
                        node_trace.append("dag:pin_historical_snapshot")
                        emit("dag_snapshot_pinned", pinned_snapshot_sha256=existing["snapshot"]["snapshot_sha256"], drift=snapshot_drift)
                    elif resume_policy == "recompute_affected":
                        review_mode = "impact_scoped_recompute"
                        comp_existing = dict(existing.get("completed_nodes") or {})
                        mod_or_rem = set(snapshot_drift["modified_days"]) | set(snapshot_drift["removed_days"])
                        any_day_change = bool(snapshot_drift["changed_days"])
                        diag_changed = bool(snapshot_drift["modified_diagrams"])
                        dependents: dict[str, list[str]] = {n.node_id: [] for n in graph.nodes()}
                        for src, dst in graph.edges():
                            dependents.setdefault(src, []).append(dst)

                        direct_invalid: set[str] = set()
                        for nid, rec in comp_existing.items():
                            kind = rec.get("kind") or ""
                            if kind in ("guard", "plan"):
                                continue
                            if not snapshot_drift["has_manifest"]:
                                direct_invalid.add(nid)
                                continue
                            scope = rec.get("data_scope") or {"days": [], "reads_all_days": True, "reads_diagrams": False}
                            node_days = set(scope.get("days") or [])
                            if scope.get("reads_all_days") and any_day_change:
                                direct_invalid.add(nid)
                            elif node_days & mod_or_rem:
                                direct_invalid.add(nid)
                            elif scope.get("reads_diagrams") and diag_changed:
                                direct_invalid.add(nid)

                        all_invalid: set[str] = set(direct_invalid)
                        q_inv = deque(direct_invalid)
                        while q_inv:
                            cur = q_inv.popleft()
                            for nxt in dependents.get(cur, []):
                                if nxt in comp_existing and nxt not in all_invalid:
                                    all_invalid.add(nxt)
                                    q_inv.append(nxt)

                        for nid in [n.node_id for n in graph.nodes() if n.node_id in all_invalid]:
                            comp_existing.pop(nid, None)
                            invalidated_nodes.append(nid)
                            node_trace.append(f"invalidate:{nid}")
                        existing["completed_nodes"] = comp_existing
                        existing["snapshot"] = snap
                        emit("dag_impact_recompute", invalidated_nodes=invalidated_nodes, drift=snapshot_drift)

                state = existing
                state["resume_count"] = int(state.get("resume_count", 0)) + 1
                state["resume_policy"] = resume_policy
                state["review_mode"] = review_mode
                if snapshot_drift is not None:
                    state["snapshot_drift"] = snapshot_drift
                    if resume_policy == "pin_snapshot":
                        state["pinned_snapshot_sha256"] = existing["snapshot"]["snapshot_sha256"]
                        state["current_snapshot_sha256"] = snap["snapshot_sha256"]

                prev_hi = dict(state.get("accumulated_human_inputs") or {})
                if human_input:
                    prev_params = dict(prev_hi.get("parameters") or {})
                    new_params = dict(human_input.get("parameters") or {})
                    merged_hi = {**prev_hi, **human_input}
                    if prev_params or new_params:
                        merged_hi["parameters"] = {**prev_params, **new_params}
                    approved = set(prev_hi.get("_approved_nodes") or [])
                    if state.get("suspended_at_node"):
                        approved.add(state["suspended_at_node"])
                    if human_input.get("approve_node"):
                        approved.add(str(human_input["approve_node"]))
                    merged_hi["_approved_nodes"] = sorted(approved)
                    state["accumulated_human_inputs"] = merged_hi
                elif state.get("suspended_at_node"):
                    approved = set(prev_hi.get("_approved_nodes") or [])
                    approved.add(state["suspended_at_node"])
                    prev_hi["_approved_nodes"] = sorted(approved)
                    state["accumulated_human_inputs"] = prev_hi
                emit("dag_resumed", mode=mode, resume_count=state["resume_count"], completed_count=len(state.get("completed_nodes") or {}), resume_policy=resume_policy)
            else:
                node_trace.append("dag:new_thread")
                state = {
                    "thread_id": tid,
                    "mode": mode,
                    "question": question,
                    "input_spec": spec,
                    "input_hash": input_hash,
                    "snapshot": snap,
                    "max_budget": max_budget,
                    "budget_used": 0,
                    "resume_count": 0,
                    "resume_policy": resume_policy,
                    "review_mode": review_mode,
                    "snapshot_drift": None,
                    "status": "running",
                    "stop_reason": "",
                    "suspended_at_node": None,
                    "interrupt": None,
                    "completed_nodes": {},
                    "accumulated_human_inputs": dict(human_input or {}),
                    "node_trace": [],
                    "dag_topology": graph.describe(),
                }
                emit("dag_compiled", mode=mode, graph_name=graph.name, waves=[[n.node_id for n in w] for w in waves])

            completed_nodes: dict[str, dict[str, Any]] = state["completed_nodes"]
            human_inputs: dict[str, Any] = state["accumulated_human_inputs"]
            approved_nodes = set(human_inputs.get("_approved_nodes") or [])

            def _outputs_view() -> dict[str, Any]:
                return {nid: rec["output"] for nid, rec in completed_nodes.items()}

            def _persist_and_return(status: str, stop_reason: str, suspended_at: str | None = None, interrupt: dict | None = None) -> dict[str, Any]:
                state["status"] = status
                state["stop_reason"] = stop_reason
                state["suspended_at_node"] = suspended_at
                state["interrupt"] = interrupt
                state["node_trace"] = list(state.get("node_trace") or []) + node_trace
                state["executed_nodes"] = executed_nodes
                state["reused_nodes"] = reused_nodes
                state["invalidated_nodes"] = invalidated_nodes
                state["elapsed_seconds"] = round(time.monotonic() - t0, 3)
                state["dag_topology"] = graph.describe(
                    completed_nodes=completed_nodes,
                    reused_nodes=reused_nodes,
                    suspended_at=suspended_at,
                )
                if persist:
                    self.store.save(state)
                log_event(
                    LOGGER,
                    logging.INFO,
                    f"dag_{status}",
                    f"DAG {tid} ({mode}) -> {status} ({stop_reason})",
                    thread_id=tid,
                    mode=mode,
                    executed=len(executed_nodes),
                    reused=len(reused_nodes),
                    invalidated=len(invalidated_nodes),
                    suspended_at=suspended_at or "",
                )
                return state

            for wave in waves:
                runnable: list[DAGNode] = []
                for node in wave:
                    if node.node_id in completed_nodes:
                        reused_nodes.append(node.node_id)
                        node_trace.append(f"reuse:{node.node_id}")
                        emit("dag_node_reused", node_id=node.node_id, kind=node.kind, title=node.title)
                        continue

                    if (pause_before_node == node.node_id or node.interrupt_before) and node.node_id not in approved_nodes:
                        node_trace.append(f"pause_before:{node.node_id}")
                        intr = {
                            "reason": "breakpoint_pause_before_node",
                            "node_id": node.node_id,
                            "node_title": node.title,
                            "missing_inputs": [],
                            "prompt": f"执行已在节点「{node.title} ({node.node_id})」前挂起，等待人工审批或补充参数后继续执行。",
                            "payload": {"breakpoint": "before"},
                        }
                        emit("dag_suspended", node_id=node.node_id, interrupt=intr)
                        return _persist_and_return("suspended", "awaiting_human_approval", suspended_at=node.node_id, interrupt=intr)

                    if max_budget is not None and node.counts_toward_budget and state["budget_used"] >= max_budget:
                        node_trace.append(f"restricted:budget_exhausted:{node.node_id}")
                        return _persist_and_return("restricted", "budget_exhausted")

                    runnable.append(node)

                if not runnable:
                    continue

                def _run_one(n: DAGNode) -> tuple[DAGNode, Any, float]:
                    t_n = time.monotonic()
                    ctx = DAGRunContext(
                        thread_id=tid,
                        mode=mode,
                        question=question,
                        node_id=n.node_id,
                        completed_outputs=_outputs_view(),
                        human_inputs=human_inputs,
                        emit=emit,
                    )
                    out = n.handler(ctx)
                    return n, out, round((time.monotonic() - t_n) * 1000.0, 2)

                if parallel and len(runnable) > 1:
                    for n in runnable:
                        node_trace.append(f"exec:{n.node_id}")
                        emit("dag_node_start", node_id=n.node_id, kind=n.kind, title=n.title)
                    results_wave: list[tuple[DAGNode, Any, float]] = []
                    with ThreadPoolExecutor(max_workers=len(runnable)) as pool:
                        futs = {pool.submit(_run_one, n): n for n in runnable}
                        for fut in as_completed(futs):
                            n_obj = futs[fut]
                            try:
                                results_wave.append(fut.result())
                            except NodeInterrupt as intr_exc:
                                node_trace.append(f"interrupt:{n_obj.node_id}")
                                intr = {
                                    "reason": intr_exc.reason,
                                    "node_id": n_obj.node_id,
                                    "node_title": n_obj.title,
                                    "missing_inputs": intr_exc.missing_inputs,
                                    "prompt": intr_exc.prompt,
                                    "payload": intr_exc.payload,
                                }
                                emit("dag_suspended", node_id=n_obj.node_id, interrupt=intr)
                                return _persist_and_return("suspended", intr_exc.reason, suspended_at=n_obj.node_id, interrupt=intr)
                            except NodeRestricted as rest_exc:
                                node_trace.append(f"restricted:{n_obj.node_id}:{rest_exc.stop_reason}")
                                state["restricted_payload"] = rest_exc.payload
                                return _persist_and_return("restricted", rest_exc.stop_reason)
                    by_id = {n.node_id: (n, out, ms) for n, out, ms in results_wave}
                    for n in runnable:
                        _, out, ms = by_id[n.node_id]
                        if n.counts_toward_budget:
                            state["budget_used"] += 1
                        completed_nodes[n.node_id] = {
                            "node_id": n.node_id,
                            "kind": n.kind,
                            "title": n.title,
                            "output": out,
                            "receipt_sha256": _hash_payload(out),
                            "data_scope": _infer_node_data_scope(n, out),
                            "elapsed_ms": ms,
                        }
                        executed_nodes.append(n.node_id)
                        emit("dag_node_done", node_id=n.node_id, kind=n.kind, elapsed_ms=ms)
                else:
                    for n in runnable:
                        node_trace.append(f"exec:{n.node_id}")
                        emit("dag_node_start", node_id=n.node_id, kind=n.kind, title=n.title)
                        try:
                            _, out, ms = _run_one(n)
                        except NodeInterrupt as intr_exc:
                            node_trace.append(f"interrupt:{n.node_id}")
                            intr = {
                                "reason": intr_exc.reason,
                                "node_id": n.node_id,
                                "node_title": n.title,
                                "missing_inputs": intr_exc.missing_inputs,
                                "prompt": intr_exc.prompt,
                                "payload": intr_exc.payload,
                            }
                            emit("dag_suspended", node_id=n.node_id, interrupt=intr)
                            return _persist_and_return("suspended", intr_exc.reason, suspended_at=n.node_id, interrupt=intr)
                        except NodeRestricted as rest_exc:
                            node_trace.append(f"restricted:{n.node_id}:{rest_exc.stop_reason}")
                            state["restricted_payload"] = rest_exc.payload
                            return _persist_and_return("restricted", rest_exc.stop_reason)

                        if n.counts_toward_budget:
                            state["budget_used"] += 1
                        completed_nodes[n.node_id] = {
                            "node_id": n.node_id,
                            "kind": n.kind,
                            "title": n.title,
                            "output": out,
                            "receipt_sha256": _hash_payload(out),
                            "data_scope": _infer_node_data_scope(n, out),
                            "elapsed_ms": ms,
                        }
                        executed_nodes.append(n.node_id)
                        emit("dag_node_done", node_id=n.node_id, kind=n.kind, elapsed_ms=ms)

                        if (pause_after_node == n.node_id or n.interrupt_after) and f"after:{n.node_id}" not in approved_nodes:
                            node_trace.append(f"pause_after:{n.node_id}")
                            intr = {
                                "reason": "breakpoint_pause_after_node",
                                "node_id": f"after:{n.node_id}",
                                "node_title": n.title,
                                "missing_inputs": [],
                                "prompt": f"节点「{n.title} ({n.node_id})」已完成并持久化回执，按断点暂停等待人工复核后继续下游节点。",
                                "payload": {"breakpoint": "after", "completed_node": n.node_id},
                            }
                            emit("dag_suspended", node_id=n.node_id, interrupt=intr)
                            return _persist_and_return("suspended", "awaiting_human_approval", suspended_at=f"after:{n.node_id}", interrupt=intr)

                if persist:
                    state["dag_topology"] = graph.describe(completed_nodes=completed_nodes, reused_nodes=reused_nodes)
                    self.store.save(state)

            emit("dag_completed", mode=mode, executed_nodes=executed_nodes, reused_nodes=reused_nodes, invalidated_nodes=invalidated_nodes)
            return _persist_and_return("completed", "all_nodes_completed")
        finally:
            if lock_acquired:
                try:
                    self.store.release_resume_lock(tid, lock_owner)
                except Exception:
                    pass


def infer_execution_mode(question: str) -> str:
    """Automatically choose the optimal DAG template for any free-form coal-mine question."""
    q = question or ""
    if any(k in q for k in ("无功补偿", "多专业", "会诊", "冲突", "综合治理", "证伪", "并同时评估")):
        return "multi_agent"
    if any(k in q for k in ("热稳定裕度核验", "单进线过载核验", "N-1载流核验", "补证核验")):
        return "audit_graph"
    return "diagnose"


def compile_and_run_question_dag(
    archive,
    question: str,
    *,
    mode: str = "auto",
    thread_id: str | None = None,
    checkpoint_db: Path | str | None = None,
    pause_before_node: str | None = None,
    pause_after_node: str | None = None,
    human_input: dict[str, Any] | None = None,
    resume_policy: str = "strict",
    client=None,
    on_event=None,
    profile: MineProfile | None = None,
) -> dict[str, Any]:
    """Unified entry point: compile ANY free-form question into a checkpointed DAG and run/resume it."""
    eff_mode = infer_execution_mode(question) if mode == "auto" else mode
    ckpt_path = Path(checkpoint_db) if checkpoint_db else DAG_CHECKPOINT_DB
    tid = thread_id or f"DAG-{eff_mode[:4].upper()}-{uuid.uuid4().hex[:10].upper()}"

    if eff_mode == "multi_agent":
        from laowadi.multi_agent import run_multi_agent_consultation

        res = run_multi_agent_consultation(
            archive,
            question,
            client=client,
            on_event=on_event,
            thread_id=tid,
            checkpoint_db=ckpt_path,
            pause_before_node=pause_before_node,
            pause_after_node=pause_after_node,
            human_input=human_input,
            resume_policy=resume_policy,
        )
        return {
            "thread_id": tid,
            "mode": "multi_agent",
            "status": (res.get("dag_execution") or {}).get("status", res.get("status")),
            "stop_reason": res.get("stop_reason"),
            "business_status": res.get("business_status"),
            "business_trusted": res.get("business_trusted"),
            "dag_execution": res.get("dag_execution"),
            "result": res,
        }

    if eff_mode == "audit_graph":
        from laowadi.graph import CheckpointStore, LaowadiAuditGraph

        task_type = (
            "underground_n1_capacity_audit"
            if any(k in question for k in ("N-1", "n-1", "下井", "1009", "1010", "转供"))
            else "single_incomer_overload_audit"
        )
        store = CheckpointStore(ckpt_path)
        g = LaowadiAuditGraph(archive, store=store, profile=profile)
        supp_pkt = human_input if (human_input and human_input.get("parameters")) else None
        st = g.run(
            tid,
            {"task_type": task_type, "question": question},
            supplement_packet=supp_pkt,
            pause_before_node=pause_before_node,
            human_input=human_input,
            resume_policy=resume_policy,
            on_event=on_event,
        )
        return {
            "thread_id": tid,
            "mode": "audit_graph",
            "status": (st.get("dag_execution") or {}).get("status", st.get("terminal_status")),
            "stop_reason": st.get("stop_reason"),
            "business_status": st.get("business_status"),
            "business_trusted": st.get("business_trusted"),
            "dag_execution": st.get("dag_execution"),
            "result": st,
        }

    # Default: Primary Diagnosis (`diagnose`) compiled onto UnifiedDAGRuntime
    from laowadi.diagnose import diagnose_question

    res = diagnose_question(
        archive,
        question,
        client=client,
        on_event=on_event,
        checkpoint_db=ckpt_path,
        thread_id=tid,
        pause_before_node=pause_before_node,
        pause_after_node=pause_after_node,
        human_input=human_input,
        resume_policy=resume_policy,
    )
    return {
        "thread_id": tid,
        "mode": "diagnose",
        "status": (res.get("dag_execution") or {}).get("status", res.get("status")),
        "stop_reason": res.get("stop_reason"),
        "business_status": res.get("business_status"),
        "business_trusted": res.get("business_trusted"),
        "dag_execution": res.get("dag_execution"),
        "result": res,
    }


def resume_dag_thread(
    archive,
    thread_id: str,
    *,
    human_input: dict[str, Any] | None = None,
    resume_policy: str = "strict",
    checkpoint_db: Path | str | None = None,
    client=None,
    on_event=None,
    profile: MineProfile | None = None,
) -> dict[str, Any]:
    """Resume ANY previously suspended DAG thread across processes with explicit snapshot & concurrency semantics."""
    ckpt_path = Path(checkpoint_db) if checkpoint_db else DAG_CHECKPOINT_DB
    store = DAGCheckpointStore(ckpt_path)
    saved = store.load(thread_id)
    if saved is None:
        raise ValueError(f"未找到可恢复的 DAG 线程：{thread_id}")
    return compile_and_run_question_dag(
        archive,
        saved["question"],
        mode=saved["mode"],
        thread_id=thread_id,
        checkpoint_db=ckpt_path,
        human_input=human_input or {"approved": True},
        resume_policy=resume_policy,
        client=client,
        on_event=on_event,
        profile=profile,
    )
