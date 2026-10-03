# -*- coding: utf-8 -*-
"""构建基石前端核心（Rust）为 **Python 扩展模块**。

用法：
    python core/build.py            # 构建 + 复制到 jishi/_core.pyd
    python core/build.py --check    # 只检查环境（cargo / 扩展在不在），不构建

产物：`core/target/release/_core{,.so,.dylib}` → 复制成
`jishi/_core.pyd`（Windows）/ `jishi/_core.so`（Linux/macOS）。

⚠️ **这个产物不进仓库**（`.gitignore` 已挡）：它是按当前 Python 的 ABI 编的，
换个 Python 就得重编。没有它时 `JISHI_FRONTEND=rust` 会**明确报错**，
不会静默回落到 Python（「跳过」与「通过」必须能分得开 —— R0 立下的规矩）。

## 两个环境坑（都踩过，写在这里省下一次排查）

1. **pyo3 的构建脚本要跑一次 Python**。本机（Windows + 受限环境）上它会以
   `os error 231（所有的管道范例都在使用中）` 失败 —— 那时**很难看出**是
   环境问题而不是我们写错了。对策：由本脚本按当前解释器生成一份
   `PYO3_CONFIG_FILE`，pyo3 就不再自己去探测 Python 了。
2. **`windows-gnu` 目标需要 `dlltool`**（pyo3-ffi 用它生成导入库）。
   它随 MinGW 一起来了（和 `gcc` 同目录），但常常不在 PATH 里 ——
   本脚本按与 `cvm/build.py` 同样的候选清单找它，也可用 `JISHI_MINGW` 指定。
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
import sysconfig
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
TARGET = CORE / "target" / "release"
PKG = ROOT / "jishi"

#: 扩展模块的落点（与 `jishi/tokenizer.py` 里的 `from . import _core` 对应）
EXT_SUFFIX = ".pyd" if os.name == "nt" else ".so"
EXT_DST = PKG / f"_core{EXT_SUFFIX}"

#: Windows 上 gcc/dlltool 不一定在 PATH（与 `cvm/build.py` 同一套候选位置）
MINGW_CANDIDATES = [
    r"C:\msys64\ucrt64\bin",
    r"C:\msys64\mingw64\bin",
    r"C:\mingw64\bin",
    r"C:\ProgramData\chocolatey\lib\mingw\tools\install\mingw64\bin",
]


def find_mingw() -> "Path | None":
    """找到带 `dlltool.exe` 的目录（只在 Windows 上需要）。"""
    if os.name != "nt":
        return None
    env = os.environ.get("JISHI_MINGW")
    if env and (Path(env) / "dlltool.exe").is_file():
        return Path(env)
    if shutil.which("dlltool"):
        return None                      # 已经在 PATH 里，不用额外加
    for cand in MINGW_CANDIDATES:
        if (Path(cand) / "dlltool.exe").is_file():
            return Path(cand)
    return None


def write_pyo3_config() -> Path:
    """按**当前解释器**生成 pyo3 的配置文件（绕开「构建脚本跑 Python」）。

    字段含义见 pyo3-build-config；这里只填它读的那几个。
    """
    info = sys.version_info
    version = f"{info.major}.{info.minor}"
    nodot = f"{info.major}{info.minor}"
    lib_dir = sysconfig.get_config_var("LIBDIR")
    if not lib_dir:
        # Windows 上 LIBDIR 常为空：导入库在 <base_prefix>\libs
        lib_dir = str(Path(sys.base_prefix) / "libs")
    shared = bool(sysconfig.get_config_var("Py_ENABLE_SHARED"))
    if os.name == "nt":
        shared = True                    # Windows 上 pythonXY.dll 就是共享库
    # Windows 的导入库叫 pythonXY.lib；POSIX 上是 libpythonX.Y.so / .a
    lib_name = f"python{nodot}" if os.name == "nt" else \
        str(sysconfig.get_config_var("LDVERSION") or version)
    text = "\n".join([
        "implementation=CPython",
        f"version={version}",
        f"shared={'true' if shared else 'false'}",
        "abi3=false",
        f"lib_name={lib_name}",
        f"lib_dir={lib_dir}",
        f"executable={sys.executable}",
        f"pointer_width={struct.calcsize('P') * 8}",
        "build_flags=",
        "suppress_build_script_link_lines=false",
        "",
    ])
    dst = CORE / "target" / "pyo3-config.txt"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8", newline="\n")
    return dst


def cargo_env() -> dict:
    env = dict(os.environ)
    mingw = find_mingw()
    if mingw is not None:
        env["PATH"] = str(mingw) + os.pathsep + env.get("PATH", "")
    env["PYO3_CONFIG_FILE"] = str(write_pyo3_config())
    env.setdefault("PYTHONUTF8", "1")
    return env


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(prog="core/build.py",
                                 description="构建基石前端核心（Rust → Python 扩展）")
    ap.add_argument("--check", action="store_true",
                    help="只检查环境，不构建")
    args = ap.parse_args(argv)

    cargo = shutil.which("cargo")
    if not cargo:
        print("❌ 找不到 cargo —— 装 Rust 工具链后再来（https://rustup.rs/）",
              file=sys.stderr)
        return 1

    if args.check:
        print(f"cargo      : {cargo}")
        mingw = find_mingw()
        print(f"dlltool 目录: {mingw or '（PATH 里已有 / 非 Windows）'}")
        print(f"扩展模块    : {EXT_DST} "
              f"{'✅ 在' if EXT_DST.exists() else '❌ 不在（先跑 python core/build.py）'}")
        return 0

    env = cargo_env()
    print(f"🔧 构建 {CORE.name}/（pyo3 → {EXT_DST.name}）…")
    proc = subprocess.run([cargo, "build", "--release"], cwd=str(CORE), env=env)
    if proc.returncode != 0:
        print("❌ 构建失败（上面是 cargo 的输出）", file=sys.stderr)
        return proc.returncode

    built = None
    for name in ("_core.dll", "_core.so", "_core.dylib", "lib_core.so",
                 "lib_core.dylib"):
        p = TARGET / name
        if p.is_file():
            built = p
            break
    if built is None:
        print(f"❌ 没找到构建产物（{TARGET}）—— 看 Cargo.toml 的 crate-type",
              file=sys.stderr)
        return 1

    EXT_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(built, EXT_DST)
    print(f"✅ 已生成 {EXT_DST.relative_to(ROOT).as_posix()} "
          f"（{EXT_DST.stat().st_size / 1024:.0f} KB）")
    print("   ⚠️ 换 Python 版本要重编（扩展按 ABI 绑死）；"
          "本产物**不进仓库**。")
    print("   用法：JISHI_FRONTEND=rust python -m jishi.cli 程序.jsh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
