# -*- coding: utf-8 -*-
"""基石文档站生成器（零第三方依赖）。

把 README + docs/ 下的语言规格、设计决策、教程 7 章，渲染成带侧边栏的
中文静态 HTML 站，输出到 site/（已 gitignore，属构建产物）。

用法：
    python tools/build_site.py           生成全部页面
    python tools/build_site.py --open    生成后在默认浏览器打开首页

支持的子集（够本项目文档用）：
    标题(#~######)、围栏代码块、表格、引用块、无序/有序列表(含嵌套)、
    行内代码、粗体/斜体、[文字](链接) 与站内相对链接改写。
"""

from __future__ import annotations

import html
import os
import re
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
OUT = ROOT / "site"

#: 站内页面清单：(相对 .md 的路径, 侧边栏标题, 分组)
PAGES: list[tuple[Path, str, str]] = [
    (Path("README.md"), "概览", "开始"),
    (Path("docs/语言规格.md"), "语言规格", "参考"),
    (Path("docs/设计决策.md"), "设计决策", "参考"),
    (Path("docs/路线图.md"), "路线图（M7–M11）", "参考"),
    (Path("docs/ai/system-prompt.md"), "AI 系统提示词", "AI"),
    (Path("docs/ai/prompt-guide.md"), "AI 提示词指南", "AI"),
    (Path("examples/ai/few-shot.md"), "few-shot 示例库", "AI"),
    (Path("docs/tutorial/01_你好基石.md"), "01 你好基石", "教程"),
    (Path("docs/tutorial/02_数与文本.md"), "02 数与文本", "教程"),
    (Path("docs/tutorial/03_判断与循环.md"), "03 判断与循环", "教程"),
    (Path("docs/tutorial/04_函数.md"), "04 函数", "教程"),
    (Path("docs/tutorial/05_字典与结构化数据.md"), "05 字典与结构化数据", "教程"),
    (Path("docs/tutorial/06_文件与标准库.md"), "06 文件与标准库", "教程"),
    (Path("docs/tutorial/07_综合实战.md"), "07 综合实战", "教程"),
]
for _i, (_p, _t, _g) in enumerate(PAGES):
    if not _p.is_absolute():
        PAGES[_i] = (ROOT / _p, _t, _g)

CSS = """\
:root{--bg:#fff;--fg:#1f2430;--muted:#5c6370;--accent:#0b5fff;--code-bg:#f6f8fa;
--border:#e4e7ec;--nav-bg:#fafbfc;--radius:8px}
*{box-sizing:border-box}body{margin:0;font:16px/1.7 -apple-system,"Segoe UI",
"Microsoft YaHei",sans-serif;color:var(--fg);background:var(--bg)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.layout{display:flex;min-height:100vh}
nav.side{width:260px;flex:0 0 260px;background:var(--nav-bg);border-right:1px solid var(--border);
padding:20px 0;position:sticky;top:0;height:100vh;overflow-y:auto}
nav.side .brand{padding:0 20px 14px;font-weight:700;font-size:18px}
nav.side .brand small{display:block;font-weight:400;color:var(--muted);font-size:12px}
nav.side .group{padding:6px 20px;color:var(--muted);font-size:12px;letter-spacing:.08em}
nav.side a{display:block;padding:4px 20px;color:var(--fg);font-size:14px}
nav.side a.active{background:#e8f0ff;color:var(--accent);font-weight:600;border-right:3px solid var(--accent)}
main{flex:1;max-width:880px;padding:28px 48px 80px;margin:0 auto}
main h1{font-size:30px;border-bottom:1px solid var(--border);padding-bottom:12px;margin-top:8px}
main h2{font-size:23px;margin-top:36px;padding-top:8px}
main h3{font-size:19px;margin-top:26px}
main h2,main h3{scroll-margin-top:16px}
main h2 a,main h3 a,h4 a{color:inherit;text-decoration:none}
main h2:hover a::before,main h3:hover a::before,main h4:hover a::before{content:"# ";color:var(--accent)}
pre{background:var(--code-bg);border:1px solid var(--border);border-radius:var(--radius);
padding:14px 16px;overflow-x:auto;font-size:13.5px;line-height:1.55}
code{font-family:"JetBrains Mono",Consolas,monospace;background:var(--code-bg);
padding:2px 5px;border-radius:4px;font-size:.9em}
pre code{background:none;padding:0;font-size:13.5px}
pre .lang-tag{display:inline-block;color:var(--muted);font-size:11px;margin-bottom:8px}
table{border-collapse:collapse;margin:16px 0;width:100%;font-size:14.5px}
th,td{border:1px solid var(--border);padding:7px 12px;text-align:left}
th{background:var(--nav-bg)}
blockquote{margin:16px 0;padding:2px 18px;border-left:4px solid var(--accent);
background:var(--nav-bg);border-radius:0 var(--radius) var(--radius) 0}
blockquote p{margin:.5em 0}
ul,ol{padding-left:26px}
li{margin:3px 0}hr{border:none;border-top:1px solid var(--border);margin:28px 0}
.pager{display:flex;justify-content:space-between;margin-top:60px;padding-top:16px;
border-top:1px solid var(--border);color:var(--muted);font-size:14px}
.pager span.empty{color:#c8ccd4}
@media(max-width:900px){nav.side{display:none}main{padding:20px}}
"""

