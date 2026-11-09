# -*- coding: utf-8 -*-
"""基石标准库：瓦片（M187）—— 瓦片地图的「画 / 碰 / 取」。

地图数据是**二维整数列表**：`-1` 或 `空` = 这一格没有瓦片，其余是图集里的瓦片号
（按 `列数` 折行：号 0 是左上角那格）。

⚠️ 三份实现（Rust / Python / Node）的规则要逐字一致，对拍判据盯着。
"""

from ..errors import RunTypeError, RunValueError

__all__ = ["载入", "画", "碰", "取"]


def _字典(v, 名):
    if not isinstance(v, dict):
        raise RunTypeError(f"「{名}」的第一个参数要瓦片图（`瓦片.载入` 造的），给的是{type(v).__name__}")
    return v


def _数(v, 名, 参):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise RunTypeError(f"「{名}」的「{参}」要数字，给的是{type(v).__name__}")
    return float(v)


def _行们(行们, 名):
    if not isinstance(行们, (list, tuple)):
        raise RunTypeError(f"「{名}」的「行们」要二维列表，给的是{type(行们).__name__}")
    出 = []
    for i, r in enumerate(行们):
        if not isinstance(r, (list, tuple)):
            raise RunValueError(f"「{名}」的「行们」第 {i + 1} 行不是列表")
        行 = []
        for c in r:
            if c is None:
                行.append(-1)
            elif isinstance(c, bool) or not isinstance(c, int):
                raise RunValueError(f"「{名}」的「行们」第 {i + 1} 行里有非整数：{c!r}")
            else:
                行.append(int(c))
        出.append(行)
    return 出


def 载入(选项):
    """M187：`{"图集","瓦片宽","瓦片高","行们"}` → 瓦片图。"""
    for 键 in ("图集", "瓦片宽", "瓦片高"):
        if 键 not in 选项:
            raise RunValueError(f"「瓦片.载入」的选项里缺「{键}」")
    宽 = _数(选项["瓦片宽"], "载入", "瓦片宽")
    高 = _数(选项["瓦片高"], "载入", "瓦片高")
    if 宽 <= 0 or 高 <= 0:
        raise RunValueError("「瓦片.载入」的瓦片宽高必须大于 0")
    行们 = _行们(选项.get("行们", []), "载入")
    return {
        "图集": 选项["图集"], "瓦片宽": 宽, "瓦片高": 高, "行们": 行们,
        "列数": max((len(r) for r in 行们), default=0),
        "行数": len(行们),
    }


def 画(瓦片图, 相机=None):
    """M187：把地图画出来（给了相机就只画视口内那几格）。返回画了几块。"""
    from . import 图形 as _图形

    d = _字典(瓦片图, "画")
    宽, 高 = d["瓦片宽"], d["瓦片高"]
    列数 = max(1, d["列数"])
    c = 相机 or {}
    vx = float(c.get("x", 0))
    vy = float(c.get("y", 0))
    vw = float(c.get("宽", float("inf")))
    vh = float(c.get("高", float("inf")))
    画了 = 0
    for r, 行 in enumerate(d["行们"]):
        for col, 号 in enumerate(行):
            if 号 < 0:
                continue
            x = col * 宽
            y = r * 高
            if x + 宽 < vx or x > vx + vw or y + 高 < vy or y > vy + vh:
                continue
            _图形.画子图(d["图集"], (号 % 列数) * 宽, (号 // 列数) * 高, 宽, 高,
                        x, y, 宽, 高)
            画了 += 1
    return 画了


def 碰(瓦片图, 矩形):
    """M187：这个矩形压到了哪些瓦片？返回**去重升序**的瓦片号列表。"""
    d = _字典(瓦片图, "碰")
    宽, 高 = d["瓦片宽"], d["瓦片高"]
    for 键 in ("x", "y", "宽", "高"):
        if 键 not in 矩形:
            raise RunValueError(f"「瓦片.碰」的矩形里缺「{键}」")
    bx, by = float(矩形["x"]), float(矩形["y"])
    bw, bh = float(矩形["宽"]), float(矩形["高"])
    c0 = max(0, int(bx // 宽))
    c1 = max(0, int((bx + bw) // 宽))
    r0 = max(0, int(by // 高))
    r1 = max(0, int((by + bh) // 高))
    中 = []
    for r in range(r0, r1 + 1):
        for c in range(c0, c1 + 1):
            if r < len(d["行们"]) and c < len(d["行们"][r]):
                号 = d["行们"][r][c]
                if 号 >= 0 and 号 not in 中:
                    中.append(号)
    中.sort()
    return 中


def 取(瓦片图, 列, 行):
    """M187：读一格（越界 / 空 = `-1`）。"""
    d = _字典(瓦片图, "取")
    列, 行 = int(列), int(行)
    if 行 < 0 or 行 >= len(d["行们"]):
        return -1
    if 列 < 0 or 列 >= len(d["行们"][行]):
        return -1
    return d["行们"][行][列]
