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


def test_repl_none_echoes_chinese():
    """「空」也是合法的表达式值，与「真」一样要回显中文。

    以前这里断言「不回显」——根因是 `_last_value` 用 `None` 同时表示
    「没有表达式语句」和「表达式的值是空」，两种含义撞在一个值上。
    M27 换成哨兵后，输入 `空` 回显 `空`，与 `真` 回显 `真` 一致。
    """
    out = run_repl("空\n1 + 2\n退出\n")
    assert "空" in out
    assert "None" not in out
    assert "3" in out


def test_repl_statement_without_value_no_echo():
    """没有表达式语句的输入（如 `令 甲 = 42`）不该回显值。"""
    out = run_repl("令 甲 = 42\n退出\n")
    assert "\n42\n" not in out


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
