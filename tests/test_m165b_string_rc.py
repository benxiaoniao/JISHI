# -*- coding: utf-8 -*-
"""M165b · `Val::Str` 从 `String` 换成 `Rc<str>`（外加内建打包）。

## 为什么要换

`Val::Str(String)` 有个要命的地方：**`Val::clone()` 对它深拷贝整串**。
而 VM 里几乎每一步都在 clone —— `LOAD_FAST` 每读一次字符串变量、每次压栈、
每次当参数传出去。实测（改前，10 万次循环）：

| 读一个变量 | 耗时 |
|---|---|
| 整数 | 124 ns |
| 5 字串 | **387 ns** |
| 1000 字串 | **655 ns** |

CPython 这里几乎是 0 —— 它只把引用计数 +1。等价地，换成 `Rc<str>` 后
克隆是 O(1)，字符串变量读取不再随长度线性变慢。

## 实测收益（同刻 A/B，同一台机器新旧二进制交替跑）

| 场景 | 收益 |
|---|---|
| 字符串拼接（边拼边漏） | **+167%** |
| 读 1000 字串 | **+44%** |
| 文本比较 | **+33%** |
| 内建调用密集 | **+27%** |
| 文本列表遍历 | **+27%** |
| 读 5 字串 | **+26%** |
| 字典文本键 | +5% |

整体几何平均 **0.95x → 1.01x**，比 CPython 慢的场景 **11 → 7**。

## `Val` 为什么没变小（如实记）

本来还想把 `Val` 从 32 字节降到 24。**做不到**：`Int(i128)` 的**对齐要求是 16**，
于是整个 enum 的对齐也是 16，24 不是 16 的倍数，编译器只能补到 32。
（顺带把 `Builtin` 的 `(&'static str, fn)` 打包成 `Rc<BuiltinDef>`、`ExcType` 换成
`Rc<str>` 也白做了 —— 省下的字节全被对齐吃掉，只是代码更整齐。）

真要降到 24 得把 `Int` 改成 `i64` + 溢出走 `Rc<i128>`，那是另一站的事。

## 判据为什么是源码级

行为差异是**性能**上的（O(n) → O(1)），拿墙上时间做判据在这台机器上
（固定负载波动 15.5%）只会假红。而「有人把它改回 `String`」这种回归，
源码级判据一眼能抓住，且**不依赖测试跑在哪台机器上**。
"""

import shutil
import subprocess
import sys
from pathlib import Path

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
# 一、源码级护栏：别让 `Val::Str` 退回 `String`
# ---------------------------------------------------------------------------

def test_Val的字符串是Rc而不是String():
    """`Val::Str` 必须是 `Rc<str>`。

    改回 `String` 的后果是**静默的**：所有测试照样绿，只是每读一次字符串变量
    就深拷贝一遍 —— 「字符串拼接」会慢 2.7 倍，「读长串」慢 44%。
    这种回归只有这条判据（或者人肉盯基准表）能发现。
    """
    源码 = (ROOT / "rust" / "src" / "lib.rs").read_text(encoding="utf-8")
    assert "Str(Rc<str>)" in 源码, (
        "`Val::Str` 不再是 `Rc<str>` —— 改回 `String` 会让每次读字符串变量都深拷贝整串"
        "（实测「字符串拼接」慢 2.7 倍）")
    assert "Str(String)" not in 源码, "`Val::Str` 又变回 `String` 了"


def test_内建打包成一个字段():
    """`Val::Builtin` 必须是单字段（`Rc<BuiltinDef>`）。

    `&'static str` 是**胖指针**（16 字节）+ 函数指针 8 = 24，原来把 `Val` 顶在 32。
    打包后是 8 —— 虽然 `Val` 最终因为 `i128` 的 16 字节对齐仍是 32，
    但两个 24 字节的变体（`Builtin` / `ExcType`）必须先清掉，
    将来把 `Int` 改成 `i64` 才能真的降到 24。
    """
    源码 = (ROOT / "rust" / "src" / "lib.rs").read_text(encoding="utf-8")
    assert "pub(crate) struct BuiltinDef" in 源码, "缺了 `BuiltinDef`（内建打包结构）"
    assert "Builtin(Rc<BuiltinDef>)" in 源码, "`Val::Builtin` 不是单字段"
    assert "Builtin(&'static str" not in 源码, "`Val::Builtin` 又变成双字段（胖指针）了"
    assert "ExcType(Rc<str>)" in 源码, "`Val::ExcType` 应改成 `Rc<str>`"


