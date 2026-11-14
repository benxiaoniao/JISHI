# -*- coding: utf-8 -*-
"""M188 · 打包三连：三个示例都能出包，**解开的包真能开窗**。

## 为什么这条判据不能只看「命令退出码 0」

`打包` 最典型的坏法全是**静默**的：

| 坏法 | 表现 |
|---|---|
| 素材没跟进去（`.png` / `.wav`） | 游戏起来了，但没有图、没有声音 |
| `jishi-wasm.js` / `jishi_wasm.wasm` 没带 | **白窗口**，一行报错都没有（M173 的教训） |
| 页面路径按 cwd 写死 | 装到 `app/` 下就「找不到页面」 |

所以这里做三件事：**看包里有什么**、**解开之后跑起来**、**把入口名验对**。

## 判据分级

- 结构类（包里有哪些文件、`install.sh` 里的入口名、安装包的尾部格式）——
  到处都能跑；
- **真开窗**那条要桌面（`JISHI_NO_GUI=1` 跳过，CI 上是空白，见 CHANGELOG）。

📌 打包出来的 `bin/jishi.exe` 有 7 MB 多，三个示例各打一次 ≈ 一秒级，可以接受。
"""

import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                            # noqa: E402

例子 = ROOT / "examples" / "desktop"
素材 = ROOT / "site"

#: (名字, 入口, 这个示例要带的文件)
游戏们 = [
    ("打砖块", "打砖块.jsh",
     ["打砖块.html", "打砖块_游戏.jsh", "游戏启动.js", "碰.wav"]),
    ("贪吃蛇", "贪吃蛇.jsh",
     ["贪吃蛇.html", "贪吃蛇_游戏.jsh", "游戏启动.js", "碰.wav"]),
    ("平台跳跃", "平台跳跃.jsh",
     ["平台跳跃.html", "平台跳跃_游戏.jsh", "游戏启动.js", "瓦片图.png", "小人.png"]),
]


def _跳(理由: str):
    pytest.skip(理由)


@pytest.fixture(scope="module")
def 引擎目录(tmp_path_factory) -> Path:
    """搭一个「装好的基石」：exe + wasm 素材 + WebView2Loader.dll。

    `打包 --自带运行时` 要求 `--引擎` 指到这个目录（它会把这三样搬进 `bin/`）。
    """
    exe = rust_exe_path()
    if not exe.is_file():
        _跳("没有 Rust 宿主（cargo build --release）")
    for 名 in ("jishi-wasm.js", "jishi_wasm.wasm"):
        if not (素材 / 名).is_file():
            _跳(f"缺 {素材 / 名}（跑 python tools/build_wasm.py）")
    d = tmp_path_factory.mktemp("m188引擎")
    shutil.copyfile(exe, d / ("jishi.exe" if os.name == "nt" else "jishi"))
    for 名 in ("jishi-wasm.js", "jishi_wasm.wasm"):
        shutil.copyfile(素材 / 名, d / 名)
    dll = exe.parent / "WebView2Loader.dll"
    if dll.is_file():
        shutil.copyfile(dll, d / "WebView2Loader.dll")
    return d


@pytest.fixture(scope="module")
def 打好的包(tmp_path_factory, 引擎目录) -> dict:
    """三个示例各打一次（便携 + 压缩包），Windows 上再给打砖块打一次安装包。"""
    出 = tmp_path_factory.mktemp("m188包")
    结果 = {}
    exe = rust_exe_path()
    for 名, 入口, 文件们 in 游戏们:
        程序 = 出 / f"{名}源"
        程序.mkdir()
        for f in [入口] + 文件们:          # 入口本身也要在目录里（打包会查这一条）
            shutil.copyfile(例子 / f, 程序 / f)
        目标 = "便携,压缩包" + (",安装包" if os.name == "nt" else "")
        r = subprocess.run(
            [str(exe), "打包", str(程序), "--名字", 名, "--版本", "0.3.3",
             "--入口", 入口, "--目标", 目标, "--自带运行时",
             "--引擎", str(引擎目录), "--出", str(出 / 名)],
            capture_output=True, timeout=180)
        文 = (r.stdout + r.stderr).decode("utf-8", "replace")
        assert r.returncode == 0, f"「{名}」打包失败：\n{文[-800:]}"
        结果[名] = {"出": 出 / 名, "入口": 入口, "文件们": 文件们}
    return 结果


# ---------------------------------------------------------------------------
# 一、包里到底装了什么
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("名,入口,文件们", 游戏们)
def test_便携包里有程序也有运行时(打好的包, 名, 入口, 文件们):
    """🔴 两半都必须在：`app/`（程序）+ `bin/`（运行时与 wasm 素材）。

    少了 `bin/jishi_wasm.wasm` = **白窗口、零报错**（M173 踩过）。
    """
    z = zipfile.ZipFile(打好的包[名]["出"] / f"{名}-0.3.3.zip")
    有 = set(z.namelist())
    for f in [入口] + 文件们:
        assert f"app/{f}" in 有, f"包里少了程序文件 app/{f}"
    for f in ("bin/jishi-wasm.js", "bin/jishi_wasm.wasm"):
        assert f in 有, f"包里少了引擎素材 {f} —— 装上去就是白窗口"
    assert any(n.rstrip("/").endswith(("jishi.exe", "jishi")) for n in 有), "包里没有可执行文件"


