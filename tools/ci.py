# -*- coding: utf-8 -*-
"""M6 一键全检（CI 的本地版）：C VM 构建 → 测试 → 对拍确认 → 基准冒烟。

用法：
    python tools/ci.py            完整检查（失败即非零退出）
    python tools/ci.py --skip-bench   跳过基准（CI 快检时用）

检查项（顺序执行，任一步失败立即退出）：
  1. C 虚拟机构建                cvm/build.py（gcc 可用时；放在最前是因为
                                 Windows 下已加载的 DLL 无法被覆盖写入）
  2. 单元/黄金/错误快照测试      pytest tests/
  3. 一致性夹具（R0）            tools/conformance.py check —— 前端四个阶段
                                 （tokens/AST/字节码/诊断）与冻结基线逐项比对。
                                 它其实也在 pytest 里跑（`test_m57_conformance.py`），
                                 这里单独一条是为了**失败时报告更聚焦**：
                                 不用在两千多项测试里找那一行。
  4. C VM 对拍可用性确认         tests/test_cvm.py 里的 C 用例是否真正在跑
  5. 评测集标准答案自测          bench/ai-eval/eval.py --self-test（50 题）
  6. 基准冒烟                    tools/benchmark.py --quick
  7. 内建体检（--fast）          tools/probe_builtins.py —— 每个内建在三执行器
                                 上真跑（只比名字的漂移检测抓不到「名字在表里
                                 却用不了」，M56 的 `超` 就是这么漏的）
  8. Rust 前端四阶段对拍（R2–R5） emit --frontend rust → compare 四个阶段
                                 （tokens / ast / bytecode / diagnostics）
                                 ⚠️ 需要 Rust 扩展已构建；没构建就**明确跳过**
                                 （打印「跳过」而不是默默当通过 —— 这两件事
                                 必须在输出里分得开）
  9. 执行器体检（R5.1）          tools/check_engines.py —— 全语料
                                 （**含 error_cases**）在各执行器上真跑，
                                 逐字节比「树遍历 vs 各 VM」。
                                 ⚠️ 为什么非它不可：一致性夹具比的是**前端产物**，
                                 而**「产物一样 ≠ 跑起来一样」** —— R5.1 修掉的四个
                                 既有 bug 里有三个是夹具全程绿的情况下存在的。
"""

