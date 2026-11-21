import React, {useEffect, useReducer, useRef, useState} from "react";
import {Badge} from "@astryxdesign/core/Badge";
import {Button} from "@astryxdesign/core/Button";
import {Card} from "@astryxdesign/core/Card";
import {TextInput} from "@astryxdesign/core/TextInput";
import {apiRequest, signedKw, sourceUrl} from "./api.js";
import {begin, clock, hourOf, LAST_HOUR, replay, STEP_MS, visibleEvents} from "./replay.js";
import {Trend, format} from "./Trend.jsx";

const sourceLabel = source => source.file + (source.sheet ? " · " + source.sheet + "!" + source.cell : "");

function evidenceFrom(answer) {
  const sources = new Map();
  for (const relation of answer.comparisons || []) {
    for (const point of [relation.before, relation.after]) {
      sources.set(sourceLabel(point.source), point.source);
    }
  }
  if (answer.source) sources.set(sourceLabel(answer.source), answer.source);
  for (const step of answer.steps || []) {
    for (const link of step.result.links || []) {
      for (const source of link.sources) {
        sources.set(source.filename, {file: source.filename});
      }
    }
  }
  return [...sources.values()];
}

function ReviewChat({event, model}) {
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState("local");
  const [reply, setReply] = useState(null);
  const [problem, setProblem] = useState("");
  const [waiting, setWaiting] = useState(false);
  const inFlight = useRef(null);
  useEffect(() => () => inFlight.current?.abort(), []);
  const compareCabinet = event.cabinet === "306" ? "303" : "306";
  const sample = compareCabinet + " 柜同期也变化了吗？";

  async function submit(text) {
    if (!text.trim() || inFlight.current) return;
    const active = new AbortController();
    inFlight.current = active;
    setWaiting(true);
    setReply(null);
    setProblem("");
    try {
      const result = await apiRequest("/api/ask", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({question: text, mode, eventId: event.id}),
        signal: active.signal,
      });
      setReply({...result, question: text, mode});
    } catch (error) {
      if (error.name !== "AbortError") setProblem(error.message);
    } finally {
      if (!active.signal.aborted) setWaiting(false);
      inFlight.current = null;
    }
  }

  return <section className="review-chat" aria-label="值班问答">
    <div className="chat-heading">
      <h3>同期事件快速追问</h3>
      <label className="select-pill compact">
        <select value={mode} disabled={waiting} onChange={e => setMode(e.target.value)}>
          <option value="local">本地规则</option>
          <option value="deepseek" disabled={!model}>DeepSeek 复核</option>
        </select>
      </label>
    </div>
    <Button type="button" label={sample} variant="secondary" size="sm"
      isDisabled={waiting} onClick={() => {setQuestion(sample); submit(sample);}} />
    <form className="ask-form" onSubmit={e => {e.preventDefault(); submit(question);}}>
      <TextInput label="复核问题" isLabelHidden width="100%" value={question} onChange={setQuestion}
        placeholder="输入要核对的同期问题" maxLength={400} />
      <Button type="submit" label="发送" variant="primary" isDisabled={waiting || !question.trim()} />
    </form>
    {waiting && <p className="muted" role="status">{mode === "deepseek" ? "等待 DeepSeek 回执…" : "正在核对归档…"}</p>}
    {problem && <p className="save-err" role="alert">{problem}</p>}
    {reply && <div className="answer" role="log" aria-label="问答回执">
      <div className="answer-head"><strong>{reply.question}</strong><Badge label={reply.mode === "deepseek" ? "DeepSeek" : "本地规则"} /></div>
      {reply.answer.comparisons?.map(item => <p className="answer-metric" key={item.cabinet}>
        {item.cabinet} 柜 <strong>{signedKw(item.change_kw)} kW</strong>
      </p>)}
      <p className="answer-text">{reply.text}</p>
      <div className="inline-cells">
        {evidenceFrom(reply.answer).map(source =>
          <a className="cell-chip" key={sourceLabel(source)} href={sourceUrl(source.file)} download>{sourceLabel(source)}</a>
        )}
      </div>
    </div>}
  </section>;
}

