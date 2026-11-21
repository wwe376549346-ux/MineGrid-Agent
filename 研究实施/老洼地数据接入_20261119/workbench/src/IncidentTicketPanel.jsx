import React, {useEffect, useState} from "react";
import {
  Activity,
  AlertTriangle,
  Bell,
  CheckCircle2,
  ClipboardCheck,
  Download,
  RefreshCw,
  Send,
  ShieldAlert,
  Sparkles,
  UserCheck,
} from "lucide-react";
import {Badge} from "@astryxdesign/core/Badge";
import {Button} from "@astryxdesign/core/Button";
import {Card} from "@astryxdesign/core/Card";
import {Spinner} from "@astryxdesign/core/Spinner";
import {TextInput} from "@astryxdesign/core/TextInput";
import {apiRequest, authHeaders, cellHref, SEVERITY_META} from "./api.js";

const STATUS_BADGE = {
  open: "error",
  claimed: "info",
  supplemented: "teal",
  disputed: "warning",
  escalated: "error",
  handed_over: "warning",
  closed: "success",
  reopened: "error",
  superseded: "neutral",
};

const FILTER_OPTIONS = [
  {id: "active", label: "当前活跃"},
  {id: "open", label: "待认领"},
  {id: "claimed", label: "处理中"},
  {id: "handed_over", label: "交接中"},
  {id: "disputed", label: "否决/冲突"},
  {id: "closed", label: "已销项"},
  {id: "superseded", label: "已替代"},
  {id: "all", label: "全部"},
];

const LIFECYCLE_ACTIONS = [
  {action: "claim", label: "认领处理", variant: "primary"},
  {action: "supplement", label: "已补证复算", variant: "secondary"},
  {action: "dispute", label: "人工否决/留冲突", variant: "secondary"},
  {action: "escalate", label: "超时/高危升级", variant: "secondary"},
  {action: "handover", label: "交接下一班", variant: "secondary"},
  {action: "close", label: "销项闭环", variant: "primary"},
  {action: "reopen", label: "复开工单", variant: "secondary"},
];

const OUTBOX_LABELS = {
  pending: "待发送",
  claimed: "待发送",
  sending: "发送中",
  failed: "等待重试",
  dead_letter: "重试用尽",
  delivered: "已送达",
  cancelled: "已取消发送",
};

function notificationLabel(notification) {
  if (notification.status === "revoked") {
    return notification.delivered_at ? "已送达后撤销" : "已撤销，送达情况待核对";
  }
  return OUTBOX_LABELS[notification.status] || notification.status;
}

const REVIEW_STATUS = {
  verified: "原表证据已核验",
  pending_site_review: "原因待现场核证",
  insufficient_data: "归档证据不足",
  warning: "证据核验待复查",
};

