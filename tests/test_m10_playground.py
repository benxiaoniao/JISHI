# -*- coding: utf-8 -*-
"""M10.3 / R8.2：浏览器试玩页（Playground）的**生成**判据。

R8.2 把引擎从 **Pyodide（浏览器里的 Python 实现）**换成 **wasm**（R8.1 的产物）。
本文件只管**生成出来的 HTML**长什么样 —— 「页面真能跑」由
`tests/test_m97_wasm_playground.py` 在 Node 里用 DOM 桩验。
"""

import sys
from pathlib import Path

sys.path.insert(0, ".")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_playground  # noqa: E402


def _generate(tmp_path, monkeypatch, name="playground.html") -> str:
    monkeypatch.setattr(build_playground, "OUT", tmp_path / name)
    build_playground.build()
    return (tmp_path / name).read_text(encoding="utf-8")


def test_页面不再依赖_pyodide(tmp_path, monkeypatch):
    """R8.2 的核心：页面里**一个 Python 字节都不该有**。

    以前这条页面要把 `interpreter/parser/tokenizer/runtime/errors/ast_nodes`
    六个模块的源码整个内嵌进去、再靠 CDN 拉 Pyodide 来跑。现在只引用 wasm。
    """
    html = _generate(tmp_path, monkeypatch)
    for bad in ("pyodide", "Pyodide", "loadPyodide", "run_source",
                "__jishi_code", "oracle/jishi/interpreter.py", "oracle/jishi/stdlib/"):
        assert bad not in html, f"页面里还残留「{bad}」"


def test_页面引用_wasm三件(tmp_path, monkeypatch):
    html = _generate(tmp_path, monkeypatch)
    # 胶水是外链（**源码唯一**在 wasm/jishi-wasm.js，不内嵌一份拷贝）
    assert 'src="jishi-wasm.js"' in html
    # 引擎产物相对引用
    assert "jishi_wasm.wasm" in html
    # 走的是 R8.1 的 shim API
    assert "JishiWasm.load" in html
    assert "engine.run(" in html
    # 只有两个 script：外链胶水 + 内联页面逻辑
    assert html.count("<script") == 2, html.count("<script")


def test_页面是完整_html(tmp_path, monkeypatch):
    html = _generate(tmp_path, monkeypatch)
    assert html.startswith("<!DOCTYPE html>")
    assert html.rstrip().endswith("</html>")
    assert '<meta charset="utf-8">' in html
    for eid in ('id="code"', 'id="run"', 'id="out"', 'id="status"'):
        assert eid in html, eid


def test_页面体积在网页量级(tmp_path, monkeypatch):
    """换 wasm 的收益之一：页面从 432KB 缩到几 KB。

    这条**不设死上限**（版面文字会长），只保证「不再内嵌整个实现」这个量级。
    """
    html = _generate(tmp_path, monkeypatch)
    n = len(html.encode("utf-8"))
    assert n < 64 * 1024, f"页面 {n:,} 字节 —— 是不是又把什么内嵌进去了？"


def test_示例程序被原样嵌进页面(tmp_path, monkeypatch):
    """预置示例要和 `SAMPLE` 一致（HTML 转义后）—— 别让它悄悄漂移。"""
    html = _generate(tmp_path, monkeypatch)
    first = build_playground.SAMPLE.splitlines()[0]
    assert first in html
    assert "斐波那契第 10 项" in html


def test_缺产物时明确报警(tmp_path, monkeypatch, capsys):
    """wasm 产物不在时，构建脚本要**说清楚**，而不是默默生成一个跑不起来的页面。"""
    monkeypatch.setattr(build_playground, "WASM", tmp_path / "没有这个文件.wasm")
    monkeypatch.setattr(build_playground, "OUT", tmp_path / "pg.html")
    build_playground.build()
    out = capsys.readouterr().out
    assert "jishi_wasm.wasm" in out and "build_wasm.py" in out, out
