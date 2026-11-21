"""End-to-end verification for the refactored Astryx workbench server, APIs, and review feedback loop."""

from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
from http.server import ThreadingHTTPServer
from importlib import import_module
import json
import math
from pathlib import Path
import sys
import time
import re
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote
from urllib.request import Request, urlopen

from fastapi.testclient import TestClient

from laowadi.api import create_app
from laowadi.monitor import EVENT_DB
from laowadi.security import issue_token
from laowadi.storage import DB, ROOT, connect
from laowadi.workbench import BUILD, SOURCE_DIR, Handler, Workbench


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch_json(base: str, path: str, payload: dict | None = None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = Request(base + path, data=data, headers=headers, method="POST" if payload is not None else "GET")
    with urlopen(req, timeout=15) as resp:
        assert resp.status == 200
        return json.loads(resp.read().decode("utf-8"))


def decode_named_events(body: str) -> list[dict]:
    events = []
    for frame in body.split("\n\n"):
        lines = frame.splitlines()
        data = [line[5:].lstrip() for line in lines if line.startswith("data:")]
        if data:
            event = json.loads("\n".join(data))
            assert f"event: {event['type']}" in lines, lines
            events.append(event)
    return events


def verify_sensitivity_voi(workbench: Workbench) -> dict:
    path = "/api/powerflow/sensitivity-voi"
    with TestClient(create_app(workbench, require_auth=True)) as client:
        headers = {"Authorization": f"Bearer {issue_token('workbench-voi', 'viewer')['access_token']}"}
        assert client.get(path).status_code == 401
        assert client.get(path, headers=headers, params={"at_time": "2026-11-13 17:15:00"}).status_code == 400
        assert client.get(path, headers=headers, params={"at_time": "2026-11-31 17:00:00"}).status_code == 400
        assert client.get(path, headers=headers, params={"target_cabinet": "unknown"}).status_code == 400
        assert client.get(path, headers=headers, params={"target_cabinet": "303"}).status_code == 400
        assert client.get(path, headers=headers, params={"target_cabinet": "9999"}).status_code == 404
        assert client.get(path, headers=headers, params={"at_time": "2026-11-17 17:00:00"}).status_code == 404
        for cabinet in ("1009", "1020"):
            unsupported = client.get(path, headers=headers, params={"target_cabinet": cabinet})
            assert unsupported.status_code == 422 and "1010" in unsupported.json()["error"]
        with patch("laowadi.powerflow.analyze_parameter_sensitivity_voi", None):
            assert client.get(path, headers=headers).status_code == 503
        response = client.get(path, headers=headers)
        assert response.status_code == 200, response.text
        result = response.json()
        explicit = client.get(path, headers=headers, params={"at_time": "2026-11-13T17:00:00", "target_cabinet": "1010"})
        assert explicit.status_code == 200 and explicit.json() == result

    assert result["status"] == "ok" and result["at_time"] == "2026-11-13 17:00:00"
    assert result["target_cabinet"] == "1010"
    for field in ("bus_10kv_ii_kv", "n1_min_ug_kv", "n1_current_a", "total_loss_kw"):
        assert math.isfinite(result["baseline"][field]) and result["baseline"][field] > 0
    items, ranking = result["tornado_items"], result["voi_ranking"]
    assert len(items) >= 2 and len(items) == len(ranking)
    assert len({item["param_key"] for item in items}) == len(items)
    scores = [item["voi_score"] for item in items]
    assert scores == sorted(scores, reverse=True) and 0 <= scores[-1] <= scores[0] <= 100
    fields = (
        "base_value", "low_value", "high_value", "low_delta_u_kv", "high_delta_u_kv", "voltage_span_kv",
        "low_delta_n1_i_a", "high_delta_n1_i_a", "n1_current_span_a",
        "low_delta_loss_kw", "high_delta_loss_kw", "loss_span_kw",
    )
    for rank, (item, row) in enumerate(zip(items, ranking), 1):
        assert row["rank"] == item["priority_rank"] == rank
        assert all(item[key] == row[key] for key in ("param_key", "label", "voi_score", "required_field_document", "rationale"))
        assert item["unit"] and item["perturbation_label"] and row["required_field_document"] and row["dominant_impact"]
        assert all(math.isfinite(item[k]) for k in fields)
    assert ranking[0]["label"] in result["summary"]
    return result


def verify_autonomous_endpoints(workbench: Workbench) -> dict:
    agent_path, sandbox_path = "/api/agent/stream", "/api/sandbox/run"
    agent_module, sandbox_module = "laowadi.autonomous_agent", "laowadi.code_sandbox"
    question = "用老洼地全周数据分析 1006 柜有功功率与线电压的相关性，给出散点图。"
    code = "print(6 * 7)"
    app = create_app(workbench, require_auth=True)

    def auth(subject, role):
        return {"Authorization": f"Bearer {issue_token(subject, role)['access_token']}"}

    dispatcher, engineer = auth("test-agent", "dispatcher"), auth("test-sandbox", "engineer")
    expected = [
        {"type": "plan_update", "items": [{"id": "archive", "title": "查询原表", "status": "running"}]},
        {"type": "reasoning_delta", "delta": "按柜号和整点查归档。"},
        {"type": "tool_start", "tool": "get_hourly_reading", "step": 1},
        {"type": "tool_end", "tool": "get_hourly_reading", "step": 1, "status": "ok"},
        {"type": "code_run", "code": code, "result": {"stdout": "42\n", "stderr": ""}},
        {"type": "content_delta", "delta": "查询完成。"},
        {"type": "verification_badge", "business_trusted": True, "status": "verified"},
        {"type": "done", "session_id": "test-agent-stream"},
    ]

    async def verify_arrival_before_completion():
        first_sent = threading.Event()
        sent = bytearray()
        request_read = False
        body = json.dumps({"question": question, "sessionId": "test-agent-stream"}).encode()

        model_client = object()

        def run_agent(archive, task, *, client, session, max_steps, memory_db_path, on_event):
            assert ("S35", "1006") in archive.devices and task == question
            assert client is model_client and session is not None and max_steps == 8
            assert memory_db_path.parent == workbench.sessions_db.parent
            on_event(expected[0])
            assert first_sent.wait(timeout=3), "Agent SSE buffered the first frame until completion"
            for event in expected[1:]:
                on_event(event)

        async def receive():
            nonlocal request_read
            if not request_read:
                request_read = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == "http.response.start":
                assert message["status"] == 200
                headers = dict(message["headers"])
                assert headers[b"content-type"].startswith(b"text/event-stream")
                assert headers[b"x-accel-buffering"] == b"no"
            elif message.get("body"):
                sent.extend(message["body"])
                if not first_sent.is_set():
                    assert decode_named_events(sent.decode()) == expected[:1]
                    first_sent.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "POST", "scheme": "http", "path": agent_path,
            "raw_path": agent_path.encode(), "query_string": b"", "root_path": "",
            "server": ("testserver", 80), "client": ("testclient", 1),
            "headers": [
                (b"host", b"testserver"), (b"content-type", b"application/json"),
                (b"authorization", dispatcher["Authorization"].encode()),
            ],
        }
        with (patch.dict(sys.modules, {agent_module: SimpleNamespace(run_autonomous_agent=run_agent)}),
              patch("laowadi.api.ChatClient.from_env", return_value=model_client)):
            await asyncio.wait_for(app(scope, receive, send), timeout=5)
        assert first_sent.is_set() and decode_named_events(sent.decode()) == expected

    asyncio.run(verify_arrival_before_completion())

    with TestClient(app) as client:
        assert client.post(agent_path, json={"question": question}).status_code == 401
        assert client.post(sandbox_path, headers=dispatcher, json={"code": code}).status_code == 403
        assert client.post(agent_path, headers=dispatcher, json={"question": " ", "maxSteps": 0}).status_code == 400
        assert client.post(sandbox_path, headers=engineer, json={"code": code, "timeout_s": 0}).status_code == 400
        with patch.dict(sys.modules, {agent_module: SimpleNamespace(), sandbox_module: None}):
            assert client.post(agent_path, headers=dispatcher, json={"question": question}).status_code == 503
            assert client.post(sandbox_path, headers=engineer, json={"code": code}).status_code == 503

        def run_sandbox(archive, script, *, timeout_s):
            assert ("S35", "1006") in archive.devices and script == code and timeout_s == 6.0
            return {"stdout": "42\n", "stderr": ""}

        with TestClient(create_app(workbench, require_auth=True, rate_limit_per_min=1)) as limited:
            with patch.dict(sys.modules, {sandbox_module: SimpleNamespace(run_python_analysis=run_sandbox)}):
                headers = auth("test-sandbox-limited", "engineer")
                response = limited.post(sandbox_path, headers=headers, json={"code": code})
                assert response.status_code == 200 and response.json() == {"stdout": "42\n", "stderr": ""}
                assert limited.post(sandbox_path, headers=headers, json={"code": code}).status_code == 429

        coverage = {}
        for module, entrypoint, path, headers, payload in (
            (agent_module, "run_autonomous_agent", agent_path, dispatcher, {"question": question, "mode": "local"}),
            (sandbox_module, "run_python_analysis", sandbox_path, engineer, {"code": code}),
        ):
            try:
                ready = callable(getattr(import_module(module), entrypoint, None))
            except ImportError:
                ready = False
            response = client.post(path, headers=headers, json=payload)
            if not ready:
                assert response.status_code == 503, response.text
                coverage[path] = "503 (runner unavailable)"
                continue
            assert response.status_code == 200, response.text
            if path == agent_path:
                events = decode_named_events(response.text)
                assert events[-1]["type"] == "done" and all(event["type"] != "error" for event in events), events
                assert {event["type"] for event in events} == {event["type"] for event in expected}, events
                run = next(event for event in events if event["type"] == "code_run")
                assert run["status"] == "ok" and run["tables"] and run["charts"][0]["chart_type"] == "scatter", run
            else:
                assert response.json()["stdout"].strip() == "42", response.text
            coverage[path] = "end-to-end"
        return coverage


