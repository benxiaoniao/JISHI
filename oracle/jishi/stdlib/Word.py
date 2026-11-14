# -*- coding: utf-8 -*-
"""基石标准库：Word（.docx 读写）。

`.docx` 和 `.xlsx` 是同一种东西：**一个 zip 包 + 几份 XML**。真正要紧的只有
`word/document.xml`（正文），可选 `word/styles.xml`（样式名）与
`word/header1.xml` / `word/footer1.xml`（页眉页脚）。

## 八个函数

| 函数 | 干什么 |
|---|---|
| `读文档(路径)` | 一次解析出**段落 / 表格 / 图片数 / 页眉 / 页脚** |
| `新建()` | 造一个空文档（就是个字典，当场能加东西） |
| `加标题(文档, 文本, 级别)` | 加一级/二级/三级标题 |
| `加段落(文档, 文本, 选项)` | 加正文段落，选项里能写粗体/斜体/字号/对齐 |
| `加表格(文档, 表)` | 把一张**表格值**原样塞进文档 |
| `加分页(文档)` | 强制换页 |
| `保存(文档, 路径)` | 写成一个真正的 `.docx` |
| `填充模板(模板, 路径, 数据)` | 把模板里的 `{{键}}` 换成数据，另存为新文件 |

## 为什么写出去的文件 Word 能打开

必须凑齐三份东西，少一份 Word 就说「文件已损坏」：

1. `[Content_Types].xml` —— 告诉 Word「这是 docx、里面有哪些部件」；
2. `_rels/.rels` —— 从包根找到 `word/document.xml`；
3. `word/document.xml` —— 正文本身。

📌 **写出去是 stored（不压缩）**：与 `Excel.写工作簿` 同一个理由 ——
deflate 的输出在不同 zlib 版本上可能不同字节，而 `stored` 就是原样存，
**写出来的文件逐字节可复现**，判据才好钉。

## 这一版明确不做

- **写图片**：插图要在 `document.xml` 里拼 `w:drawing` + 写 `word/media/` +
  补关系文件，是独立一块活。**读**的时候能报「有几张」（`图片数`）。
- **段落内混排**（半句粗体半句正常）：一段一个样式。要混排得拆成多个 run，
  接口会从「一段一个文本」变成「一串片段」，那是另一套 API。
- 批注 / 修订 / 域代码 / 公式 / 图表 / 自动目录 / 脚注。
"""

from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree as _ET

from ..errors import RunFileError, RunTypeError, RunValueError
from ..runtime import type_name

__all__ = [
    "读文档", "新建", "加标题", "加段落", "加表格", "加分页", "保存", "填充模板",
]

#: WordprocessingML 的命名空间。**解析时不靠它**（按本地名认标签）——
#: 因为有的工具会写 `w:`、有的写 `w14:`，前缀本来就是任意取的。
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

#: 对齐：中文名 ↔ Word 内部值。写的时候查前一张，读的时候查后一张。
_对齐们 = {"左": "left", "居中": "center", "右": "right", "两端": "both"}

#: 样式 ID ↔ 中文名。Word 文件里存的是**样式 ID**（`Heading1`），
#: 而用户看到的是「标题 1」。读的时候翻过来 —— 否则用户拿到的是
#: `Heading1` 这种内部名字，既不好懂也没法直接拿去判断。
_样式中文 = {
    "Heading1": "标题1", "Heading2": "标题2", "Heading3": "标题3",
    "Heading4": "标题4", "Heading5": "标题5", "Heading6": "标题6",
    "Title": "标题", "Normal": "正文", "Subtitle": "副标题",
}
_样式反向 = {v: k for k, v in _样式中文.items()}


def _本地(tag) -> str:
    """`{ns}p` → `p`；没有命名空间就原样返回。"""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _属性(el, 名: str) -> str:
    """按**本地名**取属性（`w:val` 与 `val` 都认）。"""
    for k, v in el.attrib.items():
        if _本地(k) == 名:
            return v
    return ""


