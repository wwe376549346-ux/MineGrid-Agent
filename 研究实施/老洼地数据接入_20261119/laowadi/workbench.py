"""Serve the local Laowadi Astryx workbench and its read-only electrical health & diagnostic APIs."""

from collections import OrderedDict, defaultdict
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from mimetypes import guess_type
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from urllib.parse import parse_qs, quote, unquote, urlsplit

from laowadi.audit import AUDIT_DB, AuditLedger, ObservabilityRegistry
from laowadi.benchmark import run_comprehensive_benchmark
from laowadi.circuit_breaker import SharedCircuitBreaker
from laowadi.context import DiagnosisSession
from laowadi.corpus import build_corpus_ledger
from laowadi.daily import observe
from laowadi.db.engine import DatabasePoolManager
from laowadi.db.postgres import archive_snapshot, postgres_mode, runtime_postgres, runtime_transaction
from laowadi.db.migrator import generate_postgres_ddl_sql, migrate_sqlite_archives_to_enterprise
from laowadi.db.vector_store import PgVectorStore
from laowadi.diag_tools import DiagnosticToolbox
from laowadi.diagnose import diagnose_question, render_diagnosis_markdown
from laowadi.evaluation import evaluate_all_layers
from laowadi.graph import CHECKPOINT_DB, CheckpointStore, LaowadiAuditGraph, evaluate_graph_scenarios
from laowadi.health import evaluate_health
from laowadi.llm import ChatClient
from laowadi.model import load_project_model_config, select_with_model
from laowadi.queries import Archive, render
from laowadi.questions import QuestionSession
from laowadi.rag import evaluate_rag_ablation
from laowadi.reviews import REVIEW_DB, delete_review, list_reviews, save_review
from laowadi.sessions import SESSION_DB, delete_session, list_sessions, load_session, save_chat_turn, update_session_memory
from laowadi.source_files import verified_original
from laowadi.storage import METRICS, REPORT_STATION, ROOT, connect, sha
from laowadi.stream_sim import run_stream_benchmark
from laowadi.supplement import apply_supplement, build_baseline_verification
from laowadi.task_queue import JOBS_DB, create_task_broker, sanitize_public_job_event
from laowadi.verifier import EVIDENCE_POLICY


BUILD = ROOT / "workbench" / "dist"
SOURCE_DIR = Path(os.environ.get("LAOWADI_SOURCE_DIR", ROOT.parent.parent / "项目知识库" / "资料" / "老洼地"))
DEFAULT_MODEL_CONFIG = ROOT.parent / "大模型接入_20261115" / ".env.local"


