"""Enterprise Security Layer: RBAC, HMAC-SHA256 Bearer Tokens, Rate Limiting & Prompt-Injection Firewall.

Designed for industrial coal-mine deployment:
- Four hierarchical operational roles: viewer < dispatcher < engineer < admin.
- Signed stateless tokens (HMAC-SHA256) with expiration and role binding.
- Sliding-window per-client rate limiter protecting compute-heavy endpoints.
- Deterministic pre-execution firewall blocking remote control attempts, prompt injection/jailbreaks,
  and raw SQL/database mutation probes before any LLM or tool execution.
"""

from __future__ import annotations

import base64
from collections import defaultdict, deque
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any, Literal

from laowadi.storage import ROOT


RoleName = Literal["viewer", "dispatcher", "engineer", "admin"]

ROLE_LEVEL: dict[str, int] = {
    "viewer": 10,
    "dispatcher": 20,
    "engineer": 30,
    "admin": 40,
}

ROLE_DESCRIPTIONS: dict[str, dict[str, Any]] = {
    "viewer": {
        "label": "值班观摩 / 审计只读",
        "level": 10,
        "permissions": ["read:monitoring", "read:health", "read:profile", "read:audit", "read:metrics"],
    },
    "dispatcher": {
        "label": "变电所值班调度员",
        "level": 20,
        "permissions": [
            "read:*",
            "exec:diagnose",
            "exec:powerflow",
            "exec:multi_agent",
            "write:reviews",
            "exec:hitl_resume",
            "exec:supplement",
        ],
    },
    "engineer": {
        "label": "矿机电科电气工程师",
        "level": 30,
        "permissions": ["dispatcher:*", "delete:reviews", "delete:sessions", "exec:audit_verify"],
    },
    "admin": {
        "label": "系统安全管理员",
        "level": 40,
        "permissions": ["*"],
    },
}

# Route -> minimum required role
ROUTE_MIN_ROLE: list[tuple[str, str, RoleName]] = [
    # Sensitive destructive / governance operations
    ("POST", "/api/reviews/delete", "engineer"),
    ("POST", "/api/sessions/delete", "engineer"),
    ("POST", "/api/audit/verify", "engineer"),
    ("POST", "/api/runtime/circuit-breaker/", "engineer"),
    ("POST", "/api/storage/migrate", "engineer"),
    ("POST", "/api/ops/backup", "engineer"),
    ("POST", "/api/ops/dr-drill", "engineer"),
    # Read-only vector search endpoint
    ("POST", "/api/storage/vector-search", "viewer"),
    # Operational diagnostic, simulation, HITL & review write operations
    ("POST", "/api/jobs", "dispatcher"),
    ("POST", "/api/jobs/", "dispatcher"),
    ("POST", "/api/ask", "dispatcher"),
    ("POST", "/api/diagnose", "dispatcher"),
    ("POST", "/api/diagnose/stream", "dispatcher"),
    ("POST", "/api/diagnose/reset", "dispatcher"),
    ("POST", "/api/powerflow", "dispatcher"),
    ("POST", "/api/multi-agent/consult", "dispatcher"),
    ("POST", "/api/reviews", "dispatcher"),
    ("POST", "/api/supplement", "dispatcher"),
    ("POST", "/api/graph/demo", "dispatcher"),
    ("POST", "/api/dag/run", "dispatcher"),
    ("POST", "/api/dag/resume", "dispatcher"),
    ("POST", "/api/tickets", "dispatcher"),
    ("POST", "/api/tickets/", "dispatcher"),
    ("POST", "/api/proactive/scan", "dispatcher"),
    ("POST", "/api/proactive/dispatch", "dispatcher"),
    ("POST", "/api/proactive/reconcile", "engineer"),
    ("POST", "/api/feedback/export-negative", "engineer"),
    ("POST", "/api/memory/prune-expired", "dispatcher"),
    ("POST", "/api/intake/image", "dispatcher"),
    ("POST", "/api/feedback", "dispatcher"),
    # Read-only endpoints
    ("GET", "/api/", "viewer"),
    ("GET", "/metrics", "viewer"),
]

