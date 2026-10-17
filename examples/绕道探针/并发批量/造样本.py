# -*- coding: utf-8 -*-
"""造一批**小文件**，用来量「并发到底值多少」（M114 的验收手段）。

为什么要有它：并发**不是「加了就更快」**（见 README 的两条实测）。
要比出真实收益，样本得像真实场景 —— **大量、小、各自在等 IO**。

    python 造样本.py <目录> <个数> [每个多少 KB]
    python 造样本.py /tmp/样本 2000 4        # 2000 个 4 KB（README 里那组）
    python 造样本.py /tmp/大样本 40 1024     # 40 个 1 MB（反例那组）

内容按序号确定（可复现），文件不会重复。
"""

import sys
from pathlib import Path


def main(argv):
    if len(argv) < 2:
        print("用法：python 造样本.py <目录> <个数> [每个多少 KB]")
        return
    root = Path(argv[0])
    个数 = int(argv[1])
    每份 = int(argv[2]) * 1024 if len(argv) > 2 else 4096
    root.mkdir(parents=True, exist_ok=True)
    # 一字节一字节地造太慢 —— 先造一块底子，再按序号错开，保证内容各不相同
    底 = bytes(range(256)) * (每份 // 256 + 1)
    for i in range(个数):
        (root / f"s{i:05d}.bin").write_bytes(底[i % 256:][:每份])
    print(f"造好 {个数} 个文件（每个 {每份 // 1024 or 1} KB）→ {root}")


if __name__ == "__main__":
    main(sys.argv[1:])
