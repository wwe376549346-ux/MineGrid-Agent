"""End-to-end verification for the refactored Astryx workbench server, APIs, and review feedback loop."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
from http.server import ThreadingHTTPServer
from importlib import import_module
import json
import math
import os
from pathlib import Path
import subprocess
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
from laowadi.sessions import load_session, save_chat_turn
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
    sid = "test-agent-stream"
    audit_result = {"report": {"summary": "旧审计单"}, "markdown": "旧审计单"}
    audit_anchors = {"cabinets": ["303"]}
    audit_digest = [{"summary": "旧审计单"}]
    save_chat_turn(sid, "已有审计问答", "local", audit_result, active_anchors=audit_anchors,
                   turns_digest=audit_digest, path=workbench.sessions_db)
    chart = {"kind": "chart", "chart_type": "line", "title": "潮流曲线", "points": [[1, 10.9], [2, 10.6]]}
    table = {"kind": "table", "title": "独立统计表", "columns": ["档位"], "rows": [[2]]}
    plan = [{"id": "archive", "title": "查询原表", "status": "done"}]
    badge = {"status": "verified", "checked_cells": 1, "verified_cells": 1}
    code_run = {"run_id": "c2", "code": code, "stdout": "42\n", "stderr": ""}
    result = {
        "answer_markdown": "查询完成。",
        "plan": plan,
        "artifacts": [chart, table],
        "tool_trace": [{"tool": "get_hourly_reading", "summary": "读取 1006 柜 1 条整点数据"}],
        "runs": [{**code_run, "elapsed_ms": 4}],
        "verification_badge": badge,
        "ui_artifact_hint": {"canvas": "powerflow", "focus_cabinet": "1006", "replay_index": 89},
        "recommended_action": {"kind": "field_verification", "target_cabinet": "1006"},
        "deep_research_goal": "核查 1006 柜越限与同期负荷",
    }
    expected = [
        {"type": "plan_update", "items": [{"id": "archive", "title": "查询原表", "status": "running"}]},
        {"type": "reasoning_delta", "delta": "按柜号和整点查归档。"},
        {"type": "tool_start", "tool": "get_hourly_reading", "call_id": "c1"},
        {"type": "tool_end", "tool": "get_hourly_reading", "call_id": "c1", "status": "ok", "summary": "已取证"},
        {"type": "code_run", "run_id": "c2", "code": code, "result": {"stdout": "42\n", "stderr": ""}},
        {"type": "artifact", "artifact": chart},
        {"type": "content_delta", "delta": "查询完成。"},
        {"type": "verification_badge", **badge},
        {"type": "done", "result": result},
    ]

    async def verify_arrival_before_completion():
        first_sent = threading.Event()
        sent = bytearray()
        request_read = False
        body = json.dumps({"question": question, "sessionId": sid}).encode()

        model_client = object()

        def run_agent(archive, task, *, client, session, max_steps, memory_db_path, on_event):
            assert ("S35", "1006") in archive.devices and task == question
            assert client is model_client and session is not None and max_steps == 8
            assert not session.active_anchors["cabinets"], "Audit anchors leaked into autonomous context"
            session.resolve_question("2026年11月13日15:00，1006柜的有功功率是多少？")
            assert memory_db_path.parent == workbench.sessions_db.parent
            on_event(expected[0])
            assert first_sent.wait(timeout=3), "Agent SSE buffered the first frame until completion"
            for event in expected[1:]:
                on_event(event)
            on_event(expected[-1])

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
                if message["body"].startswith(b"event: done"):
                    saved = load_session(sid, path=workbench.sessions_db)
                    assert len(saved["turns"]) == 2, saved
                    assert saved["turns"][1]["result"] == decode_named_events(message["body"].decode())[0]["result"]

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
        events = decode_named_events(sent.decode())
        assert first_sent.is_set() and events[:-1] == expected[:-1]
        final = events[-1]
        assert final["type"] == "done" and final["session_id"] == sid
        saved = final["result"]
        assert saved["turn_kind"] == "autonomous" and saved["code_runs"] == result["runs"]
        assert saved["tool_trace"] == [{**result["tool_trace"][0], "call_id": "c1", "status": "ok"}]
        assert all(saved[key] == value for key, value in result.items() if key != "tool_trace")

    asyncio.run(verify_arrival_before_completion())

    with TestClient(app) as client:
        assert client.post(agent_path, json={"question": question}).status_code == 401
        assert client.post(sandbox_path, headers=dispatcher, json={"code": code}).status_code == 403
        assert client.post(agent_path, headers=dispatcher, json={"question": " ", "maxSteps": 0}).status_code == 400
        assert client.post(sandbox_path, headers=engineer, json={"code": code, "timeout_s": 0}).status_code == 400
        with patch.dict(sys.modules, {agent_module: SimpleNamespace(), sandbox_module: None}):
            assert client.post(agent_path, headers=dispatcher, json={"question": question}).status_code == 503
            assert client.post(sandbox_path, headers=engineer, json={"code": code}).status_code == 503

        listing = client.get("/api/sessions", headers=dispatcher).json()["sessions"]
        assert any(item["session_id"] == sid and item["turn_count"] == 2 for item in listing)
        saved = client.get(f"/api/sessions/{sid}", headers=dispatcher).json()
        assert saved["turns"][0]["result"] == audit_result
        assert saved["active_anchors"] == audit_anchors and saved["turns_digest"] == audit_digest
        assert saved["turns"][1]["mode"] == "auto"

        model_client = object()
        seen_clients = []

        def test_mode(archive, task, *, client, session, on_event, **kwargs):
            seen_clients.append(client)
            assert session.active_anchors["cabinets"] == ["1006"]
            on_event({"type": "done", "result": {"answer_markdown": task}})

        with (patch.dict(sys.modules, {agent_module: SimpleNamespace(run_autonomous_agent=test_mode)}),
              patch("laowadi.api.ChatClient.from_env", return_value=model_client) as model_factory):
            for mode in ("local", "deepseek"):
                response = client.post(agent_path, headers=dispatcher, json={"question": question, "mode": mode, "sessionId": sid})
                assert response.status_code == 200 and decode_named_events(response.text)[-1]["type"] == "done"
            model_factory.assert_called_once_with(stream=True)
        assert seen_clients == [None, model_client]
        saved = client.get(f"/api/sessions/{sid}", headers=dispatcher).json()
        assert [turn["mode"] for turn in saved["turns"]] == ["local", "auto", "local", "deepseek"]

        def fail_before_done(archive, task, *, on_event, **kwargs):
            on_event({"type": "content_delta", "delta": "未完成"})
            on_event({"type": "error", "error": "模拟中断"})

        with patch.dict(sys.modules, {agent_module: SimpleNamespace(run_autonomous_agent=fail_before_done)}):
            response = client.post(agent_path, headers=dispatcher, json={
                "question": question, "mode": "local", "sessionId": "test-agent-interrupted",
            })
            assert decode_named_events(response.text)[-1]["type"] == "error"
            assert client.get("/api/sessions/test-agent-interrupted", headers=dispatcher).status_code == 404

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
                required = {event["type"] for event in expected} - {"artifact"}
                assert required <= {event["type"] for event in events}, events
                run = next(event for event in events if event["type"] == "code_run")
                assert run["status"] == "ok" and run["tables"] and run["charts"][0]["chart_type"] == "scatter", run
                persisted = client.get(f"/api/sessions/{events[-1]['session_id']}", headers=dispatcher).json()
                assert persisted["turns"][0]["result"] == events[-1]["result"]
                assert persisted["turns"][0]["result"]["code_runs"][0]["charts"] == run["charts"]

                tap = client.post(agent_path, headers=headers, json={
                    "mode": "local", "sessionId": "test-agent-tap",
                    "question": "2026年11月13日17:00，2# 主变分接头档位对 10 kV II 段电压的潮流曲线。",
                })
                tap_result = decode_named_events(tap.text)[-1]["result"]
                assert not tap_result["code_runs"]
                assert {artifact["kind"] for artifact in tap_result["artifacts"]} == {"table", "chart"}
                persisted_tap = client.get("/api/sessions/test-agent-tap", headers=dispatcher).json()
                assert persisted_tap["turns"][0]["result"] == tap_result

                exact = client.post(agent_path, headers=headers, json={
                    "mode": "local",
                    "question": "2026年11月13日15:00，303柜的有功功率是多少？请给出数值、单位和日报单元格坐标。",
                })
                exact_result = decode_named_events(exact.text)[-1]["result"]
                assert exact_result["tool_trace"][0]["tool"] == "get_hourly_reading"
                assert "5710.12 kW" in exact_result["answer_markdown"]
                assert "1113.xls / New Sheet1!D19" in exact_result["answer_markdown"]
            else:
                assert response.json()["stdout"].strip() == "42", response.text
            coverage[path] = "end-to-end"
        return coverage


def verify_autonomous_browser(base: str) -> str:
    # Set this to a local Playwright or playwright-core module to exercise the production bundle.
    module = os.environ.get("LAOWADI_PLAYWRIGHT_MODULE")
    if not module:
        return "skipped (LAOWADI_PLAYWRIGHT_MODULE unset)"
    script = r"""
