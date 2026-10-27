# -*- coding: utf-8 -*-
"""基石包管理（M10.2 格式定义 / M14.2 安装 / M22 生态）。

本模块负责「格式定义与解析」「打包发布」「依赖冲突检测」，不建站——索引
以静态 JSON 托管（默认索引见 ``DEFAULT_INDEX_URL``）。零第三方依赖。

远端索引格式（JSON，顶层结构固定）：
    {
      "格式": "jishi-package-index",   # 固定标记
      "版本": 1,                       # 索引格式版本号
      "包": {                          # 包名 → 元信息
        "某包": {
          "版本": "1.0.0",             # 语义化版本
          "下载地址": "https://…/某包-1.0.0.zip",
          "依赖": {"基础库": ">=1.0.0"},   # 可选：名字 → 版本约束
          "描述": "……",               # 可选
          "校验和": "sha256:…"          # 可选：M22 发布工具生成
        }
      }
    }

版本约束语法（`version_satisfies`）：
    `*` 或空           任意版本
    `1.2.3`            精确等于
    `==1.2.3`          精确等于
    `>=1.2.3` / `<=1.2.3` / `>1.2.3` / `<1.2.3`
    `^1.2.3`           主版本锁定（>=1.2.3 且 <2.0.0）
"""

from __future__ import annotations

import hashlib
import json
import os

from .errors import (
    PackageBuildError, PackageChecksumError, PackageConflictError,
    PackageCycleError, PackageIndexError, PackageLockError, PackageMetaError,
    PackageMissingDepError, PackageNotFoundError, PackageVersionError,
)
import re
import shutil
import tempfile
import zipfile
from typing import Any, Optional

INDEX_FORMAT = "jishi-package-index"
INDEX_VERSION = 1

#: 默认索引源，**按顺序尝试**（M49c）。两个都是静态 JSON，不需要任何服务端。
#:
#: ① **自建源**（基石.cn，EdgeOne CDN + 大陆轻量云）—— 大陆直连、响应快，
#:    而且部署时会把索引里的「下载地址」一并改写成同一个域名，
#:    所以整条安装链（索引 + zip）都在境内；
#: ② **GitHub 源** —— 备用。实测（2026-09-29，上海）：大陆访问 GitHub 的
#:    `git fetch` 传 3MB 就 300 秒超时，`raw.githubusercontent.com` 也常超时。
#:    它只当兜底，**不再是首选**（这正是 M49 这条主线要解决的事）。
#:
#: 📌 两个源各自「自洽」：谁给的索引，下载地址就指向谁的主机 ——
#: 自建源故障时退到 GitHub，会拉到 GitHub 上的 zip（慢，但能用），
#: 不会出现「索引拿到了、下载地址却是那台已经挂掉的机器」。
#:
#: 可用 `--index` 传单个地址，或用环境变量 `JISHI_INDEX` 覆盖（只用一个源）。
DEFAULT_INDEX_URLS = (
    "https://xn--3jsy75e.cn/packages/%E7%B4%A2%E5%BC%95.json",
    "https://raw.githubusercontent.com/benxiaoniao/JISHI/master/"
    "packages/%E7%B4%A2%E5%BC%95.json",
)

#: 首选索引（`DEFAULT_INDEX_URLS[0]`）。保留这个名字给旧调用点与文档。
DEFAULT_INDEX_URL = DEFAULT_INDEX_URLS[0]

#: 覆盖默认索引的环境变量名
INDEX_ENV_VAR = "JISHI_INDEX"

#: 单个索引源的读取超时（秒）。
#: **要短** —— 源不可达时不能让用户干等，快速失败才来得及去试下一个源。
#: （超时太长的教训：以前这里是 15 秒，两个源都不可达就要等半分钟。）
INDEX_TIMEOUT = 6.0

#: 包元信息里允许出现的字段（发布时校验，拼错字段名会被拦下）
ALLOWED_META_KEYS = {
    "名字", "版本", "描述", "依赖", "入口", "许可", "作者", "主页", "关键字",
    # M145：包可以声明「我要多新的基石」。形如 `>=0.2.8`（约束语法与依赖一致）。
    "基石版本",
}


def default_index_urls() -> tuple[str, ...]:
    """按优先级返回索引源列表（M49a）。

    环境变量 ``JISHI_INDEX`` 给了就**只用**它（调试 / 内网镜像的用法）。
    """
    env = (os.environ.get(INDEX_ENV_VAR) or "").strip()
    return (env,) if env else DEFAULT_INDEX_URLS


