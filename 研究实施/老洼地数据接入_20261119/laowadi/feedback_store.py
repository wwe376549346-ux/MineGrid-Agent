"""Operator Feedback Store & Negative-Sample AgentOps Regression Exporter.

Captures duty-engineer ratings, rejections, disputed findings, and corrections into an
isolated local SQLite database (`local/laowadi_feedback.sqlite3`) without mutating golden
read-only archives, feeds live telemetry to `GLOBAL_LLMOPS`, and exports standardized
regression evaluation case drafts (`agentops_negative_eval_v1`).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
import uuid

from laowadi.llmops import GLOBAL_LLMOPS
from laowadi.storage import ROOT


FEEDBACK_DB = ROOT / "local" / "laowadi_feedback.sqlite3"
FEEDBACK_TARGET_TYPES = (
    "diagnosis",
    "autonomous_turn",
    "action_ticket",
    "incident_ticket",
    "deep_research",
)
FEEDBACK_VERDICTS = (
    "helpful",
    "incorrect",
    "incomplete",
    "unsafe_suggestion",
    "rejected_ticket",
    "disputed_finding",
)
NEGATIVE_VERDICTS = frozenset(v for v in FEEDBACK_VERDICTS if v != "helpful")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else FEEDBACK_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operator_feedback (
            id TEXT PRIMARY KEY,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL DEFAULT '',
            verdict TEXT NOT NULL,
            question TEXT NOT NULL DEFAULT '',
            answer_excerpt TEXT NOT NULL DEFAULT '',
            expected_correction TEXT NOT NULL DEFAULT '',
            reason_tags_json TEXT NOT NULL DEFAULT '[]',
            mode TEXT NOT NULL DEFAULT '',
            cabinet TEXT NOT NULL DEFAULT '',
            at_time TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL DEFAULT 'operator',
            latency_ms REAL,
            total_tokens INTEGER,
            verification_status TEXT NOT NULL DEFAULT '',
            exported_to_eval INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fbk_verdict_time ON operator_feedback(verdict, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fbk_target ON operator_feedback(target_type, target_id)"
    )
    conn.commit()
    return conn


def _normalize_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p.strip()[:40] for p in re.split(r"[,，;；\s]+", raw) if p.strip()]
        return list(dict.fromkeys(parts))[:12]
    if isinstance(raw, (list, tuple, set)):
        parts = [str(x).strip()[:40] for x in raw if str(x).strip()]
        return list(dict.fromkeys(parts))[:12]
    return []


def _row_to_feedback(row: sqlite3.Row) -> dict[str, Any]:
    try:
        tags = json.loads(row["reason_tags_json"] or "[]")
        if not isinstance(tags, list):
            tags = []
    except Exception:
        tags = []
    return {
        "id": row["id"],
        "target_type": row["target_type"],
        "target_id": row["target_id"],
        "verdict": row["verdict"],
        "is_negative": row["verdict"] in NEGATIVE_VERDICTS,
        "question": row["question"],
        "answer_excerpt": row["answer_excerpt"],
        "expected_correction": row["expected_correction"],
        "reason_tags": tags,
        "mode": row["mode"],
        "cabinet": row["cabinet"],
        "at_time": row["at_time"],
        "actor": row["actor"],
        "latency_ms": row["latency_ms"],
        "total_tokens": row["total_tokens"],
        "verification_status": row["verification_status"],
        "exported_to_eval": bool(row["exported_to_eval"]),
        "created_at": row["created_at"],
    }


def record_feedback(
    *,
    target_type: str,
    verdict: str,
    target_id: str = "",
    question: str = "",
    answer_excerpt: str = "",
    expected_correction: str = "",
    reason_tags: Any = None,
    mode: str = "",
    cabinet: str = "",
    at_time: str = "",
    actor: str = "operator",
    latency_ms: float | None = None,
    total_tokens: int | None = None,
    verification_status: str = "",
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Persist an operator feedback event and forward counts into `GLOBAL_LLMOPS`."""
    clean_target = str(target_type or "").strip()
    if clean_target not in FEEDBACK_TARGET_TYPES:
        raise ValueError(f"target_type 须为 {', '.join(FEEDBACK_TARGET_TYPES)} 之一。")
    clean_verdict = str(verdict or "").strip()
    if clean_verdict not in FEEDBACK_VERDICTS:
        raise ValueError(f"verdict 须为 {', '.join(FEEDBACK_VERDICTS)} 之一。")

    fbk_id = f"FBK-{uuid.uuid4().hex[:12].upper()}"
    now = _utc_now()
    tags = _normalize_tags(reason_tags)
    lat_val = float(latency_ms) if latency_ms is not None else None
    tok_val = int(total_tokens) if total_tokens is not None else None

    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO operator_feedback (
                id, target_type, target_id, verdict, question, answer_excerpt,
                expected_correction, reason_tags_json, mode, cabinet, at_time,
                actor, latency_ms, total_tokens, verification_status, exported_to_eval, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                fbk_id,
                clean_target,
                str(target_id or "")[:64],
                clean_verdict,
                str(question or "").strip()[:1000],
                str(answer_excerpt or "").strip()[:1200],
                str(expected_correction or "").strip()[:1200],
                json.dumps(tags, ensure_ascii=False),
                str(mode or "")[:32],
                str(cabinet or "")[:32],
                str(at_time or "")[:32],
                str(actor or "operator")[:48],
                lat_val,
                tok_val,
                str(verification_status or "")[:40],
                now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM operator_feedback WHERE id = ?", (fbk_id,)).fetchone()
        out = _row_to_feedback(row)
    finally:
        conn.close()

    GLOBAL_LLMOPS.record_operator_feedback(verdict=clean_verdict, target_type=clean_target)
    return out


def list_feedback(
    *,
    verdict: str | None = None,
    target_type: str | None = None,
    negative_only: bool = False,
    limit: int = 100,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    if verdict is not None and verdict not in FEEDBACK_VERDICTS:
        raise ValueError(f"verdict 须为 {', '.join(FEEDBACK_VERDICTS)} 之一。")
    if target_type is not None and target_type not in FEEDBACK_TARGET_TYPES:
        raise ValueError(f"target_type 须为 {', '.join(FEEDBACK_TARGET_TYPES)} 之一。")

    clauses: list[str] = []
    params: list[Any] = []
    if verdict:
        clauses.append("verdict = ?")
        params.append(verdict)
    elif negative_only:
        placeholders = ", ".join("?" for _ in NEGATIVE_VERDICTS)
        clauses.append(f"verdict IN ({placeholders})")
        params.extend(sorted(NEGATIVE_VERDICTS))
    if target_type:
        clauses.append("target_type = ?")
        params.append(target_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 500)))

    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM operator_feedback {where} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [_row_to_feedback(r) for r in rows]
    finally:
        conn.close()


