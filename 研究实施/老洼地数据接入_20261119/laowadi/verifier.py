"""Verify archive observations and publish evidence-bound diagnostic reports."""

from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
import json
import re

from laowadi.storage import METRICS


EVIDENCE_POLICY = "archive-evidence-v1"

_CELL_RE = re.compile(r"\b[^/\r\n]+?\.xls\s*/\s*[^!\r\n]+![A-Z]{1,3}\d+\b", re.I)
_CELL_PARTS = re.compile(r"([^/\r\n]+?\.xls) / ([^!\r\n]+)!([A-Z]{1,3}\d+)", re.I)
_NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_UNIT_NUM_RE = re.compile(rf"({_NUMBER})\s*(kVar|kVA|kWh|kW|kV|A)(?![A-Za-z])", re.I)
_COS_NUM_RE = re.compile(rf"(?:cos|功率因数)\s*[=为是:：]?\s*({_NUMBER})", re.I)
_TIMESTAMP = r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?"
_READING_HEADER = re.compile(rf"(?P<time>{_TIMESTAMP})\s+(?P<cabinet>[\w-]+?)\s*柜\s*(?:实测\s*[:：]\s*)?")
_METRIC_PATTERN = {
    "P": r"(?:有功(?:功率)?|P)",
    "Ia": r"(?:电流|Ia)",
    "Uab": r"(?:线电压|电压|Uab)",
    "cos": r"(?:功率因数|cos)",
}
_READING_PART = re.compile(
    rf"(?P<metric>{'|'.join(_METRIC_PATTERN.values())})\s*(?:为|达|是|[:：=])?\s*"
    rf"(?P<value>{_NUMBER})\s*(?P<unit>kVar|kVA|kWh|kW|kV|A)?", re.I,
)
_VERIFIED_CHECKS = [
    "核对同一设备、同一时段的开关变位和保护 SOE 记录。",
    "核对现场设备台账、计量口径及参数来源；由矿方人员复核候选原因。",
]
_REVIEW_TOPICS = {
    "metering": "待核对计量变比、相序和极性",
    "power_quality": "待核对母线电压与同期无功补偿投切",
    "system_balance": "待核对运行方式与多测点采样同步性",
    "diurnal_baseline": "待核对班次负荷与同钟点基线差异",
}


def _normalize_cell(text: str) -> str:
    return re.sub(r"\s*/\s*", " / ", text.strip())


def _cells(obj) -> set[str]:
    """Collect coordinates from structured tool fields, never from arbitrary prose."""
    if isinstance(obj, dict):
        refs = set()
        if {"file", "sheet", "cell"} <= obj.keys() and isinstance(obj["cell"], str):
            ref = f'{obj["file"]} / {obj["sheet"]}!{obj["cell"]}'
            if _CELL_PARTS.fullmatch(ref):
                refs.add(ref)
        for key, value in obj.items():
            if isinstance(value, (dict, list)):
                refs.update(_cells(value))
            elif (
                isinstance(value, str)
                and (key == "cell" or key.endswith("_cell"))
                and _CELL_PARTS.fullmatch(_normalize_cell(value))
            ):
                refs.add(_normalize_cell(value))
        return refs
    if isinstance(obj, list):
        refs = set()
        for item in obj:
            if isinstance(item, str) and _CELL_PARTS.fullmatch(_normalize_cell(item)):
                refs.add(_normalize_cell(item))
            elif isinstance(item, (list, dict)):
                refs.update(_cells(item))
        return refs
    return set()


def _standards(tool_trace):
    found = {}
    for step in tool_trace:
        result = step.get("result") or {}
        if result.get("kind") == "standard_search":
            entries = result.get("hits", [])
        elif result.get("kind") == "conditional_rag":
            entries = result.get("standard_clauses", [])
        else:
            continue
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                found.setdefault(entry["id"], entry)
    return found


def _tool_cells(tool_trace):
    return set().union(*(
        _cells(result) for step in tool_trace
        if isinstance(result := step.get("result"), dict) and result.get("status", "ok") == "ok"
    ))


