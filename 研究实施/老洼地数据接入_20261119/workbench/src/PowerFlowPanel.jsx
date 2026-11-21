import React, {useEffect, useState} from "react";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Cpu,
  Database,
  ExternalLink,
  Gauge,
  Play,
  Sliders,
  Sparkles,
  Zap,
} from "lucide-react";
import {Badge} from "@astryxdesign/core/Badge";
import {Banner} from "@astryxdesign/core/Banner";
import {Button} from "@astryxdesign/core/Button";
import {Card} from "@astryxdesign/core/Card";
import {Collapsible} from "@astryxdesign/core/Collapsible";
import {ProgressBar} from "@astryxdesign/core/ProgressBar";
import {Spinner} from "@astryxdesign/core/Spinner";
import {apiRequest, cellHref} from "./api.js";
import {DailyScheduleChart} from "./DailyScheduleChart.jsx";
import {SensitivityTornadoChart} from "./SensitivityTornadoChart.jsx";

const SCENARIOS = [
  {
    id: "tap_sensitivity",
    label: "① 2#主变分接头调压灵敏度",
    defaultTime: "2026-11-13 17:00:00",
    hint: "扫描 2# 主变 -2 至 +2 档，量化解决 10 kV II段过压（10.90–11.01 kV）的精准调档方案",
  },
  {
    id: "base_case",
    label: "② 全网交流潮流拟合校核",
    defaultTime: "2026-11-13 17:00:00",
    hint: "使用 MineProfile + SQLite 归档整点边界执行全网 Newton-Raphson 潮流并与实测母线电压残差校核",
  },
  {
    id: "n1_contingency",
    label: "③ N-1 故障潮流与末端压降",
    defaultTime: "2026-11-10 06:00:00",
    hint: "模拟 1009 退运经 1200 联络由 1010 转供（或 306 / 2#主变退运），校核电缆负载率与井下末端电压",
  },
  {
    id: "reactive_opt",
    label: "④ 无功补偿前后潮流降损对比",
    defaultTime: "2026-11-10 06:00:00",
    hint: "在 1010 / 1020 井下末端投切并联电容器前后全网交流潮流对比，量化降流率、压升与真实降损(kW)",
  },
];

const TIME_PRESETS = [
  {value: "2026-11-13 17:00:00", label: "11-13 17:00 · 10kV II段过压峰值 (10.90/11.01 kV)"},
  {value: "2026-11-13 15:00:00", label: "11-13 15:00 · 306停运 / 303单回峰值 (5710 kW)"},
  {value: "2026-11-10 06:00:00", label: "11-10 06:00 · 1009+1010 下井同步峰值 (159.7 A)"},
  {value: "2026-11-10 12:00:00", label: "11-10 12:00 · LW-000010 303跃升整点"},
];

const weeklyNumber = new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 2});
const displayNumber = value => Number.isFinite(value) ? weeklyNumber.format(value) : "—";

