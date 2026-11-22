# -*- coding: utf-8 -*-
"""基石标准库：矩阵。构造、乘法、转置、行列式、解线性方程组。

**为什么需要**（0.2.9 的主菜之一）：`向量` 解决了「一列数一起算」，
但「多列数一起算」——解方程组、坐标变换、线性回归——需要矩阵。
手写三重循环既啰嗦又容易把行列搞反。

## 四条刻意的设计

1. 🔴 **矩阵就是「列表的列表」，不是新类型**（与 `向量` 同一决定，用户 2026-10-26 拍板）。
   所以 `[[1, 2], [3, 4]]` 既是列表也是矩阵，能下标、能 `遍历`、能直接打印。
2. 🔴 **一律写全名字**：`矩阵.乘(甲, 乙)`，不是 `甲 * 乙`（D89：不做运算符重载）。
3. 🔴 **维度不对必须报错**，并且**把两个矩阵的尺寸都打出来** ——
   「2×3 乘 4×5」一眼就知道哪儿错了。静默按能算的算是最坏的。
4. **行优先**：`甲[行][列]`，与数学课本的写法一致（不是列优先）。

## 两条数值上的决定（写清楚，免得以后争论）

- **`行列式` 对整数矩阵给精确整数**：用无分数消元（Bareiss），
  `[[1,2],[3,4]]` → `-2`。⚠️ 纯浮点消元会给出 `-1.9999999999999993` 这种数，
  在教学语言里**不可接受**；而「差不多等于整数就取整」那种容差补丁更是掩盖问题。
  含小数的矩阵才走浮点消元（那时本来就没有「精确」可言）。
- **`解方程` 对整数方程组给精确解**：克莱姆法则 + 上面那个精确行列式，
  `2x+y-z=8 …` 解出来就是 `[2, 3, -1]`，不是 `[2.0000000000000004, …]`。
  ⚠️ 同理不做「差一点就取整」—— 那会掩盖真信息（真答案是 1.0000000001 也会被抹成 1）。
  含小数的方程组才走浮点消元。
  ⚠️ 代价：克莱姆是 O(n⁴)（每个未知数算一次行列式）。**n 小的时候无感**
  （三元方程 = 4 次行列式），**上百阶就别用它** —— 那种规模本来也该先转小数。
- **超过 2^53 的整数结果退回浮点**（三个宿主同一个界）：JS 的安全整数是 2^53，
  再大就表示不准；与其三个宿主各说各话，不如统一定界。
  ⚠️ 已知的**既有**显示差异（不是本模块引入的）：这种量级的**浮点**
  Rust 宿主打全位数（`1208925819614629174706176.0`），Python / Node 打科学计数法
  （`1.2089258196146292e+24`）。判据只钉到「类型一致」，显示格式另记在路径图里。
"""

from __future__ import annotations

from ..errors import RunTypeError, RunValueError

__all__ = ["构造", "单位", "行数", "列数", "转置", "乘", "行列式", "解方程"]

#: 浮点消元里「算 0」的阈值。定死的常数（不是相对误差）——
#: 三个宿主用同一个值，才谈得上逐字节一致。
_零 = 1e-12

#: 超过这个绝对值就退回浮点。三个宿主同一个界（JS 的安全整数上限 2^53）：
#: 再大 JS 就表示不准，与其三边各说各话，不如统一定界。
_安全整数上限 = 9007199254740992


def _定形(v):
    """精确整数结果 → 值：超过 2^53 给浮点（与 Rust / Node 同一口径）。"""
    return float(v) if abs(v) > _安全整数上限 else v


def _是数(v) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float))


def _矩阵(m, 名字: str) -> list:
    """校验并返回「行列表的副本」；不是矩阵就给说清位置的错。"""
    if isinstance(m, (str, bytes)):
        raise RunTypeError(f"「{名字}」要传一个矩阵（列表的列表），不能传文本")
    try:
        行们 = list(m)
    except TypeError:
        raise RunTypeError(f"「{名字}」要传一个矩阵（列表的列表）")
    if not 行们:
        raise RunValueError(f"「{名字}」要至少有一行，现在是空的")
    宽 = None
    for i, 行 in enumerate(行们, 1):
        if isinstance(行, (str, bytes)):
            raise RunTypeError(f"「{名字}」第 {i} 行不是列表（是文本）")
        try:
            列们 = list(行)
        except TypeError:
            raise RunTypeError(f"「{名字}」第 {i} 行不是列表")
        if 宽 is None:
            宽 = len(列们)
        elif len(列们) != 宽:
            raise RunValueError(
                f"「{名字}」每行的长度要相同：第 1 行有 {宽} 个，第 {i} 行有 {len(列们)} 个")
        for j, v in enumerate(列们, 1):
            if not _是数(v):
                raise RunTypeError(f"「{名字}」第 {i} 行第 {j} 个不是数字：{v}")
        行们[i - 1] = 列们
    return 行们


