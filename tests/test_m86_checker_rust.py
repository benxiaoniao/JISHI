# -*- coding: utf-8 -*-
"""R7.1-c：源码静态检查搬进 Rust —— **与 Python 侧逐字对拍 + 符号表漂移检测**。

R7 的验收是「编辑器不再依赖 Python」。静态检查是第二件（第一件是错误码查询，
见 `test_m85`），也是 LSP 实时诊断要用到的那条规则（`scope.py` 那套）——
只是这里换成了 Rust 实现。

判据：

* **两条输出形态都逐字一致**：人类可读的文本、以及 `--json`（含 `indent=2`
  的排版与退出码约定）；拿**仓库里的真语料**当输入（`tests/cases` /
  `tests/error_cases` / `tests/frontend_cases` / `examples`）；
* **四条规则都真的会触发**：语料太干净（只有语法错那批会报），所以另造一份
  「四条都中」的夹具单独比 —— 否则「实现漏了一条规则」这种错会被
  「两边都没报」掩盖过去；
* **符号表不许漂**：`jishi-rs --dump-check-symbols` 自报家底，与 Python 侧
  `langdata.LangData` 的四个集合（关键字 / 内建 / 标准库模块 / 异常类型）
  **逐项相等** —— 这四张表决定了「未定义名 / 遮蔽内建」的判据，差一个名字就会
  在同一份文件上给出不同结论。

⚠️ 已知**有意的**差异（不在判据里）：文件读不出来时的消息文案（Python 是
`OSError` 的 repr、Rust 是 `io::Error` 的 Display）—— 那是运行时的措辞，
不是语言行为。测试只比 `check.read` 那条的**码 / 级别 / 行列**。
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: 真语料（覆盖：干净代码 / 语法错 / 前端语料 / 教程示例）
TARGETS = ("tests/cases", "tests/error_cases", "tests/frontend_cases", "examples")

#: 四条规则各中一次的夹具（语料太干净，触不全）
_FIXTURE = """令 类型 = 1
令 用了的 = 2
函数 收(参)：
    令 局部没用 = 参
    返回 用了的
遍历 项 在 [1, 2]：
    打印(项)
如果 用了的 == 用了的：
    打印(类型, 收(1))
