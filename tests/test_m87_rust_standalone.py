# -*- coding: utf-8 -*-
"""Rust 工具链的**独立判据** —— 不依赖 Python、不 import oracle.jishi、不 spawn Python。

## 为什么单独一份（用户 2026-10-05 提醒）

`test_m85`（错误码）与 `test_m86`（静态检查）是「**与 Python 对拍**」——
它们能抓住漂移，但**前提是 Python 还在**。而 R 线的终点是 **Rust 替代 Python**
（R8：单二进制、退役 Python），到那时那些对拍测试要么删掉、要么变成空壳。

所以这里把**「Rust 自己该输出什么」写成写死的期望**：只有本仓库的
`jishi-rs` 二进制，期望值是人工核对过（创建时与 Python 侧逐字比过）之后写进来的。
**Python 退场之后，这一份仍然要绿。**

⚠️ 判据里**不许出现**：`import oracle.jishi`、`sys.executable`、`-m oracle.jishi.cli`、
或者任何「让 Python 算一个期望值」的写法 —— 那等于把判据又押回 Python 上。
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
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


def _run(*argv: str, env: dict | None = None):
    if env is None:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([str(_rust()), *argv], capture_output=True, input=b"",
                       env=env, cwd=str(ROOT))
    return (r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"), r.returncode)


# ---------------------------------------------------------------------------
# 一、`jishi-rs 错误 [码]`（R7.1-b）
# ---------------------------------------------------------------------------

#: E0301 的**完整**输出（人工核对过；Python 退场后这就是唯一判据）
_E0301 = """E0301  找不到这个名字

什么意思
  用了一个「没定义过」的名字（变量 / 函数 / 内建 / 标准库模块）。

常见成因
  · 名字打错了 —— 报错里常会带「你是不是想写「X」？」
  · 标准库忘了导入（内建不用导入，但 `数学.开方` 要先 `导入 数学`）
  · 写成了英文名（基石的内建是中文：`长度` 不是 `len`）

怎么改
  照报错给的候选名改；想看有哪些名字，跑 `jishi --ai-card`。
