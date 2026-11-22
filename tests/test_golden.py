# -*- coding: utf-8 -*-
"""语料输出的**跨平台可比性**检查（`test_golden` / `test_error_snapshot` 已退役）。

## 那两条去哪了（M138，2026-10-26）

`test_golden`（43 条）与 `test_error_snapshot`（80 条）**搬去 `tools/自测.jsh` 了** ——
那是 0.2.8 的「用基石测基石」：**每个用例一个子进程**、比 stdout / stderr / 退出码，
而且**在 CI 上跑**（`.github/workflows/ci.yml` 的「基石自测」两步，被测分别是
Python 参考实现与构建出来的 `jishi-rs`）。

⚠️ **顺序是有讲究的**：必须先让运行器进 CI，再删这里的两条 ——
反过来就等于把这 126 条用例**从 CI 上抹掉**（CI 只跑 pytest）。

📌 **删了不等于没人守**：`tests/test_m138_runner_summary.py` 仍会**在 pytest 里**
把整批语料交给运行器跑一遍，并独立数一遍语料与汇总对账 ——
所以本地 `pytest` 照样能发现「某条语料的输出变了」。

## 留下的这条是另一件事

它查的是「**同一份 `.out` 在别的平台上会不会对不上**」——
跟「哪个执行器跑」无关，运行器（只在跑的那台机器上比）也管不了它。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CASES = Path(__file__).parent / "cases"


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
