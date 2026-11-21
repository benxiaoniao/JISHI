# -*- coding: utf-8 -*-
"""基石标准库：html。生成 HTML 片段与转义。

**为什么需要**：`examples/projects/基石自述站/生成.jsh` 就是一个「用基石写静态站
生成器」的真实项目，里面**手写转义**（把 `&` `<` `>` 换成实体）——正是本模块要
消掉的绕道。手写转义还容易漏（比如忘了引号），漏了就是 XSS 隐患。

⚠️ **实现上的一个坑（M50 实测，别踩回去）**：`原样(...)` 标记**不能用 `str` 子类**
实现。M47 把文本改成 C 侧原生字符串之后，`c2py` 是**直接读字节**再 `decode` 成
普通 `str` 的，于是子类的身份在 C VM 下**丢失**——`isinstance(x, _Raw)` 在树遍历与
Python VM 里是 `真`、在 C VM 里是 `假`。那会让 C VM 把本该原样输出的片段**又转义
一遍**（输出多出 `&amp;lt;`），而且是**静默的**（不报错、普通用例还过得去）。
所以这里用**普通对象**+内部类型名——身份在三个执行器上都保得住。

⚠️ **API 取舍：属性用「字典」传**——`标签("p", "内容", {"类名": "提示"})`。
理由见 `_属性串`（关键字参数在两个宿主上会**静默丢失**）。
"""

from __future__ import annotations

import html as _py_html

from ..errors import RunTypeError

__all__ = [
    "转义", "原样", "标签", "空元素", "链接", "属性文本", "有序列表", "无序列表",
    # M107：解析方向（从网页里取东西）
    "取文本", "取链接", "取表格",
    # M166：页面构件（表单 / 表格 / 布局 / 卡片）
    "输入框", "多行文本", "下拉", "多选", "按钮", "表单",
    "由表格", "页面", "卡片", "提示",
]

# ---------------------------------------------------------------------------
# M107：解析方向
#
# ⚠️ **为什么不用 Python 的 `html.parser` / `HTMLParser`**：`html` 模块在
# **五个执行器**上都要给出同一个结果（`test_m51` 的漂移检测盯着），而 Rust /
# Node 那两份不可能去复刻 CPython 的容错规则。所以这里自己写一套**小而确定**的
# 扫描规则，三处逐字对齐（Python 这份是判据基准）：
#
# * 标签：`<` 到**下一个** `>`；`<!-- -->` 注释与 `<!doctype>` / `<?…?>` 整段丢掉；
#   `<` 后面找不到 `>` 就把剩下的都当文本。
# * `<script>` / `<style>`：**标签与内容一起丢**（取文本时不该带出 JS/CSS）。
# * 实体：固定一小张表（`&amp; &lt; &gt; &quot; &#39; &apos; &nbsp;`）+
#   十进制 `&#123;` + 十六进制 `&#x1F600;`；认不出来就**原样留着**。
#   ⚠️ 代理区（U+D800–U+DFFF）显式不认（Rust 的 `char` 装不下它，三处会分叉）。
# * 空白：压成一个空格并去两端 —— 用各自语言的「unicode 空白」口径
#   （Python `str.split()` / Rust `char::is_whitespace` / JS `\s`，三个都含 U+00A0）。
#
# 这套规则**不追求容错**：真实世界那些残缺 HTML 它不兜。够用于「规整页面」，
# 也够把「正则硬挖」那类绕道消掉 —— 想更进一步得引 HTML 解析库，那是另一件事。
# ---------------------------------------------------------------------------

#: 固定实体表（三处**必须一致**，别各自加戏）
_实体表 = {
    "amp": "&", "lt": "<", "gt": ">", "quot": '"',
    "apos": "'", "nbsp": " ",
}


def _解实体(文本: str) -> str:
    """把 `&…;` 换成字符；认不出来的原样留着。"""
    if "&" not in 文本:
        return 文本
    出: list = []
    i = 0
    n = len(文本)
    while i < n:
        c = 文本[i]
        if c != "&":
            出.append(c)
            i += 1
            continue
        分号 = 文本.find(";", i + 1)
        # `&#123;` 最长 9 个字符，超长的就不是实体了
        if 分号 < 0 or 分号 - i > 10:
            出.append("&")
            i += 1
            continue
        体 = 文本[i + 1:分号]
        码 = None
        if 体.startswith("#"):
            数字 = 体[1:]
            try:
                if 数字[:1] in ("x", "X"):
                    码 = int(数字[1:], 16)
                else:
                    码 = int(数字, 10)
            except ValueError:
                码 = None
            if 码 is not None and not (0xD800 <= 码 <= 0xDFFF) \
                    and 0 <= 码 <= 0x10FFFF:
                出.append(chr(码))
                i = 分号 + 1
                continue
        elif 体 in _实体表:
            出.append(_实体表[体])
            i = 分号 + 1
            continue
        出.append("&")
        i += 1
    return "".join(出)


