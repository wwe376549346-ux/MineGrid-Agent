"""Resilient OpenAI-compatible chat client with SSE streaming, retries, and parallel tool calls."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from runpy import run_path
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from laowadi.storage import ROOT


RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """Raised when the configured LLM endpoint fails after retries or returns an invalid payload."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LLMError("模型端点返回了重定向，已中止请求以保护密钥。")


def load_config_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValueError(f"指定的模型配置文件不存在：{path}")
    client_mod = run_path(str(ROOT.parent / "大模型接入_20261115" / "client.py"))
    try:
        return client_mod["load_config"](path)
    except client_mod["APIError"] as exc:
        raise ValueError(str(exc)) from None


def _parse_sse_stream(response, max_bytes: int = 2_000_000, on_delta=None, *, t0: float | None = None) -> dict:
    """Assemble an OpenAI chat completion message from an SSE text/event-stream with TTFT tracking & live deltas."""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_slots: dict[int, dict[str, str]] = {}
    finish_reason = None
    usage = {}
    response_model = None
    response_id = None
    chunks = 0
    total_bytes = 0
    last_emit_ts = 0.0
    first_token_ts: float | None = None
    start_ts = t0 if t0 is not None else time.monotonic()

    def _mark_first():
        nonlocal first_token_ts
        if first_token_ts is None:
            first_token_ts = time.monotonic()

    def _maybe_emit(kind: str, text_so_far: str, force: bool = False):
        nonlocal last_emit_ts
        if on_delta is None:
            return
        now = time.monotonic()
        if force or (now - last_emit_ts) >= 0.45:
            last_emit_ts = now
            on_delta(kind, text_so_far)

    for raw_line in response:
        total_bytes += len(raw_line)
        if total_bytes > max_bytes:
            raise ValueError("流式响应超过本地字节上限。")
        line = raw_line.decode("utf-8", errors="ignore").strip()
        if not line or not line.startswith("data:"):
            continue
        payload_str = line[5:].strip()
        if payload_str == "[DONE]":
            break
        obj = json.loads(payload_str)
        chunks += 1
        if obj.get("model"):
            response_model = obj["model"]
        if obj.get("id"):
            response_id = obj["id"]
        if obj.get("usage"):
            usage = obj["usage"]
        choices = obj.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
        delta = choice.get("delta") or {}
        if delta.get("content"):
            _mark_first()
            content_parts.append(delta["content"])
            _maybe_emit("content", "".join(content_parts))
        if delta.get("reasoning_content"):
            _mark_first()
            reasoning_parts.append(delta["reasoning_content"])
            _maybe_emit("reasoning", "".join(reasoning_parts))
        for tc in delta.get("tool_calls") or []:
            _mark_first()
            idx = int(tc.get("index", 0))
            slot = tool_slots.setdefault(idx, {"id": "", "type": "function", "name": "", "arguments": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            if tc.get("type"):
                slot["type"] = tc["type"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] += fn["name"]
                _maybe_emit("tool_planning", ", ".join(s["name"] for s in tool_slots.values() if s["name"]), force=True)
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]

    message: dict = {"role": "assistant", "content": "".join(content_parts)}
    if tool_slots:
        message["tool_calls"] = [
            {
                "id": slot["id"] or f"call_{idx}",
                "type": slot["type"] or "function",
                "function": {"name": slot["name"], "arguments": slot["arguments"]},
            }
            for idx, slot in sorted(tool_slots.items())
        ]
    ttft_ms = round((first_token_ts - start_ts) * 1000.0, 2) if first_token_ts is not None else None
    return {
        "message": message,
        "finish_reason": finish_reason,
        "usage": usage,
        "response_model": response_model,
        "response_id": response_id,
        "chunks": chunks,
        "has_reasoning": bool(reasoning_parts),
        "ttft_ms": ttft_ms,
    }


def _parse_json_response(response, max_bytes: int = 2_000_000) -> dict:
    raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("非流式响应超过本地字节上限。")
    data = json.loads(raw)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("响应缺少 choices。")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ValueError("响应角色不符合协议。")
    clean_msg = {k: v for k, v in message.items() if k in {"role", "content", "tool_calls"}}
    return {
        "message": clean_msg,
        "finish_reason": choice.get("finish_reason"),
        "usage": data.get("usage", {}),
        "response_model": data.get("model"),
        "response_id": data.get("id"),
        "chunks": 1,
        "has_reasoning": bool(message.get("reasoning_content")),
    }


@dataclass
class ChatClient:
    """OpenAI-compatible client with SSE streaming, bounded retries, and trace records."""

    base_url: str
    model: str
    api_key: str = ""
    timeout_seconds: int = 120
    default_thinking: str = "disabled"
    max_retries: int = 2
    stream: bool = True
    records: list[dict] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, *, stream: bool = True, max_retries: int = 2) -> "ChatClient":
        src = env if env is not None else os.environ
        base = src.get("COAL_API_BASE_URL", "").rstrip("/")
        model = src.get("COAL_API_MODEL", "")
        key = src.get("COAL_API_KEY", "")
        thinking = src.get("COAL_API_THINKING", "disabled")
        try:
            url = urlsplit(base)
            loopback = url.hostname in {"localhost", "127.0.0.1", "::1"}
            if (
                not model
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.scheme not in ({"https", "http"} if loopback else {"https"})
                or (not loopback and not key)
            ):
                raise ValueError
            timeout = int(src.get("COAL_API_TIMEOUT_SECONDS", "120"))
            if timeout <= 0 or thinking not in {"", "enabled", "disabled"}:
                raise ValueError
        except ValueError:
            raise LLMError("模型配置无效：请核对 COAL_API_BASE_URL、COAL_API_MODEL、COAL_API_KEY 与超时配置。") from None
        return cls(
            base_url=base,
            model=model,
            api_key=key,
            timeout_seconds=timeout,
            default_thinking=thinking,
            max_retries=max_retries,
            stream=stream,
        )

    @classmethod
    def from_config_file(cls, path: Path, *, stream: bool = True, max_retries: int = 2) -> "ChatClient":
        cfg = load_config_file(path)
        return cls.from_env(cfg, stream=stream, max_retries=max_retries)

    def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        max_tokens: int = 2048,
        thinking: str | None = None,
        json_mode: bool = False,
        stream: bool | None = None,
        on_delta=None,
    ) -> tuple[dict, dict]:
        use_stream = self.stream if stream is None else stream
        think_mode = self.default_thinking if thinking is None else thinking
        endpoint = self.base_url if self.base_url.endswith("/chat/completions") else self.base_url + "/chat/completions"
        url = urlsplit(endpoint)
        loopback = url.hostname in {"localhost", "127.0.0.1", "::1"}

        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": use_stream,
            "max_tokens": max_tokens,
        }
        if think_mode:
            payload["thinking"] = {"type": think_mode}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        if json_mode and not tools:
            payload["response_format"] = {"type": "json_object"}

        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        handlers = [NoRedirect()]
        if loopback:
            handlers.insert(0, ProxyHandler({}))
        else:
            handlers.append(HTTPSHandler(context=ssl.create_default_context()))
        opener = build_opener(*handlers)

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            t0 = time.monotonic()
            record: dict = {
                "sequence": len(self.records) + 1,
                "attempt": attempt + 1,
                "stream": use_stream,
                "thinking": think_mode or "default",
                "tools_advertised": len(tools or []),
                "max_tokens": max_tokens,
            }
            try:
                with opener.open(Request(endpoint, data=body, headers=headers), timeout=self.timeout_seconds) as resp:
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    record["http_status"] = resp.status
                    if use_stream and "text/event-stream" in ctype:
                        parsed = _parse_sse_stream(resp, on_delta=on_delta, t0=t0)
                    else:
                        parsed = _parse_json_response(resp)
                elapsed_s = round(time.monotonic() - t0, 3)
                from laowadi.llmops import GLOBAL_LLMOPS, estimate_tokens_and_cost

                tok_info = estimate_tokens_and_cost(
                    messages,
                    str(parsed["message"].get("content") or "") + json.dumps(parsed["message"].get("tool_calls") or [], ensure_ascii=False),
                    parsed["usage"],
                )
                ttft_ms = parsed.get("ttft_ms") if parsed.get("ttft_ms") is not None else round(elapsed_s * 1000.0, 2)
                gen_ms = max(1.0, elapsed_s * 1000.0 - ttft_ms)
                tpot_ms = round(gen_ms / max(1, tok_info["completion_tokens"]), 2)
                record.update(
                    status="completed",
                    elapsed_seconds=elapsed_s,
                    ttft_ms=ttft_ms,
                    tpot_ms=tpot_ms,
                    finish_reason=parsed["finish_reason"],
                    chunks=parsed["chunks"],
                    tool_calls=len(parsed["message"].get("tool_calls") or []),
                    usage=parsed["usage"],
                    token_accounting=tok_info,
                    response_model=parsed["response_model"] or self.model,
                    served_by_model=parsed["response_model"] or self.model,
                )
                self.records.append(record)
                GLOBAL_LLMOPS.record_llm_call(record)
                return parsed["message"], record
            except HTTPError as exc:
                record.update(status="http_error", http_status=exc.code, elapsed_seconds=round(time.monotonic() - t0, 3))
                self.records.append(record)
                if exc.code in RETRYABLE_HTTP_CODES and attempt < self.max_retries:
                    time.sleep(min(2.0 * (attempt + 1), 5.0))
                    last_err = exc
                    continue
                raise LLMError(f"模型端点返回 HTTP {exc.code}。") from None
            except LLMError:
                record.update(status="protocol_error", elapsed_seconds=round(time.monotonic() - t0, 3))
                self.records.append(record)
                raise
            except (URLError, TimeoutError, OSError, ValueError, TypeError, KeyError, IndexError) as exc:
                record.update(
                    status="transport_or_parse_error",
                    error_type=type(exc).__name__,
                    elapsed_seconds=round(time.monotonic() - t0, 3),
                )
                self.records.append(record)
                last_err = exc
                if attempt < self.max_retries:
                    time.sleep(min(1.5 * (attempt + 1), 4.0))
                    continue
                raise LLMError(f"模型端点连接失败或响应格式无效（{type(exc).__name__}）。") from None

        raise LLMError(f"模型调用重试后仍失败（{type(last_err).__name__ if last_err else 'unknown'}）。")


