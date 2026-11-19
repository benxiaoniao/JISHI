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


def _跑(命令, 工作目录=None, 超时=300, 环境=None):
    r = subprocess.run([str(x) for x in 命令], capture_output=True, timeout=超时,
                       cwd=str(工作目录) if 工作目录 else None, input=b"",
                       env=环境)
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


def _装(包: Path, 目录: Path, *额外, 系统: bool = False):
    """跑一次安装。

    🔴 `系统=False`（默认）会带 `JISHI_SETUP_NO_SYSTEM=1`：不写注册表 /
    开始菜单 / 桌面 / PATH。**这条是为了不再污染开发机** —— M200 第一轮的判据
    装完从不卸载，结果在「设置 → 应用」里留下一条指着 `%TEMP%/pytest-…` 的
    「升级演练」，点卸载报「找不到 uninstall.exe」（用户实测撞到的）。

    要验系统集成的判据传 `系统=True`，并且**必须装到临时目录之外**、
    在 fixture 里 `finally` 清干净。
    """
    环境 = None if 系统 else dict(os.environ, JISHI_SETUP_NO_SYSTEM="1")
    return _跑([包, "--目标", 目录, *额外], 环境=环境)


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

    ⚠️ 这里**故意不点**按钮：这条只回答「窗口起不起来」。
       点下去之后的事由下面 `test_双击向导能自己装完_进度真的在走` 覆盖 ——
       那条走 `JISHI_SETUP_AUTO`，注入的 JS 是 `document.getElementById("go").click()`，
       即**页面真的点了一下**（不是绕过 UI 直接调安装函数）。
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


# ---------------------------------------------------------------------------
# 四、M200 第二轮：用户实测撞出来的四件事
# ---------------------------------------------------------------------------
#
# 1. 双击**闪黑窗** —— exe 是控制台子系统；
# 2. 「设置 → 应用」里留着一条**点不动的**「升级演练」—— 判据在临时目录真装过，
#    注册表项指着临时路径，测试结束临时目录被删，那条项永久留着；
# 3. 卸载项图标是白板、还挂着「修改」按钮 —— `DisplayIcon` 指到 `.jsh`、
#    `NoModify` 根本没写；
# 4. 没有进度条 / 桌面快捷方式 / 「点安装」自动化不了。


def _键(名字: str) -> str:
    return "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\" + 名字


def _读卸载项(名字: str):
    """读 HKCU 下那条卸载项；键不存在返回 `None`。"""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _键(名字)) as k:
            出 = {}
            i = 0
            while True:
                try:
                    名, 值, _类 = winreg.EnumValue(k, i)
                except OSError:
                    break
                出[名] = 值
                i += 1
            return 出
    except FileNotFoundError:
        return None


def _删卸载项(名字: str) -> None:
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _键(名字))
    except FileNotFoundError:
        pass


def _桌面目录() -> Path:
    """与安装器**同一套规则**：读 `User Shell Folders` 再展开 `%VAR%`。

    不用 `~/Desktop`：OneDrive 会把桌面重定向，中文 Windows 的物理目录还可能
    叫「桌面」—— 判据要是自己猜一个路径，就会出现「实现对了、判据红」。
    """
    import winreg

    v = ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\User Shell Folders",
        ) as k:
            v = winreg.QueryValueEx(k, "Desktop")[0]
    except OSError:
        pass
    v = os.path.expandvars(v) if v else ""
    return Path(v) if v else Path(os.path.expanduser("~")) / "Desktop"


def _开始菜单(名字: str) -> Path:
    return (
        Path(os.environ["APPDATA"])
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / f"{名字}.lnk"
    )


def _安装日志() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", ".")) / "jishi-upgrade-backup" / "安装.log"


def _日志快照() -> list:
    try:
        return _安装日志().read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []


@pytest.fixture
def 真装(tmp_path):
    """给「要写系统集成」的判据用的名字 + 目录，**事后一定清干净**。

    🔴 目录**放在 `build/` 而不是 `tmp_path`**：这几条要验注册表 / 开始菜单 /
    桌面，而 `tmp_path` 在 `%TEMP%` 下 —— 那等于每次跑完都在「设置 → 应用」里
    留一条指向临时目录的死项（测试一结束路径就没了）。`build/` 下的目录由本
    fixture 在收尾时删，注册表和 `.lnk` 也一起收。
    """
    名字 = f"M200自检{os.getpid()}"
    目录 = ROOT / "build" / "m200_test" / f"{名字}-{tmp_path.name}"
    shutil.rmtree(目录, ignore_errors=True)
    _删卸载项(名字)
    yield 名字, 目录
    shutil.rmtree(目录, ignore_errors=True)
    _删卸载项(名字)
    for p in (_开始菜单(名字), _桌面目录() / f"{名字}.lnk", 目录):
        try:
            p.unlink()
        except (FileNotFoundError, PermissionError, IsADirectoryError):
            pass


