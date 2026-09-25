# -*- coding: utf-8 -*-
"""基石标准库：统计。一组数据的集中趋势与离散程度。

**为什么需要**：数据分析是核心场景，而 `表格.汇总` 只有平均/最大/最小——
算中位数、标准差、众数现在都得手写（`成绩分析` 项目里就是手写的）。

约定与其它标准库一致：函数名中文、报错中文、**空数据给明确报错**（不返回 空，
否则调用方会在后面某处莫名其妙地崩）。
"""

from __future__ import annotations

import math as _py_math

from ..errors import RunTypeError, RunValueError

__all__ = [
    "求和", "平均", "中位数", "众数", "方差", "标准差", "极差",
    "分位数", "去极值平均", "加权平均",
]


def _nums(数据, 名字: str) -> list:
    """把输入统一成数字列表（列表/元组/范围都收）。"""
    if isinstance(数据, (str, bytes)):
        raise RunTypeError(f"「{名字}」要传一组数字，不能传文本")
    try:
        seq = list(数据)
    except TypeError:
        raise RunTypeError(f"「{名字}」要传一组数字（列表）")
    out = []
    for i, v in enumerate(seq):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise RunTypeError(
                f"「{名字}」里第 {i + 1} 个不是数字：{v!r}")
        out.append(v)
    return out


def _need(数据: list, 名字: str) -> list:
    if not 数据:
        raise RunValueError(f"「{名字}」要至少有一个数字，现在是空的")
    return 数据


def 求和(数据) -> object:
    """返回所有数字的和（空列表返回 0）。"""
    return sum(_nums(数据, "求和"))


def 平均(数据) -> float:
    """返回算术平均数。"""
    xs = _need(_nums(数据, "平均"), "平均")
    return sum(xs) / len(xs)


def 中位数(数据) -> float:
    """返回中位数（偶数个时取中间两个的平均）。

    中位数比平均数**抗极端值**：`[1, 2, 3, 4, 1000]` 的平均数是 202，
    中位数仍是 3——看「大多数人是什么水平」时用中位数。
    """
    xs = sorted(_need(_nums(数据, "中位数"), "中位数"))
    n = len(xs)
    mid = n // 2
    if n % 2:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2


def 众数(数据):
    """返回出现次数最多的值。

    并列最多时返回**最先出现**的那个（保证结果可复现，不随字典顺序漂）。
    """
    xs = _need(_nums(数据, "众数"), "众数")
    counts: dict = {}
    order: list = []
    for v in xs:
        if v not in counts:
            counts[v] = 0
            order.append(v)
        counts[v] += 1
    best, best_n = order[0], counts[order[0]]
    for v in order:
        if counts[v] > best_n:
            best, best_n = v, counts[v]
    return best


def 方差(数据, 样本: bool = False) -> float:
    """返回方差（默认**总体方差**；`样本=真` 时用样本方差，除以 n-1）。"""
    xs = _need(_nums(数据, "方差"), "方差")
    n = len(xs)
    if 样本:
        if n < 2:
            raise RunValueError("样本方差至少要 2 个数（要除以 n-1）")
        m = sum(xs) / n
        return sum((x - m) ** 2 for x in xs) / (n - 1)
    m = sum(xs) / n
    return sum((x - m) ** 2 for x in xs) / n


def 标准差(数据, 样本: bool = False) -> float:
    """返回标准差（方差的开方）。`样本=真` 时用样本标准差。"""
    return _py_math.sqrt(方差(数据, 样本))


def 极差(数据) -> object:
    """返回最大值与最小值的差。"""
    xs = _need(_nums(数据, "极差"), "极差")
    return max(xs) - min(xs)


def 分位数(数据, p: float) -> float:
    """返回第 p 分位数（p 取 0–1；0.5 就是中位数，0.25 是下四分位）。

    用**线性插值**（与多数统计软件一致）：`[1, 2, 3, 4]` 的 0.25 分位是 1.75。
    """
    if not isinstance(p, (int, float)) or isinstance(p, bool):
        raise RunTypeError("「分位数」的第二个参数要传 0 到 1 之间的小数")
    if not 0 <= p <= 1:
        raise RunValueError(f"分位数要在 0 到 1 之间，现在是 {p}")
    xs = sorted(_need(_nums(数据, "分位数"), "分位数"))
    if len(xs) == 1:
        return xs[0]
    pos = p * (len(xs) - 1)
    lo = int(_py_math.floor(pos))
    hi = int(_py_math.ceil(pos))
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def 去极值平均(数据, 去掉个数: int = 1) -> float:
    """去掉最大和最小的各「去掉个数」个之后求平均（比赛评分常用）。

    例：`[9.5, 8.0, 9.0, 7.5, 9.9]` 去掉一个最高一个最低后是
    `[9.5, 8.0, 9.0]` 的平均 8.833…。
    """
    if isinstance(去掉个数, bool) or not isinstance(去掉个数, int):
        raise RunTypeError("「去掉个数」要传整数")
    if 去掉个数 < 0:
        raise RunValueError("「去掉个数」不能是负数")
    xs = sorted(_need(_nums(数据, "去极值平均"), "去极值平均"))
    if 去掉个数 * 2 >= len(xs):
        raise RunValueError(
            f"去掉 {去掉个数} 个最高和最低之后就没数据了"
            f"（一共 {len(xs)} 个）")
    kept = xs[去掉个数:len(xs) - 去掉个数]
    return sum(kept) / len(kept)


def 加权平均(数据, 权重) -> float:
    """返回加权平均数：每个数乘自己的权重再求和，除以权重之和。

    `加权平均([80, 90], [1, 3])` = (80×1 + 90×3) / 4 = 87.5。
    """
    xs = _need(_nums(数据, "加权平均"), "加权平均")
    ws = _nums(权重, "加权平均的权重")
    if len(xs) != len(ws):
        raise RunValueError(
            f"数据和权重的个数不一样：{len(xs)} 个数据、{len(ws)} 个权重")
    total = sum(ws)
    if total == 0:
        raise RunValueError("权重之和是 0，没法算加权平均")
    return sum(x * w for x, w in zip(xs, ws)) / total
