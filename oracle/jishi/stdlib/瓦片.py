# -*- coding: utf-8 -*-
"""基石标准库：瓦片（M187）—— 瓦片地图的「画 / 碰 / 取」。

地图数据是**二维整数列表**：`-1` 或 `空` = 这一格没有瓦片，其余是图集里的瓦片号
（按 `列数` 折行：号 0 是左上角那格）。

⚠️ 三份实现（Rust / Python / Node）的规则要逐字一致，对拍判据盯着。
"""

from ..errors import RunTypeError, RunValueError

__all__ = [
    "载入", "画", "碰", "取",
    # M197：碰撞层 / 斜坡 / 矩形取格 / 最近可走格
    "碰撞层", "是实心", "斜坡", "是斜坡", "取矩形内瓦片", "最近可走格",
]


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


# ---------------------------------------------------------------------------
# M197：碰撞层 / 斜坡 / 矩形取格 / 最近可走格
# ---------------------------------------------------------------------------
#
# 三条规则（三宿主必须逐字一致）：
#
# 1. **碰撞层是「哪些瓦片号算实心」的一张名单**，不改原图。同一个图可以建多个
#    碰撞层（地面一个、水面一个）—— 这也是为什么它不写进图里。
# 2. **斜坡只记方向**（`"左"` / `"右"`），不做物理。要「沿斜面滑」的完整手感
#    得另外算 —— 这一版只保证「知道这一格是坡、朝哪边」。
# 3. **`最近可走格` 从当前格向外一圈圈找**，最多找 16 圈；找不到给 `空`，
#    不给「假装找到的」坐标（会把角色瞬移到墙里）。


def _碰撞层(v, 名):
    if not isinstance(v, dict) or "图" not in v or "实心" not in v:
        raise RunTypeError(
            f"「{名}」的第一个参数要碰撞层（`瓦片.碰撞层` 造的），给的是{type(v).__name__}")
    return v


def 碰撞层(瓦片图, 实心号们):
    """M197：造一层碰撞 —— 名单里的瓦片号算**实心**（挡住人）。

    例::

        令 墙 = 瓦片.碰撞层(地牢, [0, 1, 2])
        如果 瓦片.是实心(墙, 5, 3)： …

    同一个图可以建好几层（地面一层、水一层），所以**不改原图**。
    """
    d = _字典(瓦片图, "碰撞层")
    if not isinstance(实心号们, (list, tuple)):
        raise RunTypeError(
            f"「碰撞层」的「实心号们」要整数列表（比如 [0, 1, 2]），"
            f"给的是{type(实心号们).__name__}")
    号们 = []
    for x in 实心号们:
        if isinstance(x, bool) or not isinstance(x, int):
            raise RunValueError(f"「碰撞层」的实心号们只能是整数，出现了 {x!r}")
        if int(x) not in 号们:
            号们.append(int(x))
    号们.sort()
    return {"图": d, "实心": 号们}


def 是实心(碰撞层对象, 列, 行):
    """M197：这一格会不会挡住人（越界算**不实心** —— 地图外面不该是墙）。"""
    c = _碰撞层(碰撞层对象, "是实心")
    return int(取(c["图"], 列, 行)) in c["实心"]


def 斜坡(瓦片图, 列, 行, 方向):
    """M197：把某一格标成斜坡。`方向` 只能是 `"左"`（左低右高）或 `"右"`。

    ⚠️ 这一版**只记方向、不做物理**：`是斜坡` 能问到，但「沿坡滑下去」的手感
    要自己算（按 `方向` 和玩家在格内的相对位置插值高度）。
    """
    d = _字典(瓦片图, "斜坡")
    名 = str(方向)
    if 名 not in ("左", "右"):
        raise RunValueError(f"「斜坡」的方向只能是 左 / 右，给的是「{名}」")
    c, r = int(列), int(行)
    if c < 0 or r < 0 or r >= len(d["行们"]) or c >= len(d["行们"][r]):
        raise RunValueError(
            f"「斜坡」的格子 ({c}, {r}) 不在地图里（地图是 "
            f"{max((len(x) for x in d['行们']), default=0)} 列 × {len(d['行们'])} 行）")
    if "斜坡们" not in d or not isinstance(d["斜坡们"], dict):
        d["斜坡们"] = {}
    # 🔴 键用**文本** `"列,行"`：Python 的 tuple 键到了 Node 就变成数组，
    # 三宿主对不上。文本键是唯一能三边都一样的表示。
    d["斜坡们"][f"{c},{r}"] = 名
    return d


