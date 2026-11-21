import React, {useEffect, useMemo, useState} from "react";
import {Badge} from "@astryxdesign/core/Badge";
import {Button} from "@astryxdesign/core/Button";
import {Card} from "@astryxdesign/core/Card";
import {SegmentedControl, SegmentedControlItem} from "@astryxdesign/core/SegmentedControl";
import {StatusDot} from "@astryxdesign/core/StatusDot";
import {TextArea} from "@astryxdesign/core/TextArea";
import {TextInput} from "@astryxdesign/core/TextInput";
import {
  apiRequest,
  DIMENSION_LABELS,
  DISPOSITION_META,
  formatNum,
  SEVERITY_META,
  sourceUrl,
} from "./api.js";
import {SingleLineDiagram} from "./SingleLineDiagram.jsx";

function ReviewForm({finding, currentReview, scopeDay, onSaved}) {
  const [disposition, setDisposition] = useState(currentReview?.disposition || "pending");
  const [switchState, setSwitchState] = useState(currentReview?.switch_state || "");
  const [note, setNote] = useState(currentReview?.operator_note || "");
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [err, setErr] = useState("");

  useEffect(() => {
    setDisposition(currentReview?.disposition || "pending");
    setSwitchState(currentReview?.switch_state || "");
    setNote(currentReview?.operator_note || "");
    setFeedback("");
    setErr("");
  }, [finding.id, scopeDay, currentReview?.updated_at]);

  async function submit(event) {
    event.preventDefault();
    setSaving(true);
    setFeedback("");
    setErr("");
    try {
      const res = await apiRequest("/api/reviews", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          finding_id: finding.id,
          scope_day: scopeDay || "ALL",
          disposition,
          switch_state: switchState,
          operator_note: note,
        }),
      });
      onSaved(res.reviews);
      setFeedback(`已写入值班复核台账（${res.saved.updated_at}），后续智能诊断将自动复用该记录。`);
    } catch (error) {
      setErr(error.message);
    } finally {
      setSaving(false);
    }
  }

  return <form className="review-form" onSubmit={submit}>
    <div className="review-form-head">
      <h4>值班员人工复核与案例沉淀</h4>
      {currentReview && <Badge variant={DISPOSITION_META[currentReview.disposition]?.badge || "neutral"}
        label={`当前：${currentReview.disposition_label}`} />}
    </div>
    <SegmentedControl label="人工复核判定" size="sm" layout="fill" value={disposition} onChange={setDisposition}>
      <SegmentedControlItem value="pending" label="待复核" />
      <SegmentedControlItem value="confirmed_anomaly" label="确认异常" />
      <SegmentedControlItem value="normal_operation" label="正常/计划倒闸" />
      <SegmentedControlItem value="needs_site_check" label="待现场核查" />
    </SegmentedControl>
    <div className="review-inputs">
      <TextInput
        label="补录开关 / 运行方式状态（可选）"
        value={switchState}
        onChange={setSwitchState}
        placeholder="例如：35kV母联300合位、306检修退出、2#主变分接头3档"
        width="100%"
      />
      <TextArea
        label="现场复核说明 / 倒闸票号 / 核查结论"
        value={note}
        onChange={setNote}
        placeholder="记录现场核实结果或待查事项。保存后仅写入独立复核台账，不改动原始日报库。"
        rows={3}
        width="100%"
      />
    </div>
    <div className="review-actions">
      <Button type="submit" variant="primary" size="md" label={saving ? "正在保存…" : "保存复核结论"} isDisabled={saving} />
      {feedback && <span className="save-ok" role="status">{feedback}</span>}
      {err && <span className="save-err" role="alert">{err}</span>}
    </div>
  </form>;
}