def _方形(m: list, 名字: str) -> list:
    if len(m) != len(m[0]):
        raise RunValueError(
            f"「{名字}」只对方阵有定义，这个矩阵是 {len(m)}×{len(m[0])}")
    return m


def _要整数(名字: str, v, 中文名: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise RunTypeError(f"「{名字}」的{中文名}要是整数，给的是 {v}")
    return v


def 构造(行数, 列数, 初值=0) -> list:
    """造一个 `行数 × 列数` 的矩阵，每个元素都是 `初值`（默认 0）。"""
    r = _要整数("构造", 行数, "行数")
    c = _要整数("构造", 列数, "列数")
    if r <= 0 or c <= 0:
        raise RunValueError(f"「构造」的行数和列数都要大于 0（给的是 {r} 和 {c}）")
    if not _是数(初值):
        raise RunTypeError(f"「构造」的初值要是数字，给的是 {初值}")
    return [[初值] * c for _ in range(r)]


def 单位(阶) -> list:
    """造一个 `阶 × 阶` 的单位阵（对角线是 1，其余是 0）。"""
    n = _要整数("单位", 阶, "阶")
    if n <= 0:
        raise RunValueError(f"「单位」的阶要大于 0（给的是 {n}）")
    return [[1 if i == j else 0 for j in range(n)] for i in range(n)]


def 行数(甲) -> int:
    """矩阵有几行。"""
    return len(_矩阵(甲, "行数"))


def 列数(甲) -> int:
    """矩阵有几列。"""
    return len(_矩阵(甲, "列数")[0])


def 转置(甲) -> list:
    """行列互换：`2×3` 进来，`3×2` 出去。"""
    a = _矩阵(甲, "转置")
    return [[a[i][j] for i in range(len(a))] for j in range(len(a[0]))]


def 乘(甲, 乙) -> list:
    """矩阵相乘（`甲` 的列数必须等于 `乙` 的行数）。"""
    a = _矩阵(甲, "乘")
    b = _矩阵(乙, "乘")
    if len(a[0]) != len(b):
        raise RunValueError(
            f"「乘」要求左矩阵的列数等于右矩阵的行数，实际是 "
            f"{len(a)}×{len(a[0])} 乘 {len(b)}×{len(b[0])}")
    出 = []
    for i in range(len(a)):
        行 = []
        for j in range(len(b[0])):
            合 = 0
            for k in range(len(b)):
                合 = 合 + a[i][k] * b[k][j]
            行.append(合)
        出.append(行)
    return 出


def 行列式(甲):
    """矩阵的行列式（只对方阵有定义）。

    整数矩阵给**精确整数**（无分数消元）；含小数才走浮点消元。
    """
    a = _方形(_矩阵(甲, "行列式"), "行列式")
    if all(isinstance(v, int) for 行 in a for v in 行):
        return _定形(_整数行列式(a))
    return _浮点行列式([[float(v) for v in 行] for 行 in a])


def _整数行列式(a: list) -> int:
    """Bareiss 无分数消元：全程整数除法，结果精确（不产生 1e-16 那种脏值）。"""
    n = len(a)
    m = [行[:] for 行 in a]
    if n == 1:
        return m[0][0]
    上一 = 1
    符号 = 1
    for k in range(n - 1):
        if m[k][k] == 0:
            换 = None
            for r in range(k + 1, n):
                if m[r][k] != 0:
                    换 = r
                    break
            if 换 is None:
                return 0
            m[k], m[换] = m[换], m[k]
            符号 = -符号
        主 = m[k][k]
        for i in range(k + 1, n):
            for j in range(k + 1, n):
                m[i][j] = (m[i][j] * 主 - m[i][k] * m[k][j]) // 上一
        上一 = 主
    return 符号 * m[n - 1][n - 1]


def _浮点行列式(a: list) -> float:
    """带主元选取的浮点消元（含小数的矩阵才走这里）。"""
    n = len(a)
    m = [行[:] for 行 in a]
    结果 = 1.0
    for k in range(n):
        主行 = k
        for r in range(k + 1, n):
            if abs(m[r][k]) > abs(m[主行][k]):
                主行 = r
        if abs(m[主行][k]) < _零:
            return 0.0
        if 主行 != k:
            m[k], m[主行] = m[主行], m[k]
            结果 = -结果
        结果 = 结果 * m[k][k]
        for r in range(k + 1, n):
            倍 = m[r][k] / m[k][k]
            for c in range(k, n):
                m[r][c] = m[r][c] - 倍 * m[k][c]
    return 结果


def 解方程(甲, 乙) -> list:
    """解线性方程组 `甲 · x = 乙`，返回 `x` 这一列。

    `甲` 是 `n × n` 的系数矩阵，`乙` 是长度 `n` 的一列数。
    **整数方程组给精确解**（`[2, 3, -1]`），含小数的走浮点（`[1.0, 3.0]`）。
    无唯一解（系数矩阵奇异）时**报错**，不返回一堆 `nan`。
    """
    a = _方形(_矩阵(甲, "解方程"), "解方程")
    n = len(a)
    右值 = _向量(乙, n)
    if (all(isinstance(v, int) for 行 in a for v in 行)
            and all(isinstance(v, int) for v in 右值)):
        精确 = _整数解(a, 右值)
        if 精确 is not None:
            return 精确
    m = [[float(v) for v in 行] for 行 in a]
    右 = [float(v) for v in 右值]
    for k in range(n):
        主行 = k
        for r in range(k + 1, n):
            if abs(m[r][k]) > abs(m[主行][k]):
                主行 = r
        if abs(m[主行][k]) < _零:
            raise RunValueError(
                "「解方程」这个方程组没有唯一解（系数矩阵的行列式为 0）")
        if 主行 != k:
            m[k], m[主行] = m[主行], m[k]
            右[k], 右[主行] = 右[主行], 右[k]
        for r in range(n):
            if r == k:
                continue
            倍 = m[r][k] / m[k][k]
            if 倍 == 0.0:
                continue
            for c in range(k, n):
                m[r][c] = m[r][c] - 倍 * m[k][c]
            右[r] = 右[r] - 倍 * 右[k]
    return [右[i] / m[i][i] for i in range(n)]


def _整数解(a: list, b: list):
    """整数方程组的**精确解**：克莱姆法则（每个未知数算一次精确行列式）。

    `x_i = det(A_i) / det(A)`，其中 `A_i` 是把第 i 列换成常数项。
    能整除就给整数（教学例子基本都是），不能整除就给小数（解本来就是分数）。
    """
    n = len(a)
    底 = _整数行列式([行[:] for 行 in a])
    if 底 == 0:
        raise RunValueError("「解方程」这个方程组没有唯一解（系数矩阵的行列式为 0）")
    if abs(底) > _安全整数上限:
        return None                      # 太大：交回浮点路径，免得三宿主精度不一
    解 = []
    for i in range(n):
        列 = [[b[r] if c == i else a[r][c] for c in range(n)] for r in range(n)]
        分子 = _整数行列式(列)
        if 分子 % 底 == 0:
            解.append(_定形(分子 // 底))
        else:
            解.append(分子 / 底)
    return 解


def _向量(乙, n: int) -> list:
    """解方程右边那一列：接受一列数（长度必须等于 n）。"""
    if isinstance(乙, (str, bytes)):
        raise RunTypeError("「解方程」右边的常数项要传一列数，不能传文本")
    try:
        列 = list(乙)
    except TypeError:
        raise RunTypeError("「解方程」右边的常数项要传一列数（列表）")
    if len(列) != n:
        raise RunValueError(
            f"「解方程」右边要有 {n} 个常数（与系数矩阵的行数一致），实际有 {len(列)} 个")
    for i, v in enumerate(列, 1):
        if not _是数(v):
            raise RunTypeError(f"「解方程」右边第 {i} 个不是数字：{v}")
    return 列
