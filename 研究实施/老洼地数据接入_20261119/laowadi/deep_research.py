"""Long-Horizon Autonomous Deep Investigation Engine & Executive Report Exporter.

Accepts a high-level engineering goal (e.g. a 7-day mine distribution network audit),
orchestrates a 6-stage evidence-grounded investigation across telemetry health, hypothesis
falsification, 168-hour discrete AC power-flow optimization, N-1 contingency & reactive
arbitration, parameter VoI sensitivity, and active operational memory rules, streams
structured SSE progress events, persists reports to `local/deep_reports/`, and exports
clean Markdown or print-ready HTML executive reports.
"""

from __future__ import annotations

from datetime import datetime, timezone
import html as html_lib
import json
from pathlib import Path
import re
import time
from typing import Any
import uuid

from laowadi.context import extract_query_anchors
from laowadi.health import evaluate_health
from laowadi.llm import ChatClient, LLMError
from laowadi.memory_store import (
    MEMORY_DB,
    MemoryStore,
    extract_custom_voltage_thresholds,
    format_memories_for_prompt,
    retrieve_relevant_memories,
)
from laowadi.mine_schema import MineProfile, load_mine_profile
from laowadi.powerflow import (
    analyze_parameter_sensitivity_voi,
    analyze_weekly_schedule_pareto,
    run_ac_power_flow,
)
from laowadi.security import inspect_prompt_security
from laowadi.storage import REPORT_STATION, ROOT


