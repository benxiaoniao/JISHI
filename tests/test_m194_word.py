# -*- coding: utf-8 -*-
"""M194 · `Word`（.docx 读写）8 个函数。

## 判据分三层

1. **自写自读**：写一份（标题/段落/表格/分页）再读回来，内容与样式都对；
2. **读别人的**：读一份**独立造出来**的最小 `.docx`（deflate 压缩、合并单元格、
   页眉页脚、XML 实体）—— 证明不是「只能读自己写的」；
3. **模板填充**：`{{键}}` 被替换、**没给的键留着**（静默清空最危险）。

## 为什么还要比「三宿主」

`Word` 在 Python 走 `zipfile`+`ElementTree`、Rust 走自写的 `zip.rs`+`xml.rs`、
Node 走 `node:zlib`+自己拼的 XML 扫描。三条完全不同的路，输出必须一样。
"""

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                        # noqa: E402

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


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


def _造docx(路径: Path, 正文: str, 额外=None) -> None:
    """独立造一份最小 .docx（**deflate 压缩**，与真实 Word 一样）。"""
    with zipfile.ZipFile(路径, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships/>')
        z.writestr("word/document.xml", 正文)
        for 名, 内容 in (额外 or {}).items():
            z.writestr(名, 内容)


def test_自写自读(tmp_path):
    源 = (
        "导入 Word\n导入 表格\n"
        "令 文 = Word.新建()\n"
        'Word.加标题(文, "季度报告", 1)\n'
        'Word.加段落(文, "同比增长 12%。", {"粗体": 真, "字号": 14, "对齐": "居中"})\n'
        'Word.加表格(文, 表格.从记录([{"科目": "语文", "分数": 90}, '
        '{"科目": "数学", "分数": 85}]))\n'
        "Word.加分页(文)\n"
        'Word.加段落(文, "第二页")\n'
        'Word.保存(文, "报告.docx")\n'
        '令 读 = Word.读文档("报告.docx")\n'
        '打印("段数 ", 长度(读["段落"]))\n'
        '打印("样式0 ", 读["段落"][0]["样式"])\n'
        '打印("文本0 ", 读["段落"][0]["文本"])\n'
        '打印("表格数 ", 长度(读["表格"]))\n'
        '打印(表格.转TSV(读["表格"][0]))\n'
        '打印("图片 ", 读["图片数"])\n'
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-800:]
    assert "段数  4" in 出, 出          # 标题 + 正文 + 分页产生的空段 + 第二页
    assert "样式0  标题1" in 出, 出
    assert "文本0  季度报告" in 出, 出
    assert "表格数  1" in 出, 出
    assert "科目\t分数" in 出 and "语文\t90" in 出, 出
    assert "图片  0" in 出, 出


def test_读外部造的docx(tmp_path):
    """deflate + 合并单元格 + 多 run + 实体 + 页眉页脚。"""
    正文 = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W}"><w:body>'
        '<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr><w:r><w:t>外部文档</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>A&amp;B 公司</w:t></w:r><w:r><w:t xml:space="preserve"> 报表</w:t></w:r></w:p>'
        '<w:tbl><w:tr>'
        '<w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>合并表头</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>第三列</w:t></w:r></w:p></w:tc></w:tr>'
        '<w:tr><w:tc><w:p><w:r><w:t>1</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>2</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>3</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        '<w:p><w:r><w:drawing/></w:r></w:p>'
        '<w:sectPr/></w:body></w:document>'
    )
    页眉 = f'<?xml version="1.0"?><w:hdr xmlns:w="{W}"><w:p><w:r><w:t>公司机密</w:t></w:r></w:p></w:hdr>'
    页脚 = f'<?xml version="1.0"?><w:ftr xmlns:w="{W}"><w:p><w:r><w:t>第 1 页</w:t></w:r></w:p></w:ftr>'
    _造docx(tmp_path / "外部.docx", 正文,
            {"word/header1.xml": 页眉, "word/footer1.xml": 页脚})

    源 = (
        "导入 Word\n导入 表格\n"
        '令 读 = Word.读文档("外部.docx")\n'
        '打印("[", 读["段落"][0]["样式"], "]", 读["段落"][0]["文本"])\n'
        '打印("[", 读["段落"][1]["样式"], "]", 读["段落"][1]["文本"])\n'
        '打印("表格数 ", 长度(读["表格"]))\n'
        '打印(表格.转TSV(读["表格"][0]))\n'
        '打印("图片 ", 读["图片数"])\n'
        '打印("页眉 ", 读["页眉"], " 页脚 ", 读["页脚"])\n'
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-800:]
    assert "[ 标题 ] 外部文档" in 出, 出        # Title → 标题
    assert "A&B 公司 报表" in 出, 出           # 实体还原 + 多 run 合并
    assert "合并表头\t合并表头2\t第三列" in 出, 出   # gridSpan 展开 + 重名补后缀
    assert "图片  1" in 出, 出
    assert "公司机密" in 出 and "第 1 页" in 出, 出


