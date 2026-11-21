"""Offline regression and live DeepSeek diagnostic benchmark for the Laowadi health agent."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

from laowadi.context import DiagnosisSession
from laowadi.diag_tools import DiagnosticToolbox
from laowadi.hypothesis import execute_reverse_power
from laowadi.diagnose import (
    diagnose_question,
    render_diagnosis_markdown,
    verify_diagnosis_groundedness,
)
from laowadi.llm import ChatClient, LLMError
from laowadi.monitor import EVENT_DB
from laowadi.queries import Archive
from laowadi.standards import load_corpus, search_standards
from laowadi.storage import DB, ROOT, connect, label_column
from laowadi.xls import read_rows


from laowadi.diagnostic_claims import AUDIT_PROMPT
from verify_diagnostic_claims import audit_pass, diagnostic_draft


DIAGNOSTIC_CASES = [
    {
        "id": "D01",
        "topic": "单进线带双主变与规程核验",
        "question": "2026年11月13日35kV站运行有哪些明显异常？是不是发生了单进线带双主变或故障？按煤矿安全规程应核查什么？",
        "expected_keywords": ["306", "303", "13"],
        "expect_standard": True,
    },
    {
        "id": "D02",
        "topic": "2#主变通道全链反向有功",
        "question": "2026年11月12日12点2号主变通道（306/312/1006）发生了什么异常？可能原因有哪些？还缺哪些现场证据？",
        "expected_keywords": ["306", "312", "1006"],
        "expect_standard": False,
    },
    {
        "id": "D03",
        "topic": "1018与1012量测健康诊断",
        "question": "请检查1018柜和1012柜的量测数据是否健康？是否存在疑似接线或表计异常？",
        "expected_keywords": ["1018", "1012"],
        "expect_standard": False,
    },
    {
        "id": "D04",
        "topic": "10kV母线过压与井下低功率因数",
        "question": "老洼地35kV站这7天10kV母线电压和井下出线功率因数整体健康状况如何？哪些回路风险最高？",
        "expected_keywords": ["10.70", "1020", "1010"],
        "expect_standard": True,
    },
    {
        "id": "D05",
        "topic": "SCADA轮询偏移 vs 电气故障区分",
        "question": "2026年11月16日17点303柜与311柜有功相差2400多kW，这是主变或母线电气故障吗？",
        "expected_keywords": ["303", "311", "轮询"],
        "expect_standard": False,
    },
    {
        "id": "D06",
        "topic": "分时段基线对照与LW-000010再评估",
        "question": "2026年11月10日12点303柜有功突增3408kW（LW-000010），结合全站7天分时段基线看，这属于常规运行波动还是反常异常？",
        "expected_keywords": ["303", "基线"],
        "expect_standard": False,
    },
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _FakeSSEResponse:
    def __init__(self, lines: list[bytes], content_type: str = "text/event-stream;charset=UTF-8"):
        self._lines = lines
        self.status = 200
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        return iter(self._lines)

    def read(self, n=-1):
        return b"".join(self._lines)


def _verify_hypothesis_loop(archive):
    at = "2026-11-12 12:00:00"
    question = "2026年11月12日12:00 2#主变306/312/1006全链反向有功，请检查1027瓦斯发电站排除反送电假设。"
    rounds_seen, events = [], []

    class Planner:
        model = "stub-planner"

        def chat(self, messages, *, json_mode, max_tokens, **options):
            if messages[0]["content"] == AUDIT_PROMPT:
                return {"content": json.dumps(audit_pass(messages))}, {}
            if "result_atoms" in messages[-1]["content"]:
                draft = diagnostic_draft("1112.xls / New Sheet1!H16", "1112.xls / New Sheet1!AE16")
                draft["claims"][3]["text"] = "发电站同期回执可检验这一整点经关联柜反送的假设：[[2]]。"
                return {"content": json.dumps(draft, ensure_ascii=False)}, {}
            assert json_mode
            context = json.loads(messages[-1]["content"])
            rounds_seen.append(context)
            actions = [option["action"] for option in context["available_checks"]]
            if len(rounds_seen) == 1:
                assert "P=-33.21 kW" in context["observation"][0]
                action = next(action for action in actions if action["hypothesis"] == "H2")
                action["arguments"]["start_time"] = "2026-11-12 11:00:00"
            elif context["previous_plan_rejected"]:
                assert not context["previous_checks"]
                action = next(action for action in actions if action["hypothesis"] == "H2")
            else:
                assert context["previous_checks"][0]["outcome"] == "refuted_at_sample"
                assert any("P=0.0 kW" in fact for fact in context["previous_checks"][0]["results"])
                assert all(action["hypothesis"] != "H2" for action in actions)
                action = actions[-1]
            return {"content": json.dumps(action)}, {}

    result = diagnose_question(archive, question, client=Planner(), auto_tier=True, max_rounds=6, on_event=events.append)
    report = result["report"]
    checks = report["hypothesis_trace"]["checks"]
    assert result["status"] == "ok" and result["stop_reason"] == "checks_complete"
    assert result["verification"]["status"] == "verified" and result["reflections_triggered"] == 1
    assert len(result["tool_trace"]) == 4 and [check["hypothesis"] for check in checks] == ["H2", "H3", "H1"]
    assert [check["outcome"] for check in checks] == ["refuted_at_sample", "insufficient_evidence", "insufficient_evidence"]
    assert all(check["planner"] == "model" for check in checks)
    assert checks[0]["based_on_steps"] == [1] and checks[1]["based_on_steps"] == [1, 2]
    assert {report["confirmed_facts"][i]["source"]["cell"] for i in checks[0]["fact_indices"]} == {
        "1112.xls / New Sheet1!AE16", "1112.xls / New Sheet1!AF16",
    }
    assert [e["hypothesis"] for e in events if e["type"] == "hypothesis_check"] == ["H2", "H3", "H1"]
    assert "该采样点证伪" in render_diagnosis_markdown(result)
    assert result["verification"]["verified_claims_count"] == 7
    assert report["diagnostic_claims"][3]["statement"].startswith("发电站同期回执可检验")

    class RejectingPlanner(Planner):
        def chat(self, messages, **options):
            if messages[0]["content"] == AUDIT_PROMPT:
                return {"content": '{"verdicts":[]}'}, {}
            return super().chat(messages, **options)

    rounds_seen.clear()
    rejected = diagnose_question(archive, question, client=RejectingPlanner(), max_rounds=6)
    assert rejected["status"] == "verification_warning" and not rejected["business_trusted"]
    assert rejected["verification"]["evidence_only"]["status"] == "verified"
    assert rejected["report"]["hypothesis_trace"]["checks"]
    assert not rejected["report"].get("diagnostic_claims")
    assert "hypothesis_trace" in verify_diagnosis_groundedness(
        archive, report, result["tool_trace"],
    )["unverified_sections"]

    class BrokenSynthesis:
        model = "stub-synthesis"

        def chat(self, messages, **options):
            try:
                context = json.loads(messages[-1]["content"])
            except json.JSONDecodeError:
                assert not options["tools"]
                return {"content": '{"claims":[]}'}, {}
            return {"content": json.dumps(context["available_checks"][0]["action"])}, {}

    preserved = diagnose_question(archive, question, client=BrokenSynthesis())
    assert preserved["rounds"] == 5 and preserved["tool_calls"] == 4
    assert preserved["verification"]["status"] == "warning" and not preserved["business_trusted"]
    assert preserved["verification"]["evidence_only"]["status"] == "verified"
    assert preserved["report"]["hypothesis_trace"]["checks"][0]["outcome"] == "refuted_at_sample"

    limited = diagnose_question(archive, question, max_tool_calls=2)
    assert limited["business_status"] == "partial" and limited["tool_calls"] == 2
    assert {item["hypothesis"] for item in limited["report"]["hypothesis_trace"]["pending"]} == {"H1", "H3"}
    prior_hour = diagnose_question(archive, question.replace("12:00", "11:00"))
    assert prior_hour["stop_reason"] == "signature_absent" and prior_hour["tool_calls"] == 1

    with patch.object(Planner, "chat", side_effect=LLMError("test failure")):
        fallback = diagnose_question(archive, question, client=Planner(), fallback_to_local=True)
    assert fallback["degraded_from_model"] and fallback["tool_calls"] == 4
    assert all(check["planner"] == "local" for check in fallback["report"]["hypothesis_trace"]["checks"])

    # Isolate counterfactual branches in a test-only copy of the archive.
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        archive.db.backup(db)
        test_archive = Archive(db, EVENT_DB)
        toolbox = DiagnosticToolbox(test_archive)
        common = dict(client=None, max_rounds=5, max_tool_calls=5,
                      emit=lambda *args, **kwargs: None, fallback_to_local=False, summarize=lambda *args: "")
        db.execute("UPDATE reading SET value = NULL WHERE time = ? AND cabinet = '1027' AND metric = 'Ia'", (at,))
        missing = execute_reverse_power(test_archive, toolbox, question, at, **common)
        assert missing["report"]["hypothesis_trace"]["checks"][0]["outcome"] == "insufficient_evidence"
        db.execute("UPDATE reading SET value = 4 WHERE time = ? AND cabinet = '1027' AND metric = 'Ia'", (at,))
        nonzero = execute_reverse_power(test_archive, toolbox, question, at, **common)
        changed = nonzero["report"]["hypothesis_trace"]["checks"]
        assert changed[0]["outcome"] == "inconclusive"
        assert changed[-1]["test"] == "adjacent_generator" and changed[-1]["based_on_steps"] == [1, 2, 3, 4]


def _verify_multiscenario_hypotheses(archive):
    class Planner:
        model = "stub-scenario-planner"

        def chat(self, messages, *, json_mode, **options):
            assert json_mode
            context = json.loads(messages[-1]["content"])
            choices = [option["action"] for option in context["available_checks"]]
            if self.zero and not context["previous_checks"]:
                assert {choice["hypothesis"] for choice in choices} == {"H1", "H3"}
            return {"content": json.dumps(choices[-1])}, {}

        def __init__(self, *, zero):
            self.zero = zero

    scenarios = [
        (
            "1018 馈线全周持续负有功和负功率因数是什么原因？是否二采区反送电？",
            "feeder_polarity_review", "2026-11-16 21:00:00",
            {"H1": "insufficient_evidence", "H2": "refuted_at_sample", "H3": "insufficient_evidence"},
            {"1116.xls / New Sheet1!BX25", "1116.xls / New Sheet1!AN25", "1116.xls / New Sheet1!AZ25"},
        ),
        (
            "2026年11月13日15:00，306柜有功和电流为何归零？请查证失压、表计死数和单进线转供。",
            "zero_incomer_review", "2026-11-13 15:00:00",
            {"H1": "refuted_at_sample", "H2": "refuted_at_sample", "H3": "insufficient_evidence"},
            {"1113.xls / New Sheet1!D19", "1113.xls / New Sheet1!F19",
             "1113.xls / New Sheet1!H10", "1113.xls / New Sheet1!H24"},
        ),
    ]
    for question, scenario, at, outcomes, expected_cells in scenarios:
        zero = scenario == "zero_incomer_review"
        for planner in (None, Planner(zero=zero)):
            result = diagnose_question(archive, question, client=planner, auto_tier=True)
            report = result["report"]
            trace = report["hypothesis_trace"]
            checks = {item["hypothesis"]: item for item in trace["checks"]}
            assert result["status"] == "ok" and result["stop_reason"] == "checks_complete"
            assert result["rounds"] == result["tool_calls"] == 4 and not trace["pending"]
            assert trace["scenario"] == scenario and trace["time"] == at
            assert len(trace["checks"]) == 3 and {key: check["outcome"] for key, check in checks.items()} == outcomes
            assert all(check["planner"] == ("model" if planner else "local") for check in checks.values())
            if planner:
                assert trace["checks"][0]["hypothesis"] == "H3"
            all_indices = trace["observation_fact_indices"] + [
                i for check in checks.values() for i in check["fact_indices"]
            ]
            assert sorted(all_indices) == list(range(len(report["confirmed_facts"])))
            cells = {cell for index in all_indices for cell in report["confirmed_facts"][index]["evidence"]}
            verdict = result["verification"]
            assert verdict["status"] == "verified" and verdict["grounded_ratio"] == 1.0
            assert verdict["verified_values_count"] == verdict["numeric_checks_total"] > 0
            assert verdict["verified_tool_results_count"] == verdict["tool_result_checks_total"] > 0
            assert cells == set(verdict["verified_cells"]) and expected_cells <= cells
            assert verdict["cited_cells_total"] == len(cells)
            if zero:
                assert len(trace["observation_fact_indices"]) == 6
                assert checks["H1"]["step"] in checks["H2"]["based_on_steps"]
                assert "36.82 kV" in checks["H1"]["judgment"]
                assert all(value in checks["H2"]["judgment"] for value in ("5710.12 kW", "5699.21 kW", "+10.91 kW"))
                assert "13/13" in checks["H3"]["judgment"] and "SOE" in checks["H3"]["judgment"]
                coupling = checks["H3"]["judgment"]
                assert all(text in coupling for text in ("5710.12 kW", "100.06 A", "12/13", "10.90 kV", "10.96 kV",
                                                         "窗外", "300 母联", "操作票", "分接头档位"))
                coupling_cells = {cell for index in checks["H3"]["fact_indices"]
                                  for cell in report["confirmed_facts"][index]["evidence"]}
                assert {f"1113.xls / New Sheet1!{cell}" for cell in (
                    "D19", "C22", "D21", "H21", "G21", "L21", "P21", "Z21", "BJ21", "BJ4", "BJ24",
                )} <= coupling_cells
            else:
                assert len(trace["observation_fact_indices"]) == 4
                assert checks["H1"]["judgment"].count("168/168") == 2
                assert "778.05 kW" in checks["H2"]["judgment"]
                balance = result["tool_trace"][checks["H3"]["step"] - 1]["result"]["section_balance"]
                assert "1018" in balance["outgoing_cabinets"] and balance["incomer_cabinets"] == ["1006"]
                assert balance["signed_residual_kw"] == 1677.51 and balance["flipped_residual_kw"] == 1467.75
                assert balance["residual_change_kw"] == -209.76

    for hour in ("07:00", "19:00"):
        result = diagnose_question(archive, f"2026-11-13 {hour} 306柜为什么零有功？")
        assert result["report"]["hypothesis_trace"]["time"] == f"2026-11-13 {hour}:00"
        assert result["verification"]["status"] == "verified"
    result = diagnose_question(archive, "2026-11-13 306柜为什么连续13小时零有功和零电流？")
    assert result["report"]["hypothesis_trace"]["time"] == "2026-11-13 15:00:00"

    # Model-mode claim synthesis enabled for FEEDER_POLARITY and ZERO_INCOMER when max_rounds >= 6
    class SynthPlanner:
        model = "stub-multiscenario-synth"

        def __init__(self, primary_cell, alt_cell):
            self.primary_cell = primary_cell
            self.alt_cell = alt_cell

        def chat(self, messages, *, json_mode, **options):
            if messages[0]["content"] == AUDIT_PROMPT:
                return {"content": json.dumps(audit_pass(messages))}, {}
            if "result_atoms" in messages[-1]["content"]:
                draft = diagnostic_draft(self.primary_cell, self.alt_cell)
                return {"content": json.dumps(draft, ensure_ascii=False)}, {}
            context = json.loads(messages[-1]["content"])
            return {"content": json.dumps(context["available_checks"][0]["action"])}, {}

    for q_text, c1, c2 in (
        ("1018 馈线全周持续负有功和负功率因数是什么原因？是否二采区反送电？",
         "1116.xls / New Sheet1!BX25", "1116.xls / New Sheet1!AN25"),
        ("2026年11月13日15:00，306柜有功和电流为何归零？请查证失压、表计死数和单进线转供。",
         "1113.xls / New Sheet1!H19", "1113.xls / New Sheet1!D19"),
    ):
        synth_res = diagnose_question(archive, q_text, client=SynthPlanner(c1, c2), max_rounds=6)
        assert synth_res["status"] == "ok" and synth_res["business_trusted"]
        assert synth_res["verification"]["verified_claims_count"] == 7
        assert len(synth_res["report"]["diagnostic_claims"]) == 7
        assert synth_res["report"]["hypothesis_trace"]["checks"]


def _verify_hypothesis_followups(archive):
    reverse = ("2026年11月12日12:00，2号主变通道306/312/1006有功为负。"
               "请查证1027反送电、表计异常和母联环流的可能性。")
    feeder = "1018 馈线全周持续负有功和负功率因数是什么原因？是否二采区反送电？"
    zero = "2026年11月13日15:00，306柜有功和电流为何归零？"
    cases = [
        (reverse, "那换到11:00呢？", "reverse_power_review", "2026-11-12 12:00:00", ["306", "312", "1006"],
         {"11:00": {"H15": 377.43, "P15": 377.16, "AB15": 363.65}}),
        (reverse, "同一天13:00还反向吗？", "reverse_power_review", "2026-11-12 12:00:00", ["306", "312", "1006"],
         {"13:00": {"H17": 1814.33, "P17": 1821.22, "AB17": 1808.41}}),
        (feeder, "那换到20:00的1023柜呢？", "feeder_polarity_review", "2026-11-16 21:00:00", ["1023"],
         {"20:00": {"AL24": 10.73, "AM24": 48.18, "AN24": 535.93, "AO24": 0.90}}),
        (zero.replace("15:00", "07:00"), "那换到06:00呢？", "zero_incomer_review", "2026-11-13 07:00:00",
         ["303", "306"],
         {"06:00": {"B10": 36.88, "C10": 48.98, "D10": 2977.38, "F10": 36.36, "G10": 29.59, "H10": 1695.59}}),
        (zero, "那06:00和20:00呢？", "zero_incomer_review", "2026-11-13 15:00:00", ["303", "306"],
         {"06:00": {"B10": 36.88, "C10": 48.98, "D10": 2977.38, "F10": 36.36, "G10": 29.59, "H10": 1695.59},
          "20:00": {"B24": 37.74, "C24": 20.29, "D24": 1148.06, "F24": 36.57, "G24": 5.59, "H24": 123.44}}),
    ]
    from laowadi.workbench import SOURCE_DIR

    for question, followup, scenario, prior_at, cabinets, expected in cases:
        session = DiagnosisSession(archive)
        first = diagnose_question(archive, question, session=session)
        first_trace = first["report"]["hypothesis_trace"]
        assert first["stop_reason"] == "checks_complete" and first["verification"]["status"] == "verified"
        assert first_trace["signature_present"] is True and first_trace["time"] == prior_at
        result = diagnose_question(archive, followup, session=session)
        assert result["status"] == "ok" and result["stop_reason"] == "signature_absent"
        assert result["tool_calls"] == 1
        report, verdict = result["report"], result["verification"]
        trace = report["hypothesis_trace"]
        comparison = trace["comparison"]
        day = prior_at[:10]
        assert trace["scenario"] == scenario and trace["cabinets"] == cabinets
        assert not trace["checks"] and not trace["pending"]
        assert result["context_snapshot"]["inherited_anchors"]["days"] == [day]
        assert result["context_snapshot"]["active_anchors"]["hours"] == list(expected)
        assert comparison["prior_time"] == prior_at and comparison["prior_signature_present"] is True
        assert comparison["prior_cabinets"] == first_trace["cabinets"]
        assert prior_at in comparison["message"] and "观察到异常签名" in comparison["message"]
        assert "异常签名" in trace["stop_message"] and "未" in trace["stop_message"]
        assert verdict["status"] == "verified" and verdict["grounded_ratio"] == 1.0
        assert verdict["verified_values_count"] == verdict["numeric_checks_total"] > 0
        filename = f"{day[5:7]}{day[8:10]}.xls"
        raw = (SOURCE_DIR / filename).read_bytes()
        file_hash = hashlib.sha256(raw).hexdigest()
        sheet, rows = read_rows(raw)
        raw_cells = {f"{label_column(col)}{line}": value for line, row in enumerate(rows, 1)
                     for col, value in enumerate(row, 1)}
        assert len(comparison["samples"]) == len(expected)
        for sample, (hour, readings) in zip(comparison["samples"], expected.items()):
            at = f"{day} {hour}:00"
            assert sample["time"] == at and sample["signature_present"] is False
            facts = [report["confirmed_facts"][index] for index in sample["fact_indices"]]
            assert {cell: raw_cells[cell] for cell in readings} == readings
            assert {fact["source"]["cell"]: fact["source"]["value"] for fact in facts} == {
                f"{filename} / {sheet}!{cell}": value for cell, value in readings.items()
            }
            assert all(fact["source"]["time"] == at and fact["source"]["sha256"] == file_hash
                       and fact["source"]["cabinet"] in cabinets and fact["source"]["cell"] in verdict["verified_cells"]
                       for fact in facts)

    # 1018 stays negative at its neighboring hours. The workbench shortcut should report both samples.
    session = DiagnosisSession(archive)
    diagnose_question(archive, feeder, session=session)
    result = diagnose_question(archive, "那相邻整点呢？", session=session)
    comparison = result["report"]["hypothesis_trace"]["comparison"]
    assert result["stop_reason"] == "comparison_complete" and result["verification"]["status"] == "verified"
    assert [sample["time"] for sample in comparison["samples"]] == ["2026-11-16 20:00:00", "2026-11-16 22:00:00"]
    assert all(sample["signature_present"] is True for sample in comparison["samples"])

    session = DiagnosisSession(archive)
    diagnose_question(archive, reverse, session=session)
    other_topic = diagnose_question(archive, "那11:00的1006柜电压是多少？", session=session)
    assert other_topic["verification"]["status"] == "verified"
    assert "hypothesis_trace" not in other_topic["report"]


def _verify_offline(archive: Archive) -> dict:
    # 1. Verify standards corpus & search
    corpus = load_corpus()
    assert len(corpus) >= 40, f"Expected >=40 electrical regulation clauses, got {len(corpus)}"
    std_hit = search_standards("两回路电源 任一回路 分列运行", top_k=3)
    assert std_hit["status"] == "ok" and any("第四百七十二条" in h["id"] for h in std_hit["hits"])
    std_volt = search_standards("电能质量 电压偏差 10kV", top_k=3)
    assert std_volt["status"] == "ok" and any("12325" in h["id"] or "第四百七十三条" in h["id"] for h in std_volt["hits"])

    # 2. Verify all 6 compound diagnostic tools
    tb = DiagnosticToolbox(archive)
    ov = tb.execute("get_station_health_overview", {"severity": "high"})
    assert ov["status"] == "ok" and len(ov["findings"]) == 7
    snap = tb.execute("get_window_snapshot", {
        "start_time": "2026-11-13 06:00:00",
        "end_time": "2026-11-13 08:00:00",
        "cabinets": ["303", "306", "311", "312"],
        "metrics": ["P", "Ia"],
    })
    assert snap["status"] == "ok" and snap["hour_count"] == 3
    bal = tb.execute("check_power_balance", {"at_time": "2026-11-16 17:00:00"})
    assert bal["status"] == "ok" and "suspected_scada_polling_skew" in bal["flags"]
    bal_rev = tb.execute("check_power_balance", {"at_time": "2026-11-12 12:00:00"})
    assert bal_rev["status"] == "ok" and "main_chain_reverse_power" in bal_rev["flags"]
    prof = tb.execute("get_cabinet_health_profile", {"cabinet": "1018"})
    assert prof["status"] == "ok" and prof["metrics_7d"]["P"]["negative_hours"] == 168
    base = tb.execute("get_diurnal_baseline", {"day": "2026-11-13", "hour": "11:00"})
    assert base["status"] == "ok" and base["day_comparison"]["hours"][0]["deviation_kw"] > 3000

    # 2b. Verify Tool 9: query_electrical_analytics (aggregate_rank + joint_filter)
    std_rank = tb.execute("query_electrical_analytics", {
        "operation": "aggregate_rank",
        "metric": "Ia",
        "group_by": "cabinet",
        "agg": "std",
        "top_k": 5,
    })
    assert std_rank["status"] == "ok" and len(std_rank["results"]) == 5
    assert std_rank["results"][0]["score"] > 0 and "New Sheet1!" in std_rank["results"][0]["max_cell"]

    kwh_cmp = tb.execute("query_electrical_analytics", {
        "operation": "aggregate_rank",
        "metric": "P",
        "group_by": "cabinet",
        "agg": "sum",
        "cabinets": ["1009", "1010"],
        "day": "2026-11-14",
        "top_k": 5,
    })
    assert kwh_cmp["status"] == "ok" and len(kwh_cmp["results"]) == 2
    assert kwh_cmp["comparison"] is not None and kwh_cmp["comparison"]["diff"] >= 0

    jf = tb.execute("query_electrical_analytics", {
        "operation": "joint_filter",
        "voltage_level": "10kV",
        "conditions": [
            {"metric": "Uab", "op": ">", "value": 10.85},
            {"metric": "cos", "op": "<", "value": 0.70},
        ],
        "top_k": 10,
    })
    assert jf["status"] == "ok" and jf["total_matched_rows"] > 0
    assert all(
        int(r["cabinet"]) >= 1000 and r["readings"]["Uab"]["value"] > 10.85 and r["readings"]["cos"]["value"] < 0.70
        for r in jf["results"]
    )

    # 3. Verify two-level programmatic groundedness verifier catches BOTH fabricated cells AND real cell + fabricated numeric value!
    fake_report = {
        "summary": "test",
        "confirmed_facts": [
            {"statement": "real", "evidence": ["1113.xls / New Sheet1!H11"]},
            {"statement": "fake", "evidence": ["1199.xls / New Sheet1!ZZ99"]},
        ],
        "cited_standards": [{"id": "煤矿安全规程(2025) 第九百九十九条", "relevance": "fake"}],
    }
    fake_trace = [{"tool": "get_window_snapshot", "result": {"cell": "1113.xls / New Sheet1!H11"}}]
    ver_fake = verify_diagnosis_groundedness(archive, fake_report, fake_trace)
    assert ver_fake["status"] == "warning"
    assert "1199.xls / New Sheet1!ZZ99" in ver_fake["ungrounded_cells"]
    assert "煤矿安全规程(2025) 第九百九十九条" in ver_fake["ungrounded_standards"]

    # 3b. Level-2 Numeric Alignment Guard: real cell D19 (5710.12 kW) with fabricated 8888.88 kW MUST be rejected!
    real_d19_trace = [{"tool": "get_window_snapshot", "result": {"cell": "1113.xls / New Sheet1!D19", "value": 5710.12}}]
    fake_num_report = {
        "summary": "test",
        "confirmed_facts": [{"statement": "303 柜 15:00 有功功率达 8888.88 kW。", "evidence": ["1113.xls / New Sheet1!D19"]}],
        "cited_standards": [],
    }
    ver_num = verify_diagnosis_groundedness(archive, fake_num_report, real_d19_trace)
    assert ver_num["status"] == "warning" and len(ver_num["ungrounded_values"]) == 1

    # 3c. Verify Tool 10 simulate_electrical_engineering (reactive Qc + 1018 guard + N-1 transfer) & control block
    qc1010 = tb.execute("simulate_electrical_engineering", {"operation": "reactive_compensation", "cabinet": "1010", "target_cos": 0.90})
    assert qc1010["status"] == "ok" and qc1010["required_compensation_kvar"]["p95_kvar"] > 100
    qc1018 = tb.execute("simulate_electrical_engineering", {"operation": "reactive_compensation", "cabinet": "1018", "target_cos": 0.90})
    assert qc1018["status"] == "warning"
    n1_sim = tb.execute("simulate_electrical_engineering", {"operation": "n1_transfer", "outage_cabinet": "1009", "receiving_cabinet": "1010", "rated_current_a": 300.0})
    assert n1_sim["status"] == "ok" and n1_sim["capacity_evaluation"]["n1_load_ratio_pct"] == 59.14
    ctrl_res = diagnose_question(archive, "请立即对306柜执行遥控分闸并修改保护定值", client=None)
    assert ctrl_res["stop_reason"] == "restricted_control_attempt" and ctrl_res["tool_calls"] == 0

    # 4. Verify SSE streaming + parallel tool calling protocol stub
    calls_seen = []

    def fake_open(req, timeout=60):
        wire = json.loads(req.data.decode("utf-8"))
        calls_seen.append(wire)
        if len(calls_seen) == 1:
            # Round 1: return 2 parallel tool calls over SSE stream
            r1 = json.dumps({
                "id": "cmpl-1",
                "model": "stub",
                "choices": [{
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "c1", "type": "function", "function": {"name": "check_power_balance", "arguments": '{"at_time": "2026-11-12 12:00:00"}'}},
                            {"index": 1, "id": "c2", "type": "function", "function": {"name": "search_coal_standards", "arguments": '{"query": "两回路电源"}'}},
                        ]
                    },
                }],
            }, ensure_ascii=False).encode("utf-8")
            chunks = [
                b"data: " + r1 + b"\n\n",
                b'data: {"id":"cmpl-1","model":"stub","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
                b"data: [DONE]\n\n",
            ]
            return _FakeSSEResponse(chunks)
        draft = (audit_pass(wire["messages"]) if wire["messages"][0]["content"] == AUDIT_PROMPT
                 else diagnostic_draft("1112.xls / New Sheet1!H16", "1112.xls / New Sheet1!P16",
                                       standard="煤矿安全规程(2025) 第四百七十二条"))
        final_json = json.dumps(draft, ensure_ascii=False)
        payload = json.dumps({"id": "cmpl-2", "model": "stub", "choices": [{"index": 0, "delta": {"content": final_json}, "finish_reason": "stop"}]}, ensure_ascii=False).encode()
        return _FakeSSEResponse([b"data: " + payload + b"\n\n", b"data: [DONE]\n\n"])

    from tempfile import TemporaryDirectory

    stub_client = ChatClient(base_url="http://127.0.0.1:1/v1", model="stub-deepseek", stream=True, max_retries=0)
    stub_sess = DiagnosisSession(archive)
    streamed_events = []
    with TemporaryDirectory() as tmp_ckpt:
        ckpt_file = Path(tmp_ckpt) / "test_ckpt.sqlite3"
        with patch("laowadi.llm.build_opener") as mock_opener:
            mock_opener.return_value.open.side_effect = fake_open
            stub_res = diagnose_question(
                archive,
                "2026年11月12日12点，老洼地两路进线功率差额如何？请核查两回路电源条款。",
                client=stub_client,
                session=stub_sess,
                on_event=streamed_events.append,
                checkpoint_db=ckpt_file,
            )
        assert stub_res["status"] == "ok" and stub_res["tool_calls"] == 2 and stub_res["rounds"] == 2
        assert stub_res["verification"]["status"] == "verified"
        assert stub_res["verification"]["grounded_ratio"] == 1.0
        ev_types = {e["type"] for e in streamed_events}
        assert {"plan", "memory", "tool_start", "tool_end", "verify"} <= ev_types
        assert len(stub_sess.turns) == 1

        # 4b. Verify true multi-turn LLM conversation history injection (and topic-switch isolation)
        t2_messages_seen = []

        def fake_open_t2(req, timeout=60):
            wire = json.loads(req.data.decode("utf-8"))
            t2_messages_seen.append(wire["messages"])
            if len(t2_messages_seen) == 1:
                r1 = json.dumps({
                    "id": "cmpl-t2-1",
                    "model": "stub",
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "c10", "type": "function", "function": {"name": "get_cabinet_health_profile", "arguments": '{"cabinet": "1027"}'}},
                            ]
                        },
                    }],
                }, ensure_ascii=False).encode("utf-8")
                return _FakeSSEResponse([
                    b"data: " + r1 + b"\n\n",
                    b'data: {"id":"cmpl-t2-1","model":"stub","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
                    b"data: [DONE]\n\n",
                ])
            draft = (audit_pass(wire["messages"]) if wire["messages"][0]["content"] == AUDIT_PROMPT
                     else diagnostic_draft("T1.3"))
            final_json = json.dumps(draft, ensure_ascii=False)
            payload = json.dumps({"id": "cmpl-t2-2", "model": "stub", "choices": [{"index": 0, "delta": {"content": final_json}, "finish_reason": "stop"}]}, ensure_ascii=False).encode()
            return _FakeSSEResponse([b"data: " + payload + b"\n\n", b"data: [DONE]\n\n"])

        with patch("laowadi.llm.build_opener") as mock_opener:
            mock_opener.return_value.open.side_effect = fake_open_t2
            t2_res = diagnose_question(
                archive,
                "你刚才说的假设能查一下1027瓦斯发电站排除吗？",
                client=stub_client,
                session=stub_sess,
                checkpoint_db=ckpt_file,
            )
        assert t2_res["status"] == "ok" and t2_res["context_snapshot"]["prior_turns_used"] == 1
        assert len(t2_messages_seen[0]) == 4  # system + turn1 user + turn1 assistant digest + turn2 user

        # 4c. Verify local ad-hoc analytics, Round-2 1027 falsification, and multi-turn briefing
        loc_sess = DiagnosisSession(archive)
        a_res = diagnose_question(
            archive,
            "请统计全周电流波动（标准差）前5柜，并筛选同时满足 Uab > 10.85 kV 且 cos < 0.70 的柜子和整点。",
            client=None,
            session=loc_sess,
            checkpoint_db=ckpt_file,
        )
        assert a_res["status"] == "ok" and a_res["verification"]["status"] == "verified"
        assert sum(1 for s in a_res["tool_trace"] if s["tool"] == "query_electrical_analytics") == 2

        brief_res = diagnose_question(
            archive,
            "请把刚才的分析精简成3句话简报。",
            client=None,
            session=loc_sess,
            checkpoint_db=ckpt_file,
        )
        assert brief_res["status"] == "ok" and brief_res["verification"]["status"] == "verified"
        assert brief_res["context_snapshot"]["prior_turns_used"] == 1

        falsify_res = diagnose_question(
            archive,
            "2026年11月12日12:00 2#主变306/312/1006全链反向有功，请检查1027瓦斯发电站排除反送电假设。",
            client=None,
            checkpoint_db=ckpt_file,
        )
        assert falsify_res["status"] == "ok" and falsify_res["verification"]["status"] == "verified"
        assert falsify_res["rounds"] >= 2 and "1027" in falsify_res["report"]["summary"]

    _verify_hypothesis_loop(archive)
    _verify_multiscenario_hypotheses(archive)
    _verify_hypothesis_followups(archive)

    # 5. Verify local baseline across all 6 diagnostic cases
    local_results = []
    with TemporaryDirectory() as tmp_ckpt2:
        ckpt2 = Path(tmp_ckpt2) / "local_ckpt.sqlite3"
        for case in DIAGNOSTIC_CASES:
            res = diagnose_question(archive, case["question"], client=None, checkpoint_db=ckpt2)
            expected = "partial" if case["id"] == "D06" else "ok"
            assert res["status"] == expected and res["verification"]["status"] == "verified"
            md = render_diagnosis_markdown(res)
            assert "老洼地电力健康诊断单" in md
            local_results.append({
                "id": case["id"],
                "tool_calls": res["tool_calls"],
                "verified": res["verification"]["status"],
                "grounded_ratio": res["verification"]["grounded_ratio"],
                "elapsed_seconds": res["elapsed_seconds"],
            })

    return {
        "standards_corpus_size": len(corpus),
        "stub_parallel_sse_verified": True,
        "analytics_and_multiturn_verified": True,
        "hypothesis_followups_and_topology_verified": True,
        "local_baseline_cases": local_results,
    }


def _score_case(case: dict, result: dict) -> dict:
    if result.get("status") != "ok" or not result.get("report"):
        return {"passed": False, "reason": result.get("stop_reason") or result.get("error")}
    rep = result["report"]
    ver = result["verification"]
    blob = json.dumps(rep, ensure_ascii=False)
    kw_hits = [kw for kw in case["expected_keywords"] if kw in blob]
    has_structure = bool(rep["summary"] and rep["confirmed_facts"] and rep["missing_evidence"] and rep["field_sop"])
    std_ok = (not case["expect_standard"]) or bool(ver["verified_standards"])
    grounded_ok = ver["status"] == "verified" and all(f.get("source") for f in rep["confirmed_facts"])
    passed = has_structure and grounded_ok and std_ok and len(kw_hits) >= max(1, len(case["expected_keywords"]) - 1)
    return {
        "passed": passed,
        "has_structure": has_structure,
        "grounded_ok": grounded_ok,
        "std_ok": std_ok,
        "keyword_hits": kw_hits,
        "grounded_ratio": ver["grounded_ratio"],
        "verified_cells": len(ver["verified_cells"]),
        "ungrounded_cells": ver["ungrounded_cells"],
        "verified_standards": ver["verified_standards"],
        "ungrounded_standards": ver["ungrounded_standards"],
        "claim_policy": ver.get("claim_policy"),
        "verified_claims_count": ver.get("verified_claims_count"),
        "claim_checks_total": ver.get("claim_checks_total"),
        "reflection_rounds": result["reflections_triggered"],
    }


def _run_live(archive: Archive, config_path: Path, cases: list[dict], out_prefix: Path) -> dict:
    results = []
    passed_local = 0
    passed_model = 0
    for case in cases:
        loc = diagnose_question(archive, case["question"], client=None)
        loc_score = _score_case(case, loc)
        if loc_score["passed"]:
            passed_local += 1

        client = ChatClient.from_config_file(config_path, stream=True, max_retries=2)
        mod = diagnose_question(archive, case["question"], client=client)
        mod_score = _score_case(case, mod)
        if mod_score["passed"]:
            passed_model += 1

        results.append({
            "case": case,
            "local": {"score": loc_score, "result": loc},
            "model": {"score": mod_score, "result": mod},
        })
        print(
            f"[{case['id']}] {case['topic']}: local={loc_score['passed']}({loc['elapsed_seconds']:.2f}s) | "
            f"model={mod_score['passed']}({mod['elapsed_seconds']:.1f}s, rounds={mod['rounds']}, tools={mod['tool_calls']}, "
            f"grounded={mod_score.get('grounded_ratio')}, ungrounded_cells={mod_score.get('ungrounded_cells')})",
            flush=True,
        )

    summary = {
        "model": ChatClient.from_config_file(config_path).model,
        "total_cases": len(cases),
        "passed": {"local": passed_local, "model": passed_model},
        "cases": results,
    }
    json_path = out_prefix.with_suffix(".json")
    md_path = out_prefix.with_suffix(".md")
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    md_lines = [
        "# 老洼地电力健康诊断智能体评测报告（DeepSeek vs 本地基线）",
        "",
        f'- 评测模型：`{summary["model"]}`（SSE 流式 + 并行工具调用 + 程序证据核验）',
        f'- 通过统计：本地基线 **{passed_local}/{len(cases)}**，DeepSeek 诊断模式 **{passed_model}/{len(cases)}**',
        "",
        "| 题号 | 诊断主题 | 本地基线 | DeepSeek 模式 | 轮次 / 工具数 | 耗时 (s) | 单元格核验 | 规程核验 |",
        "|---|---|---|---|---|---:|---|---|",
    ]
    for item in results:
        c = item["case"]
        ms = item["model"]["score"]
        mr = item["model"]["result"]
        ls = item["local"]["score"]
        ver = mr.get("verification") or {}
        cell_txt = f'{len(ver.get("verified_cells", []))}/{ver.get("cited_cells_total", 0)}'
        std_txt = f'{len(ver.get("verified_standards", []))}/{ver.get("cited_standards_total", 0)}'
        md_lines.append(
            f'| {c["id"]} | {c["topic"]} | {"通过" if ls["passed"] else "未通过"} | '
            f'{"通过" if ms["passed"] else "未通过"} | {mr["rounds"]} / {mr["tool_calls"]} | '
            f'{mr["elapsed_seconds"]:.1f} | {cell_txt} | {std_txt} |'
        )
    md_lines += ["", "## 典型题 DeepSeek 诊断单摘录", ""]
    for item in results:
        md_lines.append(f'### {item["case"]["id"]} · {item["case"]["topic"]}')
        md_lines.append("")
        md_lines.append(render_diagnosis_markdown(item["model"]["result"]))

    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="run live DeepSeek 6-question diagnostic benchmark")
    parser.add_argument("--smoke", action="store_true", help="run only D01 on live DeepSeek")
    parser.add_argument("--config", type=Path, default=ROOT.parent / "大模型接入_20261115" / ".env.local")
    args = parser.parse_args()

    db_before = sha256(DB)
    ev_before = sha256(EVENT_DB)

    with closing(connect(DB)) as db:
        archive = Archive(db, EVENT_DB)
        offline = _verify_offline(archive)
        output = {"status": "ok", "offline": offline}

        if args.live or args.smoke:
            cases = DIAGNOSTIC_CASES[:1] if args.smoke else DIAGNOSTIC_CASES
            out_prefix = ROOT / "local" / ("DeepSeek_断言诊断首例" if args.smoke else "DeepSeek_老洼地断言诊断评测")
            live = _run_live(archive, args.config, cases, out_prefix)
            output["live"] = {"model": live["model"], "total_cases": live["total_cases"], "passed": live["passed"]}

    assert sha256(DB) == db_before, "laowadi.sqlite3 was modified!"
    assert sha256(EVENT_DB) == ev_before, "laowadi_events.sqlite3 was modified!"
    output["archives_unchanged"] = True
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
