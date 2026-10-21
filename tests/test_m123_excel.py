# -*- coding: utf-8 -*-
"""M123：`Excel` 读。

**取值口径**的判据主要在语料 `tests/cases/41_标准库_M123.jsh`（那样才三宿主逐字节对拍）。
这个文件补两件语料管不到的事：

1. **别人写的真 .xlsx** —— 语料里的 xlsx 是我们自己用 `压缩.打包` 拼的，
   而真实文件是 openpyxl / Excel 生成的（有共享字符串表、有 styles、有 rels）。
   两份都读得对，才算「能读 Excel」。
2. **共享字符串表**（`t="s"`）—— 语料用的是 `inlineStr`，这条路径只在真文件里出现。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from oracle.jishi.stdlib import Excel, 表格  # noqa: E402


@pytest.fixture()
def 真文件(tmp_path):
    """让 **openpyxl 自己写**一份 —— 不是我们拼的 XML。"""
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "销售"
    ws.append(["月份", "金额", "备注"])
    ws.append(["一月", 1200, "正常"])
    ws.append(["二月", 980.5, None])
    ws.append(["三月", 1500, None])
    表二 = wb.create_sheet("汇总")
    表二.append(["合计"])
    表二.append([3680.5])
    p = tmp_path / "真.xlsx"
    wb.save(p)
    return p


def test_读_openpyxl_写的真文件(真文件):
    簿 = Excel.读工作簿(str(真文件))
    assert 簿["工作表们"] == ["销售", "汇总"], "工作表名要按标签页顺序"
    assert 簿["路径"] == str(真文件)

    表 = Excel.读工作表(簿, "销售")
    assert 表格.表头(表) == ["月份", "金额", "备注"]
    assert 表格.数据行(表) == [
        ["一月", 1200, "正常"],
        ["二月", 980.5, None],
        ["三月", 1500, None],
    ]


def test_整数不会被读成小数(真文件):
    """xlsx 里数字都是文本存的，`1200` 读出来必须还是 `1200` 而不是 `1200.0`。

    ⚠️ 这条单独写，是因为它最容易在「字符串转数字」时被顺手写成 double。
    """
    行们 = 表格.数据行(Excel.读工作表(Excel.读工作簿(str(真文件)), "销售"))
    金额 = 行们[0][1]
    assert 金额 == 1200
    assert isinstance(金额, int), f"整数被读成了 {type(金额).__name__}"
    assert isinstance(行们[1][1], float), "有小数点的应当给小数"


def test_空格子是空而不是空串(真文件):
    """Excel 里没填的格子是 `空`；把它读成 `""` 会让「有没有填」判不出来。"""
    行们 = 表格.数据行(Excel.读工作表(Excel.读工作簿(str(真文件)), "销售"))
    assert 行们[1][2] is None
    assert 行们[2][2] is None


def test_读出来能喂给表格的算子(真文件):
    """读出来的必须是**同一个**表格类型，能直接 `汇总` / `筛选`。"""
    表 = Excel.读工作表(Excel.读工作簿(str(真文件)), "销售")
    汇总 = 表格.汇总(表, "金额")
    assert 汇总["个数"] == 3
    assert 汇总["总和"] == 3680.5
    # ⚠️ `筛选` 是**按值相等**筛（列名 + 值），不是谓词 —— 别传 lambda
    筛 = 表格.筛选(表, "月份", "一月")
    assert 表格.数据行(筛) == [["一月", 1200, "正常"]]


def test_名字写错要说清有哪些(真文件):
    簿 = Excel.读工作簿(str(真文件))
    with pytest.raises(Exception) as ei:
        Excel.读工作表(簿, "没有这个")
    话 = str(ei.value)
    assert "没有叫「没有这个」的工作表" in 话
    assert "销售" in 话 and "汇总" in 话, "要把可用的名字列出来"


def test_不是_xlsx_说清楚(tmp_path):
    p = tmp_path / "假.xlsx"
    p.write_bytes(b"this is not a zip")
    with pytest.raises(Exception) as ei:
        Excel.读工作簿(str(p))
    assert ".xlsx" in str(ei.value) and ".xls" in str(ei.value)
