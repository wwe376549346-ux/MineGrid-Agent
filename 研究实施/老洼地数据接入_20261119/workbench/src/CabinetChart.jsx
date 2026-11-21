import React, {useEffect, useRef, useState} from "react";
import {FileSpreadsheet} from "lucide-react";
import {formatNum, sourceUrl} from "./api.js";

const METRIC_META = {
  P: {label: "有功功率 P", unit: "kW", color: "#0071e3", darkColor: "#0a84ff"},
  Uab: {label: "线电压 Uab", unit: "kV", color: "#5856d6", darkColor: "#5e5ce6"},
  Ia: {label: "A 相电流 Ia", unit: "A", color: "#00a39b", darkColor: "#64d2ff"},
  cos: {label: "功率因数 cosφ", unit: "", color: "#ff9500", darkColor: "#ff9f0a"},
};

const PAD = {left: 56, right: 18, top: 22, bottom: 34};

function getThresholds(cabinet, metric) {
  const num = Number(cabinet);
  if (metric === "Uab") {
    if (num < 1000) return [{value: 37.45, label: "+7% 关注线 37.45 kV", color: "#ff9500"}];
    return [
      {value: 10.7, label: "GB/T 12325 +7% (10.70 kV)", color: "#ff3b30"},
      {value: 9.3, label: "-7% (9.30 kV)", color: "#ff3b30"},
    ];
  }
  if (metric === "cos" && cabinet !== "1011" && cabinet !== "1012") {
    return [
      {value: 0.9, label: "cosφ 0.90 优良线", color: "#28cd41"},
      {value: 0.8, label: "cosφ 0.80 考核线", color: "#ff9500"},
    ];
  }
  if (metric === "P") {
    return [{value: 0, label: "0 kW 零线", color: "#8e8e93"}];
  }
  return [];
}