_SECRET_FILE = ROOT / "local" / ".auth_secret"


def get_auth_secret() -> bytes:
    env_secret = os.environ.get("LAOWADI_AUTH_SECRET", "").strip()
    if env_secret:
        return env_secret.encode("utf-8")
    _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _SECRET_FILE.is_file():
        data = _SECRET_FILE.read_text(encoding="utf-8").strip()
        if len(data) >= 32:
            return data.encode("utf-8")
    generated = secrets.token_hex(32)
    _SECRET_FILE.write_text(generated, encoding="utf-8")
    try:
        os.chmod(_SECRET_FILE, 0o600)
    except OSError:
        pass
    return generated.encode("utf-8")


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + pad).encode("ascii"))


def issue_token(
    subject: str,
    role: RoleName = "dispatcher",
    *,
    ttl_seconds: int = 28800,
    secret: bytes | None = None,
) -> dict[str, Any]:
    if role not in ROLE_LEVEL:
        raise ValueError(f"无效角色：{role}，须为 viewer / dispatcher / engineer / admin。")
    sub = str(subject or "operator").strip()[:64] or "operator"
    ttl = max(60, min(int(ttl_seconds), 86400 * 7))
    now = int(time.time())
    payload = {
        "sub": sub,
        "role": role,
        "iat": now,
        "exp": now + ttl,
        "iss": "laowadi-power-agent",
    }
    key = secret or get_auth_secret()
    body = _b64u_encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = _b64u_encode(hmac.new(key, f"v1.{body}".encode("ascii"), hashlib.sha256).digest())
    token = f"v1.{body}.{sig}"
    return {
        "access_token": token,
        "token_type": "bearer",
        "subject": sub,
        "role": role,
        "role_label": ROLE_DESCRIPTIONS[role]["label"],
        "expires_at": now + ttl,
        "ttl_seconds": ttl,
    }


