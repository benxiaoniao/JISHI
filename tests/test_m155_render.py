# -*- coding: utf-8 -*-
"""M155 · 两条渲染分叉：`打印(异常)` 与「序列 × 非整数」「长度(非容器)」。

这两类都属于「**同一段程序，五个执行器说五种话**」—— 语料里没人写过，
所以五引擎对拍一直没照出来（`check_engines` 的 171 条语料里一条都没有）。
M155 把它们一条条摊平：

1. **`打印(被捕获的异常)`**：Python / Node 印「头 + 位置框 + 调用链 + 提示」，
   Rust 只印一行头。`打印(e)` 恰恰是用户排查问题的主要手段 ——
   长得不一样就等于「同一段程序三种行为」。
2. **`长度(5)` / `长度(真)`**：Rust 的文案**没有那个值**（只说「没有长度」），
   Python 把布尔印成英文 `True`（Node 印「真」）。
3. **`[1, 2] * 1.5`**：Rust 说「不支持的乘法」，其余四个说
   「序列只能乘整数，不能乘「小数」」。

## 判据口径

**五个执行器逐字相同**，而且各自的**关键片段**也要在（防止「一起变错」）。
"""

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
        r = subprocess.run(命令, cwd=str(tmp_path), capture_output=True,
                           timeout=60, input=b"")     # 空 stdin：`输入()` 才会撞结束
        出[名字] = 去掉遮蔽提示((r.stdout + r.stderr).decode("utf-8", "replace"))
    return 出


def _一致(源码: str, tmp_path: Path) -> str:
    出 = _五份(源码, tmp_path)
    值 = set(出.values())
    assert len(值) == 1, "五个执行器说法不一致：\n" + "\n".join(
        f"—— {k} ——\n{v}" for k, v in 出.items())
    return 值.pop()


# 一个「在函数里抛、在外层捕获」的程序 —— 打印时应该看到调用链
函数里抛 = ('函数 甲()：\n'
          '    令 x = 1 / 0\n'
          '\n'
          '尝试：\n'
          '    甲()\n'
          '捕获 异常 为 e：\n'
          '    打印(e)\n')


def test_打印被捕获的异常带位置框与调用链(tmp_path):
    文本 = _一致(函数里抛, tmp_path)
    assert 文本.startswith("错误 E2001：不能除以零"), 文本
    assert "┌─ 程序.jsh:2:13" in 文本, f"位置框没了：{文本!r}"
    assert "调用链（从外到内）：" in 文本, f"调用链没了：{文本!r}"
    assert "甲 ← 第 5 行" in 文本, 文本
    # 被捕获的异常**没有源码行**（源码行只在 CLI 出口补，三边一致）
    assert "令 x = 1 / 0" not in 文本, "不该有源码行"


def test_打印被捕获的异常带提示(tmp_path):
    """`输入结束` 带 hint —— 宿主以前连「提示」这一行都没有。"""
    文本 = _一致('尝试：\n    输入("> ")\n捕获 异常 为 e：\n    打印(e)\n', tmp_path)
    assert "错误 E2010：输入结束" in 文本, 文本
    assert "提示：交互程序要用" in 文本, f"提示没了：{文本!r}"


def test_打印被捕获的异常带起因链(tmp_path):
    """`捕获` 里又抛 —— `打印(外层e)` 要摊出「起因：」。"""
    文本 = _一致(
        '尝试：\n'
        '    尝试：\n'
        '        令 x = 1 / 0\n'
        # ⚠️ 必须用 `捕获 异常`（除零是 **除零错误**，不是值错误）——
        # 写成 `捕获 值错误` 的话内层根本接不住，整段只会印最里面那个错。
        '    捕获 异常 为 内：\n'
        '        抛出 运行期错误("外面这层")\n'
        '捕获 异常 为 e：\n'
        '    打印(e)\n', tmp_path)
    assert "起因：" in 文本, f"起因链没了：{文本!r}"
    assert "不能除以零" in 文本, 文本


def test_容器里的异常(tmp_path):
    """嵌套显示（`打印([1, e])`）走的是另一条路径，也得同形。"""
    文本 = _一致('尝试：\n    令 x = 1 / 0\n捕获 异常 为 e：\n    打印([1, e])\n',
                tmp_path)
    assert "错误 E2001：不能除以零" in 文本, 文本


@pytest.mark.parametrize("源码,片段", [
    ("打印(长度(5))", "「5」没有长度"),
    ("打印(长度(真))", "「真」没有长度"),      # 以前 Python 印「True」
    ("打印([1, 2] * 1.5)", "序列只能乘整数，不能乘「小数」"),
    ('打印("ab" * 1.5)', "序列只能乘整数，不能乘「小数」"),
    ("打印(长度(空))", "「空」没有长度"),
], ids=["长度整数", "长度布尔", "列表乘小数", "文本乘小数", "长度空"])
def test_报错文案带上那个值(源码, 片段, tmp_path):
    文本 = _一致(源码 + "\n", tmp_path)
    assert 片段 in 文本, f"文案不对：{文本!r}"
