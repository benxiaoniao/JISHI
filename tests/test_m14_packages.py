# -*- coding: utf-8 -*-
"""M14 包管理与生态：安装/卸载/列表命令 + 包清单扩充 + 标准库扩充。"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from jishi import packages as PKG
from jishi import runtime
from jishi.errors import JishiError
from jishi.interpreter import run_source


# ---------------------------------------------------------------------------
# 14.1 包清单：入口 / 许可 字段
# ---------------------------------------------------------------------------

def _write_pkg(base, name, meta, modules):
    import json
    pkg = base / name
    pkg.mkdir()
    (pkg / "包.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    for fn, src in modules.items():
        (pkg / fn).write_text(src, encoding="utf-8")
    return pkg


def test_package_entry_field(tmp_path, monkeypatch):
    """声明「入口」只执行入口模块，内部实现模块不导出。"""
    _write_pkg(tmp_path, "甲", {"名字": "甲", "版本": "1.0.0", "入口": "主"},
               {"主.jsh": "函数 算(x):\n    返回 x + 1\n",
                "内部.jsh": "令 私密 = 999\n"})
    monkeypatch.setattr(runtime, "_package_dirs", lambda: [str(tmp_path)])
    out = io.StringIO()
    with redirect_stdout(out):
        run_source("导入 甲 从 本地包\n打印(甲.算(1))\n")
    assert out.getvalue() == "2\n"
    # 内部模块的顶层变量不应导出：访问应报「没有」
    with pytest.raises(JishiError) as ei:
        run_source("导入 甲 从 本地包\n打印(甲.私密)\n")
    assert "私密" in str(ei.value)


def test_package_entry_missing(tmp_path, monkeypatch):
    _write_pkg(tmp_path, "甲", {"名字": "甲", "入口": "不存在的模块"},
               {"a.jsh": "令 x = 1\n"})
    monkeypatch.setattr(runtime, "_package_dirs", lambda: [str(tmp_path)])
    with pytest.raises(JishiError) as ei:
        run_source("导入 甲 从 本地包\n")
    assert "入口" in str(ei.value)


def test_package_license_field(tmp_path, monkeypatch):
    """「许可」字段不影响导入，仅作元信息记录。"""
    _write_pkg(tmp_path, "甲", {"名字": "甲", "版本": "1.0.0", "许可": "MIT"},
               {"a.jsh": "函数 你好():\n    返回 \"你好\"\n"})
    monkeypatch.setattr(runtime, "_package_dirs", lambda: [str(tmp_path)])
    out = io.StringIO()
    with redirect_stdout(out):
        run_source("导入 甲 从 本地包\n打印(甲.你好())\n")
    assert out.getvalue() == "你好\n"


# ---------------------------------------------------------------------------
# 14.2 安装 / 卸载 / 列表 命令
# ---------------------------------------------------------------------------

def test_install_dir_and_uninstall(tmp_path, monkeypatch):
    pkg = _write_pkg(tmp_path, "甲", {"名字": "甲", "版本": "1.0.0", "描述": "测试"},
                     {"a.jsh": "函数 算(x):\n    返回 x * 2\n"})
    dest = tmp_path / "缓存"
    PKG.install_package_dir(str(pkg), "甲", dest_root=str(dest))
    assert (dest / "甲" / "包.json").is_file()

    # 列表
    pkgs = PKG.list_packages(dest_root=str(dest))
    assert any(p["名字"] == "甲" and p["版本"] == "1.0.0" for p in pkgs)

    # 重复安装报错
    with pytest.raises(ValueError) as ei:
        PKG.install_package_dir(str(pkg), "甲", dest_root=str(dest))
    assert "已安装" in str(ei.value)

    # force 覆盖
    PKG.install_package_dir(str(pkg), "甲", dest_root=str(dest), force=True)

    # 卸载
    assert PKG.uninstall_package("甲", dest_root=str(dest)) is True
    assert not (dest / "甲").exists()
    assert PKG.uninstall_package("甲", dest_root=str(dest)) is False


def test_install_name_mismatch(tmp_path):
    _write_pkg(tmp_path, "甲", {"名字": "乙"}, {"a.jsh": "令 x = 1\n"})
    with pytest.raises(ValueError) as ei:
        PKG.install_package_dir(str(tmp_path / "甲"), "甲")
    assert "不一致" in str(ei.value)


def test_install_zip(tmp_path):
    import zipfile
    pkg = _write_pkg(tmp_path, "甲", {"名字": "甲", "版本": "1.0.0"},
                     {"a.jsh": "令 x = 1\n"})
    # 打 zip：甲/ 顶层目录
    zip_path = tmp_path / "甲-1.0.0.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(str(pkg / "包.json"), "甲/包.json")
        zf.write(str(pkg / "a.jsh"), "甲/a.jsh")
    dest = tmp_path / "缓存"
    PKG.install_package_zip(str(zip_path), "甲", dest_root=str(dest))
    assert (dest / "甲" / "包.json").is_file()


# ---------------------------------------------------------------------------
# 14.4 新标准库：路径 / 加密 / 压缩 / 系统
# ---------------------------------------------------------------------------

def _run(src):
    buf = io.StringIO()
    with redirect_stdout(buf):
        run_source(src)
    return buf.getvalue()


def test_stdlib_path():
    out = _run('导入 路径\n'
               '打印(路径.文件名("a/b/c.txt"))\n'
               '打印(路径.后缀("a/b/c.txt"))\n'
               '打印(路径.无后缀名("a/b/c.txt"))\n'
               '打印(路径.连接("a", "b", "c.txt"))\n')
    assert "c.txt" in out and ".txt" in out and "c" in out


def test_stdlib_path_exists(tmp_path):
    f = tmp_path / "存在.txt"
    f.write_text("x", encoding="utf-8")
    fp = f.as_posix()  # 正斜杠，避免 \t 被转义
    out = _run(f'导入 路径\n'
               f'打印(路径.存在("{fp}"))\n'
               f'打印(路径.是文件("{fp}"))\n'
               f'打印(路径.父目录("{fp}"))\n')
    # 标准库薄封装返回 Python bool，显示为 True/False（与 日期.早于 一致）
    assert "True" in out


def test_stdlib_crypto():
    out = _run('导入 加密\n'
               '打印(加密.sha256("你好"))\n'
               '打印(加密.md5("abc"))\n'
               '打印(加密.支持的算法())\n')
    # sha256("你好") 固定值
    assert "670d9743" in out
    # md5("abc") 固定值
    assert "90015098" in out


def test_stdlib_crypto_file(tmp_path):
    f = tmp_path / "数据.txt"
    f.write_text("abc", encoding="utf-8")
    fp = f.as_posix()
    out = _run(f'导入 加密\n打印(加密.文件摘要("{fp}", "md5"))\n')
    assert "90015098" in out  # md5("abc")


def test_stdlib_crypto_bad_algo():
    with pytest.raises(JishiError) as ei:
        run_source('导入 加密\n打印(加密.摘要("x", "不存在的算法"))\n')
    assert "不支持的摘要算法" in str(ei.value)


def test_stdlib_zip(tmp_path):
    import zipfile
    src = tmp_path / "源.txt"
    src.write_text("hello", encoding="utf-8")
    zp = tmp_path / "包.zip"
    zp_p = zp.as_posix()
    src_p = src.as_posix()
    out = _run(f'导入 压缩\n'
               f'压缩.打包("{zp_p}", "{src_p}")\n'
               f'打印(压缩.列出内容("{zp_p}"))\n')
    assert "源.txt" in out


def test_stdlib_zip_roundtrip(tmp_path):
    import zipfile
    src = tmp_path / "源.txt"
    src.write_text("hello", encoding="utf-8")
    zp = tmp_path / "包.zip"
    target = tmp_path / "解压"
    zp_p = zp.as_posix()
    src_p = src.as_posix()
    target_p = target.as_posix()
    _run(f'导入 压缩\n'
         f'压缩.打包("{zp_p}", "{src_p}")\n'
         f'压缩.解压("{zp_p}", "{target_p}")\n')
    assert (target / "源.txt").read_text(encoding="utf-8") == "hello"


def test_stdlib_system():
    out = _run('导入 系统\n'
               '打印(系统.系统名())\n'
               '打印(系统.机器架构())\n')
    assert out.strip() != ""


def test_stdlib_system_env(tmp_path, monkeypatch):
    monkeypatch.setenv("基石测试变量", "值123")
    out = _run('导入 系统\n打印(系统.环境变量("基石测试变量"))\n')
    assert "值123" in out


def test_stdlib_system_exec():
    """执行 echo 类命令（跨平台用 python 已保证存在）。"""
    out = _run('导入 系统\n打印(系统.退出码(["python", "--version"]))\n')
    assert "0" in out


# ---------------------------------------------------------------------------
# 沙箱白名单（14.4）：路径/压缩/系统 默认拦截，加密 放行
# ---------------------------------------------------------------------------

def test_sandbox_blocks_path():
    from jishi.sandbox import SandboxOpts, run_sandboxed
    r = run_sandboxed("导入 路径\n", SandboxOpts())
    assert not r["ok"]


def test_sandbox_blocks_system():
    from jishi.sandbox import SandboxOpts, run_sandboxed
    r = run_sandboxed("导入 系统\n", SandboxOpts())
    assert not r["ok"]


def test_sandbox_allows_crypto():
    from jishi.sandbox import SandboxOpts, run_sandboxed
    r = run_sandboxed('导入 加密\n打印(加密.md5("abc"))\n', SandboxOpts())
    assert r["ok"] and "90015098" in r["stdout"]