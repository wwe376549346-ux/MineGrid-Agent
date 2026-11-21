"""Import unmodified hourly readings and report footer statistics into SQLite."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import date
from math import isfinite
from pathlib import Path
import re

from laowadi.xls import need, read_rows


ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "local" / "laowadi.sqlite3"
CATALOG = Path(__file__).with_name("topology.json")
REPORT_STATION = "S35"
METRICS = {"Uab": ("线电压 Uab", "kV"), "Ia": ("A 相电流 Ia", "A"),
           "P": ("有功功率 P", "kW"), "cos": ("功率因数 cos", "")}
STATISTICS = {"max": "最大值", "min": "最小值", "avg": "平均值"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS station (code TEXT PRIMARY KEY, name TEXT NOT NULL, diagram TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS diagram (filename TEXT PRIMARY KEY, captured_at TEXT NOT NULL, sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cabinet (
    station TEXT NOT NULL REFERENCES station(code), number TEXT NOT NULL, label TEXT NOT NULL,
    aliases TEXT NOT NULL, PRIMARY KEY (station, number));
CREATE TABLE IF NOT EXISTS line (
    from_station TEXT NOT NULL, from_cabinet TEXT NOT NULL, to_station TEXT NOT NULL, to_cabinet TEXT NOT NULL,
    label TEXT NOT NULL, PRIMARY KEY (from_station, from_cabinet, to_station, to_cabinet),
    FOREIGN KEY (from_station, from_cabinet) REFERENCES cabinet(station, number),
    FOREIGN KEY (to_station, to_cabinet) REFERENCES cabinet(station, number));
CREATE TABLE IF NOT EXISTS report (day TEXT PRIMARY KEY, filename TEXT NOT NULL, sheet TEXT NOT NULL, sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reading (
    day TEXT NOT NULL REFERENCES report(day), time TEXT NOT NULL, station TEXT NOT NULL, cabinet TEXT NOT NULL,
    metric TEXT NOT NULL, value REAL, marker TEXT, source_row INTEGER NOT NULL, source_col TEXT NOT NULL,
    PRIMARY KEY (station, cabinet, metric, time),
    FOREIGN KEY (station, cabinet) REFERENCES cabinet(station, number));
CREATE INDEX IF NOT EXISTS reading_by_time ON reading(time);
CREATE TABLE IF NOT EXISTS summary (
    day TEXT NOT NULL REFERENCES report(day), station TEXT NOT NULL, cabinet TEXT NOT NULL, metric TEXT NOT NULL,
    statistic TEXT NOT NULL, value REAL, marker TEXT, source_row INTEGER NOT NULL, source_col TEXT NOT NULL,
    PRIMARY KEY (station, cabinet, metric, day, statistic),
    FOREIGN KEY (station, cabinet) REFERENCES cabinet(station, number));
"""


def sha(data):
    return hashlib.sha256(data).hexdigest()


def catalog():
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def connect(path, writable=False):
    uri = Path(path).resolve().as_uri() + ("?mode=rwc" if writable else "?mode=ro")
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def label_column(index):
    label = ""
    while index:
        index, remaining = divmod(index - 1, 26)
        label = chr(65 + remaining) + label
    return label


def number(value):
    if isinstance(value, float) and isfinite(value):
        return value, None
    need(value in {"-", ""}, f"预期有限数值、空单元或短横线，收到 {value!r}。")
    return None, value


