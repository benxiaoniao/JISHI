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

# Windows 控制台/CI 的 stdout 默认可能是非 UTF-8，打印中文会 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

from oracle.jishi.version import VERSION  # noqa: E402


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


def _find_iscc() -> "Path | None":
    r"""定位 Inno Setup 编译器 ISCC.exe（Windows 安装程序用）。

    覆盖三种常见安装位置：系统级 Program Files（Inno Setup 6/7）、
    用户级 %LOCALAPPDATA%\Programs（`/CURRENTUSER` 安装）、以及 PATH。
    优先用较新版本（7 优于 6）。
    """
    if sys.platform != "win32":
        return None
    cands: list[Path] = []
    for pf in (os.environ.get("ProgramFiles(x86)"),
               os.environ.get("ProgramFiles")):
        if pf:
            for ver in ("Inno Setup 7", "Inno Setup 6"):
                cands.append(Path(pf) / ver / "ISCC.exe")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        for ver in ("Inno Setup 7", "Inno Setup 6"):
            cands.append(Path(local) / "Programs" / ver / "ISCC.exe")
    for c in cands:
        if c.exists():
            return c
    exe = shutil.which("ISCC") or shutil.which("iscc")
    return Path(exe) if exe else None


def _build_installer(out: Path, dist: Path) -> "Path | None":
    """用 Inno Setup 编译 Windows 安装程序；找不到 ISCC 则跳过并提示。

    M19.1：把「压缩包」升级为真正的安装程序（开始菜单 / PATH / 卸载）。
    """
    if sys.platform != "win32":
        return None
    iscc = _find_iscc()
    if iscc is None:
        print("  跳过 Windows 安装程序：未找到 Inno Setup（ISCC.exe）。\n"
              "    安装后可自动启用：winget install -e --id JRSoftware.InnoSetup")
        return None
    iss = ROOT / "tools" / "installer.iss"
    print("  编译 Windows 安装程序（Inno Setup）...")
    args = [str(iscc),
            f"/DSourceDir={out}",
            f"/DAppVersion={VERSION}",
            f"/DOutputDir={dist}"]
    # 图标路径由编译期确定（SetupIconFile），所以这里传**绝对路径**：.iss 自身
    # 的编码在英文 Windows 上可能被当成 ANSI，中文相对路径会变乱码导致编译失败。
    if ICON_WIN.exists():
        args.append(f"/DMyIconFile={ICON_WIN}")
    args.append(str(iss))
    subprocess.run(args, cwd=str(ROOT), check=True)
    inst = dist / f"jishi-{VERSION}-windows-x64-setup.exe"
    if inst.exists():
        print(f"  已生成安装程序：{inst.name}")
        return inst
    print("  警告：安装程序似乎未生成，请检查 Inno Setup 输出")
    return None


#: 各平台的图标资产。Windows 用 .ico（可执行文件内嵌图标只认 ico），
#: macOS 用 .icns，Linux 的可执行文件**不内嵌图标**（那是桌面项的事，
#: 见下面写出的 .desktop），所以不传给 PyInstaller。
ICON_WIN = ROOT / "assets" / "基石.ico"
ICON_MAC = ROOT / "assets" / "基石.icns"


def _icon_args() -> list[str]:
    """PyInstaller 的 `--icon` 参数（按平台挑文件；没有就返回空）。

    为什么 Linux 不给：ELF 可执行文件没有图标资源的概念，PyInstaller 会
    在参数不适用时警告并忽略。Linux 的图标靠 `share/基石.desktop` 指到
    PNG（安装脚本会把桌面项放到 ~/.local/share/applications）。
    """
    if sys.platform == "win32":
        ic = ICON_WIN
    elif sys.platform == "darwin":
        ic = ICON_MAC
    else:
        return []
    if not ic.exists():
        print(f"  提示：没找到图标 {ic.name}——先跑 python tools/make_logo.py 生成。")
        return []
    return ["--icon", str(ic)]


def _find_cvm_dll() -> Path:
    """定位 C VM 动态库（优先 wheel 预编译目录，其次源码树 cvm/bin）。"""
    for cand in (
        ROOT / "oracle" / "jishi" / "_native" / _dll_name(),
        ROOT / "cvm" / "bin" / _dll_name(),
    ):
        if cand.exists():
            return cand
    raise FileNotFoundError(
        f"找不到 C VM 动态库 {_dll_name()}，请先运行 python cvm/build.py")


