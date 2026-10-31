# -*- coding: utf-8 -*-
"""M153 · 数字要按人想的样子写出来（四舍五入改真「五入」+ 三个格式化函数）。

## 一、`数学.四舍五入` 的口径变了（**破坏性变更**）

以前它直接调 Python 的 `round`：`四舍五入(2.5)` 给 **2**（银行家舍入）、
`四舍五入(1.005, 2)` 给 **1.0**（1.005 的二进制其实是 1.00499999…）。
用户说「四舍五入」时想的是「五就进一位、按我写下的那个十进制数算」——
金额场景（D90）更是必须这样。

现在一律**在十进制字符串上舍**、半入远离零：`2.5`→3、`-2.5`→-3、
`1.005` 舍 2 位→1.01、`2.675` 舍 2 位→2.68。

⚠️ **为什么非要在字符串上做**：三个宿主各用一套浮点舍入的话，`2.675`
这种边界必然各说各话；而「先转十进制字符串、再按位取舍」是唯一能让
Python / Rust / Node 逐字一致的做法。

## 二、新增三个「数字 → 文本」

`文本.小数位(值, 位=2)` / `文本.百分比(比例, 位=0)` / `文本.千分位(值)` ——
把「金额、占比、大数字」三件日常事从 `格式化("{:.2f}", x)` 那个暗号里解放出来（D90）。
"""

import io
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path, 去掉遮蔽提示          # noqa: E402

CVM驱动 = '''\
import sys
sys.path.insert(0, r"{root}")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(newline="\\n")
    except Exception:
        pass
from oracle.jishi import cvm_bind, errors as E

src = open("程序.jsh", encoding="utf-8").read()
try:
    cvm_bind.run_source_c(src, filename="程序.jsh")
except E.JishiError as e:
    e.with_source(src.replace("\\r\\n", "\\n").split("\\n"))
    sys.stderr.write(e.render() + "\\n")
    sys.exit(1)
'''


def _五份(源码: str, tmp_path: Path) -> dict:
    from oracle.jishi import serialize
    from oracle.jishi.compiler import compile_source

    (tmp_path / "程序.jsh").write_text(源码, encoding="utf-8", newline="\n")
    (tmp_path / "驱动.py").write_text(
        CVM驱动.format(root=str(ROOT).replace("\\", "\\\\")), encoding="utf-8")
    表 = {
        "树遍历": [sys.executable, "-m", "oracle.jishi.cli", "--tree", "程序.jsh"],
        "Python VM": [sys.executable, "-m", "oracle.jishi.cli", "程序.jsh"],
        "C VM": [sys.executable, "驱动.py"],
    }
    rs = rust_exe_path()
    if rs.is_file():
        表["Rust 宿主"] = [str(rs), "程序.jsh"]
    node = shutil.which("node")
    if node:
        (tmp_path / "程序.json").write_text(
            serialize.dumps(compile_source(源码, "程序.jsh")), encoding="utf-8")
        表["Node 宿主"] = [node, str(ROOT / "oracle" / "node" / "index.js"),
                           "程序.json"]
    出 = {}
    for 名字, 命令 in 表.items():
        r = subprocess.run(命令, cwd=str(tmp_path), capture_output=True, timeout=60)
        出[名字] = 去掉遮蔽提示((r.stdout + r.stderr).decode("utf-8", "replace"))
    return 出


def _一致(源码: str, tmp_path: Path, 期望: str) -> None:
    出 = _五份(源码, tmp_path)
    值 = set(出.values())
    assert len(值) == 1, "五个执行器说法不一致：\n" + "\n".join(
        f"—— {k} ——\n{v}" for k, v in 出.items())
    assert 值.pop().strip() == 期望, f"结果不对：{源码!r}"


@pytest.mark.parametrize("表达式,期望", [
    ("数学.四舍五入(2.5)", "3"),
    ("数学.四舍五入(-2.5)", "-3"),
    ("数学.四舍五入(1.5)", "2"),
    ("数学.四舍五入(0.5)", "1"),
    ("数学.四舍五入(-0.4)", "0"),
    ("数学.四舍五入(1.005, 2)", "1.01"),
    ("数学.四舍五入(2.675, 2)", "2.68"),
    ("数学.四舍五入(9.99, 1)", "10.0"),
    ("数学.四舍五入(1234, -2)", "1200"),
    ("数学.四舍五入(1250, -2)", "1300"),
    ("数学.四舍五入(3.14159, 20)", "3.14159"),
], ids=["2.5", "-2.5", "1.5", "0.5", "-0.4", "1.005", "2.675", "9.99",
        "整百", "1250整百", "位数多于有效位"])
def test_四舍五入按十进制写法半入(表达式, 期望, tmp_path):
    """**这是破坏性变更的判据**：以前 `2.5` 给 2、`1.005` 舍 2 位给 1.0。"""
    _一致(f"导入 数学\n打印({表达式})\n", tmp_path, 期望)


@pytest.mark.parametrize("表达式,期望", [
    ("文本.小数位(3.14159, 2)", "3.14"),
    ("文本.小数位(3.1, 2)", "3.10"),          # 位数不够要补零（金额场景）
    ("文本.小数位(2.5, 0)", "3"),             # 与四舍五入同一口径
    ("文本.小数位(2)", "2.00"),               # 整数也补两位
    ("文本.百分比(0.2564, 1)", "25.6%"),
    ("文本.百分比(0.256)", "26%"),
    ("文本.百分比(0.07)", "7%"),              # 浮点乘 100 会多出 000000000001
    ("文本.百分比(-0.005, 1)", "-0.5%"),
    ("文本.千分位(1234567)", "1,234,567"),
    ("文本.千分位(1234567.891)", "1,234,567.891"),
    ("文本.千分位(-9876543)", "-9,876,543"),
    ("文本.千分位(999)", "999"),
    ("文本.千分位(0)", "0"),
], ids=["小数位2", "小数位补零", "小数位0", "整数补零", "百分比1位", "百分比0位",
        "百分比浮点", "百分比负", "千分位整数", "千分位小数", "千分位负",
        "千分位三位", "千分位零"])
def test_三个格式化函数(表达式, 期望, tmp_path):
    _一致(f"导入 文本\n打印({表达式})\n", tmp_path, 期望)


def test_四舍五入的位数不能是负数以外的东西(tmp_path):
    """`位数` 得是整数；给文本要报类型错误（别静默当 0）。"""
    出 = _五份('导入 数学\n打印(数学.四舍五入(2.5, "两位"))\n', tmp_path)
    文本 = next(iter(出.values()))
    assert "错误" in 文本 and "类型" in 文本, 文本


def test_小数位的位不能是负数(tmp_path):
    出 = _五份('导入 文本\n打印(文本.小数位(1.234, -1))\n', tmp_path)
    文本 = next(iter(出.values()))
    assert "不能是负数" in 文本, 文本
