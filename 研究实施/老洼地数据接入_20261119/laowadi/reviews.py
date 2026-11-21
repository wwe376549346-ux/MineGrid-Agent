"""Separate SQLite ledger for operator confirmations and field feedback (keeps source DBs read-only)."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3

from laowadi.db.postgres import connect_runtime as connect, runtime_exists
from laowadi.storage import ROOT


REVIEW_DB = ROOT / "local" / "laowadi_reviews.sqlite3"
DISPOSITIONS = {
    "pending": "待复核",
    "confirmed_anomaly": "确认为异常",
    "normal_operation": "正常生产/计划倒闸",
    "needs_site_check": "待现场核查",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS operator_review (
    finding_id TEXT NOT NULL,
    scope_day TEXT NOT NULL DEFAULT 'ALL',
    disposition TEXT NOT NULL,
    switch_state TEXT NOT NULL DEFAULT '',
    operator_note TEXT NOT NULL DEFAULT '',
    operator_name TEXT NOT NULL DEFAULT '值班员',
    mine_id TEXT NOT NULL DEFAULT '',
    station TEXT NOT NULL DEFAULT '',
    archive_version TEXT NOT NULL DEFAULT '',
    window_label TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (finding_id, scope_day)
);
CREATE TABLE IF NOT EXISTS operator_review_history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id TEXT NOT NULL,
    scope_day TEXT NOT NULL DEFAULT 'ALL',
    disposition TEXT NOT NULL,
    switch_state TEXT NOT NULL DEFAULT '',
    operator_note TEXT NOT NULL DEFAULT '',
    operator_name TEXT NOT NULL DEFAULT '值班员',
    mine_id TEXT NOT NULL DEFAULT '',
    station TEXT NOT NULL DEFAULT '',
    archive_version TEXT NOT NULL DEFAULT '',
    window_label TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL
);
"""


def _norm_scope_day(scope_day: str | None) -> str:
    s = str(scope_day or "").strip()
    if not s or s.upper() == "ALL":
        return "ALL"
    return s[:32]


def _ensure_schema(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path, writable=True)) as db:
        cols = [r["name"] for r in db.execute("PRAGMA table_info(operator_review)").fetchall()]
        if cols and "scope_day" not in cols:
            # Seamless in-place migration from legacy single-PK operator_review -> composite PK (finding_id, scope_day)
            with db:
                db.execute("ALTER TABLE operator_review RENAME TO operator_review_legacy")
                db.executescript(SCHEMA)
                db.execute(
                    """
                    INSERT OR IGNORE INTO operator_review(
                        finding_id, scope_day, disposition, switch_state, operator_note,
                        operator_name, mine_id, station, archive_version, window_label, updated_at
                    )
                    SELECT finding_id, 'ALL', disposition, switch_state, operator_note,
                           operator_name, '', '', '', '', updated_at
                    FROM operator_review_legacy
                    """
                )
                db.execute("DROP TABLE operator_review_legacy")
        else:
            db.executescript(SCHEMA)
        rcols = {r["name"] for r in db.execute("PRAGMA table_info(operator_review)").fetchall()}
        with db:
            if "mine_id" not in rcols:
                db.execute("ALTER TABLE operator_review ADD COLUMN mine_id TEXT NOT NULL DEFAULT ''")
            if "station" not in rcols:
                db.execute("ALTER TABLE operator_review ADD COLUMN station TEXT NOT NULL DEFAULT ''")
        hcols = {r["name"] for r in db.execute("PRAGMA table_info(operator_review_history)").fetchall()}
        with db:
            if hcols and "scope_day" not in hcols:
                db.execute("ALTER TABLE operator_review_history ADD COLUMN scope_day TEXT NOT NULL DEFAULT 'ALL'")
                db.execute("ALTER TABLE operator_review_history ADD COLUMN archive_version TEXT NOT NULL DEFAULT ''")
                db.execute("ALTER TABLE operator_review_history ADD COLUMN window_label TEXT NOT NULL DEFAULT ''")
            if hcols and "mine_id" not in hcols:
                db.execute("ALTER TABLE operator_review_history ADD COLUMN mine_id TEXT NOT NULL DEFAULT ''")
            if hcols and "station" not in hcols:
                db.execute("ALTER TABLE operator_review_history ADD COLUMN station TEXT NOT NULL DEFAULT ''")


