# MineGrid-Agent `v1.0.0` Release Notes

**Release Date:** 2026-11-22  
**Tag:** `v1.0.0`  
**Stack:** Python 3.12 · FastAPI + Uvicorn · `pandapower` 3.5.6 · React 19 + Vite · Read-Only SQLite / JSON-RPC 2.0 MCP

---

## Highlights in `v1.0.0`

### 1. Evidence-Grounded Neuro-Symbolic Diagnosis & AC Power-Flow Arbitration
- **Dual Operator Interaction Modes**: Seamlessly switch between **Strict Audit Mode** (`diagnose.py` + `verifier.py` claim-level Excel provenance & Patch-Only Reflection) and **Autonomous Deep Agent Mode** (`autonomous_agent.py` + AST-audited read-only Python/SQL/NumPy/SciPy/`pandapower` sandbox in `code_sandbox.py` with inline SVG scatter/line/bar charts).
- **Real Nonlinear Physics Integration**: `powerflow.py` couples declarative `MineProfile` topology schemas (`mine_schema.py`) to `pandapower` Newton-Raphson AC power flow (`base_case`, `tap_sensitivity`, `n1_contingency`, `reactive_opt`), 24h/168h discrete dynamic programming tap/capacitor scheduling, Pareto frontier sweeps, and Value-of-Information (`VoI`) parameter sensitivity ranking.
- **High-Order Multi-Agent & Industrial Ops**: 3-way parallel specialist debate (`subagent_debate.py`), 3-layer persistent operational memory with colloquial Auto-Memory distillation (`memory_store.py`), 6-stage deep investigation reports exportable to HTML/Markdown (`deep_research.py`), hot-loadable Markdown skills + read-only MCP registry (`skills_engine.py`), read-only industrial protocol adapters (`Modbus TCP` / `OPC-UA` / `MQTT` in `protocol_adapter.py`), and human-approved electrical action tickets with mandatory checklist `HTTP 409` guards (`action_tickets.py`).

### 2. Performance & Reliability Refinements
- **Thread-Safe Bounded LRU Power-Flow Caching** (`_BoundedLRUCache` in `powerflow.py`) & topic-dynamic 6-class subagent debate arbitration.
- **Live Latency & Provenance Optimizations**:
  - Added deterministic fast-path routing for unarchived underground substation (`C1535`) queries (`diagnose.py`), cutting Q3 live latency from **`160.64 s` (`warning`) → `0.07 s` (`verified`)**.
  - Upgraded colloquial voltage threshold extraction (`memory_store.py`) and parallel tool guidance (`autonomous_agent.py`), raising our 6-question live `deepseek-v4-pro` end-to-end smoke slice from **`5/6 (83.3%, mean 103.33 s)` → `6/6 (mean 57.76 s)`** ([LIVE_EVAL_REPORT.md](LIVE_EVAL_REPORT.md)).
- **Vite Vendor Code Splitting**: Split React, UI vendor, and icons cleanly (`workbench/vite.config.js`), eliminating `>500 kB` chunk warnings.
- **Second-Mine Declarative Onboarding Example**: Added [`profiles/dongpo_demo.json`](../研究实施/老洼地数据接入_20261119/profiles/dongpo_demo.json) (35/6 kV onboarding demo) & `GET /api/profiles`, plus top-level [Makefile](../Makefile) and GitHub Actions `ruff` lint gate.

### 3. Live `deepseek-v4-pro` Stratified 4-Arm Ablation (`N = 18` / `21` Turns)
Measured directly against the external `deepseek-v4-pro` API ([LIVE_ABLATION_BENCHMARK.md](LIVE_ABLATION_BENCHMARK.md)):
- **Task Exact Accuracy**: **`55.6%`** (`Ours`) vs. `38.9%` (`w/o pandapower`), `27.8%` (`Vanilla LLM`), `22.2%` (`w/o Verifier`).
- **Numeric Hallucination Reduction**: **`5.6%`** (`Ours`) vs. **`31.6%`** (`Vanilla LLM`) — a **`-26.0%`** paired cluster-bootstrap reduction (`95% CI: [-50.0%, -0.4%]`).
- **AC Power Flow Layer (`P01/P03/P04`)**: **`66.7%` exact / `90.9%` Citation F1 / `0.0%` hallucination** vs. `0.0%` without `pandapower` or in Vanilla.

---

## Quick Start

```bash
git clone git@github.com:wwe376549346-ux/MineGrid-Agent.git
cd MineGrid-Agent

# Containerized launch
make docker   # http://127.0.0.1:8765/

# Or local Python venv + full 20 offline gates
make setup && make test && make up
```

---

## Scope & Known Limitations
- Empirical archive covers **7 consecutive days** (`2026-11-10`–`2026-11-16`, 168 hourly snapshots) from one real coal mine (`laowadi`); `dongpo_demo.json` is a synthetic multi-mine onboarding fixture rather than a second field dataset.
- Equivalent circuit parameters missing from daily reports (transformer tap position, `uk%`, `Sn`, cable ampacity) remain explicitly surfaced as `model_assumptions` and prioritized via `VoI`.
- Intended strictly for engineering analysis and human-approved decision support — **not** sub-second protection relaying or unattended remote breaker actuation.
