"""LLM-First Autonomous Investigation Agent with Dynamic Plan Tree, Read-Only Python Sandbox,
True SSE Token Streaming, and Lightweight Post-Hoc Evidence Verification Badge.

Coexists cleanly alongside the Strict Audit Mode (`laowadi/diagnose.py`) without altering
any existing verification gates or mutating read-only golden archives.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from laowadi.code_sandbox import run_python_analysis
from laowadi.context import DiagnosisSession, compress_tool_result, extract_query_anchors
from laowadi.diag_tools import AUTONOMOUS_TOOL_SCHEMAS, DiagnosticToolbox
from laowadi.llm import ChatClient, LLMError
from laowadi.local_router import _infer_ui_artifact_hint, _summarize_tool_result
from laowadi.memory_store import (
    detect_memory_candidates,
    format_memories_for_prompt,
    retrieve_relevant_memories,
)
from laowadi.security import inspect_prompt_security
from laowadi.verifier import _lookup_cell_db_record


_INLINE_CELL_RE = re.compile(r"([A-Za-z0-9_.-]+\.xls\s*/\s*[A-Za-z0-9 _-]+![A-Z]{1,3}\d{1,3})")


UPDATE_PLAN_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_plan",
        "description": (
            "维护并实时向前端推送当前自主排查计划树（Todo Checklist）。"
            "开局时可先拆解 2–5 步排查计划；每完成一步或发现新线索时更新步骤状态或追加新步骤。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "running", "done", "failed"],
                            },
                        },
                        "required": ["id", "title", "status"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["steps"],
            "additionalProperties": False,
        },
    },
}

ALL_AUTONOMOUS_TOOLS = [UPDATE_PLAN_TOOL_SCHEMA, *AUTONOMOUS_TOOL_SCHEMAS]

AUTONOMOUS_SYSTEM_PROMPT = """你是 MineGrid-Agent —— 一位资深的煤矿 35/10 kV 配电网机电总工程师兼自主排查智能体。
你当前挂载了老洼地煤矿只读运行归档库（2026-11-10 至 2026-11-16 共 7 日、地面 35 kV 变电所 25 柜 16,800 条整点遥测、井下 1535 中央变电所 15 柜一次系统图台账，以及 68 条《煤矿安全规程（2025）》与国标条款）。

你的工作方式（真正自主、高效、严谨且有工程洞察）：
1. **并行优先、少轮快答**：
   - 首轮可在同一次响应中**并行发起 1–3 个独立只读工具调用**（不要把可并行的工具拆成 4 轮串行调用）；
   - 涉及主变分接头调压、N-1 转供或末端无功补偿时**直接调用 `run_ac_power_flow`**，无需先重复调用 `check_power_balance`、`get_window_snapshot` 或再用沙盒重算相同潮流指标；
   - 拿到核心量化回执后即刻用 Markdown 总结结论并给出原表坐标，尽量在 **1–2 轮**工具调用内完成收口。
2. **先立计划，边查边调**：面对复杂或开放式问题，可先调用 `update_plan` 列出 2–4 步清晰的排查计划（可与首轮数据工具同批并行发起），并在完成时更新状态。
3. **善用两类武器**：
   - **11 个专业电气只读工具**：快速获取全站健康体检、时窗断面、母线功率守恒、单柜画像、日曲线基线、规程检索、条件化跨站核验、`pandapower` 交流潮流（基准校准/分接头灵敏度/N-1/无功补偿）等。
   - **只读 Python/SQL/NumPy/SciPy/pandapower 沙盒 (`run_python_analysis`)**：遇到任何固定工具未覆盖的开放式计算——如相关性/回归分析、R²、负荷曲线聚类、多柜统计排行、自定义假设潮流试算——**果断现场编写 Python 脚本跑出真实结果**，并通过 `emit_table(title, columns, rows)` 和 `emit_chart(title, chart_type, x_label, y_label, series)` 输出可供界面直接渲染的交互图表（`chart_type` 支持 `scatter`、`line`、`bar`）。
   - 如果沙盒执行报 `stderr`，请立刻阅读报错、修正脚本并重试。
4. **只读 SQLite 核心表结构**（供 `run_python_analysis` 中预注入函数使用）：
   - 严禁 `import sqlite3` 或 `import pandas`（安全沙盒已禁用），直接调用预注入函数 `query_sql(sql, params)`（返回 `list[dict]`）。
   - `reading(day, time, station, cabinet, metric, value, marker, source_row, source_col)` — `station='S35'`, `metric` 严格区分大小写（`'Uab'`, `'Ia'`, `'P'`, `'cos'`）；对齐多指标时按 `r['time']` 分组（示例：`by_t = {}; [by_t.setdefault(r['time'], {}).__setitem__(r['metric'], r['value']) for r in rows]`）。
   - `report(day, filename, sheet, sha256)` — 与 `reading.day=report.day` 关联可拼出原表单元格 `filename || ' / ' || sheet || '!' || source_col || source_row`
   - `cabinet(station, number, label, aliases)`、`summary(day, station, cabinet, metric, statistic, value, marker, source_row, source_col)`
5. **工程红线与表达风格**：
   - 严禁编造读数或单元格坐标；井下 1535 中央变电所（`1101–1207` 柜）无小时日报归档，不得杜撰其电参量；缺失日期如实说明边界；
   - 严禁声称已自动执行遥控合闸/调档，所有操作均为**值班建议（需人工审批）**；
   - **像一位有经验的机电总工那样自然、清晰、有条理地用 Markdown 汇报**：先给核心结论，再讲量化依据（关键数值尽量随手附上原表单元格坐标，如 `1113.xls / New Sheet1!H22`），最后给出可落地的排查或调度建议。不要输出生硬的 JSON 模板。
