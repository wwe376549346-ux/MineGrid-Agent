"""Restricted read-only Python / SQL / NumPy / SciPy / pandapower analytical sandbox.

Enables the autonomous agent to answer open-ended electrical engineering questions
(correlation regressions, load-curve clustering, custom multi-hour statistics,
what-if pandapower simulations, and custom SVG-ready chart generation) without
mutating the host filesystem, reading secrets, or modifying the golden SQLite archives.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

from laowadi.monitor import EVENT_DB
from laowadi.storage import DB


MAX_CODE_CHARS = 16_000
MAX_STDOUT_CHARS = 12_000
MAX_STDERR_CHARS = 4_000
MAX_TABLES = 6
MAX_TABLE_ROWS = 120
MAX_CHARTS = 4
MAX_CHART_POINTS = 250
DEFAULT_TIMEOUT_S = 8.0
MAX_TIMEOUT_S = 25.0

ALLOWED_IMPORTS = frozenset({
    "math",
    "statistics",
    "json",
    "collections",
    "itertools",
    "datetime",
    "re",
    "functools",
    "operator",
    "decimal",
    "fractions",
    "numpy",
    "scipy",
    "scipy.stats",
    "scipy.optimize",
    "scipy.cluster",
    "scipy.cluster.vq",
    "pandapower",
})

FORBIDDEN_CALLS = frozenset({
    "open",
    "exec",
    "eval",
    "compile",
    "__import__",
    "getattr",
    "setattr",
    "delattr",
    "globals",
    "locals",
    "vars",
    "dir",
    "input",
    "breakpoint",
    "memoryview",
    "type",
})

_BLOCKED_ATTRIBUTES = frozenset({
    "ag_frame", "gi_frame", "cr_frame", "f_globals", "f_locals", "f_builtins",
    "tb_frame", "f_back", "gi_yieldfrom", "cr_await", "ag_await", "attrgetter",
    "methodcaller", "format", "format_map",
    "system", "popen", "spawn", "unlink", "remove", "rmdir", "mkdir", "write_text",
    "write_bytes", "to_sql", "to_csv", "to_excel", "to_json", "to_pickle", "save",
    "savez", "savez_compressed", "dump", "load", "loadtxt", "genfromtxt", "fromfile",
    "tofile", "memmap", "DataSource", "ctypeslib", "f2py", "read_csv", "read_pickle",
    "from_pickle", "from_json", "from_excel",
})


def _restricted_attribute(name: str) -> bool:
    return name.startswith("_") or name in _BLOCKED_ATTRIBUTES


_RESULT_START = "__MINEGRID_SANDBOX_JSON_START__"
_RESULT_END = "__MINEGRID_SANDBOX_JSON_END__"


def validate_sandbox_code(code: str) -> list[str]:
    """Static AST security audit before launching the isolated sandbox subprocess."""
    violations: list[str] = []
    if not isinstance(code, str) or not code.strip():
        return ["分析代码不能为空。"]
    if len(code) > MAX_CODE_CHARS:
        return [f"代码长度超限（最大 {MAX_CODE_CHARS} 字符）。"]

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"Python 语法错误（第 {exc.lineno} 行）：{exc.msg}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name
                if mod not in ALLOWED_IMPORTS:
                    violations.append(f"禁止导入模块：{mod}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:
                violations.append("禁止相对导入。")
            elif mod not in ALLOWED_IMPORTS:
                violations.append(f"禁止从模块导入：{mod}")
            for alias in node.names:
                if alias.name == "*" or _restricted_attribute(alias.name):
                    violations.append(f"禁止导入内部或受限属性：{alias.name}")
        elif isinstance(node, ast.Attribute):
            if _restricted_attribute(node.attr):
                violations.append(f"禁止访问内部或受限属性：{node.attr}")
        elif isinstance(node, ast.Name):
            if node.id.startswith("__") or node.id in FORBIDDEN_CALLS:
                violations.append(f"禁止使用保留或受限标识符：{node.id}")

    # Deduplicate preserving order
    return list(dict.fromkeys(violations))


def _resolve_db_path(archive: Any, fallback: Path) -> Path:
    if archive is not None and getattr(archive, "db", None) is not None:
        try:
            row = archive.db.execute("PRAGMA database_list").fetchone()
            if row:
                # sqlite3.Row or tuple: (seq, name, file)
                file_path = row["file"] if hasattr(row, "keys") else row[2]
                if file_path:
                    return Path(file_path).resolve()
        except Exception:
            pass
    return fallback.resolve()


def _resolve_event_db_path(archive: Any, fallback: Path) -> Path | None:
    if archive is not None:
        ev = getattr(archive, "events_db", None) or getattr(archive, "event_db_path", None)
        return Path(ev).resolve() if ev else None
    return fallback.resolve()


def _clean_json_number(val: Any) -> Any:
    if isinstance(val, bool) or val is None:
        return val
    if isinstance(val, (int, float)):
        if math.isnan(val) or math.isinf(val):
            return None
        return round(float(val), 6) if isinstance(val, float) else val
    return val


def _sanitize_tables(raw_tables: list[dict]) -> list[dict]:
    out: list[dict] = []
    for tbl in (raw_tables or [])[:MAX_TABLES]:
        if not isinstance(tbl, dict):
            continue
        cols = [str(c) for c in (tbl.get("columns") or [])][:24]
        rows = []
        for r in (tbl.get("rows") or [])[:MAX_TABLE_ROWS]:
            if isinstance(r, (list, tuple)):
                rows.append([_clean_json_number(v) if isinstance(v, (int, float, bool, type(None))) else str(v)[:160] for v in r[: len(cols) or 24]])
        out.append({
            "title": str(tbl.get("title") or "分析结果表")[:120],
            "columns": cols,
            "rows": rows,
        })
    return out


def _sanitize_charts(raw_charts: list[dict]) -> list[dict]:
    out: list[dict] = []
    for ch in (raw_charts or [])[:MAX_CHARTS]:
        if not isinstance(ch, dict):
            continue
        ctype = str(ch.get("chart_type") or "line").lower()
        if ctype not in {"line", "bar", "scatter"}:
            ctype = "line"
        clean_series = []
        for s in (ch.get("series") or [])[:6]:
            if not isinstance(s, dict):
                continue
            pts = []
            for pt in (s.get("points") or [])[:MAX_CHART_POINTS]:
                if not isinstance(pt, dict):
                    continue
                x = pt.get("x")
                y = _clean_json_number(pt.get("y"))
                if y is None:
                    continue
                x_clean = _clean_json_number(x) if isinstance(x, (int, float)) else str(x)[:48]
                item = {"x": x_clean, "y": y}
                if pt.get("label") is not None:
                    item["label"] = str(pt.get("label"))[:64]
                pts.append(item)
            if pts:
                clean_series.append({"name": str(s.get("name") or "系列")[:64], "points": pts})
        if clean_series:
            out.append({
                "title": str(ch.get("title") or "自定义分析图表")[:120],
                "chart_type": ctype,
                "x_label": str(ch.get("x_label") or "")[:48],
                "y_label": str(ch.get("y_label") or "")[:48],
                "series": clean_series,
            })
    return out


_RUNNER_SCRIPT = r'''
import builtins
import collections
import contextlib
import datetime
import io
import json
import math
import re
import sqlite3
import statistics
import sys
import traceback

import os
import resource
import threading
import time
from pathlib import Path

payload = json.loads(sys.stdin.read())
user_code = payload["code"]
db_path = payload["db_path"]
event_db_path = payload["event_db_path"]

def _limit(kind, ceiling):
    _, hard = resource.getrlimit(kind)
    if hard != resource.RLIM_INFINITY:
        ceiling = min(ceiling, hard)
    resource.setrlimit(kind, (ceiling, ceiling))

_limit(resource.RLIMIT_CPU, payload["cpu_seconds"])
_limit(resource.RLIMIT_NOFILE, 96)
_limit(resource.RLIMIT_FSIZE, 0)
_limit(resource.RLIMIT_CORE, 0)
memory_bytes = 2 * 1024 ** 3
try:
    _limit(resource.RLIMIT_AS, memory_bytes)
except (ValueError, OSError):
    if sys.platform != "darwin":
        raise

    def _enforce_rss():
        while True:
            if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss > memory_bytes:
                os._exit(70)
            time.sleep(0.05)

    threading.Thread(target=_enforce_rss, daemon=True).start()

ALLOWED_IMPORTS = set(payload["allowed_imports"])
_real_import = builtins.__import__

def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level != 0:
        raise ImportError("Relative imports are disabled in sandbox.")
    if name not in ALLOWED_IMPORTS and name.split(".")[0] not in ALLOWED_IMPORTS:
        raise ImportError(f"Module '{name}' is blocked in read-only sandbox.")
    return _real_import(name, globals, locals, fromlist, level)

_SAFE_BUILTINS = {
    k: getattr(builtins, k)
    for k in (
        "abs", "all", "any", "bin", "bool", "bytes", "callable", "chr", "complex",
        "dict", "divmod", "enumerate", "filter", "float", "format", "frozenset",
        "hash", "hex", "int", "isinstance", "issubclass", "iter", "len", "list",
        "map", "max", "min", "next", "object", "oct", "ord", "pow", "print",
        "range", "repr", "reversed", "round", "set", "slice", "sorted", "str",
        "sum", "tuple", "zip",
        "Exception", "ValueError", "TypeError", "KeyError", "IndexError", "ZeroDivisionError",
    )
}
_SAFE_BUILTINS["__import__"] = _safe_import

_READONLY_SQL_RE = re.compile(r"^\s*(?:SELECT|WITH|EXPLAIN\s+QUERY\s+PLAN|PRAGMA\s+table_info)\b", re.I)

def _open_ro(path_str):
    conn = sqlite3.connect(Path(path_str).as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn

_main_conn = _open_ro(db_path)
_event_conn = _open_ro(event_db_path) if event_db_path else None

def query_sql(sql, params=()):
    if not isinstance(sql, str) or not _READONLY_SQL_RE.match(sql):
        raise ValueError("仅允许执行只读 SELECT / WITH 查询。")
    if ";" in sql.strip().rstrip(";"):
        raise ValueError("禁止多语句 SQL。")
    cur = _main_conn.execute(sql, tuple(params) if params is not None else ())
    rows = cur.fetchmany(5000)
    return [dict(r) for r in rows]

def query_events(sql, params=()):
    if _event_conn is None:
        return []
    if not isinstance(sql, str) or not _READONLY_SQL_RE.match(sql):
        raise ValueError("仅允许执行只读 SELECT / WITH 查询。")
    if ";" in sql.strip().rstrip(";"):
        raise ValueError("禁止多语句 SQL。")
    cur = _event_conn.execute(sql, tuple(params) if params is not None else ())
    rows = cur.fetchmany(2000)
    return [dict(r) for r in rows]

_tables = []
_charts = []

def emit_table(title, columns, rows):
    _tables.append({
        "title": str(title),
        "columns": list(columns),
        "rows": [list(r) for r in rows],
    })

def emit_chart(title, chart_type, x_label, y_label, series):
    _charts.append({
        "title": str(title),
        "chart_type": str(chart_type),
        "x_label": str(x_label),
        "y_label": str(y_label),
        "series": series,
    })

env = {
    "__builtins__": _SAFE_BUILTINS,
    "query_sql": query_sql,
    "query_events": query_events,
    "emit_table": emit_table,
    "emit_chart": emit_chart,
    "math": math,
    "statistics": statistics,
    "json": json,
    "re": re,
    "datetime": datetime,
    "collections": collections,
}
try:
    import numpy as np
    env["np"] = np
except Exception:
    pass

stdout_buf = io.StringIO()
stderr_buf = io.StringIO()
status = "ok"
try:
    compiled = compile(user_code, "<minegrid_sandbox>", "exec")
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        exec(compiled, env, env)
except Exception:
    status = "error"
    stderr_buf.write(traceback.format_exc())
finally:
    try:
        _main_conn.close()
    except Exception:
        pass
    if _event_conn is not None:
        try:
            _event_conn.close()
        except Exception:
            pass

out = {
    "status": status,
    "stdout": stdout_buf.getvalue(),
    "stderr": stderr_buf.getvalue(),
    "tables": _tables,
    "charts": _charts,
}
sys.stdout.write(payload["start_marker"])
sys.stdout.write(json.dumps(out, ensure_ascii=False, default=str))
sys.stdout.write(payload["end_marker"])
'''


def run_python_analysis(
    archive: Any | None,
    code: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    container_image: str | None = None,
) -> dict[str, Any]:
    """Execute read-only analytical Python code in an isolated subprocess against the archive."""
    t0 = time.monotonic()
    code_str = str(code or "")
    code_sha = hashlib.sha256(code_str.encode("utf-8")).hexdigest()[:16]
    clamped_timeout = max(0.5, min(float(timeout_s or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))

    violations = validate_sandbox_code(code_str)
    if violations:
        return {
            "status": "blocked",
            "stdout": "",
            "stderr": "安全沙盒已拦截：" + "；".join(violations),
            "violations": violations,
            "tables": [],
            "charts": [],
            "elapsed_ms": round((time.monotonic() - t0) * 1000.0, 2),
            "code_sha256": code_sha,
        }

    db_path = _resolve_db_path(archive, DB)
    event_db_path = _resolve_event_db_path(archive, EVENT_DB)
    payload = {
        "code": code_str,
        "db_path": str(db_path),
        "event_db_path": str(event_db_path) if event_db_path and event_db_path.is_file() else "",
        "allowed_imports": sorted(ALLOWED_IMPORTS),
        "cpu_seconds": math.ceil(clamped_timeout),
        "start_marker": _RESULT_START,
        "end_marker": _RESULT_END,
    }

    # Strip all secrets (API keys, DB credentials, tokens) from child env
    clean_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "PYTHONUTF8": "1",
        "LANG": "C.UTF-8",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "PYTHONNOUSERSITE": "1",
    }

    container_image = container_image or os.environ.get("LAOWADI_SANDBOX_CONTAINER_IMAGE")
    command = [sys.executable, "-B", "-c", _RUNNER_SCRIPT]
    container_name = None
    if container_image:
        container_name = f"minegrid-analysis-{uuid4().hex}"
        docker = shutil.which("docker") or "docker"
        command = [
            docker, "run", "--rm", "-i", "--name", container_name,
            "--pull=never", "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=64",
            "--memory=2g", "--memory-swap=2g", "--cpus=1", "--user=65534:65534",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
        ]
        for key, path in (("db_path", db_path), ("event_db_path", event_db_path)):
            if payload[key]:
                target = f"/{key}.sqlite3"
                command.extend(["--mount", f"type=bind,source={path},target={target},readonly"])
                payload[key] = target
        for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "PYTHONNOUSERSITE"):
            command.extend(["-e", f"{key}={clean_env[key]}"])
        command.extend(["--entrypoint", "python3", "--", container_image, "-B", "-c", _RUNNER_SCRIPT])

    try:
        proc = subprocess.run(
            command,
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=clamped_timeout,
            env=clean_env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        timed_out = isinstance(exc, subprocess.TimeoutExpired)
        if timed_out and container_name:
            try:
                subprocess.run([command[0], "rm", "-f", container_name], env=clean_env,
                               capture_output=True, timeout=3, check=True)
            except (OSError, subprocess.SubprocessError):
                pass
        return {
            "status": "timeout" if timed_out else "error",
            "stdout": "",
            "stderr": f"沙盒执行超时（超过 {clamped_timeout:.1f}s 限制）。" if timed_out else str(exc),
            "tables": [],
            "charts": [],
            "elapsed_ms": round((time.monotonic() - t0) * 1000.0, 2),
            "code_sha256": code_sha,
        }

    raw_out = proc.stdout or ""
    s_idx = raw_out.find(_RESULT_START)
    e_idx = raw_out.rfind(_RESULT_END)
    if s_idx < 0 or e_idx < s_idx + len(_RESULT_START):
        err_msg = (proc.stderr or raw_out or f"沙盒进程异常退出 (rc={proc.returncode})")[:MAX_STDERR_CHARS]
        return {
            "status": "error",
            "stdout": "",
            "stderr": err_msg,
            "tables": [],
            "charts": [],
            "elapsed_ms": round((time.monotonic() - t0) * 1000.0, 2),
            "code_sha256": code_sha,
        }

    parsed = json.loads(raw_out[s_idx + len(_RESULT_START) : e_idx])
    stdout_txt = str(parsed.get("stdout") or "")
    stderr_txt = str(parsed.get("stderr") or "")
    if len(stdout_txt) > MAX_STDOUT_CHARS:
        stdout_txt = stdout_txt[:MAX_STDOUT_CHARS] + "\n...[stdout truncated]"
    if len(stderr_txt) > MAX_STDERR_CHARS:
        stderr_txt = stderr_txt[:MAX_STDERR_CHARS] + "\n...[stderr truncated]"

    return {
        "status": parsed.get("status", "ok"),
        "stdout": stdout_txt,
        "stderr": stderr_txt,
        "tables": _sanitize_tables(parsed.get("tables") or []),
        "charts": _sanitize_charts(parsed.get("charts") or []),
        "elapsed_ms": round((time.monotonic() - t0) * 1000.0, 2),
        "code_sha256": code_sha,
    }
