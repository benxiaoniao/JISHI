# -*- coding: utf-8 -*-
"""M1-4：教程配套示例测试（保证教程里每个示例都能跑）。"""
import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EX = ROOT / "examples" / "tutorial"

sys.path.insert(0, str(ROOT))

from jishi.interpreter import run_source  # noqa: E402


def run_file_stdout(name: str) -> str:
    src = (EX / name).read_text(encoding="utf-8")
    out = io.StringIO()
    with redirect_stdout(out):
        run_source(src, filename=name)
    return out.getvalue()


# -- 无输入示例（黄金输出） -------------------------------------------------

def test_tutorial_hello():
    assert run_file_stdout("01_你好.jsh") == "你好， 世界 ！\n现在是 2026 年\n"


def test_tutorial_list():
    out = run_file_stdout("03_清单.jsh")
    assert "['牛奶', '鸡蛋', '苹果']" in out
    assert "共 3 样东西" in out
    assert "牛奶还在" in out
    assert "基石语言真好学" in out
    assert "真" in out


def test_tutorial_multiplication_table():
    out = run_file_stdout("05_九九乘法表.jsh")
    lines = out.strip().splitlines()
    assert len(lines) == 9
    assert lines[0].strip() == "1x1=1"
    assert lines[8].strip().endswith("9x9=81")


# -- M2 教程示例（第 4-7 章） ------------------------------------------------

def test_tutorial_functions():
    out = run_file_stdout("06_函数演示.jsh")
    lines = out.strip().splitlines()
    assert "你好，小明！" in lines[0]
    assert lines[2] == "平均分： 85.0"
    assert lines[3] == "斐波那契数列第 10 项： 55"
    assert lines[-1] == "程序结束"


def test_tutorial_contacts():
    out = run_file_stdout("07_通讯录.jsh")
    assert "小红的电话： 13800000002" in out
    assert "小刚的电话： 没有登记" in out
    assert "共 3 位联系人" in out
    assert "删除后剩 2 位" in out


def test_tutorial_file_io(tmp_path, monkeypatch):
    # 示例在当前目录写临时文件，切到 tmp 里运行避免污染项目目录
    monkeypatch.chdir(tmp_path)
    out = run_file_stdout("08_读写文件.jsh")
    assert "基石语言真有趣！" in out
    assert "日记： 9月1日：学了函数" in out
    assert "演示文件已清理" in out


def test_tutorial_csv_analysis(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = run_file_stdout("09_成绩表分析.jsh")
    assert "姓名列： ['小明', '小红', '小刚', '小丽']" in out
    assert "平均分： 85.75" in out
    assert "第一名： 小红" in out
    assert "演示完成" in out


def test_tutorial_expense_tracker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = run_file_stdout("10_记账程序.jsh")
    assert "共 4 笔，合计 72.0 元" in out
    assert "最贵的一笔： 买书" in out
    assert "演示完成（账本已清理）" in out


# -- 交互示例（子进程冒烟） --------------------------------------------------

def _run_cli(path: str, inputs: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "jishi.cli", path],
        input=inputs, capture_output=True, text=True, encoding="utf-8",
        cwd=str(ROOT), timeout=30)


def test_tutorial_calculator():
    proc = _run_cli("examples/tutorial/02_计算器.jsh", "3.5\n2\n")
    assert proc.returncode == 0, proc.stderr
    assert "3.5 + 2.0 = 5.5" in proc.stdout


def test_tutorial_guess_number():
    # 输入 1..100，答案必在其中；正常结束（猜中或次数用完）即可
    inputs = "\n".join(str(i) for i in range(1, 101)) + "\n"
    proc = _run_cli("examples/tutorial/04_猜数字.jsh", inputs)
    assert proc.returncode == 0, proc.stderr
    assert ("猜对了" in proc.stdout) or ("次数用完了" in proc.stdout)
