"""Strong-typed Pydantic v2 API contracts for the Laowadi Enterprise FastAPI backend."""

from __future__ import annotations

from datetime import date, datetime
import json
import math
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class HealthzResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str = "laowadi-power-agent-api"
    version: str = "2.0.0"
    engine: str = "fastapi+uvicorn"
    mine_id: str
    model: str | None = None
    pandapower_version: str | None = None
    uptime_seconds: float


class ReadyzResponse(BaseModel):
    status: Literal["ready", "degraded"]
    archive_ok: bool
    events_db_ok: bool
    profile_consistent: bool
    frontend_built: bool
    days_count: int
    cabinet_count: int
    backend: Literal["sqlite", "postgresql"] = "sqlite"
    runtime_ok: bool | None = None
    external_worker_ok: bool | None = None
    source_originals_ok: bool | None = None
    archive_version: str | None = None
    latest_reading_at: str | None = None
    freshness_mode: Literal["historical_archive", "max_age"] = "historical_archive"
    freshness_ok: bool | None = None


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    eventId: str = Field(..., min_length=1, max_length=128)
    question: str = Field(..., min_length=1, max_length=400)
    mode: Literal["local", "deepseek"] = "local"


class DiagnoseRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question: str = Field(..., min_length=1, max_length=500)
    mode: Literal["local", "deepseek"] = "local"
    sessionId: str = Field(default="default", max_length=64)


class AgentStreamRequest(BaseModel):
    question: str = Field(min_length=1, max_length=600, pattern=r"\S")
    mode: Literal["auto", "local", "deepseek"] = "auto"
    sessionId: str | None = Field(default=None, max_length=64)
    maxSteps: int = Field(default=8, ge=1, le=12)


AgentExecutionMode = Literal["auto", "local", "deepseek"]


class SubagentDebateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=600, pattern=r"\S")
    mode: AgentExecutionMode = "auto"


SkillIdentifier = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,47}$")]
SkillCabinet = Annotated[str, Field(pattern=r"^[0-9]{1,16}$")]


class SkillCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    skill_id: SkillIdentifier = Field(validation_alias=AliasChoices("skill_id", "id"))
    title: str = Field(min_length=1, max_length=120)
    body_markdown: str = Field(min_length=1, max_length=8000, validation_alias=AliasChoices("body_markdown", "markdown"))
    triggers: list[Annotated[str, Field(min_length=1, max_length=32)]] = Field(default_factory=list, max_length=12)
    default_cabinet: SkillCabinet = "1006"
    action_kind: Literal["switching_order", "defect_notice", "field_verification"] = "field_verification"


class SkillRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target_cabinet: SkillCabinet | None = None
    at_time: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:00(?::00)?$")

    @field_validator("at_time")
    @classmethod
    def normalize_skill_time(cls, value: str | None) -> str | None:
        return datetime.fromisoformat(value).isoformat(sep=" ") if value else None


class MCPClientRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80)
    transport: Literal["http", "stdio"] = "http"
    endpoint: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=240)
    read_only: Literal[True] = True

    @field_validator("endpoint")
    @classmethod
    def validate_mcp_endpoint(cls, value: str, info: ValidationInfo) -> str:
        if info.data.get("transport") == "http":
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError("MCP 地址须为不含账号、密码或片段的 HTTP(S) 地址。")
            if parsed.port == 0:
                raise ValueError("MCP 端口须为有效的服务端口。")
        return value


class ProtocolFrameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

    protocol: Literal["modbus_tcp", "opc_ua", "mqtt_scada", "mqtt"]
    payload: dict[str, Any] = Field(min_length=1, validation_alias=AliasChoices("payload", "frame"))
    voltage_limit_kv: float = Field(default=10.70, gt=0, le=60)

    @field_validator("payload")
    @classmethod
    def validate_frame(cls, value: dict[str, Any], info: ValidationInfo) -> dict[str, Any]:
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 32_768:
            raise ValueError("遥测帧最多 32 KiB。")
        protocol = info.data.get("protocol")
        if protocol == "modbus_tcp":
            function = value.get("function_code", 3)
            if type(function) is not int or function not in (3, 4):
                raise ValueError("Modbus 遥测仅接受只读功能码 FC03/FC04。")
            rows = value.get("registers")
        elif protocol == "opc_ua":
            if str(value.get("operation", "read")).lower() != "read":
                raise ValueError("OPC-UA 遥测仅接受只读操作。")
            rows = value.get("nodes")
        else:
            topic = value.get("topic", "mine/s35/telemetry/hourly")
            if (not isinstance(topic, str) or not (topic == "mine/s35/telemetry" or topic.startswith("mine/s35/telemetry/"))
                    or str(value.get("operation", "subscribe")).lower() not in {"subscribe", "read"}):
                raise ValueError("MQTT 遥测仅接受订阅得到的遥测主题数据。")
            rows = value.get("measurements")
        if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
            raise ValueError("遥测帧须包含至少一条记录。")
        for row in [value, *rows]:
            for key in ("timestamp", "cabinet", "metric", "node_id"):
                field = row.get(key)
                if field is not None and (not isinstance(field, str) or any(char in field for char in ",\r\n")):
                    raise ValueError(f"遥测字段 {key} 须为单行文本，且不得包含逗号。")
        return value


