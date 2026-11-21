"""Incremental SCADA CSV Report Ingestion & Real-Time Telemetry Drill Stream.

All uploaded reports are stored in an isolated local sandbox SQLite database
(`local/laowadi_live.sqlite3`), guaranteeing 100% immutability of the read-only
golden archives (`laowadi.sqlite3` and `laowadi_events.sqlite3`).
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any
import uuid

from laowadi.memory_store import MEMORY_DB, extract_custom_voltage_thresholds, list_memories
from laowadi.powerflow import run_ac_power_flow
from laowadi.storage import METRICS, REPORT_STATION, ROOT


LIVE_DB = ROOT / "local" / "laowadi_live.sqlite3"
MAX_UPLOAD_CHARS = 2_000_000
MAX_UPLOAD_ROWS = 20_000

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect_live(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else LIVE_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS live_upload (
            upload_id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            day TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            cabinet_count INTEGER NOT NULL,
            findings_count INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            findings_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS live_reading (
            upload_id TEXT NOT NULL,
            day TEXT NOT NULL,
            time TEXT NOT NULL,
            station TEXT NOT NULL,
            cabinet TEXT NOT NULL,
            metric TEXT NOT NULL,
            value REAL NOT NULL,
            PRIMARY KEY (day, time, station, cabinet, metric)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_live_day_time ON live_reading(day, time)")
    conn.commit()
    return conn


def _normalize_timestamp(raw_time: str, fallback_day: str | None) -> tuple[str, str]:
    t = str(raw_time or "").strip().replace("/", "-")
    if _TIME_RE.match(t):
        t = t.replace("T", " ")
        if len(t) == 16:
            t += ":00"
        return t[:10], t
    # Allow HH:00 when fallback_day is supplied
    m_hm = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", t)
    if m_hm and fallback_day and _DAY_RE.match(fallback_day):
        hh, mm, ss = int(m_hm.group(1)), int(m_hm.group(2)), int(m_hm.group(3) or 0)
        if 0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59:
            return fallback_day, f"{fallback_day} {hh:02d}:{mm:02d}:{ss:02d}"
    raise ValueError(f"无法解析时间戳：{raw_time}（需为 YYYY-MM-DD HH:00:00 或配合日期提供 HH:00）")


def _screen_uploaded_readings(
    readings: list[tuple[str, str, str, str, str, float]],
    *,
    voltage_limit_kv: float = 10.70,
) -> list[dict[str, Any]]:
    """Run immediate deterministic 4D electrical health screening on uploaded incremental telemetry."""
    findings: list[dict[str, Any]] = []
    by_cab_metric: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for _day, ts, _st, cab, metric, val in readings:
        by_cab_metric.setdefault((cab, metric), []).append((ts, val))

    # 1. 10 kV Overvoltage / Undervoltage
    for (cab, metric), pts in sorted(by_cab_metric.items()):
        if metric != "Uab":
            continue
        is_10kv = cab.isdigit() and int(cab) >= 1000
        if not is_10kv:
            continue
        overs = [(t, v) for t, v in pts if v > voltage_limit_kv]
        if overs:
            peak_t, peak_v = max(overs, key=lambda x: x[1])
            findings.append({
                "id": f"LIVE-OV-{cab}",
                "severity": "high",
                "cabinet": cab,
                "kind": "overvoltage",
                "title": f"{cab} 柜 10 kV 线电压越上限（>{voltage_limit_kv:.2f} kV）",
                "summary": f"上传数据检出 {len(overs)} 个整点超限，峰值 {peak_v:.2f} kV（{peak_t}）。",
                "peak_time": peak_t,
                "peak_value": round(peak_v, 3),
                "recommended_ticket_kind": "switching_order",
            })
        unders = [(t, v) for t, v in pts if 0 < v < 9.30]
        if unders:
            low_t, low_v = min(unders, key=lambda x: x[1])
            findings.append({
                "id": f"LIVE-UV-{cab}",
                "severity": "high",
                "cabinet": cab,
                "kind": "undervoltage",
                "title": f"{cab} 柜 10 kV 线电压越下限（<9.30 kV）",
                "summary": f"上传数据检出 {len(unders)} 个整点欠压，最低 {low_v:.2f} kV（{low_t}）。",
                "peak_time": low_t,
                "peak_value": round(low_v, 3),
                "recommended_ticket_kind": "switching_order",
            })

    # 2. Reverse active power
    for (cab, metric), pts in sorted(by_cab_metric.items()):
        if metric != "P":
            continue
        negs = [(t, v) for t, v in pts if v < -5.0]
        if negs:
            min_t, min_v = min(negs, key=lambda x: x[1])
            findings.append({
                "id": f"LIVE-RP-{cab}",
                "severity": "high",
                "cabinet": cab,
                "kind": "reverse_power",
                "title": f"{cab} 柜检出负有功功率读数",
                "summary": f"上传数据检出 {len(negs)} 个负有功整点，最低 {min_v:.1f} kW（{min_t}），建议核校二次极性与反送电。",
                "peak_time": min_t,
                "peak_value": round(min_v, 2),
                "recommended_ticket_kind": "defect_notice",
            })

    # 3. Low power factor
    for (cab, metric), pts in sorted(by_cab_metric.items()):
        if metric != "cos":
            continue
        lows = [(t, v) for t, v in pts if 0.05 < v < 0.85]
        if lows:
            min_t, min_v = min(lows, key=lambda x: x[1])
            findings.append({
                "id": f"LIVE-PF-{cab}",
                "severity": "medium",
                "cabinet": cab,
                "kind": "low_power_factor",
                "title": f"{cab} 柜功率因数偏低（<0.85）",
                "summary": f"上传数据检出 {len(lows)} 个低功率因数整点，最低 {min_v:.3f}（{min_t}），建议评估无功补偿。",
                "peak_time": min_t,
                "peak_value": round(min_v, 3),
                "recommended_ticket_kind": "switching_order",
            })

    return findings


def ingest_csv_report(
    csv_text: str,
    *,
    filename: str = "incremental_report.csv",
    day: str | None = None,
    voltage_limit_kv: float = 10.70,
    live_db_path: Path | None = None,
) -> dict[str, Any]:
    """Parse tall or wide SCADA CSV telemetry into isolated `laowadi_live.sqlite3` and screen anomalies."""
    raw = str(csv_text or "").strip()
    if not raw:
        raise ValueError("上传的 CSV 内容不能为空。")
    if len(raw) > MAX_UPLOAD_CHARS:
        raise ValueError(f"上传文本超过 {MAX_UPLOAD_CHARS} 字符上限。")
    if day is not None and not _DAY_RE.match(str(day).strip()):
        raise ValueError("日期格式须为 YYYY-MM-DD。")

    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        raise ValueError("CSV 缺少表头字段。")
    headers = [h.strip() for h in reader.fieldnames if h]
    header_map = {h.lower(): h for h in headers}

    is_tall = "metric" in header_map and "value" in header_map and "cabinet" in header_map
    wide_metrics = [m for m in METRICS if m.lower() in header_map]
    is_wide = "cabinet" in header_map and bool(wide_metrics)
    if not is_tall and not is_wide:
        raise ValueError(
            "CSV 须包含 time,cabinet,metric,value（长表）或 time,cabinet,Uab,Ia,P,cos（宽表）字段。"
        )

    time_col = header_map.get("time") or header_map.get("timestamp") or header_map.get("时刻")
    if not time_col:
        raise ValueError("CSV 缺少 time / timestamp 时间列。")
    cab_col = header_map["cabinet"]
    station_col = header_map.get("station")

    parsed: list[tuple[str, str, str, str, str, float]] = []
    days_seen: set[str] = set()
    cabs_seen: set[str] = set()

    for idx, row in enumerate(reader, start=2):
        if len(parsed) >= MAX_UPLOAD_ROWS:
            raise ValueError(f"单次导入记录超过 {MAX_UPLOAD_ROWS} 行上限。")
        cab = str(row.get(cab_col) or "").strip()
        if not cab:
            continue
        station = str(row.get(station_col) or REPORT_STATION).strip() if station_col else REPORT_STATION
        row_day, row_ts = _normalize_timestamp(str(row.get(time_col) or ""), day)
        days_seen.add(row_day)
        cabs_seen.add(cab)

        if is_tall:
            m_raw = str(row.get(header_map["metric"]) or "").strip()
            m_norm = next((m for m in METRICS if m.lower() == m_raw.lower()), None)
            if not m_norm:
                raise ValueError(f"第 {idx} 行电气量 metric={m_raw!r} 无效，须为 {', '.join(METRICS)}。")
            v_str = str(row.get(header_map["value"]) or "").strip()
            if not v_str:
                continue
            try:
                val = float(v_str)
            except ValueError as exc:
                raise ValueError(f"第 {idx} 行数值 {v_str!r} 无法转为浮点数。") from exc
            parsed.append((row_day, row_ts, station, cab, m_norm, val))
        else:
            for m in wide_metrics:
                v_str = str(row.get(header_map[m.lower()]) or "").strip()
                if not v_str:
                    continue
                try:
                    val = float(v_str)
                except ValueError as exc:
                    raise ValueError(f"第 {idx} 行 {m}={v_str!r} 无法转为浮点数。") from exc
                parsed.append((row_day, row_ts, station, cab, m, val))

    if not parsed:
        raise ValueError("CSV 中未解析到有效数值记录。")

    primary_day = day or sorted(days_seen)[0]
    findings = _screen_uploaded_readings(parsed, voltage_limit_kv=float(voltage_limit_kv))
    upload_id = f"UPL-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    now = _utc_now()
    clean_fname = Path(str(filename or "incremental_report.csv")).name[:120]

    conn = _connect_live(live_db_path)
    try:
        conn.execute(
            """
            INSERT INTO live_upload
              (upload_id, filename, day, row_count, cabinet_count, findings_count, sha256, findings_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                upload_id,
                clean_fname,
                primary_day,
                len(parsed),
                len(cabs_seen),
                len(findings),
                digest,
                json.dumps(findings, ensure_ascii=False),
                now,
            ),
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO live_reading (upload_id, day, time, station, cabinet, metric, value)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [(upload_id, d, ts, st, cab, m, val) for d, ts, st, cab, m, val in parsed],
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "ok",
        "upload_id": upload_id,
        "filename": clean_fname,
        "day": primary_day,
        "days_covered": sorted(days_seen),
        "row_count": len(parsed),
        "cabinet_count": len(cabs_seen),
        "findings_count": len(findings),
        "sha256": digest,
        "findings": findings,
        "created_at": now,
        "summary": (
            f"已将 {clean_fname}（{len(parsed)} 条读数 / {len(cabs_seen)} 柜 / {primary_day}）"
            f"导入隔离沙盒库（黄金归档库 100% 未改动），自动检出 {len(findings)} 项异常。"
        ),
    }


