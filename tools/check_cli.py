# -*- coding: utf-8 -*-
"""CLI 差异闸门（R7.4）：把**同一份输入**喂 `jishi-rs` 与 `jishi`，逐字节比。

为什么单列一个工具（而不是塞进 pytest）：它是**全语料 × 三种模式 × 两个引擎**
的横向扫描（几百次进程启动），与 `tools/check_engines.py` 同一类 ——
「改完引擎行为就跑一遍」的那种闸门。定向判据在 `tests/test_m92_cli_rust.py`。

用法：
    python tools/check_cli.py                 # 跑默认目录
    python tools/check_cli.py tests/cases     # 只跑一个目录
    python tools/check_cli.py --timeout 20    # 单次调用超时（秒）

比较的三种模式（都是 R7.4 补进来的能力）：

| 模式 | 说明 |
|---|---|
| 跑源码 | `jishi-rs <文件>` vs `jishi <文件>`（stdout / stderr / 退出码） |
| `--ast` | 缩进语法树 |
| `--json-errors` | 机器可读报错（含 `line/col/hint/fix`） |

外加**沙箱**那一组（`--stdin --sandbox --json-result`）—— 那是 MCP 的原语；
比之前把 `duration_ms` 归一（墙上时间，两边永远不可能相等）。

⚠️ **跳过读标准输入的程序**（`输入(`）：两边都会停在等输入上，喂不进去
（MCP 那条路走的是子进程，而这里直接起进程）。它们不是「不一致」，是没法这么比。

⚠️ **跳过的三类都显式登记**（`SKIP_RULES`）—— 不许静默略过（M51 的规矩：
豁免必须写出来、能复核、补掉了就回来删一行）。

⚠️ 语料在**私有 cwd** 里跑：tutorial / examples 里那几个程序会往 cwd 写真实
文件，落在仓库根就是给 `tools/check_engines.py` 埋雷（那一课付过代价）。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DIRS = ["tests/cases", "tests/error_cases", "tests/frontend_cases", "examples"]

#: `duration_ms` 是墙上时间 —— 归一后再比
DUR = re.compile(rb'duration_ms[^0-9]*[-0-9][0-9.eE+-]*')
DUR_ZERO = b'duration_ms": 0'

#: 「这份语料不参与逐字节对拍」的**显式登记**：`(理由, 判定)`。
#: 判定拿 `(文件名, 源码文本)` 进去。**补掉一条就回来删一行** —— 这里不是垃圾桶，
#: 每一条都要能说出「为什么这不是 bug」。
SKIP_RULES = [
    ("要读标准输入（两边都会停在等输入上，喂不进去）",
     lambda name, text: "输入(" in text),
    ("要从 Python / 本地包导入 —— 单二进制**故意没有**这个能力"
     "（AGENTS.md §2 的「搬不动的显式登记」：Rust 宿主不链 CPython）",
     lambda name, text: "从 python" in text or "从 本地包" in text),
    ("`时间.计时` 是 m51 `KNOWN_GAPS` 里**已登记**的缺口（要上下文协议 + 带方法的"
     "对象，归基石库层）；且这一份的输出含墙上时间跑分，本来就不可逐字节比",
     lambda name, text: name == "排序.jsh"),
]


def _skip_reason(f: Path, text: str) -> str | None:
    for reason, pred in SKIP_RULES:
        if pred(f.name, text):
            return reason
    return None


#: 沙箱用例（与 `tests/test_m92_cli_rust.py` 同一组）
SANDBOX = [
    '打印("你好")\n',
    '导入 数学\n打印(数学.开方(4))\n',
    '导入 文件\n',
    '导入 网络\n',
    '导入 不存在的模块\n',
    '导入 os 从 python\n',
    '导入 x 从 本地包\n',
    '打印(1 / 0)\n',
    '打印(没有这个名)\n',
    '如果 真\n',
    'print(1)\n',
    '令 列 = [1, 2, 3]\n遍历 x 在 列：\n    打印(x)\n',
    '遍历 i 在 范围(3000)：\n    打印("一二三四五")\n',
    '抛出 值错误("自己抛的")\n',
    '函数 甲()：\n    返回 1 / 0\n甲()\n',
]


def _exe() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    env["PYTHONPATH"] = str(ROOT)
    return env


class Runner:
    def __init__(self, timeout: float, cwd: Path):
        self.timeout = timeout
        self.cwd = cwd

    def run(self, argv, stdin: bytes = b""):
        try:
            r = subprocess.run(argv, input=stdin, capture_output=True,
                               env=_env(), cwd=str(self.cwd), timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return b"<TIMEOUT>", b"", -1
        return r.stdout, r.stderr, r.returncode

    def rust(self, args, stdin: bytes = b""):
        return self.run([str(_exe())] + list(args), stdin)

    def py(self, args, stdin: bytes = b""):
        return self.run([sys.executable, "-m", "oracle.jishi.cli"] + list(args), stdin)


def _norm(t: bytes) -> bytes:
    return DUR.sub(DUR_ZERO, t)


def main() -> int:
    ap = argparse.ArgumentParser(description="CLI 差异闸门（jishi-rs vs jishi）")
    ap.add_argument("dirs", nargs="*", default=None, help="只跑这些目录")
    ap.add_argument("--timeout", type=float, default=25.0, help="单次调用超时秒")
    args = ap.parse_args()

    if not _exe().exists():
        print(f"（注：{_exe().name} 不存在，先 `cd rust && cargo build --release`）")
        return 2

    dirs = args.dirs or DIRS
    files = []
    skipped = []      # [(Path, 理由)]
    for d in dirs:
        p = ROOT / d
        if not p.is_dir():
            continue
        for f in sorted(x for x in p.rglob("*.jsh") if x.is_file()):
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            reason = _skip_reason(f, text)
            if reason:
                skipped.append((f, reason))
                continue
            files.append(f)

    cwd = Path(tempfile.mkdtemp(prefix="check_cli_"))
    r = Runner(args.timeout, cwd)
    bad: list[str] = []
    n_ok = 0

    def cmp(tag, a, b, norm=True):
        nonlocal n_ok
        if norm:
            a = (_norm(a[0]), a[1], a[2])
            b = (_norm(b[0]), b[1], b[2])
        if a == b:
            n_ok += 1
            return
        bad.append(tag)
        print(f"✗ {tag}")
        for name, x, y in (("stdout", a[0], b[0]), ("stderr", a[1], b[1]),
                           ("退出码", str(a[2]), str(b[2]))):
            if x != y:
                print(f"    [{name}] rust={x[:300]!r}")
                print(f"    [{name}] py  ={y[:300]!r}")

    print(f"语料 {len(files)} 个（登记跳过 {len(skipped)} 个）")
    for f in files:
        p = str(f)
        rel = f.name
        cmp(f"跑源码 {rel}", r.rust([p]), r.py([p]), norm=False)
        cmp(f"--ast {rel}", r.rust([p, "--ast"]), r.py([p, "--ast"]), norm=False)
        cmp(f"--json-errors {rel}", r.rust([p, "--json-errors"]),
            r.py([p, "--json-errors"]), norm=False)

    for src in SANDBOX:
        cmp(f"沙箱 {src!r}", r.rust(["--stdin", "--sandbox", "--json-result",
                                     "--timeout", "1"], src.encode("utf-8")),
            r.py(["--stdin", "--sandbox", "--json-result",
                  "--timeout", "1"], src.encode("utf-8")))

    print(f"\n一致 {n_ok} · 不一致 {len(bad)}")
    if skipped:
        print(f"\n登记的跳过（{len(skipped)} 个）：")
        for f, reason in skipped:
            print(f"  · {f.name} —— {reason}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
