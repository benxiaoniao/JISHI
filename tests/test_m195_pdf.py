# -*- coding: utf-8 -*-
"""M195 · `PDF`（写英文页 + 读文本）8 个函数。

## 判据分四层

1. **自写自读**：写文本 / 多行 / 线 / 矩形 / 多页，读回来逐行对上；
2. **读别人的**：读**独立造出来**的最小 PDF —— 未压缩、FlateDecode、
   带 `ToUnicode` 的子集字体、页对象藏在对象流（`/ObjStm`）里。
   这一层专门证明「不是只能读自己写的」；
3. **结构自洽**：`xref` 每条正好 20 字节、**每个偏移都真的指向 `N 0 obj`**、
   `startxref` 真的指向 `xref` —— 这是 PDF 最容易写错、又最难靠「能打开」发现的地方；
4. **明确不做的要说清楚**：扫描件报「没有文本层」、中文报错并指路 `Word`。

## 为什么还要比「三宿主」

`PDF` 在 Python 走 `zlib`、Rust 走自写的 `inflate_raw`、Node 走 `node:zlib`；
文本抽取三边各写了一套词法。三条完全不同的路，**写出的字节必须一模一样**。
"""

import re
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                        # noqa: E402


def _跑(源: str, tmp_path: Path, 名="脚本.jsh") -> tuple:
    脚本 = tmp_path / 名
    脚本.write_text(源, encoding="utf-8", newline="\n")
    r = subprocess.run([str(rust_exe_path()), str(脚本)], capture_output=True,
                       timeout=180, cwd=str(tmp_path))
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


@pytest.fixture(scope="module", autouse=True)
def _要有宿主():
    if not rust_exe_path().is_file():
        pytest.skip("没有 Rust 宿主（cargo build --release）")


# ---------------------------------------------------------------------------
# 独立造 PDF（**不用**基石自己的写出器 —— 否则「自产自读」只能证明自洽）
# ---------------------------------------------------------------------------

def _拼(对象们: list) -> bytes:
    数据 = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    偏们 = []
    for i, 体 in enumerate(对象们):
        偏们.append(len(数据))
        数据 += b"%d 0 obj\n" % (i + 1) + 体 + b"\nendobj\n"
    叉 = len(数据)
    数据 += b"xref\n0 %d\n" % (len(对象们) + 1)
    数据 += b"0000000000 65535 f \n"
    for 偏 in 偏们:
        数据 += ("%010d 00000 n \n" % 偏).encode()
    数据 += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(对象们) + 1, 叉)
    return bytes(数据)


def _流(内容: bytes, 额外=b"") -> bytes:
    return b"<< /Length %d%s >>\nstream\n" % (len(内容), 额外) + 内容 + b"endstream"


def _造最小pdf(路径: Path, 正文: bytes, 压缩=False) -> None:
    """一页、一个 Helvetica、内容流。"""
    页 = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842]"
          b" /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>")
    体 = _流(zlib.compress(正文), b" /Filter /FlateDecode") if 压缩 else _流(正文)
    路径.write_bytes(_拼([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        页,
        体,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]))


# ---------------------------------------------------------------------------
# 1. 自写自读
# ---------------------------------------------------------------------------

def test_自写自读闭环(tmp_path):
    源 = (
        "导入 PDF\n"
        "令 文 = PDF.新建()\n"
        'PDF.写文本(文, "Quarterly Report", 72, 72, {"字号": 20, "粗体": 真})\n'
        'PDF.写文本(文, "one\\ntwo\\n\\nfour", 72, 120, {"字体": "衬线"})\n'
        'PDF.画线(文, 72, 240, 523, 240)\n'
        'PDF.画矩形(文, 72, 260, 100, 50, {"填充": [200, 200, 200]})\n'
        "PDF.加页(文)\n"
        'PDF.写文本(文, "page two", 100, 100)\n'
        'PDF.保存(文, "出.pdf")\n'
        '打印("页数 ", PDF.页数("出.pdf"))\n'
        '令 读 = PDF.读文本("出.pdf")\n'
        '打印("读到的页数 ", 读["页数"])\n'
        '遍历 页号 在 范围(长度(读["页们"]))：\n'
        '    打印("-- ", 页号 + 1)\n'
        '    遍历 行 在 读["页们"][页号]：\n'
        '        打印("  [", 行, "]")\n'
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-800:]
    assert "页数  2" in 出
    assert "读到的页数  2" in 出
    assert "[ Quarterly Report ]" in 出
    # 多行：\n 拆开，**空行不产生输出行**（但会把后面的行推下去）
    assert "[ one ]" in 出 and "[ two ]" in 出 and "[ four ]" in 出
    assert "[ page two ]" in 出, 出
    assert 出.count("[ " ) == 5, f"第 1 页 4 行 + 第 2 页 1 行：{出}"


