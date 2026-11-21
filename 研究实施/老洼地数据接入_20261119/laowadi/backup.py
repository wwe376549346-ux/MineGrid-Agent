"""Enterprise Backup, Cryptographic Verification, Restore & Automated Disaster Recovery (DR) Drill.

Capabilities:
- Hot consistent SQLite snapshots via `sqlite3.Connection.backup()` (safe under active WAL writers).
- Live PostgreSQL / TimescaleDB logical custom-format dumps via `pg_dump --format=custom`
  (uses the configured PostgreSQL client directory or locally installed client).
- SHA-256 manifest (`manifest.json` + `manifest.sha256`) + `.tar.gz` immutable bundle + retention pruning.
- Pre-restore tamper detection & SQLite `PRAGMA integrity_check` + `pg_restore --list` verification.
- End-to-end automated DR drill (`run_dr_drill`) measuring exact RTO (seconds) in an isolated sandbox.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

from psycopg import sql

from laowadi.audit import AuditLedger
from laowadi.db.engine import DatabasePoolManager, default_database_url
from laowadi.db.postgres import ArchiveConnection, pg_connect, postgres_mode
from laowadi.source_files import document_manifest, verified_original
from laowadi.standards import REGULATION_TXT
from laowadi.logging_config import get_logger, log_event
from laowadi.storage import ROOT


BACKUP_DIR = Path(os.environ.get("LAOWADI_BACKUP_DIR", ROOT / "local" / "backups"))
LOGGER = get_logger("backup")

SQLITE_BACKUP_TARGETS: list[tuple[str, Path]] = [
    ("laowadi.sqlite3", ROOT / "local" / "laowadi.sqlite3"),
    ("laowadi_events.sqlite3", ROOT / "local" / "laowadi_events.sqlite3"),
    ("laowadi_enterprise.sqlite3", ROOT / "local" / "laowadi_enterprise.sqlite3"),
    ("laowadi_reviews.sqlite3", ROOT / "local" / "laowadi_reviews.sqlite3"),
    ("laowadi_sessions.sqlite3", ROOT / "local" / "laowadi_sessions.sqlite3"),
    ("laowadi_audit.sqlite3", ROOT / "local" / "laowadi_audit.sqlite3"),
    ("laowadi_jobs.sqlite3", ROOT / "local" / "laowadi_jobs.sqlite3"),
    ("laowadi_checkpoints.sqlite3", ROOT / "local" / "laowadi_checkpoints.sqlite3"),
    ("dag_checkpoints.sqlite3", ROOT / "local" / "dag_checkpoints.sqlite3"),
    ("laowadi_proactive.sqlite3", ROOT / "local" / "laowadi_proactive.sqlite3"),
]

CONFIG_BACKUP_TARGETS: list[tuple[str, Path]] = [
    ("mine_profile.json", ROOT / "laowadi" / "mine_profile.json"),
    ("topology.json", ROOT / "laowadi" / "topology.json"),
    ("alembic.ini", ROOT / "alembic.ini"),
]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_pg_tool(tool_name: str) -> str | None:
    if directory := os.environ.get("LAOWADI_PG_TOOL_DIR"):
        candidate = Path(directory) / tool_name
        if candidate.is_file():
            return str(candidate)
        raise ValueError(f"Configured PostgreSQL client tool not found: {tool_name}")
    found = shutil.which(tool_name)
    if found:
        return found
    pg_base = Path("/Library/PostgreSQL")
    if pg_base.is_dir():
        for d in sorted(pg_base.iterdir(), reverse=True):
            cand = d / "bin" / tool_name
            if cand.is_file():
                return str(cand)
    return None


def _parse_pg_url(url: str) -> dict[str, Any]:
    clean = url.replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")
    u = urlparse(clean)
    return {
        "host": u.hostname or "127.0.0.1",
        "port": str(u.port or 5432),
        "user": unquote(u.username or "postgres"),
        "password": unquote(u.password or ""),
        "dbname": unquote((u.path or "/laowadi").lstrip("/") or "laowadi"),
    }


def _hot_backup_sqlite(src: Path, dst: Path) -> dict[str, Any]:
    """Use SQLite Online Backup API for transactionally consistent hot snapshots under active WAL."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=15.0)
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
        integrity = dst_conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst_conn.close()
        src_conn.close()
    if str(integrity).lower() != "ok":
        raise RuntimeError(f"SQLite backup integrity check failed for {src.name}: {integrity}")
    return {
        "name": src.name,
        "kind": "sqlite_hot_backup",
        "size_bytes": dst.stat().st_size,
        "sha256": _sha256_file(dst),
        "integrity_check": "ok",
    }


