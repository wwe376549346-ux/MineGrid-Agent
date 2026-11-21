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
from contextlib import suppress
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
import json
import logging
from mimetypes import guess_type
import os
from pathlib import Path
import sqlite3
import time
from typing import Annotated
from urllib.parse import quote, unquote
import uuid

import anyio
from fastapi import FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from psycopg import Error as PostgresError

from laowadi.db.postgres import probe_runtime
from laowadi.source_files import document_manifest, verified_original
from laowadi.logging_config import bind_log_context, configure_logging, get_logger, log_event
from laowadi.llm import ChatClient, LLMError
from laowadi.mine_schema import load_mine_profile
from laowadi.schemas import (
    AgentStreamRequest,
    AskRequest,
    CircuitBreakerResetRequest,
    CircuitBreakerTripRequest,
    DAGResumeRequest,
    DAGRunRequest,
    DiagnoseRequest,
    GraphDemoRequest,
    HealthzResponse,
    JobSubmitRequest,
    MultiAgentConsultRequest,
    OutboxDispatchRequest,
    PowerFlowRequest,
    ProactiveReconcileRequest,
    ProactiveScanRequest,
    ReadyzResponse,
    ReviewCreateRequest,
    ReviewDeleteRequest,
    SandboxRunRequest,
    SecurityPromptTestRequest,
    SensitivityVoiQuery,
    SessionIdRequest,
    StorageMigrateRequest,
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
from laowadi.task_queue import TERMINAL_STATUSES
from laowadi.workbench import BUILD, RequestError, Workbench


WEEKLY_SCHEDULE_PATH = "/api/powerflow/weekly-schedule"
SENSITIVITY_VOI_PATH = "/api/powerflow/sensitivity-voi"
READ_ONLY_COMPUTE_PATHS = {WEEKLY_SCHEDULE_PATH, SENSITIVITY_VOI_PATH}

COMPUTE_RATE_LIMITED_PATHS = {
    "/api/diagnose",
    "/api/diagnose/stream",
    "/api/agent/stream",
    "/api/sandbox/run",
    "/api/powerflow",
    *READ_ONLY_COMPUTE_PATHS,
    "/api/multi-agent/consult",
    "/api/jobs",
}

AUDITED_PATHS = {
    "/api/auth/token",
    "/api/jobs",
    "/api/ask",
    "/api/diagnose",
    "/api/diagnose/stream",
    "/api/agent/stream",
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
    "content_delta", "verification_badge", "done", "error",
}


def _load_optional_runner(module: str, name: str, unavailable: str):
    try:
        runner = getattr(import_module(f"laowadi.{module}"), name, None)
    except ImportError as exc:
        raise RequestError(503, unavailable) from exc
    if not callable(runner):
        raise RequestError(503, unavailable)
    return runner


def _security_headers(headers: dict[str, str] | None = None) -> dict[str, str]:
    base = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if headers:
        base.update(headers)
    return base


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
            if action or path in AUDITED_PATHS or resp.status_code in (401, 403, 429):
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
        if method == "POST":
            req_role = {
                "/api/agent/stream": "dispatcher",
                "/api/sandbox/run": "engineer",
            }.get(path, req_role)
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
        if path in COMPUTE_RATE_LIMITED_PATHS and (
            method == "POST" or (method == "GET" and path in READ_ONLY_COMPUTE_PATHS)
        ):
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

        client = None
        if req.mode == "deepseek" or (req.mode == "auto" and workbench.model is not None):
            try:
                client = ChatClient.from_env(stream=True)
            except LLMError:
                if req.mode == "deepseek":
                    raise RequestError(409, "本次工作台尚未启用可用的 DeepSeek 配置。") from None

        async def events():
            sender, receiver = anyio.create_memory_object_stream[str](16)

            def emit(event):
                kind = event["type"]
                if kind in AUTONOMOUS_EVENT_TYPES:
                    # Serialize here because the runner mutates plan steps between callbacks.
                    data = json.dumps(jsonable_encoder(event), ensure_ascii=False, allow_nan=False)
                    anyio.from_thread.run(sender.send, f"event: {kind}\ndata: {data}\n\n")

            def run():
                with workbench.archive() as archive:
                    session = workbench._get_or_restore_session(archive, req.sessionId) if req.sessionId else None
                    run_agent(archive, req.question, client=client, session=session, max_steps=req.maxSteps, on_event=emit)

            async def produce():
                async with sender:
                    try:
                        await anyio.to_thread.run_sync(run, abandon_on_cancel=True)
                    except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                        return
                    except Exception:
                        api_logger.exception("Autonomous agent stream failed")
                        data = json.dumps({"type": "error", "error": "自主深度 Agent 执行中断。"}, ensure_ascii=False)
                        await sender.send(f"event: error\ndata: {data}\n\n")

            worker = asyncio.create_task(produce())
            try:
                async with receiver:
                    async for frame in receiver:
                        yield frame
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers=_security_headers({"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"}),
        )

    @app.post("/api/sandbox/run", tags=["Autonomous Agent"])
    def api_sandbox_run(req: SandboxRunRequest):
        run_python = _load_optional_runner(
            "code_sandbox", "run_python_analysis", "Python 分析沙盒暂未就绪。",
        )
        with workbench.archive() as archive:
            return run_python(archive, req.code, timeout_s=req.timeout_s)

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
