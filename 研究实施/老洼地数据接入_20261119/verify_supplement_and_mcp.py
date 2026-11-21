"""Verification for Stage 11 (Supplement & Local Recalculation) and Stage 13 (Read-Only MCP Server)."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from laowadi.diag_tools import DiagnosticToolbox
from laowadi.mcp_server import ReadOnlyMCPServer
from laowadi.monitor import EVENT_DB
from laowadi.queries import Archive
from laowadi.storage import DB, REPORT_STATION, ROOT, connect
from laowadi.supplement import (
    apply_supplement,
    build_baseline_verification,
    render_supplement_markdown,
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_stage11_supplement(archive: Archive) -> dict:
    # 1. Baseline v1: missing parameters preserve confirmed local facts and cells
    v1 = build_baseline_verification(archive)
    assert v1["version"] == 1 and v1["parent_run_id"] is None
    assert v1["summary"]["needs_supplement"] == 4
    v01_base = next(x for x in v1["items"] if x["id"] == "V01")
    assert v01_base["confirmed_local_facts"]["i303_peak_a"] == 100.06
    assert "1113.xls / New Sheet1!C22" in v01_base["confirmed_local_facts"]["evidence_cells"]
    assert v01_base["conditional_judgment"] is None

    # 2. Guardrail: reject unbound station or invalid derating factor
    for bad_pkt in (
        {"station": "C1535", "source_doc": "bad", "parameters": {"303_rated_current_a": 200}},
        {"station": "S35", "source_doc": "", "parameters": {"303_rated_current_a": 200}},
        {"station": "S35", "source_doc": "bad", "parameters": {"ambient_derating_factor": 1.8}},
    ):
        try:
            apply_supplement(v1, bad_pkt)
            raise AssertionError(f"Expected ValueError for invalid packet {bad_pkt}")
        except ValueError:
            pass

    # 3. Guardrail: incomplete dependencies keep item in partial_supplement without conditional judgment
    v_partial = apply_supplement(v1, {
        "station": REPORT_STATION,
        "source_doc": "局部测试单",
        "parameters": {"303_rated_current_a": 200.0},
    })
    v01_part = next(x for x in v_partial["items"] if x["id"] == "V01")
    assert v01_part["status"] == "partial_supplement" and v01_part["conditional_judgment"] is None
    assert v_partial["summary"]["recalculated_items"] == 1 and v_partial["summary"]["reused_items"] == 3

    # 4. Full valid supplement for V01 & V02 -> selective local recalculation, V03 & V04 reused from v1!
    valid_packet = {
        "station": REPORT_STATION,
        "source_doc": "老洼地机电科参数与操作核实单-20261119（示例核验批次）",
        "parameters": {
            "303_rated_current_a": 200.0,
            "main_transformer_rated_kva": 6300.0,
            "ambient_derating_factor": 0.90,
            "switch_log_20261113": "35kV母联300开关合位，306柜07:00-19:00计划停电检修",
            "1009_1010_rated_current_a": 300.0,
            "c1535_tie_switch_mode": "1535中央变电所1200联络开关热备用可自动切换",
        },
    }
    v2 = apply_supplement(v1, valid_packet)
    assert v2["version"] == 2 and v2["parent_run_id"] == v1["run_id"]
    assert v2["snapshot"]["snapshot_sha256"] == v1["snapshot"]["snapshot_sha256"]
    assert v2["summary"]["recalculated_items"] == 2
    assert v2["summary"]["reused_items"] == 2
    assert v2["summary"]["conditionally_satisfied"] == 2

    by_id = {x["id"]: x for x in v2["items"]}
    assert by_id["V01"]["status"] == "conditionally_satisfied" and by_id["V01"]["recalculated"] is True
    assert by_id["V01"]["conditional_judgment"]["current_load_ratio_pct"] == 55.59
    assert by_id["V02"]["status"] == "conditionally_satisfied" and by_id["V02"]["recalculated"] is True
    assert by_id["V03"]["recalculated"] is False and by_id["V03"]["reused_from_run"] == v1["run_id"]
    assert by_id["V04"]["recalculated"] is False and by_id["V04"]["reused_from_run"] == v1["run_id"]

    # Save v1/v2 report artifacts
    md = render_supplement_markdown(v1, v2)
    (ROOT / "local" / "老洼地补证与局部复算报告.md").write_text(md, encoding="utf-8")
    (ROOT / "local" / "老洼地补证与局部复算报告.json").write_text(
        json.dumps({"baseline_v1": v1, "supplemented_v2": v2}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "v1_run_id": v1["run_id"],
        "v2_run_id": v2["run_id"],
        "parent_linked": v2["parent_run_id"] == v1["run_id"],
        "recalculated_items": v2["summary"]["recalculated_items"],
        "reused_items": v2["summary"]["reused_items"],
    }


def verify_stage13_mcp(archive: Archive) -> dict:
    server = ReadOnlyMCPServer(archive)
    toolbox = DiagnosticToolbox(archive)

    # 1. Initialize & tools/list
    init_resp = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init_resp["result"]["protocolVersion"] == "2024-11-05"
    assert init_resp["result"]["meta"]["readOnly"] is True

    tools_resp = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = tools_resp["result"]["tools"]
    assert len(tools) == 15
    assert all(t["annotations"]["readOnlyHint"] is True for t in tools)

    # 2. Exact parity check: MCP tools/call vs direct Archive/DiagnosticToolbox calls
    parity_checks = [
        (
            "get_hourly_reading",
            {"station": "S35", "cabinet": "303", "metric": "P", "at": "2026-11-10 12:00:00"},
            archive.call("get_hourly_reading", {"station": "S35", "cabinet": "303", "metric": "P", "at": "2026-11-10 12:00:00"}),
        ),
        (
            "get_monitor_event",
            {"event_id": "LW-000010"},
            archive.call("get_monitor_event", {"event_id": "LW-000010"}),
        ),
        (
            "get_supply_link",
            {"station": "S35", "cabinet": "1009", "direction": "downstream"},
            archive.call("get_supply_link", {"station": "S35", "cabinet": "1009", "direction": "downstream"}),
        ),
        (
            "check_power_balance",
            {"at_time": "2026-11-12 12:00:00"},
            toolbox.execute("check_power_balance", {"at_time": "2026-11-12 12:00:00"}),
        ),
        (
            "query_electrical_analytics",
            {"operation": "aggregate_rank", "metric": "P", "agg": "sum", "cabinets": ["1009", "1010"], "day": "2026-11-14", "top_k": 5},
            toolbox.execute("query_electrical_analytics", {"operation": "aggregate_rank", "metric": "P", "agg": "sum", "cabinets": ["1009", "1010"], "day": "2026-11-14", "top_k": 5}),
        ),
        (
            "simulate_electrical_engineering",
            {"operation": "reactive_compensation", "cabinet": "1010", "target_cos": 0.90},
            toolbox.execute("simulate_electrical_engineering", {"operation": "reactive_compensation", "cabinet": "1010", "target_cos": 0.90}),
        ),
        (
            "run_ac_power_flow",
            {"scenario": "tap_sensitivity", "at_time": "2026-11-13 17:00:00"},
            toolbox.execute("run_ac_power_flow", {"scenario": "tap_sensitivity", "at_time": "2026-11-13 17:00:00"}),
        ),
    ]
    for idx, (t_name, t_args, expected) in enumerate(parity_checks, 10):
        resp = server.handle_message({
            "jsonrpc": "2.0",
            "id": idx,
            "method": "tools/call",
            "params": {"name": t_name, "arguments": t_args},
        })
        assert resp["result"]["isError"] is False
        assert resp["result"]["structuredContent"] == expected, f"Parity mismatch on {t_name}"

    # 3. Illegal, out-of-bounds, unknown tool, and timeout requests receive explicit feedback
    unk_resp = server.handle_message({
        "jsonrpc": "2.0",
        "id": 30,
        "method": "tools/call",
        "params": {"name": "close_breaker_306", "arguments": {}},
    })
    assert "error" in unk_resp and unk_resp["error"]["code"] == -32602

    bad_arg_resp = server.handle_message({
        "jsonrpc": "2.0",
        "id": 31,
        "method": "tools/call",
        "params": {"name": "get_hourly_reading", "arguments": {"cabinet": "9999", "metric": "P", "at": "2026-11-10 12:00:00"}},
    })
    assert bad_arg_resp["result"]["isError"] is True

    timeout_resp = server.handle_message({
        "jsonrpc": "2.0",
        "id": 32,
        "method": "tools/call",
        "params": {"name": "get_station_health_overview", "arguments": {}, "timeout_ms": 999999},
    })
    assert "error" in timeout_resp and timeout_resp["error"]["code"] == -32602

    # 4. Subprocess stdio transport end-to-end check
    stdio_input = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "get_hourly_reading",
                "arguments": {"cabinet": "306", "metric": "P", "at": "2026-11-10 12:00:00"},
            },
        }),
    ]) + "\n"
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "laowadi", "mcp"],
        cwd=ROOT,
        input=stdio_input,
        text=True,
        capture_output=True,
        check=True,
    )
    out_lines = [json.loads(line) for line in proc.stdout.strip().splitlines() if line.strip()]
    assert len(out_lines) == 2
    assert out_lines[1]["result"]["structuredContent"]["value"] == 223.13
    assert out_lines[1]["result"]["structuredContent"]["source"]["cell"] == "H16"

    return {
        "tools_exposed": len(tools),
        "parity_checks_passed": len(parity_checks),
        "error_handling_verified": True,
        "stdio_subprocess_verified": True,
    }


def main():
    db_before = sha256(DB)
    ev_before = sha256(EVENT_DB)

    with closing(connect(DB)) as db:
        archive = Archive(db, EVENT_DB)
        s11 = verify_stage11_supplement(archive)
        s13 = verify_stage13_mcp(archive)

    assert sha256(DB) == db_before, "laowadi.sqlite3 was modified!"
    assert sha256(EVENT_DB) == ev_before, "laowadi_events.sqlite3 was modified!"

    print(
        json.dumps(
            {
                "status": "ok",
                "stage11_supplement": s11,
                "stage13_mcp": s13,
                "archives_unchanged": True,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