import os
import subprocess
import sys
import tempfile
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
    #
    # ⚠️ **CI 上跳过这一遍**（2026-10-10）：workflow 里前面那步「全量测试」跑的
    #    就是同一条命令，再跑一遍是纯浪费 —— 实测 Windows runner 上这一遍要
    #    **3924 秒（65 分钟）**，两个步骤叠一起把 job 拖到 **2 小时 10 分**。
    #    由 workflow 传 `JISHI_SKIP_PYTEST=1`；**本机跑不该跳**（`ci.py` 的定位
    #    就是「一键全检」，本地一次跑完）。
    #    另外 CI 那步带了 `-m "not 计时"` —— 墙上护栏在共享 runner 上没有判别力
    #    （同一份代码本机 202ms、CI Windows **301ms**，只超 1.3 毫秒就判红）。
    if os.environ.get("JISHI_SKIP_PYTEST"):
        print("\n=== pytest 全量测试 ===")
        print("--- pytest 全量测试: 跳过"
              "（JISHI_SKIP_PYTEST=1：调用方已经跑过一遍，不重复） ---")
    elif not _run([PYTHON, "-m", "pytest", str(ROOT / "tests"), "-q"],
                  "pytest 全量测试"):
        return 1

    # 3) 一致性夹具（R0 建）：前端四个阶段与冻结基线逐项比对。
    #    为什么要单独一条（pytest 里也跑）：失败时报告更聚焦 —— 直接给出
    #    「哪个语料的哪个阶段哪一项不同」，不必在两千多项测试里翻。
    if not _run([PYTHON, str(ROOT / "tools" / "conformance.py"), "check"],
                "一致性夹具（前端四阶段 vs 基线）"):
        return 1

    # 4) 确认 C VM 对拍真的在跑（HAS_CVM 必须为真）
    probe = ("import sys; sys.path.insert(0, r'%s'); "
             "from oracle.jishi import cvm_bind; cvm_bind.load_lib(); "
             "print('C VM 可用，对拍含 C 用例')") % str(ROOT)
    if not _run([PYTHON, "-c", probe], "C VM 对拍可用性确认"):
        return 1

    # 5) 评测集标准答案自测（语法改动不得使通过率下降）
    if not _run([PYTHON, str(ROOT / "bench" / "ai-eval" / "eval.py"),
                 "--self-test"], "评测集 JishiEval 标准答案自测"):
        return 1

    # 6) 基准冒烟（可选跳过）
    if not skip_bench:
        if not _run([PYTHON, str(ROOT / "tools" / "benchmark.py"), "--quick"],
                    "基准冒烟（--quick）"):
            return 1

    # 7) 内建体检（M56 建）：给**每个内建**一条最小程序，在真引擎上跑一遍 ——
    #    只比名字的漂移检测抓不到「名字在表里、调用却不行」（M53 的 Rust
    #    `文本.查找`、M56 的 `超` 都是这么漏的）。`--fast` 只跑三执行器；
    #    两宿主那两位由 pytest 里的五引擎对拍覆盖。
    if not _run([PYTHON, str(ROOT / "tools" / "probe_builtins.py"), "--fast"],
                "内建体检（--fast）"):
        return 1

    # 8) Rust 前端四阶段对拍（R2–R5）：让 Rust 前端按契约产出四个阶段的产物，
    #    再拿基线逐项比（R2 只比 tokens，R3 加 ast、R5 加 bytecode+diagnostics）。
    #    ⚠️ 扩展是按当前 Python 的 ABI 编出来的，CI 上不一定有（CI 不编译 Rust）。
    #    没构建时**明确报「跳过」**，绝不静默当通过 —— 「跳过」与「通过」
    #    在 CI 输出里长得一样是本项目反复踩过的坑（R0 的冻 bug、M49 的静默校验）。
    probe = ("import sys; sys.path.insert(0, r'%s'); "
             "from oracle.jishi import tokenizer as T; "
             "print('YES' if T.rust_available() else 'NO')") % str(ROOT)
    got = subprocess.run([PYTHON, "-c", probe], cwd=ROOT, capture_output=True)
    if got.stdout.strip().endswith(b"YES"):
        with tempfile.TemporaryDirectory() as tmp:
            if not _run([PYTHON, str(ROOT / "tools" / "conformance.py"), "emit",
                         "--frontend", "rust", "--out", tmp],
                        "Rust 前端产出 tokens"):
                return 1
            for stage in ("tokens", "ast", "bytecode", "diagnostics"):
                if not _run([PYTHON, str(ROOT / "tools" / "conformance.py"),
                             "compare", "--from", tmp, "--stage", stage],
                            f"Rust 前端 {stage} vs 基线（R2–R5 验收）"):
                    return 1
    else:
        print("\n=== Rust 前端分词对拍（R2）：⏭ 跳过 —— 扩展未构建 ===")
        print("    要跑它先 `python core/build.py`。**这里没跑，不是通过。**")

    # 9) 执行器体检（R5.1 建）：全语料（**含 error_cases** —— 报错文本也是
    #    可观察行为）在各执行器上真跑，逐字节比「树遍历 vs 各 VM」。
    #    ⚠️ **它管的是一致性夹具管不到的那一半**：夹具比「前端产物」，
    #    而「产物一样 ≠ 跑起来一样」—— R5.1 修掉的四个既有 bug 里有三个
    #    是夹具全程绿的情况下存在的（VM 撞掉帧栈基址 / 解释器的作用域 /
    #    报错里的调用链）。Rust VM 有已知缺口登记表，登记过期会报红。
    if not _run([PYTHON, str(ROOT / "tools" / "check_engines.py")],
                "执行器体检（全语料 × 各执行器，含 error_cases）"):
        return 1

    print("\n✓ 全检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
