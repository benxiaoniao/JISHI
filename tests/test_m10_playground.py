# -*- coding: utf-8 -*-
"""M10.3 Playground 测试：浏览器试玩页生成。"""

import sys
from pathlib import Path

sys.path.insert(0, ".")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_playground import _collect, build  # noqa: E402


def test_collect_core_modules():
    src = _collect()
    # 树遍历解释器核心模块必须在
    for m in ("jishi/interpreter.py", "jishi/parser.py", "jishi/tokenizer.py",
              "jishi/runtime.py", "jishi/errors.py", "jishi/ast_nodes.py"):
        assert m in src, f"缺少 {m}"
    # 标准库模块在内
    assert any(k.startswith("jishi/stdlib/") for k in src)
    # 不含 C 扩展依赖
    assert not any("cvm_bind" in k for k in src)


def test_build_generates_html(tmp_path, monkeypatch):
    import build_playground
    monkeypatch.setattr(build_playground, "OUT",
                        tmp_path / "playground.html")
    build()
    html = (tmp_path / "playground.html").read_text(encoding="utf-8")
    assert "pyodide" in html
    assert "run_source" in html
    assert "__jishi_code" in html
    assert "loadPyodide" in html


def test_build_contains_chinese_modules(tmp_path, monkeypatch):
    import build_playground
    monkeypatch.setattr(build_playground, "OUT",
                        tmp_path / "pg.html")
    build()
    html = (tmp_path / "pg.html").read_text(encoding="utf-8")
    # 中文标准库模块名内嵌（json/日期 等）
    assert "jishi/stdlib/json.py" in html
    assert "日期" in html
