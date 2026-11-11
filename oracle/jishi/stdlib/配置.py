# -*- coding: utf-8 -*-
"""基石标准库：配置。读写配置文件（JSON / 键值文本）。

**为什么需要**：工具类项目都要配置文件，而之前只能自己读 JSON 再手写校验——
「键不在就崩」「类型不对不吭声」是常见坑。本模块把这两件事一次办掉：

- `读JSON(路径, 默认)`：文件不在 / 内容坏了，给**默认值**或**中文报错**，
  而不是抛一个看不懂的英文异常；
- `取(配置, "键.子键", 默认)`：按点号取嵌套值，缺了给默认值。

⚠️ **关于 TOML**：路线图原本把 toml 列进来，但**基石当前没有 toml 解析器**，
Python 3.11 之前的标准库也没有（`tomllib` 是 3.11+）。为不引入版本依赖，
本模块**只做 JSON 与键值文本**这两种——它们覆盖了绝大多数工具配置。
要 TOML 请单独提需求（那是一个独立的解析器工程）。
"""

from __future__ import annotations

import json as _py_json
import os as _py_os

from ..errors import RunFileError, RunTypeError, RunValueError

__all__ = ["读JSON", "写JSON", "读键值", "写键值", "取", "设", "合并", "展开"]


#: 「没传」的哨兵：**普通文本常量**（能安全过基石↔Python 边界）。
#:
#: ⚠️ **M50 踩到的一个真 bug，别再犯**：原先用 `默认=...`（Python 的
#: `Ellipsis`）当「没传」的哨兵，结果 `配置.取` **永远返回 `空`**——因为函数
#: 是**从基石侧被调用**的，`...` 在跨边界那一步被转成 `空`，于是
#: `默认 is not ...` 恒为真、「缺键就返回默认值」每次都命中。
#: **教训：标准库函数的默认值必须是能过边界的值**（数字 / 文本 / `空` / 容器），
#: 不能用 Python 特有的哨兵对象。正常配置里不会有人用这个值。
_未给 = "\x00未给\x00"


def _path(路径) -> str:
    return str(路径)


def 读JSON(路径, 默认=_未给):
    """读一个 JSON 配置文件。

    - 文件不存在：给了 `默认` 就返回它；没给就报错（提示文件路径）。
    - 内容不是合法 JSON：**总是报错**（不静默返回默认值——那会让「配置写错了」
      变成「配置没生效」，更难查）。
    """
    p = _path(路径)
    if not _py_os.path.exists(p):
        if 默认 is not _未给 and 默认 != _未给:
            return 默认
        raise RunFileError(f"配置文件「{p}」不存在")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return _py_json.load(f)
    except _py_json.JSONDecodeError as e:
        raise RunFileError(
            f"配置文件「{p}」不是合法的 JSON：第 {e.lineno} 行第 {e.colno} 列：{e.msg}")
    except OSError as e:
        raise RunFileError(f"读配置文件「{p}」失败：{e}")


def 写JSON(路径, 数据, 缩进: int = 2) -> str:
    """把数据写成 JSON 文件（**中文原样保留**，不转成 \\uXXXX）。返回文件路径。"""
    p = _path(路径)
    if not isinstance(缩进, int) or isinstance(缩进, bool) or 缩进 < 0:
        raise RunTypeError("「缩进」要传 0 或正整数")
    try:
        text = _py_json.dumps(数据, ensure_ascii=False,
                              indent=(缩进 or None))
    except (TypeError, ValueError) as e:
        raise RunValueError(f"这份数据没法写成 JSON：{e}")
    d = _py_os.path.dirname(p)
    if d:
        _py_os.makedirs(d, exist_ok=True)
    try:
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text + "\n")
    except OSError as e:
        raise RunFileError(f"写配置文件「{p}」失败：{e}")
    return p


