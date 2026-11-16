# -*- coding: utf-8 -*-
"""默认参数可以写 `空` —— 「没给参数」与「给的是 `空`」必须分得开。

## 这条判据要防什么

🔴 2026-10-11 修的真 bug：四个执行器（Python 树遍历 / Python 字节码 VM /
Rust VM / Node）在**运行期**用 `None`/`null` 同时表示两件事：

* 「这个形参没有默认值」；
* 「这个形参的默认值就是 `空`」。

而 `空` 在值层面**就是** `None` ⇒ `函数 f(a, b = 空)` 在 `f(1)` 时被判成
「缺少参数：b」。**C VM 一直是按「下标折算」（`first_def = npos - ndefaults`）
做的，所以只有它是对的** —— 这条差异只在「默认值写成 `空`」时才现形，
而 `oracle/jishi/stdlib-jishi/参数.jsh` 里就有 `参数们 = 空`，是一直踩着的。

修法：运行期一律存**紧凑尾部**（只留有默认值的那些），按下标补，
不再需要任何哨兵。AST 那层不用改 —— 那里 `None`/`null` 只表示「没默认值」，
写成 `空` 的是 `Name` 节点，本来就分得开（`test_AST_那份用空当占位是无歧义的`
钉住这条前提）。

## 判据分四层

1. **语义**（三种进程内执行器一致）；
2. **边界**：默认值可以是 `空` / `假` / `[]` / 文本，且都能和「没给」区分；
3. **五条执行器逐字节一致**（含 Rust 字节码 VM / Rust 树遍历 / Node）；
4. **源码级**：别再退回「拿 `None`/`null` 当哨兵」的写法。
"""

import io
import json
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

from conftest import rust_exe_path, tmp_bc_path   # noqa: E402

from oracle.jishi import cvm_bind                 # noqa: E402
from oracle.jishi import serialize                # noqa: E402
from oracle.jishi import vm as vm_mod             # noqa: E402
from oracle.jishi.compiler import compile_source  # noqa: E402
from oracle.jishi.errors import RunTypeError      # noqa: E402
from oracle.jishi.interpreter import run_source   # noqa: E402

RUST_EXE = rust_exe_path()

#: 语料（黄金文件）：`28_默认值可为空`。五条执行器都对它，期望输出就是 `.out`。
语料 = ROOT / "tests" / "cases" / "28_默认值可为空.jsh"
期望 = (ROOT / "tests" / "cases" / "28_默认值可为空.out").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 三种进程内执行器
# ---------------------------------------------------------------------------

