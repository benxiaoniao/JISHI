# 基石自述站 —— 用基石自己写的一个静态站点生成器
#
# 它不写死任何数字：版本号、测试数、能力探针、里程碑、模块规模、示例数
# 全部**当场从仓库里读出来**，于是这份「自述」不会随时间失真。
#
# 用法（在仓库根目录跑）：
#     jishi examples/projects/基石自述站/生成.jsh
#     jishi examples/projects/基石自述站/生成.jsh 输出目录 仓库根目录
#
# 三个执行器都能跑：`jishi 生成.jsh` / `jishi 生成.jsh --vm` / `--cvm`。

导入 文件
导入 路径
导入 文本 为 文本库  # 别名导入：模块「文本」会遮蔽内建 文本(...)，
        # 而 f-string 插值正是靠它把值转成文本（见 docs/语言规格.md）
导入 系统
导入 html

# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

函数 夹取(源, 左标记, 右标记):
    # 取「左标记」之后、第一个「右标记」之前的内容
    令 起 = 源.查找(左标记)
    如果 起 < 0:
        返回 ""
    令 后半 = 源[起 + 长度(左标记):]
    令 尾 = 后半.查找(右标记)
    如果 尾 < 0:
        # ⚠️ 找不到右标记时**必须返回空**，不能把「后半段」整段吐出去：
        #    README 里「能力探针（当前 **42/50 可用**…」的写法一变，
        #    旧的右标记 `** 可用` 就匹配不上了 —— 于是整篇 README 被当成
        #    一个数字塞进统计卡片，页面上糊出一大坨原始 Markdown
        #    （2026-10-26 用户报「自述页写得不行」，真凶就是这一行）。
        返回 ""
    返回 后半[0:尾]

# 转义改用标准库 `html.转义`（M50）：原先这里手写四次 `替换`，
# 而且**漏了引号**——属性值里带引号的内容会破坏标签结构。
# `html.转义` 把 `& < > " '` 五个都换掉。
函数 转义(源):
    返回 html.转义(源)

函数 简单排版(源):
    # 极简 markdown：去掉 ** 加粗标记，把 `代码` 变成 <code>代码</code>
    令 平 = 转义(源).替换("**", "")
    令 段 = 平.拆分("`")
    令 出 = 段[0]
    遍历 i 在 范围(1, 长度(段)):
        如果 i % 2 == 1:
            出 = 出 + "<code>" + 段[i]
        否则:
            出 = 出 + "</code>" + 段[i]
    返回 出

函数 读行(路径):
    返回 文件.按行读(路径)

函数 找行(路径, 关键字):
    # 第一条含该关键字的行；找不到返回「空文本」
    遍历 行 在 文件.按行读(路径):
        如果 行.包含(关键字):
            返回 行
    返回 ""

函数 数文件(根目录, 后缀):
    # M108 起交给 `路径.递归列出`（以前这里是一个手写递归 —— 正是这类
    # 「数一数有几个文件」的小活，标准库里就该有）。
    返回 长度(路径.递归列出(根目录, 后缀))

函数 量规模(目录, 后缀):
    # 返回 [[行数, 文件名], ...]，按行数升序
    令 行们 = []
    遍历 名 在 路径.按模式找(目录, "*" + 后缀, 假):
        令 全 = 文件.路径拼接(目录, 名)
        行们.追加([长度(文件.按行读(全)), 名])
    行们.排序()
    返回 行们

函数 数用例(目录):
    # 在测试文件里数 def test 的行数——这就是「用基石统计基石自己」
    令 个数 = 0
    遍历 名 在 路径.按模式找(目录, "test_*.py", 假):
        遍历 行 在 文件.按行读(文件.路径拼接(目录, 名)):
            如果 行.开头是("def test") 或 行.开头是("    def test"):
                个数 = 个数 + 1
    返回 个数

# ---------------------------------------------------------------------------
# 采集数据
# ---------------------------------------------------------------------------

函数 采集(根):
    令 数据 = {}
    令 版本行 = 找行(文件.路径拼接(根, "pyproject.toml"), "version =")
    数据["版本"] = 夹取(版本行, "\"", "\"")
    令 自述 = 文件.读文本(文件.路径拼接(根, "README.md"))
    数据["测试数"] = 夹取(自述, "测试现状：", " 项")
    数据["探针"] = 夹取(自述, "（当前 **", "** 可用")
    数据["模块"] = 量规模(文件.路径拼接(根, "oracle/jishi"), ".py")
    数据["示例数"] = 数文件(文件.路径拼接(根, "examples"), ".jsh")
    数据["用例数"] = 数用例(文件.路径拼接(根, "tests"))
    数据["里程碑"] = 读里程碑(文件.路径拼接(根, "PROGRESS.md"))
    返回 数据