def _ensure_native() -> None:
    """把 C VM DLL 复制到 oracle/jishi/_native/，供 PyInstaller 与 wheel 共用。"""
    src = _find_cvm_dll()
    dest_dir = ROOT / "oracle" / "jishi" / "_native"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / _dll_name()
    if not dest.exists() or src.stat().st_mtime > dest.stat().st_mtime:
        shutil.copy2(src, dest)
        print(f"  已同步 C VM 动态库 → oracle/jishi/_native/{_dll_name()}")


def _add_data_args() -> list[str]:
    """PyInstaller --add-data 参数（Windows 分隔符 `;`，Unix `:`）。"""
    sep = ";" if sys.platform == "win32" else ":"
    args = []
    # 标准库源文件（ai.py 的 _stdlib_api 需要扫描源码）
    args += ["--add-data", f"{ROOT / 'jishi' / 'stdlib'}{sep}oracle/jishi/stdlib"]
    # M52：**用基石写的标准库**（`stdlib-jishi/*.jsh`）。`jishilib.lib_dir()`
    # 会优先找 `sys._MEIPASS` 下的这份，少了它打包版一 `导入 统计` 就报
    # 「没有找到标准库模块」。
    args += ["--add-data", f"{ROOT / 'jishi' / 'stdlib-jishi'}{sep}oracle/jishi/stdlib-jishi"]
    # C VM 动态库
    args += ["--add-data", f"{ROOT / 'jishi' / '_native'}{sep}oracle/jishi/_native"]
    # 品牌资产
    logo = ROOT / "assets" / "基石_logo.png"
    if logo.exists():
        args += ["--add-data", f"{logo}{sep}assets"]
    return args


def _rust_exe_name() -> str:
    return "jishi-rs.exe" if sys.platform == "win32" else "jishi-rs"


def _build_rust_into(bin_dir: Path) -> None:
    """`cargo build --release` → **`bin/jishi(.exe)` 单文件**（R8.3）。

    ⚠️ 产物在仓库里叫 `jishi-rs`（`jishi` 这个名字还被 Python 版占着，**R8.4 才交换**）；
    **发行包里一律改名成 `jishi`** —— 用户看到的是「基石」，不是「基石（Rust 版）」。
    等 R8.4 做完，仓库里的名字也会跟着换。
    """
    crate = ROOT / "rust"
    print("  运行 cargo build --release --locked ...")
    # 🔴 `--locked`（M176）：**发行包里的依赖版本必须钉死**。`Cargo.lock` 已经
    #    进仓库了，加上这个参数，锁与 `Cargo.toml` 对不上会当场报错 —— 而不是
    #    在用户机器上装到一个没人测过的补丁版。
    subprocess.run(["cargo", "build", "--release", "--locked"], cwd=str(crate), check=True)
    src = crate / "target" / "release" / _rust_exe_name()
    if not src.exists():
        raise SystemExit(f"cargo 产物不存在：{src}")
    dst_name = _build_exe_name()
    dst = bin_dir / dst_name
    shutil.copy2(src, dst)
    if sys.platform != "win32":
        dst.chmod(0o755)
    n = dst.stat().st_size
    print(f"  已放入 bin/{dst_name}（{n:,} 字节 = {n / 1024 / 1024:.2f} MB）")

    # 🔴 M174：**必须带上 `WebView2Loader.dll`**。
    #
    # wry 在 Windows 上链的是 `WebView2Loader.dll.lib`（导入库）⇒ exe 里有一条
    # 对 `WebView2Loader.dll` 的**动态**依赖，而它既不在 exe 旁边、也不在 System32。
    # 少了它，用户装完双击就是「没反应、也没有报错」（进程一启动就 0xC0000135）。
    #
    # DLL 由 `rust/build.rs` 在构建后拷到 `target/release/`，这里跟着一起进包。
    if sys.platform == "win32":
        带 = crate / "target" / "release" / "WebView2Loader.dll"
        if 带.is_file():
            shutil.copy2(带, bin_dir / "WebView2Loader.dll")
            print(f"  已放入 bin/WebView2Loader.dll（{带.stat().st_size:,} 字节）")
        else:
            print("  ⚠️ 没找到 WebView2Loader.dll —— 原生窗口在用户机器上会启动失败")


