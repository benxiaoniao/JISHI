# -*- coding: utf-8 -*-
"""基石标准库：PDF（写英文页 + 读文本）。

PDF 和 `Word` / `Excel` 不是一类东西 —— 它不是 zip 包，而是**一串对象 + 一张
交叉引用表（xref）**。文件末尾的 `startxref` 指向 xref，xref 里记着每个对象
在文件里的**字节偏移**。所以写 PDF 的要害不是拼 XML，而是**把每个字节数准**：
偏移差一个字节，严格的阅读器就说文件坏了。

## 八个函数

| 函数 | 干什么 |
|---|---|
| `新建(宽, 高)` | 造一个空文档（A4），**自带一页** |
| `加页(文档)` | 加一页，返回页号（从 1 数） |
| `写文本(文档, 文本, x, y, 选项)` | 写一段文字，`文本` 里有 `\\n` 就自动换行 |
| `画线(文档, x1, y1, x2, y2, 选项)` | 一条直线 |
| `画矩形(文档, x, y, 宽, 高, 选项)` | 矩形（可描边、可填充） |
| `保存(文档, 路径)` | 写成真正的 `.pdf` |
| `页数(路径)` | 这份 PDF 有几页 |
| `读文本(路径)` | 按页读出文本行 |

## 坐标：原点在**左上角**，y 向**下**

PDF 规范的原点在左下角、y 向上。但屏幕上、`图形` 模块里都是「左上角、y 向下」，
两套坐标混着用最容易把图画反。所以这里**统一用屏幕坐标**，内部再翻成 PDF 的。

例（A4 是 595 × 842）::

    令 文 = PDF.新建()
    PDF.写文本(文, "季度报告", 72, 72, {"字号": 20, "粗体": 真})
    PDF.写文本(文, "第一行\\n第二行", 72, 120)
    PDF.画线(文, 72, 110, 523, 110)
    PDF.保存(文, "报告.pdf")

## 字体：只用 14 种标准字体，**写不了中文**

PDF 规范自带 14 种字体（Helvetica / Times / Courier 各四种字形，加 Symbol、
ZapfDingbats），它们**不需要嵌进文件**、任何阅读器都认得。代价是：
**只能写 Latin-1 里的字**。中文要嵌字体子集（文件 +1 MB，还要处理 OFL 许可），
这一版明确不做。

所以 `写文本` 遇到 ASCII 以外的字符会**直接报错**，并且告诉你怎么办：

    要中文报告请用 Word（`Word.加段落`），再由 Word / LibreOffice 导出 PDF。

📌 报错而不是「悄悄输出乱码」：乱码 PDF 打开才发现的代价，比写的时候就报大得多。

## 这一版明确不做

- **中文 / 日文 / 韩文**：要嵌字体子集（见上）。
- **表格自动排版**：`加表格` 不存在。要画表就自己算 `写文本` 的 x / y 加 `画线`。
- **页眉页脚 / 目录 / 页码**：自己往每页写。
- **压缩流**（写出去是**不压缩**的裸流）：省那几百字节不值得，而且 deflate 的
  输出在不同 zlib 版本上可能不同字节 —— 不压缩才能保证**逐字节可复现**。
- **读** 时不做：OCR（扫描件会明确报「没有文本层」）、加密 PDF、LZW / ASCII85
  之外的花式过滤器、分栏还原（多栏会被拼成一行）。
"""

import re
import zlib

from ..errors import RunFileError, RunTypeError, RunValueError
from ..runtime import type_name

__all__ = [
    "新建", "加页", "写文本", "画线", "画矩形", "保存", "页数", "读文本",
]

#: A4，单位是 PDF 的点（1 点 = 1/72 英寸）。
_A4宽 = 595.0
_A4高 = 842.0

#: 14 种标准字体里我们认识的三种「家族」，每种四款字形。
#: 索引 = `粗体 * 2 + 斜体`，正好对应 PDF 的四种后缀。
_字体家族 = {
    "Helvetica": ("Helvetica", "Helvetica-Oblique",
                  "Helvetica-Bold", "Helvetica-BoldOblique"),
    "Times": ("Times-Roman", "Times-Italic",
              "Times-Bold", "Times-BoldItalic"),
    "Courier": ("Courier", "Courier-Oblique",
                "Courier-Bold", "Courier-BoldOblique"),
}
#: 中文别名 —— 用户不该被迫记 `Helvetica` 这种拼写。
_字体别名 = {
    "无衬线": "Helvetica", "黑体": "Helvetica",
    "衬线": "Times", "宋体": "Times",
    "等宽": "Courier", "打字机": "Courier",
}


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------

def 新建(宽=None, 高=None) -> dict:
    """造一个空 PDF，**自带一页**（和 Word 新建文档就有一页一样）。

    默认是 A4（595 × 842 点）。要别的尺寸就给 `宽` / `高`，单位是点。
    """
    宽 = _尺寸(宽, _A4宽, "宽")
    高 = _尺寸(高, _A4高, "高")
    return {"宽": 宽, "高": 高, "页们": [{"内容": []}]}


