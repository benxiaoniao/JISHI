# -*- coding: utf-8 -*-
"""M222 判据：系统集成（文件关联 + 最近文件 + 徽标）。

| # | 这一站做的 | 判据 |
|---|---|---|
| ① | `jishi 打包 --关联 .jishi` 把声明写进清单 | 包里的 `清单.json` 有「关联」数组（扩展名/说明都对） |
| ② | 三宿主清单文本逐字一致 | 同输入 → 三个宿主各打一个包 → 解出的清单字节相同 |
| ③ | 安装器写注册表（HKCU\\Software\\Classes） | `.jishi` 默认值 = 本程序 ProgID；command 是 `"引擎" "入口" "%1"` |
| ④ | 卸载回收 | 键消失（只在默认值还是自己时才动） |
| ⑤ | `JISHI_SETUP_NO_SYSTEM=1` 只验文件 | 文件装好、注册表**一条都没写** |
| ⑥ | 双击关联文件 → 程序拿得到路径 | 模拟命令行 `引擎 入口 文件` → `系统.参数()` 第一项 = 文件 |
| ⑦ | 无引擎的包不写关联 | 不写注册表，且安装时明确提示原因 |
| ⑧ | `最近文件` / `徽标` 的口径 | Rust 真/假；Python 假 + stderr 同一句 |
| ⑨ | `--安装器 inno` + `--关联` | 明确报错（这一版 Inno 不写关联，不静默降级） |

🔴 **这条判据会写注册表**（`HKCU\\Software\\Classes`）—— 所以：
* 程序名带 pid（唯一），`finally` 里递归删键 + 删目录；
* 真装只装到 `tmp_path` 下，**不碰用户已装的东西**；
* `JISHI_SETUP_NO_SYSTEM=1` 那条是给「不想动系统」的环境用的。

⚠️ 真装 + 拷 10 MB 引擎 ⇒ 打 `计时`（串行跑，别和别的真开窗判据抢）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUST = ROOT / "rust" / "target" / "release" / (
    "jishi-rs.exe" if os.name == "nt" else "jishi-rs")
STUB = ROOT / "rust" / "target" / "release" / (
    "jishi-pack-stub.exe" if os.name == "nt" else "jishi-pack-stub")
WASM_JS = ROOT / "site" / "jishi-wasm.js"
WASM = ROOT / "site" / "jishi_wasm.wasm"
WEBVIEW = ROOT / "rust" / "target" / "release" / "WebView2Loader.dll"

pytestmark = [
    pytest.mark.skipif(not RUST.is_file(), reason="没有发行版二进制"),
    pytest.mark.skipif(os.name != "nt", reason="文件关联这一版只做了 Windows"),
]


def _跑(cmd, cwd=None, timeout=180, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    r = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                       capture_output=True, timeout=timeout, input=b"", env=e)
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def _写(路径: Path, 文: str):
    路径.parent.mkdir(parents=True, exist_ok=True)
    路径.write_text(文, encoding="utf-8", newline="\n")


def _引擎目录(tmp_path: Path) -> Path:
    """造一份「发行版解压开」的引擎四件套（打包 --自带运行时 要用）。"""
    目 = tmp_path / "引擎"
    目.mkdir()
    shutil.copy2(RUST, 目 / "jishi.exe")
    if not WASM_JS.is_file() or not WASM.is_file():
        pytest.skip("还没有 wasm 素材（跑 tools/build_wasm.py）")
    shutil.copy2(WASM_JS, 目 / "jishi-wasm.js")
    shutil.copy2(WASM, 目 / "jishi_wasm.wasm")
    if WEBVIEW.is_file():
        shutil.copy2(WEBVIEW, 目 / "WebView2Loader.dll")
    return 目


def _程序目录(tmp_path: Path) -> Path:
    目 = tmp_path / "程序"
    _写(目 / "程序.jsh", '导入 系统\n打印("参数", 系统.参数())\n')
    return 目


def _打包(tmp_path: Path, 名字: str, 关联: str | None, 出: Path,
          带运行时: bool = False, 引擎: Path | None = None) -> Path:
    命令 = [RUST, "打包", str(_程序目录(tmp_path)),
            "--名字", 名字, "--版本", "1.0.0", "--入口", "程序.jsh",
            "--目标", "安装包", "--出", str(出)]
    if 关联:
        命令 += ["--关联", 关联]
    if 带运行时:
        命令 += ["--自带运行时", "--引擎", str(引擎)]
    码, 文 = _跑(命令, timeout=300)
    assert 码 == 0, 文[-800:]
    包 = [p for p in 出.glob("*setup.exe")]
    assert 包, f"没产出安装包：{文[-400:]}"
    return 包[0]


def _清单(包: Path) -> dict:
    import json
    with zipfile.ZipFile(包) as z:
        名 = [n for n in z.namelist() if n.endswith("清单.json")]
        assert 名, "包里没有清单.json"
        return json.loads(z.read(名[0]).decode("utf-8"))


# ---------------------------------------------------------------------------
# ① 打包声明
# ---------------------------------------------------------------------------

@pytest.mark.计时
def test_打包把关联写进清单(tmp_path):
    if not STUB.is_file():
        pytest.skip("没有 jishi-pack-stub（先 cargo build --release）")
    包 = _打包(tmp_path, "M222声明", ".jishi,jsh", tmp_path / "出")
    m = _清单(包)
    assert m["关联"] == [
        {"扩展名": ".jishi", "说明": "基石程序"},
        {"扩展名": ".jsh", "说明": "基石程序"},
    ], m.get("关联")
    # 不带 --关联 的包：**老格式，键都不出现**（字节兼容）
    if STUB.is_file():
        包2 = _打包(tmp_path, "M222无关联", None, tmp_path / "出2")
        assert "关联" not in _清单(包2)


# ---------------------------------------------------------------------------
# ② 三宿主清单一致
# ---------------------------------------------------------------------------

@pytest.mark.计时
def test_三宿主清单逐字一致(tmp_path):
    """同输入 → 三宿主清单**字节**相同（含「关联」那一行）。"""
    源 = _程序目录(tmp_path)
    选项 = ('{"名字": "M222清单", "版本": "1.0.0", "平台": "windows-x64", '
            '"入口": "程序.jsh", "基础": "%s", "关联": {".jishi": "基石脚本"}}'
            % str(源).replace("\\", "/"))
    脚本 = f'导入 打包\n令 出 = 打包.生成(["{str(源 / "程序.jsh").replace(chr(92), "/")}"], ' \
           f'"%s", {选项})\n'
    清单们 = {}
    for 名 in ("rust", "python"):
        出 = tmp_path / f"h_{名}"
        出.mkdir()
        _写(tmp_path / f"h_{名}.jsh", 脚本 % str(出).replace("\\", "/"))
        命令 = ([RUST] if 名 == "rust"
                else [sys.executable, "-m", "oracle.jishi.cli"]) + [str(tmp_path / f"h_{名}.jsh")]
        码, 文 = _跑(命令, cwd=ROOT, timeout=180)
        assert 码 == 0, 文[-600:]
        包 = [p for p in 出.glob("*.zip")]
        assert 包, 文[-400:]
        with zipfile.ZipFile(包[0]) as z:
            清单们[名] = z.read("清单.json")
    assert 清单们["rust"] == 清单们["python"], "两宿主清单字节不同"
    assert "关联".encode("utf-8") in 清单们["rust"]


# ---------------------------------------------------------------------------
# ③④⑤⑦ 安装 / 卸载 / 不碰系统 / 无引擎
# ---------------------------------------------------------------------------

def _注册表读(键: str, 名: str = "") -> str | None:
    import winreg
    try:
        h = winreg.OpenKey(winreg.HKEY_CURRENT_USER, 键)
        v = winreg.QueryValueEx(h, 名)[0]
        h.Close()
        return v
    except OSError:
        return None


def _注册表递删(键: str):
    import winreg

    def 删(路径):
        try:
            h = winreg.OpenKey(winreg.HKEY_CURRENT_USER, 路径, 0, winreg.KEY_ALL_ACCESS)
        except OSError:
            return
        while True:
            try:
                sub = winreg.EnumKey(h, 0)
            except OSError:
                break
            删(路径 + "\\" + sub)
        h.Close()
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, 路径)
        except OSError:
            pass

    删(键)


@pytest.mark.计时
def test_安装写注册表_卸载回收(tmp_path):
    名字 = f"M222关联{os.getpid()}"
    装到 = tmp_path / "装"
    try:
        包 = _打包(tmp_path, 名字, ".jishi", tmp_path / "出", 带运行时=True,
                   引擎=_引擎目录(tmp_path))
        码, 文 = _跑([包, "--目标", str(装到), "/S"], timeout=300)
        assert 码 == 0, 文[-800:]

        # ③ 关联键
        pid = _注册表读(r"Software\Classes\.jishi")
        assert pid, "`.jishi` 没有默认值（关联没写进去）"
        assert pid.startswith("Jishi."), pid
        命令 = _注册表读(rf"Software\Classes\{pid}\shell\open\command") or ""
        assert "jishi.exe" in 命令.lower() and "%1" in 命令, 命令
        assert str(装到).lower().replace("\\", "/") in 命令.lower().replace("\\", "/"), 命令
        图标 = _注册表读(rf"Software\Classes\{pid}\DefaultIcon") or ""
        assert 图标.endswith(",0"), 图标

        # ④ 卸载回收
        uninst = 装到 / "uninstall.exe"
        assert uninst.is_file(), "没写卸载程序"
        码, 文 = _跑([uninst, "--卸载", "/S"], timeout=300)
        assert 码 == 0, 文[-800:]
        assert _注册表读(r"Software\Classes\.jishi") is None, "卸载没回收 `.jishi`"
        assert _注册表读(rf"Software\Classes\{pid}") is None, "ProgID 键还在"
    finally:
        _注册表递删(r"Software\Classes\.jishi")
        _注册表递删(rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{名字}")
        shutil.rmtree(装到, ignore_errors=True)


@pytest.mark.计时
def test_不动系统时只验文件(tmp_path):
    名字 = f"M222不动系统{os.getpid()}"
    装到 = tmp_path / "装"
    try:
        包 = _打包(tmp_path, 名字, ".jishi", tmp_path / "出", 带运行时=True,
                   引擎=_引擎目录(tmp_path))
        码, 文 = _跑([包, "--目标", str(装到), "/S"], timeout=300,
                    env={"JISHI_SETUP_NO_SYSTEM": "1"})
        assert 码 == 0, 文[-800:]
        assert (装到 / "bin" / "jishi.exe").is_file(), "文件没装好"
        assert _注册表读(r"Software\Classes\.jishi") is None, \
            "JISHI_SETUP_NO_SYSTEM=1 却写了注册表"
    finally:
        _注册表递删(r"Software\Classes\.jishi")
        shutil.rmtree(装到, ignore_errors=True)


@pytest.mark.计时
def test_没有引擎的包不写关联并说明(tmp_path):
    名字 = f"M222无引擎{os.getpid()}"
    装到 = tmp_path / "装"
    try:
        包 = _打包(tmp_path, 名字, ".jishi", tmp_path / "出")
        码, 文 = _跑([包, "--目标", str(装到), "/S"], timeout=180)
        assert 码 == 0, 文[-600:]
        assert "没自带运行时" in 文, 文[-600:]
        assert _注册表读(r"Software\Classes\.jishi") is None, "无引擎却写了关联"
    finally:
        # 🔴 这条**真装了文件** ⇒ 安装器写了「应用和功能」的卸载项。判据不许
        #    污染系统（M200 的教训）：目录和注册表键都要清。
        _注册表递删(rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{名字}")
        _注册表递删(r"Software\Classes\.jishi")
        shutil.rmtree(装到, ignore_errors=True)


# ---------------------------------------------------------------------------
# ⑥ 双击 → 拿路径
# ---------------------------------------------------------------------------

def test_双击关联文件程序拿得到路径(tmp_path):
    """关联命令是 `"引擎" "入口" "%1"`——模拟这条命令行，脚本第一个参数就是文件。"""
    源 = tmp_path / "程序2"
    _写(源 / "程序.jsh", '导入 系统\n打印(系统.参数()[0])\n')
    文件 = tmp_path / "我的文档.jishi"
    文件.write_text("内容", encoding="utf-8")
    码, 文 = _跑([RUST, str(源 / "程序.jsh"), str(文件)], timeout=120)
    assert 码 == 0, 文[-600:]
    # ⚠️ 打印**列表**时字符串按 repr 显示（`\` → `\\`）—— 所以这里只打第一项
    assert 文.strip() == str(文件), (文, str(文件))


# ---------------------------------------------------------------------------
# ⑧ 最近文件 / 徽标
# ---------------------------------------------------------------------------

def test_最近文件与徽标的口径(tmp_path):
    文件 = tmp_path / "最近.jsh"
    文件.write_text("", encoding="utf-8")
    _写(tmp_path / "口径.jsh",
        f'导入 窗口\n打印(窗口.最近文件("{str(文件).replace(chr(92), "/")}"))\n'
        '打印(窗口.徽标("3"))\n')
    # 连跑 3 次：`SHAddToRecentDocs` 的常量写错时（M222 把 SHARD_PATHW 写成 1）
    # 是**偶发** 0xC0000005 —— 单跑一次可能不炸，三次基本必现。
    for _ in range(3):
        码, 文 = _跑([RUST, str(tmp_path / "口径.jsh")], timeout=120)
        assert 码 == 0, 文[-400:]
        assert 文.strip().splitlines() == ["真", "假"], 文

    码, 文 = _跑([sys.executable, "-m", "oracle.jishi.cli", str(tmp_path / "口径.jsh")],
                cwd=ROOT, timeout=120)
    assert 码 == 0, 文[-400:]
    行 = [l for l in 文.splitlines() if l.strip() and "没记上" not in l]
    assert 行 == ["假", "假"], 文
    assert "最近文件没记上：这个宿主没有系统「最近使用」能力" in 文, 文


# ---------------------------------------------------------------------------
# ⑨ Inno + 关联
# ---------------------------------------------------------------------------

def test_inno版不写关联要明确报错(tmp_path):
    码, 文 = _跑([RUST, "打包", str(_程序目录(tmp_path)),
                  "--名字", "M222Inno", "--版本", "1.0.0", "--入口", "程序.jsh",
                  "--关联", ".jishi", "--安装器", "inno",
                  "--目标", "安装包", "--出", str(tmp_path / "出")], timeout=180)
    assert 码 != 0, "Inno + 关联 应当报错"
    assert "只支持自研安装器" in 文, 文[-400:]
