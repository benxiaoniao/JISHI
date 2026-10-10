# -*- coding: utf-8 -*-
"""运行期错误体检：**常见运行期错误的 (类型, 消息) 在五个执行器上必须一致**。

## 为什么要有它

`tools/check_engines.py` 扫的是**仓库里的语料**——而语料里几乎没有「运行期错误」
的形状（多数语料是正常跑完的程序）。于是这类分岔可以长期存在而无人察觉：

R6.2 第一次跑这个探针，**19 个形状里 18 个不一致**：

| 形状 | 树遍历 / PyVM / CVM | Rust VM |
|---|---|---|
| `列[9]` | `索引错误` / `list index out of range` | `索引错误` / `越界` |
| `字["乙"]` | `键错误` / `'乙'`（**键的 repr**） | `键错误` / `找不到这个键` |
| `甲()`（非函数） | `这个东西不能调用` / `「5」不能调用` | `运行期错误` / `「5」不能调用` |
| `没定义过的东西` | `找不到这个名字` / … | `异常` / … |
| `1.某某` | `「整数」没有属性「某某」` | `「1」没有属性「某某」`（**打的是值**） |
| `[1] + 2` | `「列表」只能和「列表」相加…` | `类型错误` / `不支持的加法` |

两类根因：
* **Python 侧漏内部表示**：`list index out of range` 是英文、`'乙'` 是键的 repr
  —— 一门中文教学语言里的中英混排（`errors.py` 里 `_type_error_zh` 早就为
  `TypeError` 解决过同一件事，这两支当时没覆盖）；
* **宿主侧「笼统化」**：Rust 把多种语义错误都报成「异常」「运行期错误」，
  于是 `e.类型` 与 `捕获 X` 的匹配都对不上；有的地方还打**值**而不是**类型名**。

## 判据

每个形状都**自己接住**再打印 `类型` 与 `消息`，于是比的是 stdout ——
这样连「Rust VM 画不出源码行」那件事都不影响它（那是另一个缺口，见
`tools/check_engines.py` 的登记）。

## R6.4：把 **Node 宿主**也拉进来

以前这里只比四个执行器（三个 Python + Rust VM）—— **Node 宿主从头到尾不在视野里**。
把它拉进来之后（判据照旧：自己接住再打印 `类型` 与 `消息`），
**19 个形状里有 11 个不一致**，其中两条还是**静默成功**：

| 形状 | Node（改之前） | Python 基准 |
|---|---|---|
| `列[9] = 3`（越界赋值） | **什么都不报，还把列表撑长了** | 索引错误 / 列表下标越界 |
| `[1].弹出(9)` | `undefined`（静默） | 索引错误 / 列表下标越界 |
| `1 / 0` | 消息 `不能除以零` | 消息**空**（标题里已经有了） |
| `列[9]` | `下标「9」越界` | `列表下标越界` |
| `甲()`（非函数） | 类型 `运行期错误` | `这个东西不能调用`（E2006） |
| `没定义过的东西` | 类型 `异常` | `找不到这个名字`（E0301） |
| `导入 不存在的模块` | 类型 `异常` | `找不到这个函数`（E0302） |

📌 「静默成功」那一类最要命：**JS 数组 `a[9] = x` 会自动补长**，于是越界赋值
不报错、还悄悄改了列表 —— 而 Python 侧报错。这就是本项目那句「不报错的错答案最难查」。

⚠️ **加运行期错误时跑它。** Python 侧（`errors.py` / `runtime.py`）、Rust 宿主
（`rust/src/lib.rs` 的 `T_NAME` / `T_NOT_CALLABLE` / `INDEX_MSG` / …）、
Node 宿主（`node/runtime.js` 的 `ERROR_CODES` / `node/vm.js`）**各有一份文案**，
**都要一起改** —— 这份探针就是那份「一起改」的凭证。

用法：
    python tools/probe_runtime_errors.py        # 全部形状
    python tools/probe_runtime_errors.py -v     # 每个形状都打一行
    python tools/probe_runtime_errors.py --no-node   # 不带 Node 宿主
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oracle.jishi import cvm_bind                                        # noqa: E402
from oracle.jishi.compiler import compile_source, compile_to_json        # noqa: E402
from oracle.jishi.interpreter import run_source as run_tree              # noqa: E402
from oracle.jishi.vm import VM                                           # noqa: E402

#: 形状名 → 会**抛错**的那段源码（会被包进 `尝试 … 捕获 异常 为 错`）。
#: 每条都钉住一类**用户真会撞上**的错；不追求覆盖全部，追求覆盖**常见且易漂**的。
SHAPES: dict[str, str] = {
    "列表越界·读": '令 列 = [1, 2]\n列[9]\n',
    "列表越界·赋值": '令 列 = [1, 2]\n列[9] = 3\n',
    "文本越界": '令 文 = "甲乙"\n文[9]\n',
    "字典缺键·文本": '令 字 = {“甲”: 1}\n字["乙"]\n',
    "字典缺键·整数": '令 字 = {1: 1}\n字[9]\n',
    "列表弹出·空": '令 列 = []\n列.弹出()\n',
    "列表弹出·越界": '令 列 = [1]\n列.弹出(9)\n',
    "除以零": '令 甲 = 0\n打印(1 / 甲)\n',
    "整除零": '打印(1 // 0)\n',
    "取余零": '打印(1 % 0)\n',
    "调用非函数": '令 甲 = 5\n甲()\n',
    "整数取下标": '令 甲 = 5\n甲[0]\n',
    "加错类型": '打印([1] + 2)\n',
    "未定义名字": '打印(没定义过的东西)\n',
    "属性不存在": '令 甲 = 1\n打印(甲.某某)\n',
    "赋值前读局部": '函数 f()：\n    令 甲 = 乙\n    令 乙 = 1\n    返回 甲\n打印(f())\n',
    "找不存在的模块": '导入 不存在的模块\n',
    "文本转整数失败": '打印(整数("甲乙"))\n',
    "取不存在的键": '令 字 = {}\n字["甲"]\n',
}

#: 形状源码外面套的壳：**自己接住**，把「类型」与「消息」打到 stdout。
TEMPLATE = """尝试：
{body}捕获 异常 为 错：
    打印("类型=" + 错.类型)
    打印("消息=" + 错.消息)
