# -*- coding: utf-8 -*-
r"""R8.4b：交互式 REPL（`jishi -i`）搬进 Rust。

发行包改走单二进制（R8.3）之后 `-i` 不能再缺 —— 不留缺口才能换包。

判据是**逐字节对拍**：同一段会话喂给两边的 stdin，比 stdout / stderr / 退码。

⚠️ 两条对话里踩出来的细节（都是「只有逐字节比才发现」的那类）：

1. **提示符后面的空行**：Python 的 `BANNER` 是三引号字符串、末尾自带 `\n`，
   `print(BANNER)` 再补一个 ⇒ 屏幕上**有一行空行**。`HELP_TEXT` 同理。
   少这一行就与 Python 差一行。
2. **报错里的文件名不是同一个**：**语法**错显示 `<交互>`（`parse_source` 传进去的），
   **运行期**错显示 `<输入>`（`JishiError.filename` 的默认值，运行期错误不带文件名）。
   两条路名字不一样是 Python 的**既成行为**，看着别扭也得照抄。

⚠️ **一处如实标注的差异**：Rust 这版**没有行编辑**（`↑/↓` 翻历史、`Tab` 补全）——
那些在 `oracle/jishi/lineedit.py` 里，是另一件独立的事。`历史` 命令照样可用，
判据也覆盖了它；只是**真终端里的按键**这一层没有。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "rust" / "target" / "release" / ("jishi-rs.exe" if os.name == "nt" else "jishi-rs")
TMP_SUFFIX = f"{os.getpid()}"

need_rust = pytest.mark.skipif(
    not EXE.is_file(),
    reason="缺 rust/target/release/jishi-rs（跑 `cd rust && cargo build --release`）"
           "—— **跳过而不是替人构建**")


def _run(cmd: list[str], session: str, tmp: Path, tag: str) -> tuple[int, bytes, bytes]:
    """喂一段会话进 REPL，返回 `(退码, stdout, stderr)`。

    ⚠️ 两边各用**自己的** `JISHI_HISTORY`（放在 tmp 里）：
    历史文件是共享状态，用同一份会互相污染，`历史` 命令的输出就永远对不上。
    """
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT)
    env["JISHI_HISTORY"] = str(tmp / f"hist_{tag}_{TMP_SUFFIX}.txt")
    r = subprocess.run(cmd, input=session.encode("utf-8"), capture_output=True,
                       cwd=str(ROOT), env=env, timeout=120)
    return r.returncode, r.stdout, r.stderr


def _agree(session: str, tmp: Path, tag: str) -> None:
    p = _run([sys.executable, "-m", "oracle.jishi.cli", "-i"], session, tmp, "py" + tag)
    r = _run([str(EXE), "-i"], session, tmp, "rs" + tag)
    assert r[0] == p[0], f"退码 py={p[0]} rs={r[0]}"
    assert r[1] == p[1], (f"stdout 不一致：\n--- py ---\n"
                          f"{p[1].decode('utf-8', 'replace')}\n--- rs ---\n"
                          f"{r[1].decode('utf-8', 'replace')}")
    assert r[2] == p[2], (f"stderr 不一致：\n--- py ---\n"
                          f"{p[2].decode('utf-8', 'replace')}\n--- rs ---\n"
                          f"{r[2].decode('utf-8', 'replace')}")


# ---------------------------------------------------------------------------
# 一、表情回显（顶层表达式有值就回显，与 `打印` 分工同 Python）
# ---------------------------------------------------------------------------

@need_rust
def test_回显_基本表达式(tmp_path):
    """算术 / 文本 / 赋值不回显 / 布尔中文 —— 「空」也要回显（M27 那笔账）。"""
    _agree('1 + 2\n"你好"\n令 a = 5\na * 2\n空\n真 与 假\n退出\n', tmp_path, "echo")


@need_rust
def test_回显_容器与方法(tmp_path):
    """列表 / 长度 / 方法调用的返回值都是表达式语句，都要回显。"""
    _agree("令 甲 = [1,2,3]\n甲\n长度(甲)\n甲.追加(4)\n甲\n退出\n", tmp_path, "cont")


# ---------------------------------------------------------------------------
# 二、多行块
# ---------------------------------------------------------------------------

@need_rust
def test_多行_冒号结尾续行(tmp_path):
    """冒号结尾 → 续行；空行结束并执行。"""
    _agree("如果 真：\n    打印(\"是\")\n否则：\n    打印(\"否\")\n\n退出\n", tmp_path, "block")


@need_rust
def test_多行_函数定义与调用(tmp_path):
    """函数定义跨多行，定义完能接着调用（**同一份环境**不能丢）。"""
    _agree("函数 甲(x)：\n    返回 x * 2\n\n甲(21)\n退出\n", tmp_path, "func")


@need_rust
def test_多行_未闭合括号与引号(tmp_path):
    """括号 / 引号没闭合时都要继续读 —— 判定交给真词法器，不自己数引号。"""
    _agree('令 甲 = [1,\n2,\n3]\n甲\n打印("没写完\n的字符串")\n退出\n', tmp_path, "unclosed")


# ---------------------------------------------------------------------------
# 三、报错（语法错 / 运行期错，含源码行与 `^`）
# ---------------------------------------------------------------------------

@need_rust
def test_报错_语法错位置标签是交互(tmp_path):
    """**语法**错的 `┌─` 那一行是 `<交互>`（`parse_source` 传进去的名字）。"""
    _agree("打印(甲 +)\n退出\n", tmp_path, "parse")


@need_rust
def test_报错_运行期错位置标签是输入(tmp_path):
    """**运行期**错的 `┌─` 那一行是 `<输入>` —— 照抄 Python 的既成行为。"""
    _agree('打印("你好")\n1 / 0\n退出\n', tmp_path, "run")


@need_rust
def test_报错_之后还能继续用(tmp_path):
    """报错**不退出**会话：出错后环境还在，下一行照样能跑。"""
    _agree("令 甲 = 1\n甲 / 0\n甲 + 1\n退出\n", tmp_path, "recover")


# ---------------------------------------------------------------------------
# 四、命令
# ---------------------------------------------------------------------------

@need_rust
def test_命令_帮助(tmp_path):
    """帮助正文逐字一致（含结尾那行空行）。"""
    _agree("帮助\n退出\n", tmp_path, "help")


@need_rust
def test_命令_历史与清空(tmp_path):
    """`历史`（本次会话为空 → 「（还没有历史）」）与 `清空`。"""
    _agree("历史\n清空\n退出\n", tmp_path, "hist")


@need_rust
def test_命令_别名与无参数进REPL(tmp_path):
    """`q` 别名退出；**不给文件也要进 REPL**（与 `jishi -i` 同行为）。"""
    _agree("1 + 1\nq\n", tmp_path, "alias")
    # 不带 -i 时同样进 REPL
    env = dict(os.environ)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONPATH=str(ROOT),
               JISHI_HISTORY=str(tmp_path / f"h_noarg_{TMP_SUFFIX}.txt"))
    p = subprocess.run([sys.executable, "-m", "oracle.jishi.cli"], input=b"1 + 1\nq\n",
                       capture_output=True, cwd=str(ROOT), env=env, timeout=120)
    env["JISHI_HISTORY"] = str(tmp_path / f"h_noarg2_{TMP_SUFFIX}.txt")
    r = subprocess.run([str(EXE)], input=b"1 + 1\nq\n", capture_output=True,
                       cwd=str(ROOT), env=env, timeout=120)
    assert (r.returncode, r.stdout) == (p.returncode, p.stdout), \
        f"裸命令行不一致：\npy={p.stdout.decode('utf-8', 'replace')}\n" \
        f"rs={r.stdout.decode('utf-8', 'replace')}"
