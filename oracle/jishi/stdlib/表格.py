# -*- coding: utf-8 -*-
"""基石标准库：表格。CSV 表格数据的读写与查询（薄封装 Python csv 模块）。

设计：读表格 返回一个「表格」对象，配 表头/数据行/挑选/筛选/排序按/汇总
等操作，让零基础用户不需要理解嵌套列表也能做常见数据处理。
"""

from __future__ import annotations

import csv as _py_csv

from ..errors import RunFileError, RunTypeError
from ..runtime import jishi_repr
from .文本 import 显示宽度

__all__ = [
    "读表格", "写表格", "写字典表格", "读字典表格", "追加",
    "从记录", "转记录",
    "表头", "数据行", "挑选", "筛选", "排序按", "汇总", "分组汇总", "连接", "转置",
    "转文本", "转TSV",
    # M192：数据分析（透视 / 多列分组 / 宽长表 / 缺失值）
    "透视", "按列分组", "融化", "展开列", "填充缺失", "丢弃缺失",
    "取列", "切片行", "去重", "转换列",
    "合并", "按时间重采样",
]


class _Table:
    """表格对象：表头 + 数据行。"""

    __slots__ = ("headers", "rows")

    def __init__(self, headers: list, rows: list):
        self.headers = list(headers)
        self.rows = [list(r) for r in rows]

    #: 给 `类型()` 用的中文名（见 runtime.type_name 的钩子）
    _jishi_type_name = "表格"

    def __repr__(self):
        return f"<表格 {len(self.rows)} 行 x {len(self.headers)} 列>"


def _check_path(路径) -> str:
    if 路径 is None:
        raise RunTypeError("需要提供文件路径")
    return str(路径)


def _column_index(t: _Table, 列名) -> int:
    name = str(列名)
    try:
        return t.headers.index(name)
    except ValueError:
        raise RunTypeError(
            f"表格里没有「{name}」这一列",
            hint=f"现有的列：{'、'.join(t.headers)}")


# -- 读写 ------------------------------------------------------------------

def 读表格(路径) -> _Table:
    """读取带表头的 CSV 文件，返回表格对象。

    第一行是表头，例如 姓名,分数 → 表头(t) 得 ['姓名', '分数']。
    """
    try:
        with open(_check_path(路径), "r", encoding="utf-8-sig", newline="") as f:
            rows = [row for row in _py_csv.reader(f)]
    except FileNotFoundError:
        raise RunFileError(f"找不到文件「{路径}」")
    except OSError:
        raise RunFileError(f"文件「{路径}」读不出来")
    if not rows:
        raise RunFileError(f"文件「{路径}」是空的，没有表头")
    return _Table(rows[0], rows[1:])


def 写表格(路径, 行列表: list) -> None:
    """把行列表写进 CSV（覆盖原文件）。第一行应为表头。"""
    try:
        with open(_check_path(路径), "w", encoding="utf-8-sig", newline="") as f:
            writer = _py_csv.writer(f)
            for row in 行列表:
                writer.writerow(row)
    except OSError:
        raise RunFileError(f"没法写入文件「{路径}」")


def 写字典表格(路径, 字典列表: list) -> None:
    """把字典列表写进 CSV（键作表头，来自第一个字典）。"""
    if not isinstance(字典列表, list) or not 字典列表:
        raise RunTypeError("「写字典表格」需要一个非空的字典列表")
    first = 字典列表[0]
    if not isinstance(first, dict):
        raise RunTypeError(f"列表里的元素应该是字典，但出现了「{first}」")
    headers = list(first.keys())
    try:
        with open(_check_path(路径), "w", encoding="utf-8-sig", newline="") as f:
            writer = _py_csv.writer(f)
            writer.writerow(headers)
            for d in 字典列表:
                writer.writerow([d.get(h, "") for h in headers])
    except OSError:
        raise RunFileError(f"没法写入文件「{路径}」")


def 读字典表格(路径) -> list:
    """读取带表头的 CSV，返回字典列表：[{列名: 值}, ...]。"""
    try:
        with open(_check_path(路径), "r", encoding="utf-8-sig", newline="") as f:
            return list(_py_csv.DictReader(f))
    except FileNotFoundError:
        raise RunFileError(f"找不到文件「{路径}」")
    except OSError:
        raise RunFileError(f"文件「{路径}」读不出来")


def 追加(路径, 行: list) -> None:
    """往 CSV 文件末尾追加一行（文件不存在则创建，注意表头要自己保证）。"""
    try:
        with open(_check_path(路径), "a", encoding="utf-8-sig", newline="") as f:
            _py_csv.writer(f).writerow(行)
    except OSError:
        raise RunFileError(f"没法写入文件「{路径}」")


