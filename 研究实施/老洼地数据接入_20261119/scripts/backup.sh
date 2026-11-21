#!/usr/bin/env bash
# Laowadi Enterprise Automated Hot Backup Script (cron / K8s CronJob / ops manual run)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${LAOWADI_PYTHON:-${PROJECT_DIR}/../.venv-power/bin/python}"
if [ ! -x "${PYTHON_BIN}" ]; then
  PYTHON_BIN="$(command -v python3 || command -v python)"
fi

OUTPUT_DIR="${1:-${PROJECT_DIR}/local/backups}"
RETENTION_DAYS="${LAOWADI_BACKUP_RETENTION_DAYS:-14}"

cd "${PROJECT_DIR}"
exec "${PYTHON_BIN}" -B -m laowadi ops backup \
  --output-dir "${OUTPUT_DIR}" \
  --retention-days "${RETENTION_DAYS}"
