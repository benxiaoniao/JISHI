# -*- coding: utf-8 -*-
"""M172 · `jishi 打包`：一条命令出四类产物。

## 这一组在防什么

1. **四类产物真的都出得来**，而且名字/内容互相对得上（清单里的「平台」跟产物名
   一致，安装器解出来的是同一个程序）。
2. **两套命令行（Python / Rust）产物同名、同结构、同文本** —— 文本类产物
   （`install.sh` / `Info.plist`）是**逐字**比的，因为它们会被用户直接看到、
   直接执行。二进制类（zip / tar.gz）比「解出来的内容」：压缩算法不同是已知的
   （Rust 侧只做 stored），口径与 `压缩.打包确定` 一致。
3. **`install.sh` 真能装、真能卸** —— 不是「生成了一个文件就算完」。
4. **可复现**：同样的输入打两遍，字节相同（沿用 M109/M168/M171 那条规矩）。
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                            # noqa: E402

有sh = shutil.which("sh") is not None


def _造源(tmp_path):
    源 = tmp_path / "app"
    (源 / "bin").mkdir(parents=True)
    (源 / "share").mkdir(parents=True)
    (源 / "bin" / "演示.exe").write_bytes(b"MZ fake")
    (源 / "share" / "读我.txt").write_text("用法：演示", encoding="utf-8")
    return 源


def _跑(参数, tmp_path, exe=None, 期望=0):
    命令 = ([str(exe)] if exe else [sys.executable, "-m", "oracle.jishi.cli"]) + ["打包"] + 参数
    r = subprocess.run(命令, cwd=str(tmp_path), capture_output=True, timeout=300, input=b"")
    出 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode == 期望, f"退出码 {r.returncode}（要 {期望}）：{出[:600]}"
    return 出


def _产物(目录):
    """产物名集合（`.app` 只算目录本身）。"""
    return sorted(os.listdir(目录))


# ---------------------------------------------------------------------------
# 一、四类产物
# ---------------------------------------------------------------------------

def test_一条命令出四类产物(tmp_path):
    _造源(tmp_path)
    出 = _跑(["app", "--名字", "演示", "--版本", "1.0.0", "--入口", "bin/演示.exe",
              "--出", "出"], tmp_path)
    目录 = tmp_path / "出"
    名们 = _产物(目录)
    assert 名们 == sorted([
        "演示-1.0.0.zip",
        "演示-1.0.0-windows-x64-setup.exe",
        "演示.app",
        "演示-1.0.0-macos-arm64.tar.gz",
        "演示-1.0.0-linux-x64.tar.gz",
    ]), 名们
    for 键 in ("便携", "安装包", "应用", "压缩包"):
        assert 键 in 出, f"没打印「{键}」：{出[:400]}"


def test_便携zip解开就能用(tmp_path):
    _造源(tmp_path)
    _跑(["app", "--名字", "演示", "--版本", "1.0.0", "--入口", "bin/演示.exe",
         "--出", "出"], tmp_path)
    z = zipfile.ZipFile(tmp_path / "出" / "演示-1.0.0.zip")
    assert z.namelist() == ["清单.json", "bin/演示.exe", "share/读我.txt"], z.namelist()
    清单 = json.loads(z.read("清单.json").decode("utf-8"))
    assert 清单["名字"] == "演示" and 清单["版本"] == "1.0.0"
    assert 清单["平台"] == "", "便携包是全平台的，清单里的平台不该写成某一台"


def test_app束里有Info_plist和入口(tmp_path):
    _造源(tmp_path)
    _跑(["app", "--名字", "演示", "--版本", "1.0.0", "--入口", "bin/演示.exe",
         "--出", "出"], tmp_path)
    内容 = tmp_path / "出" / "演示.app" / "Contents"
    assert (内容 / "MacOS" / "bin" / "演示.exe").read_bytes() == b"MZ fake"
    plist = (内容 / "Info.plist").read_text(encoding="utf-8")
    assert "<string>演示.exe</string>" in plist, plist
    assert "<string>1.0.0</string>" in plist
    assert "<key>CFBundlePackageType</key>" in plist
    assert (内容 / "PkgInfo").read_text(encoding="utf-8") == "APPL????"
    # 标识必须是 ASCII 反向域名（名字是中文，不能直接塞进去）
    import re
    m = re.search(r"<key>CFBundleIdentifier</key>\s*<string>([^<]+)</string>", plist)
    assert m and m.group(1).startswith("cn.jishi.")
    assert m.group(1).isascii(), m.group(1)


@pytest.mark.skipif(not 有sh, reason="这台机器没有 sh")
def test_install_sh真能装真能卸(tmp_path):
    """Linux 那包解开跑 `install.sh`：文件到位、`bin/` 里有能执行的入口，卸完干净。"""
    _造源(tmp_path)
    _跑(["app", "--名字", "演示", "--版本", "1.0.0", "--入口", "bin/演示.exe",
         "--出", "出"], tmp_path)
    前缀 = tmp_path / "前缀"
    解 = tmp_path / "解"
    解.mkdir()
    # ⚠️ 不用 `extractall`（3.14 起会拒收没有 filter 的调用，3.10 上又没有 filter
    # 参数）—— 自己逐个写，两边都能跑。
    with tarfile.open(tmp_path / "出" / "演示-1.0.0-linux-x64.tar.gz") as t:
        for m in t.getmembers():
            if not m.isfile():
                continue
            目标 = 解 / m.name
            目标.parent.mkdir(parents=True, exist_ok=True)
            目标.write_bytes(t.extractfile(m).read())
    包目录 = 解 / "演示-1.0.0-linux-x64"
    assert (包目录 / "install.sh").is_file()

    r = subprocess.run(["sh", "install.sh", "--前缀", str(前缀)], cwd=str(包目录),
                       capture_output=True, timeout=120)
    出 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode == 0, 出[:500]
    assert (前缀 / "opt" / "演示" / "bin" / "演示.exe").read_bytes() == b"MZ fake"
    入口 = 前缀 / "bin" / "演示.exe"
    assert 入口.exists(), f"没有可在 PATH 里敲的入口：{list((前缀 / 'bin').iterdir())}"

    r = subprocess.run(["sh", "install.sh", "--前缀", str(前缀), "--卸载"], cwd=str(包目录),
                       capture_output=True, timeout=120)
    assert r.returncode == 0, (r.stdout + r.stderr).decode("utf-8", "replace")[:400]
    assert not (前缀 / "opt" / "演示").exists(), "卸载后程序目录还在"
    assert not 入口.exists(), "卸载后 bin 里的入口还在"


# ---------------------------------------------------------------------------
# 二、两套命令行：同名同结构同文本
# ---------------------------------------------------------------------------

def _文本产物(目录):
    出 = {}
    with tarfile.open(目录 / "演示-1.0.0-linux-x64.tar.gz") as t:
        for m in t.getmembers():
            if m.name.endswith("install.sh"):
                出["install.sh"] = t.extractfile(m).read()
    with tarfile.open(目录 / "演示-1.0.0-macos-arm64.tar.gz") as t:
        for m in t.getmembers():
            if m.name.endswith("Info.plist"):
                出["Info.plist"] = t.extractfile(m).read()
    return 出


@pytest.mark.skipif(not rust_exe_path().is_file(), reason="没编 jishi-rs")
def test_Rust与Python产物同名且文本逐字相同(tmp_path):
    参数 = ["app", "--名字", "演示", "--版本", "1.0.0", "--入口", "bin/演示.exe"]
    _造源(tmp_path)
    _跑(参数 + ["--出", "出_py"], tmp_path)
    _跑(参数 + ["--出", "出_rs"], tmp_path, exe=rust_exe_path())

    py, rs = tmp_path / "出_py", tmp_path / "出_rs"
    assert _产物(py) == _产物(rs), f"两边的产物不一样：{_产物(py)} vs {_产物(rs)}"
    a, b = _文本产物(py), _文本产物(rs)
    assert a.keys() == b.keys() == {"install.sh", "Info.plist"}
    for k in a:
        assert a[k] == b[k], f"{k} 两边不一致（{len(a[k])} vs {len(b[k])} 字节）"


@pytest.mark.skipif(not rust_exe_path().is_file(), reason="没编 jishi-rs")
def test_Rust与Python的清单逐字相同(tmp_path):
    """包里那份清单是契约 ⇒ 两个前端算出来的包必须通用。"""
    参数 = ["app", "--名字", "演示", "--版本", "1.0.0", "--入口", "bin/演示.exe"]
    _造源(tmp_path)
    _跑(参数 + ["--出", "出_py"], tmp_path)
    _跑(参数 + ["--出", "出_rs"], tmp_path, exe=rust_exe_path())
    a = zipfile.ZipFile(tmp_path / "出_py" / "演示-1.0.0.zip").read("清单.json")
    b = zipfile.ZipFile(tmp_path / "出_rs" / "演示-1.0.0.zip").read("清单.json")
    assert a == b, "两套命令行算出来的 清单.json 不一样"


# ---------------------------------------------------------------------------
# 三、可复现
# ---------------------------------------------------------------------------

def _摘要(目录):
    出 = {}
    for dp, _, fs in os.walk(目录):
        for f in fs:
            p = os.path.join(dp, f)
            出[os.path.relpath(p, 目录)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return 出


def test_两次打包字节完全相同(tmp_path):
    参数 = ["app", "--名字", "演示", "--版本", "1.0.0", "--入口", "bin/演示.exe",
            "--出", "出"]
    _造源(tmp_path)
    _跑(参数, tmp_path)
    a = _摘要(tmp_path / "出")
    shutil.rmtree(tmp_path / "出")
    _跑(参数, tmp_path)
    b = _摘要(tmp_path / "出")
    差 = [k for k in a if a[k] != b.get(k)]
    assert not 差, f"两次打包不一样的产物：{差}"


# ---------------------------------------------------------------------------
# 四、配置与边界
# ---------------------------------------------------------------------------

def test_打包json里的名字版本入口生效(tmp_path):
    源 = _造源(tmp_path)
    (源 / "打包.json").write_text(json.dumps(
        {"名字": "记账", "版本": "2.0.0", "入口": "bin/演示.exe"}, ensure_ascii=False),
        encoding="utf-8")
    _跑(["app", "--出", "出"], tmp_path)
    名们 = _产物(tmp_path / "出")
    assert "记账-2.0.0.zip" in 名们, 名们
    assert "记账.app" in 名们, 名们


def test_打包json不进包(tmp_path):
    """配置是打包的**输入**，不是程序的一部分。"""
    源 = _造源(tmp_path)
    (源 / "打包.json").write_text('{"名字": "记账"}', encoding="utf-8")
    _跑(["app", "--出", "出"], tmp_path)
    z = zipfile.ZipFile(tmp_path / "出" / "记账-0.0.0.zip")
    assert "打包.json" not in z.namelist(), z.namelist()


def test_出目录不会被打进包(tmp_path):
    """默认 `--出` 就在源目录里的 `_出/` —— 否则会越打越大。"""
    源 = _造源(tmp_path)
    _跑(["app", "--名字", "演示", "--入口", "bin/演示.exe"], tmp_path)
    # 第二次：此时 `app/_出` 已经存在了
    _跑(["app", "--名字", "演示", "--入口", "bin/演示.exe"], tmp_path)
    z = zipfile.ZipFile(源 / "_出" / "演示-0.0.0.zip")
    名们 = z.namelist()
    assert not any(n.startswith("_出/") for n in 名们), f"把自己的产物打进去了：{名们}"


def test_没有入口就出不了应用且要说清楚(tmp_path):
    _造源(tmp_path)
    出 = _跑(["app", "--名字", "演示", "--出", "出"], tmp_path)
    assert "入口" in 出 and "macOS" in 出, 出[:500]
    assert "演示.app" not in _产物(tmp_path / "出")


def test_目标能限定只要哪几样(tmp_path):
    _造源(tmp_path)
    _跑(["app", "--名字", "演示", "--入口", "bin/演示.exe", "--出", "出",
         "--目标", "便携"], tmp_path)
    assert _产物(tmp_path / "出") == ["演示-0.0.0.zip"]


def test_安静模式只打路径(tmp_path):
    _造源(tmp_path)
    出 = _跑(["app", "--名字", "演示", "--入口", "bin/演示.exe", "--出", "出", "--安静"],
             tmp_path)
    行们 = [ln for ln in 出.splitlines() if ln.strip()]
    assert 行们 and all(os.path.isabs(ln) or ln.startswith("出") for ln in 行们), 行们
    assert not any("打包「" in ln for ln in 行们), 出[:300]


def test_不认识的产物类型要报错(tmp_path):
    _造源(tmp_path)
    出 = _跑(["app", "--目标", "安装包,msi"], tmp_path, 期望=2)
    assert "msi" in 出 and "认" in 出, 出[:400]
