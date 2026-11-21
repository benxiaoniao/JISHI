# -*- coding: utf-8 -*-
"""M191 · 调用点单态化：让循环 JIT 跨过函数调用。

## 为什么是「内联」而不是「调用」

循环 JIT 跑在**宿主栈**上，机器码里没有「函数」这种值、也没法回调解释器。
所以 `s = 加一(s)` 想进 JIT 只有一条路：**把被调者的表达式抄进循环体**。

## 判据分三层

1. **数值**：开 / 关 JIT 逐值相同（多组规模 + 双参数 + 槽不够 + 不可内联）；
2. **守卫**（最关键）：`加一 = 加二` 之后，同一个循环必须**立刻**用新的函数 ——
   内联是把旧函数抄进了机器码，不核对就会**静默算错**（不报错，数字不对）；
3. **性能**：`函数调用密集` 从 1.19x 提到 **≥1.5x**（打 `计时`，进串行节）。

## 明确不做（这一版）

**类的方法调用**（`c.取()`）进不了 JIT：字节码是 `GET_ATTR + CALL`，而
`GET_ATTR` 要读堆对象，机器码的槽里只有 `i64`，没有「对象」这种表示。
`方法调用密集` 那条基准是**如实记录短板**的，不是拿来变快的 —— 见 CHANGELOG。
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import rust_exe_path                            # noqa: E402


def _跑(源: str, tmp_path: Path, 名="脚本.jsh", 开jit=True, 环境=None) -> tuple:
    脚本 = tmp_path / 名
    脚本.write_text(源, encoding="utf-8", newline="\n")
    变量 = dict(__import__("os").environ)
    if not 开jit:
        变量["JISHI_JIT"] = "0"
    if 环境:
        变量.update(环境)
    r = subprocess.run([str(rust_exe_path()), str(脚本)], capture_output=True,
                       timeout=180, cwd=str(tmp_path), env=变量)
    出 = (r.stdout + r.stderr).decode("utf-8", "replace")
    return r.returncode, 出


@pytest.fixture(scope="module", autouse=True)
def _要有宿主():
    if not rust_exe_path().is_file():
        pytest.skip("没有 Rust 宿主（cargo build --release）")


加一 = (
    "函数 加一(x)：\n"
    "    返回 x + 1\n"
)


def _循环(n=3000, 调用="加一(总和)"):
    return (
        加一
        + "令 总和 = 0\n"
        f"遍历 i 在 范围({n})：\n"
        f"    总和 = {调用}\n"
        "打印(总和)\n"
    )


def test_内联真的发生了(tmp_path):
    """调试输出里要有「内联 `加一`」——否则下面那些绿可能只是「从来没编过」。"""
    _, 出 = _跑(_循环(3000), tmp_path, 环境={"JISHI_JIT_DEBUG": "1"})
    assert "内联 `加一`" in 出, 出[-800:]


def test_逐值一致(tmp_path):
    """多组规模，开 / 关 JIT 一字不差。

    ⚠️ `n=1` 那种到不了热度阈值（1000）—— 正好覆盖「没编也正确」。
    """
    for n in (1, 100, 999, 1000, 1001, 3000, 8000):
        源 = _循环(n)
        码1, 出1 = _跑(源, tmp_path, f"a{n}.jsh", 开jit=False)
        码2, 出2 = _跑(源, tmp_path, f"b{n}.jsh", 开jit=True)
        assert (码1, 出1) == (码2, 出2), f"n={n}：{出1!r} vs {出2!r}"


def test_双参数也能内联(tmp_path):
    """两个形参 = 两个临时槽，是「槽寄存器只有 4 个」这条边界的正路。"""
    源 = (
        "函数 合(a, b)：\n"
        "    返回 a + b * 2\n"
        "令 总和 = 0\n"
        "遍历 i 在 范围(3000)：\n"
        "    总和 = 合(总和, i)\n"
        "打印(总和)\n"
    )
    码1, 出1 = _跑(源, tmp_path, "x.jsh", 开jit=False)
    码2, 出2 = _跑(源, tmp_path, "y.jsh", 开jit=True)
    assert (码1, 出1) == (码2, 出2), f"{出1!r} vs {出2!r}"
    _, 出 = _跑(源, tmp_path, "z.jsh", 环境={"JISHI_JIT_DEBUG": "1"})
    assert "内联 `合`" in 出, 出[-800:]


def test_守卫_函数被换掉就用新的(tmp_path):
    """🔴 这一站最关键的一条。

    `加一 = 加二` 之后，**同一个循环**（同一抬头）必须立刻用 `加二`。
    内联是把 `加一` 的表达式抄进了机器码 —— 不核对就会算错，而且**不报错**。
    期望：第一次 3000（走内联），换掉后 6000（守卫拦住、退回解释器）。
    """
    源 = (
        加一
        + "函数 加二(x)：\n"
        "    返回 x + 2\n\n"
        "函数 跑(n)：\n"
        "    令 s = 0\n"
        "    遍历 i 在 范围(n)：\n"
        "        s = 加一(s)\n"
        "    返回 s\n\n"
        "打印(跑(3000))\n"
        "加一 = 加二\n"
        "打印(跑(3000))\n"
    )
    码1, 出1 = _跑(源, tmp_path, "g1.jsh", 开jit=False)
    码2, 出2 = _跑(源, tmp_path, "g2.jsh", 开jit=True)
    assert (码1, 出1) == (码2, 出2), f"守卫没生效：{出1!r} vs {出2!r}"
    assert 出2.split() == ["3000", "6000"], f"数值不对：{出2!r}"
    # 而且能看见「换了 ⇒ 走解释器」这条诊断
    _, 出3 = _跑(源, tmp_path, "g3.jsh", 环境={"JISHI_JIT_DEBUG": "1"})
    assert "内联点的函数被换掉了" in 出3, 出3[-800:]


def test_有局部变量的函数不内联但正确(tmp_path):
    """`令 y = …` 会产生 `STORE_FAST` —— 内联要求「一条 `返回 表达式`」，所以不内联。
    重点不是「快」，是**结果必须一样**（退回解释器是正确的那条路）。
    """
    源 = (
        "函数 复杂(x)：\n"
        "    令 y = 1\n"
        "    返回 x + y\n"
        "令 总和 = 0\n"
        "遍历 i 在 范围(3000)：\n"
        "    总和 = 复杂(总和)\n"
        "打印(总和)\n"
    )
    码1, 出1 = _跑(源, tmp_path, "n1.jsh", 开jit=False)
    码2, 出2 = _跑(源, tmp_path, "n2.jsh", 开jit=True)
    assert (码1, 出1) == (码2, 出2), f"{出1!r} vs {出2!r}"
    _, 出 = _跑(源, tmp_path, "n3.jsh", 环境={"JISHI_JIT_DEBUG": "1"})
    assert "内联 `复杂`" not in 出, "这个形态本来不该被内联"


def test_槽不够也正确(tmp_path):
    """4 个变量槽已占满 + 内联还要 1 个临时槽 ⇒ 超了就不编（不是编错）。"""
    源 = (
        加一
        + "令 a = 0\n令 b = 0\n令 c = 0\n令 d = 0\n"
        "遍历 i 在 范围(3000)：\n"
        "    a = a + 1\n"
        "    b = b + 1\n"
        "    c = c + 1\n"
        "    d = 加一(d)\n"
        "打印(a, b, c, d)\n"
    )
    码1, 出1 = _跑(源, tmp_path, "f1.jsh", 开jit=False)
    码2, 出2 = _跑(源, tmp_path, "f2.jsh", 开jit=True)
    assert (码1, 出1) == (码2, 出2), f"{出1!r} vs {出2!r}"


def test_函数被当值用时不编但正确(tmp_path):
    """`令 f = 加一` 是合法的（函数是值）。这时不能被当成「调用点」内联掉。"""
    源 = (
        加一
        + "令 f = 加一\n"
        "令 总和 = 0\n"
        "遍历 i 在 范围(3000)：\n"
        "    总和 = f(总和)\n"
        "打印(总和)\n"
    )
    码1, 出1 = _跑(源, tmp_path, "v1.jsh", 开jit=False)
    码2, 出2 = _跑(源, tmp_path, "v2.jsh", 开jit=True)
    assert (码1, 出1) == (码2, 出2), f"{出1!r} vs {出2!r}"
    assert 出2.strip().endswith("3000"), 出2[-200:]


@pytest.mark.计时
def test_函数调用密集比解释器快一点五倍(tmp_path):
    """数字**当场量**：同一份脚本，开 / 关 JIT 各 3 次取最小。

    0.3.3 时这个是 1.16x（循环体里有 CALL ⇒ 整段不编）。
    内联之后 2x 上下。
    """
    import time
    # ⚠️ 规模要够大：内联之后一圈只有几纳秒，8000 圈还不够进程启动的零头
    # （实测 20 ms vs 23 ms，全是启动成本）。200 万圈才量得出来。
    源 = _循环(2_000_000)
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