def 是斜坡(瓦片图, 列, 行):
    """M197：这一格是斜坡吗？是就返回方向（`"左"` / `"右"`），不是给 `空`。"""
    d = _字典(瓦片图, "是斜坡")
    坡们 = d.get("斜坡们")
    if not isinstance(坡们, dict):
        return None
    return 坡们.get(f"{int(列)},{int(行)}")


def 取矩形内瓦片(瓦片图, 矩形):
    """M197：这个矩形压到哪些格子？返回 `[{"列", "行", "号"}, …]`（行优先）。

    📌 与 `碰` 的区别：`碰` 只给**瓦片号**（去重排序），这个给**格子坐标** ——
    要做「这一片地上有什么」「把碰到的砖块打掉」就需要知道是哪一格。
    """
    d = _字典(瓦片图, "取矩形内瓦片")
    宽, 高 = d["瓦片宽"], d["瓦片高"]
    for 键 in ("x", "y", "宽", "高"):
        if 键 not in 矩形:
            raise RunValueError(f"「取矩形内瓦片」的矩形里缺「{键}」")
    bx, by = float(矩形["x"]), float(矩形["y"])
    bw, bh = float(矩形["宽"]), float(矩形["高"])
    if bw <= 0 or bh <= 0:
        raise RunValueError("「取矩形内瓦片」的矩形宽高要大于 0")
    c0 = max(0, int(bx // 宽))
    c1 = int((bx + bw) // 宽)
    r0 = max(0, int(by // 高))
    r1 = int((by + bh) // 高)
    出 = []
    for r in range(r0, r1 + 1):
        if r >= len(d["行们"]):
            break
        for c in range(c0, c1 + 1):
            if c >= len(d["行们"][r]):
                break
            出.append({"列": c, "行": r, "号": d["行们"][r][c]})
    return 出


def 最近可走格(瓦片图, 碰撞层对象, x, y):
    """M197：从世界坐标 (x, y) 向外找**最近的不实心格**，返回格子中心的世界坐标
    `{"列", "行", "x", "y"}`；16 圈内都找不到就给 `空`。

    用途：角色被卡进墙里、或者要找「离这里最近的落脚点」时别乱瞬移。
    """
    d = _字典(瓦片图, "最近可走格")
    c = _碰撞层(碰撞层对象, "最近可走格")
    宽, 高 = d["瓦片宽"], d["瓦片高"]
    x = _数(x, "最近可走格", "x")
    y = _数(y, "最近可走格", "y")
    起列 = int(x // 宽)
    起行 = int(y // 高)
    最远 = 16
    for 圈 in range(0, 最远 + 1):
        # 按「到中心格的距离」遍历这一圈（行优先，保证结果稳定）
        候们 = []
        for dr in range(-圈, 圈 + 1):
            for dc in range(-圈, 圈 + 1):
                if max(abs(dr), abs(dc)) != 圈:
                    continue
                候们.append((起行 + dr, 起列 + dc))
        for r, cc in 候们:
            if r < 0 or cc < 0 or r >= len(d["行们"]) or cc >= len(d["行们"][r]):
                continue
            if d["行们"][r][cc] in c["实心"]:
                continue
            return {"列": cc, "行": r,
                    "x": (cc + 0.5) * 宽, "y": (r + 0.5) * 高}
    return None