def _要文档(路径) -> list:
    """打开一个 .docx，返回 `[(成员名, 字节), …]`。"""
    p = str(路径)
    try:
        with zipfile.ZipFile(p) as z:
            return [(i.filename, z.read(i.filename)) for i in z.infolist()]
    except FileNotFoundError:
        raise RunFileError(
            f"找不到文件「{p}」", hint="检查路径；.docx 要用 Word 存成「Word 文档」格式")
    except zipfile.BadZipFile:
        raise RunTypeError(
            f"「{p}」不是 .docx（它连 zip 包都不是）",
            hint=".docx / .xlsx / .pptx 都是 zip 包；老的 .doc 不是，要用 Word 另存")


def _找部件(成员们, 名字: str):
    名 = 名字.lower()
    for n, b in 成员们:
        if n.lower() == 名:
            return b
    return None


def _取正文(成员们) -> bytes:
    b = _找部件(成员们, "word/document.xml")
    if b is None:
        raise RunTypeError(
            "这个 zip 里没有 word/document.xml —— 它不是 Word 文档",
            hint=".docx 的正文一定在 word/document.xml；xlsx / pptx 里没有这个文件")
    return b


def _文字(el) -> str:
    """一个元素下**所有** `t` 里的文字拼起来（一个段落可能有多个 run）。"""
    出 = []
    for x in el.iter():
        if _本地(x.tag) == "t" and x.text:
            出.append(x.text)
        elif _本地(x.tag) == "tab":
            出.append("\t")
        elif _本地(x.tag) == "br":
            出.append("\n")
    return "".join(出)


def _段落样式(p) -> str:
    for x in p.iter():
        if _本地(x.tag) == "pStyle":
            原 = _属性(x, "val")
            # 样式 ID → 中文名；不认识的**原样给**（自定义样式就是这么用的）
            return _样式中文.get(原, 原)
    return ""


def _格子们(tbl) -> list:
    """一个 `w:tbl` → 二维列表。横向合并（`gridSpan`）的格子**重复填**。"""
    数据 = []
    for tr in tbl:
        if _本地(tr.tag) != "tr":
            continue
        行 = []
        for tc in tr:
            if _本地(tc.tag) != "tc":
                continue
            文本 = _文字(tc)
            跨 = 1
            for x in tc.iter():
                if _本地(x.tag) == "gridSpan":
                    try:
                        跨 = max(1, int(_属性(x, "val") or "1"))
                    except ValueError:
                        跨 = 1
            for _ in range(跨):
                行.append(文本)
        数据.append(行)
    return 数据


def _表头们(数据: list) -> list:
    """拿第一行当表头；列名重名就补后缀（表格不允许重名，`取列` 会取到第一个）。"""
    if not 数据:
        return []
    原 = [str(x) for x in 数据[0]]
    见: dict = {}
    出 = []
    for 名 in 原:
        基 = 名 if 名 else "列"
        if 基 in 见:
            见[基] += 1
            出.append(f"{基}{见[基] + 1}")
        else:
            见[基] = 0
            出.append(基)
    return 出


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------

