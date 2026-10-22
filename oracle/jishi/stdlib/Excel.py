# -*- coding: utf-8 -*-
"""基石标准库：Excel 读（M123）。

`.xlsx` 本质上是一个 **zip 包**，里面装着一堆 XML：工作表名在 `xl/workbook.xml`，
每个工作表在 `xl/worksheets/sheetN.xml`，文本常放在 `xl/sharedStrings.xml`。
所以这个模块干的事是「解 zip → 读几份 XML → 拼成 `表格`」。

## 两个函数

| 函数 | 返回 |
|---|---|
| `读工作簿(路径)` | `{"路径": …, "工作表们": ["Sheet1", …]}` |
| `读工作表(簿, 名)` | **`表格` 对象**（与 `表格.读表格` 同一个类型，能直接喂给 `筛选`/`汇总`/`转文本`） |

⚠️ `读工作簿` 返回值里**带路径**，是因为 `读工作表` 还要靠它去打开文件 ——
只给一个名字列表的话，第二步就没法知道读哪个文件了。

## 取值口径（三宿主必须一致，语料 `41_标准库_M123.jsh` 盯着）

| xlsx 里长这样 | 基石里是 |
|---|---|
| `<c><v>42</v></c>` | 数字 `42`（**整数就给整数**，有小数点才是小数） |
| `<c t="s"><v>0</v></c>` | 共享字符串表的第 0 条（文本） |
| `<c t="inlineStr"><is><t>你好</t></is></c>` | 文本 `你好` |
| `<c t="b"><v>1</v></c>` | `真` / `假` |
| `<c t="str"><v>…</v></c>` | 文本（公式算出来的结果） |
| 单元格**不存在** / `<c/>` 里没有 `<v>` | `空` |
| 整行都比表头短 | 缺的列补 `空` |

**第一行当表头**（与 `表格.读表格` 同一口径）。表头空、重名都照原样留着，
不替用户改名 —— 改名会让人对不上 Excel 里看到的东西。

## 已知边界（如实说）

- **不读公式**：`<c>` 里如果有 `<f>`，只取 `<v>`（缓存的计算结果）。没算过的
  （`<v>` 缺失）就是 `空` —— 基石不去求值别人的公式。
- **日期时间按数字读**：xlsx 里日期是「从 1900-01-01 起的天数」+ 一个格式标记。
  这个模块**不做日期换算**，给的就是那个数字（要日期请自己换算或等后继里程碑）。
- **不读合并单元格、样式、批注、条件格式**：只要值。
- `.xls`（老的二进制格式）**不支持**，只支持 `.xlsx`。
"""

from __future__ import annotations

import re as _re
import zipfile as _zip

from ..errors import RunError, RunTypeError, RunValueError

__all__ = ["读工作簿", "读工作表", "写工作簿", "写多表"]

# ---------------------------------------------------------------------------
# 写：三条硬规矩
#
# 1. **stored（不压缩）**：`ZIP_DEFLATED` 的输出在不同 zlib 版本上可能不同字节，
#    而 `stored` 就是原样存 ⇒ 写出来的文件**逐字节可复现**。
# 2. **时间戳写死 1980-01-01**：zip 头里有两个 DOS 时间字段，默认取文件 mtime
#    ⇒ 同一个表格写两次会得到不同字节。写死之后才能比。
# 3. **成员顺序固定**：`[Content_Types].xml` → `_rels/.rels` → `xl/workbook.xml`
#    → `xl/_rels/workbook.xml.rels` → 各 `sheetN.xml`。
#
# 这三条一起，才有「同一份数据写两次、三个宿主各写一次，字节完全相同」这个判据。
# 文本一律写 `inlineStr`（不建共享字符串表）—— 少一个成员，少一处能分叉的地方。
# ---------------------------------------------------------------------------

#: 固定的 zip 时间戳（DOS 时间最小合法值：1980-01-01 00:00:00）。
_ZIP时间 = (1980, 1, 1, 0, 0, 0)

_头 = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_NS主 = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS关 = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS包 = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS型 = "http://schemas.openxmlformats.org/package/2006/content-types"


def _逃(s: str) -> str:
    """XML 文本转义。**`&` 必须第一个换**，否则会把后面生成的 `&lt;` 二次转义。"""
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;"))


def _逃属性(s: str) -> str:
    return _逃(s).replace('"', "&quot;")


