# -*- coding: utf-8 -*-
"""基石标准库：打包。把「一个程序」打成**一个带清单的包**，并能验回来。

**为什么需要**：Windows 安装包一直靠 Inno Setup 的 `.iss` 手写，用户要给自己写的
程序打包时**一个工具都没有**。这一层只管三件「事实与规则」：
清单（里头有什么）、生成（装进一个 zip）、校验（解开后还是不是原来那些字节）。

## 三条硬规矩

1. **可复现**：同一个目录、两次 `生成`，**字节完全相同**（与 `压缩.打包确定`
   同一套：时间固定 1980-01-01、条目排序、权限固定）。
2. **清单是唯一事实**：`生成` 把 `清单.json` 放在包**根上**，`校验` 只认它 ——
   安装器（自解压 stub）解完之后照这份清单逐份核对，对不上就报错，不是「大概齐」。
3. **三宿主逐字一致**：`清单.json` 的**文本本身**也定死了格式（键序、缩进、
   转义规则），否则同一个目录在 Python / Rust / Node 下算出的包不通用。

## 数据长什么样

```jsh
导入 打包
令 选项 = {"名字": "记账", "版本": "1.0.0", "平台": "windows-x64", "入口": "记账.exe"}
令 包 = 打包.生成("发布/", "_出", 选项)     # → _出/记账-1.0.0-windows-x64.zip
令 结果 = 打包.校验(包)
如果 非 结果["通过"]：
    打印(结果["问题们"])
```

`清单(来源, 选项)` 返回的字典就是 `清单.json` 的内容：

```
{
  "名字": "记账",
  "版本": "1.0.0",
  "平台": "windows-x64",
  "入口": "记账.exe",
  "文件数": 3,
  "字节数": 12345,
  "文件们": [
    {"路径": "记账.exe", "大小": 1234, "sha256": "…"},
    {"路径": "share/样式.css", "大小": 567, "sha256": "…"}
  ]
}
```

## 与 `压缩` 的分工

`压缩` 管**怎么装进 zip**（压缩、解压、路径安全），`打包` 管**装进去的东西
该长什么样、装完怎么验**。`生成` 内部用的就是 `压缩` 那套可复现写法。
"""

import hashlib
import json
import os
import struct
import zipfile

from ..errors import RunError, RunValueError

__all__ = ["清单", "生成", "校验", "安装包"]

#: 包里那份清单的文件名。**必须放在包根上** —— 安装器解完第一件事就是找它。
清单名 = "清单.json"

#: 可复现打包的统一条目时间（与 `压缩.打包确定` 同一条）。
_确定时间 = (1980, 1, 1, 0, 0, 0)

#: 自解压包的**尾部索引**魔数（M171）。8 字节，正好贴在文件最末尾。
#:
#: ```text
#: [ 存根二进制 ][ zip 包 ][ zip 起始偏移 u64 ][ zip 长度 u64 ][ JISHIPK1 ]
#: ```
#:
#: 为什么不靠「在文件里搜 zip 魔数」：用户程序里随便一段数据都可能是 `PK\x03\x04`，
#: 从末尾倒着搜会搜错。尾部固定 24 字节索引，读一次就定位得到。
安装包魔数 = b"JISHIPK1"


# ---------------------------------------------------------------------------
# 收集
# ---------------------------------------------------------------------------

def _条目名(full: str, base: str) -> str:
    """`full` 相对 `base` 的条目名（用 `/` 分隔）。

    **纯字符串前缀**，不做 `os.path.relpath`：跨引擎要逐字一致，而 relpath
    在三个宿主上对「相对/绝对混着给」的处理各家不同。给的时候统一着给即可
    （目录式收录天然一致）。算不出就报错 —— 猜一个出来比报错糟。
    """
    f = str(full).replace("\\", "/")
    b = str(base).replace("\\", "/").rstrip("/")
    if f == b:
        return ""
    pre = b + "/"
    if not f.startswith(pre):
        raise RunValueError(
            f"「{full}」不在基础目录「{base}」里面 —— 算不出包里的条目名")
    return f[len(pre):]


