import React, {useEffect, useRef, useState} from "react";
import {Badge} from "@astryxdesign/core/Badge";
import {Card} from "@astryxdesign/core/Card";
import {SegmentedControl, SegmentedControlItem} from "@astryxdesign/core/SegmentedControl";
import {clock} from "./replay.js";

export const METRICS = {
  P: {label: "有功功率 P", unit: "kW"},
  Ia: {label: "A 相电流 Ia", unit: "A"},
  Uab: {label: "线电压 Uab", unit: "kV"},
  cos: {label: "功率因数 cosφ", unit: ""},
};
export const pointFormat = new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 4});
export const format = value => (value == null ? "—" : pointFormat.format(value));

const APPLE_PALETTE = ["#0071e3", "#ff9500", "#28cd41", "#5856d6"];
const labels = {"303": "一回进线", "306": "二回进线", "1009": "井下一回", "1010": "井下二回"};
const hours = [0, 4, 8, 12, 16, 20, 23];
const inset = {left: 58, right: 18, top: 18, bottom: 36};

function gridRange(values) {
  const min = Math.min(0, ...values);
  const max = Math.max(...values, 0);
  const ideal = (max - min || 1) / 4;
  const scale = Math.pow(10, Math.floor(Math.log10(ideal)));
  const step = [1, 2, 2.5, 5, 10].find(multiplier => multiplier * scale >= ideal) * scale;
  return {low: Math.floor(min / step), high: Math.ceil(max / step) || 1, step};
}

function TrendCanvas({series, hour, hover, setHover, selection, metric}) {
  const ref = useRef(null);
  const [isDark, setIsDark] = useState(() => document.documentElement.dataset.theme === "dark");

  useEffect(() => {
    const obs = new MutationObserver(() => {
      setIsDark(document.documentElement.dataset.theme === "dark");
    });
    obs.observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});
    return () => obs.disconnect();
  }, []);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return undefined;
    const gridColor = isDark ? "rgba(255, 255, 255, 0.075)" : "rgba(0, 0, 0, 0.055)";
    const textColor = isDark ? "#98989d" : "#86868b";

    const draw = () => {
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      if (!width || !height) return;
      const pixelRatio = window.devicePixelRatio || 1;
      canvas.width = Math.round(width * pixelRatio);
      canvas.height = Math.round(height * pixelRatio);
      const ctx = canvas.getContext("2d");
      ctx.scale(pixelRatio, pixelRatio);
      ctx.clearRect(0, 0, width, height);

      const x = index => inset.left + (index / 23) * (width - inset.left - inset.right);
      const all = series.filter(row => !selection || selection === row.cabinet);
      const {low, high, step} = gridRange(
        all.flatMap(row => row.hourly[metric].map(item => item.value).filter(value => value !== null))
      );
      const y = value => inset.top + ((high - value / step) / (high - low)) * (height - inset.top - inset.bottom);

      ctx.font = '11px "SF Mono", ui-monospace, SFMono-Regular, Menlo, monospace';
      ctx.textBaseline = "middle";
      ctx.lineWidth = 1;
      for (let tick = low; tick <= high; tick++) {
        const ordinate = y(tick * step);
        ctx.strokeStyle = gridColor;
        ctx.beginPath();
        ctx.moveTo(inset.left, ordinate);
        ctx.lineTo(width - inset.right, ordinate);
        ctx.stroke();
        ctx.fillStyle = textColor;
        ctx.textAlign = "right";
        ctx.fillText(format(tick * step), inset.left - 8, ordinate);
      }

      ctx.textAlign = "center";
      ctx.fillStyle = textColor;
      for (const index of width < 420 ? [0, 6, 12, 18, 23] : hours) {
        ctx.fillText(clock(index), x(index), height - 16);
      }

      const cursor = hover ?? hour;
      ctx.save();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = isDark ? "rgba(255, 255, 255, 0.3)" : "rgba(0, 0, 0, 0.22)";
      ctx.beginPath();
      ctx.moveTo(x(cursor), inset.top);
      ctx.lineTo(x(cursor), height - inset.bottom);
      ctx.stroke();
      ctx.restore();

      for (const row of all) {
        ctx.strokeStyle = row.color;
        ctx.lineWidth = 2.2;
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        let started = false;
        ctx.beginPath();
        row.hourly[metric].forEach(({value}, index) => {
          if (value === null) {
            started = false;
          } else {
            if (started) ctx.lineTo(x(index), y(value));
            else ctx.moveTo(x(index), y(value));
            started = true;
          }
        });
        ctx.stroke();

        const value = row.hourly[metric][cursor].value;
        if (value !== null) {
          ctx.fillStyle = isDark ? "#16161a" : "#ffffff";
          ctx.strokeStyle = row.color;
          ctx.lineWidth = 2.4;
          ctx.beginPath();
          ctx.arc(x(cursor), y(value), 4.2, 0, 2 * Math.PI);
          ctx.fill();
          ctx.stroke();
        }
      }
    };

    const resize = new ResizeObserver(draw);
    resize.observe(canvas);
    draw();
    return () => resize.disconnect();
  }, [series, hour, hover, selection, metric, isDark]);

  return <canvas
    ref={ref}
    className="chart"
    role="img"
    aria-label={METRICS[metric].label + "整点曲线"}
    onPointerMove={event => {
      const bounds = event.currentTarget.getBoundingClientRect();
      const index = Math.round(((event.clientX - bounds.left - inset.left) / (bounds.width - inset.left - inset.right)) * 23);
      setHover(Math.min(23, Math.max(0, index)));
    }}
    onPointerLeave={() => setHover(null)}
  />;
}

