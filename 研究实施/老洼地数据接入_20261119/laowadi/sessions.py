"""Persistent SQLite conversation & working-memory store (keeps archived report/event DBs strictly read-only)."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
import json
from pathlib import Path

from laowadi.db.postgres import connect_runtime as connect, runtime_exists
from laowadi.storage import ROOT


SESSION_DB = ROOT / "local" / "laowadi_sessions.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_session (
    session_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    mine_id TEXT NOT NULL DEFAULT '',
    station TEXT NOT NULL DEFAULT '',
    archive_snapshot_sha256 TEXT NOT NULL DEFAULT '',
    active_anchors_json TEXT NOT NULL DEFAULT '{}',
    turns_digest_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS chat_message (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    question TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'local',
    result_json TEXT,
    error_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_message_session ON chat_message(session_id, seq);
"""


def _ensure_schema(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path, writable=True)) as db:
        db.executescript(SCHEMA)
        cols = {r["name"] for r in db.execute("PRAGMA table_info(chat_session)").fetchall()}
        with db:
            if "mine_id" not in cols:
                db.execute("ALTER TABLE chat_session ADD COLUMN mine_id TEXT NOT NULL DEFAULT ''")
            if "station" not in cols:
                db.execute("ALTER TABLE chat_session ADD COLUMN station TEXT NOT NULL DEFAULT ''")
            if "archive_snapshot_sha256" not in cols:
                db.execute("ALTER TABLE chat_session ADD COLUMN archive_snapshot_sha256 TEXT NOT NULL DEFAULT ''")


def save_chat_turn(
    session_id: str,
    question: str,
    mode: str,
    result: dict | None,
    *,
    error_text: str = "",
    active_anchors: dict | None = None,
    turns_digest: list[dict] | None = None,
    mine_id: str = "",
    station: str = "",
    archive_snapshot_sha256: str = "",
    path: Path = SESSION_DB,
) -> dict:
    sid = str(session_id or "default").strip()[:64] or "default"
    q = str(question or "").strip()
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    title = (q[:32] + "…") if len(q) > 32 else (q or "新建诊断会话")
    anchors_json = json.dumps(active_anchors or {}, ensure_ascii=False)
    digest_json = json.dumps(turns_digest or [], ensure_ascii=False)
    res_json = json.dumps(result, ensure_ascii=False, allow_nan=False) if result is not None else None

    _ensure_schema(Path(path))
    with closing(connect(Path(path), writable=True)) as db:
        with db:
            db.execute(
                """
                INSERT INTO chat_session (
                    session_id, title, created_at, updated_at, mine_id, station,
                    archive_snapshot_sha256, active_anchors_json, turns_digest_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    mine_id = excluded.mine_id,
                    station = excluded.station,
                    archive_snapshot_sha256 = excluded.archive_snapshot_sha256,
                    active_anchors_json = excluded.active_anchors_json,
                    turns_digest_json = excluded.turns_digest_json
                """,
                (sid, title, now, now, str(mine_id or "")[:64], str(station or "")[:32], str(archive_snapshot_sha256 or "")[:64], anchors_json, digest_json),
            )
            cur = db.execute(
                """
                INSERT INTO chat_message (session_id, question, mode, result_json, error_text, created_at)
                VALUES (?, ?, ?, ?, ?, ?) RETURNING seq
                """,
                (sid, q, mode, res_json, error_text or "", now),
            )
            seq = cur.fetchone()[0]
    return {"session_id": sid, "seq": seq, "updated_at": now}


def update_session_memory(
    session_id: str,
    *,
    active_anchors: dict,
    turns_digest: list[dict],
    mine_id: str = "",
    station: str = "",
    archive_snapshot_sha256: str = "",
    path: Path = SESSION_DB,
) -> None:
    sid = str(session_id or "").strip()
    if not sid or not runtime_exists(path):
        return
    _ensure_schema(Path(path))
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    with closing(connect(Path(path), writable=True)) as db:
        with db:
            db.execute(
                """
                UPDATE chat_session
                SET updated_at = ?, mine_id = ?, station = ?, archive_snapshot_sha256 = ?,
                    active_anchors_json = ?, turns_digest_json = ?
                WHERE session_id = ?
                """,
                (
                    now,
                    str(mine_id or "")[:64],
                    str(station or "")[:32],
                    str(archive_snapshot_sha256 or "")[:64],
                    json.dumps(active_anchors, ensure_ascii=False),
                    json.dumps(turns_digest, ensure_ascii=False),
                    sid,
                ),
            )


def list_sessions(limit: int = 30, *, path: Path = SESSION_DB) -> list[dict]:
    if not path or not runtime_exists(path):
        return []
    _ensure_schema(Path(path))
    with closing(connect(Path(path))) as db:
        rows = db.execute(
            """
            SELECT s.session_id, s.title, s.created_at, s.updated_at, s.mine_id, s.station, s.archive_snapshot_sha256,
                   (SELECT COUNT(*) FROM chat_message m WHERE m.session_id = s.session_id) AS turn_count
            FROM chat_session s
            ORDER BY s.updated_at DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    return [dict(r) for r in rows]


def load_session(session_id: str, *, path: Path = SESSION_DB) -> dict | None:
    sid = str(session_id or "").strip()
    if not sid or not path or not runtime_exists(path):
        return None
    _ensure_schema(Path(path))
    with closing(connect(Path(path))) as db:
        srow = db.execute(
            """
            SELECT session_id, title, created_at, updated_at, mine_id, station,
                   archive_snapshot_sha256, active_anchors_json, turns_digest_json
            FROM chat_session WHERE session_id = ?
            """,
            (sid,),
        ).fetchone()
        if srow is None:
            return None
        mrows = db.execute(
            "SELECT seq, question, mode, result_json, error_text, created_at FROM chat_message WHERE session_id = ? ORDER BY seq ASC",
            (sid,),
        ).fetchall()
    turns = []
    for m in mrows:
        turns.append({
            "id": m["seq"],
            "question": m["question"],
            "mode": m["mode"],
            "result": json.loads(m["result_json"]) if m["result_json"] else None,
            "error": m["error_text"] or None,
            "created_at": m["created_at"],
        })
    return {
        "session_id": srow["session_id"],
        "title": srow["title"],
        "created_at": srow["created_at"],
        "updated_at": srow["updated_at"],
        "mine_id": srow["mine_id"],
        "station": srow["station"],
        "archive_snapshot_sha256": srow["archive_snapshot_sha256"],
        "active_anchors": json.loads(srow["active_anchors_json"] or "{}"),
        "turns_digest": json.loads(srow["turns_digest_json"] or "[]"),
        "turns": turns,
    }


def delete_session(session_id: str, *, path: Path = SESSION_DB) -> bool:
    sid = str(session_id or "").strip()
    if not sid or not path or not runtime_exists(path):
        return False
    with closing(connect(Path(path), writable=True)) as db:
        with db:
            db.execute("DELETE FROM chat_message WHERE session_id = ?", (sid,))
            cur = db.execute("DELETE FROM chat_session WHERE session_id = ?", (sid,))
            return cur.rowcount > 0
