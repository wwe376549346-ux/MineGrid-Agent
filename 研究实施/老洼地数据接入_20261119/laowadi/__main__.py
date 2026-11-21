"""Local entry points for importing and using Laowadi hourly reports."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys

from laowadi.agent import MAX_CALLS
from laowadi.corpus import build_corpus_ledger, render_corpus_markdown
from laowadi.daily import markdown, observe
from laowadi.diagnose import MAX_ROUNDS, MAX_TOOL_CALLS, diagnose_question, render_diagnosis_markdown
from laowadi.evaluation import evaluate_all_layers, render_evaluation_markdown
from laowadi.health import evaluate_health, render_health_markdown
from laowadi.llm import ChatClient
from laowadi.mcp_server import serve_stdio
from laowadi.model import load_project_model_config, select_with_model
from laowadi.monitor import EVENT_DB, run_monitor, show_event
from laowadi.queries import Archive, render
from laowadi.questions import QuestionSession
from laowadi.storage import DB, connect, import_reports
from laowadi.supplement import apply_supplement, build_baseline_verification, render_supplement_markdown
from laowadi.workbench import SOURCE_DIR, serve


def output(data):
    print(json.dumps(data, ensure_ascii=False, allow_nan=False), flush=True)


def ask(archive, question, json_output, use_model, max_calls):
    session = QuestionSession(archive, selector=select_with_model if use_model else None, max_tool_calls=max_calls)
    if question is None and sys.stdin.isatty():
        print("输入历史查数、供电关系或监测事件问题。输入「退出」结束。", flush=True)
    for text in [question] if question is not None else sys.stdin:
        text = text.strip()
        if text in {"退出", "quit", "exit"}:
            break
        if text:
            answer = session.ask(text)
            if json_output:
                output(answer)
            else:
                print(render(answer), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    import_cmd = commands.add_parser("import", help="import 35 kV daily reports and diagram metadata")
    import_cmd.add_argument("--source", type=Path, required=True)
    ask_cmd = commands.add_parser("ask", help="ask Chinese history and topology questions")
    ask_cmd.add_argument("question", nargs="?")
    ask_cmd.add_argument("--json", action="store_true")
    ask_cmd.add_argument("--agent-model", action="store_true", help="use the configured model for multi-step tool selection")
    ask_cmd.add_argument("--model-config", type=Path, help="read a project model configuration file and enable model selection")
    ask_cmd.add_argument("--max-tool-calls", type=int, choices=range(1, MAX_CALLS + 1), default=MAX_CALLS,
                         help="limit calls per multi-step review (default: 4)")
    replay_cmd = commands.add_parser("replay", help="emit hourly history frames as NDJSON in source order")
    replay_cmd.add_argument("--date")
    monitor_cmd = commands.add_parser("monitor", help="replay hourly records, detect and archive events")
    monitor_cmd.add_argument("--date")
    monitor_cmd.add_argument("--threshold-kw", type=float, default=1000)
    monitor_cmd.add_argument("--simulate-missing", metavar="SOURCE_HOUR")
    monitor_cmd.add_argument("--seconds-per-hour", type=float, default=0)
    monitor_cmd.add_argument("--json", action="store_true")
    daily_cmd = commands.add_parser("daily", help="summarize the historical observations of four pilot cabinets")
    daily_cmd.add_argument("--date")
    daily_cmd.add_argument("--json", action="store_true")
    health_cmd = commands.add_parser("health", help="run full-station 25-cabinet 4-metric electrical health inspection")
    health_cmd.add_argument("--date")
    health_cmd.add_argument("--json", action="store_true")
    diag_cmd = commands.add_parser("diagnose", help="run hypothesis-driven electrical health diagnosis")
    diag_cmd.add_argument("question", help="Chinese electrical health or anomaly diagnosis question")
    diag_cmd.add_argument("--model-config", type=Path, help="use the configured DeepSeek model for live diagnosis")
    diag_cmd.add_argument("--agent-model", action="store_true", help="use COAL_API_* environment variables for live diagnosis")
    diag_cmd.add_argument("--max-rounds", type=int, default=MAX_ROUNDS)
    diag_cmd.add_argument("--max-tool-calls", type=int, default=MAX_TOOL_CALLS)
    diag_cmd.add_argument("--json", action="store_true")
    corpus_cmd = commands.add_parser("corpus", help="inspect existing materials ledger and 9-question answerability boundary")
    corpus_cmd.add_argument("--json", action="store_true")
    eval_cmd = commands.add_parser("evaluate", help="generate 5-layer BFCL + GAIA evaluation report")
    eval_cmd.add_argument("--json", action="store_true")
    supp_cmd = commands.add_parser("supplement", help="run versioned professional verification and selective supplement recalculation")
    supp_cmd.add_argument("--packet", help="optional JSON string or path to supplement packet")
    supp_cmd.add_argument("--json", action="store_true")
    mcp_cmd = commands.add_parser("mcp", help="run read-only MCP JSON-RPC 2.0 server over stdio")
    workbench_cmd = commands.add_parser("workbench", help="serve the local Laowadi workbench (FastAPI multi-worker + external worker pool)")
    workbench_cmd.add_argument("--port", type=int, default=8765)
    workbench_cmd.add_argument("--source", type=Path, default=SOURCE_DIR)
    workbench_cmd.add_argument("--model-config", type=Path)
    workbench_cmd.add_argument("--workers", type=int, default=2, help="number of Uvicorn ASGI web worker OS processes (default: 2)")
    workbench_cmd.add_argument("--external-workers", type=int, default=2, help="number of decoupled external job worker OS processes (default: 2; 0 to run external workers separately)")
    worker_cmd = commands.add_parser("worker", help="run a decoupled out-of-process external job worker consuming the durable task queue")
    worker_cmd.add_argument("--source", type=Path, default=Path(os.environ.get("LAOWADI_SOURCE_DIR", SOURCE_DIR)))
    worker_cmd.add_argument("--model-config", type=Path)
    worker_cmd.add_argument("--jobs-db", type=Path, default=None)
    worker_cmd.add_argument("--worker-id", default=None)
    worker_cmd.add_argument("--lease-seconds", type=float, default=20.0)
    worker_cmd.add_argument("--poll-interval", type=float, default=0.15)
    db_cmd = commands.add_parser("db", help="manage Alembic migrations, connection pool & PostgreSQL/TimescaleDB/pgvector ETL")
    db_cmd.add_argument("action", choices=["upgrade", "downgrade", "migrate-archive", "status", "ddl"])
    db_cmd.add_argument("--database-url", default=None, help="target SQLAlchemy URL (defaults to LAOWADI_DATABASE_URL or local/laowadi_enterprise.sqlite3)")
    db_cmd.add_argument("--source-dir", type=Path, default=os.environ.get("LAOWADI_SOURCE_DIR"))
    db_cmd.add_argument("--allow-corrections", action="store_true", help="publish an explicitly reviewed corrected archive")
    db_cmd.add_argument("--revision", default=None, help="Alembic target revision (default: head for upgrade, base for downgrade)")
    ops_cmd = commands.add_parser("ops", help="enterprise operations: hot backup, cryptographic verify, restore & DR drill")
    ops_cmd.add_argument("action", choices=["backup", "verify-backup", "restore", "dr-drill", "list-backups"])
    ops_cmd.add_argument("--output-dir", type=Path, default=None, help="backup output directory (default: local/backups)")
    ops_cmd.add_argument("--bundle", type=Path, default=None, help="backup .tar.gz path for verify-backup or restore")
    ops_cmd.add_argument("--target-dir", type=Path, default=None, help="restore destination directory")
    ops_cmd.add_argument("--target-pg-url", default=None, help="optional PostgreSQL URL for pg_restore")
    ops_cmd.add_argument("--database-url", default=None, help="source PostgreSQL/SQLAlchemy URL for backup/dr-drill")
    ops_cmd.add_argument("--retention-days", type=int, default=14)
    ops_cmd.add_argument("--dry-run", action="store_true")
    tool_cmd = commands.add_parser("tool", help="call a structured read-only query")
    tool_cmd.add_argument("name", choices=["get_hourly_reading", "get_daily_statistic", "get_supply_link", "get_monitor_event"])
    tool_cmd.add_argument("--args", required=True)
    for cmd in (monitor_cmd, daily_cmd, health_cmd, diag_cmd, corpus_cmd, eval_cmd, supp_cmd, mcp_cmd, ask_cmd, tool_cmd, workbench_cmd, worker_cmd, db_cmd):
        cmd.add_argument("--events-db", type=Path, default=EVENT_DB)
    for cmd in (ask_cmd, daily_cmd, tool_cmd):
        cmd.add_argument("--run", help="query one particular historical or simulated replay")
    for cmd in (import_cmd, ask_cmd, replay_cmd, tool_cmd, monitor_cmd, daily_cmd, health_cmd, diag_cmd, corpus_cmd, eval_cmd, supp_cmd, mcp_cmd, workbench_cmd, worker_cmd, db_cmd):
        cmd.add_argument("--db", type=Path, default=DB)
    args = parser.parse_args()
    try:
        if args.command == "import":
            output(import_reports(args.source, args.db))
        elif args.command == "workbench":
            serve(
                args.port,
                args.db,
                args.events_db,
                args.source,
                args.model_config,
                workers=args.workers,
                external_workers=args.external_workers,
            )
        elif args.command == "worker":
            from laowadi.worker import run_worker_process

            run_worker_process(
                db_path=args.db,
                events_path=args.events_db,
                source_dir=args.source,
                model_config=args.model_config,
                jobs_db=args.jobs_db or (Path(os.environ["LAOWADI_JOBS_DB"]) if os.environ.get("LAOWADI_JOBS_DB") else None),
                worker_id=args.worker_id,
                lease_seconds=args.lease_seconds,
                poll_interval=args.poll_interval,
            )
        elif args.command == "mcp":
            serve_stdio(args.db, args.events_db)
        elif args.command == "db":
            from laowadi.db.engine import DatabasePoolManager
            from laowadi.db.migrator import (
                generate_postgres_ddl_sql,
                migrate_sqlite_archives_to_enterprise,
                run_alembic_downgrade,
                run_alembic_upgrade,
            )

            if args.action == "ddl":
                print(generate_postgres_ddl_sql(), end="")
            elif args.action == "upgrade":
                output(run_alembic_upgrade(args.database_url, args.revision or "head"))
            elif args.action == "downgrade":
                output(run_alembic_downgrade(args.database_url, args.revision or "base"))
            elif args.action == "status":
                mgr = DatabasePoolManager(args.database_url)
                try:
                    output(mgr.status_summary())
                finally:
                    mgr.dispose()
            elif args.action == "migrate-archive":
                mgr = DatabasePoolManager(args.database_url)
                try:
                    output(migrate_sqlite_archives_to_enterprise(
                        mgr, source_db=args.db, events_db=args.events_db, allow_corrections=args.allow_corrections,
                        source_dir=args.source_dir,
                    ))
                finally:
                    mgr.dispose()
        elif args.command == "ops":
            from laowadi.backup import (
                BACKUP_DIR,
                create_backup,
                list_backups,
                restore_backup,
                run_dr_drill,
                verify_backup,
            )

            out_dir = args.output_dir or BACKUP_DIR
            if args.action == "backup":
                output(create_backup(output_dir=out_dir, database_url=args.database_url, retention_days=args.retention_days))
            elif args.action == "list-backups":
                output(list_backups(backup_dir=out_dir))
            elif args.action == "verify-backup":
                if not args.bundle:
                    raise ValueError("verify-backup 需要 --bundle 路径")
                res = verify_backup(args.bundle)
                output(res)
                if not res.get("valid"):
                    return 1
            elif args.action == "restore":
                if not args.bundle or not args.target_dir:
                    raise ValueError("restore 需要 --bundle 和 --target-dir 参数")
                output(
                    restore_backup(
                        args.bundle,
                        target_dir=args.target_dir,
                        target_pg_url=args.target_pg_url,
                        dry_run=args.dry_run,
                    )
                )
            elif args.action == "dr-drill":
                output(run_dr_drill(output_dir=out_dir, database_url=args.database_url, target_pg_url=args.target_pg_url))
        else:
            with closing(connect(args.db)) as db:
                archive = Archive(db, getattr(args, "events_db", EVENT_DB), getattr(args, "run", None))
                if args.command == "ask":
                    if args.model_config:
                        os.environ.update(load_project_model_config(args.model_config))
                    ask(archive, args.question, args.json, args.agent_model or bool(args.model_config), args.max_tool_calls)
                elif args.command == "tool":
                    output(archive.call(args.name, json.loads(args.args)))
                elif args.command == "daily":
                    db.execute("BEGIN")
                    report = observe(archive, args.date)
                    if args.json:
                        output(report)
                    else:
                        print(markdown(report), end="")
                elif args.command == "health":
                    db.execute("BEGIN")
                    report = evaluate_health(archive, args.date)
                    if args.json:
                        output(report)
                    else:
                        print(render_health_markdown(report), end="")
                elif args.command == "diagnose":
                    client = None
                    if args.model_config:
                        client = ChatClient.from_config_file(args.model_config)
                    elif args.agent_model:
                        client = ChatClient.from_env()
                    result = diagnose_question(
                        archive,
                        args.question,
                        client=client,
                        max_rounds=args.max_rounds,
                        max_tool_calls=args.max_tool_calls,
                    )
                    if args.json:
                        output(result)
                    else:
                        print(render_diagnosis_markdown(result), end="")
                elif args.command == "corpus":
                    db.execute("BEGIN")
                    ledger = build_corpus_ledger(archive)
                    if args.json:
                        output(ledger)
                    else:
                        print(render_corpus_markdown(ledger), end="")
                elif args.command == "evaluate":
                    db.execute("BEGIN")
                    ev = evaluate_all_layers(archive)
                    if args.json:
                        output(ev)
                    else:
                        print(render_evaluation_markdown(ev), end="")
                elif args.command == "supplement":
                    db.execute("BEGIN")
                    v1 = build_baseline_verification(archive)
                    v2 = None
                    if args.packet:
                        p = Path(args.packet)
                        pkt = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else json.loads(args.packet)
                        v2 = apply_supplement(v1, pkt)
                    if args.json:
                        output({"baseline_v1": v1, "supplemented_v2": v2})
                    else:
                        print(render_supplement_markdown(v1, v2), end="")
                elif args.command == "monitor":
                    def notify(event):
                        if args.json:
                            output({"type": "monitor_notice", "event": event})
                        else:
                            print(f"[主动提示] {show_event(event)}", flush=True)

                    output(run_monitor(archive, args.events_db, day=args.date, threshold_kw=args.threshold_kw,
                                       skip_hour=args.simulate_missing, seconds_per_hour=args.seconds_per_hour, notify=notify))
                else:
                    for frame in archive.frames(args.date):
                        output(frame)
    except BrokenPipeError:
        return 0
    except (ValueError, TypeError, OSError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
