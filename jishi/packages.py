# -*- coding: utf-8 -*-
"""基石包管理雏形（M10.2）：远端索引格式定义 + 依赖版本约束。

本模块只做「格式定义与解析」，不建站、不发网络请求（先以静态 JSON 文件
托管索引即可，见 docs/语言规格.md「包管理」小节）。零第三方依赖。

远端索引格式（JSON，顶层结构固定）：
    {
      "格式": "jishi-package-index",   # 固定标记
      "版本": 1,                       # 索引格式版本号
      "包": {                          # 包名 → 元信息
        "某包": {
          "版本": "1.0.0",             # 语义化版本
          "下载地址": "https://…/某包-1.0.0.zip",
          "依赖": {"基础库": ">=1.0.0"},   # 可选：名字 → 版本约束
          "描述": "……"                 # 可选
        }
      }
    }

版本约束语法（`_version_satisfies`）：
    `*` 或空           任意版本
    `1.2.3`            精确等于
    `==1.2.3`          精确等于
    `>=1.2.3` / `<=1.2.3` / `>1.2.3` / `<1.2.3`
    `^1.2.3`           主版本锁定（>=1.2.3 且 <2.0.0）
"""

from __future__ import annotations

INDEX_FORMAT = "jishi-package-index"
INDEX_VERSION = 1


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
    import json

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
        raise ValueError(f"索引里没有包「{name}」")
    meta = packages[name]
    if not isinstance(meta, dict):
        raise ValueError(f"包「{name}」的元信息格式错误")
    return meta


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
