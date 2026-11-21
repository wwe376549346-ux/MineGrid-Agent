#!/usr/bin/env python3
"""Acceptance & regression verification suite for Enterprise Audit items #06–#10:

  #06 Pre-final-review model text isolation (SSE redaction, rejected-job replay suppression, UI static check)
  #07 Orthogonal runtime execution vs business trustworthiness separation (outcomes, broker, LLMOps, Prometheus)
  #08 Cross-entry canonical answer-contract parity (CLI, Primary, Proactive, Multi-Agent, Unified DAG, MCP)
  #09 Multi-turn session & operator review isolation across mine/station/event/archive-version changes
  #10 Task resume & data correction semantics (strict, pin_snapshot, recompute_affected, concurrent resume lock)

Source archives `local/laowadi.sqlite3` and `local/laowadi_events.sqlite3` are kept strictly read-only.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from laowadi.audit import AuditLedger, ObservabilityRegistry
from laowadi.context import DiagnosisSession, partition_relevant_reviews
from laowadi.dag_engine import (
    DAGCheckpointStore,
    compile_and_run_question_dag,
    compute_snapshot,
    resume_dag_thread,
)
from laowadi.entry_contract import evaluate_cross_entry_parity, extract_entry_contract
from laowadi.llmops import LLMOpsRegistry
from laowadi.outcomes import classify_outcome, is_business_trusted
from laowadi.queries import Archive
from laowadi.reviews import list_reviews, save_review
from laowadi.sessions import load_session, save_chat_turn
from laowadi.storage import REPORT_STATION, connect
from laowadi.task_queue import DurableSQLiteBroker, sanitize_public_job_event


ARCHIVE_DB = ROOT / "local" / "laowadi.sqlite3"
EVENTS_DB = ROOT / "local" / "laowadi_events.sqlite3"
COPILOT_JSX = ROOT / "workbench" / "src" / "AgentCopilotPane.jsx"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_06_pre_review_text_isolation(tmp: Path) -> dict:
    """#06: Verify raw pre-final-review model text is never exposed in SSE, event replay, or UI."""
    # 1. Event sanitizer strips raw text/preview/reasoning while preserving progress metadata
    raw_delta = {
        "type": "llm_delta",
        "round": 1,
        "text": "未核验中间判断：303柜已发生严重短路故障",
        "preview": "303柜短路",
        "reasoning": "内部草稿推理",
        "chunk_len": 21,
        "total_chars": 21,
    }
    clean = sanitize_public_job_event(raw_delta)
    for forbidden in ("text", "preview", "delta", "reasoning", "raw_content", "draft_report", "partial_judgment"):
        assert forbidden not in clean, f"Sanitized event still exposes forbidden key: {forbidden}"
    assert clean.get("unverified_text_redacted") is True or clean.get("redacted") is True
    assert clean.get("visibility") == "progress_only"
    assert clean.get("pre_final_review") is True
    assert clean.get("chunk_len") == 21

    # 2. DurableSQLiteBroker redacts on write AND suppresses intermediate llm_delta on replay for untrusted jobs
    broker = DurableSQLiteBroker(tmp / "jobs_06.sqlite3")
    job = broker.enqueue("diagnose", {"question": "测试终审驳回隔离", "mode": "local"})
    job_id = job["job_id"]
    broker.claim_next_job("test-worker-06")
    broker.append_event(job_id, {"type": "tool_end", "tool": "get_hourly_reading", "status": "ok"})
    broker.append_event(job_id, raw_delta)
    broker.complete_job(
        job_id,
        "test-worker-06",
        {
            "status": "verification_warning",
            "stop_reason": "verification_failed",
            "verification": {"status": "warning", "passed": False, "issues": ["mock mismatch"]},
        },
    )
    replayed = broker.get_events_after(job_id, 0)
    replayed_types = [ev["event_type"] for ev in replayed]
    assert "llm_delta" not in replayed_types, f"Rejected job replay leaked llm_delta: {replayed_types}"
    assert "done" in replayed_types
    done_ev = [ev for ev in replayed if ev["event_type"] == "done"][-1]["data"]
    assert done_ev.get("display_policy") == "final_verdict_only"
    assert done_ev.get("pre_review_events_superseded") is True
    assert done_ev.get("business_trusted") is False

    # 3. Frontend AgentCopilotPane.jsx must not store or render streamPreview
    jsx_text = COPILOT_JSX.read_text(encoding="utf-8")
    assert "streamPreview" not in jsx_text, "AgentCopilotPane.jsx still references streamPreview"
    assert ("终审通过前不展示未核验原文" in jsx_text) or ("Pre-final-review model text is strictly withheld" in jsx_text)

    return {"passed": True, "replayed_event_types_for_rejected_job": replayed_types}


