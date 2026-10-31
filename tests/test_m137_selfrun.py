# -*- coding: utf-8 -*-
"""M137：`jishi 自测` —— 用「固定版」跑基石自带的黑盒运行器。

## 它补的是「自举」那一环

0.2.8 做的正是「用基石测基石」。但先有鸡还是先有蛋：**新构建的基石要是坏了
（连解析都不行），拿它去跑测试就什么都测不出来** —— 你只看到一次启动失败，
看不出「到底哪 17 条用例挂了」。

⇒ 把两件事**分开记住**：**运行者**（固定版，跑尺子的）和**被测**（被量的）。
尺子用上一版，被量的用新版 —— 新版坏成什么样，尺子都能把结论说出来。

## 判据分两类

**行为**（Python 侧逐条钉）：记住 / 看 / 忘掉 / 找不到就报错 / 透传 / 退出码。

**对齐**（两宿主）：同一条命令、同一份配置下，Python 版和 Rust 版
**输出逐字一致**（只差「用时」）—— `自测` 是开发工具，但它照样有两个实现。

## 🔴 三条容易写歪的地方，这里都钉住了

1. **配置写哪**：只认 `JISHI_HOME`（默认 `~/.jishi`）。判据一律把 `JISHI_HOME`
   指到临时目录 —— 绝不能去改开发机真实的 `~/.jishi`；
2. **固定版找不到时不许退化成「当前版」**：那样自测会变成**自欺**，
   而且是**绿的**（拿自己考自己当然过），比红更糟 ⇒ 一句 `rc=2` + 不写配置；
3. **`--看` 只看不写**：它是「我记着什么」，不是「改成这些」。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
RUST = ROOT / "rust" / "target" / "release" / ("jishi-rs.exe" if os.name == "nt" else "jishi-rs")

运行者文件 = "自测-运行者.txt"
被测文件 = "自测-被测.txt"


def _宿主(宿: str) -> list[str]:
    if 宿 == "python":
        return [PY, "-m", "oracle.jishi.cli"]
    if 宿 == "rust":
        return [str(RUST)]
    raise AssertionError(宿)


def _跑(参数, home: Path, 宿="python", cwd=None):
    """跑 `jishi 自测 …`，把 JISHI_HOME 钉到 `home`（**不碰真实的 ~/.jishi**）。"""
    env = dict(os.environ)
    env["JISHI_HOME"] = str(home)
    return subprocess.run(_宿主(宿) + ["自测"] + list(参数), cwd=str(cwd or ROOT), env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)


def _配用例目录(tmp: Path, 通过=True, 失败=False) -> Path:
    """造一个两三条的小语料（`自测` 的 `--用例` 直接吃它，跑得快）。"""
    目 = tmp / "用例"
    目.mkdir(parents=True, exist_ok=True)
    if 通过:
        (目 / "好.jsh").write_text("打印(1 + 1)\n", encoding="utf-8", newline="\n")
        (目 / "好.out").write_text("2\n", encoding="utf-8", newline="")
    if 失败:
        (目 / "差.jsh").write_text("打印(3)\n", encoding="utf-8", newline="\n")
        (目 / "差.out").write_text("2\n", encoding="utf-8", newline="")
    return 目


def _去掉用时(文: str) -> str:
    return "\n".join(
        行.split("· 用时")[0].rstrip() if "· 用时" in 行 else 行
        for 行 in 文.splitlines())


# ---------------------------------------------------------------------------
# 记 / 看 / 忘
# ---------------------------------------------------------------------------

def test_什么都没记时_看会说清楚用当前这个(tmp_path):
    home = tmp_path / "home"
    结果 = _跑(["--看"], home)
    assert 结果.returncode == 0, 结果.stdout + 结果.stderr
    assert "现在记着" in 结果.stdout
    assert "没记固定版 ⇒ 用当前这个 jishi" in 结果.stdout, 结果.stdout
    assert "没指定 ⇒ 用当前这个 jishi" in 结果.stdout, 结果.stdout
    # `--看` **不许写**任何东西（它不是「改成这些」）
    assert not (home / 运行者文件).exists(), "--看 不该写配置"
    assert not (home / 被测文件).exists(), "--看 不该写配置"


def test_固定版会被记住(tmp_path):
    home = tmp_path / "home"
    目 = _配用例目录(tmp_path)
    结果 = _跑(["--固定版", PY, "-m", "oracle.jishi.cli",
                "--用例", str(目)], home)
    assert 结果.returncode == 0, 结果.stdout + 结果.stderr
    assert "（固定版）" in 结果.stdout, 结果.stdout

    记下 = (home / 运行者文件).read_text(encoding="utf-8").splitlines()
    assert 记下[0] == str(Path(PY).resolve()) or 记下[0].endswith("python.exe") or "python" in 记下[0], 记下
    assert 记下[1:] == ["-m", "oracle.jishi.cli"], 记下

    # 再跑一次不带 --固定版：它应从配置里来
    再看 = _跑(["--看"], home)
    assert str(home) not in 再看.stdout
    assert "（没记固定版" not in 再看.stdout, 再看.stdout
    assert "-m oracle.jishi.cli" in 再看.stdout, 再看.stdout


def test_忘掉会清空并且可以再忘一次(tmp_path):
    home = tmp_path / "home"
    目 = _配用例目录(tmp_path)
    _跑(["--固定版", PY, "-m", "oracle.jishi.cli", "--用例", str(目)], home)
    assert (home / 运行者文件).is_file()

    结果 = _跑(["--忘掉"], home)
    assert 结果.returncode == 0
    assert "已忘掉固定版" in 结果.stdout, 结果.stdout
    assert not (home / 运行者文件).exists()
    assert not (home / 被测文件).exists()

    再忘 = _跑(["--忘掉"], home)
    assert 再忘.returncode == 0
    assert "本来就没记着" in 再忘.stdout, 再忘.stdout


def test_被测也能记住并显示成指定(tmp_path):
    home = tmp_path / "home"
    目 = _配用例目录(tmp_path)
    结果 = _跑(["--被测", PY, "-m", "oracle.jishi.cli", "--用例", str(目)], home)
    assert 结果.returncode == 0, 结果.stdout + 结果.stderr
    assert "（指定）" in 结果.stdout, 结果.stdout
    assert (home / 被测文件).is_file()
    # 只指定被测时，运行者仍是「当前」——「（当前）」这个标记是**跑的时候**才有的，
    # `--看` 那边用的是另一句话（「没记固定版 ⇒ 用当前这个 jishi」）。
    assert "（当前）" in 结果.stdout, "只指定被测时，运行者该标「当前」：" + 结果.stdout


# ---------------------------------------------------------------------------
# 🔴 找不到就报错 —— 绝不悄悄退回「当前版」
# ---------------------------------------------------------------------------

def test_固定版找不到要报错而且不写配置(tmp_path):
    """**悄悄退回去是自欺**：那样自测会拿当前版考当前版，结果还是绿的。"""
    home = tmp_path / "home"
    结果 = _跑(["--固定版", "C:/根本没有这个/jishi.exe"], home)
    assert 结果.returncode == 2, 结果.stdout + 结果.stderr
    assert "找不到这个程序" in 结果.stderr, 结果.stderr
    assert not (home / 运行者文件).exists(), "报错了就不该留下半截配置"


def test_运行器脚本不存在要报错并指路(tmp_path):
    home = tmp_path / "home"
    结果 = _跑(["--运行器", str(tmp_path / "不存在.jsh")], home)
    assert 结果.returncode == 2
    assert "找不到运行器脚本" in 结果.stderr, 结果.stderr
    assert "--运行器" in 结果.stderr, "要告诉人怎么改：" + 结果.stderr


def test_帮助要说清和_测试_的区别(tmp_path):
    """`测试` 跑你项目里的 *_test.jsh；`自测` 跑基石自己的运行器 —— 名字近，必须说清。"""
    结果 = _跑(["--帮助"], tmp_path / "home")
    assert 结果.returncode == 0
    assert "用法：jishi 自测" in 结果.stdout, 结果.stdout
    assert "_test.jsh" in 结果.stdout, 结果.stdout


# ---------------------------------------------------------------------------
# 跑起来：透传 + 退出码
# ---------------------------------------------------------------------------

def test_退出码透传_有失败用例就退1(tmp_path):
    home = tmp_path / "home"
    目 = _配用例目录(tmp_path, 通过=True, 失败=True)
    结果 = _跑(["--用例", str(目)], home)
    assert 结果.returncode == 1, f"有失败用例该退 1：\n{结果.stdout}"
    assert "失败 1" in 结果.stdout, 结果.stdout


def test_透传参数真的到了运行器手上(tmp_path):
    home = tmp_path / "home"
    目 = _配用例目录(tmp_path)
    结果 = _跑(["--用例", str(目), "--详细"], home)
    assert 结果.returncode == 0, 结果.stdout + 结果.stderr
    assert "✓ 好.jsh" in 结果.stdout, "`--详细` 没透传过去：\n" + 结果.stdout


def test_跑之前先写清谁跑谁被测(tmp_path):
    """不许让人猜「这次到底是哪两版在比」—— 这三行必须在运行器输出**之前**。"""
    home = tmp_path / "home"
    目 = _配用例目录(tmp_path)
    结果 = _跑(["--用例", str(目)], home)
    行们 = 结果.stdout.splitlines()
    assert 行们[0].startswith("自测 · 运行者："), 行们[:3]
    assert 行们[1].lstrip().startswith("被测："), 行们[:3]
    assert 行们[2].lstrip().startswith("运行器："), 行们[:3]
    assert 结果.stdout.index("== 自测（基石自己写的黑盒运行器）==") > 0


# ---------------------------------------------------------------------------
# 两宿主：同一条命令、同一份配置 ⇒ 输出逐字一致
# ---------------------------------------------------------------------------

def test_两宿主逐字一致(tmp_path):
    """`自测` 是开发工具，但它照样有两个实现 —— 两边说法必须一样。

    ⚠️ 比的时候要**去掉「用时」**（那是墙上时间，本来就会差几毫秒）。
    """
    if not RUST.is_file():
        pytest.skip(f"没构建 {RUST.name}（cd rust && cargo build --release）")
    目 = _配用例目录(tmp_path)
    参数 = ["--固定版", PY, "-m", "oracle.jishi.cli",
            "--被测", PY, "-m", "oracle.jishi.cli",
            "--用例", str(目)]
    左 = _跑(参数, tmp_path / "home_py", 宿="python")
    右 = _跑(参数, tmp_path / "home_rs", 宿="rust")
    assert 左.returncode == 右.returncode == 0, 左.stdout + 左.stderr + 右.stdout + 右.stderr
    assert _去掉用时(左.stdout) == _去掉用时(右.stdout), (
        "两宿主的 stdout 不一致\n--- python ---\n" + 左.stdout + "\n--- rust ---\n" + 右.stdout)
    assert 左.stderr == 右.stderr

    # 配置文件也得一样（同一份格式，谁写都该长一样）
    左边配置 = (tmp_path / "home_py" / 运行者文件).read_text(encoding="utf-8")
    右边配置 = (tmp_path / "home_rs" / 运行者文件).read_text(encoding="utf-8")
    assert 左边配置 == 右边配置, f"{左边配置!r} != {右边配置!r}"


def test_两宿主的_看_也一致(tmp_path):
    if not RUST.is_file():
        pytest.skip(f"没构建 {RUST.name}（cd rust && cargo build --release）")
    目 = _配用例目录(tmp_path)
    # 先让两边都记上**同样的**固定版和被测 —— 否则「没记着」的那一行会各自报
    # 「当前这个 jishi」，而两个宿主的「当前」本来就不是同一个文件（那是**对的**）。
    for 宿, 子 in (("python", "home_py"), ("rust", "home_rs")):
        _跑(["--固定版", PY, "-m", "oracle.jishi.cli",
             "--被测", PY, "-m", "oracle.jishi.cli", "--用例", str(目)],
            tmp_path / 子, 宿=宿)
    左 = _跑(["--看"], tmp_path / "home_py", 宿="python")
    右 = _跑(["--看"], tmp_path / "home_rs", 宿="rust")
    assert 左.stdout == 右.stdout, "--- python ---\n" + 左.stdout + "--- rust ---\n" + 右.stdout
