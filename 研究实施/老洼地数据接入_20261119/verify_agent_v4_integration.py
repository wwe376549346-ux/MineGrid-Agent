"""End-to-end Phase-2 FastAPI + RBAC integration gate uniting all 6 agent upgrades.

Exercises via isolated FastAPI TestClient (zero golden archive mutation):
1. Memory expiration (`expires_at`), conflict reporting, `supersede_conflicts`, and `prune-expired`.
2. Incident root-cause grouping (`/api/tickets/groups`) and ticket-to-memory distillation (`/distill-memory`).
3. Multimodal field intake (`/api/intake/image`) for multipart + JSON + RBAC + invalid image 400 + mocked VLM.
4. Operator feedback (`/api/feedback`), negative-case export (`/api/feedback/export-negative`), and `/api/eval/observability`.
5. Autonomous SSE `/api/agent/stream` ambiguity clarification (`clarify`) & bounded verification self-repair (`verification_repair`), plus free-topic debate `/api/subagents/debate/stream`.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from laowadi.api import create_app
from laowadi.monitor import EVENT_DB
from laowadi.security import issue_token
from laowadi.storage import DB
from laowadi.workbench import SOURCE_DIR, Workbench


EXPECTED_DB_SHA = {
    "laowadi.sqlite3": "37946d8abb40bd34295b7787c3426cc3e19a0ece9c9df70e45481f2253fb573a",
    "laowadi_events.sqlite3": "99373c36ce28939905d12a93714e05ca886ea8068cd31690fd33ea2d2a5c8d05",
}


def decode_sse(raw_text: str) -> list[dict]:
    events: list[dict] = []
    event_name = ""
    data_lines: list[str] = []
    for line in raw_text.splitlines():
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line == "" and data_lines:
            payload = json.loads("\n".join(data_lines))
            if event_name and "type" not in payload:
                payload["type"] = event_name
            events.append(payload)
            event_name = ""
            data_lines = []
    return events


def main() -> None:
    with TemporaryDirectory() as tmp:
        runtime = Path(tmp)
        wb = Workbench(
            DB,
            EVENT_DB,
            SOURCE_DIR,
            model="deepseek-v4-pro",
            reviews_db=runtime / "reviews.sqlite3",
            sessions_db=runtime / "sessions.sqlite3",
            audit_db=runtime / "audit.sqlite3",
            jobs_db=runtime / "jobs.sqlite3",
            database_url=f"sqlite+pysqlite:///{runtime / 'enterprise.sqlite3'}",
        )
        with TestClient(create_app(wb, require_auth=True)) as client:
            viewer = {"Authorization": "Bearer " + issue_token("u-viewer", "viewer")["access_token"]}
            dispatcher = {"Authorization": "Bearer " + issue_token("u-disp", "dispatcher")["access_token"]}
            engineer = {"Authorization": "Bearer " + issue_token("u-eng", "engineer")["access_token"]}

            # 1. Memory governance APIs (expiration + conflicts + supersede + prune)
            r_exp = client.post(
                "/api/memory",
                headers=dispatcher,
                json={
                    "category": "semantic",
                    "title": "1006 历史临时红线",
                    "content": "1006 临时 10.50 kV",
                    "scope_cabinet": "1006",
                    "metric_threshold": {"metric": "Uab", "operator": ">", "value": 10.50, "unit": "kV"},
                    "expires_at": "2020-01-01",
                },
            )
            assert r_exp.status_code == 201, r_exp.text
            assert client.get("/api/memory?enabled_only=true", headers=viewer).json()["memories"] == []
            assert len(client.get("/api/memory?include_expired=true", headers=viewer).json()["memories"]) == 1

            pruned = client.post("/api/memory/prune-expired", headers=dispatcher, json={"disable_only": True})
            assert pruned.status_code == 200 and pruned.json()["count"] == 1, pruned.text

            r_a = client.post(
                "/api/memory",
                headers=dispatcher,
                json={
                    "category": "semantic",
                    "title": "1006 考核 10.65 kV",
                    "content": "1006 按 10.65 kV",
                    "scope_cabinet": "1006",
                    "metric_threshold": {"metric": "Uab", "operator": ">", "value": 10.65, "unit": "kV"},
                    "expires_at": "2099-12-31",
                },
            )
            assert r_a.status_code == 201 and r_a.json()["conflicts"] == []

            r_b = client.post(
                "/api/memory",
                headers=dispatcher,
                json={
                    "category": "semantic",
                    "title": "1006 新红线 10.58 kV",
                    "content": "1006 收紧至 10.58 kV",
                    "scope_cabinet": "1006",
                    "metric_threshold": {"metric": "Uab", "operator": ">", "value": 10.58, "unit": "kV"},
                },
            )
            assert r_b.status_code == 201 and len(r_b.json()["conflicts"]) == 1, r_b.text

            r_c = client.post(
                "/api/memory",
                headers=dispatcher,
                json={
                    "category": "semantic",
                    "title": "1006 最终红线 10.60 kV",
                    "content": "1006 按 10.60 kV 并替代旧规则",
                    "scope_cabinet": "1006",
                    "metric_threshold": {"metric": "Uab", "operator": ">", "value": 10.60, "unit": "kV"},
                    "supersede_conflicts": True,
                },
            )
            assert r_c.status_code == 201 and len(r_c.json()["superseded_memory_ids"]) == 2, r_c.text

            # 2. Proactive scan + /api/tickets/groups + transition memory_candidate + /distill-memory
            scan = client.post("/api/proactive/scan", headers=dispatcher, json={"max_new_tickets": 20})
            assert scan.status_code == 200 and scan.json()["created_count"] >= 10, scan.text
            grp_res = client.get("/api/tickets/groups?status=active", headers=viewer)
            assert grp_res.status_code == 200, grp_res.text
            groups = grp_res.json()["groups"]
            assert any(g["is_composite"] and g["root_source_finding_id"] == "HLTH-MODE-SINGLE-01" for g in groups), groups

            tickets = client.get("/api/tickets?status=active&limit=50", headers=viewer).json()["tickets"]
            t_1018 = next(t for t in tickets if t["source_finding_id"] == "HLTH-MTR-1018")
            tr1 = client.post(
                f"/api/tickets/{t_1018['ticket_id']}/transition",
                headers=dispatcher,
                json={"action": "claim", "actor": "值班长", "expected_revision": t_1018["revision"]},
            )
            assert tr1.status_code == 200
            tr2 = client.post(
                f"/api/tickets/{t_1018['ticket_id']}/transition",
                headers=dispatcher,
                json={
                    "action": "dispute",
                    "actor": "值班长",
                    "note": "现场核实 1018 柜电流互感器二次接线反接，非回馈倒送",
                    "expected_revision": tr1.json()["ticket"]["revision"],
                },
            )
            assert tr2.status_code == 200
            cand = tr2.json().get("memory_candidate")
            assert cand and cand["category"] == "episodic" and cand["scope_cabinet"] == "1018", tr2.json()
            distilled = client.post(f"/api/tickets/{t_1018['ticket_id']}/distill-memory", headers=dispatcher)
            assert distilled.status_code == 200 and distilled.json()["requires_human_confirmation"] is True
            assert distilled.json()["candidate"]["scope_cabinet"] == "1018"

            # 3. Multimodal intake (/api/intake/image) RBAC, validation, local & mocked VLM
            png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
            b64_png = base64.b64encode(png_bytes).decode("ascii")
            assert client.post(
                "/api/intake/image",
                headers=viewer,
                json={"image_base64": b64_png, "mime_type": "image/png", "mode": "local"},
            ).status_code == 403
            assert client.post(
                "/api/intake/image",
                headers=dispatcher,
                json={"image_base64": base64.b64encode(b"not-a-png").decode("ascii"), "mime_type": "image/png", "mode": "local"},
            ).status_code == 400
            local_img = client.post(
                "/api/intake/image",
                headers=dispatcher,
                json={"image_base64": b64_png, "mime_type": "image/png", "mode": "local", "hint": "铭牌"},
            )
            assert local_img.status_code == 200 and local_img.json()["status"] == "unavailable"
            assert local_img.json()["requires_human_confirmation"] is True

            class MockVisionClient:
                def chat(self, _messages, **_opts):
                    return {
                        "content": json.dumps({
                            "candidates": [{"field": "额定电流", "value": 220, "unit": "A", "evidence": "铭牌右侧"}]
                        })
                    }, {}

            with patch("laowadi.api._stream_model_client", return_value=MockVisionClient()):
                mp = client.post(
                    "/api/intake/image",
                    headers=dispatcher,
                    data={"hint": "303柜铭牌", "mode": "auto"},
                    files={"file": ("plate.png", png_bytes, "image/png")},
                )
            assert mp.status_code == 200, mp.text
            assert mp.json()["status"] == "pending_review"
            assert mp.json()["candidates"][0]["value"] == 220
            assert mp.json()["requires_human_confirmation"] is True

            # 4. Feedback & Observability
            fb_ok = client.post(
                "/api/feedback",
                headers=dispatcher,
                json={"target_type": "diagnosis", "verdict": "helpful", "target_id": "D-1", "cabinet": "303"},
            )
            assert fb_ok.status_code == 201, fb_ok.text
            fb_bad = client.post(
                "/api/feedback",
                headers=dispatcher,
                json={
                    "target_type": "autonomous_turn",
                    "verdict": "incorrect",
                    "target_id": "A-1",
                    "question": "1018 有功正负？",
                    "expected_correction": "负值不可改写作正值",
                    "reason_tags": ["sign_flip"],
                },
            )
            assert fb_bad.status_code == 201, fb_bad.text
            assert client.post("/api/feedback/export-negative", headers=dispatcher, json={}).status_code == 403
            fb_list = client.get("/api/feedback?negative_only=true", headers=viewer)
            assert fb_list.status_code == 200 and len(fb_list.json()["items"]) == 1
            fb_exp = client.post("/api/feedback/export-negative", headers=engineer, json={"mark_exported": True})
            assert fb_exp.status_code == 200 and fb_exp.json()["schema"] == "agentops_negative_eval_v1"
            assert fb_exp.json()["count"] == 1

            # 5. Autonomous clarify + repair SSE streaming + free-topic debate
            clarify_sse = client.post(
                "/api/agent/stream",
                headers=dispatcher,
                json={"question": "下午15点那个柜的电流是多少？", "mode": "local"},
            )
            assert clarify_sse.status_code == 200, clarify_sse.text
            c_events = decode_sse(clarify_sse.text)
            assert any(e["type"] == "clarification_request" for e in c_events), c_events
            assert c_events[-1]["type"] == "done" and c_events[-1]["result"]["status"] == "clarify"

            class RepairStreamClient:
                model = "mock-repair-model"

                def __init__(self):
                    self.turn = 0

                def chat(self, _messages, **opts):
                    self.turn += 1
                    if self.turn == 1:
                        return {
                            "role": "assistant",
                            "tool_calls": [{
                                "type": "function",
                                "id": "call_snap",
                                "function": {
                                    "name": "get_window_snapshot",
                                    "arguments": json.dumps({
                                        "start_time": "2026-11-13 15:00:00",
                                        "end_time": "2026-11-13 15:00:00",
                                        "cabinets": ["303"],
                                        "metrics": ["P"],
                                    }),
                                },
                            }],
                        }, {}
                    if self.turn == 2:
                        bad = "303 柜有功为 5710.12 kW（1113.xls / New Sheet1!ZZZ999）。"
                        if opts.get("on_delta"):
                            opts["on_delta"]("content", bad)
                        return {"content": bad}, {}
                    good = "303 柜 11-13 15:00 有功功率为 5710.12 kW（1113.xls / New Sheet1!D19）。"
                    return {"content": good}, {}

            with patch("laowadi.api._stream_model_client", return_value=RepairStreamClient()):
                rep_sse = client.post(
                    "/api/agent/stream",
                    headers=dispatcher,
                    json={"question": "2026-11-13 15:00 请核查 303 柜的有功读数", "mode": "auto"},
                )
            assert rep_sse.status_code == 200
            r_events = decode_sse(rep_sse.text)
            assert any(e["type"] == "verification_repair" and e.get("status") == "succeeded" for e in r_events), r_events
            done_res = r_events[-1]["result"]
            assert done_res["repair_attempted"] is True and done_res["repair_succeeded"] is True
            assert "ZZZ999" not in done_res["answer_markdown"]

            debate_sse = client.post(
                "/api/subagents/debate/stream",
                headers=dispatcher,
                json={"question": "如何核对班次交接期间的记录一致性？", "mode": "local"},
            )
            assert debate_sse.status_code == 200
            d_events = decode_sse(debate_sse.text)
            assert any(e["type"] == "arbitration_done" for e in d_events)
            assert d_events[-1]["result"]["topic"] == "free_topic"

            obs = client.get("/api/eval/observability", headers=viewer)
            assert obs.status_code == 200
            obs_json = obs.json()
            assert obs_json["feedback"]["total"] == 2 and obs_json["feedback"]["unexported_negative_count"] == 0
            assert obs_json["llmops"]["operator_feedback"]["total"] >= 2

    for name, expected in EXPECTED_DB_SHA.items():
        actual = hashlib.sha256((DB.parent / name).read_bytes()).hexdigest()
        assert actual == expected, f"{name} mutated: {actual}"
    print("PASS verify_agent_v4_integration.py")


if __name__ == "__main__":
    main()
