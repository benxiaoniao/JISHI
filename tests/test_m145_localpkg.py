# -*- coding: utf-8 -*-
"""M145 —— 包管理补缺口。

四件事，一件一组判据：

| 缺口 | 判据 |
|---|---|
| **①产品版导入不了包**（P0） | 本地包在**编译期展开**成函数；五引擎逐字一致；依赖注入 / 循环依赖 / 版本约束 / 别名 / 包不存在 |
| **⑤闭包里「已装」的包让整次安装失败** | 装 `数学工具`（依赖 `基础库`，已装）→ 跳过并写 lock，退出码 0 |
| **⑥GitHub 兜底是坏的** | `publish_github._export_packages` 真的产出 `packages/索引.json` + `下载/*.zip`，校验和自洽 |
| **③`包.json` 表达不了「要求的基石版本」** | 加 `基石版本` 字段，装的时候拦下并给 E4010 |

## 为什么 P0 用「编译期展开」而不是「运行时加载」

先试的是运行时加载（照 Python 参考实现那样），在 Rust 宿主上一调用包里的函数就

    panicked: index out of bounds: the len is 1 but the index is 19

因为字节码里函数值只记「本模块代码表的下标」，跨模块调用必然越界 ——
这正是 `jishilib.py` 文档里写的「跨 VM 调用」那个坑。而且 Node / C VM
**没有编译器**，读不了 `.jsh` 源码，运行时加载对它们根本不通。
⇒ 展开成函数：五个引擎跑同一份字节码，问题从根上没有了（见 `test_展开成函数`）。
"""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_m52_jishilib import RUST, _engines              # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 造一个「最小工程」：两个包 + 一个主程序
# ---------------------------------------------------------------------------