def default_index_url() -> str:
    """首选索引地址（= ``default_index_urls()[0]``）。"""
    return default_index_urls()[0]


def _parse_version(v: str) -> tuple[int, int, int]:
    """把 `1.2.3` 解析成 (1, 2, 3)；非法段按 0 处理。"""
    parts: list[int] = []
    for x in str(v).strip().split(".")[:3]:
        try:
            parts.append(int(x))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)  # type: ignore[return-value]


def version_satisfies(version: str, constraint: str) -> bool:
    """判断 `version` 是否满足 `constraint`（简单语义化版本约束）。"""
    c = (constraint or "").strip()
    if not c or c == "*":
        return True
    ver = _parse_version(version)
    if c.startswith("^"):
        base = _parse_version(c[1:])
        return base <= ver < (base[0] + 1, 0, 0)
    for op in ("==", ">=", "<=", ">", "<"):
        if c.startswith(op):
            want = _parse_version(c[len(op):])
            if op == "==":
                return ver == want
            if op == ">=":
                return ver >= want
            if op == "<=":
                return ver <= want
            if op == ">":
                return ver > want
            if op == "<":
                return ver < want
    return ver == _parse_version(c)


def parse_index(text: str) -> dict:
    """解析远端索引 JSON 文本，校验格式标记与版本号，返回 dict。

    校验失败抛 ValueError（全中文），供 `jishi 安装` 命令在 M14 复用。
    """
    try:
        body = json.loads(text)
    except json.JSONDecodeError as e:
        # ⚠️ **不要带 `{e}`**（M132 对拍抓到的）：Python 的 `json` 说
        #    「Expecting value: line 1 column 1 (char 0)」，宿主自己的解析器说
        #    「第 0 字节处解析失败」—— 两边各自把**底层解析器**的措辞带出来，
        #    同一份坏索引给出两句不同的话，对拍永远对不上。
        #    📌 这是 M113 那条教训的同一件事：**只报原文，不把底层异常带出来**。
        raise ValueError("远端索引不是合法 JSON") from e
    if not isinstance(body, dict):
        raise ValueError("远端索引顶层必须是 JSON 对象")
    if body.get("格式") != INDEX_FORMAT:
        raise ValueError(
            f"不是基石包索引（格式标记应为「{INDEX_FORMAT}」，"
            f"看到「{body.get('格式')}」）")
    if body.get("版本") != INDEX_VERSION:
        raise ValueError(
            f"索引格式版本 {body.get('版本')} 与当前支持的 {INDEX_VERSION} 不兼容")
    packages = body.get("包")
    if not isinstance(packages, dict):
        raise ValueError("索引里缺少「包」对象（包名 → 元信息）")
    return body


def resolve_package(index: dict, name: str) -> dict:
    """从已解析的索引里取某包的元信息；不存在抛 ValueError。"""
    packages = index.get("包", {})
    if name not in packages:
        # 给出「你是不是想装某个已有的包」提示，减少拼错带来的困惑
        hint = ""
        close = [n for n in packages if name and (name in n or n in name)]
        if close:
            hint = f"，你是不是想装 {'、'.join(close[:3])}？"
        raise PackageNotFoundError(f"索引里没有包「{name}」{hint}")
    meta = packages[name]
    if not isinstance(meta, dict):
        raise ValueError(f"包「{name}」的元信息格式错误")
    return meta


# ---------------------------------------------------------------------------
# 打包与索引构建（M22.2）：`jishi 发布` / `jishi 索引`
# ---------------------------------------------------------------------------

