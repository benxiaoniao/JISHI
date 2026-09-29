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


def download(url: str, dest: Path, expect_size: int) -> None:
    """下载到 dest（已存在且大小一致就跳过）。"""
    if dest.is_file() and dest.stat().st_size == expect_size:
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
        for a in assets:
            if a["name"] == "SHA256SUMS":
                sums_name = a
                download(a["browser_download_url"], OUT / a["name"], a["size"])
        if sums_name is None:
            print("   ⚠️ 没有 SHA256SUMS，无法校验（仍会下载，但会提示）", file=sys.stderr)

        wanted = [a for a in assets if a["name"] != "SHA256SUMS"]
        for a in wanted:
            download(a["browser_download_url"], OUT / a["name"], a["size"])

        # 逐个校验：官网那份必须与 GitHub 那份逐字节相同
        sums_file = OUT / "SHA256SUMS"
        if sums_file.is_file():
            want = {}
            for line in sums_file.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    want[parts[-1].lstrip("*")] = parts[0]
            for a in wanted:
                name = a["name"]
                if name not in want:
                    print(f"   ⚠️ SHA256SUMS 里没有 {name}", file=sys.stderr)
                    continue
                got = sha256(OUT / name)
                ok = got == want[name]
                print(f"   {'✓' if ok else '❌'} {name}")
                if not ok:
                    raise SystemExit(f"❌ {name} 校验和不一致，别发布")

    total = sum(f.stat().st_size for f in OUT.glob("jishi-*"))
    print(f"\n✓ 就绪 → {OUT}（共 {total // 1024 // 1024} MB）")
    print("  下一步：JISHI_SSH_KEY=… JISHI_SSH_HOST=… bash tools/deploy_push.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
