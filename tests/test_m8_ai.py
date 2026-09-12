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
    assert spec["version"] == ai.VERSION
    assert "令" in spec["keywords"]
    assert "函数" in spec["keywords"]
    assert spec["operators"]["binary"]      # 非空
    assert any(b["name"] == "打印" for b in spec["builtins"])
    assert any(m["module"] == "随机" for m in spec["stdlib"])
    assert "值错误" in spec["exceptions"]


def test_lang_spec_json_valid():
    raw = ai.lang_spec_json()
    spec = json.loads(raw)
    assert spec["version"] == ai.VERSION
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


# -- M8.6 评测集自测 ---------------------------------------------------------

def test_jiishi_eval_all_answers_pass():
    """评测集标准答案必须 100% 通过（语法改动不得使通过率下降）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "jiishi_eval", "bench/ai-eval/eval.py")
    jeval = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(jeval)
    assert len(jeval.TASKS) >= 50
    for t in jeval.TASKS:
        out = jeval.run_code(t["answer"])
        assert out == t["stdout"], (
            f"{t['id']} 答案输出不匹配\n"
            f"期望 {t['stdout']!r}\n实际 {out!r}")


# -- M12 评测驱动闭环：fix 提取 + 自愈 ---------------------------------------

def _load_jeval():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "jiishi_eval_m12", "bench/ai-eval/eval.py")
    jeval = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(jeval)
    return jeval


def test_eval_run_code_result_extracts_fix():
    jeval = _load_jeval()
    ok, _out, err = jeval.run_code_result("def 计算(x):\n    返回 x * 2")
    assert ok is False
    assert err["code"] == "E0201"
    assert err["fix"] == {"old": "def", "new": "函数"}


def test_eval_apply_fix_replaces_keyword():
    jeval = _load_jeval()
    src = "def 计算(x):\n    返回 x * 2"
    _ok, _out, err = jeval.run_code_result(src)
    fixed = jeval._apply_fix(src, err)
    assert fixed == "函数 计算(x):\n    返回 x * 2"


def test_eval_heal_loop_two_rounds():
    """def + return 两个英文关键字，两轮自愈后通过。"""
    jeval = _load_jeval()
    src = "def 计算(x):\n    return x * 2\n打印(计算(21))"
    cur = src
    for _ in range(3):
        ok, out, err = jeval.run_code_result(cur)
        if ok:
            break
        nxt = jeval._apply_fix(cur, err)
        if nxt is None or nxt == cur:
            break
        cur = nxt
    assert jeval.run_code_result(cur)[1] == "42\n"


def test_eval_apply_fix_no_false_positive():
    """无 fix 的错误不应触发替换。"""
    jeval = _load_jeval()
    ok, _out, err = jeval.run_code_result("令 x = 1 / 0")
    assert ok is False
    assert jeval._apply_fix("令 x = 1 / 0", err) is None
