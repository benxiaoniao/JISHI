# -*- coding: utf-8 -*-
"""构建基石 C 虚拟机动态库。

用法：
    python cvm/build.py             # 生成 opcodes.h 并编译 DLL/SO
    python cvm/build.py --gen-header  # 只重新生成指令码头文件

设计见 cvm/bytecode.md。指令号的唯一事实来源是 jishi/opcodes.py，
cvm/include/opcodes.h 由本脚本生成，两侧永远同步。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Windows 控制台/CI 的 stdout 默认可能是 cp1252 等非 UTF-8 编码，
# 打印中文（如「构建完成」）会抛 UnicodeEncodeError（GitHub Windows CI 踩过）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oracle.jishi import opcodes as O  # noqa: E402

CVM = ROOT / "cvm"
INCLUDE = CVM / "include"
SRC = CVM / "src"
BIN = CVM / "bin"

#: Windows 下 gcc 不一定在 PATH，给出常见安装位置。
#:
#: 🔴 **个人环境的编译器路径不写在这里**（2026-11-25）：仓库是公开的，
#: 写死某个 IDE 自带的 MinGW 路径既暴露别人的机器布局，也让发布前的隐私终检
#: 必须专门去「刮掉那些行」—— 那是给脏数据打补丁。
#: 本机需要额外候选就写在**同目录的 `.gcc-paths.txt`**（一行一个，`#` 是注释，
#: 该文件在 `.git/info/exclude` 里、不进仓库），或者设环境变量 `JISHI_GCC`。
GCC_CANDIDATES = [
    "gcc",
    r"C:\msys64\ucrt64\bin\gcc.exe",
    r"C:\msys64\mingw64\bin\gcc.exe",
    r"C:\mingw64\bin\gcc.exe",
    # Chocolatey 的 mingw 包（GitHub Actions 常用，装完不一定在 PATH）
    r"C:\ProgramData\chocolatey\lib\mingw\tools\install\mingw64\bin\gcc.exe",
    r"C:\ProgramData\chocolatey\bin\gcc.exe",
]


def _本机额外候选() -> "list[str]":
    """本机专属的 gcc 路径（从 `JISHI_GCC` 与 `.gcc-paths.txt` 读）。

    为什么要有它：见 `GCC_CANDIDATES` 上面那段 —— 个人路径不进仓库，
    但本机构建照样要能找到编译器。
    """
    候选: "list[str]" = []
    环境 = os.environ.get("JISHI_GCC")
    if 环境:
        候选.append(环境)
    路 = Path(__file__).with_name(".gcc-paths.txt")
    if 路.is_file():
        for 行 in 路.read_text(encoding="utf-8").splitlines():
            行 = 行.strip()
            if 行 and not 行.startswith("#"):
                候选.append(行)
    return 候选


# ---------------------------------------------------------------------------
# 头文件生成
# ---------------------------------------------------------------------------

def gen_header() -> Path:
    INCLUDE.mkdir(parents=True, exist_ok=True)
    lines = [
        "/* opcodes.h —— 自动生成，勿手改。",
        " * 来源：jishi/opcodes.py（唯一事实来源）。",
        " * 重新生成：python cvm/build.py --gen-header",
        " */",
        "#ifndef JS_OPCODES_H",
        "#define JS_OPCODES_H",
        "",
    ]

    def emit_enum(name: str, prefix: str, pairs: list[tuple[str, int]]):
        lines.append("enum {")
        for key, val in pairs:
            lines.append(f"    {prefix}{key} = {val},")
        lines.append("};")
        lines.append("")

    emit_enum("JsOp", "JS_OP_",
              [(name, getattr(O.Op, name)) for name in O.OP_NAMES])

    def enum_pairs(cls) -> list[tuple[str, int]]:
        """取普通类的整型成员（跳过下划线开头的内建项）。"""
        return sorted(
            ((k, v) for k, v in vars(cls).items()
             if not k.startswith("_") and isinstance(v, int)),
            key=lambda kv: kv[1],
        )

    emit_enum("JsBin", "JS_BIN_", enum_pairs(O.BinOp))
    emit_enum("JsCmp", "JS_CMP_", enum_pairs(O.CmpOp))
    lines += [
        "/* 注意：JS_CMP_IN/NOT_IN/IS/IS_NOT（6-9）没有 C 侧快路径实现。",
        " * compare_fast 只处理 0-5，其余自动回落 host->compare —— 这是",
        " * 「加语义比较运算符零 C 改动」的前提，见 jishi/opcodes.py 的 CmpOp。 */",
        "",
    ]
    emit_enum("JsUn", "JS_UN_", enum_pairs(O.UnOp))
    emit_enum("JsParam", "JS_PARAM_", enum_pairs(O.ParamKind))

    emit_enum("JsTag", "JS_TAG_", [
        ("UNSET", O.T_UNSET), ("NULL", O.T_NULL), ("FALSE", O.T_FALSE),
        ("TRUE", O.T_TRUE), ("INT", O.T_INT), ("FLOAT", O.T_FLOAT),
        ("HOST", O.T_HOST), ("CELL", O.T_CELL), ("FUNC", O.T_FUNC),
        ("ITER", O.T_ITER), ("STR", O.T_STR),
    ])

    lines += [
        "/* 帧深度上限（与 jishi/vm.py 的 MAX_FRAMES 一致） */",
        "#define JS_MAX_FRAMES 1000",
        "",
        "#endif /* JS_OPCODES_H */",
    ]

    path = INCLUDE / "opcodes.h"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 编译
# ---------------------------------------------------------------------------

def find_gcc() -> str:
    for cand in GCC_CANDIDATES + _本机额外候选():
        if cand == "gcc":
            if shutil.which("gcc"):
                return "gcc"
            # macOS 常只有 clang（gcc 可能不存在）；Linux CI 也可能如此
            if shutil.which("clang"):
                return "clang"
            continue
        if Path(cand).exists():
            return cand
    raise SystemExit(
        "找不到 gcc。请安装 MinGW-w64；本机的 gcc 在别处就设环境变量 "
        "`JISHI_GCC=<gcc 的完整路径>`，或写在 cvm/.gcc-paths.txt 里（一行一个）。")


def build(out_dir: Path = BIN) -> Path:
    gen_header()
    SRC.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    if sys.platform == "win32":
        out = out_dir / "jsvm.dll"
        extra = []
    elif sys.platform == "darwin":
        out = out_dir / "libjsvm.dylib"
        extra = ["-fPIC"]
    else:
        out = out_dir / "libjsvm.so"
        extra = ["-fPIC"]

    gcc = find_gcc()
    cmd = [gcc, "-O2", "-std=c11", "-Wall", "-Wextra",
           "-DJSVM_BUILD",          # 导出 ABI 符号（M9.4）
           *extra, "-shared",
           "-I", str(INCLUDE),
           str(SRC / "jsvm.c"),
           "-o", str(out)]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)
    print(f"构建完成：{out}")
    return out


if __name__ == "__main__":
    if "--gen-header" in sys.argv:
        print(f"已生成 {gen_header()}")
    elif "--out" in sys.argv:
        # 编译到指定目录（M11：python cvm/build.py --out jishi/_native）
        build(Path(sys.argv[sys.argv.index("--out") + 1]))
    else:
        build()
