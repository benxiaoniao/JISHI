# -*- coding: utf-8 -*-
"""M170 · 基石库层的报错，位置要指到**用户的调用点**（不是库源码行）。

## 那个毛病长什么样

用户写两行的程序，报错说「第 237 行出错」：

```
错误 E2000：运行期错误（「参数.解析」的一次式需要两个参数…）
  ┌─ 程序.jsh:237:17        ← 237 是 `stdlib-jishi/参数.jsh` 的行号
```

而同一个错的 Rust 侧报的是 `2:9`（宿主不展开库层，跑的是原生实现）。
**同一个错两个位置**，用户看到的那份还指到自己的文件里一个不存在的地方。

## 修法（两层，缺一不可）

1. **库层行号整体抬进「保留带」**（`errors.库层行号带` = 1_000_000）：
   库层是编译期展开进主程序的，和用户代码共用一套行号 —— 不给它一段保留区，
   「这个位置在库内部」就只能靠**猜行号大小**。抬进带里之后是**结构性可判定**。
2. **渲染/输出前换回调用点**：取 `trace` 里「最后一个不在带里的帧」，
   那正是用户写的那一行。三个引擎（Python / Rust / Node）同一条规则。

## 顺带修的两个真问题

- **`_shift_lines` 从来没进过嵌套块**（`如果` 里那句 `抛出` 的行号少 1）。
  Rust 侧当年为了「与 Python 逐字相同」**故意**只下探一层，把 Python 的
  bug 当成了标准（`frontend/src/jishilib.rs` 的老注释还写着「多下探一层会差一行」）。
  M170 两边一起改成通用递归。
- **`参数.选项(p, "名字")` 直接 panic**（Rust 侧无条件 `args[2]`，
  而 `默认` 是可选参数）—— 一句合法调用把进程打崩，见本文件最后一条判据。
"""

import sys

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                            # noqa: E402

#: 触发库层报错的最小程序：`选项` 省略默认值 + 一个没声明的选项。
#: 报错点在 `_逐项解析` 的**嵌套 `如果` 里** —— 正好也在验「嵌套块行号」那条。
程序 = """\
导入 参数
令 p = 参数.新建("演示")
参数.选项(p, "名字")
参数.解析(p, ["--不认识"])
"""
期望行, 期望列 = 4, 6

#: 程序内 `打印(e)` 走的是渲染那条路（不经 CLI 的 `with_source`），位置也得对。
程序2 = """\
导入 参数
尝试：
    参数.解析(["a"])
捕获 异常 为 e：
    打印(e)
"""
期望行2, 期望列2 = 3, 10


def _跑(源: str, tmp_path, exe=None):
    import subprocess

    (tmp_path / "程序.jsh").write_text(源, encoding="utf-8", newline="\n")
    命令 = ([str(exe)] if exe else [sys.executable, "-m", "oracle.jishi.cli"])
    r = subprocess.run(命令 + ["程序.jsh"], cwd=str(tmp_path),
                       capture_output=True, timeout=180, input=b"")
    return (r.stdout + r.stderr).decode("utf-8", "replace"), r.returncode


# ---------------------------------------------------------------------------
# 一、位置
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("源,行,列,说明,期望码", [
    (程序, 期望行, 期望列, "库函数在嵌套块里报错", 1),
    # `程序2` 自己 `捕获` 住了 —— 程序正常退（0），错误是**打印**出来的
    (程序2, 期望行2, 期望列2, "程序内 打印(e)", 0),
])
def test_库层报错的位置是调用点(源, 行, 列, 说明, 期望码, tmp_path):
    """树遍历：位置必须是**用户文件**的那一行，而且行号不能是库层的。"""
    出, 码 = _跑(源, tmp_path)
    assert 码 == 期望码, f"（{说明}）退出码 {码}，应当是 {期望码}：{出[:300]}"
    assert f"程序.jsh:{行}:{列}" in 出, f"（{说明}）位置不是调用点：{出[:400]}"
    # 库里那句 `抛出` 在 200 行开外 —— 出现这种大行号就是没规整
    for 坏 in (":237:", ":236:", ":1000"):
        assert 坏 not in 出, f"（{说明}）报出了库源码行号（{坏}）：{出[:400]}"


