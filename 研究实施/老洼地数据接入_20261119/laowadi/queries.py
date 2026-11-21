"""Deterministic query tools that retain Excel and diagram evidence."""

from contextlib import closing, contextmanager
import json
from pathlib import Path
from datetime import date, datetime
from inspect import signature
from itertools import groupby
from statistics import fmean

from laowadi.agent import render_review
from laowadi.monitor import EVENT_DB, lookup, show_event
from laowadi.db.postgres import archive_snapshot
from laowadi.storage import METRICS, STATISTICS, catalog, connect


READINGS = """SELECT reading.*, report.filename, report.sheet, report.sha256
              FROM reading JOIN report USING(day)"""


def evidence(row):
    return {"file": row["filename"], "sheet": row["sheet"], "cell": f'{row["source_col"]}{row["source_row"]}',
            "sha256": row["sha256"]}


def label_device(device):
    return f'{device["station_name"]}{device["label"]}（{device["number"]} 柜）'


class Archive:
    def __init__(self, db, events_db=EVENT_DB, run_id=None, *, pg_url=None):
        self.db = db
        self.events_db = events_db
        self.run_id = run_id
        self.pg_url = pg_url
        self.spec = catalog()
        self.devices = {}
        for row in db.execute("SELECT cabinet.*, station.name AS station_name FROM cabinet JOIN station ON station.code = cabinet.station"):
            device = dict(row)
            device["aliases"] = json.loads(device["aliases"])
            self.devices[row["station"], row["number"]] = device

    def has_events(self):
        return bool(self.pg_url or (self.events_db and Path(self.events_db).is_file()))

    @contextmanager
    def event_connection(self):
        if self.pg_url:
            yield self.db
        else:
            with closing(connect(self.events_db)) as db:
                yield db

    def parallel_reader(self):
        """Capture one snapshot for all parallel PostgreSQL specialists."""
        if self.pg_url:
            snapshot = self.db.execute("SELECT pg_export_snapshot()").fetchone()[0]

            @contextmanager
            def reader():
                with archive_snapshot(self.pg_url, snapshot) as db:
                    yield Archive(db, self.events_db, self.run_id, pg_url=self.pg_url)
            return reader

        db_file = self.db.execute("PRAGMA database_list").fetchone()["file"]
        if not db_file or not Path(db_file).is_file():
            return None

        @contextmanager
        def reader():
            with closing(connect(db_file)) as db:
                db.execute("BEGIN")
                yield Archive(db, self.events_db, self.run_id)
        return reader

    def source_range(self):
        return self.db.execute("SELECT min(day), max(day) FROM report").fetchone()

    def days(self):
        return [row[0] for row in self.db.execute("SELECT day FROM report ORDER BY day")]

    def device(self, station, cabinet):
        if not isinstance(station, str) or not isinstance(cabinet, str):
            raise ValueError("站代码和柜号须为字符串。")
        try:
            return self.devices[station, cabinet]
        except KeyError as exc:
            raise ValueError("台账中没有指定的站和柜号。") from exc

    def base(self, station, cabinet, metric):
        device = self.device(station, cabinet)
        if not isinstance(metric, str) or metric not in METRICS:
            raise ValueError("量名可选 Uab、Ia、P 或 cos。")
        metric_name, unit = METRICS[metric]
        return {"device": device, "metric": metric, "metric_name": metric_name, "unit": unit}

    def reading(self, station, cabinet, metric, at=None, day=None):
        result = {**self.base(station, cabinet, metric), "kind": "reading", "basis": "hourly", "status": "missing"}
        parameters = [station, cabinet, metric]
        statement = READINGS + " WHERE station = ? AND cabinet = ? AND metric = ?"
        if at is not None:
            at = datetime.fromisoformat(at)
            if at.tzinfo is not None:
                raise ValueError("请用日报中的本地钟表时间查询。")
            stamp = at.isoformat(sep=" ")
            result["time"] = stamp
            statement += " AND time = ?"
            parameters.append(stamp)
        if day is not None:
            result["day"] = date.fromisoformat(day).isoformat()
            statement += " AND day = ?"
            parameters.append(result["day"])
        row = self.db.execute(statement + " ORDER BY time DESC LIMIT 1", parameters).fetchone()
        if row:
            result.update(status="ok" if row["value"] is not None else "missing_value", time=row["time"],
                          value=row["value"], source=evidence(row), marker=row["marker"], latest=at is None)
        return result

    def statistic(self, station, cabinet, metric, day, statistic, basis="both"):
        result = {**self.base(station, cabinet, metric), "kind": "statistic", "basis": basis, "status": "missing",
                  "day": date.fromisoformat(day).isoformat(), "statistic": statistic}
        if statistic not in STATISTICS or basis not in {"hourly", "footer", "both"}:
            raise ValueError("统计量或来源口径无效。")
        if basis == "both":
            footer = self.statistic(station, cabinet, metric, day, statistic, basis="footer")
            hourly = self.statistic(station, cabinet, metric, day, statistic, basis="hourly")
            status = "ok" if "ok" in {footer["status"], hourly["status"]} else "missing"
            return {**result, "kind": "statistic_comparison", "status": status,
                    "report_footer": footer, "hourly_samples": hourly}
        params = [station, cabinet, metric, result["day"]]
        if basis == "footer":
            row = self.db.execute("""SELECT summary.*, report.filename, report.sheet, report.sha256
                FROM summary JOIN report USING(day) WHERE station = ? AND cabinet = ? AND metric = ? AND day = ? AND statistic = ?""",
                [*params, statistic]).fetchone()
            if row:
                result.update(status="ok" if row["value"] is not None else "missing_value", value=row["value"],
                              source=evidence(row), marker=row["marker"])
            return result

        rows = list(self.db.execute(READINGS + " WHERE station = ? AND cabinet = ? AND metric = ? AND day = ? ORDER BY time", params))
        valid = [row for row in rows if row["value"] is not None]
        result.update(valid_samples=len(valid), planned_samples=24)
        if not valid:
            return result
        if statistic == "avg":
            value = fmean(row["value"] for row in valid)
            result.update(status="ok", value=value,
                          source={**evidence(valid[0]), "cell": f'{evidence(rows[0])["cell"]}:{evidence(rows[-1])["cell"]}'},
                          sample_boundaries=[evidence(row) for row in (rows[0], rows[-1])])
            return result
        selected = (max if statistic == "max" else min)(valid, key=lambda row: row["value"])
        result.update(status="ok", value=selected["value"], time=selected["time"], source=evidence(selected))
        return result

    def supply(self, station, cabinet, direction="upstream"):
        if direction not in {"upstream", "downstream"}:
            raise ValueError("关系方向须为 upstream 或 downstream。")
        device = self.device(station, cabinet)
        end = "to" if direction == "upstream" else "from"
        rows = self.db.execute(f"SELECT * FROM line WHERE {end}_station = ? AND {end}_cabinet = ?", (station, cabinet))
        links = []
        for row in rows:
            diagrams = self.db.execute("SELECT filename, captured_at, sha256 FROM diagram JOIN station ON station.diagram = diagram.filename WHERE station.code IN (?, ?) ORDER BY captured_at",
                                       (row["from_station"], row["to_station"]))
            links.append({"from": self.device(row["from_station"], row["from_cabinet"]),
                          "to": self.device(row["to_station"], row["to_cabinet"]), "label": row["label"],
                          "sources": [dict(diagram) for diagram in diagrams]})
        return {"kind": "supply", "device": device, "status": "ok" if links else "missing", "links": links,
                "basis": "single_line_diagram_labels"}

    def monitor_event(self, event_id=None, cabinet=None, day=None, at=None, event_type=None, run_id=None):
        return lookup(self.events_db, run_id=run_id or self.run_id, event_id=event_id, cabinet=cabinet,
                      day=day, at=at, event_type=event_type, connection=self.db if self.pg_url else None)

    def call(self, tool, arguments):
        endpoints = {"get_hourly_reading": self.reading, "get_daily_statistic": self.statistic,
                     "get_supply_link": self.supply, "get_monitor_event": self.monitor_event}
        if tool not in endpoints or not isinstance(arguments, dict):
            raise ValueError("查询工具或参数对象无效。")
        operation = endpoints[tool]
        signature(operation).bind(**arguments)
        return operation(**arguments)

    def frames(self, day=None):
        args = () if day is None else (date.fromisoformat(day).isoformat(),)
        rows = self.db.execute(READINGS + (" WHERE day = ?" if args else "") + " ORDER BY time, cabinet, metric", args)
        for stamp, group in groupby(rows, key=lambda row: row["time"]):
            readings = {}
            row = None
            for row in group:
                readings.setdefault(row["cabinet"], {})[row["metric"]] = {"value": row["value"], "column": row["source_col"],
                                                                          "status": "observed" if row["value"] is not None else "missing"}
            yield {"type": "hourly_frame", "time": stamp, "interval_seconds": 3600, "station": row["station"],
                   "clock": "report_wall_clock", "source": {"file": row["filename"], "sheet": row["sheet"],
                                                              "row": row["source_row"], "sha256": row["sha256"]},
                   "readings": readings}


