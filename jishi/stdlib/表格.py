# -*- coding: utf-8 -*-
"""基石标准库：表格。CSV 表格数据的读写与查询（薄封装 Python csv 模块）。

设计：读表格 返回一个「表格」对象，配 表头/数据行/挑选/筛选/排序按/汇总
等操作，让零基础用户不需要理解嵌套列表也能做常见数据处理。
"""

from __future__ import annotations

import csv as _py_csv

from ..errors import RunFileError, RunTypeError

__all__ = [
    "读表格", "写表格", "写字典表格", "读字典表格", "追加",
    "表头", "数据行", "挑选", "筛选", "排序按", "汇总", "转置",
]


class _Table:
    """表格对象：表头 + 数据行。"""

    __slots__ = ("headers", "rows")

    def __init__(self, headers: list, rows: list):
        self.headers = list(headers)
        self.rows = [list(r) for r in rows]

    def __repr__(self):
        return f"<表格 {len(self.rows)} 行 x {len(self.headers)} 列>"


def _check_path(路径) -> str:
    if 路径 is None:
        raise RunTypeError("需要提供文件路径")
    return str(路径)


def _column_index(t: _Table, 列名) -> int:
    name = str(列名)
    try:
        return t.headers.index(name)
    except ValueError:
        raise RunTypeError(
            f"表格里没有「{name}」这一列",
            hint=f"现有的列：{'、'.join(t.headers)}")


# -- 读写 ------------------------------------------------------------------

def 读表格(路径) -> _Table:
    """读取带表头的 CSV 文件，返回表格对象。

    第一行是表头，例如 姓名,分数 → 表头(t) 得 ['姓名', '分数']。
    """
    try:
        with open(_check_path(路径), "r", encoding="utf-8-sig", newline="") as f:
            rows = [row for row in _py_csv.reader(f)]
    except FileNotFoundError:
        raise RunFileError(f"找不到文件「{路径}」")
    except OSError:
        raise RunFileError(f"文件「{路径}」读不出来")
    if not rows:
        raise RunFileError(f"文件「{路径}」是空的，没有表头")
    return _Table(rows[0], rows[1:])


def 写表格(路径, 行列表: list) -> None:
    """把行列表写进 CSV（覆盖原文件）。第一行应为表头。"""
    try:
        with open(_check_path(路径), "w", encoding="utf-8-sig", newline="") as f:
            writer = _py_csv.writer(f)
            for row in 行列表:
                writer.writerow(row)
    except OSError:
        raise RunFileError(f"没法写入文件「{路径}」")


def 写字典表格(路径, 字典列表: list) -> None:
    """把字典列表写进 CSV（键作表头，来自第一个字典）。"""
    if not isinstance(字典列表, list) or not 字典列表:
        raise RunTypeError("「写字典表格」需要一个非空的字典列表")
    first = 字典列表[0]
    if not isinstance(first, dict):
        raise RunTypeError(f"列表里的元素应该是字典，但出现了「{first}」")
    headers = list(first.keys())
    try:
        with open(_check_path(路径), "w", encoding="utf-8-sig", newline="") as f:
            writer = _py_csv.writer(f)
            writer.writerow(headers)
            for d in 字典列表:
                writer.writerow([d.get(h, "") for h in headers])
    except OSError:
        raise RunFileError(f"没法写入文件「{路径}」")


def 读字典表格(路径) -> list:
    """读取带表头的 CSV，返回字典列表：[{列名: 值}, ...]。"""
    try:
        with open(_check_path(路径), "r", encoding="utf-8-sig", newline="") as f:
            return list(_py_csv.DictReader(f))
    except FileNotFoundError:
        raise RunFileError(f"找不到文件「{路径}」")
    except OSError:
        raise RunFileError(f"文件「{路径}」读不出来")


def 追加(路径, 行: list) -> None:
    """往 CSV 文件末尾追加一行（文件不存在则创建，注意表头要自己保证）。"""
    try:
        with open(_check_path(路径), "a", encoding="utf-8-sig", newline="") as f:
            _py_csv.writer(f).writerow(行)
    except OSError:
        raise RunFileError(f"没法写入文件「{路径}」")


# -- 查询 ------------------------------------------------------------------

def 表头(t: _Table) -> list:
    """返回表格的表头（列名列表）。"""
    return list(t.headers)


def 数据行(t: _Table) -> list:
    """返回表格的所有数据行（不含表头）。"""
    return [list(r) for r in t.rows]


def 挑选(t: _Table, 列):
    """从表格里挑出一列或多列。

    挑选(t, "姓名") → 这列所有值的列表；
    挑选(t, ["姓名", "分数"]) → 每行只保留这两列。
    """
    if isinstance(列, (list, tuple)):
        indices = [_column_index(t, c) for c in 列]
        return [[row[i] for i in indices] for row in t.rows]
    i = _column_index(t, 列)
    return [row[i] for row in t.rows]


def 筛选(t: _Table, 列名, 值) -> _Table:
    """筛选出「列名」这一列的值等于「值」的行，返回新表格。"""
    i = _column_index(t, 列名)
    return _Table(t.headers, [row for row in t.rows if row[i] == 值])


def _sort_key(row, i):
    """数值感知排序键：能转数字按数字排，否则按文本排。"""
    v = row[i]
    try:
        return (0, float(v))
    except (TypeError, ValueError):
        return (1, str(v))


def 排序按(t: _Table, 列名, 倒序: bool = False) -> _Table:
    """按某一列排序，返回新表格；「倒序」为 真 时从大到小。"""
    i = _column_index(t, 列名)
    rows = sorted(t.rows, key=lambda row: _sort_key(row, i), reverse=bool(倒序))
    return _Table(t.headers, rows)


def 汇总(t: _Table, 列名) -> dict:
    """统计某数字列：个数/总和/平均/最大/最小。"""
    i = _column_index(t, 列名)
    values = []
    for row in t.rows:
        try:
            values.append(float(row[i]))
        except (TypeError, ValueError):
            raise RunTypeError(
                f"列「{列名}」里有不是数字的值「{row[i]}」，没法汇总")
    if not values:
        raise RunTypeError(f"表格没有数据行，没法汇总「{列名}」")
    return {
        "个数": len(values),
        "总和": sum(values),
        "平均": sum(values) / len(values),
        "最大": max(values),
        "最小": min(values),
    }


def 转置(行列表: list) -> list:
    """把「列表的列表」行列互换。"""
    if not 行列表:
        return []
    return [list(col) for col in zip(*行列表)]
