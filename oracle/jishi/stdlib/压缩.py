# -*- coding: utf-8 -*-
"""基石标准库：压缩。薄封装 Python 的 zipfile / gzip，中文 API。

- **zip**（`打包` / `解压` / `列出内容`）：一箱多件，能装目录树。
- **gzip**（`压缩文件` / `解压文件`）：**单个文件**一个流，解压时不用先读目录表
  —— 日志轮转、备份单份产物常用它。

零第三方依赖。
"""

from __future__ import annotations

import gzip as _py_gzip
import os as _os
import zipfile as _py_zip

from ..errors import RunError, RunValueError
from ..runtime import jishi_repr, JishiBytes

__all__ = [
    "打包", "打包确定", "解压", "列出内容", "压缩文件", "解压文件", "解压成员",
]


def 打包(zip路径, *文件或目录) -> str:
    """把若干文件或目录打包进一个 zip。返回 zip 路径。

    目录会被递归收录；目录条目以「目录名/」作为前缀。
    """
    import os
    if not 文件或目录:
        raise RunValueError("打包至少需要提供一个文件或目录")
    # 允许第一参数是列表（基石调用常传列表）
    items = 文件或目录
    if len(items) == 1 and isinstance(items[0], (list, tuple)):
        items = tuple(items[0])
    try:
        with _py_zip.ZipFile(str(zip路径), "w", _py_zip.ZIP_DEFLATED) as zf:
            for item in items:
                p = str(item)
                if os.path.isdir(p):
                    for root, _, files in os.walk(p):
                        for fn in files:
                            full = os.path.join(root, fn)
                            arc = os.path.relpath(full, os.path.dirname(p))
                            zf.write(full, arc)
                elif os.path.isfile(p):
                    zf.write(p, os.path.basename(p))
                else:
                    raise RunValueError(f"「{p}」不存在，无法打包")
    except OSError as e:
        raise RunError(f"打包到「{zip路径}」失败：{e}")
    return str(zip路径)


#: 解压总量的上限（M160）：超过就**不解**，让人自己挑着取。
#: 512 MB 的来由：常见发行包里最大的单文件也就几十 MB；而「39 KB 解出 40 MB」
#: 这种压缩炸弹比例能到 1000:1，不设上限时一个很小的包就能把内存/磁盘吃光。
_最大解压字节 = 512 * 1024 * 1024


def _条目名问题(名: str) -> str:
    """条目名安全检查（M160）。返回「问题的一句话」，空串表示没问题。

    🔴 **不靠黑名单关键字，靠结构**：条目名只能是**相对路径**，且不能往上走。
    解压的写法是「拿条目名当相对路径拼到目标目录后面」，而条目名是**包里写的**、
    不是用户写的 —— 一个 `../../.ssh/authorized_keys` 就能写到目标目录之外。
    这是包管理器里最经典的一类漏洞（PyPI 上出现过多次）。

    ⚠️ 三个宿主（Python / Rust / Node）必须**逐字一致**：这套规则和那句中文
    在三处各写一遍，靠 `tests/test_m160_zip_slip.py` 钉住。
    """
    if not 名:
        return "条目名是空的"
    if "\0" in 名:
        return "条目名里有 NUL 字符"
    if 名.startswith(("/", "\\")):
        return "条目名以「/」开头（绝对路径）"
    if len(名) >= 2 and 名[1] == ":" and 名[0].isalpha():
        return "条目名带盘符（如「C:」）"
    for 段 in 名.replace("\\", "/").split("/"):
        if 段 == "..":
            return "条目名里有「..」（会往上跳到目标目录之外）"
    return ""


#: 「可复现打包」里统一的条目时间（zip 存的是 DOS 时间，1980-01-01 是它的零点）。
#: 定死它——否则同一个目录打两次，字节就不一样了（文件 mtime 每次都参与）。
_确定时间 = (1980, 1, 1, 0, 0, 0)


