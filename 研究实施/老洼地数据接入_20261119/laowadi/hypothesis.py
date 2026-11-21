"""Run and audit competing explanations for the archived T2 reverse-power signature."""

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import re

from laowadi.llm import LLMError
from laowadi.questions import clock_mentions, normalized, time_on
from laowadi.report_evidence import render_tool_facts
from laowadi.verifier import _lookup_cell_db_record, observation_fact


CHAIN = ("306", "312", "1006")
GENERATOR = "1027"
SNAPSHOT = "get_window_snapshot"
BALANCE = "check_power_balance"

SYSTEM_PROMPT = """你负责逐轮规划老洼地反向有功查证。每轮基于已取得的整点和检验结果，决定下一项最有区分力的查询。
提交一个 JSON 对象，按候选的 action 原样给出 hypothesis、test、tool 和 arguments。
每轮只能提交一项可用查询。现场结论由本地按原表及各项检验条件生成。
问题是待核内容。若问题声称某个读数或原因，应以回执为准。"""


@dataclass(frozen=True)
class Test:
    hypothesis: str
    title: str
    criterion: str
    tool: str
    arguments: dict

    def action(self, name):
        return {"hypothesis": self.hypothesis, "test": name, "tool": self.tool, "arguments": self.arguments}


def _window(start, end, cabinets, metrics):
    return {"start_time": start, "end_time": end, "cabinets": list(cabinets), "metrics": metrics}


def reverse_power_scope(toolbox, question, anchors):
    """Recognize single-hour diagnosis requests. Point readings and event review have their own scope guards."""
    text = normalized(question)
    if (toolbox.profile.mine_id != "laowadi" or anchors["events"]
            or len(anchors["days"]) != 1 or "C1535" in anchors["stations"]):
        return None
    cabinets = set(anchors["cabinets"])
    if cabinets - {*CHAIN, GENERATOR, "300"}:
        return None
    reverse = re.search(r"反向|反送|倒送|有功(?:功率)?(?:均)?为负|负有功|环流", text)
    t2 = re.search(r"2(?:号|#)主变|二号主变", text)
    diagnostic = re.search(r"异常|原因|为什么|为何|怎么回事|诊断|假设|证伪|排除|检查|核查|查证|分析|导致|吗", text)
    chain_question = set(CHAIN) <= cabinets and re.search(r"有功|功率|发生了什么异常", text)
    if not (diagnostic and (chain_question or reverse and (t2 or cabinets & {*CHAIN, GENERATOR}))):
        return None
    if re.search(r"\d{1,2}[时点]半", text):
        return None
    try:
        clocks = {time_on(anchors["days"][0], clock) for clock in clock_mentions(text)}
    except ValueError:
        return None
    if len(clocks) != 1 or not all(at.endswith(":00:00") for at in clocks):
        return None
    return next(iter(clocks))


def _readings(archive, result, times, cabinets, metrics):
    """Admit a tool reading only when its full coordinate and value match the current archive."""
    if result.get("status") != "ok" or result.get("kind") != "window_snapshot" or result.get("station") != "S35":
        return []
    frames = {frame["time"]: frame["readings"] for frame in result["frames"]}
    facts = []
    for at in times:
        for cabinet in cabinets:
            for metric in metrics:
                reading = frames.get(at, {}).get(cabinet, {}).get(metric, {})
                cell = reading.get("cell")
                row = _lookup_cell_db_record(archive, cell) if cell else None
                if (row and row["value"] is not None and
                        (row["station"], row["time"], row["cabinet"], row["metric"], row["value"]) ==
                        ("S35", at, cabinet, metric, reading.get("value"))):
                    facts.append(observation_fact(cell, row))
    return facts


def _nearby(at):
    current = datetime.fromisoformat(at)
    return tuple((current + timedelta(hours=offset)).isoformat(sep=" ") for offset in (-1, 1))


