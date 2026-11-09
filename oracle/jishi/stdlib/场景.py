# -*- coding: utf-8 -*-
"""基石标准库：场景（M187）—— 标题页 → 游戏中 → 结束页 的开关。

只做**状态机**，不做转场动画（那要时间轴，属于表现层）。

语义（三宿主必须一致）：

- `定(名, {"初始化"?, "每帧"?})` 登记一个场景；
- `切(名)`：第一次切进某场景时**跑一次**它的 `初始化`；**已经在这个场景就什么都不做**
  （返回假）—— 否则在 `每帧` 里无条件 `切` 会反复跑初始化；
- `现在()` 返回当前场景名（还没切过 = 空文本）。
"""

from ..errors import RunTypeError, RunValueError

__all__ = ["定", "切", "现在"]

#: 场景表：名字 → {"初始化": 函数或 None, "每帧": 函数或 None}
_场景们 = {}
#: 当前场景（空 = 还没切过）
_现在 = ""


def 定(名, 选项):
    """M187：登记一个场景（`选项` 里 `初始化` / `每帧` 都是可选函数）。"""
    if not isinstance(选项, dict):
        raise RunTypeError(f"「场景.定」的选项要字典，给的是{type(选项).__name__}")
    for 键 in ("初始化", "每帧"):
        v = 选项.get(键)
        if v is not None and not callable(v):
            raise RunTypeError(f"「场景.定」的「{键}」要函数，给的是{type(v).__name__}")
    _场景们[str(名)] = {"初始化": 选项.get("初始化"), "每帧": 选项.get("每帧")}
    return True


def 切(名):
    """M187：切到某个场景（第一次进来会跑一次它的 `初始化`）。"""
    global _现在
    名 = str(名)
    if 名 not in _场景们:
        raise RunValueError(f"没有叫「{名}」的场景",
                            hint='先 `场景.定("名字", {"初始化": 某函数})` 再切')
    if _现在 == 名:
        return False
    _现在 = 名
    初 = _场景们[名]["初始化"]
    if 初 is not None:
        初()
    return True


def 现在():
    """M187：当前场景名（还没切过 = 空文本）。"""
    return _现在
