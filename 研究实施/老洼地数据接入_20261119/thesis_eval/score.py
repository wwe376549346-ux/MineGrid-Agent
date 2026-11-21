"""Score the frozen evaluation by answer, evidence, refusal, and cost."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import json
from math import ceil, isfinite
from pathlib import Path
import random
from statistics import mean, median

from thesis_eval.run import ARMS, HERE, digest, write


LABELS = {
    "vanilla": "Vanilla LLM / Prompt-only",
    "rag": "Standard RAG",
    "react": "Standard ReAct Agent",
    "ours": "Ours（Laowadi Domain Agent，评测适配）",
    "without_hybrid": "去条件化混合 RAG",
    "without_gssc": "去 GSSC",
    "without_pandapower": "去 pandapower",
    "without_verifier": "去双层核验与 Reflection",
}
LAYERS = {"single": "单点溯源", "statistics": "多柜时空统计", "abstention": "跨站与缺证拒答",
          "attribution": "多假设归因", "physics": "潮流与 N-1", "hitl": "人工补证"}
KEY_METRICS = ("task_exact", "numeric_hallucination_rate", "citation_f1", "abstention_accuracy",
               "mean_total_tokens", "p50_seconds", "p95_seconds")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def ratio(a, b):
    return a / b if b else None


def percentile(seq, p):
    return sorted(seq)[ceil(p * len(seq)) - 1] if seq else None


def accepted(claim, gold):
    if gold is None or claim["unit"] != gold["unit"]:
        return False
    expected, actual = gold["value"], claim["value"]
    if type(expected) in (int, float):
        return (type(actual) in (int, float) and isfinite(actual) and
                abs(expected - actual) <= gold["tolerance"] + 1e-10)
    return actual == expected


def score_turn(observed, gold):
    counts = Counter()
    expected = gold["fields"]
    answer = observed.get("answer")
    found = defaultdict(list)
    if answer is not None:
        for claim in answer["claims"]:
            found[claim["key"]].append(claim)
        counts["decision_correct"] = int(answer["decision"] == gold["decision"])
        is_refusal = gold["decision"] == "insufficient"
        refused = answer["decision"] == "insufficient"
        counts["refusal_eligible"] = int(is_refusal)
        counts["answerable_eligible"] = int(not is_refusal)
        counts["refusal_correct"] = int(is_refusal and refused)
        counts["false_refusal"] = int(not is_refusal and refused)
        counts["binary_refusal_correct"] = int(refused == is_refusal)
        counts["decoded"] = 1
    fields = []
    for key in sorted(found.keys() | expected.keys()):
        claims = found.get(key, [])
        ref = expected.get(key)
        expected_refs = set(ref["citations"]) if ref else set()
        predicted_refs = {citation for claim in claims for citation in claim["citations"]}
        value_ok = len(claims) == 1 and accepted(claims[0], ref)
        tp = expected_refs & predicted_refs if value_ok else set()
        fp, fn = predicted_refs - tp, expected_refs - tp
        counts.update(citation_tp=len(tp), citation_fp=len(fp), citation_fn=len(fn))
        # Count even unknown fields, redundant numbers, numbers attached to a wrong
        # slot, and numeric assertions made alongside an abstention.
        for claim in claims:
            if type(claim["value"]) in (int, float):
                counts["numeric_asserted"] += 1
                supported = accepted(claim, ref) and (not expected_refs or set(claim["citations"]) == expected_refs)
                counts["wrong_number_or_unit"] += int(not accepted(claim, ref))
                counts["hallucinated"] += int(not supported or len(claims) != 1)
        if ref and type(ref["value"]) in (int, float):
            counts["numeric_expected"] += 1
            counts["numeric_supported_correct"] += int(value_ok and not fp and not fn)
        fields.append({"key": key, "value_ok": value_ok, "citation_tp": sorted(tp),
                       "citation_fp": sorted(fp), "citation_fn": sorted(fn),
                       "gold": ref, "submitted": claims})
    counts["exact"] = int(bool(counts["decision_correct"]) and all(
        f["value_ok"] and not f["citation_fp"] and not f["citation_fn"] for f in fields))
    records = observed.get("api", [])
    complete_records = [r for r in records if r["status"] == "completed"]
    has_all_usage = bool(records) and len(records) == len(complete_records) and all(
        all(type(r.get("usage", {}).get(k)) is int for k in ("prompt_tokens", "completion_tokens", "total_tokens"))
        for r in complete_records)
    return {"status": observed["status"], "counts": dict(counts), "fields": fields,
            "expected_decision": gold["decision"], "submitted_decision": answer["decision"] if answer else None,
            "wall_seconds": observed.get("wall_seconds"),
            "usage": ({key: sum(r["usage"][key] for r in complete_records)
                       for key in ("prompt_tokens", "completion_tokens", "total_tokens")} if has_all_usage else None),
            "api_attempts": len(records), "api_errors": len(records) - len(complete_records),
            "tool_calls": sum(t["receipt"] is not None for t in observed.get("tools", [])),
            "raw_chars": sum(t.get("raw_chars", 0) for t in observed.get("tools", [])),
            "visible_chars": sum(t.get("compressed_chars", 0) for t in observed.get("tools", [])),
            "drafts": len(observed.get("drafts", []))}


def aggregate(rows):
    turns = [t for r in rows for t in r["turns"]]
    counts = Counter()
    for turn in turns:
        counts.update(turn["counts"])
    times = [t["wall_seconds"] for t in turns if t["wall_seconds"] is not None]
    usage = [t["usage"] for t in turns if t["usage"] is not None]
    return {
        "episodes_planned": len(rows), "episodes_recorded": sum(r["recorded"] for r in rows),
        "turns_planned": len(turns), "turns_decoded": counts["decoded"],
        "task_exact": ratio(sum(all(t["counts"]["exact"] for t in r["turns"]) for r in rows), len(rows)),
        "numeric_hallucination_rate": ratio(counts["hallucinated"], counts["numeric_asserted"]),
        "numeric_error_rate": ratio(counts["wrong_number_or_unit"], counts["numeric_asserted"]),
        "numeric_coverage": ratio(counts["numeric_supported_correct"], counts["numeric_expected"]),
        "citation_precision": ratio(counts["citation_tp"], counts["citation_tp"] + counts["citation_fp"]),
        "citation_recall": ratio(counts["citation_tp"], counts["citation_tp"] + counts["citation_fn"]),
        "citation_f1": ratio(2 * counts["citation_tp"], 2 * counts["citation_tp"] + counts["citation_fp"] + counts["citation_fn"]),
        "abstention_accuracy": ratio(counts["binary_refusal_correct"], counts["decoded"]),
        "negative_recall": ratio(counts["refusal_correct"], counts["refusal_eligible"]),
        "false_refusal_rate": ratio(counts["false_refusal"], counts["answerable_eligible"]),
        "mean_total_tokens": mean(u["total_tokens"] for u in usage) if usage else None,
        "mean_prompt_tokens": mean(u["prompt_tokens"] for u in usage) if usage else None,
        "mean_completion_tokens": mean(u["completion_tokens"] for u in usage) if usage else None,
        "usage_coverage": [len(usage), len(turns)],
        "p50_seconds": median(times) if times else None,
        "p95_seconds": percentile(times, 0.95),
        "status_counts": dict(Counter(t["status"] for t in turns)),
        "api_attempts": sum(t["api_attempts"] for t in turns),
        "api_errors": sum(t["api_errors"] for t in turns),
        "raw_chars": sum(t["raw_chars"] for t in turns),
        "visible_chars": sum(t["visible_chars"] for t in turns),
        "counts": dict(counts),
    }


def differences(ours, other, draws, seed=20261121):
    """Cluster bootstrap, keeping repeated case/turn/arm observations paired."""
    groups = defaultdict(list)
    other_by_key = {(r["id"], r["repeat"]): r for r in other}
    for r in ours:
        peer = other_by_key.get((r["id"], r["repeat"]))
        if peer and r["recorded"] and peer["recorded"]:
            groups[r["cluster"]].append((r, peer))
    clusters = list(groups.values())

    def deltas(pairs):
        a = aggregate([x for x, _ in pairs])
        b = aggregate([y for _, y in pairs])
        return {k: a[k] - b[k] if a[k] is not None and b[k] is not None else None for k in KEY_METRICS}

    observed = deltas([pair for group in clusters for pair in group])
    distribution = defaultdict(list)
    rng = random.Random(seed)
    if len(clusters) >= 2:
        for _ in range(draws):
            selected = rng.choices(clusters, k=len(clusters))
            for key, delta in deltas([pair for group in selected for pair in group]).items():
                if delta is not None:
                    distribution[key].append(delta)
    return {"paired_clusters": len(clusters), "paired_episodes": sum(map(len, clusters)),
            "ours_minus_arm": {key: {"estimate": value, "95pct_cluster_bootstrap": [
                percentile(distribution[key], 0.025), percentile(distribution[key], 0.975),
            ] if distribution[key] else None} for key, value in observed.items()}}


def score_matrix(records, draws):
    suite, gold = load(HERE / "cases.json"), load(HERE / "gold.json")
    settings = load(records / "run_manifest.json")
    if settings["cases_sha256"] != digest(HERE / "cases.json") or settings["gold_sha256"] != digest(HERE / "gold.json"):
        raise ValueError("运行记录与冻结题集或答案键不一致")
    expected_cases = set(settings["intended_cases"])
    rows = []
    for arm in settings["arms"]:
        for repeat in range(1, settings["repetitions"] + 1):
            for case in suite["cases"]:
                cid = case["id"]
                if cid not in expected_cases:
                    continue
                path = records / f"{repeat:02}-{arm}-{cid}.json"
                marks = gold[cid]["turns"]
                record = load(path) if path.exists() else {"turns": [{"status": "not_run"} for _ in marks]}
                if path.exists() and [record.get(k) for k in ("id", "arm", "repeat")] != [cid, arm, repeat]:
                    raise ValueError(f"记录标识不符 {path}")
                if len(record["turns"]) != len(marks):
                    raise ValueError(f"记录轮数不符 {path}")
                rows.append({"id": cid, "arm": arm, "repeat": repeat, "layer": case["layer"],
                             "cluster": case["cluster"], "recorded": path.exists(),
                             "turns": [score_turn(data, mark) for data, mark in zip(record["turns"], marks)]})
    arms = {name: [r for r in rows if r["arm"] == name] for name in settings["arms"]}
    return {"run": settings, "bootstrap_draws": draws, "rows": rows,
            "overall": {name: aggregate(group) for name, group in arms.items()},
            "by_layer": {name: {layer: aggregate([r for r in group if r["layer"] == layer])
                                for layer in LAYERS} for name, group in arms.items()},
            "paired": {name: differences(arms["ours"], group, draws) for name, group in arms.items()
                       if "ours" in arms and name != "ours"}}


def display(value, percentage=False):
    if value is None:
        return "—"
    return f"{value:.1%}" if percentage else f"{value:.2f}"


def reports(summary, output):
    keys = ("task_exact", "numeric_hallucination_rate", "citation_f1", "abstention_accuracy",
            "numeric_coverage", "mean_total_tokens", "p50_seconds", "p95_seconds")
    lines = ["# 老洼地 48 题同题评测", "",
             f'模型 {summary["run"]["model"]}，计划 {summary["run"]["repetitions"]} 轮重复。未得到可评分回答的轮次见状态分布。', "",
             "| 方法 | 记录题数 | 可解析轮数 | 整题正确 | 数值幻觉 | Citation F1 | 拒答准确 | 数值覆盖 | 平均 Token | P50 秒 | P95 秒 |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for arm, stats in summary["overall"].items():
        values = [LABELS[arm], f'{stats["episodes_recorded"]}/{stats["episodes_planned"]}',
                  f'{stats["turns_decoded"]}/{stats["turns_planned"]}']
        values += [display(stats[key], i < 5) for i, key in enumerate(keys)]
        lines.append("| " + " | ".join(values) + " |")
    lines += ["", "数值幻觉率与拒答准确率分别以数值主张和可解析回答为分母。Token 只统计 API 用量齐全的轮次。— 表示无可估值。", "",
              "逐层指标、置信区间、缺失记录、逐字段与引用错误详见 score.json。", ""]
    (output / "results.md").write_text("\n".join(lines), encoding="utf-8")
    with (output / "by_layer.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["arm", "layer", "episodes_recorded", "turns_decoded", *keys])
        for arm, groups in summary["by_layer"].items():
            for layer, stats in groups.items():
                writer.writerow([arm, layer, stats["episodes_recorded"], stats["turns_decoded"],
                                 *(stats[key] for key in keys)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", type=Path)
    parser.add_argument("--draws", type=int, default=2000)
    args = parser.parse_args()
    if args.draws < 0:
        parser.error("draws cannot be negative")
    summary = score_matrix(args.records, args.draws)
    write(args.records.parent / "score.json", summary)
    reports(summary, args.records.parent)


if __name__ == "__main__":
    main()