# --- 行级解析 --------------------------------------------------------------

_INLINE = [
    # 行内代码（先转义）
    (re.compile(r"`([^`]+)`"), lambda m: "<code>" + html.escape(m.group(1)) + "</code>"),
    # [文字](目标)
    (re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)"), None),  # 链接在下方处理
    # **粗体**
    (re.compile(r"\*\*([^*]+)\*\*"), lambda m: "<strong>" + m.group(1) + "</strong>"),
    # *斜体*
    (re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)"), lambda m: "<em>" + m.group(1) + "</em>"),
]


def _link_repl(m: re.Match, page: Path) -> str:
    text, target = m.group(1), m.group(2)
    if target.startswith(("http://", "https://", "#", "mailto:")):
        href = target
    else:
        # 站内相对链接：按 .md 相对 ROOT 解析，再换算成页面间的相对路径
        src_abs = (ROOT / page).resolve()
        dst_abs = (src_abs.parent / target).resolve()
        src_html = (OUT / page).with_suffix(".html").resolve()
        href = None
        for p, _t, _g in PAGES:
            if (ROOT / p).resolve() == dst_abs:
                dst_html = (OUT / p).with_suffix(".html").resolve()
                href = dst_html.relative_to(src_html.parent).as_posix()
                break
        if href is None:
            href = target  # 指向站外或本站没有的文件，原样保留
    return f'<a href="{html.escape(href)}">{text}</a>'


def inline(text: str, page: Path) -> str:
    text = html.escape(text)
    # 图片（必须在链接之前处理：![alt](src) 也匹配链接模式）
    text = re.sub(
        r"!\[([^\]\n]*)\]\(([^)\s]+)\)",
        lambda m: (f'<img src="{html.escape(m.group(2))}" '
                   f'alt="{html.escape(m.group(1))}" '
                   f'style="max-width:100%">'),
        text)
    # 链接
    text = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)",
                  lambda m: _link_repl(m, page), text)
    for pat, fn in _INLINE:
        if fn:
            text = pat.sub(fn, text)
    return text


def slugify(title: str, used: set[str]) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", title).strip("-")
    s = s or "section"
    n, base = 2, s
    while s in used:
        s = f"{base}-{n}"
        n += 1
    used.add(s)
    return s


