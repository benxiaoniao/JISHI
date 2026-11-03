# -*- coding: utf-8 -*-
"""R6.4 · **Node 宿主**的报错对齐（R6.2/R6.3 在 Rust 上做过的那件事，补 Node）。

## 为什么单独一轮

`tools/check_engines.py` 与 `tools/probe_runtime_errors.py` 以前**都不带 Node** ——
于是这个宿主的报错**从头到尾没有进过判据**：

* 报错渲染只有「一行标题 + 一行位置」（用户看不到自己写的那一行）；
* 类型名笼统成「异常」「运行期错误」（`e.类型`、`捕获 X`、错误码**三处一起错**）；
* 几条**静默成功**——最重的两条是 `列[9] = 3`（JS 数组自动补长，**不报错还改了列表**）
  和 `1 < "a"`（JS 给 `false`，Python 报「不能比较」）。

把 Node 拉进两个工具的当天，探针就从「19 个形状 0 不一致」变成 **11 个不一致**
（那是四个执行器时代的 0）。本文件钉住修完之后的现状。

## 三件事

1. **渲染逐字一致**：Node 的 `stderr` 与 Python CLI 的 `stderr` 逐字节相同
   （标题 + `┌─ 文件:行:列` + 源码行 + `^` + 调用链 + 提示）；
2. **静默成功不许回来**：越界赋值 / 越界弹出 / 类型不可比 —— 这三条以前
   要么悄悄改数据、要么给个错的答案；
3. **类型名与错误码对齐 Python 侧的类**（E0301 / E0302 / E2006 / E2010）。
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from oracle.jishi.compiler import compile_to_json                         # noqa: E402
from probe_runtime_errors import SHAPES, build_source, engine_outputs  # noqa: E402

NODE = shutil.which("node")
HAS_NODE = NODE is not None
need_node = pytest.mark.skipif(not HAS_NODE, reason="本机无 Node.js，跳过宿主对拍")


def _write(src: str, tag: str) -> tuple[str, Path]:
    """把源码写到 `build/` 下，返回 `(相对仓库根路径, 绝对路径)`。

    ⚠️ **必须是相对仓库根的路径**：Node 是按字节码里的 `filename` 原样去读
    源文件的（画源码行与 `^` 用），测试的 cwd 也是仓库根 —— 两边得看到同一个文件。
    """
    name = f"_m66_{tag}_{os.getpid()}.jsh"
    path = ROOT / "build" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(src, encoding="utf-8")
    return f"build/{name}", path


def _both(src: str, tag: str):
    """**同一个 `.jsh` 文件**分别交给 Python CLI 与 Node 宿主 → `((py_out, py_err), (nd_out, nd_err))`。

    ⚠️ 必须是同一个文件：两边都是按字节码里的 `filename` 原样去读源码的，
    文件名或路径形式只要差一点，方框里的 `┌─ …` 那一行就不一样了。
    两边也都用**真 CLI / 真宿主**（不是库调用）—— 源码行是出口层补的。
    """
    rel, _ = _write(src, tag)
    bc = ROOT / "build" / (Path(rel).stem + ".json")
    bc.write_text(compile_to_json(src, rel), encoding="utf-8")
    py = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", rel],
                        capture_output=True, input=b"", cwd=str(ROOT))
    nd = subprocess.run([NODE, "oracle/node/index.js", str(bc.relative_to(ROOT))],
                        capture_output=True, input=b"", cwd=str(ROOT))
    dec = lambda b: b.decode("utf-8", "replace")              # noqa: E731
    return ((dec(py.stdout), dec(py.stderr)), (dec(nd.stdout), dec(nd.stderr)))


def _node(src: str, tag: str) -> tuple[str, str]:
    """Node 宿主跑一遍 → `(stdout, stderr)`（走 CLI 出口，与用户看到的完全一样）。"""
    return _both(src, tag)[1]


# ---------------------------------------------------------------------------
# 一、报错渲染：Node 与 Python **逐字相同**
# ---------------------------------------------------------------------------

#: 每条都挑一个「渲染的某个部件」：位置 / 源码行 / `^` / 调用链 / 提示。
_RENDER_CASES = {
    "除零": '令 甲 = 1\n令 乙 = 0\n打印(甲 / 乙)\n',
    "索引越界": '函数 取第九个(列)：\n    返回 列[9]\n打印(取第九个([1, 2]))\n',
    "未定义名字": '打印(没定义过的东西)\n',
    "方法不存在": '令 a = [1]\na.排续()\n',
    "断言失败": '断言(假, "该失败")\n',
}


@need_node
@pytest.mark.parametrize("tag", sorted(_RENDER_CASES))
def test_Node报错渲染与Python逐字相同(tag):
    """同一个程序，Node 的 `stderr` 与 Python CLI 的 `stderr` **逐字节相同**。

    ⚠️ 为什么要一模一样（而不是「大致像」）：三个 Python 执行器共用
    `JishiError.render()`，而宿主是 R7/R8 之后**唯一的形态** ——
    它必须自己长出完整的那一份，否则单二进制的报错会**退步**。
    """
    src = _RENDER_CASES[tag]
    (py_out, py_err), (nd_out, nd_err) = _both(src, tag)
    assert py_out == nd_out, f"{tag}：stdout 不同\npy={py_out!r}\nnd={nd_out!r}"
    assert py_err and nd_err, f"{tag}：本该报错（py={py_err!r} nd={nd_err!r}）"
    assert py_err == nd_err, (
        f"{tag}：报错渲染不一致\n"
        f"--- Python ---\n{py_err}\n--- Node ---\n{nd_err}")


@need_node
def test_Node报错渲染的五个部件都在():
    """位置 / `┌─` / 源码行 / `^` / 调用链 —— 一个都不能少。

    以前 Node 只有前两行（标题 + 「← 第 N 行第 C 列」）。
    """
    src = ('函数 算折扣(原价)：\n'
           '    返回 原价[9]\n'
           '函数 主流程()：\n'
           '    返回 算折扣([1])\n'
           '主流程()\n')
    _, err = _node(src, "parts")
    assert "┌─ build/_m66_parts_" in err, err            # 带文件名的位置
    assert "│     返回 原价[9]" in err, err              # 源码行（原样）
    assert "^" in err, err                              # 指示符
    assert "调用链（从外到内）：" in err and "主流程" in err, err


@need_node
def test_读不到源文件时不崩也不报IO错():
    """字节码里的 `filename` 指向一个不存在的文件 → 退回「没有源码行」那一档。

    ⚠️ 这条是**有意设计**（R6.3/R6.4 两处）：源码**不塞进字节码**（那会让每份
    `.json` 都背上整份源码、还和源文件两份），而是出错那一刻按 `filename` 现读。
    嵌入场景 / 文件被删时读不到 —— 那就只画位置框，**不崩、也不报 IO 错**。
    """
    src = '打印(1 / 0)\n'
    bc = ROOT / "build" / f"_m66_missing_{os.getpid()}.json"
    # 故意给一个不存在的文件名
    bc.write_text(compile_to_json(src, "build/不存在的文件.jsh"), encoding="utf-8")
    r = subprocess.run([NODE, "oracle/node/index.js", str(bc.relative_to(ROOT))],
                       capture_output=True, input=b"", cwd=str(ROOT))
    err = r.stderr.decode("utf-8", "replace")
    assert r.returncode == 1, err
    assert "错误 E2001：不能除以零" in err, err
    assert "┌─ build/不存在的文件.jsh:1:6" in err, err
    assert "IOError" not in err and "ENOENT" not in err, err


@need_node
def test_报错前的打印要吐出来():
    """`打印` 几行之后崩掉 —— 前面那几行不能丢（用户看不到跑到哪就没线索了）。

    ⚠️ 这条在 Rust 上是 R6.2 修的（它原来只在 `Ok` 分支打输出）；
    Node 是边跑边写 stdout，天然就对 —— 但**判据得有**，不然将来重构没人拦。
    """
    src = '打印("第一行")\n打印("第二行")\n打印(1 / 0)\n'
    out, err = _node(src, "flush")
    assert out == "第一行\n第二行\n", out
    assert "E2001" in err, err


# ---------------------------------------------------------------------------
# 二、静默成功不许回来（这三条以前**不报错**，还给出错答案）
# ---------------------------------------------------------------------------

@need_node
def test_越界赋值不再静默扩容():
    """`列[9] = 3` 必须**报错**，不能悄悄把列表撑长。

    ⚠️ JS 数组 `a[9] = x` 会自动补长，而 Python 的 list **不能**越界赋值 ——
    这就是本项目最恨的那类 bug：**不报错的错答案**（用户以为写进去了）。
    """
    src = '令 列 = [1, 2]\n列[9] = 3\n打印(列)\n'
    out, err = _node(src, "setitem")
    assert out == "", f"不该有输出（那说明它静默成功了）：{out!r}"
    assert "错误 E2003：索引越界（列表下标越界）" in err, err


@need_node
def test_越界弹出不再给undefined():
    """`[1].弹出(9)` 必须报「列表下标越界」——而且**不能说「列表是空的」**。

    ⚠️ 两件事一起修（R6.2 在 Python/Rust 上修过，R6.4 补 Node）：
    越界弹出以前静默给 `undefined`；而哪怕报了错，「列表是空的」也是句错话
    —— `[1]` 并不空，那句话会让人往错的方向查。
    """
    _, err = _node('令 列 = [1]\n列.弹出(9)\n', "pop")
    assert "E2003" in err and "列表下标越界" in err, err
    assert "列表是空的" not in err, err
    # 空列表**才**说「列表是空的」（与越界区分开）
    _, err2 = _node('令 列 = []\n列.弹出()\n', "popempty")
    assert "列表是空的" in err2 and "E2005" in err2, err2


@need_node
def test_类型不可比要报错而不是给假():
    """`1 < "a"` 必须报「不能比较」，不能静默给「假」。

    ⚠️ JS 的 `1 < "a"` 是 `false`（不报错）—— 又是一个「不报错的错答案」。
    同类的还有 `[1] < [2]`：JS 比的是**转成字符串**之后的结果，与 Python 的
    逐元素字典序不是一回事（这个方向恰好蒙对，但字典序得真按元素比）。
    """
    _, err = _node('打印(1 < "a")\n', "cmp")
    assert "E2002" in err and "不能比较「1」和「a」" in err, err
    # 可比的那些要照常给结果（不能为了拦错把正常路径也拦了）
    out, _ = _node('打印([1] < [2], [1] < [1, 2], "ab" < "b", 1 < 真)\n', "cmpok")
    assert out == "真 真 真 假\n", out


# ---------------------------------------------------------------------------
# 三、类型名与错误码：与 Python 侧的错误类逐项对齐
# ---------------------------------------------------------------------------

#: `(源码, 错误码, 类型名)` —— 这三条以前 Node 报的是「异常」「运行期错误」，
#: 于是 `e.类型`、`捕获 X` 的匹配、`jishi 错误 <码>` **三处一起错**。
_TYPE_CASES = (
    ('打印(不存在的名字)\n', "E0301", "找不到这个名字"),
    ('导入 不存在的模块\n', "E0302", "找不到这个函数"),
    ('令 甲 = 5\n甲()\n', "E2006", "这个东西不能调用"),
    ('函数 f()：\n    令 甲 = 乙\n    令 乙 = 1\n    返回 甲\n打印(f())\n',
     "E0301", "找不到这个名字"),
    ('令 甲 = 1\n令 乙 = 0\n打印(甲 / 乙)\n', "E2001", "不能除以零"),
    ('令 甲 = [1]\n打印(甲[9])\n', "E2003", "索引越界"),
)


@need_node
@pytest.mark.parametrize("src,code,title", _TYPE_CASES)
def test_Node错误码与类型名对齐Python(src, code, title):
    _, err = _node(src, f"type_{code}")
    assert code in err, f"没给码（应为 {code}）：{err!r}"
    assert title in err, f"类型名不对（应为「{title}」）：{err!r}"


@need_node
def test_宿主类型名与Python侧的错误类逐项一致():
    """把「程序自己接住的 `错.类型`」在两边的输出比一遍 —— **这才是 `e.类型` 的真值**。

    ⚠️ 渲染文本里出现某个词，不等于 `e.类型` 就是那个值（可能是消息里的字）。
    这条走「自己接住再打印 `错.类型`」，所以比的**就是** `e.类型`。
    """
    for tag, body in SHAPES.items():
        src = build_source(body)
        base = engine_outputs(src, with_node=False)["树遍历"]
        node_out = engine_outputs(src, with_node=True).get("Node")
        assert node_out is not None
        assert node_out == base, (
            f"{tag}：Node 与 Python 基准不一致\n  基准 {base[:130]}\n  Node {node_out[:130]}")


@need_node
def test_探针确实带着Node():
    """⚠️ **判据不许把 Node 漏掉**（R6.4 的教训）。

    以前两个体检工具都只带「三个 Python + Rust」——Node 的问题**一个都抓不到**。
    这条是防回退的：探针的 `engine_outputs` 必须真的把 Node 算进去。
    """
    outs = engine_outputs("打印(1)\n")
    assert "Node" in outs, "探针没带 Node —— 判据的视野又窄回去了"
