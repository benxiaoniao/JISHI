# -*- coding: utf-8 -*-
"""M16.2 发行版构建：用 PyInstaller 打包独立可执行文件。

产出（在 `dist/release/`）：
    jishi-{版本}-{平台}.zip / .tar.gz    发行包
    内含 bin/jishi、bin/jishi-mcp、jsvm.dll、share/、install/uninstall 脚本

构建期依赖：PyInstaller（仅打包用），基石运行时仍零第三方依赖。

用法：
    python tools/build_release.py            # 构建当前平台发行包
    python tools/build_release.py --no-zip   # 只产 onedir，不打 zip（调试）
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jishi.version import VERSION  # noqa: E402


def _platform_tag() -> str:
    import platform
    sys_name = platform.system().lower()
    if sys_name == "windows":
        return "windows-x64"
    if sys_name == "darwin":
        return "macos-arm64" if platform.machine() == "arm64" else "macos-x64"
    return "linux-x64"


def _dll_name() -> str:
    if sys.platform == "win32":
        return "jsvm.dll"
    if sys.platform == "darwin":
        return "libjsvm.dylib"
    return "libjsvm.so"


def _find_cvm_dll() -> Path:
    """定位 C VM 动态库（优先 wheel 预编译目录，其次源码树 cvm/bin）。"""
    for cand in (
        ROOT / "jishi" / "_native" / _dll_name(),
        ROOT / "cvm" / "bin" / _dll_name(),
    ):
        if cand.exists():
            return cand
    raise FileNotFoundError(
        f"找不到 C VM 动态库 {_dll_name()}，请先运行 python cvm/build.py")


def _ensure_native() -> None:
    """把 C VM DLL 复制到 jishi/_native/，供 PyInstaller 与 wheel 共用。"""
    src = _find_cvm_dll()
    dest_dir = ROOT / "jishi" / "_native"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / _dll_name()
    if not dest.exists() or src.stat().st_mtime > dest.stat().st_mtime:
        shutil.copy2(src, dest)
        print(f"  已同步 C VM 动态库 → jishi/_native/{_dll_name()}")


def _add_data_args() -> list[str]:
    """PyInstaller --add-data 参数（Windows 分隔符 `;`，Unix `:`）。"""
    sep = ";" if sys.platform == "win32" else ":"
    args = []
    # 标准库源文件（ai.py 的 _stdlib_api 需要扫描源码）
    args += ["--add-data", f"{ROOT / 'jishi' / 'stdlib'}{sep}jishi/stdlib"]
    # C VM 动态库
    args += ["--add-data", f"{ROOT / 'jishi' / '_native'}{sep}jishi/_native"]
    # 品牌资产
    logo = ROOT / "assets" / "基石_logo.png"
    if logo.exists():
        args += ["--add-data", f"{logo}{sep}assets"]
    return args


def build(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="构建基石发行包")
    p.add_argument("--no-zip", action="store_true", help="只产 onedir 不打包 zip")
    p.add_argument("--clean", action="store_true", help="清理构建缓存")
    args = p.parse_args(argv)

    print(f"== 基石发行版构建 {VERSION} ==")
    _ensure_native()

    work = ROOT / "build" / "release"
    dist = ROOT / "dist" / "release"
    if args.clean:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(dist, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    dist.mkdir(parents=True, exist_ok=True)

    # 用 onedir（启动快、体积适中；onefile 冷启动每次解压，实测慢于预期）
    # 注意：入口用 tools/entry_cli.py（包外绝对导入），避免 PyInstaller 直接
    # 打包包内模块 cli.py 时相对导入失败。
    pyinstaller = [sys.executable, "-m", "PyInstaller",
                   "--noconfirm",
                   "--name", "jishi",
                   "--distpath", str(work),
                   "--workpath", str(work / "build"),
                   "--specpath", str(work),
                   "--console",
                   * _add_data_args(),
                   str(ROOT / "tools" / "entry_cli.py")]

    print("  运行 PyInstaller ...")
    subprocess.run(pyinstaller, cwd=str(ROOT), check=True)

    # MCP Server 也打进同一个目录（复用 jishi 主程序的解释器不可行，
    # 单独打包一个 jishi-mcp 入口）
    subprocess.run(
        [sys.executable, "-m", "PyInstaller",
         "--noconfirm",
         "--name", "jishi-mcp",
         "--distpath", str(work),
         "--workpath", str(work / "build-mcp"),
         "--specpath", str(work),
         "--console",
         * _add_data_args(),
         str(ROOT / "tools" / "entry_mcp.py")],
        cwd=str(ROOT), check=True)

    # 组装发行目录
    tag = _platform_tag()
    pkg_name = f"jishi-{VERSION}-{tag}"
    out = dist / pkg_name
    if out.exists():
        shutil.rmtree(out)
    bin_dir = out / "bin"
    bin_dir.mkdir(parents=True)

    # 把 onedir 产物复制到 bin/
    for exe in ("jishi", "jishi-mcp"):
        src = work / exe
        if not src.exists():
            continue
        if (src / f"{exe}.exe").exists():
            # Windows onedir：复制整个目录
            shutil.copytree(src, bin_dir / exe, dirs_exist_ok=True)
        else:
            shutil.copytree(src, bin_dir / exe, dirs_exist_ok=True)

    # share/ 资源
    share = out / "share"
    share.mkdir(exist_ok=True)
    logo = ROOT / "assets" / "基石_logo.png"
    if logo.exists():
        shutil.copy2(logo, share / "logo.png")
    # 语言卡与系统提示词
    try:
        sys.path.insert(0, str(ROOT))
        from jishi.ai import render_ai_card
        (share / "ai-card.md").write_text(render_ai_card(), encoding="utf-8")
        sp = ROOT / "docs" / "ai" / "system-prompt.md"
        if sp.exists():
            shutil.copy2(sp, share / "system-prompt.md")
    except Exception as e:  # noqa: BLE001
        print(f"  警告：生成离线语言卡失败：{e}")

    # 安装/卸载脚本（16.3）
    _write_install_scripts(out)

    # README 与 LICENSE
    (out / "README.md").write_text(
        f"# 基石 jishi {VERSION}\n\n"
        f"一门像 Python 一样简单易用的中文编程语言。\n\n"
        f"安装：运行 install.{'ps1' if sys.platform == 'win32' else 'sh'}\n"
        f"详见项目 README.md 与 docs/。\n",
        encoding="utf-8")

    # 打包 zip / tar.gz
    if not args.no_zip:
        _make_archive(out, pkg_name)
        # SHA256SUMS
        _write_checksums(dist, pkg_name, tag)

    print(f"\n✅ 发行包生成完成：{out}")
    return 0


def _write_install_scripts(out: Path) -> None:
    """写入安装/卸载脚本（详见 16.3，完整版在 tools 目录）。"""
    # 这里由 tools 目录的脚本模板复制，若无则生成最小版本
    src_scripts = ROOT / "tools"
    for name in ("install.sh", "install.ps1", "uninstall.sh", "uninstall.ps1"):
        src = src_scripts / name
        if src.exists():
            shutil.copy2(src, out / name)


def _make_archive(out: Path, pkg_name: str) -> None:
    parent = out.parent
    if sys.platform == "win32":
        arc = parent / f"{pkg_name}.zip"
        with zipfile.ZipFile(arc, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(out):
                for f in files:
                    full = Path(root) / f
                    rel = full.relative_to(parent)
                    zf.write(full, str(rel))
        print(f"  已打包：{arc}")
    else:
        arc = parent / f"{pkg_name}.tar.gz"
        with tarfile.open(arc, "w:gz") as tf:
            tf.add(out, arcname=pkg_name)
        print(f"  已打包：{arc}")


def _write_checksums(dist: Path, pkg_name: str, tag: str) -> None:
    import hashlib
    arcs = sorted(dist.glob(f"{pkg_name}.*"))
    lines = []
    for a in arcs:
        h = hashlib.sha256(a.read_bytes()).hexdigest()
        lines.append(f"{h}  {a.name}")
    (dist / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("  已生成 SHA256SUMS")


if __name__ == "__main__":
    sys.exit(build(sys.argv[1:]))
