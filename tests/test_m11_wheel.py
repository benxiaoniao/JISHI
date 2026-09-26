# -*- coding: utf-8 -*-
"""M11 性能与分发测试：wheel 内带预编译 DLL、动态库定位优先级。"""

import sys
from pathlib import Path

import pytest

from jishi import cvm_bind as CB


def test_lib_name_platform():
    """动态库文件名按平台映射。"""
    name = CB._lib_name()
    if sys.platform == "win32":
        assert name == "jsvm.dll"
    elif sys.platform == "darwin":
        assert name == "libjsvm.dylib"
    else:
        assert name == "libjsvm.so"


def test_lib_path_prefers_package_native(monkeypatch, tmp_path):
    """包内 _native 优先于源码树 cvm/bin。"""
    # 伪造一个包内 native 目录，塞一个假 DLL 文件
    fake_pkg = tmp_path / "_native"
    fake_pkg.mkdir()
    fake_lib = fake_pkg / CB._lib_name()
    fake_lib.write_bytes(b"fake")
    monkeypatch.setattr(CB, "_NATIVE", fake_pkg)

    # 源码树 cvm/bin 也存在（开发模式），但包内应优先
    assert CB._lib_path() == fake_lib


def test_lib_path_fallback_to_dev(monkeypatch, tmp_path):
    """包内无 DLL 时回退源码树 cvm/bin。"""
    empty = tmp_path / "empty_native"
    empty.mkdir()
    monkeypatch.setattr(CB, "_NATIVE", empty)

    dev = Path(CB._BIN) / CB._lib_name()
    if dev.exists():
        assert CB._lib_path() == dev
    else:
        # 无 cvm/bin 也无包内 → 报错（环境无 C 编译器时）
        with pytest.raises(FileNotFoundError):
            CB._lib_path()


def test_build_wheel_module_maps_platform_lib():
    """tools/build_wheel.py 的平台库名映射正确。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_wheel", str(Path("tools/build_wheel.py").resolve()))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    expected = {"win32": "jsvm.dll",
                "darwin": "libjsvm.dylib"}.get(sys.platform, "libjsvm.so")
    assert mod.LIBS == expected
    assert mod.NATIVE.name == "_native"
    assert mod.NATIVE.parent.name == "jishi"
    assert mod.DIST.name == "dist"
