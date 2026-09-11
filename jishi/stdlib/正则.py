# -*- coding: utf-8 -*-
"""基石标准库：正则。薄封装 Python 的 re，中文友好 API。"""

from __future__ import annotations

import re as _py_re

from ..errors import RunValueError

__all__ = ["匹配", "搜索", "查找全部", "替换", "拆分", "分组"]


def _compile(模式):
    try:
        return _py_re.compile(模式)
    except _py_re.error as e:
        raise RunValueError(f"正则表达式写错了：{e}")


def 匹配(模式, 文本) -> bool:
    """从头匹配：文本开头是否符合模式，返回布尔。"""
    return _compile(模式).match(文本) is not None


def 搜索(模式, 文本) -> str:
    """在文本中搜索第一个符合模式的内容，返回匹配文本（找不到返回空文本）。"""
    m = _compile(模式).search(文本)
    return m.group(0) if m else ""


def 查找全部(模式, 文本) -> list:
    """找出文本中所有符合模式的内容，返回匹配文本列表。"""
    return _compile(模式).findall(文本)


def 替换(模式, 替换为, 文本) -> str:
    """把文本中所有符合模式的部分替换成新内容。"""
    return _compile(模式).sub(替换为, 文本)


def 拆分(模式, 文本) -> list:
    """按模式把文本拆分成列表。"""
    return _compile(模式).split(文本)


def 分组(模式, 文本) -> list:
    """搜索并返回所有分组（含第 0 组完整匹配）。找不到返回空列表。"""
    m = _compile(模式).search(文本)
    return list(m.groups()) if m else []
