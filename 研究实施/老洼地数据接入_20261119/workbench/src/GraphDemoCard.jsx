import React, {useState} from "react";
import {
  CheckCircle2,
  FastForward,
  GitBranch,
  PauseCircle,
  Play,
  ShieldAlert,
  ShieldOff,
} from "lucide-react";
import {Badge} from "@astryxdesign/core/Badge";
import {Card} from "@astryxdesign/core/Card";
import {ProgressBar} from "@astryxdesign/core/ProgressBar";
import {apiRequest} from "./api.js";

const NODES = [
  {id: "init_snapshot", num: "01", label: "快照绑定", desc: "锁定 7 日报 + 2 截图 SHA-256"},
  {id: "guard_check", num: "02", label: "安全门控", desc: "拦截写操作与控制命令"},
  {id: "select_action", num: "03", label: "动作规划", desc: "按任务编排只读工具序列"},
  {id: "execute_tool", num: "04", label: "只读查库", desc: "恢复时跳过已确认步骤"},
  {id: "compile_evidence", num: "05", label: "证据编译", desc: "汇总原表单元格与规程"},
  {id: "evaluate_gate", num: "06", label: "补证判定", desc: "缺参数挂起 / 齐参数复算"},
  {id: "finalize_terminal", num: "07", label: "显式终态", desc: "completed / suspend / restricted"},
];

