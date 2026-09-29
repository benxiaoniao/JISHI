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
    # ⚠️ **`docs/路线图.md` 故意不在这里**（2026-09-29 用户要求）。
    #
    # 它里面有**自建分发服务器的实例 ID / 公网 IP / 备案与运维细节**，
    # 属于「与基石语言本身无关」的隐私。三条通道都已封住：
    #   ① GitHub —— `tools/publish_github.py` 把它从快照里整个删掉；
    #   ② **站点 —— 就是不放进 PAGES**（这条以前漏了：本地构建会把整个路线图
    #      渲染进 `site/`，一部署就等于公开发布）；
    #   ③ 别的文档引用它 —— 发布时正则替换成「内部路线图（未公开）」。
    # 想在本地看路线图，直接读 `docs/路线图.md`，**不要**加回这里。
    (Path("docs/调试器.md"), "调试器", "参考"),
    (Path("docs/工具链.md"), "工具链（LSP/REPL/格式化）", "参考"),
    (Path("docs/真实项目.md"), "真实项目与行数比", "参考"),
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

    # 「基石自述」那一套页面是**用基石语言自己写**的生成器产出的
    # （examples/projects/基石自述站/生成.jsh）。它跑过就把链接挂进导航，
    # 没跑过就不挂（不让文档站出现死链）。
    self_site = OUT / "基石自述" / "index.html"
    if self_site.exists():
        rel = os.path.relpath(self_site, cur_dir).replace("\\", "/")
        nav.append('<div class="group">自述</div>')
        nav.append(f'<a href="{rel}">基石自述（基石写的）</a>')

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
    "docs/路线图.md": "唯一的路线图：已完成总表 + 主线 A/B/C 规划 + 不做清单 + 度量体系",
    "docs/调试器.md": "调试器用法与命令表：断点 / 单步 / 变量 / 调用栈（含已知边界）",
    "docs/工具链.md": "LSP 增量同步 / REPL 增强 / 格式化器的实测数据与已知边界",
    "docs/真实项目.md": "五类真实项目 + 行数比基线（口径、逐项数据）+ 别扭点清单",
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


def _site_stats() -> dict:
    """官网上要显示的数字 —— **从代码里读**，不手写（手写的一过期就是错的）。"""
    sys.path.insert(0, str(ROOT))
    from jishi.ai import build_lang_spec
    from jishi.version import VERSION

    spec = build_lang_spec()
    return {
        "版本": VERSION,
        "模块数": len(spec["stdlib"]),
        "函数数": sum(len(m["functions"]) for m in spec["stdlib"]),
        "内建数": len(spec["builtins"]),
        "关键字数": len(spec["keywords"]),
    }


#: ICP 备案号 —— **法定要求**：网站主页最下方必须展示，并链接到工信部备案系统。
#: 这份信息是**公开**的（备案本身就是公开登记），所以进仓库没问题；
#: 但**服务器地址 / 实例 ID / 备案主体信息**绝不能进仓库（见 `AGENTS.md` §8）。
ICP_BEIAN = "苏ICP备2026073372号"
ICP_URL = "https://beian.miit.gov.cn/"