def _数字文本(v) -> str:
    """把数值写成 xlsx 里的文本形式。

    ⚠️ 整数**不带小数点**（`1200` 不是 `1200.0`）、布尔给 `1/0`。
    ⚠️ 浮点用 `repr` 的最短往返形式 —— Python 的 `repr(float)` 保证「读回来一模一样」。
    """
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    # 走到这里一定是 float：`%.15g` 会丢掉末位精度，所以用 repr 的往返保证
    文 = repr(float(v))
    return 文


def _单元格XML(列: int, 行号: int, 值) -> str:
    """一个单元格。**空值直接不写** —— xlsx 允许跳过，读的时候会补 `空`。"""
    引用 = _列字母(列) + str(行号)
    if 值 is None:
        return ""
    if isinstance(值, bool):
        return f'<c r="{引用}" t="b"><v>{_数字文本(值)}</v></c>'
    if isinstance(值, (int, float)):
        return f'<c r="{引用}"><v>{_数字文本(值)}</v></c>'
    return (f'<c r="{引用}" t="inlineStr"><is><t>{_逃(str(值))}</t></is></c>')


def _列字母(列: int) -> str:
    """0 → `A`、25 → `Z`、26 → `AA`。"""
    出 = ""
    列 += 1
    while 列 > 0:
        列, 余 = divmod(列 - 1, 26)
        出 = chr(ord("A") + 余) + 出
    return 出


def _表XML(表头: list, 行们: list) -> str:
    """一个工作表的 XML。表头是第 1 行。"""
    片 = [_头, f'<worksheet xmlns="{_NS主}"><sheetData>']
    第 = 1

    def 写一行(值们):
        nonlocal 第
        片.append(f'<row r="{第}">')
        for 列, 值 in enumerate(值们):
            片.append(_单元格XML(列, 第, 值))
        片.append("</row>")
        第 += 1

    写一行(表头)
    for 行 in 行们:
        写一行(行)
    片.append("</sheetData></worksheet>")
    return "".join(片)


def _要表格(t, 名: str):
    from .表格 import _Table

    if not isinstance(t, _Table):
        raise RunTypeError(f"「{名}」要是一个表格（`表格.读表格` 或 `Excel.读工作表` 的返回值）")
    return t


def _写包(路径: str, 表们: list[tuple[str, object]]) -> str:
    """把 `[(表名, 表格)]` 写成一个 .xlsx。返回路径。"""
    import zipfile as _z

    表们 = [(n, _要表格(t, "表")) for n, t in 表们]
    if not 表们:
        raise RunValueError("至少要有一个工作表")

    成员: list[tuple[str, str]] = [
        ("[Content_Types].xml",
         _头 + f'<Types xmlns="{_NS型}">'
         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
         '<Default Extension="xml" ContentType="application/xml"/>'
         '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
         + "".join(
             f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" '
             'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
             for i in range(len(表们)))
         + "</Types>"),
        ("_rels/.rels",
         _头 + f'<Relationships xmlns="{_NS包}">'
         '<Relationship Id="rId1" '
         'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
         'Target="xl/workbook.xml"/></Relationships>'),
        ("xl/workbook.xml",
         _头 + f'<workbook xmlns="{_NS主}" xmlns:r="{_NS关}"><sheets>'
         + "".join(
             f'<sheet name="{_逃属性(n)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
             for i, (n, _) in enumerate(表们))
         + "</sheets></workbook>"),
        ("xl/_rels/workbook.xml.rels",
         _头 + f'<Relationships xmlns="{_NS包}">'
         + "".join(
             f'<Relationship Id="rId{i + 1}" '
             'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
             f'Target="worksheets/sheet{i + 1}.xml"/>'
             for i in range(len(表们)))
         + "</Relationships>"),
    ]
    for i, (_, t) in enumerate(表们):
        成员.append((f"xl/worksheets/sheet{i + 1}.xml", _表XML(t.headers, t.rows)))

    try:
        with _z.ZipFile(路径, "w") as zf:
            for 名, 内容 in 成员:
                信息 = _z.ZipInfo(名, date_time=_ZIP时间)
                信息.compress_type = _z.ZIP_STORED      # 不压缩 ⇒ 字节可复现
                信息.external_attr = 0o600 << 16        # 权限位也写死（否则随 umask 变）
                zf.writestr(信息, 内容.encode("utf-8"))
    except OSError as e:
        raise RunError(f"写不进「{路径}」：{e}") from None
    return 路径


