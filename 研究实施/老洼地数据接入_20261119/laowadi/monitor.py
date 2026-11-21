"""Replay hourly readings into a separate, traceable local event archive."""

import json
from contextlib import closing, nullcontext
from datetime import date, datetime, timedelta
from decimal import Decimal
from math import isfinite
from pathlib import Path
from time import sleep

from laowadi.storage import ROOT, connect, sha


EVENT_DB = ROOT / "local" / "laowadi_events.sqlite3"
MONITORED = ("303", "306", "1009", "1010")
HOUR = timedelta(hours=1)
KINDS = {"power_change": "有功功率变化", "hourly_gap": "回放整点缺测", "missing_power": "整点有功读数缺失"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS monitor_run (
    id TEXT PRIMARY KEY, mode TEXT NOT NULL, description TEXT NOT NULL,
    last_time TEXT, frames_seen INTEGER NOT NULL DEFAULT 0, complete INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS monitor_event (
    seq INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES monitor_run(id),
    type TEXT NOT NULL, at TEXT NOT NULL, cabinet TEXT NOT NULL, evidence TEXT NOT NULL,
    UNIQUE (run_id, type, at, cabinet));
CREATE INDEX IF NOT EXISTS monitor_event_by_time ON monitor_event(run_id, at, seq);
"""


def packed(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, allow_nan=False)


def stamp(value):
    value = datetime.fromisoformat(value)
    if value.tzinfo or value.minute or value.second or value.microsecond:
        raise ValueError("请输入以日报钟表时刻表示的整点。")
    return value.isoformat(sep=" ")


def reading(frame, cabinet):
    item = frame["readings"].get(cabinet, {}).get("P")
    value = item["value"] if item else None
    evidence = dict(frame["source"])
    if item:
        evidence["cell"] = f'{item["column"]}{evidence["row"]}'
    return {"time": frame["time"], "value_kw": value, "source": evidence}


def detect(previous, frame, threshold):
    if previous:
        before = datetime.fromisoformat(previous["time"])
        after = datetime.fromisoformat(frame["time"])
        if after <= before:
            raise ValueError("历史回放时刻应严格递增。")
        if after - before > HOUR:
            missing = int((after - before) // HOUR) - 1
            first = (before + HOUR).isoformat(sep=" ")
            yield "hourly_gap", first, "", {
                "rule": "two_received_frames_separated_by_more_than_one_hour",
                "missing_hours": missing, "missing_from": first,
                "missing_through": (after - HOUR).isoformat(sep=" "),
                "detected_at": frame["time"],
                "last_frame": {"time": previous["time"], "source": previous["source"]},
                "next_frame": {"time": frame["time"], "source": frame["source"]},
            }
            previous = None
    for cabinet in MONITORED:
        now = reading(frame, cabinet)
        if now["value_kw"] is None:
            yield "missing_power", frame["time"], cabinet, {"rule": "active_power_cell_unavailable", "current": now}
            continue
        if previous is None:
            continue
        old = reading(previous, cabinet)
        if old["value_kw"] is None:
            continue
        change = Decimal(str(now["value_kw"])) - Decimal(str(old["value_kw"]))
        if abs(change) >= threshold:
            yield "power_change", frame["time"], cabinet, {"rule": "absolute_change_in_consecutive_hourly_power",
                 "before": old, "after": now, "change_kw": float(change), "threshold_kw": float(threshold)}


def run_monitor(archive, path, day=None, threshold_kw=1000, skip_hour=None, seconds_per_hour=0, notify=None):
    if not isfinite(threshold_kw) or threshold_kw <= 0 or not isfinite(seconds_per_hour) or not 0 <= seconds_per_hour <= 60:
        raise ValueError("筛查门槛须为正数，每个历史小时的回放间隔可设为 0 至 60 秒。")
    threshold = Decimal(str(threshold_kw))
    skip = stamp(skip_hour) if skip_hour else None
    parameters = (date.fromisoformat(day).isoformat(),) if day else ()
    source_path = archive.db.execute("PRAGMA database_list").fetchone()[2]
    if source_path and Path(source_path).resolve() == Path(path).resolve():
        raise ValueError("请为监测事件指定独立的 SQLite 文件。")
    archive.db.execute("BEGIN")
    try:
        sources = [dict(row) for row in archive.db.execute(
            "SELECT day, filename, sha256 FROM report" + (" WHERE day = ?" if parameters else "") + " ORDER BY day", parameters)]
        frames = list(archive.frames(day))
    finally:
        archive.db.rollback()
    if not frames:
        raise ValueError("指定范围内没有整点记录。")
    if skip and skip not in {frame["time"] for frame in frames[1:-1]}:
        raise ValueError("模拟缺测必须是所选回放范围内实际存在的中间整点。")
    mode = "simulated_gap" if skip else "historical"
    description = {"mode": mode, "configured_threshold_kw": float(threshold), "cabinets": MONITORED,
                   "omitted_hour": skip, "source_reports": sources, "rule_version": 1}
    serialized = packed(description)
    run_id = "LWR-" + sha(serialized.encode("utf-8"))[:16]

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path, writable=True)) as db:
        db.executescript(SCHEMA)
        with db:
            db.execute("INSERT OR IGNORE INTO monitor_run(id, mode, description) VALUES (?, ?, ?)", (run_id, mode, serialized))
        previous = None
        printed = 0
        for frame in frames:
            if frame["time"] == skip:
                continue
            with db:
                db.execute("BEGIN IMMEDIATE")
                last = db.execute("SELECT last_time FROM monitor_run WHERE id = ?", (run_id,)).fetchone()[0]
                if last and frame["time"] <= last:
                    previous = frame
                    continue
                updates = []
                for kind, at, cabinet, evidence in detect(previous, frame, threshold):
                    entry = db.execute("INSERT INTO monitor_event(run_id,type,at,cabinet,evidence) VALUES(?,?,?,?,?)",
                                       (run_id, kind, at, cabinet, packed(evidence)))
                    updates.append({"id": f"LW-{entry.lastrowid:06d}", "run_id": run_id, "mode": mode,
                                    "type": kind, "time": at, "cabinet": cabinet or None, "evidence": evidence})
                db.execute("UPDATE monitor_run SET last_time = ?, frames_seen = frames_seen + 1 WHERE id = ?", (frame["time"], run_id))
            if notify:
                for event in updates:
                    notify(event)
                    printed += 1
            previous = frame
            if seconds_per_hour and frame is not frames[-1]:
                sleep(seconds_per_hour)
        with db:
            db.execute("UPDATE monitor_run SET complete = 1 WHERE id = ?", (run_id,))
        counts = {row[0]: row[1] for row in db.execute("SELECT type,count(*) FROM monitor_event WHERE run_id=? GROUP BY type", (run_id,))}
        count = db.execute("SELECT frames_seen FROM monitor_run WHERE id=?", (run_id,)).fetchone()[0]
    return {"type": "monitor_completed", "run_id": run_id, "mode": mode, "source_frames": len(frames),
            "processed_frames": count, "event_counts": {kind: counts.get(kind, 0) for kind in KINDS}, "new_notices": printed,
            "threshold_kw": float(threshold), "omitted_hour": skip}


def lookup(path, run_id=None, event_id=None, cabinet=None, day=None, at=None, event_type=None, *, connection=None):
    result = {"kind": "monitor_event", "status": "missing"}
    if connection is None and (not path or not Path(path).is_file()):
        return {**result, "status": "no_archive"}
    if event_type is not None and event_type not in KINDS:
        raise ValueError("事件类别无效。")
    source = nullcontext(connection) if connection is not None else closing(connect(path))
    with source as db:
        sql = "SELECT e.*, r.mode FROM monitor_event e JOIN monitor_run r ON r.id=e.run_id WHERE 1=1"
        options = {}
        if event_id is not None:
            if not isinstance(event_id, str) or not event_id.startswith("LW-") or not event_id[3:].isascii() or not event_id[3:].isdigit():
                raise ValueError("事件编号格式应为 LW- 加数字。")
            numeric = event_id[3:].lstrip("0") or "0"
            if len(numeric) > 19 or int(numeric) >= 2 ** 63:
                return result
            options["e.seq"] = int(numeric)
        if run_id:
            options["r.id"] = run_id
        elif event_id is None:
            run = db.execute("SELECT id FROM monitor_run WHERE mode = 'historical' ORDER BY rowid DESC LIMIT 1").fetchone()
            if not run:
                return {**result, "status": "no_historical_run"}
            options["r.id"] = run[0]
        if cabinet is not None:
            options["e.cabinet"] = cabinet
        if event_type is not None:
            options["e.type"] = event_type
        if at is not None:
            options["e.at"] = stamp(at)
        if day is not None:
            options["substr(e.at,1,10)"] = date.fromisoformat(day).isoformat()
        sql += "".join(f" AND {key} = ?" for key in options) + " ORDER BY e.at DESC,e.seq DESC LIMIT 1"
        row = db.execute(sql, tuple(options.values())).fetchone()
    if not row:
        return result
    return {**result, "status": "ok", "event": {"id": f'LW-{row["seq"]:06d}', "run_id": row["run_id"],
            "mode": row["mode"], "time": row["at"], "type": row["type"], "cabinet": row["cabinet"] or None,
            "evidence": json.loads(row["evidence"])}}


def origin(location):
    position = location.get("cell") or f'第 {location["row"]} 行'
    return f'{location["file"]} / {location["sheet"]}!{position}'


def show_event(event):
    details = event["evidence"]
    label = "模拟缺测回放" if event["mode"] == "simulated_gap" else "历史整点回放"
    lead = f'{event["id"]} {label}。{KINDS[event["type"]]}。'
    if event["type"] == "hourly_gap":
        earlier, later = details["last_frame"], details["next_frame"]
        span = details["missing_from"]
        if details["missing_hours"] > 1:
            span += f' 至 {details["missing_through"]}'
        return (f'{lead} 缺测时段 {span}，共缺 {details["missing_hours"]} 个整点。'
                f'收到 {details["detected_at"]} 的帧时识别。相邻可用帧来源 {origin(earlier["source"])} 和 {origin(later["source"])}。')
    if event["type"] == "missing_power":
        return f'{lead}{event["cabinet"]} 柜 {event["time"]} 缺少有功数值。来源 {origin(details["current"]["source"])}。'
    before, after = details["before"], details["after"]
    old = f'{before["value_kw"]:,.8f}'.rstrip("0").rstrip(".")
    new = f'{after["value_kw"]:,.8f}'.rstrip("0").rstrip(".")
    change = f'{details["change_kw"]:+,.8f}'.rstrip("0").rstrip(".")
    return (f'{lead}{event["cabinet"]} 柜在 {before["time"]} 的有功为 {old} kW（{origin(before["source"])}），'
            f'在 {after["time"]} 为 {new} kW（{origin(after["source"])}）。变化 {change} kW，'
            f'达到相邻整点变化绝对值 ≥ {details["threshold_kw"]:g} kW 的回放筛查门槛。待复核，物理原因需结合运行记录。')
