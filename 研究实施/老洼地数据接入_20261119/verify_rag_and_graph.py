"""End-to-end verification for Stage 09 (Conditional RAG), Stage 10 (State-Graph Cross-Process Resume),
and Stage 15 (Continuous Stream Fault-Injection & Latency Simulation)."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from laowadi.benchmark import write_benchmark_reports
from laowadi.graph import write_graph_reports
from laowadi.monitor import EVENT_DB
from laowadi.queries import Archive
from laowadi.rag import ConditionalRAGEngine, write_rag_reports
from laowadi.storage import DB, ROOT, connect
from laowadi.stream_sim import write_stream_reports


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_true_cross_process_resume(ckpt_file: Path):
    """Run Phase 1 in Subprocess A (suspend at needs_supplement) and Phase 2 in Subprocess B (resume -> completed)."""
    code_phase1 = f"""
from contextlib import closing
from pathlib import Path
import json
from laowadi.storage import DB, connect
from laowadi.monitor import EVENT_DB
from laowadi.queries import Archive
from laowadi.graph import CheckpointStore, LaowadiAuditGraph

store = CheckpointStore(Path({str(ckpt_file)!r}))
with closing(connect(DB)) as db:
    graph = LaowadiAuditGraph(Archive(db, EVENT_DB), store=store)
    state = graph.run("TH-CROSS-PROC-01", {{
        "task_type": "single_incomer_overload_audit",
        "question": "核验 2026-11-13 303 单进线带双主变是否过载",
    }})
    print(json.dumps({{
        "terminal_status": state["terminal_status"],
        "executed": len(state["executed_in_this_run"]),
        "reused": len(state["reused_steps_in_this_run"]),
        "snapshot_sha256": state["snapshot"]["snapshot_sha256"],
    }}))
"""
    p1 = subprocess.run([sys.executable, "-B", "-c", code_phase1], cwd=str(ROOT), capture_output=True, text=True, check=True)
    res1 = json.loads(p1.stdout.strip())
    assert res1["terminal_status"] == "needs_supplement"
    assert res1["executed"] == 3 and res1["reused"] == 0

    code_phase2 = f"""
from contextlib import closing
from pathlib import Path
import json
from laowadi.storage import DB, REPORT_STATION, connect
from laowadi.monitor import EVENT_DB
from laowadi.queries import Archive
from laowadi.graph import CheckpointStore, LaowadiAuditGraph

store = CheckpointStore(Path({str(ckpt_file)!r}))
packet = {{
    "station": REPORT_STATION,
    "source_doc": "跨进程恢复补证核实单",
    "parameters": {{
        "303_rated_current_a": 200.0,
        "main_transformer_rated_kva": 6300.0,
        "ambient_derating_factor": 0.90,
        "switch_log_20261113": "35kV母联300合位，306检修",
    }},
}}
with closing(connect(DB)) as db:
    graph = LaowadiAuditGraph(Archive(db, EVENT_DB), store=store)
    state = graph.run("TH-CROSS-PROC-01", {{
        "task_type": "single_incomer_overload_audit",
        "question": "核验 2026-11-13 303 单进线带双主变是否过载",
    }}, supplement_packet=packet)
    print(json.dumps({{
        "terminal_status": state["terminal_status"],
        "executed": len(state["executed_in_this_run"]),
        "reused": len(state["reused_steps_in_this_run"]),
        "resume_count": state["resume_count"],
        "v01_status": state["supplement_result"]["v01_status"],
    }}))
"""
    p2 = subprocess.run([sys.executable, "-B", "-c", code_phase2], cwd=str(ROOT), capture_output=True, text=True, check=True)
    res2 = json.loads(p2.stdout.strip())
    assert res2["terminal_status"] == "completed"
    assert res2["executed"] == 0, "Cross-process resume re-executed already confirmed steps!"
    assert res2["reused"] == 3
    assert res2["resume_count"] == 1
    assert res2["v01_status"] == "conditionally_satisfied"
    return {"phase1_subprocess": res1, "phase2_subprocess": res2}


def main():
    db_before = sha256(DB)
    ev_before = sha256(EVENT_DB)

    with closing(connect(DB)) as db:
        archive = Archive(db, EVENT_DB)

        # 1. Stage 09: Conditional RAG & Ablation
        rag_engine = ConditionalRAGEngine(archive)
        # Anti-regression: every evidence card must have non-empty verified SHA-256 hashes from SQLite
        for card in rag_engine.cards:
            assert card["source_sha256"], f"Card {card['card_id']} missing source_sha256"
            for fname, digest in card["source_sha256"].items():
                assert len(digest) == 64, f"Empty/invalid sha256 for {fname} in {card['card_id']}"
        trap = rag_engine.retrieve("请查询1535中央变电所1101柜12:00的有功功率读数", cabinet="1101", day="2026-11-13")
        assert trap["applicability_status"] == "rejected_precondition"
        assert len(trap["applicability_warnings"]) >= 1

        rag_report = write_rag_reports(archive)
        assert rag_report["tools_plus_conditional_rag"]["passed"] == 12
        assert rag_report["direct_tools_only"]["passed"] == 4
        assert len(rag_report["improved_case_ids"]) == 8

        # 2. Stage 10: State-Graph Orchestrator & Subprocess Resume (V01 + V02 + tamper + restrict)
        graph_report = write_graph_reports(archive)
        assert graph_report["passed_scenarios"] == 6

        # 3. Stage 15: Continuous Stream Fault-Injection & Latency Simulation
        stream_report = write_stream_reports(archive)
        assert stream_report["passed_tests"] == 4
        assert stream_report["latency_ms"]["p95_ms"] < 10.0

        # 4. Comprehensive 36-Case Thesis Benchmark & 4-Axis Ablation Matrix
        bench36 = write_benchmark_reports(archive)
        assert bench36["total_passed"] == 36 and bench36["total_cases"] == 36
        assert bench36["ablation_matrix"][0]["full_two_level_caught"] == 4

    with TemporaryDirectory() as tmp:
        cross_proc = verify_true_cross_process_resume(Path(tmp) / "cross_ckpt.sqlite3")

    assert sha256(DB) == db_before, "laowadi.sqlite3 was modified!"
    assert sha256(EVENT_DB) == ev_before, "laowadi_events.sqlite3 was modified!"

    print(
        json.dumps(
            {
                "status": "ok",
                "stage09_rag_ablation": f"{rag_report['direct_tools_only']['passed']}/{rag_report['total_cases']} -> {rag_report['tools_plus_conditional_rag']['passed']}/{rag_report['total_cases']} (improved {len(rag_report['improved_case_ids'])})",
                "stage10_graph_scenarios": f"{graph_report['passed_scenarios']}/{graph_report['total_scenarios']}",
                "stage10_true_subprocess_resume_verified": cross_proc["phase2_subprocess"]["reused"] == 3,
                "stage15_stream_tests": f"{stream_report['passed_tests']}/{stream_report['total_tests']}",
                "stage15_frame_latency_p50_ms": stream_report["latency_ms"]["p50_ms"],
                "benchmark_36_passed": f"{bench36['total_passed']}/{bench36['total_cases']}",
                "archives_unchanged": True,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
