import React, {lazy, Suspense, useEffect, useState} from "react";
import {createRoot} from "react-dom/client";
import {
  Activity,
  ArrowLeft,
  BarChart3,
  ChevronDown,
  ClipboardCheck,
  Cpu,
  FlaskConical,
  GitBranch,
  Layers,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  ShieldCheck,
  Sun,
} from "lucide-react";
import {Button} from "@astryxdesign/core/Button";
import {Card} from "@astryxdesign/core/Card";
import {Spinner} from "@astryxdesign/core/Spinner";
import {Theme} from "@astryxdesign/core/theme";
import {neutralTheme} from "@astryxdesign/theme-neutral/built";
import "@astryxdesign/core/reset.css";
import "@astryxdesign/core/astryx.css";
import "@astryxdesign/theme-neutral/theme.css";
import "./workbench.css";
import "./duty-layout.css";

import {AgentCopilotPane} from "./AgentCopilotPane.jsx";
import {apiRequest} from "./api.js";
import {CabinetDrawer} from "./CabinetDrawer.jsx";

const CabinetView = lazy(() => import("./CabinetView.jsx").then(module => ({default: module.CabinetView})));
const GovernanceMultiAgentPanel = lazy(() => import("./GovernanceMultiAgentPanel.jsx").then(module => ({default: module.GovernanceMultiAgentPanel})));
const HealthView = lazy(() => import("./HealthView.jsx").then(module => ({default: module.HealthView})));
const IncidentTicketPanel = lazy(() => import("./IncidentTicketPanel.jsx").then(module => ({default: module.IncidentTicketPanel})));
const PowerFlowPanel = lazy(() => import("./PowerFlowPanel.jsx").then(module => ({default: module.PowerFlowPanel})));
const ReplayView = lazy(() => import("./ReplayView.jsx").then(module => ({default: module.ReplayView})));
const SupplementEvalView = lazy(() => import("./SupplementEvalView.jsx").then(module => ({default: module.SupplementEvalView})));

const DUTY_TABS = [
  {id: "topology", Icon: Layers, label: "主接线巡检"},
  {id: "tickets", Icon: ClipboardCheck, label: "事件工单"},
  {id: "cabinet", Icon: Activity, label: "柜级档案"},
];

const RESEARCH_TABS = [
  {id: "powerflow", Icon: Cpu, label: "交流潮流"},
  {id: "graph", Icon: GitBranch, label: "状态图补证"},
  {id: "eval", Icon: BarChart3, label: "离线评测"},
  {id: "governance", Icon: ShieldCheck, label: "多智体与治理"},
];

const ALL_TABS = [...DUTY_TABS, ...RESEARCH_TABS];