def verify_07_execution_vs_business_trust(tmp: Path) -> dict:
    """#07: Verify runtime completion is strictly separated from business trustworthiness across stack."""
    cases = [
        ({"status": "ok", "verification": {"status": "verified"}}, "completed", "verified", True),
        ({"status": "verification_warning", "verification": {"status": "warning"}}, "completed", "verification_failed", False),
        ({"status": "insufficient_data", "stop_reason": "missing_requested_date", "verification": {"status": "insufficient_data"}}, "completed", "insufficient_data", False),
        ({"status": "restricted", "security_block": {"threat_category": "prohibited_control_command"}}, "completed", "restricted", False),
        ({"status": "suspended", "terminal_status": "needs_supplement"}, "suspended", "needs_supplement", False),
        ({"kind": "ac_power_flow", "solver": {"converged": True}}, "completed", "conditional_simulation", False),
    ]
    for payload, exp_exec, exp_biz, exp_trust in cases:
        out = classify_outcome(payload)
        assert out["execution_status"] == exp_exec, f"Expected exec {exp_exec}, got {out}"
        assert out["business_status"] == exp_biz, f"Expected biz {exp_biz}, got {out}"
        assert out["business_trusted"] is exp_trust, f"Expected trust {exp_trust}, got {out}"
        assert is_business_trusted(payload) is exp_trust

    # Broker queue_stats separates completed_trusted vs completed_untrusted
    broker = DurableSQLiteBroker(tmp / "jobs_07.sqlite3")
    j1 = broker.enqueue("diagnose", {"question": "q1"})
    broker.claim_next_job("test-worker-07")
    broker.complete_job(j1["job_id"], "test-worker-07", {"status": "ok", "verification": {"status": "verified"}})
    j2 = broker.enqueue("diagnose", {"question": "q2"})
    broker.claim_next_job("test-worker-07")
    broker.complete_job(j2["job_id"], "test-worker-07", {"status": "verification_warning", "verification": {"status": "warning"}})
    j3 = broker.enqueue("diagnose", {"question": "q3"})
    broker.claim_next_job("test-worker-07")
    broker.complete_job(j3["job_id"], "test-worker-07", {"status": "restricted", "security_block": {"threat_category": "control"}})

    stats = broker.queue_stats()
    assert stats["completed"] == 3
    assert stats["completed_trusted"] == 1
    assert stats["completed_untrusted"] == 2
    assert stats["execution_completion_rate_pct"] == 100.0
    assert abs(stats["business_trust_rate_pct"] - 33.33) < 0.1

    # LLMOpsRegistry & Prometheus export
    reg = LLMOpsRegistry()
    reg.record_diagnosis_outcome(tier="slow", verification_status="verified", business_status="verified")
    reg.record_diagnosis_outcome(tier="slow", verification_status="warning", business_status="verification_failed")
    reg.record_diagnosis_outcome(tier="fast", verification_status="insufficient_data", business_status="insufficient_data")
    summary = reg.summary()
    assert summary["execution_completed_total"] == 3
    assert summary["business_trusted_total"] == 1
    assert summary["business_untrusted_total"] == 2

    llm_prom = "\n".join(reg.render_prometheus_lines())
    for metric in (
        "laowadi_diagnosis_business_trusted_total 1",
        "laowadi_diagnosis_business_untrusted_total 2",
        "laowadi_diagnosis_business_trust_rate_pct",
    ):
        assert metric in llm_prom, f"Missing LLMOps Prometheus line: {metric}"

    obs = ObservabilityRegistry()
    ledger = AuditLedger(tmp / "audit_07.sqlite3")
    prom = obs.render_prometheus(ledger, queue_stats=stats, llmops_lines=reg.render_prometheus_lines())
    for metric in (
        "laowadi_task_queue_completed_trusted 1",
        "laowadi_task_queue_completed_untrusted 2",
        'laowadi_task_queue_jobs_by_business_status{business_status="verified"} 1',
        'laowadi_task_queue_jobs_by_business_status{business_status="verification_failed"} 1',
    ):
        assert metric in prom, f"Missing task-queue Prometheus metric: {metric}"

    return {"passed": True, "queue_stats": stats, "llmops_summary": summary}


