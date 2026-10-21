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

__all__ = ["读工作簿", "读工作表"]

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
            f"「{p}」不是一个 .xlsx（Excel 的 .xlsx 是 zip 包；老的 .xls 不支持）"
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
