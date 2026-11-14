# -*- coding: utf-8 -*-
"""基石标准库：统计。一组数据的集中趋势与离散程度。

**为什么需要**：数据分析是核心场景，而 `表格.汇总` 只有平均/最大/最小——
算中位数、标准差、众数现在都得手写（`成绩分析` 项目里就是手写的）。

约定与其它标准库一致：函数名中文、报错中文、**空数据给明确报错**（不返回 空，
否则调用方会在后面某处莫名其妙地崩）。
"""

from __future__ import annotations

import math as _py_math

from ..errors import RunTypeError, RunValueError

__all__ = [
    "求和", "平均", "中位数", "众数", "方差", "标准差", "极差",
    "分位数", "去极值平均", "加权平均",
    # M192：相关 / 回归 / 标准化 / 移动平均 / 直方图 / 秩 / 百分位 / 协方差
    "相关系数", "线性回归", "标准化", "移动平均", "直方图", "秩", "百分位排名", "协方差",
]


def _nums(数据, 名字: str) -> list:
    """把输入统一成数字列表（列表/元组/范围都收）。"""
    if isinstance(数据, (str, bytes)):
        raise RunTypeError(f"「{名字}」要传一组数字，不能传文本")
    try:
        seq = list(数据)
    except TypeError:
        raise RunTypeError(f"「{名字}」要传一组数字（列表）")
    out = []
    for i, v in enumerate(seq):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise RunTypeError(
                f"「{名字}」里第 {i + 1} 个不是数字：{v!r}")
        out.append(v)
    return out


def _need(数据: list, 名字: str) -> list:
    if not 数据:
        raise RunValueError(f"「{名字}」要至少有一个数字，现在是空的")
    return 数据


def 求和(数据) -> object:
    """返回所有数字的和（空列表返回 0）。"""
    return sum(_nums(数据, "求和"))


def 平均(数据) -> float:
    """返回算术平均数。"""
    xs = _need(_nums(数据, "平均"), "平均")
    return sum(xs) / len(xs)


def 中位数(数据) -> float:
    """返回中位数（偶数个时取中间两个的平均）。

    中位数比平均数**抗极端值**：`[1, 2, 3, 4, 1000]` 的平均数是 202，
    中位数仍是 3——看「大多数人是什么水平」时用中位数。
    """
    xs = sorted(_need(_nums(数据, "中位数"), "中位数"))
    n = len(xs)
    mid = n // 2
    if n % 2:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2


def 众数(数据):
    """返回出现次数最多的值。

    并列最多时返回**最先出现**的那个（保证结果可复现，不随字典顺序漂）。
    """
    xs = _need(_nums(数据, "众数"), "众数")
    counts: dict = {}
    order: list = []
    for v in xs:
        if v not in counts:
            counts[v] = 0
            order.append(v)
        counts[v] += 1
    best, best_n = order[0], counts[order[0]]
    for v in order:
        if counts[v] > best_n:
            best, best_n = v, counts[v]
    return best


def 方差(数据, 样本: bool = False) -> float:
    """返回方差（默认**总体方差**；`样本=真` 时用样本方差，除以 n-1）。"""
    xs = _need(_nums(数据, "方差"), "方差")
    n = len(xs)
    if 样本:
        if n < 2:
            raise RunValueError("样本方差至少要 2 个数（要除以 n-1）")
        m = sum(xs) / n
        return sum((x - m) ** 2 for x in xs) / (n - 1)
    m = sum(xs) / n
    return sum((x - m) ** 2 for x in xs) / n


def 标准差(数据, 样本: bool = False) -> float:
    """返回标准差（方差的开方）。`样本=真` 时用样本标准差。"""
    return _py_math.sqrt(方差(数据, 样本))


def 极差(数据) -> object:
    """返回最大值与最小值的差。"""
    xs = _need(_nums(数据, "极差"), "极差")
    return max(xs) - min(xs)


