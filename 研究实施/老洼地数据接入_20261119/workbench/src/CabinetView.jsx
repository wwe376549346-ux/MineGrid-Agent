import React, {useEffect, useMemo, useState} from "react";
import {Badge} from "@astryxdesign/core/Badge";
import {Button} from "@astryxdesign/core/Button";
import {Card} from "@astryxdesign/core/Card";
import {SegmentedControl, SegmentedControlItem} from "@astryxdesign/core/SegmentedControl";
import {Spinner} from "@astryxdesign/core/Spinner";
import {StatusDot} from "@astryxdesign/core/StatusDot";
import {TextInput} from "@astryxdesign/core/TextInput";
import {apiRequest, formatNum, SEVERITY_META, sourceUrl} from "./api.js";
import {CabinetChart} from "./CabinetChart.jsx";

const GROUP_LABELS = {
  "35kV": "35 kV 进线与主变高压侧",
  "10kV_I": "10 kV I段母线（1# 主变侧）",
  "10kV_II": "10 kV II段母线（2# 主变侧）",
  spare: "备用柜",
};

export function CabinetView({health, initialCabinet, onAskDiagnose}) {
  const cards = health.cabinets_cards || [];
  const [query, setQuery] = useState("");
  const [groupFilter, setGroupFilter] = useState("all");
  const [selectedCab, setSelectedCab] = useState(initialCabinet || "1018");
  const [metric, setMetric] = useState("P");
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (initialCabinet) setSelectedCab(initialCabinet);
  }, [initialCabinet]);

  useEffect(() => {
    if (!selectedCab) return;
    const ctrl = new AbortController();
    setLoading(true);
    setErr("");
    apiRequest("/api/cabinet?cabinet=" + encodeURIComponent(selectedCab), {signal: ctrl.signal})
      .then(res => setDetail(res))
      .catch(error => {
        if (error.name !== "AbortError") setErr(error.message);
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoading(false);
      });
    return () => ctrl.abort();
  }, [selectedCab]);

  const filteredCards = useMemo(() => {
    const q = query.trim().toLowerCase();
    return cards.filter(c => {
      if (groupFilter !== "all" && c.group !== groupFilter) return false;
      if (q && !c.cabinet.includes(q) && !c.label.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [cards, groupFilter, query]);

  const grouped = useMemo(() => {
    const order = ["35kV", "10kV_I", "10kV_II", "spare"];
    return order
      .map(key => ({key, title: GROUP_LABELS[key], items: filteredCards.filter(c => c.group === key)}))
      .filter(g => g.items.length > 0);
  }, [filteredCards]);

  return <div className="view-stack">
    <div className="view-header">
      <div>
        <p className="eyebrow">35 kV 地面变电所 · 全量台账与量测</p>
        <h1>25 柜电气画像</h1>
      </div>
      <div className="header-controls">
        <TextInput
          label="搜索柜号或名称"
          isLabelHidden
          value={query}
          onChange={setQuery}
          placeholder="搜柜号或设备名（如 1018、井下）"
          width="240px"
        />
      </div>
    </div>

    <div className="filter-bar">
      <SegmentedControl label="按母线段筛选柜号" size="md" value={groupFilter} onChange={setGroupFilter}>
        <SegmentedControlItem value="all" label={`全部 (${cards.length})`} />
        <SegmentedControlItem value="35kV" label="35 kV" />
        <SegmentedControlItem value="10kV_I" label="10 kV I段" />
        <SegmentedControlItem value="10kV_II" label="10 kV II段" />
        <SegmentedControlItem value="spare" label="备用" />
      </SegmentedControl>
    </div>

    <div className="cabinet-layout">
      <div className="cabinet-catalog">
        {grouped.map(group => <section key={group.key} className="cabinet-group">
          <h3 className="group-heading">{group.title}<span>{group.items.length} 柜</span></h3>
          <div className="cabinet-grid">
            {group.items.map(c => {
              const meta = SEVERITY_META[c.level] || SEVERITY_META.ok;
              const isSel = selectedCab === c.cabinet;
              return <button
                key={c.cabinet}
                type="button"
                className={"cab-tile" + (isSel ? " is-selected" : "") + (c.level === "high" ? " is-alert" : "")}
                onClick={() => setSelectedCab(c.cabinet)}
              >
                <div className="cab-tile-top">
                  <span className="cab-num"><StatusDot variant={meta.dot} label={meta.label} />{c.cabinet}</span>
                  <span className="cab-bus">{c.bus_segment}</span>
                </div>
                <strong className="cab-name">{c.label}</strong>
                <div className="cab-mini-stats">
                  <span>P均值 <b>{formatNum(c.metrics?.P?.avg)}</b> kW</span>
                  <span>cos <b>{c.metrics?.cos?.avg?.toFixed(2) ?? "—"}</b></span>
                </div>
                {c.flags.length > 0 && <div className="cab-flags">
                  {c.flags.slice(0, 2).map(fl => <Badge key={fl} variant={meta.badge} label={fl} />)}
                </div>}
              </button>;
            })}
          </div>
        </section>)}
      </div>

      <Card padding={0} className="cabinet-inspector">
        {loading && <div className="center-state"><Spinner size="lg" /><span>正在读取 {selectedCab} 柜 7 天量测…</span></div>}
        {!loading && err && <div className="center-state"><p role="alert">{err}</p></div>}
        {!loading && !err && detail && <div className="inspector-body">
          <div className="inspector-head">
            <div>
              <div className="detail-badges">
                <Badge variant="teal" label={`${detail.device.number} 柜`} />
                {detail.voltage_check?.bus_segment && <Badge variant="neutral" label={`10 kV ${detail.voltage_check.bus_segment}`} />}
                {detail.metering_check?.ratio_median != null && (
                  <Badge variant={detail.metering_check.status === "normal" ? "success" : "warning"}
                    label={`P/(√3UIcos) = ${detail.metering_check.ratio_median.toFixed(3)}`} />
                )}
              </div>
              <h2>{detail.device.label}</h2>
              <p className="muted">{detail.device.station_name}</p>
            </div>
            <Button
              size="sm"
              variant="primary"
              label="对该柜发起 AI 诊断"
              onClick={() => onAskDiagnose(`请全面诊断 ${detail.device.number} 柜（${detail.device.label}）近 7 天的量测质量、电压偏差、功率因数与运行健康状况，并给出排查建议。`)}
            />
          </div>

          <div className="metric-tabs">
            <SegmentedControl label="选择曲线指标" size="sm" value={metric} onChange={setMetric}>
              <SegmentedControlItem value="P" label="有功 P (kW)" />
              <SegmentedControlItem value="Uab" label="电压 Uab (kV)" />
              <SegmentedControlItem value="Ia" label="电流 Ia (A)" />
              <SegmentedControlItem value="cos" label="功率因数 cos" />
            </SegmentedControl>
          </div>

          {detail.series?.[metric]?.length > 0 && (
            <CabinetChart cabinet={detail.device.number} metric={metric} points={detail.series[metric]} />
          )}

          <div className="metric-cards">
            {["P", "Uab", "Ia", "cos"].map(m => {
              const st = detail.metrics_7d?.[m];
              if (!st) return null;
              return <div key={m} className="m-card">
                <span className="m-name">{m} {st.unit ? `(${st.unit})` : ""}</span>
                <strong className="m-avg">均值 {formatNum(st.avg)}</strong>
                <span className="m-range">{formatNum(st.min)} ~ {formatNum(st.max)}</span>
                <div className="m-links">
                  {st.max_cell && <a href={sourceUrl(st.max_cell.split(" / ")[0])} download>峰值 {st.max_cell.split("!")[1]}</a>}
                  {st.negative_hours > 0 && <span className="neg-tag">负值 {st.negative_hours}h</span>}
                </div>
              </div>;
            })}
          </div>

          {detail.metering_check && <div className="detail-block">
            <h4>三相量测物理自洽说明</h4>
            <p>{detail.metering_check.note}</p>
          </div>}

          {(detail.supply_links?.upstream?.length > 0 || detail.supply_links?.downstream?.length > 0) && (
            <div className="detail-block">
              <h4>供电图拓扑连接</h4>
              <ul className="plain-list">
                {detail.supply_links.upstream.map((l, i) => <li key={"u" + i}>上游：{l.from.station_name} {l.from.number} 柜（{l.from.label}）— {l.label}</li>)}
                {detail.supply_links.downstream.map((l, i) => <li key={"d" + i}>下游：{l.to.station_name} {l.to.number} 柜（{l.to.label}）— {l.label}</li>)}
              </ul>
            </div>
          )}

          {detail.related_findings?.length > 0 && <div className="detail-block">
            <h4>该柜关联健康发现（{detail.related_findings.length} 项）</h4>
            <ul className="related-list">
              {detail.related_findings.map(f => <li key={f.id}>
                <Badge variant={SEVERITY_META[f.severity]?.badge || "neutral"} label={SEVERITY_META[f.severity]?.label} />
                <div>
                  <strong>{f.title}</strong>
                  <p>{f.summary}</p>
                </div>
              </li>)}
            </ul>
          </div>}
        </div>}
      </Card>
    </div>
  </div>;
}
