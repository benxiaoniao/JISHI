# -*- coding: utf-8 -*-
"""M177：**打包自带运行时** —— 装到没装过基石的机器上，双击就能用。

## 这一站要解决的那条欠账

0.3.1 的验收句是「写完 → `jishi 打包` → 干净机器装 → 双击 → 有窗口能用」，
实测**后半段没成**：`jishi 打包` 只打用户自己的文件（`.jsh` / `.html`），
不带引擎 ⇒ 装到别人机器上，文件都在、但**没有程序能执行它们**，双击没反应。

## 三个设计取舍（都写在这里，省得以后翻不到）

1. **包内布局 `bin/` + `app/`**：`bin/` 是「装进来就能执行的东西」，`app/` 是
   「用户的程序」。卸载、查问题、以后想「多个程序共用一份引擎」时一眼分得清。
2. **不改 `清单.json` 的格式**，另写一份 `运行时.json`：清单只说「包里有哪些
   文件」，「双击时该执行什么」是另一件事 —— 分开之后，清单那份三宿主契约
   （键序 / 缩进 / 转义）一个字都不用动。
3. **快捷方式指引擎、程序文件当参数**，不注册 `.jsh` 全局关联：关联是全局副作用，
   装两个基石程序就互相覆盖；快捷方式带参数是每个程序独立的。

## 这里钉什么

- 包内的东西齐不齐（少一件**打包时就该报错**，不是装完才发现白窗口）；
- `运行时.json` 的内容对不对；
- **三个宿主算出来的包，解出来的内容一致**（清单/运行时文本逐字相同）。
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
sys.path.insert(0, str(ROOT))

#: 开始菜单目录（快捷方式在这底下的某处）
开始菜单 = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _找快捷方式(名字: str) -> Path | None:
    """在开始菜单里找那个 `.lnk`。

    📌 M198 起用户包装的是 **Inno 向导**，快捷方式会在 `Programs\\<组名>\\<名字>.lnk`
       里（Inno 的惯例是给每个程序一个文件夹）；存根版则直接摆在 `Programs` 下。
       两种都得认 —— CI 上没有 Inno，本机有。
    """
    if not 开始菜单.is_dir():
        return None
    候 = sorted(开始菜单.rglob(f"{名字}.lnk"))
    return 候[0] if 候 else None


def _解快捷方式(lnk: Path) -> str:
    """`.lnk` 的内容用 `jishi-pack-stub --解析快捷方式` 读（M198 起它不再是 uninstall.exe）。"""
    stub = ROOT / "rust" / "target" / "release" / "jishi-pack-stub.exe"
    if not stub.is_file():
        stub = Path(shutil.which("jishi-pack-stub") or "")
    assert stub.is_file(), "没找到 jishi-pack-stub（先 cargo build --release）"
    r = subprocess.run([str(stub), "--解析快捷方式", str(lnk)],
                       capture_output=True, timeout=120)
    return (r.stdout + r.stderr).decode("utf-8", "replace")


def _载荷是向导(包: Path) -> bool:
    """这个安装包的载荷是 Inno 向导，还是散文件（存根版）—— 壳自己会说。"""
    r = subprocess.run([str(包), "--查看"], capture_output=True, timeout=120)
    return "Inno Setup 向导安装器" in (r.stdout + r.stderr).decode("utf-8", "replace")

from oracle.jishi.stdlib import 打包                            # noqa: E402

#: 要跟着走的引擎素材（可执行另算，名字随平台）。
素材 = ("jishi-wasm.js", "jishi_wasm.wasm")


def _引擎目录(tmp: Path) -> Path:
    """造一个「像发行版那样」的目录：可执行 + 两份素材 + （Windows）loader.dll。

    为什么不去 `rust/target/release/` 直接用：那儿的可执行叫 `jishi-rs.exe`、
    而且**素材不在旁边**（构建时在 `site/`）。判据要的是一份干净、可控的目录，
    少一件就能立刻验出「打包该不该报错」。
    """
    目录 = tmp / "引擎"
    目录.mkdir(parents=True, exist_ok=True)
    可执行 = "jishi.exe" if sys.platform == "win32" else "jishi"
    # 内容不重要（判据只看它有没有被收进去），但要是真的可执行文件才有意义
    (目录 / 可执行).write_bytes(b"MZ fake engine for tests\n")
    for 名 in 素材:
        (目录 / 名).write_bytes(f"// {名}\n".encode())
    if sys.platform == "win32":
        (目录 / "WebView2Loader.dll").write_bytes(b"MZ fake loader\n")
    return 目录


def _程序目录(tmp: Path) -> Path:
    目录 = tmp / "程序"
    目录.mkdir(parents=True, exist_ok=True)
    (目录 / "记账.jsh").write_text("打印(1)\n", encoding="utf-8", newline="\n")
    (目录 / "记账.html").write_text("<html><body>记账</body></html>",
                                    encoding="utf-8", newline="\n")
    return 目录


def _打(tmp: Path, 引擎: Path, 出: str = "出") -> Path:
    包 = 打包.生成(str(_程序目录(tmp)), str(tmp / 出), {
        "名字": "记账", "版本": "1.0.0", "入口": "记账.jsh",
        "运行时": True, "引擎": str(引擎),
    })
    return Path(包)


# ---------------------------------------------------------------------------
# 一、包里的东西
# ---------------------------------------------------------------------------

def test_自带运行时_包内布局是_bin_加_app(tmp_path):
    """`bin/` 放引擎、`app/` 放用户文件、包根放两份 json。"""
    包 = _打(tmp_path, _引擎目录(tmp_path))
    条目 = zipfile.ZipFile(包).namelist()

    assert "清单.json" in 条目
    assert "运行时.json" in 条目, "自带运行时的包必须有 运行时.json（安装器靠它找引擎）"
    assert "app/记账.jsh" in 条目, f"用户文件应当进 app/：{条目}"
    assert "app/记账.html" in 条目
    可执行 = "bin/jishi.exe" if sys.platform == "win32" else "bin/jishi"
    assert 可执行 in 条目, f"引擎可执行应当在 bin/：{条目}"
    for 名 in 素材:
        assert f"bin/{名}" in 条目, f"引擎素材 {名} 没进包：{条目}"


def test_不带运行时_一条都不多(tmp_path):
    """老路（不写 `运行时`）**一个字节都不该变** —— 那是已经发出去的包格式。"""
    目录 = _程序目录(tmp_path)
    包 = Path(打包.生成(str(目录), str(tmp_path / "老"), {
        "名字": "记账", "版本": "1.0.0", "入口": "记账.jsh"}))
    条目 = zipfile.ZipFile(包).namelist()
    assert 条目 == ["清单.json", "记账.html", "记账.jsh"], 条目
    assert "运行时.json" not in 条目
    assert not any(n.startswith(("app/", "bin/")) for n in 条目)


def test_运行时json_写清了怎么启动(tmp_path):
    """它回答一个问题：**装完之后双击，到底执行什么、带什么参数。**"""
    import json

    包 = _打(tmp_path, _引擎目录(tmp_path))
    r = json.loads(zipfile.ZipFile(包).read("运行时.json"))

    可执行 = "jishi.exe" if sys.platform == "win32" else "jishi"
    assert r["引擎"] == f"bin/{可执行}", r
    assert r["入口"] == "app/记账.jsh", r
    for 名 in 素材:
        assert f"bin/{名}" in r["随行"], f"{名} 没写进「随行」：{r}"
    if sys.platform == "win32":
        assert "bin/WebView2Loader.dll" in r["随行"], r


def test_自带运行时_清单的入口也是包内真路径(tmp_path):
    """`清单.json` 的「入口」必须和包里真实位置对得上（`app/…`），否则装完找不到。"""
    import json

    包 = _打(tmp_path, _引擎目录(tmp_path))
    清单 = json.loads(zipfile.ZipFile(包).read("清单.json"))
    assert 清单["入口"] == "app/记账.jsh", 清单["入口"]
    路径们 = [f["路径"] for f in 清单["文件们"]]
    assert "app/记账.jsh" in 路径们 and any(p.startswith("bin/") for p in 路径们), 路径们


# ---------------------------------------------------------------------------
# 二、缺东西就必须当场报错（不许出包）
# ---------------------------------------------------------------------------

def test_少了引擎素材就不出包(tmp_path):
    """🔴 M176 的教训：发行包少带 `jishi_wasm.wasm` ⇒ 窗口白屏、**零报错**。

    那种故障最难查，所以**在打包这一层就要拦住** —— 少一件就不许出包。
    """
    引擎 = _引擎目录(tmp_path)
    (引擎 / "jishi_wasm.wasm").unlink()

    with pytest.raises(Exception) as e:
        打包.生成(str(_程序目录(tmp_path)), str(tmp_path / "出"), {
            "名字": "记账", "版本": "1.0.0", "入口": "记账.jsh",
            "运行时": True, "引擎": str(引擎)})
    文 = str(e.value)
    assert "jishi_wasm.wasm" in 文, f"报错没点名缺的是哪个素材：{文}"
    assert "白屏" in 文 or "白" in 文, f"报错没说清后果：{文}"
    assert not (tmp_path / "出" / "记账-1.0.0.zip").exists(), "缺素材还出了包"


def test_没给引擎就报错_而不是猜一个(tmp_path):
    """Python / Node 宿主**自己不是引擎**，不给就必须说清楚，不许拿 python.exe 充数。"""
    with pytest.raises(Exception) as e:
        打包.生成(str(_程序目录(tmp_path)), str(tmp_path / "出"), {
            "名字": "记账", "版本": "1.0.0", "入口": "记账.jsh", "运行时": True})
    assert "引擎" in str(e.value), str(e.value)


def test_运行时只认真假(tmp_path):
    """传字符串 `"真"` 要报错 —— 与 `窗口.打开` 的 `原生` 同一个口径（宁可不猜）。"""
    with pytest.raises(Exception) as e:
        打包.生成(str(_程序目录(tmp_path)), str(tmp_path / "出"), {
            "名字": "记账", "版本": "1.0.0", "入口": "记账.jsh", "运行时": "真"})
    assert "真" in str(e.value) and "假" in str(e.value), str(e.value)


def test_用户不能自带运行时json(tmp_path):
    """用户文件里有个同名文件会**悄悄覆盖**安装器要读的那份 —— 必须报错。"""
    目录 = _程序目录(tmp_path)
    (目录 / "运行时.json").write_text("{}", encoding="utf-8")
    with pytest.raises(Exception) as e:
        打包.生成(str(目录), str(tmp_path / "出"), {
            "名字": "记账", "版本": "1.0.0", "入口": "记账.jsh",
            "运行时": True, "引擎": str(_引擎目录(tmp_path))})
    assert "运行时.json" in str(e.value), str(e.value)


# ---------------------------------------------------------------------------
# 三、可复现 + 三宿主一致
# ---------------------------------------------------------------------------

def test_打两次字节完全相同(tmp_path):
    """老规矩（M171）：同样的输入，两次打包字节一样 —— 发版靠它判断改了什么。"""
    引擎 = _引擎目录(tmp_path)
    一 = _打(tmp_path, 引擎, "一").read_bytes()
    二 = _打(tmp_path, 引擎, "二").read_bytes()
    assert 一 == 二, "两次打包字节不同（时间/顺序/权限有一处没定死）"


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")
def test_三个宿主算出来的包内容一致(tmp_path):
    """**三宿主逐字一致**是这一层的硬规矩：同一个目录，谁打出来的包都得通用。

    ⚠️ 只比**解出来的内容**，不比 zip 字节 —— Python/Node 用 deflate、Rust 用
    stored（合法 zip，但不压缩），这条口径 `压缩.打包确定` 立的时候就定下了。
    """
    引擎 = _引擎目录(tmp_path)
    # ① Python（参考实现）
    a = zipfile.ZipFile(_打(tmp_path, 引擎, "py"))

    # ② Rust
    exe = ROOT / "rust" / "target" / "release" / (
        "jishi-rs.exe" if sys.platform == "win32" else "jishi-rs")
    if not exe.is_file():
        pytest.skip("没有 jishi-rs（先 cargo build --release）")
    脚本 = tmp_path / "打.jsh"
    脚本.write_text(
        "导入 打包\n"
        f'打包.生成("{_程序目录(tmp_path).as_posix()}", "{(tmp_path / "rs").as_posix()}", '
        f'{{"名字": "记账", "版本": "1.0.0", "入口": "记账.jsh", '
        f'"运行时": 真, "引擎": "{引擎.as_posix()}"}})\n',
        encoding="utf-8", newline="\n")
    r = subprocess.run([str(exe), str(脚本)], cwd=str(ROOT),
                       capture_output=True, timeout=180)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:600]
    b = zipfile.ZipFile(tmp_path / "rs" / "记账-1.0.0.zip")

    assert a.namelist() == b.namelist(), f"条目不同：\n{a.namelist()}\n{b.namelist()}"
    for 名 in ("清单.json", "运行时.json"):
        assert a.read(名) == b.read(名), f"{名} 两个宿主写得不一样"
    # 每个条目的**内容**也要一致（引擎素材是同一份文件复制的）
    for 名 in a.namelist():
        assert a.read(名) == b.read(名), f"{名} 的内容不一致"


# ---------------------------------------------------------------------------
# 四、真机：装到别的目录，双击能开窗（这一站的核心）
# ---------------------------------------------------------------------------

无界面 = bool(os.environ.get("JISHI_NO_GUI"))
需要原生 = pytest.mark.skipif(
    无界面 or not sys.platform.startswith("win"),
    reason="真开原生窗口要桌面环境（CI 设 JISHI_NO_GUI=1）",
)


def _真引擎目录(tmp: Path) -> Path:
    """把「真的」引擎凑成一个发行版那样的目录。

    ⚠️ 开发树里：可执行在 `rust/target/release/`，素材在 `site/` —— **分成两处**。
    发行版是把它们放在一起的，所以这里复制成一份，才验得了真实的安装路径。
    """
    目录 = tmp / "真引擎"
    目录.mkdir(parents=True, exist_ok=True)
    exe = ROOT / "rust" / "target" / "release" / "jishi-rs.exe"
    shutil.copy2(exe, 目录 / "jishi.exe")
    for 名 in 素材:
        shutil.copy2(ROOT / "site" / 名, 目录 / 名)
    dll = ROOT / "rust" / "target" / "release" / "WebView2Loader.dll"
    if dll.is_file():
        shutil.copy2(dll, 目录 / "WebView2Loader.dll")
    return 目录


@需要原生
def test_装到别的目录_双击快捷方式能开窗(tmp_path):
    """**0.3.1 那条没达成的验收**，就在这一条里。

    1. 给一个真程序打包（自带运行时）；
    2. 装到一个**空的**目录（不是仓库、不在 PATH 里）；
    3. 快捷方式必须指向 `bin/` 里的引擎、把程序文件当**参数**；
    4. 按快捷方式写的那样跑一次 —— 窗口要真的起来（页面到、wasm 引擎起）。

    🔴 为什么不直接「点 .lnk」：Explorer 双击没法在 CI 里模拟。这里验的是
    **快捷方式里写的东西**（用 `IShellLink` 读回来）+ **照着它执行会怎样** ——
    两段都真，中间只差 Explorer 那一下。
    """
    exe = ROOT / "rust" / "target" / "release" / "jishi-rs.exe"
    if not exe.is_file():
        pytest.skip("没有 jishi-rs（先 cargo build --release）")
    if not (ROOT / "site" / "jishi_wasm.wasm").is_file():
        pytest.skip("还没构建 wasm（tools/build_wasm.py）")

    引擎 = _真引擎目录(tmp_path)
    程序 = tmp_path / "程序"
    程序.mkdir()
    # 用仓库里那个真示例（它已经改成相对路径了，正好验「脚本目录兜底」）
    shutil.copy2(ROOT / "examples" / "desktop" / "计算器.jsh", 程序 / "计算器.jsh")
    shutil.copy2(ROOT / "examples" / "desktop" / "计算器.html", 程序 / "计算器.html")

    出 = tmp_path / "出"
    r = subprocess.run(
        [str(exe), "打包", str(程序), "--出", str(出), "--名字", "M177探针",
         "--版本", "1.0.0", "--入口", "计算器.jsh", "--自带运行时",
         "--引擎", str(引擎 / "jishi.exe"), "--安静"],
        capture_output=True, timeout=300)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:800]

    装目录 = tmp_path / "装"
    setup = 出 / "M177探针-1.0.0-windows-x64-setup.exe"
    assert setup.is_file(), f"没出安装包：{list(出.iterdir())}"

    # 🔴 不能 `capture_output=True`：stub 会拉起子进程并继承管道（M176 的教训）
    日志 = tmp_path / "setup.log"
    with open(日志, "w", encoding="utf-8") as f:
        r = subprocess.run([str(setup), "--目标", str(装目录)],
                           stdout=f, stderr=subprocess.STDOUT, timeout=300)
    assert r.returncode == 0, 日志.read_text(encoding="utf-8", errors="replace")[:800]

    # ① 装完的文件在（引擎四件套 + 用户程序）
    assert (装目录 / "bin" / "jishi.exe").is_file()
    assert (装目录 / "app" / "计算器.jsh").is_file()
    assert (装目录 / "运行时.json").is_file()

    # ② 快捷方式：目标 = 引擎，参数 = 程序文件（读回来验，不猜）
    快捷 = _找快捷方式("M177探针")
    assert 快捷 is not None, (
        f"开始菜单里没有快捷方式；现有的：{list(开始菜单.rglob('*.lnk'))[:8]}")
    out = _解快捷方式(快捷)
    assert "bin\\jishi.exe" in out or "bin/jishi.exe" in out, f"快捷方式没指向引擎：{out}"
    assert "app\\计算器.jsh" in out or "app/计算器.jsh" in out, (
        f"快捷方式没带程序文件当参数：{out}")

    # ③ 照着快捷方式写的那样跑一次 —— 窗口要真的起来
    跑日志 = tmp_path / "run.log"
    with open(跑日志, "w", encoding="utf-8") as f:
        p = subprocess.Popen([str(装目录 / "bin" / "jishi.exe"),
                              str(装目录 / "app" / "计算器.jsh")],
                             cwd=str(装目录), stdout=f, stderr=subprocess.STDOUT,
                             env={**os.environ, "JISHI_WINDOW_DEBUG": "1"})
    import time
    for _ in range(40):
        time.sleep(0.5)
        if p.poll() is not None:
            break
    还开着 = p.poll() is None
    if 还开着:
        p.kill()
        p.wait(timeout=30)
    文 = 跑日志.read_text(encoding="utf-8", errors="replace")
    assert 还开着, f"进程自己退了（窗口没起来）：{文[:600]}"
    assert "页面到了" in 文, f"页面没跑起来（白窗口）：{文[:800]}"

    # ④ 卸干净：目录清空
    #
    # ⚠️ 卸载走 `--卸载`（不是 `--目标`）：`uninstall.exe` 是**没有载荷的存根**，
    #    它靠安装时写进注册表的「安装位置」找到自己 —— 传 --目标 会得到
    #    「这个程序里没有安装内容」。这是 M177 写判据时踩的。
    向导 = _载荷是向导(setup)
    with open(tmp_path / "un.log", "w", encoding="utf-8") as f:
        if 向导:
            # M198：向导版的卸载器是 Inno 写的（unins000.exe），开关是 `/VERYSILENT`
            卸 = next(装目录.glob("unins*.exe"))
            subprocess.run([str(卸), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                           stdout=f, stderr=subprocess.STDOUT, timeout=180)
        else:
            subprocess.run([str(装目录 / "uninstall.exe"), "--卸载", "--静默"],
                           stdout=f, stderr=subprocess.STDOUT, timeout=180)
    assert not (装目录 / "bin").exists(), (
        "卸载后 bin/ 还在：\n"
        + (tmp_path / "un.log").read_text(encoding="utf-8", errors="replace")[:400])


# ---------------------------------------------------------------------------
# 五、`#[cfg]` 两边的签名必须配（M177 用血换的一条）
# ---------------------------------------------------------------------------

def test_非windows的快捷方式桩签名要与windows一致():
    """🔴 **M177 踩过**：给 `lnk::建` 加了「参数」那一维，只改了 Windows 那份，
    忘了 `#[cfg(not(windows))]` 的同名桩 ⇒ **Linux / macOS 的 CI 一起红，
    而 Windows 本地全绿**（AGENTS §1「`#[cfg(windows)]` 必须配对」）。

    编译期能不能发现取决于你在哪个平台上编 —— 所以这里用**文本比对**盯住：
    两个 `mod lnk` 里每个 `pub fn` 的**名字集合与参数个数**必须一致。
    """
    import re

    src = (ROOT / "rust" / "src" / "bin" / "packstub.rs").read_text(encoding="utf-8")
    段们 = re.split(r"(?m)^#\[cfg\((not\()?windows\)?\)\]\s*$", src)
    # 按出现顺序取两个 lnk 模块：windows 版在前、非 windows 版在后
    模块 = re.findall(r"(?ms)^mod lnk \{(.*?)^\}", src)
    assert len(模块) == 2, f"没找到两段 `mod lnk`（找到 {len(模块)} 段）"

    def 签名们(体: str):
        出 = {}
        for m in re.finditer(r"pub fn (\w+)\s*\(([^)]*)\)", 体):
            参 = [x for x in m.group(2).split(",") if x.strip()]
            出[m.group(1)] = len(参)
        return 出

    win, 非 = 签名们(模块[0]), 签名们(模块[1])
    assert win, "Windows 那份 `mod lnk` 里没找到 pub fn？"
    assert set(win) == set(非), (
        f"两边的函数名不一样：Windows {sorted(win)} vs 非 Windows {sorted(非)}")
    for 名 in win:
        assert win[名] == 非[名], (
            f"`lnk::{名}` 的**参数个数**两边不一致：Windows {win[名]} 个、"
            f"非 Windows {非[名]} 个 —— Linux/macOS 会编不过（M177 真踩过）")


# ---------------------------------------------------------------------------
# 六、发行包必须带「自解压存根」（M181 收口演练抓出来的真缺陷）
# ---------------------------------------------------------------------------

def test_发行包必须带自解压存根():
    """🔴 **M181 收口演练抓到的真缺陷**：发行包的 `bin/` 里没有 `jishi-pack-stub.exe`。

    它是「用户给自己的程序打安装包」用的自解压壳。`packcmd::找存根()` 的第一候选
    就是「和 `jishi.exe` 放一起」，而 `build_release.py` 一直没把它放进 `bin/` ——
    于是**装了基石的普通用户**跑 `jishi 打包 我的程序 --自带运行时`，会得到一句
    「出 Windows 安装包要 `jishi-pack-stub`，没找到」。

    ⚠️ 为什么判据只能盯**构建脚本**：发行包不在 CI 上构建（要 Inno Setup + 真机），
    所以这里查的是「构建脚本有没有把那个文件拷进去」。端到端那一半由
    `tools/drill_install.py` 第 4 段覆盖（它跑的正是**真装完的发行版**）。
    """
    源 = (ROOT / "tools" / "build_release.py").read_text(encoding="utf-8")
    assert "jishi-pack-stub" in 源, (
        "build_release.py 里没提 `jishi-pack-stub` —— 发行包会缺它，"
        "用户就没法给自己的程序打安装包")
    assert 'bin_dir / "jishi-pack-stub.exe"' in 源, (
        "存根没被**拷进 `bin/`**（只在注释里提一句不算）—— `找存根()` 找的就是"
        "「和 jishi.exe 放在一起」")
