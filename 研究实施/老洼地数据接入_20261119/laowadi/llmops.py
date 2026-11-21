"""LLMOps Telemetry, Fast/Slow Tiered Query Router, and Thread-Safe Parallel Tool Executor.

Implements P1-2:
- `classify_query_tier`: routes deterministic single-point lookups & pure tabular analytics to the millisecond
  fast path while sending complex multi-hop diagnostic reasoning to the LLM slow path.
- `execute_tool_calls_parallel`: executes multi-tool LLM batches concurrently via `ThreadPoolExecutor` with
  per-thread read-only SQLite connections (100% SQLite thread-safe, preserves call order, tracks saved ms).
- `LLMOpsRegistry`: aggregates TTFT, TPOT, token consumption, CNY cost estimation, failover counts,
  Reflection rate, Verifier warning rate, fast-tier bypasses, and parallel tool batch speedups for both
  JSON API (`/api/llmops/metrics`) and Prometheus (`/metrics`).
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import re
import threading
import time
from typing import Any

from laowadi.diag_tools import DiagnosticToolbox
from laowadi.queries import Archive
from laowadi.storage import connect

# Approximate DeepSeek-V4-Pro / DeepSeek-Chat CNY pricing per 1M tokens (configurable reference)
INPUT_CNY_PER_1M = 2.0
OUTPUT_CNY_PER_1M = 8.0


def estimate_tokens_and_cost(
    messages: list[dict],
    content: str,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract real token usage if provided by gateway, else honestly estimate from character counts."""
    u = usage or {}
    if isinstance(u.get("prompt_tokens"), int) and isinstance(u.get("completion_tokens"), int):
        p_tok = int(u["prompt_tokens"])
        c_tok = int(u["completion_tokens"])
        tot = int(u.get("total_tokens") or (p_tok + c_tok))
        estimated = False
    else:
        prompt_chars = sum(len(str(m.get("content") or "")) for m in (messages or []))
        comp_chars = len(str(content or ""))
        p_tok = max(1, int(round(prompt_chars * 0.75)))
        c_tok = max(1, int(round(comp_chars * 0.75)))
        tot = p_tok + c_tok
        estimated = True
    cost_cny = round((p_tok / 1_000_000.0) * INPUT_CNY_PER_1M + (c_tok / 1_000_000.0) * OUTPUT_CNY_PER_1M, 6)
    return {
        "prompt_tokens": p_tok,
        "completion_tokens": c_tok,
        "total_tokens": tot,
        "estimated": estimated,
        "estimated_cost_cny": cost_cny,
    }


def classify_query_tier(question: str, anchors: dict[str, Any] | None = None) -> dict[str, Any]:
    """Classify a user query into `fast` (deterministic ms path) vs `slow` (full multi-round LLM ReAct)."""
    q = str(question or "").strip()
    anc = anchors or {}
    days = anc.get("days") or []
    hours = anc.get("hours") or []
    cabs = anc.get("cabinets") or []

    complex_signals = (
        "为什么", "原因", "怎么回事", "异常", "诊断", "健康", "假设", "证伪", "排查", "规程",
        "潮流", "分接头", "无功补偿", "方案", "建议", "会诊", "故障", "跳闸", "过载", "补证",
        "评估", "分析", "风险", "联合治理", "简报",
    )
    if any(k in q for k in complex_signals):
        return {"tier": "slow", "reason": "complex_diagnostic_reasoning"}

    # Fast-tier 1: Direct single-point lookup (date + hour + cabinet)
    if len(days) == 1 and len(hours) >= 1 and len(cabs) >= 1 and any(k in q for k in ("是多少", "读数", "多少", "同期", "什么值")):
        return {"tier": "fast", "reason": "direct_point_lookup"}

    # Fast-tier 2: Pure tabular statistical ranking / cumulative kWh / joint threshold filter
    stat_signals = ("排行", "排名", "标准差", "波动最大", "波动前", "累计用电", "用电量", "总电量", "平均有功", "日均", "筛选", "同时满足", "最高", "最低", "极差")
    if any(k in q for k in stat_signals):
        return {"tier": "fast", "reason": "deterministic_analytics_rank"}

    return {"tier": "slow", "reason": "open_ended_question"}