function ReplayDashboard({report, model, onAskDiagnose}) {
  const [state, dispatch] = useReducer(replay, report.events, begin);
  const [selected, setSelected] = useState(null);
  const visible = visibleEvents(state);
  const event = visible.find(item => item.id === selected);
  const alert = state.alert?.events.at(-1);

  useEffect(() => {
    if (!state.playing) return;
    const timer = window.setInterval(() => dispatch({type: "tick"}), STEP_MS);
    return () => window.clearInterval(timer);
  }, [state.playing]);

  useEffect(() => {
    if (!state.alert) return;
    const timer = window.setTimeout(() => dispatch({type: "dismiss"}), 8500);
    return () => window.clearTimeout(timer);
  }, [state.alert]);

  function send(action) {
    if (action.type === "reset" || (action.type === "seek" && event && hourOf(event) > action.hour)) setSelected(null);
    dispatch(action);
  }
  function open(item) {
    dispatch({type: "pause"});
    dispatch({type: "dismiss"});
    setSelected(item.id);
  }

  const progress = (100 * state.hour) / LAST_HOUR;

  return <>
    <Card padding={18} className="playback-card">
      <div className="playback-row">
        <div className="clock-group">
          <div className="clock-pill" data-hour={state.hour}>{clock(state.hour)}</div>
          <div>
            <h2>四柜小时级历史回放</h2>
            <p className="muted">{report.source.day} · {state.playing ? "自动回放中" : state.hour === LAST_HOUR ? "已回放至 23:00" : "已暂停"}</p>
          </div>
        </div>
        <div className="playback-btns">
          <Button size="md" variant="primary"
            label={state.playing ? "暂停回放" : state.hour === LAST_HOUR ? "重新回放" : "开始回放"}
            onClick={() => send({type: state.playing ? "pause" : "play"})} />
          <Button size="md" variant="secondary" label="前进 1 小时"
            isDisabled={state.playing || state.hour === LAST_HOUR} onClick={() => send({type: "step"})} />
          <Button size="md" variant="secondary" label="回到 00:00" onClick={() => send({type: "reset"})} />
        </div>
      </div>
      <label className="replay-track">
        <span className="sr-only">回放整点</span>
        <input type="range" min={0} max={LAST_HOUR} step={1} value={state.hour}
          style={{"--progress": progress + "%"}}
          onChange={e => send({type: "seek", hour: Number(e.target.value)})} />
      </label>
      <div className="track-labels" aria-hidden="true">
        {[0, 6, 12, 18, 23].map(h => <span key={h}>{clock(h)}</span>)}
      </div>
    </Card>

    <div className="replay-grid">
      <Trend report={report} hour={state.hour} />
      <Card padding={0} className="replay-side">
        {event ? <div className="detail-content">
          <div className="detail-top">
            <Button size="sm" variant="secondary" label="返回提示列表" onClick={() => setSelected(null)} />
            <Button size="sm" variant="primary" label="转入深度诊断台"
              onClick={() => onAskDiagnose(`请结合全站 7 天基线和功率平衡，诊断历史回放事件 ${event.id}（${event.time}，${event.cabinet} 柜有功变化 ${signedKw(event.change_kw)} kW）属于常规避峰波动还是异常？`)} />
          </div>
          <p className="mono muted">{event.id} · {event.time.slice(0, 16)}</p>
          <h2 className="detail-title">{event.cabinet} 柜有功变化 {signedKw(event.change_kw)} kW</h2>
          <div className="evidence-chips">
            {[["前一整点", event.before], ["触发整点", event.after]].map(([lbl, pt]) => <a
              key={lbl}
              className="evidence-chip"
              href={sourceUrl(pt.source.file)}
              download
            >
              <span className="mono">{lbl} · {pt.time.slice(5, 16)}</span>
              <strong>{pt.source.file} · {pt.source.sheet}!{pt.source.cell}</strong>
              <small>{format(pt.value_kw)} kW</small>
            </a>)}
          </div>
          <ReviewChat key={event.id} event={event} model={model} />
        </div> : <>
          <div className="pane-head">
            <div>
              <h2>待复核变化提示</h2>
              <p className="muted">相邻整点有功变化 ≥ {format(report.threshold_kw)} kW</p>
            </div>
            <Badge variant="neutral" label={`${visible.length} / ${report.events.length}`} />
          </div>
          {visible.length === 0 ? <div className="empty-pane">
            回放到触发时刻时，提示会自动出现（例如 11 月 10 日 12:00 触发 LW-000010）。
          </div> : <ul className="event-list">
            {[...visible].reverse().map(item => <li key={item.id}>
              <button type="button" className="finding-row" data-event-id={item.id} onClick={() => open(item)}>
                <div className="finding-row-top">
                  <span className="mono">{item.time.slice(11, 16)} · {item.id}</span>
                  <Badge variant={item.change_kw > 0 ? "warning" : "info"} label={`${signedKw(item.change_kw)} kW`} />
                </div>
                <strong>{item.cabinet} 柜有功变化</strong>
              </button>
            </li>)}
          </ul>}
        </>}
      </Card>
    </div>

    {alert && <div className="toast-alert" role="status">
      <div>
        <strong>{state.alert.events.length > 1 ? `${state.alert.events.length} 条新提示` : "触发历史回放提示"}</strong>
        <p>{alert.time.slice(11, 16)} · {alert.cabinet} 柜 · {signedKw(alert.change_kw)} kW</p>
      </div>
      <Button size="sm" variant="primary" label="查看" onClick={() => open(alert)} />
      <Button size="sm" variant="secondary" label="×" onClick={() => dispatch({type: "dismiss"})} />
    </div>}
  </>;
}

