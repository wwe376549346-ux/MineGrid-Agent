"""pgvector-Native 128-Dim Semantic Vector Store with SQLAlchemy 2.0 Pooled Connections.

- On PostgreSQL + pgvector: executes native `ORDER BY embedding <=> CAST(:qvec AS vector)` HNSW search.
- On SQLite (local dev without PG daemon): executes exact cosine similarity over the identical
  Alembic-managed `vector_document` table and 128-dim electrical domain semantic embeddings.
"""

from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
import math
import re
from typing import Any

from sqlalchemy import text

from laowadi.db.engine import DatabasePoolManager
from laowadi.db.models import VECTOR_DIM, VectorDocumentORM


# Coal-mine power distribution semantic concept clusters (32 core domain axes + 96 hashed n-gram features = 128 dim)
DOMAIN_CONCEPT_GROUPS: list[tuple[str, ...]] = [
    ("过压", "电压偏高", "母线高压", "越上限", "10.70", "10.90", "11.01", "12325"),
    ("欠压", "电压偏低", "压降", "末端电压", "9.30"),
    ("分接头", "调压", "调档", "变比", "3.556", "3.440", "主变"),
    ("功率因数", "低cos", "无功", "感性无功", "补偿", "kvar", "电容器", "svg", "无功倒补"),
    ("负有功", "反向有功", "逆潮流", "功率倒灌", "极性", "ct反接", "相序", "1018"),
    ("瓦斯发电", "反送电", "并网", "1027"),
    ("单进线", "双主变", "停运", "零功率", "母联", "300", "306", "303"),
    ("n-1", "转供", "负荷转移", "备用电源", "两回路", "分列运行", "50417"),
    ("下井", "井下", "1535", "中央变电所", "1009", "1010", "1101", "1201", "1200"),
    ("1750", "配电点", "1020"),
    ("补偿控制柜", "1011", "1012", "表计异常"),
    ("备用柜", "悬浮电压", "pt零漂", "1030", "1032", "313"),
    ("功率守恒", "功率平衡", "scada", "轮询", "非同步", "偏移", "阶跃"),
    ("避峰填谷", "分时基线", "班次", "电价", "日内曲线", "负荷波动"),
    ("比例系数", "倍率", "ct变比", "两表法", "计量口径", "0.684"),
    ("潮流", "pandapower", "牛顿拉夫逊", "网损", "线损", "铜损", "降损"),
    ("电缆", "载流量", "截面积", "型号", "降容", "热稳定"),
    ("保护定值", "继电保护", "soe", "跳闸", "录波", "短路", "接地", "中性点"),
    ("安全规程", "煤矿安全规程", "第四百", "国标", "gb/t", "合规", "台账"),
    ("检修", "倒闸", "操作票", "停电", "开关状态", "复核", "补证"),
    ("主变", "311", "312", "1005", "1006", "1号主变", "2号主变"),
    ("二采区", "1023", "1018", "主平硐", "1017", "充电桩", "1007"),
    ("变压器", "1016", "1019", "1022", "1024", "1025", "1026"),
    ("页尾", "整点", "双口径", "单元格", "excel", "sha256", "溯源"),
    ("lw-000010", "11-10", "11-12", "11-13", "11-16", "1110.xls", "1113.xls"),
    ("过载", "裕度", "额定容量", "kva", "负载率", "条件满足"),
    ("缺证", "证据不足", "跨站", "无读数", "适用性"),
    ("多智能体", "仲裁", "冲突", "协同", "审计"),
    ("熔断", "限流", "rbac", "安全围栏", "只读"),
    ("排水泵", "主排水", "水仓", "采面", "运巷", "回风"),
    ("谐波", "thd", "串联谐振", "过补偿"),
    ("电流", "有功", "线电压", "标准差", "累计电量", "kwh"),
]

_TOKEN_RE = re.compile(r"[a-z0-9_.\-/]+|[\u4e00-\u9fff]{1,2}", re.I)


def embed_text_128(text_input: str) -> list[float]:
    """Produce a deterministic L2-normalized 128-dim domain+subword semantic vector (pgvector compatible)."""
    raw = str(text_input or "").lower()
    vec = [0.0] * VECTOR_DIM

    # Dimensions 0..31: coal-mine power domain semantic concept axes (weighted 2.5x for high recall on paraphrases)
    for idx, terms in enumerate(DOMAIN_CONCEPT_GROUPS[:32]):
        hits = sum(1 for t in terms if t in raw)
        if hits > 0:
            vec[idx] += 2.5 * (1.0 + math.log1p(hits))

    # Dimensions 32..127: signed feature hashing over tokens & character bigrams
    tokens = _TOKEN_RE.findall(raw)
    for tok in tokens:
        digest = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
        bucket = 32 + (int.from_bytes(digest[:4], "little") % (VECTOR_DIM - 32))
        sign = 1.0 if (digest[4] & 1) == 0 else -1.0
        vec[bucket] += sign

    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 1e-12:
        vec = [round(v / norm, 6) for v in vec]
    return vec


def format_pgvector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


def parse_vector_literal(raw: Any) -> list[float]:
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    s = str(raw or "").strip()
    if s.startswith("[") and s.endswith("]"):
        return [float(x) for x in s[1:-1].split(",") if x.strip()]
    return []


