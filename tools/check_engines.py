# -*- coding: utf-8 -*-
"""执行器体检：**全部语料**在四个执行器上跑出来逐字节相同。

四个执行器 = 树遍历（语义基准）/ Python VM / C VM / **Rust VM**（R6 起）。

为什么需要它（这是本项目第 1 条硬约束的日常闸门）：

- `tools/conformance.py` 比的是**前端产物**（tokens / ast / bytecode / diagnostics）
  —— 产物一样**不等于**跑起来一样。R5.1 抓到的「`BUILD_CLASS` 撞掉帧栈基址」
  就是**产物完全一致、VM 独自跑错**（`conformance.py check` 全程是绿的）。
- `tests/test_cvm.py` 已经在对拍，但它的语料是写死的一批；这个工具**扫全仓库**，
  连 `tests/error_cases/*.jsh` 也跑（**报错文本也是可观察行为** ——
  调用链丢失那个 bug 就只在报错文本里看得出来）。

判据：**树遍历的 (stdout, 报错文本) == 各 VM 的** —— **两段都比**。
树遍历是语义基准（默认执行器），它对了才算对。

⚠️ **「报错前的 stdout」也是一段**（R6.2 起）：以前只在没出错时比 stdout，
于是「`打印` 几行之后崩掉」这个形状比不出来 —— 而 Rust VM 的缺口 ③
（报错时一个字都不吐）恰恰只在那个形状下可见。**判据变了，才看得见东西。**

## Rust VM（第四个执行器）的两条注意事项

1. 它是**独立进程**（`rust/target/release/jishi-rs`），吃的是**字节码 JSON** ——
   正好接上 R5 的产物：`JISHI_FRONTEND=rust` 编出 JSON → 交给它跑，
   **全程不经过 Python 对象**。这是 R6/R8 要的那条链。
2. ✅ **它的「报错渲染」从 R6.3 起与 Python 的 `render()` 同形**（源码行 /
   `^` 下划线 / 调用链都有）—— 宿主**按 `filename` 在出错那一刻读一次源文件**
   就有了，不必把整份源码塞进字节码。所以本工具**两段都比**（stdout + 报错渲染），
   和另外三个执行器一视同仁。

用法：
    python tools/check_engines.py            # 全量（含 Rust VM，有就自动带上）
    python tools/check_engines.py -v         # 每个语料都报一行
    python tools/check_engines.py --frontend rust   # 换前端编（默认 python）
    python tools/check_engines.py --no-rust-vm      # 明确不要 Rust VM
"""

from __future__ import annotations

import argparse
import glob
import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jishi import cvm_bind                                        # noqa: E402
from jishi import tokenizer as T                                  # noqa: E402
from jishi.compiler import compile_source, compile_to_json        # noqa: E402
from jishi.errors import JishiError                               # noqa: E402
from jishi.interpreter import run_source as run_tree              # noqa: E402
from jishi.vm import VM                                           # noqa: E402

#: 语料来源（与 `conformance.py` 的 `CORPUS_SOURCES` 同源，但**含 error_cases**）
SOURCES = (
    "tests/cases/*.jsh",
    "tests/error_cases/*.jsh",
    "tests/frontend_cases/*.jsh",
    "examples/**/*.jsh",
    "jishi/stdlib-jishi/*.jsh",
)

#: **豁免**：这些语料**本来就允许**三个执行器不一致，每条都要能复核。
#: （照本项目的规矩：豁免必须能复核 —— 说得出「为什么它不一样是对的」。）
EXEMPT: dict[str, str] = {
    "tests/frontend_cases/01_超范围整数.jsh":
        "故意的：Python 整数任意精度、C VM 是 64 位。见 conformance.py 里 "
        "`frontend` 类别的说明（这类语料**只给前端看**，不参与运行期对拍）",
    "examples/projects/排序跑分/排序.jsh":
        "打印的是**墙上时间**（毫秒），三次跑天然不同 —— 噪声，不是语义差异",
}


