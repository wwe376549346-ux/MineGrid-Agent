import React, {useEffect, useId, useMemo, useState} from "react";
import {Badge} from "@astryxdesign/core/Badge";
import {Check, Circle, Code2, ListTodo, LoaderCircle, ShieldCheck, X} from "lucide-react";
import {cellHref} from "./api.js";

export const emptyAgentStream = {
  plan: [], reasoning: "", tools: [], runs: [], content: "", verification: null, status: "running",
};

function deltaText(event) {
  const text = event.delta ?? event.text ?? event.content ?? event.reasoning ?? event.message;
  return typeof text === "string" ? text : "";
}

function mergeRun(items, item, id) {
  const index = id == null ? -1 : items.findLastIndex(prev => prev.id === id);
  if (index === -1) return [...items, {...item, id}];
  return items.map((prev, i) => i === index ? {...prev, ...item} : prev);
}

export function applyAgentEvent(state, frame) {
  const event = {...frame, ...frame.data};
  switch (frame.type) {
    case "plan_update": {
      const plan = event.items ?? event.todos ?? event.steps ?? event.plan;
      return {...state, plan: Array.isArray(plan) ? plan : (plan?.items ?? plan?.steps ?? state.plan)};
    }
    case "reasoning_delta":
      return {...state, reasoning: state.reasoning + deltaText(event)};
    case "tool_start":
    case "tool_end": {
      const name = event.tool ?? event.name ?? "工具";
      const previous = state.tools.findLast(tool => tool.name === name && tool.status === "running");
      const id = event.call_id ?? event.tool_call_id ?? event.step ?? previous?.id ?? state.tools.length;
      const status = frame.type === "tool_start" ? "running" : event.status ?? (event.error || event.ok === false ? "failed" : "completed");
      return {...state, tools: mergeRun(state.tools, {...event, name, status}, id)};
    }
    case "code_run": {
      const run = {...event, ...(event.result ?? event.execution ?? event.output)};
      return {...state, runs: mergeRun(state.runs, run, event.run_id ?? event.call_id ?? event.id)};
    }
    case "content_delta":
      return {...state, content: state.content + deltaText(event)};
    case "verification_badge":
      return {...state, verification: event.verification ?? event.badge ?? event};
    case "done": {
      const content = event.result?.answer_markdown ?? event.content ?? event.answer;
      return {...state, status: "done", content: typeof content === "string" && content ? content : state.content};
    }
    default:
      return state;
  }
}

const PLAN_STATUS = {
  pending: ["待执行", Circle, ""],
  running: ["进行中", LoaderCircle, "is-active"],
  in_progress: ["进行中", LoaderCircle, "is-active"],
  completed: ["已完成", Check, "is-success"],
  done: ["已完成", Check, "is-success"],
  ok: ["已完成", Check, "is-success"],
  success: ["已完成", Check, "is-success"],
  failed: ["未完成", X, "is-failed"],
  error: ["未完成", X, "is-failed"],
  blocked: ["已拦截", X, "is-failed"],
  timeout: ["执行超时", X, "is-failed"],
  skipped: ["已跳过", Circle, ""],
};

function Status({status}) {
  const [text, Icon, tone] = PLAN_STATUS[status] ?? PLAN_STATUS.pending;
  return <span className={`agent-stream-status ${tone}`}><Icon size={13} aria-hidden="true" />{text}</span>;
}

function PlanItems({items}) {
  return <ul className="agent-stream-plan">
    {items.map((item, index) => {
      const children = item.children ?? item.subtasks;
      return <li key={item.id ?? index}>
        <div className="agent-stream-step">
          <span>{item.title ?? item.label ?? item.task ?? item.text ?? item.description ?? (typeof item === "string" ? item : "分析步骤")}</span>
          <Status status={item.status} />
        </div>
        {Array.isArray(children) && children.length > 0 && <PlanItems items={children} />}
      </li>;
    })}
  </ul>;
}

