#!/usr/bin/env bash
# Laowadi Enterprise Verified Restore Script (verifies SHA-256 & integrity before restoring)
set -euo pipefail

if [ $# -lt 2 ]; then
  echo "Usage: $0 <backup_bundle.tar.gz> <target_dir> [--target-pg-url <postgresql+psycopg://...>]" >&2
  exit 2
fi

BUNDLE_PATH="$1"
TARGET_DIR="$2"
shift 2

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${LAOWADI_PYTHON:-${PROJECT_DIR}/../.venv-power/bin/python}"
if [ ! -x "${PYTHON_BIN}" ]; then
  PYTHON_BIN="$(command -v python3 || command -v python)"
fi

cd "${PROJECT_DIR}"
exec "${PYTHON_BIN}" -B -m laowadi ops restore \
  --bundle "${BUNDLE_PATH}" \
  --target-dir "${TARGET_DIR}" \
  "$@"