def test_建出来的文件是合法PDF结构(tmp_path):
    """🔴 独立判据：xref 每条 20 字节，且**每个偏移真的指向 `N 0 obj`**。

    这是 PDF 最容易写错的地方 —— 偏移差一个字节，宽松的阅读器会「自动修复」
    照样打开，严格的不行。所以不能只测「能读回来」。
    """
    源 = (
        "导入 PDF\n令 文 = PDF.新建()\n"
        'PDF.写文本(文, "A", 10, 10)\nPDF.加页(文)\n'
        'PDF.写文本(文, "B", 10, 10, {"字体": "等宽", "粗体": 真})\n'
        'PDF.保存(文, "结构.pdf")\n'
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-600:]

    数据 = (tmp_path / "结构.pdf").read_bytes()
    assert 数据[:5] == b"%PDF-", "文件头不对"
    assert 数据.rstrip().endswith(b"%%EOF"), "文件尾没有 %%EOF"

    起 = int(re.search(rb"startxref\s+(\d+)", 数据).group(1))
    assert 数据[起:起 + 4] == b"xref", "startxref 没指向 xref"

    k = 数据.index(b"\n", 起)                     # xref
    k = 数据.index(b"\n", k + 1)                  # "0 N"
    总数 = int(数据[起:k].split(b"\n")[-1].split()[1])
    条 = 数据[k + 1:k + 1 + 20 * 总数]
    assert len(条) == 20 * 总数, "xref 块长度不是 20 × 条数"

    对象数 = 0
    for i in range(总数):
        e = 条[i * 20:(i + 1) * 20]
        assert len(e) == 20, f"第 {i} 条不是 20 字节：{e!r}"
        assert e[17:18] in (b"n", b"f"), f"第 {i} 条类型位不对：{e!r}"
        if e[17:18] == b"f":
            assert i == 0, "只有第 0 条该是空闲"
            continue
        对象数 += 1
        偏 = int(e[:10])
        期望 = b"%d 0 obj" % i
        assert 数据[偏:偏 + len(期望)] == 期望, (
            f"对象 {i} 的偏移 {偏} 指向 {数据[偏:偏 + 16]!r}")

    assert 对象数 >= 5, f"至少 1+1+2 页+2 内容=6 个对象，只有 {对象数}"


# ---------------------------------------------------------------------------
# 2. 读别人的
# ---------------------------------------------------------------------------

def test_读未压缩与FlateDecode(tmp_path):
    for 名, 压缩 in (("未压缩.pdf", False), ("压缩.pdf", True)):
        _造最小pdf(tmp_path / 名, b"BT /F1 12 Tf 1 0 0 1 72 700 Tm"
                                  b" (Hello PDF) Tj 0 -14 Td (second line) Tj ET",
                   压缩=压缩)
    源 = (
        "导入 PDF\n"
        '遍历 名 在 ["未压缩.pdf", "压缩.pdf"]：\n'
        '    打印("==", 名, PDF.页数(名))\n'
        "    令 读 = PDF.读文本(名)\n"
        '    遍历 行 在 读["页们"][0]：\n'
        '        打印("  [", 行, "]")\n'
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-800:]
    assert "== 未压缩.pdf 1" in 出
    assert "== 压缩.pdf 1" in 出
    assert 出.count("[ Hello PDF ]") == 2, "两种存法都要读出同样的字"
    assert 出.count("[ second line ]") == 2


def test_读带ToUnicode的子集字体(tmp_path):
    """子集字体里内容是**码**（`<010203>`），要靠 `ToUnicode` 才变回文字。"""
    正文 = b"BT /F1 12 Tf 1 0 0 1 72 700 Tm <010203> Tj 0 -14 Td <0405> Tj ET"
    cmap = (b"/CIDInit /ProcSet findresource begin\n12 dict begin begincmap\n"
            b"1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
            b"2 beginbfchar\n<01> <0048>\n<02> <0069>\nendbfchar\n"
            b"1 beginbfrange\n<03> <05> <0021>\nendbfrange\n"
            b"endcmap\nend\nend\n")
    页 = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842]"
          b" /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>")
    (tmp_path / "子集.pdf").write_bytes(_拼([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        页,
        _流(正文),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /ABCDEF+Custom"
        b" /ToUnicode 6 0 R >>",
        _流(zlib.compress(cmap), b" /Filter /FlateDecode"),
    ]))
    码, 出 = _跑("导入 PDF\n令 读 = PDF.读文本(\"子集.pdf\")\n"
                '遍历 行 在 读["页们"][0]：\n    打印("[", 行, "]")\n', tmp_path)
    assert 码 == 0, 出[-600:]
    assert '["' in 出 or "[ Hi! ]" in 出, 出
    assert "!" in 出 and '#' in 出, f"bfchar + bfrange 都要生效：{出}"