export function HealthView({
  health,
  dates,
  dayFilter,
  setDayFilter,
  onReviewsUpdate,
  onAskDiagnose,
  onOpenCabinet,
  externalReplayIndex = null,
  focusCabinet = "",
}) {
  const [subMode, setSubMode] = useState("sld");
  const [sevFilter, setSevFilter] = useState("all");
  const [selectedId, setSelectedId] = useState("");

  const reviews = health.reviews || {};
  const counts = health.summary.finding_counts;
  const bus = health.dimensions.power_quality.bus_segments;
  const bal = health.dimensions.system_balance.balance_summary;

  const filtered = useMemo(() => {
    return (health.findings || []).filter(item => sevFilter === "all" || item.severity === sevFilter);
  }, [health.findings, sevFilter]);

  useEffect(() => {
    if (!filtered.length) {
      setSelectedId("");
      return;
    }
    if (!filtered.some(item => item.id === selectedId)) {
      setSelectedId(filtered[0].id);
    }
  }, [filtered, selectedId]);

  const active = filtered.find(item => item.id === selectedId) || filtered[0] || null;
  const reviewedCount = (health.findings || []).filter(f => reviews[f.id] && reviews[f.id].disposition !== "pending").length;
  const legacyRaw = health.summary.legacy_raw_notices;
  const actionable = health.summary.actionable_events;

  function triggerAiFor(finding) {
    const q = `请针对健康体检发现【${finding.id} · ${finding.title}】（对象：${finding.cabinet}）开展深度电气诊断：核对相关柜前后窗口与功率平衡，给出按可能性排序的候选原因、关联《煤矿安全规程》条款及现场排查步骤。`;
    onAskDiagnose(q);
  }

  return <div className="view-stack">
    <div className="canvas-subbar">
      <div className="apple-sld-filters" role="tablist" aria-label="画布子视图">
        <button
          type="button"
          role="tab"
          aria-selected={subMode === "sld"}
          className={"apple-filter-pill" + (subMode === "sld" ? " is-active" : "")}
          onClick={() => setSubMode("sld")}
        >
          <span>一次主接线拓扑 (168h 动态潮流)</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={subMode === "findings"}
          className={"apple-filter-pill" + (subMode === "findings" ? " is-active" : "")}
          onClick={() => setSubMode("findings")}
        >
          <span>健康发现与人工复核台账 ({actionable})</span>
        </button>
      </div>

      <div className="canvas-compact-metrics">
        <span className="compact-metric-pill">
          数据完整 <strong>{formatNum(health.scope.completeness.valid)}</strong>
        </span>
        <span className="compact-metric-pill is-blue">
          降噪 <strong>{legacyRaw != null ? `${legacyRaw}→${actionable}` : actionable}</strong>
        </span>
        <span className="compact-metric-pill is-red">
          高优 <strong>{counts.high}</strong> 项
        </span>
        <span className="compact-metric-pill">
          II−I压差 <strong className="warn-text">+{bus.delta_II_minus_I_kv.toFixed(3)}kV</strong>
        </span>
        <span className="compact-metric-pill">
          复核 <strong>{reviewedCount}/{counts.total}</strong>
        </span>
        <label className="select-pill compact">
          <select value={dayFilter} onChange={e => setDayFilter(e.target.value)} aria-label="体检周期">
            <option value="">整周 7 天</option>
            {dates.map(d => <option key={d} value={d}>{d}</option>)}
          </select>
        </label>
      </div>
    </div>

    {subMode === "sld" ? (
      <SingleLineDiagram
        health={health}
        selectedCab={focusCabinet || (/^\d+$/.test(active?.cabinet || "") ? active.cabinet : "")}
        onSelectCabinet={onOpenCabinet}
        externalReplayIndex={externalReplayIndex}
      />
    ) : <>
      <div className="filter-bar">
        <SegmentedControl label="按级别筛选健康发现" size="sm" value={sevFilter} onChange={setSevFilter}>
          <SegmentedControlItem value="all" label={`全部 (${counts.total})`} />
          <SegmentedControlItem value="high" label={`高优先级 (${counts.high})`} />
          <SegmentedControlItem value="medium" label={`中优先级 (${counts.medium})`} />
          <SegmentedControlItem value="info" label={`口径提示 (${counts.info})`} />
        </SegmentedControl>
      </div>

      <div className="master-detail">
      <Card padding={0} className="list-pane">
        <div className="pane-head">
          <h2>分级健康发现</h2>
          <Badge variant="neutral" label={`${filtered.length} 项`} />
        </div>
        <ul className="finding-list" aria-label="全站健康发现列表">
          {filtered.map(item => {
            const meta = SEVERITY_META[item.severity] || SEVERITY_META.info;
            const rev = reviews[item.id];
            const isSel = active?.id === item.id;
            return <li key={item.id}>
              <button
                type="button"
                className={"finding-row" + (isSel ? " is-selected" : "")}
                onClick={() => setSelectedId(item.id)}
              >
                <div className="finding-row-top">
                  <span className="finding-id-group">
                    <StatusDot variant={meta.dot} label={meta.label} />
                    <code className="mono">{item.id}</code>
                    <Badge variant="neutral" label={DIMENSION_LABELS[item.dimension] || item.dimension} />
                  </span>
                  {rev && rev.disposition !== "pending" && (
                    <Badge variant={DISPOSITION_META[rev.disposition]?.badge || "neutral"} label={rev.disposition_label} />
                  )}
                </div>
                <strong className="finding-title">{item.title}</strong>
                <span className="finding-cab">涉及对象：{item.cabinet} · {item.label}</span>
              </button>
            </li>;
          })}
        </ul>
      </Card>

      <Card padding={0} className="detail-pane">
        {active ? <div className="detail-content">
          <div className="detail-top">
            <div className="detail-badges">
              <Badge variant={SEVERITY_META[active.severity]?.badge || "neutral"} label={SEVERITY_META[active.severity]?.label} />
              <Badge variant="teal" label={DIMENSION_LABELS[active.dimension] || active.dimension} />
              <code className="mono">{active.id}</code>
            </div>
            <div className="detail-actions">
              {/^\d+$/.test(active.cabinet) && (
                <Button size="sm" variant="secondary" label={`查看 ${active.cabinet} 柜曲线`}
                  onClick={() => onOpenCabinet(active.cabinet)} />
              )}
              <Button size="sm" variant="primary" label="一键 AI 深度诊断"
                onClick={() => triggerAiFor(active)} />
            </div>
          </div>

          <h2 className="detail-title">{active.title}</h2>
          <p className="detail-summary">{active.summary}</p>

          <div className="detail-block">
            <h4>原始日报单元格出处（点击下载核对原表）</h4>
            <div className="evidence-chips">
              {(active.evidence || []).map((ev, i) => <a
                key={`${ev.file}-${ev.cell}-${i}`}
                className="evidence-chip"
                href={sourceUrl(ev.file)}
                download
              >
                <span className="mono">{ev.time?.slice(0, 16)}</span>
                <strong>{ev.file} · {ev.sheet}!{ev.cell}</strong>
                <small>读数 {formatNum(ev.value)}</small>
              </a>)}
            </div>
          </div>

          <div className="detail-block action-box">
            <h4>建议现场排查动作</h4>
            <p>{active.action}</p>
          </div>

          <ReviewForm
            finding={active}
            currentReview={reviews[active.id]}
            scopeDay={health?.scope?.days?.length === 1 ? health.scope.days[0] : "ALL"}
            onSaved={onReviewsUpdate}
          />
        </div> : <div className="empty-pane">请选择左侧任意健康发现查看详细证据与人工复核表单。</div>}
      </Card>
    </div>

      <Card padding={18} className="missing-card">
        <h3>当前缺失现场证据清单（按证据不足登记）</h3>
        <ol className="missing-list">
          {(health.missing_field_evidence || []).map((line, idx) => <li key={idx}>{line}</li>)}
        </ol>
      </Card>
    </>}
  </div>;
}
