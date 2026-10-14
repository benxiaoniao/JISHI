#!/usr/bin/env python3
"""绕道台账：把探针项目里的「绕道清单」汇总成一份**可复核**的事实来源。

用法::

    python tools/detour_ledger.py             # 生成 docs/绕道台账.md
    python tools/detour_ledger.py --check     # 只自检，不写文件

## 为什么要「扫出来」，不手写

台账要是手抄的，就会与代码脱节：补了没更新、删了还留着。这里把台账做成
**代码的投影** —— 条目写在**撞到它的那个项目的 README** 里（人的第一手记录），
台账由本脚本汇总。于是台账里每一条都能指回「哪个项目的哪一行」，
而「这条还成立吗」可以机器判（见下）。

## 自检的四条（`--check`）

| # | 检查 | 为什么 |
|---|---|---|
| 1 | 编号全仓库唯一 | 编号是台账的引用锚点，重了就没法指 |
| 2 | `位置=<文件>:<行>` 的文件存在、行号在范围内 | 位置是「可复核」的全部意义 |
| 3 | `缺=<模块>.<函数>` —— 该函数**现在不该存在** | 补上了却还标着「缺」，说明台账过期了（**会过期报红**） |
| 4 | `增强=<模块>.<函数>` / `新增模块=<名>` 同理 | 同理：目标已存在，条目就该消掉或改写 |

第 3/4 条是本文件的要点：**「已经能做」与「还做不了」必须分得开** ——
和 `AGENTS.md` 里「豁免必须显式、可复核、会过期报红」是同一条纪律。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBES = ROOT / "examples" / "绕道探针"
STDLIB = ROOT / "oracle" / "jishi" / "stdlib"
LEDGER = ROOT / "docs" / "绕道台账.md"

#: 条目行：`- [编号] <前缀>=<目标> [位置=<文件>:<行>] | <说明>`
ENTRY_RE = re.compile(
    r"^-\s*\[([A-Za-z]+\d+)\]\s*"
    r"(缺|增强|新增模块)=(\S+?)\s*"
    r"(?:位置=(\S+?)\s*)?"
    r"\|\s*(.+?)\s*$"
)

#: README 里「绕道清单」小节的标题（扫描从这里开始，到下一个小节为止）
SECTION = "## 绕道清单"


def stdlib_api() -> dict[str, set[str]]:
    """`{模块名: {函数名, …}}` —— 直接扫 `oracle/jishi/stdlib/*.py` 的源码。

    ⚠️ **不导入 `oracle.jishi` 包**（那会拉起解释器的一整套初始化）。
    这里只读文件 + `ast`，与 `oracle/jishi/ai.py` 的 `--lang-spec` 同源同口径：
    有状态模块（`日志` / `测试`）把函数放在 `__基石新建__` 工厂里，要往里再看一层。
    """
    api: dict[str, set[str]] = {}
    for path in sorted(STDLIB.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        names: set[str] = set()

        def 收(节点们) -> None:
            for node in 节点们:
                if isinstance(node, ast.FunctionDef):
                    if node.name == "__基石新建__":
                        收(node.body)
                    elif not node.name.startswith("_"):
                        names.add(node.name)

        收(tree.body)
        api[path.stem] = names
    return api


def parse_section(readme: Path) -> list[dict]:
    """从一份 README 里抽「绕道清单」小节的条目。"""
    出: list[dict] = []
    在节里 = False
    for line in readme.read_text(encoding="utf-8").splitlines():
        if line.startswith(SECTION):
            在节里 = True
            continue
        if 在节里 and line.startswith("## "):
            break                       # 到下一个小节为止
        if not 在节里:
            continue
        m = ENTRY_RE.match(line)
        if m:
            出.append({
                "编号": m.group(1),
                "类型": m.group(2),
                "目标": m.group(3),
                "位置": m.group(4) or "",
                "说明": m.group(5),
                "项目": readme.parent.name,
                "来源": f"{readme.parent.name}/README.md",
            })
    return 出


def collect() -> list[dict]:
    """扫所有探针项目，按编号排序返回全部条目。"""
    if not PROBES.is_dir():
        return []
    出: list[dict] = []
    for readme in sorted(PROBES.glob("*/README.md")):
        出.extend(parse_section(readme))
    return sorted(出, key=lambda e: e["编号"])


def check(entries: list[dict]) -> list[str]:
    """自检，返回问题清单（空 = 全过）。"""
    api = stdlib_api()
    problems: list[str] = []

    seen: dict[str, str] = {}
    for e in entries:
        if e["编号"] in seen:
            problems.append(
                f"编号重复：{e['编号']}（{seen[e['编号']]} 与 {e['来源']}）")
        seen[e["编号"]] = e["来源"]

        # 2) 位置可复核
        if e["位置"]:
            if ":" not in e["位置"]:
                problems.append(f"{e['编号']} 的位置要写成 文件:行，现在是「{e['位置']}」")
            else:
                文件名, _, 行号 = e["位置"].rpartition(":")
                目标 = PROBES / e["项目"] / 文件名
                if not 目标.is_file():
                    problems.append(f"{e['编号']} 指的文件不存在：{目标.relative_to(ROOT)}")
                elif not 行号.isdigit() or int(行号) < 1:
                    problems.append(f"{e['编号']} 的行号不是正整数：「{行号}」")
                elif int(行号) > len(目标.read_text(encoding="utf-8").splitlines()):
                    problems.append(
                        f"{e['编号']} 的行号 {行号} 超出 "
                        f"{目标.relative_to(ROOT)} 的行数")

        # 3/4) 「还做不了」要对得上现状 —— 这就是「会过期报红」
        类型, 目标名 = e["类型"], e["目标"]
        if 类型 == "新增模块":
            if 目标名 in api:
                problems.append(
                    f"{e['编号']} 标着「新增模块 {目标名}」，但标准库里**已经有**这个模块了 "
                    f"—— 绕道消掉了，去更新 {e['来源']}")
        else:                                   # 缺 / 增强
            if "." not in 目标名:
                problems.append(f"{e['编号']} 的 {类型}= 要写成 模块.函数，现在是「{目标名}」")
                continue
            模块, _, 函数 = 目标名.partition(".")
            if 模块 not in api:
                problems.append(
                    f"{e['编号']} 指向的模块「{模块}」不存在（看看是不是名字写错了）")
                continue
            存在 = 函数 in api[模块]
            if 类型 == "缺" and 存在:
                problems.append(
                    f"{e['编号']} 标着「缺 {目标名}」，但这个函数**已经有了** "
                    f"—— 绕道消掉了，去更新 {e['来源']}")
            if 类型 == "增强" and not 存在:
                problems.append(
                    f"{e['编号']} 标着「增强 {目标名}」，但这个函数**还不存在** "
                    f"—— 那应该写成「缺 {目标名}」")
    return problems


def counts(entries: list[dict]) -> dict[str, int]:
    出 = {"缺": 0, "增强": 0, "新增模块": 0}
    for e in entries:
        出[e["类型"]] += 1
    return 出


def render(entries: list[dict]) -> str:
    n = counts(entries)
    行 = [
        "# 绕道台账",
        "",
        "> ⚠️ **这份台账是扫出来的，不要手改。**",
        "> 条目写在**撞到它的那个项目的 README**里（`examples/绕道探针/*/README.md` 的",
        "> 「绕道清单」小节），本文件由 `python tools/detour_ledger.py` 汇总生成。",
        "> 自检：`python tools/detour_ledger.py --check`（判据在 `tests/test_m105_detour_ledger.py`）。",
        "",
        f"**现在共 {len(entries)} 条**：缺（要一个还不存在的函数）{n['缺']} · "
        f"增强（函数在、但不够用）{n['增强']} · 新增模块 {n['新增模块']}。",
        "",
        "## 明细",
        "",
        "| 编号 | 类型 | 目标 | 位置 | 说明 |",
        "|---|---|---|---|---|",
    ]
    for e in entries:
        位置 = f"`{e['项目']}/{e['位置']}`" if e["位置"] else "—（写不出来）"
        行.append(f"| {e['编号']} | {e['类型']} | `{e['目标']}` | {位置} | {e['说明']} |")

    # 按「要做什么」分组 —— 这是 M106–M109 的采购单
    行 += ["", "## 按结论看（这就是接下来要补的）", ""]
    新模块: dict[str, list[str]] = {}
    加深: dict[str, list[str]] = {}
    for e in entries:
        (新模块 if e["类型"] == "新增模块" else 加深).setdefault(
            e["目标"], []).append(e["编号"])

    行.append("### 新增大类（撞到几处）")
    行.append("")
    if 新模块:
        for 名, 编号们 in sorted(新模块.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            行.append(f"- **{名}** —— {len(编号们)} 处（{'、'.join(编号们)}）")
    else:
        行.append("- （还没有）")
    行 += ["", "### 现有模块要加深 / 补函数", ""]
    if 加深:
        for 名, 编号们 in sorted(加深.items()):
            行.append(f"- `{名}` —— {'、'.join(编号们)}")
    else:
        行.append("- （还没有）")
    行 += [
        "",
        "> 📌 上面这些**都还不是承诺**：进哪一批、做不做，照**内部路线图（未公开）**§12.1 的",
        "> 准入三关（真实需求驱动 / 可度量验收 / 投入产出比）走。本台账只干一件事 ——",
        "> 把「哪里真的绕道了」记成**可复核**的事实。",
    ]
    return "\n".join(行) + "\n"


def main(argv: list[str]) -> int:
    entries = collect()
    if not entries:
        print("⚠️ 没扫到任何绕道条目 —— 看看 examples/绕道探针/*/README.md 还在不在",
              file=sys.stderr)
        return 1

    problems = check(entries)
    if problems:
        print("❌ 台账自检没过：", file=sys.stderr)
        for p in problems:
            print(f"   - {p}", file=sys.stderr)
        return 1

    n = counts(entries)
    print(f"✓ 绕道 {len(entries)} 条"
          f"（缺 {n['缺']} · 增强 {n['增强']} · 新增模块 {n['新增模块']}），自检过")

    if "--check" in argv:
        return 0

    LEDGER.write_text(render(entries), encoding="utf-8", newline="\n")
    print(f"  已写入 {LEDGER.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