def _lookup_cell_db_record(archive, cell_ref: str, *, footer=False) -> dict | None:
    match = _CELL_PARTS.fullmatch(cell_ref)
    if match is None:
        return None
    if footer:
        rows = archive.db.execute(
            """
            SELECT s.station, s.cabinet, s.day, s.metric, s.statistic, s.value, rep.sha256
            FROM summary s JOIN report rep ON rep.day = s.day
            WHERE rep.filename = ? AND rep.sheet = ? AND s.source_col || s.source_row = ?
            LIMIT 2
            """, match.groups(),
        ).fetchall()
        return dict(rows[0]) if len(rows) == 1 else None
    row = archive.db.execute(
        """
        SELECT r.station, r.cabinet, r.time, r.metric, r.value, rep.sha256
        FROM reading r JOIN report rep ON rep.day = r.day
        WHERE rep.filename = ? AND rep.sheet = ? AND r.source_col || r.source_row = ?
        LIMIT 2
        """,
        match.groups(),
    ).fetchall()
    if len(row) != 1:
        return None
    record = dict(row[0])
    record["unit"] = METRICS[record["metric"]][1]
    return record


def observation_fact(cell, record):
    unit = f' {record["unit"]}' if record["unit"] else ""
    return {
        "statement": (
            f'{record["time"]} {record["cabinet"]} 柜实测 '
            f'{record["metric"]}={record["value"]}{unit}。'
        ),
        "evidence": [cell],
        "source": {"kind": "measurement", "cell": cell, **record},
    }


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def bind_tool_fact(fact, step, *path, kind="calculation"):
    """Attach source metadata at the local statement's construction site."""
    source = {
        "kind": kind,
        "tool": step["tool"],
        "arguments": step.get("arguments") or {},
        "result_sha256": _digest(step["result"]),
        "result_path": list(path),
    }
    result = step["result"]
    if "query_spec" in result:
        source["query_spec"] = result["query_spec"]
    if "formula" in result:
        source["formula"] = result["formula"]
    value = _lookup_path(result, path)
    if kind == "screening" and isinstance(value, dict) and value.get("dimension") in _REVIEW_TOPICS:
        source["dimension"] = value["dimension"]
    if kind == "simulation" and isinstance(value, dict) and "conditional_judgment" in value:
        source["calculation_status"] = value["status"]
    source["claim_sha256"] = _digest([fact["statement"], fact["evidence"], source])
    return {**fact, "source": source}


def _lookup_path(result, path):
    try:
        for key in path:
            result = result[key]
    except (KeyError, IndexError, TypeError):
        return None
    return result


def _legacy_readings(statement):
    """Read exact standalone observations for direct verifier callers."""
    header = _READING_HEADER.match(statement)
    if header is None:
        return []
    parts = re.split(r"[,，、]\s*", re.sub(r"(?<=\d),(?=\d{3}(?:[.,]\d|[^\d]|$))", "\x00", statement[header.end():].strip().rstrip("。")))
    # Preserve thousands separators while splitting independent readings.
    claims = []
    time = header["time"].replace("T", " ")
    if len(time) == 16:
        time += ":00"
    for part in parts:
        match = _READING_PART.fullmatch(part.replace("\x00", ",").strip())
        if match is None:
            return []
        metric = next(key for key, regex in _METRIC_PATTERN.items() if re.fullmatch(regex, match["metric"], re.I))
        unit = match["unit"] or ""
        if unit.lower() != METRICS[metric][1].lower():
            return []
        claims.append((header["cabinet"], time, metric, Decimal(match["value"].replace(",", ""))))
    return claims


def _check_observations(statement, refs, records):
    claims = _legacy_readings(statement)
    if not claims:
        return False, len(list(_UNIT_NUM_RE.finditer(statement))) + len(list(_COS_NUM_RE.finditer(statement)))
    values = {
        (r["cabinet"], r["time"], r["metric"], Decimal(str(r["value"])))
        for c in refs if (r := records.get(c)) is not None and r["value"] is not None
    }
    return len(refs) == len(values) == len(claims) and values == set(claims), len(claims)


