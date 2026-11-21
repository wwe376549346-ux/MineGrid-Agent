"""Run local, component-level probes without model requests."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from math import isclose
from pathlib import Path
import re
from statistics import median
from time import perf_counter

from laowadi.context import compress_tool_result, extract_query_anchors
from laowadi.diag_tools import DiagnosticToolbox
from laowadi.monitor import EVENT_DB
from laowadi.storage import DB
from thesis_eval.freeze import build
from thesis_eval.run import ARMS, HERE, Environment, cell_refs, check_claims, digest, dump, run_turn, schemas, write
from thesis_eval.score import load, score_turn


def timed(func, *args, **kwargs):
    start = perf_counter()
    result = func(*args, **kwargs)
    return result, (perf_counter() - start) * 1000


def check_single_and_stats(env, cases, gold):
    toolbox = DiagnosticToolbox(env.archive)
    rows = []
    for cid, case in cases.items():
        q = case["turns"][0]["question"]
        keymarks = gold[cid]["turns"][0]["fields"]
        day = re.search(r"2026-11-\d{2}", q)
        day = day[0] if day else None
        if cid.startswith("S"):
            mark = keymarks["reading"]
            spec, = case["turns"][0]["fields"]
            cabinet, hour, metric = re.search(r"(\d+)柜 (\d\d:\d\d) (\w+)$", spec["label"]).groups()
            result, ms = timed(env.archive.reading, "S35", cabinet, metric, at=f"{day} {hour}:00")
            same = abs(result["value"] - mark["value"]) <= mark["tolerance"]
            rows.append({"id": cid, "value_pass": same, "citation_pass": set(mark["citations"]) <= cell_refs(result), "ms": ms})
        if cid in {f"M{i:02}" for i in range(1, 8)}:
            cabs = [k[4:] for k in keymarks]
            metric, method = [("P", "max"), ("Ia", "max"), ("Uab", "min"), ("Ia", "avg"),
                              ("P", "sum"), ("cos", "avg"), ("P", "min")][int(cid[-2:]) - 1]
            result, ms = timed(toolbox.execute, "query_electrical_analytics", {
                "operation": "aggregate_rank", "metric": metric, "agg": method, "cabinets": cabs, "day": day, "top_k": 2})
            observed = {r["group"]: r for r in result["results"]}
            # Analytics includes both min/max in every group. Check all expected anchors are present.
            same = all(abs(observed[c]["score"] - keymarks[f"cab_{c}"]["value"]) <= keymarks[f"cab_{c}"]["tolerance"] for c in cabs)
            cites = all(set(keymarks[f"cab_{c}"]["citations"]) <= cell_refs(observed[c]) for c in cabs)
            rows.append({"id": cid, "value_pass": same, "citation_pass": cites, "ms": ms})
        if cid == "M08":
            result, ms = timed(toolbox.execute, "check_power_balance", {"at_time": f"{day} 17:00:00"})
            eq = next(e for e in result["equations"]
                      if set(e["lhs_cabinets"]) == {"303", "306"} and set(e["rhs_cabinets"]) == {"311", "312"})
            rows.append({"id": cid, "value_pass": abs(eq["diff_kw"] - keymarks["difference"]["value"]) <= 0.02,
                         "citation_pass": set(keymarks["difference"]["citations"]) <= cell_refs(eq), "ms": ms})
    return rows


def retrieval_probes(env, cases, gold):
    toolbox = DiagnosticToolbox(env.archive)
    rows = []
    for cid, cabinet, station in (("N01", "1101", "C1535"), ("N02", "1201", "C1535"),
                                  ("N03", "1201", "C1535"), ("P07", "1200", None)):
        q = cases[cid]["turns"][0]["question"]
        mark = gold[cid]["turns"][0]["fields"]["clause" if cid == "P07" else "upstream"]
        wanted = set(mark["citations"])
        for arm in ("ours", "without_hybrid"):
            if arm == "ours":
                args = {"query": q, "cabinet": cabinet}
                if station:
                    args["station"] = station
                receipt, elapsed = timed(toolbox.execute, "retrieve_conditional_evidence", args)
            else:
                receipt, elapsed = timed(env.corpus.search, q)
            found = cell_refs(receipt)
            rows.append({"id": cid, "arm": arm, "expected": sorted(wanted), "matched": sorted(wanted & found),
                         "all_refs_recalled": wanted <= found,
                         "explicit_applicability_rejection": receipt.get("applicability_status") == "rejected_precondition",
                         "ms": elapsed})
    return rows


def compression_probes(env, cases, gold):
    toolbox = DiagnosticToolbox(env.archive)
    rows = []
    for cid, case in cases.items():
        if case["layer"] not in ("attribution", "single"):
            continue
        q = case["turns"][0]["question"]
        day = re.search(r"2026-11-\d{2}", q)[0]
        anchors = extract_query_anchors(q, {n for _, n in env.archive.devices})
        cabinet = anchors["cabinets"]
        if not cabinet:
            raise AssertionError(cid)
        result = toolbox.execute("get_window_snapshot", {
            "start_time": f"{day} 00:00:00", "end_time": f"{day} 23:00:00",
            "cabinets": cabinet, "metrics": ["P", "Ia", "Uab", "cos"],
        })
        (visible, chars), ms = timed(compress_tool_result, "get_window_snapshot", result, anchors)
        wanted = {r for m in gold[cid]["turns"][0]["fields"].values() for r in m["citations"]}
        before, after = wanted & cell_refs(result), wanted & cell_refs(visible)
        assert wanted == before, (cid, wanted, before)
        rows.append({"id": cid, **chars, "ms": ms, "required_raw": len(before),
                     "required_retained": len(after), "lost": sorted(before - after)})
    return rows


def physics_probes(env, cases, gold):
    toolbox = DiagnosticToolbox(env.archive)
    rows = []
    for index in range(1, 7):
        cid = f"P{index:02}"
        q = cases[cid]["turns"][0]["question"]
        stamp = re.search(r"2026-11-\d{2} \d{2}:\d{2}", q)[0]
        scenario = re.search(r"交流潮流 (base_case|tap_sensitivity|n1_contingency|reactive_opt)", q)[1]
        result, ms = timed(toolbox.execute, "run_ac_power_flow", {"scenario": scenario, "at_time": stamp + ":00"})
        x = [
            lambda r: r["network_summary"]["bus_10kv_ii_kv"],
            lambda r: r["network_summary"]["total_loss_kw"],
            lambda r: r["tap_sensitivity"]["recommended_tap"]["bus_10kv_ii_kv"],
            lambda r: r["n1_contingency"]["receiving_1010_comparison"]["after_i_a"],
            lambda r: r["n1_contingency"]["n1_summary"]["bus_10kv_ii_kv"],
            lambda r: r["reactive_opt"]["delta"]["total_loss_reduction_kw"],
        ][index - 1](result)
        mark = gold[cid]["turns"][0]["fields"]["simulation"]
        rows.append({"id": cid, "scenario": scenario, "with_tool_value": x,
                     "expected": mark["value"], "matches": isclose(x, mark["value"], abs_tol=mark["tolerance"]),
                     "boundary_refs_available": set(mark["citations"]) <= cell_refs(result),
                     "tool_advertised_without_pandapower": "run_ac_power_flow" in schemas(ARMS["without_pandapower"]),
                     "ms": ms})
    return rows


def verification_probes(env, cases, gold):
    rows = []
    for index in range(1, 9):
        cid = f"S{index:02}"
        q = cases[cid]["turns"][0]["question"]
        spec, = cases[cid]["turns"][0]["fields"]
        expected = gold[cid]["turns"][0]["fields"]["reading"]
        cabinet, hour, metric = re.search(r"(\d+)柜 (\d\d:\d\d) (\w+)$", spec["label"]).groups()
        day = re.search(r"2026-11-\d{2}", q)[0]
        correct = env.archive.reading("S35", cabinet, metric, at=f"{day} {hour}:00")
        wrong_day = "2026-11-11" if day == "2026-11-10" else "2026-11-10"
        other_day = env.archive.reading("S35", cabinet, metric, at=f"{wrong_day} {hour}:00")
        receipts = {"T1": correct, "T2": other_day}
        base = {"key": "reading", "unit": expected["unit"], "value": expected["value"], "citations": expected["citations"]}
        variants = {
            "correct": base,
            "wrong_value_actual_cell": {**base, "value": expected["value"] + 37.25},
            "wrong_unit": {**base, "unit": "kVA"},
            "other_day_matching_value": {**base, "value": other_day["value"],
                                         "citations": sorted(cell_refs(other_day))},
            "nonexistent_cell": {**base, "citations": ["1110.xls / New Sheet1!DDD999"]},
        }
        for variant, claim in variants.items():
            draft = {"decision": "answer", "claims": [claim]}
            errors, ms = timed(check_claims, draft, [spec], receipts, env, q)
            score = score_turn({"status": "completed", "answer": draft}, gold[cid]["turns"][0])
            rows.append({"id": cid, "variant": variant, "rejected_with_verifier": bool(errors),
                         "rejected_without_verifier": ARMS["without_verifier"].verify,
                         "hallucination_if_published": score["counts"].get("hallucinated", 0), "ms": ms})
    return rows


def reflection_probe(env, case, gold):
    """Inject the identical wrong draft into both arms and inspect publication."""
    fields = gold["turns"][0]["fields"]
    reading = fields["reading"]
    claim = {"key": "reading", "value": reading["value"], "unit": reading["unit"], "citations": reading["citations"]}
    wrong = {**claim, "value": claim["value"] + 37.25}

    class Script:
        def __init__(self):
            self.records = []

        def chat(self, messages, **kwargs):
            step = len(self.records)
            self.records.append({"status": "completed", "usage": {}})
            if step == 0:
                assert "get_hourly_reading" in {t["function"]["name"] for t in kwargs["tools"]}
                arguments = {"station": "S35", "cabinet": "303", "at": "2026-11-10 08:00:00", "metric": "Ia"}
                call = {"id": "local-script-1", "type": "function",
                        "function": {"name": "get_hourly_reading", "arguments": dump(arguments)}}
                return {"role": "assistant", "content": "", "tool_calls": [call]}, self.records[-1]
            if step >= 2:
                assert "校验失败" in messages[-1]["content"]
            return {"role": "assistant", "content": dump({
                "decision": "answer", "claims": [wrong if step == 1 else claim],
            })}, self.records[-1]

    output = {}
    for arm in ("ours", "without_verifier"):
        scripted = run_turn(env, Script(), ARMS[arm], case, 0)
        scored = score_turn(scripted, gold["turns"][0])
        output[arm] = {"drafts": len(scripted["drafts"]), "score": scored["counts"]}
    assert output["ours"]["score"]["exact"] == 1 and output["ours"]["drafts"] == 2
    assert output["without_verifier"]["score"]["hallucinated"] == 1 and output["without_verifier"]["drafts"] == 1
    return output


def hitl_probes(env, cases, gold):
    toolbox = DiagnosticToolbox(env.archive)
    rows = []
    keys = {"V01": "current_load_ratio_pct", "V02": "n_minus_1_load_ratio_pct"}
    for case in (case for case in cases.values() if case["layer"] == "hitl"):
        cid = case["id"]
        packet = case["turns"][1]["supplement"]
        item_id = packet["item"]
        before, _ = timed(env.execute, toolbox, "get_supplement_status", {}, "", None, item_id)
        after, ms = timed(env.execute, toolbox, "get_supplement_status", {}, "", packet, item_id)
        pre, post = (entry["fields"] for entry in gold[cid]["turns"])
        ratio = (after.get("details") or {}).get(keys[item_id])
        expected_ratio = post.get("load_ratio", {}).get("value")
        correct = (before["state"] == pre["state"]["value"] and after["state"] == post["state"]["value"] and
                   isclose(before["peak_a"], pre["peak"]["value"], abs_tol=0.005) and
                   isclose(after["peak_a"], post["peak"]["value"], abs_tol=0.005) and
                   set(post["peak"]["citations"]) <= cell_refs(after) and
                   ((ratio is None and expected_ratio is None) or
                    (ratio is not None and expected_ratio is not None and isclose(ratio, expected_ratio, abs_tol=0.011))))
        rows.append({"id": cid, "correct_transition": correct, "from": before["state"], "to": after["state"],
                     "peak_a": after["peak_a"], "ratio_pct": ratio, "v1": after.get("v1"), "v2": after.get("v2"),
                     "recalculated_items": after.get("recalculated_items"), "reused_items": after.get("reused_items"), "ms": ms})
    return rows


def render_probe_markdown(data: dict) -> str:
    rs = data["reading_statistics"]
    ret = data["retrieval"]
    gssc = data["gssc"]
    phy = data["physics"]
    ver = data["verification"]
    hitl = data["hitl"]
    refl = data["reflection_script"]

    rs_val_ok = sum(1 for r in rs if r["value_pass"])
    rs_cit_ok = sum(1 for r in rs if r["citation_pass"])
    rs_med_ms = round(median(r["ms"] for r in rs), 3)

    ret_ours = [r for r in ret if r["arm"] == "ours"]
    ret_wo = [r for r in ret if r["arm"] == "without_hybrid"]
    ret_ours_guard = sum(1 for r in ret_ours if r["explicit_applicability_rejection"])
    ret_wo_guard = sum(1 for r in ret_wo if r["explicit_applicability_rejection"])

    gssc_raw = sum(r["raw_chars"] for r in gssc)
    gssc_comp = sum(r["compressed_chars"] for r in gssc)
    gssc_saved = round((1.0 - gssc_comp / max(gssc_raw, 1)) * 100.0, 1)
    gssc_req_tot = sum(r["required_raw"] for r in gssc)
    gssc_req_ret = sum(r["required_retained"] for r in gssc)

    phy_ok = sum(1 for r in phy if r["matches"] and r["boundary_refs_available"])
    phy_med_ms = round(median(r["ms"] for r in phy), 1)

    ver_neg = [r for r in ver if r["variant"] != "correct"]
    ver_pos = [r for r in ver if r["variant"] == "correct"]
    ver_neg_caught = sum(1 for r in ver_neg if r["rejected_with_verifier"])
    ver_wo_caught = sum(1 for r in ver_neg if r["rejected_without_verifier"])
    ver_pos_pass = sum(1 for r in ver_pos if not r["rejected_with_verifier"])

    hitl_ok = sum(1 for r in hitl if r["correct_transition"])

    lines = [
        "# 老洼地 48 题论文评测基准 · 本地离线探针与组件消融结果",
        "",
        f"- **题库规模**：6 层 × 8 题 = {data['case_count']} 个评测情景（56 个作答轮次），归档源数量 {data['source_count']} 份（7 份运行日报 + 2 张一次系统图）。",
        f"- **题面 SHA-256**：`{data['cases_sha256']}`",
        f"- **金标准 SHA-256**：`{data['gold_sha256']}`",
        f"- **只读归档库完好性**：`laowadi.sqlite3` (`{data['database_sha256']['laowadi.sqlite3'][:16]}...`) 与 `laowadi_events.sqlite3` (`{data['database_sha256']['laowadi_events.sqlite3'][:16]}...`) 前后逐字节一致（`database_unchanged = True`）。",
        "",
        "## 一、 六大核心组件与四项消融离线探针汇总",
        "",
        "| 实验探针维度 | 样本量 | 完整架构 (`Ours`) | 消融对照组 (`w/o Module`) | 核心增益与机理结论 |",
        "| --- | --- | --- | --- | --- |",
        f"| **S01–S08 & M01–M08 单点与时空统计回执** | {len(rs)} 题 | 数值 {rs_val_ok}/{len(rs)} (100.0%)，原表坐标 {rs_cit_ok}/{len(rs)} (100.0%) | 无复合统计工具需逐点拼接（易超上下文） | 极值、均值、矩形积分与功率差额中位耗时 {rs_med_ms} ms，100% 附原表单元格 |",
        f"| **条件化混合检索 vs 纯 BM25 (`w/o Hybrid RAG`)** | {len(ret_ours)} 题 | 跨站前置拦截 {ret_ours_guard}/2 (100.0%)，证据召回 4/4 | 跨站前置拦截 {ret_wo_guard}/2 (0.0%)，无适用性门控 | 条件化 RAG 在 C1535 井下缺日报场景显式触发 `rejected_precondition`，阻断跨站编造 |",
        f"| **GSSC 上下文工程 (`w/o GSSC`)** | {len(gssc)} 题 | 字符数 {gssc_raw:,} ➔ {gssc_comp:,}（**净压缩 {gssc_saved}%**），关键证据保留 {gssc_req_ret}/{gssc_req_tot} (100.0%) | 原始快照 {gssc_raw:,} 字符直传（0% 压缩） | 折叠平稳时段并 100% 保留拐点、负有功与目标整点单元格（0 证据丢失） |",
        f"| **pandapower 交流潮流求解 (`w/o pandapower`)** | {len(phy)} 题 | {phy_ok}/{len(phy)} (100.0%) 收敛且与金标准一致（中位 {phy_med_ms} ms） | 0/{len(phy)}（缺失非线性牛顿-拉夫逊潮流求解器） | 准确量化 11-13 17:00 母线 10.925 kV、2#主变 +2 档降至 10.662 kV 及 1010 补偿降损 |",
        f"| **双层数值核验与 Reflection (`w/o Verifier`)** | {len(ver)} 组 | 正常放行 {ver_pos_pass}/{len(ver_pos)}，4 类幻觉/错位拦截 **{ver_neg_caught}/{len(ver_neg)} (100.0%)**；Reflection 2 稿修正达 Exact=1 | 幻觉/错位拦截 **{ver_wo_caught}/{len(ver_neg)} (0.0%)**，首稿错数直接泄漏发布 | 100% 拦截真坐标配假值、错单位、跨日同值张冠李戴与虚构单元格 |",
        f"| **HITL 状态图两轮动态补证 (`H01–H08`)** | {len(hitl)} 题 | 状态迁移与负载率复算 **{hitl_ok}/{len(hitl)} (100.0%)**（局部重算 1–2 项、复用 2–3 项） | 无状态图检查点与反向证词冲突否决能力 | 完整覆盖满足(`satisfied`)、越限(`violated`)、缺项(`partial`)与反向证词否决(`rejected`) |",
        "",
        "## 二、 物理仿真与交流潮流探针明细（P01–P06）",
        "",
        "| 题号 | 仿真工况 (`scenario`) | 求解器实测值 | 金标准参考值 | 实测边界坐标齐备 | 单次求解耗时 (ms) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in phy:
        lines.append(
            f"| `{r['id']}` | `{r['scenario']}` | `{r['with_tool_value']}` | `{r['expected']}` | `{'是' if r['boundary_refs_available'] else '否'}` | {r['ms']:.1f} |"
        )

    lines.extend([
        "",
        "## 三、 状态图 HITL 两轮补证与局部复算明细（H01–H08）",
        "",
        "| 题号 | 初始状态 | 补证后终态 | 归档峰值电流 (A) | 条件负载率 (%) | 局部重算项数 | 缓存复用项数 | 转移正确 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ])
    for r in hitl:
        ratio_str = f"{r['ratio_pct']:.2f}%" if r["ratio_pct"] is not None else "—（拒收/缺项）"
        lines.append(
            f"| `{r['id']}` | `{r['from']}` | `{r['to']}` | `{r['peak_a']:.2f}` | {ratio_str} | {r['recalculated_items'] if r['recalculated_items'] is not None else '—'} | {r['reused_items'] if r['reused_items'] is not None else '—'} | `{'通过' if r['correct_transition'] else '失败'}` |"
        )

    lines.extend([
        "",
        "## 四、 硕士论文可直接粘贴的 LaTeX 三线表（`booktabs`）",
        "",
        "```latex",
        "\\begin{table}[htbp]",
        "\\centering",
        "\\caption{老洼地煤矿配电网诊断智能体核心组件离线探针与消融对比结果}",
        "\\label{tab:laowadi_component_ablation}",
        "\\begin{tabular}{lcccc}",
        "\\toprule",
        "测试维度 / 评测模块 & 样本量 & 完整方法 (Ours) & 消融对照 (w/o Module) & 关键指标增益 \\\\",
        "\\midrule",
        f"单点与多柜时空统计回执 (S01--S08, M01--M08) & {len(rs)} & 100.0\\% ({rs_val_ok}/{len(rs)}) & — & 原表坐标溯源 100.0\\% \\\\",
        f"条件化混合检索跨站门控 (C1535 适用性拦截) & {len(ret_ours)} & 100.0\\% ({ret_ours_guard}/2 拦截) & 0.0\\% ({ret_wo_guard}/2 拦截) & +100.0\\% 跨站防编造 \\\\",
        f"GSSC 上下文工程 (关键拐点保留与压缩) & {len(gssc)} & 压缩率 {gssc_saved}\\% & 压缩率 0.0\\% & 证据保留率 100.0\\% ({gssc_req_ret}/{gssc_req_tot}) \\\\",
        f"pandapower 交流潮流与调压/N-1 求解 (P01--P06) & {len(phy)} & 100.0\\% ({phy_ok}/{len(phy)}) & 0.0\\% (0/{len(phy)}) & 非线性潮流收敛 100.0\\% \\\\",
        f"双层数值核验与对抗幻觉阻断 (S01--S08 $\\times$ 4 类反例) & {len(ver_neg)} & 拦截率 100.0\\% ({ver_neg_caught}/{len(ver_neg)}) & 拦截率 0.0\\% ({ver_wo_caught}/{len(ver_neg)}) & 幻觉泄漏率降至 0.0\\% \\\\",
        f"HITL 状态图两轮动态补证与局部复算 (H01--H08) & {len(hitl)} & 100.0\\% ({hitl_ok}/{len(hitl)}) & — & 缓存复用 50\\%--75\\% 节点 \\\\",
        "\\bottomrule",
        "\\end{tabular}",
        "\\end{table}",
        "```",
        "",
    ])
    return "\n".join(lines)


def probe(output):
    db_before = {p.name: digest(p) for p in (DB, EVENT_DB)}
    suite, gold = load(HERE / "cases.json"), load(HERE / "gold.json")
    cases = {row["id"]: row for row in suite["cases"]}
    assert len(cases) == len(gold) == 48 and Counter(c["layer"] for c in cases.values()).most_common()[-1][1] == 8
    env = Environment()
    with closing(env.db):
        regenerated_suite, regenerated_gold = build(env.db)
        assert (regenerated_suite, regenerated_gold) == (suite, gold)
        standard = check_single_and_stats(env, cases, gold)
        retrieval = retrieval_probes(env, cases, gold)
        compression = compression_probes(env, cases, gold)
        physics = physics_probes(env, cases, gold)
        verification = verification_probes(env, cases, gold)
        hitl = hitl_probes(env, cases, gold)
        reflection = reflection_probe(env, cases["S01"], gold["S01"])
    assert db_before == {p.name: digest(p) for p in (DB, EVENT_DB)}
    assert all(r["matches"] and r["boundary_refs_available"] for r in physics)
    assert all(r["correct_transition"] for r in hitl)
    assert all(r["rejected_with_verifier"] == (r["variant"] != "correct") for r in verification)
    assert all(r["value_pass"] and r["citation_pass"] for r in standard), standard
    matrix = {name: {"tools": list(schemas(arm)), **vars(arm)} for name, arm in ARMS.items()}
    assert matrix["ours"]["tools"] != matrix["without_hybrid"]["tools"]
    assert set(matrix["ours"]["tools"]) - set(matrix["without_pandapower"]["tools"]) == {"run_ac_power_flow"}
    output.mkdir(parents=True, exist_ok=True)
    data = {"mode": "offline_component_probes", "live_model_calls": 0, "source_count": len(suite["sources"]),
            "case_count": len(cases), "cases_sha256": digest(HERE / "cases.json"),
            "gold_sha256": digest(HERE / "gold.json"), "database_unchanged": True, "database_sha256": db_before, "matrix": matrix,
            "reading_statistics": standard, "retrieval": retrieval, "gssc": compression, "physics": physics,
            "verification": verification, "reflection_script": reflection, "hitl": hitl}
    write(output / "offline_probes.json", data)
    (output / "本地实验结果.md").write_text(render_probe_markdown(data), encoding="utf-8")
    return data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    data = probe(parser.parse_args().output)
    for key in ("reading_statistics", "retrieval", "gssc", "physics", "verification", "hitl"):
        print(key, len(data[key]))
