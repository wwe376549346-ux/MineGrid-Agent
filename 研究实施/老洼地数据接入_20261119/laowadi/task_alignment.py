"""Resolve concrete questions before general health screening can select evidence."""

from datetime import date
from decimal import Decimal
import re

from laowadi.agent import MAX_CALLS, EVENT, READING, LINK
from laowadi.context import _BARE_DAY_RE
from laowadi.questions import (
    CAUSE, COMPARE, EVENT_ID, EVENT_REF, METRICS, TOPOLOGY, DATE, QuestionSession,
    clock_mentions, match_term, normalized, time_on,
)
from laowadi.queries import render
from laowadi.storage import REPORT_STATION
from laowadi.verifier import _cells, _lookup_cell_db_record, bind_tool_fact, build_observations


_DAILY = re.compile(r"最大(?:值)?|最小(?:值)?|最高(?:值)?|最低(?:值)?|峰值|平均(?:值)?")
_FOOTER = re.compile(r"页尾|表尾|日报统计|日报所列|报表统计|原表(?:最大|最小|平均)")
_HOURLY = re.compile(r"整点|逐小时|小时样本|小时记录|按小时|重算")
_COMPARISON = re.compile(r"比较|对比|对照|哪.{0,12}(?:更高|更低|最大|最小)|高多少|低多少|差多少|差值")
_BROAD = re.compile(
    r"异常|故障|过压|平衡|健康|风险|全站|全周|近[一二三四五六七八九十0-9]+天|排名|前[一二三四五六七八九十0-9]+|筛选|阈值|反向|倒送|"
    r"波动|趋势|标准差|累计|估算|仿真|规程|容量|超限|负荷率|单进线|双主变|裕度|转供|补偿|潮流|简报|区间|从.{0,18}到.{0,8}(?:点|时)|总和|合计|均值名次"
)
_CABINET = re.compile(r"(?<![0-9])(\d+)号?柜")
_LOOKUP = re.compile(r"查询|^查|^帮我查|^给出|是多少|读数|请给|原表|出处|哪个更[高低]|哪天更[高低]")
_SCOPE_FOLLOWUP = re.compile(r"^(?:那|改|换)|同期|同一时刻|当时|刚才|这条|页尾的|整点的")


def _bound(statement, step, *path, kind="calculation"):
    value = step["result"]
    for key in path:
        value = value[key]
    return bind_tool_fact({"statement": statement, "evidence": sorted(_cells(value))},
                          step, *path, kind=kind)


def _result(kind, status, reason, scope, trace, facts, gaps):
    summary = " ".join([*(fact["statement"] for fact in facts), *gaps])
    return {
        "status": {"completed": "ok", "clarification_required": "clarify"}.get(status, status),
        "stop_reason": reason,
        "task_alignment": {"kind": kind, "status": status, "scope": scope, "gaps": gaps},
        "report": {"summary": summary, "confirmed_facts": facts, "missing_evidence": gaps,
                   "hypotheses": [], "field_sop": [], "cited_standards": []},
        "tool_trace": trace,
        "rounds": int(bool(trace)),
    }


def _event_facts(event, step, archive):
    if event["type"] == "power_change":
        for part in ("before", "after"):
            entry = event["evidence"][part]
            refs = _cells(entry)
            row = _lookup_cell_db_record(archive, next(iter(refs))) if len(refs) == 1 else None
            if not row or (row["station"], row["cabinet"], row["time"], row["metric"],
                           row["value"], row["sha256"]) != (
                REPORT_STATION, event["cabinet"], entry["time"], "P",
                entry["value_kw"], entry["source"]["sha256"],
            ):
                return []
    return [_bound(render(step["result"]), step, "event")]


