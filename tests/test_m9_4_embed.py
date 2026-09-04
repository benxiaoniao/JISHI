# -*- coding: utf-8 -*-
"""M9.4 测试：C ABI 稳定化（头文件 + 导出符号 + 嵌入编译）。"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JSVM_H = ROOT / "cvm" / "include" / "jsvm.h"
JSVM_DLL = ROOT / "cvm" / "bin" / ("jsvm.dll" if os.name == "nt" else "jsvm.so")


def test_jsvm_header_has_abi_version():
    assert JSVM_H.exists(), "jsvm.h 头文件不存在"
    text = JSVM_H.read_text(encoding="utf-8")
    assert "JSVM_ABI_VERSION 1" in text
    assert "JSVM_API" in text
    assert "typedef struct JVal" in text
    assert "typedef struct JsHost" in text
    assert "jsvm_create" in text
    assert "jsvm_load_module" in text
    assert "jsvm_run" in text
    assert "jsvm_destroy" in text


def test_jsvm_header_has_value_ctors():
    text = JSVM_H.read_text(encoding="utf-8")
    for ctor in ("jsvm_int", "jsvm_float", "jsvm_bool", "jsvm_null",
                 "jsvm_host", "jsvm_unset"):
        assert ctor in text, f"缺少构造器 {ctor}"


@pytest.mark.skipif(not JSVM_DLL.exists(), reason="jsvm 动态库未构建")
def test_jsvm_dll_exports_symbols():
    import ctypes
    lib = ctypes.CDLL(str(JSVM_DLL))
    # 关键导出符号必须存在（创建 → 销毁的最小流程）
    lib.jsvm_create.restype = ctypes.c_void_p
    lib.jsvm_destroy.argtypes = [ctypes.c_void_p]
    vm = lib.jsvm_create()
    assert vm, "jsvm_create 返回空"
    lib.jsvm_destroy(vm)


@pytest.mark.skipif(shutil.which("gcc") is None and
                    not (ROOT / "cvm").exists(),
                    reason="无 gcc，跳过嵌入编译测试")
def test_embed_hello_compiles_and_runs(tmp_path):
    gcc = shutil.which("gcc")
    if gcc is None:
        # 用 build.py 里的 GCC_CANDIDATES 找 MinGW gcc
        for cand in (r"D:\CLion\CLion 2026.2.1\bin\mingw\bin\gcc.exe",
                     r"C:\msys64\ucrt64\bin\gcc.exe",
                     r"C:\msys64\mingw64\bin\gcc.exe"):
            if os.path.exists(cand):
                gcc = cand
                break
    if gcc is None or not JSVM_DLL.exists():
        pytest.skip("无可用 gcc 或 jsvm.dll 未构建")

    hello_c = ROOT / "examples" / "embed" / "hello.c"
    exe = tmp_path / "hello.exe"
    include = ROOT / "cvm" / "include"
    compile_cmd = [gcc, "-O2", "-std=c11", str(hello_c),
                   "-I", str(include), str(JSVM_DLL), "-o", str(exe)]
    r = subprocess.run(compile_cmd, capture_output=True, text=True)
    assert r.returncode == 0, f"编译失败：{r.stderr}"

    # 运行需要 jsvm.dll 在同目录
    shutil.copy(JSVM_DLL, tmp_path / JSVM_DLL.name)
    out = subprocess.run([str(exe)], capture_output=True, text=True)
    assert out.returncode == 0
    assert "OK: last_value = 42" in out.stdout
