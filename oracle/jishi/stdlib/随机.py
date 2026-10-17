# -*- coding: utf-8 -*-
"""基石标准库：随机。薄封装 Python 的 random。"""

from __future__ import annotations

import random as _py_random

__all__ = ["随机整数", "随机小数", "随机选择", "洗牌"]


def 随机整数(最小: int, 最大: int) -> int:
    """返回 [最小, 最大] 之间的随机整数（含两端）。"""
    return _py_random.randint(int(最小), int(最大))


def 随机小数() -> float:
    """返回 0 到 1 之间的随机小数。"""
    return _py_random.random()


def 随机选择(序列) -> object:
    """从序列中随机挑一个元素。"""
    return _py_random.choice(序列)


def 洗牌(序列) -> list:
    """把列表顺序打乱，返回新列表（不修改原列表）。"""
    out = list(序列)
    _py_random.shuffle(out)
    return out