def 打包确定(zip路径, 来源, 基础: str = "") -> str:
    """**可复现**的 zip：同一个目录打两次，字节完全相同。

    和 `打包` 的差别只有三点，但每一点都必须定死，否则字节就会漂：

    | 会漂的东西 | `打包` | `打包确定` |
    |---|---|---|
    | 条目时间 | 取文件 mtime | 一律 1980-01-01 00:00 |
    | 条目顺序 | 走目录的顺序 | 按条目名**排序** |
    | 权限 / 制作系统 | 跟着平台走 | 一律 `0o600`、MS-DOS |

    ⚠️ **跨引擎不保证逐字节相同**：三个宿主用的压缩算法不同（Python/Node 用
    deflate，Rust 侧只实现了 stored）。可复现的口径是「**同一台机器、同一个
    引擎、同样的输入 ⇒ 同样的字节**」—— 发版产物要的是这个。

    `来源` 给目录就递归收录（条目名是相对 `基础` 的路径）；给文件列表就必须
    同时给 `基础`，否则算不出条目名。
    """
    if not _os.path.isdir(str(来源)) and not isinstance(来源, (list, tuple)):
        raise RunValueError(f"「{来源}」既不是目录也不是列表，无法确定要打包什么")

    项们 = []
    if isinstance(来源, (list, tuple)):
        if not 基础:
            raise RunValueError(
                "传文件列表时必须给「基础」目录 —— 否则算不出 zip 里的条目名",
                hint='例如 压缩.打包确定("包.zip", ["bin/jishi.exe"], 基础="发布")')
        for one in 来源:
            full = str(one)
            项们.append((_os.path.relpath(full, 基础), full))
    else:
        root = str(来源)
        基础 = 基础 or root
        for dirpath, dirnames, filenames in _os.walk(root):
            dirnames.sort()
            for fn in sorted(filenames):
                full = _os.path.join(dirpath, fn)
                项们.append((_os.path.relpath(full, 基础), full))

    if not 项们:
        raise RunValueError(f"「{来源}」里没有文件，打出来的包会是空的")

    项们.sort(key=lambda x: x[0].replace(_os.sep, "/"))
    try:
        with _py_zip.ZipFile(str(zip路径), "w", _py_zip.ZIP_DEFLATED) as zf:
            for arc, full in 项们:
                info = _py_zip.ZipInfo(arc.replace(_os.sep, "/"), date_time=_确定时间)
                info.compress_type = _py_zip.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                # ⚠️ **制作系统也要定死**：ZipInfo 的默认值跟着平台走
                #    （Unix=3 / Windows=0），不定的话同一份代码在两台机器上
                #    打出来的 zip 头就不一样。
                info.create_system = 0
                with open(full, "rb") as f:
                    zf.writestr(info, f.read())
    except OSError as e:
        raise RunError(f"写 zip 失败：{e}") from None
    return str(zip路径)


def 解压(zip路径, 目标目录: str = "") -> str:
    """把 zip 解压到目标目录（默认当前目录），返回目标目录。

    ⚠️ **M160 起有两条硬规矩**（三个宿主一致）：
      ① 条目名不合法（绝对路径 / 盘符 / `..`）⇒ **整包不解**，报错说清是哪个条目；
      ② 解开总量超过 512 MB ⇒ 也不解，提示用 `解压成员` 挑着取。
    早先 Python 侧靠 `extractall` 的「静默改名」躲过一次穿越，但那是**默默放错地方**，
    比报错更糟；Rust / Node 则压根没拦 ⇒ 三边行为本来就不一样。
    """
    import os
    target = str(目标目录) if 目标目录 else "."
    if not _os.path.isfile(str(zip路径)):
        raise RunValueError(f"「{zip路径}」不是文件")
    包名 = _os.path.basename(str(zip路径))
    try:
        with _py_zip.ZipFile(str(zip路径)) as zf:
            条们 = zf.infolist()
            总量 = 0
            for zi in 条们:
                问题 = _条目名问题(zi.filename)
                if 问题:
                    raise RunError(
                        f"解压「{包名}」失败：条目「{zi.filename}」不合法（{问题}）")
                总量 += zi.file_size
            if 总量 > _最大解压字节:
                raise RunError(
                    f"解压「{包名}」失败：解开需要 {总量 / (1024 * 1024):.1f} MB，"
                    f"超过上限 {_最大解压字节 // (1024 * 1024)} MB"
                    "（先用「压缩.列出内容」看看有哪些，再用「压缩.解压成员」只取需要的）")
            os.makedirs(target, exist_ok=True)
            for zi in 条们:
                dest = _os.path.join(target, zi.filename)
                if zi.is_dir():
                    os.makedirs(dest, exist_ok=True)
                    continue
                上层 = _os.path.dirname(dest)
                if 上层:
                    os.makedirs(上层, exist_ok=True)
                with zf.open(zi) as src, open(dest, "wb") as dst:
                    while True:
                        buf = src.read(65536)
                        if not buf:
                            break
                        dst.write(buf)
    except (_py_zip.BadZipFile, OSError) as e:
        raise RunError(f"解压「{包名}」失败：{e}")
    return target