export function Trend({report, hour}) {
  const [metric, setMetric] = useState("P");
  const [selection, setSelection] = useState("");
  const [hover, setHover] = useState(null);
  const {label, unit} = METRICS[metric];
  const cabinets = report.cabinets.map((row, index) => ({...row, color: APPLE_PALETTE[index % APPLE_PALETTE.length]}));
  const cursor = hover ?? hour;

  return <Card padding={20} className="trend-panel" aria-label="柜级日况和趋势">
    <div className="trend-head">
      <div>
        <h2>四柜日况与 24 整点趋势</h2>
        <p className="muted">
          {report.hours_with_frame}/24 个整点 · {report.source.filename}
        </p>
      </div>
      <SegmentedControl label="选择量测指标" size="sm" value={metric} onChange={setMetric}>
        {Object.entries(METRICS).map(([id, option]) => (
          <SegmentedControlItem key={id} value={id} label={option.label} />
        ))}
      </SegmentedControl>
    </div>

    <div className="replay-cab-cards">
      {cabinets.map(item => {
        const peak = item.peaks[metric];
        const count = item.valid_samples[metric];
        const selected = selection === item.cabinet;
        return <button
          type="button"
          key={item.cabinet}
          className={"cab-tile" + (selected ? " is-selected" : "")}
          aria-pressed={selected}
          onClick={() => setSelection(selected ? "" : item.cabinet)}
        >
          <div className="cab-tile-top">
            <span className="cab-num">
              <span className="apple-dot" style={{background: item.color}} />
              {item.cabinet} 柜
            </span>
            <span className="cab-bus">{labels[item.cabinet]}</span>
          </div>
          <strong className="mono" style={{fontSize: 15}}>
            {format(peak.value)} <small>{unit}</small>
          </strong>
          <div className="cab-mini-stats">
            <span>{peak.time ? peak.time.slice(11, 16) + " 峰值" : "峰值缺测"}</span>
            <span>{count}/24 整点</span>
          </div>
        </button>;
      })}
    </div>

    <div className="cab-chart-box">
      <div className="cab-chart-bar">
        <div className="cab-chart-title">
          <strong>{label} 24h 曲线</strong>
          <span>点击上方四柜卡片可单独聚焦</span>
        </div>
        <Badge variant="teal" label={`当前时刻 ${clock(cursor)}`} />
      </div>
      <TrendCanvas series={cabinets} hour={hour} hover={hover} setHover={setHover} selection={selection} metric={metric} />
      <div className="replay-legend-row">
        {cabinets
          .filter(item => !selection || selection === item.cabinet)
          .map(item => (
            <span key={item.cabinet} className="replay-legend-item">
              <span className="apple-dot" style={{background: item.color}} />
              <span>{item.cabinet} 柜：</span>
              <strong className="mono">
                {format(item.hourly[metric][cursor].value)} {unit}
              </strong>
            </span>
          ))}
      </div>
    </div>

    <details className="apple-details-table">
      <summary>展开 24 个整点原始单元格明细表</summary>
      <div className="apple-table-scroll">
        <table className="trend-table">
          <thead>
            <tr>
              <th scope="col">时刻</th>
              {cabinets.map(item => (
                <th scope="col" key={item.cabinet}>
                  {item.cabinet} 柜 ({labels[item.cabinet]})
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {cabinets[0].hourly[metric].map((reading, index) => (
              <tr key={reading.time}>
                <th scope="row" className="mono">{clock(index)}</th>
                {cabinets.map(item => {
                  const point = item.hourly[metric][index];
                  return <td key={item.cabinet} className="mono">
                    {format(point.value)}{" "}
                    <small className="muted">{point.source?.cell}</small>
                  </td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  </Card>;
}