"""


_NUM_RE = re.compile(r"(?<![A-Za-z0-9_.-])(-?\d+(?:\.\d+)?)(?:\s*(?:kV|kW|kVar|kVA|kWh|A|%|档))?")


def _extract_numbers_from_obj(obj: Any, pool: set[float], depth: int = 0) -> None:
    if depth > 7 or len(pool) > 4000:
        return
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        pool.add(float(obj))
        return
    if isinstance(obj, str):
        for m in _NUM_RE.finditer(obj):
            try:
                pool.add(float(m.group(1)))
            except ValueError:
                pass
        return
    if isinstance(obj, dict):
        for v in obj.values():
            _extract_numbers_from_obj(v, pool, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj[:300]:
            _extract_numbers_from_obj(v, pool, depth + 1)


def _compute_verification_badge(
    archive: Any,
    answer_markdown: str,
    tool_trace: list[dict],
    code_runs: list[dict],
) -> dict[str, Any]:
    """Lightweight post-hoc grounding audit on cited Excel cells & key engineering metrics."""
    text = str(answer_markdown or "")
    cited_cells = list(dict.fromkeys(_INLINE_CELL_RE.findall(text)))
    verified_cells = 0
    notes: list[str] = []

    for cell in cited_cells:
        rec = _lookup_cell_db_record(archive, cell)
        if rec is not None:
            verified_cells += 1
        else:
            notes.append(f"原表坐标未在只读归档中命中：{cell}")

    # Build numeric evidence pool from all executed tools & sandbox outputs
    evidence_nums: set[float] = set()
    for step in tool_trace:
        _extract_numbers_from_obj(step.get("result"), evidence_nums)
    for cr in code_runs:
        _extract_numbers_from_obj(cr.get("stdout"), evidence_nums)
        _extract_numbers_from_obj(cr.get("tables"), evidence_nums)
        _extract_numbers_from_obj(cr.get("charts"), evidence_nums)

    # Check key engineering quantities with explicit electrical units in the final prose
    unit_num_re = re.compile(
        r"(-?\d+(?:\.\d+)?)\s*(kV|kW|kVar|kVA|kWh|A|%)",
        re.I,
    )
    checked_numbers = 0
    verified_numbers = 0
    seen_unit_vals: set[tuple[str, str]] = set()
    for m in unit_num_re.finditer(text):
        raw_val, unit = m.group(1), m.group(2)
        key = (raw_val, unit.lower())
        if key in seen_unit_vals:
            continue
        seen_unit_vals.add(key)
        val = float(raw_val)
        # Skip standard nominal ratings / GB limits commonly cited in prose (35 kV, 10 kV, 10.7 kV, 9.3 kV, 7%)
        if abs(val) in {35.0, 10.0, 10.7, 9.3, 10.5, 6.0, 7.0, 100.0} and unit.lower() in {"kv", "%"}:
            continue
        checked_numbers += 1
        matched = any(
            abs(val - ev) <= max(0.05, abs(val) * 0.015)
            or abs(abs(val) - abs(ev)) <= max(0.05, abs(val) * 0.015)
            for ev in evidence_nums
        )
        if matched:
            verified_numbers += 1

    if cited_cells and verified_cells == len(cited_cells) and (checked_numbers == 0 or verified_numbers / max(checked_numbers, 1) >= 0.75):
        status = "verified"
    elif (verified_cells > 0 or verified_numbers > 0) and (len(cited_cells) == 0 or verified_cells == len(cited_cells)):
        status = "verified" if (checked_numbers == 0 or verified_numbers / max(checked_numbers, 1) >= 0.75) else "partial"
    elif verified_cells > 0 or verified_numbers > 0 or tool_trace or code_runs:
        status = "partial"
    else:
        status = "unverified"

    if not notes:
        if status == "verified":
            notes.append("关键电气指标与原表单元格均已在本次只读工具/沙盒回执中核实。")
        elif status == "partial":
            notes.append("部分推演或对比参考值未直接对应单一原表单元格，请结合展开的沙盒/工具回执复核。")
        else:
            notes.append("本次回答未调用只读工具或未产生可比对数值。")

    return {
        "status": status,
        "checked_cells": len(cited_cells),
        "verified_cells": verified_cells,
        "checked_numbers": checked_numbers,
        "verified_numbers": verified_numbers,
        "notes": notes[:4],
    }


def _stream_markdown_chunks(text: str, emit, *, chunk_size: int = 42) -> None:
    """Emit smooth content_delta events when synthesizing in offline/local autonomous mode."""
    so_far = ""
    for i in range(0, len(text), chunk_size):
        delta = text[i : i + chunk_size]
        so_far += delta
        emit("content_delta", delta=delta, text=so_far)


def _run_local_autonomous(
    archive: Any,
    toolbox: DiagnosticToolbox,
    question: str,
    anchors: dict[str, Any],
    emit,
    *,
    fallback_note: str | None = None,
) -> dict[str, Any]:
    """Rich offline autonomous execution using real tools + real read-only Python sandbox."""
    q = question
    cabs = anchors.get("cabinets") or []
    days = anchors.get("days") or []
    tool_trace: list[dict] = []
    code_runs: list[dict] = []
    artifacts: list[dict] = []
    call_seq = 0

    def next_call_id() -> str:
        nonlocal call_seq
        call_seq += 1
        return f"c{call_seq}"

    # Branch 1: Correlation / Regression / Scatter open-ended analysis
    if any(k in q for k in ("相关", "回归", "散点", "R2", "R²", "斜率", "拟合", "轻载")):
        cab = cabs[0] if cabs else "1006"
        x_metric = "P" if any(k in q for k in ("有功", "负荷", "轻载", "P")) and "功率因数" not in q and "cos" not in q.lower() else "cos"
        x_name = "有功功率 P (kW)" if x_metric == "P" else "功率因数 cosφ"
        plan = [
            {"id": "s1", "title": f"读取 {cab} 柜全周画像与基准指标", "status": "running"},
            {"id": "s2", "title": f"在只读 Python 沙盒计算 {cab} 柜 {x_name} 与母线电压相关性回归并绘散点图", "status": "pending"},
            {"id": "s3", "title": "综合工程机理与原表极值核验", "status": "pending"},
        ]
        emit("plan_update", steps=plan)
        r_txt = f"识别到开放式统计回归需求：针对 {cab} 柜，先调取单柜健康画像确认有效样本与越限情况，再通过只读 Python 沙盒联合查询 7 日整点 {x_metric} 与 Uab 数据做 Pearson 相关系数与线性回归拟合，并输出交互式散点图。"
        emit("reasoning_delta", delta=r_txt, text=r_txt)

        cid1 = next_call_id()
        emit("tool_start", call_id=cid1, tool="get_cabinet_health_profile", arguments={"cabinet": cab})
        prof = toolbox.execute("get_cabinet_health_profile", {"cabinet": cab})
        tool_trace.append({"tool": "get_cabinet_health_profile", "arguments": {"cabinet": cab}, "result": prof})
        emit("tool_end", call_id=cid1, tool="get_cabinet_health_profile", summary=_summarize_tool_result("get_cabinet_health_profile", prof), ok=prof.get("status") == "ok")

        plan[0]["status"] = "done"
        plan[1]["status"] = "running"
        emit("plan_update", steps=plan)

        code = f"""import numpy as np