#: ⚠️ **Rust VM（第四个执行器）的已知缺口登记** —— R6.0 摸出来的，键是语料路径。
#:
#: 为什么逐个登记、而不是「凡是报错的语料都不比」：那样会把**将来新出现的问题**
#: 一起盖住。登记表的好处是**它会过期**：某个语料哪天好了，工具会说「登记过期」，
#: 逼着人来删一行（本项目的老规矩：**豁免必须能复核**）。
#: 上面四条共用的原因：**「报在哪一列」没有共同答案**。
_POS_WHY = (
    "**位置口径不同**：Python 侧这类错用的是 **AST 调用节点**的列"
    "（`a.追加()` 报在 `a`，第 2 列），宿主**手里没有 AST**、"
    "只能报**指令**的列（`.追加` 那一步，第 5 列）。"
    "类型 / 消息 / 源码行 / 调用链全都一致，**只差 `^` 落在哪个字符下**。"
    "要对齐只有一条路：让编译器把「调用节点的位置」也发进字节码"
    "（**动格式**）。**待拍板**——在那之前，宿主的位置是「指到这个调用上」，可用但不完全等同。")

RUST_VM_KNOWN_GAPS: dict[str, str] = {
    "examples/03_调python生态.jsh":
        "**有意的边界**：这个示例演示「导入 Python 生态（pandas 等）」，"
        "而 Rust 引擎明确不支持 `从 python/本地包` 导入 —— 单二进制里没有 Python。"
        "R 线的既定方向是「用 Rust 原生标准库替掉对 Python 生态的依赖」。",
    "examples/11_Python桥接演示.jsh":
        "同 03：演示的是 Python 桥接，Rust 引擎有意不支持。",
    # -- R6.3：宿主报错渲染对齐之后剩下的三类（都记着「为什么」）-------------
    "tests/error_cases/13_从python导入.jsh":
        "**有意的边界**（同 03 / 11）：这个语料考的正是 `从 python 导入`，"
        "而 Rust 引擎明确不支持 —— 单二进制里没有 Python。"
        "它报的是「Rust 引擎不支持…」，这句本身是**给用户看的正确信息**。",
    "tests/error_cases/15_方法参数个数.jsh":
        _POS_WHY,
    "tests/error_cases/16_移除不存在.jsh":
        _POS_WHY,
    "tests/error_cases/17_文本转整数失败.jsh":
        _POS_WHY,
    "tests/error_cases/18_字典弹出缺失键.jsh":
        _POS_WHY,
    "tests/error_cases/35_赋值前读局部.jsh":
        "**提示语不同**：Python 侧这句是 `Code.local_hints` 里的**按槽位**的具体说法"
        "（「外层也有一个叫「全局」的变量…」），而 `local_hints` "
        "**不在字节码 JSON 里**（见 `docs/前端契约.md` §12.3）—— "
        "宿主手里只有通用说法。要一致就得把它写进字节码格式（**动格式 = 版本号 + 重冻**），"
        "**待拍板**。"
        "⚠️ 类型与消息本身是一致的，只差这一句提示。",
}

# ⚠️ `examples/12_异常处理.jsh` 原来在这里登记过（缺口②③），**R6.2 已修好**
# 并被这条登记机制**报出来**（"登记过期"）→ 按规矩删掉那一行。
# 这就是「登记必须能过期」的意义：修好了不会静悄悄留着，它会逼着人来删。


def _registered(rel: str) -> bool:
    return rel in RUST_VM_KNOWN_GAPS


def _is_exempt(rel: str) -> bool:
    if rel in EXEMPT:
        return True
    # 计时类：文件名里带「跑分 / 基准 / bench」的一律当噪声
    name = Path(rel).name
    return any(k in rel for k in ("跑分", "基准")) or "bench" in name


def _rust_exe() -> Path:
    """Rust 宿主二进制（按平台取名 —— 与 `tests/conftest.py::rust_exe_path` 一致）。"""
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


