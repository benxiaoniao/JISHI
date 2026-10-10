# -*- coding: utf-8 -*-
"""R7.5 收尾：`jishi 调试`（交互式命令行）搬进 Rust —— 逐字对拍 + 独立判据。

R7.5 把调试**会话核心**（`rust/src/debugger.rs`，对应 `debugger.py` 的
`_Session`）与 DAP 适配器搬进了 Rust，但只实现了 DAP 会用到的**恢复类**命令
（继续/单步/下一步/跳出/退出）。这一轮补齐**命令行专用的命令表**
（断点/取消断点/清空断点/变量/查看/栈/源码/帮助）与「读一行」的驱动
（`LineDriver` —— `PauseDriver` 这个抽象当初就是为它留的口子）。

判据分两半（本项目的惯例，R7.1-c 起的纪律）：

* **对拍**：与 `jishi 调试`（默认就是**树遍历**执行器）在同一串 `--命令` 下
  **stdout / stderr / 退出码逐字节一致**；
* **独立判据**：把「Rust 自己该输出什么」写成写死的期望 —— 不 import oracle.jishi、
  不 spawn Python，R8 之后 Python 退场它仍然要绿。

⚠️ **已知且刻意保留的差异**（如实登记，见 `AGENTS.md` 的 R7 段）：
Rust 只支持**树遍历**执行器；`--执行器 vm/cvm` **明确拒绝**（退码 2），
不静默降级 —— 字节码执行器的调试钩子还没搬过来（与 `jishi-rs dap` 同一条）。
所以本文件的对拍**不覆盖** `--执行器 vm/cvm`。

⚠️ 顺带修掉的一个**宿主真 bug**（由本轮的 `变量 全部` 对拍照出来）：宿主把内建
函数渲染成 `<内建函数 打印>`、异常类型渲染成裸的 `值错误`，而 Python/Node 是
`<内建 打印>` / `<异常类型 值错误>`。旧的 `test_m32` 只比 Python VM 与 Node
（`_agree` 没算上 Rust），所以一直没被发现 —— 这里钉住它。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _exe() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


EXE = _exe()
need_rust = pytest.mark.skipif(
    not EXE.exists(),
    reason="Rust 宿主未构建（cd rust && cargo build --release）"
           "—— 与其假装通过，不如明确跳过")


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    env["PYTHONPATH"] = str(ROOT)      # 私有 cwd 里 `-m oracle.jishi.cli` 才找得到包
    return env


def _run(cmd, cwd=None, timeout: float = 60.0):
    r = subprocess.run(cmd, input=b"", capture_output=True, env=_env(),
                       cwd=str(cwd or ROOT), timeout=timeout)
    return r.stdout, r.stderr, r.returncode


def _rs(args, cwd=None):
    return _run([str(EXE)] + list(args), cwd)


def _py(args, cwd=None):
    return _run([sys.executable, "-m", "oracle.jishi.cli"] + list(args), cwd)


# ---------------------------------------------------------------------------
# 被调试的三段程序
# ---------------------------------------------------------------------------

#: 普通程序：模块级 + 函数 + 打印（覆盖单步、变量、栈、查看）
PROG_OK = """令 单价 = 12
令 数量 = 3

函数 求和(甲, 乙)：
    令 和 = 甲 + 乙
    返回 和

令 总额 = 求和(单价, 数量)
打印("总额 =", 总额)
"""

#: 模块顶层带**脱糖临时变量**（`匹配` 的 `__匹配_N__`）—— 考「看变量」默认藏它
PROG_TEMP = """令 分数 = 85
匹配 分数：
    情形 90：
        打印("优秀")
    情形 85：
        打印("良好")
    情形 其他：
        打印("一般")
"""

#: 出错程序：内层函数里除零 —— 考「调试视角的出错现场」
PROG_ERR = """函数 除(甲, 乙)：
    令 商 = 甲 / 乙
    返回 商

函数 主()：
    令 x = 10
    令 y = 0
    打印(除(x, y))