class SandboxRunRequest(BaseModel):
    code: str = Field(min_length=1, max_length=16_000, pattern=r"\S")
    timeout_s: float = Field(default=6.0, ge=0.5, le=15.0)


MemoryCategory = Literal["semantic", "episodic", "procedural"]
MemoryContent = Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]
MemoryTag = Annotated[str, Field(min_length=1, max_length=40, pattern=r"\S")]


class MemoryCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

    category: MemoryCategory
    title: str = Field(min_length=1, max_length=140, pattern=r"\S")
    content: MemoryContent
    scope_cabinet: str | None = Field(default=None, min_length=1, max_length=24)
    metric_threshold: dict[str, Any] | Annotated[str, Field(max_length=240)] | float | None = None
    tags: list[MemoryTag] = Field(default_factory=list, max_length=12)

    @field_validator("metric_threshold")
    @classmethod
    def validate_metric_threshold(cls, threshold):
        if isinstance(threshold, dict):
            if len(json.dumps(threshold, allow_nan=False, ensure_ascii=False)) > 2000:
                raise ValueError("阈值配置最多 2000 字符。")
            if str(threshold.get("metric", "")).strip().lower() in {"uab", "voltage", "电压"}:
                try:
                    value = float(threshold["value"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("电压上限需要有效的 kV 数值。") from exc
                if not math.isfinite(value) or value <= 0 or isinstance(threshold["value"], bool):
                    raise ValueError("电压上限需要大于零的 kV 数值。")
                if threshold.get("operator", ">") != ">" or threshold.get("unit", "kV") != "kV":
                    raise ValueError("当前专项排查仅支持以 kV 表示的严格电压上限。")
        return threshold


class MemoryPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool | None = None
    content: MemoryContent | None = None


class DeepResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    goal: str = Field(min_length=1, max_length=1200, pattern=r"\S")
    mode: Literal["auto", "local", "deepseek"] = "auto"


LIVE_CSV_MAX_BYTES = 2 * 1024 * 1024
ActionTicketKind = Literal["switching_order", "defect_notice", "field_verification"]


class LiveUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    csv_text: str = Field(min_length=1, max_length=LIVE_CSV_MAX_BYTES, pattern=r"\S")
    filename: str = Field(default="report.csv", min_length=1, max_length=160)


class LiveStreamRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: date
    speed_ms: int = Field(default=500, ge=0, le=1000)
    custom_threshold_kv: float | None = Field(default=None, gt=0, le=60)


class ActionDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: ActionTicketKind
    target_cabinet: str = Field(pattern=r"^[0-9]{1,16}$")
    at_time: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?$")
    rationale: str = Field(default="", max_length=1600)

    @field_validator("at_time")
    @classmethod
    def normalize_action_time(cls, value: str | None) -> str | None:
        return datetime.fromisoformat(value).isoformat(sep=" ") if value else None


class ActionChecklistRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    item_id: str = Field(min_length=1, max_length=64)
    checked: bool = Field(strict=True)


class ActionApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    note: str = Field(default="", max_length=240, validation_alias=AliasChoices("note", "comment"))


class ActionRejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=240, pattern=r"\S")


class ActionDispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool = Field(default=True, strict=True, validation_alias=AliasChoices("dry_run", "simulate"))


class SessionIdRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sessionId: str = Field(default="default", max_length=64)


class ReviewCreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    finding_id: str = Field(..., min_length=1, max_length=64)
    disposition: str = Field(default="pending", max_length=32)
    switch_state: str = Field(default="", max_length=200)
    operator_note: str = Field(default="", max_length=1000)
    operator_name: str = Field(default="值班员", max_length=64)
    scope_day: str = Field(default="ALL", max_length=32)
    mine_id: str = Field(default="", max_length=64)
    station: str = Field(default="", max_length=32)
    archive_version: str = Field(default="", max_length=64)
    window_label: str = Field(default="", max_length=80)


class ReviewDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    finding_id: str = Field(..., min_length=1, max_length=80)
    scope_day: str | None = Field(default=None, max_length=32)


class SupplementRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    station: str = Field(default="S35")
    source_doc: str = Field(default="工作台交互式补证单")
    parameters: dict[str, Any] = Field(default_factory=dict)


class GraphDemoRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["suspend", "resume", "tamper", "restrict"] = "suspend"
    threadId: str = Field(default="UI-DEMO-303-1113", max_length=64)
    taskType: Literal["single_incomer_overload_audit", "underground_n1_capacity_audit"] = "single_incomer_overload_audit"
    sessionId: str = Field(default="", max_length=64)
    source_doc: str = Field(default="工作台交互式补证核实单", max_length=128)
    parameters: dict[str, Any] | None = None


class PowerFlowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: Literal["base_case", "tap_sensitivity", "n1_contingency", "reactive_opt"] = "base_case"
    at_time: str | None = Field(default=None, description="Archived hourly timestamp YYYY-MM-DD HH:00:00")
    transformer_sn_mva: float | None = Field(default=None, gt=0.5, le=100.0)
    transformer_vk_percent: float | None = Field(default=None, gt=1.0, le=20.0)
    tr1_tap_pos: int | None = Field(default=None, ge=-2, le=2)
    tr2_tap_pos: int | None = Field(default=None, ge=-2, le=2)
    cable_length_km: float | None = Field(default=None, gt=0.1, le=20.0)
    outage_mode: Literal["feeder_1009", "incomer_306", "transformer_t2"] = "feeder_1009"
    target_cabinet: str = Field(default="1010", max_length=16)
    qc_kvar: float | None = Field(default=None, ge=0.0, le=5000.0)


class WeeklyScheduleQuery(BaseModel):
    target_cabinet: str = Field(default="1010", min_length=1, max_length=16)
    pareto_day: date = date(2026, 11, 13)


class SensitivityVoiQuery(BaseModel):
    at_time: str = Field(default="2026-11-13 17:00:00", pattern=r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:00:00$")
    target_cabinet: str = Field(default="1010", pattern=r"^[0-9]{1,16}$")

    @field_validator("at_time")
    @classmethod
    def validate_archived_hour(cls, value: str) -> str:
        return datetime.fromisoformat(value).isoformat(sep=" ")


class DiagnosticFact(BaseModel):
    statement: str
    evidence: list[str] = Field(default_factory=list)


class DiagnosticHypothesis(BaseModel):
    rank: int
    title: str
    confidence: str
    supporting_evidence: list[str] = Field(default_factory=list)
    caveats: str = ""


class DiagnosticReportModel(BaseModel):
    summary: str
    confirmed_facts: list[DiagnosticFact] = Field(default_factory=list)
    hypotheses: list[DiagnosticHypothesis] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    field_sop: list[str] = Field(default_factory=list)
    cited_standards: list[dict[str, str]] = Field(default_factory=list)


class TokenIssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(default="operator", min_length=1, max_length=64)
    role: Literal["viewer", "dispatcher", "engineer", "admin"] = "dispatcher"
    ttl_seconds: int = Field(default=28800, ge=60, le=604800)


class MultiAgentConsultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., min_length=1, max_length=600)
    mode: Literal["local", "deepseek"] = "local"


class SecurityPromptTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(..., min_length=1, max_length=1000)


class JobSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_type: Literal["diagnose", "multi_agent", "powerflow"] = "diagnose"
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=128)
    max_attempts: int = Field(default=3, ge=1, le=5)


class CircuitBreakerTripRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="llm:deepseek", min_length=1, max_length=64)
    reason: str = Field(default="管理员触发熔断演练", max_length=240)
    cooldown_seconds: float = Field(default=30.0, ge=1.0, le=600.0)


class CircuitBreakerResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="llm:deepseek", min_length=1, max_length=64)


class VectorSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=400)
    top_k: int = Field(default=5, ge=1, le=20)
    doc_kind: Literal["standard", "evidence_card"] | None = None


class StorageMigrateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_url: str | None = Field(default=None, max_length=512)


class DAGRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., min_length=1, max_length=600)
    mode: Literal["auto", "diagnose", "multi_agent", "audit_graph"] = "auto"
    thread_id: str | None = Field(default=None, max_length=64)
    pause_before_node: str | None = Field(default=None, max_length=64)
    pause_after_node: str | None = Field(default=None, max_length=64)
    human_input: dict[str, Any] | None = None


class DAGResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(..., min_length=1, max_length=64)
    human_input: dict[str, Any] | None = None
    resume_policy: Literal["strict", "pin_snapshot", "recompute_affected"] = "strict"


class TicketCreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_finding_id: str = Field(..., min_length=1, max_length=80)
    title: str = Field(..., min_length=1, max_length=200)
    summary: str = Field(default="", max_length=1200)
    cabinet: str = Field(default="", max_length=64)
    at_time: str = Field(default="", max_length=32)
    severity: Literal["high", "medium", "info"] = "medium"
    evidence_cells: list[str] = Field(default_factory=list)
    shift_name: str = Field(default="当前值班班次", max_length=64)
    channels: list[dict[str, str]] | None = None


class TicketTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["claim", "supplement", "dispute", "escalate", "handover", "close", "reopen", "supersede"]
    actor: str = Field(default="", max_length=64)
    shift_name: str = Field(default="", max_length=64)
    note: str = Field(default="", max_length=1000)
    assignee: str | None = Field(default=None, max_length=64)
    extra_payload: dict[str, Any] | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class ProactiveScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_new_tickets: int = Field(default=4, ge=1, le=20)
    channels: list[dict[str, str]] | None = None


class ProactiveReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cabinet: str = Field(..., min_length=1, max_length=32)
    at_time: str = Field(..., min_length=10, max_length=32)
    prev_p_kw: float
    curr_p_kw: float
    threshold_kw: float = Field(default=1000.0, gt=0.0)
    reason: str = Field(default="原始归档读数经更正后差值回落至门槛以下", max_length=300)


class OutboxDispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=50, ge=1, le=200)
    force_retry_now: bool = False