export function ReplayView({dates, model, onAskDiagnose}) {
  const [date, setDate] = useState(dates[0] || "");
  const [report, setReport] = useState(null);
  const [diagrams, setDiagrams] = useState([]);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!date && dates.length) setDate(dates[0]);
  }, [dates, date]);

  useEffect(() => {
    if (!date) return;
    const ctrl = new AbortController();
    setErr("");
    apiRequest("/api/day?date=" + encodeURIComponent(date), {signal: ctrl.signal})
      .then(setReport)
      .catch(e => {if (e.name !== "AbortError") setErr(e.message);});
    return () => ctrl.abort();
  }, [date]);

  useEffect(() => {
    apiRequest("/api/diagrams").then(res => setDiagrams(res.diagrams || [])).catch(() => {});
  }, []);

  return <div className="view-stack">
    <div className="view-header">
      <div>
        <p className="eyebrow">原始日报回放与监控画面档案</p>
        <h1>历史回放与接线图</h1>
      </div>
      <div className="header-controls">
        <label className="select-pill">
          <span>回放日期</span>
          <select value={date} onChange={e => setDate(e.target.value)}>
            {dates.map(d => <option key={d} value={d}>{d}</option>)}
          </select>
        </label>
        {report && <a className="download-link" href={sourceUrl(report.source.filename)} download>
          下载 {report.source.filename} 原表
        </a>}
      </div>
    </div>

    {err && <Card padding={16}><p className="save-err" role="alert">{err}</p></Card>}
    {report?.source.day === date && <ReplayDashboard key={date + report.run_id} report={report} model={model} onAskDiagnose={onAskDiagnose} />}

    {diagrams.length > 0 && <section className="diagram-section">
      <h2>归档监控主接线截图</h2>
      <div className="two-col">
        {diagrams.map(fig => <Card key={fig.filename} padding={14} className="diagram-card">
          <a href={fig.image_url} target="_blank" rel="noreferrer">
            <img src={fig.image_url} alt={fig.filename} loading="lazy" />
          </a>
          <div className="diagram-foot">
            <div>
              <strong>{fig.filename}</strong>
              <span className="muted">截取时间：{fig.captured_at}</span>
            </div>
            <a className="cell-chip" href={fig.download_url} download>下载原图</a>
          </div>
        </Card>)}
      </div>
    </section>}
  </div>;
}
