#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把基石打包并（可选）上传到 PyPI。

用法::

    python tools/publish_pypi.py                    # 打包 + 校验 + 隐私扫描（不上传）
    python tools/publish_pypi.py --上传 --仓库 testpypi   # 先传 TestPyPI 试一遍
    python tools/publish_pypi.py --上传             # 正式传 PyPI

**为什么默认不上传**：PyPI 上「一个版本只能传一次」——传错了不能覆盖，连删除都
受限（要发邮件申请）。这跟 `publish_github.py` 完全不同（那边可以 force push）。
所以「传」在这里是显式动作，默认只做只读检查。

上传凭据（脚本不落盘、不打印）::

    set PYPI_TOKEN=pypi-xxxxxxxx        # 环境变量（推荐）
    # 或者按 twine 的常规做法写 ~/.pypirc

顺带说一句：本仓库的 `publish_github.py` 是「先隐私屏蔽再发布」，这里**不屏蔽**
——因为 PyPI 的 sdist/wheel 只含 `jishi/` 包与元数据（不含 tests/tools/docs），
本来就该是干净的；所以反过来做：**扫一遍，发现本机路径就拒绝打包**。
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

#: 上传目标。名字 → (twine 仓库名, 说明)
REPOS = {
    "pypi": ("pypi", "https://pypi.org/project/{name}/"),
    "testpypi": ("testpypi", "https://test.pypi.org/project/{name}/"),
}

#: 隐私扫描：这些字样不该出现在要发布的包里。
#: `benxi` 要排除 `benxiaoniao`（那是公开的 GitHub 用户名，出现在仓库 URL 里，正常）。
_PRIVACY = [
    (re.compile(r"benxi(?!aoniao)"), "本机用户名"),
    (re.compile(r"[A-Za-z]:[\\/]+code[\\/]+基石"), "本机项目路径"),
    (re.compile(r"[\\/]\.venv[\\/]"), "本机虚拟环境路径"),
    (re.compile(r"CLion|AppData[\\/]+Roaming"), "本机安装/用户目录"),
    (re.compile(r"xwechat_files|WeChat Files"), "本机微信目录"),
]

#: 只扫这些后缀（二进制文件跳过）
_TEXT_EXT = {".py", ".md", ".txt", ".toml", ".cfg", ".ini", ".json", ".yml",
             ".yaml", ".jsh", ".iss", ".ps1", ".sh", ".rst"}


def read_version() -> str:
    """版本号从 `pyproject.toml` 读（它是单一来源）。"""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("!! pyproject.toml 里找不到 version")
    return m.group(1)


def project_name() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^name\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("!! pyproject.toml 里找不到 name")
    return m.group(1)


def check_version_sync(version: str) -> None:
    """三处版本号必须一致：pyproject ↔ jishi/version.py 兜底常量。

    （`tests/test_m16_release.py` 也钉着这条；这里再查一次，是为了**在打包前**
    就拦住——真传上去一个版本号不符的包，是修不回来的。）
    """
    src = (ROOT / "jishi" / "version.py").read_text(encoding="utf-8")
    m = re.search(r'__FALLBACK__\s*=\s*"([^"]+)"', src)
    if not m:
        raise SystemExit("!! jishi/version.py 里找不到 __FALLBACK__")
    if m.group(1) != version:
        raise SystemExit(
            f"!! 版本号不同步：pyproject={version} / version.py 兜底={m.group(1)}\n"
            f"   先统一它们，再打包。")
    print(f"   版本 {version}（pyproject 与 version.py 一致）")


def query_pypi(name: str, version: str) -> "str | None":
    """查 PyPI：返回 `None`（可发布）/ `'版本已存在'` / `'包已存在'` / `'查不到'`。

    用公开 JSON 接口，不需要认证。网络不通时返回 `'查不到'`——**不阻塞打包**，
    只是上传时 PyPI 自己会拦重复版本。
    """
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            json.load(r)
        return "版本已存在"
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return "查不到"
    except Exception:               # noqa: BLE001 - 网络问题不该挡住打包
        return "查不到"
    # 版本 404 → 看包在不在
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json",
                                    timeout=20) as r:
            json.load(r)
        return "包已存在"
    except urllib.error.HTTPError:
        return None
    except Exception:               # noqa: BLE001
        return "查不到"


