# -*- coding: utf-8 -*-
"""R7.1-d：代码格式化搬进 Rust —— `jishi-rs 格式化` vs `jishi 格式化`。

两半（用户 2026-10-05 定的纪律：**Rust 最终要替代 Python，判据不能只押在对拍上**）：

* **对拍**（§一~§四）：全语料**逐字节**一致 + 各模式（--check / --stdin /
  --indent / 目录）+ 符号集合**漂移检测**（`--dump-format-tables`）；
* **独立判据**（§五）：不 import oracle.jishi、不 spawn Python 的**写死期望** ——
  R8 之后 Python 退场，这一半仍要绿。

格式化器的验收原文就是「**全语料逐字节一致**」+「**幂等**」（`formatter.py` 顶注）。
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: 与 Python 侧对拍用的语料目录（**真语料**，别只挑“干净”的）。
_CORPUS_DIRS = ["tests/cases", "tests/error_cases", "tests/frontend_cases",
                "examples", "oracle/jishi/stdlib-jishi"]


def _rust() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


def _need_rust(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not _rust().exists():
            pytest.skip("Rust 宿主未构建（cd rust && cargo build --release）")
        return fn(*args, **kwargs)
    return wrapper


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    return env


def _rust_run(*argv: str, stdin: bytes = b"") -> tuple[str, str, int]:
    r = subprocess.run([str(_rust()), *argv], capture_output=True, input=stdin,
                       env=_env(), cwd=str(ROOT))
    return (r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"), r.returncode)


def _py_run(*argv: str, stdin: bytes = b"") -> tuple[str, str, int]:
    r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", *argv],
                       capture_output=True, input=stdin, env=_env(), cwd=str(ROOT))
    return (r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"), r.returncode)


def _corpus() -> list[Path]:
    files: list[Path] = []
    for d in _CORPUS_DIRS:
        p = ROOT / d
        if p.is_dir():
            files += sorted(x for x in p.rglob("*.jsh") if x.is_file())
    return files


# ---------------------------------------------------------------------------
# 一、全语料逐字节一致（文本输出 + 退出码）
# ---------------------------------------------------------------------------


@_need_rust
def test_全语料格式化逐字节一致():
    """每个 .jsh 单文件格式化，`jishi-rs 格式化` 与 `jishi 格式化` **逐字节**一致。

    ⚠️ 语料要**够多**：光挑几个“干净”的样例掩盖不了「某类 token 的间距规则漂了」。
    这里覆盖 cases / error_cases / frontend_cases / examples / stdlib-jishi ——
    含**词法错**的文件（error_cases），那条路走的是「错误渲染」，一并钉住。
    """
    files = _corpus()
    assert len(files) >= 100, f"语料太少（{len(files)}）—— 判据是不是没覆盖到？"
    bad: list[str] = []
    for f in files:
        a = _rust_run("格式化", str(f))
        b = _py_run("格式化", str(f))
        if a != b:
            bad.append(str(f.relative_to(ROOT)))
            if len(bad) == 1:
                print("首个不一致：", f)
                for name, x, y in (("stdout", a[0], b[0]), ("stderr", a[1], b[1]),
                                   ("退出码", str(a[2]), str(b[2]))):
                    if x != y:
                        print(f"  [{name}] rust={x!r}\n         py  ={y!r}")
    assert not bad, f"格式化输出不一致 {len(bad)} 处：{bad[:5]}"


# ---------------------------------------------------------------------------
# 二、幂等（验收原文的另一半）
# ---------------------------------------------------------------------------


@_need_rust
def test_幂等_格式化两次等于一次(tmp_path: Path):
    """把 `jishi-rs` 的输出**再喂一次**，逐字节不变。"""
    bad: list[str] = []
    for f in _corpus():
        a = _rust_run("格式化", str(f))
        if a[2] != 0:
            continue  # 词法错的文件没有格式化输出，跳过
        out = tmp_path / "再格式化.jsh"
        out.write_text(a[0], encoding="utf-8")
        c = _rust_run("格式化", str(out))
        if c[0] != a[0] or c[2] != 0:
            bad.append(str(f.relative_to(ROOT)))
    assert not bad, f"不幂等 {len(bad)} 处：{bad[:5]}"


# ---------------------------------------------------------------------------
# 三、各模式与 Python 一致（--check / --stdin / --indent / 目录）
# ---------------------------------------------------------------------------


@_need_rust
def test_模式_check_stdin_indent(tmp_path: Path):
    mis = tmp_path / "未格式化.jsh"
    mis.write_text("令 甲=1\n如果 甲>0：\n  打印( 甲 )\n", encoding="utf-8")
    good = sorted((ROOT / "tests" / "cases").glob("*.jsh"))[0]
    src = mis.read_bytes()
    cases = [
        ("--check 已格式化", ["格式化", str(good), "--check"], b""),
        ("--check 未格式化", ["格式化", str(mis), "--check"], b""),
        ("--stdin", ["格式化", "--stdin"], src),
        ("--indent 2", ["格式化", str(mis), "--indent", "2"], b""),
        ("--stdin --check（未格式化）", ["格式化", "--stdin", "--check"], src),
    ]
    for tag, args, stdin in cases:
        a = _rust_run(*args, stdin=stdin)
        b = _py_run(*args, stdin=stdin)
        assert a == b, f"{tag}：\n  rust={a!r}\n  py  ={b!r}"


@_need_rust
def test_目录_check一致():
    """目录模式：总结行 + 文件清单 + 退出码逐字一致（排序口径也要一致）。"""
    d = "tests/cases"
    a = _rust_run("格式化", d, "--check")
    b = _py_run("格式化", d, "--check")
    assert a == b, f"目录 --check：\n  rust={a!r}\n  py  ={b!r}"


@_need_rust
def test_目录_write一致(tmp_path: Path):
    """目录 `--write`：两边各写一份副本，**每个文件的内容**都要一致。"""
    import shutil

    src_dir = ROOT / "tests" / "frontend_cases"
    dr = tmp_path / "dr"
    dp = tmp_path / "dp"
    shutil.copytree(src_dir, dr)
    shutil.copytree(src_dir, dp)
    a = _rust_run("格式化", str(dr), "--write")
    b = _py_run("格式化", str(dp), "--write")
    assert a[2] == b[2], f"退出码：{a[2]} vs {b[2]}"
    for rel in (p.relative_to(dr) for p in dr.rglob("*.jsh")):
        assert (dr / rel).read_bytes() == (dp / rel).read_bytes(), f"内容不同：{rel}"


# ---------------------------------------------------------------------------
# 四、符号集合的漂移检测（R7.1-b 起那条流水线）
# ---------------------------------------------------------------------------


@_need_rust
def test_漂移检测_格式化符号集合与Python一致():
    """运算符 / 关键字 / 引号这些**集合**差一个，格式化结论就可能不同。

    加一个运算符只改一侧时，这条会报红 —— 否则要等语料**恰好用到它**才暴露
    （`formatter.py` 里那些 `_BINARY_OPS` 之类，正是「两边各写一份」的高危处）。
    """
    from oracle.jishi import formatter as F

    a = _rust_run("--dump-format-tables")
    assert a[2] == 0, a[1]
    got = json.loads(a[0])
    want = {
        "no_space_before": sorted(F._NO_SPACE_BEFORE),
        "no_space_after": sorted(F._NO_SPACE_AFTER),
        "binary_ops": sorted(F._BINARY_OPS),
        "sign_ops": sorted(F._SIGN_OPS),
        "assign_ops": sorted(F._ASSIGN_OPS),
        "value_keywords": sorted(F._VALUE_KEYWORDS),
        "triple_quotes": sorted(F._TRIPLE_QUOTES),
        "paired": sorted([list(x) for x in F._PAIRED.items()]),
    }
    assert got == want


# ---------------------------------------------------------------------------
# 五、独立判据（**不 import oracle.jishi、不 spawn Python** —— R8 之后仍要绿）
# ---------------------------------------------------------------------------

#: 人工核对过（与 Python 侧逐字比过）之后写死的期望。
_BASIC_IN = ("令 甲=1\n如果 甲>0：\n  打印( 甲 ,\"全角：测试\" )   # 注释留着\n"
             "否则：\n  打印( [1,2, 3] )\n")
_BASIC_OUT = ("令 甲 = 1\n如果 甲 > 0：\n    打印(甲, \"全角：测试\")  # 注释留着\n"
              "否则：\n    打印([1, 2, 3])\n")


@_need_rust
def test_独立判据_基本排版(tmp_path: Path):
    f = tmp_path / "a.jsh"
    f.write_text(_BASIC_IN, encoding="utf-8")
    out, err, rc = _rust_run("格式化", str(f))
    assert (out, err, rc) == (_BASIC_OUT, "", 0)


@_need_rust
def test_独立判据_幂等(tmp_path: Path):
    f = tmp_path / "a.jsh"
    f.write_text(_BASIC_OUT, encoding="utf-8")
    out, err, rc = _rust_run("格式化", str(f))
    assert (out, err, rc) == (_BASIC_OUT, "", 0)


@_need_rust
def test_独立判据_保留注释与字符串原样(tmp_path: Path):
    """字符串里的 `#` 不是注释（M38 B4）；引号风格（含中文引号）原样保留。"""
    f = tmp_path / "a.jsh"
    f.write_text('令 甲 = "含 # 号"\n令 乙 = 「中文引号」\n', encoding="utf-8")
    out, err, rc = _rust_run("格式化", str(f))
    assert (out, err, rc) == ('令 甲 = "含 # 号"\n令 乙 = 「中文引号」\n', "", 0)


@_need_rust
def test_独立判据_多字符下划线():
    """词法错的**多字符下划线**（`~^`）—— 与 Python 同形。

    ⚠️ 这条钉的是 `render_error_with` 对 `underline` 的处理：2026-10-05 之前
    宿主**没有**这个字段，渲染出来只有一个 `^`（少几个 `~`）。
    """
    src = "如果分数 > 60：\n    打印(1)\n"
    out, err, rc = _rust_run("格式化", "--stdin", stdin=src.encode("utf-8"))
    assert rc == 1 and out == ""
    assert err == (
        "错误 E0106：关键字与后面的内容粘连了（「如果分数」被当成了一个名字，"
        "但它的开头是关键字「如果」）\n"
        "  ┌─ <stdin>:1:1\n"
        "  │\n"
        "  1 │ 如果分数 > 60：\n"
        "    │ ~^\n"
        "  │\n"
        "  提示：你是不是想写「如果 分数」？关键字和后面的内容之间要留空格\n"
    ), repr(err)


@_need_rust
def test_独立判据_没有PATH也能跑(tmp_path: Path):
    """清空环境也要能格式化 —— 它全靠二进制自己，不 shell out 找 python/node。"""
    f = tmp_path / "a.jsh"
    f.write_text("令 甲=1\n", encoding="utf-8")
    env = {"SystemRoot": os.environ.get("SystemRoot", "C:\\Windows")} \
        if os.name == "nt" else {}
    r = subprocess.run([str(_rust()), "格式化", str(f)], capture_output=True,
                       input=b"", env=env, cwd=str(ROOT))
    assert r.returncode == 0
    assert r.stdout.decode("utf-8") == "令 甲 = 1\n"
