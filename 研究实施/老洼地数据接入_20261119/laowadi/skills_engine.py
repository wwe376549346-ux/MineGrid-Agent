"""Pluggable Markdown Domain Skills Engine & Read-Only MCP Client Registry.

Allows engineers to drop or author Markdown playbooks under `skills/*.md` that hot-load
into the operator workbench and execute real read-only telemetry / pandapower checks,
while also supporting registration & tool discovery for read-only MCP servers.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit
import yaml

from laowadi.diag_tools import DiagnosticToolbox
from laowadi.mcp_server import ReadOnlyMCPServer
from laowadi.powerflow import analyze_parameter_sensitivity_voi, run_ac_power_flow
from laowadi.storage import ROOT


SKILLS_DIR = ROOT / "skills"
MCP_CLIENTS_FILE = ROOT / "local" / "laowadi_mcp_clients.json"
_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,47}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _skills_root(skills_dir: Path | None = None) -> Path:
    root = Path(skills_dir) if skills_dir is not None else SKILLS_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _parse_skill_file(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    meta: dict[str, Any] = {}
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            try:
                parsed = yaml.safe_load(parts[1]) or {}
                if isinstance(parsed, dict):
                    meta = parsed
            except Exception:
                meta = {}
            body = parts[2].strip()
    sid = str(meta.get("id") or path.stem).strip().lower()
    title = str(meta.get("title") or sid).strip()
    triggers = meta.get("triggers") or []
    if isinstance(triggers, str):
        triggers = [t.strip() for t in re.split(r"[,，;；\s]+", triggers) if t.strip()]
    steps = [
        line.strip()
        for line in body.splitlines()
        if re.match(r"^\d+\.\s+", line.strip())
    ]
    return {
        "id": sid,
        "title": title,
        "triggers": [str(t) for t in triggers][:12],
        "default_cabinet": str(meta.get("default_cabinet") or "1006"),
        "action_kind": str(meta.get("action_kind") or "switching_order"),
        "steps": steps,
        "body_markdown": body,
        "filename": path.name,
        "builtin": sid in {"main_fan_switchover", "storm_lightning_patrol", "tap_change_precheck"},
    }


def list_skills(*, skills_dir: Path | None = None) -> list[dict[str, Any]]:
    root = _skills_root(skills_dir)
    items: list[dict[str, Any]] = []
    for p in sorted(root.glob("*.md")):
        try:
            items.append(_parse_skill_file(p))
        except Exception:
            continue
    return items


def create_skill(
    *,
    skill_id: str,
    title: str,
    triggers: list[str] | str | None = None,
    body_markdown: str,
    default_cabinet: str = "1006",
    action_kind: str = "field_verification",
    skills_dir: Path | None = None,
) -> dict[str, Any]:
    sid = str(skill_id or "").strip().lower()
    if not _SKILL_ID_RE.match(sid):
        raise ValueError("技能 ID 须为 2–48 位小写字母、数字、下划线或短横线（如 shift_voltage_check）。")
    clean_title = str(title or "").strip()[:120]
    clean_body = str(body_markdown or "").strip()[:8000]
    if not clean_title or not clean_body:
        raise ValueError("技能标题与 Markdown 正文不能为空。")
    if isinstance(triggers, str):
        trig_list = [t.strip()[:32] for t in re.split(r"[,，;；\s]+", triggers) if t.strip()]
    else:
        trig_list = [str(t).strip()[:32] for t in (triggers or []) if str(t).strip()]
    trig_list = list(dict.fromkeys(trig_list))[:12]

    fm = yaml.safe_dump(
        {
            "id": sid,
            "title": clean_title,
            "triggers": trig_list,
            "default_cabinet": str(default_cabinet or "1006").strip()[:16],
            "action_kind": str(action_kind or "field_verification").strip()[:32],
        },
        allow_unicode=True,
        sort_keys=False,
    ).strip()
    content = f"---\n{fm}\n---\n\n{clean_body}\n"
    root = _skills_root(skills_dir)
    target = root / f"{sid}.md"
    target.write_text(content, encoding="utf-8")
    return _parse_skill_file(target)


def run_skill(
    archive: Any,
    skill_id: str,
    *,
    target_cabinet: str | None = None,
    at_time: str | None = None,
    skills_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute a pluggable domain skill against read-only archive + pandapower checks."""
    sid = str(skill_id or "").strip().lower()
    if not _SKILL_ID_RE.match(sid):
        raise ValueError("无效的技能 ID。")
    p = _skills_root(skills_dir) / f"{sid}.md"
    if not p.is_file():
        raise FileNotFoundError(f"未找到技能：{sid}")

    skill = _parse_skill_file(p)
    cab = str(target_cabinet or skill["default_cabinet"] or "1006").strip()
    ref_time = str(at_time or "2026-11-13 17:00:00").strip()
    tb = DiagnosticToolbox(archive)

    checks: list[dict[str, Any]] = []
    evidence_cells: list[str] = []

    ov = tb.execute("get_station_health_overview", {})
    hi_cnt = sum(1 for f in (ov.get("findings") or []) if f.get("severity") == "high")
    checks.append({
        "step": "全站四维运行体检基线",
        "status": "warning" if hi_cnt > 0 else "ok",
        "detail": f"归档覆盖 {ov.get('scope', {}).get('hours', 168)} 整点，检出 {hi_cnt} 项高风险异常。",
    })

    if ("S35", cab) in archive.devices:
        prof = tb.execute("get_cabinet_health_profile", {"cabinet": cab})
        m7 = prof.get("metrics_7d") or {}
        u_max = (m7.get("Uab") or {}).get("max")
        u_cell = (m7.get("Uab") or {}).get("max_cell")
        if u_cell:
            evidence_cells.append(u_cell)
        checks.append({
            "step": f"目标柜 {cab} 7 日电气画像",
            "status": "warning" if (u_max is not None and u_max > 10.70 and cab.isdigit() and int(cab) >= 1000) else "ok",
            "detail": f"{cab} 柜最高电压 {u_max} kV，最低有功 {(m7.get('P') or {}).get('min')} kW。",
        })

    if sid in {"tap_change_precheck", "storm_lightning_patrol"} or any(k in skill["body_markdown"] for k in ("潮流", "分接头", "pandapower")):
        pf = run_ac_power_flow(archive, scenario="tap_sensitivity", at_time=ref_time)
        rec = (pf.get("tap_sensitivity") or {}).get("recommended_tap") or {}
        checks.append({
            "step": "pandapower 交流潮流调压校验",
            "status": "ok" if rec.get("gb12325_compliant") else "warning",
            "detail": f"推荐 {rec.get('tap_label', '+2 档')}，预期 10 kV II 段母线 {rec.get('bus_10kv_ii_kv', '-')} kV。",
        })

    if sid == "main_fan_switchover" or any(k in skill["body_markdown"] for k in ("N-1", "双回路", "主扇")):
        n1 = run_ac_power_flow(archive, scenario="n1_contingency", outage_mode="feeder_1009")
        comp = n1["n1_contingency"]["receiving_1010_comparison"]
        checks.append({
            "step": "双回路 N-1 转供潮流核查",
            "status": "ok" if comp["min_underground_kv"] >= 9.30 else "warning",
            "detail": f"1009 退运由 1010 受电后电流 {comp['after_i_a']} A，井下末端 {comp['min_underground_kv']} kV。",
        })

    voi = analyze_parameter_sensitivity_voi(archive)
    top_voi = voi["voi_ranking"][0]
    checks.append({
        "step": "现场高价值缺证台账提示 (VoI #1)",
        "status": "info",
        "detail": f"优先核实「{top_voi['required_field_document']}」（VoI {top_voi['voi_score']}）。",
    })

    return {
        "status": "ok",
        "skill_id": skill["id"],
        "title": skill["title"],
        "target_cabinet": cab,
        "at_time": ref_time,
        "steps": skill["steps"],
        "checks": checks,
        "evidence_cells": list(dict.fromkeys(evidence_cells)),
        "recommended_action": {
            "kind": skill["action_kind"],
            "target_cabinet": cab,
            "at_time": ref_time,
            "rationale": f"执行技能「{skill['title']}」后的标准化操作/核查建议。",
        },
        "executed_at": _utc_now(),
    }