def read_package_meta(pkg_dir: str) -> dict:
    """读并校验一个包目录的 包.json，返回元信息 dict。

    发布路径上的把关口：名字/版本必填，「依赖」必须是 dict，字段名要在
    白名单里（拼错字段名会被拦下，而不是静默忽略）。
    """
    meta_path = os.path.join(pkg_dir, "包.json")
    if not os.path.isfile(meta_path):
        raise PackageMetaError(f"「{pkg_dir}」里没有 包.json，不是一个基石包")
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    except json.JSONDecodeError as e:
        # 同上：不带底层解析器的措辞（见 `parse_index` 那段说明）。
        raise PackageMetaError(f"「{meta_path}」不是合法 JSON") from e
    except OSError as e:
        raise PackageMetaError(f"读不了「{meta_path}」：{e}") from e
    if not isinstance(meta, dict):
        raise PackageMetaError(f"「{meta_path}」顶层必须是 JSON 对象")

    name = (meta.get("名字") or "").strip()
    if not name:
        raise PackageMetaError(f"「{meta_path}」缺少必填字段「名字」")
    version = str(meta.get("版本") or "").strip()
    if not version:
        raise PackageMetaError(f"包「{name}」的 包.json 缺少必填字段「版本」")
    if not re.fullmatch(r"\d+(\.\d+){0,2}", version):
        raise PackageMetaError(
            f"包「{name}」的版本「{version}」不是语义化版本（应形如 1.0.0）")

    unknown = set(meta) - ALLOWED_META_KEYS
    if unknown:
        raise PackageMetaError(
            f"包「{name}」的 包.json 有无法识别的字段：{'、'.join(sorted(unknown))}"
            f"（可用字段：{'、'.join(sorted(ALLOWED_META_KEYS))}）")

    deps = meta.get("依赖")
    if deps is not None:
        if not isinstance(deps, dict):
            raise PackageMetaError(f"包「{name}」的「依赖」必须是对象（依赖名 → 版本约束）")
        for dep_name, constraint in deps.items():
            if not isinstance(constraint, str):
                raise PackageMetaError(
                    f"包「{name}」依赖「{dep_name}」的约束必须是字符串")

    entry = meta.get("入口")
    if entry is not None and not isinstance(entry, str):
        raise PackageMetaError(f"包「{name}」的「入口」必须是字符串（模块名）")

    base = meta.get("基石版本")
    if base is not None and not isinstance(base, str):
        raise PackageMetaError(
            f"包「{name}」的「基石版本」必须是字符串（形如 \">=0.2.8\"）")
    return meta


