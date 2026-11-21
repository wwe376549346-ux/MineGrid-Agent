<div align="center">

# MineGrid-Agent

**탄광 배전망 건전성 모니터링·교류 조류 최적화·반사실 진단을 위한 근거 제약형 멀티 에이전트 플랫폼**

[![CI](https://github.com/wwe376549346-ux/MineGrid-Agent/actions/workflows/ci.yml/badge.svg)](https://github.com/wwe376549346-ux/MineGrid-Agent/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-OpenAPI%203.1-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React 19](https://img.shields.io/badge/React-19%20%2B%20Meta%20Astryx-61DAFB?logo=react&logoColor=black)](https://react.dev/)
[![pandapower](https://img.shields.io/badge/AC%20Power%20Flow-pandapower%203.x-orange)](https://www.pandapower.org/)
[![MCP](https://img.shields.io/badge/MCP-JSON--RPC%202.0%20Read--Only-6B4FBB)](https://modelcontextprotocol.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

<p>
  <a href="README.md">English</a> |
  <a href="README.zh-CN.md">简体中文</a> |
  <a href="README.ja.md">日本語</a> |
  <strong>한국어</strong>
</p>

</div>

---

**MineGrid-Agent**는 탄광 35/10 kV 배전망의 상시 건전성 모니터링, 근거 기반 원인 진단, 비선형 교류 조류 최적화 및 운전원 의사결정 지원을 위해 설계된 뉴로-심볼릭 멀티 에이전트 시스템입니다.

실제 시간별 SCADA 운전 아카이브(지상 35 kV 변전소 25개 배전반, 지하 1535 중앙변전소 15개 배전반, 16,800건 시간별 계측 데이터, 단선결선도 및 68개 안전 규정·국가표준)를 읽기 전용 골든 기준으로 삼아 LLM 추론, 결정론적 전기 검증, `pandapower` 뉴턴-랩슨 교류 조류 해석기 및 HITL 상태 그래프를 결합하여 산업 전력 환경의 수치 환각을 원천 차단합니다.

---

## 핵심 기능

- **자율형 에이전트 & 읽기 전용 Python / pandapower 샌드박스**: AST 정적 보안 감사와 격리된 서브프로세스 환경에서 상관·회귀 분석, 부하 변동성 랭킹, 맞춤형 조류 시뮬레이션을 실시간 실행하고 대화창에 순수 SVG 차트(산점도·선·막대)와 표를 즉시 렌더링합니다.
- **문장 단위 근거 바인딩 & Patch-Only Reflection**: 모든 수치와 시간차 변화량을 원본 Excel 셀 좌표 또는 도구 계산 영수증에 엄격히 바인딩하고, 검증 실패 항목만 국소 수정합니다.
- **병렬 서브에이전트 토론 & 168시간 이산 최적화 / VoI 민감도 분석**: 계측·조류 물리·안전 규정 3개 병렬 서브에이전트가 교차 반박 및 중재를 수행합니다. 7일(168시간) 탭·커패시터 동적 계획 최적화로 과전압 위반을 **35시간 → 0시간**으로 제거하고 전력 손실을 **109.02 kWh (+1.85%)** 절감합니다.
- **3계층 영구 운영 메모리(Auto-Memory) & 6단계 심층 조사 리포트**: 사용자 지정 전압 기준선, 과거 장애 교훈, 운영 SOP를 자동 추출·저장하며 인쇄용 HTML / Markdown 종합 진단 보고서를 원클릭으로 내보냅니다.
- **실시간 텔레메트리 시뮬레이션·산업 프로토콜(읽기 전용) & 작업표 승인 워크플로우**: 증분 CSV 업로드와 Modbus TCP / OPC-UA / MQTT 읽기 전용 프레임의 격리 진단을 지원합니다. 전기 조작표 초안은 운전원이 모든 필수 안전 점검 항목을 직접 체크해야만 승인(미체크 시 HTTP 409 차단)됩니다.
- **플러그형 Markdown 스킬 & 양방향 MCP 생태계**: `skills/*.md` 운영 플레이북 핫로딩 및 15개 읽기 전용 MCP JSON-RPC 2.0 도구를 제공합니다.

---

## 빠른 시작

### Docker 원클릭 실행

```bash
git clone git@github.com:wwe376549346-ux/MineGrid-Agent.git
cd MineGrid-Agent
docker compose up --build
# 브라우저에서 http://127.0.0.1:8765/ 접속
```

### 로컬 Python 가상환경 실행

```bash
git clone git@github.com:wwe376549346-ux/MineGrid-Agent.git
cd MineGrid-Agent
bash 研究实施/老洼地数据接入_20261119/scripts/setup_collab.sh

cd 研究实施/老洼地数据接入_20261119
../.venv-power/bin/python -B -m laowadi workbench --port 8765 --workers 1 --external-workers 1
```

> **보안 원칙**: 골든 SQLite 아카이브는 항상 `mode=ro`(SHA-256 무결성 검증)로 보호되며, 모든 제어 권고안은 운전원의 명시적 승인을 필수 조건으로 합니다. 라이선스는 [MIT License](LICENSE)를 따릅니다.