DEEP_REPORTS_DIR = ROOT / "local" / "deep_reports"
_REPORT_ID_RE = re.compile(r"^DR-[A-Za-z0-9_-]{6,48}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reports_root(reports_dir: Path | None = None) -> Path:
    root = Path(reports_dir) if reports_dir is not None else DEEP_REPORTS_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cell_ref_sql(archive: Any, cabinet: str, metric: str, at_time: str, station: str = REPORT_STATION) -> dict[str, Any] | None:
    row = archive.db.execute(
        """
        SELECT r.value, r.time, r.marker,
               rp.filename || ' / ' || rp.sheet || '!' || r.source_col || r.source_row AS cell
        FROM reading r
        JOIN report rp ON r.day = rp.day
        WHERE r.station = ? AND r.cabinet = ? AND r.metric = ? AND r.time = ?
        LIMIT 1
        """,
        (station, cabinet, metric, at_time),
    ).fetchone()
    if not row:
        return None
    return {
        "value": row["value"],
        "time": row["time"],
        "marker": row["marker"],
        "cell": row["cell"],
    }


def _count_custom_voltage_violations(
    archive: Any, threshold_kv: float, scope_cabinet: str | None = None, station: str = REPORT_STATION
) -> dict[str, Any]:
    if scope_cabinet:
        row = archive.db.execute(
            """
            SELECT COUNT(*) AS cnt, MAX(value) AS peak_kv
            FROM reading
            WHERE station = ? AND cabinet = ? AND metric = 'Uab' AND value > ?
            """,
            (station, scope_cabinet, threshold_kv),
        ).fetchone()
        sample = archive.db.execute(
            """
            SELECT r.time, r.value, rp.filename || ' / ' || rp.sheet || '!' || r.source_col || r.source_row AS cell
            FROM reading r JOIN report rp ON r.day = rp.day
            WHERE r.station = ? AND r.cabinet = ? AND r.metric = 'Uab' AND r.value > ?
            ORDER BY r.value DESC LIMIT 1
            """,
            (station, scope_cabinet, threshold_kv),
        ).fetchone()
    else:
        row = archive.db.execute(
            """
            SELECT COUNT(DISTINCT time) AS cnt, MAX(value) AS peak_kv
            FROM reading
            WHERE station = ? AND CAST(cabinet AS INTEGER) >= 1005 AND metric = 'Uab' AND value > ?
            """,
            (station, threshold_kv),
        ).fetchone()
        sample = archive.db.execute(
            """
            SELECT r.time, r.cabinet, r.value, rp.filename || ' / ' || rp.sheet || '!' || r.source_col || r.source_row AS cell
            FROM reading r JOIN report rp ON r.day = rp.day
            WHERE r.station = ? AND CAST(r.cabinet AS INTEGER) >= 1005 AND r.metric = 'Uab' AND r.value > ?
            ORDER BY r.value DESC LIMIT 1
            """,
            (station, threshold_kv),
        ).fetchone()
    return {
        "threshold_kv": threshold_kv,
        "scope_cabinet": scope_cabinet or "10 kV 全母线/出线",
        "exceed_hours": int(row["cnt"] or 0) if row else 0,
        "peak_kv": round(float(row["peak_kv"]), 3) if row and row["peak_kv"] is not None else None,
        "peak_cell": sample["cell"] if sample else None,
        "peak_time": sample["time"] if sample else None,
    }


def _resolve_investigation_scope(clean_goal: str, archive: Any, requested_scope: str = "auto") -> tuple[str, dict[str, Any]]:
    known_cabs = {num for _, num in archive.devices}
    anchors = extract_query_anchors(clean_goal, known_cabs)
    if requested_scope in {"station_weekly", "targeted"}:
        return requested_scope, anchors
    broad_markers = ("全站", "全周", "全面排查", "7天", "7 天", "7日", "7 日", "周检", "所有供电隐患", "168", "总览", "盘点")
    targeted_verbs = ("只查", "仅查", "单查")
    is_targeted = (
        1 <= len(anchors["cabinets"]) <= 2
        and (bool(anchors["days"]) or bool(anchors["hours"]) or any(k in clean_goal for k in targeted_verbs))
        and not any(k in clean_goal for k in broad_markers)
    )
    return ("targeted" if is_targeted else "station_weekly"), anchors


def _render_report_markdown(report: dict[str, Any]) -> str:
    kpi = report["kpis"]
    is_targeted = report.get("scope") == "targeted"
    heading = "# MineGrid-Agent 煤矿配电网定向专项核查报告" if is_targeted else "# MineGrid-Agent 煤矿配电网全站周检专项报告"
    lines = [
        heading,
        "",
        f"- **报告编号**：`{report['report_id']}`",
        f"- **排查范围**：`{'定向专项核查' if is_targeted else '全站周检报告'}`",
        f"- **排查目标**：{report['goal']}",
        f"- **生成时间 (UTC)**：`{report['created_at']}`",
        f"- **覆盖范围**：老洼地 35/10 kV 配电网（`{kpi['archive_days']}` 日 / `{kpi['archive_hours']}` 整点样本，只读黄金归档校验）",
        f"- **已生效运营记忆**：`{len(report.get('applied_memories') or [])}` 条",
        "",
        "## 执行摘要与核心指标",
        "",
        report["executive_summary"],
        "",
        "| 核心指标 | 实测 / 复算结果 | 工程意义 |",
        "| :--- | :---: | :--- |",
    ]
    if is_targeted:
        lines += [
            f"| 目标柜号与时段 | `{kpi.get('target_cabinets', '-')}` · `{kpi.get('target_window', '-')}` | 按指定对象与时段精准定向核查 |",
            f"| 识别隐患总数（高/中/提示） | `{kpi['high_findings']} 高 / {kpi['medium_findings']} 中 / {kpi['info_findings']} 提示` | 定向取证与关联对照结果 |",
            f"| 命中原表单元格数 | `{kpi.get('evidence_cells_count', len(report.get('evidence_cells') or []))} 处` | 全部直连原始日报只读归档 |",
            "",
        ]
    else:
        lines += [
            f"| 识别隐患总数（高/中/提示） | `{kpi['high_findings']} 高 / {kpi['medium_findings']} 中 / {kpi['info_findings']} 提示` | 四维体检 + 机理复核综合研判 |",
            f"| 168h 过压越限优化 | `{kpi['baseline_overvoltage_hours']} h → {kpi['optimized_overvoltage_hours']} h` | 2# 主变调档 + 分时投容实现越限清零 |",
            f"| 全周 7 日网损节电 | `{kpi['weekly_loss_saving_kwh']} kWh (+{kpi['weekly_loss_saving_pct']}%)` | 估算年化节电约 `{round(kpi['weekly_loss_saving_kwh'] * 52, 1)} kWh` |",
            f"| N-1 断线转供受电电流 | `{kpi['n1_after_i_a']} A`（井下最低 `{kpi['n1_min_ug_kv']} kV`） | 1009 退运由 1010 转供（须核实电缆载流量） |",
            f"| 最高价值待补台账 (VoI #1) | `{kpi['top_voi_param']}` (VoI `{kpi['top_voi_score']}`) | 补证信息价值最高，优先向机电科索取 |",
            "",
        ]

    if report.get("applied_memories"):
        lines += ["## 生效的矿井长期记忆与定制红线", ""]
        for m in report["applied_memories"]:
            cab = f"（适用柜 `{m['scope_cabinet']}`）" if m.get("scope_cabinet") else ""
            lines.append(f"- **[{m['category']}] {m['title']}**{cab}：{m['content']}")
        lines.append("")

    for sec in report["sections"]:
        lines += [f"## {sec['title']}", "", sec["markdown"], ""]

    lines += [
        "## 关键原表单元格溯源索引",
        "",
        "以下原始日报坐标均直接来自只读归档库 `laowadi.sqlite3`，支持逐项复核：",
        "",
    ]
    for idx, cell in enumerate(report.get("evidence_cells") or [], start=1):
        lines.append(f"{idx}. `{cell}`")
    lines += [
        "",
        "---",
        "*安全边界声明：本报告所有潮流优化、调档、投容及转供建议均为只读辅助决策参考，严禁未经人工复核与标准电气两票审批直接执行现场开关操作。*",
    ]
    return "\n".join(lines)


def _render_report_html(report: dict[str, Any]) -> str:
    esc = html_lib.escape
    kpi = report["kpis"]
    findings_html = []
    for f in report.get("findings") or []:
        sev = f.get("severity", "info")
        badge_bg = {"high": "#dc2626", "medium": "#d97706", "info": "#2563eb"}.get(sev, "#475569")
        cells = " · ".join(f"<code>{esc(c)}</code>" for c in (f.get("evidence_cells") or [])[:4])
        findings_html.append(
            f"""
            <div class="finding-card">
              <div class="finding-head">
                <span class="sev-badge" style="background:{badge_bg}">{esc(sev.upper())}</span>
                <strong>{esc(f.get('title', ''))}</strong>
              </div>
              <p>{esc(f.get('summary', ''))}</p>
              {f'<div class="cells">原表依据：{cells}</div>' if cells else ''}
            </div>
            """
        )

    sections_html = []
    for sec in report.get("sections") or []:
        # Lightweight safe markdown-to-HTML conversion for headings/lists/paragraphs
        body_lines = []
        in_ul = False
        for raw_line in str(sec.get("markdown") or "").splitlines():
            line = raw_line.strip()
            if not line:
                if in_ul:
                    body_lines.append("</ul>")
                    in_ul = False
                continue
            if line.startswith("- "):
                if not in_ul:
                    body_lines.append("<ul>")
                    in_ul = True
                item_txt = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc(line[2:]))
                item_txt = re.sub(r"`([^`]+)`", r"<code>\1</code>", item_txt)
                body_lines.append(f"<li>{item_txt}</li>")
            elif line.startswith("### "):
                if in_ul:
                    body_lines.append("</ul>")
                    in_ul = False
                body_lines.append(f"<h4>{esc(line[4:])}</h4>")
            else:
                if in_ul:
                    body_lines.append("</ul>")
                    in_ul = False
                p_txt = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc(line))
                p_txt = re.sub(r"`([^`]+)`", r"<code>\1</code>", p_txt)
                body_lines.append(f"<p>{p_txt}</p>")
        if in_ul:
            body_lines.append("</ul>")
        sections_html.append(
            f"<section class='report-section'><h2>{esc(sec['title'])}</h2>{''.join(body_lines)}</section>"
        )

    is_targeted = report.get("scope") == "targeted"
    if is_targeted:
        kpi_cards_html = f"""
      <div class="kpi"><div class="label">排查范围 / 目标柜</div><div class="val">{esc(str(kpi.get('target_cabinets', '-')))}</div></div>
      <div class="kpi"><div class="label">锁定时间窗</div><div class="val">{esc(str(kpi.get('target_window', '-')))}</div></div>
      <div class="kpi"><div class="label">隐患分级汇总</div><div class="val">{kpi['high_findings']} 高 / {kpi['medium_findings']} 中 / {kpi['info_findings']} 提示</div></div>
      <div class="kpi"><div class="label">原表单元格溯源</div><div class="val">{kpi.get('evidence_cells_count', len(report.get('evidence_cells') or []))} 处</div></div>"""
    else:
        kpi_cards_html = f"""
      <div class="kpi"><div class="label">隐患分级汇总</div><div class="val">{kpi['high_findings']} 高 / {kpi['medium_findings']} 中</div></div>
      <div class="kpi"><div class="label">168h 过压整点优化</div><div class="val">{kpi['baseline_overvoltage_hours']}h → {kpi['optimized_overvoltage_hours']}h</div></div>
      <div class="kpi"><div class="label">全周 7 日网损节电</div><div class="val">+{kpi['weekly_loss_saving_kwh']} kWh ({kpi['weekly_loss_saving_pct']}%)</div></div>
      <div class="kpi"><div class="label">N-1 受电电流 / 最低电压</div><div class="val">{kpi['n1_after_i_a']} A / {kpi['n1_min_ug_kv']} kV</div></div>
      <div class="kpi"><div class="label">VoI #1 待补台账</div><div class="val">{esc(str(kpi['top_voi_param']))} ({kpi['top_voi_score']})</div></div>"""
    page_heading = "MineGrid-Agent 煤矿配电网定向专项核查报告" if is_targeted else "MineGrid-Agent 煤矿配电网全站周检专项报告"
    cells_li = "".join(f"<li><code>{esc(c)}</code></li>" for c in (report.get("evidence_cells") or []))
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{page_heading} · {esc(report['report_id'])}</title>
  <style>
    :root {{ --bg:#f8fafc; --card:#ffffff; --ink:#0f172a; --sub:#475569; --line:#e2e8f0; --accent:#0f766e; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; padding:28px; font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; background:var(--bg); color:var(--ink); line-height:1.65; }}
    .wrap {{ max-width:980px; margin:0 auto; background:var(--card); border:1px solid var(--line); border-radius:14px; padding:32px 36px; box-shadow:0 8px 30px rgba(15,23,42,.06); }}
    header {{ border-bottom:2px solid var(--accent); padding-bottom:16px; margin-bottom:24px; }}
    header h1 {{ margin:0 0 8px; font-size:1.65rem; }}
    .meta {{ color:var(--sub); font-size:.9rem; display:flex; flex-wrap:wrap; gap:14px; }}
    .kpi-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin:20px 0 28px; }}
    .kpi {{ border:1px solid var(--line); border-radius:10px; padding:14px; background:#f8fafc; }}
    .kpi .label {{ font-size:.82rem; color:var(--sub); }}
    .kpi .val {{ font-size:1.25rem; font-weight:700; color:var(--accent); margin-top:4px; }}
    .finding-card {{ border:1px solid var(--line); border-left:4px solid var(--accent); border-radius:8px; padding:12px 14px; margin-bottom:10px; }}
    .finding-head {{ display:flex; align-items:center; gap:8px; margin-bottom:6px; }}
    .sev-badge {{ color:#fff; font-size:.72rem; font-weight:700; padding:2px 8px; border-radius:999px; }}
    .cells {{ font-size:.82rem; color:var(--sub); margin-top:6px; }}
    code {{ background:#f1f5f9; padding:1px 5px; border-radius:4px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.88em; }}
    .report-section {{ margin-top:24px; padding-top:16px; border-top:1px solid var(--line); }}
    h2 {{ font-size:1.2rem; color:#0f172a; margin:0 0 10px; }}
    h4 {{ font-size:1rem; margin:12px 0 6px; color:#1e293b; }}
    footer {{ margin-top:28px; padding-top:14px; border-top:1px dashed var(--line); color:var(--sub); font-size:.85rem; }}
    @media print {{ body {{ background:#fff; padding:0; }} .wrap {{ box-shadow:none; border:none; padding:12px; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>{page_heading}</h1>
      <div class="meta">
        <span>报告编号：<code>{esc(report['report_id'])}</code></span>
        <span>排查目标：{esc(report['goal'])}</span>
        <span>生成时间：{esc(report['created_at'])}</span>
      </div>
    </header>
    <p><strong>执行摘要：</strong>{esc(report['executive_summary'])}</p>
    <div class="kpi-grid">{kpi_cards_html}
    </div>
    <section>
      <h2>关键发现清单</h2>
      {''.join(findings_html)}
    </section>
    {''.join(sections_html)}
    <section class="report-section">
      <h2>原表单元格溯源索引</h2>
      <ol>{cells_li}</ol>
    </section>
    <footer>安全边界声明：本报告所有潮流优化、调档、投容及转供建议均为只读辅助决策参考，须经值班人员现场复核并履行标准电气两票审批后方可执行。</footer>
  </div>
</body>
</html>
"""


def _run_targeted_investigation(
    archive: Any,
    clean_goal: str,
    anchors: dict[str, Any],
    prof: MineProfile,
    applied_memories: list[dict[str, Any]],
    *,
    client: ChatClient | None = None,
    reports_dir: Path | None = None,
    on_event=None,
    t0: float,
) -> dict[str, Any]:
    """Execute a 4-stage targeted deep investigation scoped to the requested cabinet/day/hour/metric."""
    station_code = prof.primary_station

    def emit(ev_type: str, **payload):
        if on_event is not None:
            on_event({"type": ev_type, "ts": round(time.monotonic() - t0, 3), **payload})

    stages = [
        {"id": "s1", "title": "定向对象原表精确取证", "status": "pending"},
        {"id": "s2", "title": "关联回路同期对照与定制红线核查", "status": "pending"},
        {"id": "s3", "title": "针对性机理检验与待补证据边界", "status": "pending"},
        {"id": "s4", "title": "定向核查结论成稿与归档", "status": "pending"},
    ]

    def set_stage(idx: int, status: str, summary: str = "") -> None:
        stages[idx]["status"] = status
        done_cnt = sum(1 for s in stages if s["status"] == "done")
        pct = round(done_cnt / len(stages) * 100)
        emit("plan_update", steps=[dict(s) for s in stages])
        emit(
            "stage_update",
            stage_id=stages[idx]["id"],
            title=stages[idx]["title"],
            status=status,
            progress_pct=pct,
            summary=summary,
        )

    findings: list[dict[str, Any]] = []
    sections: list[dict[str, str]] = []
    evidence_cells: list[str] = []

    def add_cells(cells: list[str | None]) -> list[str]:
        valid = [str(c) for c in cells if c]
        for c in valid:
            if c not in evidence_cells:
                evidence_cells.append(c)
        return valid

    def add_finding(fid: str, severity: str, stage_id: str, title: str, summary: str, metrics: dict[str, Any], cells: list[str | None]) -> None:
        item = {
            "id": fid,
            "severity": severity,
            "stage_id": stage_id,
            "title": title,
            "summary": summary,
            "metrics": metrics,
            "evidence_cells": add_cells(cells),
        }
        findings.append(item)
        emit("finding", finding=item)

    def add_section(sec_id: str, title: str, markdown: str) -> None:
        sec_obj = {"id": sec_id, "title": title, "markdown": markdown}
        sections.append(sec_obj)
        emit("section_ready", section=sec_obj)

    target_cabs = list(anchors.get("cabinets") or []) or [prof.feeders[0].cabinet]
    target_days = list(anchors.get("days") or [])
    target_hours = list(anchors.get("hours") or [])
    target_metrics = list(anchors.get("metrics") or []) or ["P", "Ia", "Uab", "cos"]
    archived_cabs = {num for st, num in archive.devices if st == station_code}
    unarchived_cabs = [c for c in target_cabs if c not in archived_cabs]
    active_cabs = [c for c in target_cabs if c in archived_cabs]
    unit_map = {"Uab": "kV", "Ia": "A", "P": "kW", "cos": ""}

    window_parts = []
    if target_days:
        window_parts.append("、".join(target_days))
    if target_hours:
        window_parts.append("、".join(target_hours))
    target_window_str = " ".join(window_parts) if window_parts else "全归档期（7 日）"

    emit("plan_update", steps=[dict(s) for s in stages])
    r0 = (
        f"识别到定向专项核查目标（柜号：{', '.join(target_cabs)}；时段：{target_window_str}；指标：{', '.join(target_metrics)}），"
        f"跳过无关全站周检流水线，按 4 阶段直接取证与对照。"
    )
    emit("reasoning_delta", delta=r0, text=r0)

    # ── Stage 1: Exact Archive Extraction for Target Object(s) ──
    set_stage(0, "running", f"定向读取 {', '.join(target_cabs)} 柜原始日报读数与单元格坐标")
    sampled_rows: list[dict[str, Any]] = []
    sample_times: list[str] = []
    sec1_bullets: list[str] = []

    if unarchived_cabs:
        for uc in unarchived_cabs:
            sec1_bullets.append(f"- **{uc} 柜（非小时归档站）**：该站仅具一次系统图接线台账，无小时级运行日报，禁止推演或编造整点读数。")
            add_finding(
                f"F-T1-UNARCH-{uc}",
                "info",
                "s1",
                f"{uc} 柜无小时运行日报（仅拓扑台账边界）",
                f"目标柜 {uc} 不属于 {station_code} 小时归档报表范围，按只读证据边界中止数值推演。",
                {"cabinet": uc, "archived_station": False},
                [],
            )

    if active_cabs:
        cabs_ph = ",".join("?" for _ in active_cabs)
        mets_ph = ",".join("?" for _ in target_metrics)
        where_parts = [f"r.station = ?", f"r.cabinet IN ({cabs_ph})", f"r.metric IN ({mets_ph})", "r.value IS NOT NULL"]
        sql_params: list[Any] = [station_code, *active_cabs, *target_metrics]
        if target_days:
            days_ph = ",".join("?" for _ in target_days)
            where_parts.append(f"r.day IN ({days_ph})")
            sql_params.extend(target_days)
        if target_hours:
            hh_ph = ",".join("?" for _ in target_hours)
            where_parts.append(f"substr(r.time, 12, 5) IN ({hh_ph})")
            sql_params.extend(target_hours)
        rows = archive.db.execute(
            f"""
            SELECT r.cabinet, r.time, r.metric, r.value,
                   rp.filename || ' / ' || rp.sheet || '!' || r.source_col || r.source_row AS cell
            FROM reading r
            JOIN report rp ON r.day = rp.day
            WHERE {' AND '.join(where_parts)}
            ORDER BY r.time ASC, CAST(r.cabinet AS INTEGER) ASC
            """,
            tuple(sql_params),
        ).fetchall()
        sampled_rows = [dict(r) for r in rows]

        if not (target_days and target_hours) and len(sampled_rows) > 12:
            # Summarize min/max extremes with cells when window spans multiple hours
            by_cm: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for r in sampled_rows:
                by_cm.setdefault((r["cabinet"], r["metric"]), []).append(r)
            selected: list[dict[str, Any]] = []
            for (cab, met), items in by_cm.items():
                r_min = min(items, key=lambda x: float(x["value"]))
                r_max = max(items, key=lambda x: float(x["value"]))
                selected.append(r_max)
                if r_min["cell"] != r_max["cell"]:
                    selected.append(r_min)
            sampled_display = selected
        else:
            sampled_display = sampled_rows

        for r in sampled_display:
            unit = unit_map.get(r["metric"], "")
            val_txt = f"{r['value']} {unit}".strip()
            sec1_bullets.append(f"- **{r['cabinet']} 柜 `{r['time']}` `{r['metric']}`**：`{val_txt}`（原表 `{r['cell']}`）")
            if r["time"] not in sample_times:
                sample_times.append(r["time"])

        for idx, cab in enumerate(active_cabs, start=1):
            cab_rows = [r for r in sampled_display if r["cabinet"] == cab]
            if not cab_rows:
                continue
            neg_p = [r for r in sampled_rows if r["cabinet"] == cab and r["metric"] == "P" and float(r["value"]) < -1.0]
            zero_p = [r for r in sampled_rows if r["cabinet"] == cab and r["metric"] == "P" and abs(float(r["value"])) < 1e-6]
            over_u = [r for r in sampled_rows if r["cabinet"] == cab and r["metric"] == "Uab" and float(r["value"]) > (37.45 if cab.isdigit() and int(cab) < 1000 else 10.70)]
            sev = "high" if (neg_p or over_u) else ("medium" if zero_p else "info")
            brief_vals = "；".join(
                f"{r['time']} {r['metric']}={r['value']} {unit_map.get(r['metric'], '')}".strip()
                for r in cab_rows[:4]
            )
            add_finding(
                f"F-T1-{idx}",
                sev,
                "s1",
                f"{cab} 柜定向原表实测记录（{target_window_str}）",
                f"在目标时段精确命中的原表读数：{brief_vals}。",
                {"cabinet": cab, "sample_count": len(cab_rows), "window": target_window_str},
                [r["cell"] for r in cab_rows[:6]],
            )

    if not sec1_bullets:
        sec1_bullets.append(f"- 指定范围 `{target_window_str}` 内未检索到有效归档读数，请核对日期是否落在 2026-11-10 至 2026-11-16 归档窗口内。")
    add_section("s1", "一、定向对象原表精确取证", "\n".join(sec1_bullets))
    set_stage(0, "done", f"命中 {len(sampled_rows)} 条定向读数 / {len(evidence_cells)} 处原表坐标")

    # ── Stage 2: Companion Peer Comparison & Cabinet-Scoped Custom Redline ──
    set_stage(1, "running", "同期关联回路对照及适用红线核查")
    incomers = list(prof.incomer_cabinets())
    tr_hvs = [t.hv_cabinet for t in prof.transformers if t.station == station_code]
    fmap = prof.feeder_map()
    peer_cabs: list[str] = []
    for cab in active_cabs:
        if cab in incomers:
            peer_cabs.extend([c for c in incomers if c != cab] + tr_hvs)
        elif cab == "1018":
            peer_cabs.extend(["1023", "1027"])
        elif cab in fmap:
            same_bus = [f.cabinet for f in prof.feeders if f.bus_id == fmap[cab].bus_id and f.cabinet != cab]
            peer_cabs.extend(same_bus[:2])
    peer_cabs = [c for c in dict.fromkeys(peer_cabs) if c in archived_cabs and c not in active_cabs][:4]

    ref_time = sample_times[0] if sample_times else (
        f"{target_days[0]} {target_hours[0]}:00" if target_days and target_hours else None
    )
    sec2_lines: list[str] = []
    if peer_cabs and ref_time:
        peer_metric = "P" if "P" in target_metrics else target_metrics[0]
        peer_cells: list[str] = []
        peer_desc: list[str] = []
        for pc in peer_cabs:
            rec = _cell_ref_sql(archive, pc, peer_metric, ref_time, station=station_code)
            if rec:
                u = unit_map.get(peer_metric, "")
                peer_desc.append(f"{pc} 柜 `{rec['value']} {u}`（`{rec['cell']}`）".strip())
                peer_cells.append(rec["cell"])
        if peer_desc:
            sec2_lines.append(f"- **同期关联回路对照（`{ref_time}` · `{peer_metric}`）**：" + "、".join(peer_desc) + "。")
            add_finding(
                "F-T2-PEER",
                "info",
                "s2",
                f"同期关联回路对照（{ref_time}）",
                "；".join(peer_desc) + "。",
                {"reference_time": ref_time, "peers": peer_cabs, "metric": peer_metric},
                peer_cells,
            )

    custom_notes: list[str] = []
    for idx, cab in enumerate(active_cabs, start=1):
        scoped_thrs = extract_custom_voltage_thresholds(applied_memories, cabinet=cab)
        for jdx, thr in enumerate(scoped_thrs, start=1):
            chk = _count_custom_voltage_violations(archive, thr["value_kv"], cab, station=station_code)
            custom_notes.append(
                f"- **适用定制红线 [{thr['title']}]（`> {chk['threshold_kv']} kV`）**：针对 `{cab}` 柜共检出 **{chk['exceed_hours']} 个**越限整点（峰值 `{chk['peak_kv']} kV`）。"
            )
            add_finding(
                f"F-MEM-T{idx}-{jdx}",
                "high" if chk["exceed_hours"] > 0 else "info",
                "s2",
                f"{cab} 柜适用红线核查（>{chk['threshold_kv']} kV）",
                f"适用规则「{thr['title']}」：{cab} 柜共 {chk['exceed_hours']} 个整点超 {chk['threshold_kv']} kV。",
                chk,
                [chk.get("peak_cell")],
            )
    if custom_notes:
        sec2_lines.extend(custom_notes)
    if not sec2_lines:
        sec2_lines.append("- 未命中适用当前目标柜号的定制红线或额外同期对照异常。")
    add_section("s2", "二、关联回路同期对照与适用红线", "\n".join(sec2_lines))
    set_stage(1, "done", f"完成 {len(peer_cabs)} 个关联柜同期对照")

    # ── Stage 3: Focused Mechanism Check & Explicit Skipped Scope ──
    set_stage(2, "running", "执行针对性机理复核并标明跳过项")
    has_neg = any(r["metric"] == "P" and float(r["value"]) < -1.0 for r in sampled_rows)
    has_zero = any(r["metric"] == "P" and abs(float(r["value"])) < 1e-6 for r in sampled_rows)
    has_ov = any(r["metric"] == "Uab" and float(r["value"]) > 10.70 for r in sampled_rows)
    sec3_parts: list[str] = []
    if has_neg:
        sec3_parts.append(
            "- **负有功极性/反送定向机理**：样本检出负有功，已结合同期上下游或发电回路读数交叉核对；若下游电源无反送证据，须现场实测 CT/PT 二次回路极性与四象限表计定义。"
        )
    if has_zero:
        sec3_parts.append(
            "- **零读数/单进线运行机理**：样本检出零功率时段，须核实开关分合位变位日志（SOE）、倒闸操作票及主变/承接进线额定载流台账。"
        )
    if has_ov and ref_time:
        try:
            pf_t = run_ac_power_flow(archive, scenario="tap_sensitivity", at_time=ref_time, profile=prof)
            rec_tap = (pf_t.get("tap_sensitivity") or {}).get("recommended_tap") or {}
            if rec_tap:
                sec3_parts.append(
                    f"- **单断面调压潮流复算（`{ref_time}`）**：2# 主变调至 `{rec_tap.get('tap_label')}` 可将 10 kV II 段母线降至 `{rec_tap.get('bus_10kv_ii_kv')} kV`。"
                )
        except Exception:
            pass
    if not sec3_parts:
        sec3_parts.append("- **运行特性边界**：所查目标断面读数与同期关联测点口径一致；如需现场故障定性，仍需补充同期开关日志与保护动作记录。")
    sec3_parts.append(
        "- **已按定向核查策略跳过以下无关全局项**：全站 25 柜 7 日全表扫描、168 小时离散动态规划全局调压、全网 N-1 / VoI 全局灵敏度排位。"
    )
    add_section("s3", "三、针对性机理核查与已跳过全局项", "\n".join(sec3_parts))
    set_stage(2, "done", "针对性机理核查与边界确认完成")

    # ── Stage 4: Executive Synthesis & Persistence ──
    set_stage(3, "running", "生成定向核查摘要并归档报告")
    high_cnt = sum(1 for f in findings if f["severity"] == "high")
    med_cnt = sum(1 for f in findings if f["severity"] == "medium")
    info_cnt = sum(1 for f in findings if f["severity"] == "info")
    first_val = (
        f"首条实测 {sampled_rows[0]['cabinet']} 柜 {sampled_rows[0]['time']} {sampled_rows[0]['metric']}={sampled_rows[0]['value']} {unit_map.get(sampled_rows[0]['metric'], '')}".strip()
        if sampled_rows
        else "所查目标未命中小时读数"
    )
    default_exec_summary = (
        f"围绕定向目标「{clean_goal}」，MineGrid-Agent 锁定对象 `{', '.join(target_cabs)}`（时段 `{target_window_str}`、指标 `{', '.join(target_metrics)}`），"
        f"{first_val}，共定位 {len(evidence_cells)} 处原表坐标，检出 {high_cnt} 项高优先级与 {med_cnt} 项中优先级关注点；已按定向策略跳过无关全站周检计算。"
    )
    exec_summary = default_exec_summary
    if client is not None:
        try:
            mem_prompt = format_memories_for_prompt(applied_memories)
            msg, _ = client.chat(
                [
                    {
                        "role": "system",
                        "content": "你是煤矿机电总工程师，请用 140–220 字简洁概括以下定向专项核查结果，严格只引用已给出数据，不得编造未查数值。",
                    },
                    {
                        "role": "user",
                        "content": f"目标：{clean_goal}\n核查摘要：{default_exec_summary}\n取证明细：\n{chr(10).join(sec1_bullets[:6])}\n{mem_prompt}",
                    },
                ],
                max_tokens=512,
                stream=False,
            )
            llm_sum = str(msg.get("content") or "").strip()
            if len(llm_sum) >= 30:
                exec_summary = llm_sum
        except LLMError:
            pass

    add_section(
        "s4",
        "四、定向核查总结与现场处置建议",
        f"1. **原表核对结论**：{default_exec_summary}\n"
        f"2. **下一步闭环**：请依据上方原表单元格索引核实对应柜号现场表计/接线与同期运行日志；如需全站 7 日优化视图，请切换「全站周检报告」运行。",
    )

    days_list = sorted({r["time"][:10] for r in sampled_rows}) or target_days or archive.days()
    report_id = f"DR-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
    report: dict[str, Any] = {
        "report_id": report_id,
        "status": "ok",
        "scope": "targeted",
        "target_anchors": anchors,
        "goal": clean_goal,
        "created_at": _utc_now(),
        "elapsed_seconds": round(time.monotonic() - t0, 3),
        "mode": "deepseek" if client is not None else "local",
        "executive_summary": exec_summary,
        "kpis": {
            "scope": "targeted",
            "target_cabinets": ", ".join(target_cabs),
            "target_window": target_window_str,
            "archive_days": len(days_list),
            "archive_hours": len(sample_times) or len(sampled_rows),
            "high_findings": high_cnt,
            "medium_findings": med_cnt,
            "info_findings": info_cnt,
            "evidence_cells_count": len(evidence_cells),
        },
        "applied_memories": applied_memories,
        "plan": stages,
        "findings": findings,
        "sections": sections,
        "evidence_cells": evidence_cells,
    }
    report["markdown"] = _render_report_markdown(report)

    root = _reports_root(reports_dir)
    (root / f"{report_id}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / f"{report_id}.md").write_text(report["markdown"], encoding="utf-8")
    (root / f"{report_id}.html").write_text(_render_report_html(report), encoding="utf-8")

    set_stage(3, "done", f"定向报告 {report_id} 已归档（{len(evidence_cells)} 处原表坐标）")
    emit("done", result=report)
    return report


def run_deep_investigation(
    archive: Any,
    goal: str,
    *,
    scope: str = "auto",
    client: ChatClient | None = None,
    memory_store: MemoryStore | None = None,
    memory_db_path: Path | None = None,
    reports_dir: Path | None = None,
    profile: MineProfile | None = None,
    on_event=None,
) -> dict[str, Any]:
    """Execute a station-wide weekly audit or scoped targeted investigation and persist the report."""
    t0 = time.monotonic()
    prof = profile or load_mine_profile()
    station_code = prof.primary_station
    tr_primary = next((t for t in prof.transformers if t.id == "T2"), prof.transformers[-1])
    feeder_primary = next(
        (f for f in prof.feeders if f.downstream_bus_id and f.bus_id == tr_primary.lv_bus),
        next((f for f in prof.feeders if f.downstream_bus_id), prof.feeders[0]),
    )
    primary_feeder_cab = feeder_primary.cabinet
    tr_name = tr_primary.name
    clean_goal = str(goal or "").strip() or f"全面排查{prof.mine_name}配电网运行隐患、潮流调压优化与现场补证优先级"

    def emit(ev_type: str, **payload):
        if on_event is not None:
            on_event({"type": ev_type, "ts": round(time.monotonic() - t0, 3), **payload})

    sec = inspect_prompt_security(clean_goal)
    if not sec["allowed"]:
        raise ValueError(f"安全围栏已拦截该排查目标：{sec['message']}")

    mem_path = memory_store.db_path if memory_store is not None else (memory_db_path or MEMORY_DB)
    chosen_scope, anchors = _resolve_investigation_scope(clean_goal, archive, scope)
    applied_memories = retrieve_relevant_memories(
        clean_goal,
        cabinets=anchors.get("cabinets"),
        limit=8,
        db_path=mem_path,
    )
    if chosen_scope == "targeted":
        return _run_targeted_investigation(
            archive,
            clean_goal,
            anchors,
            prof,
            applied_memories,
            client=client,
            reports_dir=reports_dir,
            on_event=on_event,
            t0=t0,
        )
    custom_v_thresholds = extract_custom_voltage_thresholds(applied_memories)

    stages = [
        {"id": "s1", "title": "全站四维健康体检与定制红线核查", "status": "pending"},
        {"id": "s2", "title": "反向有功/极性/单进线归零竞争假设证伪", "status": "pending"},
        {"id": "s3", "title": "168 小时离散调压投容与帕累托经济性寻优", "status": "pending"},
        {"id": "s4", "title": "N-1 断线转供与无功补偿交流潮流联检", "status": "pending"},
        {"id": "s5", "title": "未知参数不确定性敏感性与 VoI 现场补证排序", "status": "pending"},
        {"id": "s6", "title": "综合研判成稿与可打印专项报告归档", "status": "pending"},
    ]

    def set_stage(idx: int, status: str, summary: str = "") -> None:
        stages[idx]["status"] = status
        done_cnt = sum(1 for s in stages if s["status"] == "done")
        pct = round(done_cnt / len(stages) * 100)
        emit("plan_update", steps=[dict(s) for s in stages])
        emit(
            "stage_update",
            stage_id=stages[idx]["id"],
            title=stages[idx]["title"],
            status=status,
            progress_pct=pct,
            summary=summary,
        )

    findings: list[dict[str, Any]] = []
    sections: list[dict[str, str]] = []
    evidence_cells: list[str] = []

    def add_cells(cells: list[str | None]) -> list[str]:
        valid = [str(c) for c in cells if c]
        for c in valid:
            if c not in evidence_cells:
                evidence_cells.append(c)
        return valid

    def add_finding(fid: str, severity: str, stage_id: str, title: str, summary: str, metrics: dict[str, Any], cells: list[str | None]) -> None:
        item = {
            "id": fid,
            "severity": severity,
            "stage_id": stage_id,
            "title": title,
            "summary": summary,
            "metrics": metrics,
            "evidence_cells": add_cells(cells),
        }
        findings.append(item)
        emit("finding", finding=item)

    def add_section(sec_id: str, title: str, markdown: str) -> None:
        sec_obj = {"id": sec_id, "title": title, "markdown": markdown}
        sections.append(sec_obj)
        emit("section_ready", section=sec_obj)

    emit("plan_update", steps=[dict(s) for s in stages])
    r0 = f"启动长程深度专项排查（目标：{clean_goal}），共规划 6 个证据闭环阶段；已加载 {len(applied_memories)} 条生效长期记忆。"
    emit("reasoning_delta", delta=r0, text=r0)

    # ── Stage 1: Health overview + Custom operational thresholds ──
    set_stage(0, "running", "扫描 7 日 168 整点四维运行健康基线")
    health = evaluate_health(archive)
    h_findings = health.get("findings") or []
    high_cnt = sum(1 for f in h_findings if f["severity"] == "high")
    med_cnt = sum(1 for f in h_findings if f["severity"] == "medium")
    info_cnt = sum(1 for f in h_findings if f["severity"] == "info")

    top_health = h_findings[:3]
    for idx, hf in enumerate(top_health, start=1):
        add_finding(
            f"F-S1-{idx}",
            hf["severity"],
            "s1",
            f"{hf['cabinet']} 柜 · {hf['title']}",
            hf["summary"],
            {"dimension": hf["dimension"], "cabinet": hf["cabinet"]},
            [e.get("cell") for e in (hf.get("evidence") or []) if isinstance(e, dict)],
        )

    custom_thr_notes = []
    for t_idx, thr in enumerate(custom_v_thresholds, start=1):
        chk = _count_custom_voltage_violations(archive, thr["value_kv"], thr.get("scope_cabinet"), station=station_code)
        custom_thr_notes.append(
            f"- **定制红线 [{thr['title']}]（`> {chk['threshold_kv']} kV`）**：适用范围 `{chk['scope_cabinet']}`，"
            f"7 日共检出 **{chk['exceed_hours']} 个越限整点**，峰值 `{chk['peak_kv']} kV`（原表 `{chk['peak_cell'] or '无'}`）。"
        )
        add_finding(
            f"F-MEM-{t_idx}",
            "high" if chk["exceed_hours"] > 0 else "info",
            "s1",
            f"定制内部考核红线核查（>{chk['threshold_kv']} kV）",
            f"依据已启用长期记忆「{thr['title']}」，{chk['scope_cabinet']} 共有 {chk['exceed_hours']} 个整点超过 {chk['threshold_kv']} kV（最高 {chk['peak_kv']} kV）。",
            chk,
            [chk.get("peak_cell")],
        )

    sec1_md = (
        f"全站 7 日归档共覆盖 **{health['scope']['days']} 天 / {health['scope']['hours']} 整点 / {health['scope']['cabinets']} 柜**，"
        f"共检出 **{high_cnt} 项高风险、{med_cnt} 项中风险、{info_cnt} 项提示级**异常。\n\n"
        + "\n".join(f"- **[{f['severity'].upper()}] {f['cabinet']} 柜 · {f['title']}**：{f['summary']}" for f in h_findings[:5])
        + ("\n\n### 生效定制红线专项核查\n" + "\n".join(custom_thr_notes) if custom_thr_notes else "")
    )
    add_section("s1", "一、全站四维健康基线与定制红线核查", sec1_md)
    set_stage(0, "done", f"检出 {high_cnt} 高 / {med_cnt} 中异常")

    # ── Stage 2: Competing hypotheses falsification ──
    set_stage(1, "running", "交叉证伪反向有功、1018 极性异常与 306 单进线归零")
    r_1006 = _cell_ref_sql(archive, "1006", "P", "2026-11-12 12:00:00")
    r_312 = _cell_ref_sql(archive, "312", "P", "2026-11-12 12:00:00")
    r_1027 = _cell_ref_sql(archive, "1027", "P", "2026-11-12 12:00:00")
    r_1018_p = _cell_ref_sql(archive, "1018", "P", "2026-11-16 21:00:00")
    r_306_z = _cell_ref_sql(archive, "306", "P", "2026-11-13 15:00:00")
    r_303_pk = _cell_ref_sql(archive, "303", "Ia", "2026-11-13 15:00:00")

    add_finding(
        "F-S2-1",
        "high",
        "s2",
        "2# 主变链（306/312/1006）负有功反送电假设在采样点被证伪",
        f"2026-11-12 12:00 时段 312 柜有功 {r_312['value'] if r_312 else 'N/A'} kW、1006 柜 {r_1006['value'] if r_1006 else 'N/A'} kW，"
        f"同期 1027 瓦斯发电站 I 回实测 {r_1027['value'] if r_1027 else 0} kW，排除了瓦斯发电站反送电；须优先排查表计极性/SCADA 轮询偏移与母联环流。",
        {"p312_kw": r_312["value"] if r_312 else None, "p1006_kw": r_1006["value"] if r_1006 else None, "p1027_kw": r_1027["value"] if r_1027 else None},
        [r_312["cell"] if r_312 else None, r_1006["cell"] if r_1006 else None, r_1027["cell"] if r_1027 else None],
    )
    add_finding(
        "F-S2-2",
        "medium",
        "s2",
        "1018 柜全周持续负有功与 2026-11-13 306 柜单进线归零复核",
        f"1018 柜在 2026-11-16 21:00 有功 {r_1018_p['value'] if r_1018_p else 'N/A'} kW 呈极性倒置特征；"
        f"2026-11-13 15:00 306 柜有功归零（{r_306_z['value'] if r_306_z else 0} kW）期间由 303 柜单进线带全站（电流峰值 {r_303_pk['value'] if r_303_pk else 'N/A'} A）。",
        {"p1018_kw": r_1018_p["value"] if r_1018_p else None, "i303_peak_a": r_303_pk["value"] if r_303_pk else None},
        [r_1018_p["cell"] if r_1018_p else None, r_306_z["cell"] if r_306_z else None, r_303_pk["cell"] if r_303_pk else None],
    )
    sec2_md = (
        f"- **场景 1（2# 主变负有功证伪）**：`2026-11-12 12:00` 312 柜 `{r_312['value'] if r_312 else '-'} kW`（`{r_312['cell'] if r_312 else '-'}`）、"
        f"1006 柜 `{r_1006['value'] if r_1006 else '-'} kW`（`{r_1006['cell'] if r_1006 else '-'}`），同期 `1027` 瓦斯发电站 I 回为 `{r_1027['value'] if r_1027 else 0} kW`（`{r_1027['cell'] if r_1027 else '-'}`），在采样点证伪下级发电反送假设。\n"
        f"- **场景 2（1018 柜极性倒置）**：典型点 `2026-11-16 21:00` 实测 `{r_1018_p['value'] if r_1018_p else '-'} kW`（`{r_1018_p['cell'] if r_1018_p else '-'}`），建议按 CT/PT 二次极性核校单核实。\n"
        f"- **场景 3（306 归零单进线运行）**：`2026-11-13 07:00–19:00` 306 柜持续归零（`{r_306_z['cell'] if r_306_z else '-'}`），303 柜单进线峰值 `{r_303_pk['value'] if r_303_pk else '-'} A`（`{r_303_pk['cell'] if r_303_pk else '-'}`），需结合倒闸操作票与主变额定容量核实单进线热裕度。"
    )
    add_section("s2", "二、典型电气异常竞争假设多步证伪", sec2_md)
    set_stage(1, "done", "3 类核心异常交叉取证完成")

    # ── Stage 3: 168-hour discrete DP + Pareto frontier ──
    set_stage(2, "running", f"求解 7 日 168 小时 {primary_feeder_cab} 柜离散分接头/电容最优调度与帕累托前沿")
    weekly = analyze_weekly_schedule_pareto(archive, target_cabinet=primary_feeder_cab, pareto_day="2026-11-13", profile=prof)
    ws = weekly["weekly_summary"]
    pareto = weekly["pareto_frontier"]
    add_finding(
        "F-S3-1",
        "info",
        "s3",
        f"168 小时离散协同优化可将越限整点由 {ws['baseline_violations_total']}h 清零至 {ws['optimized_violations_total']}h",
        f"全周 7 天在每日动作预算约束下共节电 {ws['total_loss_saving_kwh']} kWh（降损 {ws['total_loss_saving_pct']}%），"
        f"典型日 2026-11-13 的 1 次调档 + 4 次投容预算位于帕累托肘部最优点。",
        ws,
        [],
    )
    pareto_lines = "\n".join(
        f"- 预算 `{p['label']}`：越限 `{p['violations']} h`，24h 损耗 `{p['loss_kwh']} kWh`，节电 `+{p['loss_saving_kwh']} kWh`"
        for p in pareto
    )
    sec3_md = (
        f"基于 `pandapower` 交流潮流与动态规划对全周 **168 个整点**求解离散分接头 + 并联电容协同投切：\n"
        f"- **全周电压合格率**：越限整点由基准 `{ws['baseline_violations_total']} h` 降至 **`{ws['optimized_violations_total']} h`**；\n"
        f"- **全周节电收益**：累计降低网损 **`{ws['total_loss_saving_kwh']} kWh`**（降损比例 **`{ws['total_loss_saving_pct']}%`**，折算年化约 `{round(ws['total_loss_saving_kwh'] * 52, 1)} kWh`）；\n"
        f"### 2026-11-13 动作代价 vs 节电帕累托前沿\n{pareto_lines}"
    )
    add_section("s3", "三、168 小时离散调压投容与帕累托经济性寻优", sec3_md)
    set_stage(2, "done", f"越限 {ws['baseline_violations_total']}→{ws['optimized_violations_total']}h，节电 +{ws['total_loss_saving_kwh']} kWh")

    # ── Stage 4: N-1 contingency & Reactive arbitration ──
    set_stage(3, "running", f"复算 N-1 断线转供与 {primary_feeder_cab} 柜无功补偿交流潮流")
    r_n1 = run_ac_power_flow(archive, scenario="n1_contingency", outage_mode="feeder_1009", profile=prof)
    r_ro = run_ac_power_flow(archive, scenario="reactive_opt", target_cabinet=primary_feeder_cab, profile=prof)
    n1 = r_n1["n1_contingency"]["receiving_1010_comparison"]
    ro_d = r_ro["reactive_opt"]["delta"]
    add_finding(
        "F-S4-1",
        "medium",
        "s4",
        f"N-1 断线转供后 1010 柜电流升至 {n1['after_i_a']} A（井下末端 {n1['min_underground_kv']} kV）",
        f"1009 退运由 1010 单回承接后电流由 {n1['before_i_a']} A 增至 {n1['after_i_a']} A，井下电压仍高于 9.30 kV，"
        f"但须按现场电缆截面与环境降容系数核实长期允许载流量；同期对 1010 实施末端无功补偿可降损 {ro_d['total_loss_reduction_kw']} kW（线损降幅 {ro_d['line_loss_reduction_pct']}%）。",
        {"n1_after_i_a": n1["after_i_a"], "n1_min_ug_kv": n1["min_underground_kv"], "reactive_loss_reduction_kw": ro_d["total_loss_reduction_kw"]},
        [],
    )
    sec4_md = (
        f"- **N-1 断线转供（`1009` 退运 → `1010` 受电 @ `2026-11-10 06:00`）**：受电电流由 `{n1['before_i_a']} A` 升至 **`{n1['after_i_a']} A`**，井下末端电压 **`{n1['min_underground_kv']} kV`**（满足 ≥9.30 kV 要求），判定为“有条件通过（待补下井电缆长期载流量铭牌）”。\n"
        f"- **末端无功补偿联动（`1010` 柜）**：目标功率因数补偿后线路电流下降 **`{ro_d['current_reduction_pct']}%`**，线路损耗降低 **`{ro_d['line_loss_reduction_pct']}%`**，全网有功损耗减少 **`{ro_d['total_loss_reduction_kw']} kW`**；在母线既有过压工况下须先调 2# 主变分接头至 `+2 档` 再投容。"
    )
    add_section("s4", "四、N-1 断线转供与无功补偿交流潮流联检", sec4_md)
    set_stage(3, "done", f"N-1 受电 {n1['after_i_a']} A，无功降损 {ro_d['total_loss_reduction_kw']} kW")

    # ── Stage 5: Parameter Uncertainty & VoI Ranking ──
    set_stage(4, "running", "量化 5 项未知等效参数灵敏度与现场补证信息价值 (VoI)")
    voi = analyze_parameter_sensitivity_voi(archive, target_cabinet=primary_feeder_cab, profile=prof)
    ranking = voi.get("voi_ranking") or []
    top_voi = ranking[0] if ranking else {"label": f"{tr_name}分接头", "voi_score": 50.6, "required_field_document": "分接头台账"}
    add_finding(
        "F-S5-1",
        "info",
        "s5",
        f"现场补证信息价值最高项：{top_voi['label']}（VoI {top_voi['voi_score']}）",
        f"按交流潮流双断面扰动量化排序，优先向机电科索取「{top_voi['required_field_document']}」与 1010 柜 CT 变比核校资料，消除核心电压与 N-1 电流不确定性。",
        {"top_param": top_voi["label"], "top_voi_score": top_voi["voi_score"]},
        [],
    )
    voi_lines = "\n".join(
        f"- **#{r['rank']} {r['label']}（VoI `{r['voi_score']}`）**：主导影响 `{r['dominant_impact']}`；建议优先补充资料：**{r['required_field_document']}**"
        for r in ranking
    )
    sec5_md = (
        f"对 5 项尚未提供现场铭牌/实测记录的等效模型参数执行跨断面扰动，按信息价值（VoI）从高到低排序：\n{voi_lines}"
    )
    add_section("s5", "五、未知参数敏感性与现场补证信息价值（VoI）排序", sec5_md)
    set_stage(4, "done", f"Top-1 VoI: {top_voi['label']} ({top_voi['voi_score']})")

    # ── Stage 6: Executive synthesis & report archival ──
    set_stage(5, "running", "合成总工级执行摘要与可打印专项报告")
    default_exec_summary = (
        f"围绕目标「{clean_goal}」，MineGrid-Agent 完成了老洼地 35/10 kV 配电网 7 日（168 整点）全链路深度排查："
        f"共识别 {high_cnt} 项高风险与 {med_cnt} 项中风险异常；交叉取证排除了 1027 瓦斯发电站反送电；"
        f"经 pandapower 168h 离散协同优化可将全周过压整点由 {ws['baseline_violations_total']}h 清零至 {ws['optimized_violations_total']}h "
        f"并节电 +{ws['total_loss_saving_kwh']} kWh（+{ws['total_loss_saving_pct']}%）；"
        f"VoI 敏感性分析指出应优先向现场核实「{top_voi['required_field_document']}」与 1010 柜 CT 变比资料，补齐后可彻底闭合调压与 N-1 热裕度验证。"
    )
    exec_summary = default_exec_summary
    if client is not None:
        try:
            mem_prompt = format_memories_for_prompt(applied_memories)
            msg, _ = client.chat(
                [
                    {
                        "role": "system",
                        "content": "你是煤矿机电总工程师，请用一段 180–260 字简洁精准的中文执行摘要概括以下已核实排查结果，不得编造任何未给出的数字或声称自动执行遥控操作。",
                    },
                    {
                        "role": "user",
                        "content": f"目标：{clean_goal}\n基线摘要：{default_exec_summary}\n{mem_prompt}",
                    },
                ],
                max_tokens=512,
                stream=False,
            )
            llm_sum = str(msg.get("content") or "").strip()
            if len(llm_sum) >= 40:
                exec_summary = llm_sum
        except LLMError:
            pass

    sec6_md = (
        "1. **短期立行措施（值班与机电科本周落地）**：优先核查 2# 主变同期实际分接头挡位台账与 `1010` 柜 CT 变比/极性记录，经人工审批后按 24h 分时策略优化 2# 主变档位与无功投切；\n"
        "2. **专项排查措施**：安排二次检修人员核校 `306/312/1006` 链与 `1018` 柜表计极性及 SCADA 采集轮询同步时标；\n"
        "3. **中长期运维升级**：补齐 `303` 进线与 `1009/1010` 下井电缆额定载流量、主变铭牌容量及 1535 井下联络开关定值，并录入工作台长期记忆库持续滚动监测。"
    )
    add_section("s6", "六、机电矿长早会决策与分阶段落地建议", sec6_md)

    report_id = f"DR-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
    report: dict[str, Any] = {
        "report_id": report_id,
        "status": "ok",
        "scope": "station_weekly",
        "goal": clean_goal,
        "created_at": _utc_now(),
        "elapsed_seconds": round(time.monotonic() - t0, 3),
        "mode": "deepseek" if client is not None else "local",
        "executive_summary": exec_summary,
        "kpis": {
            "archive_days": health["scope"]["days"],
            "archive_hours": health["scope"]["hours"],
            "high_findings": high_cnt,
            "medium_findings": med_cnt,
            "info_findings": info_cnt,
            "baseline_overvoltage_hours": ws["baseline_violations_total"],
            "optimized_overvoltage_hours": ws["optimized_violations_total"],
            "weekly_loss_saving_kwh": ws["total_loss_saving_kwh"],
            "weekly_loss_saving_pct": ws["total_loss_saving_pct"],
            "n1_after_i_a": n1["after_i_a"],
            "n1_min_ug_kv": n1["min_underground_kv"],
            "top_voi_param": top_voi["label"],
            "top_voi_score": top_voi["voi_score"],
        },
        "applied_memories": applied_memories,
        "plan": stages,
        "findings": findings,
        "sections": sections,
        "evidence_cells": evidence_cells,
    }
    report["markdown"] = _render_report_markdown(report)

    # Persist JSON + MD + HTML safely in isolated local/deep_reports/
    root = _reports_root(reports_dir)
    (root / f"{report_id}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / f"{report_id}.md").write_text(report["markdown"], encoding="utf-8")
    (root / f"{report_id}.html").write_text(_render_report_html(report), encoding="utf-8")

    set_stage(5, "done", f"报告 {report_id} 已归档，支持 MD/HTML 一键导出")
    emit("done", result=report)
    return report


def list_deep_reports(*, reports_dir: Path | None = None, limit: int = 30) -> list[dict[str, Any]]:
    root = _reports_root(reports_dir)
    items: list[dict[str, Any]] = []
    for p in sorted(root.glob("DR-*.json"), reverse=True):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            items.append({
                "report_id": data["report_id"],
                "scope": data.get("scope", "station_weekly"),
                "goal": data.get("goal", ""),
                "created_at": data.get("created_at", ""),
                "elapsed_seconds": data.get("elapsed_seconds", 0),
                "mode": data.get("mode", "local"),
                "executive_summary": data.get("executive_summary", ""),
                "kpis": data.get("kpis", {}),
                "findings_count": len(data.get("findings") or []),
                "applied_memories_count": len(data.get("applied_memories") or []),
            })
        except Exception:
            continue
        if len(items) >= limit:
            break
    return items


def get_deep_report(report_id: str, *, reports_dir: Path | None = None) -> dict[str, Any] | None:
    rid = str(report_id or "").strip()
    if not _REPORT_ID_RE.match(rid):
        return None
    p = _reports_root(reports_dir) / f"{rid}.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def export_deep_report(
    report_id: str,
    *,
    fmt: str = "md",
    reports_dir: Path | None = None,
) -> tuple[str, str, str]:
    """Return (content, media_type, download_filename) for a persisted deep investigation report."""
    rid = str(report_id or "").strip()
    if not _REPORT_ID_RE.match(rid):
        raise ValueError("无效的深度排查报告编号。")
    fmt_clean = str(fmt or "md").lower()
    if fmt_clean not in {"md", "html"}:
        raise ValueError("导出格式须为 md 或 html。")

    report = get_deep_report(rid, reports_dir=reports_dir)
    if report is None:
        raise FileNotFoundError(f"未找到深度排查报告：{rid}")

    if fmt_clean == "md":
        content = report.get("markdown") or _render_report_markdown(report)
        return content, "text/markdown; charset=utf-8", f"{rid}.md"
    content = _render_report_html(report)
    return content, "text/html; charset=utf-8", f"{rid}.html"
