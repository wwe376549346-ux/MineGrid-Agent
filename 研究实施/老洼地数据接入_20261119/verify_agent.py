"""Verify review routing, evidence, limits and the native tool-call protocol."""

import argparse
from contextlib import closing
from io import BytesIO
import json
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory
from unittest.mock import patch

from laowadi.agent import EVENT, LINK, READING
from laowadi.model import select_with_model
from laowadi.monitor import run_monitor
from laowadi.queries import Archive, render
from laowadi.questions import QuestionSession
from laowadi.storage import DB, connect, sha
from laowadi.xls import need


def verify_protocol(archive, question):
    requests = []

    def reply(request, *, timeout):
        wire = json.loads(request.data)
        state = json.loads(wire["messages"][-1]["content"])
        advertised = {item["function"]["name"] for item in wire["tools"]}
        need(request.full_url == "http://127.0.0.1:1/v1/chat/completions" and timeout == 30
             and wire["tool_choice"] == "required", "OpenAI-compatible request")
        need(advertised == {option["tool"] for option in state["allowed_calls"]}, "Only available functions advertised")
        requests.append(state)
        selected = state["allowed_calls"][-1]
        tool_call = {"id": f"call_{len(requests)}", "type": "function",
                     "function": {"name": selected["tool"], "arguments": json.dumps(selected["arguments"])}}
        return BytesIO(json.dumps({"choices": [{"message": {"role": "assistant", "tool_calls": [tool_call]}}]}).encode())

    config = {"COAL_API_BASE_URL": "http://127.0.0.1:1/v1", "COAL_API_MODEL": "protocol-fixture"}
    with patch.dict("os.environ", config, clear=True), patch("laowadi.model.build_opener") as opener:
        opener.return_value.open.side_effect = reply
        answer = QuestionSession(archive, selector=select_with_model).ask(question)
        need(answer["decision_mode"] == "model" and answer["stop_reason"] == "completed"
             and [step["tool"] for step in answer["steps"]] == [EVENT, LINK, READING, READING],
             "Model can change the choice order within the review")
        completed = [{key: step[key] for key in ("tool", "arguments")} for step in answer["steps"]]
        need([state["acquired"] for state in requests] == [completed[:i] for i in range(4)]
             and all(state["question"] == question for state in requests), "Only executed call parameters reach the next model turn")
        opener.return_value.open.side_effect = None
        opener.return_value.open.return_value = BytesIO(
            b'{"choices":[{"message":{"role":"assistant","content":"Fabricated answer","tool_calls":[]}}]}')
        invalid = QuestionSession(archive, selector=select_with_model).ask(question)
        need(invalid["stop_reason"] == "invalid_call" and invalid["tool_calls"] == 0
             and "Fabricated answer" not in render(invalid), "Unstructured model prose is not returned as evidence")
    with patch.dict("os.environ", {}, clear=True):
        unavailable = QuestionSession(archive, selector=select_with_model).ask(question)
        need(unavailable["stop_reason"] == "model_error" and unavailable["tool_calls"] == 0,
             "Missing model configuration is explicit")
    return len(requests)


