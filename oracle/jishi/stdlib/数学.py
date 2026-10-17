# -*- coding: utf-8 -*-
"""基石标准库：数学。薄封装 Python 的 math。"""

from __future__ import annotations

import math as _py_math

from ..errors import RunTypeError, RunValueError

__all__ = [
    "开方", "平方", "幂", "绝对值", "向上取整", "向下取整", "四舍五入",
    "圆周率", "自然常数", "正弦", "余弦", "正切", "角度转弧度",
    "对数", "常用对数", "最大公约数", "最小公倍数", "阶乘",
    "限制", "符号",
]


def 开方(x: float) -> float:
    """返回 x 的平方根。"""
    try:
        return _py_math.sqrt(x)
    except (TypeError, ValueError):
        raise RunTypeError(f"「{x}」不能开平方（负数没有实数平方根）")


def 平方(x: float) -> float:
    """返回 x 的平方（x * x）。"""
    return x * x


def 幂(底数: float, 指数: float) -> float:
    """返回 底数 的 指数 次方（整数次方结果保持整数）。"""
    try:
        return 底数 ** 指数
    except TypeError:
        return _py_math.pow(底数, 指数)


def 绝对值(x: float) -> float:
    """返回 x 的绝对值。"""
    return abs(x)


def 向上取整(x: float) -> int:
    """返回大于等于 x 的最小整数。"""
    return _py_math.ceil(x)


def 向下取整(x: float) -> int:
    """返回小于等于 x 的最大整数。"""
    return _py_math.floor(x)


def 四舍五入(x: float, 位数: int = 0) -> float:
    """把 x 四舍五入到指定小数位数。"""
    try:
        result = round(x, int(位数))
        if 位数 <= 0:
            return round(result)
        return result
    except (TypeError, ValueError):
        raise RunTypeError(f"「{x}」不能四舍五入")


# 常量
圆周率 = _py_math.pi
自然常数 = _py_math.e


def 正弦(角度_弧度: float) -> float:
    """返回弧度角的正弦值。"""
    return _py_math.sin(角度_弧度)


def 余弦(角度_弧度: float) -> float:
    """返回弧度角的余弦值。"""
    return _py_math.cos(角度_弧度)


def 正切(角度_弧度: float) -> float:
    """返回弧度角的正切值。"""
    return _py_math.tan(角度_弧度)


def 角度转弧度(角度: float) -> float:
    """把角度（0-360）换算成弧度。"""
    return _py_math.radians(角度)


def 对数(x: float, 底: float = _py_math.e) -> float:
    """返回以「底」为底的对数，默认自然对数。"""
    try:
        return _py_math.log(x, 底)
    except (TypeError, ValueError, ZeroDivisionError):
        raise RunTypeError(f"「{x}」不能求以 {底} 为底的对数")


def 常用对数(x: float) -> float:
    """返回以 10 为底的对数。"""
    try:
        return _py_math.log10(x)
    except (TypeError, ValueError):
        raise RunTypeError(f"「{x}」不能求常用对数")


def 最大公约数(a: int, b: int) -> int:
    """返回两个整数的最大公约数。"""
    return _py_math.gcd(int(a), int(b))


def 最小公倍数(a: int, b: int) -> int:
    """返回两个整数的最小公倍数。"""
    return _py_math.lcm(int(a), int(b))


def 阶乘(n: int) -> int:
    """返回 n 的阶乘（1×2×…×n）。"""
    try:
        return _py_math.factorial(int(n))
    except (TypeError, ValueError):
        raise RunTypeError(f"「{n}」不能求阶乘（需要非负整数）")


def 限制(值, 底, 顶):
    """把 值 卡在 [底, 顶] 区间内（小了给底，大了给顶）。

    **不改变类型**：整数还是整数、小数还是小数（返回的是三个参数之一）。
    底大于顶时报错 —— 那种区间没有意义，静默给个值只会让人更难查。
    """
    try:
        底大 = 底 > 顶
    except TypeError:
        raise RunTypeError(f"「限制」的三个参数都要是数字（现在是「{底}」「{顶}」）")
    if 底大:
        raise RunValueError(f"「限制」的底（{底}）不能大于顶（{顶}）")
    try:
        if 值 < 底:
            return 底
        if 值 > 顶:
            return 顶
    except TypeError:
        raise RunTypeError(f"「{值}」不能和「{底}」比大小（都要是数字）")
    return 值


def 符号(值):
    """返回 -1、0 或 1（值分别是负数、零、正数）。

    返回的**永远是整数**（不是小数），方便直接拿去当方向/系数用。
    """
    try:
        if 值 > 0:
            return 1
        if 值 < 0:
            return -1
    except TypeError:
        raise RunTypeError(f"「{值}」不能取符号（需要数字）")
    return 0