# ---------------------------------------------------------------------------
# 二、行为：文本密集的六件事，五个执行器逐字一致
# ---------------------------------------------------------------------------

文本源 = '''\
令 甲 = "内容" * 50
令 乙 = 甲 + "尾巴"
打印(长度(乙))
打印(乙[0:3])
打印("内容" 在 乙)
令 字 = {}
字[甲] = 1
字["短"] = 2
打印(长度(字))
打印(字[甲])
令 计 = 0
遍历 字2 在 "一二三"：
    计 = 计 + 1
打印(计)
令 文 = ""
遍历 i 在 范围(50)：
    文 = 文 + "x"
打印(长度(文))
打印(文 == "x" * 50)
打印(乙.替换("内容", "X")[0:6])
'''

文本期望 = """\
102
内容内
真
2
1
3
50
真
XXXXXX"""


def test_文本密集操作_五引擎逐字一致(tmp_path):
    """覆盖 `Rc<str>` 改动会碰到的每条路：

    ① 拼接（`甲 + "尾巴"`）—— 新串要正确、长度要对；
    ② 切片（`乙[0:3]`）—— 按**字符**切，不是按字节；
    ③ 子串查找（`"内容" 在 乙`）；
    ④ **长文本当字典键**（`字[甲]`）—— 哈希与相等都走 `Rc<str>`；
    ⑤ 遍历文本（`遍历 字2 在 "一二三"`）—— 多字节字符要按字符走；
    ⑥ 循环里反复拼接（`文 = 文 + "x"`）—— 这是收益最大的那条路，
       也是最容易在改成 `Rc` 之后语义走样的地方。
    """
    出 = _五份(文本源, tmp_path)
    assert len(出) >= 3, f"能跑的执行器太少：{list(出)}"
    不一样 = {名: _归一(文) for 名, 文 in 出.items() if _归一(文) != 文本期望}
    assert 不一样 == {}, (
        "文本操作的结果不对（期望见本文件里的 `文本期望`）：\n"
        + "\n".join(f"--- {名} ---\n{文}" for 名, 文 in 不一样.items()))


def test_文本遍历按字符不按字节(tmp_path):
    """`Rc<str>` 是 UTF-8 字节串 —— 遍历/切片/长度都必须按**字符**走。

    这条专门盯「不小心把字符操作退化成字节操作」（`s.len()` 是字节数，
    `s.chars().count()` 才是字数；两者在中文上差 3 倍）。
    """
    源 = ('令 甲 = "a中🙂b"\n'
          '令 出 = []\n'
          '遍历 字 在 甲：\n'
          '    出.追加(字)\n'
          '打印(出)\n'
          '打印(长度(甲))\n'
          '打印(甲[1])\n'
          '打印(甲[2:4])\n')
    出 = _五份(源, tmp_path)
    期望 = "['a', '中', '🙂', 'b']\n4\n中\n🙂b"
    错 = {名: _归一(文) for 名, 文 in 出.items() if _归一(文) != 期望}
    assert 错 == {}, f"按字节处理了？期望 {期望!r}，实际：{错}"


def test_遍历中改原变量仍走旧串(tmp_path):
    """`遍历 字 在 乙: 乙 = "xyz"` —— 迭代器拿的是**旧串**。

    文本迭代是「快照」语义（`make_iterator` 里 `reversed_iter` 那一套）。
    换成 `Rc<str>` 之后，快照靠**引用计数**活着 —— 这个行为必须与
    Python 一致（Python 的字符串迭代器同样拿旧值）。
    """
    源 = ('令 乙 = "abc"\n'
          '令 见 = []\n'
          '遍历 字 在 乙：\n'
          '    见.追加(字)\n'
          '    乙 = "xyz"\n'
          '打印(见)\n')
    出 = _五份(源, tmp_path)
    错 = {名: _归一(文) for 名, 文 in 出.items() if _归一(文) != "['a', 'b', 'c']"}
    assert 错 == {}, f"遍历中改原变量后拿到的不是旧串：{错}"
