# -*- coding: utf-8 -*-
"""测试用例体检（M130 附带，2026-10-24）。

## 为什么要有这个

用户的话：「是不是有些测试会过时…我们也需要建立一个**测试用例退役的流程**，
不是单纯的就堆砌这个用例。」

背景是真的：**前端从 Python 换成了 Rust**（R7.x），语言也一直在加东西。
换实现之后，有些测试**测的还是已经不存在的实现细节**；它们不会红（因为
被测的东西还在，只是不再是「唯一实现」），但会**误导人**——比如让人以为
Python 的 tokenizer 还是权威实现。

## 这个工具做什么、不做什么

**只报告，不删**。删测试是判断，不是脚本能替人做的决定；
但「哪些该看一眼」机器能列出来。

四类信号：

| 信号 | 为什么可疑 |
|---|---|
| **长期 skip / xfail** | 最常见的形式：「等 X 做完就回来」。做完之后没人回来 —— 它就永远灰着 |
| **提到已删除的脚本/模块** | 搬仓、退役留下的引用（M129 刚踩过：`build_home` 搬走了，主仓库测试还在 import） |
| **最久没被动过的测试** | 语言改了这么多轮它一次没改过 ⇒ 要么它测的东西很稳，要么它测的东西已经没人用了 |
| **测「实现细节」而非行为** | 断言 AST 结构、内部函数名的测试，换实现就失去意义 |

用法：
    python tools/audit_tests.py            # 全量报告
    python tools/audit_tests.py --简      # 只看需要行动的
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

#: 已经退役/搬走的东西 —— 测试里再提到就说明它该跟着走或者该删。
#: ⚠️ 这张表**会过期**：某天这些东西回来了（不太可能），条目要删。
已退役 = {
    "build_home": "拆仓时搬到官网仓库（M129）",
    "build_site": "拆仓时搬到官网仓库",
    "build_playground": "拆仓时搬到官网仓库",
    "build_index_mirror": "拆仓时搬到官网仓库",
    "sync_release": "拆仓时搬到官网仓库",
    "deploy_push": "拆仓时搬到官网仓库",
    "fetch_release_mirror": "拆仓时搬到官网仓库",
}


def _测试文件们() -> list[Path]:
    return sorted(TESTS.glob("test_*.py"))


def _读(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def 查_skip(p: Path, 源码: str) -> list[dict]:
    """把 skip / xfail 连同**原因文本**列出来。

    原因比数量重要：`skip(reason="Windows 没有 fork")` 是**永久有效**的边界，
    `skip(reason="等 M112 做完")` 是**应该过期**的债。脚本分不出来，但人能——
    所以这里把原因原样打出来。
    """
    出 = []
    树 = None
    尝试 = False
    try:
        树 = ast.parse(源码)
        尝试 = True
    except SyntaxError:
        pass

    if 尝试:
        for 节点 in ast.walk(树):
            if not isinstance(节点, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for dec in 节点.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                名 = ""
                if isinstance(dec.func, ast.Attribute):
                    名 = dec.func.attr
                elif isinstance(dec.func, ast.Name):
                    名 = dec.func.id
                if 名 not in ("skip", "skipif", "xfail"):
                    continue
                因 = ""
                for kw in dec.keywords:
                    if kw.arg == "reason" and isinstance(kw.value, ast.Constant):
                        因 = str(kw.value.value)
                if not 因 and dec.args and isinstance(dec.args[0], ast.Constant):
                    因 = str(dec.args[0].value)
                出.append({"文件": p.name, "用例": 节点.name, "类型": 名,
                           "原因": 因, "行": dec.lineno})
    else:
        for m in re.finditer(r"@pytest\.mark\.(skip|skipif|xfail)[^\n]*", 源码):
            出.append({"文件": p.name, "用例": "?", "类型": m.group(1),
                       "原因": m.group(0)[:80], "行": 源码[:m.start()].count("\n") + 1})
    return 出


def 查_已退役引用(p: Path, 源码: str) -> list[dict]:
    """测试里提到已搬走/退役的脚本 ⇒ 它多半该跟着走，或者已经失效（只是碰巧还绿）。"""
    出 = []
    在三引号 = False
    for 行号, 行 in enumerate(源码.splitlines(), 1):
        # ⚠️ **跳过注释行和 docstring 里的文字**：`test_m57` 的注释里写着
        # 「2026-09-30 的实况：`tools/build_home.py` 里有 12 处 f-string 反斜杠」——
        # 那是**历史教训的文字**，不是引用。把它当「悬空引用」是误报。
        # 📌 体检工具最怕噪音：**报十条有九条是假的，人就不看了**。
        #
        # 判 docstring 用最笨的办法（数三引号）：`ast` 能精确做，但这里要的是
        # 「别把说明文字当代码」，一个状态机够了 —— 漏判的代价只是多一行提示。
        引号数 = 行.count('"""') + 行.count(chr(39) * 3)
        if 引号数 % 2 == 1:
            在三引号 = not 在三引号
        if 在三引号 or 行.lstrip().startswith("#"):
            continue
        for 名, 说明 in 已退役.items():
            # 只认「像模块名一样出现」的，避免 `build_homepage` 之类的误报
            if re.search(rf"\b{re.escape(名)}\b", 行):
                出.append({"文件": p.name, "行": 行号, "引用": 名,
                           "说明": 说明, "原文": 行.strip()[:100]})
    return 出


