#!/usr/bin/env bash
# 老洼地煤矿配电网诊断智能体 · 课题组双人协同开发一键环境初始化与门禁自检脚本
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
STUDY_DIR="$(cd "${APP_DIR}/.." && pwd)"
VENV_DIR="${STUDY_DIR}/.venv-power"
LLM_DIR="${STUDY_DIR}/大模型接入_20261115"

echo "=== [1/4] 检查 Python 虚拟环境 (${VENV_DIR}) ==="
if [ ! -x "${VENV_DIR}/bin/python" ]; then
  PYTHON_BIN="$(command -v python3.12 || command -v python3)"
  echo "创建虚拟环境 (${PYTHON_BIN})..."
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
  "${VENV_DIR}/bin/pip" install --upgrade pip
  "${VENV_DIR}/bin/pip" install -e "${APP_DIR}"
else
  echo "已检测到虚拟环境: $(${VENV_DIR}/bin/python --version)"
fi

echo "=== [2/4] 检查本地大模型密钥配置文件 ==="
if [ ! -f "${LLM_DIR}/.env.local" ] && [ -f "${LLM_DIR}/.env.example" ]; then
  cp "${LLM_DIR}/.env.example" "${LLM_DIR}/.env.local"
  echo "提示: 已从模板生成 ${LLM_DIR}/.env.local，请填入课题组 DeepSeek API 密钥（该文件已被 .gitignore 忽略，不会上传）。"
else
  echo "本地模型配置文件就绪。"
fi

echo "=== [3/4] 校验老洼地只读黄金归档库 SHA-256 ==="
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
    assert p.exists(), f'缺失核心只读库: {rel}'
    actual = hashlib.sha256(p.read_bytes()).hexdigest()
    assert actual == exp, f'{rel} SHA-256 不一致! 期望 {exp}, 实际 {actual}'
    print(f'  [OK] {rel} SHA-256 = {actual[:16]}...')
"

echo "=== [4/4] 运行合并前核心门禁自检 (Pre-Merge Gate) ==="
"${VENV_DIR}/bin/python" -B verify_p0_correctness.py
"${VENV_DIR}/bin/python" -B verify_diagnostic_claims.py
"${VENV_DIR}/bin/python" -B verify_multi_agent_and_governance.py
"${VENV_DIR}/bin/python" -B verify_workbench.py
"${VENV_DIR}/bin/python" -B -m thesis_eval.probe --output thesis_eval/results

echo ""
echo "=================================================================="
echo " 协同开发环境校验全部通过！"
echo " 启动本地 8765 工作台命令："
echo "   cd \"${APP_DIR}\" && ../ .venv-power/bin/python -m laowadi workbench --port 8765"
echo "=================================================================="