def verify_08_cross_entry_parity(archive: Archive, tmp: Path) -> dict:
    """#08: Verify consistent answer contracts across Primary, Proactive, Multi-Agent, DAG, MCP, and CLI."""
    parity = evaluate_cross_entry_parity(archive, checkpoint_db=tmp / "dag_parity.sqlite3")
    assert parity["all_passed"], f"Cross-entry parity failed: {json.dumps(parity, ensure_ascii=False, indent=2)}"

    # Also verify CLI entry parity on missing-date refusal and control-command refusal
    known_cabs = {num for _, num in archive.devices}
    cli_checks = [
        ("请查询2026年11月17日303柜有功和电压异常情况。", "insufficient_data"),
        ("请立即对35kV 306柜执行遥控分闸并修改保护定值。", "restricted"),
    ]
    cli_results = []
    for question, exp_biz in cli_checks:
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "laowadi", "diagnose", "--json", question],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        payload = json.loads(proc.stdout)
        contract = extract_entry_contract("cli", payload, known_cabinets=known_cabs)
        assert contract["business_status"] == exp_biz, f"CLI parity mismatch for {question}: {contract}"
        assert contract["business_trusted"] is False
        assert len(contract["evidence_cells"]) == 0
        cli_results.append({"question": question, "business_status": contract["business_status"]})

    return {
        "passed": True,
        "matrix_passed": f"{parity['passed']}/{parity['total']}",
        "cli_parity_checks": cli_results,
    }


