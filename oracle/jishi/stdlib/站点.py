# -*- coding: utf-8 -*-
"""基石标准库：站点。把「一堆页面」写成**一个能直接传服务器的静态站**。

**为什么需要**：`examples/projects/基石自述站` 早就是用基石写的静态站生成器，
但它是**示例** —— 别人要抄 400 行才知道「页面骨架、导航高亮、样式表、sitemap」
该怎么写。本模块把这套骨架提炼出来，让建站变成七八个调用。

## 四条硬规矩

1. **可复现**：同一份输入、两次生成，**字节完全相同**。所以本模块
   **绝不写时间戳**（sitemap 里没有 `lastmod`、页脚没有「生成于 …」），
   一切顺序都是**给定顺序或排序后的顺序**，不走哈希遍历。
2. **资源指纹**：静态资源的文件名带内容哈希（`style.a1b2c3d4.css`），
   内容一变名字就变 —— 用户的浏览器缓存不会打架，也就不需要 `?v=13` 这种手记版本号。
3. **服务端渲染、不引框架、不引 CDN**：产物是一堆 `.html` + 一个 `.css` +
   一个 `sitemap.xml`，丢进任何静态托管（Nginx / Pages / 对象存储）都能跑。
4. **五引擎逐字一致**：全部是纯字符串运算，没有随机、没有时间、没有文件系统探测
   （除了 `写站` 真的落盘）。指纹用 **FNV-1a 32 位** —— 三行就能写出来，
   Python / Rust / JS 各实现一份也不会跑偏（不用 SHA-256：那要拖一份实现进来）。

## 数据长什么样

```jsh
导入 站点
令 布局 = 站点.布局({
    "标题后缀": "我的博客",
    "品牌": "我的博客", "品牌副": "blog",
    "导航项": [["index.html", "首页"], ["关于.html", "关于"]],
    "脚注": "由基石生成",
})
令 页们 = [
    {"路径": "index.html", "标题": "首页", "正文": "<h1>你好</h1>"},
    {"路径": "关于.html", "标题": "关于", "正文": "<h1>关于我</h1>"},
]
站点.写站("_站", 页们, 布局)
```

## 与 `html` 模块的分工

`html` 造**片段**（一个卡片、一张表、一个表单），`站点` 管**整站**
（页面骨架、导航、落盘、指纹、sitemap）。`站点.页面` 内部就用 `html.转义`。
"""

from __future__ import annotations

import os

from ..errors import RunTypeError
from .html import 转义

__all__ = [
    "布局", "导航", "页面", "资源指纹", "渲染", "sitemap", "写站",
]

#: 默认样式表。够写一篇文章、一张表、一段代码 —— 不追求好看，追求「开箱不丑」。
#: ⚠️ 这份字符串**三个宿主各有一份**（Rust / Node 从 Python 侧导出，不是手抄，
#: 见 M166 的教训：手抄 CSS 漏一个引号，页面照样能看，只有逐字比才抓得住）。
默认样式 = """/* 基石站点的默认样式（由「站点」模块写出） */
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
table { width: 100%; border-collapse: collapse; background: var(--card);
        border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--line);
         vertical-align: top; font-size: 15px; }
th { background: var(--accent-soft); font-weight: 600; }
tr:last-child td { border-bottom: none; }
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
code { background: #eef1f5; padding: 1px 5px; border-radius: 4px;
       font-family: "Cascadia Code", Consolas, monospace; font-size: 14px; }
pre { background: #1f2430; color: #e6e9ef; padding: 14px 16px; border-radius: 8px;
      overflow-x: auto; }
pre code { background: none; color: inherit; padding: 0; }
footer { border-top: 1px solid var(--line); color: var(--muted); font-size: 13px;
         padding: 18px 28px 40px; max-width: 960px; margin: 0 auto; }
"""

