"""Chapter 9 GSSC (Gather-Select-Structure-Compress) context engineering and Chapter 8 memory recall."""

from __future__ import annotations

from copy import deepcopy
import json
import re
from unicodedata import normalize

from laowadi.storage import REPORT_STATION
from laowadi.verifier import EVIDENCE_POLICY


_DATE_RE = re.compile(r"(?:(\d{4})[年-])?(\d{1,2})[月-](\d{1,2})[日号]?")
_DAY_RANGE_RE = re.compile(
    r"(?:(\d{4})[年-])?(?:(\d{1,2})[月-])?(\d{1,2})[日号]?\s*(?:至|到|~|—|–|-)\s*(?:(\d{1,2})[月-])?(\d{1,2})[日号]"
)
_BARE_DAY_RE = re.compile(r"(?<!\d)(\d{1,2})(?:日|号(?!\s*(?:主变|变压器|柜|进线|出线|线路|电缆|母线|电源)))")
_HOUR_RE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:点|时|:00)")
_EVENT_RE = re.compile(r"(?<![A-Z0-9])(LW-\d+|HLTH-[A-Z0-9-]+)(?![A-Z0-9])", re.I)


def extract_query_anchors(question: str, known_cabinets: set[str], *, default_year: int = 2026, default_month: int = 11) -> dict:
    """Extract date (including ranges & bare day refs like 17日 / 12–13日), hour, cabinet, event ID, and dimension anchors."""
    question = normalize("NFKC", question)
    days = []
    # 1. Explicit date ranges like "12–13日" or "11月12日至13日"
    for m in _DAY_RANGE_RE.finditer(question):
        y = int(m.group(1) or default_year)
        m1 = int(m.group(2) or default_month)
        d1 = int(m.group(3))
        m2 = int(m.group(4) or m1)
        d2 = int(m.group(5))
        if m1 == m2 and 1 <= d1 <= d2 <= 31 and (d2 - d1) <= 14:
            for dd in range(d1, d2 + 1):
                day_str = f"{y:04d}-{m1:02d}-{dd:02d}"
                if day_str not in days:
                    days.append(day_str)

    # 2. Explicit month-day dates like "2026-11-17" or "11月17日"
    for m in _DATE_RE.finditer(question):
        y = int(m.group(1) or default_year)
        mm, dd = int(m.group(2)), int(m.group(3))
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            day_str = f"{y:04d}-{mm:02d}-{dd:02d}"
            if day_str not in days:
                days.append(day_str)

    # 3. Bare day references like "17日的303柜" when not already matched by full date/range
    scrubbed_for_bare = _DAY_RANGE_RE.sub(" ", _DATE_RE.sub(" ", question))
    for m in _BARE_DAY_RE.finditer(scrubbed_for_bare):
        dd = int(m.group(1))
        if 1 <= dd <= 31:
            day_str = f"{default_year:04d}-{default_month:02d}-{dd:02d}"
            if day_str not in days:
                days.append(day_str)

    # Remove date substrings before matching hours so "11月10日" doesn't match "10" as an hour
    scrubbed = _BARE_DAY_RE.sub(" ", _DAY_RANGE_RE.sub(" ", _DATE_RE.sub(" ", question)))
    hours = []
    for m in _HOUR_RE.finditer(scrubbed):
        hh = int(m.group(1))
        if 0 <= hh <= 23:
            h_str = f"{hh:02d}:00"
            if h_str not in hours:
                hours.append(h_str)

    cabinets = [
        cab for cab in sorted(known_cabinets, key=int)
        if re.search(rf"(?<!\d){re.escape(cab)}(?!\d)", question)
    ]
    events = [m.group(1).upper() for m in _EVENT_RE.finditer(question)]

    stations: list[str] = []
    if any(k in question for k in ("C1535", "c1535", "井下", "1535", "中央变电所")) or any(
        c.isdigit() and 1100 <= int(c) <= 1299 for c in cabinets
    ):
        stations.append("C1535")
    if any(k in question for k in ("S35", "s35", "地面", "35kV", "35 kV")) or any(
        c.isdigit() and (300 <= int(c) <= 399 or 1000 <= int(c) <= 1099) for c in cabinets
    ):
        if "S35" not in stations:
            stations.append("S35")

    dimensions = []
    if any(k in question for k in ("电压", "过压", "母线", "功率因数", "cos", "无功", "电能质量")):
        dimensions.append("power_quality")
    if any(k in question for k in ("量测", "表计", "接线", "极性", "相序", "误差", "自洽", "1018", "1012")):
        dimensions.append("metering")
    if any(k in question for k in ("平衡", "守恒", "轮询", "单进线", "双主变", "反向", "倒送", "主变")):
        dimensions.append("system_balance")
    if any(k in question for k in ("基线", "避峰", "突增", "波动", "常规", "反常")):
        dimensions.append("diurnal_baseline")

    return {
        "days": days,
        "hours": hours,
        "cabinets": cabinets,
        "events": events,
        "stations": stations,
        "dimensions": dimensions,
    }


