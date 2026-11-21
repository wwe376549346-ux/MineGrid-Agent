"""Bounded reviews using only archived event, hourly and topology evidence."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import sqlite3

from laowadi.monitor import HOUR, packed, stamp
from laowadi.storage import REPORT_STATION


MAX_CALLS = 4
EVENT, READING, LINK = "get_monitor_event", "get_hourly_reading", "get_supply_link"
TOOLS = (EVENT, READING, LINK)


class SelectionError(ValueError):
    """The model did not return exactly one usable function call."""


class ModelUnavailable(RuntimeError):
    """The configured model could not be reached or returned an invalid response."""


@dataclass(frozen=True)
class Review:
    event: dict
    compare: bool
    cabinets: tuple
    clocks: tuple = ()
    days: tuple = ()
    link: dict | None = None
    logs: tuple = ()


def call(tool, arguments):
    return {"tool": tool, "arguments": arguments}


def aligned_pair(event, review):
    if event["type"] != "power_change":
        return (), "该事件没有一对相邻整点的有功读数。请提供有功变化事件编号。"
    details = event["evidence"]
    pair = tuple(stamp(details[key]["time"]) for key in ("before", "after"))
    if datetime.fromisoformat(pair[1]) - datetime.fromisoformat(pair[0]) != HOUR or pair[1] != event["time"]:
        return (), "事件档案中的两个时刻无法确认为该事件的相邻整点。"
    candidates = (pair[1],) if len(review.clocks) == 1 else pair
    if (len(review.clocks) > 2
            or any(not any(hour == item[11:13] and (day is None or day == item[:10]) for item in candidates)
                   for day, hour in review.clocks)
            or set(review.days) - {item[:10] for item in pair}):
        return (), f"问题中的时段与事件的 {pair[0]} 至 {pair[1]} 未对齐。请确认要比较的整点。"
    # A dated and an undated reference to the same hour must not count as two distinct endpoints.
    if len(review.clocks) == 2 and len({hour for _, hour in review.clocks}) != 2:
        return (), f"请确认要比较的两个整点。该事件使用 {pair[0]} 和 {pair[1]}。"
    return pair, ""


def review_event(archive, question, review, *, selector=None, max_calls=MAX_CALLS):
    if type(max_calls) is not int or not 1 <= max_calls <= MAX_CALLS:
        raise ValueError("每次复核的调用预算须为 1 至 4。")
    steps, comparisons, pending = [], [], []
    result = {"kind": "agent_review", "decision_mode": "model" if selector else "local",
              "max_tool_calls": max_calls, "steps": steps, "comparisons": comparisons, "pending": pending}

    def finish(reason, message=None):
        if message:
            pending.append(message)
        status = {"completed": "ok", "time_alignment": "clarify", "cabinet_required": "clarify"}.get(reason, "partial")
        return {**result, "status": status, "stop_reason": reason, "tool_calls": len(steps)}

    seen = set()

    def advance(options):
        if len(steps) >= max_calls:
            return "budget_exhausted", "调用预算已用完。请单独查询上面尚缺的整点读数或供电关系。"
        try:
            choice = selector(question, deepcopy(options), deepcopy(steps)) if selector else options[0]
        except SelectionError:
            return "invalid_call", "模型未返回一个可执行的只读调用。"
        except ModelUnavailable as exc:
            return "model_error", str(exc)
        if (not isinstance(choice, dict) or choice.keys() != {"tool", "arguments"}
                or not isinstance(choice["tool"], str) or choice["tool"] not in TOOLS
                or not isinstance(choice["arguments"], dict)
                or not all(isinstance(key, str) and isinstance(value, str) for key, value in choice["arguments"].items())):
            return "invalid_call", "工具或参数未获准，本轮停止。"
        key = packed(choice)
        if key in seen:
            return "duplicate_call", "重复的工具及参数已拒绝，本轮停止。"
        if choice not in options:
            return "invalid_call", "工具参数与本轮待查的柜号、时刻或关系不一致，本轮停止。"
        seen.add(key)
        try:
            evidence = archive.call(choice["tool"], choice["arguments"])
        except (ValueError, TypeError, OSError, sqlite3.Error):
            steps.append({**choice, "result": {"status": "error"}})
            return "tool_error", "只读查询失败。请核对日报库和事件库是否可用。"
        steps.append({**choice, "result": evidence})
        return None

    if failure := advance([call(EVENT, review.event)]):
        return finish(*failure)
    event_result = steps[0]["result"]
    if event_result["status"] != "ok":
        return finish("needs_evidence", "请核对事件编号及回放范围，或先完成对应日期的回放归档。")

    event = event_result["event"]
    pending.extend(review.logs)
    options = []
    pair = ()
    cabinets = tuple(cabinet for cabinet in review.cabinets if cabinet != event["cabinet"])
    if review.compare:
        if not cabinets:
            return finish("cabinet_required", "请指定要与该事件比较的另一柜号。")
        pair, issue = aligned_pair(event, review)
        if not pair:
            reason = "time_alignment" if event["type"] == "power_change" else "needs_evidence"
            return finish(reason, issue)
        options.extend(call(READING, {"station": REPORT_STATION, "cabinet": cabinet, "metric": "P", "at": hour})
                       for cabinet in cabinets for hour in pair)
    if review.link:
        options.append(call(LINK, review.link))

    failure = None
    while options and not failure:
        failure = advance(options)
        if not failure:
            options.remove(call(steps[-1]["tool"], steps[-1]["arguments"]))

    readings = {(row["arguments"]["cabinet"], row["arguments"]["at"]): row["result"]
                for row in steps if row["tool"] == READING and row["result"]["status"] != "error"}
    for cabinet in cabinets if pair else ():
        before, after = (readings.get((cabinet, hour)) for hour in pair)
        if before is None or after is None:
            missing = "、".join(hour for hour in pair if (cabinet, hour) not in readings)
            pending.append(f"尚待查询 {cabinet} 柜有功整点 {missing}。")
            continue
        if any(row["status"] != "ok" for row in (before, after)):
            missing = "、".join(hour for hour, row in zip(pair, (before, after)) if row["status"] != "ok")
            pending.append(f"需补齐 {cabinet} 柜有功整点 {missing} 的读数。")
            continue
        event_sources = [event["evidence"][part]["source"]["sha256"] for part in ("before", "after")]
        if [(row["time"], row["source"]["sha256"]) for row in (before, after)] != list(zip(pair, event_sources)):
            pending.append(f"{cabinet} 柜读数与事件档案的时刻或日报版本不一致，需核对原始日报。")
            continue
        change = Decimal(str(after["value"])) - Decimal(str(before["value"]))
        comparisons.append({"cabinet": cabinet, "before": before, "after": after, "change_kw": float(change)})

    links = [row["result"] for row in steps if row["tool"] == LINK]
    if review.link:
        cabinet = review.link["cabinet"]
        if not links or links[0]["status"] == "error":
            pending.append(f"尚待查询 {cabinet} 柜的供电关系。")
        elif links[0]["status"] != "ok":
            pending.append(f"需核对 {cabinet} 柜的供电图或接线台账。")
    if failure:
        return finish(*failure)
    return finish("needs_evidence" if pending else "completed")


def render_review(result, render):
    lines = [render(step["result"]) for step in result["steps"] if step["result"]["status"] != "error"]
    for item in result["comparisons"]:
        change = f'{item["change_kw"]:+,.8f}'.rstrip("0").rstrip(".")
        lines.append(f'{item["cabinet"]} 柜在该事件的同一对整点之间，有功变化 {change} kW。')
    return "\n".join([*lines, *result["pending"]])
