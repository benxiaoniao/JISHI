# -*- coding: utf-8 -*-
"""M168 · 建站：`站点` 模块 + `jishi 建站`（前端第三件）。

## 补的是什么

`examples/projects/基石自述站` 早就是「用基石写的静态站生成器」，但它是**示例** ——
别人要抄 400 行才知道骨架、导航高亮、样式表、sitemap 该怎么写。这一站把这套骨架
提炼成标准库：`布局` / `导航` / `页面` / `资源指纹` / `渲染` / `sitemap` / `写站`
（标准库 34 模块 / 368 函数），并让自述站**改用它**。

## 两条判据是「契约」不是「功能测试」

1. **`test_自述站骨架与旧实现逐字一致`** —— 迁移前那套手写骨架（`<head>` 有哪几行、
   缩进几个空格、`<nav>` 在哪）**原样抄在测试里当 oracle**。自述站是官网的一个子站，
   页面骨架一变，官网那边嵌进去的样式/脚本就可能对不上。所以这里比的是
   **「迁移前后页面字节不变」**，不是「看起来还行」。
2. **`test_写站两次输出字节相同`** —— 可复现。落盘的东西**不许带时间戳**，
   否则同样的输入两次生成两个哈希，发布流程就没法判断「这次到底改了什么」。

## 为什么语料里不调 `写站`

语料在仓库根目录跑（`check_engines` 的老教训：在 cwd 写真实文件会污染下一轮），
所以 `tests/cases/46_站点.jsh` 只测纯函数。`写站` 在**临时目录**里测，见下面。
"""

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                            # noqa: E402

CVM驱动 = '''\
import sys
sys.path.insert(0, r"{root}")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(newline="\\n")
    except Exception:
        pass
from oracle.jishi import cvm_bind, errors as E

src = open("程序.jsh", encoding="utf-8").read()
try:
    cvm_bind.run_source_c(src, filename="程序.jsh")
except E.JishiError as e:
    e.with_source(src.replace("\\r\\n", "\\n").split("\\n"))
    sys.stderr.write(e.render() + "\\n")
    sys.exit(1)
'''

#: 建站程序：写两页 + 自定义样式。**只改输出目录名**，逻辑完全相同。
程序 = '''\
导入 站点
导入 html

令 表 = 站点.布局({{
    "标题后缀": "测试站",
    "品牌": "测试站",
    "品牌副": "test",
    "导航项": [["index.html", "首页"], ["关于.html", "关于"]],
    "脚注": "由基石生成",
    "网址前缀": "https://例子.cn",
}})

令 页们 = [
    {{"路径": "index.html", "标题": "首页",
      "正文": html.标签("h1", "你好") + html.标签("p", "第一段")}},
    {{"路径": "关于.html", "标题": "关于", "正文": html.标签("h1", "关于我")}},
]
令 统计 = 站点.写站("{出}", 页们, 表)
打印(统计["页面数"])
打印(统计["样式文件"])
打印(统计["指纹"])
'''