def _摊开(来源, 基础: str):
    """把「来源」摊成 `[(条目名, 磁盘路径)]`。口径与 `压缩.打包确定` 相同。"""
    if isinstance(来源, (list, tuple)):
        if not 基础:
            raise RunValueError(
                "传文件列表时必须给「基础」目录 —— 否则算不出包里的条目名",
                hint='例如 打包.清单([\"发布/记账.exe\"], {\"名字\": \"记账\", \"基础\": \"发布\"})')
        项们 = [(_条目名(one, 基础), str(one)) for one in 来源]
    else:
        root = str(来源)
        if not os.path.isdir(root):
            raise RunValueError(
                f"「{root}」既不是目录也不是列表，无法确定要打包什么")
        base = str(基础) if 基础 else root
        项们 = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                项们.append((_条目名(full, base), full))
    for arc, _ in 项们:
        if not arc or arc.endswith("/"):
            raise RunValueError(f"「{arc}」不是有效的包内路径")
    return 项们


def _文件摘要(路径: str) -> str:
    h = hashlib.sha256()
    try:
        with open(路径, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError as e:
        raise RunError(f"读「{路径}」失败：{e}") from None
    return h.hexdigest()


def _取文本(选项, 键: str, 默认: str = "") -> str:
    v = 选项.get(键) if 选项 else None
    if v is None:
        return 默认
    if not isinstance(v, str):
        raise RunValueError(f"选项「{键}」要传文本，给的是 {type(v).__name__}")
    return v


def _选项(选项):
    if 选项 is None:
        return {"名字": "", "版本": "", "平台": "", "入口": "", "基础": ""}
    if not isinstance(选项, dict):
        raise RunValueError(
            "「选项」要传字典，例如 {\"名字\": \"记账\", \"版本\": \"1.0.0\"}")
    return {
        "名字": _取文本(选项, "名字"),
        "版本": _取文本(选项, "版本"),
        "平台": _取文本(选项, "平台"),
        "入口": _取文本(选项, "入口"),
        "基础": _取文本(选项, "基础"),
    }


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------

def _排序键(路径: str) -> bytes:
    """按 **UTF-8 字节序**排 —— Python 默认按码点、JS 按 UTF-16 单元，
    遇到 emoji 文件名三个宿主会各排各的（M168 的教训）。"""
    return str(路径).encode("utf-8")


def _建清单(项们, 选) -> dict:
    文件们 = []
    总 = 0
    for arc, full in 项们:
        try:
            大小 = os.path.getsize(full)
        except OSError as e:
            raise RunError(f"读「{full}」的大小失败：{e}") from None
        总 += 大小
        文件们.append({"路径": arc, "大小": 大小, "sha256": _文件摘要(full)})
    文件们.sort(key=lambda x: _排序键(x["路径"]))
    return {
        "名字": 选["名字"], "版本": 选["版本"], "平台": 选["平台"], "入口": 选["入口"],
        "文件数": len(文件们), "字节数": 总, "文件们": 文件们,
    }


def 清单(来源, 选项=None) -> dict:
    """算这份来源的**清单**（不落盘）：每份文件的大小 + sha256 + 版本/平台/入口。

    `来源` 给目录就递归收录；给文件列表就必须同时给 `选项["基础"]`。
    """
    选 = _选项(选项)
    return _建清单(_摊开(来源, 选["基础"]), 选)


# ---------------------------------------------------------------------------
# 清单.json 的**文本格式**（三宿主逐字一致，所以手写序列化，不用 json.dumps）
# ---------------------------------------------------------------------------

def _转义(s: str) -> str:
    out = []
    for ch in str(s):
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif o < 0x20:
            out.append("\\u%04x" % o)
        else:
            out.append(ch)
    return "".join(out)


def _清单文本(m: dict) -> str:
    """清单 → `清单.json` 的文本。**格式是契约**，改它等于换包格式。"""
    L = ["{"]
    L.append('  "名字": "%s",' % _转义(m["名字"]))
    L.append('  "版本": "%s",' % _转义(m["版本"]))
    L.append('  "平台": "%s",' % _转义(m["平台"]))
    L.append('  "入口": "%s",' % _转义(m["入口"]))
    L.append('  "文件数": %d,' % m["文件数"])
    L.append('  "字节数": %d,' % m["字节数"])
    if not m["文件们"]:
        L.append('  "文件们": []')
    else:
        L.append('  "文件们": [')
        末 = len(m["文件们"]) - 1
        for i, f in enumerate(m["文件们"]):
            L.append('    {"路径": "%s", "大小": %d, "sha256": "%s"}%s'
                     % (_转义(f["路径"]), f["大小"], f["sha256"],
                        "," if i < 末 else ""))
        L.append("  ]")
    L.append("}")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------

def _包文件名(m: dict) -> str:
    名, 版本, 平台 = m["名字"], m["版本"], m["平台"]
    if not 名:
        raise RunValueError(
            "「名字」不能空 —— 它是产物的文件名，也是安装后在开始菜单里显示的名字")
    for 段, 键 in ((名, "名字"), (版本, "版本"), (平台, "平台")):
        for bad in ("/", "\\", ":", "*", "?", '"', "<", ">", "|"):
            if bad in 段:
                raise RunValueError(
                    f"选项「{键}」里不能有「{bad}」—— 它要当文件名用")
    部分 = [名] + ([版本] if 版本 else []) + ([平台] if 平台 else [])
    return "-".join(部分) + ".zip"


def 生成(来源, 输出目录, 选项=None) -> str:
    """把「来源」打成一个带 `清单.json` 的 zip，返回**包路径**。

    同一个目录、同样的选项，跑两次**字节完全相同** —— 发版流程靠这个判断
    「这次到底改了什么」。
    """
    选 = _选项(选项)
    项们 = _摊开(来源, 选["基础"])
    if not 项们:
        raise RunValueError(f"「{来源}」里没有文件，打出来的包会是空的")
    for arc, _ in 项们:
        if arc == 清单名:
            raise RunValueError(
                f"「{清单名}」是打包自己用的名字，不能当程序文件",
                hint="改个名（例如 说明.json）；包根上那份是清单，会被覆盖")
    m = _建清单(项们, 选)
    名 = _包文件名(m)
    输出目录 = str(输出目录)
    try:
        os.makedirs(输出目录, exist_ok=True)
    except OSError as e:
        raise RunError(f"建输出目录「{输出目录}」失败：{e}") from None
    包 = os.path.join(输出目录, 名)

    # 条目按「清单顺序」写：清单.json 在**第一个**，之后是排好序的文件。
    # 顺序定死是包字节可复现的一半（另一半是时间/权限）。
    顺序 = {f["路径"]: i for i, f in enumerate(m["文件们"])}
    项们.sort(key=lambda x: 顺序[x[0]])

    try:
        with zipfile.ZipFile(包, "w", zipfile.ZIP_DEFLATED) as zf:
            _写一条(zf, 清单名, _清单文本(m).encode("utf-8"))
            for arc, full in 项们:
                with open(full, "rb") as f:
                    _写一条(zf, arc, f.read())
    except OSError as e:
        raise RunError(f"写包「{包}」失败：{e}") from None
    return 包


def _写一条(zf, arc: str, 数据: bytes) -> None:
    info = zipfile.ZipInfo(arc, date_time=_确定时间)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    # ⚠️ 制作系统也要定死：默认值跟着平台走（Unix=3 / Windows=0）
    info.create_system = 0
    zf.writestr(info, 数据)


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def 校验(包路径) -> dict:
    """按包里的 `清单.json` 逐份核对，返回 `{通过, 问题们, 清单}`。

    核对四件事：**缺文件** / **大小不对** / **内容不对（sha256）** /
    **清单外多出文件**。四个都会让 `通过` 为假 —— 只比名字不比内容，
    等于没验。
    """
    路径 = str(包路径)
    结果 = {"通过": False, "问题们": [], "清单": {}}
    try:
        zf = zipfile.ZipFile(路径)
    except (OSError, zipfile.BadZipFile) as e:
        return dict(结果, 问题们=[f"打不开包「{路径}」：{e}"])
    with zf:
        names = zf.namelist()
        if 清单名 not in names:
            return dict(结果, 问题们=[
                f"包里没有「{清单名}」—— 这不是「打包.生成」造的包（或者是老包）"])
        try:
            m = json.loads(zf.read(清单名).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            return dict(结果, 问题们=[f"「{清单名}」读不出来：{e}"])
        if not isinstance(m, dict) or not isinstance(m.get("文件们"), list):
            return dict(结果, 问题们=[f"「{清单名}」格式不对：缺「文件们」那一项"])

        应有 = {}
        for f in m["文件们"]:
            if not isinstance(f, dict) or "路径" not in f:
                return dict(结果, 问题们=[f"「{清单名}」里有一条不认识：{f}"])
            应有[str(f["路径"])] = f

        问题们 = []
        实测 = {}
        for n in names:
            if n == 清单名 or n.endswith("/"):
                continue
            实测[n] = zf.read(n)

        for 名 in sorted(应有, key=_排序键):
            if 名 not in 实测:
                问题们.append(f"缺文件：{名}")
                continue
            数据 = 实测[名]
            应该大小 = 应有[名].get("大小")
            if isinstance(应该大小, int) and len(数据) != 应该大小:
                问题们.append(f"大小不对：{名}（清单 {应该大小}，实际 {len(数据)}）")
                continue
            应该摘要 = 应有[名].get("sha256")
            if isinstance(应该摘要, str) and hashlib.sha256(数据).hexdigest() != 应该摘要:
                问题们.append(f"内容不对：{名}（sha256 与清单不符）")
        for 名 in sorted(实测, key=_排序键):
            if 名 not in 应有:
                问题们.append(f"清单外多出：{名}")

    结果["通过"] = not 问题们
    结果["问题们"] = 问题们
    结果["清单"] = m
    return 结果


# ---------------------------------------------------------------------------
# 安装包（自解压）
# ---------------------------------------------------------------------------

def 安装包(包路径, 存根路径, 输出路径="") -> str:
    """把「包」接到「自解压存根」后面，做成**一个能双击的 `setup.exe`**。

    - `存根路径`：`jishi-pack-stub.exe`（Rust 编的原生小程序，**不依赖基石**，
      所以用户还没装基石时它也能跑）。它不在这里生成 —— 模块只负责**拼**。
    - `输出路径`：不给就放在包旁边，叫 `<名字>-<版本>-<平台>-setup.exe`。

    拼法（见 `安装包魔数`）：`[存根][包][偏移 u64][长度 u64][魔数]`。
    存根从自己末尾读那 24 字节，就知道包在哪、多长。
    """
    包 = str(包路径)
    存根 = str(存根路径)
    原始 = _读文件(包, "包")
    try:
        with zipfile.ZipFile(包) as zf:
            m = json.loads(zf.read(清单名).decode("utf-8"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as e:
        raise RunValueError(
            f"「{包}」不是「打包.生成」造的包，接不成安装包：{e}") from None
    if not isinstance(m, dict):
        raise RunValueError(f"「{包}」里的清单不是字典，接不成安装包")
    存根字节 = _读文件(存根, "存根")

    if not 输出路径:
        部 = [str(m.get("名字") or "程序")] + [
            str(m[k]) for k in ("版本", "平台") if m.get(k)]
        输出路径 = os.path.join(os.path.dirname(包) or ".", "-".join(部) + "-setup.exe")
    输出路径 = str(输出路径)
    尾部 = struct.pack("<QQ", len(存根字节), len(原始)) + 安装包魔数
    try:
        目录 = os.path.dirname(输出路径)
        if 目录:
            os.makedirs(目录, exist_ok=True)
        with open(输出路径, "wb") as f:
            f.write(存根字节)
            f.write(原始)
            f.write(尾部)
    except OSError as e:
        raise RunError(f"写安装包「{输出路径}」失败：{e}") from None
    return 输出路径


def _读文件(路径: str, 叫什么: str) -> bytes:
    try:
        with open(路径, "rb") as f:
            return f.read()
    except OSError as e:
        raise RunError(f"读{叫什么}「{路径}」失败：{e}") from None