@pytest.mark.skipif(not rust_exe_path().is_file(), reason="没编 jishi-rs")
def test_Rust_与_Python_位置逐字一致(tmp_path):
    """两个真正会发行的引擎，同一个错的「位置 + 源码行 + 调用链」要一模一样。

    ⚠️ 这条最值钱：Python 侧跑的是**基石库层**（编译期展开），Rust 侧跑的是
    **原生实现** —— 两条完全不同的代码路径。它们能对上，才说明规整规则是
    「按位置语义」而不是「按某条实现路径」。
    """
    def 要点(出: str) -> list[str]:
        """只取「标题 + 位置 + 源码行 + ^」，**不比调用链**。

        ⚠️ 调用链里有没有库函数那一帧，取决于该引擎跑的是**哪份实现**：
        Python 侧跑基石库层（`解析` 是个 Jishi 函数，有帧），
        Rust 侧跑**原生实现**（栈里根本没有 `解析` 这一层）。
        这不是本次改动引入的差异，而且「宿主不展开库层」是既定设计
        （见 `rust/src/run.rs` 里 `--dump-bytecode` 那条已知差异说明）。
        """
        return [ln for ln in 出.splitlines()
                if ln.startswith("错误 ") or "┌─ 程序.jsh:" in ln
                or ln.lstrip().startswith("4 │") or ln.lstrip().startswith("^")
                or ln.strip().startswith("│ ^")]

    py, _ = _跑(程序, tmp_path)
    rs, _ = _跑(程序, tmp_path, exe=rust_exe_path())
    assert 要点(rs) == 要点(py), f"--- Python ---\n{py}\n--- Rust ---\n{rs}"


# ---------------------------------------------------------------------------
# 二、结构性判据：库层行号必须整段落在保留带里
# ---------------------------------------------------------------------------

def test_库层行号全在保留带里():
    """编译一遍，所有指令的行号只能是「用户文件里的行」或「保留带里的行」。

    中间地带出现任何行号，就说明**有节点没跟着进带** —— 老代码里
    `如果` 内部的节点就是这样漏掉的（它们停在 200 多行，正好落在用户文件的行号
    范围之外、带之内，于是渲染时被当成用户行）。
    """
    from oracle.jishi import serialize
    from oracle.jishi.compiler import compile_source
    from oracle.jishi.errors import 库层行号带

    源行数 = 程序.count("\n")
    字节码 = serialize.loads(serialize.dumps(compile_source(程序, "x.jsh")))
    坏 = []
    for code in 字节码.codes:
        for ins in code.instrs:
            if ins.line is None:
                continue
            if ins.line > 源行数 and ins.line < 库层行号带:
                坏.append((code.name, ins.line))
    assert not 坏, f"这些行号落在「用户行号」与库层保留带之间（说明没进带）：{坏[:8]}"


# ---------------------------------------------------------------------------
# 三、回归：一句话把宿主打崩的那条
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not rust_exe_path().is_file(), reason="没编 jishi-rs")
def test_选项省略默认值不会把宿主打崩(tmp_path):
    """`参数.选项(p, "名字")` 是**合法**写法（`默认` 有默认值）。

    Rust 侧曾经无条件取 `args[2]` ⇒ `index out of bounds` ⇒ **进程 panic**。
    判据不是「有没有报错」，而是「**有没有正常跑完**」—— panic 的输出里
    既没有中文错误也没有退出码语义，用户只会看到一句英文。
    """
    import subprocess

    源 = '导入 参数\n令 p = 参数.新建("演示")\n参数.选项(p, "名字")\n打印("活下来了")\n'
    (tmp_path / "程序.jsh").write_text(源, encoding="utf-8", newline="\n")
    r = subprocess.run([str(rust_exe_path()), "程序.jsh"], cwd=str(tmp_path),
                       capture_output=True, timeout=180, input=b"")
    出 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert "panicked" not in 出, f"宿主 panic 了：{出[:400]}"
    assert r.returncode == 0, 出[:400]
    assert "活下来了" in 出