function displayValue(value) {
  if (value == null) return "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function ResultTable({table}) {
  const rows = table.rows ?? table.data;
  if (!Array.isArray(rows) || rows.length === 0) return null;
  const columns = table.columns ?? (Array.isArray(rows[0])
    ? rows[0].map((_, index) => String(index + 1))
    : Object.keys(rows[0] ?? {}));
  return <div className="agent-stream-table" tabIndex={0} role="region" aria-label={table.title || "计算结果表格"}>
    <table>
      {table.title && <caption>{table.title}</caption>}
      <thead><tr>{columns.map((column, index) => <th key={index} scope="col">
        {column.label ?? column.name ?? column}
      </th>)}</tr></thead>
      <tbody>{rows.map((row, index) => <tr key={index}>
        {columns.map((column, i) => <td key={i}>
          {displayValue(Array.isArray(row) ? row[i] : row?.[column.key ?? column.name ?? column])}
        </td>)}
      </tr>)}</tbody>
    </table>
  </div>;
}

const SERIES_COLORS = ["#3679db", "#078478", "#ad55aa", "#c26818", "#7060e3"];
const tickLabel = new Intl.NumberFormat("zh-CN", {maximumSignificantDigits: 4});

function chartPoints(series, chart) {
  const raw = series.points ?? series.data ?? series.y?.map((y, index) => [series.x?.[index] ?? index + 1, y]);
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((point, index) => {
    const [x, y] = Array.isArray(point)
      ? point
      : [point?.[chart.x_key ?? "x"] ?? index + 1, point?.[chart.y_key ?? "y"] ?? point];
    const value = typeof y === "string" && y.trim() ? Number(y) : y;
    return x != null && Number.isFinite(value) ? [{x, y: value, label: point?.label}] : [];
  });
}