def 读键值(路径, 默认=_未给, 分隔符: str = "=") -> dict:
    """读「键 = 值」形式的简单配置文本，返回字典。

    规则（刻意做得简单、可预期）：
    - 空行与 `#` 开头的行**跳过**；
    - 行内 `#` 之后的内容当注释去掉（想写 `#` 本身就用引号包起来）；
    - 键值两边的空白去掉；值不加引号时按文本处理（不自动转数字）。
    """
    p = _path(路径)
    if not _py_os.path.exists(p):
        if 默认 is not _未给 and 默认 != _未给:
            return 默认
        raise RunFileError(f"配置文件「{p}」不存在")
    if not isinstance(分隔符, str) or not 分隔符:
        raise RunTypeError("「分隔符」要传非空文本")
    out: dict = {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, 1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if 分隔符 not in line:
                    raise RunFileError(
                        f"配置文件「{p}」第 {lineno} 行没有「{分隔符}」：{line}")
                k, _, v = line.partition(分隔符)
                k, v = k.strip(), v.strip()
                v = _去注释(v)
                if not k:
                    raise RunFileError(f"配置文件「{p}」第 {lineno} 行的键是空的")
                out[k] = v
    except OSError as e:
        raise RunFileError(f"读配置文件「{p}」失败：{e}")
    return out


def _去注释(值: str) -> str:
    """去掉行内注释；用引号包起来的部分不当注释。"""
    if 值[:1] in ('"', "'") and 值[-1:] == 值[:1] and len(值) >= 2:
        return 值[1:-1]                       # 整段被引号包着 → 原样（含 #）
    q = None
    for i, ch in enumerate(值):
        if q:
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
        elif ch == "#":
            return 值[:i].strip()
    return 值.strip()


def 写键值(路径, 配置: dict, 分隔符: str = " = ") -> str:
    """把字典写成「键 = 值」形式的配置文本。返回文件路径。

    值里含 `#` 或首尾有空白时**自动加引号**，这样 `读键值` 能原样读回来。
    """
    p = _path(路径)
    if not isinstance(配置, dict):
        raise RunTypeError("「写键值」的第二个参数要传字典")
    lines = []
    for k, v in 配置.items():
        s = str(v)
        if "#" in s or s != s.strip() or (s[:1] in "\"'" ):
            s = '"' + s.replace('"', '\\"') + '"'
        lines.append(f"{k}{分隔符}{s}")
    d = _py_os.path.dirname(p)
    if d:
        _py_os.makedirs(d, exist_ok=True)
    try:
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
    except OSError as e:
        raise RunFileError(f"写配置文件「{p}」失败：{e}")
    return p


def 取(配置, 路径: str, 默认=_未给):
    """按点号路径取嵌套值：`取(配置, "数据库.端口", 5432)`。

    路径里用 `数字` 也能取列表元素（`取(配置, "服务器.0.地址")`）。
    缺任何一段：给了 `默认` 就返回它，没给就报错并**指出缺在哪一段**。
    """
    if not isinstance(路径, str) or not 路径:
        raise RunTypeError("「取」的路径要传非空文本")
    cur = 配置
    walked: list = []
    for seg in 路径.split("."):
        walked.append(seg)
        if isinstance(cur, dict):
            if seg in cur:
                cur = cur[seg]
                continue
        elif isinstance(cur, (list, tuple)):
            if seg.isdigit() and int(seg) < len(cur):
                cur = cur[int(seg)]
                continue
        if 默认 is not _未给 and 默认 != _未给:
            return 默认
        raise RunValueError(
            f"配置里没有「{'.'.join(walked)}」这一段"
            f"（「{'.'.join(walked[:-1]) or '<根>'}」下面是"
            f"{_形状(cur)}）")
    return cur          # ⚠️ 成功路径必须返回（M50 漏过一次，`取` 恒返回 空）


def _形状(v) -> str:
    if isinstance(v, dict):
        keys = list(v.keys())
        shown = "、".join(str(k) for k in keys[:8])
        more = f"…等 {len(keys)} 个" if len(keys) > 8 else ""
        return f"这些键：{shown}{more}" if keys else "空字典"
    if isinstance(v, (list, tuple)):
        return f"一个 {len(v)} 项的列表"
    return f"{type(v).__name__} 类型"


def 设(配置: dict, 路径: str, 值) -> dict:
    """按点号路径设置嵌套值，返回**新的**字典（不改原字典）。

    中间缺的层会自动建出来（`设(空字典, "甲.乙", 1)` 得到 `{"甲": {"乙": 1}}`）。
    """
    if not isinstance(配置, dict):
        raise RunTypeError("「设」的第一个参数要传字典")
    if not isinstance(路径, str) or not 路径:
        raise RunTypeError("「设」的路径要传非空文本")
    segs = 路径.split(".")
    out = dict(配置)
    cur = out
    for seg in segs[:-1]:
        nxt = cur.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
        else:
            nxt = dict(nxt)
        cur[seg] = nxt
        cur = nxt
    cur[segs[-1]] = 值
    return out


def 合并(基础: dict, 覆盖: dict) -> dict:
    """深度合并两个字典（`覆盖` 里的值优先），返回新字典。

    嵌套字典会**逐层合并**而不是整体替换——配置文件「只写要改的那几项」靠它。
    """
    if not isinstance(基础, dict) or not isinstance(覆盖, dict):
        raise RunTypeError("「合并」的两个参数都要传字典")
    out = dict(基础)
    for k, v in 覆盖.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = 合并(out[k], v)
        else:
            out[k] = v
    return out


def 展开(配置: dict, 前缀: str = "") -> dict:
    """把嵌套字典**拍平**成「点号路径 → 值」的一层字典（`取` 的逆操作）。

    打印配置、或写进日志时好用。空字典会被保留（值是 `{}`）。
    """
    if not isinstance(配置, dict):
        raise RunTypeError("「展开」的第一个参数要传字典")
    out: dict = {}
    for k, v in 配置.items():
        key = f"{前缀}{k}"
        if isinstance(v, dict):
            if v:
                out.update(展开(v, key + "."))
            else:
                out[key] = {}
        else:
            out[key] = v
    return out