def build_packages() -> "list[Path]":
    """清掉旧 dist/ 并构建 sdist + wheel。

    用 `--no-isolation`：本地已装 setuptools，不必为构建再开一个隔离环境去
    联网下载（那样在弱网下会失败得很莫名其妙）。
    """
    shutil.rmtree(DIST, ignore_errors=True)
    r = subprocess.run([sys.executable, "-m", "build", "--no-isolation"],
                       cwd=str(ROOT))
    if r.returncode != 0:
        raise SystemExit("!! 构建失败（看上面 build 的输出）")
    files = sorted(DIST.glob("*"))
    if not files:
        raise SystemExit("!! dist/ 里没有产物")
    return files


def twine_check() -> None:
    r = subprocess.run([sys.executable, "-m", "twine", "check", *map(str, DIST.glob("*"))],
                       cwd=str(ROOT))
    if r.returncode != 0:
        raise SystemExit("!! twine check 没通过（README 渲染或元数据有问题）")


def iter_members(pkg: Path):
    """遍历包内文件：yield (名字, 读取函数)。sdist 是 tar.gz，wheel 是 zip。"""
    if pkg.suffix == ".whl":
        with zipfile.ZipFile(pkg) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                yield info.filename, (lambda n=info.filename: z.read(n))
    elif pkg.name.endswith(".tar.gz"):
        with tarfile.open(pkg, "r:gz") as t:
            for m in t.getmembers():
                if not m.isfile():
                    continue
                yield m.name, (lambda n=m.name: t.extractfile(n).read())


def privacy_scan(packages: "list[Path]") -> int:
    """扫包内文本文件，命中本机路径就报错。返回扫过的文件数。"""
    hits: list[str] = []
    scanned = 0
    for pkg in packages:
        for name, reader in iter_members(pkg):
            if Path(name).suffix.lower() not in _TEXT_EXT:
                continue
            try:
                text = reader().decode("utf-8", "replace")
            except Exception:           # noqa: BLE001
                continue
            scanned += 1
            for pat, what in _PRIVACY:
                m = pat.search(text)
                if m:
                    line = text[:m.start()].count("\n") + 1
                    hits.append(f"{pkg.name}::{name}:{line} 命中「{what}」→ {m.group(0)!r}")
    for h in hits:
        print("   ✗", h)
    if hits:
        raise SystemExit(
            f"!! 包里有 {len(hits)} 处本机路径，拒绝继续。\n"
            f"   （README/文档不写个人路径是硬约束，见 AGENTS.md 第 1 节）")
    return scanned


def summarize(packages: "list[Path]") -> None:
    for pkg in packages:
        kb = pkg.stat().st_size / 1024
        if pkg.suffix == ".whl":
            with zipfile.ZipFile(pkg) as z:
                names = [n for n in z.namelist() if not n.endswith("/")]
        else:
            with tarfile.open(pkg, "r:gz") as t:
                names = [m.name for m in t.getmembers() if m.isfile()]
        print(f"   {pkg.name}  {kb:.1f} KB  {len(names)} 个文件")
        if pkg.name.endswith(".tar.gz"):
            tops = sorted({n.split("/", 1)[1].split("/", 1)[0]
                           for n in names if "/" in n and n.startswith("jishi-")})
            print("   ↑ sdist 顶层内容：" + "、".join(tops) +
                  "（setuptools 默认把 tests/ 也放进去——下游拿到 sdist 能自己跑测试）")
    # C VM 动态库是不是只覆盖了本平台？如实说，别让人以为全平台都带
    native = sorted(p.name for p in (ROOT / "jishi" / "_native").glob("*")
                    if p.suffix in (".dll", ".so", ".dylib"))
    print(f"   随包发出的 C VM 动态库：{native or '（无）'}")
    if not any(n.endswith((".so", ".dylib")) for n in native):
        print("   ⚠️ 只带了 Windows 的 DLL——其他平台的用户装上是**树遍历 / 字节码**"
              "两路可用，C VM 会自动降级（这是已知边界，不是缺陷）。")


