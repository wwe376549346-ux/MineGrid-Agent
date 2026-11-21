<div align="center">

# MineGrid-Agent

**面向煤矿配电网运行健康监测、交流潮流优化与反事实诊断的证据约束型多智能体平台**

[![CI](https://github.com/wwe376549346-ux/MineGrid-Agent/actions/workflows/ci.yml/badge.svg)](https://github.com/wwe376549346-ux/MineGrid-Agent/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-OpenAPI%203.1-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React 19](https://img.shields.io/badge/React-19%20%2B%20Meta%20Astryx-61DAFB?logo=react&logoColor=black)](https://react.dev/)
[![pandapower](https://img.shields.io/badge/AC%20Power%20Flow-pandapower%203.x-orange)](https://www.pandapower.org/)
[![MCP](https://img.shields.io/badge/MCP-JSON--RPC%202.0%20Read--Only-6B4FBB)](https://modelcontextprotocol.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

<p>
  <a href="README.md">English</a> |
  <strong>简体中文</strong> |
  <a href="README.ja.md">日本語</a> |
  <a href="README.ko.md">한국어</a>
</p>

</div>

---

**MineGrid-Agent** 是一个面向煤矿 35/10 kV 配电网持续健康监测、证据约束型根因诊断、交流潮流优化与值班决策支持的垂直领域神经-符号多智能体系统。

系统基于真实小时级 SCADA 运行归档（地面 35 kV 变电所 25 柜、井下 1535 中央变电所 15 柜、16,800 条小时遥测、2,100 条页尾统计、一次系统接线图以及 68 条《煤矿安全规程（2025）》《GB/T 50417-2017》《GB/T 12325》条款），将大语言模型推理与确定性电气核验、非线性牛顿-拉夫逊交流潮流求解器（`pandapower`）及人机协同状态图深度融合，彻底消除工业电力场景中的数值幻觉与无依据归因。

---

## 系统架构

```mermaid
flowchart LR
  subgraph Data["只读运行归档与领域知识底座"]
    A1["小时级 SCADA 运行日报 (.xls → SQLite)<br/>16,800 整点 · 2,100 页尾统计"]
    A2["一次主接线图 (.png)<br/>地面 25 柜 · 井下 15 柜"]
    A3["68 条煤矿安规与国标<br/>MineProfile 声明式矿井图模"]
  end

  subgraph Core["MineGrid-Agent 自主推理与物理计算层"]
    B1["自主深度 Agent + 只读 Python/pandapower 沙盒<br/>动态计划树 · 报错自修 · 内嵌 SVG 图表"]
    B2["并行子智能体辩论与多智体仲裁<br/>量测取证 ⇄ 交流潮流仿真 ⇄ 安规对抗挑刺"]
    B3["168h 离散 DP 调压投容 & VoI 灵敏度<br/>6 组帕累托前沿 · 5 参数补证排序"]
    B4["三层持久化运营记忆 & 6 阶段深度专项排查<br/>Auto-Memory 提炼 · 可打印 HTML/MD 报告"]
    B5["可插拔 Markdown 技能 & 只读工业协议接入<br/>Modbus TCP / OPC-UA / MQTT · MCP 双向生态"]
  end

  subgraph Guard["双层核验、审批治理与交互工作台"]
    C1["句级证据槽位绑定 + Patch-Only Reflection<br/>原表单元格坐标与数值双重校核"]
    C2["标准化电气两票人工逐项审批流<br/>未勾全安全前置强制拦截 (HTTP 409)"]
    C3["React 19 工作台 · FastAPI (OpenAPI 3.1) · Docker"]
  end

  Data --> Core --> Guard
```

---

## 核心能力

1. **自主深度 Agent 与受控只读 Python 分析沙盒**：支持「自主深度 Agent」与「严格审计单」双模式一键切换；在 AST 静态审计、脱敏子进程与只读 SQLite 沙盒中现场编写 Python/SQL/NumPy/SciPy/`pandapower` 脚本，完成相关性回归、负荷曲线波动排行、自定义潮流试算并直接在对话气泡渲染纯 SVG 图表与表格。
2. **句级神经-符号证据核验与 Patch-Only Reflection**：每个数值与跨时差值强制绑定至原表单元格或只读工具回执，对未通过断言执行局部增量修补，阻断 100% 数值与坐标幻觉。
3. **并行子智能体辩论与交流潮流联合寻优**：并行派生量测取证、潮流仿真与安规对抗挑刺子智能体开展结构化质疑与仲裁；168 小时离散动态规划将全周过压整点由 **35 h 清零至 0 h**，累计节电 **109.02 kWh（+1.85%）**，并按信息价值（VoI）量化现场待补台账优先级。
4. **三层持久化长期记忆（含 Auto-Memory）与 6 阶段深度专项排查**：持久化存储矿井定制考核红线、历史排障案例与运维 SOP，对话中自动识别新规则一键入库；6 阶段长程排查一键导出可打印 HTML / Markdown 专项报告。
5. **实时态势演练、只读工业协议接入与标准化两票审批**：支持增量 CSV 上传与 Modbus TCP / OPC-UA / MQTT 只读遥测帧接入隔离沙盒体检；越限时即时预诊断并起草《电气倒闸操作票草案》《二次缺陷消缺单》《现场台账补录单》，全部必核安全项经人工勾选后方允许签发，严禁无人值守遥控开关。
6. **可插拔 Markdown 技能库与双向 MCP 生态**：支持热加载 `skills/*.md` 矿井值班技能，对外暴露 15 个严格只读 MCP JSON-RPC 2.0 工具，同时支持注册探测外部只读 MCP Server。

---

## 快速开始

### 方式一：Docker 一键启动（推荐快速体验）

```bash
git clone git@github.com:wwe376549346-ux/MineGrid-Agent.git
cd MineGrid-Agent
docker compose up --build
# 浏览器访问 http://127.0.0.1:8765/
```

### 方式二：本地 Python 虚拟环境启动

```bash
git clone git@github.com:wwe376549346-ux/MineGrid-Agent.git
cd MineGrid-Agent
bash 研究实施/老洼地数据接入_20261119/scripts/setup_collab.sh

# 启动交互工作台
cd 研究实施/老洼地数据接入_20261119
../.venv-power/bin/python -B -m laowadi workbench --port 8765 --workers 1 --external-workers 1
```

可选配置 OpenAI 兼容大模型端点（本地离线模式无需 API Key 即可完整运行）：编辑 `研究实施/大模型接入_20261115/.env.local` 填入 `COAL_API_BASE_URL`、`COAL_API_MODEL` 与 `COAL_API_KEY`。

---

## 安全与只读数据边界

- **黄金归档库只读不可变**：核心 SQLite 归档以 `mode=ro` 打开并在每次门禁与 CI 中校验 `SHA-256`。
- **零无人值守遥控执行**：所有 15 项诊断/MCP 工具与工业协议适配器均严格只读，两票草案必须经人工逐项勾选审批。
- **MIT 开源许可**：详见 [LICENSE](LICENSE)。
