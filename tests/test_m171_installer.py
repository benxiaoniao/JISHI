# -*- coding: utf-8 -*-
"""M171 · 自解压安装器：`打包.安装包` + `jishi-pack-stub`。

## 这一组判据压在哪

「能打个包」和「双击能装上」是两件事。这一组验的是后者：

1. **尾部索引对得上** —— `[存根][包][偏移][长度][JISHIPK1]`，存根靠它定位载荷。
   写错一个字段，装出来的就是半截文件（而且不会有任何报错）。
2. **装 → 验 → 卸 一条龙** —— 文件到位、清单也留在安装目录、注册表里有卸载项；
   卸完**目录 / 注册表项**都不留。
3. **坏包不落盘** —— 内容被改过的包，安装器必须**在动盘之前**就拒绝。
   （先装一半再报错，用户机器上就留下半套文件了。）

⚠️ 注册表那几条只在 Windows 上跑；其余平台上存根只有「解包 / 卸载文件」这一半。
"""

import os
import struct
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import rust_exe_path                                # noqa: E402

#: 存根和 jishi-rs 编在同一个目录（`cargo build --release` 两个 bin 一起出）。
STUB = rust_exe_path().with_name(
    "jishi-pack-stub" + (".exe" if os.name == "nt" else ""))

#: 🔴 **自解压安装器是 Windows 专属**（M174 修）。
#:
#: 这一整份判据验的是「双击 setup.exe → 装进 %LOCALAPPDATA% → 写注册表卸载项 →
#: 开始菜单快捷方式 → 卸载干净」——**每一样都是 Windows 概念**：
#: 存根是 PE 文件、卸载项在 `HKCU\\...\\Uninstall`、快捷方式是 `.lnk`。
#:
#: Linux / macOS 上 `jishi-pack-stub` 也会被编出来（同一个 `cargo build`），
#: 所以光判「存根在不在」放不住 —— 那边的产物是 ELF / Mach-O，
#: 判据跑到「存根不是 PE 文件？」和「跑 .exe」就挂了。
#: 那两平台的安装方式是 `tar.gz` + `install.sh`，由 `test_m172_pack_cmd` 覆盖。
#:
#: ⚠️ 教训（和 `packstub.rs` 的 cfg 是同一类）：**本地只验 Windows ⇒ 平台专属的
#: 判据没加平台门槛**，Linux/macOS 的 CI 就一直红。凡是「只在一个平台成立」的判据，
#: 都要把它说在 `skipif` 里。
pytestmark = pytest.mark.skipif(
    not STUB.is_file() or sys.platform != "win32",
    reason="自解压安装器只在 Windows 上有意义（Linux/macOS 用 tar.gz + install.sh）；"
           "没编 jishi-pack-stub 时也跳过（cargo build --release）")

选项 = '{"名字": "%s", "版本": "1.0.0", "平台": "windows-x64", "入口": "bin/演示.exe"}'


def _造树(tmp_path):
    源 = tmp_path / "发布"
    (源 / "bin").mkdir(parents=True)
    (源 / "share").mkdir(parents=True)
    (源 / "bin" / "演示.exe").write_bytes(b"MZ fake")
    (源 / "share" / "读我.txt").write_text("帮助", encoding="utf-8")
    return 源


def _跑(源, tmp_path, exe=None):
    import subprocess

    (tmp_path / "程序.jsh").write_text(源, encoding="utf-8", newline="\n")
    命令 = ([str(exe)] if exe else [sys.executable, "-m", "oracle.jishi.cli"])
    r = subprocess.run(命令 + ["程序.jsh"], cwd=str(tmp_path),
                       capture_output=True, timeout=300, input=b"")
    return (r.stdout + r.stderr).decode("utf-8", "replace"), r.returncode


def _造安装包(tmp_path, 名字):
    _造树(tmp_path)
    程序 = f'''导入 打包
令 选 = {选项 % 名字}
令 包 = 打包.生成("发布", "出", 选)
打印(打包.安装包(包, "{STUB.as_posix()}"))
'''
    出, 码 = _跑(程序, tmp_path)
    assert 码 == 0, 出[:500]
    return tmp_path / "出" / f"{名字}-1.0.0-windows-x64-setup.exe"


def _存根(setup, *参数, cwd=None):
    import subprocess

    r = subprocess.run([str(setup)] + list(参数), cwd=str(cwd) if cwd else None,
                       capture_output=True, timeout=300, input=b"")
    return (r.stdout + r.stderr).decode("utf-8", "replace"), r.returncode


def _注册表读(名字):
    import winreg

    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                       rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{名字}")
    out = {}
    i = 0
    while True:
        try:
            n, v, _ = winreg.EnumValue(k, i)
        except OSError:
            break
        out[n] = v
        i += 1
    k.Close()
    return out


def _注册表有(名字):
    import winreg

    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{名字}")
    except OSError:
        return False
    k.Close()
    return True


