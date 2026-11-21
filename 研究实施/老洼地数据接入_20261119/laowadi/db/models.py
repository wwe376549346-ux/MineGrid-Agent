"""Unified SQLAlchemy 2.0 Declarative ORM Models for Coal-Mine Power Distribution Agent.

Designed for PostgreSQL 16 + TimescaleDB (Hypertable on `telemetry_reading`) + pgvector (HNSW on
`vector_document.embedding`), while remaining 100% portable to SQLite for zero-dependency local dev.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


VECTOR_DIM = 128


class Base(DeclarativeBase):
    pass


# ── 1. Relational Asset & Archive Manifest Tables ──────────────────────────────
class StationORM(Base):
    __tablename__ = "station"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    diagram: Mapped[str] = mapped_column(Text, nullable=False, default="")
    role: Mapped[str] = mapped_column(String(48), nullable=False, default="surface_substation")
    mine_id: Mapped[str] = mapped_column(String(64), nullable=False, default="laowadi")
    has_hourly_archive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CabinetORM(Base):
    __tablename__ = "cabinet"

    station: Mapped[str] = mapped_column(String(32), primary_key=True)
    number: Mapped[str] = mapped_column(String(32), primary_key=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    aliases: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    bus_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="feeder")
    nominal_kv: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)


class SupplyLineORM(Base):
    __tablename__ = "supply_line"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_station: Mapped[str] = mapped_column(String(32), nullable=False)
    from_cabinet: Mapped[str] = mapped_column(String(32), nullable=False)
    to_station: Mapped[str] = mapped_column(String(32), nullable=False)
    to_cabinet: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(String(256), nullable=False, default="")

    __table_args__ = (
        UniqueConstraint("from_station", "from_cabinet", "to_station", "to_cabinet", name="uq_supply_line_edge"),
    )


class ReportManifestORM(Base):
    __tablename__ = "report_manifest"

    day: Mapped[str] = mapped_column(String(16), primary_key=True)
    filename: Mapped[str] = mapped_column(String(128), nullable=False)
    sheet: Mapped[str] = mapped_column(String(64), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class DiagramManifestORM(Base):
    __tablename__ = "diagram_manifest"

    filename: Mapped[str] = mapped_column(String(128), primary_key=True)
    captured_at: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)


# ── 2. Time-Series Tables (TimescaleDB Hypertable on PostgreSQL!) ──────────────
class TelemetryReadingORM(Base):
    """Primary time-series table; converted to a TimescaleDB Hypertable (chunk=7d) on PostgreSQL."""

    __tablename__ = "telemetry_reading"

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=False), primary_key=True)
    station: Mapped[str] = mapped_column(String(32), primary_key=True)
    cabinet: Mapped[str] = mapped_column(String(32), primary_key=True)
    metric: Mapped[str] = mapped_column(String(16), primary_key=True)
    day: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    marker: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_row: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_col: Mapped[str] = mapped_column(String(16), nullable=False, default="")

    __table_args__ = (
        Index("idx_telemetry_station_cab_ts", "station", "cabinet", "metric", "ts"),
        Index("idx_telemetry_day", "day", "station"),
    )


class DailyFooterSummaryORM(Base):
    __tablename__ = "daily_footer_summary"

    day: Mapped[str] = mapped_column(String(16), primary_key=True)
    station: Mapped[str] = mapped_column(String(32), primary_key=True)
    cabinet: Mapped[str] = mapped_column(String(32), primary_key=True)
    metric: Mapped[str] = mapped_column(String(16), primary_key=True)
    statistic: Mapped[str] = mapped_column(String(16), primary_key=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    marker: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_row: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_col: Mapped[str] = mapped_column(String(16), nullable=False, default="")


class MonitorEventORM(Base):
    __tablename__ = "monitor_event"

    event_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="historical")
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    at_ts: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    cabinet: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("idx_monitor_event_ts", "at_ts", "cabinet"),)


# ── 3. Vector Knowledge Store (pgvector VECTOR(128) + HNSW on PostgreSQL!) ─────
class VectorDocumentORM(Base):
    """Stores coal-mine standards & archive evidence cards with 128-dim normalized embeddings.

    Stored as JSON text on SQLite and altered to native `vector(128)` with HNSW cosine index
    by Alembic revision `0002_timescaledb_and_pgvector` on PostgreSQL.
    """

    __tablename__ = "vector_document"

    doc_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    doc_kind: Mapped[str] = mapped_column(String(32), nullable=False)  # "standard" | "evidence_card"
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    embedding: Mapped[str] = mapped_column(Text, nullable=False)  # pgvector text literal "[0.1,0.2,...]"

    __table_args__ = (Index("idx_vector_doc_kind", "doc_kind"),)


# ── 4. Operational Governance, Sessions, Reviews & Distributed Queue Tables ────
class OperatorReviewORM(Base):
    __tablename__ = "operator_review"

    finding_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    disposition: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    switch_state: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    operator_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    operator_name: Mapped[str] = mapped_column(String(64), nullable=False, default="值班员")
    updated_at: Mapped[str] = mapped_column(String(48), nullable=False)


class AuditLedgerORM(Base):
    __tablename__ = "enterprise_audit_event"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(48), nullable=False)
    timestamp_utc: Mapped[str] = mapped_column(String(48), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    path: Mapped[str] = mapped_column(String(256), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (Index("idx_ent_audit_trace", "trace_id"),)


def model_table_names() -> list[str]:
    return sorted(Base.metadata.tables.keys())


def describe_schema() -> dict[str, Any]:
    return {
        "tables": model_table_names(),
        "table_count": len(Base.metadata.tables),
        "timescaledb_hypertables": ["telemetry_reading"],
        "timescaledb_continuous_aggregates": ["telemetry_daily_rollup"],
        "pgvector_tables": [{"table": "vector_document", "column": "embedding", "dim": VECTOR_DIM, "index": "hnsw(vector_cosine_ops)"}],
    }