def _build_wasm_into(bin_dir: Path) -> None:
    """构建 wasm 引擎并把两份素材放进 `bin/`（M174）。

    ## 为什么发行包必须带它

    `窗口` 打开页面时，页面要 `import "/__jishi/jishi-wasm.js"` 再
    `JishiWasm.load("/__jishi/jishi_wasm.wasm")` —— 那两样由 `窗口` 的小服务
    从**本机目录**喂出去。缺了它们页面一片空白，而且**一行报错都没有**
    （浏览器只会在 devtools 里记一条 404）。

    ⚠️ 在此之前（M173 起）发行包是**不带**这两份的 —— 也就是说装了基石的用户
    开出来的窗口是白的。判据里那条 `test_服务能把页面和引擎素材喂出去` 在 CI 上
    404，把这个洞暴露了出来。

    ## 为什么先构建而不是拷 `site/`

    `site/` 是 `.gitignore` 掉的构建产物，CI 的干净检出里没有。
    所以这里**现编**（本机约 30 秒）—— 顺带保证发行包里的 wasm 与这版源码一致，
    而不是某个陈旧副本。
    """
    print("  构建 wasm 引擎（窗口要用）...")
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "build_wasm.py")],
                       cwd=str(ROOT), capture_output=True, text=True)
    if r.returncode != 0:
        # 缺 wasm target 是最常见的原因，把 build_wasm 自己的提示带出来
        print(r.stdout[-800:])
        print(r.stderr[-800:], file=sys.stderr)
        raise SystemExit("构建 wasm 失败（上面有原因；多半是 `rustup target add wasm32-unknown-unknown`）")

    n = 0
    for 名 in ("jishi-wasm.js", "jishi_wasm.wasm"):
        源 = ROOT / "site" / 名
        if not 源.is_file():
            raise SystemExit(f"wasm 构建完了却没有 {源} —— build_wasm.py 的行为变了？")
        shutil.copy2(源, bin_dir / 名)
        n += 源.stat().st_size
    print(f"  已放入 bin/jishi-wasm.js + bin/jishi_wasm.wasm（{n:,} 字节）")


def _build_python_into(bin_dir: Path, work: Path) -> None:
    """PyInstaller onedir → `bin/oracle/jishi/` + `bin/jishi-mcp/`（**R8.4 前保留**）。

    ⚠️ 这套布局是**嵌套**的（`bin/oracle/jishi/jishi.exe`），而 `install.sh` / `install.ps1`
    的自检写的是**平铺**的 `bin/jishi` —— 也就是说**这条路上的 zip/tar.gz 装完自检
    是失败的**（见 `build()` 里那段说明）。换成 rust 引擎就没有这个问题；
    这里保留只为「R8.4 之前还能发一版 Python 包」。
    """
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
                   * _icon_args(),
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
         * _icon_args(),
         str(ROOT / "tools" / "entry_mcp.py")],
        cwd=str(ROOT), check=True)

    for exe in ("jishi", "jishi-mcp"):
        src = work / exe
        if src.exists():
            shutil.copytree(src, bin_dir / exe, dirs_exist_ok=True)


def _make_parser() -> argparse.ArgumentParser:
    """单独拎出来，好让判据能检查「默认引擎是哪条路」。"""
    p = argparse.ArgumentParser(description="构建基石发行包")
    p.add_argument("--engine", choices=("rust", "python"), default="rust",
                   help="发行物的引擎：rust（默认，cargo 单二进制）或 python（PyInstaller，R8.4 前保留）")
    p.add_argument("--no-zip", action="store_true", help="只产 onedir 不打包 zip")
    p.add_argument("--no-installer", action="store_true",
                   help="不生成 Windows 安装程序（默认在 Windows 上自动尝试）")
    p.add_argument("--clean", action="store_true", help="清理构建缓存")
    return p


def _build_exe_name() -> str:
    """发行包里那个可执行文件名（`jishi` / `jishi.exe`）—— **平铺在 `bin/` 下**。"""
    return "jishi.exe" if sys.platform == "win32" else "jishi"


