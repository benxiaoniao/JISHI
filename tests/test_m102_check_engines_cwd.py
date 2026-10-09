# -*- coding: utf-8 -*-
"""R8.5a：`check_engines.py` 必须在**私有 cwd** 里跑语料 —— 仓库根不许留产物。

## 为什么需要这条判据

教程类语料（`examples/tutorial/09_成绩表分析.jsh` / `10_记账程序.jsh`）
会在 **cwd** 里读写真实文件（`成绩.csv` / `账本.csv`）。工具**以前直接在仓库根跑**，
于是：

* 跑完（或被打断 —— timeout / Ctrl-C）就在仓库根留下这些文件；
* 下一轮**第一台引擎**读到翻倍的数据 → 报「树遍历 8 笔 / 其余 4 笔」这种**假红**。

这正是 AGENTS.md §1 里记着的那条。`check_walk.py` 早就改成了「每语料一个私有 cwd」，
这里把 `check_engines.py` 补上同一做法。

## 判据分两层

* **非空**：语料集里**确实**有会往 cwd 写文件的教程 —— 否则这条判据是空转的。
* **行为**：真跑一遍工具（只用三个 **进程内** Python 执行器，够快）→
  **退码 0** 且 **仓库根没有那三个文件**。
  （进程内执行器正是需要 `os.chdir` 隔离的那一批；两个宿主本来就是独立进程、
  传 `cwd=` 即可，`--no-rust-vm --no-node` 不影响对 chdir 这一半的检验。）
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 教程语料会往 cwd 写的文件名（AGENTS.md §1 点的就是这三个）
ARTIFACTS = ("账本.csv", "成绩.csv", "x.txt")

#: 语料集里**确实会写文件**的教程（判据非空的锚）
WRITERS = (
    "examples/tutorial/09_成绩表分析.jsh",
    "examples/tutorial/10_记账程序.jsh",
)


def test_语料集里确实有会写文件的教程():
    for rel in WRITERS:
        assert (ROOT / rel).exists(), f"{rel} 不见了 —— 这条判据失去意义"


def test_工具在私有_cwd_里跑_仓库根不留产物():
    # 先把可能的历史残留清掉（否则分不清是工具留的还是上轮留的）
    for name in ARTIFACTS:
        (ROOT / name).unlink(missing_ok=True)

    r = subprocess.run(
        [sys.executable, "tools/check_engines.py", "--no-rust-vm", "--no-node"],
        cwd=str(ROOT), capture_output=True, timeout=300,
    )
    out = r.stdout.decode("utf-8", "replace")
    err = r.stderr.decode("utf-8", "replace")
    assert r.returncode == 0, (
        "check_engines（三执行器）不一致或报错：\n" + out[-1500:] + err[-800:]
    )

    left = [n for n in ARTIFACTS if (ROOT / n).exists()]
    assert not left, (
        f"工具在**仓库根**留下了产物：{left} —— 私有 cwd 失效了？"
        "（`tools/check_engines.py` 的 `_in_dir` / `cwd=run_dir`）"
    )