def _注册表删(名字):
    """测试收尾：卸载项 + 开始菜单快捷方式都清掉。

    ⚠️ 这两样**是写到用户真实环境里的**（HKCU 与 `%APPDATA%\\…\\Start Menu`），
    所以每个用例的 `finally` 都必须调它 —— 判据把自己造成的痕迹收拾干净，
    否则跑几轮测试，用户的开始菜单里会攒一排「基石自检…」。
    """
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                         rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{名字}")
    except OSError:
        pass
    try:
        _快捷方式路径(名字).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 一、尾部索引
# ---------------------------------------------------------------------------

def test_尾部索引指得准载荷(tmp_path):
    """偏移/长度/魔数三个字段**任何一个**写错，装出来的就是半截文件。"""
    setup = _造安装包(tmp_path, "基石打包索引自检")
    b = setup.read_bytes()
    assert b[-8:] == b"JISHIPK1", b[-8:]
    off, ln = struct.unpack("<QQ", b[-24:-8])
    assert off + ln + 24 == len(b), (off, ln, len(b))
    # 那一段必须真的是个 zip，而且里面有清单
    z = zipfile.ZipFile(__import__("io").BytesIO(b[off:off + ln]))
    assert "清单.json" in z.namelist(), z.namelist()
    # 存根那一段不能带载荷（尾部索引得在 zip 后面，不是文件最前面）
    assert b[:2] == b"MZ", "存根不是 PE 文件？"


# ---------------------------------------------------------------------------
# 二、装 / 验 / 卸
# ---------------------------------------------------------------------------

def test_安装后文件到位且清单留在安装目录(tmp_path):
    名字 = "基石打包装检"
    装 = tmp_path / "装"
    setup = _造安装包(tmp_path, 名字)
    try:
        出, 码 = _存根(setup, "--目标", str(装))
        assert 码 == 0, 出[:500]
        assert (装 / "bin" / "演示.exe").read_bytes() == b"MZ fake"
        assert (装 / "share" / "读我.txt").read_text(encoding="utf-8") == "帮助"
        assert (装 / "清单.json").is_file(), "清单要留在安装目录（卸载/升级靠它）"
        assert (装 / "uninstall.exe").is_file(), "卸载程序没写出来"
        assert (装 / "uninstall.exe").stat().st_size < setup.stat().st_size, (
            "卸载程序应当只是存根那一段（不含载荷）")
        assert "完成" in 出, 出[:400]
    finally:
        if os.name == "nt":
            _注册表删(名字)


@pytest.mark.skipif(os.name != "nt", reason="注册表只有 Windows 有")
def test_装完有卸载项且指向uninstall_exe(tmp_path):
    名字 = "基石打包注册表自检"
    装 = tmp_path / "装"
    setup = _造安装包(tmp_path, 名字)
    try:
        出, 码 = _存根(setup, "--目标", str(装))
        assert 码 == 0, 出[:500]
        项 = _注册表读(名字)
        assert 项.get("DisplayName") == 名字, 项
        assert 项.get("DisplayVersion") == "1.0.0", 项
        assert 项.get("InstallLocation") == str(装), 项
        assert "uninstall.exe" in 项.get("UninstallString", ""), 项
        assert "bin\\演示.exe" in 项.get("DisplayIcon", ""), 项
    finally:
        _注册表删(名字)


@pytest.mark.skipif(os.name != "nt", reason="注册表只有 Windows 有")
def test_卸载后目录和注册表项都不留(tmp_path):
    名字 = "基石打包卸载自检"
    装 = tmp_path / "装"
    setup = _造安装包(tmp_path, 名字)
    try:
        _存根(setup, "--目标", str(装))
        assert 装.is_dir() and _注册表有(名字)

        出, 码 = _存根(装 / "uninstall.exe", "--卸载")
        assert 码 == 0, 出[:400]
        assert not 装.exists(), f"目录没删干净：{list(装.rglob('*'))[:5]}"
        assert not _注册表有(名字), "卸载项还在（设置 → 应用 里会留一条空的）"
    finally:
        _注册表删(名字)


def test_卸载时用户自己放进去的文件不动(tmp_path):
    """卸载**只删清单里的东西** —— 用户往安装目录里放的文件不该被顺手清掉。"""
    名字 = "基石打包用户文件自检"
    装 = tmp_path / "装"
    setup = _造安装包(tmp_path, 名字)
    try:
        _存根(setup, "--目标", str(装))
        (装 / "我自己的笔记.txt").write_text("别删我", encoding="utf-8")
        出, 码 = _存根(装 / "uninstall.exe", "--卸载")
        assert 码 == 0, 出[:400]
        assert (装 / "我自己的笔记.txt").read_text(encoding="utf-8") == "别删我"
        assert not (装 / "bin" / "演示.exe").exists()
    finally:
        if os.name == "nt":
            _注册表删(名字)