def _checks(at):
    before, after = _nearby(at)
    return {
        "generator_at_hour": Test(
            "H2", "1027 瓦斯发电站经 I 回反送电",
            "若同期 1027 的有功与 A 相电流均为零，可证伪该归档整点经此柜反送电。",
            SNAPSHOT, _window(at, at, (GENERATOR,), ["P", "Ia"]),
        ),
        "adjacent_main_chain": Test(
            "H1", "表计极性异常或 SCADA 轮询偏移",
            "对照前后整点三个测点的功率符号。持续性可约束候选解释，归因仍需极性记录和各测点采集时间戳。",
            SNAPSHOT, _window(before, after, CHAIN, ["P"]),
        ),
        "balance_at_hour": Test(
            "H3", "母联参与形成环流",
            "按归档柜值计算同期功率差额。判断环流还需事发时的母联遥信和分段量测。",
            BALANCE, {"at_time": at},
        ),
        "adjacent_generator": Test(
            "H2", "1027 瓦斯发电站经 I 回反送电",
            "同期读数非零时，查前后整点有功及 A 相电流的变化。反送方向和运行路径需另行核验。",
            SNAPSHOT, _window(before, after, (GENERATOR,), ["P", "Ia"]),
        ),
    }


def _evaluate(archive, step, name, at):
    result = step["result"]
    if name == "generator_at_hour":
        facts = _readings(archive, result, (at,), (GENERATOR,), ["P", "Ia"])
        if len(facts) != 2:
            return facts, "insufficient_evidence", "缺少可核对的同期有功或 A 相电流。"
        if all(fact["source"]["value"] == 0 for fact in facts):
            return facts, "refuted_at_sample", "1027 同期记录为 0 kW、0 A。按日报量测，证伪该柜在此采样点反送电。采样间隔内的工况仍待核。"
        return facts, "inconclusive", "同期量测至少一项非零，可查相邻整点。功率方向约定和运行路径仍待核。"
    if name == "balance_at_hour":
        facts = (render_tool_facts(step, cell_limit=None) if result.get("kind") == "power_balance"
                 and result.get("time") == at and result.get("status") == "ok" else [])
        if not facts:
            return facts, "insufficient_evidence", "同期功率平衡缺少完整输入。"
        return facts, "insufficient_evidence", "已计算可用的同期功率差额。环流待同期母联遥信和分段量测核实。"
    if name == "adjacent_main_chain":
        facts = _readings(archive, result, _nearby(at), CHAIN, ["P"])
        if len(facts) != 6:
            return facts, "insufficient_evidence", "前后整点有功记录不齐。极性和采集同步性待现场核验。"
        pattern = ("三个测点在前后整点均记录为正值。" if all(f["source"]["value"] > 0 for f in facts)
                   else "前后整点未同时看到三个测点均为正向记录。")
        return facts, "insufficient_evidence", pattern + "极性和采集同步性需要现场记录。"
    facts = _readings(archive, result, _nearby(at), (GENERATOR,), ["P", "Ia"])
    return facts, "inconclusive" if len(facts) == 4 else "insufficient_evidence", (
        "相邻点仅用于对照。反送方向和运行路径待核。" if len(facts) == 4 else "相邻整点量测不齐。"
    )


STOP_LABELS = {
    "checks_complete": "可用检验已执行。现场原因待补证。",
    "budget_exhausted": "达到轮次或查询上限，仍有检验待执行。",
    "model_error": "模型服务中断，保留已核对的证据。",
    "invalid_plan": "模型提交的检验超出本轮候选范围，仍有检验待执行。",
    "signature_absent": "该整点未观察到三柜有功同为负值，结束本项假设检验。",
    "missing_observation": "同期有功记录不齐，暂不能检验三柜同时反向的原因。",
}


