import React, {useId} from "react";

const WIDTH = 456;
const HEIGHT = 204;
const LEFT = 43;
const RIGHT = 388;
const TOP = 30;
const BOTTOM = 174;
const HOUR_WIDTH = (RIGHT - LEFT) / 24;
const BLUE = "var(--apple-blue)";
const INK = "var(--ink)";
const MUTED = "var(--sub)";
const RED = "var(--danger)";
const ORANGE = "var(--warn)";
const BASELINE = "var(--sub)";
const UPPER_KV = 10.70;
const LOWER_KV = 9.30;
const CAP_MAX = 400;
const HOUR_TICKS = [0, 4, 7, 12, 16, 20, 23];
const ACTION_STYLES = {
  tap_change: {label: "调档", glyph: "调", color: BLUE, y: 10},
  cap_switch: {label: "投切", glyph: "投", color: ORANGE, y: 24},
};
const FIGURE_STYLE = {margin: 0, minWidth: 0, border: "1px solid var(--border)", borderRadius: 10, padding: 10};
const SVG_STYLE = {display: "block", width: "100%", height: "auto", background: "var(--surface-subtle)", borderRadius: 6};

const x = hour => LEFT + (hour + 0.5) * HOUR_WIDTH;
const clock = hour => `${String(hour).padStart(2, "0")}:00`;

function hourIndex(hour) {
  if (Number.isInteger(hour)) return hour >= 0 && hour < 24 ? hour : null;
  const match = typeof hour === "string" && hour.match(/(?:^|[ T])([01]\d|2[0-3]):00(?::00)?$/);
  return match ? Number(match[1]) : null;
}

function verticalScale(values, minimum, maximum, padding) {
  const numbers = values.filter(Number.isFinite);
  const low = Math.min(minimum, ...numbers) - padding;
  const high = Math.max(maximum, ...numbers) + padding;
  return value => BOTTOM - (value - low) / (high - low) * (BOTTOM - TOP);
}

function Axes({y, ticks, unit}) {
  return (
    <g fontSize="11" fill={MUTED}>
      <text x={8} y={17}>{unit}</text>
      {ticks.map(value => (
        <g key={value}>
          <line x1={LEFT} x2={RIGHT} y1={y(value)} y2={y(value)} stroke="var(--border-strong)" />
          <text x={LEFT - 5} y={y(value)} dy=".32em" textAnchor="end">{value}</text>
        </g>
      ))}
      <line x1={LEFT} x2={RIGHT} y1={BOTTOM} y2={BOTTOM} stroke="var(--border-strong)" />
      {HOUR_TICKS.map(hour => (
        <text key={hour} x={x(hour)} y={BOTTOM + 17} textAnchor="middle">{clock(hour)}</text>
      ))}
    </g>
  );
}

function LimitLine({y, label}) {
  return (
    <g fill={RED} fontSize="11">
      <line x1={LEFT} x2={RIGHT} y1={y} y2={y} stroke={RED} strokeDasharray="4 3" />
      <text x={RIGHT + 5} y={y} dy=".32em">{label}</text>
    </g>
  );
}

function Trend({hours, field, label, unit, color, y, dashed = false}) {
  let previousHour = -2;
  const points = hours.filter(row => Number.isFinite(row[field]));
  const path = points.map(row => {
    const command = row.index === previousHour + 1 ? "L" : "M";
    previousHour = row.index;
    return `${command}${x(row.index)} ${y(row[field])}`;
  }).join(" ");
  return (
    <g>
      <path d={path} fill="none" stroke={color} strokeWidth={dashed ? 1.6 : 2.2} strokeDasharray={dashed ? "5 4" : undefined} />
      {points.map(row => (
        <circle key={row.index} cx={x(row.index)} cy={y(row[field])} r={dashed ? 1.5 : 2} fill={color}>
          <title>{clock(row.index)} {label} {row[field]} {unit}</title>
        </circle>
      ))}
    </g>
  );
}

function ActionMarkers({actions}) {
  return actions.map(({index, type, from_val, to_val}, pos) => {
    const {label, glyph, color, y} = ACTION_STYLES[type];
    return (
      <g key={`${index}-${type}-${pos}`}>
        <title>{clock(index)} {label} {from_val} → {to_val}</title>
        <line x1={x(index)} x2={x(index)} y1={y + 3} y2={BOTTOM} stroke={color} strokeDasharray="2 3" opacity=".6" />
        <text x={x(index)} y={y} textAnchor="middle" fill={color} fontSize="11" fontWeight="650">{glyph}</text>
      </g>
    );
  });
}

