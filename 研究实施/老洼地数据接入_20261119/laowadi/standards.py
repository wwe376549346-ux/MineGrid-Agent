"""Local read-only retrieval over archived Coal Mine Safety Regulations (2025) and GB references."""

from __future__ import annotations

from collections import Counter
import math
from pathlib import Path
import os
import re

from laowadi.storage import ROOT


STANDARDS_DIR = Path(os.environ.get("LAOWADI_STANDARDS_DIR", ROOT.parent.parent / "公开数据与标准_20261113"))
REGULATION_TXT = STANDARDS_DIR / "核验记录" / "煤矿安全规程_2025_全文.txt"

# Audited reference summaries from 公开数据与标准_20261113/标准条款与数据判据.md
AUDITED_GB_CARDS = [
    {
        "id": "GB/T 50417-2017 3.0.3",
        "source": "GB/T 50417-2017《煤矿井下供配电设计规范》",
        "legal_tier": "推荐性国家标准（已目读扫描原件）",
        "pdf_page": "PDF 10–11 页（正文 6–7 页）",
        "title": "井下中央/分水平变电所两回路供电要求",
        "text": (
            "井下中央变电所和分水平变电所应采用两回路电源线路供电，来自上级变电所不同母线段；"
            "当任一回路停止供电时，其余回路应能担负全部负荷（或按规定负荷分级供电）。"
            "核验前提：需生效单线图、线路来源、开关状态及设备/线路载流容量。"
        ),
        "applicability": "适用于井下中央变电所（如老洼地 1535 中央变电所 1101/1201 两回进线）供电结构校核。",
    },
    {
        "id": "GB/T 50417-2017 3.0.4",
        "source": "GB/T 50417-2017《煤矿井下供配电设计规范》",
        "legal_tier": "推荐性国家标准（已目读扫描原件）",
        "pdf_page": "PDF 11 页",
        "title": "采区变电所回路数与备用供电能力",
        "text": (
            "采区变电所供电回路配置及任一回路退出后的用电负荷供给能力要求。"
            "核验前提：需采区身份、回路拓扑、设备与电缆额定限额及同时用电工况；仅靠电流相关性不能证明电源独立。"
        ),
        "applicability": "适用于采区配电回路（如二采区 I/II 回、1750 配电点）备用能力核查。",
    },
    {
        "id": "GB/T 50417-2017 3.0.5",
        "source": "GB/T 50417-2017《煤矿井下供配电设计规范》",
        "legal_tier": "推荐性国家标准（已目读扫描原件）",
        "pdf_page": "PDF 11 页",
        "title": "严禁井下变压器中性点直接接地",
        "text": (
            "严禁井下配电变压器中性点直接接地，严禁由地面中性点直接接地的变压器或者发电机直接向井下供电。"
            "（与《煤矿安全规程》2025 版第四百七十六条一致；须查变压器接线台账与图纸，不能仅凭电表读数判定。）"
        ),
        "applicability": "适用于矿井地面向井下供电变压器及中性点接地方式台账审核。",
    },
    {
        "id": "GB/T 50417-2017 3.0.6",
        "source": "GB/T 50417-2017《煤矿井下供配电设计规范》",
        "legal_tier": "推荐性国家标准（已目读扫描原件）",
        "pdf_page": "PDF 11 页",
        "title": "局部通风机供电与风电/甲烷电闭锁",
        "text": (
            "局部通风机双电源/专用变压器与风电、甲烷电闭锁供电要求。"
            "核验前提：需瓦斯等级、设备台账、馈电开关状态及风速/甲烷联锁试验记录。"
        ),
        "applicability": "适用于掘进面局部通风机供电专项核查。",
    },
    {
        "id": "GB/T 12325-2008（元数据已核，全文待复核）",
        "source": "GB/T 12325-2008《电能质量 供电电压偏差》",
        "legal_tier": "推荐性国家标准（现行版本元数据已核，工程参考口径）",
        "pdf_page": "待补全文核对",
        "title": "供电电压允许偏差参考口径（10 kV ±7% / 35 kV 正负偏差绝对值之和 ≤10%）",
        "text": (
            "行业通用参考口径：20 kV 及以下三相供电电压允许偏差为标称电压的 ±7%（10 kV 对应 9.30–10.70 kV）；"
            "35 kV 及以上供电电压正、负偏差绝对值之和不超过标称电压的 10%。"
            "注意：本项目尚未归档 GB/T 12325-2008 完整原文，电压超限结果目前作运行筛查与待核参考，正式合规判定须复核国标原文及测量统计窗口。"
        ),
        "applicability": "用于老洼地 35 kV 与 10 kV I/II 段母线电压偏差运行筛查。",
    },
]

