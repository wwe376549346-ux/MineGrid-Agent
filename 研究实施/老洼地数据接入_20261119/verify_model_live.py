"""Compare the fixed Laowadi task set locally and, optionally, with a real model."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sys
from time import monotonic

from laowadi.agent import EVENT, LINK, MAX_CALLS
from laowadi.model import load_project_model_config, select_with_model
from laowadi.monitor import EVENT_DB
from laowadi.queries import Archive, render
from laowadi.questions import QuestionSession
from laowadi.storage import DB, connect, sha


RUN = "LWR-e0c6b18a0d5023d4"
EVENT_ID = "LW-000010"
BEFORE = "2026-11-10 11:00:00"
AFTER = "2026-11-10 12:00:00"
COMP_306 = ("306", -204.41, (BEFORE, "1110.xls", "H15"), (AFTER, "1110.xls", "H16"))
COMP_1009 = ("1009", 459.92, (BEFORE, "1110.xls", "BD15"), (AFTER, "1110.xls", "BD16"))
CROSS_DAY = ("303", -652.33, ("2026-11-10 23:00:00", "1110.xls", "D27"), ("2026-11-11 00:00:00", "1111.xls", "D4"))
CASES = (
    dict(id="Q01", question="LW-000010，306柜同期有功也变化了吗？再查1009柜下游。",
         reason="completed", calls=4, comparison=COMP_306, link=("1009", "1101")),
    dict(id="Q02", question="11月16日303柜有功最大值是多少？按整点。",
         reason="ok", calls=0, statistic=(4969.78, "1116.xls", "D19")),
    dict(id="Q03", question="LW-000010，比较306柜同期有功。是不是跳闸导致的？",
         reason="needs_evidence", calls=3, comparison=COMP_306, pending=("运行日志", "开关变位", "保护动作")),
    dict(id="Q04", question="LW-000010，比较306柜13点的有功。", reason="time_alignment", calls=1),
    dict(id="Q05", question="LW-000010，比较306柜11月11日12点的有功。", reason="time_alignment", calls=1),
    dict(id="Q06", question="11月11日0点306柜提示变化，比较303柜23点和0点。",
         reason="completed", calls=3, comparison=CROSS_DAY,
         event={"cabinet": "306", "event_type": "power_change", "at": "2026-11-11 00:00:00"}),
    dict(id="Q07", question="LW-999999，比较306柜同期有功。", reason="needs_evidence", calls=1,
         event={"event_id": "LW-999999"}, event_status="missing"),
    dict(id="Q08", question="LW-000010，比较306柜同期有功。再查1010柜下游。",
         reason="completed", calls=4, comparison=COMP_306, link=("1010", "1201")),
    dict(id="Q09", question="LW-000010，比较1009柜同期有功。", reason="completed", calls=3, comparison=COMP_1009),
    dict(id="Q10", question="LW-000010，306柜12:30同期有功？", reason="clarify", calls=0),
    dict(id="Q11", question="LW-000010，比较306柜和1009柜同期有功，请优先查齐1009柜两个整点。",
         reason="budget_exhausted", calls=4, comparison=COMP_1009, pending=("尚待查询 306 柜",)),
    dict(id="Q12", question="LW-000010，比较1009柜和306柜同期有功，请优先查齐306柜两个整点。",
         reason="budget_exhausted", calls=4, comparison=COMP_306, pending=("尚待查询 1009 柜",)),
)


def match_comparison(item):
    endpoints = tuple((point["time"], point["source"]["file"], point["source"]["cell"])
                      for point in (item["before"], item["after"]))
    return item["cabinet"], item["change_kw"], *endpoints


def grade(case, answer):
    reason = answer.get("stop_reason", answer.get("status", answer["kind"]))
    steps = answer.get("steps", [])
    comparison = [match_comparison(item) for item in answer.get("comparisons", [])]
    links = [(link["from"]["number"], link["to"]["number"])
             for step in steps if step["tool"] == LINK
             for link in step["result"].get("links", []) if len(link["sources"]) == 2]
    checks = {
        "stop_and_budget": reason == case["reason"] and len(steps) == case["calls"],
        "aligned_evidence": comparison == ([case["comparison"]] if "comparison" in case else []),
        "topology": links == ([case["link"]] if "link" in case else []),
        "pending": all(item in "\n".join(answer.get("pending", [])) for item in case.get("pending", ())),
    }
    if case["calls"]:
        expected_args = case.get("event", {"event_id": EVENT_ID})
        checks["event_lookup"] = bool(steps and steps[0]["tool"] == EVENT and steps[0]["arguments"] == expected_args
                                      and steps[0]["result"]["status"] == case.get("event_status", "ok"))
    if "statistic" in case:
        source = answer.get("source", {})
        checks["hourly_peak"] = (answer.get("value"), source.get("file"), source.get("cell")) == case["statistic"]
    return checks


def evaluate(case, modes):
    results = {}
    with closing(connect(DB)) as db:
        for mode in modes:
            start = monotonic()
            elapsed = []

            def timed_query(question, options, steps):
                begin = monotonic()
                try:
                    return select_with_model(question, options, steps)
                finally:
                    elapsed.append(round(monotonic() - begin, 3))

            selector = timed_query if mode == "model" else None
            answer = QuestionSession(Archive(db, EVENT_DB, RUN), selector=selector, max_tool_calls=MAX_CALLS).ask(case["question"])
            checks = grade(case, answer)
            result = dict(passed=all(checks.values()), checks=checks, elapsed_seconds=round(monotonic() - start, 3),
                          request_seconds=elapsed, answer=answer, text=render(answer))
            results[mode] = result
            print(f'{case["id"]} {mode} {"通过" if result["passed"] else "未通过"} '
                  f'({result["elapsed_seconds"]:.2f} s, {len(elapsed)} 次模型请求)', file=sys.stderr, flush=True)
    return {"case": case, "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="explicit local credential file for live model requests")
    parser.add_argument("--smoke", action="store_true", help="run the first case only")
    args = parser.parse_args()
    modes = ("local",)
    if args.config:
        os.environ.update(load_project_model_config(args.config))
        modes += ("model",)
    archive_hashes = {path.name: sha(path.read_bytes()) for path in (DB, EVENT_DB)}
    with ThreadPoolExecutor(max_workers=2 if args.config else 1) as pool:
        cases = CASES[:1] if args.smoke else CASES
        results = list(pool.map(lambda case: evaluate(case, modes), cases))
    unchanged = archive_hashes == {path.name: sha(path.read_bytes()) for path in (DB, EVENT_DB)}
    summary = {mode: sum(item["results"][mode]["passed"] for item in results) for mode in modes}
    print(json.dumps({"run_id": RUN, "max_tool_calls": MAX_CALLS, "model": os.environ.get("COAL_API_MODEL") if args.config else None,
                      "source_hashes": archive_hashes, "archives_unchanged": unchanged, "passed": summary, "cases": results},
                     ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if unchanged else 1


if __name__ == "__main__":
    raise SystemExit(main())
