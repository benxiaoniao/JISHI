# -*- coding: utf-8 -*-
"""构建基石 C 虚拟机动态库。

用法：
    python cvm/build.py             # 生成 opcodes.h 并编译 DLL/SO
    python cvm/build.py --gen-header  # 只重新生成指令码头文件

设计见 cvm/bytecode.md。指令号的唯一事实来源是 jishi/opcodes.py，
cvm/include/opcodes.h 由本脚本生成，两侧永远同步。
"""

from __future__ import annotations

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

from jishi import opcodes as O  # noqa: E402

CVM = ROOT / "cvm"
INCLUDE = CVM / "include"
SRC = CVM / "src"
BIN = CVM / "bin"

#: Windows 下 gcc 不一定在 PATH，给出常见安装位置
GCC_CANDIDATES = [
    "gcc",
    r"C:\msys64\ucrt64\bin\gcc.exe",
    r"C:\msys64\mingw64\bin\gcc.exe",
    r"C:\mingw64\bin\gcc.exe",
    # Chocolatey 的 mingw 包（GitHub Actions 常用，装完不一定在 PATH）
    r"C:\ProgramData\chocolatey\lib\mingw\tools\install\mingw64\bin\gcc.exe",
    r"C:\ProgramData\chocolatey\bin\gcc.exe",
]


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
    for cand in GCC_CANDIDATES:
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
        "找不到 gcc。请安装 MinGW-w64（或修改 cvm/build.py 里的 "
        "GCC_CANDIDATES 指向你的 gcc 路径）。")


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