#: 空白字符 —— 只认这六个 ASCII 空白。
#: ⚠️ **不要**用各语言的「unicode 空白」（`str.isspace` / `char::is_whitespace` /
#: `\s`）：它们在 U+001C–U+001F、U+0085 这些小地方**对不齐**，三处会分叉。
#: `&nbsp;`（U+00A0）在解实体时就换成了普通空格，所以也压得掉。
_空白 = " \t\n\r\x0b\x0c"

#: 标签名/属性名允许的字符（ASCII，写死）
_名字字符 = ("abcdefghijklmnopqrstuvwxyz"
             "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
             "0123456789-_:.")


def _是空白(c: str) -> bool:
    return c in _空白


def _是名字字符(c: str) -> bool:
    return c in _名字字符


def _压空白(文本: str) -> str:
    """把连续空白压成一个空格并去掉两端（**口径写死**，见上面说明）。"""
    出: list = []
    在上个是空白 = False
    for c in 文本:
        if _是空白(c):
            if not 在上个是空白:
                出.append(" ")
            在上个是空白 = True
        else:
            出.append(c)
            在上个是空白 = False
    return "".join(出).strip(" ")


def _扫(页面) -> list:
    """把 HTML 扫成一串节点：`["文本", "", 原文]` / `["开始", 标签名, 标签体]`
    / `["结束", 标签名, ""]`。

    标签名一律**小写**；`script` / `style` 的标签与内容都不产出。
    """
    s = str(页面)
    n = len(s)
    出: list = []
    i = 0
    while i < n:
        lt = s.find("<", i)
        if lt < 0:
            出.append(["文本", "", s[i:]])
            break
        if lt > i:
            出.append(["文本", "", s[i:lt]])
        # 注释
        if s.startswith("<!--", lt):
            末 = s.find("-->", lt + 4)
            i = n if 末 < 0 else 末 + 3
            continue
        # <!doctype …> / <?…>
        if lt + 1 < n and s[lt + 1] in "!?":
            末 = s.find(">", lt + 1)
            i = n if 末 < 0 else 末 + 1
            continue
        末 = s.find(">", lt + 1)
        if 末 < 0:
            出.append(["文本", "", s[lt:]])
            break
        体 = s[lt + 1:末]
        i = 末 + 1
        结束 = 体.startswith("/")
        if 结束:
            体 = 体[1:]
        # 标签名
        j = 0
        while j < len(体) and _是空白(体[j]):
            j += 1
        起 = j
        while j < len(体) and _是名字字符(体[j]):
            j += 1
        名 = 体[起:j].lower()
        if not 名:
            continue
        if 名 in ("script", "style"):
            if not 结束:
                # 丢掉标签与整段内容，直到对应的结束标签
                针 = "</" + 名
                低 = s.lower()
                末2 = 低.find(针, i)
                if 末2 < 0:
                    i = n
                else:
                    尖 = s.find(">", 末2)
                    i = n if 尖 < 0 else 尖 + 1
            continue
        出.append(["结束" if 结束 else "开始", 名, 体])
    return 出


def _文本节点(节点们) -> str:
    """把一串节点还原成纯文本（只要文本节点，解实体、压空白）。"""
    块: list = []
    for 节 in 节点们:
        if 节[0] == "文本":
            块.append(节[2])
    return _压空白(_解实体("".join(块)))


def 取文本(片段) -> str:
    """从一段 HTML 里取出**纯文本**：去标签、解实体、把连续空白压成一个空格。

    例：`html.取文本("<p>你好 <b>世界</b></p>")` → `"你好 世界"`。
    `<script>` / `<style>` 的**内容也不会跑出来**。
    """
    return _文本节点(_扫(片段))


