"""Enterprise FastAPI + Uvicorn Multi-Worker ASGI Backend for the Laowadi Power Distribution Agent.

Enterprise Capabilities:
- Workstream 1: Metadata-driven MineProfile (`/api/mine-profile`), Pydantic v2 contracts, OpenAPI `/docs`, `/healthz`, `/readyz`.
- Workstream 2: Real `pandapower` Newton-Raphson AC Power-Flow (`POST /api/powerflow`).
- Workstream 3: 5-Role Parallel Multi-Agent Specialist Consultation (`POST /api/multi-agent/consult`, `GET /api/multi-agent/benchmark`).
- Workstream 4: RBAC + Signed HMAC-SHA256 Tokens, Shared Cross-Process Rate Limiter, Prompt-Injection Firewall,
                SHA-256 Hash-Chained Tamper-Evident Audit Ledger, Structured Tracing (`X-Trace-Id`, `X-Worker-Pid`) & Prometheus `/metrics`.
- Distributed Runtime Upgrade:
  - Decoupled External Durable Task Queue (`POST /api/jobs`, `GET /api/jobs`, `GET /api/jobs/{job_id}`).
  - Resumable SSE Streaming (`GET /api/jobs/{job_id}/stream` & `POST /api/diagnose/stream`) with `Last-Event-ID`
    & `?after_seq=N` replay so restarting the web server NEVER interrupts or drops ongoing SSE streams!
  - Cross-Process Shared Circuit Breaker (`GET /api/runtime/status`, `/api/runtime/circuit-breaker/trip|reset`).
  - Multi-Worker Uvicorn Horizontal Scaling (`laowadi.asgi:app`, `--workers N --external-workers M`).
"""

from __future__ import annotations

import asyncio
import base64
from collections import Counter
from contextlib import contextmanager, suppress
import csv
from email import policy
from email.parser import BytesParser
import fcntl
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from io import StringIO
import json
import logging
import math
from mimetypes import guess_type
import os
from pathlib import Path
import sqlite3
from threading import Lock
import time
from typing import Annotated, Any, Literal
from urllib.parse import quote, unquote, urlsplit
import uuid

import anyio
from fastapi import FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from psycopg import Error as PostgresError
from pydantic import ValidationError

from laowadi.context import DiagnosisSession
from laowadi.db.postgres import probe_runtime
from laowadi.source_files import document_manifest, verified_original
from laowadi.logging_config import bind_log_context, configure_logging, get_logger, log_event
from laowadi.llm import ChatClient, LLMError
from laowadi.mine_schema import MineProfile, load_mine_profile
from laowadi.schemas import (
    ActionApproveRequest,
    ActionChecklistRequest,
    ActionDispatchRequest,
    ActionDraftRequest,
    ActionRejectRequest,
    ActionTicketKind,
    AgentStreamRequest,
    AskRequest,
    CircuitBreakerResetRequest,
    CircuitBreakerTripRequest,
    DAGResumeRequest,
    DAGRunRequest,
    DeepResearchRequest,
    DiagnoseRequest,
    FeedbackCreateRequest,
    FeedbackExportRequest,
    GraphDemoRequest,
    HealthzResponse,
    JobSubmitRequest,
    LIVE_CSV_MAX_BYTES,
    LiveStreamRequest,
    LiveUploadRequest,
    MCPClientRegisterRequest,
    MemoryCategory,
    MemoryCreateRequest,
    MemoryPatchRequest,
    MemoryPruneRequest,
    MultiAgentConsultRequest,
    MultimodalIntakeJsonRequest,
    OutboxDispatchRequest,
    PowerFlowRequest,
    ProactiveReconcileRequest,
    ProactiveScanRequest,
    ProtocolFrameRequest,
    ReadyzResponse,
    ReviewCreateRequest,
    ReviewDeleteRequest,
    SandboxRunRequest,
    SecurityPromptTestRequest,
    SensitivityVoiQuery,
    SessionIdRequest,
    SkillCreateRequest,
    SkillIdentifier,
    SkillRunRequest,
    StorageMigrateRequest,
    SubagentDebateRequest,
    SupplementRequest,
    TicketCreateRequest,
    TicketTransitionRequest,
    TokenIssueRequest,
    VectorSearchRequest,
    WeeklyScheduleQuery,
)
from laowadi.security import (
    ROLE_DESCRIPTIONS,
    ROLE_LEVEL,
    inspect_prompt_security,
    issue_token,
    required_role_for,
    role_allows,
    verify_token,
)
from laowadi.sessions import load_session, save_chat_turn
from laowadi.storage import METRICS, REPORT_STATION, ROOT
from laowadi.task_queue import TERMINAL_STATUSES
from laowadi.workbench import BUILD, RequestError, Workbench


WEEKLY_SCHEDULE_PATH = "/api/powerflow/weekly-schedule"
SENSITIVITY_VOI_PATH = "/api/powerflow/sensitivity-voi"
READ_ONLY_COMPUTE_PATHS = {WEEKLY_SCHEDULE_PATH, SENSITIVITY_VOI_PATH}

COMPUTE_RATE_LIMITED_PATHS = {
    "/api/diagnose",
    "/api/diagnose/stream",
    "/api/agent/stream",
    "/api/subagents/debate/stream",
    "/api/protocols/ingest-frame",
    "/api/deep-research/stream",
    "/api/live/stream",
    "/api/live/upload-report",
    "/api/actions/tickets/draft",
    "/api/sandbox/run",
    "/api/powerflow",
    *READ_ONLY_COMPUTE_PATHS,
    "/api/multi-agent/consult",
    "/api/intake/image",
    "/api/jobs",
}

AUDITED_PATHS = {
    "/api/auth/token",
    "/api/jobs",
    "/api/ask",
    "/api/diagnose",
    "/api/diagnose/stream",
    "/api/agent/stream",
    "/api/subagents/debate/stream",
    "/api/protocols/ingest-frame",
    "/api/deep-research/stream",
    "/api/live/stream",
    "/api/live/upload-report",
    "/api/actions/tickets/draft",
    "/api/memory",
    "/api/memory/prune-expired",
    "/api/feedback",
    "/api/feedback/export-negative",
    "/api/intake/image",
    "/api/skills",
    "/api/mcp/clients",
    "/api/sandbox/run",
    "/api/diagnose/reset",
    "/api/powerflow",
    "/api/multi-agent/consult",
    "/api/reviews",
    "/api/reviews/delete",
    "/api/sessions/delete",
    "/api/supplement",
    "/api/graph/demo",
    "/api/audit/verify",
    "/api/security/inspect-prompt",
    "/api/runtime/circuit-breaker/trip",
    "/api/runtime/circuit-breaker/reset",
}

AUTONOMOUS_EVENT_TYPES = {
    "plan_update", "reasoning_delta", "tool_start", "tool_end", "code_run",
    "artifact", "content_delta", "verification_badge", "verification_repair",
    "clarification_request", "memory_suggestion", "done", "error",
}
DEBATE_EVENT_TYPES = {"subagent_start", "subagent_done", "debate_rebuttal", "arbitration_done", "done", "error"}
RESEARCH_EVENT_TYPES = {
    "plan_update", "stage_update", "finding", "reasoning_delta", "section_ready", "done", "error",
}
LIVE_EVENT_TYPES = {"telemetry_tick", "live_alert", "done", "error"}


async def _read_live_csv_upload(request: Request) -> LiveUploadRequest:
    content_type = request.headers.get("content-type", "")
    media_type = content_type.partition(";")[0].strip().lower()
    if media_type not in {"application/json", "text/csv", "text/plain", "multipart/form-data"}:
        raise RequestError(415, "请提交 CSV 文件或 CSV 文本。")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > LIVE_CSV_MAX_BYTES + 65536:
            raise RequestError(413, "CSV 文件最多 2 MiB。")
        raw.extend(chunk)

    try:
        if media_type == "application/json":
            upload = LiveUploadRequest.model_validate_json(raw)
        else:
            filename = "report.csv"
            csv_bytes = bytes(raw)
            if media_type == "multipart/form-data":
                header = ("Content-Type: " + content_type + "\r\n\r\n").encode("ascii")
                form = BytesParser(policy=policy.default).parsebytes(header + csv_bytes)
                parts = [part for part in form.iter_parts()
                         if part.get_content_disposition() == "form-data"
                         and part.get_param("name", header="content-disposition") == "file"]
                if form.defects or len(parts) != 1 or parts[0].is_multipart():
                    raise RequestError(400, "请在 file 字段上传一份 CSV 文件。")
                csv_bytes = parts[0].get_payload(decode=True) or b""
                filename = parts[0].get_filename() or filename
            if len(csv_bytes) > LIVE_CSV_MAX_BYTES:
                raise RequestError(413, "CSV 文件最多 2 MiB。")
            try:
                csv_text = csv_bytes.decode("utf-8-sig")
            except UnicodeDecodeError:
                csv_text = csv_bytes.decode("gb18030")
            upload = LiveUploadRequest(csv_text=csv_text, filename=filename)
    except (UnicodeError, ValidationError) as exc:
        raise RequestError(400, "CSV 文本为空、编码无效或文件元数据超限。") from exc
    if len(upload.csv_text.encode("utf-8")) > LIVE_CSV_MAX_BYTES:
        raise RequestError(413, "CSV 文本最多 2 MiB。")
    filename = upload.filename.replace("\\", "/").rsplit("/", 1)[-1] or "report.csv"
    csv_text = upload.csv_text.removeprefix("\ufeff")
    if not csv_text.strip():
        raise RequestError(400, "CSV 文本不能为空。")
    return upload.model_copy(update={"csv_text": csv_text, "filename": filename})


def _validate_csv_measurements(text: str):
    """Reject nonfinite readings and impossible timestamps before the sandbox insert."""
    reader = csv.DictReader(StringIO(text.strip()))
    measurement_fields = {metric.lower() for metric in METRICS} | {"value"}
    for line, row in enumerate(reader, start=2):
        for key, value in row.items():
            if not key or not value:
                continue
            field = key.strip().lower()
            if field in measurement_fields:
                try:
                    finite = math.isfinite(float(value))
                except ValueError:
                    continue  # The parser reports its field-specific validation error.
                if not finite:
                    raise RequestError(400, f"CSV 第 {line} 行的 {key} 必须是有限数值。")
            elif field in {"time", "timestamp", "时刻"}:
                try:
                    datetime.fromisoformat(value.strip().replace("/", "-"))
                except ValueError as exc:
                    raise RequestError(400, f"CSV 第 {line} 行的时间须为有效的完整日期和时刻。") from exc
            elif field == "station" and value.strip() not in {"", REPORT_STATION}:
                raise RequestError(400, f"CSV 第 {line} 行站点不属于当前 {REPORT_STATION} 回放范围。")


