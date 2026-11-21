import React, {useEffect, useReducer, useRef, useState} from "react";
import {Badge} from "@astryxdesign/core/Badge";
import {Button} from "@astryxdesign/core/Button";
import {Spinner} from "@astryxdesign/core/Spinner";
import {Check, Circle, ClipboardList, Download, FileText, Printer, Search, Square} from "lucide-react";
import {consumeAgentStream} from "./AgentCopilotPane.jsx";
import {apiRequest, authHeaders, cellHref} from "./api.js";

const QUICK_GOALS = [
  ["全站运行风险", "深度排查老洼地全周运行风险，核对电压、电流、有功和功率因数，列出有证据的隐患和待补资料。"],
  ["2 号主变反向有功", "深度排查 2 号主变及关联馈线的反向有功，比较同期断面并列出可能原因及所需现场证据。"],
  ["1010 柜电压与无功", "深度排查 1010 柜电压偏差与无功变化，区分已观测现象、条件化计算和待核设备参数。"],
];
const INITIAL = {plan: [], findings: [], sections: [], reasoning: "", report: null, activity: ""};
const STAGE_STATUS = {
  running: ["进行中", "info"],
  in_progress: ["进行中", "info"],
  done: ["已完成", "success"],
  completed: ["已完成", "success"],
  failed: ["未完成", "error"],
  error: ["未完成", "error"],
  skipped: ["已跳过", "neutral"],
};

const reportPath = (id, format) => `/api/deep-research/reports/${encodeURIComponent(id)}/export?format=${format}`;
const reportId = report => report?.report_id ?? report?.id;
const stageId = stage => stage.id ?? stage.stage_id ?? stage.stage;
function upsert(list, value, getId) {
  const id = getId(value);
  const index = id == null ? -1 : list.findIndex(item => getId(item) === id);
  return index === -1 ? [...list, value] : list.map((item, i) => i === index ? {...item, ...value} : item);
}

function reduceResearch(state, event) {
  switch (event.type) {
    case "reset": return INITIAL;
    case "plan_update": {
      const steps = event.steps ?? event.stages ?? event.plan;
      const previous = new Map(state.plan.map(stage => [stageId(stage), stage]));
      return {...state, plan: Array.isArray(steps)
        ? steps.map(step => ({...previous.get(stageId(step)), ...step}))
        : state.plan};
    }
    case "stage_update": {
      const stage = typeof event.stage === "object" ? event.stage : event;
      return {...state, plan: upsert(state.plan, stage, stageId), activity: event.message || stage.summary || stage.title || state.activity};
    }
    case "finding": {
      const finding = event.finding ?? event;
      return {...state, findings: upsert(state.findings, finding, item => item.id ?? item.finding_id)};
    }
    case "section_ready": {
      const section = typeof event.section === "object" ? event.section : event;
      return {...state, sections: upsert(state.sections, section, item => item.id ?? item.section_id ?? item.title ?? item.section)};
    }
    case "reasoning_delta":
      return {...state, reasoning: state.reasoning + (event.delta ?? event.text ?? event.message ?? "")};
    case "done":
      return {...state, report: event.report ?? event.result ?? event};
    default:
      return state;
  }
}

function StageTree({stages}) {
  return <ol className="investigation-stages">
    {stages.map((stage, index) => {
      const [label, tone] = STAGE_STATUS[stage.status] ?? ["待执行", "neutral"];
      const Icon = tone === "success" ? Check : Circle;
      const children = stage.children ?? stage.steps;
      return <li key={stageId(stage) ?? index}>
        <div className="investigation-stage"><Icon size={15} aria-hidden="true" />
          <div><strong>{stage.title ?? stage.name ?? stage.stage ?? stageId(stage)}</strong>
            {(stage.detail || stage.summary) && <p>{stage.detail || stage.summary}</p>}
          </div>
          <Badge variant={tone} label={label} />
        </div>
        {Array.isArray(children) && children.length > 0 && <StageTree stages={children} />}
      </li>;
    })}
  </ol>;
}

