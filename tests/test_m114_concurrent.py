# -*- coding: utf-8 -*-
"""M114：`并发` —— 把**一批互相独立的 IO** 同时做（结果顺序与输入一致）。

语料（`tests/cases/33_标准库_M114.jsh` + 三条 `error_cases`）管「五引擎逐字节一致」；
这一份管**语料管不到**的那些点：

1. **顺序是硬保证**（M114 的立身之本）：换输入顺序 ⇒ 结果跟着换、
   **多轮 / 多线程数跑出来的结果逐项相同** —— 这是「并发不影响可复现性」的证据，
   也是五引擎还能对拍的前提；
2. **它确实是并发的**（不是顺序跑的假包装）：小文件批量上真有加速
   —— ⚠️ 这是**墙上时间**判据，打 `计时`（CI 不跑，只在本机串行跑，见 AGENTS §1）；
3. **大文件上它会更慢** —— 把台账那条反例钉成判据（别人别拿它去泡大文件）；
4. **报错可操作**，且**校验顺序固定**（算法 → 线程数 → 列表）；
5. **语言事实同步**（`--lang-spec` 里有 `并发` 与这三个函数）。

⚠️ 纪律：临时文件一律在 `tmp_path` 里；不在仓库根读写真实文件。
"""

import io
import os
import subprocess
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oracle.jishi import cvm_bind                 # noqa: E402
from oracle.jishi import vm as vm_mod             # noqa: E402
from oracle.jishi.compiler import compile_source  # noqa: E402
from oracle.jishi.errors import JishiError        # noqa: E402
from oracle.jishi.interpreter import run_source   # noqa: E402


