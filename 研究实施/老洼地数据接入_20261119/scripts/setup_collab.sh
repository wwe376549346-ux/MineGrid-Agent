#!/usr/bin/env bash
# MineGrid-Agent: One-command environment bootstrap, archive checksum check & pre-merge verification gate
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
STUDY_DIR="$(cd "${APP_DIR}/.." && pwd)"
VENV_DIR="${STUDY_DIR}/.venv-power"
LLM_DIR="${STUDY_DIR}/大模型接入_20261115"

echo "=== [1/4] Checking Python virtual environment (${VENV_DIR}) ==="
if [ ! -x "${VENV_DIR}/bin/python" ]; then
  PYTHON_BIN="$(command -v python3.12 || command -v python3)"
  echo "Creating virtual environment with ${PYTHON_BIN}..."
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
  "${VENV_DIR}/bin/pip" install --upgrade pip
  "${VENV_DIR}/bin/pip" install -e "${APP_DIR}"
else
  echo "Detected existing virtual environment: $(${VENV_DIR}/bin/python --version)"
fi

echo "=== [2/4] Checking local LLM configuration ==="
if [ ! -f "${LLM_DIR}/.env.local" ] && [ -f "${LLM_DIR}/.env.example" ]; then
  cp "${LLM_DIR}/.env.example" "${LLM_DIR}/.env.local"
  echo "Created ${LLM_DIR}/.env.local from template. Add your API key locally (ignored by Git)."
else
  echo "Local model configuration ready."
fi

echo "=== [3/4] Verifying read-only golden archive SHA-256 checksums ==="
cd "${APP_DIR}"
"${VENV_DIR}/bin/python" -B -c "
import hashlib
from pathlib import Path
expected = {
    'local/laowadi.sqlite3': '37946d8abb40bd34295b7787c3426cc3e19a0ece9c9df70e45481f2253fb573a',
    'local/laowadi_events.sqlite3': '99373c36ce28939905d12a93714e05ca886ea8068cd31690fd33ea2d2a5c8d05',
}
for rel, exp in expected.items():
    p = Path(rel)
    assert p.exists(), f'Missing core read-only archive: {rel}'
    actual = hashlib.sha256(p.read_bytes()).hexdigest()
    assert actual == exp, f'{rel} SHA-256 mismatch! Expected {exp}, got {actual}'
    print(f'  [OK] {rel} SHA-256 = {actual[:16]}...')
"

echo "=== [4/4] Running pre-merge verification gates ==="
"${VENV_DIR}/bin/python" -B verify_p0_correctness.py
"${VENV_DIR}/bin/python" -B verify_diagnostic_claims.py
"${VENV_DIR}/bin/python" -B verify_diagnose.py
"${VENV_DIR}/bin/python" -B verify_multi_agent_and_governance.py
"${VENV_DIR}/bin/python" -B verify_enterprise_and_powerflow.py
"${VENV_DIR}/bin/python" -B verify_proactive_delivery.py
"${VENV_DIR}/bin/python" -B verify_workbench.py
"${VENV_DIR}/bin/python" -B -m thesis_eval.probe --output thesis_eval/results

echo ""
echo "=================================================================="
echo " MineGrid-Agent environment & verification gates PASSED!"
echo " Launch the interactive operator workbench:"
echo "   cd \"${APP_DIR}\" && ../.venv-power/bin/python -B -m laowadi workbench --port 8765"
echo "=================================================================="