function FindingCard({item}) {
  const severity = {
    critical: ["紧急", "error"], high: ["高优先级", "error"], medium: ["需复核", "warning"],
    warning: ["需复核", "warning"], low: ["关注", "neutral"], info: ["提示", "info"],
  }[item.severity] ?? ["待复核", "neutral"];
  const evidence = item.evidence ?? item.evidence_cells ?? item.sources ?? [];
  const sources = Array.isArray(evidence) ? evidence : [evidence];
  return <article className="investigation-finding">
    <div className="investigation-card-head"><strong>{item.title ?? item.name ?? "调查发现"}</strong>
      <Badge variant={severity[1]} label={severity[0]} /></div>
    {(item.summary || item.description || item.detail) && <p>{item.summary || item.description || item.detail}</p>}
    {(item.cabinet || item.at_time) && <div className="investigation-meta">{[item.cabinet, item.at_time].filter(Boolean).join(" · ")}</div>}
    {sources.length > 0 && <div className="investigation-sources">{sources.map((source, index) => {
      const text = typeof source === "string" ? source : source?.ref ?? source?.cell ?? source?.label ?? JSON.stringify(source);
      const href = cellHref(text);
      return href ? <a key={index} href={href} target="_blank" rel="noreferrer">{text}</a> : <span key={index}>{text}</span>;
    })}</div>}
  </article>;
}

async function reportResponse(id, format, signal) {
  const response = await fetch(reportPath(id, format), {headers: authHeaders(), signal});
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(error?.error || `报告暂不可用 (${response.status})`);
  }
  return response;
}