打印(来自别的文件的名字)
"""

#: 纯语法错（走「语法错也当一条问题返回」那条路）
_BAD = "令 甲 = \n"


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


def _rust_check(*argv: str):
    r = subprocess.run([str(_rust()), "源码静态检查", *argv], capture_output=True,
                       input=b"", env=_env(), cwd=str(ROOT))
    return (r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"), r.returncode)


def _py_check(*argv: str):
    r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", "源码静态检查",
                        *argv], capture_output=True, input=b"", env=_env(),
                       cwd=str(ROOT))
    return (r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"), r.returncode)


def _diff_note(a: str, b: str) -> str:
    """把第一处不同的行找出来（报错信息里最有用的一条线索）。"""
    la, lb = a.splitlines(), b.splitlines()
    for i in range(max(len(la), len(lb))):
        x = la[i] if i < len(la) else "<无>"
        y = lb[i] if i < len(lb) else "<无>"
        if x != y:
            return f"第 {i + 1} 行不同：\n  Rust: {x!r}\n  Py  : {y!r}"
    return "行数相同但整体不等（末尾换行？）"


# ---------------------------------------------------------------------------
# 一、符号表漂移检测
# ---------------------------------------------------------------------------


def _host_exempt() -> frozenset:
    """Rust 宿主**有意不实现**的标准库模块（现在是空的 —— `测试` 已补上）。

    ⚠️ 直接 import `test_m51_consistency.HOST_EXEMPT_BY_HOST["Rust"]` —— 那是
    这件事的**单一来源**。**别在这里再抄一份**：本项目为「两处各自解释同一件
    事」付过代价。谁改了 m51 的豁免，这里一起动。
    """
    sys.path.insert(0, str(ROOT / "tests"))
    from test_m51_consistency import HOST_EXEMPT_BY_HOST
    return HOST_EXEMPT_BY_HOST["Rust"]


@_need_rust
def test_漂移检测_四个符号集合与Python一致():
    """四个集合决定「未定义名 / 遮蔽内建」的判据，**差一个名字就会给出不同结论**。"""
    from oracle.jishi.langdata import LangData

    r = subprocess.run([str(_rust()), "--dump-check-symbols"], capture_output=True,
                       input=b"", env=_env(), cwd=str(ROOT))
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    got = json.loads(r.stdout.decode("utf-8"))

    ld = LangData()
    exempt = _host_exempt()
    # 宿主**没有**的模块（m51 已登记的有意豁免）：静态检查在两边会给出不同结论
    # 是**有意的** —— 宿主的单二进制里确实没有那个模块，报「未定义」是真话。
    want = {
        "keywords": sorted(ld.keywords),
        "builtins": sorted(ld.builtins),
        "stdlib": sorted(set(ld.stdlib) - set(exempt)),
        "exceptions": sorted(ld.exceptions),
    }
    # 差集必须**恰好**是登记的那几个 —— 冒出新的立刻报红。
    missing = sorted(set(ld.stdlib) - set(got.get("stdlib", [])))
    assert missing == sorted(exempt), (
        f"标准库集合的差集与 m51 登记的豁免对不上：\n"
        f"  实际缺：{missing}\n  登记的：{sorted(exempt)}")
    for key in want:
        assert got.get(key) == want[key], (
            f"符号集合「{key}」漂了：\n  Rust 独有：{sorted(set(got.get(key, [])) - set(want[key]))}\n"
            f"  Python 独有：{sorted(set(want[key]) - set(got.get(key, [])))}\n"
            "（Rust 侧那几处：内建 ← `rust/src/lib.rs::builtin_names()`、"
            "关键字 ← `frontend/src/tokenizer.rs::KEYWORDS`、"
            "标准库 ← `stdlib::all_modules()`、异常 ← `EXCEPTION_NAMES`）")


# ---------------------------------------------------------------------------
# 二、真语料逐字对拍（文本 + --json）
# ---------------------------------------------------------------------------


@_need_rust
@pytest.mark.parametrize("target", TARGETS)
def test_文本输出逐字一致(target):
    rs = _rust_check(target)
    py = _py_check(target)
    assert rs == py, f"{target}（文本）不一致：\n{_diff_note(rs[0], py[0])}"


@_need_rust
@pytest.mark.parametrize("target", TARGETS)
def test_json输出逐字一致(target):
    rs = _rust_check(target, "--json")
    py = _py_check(target, "--json")
    assert rs == py, f"{target}（--json）不一致：\n{_diff_note(rs[0], py[0])}"
    if rs[0].strip():
        json.loads(rs[0])          # 顺带钉住「它确实是合法 JSON」


# ---------------------------------------------------------------------------
# 三、四条规则都要真的触发（不能用「两边都没报」蒙混过关）
# ---------------------------------------------------------------------------


@_need_rust
def test_四条规则都被触发且与Python一致():
    d = ROOT / "build" / f"_m86_{os.getpid()}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "四条.jsh").write_text(_FIXTURE, encoding="utf-8")
    (d / "语法错.jsh").write_text(_BAD, encoding="utf-8")
    try:
        rs = _rust_check(str(d.relative_to(ROOT)), "--json")
        py = _py_check(str(d.relative_to(ROOT)), "--json")
        assert rs == py, f"夹具不一致：\n{_diff_note(rs[0], py[0])}"
        payload = json.loads(rs[0])
        codes = sorted({i["code"] for i in payload["issues"]})
        assert codes == ["E0201", "compare.self", "name.shadow",
                         "name.undefined", "name.unused"], codes
        assert rs[2] == 1, "发现问题必须退码 1（供 CI / 提交钩子用）"
    finally:
        for f in d.glob("*.jsh"):
            f.unlink()
        d.rmdir()


@_need_rust
def test_没问题时退码0():
    """干净文件：报「没发现问题」且退码 0（与 Python 一致）。"""
    d = ROOT / "build" / f"_m86_ok_{os.getpid()}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "干净.jsh").write_text("打印(1)\n", encoding="utf-8")
    try:
        rel = str(d.relative_to(ROOT))
        rs = _rust_check(rel)
        py = _py_check(rel)
        assert rs == py, f"{_diff_note(rs[0], py[0])}"
        assert rs[2] == 0 and "没发现问题" in rs[0]
    finally:
        (d / "干净.jsh").unlink()
        d.rmdir()
