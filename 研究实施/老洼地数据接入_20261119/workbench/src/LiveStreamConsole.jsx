import React, {useEffect, useMemo, useRef, useState} from "react";
import {Badge} from "@astryxdesign/core/Badge";
import {Button} from "@astryxdesign/core/Button";
import {Spinner} from "@astryxdesign/core/Spinner";
import {Activity, AlertTriangle, ClipboardList, FileUp, Play, Square} from "lucide-react";
import {consumeAgentStream} from "./AgentCopilotPane.jsx";
import {ACTION_KIND_LABELS, ActionApprovalCard} from "./ActionApprovalCard.jsx";
import {apiRequest, authHeaders} from "./api.js";
import {EvidenceDownloadLink} from "./IncidentTicketPanel.jsx";

const RATE_OPTIONS = [[1000, "1×"], [500, "2×"], [250, "4×"], [125, "8×"]];
const JSON_HEADERS = {"Content-Type": "application/json"};
const getTicketId = ticket => ticket?.ticket_id ?? ticket?.id;
const tickTime = tick => tick?.at_time ?? tick?.time ?? tick?.timestamp;
const metricLabels = {voltage: "线电压", current: "相电流"};
const numberLabel = new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 2});

function normalizeTick(frame) {
  const metrics = frame.metrics ?? {};
  return {
    time: tickTime(frame),
    source: frame.source,
    samples: {
      voltage: {"1005": metrics.u_1005_kv, "1006": metrics.u_1006_kv},
      current: {"303": metrics.i_303_a, "1010": metrics.i_1010_a},
    },
  };
}

