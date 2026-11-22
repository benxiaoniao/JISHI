# -*- coding: utf-8 -*-
"""M171 · `打包`：把「一个程序」打成**一个带清单的 zip**，并能验回来。

## 这一组判据在防什么

打包线真正怕的不是「压不进去」，而是**包里的东西和清单说的不一样，却没人发现**。
所以这里的重心压在两条上：

1. **可复现**：同一个目录两次 `生成` ⇒ 字节完全相同（沿用 M109 / M168 那条规矩）。
2. **校验真的会红**：判据不能只验「好包通过」，必须**造三个坏包**
   （改内容 / 删文件 / 塞多余文件）确认它每次都红 —— 只比名字不比内容，
   等于没验（这正是 `test_m169_ai_card` 那条「故意喂坏函数」同一个理由）。

另外 `清单.json` 的**文本格式是三宿主的契约**（安装器解完只认它），
所以有一条专门比「Python / Rust / Node 算出来的清单字节相同」。
"""

import hashlib
import sys
import zipfile

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                            # noqa: E402


def _造树(tmp_path):
    """一棵小目录树：两层 + 一个中文名（顺带验 UTF-8 条目名）。"""
    源 = tmp_path / "发布"
    (源 / "bin").mkdir(parents=True)
    (源 / "share" / "子").mkdir(parents=True)
    (源 / "bin" / "app.exe").write_bytes(b"A")
    (源 / "share" / "b.txt").write_bytes(b"BB")
    (源 / "share" / "子" / "说明.txt").write_bytes("中文内容".encode("utf-8"))
    return 源


def _跑(源, tmp_path, exe=None):
    import subprocess

    (tmp_path / "程序.jsh").write_text(源, encoding="utf-8", newline="\n")
    命令 = ([str(exe)] if exe else [sys.executable, "-m", "oracle.jishi.cli"])
    r = subprocess.run(命令 + ["程序.jsh"], cwd=str(tmp_path),
                       capture_output=True, timeout=240, input=b"")
    return (r.stdout + r.stderr).decode("utf-8", "replace"), r.returncode


def _跑Node(源, tmp_path):
    import subprocess

    from oracle.jishi import serialize
    from oracle.jishi.compiler import compile_source

    p = tmp_path / "程序.json"
    p.write_text(serialize.dumps(compile_source(源, "程序.jsh")), encoding="utf-8")
    r = subprocess.run(["node", str(ROOT / "oracle" / "node" / "index.js"), str(p)],
                       cwd=str(tmp_path), capture_output=True, timeout=240)
    return (r.stdout + r.stderr).decode("utf-8", "replace"), r.returncode


选项 = '{"名字": "演示", "版本": "1.2.3", "平台": "windows-x64", "入口": "bin/app.exe"}'

程序 = f"""\
导入 打包
令 m = 打包.清单("发布", {选项})
打印(m["名字"], m["版本"], m["平台"], m["入口"])
打印(m["文件数"], m["字节数"])
遍历 项 在 m["文件们"]：
    打印(项["路径"], 项["大小"])
令 包 = 打包.生成("发布", "出", {选项})
令 r = 打包.校验(包)
打印("通过", r["通过"], 长度(r["问题们"]))
"""


# ---------------------------------------------------------------------------
# 一、清单
# ---------------------------------------------------------------------------

def test_清单内容对得上(tmp_path):
    _造树(tmp_path)
    出, 码 = _跑(程序, tmp_path)
    assert 码 == 0, 出[:500]
    行 = 出.splitlines()
    assert 行[0] == "演示 1.2.3 windows-x64 bin/app.exe", 行[:2]
    # 1 + 2 + 「中文内容」12 字节 = 15
    assert 行[1] == "3 15", f"文件数/字节数不对：{行[1]}"
    # 排序按 **UTF-8 字节序**：bin/ 在 share/ 前，`b.txt` 在 `子/说明.txt` 前
    assert 行[2:5] == ["bin/app.exe 1", "share/b.txt 2", "share/子/说明.txt 12"], 行[2:5]
    assert 行[5] == "通过 真 0", 行[5]


def test_清单里的sha256是内容摘要(tmp_path):
    _造树(tmp_path)
    _跑(程序, tmp_path)
    z = zipfile.ZipFile(tmp_path / "出" / "演示-1.2.3-windows-x64.zip")
    清单 = __import__("json").loads(z.read("清单.json").decode("utf-8"))
    for 项 in 清单["文件们"]:
        assert 项["sha256"] == hashlib.sha256(z.read(项["路径"])).hexdigest(), 项


# ---------------------------------------------------------------------------
# 二、可复现
# ---------------------------------------------------------------------------

def test_生成两次字节相同(tmp_path):
    源 = _造树(tmp_path)
    _跑(程序, tmp_path)
    包 = tmp_path / "出" / "演示-1.2.3-windows-x64.zip"
    a = 包.read_bytes()
    包.unlink()
    _跑(程序, tmp_path)
    b = 包.read_bytes()
    assert a == b, (
        f"两次生成的字节不同（{len(a)} vs {len(b)}）—— 时间/顺序/权限里有一样还跟着磁盘走")
    assert (源 / "bin" / "app.exe").read_bytes() == b"A"   # 源没被动过


# ---------------------------------------------------------------------------
# 三、三宿主：清单文本必须是同一份
# ---------------------------------------------------------------------------

def _清单字节(tmp_path):
    z = zipfile.ZipFile(tmp_path / "出" / "演示-1.2.3-windows-x64.zip")
    return z.read("清单.json")