#: 布局的默认值。`布局()` 就是「这份默认 + 用户给的覆盖」。
#: ⚠️ 键名一律**中文**，与语言其他部分一致；`样式文件` 默认 `style.css`，
#: `写站` 会把它换成带指纹的名字。
_布局默认 = {
    "标题分隔": " · ",
    "标题后缀": "",
    "品牌": "",
    "品牌副": "",
    "导航项": [],
    "脚注": "",
    "语言": "zh-CN",
    "样式文件": "style.css",
    "样式": 默认样式,
    "网址前缀": "",
    #: 样式表要不要改成带指纹的名字。默认要（缓存不打架）。
    #: 给 `假` 是为了一种真实场景：**别人已经按固定文件名引用了你的 CSS**
    #: （比如自述站嵌在官网的一个子目录里，外层页面写着 `基石自述/style.css`）。
    "指纹": True,
}


def _要文本(名: str, 值) -> str:
    """要一个文本参数。

    ⚠️ **只收文本，不收数字**（虽然 `文本(3)` 是常识）：这里一旦开始做类型转换，
    三个宿主就得把「数字怎么转文本」的规则各实现一遍（`0.1+0.2` 的位数、
    `-0.0`、大整数…），而那条路最容易出现「三个宿主各说各话」。
    要拼数字请显式写 `文本(3)` —— 错误文案里也这么提示。
    """
    if isinstance(值, str):
        return 值
    raise RunTypeError(
        f"「{名}」要传文本，但给的是{type(值).__name__}（要拼数字请写 文本(值)）")


def _取(项, 键: str, 默认):
    """从字典里取一个键（没有就给默认）。"""
    if not isinstance(项, dict):
        raise RunTypeError(f"要传字典，但给的是{type(项).__name__}")
    return 项[键] if 键 in 项 else 默认


def 布局(选项=None) -> dict:
    """补齐一份布局。给了什么就用什么，没给的用默认值。

    可给的键：`标题分隔` / `标题后缀` / `品牌` / `品牌副` / `导航项` /
    `脚注` / `语言` / `样式文件` / `样式` / `网址前缀` / `指纹`。

    - `标题后缀`：页面 `<title>` 是「标题 + 分隔 + 后缀」（空后缀则只有标题）；
    - `导航项`：`[["index.html", "首页"], …]`，`页面` 会把**当前页**那项加上
      `class="now"`；
    - `样式`：CSS 文本。留空（`""`）= 不写样式表；不传 = 用内置默认样式；
    - `网址前缀`：sitemap 里的绝对前缀（`https://例子.cn`）。空 = 写相对路径；
    - `指纹`：`真`（默认）= 样式表改名为 `style.<指纹>.css`；`假` = 保持
      `样式文件` 原名（有人按固定名字引用你的 CSS 时用）。

    📌 **返回的是一份新字典**，改它不影响默认值（默认值是模块级常量）。
    """
    if 选项 is None:
        选项 = {}
    出 = dict(_布局默认)
    for 键 in _布局默认:
        if 键 in 选项:
            出[键] = 选项[键]
    # 导航项要规范成「[地址, 文字] 对的列表」——允许用户传元组，也允许漏文字
    项们 = []
    for 项 in 出["导航项"]:
        if not isinstance(项, (list, tuple)) or len(项) < 2:
            raise RunTypeError(
                "「导航项」要是 [\"地址\", \"文字\"] 这样的对，"
                f"但有一项是{type(项).__name__}")
        项们.append([_要文本("导航项的地址", 项[0]), _要文本("导航项的文字", 项[1])])
    出["导航项"] = 项们
    return 出


def 导航(项们, 当前="") -> str:
    """把导航项拼成 `<nav>` 里的那一串链接。`当前` 是**地址**（不是标题）。

    当前页那一条带 `class="now"`，样式表里据此高亮。
    """
    片 = []
    for 项 in 项们:
        if not isinstance(项, (list, tuple)) or len(项) < 2:
            raise RunTypeError(
                "「导航」的每一项要是 [\"地址\", \"文字\"]，"
                f"但有一项是{type(项).__name__}")
        址 = _要文本("导航项的地址", 项[0])
        字 = _要文本("导航项的文字", 项[1])
        类 = ' class="now"' if 址 == 当前 else ""
        片.append(f'<a{类} href="{转义(址)}">{转义(字)}</a>')
    return "".join(片)


