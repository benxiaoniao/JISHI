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


def test_lib_path_prefers_dev_in_source_tree(monkeypatch, tmp_path):
    """**源码树里 `cvm/bin/` 优先**（判据：`cvm/src/` 在）。

    ⚠️ M11 时这条是反的（包内 `_native` 固定优先），判据只写了「包内存在」——
    于是**开发仓库里也走包内那份**。R6.7 真踩到：改了 `cvm/src/jsvm.c`、
    跑了 `cvm/build.py`，Python 加载的仍是 `_native/` 里那份**九天前的** dll，
    **改了没生效、一句提示都没有**（而且这个坑 M40 已经踩过一次）。
    → 判据改成「**这是不是源码树**」：`cvm/src/` 在才算，安装版里没有它。
    """
    fake_root = tmp_path / "src"
    fake_bin = fake_root / "cvm" / "bin"
    fake_native = fake_root / "jishi" / "_native"
    (fake_root / "cvm" / "src").mkdir(parents=True)     # ← 源码树标志
    fake_bin.mkdir(parents=True)
    fake_native.mkdir(parents=True)
    dev_lib = fake_bin / CB._lib_name()
    dev_lib.write_bytes(b"fake-dev")
    (fake_native / CB._lib_name()).write_bytes(b"fake-native")
    monkeypatch.setattr(CB, "_ROOT", fake_root)
    monkeypatch.setattr(CB, "_BIN", fake_bin)
    monkeypatch.setattr(CB, "_NATIVE", fake_native)
    assert CB._lib_path() == dev_lib


def test_lib_path_installed_falls_back_to_package_native(monkeypatch, tmp_path):
    """**安装版**（没有 `cvm/src/`）才用包内的 `_native` —— 老语义只在这半边成立。"""
    fake_root = tmp_path / "installed"
    fake_bin = fake_root / "cvm" / "bin"
    fake_native = fake_root / "jishi" / "_native"
    fake_bin.mkdir(parents=True)                        # 有产物、但**没有** cvm/src
    fake_native.mkdir(parents=True)
    pkg_lib = fake_native / CB._lib_name()
    pkg_lib.write_bytes(b"fake-native")
    monkeypatch.setattr(CB, "_ROOT", fake_root)
    monkeypatch.setattr(CB, "_BIN", fake_bin)
    monkeypatch.setattr(CB, "_NATIVE", fake_native)
    assert CB._lib_path() == pkg_lib


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
