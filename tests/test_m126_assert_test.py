# -*- coding: utf-8 -*-
"""M126：`jishi 测试` 命令。

## 先说清楚 M126 到底交付了什么

`测试` 模块（R7.1-c+ 就有了，11 个函数：`用例` / `相等` / `为真` / `近似` /
`包含` / `抛出` / `统计` / `汇总` …）早就解决了「**一个文件里**跑多个用例」。
M126 补的是另一半：**不用人记文件名**。以前要一个个 `jishi xxx_test.jsh` 敲，
十个文件敲十次 —— 文件一多就没人跑了。

⚠️ 所以这里**不测「断言怎么工作」**（那是 `测试` 模块自己的判据，见 `test_m88`），
只测**调度**：能不能找到、能不能跑完、两类失败能不能分开、有没有总览。

⚠️ 有一处**故意**：`test_断言失败报成测试没过` 等会跑**必定失败**的测试文件 ——
那是被测行为，不是本项目自己红。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _cli(*arg: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "oracle.jishi.cli", *arg],
        capture_output=True, cwd=str(cwd or ROOT), timeout=120)


# ---------------------------------------------------------------------------
# 一、找文件：命名约定就是接口
# ---------------------------------------------------------------------------

def _建(tmp_path: Path, **文件: str) -> Path:
    目录 = tmp_path / "试验场"
    目录.mkdir()
    for 名, 内容 in 文件.items():
        (目录 / f"{名}.jsh").write_text(内容, encoding="utf-8")
    return 目录


_过 = """导入 测试

函数 甲()：
    测试.相等(1 + 1, 2)

测试.用例("一加一", 甲)
测试.汇总()
"""

_红 = """导入 测试

函数 甲()：
    测试.相等(2, 3, "故意写错")

测试.用例("故意写错", 甲)
测试.汇总()
"""

_崩 = "令 甲 = 1 / 0\n"


def test_全过时退出码是0(tmp_path):
    目录 = _建(tmp_path, **{"甲_test": _过, "乙_test": _过})
    r = _cli("测试", str(目录))
    出 = r.stdout.decode("utf-8")
    assert r.returncode == 0, 出 + r.stderr.decode("utf-8", "replace")
    assert "2/2 个测试文件通过" in 出, 出


def test_只扫_test_jsh(tmp_path):
    """`随便.jsh` 不该被当成测试跑 —— 否则一个项目里所有脚本都会被跑一遍。"""
    目录 = _建(tmp_path, **{"真测试_test": _过, "干扰": _崩})
    r = _cli("测试", str(目录))
    出 = r.stdout.decode("utf-8")
    assert r.returncode == 0, 出 + r.stderr.decode("utf-8", "replace")
    assert "1/1 个测试文件通过" in 出, 出


def test_会递归子目录(tmp_path):
    """测试通常分目录放（`测试/文本/`、`测试/网络/`），不递归就得一个个进门。"""
    根 = tmp_path / "项目"
    (根 / "子" / "孙").mkdir(parents=True)
    (根 / "子" / "孙" / "深_test.jsh").write_text(_过, encoding="utf-8")
    r = _cli("测试", str(根))
    出 = r.stdout.decode("utf-8")
    assert r.returncode == 0, 出 + r.stderr.decode("utf-8", "replace")
    assert "深_test.jsh" in 出 and "1/1" in 出, 出


def test_没有测试文件要说清楚(tmp_path):
    """空目录**不能静默返回 0** —— 那看起来像「全绿」，实际什么都没跑。"""
    空 = tmp_path / "空的"
    空.mkdir()
    r = _cli("测试", str(空))
    assert r.returncode == 2
    assert "_test.jsh" in r.stderr.decode("utf-8", "replace")


def test_目录不存在给非0(tmp_path):
    r = _cli("测试", str(tmp_path / "根本没有"))
    assert r.returncode == 2


# ---------------------------------------------------------------------------
# 二、两类失败必须分开报
# ---------------------------------------------------------------------------

def test_断言失败报成测试没过(tmp_path):
    目录 = _建(tmp_path, **{"红_test": _红})
    r = _cli("测试", str(目录))
    出 = r.stdout.decode("utf-8")
    assert r.returncode == 1
    assert "1 个有断言没通过" in 出, 出
    assert "不是断言失败" not in 出, 出          # 断言失败不该被说成「程序出错」


def test_程序崩了单独算(tmp_path):
    """🔴 本里程碑的重点：**「你的断言写错了」与「你的程序炸了」是两回事**。

    混成一句「测试失败」，用户会去翻断言 —— 可实际是他的代码在更早就崩了。
    """
    目录 = _建(tmp_path, **{"崩_test": _崩})
    r = _cli("测试", str(目录))
    出 = r.stdout.decode("utf-8")
    assert r.returncode == 1
    assert "1 个程序出错" in 出, 出
    assert "不是断言失败" in 出, 出
    assert "1 个有断言没通过" not in 出, 出


def test_两种失败同在时都数(tmp_path):
    目录 = _建(tmp_path, **{"红_test": _红, "崩_test": _崩, "绿_test": _过})
    r = _cli("测试", str(目录))
    出 = r.stdout.decode("utf-8")
    assert r.returncode == 1
    assert "1/3 个测试文件通过" in 出, 出
    assert "1 个有断言没通过" in 出, 出
    assert "1 个程序出错" in 出, 出


# ---------------------------------------------------------------------------
# 三、明细交给 `测试.汇总()` 自己报（别重复拼）
# ---------------------------------------------------------------------------

def test_复用测试模块的明细(tmp_path):
    """文件里的 `测试.汇总()` 会把**每个用例的名字**和失败原因打出来。

    命令这边不该再拼一遍（拼出来的只会更差），只该给「哪个文件、几个过了」。
    """
    目录 = _建(tmp_path, **{"红_test": _红})
    r = _cli("测试", str(目录))
    都 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert "故意写错" in 都, 都               # 用例名（`测试.汇总()` 给的）
    assert "个用例" in 都, 都


def test_示例目录能跑起来():
    """`examples/测试示例/` 是给用户抄的样板 —— 它必须真的能跑。

    ⚠️ 那个目录里有一个**故意失败**的文件（示范「红的长什么样」），
    所以这里是「能跑、且两种结果都出现」，不是「全绿」。
    """
    r = _cli("测试", str(ROOT / "examples" / "测试示例"))
    出 = r.stdout.decode("utf-8")
    assert "求和_test.jsh" in 出, 出
    assert "故意失败_test.jsh" in 出, 出
    assert "1/2 个测试文件通过" in 出, 出
