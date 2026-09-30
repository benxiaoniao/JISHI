# -*- coding: utf-8 -*-
"""M9.3 MCP Server：让 agent（WorkBuddy 等）通过 MCP 协议调用基石。

stdio 传输（换行分隔的 JSON-RPC 2.0），暴露七个工具（M45 起从三个补到七个，
把「跑片段 / 看报错 / 查规格 / 读标准库」四条路走全）：

| 工具 | 作用 |
|---|---|
| `run_script(script, ...)` | 沙箱执行基石脚本（subprocess 隔离，真正超时 kill） |
| `eval_expr(expr)` | 沙箱求值一个表达式 |
| `describe_language()` | 返回 AI 语言卡 |
| `check_source(source)` | 静态检查（M44 checker 的同一份实现） |
| `lookup_error(code)` | 查错误码 / 静态检查码的含义 |
| `lookup_stdlib(module[, function])` | 查标准库签名与说明 |
| `format_source(source[, indent])` | 按官方风格格式化 |

**全部工具都从语言规格（`ai.build_lang_spec()`）与既有实现取事实，不另抄一份**：
错误码表来自 `errors` 登记、检查码来自 `checker.CHECK_CATALOG`、标准库签名来自
扫 `stdlib/*.py`——规格变了工具自动跟着变，不会各说各话。

入口：python -m jishi.mcp_server
日志一律走 stderr（stdout 被 JSON-RPC 占用）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional


def _cli_command() -> list[str]:
    """构造「调用基石 CLI」的命令前缀。

    源码运行时是 ``[python, -m, jishi.cli]``；但 PyInstaller 打包后
    ``sys.executable`` 指向 ``jishi-mcp.exe`` **自己**，不是 Python 解释器，
    照原样调用会得到「子进程异常退出（code=0）」。此时改用发行目录里同级的
    ``jishi`` 可执行文件（布局为 ``bin/jishi/jishi.exe`` 与
    ``bin/jishi-mcp/jishi-mcp.exe``，互为兄弟目录）。
    """
    if not getattr(sys, "frozen", False):
        return [sys.executable, "-m", "jishi.cli"]

    exe = Path(sys.executable)
    suffix = ".exe" if os.name == "nt" else ""
    candidates = [
        exe.parent / f"jishi{suffix}",                  # 同目录
        exe.parent.parent / "jishi" / f"jishi{suffix}",  # 发行版 bin/ 布局
    ]
    for cand in candidates:
        if cand.is_file():
            return [str(cand)]
    # 找不到同级 jishi：退回自己（至少能让上层给出可读的错误信息）
    return [str(exe)]

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
    # --- M45 补齐：把「跑片段 / 看报错 / 查规格 / 读标准库」四条路走全 -----
    {
        "name": "check_source",
        "description": "对一段基石源码做静态检查（不运行），返回未定义名 / 遮蔽内建 / "
                       "未使用变量 / 可疑相等等问题。写代码后自查、或排查"
                       "「为什么编辑器画了黄线」时用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "要检查的基石源码"},
            },
            "required": ["source"],
        },
    },
    {
        "name": "lookup_error",
        "description": "查基石错误码的含义。拿到报错里的 E 码（如 E2002）后，"
                       "用它换到中文标题、触发类与修复建议。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string",
                         "description": "错误码，如 E2002；也接受静态检查码如 name.undefined"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "lookup_stdlib",
        "description": "查基石标准库的用法。传模块名列出该模块全部函数与签名；"
                       "再传函数名则返回该函数的说明。遇到「这个功能有没有内建」时先查这里，"
                       "别自己手写。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "module": {"type": "string",
                           "description": "标准库模块名，如 数学 / 容器 / 迭代 / 正则"},
                "function": {"type": "string",
                             "description": "可选，模块内的函数名；不传则列出整个模块"},
            },
            "required": ["module"],
        },
    },
    {
        "name": "format_source",
        "description": "按基石官方风格格式化一段源码（统一缩进与空格），返回格式化后的代码。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "要格式化的基石源码"},
                "indent": {"type": "number",
                           "description": "缩进空格数，默认 4"},
            },
            "required": ["source"],
        },
    },
]


def _run_subprocess(script: str, timeout: float,
                    allow_write: bool, allow_python: bool) -> str:
    """用 subprocess 隔离执行（真正 kill 超时子进程），返回 protocol JSON 字符串。"""
    args = [*_cli_command(), "--stdin",
            "--sandbox", "--json-result", "--timeout", str(timeout)]
    # 通过环境变量传递沙箱选项（CLI 层面未展开的 allow_write/allow_python）
    env = dict(os.environ)
    if allow_write:
        env["JISHI_SANDBOX_ALLOW_WRITE"] = "1"
    if allow_python:
        env["JISHI_SANDBOX_ALLOW_PYTHON"] = "1"
    try:
        proc = subprocess.run(
            args, input=script, capture_output=True,
            # 显式 UTF-8：只给 text 模式而不指定编码时，Python 会用本地代码页
            # （Windows 中文机是 GBK）编码子进程输入，中文源码在管道里就坏了。
            encoding="utf-8", errors="replace",
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


def _tool_check_source(args: dict) -> str:
    """静态检查一段源码（M44 的 checker，命令行 `jishi 源码静态检查` 的同一份）。"""
    from .checker import check_source
    src = args.get("source", "")
    issues = check_source(src)
    return json.dumps({
        "ok": True,
        "问题数": len(issues),
        "问题": [i.to_dict() for i in issues],
    }, ensure_ascii=False)


def _tool_lookup_error(args: dict) -> str:
    """查错误码 / 静态检查码的含义。两张表都来自语言规格（不另抄一份）。"""
    from .ai import build_lang_spec
    code = str(args.get("code", "")).strip()
    if not code:
        return json.dumps({"ok": False, "error": {"message": "缺少 code 参数"}},
                          ensure_ascii=False)
    spec = build_lang_spec()
    # 静态检查码（name.undefined 这种）先查——它们不是 E 码
    for item in spec.get("check_codes", []):
        if item["code"].lower() == code.lower():
            return json.dumps({"ok": True, "kind": "静态检查码", **item},
                              ensure_ascii=False)
    for item in spec.get("error_codes", []):
        if item["code"].upper() == code.upper():
            return json.dumps({"ok": True, "kind": "错误码", **item},
                              ensure_ascii=False)
    return json.dumps({
        "ok": False,
        "error": {"message": f"没有这个错误码：{code}"},
        "可用错误码": [i["code"] for i in spec.get("error_codes", [])],
        "可用检查码": [i["code"] for i in spec.get("check_codes", [])],
    }, ensure_ascii=False)


def _tool_lookup_stdlib(args: dict) -> str:
    """查标准库：只给 module 就列全部函数，再给 function 就查那一个。"""
    from .ai import build_lang_spec
    name = str(args.get("module", "")).strip()
    if not name:
        return json.dumps({"ok": False, "error": {"message": "缺少 module 参数"}},
                          ensure_ascii=False)
    spec = build_lang_spec()
    mods = {m["module"]: m for m in spec.get("stdlib", [])}
    if name not in mods:
        return json.dumps({
            "ok": False,
            "error": {"message": f"没有这个标准库模块：{name}"},
            "可用模块": sorted(mods),
            "提示": "内建函数（不用 导入 就能用）见 describe_language 返回的语言卡",
        }, ensure_ascii=False)
    mod = mods[name]
    func = str(args.get("function", "")).strip()
    if not func:
        return json.dumps({
            "ok": True, "module": name,
            "用法": f"导入 {name}",
            "函数": mod["functions"],
            "提示": "再传 function 可查单个函数的详细签名",
        }, ensure_ascii=False)
    for f in mod["functions"]:
        if f["name"] == func:
            return json.dumps({
                "ok": True, "module": name, "用法": f"{name}.{func}",
                **f,
            }, ensure_ascii=False)
    return json.dumps({
        "ok": False,
        "error": {"message": f"模块「{name}」里没有函数「{func}」"},
        "可用函数": [f["name"] for f in mod["functions"]],
    }, ensure_ascii=False)


def _tool_format_source(args: dict) -> str:
    """按官方风格格式化（与 `jishi 格式化` 同一份实现）。

    ⚠️ 先做一道**解析前置校验**：格式化器只按缩进层级重排文本，遇到语法错
    （例如 `如果 真` 少了冒号）它**不会报错**，而是照原样返回一个半成品。
    对 agent 来说这是最坏的结果——它以为拿到的是干净代码。所以这里先解析一遍，
    语法错就如实返回错误，让 agent 先修语法再格式化。
    """
    from .checker import parse_source
    from .errors import JishiError
    from .formatter import format_source

    src = args.get("source", "")
    indent = int(args.get("indent", 4) or 4)

    try:
        parse_source(src, "<格式化>")
    except JishiError as e:
        return json.dumps({
            "ok": False,
            "error": {"message": f"源码有语法错，请先修好再格式化：{e.title}"
                                 + (f"（{e.message}）" if e.message else ""),
                      "code": e.code, "line": e.line, "col": e.col,
                      "hint": e.hint},
        }, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return json.dumps({
            "ok": False,
            "error": {"message": f"源码解析失败：{type(e).__name__}: {e}"},
        }, ensure_ascii=False)

    try:
        out = format_source(src, indent=indent)
    except Exception as e:  # noqa: BLE001
        return json.dumps({
            "ok": False,
            "error": {"message": f"格式化失败：{type(e).__name__}: {e}"},
        }, ensure_ascii=False)
    return json.dumps({
        "ok": True, "格式化后": out,
        "是否已符合风格": out == src.replace("\r\n", "\n").replace("\r", "\n"),
    }, ensure_ascii=False)


def _dispatch(name: str, args: dict) -> str:
    """分发工具调用，返回结果文本。"""
    if name == "run_script":
        return _tool_run_script(args)
    if name == "eval_expr":
        return _tool_eval_expr(args)
    if name == "describe_language":
        return _tool_describe_language(args)
    if name == "check_source":
        return _tool_check_source(args)
    if name == "lookup_error":
        return _tool_lookup_error(args)
    if name == "lookup_stdlib":
        return _tool_lookup_stdlib(args)
    if name == "format_source":
        return _tool_format_source(args)
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


def _iter_stdin_lines():
    """逐行读标准输入（按字节读 + UTF-8 解码）。

    同 cli._read_stdin 的理由：Windows 上文本层默认按 GBK 解码，JSON-RPC
    里夹带的中文源码会变乱码。字节层读取不受环境编码影响。
    """
    buf = getattr(sys.stdin, "buffer", None)
    if buf is None:
        for line in sys.stdin:
            yield line
        return
    for raw in buf:
        if isinstance(raw, str):
            yield raw
            continue
        try:
            yield raw.decode("utf-8")
        except UnicodeDecodeError:
            yield raw.decode("utf-8", "replace")


def main() -> int:
    for line in _iter_stdin_lines():
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
