"""SQLAlchemy 2.0 Connection Pool Manager supporting PostgreSQL+TimescaleDB+pgvector & SQLite fallback."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import threading
from typing import Any, Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from laowadi.db.models import Base, describe_schema
from laowadi.storage import ROOT


ENTERPRISE_DB_FILE = ROOT / "local" / "laowadi_enterprise.sqlite3"
DB_ENV_FILE = ROOT / "local" / ".db.env"


def sqlalchemy_database_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def default_database_url() -> str:
    env_url = os.environ.get("LAOWADI_DATABASE_URL", "").strip()
    if env_url:
        return env_url
    if DB_ENV_FILE.is_file():
        for raw_line in DB_ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "LAOWADI_DATABASE_URL" and v.strip():
                return v.strip().strip('"').strip("'")
    ENTERPRISE_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+pysqlite:///{ENTERPRISE_DB_FILE.resolve()}"


class DatabasePoolManager:
    """Thread-safe SQLAlchemy 2.0 Engine & QueuePool connection pool manager."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        pool_size: int = 10,
        max_overflow: int = 20,
        pool_timeout: float = 15.0,
        pool_recycle: int = 1800,
    ):
        self.database_url = database_url or default_database_url()
        self.is_postgres = self.database_url.startswith("postgresql")
        self.pool_size = int(pool_size)
        self.max_overflow = int(max_overflow)
        self.pool_timeout = float(pool_timeout)
        self.pool_recycle = int(pool_recycle)

        engine_kwargs: dict[str, Any] = {
            "poolclass": QueuePool,
            "pool_size": self.pool_size,
            "max_overflow": self.max_overflow,
            "pool_timeout": self.pool_timeout,
            "pool_recycle": self.pool_recycle,
            "pool_pre_ping": True,
            "future": True,
        }
        if not self.is_postgres:
            engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15.0}

        self.engine: Engine = create_engine(sqlalchemy_database_url(self.database_url), **engine_kwargs)

        if not self.is_postgres:
            @event.listens_for(self.engine, "connect")
            def _set_sqlite_pragmas(dbapi_conn, _conn_record):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA busy_timeout=15000")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        sess = self.session_factory()
        try:
            yield sess
            sess.commit()
        except Exception:
            sess.rollback()
            raise
        finally:
            sess.close()

    def create_all_direct(self) -> None:
        Base.metadata.create_all(self.engine)

    def current_alembic_revision(self) -> str | None:
        try:
            insp = inspect(self.engine)
            if "alembic_version" not in insp.get_table_names():
                return None
            with self.engine.connect() as conn:
                row = conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).fetchone()
            return str(row[0]) if row else None
        except Exception:
            return None

    def extensions_status(self) -> dict[str, Any]:
        if not self.is_postgres:
            return {
                "dialect": "sqlite+pysqlite (local dev fallback; set LAOWADI_DATABASE_URL for PostgreSQL)",
                "timescaledb_active": False,
                "pgvector_active": False,
                "hypertable_configured": False,
                "hnsw_index_configured": False,
                "ready_for_postgres_migration": True,
            }
        exts = set()
        hypertable = False
        cag = False
        hnsw = False
        pg_version = ""
        try:
            with self.engine.connect() as conn:
                pg_version = str(conn.execute(text("SHOW server_version")).scalar() or "")
                for row in conn.execute(text("SELECT extname FROM pg_extension")):
                    exts.add(row[0])
                if "timescaledb" in exts:
                    ht = conn.execute(
                        text("SELECT COUNT(*) FROM timescaledb_information.hypertables WHERE hypertable_name = 'telemetry_reading'")
                    ).scalar()
                    hypertable = bool(ht and int(ht) > 0)
                    ca = conn.execute(
                        text("SELECT COUNT(*) FROM timescaledb_information.continuous_aggregates WHERE view_name = 'telemetry_daily_rollup'")
                    ).scalar()
                    cag = bool(ca and int(ca) > 0)
                if "vector" in exts:
                    idx = conn.execute(
                        text("SELECT COUNT(*) FROM pg_indexes WHERE tablename = 'vector_document' AND indexname = 'idx_vector_document_hnsw'")
                    ).scalar()
                    hnsw = bool(idx and int(idx) > 0)
        except Exception:
            pass
        return {
            "dialect": f"postgresql+psycopg (PG {pg_version})" if pg_version else "postgresql+psycopg",
            "postgres_version": pg_version,
            "timescaledb_active": "timescaledb" in exts,
            "pgvector_active": "vector" in exts,
            "hypertable_configured": hypertable,
            "continuous_aggregate_configured": cag,
            "hnsw_index_configured": hnsw,
            "ready_for_postgres_migration": True,
        }

    def pool_metrics(self) -> dict[str, Any]:
        pool = self.engine.pool
        return {
            "pool_class": type(pool).__name__,
            "pool_size": int(pool.size()) if hasattr(pool, "size") else self.pool_size,
            "checked_in": int(pool.checkedin()) if hasattr(pool, "checkedin") else 0,
            "checked_out": int(pool.checkedout()) if hasattr(pool, "checkedout") else 0,
            "overflow": int(pool.overflow()) if hasattr(pool, "overflow") else 0,
            "max_overflow": self.max_overflow,
            "pool_pre_ping": True,
            "pool_recycle_s": self.pool_recycle,
        }

    def table_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        try:
            insp = inspect(self.engine)
            existing = set(insp.get_table_names())
            with self.engine.connect() as conn:
                for tbl in sorted(Base.metadata.tables.keys()):
                    if tbl in existing:
                        counts[tbl] = int(conn.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar() or 0)
                    else:
                        counts[tbl] = 0
        except Exception:
            pass
        return counts

    def masked_url(self) -> str:
        url = self.database_url
        if "://" in url and "@" in url:
            scheme, rest = url.split("://", 1)
            creds, host = rest.rsplit("@", 1)
            user = creds.split(":", 1)[0]
            return f"{scheme}://{user}:***@{host}"
        return url

    def status_summary(self) -> dict[str, Any]:
        return {
            "database_url": self.masked_url(),
            "is_postgres": self.is_postgres,
            "alembic_revision": self.current_alembic_revision(),
            "extensions": self.extensions_status(),
            "pool": self.pool_metrics(),
            "schema": describe_schema(),
            "table_counts": self.table_counts(),
        }

    def dispose(self) -> None:
        self.engine.dispose()


_MANAGER_LOCK = threading.Lock()
_DEFAULT_MANAGER: DatabasePoolManager | None = None


def get_db_manager(database_url: str | None = None) -> DatabasePoolManager:
    global _DEFAULT_MANAGER
    if database_url is not None:
        return DatabasePoolManager(database_url)
    with _MANAGER_LOCK:
        if _DEFAULT_MANAGER is None:
            _DEFAULT_MANAGER = DatabasePoolManager()
        return _DEFAULT_MANAGER
