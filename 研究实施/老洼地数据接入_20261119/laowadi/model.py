"""Optional OpenAI-compatible function selection for the Laowadi review loop."""

import json
import os
from runpy import run_path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from laowadi.agent import EVENT, LINK, READING, ModelUnavailable, SelectionError
from laowadi.storage import ROOT


DESCRIPTIONS = {
    EVENT: "按编号或精确条件读取已归档的监测事件。",
    READING: "读取指定柜号在事件前后某一个整点的有功读数。",
    LINK: "读取指定柜号在供电图中的上游或下游连接。",
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ModelUnavailable("模型端点返回了重定向，本轮停止。")


def load_project_model_config(path):
    if not path.is_file():
        raise ValueError("指定的模型配置文件不存在。")
    client = run_path(str(ROOT.parent / "大模型接入_20261115" / "client.py"))
    try:
        return client["load_config"](path)
    except client["APIError"] as exc:
        raise ValueError(str(exc)) from None


def advertised_tools(options):
    tools = []
    for name in dict.fromkeys(option["tool"] for option in options):
        variants = [option["arguments"] for option in options if option["tool"] == name]
        properties = {key: {"type": "string", "enum": list(dict.fromkeys(args[key] for args in variants))}
                      for key in variants[0]}
        tools.append({"type": "function", "function": {"name": name, "description": DESCRIPTIONS[name],
                      "parameters": {"type": "object", "properties": properties, "required": list(properties),
                                     "additionalProperties": False}}})
    return tools


def selection_messages(question, options, steps):
    acquired = [{"tool": step["tool"], "arguments": step["arguments"]} for step in steps]
    messages = [
        {"role": "system", "content": "你是矿井电力历史回放的只读查询选择器。依据用户问题和已执行查询，"
         "从候选调用中原样选择一个有助于完成本轮复核的函数及参数。即使多个候选都相关，本次也只返回一个工具调用。"
         "问题明确要求优先查某个柜时，优先查齐这个柜的两个整点。"
         "程序负责核验待查项、停止条件和证据展示。"},
        {"role": "user", "content": json.dumps({"question": question, "acquired": acquired, "allowed_calls": options},
                                              ensure_ascii=False, allow_nan=False)},
    ]
    return messages


def select_with_client(client, question, options, steps):
    """Use the main workbench client with only candidates for this event."""
    from laowadi.llm import LLMError

    try:
        message, _ = client.chat(selection_messages(question, options, steps),
                                 tools=advertised_tools(options), tool_choice="auto", max_tokens=2048)
    except LLMError as exc:
        raise ModelUnavailable(str(exc)) from None
    return parse_selection(message, options)


def select_with_model(question, options, steps):
    base = os.environ.get("COAL_API_BASE_URL", "").rstrip("/")
    model = os.environ.get("COAL_API_MODEL")
    key = os.environ.get("COAL_API_KEY")
    thinking = os.environ.get("COAL_API_THINKING", "")
    try:
        url = urlsplit(base)
        loopback = url.hostname in {"localhost", "127.0.0.1", "::1"}
        if (not model or not url.hostname or url.username or url.password or url.query or url.fragment
                or url.scheme not in ({"https", "http"} if loopback else {"https"}) or (not loopback and not key)):
            raise ValueError
    except ValueError:
        raise ModelUnavailable("请配置 COAL_API_BASE_URL 和 COAL_API_MODEL。远程端点需使用 HTTPS 和 COAL_API_KEY。") from None
    try:
        timeout = int(os.environ.get("COAL_API_TIMEOUT_SECONDS", "30"))
        output_limit = int(os.environ.get("COAL_API_MAX_OUTPUT_TOKENS", "512"))
        if timeout <= 0 or output_limit <= 0 or thinking not in {"", "enabled", "disabled"}:
            raise ValueError
    except ValueError:
        raise ModelUnavailable("请核对模型超时、输出上限和思考模式配置。") from None

    messages = selection_messages(question, options, steps)
    if not loopback:
        # Use SSE streaming + automatic retry + generous thinking token budget on remote HTTPS gateways
        from laowadi.llm import ChatClient, LLMError

        try:
            client = ChatClient(
                base_url=base,
                model=model,
                api_key=key or "",
                timeout_seconds=max(timeout, 90),
                default_thinking=thinking or "disabled",
                stream=True,
                max_retries=2,
            )
            message, _ = client.chat(
                messages,
                tools=advertised_tools(options),
                tool_choice="auto" if thinking == "enabled" else "required",
                max_tokens=max(output_limit, 2048),
            )
        except LLMError as exc:
            raise ModelUnavailable(str(exc)) from None
    else:
        payload = {"model": model, "messages": messages, "tools": advertised_tools(options),
                   "tool_choice": "auto" if thinking == "enabled" else "required", "stream": False, "max_tokens": output_limit}
        if thinking:
            payload["thinking"] = {"type": thinking}
        body = json.dumps(payload, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
        opener = build_opener(ProxyHandler({}), NoRedirect())
        try:
            with opener.open(Request(endpoint, data=body, headers=headers), timeout=timeout) as response:
                raw = response.read(262145)
            if len(raw) > 262144:
                raise ValueError
            message = json.loads(raw)["choices"][0]["message"]
            if message["role"] != "assistant":
                raise ValueError
        except HTTPError as exc:
            raise ModelUnavailable(f"模型端点返回 HTTP {exc.code}，本轮停止。") from None
        except (URLError, OSError, ValueError, TypeError, KeyError, IndexError):
            raise ModelUnavailable("模型端点连接失败或响应格式无效，本轮停止。") from None

    return parse_selection(message, options)


def parse_selection(message, options):
    try:
        calls = message.get("tool_calls") or []
        if not calls:
            raise ValueError
        parsed = []
        for req in calls:
            if req.get("type") != "function":
                continue
            function = req["function"]
            raw_args = function.get("arguments")
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            parsed.append({"tool": function["name"], "arguments": args})
        if not parsed:
            raise ValueError
        # Prefer the first tool call that exactly matches an allowed option when model emits parallel calls
        for cand in parsed:
            if cand in options:
                return cand
        return parsed[0]
    except (ValueError, TypeError, KeyError):
        raise SelectionError from None
