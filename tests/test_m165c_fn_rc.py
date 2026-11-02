# -*- coding: utf-8 -*-
"""M165c · `Val::Func` / `BoundMethod::User` 改成 `Rc<Func>`。

## 为什么

`Func` 是 88 字节，里面有 `name: String` / `cells: Vec<JCell>` /
`defaults: Option<Vec<Val>>` —— **`clone()` 一次就是三次 malloc**
（字符串 + 两个 Vec，`cells` 里还要逐个 `Rc::clone`）。

而 VM **每调用一次函数**就要 clone 一份：`op::CALL` 把 `Val::Func(f)` 解构出来、
克隆、传给 `push_func_frame`。`实例.方法()` 走 `BoundMethod::User { func: Func }`，
同样每次深拷贝一次。

改成 `Rc<Func>` 之后 clone 只是引用计数 +1。

## 实测（同刻 A/B，新旧二进制交替跑）

| 场景 | 收益 |
|---|---|
| 0 参数调用 | **+57%**（461 → 294 ns/次） |
| 1 参数调用 | **+48%** |
| 递归斐波那契（22） | **+23%** |

整体几何平均 **1.01x → 1.09x**，比 CPython 慢的场景 **7 → 5**；
「函数调用密集」直接掉出慢场景名单（0.74x → 不再垫底）。

## 为什么安全

`Func` 里的东西**创建后就不再变**：`name` / `code_idx` / `id` 是常量，
`cells` 是闭包捕获的单元（共享本来就是语义要求，`JCell` 已经是
`Rc<RefCell<Cell>>`），`defaults` 是默认参数（编译期定死）。
所以共享一份与各自一份**语义完全等价** —— 判据用的是闭包独立性来盯这一点
（两个计数器必须互不影响）。

## 判据为什么带源码级

改回 `Func`（值）**不会红任何行为测试**，只会静默慢一半。
这种回归只有源码级判据（或者人肉盯基准表）能发现。
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
# 一、源码级护栏
# ---------------------------------------------------------------------------

def test_Func是Rc而不是值():
    """`Val::Func` 与 `BoundMethod::User.func` 都必须是 `Rc<Func>`。

    改回值类型不会红任何行为测试 —— 只会让每次函数调用多三次 malloc
    （实测一次空调用从 294ns 涨回 461ns，「递归斐波那契」慢 23%）。
    """
    源码 = (ROOT / "rust" / "src" / "lib.rs").read_text(encoding="utf-8")
    assert "Func(Rc<Func>)" in 源码, (
        "`Val::Func` 不是 `Rc<Func>` —— 每次函数调用会深拷贝一个 88 字节的 `Func`"
        "（里面还有字符串和两个 Vec，共三次 malloc）")
    assert "Func(Box<Func>)" not in 源码, "`Val::Func` 又变回 `Box<Func>` 了"
    assert "User { func: Rc<Func>," in 源码, (
        "`BoundMethod::User.func` 不是 `Rc<Func>` —— `实例.方法()` 每次调用都会深拷贝")
    assert "User { func: Func," not in 源码, "`BoundMethod::User.func` 又变回值类型了"


# ---------------------------------------------------------------------------
# 二、行为：函数相关的六件事
# ---------------------------------------------------------------------------

函数源 = '''\
函数 造计数器()：
    令 数 = [0]
    函数 加()：
        数[0] = 数[0] + 1
        返回 数[0]
    返回 加
令 c = 造计数器()
打印(c())
打印(c())
令 d = 造计数器()
打印(d())
函数 打招呼(名, 问候="你好")：
    返回 问候 + "，" + 名
打印(打招呼("世界"))
打印(打招呼("世界", "早安"))
类 计数器：
    函数 初始化(自身, 起=0)：
        自身.值 = 起
    函数 加(自身, n)：
        自身.值 = 自身.值 + n
        返回 自身.值
令 对 = 计数器(10)
打印(对.加(5))
打印(对.加(1))
函数 阶乘(n)：
    如果 n <= 1：
        返回 1
    返回 n * 阶乘(n - 1)
打印(阶乘(6))
函数 两次(f, x)：
    返回 f(f(x))
函数 加三(x)：
    返回 x + 3
打印(两次(加三, 1))
函数 甲()：
    返回 1
令 壹 = 甲
打印(壹 是 甲)
打印(类型(甲))
'''

函数期望 = """\
1
2
1
你好，世界
早安，世界
15
16
720
7
真
函数"""


def test_函数六件事_五引擎逐字一致(tmp_path):
    """覆盖 `Rc<Func>` 会碰到的每条路：

    ① **闭包捕获**（`加()` 改的是捕获的列表）—— `Func.cells` 现在被共享，
       必须仍然「每个闭包一份自己的单元」：所以 `c()` 两次给 1、2，
       而**另一个**计数器 `d()` 必须重新从 1 开始（不能串）；
    ② 默认参数（`defaults` 也是共享的，不能被调用方改坏）；
    ③ 类方法（走 `BoundMethod::User`，那条路也换成了 `Rc`）；
    ④ 递归（`Func` 自己引用自己）；
    ⑤ 函数当值传（`f(f(x))`）；
    ⑥ 函数身份（`壹 是 甲` 靠 `Func.id`，`Rc` 之后仍要相同）。
    """
    出 = _五份(函数源, tmp_path)
    assert len(出) >= 3, f"能跑的执行器太少：{list(出)}"
    不一样 = {名: _归一(文) for 名, 文 in 出.items() if _归一(文) != 函数期望}
    assert 不一样 == {}, (
        "函数语义不对（期望见本文件里的 `函数期望`）：\n"
        + "\n".join(f"--- {名} ---\n{文}" for 名, 文 in 不一样.items()))


def test_两个闭包不共享单元(tmp_path):
    """专门盯「`Rc<Func>` 会不会把两个闭包的 cell 也共享了」。

    `Func.cells` 共享的是**同一个闭包**的单元（语义要求如此）；
    但两次调用 `造计数器()` 必须产出**两份互不相干**的单元。
    这条如果错了，症状是「第二个计数器接着第一个数下去」—— 很难从大程序里看出来。
    """
    源 = ('函数 造()：\n'
          '    令 数 = [0]\n'
          '    函数 加()：\n'
          '        数[0] = 数[0] + 1\n'
          '        返回 数[0]\n'
          '    返回 加\n'
          '令 a = 造()\n'
          '令 b = 造()\n'
          '打印(a())\n'
          '打印(a())\n'
          '打印(b())\n')
    出 = _五份(源, tmp_path)
    错 = {名: _归一(文) for 名, 文 in 出.items() if _归一(文) != "1\n2\n1"}
    assert 错 == {}, f"两个闭包共享了单元（应该各自从 1 开始）：{错}"


def test_默认参数不会被调用方改坏(tmp_path):
    """`defaults` 共享之后，调用方改了传进来的默认值不能影响下一次调用。"""
    源 = ('函数 f(列=[])：\n'
          '    列.追加(1)\n'
          '    返回 长度(列)\n'
          '打印(f())\n'
          '打印(f())\n')
    出 = _五份(源, tmp_path)
    # 基石与 Python 一样：默认值只求值一次 ⇒ 两次都是 2（同一个列表）
    错 = {名: _归一(文) for 名, 文 in 出.items() if _归一(文) != "1\n2"}
    assert 错 == {}, f"默认参数的「只求值一次」语义变了：{错}"