def build_home() -> str:
    """官网首页（M49）——独立布局（不套文档站的侧边栏），带备案号页脚。"""
    s = _site_stats()
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>基石（jishi）—— 一门中文编程语言</title>
<meta name="description" content="基石是一门中文编程语言，语法对标 Python 的易用性：
关键字、内建、报错全是中文。五个引擎（树遍历 / Python VM / C VM / Node / Rust）语义一致。">
<style>
:root{{--bg:#fff;--fg:#1f2430;--muted:#5c6370;--accent:#0b5fff;--code-bg:#f6f8fa;
--border:#e4e7ec;--nav-bg:#fafbfc;--radius:8px}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0f1218;--fg:#e6e9ef;--muted:#9aa4b2;
--accent:#6ea8ff;--code-bg:#161a22;--border:#242a35;--nav-bg:#131820}}}}
*{{box-sizing:border-box}}
body{{margin:0;font:16px/1.75 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
color:var(--fg);background:var(--bg);-webkit-font-smoothing:antialiased}}
a{{color:var(--accent);text-decoration:none}}a:hover{{text-decoration:underline}}
code,pre{{font-family:"JetBrains Mono",Consolas,monospace}}
.wrap{{max-width:960px;margin:0 auto;padding:0 24px}}
header.top{{border-bottom:1px solid var(--border);position:sticky;top:0;
background:var(--bg);z-index:9}}
header.top .wrap{{display:flex;align-items:center;justify-content:space-between;
height:62px;gap:16px}}
header.top .logo{{font-weight:700;font-size:19px;color:var(--fg)}}
header.top .logo small{{color:var(--muted);font-weight:400;font-size:13px;margin-left:6px}}
header.top nav{{display:flex;gap:20px;font-size:14.5px;flex-wrap:wrap}}
header.top nav a{{color:var(--muted)}}
header.top nav a:hover{{color:var(--accent)}}
.hero{{padding:64px 0 40px;text-align:center}}
.hero img{{width:88px;height:88px;border-radius:20px}}
.hero h1{{font-size:40px;line-height:1.25;margin:22px 0 14px;letter-spacing:-.5px}}
.hero .lead{{font-size:18px;color:var(--muted);max-width:640px;margin:0 auto 26px}}
.btns{{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin-bottom:34px}}
.btn{{display:inline-block;padding:11px 24px;border:1px solid var(--border);
border-radius:var(--radius);font-size:15px;color:var(--fg);background:var(--nav-bg)}}
.btn:hover{{text-decoration:none;border-color:var(--accent)}}
.btn.primary{{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}}
.btn.primary:hover{{opacity:.9}}
pre{{background:var(--code-bg);border:1px solid var(--border);border-radius:var(--radius);
padding:18px 20px;overflow-x:auto;font-size:14px;line-height:1.7;text-align:left;
max-width:640px;margin:0 auto}}
p code,li code{{background:var(--code-bg);padding:2px 5px;border-radius:4px;font-size:.9em}}
section{{padding:44px 0;border-top:1px solid var(--border)}}
section h2{{font-size:24px;margin:0 0 22px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:18px}}
.card{{border:1px solid var(--border);border-radius:var(--radius);padding:20px;
background:var(--nav-bg)}}
.card h3{{margin:0 0 8px;font-size:16px}}
.card p{{margin:0;color:var(--muted);font-size:14.5px}}
.installs{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:18px}}
.installs .card pre{{margin:12px 0 0;font-size:13px;padding:12px 14px;max-width:none}}
footer{{border-top:1px solid var(--border);padding:34px 0 46px;color:var(--muted);
font-size:14px;text-align:center}}
footer .links{{display:flex;gap:20px;justify-content:center;flex-wrap:wrap;
margin-bottom:16px}}
footer .links a{{color:var(--muted)}}
footer .icp{{margin-top:6px}}
footer .icp a{{color:var(--muted);display:inline-flex;align-items:center;gap:6px}}
footer .icp img{{height:16px}}
@media(max-width:640px){{.hero h1{{font-size:30px}}.hero{{padding:40px 0 26px}}}}
</style>
</head>
<body>

<header class="top"><div class="wrap">
  <a class="logo" href="./">基石<small>jishi</small></a>
  <nav>
    <a href="README.html">文档</a>
    <a href="playground.html">在线试玩</a>
    <a href="docs/语言规格.html">语言规格</a>
    <a href="docs/tutorial/01-你好基石.html">教程</a>
    <a href="about/index.html">自述</a>
    <a href="https://github.com/benxiaoniao/JISHI">GitHub</a>
  </nav>
</div></header>

<div class="wrap">
  <div class="hero">
    <img src="assets/基石_logo.png" alt="基石">
    <h1>用中文写程序</h1>
    <p class="lead">基石是一门<b>中文编程语言</b>，语法对标 Python 的易用性 ——
      关键字、内建函数、报错提示都是中文。当前 <b>{s['版本']}</b>，
      标准库 <b>{s['模块数']}</b> 个模块、<b>{s['函数数']}</b> 个函数，
      <b>五个引擎</b>语义一致。</p>
    <div class="btns">
      <a class="btn primary" href="README.html">开始使用</a>
      <a class="btn" href="playground.html">在线试玩（免安装）</a>
    </div>
    <pre><code>函数 问候(名字)：
    返回 "你好，" + 名字 + "！"

令 朋友们 = ["小明", "小红", "小刚"]
遍历 名字 在 朋友们：
    打印(问候(名字))</code></pre>
  </div>
</div>

<div class="wrap">
  <section>
    <h2>为什么值得一试</h2>
    <div class="grid">
      <div class="card">
        <h3>中文就是关键字</h3>
        <p><code>如果</code> / <code>遍历</code> / <code>函数</code> / <code>返回</code>
        —— 按中文语序取名，不照搬英文。{s['关键字数']} 个关键字、{s['内建数']} 个内建，
        全是常用词。</p>
      </div>
      <div class="card">
        <h3>五个引擎，行为一致</h3>
        <p>树遍历、Python 字节码 VM、C 虚拟机，以及 Node / Rust 两个跨语言宿主 ——
        同一段程序跑出来的结果**逐字节相同**（有对拍测试盯着）。</p>
      </div>
      <div class="card">
        <h3>报错直接告诉你怎么办</h3>
        <p>错误码 + 行列位置 + 修法提示，报错里带「你是不是想写「长度」？」。
        错误码还能直接查：<code>jishi 错误 E0301</code>。</p>
      </div>
      <div class="card">
        <h3>标准库与包管理</h3>
        <p>{s['模块数']} 个中文模块（数学 / 时间 / 表格 / 正则 / 加密…），
        外加 <code>jishi 安装</code> 装第三方包。</p>
      </div>
    </div>
  </section>

  <section>
    <h2>三种方式开始</h2>
    <div class="installs">
      <div class="card">
        <h3>① 编辑器里直接用</h3>
        <p>装 VS Code 扩展，立刻有语法高亮、补全、调试器。</p>
        <pre><code>扩展 ID：jishi-lang.vscode-jishi</code></pre>
      </div>
      <div class="card">
        <h3>② 从源码安装</h3>
        <p>需要本机有 Python 3.10+。</p>
        <pre><code>pip install git+https://github.com/benxiaoniao/JISHI.git</code></pre>
      </div>
      <div class="card">
        <h3>③ 装第三方包</h3>
        <p>装好之后，用它自己的包管理。</p>
        <pre><code>jishi 安装 基础库</code></pre>
      </div>
    </div>
  </section>
</div>

<footer><div class="wrap">
  <div class="links">
    <a href="README.html">文档</a>
    <a href="playground.html">试玩</a>
    <a href="docs/语言规格.html">语言规格</a>
    <a href="docs/tutorial/01-你好基石.html">教程</a>
    <a href="llms.txt">llms.txt（给模型看的索引）</a>
    <a href="https://github.com/benxiaoniao/JISHI">GitHub</a>
  </div>
  <div>基石（jishi）· 一门中文编程语言 · MIT-0 许可</div>
  <div class="icp">
    <a href="{ICP_URL}" target="_blank" rel="noopener noreferrer">{ICP_BEIAN}</a>
  </div>
</div></footer>

</body>
</html>
"""


def main() -> int:
    if not DOCS.exists():
        print("没找到 docs/，请在项目根目录运行")
        return 1

    # 清掉「以前构建过、现在不该公开」的页面。
    #
    # ⚠️ 为什么需要这一步：`site/` 是构建产物目录，**上次构建留下的文件不会自己消失**
    # —— 路线图从 `PAGES` 里移走之后，旧的 `site/docs/路线图.html` 还在，
    # 一部署就等于把服务器 IP 与实例 ID 公开发布了。
    for stale in ("docs/路线图.html",):
        p = OUT / stale
        if p.exists():
            p.unlink()
            print(f"删除不该公开的旧页面：{stale}")

    for i, (md_path, title, group) in enumerate(PAGES):
        rel_html = (OUT / md_path.relative_to(ROOT)).with_suffix(".html")
        rel_html.parent.mkdir(parents=True, exist_ok=True)
        rel_html.write_text(
            build_page(md_path, title, group, i),
            encoding="utf-8")
        print(f"生成 {rel_html}")

    # 官网首页（M49）：独立布局，带 ICP 备案号页脚
    (OUT / "index.html").write_text(build_home(), encoding="utf-8")
    print("生成 site/index.html（官网首页，含备案号）")

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
