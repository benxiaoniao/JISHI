#!/usr/bin/env python3
"""生成**语言本体版本清单**（`jishi-release.json`）—— `jishi 升级` 靠它找新版（M110）。

用法::

    python tools/build_version_manifest.py dist/release
    python tools/build_version_manifest.py dist/release --输出 /tmp/jishi-release.json

它在发行目录里扫这些文件名（`tools/build_release.py` / CI 产出的那些）::

    jishi-<版本>-<平台>.zip          # Windows 便携包
    jishi-<版本>-<平台>.tar.gz       # Linux / macOS 包
    jishi-<版本>-<平台>-setup.exe    # Windows 安装程序（Inno）

汇总成::

    { "格式": "jishi-release-index", "格式版本": 1, "最新": "0.2.3",
      "版本": { "0.2.3": { "日期": "...", "平台": {
          "windows-x64": { "包": {"文件": …, "大小": N, "校验和": "sha256:…"},
                           "安装程序": {…} },
          "linux-x64": { "包": {…} }, "macos-arm64": { "包": {…} } } } } }

## 两条纪律

1. ⚠️ **清单里只写文件名，不写绝对地址** —— 下载端按「镜像前缀 + 文件名」拼，
   于是同一份清单能同时服务自建站与 GitHub，多镜像回退不必改清单。
2. 🔴 **必须拿真文件算 SHA256**（不是抄 `SHA256SUMS`）：两份东西各自算一遍，
   最后在测试里**逐字节比**——「清单说的」与「包里的」必须是同一个哈希
   （M105 的规矩：跳过校验必须与校验通过可区分）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 文件名 → 平台键（与 `rust/src/selfupdate.rs` 的 `平台键()` 对齐）。
平台们 = ["windows-x64", "linux-x64", "macos-arm64"]

#: `jishi-<版本>-<平台>.<后缀>`；`<后缀>` 见下（`-setup.exe` 是安装程序）。
_包名 = re.compile(
    r"^jishi-(\d+\.\d+\.\d+)-(windows-x64|linux-x64|macos-arm64)\.(zip|tar\.gz)$")
_安装程序名 = re.compile(
    r"^jishi-(\d+\.\d+\.\d+)-(windows-x64)-setup\.exe$")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def 版本键(v: str) -> tuple[int, int, int]:
    a, b, c = v.split(".")
    return (int(a), int(b), int(c))


def 扫描(目录: Path) -> dict:
    """扫目录里的发行包，返回 `{版本: {平台: {"包": 资产, "安装程序": 资产|None}}}`。"""
    出: dict[str, dict] = {}
    for p in sorted(目录.iterdir()):
        if not p.is_file():
            continue
        名 = p.name
        m = _包名.match(名)
        目标 = None
        if m:
            版本, 平台, _后缀 = m.group(1), m.group(2), m.group(3)
            目标 = ("包", 版本, 平台)
        else:
            m2 = _安装程序名.match(名)
            if m2 and 名.endswith("-setup.exe"):
                目标 = ("安装程序", m2.group(1), m2.group(2))
        if 目标 is None:
            continue
        类, 版本, 平台 = 目标
        出.setdefault(版本, {}).setdefault(平台, {"包": None, "安装程序": None})
        出[版本][平台][类] = {
            "文件": 名,
            "大小": p.stat().st_size,
            "校验和": "sha256:" + sha256(p),
        }
    return 出


def 生成(目录: Path, 版本: str | None = None, 日期: str | None = None) -> dict:
    扫 = 扫描(目录)
    if not 扫:
        raise SystemExit(f"❌ 在 {目录} 里没扫到任何发行包（jishi-<版本>-<平台>.zip / .tar.gz）")
    if 版本:
        if 版本 not in 扫:
            raise SystemExit(f"❌ 指定的版本 {版本} 在 {目录} 里没有对应文件")
        用 = 版本
    else:
        用 = max(扫, key=版本键)
    # 每个平台至少要有一个「包」，否则那一版是残的 —— 明确报错，别发布半份清单
    缺 = []
    for 平台, v in 扫[用].items():
        if v["包"] is None:
            缺.append(平台)
    if 缺:
        raise SystemExit(f"❌ 版本 {用} 的清单不完整：{ '、'.join(缺) } 只有安装程序、没有「包」")
    if 日期 is None:
        import datetime
        日期 = datetime.date.today().strftime("%Y-%m-%d")
    return {
        "格式": "jishi-release-index",
        "格式版本": 1,
        "最新": 用,
        "版本": {
            用: {
                "日期": 日期,
                "平台": {
                    平台: {k: v for k, v in 平台数据.items() if v is not None}
                    for 平台, 平台数据 in sorted(扫[用].items())
                },
            }
        },
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="生成语言本体版本清单（jishi-release.json）")
    ap.add_argument("目录", type=Path, help="发行目录（如 dist/release）")
    ap.add_argument("--版本", default=None, help="只收录这一版（默认取最高版本）")
    ap.add_argument("--日期", default=None, help="这一版的发布日期（默认今天）")
    ap.add_argument("--输出", type=Path, default=None, help="写到哪（默认 <目录>/jishi-release.json）")
    args = ap.parse_args(argv)

    清单 = 生成(args.目录, args.版本, args.日期)
    正文 = json.dumps(清单, ensure_ascii=False, indent=2) + "\n"
    出 = args.输出 or (args.目录 / "jishi-release.json")
    出.write_text(正文, encoding="utf-8", newline="\n")
    平台们 = sorted(清单["版本"][清单["最新"]]["平台"])
    print(f"✓ 版本清单：{出}（最新 {清单['最新']} · 平台 {'、'.join(平台们)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
