# -*- coding: utf-8 -*-
"""R6.2/R6.3 · 运行期错误的「类型 + 消息 + 报错渲染」四执行器对齐。

R6.2 建 `tools/probe_runtime_errors.py` 时第一次跑：**19 个形状里 18 个不一致**。
两类根因，都要盯着：

* **Python 侧漏内部表示**：`list index out of range`（英文）、`'乙'`（键的 repr）
  —— 中文教学语言里的中英混排，且 `errors.py` 里 `_type_error_zh` 早就为
  `TypeError` 解决过同一件事；
* **宿主侧「笼统化」**：Rust 把多种语义错误都报成「异常」「运行期错误」，
  于是 `e.类型` 与 `捕获 X` 的匹配都对不上；有的地方还打**值**而不是**类型名**。

本文件钉三件事：

1. `tools/probe_runtime_errors.py` 的**全部形状在四个执行器上一致**（真跑，逐字节）；
2. 异常**父子表**与 Python 侧的类继承**逐项对拍**（这是「基类能不能抓子类」
   的唯一事实来源，两边各写一份迟早漂）；
3. R6.2 修掉的两个**宿主侧**缺口不许回来：`抛出` 路径的 stdout 要冲出来、
   `捕获 运行期错误` 要接得住 `键错误` / `索引错误`。
"""

import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from oracle.jishi import errors as E                                      # noqa: E402
from oracle.jishi.compiler import compile_to_json, compile_source         # noqa: E402
from oracle.jishi.interpreter import run_source as run_tree               # noqa: E402
from oracle.jishi.vm import VM                                            # noqa: E402
from probe_runtime_errors import SHAPES, build_source, engine_outputs   # noqa: E402


def _exe() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


EXE = _exe()
need_rust_vm = pytest.mark.skipif(
    not EXE.exists(),
    reason="Rust 宿主未构建（cd rust && cargo build --release）"
           "—— 与其假装通过，不如明确跳过")


def _write_src(src: str, tag: str) -> tuple[str, Path]:
    """把源码写到 `build/` 下并返回 `(相对路径, 绝对路径)`。

    ⚠️ 路径要是**相对仓库根**的：宿主是按 `filename` 原样去读的，
    两边必须看到同一个文件（`build/` 已在 .gitignore 里）。
    """
    name = f"_m65_{tag}_{os.getpid()}.jsh"
    path = ROOT / "build" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(src, encoding="utf-8")
    return f"build/{name}", path