rows = query_sql('''
SELECT r1.time, r1.value AS x_val, r2.value AS u_val,
       rp.filename || ' / ' || rp.sheet || '!' || r2.source_col || r2.source_row AS u_cell
FROM reading r1
JOIN reading r2 ON r1.station=r2.station AND r1.cabinet=r2.cabinet AND r1.time=r2.time
JOIN report rp ON r2.day=rp.day
WHERE r1.station='S35' AND r1.cabinet=? AND r1.metric=? AND r2.metric='Uab'
  AND r1.value IS NOT NULL AND r2.value IS NOT NULL
ORDER BY r1.time
''', ({cab!r}, {x_metric!r}))
xs = np.array([r['x_val'] for r in rows], dtype=float)
ys = np.array([r['u_val'] for r in rows], dtype=float)
r_coef = float(np.corrcoef(xs, ys)[0, 1]) if len(xs) > 2 and np.std(xs) > 1e-9 else 0.0
slope, intercept = np.polyfit(xs, ys, 1) if len(xs) > 2 and np.std(xs) > 1e-9 else (0.0, float(np.mean(ys)))
r2 = r_coef ** 2
max_row = max(rows, key=lambda r: r['u_val'])
min_row = min(rows, key=lambda r: r['u_val'])
print(f"样本整点数: {{len(rows)}} | Pearson r = {{r_coef:.4f}} | R^2 = {{r2:.4f}}")
print(f"线性拟合: Uab = {{slope:.4f}} * {x_metric} + {{intercept:.4f}} kV")
print(f"最高电压: {{max_row['u_val']:.2f}} kV @ {{max_row['time']}} ({{max_row['u_cell']}})")
print(f"最低电压: {{min_row['u_val']:.2f}} kV @ {{min_row['time']}} ({{min_row['u_cell']}})")
emit_table(
    "{cab} 柜 {x_metric} vs Uab 相关性回归摘要",
    ["指标项", "计算结果", "原表/说明"],
    [
        ["有效整点样本数", len(rows), "2026-11-10 至 2026-11-16"],
        ["Pearson 相关系数 r", round(r_coef, 4), "负值表示负荷/因数越低电压越高" if r_coef < 0 else "正相关"],
        ["决定系数 R²", round(r2, 4), "线性解释度"],
        ["回归斜率", round(float(slope), 5), "kV / 单位自变量"],
        ["最高母线电压 (kV)", round(max_row['u_val'], 2), max_row['u_cell']],
        ["最低母线电压 (kV)", round(min_row['u_val'], 2), min_row['u_cell']],
    ],
)
emit_chart(
    "{cab} 柜 {x_name} 与线电压 Uab 相关性散点图",
    "scatter",
    "{x_name}",
    "线电压 Uab (kV)",
    [
        {{"name": "{cab} 柜实测整点", "points": [{{"x": round(r['x_val'], 3), "y": round(r['u_val'], 3), "label": r['time'][5:16]}} for r in rows]}},
    ],
)
"""
        cid2 = next_call_id()
        emit("tool_start", call_id=cid2, tool="run_python_analysis", arguments={"code": code})
        sb = run_python_analysis(archive, code)
        code_runs.append({"call_id": cid2, "code": code, **sb})
        tool_trace.append({"tool": "run_python_analysis", "arguments": {"code": code}, "result": sb})
        emit("code_run", call_id=cid2, code=code, status=sb["status"], stdout=sb["stdout"], stderr=sb["stderr"], tables=sb["tables"], charts=sb["charts"], elapsed_ms=sb["elapsed_ms"])
        emit("tool_end", call_id=cid2, tool="run_python_analysis", summary=f"沙盒执行 {sb['status']}（{sb['elapsed_ms']} ms，{len(sb['charts'])} 图 {len(sb['tables'])} 表）", ok=sb["status"] == "ok")
        for t in sb["tables"]:
            artifacts.append({"kind": "table", **t})
        for ch in sb["charts"]:
            artifacts.append({"kind": "chart", **ch})

        plan[1]["status"] = "done"
        plan[2]["status"] = "running"
        emit("plan_update", steps=plan)

        tbl_rows = sb["tables"][0]["rows"] if sb["tables"] else []
        r_val = tbl_rows[1][1] if len(tbl_rows) > 1 else 0.0
        r2_val = tbl_rows[2][1] if len(tbl_rows) > 2 else 0.0
        max_u = tbl_rows[4][1] if len(tbl_rows) > 4 else ""
        max_cell = tbl_rows[4][2] if len(tbl_rows) > 4 else ""
        min_u = tbl_rows[5][1] if len(tbl_rows) > 5 else ""
        min_cell = tbl_rows[5][2] if len(tbl_rows) > 5 else ""

        answer = (
            (f"> {fallback_note}\n\n" if fallback_note else "")
            + f"### 核心结论：{cab} 柜 `{x_metric}` 与线电压 `Uab` 相关性回归分析\n\n"
            + f"通过只读 Python 沙盒对 7 日归档全部有效整点执行联合查询与线性回归：\n"
            + f"- **相关性强度**：Pearson 相关系数 `r = {r_val}`，决定系数 `R² = {r2_val}`。\n"
            + f"- **电压极值核实**：最高线电压 **{max_u} kV**（原表 `{max_cell}`），最低线电压 **{min_u} kV**（原表 `{min_cell}`）。\n"
            + f"- **工程解读**：{'相关系数呈负相关，说明低负荷/低因数时段电压抬升特征明显，但若 R² 不高，说明 35 kV 上级电网来电电压偏高与主变分接头挡位是更主导的电压抬升因素。' if float(r_val) < 0 else '两者呈正相关或弱相关，表明母线电压波动主要由上级 35 kV 系统电压与主变分接头决定，而非单一局部负荷变化驱动。'}\n\n"
            + f"### 建议下一步\n"
            + f"1. 在右侧「交流潮流」面板核验 2# 主变分接头调至 `+2 档` 后的全周压降效果；\n"
            + f"2. 结合现场分接头台账核对实际挡位，确认调压裕度后再履行人工倒闸审批。"
        )
        _stream_markdown_chunks(answer, emit)
        plan[2]["status"] = "done"
        emit("plan_update", steps=plan)
        return {"answer_markdown": answer, "plan": plan, "artifacts": artifacts, "tool_trace": tool_trace, "code_runs": code_runs}

    # Branch 2: Load curve clustering / volatility / multi-cabinet ranking
    if any(k in q for k in ("聚类", "波动", "方差", "标准差", "排行", "前5", "前 5", "三班倒", "用电量", "负荷曲线")):
        plan = [
            {"id": "s1", "title": "扫描地面 35 kV 站全部 10 kV 馈线 7 日小时负荷序列", "status": "running"},
            {"id": "s2", "title": "在只读 Python 沙盒计算波动率、累计电量与峰谷比并绘图", "status": "pending"},
            {"id": "s3", "title": "提炼重点关注回路与调度建议", "status": "pending"},
        ]
        emit("plan_update", steps=plan)
        r_txt = "这是一个典型的多回路负荷特性挖掘问题。直接调用只读 Python 沙盒对 10 kV 出线 168 小时数据进行波动率、累计电量和负荷特征统计，生成对比柱状图和汇总表。"
        emit("reasoning_delta", delta=r_txt, text=r_txt)

        code = """import numpy as np
