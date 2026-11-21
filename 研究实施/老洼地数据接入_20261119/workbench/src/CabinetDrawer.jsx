import React, {useEffect, useState} from "react";
import {Badge} from "@astryxdesign/core/Badge";
import {Button} from "@astryxdesign/core/Button";
import {Collapsible} from "@astryxdesign/core/Collapsible";
import {Drawer, DrawerHeader} from "@astryxdesign/core/Drawer";
import {ProgressBar} from "@astryxdesign/core/ProgressBar";
import {SegmentedControl, SegmentedControlItem} from "@astryxdesign/core/SegmentedControl";
import {Spinner} from "@astryxdesign/core/Spinner";
import {apiRequest, formatNum, SEVERITY_META, sourceUrl} from "./api.js";
import {CabinetChart} from "./CabinetChart.jsx";

export function CabinetDrawer({cabinet, onClose, onAskDiagnose}) {
  const [metric, setMetric] = useState("P");
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!cabinet) return;
    const ctrl = new AbortController();
    setLoading(true);
    setErr("");
    apiRequest("/api/cabinet?cabinet=" + encodeURIComponent(cabinet), {signal: ctrl.signal})
      .then(setDetail)
      .catch(e => {
        if (e.name !== "AbortError") setErr(e.message);
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoading(false);
      });
    return () => ctrl.abort();
  }, [cabinet]);

  const vCheck = detail?.voltage_check;
  const pfCheck = detail?.power_factor_check;
  const mCheck = detail?.metering_check;

  const voltPassPct = vCheck
    ? Math.round(((vCheck.total_hours - vCheck.over_7pct_hours - vCheck.under_7pct_hours) / Math.max(vCheck.total_hours, 1)) * 100)
    : 100;
  const pfPassPct = pfCheck
    ? Math.round(((pfCheck.loaded_hours - pfCheck.below_090_hours) / Math.max(pfCheck.loaded_hours, 1)) * 100)
    : null;

  return <Drawer
    isOpen={Boolean(cabinet)}
    onOpenChange={open => !open && onClose()}
    width="660px"
    label={detail ? `${detail.device.number} 柜 ${detail.device.label}` : "开关柜电气画像"}
  >
    <DrawerHeader
      title={detail ? `${detail.device.number} 柜 · ${detail.device.label}` : `${cabinet || ""} 柜电气详情`}
      subtitle={detail ? `${detail.device.station_name}${vCheck?.bus_segment ? ` · 10 kV ${vCheck.bus_segment}` : ""}` : "正在读取台账…"}
      onOpenChange={open => !open && onClose()}
      hasDivider
      endContent={
        detail && <Button
          size="sm"
          variant="primary"
          label="AI 深度诊断该柜"
          onClick={() => {
            const q = `请全面诊断 ${detail.device.number} 柜（${detail.device.label}）近 7 天的量测质量、电压偏差、功率因数与运行健康状况，并给出排查建议。`;
            onClose();
            onAskDiagnose(q);
          }}
        />
      }
    />

    <div className="drawer-body">
      {loading && <div className="center-state"><Spinner size="lg" /><span>正在加载 {cabinet} 柜 168 整点量测…</span></div>}
      {!loading && err && <div className="center-state"><p className="save-err" role="alert">{err}</p></div>}
      {!loading && !err && detail && <>
        {!detail.has_hourly_readings ? (
          <div className="detail-block action-box">
            <h4>井下 1535 中央变电所拓扑台账柜</h4>
            <p>{detail.note}</p>
            {(detail.supply_links?.upstream?.length > 0 || detail.supply_links?.downstream?.length > 0) && (
              <ul className="plain-list">
                {detail.supply_links.upstream.map((l, i) => (
                  <li key={"u" + i}>上游电源：{l.from.station_name} {l.from.number} 柜（{l.from.label}）— {l.label}</li>
                ))}
                {detail.supply_links.downstream.map((l, i) => (
                  <li key={"d" + i}>下游负荷：{l.to.station_name} {l.to.number} 柜（{l.to.label}）— {l.label}</li>
                ))}
              </ul>
            )}
          </div>
        ) : <>
          <div className="drawer-gauges">
            {vCheck && (
              <div className="gauge-item">
                <ProgressBar
                  value={voltPassPct}
                  max={100}
                  label={`电压合格率（峰值 ${vCheck.max_kv} kV / +${vCheck.max_dev_pct}%，越 +7% 共 ${vCheck.over_7pct_hours}h）`}
                  hasValueLabel
                  variant={voltPassPct >= 90 ? "success" : voltPassPct >= 65 ? "warning" : "error"}
                />
              </div>
            )}
            {pfCheck && pfPassPct !== null && (
              <div className="gauge-item">
                <ProgressBar
                  value={pfPassPct}
                  max={100}
                  label={`功率因数 ≥0.90 达标率（带载均值 cos=${pfCheck.avg_cos}，最低 ${pfCheck.min_cos}）`}
                  hasValueLabel
                  variant={pfCheck.avg_cos >= 0.9 ? "success" : pfCheck.avg_cos >= 0.8 ? "warning" : "error"}
                />
              </div>
            )}
          </div>

          <SegmentedControl label="切换四项电气量整点曲线" size="sm" layout="fill" value={metric} onChange={setMetric}>
            <SegmentedControlItem value="P" label="有功功率 P (kW)" />
            <SegmentedControlItem value="Uab" label="线电压 Uab (kV)" />
            <SegmentedControlItem value="Ia" label="A相电流 Ia (A)" />
            <SegmentedControlItem value="cos" label="功率因数 cos" />
          </SegmentedControl>

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
                  {st.min_cell && <a href={sourceUrl(st.min_cell.split(" / ")[0])} download>谷值 {st.min_cell.split("!")[1]}</a>}
                  {st.negative_hours > 0 && <span className="neg-tag">负值 {st.negative_hours}h</span>}
                </div>
              </div>;
            })}
          </div>

          {mCheck && (
            <div className="detail-block">
              <div className="detail-badges">
                <h4>三相功率物理自洽校核</h4>
                {mCheck.ratio_median != null && (
                  <Badge
                    variant={mCheck.status === "normal" ? "success" : "warning"}
                    label={`P / (√3·Uab·Ia·cos) 中位数 = ${mCheck.ratio_median.toFixed(4)}`}
                  />
                )}
              </div>
              <p>{mCheck.note}</p>
            </div>
          )}

          {(detail.supply_links?.upstream?.length > 0 || detail.supply_links?.downstream?.length > 0) && (
            <div className="detail-block">
              <h4>供电拓扑连接</h4>
              <ul className="plain-list">
                {detail.supply_links.upstream.map((l, i) => (
                  <li key={"u" + i}>上游：{l.from.station_name} {l.from.number} 柜（{l.from.label}）— {l.label}</li>
                ))}
                {detail.supply_links.downstream.map((l, i) => (
                  <li key={"d" + i}>下游：{l.to.station_name} {l.to.number} 柜（{l.to.label}）— {l.label}</li>
                ))}
              </ul>
            </div>
          )}

          {detail.related_findings?.length > 0 && (
            <Collapsible
              defaultIsOpen
              trigger={<strong>该柜关联健康体检发现（{detail.related_findings.length} 项）</strong>}
            >
              <ul className="related-list">
                {detail.related_findings.map(f => (
                  <li key={f.id}>
                    <Badge variant={SEVERITY_META[f.severity]?.badge || "neutral"} label={SEVERITY_META[f.severity]?.label} />
                    <div>
                      <strong>{f.id} · {f.title}</strong>
                      <p>{f.summary}</p>
                    </div>
                  </li>
                ))}
              </ul>
            </Collapsible>
          )}
        </>}
      </>}
    </div>
  </Drawer>;
}
