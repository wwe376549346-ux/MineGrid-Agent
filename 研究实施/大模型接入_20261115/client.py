"""Small OpenAI-format HTTP adapter. No SDK, shell evaluation or key logging."""
from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent


class APIError(RuntimeError):
    pass


def redact(value, secret=""):
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if k.lower() in {"authorization", "api_key", "coal_api_key"}
                    else redact(v, secret)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    if isinstance(value, str):
        if secret:
            value = value.replace(secret, "[REDACTED]")
        return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", value)
    return value


def load_config(path=None):
    path = Path(path) if path else HERE / ".env.local"
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep or not key.startswith("COAL_API_"):
                raise APIError("配置格式错误：只接受 COAL_API_ 开头的 KEY=VALUE")
            values[key] = value.strip().strip("\"").strip("'")
    values.update({k: v for k, v in os.environ.items() if k.startswith("COAL_API_")})
    for key in ("BASE_URL", "MODEL", "KEY"):
        if not values.get("COAL_API_" + key):
            raise APIError("尚未配置 COAL_API_" + key)
    parsed = urllib.parse.urlsplit(values["COAL_API_BASE_URL"])
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        raise APIError("基础 URL 必须是无账号、查询参数的 HTTPS 地址")
    return values


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise APIError("接口发生重定向；未向其他地址转发密钥")


@dataclass
class Client:
    config: dict
    records: list = field(default_factory=list)

    @classmethod
    def from_local(cls):
        return cls(load_config())

    @property
    def public_config(self):
        return {k: v for k, v in self.config.items() if k != "COAL_API_KEY"}

    def complete(self, messages, tools=None, max_tokens=None):
        if len(self.records) >= int(self.config.get("COAL_API_MAX_REQUESTS", "30")):
            raise APIError("本进程请求次数达到上限")
        base = self.config["COAL_API_BASE_URL"].rstrip("/")
        endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
        payload = {"model": self.config["COAL_API_MODEL"], "messages": messages,
                   "stream": False, "max_tokens": max_tokens or int(self.config.get("COAL_API_MAX_OUTPUT_TOKENS", "4096"))}
        thinking = self.config.get("COAL_API_THINKING", "")
        if thinking:
            payload["thinking"] = {"type": thinking}
        if self.config.get("COAL_API_JSON_MODE") == "1":
            payload["response_format"] = {"type": "json_object"}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        req = urllib.request.Request(endpoint, data=body, method="POST", headers={
            "Authorization": "Bearer " + self.config["COAL_API_KEY"],
            "Content-Type": "application/json", "Accept": "application/json"})
        record = {"sequence": len(self.records) + 1, "request_bytes": len(body),
                  "requested_max_output_tokens": payload["max_tokens"], "tools_advertised": bool(tools)}
        self.records.append(record)
        start = time.monotonic()
        try:
            opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
            with opener.open(req, timeout=int(self.config.get("COAL_API_TIMEOUT_SECONDS", "120"))) as response:
                record["http_status"] = response.status
                record["request_id"] = response.headers.get("x-request-id")
                raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise APIError("响应超过本地长度上限")
            data = json.loads(raw)
            if not isinstance(data.get("choices"), list) or not data["choices"]:
                raise APIError("接口未返回可识别的 choices")
            choice = data["choices"][0]
            message = choice.get("message", {})
            if message.get("role") != "assistant":
                raise APIError("响应角色不符合协议")
            record.update(status="completed", response_model=data.get("model"),
                          response_id=data.get("id"), usage=data.get("usage", {}),
                          finish_reason=choice.get("finish_reason"),
                          tool_calls=len(message.get("tool_calls") or []),
                          reasoning_field_returned=bool(message.get("reasoning_content")))
            return message, record
        except urllib.error.HTTPError as exc:
            # No response body or Authorization headers are logged.
            record.update(status="http_error", http_status=exc.code)
            raise APIError("API HTTP " + str(exc.code)) from None
        except APIError:
            record.setdefault("status", "protocol_error")
            raise
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
            record["status"] = "transport_or_protocol_error"
            record["error_type"] = type(exc).__name__
            raise APIError("网络、超时或响应格式错误；详细正文未记录") from None
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - start, 3)


def public_message(message):
    """Reasoning content is replayed in RAM if supplied, never written to reports."""
    return {k: v for k, v in message.items() if k in {"role", "content", "tool_calls"}}


if __name__ == "__main__":
    client = Client.from_local()
    try:
        message, record = client.complete([
            {"role": "system", "content": "You are a connection test. Reply with exactly READY."},
            {"role": "user", "content": "Reply READY."}], max_tokens=128)
        result = {"configured": client.public_config, "connection": record,
                  "reply": public_message(message), "key_in_output": False}
        code = 0
    except APIError as exc:
        result = {"configured": client.public_config, "error": str(exc), "requests": client.records}
        code = 1
    text = json.dumps(redact(result, client.config["COAL_API_KEY"]), ensure_ascii=False, indent=2)
    (HERE / "connection_check.json").write_text(text)
    print(text)
    raise SystemExit(code)
