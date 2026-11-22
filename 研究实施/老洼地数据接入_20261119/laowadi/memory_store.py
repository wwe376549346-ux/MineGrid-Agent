"""Three-Layer Persistent Operational Memory Store for MineGrid-Agent.

Stores operator-confirmed mine knowledge across sessions in an isolated local SQLite
database (`local/laowadi_memory.sqlite3`), leaving the read-only golden archives untouched:
  1. semantic   — equipment quirks, confirmed field facts, custom internal thresholds
  2. episodic   — resolved fault investigation lessons & case memories
  3. procedural — reusable shift SOPs & electrical inspection playbooks
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
import uuid

from laowadi.storage import ROOT


MEMORY_DB = ROOT / "local" / "laowadi_memory.sqlite3"
MEMORY_CATEGORIES = ("semantic", "episodic", "procedural")

_VOLTAGE_THRESHOLD_RE = re.compile(
    r"(?:10\s*kV|II\s*段|I\s*段|母线|电压|过压|上限|红线|考核|阈值|预警|越限|Uab)[^\d\n]{0,16}(\d{1,2}\.\d{1,3})\s*kV"
    r"|(\d{1,2}\.\d{1,3})\s*kV[^\n]{0,20}(?:红线|考核|预警|上限|阈值)",
    re.I,
)


def _extract_voltage_kv(text: str) -> float | None:
    """Parse a plausible distribution-bus/feeder kV threshold (8.5–40.0 kV) from prefix or suffix phrasing."""
    m = _VOLTAGE_THRESHOLD_RE.search(text or "")
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    if not raw:
        return None
    val = float(raw)
    if 8.5 <= val <= 40.0:
        return val
    return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc_iso(ts: Any) -> datetime | None:
    raw = str(ts or "").strip()
    if not raw:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            dt = datetime.strptime(raw, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
            return dt
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _normalize_expires_at(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    dt = _parse_utc_iso(text)
    if dt is None:
        raise ValueError("expires_at 须为有效 ISO 日期或时间（如 2026-12-31 或 2026-12-31T23:59:59Z）。")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_memory_expired(expires_at: Any, *, now: datetime | None = None) -> bool:
    exp_dt = _parse_utc_iso(expires_at)
    if exp_dt is None:
        return False
    ref_now = now if now is not None else datetime.now(timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)
    else:
        ref_now = ref_now.astimezone(timezone.utc)
    return exp_dt <= ref_now


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else MEMORY_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS operational_memory (
            id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            scope_cabinet TEXT NOT NULL DEFAULT '',
            metric_threshold TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '[]',
            enabled INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'operator',
            hit_count INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(operational_memory)").fetchall()}
    if "expires_at" not in cols:
        conn.execute("ALTER TABLE operational_memory ADD COLUMN expires_at TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mem_cat_enabled ON operational_memory(category, enabled)"
    )
    conn.commit()
    return conn


def _normalize_threshold(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, (dict, list)):
        return json.dumps(raw, ensure_ascii=False)
    return str(raw).strip()[:240]


def _normalize_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in re.split(r"[,，;；\s]+", raw) if p.strip()]
        return list(dict.fromkeys(parts))[:12]
    if isinstance(raw, (list, tuple, set)):
        out = [str(x).strip()[:40] for x in raw if str(x).strip()]
        return list(dict.fromkeys(out))[:12]
    return []


def _row_to_dict(row: sqlite3.Row, *, now: datetime | None = None) -> dict[str, Any]:
    raw_tags = row["tags"] or "[]"
    try:
        tags = json.loads(raw_tags)
        if not isinstance(tags, list):
            tags = []
    except Exception:
        tags = []
    raw_thr = row["metric_threshold"] or ""
    parsed_thr: Any = raw_thr
    if raw_thr.startswith("{") or raw_thr.startswith("["):
        try:
            parsed_thr = json.loads(raw_thr)
        except Exception:
            parsed_thr = raw_thr
    raw_exp = row["expires_at"] if "expires_at" in row.keys() else ""
    clean_exp = str(raw_exp or "").strip()
    return {
        "id": row["id"],
        "category": row["category"],
        "title": row["title"],
        "content": row["content"],
        "scope_cabinet": row["scope_cabinet"] or None,
        "metric_threshold": parsed_thr or None,
        "tags": tags,
        "enabled": bool(row["enabled"]),
        "source": row["source"],
        "hit_count": int(row["hit_count"]),
        "expires_at": clean_exp or None,
        "is_expired": is_memory_expired(clean_exp, now=now),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


_METRIC_NAME_MAP = {
    "uab": "Uab",
    "voltage": "Uab",
    "电压": "Uab",
    "ia": "Ia",
    "current": "Ia",
    "电流": "Ia",
    "p": "P",
    "power": "P",
    "有功": "P",
    "cos": "cos",
    "pf": "cos",
    "功率因数": "cos",
}


def _extract_rule_signature(
    category: str,
    title: str,
    content: str,
    scope_cabinet: str | None,
    metric_threshold: Any,
) -> dict[str, Any] | None:
    if str(category or "").strip() != "semantic":
        return None
    clean_cab = str(scope_cabinet or "").strip()
    parsed_thr = metric_threshold
    if isinstance(parsed_thr, str) and (parsed_thr.strip().startswith("{") or parsed_thr.strip().startswith("[")):
        try:
            parsed_thr = json.loads(parsed_thr)
        except Exception:
            pass
    if isinstance(parsed_thr, dict):
        raw_m = str(parsed_thr.get("metric") or "").strip().lower()
        norm_m = _METRIC_NAME_MAP.get(raw_m)
        if norm_m and parsed_thr.get("value") is not None:
            try:
                val = float(parsed_thr["value"])
                op = str(parsed_thr.get("operator") or ">").strip() or ">"
                return {"metric": norm_m, "operator": op, "value": val, "scope_cabinet": clean_cab}
            except (TypeError, ValueError):
                pass
    blob = f"{title or ''} {content or ''} {metric_threshold or ''}"
    kv = _extract_voltage_kv(blob)
    if kv is not None:
        return {"metric": "Uab", "operator": ">", "value": float(kv), "scope_cabinet": clean_cab}
    return None


def _find_conflicts_against_rows(
    rows: list[dict[str, Any]],
    *,
    category: str,
    title: str,
    content: str,
    scope_cabinet: str | None = None,
    metric_threshold: Any = None,
    exclude_id: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    sig = _extract_rule_signature(category, title, content, scope_cabinet, metric_threshold)
    if sig is None:
        return []
    conflicts: list[dict[str, Any]] = []
    for mem in rows:
        if exclude_id and mem.get("id") == exclude_id:
            continue
        if not mem.get("enabled", True) or is_memory_expired(mem.get("expires_at"), now=now):
            continue
        esig = _extract_rule_signature(
            mem.get("category", ""),
            mem.get("title", ""),
            mem.get("content", ""),
            mem.get("scope_cabinet"),
            mem.get("metric_threshold"),
        )
        if esig is None or esig["metric"] != sig["metric"] or esig["operator"] != sig["operator"]:
            continue
        if abs(esig["value"] - sig["value"]) < 1e-6:
            continue
        if esig["scope_cabinet"] == sig["scope_cabinet"]:
            ctype = "direct_conflict"
            scope_desc = f"{esig['scope_cabinet']} 柜" if esig["scope_cabinet"] else "全站通用"
            reason = (
                f"与同范围（{scope_desc}）生效规则 {mem['id']} 冲突："
                f"{esig['metric']} {esig['operator']} {esig['value']} vs {sig['value']}。"
            )
        elif esig["scope_cabinet"] == "" or sig["scope_cabinet"] == "":
            ctype = "scope_overlap_conflict"
            other_scope = "全站通用" if esig["scope_cabinet"] == "" else f"{esig['scope_cabinet']} 柜"
            reason = (
                f"与{other_scope}生效规则 {mem['id']} 范围重叠且阈值不一致："
                f"{esig['metric']} {esig['operator']} {esig['value']} vs {sig['value']}。"
            )
        else:
            continue
        conflicts.append({
            "memory_id": mem["id"],
            "title": mem["title"],
            "scope_cabinet": mem.get("scope_cabinet"),
            "metric": sig["metric"],
            "operator": sig["operator"],
            "existing_value": esig["value"],
            "incoming_value": sig["value"],
            "conflict_type": ctype,
            "reason": reason,
        })
    return conflicts


def list_memories(
    *,
    category: str | None = None,
    enabled_only: bool = False,
    cabinet: str | None = None,
    include_expired: bool | None = None,
    now: datetime | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    if category is not None and category not in MEMORY_CATEGORIES:
        raise ValueError(f"category 须为 {', '.join(MEMORY_CATEGORIES)} 之一。")
    eff_include_expired = (not enabled_only) if include_expired is None else bool(include_expired)
    clauses: list[str] = []
    params: list[Any] = []
    if category:
        clauses.append("category = ?")
        params.append(category)
    if enabled_only:
        clauses.append("enabled = 1")
    if cabinet:
        clauses.append("(scope_cabinet = ? OR scope_cabinet = '')")
        params.append(str(cabinet).strip())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM operational_memory {where} ORDER BY enabled DESC, updated_at DESC",
            tuple(params),
        ).fetchall()
        items = [_row_to_dict(r, now=now) for r in rows]
        if not eff_include_expired:
            items = [m for m in items if not m["is_expired"]]
        return items
    finally:
        conn.close()


def detect_memory_conflicts(
    *,
    category: str,
    title: str,
    content: str,
    scope_cabinet: str | None = None,
    metric_threshold: Any = None,
    exclude_id: str | None = None,
    now: datetime | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Detect conflicts against currently enabled, non-expired semantic memories."""
    if category != "semantic":
        return []
    active = list_memories(
        category="semantic",
        enabled_only=True,
        include_expired=False,
        now=now,
        db_path=db_path,
    )
    return _find_conflicts_against_rows(
        active,
        category=category,
        title=title,
        content=content,
        scope_cabinet=scope_cabinet,
        metric_threshold=metric_threshold,
        exclude_id=exclude_id,
        now=now,
    )