function Trend({ticks, cabinet, metric, unit, threshold}) {
  const values = ticks.map(tick => tick.samples[metric][cabinet]);
  const samples = values.filter(Number.isFinite);
  const latest = values.at(-1);
  const lastSample = values.findLastIndex(Number.isFinite);
  const width = 470, height = 158, left = 49, top = 12, right = 458, bottom = 129;
  const bound = Number.isFinite(threshold) ? [...samples, threshold] : samples;
  const min = Math.min(...bound), max = Math.max(...bound);
  const pad = min === max ? Math.max(0.05, Math.abs(min) / 30) : (max - min) * 0.08;
  const low = min - pad, high = max + pad;
  const x = index => left + (index / Math.max(values.length - 1, 1)) * (right - left);
  const y = value => bottom - ((value - low) / (high - low)) * (bottom - top);
  const labels = [...new Set([0, Math.floor((ticks.length - 1) / 2), ticks.length - 1])];
  const line = values.flatMap((value, i) => Number.isFinite(value)
    ? [`${i > 0 && Number.isFinite(values[i - 1]) ? "L" : "M"}${x(i)},${y(value)}`]
    : []).join(" ");
  return <figure className="liveops-trend">
    <figcaption><strong>{metricLabels[metric]}</strong><span className="liveops-reading" title={Number.isFinite(latest) ? undefined : "最新整点缺测"}>
      {Number.isFinite(latest) ? numberLabel.format(latest) : "—"} <small>{unit}</small>
    </span></figcaption>
    {samples.length === 0 ? <p className="liveops-meta">等待该柜的{metricLabels[metric]}样本。</p>
      : <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${cabinet} 柜${metricLabels[metric]}趋势`}>
        {[0, 1, 2, 3].map(i => {
          const value = low + (high - low) * i / 3;
          return <g key={i}><line x1={left} x2={right} y1={y(value)} y2={y(value)} stroke="var(--duty-line)" />
            <text x={left - 6} y={y(value) + 4} textAnchor="end">{numberLabel.format(value)}</text></g>;
        })}
        {Number.isFinite(threshold) && <line x1={left} x2={right} y1={y(threshold)} y2={y(threshold)}
          stroke="var(--duty-alert)" strokeDasharray="5 4"><title>演练阈值 {threshold} kV</title></line>}
        <path d={line} stroke="var(--duty-accent)" fill="none" strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round" />
        <circle cx={x(lastSample)} cy={y(values[lastSample])} r={3.6} fill="var(--duty-accent)">
          <title>{ticks[lastSample].time} · {numberLabel.format(values[lastSample])} {unit}</title>
        </circle>
        {labels.map(i => <text key={i} x={x(i)} y={148} textAnchor={i === 0 ? "start" : i === ticks.length - 1 ? "end" : "middle"}>
          {ticks[i]?.time?.slice(11, 16) ?? ""}
        </text>)}
      </svg>}
  </figure>;
}

function AlertCard({alert, onDraft, busy}) {
  const cabinet = String(alert.cabinet ?? alert.target_cabinet ?? "");
  const recommended = alert.recommended_ticket;
  const singleIncomer = alert.kind === "single_incomer_zero";
  const title = singleIncomer ? "306 柜有功归零，303 柜电流升高" : alert.title;
  const diagnosis = singleIncomer
    ? "同刻读数显示 306 柜有功为零、303 柜电流升高。转供路径还需核对母联状态和操作票。"
    : alert.summary;
  return <article className="liveops-alert" aria-label="预诊断告警">
    <header className="liveops-card-head">
      <strong>{title ?? "电气量测越限"}</strong>
      <Badge variant={alert.severity === "high" ? "error" : "warning"} label={alert.severity === "high" ? "高优先级" : "待复核"} />
    </header>
    <p className="liveops-meta">{[cabinet && `${cabinet} 柜`, tickTime(alert)].filter(Boolean).join(" · ")}</p>
    {diagnosis && <div className="liveops-diagnosis"><strong>Agent 预诊断</strong>
      <p>{diagnosis}</p>
    </div>}
    {alert.evidence_cell && <div className="liveops-actions"><EvidenceDownloadLink cell={alert.evidence_cell} /></div>}
    {recommended?.target_cabinet && <Button label={`起草${ACTION_KIND_LABELS[recommended.kind] ?? "票据"}`} size="sm" disabled={busy}
      onClick={() => onDraft({
        ...recommended,
        rationale: singleIncomer ? diagnosis : recommended.rationale,
      })} />}
  </article>;
}

function UploadPanel({uploads, onUploaded, uploadListError, onRetry}) {
  const [csv, setCsv] = useState("");
  const [file, setFile] = useState(null);
  const [dragging, setDragging] = useState(false);
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const inputRef = useRef(null);

  function chooseFile(next) {
    if (!next || sending) return;
    setError("");
    if (!/\.csv$/i.test(next.name)) {
      setError("请选择 .csv 文件。");
      return;
    }
    if (next.size > 2 * 1024 * 1024) {
      setError("CSV 文件最多 2 MiB。");
      return;
    }
    setFile(next);
    setResult(null);
  }

  async function upload(event) {
    event.preventDefault();
    if (!file && !csv.trim()) return;
    setSending(true);
    setError("");
    setResult(null);
    try {
      let options;
      if (file) {
        const payload = new FormData();
        payload.set("file", file);
        options = {body: payload};
      } else {
        options = {headers: JSON_HEADERS, body: JSON.stringify({csv_text: csv, filename: "粘贴日报.csv"})};
      }
      const response = await apiRequest("/api/live/upload-report", {method: "POST", ...options});
      setResult(response);
      setFile(null);
      setCsv("");
      if (inputRef.current) inputRef.current.value = "";
      onUploaded(response);
    } catch (err) {
      setError(err.message);
    } finally {
      setSending(false);
    }
  }

  const stats = result?.stats ?? result?.upload ?? result;
  const findings = result?.findings ?? result?.anomalies ?? result?.health?.findings ?? [];
  return <section className="liveops-panel" aria-label="新日报上传">
    <h2><FileUp size={18} /> 新日报上传</h2>
    <p className="liveops-meta">35 kV 站日报。时刻填写完整日期和时间，列名支持 time、cabinet、Uab、Ia、P、cos。</p>
    <form className="liveops-upload-form" onSubmit={upload}>
      <div className={`liveops-drop ${dragging ? "is-dragging" : ""}`}
        onDragOver={e => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={e => { e.preventDefault(); setDragging(false); chooseFile(e.dataTransfer.files[0]); }}>
        <FileUp size={20} aria-hidden="true" />
        <span>拖拽 CSV 日报到这里</span>
        <input ref={inputRef} type="file" accept=".csv,text/csv" aria-label="选择 CSV 日报" disabled={sending}
          onChange={e => { chooseFile(e.target.files[0]); e.target.value = ""; }} />
      </div>
      {file && <div className="liveops-actions"><strong className="liveops-wrap">{file.name}</strong>
        <Button label="移除文件" type="button" size="sm" disabled={sending} onClick={() => setFile(null)} />
      </div>}
      {!file && <label className="liveops-field">粘贴 CSV 文本
        <textarea aria-label="粘贴 CSV 文本" disabled={sending} value={csv} rows={4}
          onChange={e => setCsv(e.target.value)} placeholder="将带列名的 CSV 文本粘贴到此处" />
      </label>}
      <div className="liveops-actions">
        <Button type="submit" variant="primary" label={sending ? "正在解析…" : "导入沙盒"} size="sm"
          disabled={sending || (!file && !csv.trim())} />
      </div>
    </form>
    {error && <p className="liveops-error" role="alert">{error}</p>}
    {result && <div className="liveops-upload-receipt" role="status">
      <strong>已导入增量沙盒</strong>
      <p>{[
        stats.row_count != null && `解析 ${stats.row_count} 条读数`,
        stats.cabinet_count != null && `${stats.cabinet_count} 柜`,
        (stats.inserted_rows ?? stats.rows_inserted) != null && `写入 ${stats.inserted_rows ?? stats.rows_inserted} 行`,
        (stats.skipped_rows ?? stats.rows_skipped) != null && `跳过 ${stats.skipped_rows ?? stats.rows_skipped} 行`,
        `${findings.length} 项体检提示`,
      ].filter(Boolean).join(" · ")}</p>
      {findings.length > 0 && <ul>{findings.map((item, i) => <li key={item.id ?? i}>
        {typeof item === "string" ? item : item.title ?? item.message ?? item.summary}
      </li>)}</ul>}
    </div>}
    <details className="liveops-history">
      <summary>上传历史 · {uploads.length}</summary>
      {uploadListError && <p className="liveops-error" role="alert">{uploadListError}
        <Button label="重试历史" size="sm" onClick={onRetry} /></p>}
      {!uploadListError && !uploads.length && <p className="liveops-meta">暂无增量日报。</p>}
      <ol>{uploads.map((item, index) => <li key={item.upload_id ?? item.id ?? index}>
        <strong>{item.filename ?? item.source_name ?? item.upload_id}</strong>
        <span>{[item.day, item.created_at ?? item.uploaded_at].filter(Boolean).join(" · ")}</span>
      </li>)}</ol>
    </details>
  </section>;
}

export function LiveStreamConsole({days, openedTicket, onDraft, onSourceChange}) {
  const [uploads, setUploads] = useState([]);
  const [uploadsError, setUploadsError] = useState("");
  const [uploadsRevision, setUploadsRevision] = useState(0);
  const [tickets, setTickets] = useState([]);
  const [ticketsError, setTicketsError] = useState("");
  const [ticketsRevision, setTicketsRevision] = useState(0);
  const [webhookConfigured, setWebhookConfigured] = useState(false);
  const [selectedId, setSelectedId] = useState(null);
  const [date, setDate] = useState("");
  const [speed, setSpeed] = useState(500);
  const [threshold, setThreshold] = useState("");
  const [activeThreshold, setActiveThreshold] = useState(null);
  const [ticks, setTicks] = useState([]);
  const [alerts, setAlerts] = useState([]);
  const [voltageCabinet, setVoltageCabinet] = useState("1006");
  const [currentCabinet, setCurrentCabinet] = useState("303");
  const [status, setStatus] = useState("idle");
  const [streamError, setStreamError] = useState("");
  const [draftError, setDraftError] = useState("");
  const [drafting, setDrafting] = useState(false);
  const [uploadedDays, setUploadedDays] = useState([]);
  const requestRef = useRef(null);
  const running = status === "running";
  const replayDays = useMemo(() => [...new Set([
    ...days, ...uploadedDays, ...uploads.map(item => item.day).filter(Boolean),
  ])].sort(), [days, uploads, uploadedDays]);
  const replayDay = date || replayDays.at(-1) || "";
  const [streamSource, setStreamSource] = useState(null);
  const hasSandboxData = uploadedDays.includes(replayDay) || uploads.some(item => item.day === replayDay);
  const sourceMode = streamSource ?? (hasSandboxData ? "sandbox" : "drill");
  const ticket = tickets.find(item => getTicketId(item) === selectedId);

  useEffect(() => () => requestRef.current?.abort(), []);
  useEffect(() => { onSourceChange?.(sourceMode); }, [sourceMode, onSourceChange]);
  useEffect(() => {
    const request = new AbortController();
    setUploadsError("");
    apiRequest("/api/live/uploads", {signal: request.signal})
      .then(data => { if (!request.signal.aborted) setUploads(data.uploads); })
      .catch(error => { if (!request.signal.aborted) setUploadsError(error.message); });
    return () => request.abort();
  }, [uploadsRevision]);
  useEffect(() => {
    const request = new AbortController();
    setTicketsError("");
    apiRequest("/api/actions/tickets", {signal: request.signal})
      .then(data => {
        if (request.signal.aborted) return;
        setWebhookConfigured(Boolean(data.webhook_configured));
        setTickets(current => [...current.filter(item => !data.tickets.some(server => getTicketId(server) === getTicketId(item))), ...data.tickets]);
        setSelectedId(current => current ?? getTicketId(data.tickets[0]));
      })
      .catch(error => { if (!request.signal.aborted) setTicketsError(error.message); });
    return () => request.abort();
  }, [ticketsRevision]);
  useEffect(() => {
    if (!openedTicket) return;
    const id = getTicketId(openedTicket);
    setTickets(current => current.some(item => getTicketId(item) === id)
      ? current : [openedTicket, ...current]);
    setSelectedId(id);
  }, [openedTicket]);

  async function startReplay(event) {
    event.preventDefault();
    if (!replayDay || requestRef.current) return;
    const controller = new AbortController();
    requestRef.current = controller;
    const limit = threshold !== "" ? Number(threshold) : null;
    setActiveThreshold(limit);
    setStreamSource(null);
    setTicks([]);
    setAlerts([]);
    setStreamError("");
    setStatus("running");
    try {
      const response = await fetch("/api/live/stream", {
        method: "POST",
        headers: authHeaders({...JSON_HEADERS, Accept: "text/event-stream"}),
        body: JSON.stringify({day: replayDay, speed_ms: Number(speed), custom_threshold_kv: limit}),
        signal: controller.signal,
      });
      await consumeAgentStream(response, frame => {
        if (controller.signal.aborted) return;
        const event = {...frame, ...frame.data};
        if (frame.type === "telemetry_tick") {
          setStreamSource(event.source === "live_upload" ? "sandbox" : "drill");
          setTicks(current => [...current, normalizeTick(event)].slice(-96));
          setActiveThreshold(event.threshold_kv ?? limit);
        }
        if (frame.type === "live_alert") setAlerts(current => [event.alert ?? event, ...current]);
        if (frame.type === "done") setStatus("done");
      }, "实时监测演练");
    } catch (err) {
      if (!controller.signal.aborted) {
        setStatus("error");
        setStreamError(err.message);
      }
    } finally {
      if (requestRef.current === controller) requestRef.current = null;
    }
  }

  function stopReplay() {
    requestRef.current?.abort();
    requestRef.current = null;
    setStatus("stopped");
  }

  async function draftAlert(fields) {
    setDraftError("");
    setDrafting(true);
    try {
      await onDraft(fields);
    } catch (err) {
      setDraftError(err.message);
    } finally {
      setDrafting(false);
    }
  }

  function replaceTicket(updated) {
    if (!updated) return;
    setTickets(current => current.map(item => getTicketId(item) === getTicketId(updated) ? updated : item));
  }

  return <div className="liveops">
    <style>{`
      .liveops { max-width: 1340px; margin: auto; display: grid; gap: 15px; color: var(--duty-ink); }
      .liveops h1, .liveops h2, .liveops h3, .liveops p, .liveops figure { margin: 0; }
      .liveops h1 { font-size: clamp(23px, 3vw, 31px); letter-spacing: -.02em; }
      .liveops h2 { display: flex; align-items: center; gap: 8px; font-size: 16px; }
      .liveops h3 { font-size: 15px; }
      .liveops-columns { display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(320px, 1fr); align-items: start; gap: 15px; }
      .liveops-stack, .liveops-panel, .liveops-ticket, .liveops-alert, .liveops-upload-form { display: grid; gap: 11px; min-width: 0; }
      .liveops-panel { padding: clamp(13px, 2vw, 19px); border: 1px solid var(--duty-line); border-radius: 14px; background: var(--duty-paper); }
      .liveops-card-head { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 8px; }
      .liveops-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
      .liveops-controls { display: grid; grid-template-columns: repeat(auto-fit, minmax(123px, 1fr)); gap: 8px; }
      .liveops-field, .liveops-ticket-form label { display: grid; gap: 4px; min-width: 0; font-size: 13px; font-weight: 550; }
      .liveops-field input, .liveops-field select, .liveops-field textarea, .liveops-ticket-form textarea { width: 100%; min-width: 0; box-sizing: border-box;
        padding: 7px 8px; background: var(--surface-subtle); border: 1px solid var(--duty-line); border-radius: 8px; color: inherit; font: inherit; }
      .liveops-field textarea, .liveops-ticket-form textarea { resize: vertical; }
      .liveops-meta { color: var(--duty-muted); font-size: 12px; }
      .liveops-error { color: var(--duty-alert); font-size: 13px; overflow-wrap: anywhere; }
      .liveops-wrap { white-space: pre-wrap; overflow-wrap: anywhere; }
      .liveops-run-state { display: flex; align-items: center; gap: 7px; }
      .liveops-charts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
      .liveops-trend { padding: 9px; min-width: 0; background: var(--duty-rail); border: 1px solid var(--duty-line); border-radius: 10px; }
      .liveops-trend figcaption { display: flex; align-items: center; justify-content: space-between; font-size: 13px; }
      .liveops-reading { font-weight: 650; font-variant-numeric: tabular-nums; color: var(--duty-accent); }
      .liveops-reading small { font-weight: 450; }
      .liveops-trend svg { display: block; width: 100%; height: auto; }
      .liveops-trend svg text { font-size: 13px; fill: var(--duty-muted); }
      .liveops-alert, .liveops-ticket, .liveops-upload-receipt { padding: 10px 12px; border: 1px solid var(--duty-line); border-radius: 9px; font-size: 13px; overflow-wrap: anywhere; }
      .liveops-alert { border-inline-start: 3px solid var(--duty-alert); }
      .liveops-diagnosis { padding: 8px; background: var(--duty-accent-soft); border-radius: 7px; }
      .liveops-diagnosis p { margin-top: 3px; }
      .liveops-drop { display: grid; justify-items: center; gap: 6px; padding: 12px; border: 1px dashed var(--duty-line);
        border-radius: 9px; background: var(--duty-rail); font-size: 13px; }
      .liveops-drop.is-dragging { border-color: var(--duty-accent); background: var(--duty-accent-soft); }
      .liveops-drop input { max-width: 100%; font: inherit; }
      .liveops-ticket-form, .liveops-checklist { display: grid; gap: 8px; min-width: 0; }
      .liveops-checklist { margin: 0; border: 1px solid var(--duty-line); border-radius: 8px; padding: 8px 9px; }
      .liveops-checklist legend { font-weight: 650; padding: 0 4px; }
      .liveops-check { display: flex; align-items: baseline; gap: 7px; padding: 2px 0; }
      .liveops-check input { accent-color: var(--duty-accent); }
      .liveops-check-actor { display: block; }
      .liveops-history { min-width: 0; font-size: 13px; }
      .liveops-history summary { cursor: pointer; }
      .liveops-history summary svg { display: inline; vertical-align: -3px; }
      .liveops-history ol, .liveops-upload-receipt ul { padding-inline-start: 22px; margin: 6px 0 0; }
      .liveops-history li { padding: 3px 0; overflow-wrap: anywhere; }
      .liveops-history li span { display: block; color: var(--duty-muted); font-size: 12px; }
      @media (max-width: 920px) { .liveops-columns { grid-template-columns: minmax(0, 1fr); } }
      @media (max-width: 560px) { .liveops-charts { grid-template-columns: minmax(0, 1fr); } }
    `}</style>
    <h1>实时态势与两票审批</h1>
    <div className="liveops-columns">
      <div className="liveops-stack">
        <section className="liveops-panel" aria-label="实时监测演练">
          <div className="liveops-card-head"><h2><Activity size={18} /> 实时监测演练</h2>
            <Badge variant="neutral" label="归档与增量沙盒回放" /></div>
          <form className="liveops-stack" onSubmit={startReplay}>
            <div className="liveops-controls">
              <label className="liveops-field">演练日<select aria-label="演练日" value={replayDay} disabled={running} onChange={e => { setDate(e.target.value); setStreamSource(null); }}>
                {!replayDays.length && <option value="">暂无可回放日期</option>}
                {replayDays.map(value => <option key={value} value={value}>{value}</option>)}
              </select></label>
              <label className="liveops-field">倍速<select aria-label="倍速" value={speed} disabled={running} onChange={e => setSpeed(e.target.value)}>
                {RATE_OPTIONS.map(([ms, label]) => <option key={ms} value={ms}>{label}</option>)}
              </select></label>
              <label className="liveops-field">电压上限（kV）<input aria-label="电压上限（kV）" type="number" min="0.01" max="60" step="any"
                value={threshold} disabled={running} onChange={e => setThreshold(e.target.value)} placeholder="可选" /></label>
            </div>
            <div className="liveops-actions">
              {running
                ? <Button type="button" size="sm" label="停止回放" icon={<Square size={14} />} onClick={stopReplay} />
                : <Button type="submit" size="sm" variant="primary" label="开始演练" icon={<Play size={14} />} disabled={!replayDay} />}
              {running && <small className="liveops-run-state" role="status"><Spinner size="sm" />正在回放</small>}
              {status === "done" && <small className="liveops-meta" role="status">回放完成</small>}
              {status === "stopped" && <small className="liveops-meta" role="status">已停止接收</small>}
            </div>
          </form>
          {streamError && <p className="liveops-error" role="alert">{streamError}</p>}
          <div className="liveops-card-head">
            <div className="liveops-actions">
              <label className="liveops-field">电压测点<select aria-label="电压测点" value={voltageCabinet} onChange={e => setVoltageCabinet(e.target.value)}>
                <option value="1006">1006 柜</option><option value="1005">1005 柜</option>
              </select></label>
              <label className="liveops-field">电流测点<select aria-label="电流测点" value={currentCabinet} onChange={e => setCurrentCabinet(e.target.value)}>
                <option value="303">303 柜</option><option value="1010">1010 柜</option>
              </select></label>
            </div>
            {ticks.length > 0 && <div className="liveops-actions">
              <p className="liveops-meta">最新整点 {ticks.at(-1).time}</p>
              <Badge variant="neutral" label={ticks.at(-1).source === "live_upload" ? "增量沙盒" : "只读归档"} />
            </div>}
          </div>
          <div className="liveops-charts">
            <Trend ticks={ticks} cabinet={voltageCabinet} metric="voltage" unit="kV" threshold={activeThreshold} />
            <Trend ticks={ticks} cabinet={currentCabinet} metric="current" unit="A" />
          </div>
        </section>

        <section className="liveops-panel" aria-label="预诊断告警">
          <h2><AlertTriangle size={18} /> 预诊断告警 {alerts.length || ""}</h2>
          {draftError && <p className="liveops-error" role="alert">{draftError}</p>}
          {!alerts.length && <p className="liveops-meta">{running ? "正在核对回放数据。" : "暂无回放告警。"}</p>}
          {alerts.map((alert, i) => <AlertCard key={alert.alert_id ?? alert.id ?? i} alert={alert} onDraft={draftAlert} busy={drafting} />)}
        </section>
      </div>

      <div className="liveops-stack">
        <section className="liveops-panel" aria-label="两票审批">
          <h2><ClipboardList size={18} /> 两票审批</h2>
          <p className="liveops-meta">演练流转。现场操作仍按矿井审批流程执行。</p>
          {ticketsError && <div className="liveops-error" role="alert">{ticketsError}
            <Button label="重新读取票据" size="sm" onClick={() => setTicketsRevision(value => value + 1)} /></div>}
          {tickets.length > 0 ? <label className="liveops-field">选择票据
            <select aria-label="选择票据" value={selectedId ?? ""} onChange={e => setSelectedId(e.target.value)}>
              {tickets.map(item => <option key={getTicketId(item)} value={getTicketId(item)}>
                {getTicketId(item)} · {ACTION_KIND_LABELS[item.kind] ?? item.kind} · {item.target_cabinet} 柜
              </option>)}
            </select>
          </label> : !ticketsError && <p className="liveops-meta">可从回放告警或异常工单起草票据。</p>}
          {ticket && <ActionApprovalCard key={selectedId} ticket={ticket} onChange={replaceTicket} webhookConfigured={webhookConfigured} />}
        </section>
        <UploadPanel uploads={uploads} onUploaded={result => {
          const daysCovered = result.days_covered ?? [];
          setUploadedDays(current => [...new Set([...current, ...daysCovered])]);
          if (!requestRef.current && daysCovered.length) {
            setDate(daysCovered.at(-1));
            setStreamSource(null);
          }
          setUploadsRevision(current => current + 1);
        }}
          uploadListError={uploadsError} onRetry={() => setUploadsRevision(current => current + 1)} />
      </div>
    </div>
  </div>;
}