# ---------------------------------------------------------------------------
# 三、坏包不许落盘
# ---------------------------------------------------------------------------

def test_内容被改过的包一个文件都不装(tmp_path):
    """装一半再报错 = 用户机器上留半套文件。所以**先核对完再动盘**。"""
    名字 = "基石打包坏包自检"
    setup = _造安装包(tmp_path, 名字)
    b = setup.read_bytes()
    off, ln = struct.unpack("<QQ", b[-24:-8])

    坏 = zipfile.ZipFile(__import__("io").BytesIO(b[off:off + ln]))
    条目们 = [(n, 坏.read(n)) for n in 坏.namelist() if not n.endswith("/")]
    坏.close()
    条目们 = [(n, (b"BAD!" if n == "bin/演示.exe" else d)) for n, d in 条目们]
    缓冲 = __import__("io").BytesIO()
    with zipfile.ZipFile(缓冲, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in 条目们:
            z.writestr(n, d)
    新载荷 = 缓冲.getvalue()

    尾 = struct.pack("<QQ", off, len(新载荷)) + b"JISHIPK1"
    (tmp_path / "坏.exe").write_bytes(b[:off] + 新载荷 + 尾)

    装 = tmp_path / "装"
    出, 码 = _存根(tmp_path / "坏.exe", "--目标", str(装))
    assert 码 == 1, 出[:400]
    assert "坏的" in 出 and "什么都没装" in 出, 出[:500]
    assert not 装.exists(), "坏包不该留下任何目录"


def test_校验模式不动盘(tmp_path):
    setup = _造安装包(tmp_path, "基石打包校验自检")
    出, 码 = _存根(setup, "--校验")
    assert 码 == 0, 出[:300]
    assert "校验通过" in 出, 出[:300]
    assert not (tmp_path / "基石打包校验自检").exists()


def test_静默模式不打印进度(tmp_path):
    setup = _造安装包(tmp_path, "基石打包静默自检")
    装 = tmp_path / "装"
    try:
        出, 码 = _存根(setup, "--目标", str(装), "/S")
        assert 码 == 0, 出[:300]
        assert 出.strip() == "", f"静默模式不该有输出：{出[:200]}"
        assert (装 / "bin" / "演示.exe").is_file()
    finally:
        if os.name == "nt":
            _注册表删("基石打包静默自检")


def test_不是安装包要说清楚(tmp_path):
    """直接跑存根本身（没接载荷）要给出人话，而不是崩。"""
    出, 码 = _存根(STUB)
    assert 码 == 1, 出[:300]
    assert "没有安装内容" in 出, 出[:300]


# ---------------------------------------------------------------------------
# 四、开始菜单快捷方式（M172 补上 M171 的欠账）
# ---------------------------------------------------------------------------

def _快捷方式路径(名字):
    import os as _os
    基 = _os.environ.get("APPDATA") or str(
        __import__("pathlib").Path.home() / "AppData" / "Roaming")
    return (__import__("pathlib").Path(基) / "Microsoft" / "Windows"
            / "Start Menu" / "Programs" / f"{名字}.lnk")


@pytest.mark.skipif(os.name != "nt", reason="开始菜单只有 Windows 有")
def test_装完开始菜单里有快捷方式且指得准(tmp_path):
    """`.lnk` 是调系统 `IShellLink` 写的，再用同一个接口读回来 —— 验的是
    「**系统自己认不认**」，而不是「我们写进去的字节像不像」。"""
    名字 = "基石自检快捷方式"
    装 = tmp_path / "装"
    setup = _造安装包(tmp_path, 名字)
    lnk = _快捷方式路径(名字)
    try:
        出, 码 = _存根(setup, "--目标", str(装))
        assert 码 == 0, 出[:500]
        assert lnk.is_file(), f"开始菜单里没有 {lnk}"
        读回, 码2 = _存根(setup, "--解析快捷方式", str(lnk))
        assert 码2 == 0, 读回[:300]
        目标 = Path(读回.split("目标：")[-1].strip())
        assert 目标 == 装 / "bin" / "演示.exe", 读回
    finally:
        if os.name == "nt":
            _注册表删(名字)
            try:
                lnk.unlink()
            except OSError:
                pass


@pytest.mark.skipif(os.name != "nt", reason="开始菜单只有 Windows 有")
def test_卸载会收走快捷方式(tmp_path):
    名字 = "基石自检快捷方式卸"
    装 = tmp_path / "装"
    setup = _造安装包(tmp_path, 名字)
    lnk = _快捷方式路径(名字)
    try:
        _存根(setup, "--目标", str(装))
        assert lnk.is_file()
        出, 码 = _存根(装 / "uninstall.exe", "--卸载")
        assert 码 == 0, 出[:400]
        assert not lnk.exists(), "卸载后开始菜单里还留着一个点了没反应的图标"
    finally:
        _注册表删(名字)
        try:
            lnk.unlink()
        except OSError:
            pass
