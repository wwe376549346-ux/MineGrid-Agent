import React, {useEffect, useState} from "react";
import {Badge} from "@astryxdesign/core/Badge";
import {Button} from "@astryxdesign/core/Button";
import {Activity, Cable, FileCode2, Plus, RefreshCw, Server} from "lucide-react";
import {apiRequest, formatNum} from "./api.js";
import {SkillRunReceipt, AgentStreamStyles} from "./AgentStreamArtifacts.jsx";
import "./advanced-agent.css";

const JSON_HEADERS = {"Content-Type": "application/json"};
const COUNTS = {
  autonomous_turns: "自主问答完成", memory_suggestions: "记忆建议", memory_writes: "记忆写入",
  debate_runs: "辩论完成", skill_installs: "技能安装", skill_runs: "技能运行",
  mcp_registrations: "MCP Client 注册", protocol_frames: "遥测帧接入",
  sandbox_runs: "沙盒运行", sandbox_successes: "沙盒成功", verification_samples: "核验样本",
  verified_turns: "自主核验通过", model_calls: "模型调用",
};

function useResource(url) {
  const [state, setState] = useState({data: null, pending: true, error: ""});
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    const ctrl = new AbortController();
    setState(previous => ({...previous, pending: true, error: ""}));
    apiRequest(url, {signal: ctrl.signal})
      .then(data => { if (!ctrl.signal.aborted) setState({data, pending: false, error: ""}); })
      .catch(error => {
        if (!ctrl.signal.aborted) setState(previous => ({...previous, pending: false, error: error.message}));
      });
    return () => ctrl.abort();
  }, [url, revision]);

  return {...state, refresh: () => setRevision(value => value + 1)};
}

function usePost() {
  const [state, setState] = useState({busy: false, result: null, error: ""});

  async function submit(url, body) {
    setState({busy: true, result: null, error: ""});
    try {
      const result = await apiRequest(url, {method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body)});
      setState({busy: false, result, error: ""});
      return result;
    } catch (error) {
      setState({busy: false, result: null, error: error.message});
      return null;
    }
  }

  return {...state, submit};
}

function Notice({error, pending}) {
  return error ? <p className="advanced-error" role="alert">{error}</p>
    : pending ? <p className="muted" role="status">正在读取…</p> : null;
}

function Receipt({result, label = "查看回执"}) {
  return result && <details open>
    <summary>{label}</summary><pre>{JSON.stringify(result, null, 2)}</pre>
  </details>;
}

function SkillsCard({onChange}) {
  const list = useResource("/api/skills");
  const installation = usePost();
  const run = usePost();
  const [markdown, setMarkdown] = useState("");
  const [skillId, setSkillId] = useState("");
  const [title, setTitle] = useState("");
  const [cabinet, setCabinet] = useState("");
  const [atTime, setAtTime] = useState("");
  const [runningId, setRunningId] = useState("");
  const skills = list.data?.skills ?? [];

  async function install(event) {
    event.preventDefault();
    if (!markdown.trim() || installation.busy) return;
    if (await installation.submit("/api/skills", {skill_id: skillId, title, body_markdown: markdown})) {
      setMarkdown("");
      setSkillId("");
      setTitle("");
      list.refresh();
      onChange();
    }
  }

  async function execute(id) {
    setRunningId(id);
    await run.submit("/api/skills/" + encodeURIComponent(id) + "/run", {
      target_cabinet: cabinet.trim() || undefined, at_time: atTime.trim() || undefined,
    });
    onChange();
  }

  return <section className="advanced-card">
    <h3><FileCode2 size={17} /> Markdown 技能</h3>
    <Notice {...list} />
    {!list.pending && !list.error && !skills.length && <p className="muted">暂未安装技能。</p>}
    {skills.length > 0 && <div className="advanced-controls">
      <label>目标柜号（可选）<input inputMode="numeric" maxLength={16} value={cabinet} onChange={e => setCabinet(e.target.value)} /></label>
      <label>参考整点（可选）<input type="datetime-local" step={3600} value={atTime} onChange={e => setAtTime(e.target.value)} /></label>
    </div>}
    <ul className="advanced-records">
      {skills.map(skill => <li key={skill.id}>
        <div><strong>{skill.title ?? skill.id}</strong> <code>/{skill.id}</code>
          {skill.steps?.length > 0 && <p>{skill.steps.length} 条步骤 · 默认 {skill.default_cabinet} 柜</p>}</div>
        <Button size="sm" label={run.busy && skill.id === runningId ? "运行中…" : "运行"} disabled={run.busy} onClick={() => execute(skill.id)} />
      </li>)}
    </ul>
    <Notice error={run.error} />
    <SkillRunReceipt result={run.result} />
    <details>
      <summary>新增技能</summary>
      <form className="advanced-form" onSubmit={install}>
        <label>技能 ID<input required pattern={/^[a-z0-9][a-z0-9_\-]{1,47}$/.source} value={skillId} onChange={e => setSkillId(e.target.value)}
          placeholder="shift_voltage_check" /></label>
        <label>技能标题<input required maxLength={120} value={title} onChange={e => setTitle(e.target.value)} /></label>
        <label>技能 Markdown<textarea required rows={7} maxLength={8000} value={markdown}
          onChange={e => setMarkdown(e.target.value)} /></label>
        <Button type="submit" icon={<Plus size={14} />} label={installation.busy ? "正在安装…" : "安装技能"}
          disabled={installation.busy || !skillId.trim() || !title.trim() || !markdown.trim()} />
        <Notice error={installation.error} />
        {installation.result && <p className="muted" role="status">技能已安装</p>}
      </form>
    </details>
  </section>;
}