def _尺寸(v, 默认, 谁: str) -> float:
    if v is None:
        return 默认
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise RunTypeError(f"页{谁}要给一个数字（点），给的是 {type_name(v)}")
    f = float(v)
    if not 1 <= f <= 20000:
        raise RunValueError(f"页{谁}要在 1 到 20000 点之间，现在是 {v}")
    return f


def _要文档(文档) -> dict:
    if not isinstance(文档, dict) or "页们" not in 文档:
        raise RunTypeError(
            f"这里要一个 PDF 文档（`PDF.新建()` 的返回值），给过来的却是 {type_name(文档)}",
            hint="先 令 文 = PDF.新建()，再往它里面写")
    return 文档


def _当前页(文档) -> dict:
    return 文档["页们"][-1]


def 加页(文档) -> int:
    """加一页，返回页号（**从 1 数**）。之后写的内容都落到这一页上。"""
    文 = _要文档(文档)
    文["页们"].append({"内容": []})
    return len(文["页们"])


def _坐标(v, 谁: str, 名: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise RunTypeError(
            f"「{谁}」的 {名} 要给一个数字，给的是 {type_name(v)}")
    f = float(v)
    if f != f or f in (float("inf"), float("-inf")):
        raise RunValueError(f"「{谁}」的 {名} 不能是 NaN 或无穷")
    return f


def _颜色(v, 谁: str):
    """`[红, 绿, 蓝]`，每个 **0–255**。

    📌 不用 0–1 小数：`[1, 0, 0]` 在两种口径下分别是「几乎全黑」和「纯红」，
    差别太大，不能靠猜。文档写死 0–255。
    """
    if v is None:
        return (0.0, 0.0, 0.0)
    if not isinstance(v, (list, tuple)) or len(v) != 3:
        raise RunTypeError(
            f"「{谁}」的「颜色」要给三个数 [红, 绿, 蓝]（每个 0–255），"
            f"现在是 {type_name(v)}")
    出 = []
    for x in v:
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            raise RunTypeError(f"「{谁}」的颜色分量要是数字，给的是 {type_name(x)}")
        f = float(x)
        if not 0 <= f <= 255:
            raise RunValueError(f"「{谁}」的颜色分量要在 0–255 之间，现在是 {x}")
        出.append(f / 255.0)
    return tuple(出)


def _字体(选项: dict, 谁: str) -> str:
    """选项里的 `字体` / `粗体` / `斜体` → 14 种标准字体里的完整名。"""
    名 = 选项.get("字体", "Helvetica")
    s = str(名)
    s = _字体别名.get(s, s)
    # 大小写不敏感：用户写 helvetica 也该认
    for 家族 in _字体家族:
        if s.lower() == 家族.lower():
            索 = (2 if 选项.get("粗体") else 0) + (1 if 选项.get("斜体") else 0)
            return _字体家族[家族][索]
    raise RunValueError(
        f"「{谁}」不认识的字体「{s}」—— 只能是：Helvetica / Times / Courier"
        f"（也可以写 无衬线 / 衬线 / 等宽）",
        hint="PDF 自带的标准字体只有这几种；要别的字体得嵌进文件，这一版不做")


def _查ASCII(文本: str, 谁: str) -> None:
    """标准字体只装得下 ASCII 可打印字符（PDF 的字符串是**单字节**的）。

    🔴 这里必须**报错**而不是过滤或替换成 `?`：乱码 PDF 是「打开才发现」，
    到那时已经不知道是哪一步出的问题了。
    """
    for 序号, c in enumerate(文本):
        o = ord(c)
        if c == "\n" or c == "\t":
            if c == "\t":
                raise RunValueError(
                    f"「{谁}」的文本里有制表符（第 {序号 + 1} 个字符）—— "
                    "PDF 不会自动对齐，请换成空格")
            continue
        if o < 0x20:
            raise RunValueError(
                f"「{谁}」的文本里有控制字符 U+{o:04X}（第 {序号 + 1} 个字符）")
        if o > 0x7E:
            raise RunValueError(
                f"「{谁}」只写得下英文和常见符号：第 {序号 + 1} 个字符是「{c}」"
                f"（U+{o:04X}），PDF 的 14 种标准字体里没有它",
                hint="要中文报告请用 Word（`Word.加段落`），再由 Word / LibreOffice 导出 PDF")


def 写文本(文档, 文本, x=72, y=72, 选项=None) -> None:
    """在当前页写一段文字。`x` / `y` 是**左上角原点、y 向下**的坐标（点）。

    `文本` 里的 `\\n` 自动换行（行距 = 字号 × 1.4，可用 `选项["行距"]` 改）。

    `选项` 是个字典，四项都可省：

    | 键 | 取值 | 默认 |
    |---|---|---|
    | `字体` | `"Helvetica"` / `"Times"` / `"Courier"`（或 无衬线 / 衬线 / 等宽） | Helvetica |
    | `粗体` / `斜体` | `真` / `假` | 假 |
    | `字号` | 数字（点） | 12 |
    | `颜色` | `[红, 绿, 蓝]`，各 0–255 | 黑 |
    | `行距` | 数字（点） | 字号 × 1.4 |

    ⚠️ **只做得下英文和常见符号**（ASCII）。中文会**报错**并告诉你改用 `Word`
    —— 标准字体里没有中文字形，硬写出来是乱码。
    """
    文 = _要文档(文档)
    if 选项 is None:
        选项 = {}
    if not isinstance(选项, dict):
        raise RunTypeError(
            f"「选项」要给一个字典（比如 {{\"字号\": 20}}），给的是 {type_name(选项)}")
    内容 = str(文本)
    _查ASCII(内容, "写文本")

    字号 = 选项.get("字号", 12)
    if isinstance(字号, bool) or not isinstance(字号, (int, float)):
        raise RunTypeError(f"「字号」要给一个数字（点），给的是 {type_name(字号)}")
    字号 = float(字号)
    if not 1 <= 字号 <= 400:
        raise RunValueError(f"「字号」要在 1 到 400 点之间，现在是 {选项.get('字号')}")

    行距 = 选项.get("行距", 字号 * 1.4)
    if isinstance(行距, bool) or not isinstance(行距, (int, float)):
        raise RunTypeError(f"「行距」要给一个数字（点），给的是 {type_name(行距)}")
    行距 = float(行距)
    if 行距 <= 0:
        raise RunValueError("「行距」要大于 0")

    _当前页(文)["内容"].append({
        "种": "文本",
        "文本": 内容,
        "x": _坐标(x, "写文本", "x"),
        "y": _坐标(y, "写文本", "y"),
        "字体": _字体(选项, "写文本"),
        "字号": 字号,
        "行距": 行距,
        "颜色": _颜色(选项.get("颜色"), "写文本"),
    })
    return None


def 画线(文档, x1, y1, x2, y2, 选项=None) -> None:
    """画一条直线。`选项` 认 `颜色`（0–255 三元组）和 `线宽`（点，默认 1）。"""
    文 = _要文档(文档)
    if 选项 is None:
        选项 = {}
    if not isinstance(选项, dict):
        raise RunTypeError(f"「选项」要给一个字典，给的是 {type_name(选项)}")
    线宽 = 选项.get("线宽", 1)
    if isinstance(线宽, bool) or not isinstance(线宽, (int, float)):
        raise RunTypeError(f"「线宽」要给一个数字（点），给的是 {type_name(线宽)}")
    if not 0 < float(线宽) <= 100:
        raise RunValueError(f"「线宽」要在 0 到 100 点之间，现在是 {选项.get('线宽')}")
    _当前页(文)["内容"].append({
        "种": "线",
        "x1": _坐标(x1, "画线", "x1"),
        "y1": _坐标(y1, "画线", "y1"),
        "x2": _坐标(x2, "画线", "x2"),
        "y2": _坐标(y2, "画线", "y2"),
        "线宽": float(线宽),
        "颜色": _颜色(选项.get("颜色"), "画线"),
    })
    return None


def 画矩形(文档, x, y, 宽, 高, 选项=None) -> None:
    """画一个矩形。`(x, y)` 是**左上角**（屏幕坐标），`宽` / `高` 往外长。

    `选项` 认 `颜色`（描边色）、`线宽`、`填充`（给了就填充这个颜色）。
    """
    文 = _要文档(文档)
    if 选项 is None:
        选项 = {}
    if not isinstance(选项, dict):
        raise RunTypeError(f"「选项」要给一个字典，给的是 {type_name(选项)}")
    线宽 = 选项.get("线宽", 1)
    if isinstance(线宽, bool) or not isinstance(线宽, (int, float)):
        raise RunTypeError(f"「线宽」要给一个数字（点），给的是 {type_name(线宽)}")
    if not 0 < float(线宽) <= 100:
        raise RunValueError(f"「线宽」要在 0 到 100 点之间，现在是 {选项.get('线宽')}")
    宽v = _坐标(宽, "画矩形", "宽")
    高v = _坐标(高, "画矩形", "高")
    if 宽v <= 0 or 高v <= 0:
        raise RunValueError(f"「画矩形」的宽和高要大于 0，现在是 {宽} × {高}")
    _当前页(文)["内容"].append({
        "种": "矩形",
        "x": _坐标(x, "画矩形", "x"),
        "y": _坐标(y, "画矩形", "y"),
        "宽": 宽v,
        "高": 高v,
        "线宽": float(线宽),
        "颜色": _颜色(选项.get("颜色"), "画矩形"),
        "填充": None if 选项.get("填充") is None else _颜色(选项["填充"], "画矩形"),
    })
    return None


# -- 生成文件 ---------------------------------------------------------------

def _数(v) -> str:
    """数字 → PDF 里的写法：整数不带小数点，小数最多四位（去掉尾零）。

    📌 三宿主必须**逐字节一致**，所以格式规则要写死：`round(4)` 后再打印。
    """
    f = float(v)
    r = round(f, 4)
    if r == int(r):
        return str(int(r))
    return ("%.4f" % r).rstrip("0").rstrip(".")


def _串(s: str) -> str:
    """PDF 字符串字面量。只有 `(` `)` `\\` 三个字符需要转义。"""
    出 = ["("]
    for c in s:
        if c in "()\\":
            出.append("\\" + c)
        else:
            出.append(c)
    出.append(")")
    return "".join(出)


def _内容流(页: dict, 高: float, 字体序: dict) -> str:
    出 = []
    for 操作 in 页["内容"]:
        种 = 操作["种"]
        if 种 == "文本":
            r, g, b = 操作["颜色"]
            出.append("BT")
            出.append("/F%d %s Tf" % (字体序[操作["字体"]], _数(操作["字号"])))
            出.append("%s %s %s rg" % (_数(r), _数(g), _数(b)))
            出.append("1 0 0 1 %s %s Tm" % (_数(操作["x"]), _数(高 - 操作["y"])))
            for i, 行 in enumerate(操作["文本"].split("\n")):
                if i > 0:
                    出.append("0 %s Td" % _数(-操作["行距"]))
                if 行:
                    出.append("%s Tj" % _串(行))
            出.append("ET")
        elif 种 == "线":
            r, g, b = 操作["颜色"]
            出.append("%s %s %s RG" % (_数(r), _数(g), _数(b)))
            出.append("%s w" % _数(操作["线宽"]))
            出.append("%s %s m %s %s l S" % (
                _数(操作["x1"]), _数(高 - 操作["y1"]),
                _数(操作["x2"]), _数(高 - 操作["y2"])))
        elif 种 == "矩形":
            if 操作["填充"] is not None:
                fr, fg, fb = 操作["填充"]
                出.append("%s %s %s rg" % (_数(fr), _数(fg), _数(fb)))
            r, g, b = 操作["颜色"]
            出.append("%s %s %s RG" % (_数(r), _数(g), _数(b)))
            出.append("%s w" % _数(操作["线宽"]))
            # `re` 要的是**左下角**：屏幕上 y 是顶边，PDF 里是底边
            出.append("%s %s %s %s re" % (
                _数(操作["x"]), _数(高 - 操作["y"] - 操作["高"]),
                _数(操作["宽"]), _数(操作["高"])))
            出.append("B" if 操作["填充"] is not None else "S")
    出.append("")                      # 保证末尾有换行
    return "\n".join(出)


def _生成(文: dict) -> bytes:
    """拼出整个 PDF 字节流。**对象编号布局**：

    | 编号 | 是什么 |
    |---|---|
    | 1 | Catalog（根） |
    | 2 | Pages（页树根） |
    | 3 … 3+n-1 | 每页一个 Page 对象 |
    | 3+n … 3+2n-1 | 每页一个内容流 |
    | 3+2n … | 用到的字体 |

    xref 的偏移必须**逐字节算准**，所以先拼 `body` 再回头数。
    """
    页们 = 文["页们"]
    宽 = 文["宽"]
    高 = 文["高"]
    n = len(页们)

    # 用到的字体：去重 + 排序（排序是为了**逐字节可复现**）
    用到 = []
    for 页 in 页们:
        for 操作 in 页["内容"]:
            if 操作["种"] == "文本" and 操作["字体"] not in 用到:
                用到.append(操作["字体"])
    用到.sort()
    字体序 = {}
    for i, 名 in enumerate(用到):
        字体序[名] = i + 1            # 资源名 /F1 /F2 …（每页各自一套，编号一样）

    对象们 = []                        # 下标 i 对应对象号 i+1

    # 1: Catalog
    对象们.append(b"<< /Type /Catalog /Pages 2 0 R >>")

    # 2: Pages
    孩子 = " ".join("%d 0 R" % (3 + i) for i in range(n))
    对象们.append(("<< /Type /Pages /Kids [%s] /Count %d >>" % (孩子, n)).encode("ascii"))

    # 3 … : 每页
    for i, 页 in enumerate(页们):
        内容号 = 3 + n + i
        字体项 = ""
        if 用到:
            字体项 = " /Font << " + " ".join(
                "/F%d %d 0 R" % (字体序[名], 3 + 2 * n + j)
                for j, 名 in enumerate(用到)) + " >>"
        对象们.append(
            ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %s %s]"
             " /Resources <<%s >> /Contents %d 0 R >>"
             % (_数(宽), _数(高), 字体项, 内容号)).encode("ascii"))

    # 内容流（不压缩：逐字节可复现，见模块文档）
    for 页 in 页们:
        流 = _内容流(页, 高, 字体序).encode("ascii")
        对象们.append(b"<< /Length %d >>\nstream\n" % len(流) + 流 + b"endstream")

    # 字体
    for 名 in 用到:
        对象们.append(
            ("<< /Type /Font /Subtype /Type1 /BaseFont /%s"
             " /Encoding /WinAnsiEncoding >>" % 名).encode("ascii"))

    # 拼 body，同时记下每个对象的**字节偏移**
    出 = bytearray()
    出 += b"%PDF-1.4\n"
    # 这行二进制注释是惯例：让 FTP / 邮件系统别把文件当文本改坏
    出 += b"%\xe2\xe3\xcf\xd3\n"
    偏移们 = []
    for i, 体 in enumerate(对象们):
        偏移们.append(len(出))
        出 += b"%d 0 obj\n" % (i + 1)
        出 += 体
        出 += b"\nendobj\n"

    # 交叉引用表：每条正好 20 字节（10 位偏移 + 空格 + 5 位世代 + 空格 + n/f + 空格 + LF）
    xref位置 = len(出)
    出 += b"xref\n0 %d\n" % (len(对象们) + 1)
    出 += b"0000000000 65535 f \n"        # 第 0 条是「空闲链表头」，固定这样
    for 偏 in 偏移们:
        出 += ("%010d 00000 n \n" % 偏).encode("ascii")

    出 += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(对象们) + 1)
    出 += b"startxref\n%d\n" % xref位置
    出 += b"%%EOF\n"
    return bytes(出)


