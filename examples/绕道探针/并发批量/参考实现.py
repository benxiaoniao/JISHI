# -*- coding: utf-8 -*-
"""批量摘要（Python 参考实现，只用标准库）。

与「批量.jsh」同功能、同输出口径 —— 用来对照「有并发能省多少时间」。

默认走线程池；加 `--顺序` 就走单线程，两种模式都留着，好把「并发的收益」
**量出来**（而不是声称）。
"""

import hashlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CHUNK = 1 << 20


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def main(argv):
    serial = "--顺序" in argv
    args = [a for a in argv if not a.startswith("-")]
    if not args:
        print("用法：python 参考实现.py <目录> [后缀] [--顺序]")
        return
    root = Path(args[0])
    suffix = args[1] if len(args) > 1 else ""
    if not root.is_dir():
        print(f"「{args[0]}」不是目录")
        return

    files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and (not suffix or p.suffix == suffix)
    )
    print(f"共 {len(files)} 个文件，逐个算摘要…")

    start = time.perf_counter()
    if serial:
        results = [digest(p) for p in files]
        label = "逐个处理"
    else:
        with ThreadPoolExecutor() as pool:
            results = list(pool.map(digest, files))
        label = "并发处理"
    print(f"{label}用了 {time.perf_counter() - start:.3f} 秒")

    for path, dig in zip(files, results):
        print(f"  {path.name}  {dig[:16]}")
    print()
    print("（对比：基石版只能一个个算 —— 见绕道 R005）")


if __name__ == "__main__":
    main(sys.argv[1:])
