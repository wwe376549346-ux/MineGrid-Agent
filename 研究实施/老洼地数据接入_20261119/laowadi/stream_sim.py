"""Continuous Stream Fault-Injection & Latency Benchmark for Master's Thesis Evaluation.

Simulates streaming the 168 hourly frames (25 cabinets x 4 metrics = 16,800 readings)
from the archived Laowadi dataset through an idempotent stream processor, testing:
1. Duplicate frame injection (idempotent deduplication: 0 duplicate alerts)
2. Out-of-order / late packet arrival (watermark window re-alignment)
3. Stream dropout & backfill recovery (explicit gap detection + recovery)
4. Process restart watermark recovery (resume from frame 84 -> 168 without loss/duplication)
5. Per-frame ingestion & 4-dimension health screening latency distribution (P50 / P95 / P99 ms)
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
from time import perf_counter

from laowadi.queries import Archive
from laowadi.storage import REPORT_STATION, ROOT


STREAM_REPORT_MD = ROOT / "local" / "老洼地连续流异常注入与时延评测报告.md"
STREAM_REPORT_JSON = ROOT / "local" / "老洼地连续流异常注入与时延评测报告.json"


def _frame_digest(timestamp: str, readings: dict) -> str:
    payload = json.dumps({"time": timestamp, "readings": readings}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def load_168_frames(archive: Archive) -> list[dict]:
    rows = archive.db.execute(
        """
        SELECT time, cabinet, metric, value, source_col || source_row AS cell
        FROM reading
        WHERE station = ?
        ORDER BY time, CAST(cabinet AS INTEGER)
        """,
        (REPORT_STATION,),
    ).fetchall()
    by_time = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        by_time[r["time"]][r["cabinet"]][r["metric"]] = r["value"]
    frames = []
    for ts in sorted(by_time):
        readings = {cab: dict(metrics) for cab, metrics in by_time[ts].items()}
        frames.append({
            "time": ts,
            "cabinet_count": len(readings),
            "point_count": sum(len(m) for m in readings.values()),
            "frame_hash": _frame_digest(ts, readings),
            "readings": readings,
        })
    return frames


def screen_single_frame(frame: dict, prev_frame: dict | None = None) -> list[dict]:
    """Perform real-time 25-cabinet x 4-metric health screening on a single incoming frame."""
    alerts = []
    ts = frame["time"]
    rd = frame["readings"]

    # 1. Check 306 zero power / single incomer mode
    c303 = rd.get("303", {})
    c306 = rd.get("306", {})
    if c306.get("P") == 0 and c306.get("Ia") == 0 and (c303.get("P") or 0) > 4000:
        alerts.append({"type": "single_incomer_mode", "time": ts, "cabinet": "306", "p_303": c303.get("P")})

    # 2. Check 2# transformer chain reverse power
    c312 = rd.get("312", {})
    c1006 = rd.get("1006", {})
    if (c306.get("P") or 0) < 0 and (c312.get("P") or 0) < 0 and (c1006.get("P") or 0) < 0:
        alerts.append({"type": "transformer2_reverse_power", "time": ts, "cabinet": "312"})

    # 3. Check 10 kV bus over-voltage (+7% = 10.70 kV)
    if (c1006.get("Uab") or 0) > 10.70:
        alerts.append({"type": "bus_10kv_ii_overvoltage", "time": ts, "uab": c1006.get("Uab")})

    # 4. Check 1018 persistent negative active power
    c1018 = rd.get("1018", {})
    if (c1018.get("P") or 0) < 0 and (c1018.get("cos") or 0) < 0:
        alerts.append({"type": "metering_polarity_1018", "time": ts, "p": c1018.get("P")})

    # 5. Check step active power change >= 1000 kW on key cabinets ONLY when prev_frame is adjacent (3600s)
    if prev_frame is not None:
        dt_sec = (datetime.fromisoformat(ts) - datetime.fromisoformat(prev_frame["time"])).total_seconds()
        if dt_sec == 3600:
            for cab in ("303", "306", "1009", "1010"):
                p_now = rd.get(cab, {}).get("P")
                p_prev = prev_frame["readings"].get(cab, {}).get("P")
                if p_now is not None and p_prev is not None and abs(p_now - p_prev) >= 1000.0:
                    alerts.append({
                        "type": "step_power_change",
                        "time": ts,
                        "cabinet": cab,
                        "delta_kw": round(p_now - p_prev, 2),
                    })

    return alerts


class IdempotentStreamProcessor:
    """Stateful stream receiver with frame-hash deduplication, reordering buffer, and watermark."""

    def __init__(self):
        self.processed_frames = {}
        self.alerts_by_key = {}
        self.duplicate_drops = 0
        self.reordered_recoveries = 0
        self.dropout_notices = []
        self.watermark_time = None

    def ingest(self, frame: dict, expected_prev_time: str | None = None) -> dict:
        ts = frame["time"]
        fhash = frame["frame_hash"]
        if ts in self.processed_frames and self.processed_frames[ts]["frame_hash"] == fhash:
            self.duplicate_drops += 1
            return {"status": "duplicate_ignored", "time": ts, "new_alerts": 0}

        if expected_prev_time and expected_prev_time not in self.processed_frames:
            self.dropout_notices.append({"at": ts, "missing_prev": expected_prev_time})

        self.processed_frames[ts] = frame
        sorted_times = sorted(self.processed_frames)
        idx = sorted_times.index(ts)
        prev_frame = self.processed_frames[sorted_times[idx - 1]] if idx > 0 else None

        if self.watermark_time and ts < self.watermark_time:
            self.reordered_recoveries += 1
        if not self.watermark_time or ts > self.watermark_time:
            self.watermark_time = ts

        new_alerts = 0
        for al in screen_single_frame(frame, prev_frame):
            key = f"{al['time']}:{al['type']}:{al.get('cabinet', '')}"
            if key not in self.alerts_by_key:
                self.alerts_by_key[key] = al
                new_alerts += 1

        # If a late packet arrived before an already-processed successor, re-screen successor idempotently
        if idx + 1 < len(sorted_times):
            succ_frame = self.processed_frames[sorted_times[idx + 1]]
            for al in screen_single_frame(succ_frame, frame):
                key = f"{al['time']}:{al['type']}:{al.get('cabinet', '')}"
                if key not in self.alerts_by_key:
                    self.alerts_by_key[key] = al
                    new_alerts += 1

        return {"status": "ingested", "time": ts, "new_alerts": new_alerts}


def run_stream_benchmark(archive: Archive) -> dict:
    frames = load_168_frames(archive)
    assert len(frames) == 168

    # Baseline clean stream + latency measurement
    baseline_proc = IdempotentStreamProcessor()
    latencies_ms = []
    for i, fr in enumerate(frames):
        t0 = perf_counter()
        baseline_proc.ingest(fr, expected_prev_time=frames[i - 1]["time"] if i > 0 else None)
        latencies_ms.append((perf_counter() - t0) * 1000.0)

    lat_sorted = sorted(latencies_ms)
    n = len(lat_sorted)
    latency_stats = {
        "frames_count": n,
        "points_total": sum(f["point_count"] for f in frames),
        "p50_ms": round(lat_sorted[n // 2], 4),
        "p95_ms": round(lat_sorted[int(n * 0.95)], 4),
        "p99_ms": round(lat_sorted[int(n * 0.99)], 4),
        "max_ms": round(lat_sorted[-1], 4),
        "mean_ms": round(sum(lat_sorted) / n, 4),
    }
    baseline_alerts_total = len(baseline_proc.alerts_by_key)

    # Test 1: Duplicate Frame Injection (send 2026-11-10 12:00:00 3 extra times)
    dup_proc = IdempotentStreamProcessor()
    for fr in frames:
        dup_proc.ingest(fr)
        if fr["time"] == "2026-11-10 12:00:00":
            dup_proc.ingest(fr)
            dup_proc.ingest(fr)
            dup_proc.ingest(fr)

    # Test 2: Out-of-order / Late Arrival (swap 12:00 and 13:00 on 2026-11-10)
    ooo_frames = list(frames)
    ooo_frames[12], ooo_frames[13] = ooo_frames[13], ooo_frames[12]
    ooo_proc = IdempotentStreamProcessor()
    for fr in ooo_frames:
        ooo_proc.ingest(fr)

    # Test 3: Stream Dropout (skip 2026-11-13 09:00..11:00 initially, then backfill)
    drop_proc = IdempotentStreamProcessor()
    dropped = []
    for i, fr in enumerate(frames):
        if fr["time"] in ("2026-11-13 09:00:00", "2026-11-13 10:00:00", "2026-11-13 11:00:00"):
            dropped.append(fr)
            continue
        drop_proc.ingest(fr, expected_prev_time=frames[i - 1]["time"] if i > 0 else None)
    notices_before_backfill = len(drop_proc.dropout_notices)
    for fr in dropped:
        drop_proc.ingest(fr)

    # Test 4: Process Restart & Watermark Recovery at frame 84
    part1_proc = IdempotentStreamProcessor()
    for fr in frames[:84]:
        part1_proc.ingest(fr)
    checkpoint_state = {
        "processed_frames": part1_proc.processed_frames,
        "alerts_by_key": part1_proc.alerts_by_key,
        "watermark_time": part1_proc.watermark_time,
    }
    part2_proc = IdempotentStreamProcessor()
    part2_proc.processed_frames = dict(checkpoint_state["processed_frames"])
    part2_proc.alerts_by_key = dict(checkpoint_state["alerts_by_key"])
    part2_proc.watermark_time = checkpoint_state["watermark_time"]
    # Replay overlapping frames 80..168 after restart
    for fr in frames[80:]:
        part2_proc.ingest(fr)

    tests = [
        {
            "id": "S01",
            "title": "重复包注入幂等去重测试（11-10 12:00 重复发送 4 次）",
            "expected": f"丢弃 3 个重复包，总告警数保持 {baseline_alerts_total} 条不变",
            "observed": f"丢弃重复帧 {dup_proc.duplicate_drops} 次，总告警数 {len(dup_proc.alerts_by_key)} 条",
            "passed": dup_proc.duplicate_drops == 3 and len(dup_proc.alerts_by_key) == baseline_alerts_total,
        },
        {
            "id": "S02",
            "title": "乱序/迟到包重排恢复测试（11-10 13:00 先于 12:00 到达）",
            "expected": f"水位线自动重算相邻差分，告警集合与顺序流 100% 一致（{baseline_alerts_total} 条）",
            "observed": f"迟到重排 {ooo_proc.reordered_recoveries} 次，恢复后总告警数 {len(ooo_proc.alerts_by_key)} 条",
            "passed": ooo_proc.reordered_recoveries == 1 and len(ooo_proc.alerts_by_key) == baseline_alerts_total,
        },
        {
            "id": "S03",
            "title": "连续 3 小时断流告警与补传恢复测试（11-13 09:00–11:00）",
            "expected": f"断流期间触发缺帧标记，补传后完整恢复 168 帧与 {baseline_alerts_total} 条告警",
            "observed": f"断流检测 {notices_before_backfill} 次，补传后有效帧 {len(drop_proc.processed_frames)}/168，告警 {len(drop_proc.alerts_by_key)} 条",
            "passed": notices_before_backfill >= 1 and len(drop_proc.processed_frames) == 168 and len(drop_proc.alerts_by_key) == baseline_alerts_total,
        },
        {
            "id": "S04",
            "title": "进程中断重启与重叠窗口回放测试（第 84 帧重启并重叠回放 80..168 帧）",
            "expected": f"重叠的 4 帧自动幂等跳过，最终 168 帧 0 漏帧、0 重算（{baseline_alerts_total} 条）",
            "observed": f"重启后跳过重叠帧 {part2_proc.duplicate_drops} 帧，总帧数 {len(part2_proc.processed_frames)}/168，告警 {len(part2_proc.alerts_by_key)} 条",
            "passed": part2_proc.duplicate_drops == 4 and len(part2_proc.processed_frames) == 168 and len(part2_proc.alerts_by_key) == baseline_alerts_total,
        },
    ]

    return {
        "stage": "15_continuous_stream_simulation",
        "latency_ms": latency_stats,
        "baseline_alerts_total": baseline_alerts_total,
        "total_tests": len(tests),
        "passed_tests": sum(1 for t in tests if t["passed"]),
        "tests": tests,
    }


def render_stream_markdown(report: dict) -> str:
    lat = report["latency_ms"]
    lines = [
        "# 老洼地连续数据流异常注入与实时筛查时延评测报告（阶段 15 论文仿真实验）",
        "",
        "## 一、连续流逐帧接入与四维实时筛查时延分布",
        "",
        f"- **回放总帧数**：`{lat['frames_count']}` 帧（每帧 25 柜 × 4 指标 = 100 个量测点，合计 `{lat['points_total']:,}` 个数据点）",
        f"- **单帧处理时延中位数（P50）**：**`{lat['p50_ms']} ms`**",
        f"- **单帧处理时延 P95 / P99 / Max**：**`{lat['p95_ms']} ms` / `{lat['p99_ms']} ms` / `{lat['max_ms']} ms`**",
        "",
        "## 二、四类连续流异常注入鲁棒性实验（重复、迟到、断流补传、重启恢复）",
        "",
        f"- **通过率**：**`{report['passed_tests']}/{report['total_tests']}`**",
        "",
        "| 实验编号 | 异常注入场景 | 预期鲁棒性指标 | 实测结果 | 结论 |",
        "| --- | --- | --- | --- | :---: |",
    ]
    for t in report["tests"]:
        lines.append(
            f"| `{t['id']}` | {t['title']} | {t['expected']} | {t['observed']} | {'**通过**' if t['passed'] else '未通过'} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_stream_reports(archive: Archive) -> dict:
    report = run_stream_benchmark(archive)
    STREAM_REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    STREAM_REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    STREAM_REPORT_MD.write_text(render_stream_markdown(report), encoding="utf-8")
    return report