# -- 查询 ------------------------------------------------------------------

def 表头(t: _Table) -> list:
    """返回表格的表头（列名列表）。"""
    return list(t.headers)


def 数据行(t: _Table) -> list:
    """返回表格的所有数据行（不含表头）。"""
    return [list(r) for r in t.rows]


def 挑选(t: _Table, 列):
    """从表格里挑出一列或多列。

    挑选(t, "姓名") → 这列所有值的列表；
    挑选(t, ["姓名", "分数"]) → 每行只保留这两列。
    """
    if isinstance(列, (list, tuple)):
        indices = [_column_index(t, c) for c in 列]
        return [[row[i] for i in indices] for row in t.rows]
    i = _column_index(t, 列)
    return [row[i] for row in t.rows]


def 筛选(t: _Table, 列名, 值) -> _Table:
    """筛选出「列名」这一列的值等于「值」的行，返回新表格。"""
    i = _column_index(t, 列名)
    return _Table(t.headers, [row for row in t.rows if row[i] == 值])


def _sort_key(row, i):
    """数值感知排序键：能转数字按数字排，否则按**基石的显示**排。

    ⚠️ 非数字那一支以前用 `str(v)`（M157 修）：`空` 会变成 Python 的 `None`、
    `真` 会变成 `True`，而 Rust 宿主用的是 `空` / `真` —— 于是「同一张表、
    同一列含空值」升序/降序给出的顺序在三宿主上不一样（写第 12 章教程时撞到的）。
    """
    v = row[i]
    try:
        return (0, float(v))
    except (TypeError, ValueError):
        return (1, jishi_repr(v, top=True))


def 排序按(t: _Table, 列名, 倒序: bool = False) -> _Table:
    """按某一列排序，返回新表格；「倒序」为 真 时从大到小。"""
    i = _column_index(t, 列名)
    rows = sorted(t.rows, key=lambda row: _sort_key(row, i), reverse=bool(倒序))
    return _Table(t.headers, rows)


def 汇总(t: _Table, 列名) -> dict:
    """统计某数字列：个数/总和/平均/最大/最小。"""
    i = _column_index(t, 列名)
    values = []
    for row in t.rows:
        try:
            values.append(float(row[i]))
        except (TypeError, ValueError):
            # M157：文案要用**基石的显示**（`空` 而不是 Python 的 `None`）——
            # 以前这里直接 f-string 化，`空` 单元格读出来是 `None`，
            # 而 Rust 宿主印的是「空」（写教程时撞到的三引擎三说法之一）。
            raise RunTypeError(
                f"列「{列名}」里有不是数字的值"
                f"「{jishi_repr(row[i], top=True)}」，没法汇总")
    if not values:
        raise RunTypeError(f"表格没有数据行，没法汇总「{列名}」")
    return {
        "个数": len(values),
        "总和": sum(values),
        "平均": sum(values) / len(values),
        "最大": max(values),
        "最小": min(values),
    }


def 分组汇总(t: _Table, 分组列, 值列) -> dict:
    """按「分组列」的取值分组，对「值列」做统计，返回 `{组: 汇总字典}`。

    每组的那个汇总字典与 `汇总(t, 列名)` **长得一模一样**
    （个数 / 总和 / 平均 / 最大 / 最小）—— 学会一个就会另一个。

    例（按班级算数学平均）：:

        令 各班 = 表格.分组汇总(表, "班级", "数学")
        打印(各班["一班"]["平均"])

    ⚠️ 组按**首次出现**的顺序放进字典（不排序 —— 键可能是文本，也可能是数字，
    混着排序会炸）。
    """
    i组 = _column_index(t, 分组列)
    i值 = _column_index(t, 值列)
    桶: dict = {}
    for row in t.rows:
        桶.setdefault(row[i组], []).append(row[i值])
    出: dict = {}
    for 组, 值们 in 桶.items():
        数值 = []
        for v in 值们:
            try:
                数值.append(float(v))
            except (TypeError, ValueError):
                raise RunTypeError(
                    f"列「{值列}」里有不是数字的值"
                    f"「{jishi_repr(v, top=True)}」，没法汇总")
        出[组] = {
            "个数": len(数值),
            "总和": sum(数值),
            "平均": sum(数值) / len(数值),
            "最大": max(数值),
            "最小": min(数值),
        }
    return 出