def _review_event(archive, toolbox, text, identifiers, current, budget, selector):
    kind = "event_review"
    scope = {"events": identifiers, "days": current["days"], "hours": current["hours"],
             "cabinets": current["cabinets"]}
    if len(identifiers) > 1:
        return _result(kind, "clarification_required", "multiple_events", scope, [], [],
                       ["请先指定一条要复核的监测事件。"])
    event_id = identifiers[0]
    if not EVENT_ID.search(text):
        text = (f"{event_id}，比较{text}同期" if current["cabinets"] and not COMPARE.search(text)
                and not TOPOLOGY.search(text) else f"{event_id}，{text}")
    if not budget:
        return _result(kind, "insufficient_data", "budget_exhausted", scope, [], [], ["本轮无可用查询预算。"])
    answer = QuestionSession(archive, selector=selector, max_tool_calls=min(MAX_CALLS, budget)).ask(text)
    trace = answer.get("steps", [])
    if answer["kind"] == "clarify":
        return _result(kind, "clarification_required", "event_scope_clarification", scope, [], [], [answer["message"]])
    if answer["kind"] == "monitor_event":
        trace = [{"tool": EVENT, "arguments": answer["arguments"], "result": answer}]

    event = trace[0]["result"].get("event") if trace else None
    gaps = list(answer.get("pending", []))
    reason = answer.get("stop_reason", "completed" if event else "event_not_found")
    if not event:
        return _result(kind, "insufficient_data", reason, scope, trace, [],
                       gaps or ["指定事件未在当前回放中找到，请核对编号及回放范围。"])

    stamp = event["time"]
    try:
        clocks = {time_on(stamp[:10], hit)[11:] for hit in clock_mentions(text)}
    except ValueError:
        clocks = {"invalid"}
    # The review tool additionally checks date/hour pairs for explicit comparisons.
    before = event["evidence"].get("before", {}).get("time")
    event_times = [before, stamp] if before else [stamp]
    valid_times = event_times if len(clocks) > 1 else [stamp]
    if (reason == "time_alignment" or set(current["days"]) - {at[:10] for at in event_times}
            or clocks - {at[11:] for at in valid_times} or re.search(r"半点|[时点]半", text)):
        return _result(kind, "clarification_required", "time_alignment", scope, trace, [],
                       gaps if reason == "time_alignment" else
                       [f"指定的日期或时刻与 {event_id} 的 {' 至 '.join(event_times)} 未对齐，请确认查询时段。"])
    if reason == "cabinet_required":
        return _result(kind, "clarification_required", reason, scope, trace, [], gaps)

    facts = _event_facts(event, trace[0], archive)
    if not facts:
        return _result(kind, "insufficient_data", "event_source_mismatch", scope, trace, [],
                       ["事件档案与当前日报的原表读数或版本不一致，待重放核对。"])
    if (current["cabinets"] and event["cabinet"] not in current["cabinets"]
            and not COMPARE.search(text) and not TOPOLOGY.search(text)):
        return _result(kind, "clarification_required", "cabinet_alignment", scope, trace, [],
                       [f"{event_id} 属于 {event['cabinet']} 柜，请确认是否要对照另一柜的同期有功。"])
    scope.update(days=list(dict.fromkeys(at[:10] for at in event_times)),
                 hours=[at[11:16] for at in event_times], times=event_times,
                 cabinets=current["cabinets"] or ([event["cabinet"]] if event["cabinet"] else []),
                 stations=[REPORT_STATION], metrics=["P"], statistics=[])
    # Comparisons returned by the event tool already match both timestamps and report revisions.
    for item in answer.get("comparisons", []):
        start, end = item["before"], item["after"]
        step = {"tool": "compare_aligned_readings", "record_type": "calculation", "arguments": {"event_id": event_id, "cabinet": item["cabinet"]},
                "result": {"status": "ok", "comparison": item, "formula": "P(后) − P(前)"}}
        trace.append(step)
        facts.append(_bound(f'{item["cabinet"]} 柜同期有功从 {start["time"]} 的 {start["value"]} kW '
                            f'变为 {end["time"]} 的 {end["value"]} kW，变化 {item["change_kw"]:+g} kW。',
                            step, "comparison"))
    for step in trace:
        if step["tool"] == LINK and step["result"]["status"] == "ok":
            facts.append(_bound(render(step["result"]), step, kind="coverage"))
    if "基线" in text:
        args = {"day": stamp[:10], "hour": stamp[11:16]}
        if sum(step.get("record_type") != "calculation" for step in trace) < budget and event["type"] == "power_change":
            baseline = toolbox.get_diurnal_baseline(**args)
            step = {"tool": "get_diurnal_baseline", "arguments": args, "result": baseline}
            trace.append(step)
            for index, row in enumerate(baseline.get("day_comparison", {}).get("hours", [])):
                facts.append(_bound(f'{row["time"]} 地面站 303、306 柜合计有功 {row["total_p_kw"]} kW，'
                                    f'归档同钟点基线中位数 {row["baseline_median_kw"]} kW，'
                                    f'偏差 {row["deviation_kw"]:+g} kW。',
                                    step, "day_comparison", "hours", index))
        gaps.append("本批日报仅能对照归档同钟点基线，需结合更多运行样本及同期记录判定是否为常规波动。")
    if event["mode"] == "simulated_gap":
        gaps.append("该条为模拟缺测回放，实际现场缺测尚需现场资料确认。")
    if CAUSE.search(text) and not answer.get("pending"):
        gaps.append("需核对同期运行日志、开关变位（SOE）及保护动作记录，现场原因待复核。")
    status = "partial" if gaps or reason != "completed" else "completed"
    return _result(kind, status, reason if reason != "completed" else "needs_evidence" if gaps else reason,
                   scope, trace, facts, gaps)


