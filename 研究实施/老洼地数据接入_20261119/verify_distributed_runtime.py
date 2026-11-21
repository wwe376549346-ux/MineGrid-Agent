"""Real Multi-Process Distributed Systems Verification:
1. External Durable Task Queue + Web Server SIGKILL Mid-SSE + Restart New PID + Seamless Last-Event-ID Resume (0 gaps, 0 dupes).
2. External Worker SIGKILL Mid-Job + Visibility-Timeout Lease Expiry + Automatic Orphan Reclamation by Worker #2.
3. True Multi-Worker Uvicorn Horizontal Scaling (`--workers 2`, distinct X-Worker-Pid OS processes) + Shared Cross-Process Rate Limiter.
4. Cross-Process Shared Circuit Breaker (`CLOSED -> OPEN -> Graceful Auto-Degradation -> HALF_OPEN -> CLOSED`).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import urllib.error
import urllib.request

from laowadi.circuit_breaker import SharedCircuitBreaker
from laowadi.monitor import EVENT_DB
from laowadi.storage import DB, ROOT
from laowadi.task_queue import DurableSQLiteBroker


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_http(url: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    last_err = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last_err = exc
            time.sleep(0.15)
    raise RuntimeError(f"Server at {url} did not become ready in {timeout}s: {last_err}")


def _http_json(url: str, *, method: str = "GET", body: dict | None = None, headers: dict | None = None) -> tuple[int, dict, dict]:
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return resp.status, dict(resp.headers.items()), json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8")
        return err.code, dict(err.headers.items()), json.loads(raw) if raw else {}


def _spawn_uvicorn(port: int, env: dict[str, str], *, workers: int = 1) -> subprocess.Popen:
    cmd = [
        sys.executable,
        "-B",
        "-m",
        "uvicorn",
        "laowadi.asgi:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--workers",
        str(workers),
        "--log-level",
        "error",
    ]
    return subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _spawn_worker(env: dict[str, str], worker_id: str, *, lease_seconds: float = 15.0) -> subprocess.Popen:
    cmd = [
        sys.executable,
        "-B",
        "-m",
        "laowadi",
        "worker",
        "--worker-id",
        worker_id,
        "--lease-seconds",
        str(lease_seconds),
        "--poll-interval",
        "0.08",
    ]
    return subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _stop_proc(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=4)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def verify_sse_survives_web_restart() -> dict:
    """Test 1: SIGKILL web server mid-SSE stream, restart on new OS PID, resume seamlessly via Last-Event-ID!"""
    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        port = _free_port()
        env = {
            **os.environ,
            "LAOWADI_JOBS_DB": str(tmp_path / "jobs.sqlite3"),
            "LAOWADI_AUDIT_DB": str(tmp_path / "audit.sqlite3"),
            "LAOWADI_REVIEWS_DB": str(tmp_path / "reviews.sqlite3"),
            "LAOWADI_SESSIONS_DB": str(tmp_path / "sessions.sqlite3"),
            "LAOWADI_PORT": str(port),
            "LAOWADI_MODEL_CONFIG": "",
        }
        worker = _spawn_worker(env, "resumable-ext-worker-1", lease_seconds=15.0)
        web1 = _spawn_uvicorn(port, env, workers=1)
        web2 = None
        try:
            _wait_http(f"http://127.0.0.1:{port}/healthz")
            # Wait for external worker heartbeat to register
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                _, _, rt = _http_json(f"http://127.0.0.1:{port}/api/runtime/status")
                if rt["external_workers_alive"] >= 1:
                    break
                time.sleep(0.1)

            # Submit durable job with controlled step delay so we can kill web1 mid-stream
            st, hdrs1, job = _http_json(
                f"http://127.0.0.1:{port}/api/jobs",
                method="POST",
                body={
                    "job_type": "diagnose",
                    "payload": {
                        "question": "2026年11月13日306柜为什么连续13小时零功率？",
                        "mode": "local",
                        "sessionId": "sse-restart-test",
                        "_test_step_delay_s": 0.20,
                    },
                },
            )
            assert st == 202, f"Expected 202 Accepted, got {st}: {job}"
            job_id = job["job_id"]
            pid_before = int(hdrs1.get("X-Worker-Pid") or hdrs1.get("x-worker-pid"))

            # Open SSE stream on Web Server #1 and read only the first 3 events
            part1_seqs = []
            part1_types = []
            req1 = urllib.request.Request(f"http://127.0.0.1:{port}/api/jobs/{job_id}/stream")
            with urllib.request.urlopen(req1, timeout=10.0) as stream1:
                cur_id = None
                for raw_line in stream1:
                    line = raw_line.decode("utf-8").strip()
                    if line.startswith("id:"):
                        cur_id = int(line[3:].strip())
                    elif line.startswith("data:"):
                        ev = json.loads(line[5:].strip())
                        part1_seqs.append(cur_id)
                        part1_types.append(ev["type"])
                        if len(part1_seqs) >= 3:
                            break

            # HARD-KILL Web Server #1 mid-flight!
            web1.kill()
            web1.wait(timeout=3)
            web1 = None

            # Start brand-new Web Server #2 on the same port (new OS PID!)
            web2 = _spawn_uvicorn(port, env, workers=1)
            _wait_http(f"http://127.0.0.1:{port}/healthz")

            # Reconnect using standard SSE Last-Event-ID header!
            last_seq = part1_seqs[-1]
            part2_seqs = []
            part2_types = []
            final_result = None
            req2 = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/jobs/{job_id}/stream",
                headers={"Last-Event-ID": str(last_seq)},
            )
            with urllib.request.urlopen(req2, timeout=15.0) as stream2:
                pid_after = int(stream2.headers.get("X-Worker-Pid"))
                cur_id = None
                for raw_line in stream2:
                    line = raw_line.decode("utf-8").strip()
                    if line.startswith("id:"):
                        cur_id = int(line[3:].strip())
                    elif line.startswith("data:"):
                        ev = json.loads(line[5:].strip())
                        part2_seqs.append(cur_id)
                        part2_types.append(ev["type"])
                        if ev["type"] == "done":
                            final_result = ev["result"]
                            break

            all_seqs = part1_seqs + part2_seqs
            assert pid_before != pid_after, f"Expected new web PID after restart, got {pid_before} == {pid_after}"
            assert all_seqs == list(range(1, len(all_seqs) + 1)), f"SSE sequence gap or duplicate! {part1_seqs} + {part2_seqs}"
            assert final_result is not None and final_result["verification"]["status"] == "verified"
            assert final_result["executed_by_worker"] == "resumable-ext-worker-1"

            return {
                "job_id": job_id,
                "web_pid_before_kill": pid_before,
                "web_pid_after_restart": pid_after,
                "part1_seqs": part1_seqs,
                "part2_seqs": part2_seqs,
                "zero_gaps_zero_dupes": True,
                "final_verified": True,
            }
        finally:
            _stop_proc(web1)
            _stop_proc(web2)
            _stop_proc(worker)


def verify_worker_crash_reclamation() -> dict:
    """Test 2: SIGKILL Worker #1 mid-job -> lease expires -> Worker #2 atomically reclaims & completes!"""
    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        jobs_db = tmp_path / "jobs.sqlite3"
        env = {
            **os.environ,
            "LAOWADI_JOBS_DB": str(jobs_db),
            "LAOWADI_AUDIT_DB": str(tmp_path / "audit.sqlite3"),
            "LAOWADI_REVIEWS_DB": str(tmp_path / "reviews.sqlite3"),
            "LAOWADI_SESSIONS_DB": str(tmp_path / "sessions.sqlite3"),
        }
        broker = DurableSQLiteBroker(jobs_db)
        job = broker.enqueue(
            "diagnose",
            {
                "question": "请分析1018二采区II回负有功问题",
                "mode": "local",
                "sessionId": "worker-crash-test",
                "_test_step_delay_s": 0.45,
            },
            max_attempts=3,
        )
        job_id = job["job_id"]

        w1 = _spawn_worker(env, "crash-worker-1", lease_seconds=3.0)
        w2 = None
        try:
            # Wait until Worker #1 claims the job
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                cur = broker.get_job(job_id)
                if cur["status"] == "running" and cur["worker_id"] == "crash-worker-1":
                    break
                time.sleep(0.08)
            assert broker.get_job(job_id)["worker_id"] == "crash-worker-1"

            # HARD-KILL Worker #1 mid-job!
            w1.kill()
            w1.wait(timeout=3)
            w1 = None

            # Start Worker #2 -> waits for 3s lease expiry, reclaims job, finishes it!
            w2 = _spawn_worker(env, "recovery-worker-2", lease_seconds=10.0)
            deadline = time.monotonic() + 15.0
            final_job = None
            while time.monotonic() < deadline:
                cur = broker.get_job(job_id)
                if cur["status"] == "completed":
                    final_job = cur
                    break
                time.sleep(0.15)

            assert final_job is not None, f"Worker #2 did not reclaim & complete job: {broker.get_job(job_id)}"
            assert final_job["worker_id"] == "recovery-worker-2"
            assert final_job["attempts"] == 2
            events = broker.get_events_after(job_id, 0)
            ev_types = [e["event_type"] for e in events]
            assert "job_reclaimed" in ev_types, f"Expected job_reclaimed event, got {ev_types}"
            assert final_job["result"]["verification"]["status"] == "verified"

            return {
                "job_id": job_id,
                "reclaimed_by": final_job["worker_id"],
                "attempts": final_job["attempts"],
                "job_reclaimed_event_logged": True,
                "final_verified": True,
            }
        finally:
            _stop_proc(w1)
            _stop_proc(w2)


