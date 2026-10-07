# -*- coding: utf-8 -*-
"""构建 wasm（playground 用的那份），产物落到 `site/jishi_wasm.wasm`。

    python tools/build_wasm.py            # 构建 + 拷到 site/
    python tools/build_wasm.py --check    # 只比「站点里那份是不是最新的」

为什么要一个脚本而不是直接 `cargo build`：
1. 要 `--target wasm32-unknown-unknown`，而且**这个 target 不一定装了** ——
   脚本会给出 `rustup target add …` 那句（而不是让人对着 cargo 的报错猜）；
2. 产物要**改名 + 挪位置**（`jishi_wasm.wasm` → `site/`），站点直接静态托管；
3. 顺手报**体积**（R8 的验收线里有「体积」这一项，得看得见）。

⚠️ 与原生宿主的关系：`wasm/` 是**独立 crate**，只有它开 `jishi-ffi` 的
`budget` 特征（步数预算）—— 原生宿主的构建**完全不受影响**（实测特征关掉时
热路径 1.000x）。所以这里跑 cargo **不会**污染 `rust/target/`。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WASM_CRATE = ROOT / "wasm"
TARGET = "wasm32-unknown-unknown"
ARTIFACT = WASM_CRATE / "target" / TARGET / "release" / "jishi_wasm.wasm"
SITE = ROOT / "site"
DEST = SITE / "jishi_wasm.wasm"
#: JS 胶水**是源码**（手写的，在 `wasm/` 里），站点那份是拷过去的。
#: ⚠️ `site/` 是**构建产物目录**（`.gitignore` 里有）—— 所以胶水**不能**只放在那儿，
#: 否则 `git archive` 打出来的快照里根本没有它。
SHIM_SRC = WASM_CRATE / "jishi-wasm.js"
SHIM_DEST = SITE / "jishi-wasm.js"


def build() -> int:
    r = subprocess.run(
        ["cargo", "build", "--target", TARGET, "--release"],
        cwd=WASM_CRATE, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    if r.returncode != 0:
        err = (r.stderr or "") + (r.stdout or "")
        if "can't find crate for `std`" in err or "target may not be installed" in err:
            print(f"没装 wasm 目标。先跑：\n  rustup target add {TARGET}", file=sys.stderr)
            return 2
        print("构建失败：\n" + err[-2000:], file=sys.stderr)
        return 1

    if not ARTIFACT.is_file():
        print(f"构建成功但找不到产物：{ARTIFACT}", file=sys.stderr)
        return 1
    DEST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ARTIFACT, DEST)
    shutil.copyfile(SHIM_SRC, SHIM_DEST)
    n = DEST.stat().st_size
    print(f"✓ {DEST.relative_to(ROOT)}（{n:,} 字节 = {n / 1024 / 1024:.2f} MB）")
    print(f"✓ {SHIM_DEST.relative_to(ROOT)}（从 {SHIM_SRC.relative_to(ROOT)} 拷过去）")
    return 0


def check() -> int:
    """站点里那份是不是比源码新（判据里用；不构建）。"""
    if not DEST.is_file():
        print(f"站点里没有 {DEST.name}；先跑 `python tools/build_wasm.py`", file=sys.stderr)
        return 2
    newest = max(
        [p.stat().st_mtime for p in list(WASM_CRATE.glob("src/**/*.rs"))
         + list((ROOT / "rust" / "src").glob("**/*.rs"))
         + list((ROOT / "frontend" / "src").glob("**/*.rs"))]
        + [0])
    if DEST.stat().st_mtime < newest:
        print("站点里的 wasm 比源码旧；跑 `python tools/build_wasm.py` 重构建",
              file=sys.stderr)
        return 1
    n = DEST.stat().st_size
    print(f"✓ 是最新的（{n:,} 字节 = {n / 1024 / 1024:.2f} MB）")
    return 0


def main() -> int:
    if "--check" in sys.argv:
        return check()
    if os.environ.get("JISHI_SKIP_WASM") == "1":
        print("JISHI_SKIP_WASM=1，跳过")
        return 0
    return build()


if __name__ == "__main__":
    sys.exit(main())