def list_live_uploads(*, live_db_path: Path | None = None, limit: int = 25) -> list[dict[str, Any]]:
    conn = _connect_live(live_db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM live_upload ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
        out = []
        for r in rows:
            try:
                f_list = json.loads(r["findings_json"] or "[]")
            except Exception:
                f_list = []
            out.append({
                "upload_id": r["upload_id"],
                "filename": r["filename"],
                "day": r["day"],
                "row_count": int(r["row_count"]),
                "cabinet_count": int(r["cabinet_count"]),
                "findings_count": int(r["findings_count"]),
                "sha256": r["sha256"],
                "findings": f_list,
                "created_at": r["created_at"],
            })
        return out
    finally:
        conn.close()


def _load_day_frames(archive: Any, day: str, live_db_path: Path | None = None) -> tuple[str, list[tuple[str, dict[tuple[str, str], float], dict[tuple[str, str], str]]]]:
    """Load hourly snapshots for `day` from live sandbox uploads first, then fallback to read-only archive."""
    conn = _connect_live(live_db_path)
    try:
        lrows = conn.execute(
            "SELECT time, cabinet, metric, value FROM live_reading WHERE day = ? ORDER BY time",
            (day,),
        ).fetchall()
    finally:
        conn.close()

    if lrows:
        by_t: dict[str, dict[tuple[str, str], float]] = {}
        for r in lrows:
            by_t.setdefault(r["time"], {})[(r["cabinet"], r["metric"])] = float(r["value"])
        return "live_upload", [(t, vals, {}) for t, vals in sorted(by_t.items())]

    arows = archive.db.execute(
        """
        SELECT r.time, r.cabinet, r.metric, r.value,
               rp.filename || ' / ' || rp.sheet || '!' || r.source_col || r.source_row AS cell
        FROM reading r
        JOIN report rp ON r.day = rp.day
        WHERE r.station = ? AND r.day = ? AND r.value IS NOT NULL
          AND r.cabinet IN ('303', '306', '1005', '1006', '1010', '1018')
        ORDER BY r.time
        """,
        (REPORT_STATION, day),
    ).fetchall()
    if not arows:
        raise ValueError(f"归档库与增量沙盒库中均未找到 {day} 的整点数据。")

    by_t_arch: dict[str, dict[tuple[str, str], float]] = {}
    by_t_cells: dict[str, dict[tuple[str, str], str]] = {}
    for r in arows:
        by_t_arch.setdefault(r["time"], {})[(r["cabinet"], r["metric"])] = float(r["value"])
        by_t_cells.setdefault(r["time"], {})[(r["cabinet"], r["metric"])] = r["cell"]
    return "archive", [(t, by_t_arch[t], by_t_cells[t]) for t in sorted(by_t_arch)]


def stream_live_telemetry(
    archive: Any,
    *,
    day: str = "2026-11-13",
    speed_ms: int = 80,
    custom_threshold_kv: float | None = None,
    memory_db_path: Path | None = None,
    live_db_path: Path | None = None,
    on_event=None,
) -> dict[str, Any]:
    """Stream real-time drill telemetry ticks with instant anomaly pre-diagnosis & action ticket suggestions."""
    t0 = time.monotonic()
    clean_day = str(day or "2026-11-13").strip()
    if not _DAY_RE.match(clean_day):
        raise ValueError("演练日期格式须为 YYYY-MM-DD。")
    delay_s = max(0.0, min(float(speed_ms or 0), 1000.0)) / 1000.0

    def emit(ev_type: str, **payload):
        if on_event is not None:
            on_event({"type": ev_type, "ts": round(time.monotonic() - t0, 3), **payload})

    # Resolve effective voltage threshold: explicit param -> active semantic memory -> 10.70 kV
    eff_thr = float(custom_threshold_kv) if custom_threshold_kv is not None else 10.70
    if custom_threshold_kv is None:
        mems = list_memories(category="semantic", enabled_only=True, db_path=memory_db_path or MEMORY_DB)
        custom_thrs = extract_custom_voltage_thresholds(mems)
        if custom_thrs:
            eff_thr = min(float(x["value_kv"]) for x in custom_thrs)

    source_kind, frames = _load_day_frames(archive, clean_day, live_db_path=live_db_path)
    alerts: list[dict[str, Any]] = []
    tap_hint_cache: dict[str, Any] | None = None
    ov_alerted = False
    rp_alerted = False
    zero_alerted = False

    total = len(frames)
    for idx, (ts_str, vals, cells) in enumerate(frames, start=1):
        metrics = {
            "u_1005_kv": vals.get(("1005", "Uab")),
            "u_1006_kv": vals.get(("1006", "Uab")),
            "i_303_a": vals.get(("303", "Ia")),
            "p_306_kw": vals.get(("306", "P")),
            "p_1006_kw": vals.get(("1006", "P")),
            "cos_1010": vals.get(("1010", "cos")),
            "i_1010_a": vals.get(("1010", "Ia")),
        }
        tick_cells = {
            "u_1006_kv": cells.get(("1006", "Uab")),
            "i_303_a": cells.get(("303", "Ia")),
            "p_306_kw": cells.get(("306", "P")),
            "p_1006_kw": cells.get(("1006", "P")),
        }
        emit(
            "telemetry_tick",
            index=idx,
            total=total,
            time=ts_str,
            source=source_kind,
            threshold_kv=round(eff_thr, 3),
            metrics=metrics,
            cells=tick_cells,
        )

        # Trigger 1: 10 kV II Bus Overvoltage -> Instant pandapower pre-diagnosis + switching order recommendation
        u1006 = metrics["u_1006_kv"]
        if u1006 is not None and u1006 > eff_thr and not ov_alerted:
            ov_alerted = True
            tap_note = "建议复核 2# 主变分接头调至 +2 档降压。"
            if source_kind == "archive":
                try:
                    if tap_hint_cache is None:
                        tap_hint_cache = run_ac_power_flow(archive, scenario="tap_sensitivity", at_time=ts_str)
                    rec = (tap_hint_cache.get("tap_sensitivity") or {}).get("recommended_tap")
                    if rec:
                        tap_note = f"pandapower 即时预复算：将 2# 主变调至 {rec['tap_label']} 可降至 {rec['bus_10kv_ii_kv']} kV。"
                except Exception:
                    pass
            alert = {
                "alert_id": f"ALRT-OV-{idx:02d}",
                "time": ts_str,
                "severity": "high",
                "cabinet": "1006",
                "kind": "overvoltage",
                "title": f"1006 柜线电压实时越限：{u1006:.2f} kV（红线 {eff_thr:.2f} kV）",
                "summary": f"{ts_str} 监测到 10 kV II 段母线抬升至 {u1006:.2f} kV。{tap_note}",
                "evidence_cell": tick_cells.get("u_1006_kv"),
                "recommended_ticket": {
                    "kind": "switching_order",
                    "target_cabinet": "1006",
                    "at_time": ts_str,
                    "rationale": f"实时监测 {ts_str} 1006 柜电压 {u1006:.2f} kV 超限，申请起草 2# 主变调压操作票草案。",
                },
            }
            alerts.append(alert)
            emit("live_alert", alert=alert)

        # Trigger 2: Reverse Active Power
        p1006 = metrics["p_1006_kw"]
        if p1006 is not None and p1006 < -5.0 and not rp_alerted:
            rp_alerted = True
            alert = {
                "alert_id": f"ALRT-RP-{idx:02d}",
                "time": ts_str,
                "severity": "high",
                "cabinet": "1006",
                "kind": "reverse_power",
                "title": f"1006 柜检出负有功读数：{p1006:.1f} kW",
                "summary": f"{ts_str} 1006 柜出现负有功（{p1006:.1f} kW），建议发起二次极性与 SCADA 规约消缺核查。",
                "evidence_cell": tick_cells.get("p_1006_kw"),
                "recommended_ticket": {
                    "kind": "defect_notice",
                    "target_cabinet": "1006",
                    "at_time": ts_str,
                    "rationale": f"{ts_str} 1006 柜实时负有功 {p1006:.1f} kW，起草二次量测与极性消缺单。",
                },
            }
            alerts.append(alert)
            emit("live_alert", alert=alert)

        # Trigger 3: Single-incomer zero load
        p306 = metrics["p_306_kw"]
        i303 = metrics["i_303_a"]
        if p306 is not None and abs(p306) < 1e-6 and i303 is not None and i303 >= 85.0 and not zero_alerted:
            zero_alerted = True
            alert = {
                "alert_id": f"ALRT-SI-{idx:02d}",
                "time": ts_str,
                "severity": "medium",
                "cabinet": "303",
                "kind": "single_incomer_zero",
                "title": f"306 进线归零 · 303 单进线电流升至 {i303:.2f} A",
                "summary": f"{ts_str} 306 柜有功为 0 kW，303 柜单回承接负荷（{i303:.2f} A），建议补录倒闸日志与额定载流台账。",
                "evidence_cell": tick_cells.get("i_303_a"),
                "recommended_ticket": {
                    "kind": "field_verification",
                    "target_cabinet": "303",
                    "at_time": ts_str,
                    "rationale": f"{ts_str} 306 归零期间 303 电流达 {i303:.2f} A，申请补录铭牌载流量与母联开关状态。",
                },
            }
            alerts.append(alert)
            emit("live_alert", alert=alert)

        if delay_s > 0 and idx < total:
            time.sleep(delay_s)

    result = {
        "status": "ok",
        "day": clean_day,
        "source": source_kind,
        "ticks_emitted": total,
        "threshold_kv": round(eff_thr, 3),
        "alerts_count": len(alerts),
        "alerts": alerts,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }
    emit("done", result=result)
    return result
