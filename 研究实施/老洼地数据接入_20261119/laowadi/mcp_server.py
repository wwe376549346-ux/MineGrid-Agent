"""Stage 13: Standard read-only MCP (Model Context Protocol) JSON-RPC 2.0 server over stdio and in-process."""

from __future__ import annotations

from contextlib import closing
import json
import sys
import time

from laowadi.corpus import build_corpus_ledger
from laowadi.diag_tools import DIAGNOSTIC_TOOL_SCHEMAS, DiagnosticToolbox
from laowadi.evaluation import evaluate_all_layers
from laowadi.health import evaluate_health
from laowadi.monitor import EVENT_DB
from laowadi.queries import Archive
from laowadi.storage import DB, METRICS, REPORT_STATION, STATISTICS, connect


PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "laowadi-readonly-mcp", "version": "1.0.0"}
MAX_TIMEOUT_MS = 30000

BASE_TOOL_SCHEMAS = [
    {
        "name": "get_hourly_reading",
        "description": "按站、柜号、电气量（Uab/Ia/P/cos）及整点时刻（或日期）只读查询老洼地日报整点读数及原表单元格出处。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "station": {"type": "string", "description": "站代码，默认 S35。"},
                "cabinet": {"type": "string", "description": "地面站已建账柜号（如 303、306、1009、1010、1018）。"},
                "metric": {"type": "string", "enum": list(METRICS)},
                "at": {"type": "string", "description": "整点时刻 YYYY-MM-DD HH:00:00。"},
                "day": {"type": "string", "description": "可选日期 YYYY-MM-DD（未指定 at 时返回该日最新有效整点）。"},
            },
            "required": ["cabinet", "metric"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_daily_statistic",
        "description": "只读查询指定日期、柜号、电气量的最大值/最小值/平均值（支持 hourly 整点重算、footer 页尾原值或 both 双口径对照）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "station": {"type": "string", "description": "站代码，默认 S35。"},
                "cabinet": {"type": "string"},
                "metric": {"type": "string", "enum": list(METRICS)},
                "day": {"type": "string", "description": "日期 YYYY-MM-DD。"},
                "statistic": {"type": "string", "enum": list(STATISTICS)},
                "basis": {"type": "string", "enum": ["hourly", "footer", "both"]},
            },
            "required": ["cabinet", "metric", "day", "statistic"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_supply_link",
        "description": "只读查询指定站与柜号在两张监控截图台账中的上游（upstream）或下游（downstream）图示连接及原图 SHA-256。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "station": {"type": "string", "description": "站代码 S35 或 C1535。"},
                "cabinet": {"type": "string"},
                "direction": {"type": "string", "enum": ["upstream", "downstream"]},
            },
            "required": ["cabinet"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_monitor_event",
        "description": "按事件编号（如 LW-000010）或柜号、日期、整点只读查询已归档的监测回放事件及前后整点原表单元格出处。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "run_id": {"type": "string"},
                "cabinet": {"type": "string"},
                "day": {"type": "string"},
                "at": {"type": "string"},
                "event_type": {"type": "string", "enum": ["power_change", "hourly_gap", "missing_power"]},
            },
            "additionalProperties": False,
        },
    },
]


def _all_mcp_tools() -> list[dict]:
    diag_tools = [
        {
            "name": item["function"]["name"],
            "description": item["function"]["description"],
            "inputSchema": item["function"]["parameters"],
        }
        for item in DIAGNOSTIC_TOOL_SCHEMAS
    ]
    tools = []
    for t in BASE_TOOL_SCHEMAS + diag_tools:
        tools.append({
            **t,
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
                "stationScope": ["S35", "C1535"],
                "maxTimeoutMs": MAX_TIMEOUT_MS,
            },
        })
    return tools