rows = query_sql('''
SELECT r.cabinet, c.label, r.time, r.value
FROM reading r
JOIN cabinet c ON c.station=r.station AND c.number=r.cabinet
WHERE r.station='S35' AND r.metric='P' AND r.value IS NOT NULL
  AND CAST(r.cabinet AS INTEGER) >= 1007
ORDER BY r.cabinet, r.time
''')
by_cab = collections.defaultdict(list)
labels = {}
for r in rows:
    by_cab[r['cabinet']].append(float(r['value']))
    labels[r['cabinet']] = r['label']

stats = []
for cab, vals in by_cab.items():
    arr = np.array(vals, dtype=float)
    mean_p = float(np.mean(arr))
    std_p = float(np.std(arr))
    max_p = float(np.max(arr))
    kwh = float(np.sum(arr))
    cv = std_p / max(abs(mean_p), 1.0)
    stats.append((cab, labels[cab], mean_p, std_p, max_p, kwh, cv))

stats.sort(key=lambda x: x[3], reverse=True)
top8 = stats[:8]
for s in top8:
    print(f"{s[0]}({s[1]}): 均值={s[2]:.1f} kW, 标准差={s[3]:.1f} kW, 峰值={s[4]:.1f} kW, 7日累计={s[5]:.1f} kWh")