def 分位数(数据, p: float) -> float:
    """返回第 p 分位数（p 取 0–1；0.5 就是中位数，0.25 是下四分位）。

    用**线性插值**（与多数统计软件一致）：`[1, 2, 3, 4]` 的 0.25 分位是 1.75。
    """
    if not isinstance(p, (int, float)) or isinstance(p, bool):
        raise RunTypeError("「分位数」的第二个参数要传 0 到 1 之间的小数")
    if not 0 <= p <= 1:
        raise RunValueError(f"分位数要在 0 到 1 之间，现在是 {p}")
    xs = sorted(_need(_nums(数据, "分位数"), "分位数"))
    if len(xs) == 1:
        return xs[0]
    pos = p * (len(xs) - 1)
    lo = int(_py_math.floor(pos))
    hi = int(_py_math.ceil(pos))
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def 去极值平均(数据, 去掉个数: int = 1) -> float:
    """去掉最大和最小的各「去掉个数」个之后求平均（比赛评分常用）。

    例：`[9.5, 8.0, 9.0, 7.5, 9.9]` 去掉一个最高一个最低后是
    `[9.5, 8.0, 9.0]` 的平均 8.833…。
    """
    if isinstance(去掉个数, bool) or not isinstance(去掉个数, int):
        raise RunTypeError("「去掉个数」要传整数")
    if 去掉个数 < 0:
        raise RunValueError("「去掉个数」不能是负数")
    xs = sorted(_need(_nums(数据, "去极值平均"), "去极值平均"))
    if 去掉个数 * 2 >= len(xs):
        raise RunValueError(
            f"去掉 {去掉个数} 个最高和最低之后就没数据了"
            f"（一共 {len(xs)} 个）")
    kept = xs[去掉个数:len(xs) - 去掉个数]
    return sum(kept) / len(kept)


def 加权平均(数据, 权重) -> float:
    """返回加权平均数：每个数乘自己的权重再求和，除以权重之和。

    `加权平均([80, 90], [1, 3])` = (80×1 + 90×3) / 4 = 87.5。
    """
    xs = _need(_nums(数据, "加权平均"), "加权平均")
    ws = _nums(权重, "加权平均的权重")
    if len(xs) != len(ws):
        raise RunValueError(
            f"数据和权重的个数不一样：{len(xs)} 个数据、{len(ws)} 个权重")
    total = sum(ws)
    if total == 0:
        raise RunValueError("权重之和是 0，没法算加权平均")
    return sum(x * w for x, w in zip(xs, ws)) / total


# ---------------------------------------------------------------------------
# M192：相关 / 回归 / 标准化 / 移动平均 / 直方图 / 秩 / 百分位 / 协方差
# ---------------------------------------------------------------------------
#
# 三条约定：
# 1. **两个序列必须一样长**（相关、回归、协方差）—— 不一样就报错，不截断；
# 2. **标准差为 0 时不能除**（标准化、相关系数）—— 报一句人话，不给 inf/NaN；
# 3. 结果里**不出现 NaN / inf**：算不出来就报错。


def _两列(甲, 乙, 名字: str):
    xs = _nums(甲, 名字)
    ys = _nums(乙, 名字)
    if len(xs) != len(ys):
        raise RunValueError(
            f"「{名字}」两组数据的个数不一样：{len(xs)} 和 {len(ys)}")
    if len(xs) < 2:
        raise RunValueError(f"「{名字}」至少要 2 组数据")
    return xs, ys


def _均值(xs) -> float:
    return sum(xs) / len(xs)


def 协方差(甲, 乙, 样本: bool = False) -> float:
    """返回两组数的协方差（默认**总体**；`样本=真` 时除以 n-1）。

    协方差的符号说明「一起往哪走」：正 = 一个大另一个也大；
    数值大小受量纲影响，所以比较强弱要看 `相关系数`。
    """
    xs, ys = _两列(甲, 乙, "协方差")
    n = len(xs)
    mx = _均值(xs)
    my = _均值(ys)
    s = 0.0
    for i in range(n):
        s = s + (xs[i] - mx) * (ys[i] - my)
    if 样本:
        if n < 2:
            raise RunValueError("样本协方差至少要 2 组数据")
        return s / (n - 1)
    return s / n