def verify_token(token: str, *, secret: bytes | None = None) -> dict[str, Any]:
    parts = (token or "").strip().split(".")
    if len(parts) != 3 or parts[0] != "v1":
        raise ValueError("令牌格式无效。")
    body, sig = parts[1], parts[2]
    key = secret or get_auth_secret()
    expected = _b64u_encode(hmac.new(key, f"v1.{body}".encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise ValueError("令牌签名校验失败。")
    try:
        payload = json.loads(_b64u_decode(body).decode("utf-8"))
    except Exception as exc:
        raise ValueError("令牌载荷解析失败。") from exc
    if not isinstance(payload, dict) or payload.get("role") not in ROLE_LEVEL:
        raise ValueError("令牌缺少合法角色声明。")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise ValueError("令牌已过期，请重新签发。")
    return payload


def required_role_for(method: str, path: str) -> RoleName | None:
    m = method.upper()
    # Public / probe / auth-issuance / static routes
    if path in ("/", "/healthz", "/readyz", "/docs", "/redoc", "/openapi.json", "/api/auth/token"):
        return None
    if path.startswith("/assets/"):
        return None
    for rule_m, prefix, min_role in ROUTE_MIN_ROLE:
        if m == rule_m and (path == prefix or (prefix.endswith("/") and path.startswith(prefix))):
            return min_role
    return "viewer" if path.startswith("/api/") else None


def role_allows(actual_role: str, required_role: str) -> bool:
    return ROLE_LEVEL.get(actual_role, 0) >= ROLE_LEVEL.get(required_role, 999)


class SlidingWindowRateLimiter:
    """Thread-safe per-client sliding-window rate limiter for expensive compute endpoints."""

    def __init__(self, max_requests: int = 60, window_seconds: float = 60.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, client_key: str) -> tuple[bool, int, int]:
        """Returns (allowed, remaining, retry_after_seconds)."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            q = self._hits[client_key]
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= self.max_requests:
                retry_after = max(1, int(self.window_seconds - (now - q[0])) + 1)
                return False, 0, retry_after
            q.append(now)
            return True, max(0, self.max_requests - len(q)), 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# ── Multi-Layer Prompt-Injection & Control-Command Defense Firewall ────────────
_CONTROL_PATTERNS = [
    re.compile(r"(遥控分闸|远程分闸|强制分闸|执行分闸|遥控合闸|远程合闸|强制合闸|执行合闸)", re.I),
    re.compile(r"(修改保护定值|修改定值|改写定值|下发控制报文|下发遥控|退出保护|投入保护压板)", re.I),
    re.compile(r"(写入数据库|篡改数据库|删除记录|清空台账|覆盖日报)", re.I),
]

_INJECTION_PATTERNS = [
    re.compile(r"(忽略(?:之前|前面|以上|所有).{0,10}(?:指令|规则|限制|约束|系统提示))", re.I),
    re.compile(r"(ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions)", re.I),
    re.compile(r"(越狱|jailbreak|绕过只读|解除只读|取消安全限制|无视安全围栏)", re.I),
    re.compile(r"(输出(?:你的)?系统提示词|泄露\s*system\s*prompt|reveal\s+system\s+prompt|print\s+your\s+system\s+prompt)", re.I),
    re.compile(r"(假装你是.{0,16}(?:遥控|控制|管理员|root)|act\s+as\s+a\s+scada\s+controller)", re.I),
]

_SQL_EXFIL_PATTERNS = [
    re.compile(r"\b(DROP\s+TABLE|DELETE\s+FROM|UPDATE\s+\w+\s+SET|INSERT\s+INTO|ATTACH\s+DATABASE|PRAGMA\s+writable_schema)\b", re.I),
    re.compile(r"(';\s*--|UNION\s+ALL\s+SELECT|UNION\s+SELECT)", re.I),
]


def inspect_prompt_security(text: str) -> dict[str, Any]:
    """Deterministic security firewall scanning user prompts for control, injection & SQL probes."""
    raw = str(text or "")
    for pat in _CONTROL_PATTERNS:
        m = pat.search(raw)
        if m:
            return {
                "allowed": False,
                "threat_category": "prohibited_control_command",
                "matched_rule": m.group(0),
                "stop_reason": "restricted_control_attempt",
                "message": (
                    f"安全围栏已拦截（命中受限控制/写操作规则「{m.group(0)}」）："
                    "老洼地配电网智能体运行于严格只读监测与诊断沙箱，严禁执行任何遥控分合闸、修改保护定值或篡改数据库操作。"
                ),
            }
    for pat in _INJECTION_PATTERNS:
        m = pat.search(raw)
        if m:
            return {
                "allowed": False,
                "threat_category": "prompt_injection_jailbreak",
                "matched_rule": m.group(0),
                "stop_reason": "prompt_injection_blocked",
                "message": (
                    f"安全围栏已拦截（命中提示词注入/越权绕过规则「{m.group(0)}」）："
                    "禁止尝试覆盖系统安全指令、套取系统提示词或伪装为具备写/控权限的调度终端。"
                ),
            }
    for pat in _SQL_EXFIL_PATTERNS:
        m = pat.search(raw)
        if m:
            return {
                "allowed": False,
                "threat_category": "sql_injection_attempt",
                "matched_rule": m.group(0),
                "stop_reason": "sql_injection_blocked",
                "message": (
                    f"安全围栏已拦截（命中非法 SQL/库写入探针「{m.group(0)}」）："
                    "所有查询仅允许通过白名单只读参数化工具执行。"
                ),
            }
    return {"allowed": True, "threat_category": None, "matched_rule": None, "stop_reason": None, "message": None}
