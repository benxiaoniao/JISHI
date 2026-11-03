# -*- coding: utf-8 -*-
"""M166 · 页面构件：`html` 能直接拼出**真页面**（表单 / 表格 / 布局 / 卡片）。

## 这一站补的是构件，不是语法

拼一个带表单的页面原来要手写几十行字符串。新增 10 个函数：

| 类别 | 函数 |
|---|---|
| 表单 | `输入框` / `多行文本` / `下拉` / `多选` / `按钮` / `表单` |
| 表格 | `由表格`（**直接从 `表格` 值渲染**，不用手拼 `<tr>`） |
| 布局 | `页面`（骨架 + 内置样式，一处定义） |
| 卡片 / 提示 | `卡片` / `提示`（成功 / 警告 / 错误 三色） |

**不引前端框架、不引 CDN**：服务端渲染 + 原生 `<form>`。理由有二 ——
基石的定位不是打包器；而且**纯字符串才能五引擎逐字一致**，直接进对拍。

## 三个宿主各写一份，靠什么保证一致

Python / Rust / Node **各有一份实现**（C VM 走 Python 回调，自动一致）。
一致靠两道：

1. **语料** `tests/cases/45_html页面构件.jsh` —— 15 个构件的输出逐字比；
2. **这一份的 `test_整页输出五引擎逐字一致`** —— 整页（**含 1600 多字符的内置样式**）。

⚠️ **为什么要立第二条**：语料里 `页面()` 那一行用的是 `html.取文本(页)`
（样式太长，进 `.out` 不值得），于是 **CSS 不一致它抓不到**。而 CSS 是三个宿主
**各写一份**的 —— 开发时就踩过：手抄到 Node 那份时 `"Segoe UI"` 的引号漏了一个，
产出的页面**照样能看**，只有逐字比才看得出来。
"""

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


def _五份(源码: str, tmp_path: Path) -> dict:
    from oracle.jishi import serialize
    from oracle.jishi.compiler import compile_source

    (tmp_path / "程序.jsh").write_text(源码, encoding="utf-8", newline="\n")
    (tmp_path / "驱动.py").write_text(
        CVM驱动.format(root=str(ROOT).replace("\\", "\\\\")), encoding="utf-8")
    表 = {
        "树遍历": [sys.executable, "-m", "oracle.jishi.cli", "--tree", "程序.jsh"],
        "Python VM": [sys.executable, "-m", "oracle.jishi.cli", "程序.jsh"],
        "C VM": [sys.executable, "驱动.py"],
    }
    rs = rust_exe_path()
    if rs.is_file():
        表["Rust 宿主"] = [str(rs), "程序.jsh"]
    node = shutil.which("node")
    if node:
        (tmp_path / "程序.json").write_text(
            serialize.dumps(compile_source(源码, "程序.jsh")), encoding="utf-8")
        表["Node 宿主"] = [node, str(ROOT / "oracle" / "node" / "index.js"),
                           "程序.json"]

    出 = {}
    for 名字, 命令 in 表.items():
        r = subprocess.run(命令, cwd=str(tmp_path), capture_output=True,
                           timeout=180, input=b"")
        出[名字] = (r.stdout + r.stderr).decode("utf-8", "replace")
    return 出


def _归一(文: str) -> str:
    return 文.replace("\r\n", "\n").strip()


# ---------------------------------------------------------------------------
# 一、整页（含内置样式）五引擎逐字一致
# ---------------------------------------------------------------------------

def test_整页输出五引擎逐字一致(tmp_path):
    """`页面()` 的**完整输出**（连 1600 多字符的内置 `<style>` 一起）五引擎一致。

    进语料的 `45_html页面构件.jsh` 用的是 `html.取文本(页)` —— 把样式滤掉了。
    所以**这一条才是 CSS 的唯一看门人**。
    """
    源 = ('导入 html\n'
          '打印(html.页面("记账", [html.卡片("本月", "内容")], [["首页", "/"]]))\n')
    出 = _五份(源, tmp_path)
    assert len(出) >= 3, f"能跑的执行器太少：{list(出)}"
    标准 = _归一(出["Python VM"])
    assert 标准.startswith("<!DOCTYPE html>"), 标准[:120]
    assert "<style>" in 标准 and "由基石生成" in 标准, "整页骨架不完整"
    不一样 = {名: 文 for 名, 文 in 出.items() if _归一(文) != 标准}
    assert 不一样 == {}, (
        "整页输出不一致（多半是某个宿主的 CSS 抄错了）：\n"
        + "\n".join(f"--- {名} ---\n{_归一(文)[:400]}\n（共 {len(_归一(文))} 字）"
                    for 名, 文 in 不一样.items())
        + f"\n标准长度 {len(标准)} 字")


# ---------------------------------------------------------------------------
# 二、构件是「原样片段」，不会被二次转义
# ---------------------------------------------------------------------------