def _秩表(xs) -> list:
    """升序秩（并列取平均秩）。返回与输入**同序**的秩列表。"""
    序 = sorted(range(len(xs)), key=lambda i: xs[i])
    秩 = [0.0] * len(xs)
    i = 0
    while i < len(序):
        j = i
        while j + 1 < len(序) and xs[序[j + 1]] == xs[序[i]]:
            j += 1
        # 并列：位置 i..j（0 基）共 j-i+1 个，占用的秩是 i+1..j+1
        平均 = (i + 1 + j + 1) / 2
        for k in range(i, j + 1):
            秩[序[k]] = 平均
        i = j + 1
    return 秩


def 秩(数据) -> list:
    """返回每个数的**名次**（从小到大，1 开始），并列取平均名次。

    例：`[10, 20, 20, 30]` → `[1, 2.5, 2.5, 4]`。

    📌 并列取平均是统计软件的标准做法 —— 取「先出现的排前面」会让结果依赖
    输入顺序，同一个数据集换个顺序就变，那是不能用来看数据的。
    """
    return _秩表(_nums(数据, "秩"))


def 相关系数(甲, 乙, 方法="皮尔逊") -> float:
    """返回两组数的相关系数（-1 到 1）。1 = 完全同向，0 = 没关系，-1 = 完全反向。

    `方法` 认两种：

    | 写法 | 意思 |
    |---|---|
    | `"皮尔逊"` | 看**线性**关系（默认）；要求数据是数字 |
    | `"斯皮尔曼"` | 看**单调**关系（先转名次再算皮尔逊）；对极端值不敏感 |

    例：`统计.相关系数(身高们, 体重们)` → 0.87 说明正相关较强。

    ⚠️ 某一组完全不变（标准差为 0）时**报错**，不返回 NaN ——
    NaN 会在后面每个计算里悄悄传染，最后很难查。
    """
    名 = str(方法)
    if 名 not in ("皮尔逊", "斯皮尔曼"):
        raise RunTypeError(
            f"「相关系数」不认识的方法「{名}」—— 只能是：皮尔逊 / 斯皮尔曼")
    xs, ys = _两列(甲, 乙, "相关系数")
    if 名 == "斯皮尔曼":
        xs = _秩表(xs)
        ys = _秩表(ys)
    mx = _均值(xs)
    my = _均值(ys)
    n = len(xs)
    sxy = 0.0
    sxx = 0.0
    syy = 0.0
    for i in range(n):
        dx = xs[i] - mx
        dy = ys[i] - my
        sxy = sxy + dx * dy
        sxx = sxx + dx * dx
        syy = syy + dy * dy
    if sxx == 0 or syy == 0:
        raise RunValueError(
            "「相关系数」算不出来：有一组数据的值全都一样（标准差是 0）")
    return sxy / _py_math.sqrt(sxx * syy)


def 线性回归(甲, 乙) -> dict:
    """用最小二乘法拟合一条直线 `乙 ≈ 斜率 × 甲 + 截距`。

    返回一个字典：

    | 键 | 意思 |
    |---|---|
    | `斜率` | 甲每增加 1，乙平均变化多少 |
    | `截距` | 甲为 0 时乙的预测值 |
    | `R方` | 拟合优度（0–1，越接近 1 越贴合） |
    | `残差` | 每个点的「实际值 − 预测值」 |
    """
    xs, ys = _两列(甲, 乙, "线性回归")
    n = len(xs)
    mx = _均值(xs)
    my = _均值(ys)
    sxy = 0.0
    sxx = 0.0
    for i in range(n):
        dx = xs[i] - mx
        sxy = sxy + dx * (ys[i] - my)
        sxx = sxx + dx * dx
    if sxx == 0:
        raise RunValueError("「线性回归」算不出来：甲那组数据全都一样，拟合不出直线")
    斜率 = sxy / sxx
    截距 = my - 斜率 * mx
    残差 = [ys[i] - (斜率 * xs[i] + 截距) for i in range(n)]
    # R² = 1 - SS残差 / SS总
    ss总 = 0.0
    ss残 = 0.0
    for i in range(n):
        d = ys[i] - my
        ss总 = ss总 + d * d
        ss残 = ss残 + 残差[i] * 残差[i]
    r方 = 1.0 if ss总 == 0 else 1 - ss残 / ss总
    return {"斜率": 斜率, "截距": 截距, "R方": r方, "残差": 残差}