_ARTICLE_RE = re.compile(r"第\s*([四五]\s*百\s*[零一二三四五六七八九十\s]+?)\s*条")
_PAGE_RE = re.compile(r"\[PDF页(\d+)\]")
_CJK_SPACE_RE = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")
_CACHE: list[dict] | None = None


def _clean_text(text: str) -> str:
    text = re.sub(r"—[０-９0-9]+—", "", text)
    text = _PAGE_RE.sub("", text)
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s*\n\s*", "", text)
    text = _CJK_SPACE_RE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def _parse_regulation_clauses(path: Path = REGULATION_TXT) -> list[dict]:
    if not path.is_file():
        return []
    raw = path.read_text(encoding="utf-8", errors="ignore")
    start = raw.find("第十二章\u3000电气")
    if start == -1:
        start = raw.find("第四百七十一条")
    end = raw.find("第五百三十五条")
    if start == -1:
        return []
    section = raw[start:end if end != -1 else len(raw)]

    # Track PDF page for each character offset
    pages = [(m.start(), int(m.group(1))) for m in _PAGE_RE.finditer(section)]
    # Determine initial page right before start
    prev_pages = list(_PAGE_RE.finditer(raw[:start]))
    base_page = int(prev_pages[-1].group(1)) if prev_pages else 194

    def page_at(pos: int) -> int:
        p = base_page
        for offset, num in pages:
            if offset <= pos:
                p = num
            else:
                break
        return p

    matches = list(_ARTICLE_RE.finditer(section))
    clauses = []
    for idx, m in enumerate(matches):
        body_start = m.start()
        body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(section)
        num_clean = re.sub(r"\s+", "", m.group(1))
        article_id = f"第{num_clean}条"
        p_start = page_at(body_start)
        p_end = page_at(body_end - 1)
        page_label = f"PDF {p_start} 页" if p_start == p_end else f"PDF {p_start}–{p_end} 页"
        cleaned = _clean_text(section[body_start:body_end])
        clauses.append({
            "id": f"煤矿安全规程(2025) {article_id}",
            "article": article_id,
            "source": "《煤矿安全规程》（应急管理部令第 17 号，2025 修订，2026-02-01 实施）",
            "legal_tier": "部门规章（已归档全文原文）",
            "pdf_page": page_label,
            "title": f"{article_id}（电气/监控章节原文）",
            "text": cleaned[:700] + ("……" if len(cleaned) > 700 else ""),
            "applicability": "适用于煤矿地面与井下供配电运行、保护、检修及安全监控合规核查。",
        })
    return clauses


def load_corpus() -> list[dict]:
    global _CACHE
    if _CACHE is None:
        _CACHE = list(AUDITED_GB_CARDS) + _parse_regulation_clauses()
    return _CACHE


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


def hybrid_rank_documents(
    query: str,
    doc_texts: list[str],
    *,
    rrf_k: int = 30,
) -> list[dict]:
    """Hybrid Okapi BM25 + Concept-Expanded TF-IDF Cosine Similarity fused via Reciprocal Rank Fusion (RRF)."""
    n_docs = len(doc_texts)
    if n_docs == 0 or not query.strip():
        return []

    q_lex = _tokenize(query)
    q_sem = q_lex + expand_electrical_concepts(query)
    if not q_sem:
        return []

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
        combined = rrf * 100.0 + bm25_scores[i] * 0.15 + sem_scores[i] * 6.0
        fused.append({
            "index": i,
            "bm25": round(bm25_scores[i], 3),
            "semantic": round(sem_scores[i], 4),
            "rrf": round(rrf, 5),
            "score": round(combined, 3),
        })
    fused.sort(key=lambda x: (-x["score"], x["index"]))
    return fused


def search_standards(query: str, top_k: int = 4) -> dict:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("检索关键词不能为空。")
    top_k = max(1, min(int(top_k), 8))
    corpus = load_corpus()
    doc_texts = [f'{c["id"]} {c["title"]} {c["text"]} {c["applicability"]}' for c in corpus]
    ranked = hybrid_rank_documents(query, doc_texts)

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
        "hits": hits,
    }
