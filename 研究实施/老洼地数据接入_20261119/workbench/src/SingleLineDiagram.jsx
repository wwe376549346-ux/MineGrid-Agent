import React, {useEffect, useMemo, useState} from "react";
import {
  ArrowDown,
  ChevronLeft,
  ChevronRight,
  Clock,
  Cpu,
  FileSpreadsheet,
  Layers,
  Pause,
  Play,
  RotateCcw,
  Sparkles,
  Zap,
} from "lucide-react";
import {Badge} from "@astryxdesign/core/Badge";
import {apiRequest, formatNum, sourceUrl} from "./api.js";

function TransformerSymbol({title, subtitle, tone = "normal"}) {
  const strokeColor =
    tone === "high" ? "var(--apple-red)" : tone === "warn" ? "var(--apple-orange)" : "var(--apple-blue)";
  return <div className={"apple-tr-bridge tone-" + tone}>
    <div className="apple-tr-glyph" aria-hidden="true">
      <svg width="34" height="42" viewBox="0 0 36 44" fill="none">
        <circle cx="18" cy="14" r="10" stroke={strokeColor} strokeWidth="2.1" fill="var(--surface)" />
        <circle cx="18" cy="30" r="10" stroke={strokeColor} strokeWidth="2.1" fill="var(--surface)" />
      </svg>
    </div>
    <div className="apple-tr-meta">
      <strong>{title}</strong>
      <span>{subtitle}</span>
    </div>
  </div>;
}