def _路径(路径) -> str:
    p = str(路径)
    if not p:
        raise RunValueError("保存路径不能是空的")
    if not p.lower().endswith(".pdf"):
        raise RunValueError(
            f"保存的文件名要以 .pdf 结尾，现在是「{p}」")
    return p


def 保存(文档, 路径) -> None:
    """把文档写成真正的 `.pdf` —— 双击就能用阅读器打开。"""
    文 = _要文档(文档)
    p = _路径(路径)
    try:
        数据 = _生成(文)
        with open(p, "wb") as f:
            f.write(数据)
    except OSError as e:
        raise RunFileError(f"写不了「{p}」：{e}")
    return None


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------

_对象头 = re.compile(rb"(\d+)\s+(\d+)\s+obj\b")


def _读文件(路径) -> bytes:
    p = str(路径)
    try:
        with open(p, "rb") as f:
            return f.read()
    except FileNotFoundError:
        raise RunFileError(f"找不到文件「{p}」")
    except OSError as e:
        raise RunFileError(f"读不了「{p}」：{e}")


def _拆体(体: bytes):
    """一个对象体 → `(字典字节, 流字节 or None)`。"""
    k = 体.find(b"stream")
    if k < 0:
        return 体, None
    字典 = 体[:k]
    起 = k + 6
    if 体[起:起 + 2] == b"\r\n":
        起 += 2
    elif 体[起:起 + 1] in (b"\n", b"\r"):
        起 += 1
    # `/Length` 可能是 `5 0 R`（间接引用）—— 那种情况直接用 `endstream` 找尾巴
    m = re.search(rb"/Length\s+(\d+)(?!\s+\d+\s+R)", 字典)
    终 = None
    if m:
        长 = int(m.group(1))
        if 0 <= 长 <= len(体) - 起:
            终 = 起 + 长
    if 终 is None:
        j = 体.find(b"endstream", 起)
        终 = j if j >= 0 else len(体)
    return 字典, 体[起:终]


