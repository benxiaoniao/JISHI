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
    "累积", "组合", "排列", "笛卡尔积",
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


# -- M50 补缺（都是真实项目里手写过的） ---------------------------------------

def 累积(可迭代, 初值=0) -> list:
    """前缀和：返回「每一步累加到目前」的列表，长度与原序列相同。

    例：`迭代.累积([1, 2, 3])` → `[1, 3, 6]`；`迭代.累积([1, 2], 10)`
    → `[11, 13]`（`初值` 参与第一次累加）。

    做「进度百分比」「累计销量」这类图时，之前得手写循环 + 临时变量。
    """
    全部 = _as_iterable(可迭代, "「累积」的内容")
    out = []
    acc = 初值
    for v in 全部:
        acc = acc + v
        out.append(acc)
    return out


def 组合(可迭代, 取几个: int) -> list:
    """从序列里挑 `取几个` 个（**不计顺序**），返回所有组合。

    例：`迭代.组合([1, 2, 3], 2)` → `[[1, 2], [1, 3], [2, 3]]`。
    顺序与 Python 的 `itertools.combinations` 一致（按原序列下标递增）。
    """
    import itertools as _it
    n = int(取几个)
    if n < 0:
        raise RunTypeError(f"「组合」要取的个数不能是负数，得到了 {取几个}")
    全部 = _as_iterable(可迭代, "「组合」的内容")
    if n > len(全部):
        return []
    return [list(c) for c in _it.combinations(全部, n)]


def 排列(可迭代, 取几个: int = -1) -> list:
    """从序列里挑 `取几个` 个并**计顺序**，返回所有排列。

    不给 `取几个` 就是全排列：`迭代.排列([1, 2])` → `[[1, 2], [2, 1]]`。
    """
    import itertools as _it
    全部 = _as_iterable(可迭代, "「排列」的内容")
    n = len(全部) if 取几个 == -1 else int(取几个)
    if n < 0:
        raise RunTypeError(f"「排列」要取的个数不能是负数，得到了 {取几个}")
    if n > len(全部):
        return []
    return [list(c) for c in _it.permutations(全部, n)]


def 笛卡尔积(*序列) -> list:
    """返回多个序列的笛卡尔积（每个序列里各取一个，所有搭配）。

    例：`迭代.笛卡尔积([1, 2], "甲乙")` → `[[1, "甲"], [1, "乙"], [2, "甲"], [2, "乙"]]`。
    做「所有规格组合」「参数网格」时用。
    """
    import itertools as _it
    if not 序列:
        return []
    lists = [_as_iterable(s, f"「笛卡尔积」第 {i + 1} 个序列")
             for i, s in enumerate(序列)]
    if any(not x for x in lists):
        return []
    return [list(c) for c in _it.product(*lists)]