def execute_tool_calls_parallel(
    archive: Archive,
    toolbox: DiagnosticToolbox,
    parsed_calls: list[dict[str, Any]],
    *,
    max_workers: int = 4,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Execute a batch of distinct read-only tool calls concurrently using per-thread read-only archive connections.

    Each item in `parsed_calls` must contain `{"tool_call_id", "name", "arguments"}`.
    Returns `(results_in_order, batch_stats)`.
    """
    if not parsed_calls:
        return [], {"parallel": False, "count": 0, "wall_ms": 0.0, "serial_sum_ms": 0.0, "saved_ms": 0.0}

    reader = archive.parallel_reader() if len(parsed_calls) >= 2 else None
    t_wall0 = time.monotonic()

    if reader is None:
        out = []
        serial_sum = 0.0
        for item in parsed_calls:
            t0 = time.monotonic()
            res = toolbox.execute(item["name"], item["arguments"])
            dt = (time.monotonic() - t0) * 1000.0
            serial_sum += dt
            out.append({**item, "result": res, "elapsed_ms": round(dt, 2)})
        wall_ms = round((time.monotonic() - t_wall0) * 1000.0, 2)
        return out, {
            "parallel": False,
            "count": len(parsed_calls),
            "wall_ms": wall_ms,
            "serial_sum_ms": round(serial_sum, 2),
            "saved_ms": 0.0,
        }

    def _run_one(item: dict[str, Any]) -> dict[str, Any]:
        t0 = time.monotonic()
        with reader() as thread_arc:
            thread_tb = DiagnosticToolbox(thread_arc, profile=getattr(toolbox, "profile", None))
            res = thread_tb.execute(item["name"], item["arguments"])
        dt = (time.monotonic() - t0) * 1000.0
        return {**item, "result": res, "elapsed_ms": round(dt, 2)}

    workers = max(2, min(int(max_workers), len(parsed_calls)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="diag-tool") as pool:
        futs = [pool.submit(_run_one, item) for item in parsed_calls]
        results = [f.result() for f in futs]

    wall_ms = round((time.monotonic() - t_wall0) * 1000.0, 2)
    serial_sum = round(sum(r["elapsed_ms"] for r in results), 2)
    saved_ms = round(max(0.0, serial_sum - wall_ms), 2)
    return results, {
        "parallel": True,
        "count": len(parsed_calls),
        "wall_ms": wall_ms,
        "serial_sum_ms": serial_sum,
        "saved_ms": saved_ms,
    }


def dispatch_react_tool_round(
    archive: Archive,
    toolbox: DiagnosticToolbox,
    tool_calls: list[dict],
    *,
    round_idx: int,
    max_tool_calls: int,
    seen_calls: set[str],
    trace: list[dict],
    messages: list[dict],
    gssc_log: list[dict],
    anchors: dict,
    compress_fn,
    summarize_fn,
    emit,
) -> dict[str, Any]:
    """Execute one LLM ReAct round's tool_calls concurrently while preserving budget, dedup, GSSC, and message order."""
    entries: list[dict[str, Any]] = []
    run_batch: list[dict[str, Any]] = []

    for tc in tool_calls:
        tc_id = tc.get("id") or f"call_{len(trace) + len(entries) + 1}"
        if len(trace) + len(run_batch) >= max_tool_calls:
            entries.append({"kind": "budget", "tool_call_id": tc_id})
            continue
        fn = tc.get("function") or {}
        t_name = fn.get("name", "")
        raw_args = fn.get("arguments") or "{}"
        try:
            t_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            t_args = None
        sig = json.dumps({"tool": t_name, "arguments": t_args}, ensure_ascii=False, sort_keys=True)
        if sig in seen_calls:
            entries.append({"kind": "dup", "tool_call_id": tc_id, "name": t_name, "arguments": t_args})
        else:
            seen_calls.add(sig)
            idx_in_batch = len(run_batch)
            run_batch.append({"tool_call_id": tc_id, "name": t_name, "arguments": t_args})
            entries.append({"kind": "exec", "batch_index": idx_in_batch, "tool_call_id": tc_id, "name": t_name, "arguments": t_args})

    for b_idx, item in enumerate(run_batch, 1):
        emit("tool_start", round=round_idx, step=len(trace) + b_idx, tool=item["name"], arguments=item["arguments"] or {}, parallel_batch=len(run_batch) >= 2)

    exec_results, batch_stats = execute_tool_calls_parallel(archive, toolbox, run_batch)

    for ent in entries:
        if ent["kind"] == "budget":
            messages.append({
                "role": "tool",
                "tool_call_id": ent["tool_call_id"],
                "content": json.dumps({"status": "budget_exhausted", "message": "工具调用预算已满，请直接基于已获证据输出 JSON 诊断单。"}, ensure_ascii=False),
            })
            continue
        step_no = len(trace) + 1
        if ent["kind"] == "dup":
            t_name, t_args = ent["name"], ent["arguments"]
            res = {"status": "duplicate_skipped", "message": "相同工具与参数已查询过，请复用此前结果。"}
            comp_res, comp_saved = res, 0.0
        else:
            item_res = exec_results[ent["batch_index"]]
            t_name, t_args, res = item_res["name"], item_res["arguments"], item_res["result"]
            comp_res, comp_stat = compress_fn(t_name, res, anchors)
            comp_saved = comp_stat["saved_pct"]
            gssc_log.append(comp_stat)
        trace.append({"round": round_idx, "tool": t_name, "arguments": t_args, "result": res})
        emit(
            "tool_end",
            round=round_idx,
            step=step_no,
            tool=t_name,
            status=res.get("status", "ok"),
            gssc_saved_pct=comp_saved,
            summary=summarize_fn(t_name, res),
        )
        messages.append({
            "role": "tool",
            "tool_call_id": ent["tool_call_id"],
            "content": json.dumps(comp_res, ensure_ascii=False, allow_nan=False),
        })
    return batch_stats


class LLMOpsRegistry:
    """Thread-safe in-process LLMOps telemetry store exported to `/api/llmops/metrics` & Prometheus `/metrics`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls_total = 0
        self.errors_total = 0
        self.failovers_total = 0
        self.local_fallbacks_total = 0
        self.prompt_tokens_total = 0
        self.completion_tokens_total = 0
        self.cost_cny_total = 0.0
        self.reflections_total = 0
        self.verifier_verified_total = 0
        self.verifier_warning_total = 0
        self.execution_completed_total = 0
        self.business_trusted_total = 0
        self.business_untrusted_total = 0
        self._business_status_counts: dict[str, int] = {
            "verified": 0,
            "verification_failed": 0,
            "insufficient_data": 0,
            "needs_supplement": 0,
            "restricted": 0,
            "conditional_simulation": 0,
            "error": 0,
        }
        self.fast_tier_routed_total = 0
        self.slow_tier_routed_total = 0
        self.parallel_tool_batches_total = 0
        self.parallel_tool_saved_ms_total = 0.0
        self._ttft_ms: deque[float] = deque(maxlen=200)
        self._tpot_ms: deque[float] = deque(maxlen=200)
        self._models_used: dict[str, int] = {}

    def record_llm_call(self, record: dict[str, Any]) -> None:
        with self._lock:
            self.calls_total += 1
            if record.get("status") != "completed":
                self.errors_total += 1
            if record.get("failover_used"):
                self.failovers_total += 1
            m = str(record.get("served_by_model") or record.get("response_model") or "unknown")
            self._models_used[m] = self._models_used.get(m, 0) + 1
            if record.get("ttft_ms") is not None:
                self._ttft_ms.append(float(record["ttft_ms"]))
            if record.get("tpot_ms") is not None:
                self._tpot_ms.append(float(record["tpot_ms"]))
            u = record.get("token_accounting") or {}
            self.prompt_tokens_total += int(u.get("prompt_tokens", 0))
            self.completion_tokens_total += int(u.get("completion_tokens", 0))
            self.cost_cny_total = round(self.cost_cny_total + float(u.get("estimated_cost_cny", 0.0)), 6)

    def record_diagnosis_outcome(
        self,
        *,
        tier: str,
        verification_status: str | None,
        business_status: str | None = None,
        reflections: int = 0,
        degraded_to_local: bool = False,
        parallel_batches: int = 0,
        parallel_saved_ms: float = 0.0,
    ) -> None:
        with self._lock:
            self.execution_completed_total += 1
            if tier == "fast":
                self.fast_tier_routed_total += 1
            else:
                self.slow_tier_routed_total += 1
            self.reflections_total += int(reflections or 0)
            if verification_status == "verified":
                self.verifier_verified_total += 1
            elif verification_status == "warning":
                self.verifier_warning_total += 1
            eff_biz = business_status or (
                "verified" if verification_status == "verified" else (
                    "insufficient_data" if verification_status == "insufficient_data" else (
                        "restricted" if verification_status == "restricted" else "verification_failed"
                    )
                )
            )
            self._business_status_counts[eff_biz] = self._business_status_counts.get(eff_biz, 0) + 1
            if eff_biz == "verified":
                self.business_trusted_total += 1
            else:
                self.business_untrusted_total += 1
            if degraded_to_local:
                self.local_fallbacks_total += 1
            if parallel_batches > 0:
                self.parallel_tool_batches_total += int(parallel_batches)
                self.parallel_tool_saved_ms_total = round(self.parallel_tool_saved_ms_total + float(parallel_saved_ms), 2)

    @staticmethod
    def _pct(vals: list[float], p: float) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        idx = min(len(s) - 1, max(0, int(round((len(s) - 1) * p))))
        return round(s[idx], 2)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            ttfts = list(self._ttft_ms)
            tpots = list(self._tpot_ms)
            tot_ver = self.verifier_verified_total + self.verifier_warning_total
            warn_rate = round(self.verifier_warning_total / tot_ver * 100.0, 2) if tot_ver else 0.0
            trust_rate = (
                round(self.business_trusted_total / self.execution_completed_total * 100.0, 2)
                if self.execution_completed_total
                else 0.0
            )
            return {
                "llm_calls_total": self.calls_total,
                "llm_errors_total": self.errors_total,
                "llm_failovers_total": self.failovers_total,
                "llm_local_fallbacks_total": self.local_fallbacks_total,
                "ttft_ms_avg": round(sum(ttfts) / len(ttfts), 2) if ttfts else 0.0,
                "ttft_ms_p95": self._pct(ttfts, 0.95),
                "tpot_ms_avg": round(sum(tpots) / len(tpots), 2) if tpots else 0.0,
                "prompt_tokens_total": self.prompt_tokens_total,
                "completion_tokens_total": self.completion_tokens_total,
                "total_tokens": self.prompt_tokens_total + self.completion_tokens_total,
                "estimated_cost_cny_total": round(self.cost_cny_total, 6),
                "reflections_triggered_total": self.reflections_total,
                "verifier_verified_total": self.verifier_verified_total,
                "verifier_warning_total": self.verifier_warning_total,
                "verifier_warning_rate_pct": warn_rate,
                "execution_completed_total": self.execution_completed_total,
                "business_trusted_total": self.business_trusted_total,
                "business_untrusted_total": self.business_untrusted_total,
                "business_trust_rate_pct": trust_rate,
                "by_business_status": dict(self._business_status_counts),
                "fast_tier_routed_total": self.fast_tier_routed_total,
                "slow_tier_routed_total": self.slow_tier_routed_total,
                "parallel_tool_batches_total": self.parallel_tool_batches_total,
                "parallel_tool_saved_ms_total": self.parallel_tool_saved_ms_total,
                "models_used": dict(self._models_used),
            }

    def render_prometheus_lines(self) -> list[str]:
        s = self.summary()
        return [
            "# HELP laowadi_llm_calls_total Total LLM inference calls.",
            "# TYPE laowadi_llm_calls_total counter",
            f"laowadi_llm_calls_total {s['llm_calls_total']}",
            "# HELP laowadi_llm_failovers_total Total multi-model failovers triggered.",
            "# TYPE laowadi_llm_failovers_total counter",
            f"laowadi_llm_failovers_total {s['llm_failovers_total']}",
            "# HELP laowadi_llm_ttft_ms_avg Average time-to-first-token in milliseconds.",
            "# TYPE laowadi_llm_ttft_ms_avg gauge",
            f"laowadi_llm_ttft_ms_avg {s['ttft_ms_avg']}",
            "# HELP laowadi_llm_tokens_total Total LLM prompt and completion tokens.",
            "# TYPE laowadi_llm_tokens_total counter",
            f"laowadi_llm_tokens_total {s['total_tokens']}",
            "# HELP laowadi_llm_cost_cny_total Estimated cumulative LLM inference cost in CNY.",
            "# TYPE laowadi_llm_cost_cny_total counter",
            f"laowadi_llm_cost_cny_total {s['estimated_cost_cny_total']}",
            "# HELP laowadi_llm_reflection_triggered_total Total Auditor Reflection self-correction rounds.",
            "# TYPE laowadi_llm_reflection_triggered_total counter",
            f"laowadi_llm_reflection_triggered_total {s['reflections_triggered_total']}",
            "# HELP laowadi_verifier_warning_total Total diagnostic outputs blocked with verification_warning.",
            "# TYPE laowadi_verifier_warning_total counter",
            f"laowadi_verifier_warning_total {s['verifier_warning_total']}",
            "# HELP laowadi_diagnosis_business_trusted_total Diagnostic outcomes verified and business-trusted.",
            "# TYPE laowadi_diagnosis_business_trusted_total counter",
            f"laowadi_diagnosis_business_trusted_total {s['business_trusted_total']}",
            "# HELP laowadi_diagnosis_business_untrusted_total Completed diagnostic runs not business-trusted (warning/refused/insufficient).",
            "# TYPE laowadi_diagnosis_business_untrusted_total counter",
            f"laowadi_diagnosis_business_untrusted_total {s['business_untrusted_total']}",
            "# HELP laowadi_diagnosis_business_trust_rate_pct Share of completed diagnoses that passed business verification.",
            "# TYPE laowadi_diagnosis_business_trust_rate_pct gauge",
            f"laowadi_diagnosis_business_trust_rate_pct {s['business_trust_rate_pct']}",
            "# HELP laowadi_fast_tier_routed_total Queries short-circuited via fast deterministic tier.",
            "# TYPE laowadi_fast_tier_routed_total counter",
            f"laowadi_fast_tier_routed_total {s['fast_tier_routed_total']}",
            "# HELP laowadi_parallel_tool_batches_total Multi-tool batches executed in parallel.",
            "# TYPE laowadi_parallel_tool_batches_total counter",
            f"laowadi_parallel_tool_batches_total {s['parallel_tool_batches_total']}",
        ]


GLOBAL_LLMOPS = LLMOpsRegistry()
