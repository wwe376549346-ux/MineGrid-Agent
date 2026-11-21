"""Strong-typed Pydantic v2 API contracts for the Laowadi Enterprise FastAPI backend."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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