def _取属性(标签体: str, 名: str) -> "str | None":
    """从标签体里取一个属性的值（属性名不分大小写；值可带引号也可不带）。"""
    低 = 标签体.lower()
    针 = 名.lower()
    i = 0
    n = len(低)
    while True:
        k = 低.find(针, i)
        if k < 0:
            return None
        # 前面必须是「名字边界」，后面必须紧跟 =（否则 `href` 会匹配到 `data-href`）
        前好 = k == 0 or not _是名字字符(低[k - 1])
        后 = k + len(针)
        while 后 < n and _是空白(低[后]):
            后 += 1
        if 前好 and 后 < n and 低[后] == "=":
            后 += 1
            while 后 < n and _是空白(低[后]):
                后 += 1
            if 后 < n and 低[后] in "\"'":
                引 = 低[后]
                末 = 标签体.find(引, 后 + 1)
                if 末 < 0:
                    return _解实体(标签体[后 + 1:])
                return _解实体(标签体[后 + 1:末])
            j = 后
            while j < n and not _是空白(低[j]) and 低[j] != ">":
                j += 1
            return _解实体(标签体[后:j])
        i = k + len(针)


def 取链接(页面) -> list:
    """把页面里所有 `<a href="…">文字</a>` 取成 `[[文字, 地址], …]`。

    文字已去标签/压空白；`<a>` 没有 `href` 的跳过。比手写正则稳
    （属性顺序、单双引号、标签里再套标签都不用管）。
    """
    节点们 = _扫(页面)
    出: list = []
    i = 0
    while i < len(节点们):
        节 = 节点们[i]
        if 节[0] == "开始" and 节[1] == "a":
            地址 = _取属性(节[2], "href")
            if 地址 is not None:
                块: list = []
                j = i + 1
                while j < len(节点们):
                    内 = 节点们[j]
                    if 内[0] == "结束" and 内[1] == "a":
                        break
                    块.append(内)
                    j += 1
                出.append([_文本节点(块), 地址])
                i = j + 1
                continue
        i += 1
    return 出


def 取表格(页面) -> list:
    """把页面里**第一张** `<table>` 取成「行列表的列表」：每行是各单元格文本。

    `<th>` 与 `<td>` 同等对待；`<thead>` / `<tbody>` 只是容器，不产生行。
    单元格文本已去标签/压空白。

    ⚠️ **不支持嵌套表格**（遇到内层 `<table>` 时内层会被并进单元格文本）——
    规整的表格够用；真要啃复杂页面得上 HTML 解析库。
    """
    节点们 = _扫(页面)
    i = 0
    n = len(节点们)
    # 定位第一张表
    while i < n and not (节点们[i][0] == "开始" and 节点们[i][1] == "table"):
        i += 1
    出: list = []
    在一行 = False
    在一格 = False
    格块: list = []
    行: list = []
    while i < n:
        节 = 节点们[i]
        if 节[0] == "结束" and 节[1] == "table":
            break
        if 节[0] == "开始" and 节[1] == "tr":
            在一行 = True
            行 = []
        elif 节[0] == "结束" and 节[1] == "tr":
            if 行:
                出.append(行)
            在一行 = False
        elif 节[0] == "开始" and 节[1] in ("td", "th"):
            在一格 = True
            格块 = []
        elif 节[0] == "结束" and 节[1] in ("td", "th"):
            if 在一格:
                行.append(_文本节点(格块))
            在一格 = False
        elif 在一格 and 节[0] == "文本":
            格块.append(节)
        i += 1
    return 出


def 转义(值) -> str:
    """把文本里的 HTML 特殊字符换成实体（`&` `<` `>` `"` `'` 五个都换）。

    放进网页的**任何用户内容**都该先过这一道，否则内容里的 `<script>` 会被
    浏览器当代码执行。
    """
    return _py_html.escape(str(值), quote=True)


class _Raw:
    """标记「这段内容是已经生成好的 HTML，不要再转义」。

    ⚠️ 必须是普通类，**不能是 `str` 子类**——理由见模块开头那段「实现上的一个坑」。
    """

    __slots__ = ("s",)

    #: 漏给用户时显示得像个字符串（不让 `打印(原样("<b>"))` 打出 `<__main__._Raw …>`）
    _jishi_type_name = "原样片段"

    def __init__(self, s: str):
        self.s = s

    def __str__(self) -> str:
        return self.s


