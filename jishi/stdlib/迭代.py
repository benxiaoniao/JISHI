# -*- coding: utf-8 -*-
"""基石标准库：迭代。遍历序列时的常用整理动作。

`带下标` 与 `配对` 是**内建**（与 Python 的 enumerate / zip 同地位，用起来
不用先导入），本模块装的是它们之外的常用动作：分组、滑窗、去重、展开。
"""

from __future__ import annotations

from ..errors import RunTypeError
from ..runtime import _as_iterable

__all__ = [
    "分组", "滑窗", "去重", "展开", "取前", "取后", "求和", "计数",
]


def 分组(可迭代, 每组个数: int) -> list:
    """按固定大小分组，返回 [[…], […]]（最后组可能短）。

    例：`迭代.分组(范围(7), 3)` → `[[0,1,2], [3,4,5], [6]]`。
    """
    n = int(每组个数)
    if n <= 0:
        raise RunTypeError(f"「分组」的每组个数要大于 0，得到了 {每组个数}")
    全部 = _as_iterable(可迭代, "「分组」的内容")
    return [全部[i:i + n] for i in range(0, len(全部), n)]


def 滑窗(可迭代, 窗口大小: int, 步长: int = 1) -> list:
    """滑动窗口：每次取连续 `窗口大小` 个元素，按 `步长` 往后挪。

    例：`迭代.滑窗([1,2,3,4], 2)` → `[[1,2], [2,3], [3,4]]`
    （做「相邻两项之差」这类计算时用）。
    凑不满一个窗口的尾巴丢掉（Python 的 `itertools` 同此约定）。
    """
    n = int(窗口大小)
    s = int(步长)
    if n <= 0:
        raise RunTypeError(f"「滑窗」的窗口大小要大于 0，得到了 {窗口大小}")
    if s <= 0:
        raise RunTypeError(f"「滑窗」的步长要大于 0，得到了 {步长}")
    全部 = _as_iterable(可迭代, "「滑窗」的内容")
    出 = []
    i = 0
    while i + n <= len(全部):
        出.append(全部[i:i + n])
        i += s
    return 出


def 去重(可迭代) -> list:
    """按首次出现的顺序去重，返回列表（要集合类型就用内建 `集合()`）。"""
    出: list = []
    见过: set = set()
    for 项 in _as_iterable(可迭代, "「去重」的内容"):
        try:
            键 = 项
            if 键 in 见过:
                continue
            见过.add(键)
        except TypeError:                     # 列表/字典这类不可哈希的
            键 = repr(项)
            if 键 in 见过:
                continue
            见过.add(键)
        出.append(项)
    return 出


def 展开(嵌套) -> list:
    """把「列表的列表」摊平一层。

    例：`迭代.展开([[1,2], [3], [4,5]])` → `[1,2,3,4,5]`。
    只摊平**一层**（多层嵌套要自己套几遍，免得写错了还不知道摊到哪层）。
    """
    出: list = []
    for 段 in _as_iterable(嵌套, "「展开」的内容"):
        try:
            出.extend(_as_iterable(段, "「展开」的每一段"))
        except RunTypeError:
            出.append(段)
    return 出


def 取前(可迭代, 个数: int) -> list:
    """取前 `个数` 个元素（不足就给全部）。"""
    return _as_iterable(可迭代, "「取前」的内容")[:max(0, int(个数))]


def 取后(可迭代, 个数: int) -> list:
    """取后 `个数` 个元素（不足就给全部）。"""
    全部 = _as_iterable(可迭代, "「取后」的内容")
    n = max(0, int(个数))
    return 全部[len(全部) - n:] if n else []


def 求和(可迭代) -> float:
    """求和（等价于内建 `总和`，放在这里方便 `迭代.求和` 一路写下来）。"""
    return sum(_as_iterable(可迭代, "「求和」的内容"))


def 计数(可迭代, 条件) -> int:
    """数一数有多少元素满足条件（条件是「元素 → 真/假」的函数）。

    例：`迭代.计数(分数们, 函数(x)：x >= 60)`。
    """
    个数 = 0
    for 项 in _as_iterable(可迭代, "「计数」的内容"):
        if 条件(项):
            个数 += 1
    return 个数
