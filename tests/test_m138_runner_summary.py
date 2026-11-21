# -*- coding: utf-8 -*-
"""M138 ③：**钉住运行器的汇总**（不是钉某条用例，是钉「它算得对不对」）。

## 为什么单列这一条

`tools/自测.jsh` 是 0.2.8 的「用基石测基石」。自测有个天生盲区：
**运行器自己有 bug 时，结果可能全绿而不真** —— 比如它把某条用例数漏了、
或者把「跳过」算进「通过」，屏幕上照样是一行漂亮的 `✅ 全部通过`。

⇒ 判据不能只看退出码，要**从外面独立数一遍**，再跟它报的数对账：

| 运行器报的 | 独立数出来的 |
|---|---|
| `总` | 两个用例目录下的 `*.jsh` 个数 |
| `跳过` | 没有 `.out` / `.err` 的那些 |
| `通过` + `豁免` | 有期望的那些（失败为 0 时） |

## 另一条：汇总行的格式是**契约**

CI 的「自测失败摘要」、`jishi 自测`、以及人手一眼扫，都靠这一行的正则。
所以格式也钉住 —— 改了它就得来看这里一眼（和 `README` 里那几个字面串同理）。

⚠️ 这条跑**全量语料**（126 条子进程），所以**不进并行节**的 CPU 预算 ——
但它有 Rust 就用 Rust 跑（~3 秒），没构建才退 Python（~30 秒）。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
RUNNER = ROOT / "tools" / "自测.jsh"
RUST = ROOT / "rust" / "target" / "release" / ("jishi-rs.exe" if __import__("os").name == "nt" else "jishi-rs")

#: 汇总行的**唯一格式**（`tools/自测.jsh` 末尾那一句）
汇总正则 = re.compile(
    r"^汇总 · (?P<总>\d+) 条：通过 (?P<通过>\d+) · 失败 (?P<失败>\d+)"
    r" · 跳过 (?P<跳过>\d+) · 豁免 (?P<豁免>\d+) · 用时 (?P<用时>\d+) 毫秒$")

用例目录们 = [ROOT / "tests" / "cases", ROOT / "tests" / "error_cases"]


def _数语料() -> dict[str, int]:
    """**独立**数一遍（不看运行器的任何输出）。"""
    总 = 跳过 = 0
    for 目录 in 用例目录们:
        for 源 in sorted(目录.glob("*.jsh")):
            总 += 1
            基 = 源.with_suffix("")
            if not (基.with_suffix(".out").is_file() or 基.with_suffix(".err").is_file()):
                跳过 += 1
    return {"总": 总, "跳过": 跳过}


def _跑(参数=None, 用例目录=None):
    命令 = [PY, "-m", "oracle.jishi.cli", str(RUNNER)]
    if 用例目录 is not None:
        命令 += ["--用例", str(用例目录)]
    else:
        命令 += ["--被测"] + ([str(RUST)] if RUST.is_file() else [PY, "-m", "oracle.jishi.cli"])
        命令 += ["--标签", "jishi-rs" if RUST.is_file() else "python"]
    r = subprocess.run(命令, cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    return r


def _取汇总(文: str) -> re.Match:
    行们 = [行 for 行 in 文.splitlines() if 行.startswith("汇总 · ")]
    assert len(行们) == 1, f"汇总行该正好一条，实际 {len(行们)} 条：\n{文[-800:]}"
    匹配 = 汇总正则.match(行们[0].strip())
    assert 匹配, f"汇总行格式变了（下游靠这条正则）：{行们[0]!r}"
    return 匹配


def test_汇总的算术对得上独立数出来的语料():
    期望 = _数语料()
    r = _跑()
    汇 = _取汇总(r.stdout)
    总 = int(汇["总"])
    通过, 失败 = int(汇["通过"]), int(汇["失败"])
    跳过, 豁免 = int(汇["跳过"]), int(汇["豁免"])

    assert 总 == 期望["总"], f"运行器说 {总} 条，独立数是 {期望['总']} 条"
    assert 跳过 == 期望["跳过"], f"没期望的该是 {期望['跳过']} 条，它说 {跳过} 条"
    # 🔴 这一行是**核心**：四类加起来必须正好等于总数，不能有「谁都没算」的空隙
    assert 通过 + 失败 + 跳过 + 豁免 == 总, (
        f"算术不平：通过 {通过} + 失败 {失败} + 跳过 {跳过} + 豁免 {豁免} != {总}")
    assert 失败 == 0, r.stdout[-1500:]
    assert 通过 + 豁免 == 总 - 跳过, "有期望的用例必须落在「通过」或「豁免」里"
    assert r.returncode == 0, "全过就该退 0"


def test_跳过的那几条确实没期望文件():
    """「跳过」不能变成「悄悄放过」的暗门 —— 它数的必须**正好是**没期望文件的那些。"""
    期望 = _数语料()
    if 期望["跳过"] == 0:
        pytest.skip("当前语料里每条都有期望文件")
    r = _跑()
    汇 = _取汇总(r.stdout)
    # 明细里该逐条点名（⏭ 那一行），条数要与汇总里的「跳过」一致
    明细 = [行 for 行 in r.stdout.splitlines() if 行.startswith("  ⏭")]
    assert len(明细) == int(汇["跳过"]), f"汇总说跳过 {汇['跳过']}，明细却有 {len(明细)} 条"


def test_有失败时也算得对(tmp_path):
    """造 2 条（1 过 1 败）：汇总必须是 `通过 1 · 失败 1 · 跳过 0`，且退 1。

    只验「全绿时算得对」是不够的 —— **失败路径**才是这行数字最需要准的时候。
    """
    目 = tmp_path / "小"
    目.mkdir()
    (目 / "好.jsh").write_text("打印(1)\n", encoding="utf-8", newline="\n")
    (目 / "好.out").write_text("1\n", encoding="utf-8", newline="")
    (目 / "坏.jsh").write_text("打印(2)\n", encoding="utf-8", newline="\n")
    (目 / "坏.out").write_text("1\n", encoding="utf-8", newline="")
    (目 / "没期望.jsh").write_text("打印(3)\n", encoding="utf-8", newline="\n")

    r = _跑(用例目录=目)
    汇 = _取汇总(r.stdout)
    assert (int(汇["总"]), int(汇["通过"]), int(汇["失败"]),
            int(汇["跳过"]), int(汇["豁免"])) == (3, 1, 1, 1, 0), 汇.group(0)
    assert r.returncode == 1, "有失败就该退 1"