def test_模板填充(tmp_path):
    """替换给到的、**留着没给的**（静默清空最危险）。"""
    正文 = (
        f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>'
        '<w:p><w:r><w:t>甲方：{{甲方}}</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>日期：{{日期}}</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>没给的：{{缺项}}</w:t></w:r></w:p>'
        '<w:sectPr/></w:body></w:document>'
    )
    _造docx(tmp_path / "模板.docx", 正文)
    源 = (
        "导入 Word\n"
        'Word.填充模板("模板.docx", "合同.docx", {"甲方": "张三", "日期": "2026-11-12"})\n'
        '令 读 = Word.读文档("合同.docx")\n'
        '遍历 段 在 读["段落"]：\n'
        '    打印(段["文本"])\n'
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-800:]
    assert "甲方：张三" in 出, 出
    assert "日期：2026-11-12" in 出, 出
    assert "没给的：{{缺项}}" in 出, f"没给的键应当留着：{出!r}"


def test_不是docx要报中文错(tmp_path):
    (tmp_path / "假的.docx").write_bytes(b"this is not a zip")
    (tmp_path / "x.txt").write_bytes(b"hi")
    源 = (
        "导入 Word\n"
        'Word.读文档("假的.docx")\n'
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 != 0, "不是 zip 必须报错"
    assert "不是 .docx" in 出, 出[-400:]

    # 是 zip 但没有 document.xml（比如 xlsx）
    有zip = tmp_path / "别的.docx"
    with zipfile.ZipFile(有zip, "w") as z:
        z.writestr("xl/workbook.xml", "<a/>")
    源2 = "导入 Word\nWord.读文档(\"别的.docx\")\n"
    码2, 出2 = _跑(源2, tmp_path, "t2.jsh")
    assert 码2 != 0
    assert "没有 word/document.xml" in 出2, 出2[-400:]


def test_参数边界(tmp_path):
    源 = (
        "导入 Word\n"
        "令 文 = Word.新建()\n"
        'Word.加标题(文, "x", 9)\n'
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 != 0 and "1 / 2 / 3" in 出, 出[-400:]

    源2 = '导入 Word\n令 文 = Word.新建()\nWord.保存(文, "x.doc")\n'
    码2, 出2 = _跑(源2, tmp_path, "t2.jsh")
    assert 码2 != 0 and ".docx" in 出2, 出2[-400:]

    源3 = (
        "导入 Word\n"
        "令 文 = Word.新建()\n"
        'Word.加段落(文, "x", {"颜色": "红"})\n'
    )
    码3, 出3 = _跑(源3, tmp_path, "t3.jsh")
    assert 码3 != 0 and "没有「颜色」" in 出3, 出3[-400:]


def test_三宿主一致(tmp_path):
    """Python / Rust / Node 跑同一段，输出必须一模一样。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("ce", str(ROOT / "tools" / "check_engines.py"))
    ce = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ce)
    if ce._node_exe() is None:
        pytest.skip("没有 node")

    正文 = (
        f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>'
        '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>小节</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>正文&#160;文字</w:t></w:r></w:p>'
        '<w:sectPr/></w:body></w:document>'
    )
    _造docx(tmp_path / "三.docx", 正文)
    源 = (
        "导入 Word\n导入 表格\n"
        '令 读 = Word.读文档("三.docx")\n'
        '打印("段数 ", 长度(读["段落"]))\n'
        '遍历 段 在 读["段落"]：\n'
        '    打印("[", 段["样式"], "]", 段["文本"])\n'
        "令 文 = Word.新建()\n"
        'Word.加标题(文, "报告", 2)\n'
        'Word.加段落(文, "甲 & 乙", {"斜体": 真, "对齐": "右", "字号": 12})\n'
        'Word.加表格(文, 表格.从记录([{"a": 1, "b": 2}]))\n'
        'Word.保存(文, "出.docx")\n'
        '令 回 = Word.读文档("出.docx")\n'
        '打印("回读段数 ", 长度(回["段落"]))\n'
        '打印(表格.转TSV(回["表格"][0]))\n'
    )
    脚本 = tmp_path / "三.jsh"
    脚本.write_text(源, encoding="utf-8", newline="\n")

    r = subprocess.run([str(rust_exe_path()), str(脚本)], capture_output=True,
                       timeout=180, cwd=str(tmp_path))
    rs = (r.stdout + r.stderr).decode("utf-8", "replace")

    p = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", str(脚本)],
                       capture_output=True, timeout=180, cwd=str(tmp_path))
    py = (p.stdout + p.stderr).decode("utf-8", "replace")

    nd, nd错 = ce._run_node_vm(源, str(脚本), tmp_path)

    assert rs == py, f"Rust 与 Python 不一致：\n--- py ---\n{py}\n--- rust ---\n{rs}"
    assert nd == py, f"Node 与 Python 不一致：\n--- py ---\n{py}\n--- node ---\n{nd}\n{nd错[:400]}"