def _解压(字典: bytes, 流: bytes):
    """按 `/Filter` 决定要不要解压。只认 FlateDecode（最常见的那个）。"""
    if 流 is None:
        return b""
    if b"/FlateDecode" not in 字典:
        return 流
    try:
        return zlib.decompress(流)
    except zlib.error:
        # 有的生成器写出去少两字节 zlib 头 —— 补上再试一次
        try:
            return zlib.decompress(b"\x78\x9c" + 流)
        except zlib.error as e:
            raise RunValueError(f"PDF 里的压缩流解不开（{e}）")


def _对象表(数据: bytes) -> dict:
    """`{对象号: 对象体}`。顺带把对象流（`/Type /ObjStm`）里的对象也拆出来。

    📌 **必须支持对象流**：PDF 1.5 之后，很多生成器把页对象压进一个 ObjStm 里，
    不拆的话会「一页都找不到」—— 而文件本身完全正常。
    """
    起们 = [(m.start(), m.end(), int(m.group(1))) for m in _对象头.finditer(数据)]
    表 = {}
    for i, (_, 尾, 号) in enumerate(起们):
        终 = 起们[i + 1][0] if i + 1 < len(起们) else len(数据)
        体 = 数据[尾:终]
        if 号 not in 表:               # 同一个号出现两次（增量更新）：取最靠前的
            表[号] = 体

    # 拆对象流
    新增 = {}
    for 号, 体 in list(表.items()):
        字典, 流 = _拆体(体)
        if b"/ObjStm" not in 字典:
            continue
        m = re.search(rb"/N\s+(\d+)", 字典)
        f = re.search(rb"/First\s+(\d+)", 字典)
        if not m or not f:
            continue
        解 = _解压(字典, 流)
        对数 = int(m.group(1))
        首 = int(f.group(1))
        头 = 解[:首].split()
        if len(头) < 对数 * 2:
            continue
        for k in range(对数):
            try:
                内号 = int(头[2 * k])
                偏 = int(头[2 * k + 1])
                下偏 = int(头[2 * k + 3]) if k + 1 < 对数 else len(解) - 首
            except (ValueError, IndexError):
                continue
            新增[内号] = 解[首 + 偏: 首 + 下偏]
    for 号, 体 in 新增.items():
        # 🔴 **对象流里的覆盖顶层的**，不是反过来。打包的对象在顶层本来就
        # 不该有实体；真出了同名（增量更新），对象流里那份至少是「打包过」的
        # 那一次，比顶层残留的旧版本更接近真相。
        表[号] = 体
    return 表


