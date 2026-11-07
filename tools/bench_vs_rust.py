# -*- coding: utf-8 -*-
"""**「vs 原生 Rust」那把尺子**（M179 配菜）：同一道题，一份基石、一份原生 Rust。

## 为什么要有它

`bench_vs_python.py` 回答的是「基石比 CPython 快多少」——那是**及格线**。
但「值不值得做 JIT」问的是另一件事：**离原生代码还差多远**。
没有这条线，M180 的 JIT 做出来「快了 20%」也说不清是「补上了多少缺口」。

路线图 §8.1 的 ③④ 台阶（特化 → JIT）就是靠它判的。

## 口径（与 `bench_vs_python.py` 一致，别自己发明）

- **两份程序输出必须逐字相同**，否则这一行标「不等价」、数字作废
  （本项目的老规矩：先对齐语义，再谈快慢）；
- **扣掉启动**：两个都是独立进程，启动成本单列（基石约 20–25 ms、
  原生 Rust 程序约 1–3 ms）；
- **交替取样** A/B/A/B，每档取最小值 —— 中途一阵负载会把后跑的抬高（M45 的教训）；
- **规模固定**：`--存` 会把「重复次数」写进快照，之后 `--用基线` 按同一规模跑
  （`bench_vs_python.py` 在 M162 踩过：动态规模会让两次跑的根本不是同一道题）。

## 怎么用

    python tools/bench_vs_rust.py                 # 全部场景，每档 3 轮
    python tools/bench_vs_rust.py --轮数 5 --只跑 递归
    python tools/bench_vs_rust.py --存 build/bench_rust.json     # 存基线（含重复次数）
    python tools/bench_vs_rust.py --用基线 build/bench_rust.json # 按基线规模跑

⚠️ **需要 `rustc`**（PATH 里有就行，不需要 cargo）。缺了就整体跳过并说清楚 ——
它是**开发期**工具，不参与构建、不进发行版。

⚠️ 如实说：这是「**同一个算法**」的对比，不是「同样的优化力度」。Rust 那份是最朴素的
写法（没有 SIMD、没有 unsafe、没有专门的 crate），所以它代表的是「一个人随手写出来的
原生代码」，不是「打磨到极致的 Rust」。
"""

from __future__ import annotations

import argparse
import json as _json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# ⚠️ 分平台（M174）：Windows 是 `jishi-rs.exe`，Linux / macOS 是 `jishi-rs`。
EXE = ROOT / "rust" / "target" / "release" / (
    "jishi-rs.exe" if os.name == "nt" else "jishi-rs")
构建目录 = ROOT / "build" / "bench_rust"