function ResultChart({chart}) {
  const titleId = useId();
  const kind = chart.chart_type ?? chart.kind ?? chart.type;
  if (!["scatter", "line", "bar"].includes(kind)) return null;
  const series = (Array.isArray(chart.series) ? chart.series : [chart])
    .map((item, index) => ({name: item.name, color: SERIES_COLORS[index % SERIES_COLORS.length], points: chartPoints(item, chart)}))
    .filter(item => item.points.length);
  if (!series.length) return null;

  const points = series.flatMap(item => item.points);
  const bar = kind === "bar";
  const categorical = bar || points.some(point => typeof point.x !== "number");
  const categories = new Map([...new Set(points.map(point => point.x))].map((value, index) => [value, index]));
  const xValue = x => categorical ? categories.get(x) : x;
  const xMin = categorical ? (bar ? -0.5 : 0) : Math.min(...points.map(point => point.x));
  const xMax = categorical ? categories.size - (bar ? 0.5 : 1) : Math.max(...points.map(point => point.x));
  const values = points.map(point => point.y);
  const min = Math.min(...values, ...(bar ? [0] : []));
  const max = Math.max(...values, ...(bar ? [0] : []));
  const yMin = min === max && !bar ? min - 1 : min;
  const yMax = min === max ? max + 1 : max;
  const left = 58, right = 466, top = 13, bottom = 166;
  const xAt = n => xMin === xMax ? (left + right) / 2 : left + (right - left) * (n - xMin) / (xMax - xMin);
  const yAt = n => bottom - (bottom - top) * (n - yMin) / (yMax - yMin);
  const yTicks = Array.from({length: 5}, (_, index) => yMin + (yMax - yMin) * index / 4);
  const xTicks = categorical
    ? [...categories].filter(([, index]) => index % Math.ceil(categories.size / 5) === 0).map(([label, x]) => ({x, label: String(label)}))
    : Array.from({length: xMin === xMax ? 1 : 5}, (_, i) => {
      const x = xMin + (xMax - xMin) * i / 4;
      return {x, label: tickLabel.format(x)};
    });
  const barWidth = Math.min(32, (right - left) * 0.75 / (categories.size * series.length));
  const title = chart.title || {scatter: "散点图", line: "折线图", bar: "柱状图"}[kind];
  return <figure className="agent-stream-chart">
    <figcaption>
      <strong id={titleId}>{title}</strong>
      {chart.y_label && <small>{chart.y_label}</small>}
    </figcaption>
    <svg viewBox="0 0 480 205" role="img" aria-labelledby={titleId}>
      {yTicks.map((tick, i) => <g key={i}>
        <line x1={left} x2={right} y1={yAt(tick)} y2={yAt(tick)} stroke="var(--border)" />
        <text x={left - 8} y={yAt(tick) + 4} textAnchor="end">{tickLabel.format(tick)}</text>
      </g>)}
      <path d={`M ${left} ${top} V ${bottom} H ${right}`} fill="none" stroke="var(--border-strong)" />
      {xTicks.map(({x, label}) => <text key={x} x={xAt(x)} y={bottom + 15} textAnchor="middle">
        <title>{label}</title>{label.length > 12 ? label.slice(0, 11) + "…" : label}
      </text>)}
      {chart.x_label && <text x={(left + right) / 2} y={201} textAnchor="middle">{chart.x_label}</text>}
      {series.map(({name, color, points: data}, seriesIndex) => <g key={seriesIndex} fill={color}>
        {kind === "line" && <polyline fill="none" stroke={color} strokeWidth={2}
          points={data.map(point => `${xAt(xValue(point.x))},${yAt(point.y)}`).join(" ")} />}
        {data.map((point, i) => {
          const x = xAt(xValue(point.x)), y = yAt(point.y);
          const tip = <title>{name ? `${name} · ` : ""}{point.label ?? point.x} · {point.y}</title>;
          return bar
            ? <rect key={i} x={x + (seriesIndex - series.length / 2) * barWidth} y={Math.min(y, yAt(0))}
              width={barWidth} height={Math.abs(y - yAt(0))} rx={2}>{tip}</rect>
            : <circle key={i} cx={x} cy={y} r={kind === "scatter" ? 4 : 2.7}>{tip}</circle>;
        })}
      </g>)}
    </svg>
    {series.some(item => item.name) && <div className="agent-stream-legend">
      {series.map((item, i) => <span key={i}><i style={{background: item.color}} />{item.name ?? `系列 ${i + 1}`}</span>)}
    </div>}
  </figure>;
}

function CodeRunCard({run}) {
  const outputs = Array.isArray(run.artifacts) ? run.artifacts : [];
  const tables = [...(Array.isArray(run.tables) ? run.tables : []), ...outputs.filter(item => item?.type === "table")];
  const charts = [...(Array.isArray(run.charts) ? run.charts : []), ...outputs.filter(item => ["chart", "scatter", "line", "bar"].includes(item?.type))];
  const status = run.status ?? (run.error || run.success === false || (run.exit_code != null && run.exit_code !== 0) ? "failed" :
    run.success === true || run.exit_code === 0 || run.stdout != null ? "completed" : "pending");
  const elapsed = run.duration_ms ?? run.elapsed_ms;
  return <section className="agent-stream-card agent-stream-code">
    <header className="agent-stream-step">
      <strong><Code2 size={16} aria-hidden="true" /> Python 沙盒</strong>
      <Status status={status} />
    </header>
    {Number.isFinite(elapsed) && <small className="muted">{tickLabel.format(elapsed)} ms</small>}
    {[["代码", run.code], ["stdout", run.stdout], ["stderr", run.stderr || run.error || ""]].map(([label, value]) =>
      value != null ? <details key={label} open={Boolean(value) && label !== "代码"}>
        <summary>{label}</summary><pre>{value ? displayValue(value) : "无输出"}</pre>
      </details> : null
    )}
    {tables.map((table, i) => <ResultTable key={i} table={table} />)}
    {charts.map((chart, i) => <ResultChart key={i} chart={chart} />)}
  </section>;
}

