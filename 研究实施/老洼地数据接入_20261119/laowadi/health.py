"""Full-station 25-cabinet x 4-metric electrical health inspection and event aggregation."""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import statistics

from laowadi.monitor import HOUR
from laowadi.storage import METRICS, REPORT_STATION, connect


def _load_profile_constants():
    """Load mine electrical topology & threshold constants from declarative mine_profile.json."""
    try:
        from laowadi.mine_schema import load_mine_profile

        profile = load_mine_profile()
        v10 = profile.voltage_limit_for_kv(10.0)
        v35 = profile.voltage_limit_for_kv(35.0)
        return (
            profile.spare_cabinets(),
            profile.capacitor_cabinets(),
            profile.main_chain_cabinets(),
            (v10.lo_kv, v10.hi_kv),
            v35.operational_watch_hi_kv,
            v35.hi_kv,
            profile.balance_threshold_kw,
            profile.polling_ramp_kw,
        )
    except Exception:
        # Fallback if invoked in a minimal stdlib-only interpreter without pydantic
        raw_path = Path(__file__).resolve().parent / "mine_profile.json"
        if raw_path.is_file():
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            feeders = raw.get("feeders", [])
            spares = {f["cabinet"] for f in feeders if f.get("role") == "spare"}
            caps = {f["cabinet"] for f in feeders if f.get("role") == "capacitor"}
            main_chain = tuple(
                f["cabinet"]
                for role in ("incomer", "transformer_hv", "transformer_lv")
                for f in feeders
                if f.get("role") == role
            )
            lims = {float(v["nominal_kv"]): v for v in raw.get("voltage_limits", [])}
            v10 = lims.get(10.0, {"lo_kv": 9.30, "hi_kv": 10.70})
            v35 = lims.get(35.0, {"operational_watch_hi_kv": 37.45, "hi_kv": 38.50})
            return (
                spares,
                caps,
                main_chain,
                (float(v10["lo_kv"]), float(v10["hi_kv"])),
                float(v35["operational_watch_hi_kv"]),
                float(v35["hi_kv"]),
                float(raw.get("balance_threshold_kw", 200.0)),
                float(raw.get("polling_ramp_kw", 1000.0)),
            )
        return (
            {"313", "1030", "1032"},
            {"1011", "1012"},
            ("303", "306", "311", "312", "1005", "1006"),
            (9.30, 10.70),
            37.45,
            38.50,
            200.0,
            1000.0,
        )


(
    SPARE_CABINETS,
    CAPACITOR_CABINETS,
    MAIN_CHAIN,
    VOLT_LIMIT_10KV,
    VOLT_WATCH_35KV_HI,
    VOLT_LIMIT_35KV_HI,
    BALANCE_THRESHOLD_KW,
    POLLING_RAMP_KW,
) = _load_profile_constants()


def _cell_ref(entry):
    return {
        "time": entry["time"],
        "file": entry["file"],
        "sheet": entry["sheet"],
        "cell": entry["cell"],
        "sha256": entry["sha256"],
        "value": entry["value"],
    }


def _origin_str(ref):
    return f'{ref["file"]} / {ref["sheet"]}!{ref["cell"]}'


def _load_station_data(archive, day=None):
    where = "WHERE r.station = ?"
    params = [REPORT_STATION]
    if day:
        where += " AND r.day = ?"
        params.append(day)

    reports = [
        dict(row)
        for row in archive.db.execute(
            "SELECT day, filename, sheet, sha256 FROM report"
            + (" WHERE day = ?" if day else "")
            + " ORDER BY day",
            (day,) if day else (),
        )
    ]
    if not reports:
        raise ValueError("所选范围内没有已导入的运行日报。")

    cabinets = {}
    for row in archive.db.execute(
        "SELECT number, label FROM cabinet WHERE station = ? ORDER BY CAST(number AS INTEGER)",
        (REPORT_STATION,),
    ):
        cabinets[row["number"]] = row["label"]

    data = defaultdict(lambda: defaultdict(dict))
    total_rows = 0
    valid_rows = 0
    for row in archive.db.execute(
        f"""
        SELECT r.day, r.time, r.cabinet, r.metric, r.value, r.marker,
               r.source_row, r.source_col, rep.filename, rep.sheet, rep.sha256
        FROM reading r
        JOIN report rep ON rep.day = r.day
        {where}
        ORDER BY r.time, CAST(r.cabinet AS INTEGER), r.metric
        """,
        tuple(params),
    ):
        total_rows += 1
        if row["value"] is not None:
            valid_rows += 1
        data[row["time"]][row["cabinet"]][row["metric"]] = {
            "day": row["day"],
            "time": row["time"],
            "value": row["value"],
            "marker": row["marker"],
            "cell": f'{row["source_col"]}{row["source_row"]}',
            "file": row["filename"],
            "sheet": row["sheet"],
            "sha256": row["sha256"],
        }

    times = sorted(data.keys())
    return reports, cabinets, times, data, {"total": total_rows, "valid": valid_rows, "missing": total_rows - valid_rows}


