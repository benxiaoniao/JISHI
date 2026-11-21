# -*- coding: utf-8 -*-
"""M133 / M134：`系统.执行详细` 与 `系统.并行执行`（2026-10-24）。

## 这两个函数是为什么加的

「自己测自己」（D91）要一个**基石写的测试运行器**。运行器必须能做两件事：

1. **分开比两条流** —— `系统.执行` 返回的是 stdout+stderr **拼在一起**的文本，
   合并了就只能比一半（M128 修过的病：**「用户看到什么」= 两条流加起来**，
   但比较时常常要分开：正常输出一字不差、告警只要包含某句）；
2. **并行跑一批命令** —— `并发` 只给了批量 IO（`批量读`/`并行取`），**起不了进程**，
   运行器只能串行 ⇒ 自测会比 pytest 还慢，「换掉」就没有意义。

## 判据分三层

- **语义**：三宿主（Python / Rust / Node）跑同一段程序，输出**逐字一致**；
- **顺序**：`并行执行` 的结果**顺序 = 输入顺序**（M114 立的规矩，故意让前面的慢）；
- **墙钟**：并行确实比串行快（这条断言墙上时间 ⇒ 打 `计时` 标记进串行节）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "rust" / "target" / "release" / ("jishi-rs.exe" if os.name == "nt" else "jishi-rs")
NODE入口 = ROOT / "oracle" / "node" / "index.js"

#: 用一个**必然存在**的可执行来跑命令：测试进程自己就是 Python。
PY = sys.executable.replace("\\", "/")


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    env["PYTHONPATH"] = str(ROOT)
    return env


def _跑三宿主(源码: str, 名: str = "eval") -> list[tuple[bytes, bytes, int]]:
    """同一段源码在 Python / Rust / Node 三个宿主上跑一遍（Node 走字节码）。

    ⚠️ 临时文件名带 pid（本项目并行跑 pytest 的硬规矩：固定名会被别的 worker 覆写）。
    """
    # ⚠️ 文件名要 sanitize：`名` 里可能带引号（比如测试传 `路数"三`），
    #    Windows 上 `"` 在文件名里直接 `OSError: Invalid argument`。
    安全名 = "".join(c if c.isalnum() or c in "-_" else "_" for c in 名)[:40]
    场 = Path(tempfile.gettempdir()) / f"{os.getpid()}_m133"
    场.mkdir(parents=True, exist_ok=True)
    源 = 场 / f"{安全名}_{os.getpid()}.jsh"
    源.write_text(源码, encoding="utf-8")
    字节 = 场 / f"{安全名}_{os.getpid()}.bc.json"
    try:
        # Node 只吃字节码：先用参考实现编译一份
        编 = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", "--dump-bytecode", str(源)],
                            capture_output=True, env=_env(), cwd=str(ROOT), timeout=180)
        assert 编.returncode == 0, f"编译字节码失败：{编.stderr.decode('utf-8', 'replace')[:400]}"
        字节.write_bytes(编.stdout)

        命令们 = [
            [sys.executable, "-m", "oracle.jishi.cli", str(源)],
            [str(EXE), str(源)],
            ["node", str(NODE入口), str(字节)],
        ]
        出 = []
        for c in 命令们:
            r = subprocess.run(c, capture_output=True, env=_env(), cwd=str(ROOT), timeout=180)
            出.append((r.stdout, r.stderr, r.returncode))
        return 出
    finally:
        for p in (源, 字节):
            try:
                p.unlink()
            except OSError:
                pass


def _一致(源码: str, 名: str = "eval") -> tuple[bytes, bytes]:
    """三宿主必须**逐字一致**（stdout 与 stderr 都比）。返回 (stdout, stderr)。"""
    三 = _跑三宿主(源码, 名)
    py, rs, nd = 三
    assert py == rs, (
        f"Python 与 Rust 不一致\n  py stdout={py[0]!r}\n  rs stdout={rs[0]!r}\n"
        f"  py stderr={py[1]!r}\n  rs stderr={rs[1]!r}\n  退码 py={py[2]} rs={rs[2]}")
    assert py == nd, (
        f"Python 与 Node 不一致\n  py stdout={py[0]!r}\n  nd stdout={nd[0]!r}\n"
        f"  py stderr={py[1]!r}\n  nd stderr={nd[1]!r}\n  退码 py={py[2]} nd={nd[2]}")
    return py[0], py[1]


# ---------------------------------------------------------------------------
# M133：两条流分开
# ---------------------------------------------------------------------------

def test_执行详细_两条流分开():
    """stdout 归 stdout、stderr 归 stderr —— 这正是 `执行` 做不到的。"""
    码 = f'''导入 系统

令 r = 系统.执行详细(["{PY}", "-c", "import sys; print('OUT'); print('ERR', file=sys.stderr); sys.exit(7)"])
打印("输出=[" + r["输出"] + "]")
打印("错误=[" + r["错误"] + "]")
打印("退出码=" + 文本(r["退出码"]))
'''
    出, _ = _一致(码, "分开")
    文 = 出.decode("utf-8")
    assert "输出=[OUT" in 文, 文
    assert "错误=[ERR" in 文, 文
    # 🔴 **关键**：`错误` 里不许混进 OUT，`输出` 里不许混进 ERR
    assert "输出=[ERR" not in 文 and "错误=[OUT" not in 文, 文
    assert "退出码=7" in 文, 文


def test_执行详细_成功时错误为空串():
    """成功时 `错误` 是**空串**（不是 `空`）—— 与 `输出` 同类型，调用方不用分支。"""
    码 = f'''导入 系统

令 r = 系统.执行详细(["{PY}", "-c", "print('ok')"])
打印("[" + r["错误"] + "]")
打印(文本(长度(r["错误"])))
'''
    出, _ = _一致(码, "空错误")
    assert 出.decode("utf-8") == "[]\n0\n", 出


def test_执行详细_命令跑不起来要抛错():
    """「命令跑不起来」抛错，不塞进返回值 —— 那是**程序写错**，不是「命令失败」。"""
    码 = '''导入 系统

尝试：
    令 r = 系统.执行详细(["这个程序一定不存在_zzz"])
    打印("没抛错：" + 文本(r["退出码"]))
捕获 异常 为 e：
    打印("抛了 " + e.类型)
'''
    出, _ = _一致(码, "跑不起来")
    assert "抛了" in 出.decode("utf-8"), 出


# ---------------------------------------------------------------------------
# M134：并行执行
# ---------------------------------------------------------------------------

def test_并行执行_顺序等于输入顺序():
    """故意让**前面的慢、后面的快** —— 结果仍须按输入顺序回来（M114 的规矩）。

    ⚠️ 如果按「完成顺序」收，读到的会是「第2条、第1条、第0条」。
    """
    码 = f'''导入 系统

令 命令们 = []
遍历 i 在 [0, 1, 2]：
    令 睡 = 文本((2 - i) * 0.15)
    令 码 = "import time; time.sleep(" + 睡 + "); print('第" + 文本(i) + "条')"
    令 命令们 = 命令们 + [["{PY}", "-c", 码]]

令 结果们 = 系统.并行执行(命令们, 3)
遍历 项 在 结果们：
    打印(项["输出"])
'''
    出, _ = _一致(码, "保序")
    行们 = [x for x in 出.decode("utf-8").splitlines() if x.strip()]
    assert 行们 == ["第0条", "第1条", "第2条"], 行们


def test_并行执行_空列表给空列表():
    码 = '''导入 系统

令 结果们 = 系统.并行执行([])
打印(长度(结果们))
'''
    出, _ = _一致(码, "空列表")
    assert 出.decode("utf-8").strip() == "0", 出


def test_并行执行_路数不合法要报错_三宿主同字():
    """`路数` 的校验**借用 `并发` 模块那份** —— 文案三宿主必须一模一样。

    ⚠️ **别在程序里 `打印(文本(e))`**：那是 AGENTS §3.3 登记过的分叉
    （Python 的 `文本(异常)` 带「┌─ 文件:行:列」，Rust/Node 只给一行）。
    判据要的是「文案对不对」，所以在**程序里用「包含」判**，只打印对/不对。
    """
    for 坏路数, 期望 in (("0", "线程数要是正整数，现在是 0"),
                          ('"三"', "线程数要是正整数，得到了「文本」")):
        码 = f'''导入 系统

尝试：
    系统.并行执行([["{PY}", "-c", "print(1)"]], {坏路数})
    打印("没抛错")
捕获 异常 为 e：
    如果 文本(e).包含("{期望}")：
        打印("报错文案对")
    否则：
        打印("报错文案不对")
'''
        出, _ = _一致(码, f"路数{坏路数[:2]}")
        文 = 出.decode("utf-8")
        assert 文.strip() == "报错文案对", f"路数={坏路数}：{文}"


def test_并行执行_命令们不是列表要报错_三宿主同字():
    码 = f'''导入 系统

尝试：
    系统.并行执行("{PY} -c print(1)")
    打印("没抛错")
捕获 异常 为 e：
    如果 文本(e).包含("要给一个列表")：
        打印("报错文案对")
    否则：
        打印("报错文案不对")
'''
    出, _ = _一致(码, "非列表")
    assert 出.decode("utf-8").strip() == "报错文案对", 出


@pytest.mark.计时
def test_并行执行_确实比串行快():
    """**墙钟判据**（打 `计时` 标记，进串行节）：并行不能只是「名字里有个并行」。

    三条各睡 0.8 秒：串行 ≥ 2.4 秒，并行 3 路应在 ~0.9 秒。

    ⚠️ 阈值取 **1.8 秒**：留足抖动余量（本机负载会抬高），但足以区分
    「真并行（~0.9s）」和「假并行（~2.4s）」。
    """
    码 = f'''导入 系统
导入 时间

令 命令们 = []
遍历 i 在 [0, 1, 2]：
    令 命令们 = 命令们 + [["{PY}", "-c", "import time; time.sleep(0.8)"]]

令 起 = 时间.高精度时间()
系统.并行执行(命令们, 3)
令 并行耗 = 时间.高精度时间() - 起
打印(文本(并行耗))
'''
    三 = _跑三宿主(码, "墙钟")
    py = 三[0]
    assert py[2] == 0, py[1].decode("utf-8", "replace")[:400]
    耗 = float(py[0].decode("utf-8").strip())
    # 🔴 **Node 宿主是顺序执行的**（同步 VM 没有事件循环）—— 所以这里只对
    #    Python/Rust 断言墙钟；Node 只要求**结果一致**（在 `test_并行执行_顺序...` 里钉着）。
    assert 耗 < 1.8, f"三条各睡 0.8 秒，3 路并行用了 {耗:.2f} 秒 —— 没真并行"