class FailoverChatClient:
    """Multi-model failover router backed by cross-process SharedCircuitBreaker (P1-2)."""

    def __init__(
        self,
        clients: list[Any],
        *,
        breaker_db: Path | str | None = None,
        failure_threshold: int = 2,
        cooldown_seconds: float = 30.0,
    ):
        if not clients:
            raise ValueError("FailoverChatClient 至少需要配置一个模型客户端。")
        self.clients = list(clients)
        self.breaker_db = breaker_db
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.records: list[dict] = []

    @property
    def model(self) -> str:
        return str(getattr(self.clients[0], "model", "failover-primary"))

    def _breaker_for(self, model_name: str):
        from laowadi.circuit_breaker import SharedCircuitBreaker

        kwargs = {"failure_threshold": self.failure_threshold, "cooldown_seconds": self.cooldown_seconds}
        if self.breaker_db is not None:
            kwargs["db_path"] = self.breaker_db
        return SharedCircuitBreaker(f"llm:{model_name}", **kwargs)

    def chat(self, messages: list[dict], **kwargs) -> tuple[dict, dict]:
        from laowadi.llmops import GLOBAL_LLMOPS, estimate_tokens_and_cost

        errors: list[str] = []
        for idx, client in enumerate(self.clients):
            m_name = str(getattr(client, "model", f"model-{idx}"))
            cb = self._breaker_for(m_name)
            allowed, cb_state = cb.allow_request()
            if not allowed:
                errors.append(f"{m_name}: circuit_open ({cb_state.get('last_error', '')})")
                continue
            t0 = time.monotonic()
            try:
                msg, rec = client.chat(messages, **kwargs)
                cb.record_success()
                rec = dict(rec or {})
                rec.setdefault("status", "completed")
                rec.setdefault("elapsed_seconds", round(time.monotonic() - t0, 3))
                rec.setdefault("ttft_ms", round((time.monotonic() - t0) * 1000.0, 2))
                rec.setdefault("tpot_ms", 1.0)
                if "token_accounting" not in rec:
                    rec["token_accounting"] = estimate_tokens_and_cost(messages, str(msg.get("content") or ""), rec.get("usage"))
                rec["served_by_model"] = m_name
                rec["failover_used"] = idx > 0
                if idx > 0:
                    rec["failed_models"] = list(errors)
                self.records.append(rec)
                if not isinstance(client, ChatClient):
                    GLOBAL_LLMOPS.record_llm_call(rec)
                elif idx > 0:
                    GLOBAL_LLMOPS.record_llm_call({"status": "completed", "failover_used": True, "served_by_model": m_name})
                return msg, rec
            except Exception as exc:
                cb.record_failure(str(exc))
                errors.append(f"{m_name}: {exc}")
                GLOBAL_LLMOPS.record_llm_call({"status": "error", "served_by_model": m_name})
                continue

        raise LLMError(f"全部主/备模型均不可用或已熔断：{'；'.join(errors)}")