#: 迁移前自述站用的那套手写骨架 —— **原样抄过来当 oracle**（改它就是改契约）。
旧骨架 = """<!DOCTYPE html>
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

旧导航项 = [["index.html", "概览"], ["里程碑.html", "里程碑"], ["规模.html", "代码规模"],
            ["上手.html", "上手"], ["/", "← 官网"]]


def _旧导航(当前: str) -> str:
    """迁移前自述站里的 `导航()`（原样抄，别改）——它不转义。"""
    出 = ""
    for 址, 字 in 旧导航项:
        类 = ' class="now"' if 址 == 当前 else ""
        出 += f'<a{类} href="{址}">{字}</a>'
    return 出


def _树(根: Path) -> dict:
    """目录 → {相对路径: sha256}。用来比「两次生成字节是否相同」。"""
    出 = {}
    for p in sorted(根.rglob("*")):
        if p.is_file():
            出[str(p.relative_to(根)).replace("\\", "/")] = \
                hashlib.sha256(p.read_bytes()).hexdigest()
    return 出


def _引擎表(tmp: Path):
    from oracle.jishi import serialize
    from oracle.jishi.compiler import compile_source

    表 = {
        "树遍历": [sys.executable, "-m", "oracle.jishi.cli", "--tree", "程序.jsh"],
        "Python VM": [sys.executable, "-m", "oracle.jishi.cli", "程序.jsh"],
        "C VM": [sys.executable, "驱动.py", "程序.jsh"],
    }
    rs = rust_exe_path()
    if rs.is_file():
        表["Rust 宿主"] = [str(rs), "程序.jsh"]
    node = shutil.which("node")
    if node:
        表["Node 宿主"] = [node, str(ROOT / "oracle" / "node" / "index.js"), "程序.json"]
    return 表, serialize, compile_source


def _各引擎建站(tmp_path: Path):
    """每个执行器在自己的目录里建一次站，收集 stdout 与产物指纹表。"""
    表, serialize, compile_source = _引擎表(tmp_path)
    出 = {}
    for 名, 命令 in 表.items():
        家 = tmp_path / 名
        家.mkdir()
        源码 = 程序.format(出="站")
        (家 / "程序.jsh").write_text(源码, encoding="utf-8", newline="\n")
        (家 / "驱动.py").write_text(
            CVM驱动.format(root=str(ROOT).replace("\\", "\\\\")),
            encoding="utf-8", newline="\n")
        if 名 == "Node 宿主":
            (家 / "程序.json").write_text(
                serialize.dumps(compile_source(源码, "程序.jsh")), encoding="utf-8")
        r = subprocess.run(命令, cwd=str(家), capture_output=True, timeout=180,
                           input=b"")
        文 = (r.stdout + r.stderr).decode("utf-8", "replace")
        站 = 家 / "站"
        出[名] = (文, _树(站) if 站.is_dir() else {})
    return 出


# ---------------------------------------------------------------------------
# 一、写站：五引擎逐字一致 + 可复现
# ---------------------------------------------------------------------------

def test_写站五引擎逐字一致(tmp_path):
    """同一份程序在五个执行器上写出的站，**文件名与内容都逐字节相同**。

    比的是「树」：相对路径集合 + 每个文件的 sha256。少了 sitemap、指纹不同、
    CSS 少一行，都会在这里红。
    """
    出 = _各引擎建站(tmp_path)
    assert len(出) >= 3, f"能跑的执行器太少：{list(出)}"
    基准名 = next(iter(出))
    基准文, 基准树 = 出[基准名]
    assert 基准树, f"{基准名} 没写出任何文件 —— 程序本身就没跑通：{基准文[-600:]}"
    不一样 = {名: (文, 树) for 名, (文, 树) in 出.items()
               if 树 != 基准树 or 文 != 基准文}
    assert 不一样 == {}, (
        "各执行器写出的站不一致：\n基准（" + 基准名 + "）："
        + repr(sorted(基准树)) + "\n"
        + "\n".join(f"--- {名} ---\n文件：{sorted(树)}\n输出：{文[-400:]}"
                    for 名, (文, 树) in 不一样.items()))


def test_写站两次输出字节相同(tmp_path):
    """**可复现**：同样的输入，两次生成的目录逐字节相同。

    🔴 这条盯的是「落盘的东西不许带时间戳」。sitemap 里加个 `<lastmod>`、
    页脚加一句「生成于 …」，人看着挺好，但发布流程就再也判断不出
    「这次到底改了什么」—— 每次都是全站都变了。
    """
    甲 = tmp_path / "甲"
    乙 = tmp_path / "乙"
    for 家 in (甲, 乙):
        家.mkdir()
        (家 / "程序.jsh").write_text(程序.format(出="站"), encoding="utf-8",
                                     newline="\n")
        subprocess.run([sys.executable, "-m", "oracle.jishi.cli", "程序.jsh"],
                       cwd=str(家), capture_output=True, timeout=180, input=b"")
    树甲, 树乙 = _树(甲 / "站"), _树(乙 / "站")
    assert 树甲 == 树乙, f"两次生成不一样：\n{树甲}\n{树乙}"
    assert "sitemap.xml" in 树甲
    assert any(n.startswith("style.") and n.endswith(".css") for n in 树甲), \
        f"样式表名没带指纹：{sorted(树甲)}"


# ---------------------------------------------------------------------------
# 二、契约：自述站迁移后页面骨架逐字不变
# ---------------------------------------------------------------------------

def test_自述站骨架与旧实现逐字一致():
    """`站点.页面` 渲染出的整页 == 迁移前那套手写骨架（逐字）。

    自述站是官网的子站（部署名 `about/`）。骨架一变，官网嵌进去的东西就可能对不上，
    所以这里拿**迁移前的实现**当 oracle —— 不是「看起来差不多」，是逐字节相同。
    """
    from oracle.jishi.stdlib import 站点 as S

    from oracle.jishi.stdlib import html as H

    样式 = "/* 自述站样式（内容与本条判据无关，只为让指纹可算） */\n"
    表 = S.布局({
        "标题后缀": "基石自述",
        "品牌": "基石",
        "品牌副": "jishi",
        "导航项": 旧导航项,
        "脚注": "由基石生成器产出 · 版本 0.3.0",
        "样式": 样式,
        "指纹": False,   # 基石里的 `假`；这是 Python 侧判据，直接写 False
    })
    新 = S.页面({"路径": "里程碑.html", "标题": "里程碑",
                 "正文": "<h1>里程碑</h1>", }, 表)

    # 旧实现（迁移前 `生成.jsh` 的 页面(标题, 当前, 正文, 脚注)）
    旧 = (旧骨架.replace("{0}", "里程碑").replace("{1}", _旧导航("里程碑.html"))
          .replace("{2}", "<h1>里程碑</h1>")
          .replace("{3}", "由基石生成器产出 · 版本 0.3.0"))

    assert 新 == 旧, ("站点.页面 与迁移前的手写骨架不一致 —— 自述站的页面会变。\n"
                    "如果**有意**改骨架，请同时改本测试里的 `旧骨架` 与官网那边。\n"
                    f"--- 新 ---\n{新}\n--- 旧 ---\n{旧}")


# ---------------------------------------------------------------------------
# 三、`jishi 建站`：出来的目录**直接能传服务器**
# ---------------------------------------------------------------------------

def _跑建站(命令: list, 家: Path, 名: str):
    return subprocess.run(命令 + ["建站", 名], cwd=str(家),
                          capture_output=True, timeout=300, input=b"")


def test_建站命令出的目录能直接用(tmp_path):
    """`jishi 建站 名字` → `名字/_站/`，里面是**能直接传服务器**的静态文件。

    - 有 `index.html` / `关于.html` / `sitemap.xml`；
    - 样式表名带**内容指纹**，且指纹与文件内容**真的对得上**（不是随便贴一个）；
    - 页面里引用的就是那个带指纹的名字（否则传上去 404）。
    """
    家 = tmp_path / "工作"
    家.mkdir()
    rs = rust_exe_path()
    命令 = ([str(rs)] if rs.is_file()
            else [sys.executable, "-m", "oracle.jishi.cli"])
    r = _跑建站(命令, 家, "我的站")
    assert r.returncode == 0, (r.stdout + r.stderr).decode("utf-8", "replace")[-800:]

    站 = 家 / "我的站" / "_站"
    assert 站.is_dir(), f"没生成 _站/：{sorted(p.name for p in (家 / '我的站').iterdir())}"
    文件 = {p.name for p in 站.iterdir()}
    assert {"index.html", "关于.html", "sitemap.xml"} <= 文件, 文件
    css = [n for n in 文件 if n.startswith("style.") and n.endswith(".css")]
    assert css, f"样式表没带指纹：{文件}"

    from oracle.jishi.stdlib import 站点 as S
    内容 = (站 / css[0]).read_text(encoding="utf-8")
    assert css[0] == f"style.{S.资源指纹(内容)}.css", (
        f"样式表名与内容指纹对不上：{css[0]} vs {S.资源指纹(内容)}")
    首页 = (站 / "index.html").read_text(encoding="utf-8")
    assert f'href="{css[0]}"' in 首页, "页面里引用的还是不带指纹的名字（传上去会 404）"
    assert "我的站" in 首页


def test_建站两宿主产物一致(tmp_path):
    """Python 与 Rust 的 `建站`：脚手架 + 产物**逐字节相同**。"""
    rs = rust_exe_path()
    if not rs.is_file():
        import pytest
        pytest.skip("没有 Rust 可执行文件")

    甲 = tmp_path / "甲"
    乙 = tmp_path / "乙"
    甲.mkdir()
    乙.mkdir()
    _跑建站([sys.executable, "-m", "oracle.jishi.cli"], 甲, "同名")
    _跑建站([str(rs)], 乙, "同名")
    树甲, 树乙 = _树(甲 / "同名"), _树(乙 / "同名")
    assert 树甲 == 树乙, f"两宿主建站不一致：\n{树甲}\n{树乙}"


# ---------------------------------------------------------------------------
# 四、源码级护栏：三个宿主都得真有这个模块
# ---------------------------------------------------------------------------

函数名 = ["布局", "导航", "页面", "资源指纹", "渲染", "sitemap", "写站"]


def test_三宿主都有站点模块():
    """Rust / Node 的模块表里都得有这 7 个函数。

    ⚠️ 为什么要源码级判据：C VM 与 Python 走同一份实现，**缺的永远是 Rust / Node**，
    而它们缺了只会在「真正导入时」才暴露（`test_m51` 会发现，但那条报的是
    「标准库宽度不一致」，不告诉你缺哪个函数）。
    """
    rust = (ROOT / "rust" / "src" / "stdlib" / "site.rs").read_text(encoding="utf-8")
    node = (ROOT / "oracle" / "node" / "vm.js").read_text(encoding="utf-8")
    for 名 in 函数名:
        assert f'"{名}" =>' in rust, f"Rust 的 `站点` 少函数：{名}"
        assert f"'{名}': new R.Builtin" in node, f"Node 的 `站点` 少函数：{名}"
    assert '("站点", site::module)' in (ROOT / "rust" / "src" / "stdlib" / "mod.rs"
                                       ).read_text(encoding="utf-8")
    assert "'站点': new R.Module('站点', SITE_FUNCS)" in node


def test_默认样式三宿主同源():
    """CSS 常量不是**手抄**的：Node 那份必须与 Python 的 `默认样式` 逐字相同。

    M166 的教训 —— 手抄 CSS 漏一个引号，页面照样能看，只有逐字比才抓得住。
    这里直接从 JS 源码里把那个字符串抠出来与 Python 比，**不跑节点**也能查。
    """
    import json
    import re
    from oracle.jishi.stdlib.站点 import 默认样式

    node = (ROOT / "oracle" / "node" / "vm.js").read_text(encoding="utf-8")
    m = re.search(r"const SITE_默认样式 = (\".*?\");\n", node, re.S)
    assert m, "Node 的 vm.js 里找不到 `const SITE_默认样式 = \"…\";`"
    assert json.loads(m.group(1)) == 默认样式, "Node 那份 CSS 与 Python 不一致"

    rust = (ROOT / "rust" / "src" / "stdlib" / "site.rs").read_text(encoding="utf-8")
    m2 = re.search(r'pub\(crate\) const 默认样式: &str = r#"(.*?)"#;', rust, re.S)
    assert m2, "Rust 的 site.rs 里找不到 `默认样式` 常量"
    assert m2.group(1) == 默认样式, "Rust 那份 CSS 与 Python 不一致"