class ReadOnlyMCPServer:
    """In-process and stdio JSON-RPC 2.0 MCP server bound to read-only Laowadi SQLite archives."""

    def __init__(self, archive: Archive):
        self.archive = archive
        self.toolbox = DiagnosticToolbox(archive)
        self.tools = {t["name"]: t for t in _all_mcp_tools()}
        self.base_names = {t["name"] for t in BASE_TOOL_SCHEMAS}

    def handle_message(self, request: dict) -> dict | None:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return self._error(request.get("id") if isinstance(request, dict) else None, -32600, "无效的 JSON-RPC 2.0 请求。")

        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params") or {}

        # Notifications have no id and expect no response
        if req_id is None and method.startswith("notifications/"):
            return None

        if method == "initialize":
            days = self.archive.days()
            return self._result(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": SERVER_INFO,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"listChanged": False},
                },
                "meta": {
                    "readOnly": True,
                    "stations": ["S35", "C1535"],
                    "days": days,
                    "maxTimeoutMs": MAX_TIMEOUT_MS,
                },
            })

        if method == "ping":
            return self._result(req_id, {})

        if method == "tools/list":
            return self._result(req_id, {"tools": list(self.tools.values())})

        if method == "tools/call":
            return self._handle_tool_call(req_id, params)

        if method == "resources/list":
            return self._result(req_id, {
                "resources": [
                    {
                        "uri": "laowadi://manifest/corpus",
                        "name": "老洼地现有资料台账与可答边界",
                        "mimeType": "application/json",
                        "description": "7 份运行日报、2 张监控截图、106 条回放事件与 68 条规程条款的完整来源哈希与可答边界。",
                    },
                    {
                        "uri": "laowadi://report/health",
                        "name": "老洼地 35 kV 全站 25 柜四维电气体检报告",
                        "mimeType": "application/json",
                        "description": "覆盖量测物理自洽、电能质量、系统运行方式与平衡、分时段基线的 25 项健康发现。",
                    },
                    {
                        "uri": "laowadi://report/evaluation",
                        "name": "老洼地智能体五层 BFCL+GAIA 综合评测报告",
                        "mimeType": "application/json",
                        "description": "五层评测指标、引文支持率与错误分类表。",
                    },
                ]
            })

        if method == "resources/read":
            uri = params.get("uri")
            if uri == "laowadi://manifest/corpus":
                data = build_corpus_ledger(self.archive)
            elif uri == "laowadi://report/health":
                data = evaluate_health(self.archive)
            elif uri == "laowadi://report/evaluation":
                data = evaluate_all_layers(self.archive)
            else:
                return self._error(req_id, -32602, f"未知的资源 URI：{uri}")
            return self._result(req_id, {
                "contents": [{
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": json.dumps(data, ensure_ascii=False, allow_nan=False),
                }]
            })

        return self._error(req_id, -32601, f"不支持的 MCP 方法：{method}")

    def _handle_tool_call(self, req_id, params: dict) -> dict:
        if not isinstance(params, dict):
            return self._error(req_id, -32602, "tools/call 参数须为对象。")
        name = params.get("name")
        arguments = params.get("arguments") or {}
        timeout_ms = params.get("timeout_ms", MAX_TIMEOUT_MS)

        if not isinstance(timeout_ms, (int, float)) or timeout_ms <= 0 or timeout_ms > MAX_TIMEOUT_MS:
            return self._error(req_id, -32602, f"请求超时设置越界（须在 1 至 {MAX_TIMEOUT_MS} ms 之间）。")
        if name not in self.tools:
            return self._error(req_id, -32602, f"未获准或不存在的只读工具：{name}")
        if not isinstance(arguments, dict):
            return self._error(req_id, -32602, "工具 arguments 必须为 JSON 对象。")

        t0 = time.monotonic()
        try:
            if name in self.base_names:
                call_args = dict(arguments)
                if name in {"get_hourly_reading", "get_daily_statistic", "get_supply_link"} and "station" not in call_args:
                    call_args["station"] = REPORT_STATION
                result = self.archive.call(name, call_args)
            else:
                result = self.toolbox.execute(name, arguments)
                if result.get("status") == "error":
                    raise ValueError(result.get("error", "工具调用参数无效。"))
        except (ValueError, TypeError) as exc:
            err_payload = {"status": "error", "tool": name, "error": str(exc)}
            return self._result(req_id, {
                "isError": True,
                "content": [{"type": "text", "text": json.dumps(err_payload, ensure_ascii=False)}],
                "structuredContent": err_payload,
            })

        elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)
        if elapsed_ms > timeout_ms:
            return self._error(req_id, -32001, f"工具 {name} 执行耗时 {elapsed_ms} ms 超过客户端设定的 {timeout_ms} ms 上限。")

        return self._result(req_id, {
            "isError": False,
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, allow_nan=False)}],
            "structuredContent": result,
            "meta": {"tool": name, "elapsedMs": elapsed_ms, "readOnly": True},
        })

    @staticmethod
    def _result(req_id, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _error(req_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def serve_stdio(db_path=DB, events_db=EVENT_DB):
    """Run the read-only MCP JSON-RPC server over newline-delimited stdio."""
    with closing(connect(db_path)) as db:
        archive = Archive(db, events_db)
        server = ReadOnlyMCPServer(archive)
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                resp = ReadOnlyMCPServer._error(None, -32700, "JSON 解析失败。")
            else:
                resp = server.handle_message(req)
            if resp is not None:
                print(json.dumps(resp, ensure_ascii=False, allow_nan=False), flush=True)
