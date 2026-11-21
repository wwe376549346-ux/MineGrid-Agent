"""Verification for Stage 07 (GSSC & session isolation), Stage 08 (Corpus), Reflection, and Stage 12 (Evaluation)."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from laowadi.context import DiagnosisSession, compress_tool_result, extract_query_anchors
from laowadi.corpus import build_corpus_ledger, render_corpus_markdown
from laowadi.diagnose import diagnose_question
from laowadi.evaluation import evaluate_all_layers, render_evaluation_markdown
from laowadi.llm import ChatClient
from laowadi.monitor import EVENT_DB
from laowadi.queries import Archive
from laowadi.storage import DB, ROOT, connect
from verify_diagnose import _FakeSSEResponse
from verify_diagnostic_claims import audit_pass, diagnostic_draft


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_reflection_self_correction(archive: Archive):
    """Verify Chapter 4/15 Reflection: Auditor catches ungrounded cell in Round 2 and model corrects it in Round 3."""
    calls = []

    def fake_open(req, timeout=60):
        wire = json.loads(req.data.decode("utf-8"))
        calls.append(wire)
        if len(calls) == 1:
            # Round 1: Call check_power_balance
            r1 = json.dumps({
                "id": "c1",
                "model": "stub",
                "choices": [{
                    "index": 0,
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "check_power_balance", "arguments": '{"at_time": "2026-11-12 12:00:00"}'},
                        }]
                    },
                }],
            }, ensure_ascii=False).encode("utf-8")
            return _FakeSSEResponse([b"data: " + r1 + b"\n\n", b"data: [DONE]\n\n"])
        if len(calls) == 2:
            # Round 2: Model hallucinates a fake cell 1199.xls / New Sheet1!ZZ99 -> should trigger Reflection!
            bad_json = json.dumps(diagnostic_draft("1199.xls / New Sheet1!ZZ99"))
            p2 = json.dumps({"id": "c2", "model": "stub", "choices": [{"index": 0, "delta": {"content": bad_json}, "finish_reason": "stop"}]}, ensure_ascii=False).encode("utf-8")
            return _FakeSSEResponse([b"data: " + p2 + b"\n\n", b"data: [DONE]\n\n"])
        if len(calls) == 3:
            last_user = wire["messages"][-1]["content"]
            assert "unknown_citation" in last_user and "claim_index" in last_user
            good_json = json.dumps(diagnostic_draft("1112.xls / New Sheet1!H16"))
        else:
            good_json = json.dumps(audit_pass(wire["messages"]))
        p3 = json.dumps({"id": "c3", "model": "stub", "choices": [{"index": 0, "delta": {"content": good_json}, "finish_reason": "stop"}]}, ensure_ascii=False).encode("utf-8")
        return _FakeSSEResponse([b"data: " + p3 + b"\n\n", b"data: [DONE]\n\n"])

    client = ChatClient(base_url="http://127.0.0.1:1/v1", model="stub-reflection", stream=True, max_retries=0)
    with patch("laowadi.llm.build_opener") as mock_opener:
        mock_opener.return_value.open.side_effect = fake_open
        res = diagnose_question(archive, "2026年11月12日12点306柜功率平衡异常情况？", client=client)

    assert res["status"] == "ok"
    assert res["reflections_triggered"] == 1
    assert res["verification"]["status"] == "verified"
    assert res["verification"]["ungrounded_cells"] == []


def main():
    db_before = sha256(DB)
    ev_before = sha256(EVENT_DB)

    with closing(connect(DB)) as db:
        archive = Archive(db, EVENT_DB)

        # 1. Verify Reflection Auditor self-correction loop
        verify_reflection_self_correction(archive)

        # 2. Verify Stage 08 Corpus & 9-question boundary check, save artifacts
        ledger = build_corpus_ledger(archive)
        assert ledger["summary"] == {"total_cases": 9, "passed_cases": 9}
        corpus_md = render_corpus_markdown(ledger)
        (ROOT / "local" / "老洼地现有资料台账与可答边界.json").write_text(
            json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (ROOT / "local" / "老洼地现有资料台账与可答边界.md").write_text(corpus_md, encoding="utf-8")

        # 3. Verify Stage 12 5-layer evaluation & Stage 07 context isolation, save artifacts
        ev = evaluate_all_layers(archive)
        assert ev["layer2_corpus_and_boundaries"]["passed_cases"] == 9
        assert ev["layer3_context_and_gssc"]["status"] == "verified"
        assert ev["layer3_context_and_gssc"]["topic_switch_no_leakage_ok"] is True
        eval_md = render_evaluation_markdown(ev)
        (ROOT / "local" / "老洼地智能体分层评测报告.json").write_text(
            json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (ROOT / "local" / "老洼地智能体分层评测报告.md").write_text(eval_md, encoding="utf-8")

    assert sha256(DB) == db_before, "laowadi.sqlite3 was modified!"
    assert sha256(EVENT_DB) == ev_before, "laowadi_events.sqlite3 was modified!"

    print(
        json.dumps(
            {
                "status": "ok",
                "reflection_self_correction_verified": True,
                "corpus_cases_passed": f'{ledger["summary"]["passed_cases"]}/{ledger["summary"]["total_cases"]}',
                "context_isolation_verified": ev["layer3_context_and_gssc"]["status"],
                "gssc_saved_pct": ev["layer3_context_and_gssc"]["gssc_saved_pct_turn1"],
                "bfcl_q12_passed": ev["layer4_bfcl_bounded_queries_12"]["passed"],
                "gaia_d06_passed": ev["layer5_gaia_diagnostic_agent_6"]["passed"],
                "archives_unchanged": True,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