def _引用(字典: bytes, 键: bytes):
    """从字典里抠 `/键 12 0 R` 的对象号。"""
    m = re.search(re.escape(键) + rb"\s+(\d+)\s+\d+\s+R", 字典)
    return int(m.group(1)) if m else None


def _引用们(字典: bytes, 键: bytes) -> list:
    """从字典里抠 `/键 [1 0 R 2 0 R]` 的对象号列表；也认单个引用。"""
    m = re.search(re.escape(键) + rb"\s*\[(.*?)\]", 字典, re.S)
    if m:
        return [int(x) for x in re.findall(rb"(\d+)\s+\d+\s+R", m.group(1))]
    单 = _引用(字典, 键)
    return [单] if 单 is not None else []


def _页序(表: dict) -> list:
    """按页树（`/Kids`）的真实顺序返回页对象号 —— **不能按对象号排**。"""
    根 = None
    for 号, 体 in 表.items():
        字典, _ = _拆体(体)
        if b"/Catalog" in 字典:
            根 = _引用(字典, b"/Pages")
            break
    if 根 is None or 根 not in 表:
        # 没有可用的目录：退回「按对象号」，至少不会一页不给
        return [号 for 号, 体 in sorted(表.items())
                if b"/Page" in _拆体(体)[0] and b"/Pages" not in _拆体(体)[0]]

    出 = []

    def 展开(号: int, 深: int):
        if 深 > 12:
            return
        体 = 表.get(号)
        if 体 is None:
            return
        字典, _ = _拆体(体)
        if b"/Pages" in 字典:
            for k in _引用们(字典, b"/Kids"):
                展开(k, 深 + 1)
        else:
            出.append(号)

    展开(根, 0)
    return 出


