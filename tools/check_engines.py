# -*- coding: utf-8 -*-
"""执行器体检：**全部语料**在五个执行器上跑出来逐字节相同。

五个执行器 = 树遍历（语义基准）/ Python VM / C VM / **Rust VM**（R6 起）
/ **Node 宿主**（R6.4 起）。

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

## 两个跨语言宿主（Rust VM / Node 宿主）的注意事项

### Node 宿主（R6.4 加入）

它和 Rust VM 是同一类：**独立进程、吃字节码 JSON**。以前它不在这个工具的视野里
—— 于是它那些问题（越界赋值**静默成功**、报错渲染只有一行、类型名笼统成「异常」）
**一个都没被抓到**，只有 `tests/test_m13_node.py` 那批手写用例覆盖到的地方才有人管。
📌 **把一份实现拉进判据，本身就是一次升级**：拉进来的当天就冒出 11 个不一致
（见 `tools/probe_runtime_errors.py` 的 R6.4 小节）。

### Rust VM

1. 它是**独立进程**（`rust/target/release/jishi-rs`），吃的是**字节码 JSON** ——
   正好接上 R5 的产物：`JISHI_FRONTEND=rust` 编出 JSON → 交给它跑，
   **全程不经过 Python 对象**。这是 R6/R8 要的那条链。
2. ✅ **它的「报错渲染」从 R6.3 起与 Python 的 `render()` 同形**（源码行 /
   `^` 下划线 / 调用链都有）—— 宿主**按 `filename` 在出错那一刻读一次源文件**
   就有了，不必把整份源码塞进字节码。所以本工具**两段都比**（stdout + 报错渲染），
   和另外三个执行器一视同仁。

用法：
    python tools/check_engines.py            # 全量（含 Rust VM / Node，有就自动带上）
    python tools/check_engines.py -v         # 每个语料都报一行
    python tools/check_engines.py --frontend rust   # 换前端编（默认 python）
    python tools/check_engines.py --no-rust-vm      # 明确不要 Rust VM
    python tools/check_engines.py --no-node         # 明确不要 Node 宿主
"""

from __future__ import annotations

import argparse
import glob
import io
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oracle.jishi import cvm_bind                                        # noqa: E402
from oracle.jishi import tokenizer as T                                  # noqa: E402
from oracle.jishi.compiler import compile_source, compile_to_json        # noqa: E402
from oracle.jishi.errors import JishiError                               # noqa: E402
from oracle.jishi.interpreter import run_source as run_tree              # noqa: E402
from oracle.jishi.vm import VM                                           # noqa: E402