def test_读对象流里的页(tmp_path):
    """PDF 1.5+ 常把页对象压进 `/ObjStm` —— 不拆就「一页都找不到」。"""
    页 = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842]"
          b" /Resources << /Font << /F1 4 0 R >> >> /Contents 3 0 R >>")
    头 = b"5 0 "
    装 = 头 + 页
    (tmp_path / "对象流.pdf").write_bytes(_拼([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [5 0 R] /Count 1 >>",
        _流(b"BT /F1 12 Tf 1 0 0 1 72 700 Tm (inside object stream) Tj ET"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< >>",                        # 5 号占位：真身在对象流里
        _流(zlib.compress(装),
            b" /Type /ObjStm /N 1 /First %d /Filter /FlateDecode" % len(头)),
    ]))
    码, 出 = _跑("导入 PDF\n令 读 = PDF.读文本(\"对象流.pdf\")\n"
                '打印(长度(读["页们"][0]), " ", 读["页们"][0][0])\n', tmp_path)
    assert 码 == 0, 出[-600:]
    assert "inside object stream" in 出, 出


# ---------------------------------------------------------------------------
# 3. 明确不做的，要说清楚
# ---------------------------------------------------------------------------

def test_扫描件报没有文本层(tmp_path):
    页 = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842]"
          b" /Resources << /XObject << /Im1 5 0 R >> >> /Contents 4 0 R >>")
    (tmp_path / "扫描件.pdf").write_bytes(_拼([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        页,
        _流(b"q 595 0 0 842 0 0 cm /Im1 Do Q"),
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1"
        b" /ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>\nstream\n\x80\nendstream",
    ]))
    码, 出 = _跑('导入 PDF\n打印(PDF.读文本("扫描件.pdf"))\n', tmp_path)
    assert 码 != 0
    assert "没有文本层" in 出 and "扫描件" in 出, 出[-500:]


def test_中文报错并指路Word(tmp_path):
    源 = ('导入 PDF\n令 文 = PDF.新建()\nPDF.写文本(文, "季度报告", 72, 72)\n')
    码, 出 = _跑(源, tmp_path)
    assert 码 != 0
    assert "「季」" in 出 or "季度" in 出, 出[-500:]
    assert "Word" in 出, "报错要指路：中文报告走 Word"
    assert "标准字体" in 出


def test_参数边界都是中文错(tmp_path):
    for 源, 关键词 in [
        ('令 文 = PDF.新建()\nPDF.写文本(文, "x", 1, 1, {"字体": "微软雅黑"})\n', "不认识的字体"),
        ('令 文 = PDF.新建()\nPDF.写文本(文, "x", 1, 1, {"颜色": [300, 0, 0]})\n', "0–255"),
        ('令 文 = PDF.新建()\nPDF.画矩形(文, 1, 1, 0, 10)\n', "宽和高要大于 0"),
        ('令 文 = PDF.新建()\nPDF.保存(文, "出.doc")\n', ".pdf 结尾"),
        ('令 文 = PDF.新建()\nPDF.写文本(文, "a\\tb", 1, 1)\n', "制表符"),
        ('令 文 = 5\nPDF.加页(文)\n', "要一个 PDF 文档"),
        ("打印(PDF.页数(\"不存在.pdf\"))\n", "找不到文件"),
        ('导入 表格\n令 文 = PDF.新建()\nPDF.写文本(文, "x", 1, 1, 3)\n', "要给一个字典"),
    ]:
        码, 出 = _跑("导入 PDF\n" + 源, tmp_path, "边界.jsh")
        assert 码 != 0, f"这条应当报错：{源}"
        assert 关键词 in 出, f"「{关键词}」没出现在：{出[-400:]}"


# ---------------------------------------------------------------------------
# 4. 三宿主
# ---------------------------------------------------------------------------

