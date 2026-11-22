"""Offline contracts for the hypothesis, diagnosis, retrieval and intake upgrades."""

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch

from laowadi.autonomous_agent import run_autonomous_agent
from laowadi.code_sandbox import run_python_analysis, validate_sandbox_code
from laowadi.context import DiagnosisSession
from laowadi.diag_tools import DiagnosticToolbox
from laowadi.diagnostic_claims import AUDIT_PROMPT
from laowadi.diagnose import diagnose_question
from laowadi.hypothesis import execute_dynamic_hypotheses, generate_dynamic_hypotheses
from laowadi.llm import LLMError
from laowadi.monitor import EVENT_DB
from laowadi.multi_agent import run_multi_agent_consultation
from laowadi.multimodal_intake import extract_field_candidates
from laowadi.queries import Archive
from laowadi.rag import ConditionalRAGEngine
from laowadi.standards import DenseEmbeddingCache, load_corpus, rank_documents, search_standards
from laowadi.storage import DB, ROOT, connect
from laowadi.subagent_debate import run_parallel_subagent_debate
from thesis_eval.run import ARMS, HERE, Environment, check_claims, result_at_path, run_turn
from thesis_eval.score import score_turn
from verify_diagnostic_claims import QUESTION, audit_pass, load_case
from verify_multimine_generalization import _build_dongpo_profile, _create_dongpo_sqlite


