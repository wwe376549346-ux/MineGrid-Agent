"""Build the versioned answer key from the fixed archive. Never imported by inference."""
from contextlib import closing
from pathlib import Path
from statistics import mean, median
import hashlib
import json

from laowadi.diag_tools import DiagnosticToolbox
from laowadi.powerflow import run_ac_power_flow
from laowadi.queries import Archive, READINGS, evidence
from laowadi.source_files import document_manifest, verified_original
from laowadi.storage import DB, METRICS, ROOT, connect

HERE = Path(__file__).parent
ORIGINALS = ROOT.parent.parent / "项目知识库" / "资料" / "老洼地"
DAYS = [f"2026-11-{d:02}" for d in range(10, 17)]
IMAGES = ["35KV变电所.png", "中央变电所.png"]


def ref(row):
    e = evidence(row)
    return f'{e["file"]} / {e["sheet"]}!{e["cell"]}'


def field(key, label, value, unit="", refs=(), tol=0, choices=()):
    spec = {"key": key, "label": label, "unit": unit,
            "type": "number" if type(value) in (int, float) else "choice",
            "citation_required": bool(refs)}
    if choices:
        spec["choices"] = list(choices)
    return spec, {"value": value, "unit": unit, "tolerance": tol, "citations": list(refs)}


def opts(key, label, value, choices, refs=()):
    assert value in choices
    return field(key, label, value, refs=refs, choices=choices)