函数 读里程碑(路径):
    令 全部 = []
    遍历 行 在 文件.按行读(路径):
        如果 非 行.开头是("| **M"):
            继续
        令 格 = 行.拆分("|")
        如果 长度(格) < 5:
            继续
        全部.追加([格[1].去空白(), 格[2].去空白(), 格[3].去空白()])
    返回 全部

# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

令 样式 = """/* 基石自述站样式（由基石生成器一并写出） */
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

令 骨架 = """<!DOCTYPE html>
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

函数 导航(当前):
    令 项 = [["index.html", "概览"], ["里程碑.html", "里程碑"],
            ["规模.html", "代码规模"], ["上手.html", "上手"], ["/", "← 官网"]]
    令 出 = ""
    遍历 项 在 项:
        如果 项[0] == 当前:
            出 = 出 + "<a class=\"now\" href=\"" + 项[0] + "\">" + 项[1] + "</a>"
        否则:
            出 = 出 + "<a href=\"" + 项[0] + "\">" + 项[1] + "</a>"
    返回 出

函数 页面(标题, 当前, 正文, 脚注):
    返回 文本库.格式化(骨架, 标题, 导航(当前), 正文, 脚注)

函数 卡片(名, 值, 补充):
    返回 ("<div class=\"card\"><div class=\"k\">" + 名
           +"</div><div class=\"v\">" + 值 + " <small>" + 补充
           +"</small></div></div>")