# -- 内容流词法 -------------------------------------------------------------

def _去空白(数据: bytes, i: int) -> int:
    n = len(数据)
    while i < n:
        c = 数据[i]
        if c in b" \t\r\n\f\x00":
            i += 1
        elif c == 0x25:                # `%` 注释到行尾
            j = 数据.find(b"\n", i)
            i = n if j < 0 else j + 1
        else:
            break
    return i


def _读串(数据: bytes, i: int):
    """`(` 开头，读到配对的 `)`。返回 `(字节, 新位置)`。"""
    出 = bytearray()
    n = len(数据)
    i += 1
    深 = 1
    while i < n:
        c = 数据[i]
        if c == 0x5C:                  # 反斜杠
            i += 1
            if i >= n:
                break
            d = 数据[i]
            if d in (0x6E, 0x72, 0x74, 0x62, 0x66):     # n r t b f
                出.append({0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12}[d])
                i += 1
            elif 0x30 <= d <= 0x37:                       # 八进制，最多三位
                v = 0
                k = 0
                while k < 3 and i < n and 0x30 <= 数据[i] <= 0x37:
                    v = v * 8 + (数据[i] - 0x30)
                    i += 1
                    k += 1
                出.append(v & 0xFF)
            elif d in (0x0A, 0x0D):                       # 续行（`\` 后面直接换行）
                if d == 0x0D and i + 1 < n and 数据[i + 1] == 0x0A:
                    i += 1
                i += 1
            else:
                出.append(d)
                i += 1
            continue
        if c == 0x28:
            深 += 1
        elif c == 0x29:
            深 -= 1
            if 深 == 0:
                i += 1
                break
        出.append(c)
        i += 1
    return bytes(出), i


_分隔 = b" \t\r\n\f/[]<>(){}%"


def _十六进制字节(h: bytes) -> bytes:
    s = re.sub(rb"[^0-9A-Fa-f]", b"", h)
    if len(s) % 2:
        s += b"0"
    try:
        return bytes.fromhex(s.decode("ascii"))
    except ValueError:
        return b""