def _树(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        run_source(src, filename="<t>")
    return buf.getvalue()


def _pvm(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        vm_mod.VM(compile_source(src, "<t>"), "<t>").run()
    return buf.getvalue()


def _cvm(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        cvm_bind.run_source_c(src, "<t>")
    return buf.getvalue()


def _三一致(src: str) -> str:
    got = {"树遍历": _树(src), "Python VM": _pvm(src), "C VM": _cvm(src)}
    assert len(set(got.values())) == 1, f"三执行器输出不一致：{got}"
    return next(iter(got.values()))


# ---------------------------------------------------------------------------
# 一、语义：默认值是「空」时，省略参数 ≠ 报错
# ---------------------------------------------------------------------------

def test_省略参数时默认值取到空():
    assert _三一致(
        '函数 f(a, b = 空)：\n    返回 [a, b]\n'
        '打印(f(1))\n打印(f(1, 2))\n'
    ) == "[1, 空]\n[1, 2]\n"


def test_只空这个默认值本身():
    assert _三一致(
        '函数 f(甲 = 空)：\n    返回 甲\n'
        '打印(f(), f(9))\n'
    ) == "空 9\n"


def test_空默认值与没给参数分得开():
    """核心回归：默认值 `空` 与「这个参数没有默认值」是两件事。

    前者可以不传；后者不传必须报「缺少参数」。
    """
    assert _三一致('函数 有默认(a, b = 空)：\n    返回 b\n打印(有默认(1))\n') == "空\n"

    with pytest.raises(RunTypeError) as ei:
        _树('函数 没默认(a, b)：\n    返回 b\n打印(没默认(1))\n')
    assert "缺少参数" in str(ei.value) and "b" in str(ei.value)


def test_默认值可以是假和空列表():
    assert _三一致(
        '函数 f(甲, 乙 = 假, 丙 = [], 丁 = ""):\n    返回 [甲, 乙, 丙, 丁]\n'
        '打印(f(1))\n打印(f(1, 真, [2], "x"))\n'
    ) == "[1, 假, [], '']\n[1, 真, [2], 'x']\n"


def test_默认值在定义处求值一次():
    """`函数 f(x = 记账())` 里的表达式只在**定义时**算一次 —— 与 Python 一致。

    ⚠️ 计数要放在**列表**里改（不能在函数体里 `计数 += 1`：那是全局名，
    会踩「赋值即局部」）。
    """
    assert _三一致(
        '令 记录 = []\n'
        '函数 记账()：\n'
        '    记录.追加(1)\n'
        '    返回 空\n'
        '函数 取(值 = 记账())：\n'
        '    返回 值\n'
        '打印(取(), 取(), 长度(记录))\n'
    ) == "空 空 1\n"


# ---------------------------------------------------------------------------
# 二、和 `*参数` / `**选项` 混用（默认值只在「普通参数」尾部）
# ---------------------------------------------------------------------------

def test_空默认值与收集参数混用():
    assert _三一致(
        '函数 f(甲, 乙 = 空, *余, **选项)：\n    返回 [甲, 乙, 余, 选项]\n'
        '打印(f(1))\n打印(f(1, 2))\n打印(f(1, 2, 3, 键 = 4))\n'
    ) == "[1, 空, [], {}]\n[1, 2, [], {}]\n[1, 2, [3], {'键': 4}]\n"


def test_普通默认值行为一字不变():
    """非 `空` 的默认值走的是同一条路 —— 改前后必须完全一致。"""
    assert _三一致(
        '函数 f(甲, 乙 = 10, *余)：\n    返回 [甲, 乙, 余]\n'
        '打印(f(1))\n打印(f(1, 2))\n打印(f(1, 2, 3, 4))\n'
    ) == "[1, 10, []]\n[1, 2, []]\n[1, 2, [3, 4]]\n"


def test_关键字调用时默认值仍生效():
    assert _三一致(
        '函数 f(甲, 乙 = 空, 丙 = 7)：\n    返回 [甲, 乙, 丙]\n'
        '打印(f(1))\n打印(f(甲 = 1, 丙 = 9))\n打印(f(1, 丙 = 9))\n'
    ) == "[1, 空, 7]\n[1, 空, 9]\n[1, 空, 9]\n"


# ---------------------------------------------------------------------------
# 三、五条执行器逐字节一致（含两个 Rust 侧与 Node）
# ---------------------------------------------------------------------------

def test_五执行器一致():
    """Python 三种 + Rust 两种 + Node 全部给出**同一份输出**。

    Rust 那两处（`lib.rs` 的字节码 VM 与 `walk.rs` 的树遍历）与 Node 的
    `vm.js` 都改过同一处哨兵逻辑，必须逐个对到。
    """
    src = 语料.read_text(encoding="utf-8")
    py = _三一致(src)
    assert py == 期望, f"Python 侧就不对：\n{py!r}\n期望：\n{期望!r}"

    if not RUST_EXE.is_file():
        pytest.skip("没有 rust 宿主产物（先 `cd rust && cargo build --release`）")

    bc = tmp_bc_path(ROOT / "_tmp_default_args_bc.json")
    bc.write_text(serialize.dumps(compile_source(src, "<t>")), encoding="utf-8")
    r = subprocess.run([str(RUST_EXE), "--load-bytecode", str(bc)],
                       capture_output=True)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:400]
    assert r.stdout.decode("utf-8", "replace") == 期望, "Rust VM 不一致"

    # Rust 树遍历：吃的是**冻结好的 AST**（与 `check_walk.py` 同法）
    walk = _walk_output()
    assert walk == 期望, f"Rust 树遍历不一致：\n{walk!r}"

    from shutil import which

    if which("node") is None:
        pytest.skip("环境里没有 node（Rust 两处已经对过了）")
    r3 = subprocess.run(
        ["node", str(ROOT / "oracle" / "node" / "index.js"), str(bc)],
        capture_output=True, text=True, encoding="utf-8")
    assert r3.returncode == 0, (r3.stderr or "")[:400]
    assert r3.stdout == 期望, f"Node 不一致：\n{r3.stdout!r}"


def _walk_output() -> str:
    """用冻结的一致性基线里的 `ast` 跑一遍 Rust 树遍历，返回 stdout。

    `--walk` 吃的是 **AST JSON**（不是源码）；源文件按同名相对路径放进私有
    cwd，这样报错渲染里的文件名与基线一致。做法与 `tools/check_walk.py` 相同。
    """
    rel = "tests/cases/28_默认值可为空.jsh"
    base = (ROOT / "tests" / "conformance" / "baseline" /
            (rel.replace("/", "__")[:-len(".jsh")] + ".json"))
    doc = json.loads(base.read_text(encoding="utf-8"))
    assert doc.get("ast"), f"基线里没有 AST：{base}"

    run_dir = Path(tempfile.gettempdir()) / f"default_args_walk_{os.getpid()}"
    shutil.rmtree(run_dir, ignore_errors=True)
    dst = run_dir / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / rel, dst)
    astp = run_dir / "_ast.json"
    astp.write_text(json.dumps(doc["ast"], ensure_ascii=False), encoding="utf-8")
    r = subprocess.run([str(RUST_EXE), "--walk", str(astp), rel],
                       capture_output=True, cwd=str(run_dir))
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:400]
    return r.stdout.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# 四、源码级：别再退回「用 None / null 当哨兵」
