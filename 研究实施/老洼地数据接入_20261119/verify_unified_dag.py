"""Verification Suite: Unified Dynamic DAG Engine Across diagnose.py, multi_agent.py & graph.py.

Verifies:
1. `DAGGraph` cycle detection & Kahn topological wave partitioning (parallel diamond DAG).
2. Primary Diagnosis (`diagnose.py`) arbitrary-node pause (`pause_before_node="verify_and_finalize"`) +
   TRUE cross-process resume (`subprocess.run`) with 0 duplicate tool executions & human note injection.
3. 5-Role Multi-Agent (`multi_agent.py`) arbitrary-node pause (`pause_before_node="arbitrate"`) +
   TRUE cross-process resume with 0 duplicate specialist calls & verified conflict arbitration.
4. Checkpointed Audit Graph (`graph.py`) running on `UnifiedDAGRuntime` (NodeInterrupt suspend,
   snapshot tamper rejection, 2-round cumulative supplement merge, 0 duplicate tool steps).
5. FastAPI Unified DAG endpoints (`/api/dag/run`, `/api/dag/resume`, `/api/dag/threads`,
   `/api/dag/threads/{thread_id}`) + RBAC (`viewer` 403 / `dispatcher` 200) & 52 OpenAPI paths.
6. Strict read-only SHA-256 preservation of `local/laowadi.sqlite3` and `local/laowadi_events.sqlite3`.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from fastapi.testclient import TestClient

from laowadi.api import create_app
from laowadi.dag_engine import DAGCheckpointStore, DAGGraph, DAGNode, UnifiedDAGRuntime, compile_and_run_question_dag
from laowadi.diagnose import diagnose_question
from laowadi.graph import CheckpointStore, LaowadiAuditGraph
from laowadi.monitor import EVENT_DB
from laowadi.multi_agent import run_multi_agent_consultation
from laowadi.queries import Archive
from laowadi.security import issue_token
from laowadi.storage import DB, ROOT, connect
from laowadi.workbench import SOURCE_DIR, Workbench


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_dag_primitives() -> None:
    # Cycle detection
    cyc = DAGGraph("cyclic_test")
    cyc.add_node(DAGNode("A", "tool", "A", lambda _c: 1, depends_on=["B"]))
    cyc.add_node(DAGNode("B", "tool", "B", lambda _c: 2, depends_on=["A"]))
    try:
        cyc.topological_waves()
        raise AssertionError("Cycle should have raised ValueError")
    except ValueError:
        pass

    # Parallel diamond DAG waves
    dia = DAGGraph("diamond_test")
    dia.add_node(DAGNode("start", "guard", "Start", lambda _c: "ok"))
    dia.add_node(DAGNode("branch_1", "specialist", "B1", lambda _c: 10, depends_on=["start"]))
    dia.add_node(DAGNode("branch_2", "specialist", "B2", lambda _c: 20, depends_on=["start"]))
    dia.add_node(
        DAGNode(
            "merge",
            "arbitrate",
            "Merge",
            lambda ctx: ctx.completed_outputs["branch_1"] + ctx.completed_outputs["branch_2"],
            depends_on=["branch_1", "branch_2"],
        )
    )
    waves = [[n.node_id for n in w] for w in dia.topological_waves()]
    assert waves == [["start"], ["branch_1", "branch_2"], ["merge"]], f"Unexpected waves: {waves}"
    with tempfile.TemporaryDirectory() as td:
        rt = UnifiedDAGRuntime(DAGCheckpointStore(Path(td) / "ckpt.sqlite3"))
        st = rt.execute(dia, thread_id="DIA-1", parallel=True)
        assert st["status"] == "completed" and st["completed_nodes"]["merge"]["output"] == 30
    print("[1/6] DAGGraph cycle detection & parallel diamond wave scheduling OK")


def _resume_in_subprocess(ckpt_db: Path, thread_id: str, human_input: dict) -> dict:
    code = f"""
import json
from contextlib import closing
from pathlib import Path
from laowadi.dag_engine import resume_dag_thread
from laowadi.monitor import EVENT_DB
from laowadi.queries import Archive
from laowadi.storage import DB, connect

with closing(connect(DB)) as conn:
    arch = Archive(conn, EVENT_DB)
    out = resume_dag_thread(
        arch,
        {thread_id!r},
        human_input={human_input!r},
        checkpoint_db=Path({str(ckpt_db)!r}),
    )