def 读文档(路径) -> dict:
    """一次解析出文档的**段落、表格、图片数、页眉、页脚**。

    返回的字典长这样::

        令 文 = Word.读文档("报告.docx")
        打印(文["段落"][0]["文本"])        # 第一段的文字
        打印(文["段落"][0]["样式"])        # 「标题 1」/「正文」…（没有就是空文本）
        遍历 表 在 文["表格"]：
            打印(表格.转TSV(表))
        打印(文["图片数"], 文["页眉"], 文["页脚"])

    📌 `表格` 那一项是**表格对象**的列表（与 `表格.读表格` 同一个类型），
    能直接喂 `筛选` / `汇总` / `转文本`。
    📌 表格的**第一行当表头**；横向合并的格子会把值**重复填**进每一列。
    """
    成员们 = _要文档(路径)
    正文 = _ET.fromstring(_取正文(成员们))

    表格们 = []
    for el in 正文.iter():
        if _本地(el.tag) == "tbl":
            from .表格 import _Table

            数据 = _格子们(el)
            表格们.append(_Table(_表头们(数据), [list(r) for r in 数据[1:]]))

    # ⚠️ 段落只收 `body` 的**直接子** `w:p`：表格里的段落属于表格，
    # 再算一遍用户会看到重复内容（最容易搞错的一处）。
    段落 = _正文段落(正文)

    图片 = sum(1 for el in 正文.iter() if _本地(el.tag) in ("drawing", "pict"))

    页眉 = ""
    页脚 = ""
    for n, b in 成员们:
        低 = n.lower()
        if 低.startswith("word/header") and 页眉 == "":
            页眉 = _文字(_ET.fromstring(b))
        elif 低.startswith("word/footer") and 页脚 == "":
            页脚 = _文字(_ET.fromstring(b))

    return {
        "段落": 段落,
        "表格": 表格们,
        "图片数": 图片,
        "页眉": 页眉,
        "页脚": 页脚,
    }


def _正文段落(正文) -> list:
    """只收 `body` 的**直接子** `w:p`（表格里的段落属于表格，不该混进来）。"""
    出 = []
    for body in 正文:
        if _本地(body.tag) != "body":
            continue
        for el in body:
            if _本地(el.tag) == "p":
                出.append({"文本": _文字(el), "样式": _段落样式(el)})
    return 出


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------
#
# 文档对象就是个**字典**：`{"块们": [块, …]}`。块也是字典。这样三宿主
# （Python / Rust / Node）都用「字典 + 列表」表示，不必各造一个对象类型。

def 新建() -> dict:
    """造一个空文档，接着用 `加标题` / `加段落` / `加表格` 往里加，最后 `保存`。

    例::

        令 文 = Word.新建()
        Word.加标题(文, "季度报告", 1)
        Word.加段落(文, "这一季度的销售额同比增长 12%。")
        Word.保存(文, "报告.docx")
    """
    return {"块们": []}


def _要文(文档) -> dict:
    if not isinstance(文档, dict) or "块们" not in 文档:
        raise RunTypeError(
            f"这里要一个文档对象（`Word.新建()` 的返回值），"
            f"给过来的却是 {type_name(文档)}",
            hint="先 令 文 = Word.新建()，再往它里面加内容")
    return 文档


def 加标题(文档, 文本, 级别: int = 1) -> None:
    """加一个标题。`级别` 是 1 / 2 / 3（对应 Word 的「标题 1/2/3」）。"""
    文 = _要文(文档)
    if isinstance(级别, bool) or not isinstance(级别, int) or not 1 <= 级别 <= 3:
        raise RunValueError(
            f"标题级别只能是 1 / 2 / 3，给的是 {级别!r}",
            hint="一级标题写 1、二级标题写 2；Word 里一共六级，这一版只做了前三级")
    文["块们"].append({"种类": "段落", "文本": str(文本),
                       "样式": f"标题{级别}", "选项": {}})


