import React, {useState} from "react";
import {Badge} from "@astryxdesign/core/Badge";
import {Button} from "@astryxdesign/core/Button";
import {ClipboardCheck, Send, ShieldCheck} from "lucide-react";
import {apiRequest} from "./api.js";
import {EvidenceDownloadLink} from "./IncidentTicketPanel.jsx";

export const ACTION_KIND_LABELS = {
  switching_order: "电气操作票",
  defect_notice: "消缺通知单",
  field_verification: "现场核查单",
};

const STATUSES = {
  draft: ["待人工核对", "warning"],
  approved: ["已签发", "success"],
  rejected: ["已驳回", "error"],
  dispatched: ["已通知", "success"],
};
const RISKS = {
  critical: ["重大风险", "error"],
  high: ["高风险", "error"],
  medium: ["中风险", "warning"],
  low: ["低风险", "neutral"],
};
const HISTORY_LABELS = {
  drafted: "起草", checklist_checked: "人工勾选", checklist_unchecked: "取消勾选",
  approve: "签发", approved: "签发", reject: "驳回", rejected: "驳回",
  dispatch: "通知", dispatched: "通知", dispatch_simulated: "通知演练",
};

export function ActionApprovalCard({ticket, onChange, webhookConfigured = false}) {
  const [pending, setPending] = useState("");
  const [error, setError] = useState("");
  const [receipt, setReceipt] = useState("");
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [confirmPush, setConfirmPush] = useState(false);
  const [checking, setChecking] = useState(null);
  const id = ticket.ticket_id ?? ticket.id;
  const draft = ticket.status === "draft";
  const checklist = ticket.checklist ?? [];
  const history = ticket.audit_log ?? [];
  const required = checklist.filter(item => item.required !== false);
  const missing = required.filter(item => !item.checked);
  const allChecked = required.length > 0 && missing.length === 0;
  const solverBlocked = ticket.technical_basis?.solver_status === "failed"
    || ticket.technical_basis?.needs_verification === true
    || ticket.checklist_progress?.verification_blocked === true;
  const templateKv = ticket.kind === "switching_order"
    ? checklist.find(item => item.id === "chk_3")?.text?.match(/(\d+(?:\.\d+)?)\s*kV/i)?.[1]
    : null;
  const calculatedKv = ticket.technical_basis?.expected_bus_10kv_ii_kv;
  const voltageMismatch = templateKv && Number.isFinite(calculatedKv) && Number(templateKv) !== calculatedKv;
  const approved = ["approved", "dispatched"].includes(ticket.status);
  const delivered = ticket.dispatch_log?.some(log => log.status === "delivered");
  const [status, statusTone] = ticket.status === "dispatched" && !delivered ? ["已演练通知", "success"]
    : STATUSES[ticket.status] ?? [ticket.status, "neutral"];
  const riskValue = ticket.risk_level ?? ticket.risk;
  const [risk, riskTone] = RISKS[riskValue] ?? [riskValue || "风险待核对", "neutral"];

  async function submit(action, body) {
    setPending(action);
    setError("");
    setReceipt("");
    setConfirmPush(false);
    try {
      const result = await apiRequest(`/api/actions/tickets/${encodeURIComponent(id)}/${action}`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      });
      onChange(result.ticket);
      if (action === "reject") setRejecting(false);
      if (action === "dispatch") {
        setReceipt(result.dispatch?.dry_run ? "Webhook 通知演练已留痕。" : "Webhook 通知已送出。");
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setPending("");
      setChecking(null);
    }
  }

  return <article className="liveops-ticket" aria-label={`票据 ${id}`}>
    <header className="liveops-card-head">
      <div><h3>{ACTION_KIND_LABELS[ticket.kind] ?? ticket.kind}</h3><code>{id}</code></div>
      <div className="liveops-actions"><Badge variant={riskTone} label={risk} /><Badge variant={statusTone} label={status} /></div>
    </header>
    <p className="liveops-meta">{[ticket.target_cabinet && `${ticket.target_cabinet} 柜`, ticket.at_time].filter(Boolean).join(" · ")}</p>
    {ticket.rationale && <p className="liveops-wrap">{ticket.rationale}</p>}
    <details className="liveops-history">
      <summary>票据草案与原表依据</summary>
      <p>{ticket.title}</p>
      {ticket.evidence_cells?.length > 0
        ? <div className="liveops-actions">{ticket.evidence_cells.map(cell => <EvidenceDownloadLink key={cell} cell={cell} />)}</div>
        : <p className="liveops-meta">缺少同柜同刻的归档原表证据，待现场补证。</p>}
      {ticket.operation_steps?.length > 0 && <ul>{ticket.operation_steps.map((step, i) => <li key={i}>{step}</li>)}</ul>}
      {ticket.rollback_plan && <p>{ticket.rollback_plan}</p>}
    </details>

    <fieldset className="liveops-checklist" disabled={!draft || Boolean(pending)}>
      <legend>安全前置核对</legend>
      {checklist.map(item => {
        const key = item.item_id ?? item.id ?? item.key;
        const label = item.text ?? key;
        return <label key={key} className="liveops-check">
          <input type="checkbox" checked={checking?.id === key ? checking.checked : Boolean(item.checked)}
            onChange={e => {
              const checked = e.target.checked;
              setChecking({id: key, checked});
              submit("checklist", {item_id: key, checked});
            }} />
          <span>{label}{item.required !== false && <small className="liveops-meta"> · 必选</small>}
            {(item.checked_by || item.checked_at) && <small className="liveops-meta liveops-check-actor">
              {[item.checked_by, item.checked_at].filter(Boolean).join(" · ")}
            </small>}
          </span>
        </label>;
      })}
    </fieldset>

    {voltageMismatch && <p className="liveops-error" role="status">
      核对项写有 {templateKv} kV，票据复算回执为 {calculatedKv} kV，待人工核实。
    </p>}
    {draft && solverBlocked && <p className="liveops-error" role="status">
      交流潮流复算未完成或缺少有效推荐挡位，当前草案锁定在待核状态，暂不可签发。
    </p>}
    {draft && !allChecked && <p className="liveops-meta" role="status">
      {missing.length > 0 ? `尚有 ${missing.length} 项必选项待人工核对，暂不可签发。` : "票据缺少必选核对项，暂不可签发。"}
    </p>}
    {error && <p className="liveops-error" role="alert">{error}</p>}
    {receipt && <p className="liveops-meta" role="status">{receipt}</p>}

    {draft && <div className="liveops-actions">
      <Button label="签发" size="sm" variant="primary" icon={<ShieldCheck size={15} />}
        disabled={!allChecked || solverBlocked || Boolean(pending)} onClick={() => submit("approve", {})} />
      <Button label="驳回" size="sm" disabled={Boolean(pending)} onClick={() => setRejecting(true)} />
    </div>}
    {draft && rejecting && <form className="liveops-ticket-form" onSubmit={e => {
      e.preventDefault();
      submit("reject", {reason: reason.trim()});
    }}>
      <label>驳回原因<textarea aria-label="驳回原因" rows={2} maxLength={240} value={reason} onChange={e => setReason(e.target.value)} required /></label>
      <div className="liveops-actions">
        <Button type="submit" label="确认驳回" size="sm" disabled={!reason.trim() || Boolean(pending)} />
        <Button type="button" label="取消驳回" size="sm" disabled={Boolean(pending)} onClick={() => setRejecting(false)} />
      </div>
    </form>}
    {approved && <div className="liveops-actions">
      <Button label="通知演练" size="sm" icon={<Send size={15} />} disabled={Boolean(pending)}
        onClick={() => submit("dispatch", {dry_run: true})} />
      {webhookConfigured && !delivered && (confirmPush ? <>
        <Button label="确认推送 Webhook" size="sm" disabled={Boolean(pending)} onClick={() => submit("dispatch", {dry_run: false})} />
        <Button label="取消推送" size="sm" onClick={() => setConfirmPush(false)} />
      </> : <Button label="推送 Webhook" size="sm" disabled={Boolean(pending)} onClick={() => setConfirmPush(true)} />)}
    </div>}

    {history.length > 0 && <details className="liveops-history">
      <summary><ClipboardCheck size={14} /> 留痕记录 · {history.length}</summary>
      <ol>{history.map((entry, index) => <li key={entry.id ?? index}>
        <strong>{HISTORY_LABELS[entry.action] ?? entry.action ?? entry.event}</strong>
        <span>{[entry.actor, entry.ts].filter(Boolean).join(" · ")}</span>
        {entry.detail && <p>{entry.detail}</p>}
      </li>)}</ol>
    </details>}
  </article>;
}