def _pg_client(tool, database_url, *arguments, timeout=180):
    executable = _find_pg_tool(tool)
    if not executable:
        raise RuntimeError(f"PostgreSQL client tool missing: {tool}")
    from psycopg.conninfo import conninfo_to_dict

    dsn = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    connection = conninfo_to_dict(dsn)
    options = {
        "host": "PGHOST", "hostaddr": "PGHOSTADDR", "port": "PGPORT", "user": "PGUSER",
        "dbname": "PGDATABASE", "password": "PGPASSWORD", "sslmode": "PGSSLMODE",
        "sslrootcert": "PGSSLROOTCERT", "sslcert": "PGSSLCERT", "sslkey": "PGSSLKEY",
    }
    if unsupported := connection.keys() - options.keys():
        raise ValueError(f"Unsupported DSN settings for backup tools: {sorted(unsupported)}")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    environment.update({options[key]: value for key, value in connection.items()})
    environment["PGCONNECT_TIMEOUT"] = "5"
    proc = subprocess.run([executable, *arguments], env=environment, capture_output=True, text=True, timeout=timeout)
    if proc.returncode:
        raise RuntimeError(f"{tool} failed: {proc.stderr}")
    return proc


def _dump_postgres(database_url: str, dst: Path, *, snapshot=None) -> dict[str, Any]:
    info = _parse_pg_url(database_url)
    args = ["-Fc", "--no-owner", "--no-privileges", "-f", str(dst)]
    if snapshot:
        args.extend(("--snapshot", snapshot))
    _pg_client("pg_dump", database_url, *args)
    return {
        "name": dst.name, "kind": "postgres_custom_dump", "method": "pg_dump",
        "host": info["host"], "port": info["port"], "dbname": info["dbname"],
        "size_bytes": dst.stat().st_size, "sha256": _sha256_file(dst),
    }


# Counts include the objects needed to continue a shift after replacement of a node.
_RECOVERY_TABLES = (
    "public.report_manifest", "public.diagram_manifest", "public.vector_document",
    "public.telemetry_reading", "public.daily_footer_summary", "public.monitor_event",
    "laowadi_archive.monitor_run", "laowadi_runtime.job", "laowadi_runtime.job_event",
    "laowadi_runtime.circuit_breaker_state", "laowadi_runtime.incident_ticket",
    "laowadi_runtime.incident_ticket_log", "laowadi_runtime.alert_outbox", "laowadi_runtime.watcher_state",
    "laowadi_runtime.dag_checkpoint", "laowadi_runtime.graph_checkpoint",
    "laowadi_runtime.chat_session", "laowadi_runtime.chat_message",
    "laowadi_runtime.operator_review", "laowadi_runtime.operator_review_history",
    "laowadi_runtime.audit_event",
)


def _recovery_state(conn):
    version = conn.execute("SELECT version FROM laowadi_archive.publication").fetchone()
    if not version:
        raise ValueError("缺少已发布归档，无法生成生产恢复点。")
    counts = {}
    for table in _RECOVERY_TABLES:
        query = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(*table.split(".")))
        counts[table] = conn.execute(query).fetchone()[0]
    return {"archive_version": version[0], "row_counts": counts}


