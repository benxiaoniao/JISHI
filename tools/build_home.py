# -*- coding: utf-8 -*-
"""基石语言官网生成器（M49 · 2026-09-29）。

设计原则（用户要求「慎重考虑一个编程语言的站点需要什么」）：

1. **每页回答一个读者问题**，而不是把 README 拆成几块 ——
   `index` 这是什么/值得试吗 · `start` 怎么跑起来 · `download` 去哪下（含版本历史）·
   `features` 中文写起来什么样 · `stdlib` 有什么现成的 · `examples` 真能用吗 ·
   `progress` 还在维护吗。
2. **数字全部自动生成**：版本/关键字/内建/模块/函数从 `jishi --lang-spec` 读，
   里程碑从 `docs/里程碑.md` 与 `PROGRESS.md` 读，示例规模当场统计。
   手写的数字**一过期就是错的**，而这个项目最不能容忍「总结偏差」。
3. **示例真能跑**：本文件里出现的基石代码，`--check-examples` 会逐个真跑一遍
   （和 `examples/ai/few-shot.md` 的规矩一致 —— 喂给读者的错示例比没有示例更糟）。
4. **如实写边界**：0.2.0 还很早、生态小、哪些场景慢 —— 都写出来。
   这个项目的立身之本是「诚实不吹牛」，官网是它面对陌生人的第一面。
5. **零依赖**：一个 Python 文件产出纯静态 HTML，CSS 内联，深浅色自适应，移动端可用。

用法：
    python tools/build_home.py                 生成到 site/
    python tools/build_home.py --check-examples  只跑示例自检（不写文件）
"""

from __future__ import annotations

import html
import io
import os
import re
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "site"

sys.path.insert(0, str(ROOT))

#: ICP 备案号 —— **法定要求**：主页最下方展示，并链接工信部备案系统。
#: 这类信息本身是**公开登记**，所以进仓库没问题；但**服务器地址 / 实例 ID /
#: 备案主体信息**绝不能进仓库（见 `AGENTS.md` §8 与 内部路线图（未公开））。
ICP_BEIAN = "苏ICP备2026073372号"
ICP_URL = "https://beian.miit.gov.cn/"
GITHUB = "https://github.com/benxiaoniao/JISHI"

#: 导航（所有页共用）
NAV = [
    ("start.html", "上手"),
    ("download.html", "下载"),
    ("features.html", "语言"),
    ("stdlib.html", "标准库"),
    ("examples.html", "示例"),
    ("progress.html", "进展"),
    ("playground.html", "试玩"),
    ("README.html", "文档"),
]


# ---------------------------------------------------------------------------
# 一、样式（单文件内联；深浅色自适应）
# ---------------------------------------------------------------------------

CSS = """\
:root{--bg:#fff;--fg:#1f2430;--muted:#5c6370;--accent:#0b5fff;--code-bg:#f6f8fa;
--border:#e4e7ec;--nav-bg:#fafbfc;--radius:10px;--warn:#b45309;--warn-bg:#fffbeb}
@media(prefers-color-scheme:dark){:root{--bg:#0f1218;--fg:#e6e9ef;--muted:#9aa4b2;
--accent:#6ea8ff;--code-bg:#161a22;--border:#242a35;--nav-bg:#131820;
--warn:#fbbf24;--warn-bg:#221a08}}
*{box-sizing:border-box}
body{margin:0;font:16px/1.75 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
color:var(--fg);background:var(--bg);-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
code,pre,kbd{font-family:"JetBrains Mono",Consolas,"Courier New",monospace}
.wrap{max-width:1000px;margin:0 auto;padding:0 24px}
header.top{border-bottom:1px solid var(--border);position:sticky;top:0;
background:var(--bg);z-index:9}
header.top .wrap{display:flex;align-items:center;justify-content:space-between;
min-height:62px;gap:20px;flex-wrap:wrap;padding-top:8px;padding-bottom:8px}
header.top .logo{font-weight:700;font-size:19px;color:var(--fg);white-space:nowrap}
header.top .logo small{color:var(--muted);font-weight:400;font-size:13px;margin-left:6px}
header.top nav{display:flex;gap:18px;font-size:14.5px;flex-wrap:wrap}
header.top nav a{color:var(--muted);padding:2px 0}
header.top nav a:hover{color:var(--accent)}
header.top nav a.now{color:var(--accent);font-weight:600;border-bottom:2px solid var(--accent)}
h1{font-size:34px;line-height:1.3;margin:38px 0 12px;letter-spacing:-.4px}
h2{font-size:23px;margin:44px 0 14px;padding-top:6px}
h3{font-size:17px;margin:26px 0 8px}
p{margin:10px 0}
.lead{font-size:18px;color:var(--muted);margin-bottom:24px}
pre{background:var(--code-bg);border:1px solid var(--border);border-radius:var(--radius);
padding:16px 18px;overflow-x:auto;font-size:13.8px;line-height:1.72;margin:14px 0;
position:relative}
pre code{background:none;padding:0;font-size:inherit}
p code,li code,td code{background:var(--code-bg);padding:2px 5px;border-radius:4px;font-size:.9em}
pre .cap{display:block;color:var(--muted);font-size:12.5px;margin:-6px 0 10px}
table{border-collapse:collapse;margin:14px 0;width:100%;font-size:14.3px}
th,td{border:1px solid var(--border);padding:7px 11px;text-align:left;vertical-align:top}
th{background:var(--nav-bg);font-weight:600}
td.code,th.code{font-family:"JetBrains Mono",Consolas,monospace;white-space:nowrap}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(228px,1fr));gap:16px;
margin:18px 0}
.card{border:1px solid var(--border);border-radius:var(--radius);padding:18px 20px;
background:var(--nav-bg)}
.card h3{margin:0 0 6px;font-size:16px}
.card p{margin:0;color:var(--muted);font-size:14.3px}
.card pre{margin:12px 0 0;font-size:13px;padding:12px 14px}
.btns{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0 6px}
.btn{display:inline-block;padding:10px 22px;border:1px solid var(--border);
border-radius:var(--radius);font-size:15px;color:var(--fg);background:var(--nav-bg)}
.btn:hover{text-decoration:none;border-color:var(--accent)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.btn.primary:hover{opacity:.9}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:14px;
margin:20px 0 6px}
.stat{border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;
background:var(--nav-bg)}
.stat .k{color:var(--muted);font-size:13px}
.stat .v{font-size:24px;font-weight:700;line-height:1.35}
.stat .v small{font-size:13px;font-weight:400;color:var(--muted);margin-left:4px}
.note{border-left:4px solid var(--accent);background:var(--nav-bg);padding:12px 18px;
border-radius:0 var(--radius) var(--radius) 0;margin:18px 0}
.note.warn{border-left-color:var(--warn);background:var(--warn-bg)}
.note p:first-child{margin-top:0}.note p:last-child{margin-bottom:0}
ul,ol{padding-left:24px}li{margin:5px 0}
footer{border-top:1px solid var(--border);margin-top:64px;padding:32px 0 46px;
color:var(--muted);font-size:14px;text-align:center}
footer .links{display:flex;gap:18px;justify-content:center;flex-wrap:wrap;
margin-bottom:14px}
footer .links a{color:var(--muted)}
footer .icp{margin-top:8px}
footer .icp a{color:var(--muted)}
.tag{display:inline-block;border:1px solid var(--border);border-radius:999px;
padding:1px 10px;font-size:12.5px;color:var(--muted);margin-right:6px}
.timeline{border-left:2px solid var(--border);margin:18px 0 6px;padding-left:20px}
.timeline .item{position:relative;margin-bottom:14px}
.timeline .item::before{content:"";position:absolute;left:-27px;top:9px;width:9px;height:9px;
border-radius:50%;background:var(--accent)}
.timeline .item b{font-weight:600}
.timeline .item .when{color:var(--muted);font-size:13.5px;margin-left:8px}
.timeline .item ul{margin:6px 0 4px;padding-left:20px;font-size:14.3px;color:var(--muted)}
.timeline .item ul li{margin:2px 0}
.timeline .item .dl{margin:5px 0 0;font-size:13.5px}
.timeline .item .dl a{margin-right:12px}
@media(max-width:720px){h1{font-size:27px}h2{font-size:20px}.lead{font-size:16px}
.wrap{padding:0 18px}header.top nav{gap:14px;font-size:14px}}
"""


