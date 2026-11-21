"""Initial enterprise relational, time-series, vector & governance schema.

Revision ID: 0001_initial_enterprise_schema
Revises:
Create Date: 2026-11-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001_initial_enterprise_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "station",
        sa.Column("code", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("role", sa.String(48), nullable=False, server_default="surface_substation"),
        sa.Column("mine_id", sa.String(64), nullable=False, server_default="laowadi"),
        sa.Column("has_hourly_archive", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    op.create_table(
        "cabinet",
        sa.Column("station", sa.String(32), primary_key=True),
        sa.Column("number", sa.String(32), primary_key=True),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("bus_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("role", sa.String(32), nullable=False, server_default="feeder"),
        sa.Column("nominal_kv", sa.Float(), nullable=False, server_default="10.0"),
    )

    op.create_table(
        "supply_line",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("from_station", sa.String(32), nullable=False),
        sa.Column("from_cabinet", sa.String(32), nullable=False),
        sa.Column("to_station", sa.String(32), nullable=False),
        sa.Column("to_cabinet", sa.String(32), nullable=False),
        sa.Column("description", sa.String(256), nullable=False, server_default=""),
        sa.UniqueConstraint("from_station", "from_cabinet", "to_station", "to_cabinet", name="uq_supply_line_edge"),
    )

    op.create_table(
        "report_manifest",
        sa.Column("day", sa.String(16), primary_key=True),
        sa.Column("filename", sa.String(128), nullable=False),
        sa.Column("sheet", sa.String(64), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
    )

    op.create_table(
        "diagram_manifest",
        sa.Column("filename", sa.String(128), primary_key=True),
        sa.Column("captured_at", sa.String(64), nullable=False, server_default=""),
        sa.Column("sha256", sa.String(64), nullable=False),
    )

    op.create_table(
        "telemetry_reading",
        sa.Column("ts", sa.DateTime(timezone=False), primary_key=True),
        sa.Column("station", sa.String(32), primary_key=True),
        sa.Column("cabinet", sa.String(32), primary_key=True),
        sa.Column("metric", sa.String(16), primary_key=True),
        sa.Column("day", sa.String(16), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("marker", sa.String(32), nullable=True),
        sa.Column("source_row", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_col", sa.String(16), nullable=False, server_default=""),
    )
    op.create_index("idx_telemetry_station_cab_ts", "telemetry_reading", ["station", "cabinet", "metric", "ts"])
    op.create_index("idx_telemetry_day", "telemetry_reading", ["day", "station"])

    op.create_table(
        "daily_footer_summary",
        sa.Column("day", sa.String(16), primary_key=True),
        sa.Column("station", sa.String(32), primary_key=True),
        sa.Column("cabinet", sa.String(32), primary_key=True),
        sa.Column("metric", sa.String(16), primary_key=True),
        sa.Column("statistic", sa.String(16), primary_key=True),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("source_row", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_col", sa.String(16), nullable=False, server_default=""),
    )

    op.create_table(
        "monitor_event",
        sa.Column("event_id", sa.String(48), primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False, server_default="historical"),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("at_ts", sa.DateTime(timezone=False), nullable=False),
        sa.Column("cabinet", sa.String(32), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_index("idx_monitor_event_ts", "monitor_event", ["at_ts", "cabinet"])

    op.create_table(
        "vector_document",
        sa.Column("doc_id", sa.String(96), primary_key=True),
        sa.Column("doc_kind", sa.String(32), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("source_sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("embedding", sa.Text(), nullable=False),
    )
    op.create_index("idx_vector_doc_kind", "vector_document", ["doc_kind"])

    op.create_table(
        "operator_review",
        sa.Column("finding_id", sa.String(64), primary_key=True),
        sa.Column("disposition", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("switch_state", sa.String(256), nullable=False, server_default=""),
        sa.Column("operator_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("operator_name", sa.String(64), nullable=False, server_default="值班员"),
        sa.Column("updated_at", sa.String(48), nullable=False),
    )

    op.create_table(
        "enterprise_audit_event",
        sa.Column("seq", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(48), nullable=False, unique=True),
        sa.Column("trace_id", sa.String(48), nullable=False),
        sa.Column("timestamp_utc", sa.String(48), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("path", sa.String(256), nullable=False),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("summary_json", sa.Text(), nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("entry_hash", sa.String(64), nullable=False),
    )
    op.create_index("idx_ent_audit_trace", "enterprise_audit_event", ["trace_id"])


def downgrade() -> None:
    op.drop_index("idx_ent_audit_trace", table_name="enterprise_audit_event")
    op.drop_table("enterprise_audit_event")
    op.drop_table("operator_review")
    op.drop_index("idx_vector_doc_kind", table_name="vector_document")
    op.drop_table("vector_document")
    op.drop_index("idx_monitor_event_ts", table_name="monitor_event")
    op.drop_table("monitor_event")
    op.drop_table("daily_footer_summary")
    op.drop_index("idx_telemetry_day", table_name="telemetry_reading")
    op.drop_index("idx_telemetry_station_cab_ts", table_name="telemetry_reading")
    op.drop_table("telemetry_reading")
    op.drop_table("diagram_manifest")
    op.drop_table("report_manifest")
    op.drop_table("supply_line")
    op.drop_table("cabinet")
    op.drop_table("station")
