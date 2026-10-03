# -*- coding: utf-8 -*-
"""M17 排错体验补全：递归翻译 / 调用栈回溯 / 协议补全 / REPL 源码行。"""

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

sys.path.insert(0, ".")

from jishi import interpreter
from jishi.errors import JishiError, translate_python_exception
from jishi.interpreter import Interpreter, run_source


def _run(src, filename="<输入>"):
    """跑一段源码，返回捕获到的 JishiError（无异常返回 None）。"""
    try:
        run_source(src, filename)
        return None
    except JishiError as e:
        return e


# ---------------------------------------------------------------------------
# 17.1 递归超限走中文翻译
# ---------------------------------------------------------------------------

def test_recursion_translated():
    e = _run("函数 递归(n):\n    返回 递归(n + 1)\n递归(1)\n")
    assert e is not None
    assert e.code == "E2000"
    assert "递归" in e.message          # 中文说明，不再是英文 RecursionError
    assert "结束条件" in e.hint


def test_translate_recursion_error_directly():
    e = translate_python_exception(RecursionError("maximum recursion depth"))
    assert isinstance(e, JishiError)
    assert "递归" in e.message
    assert e.code == "E2000"


# ---------------------------------------------------------------------------
# 17.2 调用栈回溯
# ---------------------------------------------------------------------------

def test_call_trace_rendered():
    src = ("函数 里层():\n"
           "    令 x = [1, 2]\n"
           "    打印(x[10])\n"
           "函数 中层():\n"
           "    里层()\n"
           "函数 外层():\n"
           "    中层()\n"
           "外层()\n")
    e = _run(src)
    assert e.trace is not None
    names = [f.func for f in e.trace]
    # 从外到内：外层 → 中层 → 里层
    assert names == ["外层", "中层", "里层"]
    # 渲染含调用链
    rendered = e.render()
    assert "调用链" in rendered
    assert "外层" in rendered and "中层" in rendered and "里层" in rendered


def test_call_trace_absent_when_no_error():
    src = "函数 加(a, b):\n    返回 a + b\n打印(加(1, 2))\n"
    # 正常执行无异常，也不应有残留帧
    interp = Interpreter()
    from jishi.tokenizer import tokenize
    from jishi.parser import parse
    prog = parse(tokenize(src, "x"), src.split("\n"), "x")
    interp.run(prog)
    assert interp._call_stack == []


def test_error_to_json_includes_trace():
    from jishi.ai import error_to_json
    src = ("函数 里层():\n"
           "    令 x = [1]\n"
           "    打印(x[5])\n"
           "里层()\n")
    e = _run(src)
    d = error_to_json(e)
    assert "trace" in d
    assert d["trace"][-1]["func"] == "里层"


# ---------------------------------------------------------------------------
# 17.3 沙箱/协议错误补全
# ---------------------------------------------------------------------------

def test_sandbox_timeout_has_hint():
    from jishi.sandbox import SandboxOpts, run_sandboxed
    r = run_sandboxed("令 x = 0\n当 真:\n    x = x + 1\n",
                      SandboxOpts(timeout=0.3))
    assert r["ok"] is False
    assert "死循环" in r["error"]["hint"]
    assert "超时" in r["error"]["message"]


def test_sandbox_recursion_translated():
    from jishi.sandbox import SandboxOpts, run_sandboxed
    r = run_sandboxed("函数 递归(n):\n    返回 递归(n + 1)\n递归(1)\n",
                      SandboxOpts(timeout=5))
    assert r["ok"] is False
    assert "递归" in r["error"]["message"]      # 中文，不是 RecursionError
    assert "trace" in r["error"]                # 带调用链


# ---------------------------------------------------------------------------
# 17.4 REPL 报错显示源码行
# ---------------------------------------------------------------------------

def test_repl_error_shows_source_line(monkeypatch):
    from jishi import repl
    # 用 input 序列模拟 REPL：输入一行除零，再退出
    buf_out = io.StringIO()
    buf_err = io.StringIO()
    calls = iter(["打印(1/0)", "退出"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(calls))
    with redirect_stdout(buf_out), redirect_stderr(buf_err):
        repl.repl()
    err = buf_err.getvalue()
    # 应包含源码行和 ^ 指示
    assert "打印(1/0)" in err
    assert "^" in err
