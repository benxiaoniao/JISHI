# -*- coding: utf-8 -*-
"""M9.3 MCP Server：让 agent（WorkBuddy 等）通过 MCP 协议调用基石。

stdio 传输（换行分隔的 JSON-RPC 2.0），暴露三个工具：
- run_script(script, ...)：沙箱执行基石脚本（subprocess 隔离，真正超时 kill）
- eval_expr(expr)：沙箱求值一个表达式
- describe_language()：返回 AI 语言卡

入口：python -m jishi.mcp_server
日志一律走 stderr（stdout 被 JSON-RPC 占用）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any, Optional

from .version import VERSION

PROTOCOL_VERSION = "2024-11-05"

# ---------------------------------------------------------------------------
# 工具定义（tools/list 的 inputSchema）
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "run_script",
        "description": "在沙箱里执行一段基石（中文编程语言）脚本，返回执行结果。"
                       "脚本用中文关键字（函数/如果/遍历/打印…）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "script": {"type": "string",
                           "description": "要执行的基石源码"},
                "timeout": {"type": "number", "description": "超时秒数，默认 5"},
                "allow_write": {"type": "boolean",
                                "description": "是否允许文件读写，默认否"},
                "allow_python": {"type": "boolean",
                                 "description": "是否允许 Python 桥接导入，默认否"},
            },
            "required": ["script"],
        },
    },
    {
        "name": "eval_expr",
        "description": "在沙箱里求值一个基石表达式，返回其值。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "expr": {"type": "string", "description": "要求值的表达式"},
            },
            "required": ["expr"],
        },
    },
    {
        "name": "describe_language",
        "description": "返回基石语言卡（语法速查 + 内建 API），供 agent 学习如何写基石代码。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _run_subprocess(script: str, timeout: float,
                    allow_write: bool, allow_python: bool) -> str:
    """用 subprocess 隔离执行（真正 kill 超时子进程），返回 protocol JSON 字符串。"""
    args = [sys.executable, "-m", "jishi.cli", "--stdin",
            "--sandbox", "--json-result", "--timeout", str(timeout)]
    # 通过环境变量传递沙箱选项（CLI 层面未展开的 allow_write/allow_python）
    env = dict(__import__("os").environ)
    if allow_write:
        env["JISHI_SANDBOX_ALLOW_WRITE"] = "1"
    if allow_python:
        env["JISHI_SANDBOX_ALLOW_PYTHON"] = "1"
    try:
        proc = subprocess.run(
            args, input=script, capture_output=True, text=True,
            timeout=timeout + 2.0, env=env,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({
            "ok": False,
            "error": {"code": "E2000", "title": "运行期错误",
                      "message": f"执行超时（{timeout} 秒）", "line": None,
                      "col": None, "hint": None},
            "duration_ms": timeout * 1000,
        }, ensure_ascii=False)
    out = proc.stdout.strip()
    if out:
        return out
    # 子进程没输出 JSON（异常情况），回退为内部错误
    return json.dumps({
        "ok": False,
        "error": {"code": "E0000", "title": "内部错误",
                  "message": f"子进程异常退出（code={proc.returncode}）: "
                             f"{proc.stderr[:200]}", "line": None, "col": None,
                  "hint": None},
        "duration_ms": 0,
    }, ensure_ascii=False)


def _tool_run_script(args: dict) -> str:
    script = args.get("script", "")
    timeout = float(args.get("timeout", 5.0))
    allow_write = bool(args.get("allow_write", False))
    allow_python = bool(args.get("allow_python", False))
    return _run_subprocess(script, timeout, allow_write, allow_python)


def _tool_eval_expr(args: dict) -> str:
    from .sandbox import SandboxOpts, eval_sandboxed
    from .protocol import dumps
    result = eval_sandboxed(args.get("expr", ""), SandboxOpts())
    return dumps(result)


def _tool_describe_language(_args: dict) -> str:
    from .ai import render_ai_card
    return json.dumps({"ok": True, "card": render_ai_card()},
                      ensure_ascii=False)


def _dispatch(name: str, args: dict) -> str:
    """分发工具调用，返回结果文本。"""
    if name == "run_script":
        return _tool_run_script(args)
    if name == "eval_expr":
        return _tool_eval_expr(args)
    if name == "describe_language":
        return _tool_describe_language(args)
    return json.dumps({"ok": False, "error": {"message": f"未知工具 {name}"}},
                      ensure_ascii=False)


# ---------------------------------------------------------------------------
# JSON-RPC 处理
# ---------------------------------------------------------------------------

def _respond(msg: dict, result: Any) -> None:
    out = {"jsonrpc": "2.0", "id": msg.get("id"), "result": result}
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _respond_error(msg: dict, code: int, message: str) -> None:
    out = {"jsonrpc": "2.0", "id": msg.get("id"),
           "error": {"code": code, "message": message}}
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle(msg: dict) -> None:
    method = msg.get("method")
    if method == "initialize":
        _respond(msg, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "jishi-mcp", "version": VERSION},
        })
    elif method == "notifications/initialized":
        pass  # 通知，无需响应
    elif method == "tools/list":
        _respond(msg, {"tools": TOOLS})
    elif method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name", "")
        args = params.get("arguments", {})
        try:
            text = _dispatch(name, args)
        except Exception as e:  # noqa: BLE001
            text = json.dumps(
                {"ok": False, "error": {"message": f"{type(e).__name__}: {e}"}},
                ensure_ascii=False)
        _respond(msg, {"content": [{"type": "text", "text": text}]})
    elif method == "ping":
        _respond(msg, {})
    else:
        _respond_error(msg, -32601, f"方法未实现：{method}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        _handle(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