def partition_relevant_reviews(
    reviews: dict[str, dict],
    anchors: dict,
    *,
    limit: int = 6,
    current_archive_version: str = "",
    current_day_hashes: dict[str, str] | None = None,
    current_mine_id: str = "",
    current_station: str = "",
) -> tuple[list[dict], list[dict]]:
    """Select version-, mine-, station-, and day-compatible operator reviews and return isolated stale ones."""
    if not reviews:
        return [], []
    confirmed = [r for r in reviews.values() if r.get("disposition") != "pending"]
    if not confirmed:
        return [], []

    cabs = set(anchors.get("cabinets") or [])
    ev_ids = set(anchors.get("events") or [])
    days = set(anchors.get("days") or [])
    target_stations = set(anchors.get("stations") or [])
    if not target_stations and current_station:
        target_stations = {current_station}
    day_hashes = current_day_hashes or {}

    scored = []
    isolated: list[dict] = []
    for r in confirmed:
        r_scope = str(r.get("scope_day") or "ALL")
        r_mine = str(r.get("mine_id") or "").strip()
        r_station = str(r.get("station") or "").strip()
        r_ver = str(r.get("archive_version") or "").strip()

        # Audit #09: Enforce mine, station, day, and archive-version isolation on operator reviews
        if current_mine_id and r_mine and r_mine != current_mine_id:
            isolated.append({"finding_id": r.get("finding_id"), "reason": "mine_mismatch", "review_mine": r_mine})
            continue
        if target_stations and r_station and r_station not in target_stations:
            isolated.append({"finding_id": r.get("finding_id"), "reason": "station_mismatch", "review_station": r_station})
            continue
        if days and r_scope != "ALL" and r_scope not in days:
            isolated.append({"finding_id": r.get("finding_id"), "reason": "day_mismatch", "scope_day": r_scope})
            continue
        if r_ver:
            day_hash_ok = bool(r_scope != "ALL" and day_hashes.get(r_scope) and r_ver == day_hashes[r_scope])
            snap_ok = bool(current_archive_version and r_ver == current_archive_version)
            if (current_archive_version or day_hashes) and not (snap_ok or day_hash_ok):
                isolated.append({"finding_id": r.get("finding_id"), "reason": "archive_version_mismatch", "review_version": r_ver})
                continue

        fid = r["finding_id"].upper()
        blob = f'{fid} {r_scope} {r.get("switch_state", "")} {r.get("operator_note", "")}'
        score = 0
        if fid in ev_ids:
            score += 10
        if days and r_scope in days:
            score += 6
        for c in cabs:
            if c in blob:
                score += 5
        for d in days:
            if d in blob or d.replace("-", "") in blob:
                score += 4
        scored.append((score, r["updated_at"], r))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    selected = [item[2] for item in scored if item[0] > 0]
    if not selected and not ev_ids and not cabs:
        selected = [item[2] for item in scored[: min(3, limit)]]
    return selected[:limit], isolated


def select_relevant_reviews(
    reviews: dict[str, dict],
    anchors: dict,
    limit: int = 6,
    *,
    current_archive_version: str = "",
    current_day_hashes: dict[str, str] | None = None,
    current_mine_id: str = "",
    current_station: str = "",
) -> list[dict]:
    """Select operator review records matching current cabinets, events, station, and archive version."""
    selected, _ = partition_relevant_reviews(
        reviews,
        anchors,
        limit=limit,
        current_archive_version=current_archive_version,
        current_day_hashes=current_day_hashes,
        current_mine_id=current_mine_id,
        current_station=current_station,
    )
    return selected