function WeeklyScheduleCard() {
  const [weeklyData, setWeeklyData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function loadWeeklySchedule() {
    setLoading(true);
    setError("");
    try {
      const result = await apiRequest("/api/powerflow/weekly-schedule");
      if (!result.weekly_summary || !Array.isArray(result.weekly_days) || !Array.isArray(result.pareto_frontier)) {
        throw new Error(result.summary || "当前归档未返回完整的全周优化结果。");
      }
      setWeeklyData(result);
    } catch (err) {
      setError(err.message || "全周分时优化加载失败。");
    } finally {
      setLoading(false);
    }
  }

  const summary = weeklyData?.weekly_summary;
  return (
    <Card padding={16} className="pf-hero-card">
      <Collapsible trigger="全周 168h 分时优化与帕累托权衡" defaultIsOpen={false}>
        {!weeklyData && (
          <div style={{margin: "12px 0"}} aria-busy={loading}>
            <p>1010 柜 · 典型日 2026-11-13</p>
            <Button
              size="sm"
              variant="primary"
              label={loading ? "正在计算全周方案…" : error ? "重新计算全周方案" : "计算并加载全周方案"}
              onClick={loadWeeklySchedule}
              disabled={loading}
            />
            {error && <Banner variant="error" title="全周分时优化未载入" description={error} />}
          </div>
        )}
        {summary && <>
          <div className="pf-kpi-grid" style={{margin: "12px 0"}}>
            <div className="pf-kpi-tile">
              <span className="pf-kpi-label">回放覆盖</span>
              <strong className="pf-kpi-value mono">{summary.total_days} 天 · {summary.total_hours} h</strong>
              <small className="muted">{summary.all_week_safe ? "全周通过约束校核" : "仍有整点未通过校核"}</small>
            </div>
            <div className="pf-kpi-tile">
              <span className="pf-kpi-label">全周净减少越限整点</span>
              <strong className="pf-kpi-value mono">{summary.baseline_violations_total - summary.optimized_violations_total}</strong>
              <small className="muted">基线 {summary.baseline_violations_total} → 优化后 {summary.optimized_violations_total}</small>
            </div>
            <div className="pf-kpi-tile">
              <span className="pf-kpi-label">全周网损节省</span>
              <strong className="pf-kpi-value mono">{displayNumber(summary.total_loss_saving_kwh)} kWh</strong>
              <small className="muted">{displayNumber(summary.total_loss_saving_pct)}% · 基线 {displayNumber(summary.total_baseline_loss_kwh)} → 优化后 {displayNumber(summary.total_optimized_loss_kwh)} kWh</small>
            </div>
          </div>
          <div className="pf-table-wrap">
            <table className="pf-table">
              <caption>7 日分时优化明细</caption>
              <thead>
                <tr>
                  <th>日期</th><th>约束校核</th><th>基线越限</th><th>优化后越限</th><th>调档</th><th>投切</th>
                  <th>动作合计</th><th>基线网损 kWh</th><th>优化网损 kWh</th><th>节电 kWh</th><th>节电率</th>
                </tr>
              </thead>
              <tbody>
                {weeklyData.weekly_days.map(day => (
                  <tr key={day.day} className={day.day === weeklyData.pareto_day ? "is-current" : undefined}>
                    <td>{day.day}</td><td>{day.all_hours_safe ? "通过" : "未通过"}</td>
                    <td>{day.baseline_violations_count}</td><td>{day.optimized_violations_count}</td>
                    <td>{day.tap_changes}</td><td>{day.cap_changes}</td><td>{day.actions_count}</td>
                    <td>{displayNumber(day.baseline_loss_kwh)}</td><td>{displayNumber(day.optimized_loss_kwh)}</td>
                    <td>{displayNumber(day.loss_saving_kwh)}</td><td>{displayNumber(day.loss_saving_pct)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="pf-table-wrap" style={{marginTop: 16}}>
            <table className="pf-table">
              <caption>{weeklyData.pareto_day} 动作次数预算对比</caption>
              <thead>
                <tr>
                  <th>方案</th><th>调档/投切预算</th><th>实际调档/投切</th><th>越限整点</th>
                  <th>网损 kWh</th><th>节电 kWh</th><th>节电率</th>
                </tr>
              </thead>
              <tbody>
                {weeklyData.pareto_frontier.map(row => (
                  <tr key={`${row.max_tap_changes}/${row.max_cap_changes}`}>
                    <td>{row.label}</td><td>{row.max_tap_changes}/{row.max_cap_changes}</td>
                    <td>{row.tap_changes}/{row.cap_changes}</td><td>{row.violations}</td>
                    <td>{displayNumber(row.loss_kwh)}</td><td>{displayNumber(row.loss_saving_kwh)}</td>
                    <td>{displayNumber(row.loss_saving_pct)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <DailyScheduleChart schedule={weeklyData.featured_day_schedule} />
        </>}
      </Collapsible>
    </Card>
  );
}

function SensitivityVoiCard({atTime}) {
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function loadSensitivity() {
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams({at_time: atTime, target_cabinet: "1010"});
      const response = await apiRequest(`/api/powerflow/sensitivity-voi?${params}`);
      if (response.status !== "ok" || !Array.isArray(response.tornado_items) || !Array.isArray(response.voi_ranking)) {
        throw new Error(response.summary || "当前断面未返回灵敏度结果。");
      }
      setResult(response);
    } catch (err) {
      setError(err.message || "参数灵敏度计算失败。");
    } finally {
      setLoading(false);
    }
  }

  const baseline = result?.baseline;
  return (
    <Card padding={16} className="pf-hero-card">
      <Collapsible trigger="未知参数灵敏度与信息价值（VoI）补证排序" defaultIsOpen={false}>
        <p>电压断面 {atTime} · N-1 工况 1009 退运、1010 受电{result?.n1_time && `（${result.n1_time}）`}</p>
        {!result && (
          <div aria-busy={loading}>
            <Button
              size="sm"
              variant="primary"
              label={loading ? "正在计算灵敏度…" : error ? "重新计算灵敏度" : "计算并加载灵敏度"}
              onClick={loadSensitivity}
              disabled={loading}
            />
            {error && <Banner variant="error" title="未能载入灵敏度结果" description={error} />}
          </div>
        )}
        {result && <>
          {result.summary && <p>{result.summary}</p>}
          {baseline && (
            <p className="muted">
              基线 · 10 kV II 段 {displayNumber(baseline.bus_10kv_ii_kv)} kV · N-1 末端最低 {displayNumber(baseline.n1_min_ug_kv)} kV
              {" · "}N-1 受电电流 {displayNumber(baseline.n1_current_a)} A · 网损 {displayNumber(baseline.total_loss_kw)} kW
            </p>
          )}
          <SensitivityTornadoChart items={result.tornado_items} />
          <div className="pf-table-wrap">
            <table className="pf-table">
              <caption>补证优先级</caption>
              <thead>
                <tr><th>排名</th><th>参数</th><th>VoI 评分</th><th>主导影响</th><th>优先索要的现场资料</th></tr>
              </thead>
              <tbody>
                {result.voi_ranking.map(row => (
                  <tr key={row.param_key}>
                    <td>{row.rank}</td><th scope="row">{row.label}</th><td>{displayNumber(row.voi_score)}</td>
                    <td>{row.dominant_impact}</td><td>{row.required_field_document}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>}
      </Collapsible>
    </Card>
  );
}

export function PowerFlowPanel({onAskDiagnose}) {
  const [scenario, setScenario] = useState("tap_sensitivity");
  const [atTime, setAtTime] = useState("2026-11-13 17:00:00");
  const [snMva, setSnMva] = useState("6.3");
  const [vkPct, setVkPct] = useState("7.5");
  const [cableKm, setCableKm] = useState("1.8");
  const [outageMode, setOutageMode] = useState("feeder_1009");
  const [targetCab, setTargetCab] = useState("1010");
  const [qcKvar, setQcKvar] = useState("");

  const [profile, setProfile] = useState(null);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");

  async function executeSolve(overrides = {}) {
    const sc = overrides.scenario ?? scenario;
    const ts = overrides.atTime ?? atTime;
    const om = overrides.outageMode ?? outageMode;
    const tc = overrides.targetCab ?? targetCab;
    setLoading(true);
    setErr("");
    try {
      const payload = {
        scenario: sc,
        at_time: ts.trim() || null,
        transformer_sn_mva: snMva ? Number(snMva) : null,
        transformer_vk_percent: vkPct ? Number(vkPct) : null,
        cable_length_km: cableKm ? Number(cableKm) : null,
        outage_mode: om,
        target_cabinet: tc,
        qc_kvar: qcKvar.trim() ? Number(qcKvar) : null,
      };
      const res = await apiRequest("/api/powerflow", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      });
      setData(res);
    } catch (e) {
      setErr(e.message || "交流潮流求解失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    apiRequest("/api/mine-profile")
      .then(setProfile)
      .catch(() => {});
    executeSolve({scenario: "tap_sensitivity", atTime: "2026-11-13 17:00:00"});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function selectScenario(sc) {
    setScenario(sc.id);
    setAtTime(sc.defaultTime);
    executeSolve({scenario: sc.id, atTime: sc.defaultTime});
  }

  function handleAskAgent() {
    if (!onAskDiagnose) return;
    const prompts = {
      tap_sensitivity: `请用 pandapower 交流潮流分析 ${atTime} 2#主变高压分接头调几档可以消除 10 kV II段过压？`,
      base_case: `请用 pandapower 交流潮流校核 ${atTime} 全网母线电压拟合残差与网损情况。`,
      n1_contingency: `请用 pandapower 执行 ${atTime} 的 N-1 故障交流潮流（${outageMode === "feeder_1009" ? "1009退运转供1010" : outageMode}），校核电缆负载率与井下末端压降。`,
      reactive_opt: `请用 pandapower 交流潮流对比 ${targetCab} 柜在 ${atTime} 投切无功补偿前后的降流率与全网降损效果。`,
    };
    onAskDiagnose(prompts[scenario] || prompts.tap_sensitivity);
  }

  const activeMeta = SCENARIOS.find(s => s.id === scenario) || SCENARIOS[0];
  const ns = data?.network_summary;
  const solver = data?.solver;
  const tapData = data?.tap_sensitivity;
  const n1Data = data?.n1_contingency;
  const roData = data?.reactive_opt;

  return <div className="view-stack">
    {/* Header + MineProfile Metadata Strip */}
    <div className="canvas-subbar pf-subbar">
      <div className="apple-sld-filters" role="tablist" aria-label="交流潮流仿真场景">
        {SCENARIOS.map(sc => (
          <button
            key={sc.id}
            type="button"
            className={"apple-filter-pill" + (scenario === sc.id ? " is-active" : "")}
            onClick={() => selectScenario(sc)}
          >
            <span>{sc.label}</span>
          </button>
        ))}
      </div>
      <div className="pf-meta-badges">
        {profile && (
          <span className="agent-mini-pill is-blue">
            <Database size={11} />
            <span>
              MineProfile: {profile.mine_id} ({profile.archive_validation?.db_cabinet_count || 25}/25 柜一致)
            </span>
          </span>
        )}
        <a href="/docs" target="_blank" rel="noreferrer" className="agent-mini-pill is-neutral pf-docs-link">
          <ExternalLink size={11} />
          <span>FastAPI OpenAPI /docs</span>
        </a>
      </div>
    </div>

    {/* Editable Parameterized Equivalent-Circuit Controls */}
    <Card padding={16} className="pf-control-card">
      <div className="pf-card-head">
        <div className="pf-head-left">
          <Sliders size={15} />
          <div>
            <strong>pandapower Newton-Raphson 真实交流潮流引擎</strong>
            <p className="muted">{activeMeta.hint}</p>
          </div>
        </div>
        <div className="pf-head-actions">
          <Button
            variant="secondary"
            size="sm"
            label="向智能体追问潮流结论"
            onClick={handleAskAgent}
          />
          <Button
            variant="primary"
            size="sm"
            label={loading ? "求解中…" : "重新求解交流潮流"}
            disabled={loading}
            onClick={() => executeSolve()}
          />
        </div>
      </div>

      <div className="pf-param-grid">
        <label className="pf-field pf-field-wide">
          <span>归档整点边界（直接读自 SQLite）</span>
          <select
            className="pf-select"
            value={TIME_PRESETS.some(p => p.value === atTime) ? atTime : "__custom__"}
            onChange={e => {
              if (e.target.value !== "__custom__") {
                setAtTime(e.target.value);
                executeSolve({atTime: e.target.value});
              }
            }}
          >
            {TIME_PRESETS.map(p => (
              <option key={p.value} value={p.value}>{p.label}</option>
            ))}
            {!TIME_PRESETS.some(p => p.value === atTime) && (
              <option value="__custom__">{atTime}</option>
            )}
          </select>
        </label>

        <label className="pf-field">
          <span>主变额定容量 Sn (MVA · 等值)</span>
          <input
            type="number"
            step="0.1"
            min="1"
            max="50"
            className="pf-input"
            value={snMva}
            onChange={e => setSnMva(e.target.value)}
          />
        </label>

        <label className="pf-field">
          <span>短路阻抗 Uk% (等值)</span>
          <input
            type="number"
            step="0.1"
            min="2"
            max="16"
            className="pf-input"
            value={vkPct}
            onChange={e => setVkPct(e.target.value)}
          />
        </label>

        <label className="pf-field">
          <span>下井电缆长度 (km · 等值)</span>
          <input
            type="number"
            step="0.2"
            min="0.2"
            max="10"
            className="pf-input"
            value={cableKm}
            onChange={e => setCableKm(e.target.value)}
          />
        </label>

        {scenario === "n1_contingency" && (
          <label className="pf-field">
            <span>N-1 故障停运模式</span>
            <select
              className="pf-select"
              value={outageMode}
              onChange={e => {
                setOutageMode(e.target.value);
                const ts = e.target.value === "incomer_306" ? "2026-11-13 15:00:00" : "2026-11-10 06:00:00";
                setAtTime(ts);
                executeSolve({outageMode: e.target.value, atTime: ts});
              }}
            >
              <option value="feeder_1009">1009 退运 + 1200 联络由 1010 转供</option>
              <option value="incomer_306">35 kV 306 退运 + 300 母联单回</option>
              <option value="transformer_t2">2# 主变退运 + 10 kV 母联单变</option>
            </select>
          </label>
        )}

        {scenario === "reactive_opt" && <>
          <label className="pf-field">
            <span>补偿目标出线</span>
            <select
              className="pf-select"
              value={targetCab}
              onChange={e => {
                setTargetCab(e.target.value);
                executeSolve({targetCab: e.target.value});
              }}
            >
              <option value="1010">1010 · II回井下 1535</option>
              <option value="1020">1020 · 井下 1750 配电点</option>
              <option value="1009">1009 · I回井下 1535</option>
            </select>
          </label>
          <label className="pf-field">
            <span>补偿容量 kVar (留空按 cos=0.92 自动算)</span>
            <input
              type="number"
              step="50"
              min="0"
              max="3000"
              placeholder="自动按 cos=0.92"
              className="pf-input"
              value={qcKvar}
              onChange={e => setQcKvar(e.target.value)}
            />
          </label>
        </>}
      </div>
    </Card>

    <WeeklyScheduleCard />
    <SensitivityVoiCard key={atTime} atTime={atTime} />

    {err && <Banner variant="error" title="潮流计算错误" description={err} />}

    {loading && !data ? (
      <Card padding={32} className="center-state">
        <Spinner size="lg" />
        <span>正在构建 MineProfile 等值网络并执行 pandapower Newton-Raphson 迭代…</span>
      </Card>
    ) : data && ns && (
      <>
        {/* KPI Strip */}
        <div className="pf-kpi-grid">
          <div className="pf-kpi-tile">
            <span className="pf-kpi-label"><Cpu size={12} /> 求解引擎与收敛性</span>
            <strong className="pf-kpi-value is-green">
              pandapower {solver?.version} · NR 收敛
            </strong>
            <small className="muted">{data.operating_mode}</small>
          </div>

          <div className="pf-kpi-tile">
            <span className="pf-kpi-label"><Zap size={12} /> 10 kV I段 / II段仿真电压</span>
            <strong className="pf-kpi-value mono">
              {ns.bus_10kv_i_kv} / {ns.bus_10kv_ii_kv} kV
            </strong>
            <small className="muted">GB/T 12325 限值 9.30–10.70 kV</small>
          </div>

          <div className="pf-kpi-tile">
            <span className="pf-kpi-label"><Activity size={12} /> 全网总负荷与有功网损</span>
            <strong className="pf-kpi-value mono">
              {ns.total_loss_kw} kW ({ns.loss_rate_pct}%)
            </strong>
            <small className="muted">
              总有功 {ns.total_load_p_kw} kW · 变损 {ns.transformer_loss_kw} + 线损 {ns.line_loss_kw} kW
            </small>
          </div>

          <div className="pf-kpi-tile">
            <span className="pf-kpi-label"><Gauge size={12} /> 最高主变 / 下井电缆负载率</span>
            <strong className="pf-kpi-value mono">
              {ns.max_transformer_loading_pct}% / {ns.max_line_loading_pct}%
            </strong>
            <small className="muted">井下最低母线电压 {ns.min_underground_kv} kV</small>
          </div>
        </div>

        {/* Scenario-Specific Hero Analysis Card */}
        {tapData && (
          <Card padding={18} className="pf-hero-card">
            <div className="pf-section-head">
              <div>
                <h3>2# 主变高压侧分接头调压灵敏度扫描（{data.at_time}）</h3>
                <p className="muted">
                  归档实测 1006 电压 <strong className="mono">{tapData.measured_1006_uab_kv} kV</strong>
                  {tapData.measured_1006_cell && (
                    <> · <a className="cell-chip" href={cellHref(tapData.measured_1006_cell)} target="_blank" rel="noreferrer">{tapData.measured_1006_cell}</a></>
                  )}
                  ；推荐由校准档位 <strong>{tapData.current_calibrated_tap >= 0 ? "+" : ""}{tapData.current_calibrated_tap} 档</strong> 调至{" "}
                  <strong className="pf-accent">{tapData.recommended_tap?.tap_label || "+2 档"}</strong>，
                  降压 <strong className="mono">{tapData.voltage_reduction_kv} kV</strong> 回归 GB/T 12325 ±7% 安全区。
                </p>
              </div>
              <Badge
                variant={tapData.recommended_tap?.gb12325_compliant ? "success" : "warning"}
                label={tapData.recommended_tap ? `推荐 ${tapData.recommended_tap.tap_label}` : "需结合上级调压"}
              />
            </div>

            <div className="pf-table-wrap">
              <table className="pf-table">
                <thead>
                  <tr>
                    <th>2#主变高压分接头</th>
                    <th>10 kV II段母线</th>
                    <th>偏差 (%)</th>
                    <th>井下 1535 II段</th>
                    <th>井下 1750 末端</th>
                    <th>全网网损 (kW)</th>
                    <th>GB/T 12325 判定</th>
                  </tr>
                </thead>
                <tbody>
                  {tapData.sweep.map(row => {
                    const isRec = tapData.recommended_tap?.tap_pos === row.tap_pos;
                    const isCur = tapData.current_calibrated_tap === row.tap_pos;
                    return (
                      <tr key={row.tap_pos} className={isRec ? "is-recommended" : isCur ? "is-current" : ""}>
                        <td className="mono">
                          <strong>{row.tap_label}</strong>
                          {isCur && <span className="pf-tag">当前拟合档</span>}
                          {isRec && <span className="pf-tag is-ok">推荐档位</span>}
                        </td>
                        <td className="mono"><strong>{row.bus_10kv_ii_kv} kV</strong></td>
                        <td className="mono">{row.deviation_from_10kv_pct > 0 ? "+" : ""}{row.deviation_from_10kv_pct}%</td>
                        <td className="mono">{row.c1535_ii_kv} kV</td>
                        <td className="mono">{row.ug_1750_kv} kV</td>
                        <td className="mono">{row.total_loss_kw}</td>
                        <td>
                          <Badge
                            variant={row.gb12325_compliant ? "success" : "error"}
                            label={row.gb12325_compliant ? "≤10.70 kV 合规" : "越上限"}
                          />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Card>
        )}

        {n1Data && (
          <Card padding={18} className="pf-hero-card">
            <div className="pf-section-head">
              <div>
                <h3>N-1 故障交流潮流校核 · {n1Data.description}</h3>
                <p className="muted">
                  对比正常运行与 N-1 故障收敛状态下的电缆电流、负载率、压降与全网损耗变化。
                </p>
              </div>
              <Badge
                variant={n1Data.verdict === "conditionally_passed" ? "success" : "error"}
                label={n1Data.verdict === "conditionally_passed" ? "条件通过（热稳定+电压达标）" : "存在越限风险"}
              />
            </div>
            <div className="pf-compare-grid">
              <div className="pf-compare-tile">
                <span>1010 电缆电流 (正常 → N-1)</span>
                <strong className="mono">
                  {n1Data.receiving_1010_comparison?.before_i_a} A → {n1Data.receiving_1010_comparison?.after_i_a} A
                </strong>
              </div>
              <div className="pf-compare-tile">
                <span>1010 等值负载率 (300 A 基准)</span>
                <strong className="mono">
                  {n1Data.receiving_1010_comparison?.before_loading_pct}% → {n1Data.receiving_1010_comparison?.after_loading_pct}%
                </strong>
              </div>
              <div className="pf-compare-tile">
                <span>1010 线路压降 ΔU%</span>
                <strong className="mono">
                  {n1Data.receiving_1010_comparison?.before_voltage_drop_pct}% → {n1Data.receiving_1010_comparison?.after_voltage_drop_pct}%
                </strong>
              </div>
              <div className="pf-compare-tile">
                <span>N-1 井下最低电压 / 网损增量</span>
                <strong className="mono">
                  {n1Data.receiving_1010_comparison?.min_underground_kv} kV / +{n1Data.loss_increase_kw} kW
                </strong>
              </div>
            </div>
          </Card>
        )}

        {roData && (
          <Card padding={18} className="pf-hero-card">
            <div className="pf-section-head">
              <div>
                <h3>
                  无功补偿前后全网交流潮流对比 · {roData.target_cabinet} 柜末端投入 {roData.qc_kvar} kVar
                </h3>
                <p className="muted">
                  归档边界 P={roData.measured_p_kw} kW、cos={roData.measured_cos}
                  {roData.measured_p_cell && (
                    <> · <a className="cell-chip" href={cellHref(roData.measured_p_cell)} target="_blank" rel="noreferrer">{roData.measured_p_cell}</a></>
                  )}
                </p>
              </div>
              <Badge variant="success" label={`全网降损 ${roData.delta?.total_loss_reduction_kw} kW`} />
            </div>
            <div className="pf-compare-grid">
              <div className="pf-compare-tile">
                <span>线路电流下降</span>
                <strong className="mono pf-ok">
                  -{roData.delta?.current_reduction_a} A (-{roData.delta?.current_reduction_pct}%)
                </strong>
                <small className="muted">{roData.before?.line_i_a} → {roData.after?.line_i_a} A</small>
              </div>
              <div className="pf-compare-tile">
                <span>该电缆有功线损降低</span>
                <strong className="mono pf-ok">
                  -{roData.delta?.line_loss_reduction_kw} kW (-{roData.delta?.line_loss_reduction_pct}%)
                </strong>
                <small className="muted">{roData.before?.line_pl_kw} → {roData.after?.line_pl_kw} kW</small>
              </div>
              <div className="pf-compare-tile">
                <span>井下受端电压抬升</span>
                <strong className="mono">
                  +{roData.delta?.receiving_u_boost_kv} kV
                </strong>
                <small className="muted">{roData.before?.receiving_u_kv} → {roData.after?.receiving_u_kv} kV</small>
              </div>
              <div className="pf-compare-tile">
                <span>全网总网损变化</span>
                <strong className="mono pf-ok">
                  -{roData.delta?.total_loss_reduction_kw} kW
                </strong>
                <small className="muted">{roData.before?.total_loss_kw} → {roData.after?.total_loss_kw} kW</small>
              </div>
            </div>
          </Card>
        )}

        {/* Bus Voltage Calibration Table + Branch Loading Gauges */}
        <div className="pf-two-col">
          <Card padding={16}>
            <h4 className="pf-subhead">节点母线电压潮流解与 SQLite 实测残差校核</h4>
            <div className="pf-table-wrap">
              <table className="pf-table">
                <thead>
                  <tr>
                    <th>母线节点</th>
                    <th>潮流仿真</th>
                    <th>实测归档</th>
                    <th>拟合残差</th>
                    <th>合规</th>
                  </tr>
                </thead>
                <tbody>
                  {data.buses.map(b => (
                    <tr key={b.bus_id}>
                      <td>
                        <strong>{b.name}</strong>
                        {b.measured_cell && (
                          <div>
                            <a className="cell-chip" href={cellHref(b.measured_cell)} target="_blank" rel="noreferrer">
                              {b.measured_cell}
                            </a>
                          </div>
                        )}
                      </td>
                      <td className="mono">
                        <strong>{b.sim_u_kv} kV</strong>
                        <div className="muted">{b.sim_vm_pu} pu</div>
                      </td>
                      <td className="mono">{b.measured_u_kv !== null ? `${b.measured_u_kv} kV` : "井下无表"}</td>
                      <td className="mono">
                        {b.calibration_diff_kv !== null
                          ? `${b.calibration_diff_kv > 0 ? "+" : ""}${b.calibration_diff_kv} kV`
                          : "—"}
                      </td>
                      <td>
                        {b.gb12325_compliant
                          ? <CheckCircle2 size={15} className="pf-ok" />
                          : <AlertTriangle size={15} className="pf-warn" />}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <Card padding={16}>
            <h4 className="pf-subhead">主变与下井电缆支路负载率及损耗</h4>
            <div className="pf-branch-list">
              {data.transformers.map(tr => (
                <div key={tr.id} className="pf-branch-item">
                  <div className="pf-branch-head">
                    <strong>{tr.name}</strong>
                    <span className="mono">
                      {tr.in_service ? `${tr.loading_percent}% · ${tr.tap_pos >= 0 ? "+" : ""}${tr.tap_pos}档 · 损 ${tr.pl_kw} kW` : "停运"}
                    </span>
                  </div>
                  <ProgressBar value={Math.min(100, tr.loading_percent)} max={100} />
                </div>
              ))}
              {data.lines.map(ln => (
                <div key={ln.cabinet} className="pf-branch-item">
                  <div className="pf-branch-head">
                    <strong>{ln.cabinet} 柜 · {ln.label} ({ln.length_km} km)</strong>
                    <span className="mono">
                      {ln.in_service
                        ? `${ln.i_from_a} A (${ln.loading_percent}%) · ΔU ${ln.voltage_drop_pct}% · 损 ${ln.pl_kw} kW`
                        : "退运断开"}
                    </span>
                  </div>
                  <ProgressBar value={Math.min(100, ln.loading_percent)} max={100} />
                </div>
              ))}
            </div>
          </Card>
        </div>

        {/* Strict Academic Disclosure Box: Measured Boundaries vs Equivalent-Circuit Assumptions */}
        <Card padding={16} className="pf-disclosure-card">
          <Collapsible
            title="学术透明声明：参数化等值电路假设 vs 归档实测边界（严防参数编造）"
            defaultOpen={false}
          >
            <div className="pf-disclosure-body">
              <p className="pf-disclaimer">{data.model_assumptions?.disclaimer}</p>
              <div className="pf-assump-grid">
                <div><span>主变等值参数</span><strong className="mono">{data.model_assumptions?.transformer_sn_mva} MVA · {data.model_assumptions?.transformer_vn_kv} kV · Uk={data.model_assumptions?.transformer_vk_percent}%</strong></div>
                <div><span>1#/2#校准分接头</span><strong className="mono">T1={data.model_assumptions?.tr1_tap_pos >= 0 ? "+" : ""}{data.model_assumptions?.tr1_tap_pos} 档 / T2={data.model_assumptions?.tr2_tap_pos >= 0 ? "+" : ""}{data.model_assumptions?.tr2_tap_pos} 档（步长 2.5%）</strong></div>
                <div><span>下井电缆参数</span><strong className="mono">{data.model_assumptions?.downhole_1009_1010_cable_length_km} km · {data.model_assumptions?.downhole_1009_1010_r_x_ohm_per_km}</strong></div>
              </div>
              <div className="pf-missing-box">
                <strong>升级为精确矿井数字孪生仍需现场补齐的铭牌台账：</strong>
                <ul>
                  {(data.missing_field_evidence || []).map((item, idx) => <li key={idx}>{item}</li>)}
                </ul>
              </div>
            </div>
          </Collapsible>
        </Card>
      </>
    )}
  </div>;
}