def 页面(页, 布局表) -> str:
    """渲染**一整页** HTML（含 `<!DOCTYPE html>` 与 header/nav/footer 骨架）。

    `页` 是一个字典：`{"路径": "index.html", "标题": "首页", "正文": "<h1>…</h1>"}`，
    可选 `"脚注"`（覆盖布局里的脚注，个别页要单独署名时用）。

    ⚠️ `正文` 是**原样插进去的**（它本来就是 HTML 片段，通常由 `html` 模块拼）。
    所以**绝不能把用户输入直接塞进 `正文`** —— 用户输入一律先走 `html.转义`。
    """
    if not isinstance(页, dict):
        raise RunTypeError(f"「页面」要传字典，但给的是{type(页).__name__}")
    标题 = _要文本("页的标题", _取(页, "标题", ""))
    正文 = _要文本("页的正文", _取(页, "正文", ""))
    路径 = _要文本("页的路径", _取(页, "路径", ""))
    # 脚注：页自己的优先，否则用布局的
    页脚注 = _取(页, "脚注", None)
    脚注 = 布局表["脚注"] if 页脚注 is None else _要文本("脚注", 页脚注)

    后缀 = 布局表["标题后缀"]
    全标题 = f"{标题}{布局表['标题分隔']}{后缀}" if 后缀 else 标题
    品牌副 = 布局表["品牌副"]
    牌子 = f"<span>{转义(品牌副)}</span>" if 品牌副 else ""
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="{转义(布局表["语言"])}">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{转义(全标题)}</title>\n"
        f'<link rel="stylesheet" href="{转义(布局表["样式文件"])}">\n'
        "</head>\n"
        "<body>\n"
        '<header class="top">\n'
        f'  <div class="brand">{转义(布局表["品牌"])}{牌子}</div>\n'
        f"  <nav>{导航(布局表['导航项'], 路径)}</nav>\n"
        "</header>\n"
        "<main>\n"
        f"{正文}\n"
        "</main>\n"
        f"<footer>{转义(脚注)}</footer>\n"
        "</body>\n"
        "</html>\n"
    )


def 资源指纹(内容) -> str:
    """算内容指纹：**8 位小写十六进制**（FNV-1a 32 位，按 UTF-8 字节算）。

    用途是给静态资源命名（`style.a1b2c3d4.css`）。**别当密码学哈希用** ——
    它不安全，只是「内容变了名字就变」。
    """
    文 = 内容 if isinstance(内容, str) else _要文本("内容", 内容)
    希 = 2166136261
    for 字 in 文:
        for 字节 in 字.encode("utf-8"):
            希 = 希 ^ 字节
            希 = (希 * 16777619) & 0xFFFFFFFF
    return f"{希:08x}"


def 渲染(页们, 布局表) -> list:
    """把每一页渲染成 `[路径, 内容]` 对，**不落盘**。

    要「先在内存里看一眼」「只写一部分」「交给别人去写」时用它；
    直接出站用 `写站`。
    """
    出 = []
    for 页 in 页们:
        出.append([_要文本("页的路径", _取(页, "路径", "")), 页面(页, 布局表)])
    return 出


def _拼网址(前缀: str, 路径: str) -> str:
    """把前缀和路径拼成一个像样的网址（不重复斜杠、也不漏）。

    `("https://例子.cn", "a.html")` → `https://例子.cn/a.html`；
    `("https://例子.cn/", "/a.html")` → `https://例子.cn/a.html`；
    前缀为空时原样给路径（本地预览也能直接用）。
    """
    if not 前缀:
        return 路径
    return 前缀.rstrip("/") + "/" + 路径.lstrip("/")


