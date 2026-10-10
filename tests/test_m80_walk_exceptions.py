# -*- coding: utf-8 -*-
"""R6 主体 · 树遍历参考执行器 —— 第 ⑤ 步：**异常与导入**（外加切片）。

补上的节点：`Try` / `ExceptHandler` / `Raise` / `Import`（`Slice` 是顺带补齐的
表达式 —— 它一直挂在 ① 的尾巴上，而 `examples/projects/` 里两个示例用它）。

## 这一轮的三处边界（都照既有规范，各有注释与测试）

1. **控制信号（`返回` / `中断` / `继续`）不被 `捕获` 接住**，但「最终」**一定要跑**
   —— 跑完再把信号原样往外传。「最终」自己跳出去时**覆盖**外层信号
   （`最终: 返回 2` 压过 `尝试: 返回 1`），这就是 `run_finally` 返回 `Option<Flow>`
   的原因。
2. **捕获体在「当前作用域」跑**（不新开子作用域）—— R5.1 定的：早先树遍历给
   「捕获 为 名字」单独建了子作用域，于是**捕获体里的赋值出了捕获体就没了**。
3. **`捕获体自己出错`时「最终」不跑** —— 照 `interpreter._do_try` 那个
   `except (RunBreak, RunContinue, _ReturnSignal, DebugQuit)`（四个宿主也一致），
   **不照 Python 语言本身**。

## 顺手修掉的一个真 bug

宿主把一个**不存在的异常类型名** `数值错误` 当类型用了三处，于是
`捕获 值错误` / `捕获 运行期错误` 在 Rust 宿主上**接不住切片错误**（Python / C VM /
Node 都接得住）。改成 `值错误`（Python 侧就是 `RunValueError`，码 E2005）。
语料 `25_异常与导入.jsh` 第 12 段钉住它。
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

os.environ.setdefault("JISHI_FRONTEND", "python")

import conformance as C                                            # noqa: E402
from oracle.jishi.compiler import compile_to_json                         # noqa: E402
from oracle.jishi.errors import JishiError                                # noqa: E402
from oracle.jishi.interpreter import run_source as run_tree               # noqa: E402
from oracle.jishi.parser import parse_source                              # noqa: E402

RUST = ROOT / "rust" / "target" / "release" / (
    "jishi-rs.exe" if os.name == "nt" else "jishi-rs")
need_rust = pytest.mark.skipif(
    not RUST.exists(),
    reason="Rust 宿主未构建（cd rust && cargo build --release）")

CORPUS = ROOT / "tests" / "cases" / "25_异常与导入.jsh"


def _run(args: list[str]) -> tuple[str, str]:
    r = subprocess.run([str(RUST)] + args, capture_output=True, input=b"")
    return (r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"))


def _write(obj, name: str) -> Path:
    # ⚠️ 文件名带 pid（并行不互相覆盖，同 test_m75 注释）。
    p = Path(tempfile.gettempdir()) / f"{os.getpid()}_{name}"
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return p


def _three(src: str) -> tuple[str, str, str, str]:
    """返回 `(树遍历 stdout, 树遍历 stderr, 字节码 stdout, 参考 stdout)`。

    ⚠️ 参考（Python 树遍历解释器）那边**出错前已经打印的也要算**
    （R6.2 统一过「出错前写进输出流的要吐出来」）—— 早先这里写成「一报错就
    `return`」，把缓冲里的输出丢了，于是「捕获体打印后最终块才报错」这类形状
    **假报不一致**。
    """
    def _with_err(out: str, err: str) -> str:
        """报错也算一种「输出」：**出错前打印的 + 报错首行**（两边同口径）。"""
        err = err.strip()
        return out + (("【错】" + err.splitlines()[0]) if err else "")

    ast_p = _write(C.jsonable(parse_source(src, "x.jsh")), "_m80_ast.json")
    bc_p = _write(json.loads(compile_to_json(src, "x.jsh")), "_m80_bc.json")
    wo, we = _run(["--walk", str(ast_p)])
    vo, ve = _run(["--load-bytecode", str(bc_p)])
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            run_tree(src, "x.jsh")
        ref = buf.getvalue()
    except JishiError as e:                                         # noqa: PERF203
        ref = buf.getvalue() + "【错】" + str(e).splitlines()[0]
    return _with_err(wo, we), we, _with_err(vo, ve), ref


SHAPES = [
    # ---- 捕获具体类型 / 读异常对象的属性 ----
    '尝试：\n    打印(1 / 0)\n捕获 除零错误 为 e：\n    打印(e.类型, e.消息)',
    '尝试：\n    令 甲 = [1]\n    打印(甲[9])\n捕获 索引错误 为 e：\n    打印(e.类型)',
    '尝试：\n    令 字典 = {}\n    打印(字典["键"])\n捕获 键错误 为 e：\n    打印(e.类型, e.消息)',
    # ⚠️ `打印(e)` 会把**完整渲染**打出来（含源码行 / `^`）——「宿主补源码框」
    # 是第 ⑥ 步的活，两边现在还不一样，所以这条形状**不进对拍**（见 D68 §四 ⑥）。
    '尝试：\n    抛出 值错误("x")\n捕获 值错误 为 e：\n    打印(类型(e))',
    # ---- 裸捕获 ----
    '尝试：\n    抛出 值错误("x")\n捕获：\n    打印("裸接")',
    '尝试：\n    打印(1)\n捕获：\n    打印("不该走到这")',
    # ---- 基类抓子类 ----
    '尝试：\n    抛出 键错误("k")\n捕获 运行期错误 为 e：\n    打印("父类", e.类型)',
    '尝试：\n    抛出 断言错误("a")\n捕获 异常 为 e：\n    打印("最宽", e.类型)',
    # ---- 接不住 → 往外冒 ----
    '尝试：\n    抛出 值错误("x")\n捕获 键错误：\n    打印("不该")',
    '尝试：\n    尝试：\n        抛出 键错误("k")\n    捕获 值错误：\n        打印("内层不")\n    最终：\n        打印("内层收尾")\n捕获 键错误 为 e：\n    打印("外层", e.消息)',
    # ---- 最终块 ----
    '尝试：\n    打印(1)\n最终：\n    打印(2)',
    '尝试：\n    抛出 值错误("x")\n最终：\n    打印(2)',
    '尝试：\n    打印(1 / 0)\n最终：\n    打印("清")',
    # 捕获体自己出错 → 「最终」**不跑**（既有口径）
    '尝试：\n    抛出 值错误("a")\n捕获 值错误：\n    打印(1 / 0)\n最终：\n    打印("清")',
    # ---- 控制信号不被捕获，但最终要跑 ----
    '函数 f()：\n    尝试：\n        返回 1\n    最终：\n        打印("清")\n打印(f())',
    '函数 f()：\n    尝试：\n        返回 1\n    最终：\n        返回 2\n打印(f())',
    '函数 f()：\n    遍历 i 在 范围(4)：\n        尝试：\n            如果 i == 2：\n                中断\n        最终：\n            打印("清")\n        打印(i)\n    返回 9\n打印(f())',
    '函数 f()：\n    遍历 i 在 范围(4)：\n        尝试：\n            如果 i == 3：\n                继续\n        最终：\n            打印("清")\n        打印(i)\n    返回 9\n打印(f())',
    # ---- 抛出 / 重抛 ----
    '抛出 值错误("坏了")',
    '抛出 值错误',
    '抛出 键错误',
    '尝试：\n    抛出 值错误("x")\n捕获 值错误 为 e：\n    抛出 e',
    '函数 重抛()：\n    尝试：\n        抛出 值错误("原始")\n    捕获 值错误 为 e：\n        抛出 e\n尝试：\n    重抛()\n捕获 值错误 为 e2：\n    打印(e2.消息)',
    '尝试：\n    抛出 "一段文本"\n捕获 为 e：\n    打印(e.类型, e.消息)',
    # ---- 捕获体里的赋值属于当前作用域（R5.1）----
    '函数 f()：\n    尝试：\n        抛出 值错误("x")\n    捕获 值错误：\n        令 结果 = "体里赋的"\n    返回 结果\n打印(f())',
    '函数 f()：\n    尝试：\n        抛出 值错误("x")\n    捕获 值错误 为 e：\n        令 结果 = e.消息\n    返回 结果\n打印(f())',
    # ---- 导入 ----
    '导入 随机\n打印(类型(随机))',
    '导入 随机 为 随\n打印(随.随机整数(7, 7))',
    '导入 没这个模块',
    # ⚠️ `从 python / 本地包` **不进对拍**：跨语言宿主有意不支持，报的是
    # 「宿主不支持…」（给用户看的正确信息）—— 与 Python 侧必然不同。
    # 单独有 `test_导入宿主有意不支持从python` 钉「两个宿主之间逐字相同」。
    # ---- 切片（顺带补齐）----
    '令 甲 = [1, 2, 3, 4]\n打印(甲[1:3])',
    '令 甲 = [1, 2, 3, 4]\n打印(甲[::-1])',
    '令 甲 = [1, 2, 3]\n打印(甲[:2], 甲[1:], 甲[::2])',
    '打印("甲乙丙"[0:2], "甲乙丙"[::-1])',
    '令 甲 = [1, 2, 3]\n甲[0:1] = [9]\n打印(甲)',
    '令 甲 = [1, 2, 3]\n甲[::2] = [7, 8]\n打印(甲)',
    '令 甲 = [1, 2, 3]\n打印(甲[::0])',
    '令 甲 = [1, 2, 3]\n甲[::2] = [7]',
    # 切片步长错的类型是「值错误」——`捕获 值错误` 必须接得住（宿主曾写成
    # 不存在的「数值错误」，于是只有 Rust 宿主接不住）
    '尝试：\n    令 甲 = [1, 2]\n    打印(甲[::0])\n捕获 值错误 为 e：\n    打印("接住", e.类型)',
    '尝试：\n    令 甲 = [1, 2]\n    打印(甲[::0])\n捕获 运行期错误：\n    打印("基类接住")',
    # ---- 捕获体里跳出（R6 收口，D73 §五修）：返回/中断/继续都要跑「最终」----
    '函数 f()：\n    尝试：\n        抛出 值错误("x")\n    捕获 值错误：\n        返回 5\n    最终：\n        打印("清")\n打印(f())',
    '函数 f()：\n    遍历 i 在 范围(4)：\n        尝试：\n            如果 i == 2：\n                抛出 值错误("x")\n        捕获 值错误：\n            中断\n        最终：\n            打印("清")\n        打印(i)\n    返回 9\n打印(f())',
    '函数 f()：\n    遍历 i 在 范围(4)：\n        尝试：\n            如果 i == 3：\n                抛出 值错误("x")\n        捕获 值错误：\n            继续\n        最终：\n            打印("清")\n        打印(i)\n    返回 9\n打印(f())',
    # ---- 宿主补全错误码表（M56-5 收口）----
    '抛出 "一段文本"',
    '抛出 真',
    '令 甲 = 5\n尝试：\n    抛出 值错误("x")\n捕获 甲：\n    打印("不该")',
    '导入 文本\n打印(文本.长度)',
]


@need_rust
@pytest.mark.parametrize("src", SHAPES)
def test_形状对拍(src: str):
    """树遍历 = 字节码 VM = 参考（Python 树遍历解释器）。"""
    wo, we, vo, ref = _three(src)
    assert wo == ref, f"树遍历 {wo!r} != 参考 {ref!r}（stderr={we[:200]!r}）"
    assert vo == ref, f"字节码 {vo!r} != 参考 {ref!r}"


@need_rust
def test_语料25三条路逐字一致():
    src = CORPUS.read_text(encoding="utf-8")
    exp = CORPUS.with_suffix(".out").read_text(encoding="utf-8")
    wo, we, vo, ref = _three(src)
    assert ref == exp, "参考输出与该语料的 .out 不一致"
    assert wo == exp, f"树遍历 != .out（stderr={we[:300]!r}）"
    assert vo == exp


@need_rust
def test_进度仪表全覆盖():
    """⑤ 之后，**语料里出现的节点全都实现了** —— 「还没实现」必须是 0。

    这是这个里程碑的硬指标：`还没实现 N` 那一列就是「还没接上的节点类型」。
    """
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "check_walk.py")],
                       capture_output=True, cwd=str(ROOT))
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, out + r.stderr.decode("utf-8", "replace")[-600:]
    assert "不一致 0" in out, out
    assert "还没实现 0" in out, out


@need_rust
def test_导入宿主有意不支持从python():
    """⚠️ **有意的边界**（登记在 `tools/check_engines.py` 的 `HOST_KNOWN_GAPS`）：
    跨语言宿主没有 Python，报的是「宿主不支持…」——那句本身是给用户看的正确信息。
    这里钉住**树遍历与字节码 VM 逐字相同**（不一致的话互拍会红）。"""
    for src in ("导入 随机 从 python", "导入 甲 从 本地包"):
        wo, _, vo, _ = _three(src)
        assert wo == vo, f"{src!r}：树遍历 {wo!r} != 字节码 {vo!r}"
        assert "不支持" in vo, vo


# ---------------------------------------------------------------------------
# 结构钉住
# ---------------------------------------------------------------------------

def test_报错头行走的是共用实现():
    """报错渲染**只有一份**（`render_error_with`，字节码 VM 与树遍历共用）。

    以前树遍历自己拼了一遍，于是 `抛出 值错误("x")` 在它那边渲染成
    「错误 E2005：值错误（x）」、字节码 VM 是「错误 E2005：数值不对（x）」——
    同一段源码两个宿主打出不同的字。D73 先抽了 `error_head`（头行），D74 又抽了
    `render_error_with`（含源码行 / `^` / 调用链），现在整份渲染只有一份。
    """
    walk = (ROOT / "rust" / "src" / "walk.rs").read_text(encoding="utf-8")
    lib = (ROOT / "rust" / "src" / "lib.rs").read_text(encoding="utf-8")
    # `run_walk`（在 lib.rs）渲染报错的地方必须走**共用的** `render_error_with`
    i = lib.index("pub fn run_walk")
    assert "render_error_with" in lib[i:i + 2000], "树遍历没用共用的报错渲染"
    assert lib.count("pub(crate) fn render_error_with") == 1, "渲染函数应当只有一份"
    # 头行那三条规则（标题 / 空消息不括号 / 无码兜底）收在 `error_head`，只有一份
    assert lib.count("pub(crate) fn error_head") == 1, "error_head 应当只有一份"
    assert "error_head(e)" in lib, "渲染函数没走 error_head"
    # walk.rs 不许自己拼「错误 {码}：」这种头行
    assert '错误 {' not in walk, "walk.rs 里又出现了自己拼的报错头行 —— 请改用共用渲染"


def test_不许出现不存在的异常类型名():
    """`数值错误` **不是**语言里的异常类型 —— 用它当类型名会让 `捕获 值错误`
    接不住那个错误（宿主曾经有三处这么写）。真正的类型名清单见
    `oracle/jishi/errors.py` 与 lang-spec 的 `exceptions` 节。"""
    for rel in ("rust/src/lib.rs", "rust/src/walk.rs", "oracle/node/runtime.js"):
        p = ROOT / rel
        if not p.exists():
            continue
        s = p.read_text(encoding="utf-8")
        assert "数值错误" not in s, f"{rel} 里出现了不存在的异常类型名「数值错误」"


def test_异常类型表与语言规格同宽():
    """两个宿主的异常类型清单 = **lang-spec 的 `exceptions` 节**（权威清单）。

    M55 补 `断言错误` 时发现它一直缺着（`捕获 断言错误` 在 Rust 上报
    「找不到名字」），所以拿测试钉住「清单不许比规格窄」。
    """
    import oracle.jishi.ai as AI                                          # noqa: PLC0415
    names = json.loads(AI.lang_spec_json())["exceptions"]
    lib = (ROOT / "rust" / "src" / "lib.rs").read_text(encoding="utf-8")
    node = (ROOT / "oracle" / "node" / "runtime.js").read_text(encoding="utf-8")
    for t in names:
        assert f'"{t}"' in lib, f"宿主的异常类型表里少了「{t}」"
        # Node 那边用**单引号**（`'异常': new ExcType('异常')`），两种都认
        assert (f"'{t}'" in node or f'"{t}"' in node), \
            f"Node 的异常类型表里少了「{t}」"
