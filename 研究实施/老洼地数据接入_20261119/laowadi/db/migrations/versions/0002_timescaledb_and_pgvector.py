"""Enable TimescaleDB hypertable, compression, continuous aggregate & pgvector HNSW cosine index.

Revision ID: 0002_timescaledb_and_pgvector
Revises: 0001_initial_enterprise_schema
Create Date: 2026-11-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_timescaledb_and_pgvector"
down_revision = "0001_initial_enterprise_schema"
branch_labels = None
depends_on = None


def _pg_available_extensions() -> set[str]:
    if op.get_context().as_sql:
        return {"timescaledb", "vector"}
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT name FROM pg_available_extensions WHERE name IN ('timescaledb', 'vector')"))
    return {r[0] for r in rows}


def upgrade() -> None:
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        avail = _pg_available_extensions()

        # 1 & 2 & 3. TimescaleDB Hypertable (7-day chunks), 30-day columnar compression & Continuous Aggregate
        if "timescaledb" in avail:
            op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
            op.execute(
                """
                SELECT create_hypertable(
                    'telemetry_reading',
                    by_range('ts', INTERVAL '7 days'),
                    if_not_exists => TRUE,
                    migrate_data => TRUE
                )
                """
            )
            op.execute(
                """
                ALTER TABLE telemetry_reading SET (
                    timescaledb.compress,
                    timescaledb.compress_segmentby = 'station,cabinet,metric',
                    timescaledb.compress_orderby = 'ts DESC'
                )
                """
            )
            op.execute(
                """
                SELECT add_compression_policy(
                    'telemetry_reading',
                    INTERVAL '30 days',
                    if_not_exists => TRUE
                )
                """
            )
            create_cag_sql = """
                CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_daily_rollup
                WITH (timescaledb.continuous) AS
                SELECT
                    time_bucket('1 day', ts) AS bucket_day,
                    station,
                    cabinet,
                    metric,
                    COUNT(value) AS valid_points,
                    MIN(value) AS min_val,
                    MAX(value) AS max_val,
                    AVG(value) AS avg_val
                FROM telemetry_reading
                GROUP BY bucket_day, station, cabinet, metric
                WITH NO DATA
                """
            add_cag_policy_sql = """
                SELECT add_continuous_aggregate_policy(
                    'telemetry_daily_rollup',
                    start_offset => INTERVAL '14 days',
                    end_offset => INTERVAL '1 hour',
                    schedule_interval => INTERVAL '1 hour',
                    if_not_exists => TRUE
                )
                """
            if op.get_context().as_sql:
                op.execute(create_cag_sql)
                op.execute(add_cag_policy_sql)
            else:
                with op.get_context().autocommit_block():
                    op.execute(create_cag_sql)
                    op.execute(add_cag_policy_sql)
        else:
            op.create_index("idx_telemetry_ts_metric", "telemetry_reading", ["ts", "metric"])

        # 4. Upgrade vector_document.embedding to native pgvector VECTOR(128) + HNSW cosine index
        if "vector" in avail:
            op.execute("CREATE EXTENSION IF NOT EXISTS vector")
            op.execute(
                "ALTER TABLE vector_document ALTER COLUMN embedding TYPE vector(128) USING embedding::vector(128)"
            )
            op.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_vector_document_hnsw
                ON vector_document
                USING hnsw (embedding vector_cosine_ops)
                WITH (m = 16, ef_construction = 64)
                """
            )
        else:
            op.create_index("idx_vector_sha", "vector_document", ["source_sha256"])
    else:
        # Local SQLite fallback indexes matching the PostgreSQL access paths
        op.create_index("idx_telemetry_ts_metric", "telemetry_reading", ["ts", "metric"])
        op.create_index("idx_vector_sha", "vector_document", ["source_sha256"])


def downgrade() -> None:
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        avail = _pg_available_extensions()
        if "vector" in avail:
            op.execute("DROP INDEX IF EXISTS idx_vector_document_hnsw")
            op.execute("ALTER TABLE vector_document ALTER COLUMN embedding TYPE TEXT USING embedding::text")
        else:
            op.execute("DROP INDEX IF EXISTS idx_vector_sha")
        if "timescaledb" in avail:
            if op.get_context().as_sql:
                op.execute("DROP MATERIALIZED VIEW IF EXISTS telemetry_daily_rollup CASCADE")
            else:
                with op.get_context().autocommit_block():
                    op.execute("DROP MATERIALIZED VIEW IF EXISTS telemetry_daily_rollup CASCADE")
        else:
            op.execute("DROP INDEX IF EXISTS idx_telemetry_ts_metric")
    else:
        op.drop_index("idx_vector_sha", table_name="vector_document")
        op.drop_index("idx_telemetry_ts_metric", table_name="telemetry_reading")
