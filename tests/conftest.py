# -*- coding: utf-8 -*-
"""黄金文件测试：cases/*.jsh 对照 *.out；error_cases/*.jsh 对照 *.err。

- 黄金测试：执行源码，stdout 必须与 .out 完全一致（自动收集）
- 错误快照：执行必须抛 JishiError，渲染文本必须包含 .err 中的关键子串
"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jishi.interpreter import run_source
from jishi.errors import JishiError

CASES = Path(__file__).parent / "cases"
ERROR_CASES = Path(__file__).parent / "error_cases"


def _list_cases(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    return sorted(p.name for p in directory.glob("*.jsh"))


@pytest.mark.parametrize("jsh", _list_cases(CASES))
def test_golden(jsh):
    src = (CASES / jsh).read_text(encoding="utf-8")
    expected = (CASES / jsh).with_suffix(".out").read_text(encoding="utf-8")
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            run_source(src, filename=jsh)
    except Exception as e:
        pytest.fail(f"执行失败：{e}")
    assert out.getvalue() == expected, (
        f"输出不一致\n期望：\n{expected!r}\n实际：\n{out.getvalue()!r}")


@pytest.mark.parametrize("jsh", _list_cases(ERROR_CASES))
def test_error_snapshot(jsh):
    src = (ERROR_CASES / jsh).read_text(encoding="utf-8")
    expected = (ERROR_CASES / jsh).with_suffix(".err").read_text(
        encoding="utf-8").strip()
    with pytest.raises(JishiError) as ei:
        run_source(src, filename=jsh)
    rendered = str(ei.value)
    assert expected in rendered, (
        f"错误消息不匹配\n期望包含：\n{expected}\n实际：\n{rendered}")
