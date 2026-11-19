# -*- coding: utf-8 -*-
"""基石标准库：物理（M208）—— 重力 / 摩擦 / 台阶 / 瓦片碰撞。

## 它做什么（也只做这些）

平台游戏里 90% 的「物理」就是：**往下掉、撞墙停、站得住、能上一级台阶**。
这里只实现这四件，别的（刚体旋转、连续碰撞、关节）不做 —— 那是一个物理引擎，
不是标准库该有的东西。

```jishi
令 体 = 物理.新建({"x": 32, "y": 0, "宽": 12, "高": 12, "重力": 1600, "摩擦": 8})
遍历 帧 在 图形.运行(初始化, 每帧)：
    如果 操控.按下("跳")：物理.跳(体)
    物理.步(体, 瓦片图, dt, {"实心": [1, 2], "台阶": 8})
```

## 碰撞口径（三宿主同一份）

- **轴分离**：先水平、再垂直；每轴再按「半个瓦片」分子步 ——
  一帧 100ms、速度 900 就是 90px，不分步会**整块穿过去**（看起来像随机消失）。
- **图外当空**（不挡）：掉出地图是常见结果（掉下去就死）；想围栏就自己画墙。
- **`实心`**：给空 ⇒ **非 -1 的瓦片全实心**；给列表 ⇒ 只有列表里的号实心。
- **台阶**：水平被挡住时，若抬 `台阶` 像素后能过，就抬上去再走。

## 边界

纯逻辑（不碰画布），所以命令行 / Node / 网页三边都能跑，判据能对拍。
"""

import math

from ..errors import RunTypeError, RunValueError

__all__ = ["新建", "步", "跳"]


def _字典(v, 名, 参="选项"):
    if not isinstance(v, dict):
        raise RunTypeError(f"「{名}」的「{参}」要字典，给的是{type(v).__name__}")
    return v


def _数(v, 名, 参):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise RunTypeError(f"「{名}」的「{参}」要数字，给的是{type(v).__name__}")
    return float(v)


def _必数(d, 键, 谁):
    if 键 not in d:
        raise RunValueError(f"「{谁}」的字典里缺「{键}」")
    return _数(d[键], 谁, 键)


def _行们(瓦片图):
    raw = 瓦片图.get("行们")
    if raw is None:
        return []
    出 = []
    for r in raw:
        行 = []
        for c in r:
            行.append(-1 if c is None else int(c))
        出.append(行)
    return 出


def _实心(选, 名):
    出 = []
    if 选 is None:
        return 出
    if isinstance(选, (list, tuple)):
        return [int(x) for x in 选]
    d = _字典(选, 名)
    v = d.get("实心")
    if v is None:
        return 出
    return [int(x) for x in v]


def _实心吗(号, 实心):
    return 号 >= 0 and (not 实心 or 号 in 实心)