def _run_rust_vm(src: str, rel: str) -> tuple[str, str]:
    """Rust VM 跑一遍 → `(stdout, 报错文本)`。

    ⚠️ 编译用**当前前端**（`compile_to_json`），所以 `--frontend rust` 时
    这条链是全程原生的：Rust 前端产字节码 JSON → Rust VM 消费。
    """
    body = compile_to_json(src, rel)
    tmp = Path(tempfile.gettempdir()) / f"_check_engines_{os.getpid()}.json"
    tmp.write_text(body, encoding="utf-8")
    r = subprocess.run([str(_rust_exe()), str(tmp)],
                       capture_output=True, input=b"")
    return (r.stdout.decode("utf-8", errors="replace"),
            r.stderr.decode("utf-8", errors="replace"))


def _run(fn, lines: "list[str] | None" = None) -> tuple[str, str]:
    """跑一次 → `(stdout, 报错文本)`。

    ⚠️ **报错前的 stdout 也要留着**（R6.2 改）：以前这里是「出错就只返回报错
    文本、把 buf 丢掉」，于是「`打印` 几行之后崩掉」这个形状**根本比不出来**
    —— 而 Rust VM 的缺口 ③（报错时一个字都不吐）恰恰只在那种形状下可见。
    **判据变了，能看见的东西才跟着变。**

    ⚠️ **报错文本按「用户看到的样子」渲染**（R6.3 改）：先 `with_source(源码行)`
    再 `render()`，即 CLI 上那块带源码行与 `^` 的方框。以前这里只有 `str(e)`
    （没有源码行），于是**宿主的报错渲染根本没法比** —— 只能单独数个数。
    **要比，就拿用户真正看到的那一份来比。**
    """
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            fn()
        return buf.getvalue(), ""
    except BaseException as e:                                     # noqa: BLE001
        if isinstance(e, JishiError):
            if lines is not None:
                e.with_source(list(lines))
            return buf.getvalue(), e.render()
        return buf.getvalue(), f"「{type(e).__name__}」{e}"


