"""Verification suite for Sprint 2: Persistent Operational Memory Store & Deep Investigation Engine."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from laowadi.autonomous_agent import run_autonomous_agent
from laowadi.deep_research import (
    export_deep_report,
    get_deep_report,
    list_deep_reports,
    run_deep_investigation,
)
from laowadi.memory_store import (
    MemoryStore,
    create_memory,
    delete_memory,
    extract_custom_voltage_thresholds,
    get_memory,
    list_memories,
    retrieve_relevant_memories,
    update_memory,
)
from laowadi.monitor import EVENT_DB
from laowadi.queries import Archive
from laowadi.storage import DB, connect


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_memory_store(archive: Archive, tmp: Path) -> dict:
    mem_db = tmp / "memory.sqlite3"
    assert list_memories(db_path=mem_db) == []

    # 1. Create semantic, episodic, procedural memories
    m_sem = create_memory(
        category="semantic",
        title="10 kV II 段内部严控红线 10.65 kV",
        content="本矿机电科规定 10 kV II 段（1006 柜）内部考核电压上限为 10.65 kV，早于国标 10.70 kV 预警。",
        scope_cabinet="1006",
        metric_threshold={"metric": "Uab", "operator": ">", "value": 10.65, "unit": "kV"},
        tags=["过压红线", "1006"],
        db_path=mem_db,
    )
    m_epi = create_memory(
        category="episodic",
        title="1018 柜现场二次极性复核记录",
        content="前期排查确认 1018 柜存在持续负有功与负功率因数，优先核校 CT/PT 二次接线极性而非反送电。",
        scope_cabinet="1018",
        tags=["极性", "1018"],
        db_path=mem_db,
    )
    m_pro = create_memory(
        category="procedural",
        title="主变分接头调档前 3 步必核 SOP",
        content="1) 核对现场实际挡位铭牌；2) 用 pandapower 复算目标母线电压；3) 履行人工倒闸操作票审批。",
        tags=["SOP", "调压"],
        db_path=mem_db,
    )

    assert len(list_memories(db_path=mem_db)) == 3
    assert len(list_memories(category="semantic", db_path=mem_db)) == 1
    assert get_memory(m_sem["id"], db_path=mem_db)["title"] == m_sem["title"]

    # 2. Custom voltage threshold extraction
    thrs = extract_custom_voltage_thresholds([m_sem, m_epi, m_pro])
    assert len(thrs) == 1 and thrs[0]["value_kv"] == 10.65 and thrs[0]["scope_cabinet"] == "1006"

    # 3. Retrieval + hit_count tracking + autonomous_agent integration
    hits = retrieve_relevant_memories("请分析 1006 柜过压和分接头调压方案", cabinets=["1006"], db_path=mem_db)
    assert any(h["id"] == m_sem["id"] for h in hits)
    assert get_memory(m_sem["id"], db_path=mem_db)["hit_count"] >= 1

    ag_res = run_autonomous_agent(
        archive,
        "请分析这 7 天 1006 柜功率因数与母线电压的相关性回归并画散点图",
        memory_db_path=mem_db,
    )
    assert ag_res["status"] == "ok"
    assert any(m["id"] == m_sem["id"] for m in ag_res["applied_memories"])
    assert "10.65 kV" in ag_res["answer_markdown"]

    # 4. Disable & delete lifecycle
    upd = update_memory(m_epi["id"], enabled=False, db_path=mem_db)
    assert upd is not None and upd["enabled"] is False
    assert len(list_memories(enabled_only=True, db_path=mem_db)) == 2
    assert delete_memory(m_epi["id"], db_path=mem_db) is True
    assert get_memory(m_epi["id"], db_path=mem_db) is None

    # 5. MemoryStore wrapper parity
    store = MemoryStore(mem_db)
    assert len(store.list()) == 2
    return {"created_and_verified": 3, "custom_threshold_kv": thrs[0]["value_kv"], "autonomous_injected": True}


def verify_deep_research(archive: Archive, tmp: Path) -> dict:
    mem_db = tmp / "memory.sqlite3"
    rep_dir = tmp / "deep_reports"
    events: list[dict] = []

    report = run_deep_investigation(
        archive,
        "全面排查老洼地 7 天所有供电隐患，重点核查 1006 过压、反向有功、N-1 转供与 VoI 补证优先级",
        memory_db_path=mem_db,
        reports_dir=rep_dir,
        on_event=events.append,
    )
    ev_types = [e["type"] for e in events]
    for needed in ("plan_update", "stage_update", "reasoning_delta", "finding", "section_ready", "done"):
        assert needed in ev_types, f"Missing deep-research SSE event {needed}"

    assert report["status"] == "ok"
    assert len(report["sections"]) == 6
    assert len(report["findings"]) >= 6
    # Verify custom 10.65 kV operational memory threshold actually ran a live SQL count finding!
    assert any(f["id"].startswith("F-MEM-") and f["metrics"]["threshold_kv"] == 10.65 and f["metrics"]["exceed_hours"] > 0 for f in report["findings"])
    assert report["kpis"]["baseline_overvoltage_hours"] == 35
    assert report["kpis"]["optimized_overvoltage_hours"] == 0
    assert report["kpis"]["weekly_loss_saving_kwh"] > 100.0
    assert len(report["evidence_cells"]) >= 4

    # Persistence & listing & export
    listed = list_deep_reports(reports_dir=rep_dir)
    assert len(listed) == 1 and listed[0]["report_id"] == report["report_id"]
    loaded = get_deep_report(report["report_id"], reports_dir=rep_dir)
    assert loaded is not None and loaded["report_id"] == report["report_id"]

    md_text, md_mime, md_name = export_deep_report(report["report_id"], fmt="md", reports_dir=rep_dir)
    assert "text/markdown" in md_mime and md_name.endswith(".md") and "MineGrid-Agent" in md_text

    html_text, html_mime, html_name = export_deep_report(report["report_id"], fmt="html", reports_dir=rep_dir)
    assert "text/html" in html_mime and html_name.endswith(".html") and "@media print" in html_text

    # Path traversal guard
    try:
        export_deep_report("../etc/passwd", fmt="md", reports_dir=rep_dir)
        raise AssertionError("Path traversal report_id should be rejected")
    except ValueError:
        pass

    return {
        "report_id": report["report_id"],
        "sections": len(report["sections"]),
        "findings": len(report["findings"]),
        "evidence_cells": len(report["evidence_cells"]),
        "custom_threshold_checked": True,
        "md_and_html_export_ok": True,
    }


def main():
    before_db = sha256(DB)
    before_ev = sha256(EVENT_DB)
    with TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        with closing(connect(DB)) as db:
            archive = Archive(db, EVENT_DB)
            mem_res = verify_memory_store(archive, tmp)
            dr_res = verify_deep_research(archive, tmp)
    assert sha256(DB) == before_db and sha256(EVENT_DB) == before_ev, "Golden archives mutated!"
    print(
        json.dumps(
            {
                "status": "ok",
                "archives_unchanged": True,
                "memory_store": mem_res,
                "deep_research": dr_res,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