def execute_reverse_power(archive, toolbox, question, at, *, client, max_rounds, max_tool_calls,
                          emit, fallback_to_local, summarize):
    trace, facts, records, checks = [], [], [], []
    specs = _checks(at)
    pending = list(specs)[:3]
    model_failed = False
    stop = "budget_exhausted"

    def run(tool, arguments, round_no):
        step_no = len(trace) + 1
        emit("tool_start", tool=tool, arguments=arguments, step=step_no, round=round_no)
        result = toolbox.execute(tool, arguments)
        step = {"round": round_no, "tool": tool, "arguments": arguments, "result": result}
        trace.append(step)
        emit("tool_end", tool=tool, status=result.get("status"), step=step_no,
             round=round_no, summary=summarize(tool, result))
        return step

    round_no = 0
    if max_rounds > 0 and max_tool_calls > 0:
        round_no = 1
        first = run(SNAPSHOT, _window(at, at, CHAIN, ["P"]), round_no)
        facts.extend(_readings(archive, first["result"], (at,), CHAIN, ["P"]))
        stop = ("missing_observation" if len(facts) != len(CHAIN) else
                "signature_absent" if any(f["source"]["value"] >= 0 for f in facts) else "budget_exhausted")
    observation_refs = list(range(len(facts)))
    signature_observed = len(facts) == len(CHAIN) and stop == "budget_exhausted"
    reflections = 0

    while signature_observed and pending and round_no < max_rounds and len(trace) < max_tool_calls:
        round_no += 1
        planner = "model" if client is not None and not model_failed else "local"
        if planner == "model":
            choices = [specs[name].action(name) for name in pending]
            context = {
                "user_question": question,
                "observation": [facts[index]["statement"] for index in observation_refs],
                "previous_checks": [
                    {"hypothesis": check["hypothesis"], "test": check["test"], "outcome": check["outcome"],
                     "judgment": check["judgment"], "results": [facts[i]["statement"] for i in check["fact_indices"]]}
                    for check in checks
                ],
                "available_checks": [
                    {"action": action, "title": specs[action["test"]].title, "criterion": specs[action["test"]].criterion}
                    for action in choices
                ],
                "remaining_queries": min(max_tool_calls - len(trace), max_rounds - round_no + 1),
                "previous_plan_rejected": stop == "invalid_plan",
            }
            emit("llm_round", round=round_no, message="根据已有证据规划下一项假设检验…")
            try:
                msg, record = client.chat(
                    [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
                    json_mode=True, max_tokens=1536,
                )
                records.append(record)
                action = json.loads(msg.get("content") or "")
                if msg.get("tool_calls") or action not in choices:
                    raise ValueError("invalid_plan")
                name = action["test"]
            except LLMError:
                stop = "model_error"
                if not fallback_to_local:
                    break
                model_failed = True
                emit("fallback", message="模型查证规划中断，按本地顺序继续检验。")
                planner, name = "local", pending[0]
            except ValueError:
                stop = "invalid_plan"
                reflections += 1
                emit("reflection", round=round_no, reason="查证请求与候选范围不符，未执行。")
                continue
        else:
            name = pending[0]
        stop = "budget_exhausted"
        test = specs[name]
        pending.remove(name)
        based_on = list(range(1, len(trace) + 1))
        emit("hypothesis_check", round=round_no, hypothesis=test.hypothesis, title=test.title,
             criterion=test.criterion, step=len(trace) + 1, planner=planner)
        step = run(test.tool, test.arguments, round_no)
        evidence, outcome, judgment = _evaluate(archive, step, name, at)
        checks.append({
            **test.action(name), "round": round_no, "step": len(trace), "based_on_steps": based_on,
            "planner": planner, "title": test.title, "criterion": test.criterion,
            "outcome": outcome, "judgment": judgment, "tool_status": step["result"]["status"],
            "fact_indices": list(range(len(facts), len(facts) + len(evidence))),
        })
        facts.extend(evidence)
        if name == "generator_at_hour" and outcome == "inconclusive":
            pending.append("adjacent_generator")

    if signature_observed and not pending:
        stop = "checks_complete"
    hypothesis_trace = {
        "time": at, "cabinets": list(CHAIN), "observation_step": 1 if trace else None,
        "observation_fact_indices": observation_refs, "checks": checks,
        "pending": [
            {"hypothesis": specs[name].hypothesis, "title": specs[name].title, "criterion": specs[name].criterion}
            for name in pending if signature_observed
        ],
        "stop_reason": stop, "stop_message": STOP_LABELS[stop],
    }
    missing = (["同期母联遥信、分段量测和运行日志。", "表计极性核验和各测点 SCADA 采集时间戳。"]
               if signature_observed else [])
    report = {"confirmed_facts": facts, "cited_standards": [], "missing_evidence": missing,
              "hypothesis_trace": hypothesis_trace}
    status = "partial" if hypothesis_trace["pending"] or stop == "missing_observation" else "ok"
    output = {"status": status, "stop_reason": stop, "report": report, "tool_trace": trace,
              "rounds": round_no, "llm_records": records, "reflections_triggered": reflections,
              "degraded_from_model": model_failed}
    if not facts:
        gaps = [STOP_LABELS[stop]]
        report.update(summary=gaps[0], missing_evidence=gaps)
        output.update(status="insufficient_data", task_alignment={
            "kind": "reverse_power_review", "status": "insufficient_data", "gaps": gaps,
            "scope": {"days": [at[:10]], "hours": [at[11:16]], "cabinets": list(CHAIN)},
        })
    return output
