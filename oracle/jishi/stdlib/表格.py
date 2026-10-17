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
    "从记录", "转记录",
    "表头", "数据行", "挑选", "筛选", "排序按", "汇总", "分组汇总", "连接", "转置",
]


class _Table:
    """表格对象：表头 + 数据行。"""

    __slots__ = ("headers", "rows")

    def __init__(self, headers: list, rows: list):
        self.headers = list(headers)
        self.rows = [list(r) for r in rows]

    #: 给 `类型()` 用的中文名（见 runtime.type_name 的钩子）
    _jishi_type_name = "表格"

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


def 分组汇总(t: _Table, 分组列, 值列) -> dict:
    """按「分组列」的取值分组，对「值列」做统计，返回 `{组: 汇总字典}`。

    每组的那个汇总字典与 `汇总(t, 列名)` **长得一模一样**
    （个数 / 总和 / 平均 / 最大 / 最小）—— 学会一个就会另一个。

    例（按班级算数学平均）：:

        令 各班 = 表格.分组汇总(表, "班级", "数学")
        打印(各班["一班"]["平均"])

    ⚠️ 组按**首次出现**的顺序放进字典（不排序 —— 键可能是文本，也可能是数字，
    混着排序会炸）。
    """
    i组 = _column_index(t, 分组列)
    i值 = _column_index(t, 值列)
    桶: dict = {}
    for row in t.rows:
        桶.setdefault(row[i组], []).append(row[i值])
    出: dict = {}
    for 组, 值们 in 桶.items():
        数值 = []
        for v in 值们:
            try:
                数值.append(float(v))
            except (TypeError, ValueError):
                raise RunTypeError(
                    f"列「{值列}」里有不是数字的值「{v}」，没法汇总")
        出[组] = {
            "个数": len(数值),
            "总和": sum(数值),
            "平均": sum(数值) / len(数值),
            "最大": max(数值),
            "最小": min(数值),
        }
    return 出


def 连接(表甲: _Table, 表乙: _Table, 键甲, 键乙="") -> _Table:
    """按某一列把两张表**接起来**（只留两边都有的键），返回新表格。

    `键乙` 不写就当作和 `键甲` 同名的列。新表的列 = 甲表的列 + 乙表的列
    （乙表那个键列不再重复，免得同一列出现两次）。

    例：`表格.连接(学生表, 成绩表, "学号")`

    ⚠️ 只做**内连接**（两边都有的键才留）；乙表同一个键有多行时**只取第一条**
    —— 需要「一对多展开」的场景请先自己把乙表理成一对一。
    """
    i甲 = _column_index(表甲, 键甲)
    i乙 = _column_index(表乙, 键乙 if 键乙 else 键甲)
    索引: dict = {}
    for row in 表乙.rows:
        索引.setdefault(row[i乙], row)
    出表头 = list(表甲.headers) + [
        h for j, h in enumerate(表乙.headers) if j != i乙]
    出行 = []
    for row in 表甲.rows:
        配对 = 索引.get(row[i甲])
        if 配对 is not None:
            出行.append(list(row) + [v for j, v in enumerate(配对) if j != i乙])
    return _Table(出表头, 出行)


def 从记录(记录们: list) -> _Table:
    """把**字典列表**变成表格 —— JSON 接口回来的数据直接能做表格运算。

    列名取所有记录里出现过（按**首次出现**顺序），缺的格子填空。

    例（这才是它存在的理由）：::

        令 订单们 = 网络.获取JSON(网址)["本页"]     # [{"客户": "甲", "金额": 12}, …]
        令 表 = 表格.从记录(订单们)
        打印(表格.分组汇总(表, "客户", "金额"))

    ⚠️ **M107 之前没有它**：那时只能先 `表格.写字典表格` 落一个临时 CSV、
    再 `表格.读表格` 读回来 —— 一次纯内存的整理被迫走了一遍磁盘（探针
    「接口汇总」的绕道 R103）。
    """
    if not isinstance(记录们, (list, tuple)):
        raise RunTypeError("「从记录」要传一个**字典列表**（例如 JSON 接口返回的那种）")
    列名: list = []
    for rec in 记录们:
        if not isinstance(rec, dict):
            raise RunTypeError(
                f"「从记录」的每一项都要是字典，遇到了「{type(rec).__name__}」")
        for k in rec:
            if k not in 列名:
                列名.append(k)
    行 = [[rec.get(k) for k in 列名] for rec in 记录们]
    return _Table([str(k) for k in 列名], 行)


def 转记录(t: _Table) -> list:
    """把表格变成**字典列表** —— 好喂给 `json.写文件` 或 `网络.提交JSON`。"""
    if not isinstance(t, _Table):
        raise RunTypeError("「转记录」要传一个表格（用 表格.读表格 或 表格.从记录 得到）")
    头 = [str(h) for h in t.headers]
    return [dict(zip(头, row)) for row in t.rows]


def 转置(行列表: list) -> list:
    """把「列表的列表」行列互换。"""
    if not 行列表:
        return []
    return [list(col) for col in zip(*行列表)]