def 原样(值) -> _Raw:
    """标记这段内容是**已经生成好的 HTML**，放进标签里时不再转义。

    只对你自己拼出来的片段用。**不要**对用户输入用这个——那就等于关掉了
    转义保护（`原样` 这个名字就是要让它在代码里显眼）。
    """
    if isinstance(值, _Raw):
        return 值
    return _Raw(str(值))


def _内容(值) -> str:
    """内容位：`原样` 的片段原样输出，其余一律转义。"""
    if isinstance(值, _Raw):
        return 值.s
    return _py_html.escape(str(值), quote=True)


def _属性(值) -> str:
    """属性值：**永远转义**——`原样` 在属性里也救不了引号逃逸，所以不认它。"""
    return _py_html.escape(str(值), quote=True)


#: 中文属性名 → HTML 属性名（`class` / `for` 与 Python 关键字冲突，另给中文化名）
_ATTR_ALIAS = {"类名": "class", "对应": "for"}


def _属性串(属性) -> str:
    """把**属性字典**拼成属性串。值为 `空` 时只输出属性名（HTML 布尔属性）。

    ⚠️ **为什么用字典、不用关键字参数**（M50 第二批实测的取舍）：

    本模块最初写成 `标签("p", "内容", 类名="提示")`，在 Python 侧工作得很好
    ——`_Builtin.__call__(*args, **kwargs)` 会把关键字透传下去。但**两个宿主
    收不到关键字**：Node 侧是 `fn(...args)`、Rust 侧的类型是
    `fn(&[Val], &mut VM)`——签名里**没有放关键字的位置**。于是同一份调用在
    Node / Rust 上会**静默丢掉全部属性**，产出的 HTML 与 Python 侧不一样，
    而且**不报错**。

    要根治得改两个宿主的「内建函数」类型（Node 加 kwargs 参数、Rust 换成
    带 kwargs 的签名），那是**跨宿主架构改动**，不该塞进标准库这一批。
    所以这里降级成字典参数：`标签("p", "内容", {"类名": "提示"})`。

    📌 **顺带记一笔同类既有缺口**：`加密.摘要("abc", 算法="md5")` 在
    Node / Rust 上会静默用默认的 sha256（实测两个宿主都返回 sha256 摘要）。
    同一个根因，等做「宿主 kwargs 支持」时一起修。
    """
    if 属性 is None:
        return ""
    if not isinstance(属性, dict):
        raise RunTypeError('属性要传字典，例如 标签("p", "内容", {"类名": "提示"})')
    out = []
    for k, v in 属性.items():
        key = _ATTR_ALIAS.get(str(k), str(k)).replace("_", "-")
        if v is None:
            out.append(f" {key}")
        else:
            out.append(f' {key}="{_属性(v)}"')
    return "".join(out)


def 标签(名: str, 内容="", 属性=None) -> str:
    """生成一个标签：`标签("p", "你好", {"类名": "提示"})` → `<p class="提示">你好</p>`。

    - `内容` 会被**转义**；要放自己拼的片段就套 `原样(...)`。
    - 第三个参数是**属性字典**（不是关键字参数——理由见 `_属性串`）；
      `类名` / `对应` 自动翻译成 `class` / `for`，名字里的下划线变连字符
      （`数据_值` → `data-值`）。
    - 属性值为 `空` 时只输出属性名（HTML 布尔属性，如 `disabled`）。
    """
    if not isinstance(名, str) or not 名:
        raise RunTypeError("「标签」的第一个参数要是标签名（非空文本）")
    return f"<{名}{_属性串(属性)}>{_内容(内容)}</{名}>"


def 空元素(名: str, 属性=None) -> str:
    """生成自闭合标签：`空元素("img", {"src": "a.png"})` → `<img src="a.png" />`。"""
    if not isinstance(名, str) or not 名:
        raise RunTypeError("「空元素」的第一个参数要是标签名（非空文本）")
    return f"<{名}{_属性串(属性)} />"


def 链接(文字, 地址: str, 属性=None) -> str:
    """生成 `<a href="…">文字</a>`。地址与文字都会被转义。"""
    attrs = {"href": 地址}
    if 属性:
        attrs = {**attrs, **属性}
    return 标签("a", 文字, attrs)


def 属性文本(值) -> str:
    """把文本转义成可以安全放进**属性值**的形式（引号也转）。

    需要自己拼标签（而不是用 `标签()`）时用它。
    """
    return _属性(值)


