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
    # 原生字符串的公开 ABI（M47 阶段二①）：头文件是第三方宿主的唯一依据
    for fn in ("jsvm_str_new", "jsvm_str_len", "jsvm_str_bytes",
               "jsvm_str_new_batch"):
        assert fn in text, f"缺少原生字符串 API {fn}"


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


@pytest.mark.skipif(not JSVM_DLL.exists(), reason="jsvm 动态库未构建")
def test_native_string_public_abi():
    """原生字符串的公开 ABI（M47 阶段二①）——**不镜像内部布局**也能用。

    第三方宿主（C / Node / Rust…）看得到 `jsvm.h`，看不到 `JsStr` 的内部布局。
    所以这条测试刻意**只用三个公开函数**，验证「建 → 量长度 → 取字节」这条
    文档承诺的路子真的走得通；语言绑定（Python）为省 FFI 才去镜像头部，
    那是它自己的事（由 tests/test_cvm.py 的布局往返测试管）。
    """
    import ctypes

    lib = ctypes.CDLL(str(JSVM_DLL))
    lib.jsvm_create.restype = ctypes.c_void_p
    lib.jsvm_destroy.argtypes = [ctypes.c_void_p]
    lib.jsvm_str_new.restype = ctypes.c_int
    lib.jsvm_str_new.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                ctypes.c_int32, ctypes.c_void_p]
    lib.jsvm_str_len.restype = ctypes.c_int32
    lib.jsvm_str_len.argtypes = [ctypes.c_void_p]
    lib.jsvm_str_bytes.restype = ctypes.c_void_p
    lib.jsvm_str_bytes.argtypes = [ctypes.c_void_p]
    lib.jsvm_release.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    vm = lib.jsvm_create()
    assert vm
    val = (ctypes.c_ubyte * 16)()          # JVal：16 字节，公开布局见 jsvm.h
    try:
        for raw in (b"", b"abc", "中文与 🙂".encode("utf-8")):
            assert lib.jsvm_str_new(vm, raw, len(raw), val) == 0
            assert lib.jsvm_str_len(val) == len(raw)
            got = lib.jsvm_str_bytes(val)
            assert ctypes.string_at(got, len(raw)) == raw
            # 长过 0 的文本必须是**值**（新分配），不是借用的缓冲区
            lib.jsvm_release(vm, val)      # 释放：refcnt 归零 → 内部 free
    finally:
        lib.jsvm_destroy(vm)


def _find_gcc():
    """复用 cvm/build.py 的 gcc 候选表（唯一事实来源，不含个人路径）。"""
    import importlib.util

    build_py = ROOT / "cvm" / "build.py"
    if not build_py.exists():
        return shutil.which("gcc")
    spec = importlib.util.spec_from_file_location("_jishi_cvm_build", build_py)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        return mod.find_gcc()
    except SystemExit:          # find_gcc 找不到时抛 SystemExit
        return None


@pytest.mark.skipif(shutil.which("gcc") is None and
                    not (ROOT / "cvm").exists(),
                    reason="无 gcc，跳过嵌入编译测试")
def test_embed_hello_compiles_and_runs(tmp_path):
    gcc = _find_gcc()
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