"""

#: 段的标题（总览的五个分组）
_SECTIONS = ("【E01xx · 词法】", "【E02xx · 语法】", "【E03xx · 语义】",
             "【E20xx · 运行期】", "【E29xx · 内部信号】")


@_need_rust
def test_错误码详情_写死的期望():
    out, err, code = _run("错误", "E0301")
    assert out == _E0301, f"输出与写死的期望不符：\n{out!r}"
    assert err == "" and code == 0


@_need_rust
def test_错误码总览_结构与条数():
    out, err, code = _run("错误")
    assert err == "" and code == 0
    assert out.startswith("基石错误码总览（32 个）\n\n"), out[:60]
    for head in _SECTIONS:
        assert head in out, f"总览里少了分段：{head}"
    # 每个码只出现一次（`  CODE  ` 这种排法）
    for code_ in ("E0101", "E0201", "E0301", "E2009", "E2999"):
        assert out.count(f"  {code_}  ") == 1, f"{code_} 出现次数不对"
    assert out.endswith("看某个码的详情：jishi 错误 E0301\n"
                        "（报错里给的那串 `E0301` 就是可以直接贴进来的码）\n")


@_need_rust
@pytest.mark.parametrize("arg", ["E9999", "8888"])
def test_错误码_没有这个码走stderr退码2(arg):
    out, err, code = _run("错误", arg)
    assert out == ""
    assert err == (f"没有错误码「{arg.upper() if arg.startswith('E') else 'E' + arg}」"
                   f"—— 跑 `jishi 错误` 看全部 32 个。\n"), err
    assert code == 2


@_need_rust
def test_错误码_只写数字也认():
    for arg in ("301", "0301", "e0301"):
        out, err, code = _run("错误", arg)
        assert out == _E0301, f"「{arg}」没归一到 E0301：{out[:40]!r}"
        assert code == 0


# ---------------------------------------------------------------------------
# 二、`jishi-rs 源码静态检查 [路径]`（R7.1-c；R7.6 起与 Python 同名）
# ---------------------------------------------------------------------------

#: 四条规则各中一次的夹具
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


def _expect_check_text(rel: str) -> str:
    return (
        f"{rel}:1:3: [warning] name.shadow：变量「类型」遮蔽了内建函数「类型」\n"
        "    提示：这个作用域里原来的「类型」就被挡住了——换个名字"
        "（如「我的类型」）能避免后面的人看错。\n"
        f"{rel}:4:7: [warning] name.unused：变量「局部没用」"
        "在函数「收」里绑定后从未被读取\n"
        "    提示：删掉这个绑定，或确认它是不是本该赋给别的名字。\n"
        f"{rel}:8:4: [warning] compare.self：「用了的 == 用了的」两边是同一个名字，"
        "这个比较的结果是恒定的\n"
        "    提示：是不是该拿另外两个值来比？\n"
        f"{rel}:10:4: [warning] name.undefined：名字「来自别的文件的名字」"
        "在这份文件里没有定义过\n"
        "    提示：要么是拼错了，要么它来自别的文件（本命令只做单文件分析）；"
        "若来自包依赖，那是运行时注入的，静态看不到。\n"
        "\n共 4 个问题（错误 0 个 / 警告 4 个），涉及 1 个文件。\n"
        "（只是提醒，不改你的代码；确认无误可以照常运行）\n"
    )


@_need_rust
def test_静态检查_四条规则的输出是写死的期望():
    d = ROOT / "build" / f"_m87_{os.getpid()}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "四条.jsh").write_text(_FIXTURE, encoding="utf-8")
    try:
        rel = str(d.relative_to(ROOT))
        if os.name == "nt":
            rel = rel.replace("/", "\\")
        out, err, code = _run("源码静态检查", rel)
        assert out == _expect_check_text(rel + "\\四条.jsh") if os.name == "nt" \
            else out == _expect_check_text(rel + "/四条.jsh")
        assert err == "" and code == 1
    finally:
        (d / "四条.jsh").unlink()
        d.rmdir()


@_need_rust
def test_静态检查_json形态字段齐全():
    d = ROOT / "build" / f"_m87j_{os.getpid()}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "四条.jsh").write_text(_FIXTURE, encoding="utf-8")
    try:
        out, err, code = _run("源码静态检查", str(d.relative_to(ROOT)), "--json")
        assert err == "" and code == 1
        payload = json.loads(out)                 # 顺带钉住「是合法 JSON」
        assert payload["ok"] is False
        codes = sorted({i["code"] for i in payload["issues"]})
        assert codes == ["compare.self", "name.shadow", "name.undefined",
                         "name.unused"], codes
        first = payload["issues"][0]
        assert sorted(first) == ["code", "col", "file", "hint", "level",
                                 "line", "message", "name"], sorted(first)
        assert first["level"] == "warning" and first["col"] == 3
    finally:
        (d / "四条.jsh").unlink()
        d.rmdir()


@_need_rust
def test_静态检查_语法错也当一条问题():
    d = ROOT / "build" / f"_m87b_{os.getpid()}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "坏.jsh").write_text("令 甲 = \n", encoding="utf-8")
    try:
        out, err, code = _run("源码静态检查", str(d.relative_to(ROOT)))
        assert code == 1 and err == ""
        assert "[error] E0201：" in out, out
        assert "共 1 个问题（错误 1 个 / 警告 0 个）" in out
    finally:
        (d / "坏.jsh").unlink()
        d.rmdir()


@_need_rust
def test_静态检查_干净文件退码0():
    d = ROOT / "build" / f"_m87o_{os.getpid()}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "干净.jsh").write_text("打印(1)\n", encoding="utf-8")
    try:
        rel = str(d.relative_to(ROOT))
        out, err, code = _run("源码静态检查", rel)
        assert out == "检查了 1 个路径，没发现问题。\n"
        assert err == "" and code == 0
    finally:
        (d / "干净.jsh").unlink()
        d.rmdir()


# ---------------------------------------------------------------------------
# 三、运行期**不需要外部工具**（单二进制的底线）
# ---------------------------------------------------------------------------


@_need_rust
def test_没有PATH也能跑():
    """把环境清空（不留 PATH）也要能跑 —— 这两条命令全靠二进制自己，
    不 shell out 找 python / node / git。

    ⚠️ Windows 上进程要 `SystemRoot` 才能起来，所以只留它；
    其余平台给空环境即可。
    """
    env = {"SystemRoot": os.environ.get("SystemRoot", "C:\\Windows")} \
        if os.name == "nt" else {}
    out, err, code = _run("错误", "E0301", env=env)
    assert out == _E0301 and code == 0, (out[:80], err[:200], code)
