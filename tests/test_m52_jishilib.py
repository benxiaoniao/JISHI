# -*- coding: utf-8 -*-
"""M52 · 基石库层：**用基石自己写的标准库**，一份代码五个引擎共用。

## 这个文件钉什么

1. **一致性**：同一段程序在树遍历 / Python VM / C VM / Node / Rust 上逐字节一致，
   而且与**宿主层实现**（`jishi/stdlib/*.py`）也逐字节一致 ——
   「换一种实现方式」不能改变行为，这是迁移的底线。
2. **机制**：`导入 统计` 拿到的仍是**模块对象**（`统计.求和` 照旧能用、
   `打印(统计)` 还是 `<模块 统计>`）、模块私有名字不外泄、别名导入可用、
   没用到基石库时产物**一点没变**。
3. **回落**：`JISHI_NO_JISHILIB=1` 能把整层关掉、导入回落到宿主层
   （诊断与性能对比都靠它；也证明宿主层还完整可用）。

## 机制一句话

`jishi/stdlib-jishi/统计.jsh` 在**编译期**被展开成

    函数 __基石库_统计__(): <模块源码>  返回 {"求和": 求和, …}     # 外壳函数
    统计 = $造模块("统计", __基石库_统计__())                      # 原 `导入 统计`

于是「库」在字节码层面**只是一次函数调用** —— 三个执行器与两个跨语言宿主
**一个字都没改**。完整设计见 `jishi/jishilib.py` 的模块文档。
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import rust_exe_path, tmp_bc_path      # noqa: E402

from jishi import jishilib                           # noqa: E402
from jishi import serialize                          # noqa: E402
from jishi import vm as vm_mod                       # noqa: E402
from jishi.compiler import compile_source            # noqa: E402
from jishi.errors import JishiError                  # noqa: E402
from jishi.interpreter import run_source as tree_run  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
RUST = rust_exe_path()


# ---------------------------------------------------------------------------
# 脚手架
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def lib_off():
    """临时把基石库层整体关掉（导入回落到宿主层）。"""
    old = os.environ.get("JISHI_NO_JISHILIB")
    os.environ["JISHI_NO_JISHILIB"] = "1"
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("JISHI_NO_JISHILIB", None)
        else:
            os.environ["JISHI_NO_JISHILIB"] = old


def _capture(fn, *a) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*a)
    return buf.getvalue()


def _py_vm(src: str) -> str:
    return _capture(lambda: vm_mod.VM(compile_source(src, "<t>"), "<t>").run())


def _cvm(src: str) -> str:
    from jishi import cvm_bind
    return _capture(cvm_bind.run_source_c, src, "<t>")


def _host_run(exe: list, src: str, tag: str) -> tuple:
    bc = tmp_bc_path(ROOT / f"_tmp_m52_{tag}.json")
    bc.write_text(serialize.dumps(compile_source(src, "<t>")), encoding="utf-8")
    r = subprocess.run(exe + [str(bc)], capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def _engines(tag: str = "a"):
    """五引擎：`(名字, 跑一段源码 → (是否正常退出, 输出或错误文本))`。"""
    def tree(src):
        try:
            return True, _capture(tree_run, src)
        except JishiError as e:
            return False, f"{getattr(e, 'title', '')} {e}"

    def pyvm(src):
        try:
            return True, _py_vm(src)
        except JishiError as e:
            return False, f"{getattr(e, 'title', '')} {e}"

    def cvm(src):
        try:
            return True, _cvm(src)
        except JishiError as e:
            return False, f"{getattr(e, 'title', '')} {e}"

    out = [("树遍历", tree), ("Python VM", pyvm), ("C VM", cvm)]
    if NODE:
        out.append(("Node", lambda s: _host_run(
            [NODE, str(ROOT / "node" / "index.js")], s, tag)))
    if RUST.exists():
        out.append(("Rust", lambda s: _host_run([str(RUST)], s, tag)))
    return out


def _five_agree(src: str) -> str:
    """五个引擎 stdout 逐字节一致且都正常退出；返回那份输出。"""
    base_label, base = None, None
    for label, run in _engines():
        ok, out = run(src)
        assert ok, f"{label} 本该正常退出却报错：{out[:300]}"
        if base is None:
            base_label, base = label, out
        else:
            assert out == base, (
                f"{label} 与 {base_label} 输出不一致\n"
                f"{base_label}: {base!r}\n{label}: {out!r}")
    return base


#: 每个基石库模块一段「把公开函数都摸一遍」的程序 —— 迁移的验收基准。
#: ⚠️ 加模块时**必须**在这里补一条：只改 `jishilib.names()` 的清单等于没验收。
_模块用例 = {
    "统计": """\
