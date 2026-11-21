"""Alembic Migration Runner, PostgreSQL+TimescaleDB+pgvector DDL Verifier & Archive ETL Migrator.

Strictly read-only on `local/laowadi.sqlite3` and `local/laowadi_events.sqlite3` (SHA-256 verified).
Migrates all 16,800 hourly readings, 2,100 daily footers, 40 cabinets, 211 monitor events, and 76
semantic vector documents (68 coal standards + 8 archive evidence cards) into the Alembic-managed
SQLAlchemy 2.0 database (PostgreSQL+TimescaleDB+pgvector when `LAOWADI_DATABASE_URL` is PG, or
pooled SQLite locally).
"""

from __future__ import annotations

from contextlib import closing, redirect_stdout
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import time
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from laowadi.db.engine import DatabasePoolManager, default_database_url, sqlalchemy_database_url
from laowadi.db.postgres import postgres_mode
from laowadi.db.models import (
    CabinetORM,
    DailyFooterSummaryORM,
    DiagramManifestORM,
    MonitorEventORM,
    ReportManifestORM,
    StationORM,
    SupplyLineORM,
    TelemetryReadingORM,
)
from laowadi.db.vector_store import PgVectorStore
from laowadi.mine_schema import load_mine_profile
from laowadi.monitor import EVENT_DB
from laowadi.queries import Archive
from laowadi.rag import build_laowadi_evidence_cards
from laowadi.source_files import verified_original
from laowadi.standards import load_corpus
from laowadi.storage import DB, ROOT, connect


ALEMBIC_INI = ROOT / "alembic.ini"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_alembic_config(database_url: str | None = None, *, output_buffer: io.StringIO | None = None) -> Config:
    cfg = Config(str(ALEMBIC_INI), stdout=output_buffer) if output_buffer is not None else Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str((ROOT / "laowadi" / "db" / "migrations").resolve()))
    url = sqlalchemy_database_url(database_url or default_database_url())
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def run_alembic_upgrade(database_url: str | None = None, revision: str = "head") -> dict[str, Any]:
    url = database_url or default_database_url()
    cfg = _make_alembic_config(url)
    command.upgrade(cfg, revision)
    return {"action": "upgrade", "target_revision": revision, "database_url": url}


def run_alembic_downgrade(database_url: str | None = None, revision: str = "base") -> dict[str, Any]:
    url = database_url or default_database_url()
    cfg = _make_alembic_config(url)
    command.downgrade(cfg, revision)
    return {"action": "downgrade", "target_revision": revision, "database_url": url}


def generate_postgres_ddl_sql() -> str:
    """Compile the full PostgreSQL + TimescaleDB Hypertable + pgvector HNSW DDL via Alembic offline SQL mode."""
    buf = io.StringIO()
    cfg = _make_alembic_config("postgresql+psycopg://laowadi:laowadi@localhost:5432/laowadi", output_buffer=buf)
    with redirect_stdout(buf):
        command.upgrade(cfg, "head", sql=True)
    return buf.getvalue()