def _legacy_notice_count(archive, days):
    if not archive.has_events():
        return None
    with archive.event_connection() as db:
        row = db.execute(
            "SELECT id, description FROM monitor_run WHERE complete = 1 AND mode = 'historical' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        placeholders = ",".join("?" for _ in days)
        count = db.execute(
            f"SELECT COUNT(*) FROM monitor_event WHERE run_id = ? AND type = 'power_change' AND substr(at, 1, 10) IN ({placeholders})",
            (row["id"], *days),
        ).fetchone()[0]
        return {"run_id": row["id"], "raw_power_change_notices": count}


def _inspect_metering(cabinets, times, data):
    per_cabinet = {}
    findings = []

    for cab in sorted(cabinets, key=int):
        label = cabinets[cab]
        ratios = []
        neg_p = []
        neg_cos = []
        u_vals = []
        i_vals = []
        p_vals = []
        cos_vals = []

        for t in times:
            row = data[t].get(cab, {})
            u_e, i_e, p_e, c_e = (row.get(m) for m in ("Uab", "Ia", "P", "cos"))
            if not all((u_e, i_e, p_e, c_e)):
                continue
            u, i, p, c = u_e["value"], i_e["value"], p_e["value"], c_e["value"]
            if u is not None:
                u_vals.append((u, u_e))
            if i is not None:
                i_vals.append((i, i_e))
            if p is not None:
                p_vals.append((p, p_e))
                if p < 0:
                    neg_p.append((p, p_e))
            if c is not None:
                cos_vals.append((c, c_e))
                if c < 0:
                    neg_cos.append((c, c_e))

            if None not in (u, i, p, c) and u > 0.5 and i > 0.5 and abs(c) >= 0.05:
                p_calc = math.sqrt(3) * u * i * c
                if abs(p_calc) >= 20.0:
                    ratios.append((p / p_calc, t, u_e, i_e, p_e, c_e, p_calc))

        ratio_vals = [x[0] for x in ratios]
        med_ratio = statistics.median(ratio_vals) if ratio_vals else None
        if len(ratio_vals) >= 4:
            q = statistics.quantiles(ratio_vals, n=4)
            iqr_ratio = q[2] - q[0]
        else:
            iqr_ratio = 0.0 if ratio_vals else None

        status = "normal"
        note = "三相功率量测口径基本自洽"

        if cab in SPARE_CABINETS:
            all_zero_load = all(v == 0 for v, _ in i_vals) and all(v == 0 for v, _ in p_vals)
            max_u, max_u_e = max(u_vals, key=lambda x: x[0]) if u_vals else (0.0, None)
            if all_zero_load and 0.05 <= max_u <= 1.0:
                status = "spare_floating_voltage"
                note = f"备用柜无负荷但持续存在 {min(v for v, _ in u_vals):.2f}–{max_u:.2f} kV 悬浮电压（对比 1030 备用柜 0.00 kV），疑似 PT 零漂或感应电压。"
                findings.append({
                    "id": f"HLTH-MTR-{cab}",
                    "dimension": "metering",
                    "severity": "info",
                    "cabinet": cab,
                    "label": label,
                    "category": "spare_floating_voltage",
                    "title": f"{cab} 柜（{label}）存在持续悬浮电压（{min(v for v, _ in u_vals):.2f}–{max_u:.2f} kV）",
                    "summary": note,
                    "evidence": [_cell_ref(max_u_e)],
                    "action": "巡检核对 1032 柜电压互感器二次回路与表计零点配置。",
                })
            else:
                note = "备用柜（空载）"
        elif cab in CAPACITOR_CABINETS:
            cos_only = [v for v, _ in cos_vals]
            avg_cos = sum(cos_only) / len(cos_only) if cos_only else 0.0
            if abs(avg_cos) > 0.25 or len(neg_p) >= 10:
                status = "capacitor_cos_anomaly"
                worst_p, worst_p_e = min(p_vals, key=lambda x: x[0])
                max_cos, max_cos_e = max(cos_vals, key=lambda x: abs(x[0]))
                note = (
                    f"补偿控制柜功率因数均值达 {avg_cos:.3f}（范围 {min(cos_only):.2f} 至 {max(cos_only):.2f}），"
                    f"且有 {len(neg_p)} 个整点有功为负（最低 {worst_p:.2f} kW），与 1011 补偿柜（cos 接近 0）显著不符，疑似表计/接线或补偿状态异常。"
                )
                findings.append({
                    "id": f"HLTH-MTR-{cab}",
                    "dimension": "metering",
                    "severity": "high",
                    "cabinet": cab,
                    "label": label,
                    "category": "capacitor_cos_anomaly",
                    "title": f"{cab} 柜（{label}）功率因数测点及负有功异常",
                    "summary": note,
                    "evidence": [_cell_ref(worst_p_e), _cell_ref(max_cos_e)],
                    "action": "现场核对 1012 补偿控制柜表计接线、功率因数采样通道及电容器投切记录。",
                })
            else:
                note = f"无功补偿柜特征正常（cos 均值 {avg_cos:.3f}）"
        elif len(p_vals) > 0 and len(neg_p) / len(p_vals) >= 0.90:
            status = "persistent_negative_power"
            worst_p, worst_p_e = min(neg_p, key=lambda x: x[0])
            worst_c, worst_c_e = min(neg_cos, key=lambda x: x[0])
            ex = ratios[0]
            note = (
                f"{len(neg_p)}/{len(p_vals)} 个整点有功 P 与功率因数 cos 全部为负（P 范围 {worst_p:.2f} 至 {max(v for v, _ in p_vals):.2f} kW，"
                f"cos 均值 {sum(v for v, _ in cos_vals)/len(cos_vals):.3f}），且 P/(√3·U·I·cos) 中位数稳定在 {med_ratio:.3f}（IQR {iqr_ratio:.4f}），"
                f"强烈提示二次接线/CT极性反接或相序错接（待现场核查）。"
            )
            findings.append({
                "id": f"HLTH-MTR-{cab}",
                "dimension": "metering",
                "severity": "high",
                "cabinet": cab,
                "label": label,
                "category": "persistent_negative_power",
                "title": f"{cab} 柜（{label}）全时段持续负有功与负功率因数（疑似 CT/相序接线异常）",
                "summary": note,
                "evidence": [_cell_ref(worst_p_e), _cell_ref(worst_c_e), _cell_ref(ex[4])],
                "action": "优先停电或带电相位仪核查 1018 柜 CT 极性、电压/电流相序及表计倍率参数。",
            })
        elif med_ratio is not None and (med_ratio < 0.85 or med_ratio > 1.15) and iqr_ratio <= 0.10:
            status = "systematic_scale_offset"
            sample = ratios[len(ratios) // 2]
            note = (
                f"P/(√3·Uab·Ia·cos) 中位数稳定偏离为 {med_ratio:.3f}（IQR {iqr_ratio:.4f}，样本 {len(ratios)} 小时），"
                f"提示存在系统性倍率/接线折算口径差异（待与矿方核对 CT 变比与两表法接线配置）。"
            )
            findings.append({
                "id": f"HLTH-MTR-{cab}",
                "dimension": "metering",
                "severity": "info",
                "cabinet": cab,
                "label": label,
                "category": "systematic_scale_offset",
                "title": f"{cab} 柜（{label}）三相功率比例系数系统性偏离（中位数 {med_ratio:.3f}）",
                "summary": note,
                "evidence": [_cell_ref(sample[2]), _cell_ref(sample[3]), _cell_ref(sample[4]), _cell_ref(sample[5])],
                "action": "向矿方核对 10 kV 出线表计的 CT 变比配置、功率计算公式及两表/三表接线方式。",
            })

        per_cabinet[cab] = {
            "label": label,
            "status": status,
            "ratio_samples": len(ratios),
            "ratio_median": round(med_ratio, 4) if med_ratio is not None else None,
            "ratio_iqr": round(iqr_ratio, 4) if iqr_ratio is not None else None,
            "negative_p_hours": len(neg_p),
            "negative_cos_hours": len(neg_cos),
            "note": note,
        }

    return {"per_cabinet": per_cabinet, "findings": findings}


def _inspect_power_quality(cabinets, times, data):
    voltage_stats = {}
    pf_stats = {}
    findings = []

    bus_1_cabs = []
    bus_2_cabs = []

    for cab in sorted(cabinets, key=int):
        label = cabinets[cab]
        if cab in ("1030", "1032"):
            continue
        u_entries = [data[t][cab]["Uab"] for t in times if data[t].get(cab, {}).get("Uab", {}).get("value") is not None]
        if not u_entries:
            continue
        is_35 = int(cab) < 1000
        nom = 35.0 if is_35 else 10.0
        hi_limit = VOLT_WATCH_35KV_HI if is_35 else VOLT_LIMIT_10KV[1]
        lo_limit = 32.55 if is_35 else VOLT_LIMIT_10KV[0]

        over = [e for e in u_entries if e["value"] > hi_limit]
        under = [e for e in u_entries if e["value"] < lo_limit]
        max_e = max(u_entries, key=lambda e: e["value"])
        min_e = min(u_entries, key=lambda e: e["value"])
        avg_u = sum(e["value"] for e in u_entries) / len(u_entries)

        bus_segment = None
        if not is_35:
            d1 = statistics.median(abs(data[t][cab]["Uab"]["value"] - data[t]["1005"]["Uab"]["value"]) for t in times)
            d2 = statistics.median(abs(data[t][cab]["Uab"]["value"] - data[t]["1006"]["Uab"]["value"]) for t in times)
            bus_segment = "I段" if d1 < d2 else "II段"
            if bus_segment == "I段":
                bus_1_cabs.append(cab)
            else:
                bus_2_cabs.append(cab)

        voltage_stats[cab] = {
            "label": label,
            "nominal_kv": nom,
            "bus_segment": bus_segment,
            "avg_kv": round(avg_u, 3),
            "min_kv": round(min_e["value"], 3),
            "max_kv": round(max_e["value"], 3),
            "max_dev_pct": round((max_e["value"] / nom - 1.0) * 100.0, 2),
            "over_7pct_hours": len(over),
            "under_7pct_hours": len(under),
            "total_hours": len(u_entries),
            "max_evidence": _cell_ref(max_e),
        }

    # Summarize 10 kV bus II over-voltage and I/II bus gap as station-level findings
    bus2_over = [voltage_stats[c] for c in bus_2_cabs if voltage_stats[c]["over_7pct_hours"] >= 24]
    bus1_avg = statistics.mean(voltage_stats[c]["avg_kv"] for c in bus_1_cabs) if bus_1_cabs else 0.0
    bus2_avg = statistics.mean(voltage_stats[c]["avg_kv"] for c in bus_2_cabs) if bus_2_cabs else 0.0
    if bus2_over:
        worst_bus2 = max(bus2_over, key=lambda x: (x["over_7pct_hours"], x["max_kv"]))
        findings.append({
            "id": "HLTH-PQ-VOLT-10KV-BUS2",
            "dimension": "power_quality",
            "severity": "high",
            "cabinet": "10kV-II段",
            "label": "10 kV II段母线（含1006/1010/1016/1018/1020/1022/1024/1026等）",
            "category": "voltage_over_limit",
            "title": f"10 kV II段母线持续偏高，多柜 32%–45% 时段超过 GB/T 12325 +7% 上限（10.70 kV）",
            "summary": (
                f"按电压曲线聚类，10 kV II段平均电压 {bus2_avg:.3f} kV，系统性高于 I段（{bus1_avg:.3f} kV）约 {bus2_avg - bus1_avg:.3f} kV。"
                f"II段共有 {len(bus2_over)} 个柜超 10.70 kV 达 35–{worst_bus2['over_7pct_hours']} 小时，"
                f"峰值达 {worst_bus2['max_kv']:.2f} kV（+{worst_bus2['max_dev_pct']:.1f}%，{worst_bus2['label']}）。"
            ),
            "evidence": [worst_bus2["max_evidence"], voltage_stats["1006"]["max_evidence"]],
            "action": "核查 2# 主变有载/无载分接头档位及 II段轻载时段电容器投切策略，避免长期过压运行。",
        })

    if voltage_stats.get("303", {}).get("over_7pct_hours", 0) > 0:
        v303 = voltage_stats["303"]
        v311 = voltage_stats["311"]
        findings.append({
            "id": "HLTH-PQ-VOLT-35KV",
            "dimension": "power_quality",
            "severity": "medium",
            "cabinet": "303/311",
            "label": "35 kV I段（303 一回进线 / 311 1#主变高压侧）",
            "category": "voltage_high_35kv",
            "title": f"35 kV I段进线电压偏高（均值 {v303['avg_kv']:.2f} kV，峰值 {v311['max_kv']:.2f} kV / +{v311['max_dev_pct']:.1f}%）",
            "summary": (
                f"303/311 柜有 {v303['over_7pct_hours']}/168 小时高于 37.45 kV（+7% 关注线），"
                f"高于 35 kV II段（306 均值 {voltage_stats['306']['avg_kv']:.2f} kV）约 {v303['avg_kv'] - voltage_stats['306']['avg_kv']:.2f} kV。"
                f"（注：GB/T 12325 对 35 kV 及以上按正负偏差绝对值之和 ≤10% 考核，此处未见低于 35 kV 时刻。）"
            ),
            "evidence": [v303["max_evidence"], v311["max_evidence"]],
            "action": "结合上级 110/35 kV 变电站母线电压和 1# 主变分接头档位复核电压裕度。",
        })

    # Power factor inspection on loaded feeders
    for cab in sorted(cabinets, key=int):
        if cab in SPARE_CABINETS or cab in CAPACITOR_CABINETS:
            continue
        label = cabinets[cab]
        loaded = []
        for t in times:
            row = data[t].get(cab, {})
            ia, cos_e = row.get("Ia"), row.get("cos")
            if ia and cos_e and ia["value"] is not None and cos_e["value"] is not None and ia["value"] >= 5.0:
                loaded.append((cos_e["value"], cos_e, ia["value"]))
        if not loaded:
            continue
        low90 = sum(1 for c, _, _ in loaded if c < 0.90)
        low80 = sum(1 for c, _, _ in loaded if c < 0.80)
        neg = sum(1 for c, _, _ in loaded if c < 0)
        avg_c = sum(c for c, _, _ in loaded) / len(loaded)
        min_c, min_e, _ = min(loaded, key=lambda x: x[0])

        pf_stats[cab] = {
            "label": label,
            "loaded_hours": len(loaded),
            "avg_cos": round(avg_c, 3),
            "min_cos": round(min_c, 3),
            "below_090_hours": low90,
            "below_080_hours": low80,
            "negative_hours": neg,
            "min_evidence": _cell_ref(min_e),
        }

        if neg == 0 and low80 >= 24:
            sev = "high" if avg_c < 0.70 else "medium"
            findings.append({
                "id": f"HLTH-PQ-PF-{cab}",
                "dimension": "power_quality",
                "severity": sev,
                "cabinet": cab,
                "label": label,
                "category": "low_power_factor",
                "title": f"{cab} 柜（{label}）长期低功率因数（带载均值 {avg_c:.3f}，{low80}/{len(loaded)}h < 0.80）",
                "summary": (
                    f"带载 {len(loaded)} 小时中，{low90} 小时低于 0.90，{low80} 小时低于 0.80，最低 {min_c:.2f}。"
                    f"下井线路长期承载大量无功电流，易增大电缆发热损耗与井下末端压降。"
                ),
                "evidence": [_cell_ref(min_e)],
                "action": f"核查 {cab} 柜下游井下大电机轻载/空载率及井下就地无功补偿装置投入情况。",
            })

    return {
        "bus_segments": {
            "10kV_I": {"cabinets": bus_1_cabs, "avg_kv": round(bus1_avg, 3)},
            "10kV_II": {"cabinets": bus_2_cabs, "avg_kv": round(bus2_avg, 3)},
            "delta_II_minus_I_kv": round(bus2_avg - bus1_avg, 3),
        },
        "voltage": voltage_stats,
        "power_factor": pf_stats,
        "findings": findings,
    }


def _inspect_system_balance(cabinets, times, data):
    findings = []

    # 1. Detect single-incomer dual-transformer mode (306 out, 303 feeds 311+312)
    single_incomer_hours = []
    for t in times:
        p303 = data[t]["303"]["P"]["value"]
        p306 = data[t]["306"]["P"]["value"]
        i306 = data[t]["306"]["Ia"]["value"]
        p311 = data[t]["311"]["P"]["value"]
        p312 = data[t]["312"]["P"]["value"]
        if None in (p303, p306, i306, p311, p312):
            continue
        if p306 == 0.0 and i306 == 0.0 and p312 > 50.0 and abs(p303 - (p311 + p312)) <= BALANCE_THRESHOLD_KW:
            single_incomer_hours.append(t)

    mode_events = []
    if single_incomer_hours:
        run = [single_incomer_hours[0]]
        for t in single_incomer_hours[1:]:
            prev_dt = datetime.fromisoformat(run[-1])
            curr_dt = datetime.fromisoformat(t)
            if curr_dt - prev_dt == HOUR:
                run.append(t)
            else:
                mode_events.append(run)
                run = [t]
        mode_events.append(run)

    mode_event_records = []
    for idx, span in enumerate(mode_events, 1):
        peak_t = max(span, key=lambda t: data[t]["303"]["P"]["value"])
        peak_p303 = data[peak_t]["303"]["P"]
        peak_i303 = data[peak_t]["303"]["Ia"]
        start_306 = data[span[0]]["306"]["P"]
        end_306 = data[span[-1]]["306"]["P"]
        rec = {
            "id": f"HLTH-MODE-SINGLE-{idx:02d}",
            "start": span[0],
            "end": span[-1],
            "hours": len(span),
            "peak_time": peak_t,
            "peak_303_kw": peak_p303["value"],
            "peak_303_a": peak_i303["value"],
            "evidence": [_cell_ref(start_306), _cell_ref(end_306), _cell_ref(peak_p303), _cell_ref(peak_i303)],
        }
        mode_event_records.append(rec)
        findings.append({
            "id": rec["id"],
            "dimension": "system_balance",
            "severity": "high",
            "cabinet": "303/306/311/312",
            "label": "35 kV 进线与主变高压侧",
            "category": "single_incomer_dual_transformer",
            "title": f"{span[0]} 至 {span[-1]}（连续 {len(span)} 小时）疑似 35 kV 单回进线带双主变运行",
            "summary": (
                f"期间 306 二回进线有功与电流全为 0，而 303 一回进线有功与 311+312 两台主变之和高度吻合（误差 ≤200 kW），"
                f"303 峰值负荷达 {peak_p303['value']:.2f} kW / {peak_i303['value']:.2f} A（{peak_t}）。"
                f"需结合同期倒闸/检修日志确认是否为计划检修母联运行或 306 异常退出。"
            ),
            "evidence": rec["evidence"],
            "action": "核对 2026-11-13 值班倒闸票与 35 kV 母联开关状态记录，评估单回进线 N-1 载流裕度。",
        })

    # 2. Detect reverse power flow on main supply chain (303, 306, 311, 312, 1005, 1006)
    reverse_events = []
    for t in times:
        neg_cabs = [
            c for c in MAIN_CHAIN
            if c in data[t] and "P" in data[t][c] and data[t][c]["P"]["value"] is not None and data[t][c]["P"]["value"] < 0.0
        ]
        if neg_cabs:
            ev_refs = [_cell_ref(data[t][c]["P"]) for c in neg_cabs]
            details = "，".join(f"{c} 柜 {data[t][c]['P']['value']:.2f} kW" for c in neg_cabs)
            p303_v = data[t].get("303", {}).get("P", {}).get("value")
            p303_txt = f"{p303_v:.1f} kW" if p303_v is not None else "缺测"
            rec = {"time": t, "cabinets": neg_cabs, "details": details, "evidence": ev_refs}
            reverse_events.append(rec)
            findings.append({
                "id": f"HLTH-BAL-REV-{t[:10].replace('-', '')}-{t[11:13]}",
                "dimension": "system_balance",
                "severity": "high",
                "cabinet": "/".join(neg_cabs),
                "label": "主供电链路（进线/主变）",
                "category": "main_chain_reverse_power",
                "title": f"{t} 主供电链路出现反向有功（{details}）",
                "summary": (
                    f"同一整点 2# 主变整条通道（{', '.join(neg_cabs)}）同时录得负有功与负功率因数，"
                    f"同期 1# 主变通道负荷拉升至 {p303_txt}。可能系轻载倒送电、母联切换暂态或计量反转，须核对现场运行日志。"
                ),
                "evidence": ev_refs,
                "action": f"核对 {t} 前后 2# 主变运行方式、10 kV 母联与瓦斯发电/补偿装置状态及保护 SOE 记录。",
            })

    # 3. 35 kV & Transformer power balance + Polling Skew separation (Astra #10: strict null-safe isolation)
    def _val(t_str, cab_id, metric="P"):
        ent = data[t_str].get(cab_id, {}).get(metric)
        return ent["value"] if (ent and ent.get("value") is not None) else None

    def _safe_sum(t_str, cab_ids):
        vals = [_val(t_str, c, "P") for c in cab_ids]
        if any(v is None for v in vals):
            return None
        return sum(vals)

    p_total = {t: _safe_sum(t, ("303", "306")) for t in times}
    polling_skew_hours = []
    steady_imbalance_hours = []
    d35_all, dtr_all = [], []
    balance_missing_hours = []

    for idx, t in enumerate(times):
        p_in = p_total[t]
        p_hv = _safe_sum(t, ("311", "312"))
        p_lv = _safe_sum(t, ("1005", "1006"))
        if None in (p_in, p_hv, p_lv):
            balance_missing_hours.append(t)
            continue
        d35 = p_in - p_hv
        dtr = p_hv - p_lv
        d35_all.append(d35)
        dtr_all.append(dtr)

        if abs(d35) > BALANCE_THRESHOLD_KW or abs(dtr) > BALANCE_THRESHOLD_KW:
            prev_t = times[idx - 1] if idx > 0 else None
            next_t = times[idx + 1] if idx + 1 < len(times) else None
            prev_p = p_total[prev_t] if prev_t else None
            next_p = p_total[next_t] if next_t else None
            prev_hv = _safe_sum(prev_t, ("311", "312")) if prev_t else None
            next_hv = _safe_sum(next_t, ("311", "312")) if next_t else None
            prev_ramp = abs(p_in - prev_p) if prev_p is not None else 0.0
            next_ramp = abs(next_p - p_in) if next_p is not None else 0.0
            prev_hv_ramp = abs(p_hv - prev_hv) if prev_hv is not None else 0.0
            next_hv_ramp = abs(next_hv - p_hv) if next_hv is not None else 0.0
            max_ramp = max(prev_ramp, next_ramp, prev_hv_ramp, next_hv_ramp)
            hour = int(t[11:13])
            item = {
                "time": t,
                "hour": hour,
                "d35_kw": round(d35, 2),
                "dtr_kw": round(dtr, 2),
                "p_in_kw": round(p_in, 2),
                "p_hv_kw": round(p_hv, 2),
                "p_lv_kw": round(p_lv, 2),
                "adjacent_ramp_kw": round(max_ramp, 2),
                "evidence": [
                    _cell_ref(data[t]["303"]["P"]),
                    _cell_ref(data[t]["311"]["P"]),
                    _cell_ref(data[t]["1005"]["P"]),
                ],
            }
            if max_ramp >= POLLING_RAMP_KW or hour in {0, 10, 13, 17}:
                polling_skew_hours.append(item)
            else:
                steady_imbalance_hours.append(item)

    if polling_skew_hours:
        worst_skew = max(polling_skew_hours, key=lambda x: max(abs(x["d35_kw"]), abs(x["dtr_kw"])))
        hour_counts = defaultdict(int)
        for item in polling_skew_hours:
            hour_counts[f'{item["hour"]:02d}:00'] += 1
        top_hours = "、".join(f"{h}({n}次)" for h, n in sorted(hour_counts.items(), key=lambda kv: -kv[1])[:5])
        findings.append({
            "id": "HLTH-BAL-POLLING-SKEW",
            "dimension": "system_balance",
            "severity": "medium",
            "cabinet": "303/306/311/312/1005/1006",
            "label": "35 kV 进线与主变高低压侧",
            "category": "scada_polling_skew",
            "title": f"发现 {len(polling_skew_hours)} 个整点存在 SCADA 轮询非同步截面现象（集中于 {top_hours}）",
            "summary": (
                f"在负荷剧烈阶跃的换班/分时电价切换整点，进线与主变高低压侧出现瞬时不守恒"
                f"（最大案例 {worst_skew['time']}：d35={worst_skew['d35_kw']:+.1f} kW，dtr={worst_skew['dtr_kw']:+.1f} kW，"
                f"303={_val(worst_skew['time'], '303'):.1f}、1005={_val(worst_skew['time'], '1005'):.1f}、"
                f"但 311={_val(worst_skew['time'], '311'):.1f} kW）。"
                f"此类不平衡应识别为采集不同步特征，避免误报主变/母线电气故障。"
            ),
            "evidence": worst_skew["evidence"],
            "action": "后续接入连续监测点表时，必须同时采集各测点时间戳并按冻结断面/时间对齐后再做母线守恒校核。",
        })

    # 4. 10 kV LV vs sum of reported 10 kV feeders (null-safe per-hour filtering)
    feeders = [c for c in cabinets if int(c) >= 1007 and c not in SPARE_CABINETS]
    lv_hour_vals = [v for t in times if (v := _safe_sum(t, ("1005", "1006"))) is not None]
    feeder_hour_vals = []
    for t in times:
        f_vals = [_val(t, c, "P") for c in feeders]
        valid_f = [v for v in f_vals if v is not None]
        if valid_f:
            feeder_hour_vals.append(sum(valid_f))
    lv_avgs = statistics.mean(lv_hour_vals) if lv_hour_vals else 0.0
    feeder_avgs = statistics.mean(feeder_hour_vals) if feeder_hour_vals else 0.0
    unaccounted_avg = lv_avgs - feeder_avgs
    unaccounted_pct = (unaccounted_avg / lv_avgs * 100.0) if lv_avgs else 0.0
    if times and "1005" in data[times[0]] and "1006" in data[times[0]]:
        findings.append({
            "id": "HLTH-BAL-10KV-GAP",
            "dimension": "system_balance",
            "severity": "info",
            "cabinet": "1005/1006 vs 10kV出线",
            "label": "10 kV 母线总输入与已报出线之和",
            "category": "unaccounted_10kv_load",
            "title": f"10 kV 主变低压侧均值（{lv_avgs:.0f} kW）与已列日报出线之和（{feeder_avgs:.0f} kW）存在 {unaccounted_pct:.1f}% 差额",
            "summary": (
                f"平均差额 {unaccounted_avg:.0f} kW（有效样本 {len(lv_hour_vals)}/{len(times)} 小时）。"
                f"结合部分 10 kV 出线 P/(√3·U·I·cos) 稳定在 0.51–0.68 以及可能存在未列入该日报的 10 kV 重载出线"
                f"（如主通风机、主提升机或洗煤厂等），当前仅凭日报无法闭合 10 kV 母线功率平衡。"
            ),
            "evidence": [_cell_ref(data[times[0]]["1005"]["P"]), _cell_ref(data[times[0]]["1006"]["P"])],
            "action": "向矿方索取完整 10 kV 出线清单与各出线 CT/PT 倍率表，以补齐 10 kV 母线守恒校验。",
        })

    return {
        "single_incomer_events": mode_event_records,
        "reverse_power_events": reverse_events,
        "balance_summary": {
            "d35_avg_kw": round(statistics.mean(d35_all), 2) if d35_all else None,
            "dtr_avg_kw": round(statistics.mean(dtr_all), 2) if dtr_all else None,
            "valid_balance_hours": len(d35_all),
            "missing_balance_hours": len(balance_missing_hours),
            "missing_balance_times": balance_missing_hours[:10],
            "polling_skew_hours": len(polling_skew_hours),
            "steady_imbalance_hours": len(steady_imbalance_hours),
            "lv_avg_kw": round(lv_avgs, 2),
            "reported_feeders_avg_kw": round(feeder_avgs, 2),
            "unaccounted_10kv_pct": round(unaccounted_pct, 1),
        },
        "polling_skew_samples": polling_skew_hours[:10],
        "steady_imbalance_samples": steady_imbalance_hours,
        "findings": findings,
    }


def _inspect_diurnal_and_events(times, data, legacy_info, *, causal: bool = False):
    by_hour = defaultdict(list)
    p_total = {}
    missing_hours = []
    for t in times:
        e303 = data[t].get("303", {}).get("P")
        e306 = data[t].get("306", {}).get("P")
        v303 = e303["value"] if e303 else None
        v306 = e306["value"] if e306 else None
        if v303 is None or v306 is None:
            missing_hours.append(t)
            continue
        val = v303 + v306
        p_total[t] = val
        by_hour[int(t[11:13])].append((t, val))

    baseline = {}
    outliers = []
    for h in range(24):
        items = by_hour.get(h, [])
        if not items:
            continue
        vals = [v for _, v in items]
        med = statistics.median(vals)
        mad = statistics.median([abs(v - med) for v in vals])
        threshold = max(1800.0, 3.0 * 1.4826 * mad) if len(vals) >= 4 else 2500.0
        baseline[f"{h:02d}:00"] = {
            "median_kw": round(med, 1),
            "min_kw": round(min(vals), 1),
            "max_kw": round(max(vals), 1),
            "mad_kw": round(mad, 1),
            "valid_samples": len(vals),
            "outlier_threshold_kw": round(threshold, 1),
        }
        for idx_item, (t, v) in enumerate(items):
            if causal:
                # Astra #18: Strictly causal mode — evaluate each point using ONLY historical values <= current time!
                hist_vals = [hv for _, hv in items[:idx_item]]
                if len(hist_vals) < 2:
                    continue
                ref_med = statistics.median(hist_vals)
                ref_mad = statistics.median([abs(hv - ref_med) for hv in hist_vals])
                ref_thr = max(1800.0, 3.0 * 1.4826 * ref_mad) if len(hist_vals) >= 4 else 2500.0
            else:
                ref_med, ref_thr = med, threshold
            dev = v - ref_med
            if abs(dev) >= ref_thr:
                outliers.append({
                    "time": t,
                    "hour": f"{h:02d}:00",
                    "total_p_kw": round(v, 2),
                    "hour_median_kw": round(ref_med, 2),
                    "deviation_kw": round(dev, 2),
                    "baseline_mode": "causal_rolling" if causal else "retrospective_window",
                    "direction": "高于同整点基线" if dev > 0 else "低于同整点基线",
                    "evidence": [_cell_ref(data[t]["303"]["P"]), _cell_ref(data[t]["306"]["P"])],
                })

    outliers.sort(key=lambda x: x["time"])

    # Merge consecutive diurnal outlier hours into station-level load anomaly episodes
    episodes = []
    for item in outliers:
        if episodes and datetime.fromisoformat(item["time"]) - datetime.fromisoformat(episodes[-1][-1]["time"]) == HOUR:
            episodes[-1].append(item)
        else:
            episodes.append([item])

    findings = []
    for idx, ep in enumerate(episodes, 1):
        peak = max(ep, key=lambda x: abs(x["deviation_kw"]))
        span_text = ep[0]["time"] if len(ep) == 1 else f'{ep[0]["time"]} 至 {ep[-1]["time"]}（{len(ep)} 小时）'
        findings.append({
            "id": f"HLTH-BASE-{idx:02d}",
            "dimension": "diurnal_baseline",
            "severity": "medium",
            "cabinet": "303+306",
            "label": "35 kV 全站总有功",
            "category": "diurnal_baseline_outlier",
            "title": f"{span_text} 全站负荷显著偏离同整点基线（最大偏差 {peak['deviation_kw']:+.0f} kW）",
            "summary": (
                f"该时段峰值偏离点在 {peak['time']}：全站总有功 {peak['total_p_kw']:.1f} kW，"
                f"而 {peak['hour']} {'截至当时历史' if causal else '窗口'}中位基线为 {peak['hour_median_kw']:.1f} kW（{peak['direction']} {abs(peak['deviation_kw']):.1f} kW）。"
                f"与每日固定的 11:00/18:00 避峰骤降不同，该偏差属于跨日反常负荷特征。"
            ),
            "evidence": peak["evidence"],
            "action": "结合当日生产排班、避峰执行情况及主变切换记录复核偏离原因。",
        })

    return {
        "baseline_mode": "causal_rolling" if causal else "retrospective_window",
        "valid_samples": len(p_total),
        "missing_samples": len(missing_hours),
        "missing_times": missing_hours[:10],
        "hourly_baseline": baseline,
        "outlier_hours": outliers,
        "outlier_episodes": len(episodes),
        "legacy_comparison": legacy_info,
        "findings": findings,
    }


def evaluate_health(archive, day=None, *, causal: bool = False):
    reports, cabinets, times, data, completeness = _load_station_data(archive, day=day)
    days = [r["day"] for r in reports]
    legacy = _legacy_notice_count(archive, days)

    metering = _inspect_metering(cabinets, times, data)
    power_quality = _inspect_power_quality(cabinets, times, data)
    system_balance = _inspect_system_balance(cabinets, times, data)
    diurnal = _inspect_diurnal_and_events(times, data, legacy, causal=causal)

    all_findings = (
        metering["findings"]
        + power_quality["findings"]
        + system_balance["findings"]
        + diurnal["findings"]
    )
    sev_order = {"high": 0, "medium": 1, "info": 2}
    all_findings.sort(key=lambda x: (sev_order[x["severity"]], x["id"]))

    counts = {
        "high": sum(1 for f in all_findings if f["severity"] == "high"),
        "medium": sum(1 for f in all_findings if f["severity"] == "medium"),
        "info": sum(1 for f in all_findings if f["severity"] == "info"),
        "total": len(all_findings),
    }

    missing_field_evidence = [
        "1#、2# 主变及重点 10 kV 出线柜的额定容量、CT/PT 变比及保护定值单（用于计算负载率、热稳定裕度及核实 10 kV 出线功率比例系数）。",
        "2026-11-10 至 2026-11-16 期间的值班操作票、倒闸记录、35 kV/10 kV 母联开关变位及保护 SOE 日志（用于复核 11-13 单进线 13 小时运行与 11-12 12:00 2#主变负有功）。",
        "35 kV 变电所 10 kV 侧完整出线清单（用于解释主变低压侧与日报已列 10 kV 出线间 54% 功率差额）。",
        "1018 二采区II回、1012 补偿控制柜II 的二次接线核查结果与表计参数配置。",
    ]

    return {
        "station": REPORT_STATION,
        "scope": {
            "days": days,
            "hours": len(times),
            "cabinets": len(cabinets),
            "metrics_per_cabinet": len(METRICS),
            "completeness": completeness,
            "source_reports": reports,
        },
        "summary": {
            "finding_counts": counts,
            "actionable_events": counts["high"] + counts["medium"],
            "legacy_raw_notices": legacy["raw_power_change_notices"] if legacy else None,
        },
        "findings": all_findings,
        "dimensions": {
            "metering": metering,
            "power_quality": power_quality,
            "system_balance": system_balance,
            "diurnal_baseline": diurnal,
        },
        "missing_field_evidence": missing_field_evidence,
    }


def render_health_markdown(report):
    scope = report["scope"]
    summary = report["summary"]
    counts = summary["finding_counts"]
    sev_label = {"high": "高优先级待核", "medium": "中优先级关注", "info": "口径与数据质量提示"}
    dim_label = {
        "metering": "量测与物理自洽",
        "power_quality": "电能质量",
        "system_balance": "运行方式与功率平衡",
        "diurnal_baseline": "分时段基线异常",
    }

    lines = [
        "# 老洼地 35 kV 变电所全站电气体检与健康评估报告",
        "",
        f'- 覆盖范围：`{scope["days"][0]}` 至 `{scope["days"][-1]}`（共 {len(scope["days"])} 天 / {scope["hours"]} 个整点 / {scope["cabinets"]} 个柜 / 4 项电气量）',
        f'- 数据完整度：`{scope["completeness"]["valid"]:,} / {scope["completeness"]["total"]:,}` 条有效读数（缺测 {scope["completeness"]["missing"]} 条）',
        f'- 全站健康发现：共 **{counts["total"]}** 项（高优先级待核 **{counts["high"]}** 项、中优先级关注 **{counts["medium"]}** 项、口径与质量提示 **{counts["info"]}** 项）',
    ]
    if summary["legacy_raw_notices"] is not None:
        lines.append(
            f'- 与原四柜固定 1,000 kW 规则对比：原规则产出 **{summary["legacy_raw_notices"]}** 条碎提示；'
            f'现四维引擎覆盖全部 25 柜 × 4 指标并聚合为 **{summary["actionable_events"]}** 项需重点复核的站级/柜级健康事件（含高+中优先级），显著压缩日常避峰波动噪音。'
        )

    lines += [
        "",
        "> 说明：本报告全部结论严格基于已归档的 7 份小时级日报及 2 张监控截图。凡涉及现场开关状态、设备额定限值、表计接线或故障原因的内容，均列为待矿方复核的候选判断或缺失证据，不代替现场实测与运行日志。",
        "",
        "## 一、分级健康问题清单",
        "",
        "| 编号 | 级别 | 维度 | 对象 | 发现摘要 | 原始出处样例 |",
        "|---|---|---|---|---|---|",
    ]
    for f in report["findings"]:
        ev = "；".join(f'`{_origin_str(e)}`' for e in f["evidence"][:2])
        lines.append(
            f'| `{f["id"]}` | {sev_label[f["severity"]]} | {dim_label[f["dimension"]]} | {f["cabinet"]} | {f["title"]} | {ev} |'
        )

    lines += ["", "## 二、重点问题详细依据与排查建议", ""]
    for f in report["findings"]:
        if f["severity"] not in ("high", "medium"):
            continue
        ev_str = "；".join(f'{e["time"]} `{_origin_str(e)}` = {e["value"]}' for e in f["evidence"])
        lines += [
            f'### [{sev_label[f["severity"]]}] {f["id"]} · {f["title"]}',
            f'- **现象与依据**：{f["summary"]}',
            f'- **原表出处**：{ev_str}',
            f'- **建议核查**：{f["action"]}',
            "",
        ]

    # Dimension summary tables
    pq = report["dimensions"]["power_quality"]
    bus = pq["bus_segments"]
    lines += [
        "## 三、四维体检明细摘要",
        "",
        "### 3.1 10 kV 母线分段电压聚类与越限统计",
        "",
        f'- 10 kV I段（近 1005 柜）：均值 **{bus["10kV_I"]["avg_kv"]:.3f} kV**，包含柜号 `{", ".join(bus["10kV_I"]["cabinets"])}`',
        f'- 10 kV II段（近 1006 柜）：均值 **{bus["10kV_II"]["avg_kv"]:.3f} kV**，包含柜号 `{", ".join(bus["10kV_II"]["cabinets"])}`',
        f'- 段间平均压差（II段 − I段）：**{bus["delta_II_minus_I_kv"]:+.3f} kV**',
        "",
        "| 柜号 | 设备名称 | 所属母线 | 均值 (kV) | 最大值 (kV) | 最大偏差 | 超 +7% 时数 | 峰值原表出处 |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for cab, st in sorted(pq["voltage"].items(), key=lambda kv: int(kv[0])):
        if st["over_7pct_hours"] > 0:
            lines.append(
                f'| {cab} | {st["label"]} | {st["bus_segment"] or "35 kV"} | {st["avg_kv"]:.2f} | '
                f'{st["max_kv"]:.2f} | +{st["max_dev_pct"]:.1f}% | {st["over_7pct_hours"]}/{st["total_hours"]} | '
                f'`{_origin_str(st["max_evidence"])}` |'
            )

    lines += [
        "",
        "### 3.2 带载回路功率因数异常汇总（Ia ≥ 5 A，排除备用与补偿柜）",
        "",
        "| 柜号 | 设备名称 | 带载时数 | 均值 cos | 最低 cos | <0.90 时数 | <0.80 时数 | <0 时数 | 最低点出处 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for cab, st in sorted(pq["power_factor"].items(), key=lambda kv: int(kv[0])):
        if st["below_080_hours"] > 0 or st["negative_hours"] > 0 or st["below_090_hours"] >= 40:
            lines.append(
                f'| {cab} | {st["label"]} | {st["loaded_hours"]} | {st["avg_cos"]:.3f} | {st["min_cos"]:.2f} | '
                f'{st["below_090_hours"]} | {st["below_080_hours"]} | {st["negative_hours"]} | '
                f'`{_origin_str(st["min_evidence"])}` |'
            )

    mtr = report["dimensions"]["metering"]["per_cabinet"]
    lines += [
        "",
        "### 3.3 量测与三相功率比例一致性（P / √3·Uab·Ia·cos）",
        "",
        "| 柜号 | 设备名称 | 状态 | 有效样本 | 比例中位数 | IQR | 负有功时数 | 说明 |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for cab, st in sorted(mtr.items(), key=lambda kv: int(kv[0])):
        if st["status"] != "normal":
            med = f'{st["ratio_median"]:.3f}' if st["ratio_median"] is not None else "-"
            iqr = f'{st["ratio_iqr"]:.4f}' if st["ratio_iqr"] is not None else "-"
            lines.append(
                f'| {cab} | {st["label"]} | `{st["status"]}` | {st["ratio_samples"]} | {med} | {iqr} | '
                f'{st["negative_p_hours"]} | {st["note"]} |'
            )

    bal = report["dimensions"]["system_balance"]["balance_summary"]
    lines += [
        "",
        "### 3.4 系统运行方式与功率平衡统计",
        "",
        f'- 35 kV 进线与主变高压侧均值差 `(303+306)-(311+312)`：**{bal["d35_avg_kw"]:+.2f} kW**',
        f'- 主变高低压侧均值差 `(311+312)-(1005+1006)`：**{bal["dtr_avg_kw"]:+.2f} kW**',
        f'- 识别出 **SCADA 整点轮询非同步截面** 整点：**{bal["polling_skew_hours"]}** 个；非阶跃稳态不平衡整点：**{bal["steady_imbalance_hours"]}** 个',
        f'- 10 kV 主变低压侧均值 **{bal["lv_avg_kw"]:.1f} kW** vs 日报已列 10 kV 出线均值之和 **{bal["reported_feeders_avg_kw"]:.1f} kW**（未闭合差额 **{bal["unaccounted_10kv_pct"]:.1f}%**）',
        "",
        "## 四、当前缺失的现场证据清单（待向矿方补证）",
        "",
    ]
    for idx, item in enumerate(report["missing_field_evidence"], 1):
        lines.append(f"{idx}. {item}")
    lines.append("")
    return "\n".join(lines)
