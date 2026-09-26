# -*- coding: utf-8 -*-
"""Rust 宿主（`rust/`）的端到端对拍。

这一套测试的来历值得说明：Rust 宿主从 M13.3 起就写着「最小可运行」，
但 `tests/` 里**只有 Node 的对拍，没有 Rust 的**——所以它其实从没被真正跑过。
本机 cargo 一直被环境问题挡着（`~/.cargo/bin` 里 13 个 shim 是 0 字节，
`cargo` 执行起来什么都不做），M25/M27 的 Rust 改动也因此一直挂着「未编译验证」。

把 cargo 修好、第一次真正跑起来后，立刻抓到两个**阻塞级**既有 bug：

1. `Flow::Return` 被当成两种含义（「本帧结束请弹帧」和「刚压了新帧别弹」），
   于是每次函数调用后新帧立刻被弹掉 → 外层执行那个还没拿到实参的 CALL
   → `fn_base` 下溢 → panic。**修前连 `函数 加一(x)：返回 x + 1` 都跑不了。**
2. `MAKE_FUNCTION` 的默认值对齐按「全部形参个数」算，有 `*参数` 时错位
   （Python 侧 `vm.py` 在 M25 修过同一个坑），`带默认(1)` 会报「缺少参数」。

修完之后函数/闭包/变长参数/星号解包/显示规则全部与 Python 逐字节一致，
由本文件锁住。剩余缺口（可变容器、类方法调用等）在下面用 xfail 明确标出。
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import rust_exe_path, tmp_bc_path   # noqa: E402

from jishi import serialize                       # noqa: E402
from jishi import vm as vm_mod                    # noqa: E402
from jishi.compiler import compile_source         # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXE = rust_exe_path()   # 平台感知：Windows 是 .exe，别处没有扩展名
CARGO = shutil.which("cargo")


def _build_env() -> dict:
    """构建环境：`windows-gnu` 目标用 gcc 当链接器。

    不写死任何本机路径——需要时用环境变量 `JISHI_MINGW` 指向提供 gcc 的目录
    （IDE 自带的 MinGW 也行）。没设就交给 PATH 上的 gcc；
    两者都没有时 `cargo build` 会失败，测试按「跳过并说明原因」处理。
    """
    env = dict(os.environ)
    mingw = env.get("JISHI_MINGW")
    if mingw and Path(mingw).is_dir():
        env["PATH"] = mingw + os.pathsep + env.get("PATH", "")
    return env


@pytest.fixture(scope="session")
def rust_exe() -> Path:
    """确保 release 产物存在（缺了就现场构建一次），否则跳过。"""
    if not CARGO:
        pytest.skip("本机无 cargo，跳过 Rust 宿主对拍")
    if not EXE.exists():
        r = subprocess.run(
            [CARGO, "build", "--manifest-path", str(ROOT / "rust/Cargo.toml"),
             "--release"],
            capture_output=True, text=True, encoding="utf-8",
            cwd=ROOT, env=_build_env())
        if r.returncode != 0:
            pytest.skip(f"cargo build 失败，跳过 Rust 对拍：{r.stderr[-300:]}")
    return EXE


def _py_run(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        vm_mod.VM(compile_source(src, "<t>"), "<t>").run()
    return buf.getvalue()


def _rs_run(src: str, exe: Path) -> tuple[int, str, str]:
    bc = tmp_bc_path(ROOT / "_tmp_rust_bc.json")
    bc.write_text(serialize.dumps(compile_source(src, "<t>")), encoding="utf-8")
    r = subprocess.run([str(exe), str(bc)], capture_output=True,
                       text=True, encoding="utf-8")
    return r.returncode, r.stdout, r.stderr


def _agree(src: str, exe: Path) -> str:
    py = _py_run(src)
    rc, out, err = _rs_run(src, exe)
    assert rc == 0, f"Rust 宿主非零退出：rc={rc}\nstderr={err[:400]}"
    assert out == py, f"输出不一致\nPython: {py!r}\nRust:   {out!r}"
    return out


# ---------------------------------------------------------------------------
# 核心：函数调用（M28 修帧 bug 前的阻塞点）
# ---------------------------------------------------------------------------

def test_rust_plain_function_call(rust_exe):
    """最普通的函数调用——修 `Flow::Return` 语义混淆前，这一条就 panic。"""
    assert _agree("函数 加一(x)：\n    返回 x + 1\n打印(加一(1))\n",
                  rust_exe) == "2\n"


def test_rust_nested_call(rust_exe):
    assert _agree("函数 加一(x)：\n    返回 x + 1\n打印(加一(加一(1)))\n",
                  rust_exe) == "3\n"


def test_rust_recursion(rust_exe):
    assert _agree(
        "函数 阶乘(n)：\n"
        "    如果 n <= 1：\n"
        "        返回 1\n"
        "    返回 n * 阶乘(n - 1)\n"
        "打印(阶乘(6))\n", rust_exe) == "720\n"


def test_rust_closure(rust_exe):
    assert _agree(
        "函数 外()：\n"
        "    令 n = 1\n"
        "    函数 内()：\n"
        "        返回 n + 41\n"
        "    返回 内\n"
        "打印(外()())\n", rust_exe) == "42\n"


def test_rust_anonymous_function(rust_exe):
    assert _agree("令 f = 函数(x)：x * 2\n打印(f(3))\n", rust_exe) == "6\n"


# ---------------------------------------------------------------------------
# M25：变长参数与星号解包（此前一直是「未编译验证」）
# ---------------------------------------------------------------------------

def test_rust_varargs(rust_exe):
    assert _agree(
        "函数 求和(*数)：\n"
        "    令 总 = 0\n"
        "    遍历 x 在 数：\n"
        "        总 += x\n"
        "    返回 总\n"
        "打印(求和())\n"
        "打印(求和(1, 2, 3))\n", rust_exe) == "0\n6\n"


def test_rust_var_kw(rust_exe):
    assert _agree(
        "函数 配置(**选项)：\n    返回 选项\n"
        "打印(配置())\n打印(配置(甲 = 1))\n", rust_exe) == "{}\n{'甲': 1}\n"


def test_rust_mixed_star_params(rust_exe):
    assert _agree(
        "函数 混合(甲, *余, **选)：\n    返回 [甲, 余, 选]\n"
        "打印(混合(1))\n打印(混合(1, 2, 3))\n",
        rust_exe) == "[1, [], {}]\n[1, [2, 3], {}]\n"


def test_rust_default_before_varargs(rust_exe):
    """默认值 + `*参数` 共存——这曾因 `pad` 按全部形参算而报「缺少参数」。"""
    assert _agree(
        "函数 带默认(甲, 乙 = 10, *余)：\n    返回 [甲, 乙, 余]\n"
        "打印(带默认(1))\n打印(带默认(1, 2, 3, 4))\n",
        rust_exe) == "[1, 10, []]\n[1, 2, [3, 4]]\n"


def test_rust_anonymous_varargs(rust_exe):
    assert _agree("令 f = 函数(*数)：数\n打印(f(1, 2))\n",
                  rust_exe) == "[1, 2]\n"


def test_rust_star_unpack(rust_exe):
    assert _agree(
        "令 甲, *余 = [1, 2, 3, 4]\n打印(甲, 余)\n"
        "令 a, *中, z = [1, 2, 3, 4, 5]\n打印(a, 中, z)\n"
        "令 x, *y = [1]\n打印(x, y)\n"
        "令 头, *尾 = \"你好世界\"\n打印(头, 尾)\n",
        rust_exe) == "1 [2, 3, 4]\n1 [2, 3, 4] 5\n1 []\n你 ['好', '世', '界']\n"


# ---------------------------------------------------------------------------
# M27：值的显示规则（空/真/假 中文、顶层文本不带引号）
# ---------------------------------------------------------------------------

def test_rust_display_rules(rust_exe):
    assert _agree(
        "打印(空, 真, 假)\n"
        '打印([1, 真, 空, "甲"])\n'
        '打印({"键": 假, "甲": [1, 空]})\n'
        '打印("甲")\n'
        '打印(["甲"])\n'
        "打印(`插值 {空} 与 {真}`)\n"
        "打印(类型(空), 类型(真))\n"
        "打印(1 == 1, 1 == 2)\n",
        rust_exe) == ("空 真 假\n[1, 真, 空, '甲']\n{'键': 假, '甲': [1, 空]}\n"
                      "甲\n['甲']\n插值 空 与 真\n空 布尔\n真 假\n")


def test_rust_class_display_uses_text_method(rust_exe):
    """自定义对象打印（M29）：类里定义 `文本(自身)` 就用它。

    以前 Rust 侧没有这条路——`display` 拿不到 VM，调不了用户的「文本」方法。
    现在 `打印`/`文本` 走带 VM 的 `format_value`，对象在容器里也不再掉回
    `<甲 实例>`。
    """
    assert _agree(
        "类 分数：\n"
        "    函数 初始化(自身, 分子, 分母)：\n"
        "        自身.分子 = 分子\n"
        "        自身.分母 = 分母\n"
        "    函数 文本(自身)：\n"
        '        返回 文本(自身.分子) + "/" + 文本(自身.分母)\n'
        "令 f = 新建 分数(1, 3)\n"
        "打印(f)\n打印(文本(f))\n打印([f, f])\n"
        '打印({"键": f})\n'
        "类 裸：\n    函数 初始化(自身)：\n        空\n"
        "打印(新建 裸())\n",
        rust_exe) == "1/3\n1/3\n[1/3, 1/3]\n{'键': 1/3}\n<裸 实例>\n"


# ---------------------------------------------------------------------------
# 基础控制流与数据结构（只读路径）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("src,expect", [
    ("打印(1 + 2 * 3)\n打印(7 / 2)\n打印(\"甲\" + \"乙\")\n", "7\n3.5\n甲乙\n"),
    ("令 s = 0\n遍历 i 在 范围(5)：\n    s += i\n打印(s)\n", "10\n"),
    ("令 s = 0\n循环 3 次：\n    s += 1\n打印(s)\n", "3\n"),
    ("令 i = 0\n当 i < 3：\n    i += 1\n打印(i)\n", "3\n"),
    ("如果 2 > 1：\n    打印(\"大\")\n否则：\n    打印(\"小\")\n", "大\n"),
    ("令 a, b = [1, 2]\n打印(a, b)\n", "1 2\n"),
    ("令 n = 3\n打印(`n 是 {n}`)\n", "n 是 3\n"),
    ("函数 f(a, b = 5)：\n    返回 a + b\n打印(f(1))\n", "6\n"),
])
def test_rust_basics(src, expect, rust_exe):
    assert _agree(src, rust_exe) == expect


# ---------------------------------------------------------------------------
# M28：可变容器语义（`Rc<RefCell<…>>` 重构）
# ---------------------------------------------------------------------------
# 修之前，`Val::List(Vec<Val>)` 是值类型、`Clone` 是深拷贝，所以
# `a.追加(3)` 改的是**副本**、原变量纹丝不动——不报错、结果就是错的。
# 推导式（内部走 `列表.追加`）因此返回空列表。这一组把语义锁死。

def test_rust_list_methods_mutate_original(rust_exe):
    """所有会改内容的列表方法，都必须落到原变量上。"""
    assert _agree(
        "令 a = [1, 2]\n"
        "a.追加(3)\n打印(a)\n"
        "a.插入(0, 0)\n打印(a)\n"
        "a.移除(2)\n打印(a)\n"
        "a.排序()\n打印(a)\n"
        "a.反转()\n打印(a)\n"
        "打印(a.弹出())\n打印(a)\n"
        "a.清空()\n打印(a, 长度(a))\n",
        rust_exe) == ("[1, 2, 3]\n[0, 1, 2, 3]\n[0, 1, 3]\n[0, 1, 3]\n"
                      "[3, 1, 0]\n0\n[3, 1]\n[] 0\n")


def test_rust_assignment_aliases_container(rust_exe):
    """`令 b = a` 之后两者是同一个东西（Python / JS 侧都是引用语义）。"""
    assert _agree(
        "令 a = [1]\n令 b = a\nb.追加(2)\n打印(a, b)\n",
        rust_exe) == "[1, 2] [1, 2]\n"


def test_rust_index_assignment(rust_exe):
    assert _agree(
        '令 a = [1, 2]\na[0] = 9\n打印(a)\n'
        '令 d = {"k": 1}\nd["k"] = 2\nd["n"] = 3\n打印(d)\n',
        rust_exe) == "[9, 2]\n{'k': 2, 'n': 3}\n"


def test_rust_comprehension(rust_exe):
    """推导式依赖 `列表.追加`，曾是「返回空列表」的重灾区。"""
    assert _agree(
        "打印([x * 2 遍历 x 在 范围(4)])\n"
        "打印([x 遍历 x 在 范围(6) 如果 x % 2 == 0])\n"
        "打印([[y 遍历 y 在 范围(x)] 遍历 x 在 范围(3)])\n",
        rust_exe) == "[0, 2, 4, 6]\n[0, 2, 4]\n[[], [0], [0, 1]]\n"


def test_rust_accumulate_in_loop(rust_exe):
    """循环里往同一个列表追加：容器是共享的才对。"""
    assert _agree(
        "令 结果 = []\n"
        "遍历 i 在 范围(4)：\n"
        "    结果.追加(i * i)\n"
        "打印(结果)\n",
        rust_exe) == "[0, 1, 4, 9]\n"


def test_rust_mutation_inside_function(rust_exe):
    """函数里改外部（自由变量）列表，调用方要看得见。"""
    assert _agree(
        "令 日志 = []\n"
        "函数 记(x)：\n"
        "    日志.追加(x)\n"
        "记(1)\n记(2)\n打印(日志)\n",
        rust_exe) == "[1, 2]\n"


def test_rust_nested_container_mutation(rust_exe):
    assert _agree(
        "令 网格 = [[1, 2], [3, 4]]\n"
        "网格[0].追加(9)\n打印(网格)\n"
        '令 d = {"k": [1]}\nd["k"].追加(2)\n打印(d)\n'
        "令 m = [[[1]]]\nm[0][0].追加(2)\n打印(m)\n",
        rust_exe) == "[[1, 2, 9], [3, 4]]\n{'k': [1, 2]}\n[[[1, 2]]]\n"


def test_rust_grouping_idiom(rust_exe):
    """「按首字母分组」——把 成员测试 + 字典取值 + 列表追加 串起来用。

    这条曾经在 `k 在 分组` 处就断了（Rust 侧没实现 `在`），
    补完成员测试之后才跑通。
    """
    assert _agree(
        '令 分组 = {}\n'
        '遍历 w 在 ["ab", "cd", "ae"]：\n'
        '    令 k = w[0]\n'
        '    如果 k 在 分组：\n'
        '        分组[k].追加(w)\n'
        '    否则：\n'
        '        分组[k] = [w]\n'
        '打印(分组)\n',
        rust_exe) == "{'a': ['ab', 'ae'], 'c': ['cd']}\n"


def test_rust_iteration_does_not_consume(rust_exe):
    """遍历**不能**把原列表搬空。

    这是个真实的坑：Rust 版把「列表自身」当迭代器（`iter_next` 会
    `remove(0)`）。以前列表是值类型，`Val::List(l)` 天然是独立副本，
    「遍历不消耗」是白捡的；换成共享容器后如果直接把 `l` 递出去，
    `遍历 x 在 a` 就会逐元素吃掉 `a`。所以 `make_iterator` 必须显式快照。
    """
    assert _agree(
        "令 a = [1, 2, 3]\n"
        "令 s = 0\n"
        "遍历 x 在 a：\n"
        "    s += x\n"
        "打印(s, a)\n"
        "遍历 x 在 a：\n"
        "    s += x\n"
        "打印(s, a)\n",
        rust_exe) == "6 [1, 2, 3]\n12 [1, 2, 3]\n"


def test_rust_unpack_does_not_consume(rust_exe):
    assert _agree(
        "令 a = [1, 2, 3]\n令 x, *y = a\n打印(x, y, a)\n",
        rust_exe) == "1 [2, 3] [1, 2, 3]\n"


def test_rust_list_concat_and_repeat_are_copies(rust_exe):
    """`a + b` / `a * 2` 造新列表，不动任何一边（Python 语义）。"""
    assert _agree(
        "令 a = [1]\n令 b = [2]\n"
        "打印(a + b, a, b)\n"
        "打印(a * 3, a)\n",
        rust_exe) == "[1, 2] [1] [2]\n[1, 1, 1] [1]\n"


def test_rust_container_equality_is_by_value(rust_exe):
    """容器的 `==` 按值比（`[1,2] == [1,2]` 为真），与 Python 一致。"""
    assert _agree(
        '打印([1, 2] == [1, 2], [1] == [2])\n'
        '打印({"a": 1} == {"a": 1}, {"a": 1} == {"a": 2})\n',
        rust_exe) == "真 假\n真 假\n"


def test_rust_dict_methods(rust_exe):
    assert _agree(
        '令 d = {"a": 1, "b": 2}\n'
        '打印(d.键())\n打印(d.值())\n'
        '打印(d.获取("a"), d.获取("x", 0))\n'
        '打印("a" 在 d, "x" 在 d)\n'
        'd.清空()\n打印(d, 长度(d))\n',
        rust_exe) == "['a', 'b']\n[1, 2]\n1 0\n真 假\n{} 0\n"


# ---------------------------------------------------------------------------
# M28 顺带补上：成员测试与同一性（此前 Rust 侧完全没实现）
# ---------------------------------------------------------------------------

def test_rust_membership(rust_exe):
    """`在` / `不在` —— 列表按值、字典按键、文本找子串。

    这是补容器语义时暴露的另一个缺口：`compare` 只处理 0–5，
    `在`(6)/`不在`(7)/`是`(8)/`不是`(9) 根本没实现，
    所以「按首字母分组」这种惯用法在 `如果 k 在 分组` 处直接报「需要数字」。
    """
    assert _agree(
        '打印(2 在 [1, 2], 9 在 [1, 2])\n'
        '打印(9 不在 [1, 2], 2 不在 [1, 2])\n'
        '打印("a" 在 {"a": 1}, "x" 在 {"a": 1})\n'
        '打印("好" 在 "你好", "x" 在 "你好")\n'
        '打印(3 在 范围(5), 9 在 范围(5))\n',
        rust_exe) == "真 假\n真 假\n真 假\n真 假\n真 假\n"


def test_rust_identity(rust_exe):
    """`是` / `不是`：单例与数字文本按值、列表字典按对象身份。"""
    assert _agree(
        "令 x = 空\n打印(x 是 空, x 不是 空)\n"
        "令 a = 1\n令 b = 1\n打印(a 是 b)\n"
        '令 s = "甲"\n打印(s 是 "甲")\n'
        "令 p = [1]\n令 q = [1]\n令 r = p\n打印(p 是 r, p 是 q)\n",
        rust_exe) == "真 假\n真\n真\n真 假\n"


# ---------------------------------------------------------------------------
# M29.1：用户方法调用（`对象.方法()` / `初始化` / 继承覆写）
# ---------------------------------------------------------------------------
# 此前 Rust 侧走到 `BoundMethod::User` 就报「需经 VM 帧」。根因有两层：
# ① 调方法要把实例塞进第一个形参（自身）再压帧——`call_bound` 是个自由函数，
#    拿不到 VM；
# ② 更隐蔽的是 `Instance` 是**值类型**：`自身.字段 = 值` 改的是副本，
#    不报错、静默失效。所以实例也必须共享（`Rc<RefCell<…>>`）。

def test_rust_method_call_basic(rust_exe):
    assert _agree(
        "类 甲：\n    函数 双(自身)：\n        返回 2\n"
        "令 o = 新建 甲()\n打印(o.双())\n", rust_exe) == "2\n"


def test_rust_method_with_args(rust_exe):
    assert _agree(
        "类 加法器：\n    函数 加(自身, a, b)：\n        返回 a + b\n"
        "令 o = 新建 加法器()\n打印(o.加(3, 4))\n", rust_exe) == "7\n"


def test_rust_initializer_sets_fields(rust_exe):
    """`初始化` 会被调用，且 `自身.字段 = 值` 真的落到实例上。"""
    assert _agree(
        "类 点：\n"
        "    函数 初始化(自身, x, y)：\n"
        "        自身.x = x\n        自身.y = y\n"
        "        自身.和 = x + y\n"
        "令 p = 新建 点(3, 4)\n打印(p.x, p.y, p.和)\n", rust_exe) == "3 4 7\n"


def test_rust_method_mutates_self(rust_exe):
    assert _agree(
        "类 计数：\n"
        "    函数 初始化(自身)：\n        自身.n = 0\n"
        "    函数 加一(自身)：\n        自身.n += 1\n"
        "令 c = 新建 计数()\nc.加一()\nc.加一()\n打印(c.n)\n", rust_exe) == "2\n"


def test_rust_method_calls_other_method(rust_exe):
    assert _agree(
        "类 甲：\n"
        "    函数 一(自身)：\n        返回 1\n"
        "    函数 二(自身)：\n        返回 自身.一() + 自身.一()\n"
        "打印(新建 甲().二())\n", rust_exe) == "2\n"


def test_rust_recursive_method(rust_exe):
    assert _agree(
        "类 斐：\n"
        "    函数 算(自身, n)：\n"
        "        如果 n < 2：\n            返回 n\n"
        "        返回 自身.算(n - 1) + 自身.算(n - 2)\n"
        "令 f = 新建 斐()\n打印(f.算(10))\n", rust_exe) == "55\n"


def test_rust_instances_are_independent(rust_exe):
    """两个实例各有各的字段——这是共享语义下最容易写错的一处。"""
    assert _agree(
        "类 盒：\n    函数 初始化(自身, v)：\n        自身.v = v\n"
        "令 a = 新建 盒(1)\n令 b = 新建 盒(2)\na.v = 99\n打印(a.v, b.v)\n",
        rust_exe) == "99 2\n"


def test_rust_method_override(rust_exe):
    assert _agree(
        '类 基：\n    函数 说(自身)：\n        返回 "基"\n'
        '类 子 继承 基：\n    函数 说(自身)：\n        返回 "子"\n'
        "打印(新建 子().说())\n", rust_exe) == "子\n"


def test_rust_method_returns_container(rust_exe):
    assert _agree(
        "类 甲：\n    函数 造(自身)：\n        返回 [1, 2, 3]\n"
        "打印(新建 甲().造())\n", rust_exe) == "[1, 2, 3]\n"


def test_rust_method_uses_varargs(rust_exe):
    assert _agree(
        "类 记：\n"
        "    函数 记(自身, *项)：\n        返回 项\n"
        "令 o = 新建 记()\n打印(o.记(1, 2, 3))\n", rust_exe) == "[1, 2, 3]\n"


# ---------------------------------------------------------------------------
# M29.2：列表高阶方法（数据流水线）
# ---------------------------------------------------------------------------
# 这一组得以实现的前提是 `VM::call_value`：内建/method 能反过来同步调用
# 用户函数。注意实现里必须先**快照**容器再回调——拿着 RefCell 的借用去回调，
# 回调里又改同一个容器就会撞上运行时借用冲突。

def test_rust_list_map(rust_exe):
    assert _agree("打印([1, 2, 3].映射(函数(x)：x * x))\n",
                  rust_exe) == "[1, 4, 9]\n"


def test_rust_list_map_named_function(rust_exe):
    assert _agree(
        "函数 三倍(x)：\n    返回 x * 3\n打印([1, 2, 3].映射(三倍))\n",
        rust_exe) == "[3, 6, 9]\n"


def test_rust_list_filter(rust_exe):
    assert _agree("打印([1, 2, 3, 4].过滤(函数(x)：x % 2 == 0))\n",
                  rust_exe) == "[2, 4]\n"


def test_rust_list_reduce(rust_exe):
    assert _agree(
        "打印([1, 2, 3, 4].归约(函数(累, x)：累 + x, 0))\n"
        '打印(["甲", "乙", "丙"].归约(函数(累, x)：累 + x, ""))\n',
        rust_exe) == "10\n甲乙丙\n"


def test_rust_list_sort_by(rust_exe):
    assert _agree(
        "打印([3, 1, 2].排序按(函数(x)：x))\n"
        "打印([3, 1, 2].排序按(函数(x)：-x))\n"
        '令 人 = [{"年": 20}, {"年": 18}]\n'
        '打印(人.排序按(函数(p)：p["年"]))\n',
        rust_exe) == "[1, 2, 3]\n[3, 2, 1]\n[{'年': 18}, {'年': 20}]\n"


def test_rust_sort_by_incomparable_keys_is_chinese(rust_exe):
    """排序键混着数字和文本要报中文错，不能悄悄给出错顺序。"""
    rc, out, err = _rs_run(
        '令 混合 = [{"键": 1}, {"键": "甲"}]\n'
        '打印(混合.排序按(函数(x)：x["键"]))\n', rust_exe)
    assert rc != 0
    assert "排序按" in err and "比较" in err


def test_rust_pipeline_chaining(rust_exe):
    assert _agree(
        "打印([1, 2, 3, 4, 5].过滤(函数(x)：x > 2).映射(函数(x)：x * 10))\n",
        rust_exe) == "[30, 40, 50]\n"


def test_rust_map_with_closure(rust_exe):
    assert _agree(
        "令 基 = 10\n打印([1, 2].映射(函数(x)：x + 基))\n",
        rust_exe) == "[11, 12]\n"


def test_rust_map_callback_may_mutate_outer_list(rust_exe):
    """回调里改外部容器也要生效（快照只针对被遍历的那个容器）。"""
    assert _agree(
        "令 表 = []\n[1, 2, 3].映射(函数(x)：表.追加(x * 2))\n打印(表)\n",
        rust_exe) == "[2, 4, 6]\n"


def test_rust_method_call_in_callback(rust_exe):
    assert _agree(
        "类 加一器：\n    函数 算(自身, x)：\n        返回 x + 1\n"
        "令 o = 新建 加一器()\n"
        "打印([1, 2, 3].映射(函数(x)：o.算(x)))\n",
        rust_exe) == "[2, 3, 4]\n"


# ---------------------------------------------------------------------------
# M29.3a：集合类型
# ---------------------------------------------------------------------------

def test_rust_set_literal_and_dedup(rust_exe):
    assert _agree(
        "令 s = {1, 2, 2, 3}\n打印(s)\n打印(长度(s))\n"
        "打印(集合([3, 1, 3, 2, 1]))\n打印(类型(集合()))\n",
        rust_exe) == "{1, 2, 3}\n3\n{3, 1, 2}\n集合\n"


def test_rust_set_membership(rust_exe):
    assert _agree(
        "打印(2 在 {1, 2}, 9 在 {1, 2})\n"
        "打印(9 不在 {1, 2}, 2 不在 {1, 2})\n",
        rust_exe) == "真 假\n真 假\n"


def test_rust_set_operations(rust_exe):
    assert _agree(
        "打印(集合([1, 2]).并集([2, 3]))\n"
        "打印(集合([1, 2, 3]).交集([2, 3, 4]))\n"
        "打印(集合([1, 2, 3]).差集([2]))\n"
        "打印(集合([1, 2]).对称差([2, 3]))\n"
        "打印(集合([2, 1]).转列表())\n"
        "打印(集合([1, 2]) == 集合([2, 1]), 集合([1]) == 集合([2]))\n",
        rust_exe) == "{1, 2, 3}\n{2, 3}\n{1, 3}\n{1, 3}\n[2, 1]\n真 假\n"


def test_rust_set_mutating_methods(rust_exe):
    assert _agree(
        "令 s = 集合()\ns.添加(1)\ns.添加(2)\ns.添加(1)\ns.丢弃(2)\n"
        "打印(s, 1 在 s, 2 在 s)\n"
        "令 t = 集合([1, 2])\nt.移除(1)\n打印(t)\n"
        "t.清空()\n打印(t, 长度(t))\n",
        rust_exe) == "{1} 真 假\n{2}\n集合() 0\n"


def test_rust_set_iteration_and_comprehension(rust_exe):
    assert _agree(
        "遍历 x 在 集合([3, 1, 2])：\n    打印(x)\n"
        "打印({x % 3 遍历 x 在 范围(9)})\n",
        rust_exe) == "3\n1\n2\n{0, 1, 2}\n"


def test_rust_set_rejects_mutable_elements(rust_exe):
    """列表/字典不能进集合（对齐 Python 侧「元素必须可哈希」），要报中文错。"""
    rc, _out, err = _rs_run("打印(集合([[1]]))\n", rust_exe)
    assert rc != 0
    assert "放进集合" in err


# ---------------------------------------------------------------------------
# M29.3b：文件对象与上下文管理器 `用 … 为`
# ---------------------------------------------------------------------------
# `用 … 为` 在解析期脱糖成「临时变量 + 绑定 + 尝试/最终」，所以 Rust 侧
# 只需要两个内建（进入上下文 / 退出上下文）和文件对象本身。
#
# 顺带修掉 Rust 侧两个真 bug：
# ① 出错时 `execute` 直接 `return Err(e)`，**绕过了异常分发**——于是
#    `尝试/捕获/最终` 对「内置错误」完全失效（Flow::Error 根本没人构造）；
# ② `返回` 不跑 finally（缺 Python 侧的 `_return_via_finally`），
#    于是 `用` 块里 `返回` 会让文件不关。

def test_rust_file_write_then_read(rust_exe, tmp_path):
    f = tmp_path / "甲.txt"
    assert _agree(
        f'用 打开("{f.as_posix()}", "写") 为 f：\n'
        f'    f.写行("第一行")\n'
        f'    f.写行("第二行")\n'
        f'用 打开("{f.as_posix()}") 为 f：\n'
        f'    遍历 行 在 f：\n'
        f'        打印(行)\n', rust_exe) == "第一行\n第二行\n"


def test_rust_file_read_all_lines(rust_exe, tmp_path):
    f = tmp_path / "甲.txt"
    f.write_text("第一行\n第二行\n", encoding="utf-8")
    assert _agree(f'打印(打开("{f.as_posix()}").读所有行())\n',
                  rust_exe) == "['第一行', '第二行']\n"


def test_rust_file_readline_to_end(rust_exe, tmp_path):
    f = tmp_path / "甲.txt"
    f.write_text("甲\n乙\n", encoding="utf-8")
    assert _agree(
        f'令 f = 打开("{f.as_posix()}")\n打印(f.读行())\n打印(f.读行())\n打印(f.读行())\n',
        rust_exe) == "甲\n乙\n空\n"


def test_rust_file_append(rust_exe, tmp_path):
    f = tmp_path / "乙.txt"
    assert _agree(
        f'用 打开("{f.as_posix()}", "写") 为 f：\n    f.写("甲")\n'
        f'用 打开("{f.as_posix()}", "追加") 为 f：\n    f.写("乙")\n'
        f'打印(打开("{f.as_posix()}").读())\n', rust_exe) == "甲乙\n"


def test_rust_file_missing_is_chinese(rust_exe, tmp_path):
    missing = tmp_path / "没有这个.txt"
    rc, _out, err = _rs_run(f'打印(打开("{missing.as_posix()}").读())\n', rust_exe)
    assert rc != 0
    assert "找不到文件或目录" in err


def test_rust_file_closed_is_chinese(rust_exe, tmp_path):
    f = tmp_path / "甲.txt"
    f.write_text("甲\n", encoding="utf-8")
    rc, _out, err = _rs_run(
        f'令 f = 打开("{f.as_posix()}")\nf.关闭()\nf.读()\n', rust_exe)
    assert rc != 0
    assert "已经关闭" in err


def test_rust_with_protocol_on_custom_object(rust_exe):
    assert _agree(
        "类 记：\n"
        "    函数 初始化(自身)：\n        自身.足 = []\n"
        "    函数 进入(自身)：\n        自身.足.追加(\"进\")\n        返回 自身\n"
        "    函数 退出(自身)：\n        自身.足.追加(\"出\")\n"
        "令 g = 新建 记()\n用 g 为 h：\n    h.足.追加(\"用\")\n"
        "打印(g.足)\n", rust_exe) == "['进', '用', '出']\n"


def test_rust_with_cleanup_on_exception(rust_exe):
    """`抛出` 之后 finally 也要跑（这曾因「出错绕过异常分发」而失效）。"""
    assert _agree(
        "类 门：\n"
        "    函数 初始化(自身)：\n        自身.关 = 假\n"
        "    函数 关闭(自身)：\n        自身.关 = 真\n"
        "令 d = 新建 门()\n"
        "尝试：\n    用 d 为 x：\n        抛出 \"出错了\"\n"
        "捕获 为 e：\n    打印(\"接住\")\n"
        "打印(d.关)\n", rust_exe) == "接住\n真\n"


def test_rust_with_cleanup_on_return(rust_exe):
    """`返回` 之后 finally 也要跑（这曾因缺 `_return_via_finally` 而失效）。"""
    assert _agree(
        "类 门：\n"
        "    函数 初始化(自身)：\n        自身.关 = 假\n"
        "    函数 关闭(自身)：\n        自身.关 = 真\n"
        "令 d = 新建 门()\n"
        "函数 试()：\n    用 d 为 x：\n        返回 \"提前\"\n"
        "打印(试(), d.关)\n", rust_exe) == "提前 真\n"


def test_rust_with_cleanup_on_break(rust_exe):
    """循环里的 `中断` 穿过 `用` 块时也要先清理（信号借道 finally）。"""
    assert _agree(
        "类 门：\n"
        "    函数 初始化(自身)：\n        自身.关 = 假\n"
        "    函数 关闭(自身)：\n        自身.关 = 真\n"
        "令 d = 新建 门()\n"
        "遍历 i 在 范围(3)：\n"
        "    用 d 为 x：\n"
        "        打印(i)\n"
        "        中断\n"
        "打印(d.关)\n", rust_exe) == "0\n真\n"


def test_rust_try_except_catches_builtin_error(rust_exe):
    """内置错误（除零）也要能被 `捕获` 接住——以前只有 `抛出` 之外的全漏掉。"""
    assert _agree(
        "尝试：\n    打印(1 / 0)\n"
        "捕获 除零错误 为 e：\n    打印(\"接住除零\")\n"
        "尝试：\n    打印([1][9])\n"
        "捕获 索引错误 为 e：\n    打印(\"接住越界\")\n"
        "尝试：\n    打印(1 + \"甲\")\n"
        "捕获 类型错误 为 e：\n    打印(\"接住类型\")\n",
        rust_exe) == "接住除零\n接住越界\n接住类型\n"


# ---------------------------------------------------------------------------
# M29.3c：精确小数（零依赖 BigInt 实现）
# ---------------------------------------------------------------------------
# Python 侧借的是 `decimal.Decimal`（默认 28 位有效数字、ROUND_HALF_EVEN），
# JS 侧是 BigInt 手写的十进制。Rust 侧零第三方依赖，所以也手写了 BigInt
# （`rust/src/decimal.rs`）。这一组把「逐字节一致」锁死。

@pytest.mark.parametrize("src,expect", [
    ('打印(精确("19.99"))\n', "19.99\n"),
    ("打印(精确(42))\n", "42\n"),
    ("打印(精确(1.5))\n", "1.5\n"),
    ('打印(精确("-3.25"))\n', "-3.25\n"),
    ('打印(精确("1.50"), 精确("1.50") * 2)\n', "1.50 3.00\n"),
    ('打印(精确("0.1") + 精确("0.2"))\n', "0.3\n"),
    ('打印(0.1 + 0.2 == 0.3, 精确("0.1") + 精确("0.2") == 精确("0.3"))\n',
     "假 真\n"),
    ('打印(精确("19.99") - 精确("20"))\n', "-0.01\n"),
    ('打印(精确("19.99") * 3)\n', "59.97\n"),
    ('打印(精确("10") / 精确("4"), 精确("1") / 精确("3"))\n',
     "2.5 0.3333333333333333333333333333\n"),
    ('打印(精确("7.5") // 精确("2"), 精确("7.5") % 精确("2"))\n', "3 1.5\n"),
    ('打印(精确("1.05") ** 12)\n', "1.795856326022129150390625\n"),
    ('打印(精确("2.345").舍入(2), 精确("2.5").舍入(0), 精确("2.4").舍入(0))\n',
     "2.35 3 2\n"),
    ('打印(精确("1234").舍入(-2))\n', "1200\n"),
    ('打印(精确("-3.5").绝对值())\n', "3.5\n"),
    ('打印(精确("1.5").转文本() + "元")\n', "1.5元\n"),
    ('打印(精确("0.3") == 0.3, 精确("1") < 精确("2"), 精确("2") >= 精确("2"))\n',
     "真 真 真\n"),
    ('打印(总和([精确("0.1"), 精确("0.2"), 精确("0.3")]))\n', "0.6\n"),
    ('打印(最大([精确("1.5"), 精确("9.25")]), 最小([精确("1.5"), 精确("9.25")]))\n',
     "9.25 1.5\n"),
    ('令 表 = [精确("3.3"), 精确("1.1"), 精确("2.2")]\n表.排序()\n打印(表)\n',
     "[1.1, 2.2, 3.3]\n"),
    ('打印(类型(精确("1")))\n', "精确小数\n"),
    ('打印("合计 " + 文本(精确("19.99") * 2) + " 元")\n', "合计 39.98 元\n"),
    ('打印(精确("0.1") + 0.2)\n', "0.3\n"),
])
def test_rust_decimal(src, expect, rust_exe):
    assert _agree(src, rust_exe) == expect


def test_rust_decimal_rejects_bool(rust_exe):
    rc, _out, err = _rs_run("打印(精确(真))\n", rust_exe)
    assert rc != 0
    assert "布尔值" in err


def test_rust_decimal_non_integer_power_is_chinese(rust_exe):
    """非整数次幂是近似运算，各执行器会漂移——明确不支持，不静默给近似值。"""
    rc, _out, err = _rs_run('打印(精确("2") ** 精确("0.5"))\n', rust_exe)
    assert rc != 0
    assert "指数要整数" in err


def test_rust_decimal_cannot_enter_set(rust_exe):
    """精确小数放不进集合（Python 侧 `__hash__ = None`），要报中文错。"""
    rc, _out, err = _rs_run('打印(集合([精确("1")]))\n', rust_exe)
    assert rc != 0
    assert "放进集合" in err


# ---------------------------------------------------------------------------
# M29：同一性扩展到实例（实例有了 `Rc` 身份之后）
# ---------------------------------------------------------------------------

def test_rust_identity_on_instance(rust_exe):
    """实例现在有对象身份了：`a 是 b`（b = a）为真，两个 `新建` 互不相同。"""
    assert _agree(
        "类 甲：\n    函数 初始化(自身)：\n        空\n"
        "令 a = 新建 甲()\n令 b = a\n令 c = 新建 甲()\n"
        "打印(a 是 b, a 是 c, a 不是 c)\n", rust_exe) == "真 假 真\n"


def test_rust_builtin_len_and_max_on_iterable_object(rust_exe):
    """`长度`/`最大`/`最小` 对定义了「迭代」的自定义对象也能用。"""
    assert _agree(
        "类 盒：\n"
        "    函数 初始化(自身, 项)：\n        自身.项 = 项\n"
        "    函数 迭代(自身)：\n        返回 自身.项\n"
        "令 b = 新建 盒([3, 1, 2])\n"
        "遍历 x 在 b：\n    打印(x)\n"
        "打印(长度(b), 最大(b), 最小(b))\n",
        rust_exe) == "3\n1\n2\n3 3 1\n"

