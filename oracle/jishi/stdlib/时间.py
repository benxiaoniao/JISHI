# -*- coding: utf-8 -*-
"""基石标准库：时间。薄封装 Python 的 time / datetime。"""

from __future__ import annotations

import time as _py_time
from datetime import datetime as _py_datetime, timedelta as _py_timedelta

from ..errors import RunTypeError, RunValueError

__all__ = [
    "现在",     "今天",     "此刻",     "时间戳",     "格式化时间戳",     "睡眠",     "高精度时间",     "星期名",     "加天数",     "计时",
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


# -- M50 补缺 ---------------------------------------------------------------

class _计时器:
    """`时间.计时(...)` 的返回值：`用 时间.计时("建索引") 为 t：`。

    离开块时**自动打印**耗时（不打印的话「计时」就没意义了）；
    块里也能自己读 `t.耗时()` 拿秒数，或 `t.停()` 提前停表。
    """

    __slots__ = ("_名字", "_起", "_止", "_已打印")
    _jishi_type_name = "计时器"

    def __init__(self, 名字: str):
        self._名字 = 名字
        self._起 = _py_time.perf_counter()
        self._止 = None
        self._已打印 = False

    def 进入(self):
        """（上下文协议）返回自己，于是 `为 t` 里的 `t` 就是这个计时器。"""
        return self

    def 耗时(self) -> float:
        """返回从开始到现在经过的秒数（可多次读，不停表）。"""
        end = self._止 if self._止 is not None else _py_time.perf_counter()
        return end - self._起

    def 停(self) -> float:
        """停表并返回秒数（之后 `耗时()` 固定返回这个值）。"""
        if self._止 is None:
            self._止 = _py_time.perf_counter()
        return self._止 - self._起

    def 退出(self) -> None:
        """（上下文协议）离开块时停表；**给了名字才打印**（见 `计时` 的说明）。"""
        秒 = self.停()
        if self._名字 and not self._已打印:
            self._已打印 = True
            print(f"{self._名字} 耗时 {秒:.3f} 秒")


def 计时(名字: str = ""):
    """计时器：`用 时间.计时("建索引") 为 t：`。

    **给了名字就打印**一行耗时（`建索引 耗时 0.012 秒`）；**不给名字就安静计时**，
    自己用 `t.耗时()` 取秒数——两种用法各有场合：

    - 想「看一眼这段花多久」→ 给个名字，省得自己拼打印；
    - 想把耗时**算进结果里**（比如跑分表、性能报告）→ 别给名字，
      否则计时输出会和你的表格混在一起（`排序跑分` 项目就踩过这个）。

    之前只能手动取两次 `高精度时间()` 相减，容易漏（忘了取结束时间、
    或忘了换算单位）。块里随时可 `t.耗时()` 看当前进度，`t.停()` 提前停表。
    """
    if not isinstance(名字, str):
        raise RunTypeError("「计时」的名字要传文本")
    return _计时器(名字)
