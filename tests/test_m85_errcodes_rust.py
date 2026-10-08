# -*- coding: utf-8 -*-
"""R7.1-b：错误码查询搬进 Rust —— **与 Python 侧逐字对拍 + 漂移检测**。

R7 的验收是「编辑器不再依赖 Python」。这一件是最先落地的（纯数据 + 纯文本，
不碰执行器），同时把「**Rust 侧显式表 + 两侧对拍**」这条流水线打通 ——
后面静态检查 / 格式化都照这条来。

两条判据：

* **输出逐字一致**：每个码（含总览、含「没有这个码」、含只写数字的别名写法）
  的 stdout / stderr / 退出码都与 `jishi 错误` 完全相同；
* **两张表不许漂**：`jishi-rs --dump-errcodes` 自报家底，与 Python 侧
  `errors.py`（码 + 标题）+ `errcodes.py`（说明）**逐字段相等**。
  📌 谁改了哪一侧都会报红 —— 这是「同一件事只该有一份」的落点，本项目为
  「两处各自解释同一份元数据」付过代价（见 AGENTS §2）。
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


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


def _rust_errcode(*argv: str):
    """跑 `jishi-rs 错误 …` → `(stdout, stderr, 退出码)`（都是解码后的文本）。"""
    r = subprocess.run([str(_rust()), "错误", *argv], capture_output=True,
                       input=b"", env=_env(), cwd=str(ROOT))
    return (r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"), r.returncode)


def _py_errcode(*argv: str):
    r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", "错误", *argv],
                       capture_output=True, input=b"", env=_env(), cwd=str(ROOT))
    return (r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"), r.returncode)


def _py_tables():
    """Python 侧的两张表（**与 `cli._all_error_codes` 同一套抽法**）。"""
    from oracle.jishi import errcodes as EC
    from oracle.jishi import errors as E

    codes: dict[str, str] = {}
    for obj in vars(E).values():
        if (inspect.isclass(obj) and issubclass(obj, E.JishiError)
                and obj is not E.JishiError):
            codes.setdefault(obj.code, obj.title)
    return EC.SECTIONS, sorted(codes.items()), EC.INFO


@_need_rust
def test_漂移检测_两张表与Python逐字段相等():
    """Rust 自报家底 vs Python 侧的表 —— **段、码、说明三样都要相等**。"""
    r = subprocess.run([str(_rust()), "--dump-errcodes"], capture_output=True,
                       input=b"", env=_env(), cwd=str(ROOT))
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    got = json.loads(r.stdout.decode("utf-8"))

    sections, codes, info = _py_tables()
    want = {
        "sections": [list(s) for s in sections],
        "codes": [list(c) for c in codes],
        "info": [[code, {"what": d["what"], "why": list(d["why"]),
                         "fix": d["fix"]}] for code, d in info.items()],
    }
    assert got == want, (
        "错误码表漂了 —— `rust/src/errcodes.rs` 与 "
        "Python 侧（errors.py / errcodes.py）必须一致。\n"
        "  改了 Python 侧：跑 `python tools/gen_errcodes_rs.py` 重新搬运，"
        "**看完 diff** 再提交；\n"
        "  改了 Rust 侧：同样要回头改 Python 侧（两边都是给人看的文档）。")


@_need_rust
def test_每个码的输出逐字一致():
    """逐个码比 `jishi-rs 错误 <码>` 与 `jishi 错误 <码>` 的 stdout。"""
    _, codes, _ = _py_tables()
    assert codes, "Python 侧一个码都没抽到？"
    for code, _title in codes:
        rs = _rust_errcode(code)
        py = _py_errcode(code)
        assert rs == py, f"「{code}」两边输出不一致：\n  Rust: {rs!r}\n  Py  : {py!r}"


@_need_rust
def test_总览逐字一致():
    rs = _rust_errcode()
    py = _py_errcode()
    assert rs == py, f"总览不一致：\n  Rust: {rs[0][:300]!r}\n  Py  : {py[0][:300]!r}"
    assert "基石错误码总览" in rs[0]


@_need_rust
@pytest.mark.parametrize("arg", ["E9999", "9999", "E42"])
def test_没有这个码_走stderr且退码2(arg):
    rs = _rust_errcode(arg)
    py = _py_errcode(arg)
    assert rs == py, f"「{arg}」两边不一致：\n  Rust: {rs!r}\n  Py  : {py!r}"
    assert rs[0] == "" and rs[2] == 2, "「没有这个码」必须走 stderr 且退码 2"


@_need_rust
@pytest.mark.parametrize("arg", ["301", "E301", "0301", "e0301", " e0301 "])
def test_只写数字或小写也能查到(arg):
    """用户从报错里抄码时常只抄了 `0301` —— 别名写法两边都要认。"""
    rs = _rust_errcode(arg)
    py = _py_errcode(arg)
    assert rs == py, f"「{arg}」两边不一致：\n  Rust: {rs!r}\n  Py  : {py!r}"
    assert "E0301" in rs[0], f"「{arg}」没归一到 E0301：{rs[0][:80]!r}"
    assert rs[2] == 0
