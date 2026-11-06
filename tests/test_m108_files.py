# -*- coding: utf-8 -*-
"""M108：文件批处理 + 系统。

从「文件批处理」探针（`examples/绕道探针/文件批处理/`）撞出来的缺口：
**按通配模式找文件**（`路径.按模式找`）、**原子写**（`文件.原子写`）、
**临时目录 / 临时文件**（`路径.临时目录` / `路径.临时文件` / `路径.系统临时目录`）、
**递归删除**（`路径.递归删除`）、**用默认程序打开**（`系统.打开`）。

顺带修掉一个被新函数暴露出来的**既有分歧**：容器内字符串的引用（`repr`）。
以前 Rust 一个字符都不转义（`['子\\a.md']` vs Python `['子\\\\a.md']`）、
Node 只转义 `\\` `'` `\\n` —— 五个执行器各写各的。现在三边同一套小规则，
判据见 `test_转义_五执行器逐字一致` 与 `test_转义_不许退回旧写法`。
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from conftest import rust_exe_path, tmp_bc_path          # noqa: E402
from oracle.jishi import serialize                        # noqa: E402
from oracle.jishi.interpreter import run_source           # noqa: E402

RUST_EXE = rust_exe_path()


def _跑(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        run_source(src, filename="<t>")
    return buf.getvalue()


def _字节码(src: str):
    bc = tmp_bc_path(ROOT / "_tmp_m108_bc.json")
    from oracle.jishi.compiler import compile_source
    bc.write_text(serialize.dumps(compile_source(src, "<t>")), encoding="utf-8")
    return bc


# ---------------------------------------------------------------------------
# 一、路径.按模式找
# ---------------------------------------------------------------------------

def _造样本(目录: Path) -> None:
    (目录 / "子").mkdir(parents=True, exist_ok=True)
    for 名, 内容 in [("报告A.md", "A"), ("报告B.md", "B"), ("旧报告.md", "旧"),
                     ("2026-01.log", "1"), ("2026-02.log", "2"),
                     ("说明.txt", "x")]:
        (目录 / 名).write_text(内容, encoding="utf-8")
    (目录 / "子" / "报告C.md").write_text("C", encoding="utf-8")


def test_按模式找_递归与只看本层(tmp_path):
    样本 = tmp_path / "数据"
    样本.mkdir()
    _造样本(样本)
    src = (
        '导入 路径\n'
        f'令 D = {str(样本)!r}\n'
        '打印(路径.按模式找(D, "报告*.md"))\n'
        '打印(路径.按模式找(D, "报告*.md", 假))\n'
        '打印(路径.按模式找(D, "2026-??.log"))\n'
        '打印(路径.按模式找(D, "报告[AB].md"))\n'
        '打印(路径.按模式找(D, "报告[!A].md"))\n'
        '打印(路径.按模式找(D, "旧*.md"))\n'
        '打印(长度(路径.按模式找(D, "*.md")))\n'
    )
    got = _跑(src)
    行 = got.splitlines()
    # 相对路径、已排序、只收文件（目录「子」本身不算）
    assert 行[0] == r"['子\\报告C.md', '报告A.md', '报告B.md']" \
        or 行[0] == "['子/报告C.md', '报告A.md', '报告B.md']", got
    assert 行[1] == "['报告A.md', '报告B.md']", got
    assert 行[2] == "['2026-01.log', '2026-02.log']", got
    assert 行[3] == "['报告A.md', '报告B.md']", got
    assert 行[4] == r"['子\\报告C.md', '报告B.md']" \
        or 行[4] == "['子/报告C.md', '报告B.md']", got
    assert 行[5] == "['旧报告.md']", got
    assert 行[6] == "4", got


def test_按模式找_区分大小写():
    """不论在哪个系统上，匹配都**区分大小写**（同一份程序到哪都一样）。"""
    src = ('导入 路径\n导入 文件\n'
           '令 D = 路径.临时目录("m108-大小写-")\n'
           '文件.写文本(路径.连接(D, "Report.md"), "x")\n'
           '打印(长度(路径.按模式找(D, "report*.md")))\n'
           '打印(长度(路径.按模式找(D, "Report*.md")))\n'
           '路径.递归删除(D)\n')
    assert _跑(src) == "0\n1\n"


def test_按模式找_不是目录要报错(tmp_path):
    文件 = tmp_path / "一个文件.txt"
    文件.write_text("x", encoding="utf-8")
    from oracle.jishi.errors import JishiError
    with pytest.raises(JishiError) as ei:
        _跑(f'导入 路径\n路径.按模式找({str(文件)!r}, "*")\n')
    assert "不是目录" in str(ei.value)


def test_模式匹配_与Python的fnmatch对拍():
    """钉住通配语义：基石的 `_模式匹配` 要与 `fnmatch.fnmatchcase` 一致。

    这是**源码级**的判据（不跑 VM），任何机器都能跑；
    以后谁改了匹配器，这条会立刻报。
    """
    import fnmatch
    from oracle.jishi.stdlib.路径 import _模式匹配
    用例 = [
        ("*", "abc"), ("*", ""), ("*.md", "报告A.md"), ("报告*.md", "报告A.md"),
        ("报告*.md", "旧报告.md"), ("?.md", "a.md"), ("?.md", "ab.md"),
        ("[abc].txt", "a.txt"), ("[abc].txt", "d.txt"), ("[!abc].txt", "d.txt"),
        ("[!abc].txt", "a.txt"), ("[^abc].txt", "d.txt"),
        ("[a-z]1", "m1"), ("[a-z]1", "A1"), ("[!a-z]1", "A1"),
        ("a[", "a["), ("a[", "ab"), ("a[]]", "a]"), ("[]a]", "]a]"),
        ("*[0-9]*", "x3y"), ("*[0-9]*", "xyz"), ("**", "ab"), ("?", "中"),
        ("[中A]", "中"), ("[中A]", "B"),
    ]
    for 模式, 名 in 用例:
        assert _模式匹配(模式, 名) == fnmatch.fnmatchcase(名, 模式), (模式, 名)


# ---------------------------------------------------------------------------
# 二、文件.原子写
# ---------------------------------------------------------------------------

def test_原子写_内容正确且不留临时文件(tmp_path):
    目标 = tmp_path / "索引.txt"
    src = (
        '导入 文件\n导入 路径\n'
        f'文件.原子写({str(目标)!r}, "第一版")\n'
        '打印(文件.读文本(' + repr(str(目标)) + '))\n'
        f'文件.原子写({str(目标)!r}, "第二版（覆盖）")\n'
        '打印(文件.读文本(' + repr(str(目标)) + '))\n'
        f'打印(路径.按模式找({str(tmp_path)!r}, "*.tmp"))\n'
    )
    assert _跑(src) == "第一版\n第二版（覆盖）\n[]\n"
    # 目录里除了目标，一个多余文件都没有（临时文件已改名）
    assert sorted(p.name for p in tmp_path.iterdir()) == ["索引.txt"]


def test_原子写_目标目录不存在会自己建(tmp_path):
    目标 = tmp_path / "还没建的目录" / "深层" / "a.txt"
    _跑(f'导入 文件\n文件.原子写({str(目标)!r}, "x")\n')
    assert 目标.read_text(encoding="utf-8") == "x"


# ---------------------------------------------------------------------------
# 三、递归删除 / 临时目录
# ---------------------------------------------------------------------------

def test_递归删除_连目录树一起删(tmp_path):
    树 = tmp_path / "树"
    (树 / "深" / "更深").mkdir(parents=True)
    (树 / "深" / "更深" / "a.txt").write_text("x", encoding="utf-8")
    (树 / "b.txt").write_text("y", encoding="utf-8")
    src = (f'导入 路径\n打印(路径.递归删除({str(树)!r}))\n'
           f'打印(路径.存在({str(树)!r}))\n'
           f'打印(路径.递归删除({str(树)!r}))\n')
    assert _跑(src) == "真\n假\n假\n"


def test_临时目录与临时文件_每次都唯一():
    src = ('导入 路径\n导入 文件\n'
           '令 D1 = 路径.临时目录("m108-")\n令 D2 = 路径.临时目录("m108-")\n'
           '打印(D1 == D2)\n'
           '打印(路径.是目录(D1))\n'
           '令 F1 = 路径.临时文件(".txt")\n令 F2 = 路径.临时文件(".txt")\n'
           '打印(F1 == F2)\n打印(路径.是文件(F1))\n'
           '打印(路径.后缀(F1))\n'
           '打印(路径.是目录(路径.系统临时目录()))\n'
           '路径.递归删除(D1)\n路径.递归删除(D2)\n'
           '路径.删除(F1)\n路径.删除(F2)\n')
    assert _跑(src) == "假\n真\n假\n真\n.txt\n真\n"


# ---------------------------------------------------------------------------
# 四、系统.打开
# ---------------------------------------------------------------------------

def test_系统打开_不存在的目标给假且不启动任何东西(tmp_path):
    """这条是**跨引擎可判**的：目标不存在（且不是网址）→ 假，什么都不启动。

    所以它既能挡掉「路径打错弹出空白窗」，也让测试**不会真的弹窗**。
    """
    没有 = tmp_path / "没有这个文件.zzz"
    src = (f'导入 系统\n打印(系统.打开({str(没有)!r}))\n')
    assert _跑(src) == "假\n"
    if RUST_EXE.is_file():
        bc = _字节码(src)
        r = subprocess.run([str(RUST_EXE), "--load-bytecode", str(bc)],
                           capture_output=True)
        assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:300]
        assert r.stdout.decode("utf-8", "replace") == "假\n"


# ---------------------------------------------------------------------------
# 五、容器内字符串的引用（顺带修掉的既有分歧）
# ---------------------------------------------------------------------------

_转义脚本 = (
    '打印(["制表符[\\t]结束"])\n'
    '打印(["反斜杠[\\\\]结束"])\n'
    '打印(["换行[\\n]结束"])\n'
    '打印(["回车[\\r]结束"])\n'
    '打印(["引号[\\"]结束"])\n'
    "打印([\"单引号[']结束\"])\n"
)

_转义期望 = (
    "['制表符[\\t]结束']\n"
    "['反斜杠[\\\\]结束']\n"
    "['换行[\\n]结束']\n"
    "['回车[\\r]结束']\n"
    '[\'引号["]结束\']\n'
    '["单引号[\']结束"]\n'
)


def test_转义_五执行器逐字一致():
    """容器内字符串的 `repr` 五个执行器要**逐字**一样（M108 修的分歧）。"""
    assert _跑(_转义脚本) == _转义期望
    if not RUST_EXE.is_file():
        pytest.skip("没有 rust 宿主产物")
    bc = _字节码(_转义脚本)
    r = subprocess.run([str(RUST_EXE), "--load-bytecode", str(bc)],
                       capture_output=True)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:400]
    assert r.stdout.decode("utf-8", "replace") == _转义期望, "Rust VM 不一致"

    from shutil import which
    if which("node") is None:
        pytest.skip("环境里没有 node")
    r2 = subprocess.run(["node", str(ROOT / "oracle" / "node" / "index.js"),
                         str(bc)], capture_output=True, text=True, encoding="utf-8")
    assert r2.returncode == 0, (r2.stderr or "")[:400]
    assert r2.stdout == _转义期望, f"Node 不一致：\n{r2.stdout!r}"


def test_转义_不许退回旧写法():
    """源码级判据：Rust / Node 的字符串引用**不许**退回「不转义」的老写法。

    以前：Rust 是 `format!("'{s}'")`（一个字符都不转义）、Node 只
    `replace` 了 `\\` `'` `\\n` —— 两边都对不上 Python 的 `repr`。
    """
    rust = (ROOT / "rust" / "src" / "lib.rs").read_text(encoding="utf-8")
    旧写法 = "=> format!(\"'{s}'\")"
    assert 旧写法 not in rust, "Rust 的 py_repr 又回到「不转义」了"
    assert "fn 引用文本" in rust, "Rust 少了 引用文本（转义）"

    js = (ROOT / "oracle" / "node" / "runtime.js").read_text(encoding="utf-8")
    assert "function quoteText" in js, "Node 少了 quoteText（转义）"
    assert "return quoteText(v)" in js, "Node 的 pyRepr 没走 quoteText"


# ---------------------------------------------------------------------------
# 六、五执行器一致（M108 的函数全跑一遍）
# ---------------------------------------------------------------------------

_脚本 = (
    '导入 路径\n导入 文件\n导入 系统\n'
    '令 工作 = 路径.临时目录("M108-")\n'
    '文件.创建目录(路径.连接(工作, "子"))\n'
    '文件.写文本(路径.连接(工作, "报告A.md"), "甲")\n'
    '文件.写文本(路径.连接(工作, "报告B.md"), "乙")\n'
    '文件.写文本(路径.连接(工作, "旧报告.md"), "丙")\n'
    '文件.写文本(路径.连接(工作, "子", "报告C.md"), "丁")\n'
    '文件.写文本(路径.连接(工作, "2026-01.log"), "一")\n'
    '打印(路径.按模式找(工作, "报告*.md"))\n'
    '打印(路径.按模式找(工作, "报告*.md", 假))\n'
    '打印(路径.按模式找(工作, "2026-??.log"))\n'
    '文件.原子写(路径.连接(工作, "索引.txt"), "原子写")\n'
    '打印(文件.读文本(路径.连接(工作, "索引.txt")))\n'
    '打印(路径.按模式找(工作, "*.tmp"))\n'
    '打印(系统.打开(路径.连接(工作, "没有.zzz")))\n'
    '打印(路径.是目录(路径.系统临时目录()))\n'
    '打印(路径.递归删除(工作))\n'
    '打印(路径.存在(工作))\n'
)


def _期望_脚本() -> str:
    # Windows 的相对路径里是 `\`，在容器里按 `repr` 规则**转义成 `\\`**
    sep = "\\\\" if os.name == "nt" else "/"
    return (
        f"['子{sep}报告C.md', '报告A.md', '报告B.md']\n"
        "['报告A.md', '报告B.md']\n"
        "['2026-01.log']\n"
        "原子写\n"
        "[]\n"
        "假\n"
        "真\n"
        "真\n"
        "假\n"
    )


def test_五执行器一致_M108():
    """树遍历 / 字节码 VM / Rust VM / Node 逐字节一致（C VM 走宿主标准库）。"""
    import check_engines as ce
    from oracle.jishi.vm import VM
    from oracle.jishi.compiler import compile_source
    import oracle.jishi.cvm_bind as cvm_bind

    rel = "m108.jsh"
    run_dir = os.path.join(tempfile.gettempdir(), f"_m108_{os.getpid()}")
    shutil.rmtree(run_dir, ignore_errors=True)
    os.makedirs(run_dir, exist_ok=True)
    open(os.path.join(run_dir, rel), "w", encoding="utf-8").write(_脚本)
    lines = _脚本.split("\n")
    out = {}
    try:
        with ce._in_dir(run_dir):
            out["树遍历"] = ce._run(lambda: run_source(_脚本, rel), lines)[0]
            out["字节码VM"] = ce._run(
                lambda: VM(compile_source(_脚本, rel), rel).run(), lines)[0]
            out["C VM"] = ce._run(
                lambda: cvm_bind.run_source_c(_脚本, rel), lines)[0]
        if RUST_EXE.is_file():
            out["Rust VM"] = ce._run_rust_vm(_脚本, rel, run_dir)[0]
        out["Node"] = ce._run_node_vm(_脚本, rel, run_dir)[0]
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)

    期望 = _期望_脚本()
    for 名, 值 in out.items():
        assert 值 == 期望, f"{名} 不一致：\n{值!r}\n期望：\n{期望!r}"


# ---------------------------------------------------------------------------
# 七、探针真的能跑（文件批处理）
# ---------------------------------------------------------------------------

def test_探针_文件批处理_整理归档(monkeypatch, tmp_path):
    样本 = tmp_path / "样本"
    样本.mkdir()
    (样本 / "报告A.md").write_text("A", encoding="utf-8")
    (样本 / "报告B.md").write_text("B", encoding="utf-8")
    (样本 / "旧报告.md").write_text("旧", encoding="utf-8")
    (样本 / "app.log").write_text("log", encoding="utf-8")

    探针 = (ROOT / "examples" / "绕道探针" / "文件批处理" / "整理.jsh")
    src = 探针.read_text(encoding="utf-8")
    buf = io.StringIO()
    monkeypatch.chdir(tmp_path)
    with redirect_stdout(buf):
        run_source(src, filename="整理.jsh")
    out = buf.getvalue()

    assert "找到 2 份待整理的报告" in out, out
    assert "已打包 报告.zip" in out, out
    归档 = sorted(p.name for p in (tmp_path / "归档").iterdir())
    assert "2026-报告A.md" in 归档 and "2026-报告B.md" in 归档, 归档
    assert "2026-旧报告.md" not in 归档, 归档
    assert (样本 / "旧报告.md").is_file(), "不符合模式的文件不该被动"
    assert "共 2 份报告" in (tmp_path / "归档" / "索引.md").read_text(
        encoding="utf-8")
    # 原子写不留 .tmp
    assert [p for p in (tmp_path / "归档").iterdir() if p.suffix == ".tmp"] == []