主()
"""


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p.resolve())


# ---------------------------------------------------------------------------
# 一、对拍：同一串 `--命令` 下 stdout / stderr / 退出码逐字一致
# ---------------------------------------------------------------------------

#: `(场景名, 程序键, 命令串, 额外参数)` —— 覆盖命令表每一行。
SCENARIOS = [
    ("入口与帮助", "ok", "帮助", []),
    ("单步与变量", "ok", "下一步;单步;变量;变量 全部;栈;继续", []),
    ("查看表达式", "ok", "查看 单价 * 数量;查看 甲 + 乙;继续", []),
    ("查看缺参", "ok", "查看;继续", []),
    ("函数内看变量", "ok", "变量;变量 全部;栈;查看 甲 + 乙;继续", ["--断点", "5"]),
    ("断点增删列", "ok", "断点 8;断点;取消断点 8;断点;清空断点;断点;继续", []),
    ("断点行号写法", "ok", "断点 8,9;断点;继续", []),
    ("断点坏行号", "ok", "断点 一二;继续", []),
    ("取消断点缺参", "ok", "取消断点;继续", []),
    ("死断点提示", "ok", "继续", ["--断点", "2"]),          # 第 2 行是空行
    ("源码", "ok", "源码;源码 3;源码 甲;继续", []),
    ("空命令重复", "ok", ";继续", []),
    ("未知命令", "ok", "zzz;继续", []),
    ("退出中途", "ok", "退出", []),
    ("不停在入口", "ok", "继续", ["--不停在入口"]),
    ("显式执行器", "ok", "继续", ["--执行器", "树遍历"]),
    ("临时变量隐藏", "temp", "变量;变量 全部;继续", []),
    ("出错现场", "err", "继续", []),
    ("出错现场_单步", "err", "单步;单步;单步;单步;继续", []),
    ("出错现场_变量", "err", "变量;查看 x / y;继续", ["--断点", "7"]),
]

_PROGS = {"ok": PROG_OK, "temp": PROG_TEMP, "err": PROG_ERR}


@need_rust
@pytest.mark.parametrize("name,key,cmds,extra", SCENARIOS,
                         ids=[s[0] for s in SCENARIOS])
def test_对拍_调试会话逐字节(name, key, cmds, extra, tmp_path):
    prog = _write(tmp_path, f"{key}.jsh", _PROGS[key])
    args = ["调试", prog, "--命令", cmds] + list(extra)
    po, pe, pc = _py(args)
    ro, re_, rc = _rs(args)
    if (po, pe, pc) == (ro, re_, rc):
        return
    msg = [f"场景「{name}」对拍不一致（命令：{cmds!r} 额外：{extra!r}）"]
    if po != ro:
        msg.append(f"  stdout:\n    py : {po!r}\n    rs : {ro!r}")
    if pe != re_:
        msg.append(f"  stderr:\n    py : {pe!r}\n    rs : {re_!r}")
    if pc != rc:
        msg.append(f"  退出码：py={pc} rs={rc}")
    pytest.fail("\n".join(msg))


@need_rust
def test_对拍_从标准输入读命令(tmp_path):
    """`jishi 调试 x.jsh < 命令.txt`：管道输入的两边记录要逐字一样。

    ⚠️ 管道不会替用户回显命令（终端会），所以实现里自己回显一遍 ——
    不回显的话记录里只剩提示符和输出挤在一行。
    """
    prog = _write(tmp_path, "ok.jsh", PROG_OK)
    stdin = "变量\n查看 单价 * 数量\n栈\n继续\n".encode("utf-8")
    args = ["调试", prog]
    p = subprocess.run([sys.executable, "-m", "oracle.jishi.cli"] + args, input=stdin,
                       capture_output=True, env=_env(), cwd=str(ROOT), timeout=60)
    r = subprocess.run([str(EXE)] + args, input=stdin, capture_output=True,
                       env=_env(), cwd=str(ROOT), timeout=60)
    assert (p.stdout, p.stderr, p.returncode) == (r.stdout, r.stderr, r.returncode), \
        f"stdin 路径不一致\npy : {p.stdout!r}\nrs : {r.stdout!r}"


# ---------------------------------------------------------------------------
# 二、不依赖 Python 的独立判据（R8 之后 Python 退场仍要绿）
# ---------------------------------------------------------------------------

@need_rust
def test_独立_写死的会话期望(tmp_path):
    """一整场会话的 stdout **逐字写死** —— 不 import oracle.jishi、不 spawn Python。"""
    prog = _write(tmp_path, "固定.jsh", "令 甲 = 1\n令 乙 = 2\n打印(甲 + 乙)\n")
    out, err, code = _rs(["调试", prog, "--命令", "单步;变量;栈;继续"])
    assert err == b"", err
    assert code == 0, code
    want = (
        "\n"
        "暂停（停在入口） 第 1 行 · 模块顶层\n"
        "→ 1 │ 令 甲 = 1\n"
        "（输入「帮助」看命令）\n"
        "调试 > 单步\n"
        "\n"
        "暂停（单步） 第 2 行 · 模块顶层\n"
        "→ 2 │ 令 乙 = 2\n"
        "调试 > 变量\n"
        "当前帧（模块顶层）的变量：\n"
        "  甲 = 1\n"
        "  （另有内建/临时变量没显示，要看用「变量 全部」）\n"
        "调试 > 栈\n"
        "调用栈（由内到外）：\n"
        "  #0 第    2 行  模块顶层\n"
        "调试 > 继续\n"
        "3\n"
        "（程序正常结束，本次共暂停 2 次）\n"
    )
    assert out.decode("utf-8") == want


@need_rust
def test_独立_帮助表写死(tmp_path):
    """`帮助` 打出的那张表**逐行写死**（命令表不许漂）。"""
    prog = _write(tmp_path, "h.jsh", "打印(1)\n")
    out, _, _ = _rs(["调试", prog, "--命令", "帮助"])
    body = out.decode("utf-8")
    for line in (
        "  继续 / c                跑到下一个断点（没有断点就跑完）",
        "  断点 / b                列出断点；「断点 12」或「断点 12,15」添加",
        "  取消断点 12             删掉某个断点；「清空断点」全删",
        "  变量 / v                看当前帧的变量（「变量 全部」连内建与临时变量）",
        "  查看 表达式 / p         在当前帧里求值，例如「查看 单价 * 数量」",
        "  栈 / bt                 看调用栈（由内到外，带行号）",
        "  源码 [行号] / l         看当前行（或指定行）附近的源码",
        "  退出 / q                结束调试会话（程序不再往下跑）",
        "  （直接回车 = 重复上一条命令）",
    ):
        assert line in body, line


@need_rust
def test_独立_只支持树遍历执行器(tmp_path):
    """字节码执行器的调试钩子还没搬 —— 要**明确拒绝**，不静默降级。"""
    prog = _write(tmp_path, "v.jsh", "打印(1)\n")
    for eng in ("vm", "cvm"):
        r = subprocess.run([str(EXE), "调试", prog, "--执行器", eng],
                           input=b"", capture_output=True, env=_env(),
                           cwd=str(ROOT), timeout=60)
        assert r.returncode == 2, (eng, r.returncode)
        text = r.stderr.decode("utf-8", "replace")
        assert "只支持「树遍历」执行器" in text, text
        assert eng in text


@need_rust
def test_独立_死断点提示走stderr(tmp_path):
    """断点打在空行上要**当场说**（stderr），否则用户只会怀疑调试器坏了。"""
    prog = _write(tmp_path, "gap.jsh", "令 甲 = 1\n\n# 注释行\n打印(甲)\n")
    r = subprocess.run([str(EXE), "调试", prog, "--断点", "2,3",
                        "--命令", "继续"],
                       input=b"", capture_output=True, env=_env(),
                       cwd=str(ROOT), timeout=60)
    assert r.returncode == 0, r.returncode
    text = r.stderr.decode("utf-8")
    assert "提示：第 2 行、第 3 行 上没有可停的语句" in text, text
    assert "这几个断点不会触发" in text, text


@need_rust
def test_独立_清空PATH也能跑(tmp_path):
    """Rust 二进制的意义就是**不依赖 Python** —— 把 PATH 清掉照样要跑得动。"""
    prog = _write(tmp_path, "p.jsh", "令 甲 = 1\n打印(甲)\n")
    env = _env()
    env["PATH"] = ""
    r = subprocess.run([str(EXE), "调试", prog, "--命令", "变量;继续"],
                       input=b"", capture_output=True, env=env,
                       cwd=str(ROOT), timeout=60)
    assert r.returncode == 0, (r.returncode, r.stderr[:300])
    out = r.stdout.decode("utf-8")
    assert "甲 = 1" in out, out
    assert "1\n" in out, out


@need_rust
def test_独立_内建值渲染对齐Python(tmp_path):
    """宿主把内建/异常类型渲染成 `<内建 打印>` / `<异常类型 值错误>`。

    这是 2026-10-07 修的一个**宿主真 bug**（以前给 `<内建函数 打印>` / 裸的
    `值错误`）：老测试只比 Python VM 与 Node，没算上 Rust。这里既钉引擎，
    也钉「变量 全部」里内建那几行的渲染。
    """
    prog = _write(tmp_path, "bi.jsh", '导入 数学\n打印(打印)\n打印(值错误)\n'
                                     '打印([打印, 数学])\n')
    out, err, code = _rs([prog])
    assert code == 0, (code, err)
    assert out.decode("utf-8") == (
        "<内建 打印>\n<异常类型 值错误>\n[<内建 打印>, <模块 数学>]\n"
    )