export function DailyScheduleChart({schedule}) {
  const id = useId();
  if (!Array.isArray(schedule?.hours)) return null;
  const byHour = new Map();
  for (const row of schedule.hours) {
    const index = hourIndex(row?.hour);
    if (index !== null) byHour.set(index, {...row, index});
  }
  const hours = [...byHour.values()].sort((a, b) => a.index - b.index);
  const hasVoltage = hours.some(row => Number.isFinite(row.baseline_bus_kv) || Number.isFinite(row.optimized_bus_kv));
  const hasReactive = hours.some(row => Number.isFinite(row.qc_kvar) || Number.isFinite(row.feeder_q_kvar));
  if (!hasVoltage && !hasReactive) return null;

  const actions = (Array.isArray(schedule.actions) ? schedule.actions : [])
    .filter(action => action && Object.hasOwn(ACTION_STYLES, action.type))
    .map(action => ({...action, index: hourIndex(action.hour)}))
    .filter(action => action.index !== null);
  const voltageY = verticalScale(hours.flatMap(row => [row.baseline_bus_kv, row.optimized_bus_kv]), LOWER_KV, UPPER_KV, 0.12);
  const reactiveY = verticalScale(hours.flatMap(row => [row.qc_kvar, row.feeder_q_kvar]), 0, CAP_MAX, 28);
  const earlySwitch = actions.find(action => action.type === "cap_switch" && action.index === 7);
  const seven = byHour.get(7);
  const six = byHour.get(6);
  const showEarlySwitch = earlySwitch && Number.isFinite(seven?.feeder_q_kvar) &&
    Number.isFinite(seven?.qc_kvar) && Number.isFinite(six?.qc_kvar) && seven.qc_kvar < six.qc_kvar;

  return (
    <div style={{display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 350px), 1fr))", gap: 12, margin: "12px 0"}}>
      {hasVoltage && <figure style={FIGURE_STYLE}>
        <figcaption id={`${id}-voltage`}><strong>24 小时 10 kV 母线电压对比</strong></figcaption>
        <p className="muted" style={{margin: "4px 0 8px"}}>虚线基线 · 实线优化后 · 调为调档 · 投为投切</p>
        <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-labelledby={`${id}-voltage`} style={SVG_STYLE}>
          <Axes y={voltageY} ticks={[10]} unit="kV" />
          <LimitLine y={voltageY(UPPER_KV)} label="10.70 kV" />
          <LimitLine y={voltageY(LOWER_KV)} label="9.30 kV" />
          <ActionMarkers actions={actions} />
          <Trend hours={hours} field="baseline_bus_kv" label="基线" unit="kV" color={BASELINE} y={voltageY} dashed />
          <Trend hours={hours} field="optimized_bus_kv" label="优化后" unit="kV" color={BLUE} y={voltageY} />
        </svg>
      </figure>}
      {hasReactive && <figure style={FIGURE_STYLE}>
        <figcaption id={`${id}-reactive`}><strong>24 小时离散投容与馈线无功</strong></figcaption>
        <p className="muted" style={{margin: "4px 0 8px"}}>柱形投容容量 · 折线优化后馈线无功 · 红线防倒送</p>
        <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-labelledby={`${id}-reactive`} style={SVG_STYLE}>
          <Axes y={reactiveY} ticks={[100, 200, 300, CAP_MAX]} unit="kvar" />
          {hours.filter(row => Number.isFinite(row.qc_kvar) && row.qc_kvar >= 0).map(row => (
            <rect
              key={row.index}
              x={x(row.index) - HOUR_WIDTH * .36}
              y={reactiveY(row.qc_kvar)}
              width={HOUR_WIDTH * .72}
              height={reactiveY(0) - reactiveY(row.qc_kvar)}
              fill={BLUE}
              opacity=".38"
            >
              <title>{clock(row.index)} 投容 {row.qc_kvar} kvar</title>
            </rect>
          ))}
          <LimitLine y={reactiveY(0)} label="0 kvar" />
          <Trend hours={hours} field="feeder_q_kvar" label="优化后馈线无功" unit="kvar" color={INK} y={reactiveY} />
        </svg>
        {showEarlySwitch && <p className="muted" style={{margin: "8px 0 0"}}>
          07:00 退容 {six.qc_kvar} → {seven.qc_kvar} kvar，馈线无功 {seven.feeder_q_kvar.toFixed(2)} kvar。
        </p>}
      </figure>}
    </div>
  );
}