function MCPCard({onChange}) {
  const list = useResource("/api/mcp/clients");
  const registration = usePost();
  const [name, setName] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [transport, setTransport] = useState("http");
  const [description, setDescription] = useState("");
  const clients = list.data?.clients ?? [];

  async function register(event) {
    event.preventDefault();
    if (registration.busy) return;
    if (await registration.submit("/api/mcp/clients", {name, endpoint, transport, description, read_only: true})) {
      setName("");
      setEndpoint("");
      setDescription("");
      list.refresh();
      onChange();
    }
  }

  return <section className="advanced-card">
    <h3><Server size={17} /> 只读 MCP Client</h3>
    <Notice {...list} />
    {!list.pending && !list.error && !clients.length && <p className="muted">暂无已注册客户端。</p>}
    <ul className="advanced-records">{clients.map(client => <li key={[client.client_id, client.transport, client.endpoint].join(":")}>
      <div><strong>{client.name}</strong><p>{client.transport} · {client.endpoint}</p>
        {client.description && <p>{client.description}</p>}</div>
      <Badge variant="neutral" label="只读" />
    </li>)}</ul>
    <details>
      <summary>注册客户端</summary>
      <form className="advanced-form" onSubmit={register}>
        <label>客户端名称<input required maxLength={80} value={name} onChange={e => setName(e.target.value)} /></label>
        <label>传输方式<select value={transport} onChange={e => { setTransport(e.target.value); setEndpoint(""); }}>
          <option value="http">HTTP(S)</option><option value="stdio">stdio</option>
        </select></label>
        <label>服务端点<input required type={transport === "http" ? "url" : "text"} maxLength={240}
          value={endpoint} onChange={e => setEndpoint(e.target.value)} /></label>
        <label>用途（可选）<input maxLength={240} value={description} onChange={e => setDescription(e.target.value)} /></label>
        <Button type="submit" label={registration.busy ? "正在注册…" : "注册只读客户端"}
          disabled={registration.busy || !name.trim() || !endpoint.trim()} />
        <Notice error={registration.error} />
        {registration.result && <p className="muted" role="status">客户端已注册</p>}
      </form>
    </details>
  </section>;
}