def 列出内容(zip路径) -> list:
    """返回 zip 里的条目名列表（含目录，按原始顺序）。"""
    if not _os.path.isfile(str(zip路径)):
        raise RunValueError(f"「{zip路径}」不是文件")
    try:
        with _py_zip.ZipFile(str(zip路径)) as zf:
            return zf.namelist()
    except _py_zip.BadZipFile as e:
        raise RunError(f"「{zip路径}」不是有效的 zip：{e}")


def 压缩文件(gz路径, 内容) -> str:
    """把**一份内容**压成 `.gz` 单文件，返回这个 gz 的路径。

    与 `打包`（zip）的分工：zip 是「一箱多件、要目录表」，gzip 是「一个文件
    一个流」—— 日志轮转、单个产物存档用它更合适（解压端不用先解析目录表）。
    ⚠️ 文本按 UTF-8 编码后压；`解压文件` 再按 UTF-8 解回来。
    """
    值 = jishi_repr(内容, top=True)
    目标 = str(gz路径)
    目录 = _os.path.dirname(目标)
    try:
        if 目录:
            _os.makedirs(目录, exist_ok=True)
        # `mtime=0`：同一份内容每次压出来的**头部一个字节都不变**（否则 gzip
        # 会把当前时间写进头里，两份「内容相同」的产物就永远不一样）。
        # `filename=""`：**不写原始文件名** —— 传了 fileobj 时 gzip 默认会把
        # `fileobj.name` 写进头里的 FNAME 字段，于是「同一份内容存成不同的文件名」
        # 会压出不同的字节（Node / Rust 那边都不写 FNAME）。
        with open(目标, "wb") as f:
            with _py_gzip.GzipFile(filename="", fileobj=f, mode="wb",
                                   mtime=0) as gz:
                gz.write(值.encode("utf-8"))
    except OSError as e:
        raise RunError(f"压缩到「{gz路径}」失败：{e}")
    return 目标


def 解压文件(gz路径) -> str:
    """把 `.gz` 单文件解开，返回里面的文本（按 UTF-8 解码）。

    ⚠️ 解出来的**不是 UTF-8 文本**时会明确报错（基石里文本就是文本；
    要处理二进制请走 `编码` 模块）。
    """
    目标 = str(gz路径)
    if not _os.path.isfile(目标):
        raise RunValueError(f"「{gz路径}」不是文件")
    try:
        with _py_gzip.open(目标, "rb") as gz:
            原 = gz.read()
    except (OSError, EOFError) as e:
        raise RunError(f"解压「{gz路径}」失败：{e}")
    try:
        return 原.decode("utf-8")
    except UnicodeDecodeError:
        raise RunError(f"「{gz路径}」解出来的不是 UTF-8 文本，没法按文本返回")


def 解压成员(zip路径, 成员名) -> JishiBytes:
    """只取出 zip 里的**一个成员**，返回它的字节（不用把整包解开）。

    「备份包里我只想要里面那一份配置」就是这条 —— `解压` 会把整棵树铺到磁盘上，
    而这个只在内存里取一件。成员名要写 zip 里的完整路径（`列出内容` 给的就是它）。
    """
    目标 = str(zip路径)
    if not _os.path.isfile(目标):
        raise RunValueError(f"「{zip路径}」不是文件")
    名 = str(成员名)
    try:
        with _py_zip.ZipFile(目标) as zf:
            try:
                数据 = zf.read(名)
            except KeyError:
                # ⚠️ 只报**文件名**，不回显完整路径（M121 定）：
                #    ① 路径对用户没价值 —— 他是刚刚自己传进来的；
                #    ② 完整路径会让三宿主的报错**天然不一致**（临时目录带随机串、
                #       分隔符平台相关），而报错语料是要跨引擎逐字对拍的。
                raise RunValueError(
                    f"「{_os.path.basename(目标)}」里没有「{名}」这个成员"
                    "（先用 `压缩.列出内容` 看看有哪些）")
    except _py_zip.BadZipFile as e:
        raise RunError(f"「{目标}」不是有效的 zip：{e}")
    return JishiBytes(数据)
