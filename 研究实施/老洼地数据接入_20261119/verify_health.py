"""Regression and provenance verification for full-station electrical health inspection."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from laowadi.health import evaluate_health, render_health_markdown
from laowadi.monitor import EVENT_DB
from laowadi.queries import Archive
from laowadi.storage import DB, ROOT, connect


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_evidence_in_db(db: sqlite3.Connection, ref: dict):
    row = db.execute(
        """
        SELECT r.value, r.source_col || r.source_row AS cell, rep.filename, rep.sheet, rep.sha256
        FROM reading r
        JOIN report rep ON rep.day = r.day
        WHERE r.time = ? AND rep.filename = ? AND r.source_col || r.source_row = ?
        """,
        (ref["time"], ref["file"], ref["cell"]),
    ).fetchone()
    assert row is not None, f"Evidence cell not found in DB: {ref}"
    assert row["sheet"] == ref["sheet"], f"Sheet mismatch: {row['sheet']} != {ref['sheet']}"
    assert row["sha256"] == ref["sha256"], f"SHA-256 mismatch for {ref['file']}"
    assert abs(row["value"] - ref["value"]) < 1e-6, f"Value mismatch: {row['value']} != {ref['value']}"


def main():
    db_before = sha256(DB)
    ev_before = sha256(EVENT_DB)

    with closing(connect(DB)) as db:
        archive = Archive(db, EVENT_DB)
        report = evaluate_health(archive)
        md = render_health_markdown(report)

        # 1. Coverage & completeness
        assert report["scope"]["hours"] == 168
        assert report["scope"]["cabinets"] == 25
        assert report["scope"]["completeness"] == {"total": 16800, "valid": 16800, "missing": 0}
        assert report["summary"]["legacy_raw_notices"] == 106
        assert report["summary"]["actionable_events"] < 30, "Event aggregation should compress 106 raw notices"

        by_id = {f["id"]: f for f in report["findings"]}

        # 2. Dimension 1: Metering & physical consistency
        assert "HLTH-MTR-1018" in by_id and by_id["HLTH-MTR-1018"]["severity"] == "high"
        assert report["dimensions"]["metering"]["per_cabinet"]["1018"]["negative_p_hours"] == 168
        assert "HLTH-MTR-1012" in by_id and by_id["HLTH-MTR-1012"]["severity"] == "high"
        assert "HLTH-MTR-1032" in by_id and by_id["HLTH-MTR-1032"]["category"] == "spare_floating_voltage"
        for cab in ("1009", "1010", "1020", "1022", "1023"):
            med = report["dimensions"]["metering"]["per_cabinet"][cab]["ratio_median"]
            assert 0.60 <= med <= 0.72, f"Expected ~2/3 ratio for {cab}, got {med}"

        # 3. Dimension 2: Power quality (voltage & power factor)
        assert "HLTH-PQ-VOLT-10KV-BUS2" in by_id
        bus = report["dimensions"]["power_quality"]["bus_segments"]
        assert "1009" in bus["10kV_I"]["cabinets"] and "1010" in bus["10kV_II"]["cabinets"]
        assert bus["delta_II_minus_I_kv"] > 0.10
        assert "HLTH-PQ-PF-1010" in by_id and "HLTH-PQ-PF-1020" in by_id
        assert report["dimensions"]["power_quality"]["power_factor"]["1020"]["below_080_hours"] >= 160

        # 4. Dimension 3: System mode & balance + polling skew
        assert "HLTH-MODE-SINGLE-01" in by_id
        single = report["dimensions"]["system_balance"]["single_incomer_events"][0]
        assert single["start"] == "2026-11-13 07:00:00"
        assert single["end"] == "2026-11-13 19:00:00"
        assert single["hours"] == 13
        assert "HLTH-BAL-REV-20261112-12" in by_id
        assert "HLTH-BAL-POLLING-SKEW" in by_id
        assert report["dimensions"]["system_balance"]["balance_summary"]["polling_skew_hours"] >= 20
        assert "HLTH-BAL-10KV-GAP" in by_id

        # 5. Dimension 4: Diurnal baseline outliers
        assert report["dimensions"]["diurnal_baseline"]["outlier_episodes"] >= 3
        outlier_times = {item["time"] for item in report["dimensions"]["diurnal_baseline"]["outlier_hours"]}
        assert "2026-11-13 11:00:00" in outlier_times
        assert "2026-11-13 18:00:00" in outlier_times

        # 6. Verify every single evidence cell reference against laowadi.sqlite3
        checked_refs = 0
        for finding in report["findings"]:
            assert finding["evidence"], f"Finding missing evidence: {finding['id']}"
            for ref in finding["evidence"]:
                verify_evidence_in_db(db, ref)
                checked_refs += 1

        # 7. Verify single-day invocation works cleanly
        day_report = evaluate_health(archive, day="2026-11-13")
        assert day_report["scope"]["hours"] == 24
        assert any(f["id"] == "HLTH-MODE-SINGLE-01" for f in day_report["findings"])
        assert "老洼地 35 kV 变电所全站电气体检与健康评估报告" in md

    # 8. Verify CLI JSON & Markdown entrypoints
    cli_json = subprocess.run(
        [sys.executable, "-B", "-m", "laowadi", "health", "--json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    parsed = json.loads(cli_json.stdout)
    assert parsed["summary"]["finding_counts"] == report["summary"]["finding_counts"]

    # 9. Verify databases stayed strictly read-only
    assert sha256(DB) == db_before, "laowadi.sqlite3 was modified!"
    assert sha256(EVENT_DB) == ev_before, "laowadi_events.sqlite3 was modified!"

    print(
        json.dumps(
            {
                "status": "ok",
                "finding_counts": report["summary"]["finding_counts"],
                "actionable_events": report["summary"]["actionable_events"],
                "legacy_raw_notices": report["summary"]["legacy_raw_notices"],
                "verified_evidence_cells": checked_refs,
                "archives_unchanged": True,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
