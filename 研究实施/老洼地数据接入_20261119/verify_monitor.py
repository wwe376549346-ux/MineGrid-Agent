"""Verify weekly replay, controlled missing hour and archived event questions."""

import argparse
from contextlib import closing
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from laowadi.monitor import run_monitor
from laowadi.queries import Archive, render
from laowadi.questions import QuestionSession
from laowadi.storage import DB, connect, sha
from laowadi.xls import need


OMIT = "2026-11-10 10:00:00"


def verify(source, events_db):
    with closing(connect(source)) as db:
        archive = Archive(db, events_db)
        notice = []
        real = run_monitor(archive, events_db, notify=notice.append)
        expected = db.execute("""SELECT count(*) FROM reading a JOIN reading b
            ON a.station=b.station AND a.cabinet=b.cabinet AND a.metric=b.metric AND b.time=datetime(a.time,'+1 hour')
            WHERE a.metric='P' AND a.cabinet IN ('303','306','1009','1010') AND abs(b.value-a.value)>=1000""").fetchone()[0]
        need(real["processed_frames"] == 168 and real["event_counts"] == {"power_change": expected, "hourly_gap": 0, "missing_power": 0},
             "Full week and independent power-change count")
        need(real["new_notices"] == len(notice) == expected, "The monitor proactively emitted new events")
        question = QuestionSession(archive)
        finding = question.ask("11月10日12点303柜为什么提示变化？")
        event = finding["event"]
        detail = event["evidence"]
        need(event["run_id"] == real["run_id"] and event["type"] == "power_change" and detail["change_kw"] == 3408.12
             and detail["threshold_kw"] == 1000 and detail["before"]["source"]["cell"] == "D15"
             and detail["after"]["source"]["cell"] == "D16", "Natural-language event and original cells")
        need(archive.call("get_monitor_event", {"event_id": event["id"]})["event"] == event,
             "Read-only tool and prose source evidence match")
        need(question.ask("回放整点缺测了吗？")["status"] == "missing", "Complete historical replay has no gaps")

        injected = []
        simulation = run_monitor(archive, events_db, skip_hour=OMIT, notify=injected.append)
        simulated_archive = Archive(db, events_db, simulation["run_id"])
        gap = QuestionSession(simulated_archive).ask("哪个整点缺测？")["event"]
        need(simulation["processed_frames"] == 167 and simulation["event_counts"]["hourly_gap"] == 1
             and gap in injected and gap["mode"] == "simulated_gap" and gap["time"] == OMIT
             and gap["evidence"]["detected_at"] == "2026-11-10 11:00:00", "Missing frame detected at next received hour")
        need(simulated_archive.monitor_event(event_type="power_change", at="2026-11-10 11:00:00")["status"] == "missing",
             "No calculation across the injected gap")
        need(QuestionSession(archive).ask("哪个整点缺测？")["status"] == "missing", "Simulated data are excluded from default historical query")
        second = run_monitor(archive, events_db, notify=notice.append)
        need(second["run_id"] == real["run_id"] and second["event_counts"] == real["event_counts"] and second["new_notices"] == 0,
             "A replay retry does not duplicate stored or proactive events")
        return {"historical": real, "historical_answer": render(finding), "historical_event": event,
                "simulation": simulation, "simulation_answer": render({"kind": "monitor_event", "status": "ok", "event": gap}),
                "simulation_event": gap, "retry_deduplicated": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB)
    args = parser.parse_args()
    before = sha(args.db.read_bytes())
    with TemporaryDirectory() as directory:
        report = verify(args.db, Path(directory) / "verification.sqlite3")
    need(before == sha(args.db.read_bytes()), "The hourly archive remained read-only")
    print(json.dumps({**report, "source_database_unchanged": True}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
