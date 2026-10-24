# -*- coding: utf-8 -*-
"""C VM 的「预取 + 变更即失效」结构钉（B2，`docs/设计决策.md` D77 §二）。

这一族**行为**判据在 `tests/test_m83_iter_mutation.py`（六实现逐字一致）与
`tests/cases/26_遍历中改列表.jsh`（进语料，`tools/check_engines.py` 全引擎比）。
本文件只钉三件**机制**上的事 —— 它们是上面那些行为判据的前提，坏了会**静默**：

1. **ABI 版本两处一致**（头文件 ↔ Python 常量）。回调签名/结构布局对不上时
   **不报错**，只是参数错位、算错或崩 —— 所以装载时就必须拦住。
2. **批量预取还在**：B2 的修法是「丢了再按绝对下标重取」，**不是**退回逐个
   向宿主要（实测那样要慢 1.81x，D77 §二）。判据用**调用次数**而不是墙上时间，
   免得并行跑时飘。
3. **登记表跑完就干净**：`_iter_lists` / `_iter_owner` 不能越积越多（长跑进程
   的 LSP / MCP 会一直涨）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_HEADER = ROOT / "cvm" / "include" / "jsvm.h"


def test_ABI版本号与头文件一致():
    """`cvm_bind._JSVM_ABI_VERSION` 必须等于 `jsvm.h` 的 `JSVM_ABI_VERSION`。

    两处一起改过的**唯一**保证就是这个；漏改一处 → 装载时报的版本与库不符，
    或者更糟（版本号都忘了改，那就谁也不知道签名已经变了）。
    """
    from oracle.jishi import cvm_bind

    m = re.search(r"^#define\s+JSVM_ABI_VERSION\s+(\d+)", _HEADER.read_text(
        encoding="utf-8"), re.M)
    assert m, "jsvm.h 里找不到 JSVM_ABI_VERSION"
    assert int(m.group(1)) == cvm_bind._JSVM_ABI_VERSION, (
        f"头文件 v{m.group(1)} ≠ Python 常量 v{cvm_bind._JSVM_ABI_VERSION}"
        " —— 两边一起改（见 cvm_bind 里那段说明）")


def test_动态库自报的ABI版本一致():
    """**装载时**就得核对：源码树里可能留着上一次编出来的旧库（踩过两次：
    「改了 C 源，Python 加载的却是另一份旧库，一句提示都没有」）。"""
    from oracle.jishi import cvm_bind

    lib = cvm_bind.load_lib()
    assert int(lib.jsvm_abi_version()) == cvm_bind._JSVM_ABI_VERSION, (
        "动态库版本与宿主不一致 —— 请 `python cvm/build.py` 重编"
        "（或重装 wheel）")


def _run_with_host(src: str):
    """跑一段程序，并把用过的 `CvmHost` 交出来（拿来数回调次数 / 查登记表）。"""
    from oracle.jishi import cvm_bind
    from oracle.jishi.compiler import compile_source

    class _Spy(cvm_bind.CvmHost):
        def __init__(self, *a, **k):
            self.batch_calls = 0
            super().__init__(*a, **k)

        def _cb_iter_next_batch(self, it, frm, out, cap, n, line, col):
            self.batch_calls += 1
            return super()._cb_iter_next_batch(it, frm, out, cap, n, line, col)

    saved = cvm_bind.CvmHost
    cvm_bind.CvmHost = _Spy
    try:
        cmod = compile_source(src, "x.jsh", fuse_method_call=True)
        runner = cvm_bind.CvmRunner(cmod, "x.jsh")
        try:
            runner.run()
        finally:
            runner.close()
    finally:
        cvm_bind.CvmHost = saved
    return runner.host


def test_批量预取还在_不是逐个向宿主要():
    """遍历 N 个元素，取批次数应当是 `ceil(N/256)` 量级，**不是 N**。

    这是 B2 的**前提**：如果我们为了语义正确改成「每个元素一次宿主回调」，
    行为判据照样全绿（那才是最容易犯的错），但 C VM 的列表遍历会慢 1.81x。
    所以这里用**调用次数**把它钉住（不用墙上时间 —— 那个并行时会飘）。
    """
    n = 5000
    src = (f"令 列 = [i 遍历 i 在 范围({n})]\n令 s = 0\n"
           "遍历 x 在 列：\n    s = s + x\n打印(s)\n")
    host = _run_with_host(src)
    # 建表时遍历 范围(n) 一次 + 求和时遍历列表一次 ≈ 2 * ceil(n/256) ≈ 40 次
    assert host.batch_calls <= 100, (
        f"取批 {host.batch_calls} 次（N={n}）—— 像退回逐个回调了（应当 ~40 次）")
    assert host.batch_calls >= 2, "一次取批都没有？批量路径是不是根本没走"


def test_登记表跑完就清空():
    """`_iter_lists` / `_iter_owner` 是「谁在遍历这个列表」的登记表。

    不摘干净的后果和 M47 那个漏引用同族：**不报错、判据全绿，只是长跑进程
    一直涨**。这里跑一个「反复建表 + 遍历 + 调用方法」的程序，跑完必须空。
    """
    src = ("令 总 = 0\n"
           "遍历 k 在 范围(50)：\n"
           "    令 列 = [i 遍历 i 在 范围(20)]\n"
           "    遍历 x 在 列：\n"
           "        总 = 总 + x\n"
           "    列.追加(1)\n"
           "打印(总)\n")
    host = _run_with_host(src)
    assert host._iter_lists == {}, f"登记表没清干净：{host._iter_lists!r}"
    assert host._iter_owner == {}, f"反向索引没清干净：{host._iter_owner!r}"


def test_遍历中改列表时取批次数的量级():
    """「变更即失效」的代价是**每次失效多一次取批**，不是每个元素一次。

    这条不看倍数（那是基准工具的事），只看**量级**：把 2000 个元素逐个删掉时
    取批次数应当仍在几百量级，而不是上万。
    """
    n = 2000
    src = (f"令 列 = [i 遍历 i 在 范围({n})]\n"
           "令 删 = 列[0:200]\n"
           "遍历 x 在 列：\n"
           "    如果 x < 200：\n        列.移除(x)\n"
           "打印(长度(列))\n")
    host = _run_with_host(src)
    assert host.batch_calls < 4 * n, (
        f"取批 {host.batch_calls} 次（N={n}）—— 每次改动都退化成逐个了？")


@pytest.mark.parametrize("src,expect", [
    # `from` 用错（拿宿主自己的「读到哪」当下标）会**跳掉**元素 —— 这两条
    # 是最短能看出「跳了」的形状：删掉的那个不能少、被顶上来的不能多。
    ('令 列 = [1, 2, 3]\n遍历 x 在 列：\n    打印(x)\n    如果 x == 1：\n'
     '        列.移除(2)\n', "1\n3\n"),
    ('令 列 = [1, 2, 3]\n遍历 x 在 列：\n    打印(x)\n    如果 x == 1：\n'
     '        列.插入(1, 99)\n', "1\n99\n2\n3\n"),
])
def test_失效后重取不跳元素(src, expect):
    """C VM 丢缓冲后**必须从「已交付计数」重取**（ABI v2 的 `from`）。

    用宿主自己的位置当下标就会跳掉「已读过、还没交付」的那几个 —— 症状是
    **少打印元素**（不报错）。这两条正好各卡一个方向。
    """
    from oracle.jishi import cvm_bind

    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cvm_bind.run_source_c(src, filename="x.jsh")
    assert buf.getvalue() == expect


def test_动态库定位指向仓库根():
    """`cvm_bind` 认定的「仓库根」必须真的是仓库根。

    🔴 2026-10-09 抓到的真 bug：R8.4 把 `jishi/` 搬成 `oracle/jishi/` 之后，
    `cvm_bind._ROOT = parents[1]` 就把 **`oracle/`** 当成了仓库根 ⇒
    `cvm/bin/` 永远找不到，只能退化去用 `_native/` 里那份**旧的**预编译库
    （正是本文件上面注释说的那个坑）。**本地有残留的 `_native/jsvm.dll` 遮着**，
    CI 干净检出上直接 `找不到 C 虚拟机动态库（libjsvm.so）`。

    这条判据**不依赖有没有预编译产物** —— 它只问「路径算对了没」。
    """
    from oracle.jishi import cvm_bind

    assert cvm_bind._ROOT == ROOT, (
        f"cvm_bind._ROOT = {cvm_bind._ROOT}，应为仓库根 {ROOT}"
        "（包从 `jishi/` 搬到 `oracle/jishi/` 之后要 `parents[2]`）")
    assert cvm_bind._BIN == ROOT / "cvm" / "bin", cvm_bind._BIN
    # 源码树里 `cvm/src/` 在 ⇒ 必须优先用 `cvm/bin/` 那份（R6.7 的机制）
    assert (ROOT / "cvm" / "src").is_dir()