函数 首页(数据):
    令 卡 = "<div class=\"cards\">"
    卡 = 卡 + 卡片("当前版本", 数据["版本"], "")
    卡 = 卡 + 卡片("测试用例", 数据["测试数"], "项")
    卡 = 卡 + 卡片("已完成里程碑", `{长度(数据["里程碑"])}`, "个")
    卡 = 卡 + 卡片("可运行示例", `{数据["示例数"]}`, "个")
    卡 = 卡 + "</div>"

    令 正文 = ("<h1>基石是什么</h1>"
        +"<p class=\"lead\">一门<b>用中文写程序</b>的语言。关键字、报错、"
        +"标准库、工具链都是中文；语法贴近 Python 的直觉 —— "
        +"会 Python 的人半小时能上手，没学过编程的人少一道英语门槛。</p>"

        +"<h2>它不是什么</h2>"
        +"<p>先说清楚边界，省得你抱着错期待看下去。</p>"
        +"<ul>"
        +"<li><b>不是「把英文关键字翻译成中文」的外壳。</b>市面上不少中文编程停在"
        +"「关键字替换表」那一层：报错还是英文、标准库还是英文名，一遇到没覆盖的"
        +"情况就退回英文世界。基石的关键字、内建函数、标准库、报错文案、工具链"
        +"是<b>原生中文</b>，不是套壳。</li>"
        +"<li><b>不打算取代 Python。</b>它想解决的是「中文母语者的第一门语言」"
        +"和「写点小工具时不被英文报错卡住」这两类场景，不是去重写别人的系统。</li>"
        +"<li><b>不是玩具。</b>要求很实在：一门语言该有的东西（编辑器补全、调试器、"
        +"包管理、测试）都得有，而且<b>五个执行器跑同一份字节码，输出逐字节一致</b>。"
        +"这四页「自述」本身就是用基石写的程序生成的。</li>"
        +"</ul>"

        +"<h2>为什么值得花十分钟</h2>"
        +"<div class=\"cards\">"
        +"<div class=\"card\"><div class=\"k\">读代码不用双语切换</div>"
        +"<div class=\"v\" style=\"font-size:15px;font-weight:400\">"
        +"变量、函数、类型都是中文词，读一段程序不用在两种语言之间来回翻译。</div></div>"
        +"<div class=\"card\"><div class=\"k\">报错是给人看的</div>"
        +"<div class=\"v\" style=\"font-size:15px;font-weight:400\">"
        +"精确到行列，说清「哪个方法不存在」，还列出这个类型<b>到底有哪些方法</b>。</div></div>"
        +"<div class=\"card\"><div class=\"k\">全链路中文</div>"
        +"<div class=\"v\" style=\"font-size:15px;font-weight:400\">"
        +"编辑器补全、调试器、格式化、包管理、错误码查询 —— 一套，不是一个语法糖。</div></div>"
        +"</div>"

        +"<h2>它长这样</h2>"
        +"<pre><code>函数 评级(分数):\n"
        +"    如果 分数 >= 90:\n"
        +"        返回 \"优秀\"\n"
        +"    否则如果 分数 >= 60:\n"
        +"        返回 \"及格\"\n"
        +"    返回 \"待努力\"\n"
        +"\n"
        +"令 成绩 = [[\"小明\", 95], [\"小红\", 72], [\"小刚\", 48]]\n"
        +"遍历 项 在 成绩:\n"
        +"    打印(`{项[0]}：{评级(项[1])}`)</code></pre>"
        +"<p>运行结果：</p>"
        +"<pre><code>小明：优秀\n小红：及格\n小刚：待努力</code></pre>"

        +"<h2>写错的时候</h2>"
        +"<p>报错会指出位置、说清原因，并给出能照着改的提示：</p>"
        +"<pre><code>错误 E2002：类型错误（「列表」没有方法「长度」）\n"
        +"  ┌─ 名单.jsh:2:6\n"
        +"  │\n"
        +"  2 │ 打印(名单.长度())\n"
        +"    │      ^\n"
        +"  │\n"
        +"  提示：可用的方法：包含、反转、弹出、归约、排序、排序按、"
        +"插入、映射、清空、移除、索引、计数、过滤、追加</code></pre>"
        +"<p>这类提示不是补充说明，是<b>一整套纪律</b>：能找到「你是不是想写…」的"
        +"就给候选，找不到就<b>列出这里能用的东西</b>，不说「请查阅文档」这种空话。</p>"

        +"<h2>现在到哪了</h2>"
        +卡
        +"<p>这些数字是<b>生成这一页时</b>从仓库读出来的，不是手写在页面上的 —— "
        +"做法见页脚。</p>"

        +"<h2>往下读</h2>"
        +"<ul>"
        +"<li><a href=\"上手.html\">上手</a> —— 十分钟跑起第一个程序</li>"
        +"<li><a href=\"/start.html\">官网上的教程</a> —— 11 章，从打印一行字到"
        +"一个能用的记账程序</li>"
        +"<li><a href=\"/stdlib.html\">标准库</a> —— 别自己造轮子，先看有没有现成的</li>"
        +"<li><a href=\"/download.html\">下载</a> —— Windows / macOS / Linux，"
        +"以及浏览器里的在线试玩</li>"
        +"</ul>")

    令 最近 = "<h2>最近的里程碑</h2>"
    最近 = 最近 + "<table><tr><th>里程碑</th><th>内容</th><th>完成</th></tr>"
    遍历 i 在 范围(0, 最小(3, 长度(数据["里程碑"]))):
        令 条 = 数据["里程碑"][i]
        最近 = 最近 + ("<tr><td>" + 转义(条[0]) + "</td><td>" + 简单排版(条[1])
                       +"</td><td class=\"num\">" + 转义(条[2]) + "</td></tr>")
    最近 = 最近 + "</table>"
    最近 = 最近 + ("<p>全部 "
        +`{长度(数据["里程碑"])}` + " 个里程碑见<a href=\"里程碑.html\">里程碑</a>页。</p>")

    返回 页面("概览", "index.html", 正文 + 最近,
              `由基石生成器产出 · 版本 {数据["版本"]}`)

函数 总行数(模块):
    令 合计 = 0
    遍历 条 在 模块:
        合计 = 合计 + 条[0]
    返回 合计

函数 里程碑页(数据):
    令 表 = "<table><tr><th>里程碑</th><th>内容</th><th>完成</th></tr>"
    遍历 条 在 数据["里程碑"]:
        表 = 表 + ("<tr><td>" + 转义(条[0]) + "</td><td>" + 简单排版(条[1])
                   +"</td><td class=\"num\">" + 转义(条[2]) + "</td></tr>")
    表 = 表 + "</table>"
    令 正文 = ("<h1>里程碑</h1>"
        +`<p class="lead">全部 {长度(数据["里程碑"])} 个已完成里程碑，`
        +"最新的排在最前面。这张表直接来自 <code>PROGRESS.md</code> 的时间线，"
        +"不是另抄一份。</p>" + 表)
    返回 页面("里程碑", "里程碑.html", 正文, "里程碑 · 由基石生成器产出")