导入 统计
打印(统计)
打印(统计.求和([1,2,3]), 统计.平均([1,2,3,4]), 统计.中位数([3,1,2]))
打印(统计.中位数([1,2,3,4]), 统计.众数([3,1,3,2]), 统计.极差([1,5,3]))
打印(统计.方差([1,2,3,4]), 统计.标准差([1,2,3,4]))
打印(统计.方差([1,2,3,4], 真), 统计.标准差([1,2,3,4], 真))
打印(统计.分位数([1,2,3,4], 0.25), 统计.去极值平均([9.5,8.0,9.0,7.5,9.9]))
打印(统计.加权平均([80,90],[1,3]))
打印(统计.求和([]), 统计.众数([9,9,1,1]))
""",
    "容器": """\
导入 容器
打印(容器)
打印(容器.计数(["甲","乙","甲"]))
打印(容器.最多(["甲","乙","甲"]), 容器.最多(["甲","乙","甲"], 2))
打印(容器.按值排序({"甲": 3, "乙": 1, "丙": 3}))
打印(容器.按值排序({"甲": 3, "乙": 1, "丙": 3}, 假))
打印(容器.分组([1,2,3,4], 函数(x)：x % 2))
打印(容器.取前([1,2,3], 2), 容器.取后([1,2,3], 2))
打印(容器.分块(范围(7), 3))
打印(容器.最多([9,9,1,1]), 容器.最多([], 3))
""",
    "迭代": """\
导入 迭代
打印(迭代)
打印(迭代.分组(范围(7), 3))
打印(迭代.滑窗([1,2,3,4], 2), 迭代.滑窗([1,2,3,4], 3, 2))
打印(迭代.去重([1,2,1,3,2]))
打印(迭代.展开([[1,2],[3],4]), 迭代.展开([[1,2],[]]))
打印(迭代.取前([1,2,3], 2), 迭代.取后([1,2,3], 2))
打印(迭代.求和([1,2,3]), 迭代.计数([1,2,3,4], 函数(x)：x > 2))
打印(迭代.累积([1,2,3]), 迭代.累积([1,2], 10))
打印(迭代.组合([1,2,3], 2))
打印(迭代.排列([1,2,3], 2), 迭代.排列([1,2]))
打印(迭代.笛卡尔积([1,2], "甲乙"))
""",
    "参数": """\