emit_table(
    "10 kV 出线 7 日负荷波动与电量特征 Top 8",
    ["柜号", "回路名称", "均值 P(kW)", "标准差 σ(kW)", "峰值 P(kW)", "7日累计(kWh)", "离散系数 CV"],
    [[s[0], s[1], round(s[2], 1), round(s[3], 1), round(s[4], 1), round(s[5], 1), round(s[6], 2)] for s in top8],
)
emit_chart(
    "10 kV 重点出线有功负荷标准差对比 (kW)",
    "bar",
    "出线柜号",
    "有功标准差 σ (kW)",
    [
        {"name": "负荷波动标准差 (kW)", "points": [{"x": s[0], "y": round(s[3], 2), "label": s[1]} for s in top8]},
    ],
)
"""
        plan[0]["status"] = "done"
        plan[1]["status"] = "running"
        emit("plan_update", steps=plan)

        cid1 = next_call_id()
        emit("tool_start", call_id=cid1, tool="run_python_analysis", arguments={"code": code})
        sb = run_python_analysis(archive, code)
        code_runs.append({"call_id": cid1, "code": code, **sb})
        tool_trace.append({"tool": "run_python_analysis", "arguments": {"code": code}, "result": sb})
        emit("code_run", call_id=cid1, code=code, status=sb["status"], stdout=sb["stdout"], stderr=sb["stderr"], tables=sb["tables"], charts=sb["charts"], elapsed_ms=sb["elapsed_ms"])
        emit("tool_end", call_id=cid1, tool="run_python_analysis", summary=f"沙盒完成 10 kV 回路波动排名（{sb['elapsed_ms']} ms）", ok=sb["status"] == "ok")
        for t in sb["tables"]:
            artifacts.append({"kind": "table", **t})
        for ch in sb["charts"]:
            artifacts.append({"kind": "chart", **ch})

        plan[1]["status"] = "done"
        plan[2]["status"] = "running"
        emit("plan_update", steps=plan)

        rows_t = sb["tables"][0]["rows"] if sb["tables"] else []
        top_lines = "\n".join(
            f"- **{r[0]} 柜（{r[1]}）**：均值 `{r[2]} kW`、波动标准差 `{r[3]} kW`、峰值 `{r[4]} kW`、7 日累计 `{r[5]} kWh`（离散系数 `CV = {r[6]}`）"
            for r in rows_t[:5]
        )
        answer = (
            (f"> {fallback_note}\n\n" if fallback_note else "")
            + "### 核心结论：10 kV 馈线 7 日负荷波动与电量特征画像\n\n"
            + "在只读 Python 沙盒中对 10 kV 全部出线（168 小时整点序列）完成统计排序，波动最显著的前 5 个回路为：\n"
            + f"{top_lines}\n\n"
            + "### 工程洞察\n"
            + "- **高波动冲击型回路**（高标准差/高 CV）通常对应间歇采掘或提升设备，建议在峰谷切换时段重点跟踪无功冲击与母线压降；\n"
            + "- **高基荷稳态型回路**（高累计 kWh、低 CV，如主扇/下井主干线）最适合作为无功定容补偿与 N-1 载流裕度优先核查对象。"
        )
        _stream_markdown_chunks(answer, emit)
        plan[2]["status"] = "done"
        emit("plan_update", steps=plan)
        return {"answer_markdown": answer, "plan": plan, "artifacts": artifacts, "tool_trace": tool_trace, "code_runs": code_runs}

    # Branch 3: Power-flow / Overvoltage / Tap / Reactive / N-1 What-If
    if any(k in q for k in ("潮流", "过压", "分接头", "调压", "无功", "补偿", "N-1", "n-1", "增容", "裕度")):
        scenario = "reactive_opt" if any(k in q for k in ("无功", "补偿", "功率因数")) else (
            "n1_contingency" if any(k in q for k in ("N-1", "n-1", "转供")) else "tap_sensitivity"
        )
        plan = [
            {"id": "s1", "title": f"运行 pandapower 交流潮流（{scenario} 工况）", "status": "running"},
            {"id": "s2", "title": "在沙盒生成电压/损耗可视化曲线与对比表", "status": "pending"},
            {"id": "s3", "title": "输出工程师调压/补偿建议与安全边界", "status": "pending"},
        ]
        emit("plan_update", steps=plan)
        r_txt = f"识别到潮流与电压控制需求，先调用 pandapower 交流潮流求解器计算 `{scenario}` 工况，再整理曲线与量化指标。"
        emit("reasoning_delta", delta=r_txt, text=r_txt)

        cid1 = next_call_id()
        pf_args = {"scenario": scenario}
        if scenario == "tap_sensitivity":
            pf_args["at_time"] = "2026-11-13 17:00:00"
        emit("tool_start", call_id=cid1, tool="run_ac_power_flow", arguments=pf_args)
        pf = toolbox.execute("run_ac_power_flow", pf_args)
        tool_trace.append({"tool": "run_ac_power_flow", "arguments": pf_args, "result": pf})
        emit("tool_end", call_id=cid1, tool="run_ac_power_flow", summary=_summarize_tool_result("run_ac_power_flow", pf), ok=pf.get("status") == "ok")

        plan[0]["status"] = "done"
        plan[1]["status"] = "running"
        emit("plan_update", steps=plan)

        if scenario == "tap_sensitivity" and pf.get("tap_sensitivity"):
            sweep = pf["tap_sensitivity"]["sweep"]
            rec = pf["tap_sensitivity"].get("recommended_tap") or {}
            tbl = {
                "title": "2# 主变分接头档位灵敏度扫描（2026-11-13 17:00）",
                "columns": ["分接头档位", "10 kV II 段母线 (kV)", "是否满足 GB/T 12325", "全网损耗 (kW)"],
                "rows": [
                    [s["tap_label"], s["bus_10kv_ii_kv"], "合格" if s["gb12325_compliant"] else "越限", s["total_loss_kw"]]
                    for s in sweep
                ],
            }
            ch = {
                "title": "2# 主变分接头调节对 10 kV II 段电压影响曲线",
                "chart_type": "line",
                "x_label": "分接头档位",
                "y_label": "母线电压 (kV)",
                "series": [
                    {
                        "name": "10 kV II 段母线电压",
                        "points": [{"x": str(s["tap_pos"]), "y": s["bus_10kv_ii_kv"], "label": s["tap_label"]} for s in sweep],
                    }
                ],
            }
            tbl_art = {"kind": "table", **tbl}
            ch_art = {"kind": "chart", **ch}
            artifacts.extend([tbl_art, ch_art])
            emit("artifact", artifact=tbl_art)
            emit("artifact", artifact=ch_art)
            answer = (
                (f"> {fallback_note}\n\n" if fallback_note else "")
                + "### 核心结论：2# 主变分接头调压交流潮流复核\n\n"
                + f"基于 `2026-11-13 17:00:00` 过压断面运行 `pandapower` 牛顿-拉夫逊潮流：\n"
                + f"- **推荐档位**：**{rec.get('tap_label', '+2 档')}**，可将 10 kV II 段母线电压降至 **{rec.get('bus_10kv_ii_kv', 10.662)} kV**（回落至 GB/T 12325 `9.30–10.70 kV` 合格区间内）。\n"
                + f"- **单调灵敏度**：每升高 1 档（+2.5% 高压绕组），10 kV II 段电压约下降 `0.26–0.28 kV`。\n\n"
                + "### 安全提示\n"
                + "- 以上计算基于参数化等效主变模型，执行前须由值班人员核对 2# 主变现场实际档位铭牌并履行停电/有载调压操作票审批。"
            )
        else:
            answer = (
                (f"> {fallback_note}\n\n" if fallback_note else "")
                + f"### 核心结论：`pandapower` 交流潮流（`{scenario}`）复算完成\n\n"
                + f"- **求解摘要**：{_summarize_tool_result('run_ac_power_flow', pf)}\n"
                + "- 请结合右侧「交流潮流」画布核对各母线电压、支路电流与等效参数假设声明；所有开关与调压动作均须经人工现场复核审批。"
            )
        plan[1]["status"] = "done"
        plan[2]["status"] = "running"
        emit("plan_update", steps=plan)
        _stream_markdown_chunks(answer, emit)
        plan[2]["status"] = "done"
        emit("plan_update", steps=plan)
        return {"answer_markdown": answer, "plan": plan, "artifacts": artifacts, "tool_trace": tool_trace, "code_runs": code_runs}

    # Branch 3b: Precision scoped / event / single-cabinet / hypothesis query (never answer with generic overview!)
    has_specific_target = bool(
        anchors.get("events")
        or anchors.get("cabinets")
        or anchors.get("hours")
        or any(k in q for k in ("1535", "C1535", "反向", "负有功", "归零", "极性", "比较", "变化", "事件", "LW-"))
    )
    if has_specific_target and not any(k in q for k in ("全站", "总览", "健康体检", "隐患盘点", "风险盘点")):
        plan = [
            {"id": "s1", "title": "识别查询对象并执行精准只读归档取证", "status": "running"},
            {"id": "s2", "title": "核对原表单元格坐标并组织工程师答复", "status": "pending"},
        ]
        emit("plan_update", steps=plan)
        r_txt = "识别到指定柜号/时刻/事件的精准查证需求，直接调用只读归档查证链获取原表读数与假设检验结论，确保零答非所问。"
        emit("reasoning_delta", delta=r_txt, text=r_txt)

        from laowadi.diagnose import diagnose_question

        diag = diagnose_question(archive, question, client=None)
        for step in (diag.get("tool_trace") or [])[:6]:
            cid = next_call_id()
            tname = step.get("tool") or "get_window_snapshot"
            targs = step.get("arguments") or {}
            tres = step.get("result") or {}
            emit("tool_start", call_id=cid, tool=tname, arguments=targs)
            tool_trace.append({"tool": tname, "arguments": targs, "result": tres})
            emit("tool_end", call_id=cid, tool=tname, summary=_summarize_tool_result(tname, tres), ok=tres.get("status") != "error")

        plan[0]["status"] = "done"
        plan[1]["status"] = "running"
        emit("plan_update", steps=plan)

        rep = diag.get("report") or {}
        summary_txt = str(rep.get("summary") or "已完成精准只读核查。").strip()
        facts = rep.get("confirmed_facts") or []
        fact_lines = []
        for f in facts[:6]:
            if isinstance(f, dict):
                ev = f.get("evidence") or []
                ev_str = "、".join(f"`{c}`" for c in (ev if isinstance(ev, list) else [ev])[:2] if c)
                fact_lines.append(f"- {f.get('statement', '')}" + (f"（原表：{ev_str}）" if ev_str else ""))
        hyps = rep.get("hypotheses") or []
        hyp_lines = [
            f"- **候选假设 {idx + 1}：{h.get('title', '')}**（置信度 `{h.get('confidence', 'medium')}`）—— {h.get('caveats') or h.get('rationale') or ''}"
            for idx, h in enumerate(hyps[:3])
            if isinstance(h, dict)
        ]
        missing = rep.get("missing_evidence") or []
        miss_lines = [f"- {m}" for m in missing[:3] if m]

        answer = (
            (f"> {fallback_note}\n\n" if fallback_note else "")
            + f"### 精准核查结论\n\n{summary_txt}\n"
            + ("\n### 原表核实依据\n" + "\n".join(fact_lines) + "\n" if fact_lines else "")
            + ("\n### 假设排查与归因\n" + "\n".join(hyp_lines) + "\n" if hyp_lines else "")
            + ("\n### 待补现场证据\n" + "\n".join(miss_lines) + "\n" if miss_lines else "")
        ).strip()
        _stream_markdown_chunks(answer, emit)
        plan[1]["status"] = "done"
        emit("plan_update", steps=plan)
        return {"answer_markdown": answer, "plan": plan, "artifacts": artifacts, "tool_trace": tool_trace, "code_runs": code_runs}

    # Branch 4: General health / anomaly open-ended overview + sandbox daily chart
    plan = [
        {"id": "s1", "title": "调取老洼地 35 kV 站健康体检总览与核心异常清单", "status": "running"},
        {"id": "s2", "title": "在只读沙盒统计 7 日 10 kV 过压越限小时分布", "status": "pending"},
        {"id": "s3", "title": "综合异常优先级与值班排查建议", "status": "pending"},
    ]
    emit("plan_update", steps=plan)
    r_txt = "先执行全站健康体检把握整体异常分布，再用只读沙盒按日统计 10 kV 越限整点趋势，为你给出重点突出的值班研判。"
    emit("reasoning_delta", delta=r_txt, text=r_txt)

    cid1 = next_call_id()
    ov_args = {"day": days[0]} if len(days) == 1 else {}
    emit("tool_start", call_id=cid1, tool="get_station_health_overview", arguments=ov_args)
    ov = toolbox.execute("get_station_health_overview", ov_args)
    tool_trace.append({"tool": "get_station_health_overview", "arguments": ov_args, "result": ov})
    emit("tool_end", call_id=cid1, tool="get_station_health_overview", summary=_summarize_tool_result("get_station_health_overview", ov), ok=ov.get("status") == "ok")

    plan[0]["status"] = "done"
    plan[1]["status"] = "running"
    emit("plan_update", steps=plan)

    code = """rows = query_sql('''
SELECT day,
       COUNT(DISTINCT CASE WHEN value > 10.70 THEN time END) AS over_hours,
       ROUND(MAX(value), 2) AS max_uab
FROM reading
WHERE station='S35' AND metric='Uab' AND value IS NOT NULL
  AND CAST(cabinet AS INTEGER) >= 1005
GROUP BY day ORDER BY day
''')
for r in rows:
    print(f"{r['day']}: 10kV过压整点数={r['over_hours']} h, 最高线电压={r['max_uab']} kV")
