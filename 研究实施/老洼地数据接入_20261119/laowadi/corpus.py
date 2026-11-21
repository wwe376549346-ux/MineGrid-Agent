"""Stage 08: Existing Laowadi materials ledger, answerability matrix, and 9-question provenance check."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from laowadi.diagnose import diagnose_question
from laowadi.health import evaluate_health
from laowadi.monitor import connect as connect_db
from laowadi.questions import QuestionSession
from laowadi.standards import load_corpus
from laowadi.storage import REPORT_STATION


ANSWERABILITY_MATRIX = {
    "answerable": [
        "35 kV 变电所 25 个柜在 2026-11-10 至 2026-11-16 共 168 个整点的 Uab、Ia、P、cos 读数及原表单元格坐标。",
        "日报页尾原表统计行（最大值、最小值、平均值，第 28–30 行）与基于 24 个可见整点重算的统计量双口径对照。",
        "两张监控截图（35KV变电所.png、中央变电所.png）登记的 40 个柜号及 1009→1101、1010→1201 两条站间图示供电连接。",
        "基于已有读数派生的四柜整点回放事件（106 条）及全站 25 柜四维电气体检发现（25 项，含三相功率自洽比例、10 kV I/II 段电压聚类、单进线带双主变时段、2#主变反向有功、SCADA 轮询偏移与分时段基线偏离）。",
        "本地归档的《煤矿安全规程（2025）》电气与监控章节 63 条原文及 5 张已核 GB/T 50417-2017、GB/T 12325-2008 参考卡。",
    ],
    "condition_mismatch": [
        "非整点时刻查询（如 12:30、半小时后）或未导入日期查询：日报仅含 00:00–23:00 整点，系统拒绝插值并提示澄清。",
        "站名与柜号归属不符（如查询中央变电所 306 柜或 1009 柜整点读数）：1535 中央变电所仅有截图台账，无小时级日报。",
        "事件跨柜对比时指定了与该事件（前一整点→触发整点）不一致的日期或小时：停止计算并提示对齐事件时段。",
        "任何遥控操作或定值修改请求（如分闸、合闸、倒闸、修改保护定值）：系统严格执行只读边界并拒绝控制指令。",
    ],
    "missing_evidence": [
        "电缆型号、截面、长度、敷设方式及环境温度校正系数（无法计算电缆热稳定极限与精确电压降模型）。",
        "1#、2# 主变及各高压柜的额定容量、CT/PT 变比配置、过流/速断/零序保护整定值单（无法直接判定是否越过设备额定限值或保护定值）。",
        "2026-11-10 至 2026-11-16 同期值班日志、倒闸操作票、35 kV/10 kV 母联及进出线开关分合变位、保护装置 SOE 动作记录（无法仅凭功率变化确诊物理原因是计划倒闸、正常大负荷启停还是故障跳闸）。",
        "35 kV 变电所 10 kV 侧未列入该日报的其他出线回路清单（主变低压侧与日报已列 10 kV 出线均值相差 54.0%）。",
    ],
}


def build_corpus_ledger(archive) -> dict:
    reports = [
        dict(row)
        for row in archive.db.execute(
            "SELECT day, filename, sheet, sha256 FROM report ORDER BY day"
        ).fetchall()
    ]
    diagrams = [
        dict(row)
        for row in archive.db.execute(
            "SELECT filename, captured_at, sha256 FROM diagram ORDER BY filename"
        ).fetchall()
    ]
    station_counts = {
        row["station"]: row["cnt"]
        for row in archive.db.execute(
            "SELECT station, COUNT(*) AS cnt FROM cabinet GROUP BY station"
        ).fetchall()
    }
    links = [
        dict(row)
        for row in archive.db.execute(
            "SELECT from_station, from_cabinet, to_station, to_cabinet, label FROM line ORDER BY from_cabinet"
        ).fetchall()
    ]
    reading_count = archive.db.execute("SELECT COUNT(*) FROM reading").fetchone()[0]
    summary_count = archive.db.execute("SELECT COUNT(*) FROM summary").fetchone()[0]

    replay_info = None
    if archive.has_events():
        with archive.event_connection() as ev_db:
            run = ev_db.execute(
                "SELECT id, mode, frames_seen FROM monitor_run WHERE mode='historical' AND complete=1 ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            if run:
                ev_cnt = ev_db.execute(
                    "SELECT COUNT(*) FROM monitor_event WHERE run_id=? AND type='power_change'",
                    (run["id"],),
                ).fetchone()[0]
                replay_info = {"run_id": run["id"], "frames": run["frames_seen"], "power_change_events": ev_cnt}

    health = evaluate_health(archive)
    standards = load_corpus()

    # Run 9-question corpus & boundary verification suite (C01-C09)
    session = QuestionSession(archive)

    # C01: Dual-basis (hourly vs footer) statistic check on 1009
    c01_ans = session.ask("11月10日1009柜有功最大值是多少？")
    c01_ok = (
        c01_ans["kind"] == "statistic_comparison"
        and c01_ans["report_footer"]["source"]["cell"] == "BD28"
        and c01_ans["hourly_samples"]["source"]["cell"] == "BD17"
    )

    # C02: Cross-station topology provenance check on 1009 & 1010
    c02_1009 = archive.supply(REPORT_STATION, "1009", "downstream")
    c02_1010 = archive.supply(REPORT_STATION, "1010", "downstream")
    c02_ok = (
        c02_1009["status"] == "ok"
        and c02_1009["links"][0]["to"]["number"] == "1101"
        and len(c02_1009["links"][0]["sources"]) == 2
        and c02_1010["status"] == "ok"
        and c02_1010["links"][0]["to"]["number"] == "1201"
    )

    # C03: Event-driven cross-cabinet reading provenance on 1009
    c03_ans = session.ask("LW-000010，比较1009柜同期有功。")
    c03_comp = c03_ans["comparisons"][0]
    c03_ok = (
        c03_ans["stop_reason"] == "completed"
        and c03_comp["cabinet"] == "1009"
        and c03_comp["before"]["source"]["cell"] == "BD15"
        and c03_comp["after"]["source"]["cell"] == "BD16"
    )

    # C04: Off-hour condition mismatch
    c04_ans = session.ask("11月10日12:30 1009柜有功是多少？")
    c04_ok = c04_ans["kind"] == "clarify"

    # C05: Station-cabinet mismatch (C1535 has no hourly report for 1009)
    c05_ans = session.ask("中央变电所1009柜11月10日12点有功是多少？")
    c05_ok = c05_ans["kind"] == "clarify"

    # C06: Unaligned hour on event comparison
    c06_ans = session.ask("LW-000010，比较1010柜15点的有功。")
    c06_ok = c06_ans["stop_reason"] == "time_alignment" and c06_ans["tool_calls"] == 1

    # C07: Missing cable model / rating / protection settings on 1009 & 1010
    c07_diag = diagnose_question(archive, "1009柜和1010柜下井电缆型号、截面和额定载流限值是多少？当前过载了吗？", client=None)
    c07_ok = (
        c07_diag["status"] == "ok"
        and c07_diag["verification"]["status"] == "verified"
        and any("额定" in m or "CT" in m for m in c07_diag["report"]["missing_evidence"])
    )

    # C08: 1010 low power factor confirmed + missing underground equipment ledger
    c08_diag = diagnose_question(archive, "1010柜（II回井下1535）这7天功率因数健康吗？具体最低值和原表出处在哪里？", client=None)
    c08_ok = (
        c08_diag["status"] == "ok"
        and c08_diag["verification"]["status"] == "verified"
        and "1115.xls / New Sheet1!BM16" in c08_diag["verification"]["verified_cells"]
    )

    # C09: Event physical trip cause missing evidence while keeping reading provenance
    c09_ans = session.ask("LW-000010，比较306柜同期有功。是不是跳闸导致的？")
    c09_ok = (
        c09_ans["stop_reason"] == "needs_evidence"
        and len(c09_ans["comparisons"]) == 1
        and any("运行日志" in p for p in c09_ans["pending"])
    )

    cases = [
        {
            "id": "C01",
            "category": "可答事实 · 整点与页尾双口径",
            "question": "11月10日1009柜有功最大值是多少？",
            "passed": c01_ok,
            "evidence": "1110.xls / New Sheet1!BD28（页尾 1222.51 kW）与 New Sheet1!BD17（13:00 整点 955.53 kW）",
            "note": "区分日报页尾原表值与 24 个整点样本最大值，分别保留源单元格。",
        },
        {
            "id": "C02",
            "category": "可答事实 · 站间图示连接",
            "question": "1009柜和1010柜下游分别接到哪里？",
            "passed": c02_ok,
            "evidence": "35KV变电所.png + 中央变电所.png（1009→1101 地面站10kV I段；1010→1201 地面站10kV II段）",
            "note": "绑定两张监控截图的文件名、截取时刻与 SHA-256；仅代表图示连接，不等同于某时刻合闸运行状态。",
        },
        {
            "id": "C03",
            "category": "可答事实 · 事件驱动跨柜核验",
            "question": "LW-000010，比较1009柜同期有功。",
            "passed": c03_ok,
            "evidence": f'1110.xls / New Sheet1!D15,D16（303柜）与 BD15,BD16（1009柜 {c03_comp["change_kw"]:+.2f} kW）',
            "note": "按事件前后两个整点（11:00→12:00）对齐查询另一柜并校验源文件哈希。",
        },
        {
            "id": "C04",
            "category": "条件不符 · 非整点时刻拒绝插值",
            "question": "11月10日12:30 1009柜有功是多少？",
            "passed": c04_ok,
            "evidence": "status=clarify（日报仅含 00:00–23:00 整点记录）",
            "note": "不伪造半点插值读数，要求用户指定整点。",
        },
        {
            "id": "C05",
            "category": "条件不符 · 站名与柜号归属冲突",
            "question": "中央变电所1009柜11月10日12点有功是多少？",
            "passed": c05_ok,
            "evidence": "status=clarify（1009 柜属于地面 35/10 kV 变电站，中央变电所无小时日报）",
            "note": "严格校验站名与柜号台账归属关系。",
        },
        {
            "id": "C06",
            "category": "条件不符 · 事件比较时段未对齐",
            "question": "LW-000010，比较1010柜15点的有功。",
            "passed": c06_ok,
            "evidence": "stop_reason=time_alignment（事件时段为 11:00 至 12:00，在第 1 步后立即停止）",
            "note": "防止把不同时段的读数混作同期对比。",
        },
        {
            "id": "C07",
            "category": "缺证边界 · 电缆型号与额定限值缺失",
            "question": "1009柜和1010柜下井电缆型号、截面和额定载流限值是多少？当前过载了吗？",
            "passed": c07_ok,
            "evidence": "保留 1009/1010 实际电流与有功区间，显式单列缺额定容量、电缆型号截面与保护定值单",
            "note": "缺设备参数时保留已证实读数，不编造额定值或过载结论。",
        },
        {
            "id": "C08",
            "category": "缺证边界 · 1010低功率因数与井下台账缺失",
            "question": "1010柜（II回井下1535）这7天功率因数健康吗？具体最低值和原表出处在哪里？",
            "passed": c08_ok,
            "evidence": "1115.xls / New Sheet1!BM16（最低 cos=0.32，7天均值 0.651，148/168h < 0.80）",
            "note": "准确给出量测异常证据，同时将井下就地无功补偿与电机轻载原因列为待现场核查。",
        },
        {
            "id": "C09",
            "category": "缺证边界 · 跳闸/操作原因缺日志",
            "question": "LW-000010，比较306柜同期有功。是不是跳闸导致的？",
            "passed": c09_ok,
            "evidence": "1110.xls / New Sheet1!D15,D16,H15,H16 + stop_reason=needs_evidence（缺运行日志/开关变位/保护动作）",
            "note": "已证实的四处单元格读数完整保留，物理原因明确登记为缺现场日志。",
        },
    ]

    return {
        "station": REPORT_STATION,
        "sources": {
            "daily_reports": reports,
            "diagrams": diagrams,
            "station_cabinet_counts": station_counts,
            "topology_links": links,
            "hourly_readings_total": reading_count,
            "footer_summaries_total": summary_count,
            "historical_replay": replay_info,
            "full_station_health_findings": health["summary"]["finding_counts"],
            "standards_clauses_total": len(standards),
        },
        "answerability_matrix": ANSWERABILITY_MATRIX,
        "verification_cases": cases,
        "summary": {
            "total_cases": len(cases),
            "passed_cases": sum(1 for c in cases if c["passed"]),
        },
    }


def render_corpus_markdown(ledger: dict) -> str:
    src = ledger["sources"]
    lines = [
        "# 老洼地现有资料台账与可答边界核验报告（阶段 08）",
        "",
        "## 一、已归档现场资料与派生证据总账",
        "",
        f'- **运行日报**：{len(src["daily_reports"])} 份（`2026-11-10` 至 `2026-11-16`），共 **{src["hourly_readings_total"]:,}** 条整点记录（第 4–27 行）+ **{src["footer_summaries_total"]:,}** 条页尾统计（第 28–30 行）',
        f'- **监控截图**：{len(src["diagrams"])} 张（地面 35/10 kV 站 {src["station_cabinet_counts"].get("S35", 0)} 柜、1535 中央变电所 {src["station_cabinet_counts"].get("C1535", 0)} 柜，合计 40 柜、{len(src["topology_links"])} 条站间连接）',
        f'- **派生监测与体检**：四柜整点回放 `{src["historical_replay"]["run_id"]}`（{src["historical_replay"]["power_change_events"]} 条变化事件）；全站 25 柜四维电气体检 **{src["full_station_health_findings"]["total"]}** 项发现',
        f'- **本地规程与国标库**：**{src["standards_clauses_total"]}** 条（《煤矿安全规程 2025》电气/监控章节 63 条原文 + 5 张已核 GB 参考卡）',
        "",
        "### 1.1 七份运行日报 SHA-256 台账",
        "",
        "| 日期 | 文件名 | 工作表 | SHA-256 |",
        "|---|---|---|---|",
    ]
    for rep in src["daily_reports"]:
        lines.append(f'| {rep["day"]} | `{rep["filename"]}` | `{rep["sheet"]}` | `{rep["sha256"]}` |')

    lines += [
        "",
        "### 1.2 两张监控截图与站间连接台账",
        "",
        "| 文件名 | 截取时刻 | SHA-256 | 已登记连接 |",
        "|---|---|---|---|",
    ]
    link_desc = "；".join(f'{l["from_station"]}.{l["from_cabinet"]} → {l["to_station"]}.{l["to_cabinet"]}（{l["label"]}）' for l in src["topology_links"])
    for diag in src["diagrams"]:
        lines.append(f'| `{diag["filename"]}` | {diag["captured_at"]} | `{diag["sha256"]}` | {link_desc} |')

    mat = ledger["answerability_matrix"]
    lines += [
        "",
        "## 二、三层可答边界定义",
        "",
        "### 2.1 可直接证实并回答的事实（附原表单元格/截图哈希）",
    ]
    for item in mat["answerable"]:
        lines.append(f"- {item}")
    lines += ["", "### 2.2 条件不符或越界请求（拒绝猜测、提示澄清）"]
    for item in mat["condition_mismatch"]:
        lines.append(f"- {item}")
    lines += ["", "### 2.3 当前缺失现场证据项（保留局部事实、显式单列缺证）"]
    for item in mat["missing_evidence"]:
        lines.append(f"- {item}")

    lines += [
        "",
        f'## 三、首批 9 道查证与边界核验题（通过 {ledger["summary"]["passed_cases"]}/{ledger["summary"]["total_cases"]}）',
        "",
        "| 题号 | 类别 | 问题 | 核验状态 | 证据出处与处理回执 |",
        "|---|---|---|---|---|",
    ]
    for c in ledger["verification_cases"]:
        lines.append(
            f'| `{c["id"]}` | {c["category"]} | {c["question"]} | **{"通过" if c["passed"] else "未通过"}** | `{c["evidence"]}` |'
        )
    lines.append("")
    return "\n".join(lines)