def _词们(数据: bytes) -> list:
    """把内容流切成 `(类型, 值)`：

    `数`（float）/ `名`（bytes）/ `串`（bytes）/ `符`（操作符名）/
    `[` `]` `<<` `>>`（结构标记，值恒为 None）
    """
    出 = []
    i = 0
    n = len(数据)
    while True:
        i = _去空白(数据, i)
        if i >= n:
            break
        c = 数据[i]
        if c == 0x28:
            s, i = _读串(数据, i)
            出.append(("串", s))
            continue
        if c == 0x3C:                  # `<`
            if 数据[i:i + 2] == b"<<":
                出.append(("<<", None))
                i += 2
                continue
            j = 数据.find(b">", i)
            if j < 0:
                break
            出.append(("串", _十六进制字节(数据[i + 1:j])))
            i = j + 1
            continue
        if c == 0x3E:                  # `>`
            if 数据[i:i + 2] == b">>":
                出.append((">>", None))
                i += 2
                continue
            i += 1
            continue
        if c in (0x5B, 0x5D):          # `[` `]`
            出.append((chr(c), None))
            i += 1
            continue
        if c in (0x7B, 0x7D):          # `{` `}`（PostScript 计算，用不到）
            i += 1
            continue
        if c == 0x2F:                  # `/名`
            j = i + 1
            while j < n and 数据[j] not in _分隔:
                j += 1
            出.append(("名", 数据[i + 1:j]))
            i = j
            continue
        j = i
        while j < n and 数据[j] not in _分隔:
            j += 1
        w = 数据[i:j]
        if not w:
            i += 1
            continue
        try:
            出.append(("数", float(w)))
        except ValueError:
            出.append(("符", w.decode("latin-1")))
        i = j
    return 出


# -- ToUnicode CMap ---------------------------------------------------------