#: 语料来源（与 `conformance.py` 的 `CORPUS_SOURCES` 同源，但**含 error_cases**）
SOURCES = (
    "tests/cases/*.jsh",
    "tests/error_cases/*.jsh",
    "tests/frontend_cases/*.jsh",
    "examples/**/*.jsh",
    "oracle/jishi/stdlib-jishi/*.jsh",
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


#: ⚠️ **跨语言宿主（Rust VM / Node 宿主）共用的已知缺口登记**，键是语料路径。
#:
#: 📌 **为什么合成一张**（R6.4）：R6.0 摸 Rust VM 的缺口、R6.4 把 Node 也拉进判据，
#: 两边剩下的**完全同构** —— 同一批语料、同一个原因（宿主手里没有 AST、没有
#: `local_hints`、单二进制里没有 Python）。**两张表迟早会漂**，而它们说的本是
#: 同一件事。以后哪个宿主先修好，这条登记会在**两边都对上之后**才报「过期」。
#:
#: 为什么逐个登记、而不是「凡是报错的语料都不比」：那样会把**将来新出现的问题**
#: 一起盖住。登记表的好处是**它会过期**：某个语料哪天好了，工具会说「登记过期」，
#: 逼着人来删一行（本项目的老规矩：**豁免必须能复核**）。
#: 📌 **现在只剩 3 条，而且全是「有意边界」** —— 宿主是单二进制、里面没有 Python，
#: 所以 `从 python / 本地包 导入` 一律不支持。那不是缺口，是**设计**。
HOST_KNOWN_GAPS: dict[str, str] = {
    "examples/03_调python生态.jsh":
        "**有意的边界**：这个示例演示「导入 Python 生态（pandas 等）」，"
        "而跨语言宿主明确不支持 `从 python/本地包` 导入 —— 单二进制里没有 Python。"
        "R 线的既定方向是「用宿主原生标准库替掉对 Python 生态的依赖」。",
    "examples/11_Python桥接演示.jsh":
        "同 03：演示的是 Python 桥接，跨语言宿主有意不支持。",
    # -- R6.3/R6.4：宿主报错渲染对齐之后剩下的三类（都记着「为什么」）---------
    "tests/error_cases/13_从python导入.jsh":
        "**有意的边界**（同 03 / 11）：这个语料考的正是 `从 python 导入`，"
        "而跨语言宿主明确不支持 —— 单二进制里没有 Python。"
        "它报的是「宿主不支持…」，这句本身是**给用户看的正确信息**。",
}

# ✅ **R6.6 把上面 5 条全收掉了**（用户拍板：动字节码格式）——
#   * 4 条「`^` 落在哪一列」：宿主现在用**方法值携带的位置**（取属性时记下的
#     `X.方法` 那一处），与 Python 的 `_BoundMethod.__call__` 同源；
#   * 1 条 `local_hints`：字节码升到 **v3**，`Code` 多一个 `local_hints` 字段。
# 它们被这条登记机制**报出来**（「登记过期」）→ 按规矩删掉。
# 这就是「登记必须能过期」的意义：修好了不会静悄悄留着，它会逼着人来删。

# ⚠️ `examples/12_异常处理.jsh` 原来在这里登记过（缺口②③），**R6.2 已修好**
# 并被这条登记机制**报出来**（"登记过期"）→ 按规矩删掉那一行。
# 这就是「登记必须能过期」的意义：修好了不会静悄悄留着，它会逼着人来删。


def _registered(rel: str) -> bool:
    return rel in HOST_KNOWN_GAPS


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


@contextmanager
def _in_dir(d: Path):
    """临时把进程 cwd 切到 `d`（给**进程内**的 Python 三个执行器用）。

    ⚠️ 教程类语料会在 cwd 里读写真实文件（`账本.csv` / `成绩.csv` / `x.txt`）——
    不隔离就会在**仓库根**留产物（打断一次就污染下一轮）。切之前存、出来还原。
    """
    old = os.getcwd()
    os.chdir(str(d))
    try:
        yield
    finally:
        os.chdir(old)


def _run_rust_vm(src: str, rel: str, cwd: "Path | None" = None) -> tuple[str, str]:
    """Rust VM 跑一遍 → `(stdout, 报错文本)`。

    ⚠️ 编译用**当前前端**（`compile_to_json`），所以 `--frontend rust` 时
    这条链是全程原生的：Rust 前端产字节码 JSON → Rust VM 消费。

    ⚠️ `cwd` 是**该语料的私有运行目录**：宿主按 `filename`（= `rel`）现读源文件
    渲染报错，所以私有目录里必须**按同名相对路径**放着源文件（调用方负责复制）。
    """
    body = compile_to_json(src, rel)
    tmp = Path(tempfile.gettempdir()) / f"_check_engines_{os.getpid()}.json"
    tmp.write_text(body, encoding="utf-8")
    r = subprocess.run([str(_rust_exe()), "--load-bytecode", str(tmp)],
                       capture_output=True, input=b"",
                       cwd=str(cwd) if cwd else None)
    return (r.stdout.decode("utf-8", errors="replace"),
            r.stderr.decode("utf-8", errors="replace"))


def _node_exe() -> "str | None":
    """Node 可执行文件（PATH 上没有就返回 None）。"""
    return shutil.which("node")


def _node_index() -> Path:
    return ROOT / "oracle" / "node" / "index.js"


def _run_node_vm(src: str, rel: str, cwd: "Path | None" = None) -> tuple[str, str]:
    """Node 宿主跑一遍 → `(stdout, 报错文本)`。

    与 Rust VM 同一形状：独立进程、吃**字节码 JSON**（当前前端编的）。
    ⚠️ Node 的报错渲染包含**源码行**（它按 `filename` 现读源文件），
    所以给的 `rel` 必须是**相对该语料私有 cwd 的真路径** —— 私有目录里存着源文件。
    """
    body = compile_to_json(src, rel)
    tmp = Path(tempfile.gettempdir()) / f"_check_engines_node_{os.getpid()}.json"
    tmp.write_text(body, encoding="utf-8")
    r = subprocess.run([_node_exe(), str(_node_index()), str(tmp)],
                       capture_output=True, input=b"",
                       cwd=str(cwd) if cwd else None)
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
    ap.add_argument("--no-node", action="store_true",
                    help="不把 Node 宿主算作第五个执行器")
    args = ap.parse_args(argv)

    use_node = not args.no_node and _node_exe() is not None
    if not args.no_node and not use_node:
        print("（注：PATH 上没有 node，本次不带 Node 宿主）")

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
    #: 各语料的私有运行目录根（跑完全清）—— 文件名带 pid：本工具也被 pytest 调用，
    #: pytest-xdist 并行时多个 worker 同时跑，固定名会互相覆盖。
    run_root = Path(tempfile.gettempdir()) / f"{os.getpid()}_check_engines_cwd"
    shutil.rmtree(run_root, ignore_errors=True)
    bad: list[tuple] = []
    exempted: list[str] = []
    rust_stdout_bad: list[tuple] = []
    rust_render_gap = 0                     # 报错渲染不一致的次数（已登记的缺口）
    node_bad: list[tuple] = []              # Node 宿主不一致（未登记的）
    #: 命中登记：语料 → 还差哪个宿主没对齐（登记「过期」要两边都好了才算）
    registered: dict[str, set[str]] = {}
    node_render_gap = 0                     # Node 报错渲染不一致的次数
    for path in files:
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        if _is_exempt(rel):
            exempted.append(rel)
            continue
        src = Path(path).read_text(encoding="utf-8")
        lines = src.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        # ⚠️ **每个语料一个私有 cwd**（与 `check_walk.py` 同法，2026-10-09 补）：
        #    教程类语料会在 cwd 里读写真实文件（`账本.csv` / `成绩.csv` / `x.txt`），
        #    跑在仓库根就会留产物、**打断一次就污染下一轮**（第一台引擎读到翻倍数据 → 假红）。
        #    源文件**按同名相对路径**复制进私有目录，五个执行器都在那里跑 ——
        #    报错渲染里的文件名（`rel`）不变，文件 I/O 互相隔离。
        run_dir = run_root / rel.replace("/", "__")
        run_dir.mkdir(parents=True, exist_ok=True)
        dst = run_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)
        with _in_dir(run_dir):
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
                r_out, r_err = _run_rust_vm(src, rel, run_dir)
            except Exception:                                  # noqa: BLE001
                r_out = r_err = None
            if r_out is not None:
                # R6.3 起**两段都比**：stdout 与**报错渲染**（宿主自己渲染的
                # 那份，与 Python 的 `render()` 同形）。
                if r_out != tree_out or (tree_err and r_err.rstrip("\n") != tree_err):
                    if _registered(rel):
                        # 命中登记：**不是失败**（原因写在 `HOST_KNOWN_GAPS` 里，
                        # 每条都能复核），但记下「这个宿主还没对齐」——
                        # 两个宿主都对齐了才叫「登记过期」。
                        registered.setdefault(rel, set()).add("Rust VM")
                    else:
                        rust_stdout_bad.append((rel, tree_out, r_out, tree_err, r_err))
                        ok = False
                if tree_err and r_err.strip() and r_err.rstrip("\n") != tree_err:
                    rust_render_gap += 1
        if use_node:
            # ⚠️ 与 Rust VM 同样：编译不过的语料（`error_cases` 里一堆）没有字节码，
            # 宿主无从跑起，直接跳过（**不是**不一致）。
            try:
                n_out, n_err = _run_node_vm(src, rel, run_dir)
            except Exception:                                      # noqa: BLE001
                n_out = n_err = None
            if n_out is not None:
                # 两段都比：stdout 与**报错渲染**（Node 自己渲染那份，与 Python 同形）
                if n_out != tree_out or (tree_err and n_err.rstrip("\n") != tree_err):
                    if _registered(rel):
                        registered.setdefault(rel, set()).add("Node")
                    else:
                        node_bad.append((rel, tree_out, n_out, tree_err, n_err))
                        ok = False
                if tree_err and n_err.strip() and n_err.rstrip("\n") != tree_err:
                    node_render_gap += 1
        if args.verbose:
            print(("✅ " if ok else "❌ ") + rel)
        if not ok and not any(b[0] == rel for b in bad):
            bad.append((rel, tree_out, tree_err, vm_out, vm_err, cvm_out, cvm_err))

    total = len(files)
    also = []
    if use_rust_vm:
        also.append("Rust VM")
    if use_node:
        also.append("Node")
    print(f"\n语料 {total} 个 · 对拍 {total - len(exempted)} 个 · "
          f"豁免 {len(exempted)} 个 · **不一致 {len(bad)} 个**"
          f"（前端：{args.frontend}"
          f"{('，含 ' + ' / '.join(also)) if also else ''}）")
    if use_node:
        print(f"  · **Node 宿主**不一致（stdout 或报错渲染）：**{len(node_bad)}** 个"
              f"（R6.4 起进判据）")
        print(f"  · Node 报错渲染不一致：{node_render_gap} 个")
    if use_rust_vm:
        print(f"  · Rust VM 不一致（stdout 或报错渲染）：**{len(rust_stdout_bad)}** 个")
        print(f"  · Rust VM **报错渲染**不一致：{rust_render_gap} 个"
              f"（R6.3 前这一项是「已登记的缺口」，现在是**真判据**）")
    if use_rust_vm or use_node:
        hosts = ([h for h in ("Rust VM", "Node") if (h == "Rust VM") == use_rust_vm
                  or (h == "Node") == use_node])
        print(f"  · 命中「宿主已知缺口」登记：**{len(registered)}** 个"
              f"（共登记 {len(HOST_KNOWN_GAPS)} 条）")
        for rel, missing in list(registered.items())[:6]:
            print(f"      · {rel} —— {HOST_KNOWN_GAPS[rel]}")
        if len(registered) > 6:
            print(f"      · …另 {len(registered) - 6} 个")
        stale = [r for r in HOST_KNOWN_GAPS if r not in registered]
        if stale:
            print("  ⚠️ **登记过期**（所有宿主都对齐了，去 `HOST_KNOWN_GAPS` 里删掉）：")
            for rel in stale:
                print(f"      · {rel}")
    for rel, *outs in bad[:8]:
        print(f"\n❌ {rel}")
        for nm, o in zip(("树遍历.stdout", "树遍历.报错", "PythonVM.stdout",
                          "PythonVM.报错", "CVM.stdout", "CVM.报错"), outs):
            print(f"   {nm}: {o[:220]!r}")
    for rel, t_out, n_out, t_err, n_err in node_bad:
        print(f"\n❌ Node 宿主：{rel}\n   stdout 树遍历: {t_out[:160]!r}"
              f"\n   stdout Node  : {n_out[:160]!r}")
        print(f"   报错 树遍历: {t_err[:240]!r}\n   报错 Node  : {n_err[:240]!r}")
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
    # 清理本轮所有语料的私有运行目录（失败也不留垃圾）—— 「仓库根不留产物」的落点
    shutil.rmtree(run_root, ignore_errors=True)
    # 登记过期也当失败：留着它就等于给将来的问题留了张通行证
    stale = [r for r in HOST_KNOWN_GAPS if r not in registered] if (use_rust_vm or use_node) else []
    if stale:
        return 1
    return 1 if (bad or node_bad or rust_stdout_bad) else 0


if __name__ == "__main__":
    sys.exit(main())