def _snapshot_bundle(url, stage, destination, source_dir):
    if source_dir is None:
        raise ValueError("生产备份须指定 LAOWADI_SOURCE_DIR。")
    with pg_connect(url, schema="laowadi_archive") as conn:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
        recovery = _recovery_state(conn)
        snapshot_time = conn.execute("SELECT transaction_timestamp()").fetchone()[0]
        recovery["snapshot_utc"] = snapshot_time.astimezone(timezone.utc).isoformat()
        recovery["extensions"] = dict(tuple(row) for row in conn.execute(
            "SELECT extname, extversion FROM pg_extension WHERE extname IN ('timescaledb', 'vector')"
        ))
        sources = []
        for item in document_manifest(ArchiveConnection(conn)):
            name, digest = item["filename"], item["sha256"]
            relpath = f"sources/{digest}/{name}"
            target = stage / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(verified_original(source_dir, name, digest))
            sources.append({
                "kind": "source_original", "relpath": relpath, "name": name,
                "sha256": digest, "size_bytes": target.stat().st_size,
            })
        return _dump_postgres(url, destination, snapshot=snapshot), recovery, sources


def _restore_postgres(url, archive, recovery):
    if not recovery:
        recovery = {}
    with pg_connect(url) as conn:
        # An empty target is required. There is no destructive --clean operation.
        occupied = conn.execute("""
            SELECT EXISTS (
                SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname IN ('public','laowadi_archive','laowadi_runtime')
                AND c.relkind IN ('r','p','m','v','f')
                AND NOT EXISTS (
                    SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e'
                )
            )
        """).fetchone()[0]
        if occupied:
            raise ValueError("恢复目标必须是新建的空数据库，已有业务表的实例被拒绝。")
        extensions = recovery.get("extensions", {})
        for extension in ("timescaledb", "vector"):
            if extension not in extensions:
                continue
            version = extensions[extension]
            conn.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS {} VERSION {}").format(
                sql.Identifier(extension), sql.Literal(version),
            ))
            installed = conn.execute("SELECT extversion FROM pg_extension WHERE extname=%s", (extension,)).fetchone()[0]
            if installed != version:
                raise ValueError(f"目标 {extension} 扩展版本与恢复点不一致。")
        if "timescaledb" in extensions:
            conn.execute("SELECT public.timescaledb_pre_restore()")
        try:
            _pg_client("pg_restore", url, "--exit-on-error", "--single-transaction", "--no-owner", "--no-privileges", "-d",
                       _parse_pg_url(url)["dbname"], str(archive))
        finally:
            if "timescaledb" in extensions:
                conn.execute("SELECT public.timescaledb_post_restore()")
        if recovery:
            restored = _recovery_state(conn)
            if restored["archive_version"] != recovery["archive_version"] or any(
                restored["row_counts"].get(table) != count for table, count in recovery["row_counts"].items()
            ):
                raise RuntimeError("恢复后的归档版本或业务行数与同一快照回执不一致。")
            with conn.transaction():
                conn.execute("DELETE FROM laowadi_runtime.worker_heartbeat")
                conn.execute("DELETE FROM laowadi_runtime.shared_rate_limit")
                conn.execute("UPDATE laowadi_runtime.job SET lease_expires_at=1 WHERE status='running'")
                conn.execute("UPDATE laowadi_runtime.dag_checkpoint SET lock_owner=NULL, lock_expires_at=0")
                conn.execute("UPDATE laowadi_runtime.circuit_breaker_state SET half_open_probe_inflight=0 WHERE state='half_open'")


