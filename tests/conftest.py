# -*- coding: utf-8 -*-
"""测试夹具与共用工具。

⚠️ **这里曾经放着 `test_golden` / `test_error_snapshot` 的一份副本**（2026-10-26 删）：
`conftest.py` **不是测试模块**，pytest 根本不收集它 —— 那两条**从来没跑过**，
只是和 `tests/test_golden.py` 里的正本长得一样，看着像「有两把尺子」。
📌 而正本那两条也已按 M138 的规划**搬去 `tools/自测.jsh`**（CI 上跑），
现在 pytest 侧只剩 `test_m138_runner_summary.py` 把整批语料交给运行器 + 对账。
"""

import os
import tempfile
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


ROOT = Path(__file__).resolve().parents[1]

# 项目根下的 build/ 是临时工作目录（被 .gitignore）。CI 是**全新检出**、没有这个
# 目录，而个别用例会往它里面写文件（`打开("build/…", "写")`）→ 在 CI 上报
# 「找不到文件或目录」（M35 定位）。本地因为目录一直在，永远看不到这个失败。
(ROOT / "build").mkdir(exist_ok=True)

#: REPL 历史文件的位置（M38 B3 起 REPL 会把历史落盘）。测试必须指向
#: build/ 下：**别写进用户家目录**——测试跑一遍就把开发机/CI 的
#: `~/.jishi/repl_history.txt` 改了，属于「测试影响机器状态」的坏味道。
os.environ.setdefault("JISHI_HISTORY", str(ROOT / "build" / "_repl_history_test.txt"))


#: 「模块名遮蔽内建」那条提示的特征串（目前只有 `导入 文本` 会命中）
遮蔽提示特征 = "既是标准库模块名，又是内建函数名"


def 去掉遮蔽提示(文本: str) -> str:
    """删掉「`导入 文本` 会遮蔽内建」那条提示的**整行**。

    🔴 **为什么跨引擎比输出前必须剥掉它**（2026-10-26）：

    这条提示按**进程**去重（M18.4：同一个名字一个进程只提醒一次，免得一个程序
    `导入 文本` 十次就刷十行）。于是「打了几次」取决于**这个进程之前跑过什么**——
    **同进程**跑的引擎（树遍历 / Python VM）可能一次都不打，
    **子进程**跑的（Rust / Node）每次都打。两边一比就是「不一致」，但其实
    程序算出来的东西一模一样。

    ⇒ 凡是要**跨引擎比输出**的判据，都先过这一道。
    `tools/check_engines.py` 早就这么做了（那边写着同一句理由：「这条提示的打几次
    本身就是进程模型的副产物，不是语言语义」）——这里是给 pytest 用的那一份。

    ⚠️ 只剥这**一条固定文案**，不做泛化（泛化会把真差异也吃掉）。
    """
    if not 文本 or 遮蔽提示特征 not in 文本:
        return 文本
    return "".join(行 for 行 in 文本.splitlines(keepends=True)
                   if 遮蔽提示特征 not in 行)


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
    # 🔴 **一律落到系统临时目录**（2026-10-18）。以前是「在传进来的目录里改名」
    #    —— 而 20 多处调用点传的都是 `ROOT / "_tmp_xxx.json"`，于是临时文件落在
    #    **仓库根**：① 违反 AGENTS §1 ②「别在 cwd / 项目根读写真实文件」；
    #    ② 沙箱的写保护会直接拒写 ⇒ `PermissionError` 假红（实测 m41 / m50 / m52
    #    三处就是这么红的，而且**每次只冒一处**、串行跑又全过，很难查）。
    #    改在这一个入口，所有调用点一起修好。
    return Path(tempfile.gettempdir()) / f"{p.stem}_{os.getpid()}{p.suffix}"


@pytest.fixture(scope="session", autouse=True)
def _cleanup_tmp_bytecode():
    """测试**全部结束**后，统一清掉 `_tmp_*.json` 临时字节码文件。

    M51 回归提速（2026-09-25）：各跨宿主对拍用例原先在 `finally` 里
    `unlink` 临时字节码文件——每次删除后，下一个用例的 `write_text` 就是
    **新建文件**，在 Windows 上触发实时扫描（实测**写+删 ≈ 0.53s/次**，
    而覆盖写已存在文件只要 0.08s，快 6 倍）。

    于是改成「**运行期间复用不删**」（文件名已带 pid，跨进程不撞、同进程
    串行覆盖写），到这里（session 结束）再统一清一次。文件在项目根，
    但 `.gitignore` 已挡 `_tmp_*.json`。
    """
    yield
    for 目录 in (Path(tempfile.gettempdir()), ROOT):
        for pattern in ("_tmp_*.json", "jishi_m*.json"):
            for f in 目录.glob(pattern):
                try:
                    f.unlink()
                except OSError:
                    pass
    # test_m33 等写进 build/（`.gitignore` 已挡，但也清一下）
    for f in (ROOT / "build").glob("_m33_bc_*.json"):
        try:
            f.unlink()
        except OSError:
            pass
    # test_m40 的 _loc_probe.jsh
    for f in (ROOT / "tests").glob("_loc_probe.jsh"):
        try:
            f.unlink()
        except OSError:
            pass


