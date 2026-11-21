import React, {useEffect, useReducer, useRef, useState} from "react";
import {Badge} from "@astryxdesign/core/Badge";
import {Button} from "@astryxdesign/core/Button";
import {MessageSquare, ShieldCheck} from "lucide-react";
import {authHeaders, cellHref} from "./api.js";
import {consumeAgentStream} from "./AgentCopilotPane.jsx";
import {AnswerText, AgentStreamStyles} from "./AgentStreamArtifacts.jsx";
import "./advanced-agent.css";

const INITIAL = {agents: {}, rebuttals: [], arbitration: null};
const EXAMPLE = "2026年11月13日17:00，核对 1006 柜与 10 kV II 段电压。比较调压与补偿候选，并列出待补现场证据。";

function reduceDebate(state, frame) {
  const event = {...frame, ...frame.data};
  switch (frame.type) {
    case "reset":
      return INITIAL;
    case "subagent_start":
    case "subagent_done": {
      const detail = event.agent ?? event.subagent ?? event.result;
      const id = event.agent_id ?? event.role ?? detail?.id ?? detail?.role ?? event.name;
      if (!id) return state;
      const agent = {...state.agents[id], ...event, ...detail, id, finished: frame.type === "subagent_done"};
      return {...state, agents: {...state.agents, [id]: agent}};
    }
    case "debate_rebuttal":
      return {...state, rebuttals: [...state.rebuttals, event.rebuttal ?? event]};
    case "arbitration_done":
      return {...state, arbitration: event.arbitration ?? event.result ?? event};
    case "done":
      return {...state, arbitration: event.result?.arbitration ?? event.arbitration ?? state.arbitration};
    default:
      return state;
  }
}

function EvidenceList({items}) {
  if (!Array.isArray(items) || !items.length) return null;
  const citations = items.map(item => typeof item === "string" ? item : item?.cell ?? item?.source_cell).filter(Boolean);
  if (!citations.length) return null;
  return <ul className="advanced-evidence">{citations.map((cell, index) => {
    const href = cellHref(cell);
    return <li key={index}>{href ? <a href={href} target="_blank" rel="noreferrer">{cell}</a> : cell}</li>;
  })}</ul>;
}

function ReportBody({report}) {
  const text = typeof report === "string" ? report : report?.verdict ?? report?.stance ?? report?.point ?? report?.summary;
  const sections = report && [
    ["取证发现", report.findings], ["共识", report.consensus], ["裁定分歧", report.resolved_conflicts],
  ];
  return <>
    {typeof text === "string" && <AnswerText text={text} />}
    {sections && sections.map(([label, entries]) => Array.isArray(entries) && entries.length > 0 && <div key={label}>
      <strong>{label}</strong>
      <ul className="advanced-evidence">{entries.map((entry, index) => <li key={index}>{typeof entry === "string" ? entry : JSON.stringify(entry)}</li>)}</ul>
    </div>)}
    <EvidenceList items={report?.evidence_cells ?? report?.citations ?? report?.evidence} />
    {report && typeof report === "object" && !text && !sections.some(([, entries]) => Array.isArray(entries) && entries.length)
      && <details><summary>查看回执</summary><pre>{JSON.stringify(report, null, 2)}</pre></details>}
  </>;
}

export function SubagentDebatePanel({hasModel}) {
  const [question, setQuestion] = useState(EXAMPLE);
  const [mode, setMode] = useState("auto");
  const [status, setStatus] = useState("idle");
  const [error, setError] = useState("");
  const [state, dispatch] = useReducer(reduceDebate, INITIAL);
  const request = useRef(null);
  const running = status === "running";
  const agents = Object.values(state.agents);

  useEffect(() => () => request.current?.abort(), []);

  async function start(event) {
    event.preventDefault();
    const clean = question.trim();
    if (!clean || request.current) return;
    const controller = new AbortController();
    request.current = controller;
    dispatch({type: "reset"});
    setStatus("running");
    setError("");
    try {
      const response = await fetch("/api/subagents/debate/stream", {
        method: "POST",
        headers: authHeaders({"Content-Type": "application/json", Accept: "text/event-stream"}),
        body: JSON.stringify({question: clean, mode}),
        signal: controller.signal,
      });
      await consumeAgentStream(response, frame => { if (!controller.signal.aborted) dispatch(frame); }, "子智能体辩论");
      if (!controller.signal.aborted) setStatus("done");
    } catch (e) {
      if (!controller.signal.aborted) {
        setError(e.message);
        setStatus("error");
      }
    } finally {
      if (request.current === controller) request.current = null;
    }
  }

  function stop() {
    request.current?.abort();
    request.current = null;
    setStatus("stopped");
  }

  return <div className="advanced-workspace">
    <AgentStreamStyles />
    <form className="advanced-card" onSubmit={start}>
      <h2>子智能体对抗辩论</h2>
      <label>会诊问题<textarea rows={3} maxLength={600} value={question} disabled={running} onChange={e => setQuestion(e.target.value)} /></label>
      <div className="advanced-controls">
        <label>推理方式<select value={mode} disabled={running} onChange={e => setMode(e.target.value)}>
          <option value="auto">自动选择</option>
          <option value="local">本地分析</option>
          <option value="deepseek" disabled={!hasModel}>DeepSeek</option>
        </select></label>
        {running ? <Button type="button" label="停止接收" onClick={stop} />
          : <Button type="submit" variant="primary" label="发起辩论" disabled={!question.trim()} />}
      </div>
      {error && <p className="advanced-error" role="alert">{error}</p>}
      {status === "stopped" && <p className="muted" role="status">已停止接收</p>}
    </form>

    <section className="advanced-card">
      <h3>三路并行分析</h3>
      <div className="advanced-cards">
        {Array.from({length: Math.max(3, agents.length)}, (_, index) => {
          const agent = agents[index];
          const tone = agent?.status === "error" ? "error" : agent?.finished ? "success" : "neutral";
          const label = tone === "error" ? "分析失败" : agent?.finished ? "已交付" : agent && running ? "分析中" : "待开始";
          return <article className="advanced-subcard" key={agent?.id ?? index}>
            <header><strong>{agent?.label ?? agent?.name ?? agent?.title ?? agent?.role ?? "子智能体 " + (index + 1)}</strong>
              <Badge variant={tone} label={label} /></header>
            {agent?.finished && <><ReportBody report={agent} />
              {Number.isFinite(agent.elapsed_ms) && <small className="muted">{agent.elapsed_ms} ms</small>}</>}
          </article>;
        })}
      </div>
    </section>

    {(running || state.rebuttals.length > 0) && <section className="advanced-card">
      <h3><MessageSquare size={16} /> 对抗反驳</h3>
      {!state.rebuttals.length ? <p className="muted">等候各路分析交付</p>
        : <ol className="advanced-sequence">{state.rebuttals.map((item, index) => <li key={index}>
          <header><strong>{[item.from_agent, item.to_agent].filter(Boolean).join(" → ") || "质询"}</strong>
            {item.severity && <Badge variant={item.severity === "high" ? "warning" : "neutral"}
              label={{high: "重点复核", medium: "补充校验", info: "回应"}[item.severity] ?? item.severity} />}</header>
          <ReportBody report={item} />
        </li>)}</ol>}
    </section>}

    {state.arbitration && <section className="advanced-card advanced-arbitration" aria-live="polite">
      <h3><ShieldCheck size={17} /> 总控裁决</h3>
      <ReportBody report={state.arbitration} />
    </section>}
  </div>;
}