@只有win
def test_查看在管道里也有输出(tmp_path):
    """🔴 回归：改成 GUI 子系统之后，`--查看` 一度**一个字都不输出**。

    根因：`接上控制台()` 无条件把 stdout 指向 `NUL`，把父进程给的**管道**
    盖掉了（CI / pytest / `> log.txt` 都是管道或文件）。表现是
    「退出码 0、输出空」—— 最坏的一种：看着成功。

    这条判据只看一件事：**通过管道**能读到真内容。
    """
    包 = _造包(tmp_path, "1.0.0", {"程序.jsh": "打印(1)\n"})
    码, 文 = _跑([包, "--查看"])
    assert 码 == 0, 文[-400:]
    assert "名字：升级演练" in 文, f"输出被弄丢了：{文!r}"
    assert "本次会：" in 文, f"半截输出：{文!r}"


@只有win
def test_关掉系统集成就真的不写注册表(tmp_path):
    """`JISHI_SETUP_NO_SYSTEM=1` ⇒ 文件照装，注册表 / 开始菜单 / 桌面全不碰。

    🔴 这条开关是给**判据**用的，来历是用户实测：第一轮的判据装完从不卸载，
    在「设置 → 应用」里留下一条指着 `%TEMP%/pytest-…/装/uninstall.exe` 的
    「升级演练」；测试结束临时目录被删，那条项**永久**留着，点卸载报
    「Windows 找不到文件 uninstall.exe」。

    ⚠️ 安装器自己**没有**「装到临时目录就不集成」这条规则：那会把 M171/M177
       的判据全弄红（它们正是在 `tmp_path` 里验系统集成）。职责分工是
       「安装器照做，判据用开关把自己摘干净」。
    """
    名字 = f"临时装{os.getpid()}"
    包 = _造包(tmp_path, "1.0.0", {"程序.jsh": "打印(1)\n"}, 名字=名字)
    目录 = tmp_path / "装"
    码, 文 = _装(包, 目录)          # 默认就带 JISHI_SETUP_NO_SYSTEM=1
    assert 码 == 0, 文[-600:]
    assert (目录 / "清单.json").is_file(), "文件该装还是要装"
    assert "没做系统集成" in 文, f"得说清为什么没写卸载项：{文[-500:]}"
    assert _读卸载项(名字) is None, "关掉集成了却还在写注册表"
    assert not _开始菜单(名字).exists()
    assert not (_桌面目录() / f"{名字}.lnk").exists()


@只有win
def test_桌面与卸载项_装了卸了都干净(tmp_path, 真装):
    """桌面快捷方式（勾了才建）+ 卸载项的三个细节。

    三个细节都是用户一眼能看见的：

    * `DisplayName` 得是**程序名**（不是包文件名、不是安装目录名）；
    * `DisplayIcon` 得是 `.exe` —— 指到 `.jsh` 只会在「应用」里显示**白板图标**；
    * `NoModify` / `NoRepair` 得写 1 —— 否则那儿会挂一个「修改」按钮，
      点下去把 `/MODIFY` 甩给卸载器，**什么也不会发生**。
    """
    名字, 目录 = 真装
    包 = _造包(tmp_path, "1.0.0", {"程序.jsh": "打印(1)\n"}, 名字=名字)

    码, 文 = _装(包, 目录, "--桌面", 系统=True)
    assert 码 == 0, 文[-600:]

    桌面 = _桌面目录() / f"{名字}.lnk"
    assert 桌面.is_file(), f"勾了桌面却没建出来（{桌面}）"

    项 = _读卸载项(名字)
    assert 项 is not None, "卸载项没写"
    assert 项["DisplayName"] == 名字
    assert 项["DisplayVersion"] == "1.0.0"
    assert str(项["InstallLocation"]).lower() == str(目录).lower()
    assert str(项["DisplayIcon"]).lower().endswith(".exe"), (
        f"图标指到了非 exe（{项['DisplayIcon']}）——『应用』里会是白板")
    assert 项.get("NoModify") == 1 and 项.get("NoRepair") == 1
    assert int(项.get("EstimatedSize", 0)) >= 1

    码, 文 = _跑([目录 / "uninstall.exe", "--卸载"])
    assert 码 == 0, 文[-600:]
    assert not 目录.exists(), "安装目录没删干净"
    assert not 桌面.exists(), "桌面快捷方式没删掉"
    assert not _开始菜单(名字).exists(), "开始菜单没删掉"
    assert _读卸载项(名字) is None, "卸载项没删掉"