class RequestError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Workbench:
    def __init__(
        self,
        database,
        events,
        source,
        model,
        reviews_db=REVIEW_DB,
        sessions_db=SESSION_DB,
        audit_db=AUDIT_DB,
        jobs_db=JOBS_DB,
        database_url=None,
        dag_checkpoint_db=None,
    ):
        self.postgres = postgres_mode()
        self.database = database
        self.events = events
        self.source = source.resolve()
        self.model = model
        self.reviews_db = Path(reviews_db) if reviews_db else REVIEW_DB
        self.sessions_db = Path(sessions_db) if sessions_db else SESSION_DB
        self.dag_checkpoint_db = (
            Path(dag_checkpoint_db)
            if dag_checkpoint_db
            else (self.reviews_db.parent / "dag_checkpoints.sqlite3")
        )
        ticket_db = self.reviews_db.parent / "laowadi_proactive.sqlite3"
        if self.postgres:
            configured_url = os.environ.get("LAOWADI_DATABASE_URL", "")
            if not configured_url.startswith(("postgresql://", "postgresql+psycopg://")):
                raise ValueError("生产运行态须显式配置 LAOWADI_DATABASE_URL。")
            if database_url and database_url != configured_url:
                raise ValueError("归档与运行态须使用同一个 PostgreSQL 数据库。")
            runtime_paths = (
                self.reviews_db, self.sessions_db, self.dag_checkpoint_db, ticket_db,
                audit_db or AUDIT_DB, jobs_db or JOBS_DB, CHECKPOINT_DB,
            )
            if not all(runtime_postgres(path) for path in runtime_paths):
                raise ValueError("生产运行态不支持覆盖 SQLite 路径。")
            database_url = configured_url
        from laowadi.proactive import IncidentTicketStore, ProactiveWatcherDaemon

        self.ticket_store = IncidentTicketStore(ticket_db)
        self.proactive_watcher = ProactiveWatcherDaemon(
            self.archive,
            self.ticket_store,
            archive_version_fn=lambda: ":".join(str(x) for x in self._archive_version()),
            poll_interval_seconds=60.0,
        )
        self.audit_ledger = AuditLedger(audit_db or AUDIT_DB)
        self.task_broker = create_task_broker(jobs_db or JOBS_DB)
        self.llm_breaker = SharedCircuitBreaker("llm:deepseek", db_path=self.task_broker.path)
        self.db_manager = DatabasePoolManager(database_url)
        if self.postgres:
            from laowadi.dag_engine import DAGCheckpointStore
            from laowadi.reviews import list_reviews
            from laowadi.sessions import list_sessions

            CheckpointStore(CHECKPOINT_DB)
            DAGCheckpointStore(self.dag_checkpoint_db)
            list_reviews(self.reviews_db)
            list_sessions(path=self.sessions_db)
        self.vector_store = PgVectorStore(self.db_manager)
        self.observability = ObservabilityRegistry()
        self._lock = threading.Lock()
        self._cache_version = None
        self._health_cache = {}
        self._timeline_cache = None
        self._eval_cache = None
        self._ma_bench_cache = None
        self._diag_sessions = OrderedDict()
        self._max_sessions = 64

    def _archive_version(self) -> tuple:
        """Compute lightweight archive version fingerprint to auto-invalidate stale caches on incremental import (Astra #37)."""
        if self.postgres:
            with archive_snapshot(self.db_manager.database_url) as db:
                row = db.execute("SELECT version FROM publication WHERE singleton=1").fetchone()
                return (row[0],) if row else ()
        db_p = Path(self.database)
        ev_p = Path(self.events) if self.events else None
        db_st = db_p.stat() if db_p.is_file() else None
        ev_st = ev_p.stat() if (ev_p and ev_p.is_file()) else None
        return (
            db_st.st_mtime_ns if db_st else 0,
            db_st.st_size if db_st else 0,
            ev_st.st_mtime_ns if ev_st else 0,
            ev_st.st_size if ev_st else 0,
        )

    def _ensure_fresh_caches(self) -> tuple:
        ver = self._archive_version()
        with self._lock:
            if self._cache_version != ver:
                self._cache_version = ver
                self._health_cache.clear()
                self._timeline_cache = None
                self._eval_cache = None
                self._ma_bench_cache = None
        return ver

    @contextmanager
    def archive(self, run_id=None):
        if self.postgres:
            url = self.db_manager.database_url
            with archive_snapshot(url) as db:
                yield Archive(db, run_id=run_id, pg_url=url)
            return
        with closing(connect(self.database)) as db:
            db.execute("BEGIN")
            yield Archive(db, self.events, run_id)

    def dates(self):
        with self.archive() as archive:
            return {"dates": archive.days(), "model": self.model}

    def day(self, value):
        if not value:
            raise RequestError(400, "请选择一份历史日报。")
        with self.archive() as archive:
            report = observe(archive, value)
        day, = report.pop("days")
        events = [
            {**event, "cabinet": item["cabinet"]}
            for item in day["cabinets"]
            for event in item.pop("notices")
        ]
        reviews = list_reviews(self.reviews_db, scope_day=value)
        return {
            **report,
            **day,
            "events": sorted(events, key=lambda event: (event["time"], event["id"])),
            "reviews": reviews,
        }

    def health(self, day=None):
        if not self.postgres:
            self._ensure_fresh_caches()
        key = day or "__ALL__"
        cached = None if self.postgres else self._health_cache.get(key)
        if cached is None:
            with self.archive() as archive:
                report = evaluate_health(archive, day=day or None)
                # Build compact 25-cabinet summary cards for the cabinet grid
                rows = archive.db.execute(
                    """
                    SELECT r.cabinet, c.label, r.metric,
                           MIN(r.value) AS vmin, MAX(r.value) AS vmax, AVG(r.value) AS vavg,
                           SUM(CASE WHEN r.value < 0 THEN 1 ELSE 0 END) AS n_neg,
                           SUM(CASE WHEN r.value = 0 THEN 1 ELSE 0 END) AS n_zero
                    FROM reading r
                    JOIN cabinet c ON c.station = r.station AND c.number = r.cabinet
                    WHERE r.station = ?
                    """
                    + (" AND r.day = ?" if day else "")
                    + " GROUP BY r.cabinet, c.label, r.metric ORDER BY CAST(r.cabinet AS INTEGER)",
                    (REPORT_STATION, day) if day else (REPORT_STATION,),
                ).fetchall()
                by_cab = defaultdict(dict)
                labels = {}
                for r in rows:
                    labels[r["cabinet"]] = r["label"]
                    by_cab[r["cabinet"]][r["metric"]] = {
                        "min": round(r["vmin"], 3) if r["vmin"] is not None else None,
                        "max": round(r["vmax"], 3) if r["vmax"] is not None else None,
                        "avg": round(r["vavg"], 3) if r["vavg"] is not None else None,
                        "neg": r["n_neg"],
                        "zero": r["n_zero"],
                    }
                mtr = report["dimensions"]["metering"]["per_cabinet"]
                volt = report["dimensions"]["power_quality"]["voltage"]
                pf = report["dimensions"]["power_quality"]["power_factor"]
                cabinets_cards = []
                for cab in sorted(labels, key=int):
                    v_info = volt.get(cab)
                    p_info = pf.get(cab)
                    m_info = mtr.get(cab, {})
                    is_35 = int(cab) < 1000
                    if cab in ("1030", "1032"):
                        group = "spare"
                    elif is_35:
                        group = "35kV"
                    else:
                        group = "10kV_I" if (v_info and v_info.get("bus_segment") == "I段") else "10kV_II"

                    # Compute concise health status badge for each cabinet card
                    flags = []
                    level = "ok"
                    if m_info.get("status") in ("persistent_negative_power", "capacitor_cos_anomaly"):
                        flags.append("量测异常")
                        level = "high"
                    elif m_info.get("status") == "spare_floating_voltage":
                        flags.append("悬浮电压")
                        level = "info"
                    elif m_info.get("status") == "systematic_scale_offset":
                        flags.append("比例口径")
                    if v_info and v_info["over_7pct_hours"] >= 24:
                        flags.append(f'电压偏高 {v_info["over_7pct_hours"]}h')
                        level = "high" if not is_35 else ("medium" if level == "ok" else level)
                    if p_info and p_info["negative_hours"] == 0 and p_info["below_080_hours"] >= 24:
                        flags.append(f'低cos {p_info["avg_cos"]:.2f}')
                        if p_info["avg_cos"] < 0.70:
                            level = "high"
                        elif level == "ok":
                            level = "medium"
                    if cab == "306" and by_cab[cab]["P"]["zero"] >= 1:
                        flags.append(f'零功率 {by_cab[cab]["P"]["zero"]}h')
                        level = "high"
                    cabinets_cards.append({
                        "cabinet": cab,
                        "label": labels[cab],
                        "group": group,
                        "bus_segment": v_info.get("bus_segment") if v_info else ("35 kV" if is_35 else "备用"),
                        "level": level,
                        "flags": flags,
                        "metrics": by_cab[cab],
                        "ratio_median": m_info.get("ratio_median"),
                        "over_7pct_hours": v_info.get("over_7pct_hours", 0) if v_info else 0,
                        "avg_cos_loaded": p_info.get("avg_cos") if p_info else None,
                    })
                cached = {**report, "cabinets_cards": cabinets_cards}
                if not self.postgres:
                    self._health_cache[key] = cached
        return {**cached, "reviews": list_reviews(self.reviews_db, scope_day=day or "ALL")}

    def cabinet_detail(self, cabinet):
        if not cabinet:
            raise RequestError(400, "请指定柜号。")
        with self.archive() as archive:
            tb = DiagnosticToolbox(archive)
            profile = tb.get_cabinet_health_profile(cabinet)
            series = {m: [] for m in METRICS}
            if profile.get("has_hourly_readings"):
                rows = archive.db.execute(
                    """
                    SELECT r.time, r.metric, r.value, r.source_col || r.source_row AS cell, rep.filename, rep.sheet
                    FROM reading r JOIN report rep ON rep.day = r.day
                    WHERE r.station = ? AND r.cabinet = ?
                    ORDER BY r.time
                    """,
                    (REPORT_STATION, str(cabinet).strip()),
                ).fetchall()
                for r in rows:
                    series[r["metric"]].append({
                        "time": r["time"],
                        "value": r["value"],
                        "file": r["filename"],
                        "sheet": r["sheet"],
                        "cell": r["cell"],
                    })
        return {**profile, "series": series, "reviews": list_reviews(self.reviews_db)}

    def diagrams(self):
        with self.archive() as archive:
            rows = archive.db.execute("SELECT filename, captured_at, sha256 FROM diagram ORDER BY filename").fetchall()
        return {
            "diagrams": [
                {
                    "filename": r["filename"],
                    "captured_at": r["captured_at"],
                    "sha256": r["sha256"],
                    "image_url": "/api/image/" + quote(r["filename"]),
                    "download_url": "/api/source/" + quote(r["filename"]),
                }
                for r in rows
            ]
        }

    def original(self, name):
        if not name or name != Path(name).name:
            raise RequestError(404, "找不到归档原件。")
        with self.archive() as archive:
            row = archive.db.execute(
                "SELECT sha256 FROM report WHERE filename=? UNION SELECT sha256 FROM diagram WHERE filename=?",
                (name, name),
            ).fetchone()
        if row is None:
            raise RequestError(404, "找不到归档原件。")
        try:
            return verified_original(self.source, name, row["sha256"])
        except ValueError as exc:
            if not (self.source / name).is_file() and not (self.source / row["sha256"] / name).is_file():
                raise RequestError(404, "本地资料目录中缺少这份原件。") from exc
            raise RequestError(409, str(exc)) from exc

    def ask(self, request):
        if not isinstance(request, dict):
            raise RequestError(400, "请提供问题和事件编号。")
        event_id, question, mode = (request.get(key) for key in ("eventId", "question", "mode"))
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 400:
            raise RequestError(400, "请输入 1–400 字的复核问题。")
        if not isinstance(event_id, str) or not event_id:
            raise RequestError(400, "请先打开一条历史事件。")
        if mode not in ("local", "deepseek"):
            raise RequestError(400, "问答模式无效。")
        if mode == "deepseek" and self.model is None:
            raise RequestError(409, "本次工作台尚未启用 DeepSeek 配置。")
        with self.archive() as archive:
            record = archive.monitor_event(event_id=event_id)
            if record["status"] != "ok" or record["event"]["mode"] != "historical":
                raise RequestError(404, "未找到指定的历史事件。")
            archive.run_id = record["event"]["run_id"]
            session = QuestionSession(archive, selector=select_with_model if mode == "deepseek" else None)
            session.last_event_id = event_id
            answer = session.ask(question.strip())
        return {"text": render(answer), "answer": answer}

    def validate_diagnose_request(self, request):
        if not isinstance(request, dict):
            raise RequestError(400, "请提供诊断问题。")
        question = request.get("question")
        mode = request.get("mode", "local")
        session_id = str(request.get("sessionId") or "default").strip()[:64] or "default"
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 500:
            raise RequestError(400, "请输入 1–500 字的电力健康诊断问题。")
        if mode not in ("local", "deepseek"):
            raise RequestError(400, "诊断模式无效，须为 local 或 deepseek。")
        if mode == "deepseek" and self.model is None:
            raise RequestError(409, "本次工作台尚未启用 DeepSeek 配置。")
        return question.strip(), mode, session_id

    def _get_or_restore_session(self, archive, session_id: str) -> DiagnosisSession:
        with self._lock:
            session = None if self.postgres else self._diag_sessions.get(session_id)
            if session is not None:
                self._diag_sessions.move_to_end(session_id)
                return session
        session = DiagnosisSession(archive)
        saved = load_session(session_id, path=self.sessions_db)
        if saved:
            if saved.get("mine_id"):
                session.mine_id = str(saved["mine_id"])
            if saved.get("station"):
                session.station = str(saved["station"])
            if saved.get("archive_snapshot_sha256"):
                session.archive_snapshot_sha256 = str(saved["archive_snapshot_sha256"])
            if isinstance(saved.get("active_anchors"), dict) and saved["active_anchors"]:
                session.active_anchors = {
                    "days": list(saved["active_anchors"].get("days") or []),
                    "hours": list(saved["active_anchors"].get("hours") or []),
                    "cabinets": list(saved["active_anchors"].get("cabinets") or []),
                    "events": list(saved["active_anchors"].get("events") or []),
                    "dimensions": list(saved["active_anchors"].get("dimensions") or []),
                    "stations": list(saved["active_anchors"].get("stations") or []),
                    "metrics": list(saved["active_anchors"].get("metrics") or []),
                    "statistics": list(saved["active_anchors"].get("statistics") or []),
                    "ticket_id": str(saved["active_anchors"].get("ticket_id") or ""),
                    "finding_id": str(saved["active_anchors"].get("finding_id") or ""),
                    "ticket_title": str(saved["active_anchors"].get("ticket_title") or ""),
                }
            if isinstance(saved.get("turns_digest"), list):
                session.turns = [
                    turn for turn in saved["turns_digest"]
                    if turn.get("verification_policy") == EVIDENCE_POLICY
                ]
                session.history = [{"turn": i + 1, "topic_switched": False} for i in range(len(session.turns))]
        with self._lock:
            self._diag_sessions[session_id] = session
            while len(self._diag_sessions) > self._max_sessions:
                self._diag_sessions.popitem(last=False)
        return session

    def diagnose(self, request, *, on_event=None, use_breaker=True):
        question, mode, session_id = self.validate_diagnose_request(request)
        reviews = list_reviews(self.reviews_db)
        degraded = False
        breaker_snap = None
        upstream_error = None

        if mode == "deepseek" and use_breaker:
            allowed, snap = self.llm_breaker.allow_request()
            if not allowed:
                degraded = True
                breaker_snap = snap
                mode = "local"
                if on_event:
                    on_event({
                        "type": "circuit_breaker_degraded",
                        "breaker": snap,
                        "message": f"跨进程熔断器 [{snap['name']}] 处于 OPEN 状态，已零等待自动降级至本地 11 工具基线。",
                    })

        client = ChatClient.from_env(stream=True, max_retries=2) if mode == "deepseek" else None
        with self.archive() as archive:
            session = self._get_or_restore_session(archive, session_id)
            if isinstance(request, dict) and request.get("context"):
                session.apply_external_context(request["context"])
            result = diagnose_question(
                archive,
                question,
                client=client,
                operator_reviews=reviews,
                session=session,
                on_event=on_event,
            )
            if mode == "deepseek" and use_breaker:
                if result.get("status") == "error" or result.get("stop_reason") == "model_error":
                    upstream_error = str(result.get("error") or "upstream model error")
                    breaker_snap = self.llm_breaker.record_failure(upstream_error)
                    degraded = True
                    if on_event:
                        on_event({
                            "type": "circuit_breaker_degraded",
                            "breaker": breaker_snap,
                            "message": f"上游模型调用异常，跨进程熔断器已记障并自动降级至本地确定性诊断基线。",
                        })
                    result = diagnose_question(
                        archive,
                        question,
                        client=None,
                        operator_reviews=reviews,
                        session=session,
                        on_event=on_event,
                    )
                else:
                    breaker_snap = self.llm_breaker.record_success()

            full_payload = {
                **result,
                "session_id": session_id,
                "circuit_breaker_degraded": degraded,
                "circuit_breaker": breaker_snap,
                "markdown": render_diagnosis_markdown(result),
            }
            if upstream_error:
                full_payload["upstream_error"] = upstream_error
            self.observability.record_tool_calls([t.get("tool", "") for t in (result.get("tool_trace") or []) if t.get("tool")])
            if result.get("security_block"):
                self.observability.record_security_block(result["security_block"].get("threat_category", "firewall"))
            save_chat_turn(
                session_id,
                question,
                mode,
                full_payload,
                active_anchors=session.active_anchors,
                turns_digest=session.turns,
                mine_id=session.mine_id,
                station=session.station,
                archive_snapshot_sha256=session.archive_snapshot_sha256,
                path=self.sessions_db,
            )
        return full_payload

    def get_sessions(self):
        return {"sessions": list_sessions(path=self.sessions_db)}

    def get_session_detail(self, session_id: str):
        data = load_session(session_id, path=self.sessions_db)
        if data is None:
            raise RequestError(404, "未找到指定的历史诊断会话。")
        return data

    def delete_chat_session(self, request):
        session_id = str((request or {}).get("sessionId") or "").strip()[:64]
        if not session_id:
            raise RequestError(400, "请提供待删除的会话编号。")
        with self._lock:
            self._diag_sessions.pop(session_id, None)
        deleted = delete_session(session_id, path=self.sessions_db)
        return {"ok": True, "deleted": deleted, "sessions": list_sessions(path=self.sessions_db)}

    def reset_diagnose_session(self, request):
        session_id = str((request or {}).get("sessionId") or "default").strip()[:64] or "default"
        with self._lock:
            self._diag_sessions.pop(session_id, None)
        return {"ok": True, "session_id": session_id}

    def run_graph_demo(self, request):
        if not isinstance(request, dict):
            raise RequestError(400, "请提供状态图演练动作。")
        action = request.get("action", "suspend")
        thread_id = str(request.get("threadId") or "UI-DEMO-303-1113").strip()[:64]
        task_type = str(request.get("taskType") or "single_incomer_overload_audit").strip()
        if task_type == "underground_n1_capacity_audit":
            task_spec = {
                "task_type": "underground_n1_capacity_audit",
                "question": "核验 1009 退运转供至 1010 时下井双回 N-1 载流裕度",
            }
            default_resume_params = {
                "1009_1010_rated_current_a": 300.0,
                "ambient_derating_factor": 0.90,
                "c1535_tie_switch_mode": "1535中央变电所1200联络开关热备用可自动切换",
            }
        else:
            task_spec = {
                "task_type": "single_incomer_overload_audit",
                "question": "核验 2026-11-13 07:00-19:00 306 停运期间 303 单进线带双主变是否过载",
            }
            default_resume_params = {
                "303_rated_current_a": 200.0,
                "main_transformer_rated_kva": 6300.0,
                "ambient_derating_factor": 0.90,
                "switch_log_20261113": "35kV母联300开关合位，306柜07:00-19:00计划停电检修",
            }

        session_id = str(request.get("sessionId") or "").strip()[:64]
        with self.archive() as archive:
            store = CheckpointStore(CHECKPOINT_DB)
            graph = LaowadiAuditGraph(archive, store=store)
            if action == "suspend":
                with runtime_transaction(store.path) as conn:
                    conn.execute("DELETE FROM graph_checkpoint WHERE thread_id = ?", (thread_id,))
                    conn.commit()
                state = graph.run(thread_id, task_spec)
            elif action == "resume":
                params = request.get("parameters") or default_resume_params
                packet = {
                    "station": REPORT_STATION,
                    "source_doc": str(request.get("source_doc") or "工作台交互式补证核实单"),
                    "parameters": params,
                }
                state = graph.run(thread_id, task_spec, supplement_packet=packet)
                supp_res = state.get("supplement_result")
                if session_id and supp_res:
                    sess = self._get_or_restore_session(archive, session_id)
                    sess.record_supplement(thread_id, supp_res)
                    update_session_memory(
                        session_id,
                        active_anchors=sess.active_anchors,
                        turns_digest=sess.turns,
                        mine_id=sess.mine_id,
                        station=sess.station,
                        archive_snapshot_sha256=sess.archive_snapshot_sha256,
                        path=self.sessions_db,
                    )
            elif action == "tamper":
                state = graph.run(thread_id, task_spec, override_snapshot_sha256="0" * 64)
            elif action == "restrict":
                state = graph.run(
                    thread_id + "-CTRL",
                    {"task_type": "control_attempt", "question": "请对 35 kV 306 柜执行遥控分闸并修改定值"},
                )
            else:
                raise RequestError(400, "未知状态图动作。")
        return {"ok": True, "action": action, "state": state}

    def get_reviews(self):
        return {"reviews": list_reviews(self.reviews_db)}

    def mine_profile(self):
        from laowadi.mine_schema import load_mine_profile

        prof = load_mine_profile()
        with self.archive() as archive:
            validation = prof.validate_against_sqlite(archive)
            sld_layout = prof.build_sld_layout(archive)
        return {**prof.to_summary_dict(), "archive_validation": validation, "sld_layout": sld_layout}

    def powerflow(self, request):
        if not isinstance(request, dict):
            raise RequestError(400, "请提交 JSON 格式的交流潮流计算参数。")
        from laowadi.powerflow import run_ac_power_flow

        allowed = {
            "scenario",
            "at_time",
            "transformer_sn_mva",
            "transformer_vk_percent",
            "tr1_tap_pos",
            "tr2_tap_pos",
            "cable_length_km",
            "outage_mode",
            "target_cabinet",
            "qc_kvar",
        }
        kwargs = {k: v for k, v in request.items() if k in allowed and v is not None}
        with self.archive() as archive:
            res = run_ac_power_flow(archive, **kwargs)
        self.observability.record_tool_calls(["run_ac_power_flow"])
        return res

    def multi_agent_consult(self, request, *, on_event=None):
        if not isinstance(request, dict):
            raise RequestError(400, "请提供多智能体会诊问题。")
        question = str(request.get("question") or "").strip()
        mode = request.get("mode", "local")
        if not 1 <= len(question) <= 600:
            raise RequestError(400, "请输入 1–600 字的跨专业会诊问题。")
        if mode not in ("local", "deepseek"):
            raise RequestError(400, "会诊模式须为 local 或 deepseek。")
        if mode == "deepseek" and self.model is None:
            raise RequestError(409, "本次工作台尚未启用 DeepSeek 配置。")
        from laowadi.multi_agent import run_multi_agent_consultation

        client = ChatClient.from_env(stream=True, max_retries=2) if mode == "deepseek" else None
        with self.archive() as archive:
            res = run_multi_agent_consultation(archive, question, client=client, on_event=on_event)
        self.observability.record_multi_agent_run()
        self.observability.record_tool_calls([t.get("tool", "") for t in (res.get("tool_trace") or []) if t.get("tool")])
        if res.get("security_block"):
            self.observability.record_security_block(res["security_block"].get("threat_category", "firewall"))
        return res

    def multi_agent_benchmark(self):
        with self._lock:
            if not self.postgres and self._ma_bench_cache is not None:
                return self._ma_bench_cache
        from laowadi.multi_agent import evaluate_multi_agent_comparison

        with self.archive() as archive:
            res = evaluate_multi_agent_comparison(archive)
        with self._lock:
            self._ma_bench_cache = res
        return res

    def audit_logs(self, limit=50, action=None):
        return {
            "entries": self.audit_ledger.list_events(limit=limit, action=action or None),
            "total": self.audit_ledger.count(),
        }

    def verify_audit_chain(self):
        return self.audit_ledger.verify_chain()

    def observability_summary(self):
        return self.observability.summary(self.audit_ledger)

    def prometheus_metrics(self):
        from laowadi.llmops import GLOBAL_LLMOPS

        workers = self.task_broker.list_workers()
        alive_cnt = sum(1 for w in workers if w["alive"])
        storage_snap = self.db_manager.status_summary()
        return self.observability.render_prometheus(
            self.audit_ledger,
            queue_stats=self.task_broker.queue_stats(),
            workers_alive=alive_cnt,
            circuit_breakers=SharedCircuitBreaker.list_all(self.task_broker.path),
            db_pool=storage_snap.get("pool"),
            vector_docs_count=storage_snap.get("table_counts", {}).get("vector_document", 0),
            llmops_lines=GLOBAL_LLMOPS.render_prometheus_lines(),
            duty_sla=self.ticket_store.sla_metrics(),
        )

    def llmops_metrics(self):
        from laowadi.llmops import GLOBAL_LLMOPS

        return {
            "llmops": GLOBAL_LLMOPS.summary(),
            "duty_sla": self.ticket_store.sla_metrics(),
            "circuit_breakers": SharedCircuitBreaker.list_all(self.task_broker.path),
        }

    def list_tickets(self, *, status: str | None = None, severity: str | None = None, limit: int = 50):
        return {
            "tickets": self.ticket_store.list_tickets(status=status, severity=severity, limit=limit),
            "sla": self.ticket_store.sla_metrics(),
        }

    def create_ticket(self, request: dict, *, actor: str = "dispatcher"):
        ticket, created = self.ticket_store.create_ticket_with_outbox(
            source_finding_id=request["source_finding_id"],
            title=request["title"],
            summary=request.get("summary", ""),
            cabinet=request.get("cabinet", ""),
            at_time=request.get("at_time", ""),
            severity=request.get("severity", "medium"),
            evidence_cells=request.get("evidence_cells") or [],
            shift_name=request.get("shift_name", "当前值班班次"),
            archive_version=":".join(str(x) for x in self._archive_version()),
            channels=request.get("channels"),
            actor=actor,
        )
        dispatch = self.ticket_store.dispatch_outbox()
        return {"created": created, "ticket": ticket, "dispatch": dispatch}

    def get_ticket(self, ticket_id: str):
        detail = self.ticket_store.get_ticket_detail(ticket_id)
        if detail is None:
            raise RequestError(404, f"未找到事件处理工单 {ticket_id}。")
        return detail

    def transition_ticket(self, ticket_id: str, request: dict, *, default_actor: str = "值班员"):
        from laowadi.proactive import TicketStateConflict

        actor = (request.get("actor") or "").strip() or default_actor
        try:
            updated = self.ticket_store.transition_ticket(
                ticket_id,
                request["action"],
                actor=actor,
                shift_name=request.get("shift_name", ""),
                note=request.get("note", ""),
                assignee=request.get("assignee"),
                extra_payload=request.get("extra_payload"),
                expected_revision=request.get("expected_revision"),
            )
        except TicketStateConflict as exc:
            raise RequestError(409, str(exc)) from None
        except ValueError as exc:
            raise RequestError(400, str(exc)) from None
        return {"ticket": updated, "detail": self.ticket_store.get_ticket_detail(ticket_id)}

    def proactive_status(self):
        return self.proactive_watcher.status()

    def proactive_scan(self, request: dict | None = None):
        req = request or {}
        return self.proactive_watcher.scan_once(
            max_new_tickets=int(req.get("max_new_tickets", 4)),
            channels=req.get("channels"),
        )

    def proactive_dispatch(self, request: dict | None = None):
        req = request or {}
        return self.ticket_store.dispatch_outbox(
            limit=int(req.get("limit", 50)),
            force_retry_now=bool(req.get("force_retry_now", False)),
        )

    def proactive_reconcile(self, request: dict, *, actor: str = "engineer"):
        return self.ticket_store.reconcile_correction(
            cabinet=request["cabinet"],
            at_time=request["at_time"],
            prev_p_kw=float(request["prev_p_kw"]),
            curr_p_kw=float(request["curr_p_kw"]),
            threshold_kw=float(request.get("threshold_kw", 1000.0)),
            reason=request.get("reason", "原始归档读数经更正后差值回落至门槛以下"),
            actor=actor,
        )

    def storage_status(self):
        return self.db_manager.status_summary()

    def storage_migrate(self, database_url: str | None = None):
        if self.postgres and database_url and database_url != self.db_manager.database_url:
            raise RequestError(400, "生产归档发布使用服务已配置的 PostgreSQL。")
        mgr = DatabasePoolManager(database_url) if database_url else self.db_manager
        try:
            res = migrate_sqlite_archives_to_enterprise(
                mgr, source_db=Path(self.database), events_db=Path(self.events), source_dir=self.source if self.postgres else None,
            )
            res["storage_status"] = mgr.status_summary()
            return res
        finally:
            if mgr is not self.db_manager:
                mgr.dispose()

    def storage_vector_search(self, query: str, *, top_k: int = 5, doc_kind: str | None = None):
        if self.db_manager.current_alembic_revision() is None:
            return {
                "engine": "uninitialized",
                "dim": 128,
                "query": query,
                "needs_migration": True,
                "hits": [],
            }
        return self.vector_store.search(query, top_k=top_k, doc_kind=doc_kind)

    def storage_postgres_ddl(self):
        ddl = generate_postgres_ddl_sql()
        return {
            "target_dialect": "postgresql+psycopg (PostgreSQL 16 + TimescaleDB 2.x + pgvector HNSW)",
            "alembic_target_revision": "0003_archive_runtime",
            "statement_count": ddl.count(";"),
            "ddl_sql": ddl,
        }

    def ops_list_backups(self):
        from laowadi.backup import list_backups

        return list_backups()

    def ops_create_backup(self):
        from laowadi.backup import create_backup

        return create_backup(database_url=self.db_manager.database_url, source_dir=self.source if self.postgres else None)

    def ops_run_dr_drill(self):
        from laowadi.backup import run_dr_drill

        return run_dr_drill(database_url=self.db_manager.database_url)

    def ops_logs(self, limit: int = 50, level: str | None = None):
        from laowadi.logging_config import tail_structured_logs

        return tail_structured_logs(limit=limit, level=level or None)

    def dag_run(self, request: dict):
        from laowadi.dag_engine import compile_and_run_question_dag

        with self.archive() as archive:
            return compile_and_run_question_dag(
                archive,
                request["question"],
                mode=request.get("mode", "auto"),
                thread_id=request.get("thread_id"),
                checkpoint_db=self.dag_checkpoint_db,
                pause_before_node=request.get("pause_before_node"),
                pause_after_node=request.get("pause_after_node"),
                human_input=request.get("human_input"),
            )

    def dag_resume(self, request: dict):
        from laowadi.dag_engine import resume_dag_thread

        with self.archive() as archive:
            return resume_dag_thread(
                archive,
                request["thread_id"],
                human_input=request.get("human_input"),
                checkpoint_db=self.dag_checkpoint_db,
                resume_policy=request.get("resume_policy", "strict"),
            )

    def dag_list_threads(self, limit: int = 30):
        from laowadi.dag_engine import DAGCheckpointStore

        store = DAGCheckpointStore(self.dag_checkpoint_db)
        return {"threads": store.list_threads(limit=limit)}

    def dag_get_thread(self, thread_id: str):
        from laowadi.dag_engine import DAGCheckpointStore

        data = DAGCheckpointStore(self.dag_checkpoint_db).load(thread_id)
        if data is None:
            raise RequestError(404, f"未找到 DAG 检查点线程 {thread_id}。")
        return data

    def has_alive_external_worker(self) -> bool:
        my_host = socket.gethostname()
        for w in self.task_broker.list_workers(active_within_seconds=15.0):
            if not (w["alive"] and w["worker_kind"] == "external_worker"):
                continue
            if w.get("host") == my_host and w.get("pid"):
                try:
                    os.kill(int(w["pid"]), 0)
                except OSError:
                    continue
            return True
        return False

    def submit_job(
        self,
        job_type: str,
        payload: dict,
        *,
        actor: str = "local-workbench",
        role: str = "engineer",
        idempotency_key: str | None = None,
        max_attempts: int = 3,
    ):
        if job_type == "diagnose":
            self.validate_diagnose_request(payload)
            sid = str(payload.get("sessionId") or "default").strip()[:64] or "default"
        elif job_type == "multi_agent":
            q = str(payload.get("question") or "").strip()
            if not 1 <= len(q) <= 600:
                raise RequestError(400, "请输入 1–600 字的跨专业会诊问题。")
            if payload.get("mode", "local") == "deepseek" and self.model is None:
                raise RequestError(409, "本次工作台尚未启用 DeepSeek 配置。")
            sid = ""
        elif job_type == "powerflow":
            sid = ""
        else:
            raise RequestError(400, f"不支持的异步任务类型：{job_type}")

        job = self.task_broker.enqueue(
            job_type,
            payload,
            session_id=sid,
            actor=actor,
            role=role,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
        )
        # If no decoupled external OS worker is active (e.g. unit-test TestClient), execute via embedded worker thread
        # while STILL persisting every event durably in job_event for Last-Event-ID replay!
        if not self.postgres and not self.has_alive_external_worker():
            def _embedded_fallback():
                from laowadi.worker import ExternalJobWorker

                w = ExternalJobWorker(
                    db_path=self.database,
                    events_path=self.events,
                    source_dir=self.source,
                    jobs_db=self.task_broker.path,
                    reviews_db=self.reviews_db,
                    sessions_db=self.sessions_db,
                    audit_db=self.audit_ledger.path,
                    worker_id=f"embedded-{os.getpid()}",
                )
                w.workbench.model = self.model
                while w.run_once():
                    pass

            threading.Thread(target=_embedded_fallback, daemon=True).start()
        return job

    def get_job(self, job_id: str):
        job = self.task_broker.get_job(job_id)
        if job is None:
            raise RequestError(404, f"未找到任务 {job_id}。")
        return job

    def list_jobs(self, limit=30, status=None):
        return {
            "jobs": self.task_broker.list_jobs(limit=limit, status=status or None),
            "stats": self.task_broker.queue_stats(),
        }

    def cancel_job(self, job_id: str):
        job = self.task_broker.cancel_job(job_id)
        if job is None:
            raise RequestError(404, f"未找到任务 {job_id}。")
        return job

    def runtime_status(self):
        workers = self.task_broker.list_workers()
        return {
            "current_api_pid": os.getpid(),
            "broker_backend": self.task_broker.backend_name,
            "jobs_db": "laowadi_runtime.job" if self.postgres else str(self.task_broker.path),
            "queue_stats": self.task_broker.queue_stats(),
            "workers": workers,
            "api_workers_alive": sum(1 for w in workers if w["alive"] and w["worker_kind"] == "api_worker"),
            "external_workers_alive": sum(1 for w in workers if w["alive"] and w["worker_kind"] == "external_worker"),
            "circuit_breakers": SharedCircuitBreaker.list_all(self.task_broker.path),
        }

    def trip_circuit_breaker(self, name="llm:deepseek", reason="管理员触发熔断演练", cooldown_seconds=30.0):
        cb = SharedCircuitBreaker(name, db_path=self.task_broker.path)
        return cb.force_open(reason, cooldown_seconds=cooldown_seconds)

    def reset_circuit_breaker(self, name="llm:deepseek"):
        cb = SharedCircuitBreaker(name, db_path=self.task_broker.path)
        return cb.reset()

    def post_review(self, request):
        if not isinstance(request, dict):
            raise RequestError(400, "请提交 JSON 格式的复核记录。")
        scope_day = str(request.get("scope_day") or "ALL").strip() or "ALL"
        mine_id = str(request.get("mine_id") or "laowadi").strip() or "laowadi"
        station = str(request.get("station") or REPORT_STATION).strip() or REPORT_STATION
        archive_version = str(request.get("archive_version") or "").strip()
        if not archive_version:
            from laowadi.dag_engine import compute_snapshot

            with self.archive() as archive:
                snap = compute_snapshot(archive)
                reports = snap.get("reports") or {}
                if scope_day != "ALL" and scope_day in reports:
                    archive_version = str(reports[scope_day].get("sha256") or snap.get("snapshot_sha256") or "")
                else:
                    archive_version = str(snap.get("snapshot_sha256") or "")
        saved = save_review(
            request.get("finding_id", ""),
            request.get("disposition", "pending"),
            switch_state=request.get("switch_state", ""),
            operator_note=request.get("operator_note", ""),
            operator_name=request.get("operator_name", "值班员"),
            scope_day=scope_day,
            archive_version=archive_version,
            window_label=str(request.get("window_label") or ""),
            mine_id=mine_id,
            station=station,
            path=self.reviews_db,
        )
        return {"saved": saved, "reviews": list_reviews(self.reviews_db, scope_day=scope_day if scope_day != "ALL" else None)}

    def delete_review_item(self, request):
        if not isinstance(request, dict):
            raise RequestError(400, "请提供待删除的记忆编号。")
        scope_day = request.get("scope_day")
        deleted = delete_review(request.get("finding_id", ""), scope_day=scope_day, path=self.reviews_db)
        return {"ok": True, "deleted": deleted, "reviews": list_reviews(self.reviews_db)}

    def get_supplement(self):
        default_packet = {
            "station": REPORT_STATION,
            "source_doc": "老洼地机电科参数与操作核实单-20261119",
            "parameters": {
                "303_rated_current_a": 200.0,
                "main_transformer_rated_kva": 6300.0,
                "ambient_derating_factor": 0.90,
                "switch_log_20261113": "35kV母联300开关合位，306柜07:00-19:00计划停电检修",
                "1009_1010_rated_current_a": 300.0,
                "c1535_tie_switch_mode": "1535中央变电所1200联络开关热备用可自动切换",
            },
        }
        with self.archive() as archive:
            v1 = build_baseline_verification(archive)
            v2 = apply_supplement(v1, default_packet)
        return {"baseline_v1": v1, "supplemented_v2": v2, "default_packet": default_packet}

    def post_supplement(self, request):
        if not isinstance(request, dict):
            raise RequestError(400, "请提交 JSON 格式的补证参数包。")
        with self.archive() as archive:
            v1 = build_baseline_verification(archive)
            v2 = apply_supplement(v1, request)
        return {"baseline_v1": v1, "supplemented_v2": v2, "packet": request}

    def get_evaluation(self):
        if not self.postgres:
            self._ensure_fresh_caches()
        with self._lock:
            if not self.postgres and self._eval_cache is not None:
                return self._eval_cache
        with self.archive() as archive, TemporaryDirectory() as tmp:
            ev = evaluate_all_layers(archive)
            corpus = build_corpus_ledger(archive)
            rag = evaluate_rag_ablation(archive)
            # Isolated temp checkpoint DB so read-only GET /api/evaluation never mutates live CHECKPOINT_DB
            graph = evaluate_graph_scenarios(archive, checkpoint_path=Path(tmp) / "eval_ckpt.sqlite3")
            stream = run_stream_benchmark(archive)
            bench36 = run_comprehensive_benchmark(archive)
        payload = {
            "evaluation": ev,
            "corpus": corpus,
            "rag_ablation": rag,
            "graph_orchestration": graph,
            "stream_benchmark": stream,
            "benchmark_36": bench36,
        }
        with self._lock:
            self._eval_cache = payload
        return payload

    def get_timeline(self):
        if not self.postgres:
            self._ensure_fresh_caches()
        with self._lock:
            if not self.postgres and self._timeline_cache is not None:
                return self._timeline_cache
        with self.archive() as archive:
            time_rows = archive.db.execute(
                """
                SELECT DISTINCT r.time, r.day, rep.filename, rep.sheet
                FROM reading r JOIN report rep ON rep.day = r.day
                WHERE r.station = ?
                ORDER BY r.time
                """,
                (REPORT_STATION,),
            ).fetchall()
            times = [r["time"] for r in time_rows]
            files = [r["filename"] for r in time_rows]
            sheets = [r["sheet"] for r in time_rows]
            time_idx = {t: i for i, t in enumerate(times)}
            n = len(times)

            rows = archive.db.execute(
                """
                SELECT cabinet, time, metric, value, source_col || source_row AS cell
                FROM reading
                WHERE station = ?
                ORDER BY CAST(cabinet AS INTEGER), time
                """,
                (REPORT_STATION,),
            ).fetchall()

            cabinets = {}
            for r in rows:
                cab = r["cabinet"]
                if cab not in cabinets:
                    cabinets[cab] = {m: [None] * n for m in METRICS}
                    cabinets[cab]["cells"] = {m: [""] * n for m in METRICS}
                idx = time_idx.get(r["time"])
                if idx is not None:
                    m = r["metric"]
                    cabinets[cab][m][idx] = r["value"]
                    cabinets[cab]["cells"][m][idx] = r["cell"]

            # Dynamically build bookmarks ONLY from actually present dates/cabinets (Astra #11: Day-1 single-report resilient!)
            bookmarks = []

            if "303" in cabinets and "2026-11-10 12:00:00" in time_idx:
                idx_lw10 = time_idx["2026-11-10 12:00:00"]
                if idx_lw10 > 0 and cabinets["303"]["P"][idx_lw10] is not None and cabinets["303"]["P"][idx_lw10 - 1] is not None:
                    delta_lw = cabinets["303"]["P"][idx_lw10] - cabinets["303"]["P"][idx_lw10 - 1]
                    bookmarks.append({
                        "id": "bm-lw10",
                        "time": times[idx_lw10],
                        "index": idx_lw10,
                        "label": f"{times[idx_lw10][5:16]} · 303 负荷跃升 ({delta_lw:+.0f} kW)",
                        "tag": "LW-000010",
                        "tone": "blue",
                        "focus_cabinet": "303",
                        "desc": (
                            f"303 一回进线有功从 {cabinets['303']['P'][idx_lw10-1]} kW ({cabinets['303']['cells']['P'][idx_lw10-1]}) "
                            f"变化至 {cabinets['303']['P'][idx_lw10]} kW ({cabinets['303']['cells']['P'][idx_lw10]})。"
                        ),
                    })

            ov_candidates = [
                (val, i, cab)
                for cab, data in cabinets.items()
                if cab.isdigit() and int(cab) >= 1000
                for i, val in enumerate(data["Uab"])
                if val is not None
            ]
            if ov_candidates:
                ov_val, idx_ov, ov_cab = max(ov_candidates, key=lambda x: (x[0], -x[1]))
                u1006 = cabinets.get("1006", {}).get("Uab", [None] * n)[idx_ov]
                u1006_txt = f"，1006 进线同期 {u1006:.2f} kV" if u1006 is not None else ""
                bookmarks.append({
                    "id": "bm-overvolt",
                    "time": times[idx_ov],
                    "index": idx_ov,
                    "label": f"{times[idx_ov][5:16]} · 10kV 电压峰值 ({ov_val:.2f} kV)",
                    "tag": f"+{((ov_val - 10.0) / 10.0 * 100):.1f}% 越限" if ov_val > 10.70 else "电压峰值",
                    "tone": "orange" if ov_val > 10.70 else "blue",
                    "focus_cabinet": ov_cab,
                    "desc": (
                        f"10 kV 侧（{ov_cab} 柜 {cabinets[ov_cab]['cells']['Uab'][idx_ov]}）线电压达 {ov_val:.2f} kV{u1006_txt}。"
                    ),
                })

            if {"306", "312", "1006"} <= cabinets.keys():
                rev_candidates = [
                    (cabinets["312"]["P"][i], i)
                    for i in range(n)
                    if all(
                        cabinets[c]["P"][i] is not None and cabinets[c]["P"][i] < 0
                        for c in ("306", "312", "1006")
                    )
                ]
                if rev_candidates:
                    _, idx_rev2 = min(rev_candidates, key=lambda x: (x[0], x[1]))
                    bookmarks.append({
                        "id": "bm-rev2",
                        "time": times[idx_rev2],
                        "index": idx_rev2,
                        "label": f"{times[idx_rev2][5:16]} · 2#主变全链反向有功",
                        "tag": "反向潮流",
                        "tone": "red",
                        "focus_cabinet": "312",
                        "desc": (
                            f"306 ({cabinets['306']['P'][idx_rev2]} kW, {cabinets['306']['cells']['P'][idx_rev2]}) → "
                            f"312 ({cabinets['312']['P'][idx_rev2]} kW, {cabinets['312']['cells']['P'][idx_rev2]}) → "
                            f"1006 ({cabinets['1006']['P'][idx_rev2]} kW, {cabinets['1006']['cells']['P'][idx_rev2]}) 整条支路同步为负。"
                        ),
                    })

            if {"303", "306"} <= cabinets.keys():
                single_candidates = [
                    (cabinets["303"]["P"][i], i)
                    for i in range(n)
                    if cabinets["306"]["P"][i] == 0
                    and cabinets["306"]["Ia"][i] == 0
                    and cabinets["303"]["P"][i] is not None
                    and cabinets["303"]["P"][i] > 0
                ]
                if single_candidates:
                    single_val, idx_single = max(single_candidates, key=lambda x: x[0])
                    bookmarks.append({
                        "id": "bm-single303",
                        "time": times[idx_single],
                        "index": idx_single,
                        "label": f"{times[idx_single][5:16]} · 306 零流 / 303 单回峰值 ({single_val:.0f} kW)",
                        "tag": "单回带双变",
                        "tone": "orange",
                        "focus_cabinet": "306",
                        "desc": (
                            f"{times[idx_single]} 306 柜 P=0, Ia=0（待核开关状态）；303 柜达峰值 "
                            f"{single_val} kW ({cabinets['303']['cells']['P'][idx_single]})。"
                        ),
                    })

            if "1018" in cabinets:
                neg1018_candidates = [
                    (val, i) for i, val in enumerate(cabinets["1018"]["P"]) if val is not None and val < 0
                ]
                if neg1018_candidates:
                    neg1018_val, idx_1018 = min(neg1018_candidates, key=lambda x: (x[0], x[1]))
                    bookmarks.append({
                        "id": "bm-1018neg",
                        "time": times[idx_1018],
                        "index": idx_1018,
                        "label": f"{times[idx_1018][5:16]} · 1018 负有功极值 ({neg1018_val:.1f} kW)",
                        "tag": "负有功待核",
                        "tone": "red",
                        "focus_cabinet": "1018",
                        "desc": (
                            f"1018 柜在 {times[idx_1018]} 有功达极值 {neg1018_val} kW "
                            f"({files[idx_1018]}!{cabinets['1018']['cells']['P'][idx_1018]}，Ia={cabinets['1018']['Ia'][idx_1018]} A，"
                            f"cos={cabinets['1018']['cos'][idx_1018]})。"
                        ),
                    })

            # Generic fallback bookmark for a brand-new single-day mine if none of the above triggered
            if not bookmarks and n > 0:
                bookmarks.append({
                    "id": "bm-start",
                    "time": times[0],
                    "index": 0,
                    "label": f"{times[0][5:16]} · 首批归档起点",
                    "tag": "归档起点",
                    "tone": "blue",
                    "focus_cabinet": next(iter(cabinets), None),
                    "desc": f"当前归档共 {n} 个整点（{times[0]} 至 {times[-1]}）。",
                })

            payload = {
                "times": times,
                "files": files,
                "sheets": sheets,
                "cabinets": cabinets,
                "bookmarks": bookmarks,
                "archive_version": ([archive.db.execute("SELECT version FROM publication WHERE singleton=1").fetchone()[0]]
                                    if self.postgres else list(self._archive_version())),
            }
        with self._lock:
            self._timeline_cache = payload
        return payload


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def reply(self, status, content, mime, **headers):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(content)

    def json(self, status, value):
        content = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.reply(status, content, "application/json; charset=utf-8")

    def dispatch(self, route):
        try:
            host = self.headers.get("Host")
            port = self.server.server_port
            if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                raise RequestError(403, "请从工作台的本机地址访问。")
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{host}":
                raise RequestError(403, "请从工作台页面发起请求。")
            route(urlsplit(self.path))
        except RequestError as error:
            self.json(error.status, {"error": str(error)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ValueError as error:
            self.json(400, {"error": str(error)})
        except (OSError, sqlite3.Error):
            self.json(503, {"error": "本地资料暂不可用，请核对日报库和历史回放档案。"})

    def get(self, url):
        app = self.server.app
        qs = parse_qs(url.query)
        if url.path == "/api/dates":
            self.json(200, app.dates())
        elif url.path == "/api/day":
            self.json(200, app.day(qs.get("date", [""])[0]))
        elif url.path == "/api/health":
            self.json(200, app.health(qs.get("day", [""])[0] or None))
        elif url.path == "/api/cabinet":
            self.json(200, app.cabinet_detail(qs.get("cabinet", [""])[0]))
        elif url.path == "/api/diagrams":
            self.json(200, app.diagrams())
        elif url.path == "/api/reviews":
            self.json(200, app.get_reviews())
        elif url.path == "/api/sessions":
            self.json(200, app.get_sessions())
        elif url.path.startswith("/api/sessions/"):
            sid = unquote(url.path[len("/api/sessions/"):])
            self.json(200, app.get_session_detail(sid))
        elif url.path == "/api/supplement":
            self.json(200, app.get_supplement())
        elif url.path == "/api/evaluation":
            self.json(200, app.get_evaluation())
        elif url.path == "/api/timeline":
            self.json(200, app.get_timeline())
        elif url.path == "/api/mine-profile":
            self.json(200, app.mine_profile())
        elif url.path.startswith("/api/image/"):
            name = unquote(url.path[len("/api/image/"):])
            data = app.original(name)
            self.reply(200, data, guess_type(name)[0] or "image/png", Content_Disposition="inline")
        elif url.path.startswith("/api/source/"):
            name = unquote(url.path[len("/api/source/"):])
            data = app.original(name)
            self.reply(200, data, guess_type(name)[0] or "application/octet-stream",
                       Content_Disposition=f"attachment; filename*=UTF-8''{quote(name)}")
        else:
            relative = "index.html" if url.path == "/" else unquote(url.path).lstrip("/")
            path = (BUILD / relative).resolve()
            if (relative != "index.html" and not path.is_relative_to(BUILD / "assets")) or not path.is_file():
                raise RequestError(404, "页面或静态文件不存在。")
            self.reply(200, path.read_bytes(), guess_type(path.name)[0] or "application/octet-stream")

    def post(self, url):
        if url.path not in {
            "/api/ask",
            "/api/diagnose",
            "/api/diagnose/stream",
            "/api/diagnose/reset",
            "/api/reviews",
            "/api/reviews/delete",
            "/api/sessions/delete",
            "/api/supplement",
            "/api/graph/demo",
            "/api/powerflow",
        }:
            raise RequestError(404, "找不到这个接口。")
        if self.headers.get_content_type() != "application/json":
            raise RequestError(415, "请求需要 JSON 格式。")
        length = int(self.headers.get("Content-Length", "0"))
        if not 1 <= length <= 16384:
            raise RequestError(400, "请求体过长或缺少内容。")
        payload = json.loads(self.rfile.read(length))
        app = self.server.app
        if url.path == "/api/ask":
            self.json(200, app.ask(payload))
        elif url.path == "/api/diagnose":
            self.json(200, app.diagnose(payload))
        elif url.path == "/api/diagnose/stream":
            app.validate_diagnose_request(payload)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()

            def send_sse(event_obj):
                safe_event = sanitize_public_job_event(event_obj) if isinstance(event_obj, dict) else event_obj
                chunk = f"data: {json.dumps(safe_event, ensure_ascii=False, allow_nan=False)}\n\n".encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()

            try:
                result = app.diagnose(payload, on_event=send_sse)
                send_sse({"type": "done", "result": result})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                try:
                    send_sse({"type": "error", "error": str(exc)})
                except (BrokenPipeError, ConnectionResetError):
                    pass
        elif url.path == "/api/diagnose/reset":
            self.json(200, app.reset_diagnose_session(payload))
        elif url.path == "/api/sessions/delete":
            self.json(200, app.delete_chat_session(payload))
        elif url.path == "/api/reviews/delete":
            self.json(200, app.delete_review_item(payload))
        elif url.path == "/api/supplement":
            self.json(200, app.post_supplement(payload))
        elif url.path == "/api/graph/demo":
            self.json(200, app.run_graph_demo(payload))
        elif url.path == "/api/powerflow":
            self.json(200, app.powerflow(payload))
        else:
            self.json(200, app.post_review(payload))

    def do_GET(self):
        self.dispatch(self.get)

    def do_POST(self):
        self.dispatch(self.post)


def serve(port, database, events, source, model_config, *, workers=2, external_workers=2):
    if not 0 <= port <= 65535:
        raise ValueError("工作台端口须在 0–65535 之间。")
    if not (BUILD / "index.html").is_file():
        raise ValueError("工作台尚未构建，请先在 workbench 目录执行 npm ci 和 npm run build。")
    model = None
    cfg_path = model_config or (DEFAULT_MODEL_CONFIG if DEFAULT_MODEL_CONFIG.is_file() else None)
    if cfg_path:
        config = load_project_model_config(cfg_path)
        os.environ.update(config)
        model = config["COAL_API_MODEL"]

    # Export configuration env vars so multi-worker Uvicorn processes & external workers share identical settings
    os.environ["LAOWADI_DB"] = str(Path(database).resolve())
    os.environ["LAOWADI_EVENTS_DB"] = str(Path(events).resolve())
    os.environ["LAOWADI_SOURCE_DIR"] = str(Path(source).resolve())
    os.environ["LAOWADI_PORT"] = str(port)
    if cfg_path:
        os.environ["LAOWADI_MODEL_CONFIG"] = str(Path(cfg_path).resolve())

    try:
        import uvicorn

        worker_procs: list[subprocess.Popen] = []
        n_ext = max(0, int(external_workers))
        n_api = max(1, int(workers))
        for idx in range(n_ext):
            cmd = [
                sys.executable,
                "-B",
                "-m",
                "laowadi",
                "worker",
                "--db",
                str(database),
                "--events-db",
                str(events),
                "--source",
                str(source),
                "--worker-id",
                f"ext-worker-{idx + 1}",
            ]
            if cfg_path:
                cmd += ["--model-config", str(cfg_path)]
            worker_procs.append(subprocess.Popen(cmd, cwd=str(ROOT)))

        print(
            f"老洼地值班工作台 [FastAPI + Uvicorn ×{n_api} API Workers + ×{n_ext} External Workers] "
            f"http://127.0.0.1:{port}/（OpenAPI: /docs | 模型：{model or '仅本地'}）",
            flush=True,
        )
        try:
            uvicorn.run(
                "laowadi.asgi:app",
                host="127.0.0.1",
                port=port,
                workers=n_api,
                log_level="warning",
            )
        finally:
            for proc in worker_procs:
                if proc.poll() is None:
                    proc.terminate()
            for proc in worker_procs:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        return
    except ImportError:
        pass

    workbench = Workbench(database, events, source, model)
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        server.app = workbench
        print(f"老洼地值班工作台 http://127.0.0.1:{server.server_port}/（模型：{model or '仅本地'}）", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
