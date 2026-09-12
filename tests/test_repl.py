# -*- coding: utf-8 -*-
"""M1-3：REPL 交互增强测试（通过子进程管道验证）。"""
import subprocess
import sys
import os

REPL_CMD = [sys.executable, "-m", "jishi.cli", "-i"]


def run_repl(input_text: str) -> str:
    proc = subprocess.run(
        REPL_CMD + ["-i"], input=input_text, capture_output=True,
        text=True, encoding="utf-8", cwd=os.path.dirname(os.path.dirname(__file__)),
        timeout=30)
    return proc.stdout + proc.stderr


def test_repl_expression_echo():
    out = run_repl('1 + 2\n退出\n')
    assert "3" in out


def test_repl_string_echo_repr():
    out = run_repl('"你好"\n退出\n')
    assert "'你好'" in out


def test_repl_bool_chinese():
    out = run_repl("真 与 假\n退出\n")
    assert "假" in out
    assert "False" not in out


def test_repl_none_no_echo():
    out = run_repl("空\n1 + 2\n退出\n")
    # 空 求值为 None，不应产生任何回显（输出里不该出现「空」字）
    assert "空" not in out
    assert "3" in out


def test_repl_multiline_block():
    out = run_repl("如果 真：\n    打印(1)\n    打印(2)\n\n退出\n")
    assert "1\n2" in out


def test_repl_help():
    out = run_repl("帮助\n退出\n")
    assert "内建函数" in out and "对象方法" in out


def test_repl_error_recovery():
    # 写错代码后仍能继续输入
    out = run_repl("打印(平均)\n1 + 2\n退出\n")
    assert "找不到" in out
    assert "3" in out
