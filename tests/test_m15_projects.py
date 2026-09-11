# -*- coding: utf-8 -*-
"""M15 真实项目：三个命令行工具可运行的固化测试。

通过 monkeypatch 内建 input 与网络，让三个项目在测试里端到端跑通，
产出断言关键输出。这样「真实项目」不会被改坏。
"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from jishi.interpreter import run_source

PROJECTS = Path(__file__).parent.parent / "examples" / "projects"


def _run_project(name, script, inputs):
    """跑一个项目脚本，喂 inputs 列表作 input，返回 stdout。"""
    import builtins
    src = (PROJECTS / name / script).read_text(encoding="utf-8")
    buf = io.StringIO()
    it = iter(inputs)
    real_input = builtins.input
    builtins.input = lambda *a, **k: next(it, "")
    try:
        with redirect_stdout(buf):
            run_source(src)
    finally:
        builtins.input = real_input
    return buf.getvalue()


def test_project_成绩分析(monkeypatch, tmp_path):
    monkeypatch.chdir(PROJECTS / "成绩分析")
    out = _run_project("成绩分析", "分析.jsh", [""])  # 默认 成绩.csv
    assert "共 4 名学生" in out
    assert "小丽" in out
    assert "不及格" in out
    assert "小刚" in out


def test_project_日志统计(monkeypatch, tmp_path):
    monkeypatch.chdir(PROJECTS / "日志统计")
    out = _run_project("日志统计", "统计.jsh", [""])  # 默认 访问.log
    assert "总请求数：7" in out
    assert "独立 IP 数：5" in out
    assert "404" in out


def test_project_网页采集(monkeypatch, tmp_path):
    # 切到临时目录，避免「链接.csv」产物落到项目根
    monkeypatch.chdir(tmp_path)
    # 用本地 HTML 替代网络
    import importlib
    net = importlib.import_module("jishi.stdlib.网络")
    html = tmp_path / "测试.html"
    html.write_text(
        '<a href="https://example.com/a">甲</a>\n'
        '<a href="https://blog.example.com/b">乙</a>\n',
        encoding="utf-8")
    real_get = net.获取
    net.获取 = lambda url, 超时=10: html.read_text(encoding="utf-8")
    try:
        out = _run_project("网页采集", "采集.jsh", ["https://example.com"])
    finally:
        net.获取 = real_get
    assert "共提取到 2 个链接" in out
    assert "example.com" in out
