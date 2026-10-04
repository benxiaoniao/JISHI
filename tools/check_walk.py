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

## ⚠️ 已知登记：一个「形状级」的分叉（**不在这份语料里**）

「捕获体里 `返回` / `中断`」**且**带「最终」块时，四个字节码宿主都**不跑「最终」
块**，而规范（树遍历解释器）跑 —— 根因在**编译器**：`c_try` 只把**`尝试` 体**
套进了 finally，**捕获体没有**。树遍历参考执行器站在规范这边，所以**这个形状
不进 `tests/cases/`**（这里树遍历是基准之一，进了这个工具会红）。

* 登记与**过期报红**在 `tests/test_m80_walk_exceptions.py`
  的 `test_已知登记_捕获体跳出时最终块的差异`（一旦两边一致，它会失败并提示删掉）；
* 开发时的形状对拍小工具 `build/_walk_probe.py` 也有一份 `KNOWN_DIVERGENT`（未入库）；
* 背景见 `docs/设计决策.md` D73 §五。

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

    tmp = Path(tempfile.gettempdir()) / "_check_walk.json"
    same = 0
    todo: Counter[str] = Counter()
    bad: list[tuple[str, str, str]] = []
    total = 0

    for path in sorted(BASELINE.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        ast, bc = doc.get("ast"), doc.get("bytecode")
        if ast is None or bc is None:
            continue                      # 前端就没跑通（或只到某一阶段）
        name = path.stem.replace("__", "/")
        total += 1

        tmp.write_text(json.dumps(ast, ensure_ascii=False), encoding="utf-8")
        r1 = subprocess.run([str(exe), "--walk", str(tmp)],
                            capture_output=True, input=b"")
        walk_out = r1.stdout.decode("utf-8", "replace")
        walk_err = r1.stderr.decode("utf-8", "replace")

        tmp.write_text(json.dumps(bc, ensure_ascii=False), encoding="utf-8")
        r2 = subprocess.run([str(exe), str(tmp)], capture_output=True, input=b"")
        vm_out = r2.stdout.decode("utf-8", "replace")

        m = UNIMPL.search(walk_err)
        if m:
            todo[m.group(1)] += 1
            if verbose:
                print(f"  … {name}：还没实现「{m.group(1)}」")
            continue
        if walk_out == vm_out:
            same += 1
            if verbose:
                print(f"  ✅ {name}")
        else:
            bad.append((name, walk_out, vm_out))
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
        for name, w, v in bad[:10]:
            print(f"  {name}")
            print(f"    树遍历: {w[:200]!r}")
            print(f"    字节码: {v[:200]!r}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