export function EvidenceDownloadLink({cell}) {
  const [error, setError] = useState("");
  const href = cellHref(cell);
  if (!href) return <span className="cell-chip">{cell}</span>;

  async function downloadWithAuth(event) {
    const headers = authHeaders();
    if (!headers.Authorization) return;
    event.preventDefault();
    setError("");
    try {
      const response = await fetch(href, {headers});
      if (!response.ok) throw new Error(`原表下载失败（${response.status}）`);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = decodeURIComponent(href.split("/").pop());
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <>
      <a className="cell-chip" href={href} download onClick={downloadWithAuth}>{cell}</a>
      {error && <span role="status">{error}</span>}
    </>
  );
}

function TicketEvidenceCells({cells}) {
  return (
    <div className="inline-cells">
      {cells.map(cell => <EvidenceDownloadLink key={cell} cell={cell} />)}
    </div>
  );
}

function PreDiagnosis({ticket, onContinueInChat}) {
  const pre = ticket.pre_diagnosis;
  if (!pre) return null;
  const checks = [
    ["已证伪假设", pre.refuted_hypotheses, "success", "采样点已证伪"],
    ["待核假设", pre.pending_hypotheses, "warning", "待核"],
  ];
  const missing = pre.missing_evidence ?? pre.report?.missing_evidence ?? [];
  const status = pre.verified ?? pre.verification_status;
  return (
    <div className="detail-block">
      <h4>AI 自动预证伪初诊</h4>
      {status && <Badge variant={status === "verified" ? "success" : "warning"} label={REVIEW_STATUS[status] || status} />}
      <p>{pre.summary ?? pre.report?.summary}</p>
      {checks.map(([label, hypotheses, variant, badge]) => hypotheses?.length > 0 && (
        <div key={label}>
          <strong>{label}</strong>
          <ul>
            {hypotheses.map(({hypothesis, title, judgment}, index) => (
              <li key={`${hypothesis}-${index}`}>
                <Badge variant={variant} label={badge} /> <strong>{title}</strong>
                <div>{judgment}</div>
              </li>
            ))}
          </ul>
        </div>
      ))}
      {missing.length > 0 && <>
        <strong>最小待补现场证据</strong>
        <ul>{missing.map((item, index) => <li key={index}>{item}</li>)}</ul>
      </>}
      {pre.report?.field_sop?.length > 0 && <ol>{pre.report.field_sop.map((step, index) => <li key={index}>{step}</li>)}</ol>}
      {pre.evidence_cells?.length > 0 && <>
        <strong>初诊原表单元格</strong>
        <TicketEvidenceCells cells={pre.evidence_cells} />
      </>}
      {onContinueInChat && pre.followup_question && (
        <Button
          size="sm"
          variant="primary"
          label="转入对话台继续追问"
          onClick={() => onContinueInChat(pre.followup_question, ticket)}
        />
      )}
    </div>
  );
}

function IncidentActionDraft({ticket, onDraftAction}) {
  const originalCabinet = String(ticket.cabinet ?? "").trim();
  const singleCabinet = /^\d{1,16}$/.test(originalCabinet);
  const [target, setTarget] = useState(singleCabinet ? originalCabinet : "");
  const [kind, setKind] = useState("defect_notice");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  async function submit(event) {
    event.preventDefault();
    const cleanCabinet = target.trim();
    if (submitting || !/^\d{1,16}$/.test(cleanCabinet)) return;
    setSubmitting(true);
    setError("");
    try {
      await onDraftAction({
        kind,
        target_cabinet: cleanCabinet,
        at_time: ticket.at_time || null,
        rationale: `来自异常工单 ${ticket.ticket_id} · ${ticket.title}\n${ticket.summary ?? ""}`.slice(0, 1600),
      });
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  return <form className="detail-block" onSubmit={submit}>
    <div className="detail-actions" style={{flexWrap: "wrap", alignItems: "center"}}>
      {!singleCabinet && <label>
        目标柜号 <input aria-label="两票目标柜号" inputMode="numeric" maxLength={16} value={target}
          onChange={e => setTarget(e.target.value)} disabled={submitting} required />
      </label>}
      <label>
        草案类型 <select aria-label="草案类型" value={kind} onChange={e => setKind(e.target.value)} disabled={submitting}>
          <option value="defect_notice">消缺通知单</option>
          <option value="switching_order">电气操作票</option>
          <option value="field_verification">现场核查单</option>
        </select>
      </label>
      <Button type="submit" size="sm" label={submitting ? "正在起草…" : "一键起草两票草案"}
        icon={<ClipboardCheck size={15} />} disabled={submitting || !/^\d{1,16}$/.test(target.trim())} />
    </div>
    {error && <p role="alert">{error}</p>}
  </form>;
}

export function IncidentTicketPanel({onBadgeUpdate, onAskDiagnose, onContinueInChat, onDraftAction}) {
  const [statusFilter, setStatusFilter] = useState("active");
  const [tickets, setTickets] = useState([]);
  const [sla, setSla] = useState(null);
  const [llmops, setLlmops] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [err, setErr] = useState("");

  const [actorName, setActorName] = useState("白班值班长");
  const [shiftName, setShiftName] = useState("早班 08:00-16:00");
  const [actionNote, setActionNote] = useState("");

  async function refreshAll(nextStatus = statusFilter) {
    setLoading(true);
    setErr("");
    try {
      const [tRes, mRes] = await Promise.all([
        apiRequest(`/api/tickets?status=${encodeURIComponent(nextStatus)}&limit=50`),
        apiRequest("/api/llmops/metrics"),
      ]);
      setTickets(tRes.tickets || []);
      setSla(tRes.sla || null);
      setLlmops(mRes.llmops || null);
      onBadgeUpdate?.(tRes.sla?.unread_badge_count || 0);
      const rows = tRes.tickets || [];
      setSelectedId(current => rows.some(ticket => ticket.ticket_id === current) ? current : rows[0]?.ticket_id || null);
    } catch (e) {
      setErr(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refreshAll(statusFilter);
  }, [statusFilter]);

  useEffect(() => {
    setDetail(current => current?.ticket_id === selectedId ? current : null);
    setActionNote("");
    if (!selectedId) {
      return;
    }
    const ctrl = new AbortController();
    apiRequest(`/api/tickets/${encodeURIComponent(selectedId)}`, {signal: ctrl.signal})
      .then(setDetail)
      .catch(e => {
        if (e.name !== "AbortError") setErr(e.message);
      });
    return () => ctrl.abort();
  }, [selectedId]);

  async function handleProactiveScan() {
    setBusy("scan");
    setNotice("");
    setErr("");
    try {
      const res = await apiRequest("/api/proactive/scan", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({max_new_tickets: 6}),
      });
      setNotice(
        `巡检完成，新建 ${res.created_count} 张工单，跳过已有 ${res.skipped_existing} 张，送达 ${res.dispatch?.delivered || 0} 条通知。`,
      );
      await refreshAll(statusFilter);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy("");
    }
  }

  async function handleDispatchOutbox() {
    setBusy("dispatch");
    setNotice("");
    setErr("");
    try {
      const res = await apiRequest("/api/proactive/dispatch", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({limit: 50, force_retry_now: true}),
      });
      setNotice(`Outbox 重试投递完成：处理 ${res.processed} 条（送达 ${res.delivered} / 失败 ${res.failed} / 死信 ${res.dead_letter}）。`);
      await refreshAll(statusFilter);
      if (selectedId) {
        setDetail(await apiRequest(`/api/tickets/${encodeURIComponent(selectedId)}`));
      }
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy("");
    }
  }

  async function handleTransition(action) {
    if (!selectedId || detail?.ticket_id !== selectedId) return;
    const ticketUrl = `/api/tickets/${encodeURIComponent(selectedId)}`;
    setBusy(action);
    setNotice("");
    setErr("");
    try {
      const res = await apiRequest(ticketUrl + "/transition", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          action,
          actor: actorName.trim() || "值班员",
          shift_name: shiftName.trim(),
          note: actionNote.trim(),
          expected_revision: detail.revision,
        }),
      });
      setDetail(res.detail);
      setActionNote("");
      setNotice(`工单 ${selectedId} 已流转至【${res.ticket.status_label}】。`);
      await refreshAfterTransition(res.detail);
    } catch (e) {
      if (e.status === 409) {
        const latest = await apiRequest(ticketUrl).catch(() => null);
        if (latest) {
          setDetail(latest);
          await refreshAfterTransition(latest);
        }
      }
      setErr(e.message);
    } finally {
      setBusy("");
    }
  }

  async function refreshAfterTransition(ticket) {
    const stillListed = statusFilter === "all" || statusFilter === ticket.status ||
      (statusFilter === "active" && !["closed", "superseded"].includes(ticket.status));
    if (stillListed) await refreshAll();
    else setStatusFilter("all");
  }

  async function refreshPreDiagnosis(ticket) {
    const url = `/api/tickets/${encodeURIComponent(ticket.ticket_id)}`;
    setBusy("refresh-pre-diagnosis");
    setNotice("");
    setErr("");
    try {
      await apiRequest(`${url}/pre-diagnosis?expected_revision=${ticket.revision}`, {method: "POST"});
      const latest = await apiRequest(url);
      setDetail(current => current?.ticket_id === ticket.ticket_id ? latest : current);
      await refreshAfterTransition(latest);
      setNotice(`工单 ${ticket.ticket_id} 的初诊已刷新。`);
    } catch (e) {
      if (e.status === 409) {
        const latest = await apiRequest(url).catch(() => null);
        if (latest) {
          setDetail(current => current?.ticket_id === ticket.ticket_id ? latest : current);
          await refreshAfterTransition(latest);
        }
      }
      setErr(e.message);
    } finally {
      setBusy("");
    }
  }

  function downloadCmmsWorkorder() {
    if (!detail?.cmms_workorder) return;
    const blob = new Blob([JSON.stringify(detail.cmms_workorder, null, 2)], {type: "application/json"});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${detail.ticket_id}_cmms_workorder.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return <div className="view-stack">
    {/* Top Control Strip: Proactive Watcher + Outbox + LLMOps & Duty SLA */}
    <Card padding={18}>
      <div style={{display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap"}}>
        <div>
          <h3 style={{margin: 0, display: "flex", alignItems: "center", gap: 8}}>
            <ClipboardCheck size={17} />
            <span>值班事件处理单 · 自主巡检守护与 Outbox 投递闭环</span>
            {sla && (
              <Badge
                variant={sla.unread_badge_count > 0 ? "error" : "success"}
                label={sla.unread_badge_count > 0 ? `待认领/升级 ${sla.unread_badge_count}` : "当前无未认领告警"}
              />
            )}
          </h3>
          <p style={{margin: "6px 0 0", fontSize: 13, opacity: 0.78}}>
            串联“自主巡检发现 ➔ 带原表单元格预诊断 ➔ Outbox 持久投递 ➔ 认领/补证/否决/交接班/销项/CMMS 派工单导出”全生命周期。
          </p>
        </div>
        <div style={{display: "flex", gap: 8, flexWrap: "wrap"}}>
          <Button
            size="sm"
            variant="primary"
            label={busy === "scan" ? "自主巡检中…" : "立即自主巡检预诊断"}
            disabled={!!busy}
            onClick={handleProactiveScan}
          />
          <Button
            size="sm"
            variant="secondary"
            label={busy === "dispatch" ? "投递中…" : "Outbox 重试投递"}
            disabled={!!busy}
            onClick={handleDispatchOutbox}
          />
          <Button size="sm" variant="secondary" label="刷新" disabled={loading} onClick={() => refreshAll(statusFilter)} />
        </div>
      </div>

      {/* Duty SLA + LLMOps Summary KPIs */}
      {sla && (
        <div style={{display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(145px, 1fr))", gap: 10, marginTop: 14}}>
          <div className="detail-block" style={{margin: 0, padding: "10px 12px"}}>
            <div style={{fontSize: 11, opacity: 0.7}}>活跃工单 / 总工单</div>
            <strong style={{fontSize: 18}}>{sla.tickets_active} / {sla.tickets_total}</strong>
            <div style={{fontSize: 11, opacity: 0.7}}>待认领 {sla.tickets_open} · 交接 {sla.tickets_handed_over} · 替代 {sla.tickets_superseded}</div>
          </div>
          <div className="detail-block" style={{margin: 0, padding: "10px 12px"}}>
            <div style={{fontSize: 11, opacity: 0.7}}>Outbox 投递状态</div>
            <strong style={{fontSize: 18}}>{sla.outbox_delivered} 送达</strong>
            <div style={{fontSize: 11, opacity: 0.7}}>
              待发 {sla.outbox_pending} · 重试中 {sla.outbox_failed} · 死信 {sla.outbox_dead_letter}
              {(sla.outbox_cancelled + sla.outbox_revoked > 0) && <> · 已取消或撤销 {sla.outbox_cancelled + sla.outbox_revoked}</>}
            </div>
          </div>
          <div className="detail-block" style={{margin: 0, padding: "10px 12px"}}>
            <div style={{fontSize: 11, opacity: 0.7}}>平均认领响应 (SLA)</div>
            <strong style={{fontSize: 18}}>{sla.avg_ack_latency_s} s</strong>
            <div style={{fontSize: 11, opacity: 0.7}}>已销项 {sla.tickets_closed} · 否决留冲突 {sla.tickets_disputed}</div>
          </div>
          {llmops && (
            <div className="detail-block" style={{margin: 0, padding: "10px 12px"}}>
              <div style={{fontSize: 11, opacity: 0.7}}>LLMOps · TTFT / 成本</div>
              <strong style={{fontSize: 18}}>{llmops.ttft_ms_avg} ms</strong>
              <div style={{fontSize: 11, opacity: 0.7}}>
                Token {llmops.total_tokens} · ¥{llmops.estimated_cost_cny_total.toFixed(4)} · Failover {llmops.llm_failovers_total}
              </div>
            </div>
          )}
          {llmops && (
            <div className="detail-block" style={{margin: 0, padding: "10px 12px"}}>
              <div style={{fontSize: 11, opacity: 0.7}}>快慢路由 / 并行工具</div>
              <strong style={{fontSize: 18}}>快 {llmops.fast_tier_routed_total} / 慢 {llmops.slow_tier_routed_total}</strong>
              <div style={{fontSize: 11, opacity: 0.7}}>
                并行批 {llmops.parallel_tool_batches_total} · 核验告警率 {llmops.verifier_warning_rate_pct}%
              </div>
            </div>
          )}
        </div>
      )}

      {notice && <p style={{margin: "10px 0 0", fontSize: 12.5, color: "#0a7c42"}}>{notice}</p>}
      {err && <p role="alert" style={{margin: "10px 0 0", fontSize: 12.5, color: "#c92a2a"}}>{err}</p>}
    </Card>

    {/* Status Filter Pills */}
    <div className="apple-sld-filters">
      {FILTER_OPTIONS.map(opt => (
        <button
          key={opt.id}
          type="button"
          className={"apple-filter-pill" + (statusFilter === opt.id ? " is-active" : "")}
          disabled={!!busy}
          onClick={() => setStatusFilter(opt.id)}
        >
          <span>{opt.label}</span>
        </button>
      ))}
    </div>

    {/* Split Master-Detail Ticket Workbench */}
    <div className="health-grid">
      <Card padding={0} className="findings-pane">
        <div className="pane-head">
          <h3>事件工单列表 ({tickets.length})</h3>
        </div>
        {loading ? (
          <div style={{padding: 24, textAlign: "center"}}><Spinner size="md" /></div>
        ) : tickets.length === 0 ? (
          <div className="empty-pane" style={{padding: 24}}>
            当前筛选条件下暂无工单。可点击上方「立即自主巡检预诊断」，筛查高、中优先级发现并建单。
          </div>
        ) : (
          <ul className="finding-list">
            {tickets.map(t => (
              <li key={t.ticket_id}>
                <button
                  type="button"
                  className={"finding-item" + (selectedId === t.ticket_id ? " is-selected" : "")}
                  disabled={!!busy}
                  onClick={() => setSelectedId(t.ticket_id)}
                >
                  <div className="finding-item-top">
                    <Badge variant={SEVERITY_META[t.severity]?.badge || "neutral"} label={SEVERITY_META[t.severity]?.label || t.severity} />
                    <Badge variant={STATUS_BADGE[t.status] || "neutral"} label={t.status_label} />
                    <code className="mono">{t.ticket_id}</code>
                  </div>
                  <strong className="finding-title">{t.title}</strong>
                  <span className="finding-cab">
                    对象：{t.cabinet || "-"} · 班次：{t.shift_name} {t.assignee ? `· 负责人：${t.assignee}` : ""}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card padding={0} className="detail-pane">
        {detail ? (
          <div className="detail-content">
            <div className="detail-top">
              <div className="detail-badges">
                <Badge variant={SEVERITY_META[detail.severity]?.badge || "neutral"} label={SEVERITY_META[detail.severity]?.label} />
                <Badge variant={STATUS_BADGE[detail.status] || "neutral"} label={detail.status_label} />
                <code className="mono">{detail.ticket_id}</code>
                <code className="mono">{detail.source_finding_id}</code>
              </div>
              <div className="detail-actions">
                <Button size="sm" variant="secondary" label="导出 CMMS 派工单" onClick={downloadCmmsWorkorder} />
                {detail.source_finding_id.startsWith("HLTH-") && !["closed", "superseded"].includes(detail.status) && (
                  <Button size="sm" variant="secondary" label="刷新初诊" disabled={Boolean(busy)} onClick={() => refreshPreDiagnosis(detail)} />
                )}
                {onAskDiagnose && !detail.pre_diagnosis?.followup_question && (
                  <Button
                    size="sm"
                    variant="primary"
                    label="深度追问"
                    onClick={() => onAskDiagnose(`针对工单 ${detail.ticket_id}（${detail.title}），请复核关键证据并给出下一步班次排查要点。`)}
                  />
                )}
              </div>
            </div>

            <h2 className="detail-title">{detail.title}</h2>
            <p className="detail-summary">{detail.summary}</p>
            {onDraftAction && <IncidentActionDraft key={detail.ticket_id} ticket={detail} onDraftAction={onDraftAction} />}
            {detail.evidence_cells?.length > 0 && (
              <div className="detail-block">
                <h4>原表证据</h4>
                <TicketEvidenceCells cells={detail.evidence_cells} />
              </div>
            )}
            {detail.superseded_reason && (
              <div className="detail-block" style={{borderLeft: "3px solid #f59f00", paddingLeft: 10}}>
                <strong>数据更正替代说明：</strong>{detail.superseded_reason}
              </div>
            )}

            <PreDiagnosis ticket={detail} onContinueInChat={onContinueInChat} />

            {/* Shift Lifecycle Transition Controls */}
            <div className="detail-block">
              <h4>班次流转与交接操作</h4>
              <div style={{display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginBottom: 8}}>
                <TextInput label="值班负责人" value={actorName} onChange={setActorName} width="100%" />
                <TextInput label="所属班次" value={shiftName} onChange={setShiftName} width="100%" />
              </div>
              <div style={{marginBottom: 10}}>
                <TextInput
                  label="处置/否决/交接说明"
                  placeholder="销项和复开须填写依据"
                  value={actionNote}
                  onChange={setActionNote}
                  width="100%"
                />
              </div>
              <div style={{display: "flex", gap: 6, flexWrap: "wrap"}}>
                {LIFECYCLE_ACTIONS.filter(act => detail.allowed_actions?.includes(act.action)).map(act => (
                  <Button
                    key={act.action}
                    size="sm"
                    variant={act.variant}
                    label={busy === act.action ? "提交中…" : act.label}
                    disabled={!!busy || (detail.note_required_actions?.includes(act.action) && !actionNote.trim())}
                    onClick={() => handleTransition(act.action)}
                  />
                ))}
              </div>
            </div>

            {/* Outbox Receipts */}
            {detail.outbox?.length > 0 && (
              <div className="detail-block">
                <h4>通知记录 ({detail.outbox.length})</h4>
                <ul style={{margin: 0, paddingLeft: 18, fontSize: 12.5}}>
                  {detail.outbox.map(o => (
                    <li key={o.outbox_id}>
                      <span>{o.channel === "in_app" ? "站内通知" : o.channel}</span> · <strong>{notificationLabel(o)}</strong>（尝试 {o.attempts}/{o.max_attempts}）
                      {o.withdrawn_at ? <> · 撤销时间 <time dateTime={o.withdrawn_at}>{o.withdrawn_at}</time></> : null}
                      {o.receipt_sha256 ? <> · 回执 <code>{o.receipt_sha256}</code></> : null}
                      {o.last_error ? <> · 错误：{o.last_error}</> : null}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {/* Immutable Shift Audit Log */}
            {detail.logs?.length > 0 && (
              <div className="detail-block">
                <h4>全生命周期审计流转记录 ({detail.logs.length})</h4>
                <ol style={{margin: 0, paddingLeft: 18, fontSize: 12.5}}>
                  {detail.logs.map(l => (
                    <li key={l.seq}>
                      <span className="mono">{l.created_at}</span> · <strong>{l.actor}</strong>（{l.shift_name || "-"}）执行{" "}
                      <code>{l.action}</code>（{l.from_status || "NEW"} → {l.to_status}）：{l.note}
                    </li>
                  ))}
                </ol>
              </div>
            )}
          </div>
        ) : (
          <div className="empty-pane">请选择左侧任意事件处理单，查看自主预诊断、Outbox 回执与班次流转操作。</div>
        )}
      </Card>
    </div>
  </div>;
}