def 查_陈旧度(p: Path) -> dict:
    """测试文件多久没动过了（按 mtime）。

    ⚠️ **只是线索，不是判决**：一条测试十年不动也可能是好测试（测的是不变的东西）。
    但它和「语言改了 30 个里程碑它一次没改」放在一起看，就有意义了。
    """
    秒 = time.time() - p.stat().st_mtime
    return {"文件": p.name, "天": int(秒 / 86400)}


def 查_实现细节(p: Path, 源码: str) -> list[dict]:
    """找「断言内部结构」的地方 —— 换实现就失去意义的测试。

    只找最典型的一种：直接读 AST 节点的属性（`node.xxx` / `ast.xxx`），
    那测的是**前端长什么样**，不是**程序行为对不对**。
    """
    出 = []
    for 行号, 行 in enumerate(源码.splitlines(), 1):
        if re.search(r"\b(ast\.\w+|节点\.\w+|node\.\w+)", 行) and "assert" in 行:
            出.append({"文件": p.name, "行": 行号, "原文": 行.strip()[:100]})
    return 出


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="测试用例体检（只报告，不删）")
    ap.add_argument("--简", action="store_true", help="只打「需要看一眼」的，不铺开")
    args = ap.parse_args(argv)

    文件们 = _测试文件们()
    if not 文件们:
        print("没找到测试文件", file=sys.stderr)
        return 2

    skips: list[dict] = []
    退役: list[dict] = []
    陈旧: list[dict] = []
    细节: list[dict] = []

    for p in 文件们:
        源码 = _读(p)
        skips += 查_skip(p, 源码)
        退役 += 查_已退役引用(p, 源码)
        细节 += 查_实现细节(p, 源码)
        陈旧.append(查_陈旧度(p))

    print(f"测试文件 {len(文件们)} 个\n")

    if 退役:
        print("=" * 60)
        print(f"🔴 引用了**已搬走/退役**的脚本（{len(退役)} 处）—— 这些测试该跟着走或删掉")
        for 项 in 退役:
            print(f"  {项['文件']}:{项['行']}  {项['引用']}  （{项['说明']}）")
            print(f"      {项['原文']}")
        print()

    if skips:
        应过期 = [s for s in skips
                if re.search(r"等|TODO|待|以后|将来|M\d+", s["原因"] or "")]
        永久 = [s for s in skips if s not in 应过期]
        print("=" * 60)
        print(f"⏳ skip / xfail 共 {len(skips)} 处")
        if 应过期:
            print(f"  🔴 其中 {len(应过期)} 处像是「**临时跳过、等做完再回来**」—— 该复查了：")
            for s in 应过期:
                print(f"    {s['文件']}:{s['行']} {s['用例']}  [{s['类型']}] {s['原因']}")
        if 永久 and not args.简:
            print(f"  ✅ 另 {len(永久)} 处看着是**永久边界**（平台差异等）：")
            for s in 永久[:8]:
                print(f"    {s['文件']}:{s['行']} {s['用例']}  {s['原因'][:60]}")
        print()

    if 细节 and not args.简:
        print("=" * 60)
        print(f"🔍 断言了「内部结构」的地方（{len(细节)} 处）—— 换实现就失去意义，看一眼")
        for 项 in 细节[:10]:
            print(f"  {项['文件']}:{项['行']}  {项['原文']}")
        if len(细节) > 10:
            print(f"  …另 {len(细节) - 10} 处")
        print()

    if not args.简:
        print("=" * 60)
        print("📅 最久没被动过的 10 个测试（只是线索：稳定 ≠ 过时）")
        for 项 in sorted(陈旧, key=lambda x: -x["天"])[:10]:
            print(f"  {项['天']:4d} 天  {项['文件']}")
        print()

    print("=" * 60)
    要动的 = len(退役) + len([s for s in skips
                           if re.search(r"等|TODO|待|以后|将来|M\d+", s["原因"] or "")])
    if 要动的:
        print(f"合计 **{要动的} 处需要看一眼**。退役流程见 docs/测试退役流程.md")
    else:
        print("没有明显该退役的。⚠️ 但这不代表没问题 —— 这个工具只找得到「有痕迹」的。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
