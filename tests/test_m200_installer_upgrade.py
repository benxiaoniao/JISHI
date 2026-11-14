# -*- coding: utf-8 -*-
"""M200 · 自研安装器：装 / 卸 / 升的判定与回滚（去 Inno 的地基）。

M171/M172 的存根安装器只会「解包 + 写卸载项」，没有「升级」这件事：
往同一个目录装第二遍就是**覆盖**，旧版本才有的文件留在那儿（越装越脏），
而且装到一半失败会留下一个**半新半旧**的目录。

这一站先做内核（路线图 12.2.11 的第一刀），四条规矩：

1. **版本相同不装**（除非 `--重装`）；
2. **往低版本装要显式要**（`--允许降级`），默认拒绝并说清装着的是哪个版本；
3. **先备份，装砸了整个还原**；
4. **升级只删「旧清单有、新清单没有」的文件** —— 用户自己放进安装目录的东西
   一个都不碰。这就是「升级保留用户数据」的全部实现，不需要另开 `data/` 约定。

## 哪些判据三平台都跑、哪些只在 Windows 跑

* 「判定」那几条用 `--查看`（**只读**），三平台都能跑 —— 造一个假安装目录 +
  一个真包就行；
* 「真装一遍」那几条 `skipif not windows`：安装器要写注册表卸载项、建 `.lnk`，
  Linux/macOS 上这些是空实现，验了也等于没验。**跳过不等于验过**，写进
  `docs/测试跳过说明.md`。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from conftest import rust_exe_path  # noqa: E402

#: 只跑 Windows 的那些（真装要注册表与 .lnk）
只有win = pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="安装器真装一遍要写注册表卸载项与开始菜单 .lnk，非 Windows 上是空实现")


def _跑(命令, 工作目录=None, 超时=300):
    r = subprocess.run([str(x) for x in 命令], capture_output=True, timeout=超时,
                       cwd=str(工作目录) if 工作目录 else None, input=b"")
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def _造包(tmp: Path, 版本: str, 文件们: dict, 名字: str = "升级演练") -> Path:
    """用**真的** `jishi 打包` 造一个安装包（不手搓 zip：那会绕开被测链路）。"""
    源 = tmp / f"源{版本}"
    源.mkdir(parents=True, exist_ok=True)
    for 名, 内容 in 文件们.items():
        (源 / 名).write_text(内容, encoding="utf-8", newline="\n")
    出 = tmp / f"出{版本}"
    码, 文 = _跑([rust_exe_path(), "打包", 源, "--出", 出, "--名字", 名字,
                 "--版本", 版本, "--入口", "程序.jsh", "--目标", "安装包",
                 "--不用inno"])
    assert 码 == 0, 文[-600:]
    候 = sorted(出.glob("*setup*"))
    assert 候, f"没打出安装包：{文[-400:]}"
    包 = 候[0]
    # POSIX 上写出来的文件没有可执行位（M198 踩过同一条）
    if os.name != "nt":
        os.chmod(包, 0o755)
    return 包


def _看(包: Path, 目录: Path):
    """`--查看`（只读）：返回 (码, 输出)。"""
    return _跑([包, "--查看", "--目标", 目录])


def _装(包: Path, 目录: Path, *额外):
    return _跑([包, "--目标", 目录, *额外])


def _写假清单(目录: Path, 版本: str, 文件们=()):
    """造一个「已经装过」的目录（只写清单，不写文件）。"""
    目录.mkdir(parents=True, exist_ok=True)
    条目 = ",\n".join(
        f'    {{"路径": "{p}", "大小": 1, "sha256": "x"}}' for p in 文件们)
    (目录 / "清单.json").write_text(
        '{\n'
        '  "名字": "升级演练",\n'
        f'  "版本": "{版本}",\n'
        '  "平台": "windows-x64",\n'
        '  "入口": "程序.jsh",\n'
        f'  "文件数": {len(文件们)},\n'
        '  "字节数": 1,\n'
        f'  "文件们": [\n{条目}\n  ]\n'
        '}\n', encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# 一、判定（三平台都跑）—— 只读，不落任何东西
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("已有,新版本,期望", [
    (None, "1.0.0", "全新安装"),
    ("1.0.0", "1.1.0", "从 1.0.0 升级"),
    ("1.0.0", "1.0.0", "已经是这个版本"),
    ("2.0.0", "1.0.0", "从 2.0.0 **降级**"),
])
def test_查看能判出四种动作(tmp_path, 已有, 新版本, 期望):
    """`--查看` 顺带报「已装什么、这次会做成什么」—— 只读，脚本能安全调。

    🔴 这条**不装**，所以三平台都跑：判定错了会在真装那一刻才暴露，
       而那时用户已经在等安装进度条了。
    """
    包 = _造包(tmp_path, 新版本, {"程序.jsh": "打印(1)\n"})
    目录 = tmp_path / "装"
    if 已有:
        _写假清单(目录, 已有)
    码, 文 = _看(包, 目录)
    assert 码 == 0, 文[-500:]
    assert f"本次会：{期望}" in 文, 文[-500:]


def test_查看不会写任何东西(tmp_path):
    """`--查看` 是只读的：跑完目录还是空的（别偷偷建目录）。"""
    包 = _造包(tmp_path, "1.0.0", {"程序.jsh": "打印(1)\n"})
    目录 = tmp_path / "空目录"
    码, 文 = _看(包, 目录)
    assert 码 == 0, 文[-400:]
    assert "本次会：全新安装" in 文
    assert not 目录.exists(), "`--查看` 把目标目录建出来了"


# ---------------------------------------------------------------------------
# 二、真装一遍（只有 Windows）
# ---------------------------------------------------------------------------

@只有win
def test_升级保留用户自己的文件_删掉旧版本才有的(tmp_path):
    """🔴 这一站的核心：升级后

    * 版本变成新的；
    * **用户自己放进安装目录的文件还在**（`我的数据.txt`）；
    * 旧版本才有的文件被清掉（`旧模块.jsh`）；
    * 新版本新加的文件在（`新模块.jsh`）。
    """
    目录 = tmp_path / "装"
    包1 = _造包(tmp_path, "1.0.0",
               {"程序.jsh": "打印(1)\n", "旧模块.jsh": "函数 旧()：\n    返回 1\n"})
    码, 文 = _装(包1, 目录)
    assert 码 == 0, 文[-600:]
    assert (目录 / "旧模块.jsh").is_file()

    # 用户自己的东西（**不在清单里** ⇒ 升级时按规矩不该碰）
    (目录 / "我的数据.txt").write_text("别删我\n", encoding="utf-8")

    包2 = _造包(tmp_path, "2.0.0",
               {"程序.jsh": "打印(2)\n", "新模块.jsh": "函数 新()：\n    返回 2\n"})
    码, 文 = _装(包2, 目录)
    assert 码 == 0, 文[-600:]
    assert "从 1.0.0 升级" in 文, 文[-600:]

    assert (目录 / "我的数据.txt").is_file(), "升级把用户自己的文件删了"
    assert (目录 / "我的数据.txt").read_text(encoding="utf-8") == "别删我\n"
    assert (目录 / "新模块.jsh").is_file(), "新版本的文件没装上"
    assert not (目录 / "旧模块.jsh").exists(), "旧版本才有的文件没清掉"
    assert '"版本": "2.0.0"' in (目录 / "清单.json").read_text(encoding="utf-8")


@只有win
def test_同版本不再装一遍(tmp_path):
    """双击同一个安装包两次，第二次什么都不做（退出码仍是 0）。"""
    目录 = tmp_path / "装"
    包 = _造包(tmp_path, "1.0.0", {"程序.jsh": "打印(1)\n"})
    assert _装(包, 目录)[0] == 0
    (目录 / "我的数据.txt").write_text("别删我\n", encoding="utf-8")
    码, 文 = _装(包, 目录)
    assert 码 == 0, 文[-500:]
    assert "不再重复安装" in 文, 文[-500:]
    assert (目录 / "我的数据.txt").is_file()


@只有win
def test_降级默认拒绝_加开关才装(tmp_path):
    """往低版本装**默认是错的**（会丢掉新功能），必须显式 `--允许降级`。

    ⚠️ 退出码要非 0：脚本靠它判断（`if setup.exe ...; then`）。
    """
    目录 = tmp_path / "装"
    assert _装(_造包(tmp_path, "2.0.0", {"程序.jsh": "打印(2)\n"}), 目录)[0] == 0

    包1 = _造包(tmp_path, "1.0.0", {"程序.jsh": "打印(1)\n"})
    码, 文 = _装(包1, 目录)
    assert 码 != 0, f"降级应当失败，却给了 {码}：{文[-400:]}"
    assert "2.0.0" in 文 and "允许降级" in 文, 文[-500:]
    # 拒绝的时候**一个文件都不许动**
    assert '"版本": "2.0.0"' in (目录 / "清单.json").read_text(encoding="utf-8")

    码, 文 = _装(包1, 目录, "--允许降级")
    assert 码 == 0, 文[-600:]
    assert '"版本": "1.0.0"' in (目录 / "清单.json").read_text(encoding="utf-8")


@只有win
def test_装砸了整个还原(tmp_path):
    """🔴 装到一半失败，目录必须回到**装之前**的样子（不是半新半旧）。

    制造失败：把要写的那个文件名占成一个目录 —— `fs::write` 必然失败，
    而且失败发生在「已经写了几个文件」之后（不是第一步就挂）。
    """
    目录 = tmp_path / "装"
    assert _装(_造包(tmp_path, "1.0.0", {"程序.jsh": "打印(1)\n",
                                        "乙.jsh": "函数 乙()：\n    返回 1\n"}), 目录)[0] == 0
    (目录 / "我的数据.txt").write_text("别删我\n", encoding="utf-8")

    # 把「新版本要写的那个文件」占成目录
    (目录 / "丙.jsh").mkdir()

    包2 = _造包(tmp_path, "2.0.0", {"程序.jsh": "打印(2)\n",
                                    "甲.jsh": "函数 甲()：\n    返回 2\n",
                                    "丙.jsh": "函数 丙()：\n    返回 3\n"})
    码, 文 = _装(包2, 目录)
    assert 码 != 0, f"应当失败：{文[-500:]}"
    assert "还原" in 文, f"没说清有没有回滚：{文[-500:]}"

    # 回滚后：版本还是 1.0.0、老文件在、用户数据在、半成品（甲.jsh）不在
    assert '"版本": "1.0.0"' in (目录 / "清单.json").read_text(encoding="utf-8")
    assert (目录 / "乙.jsh").is_file(), "老文件没了（回滚没还原）"
    assert (目录 / "我的数据.txt").is_file(), "用户数据没了（回滚没还原）"
    assert not (目录 / "甲.jsh").exists(), "半成品文件留下来了"


@只有win
def test_没装过就是全新安装(tmp_path):
    目录 = tmp_path / "装"
    码, 文 = _装(_造包(tmp_path, "1.0.0", {"程序.jsh": "打印(1)\n"}), 目录)
    assert 码 == 0, 文[-500:]
    assert "全新安装" in 文, 文[-500:]
    assert (目录 / "程序.jsh").is_file()
    assert (目录 / "清单.json").is_file()


# ---------------------------------------------------------------------------
# 三、向导能开窗（有桌面才跑）
# ---------------------------------------------------------------------------

#: 无桌面 / 非 Windows 都不跑：安装器的窗口是原生的（tao + WebView2）
有桌面 = pytest.mark.skipif(
    os.environ.get("JISHI_NO_GUI") == "1" or not sys.platform.startswith("win"),
    reason="原生窗口要桌面（CI 上给 JISHI_NO_GUI=1）")


@有桌面
def test_双击能开出向导窗口(tmp_path):
    """🔴 M200 的「脱离 Inno」靠的就是这个窗口。

    这条只验**窗口真的出现、进程活着等用户点** —— 点「安装」之后的事
    由上面那几条覆盖（它们走 `--目标` 那条路，同一套 `装并管升级`）。

    ⚠️ 为什么不能自动点：窗口里是 WebView2 渲染的 HTML，按钮是**页面元素**，
       没有 Win32 控件句柄 —— `BM_CLICK` / `WM_COMMAND` 那一套对它无效
       （M198 在 Inno 的向导上已经撞过同一堵墙）。人工验收项写进路线图。
    """
    import ctypes
    import time
    from ctypes import wintypes

    包 = _造包(tmp_path, "1.0.0", {"程序.jsh": "打印(1)\n"})
    目录 = tmp_path / "装"
    # 用 `--目标` 之外的方式起：**一个参数都不给**才是「双击」
    进程 = subprocess.Popen([str(包)], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    标题们: list = []
    try:
        u = ctypes.windll.user32

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def 收(h, _l):
            if not u.IsWindowVisible(h):
                return True
            n = u.GetWindowTextLengthW(h)
            if not n:
                return True
            b = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, b, n + 1)
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(h, ctypes.byref(pid))
            if pid.value == 进程.pid:
                标题们.append(b.value)
            return True

        截止 = time.time() + 20
        while time.time() < 截止 and not any("安装" in t for t in 标题们):
            u.EnumWindows(收, 0)
            if not any("安装" in t for t in 标题们):
                time.sleep(0.5)
    finally:
        进程.kill()
        进程.wait(timeout=30)

    assert any("安装" in t for t in 标题们), (
        f"没等到向导窗口（属于它的窗口：{标题们}）—— "
        f"要么窗口没起来，要么标题不含「安装」")
    # 窗口起来之后**不该**顺手把东西装上（用户还没点）
    assert not (目录 / "清单.json").exists()