export function DeepInvestigationView({hasModel}) {
  const [goal, setGoal] = useState("");
  const [mode, setMode] = useState("auto");
  const [taskGoal, setTaskGoal] = useState("");
  const [runStatus, setRunStatus] = useState("idle");
  const [runError, setRunError] = useState("");
  const [state, dispatch] = useReducer(reduceResearch, INITIAL);
  const [reports, setReports] = useState([]);
  const [historyError, setHistoryError] = useState("");
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyRefresh, setHistoryRefresh] = useState(0);
  const [selected, setSelected] = useState(null);
  const [preview, setPreview] = useState("");
  const [previewError, setPreviewError] = useState("");
  const [previewRefresh, setPreviewRefresh] = useState(0);
  const [downloading, setDownloading] = useState("");
  const [downloadError, setDownloadError] = useState("");
  const runningRequest = useRef(null);
  const running = runStatus === "running";

  useEffect(() => () => runningRequest.current?.abort(), []);
  useEffect(() => {
    const request = new AbortController();
    setHistoryLoading(true);
    setHistoryError("");
    apiRequest("/api/deep-research/reports", {signal: request.signal})
      .then(payload => { if (!request.signal.aborted) setReports(payload.reports); })
      .catch(err => { if (!request.signal.aborted) setHistoryError(err.message); })
      .finally(() => { if (!request.signal.aborted) setHistoryLoading(false); });
    return () => request.abort();
  }, [historyRefresh]);

  const selectedId = reportId(selected);
  const liveSections = !selected || selectedId === reportId(state.report) ? state.sections : [];
  useEffect(() => {
    setPreview("");
    setPreviewError("");
    if (!selectedId) return;
    const request = new AbortController();
    reportResponse(selectedId, "html", request.signal)
      .then(response => response.text())
      .then(html => { if (!request.signal.aborted) setPreview(html); })
      .catch(error => { if (!request.signal.aborted) setPreviewError(error.message); });
    return () => request.abort();
  }, [selectedId, previewRefresh]);

  function selectReport(report) {
    setSelected(report);
    setDownloadError("");
  }

  async function start(nextGoal) {
    const clean = nextGoal.trim();
    if (!clean || runningRequest.current) return;
    const request = new AbortController();
    runningRequest.current = request;
    setGoal(clean);
    setTaskGoal(clean);
    setRunStatus("running");
    setRunError("");
    selectReport(null);
    dispatch({type: "reset"});
    try {
      const response = await fetch("/api/deep-research/stream", {
        method: "POST",
        headers: authHeaders({"Content-Type": "application/json", Accept: "text/event-stream"}),
        body: JSON.stringify({goal: clean, mode}),
        signal: request.signal,
      });
      await consumeAgentStream(response, frame => {
        if (request.signal.aborted) return;
        const event = {...frame, ...frame.data, type: frame.type};
        dispatch(event);
        if (event.type === "done") {
          selectReport(event.report ?? event.result ?? event);
          setHistoryRefresh(value => value + 1);
        }
      }, "深度排查");
      if (!request.signal.aborted) setRunStatus("done");
    } catch (error) {
      if (!request.signal.aborted) {
        setRunError(error.message);
        setRunStatus("error");
      }
    } finally {
      if (runningRequest.current === request) runningRequest.current = null;
    }
  }

  function stop() {
    runningRequest.current?.abort();
    runningRequest.current = null;
    setRunStatus("stopped");
  }

  async function download(format) {
    const id = selectedId;
    if (!id || downloading) return;
    setDownloading(format);
    setDownloadError("");
    try {
      const response = await reportResponse(id, format);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = `老洼地专项排查-${id}.${format}`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      setDownloadError(error.message);
    } finally {
      setDownloading("");
    }
  }

  return <div className="investigation">
    <style>{`
      .investigation { max-width: 1270px; margin: 0 auto; display: grid; gap: 16px; color: var(--duty-ink); }
      .investigation h1, .investigation h2, .investigation h3, .investigation p, .investigation pre { margin: 0; }
      .investigation h1 { font-size: clamp(23px, 3vw, 31px); letter-spacing: -.02em; }
      .investigation h2 { font-size: 16px; display: flex; align-items: center; gap: 7px; }
      .investigation h3 { font-size: 14px; }
      .investigation-columns { display: grid; grid-template-columns: minmax(240px, 1fr) minmax(0, 1.65fr); gap: 16px; align-items: start; }
      .investigation-stack { display: grid; gap: 13px; min-width: 0; align-content: start; }
      .investigation-panel { display: grid; gap: 12px; min-width: 0; padding: clamp(13px, 2vw, 19px); border: 1px solid var(--duty-line);
        border-radius: 14px; background: var(--duty-paper); overflow-wrap: anywhere; }
      .investigation-goal { display: grid; gap: 7px; }
      .investigation-goal textarea { resize: vertical; box-sizing: border-box; width: 100%; min-height: 80px;
        padding: 11px 12px; border: 1px solid var(--duty-line); border-radius: 8px; color: inherit; background: var(--surface-subtle); font: inherit; }
      .investigation-actions, .investigation-quick, .investigation-status, .investigation-sources { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
      .investigation-actions label { font-size: 13px; display: flex; align-items: center; gap: 6px; margin-right: auto; }
      .investigation-actions select { padding: 5px 8px; color: inherit; border: 1px solid var(--duty-line); border-radius: 7px; background: var(--duty-paper); }
      .investigation-chip { background: var(--duty-rail); border: 1px solid var(--duty-line); color: inherit;
        border-radius: 8px; padding: 5px 9px; font-size: 12px; cursor: pointer; }
      .investigation-chip:disabled { opacity: .6; cursor: default; }
      .investigation-meta, .investigation-status { color: var(--duty-muted); font-size: 12px; }
      .investigation-alert { color: var(--duty-alert); }
      .investigation-stages { padding: 0; margin: 0; list-style: none; }
      .investigation-stages > li { padding: 7px 0 7px 9px; border-left: 1px solid var(--duty-line); }
      .investigation-stages .investigation-stages { margin: 5px 0 0 21px; }
      .investigation-stage { display: grid; align-items: baseline; grid-template-columns: auto minmax(0, 1fr) auto; gap: 8px; font-size: 13px; }
      .investigation-stage svg { color: var(--duty-accent); }
      .investigation-stage p { color: var(--duty-muted); margin-top: 3px; font-size: 12px; }
      .investigation-finding { display: grid; gap: 6px; padding: 11px; border: 1px solid var(--duty-line); border-radius: 8px; font-size: 13px; }
      .investigation-card-head { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 6px; }
      .investigation-sources { font-size: 12px; }
      .investigation-report-preview { width: 100%; height: clamp(460px, 70vh, 760px); border: 1px solid var(--duty-line); border-radius: 8px; background: white; }
      .investigation-text { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; font-size: 13px; line-height: 1.65; }
      .investigation-history { display: grid; gap: 5px; }
      .investigation-history button { display: grid; text-align: left; gap: 3px; padding: 8px; border: 1px solid var(--duty-line); border-radius: 8px;
        color: inherit; background: var(--duty-paper); cursor: pointer; }
      .investigation-history button[aria-pressed="true"] { background: var(--duty-accent-soft); border-color: var(--duty-accent); }
      .investigation-history button:disabled { opacity: .7; cursor: default; }
      @media (max-width: 920px) { .investigation-columns { grid-template-columns: 1fr; } }
    `}</style>

    <form className="investigation-panel" onSubmit={e => { e.preventDefault(); start(goal); }}>
      <h1>深度排查</h1>
      <label className="investigation-goal">排查目标
        <textarea aria-label="排查目标" value={goal} onChange={e => setGoal(e.target.value)} maxLength={1200} rows={3}
          placeholder="例如，排查 2 号主变反向有功并核对关联馈线" />
      </label>
      <div className="investigation-quick" aria-label="快捷排查目标">
        {QUICK_GOALS.map(([label, prompt]) => <button key={label} type="button" className="investigation-chip"
          disabled={running} onClick={() => start(prompt)}>{label}</button>)}
      </div>
      <div className="investigation-actions">
        <label>分析方式<select value={mode} onChange={e => setMode(e.target.value)} disabled={running}>
          <option value="auto">自动</option><option value="local">本地</option><option value="deepseek" disabled={!hasModel}>DeepSeek</option>
        </select></label>
        {running ? <Button label="停止接收" icon={<Square size={14} />} type="button" onClick={stop} />
          : <Button label="开始排查" icon={<Search size={16} />} variant="primary" type="submit" disabled={!goal.trim()} />}
      </div>
      {runError && <p className="investigation-alert" role="alert">{runError}</p>}
    </form>

    <div className="investigation-columns">
      <div className="investigation-stack">
        <section className="investigation-panel" aria-label="调查进度">
          <h2><ClipboardList size={17} /> 调查进度</h2>
          {taskGoal && <p className="investigation-meta">{taskGoal}</p>}
          {running && <div className="investigation-status" role="status"><Spinner size="sm" /> {state.activity || "正在制定计划"}</div>}
          {runStatus === "stopped" && <p className="investigation-meta">已停止接收，保留当前进度。</p>}
          {runStatus === "done" && <p className="investigation-meta">排查已完成</p>}
          {state.plan.length > 0 && <StageTree stages={state.plan} />}
          {runStatus === "idle" && <p className="investigation-meta">开始排查后查看各阶段进度。</p>}
          {state.reasoning && <details><summary>排查过程</summary><pre className="investigation-text">{state.reasoning}</pre></details>}
        </section>

        {state.findings.length > 0 && <section className="investigation-panel" aria-label="已发现隐患">
          <h2>已发现隐患</h2>
          {state.findings.map((item, i) => <FindingCard key={item.id ?? item.finding_id ?? i} item={item} />)}
        </section>}

        <section className="investigation-panel" aria-label="历史报告">
          <h2>历史报告</h2>
          {historyLoading && <div className="investigation-status" role="status"><Spinner size="sm" /> 正在读取历史报告</div>}
          {historyError && <><p className="investigation-alert" role="alert">{historyError}</p>
            <Button label="重试报告列表" size="sm" onClick={() => setHistoryRefresh(value => value + 1)} /></>}
          {!historyLoading && !historyError && reports.length === 0 && <p className="investigation-meta">暂无历史报告。</p>}
          <div className="investigation-history">{reports.map(report => <button key={reportId(report)} type="button"
            disabled={running || Boolean(downloading)} aria-pressed={selectedId === reportId(report)} onClick={() => selectReport(report)}>
            <strong>{report.title ?? report.goal ?? reportId(report)}</strong>
            <small className="investigation-meta">{report.created_at ?? report.report_date}</small>
          </button>)}</div>
        </section>
      </div>

      <section className="investigation-panel" aria-label="专项报告">
        <div className="investigation-card-head">
          <h2><FileText size={17} /> 专项报告</h2>
          {selectedId && <div className="investigation-actions">
            <Button label="导出 Markdown" icon={<Download size={15} />} size="sm" disabled={Boolean(downloading)} onClick={() => download("md")} />
            <Button label="下载打印版 HTML" icon={<Printer size={15} />} size="sm" disabled={Boolean(downloading)} onClick={() => download("html")} />
          </div>}
        </div>
        {(selectedId || state.sections.length > 0) && <p className="investigation-meta">
          历史归档回放。母联状态、操作票和保护记录待现场核实。
        </p>}
        {downloadError && <p className="investigation-alert" role="alert">{downloadError}</p>}
        {preview && <iframe title="专项报告预览" className="investigation-report-preview" sandbox="" srcDoc={preview} />}
        {!preview && (selected?.markdown || liveSections.length > 0) && <div className="investigation-stack">
          {selected?.markdown ? <pre className="investigation-text">{selected.markdown}</pre>
            : liveSections.map((section, i) => <article key={section.id ?? section.section_id ?? i}>
              <h3>{section.title ?? section.section}</h3>
              <pre className="investigation-text">{section.markdown ?? section.content ?? section.summary}</pre>
            </article>)}
        </div>}
        {selectedId && !preview && !previewError && <div className="investigation-status" role="status"><Spinner size="sm" /> 正在加载报告预览</div>}
        {previewError && <><p className="investigation-alert" role="alert">{previewError}</p>
          <Button label="重试报告预览" size="sm" onClick={() => setPreviewRefresh(value => value + 1)} /></>}
        {!selectedId && !liveSections.length && <p className="investigation-meta">排查生成的报告章节会出现在这里。</p>}
      </section>
    </div>
  </div>;
}