def sitemap(页们, 前缀="") -> str:
    """生成 `sitemap.xml` 文本。

    `页们` 可以是**页字典列表**（取每页的 `路径`），也可以是**路径文本列表**。
    `前缀` 是绝对网址前缀（`https://例子.cn`）；留空就写相对路径
    （搜索引擎要求绝对地址，所以真上线时**应该**给它）。
    """
    址 = []
    for 页 in 页们:
        if isinstance(页, dict):
            址.append(_要文本("页的路径", _取(页, "路径", "")))
        else:
            址.append(_要文本("页的路径", 页))
    # 🔴 排序两件事都要说清：
    # ① **要排序**：sitemap 是给机器读的，顺序不能取决于调用方给的顺序，
    #    否则「同样一组页面」写不出同样的字节；
    # ② **按 UTF-8 字节排**（`key=...encode("utf-8")`）：三个宿主的默认排序规则
    #    其实不一样（Python 按码点、Rust 按 UTF-8 字节、JS 按 UTF-16 单元），
    #    遇到基本平面以外的字符（emoji 文件名）就会**各自排各自的**。
    #    按字节排是唯一三处都能精确复刻的口径。
    址 = sorted(set(址), key=lambda s: s.encode("utf-8"))
    片 = ['<?xml version="1.0" encoding="UTF-8"?>',
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for 一 in 址:
        片.append(f"  <url><loc>{转义(_拼网址(前缀, 一))}</loc></url>")
    片.append("</urlset>")
    return "\n".join(片) + "\n"


def 写站(输出目录, 页们, 布局表) -> dict:
    """把整站写进 `输出目录`：页面 + 带指纹的样式表 + sitemap。

    返回一份统计（`页面数` / `样式文件` / `指纹` / `sitemap`），供程序打印或判断。

    #### 样式为什么改名

    `布局["样式"]` 非空时，CSS 写成 `style.<指纹>.css`，并把每页里
    `href="style.css"` 那一处**替换成新名字**。内容一改指纹就变、文件名就变，
    用户浏览器不会拿到旧缓存 —— 不需要手记 `?v=`。

    ⚠️ 替换只认**这一个字面量**（`href="style.css"`）。你要是把样式表写成别的
    文件名（`布局["样式文件"] = "a.css"`），替换目标也随之变成 `href="a.css"`。

    #### 可复现

    落盘的字节**不含任何时间戳**，同一份输入两次生成的目录逐字节相同。
    """
    样式 = 布局表["样式"]
    样式文件 = 布局表["样式文件"]
    指针 = bool(布局表["指纹"])
    指纹 = ""
    if 样式:
        指纹 = 资源指纹(样式)
        if 指针:
            名, 扩 = os.path.splitext(样式文件)
            真名 = f"{名}.{指纹}{扩}"
        else:
            # 留在原文件名上（有人按固定名字引用它 —— 见 `布局` 里 `指纹` 的说明）
            真名 = 样式文件
    else:
        真名 = ""
    os.makedirs(输出目录, exist_ok=True)

    页数 = 0
    for 页 in 页们:
        路径 = _要文本("页的路径", _取(页, "路径", ""))
        if not 路径:
            raise RunTypeError("「写站」的每一页都要有「路径」（如 index.html）")
        内容 = 页面(页, 布局表)
        if 真名 and 真名 != 样式文件:
            内容 = 内容.replace(f'href="{样式文件}"', f'href="{真名}"')
        全 = os.path.join(输出目录, 路径)
        父 = os.path.dirname(全)
        if 父:
            os.makedirs(父, exist_ok=True)
        with open(全, "w", encoding="utf-8", newline="\n") as f:
            f.write(内容)
        页数 += 1

    if 真名:
        with open(os.path.join(输出目录, 真名), "w", encoding="utf-8",
                  newline="\n") as f:
            f.write(样式)
    with open(os.path.join(输出目录, "sitemap.xml"), "w", encoding="utf-8",
              newline="\n") as f:
        f.write(sitemap(页们, 布局表["网址前缀"]))

    return {"页面数": 页数, "样式文件": 真名, "指纹": 指纹, "sitemap": "sitemap.xml"}