def _corpora() -> list[str]:
    out: list[str] = []
    for pat in SOURCES:
        out += sorted(glob.glob(str(ROOT / pat), recursive=True))
    return out


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="三执行器全语料体检")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="每个语料都打印一行")
    ap.add_argument("--frontend", choices=["python", "rust"], default="python",
                    help="用哪个前端编译（默认 python）")
    ap.add_argument("--no-rust-vm", action="store_true",
                    help="不把 Rust VM（独立二进制）算作第四个执行器")
    args = ap.parse_args(argv)

    use_rust_vm = not args.no_rust_vm
    if use_rust_vm and not _rust_exe().exists():
        print(f"（注：{_rust_exe().name} 不存在，本次只比三个执行器；"
              f"要带上它先 `cd rust && cargo build --release`）")
        use_rust_vm = False
    if args.frontend == "rust" and not T.rust_available():
        print("❌ 设了 --frontend rust，但扩展没构建（跑 python core/build.py）",
              file=sys.stderr)
        return 2
    os.environ[T.FRONTEND_ENV] = args.frontend

    # ⚠️ 钉死 stdin：有示例会 `输入()`，不钉就会挂在终端上读输入
    sys.stdin = io.StringIO("")

    files = _corpora()
    stale: list[str] = []                   # 登记过期：登记说「有缺口」，实际已经好了
    bad: list[tuple] = []
    exempted: list[str] = []
    rust_stdout_bad: list[tuple] = []
    rust_registered: list[str] = []         # 命中已知缺口登记（不是失败）
    rust_render_gap = 0                     # 报错渲染不一致的次数（已登记的缺口）
    for path in files:
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        if _is_exempt(rel):
            exempted.append(rel)
            continue
        src = Path(path).read_text(encoding="utf-8")
        lines = src.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        tree_out, tree_err = _run(lambda: run_tree(src, rel), lines)
        vm_out, vm_err = _run(lambda: VM(compile_source(src, rel), rel).run(),
                              lines)
        cvm_out, cvm_err = _run(lambda: cvm_bind.run_source_c(src, rel), lines)
        # 两段都比：**报错前的输出**与**报错文本**都是可观察行为
        ok = (tree_out, tree_err) == (vm_out, vm_err) == (cvm_out, cvm_err)
        if use_rust_vm:
            # ⚠️ `compile_to_json` 对**编译不过**的语料会抛（`error_cases` 里一堆）
            # —— 那种语料根本没字节码，Rust VM 无从跑起，直接跳过（不是不一致）。
            try:
                r_out, r_err = _run_rust_vm(src, rel)
            except Exception:                                  # noqa: BLE001
                r_out = r_err = None
            if r_out is not None:
                # R6.3 起**两段都比**：stdout 与**报错渲染**（宿主自己渲染的
                # 那份，与 Python 的 `render()` 同形）。
                if r_out != tree_out or (tree_err and r_err.rstrip("\n") != tree_err):
                    if _registered(rel):
                        rust_registered.append(rel)
                    else:
                        rust_stdout_bad.append((rel, tree_out, r_out, tree_err, r_err))
                        ok = False
                elif _registered(rel):
                    stale.append(rel)
                if tree_err and r_err.strip() and r_err.rstrip("\n") != tree_err:
                    rust_render_gap += 1
        if args.verbose:
            print(("✅ " if ok else "❌ ") + rel)
        if not ok and not any(b[0] == rel for b in bad):
            bad.append((rel, tree_out, tree_err, vm_out, vm_err, cvm_out, cvm_err))

    total = len(files)
    print(f"\n语料 {total} 个 · 对拍 {total - len(exempted)} 个 · "
          f"豁免 {len(exempted)} 个 · **不一致 {len(bad)} 个**"
          f"（前端：{args.frontend}"
          f"{'，含 Rust VM' if use_rust_vm else ''}）")
    if use_rust_vm:
        print(f"  · Rust VM 不一致（stdout 或报错渲染）：**{len(rust_stdout_bad)}** 个")
        print(f"  · Rust VM **报错渲染**不一致：{rust_render_gap} 个"
              f"（R6.3 前这一项是「已登记的缺口」，现在是**真判据**）")
        print(f"  · 命中「Rust VM 已知缺口」登记：**{len(rust_registered)}** 个"
              f"（共登记 {len(RUST_VM_KNOWN_GAPS)} 条）")
        for rel in rust_registered[:6]:
            print(f"      · {rel} —— {RUST_VM_KNOWN_GAPS[rel]}")
        if len(rust_registered) > 6:
            print(f"      · …另 {len(rust_registered) - 6} 个")
        if stale:
            print(f"  ⚠️ **登记过期**（这些已经好了，去 `RUST_VM_KNOWN_GAPS` 里删掉）：")
            for rel in stale:
                print(f"      · {rel}")
    for rel, *outs in bad[:8]:
        print(f"\n❌ {rel}")
        for nm, o in zip(("树遍历.stdout", "树遍历.报错", "PythonVM.stdout",
                          "PythonVM.报错", "CVM.stdout", "CVM.报错"), outs):
            print(f"   {nm}: {o[:220]!r}")
    for item in rust_stdout_bad[:4]:
        rel, a, b = item[0], item[1], item[2]
        print(f"\n❌ Rust VM：{rel}\n   stdout 树遍历: {a[:160]!r}"
              f"\n   stdout Rust: {b[:160]!r}")
        if len(item) > 4:
            print(f"   报错 树遍历: {item[3][:240]!r}\n   报错 Rust: {item[4][:240]!r}")
    if exempted and args.verbose:
        print("\n（豁免）")
        for rel in exempted:
            print(f"   · {rel} —— {EXEMPT.get(rel, '计时类噪声（文件名带「跑分/基准」）')}")
    if stale:
        # 登记过期也当失败：留着它就等于给将来的问题留了张通行证
        return 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