def 加段落(文档, 文本, 选项=None) -> None:
    """加一个正文段落。

    `选项` 是字典，可以写这四项（都可不写）::

        Word.加段落(文, "重点！", {"粗体": 真, "字号": 14, "对齐": "居中"})

    | 键 | 取值 |
    |---|---|
    | `粗体` / `斜体` | `真` / `假` |
    | `字号` | 数字（磅） |
    | `对齐` | `"左"` / `"居中"` / `"右"` / `"两端"` |
    """
    文 = _要文(文档)
    if 选项 is None:
        选项 = {}
    if not isinstance(选项, dict):
        raise RunTypeError(
            f"「选项」要给一个字典（比如 粗体 那一项），"
            f"给过来的却是 {type_name(选项)}")
    净 = {}
    for 键, 值 in 选项.items():
        名 = str(键)
        if 名 in ("粗体", "斜体"):
            净[名] = bool(值)
        elif 名 == "字号":
            if isinstance(值, bool) or not isinstance(值, (int, float)):
                raise RunTypeError(f"「字号」要给一个数字（磅），给的是 {type_name(值)}")
            if not 1 <= float(值) <= 200:
                raise RunValueError(f"「字号」要在 1 到 200 磅之间，现在是 {值}")
            净[名] = float(值)
        elif 名 == "对齐":
            对齐 = str(值)
            if 对齐 not in _对齐们:
                raise RunValueError(
                    f"「对齐」只能是 {' / '.join(_对齐们)}，给的是「{对齐}」")
            净[名] = 对齐
        else:
            raise RunValueError(
                f"「选项」里没有「{名}」这一项",
                hint="能写的只有：粗体 / 斜体 / 字号 / 对齐")
    文["块们"].append({"种类": "段落", "文本": str(文本), "样式": "正文", "选项": 净})


def 加表格(文档, 表) -> None:
    """把一张**表格值**原样塞进文档（第一行当表头）。"""
    from .表格 import _Table

    文 = _要文(文档)
    if not isinstance(表, _Table):
        raise RunTypeError(
            f"「加表格」要一个表格（`表格.读表格` 的返回值），"
            f"给过来的却是 {type_name(表)}",
            hint="要自己拼表：先 表格.从记录(...) 再传进来")
    文["块们"].append({"种类": "表格",
                       "表头": [str(h) for h in 表.headers],
                       "行们": [[str(c) for c in r] for r in 表.rows]})


def 加分页(文档) -> None:
    """强制换一页（后面加的内容从下一页开始）。"""
    _要文(文档)["块们"].append({"种类": "分页"})


# -- 生成 XML ---------------------------------------------------------------

def _转义(s: str) -> str:
    """XML 文本转义。**只转这五个**（多转会把合法内容弄坏）。"""
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;")
             .replace("'", "&apos;"))


def _段落XML(块: dict) -> str:
    样式 = 块.get("样式", "正文")
    选项 = 块.get("选项", {}) or {}
    段属性 = []
    # 「正文」是默认样式，写出来等于不写；别的样式都给个 ID
    if 样式 and 样式 != "正文":
        段属性.append(f'<w:pStyle w:val="{_样式反向.get(样式, 样式)}"/>')
    if 选项.get("对齐"):
        段属性.append(f'<w:jc w:val="{_对齐们[选项["对齐"]]}"/>')
    r属性 = []
    if 选项.get("粗体"):
        r属性.append("<w:b/>")
    if 选项.get("斜体"):
        r属性.append("<w:i/>")
    if 选项.get("字号"):
        # Word 用「半磅」：14 磅 → 28
        r属性.append(f'<w:sz w:val="{int(round(float(选项["字号"]) * 2))}"/>')
    x = "<w:p>"
    if 段属性:
        x += "<w:pPr>" + "".join(段属性) + "</w:pPr>"
    x += "<w:r>"
    if r属性:
        x += "<w:rPr>" + "".join(r属性) + "</w:rPr>"
    x += f'<w:t xml:space="preserve">{_转义(块.get("文本", ""))}</w:t></w:r></w:p>'
    return x


def _表格XML(块: dict) -> str:
    表头 = 块.get("表头", [])
    行们 = 块.get("行们", [])
    列数 = len(表头)
    if 列数 == 0:
        return ""
    out = ['<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr><w:tblGrid>']
    for _ in range(列数):
        out.append('<w:gridCol w:w="2400"/>')
    out.append("</w:tblGrid>")
    for 行 in [表头] + [list(r) for r in 行们]:
        out.append("<w:tr>")
        for i in range(列数):
            格 = 行[i] if i < len(行) else ""
            out.append('<w:tc><w:p><w:r><w:t xml:space="preserve">'
                       f'{_转义(str(格))}</w:t></w:r></w:p></w:tc>')
        out.append("</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def _分页XML() -> str:
    return '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


_内容类型 = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
    'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.wordprocessingml.document.main+xml"/></Types>'
)

