# -*- coding: utf-8 -*-
"""M34：模式匹配（`匹配`/`情形`）与枚举（`枚举`）—— 解析期脱糖。

这两个特性都走「**解析期脱糖优先**」这条路（本项目在 M23 的 `超()`、
M24 的集合字面量、M26 的 `用 … 为` 上验证过三次，这里是第四、第五次）：
解析器把新语法改写成既有语句的组合，**三个执行器一行都不用改**，
行为必然一致。

    enum 写法                          脱糖成
    -------------------------------   ------------------------------------
    匹配 x：                           __匹配_1__ = x
        情形 1：                      如果 __匹配_1__ == 1：
            …                             …
        情形 2, 3：                   否则如果 __匹配_1__ == 2 或 … == 3：
            …                             …
        情形 其他：                   否则：
            …                             …

    枚举 颜色：                       类 颜色：（初始化 / 文本 两个方法）
        红 = 1                        颜色.红 = 颜色("红", 1)
        绿 = 2                        颜色.全部 = [颜色.红, 颜色.绿]

脱糖方案能成立，靠的是几个**早就存在**的能力：
`如果/否则如果/否则`、`==`/`或`/`与`、类与实例、自定义打印（`文本()`）。
所以这一轮真正花时间的不是这两个特性本身，而是**宿主侧的被撞出来的缺口**：
枚举脱糖要用「类体外给类设属性」，而 Node / Rust 宿主此前压根不支持
（类变量在那边一直没同步）——见 `test_host_class_vars`。
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

from conftest import rust_exe_path       # noqa: E402

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
RUST_EXE = rust_exe_path()   # 平台感知：Windows 是 .exe，别处没有扩展名
CARGO = shutil.which("cargo")


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
    if not RUST_EXE.exists():
        r = subprocess.run(
            [CARGO, "build", "--manifest-path", str(ROOT / "rust/Cargo.toml"),
             "--release"],
            capture_output=True, text=True, encoding="utf-8",
            cwd=ROOT, env=_build_env())
        if r.returncode != 0:
            pytest.skip(f"cargo build 失败，跳过 Rust 对拍：{r.stderr[-300:]}")
    return RUST_EXE


# ---------------------------------------------------------------------------
# 对拍脚手架
# ---------------------------------------------------------------------------

def _tree_run(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        Interpreter(filename="<t>").run(
            parse(tokenize(src, "<t>"), src.split("\n"), "<t>"))
    return buf.getvalue()


def _py_run(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        vm_mod.VM(compile_source(src, "<t>"), "<t>").run()
    return buf.getvalue()


def _c_run(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        cvm_bind.run_source_c(src, "<t>")
    return buf.getvalue()


def _three(src: str) -> str:
    """三个执行器跑同一段代码，返回输出（不一致就失败）。"""
    outs = {"树遍历": _tree_run(src), "Python VM": _py_run(src), "C VM": _c_run(src)}
    vals = list(outs.values())
    assert all(v == vals[0] for v in vals), f"三执行器不一致：{outs!r}"
    return vals[0]


def _run_host(cmd: list[str]) -> tuple[int, str, str]:
    """跑宿主。**按字节收再解码**，不要 `text=True`（它会偷偷转换换行）。"""
    r = subprocess.run(cmd, capture_output=True, cwd=str(ROOT))
    return (r.returncode,
            r.stdout.decode("utf-8", errors="replace"),
            r.stderr.decode("utf-8", errors="replace"))


def _host_agree(src: str, tmp_path: Path, rust_exe: Path) -> dict:
    """Node / Rust 宿主跑同一段代码，返回 {宿主: (rc, 输出, 错误)}。"""
    # 字节码用 pytest 给的本轮专属临时目录：不会与并发跑的其它测试撞名
    bc = tmp_path / "m34_bc.json"
    bc.write_text(serialize.dumps(compile_source(src, "<t>")), encoding="utf-8")
    out = {}
    if NODE:
        out["Node"] = _run_host([NODE, str(ROOT / "node" / "index.js"), str(bc)])
    out["Rust"] = _run_host([str(rust_exe), str(bc)])
    return out


def _everywhere(src: str, tmp_path: Path, rust_exe: Path) -> str:
    """三执行器 + Node + Rust，五路逐字节一致；返回输出。"""
    expect = _three(src)
    for name, (rc, out, err) in _host_agree(src, tmp_path, rust_exe).items():
        assert rc == 0, f"{name} 跑失败：{err[:300]}"
        assert out == expect, f"{name} 输出不一致：{out!r} != {expect!r}"
    return expect


def _err_all(src: str) -> str:
    """三执行器都应当报错，返回错误文本（取 Python VM 的）。"""
    for label, fn in (("树遍历", _tree_run), ("Python VM", _py_run), ("C VM", _c_run)):
        with pytest.raises(JishiError) as ei:
            fn(src)
        if label == "Python VM":
            msg = str(ei.value)
    return msg


# ---------------------------------------------------------------------------
# 1. 模式匹配：基本
# ---------------------------------------------------------------------------

def test_match_value(tmp_path, rust_exe):
    assert _everywhere('''匹配 2：
    情形 1：
        打印("一")
    情形 2：
        打印("二")
    情形 其他：
        打印("其他")
''', tmp_path, rust_exe) == "二\n"


def test_match_multi_value(tmp_path, rust_exe):
    """`情形 1, 2：` 是「命中任一个即可」。"""
    assert _everywhere('''遍历 月 在 [1, 4, 7, 10]：
    匹配 月：
        情形 12, 1, 2：
            打印(月, "冬")
        情形 3, 4, 5：
            打印(月, "春")
        情形 6, 7, 8：
            打印(月, "夏")
        情形 其他：
            打印(月, "秋")
''', tmp_path, rust_exe) == "1 冬\n4 春\n7 夏\n10 秋\n"


def test_match_no_fallthrough_when_no_else(tmp_path, rust_exe):
    """没有「情形 其他」时，没命中就什么都不做（不是报错）。"""
    assert _everywhere('''函数 等级(分)：
    匹配 分：
        情形 100：
            返回 "满分"
        情形 60：
            返回 "及格"
    返回 "未知"
打印(等级(100), 等级(60), 等级(30))
''', tmp_path, rust_exe) == "满分 及格 未知\n"


def test_match_no_wildcard_matching_nothing(tmp_path, rust_exe):
    assert _everywhere('''令 说了 = []
遍历 值 在 [1, 2, 3]：
    匹配 值：
        情形 9：
            说了.追加("九")
打印(说了)
''', tmp_path, rust_exe) == "[]\n"


# ---------------------------------------------------------------------------
# 2. 模式匹配：守卫
# ---------------------------------------------------------------------------

def test_match_guard_chain(tmp_path, rust_exe):
    """`情形 如果 条件：` —— 只有条件的守卫链（最常见的用法）。"""
    assert _everywhere('''遍历 分数 在 [95, 75, 40]：
    匹配 分数：
        情形 如果 分数 >= 90：
            打印(分数, "优秀")
        情形 如果 分数 >= 60：
            打印(分数, "及格")
        情形 其他：
            打印(分数, "不及格")
''', tmp_path, rust_exe) == "95 优秀\n75 及格\n40 不及格\n"


def test_match_guard_on_value(tmp_path, rust_exe):
    """`情形 值 如果 条件：` —— 值先匹配，再看条件。"""
    assert _everywhere('''令 加分 = 真
匹配 90：
    情形 90 如果 加分：
        打印("优秀（含加分）")
    情形 90：
        打印("优秀")
''', tmp_path, rust_exe) == "优秀（含加分）\n"


def test_match_guard_falls_through_when_false(tmp_path, rust_exe):
    """守卫不成立时要**继续往下试**，不能直接跳到兜底。"""
    assert _everywhere('''令 加分 = 假
匹配 90：
    情形 90 如果 加分：
        打印("含加分")
    情形 90：
        打印("普通优秀")
    情形 其他：
        打印("其他")
''', tmp_path, rust_exe) == "普通优秀\n"


# ---------------------------------------------------------------------------
# 3. 模式匹配：求值、嵌套、控制流
# ---------------------------------------------------------------------------

def test_match_subject_evaluated_once(tmp_path, rust_exe):
    """待匹配值只求值一次（落到临时变量），分支多也不会重复跑。"""
    assert _everywhere('''令 次数 = [0]
函数 取():
    次数[0] += 1
    返回 7
匹配 取()：
    情形 1：
        打印("a")
    情形 2：
        打印("b")
    情形 7：
        打印("c")
    情形 其他：
        打印("d")
打印("求值次数:", 次数[0])
''', tmp_path, rust_exe) == "c\n求值次数: 1\n"


def test_match_nested(tmp_path, rust_exe):
    assert _everywhere('''遍历 对 在 [[1, "甲"], [1, "乙"], [2, "甲"]]：
    匹配 对[0]：
        情形 1：
            匹配 对[1]：
                情形 "甲"：
                    打印("一甲")
                情形 其他：
                    打印("一其他")
        情形 其他：
            打印("其他号")
''', tmp_path, rust_exe) == "一甲\n一其他\n其他号\n"


def test_match_pattern_can_be_expression(tmp_path, rust_exe):
    """模式是普通表达式（能取变量、能运算）。"""
    assert _everywhere('''令 基准 = 10
遍历 值 在 [10, 15, 20]：
    匹配 值：
        情形 基准：
            打印(值, "等于基准")
        情形 基准 + 5：
            打印(值, "等于基准+5")
        情形 其他：
            打印(值, "都不是")
''', tmp_path, rust_exe) == "10 等于基准\n15 等于基准+5\n20 都不是\n"


def test_match_control_flow_inside(tmp_path, rust_exe):
    """`中断`/`继续`/`返回` 在情形的块里照常工作（脱糖后就是普通 if 块）。"""
    assert _everywhere('''令 收集 = []
遍历 值 在 [1, 2, 3, 4, 5]：
    匹配 值：
        情形 3：
            继续
        情形 5：
            中断
        情形 其他：
            收集.追加(值)
打印(收集)
''', tmp_path, rust_exe) == "[1, 2, 4]\n"


def test_match_early_return(tmp_path, rust_exe):
    assert _everywhere('''函数 找(项, 目标)：
    遍历 i 在 范围(长度(项))：
        匹配 项[i]：
            情形 目标：
                返回 i
    返回 -1
打印(找(["甲", "乙", "丙"], "乙"), 找(["甲"], "丁"))
''', tmp_path, rust_exe) == "1 -1\n"


def test_match_value_is_container(tmp_path, rust_exe):
    """模式是容器时按**值**比较（M30 起容器 `==` 是按值递归的）。"""
    assert _everywhere('''遍历 点 在 [[0, 0], [1, 2], [3, 4]]：
    匹配 点：
        情形 [0, 0]：
            打印("原点")
        情形 [1, 2]：
            打印("一二")
        情形 其他：
            打印("其他")
''', tmp_path, rust_exe) == "原点\n一二\n其他\n"


def test_match_only_wildcard(tmp_path, rust_exe):
    assert _everywhere('''匹配 5：
    情形 其他：
        打印("总会执行")
''', tmp_path, rust_exe) == "总会执行\n"


# ---------------------------------------------------------------------------
# 4. 枚举
# ---------------------------------------------------------------------------

def test_enum_basic(tmp_path, rust_exe):
    assert _everywhere('''枚举 颜色：
    红 = 1
    绿 = 2
    蓝 = 3
打印(颜色.红)
打印(颜色.红.名字, 颜色.绿.值)
''', tmp_path, rust_exe) == "颜色.红\n红 2\n"


def test_enum_display_in_container(tmp_path, rust_exe):
    """成员在容器里也走自定义打印，不是 `<颜色 实例>`。"""
    assert _everywhere('''枚举 颜色：
    红 = 1
    绿 = 2
打印([颜色.红, 颜色.绿])
打印({"第一个": 颜色.红})
''', tmp_path, rust_exe) == "[颜色.红, 颜色.绿]\n{'第一个': 颜色.红}\n"


def test_enum_member_identity(tmp_path, rust_exe):
    """`==` 按身份：同一次创建的成员相等，跟值/其它类型都不等（与 Python Enum 一致）。"""
    assert _everywhere('''枚举 颜色：
    红 = 1
    绿 = 2
打印(颜色.红 == 颜色.红, 颜色.红 == 颜色.绿, 颜色.红 == 1)
''', tmp_path, rust_exe) == "真 假 假\n"


def test_enum_iterate_all(tmp_path, rust_exe):
    """`颜色.全部` 是给遍历用的清单（类对象本身不可遍历）。"""
    assert _everywhere('''枚举 状态：
    待办 = "todo"
    完成 = "done"
遍历 s 在 状态.全部：
    打印(s.名字, s.值)
''', tmp_path, rust_exe) == "待办 todo\n完成 done\n"


def test_enum_auto_numbering(tmp_path, rust_exe):
    """不写值的成员自动接着编号（第一个从 1 起）。"""
    assert _everywhere('''枚举 方向：
    上
    下
    左
    右
打印(方向.上.值, 方向.下.值, 方向.右.值)
''', tmp_path, rust_exe) == "1 2 4\n"


def test_enum_mixed_numbering(tmp_path, rust_exe):
    """写一半、省一半：省的那个接着上一个整数往上数。"""
    assert _everywhere('''枚举 码：
    A = 10
    B
    C
    D = 100
    E
打印(码.A.值, 码.B.值, 码.C.值, 码.D.值, 码.E.值)
''', tmp_path, rust_exe) == "10 11 12 100 101\n"


def test_enum_string_values(tmp_path, rust_exe):
    assert _everywhere('''枚举 状态：
    待办 = "todo"
    完成 = "done"
打印(状态.待办, 状态.待办.值, 文本(状态.完成.值))
''', tmp_path, rust_exe) == "状态.待办 todo done\n"


def test_enum_type_names(tmp_path, rust_exe):
    """成员是「颜色 实例」，类本身是「类 颜色」（与 Python 侧一致）。"""
    assert _everywhere('''枚举 颜色：
    红 = 1
打印(类型(颜色.红), 类型(颜色))
''', tmp_path, rust_exe) == "颜色 实例 类 颜色\n"


def test_enum_body_only_takes_members():
    """枚举体里只认成员（`名字 = 值` 或 `名字`）。

    想在枚举上挂方法，就直接用「类」——枚举是「一组有名字的值」，
    不是「简化的类」（Python 的 Enum 允许挂方法，那是它的复杂度，
    这里刻意不要）。
    """
    msg = _err_all('''枚举 颜色：
    红 = 1
    函数 描述(自身)：
        返回 "x"
''')
    assert "成员" in msg or "意外" in msg


def test_enum_used_in_match(tmp_path, rust_exe):
    """枚举 + 模式匹配合体（两个特性互相配合的典型场景）。"""
    assert _everywhere('''枚举 状态：
    待办 = "todo"
    完成 = "done"
函数 说(s)：
    匹配 s：
        情形 状态.待办：
            返回 "还没做"
        情形 状态.完成：
            返回 "做完了"
        情形 其他：
            返回 "?"
打印(说(状态.待办), 说(状态.完成))
''', tmp_path, rust_exe) == "还没做 做完了\n"


# ---------------------------------------------------------------------------
# 5. 宿主侧被撞出来的缺口：类变量（枚举脱糖依赖它）
# ---------------------------------------------------------------------------

def test_host_class_vars(tmp_path, rust_exe):
    """类变量（M23.3）在 Node / Rust 宿主上也要能用。

    这条是 M34 的**副产品**：枚举脱糖要用「类体外给类设属性」，
    而两个宿主此前都没有 `setAttr` 的类分支——一写枚举就报
    「不能给「<类 颜色>」的属性「红」赋值」。顺带把实例读类变量、
    继承合并类变量也一起补齐（与 Python 侧语义一致）。
    """
    src = '''类 甲：
    计数 = 0
甲.计数 = 5
打印(甲.计数)
令 a = 新建 甲()
打印(a.计数)
a.计数 = 9
打印(甲.计数, a.计数)
'''
    # 实例字段优先于类变量：`a.计数 = 9` 只改实例自己那份
    assert _everywhere(src, tmp_path, rust_exe) == "5\n5\n5 9\n"


def test_host_class_vars_inherited(tmp_path, rust_exe):
    """子类继承基类的类变量。"""
    assert _everywhere('''类 甲：
    数 = 1
类 乙 继承 甲：
    名 = "乙"
打印(乙.数, 乙.名)
令 b = 新建 乙()
打印(b.数, b.名)
''', tmp_path, rust_exe) == "1 乙\n1 乙\n"


def test_host_class_vars_declared_in_body(tmp_path, rust_exe):
    """类体里直接声明的类变量（编译器发射 SET_ATTR，宿主补上分支即可）。"""
    assert _everywhere('''类 甲：
    计数 = 0
    函数 加(自身)：
        甲.计数 += 1
        返回 甲.计数
打印(甲.计数)
令 a = 新建 甲()
a.加()
a.加()
打印(甲.计数)
''', tmp_path, rust_exe) == "0\n2\n"


# ---------------------------------------------------------------------------
# 6. 错误路径
# ---------------------------------------------------------------------------

def test_error_wildcard_not_last():
    msg = _err_all('''匹配 1：
    情形 其他：
        打印("a")
    情形 2：
        打印("b")
''')
    assert "情形 其他" in msg and "最后" in msg


def test_error_case_only_inside_match():
    msg = _err_all('''情形 1：
    打印("a")
''')
    assert "情形" in msg and "匹配" in msg


def test_error_match_block_rejects_other_statements():
    msg = _err_all('''匹配 1：
    打印("忘了写情形")
''')
    assert "只能写「情形」" in msg


def test_error_match_without_case():
    msg = _err_all('''匹配 1：
    :
''')
    assert "至少要写一个「情形」" in msg or "情形" in msg


def test_error_enum_member_needs_value():
    """上一个成员的值不是整数时，后面就不能省值（没法自动编号）。"""
    msg = _err_all('''枚举 状态：
    待办 = "todo"
    完成
''')
    assert "要写值" in msg


def test_error_enum_empty():
    msg = _err_all('''枚举 颜色：
    :
''')
    assert "至少要写一个成员" in msg or "成员" in msg


def test_error_match_missing_colon():
    msg = _err_all('''匹配 1
    情形 1：
        打印("a")
''')
    assert "冒号" in msg


# ---------------------------------------------------------------------------
# 7. 脱糖不改既有语义（回归护栏）
# ---------------------------------------------------------------------------

def test_desugar_scoping_is_plain(tmp_path, rust_exe):
    """脱糖后就是个普通 if —— 作用域行为也应当是「普通的」：

    情形的块里赋值照常生效（与 if 块一致），外层已有变量不受影响。
    """
    assert _everywhere('''令 甲 = 1
匹配 甲：
    情形 1：
        令 乙 = 2
        甲 = 99
        打印(乙)
打印(甲)
''', tmp_path, rust_exe) == "2\n99\n"


def test_existing_if_unchanged():
    """脱糖产出的就是普通 If —— 既有语法行为不受影响。"""
    assert _three('''令 分 = 75
如果 分 >= 90：
    打印("优秀")
否则如果 分 >= 60：
    打印("及格")
否则：
    打印("不及格")
''') == "及格\n"


def test_probe_reports_two_more():
    """能力探针要能看到这两项（防自嗨：探针带期望输出校验）。"""
    import subprocess as sp
    r = sp.run([sys.executable, str(ROOT / "tools" / "probe_capabilities.py")],
               capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
    text = r.stdout
    assert "模式匹配" in text and "✅ 模式匹配" in text, text
    assert "✅ 枚举" in text, text