def 写工作簿(路径, 表, 表名: str = "Sheet1") -> str:
    """把一个表格写成单表 `.xlsx`，返回路径。默认表名 `Sheet1`。"""
    p = _要文本(路径, "路径")
    名 = _要文本(表名, "表名")
    if not 名:
        raise RunValueError("「表名」不能是空文本")
    return _写包(p, [(名, 表)])


def 写多表(路径, 表们) -> str:
    """写多个工作表到同一个 `.xlsx`。

    `表们` 是**字典** `{"表名": 表格}`（按字典里的顺序成为标签页顺序）。
    """
    p = _要文本(路径, "路径")
    if not isinstance(表们, dict):
        raise RunTypeError(f"「表们」要是字典（表名 → 表格），现在是 {type(表们).__name__}")
    return _写包(p, list(表们.items()))


#: XML 里带命名空间，ElementTree 会把标签变成 `{名字空间}标签`
#: ⇒ 比较前统一剥掉（不然每处都要写一长串 URI）。
_NS = _re.compile(r"^\{[^}]*\}")


def _本地名(tag: str) -> str:
    return _NS.sub("", tag)


def _读成员(包, 名: str) -> bytes | None:
    try:
        return 包.read(名)
    except KeyError:
        return None


def _要文本(值, 名: str) -> str:
    if not isinstance(值, str):
        raise RunTypeError(f"「{名}」要是文本，现在是 {type(值).__name__}")
    return 值


def _解工作簿(包) -> list[tuple[str, str]]:
    """从 `xl/workbook.xml` + 它自己的 rels 里取出 `[(工作表名, 包内路径), …]`。

    ⚠️ 顺序就是 Excel 里**标签页的顺序**，别按字母排 —— 用户看到的是那个顺序。
    """
    from xml.etree import ElementTree as ET

    原 = _读成员(包, "xl/workbook.xml")
    if 原 is None:
        raise RunValueError("这不是一个 .xlsx 工作簿（里面没有 xl/workbook.xml）")

    # r:id 的前缀不一定叫 `r`，所以按「以 `}id` 结尾」找属性
    关系 = {}
    rel = _读成员(包, "xl/_rels/workbook.xml.rels")
    if rel is not None:
        for 节点 in ET.fromstring(rel):
            if _本地名(节点.tag) != "Relationship":
                continue
            标识 = 节点.get("Id")
            目标 = 节点.get("Target") or ""
            if not 标识:
                continue
            # Target 可能是 `worksheets/sheet1.xml`（相对）或 `/xl/worksheets/sheet1.xml`（绝对）
            if 目标.startswith("/"):
                路径 = 目标.lstrip("/")
            else:
                路径 = "xl/" + 目标
            关系[标识] = 路径.replace("/./", "/")

    结果 = []
    for 节点 in ET.fromstring(原).iter():
        if _本地名(节点.tag) != "sheet":
            continue
        名字 = 节点.get("name")
        if 名字 is None:
            continue
        标识 = None
        for 键, 值 in 节点.attrib.items():
            if 键.endswith("}id") or 键 == "r:id":
                标识 = 值
                break
        # 没有 r:id 的（极少见）退回到按位置猜 sheetN.xml
        路径 = 关系.get(标识 or "", "")
        if not 路径 or _读成员(包, 路径) is None:
            路径 = f"xl/worksheets/sheet{len(结果) + 1}.xml"
        结果.append((名字, 路径))
    return 结果


def _列号(引用: str) -> int:
    """`A1` → 0、`B1` → 1、`AA1` → 26（Excel 是 26 进制，但没有 0）。"""
    列 = 0
    for ch in 引用:
        if "A" <= ch <= "Z":
            列 = 列 * 26 + (ord(ch) - ord("A") + 1)
        elif "a" <= ch <= "z":
            列 = 列 * 26 + (ord(ch) - ord("a") + 1)
        else:
            break
    return 列 - 1


def _取共享串(包) -> list[str]:
    from xml.etree import ElementTree as ET

    原 = _读成员(包, "xl/sharedStrings.xml")
    if 原 is None:
        return []
    出 = []
    for 项 in ET.fromstring(原):
        if _本地名(项.tag) != "si":
            continue
        # 一个 `si` 里可能有多个 `<t>`（富文本分片）⇒ 直接拼起来，别丢内容
        出.append("".join(t.text or "" for t in 项.iter() if _本地名(t.tag) == "t"))
    return 出