def _points(text, anchors):
    clocks = list(clock_mentions(text))
    days = anchors["days"]
    if not clocks:
        return [(day, hour) for day in days for hour in anchors["hours"]]
    hours = [time_on("2000-01-01", clock)[11:] for clock in clocks]
    if any(hour[3:] != "00:00" for hour in hours) or re.search(r"半点|[时点]半", text):
        raise ValueError("日报查询以整点为单位，请确认要查的整点。")
    hours = [hour[:5] for hour in hours]
    if len(days) <= 1 or len(set(hours)) == 1:
        return list(dict.fromkeys((day, hour) for day in days for hour in hours))

    # Multiple distinct clocks with multiple dates require an explicit date for each clock.
    # Use equal-width masking so offsets remain relative to the original question.
    markers = [(hit.start(), hit.end(), hit[0]) for hit in DATE.finditer(text)]
    scrubbed = DATE.sub(lambda hit: " " * len(hit[0]), text)
    markers.extend((hit.start(), hit.end(), hit[0]) for hit in _BARE_DAY_RE.finditer(scrubbed))
    markers.sort()
    dated = []
    for clock, hour in zip(clocks, hours):
        marker = next((label for _, end, label in reversed(markers) if end <= clock.start()), "")
        if not marker:
            break
        if len(marker) == 10 and "-" in marker:
            matches = [day for day in days if day == marker]
        else:
            match = DATE.fullmatch(marker)
            if match:
                matches = [day for day in days if int(day[-2:]) == int(match["day"])
                           and int(day[5:7]) == int(match["month"])
                           and (not match["year"] or day[:4] == match["year"])]
            else:
                matches = [day for day in days if int(day[-2:]) == int(marker[:-1])]
        if len(matches) != 1:
            break
        dated.append((matches[0], hour))
    if len(dated) != len(hours) or set(days) != {day for day, _ in dated}:
        raise ValueError("请为每个时刻补充对应的日期，以便逐项核对。")
    return list(dict.fromkeys(dated))


def _find_devices(archive, text, anchors, current):
    cabinets = set(anchors["cabinets"]) | set(_CABINET.findall(text))
    parser = QuestionSession(archive)
    if len(cabinets) <= 1 and (current["cabinets"] or _CABINET.search(text) or not cabinets):
        return [parser.pick_device(text, for_reading=True, use_context=False)]
    for cabinet in cabinets:
        if sum(number == cabinet for _, number in archive.devices) != 1:
            raise ValueError(f"请核对 {cabinet} 柜的编号及所属变电站。")
    devices = [key for key in archive.devices if key[1] in cabinets]
    explicit_stations = {station for station, spec in archive.spec["stations"].items()
                         if any(match_term(alias, text) for alias in spec["aliases"])}
    if explicit_stations and not {station for station, _ in devices} <= explicit_stations:
        raise ValueError("站名与所列柜号归属不一致，请分别写明柜号和所属变电站。")
    return devices