def 标准化(数据) -> list:
    """返回 z 分数：每个数减均值、再除以标准差。

    标准化之后这组数的均值是 0、标准差是 1 —— 不同量纲的东西（比如身高和体重）
    放在一起比较时先做这一步。

    ⚠️ 全都一样的数据**报错**（标准差 0，除不了）。
    """
    xs = _need(_nums(数据, "标准化"), "标准化")
    n = len(xs)
    m = _均值(xs)
    # 用**总体**标准差（除以 n），与 `标准差(数据)` 一致
    s = 0.0
    for x in xs:
        d = x - m
        s = s + d * d
    sd = _py_math.sqrt(s / n)
    if sd == 0:
        raise RunValueError("「标准化」算不出来：这组数据的值全都一样（标准差是 0）")
    return [(x - m) / sd for x in xs]


def 移动平均(数据, 窗口: int = 3) -> list:
    """返回移动平均（滑动窗口求平均）。输出比输入**短**，长度是 `n - 窗口 + 1`。

    例：`[1, 2, 3, 4, 5]` 窗口 3 → `[2, 3, 4]`。

    📌 用来把抖动的时间序列「抹平」看趋势。前面凑不满一个窗口的位置
    **不输出**（而不是拿半个窗口凑）—— 半个窗口的平均值会骗人。
    """
    if isinstance(窗口, bool) or not isinstance(窗口, int):
        raise RunTypeError("「移动平均」的窗口要传整数")
    if 窗口 < 1:
        raise RunValueError("「移动平均」的窗口至少是 1")
    xs = _need(_nums(数据, "移动平均"), "移动平均")
    if 窗口 > len(xs):
        raise RunValueError(
            f"「移动平均」的窗口是 {窗口}，但一共只有 {len(xs)} 个数")
    出 = []
    和 = 0.0
    for i in range(len(xs)):
        和 = 和 + xs[i]
        if i >= 窗口:
            和 = 和 - xs[i - 窗口]
        if i >= 窗口 - 1:
            出.append(和 / 窗口)
    return 出


def 直方图(数据, 箱数: int = 10) -> dict:
    """把数据分成若干个等宽的「箱」，数每箱里有多少个。

    返回字典：`{"边界": [...], "计数": [...]}` —— `边界` 有 `箱数 + 1` 个
    （从最小值到最大值），`计数` 有 `箱数` 个。第 i 箱的范围是
    `[边界[i], 边界[i+1])`（**最后一箱含右端点**）。

    ⚠️ 所有数都一样时分不出箱（宽度 0），这时**给一个箱子装全部**。
    """
    if isinstance(箱数, bool) or not isinstance(箱数, int):
        raise RunTypeError("「直方图」的箱数要传整数")
    if 箱数 < 1:
        raise RunValueError("「直方图」的箱数至少是 1")
    xs = _need(_nums(数据, "直方图"), "直方图")
    lo = min(xs)
    hi = max(xs)
    if lo == hi:
        return {"边界": [lo, hi], "计数": [len(xs)]}
    宽 = (hi - lo) / 箱数
    边界 = [lo + 宽 * i for i in range(箱数)] + [hi]
    计数 = [0] * 箱数
    for x in xs:
        k = int((x - lo) / 宽)
        if k >= 箱数:
            k = 箱数 - 1                     # 右端点归最后一箱
        if k < 0:
            k = 0
        计数[k] += 1
    return {"边界": 边界, "计数": 计数}


def 百分位排名(数据, 值) -> float:
    """返回 `值` 在这组数据里的百分位排名（0–1）：**有多少比例的数是 ≤ 它的**。

    例：全班 40 人考了 90 分，`百分位排名(分数们, 90)` = 0.75 表示
    你排在 75%（有 75% 的人分数不高于你）。

    ⚠️ 用的是「≤」而不是「<」：0.9 和「正好 90 分」都算在里面。
    """
    xs = _nums(数据, "百分位排名")
    if not xs:
        raise RunValueError("「百分位排名」要至少有一个数字，现在是空的")
    if isinstance(值, bool) or not isinstance(值, (int, float)):
        raise RunTypeError("「百分位排名」的第二个参数要传一个数字")
    不高于 = 0
    for x in xs:
        if x <= 值:
            不高于 += 1
    return 不高于 / len(xs)
