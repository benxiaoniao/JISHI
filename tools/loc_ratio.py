# -*- coding: utf-8 -*-
"""行数比基线：同一件事，基石版与 Python 版各要写多少行。

为什么要有它：路线图主线 C 的度量是「**方便实现意图**」——同一件事写得越短，
语言越顺手。但「短」必须可度量、可复核，不能凭感觉。所以：

- 每个 `examples/projects/<项目>/` 里，`.jsh` 是基石实现，`参考实现.py` 是
  **只用标准库**的 Python 实现，两者**同功能、同输出口径**；
- 统计口径：**有效行** = 去掉空行与纯注释行（`#` 开头）之后的行数。
  总行数一起报，方便别人换口径复核；
- 比值 = 基石有效行 ÷ Python 有效行。**越小越好**。

用法：
    python tools/loc_ratio.py              # 打表
    python tools/loc_ratio.py --markdown   # 输出 Markdown 表（贴进文档）
    python tools/loc_ratio.py --check      # 每个项目都要有 Python 参考实现，
                                           # 缺了报错（保证基线随项目增长而完整）
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT / "examples" / "projects"

#: 项目 → 场景分类（与 docs/路线图.md 主线 C 的五类场景对齐）
CATEGORY: dict[str, str] = {
    "成绩分析": "数据处理",
    "日志统计": "数据处理",
    "网页采集": "网页采集",
    "待办清单": "CLI 工具",
    "排序跑分": "算法练习",
    "基石自述站": "教学演示 / 站点生成",
}


def count_lines(path: Path) -> tuple[int, int]:
    """返回 (总行, 有效行)。有效行 = 非空且不是纯注释（`#` 开头）。"""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    useful = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    return len(lines), len(useful)


def collect() -> list[dict]:
    rows = []
    for project in sorted(p for p in PROJECTS.iterdir() if p.is_dir()):
        jsh = sorted(project.glob("*.jsh"))
        py = project / "参考实现.py"
        if not jsh and not py.exists():
            continue
        entry = {"项目": project.name,
                 "分类": CATEGORY.get(project.name, "（未分类）"),
                 "基石总": 0, "基石有效": 0, "py总": 0, "py有效": 0,
                 "基石文件": [f.name for f in jsh],
                 "有参考": py.exists()}
        for f in jsh:
            total, useful = count_lines(f)
            entry["基石总"] += total
            entry["基石有效"] += useful
        if py.exists():
            entry["py总"], entry["py有效"] = count_lines(py)
        rows.append(entry)
    return rows


def ratio(a: int, b: int) -> str:
    return f"{a / b:.2f}" if b else "—"


def render_table(rows: list[dict]) -> list[str]:
    out = ["| 项目 | 场景 | 基石（有效行） | Python（有效行） | 行数比 | 总行比 |",
           "|------|------|---------------:|-----------------:|-------:|-------:|"]
    for r in rows:
        out.append(f"| {r['项目']} | {r['分类']} | {r['基石有效']} | {r['py有效']} "
                   f"| **{ratio(r['基石有效'], r['py有效'])}** "
                   f"| {ratio(r['基石总'], r['py总'])} |")
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    rows = collect()
    missing = [r["项目"] for r in rows if not r["有参考"]]
    if "--check" in argv and missing:
        print("这些项目还没有 Python 参考实现（行数比基线不完整）：", file=sys.stderr)
        for name in missing:
            print(f"  - examples/projects/{name}/参考实现.py", file=sys.stderr)
        return 1

    if "--markdown" in argv:
        print("\n".join(render_table(rows)))
    else:
        print(f"{'项目':<12}{'场景':<20}{'基石':>6}{'Python':>8}{'行数比':>8}{'总行比':>8}")
        print("-" * 64)
        for r in rows:
            print(f"{r['项目']:<12}{r['分类']:<20}{r['基石有效']:>6}"
                  f"{r['py有效']:>8}{ratio(r['基石有效'], r['py有效']):>8}"
                  f"{ratio(r['基石总'], r['py总']):>8}")
        print("-" * 64)

    done = [r for r in rows if r["有参考"] and r["py有效"]]
    if done:
        基 = sum(r["基石有效"] for r in done)
        皮 = sum(r["py有效"] for r in done)
        平均 = sum(r["基石有效"] / r["py有效"] for r in done) / len(done)
        print()
        print(f"合计：基石 {基} 行 / Python {皮} 行 → 加权比值 **{基 / 皮:.2f}**")
        print(f"逐项算术平均：**{平均:.2f}**（{len(done)} 个项目）")
    if missing:
        print()
        print("缺 Python 参考实现的项目：" + "、".join(missing))
    print()
    print("口径：有效行 = 去空行、去纯注释行；比值越小越「方便实现意图」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