def build(argv: list[str]) -> int:
    args = _make_parser().parse_args(argv)

    print(f"== 基石发行版构建 {VERSION} ==")
    engine = args.engine
    #: R8.3（2026-10-08）：发行包默认改用 **cargo 产物**（`rust/` 那个单二进制）。
    #:
    #: 🔴 **顺带修掉一个真 bug**：以前 PyInstaller 是 onedir，产物落在
    #: `bin/oracle/jishi/jishi.exe`（**套了一层目录**），而 `install.sh` / `install.ps1`
    #: 的自检写的是 `bin/jishi` / `bin\jishi.exe` —— 于是
    #: **Linux/macOS 装完自检必失败、PATH 也指错目录（bin/ 里是个目录不是可执行文件）**。
    #: Windows 那边因为推荐走 Inno Setup（`.iss` 里写的是嵌套路径）才没暴露。
    #: 换成单二进制之后 `bin/jishi`（带 `.exe`）本身就是文件，安装脚本**不用改就对了**。
    #:
    #: ⚠️ `--engine python`（PyInstaller）**保留到 R8.4**：Rust 宿主还差
    #: `医生` / `新项目` / `教程` / `-i`（见 `docs/路线图.md` §11.4），
    #: 所以它**还不能**当唯一的发行物。
    if engine == "python":
        _ensure_native()

    work = ROOT / "build" / "release"
    dist = ROOT / "dist" / "release"
    if args.clean:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(dist, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    dist.mkdir(parents=True, exist_ok=True)

    # 组装发行目录
    tag = _platform_tag()
    pkg_name = f"jishi-{VERSION}-{tag}"
    out = dist / pkg_name
    if out.exists():
        shutil.rmtree(out)
    bin_dir = out / "bin"
    bin_dir.mkdir(parents=True)

    if engine == "rust":
        _build_rust_into(bin_dir)
    else:
        _build_python_into(bin_dir, work)
    # 🔴 **引擎素材跟着发行包走**（M174）：`窗口` 打开页面时要把
    # `jishi-wasm.js` + `jishi_wasm.wasm` 喂给浏览器/WebView。少了它，
    # 窗口照样能开，但页面里的 `import "/__jishi/…"` 全部 404 ⇒ **白窗口、没报错**。
    # 位置放在 `bin/`（也就是 jishi 可执行文件旁边）—— `窗口` 找素材的第一站。
    _build_wasm_into(bin_dir)

    # share/ 资源
    share = out / "share"
    share.mkdir(exist_ok=True)
    logo = ROOT / "assets" / "基石_logo.png"
    if logo.exists():
        shutil.copy2(logo, share / "logo.png")
    # 图标资产随发行包一起走：Windows 安装程序要用 share/基石.ico 当卸载项
    # 与快捷方式图标；macOS/Linux 用 PNG/ICNS（见 M35）
    for ic, name in ((ICON_WIN, "基石.ico"), (ICON_MAC, "基石.icns")):
        if ic.exists():
            shutil.copy2(ic, share / name)
    _write_desktop_entry(out)
    # 语言卡与系统提示词
    try:
        sys.path.insert(0, str(ROOT))
        from oracle.jishi.ai import render_ai_card
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
    # 许可证正文随发行包一起走（以前这行注释写着「README 与 LICENSE」，
    # 但实际只写了 README —— 发行包里根本没带 LICENSE）。
    _license = ROOT / "LICENSE"
    if _license.exists():
        (out / "LICENSE").write_text(_license.read_text(encoding="utf-8"),
                                     encoding="utf-8")

    # 打包 zip / tar.gz
    if not args.no_zip:
        _make_archive(out, pkg_name)
        # Windows 安装程序（M19.1）：把压缩包升级为双击可装的 setup.exe
        installer = None
        if not args.no_installer:
            installer = _build_installer(out, dist)
        # SHA256SUMS（发行包 + 安装程序）
        _write_checksums(dist, pkg_name, tag, installer)

    print(f"\n✅ 发行包生成完成：{out}")
    return 0


def _write_desktop_entry(out: Path) -> None:
    """写 Linux 桌面项（`share/基石.desktop`）。

    Linux 的可执行文件不内嵌图标，语言「有图标」这件事只能靠桌面项体现
    （菜单、文件管理器、任务栏）。`Icon=` 写**绝对路径占位**：桌面项要求
    绝对路径，而安装位置由用户决定，所以同时写一份 `%f` 友好的版本，
    并在 install.sh 里按实际安装目录替换 `__INSTALL_DIR__`。

    不做 `.jsh` 文件关联：那要在用户桌面数据库里注册 MIME 类型，
    各家发行版差异大，收益不如风险。要关联的用户自己加一行即可。
    """
    target = out / "share" / "基石.desktop"
    target.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=基石 jishi\n"
        "Comment=一门像 Python 一样简单易用的中文编程语言\n"
        "Exec=__INSTALL_DIR__/bin/jishi %f\n"
        "Icon=__INSTALL_DIR__/share/logo.png\n"
        "Terminal=true\n"
        "Categories=Development;Languages;\n"
        "Keywords=中文;编程;jishi;\n",
        encoding="utf-8")
    print("  已写入 Linux 桌面项：share/基石.desktop")