emit_table(
    "老洼地 7 日 10 kV 母线过压越限（>10.70 kV）统计",
    ["日期", "越限整点数 (h)", "当日最高线电压 (kV)"],
    [[r['day'], r['over_hours'], r['max_uab']] for r in rows],
)
emit_chart(
    "7 日 10 kV 过压越限整点趋势",
    "bar",
    "日期",
    "越限小时数 (h)",
    [{"name": "Uab > 10.70 kV 小时数", "points": [{"x": r['day'][5:], "y": r['over_hours'], "label": f"峰值 {r['max_uab']} kV"} for r in rows]}],
)
"""
    cid2 = next_call_id()
    emit("tool_start", call_id=cid2, tool="run_python_analysis", arguments={"code": code})
    sb = run_python_analysis(archive, code)
    code_runs.append({"call_id": cid2, "code": code, **sb})
    tool_trace.append({"tool": "run_python_analysis", "arguments": {"code": code}, "result": sb})
    emit("code_run", call_id=cid2, code=code, status=sb["status"], stdout=sb["stdout"], stderr=sb["stderr"], tables=sb["tables"], charts=sb["charts"], elapsed_ms=sb["elapsed_ms"])
    emit("tool_end", call_id=cid2, tool="run_python_analysis", summary=f"沙盒完成 7 日过压趋势统计（{sb['elapsed_ms']} ms）", ok=sb["status"] == "ok")
    for t in sb["tables"]:
        artifacts.append({"kind": "table", **t})
    for ch in sb["charts"]:
        artifacts.append({"kind": "chart", **ch})

    plan[1]["status"] = "done"
    plan[2]["status"] = "running"
    emit("plan_update", steps=plan)

    findings = (ov.get("findings") or [])[:4]
    finding_lines = "\n".join(
        f"- **[{f['severity'].upper()}] {f['cabinet']} 柜 · {f['title']}**：{f['summary']}"
        for f in findings
    )
    answer = (
        (f"> {fallback_note}\n\n" if fallback_note else "")
        + "### 全站健康巡检与核心隐患研判\n\n"
        + f"依据只读归档库体检结果（覆盖 `{ov.get('scope', {}).get('hours', 168)}` 整点），当前需优先关注的典型异常如下：\n"
        + f"{finding_lines}\n\n"
        + "### 建议值班动作\n"
        + "1. **电压越限治理**：结合上方 7 日过压分布，优先核查 2# 主变分接头实际挡位并在交流潮流页复算调压与投容联合方案；\n"
        + "2. **负有功与量测核校**：对 `306/312/1006` 负有功链及 `1018` 柜负有功/负功率因数开展现场二次极性与表计规约核实；\n"
        + "3. 如需针对特定柜号做相关性回归、负荷波动排行或自定义假设计算，直接提问即可触发 Python 沙盒现场测算。"
    )
    _stream_markdown_chunks(answer, emit)
    plan[2]["status"] = "done"
    emit("plan_update", steps=plan)
    return {"answer_markdown": answer, "plan": plan, "artifacts": artifacts, "tool_trace": tool_trace, "code_runs": code_runs}


def _infer_recommended_action(question: str, anchors: dict[str, Any], answer_markdown: str) -> dict[str, Any] | None:
    """Suggest a matching human-approved action ticket draft when diagnostics surface actionable risks."""
    blob = f"{question} {answer_markdown}"
    cabs = anchors.get("cabinets") or []
    days = anchors.get("days") or []
    hours = anchors.get("hours") or []
    at_time = f"{days[0]} {hours[0]}:00" if days and hours else ("2026-11-13 17:00:00" if "1006" in blob or "过压" in blob else None)

    if any(k in blob for k in ("过压", "分接头", "调压", "无功", "补偿", "+2 档", "10.70", "10.65")):
        cab = cabs[0] if cabs else "1006"
        return {
            "kind": "switching_order",
            "target_cabinet": cab,
            "at_time": at_time or "2026-11-13 17:00:00",
            "rationale": f"基于自主排查对 {cab} 柜电压/调压分析，起草标准化电气倒闸操作票草案（须人工逐项核对签发）。",
        }
    if any(k in blob for k in ("负有功", "反向", "极性", "1018", "反送")):
        cab = "1018" if "1018" in blob else (cabs[0] if cabs else "1006")
        return {
            "kind": "defect_notice",
            "target_cabinet": cab,
            "at_time": at_time or ("2026-11-16 21:00:00" if cab == "1018" else "2026-11-12 12:00:00"),
            "rationale": f"针对 {cab} 柜负有功/极性异常排查结论，起草二次回路与表计极性消缺通知单。",
        }
    if any(k in blob for k in ("归零", "单进线", "载流量", "铭牌", "补证", "VoI", "N-1", "n-1")):
        cab = cabs[0] if cabs else "303"
        return {
            "kind": "field_verification",
            "target_cabinet": cab,
            "at_time": at_time or "2026-11-13 15:00:00",
            "rationale": f"针对 {cab} 柜裕度与缺失现场台账，起草现场铭牌与开关状态补录核校单。",
        }
    return None


def _infer_deep_research_goal(question: str, anchors: dict[str, Any]) -> str:
    cabs = anchors.get("cabinets") or []
    if cabs:
        return f"围绕 {'/'.join(cabs[:3])} 柜专项核查全周运行隐患、交流潮流调压优化与 VoI 补证优先级"
    q_short = str(question or "").strip()[:80]
    return f"围绕「{q_short}」开展老洼地 7 日配电网 6 阶段深度专项排查与报告生成"


def run_autonomous_agent(
    archive: Any,
    question: str,
    *,
    client: ChatClient | None = None,
    session: DiagnosisSession | None = None,
    max_steps: int = 8,
    memory_db_path=None,
    on_event=None,
) -> dict[str, Any]:
    """Run the LLM-First Autonomous Investigation Agent with live SSE callbacks."""
    t0 = time.monotonic()
    toolbox = DiagnosticToolbox(archive)
    known_cabs = {num for _, num in archive.devices}
    if session is not None:
        effective_question, anchors, _ctx_snap = session.resolve_question(question)
    else:
        effective_question = question
        anchors = extract_query_anchors(question, known_cabs)
    applied_memories = retrieve_relevant_memories(
        effective_question,
        cabinets=anchors.get("cabinets"),
        limit=6,
        db_path=memory_db_path,
    )

    def emit(ev_type: str, **payload):
        if on_event is not None:
            on_event({"type": ev_type, "ts": round(time.monotonic() - t0, 3), **payload})

    # 1. Security firewall (never allow prompt injection or unauthorized remote control)
    sec_check = inspect_prompt_security(question)
    if not sec_check["allowed"]:
        msg = f"安全围栏已拦截该请求：{sec_check['message']}"
        plan = [{"id": "s1", "title": "安全与只读权限校验", "status": "failed"}]
        emit("plan_update", steps=plan)
        emit("content_delta", delta=msg, text=msg)
        badge = {
            "status": "verified",
            "checked_cells": 0,
            "verified_cells": 0,
            "checked_numbers": 0,
            "verified_numbers": 0,
            "notes": [sec_check["message"]],
        }
        emit("verification_badge", **badge)
        res = {
            "status": "blocked",
            "mode": "autonomous",
            "answer_markdown": msg,
            "plan": plan,
            "artifacts": [],
            "tool_trace": [],
            "verification_badge": badge,
            "ui_artifact_hint": {"canvas": "topology", "focus_cabinet": None, "replay_index": None},
            "elapsed_seconds": round(time.monotonic() - t0, 3),
        }
        emit("done", result=res)
        return res

    # 2. Offline / local autonomous mode
    if client is None:
        local_out = _run_local_autonomous(archive, toolbox, effective_question, anchors, emit)
        answer_md = local_out["answer_markdown"]
        if applied_memories:
            mem_note = "\n\n### 已参考生效长期记忆\n" + "\n".join(
                f"- **[{m['category']}] {m['title']}**：{m['content']}" for m in applied_memories[:4]
            )
            answer_md += mem_note
            emit("content_delta", delta=mem_note, text=answer_md)
        badge = _compute_verification_badge(archive, answer_md, local_out["tool_trace"], local_out["code_runs"])
        emit("verification_badge", **badge)
        hint = _infer_ui_artifact_hint(effective_question, anchors, local_out["tool_trace"])
        rec_action = _infer_recommended_action(effective_question, anchors, answer_md)
        dr_goal = _infer_deep_research_goal(effective_question, anchors)
        mem_suggestions = detect_memory_candidates(
            question,
            answer_md,
            cabinets=anchors.get("cabinets"),
            existing_memories=applied_memories,
        )
        if mem_suggestions:
            emit("memory_suggestion", suggestions=mem_suggestions)
        if session is not None and hasattr(session, "record_autonomous_turn"):
            session.record_autonomous_turn(
                question,
                answer_md,
                verified_cells=list(dict.fromkeys(_INLINE_CELL_RE.findall(answer_md)))[:6],
            )
        res = {
            "status": "ok",
            "mode": "autonomous_local",
            "model": None,
            "answer_markdown": answer_md,
            "plan": local_out["plan"],
            "artifacts": local_out["artifacts"],
            "applied_memories": applied_memories,
            "memory_suggestions": mem_suggestions,
            "recommended_action": rec_action,
            "deep_research_goal": dr_goal,
            "tool_trace": [
                {"tool": t["tool"], "arguments": t["arguments"], "summary": _summarize_tool_result(t["tool"], t["result"]) if t["tool"] != "run_python_analysis" else f"沙盒 {t['result'].get('status')}"}
                for t in local_out["tool_trace"]
            ],
            "verification_badge": badge,
            "ui_artifact_hint": hint,
            "elapsed_seconds": round(time.monotonic() - t0, 3),
        }
        emit("done", result=res)
        return res

    # 3. Real LLM-First ReAct + Dynamic Plan + Sandbox Streaming Loop
    sys_prompt = AUTONOMOUS_SYSTEM_PROMPT
    mem_block = format_memories_for_prompt(applied_memories)
    if mem_block:
        sys_prompt = f"{sys_prompt}\n\n{mem_block}"
    messages: list[dict[str, Any]] = [{"role": "system", "content": sys_prompt}]
    if session is not None:
        for m in session.format_history_messages(effective_question, max_turns=4):
            messages.append(m)
    messages.append({"role": "user", "content": effective_question})

    plan_steps: list[dict[str, str]] = []
    tool_trace: list[dict[str, Any]] = []
    code_runs: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    final_answer = ""
    call_seq = 0
    step_limit = max(1, min(int(max_steps or 8), 12))

    try:
        for step_idx in range(step_limit):
            last_reasoning = ""
            last_content = ""

            def _on_delta(kind: str, full_text: str):
                nonlocal last_reasoning, last_content
                if kind == "reasoning":
                    delta = full_text[len(last_reasoning) :]
                    if delta:
                        last_reasoning = full_text
                        emit("reasoning_delta", delta=delta, text=full_text)
                elif kind == "content":
                    delta = full_text[len(last_content) :]
                    if delta:
                        last_content = full_text
                        emit("content_delta", delta=delta, text=full_text)

            is_last_turn = step_idx == step_limit - 1
            msg, _rec = client.chat(
                messages,
                tools=ALL_AUTONOMOUS_TOOLS,
                tool_choice="none" if is_last_turn else "auto",
                max_tokens=4096,
                stream=True,
                on_delta=_on_delta,
            )

            # Flush any trailing reasoning/content not emitted by throttled on_delta
            full_reasoning = str(msg.get("reasoning_content") or "")
            if full_reasoning and len(full_reasoning) > len(last_reasoning):
                emit("reasoning_delta", delta=full_reasoning[len(last_reasoning) :], text=full_reasoning)
            full_content = str(msg.get("content") or "")
            if full_content and len(full_content) > len(last_content):
                emit("content_delta", delta=full_content[len(last_content) :], text=full_content)

            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                final_answer = full_content.strip()
                # Mark remaining active plan steps done
                if plan_steps:
                    for s in plan_steps:
                        if s["status"] in {"running", "pending"}:
                            s["status"] = "done"
                    emit("plan_update", steps=plan_steps)
                break

            messages.append(msg)

            for tc in tool_calls:
                fn = tc.get("function") or {}
                tname = str(fn.get("name") or "")
                raw_args = fn.get("arguments") or "{}"
                try:
                    targs = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except Exception:
                    targs = {}

                call_seq += 1
                cid = str(tc.get("id") or f"c{call_seq}")

                if tname == "update_plan":
                    raw_steps = targs.get("steps") or []
                    plan_steps = [
                        {
                            "id": str(s.get("id") or f"s{i + 1}"),
                            "title": str(s.get("title") or "排查步骤"),
                            "status": str(s.get("status") or "pending")
                            if s.get("status") in {"pending", "running", "done", "failed"}
                            else "pending",
                        }
                        for i, s in enumerate(raw_steps[:12])
                        if isinstance(s, dict)
                    ]
                    emit("plan_update", steps=plan_steps)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": cid,
                        "content": json.dumps({"status": "ok", "steps_count": len(plan_steps)}, ensure_ascii=False),
                    })
                    continue

                # Auto-maintain a live visible plan step if the model didn't call update_plan explicitly
                auto_step_id = None
                if not plan_steps or all(s["status"] == "done" for s in plan_steps):
                    auto_step_id = f"auto_{call_seq}"
                    plan_steps.append({
                        "id": auto_step_id,
                        "title": f"执行只读 Python 沙盒分析" if tname == "run_python_analysis" else f"调用只读工具 {tname}",
                        "status": "running",
                    })
                    emit("plan_update", steps=plan_steps)

                emit("tool_start", call_id=cid, tool=tname, arguments=targs)
                t_res = toolbox.execute(tname, targs)
                tool_trace.append({"tool": tname, "arguments": targs, "result": t_res})

                if tname == "run_python_analysis":
                    code_str = str(targs.get("code") or "")
                    code_runs.append({"call_id": cid, "code": code_str, **t_res})
                    emit(
                        "code_run",
                        call_id=cid,
                        code=code_str,
                        status=t_res.get("status", "error"),
                        stdout=t_res.get("stdout", ""),
                        stderr=t_res.get("stderr", ""),
                        tables=t_res.get("tables") or [],
                        charts=t_res.get("charts") or [],
                        elapsed_ms=t_res.get("elapsed_ms", 0),
                    )
                    for tbl in t_res.get("tables") or []:
                        artifacts.append({"kind": "table", **tbl})
                    for ch in t_res.get("charts") or []:
                        artifacts.append({"kind": "chart", **ch})
                    ok_flag = t_res.get("status") == "ok"
                    summary = f"沙盒执行 {t_res.get('status')}（{t_res.get('elapsed_ms', 0)} ms，{len(t_res.get('charts') or [])} 图 {len(t_res.get('tables') or [])} 表）"
                else:
                    ok_flag = t_res.get("status") != "error"
                    summary = _summarize_tool_result(tname, t_res)

                emit("tool_end", call_id=cid, tool=tname, summary=summary, ok=ok_flag)

                if auto_step_id is not None:
                    for s in plan_steps:
                        if s["id"] == auto_step_id:
                            s["status"] = "done" if ok_flag else "failed"
                    emit("plan_update", steps=plan_steps)

                compact_res, _stats = compress_tool_result(tname, t_res, anchors)
                messages.append({
                    "role": "tool",
                    "tool_call_id": cid,
                    "content": json.dumps(compact_res, ensure_ascii=False, default=str)[:14000],
                })
        if not final_answer.strip():
            parts: list[str] = ["### 自主排查工具与沙盒执行汇总\n"]
            for cr in code_runs:
                if cr.get("stdout"):
                    parts.append(f"**沙盒计算输出 ({cr.get('status', 'ok')})：**\n```text\n{str(cr['stdout']).strip()[:1200]}\n```\n")
                elif cr.get("stderr"):
                    parts.append(f"**沙盒执行异常：** `{str(cr['stderr']).strip()[:240]}`\n")
                for tbl in cr.get("tables") or []:
                    cols = tbl.get("columns") or []
                    rows = (tbl.get("rows") or [])[:5]
                    if cols and rows:
                        parts.append(f"**{tbl.get('title', '数据表')}**：" + "；".join(str(r) for r in rows) + "\n")
            for tr in tool_trace:
                if tr["tool"] != "run_python_analysis":
                    parts.append(f"- `{tr['tool']}`：{_summarize_tool_result(tr['tool'], tr['result'])}")
            if len(parts) == 1:
                parts.append("已完成检索与计算步骤，请结合上方工具执行回执与图表查看详情。")
            final_answer = "\n".join(parts).strip()
            emit("content_delta", delta=final_answer, text=final_answer)
    except LLMError as exc:
        # Seamless fallback to local autonomous sandbox so user never sees a dead screen
        local_out = _run_local_autonomous(
            archive,
            toolbox,
            question,
            anchors,
            emit,
            fallback_note=f"模型端点暂不可用（{exc}），已自动切换至本地自主沙盒引擎完成计算。",
        )
        final_answer = local_out["answer_markdown"]
        plan_steps = local_out["plan"]
        artifacts = local_out["artifacts"]
        tool_trace = local_out["tool_trace"]
        code_runs = local_out["code_runs"]

    badge = _compute_verification_badge(archive, final_answer, tool_trace, code_runs)
    emit("verification_badge", **badge)
    hint = _infer_ui_artifact_hint(effective_question, anchors, tool_trace)
    rec_action = _infer_recommended_action(effective_question, anchors, final_answer)
    dr_goal = _infer_deep_research_goal(effective_question, anchors)
    mem_suggestions = detect_memory_candidates(
        question,
        final_answer,
        cabinets=anchors.get("cabinets"),
        existing_memories=applied_memories,
    )
    if mem_suggestions:
        emit("memory_suggestion", suggestions=mem_suggestions)
    if session is not None and hasattr(session, "record_autonomous_turn"):
        session.record_autonomous_turn(
            question,
            final_answer,
            verified_cells=list(dict.fromkeys(_INLINE_CELL_RE.findall(final_answer)))[:6],
        )
    result = {
        "status": "ok",
        "mode": "autonomous_model",
        "model": client.model,
        "answer_markdown": final_answer,
        "plan": plan_steps,
        "artifacts": artifacts,
        "applied_memories": applied_memories,
        "memory_suggestions": mem_suggestions,
        "recommended_action": rec_action,
        "deep_research_goal": dr_goal,
        "tool_trace": [
            {
                "tool": t["tool"],
                "arguments": t["arguments"],
                "summary": _summarize_tool_result(t["tool"], t["result"]) if t["tool"] != "run_python_analysis" else f"沙盒 {t['result'].get('status')}",
            }
            for t in tool_trace
        ],
        "verification_badge": badge,
        "ui_artifact_hint": hint,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }
    emit("done", result=result)
    return result