# ---------------------------------------------------------------------------

def test_AST_那份用空当占位是无歧义的():
    """前提：AST 的 `defaults` 与形参**等长对齐**，`None`/`null` 只表示
    「这个参数没默认值」—— 写 `空` 的是 `Name` 节点。所以那一层不用改。

    这条判据钉住这个前提：一旦有人把 AST 改成别的表示，这里会先红。
    """
    asttxt = (ROOT / "oracle" / "jishi" / "parser.py").read_text(encoding="utf-8")
    assert "defaults.append(None)" in asttxt, (
        "`_parse_params` 应当给「没默认值」压一个 None 占位（等长对齐）")
    # 写成 `空` 的是 Name 节点而不是 None —— 前端那侧也一样
    front = (ROOT / "frontend" / "src" / "parser.rs").read_text(encoding="utf-8")
    assert 'vec![("id", s("空"))]' in front, "`空` 在前端应当是 Name 节点"


def test_运行期是按下标折算而不是靠值判断():
    """四处运行期都要先算出「第一个有默认值的下标」，而不是看值是不是 `None`。"""
    痕迹 = {
        "oracle/jishi/vm.py": ["first_def"],
        "oracle/jishi/interpreter.py": ["first_def"],
        "rust/src/lib.rs": ["first_def"],
        "oracle/node/vm.js": ["firstDef"],
    }
    for rel, keys in 痕迹.items():
        src = (ROOT / rel).read_text(encoding="utf-8")
        for k in keys:
            assert k in src, (
                f"{rel} 里找不到「{k}」—— 默认值绑定是不是又被改回哨兵写法了？")


def test_运行期不再拿空当哨兵():
    """反例判据：那几处**代码**里不许再出现「拿 `None`/`null` 过滤默认值」。"""
    反例 = {
        "oracle/jishi/vm.py": "not in bound and d is not None",
        "oracle/jishi/interpreter.py": "and d is not None:\n                call_env",
        "oracle/node/vm.js": "!bound.has(p) && defaults[i] !=",
    }
    for rel, bad in 反例.items():
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert bad not in src, f"{rel} 里还在拿 `None`/`null` 当默认值哨兵：{bad!r}"
