"""Importable ASGI factory module enabling true multi-worker Uvicorn scaling (`uvicorn laowadi.asgi:app --workers N`).

Each Uvicorn process builds its own FastAPI app connected to the configured shared operational stores.
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path
import threading

from laowadi.api import create_app
from laowadi.audit import AUDIT_DB
from laowadi.logging_config import configure_logging, get_logger, log_event
from laowadi.model import load_project_model_config
from laowadi.monitor import EVENT_DB
from laowadi.reviews import REVIEW_DB
from laowadi.sessions import SESSION_DB
from laowadi.storage import DB
from laowadi.task_queue import JOBS_DB
from laowadi.workbench import DEFAULT_MODEL_CONFIG, SOURCE_DIR, Workbench


def build_asgi_app():
    db_path = Path(os.environ.get("LAOWADI_DB", str(DB)))
    events_path = Path(os.environ.get("LAOWADI_EVENTS_DB", str(EVENT_DB)))
    source_dir = Path(os.environ.get("LAOWADI_SOURCE_DIR", str(SOURCE_DIR)))
    reviews_db = Path(os.environ.get("LAOWADI_REVIEWS_DB", str(REVIEW_DB)))
    sessions_db = Path(os.environ.get("LAOWADI_SESSIONS_DB", str(SESSION_DB)))
    audit_db = Path(os.environ.get("LAOWADI_AUDIT_DB", str(AUDIT_DB)))
    jobs_db = Path(os.environ.get("LAOWADI_JOBS_DB", str(JOBS_DB)))

    model = os.environ.get("COAL_API_MODEL") if os.environ.get("COAL_API_KEY") else None
    cfg_env = os.environ.get("LAOWADI_MODEL_CONFIG", "").strip()
    cfg_path = Path(cfg_env) if cfg_env else (DEFAULT_MODEL_CONFIG if DEFAULT_MODEL_CONFIG.is_file() else None)
    if cfg_path and cfg_path.is_file():
        cfg = load_project_model_config(cfg_path)
        os.environ.update(cfg)
        model = cfg.get("COAL_API_MODEL")

    wb = Workbench(
        db_path,
        events_path,
        source_dir,
        model,
        reviews_db=reviews_db,
        sessions_db=sessions_db,
        audit_db=audit_db,
        jobs_db=jobs_db,
        database_url=os.environ.get("LAOWADI_DATABASE_URL") or None,
    )

    port_env = os.environ.get("LAOWADI_PORT", "").strip()
    allowed_port = int(port_env) if port_env.isdigit() and int(port_env) > 0 else None
    rate_lim = int(os.environ.get("LAOWADI_RATE_LIMIT_PER_MIN", "120"))

    app = create_app(wb, allowed_port=allowed_port, rate_limit_per_min=rate_lim)

    # Register this Uvicorn API worker PID in the shared cross-process worker registry
    worker_id = f"api-worker-{socket.gethostname()}-{os.getpid()}"
    stop_ev = threading.Event()

    def _api_heartbeat():
        while not stop_ev.is_set():
            try:
                wb.task_broker.record_worker_heartbeat(worker_id, worker_kind="api_worker", status="serving")
            except Exception:
                pass
            stop_ev.wait(5.0)

    wb.task_broker.record_worker_heartbeat(worker_id, worker_kind="api_worker", status="serving")
    threading.Thread(target=_api_heartbeat, daemon=True).start()
    if os.environ.get("LAOWADI_PROACTIVE_WATCHER", "1").strip() not in ("0", "false", "FALSE"):
        wb.proactive_watcher.start()
    configure_logging()
    log_event(
        get_logger("asgi"),
        logging.INFO,
        "api_worker_started",
        f"Uvicorn ASGI worker {worker_id} initialized",
        worker_id=worker_id,
        database_url=wb.db_manager.masked_url(),
        model=model or "local",
    )
    return app


app = build_asgi_app()
