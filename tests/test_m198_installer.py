# -*- coding: utf-8 -*-
"""M198 · 安装器：`jishi 打包 --目标 安装包` 出 **Inno 向导版**。

## 这一组压在哪

M171 那一版是「自解压存根」：能装、能卸、「应用和功能」里有条目，但**没有向导**
—— 不能选目录、不能选要不要桌面快捷方式、没有完成页。M198 把向导补上。

## 三件容易搞错、且**不报错**的事

1. **`.iss` 是生成的** —— 模板在 `tools/installer_user.iss`，两套命令行
   （Python / Rust）各自替换占位符。两边生成的脚本必须**逐字相同**，
   否则「同一个命令在两台机器上装出不同的东西」，而且没人会发现。
2. **`AppId` 由应用名派生** —— 写死一个会「后装的顶掉前一个的卸载项」；
   用随机数会「升级时变成另一个软件」。两条都是静默的。
3. **没有 Inno 时必须说清** —— 产物类型变了（向导版 → 存根版），
   静默降级的后果是「用户以为拿到向导版，双击发现没有选目录那一页」。

## 为什么正经的「装一遍」只在有 Inno 的机器上跑

CI 的 Windows runner 没有 Inno Setup（发行包一直是这样：`tools/build_release.py`
找不到 ISCC 就跳过）。所以「真编、真装、真卸」那几条 `skipif` 掉，理由写在
`docs/测试跳过说明.md`；不带 Inno 也能验的（模板结构、两宿主脚本逐字一致、
降级文案、AppId 派生、非法名字）**在 CI 上照跑** —— 那才是这一站的主要逻辑。

⚠️ 因此：**CI 全绿不等于向导版装得对**。向导版的最终判据是本机跑这一组 +
`tools/drill_install.py`，以及人工点一遍向导。
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                            # noqa: E402

模板 = ROOT / "tools" / "installer_user.iss"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _跑(参数, cwd, exe=None, 环境=None, 超时=900):
    命令 = ([str(exe)] if exe else [sys.executable, "-m", "oracle.jishi.cli"]) + 参数
    r = subprocess.run(命令, cwd=str(cwd), capture_output=True, timeout=超时,
                       input=b"", env=环境)
    return (r.stdout + r.stderr).decode("utf-8", "replace"), r.returncode


def _起(命令, 日志, 超时):
    """起一个 GUI 程序并等它结束。日志写文件，**不经管道**。

    🔴 Windows Defender 会拦「刚写出来就执行」的 exe：`CreateProcess` 直接
       抛 `WinError 225（文件包含病毒或潜在的垃圾软件）`。这不是判据的错，
       重试一两次就过去了（实测：同一条命令隔几秒再跑就没事）。所以这里
       **退避重试**（累计约 31 秒）；还不行才报错，并把「大概是杀软」写在错误里 ——
       否则下次看到 225 的人会以为是打包坏了。

    📌 为什么退避要这么长：判据是 4 路并行跑的，同一时刻好几个进程在「写完就执行」，
       Defender 的按访问扫描会排队；实测 15 秒偶尔还不够，31 秒没再撞过。
    """
    退避 = (1.0, 2.0, 4.0, 8.0, 8.0, 8.0)
    for 次 in range(len(退避) + 1):
        try:
            with open(日志, "w", encoding="utf-8") as f:
                return subprocess.run(命令, stdout=f, stderr=subprocess.STDOUT,
                                      timeout=超时)
        except OSError as e:
            if getattr(e, "winerror", None) != 225 or 次 == len(退避):
                raise
            import time
            time.sleep(退避[次])
    raise AssertionError("不可达")


def _跑程序(程序: Path, *参数, 超时=300):
    """直接跑一个 exe（不是 jishi 脚本）。**不能** capture_output，理由见 `_跑安装器`。

    📌 POSIX 上要先 `chmod +x`：产物是给 Windows 的 `.exe`，在 Linux/macOS 上
       打包出来的**没有可执行位**（`PermissionError: [Errno 13]`，CI 上撞过）。
    """
    if os.name != "nt":
        os.chmod(程序, 0o755)
    日志 = Path(tempfile.mkdtemp(prefix="m198-程序日志-")) / "out.log"
    r = _起([str(程序), *参数], 日志, 超时)
    return r.returncode, 日志.read_text(encoding="utf-8", errors="replace")


def _造源(目录: Path) -> Path:
    (目录 / "app").mkdir(parents=True, exist_ok=True)
    (目录 / "app" / "程序.jsh").write_text('打印("你好")\n', encoding="utf-8", newline="\n")
    return 目录


def _打包(源: Path, 出: Path, 名字="演练程序", 版本="1.0.0", 额外=(), exe=None,
          环境=None):
    参数 = ["打包", str(源), "--出", str(出), "--名字", 名字, "--版本", 版本,
            "--入口", "app/程序.jsh", "--目标", "安装包"] + list(额外)
    return _跑(参数, 源.parent, exe=exe, 环境=环境)


def _掩(文本: str, 出们) -> str:
    """把「每次都不一样的路径」抹掉：临时目录、输出目录。

    剩下的是真正该一致的东西（模板结构、每个占位符换成了什么）。
    """
    for d in 出们:
        s = str(d)
        文本 = 文本.replace(s, "<出>").replace(s.replace("\\", "/"), "<出>")
    文本 = re.sub(r"(?:[A-Za-z]:)?[\\/][^\"\r\n]*?jishi-pack-[^\\/\"]+", "<临时>", 文本)
    return 文本.replace("\\", "/")


def _iss(出: Path, 名字="演练程序", 版本="1.0.0") -> str:
    p = 出 / f"{名字}-{版本}-windows-x64-setup.iss"
    assert p.is_file(), f"没留下 iss：{sorted(x.name for x in 出.iterdir())}"
    return p.read_text(encoding="utf-8-sig")


def _无inno环境(tmp: Path) -> dict:
    """一个「机器上没装 Inno」的环境。

    🔴 **必须用白名单，不能用「从 os.environ 里删几个」**：Windows 的环境变量名
       大小写不敏感，而 `os.environ` 的键保留系统给的拼写（本机就是全大写
       `PROGRAMFILES`）。按名字删很容易删漏，剩下的那条照样指到真目录 ——
       于是「没有 Inno 的机器」根本没模拟出来，判据还绿着。
       （这条坑就是本判据自己先踩出来的：改了两轮才明白它在骗人。）
    """
    空 = tmp / "空"
    空.mkdir(parents=True, exist_ok=True)
    保留 = ("systemroot", "windir", "temp", "tmp", "userprofile", "appdata",
            "comspec", "pathext", "number_of_processors", "processor_architecture")
    环境 = {k: v for k, v in os.environ.items() if k.lower() in 保留}
    环境.update({"PATH": "", "ProgramFiles": str(空),
               "ProgramFiles(x86)": str(空), "LOCALAPPDATA": str(空)})
    return 环境


#: 有 Inno Setup 吗（本机有，CI 的 runner 没有）。
def _有inno() -> bool:
    if sys.platform != "win32":
        return False
    候选 = []
    指定 = os.environ.get("JISHI_ISCC", "").strip()
    if 指定:
        return Path(指定).is_file()
    for 变量 in ("ProgramFiles(x86)", "ProgramFiles"):
        根 = os.environ.get(变量)
        if 根:
            for 版 in ("Inno Setup 7", "Inno Setup 6"):
                候选.append(Path(根) / 版 / "ISCC.exe")
    本地 = os.environ.get("LOCALAPPDATA")
    if 本地:
        for 版 in ("Inno Setup 7", "Inno Setup 6"):
            候选.append(Path(本地) / "Programs" / 版 / "ISCC.exe")
    return any(p.is_file() for p in 候选)


有inno = pytest.mark.skipif(
    not _有inno(),
    reason="这台机器没装 Inno Setup（ISCC.exe）—— 向导版装不出来；"
           "CI 的 Windows runner 上一直没有 It，所以只在有 Inno 的机器上跑")


# ---------------------------------------------------------------------------
# 一、模板本身
# ---------------------------------------------------------------------------

def test_模板还在且关键段落都在():
    """模板是「向导长什么样」的唯一来源，段落缺一段就少一个向导页。

    📌 这不只是「文件在不在」：每一段对应需求里的一条（选目录页 / 开始菜单文件夹
       页 / 桌面快捷方式勾选 / 应用和功能里的条目 / 卸载干净）。
    """
    assert 模板.is_file(), "少了 tools/installer_user.iss"
    文 = 模板.read_text(encoding="utf-8")
    for 段, 为什么 in [
        ("[Setup]", "AppId / 默认目录 / 版本都在这段"),
        ("AppId=@APPID@", "AppId 要由打包填（每个应用不同）"),
        ("DefaultDirName={code:GetDefaultDir}", "选目录页的默认值走 --目标"),
        ("DisableDirPage=no", "关掉就没有「选择安装目录」那一页了"),
        ("DisableWelcomePage=no",
         "🔴 `WizardStyle=modern` 时 Inno **默认关掉欢迎页**，不写这行双击直接落在"
         "「选择目标位置」——实测踩过（向导第一屏就是选目录，用户不知道自己在装什么）"),
        ("DisableProgramGroupPage=no", "关掉就没有「开始菜单文件夹」那一页"),
        ("[Tasks]", "桌面快捷方式是个可勾选的任务"),
        ('Name: "desktopicon"', "「创建桌面快捷方式」这一项"),
        ("[Icons]", "开始菜单 / 桌面快捷方式"),
        ("[Files]", "把 app/ 和 bin/ 铺到安装目录"),
        ("function ShouldSkipPage", "老参数 `/S`、`--目标` 靠它跳完向导"),
        ("function ShouldRun", "跳页装的时候别把用户的程序拉起来"),
        ("PrivilegesRequired=lowest", "装到用户目录，不要管理员"),
    ]:
        assert 段 in 文, f"模板里少了「{段}」（{为什么}）"


def test_模板编码是干净的UTF8且没有BOM():
    """模板本身**不带 BOM**：BOM 由打包时加（见 `带bom`）。

    模板带 BOM 的话 `include_str!` 读进来的第一个字符就是 `\\u{feff}`，
    生成的脚本会多一个看不见的字符 —— 那种东西出问题时根本看不出来。
    """
    d = 模板.read_bytes()
    assert not d.startswith(b"\xef\xbb\xbf"), "模板自己不该带 BOM（写盘时才加）"
    d.decode("utf-8")


# ---------------------------------------------------------------------------
# 二、生成的 .iss（不需要 Inno，CI 上照跑）
# ---------------------------------------------------------------------------

def test_生成的iss里有名字版本和默认目录(tmp_path):
    源 = _造源(tmp_path / "源")
    出 = tmp_path / "出"
    文, 码 = _打包(源, 出, 额外=["--保留iss"])
    assert 码 == 0, 文[-800:]
    iss = _iss(出)
    for 该有 in ["AppName=演练程序", "AppVersion=1.0.0", "AppId={{",
                 "{localappdata}", 'Filename: "{app}\\app\\程序.jsh"']:
        assert 该有 in iss, f"生成的 iss 里少了「{该有}」\n{iss[:600]}"
    assert "@" not in iss, f"还有没替换掉的占位符：{[x for x in iss.split() if '@' in x][:4]}"


def test_AppId由名字派生_同名同id异名异id(tmp_path):
    """🔴 两个静默事故的护栏：

    · AppId **写死** ⇒ 装第二个程序时顶掉第一个的卸载项（卸一个，另一个成了孤儿）；
    · AppId **随机** ⇒ 升级时 Inno 认为是另一个软件（控制面板里两条、旧文件留着）。
    """
    源 = _造源(tmp_path / "源")
    出 = tmp_path / "出"
    出2 = tmp_path / "出2"
    出3 = tmp_path / "出3"
    for 目录, 名字 in ((出, "甲乙"), (出2, "甲乙"), (出3, "丙丁")):
        文, 码 = _打包(源, 目录, 名字=名字, 额外=["--保留iss"])
        assert 码 == 0, 文[-600:]
    # 只认**行首**的 `AppId=`：模板头部注释里也写着 `AppId={{…}`（讲占位符那段）
    def 取id(目录, 名字):
        return re.search(r"(?m)^AppId=(.+)$", _iss(目录, 名字)).group(1).strip()

    id甲 = 取id(出, "甲乙")
    id甲2 = 取id(出2, "甲乙")
    id丙 = 取id(出3, "丙丁")
    assert id甲 == id甲2, f"同一个名字两次算出了不同的 AppId：{id甲} / {id甲2}"
    assert id甲 != id丙, "两个不同的应用用了同一个 AppId —— 卸载项会互相顶掉"
    assert re.fullmatch(r"\{\{[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}\}", id甲), id甲


def test_两套命令行生成的iss逐字相同(tmp_path):
    """Python 与 Rust **必须**算出同一份脚本。

    📌 抹掉的两样是「每次都不一样的路径」（临时解包目录、`--出`）；
       其余逐字比。它们真要是不一样，用户在两台机器上装出来就是两样东西。
    """
    if not rust_exe_path().is_file():
        pytest.skip("没编 jishi-rs（先 cargo build --release）")
    源 = _造源(tmp_path / "源")
    出py, 出rs = tmp_path / "出py", tmp_path / "出rs"
    文, 码 = _打包(源, 出py, 额外=["--保留iss"])
    assert 码 == 0, 文[-600:]
    文, 码 = _打包(源, 出rs, 额外=["--保留iss"], exe=rust_exe_path())
    assert 码 == 0, 文[-600:]
    a = _掩(_iss(出py), [出py])
    b = _掩(_iss(出rs), [出rs])
    if a != b:
        import difflib
        差 = list(difflib.unified_diff(a.splitlines(), b.splitlines(),
                                       "python", "rust", lineterm=""))[:20]
        pytest.fail("两套命令行生成的 iss 不一样：\n" + "\n".join(差))


# ---------------------------------------------------------------------------
# 三、没有 Inno 时的降级（CI 上照跑）
# ---------------------------------------------------------------------------

def test_默认出自研安装器_不需要Inno(tmp_path):
    """🔴 M200 起**默认就是自研安装器**：机器上有没有 Inno 都不影响产物。

    M198 那会儿默认是 Inno，没 ISCC 就降级并打印一大段说明。现在反过来了 ——
    Inno 退成 `--安装器 inno` 才要的东西。所以这条判据盯的是：

    * **没有 ISCC 的机器上照样成功**（这是「脱离 Inno」的最小证据）；
    * 产物是自研版（`--查看` 说载荷不是 Inno）；
    * **不再打印「没走 Inno 向导」那类降级话** —— 自研不是降级，是默认。
    """
    源 = _造源(tmp_path / "源")
    出 = tmp_path / "出"
    文, 码 = _打包(源, 出, 环境=_无inno环境(tmp_path))
    assert 码 == 0, f"自研安装器不该依赖 Inno：{文[-600:]}"
    assert "安装包没走 Inno 向导" not in 文, f"不该再喊降级了：{文[-600:]}"
    包 = 出 / "演练程序-1.0.0-windows-x64-setup.exe"
    assert 包.is_file(), sorted(x.name for x in 出.iterdir())
    # 判「载荷是什么」用 `--查看`（语义），别去产物里搜字节：
    # Inno 的 exe 在载荷 zip 里是**压缩**过的，搜字符串搜不到
    _, 文 = _跑程序(包, "--查看")
    assert "Inno Setup 向导安装器" not in 文, f"默认不该是 Inno 包：{文[:300]}"


def test_显式要inno_没装ISCC就报错不偷换(tmp_path):
    """`--安装器 inno` + 找不到 ISCC ⇒ **报错退出**，不偷偷换成自研。

    🔴 为什么要这条：静默换产物的代价是「作者以为拿到的是 Inno 版，装出来
       却不一样」，而且**看起来一切正常** —— 那种事查起来要一整天（M198 就为
       一个「悄悄降级」写过一整段说明）。要什么没给什么，就该当场说。

    📌 老开关 `--不用inno` 仍然认（等价于 `--安装器 自研`），M198 的脚本不用改。
    """
    源 = _造源(tmp_path / "源")
    文, 码 = _打包(源, tmp_path / "出", 环境=_无inno环境(tmp_path),
                  额外=["--安装器", "inno"])
    assert 码 != 0, f"没 ISCC 却成功了：{文[-600:]}"
    assert "ISCC" in 文 and "自研" in 文, f"得说清缺什么、有哪条退路：{文[-600:]}"

def test_JISHI_ISCC指错地方要报错而不是忽略(tmp_path):
    """用户明确说了用哪个编译器 —— 那个文件不在，就该停下来说。"""
    源 = _造源(tmp_path / "源")
    出 = tmp_path / "出"
    环境 = dict(os.environ)
    环境["JISHI_ISCC"] = str(tmp_path / "并没有这个.exe")
    文, 码 = _打包(源, 出, 环境=环境)
    assert 码 == 2, f"应当拒绝：{文[-500:]}"
    assert "JISHI_ISCC" in 文 and "没有 ISCC.exe" in 文, 文[-500:]


def test_名字里的非法字符要当场说清(tmp_path):
    """`"` `{}` `'` 会把 Inno 脚本搅坏；`/ \\ : * ?` 造不出 Windows 文件名。

    这些字符到 ISCC 那里报的是「第 N 行第 M 列」，用户看不懂是自己名字的问题。
    """
    源 = _造源(tmp_path / "源")
    出 = tmp_path / "出"
    文, 码 = _打包(源, 出, 名字='a"b')
    assert 码 == 2, 文[-500:]
    assert "不能有字符" in 文, 文[-500:]
    assert not (出 / 'a"b-1.0.0-windows-x64-setup.exe').exists()


# ---------------------------------------------------------------------------
# 四、真编、真装、真卸（要有 Inno）
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def 向导包(tmp_path_factory) -> dict:
    """编一次，几条判据共用（ISCC 编一遍要几秒，别每条都重编）。"""
    if not rust_exe_path().is_file():
        pytest.skip("没编 jishi-rs（先 cargo build --release）")
    基 = tmp_path_factory.mktemp("m198向导")
    源 = _造源(基 / "源")
    出 = 基 / "出"
    文, 码 = _打包(源, 出, 名字="M198自检", 版本="1.2.3",
                  额外=["--安装器", "inno"], exe=rust_exe_path())
    assert 码 == 0, f"打包失败：{文[-1200:]}"
    包 = 出 / "M198自检-1.2.3-windows-x64-setup.exe"
    assert 包.is_file(), sorted(x.name for x in 出.iterdir())
    return {"包": 包, "基": 基}


def _跑安装器(包: Path, *参数, 超时=600):
    """GUI 子系统：**不能** capture_output（子进程继承管道，主进程退出后读端还在等）。

    ⚠️ 日志**不能**写在 `包.parent`：卸载时要传 `装/unins000.exe`，那样日志就落在
    安装目录里，接着「卸载后目录该空」那条判据会看见它 —— 判据自己把现场弄脏了。
    """
    日志 = Path(tempfile.mkdtemp(prefix="m198-安装器日志-")) / "out.log"
    r = _起([str(包), *参数], 日志, 超时)
    return r.returncode, 日志.read_text(encoding="utf-8", errors="replace")


def _卸载(装: Path, 显示名: str = "M198自检"):
    """卸载并且**等它真的卸完**。

    🔴 Inno 的卸载器会把自己复制到临时目录再让那份干活，**主进程立刻退出** ——
       不等的话，下一个用例正在装同一个 AppId，上一个卸载器在后台把注册表项和
       目录删掉：报出来的错是「装完了目录里没文件」，看着像安装器坏了。
       （本判据一开始就是这么假红的：串行跑 13 条全绿，并行跑必挂一条。）
    """
    import time

    结果 = (1, "找不到卸载器")
    for 名 in ("unins000.exe", "uninstall.exe"):
        p = 装 / 名
        if p.is_file():
            结果 = _跑安装器(p, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
            break
    for _ in range(60):
        剩 = [x for x in 装.rglob("*")] if 装.exists() else []
        if not 剩 and _卸载项(显示名) is None:
            break
        time.sleep(0.5)
    return 结果


def _卸载项(显示名: str):
    import winreg
    父 = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion\Uninstall")
    i = 0
    while True:
        try:
            k = winreg.EnumKey(父, i)
        except OSError:
            return None
        i += 1
        键 = winreg.OpenKey(父, k)
        有 = {}
        j = 0
        while True:
            try:
                名, 值, _ = winreg.EnumValue(键, j)
            except OSError:
                break
            有[名] = 值
            j += 1
        if str(有.get("DisplayName", "")).startswith(显示名):
            return 有
    return None


def _快捷方式(名字: str):
    根 = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return sorted(根.rglob(f"{名字}.lnk"))


@有inno
def test_打出来的是真的Inno安装包(向导包):
    """产物仍是「存根壳 + 载荷」，但**载荷里装的是 Inno 向导安装器**。

    📌 为什么查 `--查看` 而不是搜字节：Inno 的 exe 在载荷 zip 里是压缩过的，
       搜 "Inno Setup" 搜不到；而壳自己知道载荷是什么，它说的是实话。
    """
    包 = 向导包["包"]
    assert 包.read_bytes()[:2] == b"MZ", "不是 PE 文件"
    码, 文 = _跑程序(包, "--查看")
    assert 码 == 0, 文[-400:]
    assert "载荷：Inno Setup 向导安装器" in 文, (
        f"载荷不是向导版（最大可能是 ISCC 没找到、悄悄降级了）：{文[:400]}")


@有inno
def test_静默装到指定目录_有卸载项_有快捷方式(向导包):
    装 = 向导包["基"] / "装"
    try:
        码, 文 = _跑安装器(向导包["包"], "/VERYSILENT", "/SUPPRESSMSGBOXES",
                          "/NORESTART", "/SP-", f"/DIR={装}")
        assert 码 == 0, 文[-800:]
        assert (装 / "app" / "程序.jsh").is_file(), f"程序没装上：{list(装.iterdir())}"
        assert (装 / "清单.json").is_file(), "清单要留在安装目录"

        项 = _卸载项("M198自检")
        assert 项 is not None, "「应用和功能」里没有这一条"
        assert 项["DisplayVersion"] == "1.2.3", 项
        assert "1.2.3" in str(项["DisplayName"]), 项
        assert Path(项["InstallLocation"]).resolve() == 装.resolve(), 项

        快捷 = _快捷方式("M198自检")
        assert 快捷, "开始菜单里没有快捷方式"
    finally:
        _卸载(装)


@有inno
def test_老参数_S还能静默装(向导包):
    """🔴 存根版时代 `--目标` / `/S` 是**静默**的，老脚本还在用。

    Inno 自己做不到（详见 `rust/src/bin/packstub.rs` 里 `子安装器名` 那段），
    所以壳翻译成 `/VERYSILENT`。这条判据盯的就是「壳真的翻译了」——
    没有它的话表现是「弹一个向导等人点」，无人值守的脚本就挂在那儿。
    """
    装 = 向导包["基"] / "装S"
    try:
        码, 文 = _跑安装器(向导包["包"], "/S", "--目标", str(装), 超时=300)
        assert 码 == 0, 文[-800:]
        assert (装 / "app" / "程序.jsh").is_file(), (
            f"`/S --目标` 没装成（弹向导就会挂到超时）：{list(装.iterdir()) if 装.is_dir() else '目录都没有'}")
    finally:
        _卸载(装)


@有inno
def test_卸载之后目录注册表快捷方式全清(向导包):
    装 = 向导包["基"] / "装卸"
    码, 文 = _跑安装器(向导包["包"], "/S", "--目标", str(装))
    assert 码 == 0, 文[-600:]
    assert 装.is_dir() and _卸载项("M198自检") is not None

    码, 文 = _卸载(装)
    assert 码 == 0, 文[-600:]
    for _ in range(40):
        if not 装.exists() or not any(装.iterdir()):
            break
        import time
        time.sleep(0.5)
    剩 = [p.name for p in 装.rglob("*")] if 装.exists() else []
    assert not 剩, f"卸载后还留着：{剩[:6]}"
    assert _卸载项("M198自检") is None, "卸载后注册表里还有这一条"
    assert not _快捷方式("M198自检"), "卸载后开始菜单里还有快捷方式"