def 有序列表(项目, 属性=None) -> str:
    """生成 `<ol><li>…</li>…</ol>`；每项内容都会被转义。"""
    return 标签("ol", 原样("".join(标签("li", x) for x in 项目)), 属性)


def 无序列表(项目, 属性=None) -> str:
    """生成 `<ul><li>…</li>…</ul>`；每项内容都会被转义。"""
    return 标签("ul", 原样("".join(标签("li", x) for x in 项目)), 属性)


# ---------------------------------------------------------------------------
# M166：页面构件
#
# 📌 **输出格式是契约**：三个宿主逐字节相同。下面每个函数都写清了输出长什么样，
#    改一个字都要同时改 Rust / Node 并重录语料期望。
# ---------------------------------------------------------------------------


def _内容们(值) -> str:
    """内容位，但**列表会被拼起来**（`表单([字段们])` 这种写法）。

    列表里的每个元素照 `_内容` 的规矩走：`原样` 片段原样输出，其余转义。
    列表里**允许嵌套列表**（拼表单时常见），递归展开。
    """
    if isinstance(值, (list, tuple)):
        return "".join(_内容们(x) for x in 值)
    return _内容(值)


def _文本(名: str, 值) -> str:
    """要一个非空文本参数（名字用作报错文案）。"""
    if not isinstance(值, str) or not 值:
        raise RunTypeError(f"「{名}」要传非空文本")
    return 值


def _标签包(说明, 里面: str) -> str:
    """给控件套一层 `<label>`：有标签就带 `<span>`，没有就裸着。

    `<label>` 包住控件之后，点标签文字也能聚焦到控件上 —— 不用写 `for`/`id`，
    省掉一整类「id 拼错了点不动」的问题。
    """
    if not 说明:
        return 里面
    return f"<label><span>{_内容(说明)}</span>{里面}</label>"


def 输入框(名字: str, 说明="", 类型="text", 值="") -> str:
    """单行输入。`输入框("金额", "金额", "number")` →
    `<label><span>金额</span><input type="number" name="金额" /></label>`

    有 `值` 才输出 `value`（空值不输出，免得脏 URL）。

    📌 故意**没有「额外属性」参数**：属性顺序在三个宿主上要对齐，多一个字典参数
    就多一类「顺序不一致」的坑（而且用 `标签()` 自己拼这两行也不麻烦）。
    """
    _文本("输入框", 名字)
    属性们 = {"type": 类型 or "text", "name": 名字}
    if 值 != "" and 值 is not None:
        属性们["value"] = 值
    return 原样(_标签包(说明, 空元素("input", 属性们)))


def 多行文本(名字: str, 说明="", 值="", 行数=4) -> str:
    """多行输入。`多行文本("备注", "备注", "", 6)` →
    `<label><span>备注</span><textarea name="备注" rows="6"></textarea></label>`
    """
    _文本("多行文本", 名字)
    属性们 = {"name": 名字, "rows": 行数}
    return 原样(_标签包(说明, 标签("textarea", "" if 值 is None else 值, 属性们)))


def 下拉(名字: str, 选项, 说明="", 选中="") -> str:
    """下拉选择。`下拉("类别", ["餐饮", "交通"], "类别", "交通")` →
    `<label><span>类别</span><select name="类别">` +
    `<option value="餐饮">餐饮</option><option value="交通" selected>交通</option>` +
    `</select></label>`

    `选项` 是文本列表（值 = 显示）。`选中` 那一项带 `selected`。
    """
    _文本("下拉", 名字)
    if not isinstance(选项, (list, tuple)):
        raise RunTypeError('「下拉」的选项要传列表，例如 下拉("类别", ["餐饮", "交通"])')
    片 = []
    for 项 in 选项:
        值 = 项 if isinstance(项, str) else str(项)
        标记 = " selected" if 值 == 选中 and 选中 != "" else ""
        片.append(f'<option value="{_属性(值)}"{标记}>{_内容(值)}</option>')
    return 原样(_标签包(说明,
                       标签("select", 原样("".join(片)), {"name": 名字})))


