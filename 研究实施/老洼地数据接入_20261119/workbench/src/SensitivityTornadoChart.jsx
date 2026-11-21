import React, {useId, useState} from "react";

const METRICS = {
  voltage: {
    label: "10 kV 母线电压偏移 ΔU (kV)",
    low: "low_delta_u_kv",
    high: "high_delta_u_kv",
    precision: 3,
    minimumSpan: 0.001,
  },
  current: {
    label: "N-1 受电电流偏移 ΔI (A)",
    low: "low_delta_n1_i_a",
    high: "high_delta_n1_i_a",
    precision: 2,
    minimumSpan: 0.01,
  },
  score: {label: "综合信息价值 VoI 评分 (0–100)", precision: 1},
};
const LEFT = 18;
const RIGHT = 442;
const TOP = 31;
const ROW_HEIGHT = 65;
const LOW_COLOR = "var(--apple-blue)";
const HIGH_COLOR = "var(--warn)";
const TEXT = "var(--ink)";
const MUTED = "var(--sub)";

const format = (value, digits = 0) => Number.isFinite(value) ? value.toFixed(digits) : "—";
const signed = (value, digits) => Number.isFinite(value) && value > 0 ? `+${format(value, digits)}` : format(value, digits);

function SensitivityBar({start, end, y, color}) {
  if (start === end) return <circle cx={start} cy={y + 3.5} r="2.5" fill={color} />;
  return <rect x={Math.min(start, end)} y={y} width={Math.abs(end - start)} height="7" rx="2" fill={color} />;
}

export function SensitivityTornadoChart({items}) {
  const [metric, setMetric] = useState("voltage");
  const titleId = useId();
  const rows = Array.isArray(items)
    ? items.filter(item => item?.param_key && Number.isFinite(item.voi_score))
      .sort((a, b) => b.voi_score - a.voi_score || a.priority_rank - b.priority_rank)
    : [];
  if (rows.length === 0) return null;

  const view = METRICS[metric];
  const isScore = metric === "score";
  const endpointDeltas = rows.flatMap(row => [row[view.low], row[view.high]]).filter(Number.isFinite);
  const span = isScore ? 100 : Math.max(view.minimumSpan, ...endpointDeltas.map(Math.abs));
  const ticks = isScore ? [0, 25, 50, 75, 100] : [-span, 0, span];
  const height = TOP + rows.length * ROW_HEIGHT + 13;
  const x = value => LEFT + (isScore ? value / span : (value + span) / (2 * span)) * (RIGHT - LEFT);

  return (
    <figure style={{margin: "12px 0", minWidth: 0}}>
      <figcaption id={titleId}><strong>参数灵敏度龙卷风图</strong></figcaption>
      <label className="pf-field" style={{margin: "8px 0"}}>
        <span>指标视角</span>
        <select className="pf-select" value={metric} onChange={e => setMetric(e.target.value)}>
          {Object.entries(METRICS).map(([key, option]) => <option key={key} value={key}>{option.label}</option>)}
        </select>
      </label>
      <p className="muted" style={{margin: "0 0 8px"}}>
        {isScore ? "VoI 为 0–100 的单一评分。" : <>
          <span style={{color: LOW_COLOR}}>蓝色为低值扰动</span>{" · "}
          <span style={{color: HIGH_COLOR}}>橙色为高值扰动</span>{" · "}左右位置表示相对基线的偏移方向。
        </>}
      </p>
      <svg
        viewBox={`0 0 460 ${height}`}
        role="img"
        aria-labelledby={titleId}
        aria-describedby={`${titleId}-metric`}
        style={{display: "block", width: "100%", maxWidth: 700, height: "auto", background: "var(--surface-subtle)", borderRadius: 8}}
      >
        <desc id={`${titleId}-metric`}>{view.label}</desc>
        {ticks.map((value, index) => (
          <g key={value}>
            <line
              x1={x(value)} x2={x(value)} y1={TOP} y2={height - 7}
              stroke={value === 0 ? MUTED : "var(--border-strong)"}
              strokeDasharray={value === 0 ? undefined : "2 4"}
            />
            <text
              x={x(value)} y="22" fill={value === 0 ? TEXT : MUTED} fontSize="11"
              textAnchor={index === 0 ? "start" : index === ticks.length - 1 ? "end" : "middle"}
            >
              {value === 0 ? "0" : format(value, view.precision)}
            </text>
          </g>
        ))}
        {rows.map((row, index) => {
          const top = TOP + ROW_HEIGHT * index;
          const inputUnit = row.unit ? ` ${row.unit}` : "";
          const low = row[view.low];
          const high = row[view.high];
          return (
            <g key={row.param_key}>
              <title>{`${row.label}（${row.perturbation_label ?? "扰动区间"}）｜基线 ${row.base_value}${inputUnit}｜低值 ${row.low_value}${inputUnit} → 高值 ${row.high_value}${inputUnit}｜${view.label}${isScore ? ` ${row.voi_score}` : ` 低值 ${signed(low, view.precision)} / 高值 ${signed(high, view.precision)}`}｜待索要现场资料 ${row.required_field_document || "—"}`}</title>
              <rect x="0" y={top} width="460" height={ROW_HEIGHT} fill="transparent" />
              <text x={LEFT} y={top + 16} fill={TEXT} fontSize="13" fontWeight="650">
                {row.priority_rank ?? index + 1}. {row.label}
              </text>
              {isScore
                ? <SensitivityBar start={x(0)} end={x(row.voi_score)} y={top + 29} color={LOW_COLOR} />
                : <>
                    {Number.isFinite(low) && <SensitivityBar start={x(0)} end={x(low)} y={top + 23} color={LOW_COLOR} />}
                    {Number.isFinite(high) && <SensitivityBar start={x(0)} end={x(high)} y={top + 33} color={HIGH_COLOR} />}
                  </>}
              <text x={LEFT} y={top + 55} fill={MUTED} fontSize="11">
                低值 {row.low_value ?? "—"}{inputUnit}{!isScore && `（${signed(low, view.precision)}）`}
              </text>
              <text x={RIGHT} y={top + 55} textAnchor="end" fill={MUTED} fontSize="11">
                高值 {row.high_value ?? "—"}{inputUnit}{!isScore && `（${signed(high, view.precision)}）`}
                {isScore && ` · VoI ${format(row.voi_score, view.precision)}`}
              </text>
              <line x1={LEFT} x2={RIGHT} y1={top + ROW_HEIGHT} y2={top + ROW_HEIGHT} stroke="var(--border)" />
            </g>
          );
        })}
      </svg>
    </figure>
  );
}
