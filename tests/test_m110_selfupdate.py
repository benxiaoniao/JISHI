# -*- coding: utf-8 -*-
"""M110：语言本体的**升级**与**卸载**（`jishi 升级` / `jishi 卸载 --自身`）。

判据按 `docs/路线图.md` §12.3.2 那张表来：

| 判据 | 在哪个用例里 |
|---|---|
| 能从 N-1 升到 N（**真旧版本**） | `test_升级_从真旧版本升到真新版本` |
| **必须校验 SHA256** | `test_升级_哈希对不上就不装` |
| 已是最新 → 明确提示 | `test_升级_已是最新就说不用升级` |
| 失败**回滚** | `test_升级_替换失败要回滚` |
| 多镜像回退 | `test_升级_多镜像回退` |
| Windows 换正在运行的 exe | `test_升级_演练会说明安装方式` + 真机演练（M111 发版时） |

⚠️ **真包演练要本机有 `dist/release/` 的真发行包**（那是构建产物、不进仓库）——
没有就 `skip`（与「没有 rust 产物就 skip」同一个规矩）。有的话，跑的是
**真的 0.2.1 → 真的 0.2.2**：装的是真的 PyInstaller 老包（`bin/jishi/jishi.exe`，
嵌套布局），升完变成真的单二进制（`bin/jishi.exe`，平铺）—— 连**布局变化**都覆盖到了。
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from conftest import rust_exe_path          # noqa: E402

RUST_EXE = rust_exe_path()
RELEASE = ROOT / "dist" / "release"

旧版 = "0.2.1"
新版 = "0.2.2"


def _平台() -> str:
    if os.name == "nt":
        return "windows-x64"
    if sys.platform == "darwin":
        return "macos-arm64"
    return "linux-x64"


平台 = _平台()
包后缀 = ".zip" if 平台 == "windows-x64" else ".tar.gz"
可执行 = "bin/jishi.exe" if 平台 == "windows-x64" else "bin/jishi"


def _包路径(版本: str) -> Path:
    return RELEASE / f"jishi-{版本}-{平台}{包后缀}"


def _跑(args, env_extra=None, cwd=None):
    env = dict(os.environ)
    env["JISHI_NO_PATH"] = "1"
    env.pop("JISHI_RELEASE_INDEX", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([str(RUST_EXE), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, cwd=cwd)


def _装到(家: Path, 版本: str) -> None:
    """把一个真发行包「装」进 `家`（= 解压 + 写 VERSION，与 install.sh 做的事一致）。"""
    pkg = _包路径(版本)
    家.mkdir(parents=True, exist_ok=True)
    if 包后缀 == ".zip":
        with zipfile.ZipFile(pkg) as z:
            根 = sorted({n.split("/")[0] for n in z.namelist()})[0]
            z.extractall(家)
    else:
        with tarfile.open(pkg) as t:
            根 = sorted({m.name.split("/")[0] for m in t.getmembers()})[0]
            t.extractall(家)
    子 = 家 / 根
    for it in 子.iterdir():
        os.replace(it, 家 / it.name)
    子.rmdir()
    (家 / "VERSION").write_text(版本 + "\n", encoding="ascii")


def _可执行(家: Path) -> Path:
    """找安装目录里的那个可执行文件。

    ⚠️ **两个真实版本的布局不一样**：0.2.1 是 PyInstaller 的**嵌套**布局
    （`bin/jishi/jishi.exe` 与 `bin/jishi-mcp/`），0.2.2 起是 cargo 的**平铺**
    单二进制（`bin/jishi.exe`）。判据要按「找得到就行」写，别把布局写死。
    """
    平铺 = 家 / ("bin/jishi.exe" if os.name == "nt" else "bin/jishi")
    if 平铺.is_file():
        return 平铺
    嵌套 = 家 / ("bin/jishi/jishi.exe" if os.name == "nt" else "bin/jishi/jishi")
    if 嵌套.is_file():
        return 嵌套
    raise AssertionError(f"安装目录里找不到可执行文件：{家}")


def _自报版本(家: Path) -> str:
    r = subprocess.run([str(_可执行(家)), "--version"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return (r.stdout or "").strip()


def _bin指纹(家: Path) -> str:
    """`家/bin` 下所有文件的 (相对名, 内容) 指纹 —— 用来判「一点都没动」。"""
    h = hashlib.sha256()
    for p in sorted(家.joinpath("bin").rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(家)).encode("utf-8"))
            h.update(p.read_bytes())
    return h.hexdigest()


def _做镜像(目录: Path, 版本: str, 源包: Path | None = None) -> Path:
    """造一个 `file://` 镜像：把包拷进去 + 生成清单。返回清单路径。"""
    目录.mkdir(parents=True, exist_ok=True)
    包 = 源包 or _包路径(版本)
    shutil.copy2(包, 目录 / 包.name)
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "build_version_manifest.py"),
                        str(目录), "--版本", 版本],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return 目录 / "jishi-release.json"


def _file_url(p: Path) -> str:
    return "file:///" + str(p).replace("\\", "/").lstrip("/")


_有真包 = pytest.mark.skipif(
    not (_包路径(旧版).is_file() and _包路径(新版).is_file()),
    reason=f"本机没有 dist/release 的真发行包（{旧版} / {新版} · {平台}）")


# ---------------------------------------------------------------------------
# 一、真·旧版本 → 真·新版本（核心判据）
# ---------------------------------------------------------------------------

@_有真包
def test_升级_从真旧版本升到真新版本(tmp_path):
    """装**真的 0.2.1** → `jishi 升级` → 跑起来**真的 0.2.2**。

    跨版本的布局变化也在这里覆盖：0.2.1 是 PyInstaller 的**嵌套**布局
    （`bin/jishi/jishi.exe`），0.2.2 是 cargo 的**平铺**单二进制（`bin/jishi.exe`）。
    """
    家 = tmp_path / "家"
    _装到(家, 旧版)
    assert "0.2.1" in _自报版本(家), "先确认装的确实是旧版"

    镜像 = tmp_path / "镜像"
    清单 = _做镜像(镜像, 新版)

    r = _跑(["升级", "--源", _file_url(清单)], {"JISHI_HOME": str(家)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0.2.1" in r.stdout and "0.2.2" in r.stdout, r.stdout

    # 1) 版本记录换了
    assert (家 / "VERSION").read_text(encoding="ascii").strip() == 新版
    # 2) 新二进制**真的能跑**，而且自报的就是新版
    assert "0.2.2" in _自报版本(家), "升级后跑起来还是旧的"
    # 3) 布局真的换了（老包的嵌套目录没了）
    assert not (家 / "bin" / "jishi").exists()


@_有真包
def test_升级_已是最新就说不用升级(tmp_path):
    家 = tmp_path / "家"
    _装到(家, 新版)                      # 已经是最新
    镜像 = tmp_path / "镜像"
    清单 = _做镜像(镜像, 新版)
    r = _跑(["升级", "--源", _file_url(清单)], {"JISHI_HOME": str(家)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "已是最新" in r.stdout and "不用升级" in r.stdout, r.stdout
    assert (家 / "VERSION").read_text(encoding="ascii").strip() == 新版


@_有真包
def test_升级_演练什么都不动(tmp_path):
    家 = tmp_path / "家"
    _装到(家, 旧版)
    旧指纹 = _bin指纹(家)
    镜像 = tmp_path / "镜像"
    清单 = _做镜像(镜像, 新版)

    r = _跑(["升级", "--演练", "--源", _file_url(清单)], {"JISHI_HOME": str(家)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[演练]" in r.stdout, r.stdout
    assert (家 / "VERSION").read_text(encoding="ascii").strip() == 旧版
    assert _bin指纹(家) == 旧指纹, "演练动到文件了"


# ---------------------------------------------------------------------------
# 二、SHA256 是硬闸门
# ---------------------------------------------------------------------------

@_有真包
def test_升级_哈希对不上就不装(tmp_path):
    家 = tmp_path / "家"
    _装到(家, 旧版)
    旧指纹 = _bin指纹(家)

    镜像 = tmp_path / "镜像"
    清单 = _做镜像(镜像, 新版)
    # 偷偷改包里的一个字节（哈希就对不上了）
    包 = 镜像 / f"jishi-{新版}-{平台}{包后缀}"
    data = bytearray(包.read_bytes())
    data[-1] ^= 0xFF
    包.write_bytes(bytes(data))

    r = _跑(["升级", "--源", _file_url(清单)], {"JISHI_HOME": str(家)})
    assert r.returncode != 0, r.stdout
    assert "SHA256 对不上" in (r.stdout + r.stderr), r.stdout + r.stderr
    # 一点都没动
    assert (家 / "VERSION").read_text(encoding="ascii").strip() == 旧版
    assert _bin指纹(家) == 旧指纹, "哈希没过却动了文件"


@_有真包
def test_升级_本地包也要过哈希(tmp_path):
    家 = tmp_path / "家"
    _装到(家, 旧版)
    镜像 = tmp_path / "镜像"
    清单 = _做镜像(镜像, 新版)
    坏包 = tmp_path / "坏.zip"
    坏包.write_bytes(b"not a package at all")   # ⚠️ 字节字面量只能 ASCII

    r = _跑(["升级", "--源", _file_url(清单), "--本地包", str(坏包)],
            {"JISHI_HOME": str(家)})
    assert r.returncode != 0, r.stdout
    assert "SHA256 对不上" in (r.stdout + r.stderr), r.stdout + r.stderr


# ---------------------------------------------------------------------------
# 三、清单与多镜像
# ---------------------------------------------------------------------------

@_有真包
def test_升级_多镜像回退(tmp_path):
    """第一个源读不到（指向不存在的路径）→ 自动换第二个。"""
    家 = tmp_path / "家"
    _装到(家, 旧版)
    镜像 = tmp_path / "镜像"
    清单 = _做镜像(镜像, 新版)
    坏源 = _file_url(tmp_path / "没有这个目录" / "jishi-release.json")

    r = _跑(["升级", "--源", 坏源, "--源", _file_url(清单)], {"JISHI_HOME": str(家)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0.2.2" in r.stdout, r.stdout


def test_升级_清单格式不对要报错(tmp_path):
    家 = tmp_path / "家"
    家.mkdir()
    (家 / "VERSION").write_text("0.1.0\n", encoding="ascii")
    镜像 = tmp_path / "镜像"
    镜像.mkdir()
    假 = 镜像 / "jishi-release.json"
    假.write_text(json.dumps({"格式": "别的什么东西", "格式版本": 1, "最新": "9.9.9"},
                             ensure_ascii=False), encoding="utf-8")
    r = _跑(["升级", "--源", _file_url(假)], {"JISHI_HOME": str(家)})
    assert r.returncode != 0
    assert "不是基石的版本清单" in (r.stdout + r.stderr), r.stdout + r.stderr


def test_升级_所有源都读不到要给人话(tmp_path):
    家 = tmp_path / "家"
    家.mkdir()
    (家 / "VERSION").write_text("0.1.0\n", encoding="ascii")
    坏 = _file_url(tmp_path / "没有" / "jishi-release.json")
    r = _跑(["升级", "--源", 坏], {"JISHI_HOME": str(家)})
    assert r.returncode != 0
    出 = r.stdout + r.stderr
    assert "读不到基石的版本清单" in 出, 出
    assert "--本地包" in 出, "要给出「离线升级」的办法"


# ---------------------------------------------------------------------------
# 四、形态识别
# ---------------------------------------------------------------------------

def test_安装形态_JISHI_HOME里有卸载器就算安装程序版(tmp_path):
    家 = tmp_path / "家"
    家.mkdir()
    (家 / "VERSION").write_text("0.2.2\n", encoding="ascii")
    (家 / "unins000.exe").write_bytes(b"stub")     # 假装是 Inno 装出来的

    清单 = tmp_path / "jishi-release.json"
    清单.write_text(json.dumps({
        "格式": "jishi-release-index", "格式版本": 1, "最新": "9.9.9",
        "版本": {"9.9.9": {"日期": "x", "平台": {平台: {"包": {
            "文件": "jishi-9.9.9-x.zip", "大小": 1, "校验和": "sha256:00"}}}}},
    }, ensure_ascii=False), encoding="utf-8")

    r = _跑(["升级", "--演练", "--源", _file_url(清单)], {"JISHI_HOME": str(家)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Windows 安装程序版" in r.stdout, r.stdout
    # 清单里没有「安装程序」资产 ⇒ 演练要说清楚「走不了静默升级」
    assert "走不了静默升级" in r.stdout, r.stdout


def test_安装形态_没有卸载器就是便携版(tmp_path):
    家 = tmp_path / "家"
    家.mkdir()
    (家 / "VERSION").write_text("0.2.2\n", encoding="ascii")
    清单 = tmp_path / "jishi-release.json"
    清单.write_text(json.dumps({
        "格式": "jishi-release-index", "格式版本": 1, "最新": "9.9.9",
        "版本": {"9.9.9": {"日期": "x", "平台": {平台: {"包": {
            "文件": "jishi-9.9.9-x.zip", "大小": 1, "校验和": "sha256:00"}}}}},
    }, ensure_ascii=False), encoding="utf-8")
    r = _跑(["升级", "--演练", "--源", _file_url(清单)], {"JISHI_HOME": str(家)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "便携版" in r.stdout, r.stdout
    assert "解压" in r.stdout and "替换" in r.stdout if os.name == "nt" \
        else "install.sh" in r.stdout, r.stdout


# ---------------------------------------------------------------------------
# 五、卸载语言本体
# ---------------------------------------------------------------------------

def test_卸载_不带包名且非交互_必须加是(tmp_path):
    """🔴 不带包名那条太容易误触（用户可能以为在卸某个包）⇒ 非交互必须 `--是`。"""
    家 = tmp_path / "家"
    (家 / "bin").mkdir(parents=True)
    (家 / "bin" / "jishi.exe").write_bytes(b"x")
    (家 / "VERSION").write_text("0.2.2\n", encoding="ascii")

    r = _跑(["卸载"], {"JISHI_HOME": str(家)})
    assert r.returncode != 0, r.stdout
    assert "非交互式运行" in (r.stdout + r.stderr), r.stdout + r.stderr
    assert 家.is_dir(), "没确认就把安装目录删了"


def test_卸载_带是才真删(tmp_path):
    家 = tmp_path / "家"
    (家 / "bin").mkdir(parents=True)
    (家 / "bin" / "jishi.exe").write_bytes(b"x")
    (家 / "VERSION").write_text("0.2.2\n", encoding="ascii")

    r = _跑(["卸载", "--是"], {"JISHI_HOME": str(家)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert not 家.exists(), "说了 --是 却没删掉"
    assert "基石语言本体" in r.stdout, r.stdout


def test_卸载_自身与演练(tmp_path):
    家 = tmp_path / "家"
    (家 / "bin").mkdir(parents=True)
    (家 / "VERSION").write_text("0.2.2\n", encoding="ascii")

    r = _跑(["卸载", "--自身", "--演练"], {"JISHI_HOME": str(家)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[演练]" in r.stdout, r.stdout
    assert 家.is_dir(), "演练把目录删了"

    r = _跑(["卸载", "--自身", "--是"], {"JISHI_HOME": str(家)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert not 家.exists()


def test_卸载_包名仍然是卸包(tmp_path):
    """`卸载 <包名>` 不许被语言本体的卸载抢走（回归）。"""
    家 = tmp_path / "家"
    (家 / "bin").mkdir(parents=True)
    (家 / "VERSION").write_text("0.2.2\n", encoding="ascii")
    r = _跑(["卸载", "某个不存在的包"], {"JISHI_HOME": str(家)})
    出 = r.stdout + r.stderr
    assert "基石语言本体" not in 出, "带包名的卸载被语言本体抢走了"
    assert 家.is_dir()


# ---------------------------------------------------------------------------
# 六、版本清单生成器
# ---------------------------------------------------------------------------

def test_版本清单_只写文件名不写绝对地址(tmp_path):
    """⚠️ 写死绝对地址的话，自建站与 GitHub 就得两份清单 —— 这里钉住「只写文件名」。"""
    # 造两个假包（内容随便，哈希算得出来就行）
    (tmp_path / f"jishi-9.9.9-{平台}{包后缀}").write_bytes(b"pkg-bytes")
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "build_version_manifest.py"),
                        str(tmp_path), "--版本", "9.9.9"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    m = json.loads((tmp_path / "jishi-release.json").read_text(encoding="utf-8"))
    assert m["格式"] == "jishi-release-index" and m["格式版本"] == 1
    assert m["最新"] == "9.9.9"
    包 = m["版本"]["9.9.9"]["平台"][平台]["包"]
    assert 包["文件"] == f"jishi-9.9.9-{平台}{包后缀}"
    assert 包["校验和"] == "sha256:" + hashlib.sha256(b"pkg-bytes").hexdigest()
    assert "下载地址" not in 包 and "http" not in json.dumps(m)


def test_版本清单_只有安装程序不给过(tmp_path):
    """残清单（某个平台只有 setup.exe、没有 zip）要**明确报错**，不发布半份。"""
    (tmp_path / "jishi-9.9.9-windows-x64-setup.exe").write_bytes(b"x")
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "build_version_manifest.py"),
                        str(tmp_path), "--版本", "9.9.9"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode != 0, r.stdout
    assert "只有安装程序" in (r.stdout + r.stderr)


@_有真包
def test_版本清单的哈希与包逐字节对得上(tmp_path):
    """🔴 清单说的哈希 == 真文件的哈希 == `SHA256SUMS` 里的那个（三边一致）。"""
    清单 = _做镜像(tmp_path / "镜像", 新版)
    m = json.loads(清单.read_text(encoding="utf-8"))
    包 = m["版本"][新版]["平台"][平台]["包"]
    真 = _包路径(新版)
    assert 包["大小"] == 真.stat().st_size
    assert 包["校验和"] == "sha256:" + hashlib.sha256(真.read_bytes()).hexdigest()

    # 与发行目录里的 SHA256SUMS 再对一遍（那份是发版时生成的）。
    # ⚠️ `dist/release/SHA256SUMS` **每版只有一份**（镜像同步按最新版覆盖，
    #    因为「本站那份必须与 GitHub 那份逐字节相同」），所以只有当被测版本
    #    **就是**镜像里最新那一版时这一半才适用 —— 否则（例如 0.2.3 发布之后
    #    还在测 0.2.2 的升级演练）跳过这一半，别拿它当「对不上」。
    和 = RELEASE / "SHA256SUMS"
    if 和.is_file():
        want = None
        for 行 in 和.read_text(encoding="utf-8").splitlines():
            格 = 行.split()
            if len(格) == 2 and 格[1] == 真.name:
                want = 格[0]
        if want is not None:
            assert want == 包["校验和"].removeprefix("sha256:")


# ---------------------------------------------------------------------------
# 七、安装脚本的环境变量（升级靠它们做演练）
# ---------------------------------------------------------------------------

def test_安装脚本支持JISHI_HOME覆盖():
    for 名 in ("install.sh", "uninstall.sh", "install.ps1", "uninstall.ps1"):
        t = (ROOT / "tools" / 名).read_text(encoding="utf-8")
        assert "JISHI_HOME" in t, f"{名} 没有 JISHI_HOME 覆盖（升级 / 演练要用）"
    # install 两个脚本要写 VERSION（升级靠它判断「本机是哪一版」）
    assert "VERSION" in (ROOT / "tools" / "install.sh").read_text(encoding="utf-8")
    assert "VERSION" in (ROOT / "tools" / "install.ps1").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 八、发版链路（清单要真的发出去、而且发对）
# ---------------------------------------------------------------------------

def test_release工作流_清单排在统一校验和之前():
    """🔴 **顺序是有意义的**：`jishi-release.json` 必须先于 `SHA256SUMS` 生成，
    这样它自己也进那份清单；否则镜像同步那边会遇到「资产不在清单里」
    —— 而那正是「拿没校验充当校验通过」的形状。"""
    yml = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "build_version_manifest.py" in yml, "发版工作流没生成版本清单"
    i清单 = yml.index("生成语言本体版本清单")
    i校验和 = yml.index("汇总产物并生成统一校验和")
    assert i清单 < i校验和, "jishi-release.json 必须排在 SHA256SUMS 之前"


def test_镜像同步会强制拉版本清单():
    """清单的文件名不随版本变 ⇒ 必须 `force` 重下，否则「大小一样就跳过」会把
    上一版的清单当成这一版的（`SHA256SUMS` 踩过一模一样的坑）。"""
    t = (ROOT / "tools" / "fetch_release_mirror.py").read_text(encoding="utf-8")
    assert 'OUT / "jishi-release.json"' in t, "镜像同步没拉版本清单"
    i = t.index('OUT / "jishi-release.json"')
    assert "force=True" in t[i:i + 200], "拉版本清单没带 force"


def test_升级器取清单要绕开CDN缓存():
    """🔴 2026-10-17 实测踩到：发了 0.2.4，线上 `jishi-release.json` 仍是 0.2.3
    （`/download/` 给了 30 天缓存，而这份**文件名固定、内容每版都变**），
    于是 `jishi 升级` **永远认为「已是最新」**。

    判据：取清单时给 **http(s)** 地址带一个时间戳（绕开 CDN，不依赖运维记得刷新）。
    `file://` 离线镜像**不许**带 —— 带了就找不到本地文件。两条都在源码里钉住。
    """
    t = (ROOT / "rust" / "src" / "selfupdate.rs").read_text(encoding="utf-8")
    i = t.index("fn 清单来源")
    块 = t[i:i + 2000]
    assert "t={戳}" in 块, "取清单没带时间戳 —— CDN 会把上一版缓存很久，升级永远看不到新版"
    assert 'starts_with("http' in 块, "时间戳只该加给 http(s)：file:// 镜像带了会找不到文件"


def test_升级与卸载都在CLI用法里():
    r = _跑(["--help"])
    出 = r.stdout + r.stderr
    assert "升级" in 出 and "卸载" in 出, 出
