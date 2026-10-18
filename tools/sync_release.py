# -*- coding: utf-8 -*-
"""把 GitHub Release 的发行包同步到自建站点的 `download/` 目录。

🔴 为什么需要它（M122 加，2026-10-18）：

`deploy_push.sh` 只部署「站点 + 语言包（`packages`）」，**不碰 `download/`**。
于是每次发版完，`/var/www/jishi/download/jishi-release.json` 还是旧版本 ——
v0.2.5 发布完之后，`jishi 升级` 看到的**还是 0.2.4**。

这一步以前是手工的（下载 → 改清单 → scp），**常常忘**。现在做成一条命令，
并由 `publish_github.py --等发布` 在 Release 成功后自动调用。

⚠️ 清单是**累积式**的（`版本` 里保留所有历史版本）—— 所以**只能合并，不能覆盖**：
覆盖掉等于让老版本的用户没法回退。

用法：
    JISHI_SSH_KEY=… JISHI_SSH_HOST=… python tools/sync_release.py --版本 0.2.5
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 公开快照仓库的 Release 下载前缀
_GH = "https://github.com/benxiaoniao/JISHI/releases/download"

#: 站点对外域名（CDN 会缓存，但源站一改就生效）
站点 = "https://xn--3jsy75e.cn"

#: 兜底：远端放发行包的目录（服务器上的绝对路径）
远端目录 = os.environ.get("JISHI_REMOTE_DOWNLOAD", "/var/www/jishi/download")

#: Release 里的资产名模板
资产们 = (
    "jishi-{v}-linux-x64.tar.gz",
    "jishi-{v}-macos-arm64.tar.gz",
    "jishi-{v}-windows-x64-setup.exe",
    "jishi-{v}-windows-x64.zip",
    "jishi-release.json",
    "SHA256SUMS",
)


def 合并清单(旧: dict, 新: dict) -> dict:
    """把一份新清单并进旧的 —— **累积式，老版本必须留着**。

    🔴 这是本脚本唯一有「语义」的地方：清单是**全历史**的索引，
    覆盖掉就等于让老版本的用户没法回退到他们装过的那个版本。
    （格式/最新 跟新的走；`版本` 取并集。）
    """
    合并 = dict(旧) if 旧 else dict(新)
    合并["格式"] = 新["格式"]
    合并["格式版本"] = 新["格式版本"]
    历史 = dict(旧.get("版本", {})) if 旧 else {}
    历史.update(新.get("版本", {}))
    合并["版本"] = 历史
    合并["最新"] = 新["最新"]
    return 合并


def _取(u: str, 超时: int = 120) -> bytes:
    req = urllib.request.Request(u, headers={"User-Agent": "jishi-sync"})
    with urllib.request.urlopen(req, timeout=超时) as r:
        return r.read()


def _ssh(命令: str) -> str:
    钥匙, 主机 = os.environ["JISHI_SSH_KEY"], os.environ["JISHI_SSH_HOST"]
    r = subprocess.run(
        ["ssh", "-i", 钥匙, "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ConnectTimeout=15", 主机, 命令],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"ssh 失败：{r.stderr.strip()[:400]}")
    return r.stdout


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="同步发行包到自建站点 download/")
    ap.add_argument("--版本", required=True, help="不带 v，例如 0.2.5")
    ap.add_argument("--本地-tar", default=None,
                    help="可选：本地已有的资产目录（不传就从 GitHub Release 下）")
    ap.add_argument("--只下载", action="store_true", help="只下到本地，不上传（调试用）")
    参 = ap.parse_args(argv)
    v = 参.版本.lstrip("v")

    if not os.environ.get("JISHI_SSH_KEY") or not os.environ.get("JISHI_SSH_HOST"):
        if not 参.只下载:
            raise SystemExit("缺 JISHI_SSH_KEY / JISHI_SSH_HOST"
                             "（只想下载就加 --只下载）")

    目录 = Path(参.本地_tar) if 参.本地_tar else Path(tempfile.mkdtemp(prefix="jishi-rel-"))
    目录.mkdir(parents=True, exist_ok=True)

    # ---- 1. 取资产 --------------------------------------------------------
    print(f"[1/4] 取 v{v} 的发行包 → {目录}")
    for 名 in 资产们:
        文件 = 目录 / 名.format(v=v)
        if 文件.is_file() and 文件.stat().st_size > 0:
            print(f"   · {文件.name}（已有，跳过）")
            continue
        内容 = _取(f"{_GH}/v{v}/{名.format(v=v)}")
        文件.write_bytes(内容)
        print(f"   ✓ {文件.name}  {len(内容)/1024/1024:.1f} MB")
    if 参.只下载:
        print(f"\n--只下载：资产在 {目录}")
        return 0

    # ---- 2. 合并清单（**必须合并**：覆盖会让老版本没法回退）------------------
    print("[2/4] 合并版本清单（累积式）")
    新 = json.loads((目录 / "jishi-release.json").read_text(encoding="utf-8"))
    try:
        旧文本 = _ssh(f"cat {远端目录}/jishi-release.json 2>/dev/null || true")
        旧 = json.loads(旧文本) if 旧文本.strip() else {}
    except Exception as e:                       # noqa: BLE001
        print(f"   ⚠ 读远端旧清单失败（{type(e).__name__}），按「首次」处理")
        旧 = {}
    合并 = 合并清单(旧, 新)
    (目录 / "jishi-release.json").write_text(
        json.dumps(合并, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"   清单版本：{'、'.join(sorted(合并['版本']))}（最新 {合并['最新']}）")

    # ---- 3. 上传 ----------------------------------------------------------
    # ⚠️ **不能直接 scp 到目标目录**：`/var/www/jishi/download` 是 root 的，
    #    `ubuntu` 用户写不进去（`scp: dest open … Permission denied`）。
    #    `deploy_push.sh` 的老办法是「先传 /tmp，再 sudo 搬」—— 这里照抄。
    临时 = f"/tmp/jishi-rel-{v}"
    print(f"[3/4] 传到 {临时}，再 sudo 搬到 {远端目录}")
    _ssh(f"mkdir -p {临时}")
    要传 = [目录 / n.format(v=v) for n in 资产们]
    r = subprocess.run(
        ["scp", "-i", os.environ["JISHI_SSH_KEY"],
         "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15"]
        + [str(p) for p in 要传]
        + [f"{os.environ['JISHI_SSH_HOST']}:{临时}/"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"scp 失败：{r.stderr.strip()[:400]}")
    _ssh(f"sudo mv -f {临时}/* {远端目录}/ && rmdir {临时}")
    print(f"   ✓ {len(要传)} 个文件已就位")

    # ---- 4. 验证（真去 CDN 上读一次，不假设成功）---------------------------
    print("[4/4] 验证")
    开 = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with 开.open(urllib.request.Request(
            f"{站点}/download/jishi-release.json",
            headers={"User-Agent": "jishi-sync", "Cache-Control": "no-cache"}), timeout=30) as r:
        在线 = json.loads(r.read().decode())
    if 在线.get("最新") != v:
        raise SystemExit(f"✗ 线上清单还是 {在线.get('最新')}，不是 {v}"
                         "（可能 CDN 有缓存，等一两分钟再看）")
    print(f"   ✓ 线上清单最新 = {在线['最新']}，共 {len(在线.get('版本', {}))} 个版本")
    return 0


if __name__ == "__main__":
    sys.exit(main())
