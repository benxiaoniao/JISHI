# -*- coding: utf-8 -*-
"""一键测试：运行全部单元测试、黄金文件测试与错误快照测试。

用法：
    python tools/run_tests.py          全部测试
    python tools/run_tests.py -v       详细输出
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def main() -> int:
    args = sys.argv[1:]
    verbose = "-v" in args or "--verbose" in args
    cmd = [PYTHON, "-m", "pytest", str(ROOT / "tests"),
           "-q" if not verbose else "-v"]
    proc = subprocess.run(cmd, cwd=ROOT)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