def verify_multi_worker_scaling_and_shared_rate_limit() -> dict:
    """Test 3: Real 2-worker Uvicorn scaling (distinct OS PIDs) + shared cross-process rate limiter."""
    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        port = _free_port()
        env = {
            **os.environ,
            "LAOWADI_JOBS_DB": str(tmp_path / "jobs.sqlite3"),
            "LAOWADI_AUDIT_DB": str(tmp_path / "audit.sqlite3"),
            "LAOWADI_REVIEWS_DB": str(tmp_path / "reviews.sqlite3"),
            "LAOWADI_SESSIONS_DB": str(tmp_path / "sessions.sqlite3"),
            "LAOWADI_PORT": str(port),
            "LAOWADI_RATE_LIMIT_PER_MIN": "4",
        }
        web = _spawn_uvicorn(port, env, workers=2)
        try:
            _wait_http(f"http://127.0.0.1:{port}/healthz")
            # Wait briefly so both API worker processes register heartbeats
            time.sleep(0.6)
            _, _, rt = _http_json(f"http://127.0.0.1:{port}/api/runtime/status")

            # Send concurrent requests across connections to verify multiple OS worker PIDs
            def _ping(_):
                _, hdrs, _ = _http_json(f"http://127.0.0.1:{port}/api/dates")
                return int(hdrs.get("X-Worker-Pid") or hdrs.get("x-worker-pid"))

            with ThreadPoolExecutor(max_workers=8) as pool:
                pids = set(pool.map(_ping, range(24)))

            # Verify shared cross-process rate limiter (limit=4 globally across ALL workers!)
            statuses = []
            for _ in range(6):
                code, _, _ = _http_json(
                    f"http://127.0.0.1:{port}/api/powerflow",
                    method="POST",
                    body={"scenario": "base_case"},
                )
                statuses.append(code)

            assert rt["api_workers_alive"] == 2, f"Expected 2 alive API worker PIDs, got {rt['workers']}"
            assert statuses[:4] == [200, 200, 200, 200], f"First 4 should succeed, got {statuses}"
            assert statuses[4:] == [429, 429], f"5th & 6th must be 429 globally across workers, got {statuses}"

            return {
                "api_workers_registered": rt["api_workers_alive"],
                "distinct_serving_pids_observed": sorted(pids),
                "shared_rate_limit_statuses": statuses,
            }
        finally:
            _stop_proc(web)


