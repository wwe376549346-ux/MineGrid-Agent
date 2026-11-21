"""Route common Chinese dispatch questions to the local read-only tools."""

import re
from datetime import date, datetime
from unicodedata import normalize

from laowadi.agent import MAX_CALLS, Review, review_event
from laowadi.queries import label_device
from laowadi.storage import REPORT_STATION


DATE = re.compile(r"(?:(?P<year>\d{4})年)?(?P<month>\d{1,2})月(?P<day>\d{1,2})[日号]?|(?P<iso>\d{4}-\d{2}-\d{2})")
CLOCK = re.compile(r"(?<!\d)(\d{1,2})(?:[:](\d{2})(?:[:](\d{2}))?|[时点](?:(\d{1,2})(?:分|(?=\D|$)))?)")
EVENT_ID = re.compile(r"(?<![a-z0-9])lw-([0-9]+)(?![0-9])")
EVENT_CUE = re.compile(r"缺测|缺失|功率变化|提示变化|为什么.{0,8}变化|变化事件|监测事件|告警|报警")
COMPARE = re.compile(r"对照|比较|对比|同期|同时.{0,15}(?:变化|变动|有功|读数)|同步|同向|反向|同一(?:时段|对?整点|时间|时刻)|"
                     r"相同.{0,5}(?:整点|时刻|时间)|一起变化|跟着变|也.{0,8}变化")
TOPOLOGY = re.compile(r"上游|下游|供电关系|供电路径|接到哪里|从哪里供电|由.{0,15}供电")
CAUSE = re.compile(r"(?:物理|现场|实际|故障|什么)原因|导致|引起|联动|转供|跳闸|切换|倒闸|拉闸|分闸|合闸|保护动作|运行日志|变位|soe")
EVENT_REF = re.compile(r"(?:这条|该|这个)(?:事件|告警)")
EVENT_FOLLOWUP = re.compile(r"同期|同步|同一(?:对整点|时段|时间)")
CONTROL = re.compile(r"合闸|分闸|拉闸|跳闸|切换|倒闸|闭锁|设定保护|调整保护|设定值")
CONTROL_ACTION = re.compile(r"^(?:请|帮我|给我|立即|现在|马上|然后|并|再|执行|操作|把|将)*"
                            r"(?:[a-z0-9]+号?柜(?:的开关)?)?(?:合闸|分闸|拉闸|倒闸|闭锁|切换|设定保护|调整保护)$"
                            r"|(?:执行|下发|下令|强制|远程|直接|立即|马上).{0,10}(?:合闸|分闸|拉闸|倒闸|切换)")
CLAUSE = re.compile(r"[，,。；;！？?、]")
METRICS = {"Uab": ("线电压", "电压", "uab"), "Ia": ("a相电流", "电流", "ia"),
           "P": ("有功功率", "有功", "p"), "cos": ("功率因数", "cos")}


def normalized(text):
    return re.sub(r"\s+", "", normalize("NFKC", text).casefold())


def match_term(word, text):
    normalized_word = normalized(word)
    left = r"(?<![a-z0-9])" if normalized_word[0].isascii() and normalized_word[0].isalpha() else ""
    right = r"(?![a-z])" if normalized_word[-1].isascii() and normalized_word[-1].isalpha() else ""
    return re.search(left + re.escape(normalized_word) + right, text)


def clarify(message):
    return {"kind": "clarify", "status": "clarify", "message": message}


def time_on(day, clock):
    if not day:
        raise ValueError("请补充所查整点的日期。")
    hour, minute, second, minute_cn = (int(value or 0) for value in clock.groups())
    return datetime.fromisoformat(day).replace(hour=hour, minute=minute or minute_cn, second=second).isoformat(sep=" ")


def clock_mentions(text):
    return CLOCK.finditer(DATE.sub(lambda match: " " * len(match[0]), text))


