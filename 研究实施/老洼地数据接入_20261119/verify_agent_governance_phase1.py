"""Offline verification suite for Phase-1 Agent governance upgrades:
1. Memory expiration (`expires_at`), conflict detection & superseding, ticket experience distillation.
2. Proactive root-cause & topology-time incident clustering (`incident_group_id`, `list_incident_groups`).
3. Operator feedback store (`laowadi/feedback_store.py`) & `agentops_negative_eval_v1` export.
4. Step/tool-level LLMOps telemetry (`laowadi/llmops.py`) and Prometheus export.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
import tempfile

from laowadi.diag_tools import DiagnosticToolbox
from laowadi.feedback_store import FeedbackStore
from laowadi.llmops import GLOBAL_LLMOPS, LLMOpsRegistry, execute_tool_calls_parallel
from laowadi.memory_store import (
    MemoryStore,
    distill_ticket_memory_candidate,
    extract_custom_voltage_thresholds,
    format_memories_for_prompt,
)
from laowadi.proactive import IncidentTicketStore, ProactiveWatcherDaemon
from laowadi.queries import Archive
from laowadi.storage import DB, ROOT, connect


EXPECTED_DB_SHA = {
    "laowadi.sqlite3": "37946d8abb40bd34295b7787c3426cc3e19a0ece9c9df70e45481f2253fb573a",
    "laowadi_events.sqlite3": "99373c36ce28939905d12a93714e05ca886ea8068cd31690fd33ea2d2a5c8d05",
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def test_memory_governance(tmp: Path) -> None:
    db_path = tmp / "mem.sqlite3"
    # Simulate legacy SQLite schema without expires_at column to verify migration
    legacy = sqlite3.connect(str(db_path))
    legacy.execute(
        """
        CREATE TABLE operational_memory (
            id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            scope_cabinet TEXT NOT NULL DEFAULT '',
            metric_threshold TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '[]',
            enabled INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'operator',
            hit_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    legacy.commit()
    legacy.close()

    ms = MemoryStore(db_path=db_path)

    # 1. Expiration handling
    exp_mem = ms.create(
        category="semantic",
        title="1006 临时检修电压红线 10.55 kV",
        content="1006 柜临时检修期间按 10.55 kV 预警",
        scope_cabinet="1006",
        metric_threshold={"metric": "Uab", "operator": ">", "value": 10.55, "unit": "kV"},
        expires_at="2020-01-01",
    )
    assert exp_mem["is_expired"] is True, exp_mem
    assert exp_mem["expires_at"] == "2020-01-01T23:59:59Z", exp_mem

    active_1006 = ms.create_with_report(
        category="semantic",
        title="1006 内部考核红线 10.65 kV",
        content="1006 柜母线按 10.65 kV 考核",
        scope_cabinet="1006",
        metric_threshold={"metric": "Uab", "operator": ">", "value": 10.65, "unit": "kV"},
        expires_at="2099-12-31",
    )
    assert active_1006["memory"]["is_expired"] is False
    assert active_1006["conflicts"] == [], "Expired rule must not trigger conflict"
    active_1006_mem = active_1006["memory"]

    enabled_list = ms.list(enabled_only=True)
    assert [m["id"] for m in enabled_list] == [active_1006_mem["id"]], enabled_list
    assert len(ms.list(include_expired=True)) == 2
    thrs = extract_custom_voltage_thresholds(ms.list(include_expired=True), cabinet="1006")
    assert len(thrs) == 1 and thrs[0]["value_kv"] == 10.65, thrs

    pruned = ms.prune_expired(disable_only=True)
    assert pruned["count"] == 1 and pruned["memory_ids"] == [exp_mem["id"]], pruned
    assert ms.get(exp_mem["id"])["enabled"] is False

    # 2. Conflict detection & superseding
    ms.create(
        category="semantic",
        title="1018 独立电压红线 10.60 kV",
        content="1018 柜专用 10.60 kV 红线",
        scope_cabinet="1018",
        metric_threshold={"metric": "Uab", "operator": ">", "value": 10.60, "unit": "kV"},
    )
    conflicts_1006 = ms.detect_conflicts(
        category="semantic",
        title="1006 调严红线 10.58 kV",
        content="1006 柜调整为 10.58 kV",
        scope_cabinet="1006",
        metric_threshold={"metric": "Uab", "operator": ">", "value": 10.58, "unit": "kV"},
    )
    assert len(conflicts_1006) == 1 and conflicts_1006[0]["conflict_type"] == "direct_conflict", conflicts_1006

    # Station-wide rule detects scope_overlap_conflict with specific cabinets
    station_rule = ms.create_with_report(
        category="semantic",
        title="全站 10 kV 母线红线 10.70 kV",
        content="全站 10 kV 按 10.70 kV 执行",
        scope_cabinet=None,
        metric_threshold={"metric": "Uab", "operator": ">", "value": 10.70, "unit": "kV"},
    )
    assert any(c["conflict_type"] == "scope_overlap_conflict" for c in station_rule["conflicts"]), station_rule
    station_rule_mem = station_rule["memory"]

    prompt_txt = format_memories_for_prompt([active_1006_mem, station_rule_mem])
    assert "[有效期至: 2099-12-31]" in prompt_txt, prompt_txt
    assert "[冲突提醒]" in prompt_txt, prompt_txt

    replaced_1006 = ms.create_with_report(
        category="semantic",
        title="1006 调严红线 10.58 kV",
        content="1006 柜收紧至 10.58 kV 取代旧红线",
        scope_cabinet="1006",
        metric_threshold={"metric": "Uab", "operator": ">", "value": 10.58, "unit": "kV"},
        supersede_conflicts=True,
    )
    assert active_1006_mem["id"] in replaced_1006["superseded_memory_ids"], replaced_1006
    assert ms.get(active_1006_mem["id"])["enabled"] is False
    assert ms.get(station_rule_mem["id"])["enabled"] is True, "scope_overlap must not be auto-disabled"

    # 3. Ticket experience distillation
    ticket_sample = {
        "ticket_id": "TKT-DEMO1018",
        "source_finding_id": "HLTH-MTR-1018",
        "cabinet": "1018",
        "at_time": "2026-11-16 21:00:00",
        "status": "disputed",
        "title": "1018 柜持续负有功与负功率因数",
        "resolution_note": "现场核实 1018 柜二次电流互感器 P1/P2 极性反接，非回馈倒送",
        "evidence_cells": ["1116.xls / New Sheet1!BX24"],
    }
    assert distill_ticket_memory_candidate({**ticket_sample, "status": "open", "resolution_note": ""}) is None
    cand = ms.distill_ticket(ticket_sample)
    assert cand is not None
    assert cand["category"] == "episodic"
    assert cand["scope_cabinet"] == "1018"
    assert cand["source"] == "ticket_distill"
    assert "ticket:TKT-DEMO1018" in cand["tags"]

    saved_cand = ms.create(
        category=cand["category"],
        title=cand["title"],
        content=cand["content"],
        scope_cabinet=cand["scope_cabinet"],
        tags=cand["tags"],
        source=cand["source"],
    )
    assert saved_cand["id"]
    assert ms.distill_ticket(ticket_sample) is None, "Must deduplicate already saved ticket memory"
    print("PASS test_memory_governance")


def test_proactive_incident_correlation(tmp: Path) -> None:
    db_path = tmp / "proactive.sqlite3"
    store = IncidentTicketStore(path=db_path)

    @contextmanager
    def _arc_ctx():
        with closing(connect(DB)) as conn:
            yield Archive(conn)

    daemon = ProactiveWatcherDaemon(_arc_ctx, store)
    res = daemon.scan_once(max_new_tickets=20, channels=[{"channel": "in_app", "target_url": ""}])
    assert res["created_count"] >= 10, res
    corr = res["correlation"]
    assert corr["composite_groups"] >= 1 and corr["correlated_tickets"] >= 4, corr

    tickets = store.list_tickets(status="active", limit=100)
    by_fid = {t["source_finding_id"]: t for t in tickets}
    assert "HLTH-MODE-SINGLE-01" in by_fid, list(by_fid)
    root_t = by_fid["HLTH-MODE-SINGLE-01"]
    assert root_t["correlation_role"] == "root", root_t
    assert root_t["revision"] == 1, root_t

    gid_1113 = root_t["incident_group_id"]
    assert gid_1113.startswith("INC-20261113-"), gid_1113
    for linked_fid in ("HLTH-BASE-01", "HLTH-BASE-02", "HLTH-BASE-03", "HLTH-BASE-04", "HLTH-PQ-VOLT-10KV-BUS2"):
        assert linked_fid in by_fid, linked_fid
        assert by_fid[linked_fid]["incident_group_id"] == gid_1113, (linked_fid, by_fid[linked_fid])
        assert by_fid[linked_fid]["correlation_role"] == "correlated", (linked_fid, by_fid[linked_fid])
        assert by_fid[linked_fid]["revision"] == 1, by_fid[linked_fid]

    # Separate days/feeders stay independent
    assert by_fid["HLTH-BAL-REV-20261112-12"]["incident_group_id"] != gid_1113
    assert by_fid["HLTH-BAL-REV-20261112-12"]["correlation_role"] == "standalone"
    assert by_fid["HLTH-MTR-1018"]["incident_group_id"] != gid_1113
    assert by_fid["HLTH-MTR-1018"]["correlation_role"] == "standalone"

    groups = store.list_incident_groups(status="active")
    comp = [g for g in groups if g["incident_group_id"] == gid_1113]
    assert len(comp) == 1 and comp[0]["is_composite"] is True and comp[0]["ticket_count"] >= 6, comp
    sla = store.sla_metrics()
    assert sla["composite_incident_groups_active"] >= 1
    assert sla["correlated_tickets_active"] >= 5

    # Idempotent second pass
    corr2 = store.correlate_active_tickets()
    assert corr2["updated_tickets"] == 0, corr2
    print("PASS test_proactive_incident_correlation")


def test_feedback_and_llmops(tmp: Path) -> None:
    before_calls = GLOBAL_LLMOPS.tool_calls_total
    fb_store = FeedbackStore(db_path=tmp / "feedback.sqlite3")

    f1 = fb_store.record(
        target_type="diagnosis",
        verdict="helpful",
        target_id="TURN-1",
        question="303 柜 11-13 15:00 有功是多少？",
        mode="strict",
        cabinet="303",
        at_time="2026-11-13 15:00:00",
        latency_ms=45.0,
        verification_status="verified",
    )
    f2 = fb_store.record(
        target_type="autonomous_turn",
        verdict="incorrect",
        target_id="TURN-2",
        question="1018 柜极性怎么回事？",
        answer_excerpt="正负弄反",
        expected_correction="应为 -16.88 kW，禁止写作 +16.88 kW",
        reason_tags=["sign_flip", "polarity"],
        mode="autonomous",
        cabinet="1018",
        latency_ms=1200.0,
        total_tokens=4200,
        verification_status="failed",
    )
    f3 = fb_store.record(
        target_type="action_ticket",
        verdict="rejected_ticket",
        target_id="ACT-99",
        question="纯查数误触发票据",
        reason_tags=["false_ticket", "polarity"],
    )
    assert f1["is_negative"] is False and f2["is_negative"] is True and f3["is_negative"] is True

    neg_rows = fb_store.list(negative_only=True)
    assert len(neg_rows) == 2
    summ = fb_store.summary()
    assert summ["total"] == 3 and summ["helpful_count"] == 1 and summ["negative_count"] == 2, summ
    assert summ["unexported_negative_count"] == 2
    assert summ["top_reason_tags"][0]["tag"] == "polarity" and summ["top_reason_tags"][0]["count"] == 2, summ

    exp = fb_store.export_negative_cases(mark_exported=True)
    assert exp["schema"] == "agentops_negative_eval_v1" and exp["count"] == 2 and exp["marked_exported"] is True
    assert fb_store.summary()["unexported_negative_count"] == 0

    # Tool-level execution hooks automatically into GLOBAL_LLMOPS
    with closing(connect(DB)) as conn:
        arc = Archive(conn)
        tb = DiagnosticToolbox(arc)
        execute_tool_calls_parallel(
            arc,
            tb,
            [
                {"tool_call_id": "c1", "name": "get_cabinet_health_profile", "arguments": {"cabinet": "303"}},
                {"tool_call_id": "c2", "name": "get_cabinet_health_profile", "arguments": {"cabinet": "306"}},
            ],
        )
    after_summary = GLOBAL_LLMOPS.summary()
    assert after_summary["tool_calls_total"] >= before_calls + 2, after_summary
    assert after_summary["tool_metrics"]["get_cabinet_health_profile"]["calls"] >= 2

    reg = LLMOpsRegistry()
    reg.record_tool_execution(tool_name="run_ac_power_flow", status="ok", elapsed_ms=12.5)
    reg.record_tool_execution(tool_name="run_ac_power_flow", status="error", elapsed_ms=4.0, error="diverged")
    reg.record_step_trace(mode="autonomous", stage="self_repair", latency_ms=210.0, tokens=900, cost_cny=0.004, tool_count=1)
    reg.record_operator_feedback(verdict="helpful", target_type="diagnosis")
    reg.record_operator_feedback(verdict="incorrect", target_type="autonomous_turn")
    rs = reg.summary()
    assert rs["tool_calls_total"] == 2 and rs["tool_errors_total"] == 1
    assert rs["tool_metrics"]["run_ac_power_flow"]["error_rate_pct"] == 50.0
    assert rs["step_metrics_by_stage"]["self_repair"]["count"] == 1
    assert len(rs["recent_steps"]) == 1
    assert rs["operator_feedback"]["total"] == 2 and rs["operator_feedback"]["negative"] == 1
    prom = "\n".join(reg.render_prometheus_lines())
    for metric in (
        "laowadi_tool_calls_total 2",
        "laowadi_tool_errors_total 1",
        "laowadi_operator_feedback_total 2",
        "laowadi_operator_feedback_negative_total 1",
    ):
        assert metric in prom, prom
    print("PASS test_feedback_and_llmops")


def test_golden_hashes() -> None:
    for name, expected in EXPECTED_DB_SHA.items():
        actual = _sha256(ROOT / "local" / name)
        assert actual == expected, f"{name} mutated: {actual} != {expected}"
    print("PASS test_golden_hashes")


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_memory_governance(tmp)
        test_proactive_incident_correlation(tmp)
        test_feedback_and_llmops(tmp)
    test_golden_hashes()
    print(f"PASS verify_agent_governance_phase1.py @ {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")


if __name__ == "__main__":
    main()