def decode_report(path, accepted_cabinets):
    raw = path.read_bytes()
    name, rows = read_rows(raw)
    need(len(rows) == 30 and "35kV变电所运行日报表" in rows[0], "不是预期的 35 kV 运行日报。")
    need(rows[1][0] == "报表日期", "缺少报表日期。")
    day = date.fromisoformat(rows[1][1]).isoformat()
    columns = []
    for index, heading in enumerate(rows[2][1:], 2):
        match = re.fullmatch(r"(\d+)\n测量(Uab|Ia|P|cos)", heading)
        need(match is not None, f"{label_column(index)} 列的测点或量名无法识别。")
        columns.append((*match.groups(), label_column(index)))
    keys = [(point, metric) for point, metric, _ in columns]
    expected = {(point, metric) for point in accepted_cabinets for metric in METRICS}
    need(set(keys) == expected and len(keys) == len(expected), "日报列缺少测点，出现新测点或重复量名。")
    need([row[0] for row in rows[27:]] == list(STATISTICS.values()), "页尾统计行位置或标签与日报模板不同。")
    readings, summaries = [], []
    for offset, row in enumerate(rows[3:27]):
        expected_time = f"{offset}:00:00"
        need(row[0] in {expected_time, f"{offset:02}:00:00"}, f"日报缺少 {offset:02} 点整点行或时间排序异常。")
        stamp = f"{day} {offset:02}:00:00"
        for value, (cabinet, metric, col) in zip(row[1:], columns, strict=True):
            readings.append((day, stamp, REPORT_STATION, cabinet, metric, *number(value), offset + 4, col))
    for line, (stat, row) in enumerate(zip(STATISTICS, rows[27:], strict=True), 28):
        for value, (cabinet, metric, col) in zip(row[1:], columns, strict=True):
            summaries.append((day, REPORT_STATION, cabinet, metric, stat, *number(value), line, col))
    return (day, path.name, name, sha(raw)), readings, summaries


def prepare_topology(db, spec, source):
    seen = db.execute("SELECT value FROM metadata WHERE key = 'catalog_sha256'").fetchone()
    digest = sha(CATALOG.read_bytes())
    if seen:
        need(seen[0] == digest, "柜号图谱配置曾变化，请保留原库并建立新的来源版本。")
        return
    for code, record in spec["sources"].items():
        filename = record["filename"]
        need(sha((source / filename).read_bytes()) == record["sha256"], f"{filename} 与核对柜号的图版本不一致。")
        db.execute("INSERT INTO diagram VALUES (?, ?, ?)", (filename, record["captured_at"], record["sha256"]))
        db.execute("INSERT INTO station VALUES (?, ?, ?)", (code, spec["stations"][code]["name"], filename))
        db.executemany("INSERT INTO cabinet VALUES (?, ?, ?, ?)",
                       [(code, key, aliases[0], json.dumps(aliases, ensure_ascii=False))
                        for key, aliases in spec["cabinets"][code].items()])
    db.executemany("INSERT INTO line VALUES (?, ?, ?, ?, ?)",
                   [(*link["from"], *link["to"], link["label"]) for link in spec["links"]])
    db.execute("INSERT INTO metadata VALUES ('catalog_sha256', ?)", (digest,))


def import_reports(source, target):
    source = Path(source)
    paths = sorted(path for path in source.iterdir() if path.is_file() and path.suffix.lower() == ".xls")
    need(paths, "指定目录中没有 .xls 日报。")
    spec = catalog()
    payloads = []
    for path in paths:
        try:
            payloads.append(decode_report(path, spec["cabinets"][REPORT_STATION]))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{path.name} 无法导入：{exc}") from exc
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(target, writable=True)) as db:
        db.executescript(SCHEMA)
        with db:
            db.execute("BEGIN IMMEDIATE")
            prepare_topology(db, spec, source)
            new_days = []
            for document, rows, footer in sorted(payloads):
                day, _, _, digest = document
                previous = db.execute("SELECT sha256 FROM report WHERE day = ?", (day,)).fetchone()
                if previous:
                    need(previous[0] == digest, f"{day} 已存在不同版本的日报，当前批次未写入。")
                    continue
                db.execute("INSERT INTO report VALUES (?, ?, ?, ?)", document)
                db.executemany("INSERT INTO reading VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
                db.executemany("INSERT INTO summary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", footer)
                new_days.append(day)
        tables = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("report", "reading", "summary", "cabinet", "line")}
        bounds = db.execute("SELECT min(time), max(time), count(DISTINCT time) FROM reading").fetchone()
        return {"imported_days": new_days, "counts": tables, "first": bounds[0], "last": bounds[1], "timestamps": bounds[2]}