def compress_tool_result(tool_name: str, result: dict, anchors: dict) -> tuple[dict, dict]:
    """GSSC Structure & Compress: trim redundant hourly rows while preserving 100% of relevant evidence cells."""
    raw_json = json.dumps(result, ensure_ascii=False, allow_nan=False)
    raw_len = len(raw_json)
    if not isinstance(result, dict) or result.get("status") != "ok":
        return result, {"tool": tool_name, "raw_chars": raw_len, "compressed_chars": raw_len, "saved_pct": 0.0}

    out = deepcopy(result)
    cabs = set(anchors.get("cabinets") or [])
    dims = set(anchors.get("dimensions") or [])
    target_hours = set(anchors.get("hours") or [])

    if tool_name == "get_station_health_overview":
        findings = out.get("findings") or []
        if len(findings) > 8:
            sev_rank = {"high": 0, "medium": 1, "info": 2}

            def rank_key(f):
                c_hit = 0 if (cabs and any(c in str(f.get("cabinet", "")) for c in cabs)) else 1
                d_hit = 0 if (dims and f.get("dimension") in dims) else 1
                return (c_hit, d_hit, sev_rank.get(f.get("severity"), 2), f.get("id", ""))

            ranked = sorted(findings, key=rank_key)
            kept = ranked[:8]
            omitted = ranked[8:]
            out["findings"] = kept
            out["compressed_omitted_findings"] = [
                f'{f["id"]}({f["severity"]}/{f["cabinet"]}): {f["title"]}'
                for f in omitted
            ]

    elif tool_name == "get_diurnal_baseline":
        # Keep key tariff hours + target hours (±1h) + hours with big deviation
        keep_hours = {"00:00", "02:00", "10:00", "11:00", "12:00", "13:00", "15:00", "17:00", "18:00", "21:00"}
        for h_str in target_hours:
            hh = int(h_str[:2])
            for delta in (-1, 0, 1):
                if 0 <= hh + delta <= 23:
                    keep_hours.add(f"{hh + delta:02d}:00")

        hb = out.get("hourly_baseline") or {}
        if len(hb) > 10:
            out["hourly_baseline"] = {k: v for k, v in hb.items() if k in keep_hours}
            out["hourly_baseline_note"] = "已按 GSSC 保留峰谷切换关键整点与所查整点基线；其余平稳整点已折叠。"

        day_comp = out.get("day_comparison")
        if isinstance(day_comp, dict) and isinstance(day_comp.get("hours"), list) and len(day_comp["hours"]) > 10:
            filtered_rows = [
                row for row in day_comp["hours"]
                if row["time"][11:16] in keep_hours or abs(row.get("deviation_kw", 0)) >= 1200
            ]
            day_comp["hours"] = filtered_rows
            day_comp["compressed_note"] = "已保留峰谷切换整点、所查整点及偏离 ≥1200 kW 的整点及原表单元格。"

    elif tool_name == "get_window_snapshot":
        frames = out.get("frames") or []
        if len(frames) > 7:
            # Always keep first, last, any target hour, and any frame with large step or negative value
            keep_indices = {0, len(frames) - 1}
            for idx, fr in enumerate(frames):
                hh = fr["time"][11:16]
                if hh in target_hours:
                    keep_indices.update({max(0, idx - 1), idx, min(len(frames) - 1, idx + 1)})
                for cab_map in fr.get("readings", {}).values():
                    p_v = cab_map.get("P", {}).get("value")
                    if p_v is not None and p_v <= 0:
                        keep_indices.add(idx)
            if len(keep_indices) < 6:
                step = max(1, len(frames) // 5)
                keep_indices.update(range(0, len(frames), step))
            out["frames"] = [frames[i] for i in sorted(keep_indices)]
            out["compressed_note"] = f"原窗口 {len(frames)} 个整点，GSSC 保留 {len(out['frames'])} 个关键拐点/极值整点及原表单元格。"

    elif tool_name == "query_electrical_analytics":
        rows = out.get("results") or []
        if out.get("operation") == "joint_filter" and len(rows) > 8:
            out["results"] = rows[:8]
            out["compressed_note"] = f"原联合筛选返回 {len(rows)} 条明细，GSSC 保留前 8 条最显著整点及原表单元格，完整按柜汇总保留在 matched_cabinets_summary 中。"

    comp_json = json.dumps(out, ensure_ascii=False, allow_nan=False)
    comp_len = len(comp_json)
    saved_pct = round((1.0 - comp_len / max(raw_len, 1)) * 100.0, 1) if raw_len > comp_len else 0.0
    return out, {
        "tool": tool_name,
        "raw_chars": raw_len,
        "compressed_chars": comp_len,
        "saved_pct": saved_pct,
    }


_BACKREF_KEYWORDS = ("刚才", "上轮", "上一轮", "上面", "假设", "总结", "简报", "精简", "归纳", "排除", "继续", "前面", "那条", "该事件")


def _snapshot_meta_from_archive(archive) -> dict:
    try:
        from laowadi.dag_engine import compute_snapshot

        return compute_snapshot(archive)
    except Exception:
        return {"mine_id": "", "station": REPORT_STATION, "snapshot_sha256": "", "reports": {}}


class DiagnosisSession:
    """Multi-turn diagnostic working memory with strict entity, station, mine & archive-version isolation."""

    def __init__(self, archive, *, archive_snapshot: dict | None = None):
        self.archive = archive
        self.known_cabinets = {num for _, num in archive.devices}
        snap = archive_snapshot or _snapshot_meta_from_archive(archive)
        self.mine_id: str = str(snap.get("mine_id") or "")
        self.station: str = str(snap.get("station") or REPORT_STATION)
        self.archive_snapshot_sha256: str = str(snap.get("snapshot_sha256") or "")
        self.history: list[dict] = []
        self.turns: list[dict] = []
        self.active_anchors: dict = {
            "days": [],
            "hours": [],
            "cabinets": [],
            "events": [],
            "stations": [],
            "dimensions": [],
        }

    def resolve_question(self, question: str, *, archive_snapshot: dict | None = None) -> tuple[str, dict, dict]:
        """Resolve follow-up references using working memory while isolating mine/station/event switches and archive revisions."""
        snap = archive_snapshot or _snapshot_meta_from_archive(self.archive)
        cur_mine = str(snap.get("mine_id") or self.mine_id)
        cur_snap_sha = str(snap.get("snapshot_sha256") or "")

        current = extract_query_anchors(question, self.known_cabinets)
        inherited = {"days": [], "hours": [], "cabinets": [], "events": [], "stations": []}
        cabinet_followup = current["cabinets"] and question.strip().startswith(("那", "换", "改比"))
        date_followup = question.strip().startswith(("改到", "换到", "那改到", "那换到"))

        isolation_reasons: list[str] = []
        mine_switched = bool(self.mine_id and cur_mine and cur_mine != self.mine_id)
        archive_revised = bool(self.archive_snapshot_sha256 and cur_snap_sha and cur_snap_sha != self.archive_snapshot_sha256)
        prev_stations = set(self.active_anchors.get("stations") or [])
        cur_stations = set(current.get("stations") or [])
        station_switched = bool(cur_stations and prev_stations and cur_stations != prev_stations)
        new_event = bool(current["events"] and set(current["events"]) != set(self.active_anchors.get("events") or []))
        new_day = bool(current["days"] and set(current["days"]) != set(self.active_anchors.get("days") or []))

        if mine_switched:
            isolation_reasons.append("mine_switched")
        if archive_revised:
            isolation_reasons.append("archive_revised")
        if station_switched:
            isolation_reasons.append("station_switched")
        if new_event:
            isolation_reasons.append("event_switched")
        if new_day:
            isolation_reasons.append("day_switched")

        hard_isolated = mine_switched or archive_revised
        if hard_isolated:
            self.turns = []
            self.mine_id = cur_mine
            self.archive_snapshot_sha256 = cur_snap_sha
            merged = deepcopy(current)
        elif station_switched or new_event or new_day:
            merged = deepcopy(current)
            if new_day and date_followup and not (station_switched or new_event or current["cabinets"]):
                cabs = self.active_anchors.get("cabinets") or []
                if len(cabs) == 1 and self.active_anchors.get("metrics"):
                    merged["cabinets"] = list(cabs)
                    merged["stations"] = list(self.active_anchors.get("stations") or [])
                    inherited["cabinets"] = merged["cabinets"]
        else:
            merged = deepcopy(current)
            if not merged["days"] and self.active_anchors.get("days"):
                merged["days"] = list(self.active_anchors["days"])
                inherited["days"] = merged["days"]
            same_time = any(w in question for w in ("同期", "同一时刻", "当时", "该时刻", "此时", "这个整点", "刚才", "那时候", "那会儿"))
            if (not merged["hours"] and self.active_anchors.get("hours")
                    and (same_time or cabinet_followup and self.active_anchors.get("metrics"))):
                merged["hours"] = list(self.active_anchors["hours"])
                inherited["hours"] = merged["hours"]
            if not merged["cabinets"] and self.active_anchors.get("cabinets") and not merged["events"]:
                merged["cabinets"] = list(self.active_anchors["cabinets"])
                inherited["cabinets"] = merged["cabinets"]
            if not merged["events"] and self.active_anchors.get("events") and any(w in question for w in ("该事件", "这条", "那", "同期", "刚才", "换成", "改比")):
                merged["events"] = list(self.active_anchors["events"])
                inherited["events"] = merged["events"]
            if not merged["stations"] and self.active_anchors.get("stations"):
                merged["stations"] = list(self.active_anchors["stations"])
                inherited["stations"] = merged["stations"]

        reset_scope = hard_isolated or station_switched or new_event or new_day
        for field in ("metrics", "statistics"):
            preserve_metric = field == "metrics" and date_followup and bool(inherited["cabinets"])
            values = [] if reset_scope and not preserve_metric else list(self.active_anchors.get(field) or [])
            merged[field] = values

        self.active_anchors = {
            "days": merged["days"] if (merged["days"] or reset_scope) else list(self.active_anchors.get("days") or []),
            "hours": merged["hours"] if (merged["hours"] or reset_scope) else list(self.active_anchors.get("hours") or []),
            "cabinets": merged["cabinets"] if (merged["cabinets"] or reset_scope) else list(self.active_anchors.get("cabinets") or []),
            "events": merged["events"] if (merged["events"] or reset_scope) else list(self.active_anchors.get("events") or []),
            "stations": merged["stations"] if (merged["stations"] or hard_isolated or station_switched) else list(self.active_anchors.get("stations") or []),
            "dimensions": merged["dimensions"] if (merged["dimensions"] or hard_isolated) else list(self.active_anchors.get("dimensions") or []),
            "metrics": merged["metrics"],
            "statistics": merged["statistics"],
        }
        if cur_snap_sha and not self.archive_snapshot_sha256:
            self.archive_snapshot_sha256 = cur_snap_sha

        prefix_parts = []
        if inherited["days"]:
            prefix_parts.append(f'承接上文日期：{", ".join(inherited["days"])}')
        if inherited["hours"]:
            prefix_parts.append(f'承接上文时刻：{", ".join(inherited["hours"])}')
        if inherited["cabinets"]:
            prefix_parts.append(f'承接上文柜号：{", ".join(inherited["cabinets"])}')
        if inherited["events"]:
            prefix_parts.append(f'承接上文事件：{", ".join(inherited["events"])}')

        resolved_q = f"{question}（{'；'.join(prefix_parts)}）" if prefix_parts else question
        snapshot = {
            "turn": len(self.history) + 1,
            "raw_question": question,
            "resolved_question": resolved_q,
            "current_anchors": current,
            "inherited_anchors": inherited,
            "active_anchors": deepcopy(self.active_anchors),
            "topic_switched": bool(isolation_reasons),
            "isolation_reasons": isolation_reasons,
            "archive_revised": archive_revised,
            "mine_id": cur_mine,
            "archive_snapshot_sha256": cur_snap_sha,
        }
        self.history.append(snapshot)
        return resolved_q, merged, snapshot

    def record_turn(
        self,
        question: str,
        report: dict | None,
        verification: dict | None,
        *,
        archive_snapshot_sha256: str = "",
    ) -> None:
        """Record a compact, grounded digest of the completed turn bound to archive snapshot version."""
        if not report or not verification or verification.get("policy") != EVIDENCE_POLICY or verification["status"] != "verified":
            return
        digest = {
            "turn": len(self.history),
            "question": question,
            "resolved_question": report.get("evidence_question") or (
                self.history[-1]["resolved_question"] if self.history else question
            ),
            "verification_policy": verification["policy"],
            "summary": str(report.get("summary", ""))[:260],
            "facts": [
                {
                    "statement": str(f.get("statement", ""))[:160],
                    "evidence": (f.get("evidence") or [])[:3] if isinstance(f.get("evidence"), list) else [str(f.get("evidence"))],
                }
                for f in (report.get("confirmed_facts") or [])[:4]
                if isinstance(f, dict)
            ],
            "hypotheses": [
                {
                    "rank": h.get("rank", idx + 1),
                    "title": str(h.get("title", ""))[:100],
                    "confidence": h.get("confidence", "medium"),
                    "caveats": str(h.get("caveats", ""))[:120],
                }
                for idx, h in enumerate((report.get("hypotheses") or [])[:3])
                if isinstance(h, dict)
            ],
            "verified_cells": list(((verification or {}).get("verified_cells") or [])[:6]),
            "active_anchors": deepcopy(self.active_anchors),
            "mine_id": self.mine_id,
            "archive_snapshot_sha256": archive_snapshot_sha256 or self.archive_snapshot_sha256,
        }
        self.turns = (self.turns + [digest])[-8:]

    def record_supplement(self, thread_id: str, result: dict) -> None:
        outcome = {
            "conditionally_satisfied": "按所给参数计算，条件满足",
            "conditionally_violated": "按所给参数计算，条件未满足",
            "evidence_conflict": "现场补证存在冲突",
        }.get(result["item_status"], "仍需补证")
        record = {
            "turn": len(self.history),
            "question": f"对话内补证复算 {thread_id}",
            "verification_policy": EVIDENCE_POLICY,
            "kind": "supplement",
            "summary": f'{result["target_item_id"]} · {result["v2_run_id"]}。{outcome}。',
            "facts": [],
            "hypotheses": [],
            "verified_cells": [],
            "active_anchors": deepcopy(self.active_anchors),
            "mine_id": self.mine_id,
            "archive_snapshot_sha256": self.archive_snapshot_sha256,
        }
        self.turns = (self.turns + [record])[-8:]

    def latest_turn(self) -> dict | None:
        return next((turn for turn in reversed(self.turns) if turn.get("kind") != "supplement"), None)

    def format_history_messages(self, current_question: str, max_turns: int = 3) -> list[dict]:
        """Build compact prior [user, assistant] turns while strictly blocking cross-version/mine/station leaks."""
        if not self.turns:
            return []
        last_snap = self.history[-1] if self.history else {}
        reasons = set(last_snap.get("isolation_reasons") or [])
        if reasons & {"archive_revised", "mine_switched", "station_switched"}:
            return []
        is_switched = bool(last_snap.get("topic_switched"))
        has_backref = any(w in current_question for w in _BACKREF_KEYWORDS)
        if is_switched and not has_backref:
            return []

        msgs: list[dict] = []
        for item in self.turns[-max_turns:]:
            hyp_text = "；".join(f'假设{h["rank"]}:{h["title"]}' for h in item.get("hypotheses") or [])
            cells_text = ", ".join(item.get("verified_cells") or [])
            scope = "条件复算记录" if item.get("kind") == "supplement" else "已归档摘要"
            assistant_digest = (
                f"[第{item['turn']}轮{scope}] {item['summary']}"
                + (f"\n已列假设：{hyp_text}" if hyp_text else "")
                + (f"\n已核实单元格：{cells_text}" if cells_text else "")
            )
            msgs.append({"role": "user", "content": item["question"]})
            msgs.append({"role": "assistant", "content": assistant_digest})
        return msgs