@pytest.mark.parametrize("名,入口,文件们", 游戏们)
def test_清单里的入口写对了(打好的包, 名, 入口, 文件们):
    """入口是安装包建快捷方式的依据 —— 写错就是「装完双击没反应」。"""
    z = zipfile.ZipFile(打好的包[名]["出"] / f"{名}-0.3.3.zip")
    清单 = json.loads(z.read("清单.json").decode("utf-8"))
    assert 清单["名字"] == 名 and 清单["版本"] == "0.3.3"
    assert 清单["入口"] == f"app/{入口}",         f"入口写成 {清单['入口']!r}，应当是 app/{入口}（包内路径）"


def test_压缩包带_install_sh_且入口对(打好的包):
    """Linux 那一份：`install.sh` 里必须出现入口文件名（它会软链到 $PREFIX/bin）。"""
    tar = 打好的包["打砖块"]["出"] / "打砖块-0.3.3-linux-x64.tar.gz"
    assert tar.is_file(), "没出压缩包"
    with tarfile.open(tar) as t:
        名们 = t.getnames()
        assert any(n.endswith("install.sh") for n in 名们), "压缩包里没有 install.sh"
        脚 = [n for n in 名们 if n.endswith("install.sh")][0]
        文 = t.extractfile(脚).read().decode("utf-8")
    assert "打砖块.jsh" in 文, "install.sh 里没提入口，装完就没有命令可敲"


@pytest.mark.skipif(os.name != "nt", reason="安装包是 Windows 的自解压存根")
def test_安装包是_存根加载荷(打好的包):
    """`.exe` = `[存根][zip 载荷][偏移 u64][长度 u64]["JISHIPK1"]`。

    这里验两件：尾部标识对、载荷能当 zip 解开。

    📌 **M198 起载荷有两种**：散文件（没有 Inno 的机器）或**内嵌的 Inno 向导安装器**
       （`__installer__.exe`，有 Inno 的机器）。两种都得认 —— CI 上没有 Inno，
       本机有，判据不能只认一种。真正**装一遍**不在这里做（会在本机落一份安装），
       开窗验证走便携包那条。
    """
    import struct

    包 = 打好的包["打砖块"]["出"] / "打砖块-0.3.3-windows-x64-setup.exe"
    assert 包.is_file(), "没出安装包"
    全 = 包.read_bytes()
    assert 全[-8:] == b"JISHIPK1", "尾部标识不对（自解压存根读不出来）"
    偏移, 长度 = struct.unpack("<QQ", 全[-24:-8])
    assert 0 < 偏移 < len(全) and 0 < 长度 <= len(全) - 偏移
    载荷 = 全[偏移:偏移 + 长度]
    z = zipfile.ZipFile(__import__("io").BytesIO(载荷))
    有 = set(z.namelist())
    if "__installer__.exe" in 有:
        # 向导版：载荷只有那一个安装器（程序与引擎都编进它里面了）
        assert 有 == {"清单.json", "__installer__.exe"}, f"向导载荷里多了东西：{有}"
        assert z.getinfo("__installer__.exe").file_size > 100_000, "内嵌安装器太小了"
    else:
        assert "app/打砖块.jsh" in 有, "载荷里没有入口"
        assert "bin/jishi_wasm.wasm" in 有, "载荷里没有 wasm 素材"


# ---------------------------------------------------------------------------
# 二、解开的包真能开窗（CI 上是空白，见 CHANGELOG）
# ---------------------------------------------------------------------------

_无界面 = os.environ.get("JISHI_NO_GUI") == "1"

需要桌面 = pytest.mark.skipif(
    _无界面 or not sys.platform.startswith("win"),
    reason="要 Windows 桌面（CI 上是空白，见 CHANGELOG）")


@需要桌面
def test_解开的便携包真能开窗(打好的包, tmp_path):
    """🔴 **只验「包里有什么」是不够的** —— 真正的问题是「装到别的地方还跑不跑」。

    这条把便携包解到一个**跟仓库无关的临时目录**，用包里自带的运行时起窗口，
    断言页面把「wasm 引擎起来了」报给了宿主。素材漏带 / 路径写死 cwd，
    都会在这一步现原形。
    """
    import time

    解 = tmp_path / "解开的包"
    zipfile.ZipFile(打好的包["打砖块"]["出"] / "打砖块-0.3.3.zip").extractall(解)
    入口文件 = 解 / "app" / "打砖块.jsh"
    assert 入口文件.is_file(), f"解开之后没有入口：{sorted(p.name for p in 解.iterdir())}"
    运行时 = 解 / "bin" / ("jishi.exe" if os.name == "nt" else "jishi")
    assert 运行时.is_file(), "解开的包里没有运行时"

    日志 = tmp_path / "窗口.log"
    env = {**os.environ, "JISHI_WINDOW_DEBUG": "1"}
    with open(日志, "w", encoding="utf-8") as f:
        进程 = subprocess.Popen([str(运行时), "app/打砖块.jsh"], cwd=str(解),
                                stdout=f, stderr=subprocess.STDOUT, env=env)
    try:
        期限 = time.time() + 30
        while time.time() < 期限:
            if 进程.poll() is not None:
                break
            文 = 日志.read_text(encoding="utf-8", errors="replace")
            if "引擎起来了" in 文 or "引擎失败" in 文:
                break
            time.sleep(0.3)
        文 = 日志.read_text(encoding="utf-8", errors="replace")
        assert "引擎起来了" in 文, f"包里的程序没跑起来：\n{文[-1200:]}"
        assert "找不到 wasm 引擎素材" not in 文
    finally:
        if 进程.poll() is None:
            import ctypes
            from ctypes import wintypes
            u = ctypes.WinDLL("user32", use_last_error=True)
            u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
            h = u.FindWindowW(None, "打砖块")
            if h:
                u.PostMessageW(h, 0x0010, 0, 0)      # WM_CLOSE
            try:
                进程.wait(timeout=10)
            except subprocess.TimeoutExpired:
                进程.kill()
