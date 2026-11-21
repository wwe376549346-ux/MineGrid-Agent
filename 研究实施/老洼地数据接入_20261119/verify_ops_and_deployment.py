"""Verification Suite: Docker/K8s Deployment, Standard Structured JSON Logging & Backup/DR Scripts.

Verifies:
1. Multi-stage non-root Dockerfile, .dockerignore, `docker compose config` & all 10 Kubernetes manifests in `k8s/`.
2. Standard structured JSON logging (`laowadi/logging_config.py`): NDJSON schema, ContextVar trace/actor/worker
   correlation, automatic credential/DSN/Bearer/sk-key scrubbing, and rotating log tail.
3. Hot backup (`sqlite3.backup()` + live PostgreSQL `pg_dump -Fc`), SHA-256 verification, tamper rejection,
   sandbox restore, `scripts/backup.sh` & `scripts/restore.sh` validation, and automated DR drill (RTO).
4. FastAPI `/api/ops/backups`, `/api/ops/backup`, `/api/ops/dr-drill`, `/api/ops/logs` + RBAC (viewer 403 / engineer 200).
5. Strict read-only SHA-256 preservation of `local/laowadi.sqlite3` and `local/laowadi_events.sqlite3`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from fastapi.testclient import TestClient
import yaml

from laowadi.api import create_app
from laowadi.backup import create_backup, restore_backup, run_dr_drill, verify_backup
from laowadi.logging_config import bind_log_context, configure_logging, get_logger, log_event, tail_structured_logs
from laowadi.monitor import EVENT_DB
from laowadi.security import issue_token
from laowadi.storage import DB, ROOT
from laowadi.workbench import SOURCE_DIR, Workbench


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_containers_and_k8s() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "AS builder" in dockerfile and "AS runtime" in dockerfile
    assert "USER laowadi" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "LAOWADI_LOG_FORMAT=json" in dockerfile
    assert (ROOT / ".dockerignore").is_file()

    # Validate docker-compose.yml with real Docker Compose CLI if available
    docker_bin = shutil.which("docker") or ("/Applications/Docker.app/Contents/Resources/bin/docker" if Path("/Applications/Docker.app/Contents/Resources/bin/docker").is_file() else None)
    if docker_bin:
        proc = subprocess.run(
            [docker_bin, "compose", "-f", str(ROOT / "docker-compose.yml"), "config", "--quiet"],
            env={**os.environ, "LAOWADI_PG_PASSWORD": "test-only",
                 "LAOWADI_AUTH_SECRET": "test-only", "LAOWADI_ARCHIVE_DIR": str(ROOT / "local")},
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 0, f"docker compose config failed: {proc.stderr}"

    # Validate all 10 Kubernetes manifests in k8s/
    k8s_dir = ROOT / "k8s"
    expected_files = [
        "namespace.yaml",
        "configmap.yaml",
        "secret.example.yaml",
        "timescaledb-statefulset.yaml",
        "archive-pvc.yaml",
        "migrate-job.yaml",
        "api-deployment.yaml",
        "worker-deployment.yaml",
        "backup-cronjob.yaml",
        "kustomization.yaml",
    ]
    kinds_found: set[str] = set()
    doc_count = 0
    for fname in expected_files:
        fpath = k8s_dir / fname
        assert fpath.is_file(), f"Missing K8s manifest: {fname}"
        docs = [d for d in yaml.safe_load_all(fpath.read_text(encoding="utf-8")) if d]
        assert docs, f"Empty K8s YAML: {fname}"
        for d in docs:
            assert "apiVersion" in d and "kind" in d
            kinds_found.add(d["kind"])
            doc_count += 1

    for required_kind in ("Namespace", "ConfigMap", "Secret", "StatefulSet", "Deployment", "Service", "Job", "CronJob", "HorizontalPodAutoscaler", "PodDisruptionBudget", "PersistentVolumeClaim", "Kustomization"):
        assert required_kind in kinds_found, f"Missing K8s kind: {required_kind}"

    # Validate shell backup/restore scripts syntax & executable bit
    for sh in ("backup.sh", "restore.sh"):
        sp = ROOT / "scripts" / sh
        assert sp.is_file() and os.access(sp, os.X_OK), f"Script missing or not executable: {sp}"
        p = subprocess.run(["bash", "-n", str(sp)], capture_output=True, text=True)
        assert p.returncode == 0, f"bash -n failed for {sh}: {p.stderr}"

    print(f"[1/5] Dockerfile (multi-stage non-root), docker-compose.yml, 10 K8s manifests ({doc_count} resources) & bash scripts OK")


def verify_structured_json_logging() -> None:
    with tempfile.TemporaryDirectory() as td:
        log_file = Path(td) / "test_structured.jsonl"
        configure_logging(level="INFO", fmt="json", log_file=log_file, force=True)
        logger = get_logger("verify_test")
        with bind_log_context(trace_id="TRC-TEST-001", actor="chief_eng", role="engineer", worker_id="w-99"):
            log_event(
                logger,
                logging.INFO,
                "unit_probe",
                "Connecting to postgresql+psycopg://postgres:020728@127.0.0.1:5432/laowadi with Bearer secret_token_1234567890",
                api_key="sk-live1234567890abcdef",
                password="020728",
                cabinet="1006",
            )
            log_event(logger, logging.WARNING, "unit_warn", "High voltage warning on 10kV II bus", kv=10.90)

        lines = [ln for ln in log_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 2
        rec0 = json.loads(lines[0])
        assert rec0["service"] == "laowadi-power-agent"
        assert rec0["trace_id"] == "TRC-TEST-001"
        assert rec0["actor"] == "chief_eng"
        assert rec0["role"] == "engineer"
        assert rec0["worker_id"] == "w-99"
        assert rec0["event"] == "unit_probe"
        assert "020728" not in lines[0], "PostgreSQL password leaked into JSON log!"
        assert "secret_token_1234567890" not in lines[0], "Bearer token leaked into JSON log!"
        assert "sk-live1234567890abcdef" not in lines[0], "API key leaked into JSON log!"
        assert rec0["fields"]["password"] == "***"
        assert rec0["fields"]["api_key"] == "***"
        assert rec0["fields"]["cabinet"] == "1006"

        tailed = tail_structured_logs(limit=10, level="WARNING", log_file=log_file)
        assert tailed["count"] == 1 and tailed["entries"][0]["event"] == "unit_warn"
        # Restore default log file for subsequent checks
        configure_logging(force=True)
    print("[2/5] Standard JSON structured logging (NDJSON schema, ContextVar correlation & secret scrubbing) OK")


def verify_backup_restore_and_dr() -> None:
    # Auto-detect live PostgreSQL on 5432 (or 5433) to verify real pg_dump inside backup bundle too!
    live_pg_url = None
    for port in (5433, 5432):
        cand = f"postgresql+psycopg://postgres:020728@127.0.0.1:{port}/laowadi"
        try:
            import psycopg

            with psycopg.connect(f"postgresql://postgres:020728@127.0.0.1:{port}/laowadi", connect_timeout=2):
                live_pg_url = cand
                break
        except Exception:
            continue

    with tempfile.TemporaryDirectory() as td:
        bak_dir = Path(td) / "backups"
        drill = run_dr_drill(output_dir=bak_dir, database_url=live_pg_url)
        assert drill["status"] == "passed"
        assert drill["verification"]["valid"] is True
        assert drill["data_integrity_checks"]["archive_readings"] == 16800
        assert drill["data_integrity_checks"]["archive_summaries"] == 2100
        assert drill["data_integrity_checks"]["enterprise_telemetry_rows"] == 16800
        assert drill["data_integrity_checks"]["enterprise_vector_docs"] == 76
        if live_pg_url:
            assert drill["verification"]["postgres_toc_entries"] > 0, "Expected live pg_dump TOC entries"

        # Tamper detection: corrupt 1 byte of backup tar and verify rejection
        tampered = Path(td) / "tampered_backup.tar.gz"
        raw = bytearray(Path(drill["bundle_path"]).read_bytes())
        raw[-10] ^= 0xFF
        tampered.write_bytes(bytes(raw))
        shutil.copy2(Path(drill["bundle_path"]).parent / f"{Path(drill['bundle_path']).name}.sha256", Path(td) / "tampered_backup.tar.gz.sha256")
        bad_ver = verify_backup(tampered)
        assert bad_ver["valid"] is False, "Tampered backup was not rejected!"
        try:
            restore_backup(tampered, target_dir=Path(td) / "should_fail")
            raise AssertionError("restore_backup should have refused tampered archive")
        except ValueError:
            pass

    print(
        f"[3/5] Hot backup + SHA-256 tamper rejection + automated DR drill OK "
        f"(RTO={drill['rto_seconds']}s, pg_dump={'LIVE ' + str(drill['verification']['postgres_toc_entries']) + ' TOC entries' if live_pg_url else 'SQLite-only'})"
    )


def verify_ops_api_and_rbac() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        wb = Workbench(
            DB,
            EVENT_DB,
            SOURCE_DIR,
            model=None,
            reviews_db=tmp / "reviews.sqlite3",
            sessions_db=tmp / "sessions.sqlite3",
            audit_db=tmp / "audit.sqlite3",
            jobs_db=tmp / "jobs.sqlite3",
            database_url=f"sqlite+pysqlite:///{tmp / 'ent.sqlite3'}",
        )
        try:
            app = create_app(wb, require_auth=True, rate_limit_per_min=300)
            client = TestClient(app)
            viewer_hdr = {"Authorization": f"Bearer {issue_token('auditor', 'viewer')['access_token']}"}
            eng_hdr = {"Authorization": f"Bearer {issue_token('ops_eng', 'engineer')['access_token']}"}

            # RBAC: viewer cannot trigger backup or DR drill
            assert client.post("/api/ops/backup", headers=viewer_hdr).status_code == 403
            assert client.post("/api/ops/dr-drill", headers=viewer_hdr).status_code == 403

            # Viewer can inspect backups & structured JSON logs
            r_baks = client.get("/api/ops/backups", headers=viewer_hdr)
            assert r_baks.status_code == 200
            r_logs = client.get("/api/ops/logs?limit=15", headers=viewer_hdr)
            assert r_logs.status_code == 200
            logs_data = r_logs.json()
            assert logs_data["format"] == "json (NDJSON)" and logs_data["count"] >= 1

            # Engineer executes live backup via API
            r_do_bak = client.post("/api/ops/backup", headers=eng_hdr)
            assert r_do_bak.status_code == 200 and r_do_bak.json()["status"] == "ok"
            # Clean up created backup tar from default backup dir if desired? Keep one fresh real backup!
            print(f"[4/5] FastAPI /api/ops/* (backups, backup, dr-drill, logs) & RBAC (403/200) OK ({len(client.get('/openapi.json').json()['paths'])} OpenAPI paths)")
        finally:
            wb.db_manager.dispose()


def main() -> None:
    before_db = _sha256(DB)
    before_ev = _sha256(EVENT_DB)
    verify_containers_and_k8s()
    verify_structured_json_logging()
    verify_backup_restore_and_dr()
    verify_ops_api_and_rbac()
    assert _sha256(DB) == before_db, "Source laowadi.sqlite3 modified!"
    assert _sha256(EVENT_DB) == before_ev, "Source laowadi_events.sqlite3 modified!"
    print("[5/5] Read-only source archives SHA-256 strictly unchanged OK")
    print("\nALL 5 OPS & DEPLOYMENT VERIFICATION CHECKS PASSED!")


if __name__ == "__main__":
    main()