def prune_expired_memories(
    *,
    disable_only: bool = True,
    now: datetime | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Disable (or delete) enabled operational memories whose `expires_at` has elapsed."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, expires_at FROM operational_memory WHERE enabled = 1 AND expires_at <> ''"
        ).fetchall()
        expired_ids = [r["id"] for r in rows if is_memory_expired(r["expires_at"], now=now)]
        if expired_ids:
            if disable_only:
                ts = _utc_now()
                conn.executemany(
                    "UPDATE operational_memory SET enabled = 0, updated_at = ? WHERE id = ?",
                    [(ts, mid) for mid in expired_ids],
                )
            else:
                conn.executemany(
                    "DELETE FROM operational_memory WHERE id = ?",
                    [(mid,) for mid in expired_ids],
                )
            conn.commit()
        return {
            "count": len(expired_ids),
            "memory_ids": expired_ids,
            "mode": "disabled" if disable_only else "deleted",
        }
    finally:
        conn.close()


def get_memory(memory_id: str, *, db_path: Path | None = None) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM operational_memory WHERE id = ?",
            (str(memory_id).strip(),),
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def create_memory(
    *,
    category: str,
    title: str,
    content: str,
    scope_cabinet: str | None = None,
    metric_threshold: Any = None,
    tags: Any = None,
    enabled: bool = True,
    source: str = "operator",
    expires_at: Any = None,
    supersede_conflicts: bool = False,
    governance_report: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    if category not in MEMORY_CATEGORIES:
        raise ValueError(f"category 须为 {', '.join(MEMORY_CATEGORIES)} 之一。")
    clean_title = str(title or "").strip()[:140]
    clean_content = str(content or "").strip()[:2000]
    if not clean_title or not clean_content:
        raise ValueError("记忆标题与内容均不能为空。")
    clean_cab = str(scope_cabinet or "").strip()[:24]
    clean_thr = _normalize_threshold(metric_threshold)
    clean_tags = _normalize_tags(tags)
    clean_exp = _normalize_expires_at(expires_at)
    mem_id = f"MEM-{uuid.uuid4().hex[:12].upper()}"
    now = _utc_now()

    conn = _connect(db_path)
    try:
        conflicts: list[dict[str, Any]] = []
        superseded_ids: list[str] = []
        if enabled and not is_memory_expired(clean_exp) and category == "semantic":
            existing_rows = [
                _row_to_dict(r)
                for r in conn.execute(
                    "SELECT * FROM operational_memory WHERE category = 'semantic' AND enabled = 1"
                ).fetchall()
            ]
            conflicts = _find_conflicts_against_rows(
                existing_rows,
                category=category,
                title=clean_title,
                content=clean_content,
                scope_cabinet=clean_cab,
                metric_threshold=clean_thr,
            )
            if supersede_conflicts and conflicts:
                direct_ids = [c["memory_id"] for c in conflicts if c["conflict_type"] == "direct_conflict"]
                if direct_ids:
                    conn.executemany(
                        "UPDATE operational_memory SET enabled = 0, updated_at = ? WHERE id = ?",
                        [(now, mid) for mid in direct_ids],
                    )
                    superseded_ids = direct_ids
                    sup_set = set(direct_ids)
                    conflicts = [c for c in conflicts if c["memory_id"] not in sup_set]

        conn.execute(
            """
            INSERT INTO operational_memory
              (id, category, title, content, scope_cabinet, metric_threshold, tags, enabled, source, hit_count, expires_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                mem_id,
                category,
                clean_title,
                clean_content,
                clean_cab,
                clean_thr,
                json.dumps(clean_tags, ensure_ascii=False),
                1 if enabled else 0,
                str(source or "operator")[:32],
                clean_exp,
                now,
                now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM operational_memory WHERE id = ?", (mem_id,)).fetchone()
        out = _row_to_dict(row)
        if governance_report is not None:
            governance_report["conflicts"] = conflicts
            governance_report["superseded_memory_ids"] = superseded_ids
        return out
    finally:
        conn.close()


def create_memory_with_report(**kwargs) -> dict[str, Any]:
    """Create a memory and return `{memory, conflicts, superseded_memory_ids}`."""
    report: dict[str, Any] = {"conflicts": [], "superseded_memory_ids": []}
    mem = create_memory(governance_report=report, **kwargs)
    return {
        "memory": mem,
        "conflicts": report["conflicts"],
        "superseded_memory_ids": report["superseded_memory_ids"],
    }


def update_memory(
    memory_id: str,
    *,
    category: str | None = None,
    title: str | None = None,
    content: str | None = None,
    scope_cabinet: str | None = ...,  # type: ignore[assignment]
    metric_threshold: Any = ...,
    tags: Any = ...,
    enabled: bool | None = None,
    expires_at: Any = ...,
    supersede_conflicts: bool = False,
    governance_report: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        existing = conn.execute(
            "SELECT * FROM operational_memory WHERE id = ?",
            (str(memory_id).strip(),),
        ).fetchone()
        if not existing:
            return None

        now = _utc_now()
        updates: dict[str, Any] = {"updated_at": now}
        if category is not None:
            if category not in MEMORY_CATEGORIES:
                raise ValueError(f"category 须为 {', '.join(MEMORY_CATEGORIES)} 之一。")
            updates["category"] = category
        if title is not None:
            t = str(title).strip()[:140]
            if not t:
                raise ValueError("记忆标题不能为空。")
            updates["title"] = t
        if content is not None:
            c = str(content).strip()[:2000]
            if not c:
                raise ValueError("记忆内容不能为空。")
            updates["content"] = c
        if scope_cabinet is not ...:
            updates["scope_cabinet"] = str(scope_cabinet or "").strip()[:24]
        if metric_threshold is not ...:
            updates["metric_threshold"] = _normalize_threshold(metric_threshold)
        if tags is not ...:
            updates["tags"] = json.dumps(_normalize_tags(tags), ensure_ascii=False)
        if enabled is not None:
            updates["enabled"] = 1 if enabled else 0
        if expires_at is not ...:
            updates["expires_at"] = _normalize_expires_at(expires_at)

        eff_cat = updates.get("category", existing["category"])
        eff_title = updates.get("title", existing["title"])
        eff_content = updates.get("content", existing["content"])
        eff_cab = updates.get("scope_cabinet", existing["scope_cabinet"])
        eff_thr = updates.get("metric_threshold", existing["metric_threshold"])
        eff_enabled = bool(updates.get("enabled", existing["enabled"]))
        eff_exp = updates.get("expires_at", existing["expires_at"] if "expires_at" in existing.keys() else "")

        conflicts: list[dict[str, Any]] = []
        superseded_ids: list[str] = []
        if eff_enabled and not is_memory_expired(eff_exp) and eff_cat == "semantic":
            existing_rows = [
                _row_to_dict(r)
                for r in conn.execute(
                    "SELECT * FROM operational_memory WHERE category = 'semantic' AND enabled = 1"
                ).fetchall()
            ]
            conflicts = _find_conflicts_against_rows(
                existing_rows,
                category=eff_cat,
                title=eff_title,
                content=eff_content,
                scope_cabinet=eff_cab,
                metric_threshold=eff_thr,
                exclude_id=existing["id"],
            )
            if supersede_conflicts and conflicts:
                direct_ids = [c["memory_id"] for c in conflicts if c["conflict_type"] == "direct_conflict"]
                if direct_ids:
                    conn.executemany(
                        "UPDATE operational_memory SET enabled = 0, updated_at = ? WHERE id = ?",
                        [(now, mid) for mid in direct_ids],
                    )
                    superseded_ids = direct_ids
                    sup_set = set(direct_ids)
                    conflicts = [c for c in conflicts if c["memory_id"] not in sup_set]

        set_sql = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE operational_memory SET {set_sql} WHERE id = ?",
            (*updates.values(), existing["id"]),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM operational_memory WHERE id = ?", (existing["id"],)).fetchone()
        out = _row_to_dict(row)
        if governance_report is not None:
            governance_report["conflicts"] = conflicts
            governance_report["superseded_memory_ids"] = superseded_ids
        return out
    finally:
        conn.close()


def update_memory_with_report(memory_id: str, **kwargs) -> dict[str, Any] | None:
    """Update a memory and return `{memory, conflicts, superseded_memory_ids}` or `None`."""
    report: dict[str, Any] = {"conflicts": [], "superseded_memory_ids": []}
    mem = update_memory(memory_id, governance_report=report, **kwargs)
    if mem is None:
        return None
    return {
        "memory": mem,
        "conflicts": report["conflicts"],
        "superseded_memory_ids": report["superseded_memory_ids"],
    }


def delete_memory(memory_id: str, *, db_path: Path | None = None) -> bool:
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM operational_memory WHERE id = ?",
            (str(memory_id).strip(),),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def retrieve_relevant_memories(
    question: str,
    *,
    cabinets: list[str] | None = None,
    limit: int = 6,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Rank enabled operational memories by cabinet relevance, threshold match, and token overlap."""
    all_enabled = list_memories(enabled_only=True, db_path=db_path)
    if not all_enabled:
        return []

    q_lower = str(question or "").lower()
    cab_set = {str(c) for c in (cabinets or [])}
    # Extract compact Chinese bigrams & ASCII tokens for zero-dependency semantic matching
    tokens = set(re.findall(r"[a-z0-9_.-]{2,}", q_lower))
    zh_chars = re.findall(r"[\u4e00-\u9fff]+", q_lower)
    for seg in zh_chars:
        for i in range(len(seg) - 1):
            tokens.add(seg[i : i + 2])

    scored: list[tuple[float, dict[str, Any]]] = []
    for mem in all_enabled:
        score = 0.0
        scab = str(mem.get("scope_cabinet") or "")
        if scab and (scab in cab_set or scab in q_lower):
            score += 10.0
        if mem.get("metric_threshold"):
            score += 4.0
        if mem["category"] == "procedural":
            score += 1.5
        haystack = " ".join([
            mem["title"].lower(),
            mem["content"].lower(),
            " ".join(t.lower() for t in mem.get("tags") or []),
        ])
        hits = sum(1 for tok in tokens if tok in haystack)
        score += min(hits * 0.8, 8.0)
        if not scab and mem["category"] == "semantic":
            score += 1.0
        if score > 0.5:
            scored.append((score, mem))

    scored.sort(key=lambda x: (-x[0], x[1]["updated_at"]))
    selected = [m for _, m in scored[: max(1, int(limit))]]

    if selected:
        conn = _connect(db_path)
        try:
            conn.executemany(
                "UPDATE operational_memory SET hit_count = hit_count + 1 WHERE id = ?",
                [(m["id"],) for m in selected],
            )
            conn.commit()
        finally:
            conn.close()
        for m in selected:
            m["hit_count"] = int(m.get("hit_count", 0)) + 1

    return selected


def extract_custom_voltage_thresholds(
    memories: list[dict[str, Any]],
    *,
    cabinet: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Extract active operator-defined 10 kV / 35 kV voltage thresholds from semantic memories only."""
    target_cab = str(cabinet).strip() if cabinet is not None else ""
    found: list[dict[str, Any]] = []
    for mem in memories:
        if not mem.get("enabled", True) or mem.get("category") != "semantic":
            continue
        if is_memory_expired(mem.get("expires_at"), now=now):
            continue
        scab = str(mem.get("scope_cabinet") or "").strip()
        if target_cab and scab and scab != target_cab:
            continue
        thr = mem.get("metric_threshold")
        if isinstance(thr, dict) and str(thr.get("metric", "")).lower() in {"uab", "voltage", "电压"}:
            try:
                val = float(thr["value"])
                found.append({
                    "memory_id": mem["id"],
                    "title": mem["title"],
                    "scope_cabinet": mem.get("scope_cabinet"),
                    "operator": str(thr.get("operator") or ">"),
                    "value_kv": val,
                })
                continue
            except (KeyError, TypeError, ValueError):
                pass
        blob = f"{mem.get('title', '')} {mem.get('content', '')} {thr or ''}"
        val = _extract_voltage_kv(blob)
        if val is not None:
            found.append({
                "memory_id": mem["id"],
                "title": mem["title"],
                "scope_cabinet": mem.get("scope_cabinet"),
                "operator": ">",
                "value_kv": val,
            })
    return found


_MEMORY_INTENT_RE = re.compile(
    r"(记住|以后|今后|长期|内部考核|红线|按\s*\d+(?:\.\d+)?\s*kV|现场已(?:核实|确认)|别再|不要再|SOP|每次.{0,12}(?:必须|前先|核查))"
)
_CAB_EXTRACT_RE = re.compile(r"(?<!\d)(30[036]|31[12]|10(?:0[5-9]|1[0-9]|2[0-9])|110[1-9]|120[1-7])(?!\d)")


def detect_memory_candidates(
    question: str,
    answer_markdown: str = "",
    *,
    cabinets: list[str] | None = None,
    existing_memories: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Auto-Memory Distillation: sniff operator corrections, custom thresholds, or SOP rules from chat."""
    q = str(question or "").strip()
    if not q or not _MEMORY_INTENT_RE.search(q):
        return []

    existing_titles = {str(m.get("title", "")).strip().lower() for m in (existing_memories or [])}
    existing_contents = {str(m.get("content", "")).strip().lower() for m in (existing_memories or [])}
    found_cabs = list(dict.fromkeys([*(cabinets or []), *_CAB_EXTRACT_RE.findall(q)]))
    scope_cab = found_cabs[0] if found_cabs else None

    suggestions: list[dict[str, Any]] = []

    # 1. Custom voltage / metric threshold rule -> semantic
    val = _extract_voltage_kv(q)
    if val is not None:
        title = f"{scope_cab + ' 柜' if scope_cab else '10 kV 母线'}内部考核电压红线 {val:.2f} kV"
        if title.lower() not in existing_titles:
            suggestions.append({
                "category": "semantic",
                "title": title,
                "content": q[:500],
                "scope_cabinet": scope_cab,
                "metric_threshold": {"metric": "Uab", "operator": ">", "value": val, "unit": "kV"},
                "tags": list(dict.fromkeys(["定制红线", "电压考核", *([scope_cab] if scope_cab else [])])),
                "reason": f"检测到新的内部电压考核红线（{val:.2f} kV），入库后将在后续对话、深度排查与实时演练流中自动生效。",
            })

    # 2. Procedural SOP / check rule -> procedural
    if not suggestions and any(k in q for k in ("SOP", "每次", "操作前", "倒闸前", "规范", "流程", "必须先")):
        title = f"矿井值班 SOP：{q[:28]}"
        if title.lower() not in existing_titles and q.lower() not in existing_contents:
            suggestions.append({
                "category": "procedural",
                "title": title,
                "content": q[:500],
                "scope_cabinet": scope_cab,
                "metric_threshold": None,
                "tags": list(dict.fromkeys(["SOP", "值班规范", *([scope_cab] if scope_cab else [])])),
                "reason": "检测到可复用的值班核查规范/SOP，建议沉淀为持久程序记忆。",
            })

    # 3. Equipment quirk / field-confirmed fact -> semantic or episodic
    if not suggestions:
        is_confirmed = any(k in q for k in ("现场已核实", "现场已确认", "别再", "不要再", "记住", "以后"))
        cat = "semantic" if is_confirmed else "episodic"
        prefix = f"{scope_cab} 柜现场确认规则" if scope_cab else "矿井运维长期经验"
        title = f"{prefix}：{q[:24]}"
        if title.lower() not in existing_titles and q.lower() not in existing_contents:
            suggestions.append({
                "category": cat,
                "title": title,
                "content": q[:500],
                "scope_cabinet": scope_cab,
                "metric_threshold": None,
                "tags": list(dict.fromkeys(["自动提炼", *([scope_cab] if scope_cab else [])])),
                "reason": "检测到操作员现场确认信息或运维偏好，确认入库后 Agent 将跨会话持续遵守。",
            })

    return suggestions[:2]


def distill_ticket_memory_candidate(
    ticket: dict[str, Any],
    *,
    action: str | None = None,
    note: str = "",
    existing_memories: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Distill a close/dispute/supplement incident ticket into a draft episodic memory candidate."""
    if not isinstance(ticket, dict):
        return None
    eff_action = str(action or ticket.get("status") or "").strip().lower()
    if eff_action not in {"close", "closed", "dispute", "disputed", "supplement", "supplemented"}:
        return None
    eff_note = str(note or ticket.get("resolution_note") or "").strip()
    if len(eff_note) < 6:
        return None

    tid = str(ticket.get("ticket_id") or "").strip()
    cab_raw = str(ticket.get("cabinet") or "").strip()
    scope_cab = cab_raw if re.fullmatch(r"\d{3,4}", cab_raw) else None
    at_time = str(ticket.get("at_time") or "").strip()
    src_id = str(ticket.get("source_finding_id") or "").strip()

    if eff_action in {"dispute", "disputed"}:
        action_label = "工单人工否决经验"
        reason = "检测到值班员否决了自动告警/初诊，建议沉淀为负样本排障案例，供后续排查对照参考。"
    elif eff_action in {"supplement", "supplemented"}:
        action_label = "工单现场补证结论"
        reason = "检测到工单补充了现场台账/复核依据，建议沉淀为历史案例记忆。"
    else:
        action_label = "工单结案复盘案例"
        reason = "检测到告警工单已完成销项结案，建议将处置依据沉淀为可复用排障经验。"

    cab_prefix = f"{cab_raw} 柜" if cab_raw else "全站"
    raw_title = str(ticket.get("title") or src_id or tid or "异常处理").strip()
    title = f"{action_label}（{cab_prefix}）：{raw_title}"[:140]
    ev_cells = [str(c) for c in (ticket.get("evidence_cells") or [])[:4] if c]
    parts = [f"处置结论：{eff_note[:600]}"]
    if at_time:
        parts.append(f"关联时段：{at_time[:16]}")
    if src_id:
        parts.append(f"来源规则：{src_id}")
    if ev_cells:
        parts.append(f"原表依据：{', '.join(ev_cells)}")
    content = "；".join(parts)[:2000]

    ticket_tag = f"ticket:{tid}" if tid else ""
    existing = existing_memories or []
    for mem in existing:
        if ticket_tag and ticket_tag in (mem.get("tags") or []):
            return None
        if str(mem.get("title", "")).strip().lower() == title.lower():
            return None
        if str(mem.get("content", "")).strip().lower() == content.lower():
            return None

    tags = ["工单蒸馏", action_label]
    if scope_cab:
        tags.append(scope_cab)
    if src_id:
        tags.append(src_id)
    if ticket_tag:
        tags.append(ticket_tag)

    return {
        "category": "episodic",
        "title": title,
        "content": content,
        "scope_cabinet": scope_cab,
        "metric_threshold": None,
        "tags": list(dict.fromkeys(tags))[:12],
        "source": "ticket_distill",
        "source_ticket_id": tid or None,
        "reason": reason,
    }


def format_memories_for_prompt(memories: list[dict[str, Any]]) -> str:
    if not memories:
        return ""
    lines = ["## 当前已启用的矿井长期运营记忆与定制红线（请务必遵守并在结论中引用）"]
    cat_labels = {"semantic": "语义/红线", "episodic": "历史案例", "procedural": "SOP/规程"}
    sigs_seen: list[dict[str, Any]] = []
    has_conflict = False
    for m in memories:
        cat = cat_labels.get(m["category"], m["category"])
        cab = f" [适用柜: {m['scope_cabinet']}]" if m.get("scope_cabinet") else ""
        thr = f" [定制阈值: {m['metric_threshold']}]" if m.get("metric_threshold") else ""
        exp = f" [有效期至: {str(m['expires_at'])[:10]}]" if m.get("expires_at") else ""
        lines.append(f"- [{cat} · {m['id']}]{cab}{thr}{exp} **{m['title']}**：{m['content']}")
        sig = _extract_rule_signature(
            m.get("category", ""),
            m.get("title", ""),
            m.get("content", ""),
            m.get("scope_cabinet"),
            m.get("metric_threshold"),
        )
        if sig is not None:
            for prev in sigs_seen:
                if (
                    prev["metric"] == sig["metric"]
                    and prev["operator"] == sig["operator"]
                    and abs(prev["value"] - sig["value"]) >= 1e-6
                    and (prev["scope_cabinet"] == sig["scope_cabinet"] or prev["scope_cabinet"] == "" or sig["scope_cabinet"] == "")
                ):
                    has_conflict = True
                    break
            sigs_seen.append(sig)
    if has_conflict:
        lines.append("- [冲突提醒] 检测到多条启用中的语义阈值存在跨条冲突，回答中须显式说明差异并提示值班员核准或停用旧规则。")
    return "\n".join(lines)


class MemoryStore:
    """Convenience object wrapper around the operational memory store functions."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path) if db_path is not None else MEMORY_DB

    def list(self, **kwargs) -> list[dict[str, Any]]:
        return list_memories(db_path=self.db_path, **kwargs)

    def get(self, memory_id: str) -> dict[str, Any] | None:
        return get_memory(memory_id, db_path=self.db_path)

    def create(self, **kwargs) -> dict[str, Any]:
        return create_memory(db_path=self.db_path, **kwargs)

    def create_with_report(self, **kwargs) -> dict[str, Any]:
        return create_memory_with_report(db_path=self.db_path, **kwargs)

    def update(self, memory_id: str, **kwargs) -> dict[str, Any] | None:
        return update_memory(memory_id, db_path=self.db_path, **kwargs)

    def update_with_report(self, memory_id: str, **kwargs) -> dict[str, Any] | None:
        return update_memory_with_report(memory_id, db_path=self.db_path, **kwargs)

    def delete(self, memory_id: str) -> bool:
        return delete_memory(memory_id, db_path=self.db_path)

    def retrieve(self, question: str, *, cabinets: list[str] | None = None, limit: int = 6) -> list[dict[str, Any]]:
        return retrieve_relevant_memories(question, cabinets=cabinets, limit=limit, db_path=self.db_path)

    def detect_conflicts(self, **kwargs) -> list[dict[str, Any]]:
        return detect_memory_conflicts(db_path=self.db_path, **kwargs)

    def prune_expired(self, **kwargs) -> dict[str, Any]:
        return prune_expired_memories(db_path=self.db_path, **kwargs)

    def distill_ticket(
        self,
        ticket: dict[str, Any],
        *,
        action: str | None = None,
        note: str = "",
    ) -> dict[str, Any] | None:
        existing = self.list(include_expired=True)
        return distill_ticket_memory_candidate(
            ticket,
            action=action,
            note=note,
            existing_memories=existing,
        )