const assert = require("node:assert/strict");
const {chromium} = require(process.env.LAOWADI_PLAYWRIGHT_MODULE);
const base = process.argv[1];

(async () => {
  const executablePath = process.env.LAOWADI_CHROMIUM || undefined;
  const browser = await chromium.launch({executablePath});
  try {
    const page = await browser.newPage({reducedMotion: "reduce"});
    page.setDefaultTimeout(7000);
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const ticket = {kind: "field_verification", target_cabinet: "1006", rationale: "核对现场台账"};
    const goal = "带入 1006 柜越限专项排查目标";
    const chart = {kind: "chart", chart_type: "line", title: "非沙盒潮流曲线", points: [[1, 10.91], [2, 10.662]]};
    const table = {kind: "table", title: "非沙盒潮流扫描表", columns: ["档位"], rows: [[2]]};
    const streamedTable = {...table, title: "流式中间表"};
    const sandboxTable = {title: "沙盒样本", columns: ["样本数"], rows: [[42]]};
    const run = {call_id: "r1", status: "ok", stdout: "42", tables: [sandboxTable]};
    const plan = [{id: "p1", title: "核对档位和母线", status: "done"}];
    const verification = {status: "verified", notes: ["坐标一致"]};
    const summary = "返回 2 个档位的潮流结果";
    const result = {
      turn_kind: "autonomous", plan, verification_badge: verification, code_runs: [run],
      artifacts: [chart, table, {kind: "table", ...sandboxTable}],
      tool_trace: [{tool: "run_ac_power_flow", call_id: "t1", summary, ok: true}],
      ui_artifact_hint: {canvas: "powerflow", focus_cabinet: "1006"},
      recommended_action: ticket, deep_research_goal: goal,
      answer_markdown: [
        "| 档位 | 电压 |", "| :--- | ---: |", "| **+2** | 10.662 |", "",
        "```python", "  print(42)", "```", "", "3. 第一项", "4. 第二项",
      ].join("\n"),
    };
    const modes = [];
    const sessionTitle = "潮流排查浏览器历史";
    let saved;
    let draft;
    const list = () => ({sessions: saved ? [{session_id: saved.session_id, title: sessionTitle}] : []});
    const fulfill = payload => ({json: payload});
    await page.route("**/api/sessions", route => route.fulfill(fulfill(list())));
    await page.route("**/api/sessions/*", route => route.fulfill(fulfill(saved)));
    await page.route("**/api/dates", route => route.fulfill(fulfill({dates: ["2026-11-13"], model: "deepseek-v4-pro"})));
    await page.route("**/api/deep-research/reports", route => route.fulfill(fulfill({reports: []})));
    await page.route("**/api/actions/tickets", route => route.fulfill(fulfill({tickets: []})));
    await page.route("**/api/live/uploads", route => route.fulfill(fulfill({uploads: [{day: "2026-11-18"}]})));
    await page.route("**/api/actions/tickets/draft", route => {
      draft = route.request().postDataJSON();
      return route.fulfill(fulfill({ticket: {...draft, ticket_id: "ACT-TEST", status: "draft"}}));
    });
    await page.route("**/api/agent/stream", route => {
      const {mode, question, sessionId} = route.request().postDataJSON();
      modes.push(mode);
      const transcriptResult = {...result, artifacts: [...result.artifacts, streamedTable]};
      saved = {session_id: sessionId, turns: [{id: 1, question, mode, result: transcriptResult}]};
      const frames = [
        {type: "plan_update", steps: plan},
        {type: "artifact", artifact: chart},
        {type: "artifact", data: {artifact: streamedTable}},
        {type: "code_run", ...run},
        {type: "done", result},
      ];
      return route.fulfill({
        contentType: "text/event-stream",
        body: frames.map(frame => "event: " + frame.type + "\ndata: " + JSON.stringify(frame) + "\n\n").join(""),
      });
    });

    await page.goto(base);
    const button = name => page.getByRole("button", {name, exact: true});
    const examples = page.locator(".duty-prompt");
    await page.getByText("查整点读数", {exact: true}).waitFor();
    assert.equal(await examples.count(), 6);
    await button("自主深度 Agent").click();
    await page.getByText("精准单点查库", {exact: true}).waitFor();
    assert.equal(await examples.count(), 6);
    const engine = page.getByLabel("问答方式");
    assert.equal(await engine.inputValue(), "auto");
    const ask = async mode => {
      await engine.selectOption(mode);
      await page.getByLabel("值班问题").fill("浏览器潮流回归");
      await button("发送问题").click();
      await button(sessionTitle).waitFor();
      await button("升级为深度专项排查").waitFor();
    };
    await ask("deepseek");
    assert.equal(await page.getByText("非沙盒潮流曲线", {exact: true}).count(), 1);
    assert.equal(await page.getByRole("region", {name: "流式中间表"}).count(), 1);
    assert.equal(await page.getByRole("region", {name: "沙盒样本"}).count(), 1);
    await button("新对话").click();
    await ask("local");
    assert.deepEqual(modes, ["deepseek", "local"]);

    await button("切换深色外观").click();
    await page.reload();
    await button("切换浅色外观").waitFor();
    assert.equal(await page.locator("html").getAttribute("data-theme"), "dark");
    await button(sessionTitle).click();
    await page.getByLabel("执行计划").getByText("核对档位和母线").waitFor();
    await page.getByText("证据核验通过", {exact: true}).waitFor();
    assert.equal(await engine.inputValue(), "local");
    const answer = page.locator(".agent-stream-answer");
    assert.equal(await answer.locator("td strong").innerText(), "+2");
    assert.equal(await answer.locator("pre code").textContent(), "  print(42)");
    assert.equal(await answer.locator("ol").getAttribute("start"), "3");
    assert.equal(await answer.locator("ol li").count(), 2);
    await page.getByText("工具调用 · 1", {exact: true}).click();
    await page.getByText(summary, {exact: true}).waitFor();
    assert.equal(await page.getByRole("region", {name: "非沙盒潮流扫描表"}).count(), 1);
    const point = button("2 · 10.662");
    await point.locator("circle").last().hover();
    await page.getByRole("tooltip").getByText("2 · 10.662").waitFor();
    await point.locator("circle").last().click();
    await page.mouse.move(0, 0);
    assert.equal(await page.getByRole("tooltip").textContent(), "2 · 10.662");
    await point.press("Escape");
    assert.equal(await page.getByRole("tooltip").count(), 0);

    await button("查看关联画布").click();
    await page.getByRole("main", {name: "交流潮流"}).waitFor();
    await button("值班问答").click();
    await button("升级为深度专项排查").click();
    await page.getByRole("textbox", {name: "排查目标", exact: true}).waitFor();
    assert.equal(await page.getByRole("textbox", {name: "排查目标", exact: true}).inputValue(), goal);
    await button("值班问答").click();
    assert.equal(await page.locator(".duty-source-mode").textContent(), "历史回放");
    await button("一键起草两票草案").click();
    await page.getByRole("article", {name: "票据 ACT-TEST"}).waitFor();
    assert.deepEqual(draft, ticket);
    await page.waitForFunction(() => document.querySelector(".duty-source-mode").textContent === "增量沙盒");
    await page.getByLabel("演练日").selectOption("2026-11-13");
    await page.waitForFunction(() => document.querySelector(".duty-source-mode").textContent === "实时演练");
    await button("值班问答").click();
    assert.equal(await page.locator(".duty-source-mode").textContent(), "历史回放");
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run(["node", "-e", script, base], check=True, timeout=100)
    return "passed (Chromium production bundle)"


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


def verify_live_and_action_endpoints(workbench: Workbench) -> dict:
    upload_path = "/api/live/upload-report"
    uploads_path = "/api/live/uploads"
    stream_path = "/api/live/stream"
    tickets_path = "/api/actions/tickets"
    draft_path = tickets_path + "/draft"
    missing_ticket = tickets_path + "/ACT-MISSING"
    dispatcher = {"Authorization": f"Bearer {issue_token('workbench-approver', 'dispatcher')['access_token']}"}
    viewer = {"Authorization": f"Bearer {issue_token('workbench-viewer', 'viewer')['access_token']}"}
    csv_text = (
        "time,cabinet,Uab,Ia,P,cos\n"
        "2026-11-17 10:00:00,1006,10.86,55,220,0.94\n"
        "2026-11-17 10:00:00,303,35.6,100,4431,0.97\n"
        "2026-11-17 11:00:00,1006,10.74,56,-30,0.96\n"
    )
    upload = {"csv_text": csv_text, "filename": "增量日报.csv"}
    draft = {"kind": "switching_order", "target_cabinet": "1006", "at_time": "2026-11-13T17:00"}
    replay = {"day": "2026-11-17", "speed_ms": 0, "custom_threshold_kv": 10.65}
    config_url = "https://notifications.example.invalid/private-path-token"
    # The formal dispatch test captures the request at the runner boundary, with no network access.
    with patch.dict(os.environ, {"LAOWADI_ACTION_WEBHOOK_URL": config_url}):
        app = create_app(workbench, require_auth=True)

    coverage = {}
    with TestClient(app) as client:
        with patch.dict(sys.modules, {"laowadi.ingest_live": None, "laowadi.action_tickets": None}):
            assert client.post(upload_path, headers=dispatcher, json=upload).status_code == 503
            assert client.get(uploads_path, headers=viewer).status_code == 503
            assert client.post(stream_path, headers=dispatcher, json=replay).status_code == 503
            assert client.get(tickets_path, headers=viewer).status_code == 503
            assert client.post(draft_path, headers=dispatcher, json=draft).status_code == 503
            for action, body in (("checklist", {"item_id": "chk_1", "checked": True}), ("approve", {}),
                                 ("reject", {"reason": "验收"}), ("dispatch", {"dry_run": True})):
                assert client.post(f"{missing_ticket}/{action}", headers=dispatcher, json=body).status_code == 503

        assert client.post(upload_path, headers=viewer, json=upload).status_code == 403
        assert client.post(draft_path, headers=viewer, json=draft).status_code == 403
        assert client.post(stream_path, headers=dispatcher, json={**replay, "day": "2026-11-31"}).status_code == 400

        history = client.get(uploads_path, headers=viewer)
        if history.status_code == 503:
            coverage["live"] = "503 (runner unavailable)"
        else:
            assert history.status_code == 200 and history.json()["uploads"] == []
            wrong_station = "\n".join(f"{line},{'station' if idx == 0 else 'U1535'}"
                                      for idx, line in enumerate(csv_text.splitlines()))
            for invalid in (csv_text.replace("10.86", "inf"), csv_text.replace("2026-11-17", "2026-11-31"), wrong_station):
                assert client.post(upload_path, headers=dispatcher, json={**upload, "csv_text": invalid}).status_code == 400
            assert client.get(uploads_path, headers=viewer).json()["uploads"] == []
            text_response = client.post(upload_path, headers=dispatcher, json=upload)
            assert text_response.status_code == 201, text_response.text
            uploaded = text_response.json()
            assert uploaded["row_count"] == 12 and uploaded["cabinet_count"] == 2
            assert {"overvoltage", "reverse_power"} <= {finding["kind"] for finding in uploaded["findings"]}
            file_response = client.post(upload_path, headers=dispatcher, files={
                "file": ("下一日.csv", csv_text.replace("2026-11-17", "2026-11-18").encode("utf-8-sig"), "text/csv")
            })
            assert file_response.status_code == 201, file_response.text
            assert file_response.json()["filename"] == "下一日.csv" and file_response.json()["day"] == "2026-11-18"
            recorded = client.get(uploads_path, headers=viewer).json()["uploads"]
            assert {row["upload_id"] for row in recorded} == {uploaded["upload_id"], file_response.json()["upload_id"]}
            for day, source, count in (("2026-11-17", "live_upload", 2), ("2026-11-13", "archive", 24)):
                streamed = client.post(stream_path, headers=dispatcher, json={**replay, "day": day})
                assert streamed.status_code == 200 and streamed.headers["content-type"].startswith("text/event-stream")
                assert streamed.headers["x-accel-buffering"] == "no"
                events = decode_named_events(streamed.text)
                ticks = [event for event in events if event["type"] == "telemetry_tick"]
                assert len(ticks) == count and all(tick["source"] == source for tick in ticks)
                assert ticks[0]["threshold_kv"] == 10.65 and ticks[0]["metrics"]["u_1006_kv"] is not None
                assert any(event["type"] == "live_alert" and event["alert"]["kind"] == "overvoltage" for event in events)
                assert events[-1]["type"] == "done" and events[-1]["result"]["ticks_emitted"] == count
                if source == "archive":
                    assert any(event.get("alert", {}).get("evidence_cell") for event in events)
            coverage["live"] = {"upload_formats": ["text", "multipart"], "replay_ticks": [2, 24]}

        listed = client.get(tickets_path, headers=viewer)
        if listed.status_code == 503:
            coverage["actions"] = "503 (runner unavailable)"
            return coverage
        assert listed.status_code == 200 and listed.json()["tickets"] == []
        created = client.post(draft_path, headers=dispatcher, json=draft)
        assert created.status_code == 201, created.text
        ticket = created.json()["ticket"]
        assert ticket["risk_level"] == "high" and ticket["evidence_cells"]
        path = f"{tickets_path}/{ticket['ticket_id']}"
        required = [item["id"] for item in ticket["checklist"] if item["required"]]
        assert len(required) >= 2 and not ticket["checklist_progress"]["ready_to_approve"]
        assert client.post(path + "/approve", headers=dispatcher).status_code == 409
        assert client.post(path + "/dispatch", headers=dispatcher).status_code == 409
        assert client.post(path + "/checklist", headers=viewer, json={"item_id": required[0], "checked": True}).status_code == 403

        def check_item(ticket_path, item_id, checked=True):
            return client.post(ticket_path + "/checklist", headers=dispatcher, json={"item_id": item_id, "checked": checked})

        for item_id in required:
            changed = check_item(path, item_id)
            assert changed.status_code == 200, changed.text
        unchecked = check_item(path, required[0], False).json()["ticket"]
        assert not unchecked["checklist_progress"]["ready_to_approve"]
        assert client.post(path + "/approve", headers=dispatcher, json={}).status_code == 409
        assert check_item(path, required[0]).status_code == 200
        approved = client.post(path + "/approve", headers=dispatcher, json={"note": "只读演练逐项核验"}).json()["ticket"]
        assert approved["status"] == "approved" and approved["approved_by"] == "workbench-approver"
        assert check_item(path, required[0], False).status_code == 409
        exercise = client.post(path + "/dispatch", headers=dispatcher, json={"dry_run": True})
        assert exercise.status_code == 200 and exercise.json()["dispatch"]["status"] == "dry_run_recorded"

        with patch("laowadi.action_tickets.urllib.request.urlopen") as sender:
            sender.return_value.__enter__.return_value.status = 202
            for _ in range(2):
                pushed = client.post(path + "/dispatch", headers=dispatcher, json={"dry_run": False})
                assert pushed.status_code == 200 and pushed.json()["dispatch"]["status"] == "delivered"
                assert "private-path-token" not in pushed.text
            sender.assert_called_once()
            sent_request = sender.call_args.args[0]
            assert sent_request.full_url == config_url and json.loads(sent_request.data)["advisory_only"] is True
        assert "private-path-token" not in client.get(tickets_path, headers=viewer).text

        defect = client.post(draft_path, headers=dispatcher, json={**draft, "kind": "defect_notice"})
        defect_path = tickets_path + "/" + defect.json()["ticket"]["ticket_id"]
        rejected = client.post(defect_path + "/reject", headers=dispatcher, json={"reason": "现场记录待补"})
        assert rejected.status_code == 200 and rejected.json()["ticket"]["audit_log"][-1]["detail"] == "现场记录待补"
        assert client.post(defect_path + "/approve", headers=dispatcher).status_code == 409
        assert client.post(missing_ticket + "/approve", headers=dispatcher).status_code == 404

        field = client.post(draft_path, headers=dispatcher, json={**draft, "kind": "field_verification"}).json()["ticket"]
        field_path = tickets_path + "/" + field["ticket_id"]
        for item in field["checklist"]:
            assert check_item(field_path, item["id"]).status_code == 200
        barrier = threading.Barrier(2)

        def race(request):
            barrier.wait(timeout=5)
            return request().status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            sign = pool.submit(race, lambda: client.post(field_path + "/approve", headers=dispatcher))
            uncheck = pool.submit(race, lambda: check_item(field_path, field["checklist"][0]["id"], False))
            assert sorted([sign.result(timeout=10), uncheck.result(timeout=10)]) == [200, 409]
        persisted = client.get(tickets_path, headers=viewer, params={"kind": "field_verification"}).json()["tickets"][0]
        assert (persisted["status"] == "approved") == persisted["checklist_progress"]["ready_to_approve"]
        coverage["actions"] = {"kinds": 3, "checklist_gate": 409, "dispatch": ["dry_run", "mocked_https"]}
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
            autonomous_browser = verify_autonomous_browser(base)
            research_endpoints = verify_memory_and_research_endpoints(server.app)
            live_action_endpoints = verify_live_and_action_endpoints(server.app)

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
                "autonomous_browser": autonomous_browser,
                "research_endpoints": research_endpoints,
                "live_action_endpoints": live_action_endpoints,
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