@contextmanager
def _runtime_write_lock(db_path: Path):
    # Serialize read-modify-write operations across API workers.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with db_path.with_suffix(db_path.suffix + ".lock").open("ab") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _load_optional_module(module: str, unavailable: str):
    try:
        return import_module(f"laowadi.{module}")
    except ImportError as exc:
        raise RequestError(503, unavailable) from exc


def _module_runner(module, name: str, unavailable: str):
    runner = getattr(module, name, None)
    if not callable(runner):
        raise RequestError(503, unavailable)
    return runner


def _load_optional_runner(module: str, name: str, unavailable: str):
    return _module_runner(_load_optional_module(module, unavailable), name, unavailable)


def _security_headers(headers: dict[str, str] | None = None) -> dict[str, str]:
    base = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if headers:
        base.update(headers)
    return base


def _stream_model_client(mode: str, configured_model: str | None):
    if mode == "local" or (mode == "auto" and configured_model is None):
        return None
    try:
        return ChatClient.from_env(stream=True)
    except LLMError:
        if mode == "deepseek":
            raise RequestError(409, "本次工作台尚未启用可用的 DeepSeek 配置。") from None
        return None


def _callback_sse_response(run, event_types: set[str], logger, failure_message: str):
    """Bridge synchronous runner callbacks to SSE with bounded backpressure."""
    async def events():
        sender, receiver = anyio.create_memory_object_stream[str](16)

        def encode(event):
            # The runner may reuse and mutate event objects after the callback returns.
            data = json.dumps(jsonable_encoder(event), ensure_ascii=False, allow_nan=False)
            return f"event: {event['type']}\ndata: {data}\n\n"

        def emit(event):
            if event["type"] in event_types:
                anyio.from_thread.run(sender.send, encode(event))

        async def produce():
            async with sender:
                try:
                    await anyio.to_thread.run_sync(run, emit, abandon_on_cancel=True)
                except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                    return
                except Exception:
                    logger.exception("%s", failure_message)
                    await sender.send(encode({"type": "error", "error": failure_message}))

        worker = asyncio.create_task(produce())
        try:
            async with receiver:
                while True:
                    with anyio.move_on_after(15) as idle:
                        try:
                            frame = await receiver.receive()
                        except anyio.EndOfStream:
                            return
                    yield ": heartbeat\n\n" if idle.cancelled_caught else frame
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers=_security_headers({"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"}),
    )


async def _tail_durable_job_sse(workbench: Workbench, job_id: str, *, after_seq: int = 0):
    """Async SSE generator reading from the durable job_event log with Last-Event-ID resume support."""
    cursor = max(0, int(after_seq))
    idle_ticks = 0
    while True:
        events = await asyncio.to_thread(workbench.task_broker.get_events_after, job_id, cursor, 100)
        if events:
            idle_ticks = 0
            for ev in events:
                cursor = ev["seq"]
                data_json = json.dumps(ev["data"], ensure_ascii=False, allow_nan=False)
                yield f"id: {cursor}\ndata: {data_json}\n\n".encode("utf-8")
                if ev["event_type"] in ("done", "error", "cancelled"):
                    return
        else:
            job = await asyncio.to_thread(workbench.task_broker.get_job, job_id)
            if job is None:
                err_json = json.dumps({"type": "error", "error": f"任务 {job_id} 不存在。"}, ensure_ascii=False)
                yield f"data: {err_json}\n\n".encode("utf-8")
                return
            if job["status"] in TERMINAL_STATUSES:
                # Drain any final event written between reads
                tail = await asyncio.to_thread(workbench.task_broker.get_events_after, job_id, cursor, 50)
                for ev in tail:
                    cursor = ev["seq"]
                    data_json = json.dumps(ev["data"], ensure_ascii=False, allow_nan=False)
                    yield f"id: {cursor}\ndata: {data_json}\n\n".encode("utf-8")
                return
            idle_ticks += 1
            if idle_ticks % 25 == 0:
                yield f": heartbeat job={job_id} seq={cursor} pid={os.getpid()}\n\n".encode("utf-8")
            await asyncio.sleep(0.12)