def _树(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        run_source(src, filename="<t>")
    return buf.getvalue()


def _pvm(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        vm_mod.VM(compile_source(src, "<t>"), "<t>").run()
    return buf.getvalue()


def _cvm(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        cvm_bind.run_source_c(src, "<t>")
    return buf.getvalue()


def _三一致(src: str) -> str:
    got = {"树遍历": _树(src), "Python VM": _pvm(src), "C VM": _cvm(src)}
    assert len(set(got.values())) == 1, f"三执行器输出不一致：{got}"
    return next(iter(got.values()))


def _在(tmp: Path, src: str) -> str:
    """在 `tmp` 里跑一段基石源码（临时文件都落在那儿）。"""
    buf = io.StringIO()
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        with redirect_stdout(buf):
            run_source(src, filename="<t>")
    finally:
        os.chdir(cwd)
    return buf.getvalue()


#: 造一批小文件（默认 8 个，各 4 KB）—— 顺序与内容都是确定的。
def _造样本(tmp: Path, 个数: int = 8, 每份: int = 4096) -> list:
    路径们 = []
    for i in range(个数):
        p = tmp / f"样本{i}.bin"
        p.write_bytes(bytes([(i + j) % 256 for j in range(每份)]))
        路径们.append(p.name)
    return 路径们


# ---------------------------------------------------------------------------
# 一、顺序是硬保证
# ---------------------------------------------------------------------------

def test_结果与输入一一对应(tmp_path):
    _造样本(tmp_path, 8)
    出 = _在(tmp_path, (
        "导入 并发\n导入 文本\n"
        "令 路径们 = [文本.格式化(\"样本{}.bin\", i) 遍历 i 在 范围(8)]\n"
        "打印(并发.批量摘要(路径们) == 并发.批量摘要(路径们))\n"
        "打印(并发.批量读(路径们)[3] == 并发.批量读([\"样本3.bin\"])[0])\n"
        "打印(并发.批量摘要([\"样本3.bin\"])[0] == 并发.批量摘要(路径们)[3])\n"
    ))
    assert 出 == "真\n真\n真\n"


def test_多轮多线程数结果逐项相同(tmp_path):
    """**这是「并发不影响可复现性」的直接证据**：同一份输入，
    1 / 2 / 4 / 8 路 + 跑三轮，结果必须一模一样（否则谁先跑完就影响输出了）。
    """
    _造样本(tmp_path, 12)
    出 = _在(tmp_path, (
        "导入 并发\n导入 文本\n"
        "令 路径们 = [文本.格式化(\"样本{}.bin\", i) 遍历 i 在 范围(12)]\n"
        "令 甲 = 并发.批量摘要(路径们, \"sha256\", 1)\n"
        "打印(并发.批量摘要(路径们, \"sha256\", 2) == 甲)\n"
        "打印(并发.批量摘要(路径们, \"sha256\", 4) == 甲)\n"
        "打印(并发.批量摘要(路径们, \"sha256\", 8) == 甲)\n"
        "打印(并发.批量摘要(路径们) == 甲)\n"
        "打印(并发.批量摘要(路径们) == 甲)\n"
    ))
    assert 出 == "真\n真\n真\n真\n真\n"


def test_与一个个来逐项相同(tmp_path):
    """并发**不改语义**：每一格都等于「拿它单独调 `加密.文件摘要`」。"""
    _造样本(tmp_path, 6)
    assert _在(tmp_path, (
        "导入 并发\n导入 加密\n导入 文件\n导入 文本\n"
        "令 路径们 = [文本.格式化(\"样本{}.bin\", i) 遍历 i 在 范围(6)]\n"
        "令 逐个 = [加密.文件摘要(p) 遍历 p 在 路径们]\n"
        "打印(并发.批量摘要(路径们) == 逐个)\n"
        "打印(并发.批量读(路径们)[2] == 文件.读字节(\"样本2.bin\"))\n"
    )) == "真\n真\n"


def test_集合不收(tmp_path):
    """集合没有「顺序」可言，而本模块**保证顺序** —— 所以明确不收它。"""
    造 = "导入 并发\n"
    with pytest.raises(JishiError) as ei:
        _树(造 + '打印(并发.批量摘要(集合(["a", "b"])))\n')
    assert "要给一个列表" in str(ei.value)


def test_空列表给空列表():
    assert _三一致(
        '导入 并发\n打印(并发.批量摘要([]), 并发.批量读([]))\n'
    ) == "[] []\n"


def test_线程数不吃掉空列表的校验():
    """空列表也要**先验算法**：不然 `批量摘要([], "xx")` 会静默过。"""
    with pytest.raises(JishiError) as ei:
        _树('导入 并发\n打印(并发.批量摘要([], "xx"))\n')
    assert "不支持的摘要算法" in str(ei.value)


# ---------------------------------------------------------------------------
# 二、报错可操作 + 校验顺序固定（算法 → 线程数 → 列表）
# ---------------------------------------------------------------------------

def test_第一个参数要给列表():
    with pytest.raises(JishiError) as ei:
        _树('导入 并发\n打印(并发.批量摘要("甲.txt"))\n')
    assert "要给一个列表，不能直接给文本" in str(ei.value)


def test_线程数必须是正整数():
    for 坏, 说法 in (("0", "现在是 0"), ("-3", "现在是 -3"), ('"四"', "得到了「文本」")):
        with pytest.raises(JishiError) as ei:
            _树(f'导入 并发\n打印(并发.批量摘要([], "sha256", {坏}))\n')
        assert 说法 in str(ei.value)


def test_校验顺序固定():
    """算法 → 线程数 → 列表。三个宿主都要**同一个顺序**，
    否则「两处都不合法」时报的不是同一句（五引擎对拍的隐性前提）。
    """
    with pytest.raises(JishiError) as ei:
        _树('导入 并发\n打印(并发.批量摘要(1, "xx", 0))\n')
    assert "不支持的摘要算法" in str(ei.value)      # 算法先报，不是列表 / 线程数


def test_线程数上限有护栏(tmp_path):
    """写个天文数字不许把机器打死 —— 收到上限照样把活干完。"""
    _造样本(tmp_path, 2, 16)
    出 = _在(tmp_path, (
        "导入 并发\n"
        "打印(并发.批量摘要([\"样本0.bin\"], \"sha256\", 100000))\n"
        "打印(并发.批量读([\"样本0.bin\", \"样本1.bin\"], 999)[0] == "
        "并发.批量读([\"样本0.bin\"])[0])\n"
    ))
    assert 出.splitlines()[1] == "真"


# ---------------------------------------------------------------------------
# 三、语言事实同步（`--lang-spec`）
# ---------------------------------------------------------------------------

def test_lang_spec里有并发模块():
    r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", "--lang-spec"],
                       capture_output=True, text=True, encoding="utf-8",
                       cwd=ROOT, timeout=120)
    assert r.returncode == 0, r.stderr[:300]
    import json
    d = json.loads(r.stdout)
    模块们 = {m["module"]: [f["name"] for f in m["functions"]] for m in d["stdlib"]}
    assert "并发" in 模块们
    assert 模块们["并发"] == ["批量摘要", "批量读", "并行取"]


# ---------------------------------------------------------------------------
# 四、它**确实**是并发的 —— 墙上时间判据（本机串行跑；CI 不跑）
# ---------------------------------------------------------------------------

def _量一次(路径们文本: str, tmp: Path, 用并发: bool) -> float:
    源码 = (
        "导入 并发\n导入 加密\n导入 时间\n导入 文本\n"
        f"令 路径们 = {路径们文本}\n"
        "令 起 = 时间.高精度时间()\n"
        + ("令 果 = 并发.批量摘要(路径们)\n" if 用并发
           else "令 果 = [加密.文件摘要(p) 遍历 p 在 路径们]\n")
        + "打印(时间.高精度时间() - 起)\n"
    )
    return float(_在(tmp, 源码).strip())


@pytest.mark.计时
def test_小文件批量确实更快(tmp_path):
    """**M114 的验收那条**：大量小 IO 上量得出加速倍数。

    ⚠️ 判据用**比值**（抗负载），并照 AGENTS §1 那条教训**交错取样 + 每档取 min**
    （先量 base 再量 small 会把后量那档抬高）。
    """
    n = 800
    _造样本(tmp_path, n, 4096)
    路径们文本 = ('[文本.格式化("样本{}.bin", i) 遍历 i 在 范围(%d)]' % (n,))
    顺序 = []
    并发 = []
    for _ in range(3):                       # 交错取样
        顺序.append(_量一次(路径们文本, tmp_path, False))
        并发.append(_量一次(路径们文本, tmp_path, True))
    s, c = min(顺序), min(并发)
    assert c < s, (
        f"并发没有更快：顺序 {s:.3f}s、并发 {c:.3f}s"
        f"（{n} 个 4 KB 文件）—— 若机器负载极高，请串行重跑这条")
    assert s / c >= 1.4, (
        f"加速倍数只有 {s / c:.2f}x（顺序 {s:.3f}s、并发 {c:.3f}s）"
        f"—— 要么机器在忙，要么 `并发` 退回了顺序执行")


@pytest.mark.计时
def test_单个文件上不白开线程(tmp_path):
    """**把台账那条反例钉住**：并发不是「加了就更快」，所以本模块对
    `n == 1` **直接短路**（不建线程池）。

    ⚠️ 这条**不断言「更慢」** —— 那太依赖机器（换个平台上线程可能很便宜，
    断言就会假红）。它断言的是**没有明显额外开销**，证明那条短路真的在。
    """
    (tmp_path / "大.bin").write_bytes(b"\x00" * (40 * 1024 * 1024))
    顺序 = []
    并发 = []
    for _ in range(3):
        顺序.append(_量一次('["大.bin"]', tmp_path, False))
        并发.append(_量一次('["大.bin"]', tmp_path, True))
    assert min(并发) <= min(顺序) * 2 + 0.15, (
        f"单文件上不该有明显额外开销：顺序 {min(顺序):.3f}s、并发 {min(并发):.3f}s")