function App() {
  const [darkMode, setDarkMode] = useState(false);
  const [canvasTab, setCanvasTab] = useState(null);
  const [sidebarOpen, setSidebarOpen] = useState(() => !window.matchMedia("(max-width: 760px)").matches);
  const [unreadBadge, setUnreadBadge] = useState(0);
  const [cabinetSubTab, setCabinetSubTab] = useState("profile");
  const [catalog, setCatalog] = useState(null);
  const [dayFilter, setDayFilter] = useState("");
  const [health, setHealth] = useState(null);
  const [catalogError, setCatalogError] = useState("");
  const [healthError, setHealthError] = useState("");
  const [retry, setRetry] = useState(0);

  const [focusCabinet, setFocusCabinet] = useState("1018");
  const [drawerCabinet, setDrawerCabinet] = useState(null);
  const [agentReplayIndex, setAgentReplayIndex] = useState(null);
  const [pendingQuestion, setPendingQuestion] = useState("");

  useEffect(() => {
    document.documentElement.dataset.theme = darkMode ? "dark" : "light";
  }, [darkMode]);

  useEffect(() => {
    const ctrl = new AbortController();
    apiRequest("/api/dates", {signal: ctrl.signal})
      .then(res => {
        setCatalog(res);
        setCatalogError("");
      })
      .catch(e => {
        if (e.name !== "AbortError") setCatalogError(e.message);
      });
    return () => ctrl.abort();
  }, [retry]);

  useEffect(() => {
    const ctrl = new AbortController();
    const qs = dayFilter ? "?day=" + encodeURIComponent(dayFilter) : "";
    apiRequest("/api/health" + qs, {signal: ctrl.signal})
      .then(res => {
        setHealth(res);
        setHealthError("");
      })
      .catch(e => {
        if (e.name !== "AbortError") setHealthError(e.message);
      });
    return () => ctrl.abort();
  }, [dayFilter, retry]);

  useEffect(() => {
    const ctrl = new AbortController();
    apiRequest("/api/proactive/status", {signal: ctrl.signal})
      .then(res => setUnreadBadge(res.unread_badge_count || 0))
      .catch(() => {});
    return () => ctrl.abort();
  }, [retry]);

  useEffect(() => {
    function closeMobileNavigation(event) {
      if (event.key === "Escape" && window.matchMedia("(max-width: 760px)").matches) {
        setSidebarOpen(false);
      }
    }
    window.addEventListener("keydown", closeMobileNavigation);
    return () => window.removeEventListener("keydown", closeMobileNavigation);
  }, []);

  function finishNavigation() {
    if (window.matchMedia("(max-width: 760px)").matches) setSidebarOpen(false);
  }

  function showChat() {
    setCanvasTab(null);
    finishNavigation();
  }

  function openCabinetDrawer(cab) {
    setFocusCabinet(cab);
    setDrawerCabinet(cab);
  }

  function askAgent(question) {
    showChat();
    setPendingQuestion(question);
  }

  function syncCanvasFromAgent(hint) {
    if (!hint) return;
    if (hint.focus_cabinet) setFocusCabinet(hint.focus_cabinet);
    if (hint.replay_index !== undefined && hint.replay_index !== null) {
      setAgentReplayIndex(prev => ({
        index: hint.replay_index,
        seq: (prev?.seq || 0) + 1,
      }));
    }
    if (hint.canvas && ALL_TABS.some(t => t.id === hint.canvas)) {
      setCanvasTab(hint.canvas);
      finishNavigation();
      if (hint.canvas === "cabinet") setCabinetSubTab("profile");
    }
  }

  function handleReviewsUpdate(nextReviews) {
    setHealth(curr => (curr ? {...curr, reviews: nextReviews} : curr));
  }

  const activeTab = ALL_TABS.find(tab => tab.id === canvasTab);
  const dates = catalog?.dates || [];
  const earliest = dates[0];
  const latest = dates.at(-1);
  const shortDate = date => date.slice(5).replace("-", "/");
  const archiveRange = earliest
    ? (earliest === latest ? shortDate(earliest) : shortDate(earliest) + "–" + shortDate(latest))
    : "";
  const error = catalogError || healthError;

  function workspaceLink({id, Icon, label}) {
    return <button
      key={id}
      type="button"
      className="duty-side-link"
      aria-current={canvasTab === id ? "page" : undefined}
      onClick={() => {
        setCanvasTab(id);
        finishNavigation();
      }}
    >
      <Icon size={17} strokeWidth={1.65} />
      <span>{label}</span>
      {id === "tickets" && unreadBadge > 0 && <span className="duty-unread" aria-label={`${unreadBadge} 条待处理工单`}>{unreadBadge}</span>}
    </button>;
  }

  return <Theme theme={neutralTheme} mode={darkMode ? "dark" : "light"}>
    <div className="duty-shell" data-theme={darkMode ? "dark" : "light"}>
      <header className="duty-topbar">
        <div className="duty-topbar-leading">
          <button
            type="button"
            className="duty-icon-button"
            aria-label={sidebarOpen ? "收起侧栏" : "展开侧栏"}
            aria-expanded={sidebarOpen}
            aria-controls="duty-sidebar"
            onClick={() => setSidebarOpen(open => !open)}
          >
            {sidebarOpen ? <PanelLeftClose size={19} /> : <PanelLeftOpen size={19} />}
          </button>
          {activeTab
            ? <button type="button" className="duty-back-link" onClick={showChat}><ArrowLeft size={16} /> 值班问答</button>
            : <span className="duty-page-title">值班问答</span>}
        </div>
        <div className="duty-topbar-trailing">
          <span className="duty-source-mode"><span aria-hidden="true" />历史回放</span>
          <button
            type="button"
            className="duty-icon-button"
            aria-label={darkMode ? "切换浅色外观" : "切换深色外观"}
            onClick={() => setDarkMode(mode => !mode)}
          >
            {darkMode ? <Sun size={18} /> : <Moon size={18} />}
          </button>
        </div>
      </header>
      {sidebarOpen && <button type="button" aria-label="关闭导航" className="duty-sidebar-backdrop" onClick={finishNavigation} />}

      <AgentCopilotPane
        health={health}
        model={catalog?.model}
        pendingQuestion={pendingQuestion}
        clearPendingQuestion={() => setPendingQuestion("")}
        onSyncCanvas={syncCanvasFromAgent}
        onOpenCabinet={openCabinetDrawer}
        onReviewsUpdate={handleReviewsUpdate}
        sidebarOpen={sidebarOpen}
        chatVisible={!activeTab}
        onShowChat={showChat}
        archiveRange={archiveRange}
        connectionError={catalogError}
        onRetry={() => setRetry(n => n + 1)}
        navigation={<>
          <nav className="duty-side-nav" aria-label="值班工具">
            {DUTY_TABS.map(workspaceLink)}
          </nav>
          <details className="duty-research">
            <summary className="duty-side-link">
              <FlaskConical size={17} strokeWidth={1.65} />
              <span>研究工具</span>
              <ChevronDown size={14} className="duty-nav-chevron" />
            </summary>
            <nav className="duty-research-nav" aria-label="研究工具">
              {RESEARCH_TABS.map(workspaceLink)}
            </nav>
          </details>
        </>}
      />

      {activeTab && <main className="agent-artifact-canvas duty-workspace" aria-label={activeTab.label}>
        {error ? (
          <Card padding={24} className="center-state">
            <p role="alert">{error}</p>
            <Button variant="primary" label="重新加载" onClick={() => setRetry(n => n + 1)} />
          </Card>
        ) : !catalog || !health ? (
          <div className="duty-loading" role="status"><Spinner size="sm" />正在读取档案</div>
        ) : <Suspense fallback={<div className="duty-loading" role="status"><Spinner size="sm" />正在打开工具</div>}>
          {canvasTab === "topology" && (
            <HealthView
              health={health}
              dates={catalog.dates || []}
              dayFilter={dayFilter}
              setDayFilter={setDayFilter}
              onReviewsUpdate={handleReviewsUpdate}
              onAskDiagnose={askAgent}
              onOpenCabinet={openCabinetDrawer}
              externalReplayIndex={agentReplayIndex}
              focusCabinet={focusCabinet}
            />
          )}

          {canvasTab === "cabinet" && (
            <div className="view-stack">
              <div className="canvas-subbar">
                <div className="apple-sld-filters">
                  <button
                    type="button"
                    className={"apple-filter-pill" + (cabinetSubTab === "profile" ? " is-active" : "")}
                    onClick={() => setCabinetSubTab("profile")}
                  >
                    <span>柜级画像</span>
                  </button>
                  <button
                    type="button"
                    className={"apple-filter-pill" + (cabinetSubTab === "replay" ? " is-active" : "")}
                    onClick={() => setCabinetSubTab("replay")}
                  >
                    <span>历史回放与原图</span>
                  </button>
                </div>
              </div>
              {cabinetSubTab === "profile" ? (
                <CabinetView
                  health={health}
                  initialCabinet={focusCabinet}
                  onAskDiagnose={askAgent}
                />
              ) : (
                <ReplayView
                  dates={catalog.dates || []}
                  model={catalog.model}
                  onAskDiagnose={askAgent}
                />
              )}
            </div>
          )}

          {canvasTab === "tickets" && (
            <IncidentTicketPanel
              onBadgeUpdate={setUnreadBadge}
              onAskDiagnose={askAgent}
            />
          )}

          {canvasTab === "powerflow" && <PowerFlowPanel onAskDiagnose={askAgent} />}
          {canvasTab === "governance" && <GovernanceMultiAgentPanel />}
          {canvasTab === "graph" && <SupplementEvalView section="graph" />}
          {canvasTab === "eval" && <SupplementEvalView section="eval" />}
        </Suspense>}
      </main>}

      <CabinetDrawer
        cabinet={drawerCabinet}
        onClose={() => setDrawerCabinet(null)}
        onAskDiagnose={askAgent}
      />
    </div>
  </Theme>;
}

createRoot(document.getElementById("root")).render(<App />);