def _verify(archive, report, trace, *, generated):
    tool_cells = _tool_cells(trace)
    standards = _standards(trace)
    result_index = {
        (step["tool"], _digest(step.get("arguments") or {}), _digest(step["result"])): step
        for step in trace if isinstance(step.get("result"), dict)
    } if generated else {}
    facts = report.get("confirmed_facts") or []
    verified_cells, bad_cells = set(), set()
    cell_cache, footer_cache = {}, {}
    found_cells = set()
    errors, bad_values, bad_sections = [], [], []
    checked = checked_ok = 0
    tool_checked = tool_checked_ok = 0

    for idx, fact in enumerate(facts, 1):
        if not isinstance(fact, dict) or not isinstance(fact.get("statement"), str) or not fact["statement"].strip():
            errors.append({"fact_index": idx, "reason": "invalid_fact"})
            continue
        statement = fact["statement"]
        evidence = fact.get("evidence") or []
        if isinstance(evidence, str):
            evidence = [evidence]
        if not isinstance(evidence, list) or any(not isinstance(ref, str) for ref in evidence):
            errors.append({"fact_index": idx, "reason": "invalid_evidence"})
            continue
        refs = list(dict.fromkeys(_normalize_cell(ref) for ref in evidence))

        source = (fact.get("source") or {}) if generated else {}
        allowed = tool_cells
        valid_source = False
        footer = source.get("kind") == "daily_footer"
        records = footer_cache if footer else cell_cache
        if source.get("kind") in ("calculation", "simulation", "screening", "coverage", "daily_footer"):
            key = (source["tool"], _digest(source["arguments"]), source["result_sha256"])
            step = result_index.get(key)
            value = _lookup_path(step["result"], source["result_path"]) if step else None
            binding = {k: v for k, v in source.items() if k != "claim_sha256"}
            valid_source = bool(
                step and value is not None and step["result"].get("status") in ("ok", "warning")
                and source.get("claim_sha256") == _digest([statement, evidence, binding])
            )
            allowed = _cells(value)
        for ref in refs:
            if ref not in records:
                records[ref] = _lookup_cell_db_record(archive, ref, footer=footer)
            if records[ref] is not None:
                found_cells.add(ref)
            if ref in allowed and records[ref] is not None:
                verified_cells.add(ref)
            else:
                bad_cells.add(ref)
        if source.get("kind") == "measurement":
            cell = source.get("cell")
            row = cell_cache.get(cell)
            valid_fact = (
                refs == [cell] and row is not None and row["value"] is not None
                and fact == observation_fact(cell, row)
            )
            n = 1
        elif source:
            valid_fact, n = valid_source, 0
            tool_checked += 1
        else:
            valid_fact, n = _check_observations(statement, refs, cell_cache)
        checked += n
        if valid_fact and all(ref in allowed and records[ref] is not None for ref in refs):
            checked_ok += n
            tool_checked_ok += int(bool(source) and source.get("kind") != "measurement")
        else:
            reason = "unsupported_or_mismatched_fact" if refs else "missing_fact_evidence"
            errors.append({"fact_index": idx, "reason": reason})
            if n:
                bad_values.append({"fact_index": idx, "stated": statement, "reason": reason, "cited_cells": refs})
    cited_stds = {
        str(item.get("id", "")) for item in report.get("cited_standards") or [] if isinstance(item, dict)
    }
    bad_stds = cited_stds - standards.keys()
    if not generated:
        safe_summaries = {f["statement"] for f in facts if isinstance(f, dict) and isinstance(f.get("statement"), str)}
        summary = report.get("summary")
        if summary and (not isinstance(summary, str) or summary not in safe_summaries):
            bad_sections.append("summary")
        for field in ("hypotheses", "hypothesis_trace", "diagnostic_claims", "field_sop", "missing_evidence"):
            if report.get(field):
                bad_sections.append(field)
        if report.get("cited_standards"):
            bad_sections.append("cited_standards")
    total = len(verified_cells | bad_cells) + len(cited_stds)
    grounded = len(verified_cells - bad_cells) + len(cited_stds - bad_stds)
    return {
        "policy": EVIDENCE_POLICY,
        "status": "verified" if facts and not (errors or bad_cells or bad_stds or bad_sections) else "warning",
        "tool_cells_available": len(tool_cells),
        "cited_cells_total": len(verified_cells | bad_cells),
        "verified_cells": sorted(verified_cells - bad_cells),
        "ungrounded_cells": sorted(bad_cells),
        "ungrounded_but_in_db": sorted(bad_cells & found_cells),
        "cited_standards_total": len(cited_stds),
        "verified_standards": sorted(cited_stds - bad_stds),
        "ungrounded_standards": sorted(bad_stds),
        "grounded_ratio": round(grounded / total, 3) if total else 0.0,
        "numeric_checks_total": checked,
        "verified_values_count": checked_ok,
        "tool_result_checks_total": tool_checked,
        "verified_tool_results_count": tool_checked_ok,
        "ungrounded_values": bad_values,
        "value_grounded_ratio": round(checked_ok / checked, 3) if checked else 0.0,
        "fact_errors": errors,
        "unverified_sections": bad_sections,
    }


def verify_diagnosis_groundedness(archive, report: dict, tool_trace: list[dict]) -> dict:
    """Validate an external draft. Only exact, evidenced observations are accepted."""
    return _verify(archive, report, tool_trace, generated=False)