def verify(source, directory):
    events = directory / "events.sqlite3"
    with closing(connect(source)) as db:
        archive = Archive(db, events)
        real = run_monitor(archive, events)
        simulated = run_monitor(archive, events, skip_hour="2026-11-10 10:00:00")
        before = sha(events.read_bytes())
        session = QuestionSession(archive)
        direct = session.ask("11月10日12点303柜为什么提示变化？")
        identifier = direct["event"]["id"]
        need(direct["tool"] == EVENT and "steps" not in direct and direct["event"]["run_id"] == real["run_id"],
             "Simple event question keeps the original direct route")
        question = f"{identifier}，306柜同期有功也变化了吗？"
        full_question = question + "再查1009柜下游。"
        answer = session.ask(full_question)
        need(answer["stop_reason"] == "completed" and answer["tool_calls"] == 4
             and [step["tool"] for step in answer["steps"]] == [EVENT, READING, READING, LINK],
             "Four-call local path through all three tools")
        item, = answer["comparisons"]
        need((item["cabinet"], item["change_kw"], item["before"]["value"], item["after"]["value"]) ==
             ("306", -204.41, 427.54, 223.13), "Comparison uses the signed difference")
        cells = ["D15", "D16", "H15", "H16"]
        need(all(f"New Sheet1!{cell}" in render(answer) for cell in cells), "Both cabinets cite both original cells")
        link, = answer["steps"][-1]["result"]["links"]
        need(link["to"]["number"] == "1101" and len(link["sources"]) == 2, "Topology preserves both diagram sources")
        for text in ("306柜同期有功呢？", "11月10日12点303柜提示变化，306柜同一对整点呢？",
                     f"{identifier}，比较306柜11月10日11点和12点的有功。"):
            need(session.ask(text)["comparisons"] == [item], "Explicit and follow-up event anchors agree")

        midnight = session.ask("11月11日0点306柜提示变化，比较303柜23点和0点。")
        need(midnight["stop_reason"] == "completed" and
             [step["arguments"]["at"] for step in midnight["steps"][1:]] ==
             ["2026-11-10 23:00:00", "2026-11-11 00:00:00"], "Alignment uses both actual dates across midnight")
        for text in (f"{identifier}，比较306柜13点的有功。", f"{identifier}，比较306柜11月11日12点的有功。"):
            unaligned = session.ask(text)
            need(unaligned["stop_reason"] == "time_alignment" and unaligned["tool_calls"] == 1
                 and not unaligned["comparisons"], "Mismatched clock or date stops before reading")
        for text in (f"{identifier}，306柜12:30同期有功？", f"{identifier}，306柜半小时后也变化了吗？",
                     f"{identifier}，比较中央变电所306柜同期有功。", f"{identifier}，把306柜分闸。"):
            need(session.ask(text)["kind"] == "clarify", "Off-hour, device mismatch and control commands are rejected")
        cause = session.ask(question + "是不是跳闸导致的？")
        need(cause["stop_reason"] == "needs_evidence" and cause["comparisons"] == [item]
             and all(word in render(cause) for word in ("运行日志", "开关变位", "保护动作")),
             "Missing logs are explicit while confirmed evidence remains")
        limited = QuestionSession(archive, max_tool_calls=2).ask(question)
        need(limited["stop_reason"] == "budget_exhausted" and limited["tool_calls"] == 2
             and not limited["comparisons"] and item["after"]["time"] in render(limited),
             "Budget stop keeps acquired evidence and names the pending time")
        over_budget = session.ask(question + "同时对照1009柜。")
        need(over_budget["stop_reason"] == "budget_exhausted" and over_budget["tool_calls"] == 4,
             "Default four-call limit stops a longer request")

        gap = Archive(db, events, simulated["run_id"]).monitor_event(event_type="hourly_gap")["event"]
        gap_answer = session.ask(f"{gap['id']}，比较306柜同期有功。")
        need(gap_answer["stop_reason"] == "needs_evidence" and gap_answer["tool_calls"] == 1
             and "模拟缺测回放" in render(gap_answer), "Simulated gap is identified and cannot provide a valid pair")
        missing_events = directory / "missing.sqlite3"
        missing = QuestionSession(Archive(db, missing_events)).ask(question)
        need(missing["stop_reason"] == "needs_evidence" and missing["tool_calls"] == 1 and not missing_events.exists(),
             "Event lookup does not create an archive")

        def duplicate(_, options, steps):
            return {key: steps[0][key] for key in ("tool", "arguments")} if steps else options[0]

        def wrong_hour(_, options, steps):
            if steps:
                options[0]["arguments"]["at"] = "2026-11-10 13:00:00"
            return options[0]

        for choose, reason, count in ((duplicate, "duplicate_call", 1), (wrong_hour, "invalid_call", 1),
                (lambda *_: {"tool": "get_daily_statistic", "arguments": {}}, "invalid_call", 0)):
            rejected = QuestionSession(archive, selector=choose).ask(question)
            need(rejected["stop_reason"] == reason and rejected["tool_calls"] == count, "Disallowed action does not reach SQLite")
        request_count = verify_protocol(archive, full_question)
        need(sha(events.read_bytes()) == before, "Event archive unchanged by all review queries")

    copy = directory / "modified_reports.sqlite3"
    copyfile(source, copy)
    with closing(connect(copy, writable=True)) as db, db:
        db.execute("UPDATE reading SET value=NULL WHERE time='2026-11-10 12:00:00' AND cabinet='306' AND metric='P'")
    with closing(connect(copy)) as db:
        incomplete = QuestionSession(Archive(db, events)).ask(question)
        need(incomplete["stop_reason"] == "needs_evidence" and incomplete["steps"][-1]["result"]["status"] == "missing_value"
             and not incomplete["comparisons"], "A null reading remains unavailable")
    with closing(connect(copy, writable=True)) as db, db:
        db.execute("UPDATE reading SET value=223.13 WHERE time='2026-11-10 12:00:00' AND cabinet='306' AND metric='P'")
        db.execute("UPDATE report SET sha256=? WHERE day='2026-11-10'", ("0" * 64,))
    with closing(connect(copy)) as db:
        version = QuestionSession(Archive(db, events)).ask(question)
        need(version["stop_reason"] == "needs_evidence" and not version["comparisons"] and "日报版本" in render(version),
             "Do not combine data from mismatched report versions")
    return {"example_event": identifier, "change_kw": item["change_kw"], "cells": cells,
            "local_tool_calls": answer["tool_calls"], "protocol_fixture_requests": request_count,
            "event_archive_unchanged": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB)
    args = parser.parse_args()
    before = sha(args.db.read_bytes())
    with TemporaryDirectory() as directory:
        result = verify(args.db, Path(directory))
    need(sha(args.db.read_bytes()) == before, "Hourly archive unchanged by all review queries")
    print(json.dumps({**result, "source_database_unchanged": True}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