# ── Read-Only MCP Client Registry ──────────────────────────────────────────────

def _clients_path(registry_path: Path | None = None) -> Path:
    p = Path(registry_path) if registry_path is not None else MCP_CLIENTS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _builtin_self_client(archive: Any | None = None) -> dict[str, Any]:
    tool_names: list[str] = []
    if archive is not None:
        srv = ReadOnlyMCPServer(archive)
        resp = srv.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tool_names = [t["name"] for t in (resp.get("result") or {}).get("tools") or []]
    return {
        "client_id": "mcp-minegrid-local",
        "name": "MineGrid-Agent Built-in Read-Only MCP",
        "transport": "stdio",
        "endpoint": "python -m laowadi mcp",
        "description": "内置只读 JSON-RPC 2.0 MCP Server（15 项只读电气诊断工具）",
        "read_only": True,
        "status": "connected",
        "tools_count": len(tool_names) or 15,
        "tools": tool_names,
        "builtin": True,
        "registered_at": "2026-11-22T00:00:00Z",
    }


def list_mcp_clients(*, archive: Any | None = None, registry_path: Path | None = None) -> list[dict[str, Any]]:
    items = [_builtin_self_client(archive)]
    p = _clients_path(registry_path)
    if p.is_file():
        try:
            saved = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(saved, list):
                items.extend(saved)
        except Exception:
            pass
    return items


def register_mcp_client(
    *,
    name: str,
    endpoint: str,
    transport: str = "http",
    description: str = "",
    registry_path: Path | None = None,
) -> dict[str, Any]:
    clean_name = str(name or "").strip()[:80]
    clean_ep = str(endpoint or "").strip()[:240]
    clean_tr = str(transport or "http").strip().lower()
    if not clean_name or not clean_ep:
        raise ValueError("MCP Client 名称与端点地址不能为空。")
    if clean_tr not in {"http", "stdio"}:
        raise ValueError("transport 须为 http 或 stdio。")
    if clean_tr == "http":
        parsed = urlsplit(clean_ep)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("HTTP MCP 端点须为合法的 http/https URL。")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("非本机外部 MCP 端点须强制使用 HTTPS。")

    entry = {
        "client_id": f"mcp-{re.sub(r'[^a-z0-9]+', '-', clean_name.lower()).strip('-')[:24] or 'ext'}-{int(datetime.now(timezone.utc).timestamp()) % 10000}",
        "name": clean_name,
        "transport": clean_tr,
        "endpoint": clean_ep,
        "description": str(description or "").strip()[:240],
        "read_only": True,
        "status": "registered_readonly",
        "tools_count": 0,
        "tools": [],
        "builtin": False,
        "registered_at": _utc_now(),
    }

    p = _clients_path(registry_path)
    existing: list[dict[str, Any]] = []
    if p.is_file():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing = loaded
        except Exception:
            existing = []
    existing.append(entry)
    p.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return entry
