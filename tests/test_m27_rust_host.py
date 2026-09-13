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

from jishi import serialize                       # noqa: E402
from jishi import vm as vm_mod                    # noqa: E402
from jishi.compiler import compile_source         # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "rust" / "target" / "release" / "jishi-rs.exe"
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
    bc = ROOT / "_tmp_rust_bc.json"
    bc.write_text(serialize.dumps(compile_source(src, "<t>")), encoding="utf-8")
    try:
        r = subprocess.run([str(exe), str(bc)], capture_output=True,
                           text=True, encoding="utf-8")
        return r.returncode, r.stdout, r.stderr
    finally:
        bc.unlink(missing_ok=True)


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


def test_rust_class_display_falls_back(rust_exe):
    """Rust 侧没有自定义对象打印（拿不到 VM 调「文本」），落回 `<类名 实例>`。"""
    assert _agree(
        "类 甲：\n    函数 初始化(自身)：\n        空\n打印(1)\n",
        rust_exe) == "1\n"


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
# 已知缺口（明确标出，修好后 xfail 会变成 XPASS 提醒我们更新）
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="Rust 版用户方法调用尚未经 VM 帧实现", strict=False)
def test_rust_method_call(rust_exe):
    assert _agree(
        "类 甲：\n    函数 双(自身)：\n        返回 1\n"
        "令 o = 新建 甲()\n打印(o.双())\n", rust_exe) == "1\n"


@pytest.mark.xfail(reason="Rust 版尚未移植 M24 的集合类型", strict=False)
def test_rust_set_literal(rust_exe):
    assert _agree("令 s = {1, 2, 2}\n打印(s)\n", rust_exe) == "{1, 2}\n"


@pytest.mark.xfail(reason="Rust 版列表高阶方法需要把 VM 传进方法调用路径，"
                          "尚未实现", strict=False)
def test_rust_list_map(rust_exe):
    assert _agree("打印([1, 2].映射(函数(x)：x * 2))\n",
                  rust_exe) == "[2, 4]\n"


@pytest.mark.xfail(reason="Rust 版的类/实例是值类型、没有对象身份，"
                          "对它们用「是/不是」会明确报错（不做静默降级）",
                   strict=False)
def test_rust_identity_on_instance_errors(rust_exe):
    assert _agree(
        "类 甲：\n    函数 初始化(自身)：\n        空\n"
        "令 a = 新建 甲()\n令 b = a\n打印(a 是 b)\n", rust_exe) == "真\n"