def verify_memory_and_research_endpoints(workbench: Workbench) -> dict:
    memory_path = "/api/memory"
    stream_path = "/api/deep-research/stream"
    reports_path = "/api/deep-research/reports"
    missing_report = reports_path + "/DR-999999/export"
    missing_memory = memory_path + "/MEM-999999"
    dispatcher = {"Authorization": f"Bearer {issue_token('test-memory', 'dispatcher')['access_token']}"}
    viewer = {"Authorization": f"Bearer {issue_token('test-memory-reader', 'viewer')['access_token']}"}
    threshold = {"metric": "Uab", "operator": ">", "value": 10.65, "unit": "kV"}
    rule = {
        "category": "semantic",
        "title": "1006 柜过压红线复验",
        "content": "验收用的老洼地 1006 柜线电压上限，取 10.65 kV。",
        "scope_cabinet": "1006",
        "metric_threshold": threshold,
        "tags": ["过压", "1006"],
    }
    goal = "全周排查老洼地运行风险，重点核查 1006 柜过压与转供条件 <b>验收样例</b>"
    task = {"goal": goal, "mode": "local"}
    coverage = {}
    with TestClient(create_app(workbench, require_auth=True)) as client:
        unavailable = {"laowadi.memory_store": None, "laowadi.deep_research": None}
        with patch.dict(sys.modules, unavailable):
            assert client.get(memory_path, headers=viewer).status_code == 503
            assert client.post(memory_path, headers=dispatcher, json=rule).status_code == 503
            assert client.patch(missing_memory, headers=dispatcher, json={"enabled": False}).status_code == 503
            assert client.delete(missing_memory, headers=dispatcher).status_code == 503
            assert client.post(stream_path, headers=dispatcher, json=task).status_code == 503
            assert client.get(reports_path, headers=viewer).status_code == 503
            assert client.get(missing_report, headers=viewer).status_code == 503

        assert client.get(memory_path).status_code == 401
        assert client.post(memory_path, headers=viewer, json=rule).status_code == 403
        assert client.post(stream_path, headers=viewer, json=task).status_code == 403
        assert client.post(stream_path, headers=dispatcher, json={"goal": " "}).status_code == 400
        assert client.get(memory_path, headers=viewer, params={"category": "invalid"}).status_code == 400
        assert client.post(memory_path, headers=dispatcher,
                           json={**rule, "metric_threshold": {**threshold, "operator": "<"}}).status_code == 400

        listed = client.get(memory_path, headers=viewer)
        if listed.status_code == 503:
            coverage[memory_path] = "503 (runner unavailable)"
            memory_id = None
        else:
            assert listed.status_code == 200 and listed.json()["memories"] == []
            created = client.post(memory_path, headers=dispatcher, json=rule)
            assert created.status_code == 201, created.text
            memory = created.json()["memory"]
            memory_id = memory["id"]
            path = f"{memory_path}/{memory_id}"
            assert memory["source"] == "test-memory" and memory["enabled"]
            assert all(memory[field] == value for field, value in rule.items())
            assert client.get(memory_path, headers=viewer, params={"category": "episodic"}).json()["memories"] == []
            assert client.patch(path, headers=viewer, json={"enabled": False}).status_code == 403
            assert client.patch(path, headers=dispatcher, json={}).status_code == 400
            changed = client.patch(path, headers=dispatcher, json={"content": rule["content"] + " 已复核。", "enabled": False})
            assert changed.status_code == 200 and changed.json()["memory"]["content"].endswith("已复核。")
            assert changed.json()["memory"]["enabled"] is False
            assert client.get(memory_path, headers=viewer, params={"enabled_only": True}).json()["memories"] == []
            assert client.get(memory_path, headers=viewer).json()["memories"] == [changed.json()["memory"]]
            assert client.patch(path, headers=dispatcher, json={"enabled": True}).json()["memory"]["enabled"]
            coverage[memory_path] = "end-to-end"

        response = client.post(stream_path, headers=dispatcher, json=task)
        if response.status_code == 503:
            coverage[stream_path] = "503 (runner unavailable)"
        else:
            assert response.status_code == 200, response.text
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["x-accel-buffering"] == "no"
            events = decode_named_events(response.text)
            assert events[0]["type"] == "plan_update" and events[-1]["type"] == "done", events[-1]
            assert {e["type"] for e in events} == {
                "plan_update", "stage_update", "reasoning_delta", "finding", "section_ready", "done",
            }
            report = events[-1]["result"]
            assert report["goal"] == goal and report["mode"] == "local" and len(report["sections"]) == 6
            assert len(report["evidence_cells"]) >= 4 and all(step["status"] == "done" for step in report["plan"])
            if memory_id:
                assert memory_id in {item["id"] for item in report["applied_memories"]}
                assert any(f["id"].startswith("F-MEM-") and f["metrics"]["threshold_kv"] == 10.65
                           and f["evidence_cells"] for f in report["findings"])
            listed_reports = client.get(reports_path, headers=viewer)
            report_id = report["report_id"]
            assert listed_reports.status_code == 200 and listed_reports.json()["reports"][0]["report_id"] == report_id
            export_path = f"{reports_path}/{report_id}/export"
            markdown = client.get(export_path, headers=viewer)
            html = client.get(export_path, headers=viewer, params={"format": "html"})
            assert markdown.status_code == html.status_code == 200
            assert markdown.headers["content-type"].startswith("text/markdown") and markdown.text == report["markdown"]
            assert html.headers["content-type"].startswith("text/html") and "@media print" in html.text
            assert "&lt;b&gt;验收样例&lt;/b&gt;" in html.text and "<b>验收样例</b>" not in html.text
            for download, extension in ((markdown, "md"), (html, "html")):
                assert download.headers["content-disposition"].startswith("attachment;")
                assert f"{report_id}.{extension}" in download.headers["content-disposition"]
            assert client.get(export_path, headers=viewer, params={"format": "pdf"}).status_code == 400
            assert client.get(missing_report, headers=viewer).status_code == 404
            coverage[stream_path] = "end-to-end"
            coverage[reports_path] = ["md", "html"]

        if memory_id:
            assert client.delete(path, headers=viewer).status_code == 403
            assert client.delete(path, headers=dispatcher).json()["deleted"] is True
            assert client.delete(path, headers=dispatcher).status_code == 404
            assert client.patch(path, headers=dispatcher, json={"enabled": False}).status_code == 404
            assert client.get(memory_path, headers=viewer).json()["memories"] == []
    return coverage


