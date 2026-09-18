# -*- coding: utf-8 -*-
"""黄金文件测试：cases/*.jsh 对照 *.out；error_cases/*.jsh 对照 *.err。

- 黄金测试：执行源码，stdout 必须与 .out 完全一致（自动收集）
- 错误快照：执行必须抛 JishiError，渲染文本必须包含 .err 中的关键子串
"""

import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jishi.interpreter import run_source
from jishi.errors import JishiError

ROOT = Path(__file__).resolve().parents[1]
CASES = Path(__file__).parent / "cases"
ERROR_CASES = Path(__file__).parent / "error_cases"

# 项目根下的 build/ 是临时工作目录（被 .gitignore）。CI 是**全新检出**、没有这个
# 目录，而个别用例会往它里面写文件（`打开("build/…", "写")`）→ 在 CI 上报
# 「找不到文件或目录」（M35 定位）。本地因为目录一直在，永远看不到这个失败。
(ROOT / "build").mkdir(exist_ok=True)

#: REPL 历史文件的位置（M38 B3 起 REPL 会把历史落盘）。测试必须指向
#: build/ 下：**别写进用户家目录**——测试跑一遍就把开发机/CI 的
#: `~/.jishi/repl_history.txt` 改了，属于「测试影响机器状态」的坏味道。
os.environ.setdefault("JISHI_HISTORY", str(ROOT / "build" / "_repl_history_test.txt"))


def rust_exe_path() -> Path:
    """Rust 宿主可执行文件的路径（**按平台**取名）。

    cargo 在 Windows 上产出 `jishi-rs.exe`，在 Linux/macOS 上产出 `jishi-rs`
    （没有扩展名）。此前四个测试文件都硬编码 `jishi-rs.exe`，于是 **Rust 对拍
    在 Linux/macOS 的 CI 上全线 FileNotFoundError**（M35 定位；本机是 Windows
    所以一直没暴露）。
    """
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


def tmp_bc_path(filename: str) -> Path:
    """本轮专属的临时字节码路径（文件名里带进程号）。

    来由（M32 踩到，M35 修）：这些对拍测试原先往项目根写**固定文件名**
    （`_tmp_bc.json`、`_tmp_m30_bc.json` …）。同一个进程里测试是串行的，
    所以单跑没事；但两个进程同时跑（人工并行、或有人加了 pytest-xdist）
    就会互相覆写，产生**假失败**——表现是几十上百个 `SystemExit: 1`、
    位置每次都不一样、单跑必过。M32 为此查了很久。

    带进程号后跨进程不再撞；同进程内串行执行，所以这一个后缀就够了。
    """
    p = Path(filename)
    return p.with_name(f"{p.stem}_{os.getpid()}{p.suffix}")


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
