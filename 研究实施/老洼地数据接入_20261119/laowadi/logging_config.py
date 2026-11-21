"""Enterprise Structured JSON Logging with ContextVar Trace Correlation & Secret Redaction.

Compatible with ELK, Grafana Loki, Fluent Bit, and Datadog log pipelines:
- Emits single-line JSON (NDJSON) to stdout and optional rotating log file (`local/logs/laowadi.jsonl`).
- Automatically attaches `timestamp`, `level`, `service`, `component`, `pid`, `thread`,
  `trace_id`, `worker_id`, `actor`, `role`, `event`, `message`, and structured `fields`.
- Automatically scrubs credentials (PostgreSQL passwords in DSNs, Bearer tokens, API keys).
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import threading
from typing import Any, Iterator

from laowadi.storage import ROOT


SERVICE_NAME = "laowadi-power-agent"
DEFAULT_LOG_FILE = ROOT / "local" / "logs" / "laowadi.jsonl"

# Per-request / per-job correlation context
trace_id_var: ContextVar[str] = ContextVar("laowadi_trace_id", default="")
actor_var: ContextVar[str] = ContextVar("laowadi_actor", default="")
role_var: ContextVar[str] = ContextVar("laowadi_role", default="")
worker_id_var: ContextVar[str] = ContextVar("laowadi_worker_id", default="")

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(postgresql(?:\+\w+)?://[^:/\s]+:)([^@\s]+)(@)", re.I), r"\1***\3"),
    (re.compile(r"(redis://:?)([^@\s]+)(@)", re.I), r"\1***\3"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{12,}", re.I), r"\1***"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"), "sk-***"),
]

_CONFIG_LOCK = threading.Lock()
_CONFIGURED = False
_ACTIVE_LOG_FILE: Path | None = None


def scrub_secrets(value: Any) -> Any:
    """Recursively mask credentials, tokens, and DSN passwords before serializing logs."""
    if isinstance(value, str):
        out = value
        for pat, repl in _SECRET_PATTERNS:
            out = pat.sub(repl, out)
        return out
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for k, v in value.items():
            kl = str(k).lower()
            if any(s in kl for s in ("password", "secret", "api_key", "authorization", "access_token")):
                cleaned[k] = "***"
            else:
                cleaned[k] = scrub_secrets(v)
        return cleaned
    if isinstance(value, list):
        return [scrub_secrets(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    """Formats every Python LogRecord as a single-line RFC3339-UTC JSON object."""

    def __init__(self, service: str = SERVICE_NAME):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        payload: dict[str, Any] = {
            "timestamp": ts,
            "level": record.levelname,
            "service": self.service,
            "component": record.name,
            "pid": record.process,
            "thread": record.threadName,
            "event": getattr(record, "event", None) or "log",
            "message": scrub_secrets(record.getMessage()),
        }
        tid = getattr(record, "trace_id", None) or trace_id_var.get()
        if tid:
            payload["trace_id"] = tid
        wid = getattr(record, "worker_id", None) or worker_id_var.get()
        if wid:
            payload["worker_id"] = wid
        actor = getattr(record, "actor", None) or actor_var.get()
        if actor:
            payload["actor"] = actor
        role = getattr(record, "role", None) or role_var.get()
        if role:
            payload["role"] = role

        extra_fields = getattr(record, "fields", None)
        if isinstance(extra_fields, dict) and extra_fields:
            payload["fields"] = scrub_secrets(extra_fields)

        if record.exc_info:
            payload["exception"] = scrub_secrets(self.formatException(record.exc_info))

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


@contextmanager
def bind_log_context(
    *,
    trace_id: str | None = None,
    actor: str | None = None,
    role: str | None = None,
    worker_id: str | None = None,
) -> Iterator[None]:
    tokens = []
    if trace_id is not None:
        tokens.append((trace_id_var, trace_id_var.set(trace_id)))
    if actor is not None:
        tokens.append((actor_var, actor_var.set(actor)))
    if role is not None:
        tokens.append((role_var, role_var.set(role)))
    if worker_id is not None:
        tokens.append((worker_id_var, worker_id_var.set(worker_id)))
    try:
        yield
    finally:
        for var, tok in reversed(tokens):
            var.reset(tok)


def configure_logging(
    *,
    level: str | None = None,
    fmt: str | None = None,
    log_file: Path | str | None = None,
    force: bool = False,
) -> logging.Logger:
    """Configure root `laowadi` structured JSON logger (stderr + optional rotating NDJSON file)."""
    global _CONFIGURED, _ACTIVE_LOG_FILE
    with _CONFIG_LOCK:
        if _CONFIGURED and not force:
            return logging.getLogger("laowadi")

        eff_level = (level or os.environ.get("LAOWADI_LOG_LEVEL", "INFO")).upper()
        eff_fmt = (fmt or os.environ.get("LAOWADI_LOG_FORMAT", "json")).lower()
        file_env = os.environ.get("LAOWADI_LOG_FILE", "").strip()
        target_file = Path(log_file) if log_file is not None else (Path(file_env) if file_env else DEFAULT_LOG_FILE)

        logger = logging.getLogger("laowadi")
        logger.setLevel(getattr(logging, eff_level, logging.INFO))
        logger.propagate = False
        for h in list(logger.handlers):
            logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

        formatter: logging.Formatter = (
            JsonFormatter()
            if eff_fmt == "json"
            else logging.Formatter("%(asctime)s %(levelname)s [%(name)s pid=%(process)d] %(message)s")
        )

        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(formatter)
        logger.addHandler(sh)

        try:
            target_file.parent.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(
                str(target_file),
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            fh.setFormatter(JsonFormatter())
            logger.addHandler(fh)
            _ACTIVE_LOG_FILE = target_file
        except OSError:
            _ACTIVE_LOG_FILE = None

        _CONFIGURED = True
        return logger


def get_logger(component: str) -> logging.Logger:
    if not _CONFIGURED:
        configure_logging()
    name = component if component.startswith("laowadi") else f"laowadi.{component}"
    return logging.getLogger(name)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    message: str,
    **fields: Any,
) -> None:
    logger.log(level, message, extra={"event": event, "fields": fields})


def tail_structured_logs(
    *,
    limit: int = 50,
    level: str | None = None,
    event_prefix: str | None = None,
    log_file: Path | None = None,
) -> dict[str, Any]:
    """Read the most recent structured JSON log entries from the rotating NDJSON file."""
    path = log_file or _ACTIVE_LOG_FILE or DEFAULT_LOG_FILE
    lim = max(1, min(int(limit), 200))
    entries: list[dict[str, Any]] = []
    if path.is_file():
        buf: deque[str] = deque(maxlen=lim * 4)
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if s:
                    buf.append(s)
        for line in reversed(buf):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if level and str(rec.get("level", "")).upper() != level.upper():
                continue
            if event_prefix and not str(rec.get("event", "")).startswith(event_prefix):
                continue
            entries.append(rec)
            if len(entries) >= lim:
                break
    return {
        "log_file": str(path),
        "format": "json (NDJSON)",
        "count": len(entries),
        "entries": entries,
    }
