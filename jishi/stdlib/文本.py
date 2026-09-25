# -*- coding: utf-8 -*-
"""基石标准库：文本。薄封装 str 的批量与格式化能力。

说明：基础操作（大写/小写/去空白/拆分/替换…）是字符串自带方法，
      本模块补充的是"方法不擅长"的批量与格式化能力。
"""

from __future__ import annotations

from ..errors import RunTypeError
from ..runtime import jishi_repr

__all__ = [
    "拼接",     "居中",     "左对齐",     "右对齐",     "补零",     "重复",     "计数",     "是数字",     "是字母",     "是空白",     "是大写",     "是小写",     "首字母大写",     "去前缀",     "去后缀",     "格式化",     "切成三段",     "按行拆分",     "截断",     "填充",
]


def 拼接(序列, 分隔符: str = "") -> str:
    """把列表里的文本用分隔符连成一段。

    用 `jishi_repr` 而不是 Python 的 `str()`：后者会把 真/空 显示成
    `True`/`None`，与 `文本()`、`打印()` 的中文显示不一致（M30）。
    """
    return 分隔符.join(jishi_repr(x, top=True) for x in 序列)


def 居中(文本: str, 宽度: int, 填充: str = " ") -> str:
    """文本居中，两侧用填充字符补足到指定宽度。"""
    return str(文本).center(int(宽度), 填充)


def 左对齐(文本: str, 宽度: int, 填充: str = " ") -> str:
    """文本靠左，右侧补字符到指定宽度（适合做表格对齐）。"""
    return str(文本).ljust(int(宽度), 填充)


def 右对齐(文本: str, 宽度: int, 填充: str = " ") -> str:
    """文本靠右，左侧补字符到指定宽度（适合数字对齐）。"""
    return str(文本).rjust(int(宽度), 填充)


def 补零(文本, 宽度: int) -> str:
    """在左侧补 0 到指定宽度，比如 补零(7, 3) → "007"。"""
    return str(文本).zfill(int(宽度))


def 重复(文本: str, 次数: int) -> str:
    """把文本重复若干次。"""
    return str(文本) * int(次数)


def 计数(文本: str, 子串: str) -> int:
    """子串在文本中出现的次数。"""
    return str(文本).count(str(子串))


def 是数字(文本: str) -> bool:
    """是否全是数字字符。"""
    return str(文本).isdigit()


def 是字母(文本: str) -> bool:
    """是否全是字母（中文也算字母）。"""
    return str(文本).isalpha()


def 是空白(文本: str) -> bool:
    """是否全是空白字符。"""
    return str(文本).isspace()


def 是大写(文本: str) -> bool:
    """字母是否全是大写。"""
    return str(文本).isupper()


def 是小写(文本: str) -> bool:
    """字母是否全是小写。"""
    return str(文本).islower()


def 首字母大写(文本: str) -> str:
    """首字母大写，其余小写。"""
    return str(文本).capitalize()


def 去前缀(文本: str, 前缀: str) -> str:
    """如果文本以前缀开头就去掉它，否则原样返回。"""
    return str(文本).removeprefix(str(前缀))


def 去后缀(文本: str, 后缀: str) -> str:
    """如果文本以后缀结尾就去掉它，否则原样返回。"""
    return str(文本).removesuffix(str(后缀))


def 格式化(模板: str, *参数) -> str:
    """把 {} 占位符依次替换成参数：格式化("{}今年{}岁", "小明", 12)。

    支持常见的格式说明符（`{:.1f}`、`{:>5}`…）。布尔与空先转成中文文本，
    免得 `格式化("{}", 真)` 给出 `True`（与 `文本()` 的显示不一致）；
    数字保持原样，好让 `{:.1f}` 这类说明符仍然生效。
    """
    转好的 = [jishi_repr(a, top=True) if (a is None or isinstance(a, bool))
              else a for a in 参数]
    return str(模板).format(*转好的)


def 切成三段(文本: str, 分隔符: str) -> list:
    """按第一个分隔符切成三段：[前, 分隔符, 后]。"""
    return list(str(文本).partition(str(分隔符)))


def 按行拆分(文本: str) -> list:
    """按换行拆成行的列表。"""
    return str(文本).splitlines()


# -- M50 补缺 ---------------------------------------------------------------

def 截断(文本, 宽度: int, 省略号: str = "…") -> str:
    """超长就截断并加省略号，否则原样返回（`宽度` 含省略号本身）。

    例：`文本.截断("这是一段很长的话", 6)` → `"这是一段很…"`（6 个字符位）。
    做日志摘要、表格列宽时用；`宽度` 小于省略号长度就只返回省略号。
    """
    s = str(文本)
    w = int(宽度)
    if w < 0:
        raise RunTypeError(f"「截断」的宽度不能是负数，得到了 {宽度}")
    if len(s) <= w:
        return s
    if w <= len(省略号):
        return 省略号[:w]
    return s[:w - len(省略号)] + 省略号


def 填充(文本, 宽度: int, 填充字符: str = " ") -> str:
    """把文本**居中**填充到指定宽度（两侧补同样的字符）。

    与 `左对齐` / `右对齐` 是一组：那三个是「靠一边」，这个是「居中」。
    例：`文本.填充("abc", 7, "-")` → `"--abc--"`。
    """
    if not isinstance(填充字符, str) or len(填充字符) != 1:
        raise RunTypeError("「填充」的填充字符要传一个字符")
    return str(文本).center(int(宽度), 填充字符)
