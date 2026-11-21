"""Verification suite for High-Order Agent v3:
1. Auto-Memory Extraction (detect_memory_candidates + SSE memory_suggestion)
2. True Parallel Subagent Spawning & Adversarial Debate (ThreadPoolExecutor 3-way)
3. Pluggable Markdown Skills Engine & Read-Only MCP Client Registry
4. Read-Only Industrial Protocol Adapters (Modbus/OPC-UA/MQTT) & Observability Summary
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from laowadi.autonomous_agent import run_autonomous_agent
from laowadi.memory_store import create_memory, detect_memory_candidates
from laowadi.monitor import EVENT_DB
from laowadi.protocol_adapter import (
    get_agent_observability_summary,
    ingest_protocol_frame,
    list_protocol_adapters,
)
from laowadi.queries import Archive
from laowadi.skills_engine import create_skill, list_mcp_clients, list_skills, register_mcp_client, run_skill
from laowadi.storage import DB, connect
from laowadi.subagent_debate import run_parallel_subagent_debate


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_auto_memory(archive: Archive, tmp: Path) -> dict:
    mem_db = tmp / "auto_mem.sqlite3"
    cands = detect_memory_candidates("以后老洼地 1006 柜内部考核电压红线按 10.62 kV 执行，别按 10.70 kV 算")
    assert len(cands) >= 1 and cands[0]["category"] == "semantic"
    assert cands[0]["metric_threshold"]["value"] == 10.62 and cands[0]["scope_cabinet"] == "1006"

    # End-to-end SSE emission via run_autonomous_agent
    events: list[dict] = []
    res = run_autonomous_agent(
        archive,
        "记住：以后 1006 柜内部考核电压红线按 10.62 kV 执行，顺便分析 1006 柜过压调档方案",
        memory_db_path=mem_db,
        on_event=events.append,
    )
    assert res["status"] == "ok"
    assert any(e["type"] == "memory_suggestion" for e in events)
    assert len(res.get("memory_suggestions") or []) >= 1

    # Procedural SOP detection
    sop_cands = detect_memory_candidates("记住这条 SOP：每次倒闸操作前必须先核对 2# 主变现场实际挡位铭牌")
    assert len(sop_cands) >= 1 and sop_cands[0]["category"] == "procedural"
    return {"threshold_candidate_kv": cands[0]["metric_threshold"]["value"], "sse_emitted": True}


def verify_parallel_debate(archive: Archive, tmp: Path) -> dict:
    mem_db = tmp / "auto_mem.sqlite3"
    events: list[dict] = []
    res = run_parallel_subagent_debate(
        archive,
        "1010 柜功率因数偏低同时 10 kV II 段存在过压，能否直接单投末端电容补偿？",
        memory_db_path=mem_db,
        on_event=events.append,
    )
    ev_types = [e["type"] for e in events]
    for needed in ("subagent_start", "subagent_done", "debate_rebuttal", "arbitration_done", "done"):
        assert needed in ev_types, f"Missing debate SSE event {needed}"
    assert res["status"] == "ok" and len(res["subagents"]) == 3
    assert len(res["rebuttals"]) >= 3
    assert len(res["arbitration"]["consensus"]) >= 2
    assert res["parallel_threads_used"] >= 1
    return {
        "subagents": len(res["subagents"]),
        "rebuttals": len(res["rebuttals"]),
        "parallel_threads": res["parallel_threads_used"],
    }


def verify_skills_and_mcp_clients(archive: Archive, tmp: Path) -> dict:
    builtin = list_skills()
    assert len(builtin) >= 3
    ids = {s["id"] for s in builtin}
    assert {"main_fan_switchover", "storm_lightning_patrol", "tap_change_precheck"} <= ids

    # Run built-in skill end-to-end
    run_res = run_skill(archive, "tap_change_precheck", target_cabinet="1006")
    assert run_res["status"] == "ok" and len(run_res["checks"]) >= 3

    # Create custom skill in isolated temp dir
    custom_dir = tmp / "skills"
    sk = create_skill(
        skill_id="shift_quick_audit",
        title="交接班电压与无功快速核查",
        triggers=["交接班", "快速核查"],
        body_markdown="1. 检查 10 kV II 段最高电压。\n2. 复算 pandapower 分接头挡位。",
        skills_dir=custom_dir,
    )
    assert sk["id"] == "shift_quick_audit" and len(list_skills(skills_dir=custom_dir)) == 1
    custom_run = run_skill(archive, "shift_quick_audit", skills_dir=custom_dir)
    assert custom_run["status"] == "ok"

    # MCP Client registry
    reg_file = tmp / "mcp_clients.json"
    initial = list_mcp_clients(archive=archive, registry_path=reg_file)
    assert len(initial) == 1 and initial[0]["tools_count"] == 15
    reg = register_mcp_client(
        name="Mine SCADA ReadOnly Bridge",
        endpoint="https://scada-mcp.example.com/rpc",
        transport="http",
        description="External read-only SCADA MCP bridge",
        registry_path=reg_file,
    )
    assert reg["read_only"] is True and len(list_mcp_clients(archive=archive, registry_path=reg_file)) == 2
    # Insecure non-localhost HTTP must be rejected
    try:
        register_mcp_client(name="Bad", endpoint="http://evil.example.com/rpc", transport="http", registry_path=reg_file)
        raise AssertionError("Insecure remote HTTP MCP endpoint should be rejected")
    except ValueError:
        pass
    return {"builtin_skills": len(builtin), "custom_skill_ran": True, "mcp_clients": 2}


def verify_protocols_and_observability(archive: Archive, tmp: Path) -> dict:
    live_db = tmp / "proto_live.sqlite3"
    mem_db = tmp / "auto_mem.sqlite3"
    act_db = tmp / "proto_actions.sqlite3"

    adapters = list_protocol_adapters()
    assert len(adapters) == 3

    # 1. Valid Modbus TCP FC03 read-only frame
    mb_ok = ingest_protocol_frame(
        "modbus_tcp",
        {
            "function_code": 3,
            "timestamp": "2026-11-18 10:00:00",
            "registers": [
                {"cabinet": "1006", "metric": "Uab", "raw_value": 1092, "scale": 0.01},
                {"cabinet": "1006", "metric": "P", "raw_value": -385, "scale": 0.1},
            ],
        },
        live_db_path=live_db,
    )
    assert mb_ok["status"] == "ok" and mb_ok["findings_count"] >= 2

    # 2. HARD SECURITY GUARD: Modbus write FC06 MUST be blocked!
    try:
        ingest_protocol_frame("modbus_tcp", {"function_code": 6, "registers": []}, live_db_path=live_db)
        raise AssertionError("Modbus write FC06 must be blocked!")
    except ValueError:
        pass

    # 3. Valid OPC-UA & MQTT read-only frames
    opc_ok = ingest_protocol_frame(
        "opc_ua",
        {"timestamp": "2026-11-18 11:00:00", "nodes": [{"node_id": "ns=2;s=S35.1010.cos", "value": 0.65}]},
        live_db_path=live_db,
    )
    assert opc_ok["status"] == "ok"

    mqtt_ok = ingest_protocol_frame(
        "mqtt_scada",
        {"topic": "mine/s35/telemetry/hourly", "timestamp": "2026-11-18 12:00:00", "measurements": [{"cabinet": "1006", "Uab": 10.95}]},
        live_db_path=live_db,
    )
    assert mqtt_ok["status"] == "ok"

    # 4. MQTT control topic MUST be blocked!
    try:
        ingest_protocol_frame("mqtt_scada", {"topic": "mine/s35/control/trip", "measurements": []}, live_db_path=live_db)
        raise AssertionError("MQTT control topic must be blocked!")
    except ValueError:
        pass

    # 5. Observability summary
    create_memory(category="semantic", title="test", content="test rule", db_path=mem_db)
    obs = get_agent_observability_summary(
        archive=archive,
        memory_db_path=mem_db,
        actions_db_path=act_db,
        live_db_path=live_db,
    )
    assert obs["status"] == "ok" and obs["live_ingestion"]["uploads_count"] == 3
    assert obs["ecosystem"]["skills_count"] >= 3 and obs["ecosystem"]["read_only_tools_count"] == 15
    return {"protocols_ingested": 3, "write_blocks_verified": True, "observability_ok": True}


def main():
    before_db = sha256(DB)
    before_ev = sha256(EVENT_DB)
    with TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        with closing(connect(DB)) as db:
            archive = Archive(db, EVENT_DB)
            m = verify_auto_memory(archive, tmp)
            d = verify_parallel_debate(archive, tmp)
            s = verify_skills_and_mcp_clients(archive, tmp)
            p = verify_protocols_and_observability(archive, tmp)
    assert sha256(DB) == before_db and sha256(EVENT_DB) == before_ev, "Golden archives mutated!"
    print(
        json.dumps(
            {
                "status": "ok",
                "archives_unchanged": True,
                "auto_memory": m,
                "parallel_debate": d,
                "skills_and_mcp": s,
                "protocols_and_observability": p,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