def upload(repo: str, token: "str | None") -> int:
    args = [sys.executable, "-m", "twine", "upload", "--non-interactive"]
    if repo == "testpypi":
        args += ["--repository-url", "https://test.pypi.org/legacy/"]
    if token:
        args += ["-u", "__token__", "-p", token]
    args += [str(p) for p in sorted(DIST.glob("*"))]
    r = subprocess.run(args, cwd=str(ROOT))
    return r.returncode


def ensure_tools() -> None:
    """`build` / `twine` 是发布用的开发依赖，不在 pyproject 的 install_requires 里。

    缺了就直说怎么装——比让它跑到一半报 `No module named build` 友好。
    """
    import importlib.util

    missing = [m for m in ("build", "twine") if importlib.util.find_spec(m) is None]
    if missing:
        raise SystemExit(
            f"!! 缺少发布工具：{'、'.join(missing)}\n"
            f"   先装一下：{sys.executable} -m pip install build twine")


def main() -> int:
    # 构建子进程会往同一个 stdout 写，行缓冲能让我们的步骤与它的输出不交错
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:                   # noqa: BLE001 - 老环境没有 reconfigure
        pass
    argv = sys.argv[1:]
    do_upload = "--上传" in argv or "--upload" in argv
    repo = "pypi"
    if "--仓库" in argv:
        repo = argv[argv.index("--仓库") + 1]
    elif "--repo" in argv:
        repo = argv[argv.index("--repo") + 1]
    if repo not in REPOS:
        raise SystemExit(f"!! 不认识的仓库 {repo}（可选 {'/'.join(REPOS)}）")

    name = project_name()
    version = read_version()
    print(f"基石 PyPI 发布 · {name} {version} → {repo}\n")
    ensure_tools()

    print("[1/6] 校验版本号一致性")
    check_version_sync(version)

    print("[2/6] 查 PyPI 上这个版本是否已存在")
    state = query_pypi(name, version)
    if state == "版本已存在":
        raise SystemExit(
            f"!! PyPI 上已经有 {name} {version} 了——**同一个版本不能覆盖**。\n"
            f"   要发新版就改 pyproject.toml 的 version（并同步 jishi/version.py）。")
    elif state == "包已存在":
        print(f"   {name} 这个包名已存在（本次是**新版本**发布）")
    elif state is None:
        print(f"   {name} 在 PyPI 上还没有——本次是**首次发布**")
    else:
        print("   查询失败（网络？）——不挡打包；重复版本 PyPI 会自己拦")

    print("[3/6] 构建 sdist + wheel")
    packages = build_packages()
    print(f"   产物：{[p.name for p in packages]}")

    print("[4/6] twine check（元数据 + README 渲染）")
    twine_check()
    print("   通过")

    print("[5/6] 隐私扫描（包内不许有本机路径）")
    scanned = privacy_scan(packages)
    print(f"   扫了 {scanned} 个文本文件，无本机路径 ✓")

    print("[6/6] 汇总")
    summarize(packages)

    url = REPOS[repo][1].format(name=name)
    if not do_upload:
        print(f"\n--dry-run 完成（**没有上传**）。要正式发布：")
        print(f"   set PYPI_TOKEN=pypi-xxxxxxxx     # 或写好 ~/.pypirc")
        print(f"   python tools/publish_pypi.py --上传" +
              ("" if repo == "pypi" else " --仓库 testpypi"))
        print(f"   发布后页面：{url}")
        return 0

    token = os.environ.get("PYPI_TOKEN") or os.environ.get("TWINE_PASSWORD")
    if token:
        print(f"\n【上传】{repo}（凭据来自环境变量，不会打印）")
    else:
        print(f"\n【上传】{repo}（没找到 PYPI_TOKEN，交给 twine 自己找 ~/.pypirc）")
    print("⚠️ PyPI 不可撤销：这个版本一旦上传就不能覆盖、删除也要申请。")
    code = upload(repo, token)
    if code == 0:
        print(f"\n✓ 已发布：{url}")
        print("   注意 PyPI 页面与索引可能要等 1–2 分钟才刷新。")
    else:
        print("\n!! 上传失败（看上面 twine 的输出）")
    return code


if __name__ == "__main__":
    sys.exit(main())