def render_md(text: str, page: Path) -> str:
    """Markdown（子集）→ HTML 正文。"""
    lines = text.split("\n")
    out: list[str] = []
    used_anchors: set[str] = set()
    i, n = 0, len(lines)
    headers = []

    def flush_para(buf: list[str]) -> None:
        if buf:
            out.append("<p>" + inline(" ".join(x.strip() for x in buf), page) + "</p>")
            buf.clear()

    para: list[str] = []
    while i < n:
        line = lines[i]
        # 块级 HTML 直通（如 README 顶部的 <div align="center"> / <img>）：
        # 手写解析器不做完整 HTML 解析，但对这几个安全标签原样放行，
        # 其余 HTML 仍走转义（防止注入）
        stripped = line.strip()
        if stripped.startswith(("<div", "</div", "<img ", "<br", "<p ", "<center")):
            flush_para(para)
            out.append(stripped)
            i += 1
            continue
        # 围栏代码块
        if line.startswith("```"):
            flush_para(para)
            lang = line[3:].strip()
            i += 1
            buf: list[str] = []
            while i < n and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # 跳过闭合
            code = html.escape("\n".join(buf))
            tag = f'<span class="lang-tag">{html.escape(lang or "基石")}</span>' if lang else ""
            out.append(f"<pre>{tag}<code>{code}</code></pre>")
            continue
        # 标题
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            flush_para(para)
            level, title = len(m.group(1)), m.group(2)
            sid = slugify(title, used_anchors)
            h = f'<h{level} id="{sid}">'
            h += inline(title, page) + f"</h{level}>"
            out.append(h)
            i += 1
            continue
        # 表格（连续的 | 行，第二行为分隔 --- | ---）
        if line.lstrip().startswith("|") and i + 1 < n and re.match(
                r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]):
            flush_para(para)
            head = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            rows: list[list[str]] = []
            while i < n and lines[i].lstrip().startswith("|"):
                rows.append([c.strip() for c in
                             lines[i].strip().strip("|").split("|")])
                i += 1
            tbl = ["<table><thead><tr>"]
            tbl += [f"<th>{inline(c, page)}</th>" for c in head]
            tbl.append("</tr></thead><tbody>")
            for r in rows:
                tbl.append("<tr>" +
                           "".join(f"<td>{inline(c, page)}</td>" for c in r) +
                           "</tr>")
            tbl.append("</tbody></table>")
            out.append("".join(tbl))
            continue
        # 引用块
        if line.startswith(">"):
            flush_para(para)
            quote: list[str] = []
            while i < n and lines[i].startswith(">"):
                quote.append(lines[i][1:].strip())
                i += 1
            out.append("<blockquote><p>" +
                       inline(" ".join(quote), page) + "</p></blockquote>")
            continue
        # 无序列表
        if re.match(r"^\s*[-*]\s+", line):
            flush_para(para)
            out.append("<ul>")
            while i < n and re.match(r"^\s*[-*]\s+", lines[i]):
                out.append("<li>" + inline(
                    re.sub(r"^\s*[-*]\s+", "", lines[i]), page) + "</li>")
                i += 1
            out.append("</ul>")
            continue
        # 有序列表
        if re.match(r"^\s*\d+[.、]\s+", line):
            flush_para(para)
            out.append("<ol>")
            while i < n and re.match(r"^\s*\d+[.、]\s+", lines[i]):
                out.append("<li>" + inline(
                    re.sub(r"^\s*\d+[.、]\s+", "", lines[i]), page) + "</li>")
                i += 1
            out.append("</ol>")
            continue
        # 分隔线
        if re.match(r"^\s*(---+|\*\*\*+)\s*$", line):
            flush_para(para)
            out.append("<hr>")
            i += 1
            continue
        # 空行
        if not line.strip():
            flush_para(para)
            i += 1
            continue
        para.append(line)
        i += 1
    flush_para(para)
    return "\n".join(out)


def build_page(md_path: Path, title: str, group: str,
               idx: int) -> str:
    text = md_path.read_text(encoding="utf-8")
    body = render_md(text, md_path)

    # 侧边栏 / 翻页链接：相对当前页（html 文件所在目录）
    cur_html = (OUT / md_path.relative_to(ROOT)).with_suffix(".html")
    cur_dir = cur_html.parent

    def rel_to(j: int) -> str:
        target = (OUT / PAGES[j][0].relative_to(ROOT)).with_suffix(".html")
        return os.path.relpath(target, cur_dir).replace("\\", "/")

    nav: list[str] = []
    last_group = None
    for j, (_p, t, g) in enumerate(PAGES):
        if g != last_group:
            nav.append(f'<div class="group">{g}</div>')
            last_group = g
        active = ' class="active"' if j == idx else ""
        nav.append(f'<a{active} href="{rel_to(j)}">{t}</a>')

    prev, nxt = "", ""
    if idx > 0:
        prev = f'<a href="{rel_to(idx-1)}">← {PAGES[idx-1][1]}</a>'
    else:
        prev = '<span class="empty"></span>'
    if idx < len(PAGES) - 1:
        nxt = f'<a href="{rel_to(idx+1)}">{PAGES[idx+1][1]} →</a>'
    else:
        nxt = '<span class="empty"></span>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · 基石语言</title>