def _comparison(rows, kind, trace):
    if len(rows) < 2:
        return []
    if len({row["basis"] for row in rows}) != 1:
        return []
    ordered = sorted(rows, key=lambda row: row["value"])
    low, high = ordered[0], ordered[-1]
    diff = Decimal(str(high["value"])) - Decimal(str(low["value"]))
    def label(row):
        return f'{row.get("time", row.get("day"))} {row["device"]["number"]} 柜'

    if diff == 0:
        statement = f'所请求各项的{high["metric_name"]}相同。'
    elif len(rows) == 2:
        statement = f'{label(high)}的{high["metric_name"]}比 {label(low)}高 {diff:g} {high["unit"]}。'
    else:
        statement = (f'所请求各项中，{label(high)}的{high["metric_name"]}最高，'
                     f'{label(low)}最低，相差 {diff:g} {high["unit"]}。')
    step = {"tool": "compare_scoped_values", "record_type": "calculation", "arguments": {"kind": kind},
            "result": {"status": "ok", "values": rows, "difference": str(diff), "formula": "最大值 − 最小值"}}
    trace.append(step)
    return [_bound(statement, step, "values",
                   kind="daily_footer" if high.get("basis") == "footer" else "calculation")]


def execute_scoped_question(archive, toolbox, question, anchors, current, *, budget, emit, selector=None):
    text = normalized(question)
    explicit = [hit[0].upper() for hit in EVENT_ID.finditer(text)]
    event_ids = list(dict.fromkeys(explicit or [
        event for event in anchors["events"]
        if event.startswith("LW-") and (EVENT_REF.search(text)
            or not current["days"] and not list(clock_mentions(text))
            and (COMPARE.search(text) or current["cabinets"]))
    ]))
    if event_ids:
        result = _review_event(archive, toolbox, text, event_ids, current, budget, selector)
    else:
        result = _measure(archive, text, anchors, current, budget)
    if result:
        for index, step in enumerate(result["tool_trace"], 1):
            emit("tool_start", round=1, step=index, tool=step["tool"], arguments=step["arguments"])
            emit("tool_end", round=1, step=index, tool=step["tool"], status=step["result"]["status"])
    return result