def list_reviews(
    path: Path = REVIEW_DB,
    *,
    scope_day: str | None = None,
    mine_id: str | None = None,
    station: str | None = None,
    archive_version: str | None = None,
) -> dict[str, dict]:
    if not path or not runtime_exists(path):
        return {}
    _ensure_schema(Path(path))
    clauses: list[str] = []
    params: list[str] = []
    if mine_id:
        clauses.append("(mine_id = '' OR mine_id = ?)")
        params.append(str(mine_id).strip())
    if station:
        clauses.append("(station = '' OR station = ?)")
        params.append(str(station).strip())
    if archive_version:
        clauses.append("(archive_version = '' OR archive_version = ?)")
        params.append(str(archive_version).strip())
    extra_where = (" AND " + " AND ".join(clauses)) if clauses else ""
    with closing(connect(path)) as db:
        if scope_day is not None:
            sd = _norm_scope_day(scope_day)
            rows = db.execute(
                f"""
                SELECT finding_id, scope_day, disposition, switch_state, operator_note,
                       operator_name, mine_id, station, archive_version, window_label, updated_at
                FROM operator_review
                WHERE (scope_day = ? OR scope_day = 'ALL'){extra_where}
                ORDER BY CASE WHEN scope_day = ? THEN 0 ELSE 1 END, updated_at DESC
                """,
                tuple([sd, *params, sd]),
            ).fetchall()
            out: dict[str, dict] = {}
            for row in rows:
                fid = row["finding_id"]
                if fid in out:
                    continue  # exact scope_day takes precedence over 'ALL'
                out[fid] = {
                    "finding_id": fid,
                    "scope_day": row["scope_day"],
                    "disposition": row["disposition"],
                    "disposition_label": DISPOSITIONS.get(row["disposition"], row["disposition"]),
                    "switch_state": row["switch_state"],
                    "operator_note": row["operator_note"],
                    "operator_name": row["operator_name"],
                    "mine_id": row["mine_id"],
                    "station": row["station"],
                    "archive_version": row["archive_version"],
                    "window_label": row["window_label"],
                    "updated_at": row["updated_at"],
                }
            return out

        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = db.execute(
            f"""
            SELECT finding_id, scope_day, disposition, switch_state, operator_note,
                   operator_name, mine_id, station, archive_version, window_label, updated_at
            FROM operator_review
            {where_sql}
            ORDER BY updated_at DESC
            """,
            tuple(params),
        ).fetchall()
    out = {}
    for row in rows:
        fid = row["finding_id"]
        sd = row["scope_day"]
        key = fid if (sd == "ALL" and fid not in out) else (f"{fid}@{sd}" if sd != "ALL" else fid)
        out[key] = {
            "finding_id": fid,
            "scope_day": sd,
            "disposition": row["disposition"],
            "disposition_label": DISPOSITIONS.get(row["disposition"], row["disposition"]),
            "switch_state": row["switch_state"],
            "operator_note": row["operator_note"],
            "operator_name": row["operator_name"],
            "mine_id": row["mine_id"],
            "station": row["station"],
            "archive_version": row["archive_version"],
            "window_label": row["window_label"],
            "updated_at": row["updated_at"],
        }
    return out