def _cmap(数据: bytes) -> dict:
    """解析 `ToUnicode` CMap，返回 `{码字节: 文本}`。

    认两种写法（覆盖了几乎全部真实文件）::

        beginbfchar  <0001> <0041>  endbfchar
        beginbfrange <0001> <0003> <0041>  endbfrange
                     <0010> <0012> [<0041> <0042> <0043>]

    📌 没有它就只能按 Latin-1 硬解 —— 中文 PDF（子集字体）会变成一堆问号。
    """
    出 = {}
    文 = 数据.decode("latin-1")
    for 块 in re.findall(r"beginbfchar(.*?)endbfchar", 文, re.S):
        对 = re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", 块)
        for 码, 值 in 对:
            try:
                出[_六(码)] = _utf16(值)
            except ValueError:
                continue
    for 块 in re.findall(r"beginbfrange(.*?)endbfrange", 文, re.S):
        # 先处理「目标也是数组」的那种：<lo> <hi> [<a> <b> …]
        for m in re.finditer(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]", 块, re.S):
            try:
                低 = int(m.group(1), 16)
            except ValueError:
                continue
            目标们 = re.findall(r"<([0-9A-Fa-f]*)>", m.group(3))
            宽 = len(_六(m.group(1)))
            for k, t in enumerate(目标们):
                try:
                    出[_定宽(低 + k, 宽)] = _utf16(t)
                except ValueError:
                    continue
        # 再去掉数组那几段，免得被下面的「逐字加一」再匹配一遍
        净 = re.sub(r"<[0-9A-Fa-f]+>\s*<[0-9A-Fa-f]+>\s*\[(?:.*?)\]", " ", 块, flags=re.S)
        for m in re.finditer(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", 净):
            try:
                低 = int(m.group(1), 16)
                高 = int(m.group(2), 16)
                基 = _utf16(m.group(3))
            except ValueError:
                continue
            if 高 < 低 or 高 - 低 > 65535:
                continue
            宽 = len(_六(m.group(1)))
            for k in range(高 - 低 + 1):
                # 逐字 +1：按**最后一个码位**加，中文常用这条路
                出[_定宽(低 + k, 宽)] = _加一(基, k)
    return 出


def _六(h: str) -> bytes:
    s = h if len(h) % 2 == 0 else h + "0"
    return bytes.fromhex(s)


def _定宽(v: int, 宽: int) -> bytes:
    return v.to_bytes(max(宽, 1), "big")


def _utf16(h: str) -> str:
    if not h:
        return ""
    b = _六(h)
    if len(b) % 2:
        b += b"\x00"
    try:
        return b.decode("utf-16-be")
    except UnicodeDecodeError:
        return b.decode("latin-1")


def _加一(s: str, k: int) -> str:
    if not s:
        return s
    return s[:-1] + chr(ord(s[-1]) + k)


def _解码串(字节: bytes, cmap) -> str:
    """按 CMap 把字符串的字节解成文字。没有 CMap 就按 Latin-1。"""
    if not cmap:
        return 字节.decode("latin-1")
    出 = []
    i = 0
    n = len(字节)
    while i < n:
        for 宽 in (2, 1):
            if i + 宽 <= n:
                k = 字节[i:i + 宽]
                if k in cmap:
                    出.append(cmap[k])
                    i += 宽
                    break
        else:
            出.append(chr(字节[i]))
            i += 1
    return "".join(出)


def _页的cmap(表: dict, 页字典: bytes) -> dict:
    """这一页的 ToUnicode（取第一个带 CMap 的字体）。没有就给空字典。"""
    for m in re.finditer(rb"/(F\w+)\s+(\d+)\s+\d+\s+R", 页字典):
        号 = int(m.group(2))
        体 = 表.get(号)
        if 体 is None:
            continue
        字, _ = _拆体(体)
        映 = _引用(字, b"/ToUnicode")
        if 映 is None or 映 not in 表:
            continue
        字2, 流 = _拆体(表[映])
        if 流 is None:
            continue
        return _cmap(_解压(字2, 流))
    return {}


def _文本行们(词们: list, cmap) -> list:
    """跑一遍内容流，把 `Tj` / `TJ` / `'` / `\"` 的文本按 y 坐标分组成行。"""
    行们 = []
    当前 = []
    上y = [None]
    x = [0.0]
    y = [0.0]
    行距 = [12.0]
    操作数 = []
    i = 0

    def 落行():
        if 当前:
            文 = "".join(当前).strip()
            if 文:
                行们.append(文)
            del 当前[:]

    def 收(字节: bytes):
        文 = _解码串(字节, cmap)
        if not 文:
            return
        if 上y[0] is not None and abs(y[0] - 上y[0]) > 1.0:
            落行()
        上y[0] = y[0]
        当前.append(文)

    while i < len(词们):
        类, 值 = 词们[i]
        if 类 == "[":
            数组 = []
            深 = 1
            i += 1
            while i < len(词们):
                c2, v2 = 词们[i]
                if c2 == "[":
                    深 += 1
                elif c2 == "]":
                    深 -= 1
                    if 深 == 0:
                        break
                数组.append((c2, v2))
                i += 1
            操作数.append(("数组", 数组))
            i += 1
            continue
        if 类 != "符":
            操作数.append((类, 值))
            i += 1
            continue

        op = 值
        数们 = [t[1] for t in 操作数 if t[0] == "数"]
        if op == "Tm" and len(数们) >= 6:
            x[0] = 数们[-2]
            y[0] = 数们[-1]
        elif op in ("Td", "TD") and len(数们) >= 2:
            x[0] += 数们[-2]
            y[0] += 数们[-1]
            if op == "TD":
                行距[0] = -数们[-1]
        elif op == "T*":
            y[0] -= 行距[0]
        elif op == "Tf" and len(数们) >= 1:
            行距[0] = 数们[-1] * 1.2
        elif op == "Tj" and 操作数:
            for 类2, 值2 in reversed(操作数):
                if 类2 == "串":
                    收(值2)
                    break
        elif op in ("'", "\""):
            y[0] -= 行距[0]
            for 类2, 值2 in reversed(操作数):
                if 类2 == "串":
                    收(值2)
                    break
        elif op == "TJ" and 操作数:
            for 类2, 值2 in reversed(操作数):
                if 类2 == "数组":
                    for c3, v3 in 值2:
                        if c3 == "串":
                            收(v3)
                        elif c3 == "数" and v3 < -100:
                            # TJ 里的负数是**字距加宽**，大到一定程度才当空格
                            当前.append(" ")
                    break
        操作数 = []
        i += 1
    落行()
    return 行们


def _页们文(数据: bytes):
    表 = _对象表(数据)
    序 = _页序(表)
    页们 = []
    for 号 in 序:
        字典, _ = _拆体(表.get(号, b""))
        cmap = _页的cmap(表, 字典)
        行们 = []
        for 流号 in _引用们(字典, b"/Contents"):
            体 = 表.get(流号)
            if 体 is None:
                continue
            流字, 流 = _拆体(体)
            解 = _解压(流字, 流)
            if 解:
                行们.extend(_文本行们(_词们(解), cmap))
        页们.append(行们)
    return 页们


def _打开(路径) -> list:
    数据 = _读文件(路径)
    if not 数据[:5] == b"%PDF-":
        raise RunValueError(
            f"「{路径}」不是 PDF 文件（开头不是 %PDF-）",
            hint="Word 文档要读就用 Word.读文档；.doc 是老格式，不是 PDF")
    页们 = _页们文(数据)
    if not 页们:
        raise RunValueError(
            f"「{路径}」里一页都没找到 —— 文件可能不完整",
            hint="用阅读器打开确认一下；能打开的话把文件发出来看")
    有文本 = any(行 for 行 in 页们)
    if not 有文本:
        raise RunValueError(
            f"这个 PDF 没有文本层（{len(页们)} 页里一个字都读不出来）—— 大概是扫描件",
            hint="扫描件要 OCR 才行，这一版不做；要提文字请先用别的工具转成文本")
    return 页们


def 页数(路径) -> int:
    """这份 PDF 有几页。"""
    return len(_打开(路径))


def 读文本(路径) -> dict:
    """按页读出文本行，返回 `{"页数": n, "页们": [行列表, …]}`。

    例::

        令 文 = PDF.读文本("报告.pdf")
        打印("一共 ", 文["页数"], " 页")
        遍历 行 在 文["页们"][0]：
            打印(行)

    📌 行是**按 y 坐标**分组的：同一高度上分开写的几段会拼成一行
    （中间不加空格，除非 PDF 里本来就留了很大的字距）。
    📌 分栏排版会被拼成一行（从 2.3 开始我们就说了不做分栏还原）。
    """
    页们 = _打开(路径)
    return {"页数": len(页们), "页们": 页们}