@只有win
def test_卸载不因注册表项缺失而失败(tmp_path, 真装):
    """卸载项要是已经被别处删了，卸载**照样成功**（文件才是主体）。

    M200 第一轮这里是 `reg::删键(&键)?` —— 键不存在就让整个卸载返回错误，
    于是「文件删得干干净净、退出码却是 1」，用户看到报错以为没卸掉。
    """
    名字, 目录 = 真装
    包 = _造包(tmp_path, "1.0.0", {"程序.jsh": "打印(1)\n"}, 名字=名字)
    码, 文 = _装(包, 目录, 系统=True)
    assert 码 == 0, 文[-600:]
    _删卸载项(名字)          # 模拟「被清理工具删了 / 当初就没写成功」

    码, 文 = _跑([目录 / "uninstall.exe", "--卸载"])
    assert 码 == 0, f"卸载该容错：{文[-600:]}"
    assert not 目录.exists()


@有桌面
def test_双击向导能自己装完_进度真的在走(tmp_path, 真装):
    """双击 → 页面**真的点一下「安装」**→ 后台装 → 关窗 → 退出码 0。

    这条覆盖的是 M200 第一轮**根本跑不到**的那段：窗口里是 WebView2 渲染的
    HTML，按钮没有 Win32 句柄，`BM_CLICK` 那套点不动它。做法是
    `JISHI_SETUP_AUTO`：页面加载后由宿主注入
    `document.getElementById("go").click()` —— 走的是 onclick → IPC → 安装，
    和用户手点**同一条路**。

    另外三条断言各防一个已发生的 bug：

    * 进度 ≥ 3 条且到过 100% —— 防「画个框不动」；
    * 用时 < 60 秒 —— 防「装完了窗口不自己关」（实测过一次 115 秒：
      `请求关闭` 里用 thread_local 判活，后台线程永远判「没有窗口」）；
    * 勾了桌面就必须有 `.lnk`。
    """
    名字, 目录 = 真装
    包 = _造包(tmp_path, "1.0.0", {"程序.jsh": "打印(1)\n"}, 名字=名字)
    前 = len(_日志快照())

    env = dict(os.environ, JISHI_SETUP_AUTO=str(目录) + "|desk")
    t0 = __import__("time").time()
    r = subprocess.run([str(包)], env=env, capture_output=True, timeout=180)
    用时 = __import__("time").time() - t0
    文 = (r.stdout + r.stderr).decode("utf-8", "replace")
    新增 = _日志快照()[前:]

    assert r.returncode == 0, f"退出码 {r.returncode}：{文[-600:]}\n日志：{新增[-10:]}"
    assert (目录 / "清单.json").is_file(), f"没装上：{新增[-10:]}"

    进度 = [x for x in 新增 if "进度" in x]
    assert len(进度) >= 3, f"进度没动（只有 {len(进度)} 条）：{新增[-10:]}"
    assert any("100%" in x for x in 进度), f"没走到 100%：{进度[-5:]}"

    assert 用时 < 60, f"装完了窗口没自己关（{用时:.0f} 秒）"
    assert (_桌面目录() / f"{名字}.lnk").is_file(), "勾了桌面却没建"


def _子系统(路径: Path) -> int:
    """读 PE 可选头里的 `Subsystem`（2 = GUI，3 = 控制台）。"""
    d = 路径.read_bytes()[:4096]
    pe = int.from_bytes(d[0x3C:0x40], "little")
    opt = pe + 24
    return int.from_bytes(d[opt + 68 : opt + 70], "little")


@只有win
def test_双击不弹黑窗(tmp_path):
    """安装器必须是 **GUI 子系统** —— 用户实测的第一条反馈就是「双击闪黑窗」。

    控制台子系统（`Subsystem = 3`）的 exe 被双击时，Windows 会先给它分配一个
    控制台窗口，于是用户看到黑框一闪（甚至一直挂着）。改成 GUI 子系统后
    双击不分配控制台；从命令行用时靠 `AttachConsole` 借父进程那个，
    所以 `--查看` 的输出照旧（见上一条判据）。

    🔴 这条盯的是**产物**，不是源码：`windows_subsystem` 属性写在文件顶上，
       被谁顺手删掉、或者 `#[cfg]` 条件写错（少了 `native-window` 特性），
       编出来的就又是控制台程序，而所有功能判据都是绿的。
    """
    包 = _造包(tmp_path, "1.0.0", {"程序.jsh": "打印(1)\n"})
    assert _子系统(包) == 2, (
        f"Subsystem={_子系统(包)}（3 = 控制台）—— 双击会闪黑窗")