export function CabinetChart({cabinet, metric, points}) {
  const canvasRef = useRef(null);
  const [hoverIdx, setHoverIdx] = useState(null);
  const [isDark, setIsDark] = useState(() => document.documentElement.dataset.theme === "dark");
  const meta = METRIC_META[metric] || METRIC_META.P;
  const thresholds = getThresholds(cabinet, metric);

  useEffect(() => {
    const obs = new MutationObserver(() => {
      setIsDark(document.documentElement.dataset.theme === "dark");
    });
    obs.observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});
    return () => obs.disconnect();
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !points.length) return;
    const lineColor = isDark ? meta.darkColor : meta.color;
    const gridColor = isDark ? "rgba(255, 255, 255, 0.07)" : "rgba(0, 0, 0, 0.055)";
    const subGridColor = isDark ? "rgba(255, 255, 255, 0.05)" : "rgba(0, 0, 0, 0.04)";
    const textColor = isDark ? "#98989d" : "#86868b";

    const draw = () => {
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      if (!width || !height) return;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      const ctx = canvas.getContext("2d");
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, width, height);

      const vals = points.map(p => p.value).filter(v => v !== null);
      if (!vals.length) return;
      const refVals = thresholds.map(t => t.value).filter(v => {
        if (metric === "Uab" && Number(cabinet) >= 1000 && v < 9.5 && Math.min(...vals) > 9.8) return false;
        return true;
      });
      let minV = Math.min(...vals, ...refVals);
      let maxV = Math.max(...vals, ...refVals);
      if (maxV === minV) {
        minV -= 1;
        maxV += 1;
      }
      const padV = (maxV - minV) * 0.11;
      minV -= padV;
      maxV += padV;

      const plotW = width - PAD.left - PAD.right;
      const plotH = height - PAD.top - PAD.bottom;
      const xOf = idx => PAD.left + (points.length <= 1 ? 0 : (idx / (points.length - 1)) * plotW);
      const yOf = val => PAD.top + ((maxV - val) / (maxV - minV)) * plotH;

      // Horizontal Grid Lines
      ctx.font = '11px "SF Mono", ui-monospace, SFMono-Regular, Menlo, monospace';
      ctx.textBaseline = "middle";
      const ticks = 4;
      for (let i = 0; i <= ticks; i++) {
        const val = minV + ((maxV - minV) * i) / ticks;
        const y = yOf(val);
        ctx.strokeStyle = gridColor;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(PAD.left, y);
        ctx.lineTo(width - PAD.right, y);
        ctx.stroke();
        ctx.fillStyle = textColor;
        ctx.textAlign = "right";
        ctx.fillText(formatNum(Number(val.toFixed(2))), PAD.left - 8, y);
      }

      // Vertical Day Separators
      ctx.textAlign = "center";
      for (let i = 0; i < points.length; i++) {
        if (points[i].time.endsWith("00:00:00")) {
          const x = xOf(i);
          ctx.strokeStyle = subGridColor;
          ctx.beginPath();
          ctx.moveTo(x, PAD.top);
          ctx.lineTo(x, height - PAD.bottom);
          ctx.stroke();
          ctx.fillStyle = textColor;
          ctx.fillText(points[i].time.slice(5, 10), x + (points.length > 24 ? 22 : 0), height - 14);
        }
      }

      // Threshold Dashed Lines
      for (const th of thresholds) {
        if (th.value < minV || th.value > maxV) continue;
        const y = yOf(th.value);
        ctx.save();
        ctx.setLineDash([5, 4]);
        ctx.strokeStyle = th.color;
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(PAD.left, y);
        ctx.lineTo(width - PAD.right, y);
        ctx.stroke();
        ctx.fillStyle = th.color;
        ctx.textAlign = "right";
        ctx.fillText(th.label, width - PAD.right - 4, y - 8);
        ctx.restore();
      }

      // Apple Stocks Style Vertical Area Gradient
      ctx.save();
      ctx.beginPath();
      let started = false;
      let firstX = PAD.left;
      let lastX = PAD.left;
      points.forEach((p, idx) => {
        if (p.value === null) return;
        const x = xOf(idx);
        const y = yOf(p.value);
        if (!started) {
          ctx.moveTo(x, y);
          firstX = x;
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
        lastX = x;
      });
      if (started) {
        ctx.lineTo(lastX, height - PAD.bottom);
        ctx.lineTo(firstX, height - PAD.bottom);
        ctx.closePath();
        const grad = ctx.createLinearGradient(0, PAD.top, 0, height - PAD.bottom);
        grad.addColorStop(0, lineColor + (isDark ? "3d" : "2b"));
        grad.addColorStop(1, lineColor + "02");
        ctx.fillStyle = grad;
        ctx.fill();
      }
      ctx.restore();

      // Main Telemetry Curve
      ctx.strokeStyle = lineColor;
      ctx.lineWidth = 2.15;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.beginPath();
      started = false;
      points.forEach((p, idx) => {
        if (p.value === null) {
          started = false;
          return;
        }
        const x = xOf(idx);
        const y = yOf(p.value);
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      });
      ctx.stroke();

      // Anomalous Points Dots
      points.forEach((p, idx) => {
        if (p.value === null) return;
        const isAlert =
          (metric === "P" && p.value < 0) ||
          (metric === "Uab" && Number(cabinet) >= 1000 && p.value > 10.7) ||
          (metric === "cos" && p.value < 0);
        if (isAlert) {
          ctx.fillStyle = "#ff3b30";
          ctx.beginPath();
          ctx.arc(xOf(idx), yOf(p.value), 2.7, 0, Math.PI * 2);
          ctx.fill();
        }
      });

      // Apple Crosshair Hover Cursor
      if (hoverIdx !== null && points[hoverIdx] && points[hoverIdx].value !== null) {
        const pt = points[hoverIdx];
        const x = xOf(hoverIdx);
        const y = yOf(pt.value);
        ctx.save();
        ctx.setLineDash([3, 3]);
        ctx.strokeStyle = isDark ? "rgba(255, 255, 255, 0.35)" : "rgba(0, 0, 0, 0.25)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, PAD.top);
        ctx.lineTo(x, height - PAD.bottom);
        ctx.stroke();

        ctx.setLineDash([]);
        ctx.fillStyle = lineColor + "33";
        ctx.beginPath();
        ctx.arc(x, y, 9, 0, Math.PI * 2);
        ctx.fill();

        ctx.fillStyle = isDark ? "#16161a" : "#ffffff";
        ctx.strokeStyle = lineColor;
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        ctx.arc(x, y, 4.5, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
        ctx.restore();
      }
    };

    draw();
    window.addEventListener("resize", draw);
    return () => window.removeEventListener("resize", draw);
  }, [cabinet, metric, points, hoverIdx, thresholds, meta, isDark]);

  function handleMove(event) {
    if (!points.length) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const plotW = rect.width - PAD.left - PAD.right;
    if (plotW <= 0) return;
    const rel = Math.max(0, Math.min(1, (event.clientX - rect.left - PAD.left) / plotW));
    const idx = Math.round(rel * (points.length - 1));
    setHoverIdx(idx);
  }

  const activePt = hoverIdx !== null && points[hoverIdx] ? points[hoverIdx] : points[points.length - 1];

  return <div className="cab-chart-box">
    <div className="cab-chart-bar">
      <div className="cab-chart-title">
        <strong>{meta.label}</strong>
        <span>{points.length} 个整点连续轨迹</span>
      </div>
      {activePt && (
        <div className="cab-chart-readout">
          <span className="mono">{activePt.time.slice(0, 16)}</span>
          <strong className="mono">
            {formatNum(activePt.value)} {meta.unit}
          </strong>
          {activePt.file && activePt.cell && (
            <a className="cell-chip" href={sourceUrl(activePt.file)} download title="点击下载原始日报核对单元格">
              <FileSpreadsheet size={11} style={{marginRight: 4}} />
              {activePt.file} · {activePt.sheet}!{activePt.cell}
            </a>
          )}
        </div>
      )}
    </div>
    <canvas
      ref={canvasRef}
      className="cab-canvas"
      onMouseMove={handleMove}
      onMouseLeave={() => setHoverIdx(null)}
      aria-label={`${cabinet} 柜 ${meta.label} 168小时趋势图`}
    />
  </div>;
}