print(json.dumps(out, ensure_ascii=False))
"""
    proc = subprocess.run([sys.executable, "-B", "-c", code], cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"Subprocess resume failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    # Last line is JSON output (preceding lines may be structured JSON log lines)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def verify_diagnose_pause_and_cross_process_resume() -> None:
    q = "请分析 1018 二采区II回全周持续负有功与负功率因数的原因并对照安规给出排查建议。"
    with tempfile.TemporaryDirectory() as td:
        ckpt = Path(td) / "diag_ckpt.sqlite3"
        with closing(connect(DB)) as conn:
            arch = Archive(conn, EVENT_DB)
            p1 = diagnose_question(
                arch,
                q,
                thread_id="DAG-DIAG-TEST-01",
                checkpoint_db=ckpt,
                pause_before_node="verify_and_finalize",
            )
        assert p1["status"] == "suspended"
        assert p1["dag_execution"]["engine"] == "UnifiedDAGRuntime"
        assert p1["dag_execution"]["suspended_at_node"] == "verify_and_finalize"
        assert p1["dag_execution"]["executed_nodes"] == ["guard", "retrieve_and_execute"]
        assert len(p1["tool_trace"]) >= 2

        # Resume in a brand-new OS subprocess!
        p2 = _resume_in_subprocess(ckpt, "DAG-DIAG-TEST-01", {"operator_note": "值班员已确认1018端子排待核"})
        assert p2["status"] == "completed"
        assert p2["dag_execution"]["reused_nodes"] == ["guard", "retrieve_and_execute"]
        assert p2["dag_execution"]["executed_nodes"] == ["verify_and_finalize"]
        assert p2["result"]["verification"]["status"] == "verified"
        assert p2["result"]["report"]["operator_note"] == "值班员已确认1018端子排待核"
        assert "值班员已确认1018端子排待核" not in p2["result"]["report"]["summary"]
    print("[2/6] diagnose.py arbitrary-node pause + TRUE cross-process resume (0 duplicate tool calls) OK")


def verify_multi_agent_pause_and_cross_process_resume() -> None:
    q = "请对 1010 二回井下 1535 测算无功补偿容量与降损效果，并同时评估投切电容会不会加剧 10 kV II段过压，给出综合治理方案。"
    with tempfile.TemporaryDirectory() as td:
        ckpt = Path(td) / "ma_ckpt.sqlite3"
        with closing(connect(DB)) as conn:
            arch = Archive(conn, EVENT_DB)
            p1 = run_multi_agent_consultation(
                arch,
                q,
                thread_id="DAG-MA-TEST-01",
                checkpoint_db=ckpt,
                pause_before_node="arbitrate",
            )
        assert p1["status"] == "suspended"
        assert p1["dag_execution"]["engine"] == "UnifiedDAGRuntime"
        assert p1["dag_execution"]["suspended_at_node"] == "arbitrate"
        assert len(p1["specialist_briefs"]) == 3
        assert all(f"specialist:{r}" in p1["dag_execution"]["executed_nodes"] for r in ("telemetry", "physics", "standards"))
        original_revision = next(m for m in p1["inter_agent_messages"] if m.get("id") == "reactive-revision")

        # Resume in a brand-new OS subprocess -> 0 specialists re-run!
        p2 = _resume_in_subprocess(ckpt, "DAG-MA-TEST-01", {"operator_note": "同意先调2号主变分接头再投电容"})
        assert p2["status"] == "completed"
        reused = p2["dag_execution"]["reused_nodes"]
        assert "guard" in reused and "plan" in reused
        assert all(f"specialist:{r}" in reused for r in ("telemetry", "physics", "standards"))
        assert {"reactive:challenge", "reactive:revise"} <= set(reused)
        assert p2["dag_execution"]["executed_nodes"] == ["arbitrate", "verify"]
        assert p2["result"]["verification"]["status"] == "verified"
        assert any(c["id"] == "CONFLICT-OVERVOLT-VS-REACTIVE" for c in p2["result"]["conflicts_resolved"])
        assert original_revision in p2["result"]["inter_agent_messages"]
        assert p2["result"]["reactive_arbitration"]["selected"]["variant"] == "tap_then_cap"
    print("[3/6] multi_agent.py parallel fan-out pause before arbitrate + TRUE cross-process resume (0 duplicate specialists) OK")


def verify_audit_graph_on_unified_runtime() -> None:
    with tempfile.TemporaryDirectory() as td:
        ckpt = Path(td) / "ag_ckpt.sqlite3"
        with closing(connect(DB)) as conn:
            arch = Archive(conn, EVENT_DB)
            g = LaowadiAuditGraph(arch, store=CheckpointStore(ckpt))
            spec = {"task_type": "single_incomer_overload_audit", "question": "核验303单进线带双主变热稳定"}
            s1 = g.run("DAG-AG-01", spec)
            assert s1["terminal_status"] == "needs_supplement"
            assert s1["dag_execution"]["engine"] == "UnifiedDAGRuntime"
            assert s1["dag_execution"]["suspended_at_node"] == "evaluate_gate"
            assert len(s1["executed_in_this_run"]) == 3

            # Tampered archive snapshot rejected
            s_tamp = g.run("DAG-AG-01", spec, override_snapshot_sha256="f" * 64)
            assert s_tamp["terminal_status"] == "restricted" and s_tamp["stop_reason"] == "snapshot_version_mismatch"

            # 2-round cumulative resume -> 0 duplicate tool steps
            s2 = g.run(
                "DAG-AG-01",
                spec,
                supplement_packet={"station": "S35", "source_doc": "补证1", "parameters": {"303_rated_current_a": 200.0, "main_transformer_rated_kva": 6300.0}},
            )
            assert s2["terminal_status"] == "needs_supplement" and s2["executed_in_this_run"] == []
            s3 = g.run(
                "DAG-AG-01",
                spec,
                supplement_packet={"station": "S35", "source_doc": "补证2", "parameters": {"ambient_derating_factor": 0.9, "switch_log_20261113": "300母联合位"}},
            )
            assert s3["terminal_status"] == "completed" and s3["executed_in_this_run"] == []
            assert len(s3["reused_steps_in_this_run"]) == 3
    print("[4/6] graph.py running on UnifiedDAGRuntime (NodeInterrupt, tamper guard & 2-round cumulative resume) OK")


def verify_fastapi_dag_endpoints_and_rbac() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        wb = Workbench(
            DB,
            EVENT_DB,
            SOURCE_DIR,
            model=None,
            reviews_db=tmp / "reviews.sqlite3",
            sessions_db=tmp / "sessions.sqlite3",
            audit_db=tmp / "audit.sqlite3",
            jobs_db=tmp / "jobs.sqlite3",
            database_url=f"sqlite+pysqlite:///{tmp / 'ent.sqlite3'}",
        )
        try:
            app = create_app(wb, require_auth=True, rate_limit_per_min=300)
            client = TestClient(app)
            viewer_hdr = {"Authorization": f"Bearer {issue_token('auditor', 'viewer')['access_token']}"}
            disp_hdr = {"Authorization": f"Bearer {issue_token('disp', 'dispatcher')['access_token']}"}

            # RBAC check
            assert client.post("/api/dag/run", json={"question": "测试"}, headers=viewer_hdr).status_code == 403

            # Run & pause via API
            r_run = client.post(
                "/api/dag/run",
                json={
                    "question": "请评估 2026年11月13日17:00 10 kV II段过压 10.90 kV 的调压方案",
                    "mode": "diagnose",
                    "thread_id": "API-DAG-PAUSE-01",
                    "pause_before_node": "verify_and_finalize",
                },
                headers=disp_hdr,
            )
            assert r_run.status_code == 200, r_run.text
            d1 = r_run.json()
            assert d1["status"] == "suspended"

            # Viewer inspects thread list & detail
            assert client.get("/api/dag/threads", headers=viewer_hdr).status_code == 200
            r_det = client.get("/api/dag/threads/API-DAG-PAUSE-01", headers=viewer_hdr)
            assert r_det.status_code == 200 and r_det.json()["status"] == "suspended"

            # Dispatcher resumes thread
            r_res = client.post(
                "/api/dag/resume",
                json={"thread_id": "API-DAG-PAUSE-01", "human_input": {"approved": True, "operator_note": "批准调档评估"}},
                headers=disp_hdr,
            )
            assert r_res.status_code == 200, r_res.text
            d2 = r_res.json()
            assert d2["status"] == "completed"
            assert d2["dag_execution"]["reused_nodes"] == ["guard", "retrieve_and_execute"]
            oa_paths = len(client.get("/openapi.json", headers=viewer_hdr).json()["paths"])
            assert oa_paths >= 52, f"Expected >=52 OpenAPI paths, got {oa_paths}"
            print(f"[5/6] FastAPI /api/dag/* (run/pause/resume/threads) & RBAC OK ({oa_paths} OpenAPI paths)")
        finally:
            wb.db_manager.dispose()


def main() -> None:
    before_db = _sha256(DB)
    before_ev = _sha256(EVENT_DB)
    verify_dag_primitives()
    verify_diagnose_pause_and_cross_process_resume()
    verify_multi_agent_pause_and_cross_process_resume()
    verify_audit_graph_on_unified_runtime()
    verify_fastapi_dag_endpoints_and_rbac()
    assert _sha256(DB) == before_db and _sha256(EVENT_DB) == before_ev
    print("[6/6] Read-only source archives SHA-256 strictly unchanged OK")
    print("\nALL 6 UNIFIED DYNAMIC DAG ENGINE CHECKS PASSED!")


if __name__ == "__main__":
    main()
