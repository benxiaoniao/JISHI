# -*- coding: utf-8 -*-
"""M190 · 函数级 JIT（跨帧）：`斐波` 这类自递归函数整体编成机器码。

## 这一站为什么不是「循环内联」

`斐波` 一个循环都没有，而 M180–M184 的 JIT 只认「抬头 → 回跳」那一段 ——
给它加循环内联等于给没点火的发动机加油。所以改成**函数级**编译（D114）。

## 判据分三层

1. **数值**：`斐波(0..20)` 与关掉 JIT 逐值相同；`斐波(24)` 不崩；
2. **语义**：浮点参数、函数被重新赋值、`数(1500)` 这三种情况，**行为与关掉 JIT 一模一样**
   （包括最后那句「递归层数太深了」的中文报错，而不是 SIGSEGV）；
3. **性能**：`斐波` 从 0.5x 级提到 **≥1.5x**（这条打 `计时`，进串行节）。
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                            # noqa: E402

斐波 = (
    "函数 斐波(n)：\n"
    "    如果 n < 2：\n"
    "        返回 n\n"
    "    返回 斐波(n - 1) + 斐波(n - 2)\n"
)


def _跑(源: str, tmp_path: Path, 名="脚本.jsh", 开jit=True, 报错也收=True) -> tuple:
    脚本 = tmp_path / 名
    脚本.write_text(源, encoding="utf-8", newline="\n")
    环境 = dict(__import__("os").environ)
    if not 开jit:
        环境["JISHI_JIT"] = "0"
    r = subprocess.run([str(rust_exe_path()), str(脚本)], capture_output=True,
                       timeout=180, cwd=str(tmp_path), env=环境)
    出 = (r.stdout + r.stderr).decode("utf-8", "replace")
    return r.returncode, 出


@pytest.fixture(scope="module", autouse=True)
def _要有宿主():
    if not rust_exe_path().is_file():
        pytest.skip("没有 Rust 宿主（cargo build --release）")


def test_斐波逐值一致(tmp_path):
    """0–20 全跑一遍，两种模式必须一字不差。"""
    for n in (0, 1, 2, 3, 5, 8, 10, 13, 16, 18, 20):
        源 = 斐波 + f"打印(斐波({n}))\n"
        码1, 出1 = _跑(源, tmp_path, f"a{n}.jsh", 开jit=False)
        码2, 出2 = _跑(源, tmp_path, f"b{n}.jsh", 开jit=True)
        assert (码1, 出1) == (码2, 出2), f"n={n} 两种模式结果不同：{出1!r} vs {出2!r}"


def test_深一点的递归也不崩(tmp_path):
    """`斐波(24)`：机器码用自己的帧，曾经在这附近撞过栈（见 D114 的坑）。"""
    码, 出 = _跑(斐波 + "打印(斐波(24))\n", tmp_path)
    assert 码 == 0, 出[-800:]
    assert 出.strip().endswith("46368"), 出[-200:]


def test_浮点参数走解释器(tmp_path):
    """机器码只认 `i64`；喂 `2.5` 必须回到解释器，结果要对。"""
    源 = (
        "函数 累加(n)：\n"
        "    如果 n <= 0：\n"
        "        返回 0\n"
        "    返回 n + 累加(n - 1)\n"
        "打印(累加(100))\n"
        "打印(累加(2.5))\n"
    )
    _, 出 = _跑(源, tmp_path)
    assert "5050" in 出 and "4.5" in 出, 出[-400:]


def test_函数被重新赋值后仍然正确(tmp_path):
    """基石里函数是值：`斐波 = 别的` 之后，机器码里那个「调自己」的落点必须重新核对。"""
    源 = (
        "函数 别的(n)：\n"
        "    返回 42\n\n"
        + 斐波
        + '打印(斐波(12))\n'
        "斐波 = 别的\n"
        "打印(斐波(12))\n"
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-400:]
    assert "144" in 出 and "42" in 出, 出[-400:]


def test_递归上限与解释器同一句话(tmp_path):
    """`数(1500)` 必须报「递归层数太深了」，**不能**是 SIGSEGV。

    机器码的递归不建解释器帧，所以它自己数深度（与 `MAX_FRAMES` 同一个上限）；
    另外「已经很深了就别进机器码」这条闸也在这儿 —— 解释器一帧很大，
    1000 层时原生栈本来就用得差不多了。
    """
    源 = (
        "函数 数(n)：\n"
        "    如果 n <= 0：\n"
        "        返回 0\n"
        "    返回 1 + 数(n - 1)\n"
        "打印(数(1500))\n"
    )
    码1, 出1 = _跑(源, tmp_path, "j1.jsh", 开jit=False)
    码2, 出2 = _跑(源, tmp_path, "j2.jsh", 开jit=True)
    assert "递归层数太深" in 出1, 出1[-300:]
    assert "递归层数太深" in 出2, 出2[-300:]
    assert 码1 == 码2, f"退码不同：{码1} vs {码2}"


def test_真的编上了(tmp_path):
    """`JISHI_JIT_DEBUG` 里要出现「编好了」——否则上面那些绿可能只是「从来没编过」。"""
    import os
    脚本 = tmp_path / "编.jsh"
    脚本.write_text(斐波 + "打印(斐波(16))\n", encoding="utf-8", newline="\n")
    r = subprocess.run([str(rust_exe_path()), str(脚本)], capture_output=True,
                       timeout=120, env={**os.environ, "JISHI_JIT_DEBUG": "1"})
    出 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert "编好了" in 出, 出[-600:]


@pytest.mark.计时
def test_递归比解释器快一倍以上(tmp_path):
    """数字**当场量**：同一份脚本、同一台机器，开 / 关 JIT 各跑 3 次取最小。

    门槛 1.5x：0.3.3 时这个是 0.53x（比 CPython 慢一倍），函数级 JIT 之后 2x 上下。
    """
    import time
    源 = 斐波 + "打印(斐波(22))\n"
    脚本 = tmp_path / "快.jsh"
    脚本.write_text(源, encoding="utf-8", newline="\n")

    def 量(开: bool) -> float:
        次 = []
        for _ in range(3):
            t = time.perf_counter()
            r = subprocess.run([str(rust_exe_path()), str(脚本)], capture_output=True,
                               timeout=180,
                               env={} if 开 else {"JISHI_JIT": "0", "PATH": ""})
            assert r.returncode == 0
            次.append(time.perf_counter() - t)
        return min(次)

    关 = 量(False)
    开 = 量(True)
    assert 开 < 关 / 1.5, f"开 JIT {开*1000:.0f} ms / 关 {关*1000:.0f} ms —— 没到 1.5x"