def _write_install_scripts(out: Path) -> None:
    """写入安装/卸载脚本，并把版本占位符替换为实际版本（M16.3 / M19）。"""
    src_scripts = ROOT / "tools"
    for name in ("install.sh", "install.ps1", "uninstall.sh", "uninstall.ps1"):
        src = src_scripts / name
        if not src.exists():
            continue
        text = src.read_text(encoding="utf-8")
        # 模板里的 __VERSION__ 占位符 → 当前版本
        text = text.replace("__VERSION__", VERSION)
        (out / name).write_text(text, encoding="utf-8")


#: 归档里所有条目的时间戳**写死**（2020-01-01）。
#:
#: 🔴 两个理由（M176 实测踩过第一个）：
#: ① `zipfile.write` 会照抄文件的 mtime，而我们的 `WebView2Loader.dll` 是从
#:    cargo 的产物目录复制过来的，mtime 落在 1970 年代 ⇒
#:    `ValueError: ZIP does not support timestamps before 1980`，**整个打包当场挂**。
#: ② 交付物要**可复现**：同一个版本打两次，字节应当一样（M109 / M168 / M171 的老规矩）。
_归档时间 = (2020, 1, 1, 0, 0, 0)


def _遍历(out: Path):
    """按**固定顺序**列出 `out` 下的所有文件（相对 `out.parent`）。

    排序是为了可复现：`os.walk` 的顺序取决于文件系统。
    """
    for root, dirs, files in os.walk(out):
        dirs.sort()
        for f in sorted(files):
            full = Path(root) / f
            yield full, str(full.relative_to(out.parent)).replace(os.sep, "/")


def _make_archive(out: Path, pkg_name: str, 用zip: bool = None) -> None:
    """把 `out` 打包成 zip（Windows）或 tar.gz（其他平台）。

    `用zip` 只给判据用（两个分支都要能测到 —— tar 那条在 Windows 上跑不到）。
    """
    if 用zip is None:
        用zip = sys.platform == "win32"
    parent = out.parent
    if 用zip:
        arc = parent / f"{pkg_name}.zip"
        with zipfile.ZipFile(arc, "w", zipfile.ZIP_DEFLATED) as zf:
            for full, rel in _遍历(out):
                info = zipfile.ZipInfo(rel, _归档时间)
                info.compress_type = zipfile.ZIP_DEFLATED
                # 644；可执行文件给 755（解压后还能直接跑）
                info.external_attr = (
                    0o755 if full.suffix in (".sh", ".exe") or full.stat().st_mode & 0o111
                    else 0o644) << 16
                zf.writestr(info, full.read_bytes())
        print(f"  已打包：{arc}")
    else:
        arc = parent / f"{pkg_name}.tar.gz"
        with tarfile.open(arc, "w:gz") as tf:
            for full, rel in _遍历(out):
                info = tf.gettarinfo(str(full), arcname=rel)
                info.mtime = 0            # tar 不限制 1980，但同样为了可复现
                with open(full, "rb") as fh:
                    tf.addfile(info, fh)
        print(f"  已打包：{arc}")


def _write_checksums(dist: Path, pkg_name: str, tag: str,
                     installer: "Path | None" = None) -> None:
    import hashlib
    arcs = sorted(dist.glob(f"{pkg_name}.*"))
    if installer is not None and installer.exists():
        arcs.append(installer)
    lines = []
    for a in arcs:
        h = hashlib.sha256(a.read_bytes()).hexdigest()
        lines.append(f"{h}  {a.name}")
    (dist / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("  已生成 SHA256SUMS")


if __name__ == "__main__":
    sys.exit(build(sys.argv[1:]))