<style>{CSS}</style>
</head>
<body>
<div class="layout">
<nav class="side">
  <div class="brand">基石语言<small>jishi · 中文编程语言</small></div>
  {''.join(nav)}
</nav>
<main>
{body}
<div class="pager"><span>{prev}</span><span>{nxt}</span></div>
</main>
</div>
</body>
</html>"""


#: 各页面的一句话描述（llms.txt 索引用）
PAGE_DESC = {
    "README.md": "项目概览、能力清单、架构、环境与测试",
    "docs/语言规格.md": "完整语法与语义规范（关键字/语句/表达式/标准库/报错）",
    "docs/设计决策.md": "关键设计权衡与踩坑记录",
    "docs/路线图.md": "M7–M11 后续规划",
    "docs/ai/system-prompt.md": "教 LLM 写基石的官方系统提示词",
    "examples/ai/few-shot.md": "20 个「任务描述 → 基石代码」few-shot 示例对",
}


def build_llms_txt() -> str:
    """生成 llms.txt（索引），遵循 llmstxt.org 规范。"""
    lines = [
        "# 基石语言（jishi）",
        "",
        "> 一门像 Python 一样简单易用的中文编程语言：中文关键字、中文报错、"
        "三执行器（树遍历 / Python VM / C VM）语义一致。",
        "",
        "## 文档",
        "",
    ]
    for md_path, title, group in PAGES:
        rel = md_path.relative_to(ROOT)
        href = rel.with_suffix(".html").as_posix()
        desc = PAGE_DESC.get(rel.as_posix(), "教程章节" if group == "教程" else group)
        lines.append(f"- [{title}]({href})：{desc}")
    return "\n".join(lines) + "\n"


def build_llms_full() -> str:
    """生成 llms-full.txt：所有页面 Markdown 原文拼接（LLM 全文语料）。"""
    parts = ["# 基石语言（jishi）—— 完整文档\n"]
    for md_path, title, _group in PAGES:
        parts.append(f"\n\n---\n\n## {title}\n")
        parts.append(md_path.read_text(encoding="utf-8"))
    return "".join(parts)


def main() -> int:
    if not DOCS.exists():
        print("没找到 docs/，请在项目根目录运行")
        return 1

    for i, (md_path, title, group) in enumerate(PAGES):
        rel_html = (OUT / md_path.relative_to(ROOT)).with_suffix(".html")
        rel_html.parent.mkdir(parents=True, exist_ok=True)
        rel_html.write_text(
            build_page(md_path, title, group, i),
            encoding="utf-8")
        print(f"生成 {rel_html}")

    # llms.txt（索引）+ llms-full.txt（全文），供 LLM 爬取结构化语料
    (OUT / "llms.txt").write_text(build_llms_txt(), encoding="utf-8")
    (OUT / "llms-full.txt").write_text(build_llms_full(), encoding="utf-8")
    print("生成 site/llms.txt + site/llms-full.txt")

    # 品牌资产：README 引用了 assets/ 下的图片，复制过去才能正常显示
    assets_src = ROOT / "assets"
    if assets_src.exists():
        assets_dst = OUT / "assets"
        assets_dst.mkdir(parents=True, exist_ok=True)
        for f in assets_src.iterdir():
            if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".svg", ".ico"):
                (assets_dst / f.name).write_bytes(f.read_bytes())
        print(f"复制品牌资产 → {assets_dst}")

    # Playground（M10.3）：浏览器试玩页
    try:
        from build_playground import build as build_pg
        build_pg()
    except Exception as e:  # noqa: BLE001
        print(f"生成 Playground 失败（可忽略）：{e}")

    print(f"\n完成：{len(PAGES)} 个页面 → {OUT}")
    if "--open" in sys.argv:
        webbrowser.open((OUT / "README.html").as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