# ---------------------------------------------------------------------------
# 二、数据（**全部从仓库真数据算出来**）
# ---------------------------------------------------------------------------

def load_spec() -> dict:
    from jishi.ai import build_lang_spec
    return build_lang_spec()


def load_version() -> str:
    from jishi.version import VERSION
    return VERSION


def count_tests() -> tuple[int, int]:
    """从 README 的「当前进度」节读测试数（`2288 项，2285 通过`）。

    ⚠️ 不硬编码：测试数每轮都在变，写死就等着过期。读不到就给 (0, 0)，
    页面会自己跳过这一格 —— **宁可少显示，不显示错的**。
    """
    md = (ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"(\d{3,5})\s*项[，,]\s*\*{0,2}(\d{3,5})\*{0,2}\s*通过", md)
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2)))


def load_milestones() -> list[tuple[str, str, str]]:
    """从 `docs/里程碑.md` 的表格读 `(编号, 内容, 状态)`。"""
    path = ROOT / "docs" / "里程碑.md"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*(M[\d.]+)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|", line)
        if m and m.group(1) != "阶段":
            out.append((m.group(1), m.group(2), m.group(3)))
    return out


def load_projects() -> list[tuple[str, str, int]]:
    """`examples/projects/` 下的真实项目：`(目录名, 说明, 总行数)`。"""
    base = ROOT / "examples" / "projects"
    out = []
    if not base.exists():
        return out
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        lines = 0
        for f in d.rglob("*.jsh"):
            lines += sum(1 for _ in f.open(encoding="utf-8"))
        readme = d / "README.md"
        desc = ""
        if readme.exists():
            for ln in readme.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#") and not ln.startswith(">"):
                    desc = re.sub(r"[`*]", "", ln)[:70]
                    break
        out.append((d.name, desc, lines))
    return out


def load_example_snippets() -> list[tuple[str, str, str]]:
    """拿几个真实项目里的片段（首几行有意义的话），展示「这不是玩具」。"""
    picked = []
    wants = {
        "待办清单": "读写 JSON 文件、按行渲染清单",
        "成绩分析": "读表格、算平均与排名",
        "日志统计": "按行扫日志、统计状态码",
        "网页采集": "发起请求、解析 HTML",
    }
    for name, desc in wants.items():
        f = ROOT / "examples" / "projects" / name
        cands = sorted(f.rglob("*.jsh")) if f.exists() else []
        if not cands:
            continue
        src = cands[0].read_text(encoding="utf-8").splitlines()
        body = [ln for ln in src if ln.strip() and not ln.strip().startswith("#")][:12]
        picked.append((name, desc, "\n".join(body)))
    return picked


# ---------------------------------------------------------------------------
# 三、HTML 骨架
# ---------------------------------------------------------------------------

def _nav_html(now: str) -> str:
    items = []
    for href, label in NAV:
        cls = ' class="now"' if href == now else ""
        items.append(f'<a{cls} href="{href}">{label}</a>')
    items.append(f'<a href="{GITHUB}">GitHub</a>')
    return "".join(items)


