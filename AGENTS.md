# 煤矿配电网项目约定

- 当前老洼地开发、校准和验收使用用户已经提供的七份运行日报和两张监控截图。回放事件属于这些资料的派生证据。设备参数、运行日志或开关状态缺失时，按证据不足记录。前期构造算例、DS01 和离线检索题只用于追溯方案和回归代码。
- 项目主线是对煤矿日常供电数据持续开展健康监测。数据接入、证据化筛查、主动反馈和值班问答共同验收。当前以老洼地四柜小时级回放推进。连续监测、现场响应时延和故障识别效果须用后续老洼地数据及人工复核验证。
- 后续值班工作台前端使用 Meta Astryx（React 19+）。知识库的现场资料入口为 `项目知识库/老洼地智能体知识库.html#materials`。原件和整点预览存放在同目录下的 `资料/老洼地/`，随本地知识库一并保留。
- 大模型沿用用户提供的 DeepSeek 配置（`deepseek-v4-pro`）。密钥沿用已有本地配置文件。真实模型负责选择候选只读查询，数据和证据校验由本地工具处理。用老洼地同题结果和延迟决定是否纳入默认问答。
- 每完成一项项目任务，在交付前同步更新 `项目知识库/老洼地智能体知识库.html`。更新相关开发卡的现状、代码或交付物入口、验收条件，并向 `updates` 追加日期、实际产出、已完成的验证、待办和对应阶段。待办保留实际状态。
- 更新知识库时沿用现有文件、阶段 ID 和浏览器存储键，保留用户的个人笔记。需要修改验收项时，保留语义相同的原项。

## 课题组双人协同开发约定

- **只读黄金数据源红线**：`研究实施/老洼地数据接入_20261119/local/laowadi.sqlite3`（日报归档库）与 `laowadi_events.sqlite3`（事件归档库）以及 `项目知识库/资料/老洼地/` 下的 9 份原件为全组共用的 100% 只读基准，SHA-256 严禁变动。运行时动态产生的会话、队列、检查点、审计与工单数据库（`laowadi_jobs.sqlite3`、`laowadi_sessions.sqlite3` 等）已由 `.gitignore` 隔离在各自本机，严禁强行提交入库。
- **密钥与本地配置隔离**：大模型密钥（`研究实施/大模型接入_20261115/.env.local`）与本地数据库连接配置（`local/.db.env`）仅保存在个人本机，绝不提交到 Git 仓库。新成员克隆仓库后运行 `bash 研究实施/老洼地数据接入_20261119/scripts/setup_collab.sh` 初始化环境。
- **模块分工与接口契约**：
  - 涉及智能体核心推理、核验与评测实验时，主改 `laowadi/diagnose.py`、`laowadi/diagnostic_claims.py`、`laowadi/hypothesis.py`、`laowadi/verifier.py`、`laowadi/context.py`、`laowadi/rag.py` 及 `thesis_eval/`；
  - 涉及电力潮流机理、多智能体协同、状态图补证与工作台可视化时，主改 `laowadi/powerflow.py`、`laowadi/multi_agent.py`、`laowadi/reactive_collaboration.py`、`laowadi/supplement.py`、`laowadi/graph.py` 及 `workbench/src/`；
  - 跨模块调用严格遵循 `laowadi/diag_tools.py` 中的 `DIAGNOSTIC_TOOL_SCHEMAS`（只读工具输入输出字典契约）。
- **合并前必跑门禁**：在向 `main` 分支提交或合并代码前，必须运行 `python -B verify_p0_correctness.py`、`python -B verify_diagnostic_claims.py`、`python -B verify_multi_agent_and_governance.py`、`python -B verify_workbench.py` 与 `python -B -m thesis_eval.probe --output thesis_eval/results` 并确保 100% 通过。
