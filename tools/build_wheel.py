# -*- coding: utf-8 -*-
"""构建含预编译 C VM 动态库的 wheel（M11：pip install 即含三执行器）。

用法：
    python tools/build_wheel.py            # 编译 DLL 到包内 + 构建 wheel + 验证

流程：
    1. python cvm/build.py --out jishi/_native   编译动态库到包内
    2. python -m build --wheel                     构建 wheel（package-data 带入 DLL）
    3. zipfile 校验 wheel 内确实含动态库

产出：dist/jishi-<版本>-py3-none-<平台>.whl（平台相关，含 jsvm.dll/.so/.dylib）。
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "jishi" / "_native"
DIST = ROOT / "dist"

LIBS = {
    "win32": "jsvm.dll",
    "darwin": "libjsvm.dylib",
}.get(sys.platform, "libjsvm.so")


def _run(cmd: list[str]) -> None:
    print("$ " + " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)


def main() -> int:
    # 1. 编译动态库到包内
    print("== [1/3] 编译 C VM 动态库到包内 ==")
    _run([sys.executable, str(ROOT / "cvm" / "build.py"),
          "--out", str(NATIVE)])
    lib = NATIVE / LIBS
    if not lib.exists():
        print(f"错误：未生成 {lib}")
        return 1

    # 2. 构建 wheel
    print("\n== [2/3] 构建 wheel ==")
    _run([sys.executable, "-m", "build", "--wheel"])

    # 3. 验证 wheel 含动态库
    print("\n== [3/3] 验证 wheel 内是否含动态库 ==")
    wheels = sorted(DIST.glob("*.whl"))
    if not wheels:
        print("错误：dist/ 下没有 wheel")
        return 1
    whl = wheels[-1]          # 最新一个
    with zipfile.ZipFile(whl) as zf:
        names = zf.namelist()
        hit = [n for n in names if n.endswith(LIBS)]
    if hit:
        print(f"✓ {whl.name} 含 {hit[0]}")
        return 0
    print(f"✗ {whl.name} 不含 {LIBS}，含内容：")
    for n in names:
        print("   ", n)
    return 1


if __name__ == "__main__":
    sys.exit(main())
