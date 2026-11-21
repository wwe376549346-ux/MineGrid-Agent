"""Unified orthogonal status classification separating runtime execution from business trustworthiness.

Addresses Audit #07:
- Runtime execution completion (`completed` / HTTP 200) MUST NEVER be conflated with
  business trustworthiness (`business_trusted=True` ONLY when `business_status == "verified"`).
- Provides one canonical `classify_outcome()` used across `diagnose.py`, `multi_agent.py`,
  `graph.py`, `worker.py`, `task_queue.py`, `llmops.py`, and Prometheus `/metrics`.
"""

from __future__ import annotations

from typing import Any


BUSINESS_STATUSES = (
    "verified",
    "verification_failed",
    "insufficient_data",
    "partial",
    "clarification_required",
    "needs_supplement",
    "restricted",
    "conditional_simulation",
    "error",
)

EXECUTION_STATUSES = (
    "queued",
    "running",
    "completed",
    "suspended",
    "failed",
    "cancelled",
    "conflict",
)


def classify_outcome(result: dict[str, Any] | None, *, execution_status: str | None = None) -> dict[str, Any]:
    """Derive strictly separated execution, tool, verification, and business trust layers."""
    if not isinstance(result, dict):
        exec_st = execution_status or "failed"
        return {
            "execution_status": exec_st,
            "tool_status": "not_invoked",
            "verification_status": "none",
            "business_status": "error",
            "business_trusted": False,
            "review_signoff_status": "not_applicable",
            "stop_reason": "empty_result",
        }

    raw_status = str(result.get("status") or "").strip().lower()
    terminal_status = str(result.get("terminal_status") or "").strip().lower()
    stop_reason = str(result.get("stop_reason") or "").strip()
    ver = result.get("verification") if isinstance(result.get("verification"), dict) else {}
    ver_status = str(ver.get("status") or "").strip().lower() or "none"

    # 1. Execution layer (did the runtime finish, suspend, conflict, or error?)
    if execution_status:
        exec_st = execution_status
    elif raw_status in ("suspended",) or terminal_status == "needs_supplement":
        exec_st = "suspended"
    elif raw_status in ("conflict",) or stop_reason == "concurrent_resume_in_progress":
        exec_st = "conflict"
    elif raw_status in ("error", "failed"):
        exec_st = "failed"
    else:
        exec_st = "completed"

    # 2. Tool execution layer
    trace = result.get("tool_trace")
    if isinstance(trace, list) and trace:
        tool_errs = sum(
            1 for step in trace
            if isinstance(step, dict) and isinstance(step.get("result"), dict) and step["result"].get("status") == "error"
        )
        if tool_errs == 0:
            tool_st = "ok"
        elif tool_errs < len(trace):
            tool_st = "partial"
        else:
            tool_st = "error"
    elif result.get("kind") == "ac_power_flow" or "solver" in result:
        converged = bool((result.get("solver") or {}).get("converged"))
        tool_st = "ok" if converged else "error"
    else:
        tool_st = "not_invoked"

    task_status = (result.get("task_alignment") or {}).get("status")
    reactive_status = (result.get("reactive_arbitration") or {}).get("status")

    # 3. Business trustworthiness layer (STRICT: only verified electrical evidence is business_trusted=True)
    if result.get("security_block") or raw_status in ("restricted", "blocked") or terminal_status == "restricted" or ver_status == "restricted":
        biz_st = "restricted"
    elif raw_status == "suspended" or terminal_status == "needs_supplement":
        biz_st = "needs_supplement"
    elif raw_status == "error" or stop_reason == "model_error":
        biz_st = "error"
    elif raw_status == "verification_warning" or ver_status == "warning" or stop_reason == "verification_failed":
        biz_st = "verification_failed"
    elif task_status in ("partial", "insufficient_data", "clarification_required"):
        biz_st = task_status
    elif raw_status == "partial" and (result.get("report") or {}).get("hypothesis_trace"):
        biz_st = "partial"
    elif raw_status == "insufficient_data" or ver_status == "insufficient_data" or stop_reason == "missing_requested_date":
        biz_st = "insufficient_data"
    elif reactive_status in ("conditional_candidate", "no_feasible_candidate"):
        biz_st = "conditional_simulation"
    elif reactive_status and reactive_status != "no_low_pf":
        biz_st = "insufficient_data"
    elif result.get("kind") == "ac_power_flow" or ("solver" in result and "verification" not in result):
        converged = bool((result.get("solver") or {}).get("converged"))
        biz_st = "conditional_simulation" if converged else "error"
    elif ver_status == "verified":
        biz_st = "verified"
    elif terminal_status == "completed":
        # Audit graph completion: trusted only if no missing requirements and not conditional/unverified
        if result.get("missing_requirements"):
            biz_st = "needs_supplement"
        else:
            biz_st = "verified"
    else:
        biz_st = "verification_failed" if raw_status in ("partial",) else "error"

    trusted = biz_st == "verified"

    # 4. Human review / sign-off layer
    if biz_st in ("verified", "conditional_simulation", "needs_supplement"):
        signoff = "pending_human_review"
    else:
        signoff = "not_applicable"

    return {
        "execution_status": exec_st,
        "tool_status": tool_st,
        "verification_status": ver_status,
        "business_status": biz_st,
        "business_trusted": trusted,
        "review_signoff_status": signoff,
        "stop_reason": stop_reason,
    }


def is_business_trusted(result: dict[str, Any] | None) -> bool:
    """Return True strictly when the diagnostic/audit outcome is business-verified."""
    return bool(classify_outcome(result)["business_trusted"])
