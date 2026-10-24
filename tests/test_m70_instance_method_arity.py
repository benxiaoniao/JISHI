# -*- coding: utf-8 -*-
"""R6.7 · 「实例方法参数个数错」报在哪一行 —— 三个 Python 执行器曾经各说各的。

D63 §三 登记的那个形状：

```
类 甲：
    函数 方法(自身, x)：
        返回 x

令 对 = 甲()
对.方法()

→ 树遍历 5:5    Python VM 2:1    C VM 2:1    Rust 5:5    Node 5:5
```

树遍历与两个宿主报**调用点**（`对.方法()` 那一行），VM / C VM 报**函数定义处** ——
同一件事两个答案，违反硬约束「三执行器语义必须一致」。

## 根因不是一个，是**三个**（而且分散在三种语言里）

| 执行器 | 参数绑定错的位置从哪来 |
|---|---|
| 树遍历 | 调用的 AST 节点 —— 本来就是对的 |
| Python VM | `VM.call_function` 里 `_bind(..., line=code.firstlineno, col=1)` —— **写死定义处** |
| C VM | C 侧 `jsvm_call_function` 里 `bind_args(..., 0, 0)` —— **把宿主传下来的调用点丢了** |

修法：让「Python 侧回流」的两个入口都用**调用点** —— VM 用 `_pending_call_site`
（`_do_call` 在「可能进基石函数」的调用前记下的），C 侧直接用宿主传下来的
`call_line / call_col`；另外 `_cb_method_call` **也要记调用点**（以前只有 `_cb_call` 记，
方法调用那条路上读到的是过期值或 0）。

## 顺带撞出来的两笔

* **Rust 的用户方法调用把关键字参数静默丢了**：`Val::BoundMethod(User)` 压帧时写的是
  `&[], &[]` —— `对.方法(不存在的名=1)` 报成「缺少参数：x」（看着像另一件事），
  而其它四个报「函数「方法」没有叫「不存在」的参数」。
* **树遍历的类构造参数错没有行列**：`甲()` → 「初始化」缺参时位置是空的
  （VM / C VM / 两个宿主都有）。在共享的 `runtime.make_instance` 上补一次。

## 还有一条「改了没生效」（不在三执行器语义里，但在工具链里）

`oracle/jishi/_native/jsvm.dll`（wheel 预编译）与 `cvm/bin/jsvm.dll`（`python cvm/build.py`
的产物）是**两份**：`_lib_path()` 原先**优先前者** —— 于是「改了 `cvm/src/jsvm.c`、
也跑了 `cvm/build.py`，但 Python 加载的仍是那份 9 天前的旧 dll」，
**改了没生效、还一句提示都没有**。本文件末尾有一条测试盯着这件事。
"""

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oracle.jishi import cvm_bind                                        # noqa: E402
from oracle.jishi.compiler import compile_source, compile_to_json        # noqa: E402
from oracle.jishi.interpreter import run_source as run_tree              # noqa: E402
from oracle.jishi.vm import VM                                           # noqa: E402

NODE = shutil.which("node")
HAS_NODE = NODE is not None
need_node = pytest.mark.skipif(not HAS_NODE, reason="本机无 Node.js")


def _rust_exe() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


RUST = _rust_exe()
need_rust = pytest.mark.skipif(
    not RUST.exists(),
    reason="Rust 宿主未构建（cd rust && cargo build --release）")


# ---------------------------------------------------------------------------
# 五执行器各跑一遍，取「(行:列, 首行消息)」
# ---------------------------------------------------------------------------

_CLS = '类 甲：\n    函数 方法(自身, x)：\n        返回 x\n'

#: 左边四条是**要修的形状**，右边几条是「不许被带偏」的对照。
_CASES = {
    "实例方法少给": _CLS + '令 对 = 甲()\n对.方法()\n',
    "实例方法多给": _CLS + '令 对 = 甲()\n对.方法(1, 2)\n',
    "实例方法关键字错": _CLS + '令 对 = 甲()\n对.方法(不存在=1)\n',
    "超方法少给": (
        '类 基：\n    函数 方法(自身, x)：\n        返回 x\n'
        '类 子 继承 基：\n    函数 方法(自身, x)：\n        返回 超().方法()\n'
        '令 对 = 子()\n对.方法(1)\n'),
    "类构造少给": (
        '类 甲：\n    函数 初始化(自身, x)：\n        自身.x = x\n甲()\n'),
    # -- 对照：这些本来就对，改动不许把它们带偏 --
    "普通函数少给": '函数 f(x)：\n    返回 x\nf()\n',
    "普通函数多给": '函数 f(x)：\n    返回 x\nf(1, 2)\n',
    "内建方法少给": '令 列 = [1]\n列.追加()\n',
    "标准库少给": '导入 数学 为 数\n数.开方()\n',
}


def _pos(text: str) -> str:
    m = re.findall(r"[:：](\d+):(\d+)", text)
    return f"{m[0][0]}:{m[0][1]}" if m else "?"


def _py(src: str, kind: str) -> str:
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            if kind == "tree":
                run_tree(src, "x.jsh")
            elif kind == "vm":
                VM(compile_source(src, "x.jsh"), "x.jsh").run()
            else:
                cvm_bind.run_source_c(src, "x.jsh")
        return "no-err:" + buf.getvalue().strip()
    except BaseException as e:                                    # noqa: BLE001
        return _pos(str(e)) + " " + str(e).splitlines()[0]


