# -*- coding: utf-8 -*-
"""全量测试 —— **两节**：先并行跑主体，再串行跑剩下的。

    python tools/test_all.py                # 两节都跑
    python tools/test_all.py --只并行       # 只跑第 1 节（日常快速自检）
    python tools/test_all.py --只串行       # 只跑第 2 节
    python tools/test_all.py -n 8           # 改并行度（默认 4）
    python tools/test_all.py --超时 300     # 改单条超时（默认 180 秒，0 = 关掉）
    python tools/test_all.py --分块 4       # **兜底**：把测试文件分 4 批分别起进程跑

## 为什么分两节

**第 1 节（并行）**：`-m "not 计时"` —— 绝大多数用例。它们互不干扰，能并行。
**第 2 节（串行）**：`-m 计时` —— 断言的是**墙上时间**（「20 万次调用 < 300ms」这种）。
负载会把它们抬高 ⇒ **必须独占**，否则假红。这也是 CI 只跑第 1 节的原因。

## 三条硬规矩（踩出来的）

1. 🔴 **`--dist loadfile` 不是可选项**：xdist 默认按**测试**分发（`load`），在本题量下
   会死锁 —— 表现是「跑到 98% 就不动了」，所有 worker 在 `wait`、主进程在 `join`，
   **没有人失败**。按**文件**分发才稳。
2. 🔴 **每条用例要带超时**（`--timeout`）：上游已知问题 —— worker 崩掉之后
   `--dist loadfile` 会**永久挂住**（pytest-xdist #784 / #1313 / #1401），
   而崩溃本身在「CPU 饥荒」下是**静默**的（没有退出码）。有了单条超时，
   卡住的那条会被判失败、session 继续往前走，**不会让整轮永远停住**。
3. 🔴 **`--分块 N` 是「worker 崩了也不至于全废」的兜底**。单条 `--timeout` 只能治
   「测试卡住」，**治不了「worker 自己死掉」** —— 那时它内部没人计时了，主进程会
   一直等（上游 #784 至今未修）。分块之后每批是**独立进程**，崩一批只影响那一批，
   其他批照样出结果。**日常不用分块**（慢）；只在「明明没失败，却卡在 98%」时用。
4. ⚠️ **新写的测试先放并行那一节**。只有当它**断言墙上时间**（或依赖独占资源）时，
   才打 `@pytest.mark.计时` 放进串行节。**别图省事就往串行塞** —— 串行一节是
   全量里最慢的部分，塞进去等于让所有人等它。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


#: 出现这些字样就说明**并行 worker 崩过**（不是用例失败，是结果不可信）。
#:
#: 🔴 为什么必须单独认（2026-10-24）：pytest-xdist 的 worker 在 CPU 饥荒下会
#:    **无退出码地死掉**（上游 #784 / #1313 / #1401），而 `--dist loadfile` 会
#:    **永久等那个死掉的 worker** —— 现象是「跑到 98% 不动、没有任何失败」。
#:    更坏的情况是它**刚好在崩之前跑完**：pytest 退出码 0，于是这一轮被当成了「全绿」，
#:    而实际上**有测试根本没跑**。⇒ 见到这些字样一律判**失败**，并明说不是用例的错。
崩的记号 = (
    "node down",
    "worker 'gw",
    "crashed while running",
    "worker crashed",
    "xdist: worker",
)


def _跑(标题: str, 参数: list[str], 说明: str,
        节超时: float = 0.0) -> tuple[bool, float]:
    """跑一节 pytest，边跑边打印、同时留一份日志。

    ⚠️ **不能用 `subprocess.run` 直接继承 stdout**（2026-10-24 改）：
    那样既拿不到输出（没法事后查 `node down`），也**没有整节超时** ——
    真卡住的时候这个工具自己也不会退出，只能人去 Ctrl-C。
    现在：逐行 tee 到屏幕 + `build/test_all_<时间>.log`，并且**整节有墙钟上限**。
    """
    print(f"\n{'=' * 72}\n{标题}\n  命令：{' '.join(['pytest', *参数])}\n{'=' * 72}", flush=True)
    if 说明:
        print(说明, flush=True)
    日志目录 = ROOT / "build"
    日志目录.mkdir(exist_ok=True)
    日志 = 日志目录 / f"test_all_{time.strftime('%m%d_%H%M%S')}_{abs(hash(标题)) % 10000}.log"
    起 = time.perf_counter()
    命中崩 = ""
    with open(日志, "w", encoding="utf-8") as f:
        p = subprocess.Popen([PY, "-m", "pytest", *参数], cwd=str(ROOT),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", bufsize=1)
        assert p.stdout is not None
        超时了 = False
        for 行 in p.stdout:
            print(行, end="", flush=True)
            f.write(行)
            if not 命中崩:
                小写 = 行.lower()
                for 记号 in 崩的记号:
                    if 记号 in 小写:
                        命中崩 = 记号
                        # 🔴 **一旦确认 worker 崩了，立刻停**（2026-10-24 实测）：
                        #    崩了之后 `--dist loadfile` 会**永久等那个死掉的 worker**
                        #    （实测：`[gw0] node down: Not properly terminated` 之后
                        #    卡在 99% 再也不动）。等下去拿不到任何新信息，
                        #    只会白耗时间 —— 直接杀掉，判红，让人改用 `--分块`。
                        print(f"\n❌ 检测到 worker 崩了（「{命中崩}」）—— "
                              f"xdist 会永久等它，不等了，直接停。", flush=True)
                        f.write(f"\n[test_all] 检测到「{命中崩}」，已主动停止\n")
                        p.kill()
                        break
            if 节超时 and time.perf_counter() - 起 > 节超时:
                超时了 = True
                print(f"\n⚠️ 本节超过 {节超时:.0f} 秒 —— 判为卡住，杀掉。", flush=True)
                f.write(f"\n[test_all] 本节超过 {节超时:.0f} 秒，被杀\n")
                p.kill()
                break
        p.wait()
    用时 = time.perf_counter() - 起
    码 = p.returncode
    print(f"\n→ {标题}结束：退出码 {码}，用时 {用时:.1f} 秒（日志：{日志.relative_to(ROOT)}）",
          flush=True)

    if 超时了:
        return False, 用时
    if 命中崩:
        print(f"\n❌ 这一节里 worker 崩过（命中「{命中崩}」）—— **结果不可信**：\n"
              f"   崩掉的那个 worker 手上的测试**根本没跑**，但 pytest 可能仍然退 0。\n"
              f"   ⇒ 别把它当「全绿」。\n"
              f"   ✅ 改用：`python tools/test_all.py --分块 4`"
              f"（每批独立进程，崩一批只废那一批；实测这是这台机器上唯一稳的并行跑法）。",
              flush=True)
        return False, 用时
    return 码 == 0, 用时


def main(argv: list[str]) -> int:
    只并行 = "--只并行" in argv
    只串行 = "--只串行" in argv
    并发 = "4"
    超时 = "180"
    分块 = 0
    for i, a in enumerate(argv):
        if a == "-n" and i + 1 < len(argv):
            并发 = argv[i + 1]
        if a == "--超时" and i + 1 < len(argv):
            超时 = argv[i + 1]
        if a == "--分块" and i + 1 < len(argv):
            分块 = max(1, int(argv[i + 1]))

    并行参数 = ["tests/", "-q", "-n", 并发, "--dist", "loadfile", "-m", "not 计时",
                "--tb=line", "-rf"]
    if 超时 != "0":
        并行参数 += ["--timeout", 超时, "--timeout-method", "thread"]

    结果: dict[str, tuple[bool, float]] = {}

    if not 只串行:
        if 分块 <= 1:
            结果["第 1 节（并行）"] = _跑(
                "第 1 节：并行（`not 计时`）", 并行参数,
                "  · 按**文件**分发（`--dist loadfile`），每条带超时兜底",
                节超时=1800)
        else:
            # 分块：每批独立进程。崩一批只废那一批，其余照样出结果。
            文件们 = sorted(p.name for p in (ROOT / "tests").glob("test_*.py"))
            大小 = -(-len(文件们) // 分块)          # 向上取整
            print(f"\n{'=' * 72}\n第 1 节：并行（分 {分块} 批，共 {len(文件们)} 个测试文件）\n{'=' * 72}")
            全过 = True
            总 = 0.0
            for i in range(分块):
                批 = 文件们[i * 大小:(i + 1) * 大小]
                if not 批:
                    continue
                成功, 用时 = _跑(
                    f"  第 1 节 · 第 {i + 1}/{分块} 批（{批[0]} … {批[-1]}）",
                    [f"tests/{f}" for f in 批] + ["-q", "-n", 并发,
                     "--dist", "loadfile", "-m", "not 计时", "--tb=line", "-rf"]
                    + (["--timeout", 超时, "--timeout-method", "thread"] if 超时 != "0" else []),
                    "", 节超时=1200)
                全过 = 全过 and 成功
                总 += 用时
                结果[f"第 1 节·批{i + 1}"] = (成功, 用时)
            # 把分块细节折进「第 1 节」这一行，汇总里只露一行
            if not 全过:
                print("\n（分块跑完：有一批没通过）", flush=True)
            结果["第 1 节（并行·分块）"] = (全过, 总)
            for k in [k for k in 结果 if k.startswith("第 1 节·批")]:
                结果.pop(k)

    if not 只并行:
        结果["第 2 节（串行）"] = _跑(
            "第 2 节：串行（`计时`）",
            ["tests/", "-q", "-m", "计时", "--tb=short", "-rf"],
            "  · 这些断言**墙上时间**，必须独占 —— 并行时的负载会把它们抬成假红",
            节超时=1800)

    print(f"\n{'=' * 72}\n汇总\n{'=' * 72}")
    总用时 = 0.0
    for 名, (成功, 用时) in 结果.items():
        总用时 += 用时
        print(f"  {名:14s} {'✅ 通过' if 成功 else '❌ 有失败'}   {用时:7.1f} 秒")
    print(f"  {'合计':14s} {'':6s}   {总用时:7.1f} 秒")

    失败 = [名 for 名, (成功, _) in 结果.items() if not 成功]
    if 失败:
        print(f"\n❌ {'、'.join(失败)} 没通过 —— 先看上面的 FAILED 清单，别急着当成「环境问题」。")
        return 1
    print("\n✅ 两节都通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