function VerificationBadge({verification}) {
  const trusted = verification.business_trusted ?? verification.trusted;
  const passed = trusted === true || (trusted == null && ["verified", "passed"].includes(verification.status));
  const label = passed ? "证据核验通过" : {
    partial: "部分证据已核验", failed: "核验未通过", unverified: "证据未核验",
  }[verification.status] ?? "待核验";
  const counts = [
    verification.checked_cells > 0 && `原表 ${verification.verified_cells}/${verification.checked_cells}`,
    verification.checked_numbers > 0 && `数值 ${verification.verified_numbers}/${verification.checked_numbers}`,
  ].filter(Boolean);
  return <div className="agent-stream-verification">
    <Badge variant={passed ? "success" : "warning"} label={verification.label || label}
      icon={<ShieldCheck size={13} aria-hidden="true" />} />
    {counts.length > 0 && <span>{counts.join(" · ")}</span>}
    {Array.isArray(verification.notes) && verification.notes.length > 0 && <details>
      <summary>核验说明</summary>
      <ul>{verification.notes.map((note, i) => <li key={i}>{note}</li>)}</ul>
    </details>}
    {(verification.message ?? verification.summary) && <span>{verification.message ?? verification.summary}</span>}
  </div>;
}

function AnswerText({text}) {
  function inline(line) {
    return line.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/).map((part, i) => {
      if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
        return <strong key={i}>{part.slice(2, -2)}</strong>;
      }
      if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
        const citation = part.slice(1, -1);
        const href = cellHref(citation);
        return href ? <a key={i} href={href} target="_blank" rel="noreferrer"><code>{citation}</code></a>
          : <code key={i}>{citation}</code>;
      }
      return part;
    });
  }

  return <div className="agent-stream-answer">
    {text.split("\n").map((line, i) => {
      const heading = line.match(/^#{1,6}\s+(.+)/);
      if (heading) return <h3 key={i}>{inline(heading[1])}</h3>;
      if (line.startsWith("> ")) return <blockquote key={i}>{inline(line.slice(2))}</blockquote>;
      if (!line.trim()) return <div key={i} className="agent-stream-answer-break" aria-hidden="true" />;
      return <div key={i}>{inline(line.replace(/^[-*] /, "• "))}</div>;
    })}
  </div>;
}

export function AgentStreamArtifacts({stream, onTextProgress}) {
  const glyphs = useMemo(() => Array.from(stream.content), [stream.content]);
  const [visible, setVisible] = useState(0);
  useEffect(() => {
    if (visible >= glyphs.length) return undefined;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const step = reducedMotion ? glyphs.length : Math.max(1, Math.ceil((glyphs.length - visible) / 45));
    const timer = window.setTimeout(() => setVisible(Math.min(glyphs.length, visible + step)), 18);
    return () => window.clearTimeout(timer);
  }, [glyphs.length, visible]);
  useEffect(() => { onTextProgress(visible); }, [visible, onTextProgress]);

  return <div className="agent-stream-artifacts">
    {Array.isArray(stream.plan) && stream.plan.length > 0 && <section className="agent-stream-card" aria-label="执行计划">
      <header><strong><ListTodo size={16} aria-hidden="true" /> 执行计划</strong></header>
      <PlanItems items={stream.plan} />
    </section>}
    {stream.reasoning && <details className="agent-stream-card"><summary>思考过程</summary><pre>{stream.reasoning}</pre></details>}
    {stream.tools.length > 0 && <details className="agent-stream-card">
      <summary>工具调用 · {stream.tools.length}</summary>
      <ul className="agent-stream-plan">{stream.tools.map(tool => <li key={tool.id} className="agent-stream-step">
        <code>{tool.name}</code><Status status={tool.status} />
      </li>)}</ul>
    </details>}
    {stream.runs.map((run, i) => <CodeRunCard key={run.id ?? i} run={run} />)}
    {visible > 0 && <AnswerText text={glyphs.slice(0, visible).join("")} />}
    {stream.status === "done" && visible >= glyphs.length && stream.verification && <VerificationBadge verification={stream.verification} />}
  </div>;
}

