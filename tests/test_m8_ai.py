# -*- coding: utf-8 -*-
"""M8 AI 原生工具链测试：语言卡 / 语言规格 / 结构化报错。"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from jishi import ai
from jishi.errors import JishiError
from jishi.interpreter import run_source


def test_lang_spec_structure():
    spec = ai.build_lang_spec()
    assert spec["version"] == "0.1.0"
    assert "令" in spec["keywords"]
    assert "函数" in spec["keywords"]
    assert spec["operators"]["binary"]      # 非空
    assert any(b["name"] == "打印" for b in spec["builtins"])
    assert any(m["module"] == "随机" for m in spec["stdlib"])
    assert "值错误" in spec["exceptions"]


def test_lang_spec_json_valid():
    raw = ai.lang_spec_json()
    spec = json.loads(raw)
    assert spec["version"] == "0.1.0"
    assert len(spec["content_hash"]) == 8


def test_content_hash_stable():
    h1 = ai.content_hash(ai.build_lang_spec())
    h2 = ai.content_hash(ai.build_lang_spec())
    assert h1 == h2 and len(h1) == 8


def test_ai_card_contains_essentials():
    card = ai.render_ai_card()
    assert "语言卡" in card
    assert "函数" in card
    assert "打印" in card
    assert "def" in card            # 错误对照表
    assert "遍历" in card
    assert "推导式" in card


def test_error_to_json_with_fix():
    err = JishiError("测试", line=3, col=5, hint="改这里",
                     fix={"old": "def", "new": "函数"})
    out = ai.error_to_json(err)
    assert out["code"] == "E0000"
    assert out["line"] == 3 and out["col"] == 5
    assert out["fix"] == {"old": "def", "new": "函数", "line": 3, "col": 5}


def test_error_fix_on_en_keyword():
    with pytest.raises(JishiError) as ei:
        run_source("def f():", "<t>")
    assert ei.value.fix == {"old": "def", "new": "函数"}


def test_error_fix_on_en_builtin():
    with pytest.raises(JishiError) as ei:
        run_source("print(1)", "<t>")
    assert ei.value.fix == {"old": "print", "new": "打印"}


def _run_cli(argv):
    """调用 cli.main，返回 (exit_code, stdout, stderr)。"""
    import io
    from contextlib import redirect_stdout, redirect_stderr
    from jishi import cli
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def test_cli_ai_card():
    code, out, _ = _run_cli(["--ai-card"])
    assert code == 0
    assert "语言卡" in out


def test_cli_lang_spec():
    code, out, _ = _run_cli(["--lang-spec"])
    assert code == 0
    spec = json.loads(out)
    assert "keywords" in spec


def test_cli_json_errors(tmp_path):
    f = tmp_path / "bad.jsh"
    f.write_text("def f():\n    return 1\n", encoding="utf-8")
    code, _, err = _run_cli([str(f), "--json-errors"])
    assert code == 1
    payload = json.loads(err)
    assert payload["ok"] is False
    assert payload["errors"][0]["fix"]["old"] == "def"
