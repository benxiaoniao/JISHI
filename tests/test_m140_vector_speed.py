# -*- coding: utf-8 -*-
"""M140 · `向量` 的性能验收：**批处理必须跑在宿主原生代码里**，不许退回字节码循环。

## 背景（M139 量出来的事实）

M139 把 `向量` 写在**基石库层**（`stdlib-jishi/向量.jsh`，一份代码五引擎共用）。
功能没问题，但一万元素相加 **Python VM 488 毫秒**（Rust 靠原生实现 <1 毫秒）——
差三个数量级。原因不在算法，而在**执行方式**：`.jsh` 编译出来的是字节码，
一万次「取下标 + 加法 + 追加」全走解释器。

用户 2026-10-26 拍板：**给解释器加快速路径** —— 把这类批处理**下沉成宿主原语**
（`stdlib/向量.py` / `rust/src/stdlib/vector.rs` / `vm.js` 的 `VECTOR_FUNCS`）。
于是「一万次循环」变成宿主的一次 `map`/迭代，四个引擎一起快。

## 这个文件钉两条

1. **行为不许变**：同样的程序在五个引擎上逐字节一致（换了实现方式，不是换了语义）；
2. **快路径必须真的在**：一万元素相加每个引擎都有一个**墙上时间**上限。
   📌 上限设得宽松（60 毫秒）是有意的：它**不是性能排行榜**，而是「有没有退回
   字节码」的**探测器** —— 退回的话是 488 毫秒，差着一个数量级，一定红。

⚠️ 第 2 条断言的是**墙上时间** ⇒ 打了 `计时`，只在**串行节**跑（并行负载会把
它抬成假红，见 `AGENTS.md` §1 那条规矩）。
"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_m52_jishilib import _engines                    # noqa: E402

from oracle.jishi import jishilib                         # noqa: E402
from oracle.jishi.interpreter import run_source            # noqa: E402


def _跑(源码: str) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        run_source(源码)
    return out.getvalue()


#: 覆盖全部 11 个函数，且**故意混整数与小数**——
#: 「换了实现」最容易在这里露馅（`[1,2] × 0.5` 该是 `[0.5, 1.0]` 而不是 `[0.5, 1]`）。
_用例 = """\
导入 向量
打印(向量)
打印(向量.构造(3), 向量.构造(2, 1.5))
打印(向量.加([1, 2], [10, 20]), 向量.加([1, 2], [10.0, 20.0]))
打印(向量.减([10, 20], [1, 2]), 向量.标量乘([1, 2], 0.5))
打印(向量.点积([1, 2], [3, 4]), 向量.点积([1.5, 2], [2, 4]))
打印(向量.和([1, 2, 3]), 向量.均值([2, 4, 6]))
打印(向量.最大([1, 9, 3]), 向量.最小([1, 9, 3]), 向量.维数([1, 2, 3]))
打印(向量.切片([1, 2, 3, 4], 1, 3), 向量.切片([1, 2, 3], 1), 向量.切片([1, 2, 3], 0, 0))
"""


def test_五引擎逐字节一致():
    """换成宿主原语**不能改变行为** —— 五个引擎的输出要一模一样。"""
    base_label, base = None, None
    for label, run in _engines("m140"):
        ok, out = run(_用例)
        assert ok, f"{label} 本该正常退出却报错：{out[:300]}"
        if base is None:
            base_label, base = label, out
        else:
            assert out == base, (
                f"{label} 与 {base_label} 输出不一致\n{base_label}: {base!r}\n{label}: {out!r}")
    assert base and base.splitlines()[0] == "<模块 向量>"


def test_错误路径五引擎也一致():
    """长度不等这条最常见的错，五个引擎要说同一句话（含两个长度）。"""
    src = "导入 向量\n打印(向量.加([1, 2, 3], [1, 2]))\n"
    base_label, base = None, None
    for label, run in _engines("m140e"):
        ok, out = run(src)
        assert not ok, f"{label} 本该报错却通过了：{out[:200]}"
        # 报错渲染里带文件名/行号，只比「那句人话」
        关键 = [行 for 行 in out.splitlines() if "长度相同" in 行]
        assert 关键, f"{label} 的报错里没提「长度相同」：{out[:300]}"
        句 = 关键[0].strip()
        # ⚠️ 只比「E2005：」之后的**那句话**：树遍历那条走的是异常对象
        #    （外面会多套一层标题），与宿主自己打 stderr 的形态本来就不同 ——
        #    这是已登记的五引擎分叉（`AGENTS.md` §3.3），不是语义差异。
        if "E2005：" in 句:
            句 = 句.split("E2005：", 1)[1]
        if base is None:
            base_label, base = label, 句
        else:
            assert 句 == base, f"{label} 与 {base_label} 的说法不一致：{句!r} vs {base!r}"


def test_向量不在基石库层():
    """结构性判据：**它一旦被搬回 `stdlib-jishi/`，上面那条性能判据就会红。**

    这条写在这里是为了让人一眼看懂「为什么快」：快的根因不是代码写得好，
    而是**没走字节码**。搬回去 = 一万次解释循环。
    """
    assert not jishilib.has("向量"), (
        "`向量` 又被放进基石库层了 —— 那样一万元素相加会退回 ~488 毫秒。"
        "要么搬回来（并接受慢），要么同步调整 test_m140 的阈值再说明理由。")


# ---------------------------------------------------------------------------
# 性能探测器（串行节）
# ---------------------------------------------------------------------------

#: 一万元素相加的**墙上时间上限**（毫秒）。全部引擎同一个数：
#: 实测（2026-10-26 本机，三次取 min）：树遍历 2 · Python VM 3 · C VM 3 · Node 7 · Rust 0。
#: 定 60 = 给最慢的 Node 还留 8 倍余量；而**退回字节码是 488 毫秒**，必红。
#: ⚠️ 这不是性能排行榜，是「快路径在不在」的探测器 —— 别为了让它好看往下调。
上限毫秒 = 60

_测速程序 = """\
导入 向量
导入 时间
令 甲 = 向量.构造(10000, 1)
令 乙 = 向量.构造(10000, 2)
令 起 = 时间.高精度时间()
令 和 = 向量.加(甲, 乙)
令 完 = 时间.高精度时间()
令 起2 = 时间.高精度时间()
令 点 = 向量.点积(甲, 乙)
令 完2 = 时间.高精度时间()
打印(整数((完 - 起) * 1000), 整数((完2 - 起2) * 1000))
"""


@pytest.mark.计时
def test_一万元素相加每个引擎都在快路径上():
    """每台引擎跑三遍取 min（交错取样，见 `AGENTS.md` §1「写时间判据」）。"""
    慢: list[str] = []
    for label, run in _engines("m140p"):
        最好 = None
        for _ in range(3):
            ok, out = run(_测速程序)
            assert ok, f"{label} 测速程序跑挂了：{out[:300]}"
            加, 点 = (int(x) for x in out.split())
            本档 = max(加, 点)
            最好 = 本档 if 最好 is None else min(最好, 本档)
        if 最好 is not None and 最好 > 上限毫秒:
            慢.append(f"{label} {最好} 毫秒")
    assert not 慢, (
        "一万元素相加超过了 %.0f 毫秒：%s —— 快路径是不是没了？"
        "（退回字节码会是 ~488 毫秒）" % (上限毫秒, "；".join(慢)))