def _单元格值(节点, 共享: list[str]):
    """把一个 `<c>` 变基石值。**没值就是 `空`** —— 不是空字符串。"""
    类型 = 节点.get("t") or "n"

    if 类型 == "inlineStr":
        return "".join(t.text or "" for t in 节点.iter() if _本地名(t.tag) == "t")

    值 = None
    for 子 in 节点:
        if _本地名(子.tag) == "v":
            值 = 子.text
            break
    if 值 is None:
        return None

    if 类型 == "s":
        try:
            序 = int(值)
        except ValueError:
            return 值
        return 共享[序] if 0 <= 序 < len(共享) else ""
    if 类型 == "b":
        return 值.strip() not in ("0", "", "false", "FALSE")
    if 类型 in ("str", "e"):
        return 值
    # 数字：整数就给整数（`42` 不该变成 `42.0`），否则给小数
    try:
        if "." in 值 or "e" in 值 or "E" in 值:
            return float(值)
        return int(值)
    except ValueError:
        return 值


def _读一行(节点, 共享: list[str], 列数: int) -> list:
    """读一行，按 `r="A1"` 里的列号**把缺的列补 `空`**。

    ⚠️ 不能「按出现顺序」放 —— xlsx 允许跳着存（空单元格干脆不出现），
        按顺序放会让整行错位，而且不报错（这种错最难发现）。
    """
    行 = [None] * 列数
    for 子 in 节点:
        if _本地名(子.tag) != "c":
            continue
        引用 = 子.get("r") or ""
        列 = _列号(引用) if 引用 else None
        if 列 is None:
            # 没有 `r` 的（极简生成器会这么写）⇒ 顺着往后放
            列 = next((i for i, v in enumerate(行) if v is None), None)
            if 列 is None:
                continue
        if 列 >= 列数:
            continue
        行[列] = _单元格值(子, 共享)
    return 行


def 读工作簿(路径) -> dict:
    """读一个 `.xlsx`，返回 `{"路径": …, "工作表们": [...]}`。

    ⚠️ **不校验文件在不在**这件事交给 `zipfile` —— 它会给出「No such file」，
        我们再翻译成中文。
    """
    p = _要文本(路径, "路径")
    try:
        包 = _zip.ZipFile(p)
    except FileNotFoundError:
        raise RunError(f"找不到文件「{p}」") from None
    except (IsADirectoryError, PermissionError):
        raise RunError(f"打不开文件「{p}」") from None
    except _zip.BadZipFile:
        raise RunValueError(
            f"「{p}」不是一个 .xlsx。Excel 的 .xlsx 其实是 zip 包，老的 .xls 是二进制格式、读不了 —— 用 Excel 打开它「另存为」.xlsx 再试"
        ) from None
    with 包:
        表们 = _解工作簿(包)
    return {"路径": p, "工作表们": [名 for 名, _ in 表们]}


def 读工作表(簿, 名: str):
    """读某个工作表，返回 `表格` 对象（第一行当表头）。

    名字写错时**把可用的名字都列出来** —— 比一句「没有这个工作表」有用得多。
    """
    from .表格 import _Table

    if not isinstance(簿, dict) or "路径" not in 簿 or "工作表们" not in 簿:
        raise RunTypeError("「簿」要是 `读工作簿(…)` 的返回值")
    名 = _要文本(名, "名")

    p = 簿["路径"]
    with _zip.ZipFile(p) as 包:
        表们 = _解工作簿(包)
        命中 = next(((n, 路) for n, 路 in 表们 if n == 名), None)
        if 命中 is None:
            可用 = "、".join(f"「{n}」" for n, _ in 表们) or "（一个都没有）"
            raise RunValueError(f"没有叫「{名}」的工作表。这个工作簿里有：{可用}")

        原 = _读成员(包, 命中[1])
        if 原 is None:
            raise RunValueError(f"工作簿里找不到「{名}」的内容（{命中[1]}）")
        共享 = _取共享串(包)

    from xml.etree import ElementTree as ET

    根 = ET.fromstring(原)
    行们: list[list] = []
    for 节点 in 根.iter():
        if _本地名(节点.tag) != "row":
            continue
        行们.append(节点)

    if not 行们:
        return _Table([], [])

    # 列数 = 所有行里最宽的那一行（表头短了也不丢数据）
    列数 = 0
    for 行 in 行们:
        for 子 in 行:
            if _本地名(子.tag) == "c":
                引用 = 子.get("r") or ""
                if 引用:
                    列数 = max(列数, _列号(引用) + 1)
    if 列数 == 0:
        列数 = 1

    数据 = [_读一行(行, 共享, 列数) for 行 in 行们]
    表头 = ["" if v is None else str(v) for v in 数据[0]]
    return _Table(表头, 数据[1:])