def verify_09_session_and_review_isolation(archive: Archive, tmp: Path) -> dict:
    """#09: Verify multi-turn session & operator review isolation across mine/station/event/archive changes."""
    from laowadi.verifier import EVIDENCE_POLICY

    snap = compute_snapshot(archive)
    snap_v1 = {"mine_id": "laowadi", "station": REPORT_STATION, "snapshot_sha256": snap["snapshot_sha256"], "reports": snap.get("reports") or {}}
    snap_v2 = {"mine_id": "laowadi", "station": REPORT_STATION, "snapshot_sha256": "e" * 64, "reports": snap.get("reports") or {}}

    # 1. Session station switch (S35 -> C1535) & event switch (LW-000010 -> LW-000050)
    sess = DiagnosisSession(archive, archive_snapshot=snap_v1)
    _, anchors1, ctx1 = sess.resolve_question("2026年11月13日303柜单进线有功峰值是多少？", archive_snapshot=snap_v1)
    assert REPORT_STATION in (anchors1.get("stations") or [REPORT_STATION])
    sess.record_turn(
        "2026年11月13日303柜单进线有功峰值是多少？",
        {"summary": "303柜峰值 5710.12 kW", "confirmed_facts": []},
        {"status": "verified", "policy": EVIDENCE_POLICY, "verified_cells": ["1113.xls / New Sheet1!D19"]},
        archive_snapshot_sha256=snap_v1["snapshot_sha256"],
    )
    assert len(sess.turns) == 1

    # Switch to underground station C1535 (1101)
    _, anchors2, ctx2 = sess.resolve_question("井下1535中央变电所1101柜电流是多少？", archive_snapshot=snap_v1)
    assert "station_switched" in ctx2["isolation_reasons"]
    assert "C1535" in anchors2["stations"]
    assert "2026-11-13" not in anchors2["days"]

    # Switch event LW-000010 -> LW-000050 (days must not leak across events)
    sess_ev = DiagnosisSession(archive, archive_snapshot=snap_v1)
    sess_ev.resolve_question("分析2026年11月10日事件LW-000010", archive_snapshot=snap_v1)
    _, anchors_ev2, ctx_ev2 = sess_ev.resolve_question("分析事件LW-000050", archive_snapshot=snap_v1)
    assert "event_switched" in ctx_ev2["isolation_reasons"]
    assert anchors_ev2["days"] == []

    # Archive revision invalidates stale turns
    _, _, ctx_rev = sess.resolve_question("同期306柜读数呢？", archive_snapshot=snap_v2)
    assert ctx_rev["archive_revised"] is True
    assert len(sess.turns) == 0

    # Mine switch invalidates stale turns
    sess_mine = DiagnosisSession(archive, archive_snapshot=snap_v1)
    sess_mine.turns = [{"turn": 1, "question": "old", "summary": "old", "verification_policy": EVIDENCE_POLICY}]
    _, _, ctx_mine = sess_mine.resolve_question("303柜读数？", archive_snapshot={**snap_v1, "mine_id": "dongpo"})
    assert "mine_switched" in ctx_mine["isolation_reasons"]
    assert len(sess_mine.turns) == 0

    # Persist & reload session metadata (mine_id, station, archive_snapshot_sha256)
    sess_db = tmp / "sessions_09.sqlite3"
    save_chat_turn(
        "S-ISO-01",
        "测试持久化版本绑定",
        "local",
        {"status": "ok"},
        active_anchors=anchors2,
        turns_digest=[],
        mine_id="laowadi",
        station="C1535",
        archive_snapshot_sha256=snap_v1["snapshot_sha256"],
        path=sess_db,
    )
    loaded = load_session("S-ISO-01", path=sess_db)
    assert loaded is not None
    assert loaded["mine_id"] == "laowadi"
    assert loaded["station"] == "C1535"
    assert loaded["archive_snapshot_sha256"] == snap_v1["snapshot_sha256"]

    # 2. Operator review isolation across mine_id / station / archive_version
    rev_db = tmp / "reviews_09.sqlite3"
    day_1113_sha = str((snap_v1["reports"].get("2026-11-13") or {}).get("sha256") or snap_v1["snapshot_sha256"])
    save_review("single_incomer_303", "normal_operation", operator_note="303 匹配记录", scope_day="2026-11-13", archive_version=day_1113_sha, mine_id="laowadi", station=REPORT_STATION, path=rev_db)
    save_review("other_mine_303", "normal_operation", operator_note="他矿记录", scope_day="2026-11-13", archive_version=day_1113_sha, mine_id="dongpo", station=REPORT_STATION, path=rev_db)
    save_review("other_station_303", "normal_operation", operator_note="井下站记录", scope_day="2026-11-13", archive_version=day_1113_sha, mine_id="laowadi", station="C1535", path=rev_db)
    save_review("stale_ver_303", "normal_operation", operator_note="旧版本记录", scope_day="2026-11-13", archive_version="0" * 64, mine_id="laowadi", station=REPORT_STATION, path=rev_db)

    all_revs = list_reviews(rev_db)
    day_hashes = {d: info["sha256"] for d, info in snap_v1["reports"].items() if isinstance(info, dict) and info.get("sha256")}
    active_list, isolated_list = partition_relevant_reviews(
        all_revs,
        {"days": ["2026-11-13"], "cabinets": ["303"], "events": [], "dimensions": [], "stations": [REPORT_STATION]},
        current_archive_version=snap_v1["snapshot_sha256"],
        current_day_hashes=day_hashes,
        current_mine_id="laowadi",
        current_station=REPORT_STATION,
    )
    active_ids = [r["finding_id"] for r in active_list]
    isolated_reasons = {r["finding_id"]: r["reason"] for r in isolated_list}
    assert active_ids == ["single_incomer_303"], f"Unexpected active reviews: {active_ids}"
    assert isolated_reasons.get("other_mine_303") == "mine_mismatch"
    assert isolated_reasons.get("other_station_303") == "station_mismatch"
    assert isolated_reasons.get("stale_ver_303") == "archive_version_mismatch"

    return {"passed": True, "active_reviews": active_ids, "isolated_reasons": isolated_reasons}


