# -*- coding: utf-8 -*-
"""M9 测试：统一协议 / 沙箱 / MCP Server。"""

import json
import sys

import pytest

sys.path.insert(0, ".")

from jishi import protocol
from jishi.sandbox import SandboxOpts, eval_sandboxed, run_sandboxed


# -- 9.2 协议 ----------------------------------------------------------------

def test_protocol_success():
    r = protocol.success(stdout="2\n", value=None, duration_ms=1.5)
    assert r["ok"] is True
    assert r["stdout"] == "2\n"
    assert r["duration_ms"] == 1.5


def test_protocol_failure():
    from jishi.errors import JishiError
    err = JishiError("测试", line=1, col=2, fix={"old": "a", "new": "b"})
    r = protocol.failure(err, duration_ms=3.0)
    assert r["ok"] is False
    assert r["error"]["code"] == "E0000"
    assert r["error"]["fix"]["old"] == "a"


def test_protocol_dumps():
    r = protocol.success(stdout="你好")
    assert json.loads(protocol.dumps(r))["ok"] is True


# -- 9.1 沙箱 ----------------------------------------------------------------

def test_sandbox_normal():
    r = run_sandboxed("打印(1 + 1)")
    assert r["ok"] is True
    assert r["stdout"] == "2\n"


def test_sandbox_syntax_error_with_fix():
    r = run_sandboxed("def f():")
    assert r["ok"] is False
    assert r["error"]["fix"]["old"] == "def"


def test_sandbox_blocks_file_write():
    r = run_sandboxed("导入 文件\n文件.写文本(\"x.txt\", \"hi\")")
    assert r["ok"] is False
    assert "文件" in r["error"]["message"]


def test_sandbox_allow_write():
    r = run_sandboxed("导入 文件\n文件.写文本(\"x.txt\", \"hi\")",
                      SandboxOpts(allow_write=True))
    assert r["ok"] is True
    import os
    os.remove("x.txt")


def test_sandbox_blocks_python_import():
    r = run_sandboxed("导入 math 从 python")
    assert r["ok"] is False
    assert "从 python" in r["error"]["message"]


def test_sandbox_allow_python_but_block_network():
    r = run_sandboxed("导入 socket 从 python",
                      SandboxOpts(allow_python_import=True))
    assert r["ok"] is False
    assert "socket" in r["error"]["message"]


def test_sandbox_stdout_limit():
    r = run_sandboxed("循环 10000 次:\n    打印(\"长输出\")",
                      SandboxOpts(max_stdout=100))
    assert r["ok"] is False
    assert "上限" in r["error"]["message"]


def test_sandbox_timeout():
    # 用「睡眠」测超时（不占 CPU，daemon 线程随进程结束回收）
    r = run_sandboxed("导入 时间\n时间.睡眠(10)",
                      SandboxOpts(timeout=0.3))
    assert r["ok"] is False
    assert "超时" in r["error"]["message"]


def test_eval_sandboxed():
    r = eval_sandboxed("3 * 7")
    assert r["ok"] is True
    assert r["value"] == 21


# -- 9.3 MCP Server ----------------------------------------------------------

def test_mcp_dispatch_run_script():
    from jishi.mcp_server import _tool_run_script
    raw = _tool_run_script({"script": "打印(1 + 2)"})
    r = json.loads(raw)
    assert r["ok"] is True
    assert r["stdout"] == "3\n"


def test_mcp_dispatch_eval_expr():
    from jishi.mcp_server import _tool_eval_expr
    raw = _tool_eval_expr({"expr": "2 ** 10"})
    r = json.loads(raw)
    assert r["ok"] is True
    assert r["value"] == 1024


def test_mcp_describe_language():
    from jishi.mcp_server import _tool_describe_language
    raw = _tool_describe_language({})
    r = json.loads(raw)
    assert r["ok"] is True
    assert "语言卡" in r["card"]


def test_mcp_tools_list():
    from jishi.mcp_server import TOOLS
    names = [t["name"] for t in TOOLS]
    # M45 把工具面从 3 个补到 7 个（写片段 / 静态检查 / 查错误码 / 查标准库 / 格式化）。
    # 这条断言原先写死 3 个名字，M45 加了工具就红了——改成「前三个在、七个齐」，
    # 保留它本来的意图（工具面是稳定的契约），又不挡后续新增。
    assert names[:3] == ["run_script", "eval_expr", "describe_language"]
    assert set(names) == {
        "run_script", "eval_expr", "describe_language",
        "check_source", "lookup_error", "lookup_stdlib", "format_source",
    }
    assert len(names) == len(set(names)), "工具名有重复"
