"""P1 Verification Suite: Proactive Watcher, Transactional Outbox, Parallel Tools, Failover, LLMOps & Shift Tickets.

Verifies all 3 P1 workstreams end-to-end while keeping `local/laowadi.sqlite3` and `local/laowadi_events.sqlite3` read-only.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from laowadi.api import create_app
from laowadi.diagnose import diagnose_question
from laowadi.llm import FailoverChatClient, LLMError
from laowadi.llmops import GLOBAL_LLMOPS, classify_query_tier
from laowadi.monitor import EVENT_DB
from laowadi.proactive import IncidentTicketStore, ProactiveWatcherDaemon
from laowadi.queries import Archive
from laowadi.security import issue_token
from laowadi.storage import DB, connect
from laowadi.workbench import Workbench

from laowadi.diagnostic_claims import AUDIT_PROMPT
from verify_diagnostic_claims import audit_pass, diagnostic_draft

ROOT = Path(__file__).resolve().parent


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class _ParallelToolsStubClient:
    """Stub LLM issuing 3 simultaneous read-only tool calls in Round 1, then a grounded JSON report in Round 2."""

    model = "stub-parallel-llm"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None, tool_choice=None, max_tokens=2048, json_mode=False, on_delta=None):
        self.calls += 1
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_window_snapshot",
                            "arguments": json.dumps({"start_time": "2026-11-13 15:00:00", "end_time": "2026-11-13 15:00:00", "cabinets": ["303", "306"]}),
                        },
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {
                            "name": "get_cabinet_health_profile",
                            "arguments": json.dumps({"cabinet": "1018"}),
                        },
                    },
                    {
                        "id": "call_3",
                        "type": "function",
                        "function": {
                            "name": "search_coal_standards",
                            "arguments": json.dumps({"query": "电压偏差 10kV", "top_k": 2}),
                        },
                    },
                ],
            }, {"status": "completed", "ttft_ms": 42.5, "tpot_ms": 4.2, "usage": {"prompt_tokens": 300, "completion_tokens": 80}}

        rep = (audit_pass(messages) if messages[0]["content"] == AUDIT_PROMPT else
               diagnostic_draft("1113.xls / New Sheet1!D19", "1113.xls / New Sheet1!C19", "T2.1"))
        return {"role": "assistant", "content": json.dumps(rep, ensure_ascii=False)}, {
            "status": "completed",
            "ttft_ms": 55.0,
            "tpot_ms": 3.5,
            "usage": {"prompt_tokens": 520, "completion_tokens": 180},
        }


class _FailingModelClient:
    def __init__(self, name: str):
        self.model = name
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        raise LLMError(f"{self.model} 模拟 503 不可用")


class _HealthyBackupClient:
    model = "backup-qwen-72b"

    def chat(self, messages, **kwargs):
        return {"role": "assistant", "content": '{"ok": true}'}, {
            "status": "completed",
            "ttft_ms": 31.0,
            "tpot_ms": 2.1,
            "usage": {"prompt_tokens": 120, "completion_tokens": 20},
        }


def verify_01_proactive_watcher_and_outbox_retry() -> None:
    with TemporaryDirectory() as tmp:
        store = IncidentTicketStore(Path(tmp) / "proactive.sqlite3")

        def _arc_ctx():
            class _Ctx:
                def __enter__(self):
                    self.conn = connect(DB)
                    return Archive(self.conn, EVENT_DB)

                def __exit__(self, *args):
                    self.conn.close()

            return _Ctx()

        watcher = ProactiveWatcherDaemon(_arc_ctx, store, archive_version_fn=lambda: "v-test-1")
        scans = [watcher.scan_once(max_new_tickets=3) for _ in range(6)]
        assert [s["created_count"] for s in scans] == [3, 3, 3, 3, 2, 0]
        assert all(s["status"] == "ok" and s["dispatch"]["delivered"] == s["created_count"] for s in scans)
        assert scans[-1]["skipped_existing"] == 14
        tickets = [t for s in scans for t in s["created_tickets"]]
        assert [t["severity"] for t in tickets] == ["high"] * 7 + ["medium"] * 7
        by_kind = {t["pre_diagnosis"]["kind"]: t for t in tickets}
        for ticket in tickets:
            pre = ticket["pre_diagnosis"]
            assert pre["summary"] and pre["pending_hypotheses"] and pre["missing_evidence"]
            assert pre["followup_question"] and ticket["at_time"][:16] in pre["followup_question"]
            assert set(ticket["evidence_cells"]) <= set(pre["evidence_cells"])
            assert len(pre["evidence_cells"]) == len(set(pre["evidence_cells"]))
            assert store.get_ticket_detail(ticket["ticket_id"])["pre_diagnosis"] == pre

        # The follow-up reaches the same hypothesis path through the normal chat entry point.
        with _arc_ctx() as archive:
            for kind, cases, source_cell, site_evidence in [
                ("reverse_power_review", {"H2"}, "1112.xls / New Sheet1!AF16", "母联遥信"),
                ("feeder_polarity_review", {"H2"}, "1116.xls / New Sheet1!AO25", "相位仪"),
                ("zero_incomer_review", {"H1", "H2"}, "1113.xls / New Sheet1!F11", "倒闸操作票"),
            ]:
                pre = by_kind[kind]["pre_diagnosis"]
                assert pre["verified"] == "verified"
                assert {check["hypothesis"] for check in pre["refuted_hypotheses"]} == cases
                assert all(check["title"] and "证伪" in check["judgment"] for check in pre["refuted_hypotheses"])
                assert source_cell in pre["evidence_cells"]
                assert any(site_evidence in item for item in pre["missing_evidence"])
                followup = diagnose_question(archive, pre["followup_question"], client=None)
                assert followup["report"]["hypothesis_trace"]["scenario"] == kind
        for kind in ("voltage_over_limit", "low_power_factor", "scada_polling_skew"):
            pre = by_kind[kind]["pre_diagnosis"]
            assert pre["verified"] == "pending_site_review" and pre["refuted_hypotheses"] == []

        # Astra #12: Outbox callback fails on first attempt, retries & recovers 100% on second attempt (0 lost!)
        tkt, created = store.create_ticket_with_outbox(
            source_finding_id="LW-000010",
            title="303 柜 11-10 12:00 负荷跃升 +3408.12 kW",
            summary="从 1071.84 kW 升至 4479.96 kW",
            cabinet="303",
            at_time="2026-11-10 12:00:00",
            severity="high",
            evidence_cells=["1110.xls / New Sheet1!D15", "1110.xls / New Sheet1!D16"],
            channels=[
                {"channel": "webhook", "target_url": "https://example.invalid/hook"},
                {"channel": "wecom", "target_url": "https://example.invalid/wecom"},
                {"channel": "dingtalk", "target_url": "https://example.invalid/ding"},
            ],
        )
        assert created is True

        attempt_state = {"fail": True, "received": []}

        def _flaky_sender(ch, url, payload):
            if attempt_state["fail"]:
                raise RuntimeError("simulated network timeout")
            attempt_state["received"].append((ch, url, payload))
            return {"ok": True, "http_status": 200}

        d1 = store.dispatch_outbox(sender=_flaky_sender)
        assert d1["failed"] == 3 and d1["delivered"] == 0
        # Second attempt succeeds -> 0 lost notifications!
        attempt_state["fail"] = False
        d2 = store.dispatch_outbox(sender=_flaky_sender, force_retry_now=True)
        assert d2["delivered"] == 3 and d2["failed"] == 0 and len(attempt_state["received"]) == 3
        det = store.get_ticket_detail(tkt["ticket_id"])
        assert all(o["status"] == "delivered" and len(o["receipt_sha256"]) >= 16 for o in det["outbox"])
        assert det["cmms_workorder"]["workorder_schema"] == "coal_power_cmms_v1"

        # Astra #13: Data correction supersedes stale active alert while preserving audit history
        rec = store.reconcile_correction(
            cabinet="303",
            at_time="2026-11-10 12:00:00",
            prev_p_kw=1071.84,
            curr_p_kw=1071.84,
            reason="更正帧将 12:00 读数修正为与 11:00 一致",
        )
        assert rec["still_triggered"] is False and tkt["ticket_id"] in rec["superseded_ticket_ids"]
        active_ids = {x["ticket_id"] for x in store.list_tickets(status="active")}
        assert tkt["ticket_id"] not in active_ids
        det_after = store.get_ticket_detail(tkt["ticket_id"])
        assert det_after["status"] == "superseded" and "更正帧" in det_after["superseded_reason"]
    print("[1/6] ProactiveWatcher + Outbox failure->retry 0-loss delivery + Astra #13 correction supersede OK")


def verify_02_parallel_tools_and_fast_slow_routing() -> None:
    assert classify_query_tier("12–13日哪天全站平均有功最高？")["tier"] == "fast"
    assert classify_query_tier("2026年11月10日12点303柜有功是多少？", {"days": ["2026-11-10"], "hours": ["12:00"], "cabinets": ["303"]})["tier"] == "fast"
    assert classify_query_tier("2026年11月13日35kV站为什么出现单进线运行？按规程怎么排查？")["tier"] == "slow"

    with closing(connect(DB)) as conn:
        arc = Archive(conn, EVENT_DB)
        # Fast-tier bypasses slow LLM client completely
        stub_fast = _ParallelToolsStubClient()
        res_fast = diagnose_question(arc, "12–13日哪天全站平均有功最高？", client=stub_fast, auto_tier=True)
        assert res_fast["mode"] == "local_fast_tier" and res_fast["routing_tier"] == "fast" and stub_fast.calls == 0
        assert res_fast["verification"]["status"] == "verified"

        # Slow-tier runs 3 LLM tool_calls in parallel via ThreadPoolExecutor
        stub_par = _ParallelToolsStubClient()
        res_par = diagnose_question(arc, "请分析2026年11月13日15点303和306柜以及1018柜健康状况", client=stub_par, auto_tier=True)
        assert res_par["status"] == "ok" and res_par["verification"]["status"] == "verified"
        assert res_par["parallel_tool_batches"] == 1 and res_par["tool_calls"] == 3
        assert [t["tool"] for t in res_par["tool_trace"]] == [
            "get_window_snapshot",
            "get_cabinet_health_profile",
            "search_coal_standards",
        ]
    print("[2/6] Fast/Slow tiered routing + ThreadPoolExecutor parallel multi-tool execution OK")


def verify_03_multi_model_failover_and_local_fallback() -> None:
    with TemporaryDirectory() as tmp:
        cb_db = Path(tmp) / "cb.sqlite3"
        p1 = _FailingModelClient("deepseek-primary")
        b1 = _HealthyBackupClient()
        fc = FailoverChatClient([p1, b1], breaker_db=cb_db, failure_threshold=1, cooldown_seconds=60.0)
        msg, rec = fc.chat([{"role": "user", "content": "ping"}])
        assert rec["failover_used"] is True and rec["served_by_model"] == "backup-qwen-72b"
        assert p1.calls == 1

        # Second call sees primary circuit breaker OPEN -> skips p1 completely!
        msg2, rec2 = fc.chat([{"role": "user", "content": "ping2"}])
        assert p1.calls == 1 and rec2["failover_used"] is True

        # Total model outage -> graceful zero-hallucination local fallback in diagnose_question
        fc_all_down = FailoverChatClient(
            [_FailingModelClient("m1"), _FailingModelClient("m2")],
            breaker_db=cb_db,
            failure_threshold=1,
        )
        with closing(connect(DB)) as conn:
            arc = Archive(conn, EVENT_DB)
            res = diagnose_question(
                arc,
                "2026年11月13日35kV站运行有哪些明显异常？",
                client=fc_all_down,
                fallback_to_local=True,
            )
            assert res["status"] == "ok" and res["degraded_from_model"] is True
            assert res["verification"]["status"] == "verified"

        ops = GLOBAL_LLMOPS.summary()
        assert ops["llm_failovers_total"] >= 2 and ops["fast_tier_routed_total"] >= 1
        assert ops["parallel_tool_batches_total"] >= 1 and ops["llm_local_fallbacks_total"] >= 1
    print("[3/6] Multi-model Failover + SharedCircuitBreaker + Graceful local fallback + LLMOps telemetry OK")


def verify_04_fastapi_tickets_lifecycle_rbac_and_prometheus() -> None:
    with TemporaryDirectory() as tmp:
        tp = Path(tmp)
        wb = Workbench(
            database=DB,
            events=EVENT_DB,
            source=ROOT,
            model=None,
            reviews_db=tp / "reviews.sqlite3",
            sessions_db=tp / "sessions.sqlite3",
            audit_db=tp / "audit.sqlite3",
            jobs_db=tp / "jobs.sqlite3",
            database_url=f"sqlite+pysqlite:///{tp / 'ent.sqlite3'}",
        )
        try:
            app = create_app(wb, require_auth=True, rate_limit_per_min=300)
            client = TestClient(app)
            v_hdr = {"Authorization": f"Bearer {issue_token('auditor', 'viewer')['access_token']}"}
            d_hdr = {"Authorization": f"Bearer {issue_token('disp1', 'dispatcher')['access_token']}"}
            e_hdr = {"Authorization": f"Bearer {issue_token('eng1', 'engineer')['access_token']}"}

            # RBAC checks
            assert client.post("/api/proactive/scan", json={"max_new_tickets": 2}, headers=v_hdr).status_code == 403
            assert client.post("/api/proactive/reconcile", json={"cabinet": "303", "at_time": "2026-11-10 12:00:00", "prev_p_kw": 0, "curr_p_kw": 0}, headers=d_hdr).status_code == 403

            # Dispatcher runs proactive scan
            r_scan = client.post("/api/proactive/scan", json={"max_new_tickets": 2}, headers=d_hdr)
            assert r_scan.status_code == 200 and r_scan.json()["created_count"] == 2
            ticket = r_scan.json()["created_tickets"][0]
            tid = ticket["ticket_id"]
            pre = ticket["pre_diagnosis"]
            assert pre["kind"] == "reverse_power_review" and pre["refuted_hypotheses"]
            ticket_url = f"/api/tickets/{tid}"
            assert client.get(ticket_url, headers=v_hdr).json()["pre_diagnosis"] == pre
            listing = client.get("/api/tickets", headers=v_hdr).json()["tickets"]
            assert next(t for t in listing if t["ticket_id"] == tid)["pre_diagnosis"] == pre

            # A legacy ticket can be explicitly upgraded without another ticket or notification.
            with wb.ticket_store._write() as runtime:
                runtime.execute("UPDATE incident_ticket SET pre_diagnosis_json = NULL WHERE ticket_id = ?", (tid,))
            refresh_url = f"{ticket_url}/pre-diagnosis?expected_revision={ticket['revision']}"
            assert client.post(refresh_url, headers=v_hdr).status_code == 403
            refreshed = client.post(refresh_url, headers=d_hdr)
            assert refreshed.status_code == 200, refreshed.text
            assert refreshed.json()["revision"] == ticket["revision"] + 1 and refreshed.json()["pre_diagnosis"] == pre
            assert client.post(refresh_url, headers=d_hdr).status_code == 409
            stale_transition = client.post(
                f"{ticket_url}/transition", json={"action": "claim", "expected_revision": ticket["revision"]}, headers=d_hdr,
            )
            assert stale_transition.status_code == 409
            refreshed_detail = client.get(ticket_url, headers=v_hdr).json()
            assert len(refreshed_detail["outbox"]) == 1
            assert refreshed_detail["logs"][-1]["action"] == "refresh_pre_diagnosis"

            # Full shift lifecycle transitions on the ticket
            for act, note in [
                ("claim", "早班值班长认领"),
                ("supplement", "已补铭牌参数复算"),
                ("dispute", "现场复核提出反向意见保留冲突"),
                ("escalate", "超 30 分钟升级机电科"),
                ("handover", "移交中班继续跟踪"),
                ("close", "核实完毕销项"),
                ("reopen", "二次异常复开核查"),
            ]:
                rt = client.post(
                    f"/api/tickets/{tid}/transition",
                    json={"action": act, "shift_name": "中班", "note": note},
                    headers=d_hdr,
                )
                assert rt.status_code == 200, rt.text

            det = client.get(f"/api/tickets/{tid}", headers=v_hdr).json()
            assert det["status"] == "reopened" and len(det["logs"]) == 9  # create + refresh + 7 transitions
            supersede = client.post(
                f"{ticket_url}/transition",
                json={"action": "supersede", "note": "订正后替代原发现", "expected_revision": det["revision"]},
                headers=d_hdr,
            )
            assert supersede.status_code == 200, supersede.text
            assert client.post(
                f"{ticket_url}/pre-diagnosis?expected_revision={supersede.json()['ticket']['revision']}", headers=d_hdr,
            ).status_code == 409
            superseded = client.get(ticket_url, headers=v_hdr).json()
            assert superseded["status"] == "superseded" and superseded["pre_diagnosis"] == pre
            assert superseded["outbox"][0]["status"] == "revoked"

            # Engineer reconciles correction
            r_rec = client.post(
                "/api/proactive/reconcile",
                json={"cabinet": "303", "at_time": "2026-11-10 12:00:00", "prev_p_kw": 100.0, "curr_p_kw": 100.0},
                headers=e_hdr,
            )
            assert r_rec.status_code == 200

            # LLMOps + Duty SLA endpoint & Prometheus /metrics
            m_json = client.get("/api/llmops/metrics", headers=v_hdr).json()
            assert "llmops" in m_json and "duty_sla" in m_json
            prom = client.get("/metrics", headers=v_hdr).text
            for metric_name in (
                "laowadi_llm_ttft_ms_avg",
                "laowadi_llm_tokens_total",
                "laowadi_llm_failovers_total",
                "laowadi_parallel_tool_batches_total",
                "laowadi_tickets_open",
                "laowadi_outbox_delivered",
            ):
                assert metric_name in prom, f"Missing Prometheus metric: {metric_name}"

            oa_paths = len(client.get("/openapi.json", headers=v_hdr).json()["paths"])
            assert oa_paths >= 60, f"Expected >=60 OpenAPI paths, got {oa_paths}"
            print(f"[4/6] FastAPI /api/tickets/*, /api/proactive/*, /api/llmops/metrics, RBAC & Prometheus OK ({oa_paths} OpenAPI paths)")
        finally:
            wb.db_manager.dispose()


def verify_05_frontend_duty_research_separation() -> None:
    main_jsx = (ROOT / "workbench" / "src" / "main.jsx").read_text(encoding="utf-8")
    tkt_jsx = (ROOT / "workbench" / "src" / "IncidentTicketPanel.jsx").read_text(encoding="utf-8")
    assert all(label in main_jsx for label in ("DUTY_TABS", "RESEARCH_TABS", "值班问答", "研究工具"))
    assert "unreadBadge" in main_jsx and "/api/tickets" in tkt_jsx and "downloadCmmsWorkorder" in tkt_jsx
    assert (ROOT / "workbench" / "dist" / "index.html").is_file()
    print("[5/6] Frontend Duty/Research mode separation, IncidentTicketPanel & Vite build OK")


def main() -> None:
    before_db = _sha256(DB)
    before_ev = _sha256(EVENT_DB)
    verify_01_proactive_watcher_and_outbox_retry()
    verify_02_parallel_tools_and_fast_slow_routing()
    verify_03_multi_model_failover_and_local_fallback()
    verify_04_fastapi_tickets_lifecycle_rbac_and_prometheus()
    verify_05_frontend_duty_research_separation()
    assert _sha256(DB) == before_db and _sha256(EVENT_DB) == before_ev
    print("[6/6] Read-only source archives SHA-256 strictly unchanged OK")
    print("\nALL 6 P1 AUTONOMY, LLMOPS & SHIFT TICKET LIFECYCLE CHECKS PASSED!")


if __name__ == "__main__":
    main()
