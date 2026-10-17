# -*- coding: utf-8 -*-
"""基石标准库：文本。薄封装 str 的批量与格式化能力。

说明：基础操作（大写/小写/去空白/拆分/替换…）是字符串自带方法，
      本模块补充的是"方法不擅长"的批量与格式化能力。
"""

from __future__ import annotations

from ..errors import RunTypeError
from ..runtime import jishi_repr

__all__ = [
    "拼接",     "居中",     "左对齐",     "右对齐",     "补零",     "重复",     "计数",     "是数字",     "是字母",     "是空白",     "是大写",     "是小写",     "首字母大写",     "去前缀",     "去后缀",     "格式化",     "切成三段",     "按行拆分",     "截断",     "填充",     "显示宽度",
]


def 拼接(序列, 分隔符: str = "") -> str:
    """把列表里的文本用分隔符连成一段。

    用 `jishi_repr` 而不是 Python 的 `str()`：后者会把 真/空 显示成
    `True`/`None`，与 `文本()`、`打印()` 的中文显示不一致（M30）。
    """
    return 分隔符.join(jishi_repr(x, top=True) for x in 序列)


#: 「宽字符」的码点区间 —— 在等宽终端里占 **2 列**。
#:
#: ⚠️ 这是 Unicode East Asian Width 的**实用子集**：覆盖日常会遇到的中日韩文字、
#: 全角标点与符号；不追全（`Ambiguous` 类按 1 列算 —— 那类在东亚也常按 1 列显示）。
#: **三处实现（Python / Rust / Node）必须逐条一致** —— 判据
#: `tests/test_m106_display_width.py` 会当场比对。
_宽区间: tuple[tuple[int, int], ...] = (
    (0x1100, 0x115F),    # 韩文字母
    (0x2E80, 0x303E),    # CJK 部首 + 康熙部首 + CJK 符号与标点
    (0x3041, 0x33FF),    # 平假名 / 片假名 / 注音符号 / CJK 兼容（㈠ ㊀ 一类）
    (0x3400, 0x4DBF),    # CJK 扩展 A
    (0x4E00, 0x9FFF),    # CJK 基本区
    (0xA000, 0xA4CF),    # 彝文
    (0xAC00, 0xD7A3),    # 韩文音节
    (0xF900, 0xFAFF),    # CJK 兼容表意
    (0xFE30, 0xFE6F),    # CJK 兼容形式
    (0xFF00, 0xFF60),    # 全角 ASCII 变体
    (0xFFE0, 0xFFE6),    # 全角符号（￥ ￦ 等）
    (0x1F300, 0x1F64F),  # emoji（多数终端按 2 列）
    (0x1F900, 0x1F9FF),  # emoji 补充区
    (0x20000, 0x2FFFD),  # CJK 扩展 B 及以后
    (0x30000, 0x3FFFD),
)

#: 「零宽」区间：组合符 / 零宽控制符 —— 不占列。
_零宽区间: tuple[tuple[int, int], ...] = (
    (0x0300, 0x036F),    # 组合变音符
    (0x0483, 0x0489),    # 组合西里尔符
    (0x200B, 0x200F),    # 零宽空格 / 方向标记
    (0xFE00, 0xFE0F),    # 变体选择符
)


def _字符宽度(字符: str) -> int:
    """一个码点占几列。"""
    码点 = ord(字符)
    for 低, 高 in _零宽区间:
        if 低 <= 码点 <= 高:
            return 0
    for 低, 高 in _宽区间:
        if 低 <= 码点 <= 高:
            return 2
    return 1


def 显示宽度(值) -> int:
    """这段文本在**等宽终端**里占几列 —— 一个汉字算 2，组合符算 0。

    对齐（`左对齐` / `右对齐` / `居中` / `填充`）就是按这个数补空格的。
    中文表格对不齐，根子从来不是「中文不好排」—— 是按**字符数**补，而汉字占 **2 列**。
    """
    return sum(_字符宽度(c) for c in str(值))


def _补齐(文本: str, 宽度: int, 填充: str, 位置: str) -> str:
    """按**显示宽度**把文本补到指定列数；`位置` 取 "左" / "右" / "中"。

    填充字符只允许**一个**（与 `ljust` 那里的报错保持一致）。
    补不满时**宁可差一列也不超**（用于全角填充字符的场合）。
    """
    if not isinstance(填充, str) or len(填充) != 1:
        raise RunTypeError(f"填充字符要传一个字符，收到 {填充!r}")
    差 = 宽度 - 显示宽度(文本)
    if 差 <= 0:
        return 文本
    单位 = _字符宽度(填充) or 1          # 万一是零宽字符，按 1 算，免得除零
    个数 = 差 // 单位
    if 个数 == 0:
        return 文本
    if 位置 == "左":
        return 填充 * 个数 + 文本
    if 位置 == "右":
        return 文本 + 填充 * 个数
    # ⚠️ 居中**不是**「左右各一半」：CPython `str.center` 的偏置是
    #    `left = marg // 2 + (marg & width & 1)`（`'ab'.center(5)` → `'  ab '`，**左多**）。
    #    自己写 `marg // 2` 会在「差是奇数且宽度是奇数」时左右颠倒。
    左侧 = 个数 // 2 + (个数 & 宽度 & 1)
    return 填充 * 左侧 + 文本 + 填充 * (个数 - 左侧)


def 居中(文本: str, 宽度: int, 填充: str = " ") -> str:
    """文本居中，两侧补到指定**显示宽度**（一个汉字算 2 列，见 `显示宽度`）。"""
    return _补齐(str(文本), int(宽度), 填充, "中")


def 左对齐(文本: str, 宽度: int, 填充: str = " ") -> str:
    """文本靠左，右侧补到指定**显示宽度**（适合做表格对齐）。"""
    return _补齐(str(文本), int(宽度), 填充, "右")


def 右对齐(文本: str, 宽度: int, 填充: str = " ") -> str:
    """文本靠右，左侧补到指定**显示宽度**（适合数字对齐）。"""
    return _补齐(str(文本), int(宽度), 填充, "左")


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
    """把文本**居中**填充到指定**显示宽度**（两侧补同样的字符）。

    与 `左对齐` / `右对齐` 是一组：那三个是「靠一边」，这个是「居中」。
    例：`文本.填充("abc", 7, "-")` → `"--abc--"`。
    """
    return _补齐(str(文本), int(宽度), 填充字符, "中")