export function GraphDemoCard() {
  const [running, setRunning] = useState(false);
  const [demoState, setDemoState] = useState(null);
  const [lastAction, setLastAction] = useState("");
  const [err, setErr] = useState("");

  async function trigger(action) {
    setRunning(true);
    setErr("");
    setLastAction(action);
    try {
      const res = await apiRequest("/api/graph/demo", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({action, threadId: "UI-DEMO-303-1113"}),
      });
      setDemoState(res.state);
    } catch (e) {
      setErr(e.message);
    } finally {
      setRunning(false);
    }
  }

  const traceStr = (demoState?.node_trace || []).join(" ");
  const status = demoState?.terminal_status;
  const cj = demoState?.supplement_result?.conditional_judgment;

  return <div className="view-stack">
    <Card padding={20} className="graph-demo-card">
      <div className="graph-head-row">
        <div>
          <div className="apple-title-row">
            <GitBranch size={17} className="apple-accent-icon" />
            <h2>七节点状态图编排与跨进程检查点恢复</h2>
          </div>
          <p className="muted" style={{marginTop: 4}}>
            先点击「① 发起核验并挂起」将 3 步 SQLite 查询结果持久化到检查点，再点击「② 补参恢复」体验 0 次重复查库直接完成复算。
          </p>
        </div>
        {status && (
          <Badge
            variant={status === "completed" ? "success" : status === "needs_supplement" ? "warning" : "error"}
            label={`终态：${status}`}
          />
        )}
      </div>

      {/* Clean Two-Row Action Bar (Primary Sequence + Safety Guard Tests) */}
      <div className="graph-toolbar">
        <div className="graph-toolbar-group">
          <button
            type="button"
            className="apple-demo-btn is-primary"
            disabled={running}
            onClick={() => trigger("suspend")}
          >
            <PauseCircle size={14} />
            <span>① 发起 11-13 核验并挂起</span>
          </button>
          <span className="graph-step-sep" aria-hidden="true">→</span>
          <button
            type="button"
            className="apple-demo-btn is-green"
            disabled={running}
            onClick={() => trigger("resume")}
          >
            <Play size={14} />
            <span>② 补入定值从检查点恢复</span>
          </button>
        </div>

        <div className="graph-toolbar-group">
          <button
            type="button"
            className="apple-demo-btn is-subtle"
            disabled={running}
            onClick={() => trigger("tamper")}
          >
            <ShieldAlert size={13} />
            <span>测快照篡改拦截</span>
          </button>
          <button
            type="button"
            className="apple-demo-btn is-subtle"
            disabled={running}
            onClick={() => trigger("restrict")}
          >
            <ShieldOff size={13} />
            <span>测遥控分闸拦截</span>
          </button>
        </div>
      </div>

      {/* Clean Zero-Overflow Grid for 7 Nodes */}
      <div className="graph-node-grid">
        {NODES.map(n => {
          const hit = traceStr.includes(n.id);
          const reused =
            n.id === "execute_tool" &&
            lastAction === "resume" &&
            (demoState?.reused_steps_in_this_run?.length || 0) > 0;
          const blocked =
            status === "restricted" &&
            ((n.id === "guard_check" && lastAction === "restrict") ||
              (n.id === "init_snapshot" && lastAction === "tamper"));

          return <div
            key={n.id}
            className={
              "graph-node-tile" +
              (blocked ? " is-blocked" : reused ? " is-reused" : hit ? " is-hit" : "")
            }
          >
            <div className="graph-node-tile-top">
              <span className="mono graph-node-num">{n.num}</span>
              {reused ? (
                <span className="agent-mini-pill is-green">
                  <FastForward size={10} style={{marginRight: 3}} />
                  复用 3 步
                </span>
              ) : blocked ? (
                <span className="agent-mini-pill is-red">已拦截</span>
              ) : hit ? (
                <CheckCircle2 size={13} className="apple-accent-icon" />
              ) : null}
            </div>
            <strong>{n.label}</strong>
            <small>{n.desc}</small>
          </div>;
        })}
      </div>

      {err && <p className="save-err" role="alert">{err}</p>}

      {demoState ? (
        <div className="graph-demo-readout">
          <div className="graph-stat-row">
            <div className="graph-stat-item">
              <span>线程与恢复次数</span>
              <strong className="mono">{demoState.thread_id} · #{demoState.resume_count}</strong>
            </div>
            <div className="graph-stat-item">
              <span>本轮新执行查库</span>
              <strong className="mono">{demoState.executed_in_this_run?.length ?? 0} 步</strong>
            </div>
            <div className="graph-stat-item">
              <span>检查点直接复用</span>
              <strong className="mono" style={{color: "var(--apple-green)"}}>
                {demoState.reused_steps_in_this_run?.length ?? 0} 步
              </strong>
            </div>
            <div className="graph-stat-item">
              <span>停止原因</span>
              <strong className="mono">{demoState.stop_reason}</strong>
            </div>
          </div>

          {demoState.missing_requirements?.length > 0 && (
            <div className="graph-missing-box">
              <strong>当前挂起等待补齐的现场参数：</strong>
              <ul>
                {demoState.missing_requirements.map((m, i) => <li key={i}>{m}</li>)}
              </ul>
            </div>
          )}

          {demoState.supplement_result && (
            <div className="graph-supp-result">
              <div className="graph-supp-callout">
                <CheckCircle2 size={15} className="icon-green" style={{flex: "none", marginTop: 2}} />
                <span>
                  <strong>
                    局部复算完成（{demoState.supplement_result.parent_run_id} ➔ {demoState.supplement_result.v2_run_id}）：
                  </strong>
                  {demoState.supplement_result.explanation}
                </span>
              </div>

              {cj && (
                <div className="graph-gauge-pair">
                  <ProgressBar
                    value={cj.current_load_ratio_pct}
                    max={100}
                    label={`303 峰值电流负载率：${cj.peak_current_a} A / ${cj.effective_current_limit_a} A（裕度 +${cj.current_margin_pct}%）`}
                    hasValueLabel
                    variant={cj.current_load_ratio_pct <= 80 ? "success" : "warning"}
                  />
                  <ProgressBar
                    value={cj.transformer_load_ratio_pct}
                    max={100}
                    label={`1#主变视在负载率：${cj.transformer_peak_kva} kVA / ${cj.transformer_rated_kva} kVA（裕度 +${cj.transformer_margin_pct}%）`}
                    hasValueLabel
                    variant={cj.transformer_load_ratio_pct <= 85 ? "success" : "warning"}
                  />
                </div>
              )}
            </div>
          )}
        </div>
      ) : (
        <div className="graph-empty-hint">
          点击上方 <strong>「① 发起 11-13 核验并挂起」</strong> 开始演练状态图执行与 SQLite 检查点落盘。
        </div>
      )}
    </Card>
  </div>;
}
