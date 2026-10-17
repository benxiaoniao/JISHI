# -*- coding: utf-8 -*-
"""M106：数据整理补的四件 —— `表格.分组汇总` / `表格.连接` / `迭代.归约` / `迭代.分区`。

来历：探针（M105）里记账本撞出「按类别汇总只能手写『有就加、没有就置』」，
日志统计升级时又要反复写「按 X 分组算 Y」—— 这四件就是那类动作的通用形态。

⚠️ 顺带核实的「**不缺**」（不许重复造轮子）：
* 文本清洗（去两端 / 替换 / 查找 / 拆分）—— **字符串方法早就有了**
  （`.去空白()` / `.替换()` / `.查找()` / `.拆分()`，见 `docs/语言规格.md` §9.5）；
* 表格选列 —— `表格.挑选` **本来就支持多列**（`挑选(t, ["甲", "乙"])`）。
"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oracle.jishi.interpreter import run_source  # noqa: E402


def _跑(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        run_source(src)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 表格：分组汇总 / 连接
# ---------------------------------------------------------------------------

_成绩CSV = "姓名,班级,数学\n小明,一班,90\n小红,一班,80\n小刚,二班,70\n小丽,二班,60\n"


def test_分组汇总(monkeypatch, tmp_path):
    (tmp_path / "成绩.csv").write_text(_成绩CSV, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = _跑(
        '导入 表格\n'
        '令 t = 表格.读表格("成绩.csv")\n'
        '令 各班 = 表格.分组汇总(t, "班级", "数学")\n'
        '打印(各班["一班"]["个数"], 各班["一班"]["平均"], 各班["二班"]["总和"])\n'
    )
    assert out.strip() == "2 85.0 130.0", out


def test_分组汇总的每组字典与汇总长得一样(monkeypatch, tmp_path):
    """学会 `汇总` 就会 `分组汇总` —— 每组的键必须完全一致。"""
    (tmp_path / "成绩.csv").write_text(_成绩CSV, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = _跑(
        '导入 表格\n'
        '令 t = 表格.读表格("成绩.csv")\n'
        '令 单 = 表格.汇总(t, "数学")\n'
        '令 组 = 表格.分组汇总(t, "班级", "数学")\n'
        '打印(单.键() == 组["一班"].键())\n'
    )
    assert out.strip() == "真", out


def test_连接_内连接(monkeypatch, tmp_path):
    (tmp_path / "甲.csv").write_text("学号,姓名\n1,小明\n2,小红\n3,小刚\n", encoding="utf-8")
    (tmp_path / "乙.csv").write_text("学号,分数\n1,90\n3,70\n4,50\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = _跑(
        '导入 表格\n'
        '令 甲 = 表格.读表格("甲.csv")\n'
        '令 乙 = 表格.读表格("乙.csv")\n'
        '令 合 = 表格.连接(甲, 乙, "学号")\n'
        '打印(合)\n'
        '打印(表格.表头(合))\n'
        '遍历 行 在 表格.数据行(合)：\n'
        '    打印(行)\n'
    )
    # 学号 2 只在甲表、4 只在乙表 → 内连接后只剩 1 / 3 两行
    assert "<表格 2 行 x 3 列>" in out, out
    assert "['学号', '姓名', '分数']" in out, out
    assert "['1', '小明', '90']" in out, out
    assert "['3', '小刚', '70']" in out, out


# ---------------------------------------------------------------------------
# 迭代：归约 / 分区
# ---------------------------------------------------------------------------

def test_归约():
    out = _跑(
        '导入 迭代\n'
        '打印(迭代.归约([1, 2, 3, 4], 函数(甲, 乙)：甲 + 乙))\n'
        '打印(迭代.归约([1, 2], 函数(甲, 乙)：甲 + 乙, 10))\n'
        '打印(迭代.归约(["甲", "乙", "丙"], 函数(甲, 乙)：甲 + "-" + 乙))\n'
    )
    assert out.strip() == "10\n13\n甲-乙-丙", out


def test_归约_空列表无初值要报错():
    from oracle.jishi.errors import RunValueError

    with pytest.raises(RunValueError):
        _跑('导入 迭代\n打印(迭代.归约([], 函数(甲, 乙)：甲))\n')


def test_归约_空列表给了初值就返回初值():
    out = _跑('导入 迭代\n打印(迭代.归约([], 函数(甲, 乙)：甲, 7))\n')
    assert out.strip() == "7", out


def test_分区():
    out = _跑(
        '导入 迭代\n'
        '令 通过, 没过 = 迭代.分区([55, 80, 42, 91], 函数(x)：x >= 60)\n'
        '打印(通过, 没过)\n'
    )
    assert out.strip() == "[80, 91] [55, 42]", out


def test_分区_两边都保持原顺序():
    out = _跑(
        '导入 迭代\n'
        '令 真堆, 假堆 = 迭代.分区([1, 2, 3, 4, 5, 6], 函数(x)：x % 2 == 0)\n'
        '打印(真堆, 假堆)\n'
    )
    assert out.strip() == "[2, 4, 6] [1, 3, 5]", out


# ---------------------------------------------------------------------------
# 核实「不缺」的三件（防止有人以后重复造）
# ---------------------------------------------------------------------------

def test_文本清洗_字符串方法本来就有():
    out = _跑(
        '打印("  甲  ".去空白())\n'
        '打印("a-b-c".替换("-", "+"))\n'
        '打印("a,b,c".拆分(","))\n'
        '打印("abc".查找("b"))\n'
    )
    assert out.strip() == "甲\na+b+c\n['a', 'b', 'c']\n1", out


def test_表格挑选_本来就支持多列(monkeypatch, tmp_path):
    (tmp_path / "成绩.csv").write_text(_成绩CSV, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = _跑(
        '导入 表格\n'
        '令 t = 表格.读表格("成绩.csv")\n'
        '打印(表格.挑选(t, ["姓名", "数学"]))\n'
    )
    assert "['小明', '90']" in out, out
