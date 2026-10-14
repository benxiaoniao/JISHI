# -*- coding: utf-8 -*-
"""基石标准库：日期。薄封装 Python 的 datetime，专注日期运算。

补齐「时间」模块的短板：日期格式化 / 加减 / 比较 / 解析。
基石里日期用「年-月-日」文本表示（如 2026-09-01）。
"""

from __future__ import annotations

import calendar as _calendar
import re as _re
from datetime import datetime as _py_datetime, timedelta as _py_timedelta

from ..errors import RunValueError

__all__ = [
    "今天", "解析", "格式化", "加天数", "减天数", "加月", "加年",
    "解析ISO", "相差天数", "早于", "晚于", "相等", "星期名",
    "今年", "本月", "本年天数",
]

_星期名 = ["一", "二", "三", "四", "五", "六", "日"]  # weekday() 0-6

#: ISO 8601 的开头：`年-月-日`，后面要么到头，要么是 `T` 或空格（跟着时间）。
_ISO头 = _re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ]|$)")


def 今天() -> str:
    """返回今天的日期，例如 2026-09-01。"""
    return _py_datetime.now().strftime("%Y-%m-%d")


def 解析(日期文本) -> _py_datetime:
    """把「年-月-日」文本解析成内部日期（供本模块函数使用）。"""
    s = str(日期文本).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return _py_datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise RunValueError(
        f"「{日期文本}」不是有效的日期，请写成 2026-09-01 这样的格式")


def 格式化(日期文本, 格式: str = "%Y-%m-%d") -> str:
    """把日期按指定格式输出，例如 格式化("2026-09-01", "%Y年%m月%d日")。

    注：Windows 上某些 Python 版本（如 3.10）的 strftime 使用 locale 编码，
    直接把含中文的格式串交给它会抛 UnicodeEncodeError；这里先把非 ASCII 段
    摘出来、strftime 之后再放回（M19 CI 修复）。
    """
    if all(ord(c) < 128 for c in 格式):
        return 解析(日期文本).strftime(格式)

    stashed: list[str] = []

    def _stash(m):
        stashed.append(m.group(0))
        return "\x01%d\x01" % (len(stashed) - 1)

    masked = _re.sub(r"[^\x00-\x7f]+", _stash, 格式)
    out = 解析(日期文本).strftime(masked)
    for i, seg in enumerate(stashed):
        out = out.replace("\x01%d\x01" % i, seg)
    return out


def 加天数(天数: int, 日期文本) -> str:
    """返回日期加上 N 天后的日期。"""
    return (解析(日期文本) + _py_timedelta(days=int(天数))).strftime("%Y-%m-%d")


def 减天数(天数: int, 日期文本) -> str:
    """返回日期减去 N 天后的日期。"""
    return (解析(日期文本) - _py_timedelta(days=int(天数))).strftime("%Y-%m-%d")


def _该月天数(年: int, 月: int) -> int:
    return _calendar.monthrange(年, 月)[1]


def 加月(月数: int, 日期文本) -> str:
    """返回日期加上 N 个月后的日期。

    ⚠️ **月末会「收」到那个月的最后一天**：`加月(1, "2026-01-31")` 给
    `2026-02-28`（不是 `2026-03-03`）—— 这也是**为什么要有 `加月`**：
    「每月一次」用 `加天数(30)` 会越错越远。`月数` 可以是负数。
    """
    d = 解析(日期文本)
    总月 = d.year * 12 + (d.month - 1) + int(月数)
    年, 月 = divmod(总月, 12)
    月 += 1
    return "%04d-%02d-%02d" % (年, 月, min(d.day, _该月天数(年, 月)))


def 加年(年数: int, 日期文本) -> str:
    """返回日期加上 N 年后的日期（`2月29日` 落在平年时会「收」到 `2月28日`）。"""
    return 加月(int(年数) * 12, 日期文本)


def 解析ISO(文本) -> str:
    """把 ISO 8601 文本解析成日期（`年-月-日`）。

    认得这些写法：`2026-10-12`、`2026-10-12T08:30:00`、
    `2026-10-12T08:30:00.123Z`、`2026-10-12T08:30:00+08:00`、
    `2026-10-12 08:30:00`。

    ⚠️ **只取「日子」这一段，不按后面的时区偏移换算** —— 「2026-10-12T23:00:00Z」
    在 UTC 上是 10-12、在东八区已经是 10-13，两种答案都对；这里定死「**按字面
    取日期**」这条小规则，免得同一份程序在不同执行器上给出不同的日子
    （时区换算要靠一整套时区数据库，不在标准库里做）。
    """
    m = _ISO头.match(str(文本).strip())
    if not m:
        raise RunValueError(
            f"「{文本}」不是有效的 ISO 日期，例如 2026-10-12 或 2026-10-12T08:30:00Z")
    年, 月, 日 = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= 月 <= 12 and 1 <= 日 <= _该月天数(年, 月)):
        raise RunValueError(f"「{文本}」里的日期不存在（{年} 年 {月} 月没有 {日} 日）")
    return "%04d-%02d-%02d" % (年, 月, 日)


def 相差天数(日期文本1, 日期文本2) -> int:
    """返回两个日期相差的天数（日期1 减 日期2，可为负）。"""
    return (解析(日期文本1) - 解析(日期文本2)).days


def 早于(日期文本1, 日期文本2) -> bool:
    """日期1 是否早于日期2。"""
    return 解析(日期文本1) < 解析(日期文本2)


def 晚于(日期文本1, 日期文本2) -> bool:
    """日期1 是否晚于日期2。"""
    return 解析(日期文本1) > 解析(日期文本2)


def 相等(日期文本1, 日期文本2) -> bool:
    """两个日期是否相同。"""
    return 解析(日期文本1) == 解析(日期文本2)


def 星期名(日期文本) -> str:
    """返回日期是星期几：一/二/三/四/五/六/日。"""
    return _星期名[解析(日期文本).weekday()]


def 今年() -> int:
    """返回当前年份。"""
    return _py_datetime.now().year


def 本月() -> int:
    """返回当前月份（1-12）。"""
    return _py_datetime.now().month


def 本年天数() -> int:
    """返回当前年份一共有多少天（闰年 366，平年 365）。"""
    y = _py_datetime.now().year
    return 366 if (y % 4 == 0 and y % 100 != 0) or y % 400 == 0 else 365
