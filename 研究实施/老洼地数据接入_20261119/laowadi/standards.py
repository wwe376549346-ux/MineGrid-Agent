"""Local read-only retrieval over archived Coal Mine Safety Regulations (2025) and GB references."""

from __future__ import annotations

from collections import Counter, OrderedDict
from functools import lru_cache
import json
import math
from pathlib import Path
import os
import re
from threading import Lock
from typing import Protocol, Sequence

from laowadi.storage import ROOT


# The optional full text remains available to operators for staging or backup.
# Retrieval always uses the packaged, audited index.
STANDARDS_DIR = Path(os.environ.get("LAOWADI_STANDARDS_DIR", ROOT / "standards"))
REGULATION_TXT = STANDARDS_DIR / "核验记录" / "煤矿安全规程_2025_全文.txt"
CORPUS_PATH = Path(__file__).with_name("data") / "standards_corpus.json"


@lru_cache(maxsize=1)
def load_corpus() -> list[dict]:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


class EmbeddingProvider(Protocol):
    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Return one vector per input, in input order."""


class DenseEmbeddingCache:
    """Bounded, process-local cache tied to one embedding provider."""

    def __init__(self, provider: EmbeddingProvider, *, max_batches: int = 128):
        self.provider = provider
        self.max_batches = max(2, max_batches)
        self._batches: OrderedDict[tuple[str, ...], tuple[tuple[float, ...], ...]] = OrderedDict()
        self._lock = Lock()

    def _vectors(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        with self._lock:
            if texts in self._batches:
                self._batches.move_to_end(texts)
                return self._batches[texts]
            vectors = []
            for raw in self.provider.embed(texts):
                vector = tuple(float(v) for v in raw)
                norm = math.hypot(*vector)
                if not norm or not math.isfinite(norm):
                    raise ValueError("Invalid embedding vector")
                vectors.append(tuple(v / norm for v in vector))
            if len(vectors) != len(texts) or len({len(v) for v in vectors}) != 1:
                raise ValueError("Embedding batch shape does not match the inputs")
            result = tuple(vectors)
            self._batches[texts] = result
            if len(self._batches) > self.max_batches:
                self._batches.popitem(last=False)
            return result

    def similarities(self, query: str, docs: list[str]) -> list[float]:
        corpus = self._vectors(tuple(docs))
        query_vector, = self._vectors((query,))
        if len(query_vector) != len(corpus[0]):
            raise ValueError("Query and document embedding dimensions differ")
        return [sum(q * d for q, d in zip(query_vector, vector)) for vector in corpus]


DOMAIN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "reverse_power": ("反向有功", "逆潮流", "倒送电", "功率倒灌", "反送", "负有功", "功率反送"),
    "overvoltage": ("过压", "过电压", "电压偏高", "电压越限", "电压偏差", "母线高压", "电能质量", "10.70"),
    "low_pf": ("低功率因数", "功率因数偏低", "无功缺口", "感性负荷", "欠补偿", "无功补偿", "cos偏低"),
    "cap_anomaly": ("无功倒补", "过补偿", "电容支路", "补偿控制柜", "电容器投切", "svg"),
    "single_incomer": ("单进线", "单回供电", "单回运行", "母联合位", "双主变", "两回路", "备用电源", "n-1"),
    "ct_polarity": ("极性反接", "ct反接", "二次接线", "相序错误", "互感器极性", "接线错误", "量测异常"),
    "polling_skew": ("轮询偏移", "非同步采样", "scada时差", "扫描错位", "瞬时差额", "功率平衡"),
    "peak_valley": ("避峰填谷", "分时基线", "班次切换", "负荷突变", "常规波动", "峰谷"),
    "neutral_ground": ("中性点", "直接接地", "消弧线圈", "不接地"),
}


def expand_electrical_concepts(text: str) -> list[str]:
    """Expand Chinese coal-mine electrical synonyms into shared semantic concept tokens."""
    low = text.lower()
    concepts: list[str] = []
    for cid, phrases in DOMAIN_SYNONYMS.items():
        hits = sum(1 for p in phrases if p in low)
        if hits:
            concepts.append(f"__concept_{cid}__")
            # Also add primary canonical phrase tokens for cross-term overlap
            concepts.extend(_tokenize(phrases[0]))
    return concepts


def _tokenize(text: str) -> list[str]:
    norm = re.sub(r"\s+", "", text.lower())
    tokens = re.findall(r"[a-z0-9./+-]+", norm)
    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", norm)
    for run in cjk_runs:
        if len(run) == 1:
            tokens.append(run)
        else:
            for i in range(len(run) - 1):
                tokens.append(run[i:i + 2])
            for i in range(len(run) - 2):
                tokens.append(run[i:i + 3])
    return tokens


def rank_documents(
    query: str,
    doc_texts: list[str],
    *,
    rrf_k: int = 30,
    dense: DenseEmbeddingCache | None = None,
) -> tuple[list[dict], str]:
    """Fuse BM25 with dense cosine ranks, or use concept-expanded lexical fallback."""
    n_docs = len(doc_texts)
    if n_docs == 0 or not query.strip():
        return [], "lexical_fallback"

    q_lex = _tokenize(query)
    q_sem = q_lex + expand_electrical_concepts(query)
    docs_lex = [_tokenize(t) for t in doc_texts]
    docs_sem = [dl + expand_electrical_concepts(t) for dl, t in zip(docs_lex, doc_texts)]

    # 1. Okapi BM25 lexical ranking
    avg_len = sum(len(d) for d in docs_lex) / max(n_docs, 1)
    df_lex = Counter()
    for d in docs_lex:
        for tok in set(d):
            df_lex[tok] += 1
    q_lex_counts = Counter(q_lex)
    k1, b = 1.5, 0.75
    bm25_scores = []
    for d_toks in docs_lex:
        tf = Counter(d_toks)
        d_len = len(d_toks)
        score = 0.0
        for tok, qf in q_lex_counts.items():
            if tok not in tf:
                continue
            idf = math.log(1.0 + (n_docs - df_lex[tok] + 0.5) / (df_lex[tok] + 0.5))
            num = tf[tok] * (k1 + 1.0)
            den = tf[tok] + k1 * (1.0 - b + b * d_len / max(avg_len, 1.0))
            score += idf * (num / den) * (1.0 + 0.2 * min(qf - 1, 3))
        bm25_scores.append(score)

    backend = "lexical_fallback"
    if dense is not None:
        try:
            sem_scores = dense.similarities(query, doc_texts)
            backend = "dense+bm25"
        except Exception:
            # Provider unavailable or returned invalid vectors. Keep lexical retrieval available.
            pass
    if backend == "lexical_fallback":
        # 2. Concept-Expanded TF-IDF Cosine Vector Similarity
        df_sem = Counter()
        for d in docs_sem:
            for tok in set(d):
                df_sem[tok] += 1

        def _vec(toks: list[str]) -> dict[str, float]:
            tf = Counter(toks)
            v = {}
            for tok, cnt in tf.items():
                weight = 2.4 if tok.startswith("__concept_") else 1.0
                idf = math.log(1.0 + (n_docs + 1.0) / (df_sem.get(tok, 0) + 1.0))
                v[tok] = (1.0 + math.log(cnt)) * idf * weight
            return v

        q_vec = _vec(q_sem)
        q_norm = math.sqrt(sum(x * x for x in q_vec.values())) or 1.0
        sem_scores = []
        for d_toks in docs_sem:
            d_vec = _vec(d_toks)
            d_norm = math.sqrt(sum(x * x for x in d_vec.values())) or 1.0
            dot = sum(val * d_vec.get(tok, 0.0) for tok, val in q_vec.items())
            sem_scores.append(dot / (q_norm * d_norm))

    # 3. Reciprocal Rank Fusion (RRF)
    bm25_order = sorted(range(n_docs), key=lambda i: (-bm25_scores[i], i))
    sem_order = sorted(range(n_docs), key=lambda i: (-sem_scores[i], i))
    bm25_rank = {idx: r for r, idx in enumerate(bm25_order, 1)}
    sem_rank = {idx: r for r, idx in enumerate(sem_order, 1)}

    fused = []
    for i in range(n_docs):
        if bm25_scores[i] <= 0 and sem_scores[i] <= 0.01:
            continue
        rrf = (1.0 / (rrf_k + bm25_rank[i]) if bm25_scores[i] > 0 else 0.0) + (
            1.0 / (rrf_k + sem_rank[i]) if sem_scores[i] > 0.01 else 0.0
        )
        # Preserve magnitude scale for downstream tie-breaking
        combined = rrf * 100.0
        if backend == "lexical_fallback":
            combined += bm25_scores[i] * 0.15 + sem_scores[i] * 6.0
        fused.append({
            "index": i,
            "retrieval_backend": backend,
            "bm25": round(bm25_scores[i], 3),
            "semantic": round(sem_scores[i], 4),
            "rrf": round(rrf, 5),
            "score": round(combined, 3),
        })
    fused.sort(key=lambda x: (-x["score"], x["index"]))
    return fused, backend


def hybrid_rank_documents(
    query: str, doc_texts: list[str], *, rrf_k: int = 30, dense: DenseEmbeddingCache | None = None,
) -> list[dict]:
    return rank_documents(query, doc_texts, rrf_k=rrf_k, dense=dense)[0]


def search_standards(
    query: str, top_k: int = 4, *, embedding_provider: EmbeddingProvider | None = None,
    embedding_cache: DenseEmbeddingCache | None = None,
) -> dict:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("检索关键词不能为空。")
    top_k = max(1, min(int(top_k), 8))
    corpus = load_corpus()
    doc_texts = [f'{c["id"]} {c["title"]} {c["text"]} {c["applicability"]}' for c in corpus]
    dense = embedding_cache or (DenseEmbeddingCache(embedding_provider) if embedding_provider is not None else None)
    ranked, backend = rank_documents(query, doc_texts, dense=dense)

    # Apply exact article / standard ID boost on top of Hybrid BM25 + Semantic RRF
    q_low = query.lower()
    for item in ranked:
        card = corpus[item["index"]]
        if card["id"].lower() in q_low or (card.get("article") and card["article"] in query):
            item["score"] = round(item["score"] + 15.0, 3)
    ranked.sort(key=lambda x: (-x["score"], x["index"]))

    hits = [
        {
            "id": corpus[item["index"]]["id"],
            "source": corpus[item["index"]]["source"],
            "legal_tier": corpus[item["index"]]["legal_tier"],
            "pdf_page": corpus[item["index"]]["pdf_page"],
            "title": corpus[item["index"]]["title"],
            "excerpt": corpus[item["index"]]["text"],
            "applicability": corpus[item["index"]]["applicability"],
            "score": item["score"],
            "bm25_score": item["bm25"],
            "semantic_score": item["semantic"],
        }
        for item in ranked[:top_k]
    ]
    return {
        "kind": "standard_search",
        "retrieval_mode": "hybrid_bm25_semantic_rrf",
        "status": "ok" if hits else "missing",
        "query": query,
        "corpus_size": len(corpus),
        "retrieval_backend": backend,
        "hits": hits,
    }
