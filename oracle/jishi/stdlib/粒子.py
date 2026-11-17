# -*- coding: utf-8 -*-
"""基石标准库：粒子（M208）—— 爆炸 / 烟 / 星星，纯逻辑。

## 为什么是「纯逻辑」

粒子的**计算**（发射、重力、寿命）只是数，命令行也能跑 —— 所以它三宿主都实现，
判据才能对拍。只有 `画` 需要画布（它走 M207 的 `图形.批量`，一次通道画完）。

## 角度口径

**0 = 向右、90 = 向上**（数学口径）。屏幕 y 朝下，所以 `速y = -sin(角度)`。
想要「往上喷一锥」就写 `角度: [60, 120]`。

## 确定性

每个系统自带一条伪随机流（xorshift64*），种子在 `新建` 时定：
**同一种子 ⇒ 同一场爆炸**（判据要这个）。想要每局都不一样就把
`时间.高精度时间()` 当种子传进去。

⚠️ 三份实现（Rust / Python / Node）的**取数顺序与算式必须逐字一致**：
顺序一变，同一个种子出来的粒子就不同了。
"""

from ..errors import RunError, RunTypeError, RunValueError

__all__ = ["新建", "发射", "更新", "画", "数量", "清"]

_键们 = ["寿命", "速度", "角度", "重力", "尺寸", "颜色", "形状", "淡出"]

_MASK64 = (1 << 64) - 1

# ⚠️ 与 `图形` 的 `无画布` **逐字相同**（判据盯着）
_无画布 = ("这个平台没有画布：「图形」模块只在网页 / 窗口里有"
           "（命令行和 Node 上没有画布）。要做图表请用「表格」，要出网页请用「站点」。")


def _默认():
    return {
        "寿命": 1.0,
        "速度": [60.0, 160.0],
        "角度": [0.0, 360.0],
        "重力": 300.0,
        "尺寸": [2.0, 4.0],
        "颜色": ["#ffffff"],
        "形状": "矩形",
        "淡出": True,
    }


def _字典(v, 名, 参="选项"):
    if not isinstance(v, dict):
        raise RunTypeError(f"「{名}」的「{参}」要字典，给的是{type(v).__name__}")
    return v


def _数(v, 名, 参):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise RunTypeError(f"「{名}」的「{参}」要数字，给的是{type(v).__name__}")
    return float(v)


def _区间(选, 键, 谁):
    v = 选.get(键)
    if v is None:
        raise RunError(f"粒子选项里缺「{键}」（内部错误：默认值没填）")
    if isinstance(v, (list, tuple)):
        if len(v) != 2:
            raise RunValueError(f"「{谁}」的「{键}」要给 [最小, 最大]，现在有 {len(v)} 项")
        a, b = _数(v[0], 谁, 键), _数(v[1], 谁, 键)
        if a > b:
            raise RunValueError(f"「{谁}」的「{键}」区间反了：最小 {a} 大于最大 {b}")
        return a, b
    n = _数(v, 谁, 键)
    return n, n


def _颜色们(选, 谁):
    v = 选.get("颜色")
    if v is None:
        return ["#ffffff"]
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple)):
        出 = []
        for x in v:
            if not isinstance(x, str):
                raise RunTypeError(f"「{谁}」的颜色要给文本，给的是{type(x).__name__}")
            出.append(x)
        if not 出:
            raise RunValueError(f"「{谁}」的颜色列表是空的")
        return 出
    raise RunTypeError(
        f"「{谁}」的颜色要给文本或文本列表，给的是{type(v).__name__}")


def _摇(系统):
    """xorshift64*：**与 Rust 侧同一套位运算**（每步都掩到 64 位）。

    状态存在 `系统["状态"]`（十进制文本）：它要跨 2^53，而三宿主里只有 Python / Rust
    有大整数 —— 文本是唯一「三边都精确」的写法。`系统["种子"]` 是用户给的那个数。
    """
    x = int(系统["状态"]) & _MASK64
    x ^= (x << 13) & _MASK64
    x ^= x >> 7
    x ^= (x << 17) & _MASK64
    x &= _MASK64
    系统["状态"] = str(x)
    return (x >> 11) / float(1 << 53)