def check_base_version(meta: dict, name: str) -> None:
    """校验包要求的「基石版本」（M145）。

    为什么要单独一条错：这条的修法是「**升级语言本体**」，与其他包错误
    （换源 / 重下 / 改 包.json）完全不同 —— 混着报会把人指错方向。

    ⚠️ 用 `version_satisfies` 判断（与依赖约束同一套语义），别自己写比较：
    否则 `>=0.2.10` 与 `0.2.9` 谁大就会各说各话。
    """
    want = str(meta.get("基石版本") or "").strip()
    if not want:
        return
    from .version import VERSION

    if not version_satisfies(VERSION, want):
        raise PackageVersionError(
            f"包「{name}」要求的基石版本满足 {want!r}，当前是 {VERSION}。\n"
            f"  · 升级语言本体：jishi 升级\n"
            f"  ⚠️ 别去改 包.json 把要求调低 —— 那只会变成运行时一句"
            f"「没有这个函数」，更难查。")


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def build_package_zip(pkg_dir: str, out_dir: str) -> tuple[str, str]:
    """把一个包目录打成 zip（顶层目录 = 包名），返回 (zip 路径, 包名)。

    zip 内**不含** ``__pycache__`` 与 ``.jishi`` 等噪音；只收 ``包.json``
    与 ``.jsh`` 模块，避免把编辑器的临时文件也发出去。
    """
    meta = read_package_meta(pkg_dir)
    name = (meta["名字"] or "").strip()
    version = str(meta["版本"]).strip()

    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, f"{name}-{version}.zip")

    files: list[tuple[str, str]] = []
    for base, dirs, names in os.walk(pkg_dir):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".jishi", ".git")]
        for fn in sorted(names):
            if fn == "包.json" or fn.endswith(".jsh"):
                full = os.path.join(base, fn)
                rel = os.path.relpath(full, pkg_dir)
                files.append((full, rel))
    modules = [rel for _, rel in files if rel.endswith(".jsh")]
    if not modules:
        raise PackageBuildError(
            f"包「{name}」里没有可发布的模块（需要至少一个 .jsh 文件）")

    # 🔴 **打出来的字节必须「内容决定」**（M129，2026-10-24）：
    #    以前是 `zf.write(full, …)` —— 它会把源文件的 **mtime** 写进 zip 条目。
    #    同一份源码在另一台机器上 checkout 出来 mtime 不同 ⇒ **sha256 完全不同**
    #    （实测：内容一个字没改，只 `utime` 一下，哈希从 `e4cd3e3c…` 变 `14f91d34…`）。
    #    后果很严重：索引里登记的校验和是「某台机器那次打出来的」，别人用同源码
    #    重新打包就**永远对不上** ⇒ **校验和失去判据意义**。
    #
    #    三条固定（与 M124 写 xlsx 的同一套做法）：
    #    ① **`ZIP_STORED` 不压缩** —— Rust 宿主那边就是 stored（`压缩.打包`），
    #       两边都用 stored 才能**字节一致**（用 deflate 的话两边压缩实现细节不同）；
    #    ② **时间戳写死** 1980-01-01 —— zip 格式能表示的最早时间；
    #    ③ **权限位写死** 0644（Windows 上本来就没有，但 Unix 上有，也得固定）；
    #    成员顺序由上面对 `files` 的排序保证。
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for full, rel in files:
            # 统一用正斜杠，且顶层目录为包名（与 install_package_zip 约定一致）
            arc = f"{name}/{rel.replace(os.sep, '/')}"
            zi = zipfile.ZipInfo(arc, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_STORED
            # 「谁做的」固定成 MS-DOS：Python 默认按**当前系统**填（Unix 填 3、
            # Windows 填 0），那会让同一份源码在 Linux 和 Windows 上打出不同字节。
            # Rust 宿主写的是「制作版本 20」（高字节 0 = MS-DOS），这里跟它对齐。
            zi.create_system = 0
            # 权限位写 **0，让 `zipfile` 自己填默认的 `0o600 << 16`**（M129）：
            # `writestr` 里有一句 `if not zinfo.external_attr: … = 0o600 << 16`，
            # 填 0 是**填不进去的**。⇒ Rust 侧改成同样的 `0o600 << 16` 来对齐。
            # ⚠️ 我第一版用 `0o644 << 16`、第二版用 `0`，两次都对不上，就是这个原因。
            zi.external_attr = 0
            with open(full, "rb") as f:
                zf.writestr(zi, f.read())
    return zip_path, name


def encode_url(url: str) -> str:
    """把 URL 里非 ASCII 的部分编码好（域名走 IDNA，路径走百分号编码）。

    包名是中文，文件名形如 ``基础库-1.0.0.zip``；直接写进索引对 curl、
    浏览器、各语言 HTTP 客户端都不友好，因此在**生成索引时**就编码好。
    安装端也复用本函数，兜住「索引是手写、没编码」的情况。
    """
    from urllib.parse import quote, urlsplit, urlunsplit

    try:
        url.encode("ascii")
        return url                                  # 纯 ASCII，无需处理
    except UnicodeEncodeError:
        pass

    parts = urlsplit(url)
    netloc = parts.netloc
    if netloc:
        try:
            netloc.encode("ascii")
        except UnicodeEncodeError:
            host, _, port = netloc.partition(":")
            try:
                host = host.encode("idna").decode("ascii")
            except (UnicodeError, ValueError):
                pass                                # 编不了就原样保留
            netloc = f"{host}:{port}" if port else host
    # safe 里保留 "%"：调用方给的 base_url 可能已部分编码（如 file:// 的
    # as_uri()），再编一次会变成 %25 双重编码。
    # safe 里保留 ":"：**Windows 盘符的冒号不能被编码** —— `file:///C:/…`
    # 编成 `file:///C%3A/…` 之后 urllib 就走不通了（报「文件名、目录名或
    # 卷标语法不正确」）。`file://` 的 base_url 是预期用法（本地镜像），
    # 所以这个冒号必须留着。（M49 写测试时踩到的，是既存问题。）
    return urlunsplit((parts.scheme, netloc, quote(parts.path, safe="/%:"),
                       quote(parts.query, safe="%=&"), parts.fragment))


def index_entry(pkg_dir: str, *, base_url: str = "",
                zip_name: Optional[str] = None) -> dict:
    """由包目录生成一条索引条目（含下载地址与校验和，若给了 base_url）。"""
    meta = read_package_meta(pkg_dir)
    name = (meta["名字"] or "").strip()
    version = str(meta["版本"]).strip()

    entry: dict[str, Any] = {"版本": version}
    if base_url:
        prefix = base_url.rstrip("/")
        entry["下载地址"] = encode_url(
            f"{prefix}/{zip_name or f'{name}-{version}.zip'}")
    if meta.get("依赖"):
        entry["依赖"] = dict(meta["依赖"])
    if meta.get("描述"):
        entry["描述"] = meta["描述"]
    if meta.get("入口"):
        entry["入口"] = meta["入口"]
    return entry


def make_index(packages: dict[str, dict]) -> dict:
    """把 {包名: 条目} 组装成完整索引对象（字段顺序稳定，便于 diff）。"""
    return {
        "格式": INDEX_FORMAT,
        "版本": INDEX_VERSION,
        "包": {k: packages[k] for k in sorted(packages)},
    }


def index_to_text(index: dict) -> str:
    """序列化索引：缩进 2 空格 + 中文不转义 + 末尾换行。"""
    return json.dumps(index, ensure_ascii=False, indent=2) + "\n"


def merge_index(old: dict, name: str, entry: dict) -> dict:
    """把一条条目合并进已有索引，返回新索引（不修改入参）。"""
    packages = dict(old.get("包") or {})
    packages[name] = entry
    return make_index(packages)


def scan_packages(root: str) -> list[str]:
    """扫描目录下所有「含 包.json 的子目录」，返回包目录路径列表。"""
    out: list[str] = []
    if not os.path.isdir(root):
        raise ValueError(f"找不到目录「{root}」")
    if os.path.isfile(os.path.join(root, "包.json")):
        return [root]                      # 传进来的就是单个包
    for name in sorted(os.listdir(root)):
        sub = os.path.join(root, name)
        if os.path.isdir(sub) and os.path.isfile(os.path.join(sub, "包.json")):
            out.append(sub)
    return out


# ---------------------------------------------------------------------------
# 依赖冲突检测（M22.3）：钻石依赖
# ---------------------------------------------------------------------------

def _dep_meta_of(pkg_dir: Optional[str]) -> dict:
    """读包目录的元信息；读不到返回空 dict（缺包由调用方另行报错）。"""
    if not pkg_dir:
        return {}
    try:
        return read_package_meta(pkg_dir)
    except ValueError:
        return {}


def detect_conflicts(
    roots: list[str],
    *,
    find_dir,
    version_of=None,
) -> list[dict]:
    """遍历依赖图，找出「同一个包被要求了互不相容的版本」的情况。

    参数：
        roots      要检查的包名列表（通常是用户安装的那些）
        find_dir   名字 → 包目录（找不到返回 None）；由调用方注入，
                   便于在 CLI（本地缓存目录）与测试（临时目录）里复用
        version_of 名字 → 已安装版本；默认从包目录的 包.json 读

    返回冲突列表，每项形如：
        {
          "包": "基础库",
          "已装版本": "1.5.0",
          "要求": [
            {"来自": "甲", "约束": ">=2.0.0", "链条": ["根", "甲"]},
            {"来自": "乙", "约束": "<2.0.0", "链条": ["根", "乙"]},
          ],
        }

    设计取舍：只**报告**不自动求解（不引入 SAT 求解器）——基石包生态还小，
    把「谁和谁冲突、怎么来的」讲清楚比自动挑版本更有用。
    """
    if version_of is None:
        def version_of(n: str) -> str:                       # noqa: E306
            return str(_dep_meta_of(find_dir(n)).get("版本") or "0.0.0")

    # 包名 → {版本, 要求: [(来自, 约束, 链条)]}
    seen: dict[str, dict] = {}

    def walk(name: str, chain: list[str]) -> None:
        # 环检测：只看「这条链上是否已经出现过」——用包名判断，不能用
        # (名字, 整条链) 当键，否则 甲→乙→甲→乙… 每条链都不同，永不重复。
        if name in chain:
            return
        if len(chain) > 64:                     # 兜底，防病态深图
            return

        pkg_dir = find_dir(name)
        meta = _dep_meta_of(pkg_dir)
        rec = seen.setdefault(name, {"版本": version_of(name), "要求": []})

        for dep_name, constraint in (meta.get("依赖") or {}).items():
            dep_rec = seen.setdefault(
                dep_name, {"版本": version_of(dep_name), "要求": []})
            dep_rec["要求"].append({
                "来自": name,
                "约束": constraint,
                "链条": chain + [name],
            })
            walk(dep_name, chain + [name])

    for root in roots:
        walk(root, [])

    conflicts: list[dict] = []
    for name, rec in sorted(seen.items()):
        reqs = rec["要求"]
        if len(reqs) < 2:
            continue
        version = rec["版本"]
        if all(version_satisfies(version, r["约束"]) for r in reqs):
            continue                                # 所有约束都满足，无冲突
        # 也检查「约束之间是否本身就互斥」（即使当前装的版本恰好都不满足）
        conflicts.append({
            "包": name,
            "已装版本": version,
            "要求": reqs,
        })
    return conflicts


def format_conflict(c: dict) -> str:
    """把一条冲突渲染成中文多行说明（供 CLI 直接打印）。"""
    lines = [f"包「{c['包']}」的版本要求互相打架（当前装的是 {c['已装版本']}）："]
    for r in c["要求"]:
        chain = " → ".join(r["链条"] + [c["包"]])
        lines.append(f"  · {chain} 要求 {r['约束']}")
    lines.append("  建议：统一各上游对它的版本要求，或先卸载引起冲突的包")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 安装目标目录（M14.2）
# ---------------------------------------------------------------------------

def packages_root() -> str:
    """本地包缓存根目录：优先项目 `.jishi/packages/`，其次用户目录。

    与 runtime._package_dirs 的搜索顺序保持一致（先项目后全局）。
    """
    import os
    cwd = os.path.join(os.getcwd(), ".jishi", "packages")
    home = os.path.join(os.path.expanduser("~"), ".jishi", "packages")
    return cwd if os.path.isdir(cwd) else home


def ensure_root() -> str:
    """确保项目 `.jishi/packages/` 存在（安装默认装到这里），返回其路径。"""
    import os
    cwd = os.path.join(os.getcwd(), ".jishi", "packages")
    os.makedirs(cwd, exist_ok=True)
    return cwd


def install_package_dir(src_dir: str, name: str, *, dest_root: str | None = None,
                        force: bool = False) -> str:
    """把本地包目录安装到缓存目录，返回目标路径。

    - 校验包名一致（读 包.json 的「名字」）
    - 已存在且非 force 则报「已安装」；force 覆盖
    """
    import json
    import os
    import shutil

    meta_path = os.path.join(src_dir, "包.json")
    if not os.path.isfile(meta_path):
        raise ValueError(f"「{src_dir}」里没有 包.json，不是一个基石包")
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"「{src_dir}」的 包.json 读不了：{e}")
    real_name = meta.get("名字") or name
    if real_name != name:
        raise ValueError(f"包「{name}」的 包.json 声明名字为「{real_name}」，不一致")
    check_base_version(meta, name)

    root = dest_root or ensure_root()
    target = os.path.join(root, name)
    if os.path.exists(target) and not force:
        raise ValueError(f"包「{name}」已安装（版本 {meta.get('版本', '未知')}），"
                         f"用「jishi 安装 {name} --force」覆盖")
    if os.path.exists(target):
        shutil.rmtree(target)
    shutil.copytree(src_dir, target)
    return target