def _host(src: str, which: str) -> str:
    tmp = Path(tempfile.gettempdir()) / f"_m70_{which}_{os.getpid()}.json"
    tmp.write_text(compile_to_json(src, "x.jsh"), encoding="utf-8")
    if which == "rust":
        r = subprocess.run([str(RUST), "--load-bytecode", str(tmp)],
                           capture_output=True, input=b"")
    else:
        r = subprocess.run([NODE, "oracle/node/index.js", str(tmp)],
                           capture_output=True, input=b"")
    err = r.stderr.decode("utf-8", "replace")
    if not err.strip():
        return "no-err:" + r.stdout.decode("utf-8", "replace").strip()
    return _pos(err) + " " + err.splitlines()[0]


def _five(src: str) -> dict[str, str]:
    out = {"tree": _py(src, "tree"), "PyVM": _py(src, "vm"), "CVM": _py(src, "cvm")}
    if RUST.exists():
        out["Rust"] = _host(src, "rust")
    if HAS_NODE:
        out["Node"] = _host(src, "node")
    return out


# ---------------------------------------------------------------------------
# 一、五执行器必须说同一句话（位置 + 消息）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tag", sorted(_CASES))
def test_参数绑定错在五执行器上逐字一致(tag):
    """`(行:列, 首行)` 五处完全相同 —— 位置与文案都不许分岔。"""
    outs = _five(_CASES[tag])
    base_name, base = next(iter(outs.items()))
    for name, got in outs.items():
        assert got == base, (
            f"{tag}：{name} 与 {base_name} 不一致\n"
            + "\n".join(f"  {k:5} {v}" for k, v in outs.items()))


@pytest.mark.parametrize("tag,where", [
    # 报「调用点」：用户写这行调用在哪，就指哪
    ("实例方法少给", "5:5"),
    ("实例方法多给", "5:5"),
    ("实例方法关键字错", "5:5"),
    ("超方法少给", "6:18"),
    ("类构造少给", "4:2"),
])
def test_位置落在调用点而不是定义处(tag, where):
    """**不只是「一致」，还要落在对的地方。**

    ⚠️ 只断言「五个一样」是不够的：当初 VM / C VM **一起**错成函数定义处
    （`:2:1`）也是「一致」。用户要的是「我写的那行调用有问题」，
    不是「你那个函数的第 2 行有问题」。
    """
    for name, got in _five(_CASES[tag]).items():
        assert got.startswith(where), f"{tag}：{name} 报到 {got[:20]!r}，应为 {where}"


def test_对照形状没被带偏(tag="内建方法少给"):
    """内建方法 / 标准库 / 普通函数的报错位置**各是各的**，改动不许把它们统一掉。

    * 内建方法（`列.追加()`）→ **取属性**的位置（`列.` 那一列）；
    * 标准库（`数.开方()`）→ **调用点**（`(`）—— 模块属性不是 `BoundMethod`；
    * 普通函数（`f()`）→ 调用点。
    """
    assert _five(_CASES["内建方法少给"])["tree"].startswith("2:2")
    assert _five(_CASES["标准库少给"])["tree"].startswith("2:5")
    assert _five(_CASES["普通函数少给"])["tree"].startswith("3:2")


# ---------------------------------------------------------------------------
# 二、Rust 不许静默丢关键字参数
# ---------------------------------------------------------------------------

@need_rust
def test_Rust用户方法不再静默丢关键字参数():
    """`对.方法(不存在=1)` 要报「没有叫「不存在」的参数」。

    ⚠️ 修之前 Rust 的 `Val::BoundMethod(User)` 压帧时把 `knames/kwvals` 传成空 ——
    kwargs 被**静默丢掉**，于是报成「缺少参数：x」（像另一件事）。
    宿主静默丢参数是本项目最恨的那类 bug（M51 修过同款：`加密.摘要` 的 kwargs）。
    """
    got = _five(_CASES["实例方法关键字错"])["Rust"]
    assert "没有叫「不存在」的参数" in got, got


# ---------------------------------------------------------------------------
# 三、C 动态库：「改了没生效」不许再发生
# ---------------------------------------------------------------------------

def test_C动态库优先用源码树产物():
    """`cvm/bin/` 存在时就用它 —— 否则「改了 C 源码、重建了，跑的还是旧 dll」。

    ⚠️ R6.7 真踩到：C 侧的修法像是**完全没起作用**，查了半天才发现
    `_lib_path()` 把 wheel 预编译的 `oracle/jishi/_native/jsvm.dll`（9 天前那份）
    排在 `cvm/bin/` **前面** —— 改了没生效，还一句提示都没有。
    """
    dev = cvm_bind._BIN / cvm_bind._lib_name()
    if not dev.exists():
        pytest.skip("源码树里没有 cvm/bin 的动态库（没跑过 python cvm/build.py）")
    assert cvm_bind._lib_path() == dev, (
        f"加载的是 {cvm_bind._lib_path()}，而不是源码树里刚编的 {dev}")


def test_语料里有这个形状():
    """登记时说过「定下来之前不要进语料」——现在定下来了，它就该在。

    ⚠️ 这条同时提醒：**语料没覆盖的形状，错了也没人知道**（R6.4 那三条
    「静默成功」、R6.6 这条，都是这么潜伏下来的）。
    """
    corpus = ROOT / "tests" / "error_cases" / "36_实例方法参数个数.jsh"
    assert corpus.is_file(), "带「实例方法参数个数错」的语料不见了"
    assert corpus.with_suffix(".err").is_file(), "语料缺伴生的 .err 期望"
    assert "缺少参数" in corpus.with_suffix(".err").read_text(encoding="utf-8")