#: 场景：(基石源码, 等价的原生 Rust 源码, 单轮建议重复次数)
#:
#: 三条纪律：
#: 1. **输出必须是同一个整数**（浮点、字典顺序都会引入「格式差异」这种假不等价）；
#: 2. Rust 那份用**同一个算法与同一套数据结构**（i128 / Vec / HashMap / String），
#:    不许换更聪明的算法把差异算到「算法」头上；
#: 3. 规模要大到「单轮 ≥400 ms」，否则启动与噪声占主导。
场景 = {
    "整数循环累加": (
        "令 总和 = 0\n"
        "遍历 i 在 范围(2000000)：\n"
        "    总和 = 总和 + i\n"
        "打印(总和)\n",
        "use std::hint::black_box;\n"
        "fn main() {\n"
        "    let mut s: i128 = 0;\n"
        "    // ⚠️ `black_box` 是**必须**的：不加的话 LLVM 会把「0..N 求和」折成\n"
        "    //    一个闭式（实测 19 ms：「差 43.9x」是假的，那边根本没在算）。\n"
        "    for i in 0..black_box(2000000i128) { s += i; }\n"
        "    println!(\"{}\", black_box(s));\n"
        "}\n", 4),

    "递归斐波那契": (
        "函数 斐波(n)：\n"
        "    如果 n < 2：\n"
        "        返回 n\n"
        "    返回 斐波(n - 1) + 斐波(n - 2)\n"
        "打印(斐波(24))\n",
        "use std::hint::black_box;\n"
        "fn fib(n: i128) -> i128 {\n"
        "    if n < 2 { return n; }\n"
        "    fib(n - 1) + fib(n - 2)\n"
        "}\n"
        "fn main() { println!(\"{}\", fib(black_box(24))); }\n", 4),

    "列表构建与遍历": (
        "令 甲 = []\n"
        "遍历 i 在 范围(300000)：\n"
        "    甲.追加(i)\n"
        "令 总和 = 0\n"
        "遍历 v 在 甲：\n"
        "    总和 = 总和 + v\n"
        "打印(总和)\n",
        "fn main() {\n"
        "    let mut v: Vec<i128> = Vec::new();\n"
        "    for i in 0..black_box(300000i128) { v.push(i); }\n"
        "    let mut s: i128 = 0;\n"
        "    for x in &v { s += *x; }\n"
        "    println!(\"{}\", s);\n"
        "}\n", 2),

    "字典写入密集": (
        "令 字 = {}\n"
        "遍历 i 在 范围(200000)：\n"
        "    字[i] = i * 2\n"
        "令 和 = 0\n"
        "遍历 k 在 字：\n"
        "    和 = 和 + 字[k]\n"
        "打印(长度(字))\n"
        "打印(和)\n",
        "use std::collections::HashMap;\n"
        "fn main() {\n"
        "    let mut m: HashMap<i128, i128> = HashMap::new();\n"
        "    for i in 0..black_box(200000i128) { m.insert(i, i * 2); }\n"
        "    let mut s: i128 = 0;\n"
        "    for (_, v) in &m { s += *v; }\n"
        "    println!(\"{}\", m.len());\n"
        "    println!(\"{}\", s);\n"
        "}\n", 2),

    "字典查找密集": (
        "令 字 = {}\n"
        "遍历 i 在 范围(1000)：\n"
        "    字[i] = i * 2\n"
        "令 和 = 0\n"
        "遍历 i 在 范围(400000)：\n"
        "    令 k = i % 1000\n"
        "    和 = 和 + 字[k]\n"
        "打印(和)\n",
        "use std::collections::HashMap;\n"
        "fn main() {\n"
        "    let mut m: HashMap<i128, i128> = HashMap::new();\n"
        "    for i in 0..black_box(1000i128) { m.insert(i, i * 2); }\n"
        "    let mut s: i128 = 0;\n"
        "    for i in 0..black_box(400000i128) { s += m[&(i % 1000)]; }\n"
        "    println!(\"{}\", s);\n"
        "}\n", 3),

    "函数调用密集": (
        "函数 加一(x)：\n"
        "    返回 x + 1\n"
        "令 总和 = 0\n"
        "遍历 i 在 范围(300000)：\n"
        "    总和 = 加一(总和)\n"
        "打印(总和)\n",
        "#[inline(never)]\n"
        "fn add1(x: i128) -> i128 { black_box(x) + 1 }\n"
        "fn main() {\n"
        "    let mut s: i128 = 0;\n"
        "    for _ in 0..black_box(300000i128) { s = add1(s); }\n"
        "    println!(\"{}\", s);\n"
        "}\n", 2),

    "文本遍历": (
        "令 文 = \"一二三四五六七八九十\"\n"
        "令 计 = 0\n"
        "遍历 i 在 范围(20000)：\n"
        "    遍历 字 在 文：\n"
        "        计 = 计 + 1\n"
        "打印(计)\n",
        "fn main() {\n"
        "    let t = \"一二三四五六七八九十\";\n"
        "    let mut n: i128 = 0;\n"
        "    for _ in 0..black_box(20000) { for _c in black_box(t).chars() { n += 1; } }\n"
        "    println!(\"{}\", n);\n"
        "}\n", 2),

    "文本拼接": (
        "令 文 = \"\"\n"
        "遍历 i 在 范围(20000)：\n"
        "    文 = 文 + \"ab\"\n"
        "打印(长度(文))\n",
        "fn main() {\n"
        "    let mut s = String::new();\n"
        "    for _ in 0..black_box(20000) { s.push_str(\"ab\"); }\n"
        "    println!(\"{}\", s.len());\n"
        "}\n", 2),
}


def _跑(cmd, cwd=None, timeout=1800):
    t0 = time.perf_counter()
    r = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                       capture_output=True, timeout=timeout, input=b"")
    return time.perf_counter() - t0, r


def _有rustc():
    return shutil.which("rustc") is not None


def 编一个(名: str, 源: str, 次: int) -> Path:
    """把 Rust 源码编成一个可执行文件（编过就复用，改了源码才重编）。

    🔴 **重复次数要编进去**：基石那边是「源码重复 N 遍」，Rust 这边就得
    「main 里跑 N 遍」—— 否则两边跑的根本不是同一个工作量（第一次写的时候
    忘了这一步，于是「输出不等价：`46368\n46368` vs `46368`」）。
    """
    构建目录.mkdir(parents=True, exist_ok=True)
    rs = 构建目录 / f"{名}_{次}.rs"
    out = 构建目录 / (f"{名}_{次}.exe" if os.name == "nt" else f"{名}_{次}")
    源 = 源.replace("fn main()", "fn 一次()", 1) + \
        f"\nfn main() {{ for _ in 0..{次} {{ 一次(); }} }}\n"
    # `use` 统一在这里补：场景表里每个程序都要 `black_box`，逐个手写一遍容易漏
    # （漏了就是 rustc 的 E0425 —— 判据跑起来才发现）。
    if "black_box" in 源 and "use std::hint::black_box" not in 源:
        源 = "use std::hint::black_box;\n" + 源
    rs.write_text(源, encoding="utf-8", newline="\n")
    if out.is_file() and out.stat().st_mtime >= rs.stat().st_mtime:
        return out
    # `-O` = opt-level 2；不开 LTO、不开 target-cpu=native ——
    # 追求的是「普通人随手 rustc -O」那一档，不是「极致调优」那一档。
    _, r = _跑(["rustc", "-O", "-o", out, rs], timeout=600)
    if not out.is_file():
        raise SystemExit(
            f"rustc 编不出来：{名}\n{(r.stdout + r.stderr).decode('utf-8', 'replace')[:600]}")
    return out


