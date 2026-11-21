"""Render structured tool results selected by the model using local templates."""

from __future__ import annotations

from laowadi.storage import METRICS
from laowadi.verifier import _cells, _standards, bind_tool_fact, build_observations


_AGGREGATIONS = {
    "sum": "合计", "avg": "算术平均", "min": "最小值", "max": "最大值",
    "std": "总体标准差", "range": "极差", "count": "有效样本数",
}


def _result_scope(result):
    spec = result["query_spec"]
    start, end = spec["effective_date_range"]
    return start if start == end else f"{start} 至 {end}"


def capacity_fact(step, index):
    item = step["result"]["items"][index]
    judgment = item["conditional_judgment"]
    fields = {
        "current_load_ratio_pct": ("进线电流负载率", "%"),
        "transformer_load_ratio_pct": ("主变负载率", "%"),
        "n1_load_ratio_pct": ("转供负载率", "%"),
        "n1_margin_pct": ("转供载流裕度", "%"),
    }
    figures = "、".join(
        f"{label} {judgment[key]}{unit}" for key, (label, unit) in fields.items()
        if key in judgment and isinstance(judgment[key], (int, float))
    )
    text = f'条件复算 {item["id"]}（{item["time_window"]}）'
    if figures:
        text += f'。按本次输入参数计算，{figures}。'
    else:
        text += "。存在带条件的工具回执，须复核输入参数和资料。"
    return bind_tool_fact(
        {"statement": text, "evidence": sorted(_cells(item["confirmed_local_facts"]))[:6]},
        step, "items", index, kind="simulation",
    )


def render_tool_facts(step, *, cell_limit=6):
    result = step["result"]
    kind = result.get("kind")

    def fact(text, *path, category="calculation"):
        source_value = result
        for key in path:
            source_value = source_value[key]
        cells = sorted(_cells(source_value))
        return bind_tool_fact({"statement": text, "evidence": cells[:cell_limit]}, step, *path, kind=category)

    if kind == "electrical_analytics":
        spec = result["query_spec"]
        scope = _result_scope(result)
        if result["operation"] == "aggregate_rank":
            metric = METRICS[spec["metric"]][0]
            method = _AGGREGATIONS[spec["agg"]]
            if spec["metric"] == "P" and spec["agg"] == "sum":
                method = "有功整点矩形积分近似（ΣP × 1 h）"
            groups = {"cabinet": "按柜", "day": "按日", "hour": "按钟点"}[spec["group_by"]]
            combine = "各指定柜同期求和后" if spec.get("combine_cabinets_per_hour") else ""
            unit = "点" if spec["agg"] == "count" else spec["agg_unit"]
            facts = [
                fact(
                    f'{scope}，{combine}{groups}计算{metric}{method}。'
                    f'第 {row["rank"]} 名 {row["group"] if spec["group_by"] == "cabinet" else ""} '
                    f'{row["label"]} 为 {row["score"]} {unit}'
                    f'（有效样本 {row["stats"]["count"]} 个）。',
                    "results", idx,
                )
                for idx, row in enumerate(result["results"])
            ]
            comparison = result.get("comparison")
            if comparison and comparison["diff"] > 0:
                facts.append(fact(
                    f'{scope}按相同{method}口径，{comparison["higher"]}比'
                    f'{comparison["lower"]}高 {comparison["diff"]} {unit}。',
                ))
            return facts
        conditions = " 且 ".join(f'{c["metric"]} {c["op"]} {c["value"]}' for c in spec["conditions"])
        hits = [fact(
            f'{scope}，归档中符合「{conditions}」的柜时组合共 {result["total_matched_rows"]} 个。',
            "total_matched_rows",
        )]
        for index, row in enumerate(result["results"]):
            readings = "、".join(
                f'{metric}={reading["value"]} {METRICS[metric][1]}'.rstrip()
                for metric, reading in row["readings"].items()
            )
            hits.append(fact(f'{row["time"]} {row["cabinet"]} 柜筛选命中。{readings}。',
                             "results", index))
        return hits
    if kind == "station_health_overview":
        return [
            fact(f'归档筛查发现 · {row["title"]}。{row["summary"]}', "findings", idx, category="screening")
            for idx, row in enumerate(result["findings"])
        ]
    if kind == "cabinet_health_profile":
        device = result["device"]
        cabinet = f'{device["station_name"]} {device["number"]} 柜'
        if not result["has_hourly_readings"]:
            return [fact(f'{cabinet}。{result["note"]}', "note", category="coverage")]
        return [
            fact(
                f'{cabinet}归档周期内 {METRICS[metric][0]}均值 {stats["avg"]} {stats["unit"]}'
                f'（{stats["valid_hours"]} 个有效整点）。最低 {stats["min"]}（{stats["min_time"]}），'
                f'最高 {stats["max"]}（{stats["max_time"]}）。',
                "metrics_7d", metric,
            )
            for metric, stats in result["metrics_7d"].items()
        ]
    if kind == "power_balance":
        return [
            fact(
                f'{result["time"]} 功率差额 ({"+".join(eq["lhs_cabinets"])}) − '
                f'({"+".join(eq["rhs_cabinets"])}) = {eq["diff_kw"]} kW。',
                "equations", idx,
            )
            for idx, eq in enumerate(result["equations"]) if eq["inputs_complete"]
        ]
    if kind == "diurnal_baseline":
        return [
            fact(
                f'{row["time"]} 同期进线总有功 {row["total_p_kw"]} kW，'
                f'归档同钟点中位数 {row["baseline_median_kw"]} kW，偏差 {row["deviation_kw"]:+} kW。',
                "day_comparison", "hours", idx,
            )
            for idx, row in enumerate((result.get("day_comparison") or {}).get("hours", []))
        ]
    if kind == "conditional_capacity":
        return [
            capacity_fact(step, idx)
            for idx, item in enumerate(result.get("items", []))
            if isinstance(item.get("conditional_judgment"), dict)
        ]
    if kind == "electrical_simulation":
        if result["status"] == "warning":
            return [fact(result["message"], "message", category="coverage")]
        if result["operation"] == "reactive_compensation":
            return [fact(
                f'{result["scope_day"]}，{result["cabinet"]} 柜按功率因数目标 {result["target_cos"]} '
                f'测算无功补偿需求 P95 为 {result["required_compensation_kvar"]["p95_kvar"]} kVar'
                f'，峰值 {result["required_compensation_kvar"]["peak_kvar"]} kVar'
                f'（{result["loaded_hours"]} 个正向带载整点，参数化测算）。'
                f'按同一公式估计，低于目标时段的线路铜损平均相对变化为 '
                f'−{result["expected_benefits_on_below_hours"]["avg_line_loss_reduction_pct"]}%。',
                category="simulation",
            )]
        worst = result["worst_current_hour"]
        facts = [fact(
            f'{result["scope_day"]}，按 {result["outage_cabinet"]["number"]} 至 '
            f'{result["receiving_cabinet"]["number"]} 的假定转供场景，'
            f'合成电流上限峰值 {worst["combined_Ia_algebraic_a"]} A（{worst["time"]}），'
            f'同整点合成视在功率 {worst["combined_S_kva"]} kVA。',
            "worst_current_hour", category="simulation",
        )]
        if capacity := result.get("capacity_evaluation"):
            facts.append(fact(
                f'按输入额定电流 {capacity["rated_current_a"]} A 与降容系数 {capacity["derating_factor"]} 计算，'
                f'有效限值 {capacity["effective_limit_a"]} A，假定转供负载率 {capacity["n1_load_ratio_pct"]}%。',
                category="simulation",
            ))
        return facts
    if kind == "ac_power_flow":
        facts = [fact(
            f'{result["at_time"]} 断面（{result["scenario"]}），pandapower 等值交流潮流计算收敛，'
            f'仿真有功总网损 {result["network_summary"]["total_loss_kw"]} kW。',
            category="simulation",
        )]
        if sensitivity := result.get("tap_sensitivity"):
            if tap := sensitivity.get("recommended_tap"):
                facts.append(fact(
                    f'分接头灵敏度仿真候选 {tap["tap_label"]}，II 段母线仿真电压 '
                    f'{tap["bus_10kv_ii_kv"]} kV。现场实际档位与适用限值待核。',
                    "tap_sensitivity", category="simulation",
                ))
        if contingency := result.get("n1_contingency"):
            receiving = contingency.get("receiving_1010_comparison") or {}
            if receiving.get("after_i_a") is not None:
                facts.append(fact(
                    f'假定场景 {contingency["description"]}。仿真受电回路电流 '
                    f'{receiving["after_i_a"]} A，井下最低仿真电压 {receiving["min_underground_kv"]} kV。',
                    "n1_contingency", category="simulation",
                ))
        if reactive := result.get("reactive_opt"):
            delta = reactive["delta"]
            facts.append(fact(
                f'{reactive["target_cabinet"]} 柜末端 {reactive["qc_kvar"]} kVar 假定补偿场景，'
                f'受端仿真电压变化 {delta["receiving_u_boost_kv"]:+} kV，'
                f'总有功网损减少 {delta["total_loss_reduction_kw"]} kW。',
                "reactive_opt", category="simulation",
            ))
        return facts
    if kind == "conditional_rag":
        return [
            fact(warning, "applicability_warnings", idx, category="coverage")
            for idx, warning in enumerate(result.get("applicability_warnings", []))
        ]
    return []


