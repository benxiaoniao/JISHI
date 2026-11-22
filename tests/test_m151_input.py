# -*- coding: utf-8 -*-
"""M151 · `输入()` 遇到坏输入时，得说人话。

三件事，都是 Windows 上真撞到的（一台干净机器写命令行计算器时）：

1. **输入结束**（Ctrl+Z / `Ctrl+D` / 管道读完）：以前只给一句「输入已经结束」，
   用户完全不知道「该怎么处理」。现在带**修法**（用 `尝试` + `捕获 异常` 包住循环）。
2. **行首 BOM**：PowerShell 5.1 通过管道喂中文时会带一个 `U+FEFF`。它不可见，
   却**参与字符串比较** —— `三加五` 其实读成 `\\ufeff三加五`，于是
   `如果 行 == "三加五"` 静默不成立、`整数(行)` 直接报错。这是最阴的一条：
   没有报错、只是结果不对。
3. **不是 UTF-8**（Windows 控制台默认 GBK/936）：以前三种宿主三个说法 ——
   Python「数值不对（'utf-8' codec can't decode…）」、Rust「输入结束」、
   Node 干脆变成一堆 `�`。用户一个都看不懂。

## 判据口径

**五个执行器必须逐字相同**（同一份坏输入、同一个程序）。这类「宿主各自翻译
底层异常」的地方最容易分叉，而分叉的坏处是：用户按一台机器的报错去查，
在另一台上看到的却是另一句话。
"""

import io
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                       # noqa: E402

#: 只读一行然后把结果打出来。**行首带 `> ` 提示语**——它算输出（M150 对齐过）。
读一行 = '令 行 = 输入("> ")\n打印("读到：", 行)\n'

#: 判断 BOM 有没有被去掉：比字符串 + 比长度（长度不对也能看出来）。
判BOM = ('令 行 = 输入("> ")\n'
         '打印(行 == "三加五")\n'
         '打印(长度(行))\n')

#: 报错被 `捕获` 之后，类型与消息能读出来吗（用户要能自己处理）。
捕获看 = ('尝试：\n'
          '    输入("> ")\n'
          '捕获 异常 为 e：\n'
          '    打印("类型", e.类型)\n'
          '    打印("消息", e.消息)\n')

BOM输入 = "\ufeff三加五\n".encode("utf-8")
正常输入 = "三加五\n".encode("utf-8")
GBK输入 = "三加五\n".encode("gbk")          # 0xC8 0xFD 0xBC 0xD3 0xCE 0xE5

CVM驱动 = '''\
import sys
sys.path.insert(0, r"{root}")
# 输出必须与 CLI 一样不做换行转换：Windows 的文本模式会把换行写成 CRLF，
# 而 Python CLI 在 _setup_io 里已经 newline 关掉了。驱动脚本不关的话，
# 同一份报错在 C VM 上就比别的引擎多一个 CR（对拍会红，但那不是引擎
# 的问题，是**测量工具**的问题）。
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


def _写程序(tmp_path: Path, 源码: str) -> None:
    (tmp_path / "程序.jsh").write_text(源码, encoding="utf-8", newline="\n")


def _字节码(tmp_path: Path, 源码: str) -> str:
    from oracle.jishi import serialize
    from oracle.jishi.compiler import compile_source

    (tmp_path / "程序.json").write_text(
        serialize.dumps(compile_source(源码, "程序.jsh")), encoding="utf-8")
    return "程序.json"


def _执行器们(tmp_path: Path, 源码: str) -> dict:
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
        表["Node 宿主"] = [node, str(ROOT / "oracle" / "node" / "index.js"),
                           _字节码(tmp_path, 源码)]
    return 表


def _跑(名字: str, 命令: list, 输入: bytes, tmp_path: Path) -> str:
    r = subprocess.run(命令, input=输入, cwd=str(tmp_path),
                       capture_output=True, timeout=60)
    return (r.stdout + r.stderr).decode("utf-8", "replace")


def _五份(源码: str, 输入: bytes, tmp_path: Path) -> dict:
    _写程序(tmp_path, 源码)
    (tmp_path / "驱动.py").write_text(
        CVM驱动.format(root=str(ROOT).replace("\\", "\\\\")), encoding="utf-8")
    出 = {}
    for 名字, 命令 in _执行器们(tmp_path, 源码).items():
        出[名字] = _跑(名字, 命令, 输入, tmp_path)
    return 出


@pytest.mark.parametrize("case", ["BOM", "GBK", "EOF"])
def test_五个执行器说法逐字一致(case, tmp_path):
    """同一份坏输入，五个执行器必须**一个字都不差**。"""
    输入 = {"BOM": BOM输入, "GBK": GBK输入, "EOF": b""}[case]
    出 = _五份(读一行, 输入, tmp_path)
    值 = set(出.values())
    assert len(值) == 1, "五个执行器说法不一致：\n" + "\n".join(
        f"—— {k} ——\n{v}" for k, v in 出.items())


def test_行首BOM会被去掉(tmp_path):
    """`\\ufeff三加五` 必须当成 `三加五`（否则比较静默不成立、长度也不对）。"""
    出 = _五份(判BOM, BOM输入, tmp_path)
    for 名字, 文本 in 出.items():
        # 期望正好三行：提示语 + 「真」+ 长度 3（带 BOM 会是「假」和 4）
        行们 = [x for x in 文本.replace("\r\n", "\n").split("\n") if x != ""]
        assert 行们 == ["> 真", "3"], f"{名字}：BOM 没去掉 → {文本!r}"


def test_不是UTF8时说清楚怎么改(tmp_path):
    出 = _五份(读一行, GBK输入, tmp_path)
    文本 = next(iter(出.values()))
    assert "E2011" in 文本, 文本
    assert "编码错误" in 文本, 文本
    assert "UTF-8" in 文本, 文本
    # 关键：**告诉用户怎么办**（这是 M151 的全部意义）
    assert "chcp 65001" in 文本, f"没说怎么改：{文本!r}"
    assert "GBK" in 文本, f"没点出 Windows 控制台的默认代码页：{文本!r}"


def test_输入结束时说清楚怎么处理(tmp_path):
    出 = _五份(读一行, b"", tmp_path)
    文本 = next(iter(出.values()))
    assert "E2010" in 文本, 文本
    assert "输入结束" in 文本, 文本
    # 以前只有一句「输入已经结束」，用户不知道该拿它怎么办
    assert "捕获" in 文本 and "Ctrl+Z" in 文本, f"没说怎么处理：{文本!r}"


@pytest.mark.parametrize("case", ["GBK", "EOF"])
def test_两种坏输入都能被捕获(case, tmp_path):
    """用户要能自己接住：`捕获 异常 为 e` 之后读 `e.类型` / `e.消息` / `e.码`。

    ⚠️ 这两条**不进** `EXCEPTION_TYPES`（与「输入结束」同一档）—— 它们说的是
    输入流的事，不是程序里的值错了，所以没有 `捕获 编码错误` 这种写法；
    判据就按「捕获 异常 + 看 e.类型」来钉。
    """
    输入 = GBK输入 if case == "GBK" else b""
    出 = _五份(捕获看, 输入, tmp_path)
    文本 = next(iter(出.values()))
    if case == "GBK":
        assert "类型 编码错误" in 文本, f"接住的类型不对：{文本!r}"
        assert "标准输入不是合法的 UTF-8 文本" in 文本, 文本
    else:
        assert "类型 输入结束" in 文本, f"接住的类型不对：{文本!r}"
        assert "输入已经结束" in 文本, 文本