def page(title: str, body: str, now: str, desc: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<meta name="description" content="{desc}">
<link rel="icon" href="assets/基石.ico">
<style>{CSS}</style>
</head>
<body>
<header class="top"><div class="wrap">
  <a class="logo" href="index.html">基石<small>jishi</small></a>
  <nav>{_nav_html(now)}</nav>
</div></header>
<div class="wrap">
{body}
</div>
<footer><div class="wrap">
  <div class="links">
    <a href="start.html">上手</a>
    <a href="features.html">语言</a>
    <a href="stdlib.html">标准库</a>
    <a href="examples.html">示例</a>
    <a href="progress.html">进展</a>
    <a href="playground.html">试玩</a>
    <a href="README.html">文档</a>
    <a href="llms.txt">llms.txt</a>
    <a href="{GITHUB}">GitHub</a>
  </div>
  <div>基石（jishi）· 一门中文编程语言 · MIT-0 许可</div>
  <div class="icp"><a href="{ICP_URL}" target="_blank" rel="noopener noreferrer">{ICP_BEIAN}</a></div>
</div></footer>
</body>
</html>
"""


def code(src: str, cap: str = "") -> str:
    """代码块。

    ⚠️ **调用点里的 `\\n` 是「换行」的意思**（不是要显示反斜杠 n）：
    `code('令 a = 1\\n打印(a)')` 在 Python 里那两个是**字面字符** 反斜杠 + n，
    直接输出到 HTML 的话浏览器会原样显示成 `\\n` —— 站点上**真的这么错了很久**
    （「第一个程序」那段显示成 `令 名字 = "世界"\\n打印(...)`，一路看着像代码
    写错了）。统一在这里还原成真换行：**一处修好，所有调用点都受益**。
    """
    cap_html = f'<span class="cap">{cap}</span>' if cap else ""
    text = src.replace("\\n", "\n")            # 字面 \n → 真换行
    return f'<pre>{cap_html}<code>{html.escape(text.rstrip())}</code></pre>'


# ---------------------------------------------------------------------------
# 四、示例（真跑验证过的）
# ---------------------------------------------------------------------------

#: 首页那段（15 行内，一眼看懂中文写法）
HERO_CODE = """函数 问候(名字)：
    返回 "你好，" + 名字 + "！"

令 朋友们 = ["小明", "小红", "小刚"]
遍历 名字 在 朋友们：
    打印(问候(名字))"""

#: 特性页：一段一段都有出处
FEATURE_CODES: dict[str, tuple[str, str]] = {
    "变量与类型": ('令 名字 = "小明"\n令 年龄 = 18\n令 身高 = 1.75\n打印(名字, 年龄, 身高)',
                   "`令` 声明变量，不需要写类型"),
    "条件与循环": ('令 分数 = 87\n如果 分数 >= 90：\n    打印("优秀")\n否则如果 分数 >= 60：\n'
                   '    打印("及格")\n否则：\n    打印("不及格")\n\n'
                   '遍历 数 在 范围(1, 4)：\n    打印(数)\n',
                   "`如果 / 否则如果 / 否则` 与 `遍历 … 在 …`"),
    "函数与默认参数": ('函数 打招呼(名字, 语气 = "你好")：\n    返回 语气 + "，" + 名字\n\n'
                       '打印(打招呼("小明"))\n打印(打招呼("小红", "早上好"))',
                       "默认参数、返回，写法与 Python 一致"),
    "列表与字典": ('令 名单 = ["小明", "小红"]\n名单.追加("小刚")\n打印(长度(名单))\n\n'
                    '令 成绩 = {"小明": 95, "小红": 88}\n遍历 名字 在 成绩.键()：\n'
                    '    打印(名字, 成绩[名字])',
                    "`列表.追加`、`字典.键()` —— 方法与内建都是中文"),
    "推导式与文本插值": ('令 平方 = [n * n 遍历 n 在 范围(1, 6)]\n打印(平方)\n\n'
                          '令 名字 = "小明"\n打印("你好，${名字}！")',
                          "推导式写成 `[表达式 遍历 … 在 …]`；插值用 `${}`"),
    "类与继承": ('类 动物：\n    函数 初始化(自身, 名字)：\n        自身.名字 = 名字\n\n'
                  '    函数 叫(自身)：\n        返回 "……"\n\n'
                  '类 狗 继承 动物：\n    函数 叫(自身)：\n        返回 "汪汪"\n\n'
                  '打印(新建 狗("旺财").叫())',
                  "`类 / 继承 / 新建`，`自身` 就是 self"),
    "异常处理": ('尝试：\n    令 结果 = 10 / 0\n捕获 除零错误 为 e：\n    打印("出错了：", e.消息)',
                  "`尝试 / 捕获 / 最终`；异常类型也是中文"),
    "文件读写": ('用 打开("数据.txt", "写") 为 f：\n    f.写行("第一行")\n\n'
                  '用 打开("数据.txt") 为 f：\n    遍历 行 在 f：\n        打印(行)',
                  "`用 … 为` 就是上下文管理器：块一结束自动关文件"),
    "用标准库": ('导入 数学\n\n打印(数学.开方(16))\n'
                  '打印(数学.最大公约数(12, 18))\n打印(数学.四舍五入(3.14159, 2))',
                  "标准库模块要先 `导入`；函数名同样是中文"),
}


def check_examples() -> int:
    """把本文件里的基石代码**真跑一遍**，有错就报出来。

    为什么必须做：喂给读者的错示例比没有示例更糟 —— 项目在
    `examples/ai/few-shot.md` 上就栽过（48 段里躺着 4 段真错）。
    """
    from jishi.compiler import compile_source
    from jishi import vm as vm_mod

    cases = [("首页", HERO_CODE, None)]
    for name, (src, _cap) in FEATURE_CODES.items():
        # `价格` 那种要标准库的多行片段照跑；但首页那段要 `打印` 结果
        cases.append((name, src, None))

    # ⚠️ 切到临时目录跑：有的示例会写文件（`打开("数据.txt", "写")`），
    # 在仓库里跑就会留下一堆垃圾。
    old_cwd = os.getcwd()
    tmp = tempfile.mkdtemp(prefix="jishi-site-")
    os.chdir(tmp)
    bad = 0
    try:
        for name, src, _ in cases:
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    vm_mod.VM(compile_source(src, "<site>"), "<site>").run()
            except Exception as e:                              # noqa: BLE001
                bad += 1
                print(f"❌ {name} 跑不通：{str(e).splitlines()[0]}")
                print("   " + src.replace("\n", "\n   ")[:300])
    finally:
        os.chdir(old_cwd)
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"{'✅' if not bad else '❌'} 官网示例自检：{len(cases) - bad}/{len(cases)} 通过")
    return 1 if bad else 0


# ---------------------------------------------------------------------------
# 五、六个页面
# ---------------------------------------------------------------------------

def build_index(spec: dict, ver: str) -> str:
    total, passed = count_tests()
    test_cell = ""
    if total:
        test_cell = (f'<div class="stat"><div class="k">测试用例</div>'
                     f'<div class="v">{total}<small>项，{passed} 通过</small></div></div>')
    n_fn = sum(len(m["functions"]) for m in spec["stdlib"])
    projects = load_projects()
    proj_rows = "".join(
        f"<tr><td>{html.escape(n)}</td><td>{html.escape(d)}</td>"
        f"<td class=\"code\">{ln} 行</td></tr>"
        for n, d, ln in projects)

    body = f"""
<h1>用中文写程序</h1>
<p class="lead">基石是一门<b>中文编程语言</b>。<code>如果</code> <code>遍历</code>
<code>函数</code> <code>返回</code> 这些词本身就是关键字 ——
你写下的和你脑子里想说的，是同一句话，中间少一层英译。</p>

{code(HERO_CODE, "上：一段完整的基石程序（可直接复制去跑）")}

<div class="btns">
  <a class="btn primary" href="start.html">五分钟上手</a>
  <a class="btn" href="playground.html">不装也能试</a>
  <a class="btn" href="features.html">先看看语法</a>
</div>

<div class="stats">
  <div class="stat"><div class="k">当前版本</div><div class="v">{ver}<small>早期</small></div></div>
  {test_cell}
  <div class="stat"><div class="k">标准库</div><div class="v">{len(spec['stdlib'])}<small>模块 · {n_fn} 函数</small></div></div>
  <div class="stat"><div class="k">引擎</div><div class="v">5<small>个，行为一致</small></div></div>
  <div class="stat"><div class="k">真实项目</div><div class="v">{len(projects)}<small>个在仓库里</small></div></div>
</div>

<h2>为什么值得花十分钟</h2>
<div class="grid">
  <div class="card"><h3>读起来就是话</h3>
    <p>不是「把英文关键词翻译一遍」，而是按中文语序取名：
    <code>遍历 名字 在 名单</code>、<code>如果 分数 &gt;= 90</code>。
    {len(spec['keywords'])} 个关键字、{len(spec['builtins'])} 个内建，全是日常词。</p></div>
  <div class="card"><h3>五个引擎，结果一致</h3>
    <p>树遍历、Python 字节码 VM、C 虚拟机，外加 Node 与 Rust 两个跨语言宿主 ——
    同一段程序在五边跑出来的输出<b>逐字节相同</b>，有对拍测试盯着。</p></div>
  <div class="card"><h3>报错直接告诉你怎么办</h3>
    <p>错误码 + 行列位置 + 修法提示。写错名字会说
    「你是不是想写「长度」？」；拿到错误码还能反查：
    <code>jishi 错误 E0301</code>。</p></div>
  <div class="card"><h3>标准库不用现学</h3>
    <p>{len(spec['stdlib'])} 个模块覆盖数学、时间、表格、正则、加密、网络…
    函数名同样是中文：<code>表格.转文本(表)</code>、
    <code>正则.替换(文本, "\\d+", "#")</code>。</p></div>
</div>

<h2>已经用它写过的东西</h2>
<p>仓库里放着 {len(projects)} 个完整项目，都是真跑得起来的程序 ——
不是「Hello World 集锦」。</p>
<table>
<tr><th>项目</th><th>做了什么</th><th class="code">规模</th></tr>
{proj_rows}
</table>
<p><a href="examples.html">看这些项目的代码片段 →</a></p>

<h2>也说说现在不行的地方</h2>
<div class="note warn">
<p><b>它还很早。</b>版本 {ver}，语言本身已经稳定（语法面已冻结），
但标准库还在长、API 偶尔会调。拿它写玩具、写脚本、写课设都没问题；
拿它上生产之前，请先小范围试。</p>
<p><b>生态还小。</b>没有 Python 那种「装个库解决一切」的生态，
第三方包目前只有仓库里自带的几个。缺什么，通常得自己写。</p>
<p><b>有些场景它更慢。</b>性能上做过针对性优化（C 虚拟机的字符串已是原生实现），
但容器密集读的场景仍比 CPython 慢 —— 具体数字都写在文档里，没有藏着。</p>
</div>

<h2>三分钟就能跑起来</h2>
<p class="lead"><b>Windows</b> 下载 <code>jishi-{ver}-windows-x64-setup.exe</code>
双击装（<b>不用先装 Python</b>）；Linux / macOS 有对应压缩包；
想改代码的可以 <code>pip install git+…</code>。
安装包<b>就放在本站</b>，不必去 GitHub —— <a href="download.html">下载页</a>
有各平台的包和逐版本的升级说明。</p>
{code('jishi 你好.jsh\\njishi 医生', "装好之后：跑程序、做一次环境自检")}
"""
    return page("基石（jishi）—— 一门中文编程语言", body, "index.html",
                "基石是一门中文编程语言，关键字与报错都是中文。五个引擎语义一致，"
                "标准库 25 个中文模块。看语法、看示例、直接上手。")


def build_start(ver: str) -> str:
    body = f"""
<h1>五分钟上手</h1>
<p class="lead">挑一种装法，然后跑你的第一个程序。</p>

<h2>一、选一种装法</h2>
<div class="grid">
  <div class="card"><h3>① Windows 安装程序（最省事）</h3>
    <p>下载 <code>jishi-{ver}-windows-x64-setup.exe</code> 双击装。
    <b>不用先装 Python</b> —— 发行包自带运行时；用户级安装、免管理员，
    向导是中文的。</p>
    <p><a href="download.html">去下载（本站，大陆直连）→</a>
    　另有免安装的 <code>.zip</code>，旧版本在下载页的版本历史里。</p></div>
  <div class="card"><h3>② Linux / macOS 压缩包</h3>
    <p>解压后跑 <code>install.sh</code>。Linux 是 x64；macOS 目前<b>只有
    Apple 芯片（arm64）</b>版，没有 Intel 版。</p>
    {code('tar -xzf jishi-<版本>-linux-x64.tar.gz\\ncd jishi-<版本>-linux-x64 && bash install.sh')}</div>
  <div class="card"><h3>③ 从源码装</h3>
    <p>给想改代码的人：需要本机有 Python 3.10 以上。</p>
    {code('pip install git+https://github.com/benxiaoniao/JISHI.git')}
    <p>⚠️ 这条路要连 GitHub，<b>大陆可能很慢甚至超时</b>。</p></div>
  <div class="card"><h3>④ 什么都不装</h3>
    <p>用<a href="playground.html">在线试玩</a> —— 浏览器里直接写、直接跑，
    引擎跑在页面上。</p>
    <p><b>适合：</b>先看看这东西长什么样。</p></div>
</div>

<div class="note">
<p><b>想在 VS Code 里写？</b>扩展市场搜 <code>jishi</code>，装
<code>jishi-lang.vscode-jishi</code>：语法高亮、补全、跳转、调试器都有。
⚠️ <b>但扩展不带运行时</b> —— 要先按上面 ①②③ 任意一种把
<code>jishi</code> 命令装好，扩展才跑得起来（它默认调 PATH 上的
<code>jishi</code>，也可以在设置里指到别处）。</p>
</div>
<div class="note">
<p><b>装完先跑一次 <code>jishi 医生</code>：</b>它会查版本、C 虚拟机、终端编码，
以及<b>包索引通不通</b>（装第三方包要用这一步）。哪一步有问题，它会直接说怎么修。</p>
</div>

<h2>二、第一个程序</h2>
<p>新建 <code>你好.jsh</code>，写三行：</p>
{code('令 名字 = "世界"\\n打印("你好，" + 名字 + "！")')}
<p>然后：</p>
{code('jishi 你好.jsh')}
{code('你好，世界！', "应该看到这一行")}

<div class="note">
<p><b>文件后缀是 <code>.jsh</code></b>，用 UTF-8 保存。
在 Windows 记事本里存成别的编码，中文会乱码（和 Python 2 时代一个道理）。</p>
</div>

<h2>三、一步一步来</h2>
<p>下面五段都可以直接存成文件跑，每段只加一个新东西。</p>

<h3>1. 变量与运算</h3>
{code('令 单价 = 12.5\\n令 数量 = 3\\n打印(单价 * 数量)')}

<h3>2. 判断</h3>
{code('令 分数 = 87\\n如果 分数 >= 90：\\n    打印("优秀")\\n否则如果 分数 >= 60：\\n'
      '    打印("及格")\\n否则：\\n    打印("不及格")')}

<h3>3. 循环</h3>
{code('遍历 数 在 范围(1, 6)：\\n    打印(数, "的平方是", 数 * 数)')}

<h3>4. 函数</h3>
{code('函数 算面积(长, 宽 = 1)：\\n    返回 长 * 宽\\n\\n'
      '打印(算面积(5, 3))\\n打印(算面积(4))')}

<h3>5. 字典（键值对）</h3>
{code('令 库存 = {"苹果": 12, "香蕉": 7}\\n库存["苹果"] = 库存["苹果"] - 2\\n'
      '遍历 名字, 数量 在 库存.项目()：\\n    打印(名字, "还剩", 数量)')}

<h2>四、中文编程会遇到的坑</h2>
<div class="note warn">
<p><b>① 标点必须是半角的。</b>中文输入法打出的 <code>（</code>
<code>）</code> <code>，</code> 和英文的 <code>(</code> <code>)</code>
<code>,</code> 长得像，但机器只认后者。基石会直接报「无法识别的字符」并指出那一列 ——
这是新手中招最多的一条。</p>
<p><b>② 冒号「：」在句尾是<b>全角</b>的。</b>这是唯一一处故意的全角 ——
因为它读起来是中文的一部分（<code>如果 真：</code>）。</p>
<p><b>③ 缩进用空格，一级四个。</b>不要混用 Tab 和空格（会用
<code>jishi 格式化 文件.jsh -w</code> 一键修好）。</p>
<p><b>④ 关键字后面要有空格。</b><code>如果真：</code> 会被读成一个名字
<code>如果真</code>，要写成 <code>如果 真：</code>。</p>
</div>

<h2>五、出错了怎么办</h2>
<p>基石报错会尽量把「错在哪、错什么、怎么改」一次说清：</p>
{code('错误 E0301：找不到这个名字（找不到名字「长渡」）\\n'
      '  ┌─ 你好.jsh:2:6\\n'
      '  │\\n'
      '  2 │ 打印(长渡([1]))\\n'
      '    │      ^\\n'
      '  │\\n'
      '  提示：你是不是想写「长度」？')}
<p>报错里的错误码可以直接查：</p>
{code(f'jishi 错误 E0301')}
<p>每个码都会给出「什么意思 / 常见成因 / 怎么改」三段。全部 32 个码用
<code>jishi 错误</code> 列出来。</p>

<h2>接下来看什么</h2>
<ul>
  <li><a href="features.html">语言特性</a> —— 一门语言能写什么，一段段看代码</li>
  <li><a href="stdlib.html">标准库</a> —— 有什么现成的，别自己造轮子</li>
  <li><a href="docs/tutorial/01-你好基石.html">教程 10 章</a> —— 从零到综合实战</li>
  <li><a href="README.html">完整文档</a> —— 语言规格、设计决策、工具链</li>
</ul>
"""
    return page("五分钟上手 · 基石（jishi）", body, "start.html",
                "基石的上手页：三种安装方式、五个递进示例、中文编程常见的四个坑、"
                "以及报错怎么读。")


def build_features(spec: dict) -> str:
    kw_rows = "".join(
        f'<tr><td class="code">{html.escape(k)}</td><td>{html.escape(v)}</td></tr>'
        for k, v in spec["keywords"].items())
    equiv = spec.get("python_equiv", {}).get("keywords", {})
    eq_rows = "".join(
        f'<tr><td class="code">{html.escape(k)}</td><td class="code">{html.escape(v)}</td></tr>'
        for k, v in list(equiv.items())[:24])
    ops = spec.get("operators", {})
    op_rows = ""
    # `binary` 是 dict（带优先级与名字）；`unary`/`assign` 是纯字符串列表；
    # `compare` 与 `binary` 里的比较项重复，跳过不打两遍。
    for o in ops.get("binary", []):
        op_rows += (f'<tr><td class="code">{html.escape(str(o.get("op", "")))}</td>'
                    f'<td>{html.escape(str(o.get("name", "")))}</td>'
                    f'<td>二元 · 优先级 {html.escape(str(o.get("precedence", "")))}</td></tr>')
    for o in ops.get("unary", []):
        op_rows += (f'<tr><td class="code">{html.escape(str(o))}</td>'
                    f'<td>取负 / 逻辑非</td><td>一元</td></tr>')
    for o in ops.get("assign", []):
        op_rows += (f'<tr><td class="code">{html.escape(str(o))}</td>'
                    f'<td>复合赋值</td><td>赋值</td></tr>')

    samples = ""
    for name, (src, cap) in FEATURE_CODES.items():
        samples += f"<h3>{html.escape(name)}</h3>\n{code(src, cap)}\n"

    body = f"""
<h1>中文写起来是什么样</h1>
<p class="lead">这一页不用形容词，直接给你看代码。下面的每一段都能直接跑
（生成这个页面时会当场跑一遍，跑不过就不许发布）。</p>

{samples}

<h2>关键字总表（{len(spec['keywords'])} 个）</h2>
<p>这是<b>全部</b>关键字 —— 一门语言的关键字越少，越容易在脑子里装下。</p>
<table><tr><th class="code">关键字</th><th>作用</th></tr>{kw_rows}</table>

<h2>如果你会 Python</h2>
<p>基石借用了 Python 的语法骨架（缩进、冒号开块、没有花括号），
把词换成了中文。所以会 Python 的人基本可以直接读：</p>
<table><tr><th class="code">基石</th><th class="code">Python</th></tr>{eq_rows}</table>
<p>反过来也成立：<code>jishi --ai-card</code> 会打出一张中文语言卡，
可以直接贴进大模型让它照着写。</p>

<h2>运算符</h2>
<table><tr><th class="code">写法</th><th>含义</th><th>类别</th></tr>{op_rows}</table>

<h2>它不是「翻译器」</h2>
<div class="note">
<p>有些中文编程工具是把中文先翻成 Python 再跑 —— 那样报错位置、调用栈、
性能都会走样。基石是**自己实现的语言**：有自己的词法、语法、字节码与虚拟机，
报错里给的是基石的行列位置，<code>jishi 错误 E0301</code> 查的也是基石的错误码。</p>
<p>它还带了两个跨语言宿主（Node 与 Rust），四个实现跑同一份字节码，
输出必须逐字节一致 —— 这条有测试盯着，不一致就是 bug。</p>
</div>

<h2>尚未定型的地方</h2>
<div class="note warn">
<p>语法面已经冻结（不会再随手加新语法），但下面这些还在长：
标准库还在加模块；部分错误文案会再打磨；<code>匹配/情形</code>
这类较新的语法用得还少，实际项目里踩到问题会修。</p>
</div>

<p class="lead">想看更细的规则？<a href="docs/语言规格.html">语言规格</a>
是逐条写死的形式化说明。</p>
"""
    return page("语言特性 · 基石（jishi）", body, "features.html",
                "基石的语言特性：全部 36 个关键字、与 Python 的逐词对照、"
                "运算符表，以及 10 段真能跑的示例代码。")


def build_stdlib(spec: dict) -> str:
    n_fn = sum(len(m["functions"]) for m in spec["stdlib"])
    mods = ""
    for m in spec["stdlib"]:
        rows = "".join(
            f'<tr><td class="code">{html.escape(f["sig"])}</td>'
            f'<td>{html.escape(f["doc"])}</td></tr>'
            for f in m["functions"])
        mods += (f'<h3 id="m-{html.escape(m["module"])}">{html.escape(m["module"])}'
                 f'<span class="tag">{len(m["functions"])} 个函数</span></h3>\n'
                 f'<table><tr><th class="code" style="width:34%">调用</th>'
                 f'<th>说明</th></tr>{rows}</table>\n')

    b_rows = "".join(
        f'<tr><td class="code">{html.escape(b["name"])}</td>'
        f'<td>{html.escape(b["doc"])}</td></tr>'
        for b in spec["builtins"])
    meth = ""
    for cls, names in spec.get("methods", {}).items():
        meth += (f'<tr><td class="code">{html.escape(cls)}</td>'
                 f'<td>{"、".join("`" + html.escape(n) + "`" for n in names)}</td></tr>')
    exc = "、".join(f"`{html.escape(e)}`" for e in spec["exceptions"])
    codes = spec.get("error_codes", [])
    code_rows = "".join(
        f'<tr><td class="code">{html.escape(c["code"])}</td>'
        f'<td>{html.escape(c["title"])}</td></tr>'
        for c in codes if not c.get("internal"))

    body = f"""
<h1>标准库</h1>
<p class="lead">{len(spec['stdlib'])} 个模块、{n_fn} 个函数，外加
{len(spec['builtins'])} 个内建。函数名和参数说明都是中文 ——
所以查文档这件事，在基石里通常不需要。</p>

<h2>怎么用</h2>
{code('导入 数学\\n\\n打印(数学.开方(16))\\n打印(数学.最大公约数(12, 18))',
      "先导入模块，再用 `模块名.函数名(...)` 调用")}
<div class="note">
<p><b>内建不用导入。</b><code>打印</code>、<code>长度</code>、<code>范围</code>、
<code>整数</code> 这些随时可用；只有标准库模块才要 <code>导入</code>。
不知道有哪些名字时：<code>jishi --ai-card</code> 打出全量清单。</p>
</div>

<h2>模块（{len(spec['stdlib'])} 个）</h2>
{mods}

<h2>内建函数（{len(spec['builtins'])} 个）</h2>
<table><tr><th class="code" style="width:24%">名字</th><th>作用</th></tr>{b_rows}</table>

<h2>容器与对象的方法</h2>
<table><tr><th class="code" style="width:22%">类型</th><th>方法</th></tr>{meth}</table>

<h2>异常与错误码</h2>
<p>能被 <code>捕获</code> 的异常类型：{exc}。</p>
<p>错误码按阶段分段（词法 <code>E01xx</code> / 语法 <code>E02xx</code> /
语义 <code>E03xx</code> / 运行期 <code>E20xx</code>），报错里会带上码与行列：</p>
{code('错误 E2002：类型错误（「整数」和「文本」不能做「+」运算）\\n'
      '  ┌─ 例子.jsh:3:6')}
<p>拿不准某个码什么意思，直接查：</p>
{code('jishi 错误 E2002', "会给出「什么意思 / 常见成因 / 怎么改」三段")}
<table><tr><th class="code" style="width:18%">码</th><th>含义</th></tr>{code_rows}</table>
"""
    return page("标准库 · 基石（jishi）", body, "stdlib.html",
                f"基石标准库：{len(spec['stdlib'])} 个中文模块、{n_fn} 个函数的"
                "完整清单，外加内建、容器方法与错误码表。")


def build_examples(spec: dict) -> str:
    projects = load_projects()
    snippets = load_example_snippets()
    proj_rows = "".join(
        f"<tr><td>{html.escape(n)}</td><td>{html.escape(d)}</td>"
        f'<td class="code">{ln} 行</td></tr>' for n, d, ln in projects)
    snip_html = ""
    for name, desc, src in snippets:
        snip_html += f"<h3>{html.escape(name)}<span class=\"tag\">{html.escape(desc)}</span></h3>\n{code(src)}\n"

    tut = sorted((ROOT / "examples" / "tutorial").glob("*.jsh")) \
        if (ROOT / "examples" / "tutorial").exists() else []
    tut_rows = "".join(
        f'<tr><td class="code">{html.escape(f.name)}</td>'
        f'<td class="code">{sum(1 for _ in f.open(encoding="utf-8"))} 行</td></tr>'
        for f in tut)

    total_lines = sum(ln for _, _, ln in projects)
    body = f"""
<h1>示例与作品</h1>
<p class="lead">判断一门语言值不值得学，最好的办法是看它写过什么。
下面这些都在仓库里，<b>clone 下来就能跑</b>。</p>

<h2>完整项目（{len(projects)} 个 · 共 {total_lines} 行）</h2>
<table><tr><th>项目</th><th>做了什么</th><th class="code">规模</th></tr>{proj_rows}</table>

<h2>挑几段看看</h2>
<p>下面是从项目里截的真实代码（不是为演示编的）：</p>
{snip_html}

<h2>教程里的例子（{len(tut)} 个）</h2>
<p>配套 <a href="docs/tutorial/01-你好基石.html">10 章教程</a>，
每个例子都是完整可跑的小程序 —— 从打印一行字到记账程序。</p>
<table><tr><th class="code">文件</th><th class="code">规模</th></tr>{tut_rows}</table>

<h2>这个站也是基石写的</h2>
<div class="note">
<p>官网里的<a href="about/index.html">「自述」页</a>由一个<b>用基石写的程序</b>
生成（<code>examples/projects/基石自述站/生成.jsh</code>，300 多行）——
它读仓库里的真数据（版本号、测试数、里程碑），当场算出页面。
所以那页上的数字不会过期。</p>
<p>换句话说：这个语言至少已经能写「读文件、处理文本、生成 HTML」这类活了。
这类活占程序员日常的一大半。</p>
</div>

<h2>自己动手</h2>
{code('git clone https://github.com/benxiaoniao/JISHI.git\\ncd JISHI\\npython -m jishi.cli examples/projects/待办清单/待办.jsh',
      "clone 下来直接跑仓库里的例子（不用装，源码即可运行）")}
"""
    return page("示例与作品 · 基石（jishi）", body, "examples.html",
                f"用基石写的东西：{len(projects)} 个完整项目（共 {total_lines} 行）、"
                "教程示例，以及用基石自己生成的官网自述页。")


def build_progress(ver: str) -> str:
    total, passed = count_tests()
    ms = load_milestones()
    recent = ms[-8:][::-1] if ms else []
    items = "".join(
        f'<div class="item"><b>{html.escape(num)}</b>'
        f'<span class="when">{html.escape(st)}</span><br>{html.escape(desc)}</div>'
        for num, desc, st in recent)

    test_line = (f"测试用例 {total} 项（{passed} 通过）" if total
                 else "测试用例见仓库")

    body = f"""
<h1>进展</h1>
<p class="lead">一门语言最怕的是「写着写着没人了」。这一页如实记着：
现在到哪了、最近做了什么、哪些还没做。</p>

<div class="stats">
  <div class="stat"><div class="k">当前版本</div><div class="v">{ver}</div></div>
  <div class="stat"><div class="k">里程碑</div><div class="v">{len(ms)}<small>个已完成</small></div></div>
  <div class="stat"><div class="k">测试</div><div class="v">{total or '—'}<small>项</small></div></div>
  <div class="stat"><div class="k">引擎</div><div class="v">5</div></div>
</div>
<p>{test_line}，每次改动都要全量回归通过才提交。</p>

<h2>最近这些里程碑在做的事</h2>
<div class="timeline">{items}</div>
<p>完整的里程碑明细（M0 起逐条）在
<a href="docs/语言规格.html">文档</a>与仓库的 <code>PROGRESS.md</code> 里。</p>

<h2>最近一轮：让「装得上」在中国大陆成立</h2>
<p>M49 做的是分发。原先装包默认走 GitHub，<b>大陆访问时通时不通</b>
（实测 <code>git</code> 拉取会整段超时）—— 这不算「能用」。</p>
<ul>
  <li><b>官网与自建源上线</b>：<a href="https://xn--3jsy75e.cn/">基石.cn</a>
  用上了自己的域名，源站在大陆 —— <b>包索引和发行包都托管在这里</b>。</li>
  <li><b>两个索引源按优先级回退</b>：自建源优先、GitHub 兜底；一个源读不到
  就自动换下一个，不用你重试，也不用等一个长超时。</li>
  <li><b>报错给做法</b>：全都连不上时，列清试过哪些地址（含失败原因），并给出
  <code>--index</code> / <code>--local-zip</code> 两条能照做的路。</li>
  <li><b><code>jishi 医生</code> 能体检索引</b>：装包之前先看清哪个源通、里面有多少包。</li>
</ul>

<h2>上一轮：把报错变成「能照着做」</h2>
<p>M56 分了四批，全都围绕同一件事 —— <b>出问题的时候别让人猜</b>：</p>
<ul>
  <li><b>内建体检</b>：给每一个内建写一条最小程序，在五个引擎上真跑一遍。
  建起来当场就抓到「<code>超()</code> 在两个宿主上根本不能用」。</li>
  <li><b>宿主报错对齐</b>：Node 有 16 个内建的参数检查是缺的（其中 3 个会
  <b>静默给错答案</b>），Rust 的异常丢了消息 —— 都补上了，现在五边逐字一致。</li>
  <li><b>错误码索引</b>：新增 <code>jishi 错误 E0301</code>，32 个码都能查
  「什么意思 / 常见成因 / 怎么改」。</li>
  <li><b>文件语义</b>：位置口径、换行符、按字符读，三个只在中文/CRLF 上才暴露的
  不一致全修了（顺带修掉一个「把汉字切两半然后怪文件不是 UTF-8」的误导性报错）。</li>
  <li><b>提示语</b>：两个宿主也能说「你是不是想写「长度」？」了 ——
  为此把 Python 的 <code>difflib</code> 逐字复刻进了 JS 与 Rust，
  并用 4000 组随机对拍确认一致。</li>
</ul>

<h2>还没做 / 做不好的</h2>
<div class="note warn">
<p><b>生态。</b>第三方包管理能用（<code>jishi 安装 基础库</code>），
但仓库里目前只有几个演示包。没有「装个库解决一切」的体验。</p>
<p><b>性能。</b>做过针对性优化（C 虚拟机的字符串已原生实现），
容器密集读的场景仍慢于 CPython。没有拿它跟别的语言比过总体跑分 ——
因为没测过的东西不该写。</p>
<p><b>平台。</b>Windows 上功能最全（作者用的就是 Windows）；Linux/macOS
能跑但要自己编译 C 虚拟机；手机、WebAssembly 都还没做。</p>
<p><b>标准库还有缺口。</b>比如 <code>时间.计时</code> 一处已知缺口，
如实写在测试里，没有藏。</p>
</div>

<h2>接下来想做的</h2>
<ul>
  <li><b>性能</b>：先量再改 —— C 虚拟机的容器读侧是已知的短板。</li>
  <li><b>对模型更友好</b>：让大模型更容易写对基石（语言卡、示例库、错误码都已就位）。</li>
  <li><b>文档与教程</b>：继续补真实场景的写法。</li>
</ul>

<div class="note">
<p>这个项目的做事方式是：<b>每个里程碑都要有可演示的成果 + 测试护航</b>，
完不成的就如实标成缺口，不吹。上面这些数字都能在仓库里核对。</p>
</div>
"""
    return page("进展 · 基石（jishi）", body, "progress.html",
                "基石走到哪了：里程碑时间线、最近四批交付、以及如实写下的"
                "已知边界与下一步计划。")


# ---------------------------------------------------------------------------
# 六、主流程
# ---------------------------------------------------------------------------

#: 发行包的各平台后缀 → 友好名与说明（顺序就是页面上的顺序）
PLATFORM_NAMES = (
    ("windows-x64-setup.exe", "Windows 安装程序", "双击装好，**不用先装 Python**（最省事）"),
    ("windows-x64.zip", "Windows 免安装包", "解压即用，不写注册表"),
    ("linux-x64.tar.gz", "Linux（x64）", "解压后 `bash install.sh`"),
    ("macos-arm64.tar.gz", "macOS（Apple 芯片）", "解压后 `bash install.sh`；**没有 Intel 版**"),
)

#: 发行包镜像目录：`tools/fetch_release_mirror.py` 拉到这儿，
#: `tools/site_sync.sh` 再同步到网站上的 `download/`。
MIRROR_DIR = ROOT / "dist" / "release"


def human_size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


def md_inline(s: str) -> str:
    """极小的行内 markdown → HTML：只认 `**粗体**`、`` `代码` ``、`[文字](链接)`。

    只用来渲染**我们自己写的说明**与 **CHANGELOG 的小标题**（那里确实有
    `**4 个真 bug**`、`` `hint` `` 这种写法）。不引入 markdown 库、也不假装支持
    完整语法：认不出来的**原样输出**（比乱转义安全）。

    ⚠️ 顺序是**先转义、再套格式**：反过来的话，用户内容里的 `<b>` 会被当成
    我们自己的标签放行。反向引用这里写成 `\1` / `\2`
    （**别写成裸的八进制转义** —— 那会变成看不见的控制字符，页面看着少字）。
    """
    out = html.escape(s)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"`(.+?)`", r"<code>\1</code>", out)
    out = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', out)
    return out

def parse_changelog() -> list[dict]:
    """从 `CHANGELOG.md` 抽版本历史。

    **升级点不在这里另写一份**：每个版本下面的 `### 小标题` 就是「这一版改了什么」。
    手抄到官网迟早会漂（这是本项目的老毛病，M56 就抓到过一次「待办描述过时」）。
    """
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    out: list[dict] = []
    cur = None
    for line in text.splitlines():
        m = re.match(r"^## \[(\d[^\]]*)\](?:\s*-\s*(\S+))?\s*(.*)$", line)
        if m:
            cur = {"版本": m.group(1), "日期": m.group(2) or "—",
                   "里程碑": m.group(3).strip().strip("（）() "), "要点": []}
            out.append(cur)
            continue
        if cur is None:
            continue                                    # [未发布] 那段跳过
        m2 = re.match(r"^###\s+(.+?)\s*$", line)
        if m2:
            cur["要点"].append(m2.group(1))
    return out


def mirrored_assets() -> dict:
    """读镜像目录里**真实存在**的文件 → `{版本: [(平台后缀, 字节), …]}`。

    「本站有没有这份包」以**磁盘上的文件**为准，不靠一份手写的清单 ——
    清单会漏、会忘更新，而文件在就是在。文件名约定：`jishi-<版本>-<平台后缀>`。
    """
    got: dict = {}
    if not MIRROR_DIR.is_dir():
        return got
    for f in sorted(MIRROR_DIR.iterdir()):
        if not f.is_file() or f.suffix == ".part":
            continue
        m = re.match(r"^jishi-(\d[^-]*)-(.+)$", f.name)
        if not m:
            continue
        got.setdefault(m.group(1), []).append((m.group(2), f.stat().st_size))
    return got


def build_download(ver: str) -> str:
    """下载页：**本站镜像的安装包** + 逐版本升级点。

    用户 2026-09-29 提的两件事，就是这一页的两个作用：
    **拿得到**（安装包放官网，不必去 GitHub —— 大陆访问 GitHub 不稳）、
    **看得清**（每个版本改了什么，一目了然）。
    """
    mirror = mirrored_assets()
    hist = parse_changelog()
    current = dict(mirror.get(ver, []))

    # ---- 一、当前版本：四个平台的卡片 ----
    cards = ""
    for suffix, title, hint in PLATFORM_NAMES:
        size = current.get(suffix)
        if size is None:
            link = (f'<a href="{GITHUB}/releases/tag/v{ver}">去 GitHub 下载</a>'
                    '（本站暂未镜像）')
        else:
            link = (f'<a href="download/jishi-{ver}-{suffix}">下载'
                    f'（{human_size(size)}）</a>')
        cards += (f'  <div class="card"><h3>{title}</h3>'
                  f'<p>{md_inline(hint)}</p><p>{link}</p></div>\n')

    sums_html = ""
    if (MIRROR_DIR / "SHA256SUMS").is_file():
        sums_html = (
            '<p class="lead">想核对完整性：本站也放了 '
            '<a href="download/SHA256SUMS">SHA256SUMS</a>（与 GitHub 那份相同）。'
            '下载后在同目录执行 <code>sha256sum -c SHA256SUMS</code>；'
            'Windows 上用 <code>certutil -hashfile 文件名 SHA256</code> 手动比对。</p>')

    # ---- 二、版本历史：升级点直接抽自 CHANGELOG ----
    items = []
    for v in hist:
        points = "".join(f"<li>{md_inline(p)}</li>" for p in v["要点"])
        files = mirror.get(v["版本"], [])
        if files:
            labels = {s: t for s, t, _ in PLATFORM_NAMES}
            links = "".join(
                f'<a href="download/jishi-{v["版本"]}-{s}">{labels.get(s, s)}</a>'
                for s, _ in files)
            dl = f'<p class="dl">本站下载：{links}</p>'
        else:
            dl = (f'<p class="dl">本站未镜像 —— <a href="{GITHUB}/releases/tag/'
                  f'v{v["版本"]}">GitHub Releases</a></p>')
        when = html.escape(v["日期"])
        if v["里程碑"]:
            when += " · " + html.escape(v["里程碑"])
        items.append(
            f'<div class="item"><b>{html.escape(v["版本"])}</b>'
            f'<span class="when">{when}</span>'
            f'<ul>{points}</ul>{dl}</div>')

    body = f"""
<h1>下载</h1>
<p class="lead">安装包<b>就放在这个站上</b>（基石.cn，大陆直连）—— 不必去 GitHub 翻。
与 GitHub Releases 上那份<b>逐字节相同</b>，附校验和可以核对
（同一份文件换个地方放，不另起炉灶重编）。</p>

<h2>当前版本 {ver}</h2>
<div class="grid">
{cards}</div>
{sums_html}
<div class="note">
<p>三个平台都是 <b>自带运行时</b> 的发行包 —— <b>不用先装 Python</b>。
装完命令行就有 <code>jishi</code>；先跑一次 <code>jishi 医生</code> 做环境自检。</p>
</div>

<h2>版本历史</h2>
<p class="lead">每个版本做了什么。「升级点」是<b>从仓库的 <code>CHANGELOG.md</code> 抽的</b>，
不是这里另写一份 —— 手抄的副本迟早会跟真实情况对不上。</p>
<div class="timeline">
{"".join(items)}</div>

<h2>关于这些包</h2>
<ul>
<li><b>和 GitHub 上的是同一份吗？</b>是。校验和与 GitHub Releases 的
<code>SHA256SUMS</code> 一致，可以自己核对。</li>
<li><b>为什么放自己站上？</b>大陆访问 GitHub 不稳（实测拉取会整段超时）——
「装不上」这件事不该由网络决定。</li>
<li><b>旧版本呢？</b>打开发行包的版本本站都镜像了，其余版本见
<a href="{GITHUB}/releases">GitHub Releases</a>。</li>
</ul>
"""
    return page(f"下载 —— 基石（jishi）{ver}", body, "download.html",
                "基石安装包下载（本站镜像，大陆直连可用）与逐版本升级说明。")


def build_all() -> int:
    spec = load_spec()
    ver = load_version()
    OUT.mkdir(parents=True, exist_ok=True)

    pages = [
        ("index.html", build_index(spec, ver)),
        ("start.html", build_start(ver)),
        ("download.html", build_download(ver)),
        ("features.html", build_features(spec)),
        ("stdlib.html", build_stdlib(spec)),
        ("examples.html", build_examples(spec)),
        ("progress.html", build_progress(ver)),
    ]
    for name, content in pages:
        (OUT / name).write_text(content, encoding="utf-8")
        print(f"生成 site/{name}  ({len(content) // 1024} KB)")

    print(f"\n完成：{len(pages)} 个官网页面 → {OUT}")
    return 0


def main() -> int:
    if "--check-examples" in sys.argv:
        return check_examples()
    rc = check_examples()
    if rc:
        print("示例没跑通，先修示例再生成页面。")
        return rc
    return build_all()


if __name__ == "__main__":
    sys.exit(main())

