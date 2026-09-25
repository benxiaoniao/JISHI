# -*- coding: utf-8 -*-
"""基石自述站（Python 参考实现，只用标准库）。

与「生成.jsh」同功能——用来算「基石版行数 ÷ Python 版行数」。
`样式` 与 `骨架` 两份字符串**逐字复用** .jsh 里的同一份（模板与 CSS 与语言无关，
两边付出同样行数，比值才反映「逻辑量」）。
"""

import re
import sys
from pathlib import Path

样式 = """/* 基石自述站样式（由基石生成器一并写出） */
:root {
  --bg: #f7f8fa; --fg: #1f2430; --muted: #5c6370; --line: #e4e7ec;
  --card: #ffffff; --accent: #1a5fb4; --accent-soft: #eaf1fb;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.75 "Segoe UI", "Microsoft YaHei", -apple-system, sans-serif;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
header.top {
  background: var(--card); border-bottom: 1px solid var(--line);
  padding: 14px 28px; display: flex; align-items: center; gap: 28px;
  position: sticky; top: 0; z-index: 2;
}
.brand { font-size: 20px; font-weight: 700; letter-spacing: .04em; }
.brand span { color: var(--muted); font-size: 13px; font-weight: 400; margin-left: 6px; }
nav a { margin-right: 18px; color: var(--muted); font-size: 15px; }
nav a.now { color: var(--accent); font-weight: 600; }
main { max-width: 960px; margin: 0 auto; padding: 32px 28px 64px; }
h1 { font-size: 30px; margin: 8px 0 6px; }
h2 { font-size: 20px; margin: 34px 0 12px; padding-bottom: 6px;
     border-bottom: 1px solid var(--line); }
.lead { color: var(--muted); font-size: 17px; margin: 0 0 26px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
         gap: 14px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
        padding: 16px 18px; }
.card .k { color: var(--muted); font-size: 13px; }
.card .v { font-size: 26px; font-weight: 700; margin-top: 4px; }
.card .v small { font-size: 14px; font-weight: 400; color: var(--muted); }
table { width: 100%; border-collapse: collapse; background: var(--card);
        border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--line);
         vertical-align: top; font-size: 15px; }
th { background: var(--accent-soft); font-weight: 600; }
tr:last-child td { border-bottom: none; }
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.bar { background: var(--accent-soft); border-radius: 4px; height: 10px; min-width: 3px; }
code { background: #eef1f5; padding: 1px 5px; border-radius: 4px;
       font-family: "Cascadia Code", Consolas, monospace; font-size: 14px; }
pre { background: #1f2430; color: #e6e9ef; padding: 14px 16px; border-radius: 8px;
      overflow-x: auto; }
pre code { background: none; color: inherit; padding: 0; }
footer { border-top: 1px solid var(--line); color: var(--muted); font-size: 13px;
         padding: 18px 28px 40px; max-width: 960px; margin: 0 auto; }
.tag { display: inline-block; background: var(--accent-soft); color: var(--accent);
       border-radius: 999px; padding: 1px 10px; font-size: 12px; margin-right: 6px; }
"""

骨架 = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{0} · 基石自述</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<header class="top">
  <div class="brand">基石<span>jishi</span></div>
  <nav>{1}</nav>