def feedback_summary(*, db_path: Path | None = None) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT target_type, verdict, reason_tags_json, exported_to_eval FROM operator_feedback"
        ).fetchall()
    finally:
        conn.close()

    total = len(rows)
    helpful = 0
    negative = 0
    unexported_negative = 0
    by_verdict: dict[str, int] = {v: 0 for v in FEEDBACK_VERDICTS}
    by_target_type: dict[str, int] = {t: 0 for t in FEEDBACK_TARGET_TYPES}
    tag_counts: dict[str, int] = {}

    for r in rows:
        v = str(r["verdict"])
        t = str(r["target_type"])
        by_verdict[v] = by_verdict.get(v, 0) + 1
        by_target_type[t] = by_target_type.get(t, 0) + 1
        if v == "helpful":
            helpful += 1
        else:
            negative += 1
            if not r["exported_to_eval"]:
                unexported_negative += 1
        try:
            tags = json.loads(r["reason_tags_json"] or "[]")
            if isinstance(tags, list):
                for tag in tags:
                    tag_s = str(tag).strip()
                    if tag_s:
                        tag_counts[tag_s] = tag_counts.get(tag_s, 0) + 1
        except Exception:
            pass

    helpful_rate = round(helpful / total * 100.0, 2) if total else 0.0
    top_tags = [
        {"tag": k, "count": c}
        for k, c in sorted(tag_counts.items(), key=lambda x: (-x[1], x[0]))[:10]
    ]
    return {
        "total": total,
        "helpful_count": helpful,
        "negative_count": negative,
        "helpful_rate_pct": helpful_rate,
        "by_verdict": by_verdict,
        "by_target_type": by_target_type,
        "top_reason_tags": top_tags,
        "unexported_negative_count": unexported_negative,
    }


def export_negative_eval_cases(
    *,
    mark_exported: bool = False,
    include_exported: bool = True,
    limit: int = 100,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Export negative operator feedback rows as standardized regression case drafts."""
    neg_list = sorted(NEGATIVE_VERDICTS)
    placeholders = ", ".join("?" for _ in neg_list)
    clauses = [f"verdict IN ({placeholders})"]
    params: list[Any] = list(neg_list)
    if not include_exported:
        clauses.append("exported_to_eval = 0")
    params.append(max(1, min(int(limit), 500)))

    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM operator_feedback WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        items = [_row_to_feedback(r) for r in rows]
        if mark_exported and items:
            conn.executemany(
                "UPDATE operator_feedback SET exported_to_eval = 1 WHERE id = ?",
                [(it["id"],) for it in items],
            )
            conn.commit()
    finally:
        conn.close()

    cases = []
    for it in items:
        cases.append({
            "id": f"FBK-CASE-{it['id'][4:]}",
            "feedback_id": it["id"],
            "target_type": it["target_type"],
            "target_id": it["target_id"],
            "verdict": it["verdict"],
            "question": it["question"],
            "bad_answer_excerpt": it["answer_excerpt"],
            "expected_correction": it["expected_correction"],
            "reason_tags": it["reason_tags"],
            "anchors": {
                "cabinet": it["cabinet"] or None,
                "at_time": it["at_time"] or None,
            },
            "mode": it["mode"] or it["target_type"],
            "verification_status": it["verification_status"],
            "created_at": it["created_at"],
            "already_exported": it["exported_to_eval"],
        })

    return {
        "schema": "agentops_negative_eval_v1",
        "exported_at": _utc_now(),
        "count": len(cases),
        "marked_exported": bool(mark_exported),
        "cases": cases,
    }


class FeedbackStore:
    """Object wrapper around operator feedback storage and negative-eval export."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path) if db_path is not None else FEEDBACK_DB

    def record(self, **kwargs) -> dict[str, Any]:
        return record_feedback(db_path=self.db_path, **kwargs)

    def list(self, **kwargs) -> list[dict[str, Any]]:
        return list_feedback(db_path=self.db_path, **kwargs)

    def summary(self) -> dict[str, Any]:
        return feedback_summary(db_path=self.db_path)

    def export_negative_cases(self, **kwargs) -> dict[str, Any]:
        return export_negative_eval_cases(db_path=self.db_path, **kwargs)
