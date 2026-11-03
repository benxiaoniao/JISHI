# -*- coding: utf-8 -*-
"""M135 撞出来的**宿主分叉**：程序先 `打印`、最后 `抛出` 时，报告和错误谁在前。

## 撞见的现场

用基石写的测试运行器（`tools/自测.jsh`）跑完一批、末尾 `抛出` 表示失败：

```
$ jishi tools/自测.jsh ... > 日志 2>&1
错误 E2009：异常（自测失败：2 条没过）    ← 先出来了
== 自测（基石自己写的黑盒运行器）==        ← 报告反而在后面
```

原因：**Python 宿主的 stdout 在重定向 / 管道时是块缓冲**，
错误走 stderr 立刻出去，报告还躺在缓冲里，进程退出才刷。
而 **Rust / Node 宿主顺序是正的**。

⇒ **同一份程序在两个宿主上输出顺序不同 = 语义分叉**，不是「显示问题」：
任何「先打印进度再报错」的程序（测试运行器、批处理、安装进度）都会看见它。

修在 `oracle/jishi/cli.py` 的 `_装好错误流`：给 stderr 套一个代理，
**每次往 stderr 写字之前先冲 stdout**。一处覆盖全部写点（含标准库提示与 traceback）。

## 🔴 判据必须**合并两条流**才测得出顺序

这一点我自己踩过：先用 `capture_output=True` 分别拿 stdout / stderr 再 `+` 起来，
**那样永远测不出顺序** —— 拼接是「所有 stdout + 所有 stderr」，交错信息早丢了。
实测：把修复临时撤掉再跑，那种写法的两条**照样绿**（只有「重定向到文件」那条红了）。
⇒ 必须 `stdout=PIPE, stderr=STDOUT`（**写同一个管道**），拿到的顺序才是真的。

## 判据

1. 合并两条流时，**报告在错误前面**；
2. **两个宿主**都要满足（否则又是分叉）；
3. 报告**一个字都不能少**（缓冲丢内容比顺序更糟 —— 那正是 M127 修过的病）。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
RUST = ROOT / "rust" / "target" / "release" / "jishi-rs.exe"

#: 典型的「先打印一堆，再报错」程序 —— 测试运行器就是这个形状。
程序 = """导入 系统

打印("第一行报告")
打印("第二行报告")
抛出 "收尾时发现失败了"
"""


def _合并跑(命令, 源):
    """两条流**写进同一个管道**，于是拿到的就是「用户眼前那个顺序」。"""
    return subprocess.run(命令 + [str(源)], cwd=str(ROOT), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                          errors="replace", timeout=120)


def _写源(tmp_path, 名):
    源 = tmp_path / f"{os.getpid()}_{名}.jsh"
    源.write_text(程序, encoding="utf-8", newline="\n")
    return 源


def _查顺序(输出: str, 谁: str):
    assert "第一行报告" in 输出, f"{谁}：stdout 的内容丢了（缓冲没刷）\n{输出}"
    assert "第二行报告" in 输出, f"{谁}：第二行报告丢了\n{输出}"
    错误位置 = 输出.find("收尾时发现失败了")
    assert 错误位置 != -1, f"{谁}：连错误本身都没有\n{输出}"
    assert 输出.index("第二行报告") < 错误位置, (
        f"{谁}：**错误横幅跑到了报告前面**（stdout 没在写 stderr 之前刷新）\n"
        f"实际顺序：\n{输出}")


def test_先打印再报错_顺序正确(tmp_path):
    """合并两条流（用户真正看到的样子）时，报告在前、错误在后。"""
    r = _合并跑([PY, "-m", "oracle.jishi.cli"], _写源(tmp_path, "单"))
    assert r.returncode == 1, "抛出了异常就该退 1"
    _查顺序(r.stdout, "python 宿主")


def test_两个宿主顺序一致(tmp_path):
    """同一份程序，Rust 宿主也不能把错误放到报告前面（否则又分叉）。"""
    源 = _写源(tmp_path, "双")
    _查顺序(_合并跑([PY, "-m", "oracle.jishi.cli"], 源).stdout, "python 宿主")
    if not RUST.is_file():
        pytest.skip("没构建 jishi-rs（cd rust && cargo build --release）")
    _查顺序(_合并跑([str(RUST)], 源).stdout, "rust 宿主")


def test_重定向到文件也不丢(tmp_path):
    """`> 日志 2>&1` 是最常见的用法 —— 那里 stdout 一定是块缓冲。"""
    源 = _写源(tmp_path, "文件")
    日志 = tmp_path / f"{os.getpid()}_日志.txt"
    with open(日志, "w", encoding="utf-8", newline="\n") as f:
        subprocess.run([PY, "-m", "oracle.jishi.cli", str(源)], cwd=str(ROOT),
                       stdout=f, stderr=subprocess.STDOUT, timeout=120)
    _查顺序(日志.read_text(encoding="utf-8"), "重定向到文件")
