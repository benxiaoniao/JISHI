# -*- coding: utf-8 -*-
"""M6 一键全检（CI 的本地版）：C VM 构建 → 测试 → 对拍确认 → 基准冒烟。

用法：
    python tools/ci.py            完整检查（失败即非零退出）
    python tools/ci.py --skip-bench   跳过基准（CI 快检时用）

检查项（顺序执行，任一步失败立即退出）：
  1. C 虚拟机构建                cvm/build.py（gcc 可用时；放在最前是因为
                                 Windows 下已加载的 DLL 无法被覆盖写入）
  2. 单元/黄金/错误快照测试      pytest tests/
  3. C VM 对拍可用性确认         tests/test_cvm.py 里的 C 用例是否真正在跑
  4. 评测集标准答案自测          bench/ai-eval/eval.py --self-test（50 题）
  5. 基准冒烟                    tools/benchmark.py --quick
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def _run(cmd: list[str], label: str) -> bool:
    print(f"\n=== {label} ===")
    proc = subprocess.run(cmd, cwd=ROOT)
    ok = proc.returncode == 0
    print(f"--- {label}: {'通过' if ok else '失败'} ---")
    return ok


def main() -> int:
    args = sys.argv[1:]
    skip_bench = "--skip-bench" in args

    # 1) 先构建 C VM（本进程不得先加载 DLL，否则 Windows 上无法覆盖写入）
    if not _run([PYTHON, str(ROOT / "cvm" / "build.py")], "C 虚拟机重新构建"):
        return 1

    # 2) 全量测试（含三执行器对拍）
    if not _run([PYTHON, "-m", "pytest", str(ROOT / "tests"), "-q"],
                "pytest 全量测试"):
        return 1

    # 3) 确认 C VM 对拍真的在跑（HAS_CVM 必须为真）
    probe = ("import sys; sys.path.insert(0, r'%s'); "
             "from jishi import cvm_bind; cvm_bind.load_lib(); "
             "print('C VM 可用，对拍含 C 用例')") % str(ROOT)
    if not _run([PYTHON, "-c", probe], "C VM 对拍可用性确认"):
        return 1

    # 4) 评测集标准答案自测（语法改动不得使通过率下降）
    if not _run([PYTHON, str(ROOT / "bench" / "ai-eval" / "eval.py"),
                 "--self-test"], "评测集 JishiEval 标准答案自测"):
        return 1

    # 5) 基准冒烟（可选跳过）
    if not skip_bench:
        if not _run([PYTHON, str(ROOT / "tools" / "benchmark.py"), "--quick"],
                    "基准冒烟（--quick）"):
            return 1

    print("\n✓ 全检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
