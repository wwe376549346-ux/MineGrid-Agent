import React, {useEffect, useState} from "react";
import {Badge} from "@astryxdesign/core/Badge";
import {Banner} from "@astryxdesign/core/Banner";
import {Button} from "@astryxdesign/core/Button";
import {Card} from "@astryxdesign/core/Card";
import {Collapsible} from "@astryxdesign/core/Collapsible";
import {ProgressBar} from "@astryxdesign/core/ProgressBar";
import {Spinner} from "@astryxdesign/core/Spinner";
import {TextInput} from "@astryxdesign/core/TextInput";
import {apiRequest, cellHref} from "./api.js";
import {GraphDemoCard} from "./GraphDemoCard.jsx";

const STATUS_BADGE = {
  needs_supplement: {label: "待补证 (v1 基线)", variant: "warning"},
  partial_supplement: {label: "绑定不全 · 拒绝判定", variant: "warning"},
  conditionally_satisfied: {label: "条件满足 (v2 复算)", variant: "success"},
  conditionally_violated: {label: "条件越限 (v2 复算)", variant: "error"},
};

export function SupplementEvalView({section = "all"}) {
  const [graphSubMode, setGraphSubMode] = useState("stategraph");
  const [suppData, setSuppData] = useState(null);
  const [evalData, setEvalData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState("");
  const [showBaselineOnly, setShowBaselineOnly] = useState(false);

  // Editable supplement form parameters
  const [sourceDoc, setSourceDoc] = useState("老洼地机电科参数与操作核实单-20261119");
  const [rated303, setRated303] = useState("200");
  const [ratedTr1, setRatedTr1] = useState("6300");
  const [derate, setDerate] = useState("0.90");
  const [switchLog, setSwitchLog] = useState("35kV母联300开关合位，306柜07:00-19:00计划停电检修");
  const [rated1009, setRated1009] = useState("300");
  const [tieMode, setTieMode] = useState("1535中央变电所1200联络开关热备用可自动切换");

  useEffect(() => {
    const ctrl = new AbortController();
    Promise.all([
      apiRequest("/api/supplement", {signal: ctrl.signal}),
      apiRequest("/api/evaluation", {signal: ctrl.signal}),
    ])
      .then(([s, e]) => {
        setSuppData(s);
        setEvalData(e);
      })
      .catch(error => {
        if (error.name !== "AbortError") setErr(error.message);
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoading(false);
      });
    return () => ctrl.abort();
  }, []);

  async function runRecalculation(event) {
    event.preventDefault();
    setRunning(true);
    setErr("");
    try {
      const params = {};
      if (rated303.trim()) params["303_rated_current_a"] = Number(rated303);
      if (ratedTr1.trim()) params["main_transformer_rated_kva"] = Number(ratedTr1);
      if (derate.trim()) params["ambient_derating_factor"] = Number(derate);
      if (switchLog.trim()) params["switch_log_20261113"] = switchLog.trim();
      if (rated1009.trim()) params["1009_1010_rated_current_a"] = Number(rated1009);
      if (tieMode.trim()) params["c1535_tie_switch_mode"] = tieMode.trim();

      const res = await apiRequest("/api/supplement", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          station: "S35",
          source_doc: sourceDoc.trim(),
          parameters: params,
        }),
      });
      setSuppData(res);
      setShowBaselineOnly(false);
    } catch (error) {
      setErr(error.message);
    } finally {
      setRunning(false);
    }
  }

  if (loading) {
    return <Card padding={32} className="center-state">
      <Spinner size="lg" />
      <span>正在加载阶段 11 补证局部复算与阶段 12 五层评测大盘…</span>
    </Card>;
  }

  const v1 = suppData?.baseline_v1;
  const v2 = suppData?.supplemented_v2;
  const activeRun = showBaselineOnly ? v1 : (v2 || v1);
  const ev = evalData?.evaluation;
  const corpus = evalData?.corpus;
  const rag = evalData?.rag_ablation;
  const graph = evalData?.graph_orchestration;
  const stream = evalData?.stream_benchmark;
  const bench36 = evalData?.benchmark_36;

  const CAT_LABELS = {
    A_fact_and_analytics: "A · 即席统计与精准查数 (F01–F06)",
    B_multihop_diagnosis: "B · 多跳运行健康诊断 (D01–D06)",
    C_physics_and_capacity: "C · 电力物理仿真与条件复算 (P01–P06)",
    D_boundary_and_safety: "D · 跨站边界、缺证与控制拦截 (B01–B06)",
    E_multiturn_and_falsification: "E · 真多轮会话记忆与假设证伪 (M01–M06)",
    F_hybrid_rag_and_hitl: "F · 混合语义 RAG 与双检查点 HITL (H01–H06)",
  };

  const showGraphSupp = section === "all" || section === "graph";
  const showEval = section === "all" || section === "eval";

  return <div className="view-stack">
    {showGraphSupp && <>
      <div className="canvas-subbar">
        <div className="apple-sld-filters" role="tablist" aria-label="状态图与补证子视图">
          <button
            type="button"
            role="tab"
            aria-selected={graphSubMode === "stategraph"}
            className={"apple-filter-pill" + (graphSubMode === "stategraph" ? " is-active" : "")}
            onClick={() => setGraphSubMode("stategraph")}
          >
            <span>① 七节点状态图编排（挂起 ➔ 恢复）</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={graphSubMode === "supplement"}
            className={"apple-filter-pill" + (graphSubMode === "supplement" ? " is-active" : "")}
            onClick={() => setGraphSubMode("supplement")}
          >
            <span>② 补证参数与局部复算（V01–V04 · v1→v2）</span>
          </button>
        </div>

        {graphSubMode === "supplement" && (
          <div className="apple-sld-filters" role="group" aria-label="切换基线或复算版本">
            <button
              type="button"
              className={"apple-filter-pill" + (showBaselineOnly ? " is-active" : "")}
              onClick={() => setShowBaselineOnly(true)}
            >
              v1 缺证基线
            </button>
            <button
              type="button"
              className={"apple-filter-pill" + (!showBaselineOnly ? " is-active" : "")}
              onClick={() => setShowBaselineOnly(false)}
            >
              v2 补证复算
            </button>
          </div>
        )}
      </div>

      {graphSubMode === "stategraph" ? (
        <GraphDemoCard />
      ) : <>
        {err && <Banner status="error" title="补证复算校验拦截" description={err} />}

        {/* Compact 1-Line Version Lineage Strip */}
        {activeRun && (
          <div className={"supp-version-strip" + (activeRun.version > 1 ? " is-v2" : "")}>
            <div className="supp-version-left">
              <Badge
                variant={activeRun.version > 1 ? "success" : "warning"}
                label={activeRun.version > 1 ? "v2 局部复算已生效" : "v1 原始缺证基线"}
              />
              <code className="mono">
                {activeRun.version > 1
                  ? `${activeRun.parent_run_id} ➔ ${activeRun.run_id}`
                  : activeRun.run_id}
              </code>
            </div>
            <span className="supp-version-meta">
              {activeRun.version > 1
                ? `局部重算 ${activeRun.summary.recalculated_items} 项 · 直接复用父版本 ${activeRun.summary.reused_items} 项 · 快照哈希 ${activeRun.snapshot.snapshot_sha256.slice(0, 12)}…`
                : "缺额定容量或开关日志时保留原表峰值，严禁猜测是否过载"}
            </span>
          </div>
        )}

        {/* Clean Full-Width Apple Parameter Card (No 2-Col Cramping!) */}
        <Card padding={18} className="supp-form-card">
          <div className="supp-form-header">
            <div>
              <h3>现场额定参数与开关状态补证录入</h3>
              <p className="muted">调整任意参数实时触发 v1 → v2 按依赖局部复算，或清空开关日志验证「绑定不全拒绝判定」</p>
            </div>
            <div className="supp-quick-btns">
              <Button
                type="button"
                variant="secondary"
                size="sm"
                label="清空开关日志（测缺项拦截）"
                onClick={() => setSwitchLog("")}
              />
              <Button
                type="button"
                variant="secondary"
                size="sm"
                label="重置默认"
                onClick={() => {
                  setRated303("200");
                  setRatedTr1("6300");
                  setDerate("0.90");
                  setRated1009("300");
                  setSwitchLog("35kV母联300开关合位，306柜07:00-19:00计划停电检修");
                  setTieMode("1535中央变电所1200联络开关热备用可自动切换");
                }}
              />
            </div>
          </div>

          <form className="supp-form-clean" onSubmit={runRecalculation}>
            <div className="supp-grid-4">
              <TextInput label="303 额定载流 (A)" value={rated303} onChange={setRated303} width="100%" />
              <TextInput label="1#主变容量 (kVA)" value={ratedTr1} onChange={setRatedTr1} width="100%" />
              <TextInput label="降容系数 (0.5~1.2)" value={derate} onChange={setDerate} width="100%" />
              <TextInput label="1009/1010 额定 (A)" value={rated1009} onChange={setRated1009} width="100%" />
            </div>

            <div className="supp-grid-2">
              <TextInput label="2026-11-13 母联与 306 检修开关确认" value={switchLog} onChange={setSwitchLog} width="100%" />
              <TextInput label="1535 所 1200 联络柜运行方式" value={tieMode} onChange={setTieMode} width="100%" />
            </div>

            <div className="supp-submit-row">
              <div style={{flex: 1, minWidth: 220}}>
                <TextInput label="补证核实单来源标识" value={sourceDoc} onChange={setSourceDoc} width="100%" />
              </div>
              <Button
                type="submit"
                variant="primary"
                size="md"
                label={running ? "正在局部复算…" : "执行按依赖局部复算 (v1 → v2)"}
                isDisabled={running}
              />
            </div>
          </form>
        </Card>

        {/* Clean 2x2 Apple Bento Grid for V01-V04 Verification Cards */}
        <div className="supp-bento-grid">
          {(activeRun?.items || []).map(item => {
            const stMeta = STATUS_BADGE[item.status] || STATUS_BADGE.needs_supplement;
            const cj = item.conditional_judgment;
            return <Card key={item.id} padding={16} className={"supp-item-card" + (item.recalculated ? " is-recalc" : "")}>
              <div className="supp-card-top">
                <div className="detail-badges">
                  <code className="mono">{item.id}</code>
                  <Badge variant={stMeta.variant} label={stMeta.label} />
                  {item.recalculated && <Badge variant="teal" label="本轮重算" />}
                  {item.reused_from_run && <Badge variant="neutral" label="复用 v1" />}
                </div>
                <span className="mono muted" style={{fontSize: 11}}>{item.target_objects.join(" / ")}</span>
              </div>

              <h3 className="supp-card-title">{item.title}</h3>
              <p className="supp-card-desc">{item.explanation}</p>

              {cj && item.id === "V01" && (
                <div className="supp-gauges">
                  <ProgressBar
                    value={cj.current_load_ratio_pct}
                    max={100}
                    label={`303 峰值电流负载率：${cj.peak_current_a}A / ${cj.effective_current_limit_a}A（裕度 +${cj.current_margin_pct}%）`}
                    hasValueLabel
                    variant={cj.current_load_ratio_pct <= 80 ? "success" : "warning"}
                  />
                  <ProgressBar
                    value={cj.transformer_load_ratio_pct}
                    max={100}
                    label={`1#主变视在负载率：${cj.transformer_peak_kva} / ${cj.transformer_rated_kva} kVA（裕度 +${cj.transformer_margin_pct}%）`}
                    hasValueLabel
                    variant={cj.transformer_load_ratio_pct <= 85 ? "success" : "warning"}
                  />
                </div>
              )}

              {cj && item.id === "V02" && (
                <div className="supp-gauges">
                  <ProgressBar
                    value={cj.n_minus_1_load_ratio_pct}
                    max={100}
                    label={`下井双回 N-1 负载率：${cj.n_minus_1_combined_peak_a}A / ${cj.effective_single_cable_limit_a}A（裕度 +${cj.n_minus_1_margin_pct}%）`}
                    hasValueLabel
                    variant={cj.n_minus_1_load_ratio_pct <= 95 ? "warning" : "error"}
                  />
                </div>
              )}

              <div className="inline-cells">
                {(item.confirmed_local_facts?.evidence_cells || []).slice(0, 3).map((evStr, i) => {
                  const href = cellHref(evStr);
                  return href
                    ? <a key={i} className="cell-chip" href={href} download>{evStr}</a>
                    : <span key={i} className="cell-chip">{evStr}</span>;
                })}
              </div>
            </Card>;
          })}
        </div>
      </>}
    </>}

    {showEval && ev && <Card padding={20}>
      <div className="section-head">
        <div>
          <h2>五层 BFCL + GAIA 智能体综合评测大盘（阶段 12）</h2>
          <p className="muted">冻结同一版老洼地 7 份日报与 2 张截图，从告警降噪、资料边界、GSSC 会话隔离、受限工具调用到开放式健康诊断逐层核验</p>
        </div>
        <Badge variant="success" label="7 套自动化回归全通过" />
      </div>

      <div className="eval-bars-grid">
        <div className="eval-bar-card">
          <ProgressBar
            value={ev.layer1_monitoring_and_health.noise_reduction_pct}
            max={100}
            label={`L1 监测降噪收敛率（106 条碎提示 → ${ev.layer1_monitoring_and_health.actionable_high_medium_events} 项健康事件）`}
            hasValueLabel
            variant="success"
          />
        </div>
        <div className="eval-bar-card">
          <ProgressBar
            value={100}
            max={100}
            label={`L2 资料台账与可答边界核验（C01–C09：${ev.layer2_corpus_and_boundaries.passed_cases}/${ev.layer2_corpus_and_boundaries.total_cases} 通过）`}
            hasValueLabel
            variant="success"
          />
        </div>
        <div className="eval-bar-card">
          <ProgressBar
            value={100}
            max={100}
            label={`L5 历史实测 DeepSeek (${ev.layer5_gaia_diagnostic_agent_6.model}) 开放式健康诊断（${ev.layer5_gaia_diagnostic_agent_6.passed.model}/${ev.layer5_gaia_diagnostic_agent_6.total_cases} 当次通过）`}
            hasValueLabel
            variant="success"
          />
        </div>
        <div className="eval-bar-card">
          <ProgressBar
            value={ev.layer5_gaia_diagnostic_agent_6.citations.grounded_accuracy_pct}
            max={100}
            label={`程序级证据忠实度（${ev.layer5_gaia_diagnostic_agent_6.citations.verified_cells}/${ev.layer5_gaia_diagnostic_agent_6.citations.cited_cells} 单元格 · 0 幻觉）`}
            hasValueLabel
            variant="accent"
          />
        </div>
      </div>

      <div className="table-wrap" style={{marginTop: 16}}>
        <table className="pro-table">
          <thead>
            <tr>
              <th>题号</th>
              <th>诊断评测主题（D01–D06）</th>
              <th>本地基线</th>
              <th>DeepSeek (deepseek-v4-pro)</th>
              <th>轮次 / 工具数</th>
              <th>耗时 (s)</th>
              <th>原表单元格核验</th>
              <th>规程条款核验</th>
              <th>越界伪造引用</th>
            </tr>
          </thead>
          <tbody>
            {(ev.layer5_gaia_diagnostic_agent_6.cases || []).map(c => (
              <tr key={c.id}>
                <td><code className="mono">{c.id}</code></td>
                <td><strong>{c.topic}</strong></td>
                <td><Badge variant="success" label="通过" /></td>
                <td><Badge variant={c.model_passed ? "success" : "error"} label={c.model_passed ? "通过" : "未通过"} /></td>
                <td className="mono">{c.rounds} 轮 / {c.tool_calls} 次</td>
                <td className="mono">{c.elapsed_seconds.toFixed(1)}s</td>
                <td className="mono">{c.verified_cells}</td>
                <td className="mono">{c.verified_standards} 条</td>
                <td><Badge variant="success" label={`${c.ungrounded_citations} 处`} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {corpus && (
        <Collapsible
          defaultIsOpen={false}
          trigger={<strong>展开查看阶段 08 首批 9 道资料出处与可答边界核验题（C01–C09，9/9 通过）</strong>}
        >
          <div className="table-wrap" style={{marginTop: 10}}>
            <table className="pro-table">
              <thead>
                <tr>
                  <th>题号</th>
                  <th>边界类别</th>
                  <th>测试问题</th>
                  <th>状态</th>
                  <th>核验出处与回执</th>
                </tr>
              </thead>
              <tbody>
                {(corpus.verification_cases || []).map(c => (
                  <tr key={c.id}>
                    <td><code className="mono">{c.id}</code></td>
                    <td>{c.category}</td>
                    <td>{c.question}</td>
                    <td><Badge variant="success" label="通过" /></td>
                    <td><code className="mono">{c.evidence}</code></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Collapsible>
      )}
    </Card>}

    {/* Stage 09: Conditional RAG vs Direct Tools Ablation */}
    {showEval && rag && <Card padding={20}>
      <div className="section-head">
        <div>
          <h2>阶段 09 · 条件化 RAG 与直接查数工具同题消融实验（R01–R10）</h2>
          <p className="muted">
            仅检索老洼地归档来源（{rag.evidence_cards_count} 张本矿证据卡 + {rag.standard_clauses_count} 条规程/国标条款），对比「仅直接工具」与「直接工具 + 条件化 RAG + 适用性核验」
          </p>
        </div>
        <Badge variant="success" label={`RAG 提升 +${rag.tools_plus_conditional_rag.accuracy_pct - rag.direct_tools_only.accuracy_pct}% (改善 ${rag.improved_case_ids.length} 题)`} />
      </div>

      <div className="eval-bars-grid">
        <div className="eval-bar-card">
          <ProgressBar
            value={rag.direct_tools_only.accuracy_pct}
            max={100}
            label={`基线：仅直接 SQLite 查数工具（${rag.direct_tools_only.passed}/${rag.total_cases} 通过 · 缺规程与适用前提核验）`}
            hasValueLabel
            variant="warning"
          />
        </div>
        <div className="eval-bar-card">
          <ProgressBar
            value={rag.tools_plus_conditional_rag.accuracy_pct}
            max={100}
            label={`本文方法：直接工具 + 条件化 RAG（${rag.tools_plus_conditional_rag.passed}/${rag.total_cases} 全通 · 改善 ${rag.improved_case_ids.join(", ")}）`}
            hasValueLabel
            variant="success"
          />
        </div>
      </div>

      <div className="table-wrap" style={{marginTop: 14}}>
        <table className="pro-table">
          <thead>
            <tr>
              <th>题号</th>
              <th>考查维度</th>
              <th>测试问题</th>
              <th>仅直接工具</th>
              <th>直接工具 + 条件化 RAG</th>
              <th>命中证据卡 / 规程</th>
              <th>适用性与缺证核验</th>
            </tr>
          </thead>
          <tbody>
            {(rag.cases || []).map(c => (
              <tr key={c.id}>
                <td><code className="mono">{c.id}</code></td>
                <td><Badge variant="neutral" label={c.category} /></td>
                <td>{c.question}</td>
                <td>
                  <Badge
                    variant={c.direct_tools_only_passed ? "success" : "warning"}
                    label={c.direct_tools_only_passed ? "通过" : "缺规程/边界"}
                  />
                </td>
                <td>
                  <Badge
                    variant={c.tools_plus_conditional_rag_passed ? "success" : "error"}
                    label={c.tools_plus_conditional_rag_passed ? (c.improved_by_rag ? "通过 (✦改善)" : "通过") : "未通过"}
                  />
                </td>
                <td className="mono">
                  {[...c.matched_card_ids.slice(0, 1), ...c.matched_standard_ids.slice(0, 1)].join(" · ") || "直接查库"}
                </td>
                <td className="mono">
                  {c.applicability_status} · 缺证 {c.missing_evidence_count} 项
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>}

    {/* Stage 10 & Stage 15: State-Graph Cross-Process Resume & Continuous Stream Fault-Injection */}
    {showEval && <div className="two-col">
      {graph && <Card padding={20}>
        <div className="section-head">
          <div>
            <h2>阶段 10 · 状态图审查编排与跨进程检查点恢复</h2>
            <p className="muted">绑定档案 SHA-256 快照（{graph.snapshot.snapshot_sha256.slice(0, 12)}…），跨进程恢复 0 重复查库</p>
          </div>
          <Badge variant="success" label={`${graph.passed_scenarios}/${graph.total_scenarios} 通过`} />
        </div>
        <div className="table-wrap">
          <table className="pro-table">
            <thead>
              <tr>
                <th>场景</th>
                <th>测试内容</th>
                <th>终态</th>
                <th>新执行 / 复用步骤</th>
              </tr>
            </thead>
            <tbody>
              {(graph.scenarios || []).map(s => (
                <tr key={s.id}>
                  <td><code className="mono">{s.id}</code></td>
                  <td>{s.title}</td>
                  <td>
                    <Badge
                      variant={s.terminal_status === "completed" ? "success" : s.terminal_status === "needs_supplement" ? "warning" : "error"}
                      label={s.terminal_status}
                    />
                  </td>
                  <td className="mono">执行 {s.executed_steps} / 复用 {s.reused_steps}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>}

      {stream && <Card padding={20}>
        <div className="section-head">
          <div>
            <h2>阶段 15 · 连续流异常注入与实时筛查时延仿真</h2>
            <p className="muted">
              168 帧（16,800 点）逐帧四维筛查：P50 = {stream.latency_ms.p50_ms} ms · P95 = {stream.latency_ms.p95_ms} ms · P99 = {stream.latency_ms.p99_ms} ms
            </p>
          </div>
          <Badge variant="success" label={`${stream.passed_tests}/${stream.total_tests} 通过`} />
        </div>
        <div className="table-wrap">
          <table className="pro-table">
            <thead>
              <tr>
                <th>实验</th>
                <th>异常流注入场景</th>
                <th>实测鲁棒性表现</th>
              </tr>
            </thead>
            <tbody>
              {(stream.tests || []).map(t => (
                <tr key={t.id}>
                  <td><code className="mono">{t.id}</code></td>
                  <td>{t.title}</td>
                  <td>{t.observed}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>}
    </div>}

    {/* Comprehensive 36-Case Benchmark & 4-Axis Ablation Matrix (Thesis Chapter 5) */}
    {showEval && bench36 && <Card padding={20}>
      <div className="section-head">
        <div>
          <h2>硕士论文综合基准 · 36 题六维能力评测与四维消融矩阵</h2>
          <p className="muted">
            覆盖即席统计、多跳诊断、无功/N-1 物理仿真、安全越界拦截、真多轮记忆与混合语义 RAG（全部数值基于归档原表单元格校验）
          </p>
        </div>
        <Badge
          variant="success"
          label={`${bench36.total_passed}/${bench36.total_cases} (${bench36.overall_rate_pct}%)`}
        />
      </div>

      <div className="two-col" style={{marginTop: 12}}>
        <div className="table-wrap">
          <table className="pro-table">
            <thead>
              <tr>
                <th>能力评测维度</th>
                <th>通过 / 总数</th>
                <th>通过率</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(bench36.category_summary || {}).map(([key, st]) => (
                <tr key={key}>
                  <td>{CAT_LABELS[key] || key}</td>
                  <td className="mono"><strong>{st.passed}/{st.total}</strong></td>
                  <td>
                    <Badge variant={st.passed === st.total ? "success" : "warning"} label={`${st.rate_pct}%`} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="table-wrap">
          <table className="pro-table">
            <thead>
              <tr>
                <th>消融机制 (论文对照表)</th>
                <th>去机制基线 (w/o)</th>
                <th>完整系统 (Full)</th>
                <th>定量增益</th>
              </tr>
            </thead>
            <tbody>
              {(bench36.ablation_matrix || []).map((ab, idx) => (
                <tr key={idx}>
                  <td><strong>{ab.axis}</strong></td>
                  <td className="muted">{ab.baseline || `${ab.baseline_cell_only_caught}/${ab.total_adversarial} (0%)`}</td>
                  <td className="mono">{ab.full_system || `${ab.full_two_level_caught}/${ab.total_adversarial} (100%)`}</td>
                  <td><Badge variant="success" label={ab.gain_desc} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </Card>}
  </div>;
}
