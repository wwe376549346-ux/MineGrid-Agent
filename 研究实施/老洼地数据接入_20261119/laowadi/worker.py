"""Decoupled Out-of-Process External Job Worker Pool.

Runs independently from the FastAPI/Uvicorn web server processes (`python -m laowadi worker`).
- Atomically claims jobs from DurableSQLiteBroker (or Redis Streams).
- Persists every live SSE event (`plan`, `tool_start`, `tool_end`, `llm_delta`, `verify`, `done`)
  to the durable `job_event` log so clients can seamlessly resume after any web server restart.
- Renews job lease & worker heartbeat on a background thread; if a worker process crashes (SIGKILL),
  another worker automatically reclaims the expired lease and resumes execution.
- Integrates Cross-Process SharedCircuitBreaker (`llm:deepseek`) with automatic graceful degradation.
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path
import signal
import threading
import time
from typing import Any
import uuid

from laowadi.audit import AUDIT_DB
from laowadi.circuit_breaker import SharedCircuitBreaker
from laowadi.logging_config import bind_log_context, configure_logging, get_logger, log_event
from laowadi.model import load_project_model_config
from laowadi.monitor import EVENT_DB
from laowadi.reviews import REVIEW_DB
from laowadi.sessions import SESSION_DB
from laowadi.storage import DB
from laowadi.task_queue import JOBS_DB, DurableSQLiteBroker, create_task_broker
from laowadi.workbench import DEFAULT_MODEL_CONFIG, SOURCE_DIR, Workbench


class ExternalJobWorker:
    """Dedicated out-of-process job consumer executing long-running diagnostic & powerflow tasks."""

    def __init__(
        self,
        *,
        db_path: Path | str = DB,
        events_path: Path | str = EVENT_DB,
        source_dir: Path | str = SOURCE_DIR,
        model_config: Path | str | None = None,
        jobs_db: Path | str = JOBS_DB,
        reviews_db: Path | str = REVIEW_DB,
        sessions_db: Path | str = SESSION_DB,
        audit_db: Path | str = AUDIT_DB,
        worker_id: str | None = None,
        lease_seconds: float = 20.0,
        poll_interval: float = 0.15,
    ):
        model = os.environ.get("COAL_API_MODEL") if os.environ.get("COAL_API_KEY") else None
        cfg_path = Path(model_config) if model_config else (DEFAULT_MODEL_CONFIG if DEFAULT_MODEL_CONFIG.is_file() else None)
        if cfg_path and cfg_path.is_file():
            cfg = load_project_model_config(cfg_path)
            os.environ.update(cfg)
            model = cfg.get("COAL_API_MODEL")

        self.worker_id = worker_id or f"worker-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        configure_logging()
        self._logger = get_logger("worker")
        self.lease_seconds = max(3.0, float(lease_seconds))
        self.poll_interval = max(0.05, float(poll_interval))
        self.broker: DurableSQLiteBroker = create_task_broker(jobs_db)
        self.llm_breaker = SharedCircuitBreaker("llm:deepseek", db_path=self.broker.path)
        self.workbench = Workbench(
            Path(db_path),
            Path(events_path),
            Path(source_dir),
            model,
            reviews_db=reviews_db,
            sessions_db=sessions_db,
            audit_db=audit_db,
            jobs_db=self.broker.path,
        )
        self._stop_event = threading.Event()
        self._active_job_id: str | None = None
        self._processed_count = 0

    def _heartbeat_loop(self) -> None:
        interval = max(0.5, min(3.0, self.lease_seconds / 4.0))
        while not self._stop_event.is_set():
            active = self._active_job_id
            try:
                self.broker.record_worker_heartbeat(
                    self.worker_id,
                    worker_kind="external_worker",
                    status="busy" if active else "idle",
                    active_job_id=active,
                    processed_count=self._processed_count,
                )
                if active:
                    self.broker.renew_lease(active, self.worker_id, lease_seconds=self.lease_seconds)
            except Exception:
                pass
            self._stop_event.wait(interval)

    def _execute_job(self, job: dict[str, Any]) -> None:
        job_id = job["job_id"]
        job_type = job["job_type"]
        payload = dict(job.get("payload") or {})
        step_delay = min(1.5, max(0.0, float(payload.pop("_test_step_delay_s", 0.0) or 0.0)))

        def emit_durable(event_obj: dict[str, Any]) -> None:
            self.broker.append_event(job_id, event_obj)
            if step_delay > 0.0:
                time.sleep(step_delay)

        self._active_job_id = job_id
        t_job = time.monotonic()
        with bind_log_context(worker_id=self.worker_id, trace_id=job_id):
            log_event(self._logger, logging.INFO, "job_claimed", f"Claimed {job_type} job {job_id}", job_id=job_id, job_type=job_type)
        try:
            self.broker.record_worker_heartbeat(
                self.worker_id,
                worker_kind="external_worker",
                status="busy",
                active_job_id=job_id,
                processed_count=self._processed_count,
            )

            if job_type == "diagnose":
                result = self._run_diagnose_with_breaker(job_id, payload, emit_durable)
            elif job_type == "multi_agent":
                result = self._run_multi_agent_with_breaker(job_id, payload, emit_durable)
            elif job_type == "powerflow":
                emit_durable({"type": "plan", "message": "外部工作进程正在执行 pandapower Newton-Raphson 交流潮流求解…"})
                result = self.workbench.powerflow(payload)
                emit_durable({"type": "powerflow_solved", "scenario": result.get("scenario"), "converged": result.get("solver", {}).get("converged")})
            else:
                raise ValueError(f"不支持的任务类型：{job_type}")

            if self._stop_event.is_set():
                self.broker.release_job_on_shutdown(job_id, self.worker_id)
                return

            result = {**result, "job_id": job_id, "executed_by_worker": self.worker_id, "executed_by_pid": os.getpid()}
            self.broker.complete_job(job_id, self.worker_id, result)
            self._processed_count += 1
            with bind_log_context(worker_id=self.worker_id, trace_id=job_id):
                log_event(
                    self._logger,
                    logging.INFO,
                    "job_completed",
                    f"Completed {job_type} job {job_id}",
                    job_id=job_id,
                    job_type=job_type,
                    elapsed_s=round(time.monotonic() - t_job, 3),
                )
        except Exception as exc:
            with bind_log_context(worker_id=self.worker_id, trace_id=job_id):
                log_event(
                    self._logger,
                    logging.ERROR,
                    "job_failed",
                    f"Job {job_id} failed: {exc}",
                    job_id=job_id,
                    job_type=job_type,
                    error=str(exc),
                )
            if self._stop_event.is_set():
                self.broker.release_job_on_shutdown(job_id, self.worker_id)
            else:
                self.broker.fail_job(job_id, self.worker_id, str(exc), retryable=False)
        finally:
            self._active_job_id = None

    def _run_diagnose_with_breaker(self, job_id: str, payload: dict[str, Any], emit_durable) -> dict[str, Any]:
        req = dict(payload)
        mode = req.get("mode", "local")
        if mode != "deepseek":
            return self.workbench.diagnose(req, on_event=emit_durable, use_breaker=False)

        allowed, snap = self.llm_breaker.allow_request()
        if not allowed:
            emit_durable({
                "type": "circuit_breaker_degraded",
                "breaker": snap,
                "message": (
                    f"跨进程熔断器 [{snap['name']}] 当前处于 OPEN 状态（剩余冷却 {snap['cooldown_remaining_s']}s），"
                    "已零等待熔断并自动降级为本地 11 工具确定性诊断基线。"
                ),
            })
            req["mode"] = "local"
            res = self.workbench.diagnose(req, on_event=emit_durable, use_breaker=False)
            res["circuit_breaker_degraded"] = True
            res["circuit_breaker"] = snap
            return res

        res = self.workbench.diagnose(req, on_event=emit_durable, use_breaker=False)
        if res.get("status") == "error" or res.get("stop_reason") == "model_error":
            err_msg = str(res.get("error") or "upstream model error")
            new_snap = self.llm_breaker.record_failure(err_msg)
            emit_durable({
                "type": "circuit_breaker_degraded",
                "breaker": new_snap,
                "message": (
                    f"上游模型调用异常（{err_msg[:80]}），跨进程熔断器已记障（{new_snap['failure_count']}/{new_snap['failure_threshold']}，状态 {new_snap['state']}），"
                    "自动降级至本地 11 工具基线确保值班诊断不中断。"
                ),
            })
            req["mode"] = "local"
            fallback = self.workbench.diagnose(req, on_event=emit_durable, use_breaker=False)
            fallback["circuit_breaker_degraded"] = True
            fallback["circuit_breaker"] = new_snap
            fallback["upstream_error"] = err_msg
            return fallback

        self.llm_breaker.record_success()
        res["circuit_breaker_degraded"] = False
        res["circuit_breaker"] = self.llm_breaker.snapshot()
        return res

    def _run_multi_agent_with_breaker(self, job_id: str, payload: dict[str, Any], emit_durable) -> dict[str, Any]:
        req = dict(payload)
        mode = req.get("mode", "local")
        if mode != "deepseek":
            return self.workbench.multi_agent_consult(req, on_event=emit_durable)

        allowed, snap = self.llm_breaker.allow_request()
        if not allowed:
            emit_durable({
                "type": "circuit_breaker_degraded",
                "breaker": snap,
                "message": f"跨进程熔断器 [{snap['name']}] 处于 OPEN 状态，多智体仲裁已自动降级为本地共识综合。",
            })
            req["mode"] = "local"
            res = self.workbench.multi_agent_consult(req, on_event=emit_durable)
            res["circuit_breaker_degraded"] = True
            res["circuit_breaker"] = snap
            return res

        res = self.workbench.multi_agent_consult(req, on_event=emit_durable)
        self.llm_breaker.record_success()
        res["circuit_breaker_degraded"] = False
        res["circuit_breaker"] = self.llm_breaker.snapshot()
        return res

    def run_once(self) -> bool:
        """Claim and process a single job if available (useful for deterministic unit tests)."""
        job = self.broker.claim_next_job(self.worker_id, lease_seconds=self.lease_seconds)
        if job is None:
            return False
        self._execute_job(job)
        return True

    def stop(self) -> None:
        self._stop_event.set()

    def run_forever(self, *, install_signals: bool = True) -> None:
        if install_signals:
            def _handle_sig(_signum, _frame):
                self._stop_event.set()

            signal.signal(signal.SIGTERM, _handle_sig)
            signal.signal(signal.SIGINT, _handle_sig)

        self.broker.record_worker_heartbeat(
            self.worker_id,
            worker_kind="external_worker",
            status="idle",
            processed_count=0,
        )
        hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        hb_thread.start()
        print(
            f"[ExternalWorker] {self.worker_id} (PID {os.getpid()}) 已启动，监听队列 {self.broker.path}",
            flush=True,
        )
        try:
            while not self._stop_event.is_set():
                did_work = self.run_once()
                if not did_work:
                    self._stop_event.wait(self.poll_interval)
        finally:
            self._stop_event.set()
            hb_thread.join(timeout=2.0)
            self.broker.mark_worker_stopped(self.worker_id)
            print(
                f"[ExternalWorker] {self.worker_id} (PID {os.getpid()}) 已优雅停止（累计处理 {self._processed_count} 个任务）",
                flush=True,
            )


def run_worker_process(**kwargs) -> None:
    ExternalJobWorker(**kwargs).run_forever(install_signals=True)