def main():
    db_before = sha256(DB)
    ev_before = sha256(EVENT_DB)
    assert (BUILD / "index.html").is_file(), "dist/index.html missing; run npm run build first."

    with TemporaryDirectory() as tmp:
        runtime = Path(tmp)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.app = Workbench(
            DB, EVENT_DB, SOURCE_DIR, model="deepseek-v4-pro",
            reviews_db=runtime / "reviews.sqlite3", sessions_db=runtime / "sessions.sqlite3",
            audit_db=runtime / "audit.sqlite3", jobs_db=runtime / "jobs.sqlite3",
            database_url=f"sqlite+pysqlite:///{runtime / 'enterprise.sqlite3'}",
        )
        port = server.server_port
        base = f"http://127.0.0.1:{port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # 1. Static bundle check
            with urlopen(base + "/", timeout=10) as resp:
                html = resp.read().decode("utf-8")
                assert resp.status == 200 and "老洼地煤矿" in html
            assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)
            assert len(assets) >= 2, f"Expected JS and CSS bundle assets, got {assets}"
            assert any(asset.endswith(".js") for asset in assets) and any(asset.endswith(".css") for asset in assets)
            for asset in assets:
                with urlopen(base + asset, timeout=10) as resp:
                    assert resp.status == 200 and resp.read(), f"Missing or empty asset: {asset}"
            flow_assets = list((BUILD / "assets").glob("PowerFlowPanel-*.js"))
            assert len(flow_assets) == 1, f"Expected one compiled PowerFlowPanel chunk, got {flow_assets}"
            with urlopen(base + "/assets/" + flow_assets[0].name, timeout=10) as resp:
                flow_bundle = resp.read().decode("utf-8")
                assert resp.status == 200
                assert "/api/powerflow/sensitivity-voi" in flow_bundle and "参数灵敏度龙卷风图" in flow_bundle
            sensitivity = verify_sensitivity_voi(server.app)
            autonomous_endpoints = verify_autonomous_endpoints(server.app)
            research_endpoints = verify_memory_and_research_endpoints(server.app)

            # 2. /api/dates
            dates = fetch_json(base, "/api/dates")
            assert len(dates["dates"]) == 7 and dates["model"] == "deepseek-v4-pro"

            # 3. /api/health (7-day & single-day)
            health = fetch_json(base, "/api/health")
            assert health["scope"]["cabinets"] == 25
            assert len(health["cabinets_cards"]) == 25
            assert health["summary"]["finding_counts"]["high"] == 7
            day_health = fetch_json(base, "/api/health?day=2026-11-13")
            assert any(f["id"] == "HLTH-MODE-SINGLE-01" for f in day_health["findings"])

            # 4. /api/cabinet
            cab1018 = fetch_json(base, "/api/cabinet?cabinet=1018")
            assert cab1018["device"]["number"] == "1018"
            assert len(cab1018["series"]["P"]) == 168
            assert cab1018["metering_check"]["status"] == "persistent_negative_power"

            # 5. /api/diagrams & /api/image
            diagrams = fetch_json(base, "/api/diagrams")["diagrams"]
            assert len(diagrams) == 2
            with urlopen(base + diagrams[0]["image_url"], timeout=10) as resp:
                png = resp.read()
                assert resp.status == 200 and png[:8] == b"\x89PNG\r\n\x1a\n"

            # 6. Legacy /api/day & /api/ask
            day10 = fetch_json(base, "/api/day?date=2026-11-10")
            lw10 = next(e for e in day10["events"] if e["id"] == "LW-000010")
            assert lw10["cabinet"] == "303"
            ask = fetch_json(base, "/api/ask", {
                "eventId": "LW-000010",
                "question": "306柜同期也变化了吗？",
                "mode": "local",
            })
            assert ask["answer"]["comparisons"][0]["change_kw"] == -204.41

            # 7. Human-in-the-loop review persistence + injection into /api/diagnose
            saved = fetch_json(base, "/api/reviews", {
                "finding_id": "HLTH-MODE-SINGLE-01",
                "disposition": "normal_operation",
                "switch_state": "35kV母联300合位、306检修退出",
                "operator_note": "11月13日07:00-19:00计划检修倒闸",
            })
            assert saved["saved"]["disposition"] == "normal_operation"
            health_after = fetch_json(base, "/api/health")
            assert health_after["reviews"]["HLTH-MODE-SINGLE-01"]["disposition"] == "normal_operation"

            diag = fetch_json(base, "/api/diagnose", {
                "question": "2026年11月13日35kV站运行有哪些异常？",
                "mode": "local",
            })
            assert diag["status"] == "ok" and diag["verification"]["status"] == "verified"
            assert any(
                m["finding_id"] == "HLTH-MODE-SINGLE-01"
                for m in diag["report"].get("operator_memory_applied", [])
            ), "Saved operator review was not reused in diagnosis!"

            # 8. Verify /api/supplement (GET & POST) and /api/evaluation
            supp_get = fetch_json(base, "/api/supplement")
            assert supp_get["baseline_v1"]["version"] == 1
            assert supp_get["supplemented_v2"]["version"] == 2
            assert supp_get["supplemented_v2"]["parent_run_id"] == supp_get["baseline_v1"]["run_id"]

            supp_post = fetch_json(base, "/api/supplement", {
                "station": "S35",
                "source_doc": "界面局部复算测试单",
                "parameters": {
                    "303_rated_current_a": 220.0,
                    "main_transformer_rated_kva": 6300.0,
                    "ambient_derating_factor": 0.90,
                    "switch_log_20261113": "35kV母联300合位",
                },
            })
            assert supp_post["supplemented_v2"]["summary"]["recalculated_items"] == 2
            assert supp_post["supplemented_v2"]["summary"]["reused_items"] == 2
            assert supp_post["supplemented_v2"]["summary"]["conditionally_satisfied"] == 1

            eval_get = fetch_json(base, "/api/evaluation")
            assert eval_get["evaluation"]["layer2_corpus_and_boundaries"]["passed_cases"] == 9
            assert eval_get["evaluation"]["layer5_gaia_diagnostic_agent_6"]["passed"]["model"] == 6
            assert eval_get["benchmark_36"]["total_passed"] == 36
            assert eval_get["benchmark_36"]["total_cases"] == 36

            # 8b. Verify /api/timeline for 168-hour Single-Line Diagram dynamic power flow replay & exact peak bookmarks
            tl = fetch_json(base, "/api/timeline")
            assert len(tl["times"]) == 168
            assert len(tl["cabinets"]) == 25
            assert len(tl["bookmarks"]) == 5
            assert tl["cabinets"]["303"]["P"][12] == 4479.96
            assert tl["cabinets"]["303"]["cells"]["P"][12] == "D16"
            bm_by_id = {b["id"]: b for b in tl["bookmarks"]}
            assert tl["cabinets"][bm_by_id["bm-overvolt"]["focus_cabinet"]]["Uab"][bm_by_id["bm-overvolt"]["index"]] == 11.01
            assert tl["cabinets"]["1018"]["P"][bm_by_id["bm-1018neg"]["index"]] == -104.88
            assert tl["cabinets"]["303"]["P"][bm_by_id["bm-single303"]["index"]] == 5710.12
            assert tl["cabinets"]["306"]["P"][bm_by_id["bm-single303"]["index"]] == 0.0

            # Anti-regression: ensure SingleLineDiagram.jsx has zero non-existent cabinet numbers
            sld_src = (ROOT / "workbench" / "src" / "SingleLineDiagram.jsx").read_text(encoding="utf-8")
            with closing(connect(DB)) as _db:
                valid_all = {r[0] for r in _db.execute("SELECT number FROM cabinet").fetchall()}
            for m_cab in re.findall(r'onSelectCabinet\("(\d+)"\)|renderPrimaryCabinet\("(\d+)"', sld_src):
                cab_id = m_cab[0] or m_cab[1]
                assert cab_id in valid_all, f"Ghost cabinet {cab_id} in SingleLineDiagram.jsx!"

            # 8c. Verify multi-turn DiagnosisSession, C1535 applicability gate, NL capacity recalc, and /api/graph/demo
            sid = "test-multi-turn-session"
            t1 = fetch_json(base, "/api/diagnose", {
                "question": "2026年11月13日306柜为什么连续13小时零功率？",
                "mode": "local",
                "sessionId": sid,
            })
            assert t1["context_snapshot"]["turn"] == 1
            t2 = fetch_json(base, "/api/diagnose", {
                "question": "那同一天1005和1006低压侧怎么样？",
                "mode": "local",
                "sessionId": sid,
            })
            assert t2["context_snapshot"]["turn"] == 2
            assert "2026-11-13" in t2["context_snapshot"]["inherited_anchors"]["days"]

            ug = fetch_json(base, "/api/diagnose", {
                "question": "请查询井下1535中央变电所1101一回进线柜在11月13日的有功读数。",
                "mode": "local",
                "sessionId": "test-ug",
            })
            assert "1535" in ug["report"]["summary"] and "1009" in json.dumps(ug["report"], ensure_ascii=False)
            assert "1101" in ug["context_snapshot"]["active_anchors"]["cabinets"]
            ug_t2 = fetch_json(base, "/api/diagnose", {
                "question": "那它的上游送电柜怎么样？",
                "mode": "local",
                "sessionId": "test-ug",
            })
            assert "1101" in ug_t2["context_snapshot"]["inherited_anchors"]["cabinets"]

            cap_diag = fetch_json(base, "/api/diagnose", {
                "question": "如果303柜额定电流200A、1#主变6300kVA、降容系数0.9，且母联合位306检修，11月13日单进线会过载吗？",
                "mode": "local",
                "sessionId": "test-cap",
            })
            assert any(s["tool"] == "verify_conditional_capacity" for s in cap_diag["tool_trace"])
            assert "conditionally_satisfied" in json.dumps(cap_diag["report"], ensure_ascii=False)

            fetch_json(base, "/api/diagnose/reset", {"sessionId": sid})

            g_tid = f"TEST-UI-G1-{int(time.time() * 1000)}"
            g_susp = fetch_json(base, "/api/graph/demo", {"action": "suspend", "threadId": g_tid})
            assert g_susp["state"]["terminal_status"] == "needs_supplement"
            assert len(g_susp["state"]["executed_in_this_run"]) == 3
            g_res = fetch_json(base, "/api/graph/demo", {"action": "resume", "threadId": g_tid})
            assert g_res["state"]["terminal_status"] == "completed"
            assert len(g_res["state"]["executed_in_this_run"]) == 0
            assert len(g_res["state"]["reused_steps_in_this_run"]) == 3

            # 8d. Verify POST /api/diagnose/stream (SSE live events + inline HITL checkpoint + zero-duplicate resume)
            sse_req = Request(
                base + "/api/diagnose/stream",
                data=json.dumps({
                    "question": "2026年11月13日306柜为什么连续13小时零功率？303柜单进线带双主变是否过载？",
                    "mode": "local",
                    "sessionId": "test-sse-hitl",
                }, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(sse_req, timeout=15) as resp:
                assert resp.status == 200
                assert "text/event-stream" in resp.headers.get("Content-Type", "")
                raw_sse = resp.read().decode("utf-8")
            sse_events = [
                json.loads(line[5:].strip())
                for line in raw_sse.splitlines()
                if line.startswith("data:")
            ]
            sse_types = {e["type"] for e in sse_events}
            assert {"plan", "memory", "tool_start", "tool_end", "verify", "hitl_interrupt", "done"} <= sse_types
            done_res = next(e["result"] for e in sse_events if e["type"] == "done")
            hitl = done_res["hitl_interrupt"]
            assert hitl and hitl["required"] is True and hitl["thread_id"].startswith("CHAT-HITL-")

            # Resume that exact inline HITL checkpoint with 0 duplicate DB queries!
            hitl_resumed = fetch_json(base, "/api/graph/demo", {
                "action": "resume",
                "threadId": hitl["thread_id"],
                "parameters": {
                    "303_rated_current_a": 200.0,
                    "main_transformer_rated_kva": 6300.0,
                    "ambient_derating_factor": 0.90,
                    "switch_log_20261113": "35kV母联300开关合位，306柜检修",
                },
            })
            assert hitl_resumed["state"]["terminal_status"] == "completed"
            assert len(hitl_resumed["state"]["executed_in_this_run"]) == 0
            assert len(hitl_resumed["state"]["reused_steps_in_this_run"]) == 3

            # 8e. Verify ad-hoc electrical analytics over /api/diagnose/stream
            sse_ana = Request(
                base + "/api/diagnose/stream",
                data=json.dumps({
                    "question": "对比2026年11月14日1009和1010两个下井回路的累计用电量（kWh）。",
                    "mode": "local",
                    "sessionId": "test-sse-ana",
                }, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(sse_ana, timeout=15) as resp:
                ana_events = [
                    json.loads(line[5:].strip())
                    for line in resp.read().decode("utf-8").splitlines()
                    if line.startswith("data:")
                ]
            ana_done = next(e["result"] for e in ana_events if e["type"] == "done")
            assert ana_done["verification"]["status"] == "verified"
            assert any(s["tool"] == "query_electrical_analytics" for s in ana_done["tool_trace"])

            # 8f. Verify V02 N-1 HITL inline checkpoint + session memory write-back + SQLite session persistence + memory delete
            sse_v02 = Request(
                base + "/api/diagnose/stream",
                data=json.dumps({
                    "question": "如果1009柜退出，把负荷全部转供至1010柜，全周最严苛合成电流是多少安？下井双回N-1会过载吗？",
                    "mode": "local",
                    "sessionId": "test-v02-persist",
                }, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(sse_v02, timeout=15) as resp:
                v02_events = [
                    json.loads(line[5:].strip())
                    for line in resp.read().decode("utf-8").splitlines()
                    if line.startswith("data:")
                ]
            v02_done = next(e["result"] for e in v02_events if e["type"] == "done")
            assert v02_done["hitl_interrupt"]["target_item_id"] == "V02"
            v02_resumed = fetch_json(base, "/api/graph/demo", {
                "action": "resume",
                "threadId": v02_done["hitl_interrupt"]["thread_id"],
                "taskType": "underground_n1_capacity_audit",
                "sessionId": "test-v02-persist",
                "parameters": {
                    "1009_1010_rated_current_a": 300.0,
                    "ambient_derating_factor": 0.90,
                    "c1535_tie_switch_mode": "1200热备用可自动切换",
                },
            })
            assert v02_resumed["state"]["terminal_status"] == "completed"
            assert v02_resumed["state"]["supplement_result"]["conditional_judgment"]["n_minus_1_load_ratio_pct"] == 59.14
            assert len(v02_resumed["state"]["executed_in_this_run"]) == 0

            # Verify SQLite session persisted & recorded HITL resume turn digest
            sess_list = fetch_json(base, "/api/sessions")["sessions"]
            assert any(s["session_id"] == "test-v02-persist" for s in sess_list)
            sess_det = fetch_json(base, "/api/sessions/test-v02-persist")
            assert len(sess_det["turns"]) == 1
            assert len(sess_det["turns_digest"]) == 2  # initial turn + resumed HITL memory digest!
            assert sess_det["turns_digest"][-1]["kind"] == "supplement"
            assert sess_det["turns_digest"][-1]["facts"] == []
            del_sess = fetch_json(base, "/api/sessions/delete", {"sessionId": "test-v02-persist"})
            assert del_sess["deleted"] is True

            # Verify Episodic Memory deletion endpoint
            del_rev = fetch_json(base, "/api/reviews/delete", {"finding_id": "HLTH-MODE-SINGLE-01"})
            assert del_rev["deleted"] is True
            assert "HLTH-MODE-SINGLE-01" not in del_rev["reviews"]

            # 9. Verify source file download still works
            with urlopen(base + "/api/source/" + quote("1110.xls"), timeout=10) as resp:
                assert resp.status == 200 and len(resp.read()) > 1000
        finally:
            server.shutdown()
            server.server_close()
            server.app.db_manager.dispose()

    assert sha256(DB) == db_before, "laowadi.sqlite3 was modified!"
    assert sha256(EVENT_DB) == ev_before, "laowadi_events.sqlite3 was modified!"
    print(
        json.dumps(
            {
                "status": "ok",
                "static_assets_verified": len(assets),
                "sensitivity_voi_items_verified": len(sensitivity["voi_ranking"]),
                "autonomous_endpoints": autonomous_endpoints,
                "research_endpoints": research_endpoints,
                "cabinets_cards": len(health["cabinets_cards"]),
                "diagrams_verified": len(diagrams),
                "review_feedback_loop_verified": True,
                "legacy_replay_and_ask_verified": True,
                "archives_unchanged": True,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