</header>
<main>
{2}
</main>
<footer>{3}</footer>
</body>
</html>
"""


def 取值(text, left, right):
    start = text.find(left)
    if start < 0:
        return ""
    rest = text[start + len(left):]
    end = rest.find(right)
    return rest if end < 0 else rest[:end]


def 转义(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def 简单排版(text):
    parts = 转义(text).replace("**", "").split("`")
    out = parts[0]
    for i, part in enumerate(parts[1:], 1):
        out += ("<code>" if i % 2 else "</code>") + part
    return out


def 找行(path, keyword):
    for line in path.read_text(encoding="utf-8").splitlines():
        if keyword in line:
            return line
    return ""


def 读里程碑(path):
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| **M"):
            continue
        cells = line.split("|")
        if len(cells) < 5:
            continue
        rows.append([cells[1].strip(), cells[2].strip(), cells[3].strip()])
    return rows


def 规模(path):
    rows = [[len(f.read_text(encoding="utf-8").splitlines()), f.name]
            for f in sorted(path.glob("*.py"))]
    rows.sort()
    return rows


def 数文件(root, suffix):
    return len(list(root.rglob(f"*{suffix}")))


def 数用例(path):
    total = 0
    for f in sorted(path.glob("test_*.py")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.startswith("def test") or line.startswith("    def test"):
                total += 1
    return total


def 采集(root):
    data = {"版本": 取值(找行(root / "pyproject.toml", "version ="), '"', '"'),
            "测试数": 取值((root / "README.md").read_text(encoding="utf-8"),
                           "测试现状：", " 项"),
            "探针": 取值((root / "README.md").read_text(encoding="utf-8"),
                         "（当前 **", "** 可用"),
            "模块": 规模(root / "jishi"),
            "示例数": 数文件(root / "examples", ".jsh"),
            "用例数": 数用例(root / "tests"),
            "里程碑": 读里程碑(root / "PROGRESS.md")}
    return data


def 导航(current):
    items = [["index.html", "概览"], ["里程碑.html", "里程碑"],
             ["规模.html", "代码规模"], ["上手.html", "上手"]]
    out = ""
    for name, label in items:
        css = ' class="now"' if name == current else ""
        out += f'<a{css} href="{name}">{label}</a>'
    return out


def 页面(title, current, body, footer):
    return 骨架.format(title, 导航(current), body, footer)


def 卡片(name, value, extra):
    return (f'<div class="card"><div class="k">{name}</div>'
            f'<div class="v">{value} <small>{extra}</small></div></div>')


def 总行数(rows):
    return sum(row[0] for row in rows)


def 首页(data):
    cards = '<div class="cards">'
    cards += 卡片("当前版本", data["版本"], "")
    cards += 卡片("测试用例", data["测试数"], "项全绿")
    cards += 卡片("能力探针", data["探针"], "可用 / 50 项")
    cards += 卡片("里程碑", f"{len(data['里程碑'])}", "个已完成")
    cards += 卡片("模块规模", f"{总行数(data['模块'])}", "行 Python")
    cards += 卡片("示例程序", f"{data['示例数']}", "个 .jsh")
    cards += "</div>"

    recent = "<h2>最近的里程碑</h2>"
    recent += "<table><tr><th>里程碑</th><th>内容</th><th>完成</th></tr>"
    for row in data["里程碑"][:3]:
        recent += (f"<tr><td>{转义(row[0])}</td><td>{简单排版(row[1])}</td>"
                   f'<td class="num">{转义(row[2])}</td></tr>')
    recent += "</table>"

    body = ("<h1>基石 · 自述</h1>"
            '<p class="lead">一门中文编程语言，对标 Python 的易用性。'
            "北极星是<b>方便地实现意图</b>与<b>出问题能快速定位</b>——"
            "后者靠报错、调用栈和调试器三件东西兜住。</p>"
            + cards
            + "<h2>这个页面是谁生成的</h2>"
            + "<p>整套页面由 <code>examples/projects/基石自述站/生成.jsh</code>"
            + " 生成——它本身就是一个用基石写的程序，读的是仓库里的真数据。</p>"
            + recent)
    return 页面("概览", "index.html", body, f"由生成器产出 · 版本 {data['版本']}")


def 里程碑页(data):
    table = "<table><tr><th>里程碑</th><th>内容</th><th>完成</th></tr>"
    for row in data["里程碑"]:
        table += (f"<tr><td>{转义(row[0])}</td><td>{简单排版(row[1])}</td>"
                  f'<td class="num">{转义(row[2])}</td></tr>')
    table += "</table>"
    body = ("<h1>里程碑</h1>"
            + f'<p class="lead">全部 {len(data["里程碑"])} 个已完成里程碑，'
            + "最新的排在最前面。这张表直接来自 <code>PROGRESS.md</code> 的时间线。</p>"
            + table)
    return 页面("里程碑", "里程碑.html", body, "里程碑 · 由生成器产出")


def 规模页(data):
    rows = data["模块"]
    biggest = rows[-1][0] if rows else 1
    table = "<table><tr><th>模块</th><th>行数</th><th>占比</th></tr>"
    for count, name in reversed(rows):
        width = int(count * 100 / biggest)
        table += (f"<tr><td><code>jishi/{转义(name)}</code></td>"
                  f'<td class="num">{count}</td>'
                  f'<td><div class="bar" style="width:{width}%"></div></td></tr>')
    table += "</table>"
    body = ("<h1>代码规模</h1>"
            + '<p class="lead">统计范围：<code>jishi/</code> 下这一层的每个 '
            + "<code>.py</code>。</p>" + table
            + "<h2>测试与示例</h2>"
            + '<div class="cards">'
            + 卡片("测试函数", f"{data['用例数']}", "个 def test")
            + 卡片("示例程序", f"{data['示例数']}", "个 .jsh")
            + 卡片("模块行数", f"{总行数(rows)}", "行（本层）")
            + "</div>")
    return 页面("代码规模", "规模.html", body, "代码规模 · 由生成器产出")


def 上手页(data):
    body = ("<h1>上手</h1>"
            + '<p class="lead">四个命令就能用起来（当前版本 '
            + data["版本"] + "）。</p>"
            + "<h2>1. 安装</h2><pre><code>pip install 基石</code></pre>"
            + "<h2>2. 跑第一个程序</h2>"
            + '<pre><code>函数 问候(名字)：\n    返回 `你好，{名字}！`\n\n'
            + '打印(问候("世界"))</code></pre>'
            + "<h2>3. 出错了怎么办</h2>"
            + "<p>基石把报错写成「说清原因 + 给出能照着改的提示」。</p>"
            + "<h2>4. 调不动的时候用调试器</h2>"
            + "<pre><code>jishi 调试 脚本.jsh --断点 12</code></pre>"
            + "<p>树遍历与字节码两个执行器都能调试。</p>")
    return 页面("上手", "上手.html", body, "上手 · 由生成器产出")


def main():
    args = sys.argv[1:]
    root = Path(args[1]) if len(args) > 1 else Path(".")
    out_dir = Path(args[0]) if args else Path("site") / "基石自述"

    print("基石自述站：正在读仓库数据……")
    data = 采集(root)
    print(f'  版本 {data["版本"]} · 测试 {data["测试数"]} 项 · 探针 {data["探针"]}')
    print(f'  里程碑 {len(data["里程碑"])} 个 · 模块 {len(data["模块"])} 个'
          f' · 示例 {data["示例数"]} 个')

    out_dir.mkdir(parents=True, exist_ok=True)
    pages = [("style.css", 样式), ("index.html", 首页(data)),
             ("里程碑.html", 里程碑页(data)), ("规模.html", 规模页(data)),
             ("上手.html", 上手页(data))]
    for name, text in pages:
        (out_dir / name).write_text(text, encoding="utf-8")
        if name != "style.css":
            print(f"    {name}（{len(text.splitlines())} 行）")
    print(f"  已写出 4 个页面 + 1 个样式表 → {out_dir}")


main()
