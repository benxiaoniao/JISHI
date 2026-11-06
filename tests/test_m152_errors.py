# -*- coding: utf-8 -*-
"""M152 · 报错要教人改（关键字当名字 / 模块当函数 / 格式化个数不符）。

三条都是**「程序没跑成，但报错没指到点子上」**，来自真实项目：

1. `令 类 = 1` —— 「类」是关键字。以前只说「这里需要一个变量名」，
   用户盯着那一行看不出「类」这个词本身不能用（它长得太像一个普通词）。
2. `导入 文本` 之后写 `文本(123)` —— 那是**模块**，不是函数。以前 Rust 只说
   「「<模块 文本>」不能调用」（`<模块 …>` 是什么？），Node 报「类型错误」，
   Python 报「找不到这个函数」—— 三个宿主三个码、三句话。
3. `文本.格式化("{}", 1, 2)` / `("{}} 和 {}", 1)` —— 占位符与参数个数不符。
   以前**一声不响**：多给直接丢、少给填空串（而且三个宿主填的还不一样：
   Python 抛英文「索引越界」、Rust 填空、Node 填「空」）。

## 判据口径

每条都要求**五个执行器逐字相同**——这类「各宿主自己翻译」的地方最容易分叉，
而分叉的坏处是：用户按 A 机器的报错去查，在 B 机器上看到的却是另一句话。
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
    """同一份源码在五个执行器上跑，返回 {引擎名: 输出+报错}。"""
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
        # 有语法错的题编译不出来 —— 那种题 Node 本来就跑不到解析之后的阶段
        try:
            (tmp_path / "程序.json").write_text(
                serialize.dumps(compile_source(源码, "程序.jsh")), encoding="utf-8")
            表["Node 宿主"] = [node, str(ROOT / "oracle" / "node" / "index.js"),
                               "程序.json"]
        except Exception:                       # noqa: BLE001
            pass

    出 = {}
    for 名字, 命令 in 表.items():
        r = subprocess.run(命令, cwd=str(tmp_path), capture_output=True, timeout=60)
        # `导入 文本` 那条「遮蔽内建」提示按**进程**去重，同进程跑的引擎可能一次
        # 都不打、子进程跑的每次都打 —— 跨引擎比输出前必须剥掉（conftest 有说明）。
        出[名字] = 去掉遮蔽提示((r.stdout + r.stderr).decode("utf-8", "replace"))
    return 出


def _一致(出: dict) -> str:
    值 = set(出.values())
    assert len(值) == 1, "五个执行器说法不一致：\n" + "\n".join(
        f"—— {k} ——\n{v}" for k, v in 出.items())
    return 值.pop()


@pytest.mark.parametrize("源码", [
    '令 类 = 1\n',
    '函数 类()：\n    返回 1\n',
    '函数 甲(类)：\n    返回 1\n',
], ids=["变量名", "函数名", "参数名"])
def test_关键字当名字要点名(源码, tmp_path):
    """**必须说出「哪个词」不能用**，并给一条能照做的建议。"""
    文本 = _一致(_五份(源码, tmp_path))
    assert "关键字" in 文本, f"没说这是关键字：{文本!r}"
    assert "类" in 文本, f"没点出是哪个词：{文本!r}"
    assert "换个说法" in 文本, f"没给能照做的建议：{文本!r}"


def test_模块当函数调要说清怎么办(tmp_path):
    """`导入 文本` 之后 `文本(123)`：得告诉他「模块要接 .函数名」和「别名导入」。"""
    文本 = _一致(_五份('导入 文本\n打印(文本(123))\n', tmp_path))
    assert "E0302" in 文本, f"错误码不对（三宿主原来分别是 E0302/E2006/E2002）：{文本!r}"
    assert "模块「文本」不能直接调用" in 文本, 文本
    assert "文本.函数名" in 文本, f"没告诉正确写法：{文本!r}"
    assert "别名导入" in 文本, f"没提醒别名这条路（`导入 文本 为 文本库`）：{文本!r}"


@pytest.mark.parametrize("源码,占位,给了", [
    ('导入 文本\n打印(文本.格式化("{} 和 {}", 1))\n', 2, 1),
    ('导入 文本\n打印(文本.格式化("{}", 1, 2))\n', 1, 2),
    ('导入 文本\n打印(文本.格式化("{0}", "甲", "乙"))\n', 1, 2),
], ids=["少给", "多给", "显式序号多给"])
def test_格式化个数不符要报错(源码, 占位, 给了, tmp_path):
    """以前**静默**（多给丢、少给填空串），而且三个宿主填得还不一样。"""
    文本 = _一致(_五份(源码, tmp_path))
    assert "E2005" in 文本, f"错误码不对：{文本!r}"
    assert f"{占位} 个占位符" in 文本 and f"{给了} 个参数" in 文本, \
        f"没把两个数报清楚：{文本!r}"
    assert "花括号" in 文本, f"没给修法：{文本!r}"


@pytest.mark.parametrize("源码,期望", [
    ('导入 文本\n打印(文本.格式化("{} 和 {}", 1, 2))\n', "1 和 2"),
    ('导入 文本\n打印(文本.格式化("{1}-{0}", "甲", "乙"))\n', "乙-甲"),
    ('导入 文本\n打印(文本.格式化("{:.2f}", 3.14159))\n', "3.14"),
    # ⚠️ 转义花括号：Node 宿主原来是坏的（`String.replace` 的正则跳过 `{{`，
    # 给出 `{空}`），Python / Rust 给 `{}`。M152 顺手把 Node 改成手写扫描。
    ('导入 文本\n打印(文本.格式化("{{}}"))\n', "{}"),
    ('导入 文本\n打印(文本.格式化("{{{}}}", 7))\n', "{7}"),
], ids=["两个占位", "显式序号", "说明符", "转义花括号", "转义加占位"])
def test_格式化正常用法一个字都不许变(源码, 期望, tmp_path):
    """加检查不能把好用法弄坏 —— 五种正常写法逐字相同，且等于期望。"""
    for 名字, 文本 in _五份(源码, tmp_path).items():
        assert 文本.strip() == 期望, f"{名字}：格式化结果不对 → {文本!r}"
