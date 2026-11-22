"""Live LLM End-to-End Evaluation Slice (6 Representative Tasks).

Runs 6 real engineering queries synchronously against the configured LLM
(`研究实施/大模型接入_20261115/.env.local`) across both the Strict Verifier Pipeline
(`diagnose_question`) and the Autonomous Sandbox Agent (`run_autonomous_agent`),
measuring genuine pass/partial/fail outcomes, cell provenance, latency P50/P95,
reflection repairs, and candid failure modes — writing `docs/LIVE_EVAL_REPORT.md`.

Never mutates golden read-only archives (`laowadi.sqlite3` / `laowadi_events.sqlite3`).
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import statistics
import tempfile
import time
from typing import Any

from laowadi.autonomous_agent import _INLINE_CELL_RE, run_autonomous_agent
from laowadi.diagnose import diagnose_question
from laowadi.llm import ChatClient
from laowadi.monitor import EVENT_DB
from laowadi.queries import Archive
from laowadi.storage import DB, ROOT, connect


MODEL_CONFIG_PATH = ROOT.parent / "大模型接入_20261115" / ".env.local"
REPO_ROOT = ROOT.parent.parent
REPORT_MD_PATH = REPO_ROOT / "docs" / "LIVE_EVAL_REPORT.md"
REPORT_JSON_PATH = ROOT / "local" / "live_eval_slice.json"


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    k = (len(xs) - 1) * p
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    if f == c:
        return round(xs[f], 2)
    return round(xs[f] + (xs[c] - xs[f]) * (k - f), 2)


def _extract_ground_truth(archive: Archive) -> dict[str, Any]:
    rows = archive.db.execute(
        """
        SELECT metric, value,
               rp.filename || ' / ' || rp.sheet || '!' || r.source_col || r.source_row AS cell
        FROM reading r JOIN report rp ON r.day = rp.day
        WHERE r.station = 'S35' AND r.cabinet = '303' AND r.time = '2026-11-13 15:00:00'
          AND r.metric IN ('Uab', 'Ia', 'P')
        """
    ).fetchall()
    q1_gt = {r["metric"]: {"value": r["value"], "cell": r["cell"]} for r in rows}

    ov_cnt = archive.db.execute(
        "SELECT COUNT(*) AS cnt FROM reading WHERE station='S35' AND cabinet='1006' AND metric='Uab' AND value > 10.70"
    ).fetchone()["cnt"]
    max_1006 = archive.db.execute(
        "SELECT MAX(value) AS mv FROM reading WHERE station='S35' AND cabinet='1006' AND metric='Uab' AND value > 10.65"
    ).fetchone()["mv"]
    return {
        "q1_303": q1_gt,
        "q4_1006_over_1070_hours": int(ov_cnt),
        "q6_1006_max_kv": round(float(max_1006), 3) if max_1006 is not None else None,
    }


def _evaluate_cases(archive: Archive, client: ChatClient, gt: dict[str, Any], tmp_mem_db: Path) -> list[dict[str, Any]]:
    cases = [
        {
            "id": "Q1",
            "mode": "strict",
            "category": "Single-Incomer Zero-Load Root-Cause Diagnosis",
            "question": "请诊断 2026-11-13 306 柜归零、303 柜单进线运行异常，并给出关键原表坐标与缺失证据边界。",
        },
        {
            "id": "Q2",
            "mode": "strict",
            "category": "Competing-Hypothesis Falsification (Reverse Power)",
            "question": "2026-11-12 12:00 312 和 1006 柜出现负有功，是 1027 瓦斯发电站反送电导致的吗？",
        },
        {
            "id": "Q3",
            "mode": "strict",
            "category": "Cross-Station Boundary Refusal (C1535 Guard)",
            "question": "请诊断井下 1535 中央变电所 1101 柜 2026-11-13 的电压越限与负荷波动原因。",
        },
        {
            "id": "Q4",
            "mode": "autonomous",
            "category": "Read-Only Python Sandbox Statistical Analysis",
            "question": "用 Python 沙盒计算 1006 柜 7 天 Uab 与有功 P 的 Pearson 相关系数，并统计超过 10.70 kV 的整点数。",
        },
        {
            "id": "Q5",
            "mode": "autonomous",
            "category": "pandapower AC Power-Flow Tap Optimization",
            "question": "请用 pandapower 交流潮流评估 2026-11-13 17:00 10 kV II 段过压时，2# 主变应调至哪个分接头档位，以及调档后的预期电压。",
        },
        {
            "id": "Q6",
            "mode": "autonomous",
            "category": "Auto-Memory Redline Extraction + Threshold Query",
            "question": "今后如果 1006 柜电压超过 10.65 kV，请提前按内部红线预警，顺便核查过去 7 天超过 10.65 kV 的最高值。",
        },
    ]

    results: list[dict[str, Any]] = []
    for c in cases:
        t0 = time.monotonic()
        err = None
        ans_full = ""
        ans_excerpt = ""
        tools_used: list[str] = []
        cells: list[str] = []
        reflections = 0
        mem_sugs: list[dict[str, Any]] = []
        raw_status = "ok"
        ver_status = "n/a"

        try:
            if c["mode"] == "strict":
                res = diagnose_question(
                    archive,
                    c["question"],
                    client=client,
                    max_rounds=3,
                    max_tool_calls=4,
                    fallback_to_local=False,
                )
                report = res.get("report") or {}
                ans_full = json.dumps(report, ensure_ascii=False)
                ans_excerpt = str(report.get("summary") or ans_full)[:220]
                raw_status = str(res.get("status") or "ok")
                tools_used = [
                    str(t.get("tool"))
                    for t in (res.get("tool_trace") or [])
                    if isinstance(t, dict) and t.get("tool")
                ]
                ver = res.get("verification") or {}
                ver_status = str(ver.get("status") or "n/a")
                cells = list(ver.get("verified_cells") or [])
                reflections = int(res.get("reflections_triggered") or 0)
            else:
                res = run_autonomous_agent(
                    archive,
                    c["question"],
                    client=client,
                    max_steps=5,
                    memory_db_path=tmp_mem_db,
                )
                ans_full = str(res.get("answer_markdown") or "")
                ans_excerpt = ans_full.replace("\n", " ")[:220]
                raw_status = str(res.get("status") or "ok")
                tools_used = [
                    str(t.get("tool"))
                    for t in (res.get("tool_trace") or [])
                    if isinstance(t, dict) and t.get("tool")
                ]
                vbadge = res.get("verification_badge") or {}
                ver_status = str(vbadge.get("status") or "n/a")
                v_cells = vbadge.get("verified_cells")
                cells = list(v_cells) if isinstance(v_cells, list) else _INLINE_CELL_RE.findall(ans_full)
                mem_sugs = list(res.get("memory_suggestions") or [])
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            raw_status = "error"

        elapsed = round(time.monotonic() - t0, 2)

        checks: dict[str, bool] = {}
        notes: list[str] = []
        if err:
            verdict = "fail"
            notes.append(err)
        elif c["id"] == "Q1":
            checks["mentions_306_zero"] = ("306" in ans_full) and any(k in ans_full for k in ("归零", "0 kW", "0.0", "零负荷", "退出"))
            checks["mentions_303_load"] = "303" in ans_full
            checks["flags_missing_log"] = any(k in ans_full for k in ("操作票", "开关", "保护", "缺", "补证", "未提供"))
            checks["has_verified_cells"] = len(cells) > 0
            passed = sum(checks.values())
            verdict = "pass" if passed == len(checks) else ("partial" if passed >= 2 else "fail")
            if verdict != "pass":
                notes.append(f"Missed checks: {[k for k, v in checks.items() if not v]}; status={raw_status}")
        elif c["id"] == "Q2":
            checks["mentions_1027_zero"] = ("1027" in ans_full) and any(k in ans_full for k in ("0 kW", "0.0", "为 0", "为0", "归零", "零"))
            checks["refutes_gas_export"] = any(k in ans_full for k in ("排除", "证伪", "并非", "不是", "不支持"))
            checks["has_verified_cells"] = len(cells) > 0
            passed = sum(checks.values())
            verdict = "pass" if passed == len(checks) else ("partial" if passed >= 2 else "fail")
            if verdict != "pass":
                notes.append(f"Missed checks: {[k for k, v in checks.items() if not v]}; status={raw_status}")
        elif c["id"] == "Q3":
            checks["refuses_missing_c1535"] = any(
                k in ans_full for k in ("无小时日报", "缺少", "没有归档", "证据不足", "无法", "未收录", "缺失", "截图", "未归档", "暂缺")
            )
            checks["no_fabricated_1101_kv"] = not bool(re.search(r"1101.{0,16}\d+\.\d+\s*kV", ans_full))
            passed = sum(checks.values())
            verdict = "pass" if passed == len(checks) else ("partial" if passed >= 1 else "fail")
            if verdict != "pass":
                notes.append(f"Missed checks: {[k for k, v in checks.items() if not v]}; status={raw_status}")
        elif c["id"] == "Q4":
            checks["used_python_sandbox"] = "run_python_analysis" in tools_used
            checks["has_pearson_num"] = bool(re.search(r"-?0\.\d{2,4}", ans_full))
            checks["has_over_1070_count"] = str(gt["q4_1006_over_1070_hours"]) in ans_full
            passed = sum(checks.values())
            verdict = "pass" if passed == len(checks) else ("partial" if passed >= 2 else "fail")
            if verdict != "pass":
                notes.append(f"Missed checks: {[k for k, v in checks.items() if not v]}; tools={tools_used}")
        elif c["id"] == "Q5":
            checks["used_powerflow_tool"] = any(t in tools_used for t in ("run_ac_power_flow", "run_python_analysis"))
            checks["has_plus2_tap"] = any(k in ans_full for k in ("+2", "＋2", "2档", "2 档"))
            checks["has_post_tap_voltage"] = bool(re.search(r"10\.6\d{1,2}\s*kV", ans_full))
            passed = sum(checks.values())
            verdict = "pass" if passed == len(checks) else ("partial" if passed >= 2 else "fail")
            if verdict != "pass":
                notes.append(f"Missed checks: {[k for k, v in checks.items() if not v]}; tools={tools_used}")
        elif c["id"] == "Q6":
            checks["detected_memory_suggestion"] = len(mem_sugs) > 0 and any(
                abs(float((m.get("metric_threshold") or {}).get("value") or 0) - 10.65) < 0.01 for m in mem_sugs
            )
            max_kv = gt["q6_1006_max_kv"]
            checks["has_peak_voltage"] = max_kv is not None and (
                f"{max_kv:.2f}" in ans_full or f"{max_kv:.3f}" in ans_full or str(max_kv) in ans_full
            )
            passed = sum(checks.values())
            verdict = "pass" if passed == len(checks) else ("partial" if passed >= 1 else "fail")
            if verdict != "pass":
                notes.append(f"Missed checks: {[k for k, v in checks.items() if not v]}; sugs={len(mem_sugs)}")
        else:
            verdict = "fail"

        results.append({
            "id": c["id"],
            "mode": c["mode"],
            "category": c["category"],
            "question": c["question"],
            "verdict": verdict,
            "raw_status": raw_status,
            "verification_status": ver_status,
            "elapsed_seconds": elapsed,
            "tools_used": tools_used,
            "evidence_cells_count": len(set(cells)),
            "reflection_rounds": reflections,
            "memory_suggestions_count": len(mem_sugs),
            "checks": checks,
            "notes": notes,
            "answer_excerpt": ans_excerpt,
        })
        print(
            f"[{c['id']}] {c['mode']:10s} | {verdict.upper():7s} | {elapsed:5.2f}s | ver={ver_status:10s} "
            f"| refl={reflections} | cells={len(set(cells)):2d} | tools={tools_used} | notes={notes}"
        )
    return results


def _write_markdown_report(model_name: str, results: list[dict[str, Any]], gt: dict[str, Any]) -> None:
    latencies = [r["elapsed_seconds"] for r in results]
    pass_cnt = sum(1 for r in results if r["verdict"] == "pass")
    part_cnt = sum(1 for r in results if r["verdict"] == "partial")
    fail_cnt = sum(1 for r in results if r["verdict"] == "fail")
    refl_tot = sum(int(r.get("reflection_rounds") or 0) for r in results)
    p50 = _percentile(latencies, 0.50)
    p95 = _percentile(latencies, 0.95)
    mean_s = round(statistics.mean(latencies), 2) if latencies else 0.0
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# Live LLM End-to-End Evaluation Report",
        "",
        "> **Transparency Note:** Unlike our 20 deterministic CI regression suites (which validate SQLite cell provenance, pandapower physics, security guardrails, and API contracts offline), this report records **real, unedited live API runs** against the configured LLM model. Imperfections, partial passes, reflection repairs, and real wall-clock latencies are reported candidly.",
        "",
        "## 1. Run Configuration",
        "",
        f"- **Evaluated Timestamp:** `{now_utc}`",
        f"- **Model:** `{model_name}` (real external API, `fallback_to_local=False`)",
        "- **Dataset Scope:** Read-only 7-day SCADA archive (`laowadi.sqlite3`, 168 hourly snapshots, 2026-11-10 to 2026-11-16)",
        f"- **Sample Size:** `{len(results)}` representative engineering tasks (3 Strict Verifier Pipeline + 3 Autonomous Sandbox Agent)",
        f"- **Ground-Truth Anchors:** 303 @ `2026-11-13 15:00` (`Uab={gt['q1_303'].get('Uab', {}).get('value')} kV`, `Ia={gt['q1_303'].get('Ia', {}).get('value')} A`, `P={gt['q1_303'].get('P', {}).get('value')} kW`); 1006 `>10.70 kV` hours = `{gt['q4_1006_over_1070_hours']}`; 1006 7-day peak = `{gt['q6_1006_max_kv']} kV`",
        "",
        "## 2. Headline Results",
        "",
        "| Metric | Observed Value |",
        "| :--- | :---: |",
        f"| **Strict Pass** | `{pass_cnt} / {len(results)}` ({round(pass_cnt / len(results) * 100, 1)}%) |",
        f"| **Partial Pass** | `{part_cnt} / {len(results)}` |",
        f"| **Fail / Timeout / Error** | `{fail_cnt} / {len(results)}` |",
        f"| **Total Patch-Only Reflections Triggered** | `{refl_tot}` |",
        f"| **Mean Latency** | `{mean_s} s` |",
        f"| **P50 Latency** | `{p50} s` |",
        f"| **P95 Latency** | `{p95} s` |",
        "",
        "## 3. Per-Question Breakdown",
        "",
        "| ID | Mode | Task Category | Verdict | Latency | Verifier | Reflections | Verified Cells | Tools Invoked | Notes |",
        "| :---: | :---: | :--- | :---: | :---: | :---: | :---: | :---: | :--- | :--- |",
    ]
    for r in results:
        t_str = ", ".join(f"`{t}`" for t in r["tools_used"]) if r["tools_used"] else "*(none)*"
        n_str = "; ".join(r["notes"]) if r["notes"] else "All programmatic checks passed"
        lines.append(
            f"| **{r['id']}** | `{r['mode']}` | {r['category']} | **{r['verdict'].upper()}** | `{r['elapsed_seconds']} s` "
            f"| `{r['verification_status']}` | `{r['reflection_rounds']}` | `{r['evidence_cells_count']}` | {t_str} | {n_str} |"
        )

    lines += [
        "",
        "## 4. Candid Engineering Observations & Limitations",
        "",
        "1. **Small Evaluation Slice (N=6):** This live slice is an end-to-end smoke benchmark for real LLM tool selection, sandbox coding, and verification latency — it is **not** a statistically significant multi-mine benchmark.",
        "2. **Latency Profile:** Multi-step ReAct loops with `pandapower` AC power-flow or sandbox Python execution require multi-second round-trips (see P50/P95 above); real-time sub-second SCADA protection remains strictly outside LLM scope.",
        "3. **Deterministic Guardrail Dependency:** Exact numerical integrity relies heavily on the deterministic `verifier.py` and read-only SQLite/pandapower tools rather than raw LLM parametric memory.",
        "4. **Observed Edge Cases:**",
    ]
    imperfect = [r for r in results if r["verdict"] != "pass"]
    if imperfect:
        for r in imperfect:
            lines.append(f"   - **{r['id']} ({r['verdict'].upper()})**: {'; '.join(r['notes']) or 'Partial match on strict programmatic checks.'}")
    else:
        lines.append("   - All 6 tasks in this slice met programmatic checks; however, phrasing variations, network jitter, and unseen mine topologies can still cause partial passes or timeouts in open-ended usage.")

    lines += [
        "",
        "## 5. Reproducibility",
        "",
        "```bash",
        "cd 研究实施/老洼地数据接入_20261119",
        "python -B run_live_eval_slice.py",
        "```",
        "",
    ]
    REPORT_MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    if not MODEL_CONFIG_PATH.is_file():
        raise FileNotFoundError(f"Missing local LLM config: {MODEL_CONFIG_PATH}")
    client = ChatClient.from_config_file(MODEL_CONFIG_PATH, stream=True, max_retries=1)
    client.timeout_seconds = 60
    model_name = getattr(client, "model", None) or getattr(getattr(client, "cfg", None), "model", "deepseek-v4-pro")
    print(f"Running 6-question live evaluation slice against model={model_name} ...")

    with closing(connect(DB)) as conn, tempfile.TemporaryDirectory() as tmp:
        archive = Archive(conn, EVENT_DB)
        gt = _extract_ground_truth(archive)
        results = _evaluate_cases(archive, client, gt, Path(tmp) / "live_eval_mem.sqlite3")

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": model_name,
        "ground_truth": gt,
        "results": results,
    }
    REPORT_JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown_report(model_name, results, gt)
    print(f"[OK] Wrote {REPORT_MD_PATH} and {REPORT_JSON_PATH}")


if __name__ == "__main__":
    main()