def call(name, args, call_id="test"):
    return {"role": "assistant", "tool_calls": [{"type": "function", "id": call_id,
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


def verify_dynamic(tmp):
    profile = _build_dongpo_profile()
    db_path = tmp / "dongpo.sqlite3"
    _create_dongpo_sqlite(db_path, profile)
    # Supply unique source columns for verifiable observations in this synthetic archive.
    with closing(sqlite3.connect(db_path)) as db, db:
        for index, feeder in enumerate(profile.feeders):
            for offset, metric in enumerate(("Uab", "Ia", "P", "cos")):
                index_col = index * 4 + offset
                col = ("A" if index_col >= 26 else "") + chr(ord("A") + index_col % 26)
                db.execute("UPDATE reading SET source_col=? WHERE cabinet=? AND metric=?",
                           (col, feeder.cabinet, metric))
    with closing(connect(db_path)) as db:
        archive = Archive(db, events_db=None)
        toolbox = DiagnosticToolbox(archive, profile=profile)

        class Planner:
            model = "offline-planner"

            def chat(self, messages, **_options):
                data = json.loads(messages[-1]["content"])
                assert "F203" in data["observation"] and "-145.0" in data["observation"]
                choice = dict(data["available_checks"][-1])
                choice.pop("criterion")
                return {"content": json.dumps(choice)}, {}

        observed = {"station": "D35", "time": "2026-12-01 14:00:00", "cabinet": "F203", "metric": "P", "value": 999}
        candidates = generate_dynamic_hypotheses(profile, {**observed, "value": -145})
        assert candidates["topology_peer_readings"].arguments["cabinets"] == ["L102", "F202", "C202"]
        reviewed = execute_dynamic_hypotheses(archive, toolbox, observed, client=Planner(), max_rounds=2)
        trace = reviewed["report"]["hypothesis_trace"]
        assert reviewed["stop_reason"] == "budget_exhausted" and trace["pending"]
        assert trace["checks"][0]["planner"] == "model"
        assert reviewed["report"]["confirmed_facts"][0]["source"]["value"] == -145
        assert execute_dynamic_hypotheses(archive, toolbox, {**observed, "station": "U900"})["tool_trace"] == []
        result = diagnose_question(archive, "2026-12-01 14:00 F203柜负有功异常是什么原因？", profile=profile)
        assert result["verification"]["status"] == "verified", result["verification"]
        assert result["report"]["hypothesis_trace"]["scenario"] == "dynamic_anomaly_review"
        assert result["context_snapshot"]["mine_id"] == "dongpo"
        balance = execute_dynamic_hypotheses(archive, toolbox, {**observed, "cabinet": "H102"})
        assert any(c["tool"] == "check_power_balance" and c["fact_indices"] for c in balance["report"]["hypothesis_trace"]["checks"])


def verify_autonomy(archive, tmp):
    question = "2026-11-13 15:00 请核查303柜的有功读数"
    snapshot = {"start_time": "2026-11-13 15:00:00", "end_time": "2026-11-13 15:00:00", "cabinets": ["303"], "metrics": ["P"]}
    plan = {"steps": [{"id": "lookup", "title": "取整点读数", "status": "running"},
                      {"id": "unused", "title": "跨日对照", "status": "done"}]}

    class RepairModel:
        model = "offline-repair"

        def __init__(self, succeeds=True):
            self.turn = 0
            self.succeeds = succeeds

        def chat(self, messages, **options):
            self.turn += 1
            if self.turn == 1:
                return {"role": "assistant", "tool_calls": call("update_plan", plan, "p")["tool_calls"]
                        + call("get_window_snapshot", snapshot, "s")["tool_calls"]}, {}
            if self.turn == 2:
                return {"content": "引用不存在的原表出处 1113.xls / New Sheet1!ZZZ999。"}, {}
            assert "ZZZ999" in messages[-1]["content"] and not options.get("tools") and self.turn == 3
            return {"content": "原表读数 5710.12 kW（1113.xls / New Sheet1!D19）。" if self.succeeds else ""}, {}

    for succeeds in (True, False):
        events = []
        model = RepairModel(succeeds)
        result = run_autonomous_agent(archive, question, client=model, on_event=events.append,
                                      memory_db_path=tmp / "memory.sqlite3")
        assert model.turn == 3 and result["repair_attempted"] and result["repair_succeeded"] == succeeds
        assert "ZZZ999" not in result["answer_markdown"]
        assert result["status"] == ("ok" if succeeds else "verification_failed")
        assert [item["status"] for item in result["plan"]] == ["done", "skipped"]
        assert all("ZZZ999" not in e.get("text", "") for e in events if e["type"] == "content_delta")

    class PlanOnly:
        model = "offline-incomplete"

        def chat(self, _messages, **opts):
            return (call("update_plan", plan) if opts["tool_choice"] == "auto" else {"content": "排查全部完成。"}), {}

    unfinished = run_autonomous_agent(archive, question, client=PlanOnly(), max_steps=2,
                                     memory_db_path=tmp / "memory.sqlite3")
    assert unfinished["status"] == "incomplete" and unfinished["incomplete_reason"] == "step_budget_exhausted"
    assert not unfinished["answer_markdown"] and all(p["status"] == "skipped" for p in unfinished["plan"])
    clarification = run_autonomous_agent(archive, "下午15点那个柜的电流是多少？", client=PlanOnly())
    assert clarification["status"] == "clarify" and clarification["clarification_request"]["options"]
    assert not clarification["tool_trace"]
    session = DiagnosisSession(archive)
    session.apply_external_context({"ticket_id": "offline-fixture", "cabinet": "303", "at_time": "2026-11-13 15:00"})
    anchored = run_autonomous_agent(archive, "当时的有功读数是多少？", session=session,
                                    memory_db_path=tmp / "memory.sqlite3")
    assert anchored["status"] == "ok" and "5710.12" in anchored["answer_markdown"]


def verify_submission(archive):
    trace, draft = load_case(archive)

    class StrictModel:
        model = "offline-submit"

        def chat(self, messages, **options):
            if messages[0]["content"] == AUDIT_PROMPT:
                return {"content": json.dumps(audit_pass(messages))}, {}
            names = {tool["function"]["name"] for tool in options["tools"]}
            assert "submit_diagnosis" in names
            if "get_window_snapshot" in names:
                return call(trace[0]["tool"], trace[0]["arguments"]), {}
            return call("submit_diagnosis", draft), {}

    strict = diagnose_question(archive, QUESTION, client=StrictModel(), max_rounds=2)
    assert strict["verification"]["status"] == "verified"
    assert strict["submission_source"] == "submit_diagnosis"
    assert [item["tool"] for item in strict["tool_trace"]] == [trace[0]["tool"]]

    env = Environment()
    with closing(env.db):
        cases = {c["id"]: c for c in json.loads((HERE / "cases.json").read_text())["cases"]}
        gold = json.loads((HERE / "gold.json").read_text())
        for index in range(1, 9):
            cid = f"H{index:02}"

            class HitlModel:
                def __init__(self):
                    self.records = []

                def chat(self, messages, **options):
                    self.records.append({"status": "completed", "usage": {}})
                    assert "submit_diagnosis" in {t["function"]["name"] for t in options["tools"]}
                    last = messages[-1]
                    if last["role"] != "tool":
                        return call("get_supplement_status", {}), self.records[-1]
                    receipt = json.loads(last["content"])
                    result = receipt["result"]
                    refs = result["evidence_cells"]
                    peak_refs = [refs[2]] if result["item_id"] == "V01" else refs[:2]
                    fields = [{"key": "peak", "value": result["peak_a"], "unit": "A",
                               "citations": peak_refs, "pointer": "T1.peak_a"},
                              {"key": "state", "value": result["state"], "unit": "", "citations": []}]
                    details = result.get("details") or {}
                    ratio_field = "current_load_ratio_pct" if result["item_id"] == "V01" else "n_minus_1_load_ratio_pct"
                    if ratio_field in details:
                        fields.append({"key": "load_ratio", "value": details[ratio_field], "unit": "%",
                                       "citations": peak_refs + [details["source_doc"]],
                                       "pointer": f"T1#$.details.{ratio_field}"})
                    decision = "conditional" if details else "insufficient"
                    return call("submit_diagnosis", {"decision": decision, "claims": fields}), self.records[-1]

            previous = None
            for turn in range(2):
                observed = run_turn(env, HitlModel(), ARMS["ours"], cases[cid], turn, previous=previous)
                assert observed["status"] == "completed", (cid, turn, observed["errors"])
                assert observed["answer_source"] == "submit_diagnosis"
                assert score_turn(observed, gold[cid]["turns"][turn])["counts"]["exact"], (cid, turn, observed["answer"])
                previous = observed["answer"]

        for cid, cabinets, at, metrics, choices in (
            ("D03", ["303", "306"], "2026-11-13 15:00:00", ["P"],
             {"hypothesis": "topology_or_operation", "gap": "switch_soe", "confirmed": "unconfirmed"}),
            ("D07", ["1018"], "2026-11-11 18:00:00", ["P", "cos"],
             {"hypothesis": "meter_or_polarity", "gap": "wiring_meter", "confirmed": "unconfirmed"}),
        ):
            class ConditionalModel:
                def __init__(self):
                    self.records = []

                def chat(self, messages, **_options):
                    self.records.append({"status": "completed", "usage": {}})
                    assert "优先排查方向" in messages[0]["content"]
                    if messages[-1]["role"] != "tool":
                        response = call("get_window_snapshot", {
                            "start_time": at, "end_time": at, "cabinets": cabinets, "metrics": metrics,
                        })
                    else:
                        readings = json.loads(messages[-1]["content"])["result"]["frames"][0]["readings"]
                        keys = ("p_303", "p_306") if cid == "D03" else ("q_P", "q_cos")
                        points = [point for cabinet in cabinets for point in readings[cabinet].values()]
                        fields = [
                            {"key": key, "unit": "" if key == "q_cos" else "kW",
                             "value": point["value"], "citations": [point["cell"]]}
                            for key, point in zip(keys, points)
                        ] + [{"key": key, "unit": "", "value": value, "citations": []} for key, value in choices.items()]
                        response = call("submit_diagnosis", {"decision": "conditional", "claims": fields})
                    return response, self.records[-1]

            observed = run_turn(env, ConditionalModel(), ARMS["ours"], cases[cid], 0)
            expected = gold[cid]["turns"][0]
            assert observed["status"] == "completed" and score_turn(observed, expected)["counts"]["exact"], observed
            refused = {**observed, "answer": {**observed["answer"], "decision": "insufficient"}}
            assert score_turn(refused, expected)["counts"]["conditional_misclassified_as_insufficient"] == 1

        m05 = cases["M05"]["turns"][0]
        result = env.execute(DiagnosticToolbox(env.archive), "query_electrical_analytics", {
            "operation": "aggregate_rank", "day": "2026-11-14", "metric": "P", "agg": "sum",
            "group_by": "cabinet", "cabinets": ["1009", "1010"], "top_k": 2,
        }, m05["question"], None, None)
        receipts = {"T1": result}
        for i, row in enumerate(result["results"]):
            key = f"cab_{row['group']}"
            claim = {"key": key, "value": row["score"], "unit": "kWh",
                     "citations": [row["max_cell"], row["min_cell"]]}
            for pointer in (f"T1#/results/{i}/score", f"T1.results[{i}].score", f"T1:/results/{i}",
                            f"T1#$['result']['results'][{i}]['score']"):
                assert result_at_path(receipts, pointer)[:2] == (row["score"], "kWh")
                assert not check_claims({"claims": [{**claim, "pointer": pointer}]}, m05["fields"],
                                        receipts, env, m05["question"])
            assert result_at_path(receipts, "T1#/results/-1/score") is None
            wrong = {**claim, "key": "cab_1010" if key == "cab_1009" else "cab_1009",
                     "pointer": f"T1#/results/{i}/score"}
            assert check_claims({"claims": [wrong]}, m05["fields"], receipts, env, m05["question"])


def verify_debate(archive, tmp):
    class Model:
        model = "offline-debate"

        def chat(self, messages, **options):
            assert options["json_mode"] and not options.get("tools")
            frame = json.loads(messages[-1]["content"])
            assert "班次交接" in frame["question"]
            peer = frame["peer_positions"]
            reply = {"content": "建议对照既有记录，明确核查日期和设备。", "references": []}
            if peer:
                reply["target"] = next(iter(peer))
                reply["content"] = "当前观点需明确量测覆盖，班次日志仍待复核。"
            return {"content": json.dumps(reply, ensure_ascii=False)}, {"status": "completed"}

    question = "如何核对班次交接期间的记录一致性？"
    debate = run_parallel_subagent_debate(archive, question, client=Model(), memory_db_path=tmp / "memory.sqlite3")
    assert debate["topic"] == "free_topic" and debate["mode"] == "deepseek"
    llm = debate["model_debate"]
    assert len(llm["positions"]) == len(llm["rebuttals"]) == 3
    assert all(rb["role"] != rb["target"] for rb in llm["rebuttals"])
    assert debate["model_debate"]["requires_human_confirmation"]
    local = run_parallel_subagent_debate(archive, question, memory_db_path=tmp / "memory.sqlite3")
    assert local["mode"] == "local" and local["rebuttals"]
    consult = run_multi_agent_consultation(archive, question, client=Model())
    assert consult["model_debate"]["status"] == "model_debate"
    assert consult["verification"]["status"] == "verified" and consult["report"]["confirmed_facts"]

    class Offline:
        def chat(self, *_args, **_opts):
            raise LLMError("fixture unavailable")

    degraded = run_parallel_subagent_debate(archive, question, client=Offline(), memory_db_path=tmp / "memory.sqlite3")
    assert degraded["mode"] == "local" and degraded["model_debate"]["status"] == "degraded_to_local"


def verify_retrieval_intake(archive):
    assert len({item["id"] for item in load_corpus()}) == 68
    assert search_standards("第四百七十六条")["retrieval_backend"] == "lexical_fallback"

    class Provider:
        def __init__(self):
            self.calls = 0

        def embed(self, texts):
            self.calls += 1
            return [[1, 0] if text in {"无关表达", "目标正文"} else [0, 1] for text in texts]

    provider = Provider()
    dense = DenseEmbeddingCache(provider)
    for _ in range(2):
        hits, backend = rank_documents("无关表达", ["其他正文", "目标正文"], dense=dense)
        assert backend == "dense+bm25" and hits[0]["index"] == 1
    assert provider.calls == 2
    assert ConditionalRAGEngine(archive, embedding_cache=dense).retrieve("第四百七十六条")[
        "retrieval_backend"] == "dense+bm25"

    class InvalidProvider:
        def embed(self, texts):
            return [[float("nan")]] * len(texts)

    assert search_standards("两回路", embedding_provider=InvalidProvider())["retrieval_backend"] == "lexical_fallback"

    image = b"\x89PNG\r\n\x1a\nfixture"

    class Vision:
        def chat(self, messages, **options):
            assert options["json_mode"] and messages[-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
            return {"content": json.dumps({"status": "approved", "candidates": [
                {"field": "额定电流", "value": 180, "unit": "A", "evidence": "铭牌右上角", "approved": True},
                {"field": "未经定位", "value": 42, "unit": "A"},
            ]})}, {}

    intake = extract_field_candidates(image, "image/png", client=Vision())
    assert intake == {"status": "pending_review", "source_kind": "image", "requires_human_confirmation": True,
                      "candidates": [{"field": "额定电流", "value": 180, "unit": "A", "evidence": "铭牌右上角"}]}
    assert extract_field_candidates(image, "image/png")["status"] == "unavailable"
    assert extract_field_candidates(image, "image/jpeg", client=Vision())["status"] == "invalid_image"


def verify_sandbox_limits(archive):
    for attribute in ("__subclasses__", "__bases__", "__mro__", "__globals__", "__builtins__"):
        assert validate_sandbox_code(f"sample.{attribute}")
        assert validate_sandbox_code(f"from math import {attribute}")
    assert run_python_analysis(archive, "print(sum([1, 2, 3]))")["stdout"].strip() == "6"
    with patch("laowadi.code_sandbox.shutil.which", return_value="/test/docker"), patch(
        "laowadi.code_sandbox.subprocess.run", side_effect=FileNotFoundError("fixture")
    ) as runner:
        result = run_python_analysis(archive, "print(1)", container_image="trusted-local-fixture")
        assert result["status"] == "error"
        command = runner.call_args.args[0]
        assert "--network=none" in command and "--read-only" in command and "--memory=2g" in command
        assert any(arg.startswith("type=bind,") and arg.endswith(",readonly") for arg in command)


def main():
    source_dir = ROOT.parent.parent / "项目知识库" / "资料" / "老洼地"
    protected = [DB, EVENT_DB, *sorted(source_dir.glob("*.xls")), *sorted(source_dir.glob("*.png"))]
    before = [sha256(path.read_bytes()).digest() for path in protected]
    with TemporaryDirectory() as directory, closing(connect(DB)) as connection:
        archive = Archive(connection)
        tmp = Path(directory)
        verify_dynamic(tmp)
        verify_autonomy(archive, tmp)
        verify_submission(archive)
        verify_debate(archive, tmp)
        verify_retrieval_intake(archive)
        verify_sandbox_limits(archive)
    assert before == [sha256(path.read_bytes()).digest() for path in protected]
    print("Core engines, structured HITL, evidence boundaries and golden hashes passed.")


if __name__ == "__main__":
    main()