def _measure(archive, text, anchors, current, budget):
    metrics = [metric for metric, terms in METRICS.items() if any(match_term(term, text) for term in terms)]
    followup = bool(_SCOPE_FOLLOWUP.search(text))
    if _BROAD.search(text) or TOPOLOGY.search(text):
        return None
    if not metrics and followup:
        metrics = anchors.get("metrics") or []
    underground = current["stations"] == ["C1535"] or anchors["stations"] == ["C1535"]
    if not metrics and underground and "读数" in text:
        metrics = list(METRICS)
    if not metrics:
        return None
    statistics = {_statistic(hit[0]) for hit in _DAILY.finditer(text)}
    explicit_clock = bool(list(clock_mentions(text)) or "半点" in text)
    if not statistics and not explicit_clock and followup and (_FOOTER.search(text) or _HOURLY.search(text)):
        statistics = set(anchors.get("statistics") or [])
    compare = bool(_COMPARISON.search(text))
    if (compare and anchors["hours"] and len(statistics) == 1
            and re.search(r"(?:哪.{0,12}更[高低]|更[高低]|高多少|低多少|差值)", text)):
        statistics.clear()
    if not (statistics or explicit_clock or anchors["hours"] or
            (anchors["days"] or anchors["cabinets"]) and (compare or underground or _LOOKUP.search(text))):
        return None

    kind = "daily_statistic" if statistics else "point_reading"
    scope = {"days": anchors["days"], "hours": anchors["hours"], "cabinets": anchors["cabinets"],
             "metrics": metrics, "statistics": sorted(statistics), "events": []}
    trace, facts, gaps, rows = [], [], [], []
    try:
        for day in scope["days"]:
            date.fromisoformat(day)
        if not scope["days"]:
            raise ValueError("请指定查询日期。")
        devices = _find_devices(archive, text, anchors, current)
        scope["cabinets"] = [cabinet for _, cabinet in devices]
        scope["stations"] = list(dict.fromkeys(station for station, _ in devices))
        if statistics:
            if explicit_clock:
                raise ValueError("请确认要查询当日统计，还是指定时刻的整点读数。")
            footer, hourly = _FOOTER.search(text), _HOURLY.search(text)
            bases = []
            if hourly or not (footer or "日报" in text or "报表" in text):
                bases.append("hourly")
            if footer or not hourly:
                bases.append("footer")
            scope["bases"] = bases
            scope["hours"] = []
            queries = [("get_daily_statistic", {"station": station, "cabinet": cabinet, "metric": metric,
                                               "day": day, "basis": basis, "statistic": statistic})
                       for day in scope["days"] for station, cabinet in devices for metric in metrics
                       for statistic in scope["statistics"] for basis in bases]
        else:
            points = _points(text, anchors)
            if not points:
                if not all(station != REPORT_STATION for station, _ in devices):
                    raise ValueError("请明确查询整点，或指定所需的统计口径。")
                points = [(day, None) for day in scope["days"]]
            scope["hours"] = list(dict.fromkeys(hour for _, hour in points if hour))
            scope["times"] = [f"{day} {hour}:00" for day, hour in points if hour]
            if len(points) > 1 and len(devices) > 1:
                raise ValueError("请分开指定各柜对应的日期和整点，或按同一时刻比较柜号。")
            queries = [(READING, {"station": station, "cabinet": cabinet, "metric": metric,
                                  **({"at": f"{day} {hour}:00"} if hour else {"day": day})})
                       for day, hour in points for station, cabinet in devices for metric in metrics]
    except ValueError as exc:
        return _result(kind, "clarification_required", "query_scope_clarification", scope, trace, facts, [str(exc)])

    available = set(archive.days())
    unavailable_stations = set()
    for tool, args in queries:
        day = args.get("day") or args["at"][:10]
        if day not in available:
            gaps.append(f"缺少所查日期 {day} 的运行日报。")
            continue
        if args["station"] != REPORT_STATION:
            unavailable_stations.add((args["station"], args["cabinet"]))
            continue
        if len(trace) >= budget:
            gaps.append("本轮查询预算已用完，仍有指定读数或统计待查。")
            break
        row = archive.call(tool, args)
        step = {"tool": tool, "arguments": args, "result": row}
        trace.append(step)
        if row["status"] != "ok":
            gaps.append(render(row))
            continue
        rows.append(row)
        if tool == READING:
            observations = build_observations(archive, sorted(_cells(row)), [step])
            if observations:
                facts.extend(observations)
            else:
                gaps.append(f'{args["at"]} {args["cabinet"]} 柜的读数未能绑定到原表。')
        else:
            facts.append(_bound(render(row), step, kind="daily_footer" if row["basis"] == "footer" else "calculation"))
    for station, cabinet in sorted(unavailable_stations):
        gaps.append(f'{archive.devices[station, cabinet]["station_name"]} {cabinet} 柜缺少运行日报和整点读数，无法给出所查数值及原表单元格。')
        if len(trace) < budget:
            args = {"station": station, "cabinet": cabinet, "direction": "upstream"}
            link = archive.call(LINK, args)
            step = {"tool": LINK, "arguments": args, "result": link}
            trace.append(step)
            if link["status"] == "ok":
                facts.append(_bound("现有截图仅提供供电关系。" + render(link), step, kind="coverage"))
    gaps = list(dict.fromkeys(gaps))
    if compare and gaps:
        gaps.append("比较项缺少同口径数据，暂无法判断高低。")
    elif compare and len(metrics) == 1 and len(statistics) <= 1:
        facts.extend(_comparison(rows, kind, trace))
    if CAUSE.search(text):
        gaps.append("缺少同期运行日志、开关变位（SOE）或保护动作记录，无法确认现场原因。")
    status = "completed" if not gaps else "partial" if rows else "insufficient_data"
    return _result(kind, status, "completed" if not gaps else "requested_evidence_missing",
                   scope, trace, facts, gaps)


def _statistic(term):
    if term.startswith(("最大", "最高", "峰值")):
        return "max"
    return "min" if term.startswith(("最小", "最低")) else "avg"