def _三宿主(源: str, tmp_path: Path) -> tuple:
    import importlib.util

    spec = importlib.util.spec_from_file_location("ce", str(ROOT / "tools" / "check_engines.py"))
    ce = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ce)
    if ce._node_exe() is None:
        pytest.skip("没有 node")

    脚本 = tmp_path / "三.jsh"
    脚本.write_text(源, encoding="utf-8", newline="\n")

    r = subprocess.run([str(rust_exe_path()), str(脚本)], capture_output=True,
                       timeout=180, cwd=str(tmp_path))
    rs = (r.stdout + r.stderr).decode("utf-8", "replace")

    p = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", str(脚本)],
                       capture_output=True, timeout=180, cwd=str(tmp_path))
    py = (p.stdout + p.stderr).decode("utf-8", "replace")

    nd, nd错 = ce._run_node_vm(源, str(脚本), tmp_path)
    return py, rs, nd, nd错


def test_三宿主写出的文件逐字节一致(tmp_path):
    """三个宿主写同一个文档，**字节必须完全相同** —— 这才叫「同行为」。"""
    import importlib.util

    源 = (
        "导入 PDF\n"
        "令 文 = PDF.新建()\n"
        'PDF.写文本(文, "Byte check", 72, 72, {"字号": 18, "粗体": 真, "颜色": [200, 30, 30]})\n'
        'PDF.写文本(文, "a\\nb", 72, 120, {"字体": "等宽", "字号": 10})\n'
        'PDF.画线(文, 60, 200, 500, 200, {"线宽": 1.5, "颜色": [0, 0, 255]})\n'
        'PDF.画矩形(文, 60, 220, 120.5, 60.25, {"填充": [240, 240, 240]})\n'
        "PDF.加页(文)\n"
        'PDF.写文本(文, "page 2", 100, 100.5)\n'
        'PDF.保存(文, "字节.pdf")\n'
        '打印(PDF.页数("字节.pdf"))\n'
    )
    脚本 = tmp_path / "字节.jsh"
    脚本.write_text(源, encoding="utf-8", newline="\n")

    份们 = {}
    for 名, 命令 in (
        ("py", [sys.executable, "-m", "oracle.jishi.cli", str(脚本)]),
        ("rs", [str(rust_exe_path()), str(脚本)]),
    ):
        r = subprocess.run(命令, capture_output=True, timeout=180, cwd=str(tmp_path))
        assert r.returncode == 0, (r.stdout + r.stderr).decode("utf-8", "replace")[-500:]
        份们[名] = (tmp_path / "字节.pdf").read_bytes()
        (tmp_path / "字节.pdf").unlink()

    spec = importlib.util.spec_from_file_location("ce", str(ROOT / "tools" / "check_engines.py"))
    ce = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ce)
    if ce._node_exe() is None:
        pytest.skip("没有 node")
    nd, 错 = ce._run_node_vm(源, str(脚本), tmp_path)
    assert (tmp_path / "字节.pdf").is_file(), f"Node 没写出文件：{错[:300]}"
    份们["js"] = (tmp_path / "字节.pdf").read_bytes()

    assert 份们["py"] == 份们["rs"], "Rust 与 Python 写出的字节不同"
    assert 份们["py"] == 份们["js"], "Node 与 Python 写出的字节不同"
    assert len(份们["py"]) > 800, "文件太小，可能没写内容"


def test_三宿主读同一份PDF(tmp_path):
    _造最小pdf(tmp_path / "共读.pdf", b"BT /F1 12 Tf 1 0 0 1 72 700 Tm"
                                      b" (same file) Tj 0 -14 Td (read by all) Tj ET",
               压缩=True)
    源 = (
        "导入 PDF\n"
        '令 读 = PDF.读文本("共读.pdf")\n'
        '打印("页数 ", 读["页数"])\n'
        '遍历 行 在 读["页们"][0]：\n    打印("[", 行, "]")\n'
        "令 文 = PDF.新建()\n"
        'PDF.写文本(文, "round trip", 72, 72)\n'
        'PDF.保存(文, "回.pdf")\n'
        '打印(PDF.读文本("回.pdf")["页们"][0][0])\n'
    )
    py, rs, nd, 错 = _三宿主(源, tmp_path)
    assert rs == py, f"Rust 与 Python 不一致：\n--- py ---\n{py}\n--- rust ---\n{rs}"
    assert nd == py, f"Node 与 Python 不一致：\n--- py ---\n{py}\n--- node ---\n{nd}\n{错[:400]}"
