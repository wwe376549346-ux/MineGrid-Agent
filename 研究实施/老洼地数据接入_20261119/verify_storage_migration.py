"""Verification Suite: PostgreSQL + TimescaleDB + pgvector, Alembic Migrations & SQLAlchemy 2.0 Connection Pool.

Verifies:
1. Alembic offline PostgreSQL 16 + TimescaleDB Hypertable + Continuous Aggregate + pgvector HNSW DDL compilation.
2. Live Alembic `upgrade head` + idempotent full archive ETL (16,800 readings, 2,100 footers, 40 cabinets, 211 events, 76 vectors).
3. Concurrent SQLAlchemy 2.0 `QueuePool` stress test (16 threads) + pool metrics verification.
4. `PgVectorStore` 128-dim semantic search accuracy & `doc_kind` filtering.
5. Full Alembic reversibility (`downgrade base` -> `upgrade head`).
6. FastAPI `/api/storage/*` endpoints, RBAC enforcement (`viewer` 403 vs `engineer` 200 on migrate), & Prometheus pool/vector gauges.
7. Strict read-only SHA-256 preservation of `local/laowadi.sqlite3` and `local/laowadi_events.sqlite3`.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient
from sqlalchemy import text

from laowadi.api import create_app
from laowadi.db.engine import DatabasePoolManager
from laowadi.db.migrator import (
    generate_postgres_ddl_sql,
    migrate_sqlite_archives_to_enterprise,
    run_alembic_downgrade,
    run_alembic_upgrade,
)
from laowadi.db.vector_store import PgVectorStore
from laowadi.monitor import EVENT_DB
from laowadi.security import issue_token
from laowadi.storage import DB
from laowadi.workbench import SOURCE_DIR, Workbench


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_all() -> None:
    db_sha_before = _sha256(DB)
    ev_sha_before = _sha256(EVENT_DB)

    # 1. Alembic Offline PostgreSQL + TimescaleDB + pgvector DDL Compilation
    ddl = generate_postgres_ddl_sql()
    required_sql_tokens = [
        "CREATE EXTENSION IF NOT EXISTS timescaledb;",
        "CREATE EXTENSION IF NOT EXISTS vector;",
        "create_hypertable",
        "INTERVAL '7 days'",
        "timescaledb.compress",
        "add_compression_policy",
        "INTERVAL '30 days'",
        "CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_daily_rollup",
        "WITH (timescaledb.continuous)",
        "add_continuous_aggregate_policy",
        "ALTER TABLE vector_document ALTER COLUMN embedding TYPE vector(128)",
        "USING hnsw (embedding vector_cosine_ops)",
    ]
    for tok in required_sql_tokens:
        assert tok in ddl, f"Missing PostgreSQL/TimescaleDB/pgvector DDL token: {tok}"
    assert ";;" not in ddl, "Double semicolon syntax artifact found in compiled PostgreSQL DDL"
    print(f"[1/7] PostgreSQL 16 + TimescaleDB + pgvector HNSW DDL compilation OK ({len(ddl)} chars, {ddl.count(';')} statements)")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db_url = f"sqlite+pysqlite:///{tmp / 'enterprise_verify.sqlite3'}"
        mgr = DatabasePoolManager(db_url)
        try:
            # 2. Live Alembic upgrade head + Idempotent Archive ETL
            res1 = migrate_sqlite_archives_to_enterprise(mgr, source_db=DB, events_db=EVENT_DB)
            assert res1["status"] == "ok"
            assert res1["alembic_revision"] == "0003_archive_runtime"
            assert res1["source_archives_unchanged"] is True
            counts1 = res1["migrated_counts"]
            assert counts1 == {
                "stations": 2,
                "cabinets": 40,
                "supply_lines": 2,
                "reports": 7,
                "diagrams": 2,
                "telemetry_readings": 16800,
                "daily_footer_summaries": 2100,
                "monitor_events": 211,
                "vector_documents": 76,
            }, f"Unexpected migrated counts: {counts1}"

            # Idempotent re-run must yield identical table counts
            res2 = migrate_sqlite_archives_to_enterprise(mgr, source_db=DB, events_db=EVENT_DB)
            assert res2["migrated_counts"] == counts1
            status = mgr.status_summary()
            assert status["table_counts"]["telemetry_reading"] == 16800
            assert status["table_counts"]["vector_document"] == 76
            print(f"[2/7] Alembic upgrade + idempotent ETL OK (16,800 readings, 2,100 footers, 211 events, 76 vectors in {res1['elapsed_seconds']}s)")

            # 3. Concurrent SQLAlchemy 2.0 QueuePool Stress Test (16 threads)
            def _pooled_query(idx: int) -> tuple[int, int]:
                with mgr.engine.connect() as conn:
                    r_cnt = int(conn.execute(text("SELECT COUNT(*) FROM telemetry_reading WHERE cabinet = '1006'")).scalar() or 0)
                    v_cnt = int(conn.execute(text("SELECT COUNT(*) FROM vector_document")).scalar() or 0)
                    return r_cnt, v_cnt

            with ThreadPoolExecutor(max_workers=16) as pool:
                results = list(pool.map(_pooled_query, range(32)))
            assert all(r > 0 and v == 76 for r, v in results), f"Concurrent pooled queries inconsistent: {results[:3]}"
            pm = mgr.pool_metrics()
            assert pm["pool_class"] == "QueuePool"
            assert pm["pool_size"] == 10
            assert pm["max_overflow"] == 20
            assert pm["pool_pre_ping"] is True
            assert pm["checked_out"] == 0
            print(f"[3/7] SQLAlchemy 2.0 QueuePool 16-thread stress test OK ({pm})")

            # 4. pgvector 128-Dim Semantic Vector Search & Kind Filtering
            vs = PgVectorStore(mgr)
            q1 = vs.search("母线高压超10.70怎么调分接头", top_k=3)
            q1_ids = [h["doc_id"] for h in q1["hits"]]
            assert "CARD::EVC-LW-10KV-BUS-VOLTAGE" in q1_ids, f"Expected overvoltage card in Top-3, got {q1_ids}"
            assert any("12325" in did for did in q1_ids), f"Expected GB/T 12325 in Top-3, got {q1_ids}"

            q2 = vs.search("1018 逆潮流 功率倒灌 2号主变", top_k=3)
            q2_ids = [h["doc_id"] for h in q2["hits"]]
            assert "CARD::EVC-LW-T2-REVERSE-AND-SKEW" in q2_ids, f"Expected T2 reverse card in Top-3, got {q2_ids}"
            assert "CARD::EVC-LW-1018-POLARITY" in q2_ids, f"Expected 1018 polarity card in Top-3, got {q2_ids}"

            q_std = vs.search("安全规程 矿井两回路电源", top_k=4, doc_kind="standard")
            assert len(q_std["hits"]) == 4 and all(h["doc_kind"] == "standard" for h in q_std["hits"])
            q_card = vs.search("单进线 303 306", top_k=3, doc_kind="evidence_card")
            assert len(q_card["hits"]) == 3 and all(h["doc_kind"] == "evidence_card" for h in q_card["hits"])
            print(f"[4/7] pgvector 128-dim semantic search & filtering OK (q1={q1_ids}, q2={q2_ids})")

            # 5. Full Alembic Reversibility (downgrade base -> upgrade head)
            run_alembic_downgrade(db_url, "base")
            assert mgr.current_alembic_revision() is None, "Downgrade to base did not clear alembic revision"
            run_alembic_upgrade(db_url, "head")
            assert mgr.current_alembic_revision() == "0003_archive_runtime"
            print("[5/7] Alembic downgrade base -> upgrade head reversibility OK")
        finally:
            mgr.dispose()

        # 6. FastAPI Storage APIs, RBAC & Prometheus Metrics
        api_db_url = f"sqlite+pysqlite:///{tmp / 'api_enterprise.sqlite3'}"
        wb = Workbench(
            DB,
            EVENT_DB,
            SOURCE_DIR,
            model=None,
            reviews_db=tmp / "reviews.sqlite3",
            sessions_db=tmp / "sessions.sqlite3",
            audit_db=tmp / "audit.sqlite3",
            jobs_db=tmp / "jobs.sqlite3",
            database_url=api_db_url,
        )
        try:
            app = create_app(wb, require_auth=True, rate_limit_per_min=300)
            client = TestClient(app)

            viewer_hdr = {"Authorization": f"Bearer {issue_token('auditor', 'viewer')['access_token']}"}
            eng_hdr = {"Authorization": f"Bearer {issue_token('chief_eng', 'engineer')['access_token']}"}

            # RBAC: viewer must be forbidden from triggering migration
            r_forbid = client.post("/api/storage/migrate", json={}, headers=viewer_hdr)
            assert r_forbid.status_code == 403, f"Expected 403 for viewer migrate, got {r_forbid.status_code}"

            # Engineer triggers full migration
            r_mig = client.post("/api/storage/migrate", json={}, headers=eng_hdr)
            assert r_mig.status_code == 200, r_mig.text
            mig_data = r_mig.json()
            assert mig_data["migrated_counts"]["telemetry_readings"] == 16800
            assert mig_data["migrated_counts"]["vector_documents"] == 76

            # Viewer reads status, DDL, and executes vector search
            r_stat = client.get("/api/storage/status", headers=viewer_hdr)
            assert r_stat.status_code == 200
            assert r_stat.json()["alembic_revision"] == "0003_archive_runtime"

            r_ddl = client.get("/api/storage/postgres-ddl", headers=viewer_hdr)
            assert r_ddl.status_code == 200
            assert "create_hypertable" in r_ddl.json()["ddl_sql"]

            r_vec = client.post(
                "/api/storage/vector-search",
                json={"query": "1018 逆潮流", "top_k": 3},
                headers=viewer_hdr,
            )
            assert r_vec.status_code == 200
            assert len(r_vec.json()["hits"]) == 3

            # Prometheus metrics expose SQLAlchemy pool & pgvector document gauges
            r_prom = client.get("/metrics", headers=viewer_hdr)
            assert r_prom.status_code == 200
            prom_text = r_prom.text
            assert "laowadi_db_pool_size 10" in prom_text
            assert "laowadi_db_pool_checked_in " in prom_text
            assert "laowadi_db_pool_checked_out " in prom_text
            assert "laowadi_vector_documents_total 76" in prom_text
            print("[6/7] FastAPI /api/storage/*, RBAC (403/200) & Prometheus pool/vector metrics OK")
        finally:
            wb.db_manager.dispose()

    # 7. Source Archives SHA-256 Strict Integrity
    assert _sha256(DB) == db_sha_before, "local/laowadi.sqlite3 was modified!"
    assert _sha256(EVENT_DB) == ev_sha_before, "local/laowadi_events.sqlite3 was modified!"
    print("[7/7] Read-only source archives SHA-256 strictly unchanged OK")
    print("\nALL 7 STORAGE MIGRATION VERIFICATION CHECKS PASSED!")


def verify_live_postgres(pg_url: str) -> None:
    """Live end-to-end verification against a real PostgreSQL + TimescaleDB + pgvector server."""
    db_sha_before = _sha256(DB)
    ev_sha_before = _sha256(EVENT_DB)
    mgr = DatabasePoolManager(pg_url)
    try:
        # 1. Clean downgrade -> upgrade -> full archive ETL
        run_alembic_downgrade(pg_url, "base")
        assert mgr.current_alembic_revision() is None
        res = migrate_sqlite_archives_to_enterprise(mgr, source_db=DB, events_db=EVENT_DB)
        assert res["status"] == "ok"
        assert res["alembic_revision"] == "0003_archive_runtime"
        assert res["migrated_counts"]["telemetry_readings"] == 16800
        assert res["migrated_counts"]["vector_documents"] == 76

        # 2. Verify TimescaleDB Hypertable, Continuous Aggregate & pgvector HNSW are ACTUALLY active
        ext = mgr.extensions_status()
        assert ext["timescaledb_active"] is True, f"TimescaleDB not active: {ext}"
        assert ext["pgvector_active"] is True, f"pgvector not active: {ext}"
        assert ext["hypertable_configured"] is True, f"Hypertable not configured: {ext}"
        assert ext["continuous_aggregate_configured"] is True, f"Continuous aggregate not configured: {ext}"
        assert ext["hnsw_index_configured"] is True, f"HNSW index not configured: {ext}"

        # 3. Materialize TimescaleDB Continuous Aggregate and verify rolled-up rows
        with mgr.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("CALL refresh_continuous_aggregate('telemetry_daily_rollup', NULL, NULL)"))
            cag_rows = int(conn.execute(text("SELECT COUNT(*) FROM telemetry_daily_rollup")).scalar() or 0)
            chunk_rows = int(
                conn.execute(
                    text("SELECT COUNT(*) FROM timescaledb_information.chunks WHERE hypertable_name = 'telemetry_reading'")
                ).scalar()
                or 0
            )
        assert cag_rows > 0, f"Continuous aggregate empty: {cag_rows}"
        assert chunk_rows >= 1, f"Expected >=1 TimescaleDB hypertable chunk, got {chunk_rows}"

        # 4. Native pgvector HNSW `<=>` cosine search
        vs = PgVectorStore(mgr)
        q1 = vs.search("母线高压超10.70怎么调分接头", top_k=3)
        assert q1["engine"] == "pgvector_hnsw_cosine", f"Expected native pgvector engine, got {q1['engine']}"
        q1_ids = [h["doc_id"] for h in q1["hits"]]
        assert "CARD::EVC-LW-10KV-BUS-VOLTAGE" in q1_ids

        # 5. Concurrent QueuePool stress test on live PostgreSQL
        def _pg_query(_i: int) -> int:
            with mgr.engine.connect() as conn:
                return int(conn.execute(text("SELECT COUNT(*) FROM telemetry_reading WHERE cabinet = '1006'")).scalar() or 0)

        with ThreadPoolExecutor(max_workers=16) as pool:
            vals = list(pool.map(_pg_query, range(32)))
        assert all(v > 0 for v in vals)
        assert mgr.pool_metrics()["checked_out"] == 0

        # 6. Verify source archives strictly untouched
        assert _sha256(DB) == db_sha_before
        assert _sha256(EVENT_DB) == ev_sha_before
        print(
            f"[LIVE PG+TIMESCALEDB+PGVECTOR OK] {ext['dialect']} | hypertable chunks={chunk_rows} | "
            f"continuous_aggregate rows={cag_rows} | engine={q1['engine']} | top3={q1_ids} | ETL={res['elapsed_seconds']}s"
        )
    finally:
        mgr.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-pg-url", default=os.environ.get("LAOWADI_LIVE_PG_URL", ""))
    args = parser.parse_args()
    verify_all()
    if args.live_pg_url:
        verify_live_postgres(args.live_pg_url)