def 多选(名字: str, 选项, 说明="", 选中们=None) -> str:
    """多选框组（不是 `<select multiple>`——手机上勾比按 Ctrl 点友好）。

    `多选("标签", ["工作", "生活"], "分类", ["生活"])` →
    `<fieldset class="字段组"><legend>分类</legend>` +
    `<label><input type="checkbox" name="标签" value="工作" />工作</label>` +
    `<label><input type="checkbox" name="标签" value="生活" checked />生活</label>` +
    `</fieldset>`

    ⚠️ 这些 `<label>` **不带 `<span>`**（勾选框与文字并排更好点）。
    """
    _文本("多选", 名字)
    if not isinstance(选项, (list, tuple)):
        raise RunTypeError('「多选」的选项要传列表，例如 多选("标签", ["工作", "生活"])')
    已选 = set(选中们 or [])
    片 = []
    for 项 in 选项:
        值 = 项 if isinstance(项, str) else str(项)
        属性们 = {"type": "checkbox", "name": 名字, "value": 值}
        if 值 in 已选:
            属性们["checked"] = None
        片.append(f"<label>{空元素('input', 属性们)}{_内容(值)}</label>")
    题 = f"<legend>{_内容(说明)}</legend>" if 说明 else ""
    return 原样(f'<fieldset class="字段组">{题}{"".join(片)}</fieldset>')


def 按钮(文字: str, 类型="submit") -> str:
    """按钮。`按钮("保存")` → `<button type="submit">保存</button>`

    `类型` 可以是 `"submit"` / `"button"` / `"reset"`（原样写进 `type`）。
    """
    _文本("按钮", 文字)
    return 原样(标签("button", 文字, {"type": 类型 or "submit"}))


def 表单(内容, 提交到: str, 方法="post") -> str:
    """表单。`表单([输入框(…), 按钮("保存")], "/新建")` →
    `<form action="/新建" method="post">…</form>`

    `内容` 可以是列表（会拼起来）或单个片段。
    """
    _文本("表单", 提交到)
    return 原样(标签("form", 原样(_内容们(内容)),
                    {"action": 提交到, "method": 方法 or "post"}))


def 由表格(表, 斑马纹=False) -> str:
    """**直接从 `表格` 值渲染** `<table>`（不用手拼 `<tr>`）。

    `由表格(表)` →
    `<table class="基石表格"><thead><tr><th>列1</th></tr></thead>` +
    `<tbody><tr><td>值</td></tr></tbody></table>`

    `斑马纹` 为真时追加 `斑马纹` 类（隔行浅底，CSS 已在 `页面` 里备好）。
    单元格全部转义。
    """
    # ⚠️ 用**鸭子类型**而不是 `from . import 表格`：本包是按文件名动态加载的
    # （见 stdlib/__init__.py），模块之间互相 import 会拿到半初始化的对象。
    if not (hasattr(表, "headers") and hasattr(表, "rows")):
        raise RunTypeError(
            "「由表格」要传表格值（用 表格.读表格 / 表格.新建 造出来），"
            f"不要传{type(表).__name__}")
    类 = "基石表格 斑马纹" if 斑马纹 else "基石表格"
    头 = "".join(f"<th>{_内容(h)}</th>" for h in 表.headers)
    体 = "".join(
        "<tr>" + "".join(f"<td>{_内容(c)}</td>" for c in 行) + "</tr>"
        for 行 in 表.rows
    )
    return 原样(f'<table class="{类}"><thead><tr>{头}</tr></thead>'
               f"<tbody>{体}</tbody></table>")


