# -*- coding: utf-8 -*-
"""基石标准库：向量。一列数字上的逐元素运算与点积。

**为什么需要**（0.2.9 的主菜）：科学计算的入门动作就是「两组数据一起算」——
距离、夹角、加权总分、斜率。用列表手写要写循环，而且**长度不等时容易静默错位**。

## 三条刻意的设计（都写清楚，免得反复讨论）

1. 🔴 **向量就是普通列表，不是新类型**（用户 2026-10-26 拍板）。
   `[1, 2, 3]` 既是列表也是向量 —— 于是它跟着列表的一切能力（下标、切片、
   `遍历`、`长度`），也能直接 print 出来看。要不要「新类型」的取舍见
   `docs/设计决策.md`：D89 已定**不做运算符重载**，那值类型带来的唯一好处
   （`a + b` 这种语法糖）本来就用不上，却要为它动 `Val`/`JVal` 五处实现。

2. 🔴 **一律写全名字，不用 `+ - *`**：`向量.加(a, b)` 而不是 `a + b`。
   同一个 `+` 在列表上是拼接、在向量上是逐元素 —— 人和模型都会猜错（D89）。

3. **长度不等**是最常见的错，所以**必须报错**、并且把两个长度都打出来，
   绝不悄悄按短的那个算（那是「结果不对但程序不报错」，最难查）。

## 与 `统计` 的分工

`统计` 管**一组数**的集中趋势与离散程度（平均、标准差、中位数）；
`向量` 管**几列数之间**的运算（加、点积、夹角）。两者都接受普通列表，可以混着用。
"""

from __future__ import annotations

from ..errors import RunTypeError, RunValueError

__all__ = [
    "构造", "加", "减", "标量乘", "点积", "和", "均值", "最大", "最小",
    "维数", "切片",
]


def _nums(数据, 名字: str) -> list:
    """把输入统一成数字列表；不是数字就给中文错（与 `统计._nums` 同口径）。"""
    if isinstance(数据, (str, bytes)):
        raise RunTypeError(f"「{名字}」要传一列数字，不能传文本")
    try:
        seq = list(数据)
    except TypeError:
        raise RunTypeError(f"「{名字}」要传一列数字（列表）")
    out = []
    for i, v in enumerate(seq):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise RunTypeError(
                f"「{名字}」里第 {i + 1} 个不是数字：{v}")
        out.append(v)
    return out


def _同长(a: list, b: list, 名字: str) -> None:
    if len(a) != len(b):
        raise RunValueError(
            f"「{名字}」要求两列数字**长度相同**，实际是 {len(a)} 和 {len(b)}"
            f"（长度不等时逐元素算会错位，所以这里直接报错）")


def _非空(xs: list, 名字: str) -> list:
    if not xs:
        raise RunValueError(f"「{名字}」要至少有一个数，现在是空的")
    return xs


def 构造(个数, 初值=0) -> list:
    """造一列 `个数` 个数的向量，每个都是 `初值`（默认 0）。

    `向量.构造(3)` → `[0, 0, 0]`；`向量.构造(2, 1.5)` → `[1.5, 1.5]`。
    """
    if isinstance(个数, bool) or not isinstance(个数, int):
        raise RunTypeError(f"「构造」的个数要是整数，给的是 {个数}")
    if 个数 < 0:
        raise RunValueError(f"「构造」的个数不能是负数（给的是 {个数}）")
    if isinstance(初值, bool) or not isinstance(初值, (int, float)):
        raise RunTypeError(f"「构造」的初值要是数字，给的是 {初值}")
    return [初值] * 个数


def 加(甲, 乙) -> list:
    """逐元素相加：`向量.加([1, 2], [10, 20])` → `[11, 22]`。"""
    a = _nums(甲, "加")
    b = _nums(乙, "加")
    _同长(a, b, "加")
    return [x + y for x, y in zip(a, b)]


def 减(甲, 乙) -> list:
    """逐元素相减：`向量.减([10, 20], [1, 2])` → `[9, 18]`。"""
    a = _nums(甲, "减")
    b = _nums(乙, "减")
    _同长(a, b, "减")
    return [x - y for x, y in zip(a, b)]


def 标量乘(甲, 倍数) -> list:
    """每个元素都乘同一个数：`向量.标量乘([1, 2], 3)` → `[3, 6]`。"""
    a = _nums(甲, "标量乘")
    if isinstance(倍数, bool) or not isinstance(倍数, (int, float)):
        raise RunTypeError(f"「标量乘」的倍数要是数字，给的是 {倍数}")
    return [x * 倍数 for x in a]


def 点积(甲, 乙):
    """对应元素相乘再求和：`向量.点积([1, 2], [3, 4])` → `11`。

    几何意义：`点积 = |a| × |b| × cos(夹角)` —— 夹角就是从它反推出来的
    （`点积 / (长度 × 长度)` 就是余弦值）。
    """
    a = _nums(甲, "点积")
    b = _nums(乙, "点积")
    _同长(a, b, "点积")
    return sum(x * y for x, y in zip(a, b))


def 和(甲):
    """所有元素相加（空向量返回 0，与 `统计.求和` 一致）。"""
    return sum(_nums(甲, "和"))


def 均值(甲) -> float:
    """算术平均数（空向量报错 —— 零个数没有平均值）。"""
    a = _非空(_nums(甲, "均值"), "均值")
    return sum(a) / len(a)


def 最大(甲):
    """最大的那个元素（空向量报错）。"""
    return max(_非空(_nums(甲, "最大"), "最大"))


def 最小(甲):
    """最小的那个元素（空向量报错）。"""
    return min(_非空(_nums(甲, "最小"), "最小"))


def 维数(甲) -> int:
    """向量的维数（元素个数）。

    和内置 `长度()` 的区别：这条会**先校验元素都是数字**，
    所以 `向量.维数(["甲"])` 会报错、而 `长度(["甲"])` 是 1 ——
    想确认「这确实是一列数」时用它。

    ⚠️ **为什么不叫 `长度`**（2026-10-26 实测）：在基石里，模块里定义的函数名
    会**遮蔽同名内建** —— 一旦这个模块里有 `函数 长度(...)`，模块**自己的**
    `长度(列表)` 就会调到自己身上（实测返回 999 而不是 3）。所以只能换名。
    这条已记进 `--ai-card` 要提醒的坑里。
    """
    return len(_nums(甲, "维数"))


def 切片(甲, 起, 止=None) -> list:
    """取 `起` 到 `止`（**不含止**）的一段，`止` 不给就取到末尾。

    ⚠️ 与列表切片不同：**越界直接报错**，不悄悄截断 —— `[1, 2, 3]` 取
    `向量.切片(v, 1, 9)` 会告诉你「止 9 超出维数 3」，而不是给你 `[2, 3]`。
    （列表切片那种「静默截断」在科学计算里很容易掩盖「维度算错了」。）
    """
    a = _nums(甲, "切片")
    n = len(a)
    if isinstance(起, bool) or not isinstance(起, int):
        raise RunTypeError(f"「切片」的起要是整数，给的是 {起}")
    if 止 is None:
        止 = n
    if isinstance(止, bool) or not isinstance(止, int):
        raise RunTypeError(f"「切片」的止要是整数，给的是 {止}")
    if 起 < 0 or 止 < 0:
        raise RunValueError(f"「切片」的起和止都不能是负数（给的是 {起} 和 {止}）")
    if 起 > 止:
        raise RunValueError(f"「切片」的起 {起} 不能大于止 {止}")
    if 止 > n:
        raise RunValueError(f"「切片」的止 {止} 超出维数 {n}")
    return a[起:止]