def finalize_scoped_diagnosis(archive, report, trace):
    if not report["confirmed_facts"]:
        return report, diagnostic_boundary_verdict("insufficient_data")
    verification = _verify(archive, report, trace, generated=True)
    return (report if verification["status"] == "verified" else blocked_diagnosis_report()), verification


def standard_reference(reference, clause_id):
    return {
        "id": clause_id,
        "relevance": reference["title"],
        "source": reference.get("source"),
        "page": reference.get("pdf_page"),
        "applicability": reference.get("applicability"),
        "applicability_status": "pending_site_review",
        "source_status": "full_text_pending" if "待" in (reference.get("pdf_page") or "") else "archived_reference",
    }


def finalize_diagnosis(archive, draft, trace):
    """Finalize a local diagnostic report with bound sources."""
    report = dict(draft)
    references = _standards(trace)
    report["cited_standards"] = [
        standard_reference(references[cid], cid)
        for cid in dict.fromkeys(item["id"] for item in draft.get("cited_standards") or [])
        if cid in references
    ]
    facts = report["confirmed_facts"]
    report["summary"] = " ".join(fact["statement"] for fact in facts[:2])
    if report["summary"]:
        report["summary"] += " 现场原因与处置条件待复核。"
    if hypothesis_trace := report.get("hypothesis_trace"):
        observed = [facts[i]["source"] for i in hypothesis_trace["observation_fact_indices"]]
        readings = "、".join(f'{row["cabinet"]} 柜 {row["value"]:g} kW' for row in observed if row["metric"] == "P")
        refutations = [check["judgment"] for check in hypothesis_trace["checks"]
                       if check["outcome"] == "refuted_at_sample"]
        report["summary"] = " ".join([f'{hypothesis_trace["time"]} 有功记录为 {readings}。',
                                     *refutations, hypothesis_trace["stop_message"]])
    review_refs = {}
    for fact in facts:
        source = fact.get("source", {})
        if source.get("kind") == "screening":
            topic = _REVIEW_TOPICS.get(source.get("dimension"), "待核对测点归属与现场工况")
            review_refs.setdefault(topic, []).extend(fact["evidence"])
    report["hypotheses"] = [
        {
            "rank": index,
            "title": topic,
            "status": "pending_site_review",
            "confidence": "unassessed",
            "supporting_evidence": list(dict.fromkeys(refs))[:6],
            "caveats": "须结合同期运行日志、遥信及保护记录区分候选原因。",
        }
        for index, (topic, refs) in enumerate(review_refs.items(), 1)
    ]
    report["field_sop"] = list(_VERIFIED_CHECKS)
    report["section_status"] = {
        "summary": "local_rendering",
        "hypotheses": "pending_site_review",
        "field_sop": "evidence_collection",
        "cited_standards": "references_pending_applicability",
    }
    verification = _verify(archive, report, trace, generated=True)
    if verification["status"] != "verified":
        return blocked_diagnosis_report(), verification
    return report, verification


def blocked_diagnosis_report():
    return {
        "summary": "当前证据无法支持可发布的诊断结论。请核对本轮只读工具回执或补齐资料后重新诊断。",
        "confirmed_facts": [],
        "hypotheses": [],
        "missing_evidence": [],
        "field_sop": [],
        "cited_standards": [],
    }


def build_observations(archive, references, trace):
    available = _tool_cells(trace)
    observations = []
    for cell in dict.fromkeys(references):
        cell = _normalize_cell(cell)
        if cell not in available:
            return None
        record = _lookup_cell_db_record(archive, cell)
        if record is None or record["value"] is None:
            return None
        observations.append(observation_fact(cell, record))
    return observations


def diagnostic_boundary_verdict(status):
    """A request refusal is a system outcome and contains no verified electrical facts."""
    return {
        "policy": EVIDENCE_POLICY,
        "status": status,
        "tool_cells_available": 0,
        "cited_cells_total": 0,
        "verified_cells": [],
        "ungrounded_cells": [],
        "ungrounded_but_in_db": [],
        "cited_standards_total": 0,
        "verified_standards": [],
        "ungrounded_standards": [],
        "grounded_ratio": 0.0,
        "numeric_checks_total": 0,
        "verified_values_count": 0,
        "tool_result_checks_total": 0,
        "verified_tool_results_count": 0,
        "ungrounded_values": [],
        "value_grounded_ratio": 0.0,
        "fact_errors": [],
        "unverified_sections": [],
    }

