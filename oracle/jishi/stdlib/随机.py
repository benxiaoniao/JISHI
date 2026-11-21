# -*- coding: utf-8 -*-
"""基石标准库：随机。薄封装 Python 的 random。"""

from __future__ import annotations

import random as _py_random

from ..errors import RunTypeError, RunValueError

__all__ = ["随机整数", "随机小数", "随机选择", "洗牌", "随机文本", "按权重"]


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


#: `随机文本` 能给的几种字符集。**故意用中文短名**（核心一：别让人背枚举值）。
_字符集 = {
    "字母数字": "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "字母": "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "小写字母": "abcdefghijklmnopqrstuvwxyz",
    "大写字母": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "数字": "0123456789",
}


def 随机文本(长度: int, 字符集: str = "字母数字") -> str:
    """生成一个随机字符串（默认「字母数字」，也可给「数字」做验证码）。

    可选的种类：`字母数字`（默认）/ `字母` / `小写字母` / `大写字母` / `数字`。
    ⚠️ 这是**通用随机**，不是密码学强度的口令 —— 要口令请用 `加密.安全随机字节`。
    """
    名称 = "字母数字" if 字符集 is None else str(字符集)
    if 名称 not in _字符集:
        raise RunValueError(
            f"不认识的字符集「{名称}」（可用：{' / '.join(_字符集)}）")
    n = int(长度)
    if n < 0:
        raise RunValueError(f"长度不能是负数（现在是 {n}）")
    池 = _字符集[名称]
    return "".join(_py_random.choice(池) for _ in range(n))


def 按权重(选项们, 权重们):
    """按权重随机挑一个元素（权重越大概率越高）。

    权重个数必须与选项一样多、都是非负数，且**至少要有一个大于 0**
    （全 0 等于「没有可选项」，那不该静默返回什么）。
    """
    项 = list(选项们)
    权 = [float(w) for w in 权重们]
    if not 项:
        raise RunValueError("不能从空序列里按权重挑元素")
    if len(项) != len(权):
        raise RunValueError(
            f"选项有 {len(项)} 个，权重有 {len(权)} 个 —— 两者要一样多")
    if any(w < 0 for w in 权):
        raise RunValueError("权重不能是负数")
    总 = sum(权)
    if 总 <= 0:
        raise RunValueError("权重加起来要大于 0（全 0 等于没有可选项）")
    落 = _py_random.random() * 总
    累计 = 0.0
    for 号, w in enumerate(权):
        累计 += w
        if 落 < 累计:
            return 项[号]
    return 项[-1]          # 浮点误差兜底：落点正好压在末尾
