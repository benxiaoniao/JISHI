# -*- coding: utf-8 -*-
"""黄金文件测试与错误快照测试（自动收集 cases/ 与 error_cases/）。"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oracle.jishi.interpreter import run_source
from oracle.jishi.errors import JishiError

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


# ---------------------------------------------------------------------------
# 语料输出必须**跨平台可比**
# ---------------------------------------------------------------------------

#: 反斜杠后面允许出现的字符 —— 也就是各语言 `repr` 会用的转义引导符。
#: 其它字符跟在反斜杠后面，几乎只可能是「Windows 路径分隔符漏进了输出」。
_合法转义 = set("\\ntr'\"abfvxXuU01234567")


def test_语料输出里没有平台相关的路径分隔符():
    """🔴 为什么有这一条（2026-10-14，M111 发版时踩到）：

    `30_标准库_M108.jsh` 打印了 `路径.按模式找` 的结果，而 `路径` 给的相对路径用
    **本机分隔符** —— 在 Windows 上冻结出来的 `.out` 是 `['子\\报告C.md']`，
    到了 Linux / macOS 就变成 `['子/报告C.md']`，**CI 上必失败**。

    ⚠️ 它躲过了本地所有检查：`check_engines` 五引擎对拍、`check_walk`、conformance
    全都**只在跑测试的那一台机器上比**，「五台引擎一致」与「跨平台一致」是两件事。
    ⇒ 这条判据**在任何宿主上都能跑**，把「输出里漏了本机分隔符」当场抓出来。

    规则：`.out` 里允许出现转义序列（`\\` / `\\n` / `\\t` / `\\xNN` / `\\uNNNN` …），
    但**反斜杠后面跟别的字符**就是可疑的 —— 那多半是路径分隔符。
    ⚠️ 真要打印「本机路径」，先在语料里归一成 `/`（M108 语料就是这么做的）。
    """
    坏 = []
    for out in sorted(CASES.glob("*.out")):
        text = out.read_text(encoding="utf-8")
        for i, ch in enumerate(text):
            if ch != "\\":
                continue
            后 = text[i + 1] if i + 1 < len(text) else ""
            if 后 not in _合法转义:
                行 = text.count("\n", 0, i) + 1
                坏.append(f"{out.name}:{行} 反斜杠后面是 {后!r}")
    assert not 坏, (
        "这些语料的输出里出现了疑似「本机路径分隔符」的反斜杠 —— "
        "同一个语料在 Windows 与 Linux/macOS 上会给出不同字节：\n  - "
        + "\n  - ".join(坏)
        + "\n⇒ 语料里先把路径归一成 `/` 再打印（可照 30_标准库_M108.jsh 的做法）。")
