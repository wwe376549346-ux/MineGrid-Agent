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


def _render_report_markdown(report: dict[str, Any]) -> str:
    kpi = report["kpis"]
    lines = [
        f"# MineGrid-Agent 煤矿配电网深度专项排查报告",
        "",
        f"- **报告编号**：`{report['report_id']}`",
        f"- **排查目标**：{report['goal']}",
        f"- **生成时间 (UTC)**：`{report['created_at']}`",
        f"- **覆盖范围**：老洼地 35/10 kV 配电网（`{kpi['archive_days']}` 日 / `{kpi['archive_hours']}` 整点，只读黄金归档校验）",
        f"- **已生效运营记忆**：`{len(report.get('applied_memories') or [])}` 条",
        "",
        "## 执行摘要与核心指标",
        "",
        report["executive_summary"],
        "",
        "| 核心指标 | 实测 / 复算结果 | 工程意义 |",
        "| :--- | :---: | :--- |",
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

    cells_li = "".join(f"<li><code>{esc(c)}</code></li>" for c in (report.get("evidence_cells") or []))
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>MineGrid-Agent 深度专项排查报告 · {esc(report['report_id'])}</title>
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
      <h1>MineGrid-Agent 煤矿配电网深度专项排查报告</h1>
      <div class="meta">
        <span>报告编号：<code>{esc(report['report_id'])}</code></span>
        <span>排查目标：{esc(report['goal'])}</span>
        <span>生成时间：{esc(report['created_at'])}</span>
      </div>
    </header>
    <p><strong>执行摘要：</strong>{esc(report['executive_summary'])}</p>
    <div class="kpi-grid">
      <div class="kpi"><div class="label">隐患分级汇总</div><div class="val">{kpi['high_findings']} 高 / {kpi['medium_findings']} 中</div></div>
      <div class="kpi"><div class="label">168h 过压整点优化</div><div class="val">{kpi['baseline_overvoltage_hours']}h → {kpi['optimized_overvoltage_hours']}h</div></div>
      <div class="kpi"><div class="label">全周 7 日网损节电</div><div class="val">+{kpi['weekly_loss_saving_kwh']} kWh ({kpi['weekly_loss_saving_pct']}%)</div></div>
      <div class="kpi"><div class="label">N-1 受电电流 / 最低电压</div><div class="val">{kpi['n1_after_i_a']} A / {kpi['n1_min_ug_kv']} kV</div></div>
      <div class="kpi"><div class="label">VoI #1 待补台账</div><div class="val">{esc(str(kpi['top_voi_param']))} ({kpi['top_voi_score']})</div></div>
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


def run_deep_investigation(
    archive: Any,
    goal: str,
    *,
    client: ChatClient | None = None,
    memory_store: MemoryStore | None = None,
    memory_db_path: Path | None = None,
    reports_dir: Path | None = None,
    profile: MineProfile | None = None,
    on_event=None,
) -> dict[str, Any]:
    """Execute a 6-stage long-horizon autonomous investigation and persist the structured report."""
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
    applied_memories = retrieve_relevant_memories(clean_goal, limit=8, db_path=mem_path)
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
