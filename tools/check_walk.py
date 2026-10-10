# -*- coding: utf-8 -*-
"""**树遍历参考执行器 vs 字节码 VM** —— 两个独立实现的互拍（R6 主体）。

判据：同一段源码，两边打印出来的必须一样。差异 = **编译器 / 字节码生成**的问题
（字节码 VM 自己抓不出来）。

输出分三类：

* **一致** —— 两边一样 ✓
* **还没实现** —— 树遍历明确报「还没实现「X」」（**不静默给错值**）；
  这里按未实现的**节点/功能**归类，给出「下一步该补什么」的清单。
  **✅ 现在是 0**（第 ⑤ 步之后，`tests/cases/` 里出现过的节点类型全部实现了）；
  这个数一旦不是 0，说明语料用了还没接上的节点。
* **不一致** —— 两边都跑完了但结果不同 ✗ **这才是要立刻查的**。

数据来源：`tests/conformance/baseline/`（已冻结的 ast 与 bytecode 产物，
不用重新编译，快）。

用法：
    python tools/check_walk.py            # 全量
    python tools/check_walk.py -v         # 每个语料一行
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests" / "conformance" / "baseline"
UNIMPL = re.compile(r"还没实现「([^」]+)」")


def _exe() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


def main(argv: "list[str] | None" = None) -> int:
    verbose = "-v" in (argv if argv is not None else sys.argv[1:])
    exe = _exe()
    if not exe.exists():
        print(f"❌ 先构建宿主：cd rust && cargo build --release", file=sys.stderr)
        return 2

    # ⚠️ 文件名带 pid：本工具也被 pytest 调用（test_m76–m81 的进度仪表测试），
    # pytest-xdist 并行时多个 worker 同时跑本工具，固定名会互相覆盖。
    tmp = Path(tempfile.gettempdir()) / f"{os.getpid()}_check_walk.json"
    # 各语料的私有运行目录（跑完整个清理）
    run_root = Path(tempfile.gettempdir()) / f"{os.getpid()}_check_walk_cwd"
    same = 0
    todo: Counter[str] = Counter()
    bad: list[tuple[str, str, str]] = []
    total = 0

    for path in sorted(BASELINE.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        ast, bc = doc.get("ast"), doc.get("bytecode")
        if ast is None or bc is None:
            continue                      # 前端就没跑通（或只到某一阶段）
        name = doc.get("语料") or path.stem.replace("__", "/")
        total += 1

        # ⚠️ **报错渲染要对齐**（R6 主体 ⑥）：给 `--walk` 传**源文件路径**（就是
        # 编译字节码时用的那个相对路径），这样树遍历执行器也能打出「源码行 + `^`」，
        # 与字节码 VM 逐字一致。读不到源文件时退回「没有源码行」（两边同口径）。
        # ⚠️ **每个语料一个私有 cwd**（并行不互相踩）：教程类语料会在 cwd 里
        # 读写真实文件（`日记.txt` / `账本.csv`），pytest-xdist 并行时多个 worker
        # 同跑同一语料会互写同一个文件。把源文件按**同名相对路径**复制进私有目录、
        # 两边都在那里跑 —— 文件名渲染不变，文件 I/O 互相隔离。
        tmp.write_text(json.dumps(ast, ensure_ascii=False), encoding="utf-8")
        run_dir = run_root / path.stem
        run_dir.mkdir(parents=True, exist_ok=True)
        walk_args = [str(exe), "--walk", str(tmp)]
        # ⚠️ 源文件路径传**相对路径**（与字节码 payload 里的 filename 完全一致），
        # 否则报错渲染里「文件:行:列」的名字会对不上（一边绝对一边相对）。
        if (ROOT / name).exists():
            dst = run_dir / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, dst)
            walk_args.append(name)
        r1 = subprocess.run(walk_args, capture_output=True, input=b"", cwd=str(run_dir))
        walk_out = r1.stdout.decode("utf-8", "replace")
        walk_err = r1.stderr.decode("utf-8", "replace")

        tmp.write_text(json.dumps(bc, ensure_ascii=False), encoding="utf-8")
        r2 = subprocess.run([str(exe), "--load-bytecode", str(tmp)], capture_output=True, input=b"",
                            cwd=str(run_dir))
        vm_out = r2.stdout.decode("utf-8", "replace")
        vm_err = r2.stderr.decode("utf-8", "replace")

        m = UNIMPL.search(walk_err)
        if m:
            todo[m.group(1)] += 1
            if verbose:
                print(f"  … {name}：还没实现「{m.group(1)}」")
            continue
        # 完整比对：stdout + **报错渲染**（stderr 末尾那个换行不同不算）。
        if walk_out == vm_out and walk_err.rstrip("\n") == vm_err.rstrip("\n"):
            same += 1
            if verbose:
                print(f"  ✅ {name}")
        else:
            bad.append((name, walk_out, walk_err, vm_out, vm_err))
            if verbose:
                print(f"  ❌ {name}")

    print(f"\n语料 {total} 个 · **一致 {same}** · 还没实现 {sum(todo.values())}"
          f" · **不一致 {len(bad)}**")
    if todo:
        print("\n还没实现（按出现次数）：")
        for what, n in todo.most_common():
            print(f"  {n:>3} × {what}")
    if bad:
        print("\n❌ 不一致（**要立刻查**）：")
        for name, w, we, v, ve in bad[:10]:
            print(f"  {name}")
            print(f"    树遍历 stdout: {w[:160]!r}")
            print(f"    字节码 stdout: {v[:160]!r}")
            if we.rstrip("\n") != ve.rstrip("\n"):
                print(f"    树遍历 stderr: {we[:160]!r}")
                print(f"    字节码 stderr: {ve[:160]!r}")
    # 清理本轮所有语料的私有运行目录（失败也不留垃圾）
    shutil.rmtree(run_root, ignore_errors=True)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
