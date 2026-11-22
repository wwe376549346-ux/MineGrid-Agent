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
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
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


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
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
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_memories(
    *,
    category: str | None = None,
    enabled_only: bool = False,
    cabinet: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    if category is not None and category not in MEMORY_CATEGORIES:
        raise ValueError(f"category 须为 {', '.join(MEMORY_CATEGORIES)} 之一。")
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
        return [_row_to_dict(r) for r in rows]
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
    mem_id = f"MEM-{uuid.uuid4().hex[:12].upper()}"
    now = _utc_now()

    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO operational_memory
              (id, category, title, content, scope_cabinet, metric_threshold, tags, enabled, source, hit_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
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
                now,
                now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM operational_memory WHERE id = ?", (mem_id,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


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

        updates: dict[str, Any] = {"updated_at": _utc_now()}
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

        set_sql = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE operational_memory SET {set_sql} WHERE id = ?",
            (*updates.values(), existing["id"]),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM operational_memory WHERE id = ?", (existing["id"],)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


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


def extract_custom_voltage_thresholds(memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract active operator-defined 10 kV / 35 kV voltage thresholds from semantic memories only."""
    found: list[dict[str, Any]] = []
    for mem in memories:
        if not mem.get("enabled", True) or mem.get("category") != "semantic":
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


def format_memories_for_prompt(memories: list[dict[str, Any]]) -> str:
    if not memories:
        return ""
    lines = ["## 当前已启用的矿井长期运营记忆与定制红线（请务必遵守并在结论中引用）"]
    cat_labels = {"semantic": "语义/红线", "episodic": "历史案例", "procedural": "SOP/规程"}
    for m in memories:
        cat = cat_labels.get(m["category"], m["category"])
        cab = f" [适用柜: {m['scope_cabinet']}]" if m.get("scope_cabinet") else ""
        thr = f" [定制阈值: {m['metric_threshold']}]" if m.get("metric_threshold") else ""
        lines.append(f"- [{cat} · {m['id']}]{cab}{thr} **{m['title']}**：{m['content']}")
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

    def update(self, memory_id: str, **kwargs) -> dict[str, Any] | None:
        return update_memory(memory_id, db_path=self.db_path, **kwargs)

    def delete(self, memory_id: str) -> bool:
        return delete_memory(memory_id, db_path=self.db_path)

    def retrieve(self, question: str, *, cabinets: list[str] | None = None, limit: int = 6) -> list[dict[str, Any]]:
        return retrieve_relevant_memories(question, cabinets=cabinets, limit=limit, db_path=self.db_path)
