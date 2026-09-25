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

__all__ = ["转义", "原样", "标签", "空元素", "链接", "属性文本", "有序列表", "无序列表"]


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