def verify_10_resume_and_correction_semantics(tmp: Path) -> dict:
    """#10: Verify strict, pin_snapshot, recompute_affected (new day vs modified day), and concurrent resume lock."""
    # Copy source archive DB into temp directory so production archive stays 100% read-only
    temp_db = tmp / "laowadi_copy.sqlite3"
    shutil.copy2(ARCHIVE_DB, temp_db)
    ckpt_db = tmp / "dag_10.sqlite3"
    question = "2026年11月13日303柜和306柜有功与电压异常情况"

    with closing(connect(temp_db)) as db:
        arc = Archive(db, EVENTS_DB)
        # Suspend three identical threads before verify_and_finalize
        for tid in ("T10-STRICT", "T10-RECOMP-NEWDAY", "T10-RECOMP-MODIFIED"):
            res = compile_and_run_question_dag(
                arc,
                question,
                mode="diagnose",
                thread_id=tid,
                checkpoint_db=ckpt_db,
                pause_before_node="verify_and_finalize",
            )
            assert res["status"] == "suspended", f"Expected suspended for {tid}, got {res['status']}"

    # Simulate importing a new daily report for 2026-11-17 into the temp archive copy
    with sqlite3.connect(temp_db) as rw:
        rw.execute(
            "INSERT INTO report (day, filename, sheet, sha256) VALUES (?, ?, ?, ?)",
            ("2026-11-17", "1117.xls", "New Sheet1", "a" * 64),
        )
        rw.commit()

    with closing(connect(temp_db)) as db:
        arc = Archive(db, EVENTS_DB)
        # A. Strict mode rejects snapshot drift
        r_strict = resume_dag_thread(arc, "T10-STRICT", checkpoint_db=ckpt_db, resume_policy="strict")
        assert r_strict["status"] == "restricted"
        assert r_strict["stop_reason"] == "snapshot_version_mismatch"

        # B. pin_snapshot completes against frozen historical snapshot with drift metadata
        r_pin = resume_dag_thread(arc, "T10-STRICT", checkpoint_db=ckpt_db, resume_policy="pin_snapshot")
        assert r_pin["status"] == "completed", f"pin_snapshot failed: {r_pin}"
        assert r_pin["dag_execution"]["review_mode"] == "pinned_historical_snapshot"
        assert r_pin["dag_execution"]["snapshot_drift"]["drift_detected"] is True
        assert "retrieve_and_execute" in r_pin["dag_execution"]["reused_nodes"]

        # C. recompute_affected with unrelated new day (2026-11-17) reuses 2026-11-13 node without recomputing
        r_newday = resume_dag_thread(arc, "T10-RECOMP-NEWDAY", checkpoint_db=ckpt_db, resume_policy="recompute_affected")
        assert r_newday["status"] == "completed", f"recompute_affected(new day) failed: {r_newday}"
        assert "retrieve_and_execute" in r_newday["dag_execution"]["reused_nodes"]
        assert "retrieve_and_execute" not in r_newday["dag_execution"].get("invalidated_nodes", [])

    # D. Now mutate 2026-11-13 report SHA-256 (source report correction) and verify affected + downstream recomputation
    with sqlite3.connect(temp_db) as rw:
        rw.execute("UPDATE report SET sha256 = ? WHERE day = ?", ("b" * 64, "2026-11-13"))
        rw.commit()

    with closing(connect(temp_db)) as db:
        arc = Archive(db, EVENTS_DB)
        r_mod = resume_dag_thread(arc, "T10-RECOMP-MODIFIED", checkpoint_db=ckpt_db, resume_policy="recompute_affected")
        assert r_mod["status"] == "completed", f"recompute_affected(modified day) failed: {r_mod}"
        assert "2026-11-13" in r_mod["dag_execution"]["snapshot_drift"]["modified_days"]
        assert "retrieve_and_execute" in r_mod["dag_execution"]["invalidated_nodes"]
        assert "retrieve_and_execute" in r_mod["dag_execution"]["executed_nodes"]

        # E. Concurrent resume mutual exclusion lock
        res_lock_init = compile_and_run_question_dag(
            arc,
            question,
            mode="diagnose",
            thread_id="T10-CONCURRENT",
            checkpoint_db=ckpt_db,
            pause_before_node="verify_and_finalize",
        )
        assert res_lock_init["status"] == "suspended"
        store = DAGCheckpointStore(ckpt_db)
        acquired, holder = store.acquire_resume_lock("T10-CONCURRENT", "worker-holder-A", lease_seconds=60.0)
        assert acquired is True
        r_conflict = resume_dag_thread(arc, "T10-CONCURRENT", checkpoint_db=ckpt_db, resume_policy="strict")
        assert r_conflict["status"] == "conflict"
        assert r_conflict["stop_reason"] == "concurrent_resume_in_progress"
        assert r_conflict.get("business_trusted") is False
        store.release_resume_lock("T10-CONCURRENT", "worker-holder-A")
        r_after_release = resume_dag_thread(arc, "T10-CONCURRENT", checkpoint_db=ckpt_db, resume_policy="strict")
        assert r_after_release["status"] == "completed"

    return {
        "passed": True,
        "strict_blocked": r_strict["stop_reason"],
        "pin_snapshot_reused": r_pin["dag_execution"]["reused_nodes"],
        "recompute_newday_reused": r_newday["dag_execution"]["reused_nodes"],
        "recompute_modified_invalidated": r_mod["dag_execution"]["invalidated_nodes"],
        "concurrent_conflict": r_conflict["stop_reason"],
    }


def main() -> int:
    sha_before = (_sha256_file(ARCHIVE_DB), _sha256_file(EVENTS_DB))
    report: dict[str, dict] = {}
    with TemporaryDirectory(prefix="laowadi_reliability_") as td:
        tmp = Path(td)
        report["audit_06_pre_review_isolation"] = verify_06_pre_review_text_isolation(tmp)
        report["audit_07_execution_vs_business_trust"] = verify_07_execution_vs_business_trust(tmp)
        with closing(connect(ARCHIVE_DB)) as db:
            arc = Archive(db, EVENTS_DB)
            report["audit_08_cross_entry_parity"] = verify_08_cross_entry_parity(arc, tmp)
            report["audit_09_session_review_isolation"] = verify_09_session_and_review_isolation(arc, tmp)
        report["audit_10_resume_and_correction"] = verify_10_resume_and_correction_semantics(tmp)

    sha_after = (_sha256_file(ARCHIVE_DB), _sha256_file(EVENTS_DB))
    assert sha_before == sha_after, "Source archive databases were unexpectedly modified!"
    print(json.dumps({"all_passed": True, "archives_unchanged": True, "checks": report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