export function SingleLineDiagram({health, selectedCab, onSelectCabinet, externalReplayIndex = null}) {
  const [filterMode, setFilterMode] = useState("all");
  const [timeline, setTimeline] = useState(null);
  const [mineProfile, setMineProfile] = useState(null);
  const [replayIndex, setReplayIndex] = useState(null); // null = full-archive aggregate mode
  const [playing, setPlaying] = useState(false);
  const [activeBookmark, setActiveBookmark] = useState(null);

  const extIdx = typeof externalReplayIndex === "object" && externalReplayIndex !== null
    ? externalReplayIndex.index
    : externalReplayIndex;
  const extSeq = typeof externalReplayIndex === "object" && externalReplayIndex !== null
    ? externalReplayIndex.seq
    : 0;

  useEffect(() => {
    if (extIdx !== null && extIdx !== undefined) {
      setPlaying(false);
      setReplayIndex(extIdx);
      if (timeline?.bookmarks) {
        const match = timeline.bookmarks.find(b => b.index === extIdx) || null;
        setActiveBookmark(match);
      }
    }
  }, [extIdx, extSeq, timeline]);

  useEffect(() => {
    const ctrl = new AbortController();
    Promise.all([
      apiRequest("/api/timeline", {signal: ctrl.signal}).catch(() => null),
      apiRequest("/api/mine-profile", {signal: ctrl.signal}).catch(() => null),
    ]).then(([tl, mp]) => {
      if (tl) setTimeline(tl);
      if (mp) setMineProfile(mp);
    });
    return () => ctrl.abort();
  }, []);

  useEffect(() => {
    if (!playing || !timeline?.times?.length) return undefined;
    const timer = window.setInterval(() => {
      setReplayIndex(curr => {
        const next = curr === null ? 0 : curr + 1;
        if (next >= timeline.times.length) {
          setPlaying(false);
          return timeline.times.length - 1;
        }
        return next;
      });
    }, 650);
    return () => window.clearInterval(timer);
  }, [playing, timeline]);

  const layout = mineProfile?.sld_layout || null;

  const byCab = useMemo(() => {
    const map = {};
    for (const c of health.cabinets_cards || []) {
      map[c.cabinet] = c;
    }
    return map;
  }, [health.cabinets_cards]);

  const metaByCab = useMemo(() => {
    const map = {};
    for (const f of mineProfile?.feeders || []) {
      map[f.cabinet] = f;
    }
    return map;
  }, [mineProfile]);

  const capSet = useMemo(() => new Set(layout?.capacitor_cabinets || []), [layout]);
  const spareSet = useMemo(() => new Set(layout?.spare_cabinets || []), [layout]);
  const downholeSet = useMemo(() => new Set(layout?.downhole_cabinets || []), [layout]);

  const hvLimit = useMemo(() => {
    const hvKv = layout?.hv_nominal_kv ?? 35.0;
    const lims = layout?.voltage_limits || [];
    return lims.find(l => Math.abs(l.nominal_kv - hvKv) < 2.0) || {nominal_kv: hvKv, lo_kv: 32.55, hi_kv: 38.5, operational_watch_hi_kv: 37.45};
  }, [layout]);

  const lvLimit = useMemo(() => {
    const lvKv = layout?.lv_nominal_kv ?? 10.0;
    const lims = layout?.voltage_limits || [];
    return lims.find(l => Math.abs(l.nominal_kv - lvKv) < 2.0) || {nominal_kv: lvKv, lo_kv: 9.3, hi_kv: 10.7, operational_watch_hi_kv: 10.7};
  }, [layout]);

  const bus = health.dimensions?.power_quality?.bus_segments;

  // Compute cabinets whose high/medium findings have ALL been confirmed as normal_operation in Episodic Memory
  const reviewsMap = health.reviews || {};
  const ackedCabs = new Set();
  for (const cab of Object.keys(byCab)) {
    const cabFindings = (health.findings || []).filter(
      f => (f.severity === "high" || f.severity === "medium") && String(f.cabinet || "").split("/").includes(cab),
    );
    if (
      cabFindings.length > 0 &&
      cabFindings.every(f => reviewsMap[f.id]?.disposition === "normal_operation")
    ) {
      ackedCabs.add(cab);
    }
  }

  const isLiveHour = replayIndex !== null && timeline?.times?.[replayIndex];
  const currentTimestamp = isLiveHour ? timeline.times[replayIndex] : null;
  const currentFile = isLiveHour ? timeline.files[replayIndex] : null;
  const currentSheet = isLiveHour ? timeline.sheets[replayIndex] : null;

  function readCabValues(cab) {
    const info = byCab[cab] || {cabinet: cab, label: metaByCab[cab]?.label || cab, metrics: {}, flags: []};
    const fMeta = metaByCab[cab];
    const isAcked = ackedCabs.has(cab);
    const isSpare = spareSet.has(cab) || info.group === "spare";
    const isCap = capSet.has(cab);
    const isHv = fMeta ? Math.abs(fMeta.nominal_kv - hvLimit.nominal_kv) < 2.0 : false;
    const isIncomer = fMeta?.role === "incomer";

    if (!isLiveHour || !timeline?.cabinets?.[cab]) {
      return {
        isHourly: false,
        P: info.metrics?.P?.avg,
        Ia: info.metrics?.Ia?.avg,
        Uab: info.metrics?.Uab?.avg,
        Umax: info.metrics?.Uab?.max,
        cos: info.metrics?.cos?.avg,
        cellP: null,
        tone: isAcked ? "ok" : (isSpare ? "spare" : info.level || "ok"),
        flag: isAcked ? "已确认方式 ✓" : (info.flags?.[0] || (isSpare ? "热备用" : "运行正常")),
      };
    }

    const snap = timeline.cabinets[cab];
    const P = snap.P?.[replayIndex];
    const Ia = snap.Ia?.[replayIndex];
    const Uab = snap.Uab?.[replayIndex];
    const cos = snap.cos?.[replayIndex];
    const cellP = snap.cells?.P?.[replayIndex];

    let tone = isSpare ? "spare" : "ok";
    let flag = isSpare ? "空载" : "正常";

    if (P == null && Ia == null && Uab == null) {
      tone = "medium";
      flag = "缺测/未知";
    } else if (isIncomer && P === 0 && Ia === 0) {
      tone = "high";
      flag = "零流(待核开关)";
    } else if (P != null && P < 0) {
      tone = "high";
      flag = `反向 ${P.toFixed(1)}kW`;
    } else if (!isHv && Uab != null && Uab > lvLimit.hi_kv) {
      tone = "high";
      flag = `过压 ${Uab.toFixed(2)}kV`;
    } else if (isHv && Uab != null && Uab > hvLimit.operational_watch_hi_kv) {
      tone = "medium";
      flag = `偏高 ${Uab.toFixed(2)}kV`;
    } else if (!isSpare && !isCap && P != null && P > 10 && cos != null && cos < 0.8) {
      tone = cos < 0.7 ? "high" : "medium";
      flag = `低cos ${cos.toFixed(2)}`;
    } else if (isCap && cos != null && Math.abs(cos) > 0.5) {
      tone = "high";
      flag = `cos异常 ${cos.toFixed(2)}`;
    }

    return {
      isHourly: true,
      P,
      Ia,
      Uab,
      Umax: Uab,
      cos,
      cellP,
      tone,
      flag,
    };
  }

  function matchesFilter(cab) {
    if (filterMode === "all") return true;
    if (filterMode === "downhole") {
      return downholeSet.has(cab);
    }
    const info = byCab[cab];
    if (!info) return false;
    const vals = readCabValues(cab);
    if (filterMode === "high") return vals.tone === "high";
    if (filterMode === "medium") return vals.tone === "medium";
    if (filterMode === "ok") return vals.tone === "ok" && !spareSet.has(cab);
    return true;
  }

  function jumpToBookmark(bm) {
    setPlaying(false);
    setReplayIndex(bm.index);
    setActiveBookmark(bm);
  }

  function resetToAggregate() {
    setPlaying(false);
    setReplayIndex(null);
    setActiveBookmark(null);
  }

  function renderPrimaryCabinet(cab, roleTag) {
    const info = byCab[cab] || {cabinet: cab, label: metaByCab[cab]?.label || cab, metrics: {}, flags: []};
    const vals = readCabValues(cab);
    const isSel = selectedCab === cab;
    const dimmed = !matchesFilter(cab);

    return <button
      key={cab}
      type="button"
      onClick={() => onSelectCabinet(cab)}
      aria-label={`${cab} 柜 ${info.label}`}
      className={
        "apple-node-card apple-node-primary" +
        ` is-${vals.tone}` +
        (isSel ? " is-selected" : "") +
        (dimmed ? " is-dimmed" : "")
      }
    >
      <div className="apple-node-top">
        <div className="apple-node-id">
          <span className={`apple-dot dot-${vals.tone}`} />
          <code className="mono">{cab}</code>
          <span className="apple-node-role">{roleTag || info.label}</span>
        </div>
        <span className={`apple-pill pill-${vals.tone}`}>
          {vals.flag}
          {vals.cellP ? ` · ${vals.cellP}` : ""}
        </span>
      </div>

      <div className="apple-node-metrics-row">
        <div className="apple-metric-col">
          <small>{vals.isHourly ? "当前整点有功 / 电流" : "归档均值有功 / 电流"}</small>
          <strong className="mono">
            {formatNum(vals.isHourly ? vals.P : Math.round(vals.P ?? 0))} kW ·{" "}
            {formatNum(vals.isHourly ? vals.Ia : Math.round(vals.Ia ?? 0))} A
          </strong>
        </div>
        <div className="apple-metric-col">
          <small>线电压 / 功率因数</small>
          <strong className="mono">
            {vals.Uab != null ? vals.Uab.toFixed(2) : "—"} kV · cos {vals.cos != null ? vals.cos.toFixed(2) : "—"}
          </strong>
        </div>
      </div>
    </button>;
  }

  function renderFeederCabinet(feederObj) {
    const cab = feederObj.cabinet;
    const info = byCab[cab] || {cabinet: cab, label: feederObj.label || cab, metrics: {}, flags: []};
    const vals = readCabValues(cab);
    const isSel = selectedCab === cab;
    const dimmed = !matchesFilter(cab);
    const isSpare = feederObj.role === "spare" || spareSet.has(cab);
    const isDownhole = Boolean(feederObj.downstream_bus_id) || downholeSet.has(cab);

    return <button
      key={cab}
      type="button"
      onClick={() => onSelectCabinet(cab)}
      aria-label={`${cab} 柜 ${info.label}`}
      className={
        "apple-node-card apple-node-feeder" +
        ` is-${vals.tone}` +
        (isSel ? " is-selected" : "") +
        (dimmed ? " is-dimmed" : "") +
        (isDownhole ? " is-downhole" : "")
      }
    >
      <div className="apple-node-top">
        <span className="apple-node-id">
          <span className={`apple-dot dot-${vals.tone}`} />
          <code className="mono">{cab}</code>
        </span>
        <span className={`apple-pill pill-${vals.tone}`}>
          {vals.flag}
          {vals.cellP ? `·${vals.cellP}` : ""}
        </span>
      </div>
      <div className="apple-feeder-name" title={info.label}>
        {info.label}
      </div>
      <div className="apple-feeder-stats mono">
        {isSpare ? (
          <span>U {formatNum(vals.Umax)} kV · 空载</span>
        ) : (
          <>
            <span>P {formatNum(vals.isHourly ? vals.P : Math.round(vals.P ?? 0))} kW</span>
            <span>cos {vals.cos != null ? vals.cos.toFixed(2) : "—"}</span>
          </>
        )}
      </div>
    </button>;
  }

  const bays = layout?.primary_bays || [];
  const lvSections = layout?.lv_sections || [];
  const corridors = layout?.inter_station_corridors || [];
  const downstreamStations = layout?.downstream_stations || [];
  const autoBalanceEqs = layout?.auto_balance_equations || [];

  const totalNodes =
    (mineProfile?.feeders?.length || (health.cabinets_cards?.length || 0)) +
    downstreamStations.reduce((acc, ds) => acc + (ds.total_cabinets || 0), 0);

  // Dynamically detect single-incomer tie-closed condition from layout incomers
  const allIncomers = bays.flatMap(b => b.incomers.map(i => i.cabinet));
  const zeroIncomerCab = isLiveHour ? allIncomers.find(c => readCabValues(c).P === 0 && readCabValues(c).Ia === 0) : null;
  const activeIncomerCab = isLiveHour ? allIncomers.find(c => (readCabValues(c).P ?? 0) > 100) : null;
  const isSingleIncomerHour = Boolean(zeroIncomerCab && activeIncomerCab);

  // Dynamically evaluate auto-generated KCL power conservation equations at current hour or aggregate
  const evaluatedEquations = autoBalanceEqs.map(eq => {
    const lhs = eq.lhs_cabinets.reduce((s, c) => s + (readCabValues(c).P ?? 0), 0);
    const rhs = eq.rhs_cabinets.reduce((s, c) => s + (readCabValues(c).P ?? 0), 0);
    const diff = lhs - rhs;
    return {
      ...eq,
      lhs_kw: lhs,
      rhs_kw: rhs,
      diff_kw: diff,
      ok: Math.abs(diff) <= eq.threshold_kw,
    };
  });

  const firstTimeLabel = timeline?.times?.[0] ? timeline.times[0].slice(5, 16) : "";
  const lastTimeLabel = timeline?.times?.length ? timeline.times[timeline.times.length - 1].slice(5, 16) : "";

  const lvRef0 = lvSections[0]?.lv_incomer;
  const lvRef1 = lvSections[1]?.lv_incomer;
  const vLv0 = lvRef0 ? readCabValues(lvRef0).Uab : null;
  const vLv1 = lvRef1 ? readCabValues(lvRef1).Uab : null;

  return <section className="apple-sld-container" aria-label={`${layout?.mine_name || "矿井配电网"} 电气主接线一次拓扑图`}>
    {/* Header & Filter Bar */}
    <div className="apple-sld-header">
      <div className="apple-sld-title">
        <div className="apple-title-row">
          <Layers size={18} className="apple-accent-icon" />
          <h2>
            {layout
              ? `${layout.mine_name} · ${layout.primary_station.name}${downstreamStations.length ? ` ➔ ${downstreamStations.map(d => d.station.name).join(" / ")}` : ""}（图模自动排版）`
              : "矿井配电网电气主接线一次拓扑（图模加载中…）"}
          </h2>
        </div>
        <p>
          100% 由 MineProfile 拓扑图模动态生成分段母线、主变、馈线、站间通道与功率守恒方程 · 点击任意开关柜呼出右侧滑出抽屉
        </p>
      </div>

      <div className="apple-sld-filters" role="tablist" aria-label="拓扑节点筛选">
        {[
          {id: "all", label: `全部节点 (${totalNodes})`, dot: null},
          {id: "high", label: "高优异常", dot: "high"},
          {id: "medium", label: "中优关注", dot: "medium"},
          {id: "downhole", label: "下井互联链路", dot: "blue"},
          {id: "ok", label: "运行正常", dot: "ok"},
        ].map(tab => (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={filterMode === tab.id}
            className={"apple-filter-pill" + (filterMode === tab.id ? " is-active" : "")}
            onClick={() => setFilterMode(tab.id)}
          >
            {tab.dot && <span className={`apple-dot dot-${tab.dot}`} />}
            <span>{tab.label}</span>
          </button>
        ))}
      </div>
    </div>

    {/* Auto-Derived Topology Power Conservation Bar */}
    {evaluatedEquations.length > 0 && (
      <div className="apple-timemachine-bar" style={{padding: "10px 14px", marginBottom: 12}}>
        <div style={{display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8}}>
          <span className="apple-tm-bm-label" style={{marginRight: 4}}>
            <Sparkles size={12} className="apple-accent-icon" />
            图模自动生成守恒规则（{isLiveHour ? `${currentTimestamp.slice(11, 16)} 断面` : "归档均值"}）：
          </span>
          {evaluatedEquations.map(eq => (
            <span
              key={eq.id}
              className={`apple-pill pill-${eq.ok ? "ok" : "medium"} mono`}
              title={eq.name}
            >
              {eq.lhs_cabinets.join("+")} ({Math.round(eq.lhs_kw)}kW) vs {eq.rhs_cabinets.length > 3 ? `${eq.rhs_cabinets.length}回出线` : eq.rhs_cabinets.join("+")} ({Math.round(eq.rhs_kw)}kW) · Δ{eq.diff_kw >= 0 ? "+" : ""}{Math.round(eq.diff_kw)}kW
            </span>
          ))}
        </div>
      </div>
    )}

    {/* Apple Time-Machine Dynamic Power-Flow Scrubber & Signature Bookmarks */}
    {timeline && (
      <div className="apple-timemachine-bar">
        <div className="apple-tm-top">
          <div className="apple-tm-controls">
            <button
              type="button"
              className={"apple-tm-play" + (playing ? " is-playing" : "")}
              onClick={() => {
                if (replayIndex === null) setReplayIndex(0);
                setPlaying(p => !p);
              }}
            >
              {playing ? <Pause size={13} /> : <Play size={13} />}
              <span>{playing ? "暂停回放" : `${timeline.times.length}h 动态潮流回放`}</span>
            </button>

            <div className="apple-tm-step-group">
              <button
                type="button"
                className="apple-tm-step"
                onClick={() => {
                  setPlaying(false);
                  setReplayIndex(i => (i === null ? 0 : Math.max(0, i - 1)));
                }}
                title="上一整点 (-1h)"
              >
                <ChevronLeft size={14} />
              </button>
              <button
                type="button"
                className="apple-tm-step"
                onClick={() => {
                  setPlaying(false);
                  setReplayIndex(i => (i === null ? 0 : Math.min(timeline.times.length - 1, i + 1)));
                }}
                title="下一整点 (+1h)"
              >
                <ChevronRight size={14} />
              </button>
            </div>

            {isLiveHour && (
              <button
                type="button"
                className="apple-tm-reset"
                onClick={resetToAggregate}
                title="返回全周期均值/峰值聚合态"
              >
                <RotateCcw size={12} />
                <span>返回全周期聚合视图</span>
              </button>
            )}
          </div>

          <div className="apple-tm-readout">
            <Clock size={14} className="apple-accent-icon" />
            {isLiveHour ? (
              <>
                <strong className="mono">{currentTimestamp.slice(0, 16)}</strong>
                <span className="apple-tm-idx mono">
                  第 {replayIndex + 1} / {timeline.times.length} 整点
                </span>
                {currentFile && (
                  <a
                    className="cell-chip"
                    href={sourceUrl(currentFile)}
                    download
                    title="下载当前整点所在原始 Excel 日报"
                  >
                    <FileSpreadsheet size={11} style={{marginRight: 4}} />
                    {currentFile} · {currentSheet}
                  </a>
                )}
              </>
            ) : (
              <span>
                当前显示：<strong>全周期聚合态（{timeline.times.length} 整点均值 / 极值）</strong> · 点击下方典型时刻或拖动滑块可实时回放断面潮流
              </span>
            )}
          </div>
        </div>

        <div className="apple-tm-slider-row">
          <span className="mono apple-tm-bound">{firstTimeLabel}</span>
          <input
            type="range"
            min={0}
            max={Math.max(0, timeline.times.length - 1)}
            value={replayIndex ?? 0}
            onChange={e => {
              setPlaying(false);
              setActiveBookmark(null);
              setReplayIndex(Number(e.target.value));
            }}
            aria-label="断面潮流时间轴"
          />
          <span className="mono apple-tm-bound">{lastTimeLabel}</span>
        </div>

        <div className="apple-tm-bookmarks">
          <span className="apple-tm-bm-label">
            <Sparkles size={12} className="apple-accent-icon" />
            一键跳转典型异常断面：
          </span>
          {(timeline.bookmarks || []).map(bm => {
            const active = isLiveHour && replayIndex === bm.index;
            return <button
              key={bm.id}
              type="button"
              onClick={() => jumpToBookmark(bm)}
              className={`apple-bm-chip tone-${bm.tone}` + (active ? " is-active" : "")}
              title={bm.desc}
            >
              <span className={`apple-dot dot-${bm.tone === "red" ? "high" : bm.tone === "orange" ? "medium" : "blue"}`} />
              <span>{bm.label}</span>
            </button>;
          })}
        </div>

        {activeBookmark && isLiveHour && replayIndex === activeBookmark.index && (
          <div className="apple-bm-callout">
            <strong>{activeBookmark.tag} · 断面电气特征：</strong>
            <span>{activeBookmark.desc}</span>
            <button
              type="button"
              className="apple-bm-inspect-btn"
              onClick={() => onSelectCabinet(activeBookmark.focus_cabinet)}
            >
              查看 {activeBookmark.focus_cabinet} 柜时序曲线 →
            </button>
          </div>
        )}
      </div>
    )}

    {/* Primary Substation Canvas (100% Auto-Generated from MineProfile) */}
    {layout && (
      <div className="apple-sld-zone">
        <div className="apple-zone-bar">
          <div className="apple-zone-title">
            <Zap size={15} className="apple-accent-icon" />
            <strong>
              {layout.primary_station.name}（{layout.primary_station.code} · {mineProfile?.feeders?.length ?? 0} 面开关柜 {isLiveHour ? `${currentTimestamp.slice(0, 16)} 断面实测` : "全周期小时级日报映射"}）
            </strong>
          </div>
          <div className="apple-aux-group">
            <span className="apple-aux-label">
              {layout.hv_nominal_kv} kV / {layout.lv_nominal_kv} kV · {bays.length} 段高压母线 · {lvSections.length} 段低压母线 · 100% 图模自动排版
            </span>
          </div>
        </div>

        {/* Tier 1 & 2: Dynamic HV Incomers -> HV Busbars -> Bus Ties -> Transformers -> LV Incomers */}
        <div className="apple-primary-grid">
          {bays.map((bay, bayIdx) => {
            const refCab = bay.hv_bus.reference_cabinet || bay.incomers[0]?.cabinet;
            const refVals = refCab ? readCabValues(refCab) : {Uab: null, Umax: null};
            const topCards = [...bay.incomers, ...bay.spares];
            const bayIsZero = bay.incomers.some(inc => isLiveHour && readCabValues(inc.cabinet).P === 0 && readCabValues(inc.cabinet).Ia === 0);

            return <React.Fragment key={bay.hv_bus.id}>
              {/* Insert Tie Bridge Column between bay 0 and bay 1 automatically */}
              {bayIdx === 1 && (
                <div className="apple-tie-column">
                  <div className={"apple-tie-card" + (isSingleIncomerHour ? " is-bridged" : "")}>
                    <span className="apple-tie-eyebrow">{layout.hv_nominal_kv} kV 母联联络</span>
                    <button
                      type="button"
                      className="apple-tie-btn"
                      onClick={() => onSelectCabinet(activeIncomerCab || allIncomers[0])}
                      title="点击查看主进线运行曲线"
                    >
                      <code className="mono">{layout.hv_ties[0]?.cabinet ? `${layout.hv_ties[0].cabinet} 母联` : `${layout.hv_nominal_kv} kV 母联`}</code>
                      <span>
                        {isSingleIncomerHour ? "⚡ 当前母联合环导通" : (layout.hv_ties[0]?.normal_closed ? "常规合环" : "常规分列 / 故障合环")}
                      </span>
                      <small>
                        {isSingleIncomerHour && activeIncomerCab
                          ? `${activeIncomerCab} 单回带双变 (${formatNum(readCabValues(activeIncomerCab).P)} kW)`
                          : (layout.hv_ties[0]?.description || "高压分段母联开关")}
                      </small>
                    </button>
                  </div>

                  {lvSections.length >= 2 && (
                    <div className="apple-tie-card">
                      <span className="apple-tie-eyebrow">{layout.lv_nominal_kv} kV 母联分段</span>
                      <div className="apple-tie-pair">
                        {lvSections.slice(0, 2).map(sec => {
                          const lc = sec.lv_incomer;
                          const lvVals = lc ? readCabValues(lc) : {tone: "ok"};
                          return lc ? (
                            <button key={sec.bus.id} type="button" className="apple-aux-chip" onClick={() => onSelectCabinet(lc)}>
                              <span className={`apple-dot dot-${lvVals.tone}`} />
                              <code className="mono">{lc}</code>
                              <span>{sec.bus.segment}</span>
                            </button>
                          ) : null;
                        })}
                      </div>
                      <small className="apple-tie-note">
                        {isLiveHour && vLv0 != null && vLv1 != null
                          ? `当前压差 = ${(vLv1 - vLv0 >= 0 ? "+" : "") + (vLv1 - vLv0).toFixed(2)} kV`
                          : bus?.delta_II_minus_I_kv != null
                            ? `分段压差 ${bus.delta_II_minus_I_kv >= 0 ? "+" : ""}${bus.delta_II_minus_I_kv.toFixed(3)} kV`
                            : "低压分段运行监测"}
                      </small>
                    </div>
                  )}
                </div>
              )}

              <div className="apple-bay-column">
                <div className={"apple-incomer-row" + (topCards.length > 1 ? " two-up" : "")}>
                  {topCards.map(item => renderPrimaryCabinet(item.cabinet, `${item.nominal_kv}kV ${item.label}`))}
                </div>

                <div className="apple-busbar-rail bus-35">
                  <div className="apple-busbar-title">
                    <span className="apple-bus-tag">{bay.hv_bus.name}</span>
                    <span className="mono">
                      {isLiveHour
                        ? `当前 ${refVals.Uab?.toFixed(2) ?? "—"} kV`
                        : `均值 ${refVals.Uab?.toFixed(2) ?? "—"} kV · 峰值 ${refVals.Umax?.toFixed(2) ?? "—"} kV`}
                    </span>
                  </div>
                  <Badge
                    variant={bayIsZero || (refVals.Uab != null && refVals.Uab > hvLimit.operational_watch_hi_kv) ? "warning" : "success"}
                    label={
                      bayIsZero
                        ? "由母联合环受电"
                        : refVals.Uab != null && refVals.Uab > hvLimit.operational_watch_hi_kv
                          ? `越 ${hvLimit.operational_watch_hi_kv} kV 关注线`
                          : "电压平稳"
                    }
                  />
                </div>

                <div className="apple-tr-stack">
                  {bay.transformers.map(tr => {
                    const hvVals = readCabValues(tr.hv_cabinet);
                    const lvVals = readCabValues(tr.lv_cabinet);
                    const isRev = isLiveHour && hvVals.P != null && hvVals.P < 0;
                    const trTone = isRev ? "high" : (lvVals.Uab != null && lvVals.Uab > lvLimit.hi_kv ? "warn" : "normal");
                    return <React.Fragment key={tr.id}>
                      {renderPrimaryCabinet(tr.hv_cabinet, tr.hv_label)}
                      <TransformerSymbol
                        title={`${tr.name} (${tr.default_vn_hv_kv}/${tr.default_vn_lv_kv} kV)`}
                        subtitle={
                          isLiveHour
                            ? `高压侧 ${formatNum(hvVals.P)} kW ➔ 低压侧 ${formatNum(lvVals.P)} kW`
                            : `均值负载 ${formatNum(Math.round(hvVals.P ?? 0))} kW · 额定参考 ${tr.default_sn_mva} MVA`
                        }
                        tone={trTone}
                      />
                      {renderPrimaryCabinet(tr.lv_cabinet, tr.lv_label)}
                    </React.Fragment>;
                  })}
                </div>
              </div>
            </React.Fragment>;
          })}
        </div>

        {/* Tier 3: Dynamic LV Busbars + Outgoing Feeders */}
        <div className="apple-10kv-split">
          {lvSections.map((sec, idx) => {
            const refCab = sec.lv_incomer || sec.bus.reference_cabinet;
            const refVals = refCab ? readCabValues(refCab) : {Uab: null, Umax: null};
            const overLim = refVals.Uab != null && refVals.Uab > lvLimit.hi_kv;
            const maxOverLim = !isLiveHour && refVals.Umax != null && refVals.Umax > lvLimit.hi_kv;
            return <div key={sec.bus.id} className="apple-10kv-half">
              <div className={`apple-busbar-rail ${idx === 0 ? "bus-10-i" : "bus-10-ii"}`}>
                <div className="apple-busbar-title">
                  <span className="apple-bus-tag">{sec.bus.name}</span>
                  <span className="mono">
                    {isLiveHour
                      ? `当前 ${refVals.Uab?.toFixed(2) ?? "—"} kV · 跟随 ${refCab} 进线`
                      : `均值 ${refVals.Uab?.toFixed(2) ?? "—"} kV · 峰值 ${refVals.Umax?.toFixed(2) ?? "—"} kV`}
                  </span>
                </div>
                <Badge
                  variant={overLim || maxOverLim ? "error" : "success"}
                  label={
                    overLim
                      ? `越限 > ${lvLimit.hi_kv} kV`
                      : maxOverLim
                        ? `存在超 ${lvLimit.hi_kv} kV 时段`
                        : "电压合格"
                  }
                />
              </div>

              <div className="apple-feeder-grid">
                {sec.feeders.map(renderFeederCabinet)}
              </div>
            </div>;
          })}
        </div>
      </div>
    )}

    {/* Inter-Station Cable Corridors (100% Auto-Generated from inter_station_links) */}
    {corridors.length > 0 && (
      <div className="apple-downhole-corridor" aria-label="站间互联供电电缆通道">
        {corridors.map(ln => {
          const v = readCabValues(ln.from_cabinet);
          const warn = v.tone === "high" || (v.cos != null && v.cos < 0.75);
          return <button
            key={`${ln.from_station}-${ln.from_cabinet}-${ln.to_cabinet}`}
            type="button"
            className={"apple-cable-pill" + (warn ? " is-warn" : "")}
            onClick={() => onSelectCabinet(ln.from_cabinet)}
          >
            <span className={`apple-dot dot-${v.tone}`} />
            <ArrowDown size={14} />
            <strong>
              {ln.description || `${ln.from_label} 互联电缆`}：{ln.from_station}.{ln.from_cabinet} ➔ {ln.to_station}.{ln.to_cabinet}
            </strong>
            <span className="mono">
              {isLiveHour
                ? `当前 ${formatNum(v.P)} kW · ${formatNum(v.Ia)} A · cosφ ${v.cos?.toFixed(2) ?? "—"}`
                : `均值 ${formatNum(Math.round(v.P ?? 0))} kW · cosφ ${v.cos?.toFixed(2) ?? "—"} · 参考长度 ${ln.default_cable_length_km} km`}
            </span>
          </button>;
        })}
      </div>
    )}

    {/* Downstream / Underground Stations (100% Auto-Generated from MineProfile + Archive) */}
    {downstreamStations.map(ds => (
      <div key={ds.station.code} className="apple-sld-zone is-underground">
        <div className="apple-zone-bar">
          <div className="apple-zone-title">
            <Cpu size={15} className="apple-accent-icon" />
            <strong>
              {ds.station.name}（{ds.station.code} · {ds.total_cabinets} 面开关柜拓扑映射）
            </strong>
          </div>
          <span className="apple-zone-note">
            {ds.station.has_hourly_archive
              ? "已接入小时级运行报表，点击任意柜号查看曲线"
              : "注：当前小时级报表仅含主站，点击下级站柜号可查看上下级供电关系与证据边界"}
          </span>
        </div>

        <div className="apple-c1535-layout">
          {ds.sections.map((sec, idx) => (
            <React.Fragment key={sec.bus.id}>
              {idx === 1 && ds.ties.length > 0 && (
                <div className="apple-c1535-tie">
                  {ds.ties.map(t => (
                    <button
                      key={t.id}
                      type="button"
                      onClick={() => onSelectCabinet(t.cabinet)}
                      className={"apple-c1535-tie-btn" + (selectedCab === t.cabinet ? " is-selected" : "")}
                    >
                      <code className="mono">{t.cabinet}</code>
                      <span>母线联络柜</span>
                    </button>
                  ))}
                </div>
              )}
              <div className="apple-c1535-half">
                <div className="apple-c1535-bus">
                  {sec.bus.name}{sec.upstream_cabinet ? `（受电自上级 ${sec.upstream_cabinet} 柜）` : ""}
                </div>
                <div className="apple-c1535-cards">
                  {sec.cabinets.map(c => {
                    const isSel = selectedCab === c.cabinet;
                    const dimmed = !matchesFilter(c.cabinet);
                    return <button
                      key={c.cabinet}
                      type="button"
                      onClick={() => onSelectCabinet(c.cabinet)}
                      className={
                        "apple-c1535-card" +
                        (c.is_incomer ? (idx === 0 ? " is-incomer" : " is-incomer-warn") : "") +
                        (isSel ? " is-selected" : "") +
                        (dimmed ? " is-dimmed" : "")
                      }
                    >
                      <div className="apple-c1535-top">
                        <span className={`apple-dot ${c.is_incomer ? (idx === 0 ? "dot-medium" : "dot-high") : "dot-blue"}`} />
                        <code className="mono">{c.cabinet}</code>
                      </div>
                      <strong>{c.label}</strong>
                      <small>{c.is_incomer && sec.upstream_cabinet ? `接上级 ${sec.upstream_cabinet}` : (ds.station.has_hourly_archive ? "小时遥测" : "仅截图拓扑")}</small>
                    </button>;
                  })}
                </div>
              </div>
            </React.Fragment>
          ))}
        </div>
      </div>
    ))}
  </section>;
}