class PgVectorStore:
    """Unified semantic vector repository supporting native pgvector HNSW & SQLite fallback."""

    def __init__(self, db_manager: DatabasePoolManager):
        self.db = db_manager

    def upsert_documents(self, docs: list[dict[str, Any]], *, connection=None) -> int:
        if not docs:
            return 0
        if self.db.is_postgres:
            embedding = "CAST(:embedding AS vector)" if self.db.extensions_status().get("pgvector_active") else ":embedding"
            sql = text(
                f"""
                INSERT INTO vector_document (doc_id, doc_kind, title, content, metadata_json, source_sha256, embedding)
                VALUES (:doc_id, :doc_kind, :title, :content, :metadata_json, :source_sha256, {embedding})
                ON CONFLICT (doc_id) DO UPDATE SET
                    doc_kind = EXCLUDED.doc_kind,
                    title = EXCLUDED.title,
                    content = EXCLUDED.content,
                    metadata_json = EXCLUDED.metadata_json,
                    source_sha256 = EXCLUDED.source_sha256,
                    embedding = EXCLUDED.embedding
                """
            )
            params_list = []
            for d in docs:
                title = str(d.get("title") or "")
                content = str(d.get("content") or "")
                params_list.append({
                    "doc_id": str(d["doc_id"]),
                    "doc_kind": str(d.get("doc_kind") or "document"),
                    "title": title,
                    "content": content,
                    "metadata_json": json.dumps(d.get("metadata") or {}, ensure_ascii=False),
                    "source_sha256": str(d.get("source_sha256") or ""),
                    "embedding": format_pgvector_literal(embed_text_128(f"{title}\n{content}")),
                })
            transaction = nullcontext(connection) if connection is not None else self.db.engine.begin()
            with transaction as conn:
                conn.execute(sql, params_list)
            return len(docs)

        with self.db.session() as sess:
            for d in docs:
                doc_id = str(d["doc_id"])
                title = str(d.get("title") or "")
                content = str(d.get("content") or "")
                vec = embed_text_128(f"{title}\n{content}")
                vec_lit = format_pgvector_literal(vec)
                meta_json = json.dumps(d.get("metadata") or {}, ensure_ascii=False)
                existing = sess.get(VectorDocumentORM, doc_id)
                if existing is None:
                    sess.add(
                        VectorDocumentORM(
                            doc_id=doc_id,
                            doc_kind=str(d.get("doc_kind") or "document"),
                            title=title,
                            content=content,
                            metadata_json=meta_json,
                            source_sha256=str(d.get("source_sha256") or ""),
                            embedding=vec_lit,
                        )
                    )
                else:
                    existing.doc_kind = str(d.get("doc_kind") or existing.doc_kind)
                    existing.title = title
                    existing.content = content
                    existing.metadata_json = meta_json
                    existing.source_sha256 = str(d.get("source_sha256") or "")
                    existing.embedding = vec_lit
        return len(docs)

    def search(self, query: str, *, top_k: int = 5, doc_kind: str | None = None) -> dict[str, Any]:
        q_vec = embed_text_128(query)
        q_lit = format_pgvector_literal(q_vec)
        k = max(1, min(int(top_k), 20))

        # Native PostgreSQL + pgvector HNSW cosine distance path (`<=>`)
        if self.db.is_postgres and self.db.extensions_status().get("pgvector_active"):
            where = "WHERE doc_kind = :doc_kind" if doc_kind else ""
            sql = text(
                f"""
                SELECT doc_id, doc_kind, title, content, metadata_json, source_sha256,
                       1.0 - (embedding <=> CAST(:qvec AS vector)) AS similarity
                FROM vector_document
                {where}
                ORDER BY embedding <=> CAST(:qvec AS vector)
                LIMIT :k
                """
            )
            params: dict[str, Any] = {"qvec": q_lit, "k": k}
            if doc_kind:
                params["doc_kind"] = doc_kind
            with self.db.engine.connect() as conn:
                rows = conn.execute(sql, params).mappings().all()
            return {
                "engine": "pgvector_hnsw_cosine",
                "dim": VECTOR_DIM,
                "query": query,
                "hits": [
                    {
                        "doc_id": r["doc_id"],
                        "doc_kind": r["doc_kind"],
                        "title": r["title"],
                        "similarity": round(float(r["similarity"]), 4),
                        "source_sha256": r["source_sha256"],
                        "metadata": json.loads(r["metadata_json"] or "{}"),
                        "snippet": str(r["content"])[:260],
                    }
                    for r in rows
                ],
            }

        # Portable SQLAlchemy pooled cosine similarity over the exact same `vector_document` table
        where = "WHERE doc_kind = :doc_kind" if doc_kind else ""
        sql = text(f"SELECT doc_id, doc_kind, title, content, metadata_json, source_sha256, embedding FROM vector_document {where}")
        params = {"doc_kind": doc_kind} if doc_kind else {}
        scored = []
        with self.db.engine.connect() as conn:
            for r in conn.execute(sql, params).mappings():
                d_vec = parse_vector_literal(r["embedding"])
                sim = sum(a * b for a, b in zip(q_vec, d_vec)) if len(d_vec) == len(q_vec) else 0.0
                scored.append((sim, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        return {
            "engine": "sqlalchemy_pooled_cosine_pgvector_compatible",
            "dim": VECTOR_DIM,
            "query": query,
            "hits": [
                {
                    "doc_id": r["doc_id"],
                    "doc_kind": r["doc_kind"],
                    "title": r["title"],
                    "similarity": round(float(sim), 4),
                    "source_sha256": r["source_sha256"],
                    "metadata": json.loads(r["metadata_json"] or "{}"),
                    "snippet": str(r["content"])[:260],
                }
                for sim, r in scored[:k]
            ],
        }