def install_package_zip(zip_path: str, name: str, *, dest_root: str | None = None,
                        force: bool = False) -> str:
    """把包 zip（内含 包名/ 顶层目录）解压安装到缓存目录，返回目标路径。

    zip 结构约定：`某包/包.json` + `某包/*.jsh`（顶层目录名 = 包名）。
    """
    import os
    import shutil
    import tempfile
    import zipfile

    if not os.path.isfile(zip_path):
        raise ValueError(f"找不到包文件「{zip_path}」")
    root = dest_root or ensure_root()
    with zipfile.ZipFile(zip_path) as zf:
        # 找顶层目录（约定为包名）
        top_dirs = set()
        for n in zf.namelist():
            head = n.split("/", 1)[0]
            if head:
                top_dirs.add(head)
        inner = name if name in top_dirs else None
        if inner is None and len(top_dirs) == 1:
            inner = next(iter(top_dirs))
        if inner is None:
            raise ValueError(
                f"zip「{zip_path}」里没有与包名「{name}」对应的顶层目录")
        # 只读 zip 里的 包.json 来校验「基石版本」（M145）—— 解压之前就要拦下，
        # 装进目录之后再报错就成了「装了一半」。
        try:
            _meta = json.loads(zf.read(f"{inner}/包.json").decode("utf-8"))
        except (KeyError, json.JSONDecodeError, UnicodeDecodeError):
            _meta = {}
        if isinstance(_meta, dict):
            check_base_version(_meta, name)
        target = os.path.join(root, name)
        if os.path.exists(target) and not force:
            raise ValueError(f"包「{name}」已安装，用「--force」覆盖")
        if os.path.exists(target):
            shutil.rmtree(target)
        tmp = tempfile.mkdtemp(prefix="jishi-pkg-")
        try:
            zf.extractall(tmp)
            return install_package_dir(
                os.path.join(tmp, inner), name, dest_root=root, force=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def uninstall_package(name: str, *, dest_root: str | None = None) -> bool:
    """卸载一个本地包，返回是否真的删除了。"""
    import os
    import shutil
    root = dest_root or packages_root()
    target = os.path.join(root, name)
    if os.path.isdir(target):
        shutil.rmtree(target)
        return True
    return False


def list_packages(*, dest_root: str | None = None) -> list[dict]:
    """列出已安装的本地包（名字/版本/描述），按名字排序。"""
    import json
    import os

    root = dest_root or packages_root()
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        pdir = os.path.join(root, name)
        if not os.path.isdir(pdir):
            continue
        meta = {}
        mp = os.path.join(pdir, "包.json")
        if os.path.isfile(mp):
            try:
                with open(mp, encoding="utf-8") as f:
                    meta = json.load(f)
            except (json.JSONDecodeError, OSError):
                meta = {}
        out.append({
            "名字": meta.get("名字", name),
            "版本": meta.get("版本", "未知"),
            "描述": meta.get("描述", ""),
        })
    return out


# ---------------------------------------------------------------------------
# 依赖闭包与锁定文件（M130，2026-10-24）
# ---------------------------------------------------------------------------

LOCK_FORMAT = "jishi-lock"
LOCK_VERSION = 1


def resolve_closure(index: dict, roots: list[str],
                    *, 深度上限: int = 64) -> dict:
    """把「要装的根包」展开成**完整依赖闭包 + 安装顺序**。

    返回 `{"装序": [(名字, 条目)], "冲突": [...], "缺": [...], "环": [...]}`。

    - **装序**是**拓扑序**：依赖一定排在依赖它的包前面（先装基础库再装数学工具）；
    - **冲突**沿用 `detect_conflicts` 的思路，但这里在**解析阶段**就查，
      不等到装完才发现；
    - **缺**是「索引里没有」的依赖 —— 那是最常见的装不上原因，
      要单独说，不能混进「冲突」。

    ## 三条刻意的取舍

    1. **不做版本求解**（不挑「满足约束的最高版本」）。
       索引里每个包只有一个版本，本来就没得挑；将来有多版本再谈。
       ⚠️ 更重要的理由：**自动挑版本会让「装出来的是什么」不可预测** ——
       今天装和明天装可能不同，那就没有可复现性可言了。
    2. **冲突就报错，不静默挑一个**。npm 早期那种「随便挑个高的」是反例。
    3. **同一条依赖链上重复出现的包只装一次**（用包名判，不用链条判）。
    """
    包表 = index.get("包", {})
    装序: list[tuple[str, dict]] = []
    冲突: list[dict] = []
    缺: list[dict] = []
    环: list[list[str]] = []
    已装: set[str] = set()            # 已排进装序的
    访问中: set[str] = set()          # 当前 DFS 栈上（判环）
    # 名字 → [(来自, 约束, 链条)]，最后统一判冲突
    要求: dict[str, list[dict]] = {}

    def walk(名字: str, 链条: list[str]) -> None:
        if 名字 in 访问中:
            环.append(链条 + [名字])
            return
        if 名字 in 已装:
            return
        if len(链条) > 深度上限:
            return

        条目 = 包表.get(名字)
        if not isinstance(条目, dict):
            if 名字 not in [d["包"] for d in 缺]:
                缺.append({"包": 名字, "来自": 链条[-1] if 链条 else None,
                           "链条": list(链条)})
            return

        访问中.add(名字)
        for 依赖名, 约束 in (条目.get("依赖") or {}).items():
            要求.setdefault(依赖名, []).append(
                {"来自": 名字, "约束": str(约束), "链条": 链条 + [名字]})
            walk(依赖名, 链条 + [名字])
        访问中.discard(名字)

        已装.add(名字)
        装序.append((名字, 条目))         # 后序：依赖先入列 = 拓扑序

    for 根 in roots:
        walk(根, [])

    # 判冲突：同一个包被要求了互不相容的约束
    for 名, 要们 in 要求.items():
        条 = 包表.get(名)
        版本 = str((条 or {}).get("版本") or "")
        if not 版本:
            continue
        # 「几种不同的约束」——只要有两种互不相容就算冲突。
        # ⚠️ 不做 SAT：只两两比对，说明白「谁要什么」比求一个解更有用。
        坏 = []
        for i, 甲 in enumerate(要们):
            for 乙 in 要们[i + 1:]:
                if 甲["约束"] == 乙["约束"]:
                    continue
                都满足 = (version_satisfies(版本, 甲["约束"])
                          and version_satisfies(版本, 乙["约束"]))
                if not 都满足 and (甲["来自"] != 乙["来自"]):
                    坏.append({"包": 名, "版本": 版本, "甲": 甲, "乙": 乙})
        if 坏:
            # 去重：同一对只报一次
            见 = set()
            for 项 in 坏:
                k = tuple(sorted((项["甲"]["来自"], 项["乙"]["来自"])))
                if k in 见:
                    continue
                见.add(k)
                冲突.append(项)

    return {"装序": 装序, "冲突": 冲突, "缺": 缺, "环": 环}


def lock_from_closure(闭包: dict) -> dict:
    """把解析结果转成 `jishi.lock` 的内容（**记录的必须是实际要装的**）。"""
    包 = {}
    for 名, 条 in 闭包["装序"]:
        项: dict[str, Any] = {"版本": str(条.get("版本") or "")}
        if 条.get("校验和"):
            项["校验和"] = 条["校验和"]
        if 条.get("下载地址"):
            项["下载地址"] = 条["下载地址"]
        包[名] = 项
    return {"格式": LOCK_FORMAT, "版本": LOCK_VERSION,
            "包": {k: 包[k] for k in sorted(包)}}


def lock_to_text(lock: dict) -> str:
    return json.dumps(lock, ensure_ascii=False, indent=2) + "\n"


def parse_lock(text: str) -> dict:
    """读 `jishi.lock`；格式不对就报清楚，不要让人猜。"""
    try:
        body = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError("jishi.lock 不是合法 JSON") from e
    if not isinstance(body, dict):
        raise ValueError("jishi.lock 顶层必须是 JSON 对象")
    if body.get("格式") != LOCK_FORMAT:
        raise ValueError(
            f"jishi.lock 的「格式」应该是「{LOCK_FORMAT}」，实际是「{body.get('格式')}」")
    if not isinstance(body.get("包"), dict):
        raise ValueError("jishi.lock 缺少「包」对象")
    return body


def plan_from_lock(lock: dict, index: dict) -> dict:
    """按 lock 规划安装：**严格按 lock 里的版本**，索引变了也不许变。

    ⚠️ 三种对不上的情况要分开说（它们该给的修法完全不同）：
    - **锁里有、索引里没有** ⇒ 索引可能换源了或包被撤了 ⇒ 说「换个源或解锁重装」；
    - **索引里版本变了** ⇒ 这是 `--锁定` 的意义所在 ⇒ **仍然按 lock 装**（并提示）；
    - **校验和变了** ⇒ 🔴 最严重：**内容被改过** ⇒ **拒绝安装**。
    """
    包表 = index.get("包", {})
    装序: list[tuple[str, dict]] = []
    缺: list[dict] = []
    变: list[dict] = []
    改: list[dict] = []

    for 名 in lock["包"]:                       # lock 里已按名字排序，顺序稳定
        锁项 = lock["包"][名]
        条目 = 包表.get(名)
        if not isinstance(条目, dict):
            缺.append({"包": 名, "锁版本": 锁项.get("版本")})
            continue

        索版本 = str(条目.get("版本") or "")
        锁版本 = str(锁项.get("版本") or "")
        if 索版本 != 锁版本:
            变.append({"包": 名, "锁版本": 锁版本, "索引版本": 索版本})
            # 仍按 lock 装 —— 但下载地址是「索引里那个」，
            # 所以版本对不上时干脆不下（地址指向的是新版本，装了就是错的）

        锁校验 = str(锁项.get("校验和") or "")
        索校验 = str(条目.get("校验和") or "")
        if 锁校验 and 索校验 and 锁校验 != 索校验:
            改.append({"包": 名, "锁": 锁校验, "索引": 索校验})
            continue

        装序.append((名, 条目))

    return {"装序": 装序, "缺": 缺, "变": 变, "改": 改}
