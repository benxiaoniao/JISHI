#!/usr/bin/env python3
"""把某个版本的发行包拉到 `dist/release/`，供官网自建下载用（M49）。

用法::

    python tools/fetch_release_mirror.py 0.2.0
    python tools/fetch_release_mirror.py 0.2.0 0.1.27     # 多个版本

**为什么是「开发机拉、再传服务器」**：服务器在大陆拉 GitHub 不稳
（实测 `git fetch` 300 秒超时、tarball 还会中途 `IncompleteRead`），
而开发机拉得动 —— 于是走「开发机下载 → `scp` 到服务器」，
与站点部署同一条链路（`tools/deploy_push.sh`）。

**为什么不在本地重新构建**：发行包里**自带 Python 运行时**，三个平台各打一份，
本来就是在对应平台的 CI 上构建的（`.github/workflows/release.yml`）——
本地跨平台构建不出来，只能取官方那份（**同一份字节**，不另起炉灶）。

拉到之后会拿 `SHA256SUMS` 逐个校验：**官网上的下载必须与 GitHub 上那份逐字节相同**，
否则「换个地方下」就变成了「下到另一个东西」。
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist" / "release"
REPO = "benxiaoniao/JISHI"
UA = {"User-Agent": "jishi-fetch-mirror", "Accept": "application/vnd.github+json"}


def api(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return json.load(r)


def download(url: str, dest: Path, expect_size: int, *, force: bool = False) -> None:
    """下载到 dest。

    默认「已存在且大小一致就跳过」（大文件动辄 45 MB，每次重下没人受得了），
    但 `force=True` 时**一律重下** —— 小元数据文件（如 `SHA256SUMS`）用得上：
    它的文件名不随版本变，靠「大小一致」判断会把**上一版的清单**当成这一版的
    （实测撞过：v0.2.1 的四个包都下好了，却拿 v0.2.0 的清单去校验）。
    """
    if not force and dest.is_file() and dest.stat().st_size == expect_size:
        print(f"   已存在，跳过：{dest.name}")
        return
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"   下载 {dest.name}（{expect_size // 1024 // 1024} MB）…")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r, \
            open(tmp, "wb") as out:
        got = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            if expect_size and got % (1 << 22) < (1 << 16):
                print(f"      {got * 100 // expect_size}%", flush=True)
    if expect_size and tmp.stat().st_size != expect_size:
        raise SystemExit(f"❌ {dest.name} 大小不对："
                         f"{tmp.stat().st_size} ≠ {expect_size}")
    tmp.replace(dest)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def 挑要镜像的(资产们: list[dict], 清单: dict[str, str], tag: str) -> list[dict]:
    """从 Release 的资产里挑出「该镜像的」—— **就按清单（`SHA256SUMS`）挑**。

    清单外的一律**不镜像**，但分两种处理（2026-10-15 加）：

    * **长得像发行包的（`jishi-…`）不在清单里 ⇒ 硬错误**。那说明「手上这份清单
      不是这一版的」—— 那时候「跳过校验」与「校验通过」看起来一模一样。
    * **别的清单外资产 ⇒ 只报一声、跳过**。典型来源是**重跑一次 Release 留下的
      旧资产**（例如被 GitHub 改名过的 `default.json`）。它们不是这一版的东西；
      为此拦住整个发布，只会让「重跑一次 Release」变成「镜像永远同步不了」。
    """
    要 = []
    for a in 资产们:
        if a["name"] in ("SHA256SUMS", "jishi-release.json"):
            continue                       # 这两个单独处理（都要 force 覆盖）
        if a["name"] in 清单:
            要.append(a)
            continue
        if a["name"].startswith("jishi-"):
            raise SystemExit(
                f"❌ {tag} 的 SHA256SUMS 里没有 {a['name']} —— 清单与发行包对不上，"
                f"拒绝发布（不能拿「没校验」充当「校验通过」）")
        print(f"   ⚠️ 清单外的资产，本站不镜像：{a['name']}")
    return 要


def main() -> int:
    versions = sys.argv[1:]
    if not versions:
        print(__doc__)
        return 2
    OUT.mkdir(parents=True, exist_ok=True)

    # 只接受最新版里带的那几个平台名 —— 名字对不上说明发布方式变了，宁可停下
    for ver in versions:
        tag = ver if ver.startswith("v") else f"v{ver}"
        print(f"== {tag} ==")
        rel = api(f"https://api.github.com/repos/{REPO}/releases/tags/{tag}")
        assets = rel.get("assets", [])
        if not assets:
            print(f"   ⚠️ {tag} 没有任何发行资产，跳过")
            continue

        sums_name = None
        版本清单资产 = None
        for a in assets:
            if a["name"] == "SHA256SUMS":
                sums_name = a
                # ⚠️ `force=True`：这个文件名不随版本变，必须每次都取这一版的
                download(a["browser_download_url"], OUT / a["name"], a["size"],
                         force=True)
            elif a["name"] == "jishi-release.json":
                # M110：`jishi 升级` 的入口清单。同 SHA256SUMS 的道理 ——
                # 文件名不随版本变，**必须 force**，否则「大小一样就跳过」
                # 会把上一版的清单当成这一版的。
                版本清单资产 = a
        if sums_name is None:
            # **没有清单就不发布**：一份没校验过的包，与 GitHub 上那份是不是
            # 同一个东西无从得知 —— 宁可停下，也不要放一个「大概是吧」出去。
            raise SystemExit(f"❌ {tag} 里没有 SHA256SUMS，无法校验，拒绝发布")

        if 版本清单资产 is not None:
            download(版本清单资产["browser_download_url"], OUT / "jishi-release.json",
                     版本清单资产["size"], force=True)

        # 清单（`SHA256SUMS`）**定义「这一版有哪些资产」** —— 先读它，再决定下什么。
        sums_file = OUT / "SHA256SUMS"
        want: dict[str, str] = {}
        for line in sums_file.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                want[parts[-1].lstrip("*")] = parts[0]

        # 清单和清单本身之外，Release 里还有的那几个 —— 按清单挑。
        wanted = 挑要镜像的(assets, want, tag)

        for a in wanted:
            download(a["browser_download_url"], OUT / a["name"], a["size"])

        # 逐个校验：官网那份必须与 GitHub 那份逐字节相同
        for a in wanted:
            name = a["name"]
            got = sha256(OUT / name)
            ok = got == want[name]
            print(f"   {'✓' if ok else '❌'} {name}")
            if not ok:
                raise SystemExit(f"❌ {name} 校验和不一致，别发布")

        # 版本清单自己也在 SHA256SUMS 里 —— 一并校验
        if 版本清单资产 is not None:
            if sha256(OUT / "jishi-release.json") != want.get("jishi-release.json"):
                raise SystemExit("❌ jishi-release.json 校验和不一致，别发布")
            print("   ✓ jishi-release.json")

    total = sum(f.stat().st_size for f in OUT.glob("jishi-*"))
    print(f"\n✓ 就绪 → {OUT}（共 {total // 1024 // 1024} MB）")
    print("  下一步：JISHI_SSH_KEY=… JISHI_SSH_HOST=… bash tools/deploy_push.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