def migrate_sqlite_archives_to_enterprise(
    db_manager: DatabasePoolManager,
    *,
    source_db: Path = DB,
    events_db: Path = EVENT_DB,
    allow_corrections: bool = False,
    source_dir: Path | None = None,
) -> dict[str, Any]:
    """Run Alembic upgrade head and idempotently migrate all Laowadi archives + pgvector embeddings."""
    if postgres_mode() and (source_dir is None or not db_manager.is_postgres):
        raise ValueError("生产归档发布须指定 PostgreSQL 与原件目录。")
    t0 = time.monotonic()
    before_db_sha = _sha256_file(source_db)
    before_ev_sha = _sha256_file(events_db)

    # 1. Ensure the Alembic schema is at the current head.
    run_alembic_upgrade(db_manager.database_url, "head")

    profile = load_mine_profile()
    feeder_meta = profile.feeder_map()
    station_meta = {s.code: s for s in profile.stations}

    with closing(connect(source_db)) as src_conn:
        src_conn.execute("BEGIN")
        stations = [dict(r) for r in src_conn.execute("SELECT code, name, diagram FROM station")]
        cabinets = [dict(r) for r in src_conn.execute("SELECT station, number, label, aliases FROM cabinet")]
        lines = [dict(r) for r in src_conn.execute("SELECT from_station, from_cabinet, to_station, to_cabinet, label FROM line")]
        reports = [dict(r) for r in src_conn.execute("SELECT day, filename, sheet, sha256 FROM report")]
        diagrams = [dict(r) for r in src_conn.execute("SELECT filename, captured_at, sha256 FROM diagram")]
        readings = [
            dict(r)
            for r in src_conn.execute(
                "SELECT day, time, station, cabinet, metric, value, marker, source_row, source_col FROM reading"
            )
        ]
        summaries = [
            dict(r)
            for r in src_conn.execute(
                "SELECT day, station, cabinet, metric, statistic, value, marker, source_row, source_col FROM summary"
            )
        ]
        archive = Archive(src_conn, events_db)
        evidence_cards = build_laowadi_evidence_cards(archive)

    with closing(connect(events_db)) as ev_conn:
        ev_conn.execute("BEGIN")
        runs = [dict(row) for row in ev_conn.execute("SELECT rowid, * FROM monitor_run ORDER BY rowid")]
        runs_mode = {row["id"]: row["mode"] for row in runs}
        events = [
            dict(r)
            for r in ev_conn.execute("SELECT seq, run_id, type, at, cabinet, evidence FROM monitor_event ORDER BY seq")
        ]

    # 2. Idempotent bulk load inside SQLAlchemy 2.0 pooled transaction
    if source_dir is not None:
        for document in reports + diagrams:
            verified_original(source_dir, document["filename"], document["sha256"])

    # Prepare standards and evidence cards for the same PostgreSQL publication.
    standards = load_corpus()
    vector_docs = [
        {
            "doc_id": f"STD::{st['id']}",
            "doc_kind": "standard",
            "title": f"{st['id']} · {st['title']}",
            "content": f"{st['text']}\n适用边界：{st.get('applicability', '')}",
            "metadata": {"id": st["id"], "source": st["source"], "legal_tier": st["legal_tier"], "pdf_page": st["pdf_page"]},
            "source_sha256": hashlib.sha256(st["text"].encode("utf-8")).hexdigest(),
        }
        for st in standards
    ]
    for card in evidence_cards:
        shas = list((card.get("source_sha256") or {}).values())
        vector_docs.append({
            "doc_id": f"CARD::{card['card_id']}",
            "doc_kind": "evidence_card",
            "title": card["title"],
            "content": f"{card['confirmed_fact']}\n引用：{'；'.join(card.get('cell_citations') or [])}\n关键词：{' '.join(card.get('keywords') or [])}",
            "metadata": {
                "card_id": card["card_id"],
                "station": card["station"],
                "cabinets": card["cabinets"],
                "source_files": card["source_files"],
            },
            "source_sha256": shas[0] if shas else "",
        })

    vec_store = PgVectorStore(db_manager)
    version = hashlib.sha256(f"{before_db_sha}:{before_ev_sha}".encode()).hexdigest()
    is_pg = db_manager.engine.dialect.name == "postgresql"
    with db_manager.engine.begin() as conn:
        if is_pg:
            conn.execute(text("SELECT pg_advisory_xact_lock(71112044)"))
            old = dict(conn.execute(text("SELECT day, sha256 FROM report_manifest")).tuples().all())
            incoming = {row["day"]: row["sha256"] for row in reports}
            if old.keys() - incoming.keys():
                raise ValueError("新档案缺少已发布日报，拒绝缩减生产日期范围。")
            if not allow_corrections and any(incoming[day] != digest for day, digest in old.items()):
                raise ValueError("新档案替换了已发布日报，请先核对订正并使用 --allow-corrections。")
            old_diagrams = dict(conn.execute(text("SELECT filename, sha256 FROM diagram_manifest")).tuples().all())
            incoming_diagrams = {row["filename"]: row["sha256"] for row in diagrams}
            if old_diagrams.keys() - incoming_diagrams.keys():
                raise ValueError("新档案缺少已发布图示。")
            if not allow_corrections and any(incoming_diagrams[name] != digest for name, digest in old_diagrams.items()):
                raise ValueError("新档案替换了已发布图示，请先核对订正并使用 --allow-corrections。")
            sources = {row["day"]: (row["filename"], row["sha256"]) for row in reports}
            historical = [
                row for row in runs if row["complete"] == 1 and row["mode"] == "historical"
            ]
            if not historical or {
                row["day"]: (row["filename"], row["sha256"])
                for row in json.loads(historical[-1]["description"])["source_reports"]
            } != sources:
                raise ValueError("生产发布需要覆盖最新全部日报且源哈希一致的完整历史回放。")
            conn.execute(text("DELETE FROM laowadi_archive.monitor_run"))
        for tbl in (
            "monitor_event",
            "daily_footer_summary",
            "telemetry_reading",
            "diagram_manifest",
            "report_manifest",
            "supply_line",
            "cabinet",
            "station",
        ):
            conn.execute(text(f"DELETE FROM {tbl}"))

        conn.execute(
            StationORM.__table__.insert(),
            [
                {
                    "code": s["code"],
                    "diagram": s["diagram"],
                    "name": s["name"],
                    "role": station_meta[s["code"]].role if s["code"] in station_meta else "surface_substation",
                    "mine_id": profile.mine_id,
                    "has_hourly_archive": station_meta[s["code"]].has_hourly_archive if s["code"] in station_meta else True,
                }
                for s in stations
            ],
        )

        conn.execute(
            CabinetORM.__table__.insert(),
            [
                {
                    "station": c["station"],
                    "number": c["number"],
                    "label": c["label"],
                    "aliases": c["aliases"],
                    "bus_id": feeder_meta[c["number"]].bus_id if (c["station"] == profile.primary_station and c["number"] in feeder_meta) else "",
                    "role": feeder_meta[c["number"]].role if (c["station"] == profile.primary_station and c["number"] in feeder_meta) else "feeder",
                    "nominal_kv": feeder_meta[c["number"]].nominal_kv if (c["station"] == profile.primary_station and c["number"] in feeder_meta) else 10.0,
                }
                for c in cabinets
            ],
        )

        if lines:
            conn.execute(
                SupplyLineORM.__table__.insert(),
                [
                    {
                        "from_station": ln["from_station"],
                        "from_cabinet": ln["from_cabinet"],
                        "to_station": ln["to_station"],
                        "to_cabinet": ln["to_cabinet"],
                        "description": ln["label"] or "",
                    }
                    for ln in lines
                ],
            )

        conn.execute(ReportManifestORM.__table__.insert(), reports)
        conn.execute(DiagramManifestORM.__table__.insert(), diagrams)

        # Batch insert 16,800 hourly telemetry readings in 2,000-row chunks
        reading_rows = [
            {
                "ts": datetime.fromisoformat(r["time"]),
                "station": r["station"],
                "cabinet": r["cabinet"],
                "metric": r["metric"],
                "day": r["day"],
                "value": r["value"],
                "marker": r["marker"],
                "source_row": int(r["source_row"] or 0),
                "source_col": str(r["source_col"] or ""),
            }
            for r in readings
        ]
        for i in range(0, len(reading_rows), 2000):
            conn.execute(TelemetryReadingORM.__table__.insert(), reading_rows[i : i + 2000])

        summary_rows = [
            {
                "day": s["day"],
                "station": s["station"],
                "cabinet": s["cabinet"],
                "metric": s["metric"],
                "statistic": s["statistic"],
                "value": s["value"],
                "marker": s["marker"],
                "source_row": int(s["source_row"] or 0),
                "source_col": str(s["source_col"] or ""),
            }
            for s in summaries
        ]
        for i in range(0, len(summary_rows), 1000):
            conn.execute(DailyFooterSummaryORM.__table__.insert(), summary_rows[i : i + 1000])

        if events:
            conn.execute(
                MonitorEventORM.__table__.insert(),
                [
                    {
                        "event_id": f"LW-{int(e['seq']):06d}",
                        "run_id": e["run_id"],
                        "mode": runs_mode.get(e["run_id"], "historical"),
                        "event_type": e["type"],
                        "at_ts": datetime.fromisoformat(e["at"]),
                        "cabinet": e["cabinet"],
                        "payload_json": e["evidence"],
                    }
                    for e in events
                ],
            )

        if is_pg:
            vec_count = vec_store.upsert_documents(vector_docs, connection=conn)
            conn.execute(text("""
                INSERT INTO laowadi_archive.monitor_run
                    (rowid,id,mode,description,last_time,frames_seen,complete)
                VALUES (:rowid,:id,:mode,:description,:last_time,:frames_seen,:complete)
            """), runs)
            conn.execute(text("""
                INSERT INTO laowadi_archive.publication
                    (singleton,version,published_at,source_db_sha256,event_db_sha256)
                VALUES (1,:version,clock_timestamp(),:source,:events)
                ON CONFLICT (singleton) DO UPDATE SET
                    version=excluded.version, published_at=excluded.published_at,
                    source_db_sha256=excluded.source_db_sha256, event_db_sha256=excluded.event_db_sha256
            """), {"version": version, "source": before_db_sha, "events": before_ev_sha})
        if _sha256_file(source_db) != before_db_sha or _sha256_file(events_db) != before_ev_sha:
            raise ValueError("输入档案在导入期间发生变化，发布已回滚。")

    if not is_pg:
        vec_count = vec_store.upsert_documents(vector_docs)

    assert _sha256_file(source_db) == before_db_sha and _sha256_file(events_db) == before_ev_sha, "Source archive mutated!"

    return {
        "status": "ok",
        "database_url": db_manager.masked_url(),
        "alembic_revision": db_manager.current_alembic_revision(),
        "elapsed_seconds": round(time.monotonic() - t0, 3),
        "source_archives_unchanged": True,
        "archive_version": version,
        "migrated_counts": {
            "stations": len(stations),
            "cabinets": len(cabinets),
            "supply_lines": len(lines),
            "reports": len(reports),
            "diagrams": len(diagrams),
            "telemetry_readings": len(reading_rows),
            "daily_footer_summaries": len(summary_rows),
            "monitor_events": len(events),
            "vector_documents": vec_count,
        },
        "extensions": db_manager.extensions_status(),
    }