def build_missing_date_refusal(
    question: str,
    effective_question: str,
    missing_req_days: list[str],
    available_days: list[str],
    *,
    mode: str,
    model: str | None,
    elapsed_seconds: float,
    context_snapshot: dict,
) -> dict:
    """Build explicit out-of-range date refusal payload (Astra #03: never substitute another day)."""
    span_desc = f"{available_days[0]} 至 {available_days[-1]}" if available_days else "暂无已导入日报"
    miss_str = "、".join(missing_req_days)
    missing_report = {
        "summary": f"资料不足：当前本地归档日报仅覆盖 {span_desc}（共 {len(available_days)} 天），缺少所查询日期 {miss_str} 的运行日报，不能以其他日期数据代答。",
        "confirmed_facts": [],
        "hypotheses": [],
        "missing_evidence": [f"{miss_str} 当日运行日报原件及对应小时级遥测归档（当前归档截止：{available_days[-1] if available_days else '无'}）"],
        "field_sop": [f"请先导入 {miss_str} 的正式运行日报并通过 SHA-256 校验，再重新发起当日诊断。"],
        "cited_standards": [],
    }
    ver_miss = {
        "status": "insufficient_data",
        "tool_cells_available": 0,
        "cited_cells_total": 0,
        "verified_cells": [],
        "ungrounded_cells": [],
        "ungrounded_but_in_db": [],
        "cited_standards_total": 0,
        "verified_standards": [],
        "ungrounded_standards": [],
        "grounded_ratio": 1.0,
        "numeric_checks_total": 0,
        "verified_values_count": 0,
        "ungrounded_values": [],
        "value_grounded_ratio": 1.0,
    }
    return {
        "kind": "diagnostic_review",
        "mode": mode,
        "model": model,
        "question": question,
        "resolved_question": effective_question,
        "status": "insufficient_data",
        "stop_reason": "missing_requested_date",
        "missing_days": missing_req_days,
        "archive_date_range": [available_days[0], available_days[-1]] if available_days else [],
        "rounds": 0,
        "tool_calls": 0,
        "reflections_triggered": 0,
        "elapsed_seconds": elapsed_seconds,
        "context_snapshot": context_snapshot,
        "gssc_stats": {"raw_chars": 0, "compressed_chars": 0, "saved_pct": 0.0, "tools": []},
        "ui_artifact_hint": {"canvas": "topology", "focus_cabinet": None, "replay_index": None},
        "hitl_interrupt": None,
        "report": missing_report,
        "verification": ver_miss,
        "tool_trace": [],
        "llm_records": [],
        "dag_execution": None,
    }


