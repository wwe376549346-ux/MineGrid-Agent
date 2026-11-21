"""Regress task completion in the main diagnosis path using the archived Laowadi reports."""

from contextlib import closing
from hashlib import sha256
import json

from laowadi.diagnose import DiagnosisSession, diagnose_question
from laowadi.monitor import EVENT_DB
from laowadi.queries import Archive
from laowadi.storage import DB, connect


def check(result, status, verification, cells):
    assert result["task_alignment"]["status"] == status, result
    assert result["verification"]["status"] == verification, result
    assert set(result["verification"]["verified_cells"]) == set(cells), result
    assert result["business_trusted"] is (status == "completed"), result
    assert result["outcome_layers"]["execution_status"] == "completed"
    return result["report"]["summary"]


class ChangingModel:
    model = "deepseek-tool-protocol-fixture"

    def __init__(self, *, invalid=False):
        self.invalid = invalid
        self.requests = []

    def chat(self, messages, **options):
        state = json.loads(messages[-1]["content"])
        self.requests.append(state)
        assert all(set(step) == {"tool", "arguments"} for step in state["acquired"])
        assert {entry["function"]["name"] for entry in options["tools"]} == {"get_hourly_reading"}
        choice = state["allowed_calls"][-1]
        args = {**choice["arguments"]}
        if self.invalid:
            args["at"] = "2026-11-13 15:00:00"
        return {"tool_calls": [{"type": "function", "function": {"name": choice["tool"],
                                  "arguments": json.dumps(args)}}]}, None


def verify(archive):
    session = DiagnosisSession(archive)
    question = "复核LW-000010，比较306柜同期有功。是不是跳闸导致的？"
    event_cells = ["1110.xls / New Sheet1!" + cell for cell in ("D15", "D16", "H15", "H16")]
    for model in (None, ChangingModel(), ChangingModel(invalid=True)):
        result = diagnose_question(archive, question, session=session, client=model)
        text = check(result, "partial", "verified", event_cells)
        assert all(piece in text for piece in ("3,408.12", "427.54", "223.13", "-204.41", "SOE", "保护动作"))
        assert "1112.xls" not in json.dumps(result["tool_trace"])
        if model:
            assert len(model.requests) == 1 and result["degraded_from_model"] == model.invalid
            hours = [step["arguments"]["at"] for step in result["tool_trace"] if step["tool"] == "get_hourly_reading"]
            expected = ["2026-11-10 11:00:00", "2026-11-10 12:00:00"]
            assert hours == (expected if model.invalid else expected[::-1])

    check(diagnose_question(archive, "LW-000010，比较306柜13点的有功。", session=session),
          "clarification_required", "not_applicable", [])
    text = check(diagnose_question(archive, "那1009柜呢？", session=session), "completed", "verified",
                 ["1110.xls / New Sheet1!" + cell for cell in ("D15", "D16", "BD15", "BD16")])
    assert "+459.92" in text
    point = diagnose_question(archive, "那改查2026年11月13日15:00的303柜有功", session=session)
    assert "5710.12" in check(point, "completed", "verified", ["1113.xls / New Sheet1!D19"])
    assert point["context_snapshot"]["active_anchors"]["events"] == []
    assert "2026-11-13 13:00" in check(diagnose_question(archive, "那13点呢？", session=session),
                                      "completed", "verified", ["1113.xls / New Sheet1!D17"])
    assert "306 柜" in check(diagnose_question(archive, "那306柜呢？", session=session),
                              "completed", "verified", ["1113.xls / New Sheet1!H17"])
    assert "2026-11-14 15:00" in check(diagnose_question(archive, "换到11月14日15:00呢？", session=session),
                                      "completed", "verified", ["1114.xls / New Sheet1!H19"])
    point_half = diagnose_question(archive, "查11月13日15点半的303柜有功")
    check(point_half, "clarification_required", "not_applicable", [])
    paired = diagnose_question(archive, "比较11月13日12:00与11月14日15:00的303柜有功")
    assert "229.81" in check(paired, "completed", "verified",
                            ["1113.xls / New Sheet1!D16", "1114.xls / New Sheet1!D19"])

    missing = diagnose_question(archive, "比较2026年11月16日和11月17日15:00，303柜的有功。哪一天更高？")
    text = check(missing, "partial", "verified", ["1116.xls / New Sheet1!D19"])
    assert "4969.78" in text and "无法判断高低" in text and missing["missing_days"] == ["2026-11-17"]
    assert missing["business_status"] == "partial"
    underground = diagnose_question(archive, "查中央变电所1201柜在2026年11月13日12:00的有功，给出原表出处。")
    text = check(underground, "insufficient_data", "verified", [])
    assert "1010" in text and "1201" in text and "缺少运行日报" in text
    assert [step["tool"] for step in underground["tool_trace"]] == ["get_supply_link"]

    maximum = diagnose_question(archive, "11月13日303柜的整点和页尾有功最大值各是多少？", session=session)
    text = check(maximum, "completed", "verified", ["1113.xls / New Sheet1!D19", "1113.xls / New Sheet1!D28"])
    assert all(piece in text for piece in ("5,710.12", "6,101.04", "15:00", "24/24", "未注明"))
    text = check(diagnose_question(archive, "那页尾的呢？", session=session),
                 "completed", "verified", ["1113.xls / New Sheet1!D28"])
    assert "6,101.04" in text and "5,710.12" not in text
    average = diagnose_question(archive, "11月13日303柜的整点和页尾有功平均值各是多少？")
    text = check(average, "completed", "verified",
                 ["1113.xls / New Sheet1!D4", "1113.xls / New Sheet1!D27", "1113.xls / New Sheet1!D30"])
    assert "D4:D27" in text and "4,046.18" in text


if __name__ == "__main__":
    before = [sha256(path.read_bytes()).digest() for path in (DB, EVENT_DB)]
    with closing(connect(DB)) as conn:
        verify(Archive(conn, EVENT_DB))
    assert [sha256(path.read_bytes()).digest() for path in (DB, EVENT_DB)] == before
    print("Main question alignment and bounded model selection verified. Both archive hashes unchanged.")