def 连接(表甲: _Table, 表乙: _Table, 键甲, 键乙="") -> _Table:
    """按某一列把两张表**接起来**（只留两边都有的键），返回新表格。

    `键乙` 不写就当作和 `键甲` 同名的列。新表的列 = 甲表的列 + 乙表的列
    （乙表那个键列不再重复，免得同一列出现两次）。

    例：`表格.连接(学生表, 成绩表, "学号")`

    ⚠️ 只做**内连接**（两边都有的键才留）；乙表同一个键有多行时**只取第一条**
    —— 需要「一对多展开」的场景请先自己把乙表理成一对一。
    """
    i甲 = _column_index(表甲, 键甲)
    i乙 = _column_index(表乙, 键乙 if 键乙 else 键甲)
    索引: dict = {}
    for row in 表乙.rows:
        索引.setdefault(row[i乙], row)
    出表头 = list(表甲.headers) + [
        h for j, h in enumerate(表乙.headers) if j != i乙]
    出行 = []
    for row in 表甲.rows:
        配对 = 索引.get(row[i甲])
        if 配对 is not None:
            出行.append(list(row) + [v for j, v in enumerate(配对) if j != i乙])
    return _Table(出表头, 出行)


def 从记录(记录们: list) -> _Table:
    """把**字典列表**变成表格 —— JSON 接口回来的数据直接能做表格运算。

    列名取所有记录里出现过（按**首次出现**顺序），缺的格子填空。

    例（这才是它存在的理由）：::

        令 订单们 = 网络.获取JSON(网址)["本页"]     # [{"客户": "甲", "金额": 12}, …]
        令 表 = 表格.从记录(订单们)
        打印(表格.分组汇总(表, "客户", "金额"))

    ⚠️ **M107 之前没有它**：那时只能先 `表格.写字典表格` 落一个临时 CSV、
    再 `表格.读表格` 读回来 —— 一次纯内存的整理被迫走了一遍磁盘（探针
    「接口汇总」的绕道 R103）。
    """
    if not isinstance(记录们, (list, tuple)):
        raise RunTypeError("「从记录」要传一个**字典列表**（例如 JSON 接口返回的那种）")
    列名: list = []
    for rec in 记录们:
        if not isinstance(rec, dict):
            raise RunTypeError(
                f"「从记录」的每一项都要是字典，遇到了「{type(rec).__name__}」")
        for k in rec:
            if k not in 列名:
                列名.append(k)
    行 = [[rec.get(k) for k in 列名] for rec in 记录们]
    return _Table([str(k) for k in 列名], 行)


def 转记录(t: _Table) -> list:
    """把表格变成**字典列表** —— 好喂给 `json.写文件` 或 `网络.提交JSON`。"""
    if not isinstance(t, _Table):
        raise RunTypeError("「转记录」要传一个表格（用 表格.读表格 或 表格.从记录 得到）")
    头 = [str(h) for h in t.headers]
    return [dict(zip(头, row)) for row in t.rows]


def 转置(行列表: list) -> list:
    """把「列表的列表」行列互换。"""
    if not 行列表:
        return []
    return [list(col) for col in zip(*行列表)]


def _要表格(t, 谁: str) -> None:
    if not isinstance(t, _Table):
        # ⚠️ 文案要与 Rust / Node **逐字一致**（报错语料会跨引擎比）。
        raise RunTypeError(
            f"「{谁}」要传一个表格（现在是「{jishi_repr(t, top=True)}」）"
            "—— 用 表格.读表格 或 表格.从记录 得到")


