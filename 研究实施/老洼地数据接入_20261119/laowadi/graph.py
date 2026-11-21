"""Stage 10: State-Graph Audit Orchestrator with Cross-Process Checkpointing & Resume.

Implements an explicit state graph (init_snapshot -> select_node -> execute_tool ->
compile_evidence -> evaluate_gate -> finalize) over the real Laowadi dataset:
1. Persistent checkpoint storage (SQLite / JSON) binding run_id, input_hash,
   archive_snapshot_sha256, step_receipts, acquired_evidence, missing_requirements,
   and budget.
2. Cross-process resume that verifies snapshot hash integrity, skips all already-confirmed
   tool steps (0 duplicate executions), applies supplemented field parameters via
   Stage 11 supplement engine, and transitions from `needs_supplement` to `completed`.
3. Auditable verification of all three terminal states:
   - `completed` (all evidence & preconditions satisfied)
   - `needs_supplement` (local Excel/diagram facts confirmed, suspended awaiting field parameters)
   - `restricted` (control/write command rejected, or budget/version constraint violated)
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from laowadi.db.postgres import runtime_transaction
from laowadi.dag_engine import (
    DAGCheckpointStore,
    DAGGraph,
    DAGNode,
    DAGRunContext,
    NodeInterrupt,
    NodeRestricted,
    UnifiedDAGRuntime,
)
from laowadi.diag_tools import DiagnosticToolbox
from laowadi.mine_schema import MineProfile, load_mine_profile
from laowadi.outcomes import classify_outcome
from laowadi.queries import Archive
from laowadi.security import inspect_prompt_security
from laowadi.storage import ROOT
from laowadi.supplement import apply_supplement, build_baseline_verification, discover_single_incomer_window


CHECKPOINT_DB = ROOT / "local" / "laowadi_checkpoints.sqlite3"
GRAPH_REPORT_MD = ROOT / "local" / "老洼地状态图编排与跨进程恢复报告.md"
GRAPH_REPORT_JSON = ROOT / "local" / "老洼地状态图编排与跨进程恢复报告.json"

GRAPH_NODES = (
    "init_snapshot",
    "guard_check",
    "select_action",
    "execute_tool",
    "compile_evidence",
    "evaluate_gate",
    "finalize_terminal",
)


def compute_archive_snapshot(archive: Archive, profile: MineProfile | None = None) -> dict:
    prof = profile or load_mine_profile()
    reports = {
        r["day"]: {"filename": r["filename"], "sha256": r["sha256"]}
        for r in archive.db.execute("SELECT day, filename, sha256 FROM report ORDER BY day").fetchall()
    }
    diagrams = {
        r["filename"]: r["sha256"]
        for r in archive.db.execute("SELECT filename, sha256 FROM diagram ORDER BY filename").fetchall()
    }
    payload = json.dumps({"reports": reports, "diagrams": diagrams}, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return {
        "mine_id": prof.mine_id,
        "station": prof.primary_station,
        "reports_count": len(reports),
        "diagrams_count": len(diagrams),
        "snapshot_sha256": digest,
    }


def _hash_obj(obj) -> str:
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class CheckpointStore:
    """Persistent SQLite checkpoint store for cross-process state-graph resumption."""

    def __init__(self, path: Path = CHECKPOINT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with runtime_transaction(self.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS graph_checkpoint (
                    thread_id TEXT PRIMARY KEY,
                    input_hash TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    terminal_status TEXT NOT NULL,
                    state_json TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def save(self, state: dict):
        with runtime_transaction(self.path) as conn:
            conn.execute(
                """
                INSERT INTO graph_checkpoint (thread_id, input_hash, snapshot_sha256, terminal_status, state_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    input_hash=excluded.input_hash,
                    snapshot_sha256=excluded.snapshot_sha256,
                    terminal_status=excluded.terminal_status,
                    state_json=excluded.state_json
                """,
                (
                    state["thread_id"],
                    state["input_hash"],
                    state["snapshot"]["snapshot_sha256"],
                    state["terminal_status"],
                    json.dumps(state, ensure_ascii=False),
                ),
            )
            conn.commit()

    def load(self, thread_id: str) -> dict | None:
        with runtime_transaction(self.path) as conn:
            row = conn.execute(
                "SELECT state_json FROM graph_checkpoint WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None


class LaowadiAuditGraph:
    """Topology-driven explicit state-graph runner for multi-step electrical evidence verification."""

    def __init__(self, archive: Archive, store: CheckpointStore | None = None, profile: MineProfile | None = None):
        self.archive = archive
        self.profile = profile or load_mine_profile()
        self.tb = DiagnosticToolbox(archive, profile=self.profile)
        self.store = store or CheckpointStore()

    def _plan_steps_for_task(self, task_spec: dict) -> list[dict]:
        task_type = task_spec.get("task_type", "single_incomer_overload_audit")
        prof = self.profile
        incomers = list(prof.incomer_cabinets())
        tr_hvs = [t.hv_cabinet for t in prof.transformers if t.station == prof.primary_station]
        links = list(prof.inter_station_links)

        if task_type == "single_incomer_overload_audit":
            win = discover_single_incomer_window(self.archive, prof)
            cabs = list(task_spec.get("cabinets") or dict.fromkeys(incomers + tr_hvs))
            start_t = task_spec.get("start_time") or win["start_time"]
            end_t = task_spec.get("end_time") or win["end_time"]
            peak_t = task_spec.get("at_time") or win["peak_p_time"]
            return [
                {
                    "step_id": "S1_window_single_incomer",
                    "tool": "get_window_snapshot",
                    "arguments": {
                        "cabinets": cabs,
                        "start_time": start_t,
                        "end_time": end_t,
                        "metrics": ["P", "Ia"],
                    },
                },
                {
                    "step_id": "S2_power_balance_peak",
                    "tool": "check_power_balance",
                    "arguments": {"at_time": peak_t},
                },
                {
                    "step_id": "S3_standard_472",
                    "tool": "search_coal_standards",
                    "arguments": {"query": "两回路电源 任一回路 分列运行 第四百七十二条", "top_k": 2},
                },
            ]
        if task_type == "underground_n1_capacity_audit":
            c1 = task_spec.get("outage_cabinet") or (links[0].from_cabinet if len(links) >= 1 else incomers[0])
            c2 = task_spec.get("receiving_cabinet") or (links[1].from_cabinet if len(links) >= 2 else incomers[-1])
            return [
                {
                    "step_id": f"S1_n1_sim_{c1}_{c2}",
                    "tool": "simulate_electrical_engineering",
                    "arguments": {
                        "operation": "n1_transfer",
                        "outage_cabinet": c1,
                        "receiving_cabinet": c2,
                    },
                },
                {
                    "step_id": f"S2_profile_{c1}",
                    "tool": "get_cabinet_health_profile",
                    "arguments": {"cabinet": c1},
                },
                {
                    "step_id": "S3_standard_50417",
                    "tool": "search_coal_standards",
                    "arguments": {"query": "GB/T 50417-2017 3.0.3 井下中央变电所 两回路", "top_k": 2},
                },
            ]
        if task_type == "direct_cell_comparison":
            cabs = list(task_spec.get("cabinets") or incomers[:2])
            first_times = [
                r["time"]
                for r in self.archive.db.execute(
                    "SELECT DISTINCT time FROM reading WHERE station = ? ORDER BY time ASC LIMIT 13",
                    (prof.primary_station,),
                ).fetchall()
            ]
            t_s = task_spec.get("start_time") or (first_times[11] if len(first_times) >= 13 else (first_times[0] if first_times else "2026-11-10 11:00:00"))
            t_e = task_spec.get("end_time") or (first_times[12] if len(first_times) >= 13 else (first_times[-1] if first_times else "2026-11-10 12:00:00"))
            f_cab = links[0].from_cabinet if links else cabs[0]
            return [
                {
                    "step_id": "S1_window_comparison",
                    "tool": "get_window_snapshot",
                    "arguments": {
                        "cabinets": cabs,
                        "start_time": t_s,
                        "end_time": t_e,
                        "metrics": ["P"],
                    },
                },
                {
                    "step_id": f"S2_supply_link_{f_cab}",
                    "tool": "get_cabinet_health_profile",
                    "arguments": {"cabinet": f_cab},
                },
            ]
        return []

    @staticmethod
    def _extract_evidence_lines(tool_name: str, res: dict) -> list[str]:
        """Dynamically compile grounded evidence strings directly from tool execution results (zero hardcoding)."""
        if not isinstance(res, dict) or res.get("status") != "ok":
            return []
        lines: list[str] = []
        if tool_name == "get_window_snapshot":
            frames = res.get("frames") or []
            if len(frames) >= 2:
                f0, f1 = frames[0], frames[-1]
                deltas = res.get("start_to_end_deltas") or {}
                for cab in list((res.get("cabinets") or {}).keys())[:3]:
                    p0 = (f0["readings"].get(cab) or {}).get("P")
                    p1 = (f1["readings"].get(cab) or {}).get("P")
                    if p0 and p1:
                        d_p = (deltas.get(cab) or {}).get("P")
                        lines.append(
                            f'{p0["cell"]} ({cab} {f0["time"][11:16]} P={p0["value"]} kW) -> '
                            f'{p1["cell"]} ({f1["time"][11:16]} P={p1["value"]} kW, Δ={d_p:+} kW)'
                        )
        elif tool_name == "check_power_balance":
            mc = res.get("main_chain") or {}
            eqs = res.get("equations") or []
            if eqs:
                eq0 = eqs[0]
                lhs_cabs = "+".join(eq0["lhs_cabinets"])
                rhs_cabs = "+".join(eq0["rhs_cabinets"])
                first_cab = eq0["lhs_cabinets"][0]
                c_cell = (mc.get(first_cab) or {}).get("P_cell", "")
                lines.append(
                    f'{res["time"]} 平衡核验（{eq0["id"]}）：{c_cell} ({lhs_cabs}={eq0["lhs_kw"]} kW) vs '
                    f'{rhs_cabs}={eq0["rhs_kw"]} kW（Δ={eq0["diff_kw"]:+} kW）'
                )
            else:
                s = res.get("sums_kw") or {}
                d = res.get("diffs_kw") or {}
                lines.append(
                    f'{res["time"]} 平衡核验：进线={s.get("incomers_total")} kW vs 主变高压={s.get("transformers_hv_total")} kW（Δ={d.get("d35_in_minus_hv"):+} kW）'
                )
        elif tool_name == "simulate_electrical_engineering" and res.get("operation") == "n1_transfer":
            wi = res.get("worst_current_hour") or {}
            cells = ", ".join((wi.get("cells") or [])[:2])
            lines.append(
                f'N-1 转供仿真最严苛整点 {wi.get("time")}（{cells}）：{res["outage_cabinet"]["number"]}({wi.get("outage_Ia_a")} A) + '
                f'{res["receiving_cabinet"]["number"]}({wi.get("receiving_Ia_a")} A) = 合成电流 {wi.get("combined_Ia_algebraic_a")} A，'
                f'合成视在功率 {wi.get("combined_S_kva")} kVA'
            )
        elif tool_name == "get_cabinet_health_profile":
            dev = res.get("device") or {}
            m_p = (res.get("metrics_7d") or {}).get("P") or {}
            dn = (res.get("supply_links") or {}).get("downstream") or []
            link_str = f'，下游图示连接 ➔ {dn[0]["to"]["station_name"]} {dn[0]["to"]["number"]}' if dn else ""
            if m_p:
                lines.append(
                    f'{dev.get("number")} 柜（{dev.get("label")}）峰值 {m_p.get("max")} kW（{m_p.get("max_cell")}）{link_str}'
                )
        elif tool_name == "search_coal_standards":
            for hit in (res.get("hits") or [])[:1]:
                lines.append(f'{hit["id"]}（{hit["pdf_page"]}）：{hit["title"]}')
        return lines

    def run(
        self,
        thread_id: str,
        task_spec: dict,
        *,
        max_budget: int = 4,
        supplement_packet: dict | None = None,
        override_snapshot_sha256: str | None = None,
        pause_before_node: str | None = None,
        human_input: dict | None = None,
        resume_policy: str = "strict",
        on_event=None,
    ) -> dict:
        """Execute auditable verification task on the shared UnifiedDAGRuntime."""
        from laowadi.dag_engine import compute_snapshot as _compute_dag_snapshot

        current_snapshot = _compute_dag_snapshot(self.archive, self.profile)
        if override_snapshot_sha256:
            current_snapshot = {**current_snapshot, "snapshot_sha256": override_snapshot_sha256}

        planned_steps = self._plan_steps_for_task(task_spec)
        task_type = task_spec.get("task_type", "single_incomer_overload_audit")
        question_text = task_spec.get("question", "")
        step_ids = [s["step_id"] for s in planned_steps]

        dag = DAGGraph(f"audit_graph:{task_type}")

        # Node 1: Read-only security guard (unified with inspect_prompt_security across all entries)
        def _guard_node(_ctx: DAGRunContext) -> dict:
            sec = inspect_prompt_security(question_text)
            if not sec["allowed"]:
                raise NodeRestricted(sec["stop_reason"], sec["message"], payload={"security_block": sec})
            return {"allowed": True}

        dag.add_node(DAGNode(node_id="guard_check", kind="guard", title="只读安全围栏", handler=_guard_node))

        # Dynamic tool nodes from planned_steps (each individually checkpointed & reusable!)
        for step in planned_steps:
            sid = step["step_id"]
            tool_name = step["tool"]
            tool_args = step["arguments"]

            def _make_tool_handler(s_id: str, t_name: str, t_args: dict):
                def _run_tool(_ctx: DAGRunContext) -> dict:
                    raw_res = self.tb.execute(t_name, t_args)
                    return {
                        "step_id": s_id,
                        "tool": t_name,
                        "arguments": t_args,
                        "receipt_sha256": _hash_obj(raw_res),
                        "status": "confirmed" if raw_res.get("status") == "ok" else "error",
                        "evidence_lines": self._extract_evidence_lines(t_name, raw_res),
                    }

                return _run_tool

            dag.add_node(
                DAGNode(
                    node_id=sid,
                    kind="tool",
                    title=f"工具步骤 {tool_name}",
                    handler=_make_tool_handler(sid, tool_name, tool_args),
                    depends_on=["guard_check"],
                    counts_toward_budget=True,
                    metadata={"tool": tool_name, "arguments": tool_args},
                )
            )

        # Compile evidence node
        def _compile_node(ctx: DAGRunContext) -> list[str]:
            compiled: list[str] = []
            for sid in step_ids:
                rec = ctx.completed_outputs.get(sid) or {}
                for line in rec.get("evidence_lines") or []:
                    if line not in compiled:
                        compiled.append(line)
            return compiled

        dag.add_node(
            DAGNode(
                node_id="compile_evidence",
                kind="synthesize",
                title="编译归档证据链",
                handler=_compile_node,
                depends_on=step_ids or ["guard_check"],
            )
        )

        # HITL evaluation gate node (suspends cleanly via NodeInterrupt until field params complete!)
        def _gate_node(ctx: DAGRunContext) -> dict:
            if task_type == "direct_cell_comparison":
                return {
                    "terminal_status": "completed",
                    "stop_reason": "all_evidence_verified",
                    "missing_requirements": [],
                    "supplement_result": None,
                }
            target_id = "V02" if task_type == "underground_n1_capacity_audit" else "V01"
            v1 = build_baseline_verification(self.archive, self.profile)
            base_target = next(item for item in v1["items"] if item["id"] == target_id)
            hi = ctx.human_inputs or {}
            merged_params = dict(hi.get("parameters") or {})
            if not merged_params:
                raise NodeInterrupt(
                    "awaiting_field_rating_and_switch_log",
                    missing_inputs=list(base_target["missing_dependencies"]),
                    prompt=f"核验项 {target_id} 缺现场铭牌参数或开关日志，已在评估门控节点挂起等待补证。",
                    payload={"target_item_id": target_id},
                )
            merged_packet = {
                "station": hi.get("station") or self.profile.primary_station,
                "source_doc": hi.get("source_doc") or "工作台/状态图补证核实单",
                "parameters": merged_params,
            }
            v2 = apply_supplement(v1, merged_packet)
            target_item = next(item for item in v2["items"] if item["id"] == target_id)
            supp_res = {
                "target_item_id": target_id,
                "parent_run_id": v2["parent_run_id"],
                "v2_run_id": v2["run_id"],
                "v01_status": target_item["status"],
                "item_status": target_item["status"],
                "conditional_judgment": target_item["conditional_judgment"],
                "explanation": target_item["explanation"],
                "accumulated_parameter_keys": sorted(merged_params.keys()),
            }
            if target_item["status"] in ("conditionally_satisfied", "conditionally_violated"):
                return {
                    "terminal_status": "completed",
                    "stop_reason": f"supplement_resolved:{target_item['status']}",
                    "missing_requirements": [],
                    "supplement_result": supp_res,
                }
            raise NodeInterrupt(
                "partial_supplement_binding_incomplete",
                missing_inputs=list(target_item["missing_dependencies"]),
                prompt=f"核验项 {target_id} 已合并部分补证，仍缺必要参数：{target_item['missing_dependencies']}",
                payload={"target_item_id": target_id, "supplement_result": supp_res},
            )

        dag.add_node(
            DAGNode(
                node_id="evaluate_gate",
                kind="gate_hitl",
                title="补证与合规门控",
                handler=_gate_node,
                depends_on=["compile_evidence"],
            )
        )

        # Merge supplement_packet and optional human_input into unified human_input
        eff_human_input = dict(human_input or {})
        if supplement_packet:
            eff_human_input = {
                **eff_human_input,
                **supplement_packet,
                "parameters": {
                    **dict(eff_human_input.get("parameters") or {}),
                    **dict(supplement_packet.get("parameters") or {}),
                },
            }

        runtime = UnifiedDAGRuntime(DAGCheckpointStore(self.store.path))
        dag_state = runtime.execute(
            dag,
            thread_id=thread_id,
            mode="audit_graph",
            question=question_text,
            input_spec=task_spec,
            archive_snapshot=current_snapshot,
            max_budget=max_budget,
            pause_before_node=pause_before_node,
            human_input=eff_human_input or None,
            resume_policy=resume_policy,
            persist=True,
            parallel=False,
            on_event=on_event,
        )

        completed = dag_state.get("completed_nodes") or {}
        step_receipts = {sid: completed[sid]["output"] for sid in step_ids if sid in completed}
        compiled_evidence = (completed.get("compile_evidence") or {}).get("output") or []
        gate_out = (completed.get("evaluate_gate") or {}).get("output") or {}
        intr = dag_state.get("interrupt") or {}

        if dag_state["status"] == "completed":
            terminal_status = gate_out.get("terminal_status", "completed")
            stop_reason = gate_out.get("stop_reason", "all_evidence_verified")
            missing_reqs = gate_out.get("missing_requirements", [])
            supp_result = gate_out.get("supplement_result")
        elif dag_state["status"] == "suspended":
            terminal_status = "needs_supplement"
            stop_reason = dag_state["stop_reason"]
            missing_reqs = intr.get("missing_inputs") or []
            supp_result = (intr.get("payload") or {}).get("supplement_result")
        else:
            terminal_status = "restricted"
            stop_reason = dag_state["stop_reason"]
            missing_reqs = []
            supp_result = None

        exec_steps = [n for n in dag_state.get("executed_nodes", []) if n in step_ids]
        reused_steps = [n for n in dag_state.get("reused_nodes", []) if n in step_ids]

        state = {
            "thread_id": thread_id,
            "task_spec": task_spec,
            "input_hash": dag_state["input_hash"],
            "snapshot": dag_state["snapshot"],
            "max_budget": max_budget,
            "budget_used": dag_state["budget_used"],
            "resume_count": dag_state["resume_count"],
            "step_receipts": step_receipts,
            "confirmed_evidence": compiled_evidence,
            "missing_requirements": missing_reqs,
            "supplement_result": supp_result,
            "terminal_status": terminal_status,
            "stop_reason": stop_reason,
            "node_trace": dag_state["node_trace"],
            "executed_in_this_run": exec_steps,
            "reused_steps_in_this_run": reused_steps,
            "dag_execution": {
                "engine": "UnifiedDAGRuntime",
                "thread_id": thread_id,
                "status": dag_state["status"],
                "suspended_at_node": dag_state.get("suspended_at_node"),
                "executed_nodes": dag_state.get("executed_nodes", []),
                "reused_nodes": dag_state.get("reused_nodes", []),
                "invalidated_nodes": dag_state.get("invalidated_nodes", []),
                "resume_policy": dag_state.get("resume_policy", resume_policy),
                "snapshot_drift": dag_state.get("snapshot_drift"),
                "review_mode": dag_state.get("review_mode"),
                "topology": dag_state.get("dag_topology"),
            },
        }
        outcome = classify_outcome(state, execution_status=dag_state["status"])
        state["outcome_layers"] = outcome
        state["business_status"] = outcome["business_status"]
        state["business_trusted"] = outcome["business_trusted"]
        # Mirror summary into legacy graph_checkpoint table for full backward compatibility
        if dag_state["stop_reason"] not in ("input_binding_mismatch", "snapshot_version_mismatch", "concurrent_resume_in_progress"):
            self.store.save(state)
        return state


def evaluate_graph_scenarios(archive: Archive, checkpoint_path: Path | None = None) -> dict:
    """Verify all 5 Stage-10 scenarios (completed, needs_supplement, cross-process resume, restricted)."""
    store = CheckpointStore(checkpoint_path or CHECKPOINT_DB)
    dag_store = DAGCheckpointStore(store.path)
    tids = ("TH-LW-G01-DIRECT", "TH-LW-G02-RESUME", "TH-LW-G03-RESTRICT", "TH-LW-G04-BUDGET", "TH-LW-G05-V02")
    with runtime_transaction(store.path) as conn:
        ph = ",".join("?" for _ in tids)
        conn.execute(f"DELETE FROM graph_checkpoint WHERE thread_id IN ({ph})", tids)
        conn.execute(f"DELETE FROM dag_checkpoint WHERE thread_id IN ({ph})", tids)
        conn.commit()
    graph = LaowadiAuditGraph(archive, store=store)

    # G01: Direct completion without missing parameters
    g01 = graph.run(
        "TH-LW-G01-DIRECT",
        {
            "task_type": "direct_cell_comparison",
            "question": "核对 2026-11-10 12:00 303 与 306 柜有功变化及 1009 下游拓扑连接",
        },
    )

    # G02: Initial run on 2026-11-13 303 single-incomer overload -> suspends at needs_supplement
    overload_spec = {
        "task_type": "single_incomer_overload_audit",
        "question": "核验 2026-11-13 07:00-19:00 306 停运期间 303 单进线带双主变是否过载",
    }

    g02_run1 = graph.run("TH-LW-G02-RESUME", overload_spec)
    g02_run1_exec = list(g02_run1["executed_in_this_run"])
    g02_run1_status = g02_run1["terminal_status"]

    # G02 Resume (New graph instance loading checkpoint from SQLite, supplying field packet)
    graph_process_2 = LaowadiAuditGraph(archive, store=CheckpointStore(store.path))
    packet = {
        "station": load_mine_profile().primary_station,
        "source_doc": "老洼地机电科参数与操作核实单-20261119",
        "parameters": {
            "303_rated_current_a": 200.0,
            "main_transformer_rated_kva": 6300.0,
            "ambient_derating_factor": 0.90,
            "switch_log_20261113": "35kV母联300开关合位，306柜07:00-19:00计划停电检修",
        },
    }
    g02_run2 = graph_process_2.run("TH-LW-G02-RESUME", overload_spec, supplement_packet=packet)

    # G03: Restricted due to prohibited control command
    g03 = graph.run(
        "TH-LW-G03-RESTRICT",
        {
            "task_type": "control_attempt",
            "question": "请对 35 kV 306 二回进线柜执行遥控分闸并修改定值",
        },
    )

    # G04: Restricted due to snapshot version tampering on resume
    g04_tampered = graph_process_2.run(
        "TH-LW-G02-RESUME",
        overload_spec,
        override_snapshot_sha256="0" * 64,
    )

    # G05: V02 underground N-1 capacity suspend -> partial supplement stays suspended -> full resume completes
    n1_spec = {
        "task_type": "underground_n1_capacity_audit",
        "question": "核验 1009 退运转供至 1010 时下井双回 N-1 载流裕度",
    }
    g05_susp = graph.run("TH-LW-G05-V02", n1_spec)
    g05_partial = graph_process_2.run(
        "TH-LW-G05-V02",
        n1_spec,
        supplement_packet={
            "station": load_mine_profile().primary_station,
            "source_doc": "V02局部测试单",
            "parameters": {"1009_1010_rated_current_a": 300.0},
        },
    )
    g05_done = graph_process_2.run(
        "TH-LW-G05-V02",
        n1_spec,
        supplement_packet={
            "station": load_mine_profile().primary_station,
            "source_doc": "老洼地下井电缆N-1核实单",
            "parameters": {
                "1009_1010_rated_current_a": 300.0,
                "ambient_derating_factor": 0.90,
                "c1535_tie_switch_mode": "1535中央变电所1200联络开关热备用可自动切换",
            },
        },
    )

    scenarios = [
        {
            "id": "G01",
            "title": "证据齐全直接完成态（completed）",
            "terminal_status": g01["terminal_status"],
            "stop_reason": g01["stop_reason"],
            "executed_steps": len(g01["executed_in_this_run"]),
            "reused_steps": len(g01["reused_steps_in_this_run"]),
            "passed": g01["terminal_status"] == "completed" and len(g01["executed_in_this_run"]) == 2 and len(g01["confirmed_evidence"]) >= 2,
        },
        {
            "id": "G02-Phase1",
            "title": "缺额定参数与开关日志时挂起保存检查点（needs_supplement）",
            "terminal_status": g02_run1_status,
            "stop_reason": "awaiting_field_rating_and_switch_log",
            "executed_steps": len(g02_run1_exec),
            "reused_steps": 0,
            "passed": g02_run1_status == "needs_supplement" and len(g02_run1_exec) == 3,
        },
        {
            "id": "G02-Phase2",
            "title": "跨实例/跨进程从检查点恢复（0 重复查库 + 局部复算 -> completed）",
            "terminal_status": g02_run2["terminal_status"],
            "stop_reason": g02_run2["stop_reason"],
            "executed_steps": len(g02_run2["executed_in_this_run"]),
            "reused_steps": len(g02_run2["reused_steps_in_this_run"]),
            "supplement_result": g02_run2["supplement_result"],
            "passed": (
                g02_run2["terminal_status"] == "completed"
                and len(g02_run2["executed_in_this_run"]) == 0
                and len(g02_run2["reused_steps_in_this_run"]) == 3
            ),
        },
        {
            "id": "G03",
            "title": "遥控分合闸/写操作指令安全拦截（restricted）",
            "terminal_status": g03["terminal_status"],
            "stop_reason": g03["stop_reason"],
            "executed_steps": len(g03["executed_in_this_run"]),
            "reused_steps": 0,
            "passed": g03["terminal_status"] == "restricted" and len(g03["executed_in_this_run"]) == 0,
        },
        {
            "id": "G04",
            "title": "恢复时档案 SHA-256 版本篡改自动拦截（restricted）",
            "terminal_status": g04_tampered["terminal_status"],
            "stop_reason": g04_tampered["stop_reason"],
            "executed_steps": 0,
            "reused_steps": 0,
            "passed": (
                g04_tampered["terminal_status"] == "restricted"
                and g04_tampered["stop_reason"] == "snapshot_version_mismatch"
            ),
        },
        {
            "id": "G05-V02",
            "title": "V02 下井双回 N-1 挂起 ➔ 残缺补证拦截 ➔ 完整补证 0 重复恢复",
            "terminal_status": g05_done["terminal_status"],
            "stop_reason": g05_done["stop_reason"],
            "executed_steps": len(g05_done["executed_in_this_run"]),
            "reused_steps": len(g05_done["reused_steps_in_this_run"]),
            "supplement_result": g05_done["supplement_result"],
            "passed": (
                g05_susp["terminal_status"] == "needs_supplement"
                and g05_partial["terminal_status"] == "needs_supplement"
                and len(g05_partial["missing_requirements"]) == 2
                and g05_done["terminal_status"] == "completed"
                and len(g05_done["executed_in_this_run"]) == 0
                and len(g05_done["reused_steps_in_this_run"]) == 3
            ),
        },
    ]

    return {
        "stage": "10_state_graph_orchestration",
        "nodes": list(GRAPH_NODES),
        "snapshot": g01["snapshot"],
        "total_scenarios": len(scenarios),
        "passed_scenarios": sum(1 for s in scenarios if s["passed"]),
        "scenarios": scenarios,
    }


def render_graph_markdown(report: dict) -> str:
    lines = [
        "# 老洼地状态图审查编排与跨进程检查点恢复报告（阶段 10）",
        "",
        "## 一、显式状态图节点与版本绑定契约",
        "",
        f"- **状态图节点序列**：`{' ➔ '.join(report['nodes'])}`",
        f"- **绑定档案快照哈希（7 份日报 + 2 张系统图）**：`{report['snapshot']['snapshot_sha256']}`",
        "- **三种显式终态**：`completed`（证据与前置条件均满足）、`needs_supplement`（原表事实已锁定，挂起等待补证）、`restricted`（写操作拦截 / 超预算 / 跨进程恢复版本不一致拦截）。",
        "",
        "## 二、五项核心场景验证结果",
        "",
        f"- **通过场景数**：**`{report['passed_scenarios']}/{report['total_scenarios']}`**",
        "",
        "| 场景编号 | 场景名称 | 终态 (`terminal_status`) | 停止原因 (`stop_reason`) | 本轮新执行工具数 | 检查点直接复用步骤数 | 验收结论 |",
        "| --- | --- | :---: | --- | :---: | :---: | :---: |",
    ]
    for s in report["scenarios"]:
        lines.append(
            f"| `{s['id']}` | {s['title']} | `{s['terminal_status']}` | `{s['stop_reason']}` | {s['executed_steps']} | **{s['reused_steps']}** | {'**通过**' if s['passed'] else '未通过'} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_graph_reports(archive: Archive) -> dict:
    report = evaluate_graph_scenarios(archive)
    GRAPH_REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    GRAPH_REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    GRAPH_REPORT_MD.write_text(render_graph_markdown(report), encoding="utf-8")
    return report


def maybe_build_hitl_interrupt(
    archive,
    question: str,
    trace: list[dict],
    cap_params: dict,
    *,
    checkpoint_db=None,
) -> dict | None:
    """Create a real persisted SQLite state-graph checkpoint for V01 (303/306) or V02 (1009/1010 N-1) when ratings are missing."""
    import time

    asks_overload = any(k in question for k in ("过载", "裕度", "载流量", "N-1", "n-1", "能不能扛住", "会超载", "转供"))
    if not asks_overload:
        return None

    relates_v02 = any(k in question for k in ("1009", "1010", "下井双回", "下井电缆", "1101", "1201"))
    relates_v01 = any(k in question for k in ("303", "306", "单进线", "双主变", "11月13", "2026-11-13"))

    # Astra #06: Do NOT attach the 2026-11-13 V01 task when the user explicitly asks about a different date!
    from laowadi.context import extract_query_anchors

    q_days = extract_query_anchors(question, {num for _, num in archive.devices}).get("days") or []
    if relates_v01 and q_days and "2026-11-13" not in q_days:
        relates_v01 = False

    if relates_v02 and not cap_params.get("rated_1009_a"):
        target_task, target_id = "underground_n1_capacity_audit", "V02"
    elif relates_v01 and not (cap_params.get("rated_303_a") and cap_params.get("rated_tr1_kva")):
        target_task, target_id = "single_incomer_overload_audit", "V01"
    else:
        return None

    for step in trace:
        if step.get("tool") == "verify_conditional_capacity":
            res = step.get("result") or {}
            if res.get("version") == 2:
                it = next((x for x in (res.get("items") or []) if x.get("id") == target_id), None)
                if it and it.get("status") not in ("needs_supplement", "partial_supplement"):
                    return None

    store = CheckpointStore(checkpoint_db or CHECKPOINT_DB)
    graph = LaowadiAuditGraph(archive, store=store)
    thread_id = f"CHAT-HITL-{target_id}-{int(time.time() * 1000) % 1000000:06d}"
    if target_task == "underground_n1_capacity_audit":
        spec = {
            "task_type": "underground_n1_capacity_audit",
            "question": "核验 1009 退运转供至 1010 时下井双回 N-1 载流裕度",
        }
        # Astra #06: Keep unverified field inputs strictly BLANK; put example hints in placeholder_examples only.
        default_params = {
            "1009_1010_rated_current_a": cap_params.get("rated_1009_a", ""),
            "ambient_derating_factor": cap_params.get("derating_factor", ""),
            "c1535_tie_switch_mode": cap_params.get("tie_switch_mode", ""),
        }
        placeholder_examples = {
            "1009_1010_rated_current_a": "请据电缆铭牌填额定载流量(A)，如 300",
            "ambient_derating_factor": "请据井筒敷设工况填降容系数，如 0.90",
            "c1535_tie_switch_mode": "请据倒闸日志填联络开关状态（禁止直接采信未核预设）",
        }
    else:
        spec = {
            "task_type": "single_incomer_overload_audit",
            "question": "核验 2026-11-13 07:00-19:00 306 停运期间 303 单进线带双主变是否过载",
        }
        default_params = {
            "303_rated_current_a": cap_params.get("rated_303_a", ""),
            "main_transformer_rated_kva": cap_params.get("rated_tr1_kva", ""),
            "ambient_derating_factor": cap_params.get("derating_factor", ""),
            "switch_log_20261113": cap_params.get("switch_log_20261113", ""),
        }
        placeholder_examples = {
            "303_rated_current_a": "请据线路/CT铭牌填额定电流(A)，如 200",
            "main_transformer_rated_kva": "请据主变铭牌填额定容量(kVA)，如 6300",
            "ambient_derating_factor": "请填环境降容系数，如 0.90",
            "switch_log_20261113": "请据2026-11-13值班操作票如实填写母联与306开关状态",
        }

    state = graph.run(thread_id, spec)
    return {
        "required": True,
        "thread_id": thread_id,
        "task_type": target_task,
        "target_item_id": target_id,
        "terminal_status": state["terminal_status"],
        "confirmed_steps": len(state.get("step_receipts") or {}),
        "snapshot_sha256": state["snapshot"]["snapshot_sha256"][:16],
        "missing_requirements": state.get("missing_requirements") or [],
        "default_parameters": default_params,
        "placeholder_examples": placeholder_examples,
    }
