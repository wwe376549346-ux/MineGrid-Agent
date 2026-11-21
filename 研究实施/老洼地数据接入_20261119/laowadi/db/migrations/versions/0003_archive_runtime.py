"""Preserve archive lineage and expose PostgreSQL-backed operational read views."""

from alembic import op
import sqlalchemy as sa


revision = "0003_archive_runtime"
down_revision = "0002_timescaledb_and_pgvector"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("station", sa.Column("diagram", sa.Text(), nullable=False, server_default=""))
    op.add_column("cabinet", sa.Column("aliases", sa.Text(), nullable=False, server_default="[]"))
    op.add_column("daily_footer_summary", sa.Column("marker", sa.Text()))
    if op.get_context().dialect.name != "postgresql":
        return
    op.execute("CREATE SCHEMA laowadi_archive")
    op.execute("CREATE SCHEMA IF NOT EXISTS laowadi_runtime")
    op.execute("""
        CREATE TABLE laowadi_archive.publication (
            singleton integer PRIMARY KEY CHECK(singleton = 1),
            version text NOT NULL, published_at timestamptz NOT NULL,
            source_db_sha256 text NOT NULL, event_db_sha256 text NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE laowadi_archive.monitor_run (
            rowid bigint PRIMARY KEY, id text UNIQUE NOT NULL, mode text NOT NULL,
            description text NOT NULL, last_time text, frames_seen integer NOT NULL,
            complete integer NOT NULL
        )
    """)
    views = {
        "station": "SELECT code,name,diagram FROM public.station",
        "cabinet": "SELECT station,number,label,aliases FROM public.cabinet",
        "line": "SELECT from_station,from_cabinet,to_station,to_cabinet,description AS label FROM public.supply_line",
        "report": "SELECT day,filename,sheet,sha256 FROM public.report_manifest",
        "diagram": "SELECT filename,captured_at,sha256 FROM public.diagram_manifest",
        "reading": """SELECT day,to_char(ts,'YYYY-MM-DD HH24:MI:SS') AS time,station,cabinet,metric,
                           value,marker,source_row,source_col FROM public.telemetry_reading""",
        "summary": """SELECT day,station,cabinet,metric,statistic,value,marker,source_row,source_col
                       FROM public.daily_footer_summary""",
        "monitor_event": """SELECT CAST(substr(event_id,4) AS bigint) AS seq,run_id,event_type AS type,
                               to_char(at_ts,'YYYY-MM-DD HH24:MI:SS') AS at,cabinet,payload_json AS evidence
                            FROM public.monitor_event""",
    }
    for name, query in views.items():
        op.execute(f"CREATE VIEW laowadi_archive.{name} AS {query}")


def downgrade():
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP SCHEMA laowadi_archive CASCADE")
        # The runtime schema is managed independently of read-model revisions.
    op.drop_column("daily_footer_summary", "marker")
    op.drop_column("cabinet", "aliases")
    op.drop_column("station", "diagram")

