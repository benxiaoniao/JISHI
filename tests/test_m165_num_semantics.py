# -*- coding: utf-8 -*-
"""M165 · 整除 / 取模的符号与精度，以及字典写入语义。

## 这一站发现的两类真 bug

### ① `//` `%` 的**符号**（只有 Rust 宿主错）

原来 Rust 的 `//` `%` 一律转 f64 再算：

| 表达式 | Python / Node / C | Rust（改前） |
|---|---|---|
| `-7 % 3` | `2` | **`-1`** |
| `7 % -3` | `-2` | **`1`** |
| `-7.5 % 2` | `0.5` | **`-1`**（连类型都错） |
| `9007199254740993 // 1` | 原值 | **`9007199254740992`**（f64 只有 53 位尾数） |

根因：Rust 的 `%` 是**截断**语义（余数符号跟被除数），Python 是**向下取整**
语义（余数符号跟除数）。`//` 靠 `.floor()` 侥幸对了，但精度照样丢。

### ② 语料长期只测了正数

`tests/cases/01_算术.jsh` 原来只有 `7 // 2` 和 `7 % 2` —— **全是正数**。
这正是本项目反复踩的那条：**语料没覆盖 = 分叉一直在**。现在补了负数、
浮点、2^53 以上整数三组。

## 判据为什么写死期望值

按 MEMORY 的规矩：Rust 最终要替代 Python，所以判据**不能只押「与 Python 对拍」**
（Python 自己错了就一起错）。这里前后两段期望都是**字面量**，
不 import `oracle.jishi` 去算 —— 规则变了必须有人来改这个文件。
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                            # noqa: E402

CVM驱动 = '''\
import sys
sys.path.insert(0, r"{root}")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(newline="\\n")
    except Exception:
        pass
from oracle.jishi import cvm_bind, errors as E

src = open("程序.jsh", encoding="utf-8").read()
try:
    cvm_bind.run_source_c(src, filename="程序.jsh")
except E.JishiError as e:
    e.with_source(src.replace("\\r\\n", "\\n").split("\\n"))
    sys.stderr.write(e.render() + "\\n")
    sys.exit(1)
'''


def _五份(源码: str, tmp_path: Path) -> dict:
    """把同一段程序交给五个执行器，返回 {引擎: 输出文本}。"""
    from oracle.jishi import serialize
    from oracle.jishi.compiler import compile_source

    (tmp_path / "程序.jsh").write_text(源码, encoding="utf-8", newline="\n")
    (tmp_path / "驱动.py").write_text(
        CVM驱动.format(root=str(ROOT).replace("\\", "\\\\")), encoding="utf-8")
    表 = {
        "树遍历": [sys.executable, "-m", "oracle.jishi.cli", "--tree", "程序.jsh"],
        "Python VM": [sys.executable, "-m", "oracle.jishi.cli", "程序.jsh"],
        "C VM": [sys.executable, "驱动.py"],
    }
    rs = rust_exe_path()
    if rs.is_file():
        表["Rust 宿主"] = [str(rs), "程序.jsh"]
    node = shutil.which("node")
    if node:
        (tmp_path / "程序.json").write_text(
            serialize.dumps(compile_source(源码, "程序.jsh")), encoding="utf-8")
        表["Node 宿主"] = [node, str(ROOT / "oracle" / "node" / "index.js"),
                           "程序.json"]

    出 = {}
    for 名字, 命令 in 表.items():
        r = subprocess.run(命令, cwd=str(tmp_path), capture_output=True,
                           timeout=180, input=b"")
        出[名字] = (r.stdout + r.stderr).decode("utf-8", "replace")
    return 出


def _归一(文: str) -> str:
    return 文.replace("\r\n", "\n").strip()


# ---------------------------------------------------------------------------
# 一、整除与取模：符号 + 精度
# ---------------------------------------------------------------------------

#: ⚠️ **写死的期望值**（不是拿 Python 算出来的）。
#: `//` `%` 是向下取整语义：商的符号跟着「向下」走，余数的符号跟着**除数**走。
整除取模源 = '''\
打印(-7 // 3)
打印(-7 % 3)
打印(7 % -3)
打印(-7 % -3)
打印(7 // -3)
打印(7.5 % 2)
打印(-7.5 % 2)
打印(7.5 % -2)
打印(7.5 // 2)
打印(-7.5 // 2)
打印(9007199254740993 // 1)
打印(9007199254740993 % 7)
'''

整除取模期望 = """\
-3
2
-2
-1
-3
1.5
0.5
-0.5
3.0
-4.0
9007199254740993
5"""


def test_整除取模_五引擎逐字一致(tmp_path):
    出 = _五份(整除取模源, tmp_path)
    assert len(出) >= 3, f"能跑的执行器太少：{list(出)}"
    不一样 = {名: _归一(文) for 名, 文 in 出.items() if _归一(文) != 整除取模期望}
    assert 不一样 == {}, (
        "这些执行器的整除/取模结果不对（期望见本测试里的 `整除取模期望`）：\n"
        + "\n".join(f"--- {名} ---\n{文}" for 名, 文 in 不一样.items()))


@pytest.mark.parametrize("表达式,期望", [
    # 商：向下取整，不是向零截断
    ("-7 // 3", "-3"),
    ("-1 // 3", "-1"),
    ("7 // -3", "-3"),
    # 余数：符号跟除数
    ("-7 % 3", "2"),
    ("7 % -3", "-2"),
    ("-6 % 3", "0"),
    ("-6 % -3", "0"),
    # 有浮点参与 ⇒ 结果仍是浮点（`3.0` 不是 `3`）
    ("7.5 // 2", "3.0"),
    ("7.0 // 2.0", "3.0"),
    # 2^53 以上：整数必须走整数路径
    ("9007199254740993 // 1", "9007199254740993"),
    ("9007199254740993 + 1", "9007199254740994"),
    # 负数零：JS 的 `0 * -1` / `Math.floor(-0 / 3)` 都给 **-0**，
    # 而 Python 里整数结果只有 `0`（`-0.0` 只作为浮点字面量存在）。
    # 不归一，Node 就会印成 `-0.0` —— 三个执行器里只有它这么印。
    ("0 * -1", "0"),
    ("-1 * 0", "0"),
    ("-0 // 3", "0"),
    ("0 // -3", "0"),
    ("-6 % 3", "0"),
    ("-6.0 % 3", "0.0"),
    ("-0.0 * 1", "-0.0"),      # 浮点 -0 是**真值**，不许归一掉
])
def test_逐个表达式的符号与精度(表达式, 期望, tmp_path):
    """拆细一点：整组红了能立刻看出是哪条规则错了。"""
    src = f"打印({表达式})\n"
    出 = _五份(src, tmp_path)
    错 = {名: _归一(文) for 名, 文 in 出.items() if _归一(文) != 期望}
    assert 错 == {}, f"`{表达式}` 期望 {期望!r}，这些执行器不对：{错}"


def test_除零仍然报错(tmp_path):
    """改快路径不能把除零检查弄丢（整数路径单独判 0，容易漏）。"""
    src = "打印(7 // 0)\n"
    出 = _五份(src, tmp_path)
    assert 出, "没有执行器跑起来"
    for 名, 文 in 出.items():
        assert "不能除以零" in 文 or "除零" in 文, f"{名} 没报除零：{文[:120]}"


# ---------------------------------------------------------------------------
# 二、字典写入语义（M165 把 get+insert 换成 entry，行为一个字都不许变）
# ---------------------------------------------------------------------------

字典源 = '''\
令 字 = {}
字["甲"] = 1
字["乙"] = 2
字["甲"] = 99
打印(字)
字[真] = 10
字[1] = 20
打印(长度(字))
打印(字[真])
打印(字[1])
字.弹出("乙")
打印(字)
字["乙"] = 3
打印(字)
'''

字典期望 = """\
{'甲': 99, '乙': 2}
3
20
20
{'甲': 99, 真: 20}
{'甲': 99, 真: 20, '乙': 3}"""


def test_字典写入语义_五引擎逐字一致(tmp_path):
    """覆盖四件事：

    ① 重复赋值**只改值、不改位置**（插入顺序是字典的契约）；
    ② `真` 与 `1` 是**同一个键**（`真 == 1` 为真，哈希必须跟着一致）；
    ③ 弹出之后重新插入，位置在**末尾**（不是原位置）；
    ④ 文本键与整数键混用时顺序仍在。
    """
    出 = _五份(字典源, tmp_path)
    不一样 = {名: _归一(文) for 名, 文 in 出.items() if _归一(文) != 字典期望}
    assert 不一样 == {}, (
        "字典写入语义变了（改动 `dict_set` 的 entry 写法时最容易踩）：\n"
        + "\n".join(f"--- {名} ---\n{文}" for 名, 文 in 不一样.items()))


# ---------------------------------------------------------------------------
# 三、源码级护栏：别让这两处又退回去
# ---------------------------------------------------------------------------

def test_Rust的整除取模走整数快路径():
    """源码级护栏：Rust 的 `binop` 里 `//` `%` 必须有 `(Int, Int)` 分支。

    为什么需要这条：这次修的就是「一律转 f64」。以后有人图省事把它改回
    `to_num_pair_op` 一行，**语料只测正数时照样全绿** —— 只有这条会红。
    """
    源码 = (ROOT / "rust" / "src" / "lib.rs").read_text(encoding="utf-8")
    for 函数名 in ("fn floor_div", "fn floor_mod", "fn float_mod"):
        assert 函数名 in 源码, f"Rust 缺了 {函数名}（整除/取模的符号处理）"
    # binop 的 4 / 5 分支里要出现整数匹配
    binop段 = 源码[源码.index("pub(crate) fn binop"):]
    binop段 = binop段[:binop段.index("fn floor_div")]
    assert binop段.count("(Int(a), Int(b))") >= 2, (
        "`binop` 的 `//` / `%` 分支里看不到 `(Int(a), Int(b))` —— "
        "是不是又被改成「一律转 f64」了？那样负数取模会算错、2^53 以上会丢精度")
    assert "floor_div(" in binop段 and "floor_mod(" in binop段


def test_语料里有负数取模():
    """源码级护栏：`tests/cases/01_算术.jsh` 必须留着负数/浮点那几行。

    这次的两个 bug 能活这么久，就是因为语料只有 `7 // 2`、`7 % 2`。
    删掉这几行 = 把盲区还回去。
    """
    语料 = (ROOT / "tests" / "cases" / "01_算术.jsh").read_text(encoding="utf-8")
    for 必需 in ("-7 % 3", "-7 // 3", "7.5 % 2", "9007199254740993"):
        assert 必需 in 语料, (
            f"算术语料里少了 `{必需}` —— 负数取模 / 浮点取模 / 大整数精度"
            "这三处正是 M165 修掉的分叉，删了就又没人看着了")
