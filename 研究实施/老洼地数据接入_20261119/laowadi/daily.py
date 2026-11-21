"""Read-only daily observations from matching source reports and historical events."""

from collections import defaultdict
from contextlib import closing
from datetime import date
import json
from pathlib import Path

from laowadi.monitor import MONITORED, origin
from laowadi.storage import METRICS, REPORT_STATION, connect


def historical_events(archive, reports, all_reports):
    if not archive.has_events():
        raise ValueError("找不到历史事件库，请先完成原始资料回放。")
    expected = {row["day"] for row in reports}
    available = {row["day"]: (row["filename"], row["sha256"]) for row in all_reports}
    filters = (archive.run_id,) if archive.run_id else ()
    match_id = " AND id=?" if filters else ""
    with archive.event_connection() as db:
        for run in db.execute(
            "SELECT id,description FROM monitor_run WHERE complete=1 AND mode='historical'"
            + match_id + " ORDER BY rowid DESC", filters
        ):
            description = json.loads(run["description"])
            sources = {item["day"]: (item["filename"], item["sha256"]) for item in description["source_reports"]}
            if (tuple(description["cabinets"]) == MONITORED and expected <= sources.keys()
                    and sources.items() <= available.items()):
                break
        else:
            raise ValueError("没有覆盖所选日报且版本一致的历史回放，请核对运行编号或重新回放。")
        by_cabinet = defaultdict(list)
        for row in db.execute(
            """SELECT seq,at,cabinet,evidence FROM monitor_event
               WHERE run_id=? AND type='power_change' ORDER BY at,seq""", (run["id"],)
        ):
            if row["at"][:10] in expected:
                by_cabinet[row["at"][:10], row["cabinet"]].append({
                    "id": f'LW-{row["seq"]:06d}', "time": row["at"], **json.loads(row["evidence"]),
                })
    return run["id"], description["configured_threshold_kw"], by_cabinet


def observe(archive, day=None):
    selected = date.fromisoformat(day).isoformat() if day else None
    all_reports = list(archive.db.execute("SELECT * FROM report ORDER BY day"))
    reports = [row for row in all_reports if selected is None or row["day"] == selected]
    if not reports:
        raise ValueError("所选日期没有已导入的运行日报。")
    run_id, threshold, events = historical_events(archive, reports, all_reports)
    observations = []
    for report in reports:
        day = report["day"]
        frames = {frame["time"]: frame for frame in archive.frames(day)}
        times = [f"{day} {hour:02}:00:00" for hour in range(24)]
        cabinets = []
        for cabinet in MONITORED:
            series = {metric: [] for metric in METRICS}
            for time in times:
                frame = frames.get(time)
                readings = frame["readings"].get(cabinet, {}) if frame else {}
                for metric in METRICS:
                    item = readings.get(metric)
                    source = None
                    if item:
                        source = {**frame["source"], "cell": f'{item["column"]}{frame["source"]["row"]}'}
                    series[metric].append({"time": time, "value": item["value"] if item else None, "source": source})
            valid = {metric: sum(item["value"] is not None for item in items) for metric, items in series.items()}
            peaks = {metric: archive.statistic(REPORT_STATION, cabinet, metric, day, "max", "hourly")
                     for metric in METRICS}
            notices = events[day, cabinet]
            states = []
            if any(count != 24 for count in valid.values()):
                states.append("数据不足")
            if notices:
                states.append("有变化待复核")
            cabinets.append({
                "cabinet": cabinet, "status": states or ["当前规则下无提示"], "valid_samples": valid,
                "peaks": peaks, "hourly": series, "notices": notices,
            })
        observations.append({
            "source": dict(report), "hours_with_frame": sum(time in frames for time in times), "cabinets": cabinets,
        })
    return {"station": REPORT_STATION, "run_id": run_id, "mode": "historical",
            "threshold_kw": threshold, "expected_samples_per_metric": 24, "days": observations}


def peak_text(peak):
    if peak["status"] != "ok":
        return "缺测"
    value = f'{peak["value"]:.12g} {peak["unit"]}'.strip()
    return f'{value} / {peak["time"][11:16]} / {peak["source"]["cell"]}'


def markdown(report):
    lines = [
        "# 老洼地四柜日级运行观察报告", "",
        f'数据口径为 35 kV 地面站小时级历史回放，运行编号 `{report["run_id"]}`。'
        f'相邻整点有功变化绝对值达到 {report["threshold_kw"]:g} kW 时生成筛查提示。',
        "提示待结合对应时段运行记录复核。峰值按日报整点最大读数计算，同值取最早时点。", "",
    ]
    for day in report["days"]:
        source = day["source"]
        cabinets = day["cabinets"]
        lines += [
            f'## {source["day"]}', "",
            f'源表 `{source["filename"]}` / `{source["sheet"]}`。已收到 {day["hours_with_frame"]}/24 个整点。',
            "", "| 柜号 | 状态 | 电压 / 电流 / 有功 / 功率因数有效点 | 筛查提示 |",
            "|---|---|---|---:|",
        ]
        for item in cabinets:
            counts = " / ".join(str(item["valid_samples"][metric]) for metric in ("Uab", "Ia", "P", "cos"))
            lines.append(f'| {item["cabinet"]} | {"、".join(item["status"])} | {counts} | {len(item["notices"])} |')
        lines += [
            "", "### 整点峰值", "", "| 柜号 | 电压 | 电流 | 有功 | 功率因数 |", "|---|---|---|---|---|",
        ]
        for item in cabinets:
            data = " | ".join(peak_text(item["peaks"][metric]) for metric in ("Uab", "Ia", "P", "cos"))
            lines.append(f'| {item["cabinet"]} | {data} |')
        lines += [
            "", "### 整点有功变化趋势", "", "| 整点 | " + " | ".join(item["cabinet"] + " 柜（kW）" for item in cabinets) + " |",
            "|---|---:|---:|---:|---:|",
        ]
        for hour in range(24):
            values = [item["hourly"]["P"][hour]["value"] for item in cabinets]
            lines.append(f"| {hour:02}:00 | " + " | ".join(f"{v:.12g}" if v is not None else "缺测" for v in values) + " |")
        lines += ["", "### 待复核提示", ""]
        if not any(item["notices"] for item in cabinets):
            lines += ["当前规则下无提示。", ""]
            continue
        lines += ["| 事件 | 柜号 | 起止时刻 | 有功前值 → 后值（kW） | 变化（kW） | 来源 |",
                  "|---|---|---|---:|---:|---|"]
        for item in cabinets:
            for notice in item["notices"]:
                before, after = notice["before"], notice["after"]
                lines.append(
                    f'| {notice["id"]} | {item["cabinet"]} | {before["time"][:16]} → {after["time"][:16]} | '
                    f'{before["value_kw"]:.12g} → {after["value_kw"]:.12g} | {notice["change_kw"]:+.12g} | '
                    f'{origin(before["source"])} → {origin(after["source"])} |'
                )
        lines.append("")
    return "\n".join(lines)
