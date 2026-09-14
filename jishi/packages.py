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
import re
import shutil
import tempfile
import zipfile
from typing import Any, Optional

INDEX_FORMAT = "jishi-package-index"
INDEX_VERSION = 1

#: 默认远端索引（M22.1）。托管在本项目公开仓库里，是静态 JSON，
#: 不需要任何服务端；可用 `--index` 或环境变量 `JISHI_INDEX` 覆盖。
DEFAULT_INDEX_URL = (
    "https://raw.githubusercontent.com/benxiaoniao/JISHI/master/"
    "packages/%E7%B4%A2%E5%BC%95.json"
)

#: 覆盖默认索引的环境变量名
INDEX_ENV_VAR = "JISHI_INDEX"

#: 包元信息里允许出现的字段（发布时校验，拼错字段名会被拦下）
ALLOWED_META_KEYS = {
    "名字", "版本", "描述", "依赖", "入口", "许可", "作者", "主页", "关键字",
}


def default_index_url() -> str:
    """默认索引地址；环境变量 ``JISHI_INDEX`` 优先。"""
    return (os.environ.get(INDEX_ENV_VAR) or "").strip() or DEFAULT_INDEX_URL


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
        raise ValueError(f"远端索引不是合法 JSON：{e}") from e
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
        raise ValueError(f"索引里没有包「{name}」{hint}")
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
        raise ValueError(f"「{pkg_dir}」里没有 包.json，不是一个基石包")
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"「{meta_path}」不是合法 JSON：{e}") from e
    except OSError as e:
        raise ValueError(f"读不了「{meta_path}」：{e}") from e
    if not isinstance(meta, dict):
        raise ValueError(f"「{meta_path}」顶层必须是 JSON 对象")

    name = (meta.get("名字") or "").strip()
    if not name:
        raise ValueError(f"「{meta_path}」缺少必填字段「名字」")
    version = str(meta.get("版本") or "").strip()
    if not version:
        raise ValueError(f"包「{name}」的 包.json 缺少必填字段「版本」")
    if not re.fullmatch(r"\d+(\.\d+){0,2}", version):
        raise ValueError(
            f"包「{name}」的版本「{version}」不是语义化版本（应形如 1.0.0）")

    unknown = set(meta) - ALLOWED_META_KEYS
    if unknown:
        raise ValueError(
            f"包「{name}」的 包.json 有无法识别的字段：{'、'.join(sorted(unknown))}"
            f"（可用字段：{'、'.join(sorted(ALLOWED_META_KEYS))}）")

    deps = meta.get("依赖")
    if deps is not None:
        if not isinstance(deps, dict):
            raise ValueError(f"包「{name}」的「依赖」必须是对象（依赖名 → 版本约束）")
        for dep_name, constraint in deps.items():
            if not isinstance(constraint, str):
                raise ValueError(
                    f"包「{name}」依赖「{dep_name}」的约束必须是字符串")

    entry = meta.get("入口")
    if entry is not None and not isinstance(entry, str):
        raise ValueError(f"包「{name}」的「入口」必须是字符串（模块名）")
    return meta


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
        raise ValueError(
            f"包「{name}」里没有可发布的模块（需要至少一个 .jsh 文件）")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for full, rel in files:
            # 统一用正斜杠，且顶层目录为包名（与 install_package_zip 约定一致）
            zf.write(full, f"{name}/{rel.replace(os.sep, '/')}")
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
    return urlunsplit((parts.scheme, netloc, quote(parts.path, safe="/%"),
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