def test_构件放进容器不会被二次转义(tmp_path):
    """`表单([输入框(...), 按钮(...)], "/新建")` 里，构件必须**原样**拼进去。

    ⚠️ 这条踩过：构件第一版返回普通文本，于是 `表单` 把里面的 `<input …>`
    当成用户内容**又转义了一遍** —— 页面显示出来是一串 `&lt;input …&gt;`。
    修法是让构件返回「原样片段」（`html.原样` 那个标记）。
    """
    源 = ('导入 html\n'
          '令 f = html.表单([html.输入框("甲"), html.按钮("存")], "/新建")\n'
          '打印(f)\n')
    出 = _五份(源, tmp_path)
    # ⚠️ `输入框("甲")` 只给名字、没给说明 ⇒ **不套 `<label>`**（裸控件）。
    #    第一版期望写成了带 `<label><span>甲</span>` 的样子，是把「说明」当成
    #    「没给就默认用名字」了 —— 实际语义是「没给就是没有标签文字」。
    期望 = ('<form action="/新建" method="post">'
            '<input type="text" name="甲" />'
            '<button type="submit">存</button></form>')
    错 = {名: _归一(文) for 名, 文 in 出.items() if _归一(文) != 期望}
    assert 错 == {}, (
        "构件被二次转义了（输出里出现 `&lt;input` 就是这个问题）：\n"
        + "\n".join(f"--- {名} ---\n{文}" for 名, 文 in 错.items()))


def test_用户输入一律转义(tmp_path):
    """用户填的内容里带 `<script>`，输出里只能是**字面文本**。

    这是这一批构件唯一的「安全」判据：默认转义，想插自己拼的片段才用 `原样`。
    """
    源 = ('导入 html\n'
          '打印(html.提示("错误", "<script>alert(1)</script>"))\n'
          '打印(html.输入框("<坏>", "<标签>"))\n')
    出 = _五份(源, tmp_path)
    期望 = ('<div class="提示 提示-错误">&lt;script&gt;alert(1)&lt;/script&gt;</div>\n'
            '<label><span>&lt;标签&gt;</span><input type="text" name="&lt;坏&gt;" /></label>')
    错 = {名: _归一(文) for 名, 文 in 出.items() if _归一(文) != 期望}
    assert 错 == {}, f"转义漏了：{错}"


# ---------------------------------------------------------------------------
# 三、源码级护栏
# ---------------------------------------------------------------------------

def test_三个宿主都有这10个构件():
    """三份实现都得在 —— 少一个，`test_m51_consistency` 会红，
    但这条能直接指出「缺的是哪个宿主」。
    """
    名单 = ["输入框", "多行文本", "下拉", "多选", "按钮", "表单",
            "由表格", "页面", "卡片", "提示"]
    py = (ROOT / "oracle" / "jishi" / "stdlib" / "html.py").read_text(encoding="utf-8")
    rs = (ROOT / "rust" / "src" / "stdlib" / "htmlmod.rs").read_text(encoding="utf-8")
    js = (ROOT / "oracle" / "node" / "vm.js").read_text(encoding="utf-8")
    for 名 in 名单:
        assert f"def {名}(" in py, f"Python 侧缺 `{名}`"
        assert f'"{名}" =>' in rs, f"Rust 侧缺 `{名}`"
        assert f"'{名}': new R.Builtin('{名}'" in js, f"Node 侧缺 `{名}`"


def test_文件头说的_不引CDN():
    """样式必须是**内置常量**，不能出现远程地址。

    这条不是洁癖：`<link href="https://…">` 会让页面在断网时裸奔，
    而且「页面长什么样」就取决于别人家的服务器了。
    """
    py = (ROOT / "oracle" / "jishi" / "stdlib" / "html.py").read_text(encoding="utf-8")
    i = py.index("_样式 = (")
    段 = py[i:i + 2500]
    for 坏 in ("http://", "https://", "cdn", "fonts.googleapis"):
        assert 坏 not in 段, f"内置样式里出现了 `{坏}` —— 页面不该依赖外部资源"


# ---------------------------------------------------------------------------
# 四、教程能跑，产物是一份完整的页面
# ---------------------------------------------------------------------------

def test_教程14能跑并生成完整页面(tmp_path):
    """`examples/tutorial/14_网页.jsh` 跑完要留下一份**能打开的** HTML。

    判据只查结构（`<!DOCTYPE` / 表单 / 表格 / 内置样式 / 页脚），
    不查逐字 —— 教程是给人读的，改一句话不该让判据红。
    """
    import shutil as _sh   # 教程会在 cwd 里写 `记账.html`，必须给它私有目录
    源 = ROOT / "examples" / "tutorial" / "14_网页.jsh"
    assert 源.is_file(), "教程文件不见了"
    _sh.copy(源, tmp_path / "教程.jsh")
    r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", "教程.jsh"],
                       cwd=str(tmp_path), capture_output=True, timeout=120, input=b"")
    assert r.returncode == 0, (r.stdout + r.stderr).decode("utf-8", "replace")[-800:]

    页 = (tmp_path / "记账.html").read_text(encoding="utf-8")
    for 要素 in ("<!DOCTYPE html>", "<title>我的账本</title>", "<style>",
                 '<form action="/新建" method="post">', '<table class="基石表格 斑马纹">',
                 "<nav>", "<footer>由基石生成</footer>"):
        assert 要素 in 页, f"产出的页面里没有 {要素!r}"