def test_清单json三宿主逐字一致(tmp_path):
    """`清单.json` 是安装器唯一认的东西 ⇒ 三个宿主算出来必须**同一份字节**。

    ⚠️ 这条抓的是「Python 用 `json.dumps`、Node 用 `JSON.stringify`」那类做法：
    键序、缩进、转义各有各的规矩，最后包能装、但验不过。
    """
    _造树(tmp_path)
    _跑(程序, tmp_path)
    py = _清单字节(tmp_path)

    出n, 码n = _跑Node(程序, tmp_path)
    assert 码n == 0, 出n[:400]
    nd = _清单字节(tmp_path)
    assert nd == py, "Node 侧的清单.json 与 Python 不一致"

    if rust_exe_path().is_file():
        出r, 码r = _跑(程序, tmp_path, exe=rust_exe_path())
        assert 码r == 0, 出r[:400]
        rs = _清单字节(tmp_path)
        assert rs == py, "Rust 侧的清单.json 与 Python 不一致"

    assert py.startswith(b'{\n  "'), py[:40]


# ---------------------------------------------------------------------------
# 四、校验必须真的会红（造三个坏包）
# ---------------------------------------------------------------------------

def _改包(原包, 新包, 改法):
    """把原包重写一份，交给 `改法(条目们)` 动手（条目们是 `[名字, 内容]`）。"""
    条目们 = []
    with zipfile.ZipFile(原包) as z:
        for n in z.namelist():
            条目们.append([n, z.read(n)])
    条目们 = 改法(条目们)
    with zipfile.ZipFile(新包, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in 条目们:
            z.writestr(n, d)
    return 新包


校验程序 = """\
导入 打包
令 r = 打包.校验("坏.zip")
打印(r["通过"])
遍历 问 在 r["问题们"]：
    打印(问)
"""


def _校验(tmp_path, 原包, 改法):
    _改包(原包, tmp_path / "坏.zip", 改法)
    return _跑(校验程序, tmp_path)


def test_内容被改过要报内容不对(tmp_path):
    _造树(tmp_path)
    _跑(程序, tmp_path)
    原包 = tmp_path / "出" / "演示-1.2.3-windows-x64.zip"

    def 改(条目们):
        for it in 条目们:
            if it[0] == "share/b.txt":
                # ⚠️ **长度必须一样**：变了长度会先被「大小不对」抓住，
                # 那样就验不到 sha256 那条路（两条路各有各的判据）。
                it[1] = b"BC"
        return 条目们

    出, 码 = _校验(tmp_path, 原包, 改)
    assert 码 == 0, 出[:400]
    assert 出.splitlines()[0] == "假", 出[:400]
    assert any("内容不对：share/b.txt" in x for x in 出.splitlines()), 出[:500]


def test_少一个文件要报缺文件(tmp_path):
    _造树(tmp_path)
    _跑(程序, tmp_path)
    原包 = tmp_path / "出" / "演示-1.2.3-windows-x64.zip"

    出, 码 = _校验(tmp_path, 原包, lambda 条目们: [x for x in 条目们 if x[0] != "bin/app.exe"])
    assert 出.splitlines()[0] == "假", 出[:400]
    assert "缺文件：bin/app.exe" in 出, 出[:500]


def test_多塞一个文件要报清单外多出(tmp_path):
    _造树(tmp_path)
    _跑(程序, tmp_path)
    原包 = tmp_path / "出" / "演示-1.2.3-windows-x64.zip"

    出, 码 = _校验(tmp_path, 原包, lambda 条目们: 条目们 + [["bin/多出来的.dll", b"x"]])
    assert 出.splitlines()[0] == "假", 出[:400]
    assert "清单外多出：bin/多出来的.dll" in 出, 出[:500]


def test_没有清单的包要明确说不是打包生成的(tmp_path):
    (tmp_path / "坏.zip").write_bytes(b"")
    with zipfile.ZipFile(tmp_path / "坏.zip", "w") as z:
        z.writestr("a.txt", b"a")
    出, 码 = _跑(校验程序, tmp_path)
    assert 码 == 0, 出[:300]
    assert 出.splitlines()[0] == "假"
    assert "没有「清单.json」" in 出, 出[:400]


# ---------------------------------------------------------------------------
# 五、把话说清楚
# ---------------------------------------------------------------------------

def test_名字为空要说清楚为什么不能空(tmp_path):
    _造树(tmp_path)
    出, 码 = _跑('导入 打包\n打包.生成("发布", "出", {"版本": "1.0"})\n', tmp_path)
    assert 码 == 1, 出[:300]
    assert "名字" in 出 and "不能空" in 出, 出[:400]


def test_名字里有斜杠要报错(tmp_path):
    _造树(tmp_path)
    出, 码 = _跑('导入 打包\n打包.生成("发布", "出", {"名字": "a/b"})\n', tmp_path)
    assert 码 == 1, 出[:300]
    assert "文件名" in 出, 出[:400]


def test_传文件列表不给基础目录要报错(tmp_path):
    _造树(tmp_path)
    出, 码 = _跑('导入 打包\n打包.清单(["发布/bin/app.exe"], {"名字": "演示"})\n', tmp_path)
    assert 码 == 1, 出[:300]
    assert "基础" in 出 and "条目名" in 出, 出[:400]


def test_传文件列表给了基础目录就能算(tmp_path):
    _造树(tmp_path)
    出, 码 = _跑('''导入 打包
令 m = 打包.清单(["发布/bin/app.exe"], {"名字": "演示", "基础": "发布"})
打印(m["文件数"], m["文件们"][0]["路径"])
''', tmp_path)
    assert 码 == 0, 出[:400]
    assert 出.splitlines()[0] == "1 bin/app.exe", 出[:400]
