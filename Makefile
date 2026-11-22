# MineGrid-Agent unified developer & operator shortcuts
APP_DIR := 研究实施/老洼地数据接入_20261119
PY := $(shell [ -x 研究实施/.venv-power/bin/python ] && echo "$(CURDIR)/研究实施/.venv-power/bin/python" || echo python3)

.PHONY: help setup up test lint build-ui docker mcp

help:
	@echo "MineGrid-Agent Makefile targets:"
	@echo "  make setup     - Bootstrap Python venv, verify archive SHA-256 & run core gates"
	@echo "  make up        - Launch interactive workbench on http://127.0.0.1:8765/"
	@echo "  make test      - Run 23 verification suites and the thesis evaluation probe"
	@echo "  make lint      - Run static syntax & undefined-name lint (ruff / py_compile)"
	@echo "  make build-ui  - Build React 19 + Vite frontend bundle"
	@echo "  make docker    - Build and launch containerized stack via docker compose"
	@echo "  make mcp       - Start stdio JSON-RPC 2.0 Read-Only MCP server"

setup:
	bash $(APP_DIR)/scripts/setup_collab.sh

up:
	cd $(APP_DIR) && $(PY) -B -m laowadi workbench --port 8765 --workers 1 --external-workers 1

test:
	cd $(APP_DIR) && for s in \
		verify_duty_closure.py \
		verify_advanced_agent_v3.py \
		verify_live_and_actions.py \
		verify_memory_and_deep_research.py \
		verify_autonomous_sandbox.py \
		verify_p0_correctness.py \
		verify_diagnostic_claims.py \
		verify_diagnose.py \
		verify_task_alignment.py \
		verify_proactive_delivery.py \
		verify_p1_autonomy_and_ops.py \
		verify_reliability_and_contracts.py \
		verify_multi_agent_and_governance.py \
		verify_enterprise_and_powerflow.py \
		verify_unified_dag.py \
		verify_rag_and_graph.py \
		verify_supplement_and_mcp.py \
		verify_multimine_generalization.py \
		verify_evaluation.py \
		verify_core_engine_upgrades.py \
		verify_agent_governance_phase1.py \
		verify_agent_v4_integration.py \
		verify_workbench.py; do \
		echo ">>> $$s"; $(PY) -B "$$s" >/dev/null || exit 1; \
	done && $(PY) -B -m thesis_eval.probe --output thesis_eval/results >/dev/null
	@echo "All 23 verification suites and thesis_eval.probe passed."

lint:
	cd $(APP_DIR) && (command -v ruff >/dev/null 2>&1 && ruff check laowadi/ --select E9,F63,F7,F82 || $(PY) -m compileall -q laowadi/)

build-ui:
	cd $(APP_DIR)/workbench && npm ci && npm run build

docker:
	docker compose up --build

mcp:
	cd $(APP_DIR) && $(PY) -B -m laowadi mcp