def result_catalog(trace):
    return {
        f"T{step_no}.{index}": fact
        for step_no, step in enumerate(trace, 1)
        if step["result"].get("status") in ("ok", "warning")
        for index, fact in enumerate(render_tool_facts(step), 1)
    }


def _selected_list(selection, key, limit):
    values = selection.get(key, [])
    if not isinstance(values, list) or len(values) > limit:
        raise ValueError(key)
    if any(not isinstance(value, str) or not value.strip() or len(value) > 200 for value in values):
        raise ValueError(key)
    return list(dict.fromkeys(values))


def build_selected_report(archive, selection, trace, catalog):
    if not isinstance(selection, dict) or selection.keys() - {"cells", "result_ids", "standard_ids"}:
        raise ValueError("selection_schema")
    cells = _selected_list(selection, "cells", 12)
    keys = _selected_list(selection, "result_ids", 8)
    clauses = _selected_list(selection, "standard_ids", 5)
    observations = build_observations(archive, cells, trace)
    if observations is None:
        raise ValueError("unknown_or_missing_cell")
    if any(key not in catalog for key in keys):
        raise ValueError("unknown_result_id")
    standards = _standards(trace)
    if any(cid not in standards for cid in clauses):
        raise ValueError("unknown_standard_id")
    facts = [catalog[key] for key in keys] + observations
    if not facts:
        raise ValueError("missing_fact_evidence")
    return {
        "confirmed_facts": facts,
        "missing_evidence": ["同期运行日志、开关变位和保护 SOE 记录。"],
        "cited_standards": [{"id": cid} for cid in clauses],
    }

