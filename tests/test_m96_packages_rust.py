# -*- coding: utf-8 -*-
"""R7.6：包管理搬进 Rust —— 与 `jishi` 的包命令**逐字对拍** + 独立判据。

覆盖 `安装 / 卸载 / 列表 / 发布 / 索引 / 检查` 六个子命令（`packages.py` 597 行
+ `cli.py` 的 `_run_package_cmd` / `_cmd_*`）。

## 判据分两半（本项目惯例）

* **对拍**：两侧在**各自独立的 cwd**（`py/` 与 `rs/`）里跑同一条命令，比
  stdout / stderr / 退出码**逐字节**。⚠️ 独立 cwd 是必须的：包管理会往
  `.jishi/packages/` 写东西，共用一个目录会让「第二次跑」看到第一次的状态。
* **独立判据**：写死的期望，不 import oracle.jishi、不 spawn Python。

## 🔴 四处**刻意保留、如实登记**的差异

1. **打包出来的 zip 字节不同**：Python 用 `ZIP_DEFLATED`（压缩），宿主用自己那份
   **stored** 写出器（与标准库 `压缩.打包` 同一条已知差异）。所以 `发布` /
   `索引 --checksum` 的 **zip 体积**与 **sha256** 两边对不上 —— 测试里**归一化**
   这两处再比。**内容完全一样**，`stored` 是完全合规的 zip（有独立判据验
   Python 的 `zipfile` 读得出来）。
2. **缺必填参数时的文案**：Python 走 argparse，打的是
   `usage: jishi 安装 [-h] …` + `error: the following arguments are required`；
   宿主打的是一行中文用法。argparse 的换行**跟终端宽度有关**（本来就不稳定），
   照抄没意义 —— 只对齐**退出码 2**。
3. **网络错误的具体文案**：Python 是 `URLError: <urlopen error [WinError …]>`，
   宿主是「发请求失败：连不上服务器」。外层结构（`所有索引源都读不到…` +
   可操作建议）逐字一致。
4. **JSON 对象的键顺序**：宿主的 JSON 解析器把对象装进 `BTreeMap`（**键序丢了**），
   Python 的 `json.loads` 保序。对顶层与「包」没有影响（`make_index` 本来就排序），
   但**多条目**的 `依赖` 子对象会按字典序输出。`test_已知差异_多依赖的键序` 把这件
   事**钉成可复核的**（免得下次有人以为它是新 bug）。依赖是「名字 → 约束」的映射，
   **顺序无语义**，安装/解析都按键取值。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _exe() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


EXE = _exe()
need_rust = pytest.mark.skipif(
    not EXE.exists(),
    reason="Rust 宿主未构建（cd rust && cargo build --release）"
           "—— 与其假装通过，不如明确跳过")


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    env["PYTHONPATH"] = str(ROOT)
    return env


def _run(cmd, cwd):
    r = subprocess.run(cmd, input=b"", capture_output=True, env=_env(),
                       cwd=str(cwd), timeout=120)
    return r.stdout, r.stderr, r.returncode


def _py(args, cwd):
    return _run([sys.executable, "-m", "oracle.jishi.cli"] + list(args), cwd)


def _rs(args, cwd):
    return _run([str(EXE)] + list(args), cwd)


class Side:
    """一侧的独立工作目录（`.jishi/packages` 状态互不污染）。"""

    def __init__(self, base: Path, name: str):
        self.dir = base / name
        if self.dir.exists():
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True)


@pytest.fixture
def both(tmp_path):
    return Side(tmp_path, "py"), Side(tmp_path, "rs")


def _both(args, sides, normalize=None):
    """两侧跑同一条命令并逐字节比；返回 `(输出, 报错, 退码)`。"""
    po, pe, pc = _py(args, sides[0].dir)
    ro, re_, rc = _rs(args, sides[1].dir)
    if normalize:
        po, ro = normalize(po), normalize(ro)
        pe, re_ = normalize(pe), normalize(re_)
    assert (po, pe, pc) == (ro, re_, rc), (
        f"命令 {args!r} 对拍不一致\n"
        f"  py stdout={po!r}\n  rs stdout={ro!r}\n"
        f"  py stderr={pe!r}\n  rs stderr={re_!r}\n"
        f"  退码 py={pc} rs={rc}")
    return po, pe, pc


# ---------------------------------------------------------------------------
# 夹具：一个包
# ---------------------------------------------------------------------------

PKG_JSH = "函数 打招呼(名)：\n    返回 \"你好 \" + 名\n"


def make_pkg(parent: Path, name="基础库", version="1.2.0",
             deps=None, desc="常用工具", entry="主") -> Path:
    d = parent / name
    d.mkdir(parents=True, exist_ok=True)
    meta = {"名字": name, "版本": version}
    if deps:
        meta["依赖"] = deps
    if desc:
        meta["描述"] = desc
    if entry:
        meta["入口"] = entry
    (d / "包.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    (d / "主.jsh").write_text(PKG_JSH, encoding="utf-8")
    return d


def make_index(path: Path, packages: dict):
    path.write_text(json.dumps(
        {"格式": "jishi-package-index", "版本": 1, "包": packages},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# sha256 十六进制串 / zip 体积 —— 两侧**故意不同**（stored vs deflate），归一化再比。
# ⚠️ 字节正则不能写字面非 ASCII（本项目踩过的坑）→ 用 `.encode("utf-8")`。
_SHA = re.compile(rb"sha256:[0-9a-f]{64}")
_KB = re.compile("（\\d+\\.\\d KB）".encode("utf-8"))


def norm(t: bytes) -> bytes:
    t = _SHA.sub(b"sha256:XXX", t)
    return _KB.sub("（体积）".encode("utf-8"), t)


# ---------------------------------------------------------------------------
# 一、对拍：六个子命令
# ---------------------------------------------------------------------------

@need_rust
def test_对拍_列表空(both, tmp_path):
    _both(["列表"], both)
    _both(["检查"], both)


@need_rust
def test_对拍_本地目录安装_列表_卸载(both, tmp_path):
    pkg = make_pkg(tmp_path / "src")
    out, _, _ = _both(["安装", "基础库", "--local-dir", str(pkg)], both)
    assert out.decode("utf-8").startswith("已安装包「基础库」（来自目录 ")
    # 列表能看到
    po, _, _ = _both(["列表"], both)
    assert "基础库 1.2.0 — 常用工具" in po.decode("utf-8")
    # 重复安装报「已安装」；--force 覆盖
    _, pe, pc = _both(["安装", "基础库", "--local-dir", str(pkg)], both)
    assert pc == 1 and "已安装" in pe.decode("utf-8")
    _both(["安装", "基础库", "--local-dir", str(pkg), "--force"], both)
    # 卸载 + 再卸载
    _both(["卸载", "基础库"], both)
    _, pe, pc = _both(["卸载", "基础库"], both)
    assert pc == 1 and "未安装" in pe.decode("utf-8")


@need_rust
def test_对拍_找不到包(both, tmp_path):
    _, pe, pc = _both(["安装", "基础库", "--no-index"], both)
    assert pc == 1 and "找不到包" in pe.decode("utf-8")


@need_rust
def test_对拍_发布(both, tmp_path):
    pkg = make_pkg(tmp_path / "src")
    out, err, code = _both(
        ["发布", str(pkg), "--out", str(tmp_path / "dist"),
         "--base-url", "https://example.com/下载"], both, normalize=norm)
    assert code == 0
    text = out.decode("utf-8")
    # 索引条目的**排版与顺序**逐字（校验和那行归一化了）
    assert '"基础库": {\n    "版本": "1.2.0",' in text, text
    # 中文路径要百分号编码
    assert "https://example.com/%E4%B8%8B%E8%BD%BD/" in text, text
    assert "校验和" in text


@need_rust
def test_对拍_发布并合并索引(both, tmp_path):
    pkg = make_pkg(tmp_path / "src")
    idx = tmp_path / "索引.json"
    _both(["发布", str(pkg), "--out", str(tmp_path / "d1"), "--index", str(idx)],
          both, normalize=norm)
    # 再发一次（合并进已有索引）
    out, _, _ = _both(["发布", str(pkg), "--out", str(tmp_path / "d2"),
                       "--index", str(idx)], both, normalize=norm)
    assert "已更新索引" in out.decode("utf-8")
    got = json.loads(idx.read_text(encoding="utf-8"))
    assert got["格式"] == "jishi-package-index" and got["版本"] == 1
    assert got["包"]["基础库"]["版本"] == "1.2.0"


@need_rust
def test_对拍_索引(both, tmp_path):
    root = tmp_path / "pkgs"
    make_pkg(root, "甲", "1.0.0")
    make_pkg(root, "乙", "2.1.0", desc="")
    _both(["索引", str(root)], both)


@need_rust
def test_对拍_索引带校验和(both, tmp_path):
    root = tmp_path / "pkgs"
    make_pkg(root, "甲", "1.0.0")
    _both(["索引", str(root), "--checksum"], both, normalize=norm)


@need_rust
def test_对拍_安装自索引文件(both, tmp_path):
    """整条安装链：本地索引 → 下载 zip → 校验和 → 解压安装。"""
    dl = tmp_path / "download"
    pkg = make_pkg(tmp_path / "src")
    # 用 Python 侧生成 zip + 索引（zip 是 deflate 的；正好考宿主的解压）
    dl.mkdir()
    base = dl.as_uri()
    _py(["发布", str(pkg), "--out", str(dl), "--base-url", base,
         "--index", str(dl / "索引.json")], tmp_path)
    assert (dl / "基础库-1.2.0.zip").is_file()

    out, _, code = _both(["安装", "基础库", "--index", str(dl / "索引.json")], both)
    assert code == 0 and "已安装包「基础库」（版本 1.2.0）" in out.decode("utf-8")
    # 装出来的文件两边一样
    for s in both:
        assert (s.dir / ".jishi/packages/基础库/包.json").is_file()
        assert (s.dir / ".jishi/packages/基础库/主.jsh").read_text(
            encoding="utf-8") == PKG_JSH


@need_rust
def test_对拍_安装自file网址索引(both, tmp_path):
    """`file://` 索引源（本地镜像）—— 不联网也能走完整条链。"""
    dl = tmp_path / "download"
    dl.mkdir()
    pkg = make_pkg(tmp_path / "src")
    _py(["发布", str(pkg), "--out", str(dl), "--base-url", dl.as_uri(),
         "--index", str(dl / "索引.json")], tmp_path)
    _both(["安装", "基础库", "--index", (dl / "索引.json").as_uri()], both)


@need_rust
def test_对拍_安装自本地zip(both, tmp_path):
    dl = tmp_path / "download"
    dl.mkdir()
    pkg = make_pkg(tmp_path / "src")
    _py(["发布", str(pkg), "--out", str(dl)], tmp_path)
    z = dl / "基础库-1.2.0.zip"
    _both(["安装", "基础库", "--local-zip", str(z)], both)


@need_rust
def test_对拍_依赖冲突(both, tmp_path):
    """钻石依赖：`甲` 要 `基础库>=2.0.0`、`乙` 要 `<2.0.0`，装的是 1.5.0。"""
    for s in both:
        (s.dir / ".jishi/packages").mkdir(parents=True)
    for name, deps, ver in (("甲", {"基础库": ">=2.0.0"}, "1.0.0"),
                            ("乙", {"基础库": "<2.0.0"}, "1.0.0"),
                            ("基础库", None, "1.5.0")):
        d = make_pkg(tmp_path / "src", name, ver, deps=deps, desc="", entry="")
        for s in both:
            shutil.copytree(d, s.dir / ".jishi/packages" / name)
    out, _, code = _both(["检查"], both)
    assert code == 1, code
    text = out.decode("utf-8")
    assert "包「基础库」的版本要求互相打架（当前装的是 1.5.0）：" in text
    assert "→ 基础库 要求 >=2.0.0" in text
    assert "→ 基础库 要求 <2.0.0" in text
    assert "共发现 1 处依赖冲突" in text


@need_rust
def test_对拍_错误路径(both, tmp_path):
    # 不是包
    bad = tmp_path / "不是包"
    bad.mkdir()
    _, pe, pc = _both(["发布", str(bad)], both)
    assert pc == 1 and "里没有 包.json" in pe.decode("utf-8")
    # 索引里没有这个包 / 有下载地址但下载不了
    idx = make_index(tmp_path / "i.json", {"别的包": {"版本": "1.0.0"}})
    _, pe, pc = _both(["安装", "想要的包", "--index", str(idx)], both)
    assert pc == 1 and "索引里没有包「想要的包」" in pe.decode("utf-8")
    # 校验和对不上
    dl = tmp_path / "dl2"
    dl.mkdir()
    pkg = make_pkg(tmp_path / "src2")
    _py(["发布", str(pkg), "--out", str(dl), "--base-url", dl.as_uri(),
         "--index", str(dl / "索引.json")], tmp_path)
    got = json.loads((dl / "索引.json").read_text(encoding="utf-8"))
    got["包"]["基础库"]["校验和"] = "sha256:" + "0" * 64
    make_index(tmp_path / "坏.json", got["包"])
    _, pe, pc = _both(["安装", "基础库", "--index", str(tmp_path / "坏.json")], both)
    assert pc == 1 and "校验和对不上" in pe.decode("utf-8")
    # 无法识别的字段
    bad2 = make_pkg(tmp_path / "src3", "坏字段", "1.0.0")
    meta = json.loads((bad2 / "包.json").read_text(encoding="utf-8"))
    meta["名字拼错"] = "x"
    (bad2 / "包.json").write_text(json.dumps(meta, ensure_ascii=False),
                                  encoding="utf-8")
    _, pe, pc = _both(["发布", str(bad2)], both)
    assert pc == 1 and "无法识别的字段" in pe.decode("utf-8")


@need_rust
def test_对拍_缺参数退码2(both, tmp_path):
    """argparse 的 usage 文案是**已登记差异**，这里只钉退出码。"""
    for args in (["安装"], ["卸载"], ["发布"], ["索引"]):
        _, _, pc = _py(args, both[0].dir)
        _, _, rc = _rs(args, both[1].dir)
        assert pc == rc == 2, (args, pc, rc)


# ---------------------------------------------------------------------------
# 二、不依赖 Python 的独立判据
# ---------------------------------------------------------------------------

@need_rust
def test_独立_安装_列表_卸载_写死期望(tmp_path):
    """整条本地链的 stdout **逐字写死**（不 import oracle.jishi、不 spawn Python）。"""
    work = tmp_path / "w"
    work.mkdir()
    pkg = make_pkg(tmp_path / "src")

    out, err, code = _rs(["列表"], work)
    assert (out.decode("utf-8"), err, code) == ("（尚未安装任何本地包）\n", b"", 0)

    out, err, code = _rs(["安装", "基础库", "--local-dir", str(pkg)], work)
    assert code == 0 and err == b""
    assert out.decode("utf-8") == f"已安装包「基础库」（来自目录 {pkg}）\n"

    out, _, _ = _rs(["列表"], work)
    assert out.decode("utf-8") == "基础库 1.2.0 — 常用工具\n"

    out, err, code = _rs(["卸载", "基础库"], work)
    assert (out.decode("utf-8"), err, code) == ("已卸载包「基础库」\n", b"", 0)
    out, err, code = _rs(["卸载", "基础库"], work)
    assert out == b"" and code == 1
    assert err.decode("utf-8") == "包「基础库」未安装\n"


@need_rust
def test_独立_发布的zip是合规的(tmp_path):
    """宿主打的是 **stored** zip —— 拿 Python 的 `zipfile` 当裁判验它合规。

    这是「字节不同但内容一样」那条登记的独立判据：字节没法比，**内容**能比。
    """
    pkg = make_pkg(tmp_path / "src")
    out = tmp_path / "dist"
    _, err, code = _rs(["发布", str(pkg), "--out", str(out)], tmp_path)
    assert code == 0, err
    z = out / "基础库-1.2.0.zip"
    # ⚠️ 比**磁盘上的原始字节**（`read_text` 会把 CRLF 归一成 LF，那样比的是
    #    「归一后的内容」，盖不住「打包时字节有没有被改坏」）。
    with zipfile.ZipFile(z) as zf:
        names = zf.namelist()
        assert "基础库/包.json" in names, names
        assert "基础库/主.jsh" in names, names
        assert zf.read("基础库/主.jsh") == (pkg / "主.jsh").read_bytes()
        assert zf.read("基础库/包.json") == (pkg / "包.json").read_bytes()


@need_rust
def test_独立_索引JSON逐字写死(tmp_path):
    root = tmp_path / "pkgs"
    make_pkg(root, "甲", "1.0.0", desc="", entry="")
    out, err, code = _rs(["索引", str(root)], tmp_path)
    assert code == 0, err
    assert out.decode("utf-8") == (
        "{\n"
        '  "格式": "jishi-package-index",\n'
        '  "版本": 1,\n'
        '  "包": {\n'
        '    "甲": {\n'
        '      "版本": "1.0.0"\n'
        "    }\n"
        "  }\n"
        "}\n\n"            # ⚠️ print(text) 会在已经以 \n 结尾的文本后再补一个
    )


@need_rust
def test_独立_安装链不依赖Python(tmp_path):
    """`file://` 索引 → 下载 → 校验和 → 装好 —— 全程只跑宿主二进制。

    ⚠️ 这里的 zip 由**宿主自己**打（stored）—— 顺便证明「自己打的包自己装得上」。
    """
    work = tmp_path / "w"
    work.mkdir()
    dl = tmp_path / "dl"
    dl.mkdir()
    pkg = make_pkg(tmp_path / "src")
    _, err, code = _rs(["发布", str(pkg), "--out", str(dl),
                        "--base-url", dl.as_uri(),
                        "--index", str(dl / "索引.json")], tmp_path)
    assert code == 0, err
    _, err, code = _rs(["安装", "基础库", "--index", (dl / "索引.json").as_uri()], work)
    assert code == 0, err
    assert (work / ".jishi/packages/基础库/包.json").is_file()
    out, _, _ = _rs(["列表"], work)
    assert out.decode("utf-8") == "基础库 1.2.0 — 常用工具\n"


@need_rust
def test_独立_检查命令与源码检查已分开(tmp_path):
    """`检查` 是**包依赖冲突**、`源码静态检查` 才是源码 lint（与 Python 对齐）。

    ⚠️ R7.1-c 当初把源码 lint 挂在 `检查` 上，与 Python 的 `检查`（包冲突）
    撞名；R7.6 把 `检查` 让回给包管理 —— 这条判据把这个分工钉死。
    """
    work = tmp_path / "w"
    work.mkdir()
    out, _, code = _rs(["检查"], work)
    assert code == 0 and out.decode("utf-8") == "（尚未安装任何本地包）\n"
    # 源码检查：写一个遮蔽内建的文件，两边（`源码静态检查` 与 `静态检查`）都认
    src = work / "x.jsh"
    src.write_text("令 类型 = 1\n打印(类型)\n", encoding="utf-8")
    for name in ("源码静态检查", "静态检查"):
        out, _, code = _rs([name, str(src)], work)
        assert code == 1, (name, code)
        assert "name.shadow" in out.decode("utf-8"), name


@need_rust
def test_独立_清空PATH也能跑(tmp_path):
    """Rust 二进制的意义就是**不依赖 Python** —— 清掉 PATH 照样跑得动。"""
    work = tmp_path / "w"
    work.mkdir()
    pkg = make_pkg(tmp_path / "src")
    env = _env()
    env["PATH"] = ""
    r = subprocess.run([str(EXE), "安装", "基础库", "--local-dir", str(pkg)],
                       input=b"", capture_output=True, env=env,
                       cwd=str(work), timeout=60)
    assert r.returncode == 0, (r.returncode, r.stderr[:300])
    r = subprocess.run([str(EXE), "列表"], input=b"", capture_output=True,
                       env=env, cwd=str(work), timeout=60)
    assert r.stdout.decode("utf-8") == "基础库 1.2.0 — 常用工具\n"


@need_rust
def test_已知差异_多依赖的键序(tmp_path):
    """把「多条目 `依赖` 的键序」这条**已登记的差异**钉成可复核的。

    宿主按**字典序**输出（JSON 解析器是 `BTreeMap`），Python 按**原样**输出。
    依赖是「名字 → 约束」的映射，顺序无语义 —— 但差异本身要看得见，
    不然下次有人会当成新 bug 去查。修它的代价是让核心 JSON 类型保序
    （热路径上的类型），不划算，所以**登记而不修**。
    """
    pkg = make_pkg(tmp_path / "src", "丙", "1.0.0",
                   deps={"乙乙": ">=1.0.0", "甲甲": ">=1.0.0", "丙丙": ">=1.0.0"},
                   desc="", entry="")
    out, _, code = _rs(["索引", str(tmp_path / "src")], tmp_path)
    assert code == 0
    text = out.decode("utf-8")
    # 宿主是字典序（丙丙 < 乙乙 < 甲甲 按 UTF-8 码点）
    assert text.index("丙丙") < text.index("乙乙") < text.index("甲甲"), text
    # 三个依赖一个不少（只是顺序不同）
    for d in ("甲甲", "乙乙", "丙丙"):
        assert f'"{d}": ">=1.0.0"' in text, text
    _ = pkg