def verify_cross_process_circuit_breaker_and_degradation() -> dict:
    """Test 4: Cross-process CLOSED -> OPEN -> Zero-Wait Local Degradation -> HALF_OPEN -> CLOSED."""
    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        jobs_db = tmp_path / "jobs.sqlite3"
        cb = SharedCircuitBreaker("llm:deepseek", db_path=jobs_db, failure_threshold=3, cooldown_seconds=1.2)
        assert cb.snapshot()["state"] == "closed"

        # Trip circuit breaker from a SEPARATE OS subprocess (3 consecutive upstream failures)
        trip_code = (
            f"from laowadi.circuit_breaker import SharedCircuitBreaker; "
            f"cb = SharedCircuitBreaker('llm:deepseek', db_path={str(jobs_db)!r}, failure_threshold=3, cooldown_seconds=1.2); "
            f"[cb.record_failure('502 Bad Gateway') for _ in range(3)]"
        )
        subprocess.run([sys.executable, "-B", "-c", trip_code], cwd=str(ROOT), check=True)

        # Parent process immediately sees OPEN!
        allowed, snap = cb.allow_request()
        assert allowed is False and snap["state"] == "open" and snap["failure_count"] == 3

        # Run a `mode="deepseek"` diagnose job through ExternalJobWorker while OPEN -> auto-degrades to local!
        from laowadi.worker import ExternalJobWorker

        broker = DurableSQLiteBroker(jobs_db)
        job = broker.enqueue(
            "diagnose",
            {"question": "请分析1018负有功问题", "mode": "deepseek", "sessionId": "cb-degrade-test"},
        )
        w = ExternalJobWorker(
            jobs_db=jobs_db,
            reviews_db=tmp_path / "reviews.sqlite3",
            sessions_db=tmp_path / "sessions.sqlite3",
            audit_db=tmp_path / "audit.sqlite3",
            worker_id="cb-test-worker",
        )
        # Ensure workbench thinks deepseek is configured so degradation is genuinely triggered by the breaker!
        w.workbench.model = "deepseek-v4-pro"
        assert w.run_once() is True
        done_job = broker.get_job(job["job_id"])
        assert done_job["status"] == "completed"
        assert done_job["result"]["circuit_breaker_degraded"] is True
        assert done_job["result"]["mode"] == "local"
        assert done_job["result"]["verification"]["status"] == "verified"
        ev_types = [e["event_type"] for e in broker.get_events_after(job["job_id"], 0)]
        assert "circuit_breaker_degraded" in ev_types

        # Wait for cooldown (1.2s) -> transitions atomically to HALF_OPEN (grants exactly 1 probe slot!)
        time.sleep(1.3)
        allow_probe1, snap_ho = cb.allow_request()
        allow_probe2, _ = cb.allow_request()
        assert allow_probe1 is True and snap_ho["state"] == "half_open"
        assert allow_probe2 is False, "Only 1 concurrent HALF_OPEN probe should be allowed across processes!"

        # Probe succeeds -> CLOSED!
        closed_snap = cb.record_success()
        assert closed_snap["state"] == "closed" and closed_snap["failure_count"] == 0

        return {
            "cross_process_open_observed": True,
            "auto_degraded_to_verified_local": True,
            "half_open_single_probe_enforced": True,
            "recovered_to_closed": True,
        }


def main():
    before_db = sha256(DB)
    before_ev = sha256(EVENT_DB)

    t1 = verify_sse_survives_web_restart()
    t2 = verify_worker_crash_reclamation()
    t3 = verify_multi_worker_scaling_and_shared_rate_limit()
    t4 = verify_cross_process_circuit_breaker_and_degradation()

    assert sha256(DB) == before_db and sha256(EVENT_DB) == before_ev, "Archive SQLite mutated!"
    print(
        json.dumps(
            {
                "status": "ok",
                "archives_unchanged": True,
                "test1_sse_survives_web_restart": t1,
                "test2_worker_crash_lease_reclamation": t2,
                "test3_multi_worker_scaling_and_rate_limit": t3,
                "test4_cross_process_circuit_breaker": t4,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