def render(result):
    kind = result["kind"]
    if kind == "agent_review":
        return render_review(result, render)
    if kind == "clarify":
        return result["message"]
    if kind == "statistic_comparison":
        return "\n".join(render(value) for value in (result["report_footer"], result["hourly_samples"]))
    if kind == "monitor_event":
        if result["status"] == "ok":
            return show_event(result["event"])
        if result["status"] in {"no_archive", "no_historical_run"}:
            return "当前查询范围还没有已归档的监测回放。"
        return "指定回放和条件下未找到这类事件。"
    name = label_device(result["device"])
    if kind == "supply":
        if not result["links"]:
            return f"已整理图纸中没有{name}的这项供电连接记录。"
        return "\n".join(
            f'{label_device(link["from"])}通过{link["label"]}接入{label_device(link["to"])}。'
            f'依据 {"、".join(d["filename"] + "（" + d["captured_at"] + "）" for d in link["sources"])} 的线路标注。'
            for link in result["links"]
        )

    raw = result.get("source")
    source = f'来源 {raw["file"]} / {raw["sheet"]}!{raw["cell"]}。' if raw else ""
    metric = result["metric_name"]
    time_label = result.get("time", result.get("day", ""))
    if result["status"] == "missing":
        return f"现有日报中未找到{name} {time_label} 的{metric}。"
    if result["status"] == "missing_value":
        basis = "日报页尾统计" if result["basis"] == "footer" else "整点记录"
        marker = result["marker"] or "空单元"
        return f'{name} {time_label} 的{metric}{basis}为「{marker}」，暂无可用数值。{source}'
    value = f'{result["value"]:,.8f}'.rstrip("0").rstrip(".")
    unit = f' {result["unit"]}' if result["unit"] else ""
    if kind == "reading":
        lead = "现有日报最近整点" if result["latest"] else "日报整点"
        return f"{name}{lead} {time_label} 的{metric}为 {value}{unit}。{source}"
    stat = STATISTICS[result["statistic"]]
    if result["basis"] == "footer":
        return (f'{name} {result["day"]} 日报页尾所列{metric}的{stat}为 {value}{unit}。'
                f'按原表照录，当前资料未注明页尾统计采用的采样频度或极值发生时刻。{source}')
    stamp = f'，出现在 {result["time"][11:]}' if "time" in result else ""
    return (f'{name} {result["day"]} 的{metric}整点样本{stat}为 {value}{unit}{stamp}。'
            f'有效样本 {result["valid_samples"]}/{result["planned_samples"]}。{source}')