def _rust(src: str, rel: str = "错误.jsh") -> tuple[str, str]:
    """Rust VM 跑一遍 → `(stdout, 报错文本)`。"""
    tmp = Path(tempfile.gettempdir()) / f"_m65_{os.getpid()}.json"
    tmp.write_text(compile_to_json(src, rel), encoding="utf-8")
    r = subprocess.run([str(EXE), "--load-bytecode", str(tmp)], capture_output=True, input=b"")
    return (r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"))


def _py(fn) -> tuple[str, str]:
    buf = io.StringIO()
    old_in = sys.stdin
    sys.stdin = io.StringIO("")
    try:
        with redirect_stdout(buf):
            fn()
        return buf.getvalue(), ""
    except BaseException as e:                                     # noqa: BLE001
        return buf.getvalue(), str(e)
    finally:
        sys.stdin = old_in


# ---------------------------------------------------------------------------
# 一、常见运行期错误的 (类型, 消息) —— 四个执行器逐字节一致
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tag", sorted(SHAPES))
def test_运行期错误形状四执行器一致(tag):
    """把每个形状**自己接住**再打印 `类型` / `消息`，然后四边比。

    ⚠️ 比的是 **stdout**（不是报错渲染）—— 于是「Rust 画不出源码行」那个
    另一个缺口不会干扰这一条（它由 `check_engines.py` 单独登记）。
    """
    outs = engine_outputs(build_source(SHAPES[tag]))
    base_name = "树遍历"
    base = outs[base_name]
    bad = {k: v for k, v in outs.items() if v != base}
    assert not bad, f"「{tag}」不一致：\n  {base_name}: {base!r}\n" + "\n".join(
        f"  {k}: {v!r}" for k, v in bad.items())
    # 还得真接住了（没人接住的话四个都长一样，但那是另一回事）
    assert "类型=" in base and "消息=" in base, f"「{tag}」没被接住：{base!r}"


def test_错误消息里不许出现内部表示():
    """扫全部形状的输出：**不许有英文的宿主异常话术或 repr**。

    与 R3（插值字符串把分段表泄出来）/ R4（`None` 泄进文案）是同一个家族 ——
    R6.2 抓到的正是这一批：`list index out of range`、`'乙'`。
    """
    banned = ("out of range", "not subscriptable", "can only concatenate",
              "unsupported operand", "object is not")
    bad = []
    for tag, body in SHAPES.items():
        out = engine_outputs(build_source(body))["树遍历"]
        for mark in banned:
            if mark in out:
                bad.append(f"{tag}: {out}")
    assert not bad, "报错消息漏了宿主语言的原文：\n  · " + "\n  · ".join(bad)


# ---------------------------------------------------------------------------
# 二、异常父子表：与 Python 侧的类继承**逐项对拍**
# ---------------------------------------------------------------------------

@need_rust_vm
def test_异常父子表与Python继承一致():
    """`捕获 基类` 能不能接住子类，靠的是那张父子表。

    宿主侧是**手写的表**（`rust/src/lib.rs` 的 `EXC_PARENT`），Python 侧是
    **真类继承**（`isinstance`）—— 两边各写一份，不盯就会漂。R6.2 之前
    宿主那张表只有三支，于是 `捕获 运行期错误` 接不住 `键错误` / `索引错误`，
    而语言规格 §8.9 明写「基类能抓子类」。
    """
    # 现象层：每个「异常类型名」都该有一个 `捕获 <基类>` 能接住它
    cases = [
        ("键错误", "运行期错误"), ("索引错误", "运行期错误"),
        ("值错误", "运行期错误"), ("除零错误", "运行期错误"),
        ("类型错误", "运行期错误"), ("键错误", "异常"),
        ("除零错误", "异常"),
    ]
    for child, parent in cases:
        src = (f'尝试：\n    抛出 {child}("x")\n'
               f'捕获 {parent}：\n    打印("接住了")\n')
        outs = []
        for front in ("python", "rust"):
            old = os.environ.get("JISHI_FRONTEND")
            os.environ["JISHI_FRONTEND"] = front
            try:
                out, err = _py(lambda s=src: run_tree(s, "父子.jsh"))
                outs.append(out if out else f"【没接住】{err.splitlines()[0] if err else ''}")
            finally:
                if old is None:
                    os.environ.pop("JISHI_FRONTEND", None)
                else:
                    os.environ["JISHI_FRONTEND"] = old
        assert outs[0] == outs[1], f"{child} ← {parent} 两个前端不一致：{outs}"

    # 结构层：宿主那张表要**覆盖 Python 侧所有运行期异常类**
    import re
    rust = (ROOT / "rust" / "src" / "lib.rs").read_text(encoding="utf-8")
    block = rust.split("const EXC_PARENT", 1)[1].split("];", 1)[0]
    listed = set(re.findall(r'\("([^"]+)",\s*"([^"]+)"\)', block))
    run_children = {cls.exc_name for cls in (
        E.RunZeroDivisionError, E.RunTypeError, E.RunValueError,
        E.RunIndexError, E.RunKeyError, E.RunFileError, E.RunAssertionError)
        if getattr(cls, "exc_name", None)}
    missing = run_children - {child for child, _ in listed}
    assert not missing, (
        f"宿主的异常父子表漏了这些子类（`捕获 运行期错误` 会接不住）：{sorted(missing)}")


# ---------------------------------------------------------------------------
# 三、R6.2 修掉的两个宿主缺口不许回来
# ---------------------------------------------------------------------------

@need_rust_vm
def test_抛出路径的stdout要冲出来():
    """缺口③：宿主以前只在**正常结束**时打印 `get_output`。

    于是「`打印` 几行之后崩掉」这个形状下，**前面所有输出全丢** ——
    用户拿到的是一个空屏 + 一句报错，连「跑到哪了」都看不到。
    三个 Python 执行器是边跑边出（stdout 直连）。
    """
    src = ('打印("第一行")\n打印("第二行")\n'
           '函数 炸()：\n    抛出 值错误("boom")\n炸()\n')
    tree_out, _ = _py(lambda: run_tree(src, "冲.jsh"))
    r_out, r_err = _rust(src, "冲.jsh")
    assert tree_out == "第一行\n第二行\n", tree_out
    assert r_out == tree_out, f"宿主把报错前的输出丢了：{r_out!r}"
    assert "boom" in r_err, r_err


def _indent(body: str, pad: str = "    ") -> str:
    """把多行源码整体缩进 —— 塞进 `尝试：` 体里时必须整块缩进，
    否则第二行会掉到第 0 列、把 `尝试` 的体提前结束掉（解析器会当场报错）。"""
    return "".join(pad + ln + "\n" for ln in body.splitlines())


@need_rust_vm
def test_运行期错误接得住子类():
    """缺口②：`捕获 运行期错误`（基类）必须接得住 `键错误` / `索引错误`（子类）。

    语言规格 §8.9：「基类能抓子类」。宿主原来那张表只有三支，
    于是这两个是**漏网**的 —— 而 `examples/12_异常处理.jsh` 正好用到。
    """
    for body in ('令 字 = {“甲”: 1}\n字["乙"]\n', '令 列 = [1]\n列[9]\n'):
        src = (f'尝试：\n{_indent(body)}'
               '捕获 运行期错误：\n    打印("接住了")\n')
        out, r_err = _rust(src, "接住.jsh")
        assert "接住了" in out, (
            f"没接住：{body!r} → 报错 "
            f"{r_err.splitlines()[0] if r_err else '(无)'}")



# ---------------------------------------------------------------------------
# 四、R6.3：宿主的**报错渲染**与 Python 的 `render()` 逐字一致
# ---------------------------------------------------------------------------

#: 渲染对齐用的形状：都**不自己接住**（要的就是那块报错方框），
#: 且都带嵌套调用（调用链非空）。
_RENDER_CASES = {
    "嵌套调用里越界": (
        "函数 外层()：\n    返回 中层()\n"
        "函数 中层()：\n    令 列 = [1]\n    返回 列[9]\n"
        "打印(\"先跑一句\")\n外层()\n"),
    "未定义名字": "函数 甲()：\n    返回 乙\n甲()\n",
    "调用非函数": "函数 甲()：\n    令 丙 = 5\n    返回 丙()\n甲()\n",
    "除零": "函数 甲()：\n    令 零 = 0\n    返回 1 / 零\n甲()\n",
}


@need_rust_vm
@pytest.mark.parametrize("tag", sorted(_RENDER_CASES))
def test_报错渲染与树遍历逐字一致(tag):
    """⚠️ 判据是**用户真正看到的那一份**：先 `with_source(源码行)` 再 `render()`。

    以前体检只比 stdout，于是「宿主画不出源码行」这件事只能单独数个数 ——
    **判据选错，问题就不在视野里**。R6.3 把宿主的渲染补齐（按 `filename`
    在出错那一刻读一次源文件），这条才有得比。
    """
    src = _RENDER_CASES[tag]
    # ⚠️ 源码要**落到磁盘上**：宿主的渲染是「按 `filename` 现读源文件」，
    # 只在内存里给它一段源码，它当然画不出源码行（那正是被测的行为）。
    rel, path = _write_src(src, tag)
    tree_out, _ = _py(lambda: run_tree(src, rel))
    # 用 CLI 那条渲染口径（带源码行）
    from oracle.jishi.errors import JishiError as _JE
    err_obj = None
    try:
        run_tree(src, rel)
    except _JE as e:
        e.with_source(src.split("\n"))
        err_obj = e.render()
    assert err_obj, f"「{tag}」没报错，用例设计有问题"
    r_out, r_err = _rust(src, rel)
    assert r_out == tree_out, f"stdout 不同：{tree_out!r} vs {r_out!r}"
    assert r_err.rstrip("\n") == err_obj, (
        f"「{tag}」报错渲染不同：\n--- 树遍历 ---\n{err_obj}\n--- 宿主 ---\n{r_err}")
    path.unlink(missing_ok=True)


@need_rust_vm
def test_报错渲染三个要件都在():
    """渲染里必须同时有：源码行、`^` 指示、调用链。

    这是「报错可操作」的最小要求（**核心二**）—— 宿主在 R7/R8 之后是
    **唯一形态**，缺一样就是体验退步。
    """
    src = _RENDER_CASES["嵌套调用里越界"]
    rel, path = _write_src(src, "渲染要件")
    _, r_err = _rust(src, rel)
    path.unlink(missing_ok=True)
    assert f"  ┌─ {rel}:5:9" in r_err, r_err
    assert "  5 │     返回 列[9]" in r_err, r_err
    assert "^" in r_err, r_err
    assert "调用链（从外到内）：" in r_err, r_err
    assert "  外层 ← 第 7 行" in r_err, r_err


@need_rust_vm
def test_读不到源文件时仍打得出一行报错():
    """嵌进别的程序时源文件可能不在 —— **不能因此崩掉或什么都不报**。

    退回「没有源码行」那种形态（位置那一行照样有），而不是报个 IO 错误。
    """
    src = "令 列 = [1]\n列[9]\n"
    tmp = Path(tempfile.gettempdir()) / f"_m65_gone_{os.getpid()}.json"
    tmp.write_text(compile_to_json(src, "不存在的文件.jsh"), encoding="utf-8")
    r = subprocess.run([str(EXE), "--load-bytecode", str(tmp)], capture_output=True, input=b"")
    err = r.stderr.decode("utf-8", "replace")
    assert r.returncode == 1, r.returncode
    assert "错误 E2003" in err, err
    assert "┌─ 不存在的文件.jsh:2:2" in err, err
    assert "Traceback" not in err, err