函数 规模页(数据):
    令 表 = "<table><tr><th>模块</th><th>行数</th><th>占比</th></tr>"
    令 最大行 = 数据["模块"][长度(数据["模块"]) - 1][0]
    令 倒序 = 数据["模块"][::-1]
    遍历 条 在 倒序:
        令 宽 = 整数(条[0] * 100 / 最大行)
        表 = 表 + ("<tr><td><code>oracle/jishi/" + 转义(条[1]) + "</code></td>"
                   +"<td class=\"num\">" + `{条[0]}` + "</td>"
                   +"<td><div class=\"bar\" style=\"width:" + `{宽}`
                   +"%\"></div></td></tr>")
    表 = 表 + "</table>"
    令 正文 = ("<h1>代码规模</h1>"
        +"<p class=\"lead\">统计范围：<code>oracle/jishi/</code> 下这一层的每个 "
        +"<code>.py</code>（不含标准库子目录与构建产物）。</p>"
        +表
        +"<h2>测试与示例</h2>"
        +"<div class=\"cards\">"
        +卡片("测试函数", `{数据["用例数"]}`, "个 def test")
        +卡片("示例程序", `{数据["示例数"]}`, "个 .jsh")
        +卡片("模块行数", `{总行数(数据["模块"])}`, "行（本层）")
        +"</div>"
        +"<p>这些数字也是**当场数出来**的：测试数是把所有 "
        +"<code>def test</code> 数了一遍，示例数是递归数 "
        +"<code>examples/</code> 下的 .jsh 文件。数得对不对，看仓库就知道。</p>")
    返回 页面("代码规模", "规模.html", 正文, "代码规模 · 由基石生成器产出")

函数 上手页(数据):
    令 正文 = ("<h1>上手</h1>"
        +"<p class=\"lead\">四个命令就能用起来（当前版本 "
        +数据["版本"] + "）。</p>"
        +"<h2>1. 安装</h2>"
        +"<pre><code>pip install 基石\n# 或者用发行版里的安装程序 / 免安装压缩包</code></pre>"
        +"<h2>2. 跑第一个程序</h2>"
        +"<pre><code>函数 问候(名字)：\n"
        +"    返回 `你好，{名字}！`\n"
        +"\n"
        +"打印(问候(\"世界\"))</code></pre>"
        +"<p>存成 <code>你好.jsh</code>，然后 <code>jishi 你好.jsh</code>。</p>"
        +"<h2>3. 出错了怎么办</h2>"
        +"<p>基石把报错写成「说清原因 + 给出能照着改的提示」，并且带源码与 "
        +"^ 指示：</p>"
        +"<pre><code>jishi 坏的.jsh\n"
        +"错误 E0302：找不到这个名字（找不到名字「甲」）\n"
        +"  ┌─ 坏的.jsh:3:11\n"
        +"  │ 3 │ 打印(甲)\n"
        +"  │   │     ^^</code></pre>"
        +"<h2>4. 调不动的时候用调试器</h2>"
        +"<pre><code>jishi 调试 脚本.jsh --断点 12\n"
        +"调试 &gt; 变量\n"
        +"调试 &gt; 单步\n"
        +"调试 &gt; 栈</code></pre>"
        +"<p>树遍历与字节码（<code>--执行器 vm</code>）两个执行器都能调试，"
        +"断点、单步、变量、调用栈、出错现场一应俱全。</p>"
        +"<h2>5. 想让它变快</h2>"
        +"<p>同一份源码有三个执行器：树遍历（默认，最好调试）、"
        +"字节码 Python VM、以及 C 写的虚拟机。语义由对拍测试保证一致，"
        +"所以换执行器不需要改代码。</p>")
    返回 页面("上手", "上手.html", 正文, "上手 · 由基石生成器产出")

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

函数 主程序():
    令 参数们 = 系统.参数()
    令 根 = "."
    令 输出 = 文件.路径拼接("site", "基石自述")
    如果 长度(参数们) > 0:
        输出 = 参数们[0]
    如果 长度(参数们) > 1:
        根 = 参数们[1]

    打印("基石自述站：正在读仓库数据……")
    令 数据 = 采集(根)
    打印(`  版本 {数据["版本"]} · 测试 {数据["测试数"]} 项 · 探针 {数据["探针"]}`)
    打印(`  里程碑 {长度(数据["里程碑"])} 个 · 模块 {长度(数据["模块"])} 个`
         +` · 示例 {数据["示例数"]} 个`)

    文件.创建目录(输出)
    文件.写文本(文件.路径拼接(输出, "style.css"), 样式)
    写页面(输出, "index.html", 首页(数据))
    写页面(输出, "里程碑.html", 里程碑页(数据))
    写页面(输出, "规模.html", 规模页(数据))
    写页面(输出, "上手.html", 上手页(数据))
    打印(`  已写出 4 个页面 + 1 个样式表 → {输出}`)

函数 写页面(输出目录, 名字, 内容):
    令 全 = 文件.路径拼接(输出目录, 名字)
    文件.写文本(全, 内容)
    打印(`    {名字}（{长度(内容.拆分("\n"))} 行）`)

主程序()