def _是数字(v) -> bool:
    """这一格是不是数（布尔不算 —— Python 里 `True` 也是 `int`）。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _补(文本: str, 宽: int, 靠右: bool) -> str:
    """按**显示宽度**补空格到指定列宽（汉字算 2 列，与 `文本.显示宽度` 同一口径）。"""
    差 = 宽 - 显示宽度(文本)
    if 差 <= 0:
        return 文本
    return (" " * 差 + 文本) if 靠右 else (文本 + " " * 差)


def 转文本(t) -> str:
    """把表格渲染成**给人看**的等宽文本：表头 + 分隔线 + 对齐的数据行。

    对齐全按**显示宽度**来（**一个汉字算 2 列**，与 `文本.显示宽度` 同口径），
    所以中文表格在终端里也是齐的；**数字列自动右对齐**（好比较大小）。

    返回值**末尾不带换行** —— 直接 `打印` 就行，要多行自己加。
    要「给程序读」请用 `表格.转TSV`（或 `表格.写表格` 存 CSV）。
    """
    _要表格(t, "转文本")
    头 = [jishi_repr(h, top=True) for h in t.headers]
    列数 = len(头)
    if 列数 == 0:
        return ""
    行们 = [[jishi_repr(c, top=True) for c in r] for r in t.rows]
    宽 = [显示宽度(x) for x in 头]
    靠右 = []
    for i in range(列数):
        有值 = 全数字 = False
        for j, r in enumerate(t.rows):
            if i >= len(r):
                continue
            v = r[i]
            if v is None or (isinstance(v, str) and v == ""):
                continue
            if not 有值:
                有值 = 全数字 = True
            elif not _是数字(v):
                全数字 = False
        靠右.append(有值 and 全数字)
    for r in 行们:
        for i in range(min(列数, len(r))):
            宽[i] = max(宽[i], 显示宽度(r[i]))
    出 = ["  ".join(_补(头[i], 宽[i], 靠右[i]) for i in range(列数)).rstrip()]
    出.append("  ".join("-" * 宽[i] for i in range(列数)))
    for r in 行们:
        元 = []
        for i in range(列数):
            x = r[i] if i < len(r) else ""
            元.append(_补(x, 宽[i], 靠右[i]))
        出.append("  ".join(元).rstrip())
    return "\n".join(出)


def 转TSV(t) -> str:
    """把表格变成 **TSV 文本**（制表符分隔）—— 好粘进 Excel / 喂给别的程序。

    第一行是表头；行之间用 `\n` 分隔（**不是** `\r\n`，免得平台相关）；
    **末尾不带换行**。

    ⚠️ 单元格里含制表符或换行时**直接报错** —— TSV 装不下它们，
    静默把内容改掉更糟。那种内容请用 `表格.写表格` 存成 CSV。
    """
    _要表格(t, "转TSV")
    出 = []
    for 号, r in enumerate([t.headers] + t.rows):
        元 = []
        for 列, c in enumerate(r):
            s = jishi_repr(c, top=True)
            if "\t" in s or "\n" in s or "\r" in s:
                # ⚠️ 不用 `hint=`：Rust 侧没有 hint 机制，三边文案会对不齐。
                raise RunTypeError(
                    f"第 {号 + 1} 行第 {列 + 1} 格里含制表符或换行，TSV 装不下"
                    " —— 要写这种内容请用 表格.写表格 存成 CSV")
            元.append(s)
        出.append("\t".join(元))
    return "\n".join(出)


# ---------------------------------------------------------------------------
# M192：数据分析（透视 / 多列分组 / 宽长表 / 缺失值）
# ---------------------------------------------------------------------------
#
# 五个函数共用三条约定，写在这里，免得每个函数重复一遍：
#
# 1. **列参数可以给一个列名，也可以给列名列表** —— 中文里「按 班级 分组」
#    和「按 班级、性别 分组」都是同一句话，不该逼用户写 `["班级"]`。
# 2. **「缺失」= 空文本 或 空** —— CSV 里的空格子读进来是 `""`，
#    但从记录造出来的表里可能是 `空`。两个都算，用户不用记。
# 3. **都返回新表格，不改原表** —— 与 `筛选` / `排序按` 一致。

#: `透视` / `按列分组` 认的聚合方式（与 `汇总` 的五个口径对齐）。
聚合方式们 = ("求和", "平均", "计数", "最大", "最小")


def _列们(表: _Table, 列, 谁: str) -> list:
    """把一个「列名或列名列表」规整成 `[列名, …]`，并逐个核对存在。

    ⚠️ 单列用**字符串**、多列用**列表** —— 但用户很容易只写一个列名当列表用，
    所以这里两种都收。不认的写法直接报错（别猜）。
    """
    if isinstance(列, (list, tuple)):
        名们 = [str(x) for x in 列]
    else:
        名们 = [str(列)]
    if not 名们:
        raise RunTypeError(f"「{谁}」至少要给一个列名")
    for 名 in 名们:
        _column_index(表, 名)          # 不存在就由它报「找不到列」
    return 名们


def _缺失(v) -> bool:
    """这一格算不算「没有值」。

    CSV 的空格子读进来是**空文本**，而从记录造的表里可能是 **`空`** ——
    两个都算缺失。`0` 和 `假` **不算**（那是真值，最容易搞错的地方）。
    """
    return v is None or v == ""


def _要聚合(名, 谁: str) -> str:
    s = str(名)
    if s not in 聚合方式们:
        raise RunTypeError(
            f"「{谁}」不认识的聚合方式「{s}」—— 只能是："
            + " / ".join(聚合方式们))
    return s


def _数值们(值们, 列名, 谁: str) -> list:
    """把一批格子转成数字；有非数字就报错（文案与 `汇总` 同一句）。"""
    出 = []
    for v in 值们:
        if _缺失(v):
            continue                   # 缺失不参与计算（不是 0）
        try:
            出.append(float(v))
        except (TypeError, ValueError):
            raise RunTypeError(
                f"「{谁}」：列「{列名}」里有不是数字的值"
                f"「{jishi_repr(v, top=True)}」，没法聚合")
    return 出


def _聚合(值们, 方式: str, 列名, 谁: str):
    """按名字聚合一批值。**空批次**：`计数` 给 0，其余给 `None`（调用方转成空格）。"""
    if 方式 == "计数":
        return len([v for v in 值们 if not _缺失(v)])
    数 = _数值们(值们, 列名, 谁)
    if not 数:
        return None
    if 方式 == "求和":
        return sum(数)
    if 方式 == "平均":
        return sum(数) / len(数)
    if 方式 == "最大":
        return max(数)
    return min(数)                    # 最小


def 透视(t: _Table, 行, 列, 值, 聚合="求和") -> _Table:
    """行 × 列交叉汇总（报表里最常被问的那一句：「按地区和季度交叉看」）。

    返回**一张新表格**：第一列是行标签，后面每个列标签占一列，
    格子里是「该行该列那些值」按 `聚合` 算出来的结果。没有数据的格子留空。

    例（按班级 × 科目看平均分）::

        令 交叉 = 表格.透视(表, "班级", "科目", "分数", "平均")
        打印(交叉)

    📌 **行、列标签都按「首次出现」的顺序排**（不排序）：键可能是文本、也可能是
    数字，混着排序会炸；首次出现顺序至少是稳定的、可复现的。
    📌 同一个 `(行, 列)` 出现多行时按 `聚合` 合并 —— 这才是「透视」相对
    `分组汇总` 的意义。
    """
    _要表格(t, "透视")
    i行 = _column_index(t, 行)
    i列 = _column_index(t, 列)
    i值 = _column_index(t, 值)
    方式 = _要聚合(聚合, "透视")

    行序: list = []
    行已见 = set()
    列序: list = []
    桶: dict = {}
    for row in t.rows:
        r = row[i行]
        c = row[i列]
        # ⚠️ 桶的键是 `(行, 列)`，所以「这行见过没有」得单独记 ——
        # 写成 `if r not in 桶` 会让**每一行**都算新的（透视表直接翻倍）。
        if r not in 行已见:
            行已见.add(r)
            行序.append(r)
        if (r, c) not in 桶:
            桶[(r, c)] = []
            if c not in 列序:
                列序.append(c)
        桶[(r, c)].append(row[i值])

    表头 = [str(行)] + [jishi_repr(c, top=True) for c in 列序]
    数据 = []
    for r in 行序:
        行出 = [r]
        for c in 列序:
            v = _聚合(桶.get((r, c), []), 方式, 值, "透视")
            行出.append("" if v is None else v)
        数据.append(行出)
    return _Table(表头, 数据)


def 按列分组(t: _Table, 分组列们, 值列, 聚合="求和") -> _Table:
    """按**一列或多列**分组聚合，返回一张表格（每组的键 + 聚合结果）。

    与 `分组汇总` 的区别有两条：**能多列分组**、**返回值是表格**（好打印、
    好 `写表格` 存下来）。`分组汇总` 返回的是嵌套字典，适合在程序里取值。

    例::

        令 各班 = 表格.按列分组(表, ["班级", "性别"], "分数", "平均")
        表格.写表格("各班平均.csv", 各班)
    """
    _要表格(t, "按列分组")
    列们 = _列们(t, 分组列们, "按列分组")
    i值 = _column_index(t, 值列)
    索引 = [_column_index(t, c) for c in 列们]
    方式 = _要聚合(聚合, "按列分组")

    键序: list = []
    桶: dict = {}
    for row in t.rows:
        键 = tuple(row[i] for i in 索引)
        if 键 not in 桶:
            桶[键] = []
            键序.append(键)
        桶[键].append(row[i值])

    数据 = []
    for 键 in 键序:
        v = _聚合(桶[键], 方式, 值列, "按列分组")
        数据.append(list(键) + ["" if v is None else v])
    return _Table(list(列们) + [str(值列)], 数据)


def 融化(t: _Table, 保留列们) -> _Table:
    """**宽 → 长**：把「剩下的那些列」压成两列（`变量` / `值`）。

    `保留列们` 是「不压、原样留着」的列（通常是 ID、姓名、日期这类）。

    例::

        令 长得 = 表格.融化(表, "姓名")
        # 原表：姓名 | 语文 | 数学
        # 长出：姓名 | 变量 | 值
        #       小明 | 语文 | 90
        #       小明 | 数学 | 85

    与 `展开列` 成对：一个宽转长，一个长转宽，缺一个就得来回手写循环。
    """
    _要表格(t, "融化")
    保留 = _列们(t, 保留列们, "融化")
    保留集 = set(保留)
    其余 = [h for h in t.headers if h not in 保留集]
    保留索引 = [_column_index(t, h) for h in 保留]
    其余索引 = [_column_index(t, h) for h in 其余]

    数据 = []
    for row in t.rows:
        头 = [row[i] for i in 保留索引]
        for 名, i in zip(其余, 其余索引):
            数据.append(头 + [名, row[i]])
    return _Table(保留 + ["变量", "值"], 数据)


def 展开列(t: _Table, 键列, 值列) -> _Table:
    """**长 → 宽**：把 `键列` 里的每个取值变成一列，格子里填 `值列`。

    除 `键列` / `值列` 之外的列当作「保留列」—— 它们相同的行合成一行。
    某个组合没有值时留空。

    例::

        令 宽得 = 表格.展开列(长得, "变量", "值")
        # 变量 | 值      ⇒  姓名 | 语文 | 数学
        # 小明 | 语文        小明 | 90   | 85
        # 小明 | 数学

    📌 同一 `(保留列, 键)` 出现多次时**取最后一个** —— 宽表里一格只能放一个值，
    与其报错，不如给一条可预期的规则（文档写死，别猜）。
    """
    _要表格(t, "展开列")
    i键 = _column_index(t, 键列)
    i值 = _column_index(t, 值列)
    保留 = [h for h in t.headers if h not in (str(键列), str(值列))]
    保留索引 = [_column_index(t, h) for h in 保留]

    键序: list = []
    行序: list = []
    格: dict = {}
    for row in t.rows:
        键 = row[i键]
        头 = tuple(row[i] for i in 保留索引)
        if 键 not in 键序:
            键序.append(键)
        if 头 not in 格:
            格[头] = {}
            行序.append(头)
        格[头][键] = row[i值]           # 同一个格子出现多次：后一个赢

    数据 = []
    for 头 in 行序:
        数据.append(list(头) + [格[头].get(k, "") for k in 键序])
    return _Table(保留 + [jishi_repr(k, top=True) for k in 键序], 数据)


def 填充缺失(t: _Table, 列, 方法="前向") -> _Table:
    """把某一列的空格子补上，返回新表格（不改原表）。

    `方法` 认三种：

    | 写法 | 意思 |
    |---|---|
    | `"前向"` | 用**上一个**有值的格子补（时间序列最常用；开头那几个没得补，留空） |
    | `"平均"` | 用这一列（非缺失的）平均值补 |
    | 其它值 | 当成**常数**填进去（`0` / `"未知"` 都行） |

    📌 「缺失」= 空文本 或 `空`；`0` 和 `假` **不算**缺失。
    """
    _要表格(t, "填充缺失")
    i = _column_index(t, 列)
    名 = str(方法)

    填充值 = None
    if 名 == "平均":
        数 = _数值们([row[i] for row in t.rows], 列, "填充缺失")
        填充值 = sum(数) / len(数) if 数 else None

    数据 = []
    上一个 = None
    for row in t.rows:
        新 = list(row)
        if _缺失(新[i]):
            if 名 == "前向":
                if 上一个 is not None:
                    新[i] = 上一个
            elif 名 == "平均":
                if 填充值 is not None:
                    新[i] = 填充值
            else:
                新[i] = 方法
        else:
            上一个 = 新[i]
        数据.append(新)
    return _Table(t.headers, 数据)


def 丢弃缺失(t: _Table, 列们=None) -> _Table:
    """删掉**含空格子**的行，返回新表格。

    `列们` 省略就检查**所有列**；给了就只看这几列（别的列空着不算）。

    📌 「缺失」= 空文本 或 `空`；`0` 和 `假` 不算缺失 —— 这一条最容易搞错，
    所以 `统计` 那边也按同一口径。
    """
    _要表格(t, "丢弃缺失")
    if 列们 is None:
        索引 = list(range(len(t.headers)))
    else:
        索引 = [_column_index(t, c) for c in _列们(t, 列们, "丢弃缺失")]
    数据 = [list(row) for row in t.rows
            if not any(_缺失(row[i]) for i in 索引 if i < len(row))]
    return _Table(t.headers, 数据)


def 取列(t: _Table, 列们) -> _Table:
    """只留下这几列（顺序按你给的来），返回新表格。

    `取列(表, ["姓名", "分数"])` —— 列名也可以给单个字符串。
    与 `挑选` 的区别：`挑选` 是**按值挑一行**（行过滤），这个是**按名字挑列**。
    """
    _要表格(t, "取列")
    名们 = _列们(t, 列们, "取列")
    索引 = [_column_index(t, 名) for 名 in 名们]
    数据 = [[row[i] if i < len(row) else "" for i in 索引] for row in t.rows]
    return _Table(list(名们), 数据)


def 切片行(t: _Table, 起=0, 止=None) -> _Table:
    """取第 `起` 到第 `止` 行（**从 0 数、含头不含尾**），返回新表格。

    例：前 10 行 `切片行(表, 0, 10)`；从第 5 行到末尾 `切片行(表, 5)`。

    📌 负数按 Python 的习惯从**末尾**数（`-1` 是最后一行）——
    这对「只要最后 100 行」很有用：`切片行(表, -100)`。
    📌 越界的端点会被夹到合法范围（不报错），与 Python 的切片一致。
    """
    _要表格(t, "切片行")
    总 = len(t.rows)

    def 归一(v, 默认: int) -> int:
        if v is None:
            return 默认
        i = int(v)
        if i < 0:
            i += 总
        return max(0, min(总, i))

    起i = 归一(起, 0)
    止i = 归一(止, 总)
    if 止i < 起i:
        止i = 起i
    return _Table(t.headers, [list(r) for r in t.rows[起i:止i]])


def 去重(t: _Table, 列们=None) -> _Table:
    """按这些列去重，**保留首次出现的那一行**，返回新表格。

    `列们` 省略就整行比；给了就只看这几列（其余列取首次出现那行的值）。
    """
    _要表格(t, "去重")
    if 列们 is None:
        索引 = list(range(len(t.headers)))
    else:
        索引 = [_column_index(t, c) for c in _列们(t, 列们, "去重")]
    见过 = set()
    数据 = []
    for row in t.rows:
        # 用 repr 做键：整数 1 与文本 "1" 不能算同一行（与 `筛选` 的口径一致）
        键 = tuple(jishi_repr(row[i] if i < len(row) else "", top=True) for i in 索引)
        if 键 in 见过:
            continue
        见过.add(键)
        数据.append(list(row))
    return _Table(t.headers, 数据)


def 转换列(t: _Table, 列, 类型="整数") -> _Table:
    """把某一列的格子转成别的类型，返回新表格。

    `类型` 认三种：`"整数"` / `"小数"` / `"文本"`。

    📌 从 `.xlsx` / `.csv` 读进来的列**全是文本**，算之前先转一下是常见活儿。
    📌 转不了的格子**报错并指出第几行** —— 静默变成 0 或空，错得最难查。
    📌 空格子（缺失）**保持原样**，不当成 0。
    """
    _要表格(t, "转换列")
    i = _column_index(t, 列)
    名 = str(类型)
    if 名 not in ("整数", "小数", "文本"):
        raise RunTypeError(
            f"「转换列」不认识的类型「{名}」—— 只能是：整数 / 小数 / 文本")

    数据 = []
    for 行号, row in enumerate(t.rows):
        新 = list(row)
        if i >= len(新):
            新.extend([""] * (i + 1 - len(新)))
        v = 新[i]
        if not _缺失(v):
            try:
                if 名 == "文本":
                    新[i] = jishi_repr(v, top=True)
                elif 名 == "整数":
                    新[i] = int(float(v))
                else:
                    新[i] = float(v)
            except (TypeError, ValueError):
                raise RunTypeError(
                    f"「转换列」：第 {行号 + 1} 行的「{列}」是"
                    f"「{jishi_repr(v, top=True)}」，转不成{名}")
        数据.append(新)
    return _Table(t.headers, 数据)


def 合并(甲: _Table, 乙: _Table, 键甲, 键乙="", 方式="左") -> _Table:
    """把两张表按一个键拼起来（SQL 里的 join）。

    `方式` 认三种：

    | 写法 | 结果 |
    |---|---|
    | `"左"`（默认） | 甲表的行**全留**，乙表没有的列填空 |
    | `"内"` | **只留两边都有**的键 |
    | `"外"` | 两边的键**都留**，缺的那半填空 |

    例（给订单补上客户资料）::

        令 全 = 表格.合并(订单, 客户, "客户编号", "编号", "左")

    📌 与 `连接` 的区别：`连接` 只有左连接且只取第一条匹配；`合并` 三种方式都有。
    📌 乙表里**同一个键有多行**时取**第一条**（与 `连接` 一致）—— 多对多会产生
    笛卡尔积，那需要另一个函数（也不该悄悄发生）。
    """
    _要表格(甲, "合并")
    _要表格(乙, "合并")
    i甲 = _column_index(甲, 键甲)
    键乙名 = str(键乙) if 键乙 not in ("", None) else str(键甲)
    i乙 = _column_index(乙, 键乙名)
    名 = str(方式)
    if 名 not in ("左", "内", "外"):
        raise RunTypeError(
            f"「合并」不认识的连接方式「{名}」—— 只能是：左 / 内 / 外")

    乙索引: dict = {}
    for row in 乙.rows:
        k = jishi_repr(row[i乙], top=True)
        if k not in 乙索引:
            乙索引[k] = row

    乙列 = [j for j in range(len(乙.headers)) if j != i乙]
    表头 = list(甲.headers) + [乙.headers[j] for j in 乙列]
    空甲 = [""] * len(甲.headers)
    数据 = []
    用过 = set()
    for row in 甲.rows:
        k = jishi_repr(row[i甲], top=True)
        乙行 = 乙索引.get(k)
        if 乙行 is None:
            if 名 in ("左", "外"):
                数据.append(list(row) + [""] * len(乙列))
            continue
        用过.add(k)
        数据.append(list(row) + [乙行[j] for j in 乙列])
    if 名 == "外":
        for k, 乙行 in 乙索引.items():
            if k in 用过:
                continue
            # 🔴 只在乙出现的键要**填回甲的键列**，否则那一行看着像「没有键」
            # （第一版三边都漏了，判据当场抓到）
            新增 = list(空甲)
            新增[i甲] = 乙行[i乙]
            数据.append(新增 + [乙行[j] for j in 乙列])
    return _Table(表头, 数据)


def 按时间重采样(表: _Table, 时间列, 周期="月", 值列=None, 聚合="求和") -> _Table:
    """把「按天（或按秒）记的表」汇总成「按月 / 按年」。

    例：每天的销售额 → 每月合计::

        令 月报 = 表格.按时间重采样(日表, "日期", "月", "金额", "求和")

    `周期` 认两种：`"月"` / `"年"`（这一版还不支持「周」，见下）。
    时间列按**文本前几位**切：`"2026-03-15"` 的前 7 位是 `"2026-03"`。

    📌 **不给 `值列`** 时只做「每个周期有多少行」（相当于计数）。
    📌 ⚠️ **「周」这一版不做**：ISO 周不是字符串前缀，要真正的日期算法
    （跨年那一周属于哪一年、闰周…），而基石的时间列现在只是一段文本。
    与其算错，不如明确说不支持 —— 真要按周汇总，先用 `统计.移动平均` 或
    自己从 `日期` 模块取星期几。
    """
    _要表格(表, "按时间重采样")
    i时 = _column_index(表, 时间列)
    名 = str(周期)
    if 名 == "周":
        raise RunTypeError(
            "「按时间重采样」这一版还不支持「周」（要 ISO 周算法，不是字符串前缀）"
            "—— 先用 月 / 年，或者从 日期 模块自己算")
    if 名 not in ("月", "年"):
        raise RunTypeError(
            f"「按时间重采样」不认识的周期「{名}」—— 只能是：月 / 年")
    位数 = 7 if 名 == "月" else 4

    键序: list = []
    桶: dict = {}
    for row in 表.rows:
        t = jishi_repr(row[i时], top=True)
        k = t[:位数]
        if k not in 桶:
            桶[k] = []
            键序.append(k)
        桶[k].append(row)

    if 值列 is None:
        数据 = [[k, len(桶[k])] for k in 键序]
        return _Table([str(时间列) + "（" + 名 + "）", "行数"], 数据)

    i值 = _column_index(表, 值列)
    方式 = _要聚合(聚合, "按时间重采样")
    数据 = []
    for k in 键序:
        v = _聚合([r[i值] for r in 桶[k]], 方式, 值列, "按时间重采样")
        数据.append([k, "" if v is None else v])
    return _Table([str(时间列) + "（" + 名 + "）", str(值列)], 数据)