class _地形:
    def __init__(self, 行们, 瓦片宽, 瓦片高, 实心):
        self.行们 = 行们
        self.宽 = 瓦片宽
        self.高 = 瓦片高
        self.实心 = 实心

    def 撞(self, x, y, bw, bh):
        if bw <= 0 or bh <= 0 or self.宽 <= 0 or self.高 <= 0:
            return False
        # 缩一点点：「刚好贴着」不算撞（否则贴墙走会被自己卡住）
        缩 = 0.001
        c0 = int((x + 缩) // self.宽)
        c1 = int((x + bw - 缩) // self.宽)
        r0 = int((y + 缩) // self.高)
        r1 = int((y + bh - 缩) // self.高)
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                if r < 0 or c < 0:
                    continue          # 图外（上 / 左）当空
                if r >= len(self.行们):
                    continue
                行 = self.行们[r]
                号 = 行[c] if 0 <= c < len(行) else -1
                if _实心吗(号, self.实心):
                    return True
        return False

    def 通(self, x, y, bw, bh):
        return not self.撞(x, y, bw, bh)


def 新建(选项):
    """M208：造一个「体」`{x,y,宽,高,速x,速y,在地,重力,摩擦,最大下落}`。"""
    d = _字典(选项, "新建")
    x = _必数(d, "x", "物理.新建")
    y = _必数(d, "y", "物理.新建")
    w = _必数(d, "宽", "物理.新建")
    h = _必数(d, "高", "物理.新建")
    if w <= 0 or h <= 0:
        raise RunValueError(f"「物理.新建」的宽高要大于 0，现在是 {w} × {h}")
    重力 = _数(d["重力"], "新建", "重力") if "重力" in d else 1600.0
    摩擦 = _数(d["摩擦"], "新建", "摩擦") if "摩擦" in d else 0.0
    最大下落 = _数(d["最大下落"], "新建", "最大下落") if "最大下落" in d else 900.0
    if 最大下落 <= 0:
        raise RunValueError("「新建」的最大下落要大于 0")
    return {
        "x": x, "y": y, "宽": w, "高": h,
        "速x": 0.0, "速y": 0.0, "在地": False,
        "重力": 重力, "摩擦": 摩擦, "最大下落": 最大下落,
    }


def _是体(v, 名):
    if not isinstance(v, dict) or "速x" not in v or "在地" not in v:
        raise RunValueError(f"这个字典不是体 —— `物理.新建()` 的返回值才认（「{名}」）")
    return v


def 步(体, 瓦片图, 秒, 选项=None):
    """M208：走一步（`秒` 通常就是帧的 `dt`）。**原地改 `体`，也返回它。**"""
    b = _是体(体, "步")
    图 = _字典(瓦片图, "步", "瓦片图")
    dt = _数(秒, "步", "秒")
    if dt < 0:
        raise RunValueError(f"「步」的秒数不能是负数，现在是 {dt}")
    if dt == 0:
        return 体
    if "瓦片宽" not in 图:
        raise RunValueError("「步」的字典里缺「瓦片宽」")
    if "瓦片高" not in 图:
        raise RunValueError("「步」的字典里缺「瓦片高」")
    瓦片宽 = _数(图["瓦片宽"], "步", "瓦片宽")
    瓦片高 = _数(图["瓦片高"], "步", "瓦片高")
    if 瓦片宽 <= 0 or 瓦片高 <= 0:
        raise RunValueError("「步」的瓦片宽高必须大于 0")
    地 = _地形(_行们(图), 瓦片宽, 瓦片高, _实心(选项, "步"))
    台阶 = 0.0
    if isinstance(选项, dict) and "台阶" in 选项:
        台阶 = _数(选项["台阶"], "步", "台阶")
        if 台阶 < 0:
            raise RunValueError(f"「步」的台阶不能是负数，现在是 {台阶}")

    w, h = b["宽"], b["高"]
    if w <= 0 or h <= 0:
        raise RunValueError("「步」的体宽高要大于 0（用 `物理.新建` 造体）")
    重力 = b["重力"]
    摩擦 = b["摩擦"]
    最大下落 = max(b["最大下落"], 1.0)
    x, y = b["x"], b["y"]
    速x = b["速x"]
    速y = min(b["速y"] + 重力 * dt, 最大下落)

    # 子步长不超过「半个瓦片」（理由见文件头）
    步长 = max(min(地.宽, 地.高) * 0.5, 0.5)

    # ---- 水平 ----
    dx = 速x * dt
    段 = max(int(math.ceil(abs(dx) / 步长)), 1)
    for _ in range(段):
        一 = dx / 段
        if 地.通(x + 一, y, w, h):
            x += 一
        elif (台阶 > 0 and 地.通(x, y - 台阶, w, h)
              and 地.通(x + 一, y - 台阶, w, h)):
            y -= 台阶
            x += 一
            b["在地"] = False
        else:
            速x = 0.0
            break

    # ---- 垂直 ----
    dy = 速y * dt
    段 = max(int(math.ceil(abs(dy) / 步长)), 1)
    落地 = False
    for _ in range(段):
        一 = dy / 段
        if 地.通(x, y + 一, w, h):
            y += 一
        else:
            if 一 > 0:
                落地 = True
            速y = 0.0
            break

    # ---- 摩擦（只在地面上）----
    if 摩擦 > 0 and 落地:
        速x *= max(1.0 - 摩擦 * dt, 0.0)
        if abs(速x) < 1.0:
            速x = 0.0

    在地 = 速y >= 0 and 地.撞(x, y + h, w, 0.5)
    b["x"] = x
    b["y"] = y
    b["速x"] = 速x
    b["速y"] = 速y
    b["在地"] = bool(在地 or 落地)
    return 体


def 跳(体, 速度=520):
    """M208：在地面上就跳（返回 `真`）；空中返回 `假`（不报错）。"""
    b = _是体(体, "跳")
    力 = 520.0 if 速度 is None else _数(速度, "跳", "速度")
    if 力 <= 0:
        raise RunValueError(f"「跳」的速度要大于 0，现在是 {力}")
    if not b["在地"]:
        return False
    b["速y"] = -力
    b["在地"] = False
    return True