function ProtocolCard({onChange}) {
  const list = useResource("/api/protocols/adapters");
  const ingestion = usePost();
  const [protocol, setProtocol] = useState("");
  const [frame, setFrame] = useState("");
  const [parseError, setParseError] = useState("");
  const adapters = list.data?.adapters ?? [];
  const adapterId = item => item.protocol ?? item.id;
  const selected = adapters.find(item => adapterId(item) === protocol) ?? adapters[0];

  async function ingest(event) {
    event.preventDefault();
    if (!selected || ingestion.busy) return;
    setParseError("");
    let parsed;
    try {
      parsed = JSON.parse(frame);
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error("object required");
    } catch {
      setParseError("请填写一个 JSON 对象格式的遥测帧。");
      return;
    }
    await ingestion.submit("/api/protocols/ingest-frame", {protocol: adapterId(selected), payload: parsed});
    onChange();
  }

  return <section className="advanced-card">
    <h3><Cable size={17} /> 遥测帧接入演练</h3>
    <Notice {...list} />
    {!list.pending && !list.error && !adapters.length && <p className="muted">暂无可用适配器。</p>}
    {selected && <form className="advanced-form" onSubmit={ingest}>
      <div className="advanced-controls">
        <label>协议<select value={adapterId(selected)} onChange={e => { setProtocol(e.target.value); setFrame(""); setParseError(""); }}>
          {adapters.map(item => <option key={adapterId(item)} value={adapterId(item)}>{item.label ?? item.name ?? adapterId(item)}</option>)}
        </select></label>
        {selected.sample_payload && <Button type="button" size="sm" label="填入示例帧"
          onClick={() => setFrame(JSON.stringify(selected.sample_payload, null, 2))} />}
      </div>
      <label>遥测帧 JSON<textarea required className="mono" rows={8} maxLength={32768} value={frame} onChange={e => setFrame(e.target.value)} /></label>
      <Button type="submit" variant="primary" label={ingestion.busy ? "正在解析…" : "解析演练帧"} disabled={ingestion.busy || !frame.trim()} />
      <Notice error={parseError || ingestion.error} />
      <Receipt result={ingestion.result} label="遥测帧解析回执" />
    </form>}
  </section>;
}

function MetricsCard({metrics}) {
  const data = metrics.data;
  const tiles = data ? [
    ["平均 TTFT", data.ttft_ms_avg, " ms"],
    ["P95 TTFT", data.ttft_ms_p95, " ms"],
    ["Token 总量（含估算）", data.total_tokens, ""],
    ["沙盒成功率", data.sandbox_success_rate_pct, "%"],
    ["自主核验通过率", data.verification_rate_pct, "%"],
  ] : [];
  return <section className="advanced-card">
    <header className="advanced-title"><h3><Activity size={17} /> Agent 可观测评测</h3>
      <Button size="sm" icon={<RefreshCw size={14} />} label="刷新指标" onClick={metrics.refresh} disabled={metrics.pending} /></header>
    <Notice {...metrics} />
    {data && <>
      <p className="muted">当前 API 进程 {data.worker_pid}。成功率以该进程收到的样本为准。</p>
      <dl className="advanced-metrics">
        {tiles.map(([name, value, suffix]) => <div key={name}><dt>{name}</dt><dd>{value == null ? "—" : formatNum(value) + suffix}</dd></div>)}
      </dl>
      <dl className="advanced-counts">
        {[["启用记忆", data.operational_memory?.enabled], ["已安装技能", data.ecosystem?.skills_count],
          ["已登记客户端", data.ecosystem?.mcp_clients_count], ["沙盒导入", data.live_ingestion?.uploads_count]].map(([label, value]) =>
          <div key={label}><dt>{label}</dt><dd>{formatNum(value)}</dd></div>)}
      </dl>
      <dl className="advanced-counts">{Object.entries(COUNTS).map(([key, name]) => <div key={key}>
        <dt>{name}</dt><dd>{formatNum(data.counts?.[key])}</dd>
      </div>)}</dl>
    </>}
  </section>;
}

export function SkillsProtocolsView() {
  const metrics = useResource("/api/eval/observability");
  return <div className="advanced-workspace">
    <AgentStreamStyles />
    <MetricsCard metrics={metrics} />
    <div className="advanced-columns">
      <SkillsCard onChange={metrics.refresh} />
      <MCPCard onChange={metrics.refresh} />
    </div>
    <ProtocolCard onChange={metrics.refresh} />
  </div>;
}