def _写(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _造工程(tmp: Path) -> Path:
    """`tmp/.jishi/packages/{甲,乙}` + 主程序；返回工程根。

    `甲` 依赖 `乙`（`包.json` 的「依赖」），并且**不在源码里导入乙** ——
    直接写 `乙.加一(...)`。这考的就是「依赖注入」：展开时要给包体注入
    `乙 = $造模块("乙", …)`，否则那份代码找不到 `乙`。
    """
    base = tmp / ".jishi" / "packages"
    _写(base / "乙" / "包.json",
        json.dumps({"名字": "乙", "版本": "1.2.0", "描述": "被依赖的那个"},
                   ensure_ascii=False, indent=2) + "\n")
    _写(base / "乙" / "乙.jsh",
        "函数 加一(x)：\n    返回 x + 1\n\n\n函数 版本号()：\n    返回 \"1.2.0\"\n")

    _写(base / "甲" / "包.json",
        json.dumps({"名字": "甲", "版本": "1.0.0", "描述": "依赖乙",
                    "依赖": {"乙": ">=1.0.0"}, "入口": "甲.jsh"},
                   ensure_ascii=False, indent=2) + "\n")
    _写(base / "甲" / "甲.jsh",
        "函数 加倍(x)：\n    返回 乙.加一(乙.加一(x))\n\n\n"
        "函数 依赖版本()：\n    返回 乙.版本号()\n")

    _写(tmp / "主程序.jsh",
        "导入 甲 从 本地包\n打印(甲)\n打印(甲.加倍(5))\n打印(甲.依赖版本())\n")
    return tmp


@pytest.fixture()
def 工程(tmp_path, monkeypatch):
    """临时工程 + 把 cwd 切过去（包搜索目录是**相对 cwd** 的）。

    ⚠️ 用 `monkeypatch.chdir` 而不是自己 `os.chdir`：它会在用例结束自动还原，
    否则下一个用例会在一个已经删掉的目录里跑（并行时尤其致命）。
    """
    _造工程(tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _跑源码(src: str, tag: str = "m145"):
    """五引擎；返回 `[(名字, 是否正常, 输出)]`。

    ⚠️ 要接住 `JishiError`：包不存在 / 循环依赖这类**编译期**错误会在
    `_host_run` 里先炸（它得先编译出字节码才交给宿主），不接就会把整条用例
    变成「异常逃逸」而不是「引擎报错」。
    """
    out = []
    for 名, 跑 in _engines(tag):
        try:
            正常, 输出 = 跑(src)
        except Exception as e:                                  # noqa: BLE001
            正常, 输出 = False, f"{getattr(e, 'title', '')} {e}"
        out.append((名, 正常, 输出))
    return out


def _跑文件(路径: Path) -> tuple:
    """**两个宿主的命令行**各跑一次（不经测试助手）—— 返回 (python 输出, rust 输出)。

    为什么要单独一条：错误用例走 `_engines` 时，Node / Rust 那两路是拿
    **Python 编译出来的字节码**跑的，编译期错误根本到不了它们。
    要证明「**Rust 自己的编译器**报的是同一句话」，只能用源码喂命令行。
    """
    import subprocess

    py = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", str(路径)],
                        cwd=str(路径.parent), capture_output=True, text=True,
                        encoding="utf-8")
    if not RUST.exists():
        pytest.skip("没有编出 jishi-rs")
    rs = subprocess.run([str(RUST), str(路径)], cwd=str(路径.parent),
                        capture_output=True, text=True, encoding="utf-8")
    return ((py.stdout or "") + (py.stderr or ""),
            (rs.stdout or "") + (rs.stderr or ""))


def _第一条含(输出: str, 关键词: str) -> str:
    命中 = [行.strip() for 行 in 输出.splitlines() if 关键词 in 行]
    return 命中[0] if 命中 else ""


# ---------------------------------------------------------------------------
# ① 本地包：编译期展开（P0）
# ---------------------------------------------------------------------------

def test_五引擎一致(工程):
    """包能用、依赖被注入、结果五引擎逐字节一致。"""
    源码 = (工程 / "主程序.jsh").read_text(encoding="utf-8")
    基准名, 基准 = None, None
    for 名, 正常, 输出 in _跑源码(源码, "m145a"):
        assert 正常, f"{名} 本该正常退出却报错：{输出[:300]}"
        if 基准 is None:
            基准名, 基准 = 名, 输出
        else:
            assert 输出 == 基准, f"{名} 与 {基准名} 不一致\n{基准!r}\n{输出!r}"
    # 内容也要对：模块身份 + 依赖注入 + 依赖的版本
    assert 基准.splitlines() == ["<模块 甲>", "7", "1.2.0"], 基准


def test_展开成函数_不是运行时加载(工程):
    """结构判据：字节码里要有外壳函数与 `$造模块`，**不能**留成 IMPORT。

    这是钉「架构选择」的那条：谁把方案改回运行时加载（`IMPORT src=2`），
    Rust 宿主一调用包里的函数就会越界 panic —— 这条会先红。
    """
    from oracle.jishi import opcodes as O
    from oracle.jishi.compiler import compile_source

    源码 = (工程 / "主程序.jsh").read_text(encoding="utf-8")
    cmod = compile_source(源码, "<t>")
    names = list(cmod.names)
    assert "__本地包_甲__" in names, names
    assert "__本地包_乙__" in names, names
    assert "$造模块" in names, names

    # 展开之后**不该再有**「从本地包导入」这条指令（src=2）
    从本地包 = [i for code in cmod.codes for i in code.instrs
              if i.op == O.Op.IMPORT and i.b == 2]
    assert not 从本地包, "还有 IMPORT src=2：说明没展开（或又改回运行时加载了）"


def test_分支体里的导入也展开(工程):
    """`如果 真：导入 甲 从 本地包` —— **块体里的导入也要展开**。

    基石库层那套扫描不下探 `branches` 的块体（`统计` 有宿主原生实现兜底，
    所以看不出来）。本地包**没有兜底**：不展开的话 Python 会回落到运行时加载
    （能跑）、宿主只会报错 —— 同一段代码两个结果。
    ⇒ 本地包这一层专门下探到底。这条判据钉的就是它。
    """
    源码 = "如果 真：\n    导入 甲 从 本地包\n    打印(甲.加倍(3))\n"
    基准名, 基准 = None, None
    for 名, 正常, 输出 in _跑源码(源码, "m145f"):
        assert 正常, f"{名} 本该正常退出却报错：{输出[:300]}"
        if 基准 is None:
            基准名, 基准 = 名, 输出
        else:
            assert 输出 == 基准, f"{名} 与 {基准名} 不一致：{基准!r} vs {输出!r}"
    assert 基准.strip() == "5", 基准


def test_别名导入(工程):
    出 = _跑源码("导入 甲 从 本地包 为 工具\n打印(工具.加倍(1))\n", "m145b")
    for 名, 正常, 输出 in 出:
        assert 正常, f"{名}：{输出[:200]}"
        assert 输出.strip() == "3", (名, 输出)


def test_包不存在_两宿主说同一句(工程):
    """报错也要一致：这是「模型能自愈」的前提（核心二）。

    ⚠️ 比的是**两个宿主命令行**的整段输出（含提示行），不是「测试助手接住的那句」——
    后者只能证明 Python 编译器的说法，证明不了 Rust 自己那份。
    """
    f = 工程 / "无此包.jsh"
    _写(f, "导入 无此包 从 本地包\n")
    py, rs = _跑文件(f)
    assert "没有找到本地包「无此包」" in py, py[:400]
    assert "错误 E0302" in py, py[:400]
    assert py == rs, f"两宿主不一致\n--- python ---\n{py}\n--- rust ---\n{rs}"


def test_循环依赖报错(工程):
    base = 工程 / ".jishi" / "packages"
    for 名, 依赖 in (("环甲", "环乙"), ("环乙", "环甲")):
        _写(base / 名 / "包.json",
            json.dumps({"名字": 名, "版本": "1.0.0", "依赖": {依赖: "*"}},
                       ensure_ascii=False, indent=2) + "\n")
        _写(base / 名 / f"{名}.jsh", "函数 甲()：\n    返回 1\n")

    f = 工程 / "循环.jsh"
    _写(f, "导入 环甲 从 本地包\n")
    py, rs = _跑文件(f)
    assert "检测到循环依赖：环甲 → 环乙 → 环甲" in py, py[:400]
    assert py == rs, f"两宿主不一致\n--- python ---\n{py}\n--- rust ---\n{rs}"


def test_依赖版本不满足报错(工程):
    base = 工程 / ".jishi" / "packages"
    _写(base / "丙" / "包.json",
        json.dumps({"名字": "丙", "版本": "1.0.0", "依赖": {"乙": ">=2.0.0"}},
                   ensure_ascii=False, indent=2) + "\n")
    _写(base / "丙" / "丙.jsh", "函数 甲()：\n    返回 1\n")

    f = 工程 / "版本不满足.jsh"
    _写(f, "导入 丙 从 本地包\n")
    py, rs = _跑文件(f)
    句 = _第一条含(py, "依赖「乙」的版本")
    assert 句, py[:400]
    assert ">=2.0.0" in 句 and "1.2.0" in 句, 句
    assert py == rs, f"两宿主不一致\n--- python ---\n{py}\n--- rust ---\n{rs}"


# ---------------------------------------------------------------------------
# ⑤ 闭包里「已装」的包：跳过，不整次失败
# ---------------------------------------------------------------------------

def test_闭包里已装的包要跳过(工程, tmp_path, monkeypatch):
    """`安装 数学工具`（依赖 `基础库`，已装）→ 跳过 + 装成功 + 写 lock。

    以前是直接 `安装失败：包「基础库」已安装`，而它自己刚说过
    「重跑一次会自动跳过已装的」—— 重跑照样失败，自相矛盾。
    """
    from oracle.jishi import cli as CLI
    from oracle.jishi import packages as PKG

    dist = tmp_path / "dist"
    dist.mkdir()
    for 名 in ("基础库", "数学工具"):
        PKG.build_package_zip(str(ROOT / "examples" / "packages" / 名), str(dist))

    def 条目(名: str, 版: str, 依赖=None) -> dict:
        z = dist / f"{名}-{版}.zip"
        e = {"版本": 版,
             "下载地址": "file:///" + str(z).replace("\\", "/"),
             "校验和": "sha256:" + hashlib.sha256(z.read_bytes()).hexdigest(),
             "描述": f"{名} 测试用"}
        if 依赖:
            e["依赖"] = 依赖
        return e

    索引 = 工程 / "index.json"
    _写(索引, json.dumps(
        {"格式": PKG.INDEX_FORMAT, "版本": PKG.INDEX_VERSION,
         "包": {"基础库": 条目("基础库", "1.0.0"),
                "数学工具": 条目("数学工具", "2.0.0", {"基础库": ">=1.0.0"})}},
        ensure_ascii=False, indent=2) + "\n")

    # 第一次：只装基础库
    assert CLI.main(["安装", "基础库", "--index", str(索引)]) == 0
    # 第二次：装数学工具 —— 依赖已装，必须跳过而不是失败
    assert CLI.main(["安装", "数学工具", "--index", str(索引)]) == 0

    lock = PKG.parse_lock((工程 / ".jishi" / "包.lock").read_text(encoding="utf-8"))
    assert set(lock["包"]) == {"基础库", "数学工具"}
    # 两次装完包都在
    assert (工程 / ".jishi" / "packages" / "数学工具" / "包.json").is_file()


# ---------------------------------------------------------------------------
# ⑥ GitHub 兜底：发布时导出 packages/（索引 + zip）
# ---------------------------------------------------------------------------

def test_发布要导出包索引与zip(tmp_path):
    """`publish_github._export_packages` 必须产出**自洽**的 `packages/`。

    以前只推代码，`packages/下载/*.zip` 从来不在快照里 ⇒ 兜底 404，
    自建源一挂就装不上。
    """
    import importlib

    pub = importlib.import_module("tools.publish_github")

    快照 = tmp_path / "snap"
    # 造一个「像快照」的目录：只要 examples/packages 就行（其余它不碰）
    for 名 in ("基础库", "数学工具"):
        源 = ROOT / "examples" / "packages" / 名
        目标 = 快照 / "examples" / "packages" / 名
        目标.mkdir(parents=True)
        for f in 源.iterdir():
            if f.is_file():
                (目标 / f.name).write_bytes(f.read_bytes())

    pub._export_packages(快照)

    索引 = 快照 / "packages" / "索引.json"
    assert 索引.is_file(), "没有产出 packages/索引.json"
    数据 = json.loads(索引.read_text(encoding="utf-8"))
    assert set(数据["包"]) == {"基础库", "数学工具"}

    下载 = 快照 / "packages" / "下载"
    from oracle.jishi import packages as PKG

    for 名, 条 in 数据["包"].items():
        # 下载地址指向 GitHub 的 raw（兜底就是靠它）。
        # ⚠️ 索引里存的是 `encode_url` 之后的地址（`下载` 这种非 ASCII 会被
        #    百分号编码），所以前缀也要按同一口径比。
        assert 条["下载地址"].startswith(PKG.encode_url(pub._RAW_PKG_BASE)), 条["下载地址"]
        z = 下载 / f"{名}-{条['版本']}.zip"
        assert z.is_file(), f"缺 zip：{z}"
        # 校验和必须与**刚打出来的那份 zip** 对得上（否则装的时候会拦下）
        assert 条["校验和"] == "sha256:" + hashlib.sha256(z.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# ③ 包.json 的「基石版本」
# ---------------------------------------------------------------------------

def test_基石版本字段_满足就装得上(tmp_path):
    from oracle.jishi import packages as PKG
    from oracle.jishi.version import VERSION

    包 = tmp_path / "src"
    _写(包 / "包.json", json.dumps(
        {"名字": "甲", "版本": "1.0.0", "基石版本": f">={VERSION}"},
        ensure_ascii=False) + "\n")
    _写(包 / "甲.jsh", "函数 甲()：\n    返回 1\n")

    dest = tmp_path / "dest"
    dest.mkdir()
    PKG.install_package_dir(str(包), "甲", dest_root=str(dest))
    assert (dest / "甲" / "包.json").is_file()


def test_基石版本字段_不满足就拦下(tmp_path):
    from oracle.jishi import packages as PKG
    from oracle.jishi.errors import PackageVersionError

    包 = tmp_path / "src"
    _写(包 / "包.json", json.dumps(
        {"名字": "甲", "版本": "1.0.0", "基石版本": ">=9.9.9"},
        ensure_ascii=False) + "\n")
    _写(包 / "甲.jsh", "函数 甲()：\n    返回 1\n")

    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(PackageVersionError) as ei:
        PKG.install_package_dir(str(包), "甲", dest_root=str(dest))
    assert ">=9.9.9" in str(ei.value)
    assert "E4010" in str(ei.value)
    assert "jishi 升级" in str(ei.value)          # 修法必须可执行
    assert not (dest / "甲").exists(), "被拦下了就不该留下半个包"


def test_基石版本字段_类型不对要报清楚(tmp_path):
    from oracle.jishi import packages as PKG
    from oracle.jishi.errors import PackageMetaError

    包 = tmp_path / "src"
    _写(包 / "包.json", json.dumps(
        {"名字": "甲", "版本": "1.0.0", "基石版本": 1}, ensure_ascii=False) + "\n")
    with pytest.raises(PackageMetaError) as ei:
        PKG.read_package_meta(str(包))
    assert "基石版本" in str(ei.value)
