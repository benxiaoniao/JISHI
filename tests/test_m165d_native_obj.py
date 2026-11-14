# -*- coding: utf-8 -*-
"""M165d · 宿主对象模型：让标准库能返回「带方法的东西」。

## 这一站补的是**模型**，不是补一个函数

`时间.计时()` 在 Node 与 Rust 两个宿主上**一直缺失**（CHANGELOG M51 就登记过，
`docs/冒烟清单.md` 写着「唯一一处五引擎不同宽的已知缺口」）—— 而语言卡里
明明列着 `计时(名字='')`，**文档在说谎**。

缺的不是函数体，是**宿主对象模型**：`计时` 返回的是一个**对象**，
要能 `.耗时()` / `.停()`，还要能当上下文管理器（`用 … 为 t：` 会去查
`进入` / `退出`）。而两个宿主的属性查找与上下文查找只认「Jishi 实例」与
「内建容器」，标准库直接返回的宿主对象**走不通**。

## 做法

两个宿主各加一个「自带方法表」的宿主对象：

| 宿主 | 类型 | 方法表 |
|---|---|---|
| Rust | `Val::Native(Rc<NativeObj>)` | `&'static [(&'static str, NativeFn)]` |
| Node | `JishiNative` | `{ 名字: (obj, args, line, col) => … }` |

属性查找 / 方法调用 / 上下文协议全都认它 —— **以后再有「标准库要返回带方法的
东西」的 API（`数据库.连接` 这类），不用再动 `Val` 的变体表**。

（C VM 与 Python 侧本来就能跑：它们的标准库走 Python 回调，Python 的
`_计时器` 是个普通实例。所以缺的**只有两个宿主**。）

## 判据为什么这么写

- **行为判据的五引擎逐字比**：用「不带名字的计时器」——带名字会在退出时打印
  `xx 耗时 0.000 秒`，那个数字**天然不稳定**，逐字比必然假红。
- **打印行为单独测**：只断言「输出里出现了『耗时』和『秒』」，不断言数字。
- **源码级护栏**：`NativeObj` / `JishiNative` 被删了、或 `methodTable` 又不认它，
  行为测试会红；但如果有人「顺手把 `计时` 从宿主的模块表里摘掉」——
  那是 `test_m51_consistency` 的事（它的 `KNOWN_GAPS` 现在已经清零，
  少了任何一个函数都会报红）。
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
# 一、行为：五个执行器都能用计时器
# ---------------------------------------------------------------------------

#: ⚠️ **不给名字**：给了名字退出会打印 `xx 耗时 0.000 秒`，那个数字天然不稳定
#: （受机器负载影响），逐字比必然假红。打印行为放到下一条单独测。
计时器源 = '''\
导入 时间
令 t = 时间.计时()
令 一 = t.耗时()
令 二 = t.停()
令 三 = t.耗时()
打印(类型(t))
打印(一 >= 0)
打印(二 >= 0)
打印(二 == 三)
用 时间.计时() 为 ctx：
    打印(类型(ctx))
    打印(ctx.耗时() >= 0)
打印("块后")
'''

计时器期望 = """\
计时器
真
真
真
计时器
真
块后"""


def test_计时器_五引擎逐字一致(tmp_path):
    """覆盖 `NativeObj` / `JishiNative` 必须支撑的四件事：

    ① **`类型()`** 给「计时器」（不是「内建函数」也不是 `[object Object]`）；
    ② **方法调用** `.耗时()` / `.停()`；
    ③ **停表之后 `耗时()` 固定**（`二 == 三`）—— 这条最能抓「状态没共享/没保存」；
    ④ **上下文协议** `用 … 为 ctx：` —— 它要先找到 `进入`，出块时找 `退出`。
    """
    出 = _五份(计时器源, tmp_path)
    assert len(出) >= 3, f"能跑的执行器太少：{list(出)}"
    不一样 = {名: _归一(文) for 名, 文 in 出.items() if _归一(文) != 计时器期望}
    assert 不一样 == {}, (
        "计时器行为不对（期望见本文件里的 `计时器期望`）：\n"
        + "\n".join(f"--- {名} ---\n{文}" for 名, 文 in 不一样.items()))


def test_给了名字才打印(tmp_path):
    """「给了名字才打印」是 Python 侧定下的口径（想把耗时算进自己的表格时不该被打扰）。

    这里只断言**结构**，不断言数字：`建表 耗时 X.XXX 秒` 里的 X 每次都不一样。
    """
    源 = ('导入 时间\n'
          '用 时间.计时("建表") 为 t：\n'
          '    令 列 = []\n'
          '打印("块后")\n')
    出 = _五份(源, tmp_path)
    for 名, 文 in 出.items():
        行们 = _归一(文).splitlines()
        assert "块后" in 行们, f"{名} 没跑到最后：{文[:150]}"
        计时行 = [x for x in 行们 if "耗时" in x and "秒" in x]
        assert len(计时行) == 1, f"{名} 的计时输出不是一行：{行们}"
        assert 计时行[0].startswith("建表 耗时 "), f"{名} 的计时行格式不对：{计时行[0]!r}"


def test_不给名字就不打印(tmp_path):
    """不给名字时**一个多余的字都不能有** —— 否则会跟调用方自己的表格混在一起。"""
    源 = ('导入 时间\n'
          '用 时间.计时() 为 t：\n'
          '    令 列 = []\n'
          '打印("只该有这一行")\n')
    出 = _五份(源, tmp_path)
    for 名, 文 in 出.items():
        assert _归一(文) == "只该有这一行", f"{名} 多印了东西：{文!r}"


def test_名字要传文本(tmp_path):
    """`时间.计时(5)` 要报「「计时」的名字要传文本」—— 与 Python 侧同一条。"""
    源 = "导入 时间\n时间.计时(5)\n"
    出 = _五份(源, tmp_path)
    for 名, 文 in 出.items():
        assert "「计时」的名字要传文本" in 文, f"{名} 没报这条：{文[:200]}"


# ---------------------------------------------------------------------------
# 二、源码级护栏：对象模型别被拆掉
# ---------------------------------------------------------------------------

def test_Rust有宿主对象模型():
    """`NativeObj` 是「标准库返回带方法的东西」的唯一出口。

    删了它，`时间.计时` 会退回「函数有、但返回的东西调不了方法」——
    而行为测试（`test_计时器_五引擎逐字一致`）会立刻红，所以这条是**双保险**：
    它盯的是「机制在不在」，不是「某个函数在不在」。
    """
    源码 = (ROOT / "rust" / "src" / "lib.rs").read_text(encoding="utf-8")
    assert "pub(crate) struct NativeObj" in 源码
    assert "Native(Rc<NativeObj>)" in 源码, "`Val::Native` 不见了"
    assert "fn native_method" in 源码, "属性查找的分派不见了"
    assert "fn native_call" in 源码, "方法调用的分派不见了"
    # 上下文协议要能找 `进入`（原来只找 退出/关闭）
    assert '"进入" | "退出" | "关闭" =>' in 源码, (
        "`lookup_method` 又不找 `进入` 了 —— 宿主对象当上下文管理器时会退回"
        "「有退出就把对象本身绑给 t」，`进入()` 的返回值被丢掉")


def test_Node有宿主对象模型():
    源码 = (ROOT / "oracle" / "node" / "runtime.js").read_text(encoding="utf-8")
    assert "class JishiNative" in 源码
    assert "if (isNative(obj)) return obj.methods;" in 源码, (
        "`methodTable` 又不认宿主对象了 —— `.耗时()` 会报「没有方法」")
    assert "if (isNative(v)) return v.typeName;" in 源码, "`typeName` 又不认宿主对象了"


def test_两个宿主都有计时():
    """`时间.计时` 必须真的在两个宿主的模块表里（不是只写了机制没接上）。"""
    rs = (ROOT / "rust" / "src" / "stdlib" / "datetime.rs").read_text(encoding="utf-8")
    assert '"计时" =>' in rs, "Rust 的 `时间` 模块里没有 `计时`"
    js = (ROOT / "oracle" / "node" / "vm.js").read_text(encoding="utf-8")
    assert "'计时': new R.Builtin('计时'" in js, "Node 的 `时间` 模块里没有 `计时`"


# ---------------------------------------------------------------------------
# 三、连带修好的：树遍历的**推导式**（M165d 浮出来的下一个缺口）
# ---------------------------------------------------------------------------

推导式源 = '''\
令 数 = [1, 2, 3, 4, 5]
打印([x * 2 遍历 x 在 数])
打印([x 遍历 x 在 数 如果 x % 2 == 0])
打印({x: x * x 遍历 x 在 数 如果 x < 3})
打印([y + "!" 遍历 y 在 ["甲乙"]])
打印([c 遍历 c 在 "abc"])
打印([x 遍历 x 在 []])
令 外 = 9
打印([外 + x 遍历 x 在 数])
'''

推导式期望 = """\
[2, 4, 6, 8, 10]
[2, 4]
{1: 1, 2: 4}
['甲乙!']
['a', 'b', 'c']
[]
[10, 11, 12, 13, 14]"""


def test_推导式_五引擎逐字一致(tmp_path):
    """列表 / 字典推导式在五个执行器上逐字一致。

    ⚠️ **这条不是 M165d 规划里的内容** —— 它是修 `时间.计时` 时**浮出来的下一个缺口**：

    `check_walk` 的硬指标是「还不支持 0」，而树遍历一直没实现 `Comprehension`
    （推导式）。为什么没被发现：唯一用到推导式的语料 `排序跑分` 在 `时间.计时`
    那一步就失败了（那个函数 Node / Rust 两宿都没有）—— **一个缺口把另一个缺口
    盖住了**。M165d 补上计时器之后它才第一次跑通，`check_walk` 立刻报
    「还没实现 1 × Comprehension」。

    覆盖：带条件 / 字典推导 / 文本迭代 / 空可迭代 / **引用外层变量**
    （最后一行 —— 推导式不该把外层作用域搞乱）。
    """
    出 = _五份(推导式源, tmp_path)
    assert len(出) >= 3, f"能跑的执行器太少：{list(出)}"
    不一样 = {名: _归一(文) for 名, 文 in 出.items() if _归一(文) != 推导式期望}
    assert 不一样 == {}, (
        "推导式结果不对（期望见本文件里的 `推导式期望`）：\n"
        + "\n".join(f"--- {名} ---\n{文}" for 名, 文 in 不一样.items()))

#: 📌 「树遍历还有没有没实现的节点」这条**不在这里重测**：
#: `tests/test_m80_walk_exceptions.py::test_进度仪表全覆盖` 已经盯着它，而且那条
#: 带 `@pytest.mark.计时`（`check_walk` 满载要 190 秒，必须进串行节）。
#: 在这里再来一遍等于把同一份重活塞进并行节 —— CPU 饥荒会让 worker 静默硬崩。