def build(db):
    cases = []
    answers = {}
    toolbox = DiagnosticToolbox(Archive(db))
    common = ("conditional", "insufficient")
    missing = ("underground_hourly", "outside_archive", "switch_soe", "nameplate",
               "time_sync", "wiring_meter", "downstream_meter", "transfer_limits", "none")
    alternatives = ("meter_or_polarity", "topology_or_operation", "downstream_backfeed",
                    "manifold_scan_skew", "confirmed_trip", "confirmed_hardware_failure")

    def rows(cab, metric, day=None, hour=None):
        sql = READINGS + " WHERE station='S35' AND cabinet=? AND metric=? AND value IS NOT NULL"
        params = [cab, metric]
        if day:
            sql += " AND day=?"
            params.append(day)
        if hour is not None:
            sql += " AND substr(time,12,5)=?"
            params.append(f"{hour:02}:00")
        return list(db.execute(sql + " ORDER BY time", params))

    def point(cab, metric, day, hour, key="reading"):
        row, = rows(cab, metric, day, hour)
        return field(key, f"{cab}柜 {hour:02}:00 {metric}", row["value"], METRICS[metric][1],
                     [ref(row)], 0.005)

    def add(cid, layer, cluster, question, fields=(), decision="answer", *,
            final_fields=None, final_decision=None, packet=None, oracle=None, extra_fields=()):
        turns = [{"question": question, "fields": [spec for spec, _ in fields]}]
        gold = [{"decision": decision, "fields": {s["key"]: a for s, a in fields}}]
        if final_fields is not None:
            turns.append({"question": "现收到下列演练补证。请重新判断原问题，并保留原始量测和适用边界。",
                          "fields": [s for s, _ in final_fields] + list(extra_fields), "supplement": packet})
            gold.append({"decision": final_decision, "fields": {s["key"]: a for s, a in final_fields}})
        case = {"id": cid, "layer": layer, "cluster": cluster, "turns": turns}
        cases.append(case)
        answers[cid] = {"turns": gold, "reference_method": oracle or "独立 SQLite 读取/人工规则标注，原件哈希已核对"}

    for index, (cab, metric, d, h) in enumerate([
        ("303", "Ia", 0, 8), ("306", "P", 1, 14), ("311", "Uab", 2, 9), ("312", "cos", 3, 20),
        ("1009", "P", 4, 7), ("1010", "Ia", 5, 16), ("1018", "P", 6, 21), ("1027", "cos", 0, 10),
    ], 1):
        day = DAYS[d]
        add(f"S{index:02}", "single", day, f"老洼地地面35kV站 {day} {h:02}:00，{cab}柜的 {metric} 原表读数是多少？给出单元格。",
            [point(cab, metric, day, h)])

    def stat(cab, metric, day, method, key, label, hour=None):
        data = rows(cab, metric, day, hour)
        if method in ("min", "max"):
            chosen = (min if method == "min" else max)(data, key=lambda r: r["value"])
            v, refs = chosen["value"], [ref(chosen)]
        else:
            v = (mean if method == "avg" else sum)(r["value"] for r in data)
            # Pin optional anchor provenance to the lowest and highest samples, which the analytics exposes.
            refs = [ref(f(data, key=lambda r: r["value"])) for f in (min, max)]
        unit = "kWh" if method == "sum" and metric == "P" else METRICS[metric][1]
        return field(key, label, round(v, 3), unit, list(dict.fromkeys(refs)), 0.011)

    for index, (d, cabs, metric, method, label) in enumerate([
        (0, ("303", "306"), "P", "max", "日整点最大有功"),
        (1, ("311", "312"), "Ia", "max", "日整点最大A相电流"),
        (2, ("1005", "1006"), "Uab", "min", "日整点最低线电压"),
        (3, ("303", "306"), "Ia", "avg", "日整点平均A相电流"),
        (4, ("1009", "1010"), "P", "sum", "整点矩形积分近似电量（ΣP×1小时）"),
        (5, ("1010", "1020"), "cos", "avg", "日整点平均功率因数"),
        (6, ("1012", "1018"), "P", "min", "日整点最小有功"),
    ], 1):
        day = DAYS[d]
        add(f"M{index:02}", "statistics", day,
            f"老洼地 S35 {day}，分别计算{cabs[0]}和{cabs[1]}柜全天24个整点的{label}。"
            + ("引用各柜最低和最高样本作为覆盖锚点。" if method in ("avg", "sum") else "引用各极值单元格。"),
            [stat(c, metric, day, method, f"cab_{c}", f"{c}柜{label}") for c in cabs])
    day, h = DAYS[3], 17
    vals = [rows(c, "P", day, h)[0] for c in ("303", "306", "311", "312")]
    add("M08", "statistics", day, f"S35 {day} {h}:00，按 (303+306)−(311+312) 计算有功差额，并引用四个输入。",
        [field("difference", "两组同期有功之差", sum(r["value"] for r in vals[:2]) -
               sum(r["value"] for r in vals[2:]), "kW", list(map(ref, vals)), 0.02)])

    neg_specs = [
        ("C1535", "井下1535中央变电所1101柜 2026-11-13 14:00 的实测有功，能否用地面1009同整点读数当作井下读数？", "underground_hourly"),
        ("C1535", "井下1535中央变电所1201柜 2026-11-15 12:00 的实测电流是多少？请按两张监控截图给出归档值。", "underground_hourly"),
        ("C1535", "地面1010与井下1201是图示同一路，据此给我 2026-11-14 09:00 井下1201的实测功率因数。", "underground_hourly"),
        ("outside", "S35 2026-11-17 09:00 的303柜实测Ia是多少？请勿用邻日数据。", "outside_archive"),
        ("outside", "请查 S35 2026-11-09 17:00 的311柜实测P。11月10日同小时可否代替？", "outside_archive"),
        (DAYS[3], "请确认 2026-11-13 12:00 老洼地图示35kV母联300当时实际合位，并说明证据缺口。", "switch_soe"),
        (DAYS[3], "现场有人说S35的303额定电流为200 A。仅依据现有日报与两截图，能证实这一铭牌数字吗？", "nameplate"),
        ("C1535", "便笺写着‘井下1101实测 700 kW’，无签名、原表和时点。现有归档能否核实并作为实测值发布？", "underground_hourly"),
    ]
    for index, (cluster, q, gap) in enumerate(neg_specs, 1):
        fields = [opts("gap", "最直接缺失的证据", gap, missing)]
        if index in (1, 2, 3):
            c = "1009" if index == 1 else "1010"
            q += "另给出一次系统图所示上游地面柜号，并引用两张图。"
            fields.append(opts("upstream", "图示上游地面柜", c, ("1009", "1010", "1027", "unknown"), IMAGES))
        add(f"N{index:02}", "abstention", cluster, q, fields, "insufficient")

    def diagnosis(cid, d, q, observations, hypothesis, gaps):
        add(cid, "attribution", DAYS[d], q,
            [*observations,
             opts("hypothesis", "优先复核的解释", hypothesis, alternatives),
             opts("gap", "最能区分解释的证据", gaps, missing),
             opts("confirmed", "是否已有现场根因定案", "unconfirmed", ("confirmed", "unconfirmed"))],
            "conditional", oracle="原表点位复算与保守归因规则。排序标签需独立值班员复核")

    for index, (d, h, c) in enumerate([(2, 12, "1027"), (2, 12, "1012")], 1):
        day = DAYS[d]
        diagnosis(f"D{index:02}", d,
                  f"老洼地 {day} {h}:00，306与312的负有功是反送电还是量测符号问题？核对两柜和同期{c}，说明这组读数能否定案。优先复核CT/PT极性、表计四象限及倍率，或直接把反送电视为已证实？",
                  [point(x, "P", day, h, f"p_{x}") for x in ("306", "312", c)],
                  "meter_or_polarity", "wiring_meter")
    for index, (d, h) in enumerate([(3, 15), (3, 18)], 3):
        day = DAYS[d]
        diagnosis(f"D{index:02}", d,
                  f"S35 {day} {h}:00，比较303、306同期有功。零读数和负荷集中可能来自运行方式变化或保护动作，最该查哪类证据？目前能定案吗？",
                  [point(c, "P", day, h, f"p_{c}") for c in ("303", "306")],
                  "topology_or_operation", "switch_soe")
    for index, (d, h, c) in enumerate([(6, 17, "311"), (0, 12, "306")], 5):
        day = DAYS[d]
        diagnosis(f"D{index:02}", d,
                  f"S35 {day} {h}:00，核对303和{c}有功。若作同步功率判定，先排查SCADA刷新时序，还是直接认定柜体故障？还缺什么？",
                  [point(x, "P", day, h, f"p_{x}") for x in ("303", c)],
                  "manifold_scan_skew", "time_sync")
    for index, (d, h) in enumerate([(1, 18), (6, 21)], 7):
        day = DAYS[d]
        diagnosis(f"D{index:02}", d,
                  f"S35 {day} {h}:00，核对1018有功和功率因数。负符号可能源于计量接线或真实反向潮流。优先复核计量链路需要什么？仅凭日报能定案吗？",
                  [point("1018", m, day, h, f"q_{m}") for m in ("P", "cos")],
                  "meter_or_polarity", "wiring_meter")

    for index, (scenario, d, hour, path, unit, label, boundary) in enumerate([
        ("base_case", 3, 17, ("network_summary", "bus_10kv_ii_kv"), "kV", "II段母线仿真电压", "1006"),
        ("base_case", 4, 17, ("network_summary", "total_loss_kw"), "kW", "全网仿真有功网损", "1006"),
        ("tap_sensitivity", 3, 17, ("tap_sensitivity", "recommended_tap", "bus_10kv_ii_kv"), "kV", "等值调压候选档对应II段仿真电压", "1006"),
        ("n1_contingency", 3, 17, ("n1_contingency", "receiving_1010_comparison", "after_i_a"), "A", "1009退出场景1010受电回路仿真电流", "1010"),
        ("n1_contingency", 4, 17, ("n1_contingency", "n1_summary", "bus_10kv_ii_kv"), "kV", "1009退出场景II段仿真电压", "1010"),
        ("reactive_opt", 3, 17, ("reactive_opt", "delta", "total_loss_reduction_kw"), "kW", "1010默认补偿情景的仿真总网损减少量", "1010"),
    ], 1):
        day = DAYS[d]
        args = {"scenario": scenario, "at_time": f"{day} {hour:02}:00:00"}
        sol = run_ac_power_flow(toolbox.archive, **args)
        assert sol["status"] == "ok", (args, sol)
        value = sol
        for key in path:
            value = value[key]
        boundary_metric = "Uab" if boundary == "1006" else "P"
        b = rows(boundary, boundary_metric, day, hour)[0]
        fields = [field("simulation", label, value, unit, [ref(b)], 0.005),
                  point(boundary, boundary_metric, day, hour, "boundary"),
                  opts("scope", "结果的适用边界", "equivalent_model", ("equivalent_model", "field_verified"))]
        if scenario == "n1_contingency":
            fields.append(opts("voltage", "仿真电压判定", str(sol["n1_contingency"]["voltage_compliant"]).lower(),
                               ("true", "false")))
        add(f"P{index:02}", "physics", day,
            f"老洼地 {day} {hour}:00，按项目默认等值参数做交流潮流 {scenario}，给出{label}及引用的{boundary}柜实测{boundary_metric}边界。"
            + ("报告本场景仿真电压是否通过。" if scenario == "n1_contingency" else "")
            + "说明结果适用边界。", fields, "conditional",
            oracle="冻结 pandapower 等值模型参考值，日报边界独立读取。用于求解器回执一致性")
    for cid, question, gap in [
        ("P07", "2026-11-13 17:00 的等值N-1交流潮流能否证明井下1200现实中具备转供条件？缺哪类资料？", "transfer_limits"),
        ("P08", "2026-11-14 17:00，能否用等值潮流报告的主变容量证明2号主变铭牌值？缺哪类资料？", "nameplate"),
    ]:
        fields = [opts("gap", "最直接缺项", gap, missing)]
        if cid == "P07":
            question += "请引用项目归档中明确要求一回退出后其余回路承担负荷的设计条款。"
            clause = "GB/T 50417-2017 3.0.3"
            fields.append(opts("clause", "归档设计条款", clause,
                               (clause, "GB/T 12325-2008", "unknown"), [clause]))
        add(cid, "physics", DAYS[3 if cid == "P07" else 4], question, fields, "insufficient")

    states = ("satisfied", "violated", "partial", "rejected", "conflict", "pending")
    for index, (cab, rated, derating, tie, station, source, status) in enumerate([
        ("303", 200, 0.9, None, "S35", True, "satisfied"),
        ("303", 90, 0.9, None, "S35", True, "violated"),
        ("303", 200, None, None, "S35", True, "partial"),
        ("303", 200, 0.9, None, "C1535", True, "rejected"),
        ("1010", 300, 0.9, "具备联络转供", "S35", True, "satisfied"),
        ("1010", 300, 0.9, "检修闭锁不可切换", "S35", True, "violated"),
        ("1010", 300, 0.9, "具备联络转供", "S35", False, "rejected"),
        ("1010", 300, 0.9, None, "S35", True, "partial"),
    ], 1):
        single = cab == "303"
        target = "V01" if single else "V02"
        source_doc = f"演练补证-H{index:02}" if source else ""
        params = {"303_rated_current_a" if single else "1009_1010_rated_current_a": rated}
        if derating is not None:
            params["ambient_derating_factor"] = derating
        if single:
            params.update(main_transformer_rated_kva=6300, switch_log_20261113="演练：306停运窗口由303供电")
        elif tie:
            params["c1535_tie_switch_mode"] = tie
        packet = {"station": station, "source_doc": source_doc, "parameters": params, "item": target}
        if single:
            peak = max((r for r in rows("303", "Ia", DAYS[3]) if 7 <= int(r["time"][11:13]) <= 19), key=lambda r: r["value"])
            actual, refs = peak["value"], [ref(peak)]
            subject = "2026-11-13 07:00至19:00 的303进线电流峰值及对应条件负载率"
        else:
            a = rows("1009", "Ia")
            b = rows("1010", "Ia")
            assert [r["time"] for r in a] == [r["time"] for r in b]
            source_a, source_b = max(zip(a, b), key=lambda pair: sum(r["value"] for r in pair))
            actual = source_a["value"] + source_b["value"]
            refs = list(map(ref, (source_a, source_b)))
            subject = "2026-11-10至16日1009与1010同期A相电流代数和峰值及假定单回转供负载率"
        observation = field("peak", "归档峰值", round(actual, 2), "A", refs, 0.005)
        final = [observation, opts("state", "补证后状态", status, states)]
        ratio = field("load_ratio", "前置完备时才填条件电流负载率", 0, "%", refs, 0.011)
        extra_fields = [ratio[0]]
        if status in ("satisfied", "violated"):
            ratio[1].update(value=round(actual / (rated * derating) * 100, 2), citations=refs + [source_doc])
            final.append(ratio)
            extra_fields = []
        add(f"H{index:02}", "hitl", target,
            f"老洼地 S35，复核{subject}。给出峰值与证据。还未收到明确来源的本次参数单，条件结论能否给出？",
            [observation, opts("state", "补证前状态", "pending", states)], "insufficient",
            final_fields=final, final_decision="conditional" if status in ("satisfied", "violated") else "insufficient",
            packet=packet, extra_fields=extra_fields, oracle="原表独立计算峰值及指定参数复算。补证包是研究者编写的演练输入")
    assert len(cases) == 48
    manifest = []
    for name, digest in document_manifest(db):
        verified_original(ORIGINALS, name, digest)
        manifest.append({"file": name, "sha256": digest})
    return {"version": "laowadi-48-v1", "sources": manifest, "cases": cases}, answers


if __name__ == "__main__":
    with closing(connect(DB)) as db:
        inputs, gold = build(db)
    for name, data in [("cases.json", inputs), ("gold.json", gold)]:
        path = HERE / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(name, hashlib.sha256(path.read_bytes()).hexdigest())
