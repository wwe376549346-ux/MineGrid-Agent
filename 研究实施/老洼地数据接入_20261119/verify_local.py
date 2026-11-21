"""Check the local Laowadi import, replay, query evidence and retry behavior."""

import argparse
from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory

from laowadi.queries import Archive
from laowadi.questions import QuestionSession
from laowadi.storage import DB, connect, import_reports, sha
from laowadi.xls import need


def verify_archive(path):
    with closing(connect(path)) as db:
        archive = Archive(db)
        need(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity check")
        tables = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("report", "reading", "summary", "cabinet", "line")}
        need(tables == {"report": 7, "reading": 16800, "summary": 2100, "cabinet": 40, "line": 2}, "Imported record counts")
        need(db.execute("SELECT count(*) FROM reading WHERE value IS NULL").fetchone()[0] == 0, "All hourly values are available")
        need(db.execute("SELECT count(*) FROM summary WHERE value IS NULL AND marker = '-'").fetchone()[0] == 98,
             "Footer markers preserved as unavailable")
        need(db.execute("SELECT count(*) FROM reading WHERE metric = 'P' AND value < 0").fetchone()[0] == 192
             and db.execute("SELECT count(*) FROM reading WHERE metric = 'cos' AND value < 0").fetchone()[0] == 186,
             "Original signed values preserved")
        frames = list(archive.frames())
        stamps = [datetime.fromisoformat(frame["time"]) for frame in frames]
        need(len(frames) == 168 and all(right - left == timedelta(hours=1) for left, right in zip(stamps, stamps[1:])),
             "Full batch hourly replay")
        need(all(len(frame["readings"]) == 25 and all(len(points) == 4 for points in frame["readings"].values())
                 for frame in frames) and len(list(archive.frames("2026-11-16"))) == 24, "Frame contents and date filter")

        session = QuestionSession(archive)
        peak = session.ask("11 月 16 日一回进线的整点最大有功功率是多少？")
        footer = session.ask("日报页尾的最大值呢？")
        unspecified = session.ask("11月16日303柜有功功率最大值是多少？")
        report_max = session.ask("11月16日303柜日报的最大有功功率是多少？")
        report_hourly = session.ask("11月16日303柜日报中整点最大有功功率是多少？")
        exact = session.ask("303柜11月16日15点有功功率是多少？")
        wrong_hour = session.ask("303柜11月16日15点30有功功率是多少？")
        latest = session.ask("303柜现在的电流是多少？")
        first = session.ask("中央变电所一回进线由地面哪个柜供电？")
        second = session.ask("中央变电所二回进线由地面哪个柜供电？")
        reverse = session.ask("1009 柜的下游接到哪里？")
        unmapped = session.ask("中央变电所1101柜在2026年11月16日15点的有功功率是多少？")
        wrong_station = session.ask("中央变电所303柜电流是多少？")
        ambiguous = session.ask("一回进线的上游是哪个柜？")
        need((peak["value"], peak["time"], peak["source"]["cell"], peak["valid_samples"]) ==
             (4969.78, "2026-11-16 15:00:00", "D19", 24), "Hourly peak and coordinate")
        need((footer["basis"], footer["value"], footer["source"]["cell"]) == ("footer", 5420.9, "D28"), "Footer follow-up")
        need(unspecified["basis"] == "both" and unspecified["report_footer"]["source"]["cell"] == "D28"
             and unspecified["hourly_samples"]["source"]["cell"] == "D19" and "time" not in unspecified["report_footer"],
             "Unscoped daily maximum explicitly returns both bases with independent evidence")
        need(report_max["basis"] == "footer" and report_max["value"] == 5420.9 and
             report_hourly["basis"] == "hourly" and report_hourly["value"] == 4969.78,
             "Report maximum and report hourly-sample maximum route separately")
        need(exact["source"] == peak["source"] and wrong_hour["status"] == "missing" and latest["time"] == "2026-11-16 23:00:00",
             "Exact, unavailable non-hour and latest historical reading")
        expected_links = [("1009", "1101"), ("1010", "1201"), ("1009", "1101")]
        for link_answer, (source, target) in zip((first, second, reverse), expected_links, strict=True):
            link, = link_answer["links"]
            need((link["from"]["number"], link["to"]["number"]) == (source, target) and len(link["sources"]) == 2,
                 "Both diagram citations and link endpoints")
        need(unmapped["status"] == "missing" and wrong_station["status"] == ambiguous["status"] == "clarify",
             "Missing central measurements and ambiguous device guard")
        for answer in (peak, footer, unspecified, report_max, report_hourly, exact, latest, first, second, reverse):
            structured = archive.call(answer["tool"], answer["arguments"])
            need(structured == {key: value for key, value in answer.items() if key not in {"tool", "arguments"}},
                 "Natural language and callable tool share the same evidence")
        return {"tables": tables, "hourly_frames": len(frames), "start": frames[0]["time"], "end": frames[-1]["time"],
                "peak": peak, "footer": footer, "unspecified_daily_max": unspecified,
                "incoming_links": [first, second], "question_checks": 14}


def verify_retry(source):
    with TemporaryDirectory() as directory:
        target = Path(directory) / "test.sqlite3"
        local_copy = Path(directory) / "reports"
        source_digests = {path.name: sha(path.read_bytes()) for path in source.iterdir() if path.is_file()}
        imported = import_reports(source, target)
        retry = import_reports(source, target)
        need(len(imported["imported_days"]) == 7 and not retry["imported_days"] and imported["counts"] == retry["counts"],
             "Idempotent import on retry")
        copytree(source, local_copy)
        conflict = local_copy / "1116.xls"
        data = bytearray(conflict.read_bytes())
        data[8] ^= 1  # Change the test copy's compound metadata, retaining the visible report cells.
        conflict.write_bytes(data)
        try:
            import_reports(local_copy, target)
        except ValueError as exc:
            need("不同版本" in str(exc), "Conflicting same-day source is rejected")
        else:
            raise AssertionError("Expected a source-version conflict")
        verified = verify_archive(target)
        need(verified["tables"] == imported["counts"], "Failed import leaves database intact")
        need(source_digests == {path.name: sha(path.read_bytes()) for path in source.iterdir() if path.is_file()}, "Source bytes unchanged")
    return {"safe_retry": True, "conflict_rollback": True, "source_bytes_unchanged": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DB)
    args = parser.parse_args()
    print(json.dumps({"archive": verify_archive(args.db), "import": verify_retry(args.source)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
