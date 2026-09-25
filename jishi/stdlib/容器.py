# -*- coding: utf-8 -*-
"""基石标准库：容器。字典/列表的批量统计与整理工具。

**为什么叫「容器」而不是「集合」**：`集合` 是内建函数名（`集合([1,2,3])`
去重造集合）。若标准库模块也叫「集合」，`导入 集合` 会把那个内建遮蔽掉
——正是 M40 修掉的 f-string 遮蔽坑同一类问题。改成「容器」既避开了遮蔽，
语义也更准：这里装的是**装东西的工具**（计数、分组、取前），不是集合类型。

写 `日志统计` 时手写「有就加一、没有就置一」+ 自己排序，正是本模块要消掉的
绕道写法。
"""

from __future__ import annotations

from ..errors import RunTypeError
from ..runtime import _as_iterable

__all__ = [
    "计数", "最多", "按值排序", "分组", "取前", "取后", "分块",
]


def 计数(可迭代) -> dict:
    """数每个元素出现了几次，返回「元素 → 次数」的字典。

    等价于 Python 的 `collections.Counter(...)`。保持**首次出现的顺序**，
    所以打印出来是稳定的（本项目所有容器都保序）。
    """
    出: dict = {}
    for 项 in _as_iterable(可迭代, "「计数」的内容"):
        出[项] = 出.get(项, 0) + 1
    return 出


def 最多(可迭代, 个数: int = 1):
    """出现次数最多的前 `个数` 个，返回 [[元素, 次数], …]（降序）。

    平局时按**首次出现的先后**排（保序，不引入随机性）。
    `个数=1` 时返回单个元素（不是列表），方便 `令 冠军 = 容器.最多(词)`。
    """
    表 = 计数(可迭代)
    序 = {键: i for i, 键 in enumerate(表)}          # 首次出现的位置
    项 = sorted(表.items(), key=lambda kv: (-kv[1], 序[kv[0]]))
    if 个数 <= 0:
        raise RunTypeError(f"「最多」的个数要大于 0，得到了 {个数}")
    选出 = [[k, v] for k, v in 项[:int(个数)]]
    if int(个数) == 1:
        return 选出[0][0] if 选出 else None
    return 选出


def 按值排序(频次字典, 降序: bool = True) -> list:
    """把「元素 → 次数」这类字典按**值**排成 [[元素, 值], …]。

    `统计.jsh` 里手写「转成 [[值, 键]] 再排序再反转」就是这件事。
    平局时按键的首次出现顺序排（保序）。
    """
    if not isinstance(频次字典, dict):
        raise RunTypeError(
            f"「按值排序」要一个字典，得到了「{type(频次字典).__name__}」")
    序 = {键: i for i, 键 in enumerate(频次字典)}
    项 = sorted(频次字典.items(),
                key=lambda kv: ((-kv[1] if 降序 else kv[1]), 序[kv[0]]))
    return [[k, v] for k, v in 项]


def 分组(可迭代, 键函数) -> dict:
    """按「键函数(元素)」的返回值把元素分组，返回「键 → [元素, …]」。

    例：`容器.分组(单词们, 函数(w)：长度(w))` 按词长分组。
    """
    出: dict = {}
    for 项 in _as_iterable(可迭代, "「分组」的内容"):
        键 = 键函数(项)
        出.setdefault(键, []).append(项)
    return 出


def 取前(可迭代, 个数: int) -> list:
    """取前 `个数` 个元素（不足就给全部）。"""
    return _as_iterable(可迭代, "「取前」的内容")[:max(0, int(个数))]


def 取后(可迭代, 个数: int) -> list:
    """取后 `个数` 个元素（不足就给全部）。"""
    全部 = _as_iterable(可迭代, "「取后」的内容")
    n = max(0, int(个数))
    return 全部[len(全部) - n:] if n else []


def 分块(可迭代, 每块个数: int) -> list:
    """把序列切成若干等长小块（最后一块可能短）。

    例：`容器.分块(范围(10), 3)` → `[[0,1,2], [3,4,5], [6,7,8], [9]]`。
    """
    n = int(每块个数)
    if n <= 0:
        raise RunTypeError(f"「分块」的每块个数要大于 0，得到了 {每块个数}")
    全部 = _as_iterable(可迭代, "「分块」的内容")
    return [全部[i:i + n] for i in range(0, len(全部), n)]