def 量启动(d: Path):
    (d / "空.jsh").write_text("", encoding="utf-8")
    return min(_跑([EXE, "空.jsh"], d)[0] for _ in range(5))


def 定重复次数(名, 建议, 指定=None):
    """重复次数**固定**（不用动态标定）。

    为什么不动态：`bench_vs_python.py` 的动态标定会随机器瞬时负载变，于是
    「同一份代码两次跑」可能是**不同规模**的程序（M162 的教训）。这里干脆固定，
    规模写在场景表里；要换规模就改表，或者用 `--用基线` 沿用快照里的数字。
    """
    return 指定 if 指定 else 建议


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="基石 vs 原生 Rust（命令行程序）")
    ap.add_argument("--轮数", type=int, default=3)
    ap.add_argument("--只跑", default="")
    ap.add_argument("--存", default="", help="把结果（含重复次数）写成 JSON")
    ap.add_argument("--用基线", default="", help="按这份基线里的重复次数跑")
    a = ap.parse_args(argv)

    if not EXE.is_file():
        print(f"❌ 先构建宿主：cd rust && cargo build --release（缺 {EXE}）")
        return 1
    if not _有rustc():
        print("⚠️ 没找到 `rustc` —— 这把尺子量不了（它要现编对照程序）。")
        print("   装 Rust 工具链（rustup）之后再来；或者跳过，本工具不参与构建。")
        return 0

    固定 = {}
    if a.用基线:
        基 = _json.loads(Path(a.用基线).read_text(encoding="utf-8"))
        固定 = {r["名字"]: r["重复"] for r in 基.get("场景", []) if r.get("重复")}
        print(f"按基线重复次数跑（{Path(a.用基线).name}）")

    d = Path(tempfile.mkdtemp(prefix="jishi-vs-rust-"))
    启 = 量启动(d)
    print(f"启动成本（5 次取最优）：基石 {启*1000:.0f} ms")
    print()
    print(f"{'场景':<14}{'重复':>5}{'基石(ms)':>10}{'Rust(ms)':>10}{'差几倍':>8}   备注")
    print("-" * 62)

    结果 = []
    选 = {k: v for k, v in 场景.items() if (not a.只跑 or a.只跑 in k)}
    for 名, (jsh, rs源, 建议) in 选.items():
        次 = 定重复次数(名, 建议, 固定.get(名))
        可执行 = 编一个(名, rs源, 次)
        (d / f"{名}.jsh").write_text(jsh * 次, encoding="utf-8", newline="\n")

        # 先对齐语义（输出必须逐字相同，不然比的不是同一件事）
        _, r1 = _跑([EXE, f"{名}.jsh"], d)
        _, r2 = _跑([可执行], d)
        o1 = (r1.stdout + r1.stderr).decode("utf-8", "replace").replace("\r\n", "\n").strip()
        o2 = (r2.stdout + r2.stderr).decode("utf-8", "replace").replace("\r\n", "\n").strip()
        if o1 != o2:
            结果.append({"名字": 名, "重复": 次, "不等价": True})
            print(f"{名:<14}{次:>5}{'—':>10}{'—':>10}{'—':>8}   输出不等价："
                  f"{o1[:18]!r} vs {o2[:18]!r}")
            continue

        甲, 乙 = [], []
        for _ in range(a.轮数):
            甲.append(_跑([EXE, f"{名}.jsh"], d)[0])
            乙.append(_跑([可执行], d)[0])
        m甲, m乙 = min(甲), min(乙)
        倍 = m甲 / m乙 if m乙 > 0 else float("inf")
        结果.append({"名字": 名, "重复": 次, "基石ms": round(m甲 * 1000, 1),
                     "rust_ms": round(m乙 * 1000, 1), "差几倍": round(倍, 2)})
        print(f"{名:<14}{次:>5}{m甲*1000:>10.1f}{m乙*1000:>10.1f}{倍:>7.1f}x")

    print("-" * 62)
    有效 = [r for r in 结果 if r.get("差几倍")]
    if 有效:
        几 = sum(r["差几倍"] for r in 有效) / len(有效)
        几何 = 1.0
        for r in 有效:
            几何 *= max(r["差几倍"], 1e-9)
        几何 **= (1.0 / len(有效))
        print(f"共 {len(有效)} 个场景 · 算术平均 **{几:.1f}x** · 几何平均 **{几何:.1f}x**")
        print("（「差几倍」= 基石耗时 ÷ Rust 耗时；**越大越慢**。这是 JIT 要补的缺口。）")
    print("\n⚠️ Rust 那份是**朴素写法**（无 SIMD / 无 unsafe / 无第三方 crate），"
          "代表「随手写的原生代码」，不是「打磨到极致的 Rust」。")

    if a.存:
        快照 = {
            "_说明": "bench_vs_rust 的基线：含每个场景的重复次数，供 --用基线 复跑",
            "场景": 结果,
        }
        Path(a.存).parent.mkdir(parents=True, exist_ok=True)
        Path(a.存).write_text(_json.dumps(快照, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
        print(f"\n已写入：{a.存}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