class QuestionSession:
    def __init__(self, archive, *, selector=None, max_tool_calls=MAX_CALLS):
        self.archive = archive
        self.context = {}
        self.last_event_id = None
        self.selector = selector
        self.max_tool_calls = max_tool_calls

    def pick_day(self, text):
        return self.resolve_day(DATE.search(text))

    def resolve_day(self, match):
        if not match:
            return self.context.get("day")
        if match["iso"]:
            return date.fromisoformat(match["iso"]).isoformat()
        mm, dd = int(match["month"]), int(match["day"])
        if match["year"]:
            return date(int(match["year"]), mm, dd).isoformat()
        day_pattern = date(2000, mm, dd).isoformat()[5:]
        options = [day for day in self.archive.days() if day[5:] == day_pattern]
        if len(options) == 1:
            return options[0]
        if len(options) > 1:
            raise ValueError("库中有多个年份符合该月日，请补充年份。")
        raise ValueError("已导入的日报没有这个月日。请指定库内日期。")

    def pick_device(self, text, for_reading, *, use_context=True):
        exact = [(key, hit.start()) for key in self.archive.devices if
                 (hit := re.search(rf"(?<!\d){re.escape(key[1])}(?!\d)", text))]
        stations = [(code, hit.start(), hit.end()) for code, entry in self.archive.spec["stations"].items() for term in entry["aliases"]
                    if (hit := match_term(term, text))]
        if exact:
            if len(exact) == 1:
                key, position = exact[0]
                before = [row for row in stations if row[2] <= position]
                if before and max(before, key=lambda row: row[2])[0] != key[0]:
                    raise ValueError("站名与柜号归属不一致，请核对后再查询。")
                return key
            labels = "、".join(label_device(self.archive.devices[key]) for key, _ in exact)
            raise ValueError(f"请先指定要查询的柜号。问题中提到了{labels}。")

        candidates = [(key, hit.start(), len(hit[0])) for key, device in self.archive.devices.items() for term in device["aliases"]
                      if (hit := match_term(term, text))]
        if candidates:
            length = max(row[2] for row in candidates)
            candidates = [(key, start) for key, start, width in candidates if width == length]
            anchor = min(start for _, start in candidates)
            before = [row for row in stations if row[2] <= anchor]
            station = max(before, key=lambda row: row[2])[0] if before else (stations[0][0] if len({row[0] for row in stations}) == 1 else None)
            selected = {key for key, _ in candidates}
            if station is not None:
                selected = {key for key in selected if key[0] == station}
            elif for_reading and any(key[0] == REPORT_STATION for key in selected):
                selected = {key for key in selected if key[0] == REPORT_STATION}
            if len(selected) == 1:
                return next(iter(selected))
            if selected:
                labels = "、".join(label_device(self.archive.devices[key]) for key in sorted(selected))
                raise ValueError(f"请明确站名或柜号。符合名称的设备有{labels}。")
            raise ValueError("该站台账中未找到这个设备名称。")

        if re.search(r"\d+\s*号?柜", text) or stations:
            raise ValueError("请给出已建台账的柜号或完整设备名称。")
        if use_context and "station" in self.context:
            return self.context["station"], self.context["cabinet"]
        raise ValueError("请先指定设备名称或柜号。")

    def ask_event(self, text, identifier):
        args = {}
        if identifier:
            args["event_id"] = identifier[0].upper()
        else:
            if re.search(r"电流|电压|功率因数", text):
                return clarify("本批监测事件覆盖整点缺测和有功功率变化。")
            missing = bool(re.search(r"缺测|缺失", text))
            args["event_type"] = "missing_power" if missing and "有功" in text else "hourly_gap" if missing else "power_change"
            if args["event_type"] != "hourly_gap":
                station, cabinet = self.pick_device(text, for_reading=True)
                args["cabinet"] = cabinet
                if station != REPORT_STATION:
                    return {"kind": "monitor_event", "status": "missing"}
            if (clock := CLOCK.search(text)):
                args["at"] = time_on(self.pick_day(text), clock)
            elif DATE.search(text):
                args["day"] = self.pick_day(text)
        tool = "get_monitor_event"
        return {**self.archive.call(tool, args), "tool": tool, "arguments": args}

    def comparison_cabinets(self, clauses):
        cabinets = []
        for clause in clauses:
            text = DATE.sub(" ", EVENT_ID.sub(" ", clause))
            text = CLOCK.sub(" ", text)
            stations = [(code, hit.end()) for code, data in self.archive.spec["stations"].items()
                        for alias in data["aliases"] if (hit := match_term(alias, text))]
            for match in re.finditer(r"(?<![a-z0-9])(\d+)(号?柜)?", text):
                cabinet = match[1]
                if not match[2] and not any(number == cabinet for _, number in self.archive.devices):
                    continue
                before = [row for row in stations if row[1] <= match.start()]
                if ((REPORT_STATION, cabinet) not in self.archive.devices
                        or (before and max(before, key=lambda row: row[1])[0] != REPORT_STATION)):
                    raise ValueError(f"请核对站名，并指定有地面站日报的对照柜号。当前提到 {cabinet} 柜。")
                cabinets.append(cabinet)
        return tuple(dict.fromkeys(cabinets))

    def comparison_times(self, clauses):
        clocks, days = [], []
        for clause in clauses:
            dates = [(match.end(), self.resolve_day(match)) for match in DATE.finditer(clause)]
            days.extend(day for _, day in dates)
            for clock in clock_mentions(clause):
                day = next((day for end, day in reversed(dates) if end <= clock.start()), None)
                stamp = time_on(day or "2000-01-01", clock)
                if stamp[14:] != "00:00":
                    raise ValueError("请用整点进行对照，例如 11:00 或 12:00。")
                clocks.append((day, stamp[11:13]))
        dated = {hour for day, hour in clocks if day is not None}
        return (tuple(dict.fromkeys((day, hour) for day, hour in clocks if day or hour not in dated)),
                tuple(dict.fromkeys(days)))

    def ask_review(self, question, text, event_id):
        clauses = [clause for clause in CLAUSE.split(text) if clause]
        if any(CONTROL_ACTION.search(clause) for clause in clauses):
            return clarify("当前仅支持历史回放、日报及供电图查询。")
        topology = [clause for clause in clauses if TOPOLOGY.search(clause)]
        if len(topology) > 1:
            return clarify("请指定一个要查的柜号及供电方向。")
        link = None
        if topology:
            clause = EVENT_ID.sub("", topology[0])
            if "上游" in clause and "下游" in clause:
                return clarify("请指定要查上游还是下游。")
            station, cabinet = self.pick_device(clause, for_reading=False, use_context=False)
            direction = "downstream" if re.search(r"下游|接到哪里", clause) else "upstream"
            link = {"station": station, "cabinet": cabinet, "direction": direction}

        reading_clauses = [clause for clause in clauses if clause not in topology]
        reading_text = "，".join(reading_clauses)
        compare = bool(COMPARE.search(reading_text))
        if compare and re.search(r"(?:半|\d+)小时[前后]|(?:\d+|半)分钟[前后]|半点", reading_text):
            return clarify("请给出要比较的明确整点，或指定按事件前后的一对整点对照。")
        cabinets = self.comparison_cabinets(reading_clauses) if compare else ()
        clocks, days = self.comparison_times(reading_clauses) if compare else ((), ())
        if event_id:
            event_args = {"event_id": event_id}
        else:
            anchors = [clause for clause in reading_clauses if EVENT_CUE.search(clause)]
            if len(anchors) != 1:
                return clarify("请指定事件编号，或把事件柜号、日期和触发整点写在同一句。")
            anchor = re.split(r"(?:变化事件|提示变化|监测事件|告警|报警)(?:触发)?(?:时|后|发生时)", anchors[0], maxsplit=1)[0]
            if re.search(r"缺测|缺失", anchor):
                return clarify("请提供这条缺测事件的编号。")
            anchor_clocks = list(clock_mentions(anchor))
            if len(anchor_clocks) != 1:
                return clarify("请指定事件编号，或给出事件柜号、日期和一个触发整点。")
            station, cabinet = self.pick_device(anchor, for_reading=True, use_context=False)
            if station != REPORT_STATION:
                return clarify("请指定地面站有功变化事件的柜号或事件编号。")
            event_args = {"cabinet": cabinet, "event_type": "power_change",
                          "at": time_on(self.pick_day(anchor), anchor_clocks[0])}

        logs = ()
        if CAUSE.search(text):
            logs = ("需核对同期运行日志和开关变位记录（SOE），才能判断现场原因或设备联动。",)
            if "保护" in text or "跳闸" in text:
                logs += ("需补充同期保护动作记录。",)
        review = Review(event_args, compare, cabinets, clocks, days, link, logs)
        answer = review_event(self.archive, question, review, selector=self.selector, max_calls=self.max_tool_calls)
        if answer["steps"] and answer["steps"][0]["result"]["status"] == "ok":
            self.last_event_id = answer["steps"][0]["result"]["event"]["id"]
        return answer

    def ask(self, question):
        text = normalized(question)
        try:
            identifiers = list(EVENT_ID.finditer(text))
            if len({hit[0] for hit in identifiers}) > 1:
                return clarify("请先指定一条要复核的监测事件。")
            identifier = identifiers[0] if identifiers else None
            event = identifier or EVENT_CUE.search(text)
            ref = EVENT_REF.search(text)
            previous = self.last_event_id
            if identifier or (event and not ref):
                self.last_event_id = None
            followup = previous and (ref or (not event and EVENT_FOLLOWUP.search(text)))
            if re.search(r"无功|有功电量|电能|电网频率|ubc|uca|b相电流|c相电流", text):
                return clarify("现有日报的可查询量为 Uab、A 相电流、有功功率和功率因数。")
            comparison = COMPARE.search(text)
            if (event or followup) and (comparison or TOPOLOGY.search(text) or CAUSE.search(text)):
                if comparison and re.search(r"页尾|表尾|日报统计|最大|最小|平均|峰值", text):
                    return clarify("事件同期对照使用前后两个整点读数。请单独指定日期、柜号和统计口径查询日报统计。")
                if comparison and re.search(r"电流|电压|功率因数", text):
                    return clarify("事件同期对照使用有功功率。请单独指定整点查询其他电气量。")
                event_id = identifier[0].upper() if identifier else previous if followup else None
                return self.ask_review(question, text, event_id)
            if CONTROL.search(text):
                return clarify("当前查询范围为历史日报和截图中的供电关系。")
            if event:
                answer = self.ask_event(text, identifier)
                if answer["status"] == "ok":
                    self.last_event_id = answer["event"]["id"]
                return answer
            supply = bool(re.search(r"供电|哪里来的|接入|上游|下游|接到|送到|来自|从哪", text))
            station, cabinet = self.pick_device(text, for_reading=not supply)
            args = {"station": station, "cabinet": cabinet}
            if supply:
                tool = "get_supply_link"
                args["direction"] = "downstream" if re.search(r"下游|送到|接到|下接|去哪里", text) else "upstream"
            else:
                matches = [metric for metric, terms in METRICS.items() if any(match_term(term, text) for term in terms)]
                if len(matches) > 1:
                    return clarify("请明确本次要查询哪一个电气量。")
                metric = matches[0] if matches else self.context.get("metric")
                if not metric:
                    return clarify("请选择线电压、A 相电流、有功功率或功率因数。")
                args["metric"] = metric
                hourly = bool(re.search(r"整点|逐小时|小时样本|小时记录|按小时|重算", text))
                footer = bool(re.search(r"页尾|表尾|日报统计|日报所列|报表统计|原表最大|原表最小|原表平均", text))
                report = bool(re.search(r"日报|报表", text))
                stat = ("max" if re.search(r"最大|最高|峰值", text) else "min" if re.search(r"最小|最低", text)
                        else "avg" if "平均" in text else None)
                day = self.pick_day(text)
                if footer and stat is None:
                    stat = self.context.get("statistic")
                if stat:
                    if not day:
                        return clarify("请指定日报日期，再查询当日最大值、最小值或平均值。")
                    tool = "get_daily_statistic"
                    if hourly and not footer:
                        basis = "hourly"
                    elif not hourly and (footer or report):
                        basis = "footer"
                    else:
                        basis = "both"
                    args.update(day=day, statistic=stat, basis=basis)
                elif footer:
                    return clarify("请说明要查询日报页尾的最大值、最小值还是平均值。")
                else:
                    tool = "get_hourly_reading"
                    clock = CLOCK.search(text)
                    if clock:
                        args["at"] = time_on(day, clock)
                    elif DATE.search(text):
                        args["day"] = day
            result = self.archive.call(tool, args)
        except (TypeError, ValueError) as exc:
            return clarify(str(exc))
        self.context.update({key: value for key, value in args.items() if key in {"station", "cabinet", "metric", "day", "statistic"}})
        if "at" in args:
            self.context["day"] = args["at"][:10]
        return {**result, "tool": tool, "arguments": args}