def prune_old_backups(backup_dir: Path, *, retention_days: int = 14, keep_last: int = 10) -> list[str]:
    """Delete expired `.tar.gz` backups while always preserving at least `keep_last` recent archives."""
    if not backup_dir.is_dir():
        return []
    archives = sorted(backup_dir.glob("laowadi_backup_*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(retention_days)))
    removed: list[str] = []
    for idx, arch in enumerate(archives):
        if idx < max(1, int(keep_last)):
            continue
        mtime = datetime.fromtimestamp(arch.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            arch.unlink(missing_ok=True)
            sha_sidecar = arch.with_suffix(".gz.sha256")
            sha_sidecar.unlink(missing_ok=True)
            removed.append(arch.name)
    return removed


def create_backup(
    *,
    output_dir: Path = BACKUP_DIR,
    database_url: str | None = None,
    retention_days: int = 14,
    keep_last: int = 10,
    include_postgres: bool = True,
    source_dir: Path | None = None,
) -> dict[str, Any]:
    """Create a hot backup with SHA-256 checksums for the bundle and its artifacts."""
    t0 = time.monotonic()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    ts_slug = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bundle_id = f"laowadi_backup_{ts_slug}_{uuid4().hex[:12]}"
    bundle_tar = output_dir / f"{bundle_id}.tar.gz"

    eff_url = database_url or default_database_url()
    production = postgres_mode()
    if production and (not eff_url.startswith("postgresql") or not include_postgres):
        raise ValueError("PostgreSQL 生产模式必须保留包含全部运行状态的一致性备份。")
    source_dir = source_dir or os.environ.get("LAOWADI_SOURCE_DIR")
    recovery = None
    artifacts: list[dict[str, Any]] = []
    pg_info: dict[str, Any] | None = None

    with tempfile.TemporaryDirectory(prefix="laowadi_bak_") as td:
        stage = Path(td) / bundle_id
        (stage / "sqlite").mkdir(parents=True, exist_ok=True)
        (stage / "config").mkdir(parents=True, exist_ok=True)
        (stage / "postgres").mkdir(parents=True, exist_ok=True)

        # 1. Hot consistent SQLite backups
        for name, src_path in (() if production else SQLITE_BACKUP_TARGETS):
            if src_path.is_file():
                meta = _hot_backup_sqlite(src_path, stage / "sqlite" / name)
                meta["relpath"] = f"sqlite/{name}"
                artifacts.append(meta)

        # 2. Config & topology metadata (no secrets!)
        for name, cfg_path in CONFIG_BACKUP_TARGETS:
            if cfg_path.is_file():
                dst_cfg = stage / "config" / name
                shutil.copy2(cfg_path, dst_cfg)
                artifacts.append({
                    "name": name,
                    "relpath": f"config/{name}",
                    "kind": "config",
                    "size_bytes": dst_cfg.stat().st_size,
                    "sha256": _sha256_file(dst_cfg),
                })
        if production and REGULATION_TXT.is_file():
            relative = f"standards/核验记录/{REGULATION_TXT.name}"
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REGULATION_TXT, target)
            artifacts.append({
                "name": target.name, "relpath": relative, "kind": "standard_reference",
                "size_bytes": target.stat().st_size, "sha256": _sha256_file(target),
            })

        # 3. Live PostgreSQL / TimescaleDB dump when configured
        if include_postgres and eff_url.startswith("postgresql"):
            pg_dst = stage / "postgres" / "laowadi_pg.dump"
            if production:
                pg_info, recovery, sources = _snapshot_bundle(eff_url, stage, pg_dst, source_dir)
                artifacts.extend(sources)
            else:
                pg_info = _dump_postgres(eff_url, pg_dst)
            pg_info["relpath"] = "postgres/laowadi_pg.dump"
            artifacts.append(pg_info)

        manifest = {
            "bundle_id": bundle_id,
            "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "database_backend": "postgresql" if eff_url.startswith("postgresql") else "sqlite",
            "postgres_dump_included": pg_info is not None,
            "artifact_count": len(artifacts),
            "recovery_point": recovery,
            "artifacts": artifacts,
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        sha_lines = [f"{a['sha256']}  {a['relpath']}" for a in artifacts]
        (stage / "manifest.sha256").write_text("\n".join(sha_lines) + "\n", encoding="utf-8")

        with tempfile.TemporaryDirectory(prefix=".pending-backup-", dir=output_dir) as pending:
            pending_tar = Path(pending) / bundle_tar.name
            with tarfile.open(pending_tar, "w:gz") as tar:
                tar.add(stage, arcname=bundle_id)
            bundle_sha = _sha256_file(pending_tar)
            sidecar = pending_tar.with_name(f"{pending_tar.name}.sha256")
            sidecar.write_text(f"{bundle_sha}  {bundle_tar.name}\n", encoding="utf-8")
            os.replace(sidecar, output_dir / sidecar.name)
            os.replace(pending_tar, bundle_tar)
    pruned = prune_old_backups(output_dir, retention_days=retention_days, keep_last=keep_last)
    elapsed = round(time.monotonic() - t0, 3)

    result = {
        "status": "ok",
        "bundle_id": bundle_id,
        "bundle_path": str(bundle_tar),
        "bundle_size_bytes": bundle_tar.stat().st_size,
        "bundle_sha256": bundle_sha,
        "artifact_count": len(artifacts),
        "recovery_point": recovery,
        "postgres_dump_included": pg_info is not None,
        "pruned_expired": pruned,
        "elapsed_seconds": elapsed,
        "artifacts": artifacts,
    }
    log_event(
        LOGGER,
        logging.INFO,
        "backup_created",
        f"Enterprise backup bundle created: {bundle_id}",
        bundle_id=bundle_id,
        size_bytes=result["bundle_size_bytes"],
        artifact_count=len(artifacts),
        postgres_dump=pg_info is not None,
        elapsed_seconds=elapsed,
    )
    return result


def verify_backup(bundle_path: Path | str) -> dict[str, Any]:
    """Verify archive SHA-256 sidecar, every member's SHA-256, SQLite integrity, and PG dump readability."""
    t0 = time.monotonic()
    tar_path = Path(bundle_path)
    if not tar_path.is_file():
        return {"valid": False, "reason": f"Backup file not found: {tar_path}"}

    sidecar = tar_path.parent / f"{tar_path.name}.sha256"
    actual_tar_sha = _sha256_file(tar_path)
    if sidecar.is_file():
        expected_tar_sha = sidecar.read_text(encoding="utf-8").strip().split()[0]
        if expected_tar_sha != actual_tar_sha:
            return {
                "valid": False,
                "reason": f"Bundle SHA-256 mismatch (expected {expected_tar_sha[:16]}, actual {actual_tar_sha[:16]})",
            }

    checked: list[str] = []
    sqlite_ok = 0
    pg_toc_lines = 0
    with tempfile.TemporaryDirectory(prefix="laowadi_verify_") as td:
        extract_root = Path(td)
        with tarfile.open(tar_path, "r:gz") as tar:
            tar.extractall(extract_root, filter="data")

        manifests = list(extract_root.glob("*/manifest.json"))
        if not manifests:
            return {"valid": False, "reason": "manifest.json missing from backup archive"}
        bundle_root = manifests[0].parent.resolve()
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))

        for art in manifest.get("artifacts", []):
            rel = art["relpath"]
            fpath = bundle_root / rel
            if not fpath.resolve().is_relative_to(bundle_root) or not fpath.is_file():
                return {"valid": False, "reason": f"Missing artifact in archive: {rel}"}
            actual_sha = _sha256_file(fpath)
            if actual_sha != art["sha256"]:
                return {"valid": False, "reason": f"Artifact tampered or corrupted: {rel}"}
            if art.get("kind") == "sqlite_hot_backup":
                conn = sqlite3.connect(f"file:{fpath}?mode=ro", uri=True)
                try:
                    chk = conn.execute("PRAGMA integrity_check").fetchone()[0]
                finally:
                    conn.close()
                if str(chk).lower() != "ok":
                    return {"valid": False, "reason": f"SQLite integrity_check failed for {rel}: {chk}"}
                sqlite_ok += 1
            elif art.get("kind") == "postgres_custom_dump":
                pg_restore = _find_pg_tool("pg_restore")
                if pg_restore:
                    proc = subprocess.run([pg_restore, "--list", str(fpath)], capture_output=True, text=True, timeout=30)
                    if proc.returncode != 0:
                        return {"valid": False, "reason": f"pg_restore --list failed: {proc.stderr}"}
                    pg_toc_lines = len([ln for ln in proc.stdout.splitlines() if ln.strip() and not ln.startswith(";")])
            checked.append(rel)

    return {
        "valid": True,
        "bundle_id": manifest.get("bundle_id"),
        "bundle_sha256": actual_tar_sha,
        "verified_artifacts": len(checked),
        "sqlite_integrity_ok": sqlite_ok,
        "postgres_toc_entries": pg_toc_lines,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }


def restore_backup(
    bundle_path: Path | str,
    *,
    target_dir: Path,
    target_pg_url: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Verify and atomically restore SQLite databases (and optional PostgreSQL dump) into target_dir."""
    t0 = time.monotonic()
    ver = verify_backup(bundle_path)
    if not ver.get("valid"):
        raise ValueError(f"Refusing to restore invalid/tampered backup: {ver.get('reason')}")

    tar_path = Path(bundle_path)
    target_dir = Path(target_dir)
    restored_files: list[str] = []
    pg_restored = False

    with tempfile.TemporaryDirectory(prefix="laowadi_restore_") as td:
        extract_root = Path(td)
        with tarfile.open(tar_path, "r:gz") as tar:
            tar.extractall(extract_root, filter="data")
        bundle_root = next(extract_root.glob("*/manifest.json")).parent
        manifest = json.loads((bundle_root / "manifest.json").read_text(encoding="utf-8"))

        if not dry_run:
            if target_dir.exists() and any(target_dir.iterdir()):
                raise ValueError("恢复目录必须为空，已有文件的目标目录被拒绝。")
            recovery = manifest.get("recovery_point")
            if recovery and not target_pg_url:
                raise ValueError("此备份要求一座新建的 PostgreSQL 恢复目标。")
            target_dir.mkdir(parents=True, exist_ok=True)
            for art in manifest.get("artifacts", []):
                src = bundle_root / art["relpath"]
                kind = art.get("kind")
                if kind == "sqlite_hot_backup":
                    dst = target_dir / Path(art["name"]).name
                    _hot_backup_sqlite(src, dst)
                    restored_files.append(dst.name)
                elif kind in ("config", "source_original", "standard_reference"):
                    relative = src.resolve().relative_to(bundle_root.resolve())
                    dst = target_dir / relative
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    restored_files.append(str(relative))
                elif kind == "postgres_custom_dump" and target_pg_url:
                    _restore_postgres(target_pg_url, src, recovery)
                    pg_restored = True

    elapsed = round(time.monotonic() - t0, 3)
    log_event(
        LOGGER,
        logging.INFO,
        "backup_restored",
        f"Restore completed (dry_run={dry_run})",
        bundle_id=ver.get("bundle_id"),
        target_dir=str(target_dir),
        restored_count=len(restored_files),
        pg_restored=pg_restored,
        elapsed_seconds=elapsed,
    )
    return {
        "status": "ok",
        "dry_run": dry_run,
        "bundle_id": ver.get("bundle_id"),
        "target_dir": str(target_dir),
        "restored_files": restored_files,
        "postgres_restored": pg_restored,
        "elapsed_seconds": elapsed,
    }


def run_dr_drill(
    *,
    output_dir: Path = BACKUP_DIR,
    database_url: str | None = None,
    target_pg_url: str | None = None,
) -> dict[str, Any]:
    """Execute a full isolated Disaster Recovery drill: backup -> verify -> sandbox restore -> row & audit check -> RTO."""
    t0 = time.monotonic()
    if postgres_mode() and not target_pg_url:
        raise ValueError("生产灾备演练须经命令行提供独立空库 --target-pg-url。")
    bak = create_backup(output_dir=output_dir, database_url=database_url)
    ver = verify_backup(bak["bundle_path"])
    assert ver["valid"], f"DR drill backup verification failed: {ver}"

    t_restore = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="laowadi_dr_sandbox_") as sandbox:
        sb = Path(sandbox)
        rest = restore_backup(bak["bundle_path"], target_dir=sb, target_pg_url=target_pg_url, dry_run=False)
        checks: dict[str, Any] = {}
        if rest["postgres_restored"] and bak["recovery_point"]:
            checks["postgres_snapshot_reconciled"] = True
            checks["recovery_point"] = bak["recovery_point"]

        # Verify restored primary archive row counts
        if (sb / "laowadi.sqlite3").is_file():
            conn = sqlite3.connect(str(sb / "laowadi.sqlite3"))
            try:
                checks["archive_readings"] = int(conn.execute("SELECT COUNT(*) FROM reading").fetchone()[0])
                checks["archive_summaries"] = int(conn.execute("SELECT COUNT(*) FROM summary").fetchone()[0])
            finally:
                conn.close()

        # Verify restored enterprise DB via SQLAlchemy pool
        if (sb / "laowadi_enterprise.sqlite3").is_file():
            mgr = DatabasePoolManager(f"sqlite+pysqlite:///{sb / 'laowadi_enterprise.sqlite3'}")
            try:
                st = mgr.status_summary()
                checks["enterprise_alembic_rev"] = st["alembic_revision"]
                checks["enterprise_telemetry_rows"] = st["table_counts"].get("telemetry_reading", 0)
                checks["enterprise_vector_docs"] = st["table_counts"].get("vector_document", 0)
            finally:
                mgr.dispose()

        # Verify restored SHA-256 hash-chained audit ledger
        if (sb / "laowadi_audit.sqlite3").is_file():
            chain = AuditLedger(sb / "laowadi_audit.sqlite3").verify_chain()
            checks["audit_chain_valid"] = chain["valid"]
            checks["audit_chain_entries"] = chain["total_entries"]

    rto_seconds = round(time.monotonic() - t_restore, 3)
    total_seconds = round(time.monotonic() - t0, 3)
    report = {
        "status": "passed",
        "bundle_id": bak["bundle_id"],
        "bundle_path": bak["bundle_path"],
        "bundle_sha256": bak["bundle_sha256"],
        "rto_seconds": rto_seconds,
        "total_drill_seconds": total_seconds,
        "verification": ver,
        "restore_summary": {
            "restored_count": len(rest["restored_files"]),
            "restored_files": rest["restored_files"],
        },
        "data_integrity_checks": checks,
    }
    log_event(
        LOGGER,
        logging.INFO,
        "dr_drill_completed",
        f"Automated DR drill passed (RTO={rto_seconds}s)",
        bundle_id=bak["bundle_id"],
        rto_seconds=rto_seconds,
        checks=checks,
    )
    return report


def list_backups(backup_dir: Path = BACKUP_DIR) -> dict[str, Any]:
    backup_dir = Path(backup_dir)
    items: list[dict[str, Any]] = []
    if backup_dir.is_dir():
        for arch in sorted(backup_dir.glob("laowadi_backup_*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
            sidecar = arch.parent / f"{arch.name}.sha256"
            sha = sidecar.read_text(encoding="utf-8").strip().split()[0] if sidecar.is_file() else ""
            items.append({
                "filename": arch.name,
                "path": str(arch),
                "size_bytes": arch.stat().st_size,
                "sha256": sha,
                "modified_utc": datetime.fromtimestamp(arch.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            })
    return {"backup_dir": str(backup_dir), "count": len(items), "backups": items}