#: 页面骨架用的内置样式。**不引 CDN、不引框架** —— 一个 `<style>` 就够，
#: 而且把它做成常量（不是每个页面一份）才能让五引擎逐字对得上。
_样式 = (
    ":root{--底:#fff;--字:#1f2328;--弱:#6b7280;--线:#e5e7eb;--主:#2563eb;"
    "--成功:#16a34a;--警告:#d97706;--错误:#dc2626}"
    "*{box-sizing:border-box}"
    'body{margin:0;font:16px/1.6 system-ui,-apple-system,"Segoe UI",'
    '"Noto Sans SC",sans-serif;color:var(--字);background:var(--底)}'
    "header{padding:1rem 1.5rem;border-bottom:1px solid var(--线)}"
    "header h1{margin:0;font-size:1.25rem}"
    "nav{margin-top:.5rem}"
    "nav a{margin-right:1rem;color:var(--主);text-decoration:none}"
    "main{max-width:56rem;margin:0 auto;padding:1.5rem}"
    "footer{padding:1rem 1.5rem;color:var(--弱);border-top:1px solid var(--线);"
    "font-size:.875rem}"
    ".卡片{border:1px solid var(--线);border-radius:.5rem;padding:1rem;margin:1rem 0}"
    ".卡片 h2{margin:0 0 .5rem;font-size:1.1rem}"
    ".提示{padding:.75rem 1rem;border-radius:.375rem;margin:.75rem 0;"
    "border-left:4px solid;background:#f9fafb}"
    ".提示-成功{border-color:var(--成功);background:#f0fdf4}"
    ".提示-警告{border-color:var(--警告);background:#fffbeb}"
    ".提示-错误{border-color:var(--错误);background:#fef2f2}"
    ".基石表格{width:100%;border-collapse:collapse}"
    ".基石表格 th,.基石表格 td{padding:.5rem .75rem;"
    "border-bottom:1px solid var(--线);text-align:left}"
    ".基石表格 thead th{background:#f9fafb}"
    ".斑马纹 tbody tr:nth-child(even){background:#fafafa}"
    ".字段组{border:1px solid var(--线);border-radius:.375rem;margin:.5rem 0;"
    "padding:.5rem .75rem}"
    "label{margin:.5rem 0}"
    "label span{display:block;font-size:.875rem;color:var(--弱)}"
    "input,textarea,select{width:100%;padding:.5rem;border:1px solid var(--线);"
    "border-radius:.375rem;font:inherit}"
    'input[type="checkbox"]{width:auto;margin-right:.5rem}'
    "button{padding:.5rem 1rem;border:0;border-radius:.375rem;background:var(--主);"
    "color:#fff;font:inherit;cursor:pointer}"
)


def 页面(标题: str, 内容, 导航="") -> str:
    """整页骨架（`<!DOCTYPE html>` 到 `</html>`）。

    `页面("记账", [卡片(…), 表单(…)], [[["首页", "/"]], [["关于", "/关于"]]])`

    - 五行 `header` / `nav` / `main` / `footer` 一处定义，各页面只填内容；
    - `导航` 传 `[[文字, 地址], …]` 会自动生成链接，也可以直接传一段
      `原样(...)` 的 HTML（想加图标之类）；
    - 样式是**内置常量**（见 `_样式`），不引 CDN —— 离线也能看。
    """
    _文本("页面", 标题)
    # 导航：列表 → 自动拼 `<a>`；文本 → 原样（给「我自己拼好了」的场景）
    if isinstance(导航, (list, tuple)):
        链接们 = "".join(链接(项[0], 项[1]) for 项 in 导航)
        导航片 = f"<nav>{链接们}</nav>"
    elif 导航:
        导航片 = f"<nav>{_内容(导航)}</nav>"
    else:
        导航片 = ""
    题 = _内容(标题)
    return 原样(
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        f"<title>{题}</title>\n"
        f"<style>{_样式}</style>\n"
        "</head>\n"
        "<body>\n"
        f"<header><h1>{题}</h1>{导航片}</header>\n"
        f"<main>{_内容们(内容)}</main>\n"
        "<footer>由基石生成</footer>\n"
        "</body>\n"
        "</html>"
    )


def 卡片(标题: str, 内容) -> str:
    """卡片：`卡片("本月支出", 由表格(表))` →
    `<section class="卡片"><h2>本月支出</h2><table …></table></section>`
    """
    _文本("卡片", 标题)
    return 原样(f'<section class="卡片"><h2>{_内容(标题)}</h2>{_内容们(内容)}</section>')


#: `提示` 认的级别 → CSS 类名后缀（**只认这三个**：多一个都要在样式里加一条）
_提示级别 = {"成功": "成功", "警告": "警告", "错误": "错误"}


def 提示(级别: str, 文本) -> str:
    """提示条：`提示("错误", "金额要填正数")` →
    `<div class="提示 提示-错误">金额要填正数</div>`

    级别只认 `成功` / `警告` / `错误`（对应样式里的三种颜色）。
    写别的会**报错**而不是静默给个没颜色的条 —— 这类「拼错也看不出来」的
    问题最难查。
    """
    if 级别 not in _提示级别:
        可选 = "、".join(_提示级别)
        raise RunTypeError(f"「提示」的级别只认{可选}，收到「{级别}」")
    return 原样(f'<div class="提示 提示-{_提示级别[级别]}">{_内容们(文本)}</div>')