export function AgentStreamStyles() {
  return <style>{`
    .agent-stream-mode { display: flex; justify-content: center; gap: 4px; padding: 12px 8px 0; flex-wrap: wrap; }
    .agent-stream-mode button { padding: 6px 12px; border: 1px solid transparent; border-radius: 8px;
      background: transparent; color: var(--text-secondary, inherit); font: inherit; font-size: 13px; }
    .agent-stream-mode button[aria-pressed="true"] { background: var(--surface-subtle); border-color: var(--border); color: var(--text); font-weight: 600; }
    .agent-stream-mode button:disabled { opacity: .72; cursor: default; }
    .agent-stream-artifacts { display: grid; min-width: 0; gap: 10px; }
    .agent-stream-card { border: 1px solid var(--border); background: var(--surface); padding: 11px 13px;
      border-radius: 12px; min-width: 0; font-size: 13px; overflow-wrap: anywhere; }
    .agent-stream-card header strong, .agent-stream-status { display: inline-flex; align-items: center; gap: 6px; }
    .agent-stream-card summary { cursor: pointer; font-weight: 600; }
    .agent-stream-card summary svg { vertical-align: -2px; }
    .agent-stream-step { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
    .agent-stream-plan { list-style: none; margin: 7px 0 0; padding: 0; }
    .agent-stream-plan > li { margin: 5px 0; }
    .agent-stream-plan .agent-stream-plan { margin: 4px 0 6px 6px; padding-left: 11px; border-left: 1px solid var(--border); }
    .agent-stream-status { white-space: nowrap; font-size: 11px; color: var(--muted); }
    .agent-stream-status.is-success { color: var(--success, #138053); }
    .agent-stream-status.is-failed { color: var(--danger, #b43f40); }
    .agent-stream-status.is-active svg { animation: agent-stream-spin 1.4s linear infinite; }
    @keyframes agent-stream-spin { to { transform: rotate(360deg); } }
    @media (prefers-reduced-motion: reduce) { .agent-stream-status.is-active svg { animation: none; } }
    .agent-stream-code > details, .agent-stream-table, .agent-stream-chart { margin: 8px 0 0; }
    .agent-stream-card pre { max-height: 220px; overflow: auto; white-space: pre-wrap; margin: 7px 0 0;
      padding: 9px 10px; background: var(--surface-subtle); border-radius: 7px; font-size: 12px; }
    .agent-stream-table { overflow: auto; max-height: 210px; border: 1px solid var(--border); border-radius: 7px; }
    .agent-stream-table table { width: 100%; border-collapse: collapse; font-size: 12px; }
    .agent-stream-table caption { text-align: left; padding: 6px 8px; font-weight: 600; }
    .agent-stream-table th, .agent-stream-table td { padding: 4px 8px; text-align: left; border-bottom: 1px solid var(--border); white-space: nowrap; }
    .agent-stream-table th { position: sticky; top: 0; background: var(--surface-subtle); }
    .agent-stream-chart { background: var(--surface-subtle); border-radius: 8px; padding: 9px; }
    .agent-stream-chart figcaption { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 5px; }
    .agent-stream-chart svg { display: block; width: 100%; height: auto; max-width: 720px; margin-inline: auto; }
    .agent-stream-chart text { font-size: 11px; fill: currentColor; }
    .agent-stream-legend, .agent-stream-verification { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; font-size: 12px; }
    .agent-stream-legend i { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; }
    .agent-stream-answer { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.75; margin: 0; }
    .agent-stream-answer h3 { font-size: 1em; margin: 0; }
    .agent-stream-answer blockquote { margin: 0; padding-left: 10px; border-left: 2px solid var(--border-strong); color: var(--muted); }
    .agent-stream-answer-break { height: .6em; }
  `}</style>;
}
