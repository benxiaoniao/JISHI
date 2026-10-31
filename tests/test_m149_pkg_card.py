# -*- coding: utf-8 -*-
"""M149：`--ai-card` 带上「已装包的 API」。

## 为什么要有这一段

模型根本不知道你**装了什么包** —— 于是装完也用不对：它会去猜包里有什么函数、
或者干脆自己手写一遍。往卡片里塞「本机装了哪些包、每个包有哪些对外函数」，
是这件事最直接的解法（`AGENTS.md` §0.5 核心二）。

## 三条硬要求（本文件的判据就围着它们）

1. **两个宿主的卡片逐字一致**：卡片是**语言事实的投影**，`test_m90` 早就盯着
   这一条；M149 加的这一段同样要一字不差，所以「扫包」的那套规则两边各写一遍
   （Python 的 `ai.扫包里的公开函数` 与 Rust 的 `langspec::扫包里的公开函数`），
   拿同一份源码喂进去必须得到同一份结果。
2. **没装包时一个字节都不能变**：干净环境（CI）里的卡片必须与 M149 之前
   **完全一样**，否则所有拿它当基线的判据都会无谓地红。⇒ 没包就**整段不出现**。
3. **规则要与「导入」一致**：`包.json` 声明了「入口」就只扫入口那一个文件；
   没声明就按文件名排序把全部 `.jsh` 都扫一遍 —— 因为导入时就是这么把它们编进
   同一个作用域的（老包 `基础库` 就没写「入口」）。

⚠️ 这个功能天生**依赖环境**（你装了包才有内容）。所以本文件所有用例都把
「包目录」搬进临时目录（`JISHI_PKG_ROOTS` + `chdir` + `HOME`），
**绝不读开发机/CI 上真实装的包** —— 否则同一条判据在这台机器绿、那台机器红。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT, rust_exe_path

sys.path.insert(0, str(ROOT))

from oracle.jishi import ai  # noqa: E402
from oracle.jishi import localpkg  # noqa: E402


# ---------------------------------------------------------------------------
# 隔离：让「本机装的包」变成我们造的那几个
# ---------------------------------------------------------------------------

def _造包(根: Path, 名: str, *, 版本: str = "1.0.0", 描述: str = "",
          入口: str | None = None, 文件们: dict | None = None) -> Path:
    """在 `根` 下造一个包目录（不经过 `jishi 安装`，就是要造各种怪形状）。"""
    目录 = 根 / 名
    目录.mkdir(parents=True, exist_ok=True)
    元 = {"名字": 名, "版本": 版本}
    if 描述:
        元["描述"] = 描述
    if 入口 is not None:
        元["入口"] = 入口
    (目录 / "包.json").write_text(
        json.dumps(元, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    for 文件名, 内容 in (文件们 or {}).items():
        (目录 / 文件名).write_text(内容, encoding="utf-8", newline="\n")
    return 目录


@pytest.fixture
def 包根(tmp_path, monkeypatch):
    """把「本机装的包」收进 `tmp/pkgs`；cwd 与 HOME 也挪走（不读真实环境）。"""
    根 = tmp_path / "pkgs"
    根.mkdir()
    家 = tmp_path / "home"
    家.mkdir()
    monkeypatch.setenv(localpkg.PKG_ROOTS_ENV, str(根))
    monkeypatch.setenv("HOME", str(家))
    monkeypatch.setenv("USERPROFILE", str(家))
    monkeypatch.chdir(tmp_path)
    return 根


def _跑_rust_card(参数: list[str], cwd: Path, 包根: Path) -> str:
    rs = rust_exe_path()
    if not rs.is_file():
        pytest.skip("没有编出 jishi-rs")
    环境 = {**os.environ, localpkg.PKG_ROOTS_ENV: str(包根),
            "HOME": str(cwd / "home"), "USERPROFILE": str(cwd / "home")}
    r = subprocess.run([str(rs), "--ai-card"] + 参数, cwd=str(cwd),
                       capture_output=True, text=True, encoding="utf-8",
                       env=环境)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _跑_python_card(参数: list[str], cwd: Path, 包根: Path) -> str:
    环境 = {**os.environ, localpkg.PKG_ROOTS_ENV: str(包根),
            "HOME": str(cwd / "home"), "USERPROFILE": str(cwd / "home"),
            "PYTHONPATH": str(ROOT)}
    旧 = os.getcwd()
    os.chdir(str(cwd))
    try:
        r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", "--ai-card"] + 参数,
                           cwd=str(cwd), capture_output=True, text=True,
                           encoding="utf-8", env=环境)
    finally:
        os.chdir(旧)
    assert r.returncode == 0, r.stderr
    return r.stdout


# ---------------------------------------------------------------------------
# 一、扫函数的规则（纯函数层）
# ---------------------------------------------------------------------------

样例源码 = '''# 包的开头注释
导入 文件

函数 公开甲(甲, 乙 = 空):
    """一行说明就够了。

    第二段不该进卡片（会把卡片撑爆）。
    """
    返回 甲

函数 公开乙(宽, 高, 底色 = 空图(1, 1)):
    返回 宽

函数 _内部丙():
    """这个名字以 `_` 开头 —— 是实现细节，不算对外。"""
    返回 1

函数 没有文档(甲):
    返回 甲

函数 文档占一行(甲):
    """  两侧有空格要先去掉  """

类 不是函数:
    函数 类里的(自身):
        """缩进的方法不算包的对外函数。"""
        返回 1
'''


def test_扫函数的规则():
    """顶格 + 不以 `_` 开头 = 对外；文档取**首行**；默认值里的括号不能切断签名。"""
    函数们 = ai.扫包里的公开函数(样例源码)
    签名 = [f["sig"] for f in 函数们]
    assert 签名 == [
        "公开甲(甲, 乙 = 空)",
        # ⚠️ 默认值里有括号：切错了就会变成 `公开乙(宽, 高, 底色 = 空图(1` 这种
        "公开乙(宽, 高, 底色 = 空图(1, 1))",
        "没有文档(甲)",
        "文档占一行(甲)",
    ], 签名
    文档 = [f["doc"] for f in 函数们]
    assert 文档[0] == "一行说明就够了。", 文档          # 多行只取首行
    assert 文档[1] == "", 文档                          # 紧跟的不是文档串
    assert 文档[2] == "", 文档
    assert 文档[3] == "两侧有空格要先去掉", 文档


def test_两宿主扫出同一份(包根, tmp_path):
    """同一份源码，Python 与 Rust 扫出来的**每一条都相同**（签名与文档）。

    ⚠️ 这条是 M149 的**核心判据**：扫包规则写了两遍（一个 Python、一个 Rust），
    不拿同一份输入比就不可能知道它们一致 —— 而卡片一旦两边不同，
    `test_m90` 那条「卡片逐字一致」就红，可它红的时候你并不知道**为什么**。
    """
    _造包(包根, "样例包", 版本="2.3.4", 描述="扫包规则的样例",
          入口="样例包.jsh", 文件们={"样例包.jsh": 样例源码})
    py = ai.已装的包()
    assert [f["sig"] for f in py[0]["函数们"]] == [
        "公开甲(甲, 乙 = 空)", "公开乙(宽, 高, 底色 = 空图(1, 1))",
        "没有文档(甲)", "文档占一行(甲)"], py
    rs = _跑_rust_card([], tmp_path, 包根)
    for f in py[0]["函数们"]:
        行 = f"- `{f['sig']}`" + (f" —— {f['doc']}" if f["doc"] else "")
        assert 行 in rs, f"Rust 卡片里没有这一行：{行}\n{rs[-800:]}"


# ---------------------------------------------------------------------------
# 二、卡片里的那一段
# ---------------------------------------------------------------------------

def test_卡里带已装包_两宿主逐字一致(包根, tmp_path):
    """装了两个包：卡片里都有（按名字排序），且两个宿主的卡片**完全一样**。"""
    _造包(包根, "乙乙", 版本="0.2.0", 描述="第二个",
          入口="乙乙.jsh",
          文件们={"乙乙.jsh": '函数 算(甲):\n    """算一下。"""\n    返回 甲\n'})
    _造包(包根, "甲甲", 版本="1.0.0", 描述="第一个",
          入口="甲甲.jsh",
          文件们={"甲甲.jsh": '函数 造(个数):\n    返回 个数\n'})

    py = _跑_python_card([], tmp_path, 包根)
    rs = _跑_rust_card([], tmp_path, 包根)
    assert py == rs, "两个宿主的卡片不一致（含已装包段）"

    段 = py[py.index("## 已装的包"):py.index("## 对象方法")]
    assert "### `甲甲`（1.0.0） —— 第一个" in 段, 段
    assert "- `造(个数)`" in 段, 段                    # 没文档就不加「—— 」
    assert "### `乙乙`（0.2.0） —— 第二个" in 段, 段
    assert "- `算(甲)` —— 算一下。" in 段, 段
    # 排序**按码点**（与 `--lang-spec` 的模块清单同一条口径）：
    # `乙` U+4E59 < `甲` U+7532 ⇒ 「乙乙」在前。别按「甲乙丙」的语感猜。
    assert 段.index("乙乙") < 段.index("甲甲"), 段


def test_没装包时整段不出现(包根, tmp_path):
    """干净环境里，卡片必须与 M149 之前**一模一样** —— 一个字节都不许多。

    ⚠️ 这条是给 CI 的：CI 上没有 `.jishi/packages`，如果这里多出一个空标题
    （或者多一个空行），所有拿卡片做基线的判据都会红。
    """
    py = _跑_python_card([], tmp_path, 包根)
    rs = _跑_rust_card([], tmp_path, 包根)
    assert "已装的包" not in py, py
    assert py == rs
    # 标准库段后面紧接「对象方法」，中间**只有**一个空行（与 M149 之前一样）
    前 = py.index("## 对象方法")
    assert py[前 - 2:前] == "\n\n" and py[前 - 3] != "\n", \
        "包段那段的空行没收拾干净（多一个或少一个空行，两个宿主的卡片就不等了）"


def test_单模块分片不带包段(包根, tmp_path):
    """`--ai-card 文本` 是「只要这一个模块」，包表会把预算吃掉 ⇒ 不带。"""
    _造包(包根, "甲甲", 入口="甲甲.jsh",
          文件们={"甲甲.jsh": '函数 造():\n    返回 1\n'})
    py = _跑_python_card(["文本"], tmp_path, 包根)
    assert "已装的包" not in py, py
    assert py == _跑_rust_card(["文本"], tmp_path, 包根)


def test_任务卡也带包段(包根, tmp_path):
    """`--任务 表格` 是「这类活」—— 装了包就该看得见（与全量卡同一条判断）。"""
    _造包(包根, "甲甲", 入口="甲甲.jsh",
          文件们={"甲甲.jsh": '函数 造():\n    返回 1\n'})
    py = _跑_python_card(["--任务", "表格"], tmp_path, 包根)
    assert "## 已装的包" in py, py
    assert py == _跑_rust_card(["--任务", "表格"], tmp_path, 包根)


# ---------------------------------------------------------------------------
# 三、边界：入口规则 / 同名 / 坏包
# ---------------------------------------------------------------------------

def test_没写入口就扫全部jsh(包根, tmp_path):
    """`包.json` 没写「入口」⇒ 按文件名排序把**全部** `.jsh` 都扫（与导入一致）。

    老包（如 `基础库`）就是这样：只扫 `<包名>.jsh` 会一个字都扫不到。
    """
    _造包(包根, "老包", 版本="1.0.0",
          入口=None,
          文件们={"a_先.jsh": '函数 甲():\n    返回 1\n',
                  "b_后.jsh": '函数 乙():\n    返回 2\n'})
    包们 = ai.已装的包()
    assert [f["sig"] for f in 包们[0]["函数们"]] == ["甲()", "乙()"]


def test_同名包先出现的赢(包根, tmp_path):
    """两个包根里都有同名包 ⇒ 用**先搜到**的那个（额外根优先于项目目录）。

    卡片说的必须是「你真正 `导入` 得到的那个」；说错了，模型会照着另一个版本
    的签名写代码，然后运行时报「没有这个函数」。
    """
    项目根 = tmp_path / ".jishi" / "packages"
    _造包(项目根, "同名", 版本="9.9.9", 入口="同名.jsh",
          文件们={"同名.jsh": '函数 旧版():\n    返回 1\n'})
    _造包(包根, "同名", 版本="1.0.0", 入口="同名.jsh",
          文件们={"同名.jsh": '函数 新版():\n    返回 2\n'})
    包们 = ai.已装的包()
    assert len(包们) == 1, 包们
    assert 包们[0]["版本"] == "1.0.0", 包们[0]
    assert [f["sig"] for f in 包们[0]["函数们"]] == ["新版()"]


def test_坏包被跳过不报错(包根, tmp_path):
    """没有 `包.json` / 不是合法 JSON / 顶层不是对象的目录都要**跳过**。

    卡片是给人贴进提示词的东西 —— 不能因为某个装了一半的包就打不出来。
    """
    (包根 / "没清单").mkdir()
    (包根 / "没清单" / "没清单.jsh").write_text("函数 甲():\n    返回 1\n",
                                            encoding="utf-8")
    坏 = 包根 / "坏JSON"
    坏.mkdir()
    (坏 / "包.json").write_text("{ 不是 json", encoding="utf-8")
    顶 = 包根 / "顶层是列表"
    顶.mkdir()
    (顶 / "包.json").write_text("[1, 2]", encoding="utf-8")
    _造包(包根, "好的", 入口="好的.jsh", 文件们={"好的.jsh": "函数 甲():\n    返回 1\n"})

    assert [p["名字"] for p in ai.已装的包()] == ["好的"]
    卡 = _跑_python_card([], tmp_path, 包根)
    assert "## 已装的包" in 卡 and "### `好的`" in 卡, 卡
    assert "没清单" not in 卡 and "坏JSON" not in 卡, 卡


def test_能导入就出现在卡片里(包根, tmp_path):
    """**卡片说的必须是真的**：卡片里列出的包，`导入 X 从 本地包` 一定跑得通。

    这条把「卡片」和「导入」绑在一起 —— 否则卡片可能列一个根本导入不了的包
    （名字对不上、入口写错），模型照着写就一路报错。
    """
    _造包(包根, "可用包", 版本="1.0.0", 描述="能被导入的",
          入口="可用包.jsh",
          文件们={"可用包.jsh": '函数 加一(甲):\n    """加一。"""\n    返回 甲 + 1\n'})
    程序 = tmp_path / "用.jsh"
    程序.write_text('导入 可用包 从 本地包\n打印(可用包.加一(41))\n',
                    encoding="utf-8", newline="\n")
    r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", "用.jsh"],
                       cwd=str(tmp_path), capture_output=True, text=True,
                       encoding="utf-8",
                       env={**os.environ, localpkg.PKG_ROOTS_ENV: str(包根),
                            "PYTHONPATH": str(ROOT)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() == "42", r.stdout
    卡 = _跑_python_card([], tmp_path, 包根)
    assert "### `可用包`（1.0.0） —— 能被导入的" in 卡, 卡
    assert "- `加一(甲)` —— 加一。" in 卡, 卡
