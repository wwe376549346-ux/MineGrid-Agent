# Live LLM End-to-End Evaluation Report

> **Transparency Note:** Unlike our 20 deterministic CI regression suites (which validate SQLite cell provenance, pandapower physics, security guardrails, and API contracts offline), this report records **real, unedited live API runs** against the configured LLM model. Imperfections, partial passes, reflection repairs, and real wall-clock latencies are reported candidly.

## 1. Run Configuration

- **Evaluated Timestamp:** `2026-11-22 04:35 UTC`
- **Model:** `deepseek-v4-pro` (real external API, `fallback_to_local=False`)
- **Dataset Scope:** Read-only 7-day SCADA archive (`laowadi.sqlite3`, 168 hourly snapshots, 2026-11-10 to 2026-11-16)
- **Sample Size:** `6` representative engineering tasks (3 Strict Verifier Pipeline + 3 Autonomous Sandbox Agent)
- **Ground-Truth Anchors:** 303 @ `2026-11-13 15:00` (`Uab=36.88 kV`, `Ia=98.16 A`, `P=5710.12 kW`); 1006 `>10.70 kV` hours = `35`; 1006 7-day peak = `10.9 kV`

## 2. Headline Results

| Metric | Observed Value |
| :--- | :---: |
| **Strict Pass** | `5 / 6` (83.3%) |
| **Partial Pass** | `1 / 6` |
| **Fail / Timeout / Error** | `0 / 6` |
| **Total Patch-Only Reflections Triggered** | `3` |
| **Mean Latency** | `103.33 s` |
| **P50 Latency** | `85.5 s` |
| **P95 Latency** | `157.07 s` |

## 3. Per-Question Breakdown

| ID | Mode | Task Category | Verdict | Latency | Verifier | Reflections | Verified Cells | Tools Invoked | Notes |
| :---: | :---: | :--- | :---: | :---: | :---: | :---: | :---: | :--- | :--- |
| **Q1** | `strict` | Single-Incomer Zero-Load Root-Cause Diagnosis | **PASS** | `64.61 s` | `verified` | `2` | `6` | `get_window_snapshot` | All programmatic checks passed |
| **Q2** | `strict` | Competing-Hypothesis Falsification (Reverse Power) | **PASS** | `92.02 s` | `verified` | `0` | `11` | `get_window_snapshot`, `get_window_snapshot`, `get_window_snapshot` | All programmatic checks passed |
| **Q3** | `strict` | Cross-Station Boundary Refusal (C1535 Guard) | **PASS** | `160.64 s` | `warning` | `1` | `0` | `get_cabinet_health_profile`, `get_window_snapshot`, `retrieve_conditional_evidence`, `get_station_health_overview` | All programmatic checks passed |
| **Q4** | `autonomous` | Read-Only Python Sandbox Statistical Analysis | **PASS** | `146.37 s` | `partial` | `0` | `0` | `run_python_analysis` | All programmatic checks passed |
| **Q5** | `autonomous` | pandapower AC Power-Flow Tap Optimization | **PASS** | `78.97 s` | `verified` | `0` | `0` | `check_power_balance`, `get_window_snapshot`, `run_ac_power_flow`, `run_python_analysis` | All programmatic checks passed |
| **Q6** | `autonomous` | Auto-Memory Redline Extraction + Threshold Query | **PARTIAL** | `77.39 s` | `partial` | `0` | `9` | `get_cabinet_health_profile`, `query_electrical_analytics`, `search_coal_standards`, `run_python_analysis` | Missed checks: ['detected_memory_suggestion']; sugs=1 |

## 4. Candid Engineering Observations & Limitations

1. **Small Evaluation Slice (N=6):** This live slice is an end-to-end smoke benchmark for real LLM tool selection, sandbox coding, and verification latency — it is **not** a statistically significant multi-mine benchmark.
2. **Latency Profile:** Multi-step ReAct loops with `pandapower` AC power-flow or sandbox Python execution require multi-second round-trips (see P50/P95 above); real-time sub-second SCADA protection remains strictly outside LLM scope.
3. **Deterministic Guardrail Dependency:** Exact numerical integrity relies heavily on the deterministic `verifier.py` and read-only SQLite/pandapower tools rather than raw LLM parametric memory.
4. **Observed Edge Cases:**
   - **Q6 (PARTIAL)**: Missed checks: ['detected_memory_suggestion']; sugs=1

## 5. Reproducibility

```bash
cd 研究实施/老洼地数据接入_20261119
python -B run_live_eval_slice.py
```