def save_review(
    finding_id: str,
    disposition: str,
    *,
    switch_state: str = "",
    operator_note: str = "",
    operator_name: str = "值班员",
    scope_day: str = "ALL",
    mine_id: str = "",
    station: str = "",
    archive_version: str = "",
    window_label: str = "",
    path: Path = REVIEW_DB,
) -> dict:
    fid = str(finding_id or "").strip()
    if not fid or len(fid) > 80:
        raise ValueError("请提供有效的问题或事件编号。")
    if disposition not in DISPOSITIONS:
        raise ValueError("复核状态无效，须为 pending、confirmed_anomaly、normal_operation 或 needs_site_check。")
    sd = _norm_scope_day(scope_day)
    sw = str(switch_state or "").strip()[:200]
    note = str(operator_note or "").strip()[:1000]
    op = (str(operator_name or "").strip() or "值班员")[:40]
    mid = str(mine_id or "").strip()[:64]
    st = str(station or "").strip()[:32]
    av = str(archive_version or "").strip()[:64]
    wl = str(window_label or "").strip()[:80]
    now = datetime.now().isoformat(sep=" ", timespec="seconds")

    _ensure_schema(Path(path))
    with closing(connect(path, writable=True)) as db:
        with db:
            db.execute(
                """
                INSERT INTO operator_review(
                    finding_id, scope_day, disposition, switch_state, operator_note,
                    operator_name, mine_id, station, archive_version, window_label, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(finding_id, scope_day) DO UPDATE SET
                    disposition = excluded.disposition,
                    switch_state = excluded.switch_state,
                    operator_note = excluded.operator_note,
                    operator_name = excluded.operator_name,
                    mine_id = excluded.mine_id,
                    station = excluded.station,
                    archive_version = excluded.archive_version,
                    window_label = excluded.window_label,
                    updated_at = excluded.updated_at
                """,
                (fid, sd, disposition, sw, note, op, mid, st, av, wl, now),
            )
            db.execute(
                """
                INSERT INTO operator_review_history(
                    finding_id, scope_day, disposition, switch_state, operator_note,
                    operator_name, mine_id, station, archive_version, window_label, recorded_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (fid, sd, disposition, sw, note, op, mid, st, av, wl, now),
            )
    return {
        "finding_id": fid,
        "scope_day": sd,
        "disposition": disposition,
        "disposition_label": DISPOSITIONS[disposition],
        "switch_state": sw,
        "operator_note": note,
        "operator_name": op,
        "mine_id": mid,
        "station": st,
        "archive_version": av,
        "window_label": wl,
        "updated_at": now,
    }


def delete_review(finding_id: str, *, scope_day: str | None = None, path: Path = REVIEW_DB) -> bool:
    """Remove an operator confirmation / episodic memory record by finding_id (and optional scope_day)."""
    fid = str(finding_id or "").strip()
    if not fid:
        raise ValueError("请提供待删除的记忆编号。")
    if "@" in fid and scope_day is None:
        fid, scope_day = fid.rsplit("@", 1)
    if not path or not runtime_exists(path):
        return False
    _ensure_schema(Path(path))
    with closing(connect(Path(path), writable=True)) as db:
        with db:
            if scope_day is not None:
                cur = db.execute(
                    "DELETE FROM operator_review WHERE finding_id = ? AND scope_day = ?",
                    (fid, _norm_scope_day(scope_day)),
                )
            else:
                cur = db.execute("DELETE FROM operator_review WHERE finding_id = ?", (fid,))
            return cur.rowcount > 0


def format_reviews_memory(reviews: dict[str, dict]) -> str:
    """Format non-pending operator confirmations as concise context for the diagnostic agent."""
    confirmed = [r for r in reviews.values() if r["disposition"] != "pending"]
    if not confirmed:
        return ""
    lines = ["以下为值班人员已登记的现场人工复核记忆（可作重要参考，不得与原始报表冲突）："]
    for item in confirmed[:12]:
        parts = [f'- [{item["finding_id"]}] 判定：{item["disposition_label"]}']
        if item["switch_state"]:
            parts.append(f'开关/运行状态：{item["switch_state"]}')
        if item["operator_note"]:
            parts.append(f'备注：{item["operator_note"]}')
        parts.append(f'（{item["operator_name"]}，{item["updated_at"]}）')
        lines.append("；".join(parts))
    return "\n".join(lines)