"""


def build_source(body: str) -> str:
    """把一段会抛错的源码包成「自己接住并打印」的完整程序。"""
    indented = "".join("    " + ln + "\n" for ln in body.splitlines())
    return TEMPLATE.format(body=indented)


def _rust_exe() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


def _node_exe() -> "str | None":
    """Node 可执行文件（PATH 上没有就返回 None）。"""
    return shutil.which("node")


def _node_index() -> Path:
    return ROOT / "oracle" / "node" / "index.js"


def _cap(fn) -> str:
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn()
        return buf.getvalue().strip().replace("\n", " | ")
    except BaseException as e:                                     # noqa: BLE001
        # 没被 `捕获 异常` 接住 ⇒ 本身就是个问题（接不住！）
        return "【没人接住】" + type(e).__name__ + ":" + str(e).splitlines()[0]


def engine_outputs(src: str, rel: str = "形状.jsh",
                   with_node: bool = True) -> dict[str, str]:
    """五个执行器各跑一遍 → `{执行器名: 输出}`。

    ⚠️ Node 宿主**只比 stdout**（「自己接住再打印」的那份，与判据一致）——
    它吃的是字节码 JSON，所以这里是「当前前端编出的字节码交给它跑」。
    """
    out = {
        "树遍历": _cap(lambda: run_tree(src, rel)),
        "Python VM": _cap(lambda: VM(compile_source(src, rel), rel).run()),
        "C VM": _cap(lambda: cvm_bind.run_source_c(src, rel)),
    }
    exe = _rust_exe()
    if exe.exists():
        tmp = Path(tempfile.gettempdir()) / f"_probe_err_{os.getpid()}.json"
        tmp.write_text(compile_to_json(src, rel), encoding="utf-8")
        r = subprocess.run([str(exe), "--load-bytecode", str(tmp)], capture_output=True, input=b"")
        out["Rust VM"] = (r.stdout.decode("utf-8", "replace")
                          .strip().replace("\n", " | "))
    node = _node_exe()
    if with_node and node:
        tmp = Path(tempfile.gettempdir()) / f"_probe_err_node_{os.getpid()}.json"
        tmp.write_text(compile_to_json(src, rel), encoding="utf-8")
        r = subprocess.run([node, str(_node_index()), str(tmp)],
                           capture_output=True, input=b"")
        out["Node"] = (r.stdout.decode("utf-8", "replace")
                       .strip().replace("\n", " | "))
    return out


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="运行期错误 (类型, 消息) 五执行器体检")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="每个形状都打印一行（默认只报不一致的）")
    ap.add_argument("--no-node", action="store_true",
                    help="不把 Node 宿主算作执行器")
    args = ap.parse_args(argv)

    if not _rust_exe().exists():
        print(f"（注：{_rust_exe().name} 不存在，本次只比三个执行器；"
              f"要带上它先 `cd rust && cargo build --release`）")
    if not args.no_node and not _node_exe():
        print("（注：PATH 上没有 node，本次不带 Node 宿主）")

    diff = 0
    for tag, body in SHAPES.items():
        outs = engine_outputs(build_source(body), with_node=not args.no_node)
        vals = list(outs.values())
        same = all(v == vals[0] for v in vals)
        if not same:
            diff += 1
        if args.verbose or not same:
            print(("✅ " if same else "❌ ") + tag)
        if not same:
            base = outs["树遍历"]
            for name, v in outs.items():
                print(f"   {'  ' if v == base else '≠ '}{name:8} {v[:120]}")
    print(f"\n{len(SHAPES)} 个形状，**{diff} 个不一致**")
    return 1 if diff else 0


if __name__ == "__main__":
    sys.exit(main())