_关系 = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    f'<Relationship Id="rId1" Type="{_R}/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)


def _正文XML(文档: dict) -> str:
    块们 = 文档.get("块们", [])
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<w:document xmlns:w="{_W}"><w:body>']
    for 块 in 块们:
        种 = 块.get("种类")
        if 种 == "段落":
            out.append(_段落XML(块))
        elif 种 == "表格":
            out.append(_表格XML(块))
        elif 种 == "分页":
            out.append(_分页XML())
    # `w:sectPr` 是**必需**的：没有它 Word 会说「文件已损坏」
    out.append('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
               '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"'
               ' w:header="851" w:footer="992" w:gutter="0"/></w:sectPr>')
    out.append("</w:body></w:document>")
    return "".join(out)


def _写zip(路径: str, 成员们) -> None:
    """按**固定顺序**写 zip（判据要逐字节可复现）。"""
    with zipfile.ZipFile(路径, "w", zipfile.ZIP_STORED) as z:
        for 名, 内容 in 成员们:
            info = zipfile.ZipInfo(名, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o600 << 16
            z.writestr(info, 内容)


def _路径(路径) -> str:
    p = str(路径)
    if not p:
        raise RunValueError("保存路径不能是空的")
    if not p.lower().endswith(".docx"):
        raise RunValueError(
            f"保存的文件名要以 .docx 结尾，现在是「{p}」",
            hint="老的 .doc 是另一种格式，写不出来；存成 .docx 就好")
    return p


def 保存(文档, 路径) -> None:
    """把文档写成真正的 `.docx`（Word 能直接双击打开）。"""
    文 = _要文(文档)
    p = _路径(路径)
    _写zip(p, [
        ("[Content_Types].xml", _内容类型.encode("utf-8")),
        ("_rels/.rels", _关系.encode("utf-8")),
        ("word/document.xml", _正文XML(文).encode("utf-8")),
    ])
    return None


def 填充模板(模板, 路径, 数据) -> None:
    """把模板里的 `{{键}}` 换成数据里的值，另存为新文件。

    例（合同模板里写着 `{{甲方}}`、`{{日期}}`）::

        Word.填充模板("合同模板.docx", "张三的合同.docx",
                      {"甲方": "张三", "日期": "2026-11-12"})

    📌 **其余部件原样拷贝**（样式、页眉页脚、图片都不动），只改
    `word/document.xml` 里的文本。
    📌 数据里**没给的键保持原样**（`{{键}}` 留着）—— 静默换成空串更危险：
    你会拿到一份缺项却看着正常的合同。
    ⚠️ 只认**一个 run 里连续写完**的 `{{键}}`。Word 有时会把占位符拆成几段
    （改过中文字体、改过拼写时容易发生），那种替换不了 —— 模板里
    「全选 → 清除格式」再敲一遍占位符最稳。
    """
    from .. import runtime as _rt

    p = _路径(路径)
    if not isinstance(数据, dict):
        raise RunTypeError(
            f"「数据」要给一个字典（键 → 值），给过来的却是 {type_name(数据)}",
            hint='例如 {{"甲方": "张三", "日期": "2026-11-12"}}')
    成员们 = _要文档(模板)
    正文 = _取正文(成员们).decode("utf-8")

    import re as _re

    def 换(m):
        键 = m.group(1)
        if 键 in 数据:
            return _转义(_rt.jishi_repr(数据[键], top=True))
        return m.group(0)

    新正文 = _re.sub(r"\{\{([^{}]*)\}\}", 换, 正文)

    新成员们 = []
    for 名, 内容 in 成员们:
        if 名.lower() == "word/document.xml":
            新成员们.append((名, 新正文.encode("utf-8")))
        else:
            新成员们.append((名, 内容))
    _写zip(p, 新成员们)
    return None