def render_diagnosis_markdown(result: dict) -> str:
    if result.get("report") is None:
        err = result.get("error") or result.get("stop_reason")
        return f"诊断未能生成结构化报告（状态：{result.get('status')}，原因：{err}）。\n"

    rep = result["report"]
    ver = result.get("verification") or {}
    conf_map = {"high": "高", "medium": "中", "low": "低"}
    lines = [
        f'# 老洼地电力健康诊断单（{result["mode"]} 模式）',
        "",
        f'- **问题**：{result["question"]}',
        f'- **调用统计**：{result["rounds"]} 轮 / {result["tool_calls"]} 次只读工具调用 / 耗时 {result["elapsed_seconds"]:.2f} s',
        f'- **程序证据校验**：`{ver.get("status")}`（单元格核验 {len(ver.get("verified_cells", []))}/{ver.get("cited_cells_total", 0)}，'
        f'规程核验 {len(ver.get("verified_standards", []))}/{ver.get("cited_standards_total", 0)}，支持率 {ver.get("grounded_ratio", 0)*100:.1f}%）',
        "",
        "## 总体判断",
        "",
        rep["summary"],
        "",
        "## 一、可回查的原表读数和工具结果",
        "",
    ]
    if task := result.get("task_alignment"):
        labels = {"completed": "已完成", "partial": "部分完成", "insufficient_data": "资料不足",
                  "clarification_required": "待确认查询范围", "verification_failed": "证据核验未通过"}
        lines.insert(4, f'- **任务状态**：{labels[task["status"]]}')
    for idx, fact in enumerate(rep.get("confirmed_facts") or [], 1):
        ev = fact.get("evidence") or []
        ev_str = "；".join(f"`{e}`" for e in (ev if isinstance(ev, list) else [ev]))
        source = fact.get("source") or {}
        citation = f'`{fact["citation_id"]}` · ' if fact.get("citation_id") else ""
        receipt = f' · `{source["tool"]}` {source["result_sha256"][:12]}' if source.get("tool") else ""
        lines.append(f'{idx}. {citation}{fact.get("statement", "")}（出处：{ev_str or "工具回执"}{receipt}）')

    if rep.get("operator_note"):
        lines += ["", "## 人工复核备注（待核对原件）", "", rep["operator_note"]]
    if diagnosis := rep.get("hypothesis_trace"):
        lines += ["", "## 二、假设检验记录", "", f'归档整点 {diagnosis["time"]}']
        if diagnosis["observation_step"]:
            refs = "、".join(f'证据 {idx + 1}' for idx in diagnosis["observation_fact_indices"])
            lines += ["", f'初始观测查询 #{diagnosis["observation_step"]} · {refs or "缺少可核对读数"}']
        labels = {"refuted_at_sample": "该采样点证伪", "inconclusive": "待进一步查证",
                  "insufficient_evidence": "证据不足"}
        for check in diagnosis["checks"]:
            arguments = json.dumps(check["arguments"], ensure_ascii=False)
            prior = "、".join(f'#{step}' for step in check["based_on_steps"])
            evidence = "、".join(f'证据 {idx + 1}' for idx in check["fact_indices"])
            planner = "模型规划" if check["planner"] == "model" else "本地规划"
            lines += [
                "", f'### 第 {check["round"]} 轮 · {check["hypothesis"]} {check["title"]}', "",
                f'- 检验条件 · {check["criterion"]}',
                f'- 查询 #{check["step"]} · {planner} · 参考查询 {prior}',
                f'  `{check["tool"]}` `{arguments}`',
                f'- {labels[check["outcome"]]} · {check["judgment"]}',
            ]
            if evidence:
                lines.append(f'- 结果出处 · {evidence}')
        if diagnosis["pending"]:
            lines += ["", "### 待执行检验", ""]
            lines.extend(f'- {item["hypothesis"]} {item["title"]} · {item["criterion"]}'
                         for item in diagnosis["pending"])
        lines += ["", diagnosis["stop_message"]]

    if rep.get("diagnostic_claims"):
        from laowadi.diagnostic_claims import STAGE_LABELS

        for stage, label in STAGE_LABELS.items():
            claims = [claim for claim in rep["diagnostic_claims"] if claim["stage"] == stage]
            if not claims:
                continue
            lines += ["", f"## {label}", ""]
            for claim in claims:
                citations = "、".join(f"`{ref}`" for ref in claim["citations"])
                lines.append(f'- {claim["statement"]}' + (f"（{citations}）" if citations else ""))
        if rep.get("cited_standards"):
            lines += ["", "## 条款出处", ""]
            for std in rep["cited_standards"]:
                lines.append(f'- `{std["id"]}` · {std["source"] or "来源待核"} · {std["page"] or "页码待核"}')
        return "\n".join(lines) + "\n"

    if rep.get("hypotheses") and not rep.get("hypothesis_trace"):
        lines += ["", "## 二、待复核的候选原因", ""]
    for idx, h in enumerate(rep.get("hypotheses") or [], 1):
        conf = conf_map.get(str(h.get("confidence", "")).lower(), str(h.get("confidence", "")))
        sup = "；".join(h.get("supporting_evidence") or [])
        lines += [
            f'### 假设 {h.get("rank", idx)}（支持度：{conf}）：{h.get("title", "")}',
            f'- **支持依据**：{sup}',
            f'- **边界/待排除**：{h.get("caveats", "")}',
            "",
        ]

    lines += ["## 三、缺失现场证据与现场排查步骤", "", "### 缺失证据", ""]
    for item in rep.get("missing_evidence") or []:
        lines.append(f"- {item}")

    lines += ["", "### 现场核查与补证", ""]
    for idx, step in enumerate(rep.get("field_sop") or [], 1):
        lines.append(f"{idx}. {step}")

    if rep.get("cited_standards"):
        lines += ["", "## 四、参考条款（适用性待复核）", ""]
        for std in rep["cited_standards"]:
            lines.append(f'- **`{std.get("id", "")}`**：{std.get("relevance", "")}')

    if ver.get("ungrounded_cells") or ver.get("ungrounded_standards"):
        lines += [
            "",
            "> **程序校验警告**：发现未出现在本轮工具返回中的引用——"
            f'单元格：{ver.get("ungrounded_cells")}，条款：{ver.get("ungrounded_standards")}。请以工具原始回执为准。',
        ]
    lines.append("")
    return "\n".join(lines)
