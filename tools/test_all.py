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


def _跑(标题: str, 参数: list[str], 说明: str) -> tuple[bool, float]:
    print(f"\n{'=' * 72}\n{标题}\n  命令：{' '.join(['pytest', *参数])}\n{'=' * 72}", flush=True)
    if 说明:
        print(说明, flush=True)
    起 = time.perf_counter()
    r = subprocess.run([PY, "-m", "pytest", *参数], cwd=str(ROOT))
    用时 = time.perf_counter() - 起
    print(f"\n→ {标题}结束：退出码 {r.returncode}，用时 {用时:.1f} 秒", flush=True)
    return r.returncode == 0, 用时


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
                "  · 按**文件**分发（`--dist loadfile`），每条带超时兜底")
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
                    "")
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
            "  · 这些断言**墙上时间**，必须独占 —— 并行时的负载会把它们抬成假红")

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