def create_app(
    workbench: Workbench,
    *,
    allowed_port: int | None = None,
    require_auth: bool | None = None,
    rate_limit_per_min: int = 120,
) -> FastAPI:
    """Create the Enterprise FastAPI ASGI application bound to a Workbench service instance."""
    configure_logging()
    api_logger = get_logger("api")
    started_at = time.monotonic()
    strict_auth = (
        bool(require_auth)
        if require_auth is not None
        else os.environ.get("LAOWADI_REQUIRE_AUTH", "0").strip() in ("1", "true", "TRUE", "yes")
    )
    max_rpm = max(1, int(rate_limit_per_min))
    allowed_hosts = {"127.0.0.1", "localhost", "testserver"}
    allowed_hosts.update(host.strip().lower() for host in os.environ.get("LAOWADI_ALLOWED_HOSTS", "").split(",") if host.strip())
    local_ports = {str(allowed_port)} if allowed_port else set()
    local_ports.update(port.strip() for port in os.environ.get("LAOWADI_ALLOWED_LOCAL_PORTS", "").split(",") if port.strip())
    max_age_hours = float(os.environ.get("LAOWADI_MAX_DATA_AGE_HOURS", "0"))
    runtime_dir = workbench.sessions_db.parent
    memory_db_path = runtime_dir / "laowadi_memory.sqlite3"
    feedback_db_path = runtime_dir / "laowadi_feedback.sqlite3"
    reports_dir = runtime_dir / "deep_reports"
    live_db_path = runtime_dir / "laowadi_live.sqlite3"
    actions_db_path = runtime_dir / "laowadi_actions.sqlite3"
    action_webhook_url = os.environ.get("LAOWADI_ACTION_WEBHOOK_URL", "").strip()
    builtin_skills_dir = ROOT / "skills"
    skills_dir = runtime_dir / "agent_skills"
    mcp_registry_path = runtime_dir / "laowadi_mcp_clients.json"
    advanced_counts = Counter(dict.fromkeys((
        "autonomous_turns", "memory_suggestions", "memory_writes", "debate_runs",
        "skill_installs", "skill_runs", "mcp_registrations", "protocol_frames",
        "sandbox_runs", "sandbox_successes", "verification_samples", "verified_turns",
    ), 0))
    advanced_lock = Lock()

    def record_advanced(**counts: int):
        with advanced_lock:
            advanced_counts.update(counts)

    app = FastAPI(
        title="老洼地煤矿配电网健康监测与诊断智能体企业级 API",
        version="2.2.0",
        description=(
            "集成元数据驱动多矿配置（MineProfile）、真实交流潮流求解器（pandapower Newton-Raphson）、"
            "五角色多智能体专家并行协同、外部持久化任务队列与断线断点续传 SSE（Last-Event-ID）、"
            "跨进程共享熔断器、多 Worker 水平扩展、RBAC/HMAC 鉴权与防篡改 SHA-256 审计链。"
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.workbench = workbench
    app.state.strict_auth = strict_auth
    app.state.rate_limit_per_min = max_rpm

    # Unified Enterprise Governance Middleware: Host/Origin CSRF + RBAC Auth + Shared Rate Limit + Trace + SHA-256 Audit
    @app.middleware("http")
    async def enterprise_governance_middleware(request: Request, call_next):
        t0 = time.monotonic()
        trace_id = request.headers.get("x-trace-id") or f"TRC-{uuid.uuid4().hex[:12].upper()}"
        method = request.method.upper()
        path = request.url.path
        client_host = request.client.host if request.client else "127.0.0.1"

        def _finalize(resp: Response, actor: str, role: str, *, action: str | None = None, summary: dict | None = None):
            elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)
            resp.headers["X-Trace-Id"] = trace_id
            resp.headers["X-Process-Time-Ms"] = str(elapsed_ms)
            resp.headers["X-Actor-Role"] = role
            resp.headers["X-Worker-Pid"] = str(os.getpid())
            if "x-content-type-options" not in resp.headers:
                resp.headers["X-Content-Type-Options"] = "nosniff"
            if "cache-control" not in resp.headers:
                resp.headers["Cache-Control"] = "no-store"
            if path.startswith("/api/") or path in ("/healthz", "/readyz", "/metrics"):
                workbench.observability.record_http(
                    trace_id=trace_id,
                    method=method,
                    path=path,
                    status_code=resp.status_code,
                    latency_ms=elapsed_ms,
                    role=role,
                )
                with bind_log_context(trace_id=trace_id, actor=actor, role=role):
                    lvl = logging.WARNING if resp.status_code >= 400 else logging.INFO
                    log_event(
                        api_logger,
                        lvl,
                        "http_request",
                        f"{method} {path} -> {resp.status_code} ({elapsed_ms}ms)",
                        method=method,
                        path=path,
                        status_code=resp.status_code,
                        latency_ms=elapsed_ms,
                        client=client_host,
                        action=action or "",
                    )
            if (action or path in AUDITED_PATHS or path.startswith(("/api/memory/", "/api/actions/tickets/", "/api/skills/"))
                    or path.startswith("/api/deep-research/reports/") or resp.status_code in (401, 403, 429)):
                workbench.audit_ledger.record(
                    trace_id=trace_id,
                    actor=actor,
                    role=role,
                    method=method,
                    path=path,
                    action=action or f"{method}:{path}",
                    status_code=resp.status_code,
                    latency_ms=elapsed_ms,
                    request_payload={"query": str(request.url.query), "client": client_host},
                    summary=summary or {"status_code": resp.status_code, "pid": os.getpid()},
                )
            return resp

        # 1. Localhost Host & Origin CSRF Guard
        host = request.headers.get("host", "")
        host_base = host.split(":", 1)[0].lower()
        if host_base not in allowed_hosts:
            workbench.observability.record_security_block("host_guard")
            return _finalize(
                JSONResponse(status_code=403, content={"error": "请从工作台的本机地址访问。"}, headers=_security_headers()),
                "anonymous",
                "none",
                action="SECURITY_HOST_DENIED",
                summary={"host": host},
            )
        if local_ports and host_base in {"127.0.0.1", "localhost"}:
            if host.rpartition(":")[2] not in local_ports:
                workbench.observability.record_security_block("host_guard")
                return _finalize(
                    JSONResponse(status_code=403, content={"error": "请从工作台的本机地址访问。"}, headers=_security_headers()),
                    "anonymous",
                    "none",
                    action="SECURITY_HOST_DENIED",
                    summary={"host": host},
                )
        origin = request.headers.get("origin")
        if origin and host and origin not in {f"http://{host}", f"https://{host}"}:
            workbench.observability.record_security_block("csrf_origin")
            return _finalize(
                JSONResponse(status_code=403, content={"error": "请从工作台页面发起请求。"}, headers=_security_headers()),
                "anonymous",
                "none",
                action="SECURITY_CSRF_DENIED",
                summary={"origin": origin},
            )

        # 2. RBAC & HMAC-SHA256 Bearer Authentication
        req_role = required_role_for(method, path)
        is_skill_run = method == "POST" and path.startswith("/api/skills/") and path.endswith("/run")
        if method == "POST":
            req_role = {
                "/api/agent/stream": "dispatcher",
                "/api/subagents/debate/stream": "dispatcher",
                "/api/skills": "engineer",
                "/api/mcp/clients": "engineer",
                "/api/protocols/ingest-frame": "dispatcher",
                "/api/deep-research/stream": "dispatcher",
                "/api/live/stream": "dispatcher",
                "/api/live/upload-report": "dispatcher",
                "/api/memory": "dispatcher",
                "/api/sandbox/run": "engineer",
            }.get(path, req_role)
            if is_skill_run or path.startswith("/api/actions/tickets/"):
                req_role = "dispatcher"
        elif method in {"PATCH", "DELETE"} and path.startswith("/api/memory/"):
            req_role = "dispatcher"
        actor = "local-workbench"
        role = "engineer"
        auth_header = request.headers.get("authorization", "").strip()

        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
            try:
                claims = verify_token(token)
                actor = str(claims.get("sub") or "bearer-user")
                role = str(claims.get("role"))
            except ValueError as exc:
                workbench.observability.record_security_block("invalid_token")
                return _finalize(
                    JSONResponse(status_code=401, content={"error": f"身份认证失败：{exc}"}, headers=_security_headers()),
                    "unauthenticated",
                    "none",
                    action="AUTH_INVALID_TOKEN",
                    summary={"reason": str(exc)},
                )
        elif req_role is not None:
            if strict_auth:
                workbench.observability.record_security_block("missing_auth")
                return _finalize(
                    JSONResponse(
                        status_code=401,
                        content={"error": "当前服务已启用强制鉴权（LAOWADI_REQUIRE_AUTH=1），请先通过 POST /api/auth/token 获取 Bearer 令牌。"},
                        headers=_security_headers({"WWW-Authenticate": "Bearer"}),
                    ),
                    "unauthenticated",
                    "none",
                    action="AUTH_REQUIRED",
                    summary={"required_role": req_role},
                )
            hdr_role = request.headers.get("x-operator-role", "").strip().lower()
            hdr_actor = request.headers.get("x-operator-name", "").strip()
            if hdr_role:
                if hdr_role not in ROLE_LEVEL:
                    return _finalize(
                        JSONResponse(status_code=400, content={"error": f"无效的 X-Operator-Role：{hdr_role}"}, headers=_security_headers()),
                        hdr_actor or "local-workbench",
                        "invalid",
                        action="RBAC_INVALID_ROLE",
                    )
                role = hdr_role
            if hdr_actor:
                actor = hdr_actor[:64]

        if req_role is not None and not role_allows(role, req_role):
            workbench.observability.record_security_block("rbac_denied")
            return _finalize(
                JSONResponse(
                    status_code=403,
                    content={
                        "error": f"RBAC 权限不足：当前角色 {role}（{ROLE_DESCRIPTIONS.get(role, {}).get('label', role)}）"
                        f"无法访问 {method} {path}，至少需要角色 {req_role}（{ROLE_DESCRIPTIONS[req_role]['label']}）。",
                        "required_role": req_role,
                        "actual_role": role,
                    },
                    headers=_security_headers(),
                ),
                actor,
                role,
                action="RBAC_DENIED",
                summary={"required_role": req_role, "actual_role": role},
            )

        # 3. Cross-Process Shared Sliding-Window Rate Limiter (consistent across all Uvicorn worker PIDs!)
        if is_skill_run or (path in COMPUTE_RATE_LIMITED_PATHS and (
            method == "POST" or (method == "GET" and path in READ_ONLY_COMPUTE_PATHS)
        )):
            allowed, _remaining, retry_after = workbench.task_broker.check_shared_rate_limit(
                f"{client_host}:{actor}",
                max_requests=max_rpm,
                window_seconds=60.0,
            )
            if not allowed:
                workbench.observability.record_security_block("rate_limited")
                return _finalize(
                    JSONResponse(
                        status_code=429,
                        content={"error": f"跨进程请求频率超限（全局滑动窗口 {max_rpm} 次/分钟），请 {retry_after} 秒后重试。"},
                        headers=_security_headers({"Retry-After": str(retry_after)}),
                    ),
                    actor,
                    role,
                    action="RATE_LIMITED",
                    summary={"retry_after": retry_after},
                )

        request.state.trace_id = trace_id
        request.state.actor = actor
        request.state.role = role
        response = await call_next(request)
        return _finalize(response, actor, role)

    # Unified Exception Handlers
    @app.exception_handler(RequestError)
    async def handle_request_error(_request: Request, exc: RequestError):
        return JSONResponse(status_code=exc.status, content={"error": str(exc)}, headers=_security_headers())

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request, exc: RequestValidationError):
        errors = jsonable_encoder(exc.errors(), custom_encoder={Exception: str})
        first = errors[0] if errors else {"msg": "参数校验失败"}
        loc = ".".join(str(x) for x in first.get("loc", []))
        msg = f"请求参数校验失败（{loc}）：{first.get('msg', '')}" if loc else str(first.get("msg", "参数校验失败"))
        return JSONResponse(status_code=400, content={"error": msg, "detail": errors}, headers=_security_headers())

    @app.exception_handler(ValueError)
    async def handle_value_error(_request: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"error": str(exc)}, headers=_security_headers())

    @app.exception_handler(PostgresError)
    @app.exception_handler(sqlite3.Error)
    async def handle_sqlite_error(_request: Request, _exc: sqlite3.Error):
        return JSONResponse(
            status_code=503,
            content={"error": "本地资料暂不可用，请核对日报库和历史回放档案。"},
            headers=_security_headers(),
        )

    @app.exception_handler(OSError)
    async def handle_os_error(_request: Request, _exc: OSError):
        return JSONResponse(
            status_code=503,
            content={"error": "本地资料暂不可用，请核对日报库和历史回放档案。"},
            headers=_security_headers(),
        )

    # ── Operational Health & Readiness Probes ──────────────────────────────────
    @app.get("/healthz", response_model=HealthzResponse, tags=["Health"])
    def healthz():
        try:
            pp_ver = version("pandapower")
        except PackageNotFoundError:
            pp_ver = None
        prof = load_mine_profile()
        return HealthzResponse(
            version="2.2.0",
            mine_id=prof.mine_id,
            model=workbench.model,
            pandapower_version=pp_ver,
            uptime_seconds=round(time.monotonic() - started_at, 2),
        )

    @app.get("/readyz", response_model=ReadyzResponse, tags=["Health"])
    def readyz():
        pg = workbench.postgres
        payload = ReadyzResponse(
            status="degraded", archive_ok=False, events_db_ok=False, profile_consistent=False,
            frontend_built=(BUILD / "index.html").is_file(), days_count=0, cabinet_count=0,
            backend="postgresql" if pg else "sqlite",
            source_originals_ok=False if pg else None,
            runtime_ok=False if pg else None, external_worker_ok=False if pg else None,
            freshness_mode="max_age" if max_age_hours > 0 else "historical_archive",
            freshness_ok=False if max_age_hours > 0 else None,
        )
        probe_errors = (ValueError, OSError, sqlite3.Error, PostgresError)
        try:
            prof = load_mine_profile()
            with workbench.archive() as archive:
                payload.days_count = len(archive.days())
                payload.latest_reading_at = archive.db.execute("SELECT max(time) FROM reading").fetchone()[0]
                payload.archive_ok = bool(payload.days_count and payload.latest_reading_at)
                check = prof.validate_against_sqlite(archive)
                payload.profile_consistent = bool(check["consistent"])
                payload.cabinet_count = check["db_cabinet_count"]
                if max_age_hours > 0 and payload.latest_reading_at:
                    local = datetime.fromisoformat(payload.latest_reading_at).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                    age = (datetime.now(timezone.utc) - local).total_seconds() / 3600
                    payload.freshness_ok = -1.0 <= age <= max_age_hours

                if pg:
                    row = archive.db.execute("SELECT version FROM publication WHERE singleton=1").fetchone()
                    payload.archive_version = row[0] if row else None
                    payload.archive_ok = payload.archive_ok and bool(row)
                    try:
                        documents = document_manifest(archive.db)
                        for document in documents:
                            verified_original(workbench.source, document["filename"], document["sha256"])
                        payload.source_originals_ok = bool(documents)
                    except (ValueError, OSError):
                        pass

                if archive.has_events():
                    try:
                        with archive.event_connection() as db:
                            integrity_ok = pg or db.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
                            historical = db.execute(
                                "SELECT frames_seen, description FROM monitor_run WHERE complete=1 AND mode='historical' ORDER BY rowid DESC LIMIT 1"
                            ).fetchone()
                            db.execute("SELECT seq, run_id, at, evidence FROM monitor_event LIMIT 1").fetchone()
                            if integrity_ok and historical and historical[0] > 0:
                                replay = json.loads(historical[1]).get("source_reports", [])
                                reports = archive.db.execute("SELECT day, filename, sha256 FROM report")
                                payload.events_db_ok = {
                                    (r["day"], r["filename"], r["sha256"]) for r in replay
                                } == {tuple(r) for r in reports}
                    except (*probe_errors, KeyError, TypeError, AttributeError):
                        pass
        except probe_errors:
            pass
        if pg:
            try:
                probe_runtime()
                payload.runtime_ok = True
                payload.external_worker_ok = workbench.has_alive_external_worker()
            except probe_errors:
                pass
        checks = (
            payload.archive_ok, payload.events_db_ok, payload.profile_consistent,
            payload.frontend_built, payload.runtime_ok, payload.external_worker_ok,
            payload.source_originals_ok, payload.freshness_ok,
        )
        if any(check is False for check in checks):
            return JSONResponse(status_code=503, content=payload.model_dump())
        payload.status = "ready"
        return payload

    # ── Distributed Task Queue, Resumable SSE & Cross-Process Circuit Breaker ──
    @app.post("/api/jobs", status_code=202, tags=["External Task Queue & Resumable SSE"])
    def api_submit_job(req: JobSubmitRequest, request: Request):
        actor = getattr(request.state, "actor", "local-workbench")
        role = getattr(request.state, "role", "engineer")
        return workbench.submit_job(
            req.job_type,
            req.payload,
            actor=actor,
            role=role,
            idempotency_key=req.idempotency_key,
            max_attempts=req.max_attempts,
        )

    @app.get("/api/jobs", tags=["External Task Queue & Resumable SSE"])
    def api_list_jobs(limit: int = Query(default=30, ge=1, le=100), status: str | None = Query(default=None)):
        return workbench.list_jobs(limit=limit, status=status)

    @app.get("/api/jobs/{job_id}", tags=["External Task Queue & Resumable SSE"])
    def api_get_job(job_id: str):
        return workbench.get_job(job_id)

    @app.post("/api/jobs/{job_id}/cancel", tags=["External Task Queue & Resumable SSE"])
    def api_cancel_job(job_id: str):
        return workbench.cancel_job(job_id)

    @app.get("/api/jobs/{job_id}/stream", tags=["External Task Queue & Resumable SSE"])
    async def api_stream_job(
        job_id: str,
        request: Request,
        after_seq: int = Query(default=0, ge=0),
    ):
        # Verify job exists before opening SSE stream
        workbench.get_job(job_id)
        hdr_last = request.headers.get("last-event-id", "").strip()
        cursor = after_seq
        if hdr_last.isdigit():
            cursor = max(cursor, int(hdr_last))
        return StreamingResponse(
            _tail_durable_job_sse(workbench, job_id, after_seq=cursor),
            media_type="text/event-stream; charset=utf-8",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Accel-Buffering": "no",
                "X-Job-Id": job_id,
            },
        )

    @app.get("/api/runtime/status", tags=["Distributed Runtime & Circuit Breaker"])
    def api_runtime_status():
        return workbench.runtime_status()

    @app.post("/api/runtime/circuit-breaker/trip", tags=["Distributed Runtime & Circuit Breaker"])
    def api_trip_cb(req: CircuitBreakerTripRequest):
        return workbench.trip_circuit_breaker(req.name, req.reason, req.cooldown_seconds)

    @app.post("/api/runtime/circuit-breaker/reset", tags=["Distributed Runtime & Circuit Breaker"])
    def api_reset_cb(req: CircuitBreakerResetRequest):
        return workbench.reset_circuit_breaker(req.name)

    # ── Enterprise Storage: SQLAlchemy 2.0 Pool, Alembic, TimescaleDB & pgvector ──
    @app.get("/api/storage/status", tags=["Enterprise Storage (PG/TimescaleDB/pgvector)"])
    def api_storage_status():
        return workbench.storage_status()

    @app.post("/api/storage/migrate", tags=["Enterprise Storage (PG/TimescaleDB/pgvector)"])
    def api_storage_migrate(req: StorageMigrateRequest | None = None):
        return workbench.storage_migrate(database_url=req.database_url if req else None)

    @app.post("/api/storage/vector-search", tags=["Enterprise Storage (PG/TimescaleDB/pgvector)"])
    def api_storage_vector_search(req: VectorSearchRequest):
        return workbench.storage_vector_search(req.query, top_k=req.top_k, doc_kind=req.doc_kind)

    @app.get("/api/storage/postgres-ddl", tags=["Enterprise Storage (PG/TimescaleDB/pgvector)"])
    def api_storage_postgres_ddl():
        return workbench.storage_postgres_ddl()

    # ── Enterprise Ops: Structured JSON Logs, Hot Backups & DR Drills ──────────
    @app.get("/api/ops/backups", tags=["Enterprise Ops (Backup/DR/Logs)"])
    def api_ops_backups():
        return workbench.ops_list_backups()

    @app.post("/api/ops/backup", tags=["Enterprise Ops (Backup/DR/Logs)"])
    def api_ops_create_backup():
        return workbench.ops_create_backup()

    @app.post("/api/ops/dr-drill", tags=["Enterprise Ops (Backup/DR/Logs)"])
    def api_ops_dr_drill():
        return workbench.ops_run_dr_drill()

    @app.get("/api/ops/logs", tags=["Enterprise Ops (Backup/DR/Logs)"])
    def api_ops_logs(limit: int = Query(default=40, ge=1, le=200), level: str | None = Query(default=None)):
        return workbench.ops_logs(limit=limit, level=level)

    # ── Unified Dynamic DAG Execution & Arbitrary-Node Suspend/Resume ──────────
    @app.post("/api/dag/run", tags=["Unified Dynamic DAG Engine"])
    def api_dag_run(req: DAGRunRequest):
        return workbench.dag_run(req.model_dump())

    @app.post("/api/dag/resume", tags=["Unified Dynamic DAG Engine"])
    def api_dag_resume(req: DAGResumeRequest):
        return workbench.dag_resume(req.model_dump())

    @app.get("/api/dag/threads", tags=["Unified Dynamic DAG Engine"])
    def api_dag_threads(limit: int = Query(default=30, ge=1, le=100)):
        return workbench.dag_list_threads(limit=limit)

    @app.get("/api/dag/threads/{thread_id}", tags=["Unified Dynamic DAG Engine"])
    def api_dag_thread_detail(thread_id: str):
        return workbench.dag_get_thread(unquote(thread_id))

    # ── P1 Proactive Watcher, Outbox Delivery, Incident Tickets & LLMOps ───────
    @app.get("/api/tickets", tags=["Incident Tickets & Proactive Watcher"])
    def api_list_tickets(
        status: str | None = Query(default=None),
        severity: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        return workbench.list_tickets(status=status, severity=severity, limit=limit)

    @app.post("/api/tickets", tags=["Incident Tickets & Proactive Watcher"])
    def api_create_ticket(req: TicketCreateRequest, request: Request):
        actor = getattr(request.state, "actor", "dispatcher")
        return workbench.create_ticket(req.model_dump(), actor=actor)

    @app.get("/api/tickets/groups", tags=["Incident Tickets & Proactive Watcher"])
    def api_list_incident_groups(status: str | None = Query(default="active")):
        return workbench.list_incident_groups(status=status)

    @app.get("/api/tickets/{ticket_id}", tags=["Incident Tickets & Proactive Watcher"])
    def api_get_ticket(ticket_id: str):
        return workbench.get_ticket(unquote(ticket_id))

    @app.post("/api/tickets/{ticket_id}/pre-diagnosis", tags=["Incident Tickets & Proactive Watcher"])
    def api_refresh_pre_diagnosis(ticket_id: str, request: Request, expected_revision: int = Query(ge=1)):
        from laowadi.proactive import TicketStateConflict

        ticket = workbench.get_ticket(unquote(ticket_id))
        actor = getattr(request.state, "actor", "dispatcher")
        try:
            return workbench.proactive_watcher.refresh_pre_diagnosis(
                ticket, expected_revision=expected_revision, actor=actor,
            )
        except TicketStateConflict as exc:
            raise RequestError(409, str(exc)) from None

    @app.post("/api/tickets/{ticket_id}/transition", tags=["Incident Tickets & Proactive Watcher"])
    def api_transition_ticket(ticket_id: str, req: TicketTransitionRequest, request: Request):
        actor = getattr(request.state, "actor", "值班员")
        return workbench.transition_ticket(unquote(ticket_id), req.model_dump(), default_actor=actor)

    @app.post("/api/tickets/{ticket_id}/distill-memory", tags=["Incident Tickets & Proactive Watcher"])
    def api_distill_ticket_memory(ticket_id: str):
        detail = workbench.get_ticket(unquote(ticket_id))
        distill = memory_runner("distill_ticket_memory_candidate")
        existing = memory_runner("list_memories")(include_expired=True, db_path=memory_db_path)
        cand = distill(detail, existing_memories=existing)
        return {"ticket_id": detail["ticket_id"], "candidate": cand, "requires_human_confirmation": True}

    @app.get("/api/proactive/status", tags=["Incident Tickets & Proactive Watcher"])
    def api_proactive_status():
        return workbench.proactive_status()

    @app.post("/api/proactive/scan", tags=["Incident Tickets & Proactive Watcher"])
    def api_proactive_scan(req: ProactiveScanRequest | None = None):
        return workbench.proactive_scan(req.model_dump() if req else None)

    @app.post("/api/proactive/dispatch", tags=["Incident Tickets & Proactive Watcher"])
    def api_proactive_dispatch(req: OutboxDispatchRequest | None = None):
        return workbench.proactive_dispatch(req.model_dump() if req else None)

    @app.post("/api/proactive/reconcile", tags=["Incident Tickets & Proactive Watcher"])
    def api_proactive_reconcile(req: ProactiveReconcileRequest, request: Request):
        actor = getattr(request.state, "actor", "engineer")
        return workbench.proactive_reconcile(req.model_dump(), actor=actor)

    @app.get("/api/llmops/metrics", tags=["Observability"])
    def api_llmops_metrics():
        return workbench.llmops_metrics()

    # ── Security, RBAC Auth, Audit Ledger & Observability ──────────────────────
    @app.post("/api/auth/token", tags=["Security & Governance"])
    def api_issue_token(req: TokenIssueRequest):
        return issue_token(req.subject, req.role, ttl_seconds=req.ttl_seconds)

    @app.get("/api/security/status", tags=["Security & Governance"])
    def api_security_status(request: Request):
        return {
            "strict_auth_required": strict_auth,
            "current_actor": getattr(request.state, "actor", "local-workbench"),
            "current_role": getattr(request.state, "role", "engineer"),
            "rate_limit_per_min": max_rpm,
            "rate_limited_paths": sorted(COMPUTE_RATE_LIMITED_PATHS),
            "roles": ROLE_DESCRIPTIONS,
            "firewall_categories": [
                "prohibited_control_command (遥控分合闸 / 保护定值写入 / 写库)",
                "prompt_injection_jailbreak (提示词覆盖 / 系统提示套取 / 越狱绕过)",
                "sql_injection_attempt (SQL 破坏性写入 / UNION 注入探针)",
            ],
        }

    @app.post("/api/security/inspect-prompt", tags=["Security & Governance"])
    def api_inspect_prompt(req: SecurityPromptTestRequest):
        res = inspect_prompt_security(req.prompt)
        if not res["allowed"]:
            workbench.observability.record_security_block(res["threat_category"])
        return res

    @app.get("/api/audit/logs", tags=["Security & Governance"])
    def api_audit_logs(limit: int = Query(default=40, ge=1, le=200), action: str | None = Query(default=None)):
        return workbench.audit_logs(limit=limit, action=action)

    @app.post("/api/audit/verify", tags=["Security & Governance"])
    def api_audit_verify():
        return workbench.verify_audit_chain()

    @app.get("/api/observability/summary", tags=["Observability"])
    def api_observability_summary():
        return workbench.observability_summary()

    @app.get("/metrics", tags=["Observability"])
    def prometheus_metrics():
        return PlainTextResponse(
            content=workbench.prometheus_metrics(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    # ── Multi-Agent Specialist Consultation ────────────────────────────────────
    @app.post("/api/multi-agent/consult", tags=["Multi-Agent Collaboration"])
    def api_multi_agent_consult(req: MultiAgentConsultRequest):
        return workbench.multi_agent_consult(req.model_dump())

    @app.get("/api/multi-agent/benchmark", tags=["Multi-Agent Collaboration"])
    def api_multi_agent_benchmark():
        return workbench.multi_agent_benchmark()

    # ── Mine Profile & Real AC Power-Flow ──────────────────────────────────────
    @app.get("/api/mine-profile", tags=["Mine Profile"])
    def api_mine_profile():
        return workbench.mine_profile()

    @app.get("/api/profiles", tags=["Mine Profile"])
    def api_profiles():
        default_profile = load_mine_profile()
        profiles = {default_profile.mine_id: default_profile}
        for path in sorted((ROOT / "profiles").glob("*.json")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                profile = MineProfile.model_validate_json(path.read_bytes())
            except (OSError, ValidationError):
                log_event(api_logger, logging.WARNING, "profile_catalog_skipped", "矿井图模未通过目录校验。",
                          filename=path.name)
                continue
            if profile.mine_id in profiles:
                log_event(api_logger, logging.WARNING, "profile_catalog_duplicate", "矿井图模编号重复。",
                          filename=path.name, mine_id=profile.mine_id)
                continue
            profiles[profile.mine_id] = profile
        return {"profiles": [{
            "mine_id": profile.mine_id,
            "mine_name": profile.mine_name,
            "stations_count": len(profile.stations),
            "buses_count": len(profile.buses),
            "transformers_count": len(profile.transformers),
            "feeders_count": len(profile.feeders),
        } for profile in profiles.values()]}

    @app.post("/api/powerflow", tags=["AC Power Flow"])
    def api_powerflow(req: PowerFlowRequest):
        return workbench.powerflow(req.model_dump())

    @app.get(WEEKLY_SCHEDULE_PATH, tags=["AC Power Flow"])
    def api_weekly_schedule(query: Annotated[WeeklyScheduleQuery, Query()]):
        from laowadi import powerflow

        analyze = getattr(powerflow, "analyze_weekly_schedule_pareto", None)
        if analyze is None:
            raise RequestError(503, "全周分时优化计算暂未就绪。")
        try:
            with workbench.archive() as archive:
                return analyze(archive, target_cabinet=query.target_cabinet, pareto_day=query.pareto_day.isoformat())
        except ValueError as exc:
            raise RequestError(400, str(exc)) from None

    @app.get(SENSITIVITY_VOI_PATH, tags=["AC Power Flow"])
    def api_sensitivity_voi(query: Annotated[SensitivityVoiQuery, Query()]):
        try:
            from laowadi import powerflow
        except ImportError as exc:
            raise RequestError(503, "参数灵敏度计算所需的潮流模块暂不可用。") from exc

        analyze = getattr(powerflow, "analyze_parameter_sensitivity_voi", None)
        if analyze is None:
            raise RequestError(503, "参数灵敏度与信息价值计算暂未就绪。")
        profile = load_mine_profile()
        station = profile.primary_station
        with workbench.archive() as archive:
            if (station, query.target_cabinet) not in archive.devices:
                raise RequestError(404, f"归档中未找到 {query.target_cabinet} 柜。")
            feeder = profile.feeder_map().get(query.target_cabinet)
            if not feeder or not feeder.downstream_bus_id:
                raise RequestError(400, f"{query.target_cabinet} 柜缺少可用于灵敏度分析的下游馈线模型。")
            if query.target_cabinet != "1010":
                raise RequestError(422, "当前 N-1 灵敏度以 1009 退运、1010 受电为固定工况，目标柜须为 1010。")
            reading = archive.db.execute(
                "SELECT 1 FROM reading WHERE station = ? AND time = ? LIMIT 1",
                (station, query.at_time),
            ).fetchone()
            if reading is None:
                raise RequestError(404, f"归档中未找到 {query.at_time} 的整点断面。")
            return analyze(archive, at_time=query.at_time, target_cabinet=query.target_cabinet)

    # ── Monitoring, Health, Topology & Cabinet Endpoints ───────────────────────
    @app.get("/api/dates", tags=["Monitoring & Topology"])
    def api_dates():
        return workbench.dates()

    @app.get("/api/day", tags=["Monitoring & Topology"])
    def api_day(date: str = Query(default="")):
        return workbench.day(date)

    @app.get("/api/health", tags=["Monitoring & Topology"])
    def api_health(day: str | None = Query(default=None)):
        return workbench.health(day or None)

    @app.get("/api/cabinet", tags=["Monitoring & Topology"])
    def api_cabinet(cabinet: str = Query(default="")):
        return workbench.cabinet_detail(cabinet)

    @app.get("/api/diagrams", tags=["Monitoring & Topology"])
    def api_diagrams():
        return workbench.diagrams()

    @app.get("/api/timeline", tags=["Monitoring & Topology"])
    def api_timeline():
        return workbench.get_timeline()

    # ── Reviews & Persistent Diagnostic Sessions ───────────────────────────────
    @app.get("/api/reviews", tags=["Reviews & Sessions"])
    def api_get_reviews():
        return workbench.get_reviews()

    @app.post("/api/reviews", tags=["Reviews & Sessions"])
    def api_post_review(req: ReviewCreateRequest):
        return workbench.post_review(req.model_dump())

    @app.post("/api/reviews/delete", tags=["Reviews & Sessions"])
    def api_delete_review(req: ReviewDeleteRequest):
        return workbench.delete_review_item(req.model_dump())

    @app.get("/api/sessions", tags=["Reviews & Sessions"])
    def api_get_sessions():
        return workbench.get_sessions()

    @app.get("/api/sessions/{session_id:path}", tags=["Reviews & Sessions"])
    def api_get_session_detail(session_id: str):
        return workbench.get_session_detail(unquote(session_id))

    @app.post("/api/sessions/delete", tags=["Reviews & Sessions"])
    def api_delete_session(req: SessionIdRequest):
        return workbench.delete_chat_session(req.model_dump())

    # ── Diagnostic Agent & Durable Resumable SSE Stream ────────────────────────
    @app.post("/api/ask", tags=["Diagnostic Agent"])
    def api_ask(req: AskRequest):
        return workbench.ask(req.model_dump())

    @app.post("/api/diagnose", tags=["Diagnostic Agent"])
    def api_diagnose(req: DiagnoseRequest):
        return workbench.diagnose(req.model_dump())

    @app.post("/api/diagnose/reset", tags=["Diagnostic Agent"])
    def api_diagnose_reset(req: SessionIdRequest):
        return workbench.reset_diagnose_session(req.model_dump())

    @app.post("/api/diagnose/stream", tags=["Diagnostic Agent"])
    async def api_diagnose_stream(req: DiagnoseRequest, request: Request):
        """Enqueue a durable diagnose job into the external task queue and tail its resumable SSE log."""
        payload = req.model_dump()
        actor = getattr(request.state, "actor", "local-workbench")
        role = getattr(request.state, "role", "engineer")
        job = workbench.submit_job("diagnose", payload, actor=actor, role=role)
        job_id = job["job_id"]
        return StreamingResponse(
            _tail_durable_job_sse(workbench, job_id, after_seq=0),
            media_type="text/event-stream; charset=utf-8",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Accel-Buffering": "no",
                "X-Job-Id": job_id,
            },
        )

    @app.post(
        "/api/agent/stream",
        tags=["Autonomous Agent"],
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}}, 503: {"description": "Agent 暂未就绪"}},
    )
    async def api_agent_stream(req: AgentStreamRequest):
        run_agent = _load_optional_runner(
            "autonomous_agent", "run_autonomous_agent", "自主深度 Agent 暂未就绪。",
        )
        security = inspect_prompt_security(req.question)
        if not security["allowed"]:
            raise RequestError(400, security["message"])

        client = _stream_model_client(req.mode, workbench.model)

        requested_session = (req.sessionId or "").strip()
        session_id = requested_session or f"agent-{uuid.uuid4().hex}"

        def run(emit):
            runs, artifacts, tools = [], [], []
            content, reasoning = [], []
            plan, badge = [], None
            finished = False

            def merge_run(run):
                run_id = run.get("run_id") or run.get("call_id") or run.get("id")
                if run_id is not None:
                    for index, previous in enumerate(runs):
                        if run_id == (previous.get("run_id") or previous.get("call_id") or previous.get("id")):
                            runs[index] = {**previous, **run}
                            return
                if run not in runs:
                    runs.append(run)

            def persist_then_emit(event):
                nonlocal finished, plan, badge
                if finished:
                    return
                kind = event["type"]
                payload = {k: v for k, v in event.items() if k not in ("type", "ts", "data")}
                payload.update(event.get("data") or {})
                if kind == "code_run":
                    output = payload.get("result") or payload.get("execution") or payload.get("output")
                    run = {k: v for k, v in payload.items() if k not in ("result", "execution", "output")}
                    merge_run({**run, **(output if isinstance(output, dict) else {})})
                elif kind == "artifact":
                    outputs = payload.get("artifacts") or [payload.get("artifact", payload)]
                    for artifact in outputs:
                        if artifact not in artifacts:
                            artifacts.append(artifact)
                elif kind == "tool_end":
                    tools.append(payload)
                elif kind == "plan_update":
                    plan = payload.get("steps", payload.get("plan", payload.get("items", payload.get("todos", plan))))
                elif kind == "memory_suggestion":
                    record_advanced(memory_suggestions=len(payload.get("suggestions") or [payload]))
                elif kind == "verification_repair":
                    if payload.get("status") == "succeeded":
                        content.clear()
                elif kind == "verification_badge":
                    badge = payload.get("verification", payload.get("badge", payload))
                elif kind in ("content_delta", "reasoning_delta"):
                    delta = payload.get("delta", payload.get("text", payload.get("content", "")))
                    if isinstance(delta, str):
                        (content if kind == "content_delta" else reasoning).append(delta)
                elif kind == "done":
                    result = payload.get("result") or {}
                    for run in [*(result.get("code_runs") or []), *(result.get("runs") or [])]:
                        merge_run(run)
                    final_artifacts = list(artifacts)
                    for artifact in result.get("artifacts") or []:
                        if artifact not in final_artifacts:
                            final_artifacts.append(artifact)
                    trace = result.get("tool_trace")
                    if trace:
                        trace = [
                            {**tools[index], **tool}
                            if index < len(tools) and tools[index].get("tool") == tool.get("tool") else tool
                            for index, tool in enumerate(trace)
                        ]
                    history_offset = max(0, len(session.history) - 8)
                    result = jsonable_encoder({
                        "answer_markdown": "".join(content),
                        "plan": plan,
                        "verification_badge": badge,
                        "reasoning": "".join(reasoning),
                        **result,
                        "active_anchors": session.active_anchors,
                        "artifacts": final_artifacts,
                        "tool_trace": trace or tools,
                        "code_runs": runs,
                        "turn_kind": "autonomous",
                        "autonomous_context": {
                            "mine_id": session.mine_id,
                            "station": session.station,
                            "archive_snapshot_sha256": session.archive_snapshot_sha256,
                            "active_anchors": session.active_anchors,
                            "turns": [{**turn, "turn": turn["turn"] - history_offset} for turn in session.turns],
                            "history": [
                                {**snapshot, "turn": snapshot["turn"] - history_offset}
                                for snapshot in session.history[-8:]
                            ],
                        },
                    })
                    save_chat_turn(
                        session_id, req.question, req.mode, result,
                        active_anchors=session.active_anchors,
                        mine_id=session.mine_id, station=session.station,
                        archive_snapshot_sha256=session.archive_snapshot_sha256,
                        path=workbench.sessions_db, preserve_memory=True,
                    )
                    if hasattr(workbench, "_lock") and hasattr(workbench, "_diag_sessions"):
                        with workbench._lock:
                            workbench._diag_sessions.pop(session_id, None)
                    final_badge = result.get("verification_badge") or {}
                    trusted = final_badge.get("business_trusted", final_badge.get("trusted"))
                    is_clarify = result.get("status") == "clarify"
                    with suppress(Exception):
                        from laowadi.llmops import GLOBAL_LLMOPS

                        GLOBAL_LLMOPS.record_step_trace(
                            mode="autonomous",
                            stage="autonomous_turn",
                            latency_ms=float(result.get("elapsed_seconds") or 0.0) * 1000.0,
                            status=str(result.get("status") or "ok"),
                            tool_count=len(trace or tools),
                            error=str(result.get("incomplete_reason") or ""),
                        )
                    record_advanced(
                        autonomous_turns=1, sandbox_runs=len(runs),
                        sandbox_successes=sum(run.get("status") in ("ok", "completed", "success") for run in runs),
                        verification_samples=int(bool(final_badge) and not is_clarify),
                        verified_turns=int((not is_clarify) and (trusted is True or (trusted is None and final_badge.get("status") in ("verified", "passed")))),
                    )
                    finished = True
                    event = {**event, "session_id": session_id, "result": result}
                    if "data" in event:
                        event["data"] = {**event["data"], "session_id": session_id, "result": result}
                emit(event)

            with workbench.archive() as archive:
                # Share verified structured anchors across modes while keeping strict turns_digest isolated.
                session = DiagnosisSession(archive)
                saved = load_session(session_id, path=workbench.sessions_db) if requested_session else None
                if saved:
                    same_scope = (
                        (not saved.get("mine_id") or saved["mine_id"] == session.mine_id)
                        and (not saved.get("station") or saved["station"] == session.station)
                        and (not saved.get("archive_snapshot_sha256") or saved["archive_snapshot_sha256"] == session.archive_snapshot_sha256)
                    )
                    last_auto_idx = -1
                    prior = None
                    for idx in range(len(saved["turns"]) - 1, -1, -1):
                        cand = (saved["turns"][idx]["result"] or {}).get("autonomous_context")
                        if cand:
                            last_auto_idx = idx
                            prior = cand
                            break
                    if prior and (prior.get("mine_id"), prior.get("station"), prior.get("archive_snapshot_sha256")) == (
                        session.mine_id, session.station, session.archive_snapshot_sha256,
                    ):
                        session.active_anchors = {**session.active_anchors, **(prior.get("active_anchors") or {})}
                        session.turns = prior["turns"]
                        session.history = prior["history"]
                    if same_scope and saved["turns"] and last_auto_idx < len(saved["turns"]) - 1:
                        latest_turn_res = saved["turns"][-1].get("result") or {}
                        strict_anchors = (
                            (latest_turn_res.get("context_snapshot") or {}).get("active_anchors")
                            or saved.get("active_anchors")
                            or {}
                        )
                        if strict_anchors:
                            merged_anchors = {**session.active_anchors}
                            for k, v in strict_anchors.items():
                                if isinstance(v, list):
                                    merged_anchors[k] = list(v)
                                elif v is not None:
                                    merged_anchors[k] = v
                            session.active_anchors = merged_anchors
                if req.context:
                    session.apply_external_context(req.context)
                run_agent(
                    archive, req.question, client=client, session=session, max_steps=req.maxSteps,
                    memory_db_path=memory_db_path, on_event=persist_then_emit,
                )

        return _callback_sse_response(run, AUTONOMOUS_EVENT_TYPES, api_logger, "自主深度 Agent 执行中断。")

    @app.post("/api/sandbox/run", tags=["Autonomous Agent"])
    def api_sandbox_run(req: SandboxRunRequest):
        run_python = _load_optional_runner(
            "code_sandbox", "run_python_analysis", "Python 分析沙盒暂未就绪。",
        )
        with workbench.archive() as archive:
            result = run_python(archive, req.code, timeout_s=req.timeout_s)
        record_advanced(sandbox_runs=1, sandbox_successes=int(result.get("status") == "ok"))
        return result

    @app.post(
        "/api/subagents/debate/stream", tags=["Advanced Agent"], response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}}, 503: {"description": "辩论模块暂未就绪"}},
    )
    async def api_subagent_debate(req: SubagentDebateRequest):
        debate = _load_optional_runner("subagent_debate", "run_parallel_subagent_debate", "子智能体辩论模块暂未就绪。")
        security = inspect_prompt_security(req.question)
        if not security["allowed"]:
            raise RequestError(400, security["message"])
        client = _stream_model_client(req.mode, workbench.model)

        def run(emit):
            def on_event(event):
                if event["type"] == "done":
                    record_advanced(debate_runs=1)
                emit(event)

            with workbench.archive() as archive:
                debate(archive, req.question, client=client, memory_db_path=memory_db_path, on_event=on_event)

        return _callback_sse_response(run, DEBATE_EVENT_TYPES, api_logger, "子智能体辩论中断。")

    def skill_runner(name: str):
        return _load_optional_runner("skills_engine", name, "技能与 MCP Client 模块暂未就绪。")

    def builtin_skills(list_skills):
        return list_skills(skills_dir=builtin_skills_dir) if builtin_skills_dir.is_dir() else []

    @app.get("/api/skills", tags=["Advanced Agent"])
    def api_list_skills():
        list_skills = skill_runner("list_skills")
        with _runtime_write_lock(skills_dir):
            return {"skills": [*builtin_skills(list_skills), *list_skills(skills_dir=skills_dir)]}

    @app.post("/api/skills", tags=["Advanced Agent"], status_code=201)
    def api_install_skill(req: SkillCreateRequest):
        create_skill = skill_runner("create_skill")
        with _runtime_write_lock(skills_dir):
            filename = f"{req.skill_id}.md"
            if (builtin_skills_dir / filename).exists() or (skills_dir / filename).exists():
                raise RequestError(409, "该技能 ID 已安装。")
            skill = create_skill(**req.model_dump(), skills_dir=skills_dir)
        record_advanced(skill_installs=1)
        return {"skill": skill}

    @app.post("/api/skills/{skill_id}/run", tags=["Advanced Agent"])
    def api_run_skill(skill_id: SkillIdentifier, req: SkillRunRequest | None = None):
        execute = skill_runner("run_skill")
        with _runtime_write_lock(skills_dir):
            filename = f"{skill_id}.md"
            directory = builtin_skills_dir if (builtin_skills_dir / filename).is_file() else skills_dir
            if not (directory / filename).is_file():
                raise RequestError(404, "未找到指定的技能。")
        options = req or SkillRunRequest()
        with workbench.archive() as archive:
            if options.target_cabinet and (REPORT_STATION, options.target_cabinet) not in archive.devices:
                raise RequestError(404, "归档中未找到目标柜。")
            result = execute(archive, skill_id, **options.model_dump(), skills_dir=directory)
        record_advanced(skill_runs=1)
        return result

    def protocol_runner(name: str):
        return _load_optional_runner("protocol_adapter", name, "协议接入模块暂未就绪。")

    @app.get("/api/mcp/clients", tags=["Advanced Agent"])
    def api_list_mcp_clients():
        list_clients = skill_runner("list_mcp_clients")
        with _runtime_write_lock(mcp_registry_path), workbench.archive() as archive:
            return {"clients": list_clients(archive=archive, registry_path=mcp_registry_path)}

    @app.post("/api/mcp/clients", tags=["Advanced Agent"], status_code=201)
    def api_register_mcp_client(req: MCPClientRegisterRequest):
        register_client = skill_runner("register_mcp_client")
        with _runtime_write_lock(mcp_registry_path):
            client = register_client(**req.model_dump(exclude={"read_only"}), registry_path=mcp_registry_path)
        record_advanced(mcp_registrations=1)
        return {"client": client}

    @app.get("/api/protocols/adapters", tags=["Advanced Agent"])
    def api_protocol_adapters():
        return {"adapters": protocol_runner("list_protocol_adapters")()}

    @app.post("/api/protocols/ingest-frame", tags=["Advanced Agent"])
    def api_protocol_ingest_frame(req: ProtocolFrameRequest):
        ingest_frame = protocol_runner("ingest_protocol_frame")
        protocol = "mqtt_scada" if req.protocol == "mqtt" else req.protocol
        try:
            result = ingest_frame(protocol, req.payload, voltage_limit_kv=req.voltage_limit_kv, live_db_path=live_db_path)
        except (KeyError, TypeError, AttributeError, OverflowError) as exc:
            raise RequestError(400, "遥测帧的字段缺失或数值无效。") from exc
        record_advanced(protocol_frames=1)
        return result

    @app.get("/api/eval/observability", tags=["Advanced Agent"])
    def api_eval_observability():
        summarize = protocol_runner("get_agent_observability_summary")
        unavailable = "Agent 可观测评测暂未就绪。"
        module = _load_optional_module("llmops", unavailable)
        summary = _module_runner(getattr(module, "GLOBAL_LLMOPS", None), "summary", unavailable)()
        list_skills = skill_runner("list_skills")
        with _runtime_write_lock(skills_dir), _runtime_write_lock(mcp_registry_path):
            operational = summarize(
                memory_db_path=memory_db_path, actions_db_path=actions_db_path, live_db_path=live_db_path,
                skills_dir=skills_dir, mcp_registry_path=mcp_registry_path,
            )
            operational["ecosystem"]["skills_count"] += len(builtin_skills(list_skills))

        with advanced_lock:
            counts = dict(advanced_counts)

        def percent(successes, samples):
            return round(100 * counts[successes] / counts[samples], 2) if counts[samples] else None

        fb_summary: dict[str, Any] = {}
        with suppress(Exception):
            from laowadi.feedback_store import feedback_summary as _get_fb_summary

            fb_summary = _get_fb_summary(db_path=feedback_db_path)

        return {
            **operational, "llmops": summary,
            "feedback": fb_summary,
            "scope": "api_worker", "worker_pid": os.getpid(),
            "ttft_ms_avg": summary["ttft_ms_avg"] if summary["ttft_ms_avg"] > 0 else None,
            "ttft_ms_p95": summary["ttft_ms_p95"] if summary["ttft_ms_p95"] > 0 else None,
            "total_tokens": summary["total_tokens"],
            "sandbox_success_rate_pct": percent("sandbox_successes", "sandbox_runs"),
            "verification_rate_pct": percent("verified_turns", "verification_samples"),
            "counts": {**counts, "model_calls": summary["llm_calls_total"]},
        }

    def memory_runner(name: str):
        return _load_optional_runner("memory_store", name, "长期记忆模块暂未就绪。")

    @app.get("/api/memory", tags=["Operational Memory"])
    def api_list_memory(
        category: MemoryCategory | None = None,
        enabled_only: bool = False,
        include_expired: bool | None = None,
    ):
        items = memory_runner("list_memories")(
            category=category,
            enabled_only=enabled_only,
            include_expired=include_expired,
            db_path=memory_db_path,
        )
        return {"memories": items}

    @app.post("/api/memory", tags=["Operational Memory"], status_code=201)
    def api_create_memory(req: MemoryCreateRequest, request: Request):
        create_with_report = memory_runner("create_memory_with_report")
        try:
            out = create_with_report(**req.model_dump(), source=request.state.actor, db_path=memory_db_path)
        except ValueError as exc:
            raise RequestError(400, str(exc)) from None
        record_advanced(memory_writes=1)
        return {
            "memory": out["memory"],
            "conflicts": out["conflicts"],
            "superseded_memory_ids": out["superseded_memory_ids"],
        }

    @app.post("/api/memory/prune-expired", tags=["Operational Memory"])
    def api_prune_expired_memory(req: MemoryPruneRequest | None = None):
        prune = memory_runner("prune_expired_memories")
        return prune(disable_only=(req.disable_only if req else True), db_path=memory_db_path)

    @app.patch("/api/memory/{memory_id}", tags=["Operational Memory"])
    def api_update_memory(memory_id: str, req: MemoryPatchRequest):
        update_with_report = memory_runner("update_memory_with_report")
        changes = req.model_dump(exclude_none=True)
        if not changes:
            raise RequestError(400, "请提供启用状态、有效期或记忆内容。")
        try:
            out = update_with_report(memory_id, **changes, db_path=memory_db_path)
        except ValueError as exc:
            raise RequestError(400, str(exc)) from None
        if out is None:
            raise RequestError(404, "未找到这条长期记忆。")
        return {
            "memory": out["memory"],
            "conflicts": out["conflicts"],
            "superseded_memory_ids": out["superseded_memory_ids"],
        }

    @app.delete("/api/memory/{memory_id}", tags=["Operational Memory"])
    def api_delete_memory(memory_id: str):
        if not memory_runner("delete_memory")(memory_id, db_path=memory_db_path):
            raise RequestError(404, "未找到这条长期记忆。")
        return {"deleted": True, "id": memory_id}

    def feedback_runner(name: str):
        return _load_optional_runner("feedback_store", name, "操作员反馈库暂未就绪。")

    @app.post("/api/feedback", tags=["AgentOps Feedback"], status_code=201)
    def api_record_feedback(req: FeedbackCreateRequest, request: Request):
        rec = feedback_runner("record_feedback")
        row = rec(
            **req.model_dump(),
            actor=getattr(request.state, "actor", "operator"),
            db_path=feedback_db_path,
        )
        return {"feedback": row}

    @app.get("/api/feedback", tags=["AgentOps Feedback"])
    def api_list_feedback(
        verdict: str | None = None,
        target_type: str | None = None,
        negative_only: bool = False,
        limit: int = Query(default=50, ge=1, le=200),
    ):
        try:
            items = feedback_runner("list_feedback")(
                verdict=verdict,
                target_type=target_type,
                negative_only=negative_only,
                limit=limit,
                db_path=feedback_db_path,
            )
        except ValueError as exc:
            raise RequestError(400, str(exc)) from None
        summary = feedback_runner("feedback_summary")(db_path=feedback_db_path)
        return {"items": items, "summary": summary}

    @app.post("/api/feedback/export-negative", tags=["AgentOps Feedback"])
    def api_export_negative_feedback(req: FeedbackExportRequest | None = None):
        opts = req or FeedbackExportRequest()
        return feedback_runner("export_negative_eval_cases")(
            mark_exported=opts.mark_exported,
            include_exported=opts.include_exported,
            limit=opts.limit,
            db_path=feedback_db_path,
        )

    @app.post("/api/intake/image", tags=["Multimodal Field Intake"])
    async def api_intake_image(request: Request):
        extract = _load_optional_runner("multimodal_intake", "extract_field_candidates", "多模态现场取证模块暂未就绪。")
        content_type = request.headers.get("content-type", "")
        media_type = content_type.partition(";")[0].strip().lower()
        if media_type not in {"application/json", "multipart/form-data"}:
            raise RequestError(415, "请使用 multipart/form-data 上传图片文件，或提交 JSON base64 载荷。")

        max_image_bytes = 8 * 1024 * 1024
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > max_image_bytes + 4_194_304:
                raise RequestError(413, "上传载荷超过限制，单张现场照片最大 8 MiB。")
            raw.extend(chunk)

        hint = ""
        mode = "auto"
        if media_type == "application/json":
            try:
                req_obj = MultimodalIntakeJsonRequest.model_validate_json(bytes(raw))
                mime_type = req_obj.mime_type
                hint = req_obj.hint
                mode = req_obj.mode
                image_bytes = base64.b64decode(req_obj.image_base64, validate=True)
            except (ValidationError, ValueError) as exc:
                raise RequestError(400, "图片 JSON 载荷或 base64 编码无效。") from exc
        else:
            header = ("Content-Type: " + content_type + "\r\n\r\n").encode("ascii")
            form = BytesParser(policy=policy.default).parsebytes(header + bytes(raw))
            if form.defects:
                raise RequestError(400, "multipart 图片表单解析失败。")
            file_part = None
            for part in form.iter_parts():
                if part.get_content_disposition() != "form-data":
                    continue
                pname = part.get_param("name", header="content-disposition")
                if pname == "file" and not part.is_multipart():
                    file_part = part
                elif pname == "hint":
                    hint = (part.get_payload(decode=True) or b"").decode("utf-8", errors="ignore").strip()[:1000]
                elif pname == "mode":
                    cand_mode = (part.get_payload(decode=True) or b"").decode("utf-8", errors="ignore").strip()
                    if cand_mode in {"auto", "local", "deepseek"}:
                        mode = cand_mode
            if file_part is None:
                raise RequestError(400, "请在 file 字段上传 PNG/JPEG/WebP 现场照片。")
            image_bytes = file_part.get_payload(decode=True) or b""
            mime_type = (file_part.get_content_type() or "image/png").strip().lower()

        if len(image_bytes) > max_image_bytes:
            raise RequestError(413, "图片文件最多 8 MiB。")

        client = _stream_model_client(mode, workbench.model)
        result = await anyio.to_thread.run_sync(
            lambda: extract(image_bytes, mime_type, client=client, hint=hint)
        )
        if result.get("status") == "invalid_image":
            raise RequestError(400, "上传内容不是受支持的 PNG/JPEG/WebP 现场图片。")
        return result

    def research_runner(name: str):
        return _load_optional_runner("deep_research", name, "深度排查模块暂未就绪。")

    @app.post(
        "/api/deep-research/stream",
        tags=["Deep Research"],
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}}, 503: {"description": "深度排查暂未就绪"}},
    )
    async def api_deep_research(req: DeepResearchRequest):
        investigate = research_runner("run_deep_investigation")
        security = inspect_prompt_security(req.goal)
        if not security["allowed"]:
            raise RequestError(400, security["message"])

        client = _stream_model_client(req.mode, workbench.model)

        def run(emit):
            with workbench.archive() as archive:
                investigate(
                    archive, req.goal, scope=req.scope, client=client, memory_db_path=memory_db_path,
                    reports_dir=reports_dir, on_event=emit,
                )

        return _callback_sse_response(run, RESEARCH_EVENT_TYPES, api_logger, "深度排查执行中断。")

    @app.get("/api/deep-research/reports", tags=["Deep Research"])
    def api_deep_reports():
        reports = research_runner("list_deep_reports")(reports_dir=reports_dir)
        return {"reports": reports}

    @app.get(
        "/api/deep-research/reports/{report_id}/export",
        tags=["Deep Research"],
        response_class=Response,
        responses={200: {"content": {"text/markdown": {}, "text/html": {}}}},
    )
    def api_export_deep_report(
        report_id: str,
        report_format: Annotated[Literal["md", "html"], Query(alias="format")] = "md",
    ):
        export = research_runner("export_deep_report")
        try:
            content, mime, filename = export(report_id, fmt=report_format, reports_dir=reports_dir)
        except FileNotFoundError as exc:
            raise RequestError(404, "未找到深度排查报告。") from exc
        headers = _security_headers({"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename, safe="")})
        if report_format == "html":
            headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
        return Response(content, media_type=mime, headers=headers)

    def live_runner(name: str):
        return _load_optional_runner("ingest_live", name, "实时监测与增量上传模块暂未就绪。")

    @app.post(
        "/api/live/upload-report", tags=["Live Monitoring"], status_code=201,
        openapi_extra={"requestBody": {"required": True, "content": {
            "application/json": {"schema": LiveUploadRequest.model_json_schema()},
            "text/csv": {"schema": {"type": "string"}},
            "multipart/form-data": {"schema": {"type": "object", "required": ["file"], "properties": {
                "file": {"type": "string", "format": "binary"},
            }}},
        }}},
    )
    async def api_live_upload(request: Request):
        ingest = live_runner("ingest_csv_report")
        upload = await _read_live_csv_upload(request)

        def run():
            try:
                _validate_csv_measurements(upload.csv_text)
                return ingest(upload.csv_text, filename=upload.filename, live_db_path=live_db_path)
            except csv.Error as exc:
                raise RequestError(400, "CSV 格式错误或单个字段过长。") from exc

        return await anyio.to_thread.run_sync(run)

    @app.get("/api/live/uploads", tags=["Live Monitoring"])
    def api_live_uploads():
        return {"uploads": live_runner("list_live_uploads")(live_db_path=live_db_path)}

    @app.post(
        "/api/live/stream", tags=["Live Monitoring"], response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}}, 503: {"description": "演练模块暂未就绪"}},
    )
    async def api_live_stream(req: LiveStreamRequest):
        stream = live_runner("stream_live_telemetry")

        def run(emit):
            with workbench.archive() as archive:
                try:
                    stream(
                        archive, day=req.day.isoformat(), speed_ms=req.speed_ms,
                        custom_threshold_kv=req.custom_threshold_kv, memory_db_path=memory_db_path,
                        live_db_path=live_db_path, on_event=emit,
                    )
                except ValueError as exc:
                    emit({"type": "error", "error": str(exc)})

        return _callback_sse_response(run, LIVE_EVENT_TYPES, api_logger, "实时监测演练中断。")

    actions_unavailable = "两票审批模块暂未就绪。"

    def action_runner(name: str):
        return _load_optional_runner("action_tickets", name, actions_unavailable)

    def display_dispatch(log: dict):
        target = log.get("target") or ""
        if target.startswith("https://"):
            log = {**log, "target": "https://" + (urlsplit(target).hostname or "")}
        return log

    def display_ticket(ticket: dict):
        dispatch = ticket.get("dispatch_log") or []
        if not dispatch:
            return ticket
        public_dispatch = [display_dispatch(log) for log in dispatch]
        replacements = [(raw["target"], public["target"]) for raw, public in zip(dispatch, public_dispatch)
                        if raw.get("target") != public.get("target")]
        audit = []
        for entry in ticket.get("audit_log") or []:
            detail = entry.get("detail", "")
            for raw, public in replacements:
                detail = detail.replace(raw, public)
            audit.append({**entry, "detail": detail})
        return {**ticket, "audit_log": audit, "dispatch_log": public_dispatch}

    @app.get("/api/actions/tickets", tags=["Action Tickets"])
    def api_action_tickets(
        status: Literal["draft", "approved", "rejected", "dispatched"] | None = None,
        kind: ActionTicketKind | None = None,
    ):
        tickets = action_runner("list_action_tickets")(status=status, kind=kind, db_path=actions_db_path)
        return {"tickets": [display_ticket(ticket) for ticket in tickets], "webhook_configured": bool(action_webhook_url)}

    @app.post("/api/actions/tickets/draft", tags=["Action Tickets"], status_code=201)
    def api_action_draft(req: ActionDraftRequest, request: Request):
        draft = action_runner("draft_action_ticket")
        with workbench.archive() as archive:
            ticket = draft(archive, **req.model_dump(), actor=request.state.actor, db_path=actions_db_path)
        return {"ticket": display_ticket(ticket)}

    def transition_action(name: str, ticket_id: str, actor: str, **fields):
        module = _load_optional_module("action_tickets", actions_unavailable)
        transition = _module_runner(module, name, actions_unavailable)
        conflict = _module_runner(module, "ChecklistIncompleteError", actions_unavailable)
        with _runtime_write_lock(actions_db_path):
            try:
                if name in {"approve_action_ticket", "dispatch_action_webhook"}:
                    get_ticket = _module_runner(module, "get_action_ticket", actions_unavailable)
                    current = get_ticket(ticket_id, db_path=actions_db_path)
                    if current is None:
                        raise RequestError(404, "未找到两票记录。")
                    if name == "approve_action_ticket":
                        required = [item for item in current["checklist"] if item.get("required")]
                        if not required or any(not (item.get("checked") and item.get("checked_by") and item.get("checked_at"))
                                               for item in required):
                            raise RequestError(409, "请先逐项人工核对并勾选全部必选安全项。")
                    elif not fields["dry_run"]:
                        delivered = next((log for log in current["dispatch_log"] if log.get("status") == "delivered"), None)
                        if delivered:
                            return {"ticket": current, "dispatch": delivered}
                        if not action_webhook_url:
                            raise RequestError(409, "尚未配置正式 Webhook，可先进行通知演练。")
                        try:
                            destination = urlsplit(action_webhook_url)
                            if destination.scheme != "https" or not destination.hostname or destination.username or destination.password:
                                raise ValueError
                        except ValueError as exc:
                            raise RequestError(409, "正式 Webhook 配置须使用有效的 HTTPS 地址。") from exc
                        fields["webhook_url"] = action_webhook_url
                updated = transition(ticket_id, actor=actor, db_path=actions_db_path, **fields)
            except conflict as exc:
                raise RequestError(409, str(exc)) from exc
        if updated is None:
            raise RequestError(404, "未找到两票记录。")
        return updated

    @app.post("/api/actions/tickets/{ticket_id}/checklist", tags=["Action Tickets"])
    def api_action_checklist(ticket_id: str, req: ActionChecklistRequest, request: Request):
        ticket = transition_action("toggle_checklist_item", ticket_id, request.state.actor, **req.model_dump())
        return {"ticket": display_ticket(ticket)}

    @app.post("/api/actions/tickets/{ticket_id}/approve", tags=["Action Tickets"])
    def api_action_approve(ticket_id: str, request: Request, req: ActionApproveRequest | None = None):
        ticket = transition_action("approve_action_ticket", ticket_id, request.state.actor, comment=req.note if req else "")
        return {"ticket": display_ticket(ticket)}

    @app.post("/api/actions/tickets/{ticket_id}/reject", tags=["Action Tickets"])
    def api_action_reject(ticket_id: str, req: ActionRejectRequest, request: Request):
        ticket = transition_action("reject_action_ticket", ticket_id, request.state.actor, reason=req.reason)
        return {"ticket": display_ticket(ticket)}

    @app.post("/api/actions/tickets/{ticket_id}/dispatch", tags=["Action Tickets"])
    def api_action_dispatch(ticket_id: str, request: Request, req: ActionDispatchRequest | None = None):
        result = transition_action("dispatch_action_webhook", ticket_id, request.state.actor, dry_run=req.dry_run if req else True)
        return {"ticket": display_ticket(result["ticket"]), "dispatch": display_dispatch(result["dispatch"])}

    # ── State Graph, Supplement Recalculation & Evaluation ─────────────────────
    @app.get("/api/supplement", tags=["State Graph & Evaluation"])
    def api_get_supplement():
        return workbench.get_supplement()

    @app.post("/api/supplement", tags=["State Graph & Evaluation"])
    def api_post_supplement(req: SupplementRequest):
        return workbench.post_supplement(req.model_dump())

    @app.get("/api/evaluation", tags=["State Graph & Evaluation"])
    def api_get_evaluation():
        return workbench.get_evaluation()

    @app.post("/api/graph/demo", tags=["State Graph & Evaluation"])
    def api_graph_demo(req: GraphDemoRequest):
        return workbench.run_graph_demo(req.model_dump())

    # ── Archived Originals (Images & Excel Spreadsheets with SHA-256 Check) ────
    @app.get("/api/image/{name:path}", tags=["Archive Originals"])
    def api_image(name: str):
        decoded = unquote(name)
        data = workbench.original(decoded)
        return Response(
            content=data,
            media_type=guess_type(decoded)[0] or "image/png",
            headers=_security_headers({"Content-Disposition": "inline"}),
        )

    @app.get("/api/source/{name:path}", tags=["Archive Originals"])
    def api_source(name: str):
        decoded = unquote(name)
        data = workbench.original(decoded)
        return Response(
            content=data,
            media_type=guess_type(decoded)[0] or "application/octet-stream",
            headers=_security_headers({"Content-Disposition": f"attachment; filename*=UTF-8''{quote(decoded)}"}),
        )

    # ── Static React 19 / Meta Astryx Workbench Bundle ─────────────────────────
    @app.get("/", include_in_schema=False)
    def serve_index():
        path = (BUILD / "index.html").resolve()
        if not path.is_file():
            raise RequestError(404, "页面或静态文件不存在。")
        return Response(
            content=path.read_bytes(),
            media_type="text/html; charset=utf-8",
            headers=_security_headers(),
        )

    @app.get("/assets/{asset_path:path}", include_in_schema=False)
    def serve_assets(asset_path: str):
        decoded = unquote(asset_path).lstrip("/")
        path = (BUILD / "assets" / decoded).resolve()
        assets_root = (BUILD / "assets").resolve()
        if not path.is_relative_to(assets_root) or not path.is_file():
            raise RequestError(404, "页面或静态文件不存在。")
        return Response(
            content=path.read_bytes(),
            media_type=guess_type(path.name)[0] or "application/octet-stream",
            headers=_security_headers(),
        )

    return app
