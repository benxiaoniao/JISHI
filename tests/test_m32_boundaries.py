# -*- coding: utf-8 -*-
"""M32：把 docstring 里「剩余边界」逐条核实，修掉真正还在的「静默算错」。

这一轮的起点是一次**横向探测**（把 `docs/embed.md` 的边界清单逐条在
三个宿主上实测），因为文档会过时——上一轮就撞过一次（embed.md 给 JS 的
成员测试标了 ✅，实际根本没实现）。探测结果分三类：

**一、文档过时（其实早就好了）**

- 「JS 的 `1` 与 `1.0` 无法区分」——`node/index.js` 的 `decodeConst` 早就按
  字节码里的 `t: "float"` 把常量包成了 `FloatBox`。删掉这条。
- 「JS 的错误渲染没有源码行」——M30 已经规范化成中文。

**二、真还在的「静默算错」（本轮修掉）**

- **JS 大整数**：`9223372036854775807` 显示成 `...776000`、`2 ** 64` 算错。
  JS 的 number 是双精度。→ 必要时切 `BigInt`（任意精度，与 Python 对齐）。
- **Rust 整数溢出**：`9223372036854775807 + 1` 给 **`-9223372036854775808`**
  （回绕成负数！）。→ `i64` 提到 `i128` + `checked_*`，到边界明确报错。
- **JS 的 `打印(类)` / `打印(函数)`** 给 `[object Object]`。
  → 给那几个类补 `toString()`（对应 Python 侧靠 `__repr__` 兜底的手法）。
- **精确小数 28 位精度**：Python 的 `Decimal` 每次运算都走 context
  （`prec=28`, `ROUND_HALF_EVEN`），而 JS/Rust 只给 `div` 加了舍入——
  于是「加减乘超 28 位」时三个宿主给出**不同的数**。
  → 补齐 `add/sub/mul` 的舍入，并按 CPython 的 `__str__` 规则实现科学计数法。

**三、明确报错（本轮补上）**

- **Rust 的类型标注校验**：编译器早已把校验发射成函数的前导字节码，
  Rust 侧没实现 `检查实参` 内建，于是带标注的函数直接报「找不到名字」。
- **Rust 的 `是`/`不是` 对类与函数**：它们在 Rust 里是值类型、没有身份。
  → 创建时分配递增序号（Clone 跟着复制），等价于 Python 的对象身份。
  **绑定方法仍明确报错**：Python 里 `a.方法 是 a.方法` 为假，而 Rust 侧
  用「实例指针 + 方法名」判会得到真——宁可明确不支持，也不给反直觉的答案。

剩下的是 Rust 宿主的标准库（只有数学/文本/随机三个模块），留待下一轮。
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

from jishi import cvm_bind                        # noqa: E402
from jishi import serialize                       # noqa: E402
from jishi import vm as vm_mod                    # noqa: E402
from jishi.compiler import compile_source         # noqa: E402
from jishi.errors import JishiError               # noqa: E402
from jishi.interpreter import Interpreter         # noqa: E402
from jishi.parser import parse                    # noqa: E402
from jishi.tokenizer import tokenize              # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
EXE = rust_exe_path()   # 平台感知：Windows 是 .exe，别处没有扩展名
CARGO = shutil.which("cargo")


# ---------------------------------------------------------------------------
# 对拍脚手架
# ---------------------------------------------------------------------------

def _py_run(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        vm_mod.VM(compile_source(src, "<t>"), "<t>").run()
    return buf.getvalue()


def _tree_run(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        Interpreter(filename="<t>").run(
            parse(tokenize(src, "<t>"), src.split("\n"), "<t>"))
    return buf.getvalue()


def _c_run(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        cvm_bind.run_source_c(src, "<t>")
    return buf.getvalue()


def _write_bc(src: str, name: str) -> Path:
    # 名字带进程号：跨进程并发跑测试时不会互相覆写（M32 的假失败就是这么来的）
    bc = tmp_bc_path(ROOT / name)
    bc.write_text(serialize.dumps(compile_source(src, "<t>")), encoding="utf-8")
    return bc


def _node_run(src: str) -> tuple[int, str, str]:
    bc = _write_bc(src, "_tmp_m32_bc.json")
    r = subprocess.run([NODE, str(ROOT / "node" / "index.js"), str(bc)],
                       capture_output=True, text=True, encoding="utf-8")
    return r.returncode, r.stdout, r.stderr


def _agree(src: str) -> str:
    """Python VM 与 Node 逐字节一致（Node 不得非零退出）。"""
    py = _py_run(src)
    rc, out, err = _node_run(src)
    assert rc == 0, f"Node 非零退出：rc={rc}\n{err[:400]}"
    assert out == py, f"输出不一致\nPython: {py!r}\nNode:   {out!r}"
    return out


def _agree_all(src: str) -> str:
    """树遍历 / Python VM / C VM / Node 四路一致。"""
    t = _tree_run(src)
    p = _py_run(src)
    c = _c_run(src)
    assert t == p == c, f"三执行器不一致：树={t!r} VM={p!r} C={c!r}"
    return _agree(src)


def _node_err(src: str) -> str:
    rc, _out, err = _node_run(src)
    assert rc != 0, f"这段代码本该报错，但 Node 正常退出了：{_out!r}"
    assert "JishiError" not in err.splitlines()[0], f"错误没被接住：{err[:300]}"
    return err


def _build_env() -> dict:
    env = dict(os.environ)
    mingw = env.get("JISHI_MINGW")
    if mingw and Path(mingw).is_dir():
        env["PATH"] = mingw + os.pathsep + env.get("PATH", "")
    return env


@pytest.fixture(scope="session")
def rust_exe() -> Path:
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


def _rust_run(src: str, exe: Path) -> tuple[int, str, str]:
    bc = _write_bc(src, "_tmp_m32_rs.json")
    r = subprocess.run([str(exe), str(bc)], capture_output=True,
                       text=True, encoding="utf-8")
    return r.returncode, r.stdout, r.stderr


def _agree_rust(src: str, exe: Path) -> str:
    py = _py_run(src)
    rc, out, err = _rust_run(src, exe)
    assert rc == 0, f"Rust 宿主非零退出：rc={rc}\nstderr={err[:400]}"
    assert out == py, f"输出不一致\nPython: {py!r}\nRust:   {out!r}"
    return out


def _rust_err(src: str, exe: Path) -> str:
    rc, _out, err = _rust_run(src, exe)
    assert rc != 0, "这段代码本该报错，但 Rust 宿主正常退出了"
    return err


# ---------------------------------------------------------------------------
# 1. 文档过时：`1` 与 `1.0` 其实分得清
# ---------------------------------------------------------------------------

def test_constant_float_is_marked_in_bytecode():
    """字节码里的小数常量带 `t: "float"` 标记，宿主据此包成 FloatBox。

    这条曾长期被记成「JS 表示层的固有限制」——其实是文档没跟上：
    `decodeConst` 早就按 `c.t` 分派了。
    """
    import json
    cm = compile_source("令 x = 1.0\n令 y = 1\n", "<t>")
    body = json.loads(serialize.dumps(cm))
    kinds = [c["t"] for c in body["payload"]["consts"]]
    assert "float" in kinds and "int" in kinds, kinds


def test_annotation_accepts_float_literal():
    """`函数 f(x: 小数)` 收字面量 `1.0` 必须放行（Python 与 JS 都给 1.0）。"""
    assert _agree_all(
        '函数 f(x: 小数)：\n    返回 x\n打印(f(1.0))\n') == "1.0\n"
    assert _agree_all(
        '函数 f(x: 小数)：\n    返回 x\n打印(f(1.0 / 2))\n') == "0.5\n"


def test_annotation_rejects_int_for_float():
    """反过来，整数传给「小数」标注要报错（三个宿主文案一致）。"""
    src = '函数 f(x: 小数)：\n    返回 x\n打印(f(1))\n'
    with pytest.raises(JishiError) as ei:
        _py_run(src)
    assert "要求是「小数」" in str(ei.value)
    err = _node_err(src)
    assert "要求是「小数」" in err


# ---------------------------------------------------------------------------
# 2. JS 大整数（BigInt）
# ---------------------------------------------------------------------------

def test_big_int_literal_survives_bytecode():
    """大整数字面量要原样显示 —— 双精度会把它变成 `...776000`。"""
    # 只用 Python VM + Node：C 虚拟机的整数是 64 位（见下面的边界测试）
    assert _agree("打印(9223372036854775807)\n") == "9223372036854775807\n"


def test_big_int_addition_beyond_i64():
    assert _agree("打印(9223372036854775807 + 1)\n") == "9223372036854775808\n"


def test_big_int_power():
    """`2 ** 62` / `2 ** 64` 都要精确（Python 的 int 是任意精度）。"""
    assert _agree("打印(2 ** 62)\n") == "4611686018427387904\n"
    assert _agree("打印(2 ** 64)\n") == "18446744073709551616\n"


def test_big_int_beyond_i128():
    """Python/JS 是任意精度，所以连 2^200 都算得出（Rust 侧到边界报错）。"""
    assert _agree("打印(2 ** 100)\n") == "1267650600228229401496703205376\n"


def test_big_int_stays_integer_type():
    """大整数仍然是「整数」——不能因为内部换了表示就让 `类型()` 变样。"""
    assert _agree("打印(类型(2 ** 62), 类型(2 ** 62 // 1), 类型(2 ** 0))\n") \
        == "整数 整数 整数\n"


def test_big_int_comparison_and_equality():
    assert _agree(
        "打印(2 ** 62 > 2 ** 61, 2 ** 62 == 2 ** 62, 2 ** 62 < 1)\n"
        "令 a = 2 ** 62\n打印(a == a, a != a + 1)\n"
    ) == "真 真 假\n真 真\n"


def test_big_int_floor_div_and_mod():
    """大整数的 `//` 与 `%` 也要按 Python 语义（向下取整、符号跟除数）。"""
    assert _agree(
        "打印((2 ** 64) // 3, (2 ** 64) % 3)\n"
        "打印((0 - 2 ** 64) // 3, (0 - 2 ** 64) % 3)\n"
        "打印((2 ** 64) // (2 ** 62))\n"
    ) == "6148914691236517205 1\n-6148914691236517206 2\n4\n"


def test_big_int_in_containers():
    """大整数进容器、进集合（键规范化要把它和同值整数当同一个）。"""
    assert _agree(
        "令 big = 2 ** 62\n"
        "打印([big, big + 1])\n"
        "令 s = 集合([big, big, 1])\n"
        "打印(长度(s), big 在 s)\n"
    ) == "[4611686018427387904, 4611686018427387905]\n2 真\n"


def test_big_int_sum():
    assert _agree(
        "打印(总和([2 ** 62, 2 ** 62]))\n"
        "打印(总和([1, 2, 3]))\n"
    ) == "9223372036854775808\n6\n"      # 2**62 的两倍就是 2**63


def test_big_int_json_roundtrip():
    """JSON 编码大整数要写成数字字面量（不能带 `n`、也不能丢精度）。"""
    assert _agree(
        '导入 json\n令 big = 2 ** 64\n'
        '打印(json.转文本({"大": big}))\n'
        '打印(json.解析(json.转文本({"大": big}))["大"] == big)\n'
    ) == '{"大": 18446744073709551616}\n真\n'


def test_big_int_index_is_out_of_range():
    """`a[2 ** 62]` 该报越界——不能把大整数悄悄转成双精度去取下标。"""
    src = "令 a = [1, 2, 3]\n打印(a[2 ** 62])\n"
    with pytest.raises(JishiError):
        _py_run(src)
    assert _node_err(src)


def test_cvm_big_int_reports_clearly():
    """C 虚拟机的整数是 64 位：超出时**明确报错**，绝不静默给错值。

    修之前这里是「静默算错」——`2 ** 64` 给 `0`、`i64_MAX + 1` 给负数、
    `阶乘 25` 给一个完全不着边的数。根因是 ctypes 赋给 `c_int64` 时
    **静默截断**，所以检查必须放在 `_jint` 里（C 侧本来就有溢出回落，
    是回落之后在边界上丢的）。
    """
    for src in ["打印(9223372036854775807 + 1)\n", "打印(2 ** 64)\n",
                "打印(2 ** 100)\n"]:
        with pytest.raises(JishiError) as ei:
            _c_run(src)
        assert "64 位整数" in str(ei.value), str(ei.value)


def test_cvm_i64_range_still_agrees():
    """i64 范围内三执行器必须完全一致（绝大多数程序都落在这里）。"""
    assert _agree_all("打印(2 ** 62)\n") == "4611686018427387904\n"
    assert _agree_all("打印(9223372036854775807)\n") == "9223372036854775807\n"
    assert _agree_all("打印(2 ** 62 - 1)\n") == "4611686018427387903\n"
    assert _agree_all("打印(-(2 ** 62) * 2)\n") == "-9223372036854775808\n"


def test_small_int_path_unchanged():
    """普通小整数仍走双精度快路径（结果与行为不能因为加大整数而变）。"""
    assert _agree_all(
        "打印(1 + 2, 10 // 3, 7 % 3, 2 ** 10)\n"
        "打印(类型(1), 类型(2 ** 10), 类型(1.5))\n"
        "打印(-7 % 3, 7 % -3)\n"
    ) == "3 3 1 1024\n整数 整数 小数\n2 -2\n"


# ---------------------------------------------------------------------------
# 3. Rust 整数溢出：明确报错，不再静默回绕
# ---------------------------------------------------------------------------

def test_rust_i64_overflow_no_longer_wraps(rust_exe):
    """这条以前给 `-9223372036854775808`（回绕成负数）——最危险的静默算错。"""
    assert _agree_rust("打印(9223372036854775807 + 1)\n", rust_exe) \
        == "9223372036854775808\n"


def test_rust_power_beyond_i64(rust_exe):
    assert _agree_rust("打印(2 ** 62, 2 ** 64)\n", rust_exe) \
        == "4611686018427387904 18446744073709551616\n"


def test_rust_beyond_i128_reports_error(rust_exe):
    """i128 也会到边界。到边界就报错——绝不悄悄给一个错的数。"""
    err = _rust_err("打印(2 ** 200)\n", rust_exe)
    assert "太大" in err and "Rust" in err, err


# ---------------------------------------------------------------------------
# 4. 类与函数的显示
# ---------------------------------------------------------------------------

def test_display_of_class_and_function():
    """以前 JS 给 `[object Object]` —— 现在与 Python / Rust 一致。"""
    assert _agree_all(
        "类 甲：\n    函数 m(自身)：\n        返回 1\n"
        "函数 乙()：\n    返回 1\n"
        "打印(甲)\n打印(乙)\n打印([甲, 乙])\n"
    ) == "<类 甲>\n<函数 乙>\n[<类 甲>, <函数 乙>]\n"


def test_display_of_builtin_and_module():
    assert _agree_all(
        "导入 数学\n打印(打印)\n打印(数学)\n打印([打印, 数学])\n"
    ) == "<内建 打印>\n<模块 数学>\n[<内建 打印>, <模块 数学>]\n"


def test_display_of_instance_and_exception_type():
    assert _agree_all(
        "类 甲：\n    函数 m(自身)：\n        返回 1\n令 a = 新建 甲()\n"
        "打印(a)\n打印(值错误)\n"
    ) == "<甲 实例>\n<异常类型 值错误>\n"


def test_class_text_method_still_wins():
    """定义了「文本()」的对象仍按用户写的显示（不能被新加的兜底覆盖）。"""
    assert _agree_all(
        "类 钱：\n"
        "    函数 初始化(自身, 分)：\n        自身.分 = 分\n"
        "    函数 文本(自身)：\n        返回 文本(自身.分) + \" 分\"\n"
        "令 a = 新建 钱(5)\n"
        "打印(a)\n打印([a])\n"
    ) == "5 分\n[5 分]\n"


# ---------------------------------------------------------------------------
# 5. 精确小数：28 位精度与显示
# ---------------------------------------------------------------------------

def test_decimal_add_mul_round_to_28_digits():
    """Python 的 Decimal 每次运算都走 context（prec=28, ROUND_HALF_EVEN）。

    以前只有 `div` 做了舍入，于是「加减乘超 28 位」时三个宿主给出不同的数
    ——那是静默算错，比报错糟得多。
    """
    assert _agree_all(
        '打印(精确("9999999999999999999999999999") + 1)\n'
        '打印(精确("1234567890123456789012345678") * 10)\n'
        '打印(精确("123456789012345678901234567890") + 0)\n'
        '打印(精确("-9999999999999999999999999999") - 1)\n'
    ) == ("1.000000000000000000000000000E+28\n"
          "1.234567890123456789012345678E+28\n"
          "1.234567890123456789012345679E+29\n"
          "-1.000000000000000000000000000E+28\n")


def test_decimal_display_follows_cpython_str():
    """`str(Decimal)` 的规则：`exp <= 0 且 leftdigits > -6` 才用定点。"""
    assert _agree_all(
        '打印(精确("1200"))\n'
        '打印(精确("1.2E+3"))\n'
        '打印(精确("0.00000001"))\n'
        '打印(精确("123456789012345678901234567890"))\n'
        '打印(精确("0.00"), 精确("0"))\n'
    ) == ("1200\n1.2E+3\n1E-8\n123456789012345678901234567890\n0.00 0\n")


def test_decimal_normal_amounts_unchanged():
    """日常金额规模不受影响（十来位，远不到 28 位有效数字）。"""
    assert _agree_all(
        '打印(精确("19.99") + 精确("0.1") + 精确("0.2"))\n'
        '打印(精确("1.50") * 2)\n'
        '打印(精确("1") / 精确("3"))\n'
        '打印(精确("0.1") + 精确("0.2") == 精确("0.3"))\n'
    ) == "20.29\n3.00\n0.3333333333333333333333333333\n真\n"


def test_decimal_round_follows_python_quantize():
    """`舍入` 要对齐 Python 的 `quantize` + `Decimal(format(r, "f"))`。

    两个细节：① `舍入(2)` 要**补足小数位**（`1234` → `1234.00`），
    ② 舍到十位/百位要换回定点写法（`1.2E+3` → `1200`）。
    """
    assert _agree_all(
        '打印(精确("1234").舍入(-2))\n'
        '打印(精确("1234").舍入(0))\n'
        '打印(精确("1234").舍入(2))\n'
        '打印(精确("1250").舍入(-2))\n'
        '打印(精确("9999").舍入(-3))\n'
        '打印(精确("0.12345").舍入(3))\n'
    ) == "1200\n1234\n1234.00\n1300\n10000\n0.123\n"


def test_decimal_round_default_is_two():
    assert _agree_all('打印(精确("1.005").舍入())\n') == "1.01\n"


# ---------------------------------------------------------------------------
# 6. Rust 类型标注校验（补上 `检查实参`）
# ---------------------------------------------------------------------------

def test_rust_annotation_check(rust_exe):
    """编译器把校验发射成前导字节码，宿主实现 `检查实参` 就自动获得能力。"""
    assert _agree_rust(
        '函数 f(x: 整数)：\n    返回 x\n打印(f(1))\n', rust_exe) == "1\n"


def test_rust_annotation_rejects_wrong_type(rust_exe):
    err = _rust_err('函数 f(x: 整数)：\n    返回 x\n打印(f("甲"))\n', rust_exe)
    assert "要求是「整数」" in err and "文本" in err, err
    err = _rust_err('函数 f(x: 文本)：\n    返回 x\n打印(f(1))\n', rust_exe)
    assert "要求是「文本」" in err and "整数" in err, err


def test_rust_annotation_unknown_name_is_doc_only(rust_exe):
    """作者自定义的标注（不在表里）只作文档，不该误报。"""
    assert _agree_rust(
        '函数 f(x: 商品)：\n    返回 x\n打印(f(1))\n', rust_exe) == "1\n"


def test_rust_annotation_all_known_names(rust_exe):
    """表里的名字逐个走一遍。拆成小条，一处出错好定位。"""
    cases = [
        ('函数 f(a: 小数)：\n    返回 a\n打印(f(1.5))\n', "1.5\n"),
        ('函数 f(a: 精确小数)：\n    返回 a\n打印(f(精确("1.5")))\n', "1.5\n"),
        ('函数 f(a: 文本)：\n    返回 a\n打印(f("甲"))\n', "甲\n"),
        ('函数 f(a: 列表)：\n    返回 长度(a)\n打印(f([1, 2]))\n', "2\n"),
        ('函数 f(a: 字典)：\n    返回 长度(a)\n打印(f({"k": 1}))\n', "1\n"),
        ('函数 f(a: 集合)：\n    返回 长度(a)\n打印(f(集合([1])))\n', "1\n"),
        ('函数 f(a: 布尔)：\n    返回 a\n打印(f(真))\n', "真\n"),
        # 注：Python 的判定表里有「空」，但 `空` 是关键字、写不成类型标注，
        # 所以这条语法上立不住（两边的「死条目」，留着无害）。
        ('函数 f(a: 任意)：\n    返回 a\n打印(f(0))\n', "0\n"),
        ('函数 f(a: 值)：\n    返回 a\n打印(f(1))\n', "1\n"),
        # `函数` 与 `空` 一样是关键字，写不成类型标注——判定表里那两条
        # 语法上立不住（保留无害，将来若允许了自然生效）。
    ]
    for src, want in cases:
        assert _agree_rust(src, rust_exe) == want, src


# ---------------------------------------------------------------------------
# 7. Rust 同一性：类与函数
# ---------------------------------------------------------------------------

def test_rust_identity_of_class(rust_exe):
    assert _agree_rust(
        "类 A：\n    :\n类 B：\n    :\n打印(A 是 A, A 是 B)\n",
        rust_exe) == "真 假\n"


def test_rust_identity_of_function(rust_exe):
    assert _agree_rust(
        "函数 f()：\n    返回 1\n函数 g()：\n    返回 1\n"
        "打印(f 是 f, f 是 g)\n", rust_exe) == "真 假\n"


def test_rust_identity_alias_keeps_same_id(rust_exe):
    """别名（同一个对象赋给另一个名字）仍是同一个。"""
    assert _agree_rust(
        "类 A：\n    :\n令 b = A\n打印(A 是 b)\n"
        "函数 f()：\n    返回 1\n令 g = f\n打印(f 是 g)\n",
        rust_exe) == "真\n真\n"


def test_rust_identity_builtin_and_module_and_exctype(rust_exe):
    assert _agree_rust(
        "导入 数学\n打印(打印 是 打印, 数学 是 数学, 值错误 是 值错误)\n"
        "打印(打印 是 长度)\n", rust_exe) == "真 真 真\n假\n"


def test_rust_identity_all_sides_agree(rust_exe):
    """同一段代码在 Python / Node / Rust 三边结果一致。"""
    src = (
        "类 A：\n    函数 m(自身)：\n        返回 1\n"
        "类 B：\n    函数 m(自身)：\n        返回 1\n"
        "函数 f()：\n    返回 1\n"
        "令 a = 新建 A()\n令 a2 = a\n令 a3 = 新建 A()\n"
        "令 lst = [1]\n令 lst2 = lst\n令 lst3 = [1]\n"
        '打印(A 是 A, A 是 B)\n'
        '打印(f 是 f, a 是 a2, a 是 a3)\n'
        '打印(lst 是 lst2, lst 是 lst3)\n'
        '打印("甲" 是 "甲", 1 是 1, 空 是 空, 真 是 真)\n'
    )
    expected = "真 假\n真 真 假\n真 假\n真 真 真 真\n"
    assert _agree_all(src) == expected
    assert _agree_rust(src, rust_exe) == expected


def test_rust_bound_method_identity_still_reports_clearly(rust_exe):
    """绑定方法仍明确报错——Python 里 `a.方法 是 a.方法` 为假，
    而 Rust 侧按「实例指针 + 方法名」判会得到真。宁可明确不支持。"""
    src = ("类 A：\n    函数 m(自身)：\n        返回 1\n"
           "令 a = 新建 A()\n打印(a.m 是 a.m)\n")
    # Python 与 Node 这边不报错（每次取属性得到的是新对象，所以为假）
    assert _agree(src) == "假\n"
    err = _rust_err(src, rust_exe)
    assert "是 / 不是" in err or "暂不支持" in err, err


# ---------------------------------------------------------------------------
# 8. Python 侧配套改动
# ---------------------------------------------------------------------------

def test_serialize_encodes_big_int_as_string():
    """超出宿主安全整数范围的 int 按十进制字符串编码（旧产物仍可读）。"""
    import json
    from jishi.serialize import _const_from_json, _const_to_json
    small = _const_to_json(42)
    assert small == {"t": "int", "v": 42}
    big = _const_to_json(9223372036854775807)
    assert big["t"] == "int" and isinstance(big["v"], str)
    # 两种编码都要能读回来
    assert _const_from_json(small) == 42
    assert _const_from_json(big) == 9223372036854775807
    # 边界：正好等于 2^53-1 仍走数字形式
    assert isinstance(_const_to_json(9007199254740991)["v"], int)
    assert isinstance(_const_to_json(9007199254740992)["v"], str)
    assert json.dumps(big)   # 可序列化
