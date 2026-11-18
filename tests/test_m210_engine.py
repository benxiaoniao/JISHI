# -*- coding: utf-8 -*-
"""M210 · 写工程的短板：`全局` 声明 · `内建.X` 逃生口 · 包内依赖 · 持久文件句柄。

## 这一站解决什么

「基石数据库」与磐石 IDE 各撞过一遍的四件事：

| 缺什么 | 不补会怎样 |
|---|---|
| `全局 甲` 声明 | 函数里 `甲 = 甲 + 1` **静默**变成局部 —— 模块级状态只能靠字典绕 |
| `内建.名字(...)` | 包的门面遮住内建名后（`基石数据库` 导出 `打开`），连开文件都没词了 |
| 包内 `导入 X 从 本地包` | 整条包依赖链断在包内部（报「运行时动态导入不支持…」） |
| 持久文件句柄 | 循环里 `文件.追加文本` 每行开关一次文件（实测 **500×**） |

## 判据怎么验

- `全局` / `内建`：**同一段程序在 Rust / Python(树遍历) / Python(字节码) / Node 上逐字对比**
  —— 这是语言语义，四个引擎必须一个答案。
- 包内依赖：真造两个包（乙依赖甲），跑通；再故意造环，看报错。
- 文件句柄：**量比值**（≥5×），不量绝对毫秒（机器不同）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path, tmp_bc_path  # noqa: E402

RS = rust_exe_path()
PY = sys.executable


def _写(tmp: Path, 名: str, 源: str) -> Path:
    f = tmp / f"{名}-{os.getpid()}.jsh"
    f.write_text(源, encoding="utf-8", newline="\n")
    return f


def _跑(文件: Path, 树遍历: bool = True) -> tuple[int, str]:
    """Python 宿主跑一遍（默认树遍历；`树遍历=False` 走字节码 VM）。
    ⚠️ CLI 没有 `--执行器` 选项，走字节码要 `--dump-bytecode` 再 `--load-bytecode`。"""
    if 树遍历:
        r = subprocess.run([PY, "-m", "oracle.jishi.cli", str(文件)], cwd=str(ROOT),
                           capture_output=True, text=True, encoding="utf-8", timeout=180)
    else:
        待 = tmp_bc_path(文件.with_suffix(".bc.json"))
        r1 = subprocess.run([PY, "-m", "oracle.jishi.cli", "--dump-bytecode", str(文件)],
                            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                            timeout=180)
        assert r1.returncode == 0, (r1.stdout or "") + (r1.stderr or "")
        待.write_text(r1.stdout, encoding="utf-8")
        r = subprocess.run([PY, "-m", "oracle.jishi.cli", "--load-bytecode", str(待)],
                           cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                           timeout=180)
    return r.returncode, (r.stdout or "").replace("\r\n", "\n")


def _跑错(文件: Path) -> tuple[int, str]:
    """跑一遍，**把 stderr 也并进来**（报错都走 stderr，只看 stdout 会拿到空串）。"""
    r = subprocess.run([str(RS), str(文件)], capture_output=True, text=True,
                       encoding="utf-8", timeout=180)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).replace("\r\n", "\n")


def _跑rs(文件: Path) -> tuple[int, str]:
    r = subprocess.run([str(RS), str(文件)], capture_output=True, text=True,
                       encoding="utf-8", timeout=180)
    return r.returncode, (r.stdout or "").replace("\r\n", "\n")


def _跑node(文件: Path) -> tuple[int, str] | None:
    if shutil.which("node") is None:
        return None
    from oracle.jishi import serialize
    from oracle.jishi.compiler import compile_source
    bc = tmp_bc_path(文件.with_suffix(".json"))
    bc.write_text(serialize.dumps(compile_source(文件.read_text(encoding="utf-8"),
                                                str(文件))), encoding="utf-8")
    r = subprocess.run(["node", str(ROOT / "oracle" / "node" / "index.js"), str(bc)],
                       capture_output=True, text=True, encoding="utf-8", timeout=180,
                       cwd=str(ROOT))
    return r.returncode, (r.stdout or "").replace("\r\n", "\n")


def _四引擎一致(tmp: Path, 名: str, 源: str) -> str:
    """Rust / Python树遍历 / Python字节码 / Node —— 输出必须逐字相同。"""
    f = _写(tmp, 名, 源)
    出: dict[str, tuple[int, str]] = {}
    if RS.is_file():
        出["rust"] = _跑rs(f)
    出["py-树"] = _跑(f, 树遍历=True)
    出["py-字节码"] = _跑(f, 树遍历=False)
    n = _跑node(f)
    if n is not None:
        出["node"] = n
    for k, (码, 文) in 出.items():
        assert 码 == 0, f"[{k}] 退出码 {码}：{文[:400]}"
    基准 = 出["py-树"][1]
    for k, (_, 文) in 出.items():
        assert 文 == 基准, f"[{k}] 与基准不一致：\n基准={基准!r}\n实得={文!r}"
    return 基准


# ---------------------------------------------------------------------------
# 一、`全局` 声明（A2）
# ---------------------------------------------------------------------------

_计数源 = '''计数 = 0
缓存 = {"次数": 0}

函数 加()：
    全局 计数, 缓存
    计数 = 计数 + 1
    缓存["次数"] = 缓存["次数"] + 1

加()
加()
加()
打印(计数, 缓存["次数"])
'''


def test_全局_声明后改的是顶层(tmp_path):
    出 = _四引擎一致(tmp_path, "global", _计数源)
    assert 出.strip() == "3 3", 出


def test_没有全局声明就还是局部(tmp_path):
    """现状契约：不声明 ⇒ 函数里那个是局部（与以前一致，不静默改语义）。"""
    源 = '''计数 = 0
函数 加()：
    计数 = 5
加()
打印(计数)
'''
    出 = _四引擎一致(tmp_path, "no-global", 源)
    assert 出.strip() == "0", 出


def test_嵌套函数各自的全局声明互不影响(tmp_path):
    源 = '''总 = 0
函数 外()：
    全局 总
    总 = 总 + 1
    函数 内()：
        总 = 99
    内()
    返回 总
打印(外(), 总)
'''
    出 = _四引擎一致(tmp_path, "nested-global", 源)
    # 内() 没声明全局 ⇒ 它的 总 是局部，外() 的 总 仍是 1
    assert 出.strip() == "1 1", 出


def test_模块顶层写全局也不报错(tmp_path):
    源 = '''全局 甲
甲 = 7
打印(甲)
'''
    出 = _四引擎一致(tmp_path, "top-global", 源)
    assert 出.strip() == "7", 出


def test_全局的语法错误说得清(tmp_path):
    坏 = [
        ("全局\n", "要跟变量名"),
        ("全局 甲 乙\n", "多了"),
    ]
    for i, (片段, 要) in enumerate(坏):
        f = _写(tmp_path, f"gbad{i}", 片段)
        码, 文 = _跑错(f)
        assert 码 != 0, f"`{片段.strip()}` 竟然没报错：{文[:200]}"
        assert 要 in 文, f"`{片段.strip()}` 的报错没说清：{文[:300]}"


def test_全局变量这种名字还能用(tmp_path):
    """`全局` 进了「粘连检查跳过集」：`全局变量` / `全局配置` 仍是正常标识符。"""
    源 = '''全局变量 = 1
全局配置 = {"甲": 2}
打印(全局变量, 全局配置["甲"])
'''
    出 = _四引擎一致(tmp_path, "glue-skip", 源)
    assert 出.strip() == "1 2", 出


# ---------------------------------------------------------------------------
# 二、`内建.X` 逃生口（F1 后半）
# ---------------------------------------------------------------------------

def test_门面遮住内建名后还能用内建(tmp_path):
    源 = '''函数 打开(路径)：
    返回 "我是被包遮住的那个"

打印(打开("x"))
内建.打印("从内建打印")
令 f = 内建.打开("build/m210/占位.txt", "写")
f.关闭()
打印("内建.打开 也能用")
'''
    (ROOT / "build" / "m210").mkdir(parents=True, exist_ok=True)
    出 = _四引擎一致(tmp_path, "escape", 源)
    assert 出.strip().splitlines() == [
        "我是被包遮住的那个", "从内建打印", "内建.打开 也能用"], 出


def test_内建里没有内部名字(tmp_path):
    """`$造模块` 这类 `$` 开头的内部名不该出现在 `内建` 里（模型会照抄）。"""
    源 = '''打印(内建.$造模块)
'''
    f = _写(tmp_path, "escape-dollar", 源)
    码, 文 = _跑错(f)
    assert 码 != 0, f"`内建.$造模块` 竟然读得到：{文[:200]}"


# ---------------------------------------------------------------------------
# 三、包内 `导入 X 从 本地包`（F1 前半）
# ---------------------------------------------------------------------------

def _造包(根: Path, 名: str, 源码: str, 依赖: dict | None = None) -> None:
    目录 = 根 / ".jishi" / "packages" / 名
    目录.mkdir(parents=True, exist_ok=True)
    (目录 / f"{名}.jsh").write_text(源码, encoding="utf-8", newline="\n")
    meta = {"名": 名, "版本": "0.1.0", "入口": f"{名}.jsh"}
    if 依赖:
        meta["依赖"] = 依赖
    (目录 / "包.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def _包工程(tmp: Path, 主: str) -> Path:
    根 = tmp / f"工程-{os.getpid()}"
    根.mkdir(parents=True, exist_ok=True)
    _造包(根, "甲包", "函数 问好(名)：\n    返回 `你好，` + 名\n")
    _造包(根, "乙包",
         "导入 甲包 从 本地包\n\n函数 转告(名)：\n    返回 甲包.问好(名) + `（转）`\n")
    (根 / "主.jsh").write_text(主, encoding="utf-8", newline="\n")
    return 根


def test_包可以导入另一个包(tmp_path):
    """乙包体里写 `导入 甲包 从 本地包`（**不**在 包.json 里声明依赖）也要能跑。"""
    根 = _包工程(tmp_path, "导入 乙包 从 本地包\n打印(乙包.转告(\"世界\"))\n")
    r = subprocess.run([str(RS), "主.jsh"], cwd=str(根), capture_output=True,
                       text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")[:500]
    assert r.stdout.strip() == "你好，世界（转）", r.stdout

    r2 = subprocess.run([PY, "-m", "oracle.jishi.cli", "主.jsh"], cwd=str(根),
                        capture_output=True, text=True, encoding="utf-8", timeout=180,
                        env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert r2.returncode == 0, (r2.stdout or "") + (r2.stderr or "")[:500]
    assert r2.stdout.strip() == "你好，世界（转）", r2.stdout


def test_包内依赖成环要报清楚(tmp_path):
    根 = tmp_path / f"环-{os.getpid()}"
    根.mkdir(parents=True, exist_ok=True)
    _造包(根, "甲包", "导入 乙包 从 本地包\n函数 甲()：\n    返回 1\n")
    _造包(根, "乙包", "导入 甲包 从 本地包\n函数 乙()：\n    返回 2\n")
    (根 / "主.jsh").write_text("导入 甲包 从 本地包\n打印(甲包.甲())\n",
                               encoding="utf-8", newline="\n")
    r = subprocess.run([str(RS), "主.jsh"], cwd=str(根), capture_output=True,
                       text=True, encoding="utf-8", timeout=120)
    文 = (r.stdout or "") + (r.stderr or "")
    assert r.returncode != 0, 文[:400]
    assert "循环依赖" in 文, 文[:400]


def test_脚本旁边的包从任意目录都能找到(tmp_path):
    """B2：cd 到别处跑 `子/主.jsh`，`子/.jishi/packages/` 里的包也要找得到。"""
    根 = tmp_path / f"b2-{os.getpid()}"
    子 = 根 / "子目录"
    子.mkdir(parents=True)
    _造包(子, "甲包", "函数 问好()：\n    返回 \"找到了\"\n")
    (子 / "主.jsh").write_text("导入 甲包 从 本地包\n打印(甲包.问好())\n",
                               encoding="utf-8", newline="\n")
    r = subprocess.run([str(RS), str(子 / "主.jsh")], cwd=str(根), capture_output=True,
                       text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")[:400]
    assert r.stdout.strip() == "找到了", r.stdout


# ---------------------------------------------------------------------------
# 四、持久文件句柄（W0）：量比值
# ---------------------------------------------------------------------------

@pytest.mark.计时
def test_持久句柄比逐行追加快得多(tmp_path):
    """同一个文件、同样 400 次写：句柄 vs `文件.追加文本`，比值 ≥5×（实测 ~500×）。

    ⚠️ 打 `计时`（进串行节）：比的是**墙上时间**，并行负载会把两边一起抬；
    而且这是一条 IO 基准，独占跑才可信。判的是**比值**，不是绝对毫秒。
    """
    源 = '''导入 文件
导入 时间
导入 文本 为 文

令 路 = "build/m210/bench-句柄.txt"
文件.写文本(路, "")
令 t0 = 时间.高精度时间()
用 打开(路, "追加") 为 f：
    遍历 i 在 范围(400)：
        f.写行(`第` + 文本(i) + `行`)
令 t1 = 时间.高精度时间()

令 路2 = "build/m210/bench-逐行.txt"
文件.写文本(路2, "")
令 t2 = 时间.高精度时间()
遍历 i 在 范围(400)：
    文件.追加文本(路2, `第` + 文本(i) + `行` + "\\n")
令 t3 = 时间.高精度时间()
打印(文.小数位((t1 - t0) * 1000, 1), 文.小数位((t3 - t2) * 1000, 1))
'''
    (ROOT / "build" / "m210").mkdir(parents=True, exist_ok=True)
    f = _写(tmp_path, "io", 源)
    if not RS.is_file():
        pytest.skip("发行版没编")
    码, 文 = _跑错(f)
    assert 码 == 0, 文[:400]
    快, 慢 = [float(x) for x in 文.split()]
    assert 慢 > 快, f"句柄并没有更快：句柄 {快} ms，逐行 {慢} ms"
    # 400 次里快的那条几乎为 0；给宽容阈值，只要求「至少 5 倍」
    assert 慢 / max(快, 1e-3) >= 5.0, f"只快 {慢 / 快:.1f}×（要求 ≥5×）"


def test_追加行们一次写完也快(tmp_path):
    """`追加行们`（M199）是一次开关文件 —— 顺手钉住它没退化成逐行。"""
    源 = '''导入 文件
令 路 = "build/m210/bench-行们.txt"
文件.写文本(路, "")
文件.追加行们(路, ["甲", "乙", "丙"])
打印(文件.读文本(路))
'''
    (ROOT / "build" / "m210").mkdir(parents=True, exist_ok=True)
    f = _写(tmp_path, "lines", 源)
    码, 文 = _跑rs(f) if RS.is_file() else _跑(f)
    assert 码 == 0, 文[:400]
    assert 文.strip().splitlines() == ["甲", "乙", "丙"], 文
