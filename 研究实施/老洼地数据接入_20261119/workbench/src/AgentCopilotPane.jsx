import React, {useCallback, useEffect, useMemo, useRef, useState} from "react";
import {
  Activity,
  ArrowUp,
  BookmarkCheck,
  BookmarkPlus,
  CheckCircle2,
  ClipboardList,
  ChevronDown,
  ChevronRight,
  Cpu,
  Download,
  FileSpreadsheet,
  GitBranch,
  Layers,
  MessageSquare,
  PauseCircle,
  Play,
  Plus,
  RefreshCw,
  Search,
  ShieldAlert,
  ShieldCheck,
  Slash,
  Sparkles,
  Square,
  Trash2,
  Wrench,
  X,
  Zap,
} from "lucide-react";
import {Badge} from "@astryxdesign/core/Badge";
import {ProgressBar} from "@astryxdesign/core/ProgressBar";
import {Spinner} from "@astryxdesign/core/Spinner";
import {apiRequest, authHeaders, cellHref} from "./api.js";
import {AgentStreamArtifacts, AgentStreamStyles, SkillRunReceipt, applyAgentEvent, emptyAgentStream, restoreAutonomousTurn} from "./AgentStreamArtifacts.jsx";

export async function consumeAgentStream(response, onEvent, label = "自主深度 Agent") {
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.error || `请求失败 (${response.status})`);
  }
  if (!response.body || !response.headers.get("content-type")?.includes("text/event-stream")) {
    throw new Error(`${label}未返回事件流。`);
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let eventName = "";
  let data = [];
  try {
    while (true) {
      const {value, done} = await reader.read();
      if (done) throw new Error(`连接已提前结束，未收到${label}的完成事件。`);
      const lines = (buffer + value).split(/\r?\n/);
      buffer = lines.pop();
      for (const line of lines) {
        if (line.startsWith("event:")) {
          eventName = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          data.push(line.slice(5).replace(/^ /, ""));
        } else if (line === "") {
          if (data.length) {
            const payload = JSON.parse(data.join("\n"));
            const event = {...payload, type: eventName || payload.type};
            if (event.type === "error") {
              throw new Error(event.error ?? event.message ?? event.data?.error ?? `${label}执行中断。`);
            }
            onEvent(event);
            if (event.type === "done") return;
          }
          eventName = "";
          data = [];
        }
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

function SaveReplyMemory({question, content, cabinet, onOpenMemory}) {
  if (!content) return null;
  return <button type="button" className="chat-artifact-btn" onClick={() => onOpenMemory({
    category: "episodic",
    title: question.slice(0, 140),
    content: content.slice(0, 2000),
    scope_cabinet: cabinet ?? "",
  })}>
    <BookmarkPlus size={13} /> 写入长期记忆
  </button>;
}

function MemorySuggestion({draft}) {
  const [status, setStatus] = useState("ready");
  const [error, setError] = useState("");

  async function save() {
    setStatus("saving");
    setError("");
    try {
      await apiRequest("/api/memory", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(draft),
      });
      setStatus("saved");
    } catch (e) {
      setError(e.message);
      setStatus("ready");
    }
  }

  return <div className="duty-memory-suggestion">
    <div><small>检测到新矿井规则/经验</small><strong>{draft.title}</strong><p>{draft.content}</p></div>
    <div>{status === "saved" ? <span className="duty-memory-saved" role="status"><BookmarkCheck size={14} /> 已入库</span>
      : <button type="button" className="chat-artifact-btn" onClick={save} disabled={status === "saving"}>
        <BookmarkPlus size={14} /> {status === "saving" ? "正在入库…" : "一键写入长期记忆"}
      </button>}
      {error && <p className="chat-error-box" role="alert">{error}</p>}
    </div>
  </div>;
}

function SkillTurn({turn}) {
  return <div className="chat-turn">
    <div className="chat-user-row"><div className="chat-user-bubble">{turn.question}</div></div>
    <div className="chat-agent-row">
      <div className="chat-agent-avatar" aria-hidden="true"><Wrench size={14} /></div>
      <div className="chat-agent-body">
        {turn.running && <div className="duty-progress" role="status"><Spinner size="sm" /> 正在执行技能</div>}
        {turn.error && <div className="chat-error-box" role="alert">{turn.error}</div>}
        {turn.stopped && <small className="muted">已停止接收</small>}
        <SkillRunReceipt result={turn.result} />
      </div>
    </div>
  </div>;
}

function AutonomousTurn({turn, onTextProgress, onOpenMemory, onSyncCanvas, onDraftAction, onDeepResearch}) {
  const {stream} = turn;
  const {canvasHint, recommendedAction, deepResearchGoal} = stream;
  const [drafting, setDrafting] = useState(false);
  const [draftError, setDraftError] = useState("");
  const canDraft = ["switching_order", "defect_notice", "field_verification"].includes(recommendedAction?.kind)
    && /^[0-9]{1,16}$/.test(recommendedAction?.target_cabinet ?? "");

  async function draftAction() {
    const {kind, target_cabinet, at_time, rationale} = recommendedAction;
    setDraftError("");
    setDrafting(true);
    try {
      await onDraftAction({kind, target_cabinet: String(target_cabinet), at_time, rationale});
    } catch (error) {
      setDraftError(error.message);
    } finally {
      setDrafting(false);
    }
  }

  return <div className="chat-turn">
    <div className="chat-user-row"><div className="chat-user-bubble">{turn.question}</div></div>
    <div className="chat-agent-row">
      <div className="chat-agent-avatar" aria-hidden="true"><Sparkles size={14} /></div>
      <div className="chat-agent-body">
        <AgentStreamArtifacts stream={stream} onTextProgress={onTextProgress} restored={Boolean(turn.result)} />
        {turn.stream.status === "running" && <div className="duty-progress" role="status"><Spinner size="sm" /> 正在分析</div>}
        {turn.stream.status === "stopped" && <small className="muted">已停止接收</small>}
        {turn.error && <div className="chat-error-box" role="alert">{turn.error}</div>}
        {turn.stream.status === "done" && <div className="chat-artifact-actions">
          {canvasHint?.canvas && <button type="button" className="chat-artifact-btn" onClick={() => onSyncCanvas(canvasHint)}>
            <Layers size={13} /> 查看关联画布
          </button>}
          {canDraft && <button type="button" className="chat-artifact-btn" disabled={drafting} onClick={draftAction}>
            <ClipboardList size={13} /> {drafting ? "正在起草…" : "一键起草两票草案"}
          </button>}
          <button type="button" className="chat-artifact-btn" onClick={() => onDeepResearch(deepResearchGoal || turn.question)}>
            <Search size={13} /> 升级为深度专项排查
          </button>
          <SaveReplyMemory question={turn.question} content={stream.content} onOpenMemory={onOpenMemory} />
        </div>}
        {draftError && <div className="chat-error-box" role="alert">{draftError}</div>}
        {stream.memorySuggestions.map(({key, draft}) => <MemorySuggestion key={key} draft={draft} />)}
      </div>
    </div>
  </div>;
}

const AUDIT_PROMPTS = [
  {
    label: "查整点读数",
    detail: "303 柜有功",
    Icon: FileSpreadsheet,
    text: "2026年11月13日15:00，303柜的有功功率是多少？请给出原表出处。",
  },
  {
    label: "回查监测事件",
    detail: "LW-000010",
    Icon: Activity,
    text: "回查监测事件 LW-000010，303柜当时的有功有什么变化？",
  },
  {
    label: "比较同期读数",
    detail: "303 柜和 306 柜",
    Icon: Layers,
    text: "比较2026年11月10日11:00至12:00，303柜和306柜各自的有功变化。",
  },
  {
    label: "查证反向有功",
    detail: "2 号主变",
    Icon: Activity,
    text: "2026年11月12日12:00，2号主变通道306/312/1006有功为负。请查证1027反送电、表计异常和母联环流的可能性。",
    runOnClick: true,
  },
  {
    label: "查证全周负有功",
    detail: "1018 二采区 II 回",
    Icon: Activity,
    text: "1018 馈线全周持续负有功和负功率因数是什么原因？是否二采区反送电？",
    runOnClick: true,
  },
  {
    label: "查证进线归零",
    detail: "306 二回进线",
    Icon: Activity,
    text: "2026年11月13日15:00，306柜有功和电流为何归零？请查证失压、表计死数和单进线转供。",
    runOnClick: true,
  },
];

const AUTONOMOUS_PROMPTS = [
  {
    label: "1006 相关性回归",
    detail: "有功与线电压散点图",
    Icon: Activity,
    text: "用老洼地全周整点数据分析 1006 柜有功功率与 Uab 线电压的相关性，给出回归散点图、样本数和原表出处。",
  },
  {
    label: "10 kV 波动排行",
    detail: "比较各馈线负荷",
    Icon: Layers,
    text: "按老洼地全周整点有功功率的标准差，列出 10 kV 馈线负荷波动前 5 名，用柱状图展示并说明缺测处理。",
  },
  {
    label: "2# 主变分接头潮流",
    detail: "档位与母线电压曲线",
    Icon: Cpu,
    text: "以 2026 年 11 月 13 日 17:00 为基准，复算 2# 主变各分接头档位对 10 kV II 段母线电压的影响，给出潮流曲线和假设。",
  },
  {
    label: "精准单点查库",
    detail: "303 柜原表坐标",
    Icon: FileSpreadsheet,
    text: "2026年11月13日15:00，303柜的有功功率是多少？请给出数值、单位和日报单元格坐标。",
  },
  {
    label: "1018 极性核查",
    detail: "负有功与负功率因数",
    Icon: GitBranch,
    text: "核查 1018 馈线全周负有功和负功率因数，比较计量极性与反送电两种解释，列出支持或反驳的原表证据和待核查资料。",
  },
  {
    label: "全站风险盘点",
    detail: "按证据列出关注项",
    Icon: ShieldAlert,
    text: "盘点老洼地全站过去 7 天的运行风险，按优先级列出异常柜、关键时刻、证据和需要现场复核的事项。",
  },
];

const MENTIONS = ["@303", "@306", "@1006", "@1009", "@1010", "@1012", "@1018", "@1020", "@1027", "@1101"];

const TASK_STATUS_LABELS = {
  completed: "任务已完成",
  partial: "任务部分完成",
  insufficient_data: "资料不足",
  clarification_required: "待确认查询范围",
  verification_failed: "证据核验未通过",
};

const TOOL_LABELS = {
  get_monitor_event: "回放事件原始取证",
  get_hourly_reading: "整点原表读数",
  get_daily_statistic: "日报统计取证",
  get_supply_link: "接线图供电关系",
  compare_aligned_readings: "同期有功变化计算",
  compare_scoped_values: "指定读数差值计算",
  get_station_health_overview: "全站四维电气体检",
  get_window_snapshot: "多柜时间窗快照",
  check_power_balance: "全站功率平衡核验",
  get_cabinet_health_profile: "单柜 7 天健康画像",
  get_diurnal_baseline: "分时段负荷基线对照",
  search_coal_standards: "《煤矿安全规程》混合检索",
  retrieve_conditional_evidence: "条件化混合 RAG 与适用性门控",
  verify_conditional_capacity: "v1→v2 条件载流裕度复算",
  query_electrical_analytics: "只读即席统计与联合筛选",
  simulate_electrical_engineering: "电力物理仿真 (无功Qc / N-1转供)",
  run_ac_power_flow: "pandapower 交流潮流求解器 (NR)",
};

const DISPOSITION_OPTIONS = [
  {value: "normal_operation", label: "正常生产 / 计划倒闸（自动降噪）"},
  {value: "confirmed_anomaly", label: "确认为异常（列入现场检修）"},
  {value: "needs_site_check", label: "待现场核查（持续追踪）"},
];

function downloadMarkdown(filename, content) {
  const blob = new Blob([content], {type: "text/markdown;charset=utf-8"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function LiveExecutionCard({liveTrace, elapsed}) {
  if (!liveTrace) return null;
  const steps = liveTrace.steps || [];
  const reflections = liveTrace.reflections || [];
  return <div className="chat-agent-row">
    <div className="chat-agent-avatar is-pulsing">
      <Spinner size="sm" />
    </div>
    <div className="live-stream-card">
      <div className="live-stream-head">
        <div className="live-stream-title">
          <strong>{liveTrace.phase || "智能体正在执行只读诊断…"}</strong>
          <span className="agent-mini-pill is-blue mono">{elapsed}s</span>
        </div>
        <small className="muted">
          {liveTrace.mode === "model"
            ? `DeepSeek 实时流式推理 · 11 工具沙箱${liveTrace.priorTurnsUsed ? ` · 已承接前 ${liveTrace.priorTurnsUsed} 轮记忆` : ""}`
            : `11 工具只读推理链 · 实时 SSE 观测${liveTrace.priorTurnsUsed ? ` · 已承接前 ${liveTrace.priorTurnsUsed} 轮记忆` : ""}`}
        </small>
      </div>

      {steps.length > 0 && (
        <div className="live-step-list">
          {steps.map(st => {
            const done = st.status !== "running";
            return <div key={st.step} className={"live-step-row" + (done ? " is-done" : " is-running")}>
              <span className="live-step-icon">
                {done ? <CheckCircle2 size={12} className="icon-green" /> : <RefreshCw size={12} className="spin-icon" />}
              </span>
              <div className="live-step-main">
                <div className="live-step-top">
                  <code className="mono">{st.tool}</code>
                  <span className="muted">{TOOL_LABELS[st.tool] || "只读工具"}</span>
                  {st.round > 1 && <span className="agent-mini-pill is-blue">第 {st.round} 轮</span>}
                  {st.gssc_saved_pct > 0 && <span className="agent-mini-pill">GSSC -{st.gssc_saved_pct}%</span>}
                </div>
                {st.hypothesis && <div className="live-step-summary">
                  <strong>{st.hypothesis.hypothesis} {st.hypothesis.title}</strong>
                  <div>{st.hypothesis.criterion}</div>
                </div>}
                {st.summary && <div className="live-step-summary">{st.summary}</div>}
              </div>
            </div>;
          })}
        </div>
      )}

      {/* Audit #06: Pre-final-review model text is strictly withheld until verification passes */}
      {liveTrace.streamChars > 0 && (
        <div className="live-delta-box">
          <div className="live-delta-head">
            <span>{liveTrace.streamKind === "tool_planning" ? "模型正在规划只读工具调用" : "正在撰写带出处的诊断"}</span>
            <span className="mono">{liveTrace.streamChars} chars</span>
          </div>
        </div>
      )}

      {reflections.map((rf, i) => (
        <div key={i} className="live-reflection-pill">
          <ShieldAlert size={12} />
          <span>{rf}</span>
        </div>
      ))}

      {liveTrace.verification && (
        <div className="live-verify-pill">
          {liveTrace.verification.status === "verified" ? (
            <ShieldCheck size={12} className="icon-green" />
          ) : (
            <ShieldAlert size={12} />
          )}
          <span>
            {liveTrace.verification.status === "verified"
              ? "证据核验完成"
              : liveTrace.verification.status === "insufficient_data"
                ? "归档资料不足"
                : liveTrace.verification.status === "not_applicable"
                  ? "待确认查询范围"
                  : liveTrace.verification.status === "restricted"
                    ? "请求受限"
                    : "证据核验未通过"}
            {liveTrace.verification.claim_checks_total > 0 &&
              <> · 断言 {liveTrace.verification.verified_claims_count}/{liveTrace.verification.claim_checks_total}</>}
            {liveTrace.verification.cited_cells_total > 0 &&
              <> · 原表 {liveTrace.verification.verified_cells}/{liveTrace.verification.cited_cells_total}</>}
            {liveTrace.verification.tool_result_checks_total > 0 &&
              <> · 工具回执 {liveTrace.verification.verified_tool_results_count}/{liveTrace.verification.tool_result_checks_total}</>}
          </span>
        </div>
      )}
    </div>
  </div>;
}

function InlineHitlCard({hitl, sessionId, onSyncCanvas}) {
  const isV02 = hitl.task_type === "underground_n1_capacity_audit" || hitl.target_item_id === "V02";
  const defaults = hitl.default_parameters || {};
  const placeholders = hitl.placeholder_examples || {};

  // Astra #06: Keep unverified field inputs strictly BLANK so assumptions can never be submitted as field truth.
  const [rated303, setRated303] = useState(String(defaults["303_rated_current_a"] ?? ""));
  const [ratedTr1, setRatedTr1] = useState(String(defaults.main_transformer_rated_kva ?? ""));
  const [switchLog, setSwitchLog] = useState(String(defaults.switch_log_20261113 ?? ""));
  const [rated1009, setRated1009] = useState(String(defaults["1009_1010_rated_current_a"] ?? ""));
  const [tieMode, setTieMode] = useState(String(defaults.c1535_tie_switch_mode ?? ""));
  const [derating, setDerating] = useState(String(defaults.ambient_derating_factor ?? ""));

  const [resuming, setResuming] = useState(false);
  const [resumedState, setResumedState] = useState(null);
  const [err, setErr] = useState("");

  async function handleResume(e) {
    e.preventDefault();
    setErr("");
    if (isV02 && (!rated1009.trim() || !derating.trim() || !tieMode.trim())) {
      setErr("请依据现场铭牌与倒闸日志完整填写载流量、降容系数及联络开关状态（缺项禁止默认提交）。");
      return;
    }
    if (!isV02 && (!rated303.trim() || !ratedTr1.trim() || !derating.trim() || !switchLog.trim())) {
      setErr("请依据现场铭牌与值班操作票完整填写全部缺证项（缺项禁止默认提交）。");
      return;
    }
    setResuming(true);
    try {
      const parameters = isV02
        ? {
            "1009_1010_rated_current_a": Number(rated1009),
            ambient_derating_factor: Number(derating),
            c1535_tie_switch_mode: tieMode.trim(),
          }
        : {
            "303_rated_current_a": Number(rated303),
            main_transformer_rated_kva: Number(ratedTr1),
            ambient_derating_factor: Number(derating),
            switch_log_20261113: switchLog.trim(),
          };
      const payload = await apiRequest("/api/graph/demo", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          action: "resume",
          threadId: hitl.thread_id,
          taskType: hitl.task_type || (isV02 ? "underground_n1_capacity_audit" : "single_incomer_overload_audit"),
          sessionId,
          source_doc: isV02 ? "对话内 HITL V02 下井 N-1 参数确认" : "对话内 HITL V01 单进线参数确认",
          parameters,
        }),
      });
      setResumedState(payload.state);
    } catch (error) {
      setErr(error.message);
    } finally {
      setResuming(false);
    }
  }

  // Exact contract from laowadi/graph.py & laowadi/supplement.py (Fixes Bug #1!)
  const supp = resumedState?.supplement_result;
  const cj = supp?.conditional_judgment;

  return <div className={"inline-hitl-card" + (resumedState ? " is-resumed" : "")}>
    <div className="inline-hitl-head">
      <div className="inline-hitl-title">
        {resumedState ? (
          <CheckCircle2 size={15} className="icon-green" />
        ) : (
          <PauseCircle size={15} className="icon-orange" />
        )}
        <strong>
          {resumedState
            ? `检查点已原地唤醒恢复 (${isV02 ? "V02" : "V01"}) · 已写回对话记忆`
            : `人机协同挂起（HITL ${isV02 ? "V02 下井 N-1" : "V01 单进线过载"}）· 缺额定参数`}
        </strong>
      </div>
      <span className="agent-mini-pill mono">{hitl.thread_id}</span>
    </div>

    {!resumedState ? (
      <form className="inline-hitl-form" onSubmit={handleResume}>
        <p className="inline-hitl-desc">
          智能体已固证 <strong>{hitl.confirmed_steps} 步</strong>本地 SQLite 证据（快照{" "}
          <code className="mono">{hitl.snapshot_sha256}</code>）并写入检查点。请原地补齐现场额定参数后唤醒继续（
          <strong>0 重复查库</strong>，结果自动写回后续对话记忆）：
        </p>

        {isV02 ? (
          <div className="inline-hitl-grid">
            <label className="inline-hitl-field">
              <span>1009/1010 电缆额定载流 (A)</span>
              <input type="number" step="10" value={rated1009} onChange={e => setRated1009(e.target.value)} required />
            </label>
            <label className="inline-hitl-field">
              <span>井筒敷设降容系数</span>
              <input type="number" step="0.05" min="0.5" max="1.2" value={derating} onChange={e => setDerating(e.target.value)} required />
            </label>
            <label className="inline-hitl-field is-span-2">
              <span>1535 中央所 1200 联络开关确认</span>
              <input type="text" value={tieMode} onChange={e => setTieMode(e.target.value)} required />
            </label>
          </div>
        ) : (
          <div className="inline-hitl-grid">
            <label className="inline-hitl-field">
              <span>303 额定电流 (A)</span>
              <input type="number" step="1" value={rated303} onChange={e => setRated303(e.target.value)} required />
            </label>
            <label className="inline-hitl-field">
              <span>1# 主变容量 (kVA)</span>
              <input type="number" step="100" value={ratedTr1} onChange={e => setRatedTr1(e.target.value)} required />
            </label>
            <label className="inline-hitl-field">
              <span>降容系数</span>
              <input type="number" step="0.05" min="0.5" max="1.2" value={derating} onChange={e => setDerating(e.target.value)} required />
            </label>
            <label className="inline-hitl-field">
              <span>11-13 开关确认</span>
              <input type="text" value={switchLog} onChange={e => setSwitchLog(e.target.value)} required />
            </label>
          </div>
        )}

        {err && <div className="save-err">{err}</div>}

        <div className="inline-hitl-actions">
          <button type="submit" className="inline-hitl-submit" disabled={resuming}>
            <Play size={12} fill="currentColor" />
            <span>{resuming ? "正在校验快照哈希并恢复…" : "确认补证并原地恢复 (0 重复查库)"}</span>
          </button>
          <button
            type="button"
            className="chat-artifact-btn"
            onClick={() => onSyncCanvas({canvas: "graph"})}
          >
            <GitBranch size={12} />
            <span>右侧状态图</span>
          </button>
        </div>
      </form>
    ) : (
      <div className="inline-hitl-result">
        <div className="inline-hitl-metrics">
          <span className="agent-mini-pill is-green">
            复用历史回执 {resumedState.reused_steps_in_this_run?.length ?? 0} 步
          </span>
          <span className="agent-mini-pill is-green">
            重复查库 {resumedState.executed_in_this_run?.length ?? 0} 次
          </span>
          <span className="agent-mini-pill is-blue">
            终态：{resumedState.terminal_status}
          </span>
        </div>

        {supp?.explanation && <p className="inline-hitl-explain">{supp.explanation}</p>}

        {cj && !isV02 && cj.current_load_ratio_pct !== undefined && (
          <div className="inline-hitl-gauges">
            <ProgressBar
              label={`303 线缆电流负载率（峰值 ${cj.peak_current_a} A / 有效限值 ${cj.effective_current_limit_a} A，裕度 ${cj.current_margin_pct > 0 ? "+" : ""}${cj.current_margin_pct}%）`}
              value={Math.min(100, cj.current_load_ratio_pct)}
              valueLabel={`${cj.current_load_ratio_pct}%`}
              variant={cj.current_load_ratio_pct >= 85 ? "error" : cj.current_load_ratio_pct >= 65 ? "warning" : "success"}
              size="sm"
            />
            <ProgressBar
              label={`1# 主变视在功率负载率（估算 ${cj.transformer_peak_kva} kVA / 额定 ${cj.transformer_rated_kva} kVA，裕度 ${cj.transformer_margin_pct > 0 ? "+" : ""}${cj.transformer_margin_pct}%）`}
              value={Math.min(100, cj.transformer_load_ratio_pct)}
              valueLabel={`${cj.transformer_load_ratio_pct}%`}
              variant={cj.transformer_load_ratio_pct >= 90 ? "error" : cj.transformer_load_ratio_pct >= 75 ? "warning" : "success"}
              size="sm"
            />
          </div>
        )}

        {cj && isV02 && cj.n_minus_1_load_ratio_pct !== undefined && (
          <div className="inline-hitl-gauges">
            <ProgressBar
              label={`下井双回 N-1 载流负载率（合成峰值 ${cj.n_minus_1_combined_peak_a} A / 有效限值 ${cj.effective_single_cable_limit_a} A，裕度 ${cj.n_minus_1_margin_pct > 0 ? "+" : ""}${cj.n_minus_1_margin_pct}%）`}
              value={Math.min(100, cj.n_minus_1_load_ratio_pct)}
              valueLabel={`${cj.n_minus_1_load_ratio_pct}%`}
              variant={cj.n_minus_1_load_ratio_pct >= 90 ? "error" : cj.n_minus_1_load_ratio_pct >= 75 ? "warning" : "success"}
              size="sm"
            />
          </div>
        )}
      </div>
    )}
  </div>;
}

function ToolTraceAccordion({result}) {
  const [open, setOpen] = useState(false);
  const trace = result.tool_trace || [];
  const ver = result.verification;
  const gssc = result.gssc_stats;
  const ctx = result.context_snapshot;
  const isVerified = ver?.status === "verified" && ver?.policy === "archive-evidence-v1";

  return <div className="agent-trace-card">
    <button
      type="button"
      className="agent-trace-toggle"
      onClick={() => setOpen(v => !v)}
      aria-expanded={open}
    >
      <span className="agent-trace-left">
        {isVerified ? (
          <ShieldCheck size={14} className="icon-green" />
        ) : (
          <ShieldAlert size={14} className="icon-orange" />
        )}
        <strong>
          {result.rounds > 1 ? `${result.rounds} 轮推理 · ` : ""}只读查询 {result.tool_calls ?? trace.length} 次 · {result.elapsed_seconds?.toFixed(2)}s
        </strong>
        {gssc?.saved_pct > 0 && (
          <span className="agent-mini-pill">GSSC -{gssc.saved_pct}%</span>
        )}
        {ver && (
          <span className={"agent-mini-pill " + (isVerified ? "is-green" : "is-orange")}>
            {ver.verified_cells?.length ?? 0}/{ver.cited_cells_total ?? 0} 单元格
            {ver.numeric_checks_total > 0 ? ` · ${ver.verified_values_count}/${ver.numeric_checks_total} 数值对齐` : ""}
          </span>
        )}
      </span>
      <span className="agent-trace-chevron">
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </span>
    </button>

    {ctx && (ctx.inherited_anchors?.days?.length > 0 || ctx.inherited_anchors?.cabinets?.length > 0 || ctx.topic_switched || ctx.prior_turns_used > 0) && (
      <div className="agent-memory-strip">
        {ctx.topic_switched && !ctx.prior_turns_used ? (
          <span>↻ 检测到话题切换：已自动隔离上一轮柜号/日期锚点</span>
        ) : (
          <span>
            🧠 多轮会话记忆（第 {ctx.turn} 轮{ctx.prior_turns_used > 0 ? ` · 注入前 ${ctx.prior_turns_used} 轮历史上下文` : ""}）
            {(ctx.inherited_anchors?.days?.length > 0 || ctx.inherited_anchors?.cabinets?.length > 0) && (
              <> · 继承 {[...(ctx.inherited_anchors?.days || []), ...(ctx.inherited_anchors?.cabinets || []).map(c => `${c}柜`)].join(" · ")}</>
            )}
          </span>
        )}
      </div>
    )}

    {open && (
      <div className="agent-trace-body">
        {trace.map((step, i) => (
          <div key={i} className="agent-tool-row">
            <div className="agent-tool-row-head">
              <Wrench size={12} className="apple-accent-icon" />
              <code className="mono">{step.tool}</code>
              <span className="muted">{TOOL_LABELS[step.tool] || "只读工具"}</span>
              {step.round > 1 && <span className="agent-mini-pill is-blue">R{step.round}</span>}
              <span className="agent-mini-pill is-green">已执行</span>
            </div>
            <pre className="agent-tool-args mono">
              {JSON.stringify(step.arguments || {}, null, 0)}
            </pre>
          </div>
        ))}
      </div>
    )}
  </div>;
}

const HYPOTHESIS_OUTCOMES = {
  refuted_at_sample: "该采样点证伪",
  insufficient_evidence: "证据不足",
  inconclusive: "待进一步查证",
};

function HypothesisTrace({report, onCompareAdjacent}) {
  const diagnosis = report.hypothesis_trace;
  if (!diagnosis) return null;

  function evidenceList(indices) {
    if (!indices.length) return null;
    return <ul className="chat-mini-list">{indices.map(index => {
      const fact = report.confirmed_facts[index];
      if (!fact) return null;
      return <li key={index}>
        {fact.statement}
        <div className="inline-cells">{fact.evidence.map(cell => {
          const href = cellHref(cell);
          return href ? <a key={cell} className="cell-chip" href={href} download title={`下载原始日报 · ${cell}`}>{cell}</a>
            : <span key={cell} className="cell-chip">{cell}</span>;
        })}</div>
      </li>;
    })}</ul>;
  }

  return <section className="chat-more-panel" aria-label="假设检验记录">
    <div className="chat-subblock">
      <strong>假设检验记录</strong>
      <small className="muted">{(diagnosis.observation_times || [diagnosis.time]).join(" / ")} · 日报归档整点</small>
      {diagnosis.observation_step && <details>
        <summary>初始观测 · 查询 #{diagnosis.observation_step} · {diagnosis.cabinets.join(" / ")} 柜</summary>
        {evidenceList(diagnosis.observation_fact_indices)}
      </details>}
    </div>
    <div className="chat-hyp-list">
      {diagnosis.checks.map(check => <div key={check.step} className="chat-hyp-item">
        <div className="chat-hyp-top">
          <strong>{check.hypothesis} {check.title}</strong>
          <Badge variant={check.outcome === "refuted_at_sample" ? "success" : "warning"}
            label={HYPOTHESIS_OUTCOMES[check.outcome]} />
        </div>
        <small className="muted">第 {check.round} 轮 · {check.planner === "model" ? "模型规划" : "本地规划"} · {check.criterion}</small>
        <div>{check.judgment}</div>
        <details>
          <summary>查询 #{check.step} · {TOOL_LABELS[check.tool]} · {check.fact_indices.length} 条结果</summary>
          <div className="muted">参考查询 {check.based_on_steps.map(step => `#${step}`).join("、")}</div>
          <div className="muted mono" style={{overflowWrap: "anywhere"}}>{check.tool} {JSON.stringify(check.arguments)}</div>
          {evidenceList(check.fact_indices)}
        </details>
      </div>)}
      {diagnosis.pending.map(item => <div key={item.hypothesis} className="chat-hyp-item">
        <div className="chat-hyp-top">
          <strong>{item.hypothesis} {item.title}</strong>
          <Badge variant="warning" label="待执行" />
        </div>
        <small className="muted">{item.criterion}</small>
      </div>)}
    </div>
    <small className="muted">{diagnosis.stop_message}</small>
    {diagnosis.stop_reason === "checks_complete" && diagnosis.signature_present === true && onCompareAdjacent && (
      <div>
        <button type="button" className="chat-artifact-btn" onClick={onCompareAdjacent}>
          追问相邻整点对照
        </button>
      </div>
    )}
  </section>;
}

function AgentTurnMessage({turn, sessionId, health, onSyncCanvas, onOpenCabinet, onReviewsUpdate, onQuickFollowUp, canCompareAdjacent, onOpenMemory}) {
  const [showMore, setShowMore] = useState(false);
  const [memOpen, setMemOpen] = useState(false);
  const [memSaved, setMemSaved] = useState(false);
  const [memSaving, setMemSaving] = useState(false);
  const [memErr, setMemErr] = useState("");
  const [memDisp, setMemDisp] = useState("normal_operation");
  const [memNote, setMemNote] = useState("");

  const res = turn.result;
  const legacyReport = res?.verification?.status === "verified" && res.verification.policy !== "archive-evidence-v1";
  const rep = legacyReport ? null : res?.report;
  const taskStatus = TASK_STATUS_LABELS[res?.task_alignment?.status];
  const claims = res?.verification?.claim_policy === "diagnostic-claims-v1" &&
    res.verification.status === "verified" ? rep?.diagnostic_claims : null;
  const facts = (rep?.confirmed_facts || []).slice(0, taskStatus || claims ? undefined : 6);

  const targetFindingId = useMemo(() => {
    const findings = health?.findings || [];
    const cabs = res?.context_snapshot?.active_anchors?.cabinets || [];
    if (cabs.length > 0) {
      const hit = findings.find(f => cabs.some(c => String(f.cabinet || "").includes(c)));
      if (hit) return hit.id;
    }
    const q = turn.question || "";
    if (q.includes("1018")) return findings.find(f => String(f.cabinet).includes("1018"))?.id || "AGENT-MEM-1018";
    if (q.includes("306") || q.includes("303")) return findings.find(f => String(f.cabinet).includes("306"))?.id || "AGENT-MEM-306";
    return `AGENT-MEM-${String(turn.id).slice(-5)}`;
  }, [health, res, turn.id, turn.question]);

  async function saveMemory(e) {
    e.preventDefault();
    setMemSaving(true);
    setMemErr("");
    try {
      const noteText = (memNote || rep?.summary || "").trim().slice(0, 220);
      const data = await apiRequest("/api/reviews", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          finding_id: targetFindingId,
          disposition: memDisp,
          switch_state: `会话锚点：${(res?.context_snapshot?.active_anchors?.cabinets || []).join("/") || "全站"}`,
          operator_note: noteText,
          operator_name: "值班员（对话沉淀）",
        }),
      });
      setMemSaved(true);
      setMemOpen(false);
      if (onReviewsUpdate && data.reviews) {
        onReviewsUpdate(data.reviews);
      }
    } catch (error) {
      setMemErr(error.message);
    } finally {
      setMemSaving(false);
    }
  }

  return <div className="chat-turn">
    <div className="chat-user-row">
      <div className="chat-user-bubble">{turn.question}</div>
    </div>

    <div className="chat-agent-row">
      <div className="chat-agent-avatar" aria-hidden="true">
        <Sparkles size={14} />
      </div>
      <div className="chat-agent-body">
        {turn.error ? (
          <div className="chat-error-box">
            <strong>执行异常：</strong>
            <span>{turn.error}</span>
          </div>
        ) : legacyReport ? (
          <div className="chat-error-box">
            <strong>历史诊断使用旧核验规则，请重新发起诊断。</strong>
          </div>
        ) : !rep ? (
          <div className="chat-error-box">
            <strong>未能生成结构化诊断单（{res?.stop_reason || res?.status}）</strong>
            <p className="muted">{res?.error || "可切换本地推理引擎立即复核。"}</p>
          </div>
        ) : <>
          {taskStatus && (
            <div className="chat-task-status">
              <span className={"agent-mini-pill " + (res.task_alignment.status === "completed" ? "is-green" : "is-orange")}>
                {taskStatus}
              </span>
              {res.verification?.status === "verified" && <span>现有证据已核验</span>}
            </div>
          )}
          <p className="chat-summary">{rep.summary}</p>
          <HypothesisTrace report={rep}
            onCompareAdjacent={canCompareAdjacent && res.verification?.status === "verified"
              ? () => onQuickFollowUp("那相邻整点呢？")
              : undefined} />
          {claims && Object.entries({
            hypothesis: "候选原因 · 待现场复核", cross_check: "交叉检验", standard: "规程对标",
            gap: "待补证据", followup: "排查建议",
          }).map(([stage, label]) => {
            const items = claims.filter(claim => claim.stage === stage);
            return items.length > 0 && <div className="chat-subblock" key={stage}>
              <strong>{label}</strong>
              <ul className="chat-mini-list">
                {items.map(claim => <li key={claim.index}>
                  {claim.statement}
                  {claim.citations.length > 0 &&
                    <small className="muted"> · {claim.citations.join("、")}</small>}
                </li>)}
              </ul>
            </div>;
          })}
          {rep.operator_note && (
            <p className="muted">人工复核备注（待核对原件） · {rep.operator_note}</p>
          )}

          {facts.length > 0 && (
            <details className="chat-section duty-evidence">
              <summary><FileSpreadsheet size={15} strokeWidth={1.7} /> 查看证据 <span>{facts.length} 条</span><ChevronDown size={14} /></summary>
              <ol className="chat-facts">
                {facts.map((fact, idx) => {
                  const evs = Array.isArray(fact.evidence) ? fact.evidence : (fact.evidence ? [fact.evidence] : []);
                  return <li key={idx}>
                    <span>{fact.citation_id && <code>{fact.citation_id} · </code>}{fact.statement}</span>
                    {fact.source?.tool && (
                      <div className="muted">
                        {{calculation: "计算", simulation: "条件测算", screening: "归档筛查", coverage: "资料边界", daily_footer: "日报页尾"}[fact.source.kind]}
                        {" · "}{fact.source.tool}{" · "}{fact.source.result_sha256.slice(0, 12)}
                      </div>
                    )}
                    {evs.length > 0 && (
                      <div className="inline-cells">
                        {evs.slice(0, 4).map((ev, j) => {
                          const href = cellHref(ev);
                          return href ? (
                            <a key={j} className="cell-chip" href={href} download>
                              <FileSpreadsheet size={10} style={{marginRight: 3}} />
                              {ev}
                            </a>
                          ) : (
                            <span key={j} className="cell-chip">{ev}</span>
                          );
                        })}
                      </div>
                    )}
                  </li>;
                })}
              </ol>
            </details>
          )}

          {/* Inline HITL Checkpoint Suspend & Resume Card (supports both V01 & V02!) */}
          {res.hitl_interrupt?.required && (
            <InlineHitlCard hitl={res.hitl_interrupt} sessionId={sessionId} onSyncCanvas={onSyncCanvas} />
          )}

          {Boolean(rep.cited_standards?.length || (!claims &&
            (rep.hypotheses?.length || rep.missing_evidence?.length || rep.field_sop?.length))) && <button
            type="button"
            className="chat-expand-btn"
            aria-expanded={showMore}
            onClick={() => setShowMore(v => !v)}
          >
            {showMore ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            <span>
              {showMore ? "收起待核事项" : "查看待核事项"}
            </span>
          </button>}

          {showMore && (
            <div className="chat-more-panel">
              {!claims && rep.hypotheses?.length > 0 && (
                <div className="chat-subblock">
                  <strong>待复核的候选原因</strong>
                  <div className="chat-hyp-list">
                    {rep.hypotheses.map((h, i) => (
                      <div key={i} className="chat-hyp-item">
                        <div className="chat-hyp-top">
                          <span>#{h.rank ?? i + 1} {h.title}</span>
                          <Badge
                            variant="warning"
                            label="待现场复核"
                          />
                        </div>
                        <small className="muted">边界：{h.caveats}</small>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {!claims && rep.missing_evidence?.length > 0 && (
                <div className="chat-subblock">
                  <strong>缺失现场证据（严格不越界猜测）</strong>
                  <ul className="chat-mini-list">
                    {rep.missing_evidence.map((m, i) => <li key={i}>{m}</li>)}
                  </ul>
                </div>
              )}

              {!claims && rep.field_sop?.length > 0 && (
                <div className="chat-subblock">
                  <strong>现场核查与补证</strong>
                  <ol className="chat-mini-list">
                    {rep.field_sop.map((s, i) => <li key={i}>{s}</li>)}
                  </ol>
                </div>
              )}

              {rep.cited_standards?.length > 0 && (
                <div className="chat-subblock">
                  <strong>参考条款（适用性待复核）</strong>
                  <ul className="chat-mini-list">
                    {rep.cited_standards.map((st, i) => (
                      <li key={i}>
                        <code className="mono">{st.id}</code> · {st.relevance} · {st.page || "页码待核"}
                        {st.applicability && <p className="muted">资料所述范围 · {st.applicability}</p>}
                        {st.source_status === "full_text_pending" && <small>原文待复核</small>}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}

          {rep.operator_memory_applied?.length > 0 && (
            <div className="agent-memory-strip is-blue">
              ✓ 已复用 {rep.operator_memory_applied.length} 条值班员人工复核记忆（Episodic Memory）
            </div>
          )}

          {memSaved && (
            <div className="agent-memory-strip is-green">
              <BookmarkCheck size={12} style={{marginRight: 4}} />
              已写入本矿长期记忆（<code className="mono">{targetFindingId}</code>），主接线图告警与主动巡检已联动降噪。
            </div>
          )}

          {memOpen && !memSaved && (
            <form className="inline-memory-drawer" onSubmit={saveMemory}>
              <div className="inline-memory-row">
                <span className="muted">对象 <code className="mono">{targetFindingId}</code></span>
                <select value={memDisp} onChange={e => setMemDisp(e.target.value)}>
                  {DISPOSITION_OPTIONS.map(opt => (
                    <option key={opt.value} value={opt.value}>{opt.label}</option>
                  ))}
                </select>
              </div>
              <input
                type="text"
                value={memNote}
                onChange={e => setMemNote(e.target.value)}
                placeholder="补充现场确认备注（留空默认记入本次诊断核心摘要）"
              />
              {memErr && <div className="save-err">{memErr}</div>}
              <div className="inline-memory-actions">
                <button type="submit" className="inline-hitl-submit" disabled={memSaving}>
                  <BookmarkCheck size={12} />
                  <span>{memSaving ? "写入中…" : "确认写入本矿记忆"}</span>
                </button>
                <button type="button" className="chat-artifact-btn" onClick={() => setMemOpen(false)}>取消</button>
              </div>
            </form>
          )}

          <div className="chat-artifact-actions">
            {res.ui_artifact_hint?.focus_cabinet && (
              <button
                type="button"
                className="chat-artifact-btn"
                onClick={() => onOpenCabinet(res.ui_artifact_hint.focus_cabinet)}
              >
                <Activity size={12} />
                <span>{res.ui_artifact_hint.focus_cabinet} 柜曲线</span>
              </button>
            )}
            {res.ui_artifact_hint?.canvas === "powerflow" && (
              <button
                type="button"
                className="chat-artifact-btn"
                onClick={() => onSyncCanvas({canvas: "powerflow"})}
              >
                <Cpu size={12} />
                <span>交流潮流画布</span>
              </button>
            )}
            <button
              type="button"
              className="chat-artifact-btn"
              onClick={() => onSyncCanvas({
                canvas: "topology",
                focus_cabinet: res.ui_artifact_hint?.focus_cabinet || null,
                replay_index: res.ui_artifact_hint?.replay_index ?? null,
              })}
            >
              <Layers size={12} />
              <span>主接线定位</span>
            </button>
            <SaveReplyMemory question={turn.question} content={rep.summary || res.markdown}
              cabinet={res.context_snapshot?.active_anchors?.cabinets?.[0]} onOpenMemory={onOpenMemory} />
            <details className="duty-reply-more">
              <summary className="chat-artifact-btn">更多 <ChevronDown size={13} /></summary>
              <div className="duty-reply-menu">
                {!memSaved && (
                  <button
                    type="button"
                    className="chat-artifact-btn is-memory"
                    onClick={() => {
                      setMemNote(rep.summary?.slice(0, 120) || "");
                      setMemOpen(v => !v);
                    }}
                  >
                    <BookmarkPlus size={12} />
                    <span>添加复核备注</span>
                  </button>
                )}
                {res.markdown && (
                  <button
                    type="button"
                    className="chat-artifact-btn"
                    onClick={() => downloadMarkdown(`laowadi_diagnosis_${turn.id}.md`, res.markdown)}
                    title="导出带原表单元格出处的 Markdown 诊断单"
                  >
                    <Download size={12} />
                    <span>导出诊断记录</span>
                  </button>
                )}
                <button
                  type="button"
                  className="chat-artifact-btn"
                  onClick={() => onQuickFollowUp("请把刚才的分析精简成 3 句话值班交接简报。")}
                >
                  <Sparkles size={12} />
                  <span>3 句话简报</span>
                </button>
              </div>
            </details>
          </div>
          <ToolTraceAccordion result={res} />
        </>}
      </div>
    </div>
  </div>;
}

export function AgentCopilotPane({
  health,
  model,
  pendingQuestion,
  clearPendingQuestion,
  onSyncCanvas,
  onOpenCabinet,
  onReviewsUpdate,
  onOpenMemory,
  onDraftAction,
  onDeepResearch,
  sidebarOpen,
  chatVisible,
  onShowChat,
  archiveRange,
  connectionError,
  onRetry,
  navigation,
}) {
  const [sessionId, setSessionId] = useState(() => "sess-" + Date.now().toString(36));
  const [agentKind, setAgentKind] = useState("audit");
  const [auditMode, setAuditMode] = useState("local");
  const [autonomousMode, setAutonomousMode] = useState("auto");
  const autonomous = agentKind === "autonomous";
  const mode = autonomous ? autonomousMode : auditMode;
  const [input, setInput] = useState("");
  const [skillMenu, setSkillMenu] = useState(false);
  const [skills, setSkills] = useState([]);
  const [skillsLoading, setSkillsLoading] = useState(false);
  const [skillsError, setSkillsError] = useState("");
  const [skillCommandError, setSkillCommandError] = useState("");
  const slash = input.trimStart().match(/^\/([^\s]*)/);
  const matchingSkills = skills.filter(skill => (skill.id + " " + (skill.title ?? ""))
    .toLowerCase().includes((slash?.[1] ?? "").toLowerCase()));
  const [turns, setTurns] = useState([]);
  const [loading, setLoading] = useState(false);
  const [pendingPrompt, setPendingPrompt] = useState("");
  const [liveTrace, setLiveTrace] = useState(null);
  const [elapsed, setElapsed] = useState(0);
  const [activeAnchors, setActiveAnchors] = useState(null);

  // Persistent Session History & Episodic Memory Manager panels
  const [sessionsList, setSessionsList] = useState([]);
  const [showMemory, setShowMemory] = useState(false);

  const abortRef = useRef(null);
  const scrollRef = useRef(null);
  const inputRef = useRef(null);
  const historyRequest = useRef(0);
  const sessionListRequest = useRef(0);

  useEffect(() => () => abortRef.current?.abort(), []);

  const refreshSessions = useCallback(async signal => {
    const request = ++sessionListRequest.current;
    try {
      const data = await apiRequest("/api/sessions", {signal});
      if (!signal?.aborted && request === sessionListRequest.current) setSessionsList(data.sessions || []);
    } catch {}
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    refreshSessions(ctrl.signal);
    return () => ctrl.abort();
  }, [refreshSessions]);

  useEffect(() => {
    if (!loading) {
      setElapsed(0);
      return undefined;
    }
    const t0 = Date.now();
    const id = window.setInterval(() => setElapsed(Math.floor((Date.now() - t0) / 1000)), 500);
    return () => window.clearInterval(id);
  }, [loading]);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [turns, loading, liveTrace]);

  const followTypedAnswer = useCallback(() => {
    const thread = scrollRef.current;
    if (thread && thread.scrollHeight - thread.clientHeight - thread.scrollTop < 48) {
      thread.scrollTop = thread.scrollHeight;
    }
  }, []);

  async function loadHistorySession(sid) {
    const request = ++historyRequest.current;
    try {
      const detail = await apiRequest("/api/sessions/" + encodeURIComponent(sid));
      if (request !== historyRequest.current) return;
      setSessionId(detail.session_id);
      setInput("");
      setShowMemory(false);
      setSkillMenu(false);
      setSkillCommandError("");
      onShowChat();
      const history = (detail.turns || []).map(restoreAutonomousTurn);
      setTurns(history);
      const latest = history.at(-1);
      if (latest) {
        setAgentKind(latest.stream ? "autonomous" : "audit");
        if (latest.stream && ["local", "deepseek", "auto"].includes(latest.mode)) {
          setAutonomousMode(latest.mode === "deepseek" && !model ? "auto" : latest.mode);
        }
      }
      setActiveAnchors(detail.active_anchors && {
        turn: detail.turns_digest?.length ?? history.length,
        active_anchors: detail.active_anchors,
      });
    } catch {}
  }

  async function removeHistorySession(sid) {
    try {
      const data = await apiRequest("/api/sessions/delete", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({sessionId: sid}),
      });
      sessionListRequest.current += 1;
      setSessionsList(data.sessions || []);
      if (sid === sessionId) {
        setSessionId("sess-" + Date.now().toString(36));
        setTurns([]);
        setActiveAnchors(null);
      }
    } catch {}
  }

  async function removeMemoryItem(findingId) {
    try {
      const data = await apiRequest("/api/reviews/delete", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({finding_id: findingId}),
      });
      if (onReviewsUpdate && data.reviews) {
        onReviewsUpdate(data.reviews);
      }
    } catch {}
  }

  async function consumeResumableStream({initialResponse, jobId: initJobId, startSeq = 0, clean, turnId, ctrl, taskSessionId}) {
    const STORAGE_KEY = "laowadi_active_sse_job";
    let activeJobId = initJobId || initialResponse?.headers?.get("x-job-id") || "";
    let lastSeq = startSeq || 0;
    let finalPayload = null;
    let streamError = "";
    let currentResp = initialResponse || null;

    const persistCursor = () => {
      if (!activeJobId) return;
      try {
        sessionStorage.setItem(STORAGE_KEY, JSON.stringify({jobId: activeJobId, lastSeq, question: clean, turnId, sessionId: taskSessionId}));
      } catch {}
    };

    const handleEvent = evt => {
      if (evt.job_id && !activeJobId) activeJobId = evt.job_id;
      if (typeof evt.seq === "number" && evt.seq > lastSeq) {
        lastSeq = evt.seq;
        persistCursor();
      }
      if (evt.type === "job_queued" || evt.type === "job_claimed" || evt.type === "job_reclaimed" || evt.type === "job_requeued") {
        setLiveTrace(prev => ({
          ...(prev || {}),
          phase: evt.message || `任务 ${activeJobId} 正在由外部 Worker 执行…`,
          jobId: activeJobId,
          workerPid: evt.worker_pid || prev?.workerPid || null,
        }));
      } else if (evt.type === "circuit_breaker_degraded") {
        setLiveTrace(prev => ({
          ...(prev || {}),
          phase: evt.message || "跨进程熔断触发，已自动降级为本地基线…",
          reflections: [...((prev && prev.reflections) || []), evt.message || "跨进程熔断自动降级"],
        }));
      } else if (evt.type === "plan") {
        setLiveTrace(prev => ({
          ...(prev || {}),
          phase: evt.message || "规划只读诊断任务…",
          mode: evt.mode,
          priorTurnsUsed: evt.prior_turns_used || 0,
        }));
      } else if (evt.type === "llm_round") {
        setLiveTrace(prev => ({
          ...(prev || {}),
          phase: evt.message || `第 ${evt.round} 轮推理中…`,
          streamChars: 0,
        }));
      } else if (evt.type === "claim_audit") {
        setLiveTrace(prev => ({...(prev || {}), phase: evt.message, streamChars: 0}));
      } else if (evt.type === "llm_delta") {
        setLiveTrace(prev => ({
          ...(prev || {}),
          streamKind: evt.kind || "content",
          streamChars: (prev?.streamChars || 0) + (evt.chars || 0),
        }));
      } else if (evt.type === "hypothesis_check") {
        setLiveTrace(prev => ({
          ...(prev || {}),
          phase: `第 ${evt.round} 轮查证 ${evt.hypothesis} ${evt.title}`,
          hypothesis: evt,
        }));
      } else if (evt.type === "tool_start") {
        setLiveTrace(prev => {
          const cur = prev || {steps: [], reflections: []};
          if ((cur.steps || []).some(s => s.step === evt.step)) return cur;
          const nextSteps = [...(cur.steps || []), {
            step: evt.step,
            round: evt.round || 1,
            tool: evt.tool,
            arguments: evt.arguments,
            hypothesis: cur.hypothesis?.step === evt.step ? cur.hypothesis : undefined,
            status: "running",
            summary: "正在只读检索归档…",
          }];
          return {...cur, phase: `正在执行：${TOOL_LABELS[evt.tool] || evt.tool}`, steps: nextSteps};
        });
      } else if (evt.type === "tool_end") {
        setLiveTrace(prev => {
          const cur = prev || {steps: [], reflections: []};
          const nextSteps = (cur.steps || []).map(st => (
            st.step === evt.step
              ? {...st, status: evt.status || "ok", summary: evt.summary || "已完成", gssc_saved_pct: evt.gssc_saved_pct || 0}
              : st
          ));
          return {...cur, steps: nextSteps};
        });
      } else if (evt.type === "reflection") {
        setLiveTrace(prev => ({
          ...(prev || {}),
          phase: "触发证据与数值审计员 Reflection 自修正…",
          reflections: [...((prev && prev.reflections) || []), evt.reason || "检测到偏差，正在修正…"],
        }));
      } else if (evt.type === "verify") {
        setLiveTrace(prev => ({
          ...(prev || {}),
          phase:
            evt.status === "verified"
              ? "双层证据与数值核验通过，生成诊断单…"
              : evt.status === "insufficient_data"
                ? "归档资料不足，整理待补资料…"
                : evt.status === "not_applicable"
                  ? "需要确认查询范围…"
                  : "核验告警：发现未核实引用或数值偏差，已标记人工复核…",
          verification: evt,
        }));
      } else if (evt.type === "error") {
        streamError = evt.error || "流式执行失败";
      } else if (evt.type === "done") {
        finalPayload = evt.result;
      }
    };

    const readOneStream = async resp => {
      if (!resp.body) throw new Error("当前浏览器不支持流式响应。");
      const reader = resp.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream: true});
        const frames = buffer.split("\n\n");
        buffer = frames.pop() || "";
        for (const frame of frames) {
          const lines = frame.split("\n");
          for (const ln of lines) {
            if (ln.startsWith("id:")) {
              const idNum = Number(ln.slice(3).trim());
              if (!Number.isNaN(idNum) && idNum > lastSeq) {
                lastSeq = idNum;
                persistCursor();
              }
            }
          }
          const dataLines = lines.filter(line => line.startsWith("data:")).map(line => line.slice(5).trim());
          if (dataLines.length === 0) continue;
          try {
            handleEvent(JSON.parse(dataLines.join("\n")));
          } catch {}
        }
      }
    };

    const maxReconnects = 12;
    for (let attempt = 0; attempt <= maxReconnects; attempt++) {
      if (ctrl.signal.aborted) break;
      try {
        if (!currentResp) {
          if (!activeJobId) break;
          const url = `/api/jobs/${encodeURIComponent(activeJobId)}/stream?after_seq=${lastSeq}`;
          currentResp = await fetch(url, {
            method: "GET",
            headers: authHeaders(lastSeq > 0 ? {"Last-Event-ID": String(lastSeq)} : {}),
            signal: ctrl.signal,
          });
          if (!currentResp.ok) {
            throw new Error(`续传连接失败 (${currentResp.status})`);
          }
        }
        persistCursor();
        await readOneStream(currentResp);
        currentResp = null;
        if (finalPayload || streamError) break;
      } catch (netErr) {
        currentResp = null;
        if (ctrl.signal.aborted || !activeJobId || attempt >= maxReconnects) {
          if (!finalPayload && !streamError) throw netErr;
          break;
        }
      }
      // Stream dropped before terminal done/error -> seamlessly reconnect from Last-Event-ID!
      if (!finalPayload && !streamError && activeJobId && attempt < maxReconnects) {
        const delayMs = Math.min(2000, 500 + attempt * 250);
        setLiveTrace(prev => ({
          ...(prev || {}),
          phase: `服务端重启或网络波动 · 正在从事件 #${lastSeq} 自动断点续传（第 ${attempt + 1}/${maxReconnects} 次）…`,
        }));
        await new Promise(resolve => setTimeout(resolve, delayMs));
      }
    }

    try {
      sessionStorage.removeItem(STORAGE_KEY);
    } catch {}
    return {finalPayload, streamError};
  }

  async function sendAutonomousQuestion(clean, turnId, ctrl, targetMode) {
    setTurns(prev => [...prev, {id: turnId, question: clean, stream: emptyAgentStream}]);
    const updateTurn = update => setTurns(prev => prev.map(turn => turn.id === turnId ? update(turn) : turn));
    try {
      const response = await fetch("/api/agent/stream", {
        method: "POST",
        headers: authHeaders({"Content-Type": "application/json", Accept: "text/event-stream"}),
        body: JSON.stringify({question: clean, mode: targetMode, sessionId}),
        signal: ctrl.signal,
      });
      await consumeAgentStream(response, event => {
        if (!ctrl.signal.aborted) {
          updateTurn(turn => ({...turn, stream: applyAgentEvent(turn.stream, event)}));
        }
      });
      if (!ctrl.signal.aborted) refreshSessions();
    } catch (error) {
      if (!ctrl.signal.aborted) {
        updateTurn(turn => ({...turn, error: error.message, stream: {...turn.stream, status: "error"}}));
      }
    } finally {
      if (!ctrl.signal.aborted) setLoading(false);
    }
  }

  useEffect(() => {
    if (!skillMenu) return undefined;
    const ctrl = new AbortController();
    setSkillsError("");
    setSkillsLoading(true);
    apiRequest("/api/skills", {signal: ctrl.signal})
      .then(data => { if (!ctrl.signal.aborted) setSkills(data.skills); })
      .catch(error => { if (!ctrl.signal.aborted) setSkillsError(error.message); })
      .finally(() => { if (!ctrl.signal.aborted) setSkillsLoading(false); });
    return () => ctrl.abort();
  }, [skillMenu]);

  async function runSkill(command, id, args) {
    const target = args.match(/^(\d{1,16})(?:\s+(\d{4}-\d{2}-\d{2}[ T]\d{2}:00(?::00)?))?$/);
    if (args && !target) {
      setSkillCommandError("请输入 /技能ID，或附上柜号和整点，例如 /tap_change_precheck 1006 2026-11-13 17:00。");
      return;
    }
    setSkillCommandError("");
    historyRequest.current += 1;
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const turnId = Date.now();
    const updateTurn = update => setTurns(previous => previous.map(turn => turn.id === turnId ? {...turn, ...update} : turn));
    setInput("");
    setSkillMenu(false);
    setShowMemory(false);
    setLoading(true);
    setTurns(previous => [...previous, {id: turnId, question: command, skillRun: true, running: true}]);
    try {
      const result = await apiRequest("/api/skills/" + encodeURIComponent(id) + "/run", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({target_cabinet: target?.[1], at_time: target?.[2]}),
        signal: ctrl.signal,
      });
      if (!ctrl.signal.aborted) updateTurn({result, running: false});
    } catch (error) {
      if (!ctrl.signal.aborted) updateTurn({error: error.message, running: false});
    } finally {
      if (!ctrl.signal.aborted) setLoading(false);
    }
  }

  async function sendQuestion(text, targetMode = mode) {
    const clean = (text || "").trim();
    if (!clean || loading) return;
    const command = clean.match(/^\/([^\s]*)(?:\s+([\s\S]*))?$/);
    if (command) {
      if (command[1]) await runSkill(clean, command[1], command[2] ?? "");
      else setSkillMenu(true);
      return;
    }
    setSkillMenu(false);
    setSkillCommandError("");
    historyRequest.current += 1;
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setLoading(true);
    setInput("");
    setShowMemory(false);
    const turnId = Date.now();
    if (agentKind === "autonomous") {
      await sendAutonomousQuestion(clean, turnId, ctrl, targetMode);
      return;
    }
    setPendingPrompt(clean);
    setLiveTrace({
      phase: targetMode === "deepseek" ? "正在连接 DeepSeek…" : "正在查阅归档…",
      mode: targetMode === "deepseek" ? "model" : "local",
      priorTurnsUsed: 0,
      steps: [],
      reflections: [],
      verification: null,
      streamKind: "",
      streamChars: 0,
    });
    try {
      const response = await fetch("/api/diagnose/stream", {
        method: "POST",
        headers: authHeaders({"Content-Type": "application/json"}),
        body: JSON.stringify({question: clean, mode: targetMode, sessionId}),
        signal: ctrl.signal,
      });

      if (!response.ok) {
        let msg = `请求失败 (${response.status})`;
        try {
          const errJson = await response.json();
          if (errJson?.error) msg = errJson.error;
        } catch {}
        throw new Error(msg);
      }

      const {finalPayload, streamError} = await consumeResumableStream({
        initialResponse: response,
        clean,
        turnId,
        ctrl,
        taskSessionId: sessionId,
      });

      if (finalPayload) {
        setTurns(prev => [...prev, {id: turnId, question: clean, result: finalPayload, error: null}]);
        if (finalPayload.context_snapshot) {
          setActiveAnchors(finalPayload.context_snapshot);
        }
        refreshSessions();
      } else {
        throw new Error(streamError || "智能体流式连接已提前结束，未收到诊断结果。");
      }
    } catch (err) {
      if (err.name !== "AbortError") {
        setTurns(prev => [...prev, {id: turnId, question: clean, result: null, error: err.message}]);
      }
    } finally {
      if (!ctrl.signal.aborted) {
        setLoading(false);
        setPendingPrompt("");
        setLiveTrace(null);
      }
    }
  }

  // Auto-resume any unfinished durable SSE job after accidental page refresh
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem("laowadi_active_sse_job");
      if (!raw) return;
      const saved = JSON.parse(raw);
      if (!saved?.jobId) return;
      if (saved.sessionId) setSessionId(saved.sessionId);
      setPendingPrompt(saved.question || "恢复中的问答");
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      setLoading(true);
      setLiveTrace({
        phase: `检测到未完成任务 ${saved.jobId}，正在从事件 #${saved.lastSeq || 0} 自动断点续传…`,
        mode: "local",
        steps: [],
        reflections: [],
      });
      consumeResumableStream({
        jobId: saved.jobId,
        startSeq: saved.lastSeq || 0,
        clean: saved.question || "断点续传任务",
        turnId: saved.turnId || Date.now(),
        ctrl,
        taskSessionId: saved.sessionId,
      })
        .then(({finalPayload}) => {
          if (finalPayload) {
            setTurns(prev => [...prev, {id: saved.turnId || Date.now(), question: saved.question || "断点续传任务", result: finalPayload, error: null}]);
            refreshSessions();
          }
        })
        .catch(() => {})
        .finally(() => {
          if (!ctrl.signal.aborted) {
            setLoading(false);
            setPendingPrompt("");
            setLiveTrace(null);
          }
        });
    } catch {}
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (pendingQuestion) {
      const q = pendingQuestion;
      clearPendingQuestion();
      sendQuestion(q, mode);
    }
  }, [pendingQuestion]);

  function stopWaiting() {
    abortRef.current?.abort();
    abortRef.current = null;
    setLoading(false);
    setPendingPrompt("");
    setLiveTrace(null);
    setTurns(prev => prev.map(turn => turn.skillRun && turn.running ? {...turn, running: false, stopped: true} : turn));
    if (agentKind === "autonomous") {
      setTurns(prev => prev.map(turn => turn.stream?.status === "running"
        ? {...turn, stream: {...turn.stream, status: "stopped"}}
        : turn));
    }
    try {
      sessionStorage.removeItem("laowadi_active_sse_job");
    } catch {}
  }

  function resetSession() {
    stopWaiting();
    historyRequest.current += 1;
    setShowMemory(false);
    setSkillMenu(false);
    setSkillCommandError("");
    setSessionId("sess-" + Date.now().toString(36));
    setInput("");
    setTurns([]);
    setActiveAnchors(null);
    onShowChat();
    requestAnimationFrame(() => inputRef.current?.focus());
  }

  const allReviewsList = useMemo(
    () => Object.values(health?.reviews || {}).filter(r => r && r.disposition && r.disposition !== "pending"),
    [health],
  );

  const anchorChips = [
    ...(activeAnchors?.active_anchors?.days || []),
    ...(activeAnchors?.active_anchors?.cabinets || []).map(c => `${c}柜`),
  ];

  const empty = turns.length === 0 && !loading;

  function usePrompt(text) {
    setInput(text);
    setSkillMenu(false);
    setSkillCommandError("");
    inputRef.current?.focus();
  }

  const composer = <div className="duty-composer-wrap">
    {agentKind === "audit" && anchorChips.length > 0 && <p className="duty-context">当前上下文 <span>{anchorChips.join(" · ")}</span></p>}
    <form className="copilot-composer duty-composer" onSubmit={e => {
      e.preventDefault();
      sendQuestion(input);
    }}>
      <textarea
        ref={inputRef}
        rows={2}
        maxLength={600}
        value={input}
        onChange={e => {
          const next = e.target.value;
          setInput(next);
          setSkillCommandError("");
          setSkillMenu(/^\/[^\s]*$/.test(next.trimStart()));
        }}
        onKeyDown={e => {
          if (e.key === "Escape" && skillMenu) {
            e.stopPropagation();
            setSkillMenu(false);
          }
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            sendQuestion(input);
          }
        }}
        aria-label="值班问题"
        placeholder="输入柜号、时间或你想核对的运行问题"
      />
      {skillMenu && <div className="duty-skill-picker" id="duty-skill-picker" aria-label="已安装技能">
        {skillsLoading && <small role="status">正在读取技能…</small>}
        {skillsError && <small className="chat-error-box" role="alert">{skillsError}</small>}
        {!skillsError && !skillsLoading && !matchingSkills.length && <small>没有匹配的技能</small>}
        {!skillsError && !skillsLoading && matchingSkills.map(skill => <button type="button" key={skill.id} onClick={() => usePrompt("/" + skill.id + " ")}>
          <strong>{skill.title ?? skill.id}</strong><code>/{skill.id}</code>
        </button>)}
      </div>}
      {slash && <small className="muted">按 Enter 执行，可在技能 ID 后填柜号和整点。</small>}
      {skillCommandError && <small className="chat-error-box" role="alert">{skillCommandError}</small>}
      <div className="copilot-composer-foot">
        <div className="duty-composer-tools">
          <details className="duty-cabinet-picker">
            <summary className="duty-composer-option"><Plus size={15} /> 柜号</summary>
            <div className="duty-cabinet-menu">
              {MENTIONS.map(m => (
                <button
                  key={m}
                  type="button"
                  className="copilot-mention-pill"
                  onClick={e => {
                    const cabinet = m.slice(1);
                    usePrompt(input ? `${input.trimEnd()} ${cabinet}柜 ` : `${cabinet}柜 `);
                    e.currentTarget.closest("details").open = false;
                  }}
                >{m.slice(1)} 柜</button>
              ))}
            </div>
          </details>
          <button type="button" className="duty-composer-option" aria-expanded={skillMenu} aria-controls="duty-skill-picker"
            disabled={loading} onClick={() => setSkillMenu(open => !open)}><Slash size={14} /> 技能</button>
          <div className="duty-engine-select">
            <select value={mode} onChange={e => (autonomous ? setAutonomousMode : setAuditMode)(e.target.value)}
              aria-label="问答方式" disabled={loading}>
              {autonomous && <option value="auto">自动选择</option>}
              <option value="local">本地分析</option>
              <option value="deepseek" disabled={!model}>{model ? "DeepSeek" : "DeepSeek 未配置"}</option>
            </select>
            <ChevronDown size={13} aria-hidden="true" />
          </div>
        </div>
        {loading
          ? <button type="button" className="copilot-send-btn is-stop" onClick={stopWaiting} aria-label="停止等待" title="停止等待">
              <Square size={13} fill="currentColor" />
            </button>
          : <button type="submit" className="copilot-send-btn" disabled={!input.trim()} aria-label="发送问题" title="发送问题">
              <ArrowUp size={19} strokeWidth={2.1} />
            </button>}
      </div>
    </form>
  </div>;

  return <>
    <aside id="duty-sidebar" className="duty-sidebar" hidden={!sidebarOpen} aria-label="工作台导航">
      <div className="duty-sidebar-brand">
        <span className="duty-brand-mark"><Zap size={19} strokeWidth={1.9} /></span>
        <div>
          <strong>老洼地</strong>
          <span>配电网值班台</span>
        </div>
      </div>
      <button type="button" className="duty-new-chat" onClick={resetSession}>
        <Plus size={17} /> 新对话
      </button>
      <div className="duty-sidebar-scroll">
        <div className="duty-sidebar-section">
          {navigation}
        </div>
        <div className="duty-sidebar-section">
          <p className="duty-sidebar-label">值班记录</p>
          <button type="button" className="duty-side-link" aria-expanded={chatVisible && showMemory} onClick={() => {
            onShowChat();
            setShowMemory(open => !chatVisible || !open);
          }}>
            <BookmarkCheck size={17} strokeWidth={1.65} /> <span>人工复核记录</span>
          </button>
        </div>
        <div className="duty-sidebar-section duty-history">
          <p className="duty-sidebar-label">历史对话</p>
          {sessionsList.length === 0 ? <p className="duty-history-empty">暂无历史对话</p> : sessionsList.map(s => (
            <div key={s.session_id} className="duty-history-row">
              <button
                type="button"
                className="duty-side-link"
                aria-current={chatVisible && s.session_id === sessionId ? "page" : undefined}
                onClick={() => loadHistorySession(s.session_id)}
                title={s.title}
                disabled={loading}
              >
                <MessageSquare size={15} strokeWidth={1.65} />
                <span>{s.title}</span>
              </button>
              <button
                type="button"
                className="duty-icon-button duty-history-delete"
                onClick={() => removeHistorySession(s.session_id)}
                aria-label={"删除对话 " + s.title}
                title="删除对话"
                disabled={loading}
              ><Trash2 size={14} /></button>
            </div>
          ))}
        </div>
      </div>
      {archiveRange && <div className="duty-sidebar-footer">
        <span className="duty-archive-icon"><FileSpreadsheet size={16} strokeWidth={1.75} /></span>
        <div><strong>运行日报</strong><small>{archiveRange}</small></div>
      </div>}
    </aside>

    <main className="copilot-pane duty-chat" hidden={!chatVisible} aria-label="值班问答">
      <AgentStreamStyles />
      <div className="agent-stream-mode" role="group" aria-label="诊断模式">
        <button type="button" aria-pressed={agentKind === "autonomous"} disabled={loading}
          onClick={() => setAgentKind("autonomous")}>自主深度 Agent</button>
        <button type="button" aria-pressed={agentKind === "audit"} disabled={loading}
          onClick={() => setAgentKind("audit")}>严格审计单</button>
      </div>
      {connectionError && <div className="duty-notice" role="alert">
        <span>{connectionError}</span>
        <button type="button" onClick={onRetry}>重新连接</button>
      </div>}
      <div className={"copilot-thread duty-thread" + (empty ? " is-empty" : "")} ref={scrollRef}>
        {empty ? (
          <div className="duty-welcome">
            <div className="duty-welcome-mark" aria-hidden="true"><Zap size={25} strokeWidth={1.7} /></div>
            <h1>今天想核查什么？</h1>
            <p>{autonomous ? "查原表、比较运行趋势，或用潮流复算检验工况。"
              : "查运行日报、回看监测事件，或核对不同柜的同期读数。"}</p>
            {composer}
            <div className="duty-prompts" aria-label="示例问题">
              {(autonomous ? AUTONOMOUS_PROMPTS : AUDIT_PROMPTS).map(({label, detail, Icon, text, runOnClick}) => (
                <button type="button" key={label} className="duty-prompt"
                  onClick={() => runOnClick ? sendQuestion(text) : usePrompt(text)}>
                  <Icon size={18} strokeWidth={1.65} />
                  <span><strong>{label}</strong><small>{detail}</small></span>
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="duty-thread-column">
            {turns.map((t, index) => t.skillRun ? <SkillTurn key={t.id} turn={t} /> : t.stream ? (
              <AutonomousTurn key={t.id} turn={t} onTextProgress={followTypedAnswer} onOpenMemory={onOpenMemory}
                onSyncCanvas={onSyncCanvas} onDraftAction={onDraftAction} onDeepResearch={onDeepResearch} />
            ) : (
              <AgentTurnMessage
                key={t.id}
                turn={t}
                sessionId={sessionId}
                health={health}
                onSyncCanvas={onSyncCanvas}
                onOpenCabinet={onOpenCabinet}
                onReviewsUpdate={onReviewsUpdate}
                onOpenMemory={onOpenMemory}
                onQuickFollowUp={q => sendQuestion(q)}
                canCompareAdjacent={!loading && index === turns.length - 1}
              />
            ))}
            {loading && (pendingPrompt || liveTrace) && agentKind === "audit" && (
              <div className="chat-turn">
                {pendingPrompt && <div className="chat-user-row"><div className="chat-user-bubble">{pendingPrompt}</div></div>}
                <div className="duty-progress" role="status"><Spinner size="sm" /> 正在查证{elapsed > 0 ? ` · ${elapsed} 秒` : ""}</div>
                {liveTrace && <details className="duty-progress-detail">
                  <summary>查看处理进度</summary>
                  <LiveExecutionCard liveTrace={liveTrace} elapsed={elapsed} />
                </details>}
              </div>
            )}
          </div>
        )}
      </div>
      {!empty && <div className="duty-chat-bottom">{composer}</div>}
      {showMemory && <div className="copilot-drawer-panel duty-memory-panel">
        <div className="copilot-drawer-head">
          <strong>人工复核记录</strong>
          <button type="button" className="copilot-close-x" aria-label="关闭复核记录" onClick={() => setShowMemory(false)}><X size={15} /></button>
        </div>
        {allReviewsList.length === 0 ? <p className="muted">暂无人工复核记录。</p> : (
          <div className="copilot-drawer-list">
            {allReviewsList.map(item => (
              <div key={item.finding_id} className="copilot-drawer-item">
                <div className="copilot-drawer-main">
                  <div className="copilot-mem-top">
                    <code className="mono">{item.finding_id}</code>
                    <span className="agent-mini-pill is-green">{item.disposition_label}</span>
                  </div>
                  <small className="muted">{item.operator_note || item.switch_state || "已登记人工复核"}</small>
                </div>
                <button type="button" className="copilot-trash-btn" aria-label="删除这条人工复核记录" onClick={() => removeMemoryItem(item.finding_id)}>
                  <Trash2 size={13} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>}
    </main>
  </>;
}