def 新建(选项=None):
    """M208：造一个粒子系统。选项：寿命 / 速度 / 角度 / 重力 / 尺寸 / 颜色 / 形状 / 淡出 / 种子。"""
    合 = _默认()
    种 = 20261117
    if 选项 is not None:
        d = _字典(选项, "新建")
        for k, v in d.items():
            if k == "种子":
                if isinstance(v, bool) or not isinstance(v, int):
                    raise RunTypeError(f"「新建」的「种子」要整数，给的是{type(v).__name__}")
                if v < 0:
                    raise RunValueError(f"「新建」的种子不能是负数，现在是 {v}")
                if v > 9007199254740991:
                    raise RunValueError(
                        f"「新建」的种子最大 9007199254740991（2^53-1），现在是 {v}")
                种 = v
                continue
            if k not in _键们:
                raise RunValueError(
                    f"粒子选项里没有「{k}」这一项 —— 能用的是："
                    "寿命 / 速度 / 角度 / 重力 / 尺寸 / 颜色 / 形状 / 淡出 / 种子")
            合[k] = v
    # 先过一遍校验（错在新建时报，而不是等第一次发射）
    _区间(合, "寿命", "新建")
    _区间(合, "速度", "新建")
    _区间(合, "角度", "新建")
    _区间(合, "尺寸", "新建")
    _颜色们(合, "新建")
    形 = 合.get("形状", "矩形")
    if 形 not in ("矩形", "圆"):
        raise RunValueError(f"粒子的形状只有「矩形」和「圆」，给的是「{形}」")
    return {"粒子们": [], "选项": 合, "种子": 种, "状态": str(种 & _MASK64)}


def _是系统(v, 名):
    if not isinstance(v, dict) or "粒子们" not in v or "选项" not in v:
        raise RunValueError(f"这个字典不是粒子系统 —— `粒子.新建()` 的返回值才认（「{名}」）")
    return v


def 发射(系统, x, y, 数量, 选项=None):
    """M208：在 (x, y) 喷 `数量` 个粒子；`选项` 可以覆盖这次发射的几项。"""
    d = _是系统(系统, "发射")
    x = _数(x, "发射", "x")
    y = _数(y, "发射", "y")
    if isinstance(数量, bool) or not isinstance(数量, int):
        raise RunTypeError(f"「发射」的数量要整数，给的是{type(数量).__name__}")
    if 数量 < 0:
        raise RunValueError(f"「发射」的数量不能是负数，现在是 {数量}")
    if 数量 > 5000:
        raise RunValueError(f"「发射」一次最多 5000 个（现在要 {数量}）—— 要更多就多调几次")
    合 = dict(d["选项"])
    if 选项 is not None:
        复 = _字典(选项, "发射")
        for k, v in 复.items():
            if k not in _键们:
                raise RunValueError(
                    f"粒子选项里没有「{k}」这一项 —— 能用的是："
                    "寿命 / 速度 / 角度 / 重力 / 尺寸 / 颜色 / 形状 / 淡出")
            合[k] = v
    寿lo, 寿hi = _区间(合, "寿命", "发射")
    速lo, 速hi = _区间(合, "速度", "发射")
    角lo, 角hi = _区间(合, "角度", "发射")
    尺lo, 尺hi = _区间(合, "尺寸", "发射")
    色 = _颜色们(合, "发射")
    重 = _数(合.get("重力", 0.0), "发射", "重力")
    形 = 合.get("形状", "矩形")
    淡 = bool(合.get("淡出", True))

    import math

    def 间(lo, hi):
        return lo + (hi - lo) * _摇(d)

    for _ in range(数量):
        寿 = max(间(寿lo, 寿hi), 0.001)
        速 = 间(速lo, 速hi)
        角 = math.radians(间(角lo, 角hi))
        尺 = 间(尺lo, 尺hi)
        色一 = 色[int(_摇(d) * len(色)) % len(色)]
        d["粒子们"].append({
            "x": x, "y": y,
            "速x": 速 * math.cos(角),
            "速y": -速 * math.sin(角),
            "寿": 寿, "总寿": 寿,
            "尺寸": 尺, "颜色": 色一,
            "重力": 重, "形状": 形, "淡出": 淡,
        })
    return 数量


def 更新(系统, 秒):
    """M208：推进 `秒`（重力 + 位移 + 寿命）；返回还活着几个。"""
    d = _是系统(系统, "更新")
    dt = _数(秒, "更新", "秒")
    if dt < 0:
        raise RunValueError(f"「更新」的秒数不能是负数，现在是 {dt}")
    活 = []
    for p in d["粒子们"]:
        寿 = p["寿"] - dt
        if 寿 <= 0:
            continue
        p["寿"] = 寿
        速y = p["速y"] + p["重力"] * dt
        p["速y"] = 速y
        p["x"] = p["x"] + p["速x"] * dt
        p["y"] = p["y"] + 速y * dt
        活.append(p)
    d["粒子们"] = 活
    return len(活)


def 数量(系统):
    """M208：现在有几个粒子。"""
    return len(_是系统(系统, "数量")["粒子们"])


def 清(系统):
    """M208：清空（返回清掉几个）。"""
    d = _是系统(系统, "清")
    n = len(d["粒子们"])
    d["粒子们"] = []
    return n


def 画(系统):
    """M208：画出来（走 `图形.批量`，一次通道）。返回画了几个。**只在页面 / 窗口里有。**"""
    d = _是系统(系统, "画")
    # 先过平台检查（与 Rust 侧同序：0 个粒子也算「要用画布」）
    raise RunError(_无画布)
