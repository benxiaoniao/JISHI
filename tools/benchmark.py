# -*- coding: utf-8 -*-
"""基石语言执行器性能基准。

场景全部用基石源码写就，同一份代码分别交给：
  1. 树遍历解释器（M0，jishi/interpreter.py）—— 语义基准
  2. Python 字节码虚拟机（M4a，jishi/vm.py）
  3. C 字节码虚拟机（M4b，cvm/bin/jsvm.dll）
  4. **Rust 字节码虚拟机（R6 起：`rust/target/release/jishi-rs`，独立二进制）**

## 第 4 个执行器怎么量才算公平

它是个**独立进程**，所以「跑一次」里必然含**进程启动**。这一点必须**摆在明面上**：

* 本脚本在末尾**单独公布启动成本**（跑一个空程序的时间）；
* 拿它跟 C VM 比时，只有**单轮耗时远大于启动成本**的场景才说明问题 ——
  也就是「内建调用密集」那种：C VM 的墙是**宿主回调**（约 2.4 µs/次），
  而同进程的 Rust 原生标准库**没有这道边界**。R6 要定「C VM 留不留」，量的就是这个。

⚠️ **这是 R6 的度量起点，不是结论**：一次 subprocess 里含启动、含读文件、
含进程初始化。本项目的老规矩：**先量再说，量完再定，不预设。**

用法：
    python tools/benchmark.py            # 默认时长（每场景约 1 秒）
    python tools/benchmark.py --quick    # 快速摸底（每场景约 0.2 秒）

输出：各场景的耗时与相对树遍历的倍数。耗时用「固定重复次数、
取最优一轮」的方式测，可复现（不受机器瞬时负载抖动影响）。
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jishi.interpreter import run_source as run_tree          # noqa: E402
from jishi import vm as vm_mod                                # noqa: E402

#: 各场景的基石源码与建议重复次数（保证单轮耗时可测又不至于太久）
SCENARIOS = {
    "整数循环累加": (
        "令 总和 = 0\n"
        "遍历 i 在 范围(20000)：\n"
        "    总和 = 总和 + i\n"
        "打印(总和)\n",
        3),
    "递归斐波那契": (
        "函数 斐波(n)：\n"
        "    如果 n < 2：\n"
        "        返回 n\n"
        "    返回 斐波(n - 1) + 斐波(n - 2)\n"
        "打印(斐波(18))\n",
        2),
    "当循环倒计数": (
        "令 i = 30000\n"
        "当 i > 0：\n"
        "    i = i - 1\n"
        "打印(i)\n",
        3),
    "列表构建与遍历": (
        "令 甲 = []\n"
        "遍历 i 在 范围(5000)：\n"
        "    甲.追加(i * 2)\n"
        "令 总和 = 0\n"
        "遍历 v 在 甲：\n"
        "    总和 = 总和 + v\n"
        "打印(总和)\n",
        3),
    # M46 加：专门量「迭代批量快路径」对不同元素类型的效果。
    # 设计要点：**先建一次列表，再反复遍历 20 轮**——这样构建阶段只占约 5%，
    # 测到的基本就是迭代本身（C 侧每 256 个元素回调一次宿主填缓冲）。
    # 三个场景结构**完全一样**、循环体都一样（只计数），只换元素类型——
    # 于是倍数差异就只反映「填缓冲时构造 JVal 那一步」的成本，不掺别的东西。
    "浮点列表遍历": (
        "令 甲 = []\n"
        "遍历 i 在 范围(400)：\n"
        "    甲.追加(i * 1.5)\n"
        "令 计数 = 0\n"
        "令 轮 = 0\n"
        "当 轮 < 20：\n"
        "    遍历 v 在 甲：\n"
        "        计数 = 计数 + 1\n"
        "    轮 = 轮 + 1\n"
        "打印(计数)\n",
        3),
    "文本列表遍历": (
        "令 甲 = []\n"
        "遍历 i 在 范围(400)：\n"
        "    甲.追加(\"值\" + 文本(i))\n"
        "令 计数 = 0\n"
        "令 轮 = 0\n"
        "当 轮 < 20：\n"
        "    遍历 v 在 甲：\n"
        "        计数 = 计数 + 1\n"
        "    轮 = 轮 + 1\n"
        "打印(计数)\n",
        3),
    # M46 阶段一④：字典的两个方向分开量——写入走「延迟合并写」，
    # 读取不行（每次读都要真拿一个值，没得合并），分开才知道提升来自哪一边。
    "字典写入密集": (
        "令 字 = {}\n"
        "遍历 i 在 范围(5000)：\n"
        "    字[i] = i * 2\n"
        "打印(长度(字))\n",
        3),
    "字典查找密集": (
        "令 字 = {}\n"
        "遍历 i 在 范围(400)：\n"
        "    字[i] = i * 2\n"
        "令 和 = 0\n"
        "令 轮 = 0\n"
        "当 轮 < 20：\n"
        "    遍历 i 在 范围(400)：\n"
        "        和 = 和 + 字[i]\n"
        "    轮 = 轮 + 1\n"
        "打印(和)\n",
        3),
    "内建调用密集": (
        # M46 阶段一②：量「宿主回调」这条路本身的开销。`长度` 是 O(1)，
        # 所以这个场景测到的几乎全是「C 侧 → 宿主 → 回 C 侧」的往返成本
        # （内建 / 标准库 / 方法调用都走同一条 `_cb_call`）。
        "令 甲 = [1, 2, 3, 4, 5]\n"
        "令 和 = 0\n"
        "遍历 i 在 范围(3000)：\n"
        "    和 = 和 + 长度(甲)\n"
        "打印(和)\n",
        3),
    "布尔列表遍历": (
        "令 甲 = []\n"
        "遍历 i 在 范围(400)：\n"
        "    甲.追加(i % 2 == 0)\n"
        "令 计数 = 0\n"
        "令 轮 = 0\n"
        "当 轮 < 20：\n"
        "    遍历 v 在 甲：\n"
        "        计数 = 计数 + 1\n"
        "    轮 = 轮 + 1\n"
        "打印(计数)\n",
        3),
    "字符串拼接": (
        "令 s = \"\"\n"
        "遍历 i 在 范围(2000)：\n"
        "    s = s + \"字\"\n"
        "打印(长度(s))\n",
        3),
    # M47 阶段二①加：字符串改成 C 侧原生之后，把文本的另外几条主要路径也
    # 单独量出来。**刻意分开**——拼接/比较走 C 侧（零回调），而下标/切片必须
    # 回调宿主（C 侧看不见 Python 的切片协议），两者的收益完全不同向：
    # 分开量才不会互相掩盖（前者是 3x+，后者仍低于 1x，见文件末尾的边界说明）。
    "文本比较": (
        "令 甲 = \"abc\"\n"
        "令 计 = 0\n"
        "遍历 i 在 范围(5000)：\n"
        "    如果 甲 == \"abc\"：\n"
        "        计 = 计 + 1\n"
        "打印(计)\n",
        3),
    "文本下标": (
        "令 甲 = \"一二三四五\"\n"
        "令 和 = 0\n"
        "遍历 i 在 范围(5000)：\n"
        "    和 = 和 + 长度(甲[i % 5])\n"
        "打印(和)\n",
        3),
    "文本切片": (
        "令 甲 = \"一二三四五六七八九十\"\n"
        "令 和 = 0\n"
        "遍历 i 在 范围(3000)：\n"
        "    和 = 和 + 长度(甲[2:7])\n"
        "打印(和)\n",
        3),
    "遍历文本": (
        "令 甲 = \"一二三四五\"\n"
        "令 计 = 0\n"
        "遍历 i 在 范围(3000)：\n"
        "    遍历 字 在 甲：\n"
        "        计 = 计 + 1\n"
        "打印(计)\n",
        3),
    "函数调用密集": (
        "函数 加一(x)：\n"
        "    返回 x + 1\n"
        "令 总和 = 0\n"
        "遍历 i 在 范围(8000)：\n"
        "    总和 = 加一(总和)\n"
        "打印(总和)\n",
        3),
    "算术混合运算": (
        "令 总和 = 0\n"
        "遍历 i 在 范围(10000)：\n"
        "    总和 = 总和 + i * 3 - 7 // 2 + i % 5\n"
        "打印(总和)\n",
        3),
}

#: 四个执行器。**C VM / Rust VM 缺了就自动少一列**（不假装它跑过）。
EXECUTORS_ALL = [
    ("树遍历", "run_tree"),
    ("Python VM", "run_vm"),
    ("C VM", "run_cvm"),
    ("Rust VM", "run_rust"),
]


def _available_executors() -> list[tuple[str, str]]:
    from jishi import tokenizer as _T

    out = []
    for label, key in EXECUTORS_ALL:
        if key == "run_cvm":
            try:
                from jishi import cvm_bind
                cvm_bind.load_lib()
            except Exception:                                  # noqa: BLE001
                print("C VM 动态库不可用，少了那一列")
                continue
        if key == "run_rust":
            if not _rust_exe().exists():
                print("Rust 宿主二进制不存在，少了那一列"
                      "（先构建：cd rust && cargo build --release）")
                continue
            if not _T.rust_available():
                print("Rust 前端扩展未构建，少了那一列"
                      "（先跑 python core/build.py）")
                continue
        out.append((label, key))
    return out


def _run_tree(src: str) -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        run_tree(src, filename="<基准>")


def _run_vm(src: str) -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        vm_mod.run_source(src, filename="<基准>")


def _run_cvm(src: str) -> None:
    from jishi import cvm_bind

    buf = io.StringIO()
    with redirect_stdout(buf):
        cvm_bind.run_source_c(src, filename="<基准>")


#: Rust 宿主二进制（**按平台**取名 —— 见 `tests/conftest.py::rust_exe_path`）
def _rust_exe() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


#: 编译产物缓存：`src → 字节码 JSON 路径`（**编译成本不算进 VM 的耗时里**）。
#: ⚠️ 用 `JISHI_FRONTEND=rust` 编 —— 于是这条链路是**全程原生**的：
#: Rust 前端产字节码 JSON → Rust VM 消费。正好是 R6/R8 要的那条路。
_BC_CACHE: dict[str, Path] = {}


def _run_rust(src: str) -> None:
    import subprocess

    path = _BC_CACHE.get(src)
    if path is None:
        from jishi import tokenizer as _T
        from jishi.compiler import compile_to_json

        out = ROOT / "build" / "_bench_rust_bc.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        old = os.environ.get(_T.FRONTEND_ENV)
        os.environ[_T.FRONTEND_ENV] = "rust"
        try:
            body = compile_to_json(src, "<基准>")
        finally:
            if old is None:
                os.environ.pop(_T.FRONTEND_ENV, None)
            else:
                os.environ[_T.FRONTEND_ENV] = old
        out.write_text(body, encoding="utf-8")
        _BC_CACHE[src] = path = out
    subprocess.run([str(_rust_exe()), str(path)],
                   capture_output=True, input=b"")


_RUNNERS = {"run_tree": _run_tree, "run_vm": _run_vm, "run_cvm": _run_cvm,
            "run_rust": _run_rust}


def _best_time(fn, src: str, repeats: int, tries: int = 3) -> float:
    """重复 repeats 次为一轮，测 tries 轮取最优（秒）。"""
    best = float("inf")
    for _ in range(tries):
        t0 = time.perf_counter()
        for _ in range(repeats):
            fn(src)
        best = min(best, time.perf_counter() - t0)
    return best


def _rust_startup() -> float:
    """量 Rust VM 的**进程启动成本**（跑一个空程序，5 次取最优，单位秒）。

    ⚠️ 为什么要单独量并**从那一列里扣掉**：Rust VM 是独立进程，一次 `subprocess`
    里含进程创建 + 动态库加载 + 读文件 + 退出。不扣的话，「近 0 耗时」的场景
    （比如只要 3 ms 的 C VM 场景）会显示成 10 ms 级的差距，
    结论就从「VM 快慢」悄悄变成了「进程启动快慢」—— **指标选错，结论就会错**。
    """
    import subprocess

    exe = _rust_exe()
    tiny = ROOT / "build" / "_bench_rust_empty.json"
    tiny.parent.mkdir(parents=True, exist_ok=True)
    from jishi import tokenizer as _T
    from jishi.compiler import compile_to_json

    old = os.environ.get(_T.FRONTEND_ENV)
    os.environ[_T.FRONTEND_ENV] = "rust"
    try:
        body = compile_to_json("令 空转 = 1\n", "<空>")
    finally:
        if old is None:
            os.environ.pop(_T.FRONTEND_ENV, None)
        else:
            os.environ[_T.FRONTEND_ENV] = old
    tiny.write_text(body, encoding="utf-8")

    best = float("inf")
    for _ in range(5):
        t0 = time.perf_counter()
        subprocess.run([str(exe), str(tiny)], capture_output=True, input=b"")
        best = min(best, time.perf_counter() - t0)
    return best


def main() -> int:
    ap = argparse.ArgumentParser(description="基石三执行器性能基准")
    ap.add_argument("--quick", action="store_true", help="快速摸底模式")
    args = ap.parse_args()

    tries = 2 if args.quick else 3
    executors = _available_executors()
    has_rust = any(k == "run_rust" for _, k in executors)
    startup = _rust_startup() if has_rust else 0.0
    print(f"基石语言性能基准（每场景取 {tries} 轮最优）\n")
    if has_rust:
        print(f"⚠️ Rust VM 是**独立进程**，每轮含进程启动 —— 已从它那一列里"
              f"**扣掉**：{startup * 1000:.1f} ms/轮（空程序实测）\n")

    header = f"{'场景':<10} {'遍数':>4}  " + "".join(
        f"{name:>10} " for name, _ in executors)
    print(header)
    print("-" * len(header))

    speed_rows: list[tuple[str, list[float]]] = []
    repeats_hint = 0

    for name, (src, repeats) in SCENARIOS.items():
        times: dict[str, float] = {}
        rust_weak = False
        for label, key in executors:
            t = _best_time(_RUNNERS[key], src, repeats, tries)
            if key == "run_rust":
                # 扣掉 `repeats` 次进程启动（见 `_rust_startup` 的说明）
                t = max(t - repeats * startup, 1e-9)
                # ⚠️ **净耗时还没「启动成本」大 → 这一格不可信**：上面这次减法
                # 会把启动成本的**估计误差**放大到净耗时上。
                # R6 主体·整数循环时真踩到：「整数循环累加」显示 55ms，而同一
                # 逻辑把 n 放大到 500000 实测只有 4.2ms / 20000 次 —— 差了 3 倍。
                # 判据：净耗时 < 启动成本 ÷ 3 就打 `⚠`（比值越小越不可信）。
                rust_weak = t < repeats * startup / 3
            times[label] = t
        base = times["树遍历"]
        cells = []
        ratios = []
        for label, _ in executors:
            t = times[label]
            cells.append(f"{t * 1000:>8.1f}ms")
            ratios.append(base / t if t > 0 else 0.0)
        print(f"{name:<10} ×{repeats:<3} " + " ".join(cells)
              + ("   ⚠ Rust 列净耗时太小，别用它下判断" if rust_weak else ""))
        speed_rows.append((name, ratios))
        repeats_hint = repeats

    print()
    print("相对树遍历的提速倍数（>1 = 更快）：")
    print(f"{'场景':<10}  " + "".join(f"{name:>10} " for name, _ in executors))
    print("-" * 46)
    for name, ratios in speed_rows:
        cells = []
        for r in ratios:
            cells.append(f"{r:>9.1f}x " if r >= 1 else f"{r:>9.2f}x ")
        print(f"{name:<10}  " + " ".join(cells))

    if has_rust:
        print(f"\n（Rust VM 那一列 = 实测总时长 − {repeats_hint}×启动成本；"
              f"启动成本 {startup * 1000:.1f} ms/轮）")

    print("\n已知架构边界（透明记录，不是待修缺陷）：")
    print("  ① 文本下标 / 切片：必须回调宿主——C 侧看不见 Python 的切片协议，")
    print("     也没法自己去执行它。每次下标/切片要「参数进去、结果出来」两趟转换，")
    print("     而树遍历直接调 Python 的 C 级 str.__getitem__/切片对象。")
    print("     所以这两项 C VM 追不上树遍历；M47 把文本改成 C 侧原生字符串后，")
    print("     过路字符串的转换成本比原来的 HOST 句柄高约 1.2µs，两项各退约 8%")
    print("     （0.64x→0.59x / 0.41x→0.38x）——这是「字符串统一由 C 侧持有」")
    print("     换来的代价，换来的是拼接 8.4x、比较 11.6x 的提速。")
    print("  ② 文本列表遍历 / 遍历文本：循环体里每元素至少一次宿主回调")
    print("     （`长度`、取字符等），每次回调固定约 2.4µs。M47 已把文本迭代的")
    print("     元素构造改成一次 FFI 建整批（`jsvm_str_new_batch`），")
    print("     再往上要动「内建函数也走 C 侧」那一层。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