导入 参数
打印(参数)
令 解析器 = 参数.新建("待办清单", "一个简单的待办工具")
参数.选项(解析器, "数据", "待办.json", "数据文件路径")
参数.标志(解析器, "详细", "打印更多信息")
打印(参数.用法(解析器))
打印(参数.解析(解析器, ["添加", "--数据", "x.json", "--详细"]))
打印(参数.解析(解析器, ["--数据=y.json", "列表"]))
打印(参数.解析(解析器, ["--", "--不是选项"]))
打印(参数.解析(["甲", "--详细"], {"数据": "默认.json", "详细": 假}))
""",
}


# ---------------------------------------------------------------------------
# 一、一致性
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mod", sorted(_模块用例))
def test_模块在五引擎逐字节一致(mod):
    """四个模块的用例在树遍历 / Python VM / C VM / Node / Rust 上完全一致。

    ✅ **M54 起 Rust 没有豁免了**（2026-09-27）：M53 记下的三条 Rust 缺口
    （`容器` / `迭代` / `参数`）根因是**同一个** —— Rust 的闭包单元（cell）
    是值拷贝，不是共享的，于是「先创建闭包、后给它赋值」永远失效。
    嵌套递归函数、库函数收主程序传来的函数回调，全栽在这上面。
    修法见 `rust/src/lib.rs` 的 `JCell` 与 `docs/设计决策.md` D46。

    `RUST_GAPS` 这个机制**留着**：它是「显式的账」——下次再冒出宿主缺口，
    就往里加一条并写明原因，**不许静默跳过**；修好了必须回来删。
    """
    src = _模块用例[mod]
    base_label, base = None, None
    for label, run in _engines():
        ok, out = run(src)
        if not ok:
            assert label == "Rust" and mod in RUST_GAPS, (
                f"{label} 意外失败（{mod}）：{out[:250]}")
            continue
        if base is None:
            base_label, base = label, out
        else:
            assert out == base, (
                f"{label} 与 {base_label} 输出不一致\n"
                f"{base_label}: {base!r}\n{label}: {out!r}")
    assert base_label is not None
    assert base.splitlines()[0] == f"<模块 {mod}>"


#: Rust 宿主的**已知缺口** —— **M54 已清空**（2026-09-27）。
#:
#: 曾经的三条（`容器` / `迭代` / `参数`）根因是同一个：Rust 的闭包单元不共享。
#: 留着这个集合是**制度**，不是历史包袱：
#: 缺口在 → 跳过对应引擎并**写明原因**；修好了 → 必须回来删掉这一行。
RUST_GAPS: set[str] = set()


@pytest.mark.parametrize("mod", sorted(_模块用例))
def test_与宿主层实现逐字节一致(mod):
    """**迁移不能改变行为** —— 这是本文件最重要的一条。

    把 `统计` / `容器` / `迭代` / `参数` 的实现从「宿主各写一遍」换成「一份基石
    代码」，用户看到的结果必须**一模一样**（包括 `打印(模块)` 的输出形态）。
    """
    src = _模块用例[mod]
    lib = _capture(tree_run, src)                   # 默认 → 基石库层
    with lib_off():
        host = _capture(tree_run, src)              # → 宿主层 `jishi/stdlib/*.py`
    assert lib == host, f"{mod}: 结果不同\n库: {lib!r}\n宿主: {host!r}"


def _err_kind(src: str) -> str:
    """跑一段必须报错的源码，返回错误**大类**（尽量归一，方便跨引擎比）。"""
    try:
        _capture(tree_run, src)
        return "没报错"
    except JishiError as e:
        t = f"{getattr(e, 'title', '')} {e}"
    if "类型错误" in t:
        return "类型错误"
    if "值错误" in t or "数值不对" in t:
        return "值错误"
    return t[:40]


@pytest.mark.parametrize("expr", [
    '统计.求和("abc")', '统计.求和([1,"a"])', '统计.求和([1,真])',
    '统计.平均([])', '统计.方差([1], 真)', '统计.分位数([1,2], 2)',
    '统计.分位数([1,2], "x")', '统计.去极值平均([1,2], -1)',
    '统计.去极值平均([1,2], 1)', '统计.加权平均([1,2],[1])',
    '统计.加权平均([1],[0])', '统计.求和(5)',
])
def test_错误路径五引擎都给同一类错(expr):
    """错误**类型**五引擎必须一致（对拍比类型，文案允许略有出入）。"""
    src = f'导入 统计\n{expr}\n'
    kinds = set()
    for label, run in _engines("e"):
        ok, text = run(src)
        assert not ok, f"{label} 本该报错却正常退出：{text[:200]!r}"
        if "类型错误" in text:
            kinds.add("类型错误")
        elif "值错误" in text or "数值不对" in text:
            kinds.add("值错误")
        else:
            pytest.fail(f"{label} 报的不是类型/值错误：{text[:200]!r}")
    assert kinds in ({"类型错误"}, {"值错误"}), f"五引擎报的类型不一致：{kinds}"


def test_错误路径与宿主层同类型():
    """同一批错误，库版与宿主层版报的类要一一对上（换实现不能换报错）。"""
    cases = ['统计.求和("abc")', '统计.求和([1,"a"])', '统计.平均([])',
             '统计.方差([1], 真)', '统计.分位数([1,2], 2)',
             '统计.加权平均([1],[0])']
    old = os.environ.pop("JISHI_NO_JISHILIB", None)
    try:
        for expr in cases:
            src = f'导入 统计\n{expr}\n'
            lib = _err_kind(src)
            os.environ["JISHI_NO_JISHILIB"] = "1"
            host = _err_kind(src)
            os.environ.pop("JISHI_NO_JISHILIB", None)
            assert lib == host, f"{expr}: 库版报 {lib}，宿主层报 {host}"
    finally:
        if old is not None:
            os.environ["JISHI_NO_JISHILIB"] = old


# ---------------------------------------------------------------------------
# 二、机制
# ---------------------------------------------------------------------------

def test_导入拿到的是模块对象而不是字典():
    """`$造模块` 那一层的作用：语法一个字都不用改。"""
    src = '导入 统计\n打印(统计)\n打印(统计.求和([1,2,3]))\n'
    out = _five_agree(src)
    assert out == "<模块 统计>\n6\n"


def test_模块私有名字不外泄():
    """`_取数字表` 这些下划线开头的名字是模块私有的，不该出现在用户面前。"""
    src = '导入 统计\n打印(统计._取数字表)\n'
    for label, run in _engines("p"):
        ok, text = run(src)
        assert not ok, f"{label} 居然能拿到私有名字：{text[:200]!r}"


def test_别名导入可用():
    out = _five_agree('导入 统计 为 数\n打印(数.求和([1,2,3]), 数.中位数([1,2,3]))\n')
    assert out == "6 2\n"


def test_函数体内导入也能展开():
    """`导入` 写在函数里（或条件分支里）同样要展开，且**位置语义不变**。"""
    src = ('函数 算()：\n'
           '    导入 统计\n'
           '    返回 统计.求和([1,2,3])\n'
           '打印(算())\n')
    assert _five_agree(src) == "6\n"


def test_内部内建不进语言规格():
    """`$造模块` 是编译器专用的内部内建，不该出现在 AI 语言卡/补全里。"""
    from jishi import ai
    names = [b["name"] for b in ai.build_lang_spec()["builtins"]]
    assert "$造模块" not in names


def test_没用到基石库时产物一点没变():
    """只有真的 `导入` 了基石库模块才会改写 —— 其他程序产物与从前逐字节相同。"""
    plain = compile_source('打印(1 + 2)\n', "<t>")
    assert len(plain.codes) == 1
    assert plain.names == ["打印"] or "打印" in plain.names


def test_基石库模块清单():
    """M52 搬了 `统计`；M53 又搬了 `容器` / `迭代` / `参数`。清单是**显式**的 ——
    加模块要改这里（改完顺手把 `_模块用例` 里也补一条，否则等于没验收）。"""
    assert jishilib.names() == ["参数", "容器", "统计", "迭代"]
    assert jishilib.has("统计") and jishilib.has("迭代")
    # 这两个**故意**没搬：依赖宿主原语（见 docs/设计决策.md D45 的判据）
    assert not jishilib.has("编码")      # 要 UTF-8 编解码，而基石没有 序数/字符
    assert not jishilib.has("配置")      # 要文件与 json


def test_关掉这层就回落宿主层():
    """`JISHI_NO_JISHILIB=1`：宿主层必须**完整可用**（它是兜底，也是对照）。"""
    src = '导入 统计\n打印(统计.求和([1,2,3]), 统计.中位数([3,1,2]))\n'
    lib = _five_agree(src)                          # 五引擎走基石库层
    with lib_off():
        off_out = _capture(tree_run, src)           # 关掉 → 宿主层
    assert lib == off_out == "6 2\n"


# ---------------------------------------------------------------------------
# 三、顺带抓到的一个真 bug：循环体里「捕获后继续循环」会把栈搞脏
# ---------------------------------------------------------------------------

def test_循环体里捕获之后继续循环():
    """⚠️ M53 顺手抓到的**真 bug** 的回归测试（2026-09-26）。

    症状：下面这段在 Python VM / C VM / Node / Rust 上崩
    `TypeError: 'RunTypeError' object is not an iterator`，而树遍历是对的。

    根因（在**编译器**，不在执行器）：`尝试/捕获` 生成的字节码里多了一句
    `DUP_TOP`，而 `MATCH_EXC` 是「弹出 cond、**读**栈顶的 err（不弹）、压结果」——
    于是复制出来的那份 err **再没人弹**，永久留在栈上。平时看不出来，但只要
    「循环体里 `尝试/捕获`、捕获完**继续循环**」，下一次 `FOR_ITER` 的
    `stack[-1]` 就是那个异常对象 → 崩。

    四个引擎共用这份字节码，所以一起崩；**只有树遍历（走 Python 的 try/except）
    是对的 —— 五引擎对拍才抓得住**。修法见 `jishi/compiler.py::_c_try`。
    """
    src = ('遍历 段 在 [[1,2], 4, [3]]:\n'
           '    尝试：\n'
           '        遍历 x 在 段：\n'
           '            打印("元素", x)\n'
           '    捕获 类型错误 为 _e：\n'
           '        打印("不是序列", 段)\n'
           '打印("结束")\n')
    assert _five_agree(src) == "元素 1\n元素 2\n不是序列 4\n元素 3\n结束\n"


def test_捕获后继续循环还能再捕获():
    """同一根因的另一面：捕获完继续循环，**第二次仍然能正常捕获**。"""
    src = ('遍历 段 在 [1, 2, 3]:\n'
           '    尝试：\n'
           '        遍历 x 在 段：\n'
           '            打印("不该到这")\n'
           '    捕获 类型错误 为 _e：\n'
           '        打印("跳过", 段)\n')
    assert _five_agree(src) == "跳过 1\n跳过 2\n跳过 3\n"


# ---------------------------------------------------------------------------
# 四、M54 抓到的三个真问题（都在「五个引擎共用同一份字节码」那条线上）
# ---------------------------------------------------------------------------

def test_嵌套递归函数():
    """⚠️ **M54 修的真 bug**：Rust 上「嵌套的递归函数」直接崩 `「空」不能调用`
    （**顶层递归没事**，所以极难发现 —— 单测怎么写都碰不到）。

    根因在 **Rust 宿主的闭包单元**：`Cell` 是值语义的 `struct`，
    `LOAD_CELL` 克隆一份、`MAKE_FUNCTION` 又把**值**拷进 `Func`、
    `STORE_DEREF` 再**整体替换**帧里的那个 —— 闭包拿着的是**独立快照**。
    而嵌套递归偏偏编译成「先 `LOAD_CELL` 捕获自己的单元、再 `STORE_DEREF`
    填入函数体」，于是内层函数永远看不见自己。

    Python / Node 的 cell 是**对象**：`LOAD_CELL` 压的是它自己、
    `STORE_DEREF` 改的是**它里面的值**，所以那边一直是对的。
    修法：Rust 改用 `Rc<RefCell<Cell>>`（见 `rust/src/lib.rs` 的 `JCell`）。

    这条**只有靠跑 Rust 宿主**才抓得到 —— 三个执行器都是对的。
    """
    src = ('函数 外(n)：\n'
           '    令 出 = []\n'
           '    函数 走(k)：\n'
           '        如果 k >= n：\n'
           '            出.追加(k)\n'
           '            返回\n'
           '        走(k + 1)\n'
           '    走(0)\n'
           '    返回 出\n'
           '打印(外(3))\n')
    assert _five_agree(src) == "[3]\n"


def test_闭包内外共享同一个单元():
    """同一根因的另一面：**闭包创建之后再赋值，闭包必须看得见**。

    ⚠️ 为什么用「先建闭包、后赋值」而不是「闭包里给外层变量赋值」：
    基石**没有 `非局部` 声明**，而按 Python 的语义 `计数 = 计数 + 1` 会让
    `计数` 变成内层函数的**局部变量**（各引擎都正确地报 E0301）。
    所以「共享」必须靠这条来验 —— 它正好就是编译器「先 `LOAD_CELL` 捕获、
    再 `STORE_DEREF` 填入」的那条路，值语义的 cell 会在这里给出**旧快照**。
    """
    src = ('函数 外()：\n'
           '    令 值 = 0\n'
           '    函数 取()：\n'
           '        返回 值\n'
           '    值 = 7\n'                 # 闭包创建之后才赋值
           '    返回 取()\n'
           '打印(外())\n')
    assert _five_agree(src) == "7\n"


def test_闭包改的容器外面看得见():
    """共享的另一半：闭包**改的是同一个容器对象**（不是各改各的副本）。

    这是基石库层最常用的写法（`参数.jsh` 的 `_解析器表`、`迭代.jsh` 的 `出`），
    所以它必须在五个引擎上稳稳成立。
    """
    src = ('函数 外()：\n'
           '    令 收集 = []\n'
           '    函数 塞入(x)：\n'
           '        收集.追加(x)\n'
           '    塞入(1)\n'
           '    塞入(2)\n'
           '    返回 收集\n'
           '打印(外())\n')
    assert _five_agree(src) == "[1, 2]\n"


def test_未绑定局部与单元的报错一致():
    """「没赋过值就读」在**三个执行器**上必须是同一类异常。

    M54 之前有两处漏的：

    - C VM 的 `LOAD_DEREF` 未命中单元时报 `RunError`（E2000 运行期错误），
      而 `LOAD_FAST` 未命中局部报 `SemanticNameError`（E0301 找不到这个名字）——
      **同一个执行器两套写法**，对拍测试恰好没覆盖「单元未绑定」。
    - Rust 两条路径都**静默返回 `空`**。`空` 是合法值，跟「没赋过值」
      必须分得开 —— 静默给个错答案，比报错难查一百倍。

    ⚠️ **跨语言宿主的边界（如实记）**：Node / Rust 没有 Python 侧那套错误码
    目录，它们报「错误（异常）：找不到名字「y」」；Rust 还**不带行列位置**
    （它的 `Instr` 根本没解析 line/col）。所以这里只断言：
    三个执行器同为 E0301、两个宿主**首行**彼此相同。
    """
    cases = {
        "局部": ('函数 外()：\n'
                 '    如果 假：\n'
                 '        令 y = 1\n'
                 '    返回 y\n'
                 '打印(外())\n'),
        "闭包单元": ('函数 外()：\n'
                     '    如果 假：\n'
                     '        令 x = 1\n'
                     '    函数 内()：\n'
                     '        返回 x\n'
                     '    返回 内()\n'
                     '打印(外())\n'),
    }
    for kind, src in cases.items():
        outs = {}
        for label, run in _engines():
            ok, out = run(src)
            assert not ok, f"{label} 本该报「找不到名字」却跑通了（{kind}）"
            outs[label] = out
        for label in ("树遍历", "Python VM", "C VM"):
            assert "E0301" in outs[label] and "找不到这个名字" in outs[label], (
                f"{kind}：{label} 报的不是 E0301「找不到这个名字」—— "
                f"{outs[label][:160]!r}")
        first = {label: outs[label].strip().splitlines()[0]
                 for label in ("Node", "Rust") if label in outs}
        if len(first) == 2:
            assert first["Node"] == first["Rust"], (
                f"{kind}：两宿主报错首行不同\n"
                f"Node: {first['Node']}\nRust: {first['Rust']}")


def test_虚拟机销毁后回收函数值不崩():
    """⚠️ **M54 修的另一个真 bug**：C VM 的「用后释放」崩溃。

    `run_source_c` 在 `finally` 里销毁 VM（`host.vm = None`），可这次运行
    **返回出去的基石函数值**（`CvmFunction`）还活着。等 Python 的 GC 顺手
    回收它，`__del__` 就拿着 `None` 去回调 C 库 —— ctypes 把 `None` 当 NULL，
    C 侧解引用空指针 → `Windows fatal exception: access violation`。

    最坑的是**它崩在 GC 里**：报错位置跟出错的那行代码八竿子打不着，
    而且 `try/except` 捕不住段错误。M53 的全量回归里就有一堆这种噪音
    （`git stash -u` 回退到 M52/M53 状态复跑同样崩 → 既有问题，不是 M54 引入的）。

    放**子进程**里跑：真回归了挂的是这个子进程，不会把整个测试进程带走。
    """
    snippet = (
        "import sys, gc\n"
        "sys.path.insert(0, r'%s')\n"
        "from jishi import cvm_bind\n"
        "val = cvm_bind.run_source_c('函数 f():\\n    返回 1\\nf\\n', '<t>')\n"
        "del val\n"
        "gc.collect()\n"
        "print('OK')\n" % str(ROOT)
    )
    r = subprocess.run([sys.executable, "-c", snippet],
                       capture_output=True, text=True, encoding="utf-8",
                       cwd=str(ROOT), timeout=120)
    assert r.returncode == 0 and "OK" in (r.stdout or ""), (
        "C VM 销毁 VM 之后再回收函数值就崩了（用后释放回归）\n"
        f"退出码 {r.returncode}\nstdout: {(r.stdout or '')[:300]}\n"
        f"stderr: {(r.stderr or '')[:400]}")


