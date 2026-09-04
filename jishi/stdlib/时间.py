# -*- coding: utf-8 -*-
"""基石标准库：时间。薄封装 Python 的 time / datetime。"""

from __future__ import annotations

import time as _py_time
from datetime import datetime as _py_datetime, timedelta as _py_timedelta

from ..errors import RunValueError

__all__ = [
    "现在", "今天", "此刻", "时间戳", "格式化时间戳", "睡眠",
    "高精度时间", "星期名", "加天数",
]

_星期名 = ["一", "二", "三", "四", "五", "六", "日"]  # 对应 weekday() 0-6


def 现在() -> str:
    """返回当前的日期和时间，例如 2026-09-01 13:40:00。"""
    return _py_datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def 今天() -> str:
    """返回今天的日期，例如 2026-09-01。"""
    return _py_datetime.now().strftime("%Y-%m-%d")


def 此刻() -> dict:
    """返回当前时间的字典：年/月/日/时/分/秒/星期。"""
    d = _py_datetime.now()
    return {
        "年": d.year, "月": d.month, "日": d.day,
        "时": d.hour, "分": d.minute, "秒": d.second,
        "星期": _星期名[d.weekday()],
    }


def 时间戳() -> float:
    """返回当前的时间戳（从 1970 年开始的秒数）。"""
    return _py_time.time()


def 格式化时间戳(时间戳: float) -> str:
    """把时间戳转成「年-月-日 时:分:秒」文本。"""
    return _py_datetime.fromtimestamp(时间戳).strftime("%Y-%m-%d %H:%M:%S")


def 睡眠(秒: float) -> None:
    """暂停指定的秒数。"""
    _py_time.sleep(max(0.0, float(秒)))


def 高精度时间() -> float:
    """返回高精度计时值（用于测量代码耗时）。

    用法：开始 = 时间.高精度时间()，干完活后 耗时 = 时间.高精度时间() - 开始
    """
    return _py_time.perf_counter()


def _parse_date(日期文本) -> _py_datetime:
    s = str(日期文本).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return _py_datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise RunValueError(f"「{日期文本}」不是有效的日期，请写成 2026-09-01 这样的格式")


def 星期名(日期文本) -> str:
    """返回日期是星期几：一/二/三/四/五/六/日。"""
    return _星期名[_parse_date(日期文本).weekday()]


def 加天数(天数: int, 日期文本) -> str:
    """返回日期加上 N 天后的日期文本。"""
    d = _parse_date(日期文本) + _py_timedelta(days=int(天数))
    return d.strftime("%Y-%m-%d")
