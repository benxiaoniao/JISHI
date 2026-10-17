#!/usr/bin/env python3
"""把索引里的「下载地址」改写到另一个托管前缀（M49c：自建分发源）。

用法::

    python tools/build_index_mirror.py packages/索引.json 输出.json \\
        --from https://raw.githubusercontent.com/benxiaoniao/JISHI/master/packages/ \\
        --to   https://xn--3jsy75e.cn/packages/ \\
        --zip-dir packages/下载

为什么要「改写」而不是「重算」：索引里有 `校验和`，而校验和是**按 zip 内容**算的。
按前缀改写地址**不动校验和**，语义上安全得多（重算要真去解包每个包）。

为什么索引要分两份：
====================
仓库里那份（推 GitHub）下载地址指向 GitHub；服务器上那份指向基石.cn。
**两份各自自洽** —— 谁的索引，包就从谁那儿下。于是：

  · 大陆用户 → 先到自建源 → 索引和 zip 都在境内，快；
  · 自建源故障 → 客户端退到 GitHub 索引 → 拉 GitHub 上的 zip（慢，但能用）。

若只维护一份「下载地址指自建源」的索引推给 GitHub，自建源一挂就**全都装不了**
（索引能拿到、包拿不到）—— 这比"慢"糟得多。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def rewrite(index: dict, old_prefix: str, new_prefix: str) -> tuple[dict, list[tuple[str, str]]]:
    """把每个包的「下载地址」前缀换掉；返回（新索引，[(包名, 新地址)]）。"""
    changed: list[tuple[str, str]] = []
    pkgs = index.get("包", {})
    for name, entry in pkgs.items():
        url = str(entry.get("下载地址") or "")
        if not url:
            continue                                   # 没有下载地址的条目跳过
        if not url.startswith(old_prefix):
            raise SystemExit(
                f"❌ 包「{name}」的下载地址不是预期前缀：\n"
                f"   实际：{url}\n   期望以：{old_prefix}\n"
                "   （索引格式变了？先核对再跑，别猜着改）")
        entry["下载地址"] = new_prefix + url[len(old_prefix):]
        changed.append((name, entry["下载地址"]))
    return index, changed


def main() -> int:
    ap = argparse.ArgumentParser(description="改写索引里的下载地址前缀")
    ap.add_argument("index", help="源索引 JSON")
    ap.add_argument("out", nargs="?", help="输出索引 JSON（省略则原地覆盖）")
    ap.add_argument("--from", dest="old", required=True, help="旧前缀")
    ap.add_argument("--to", dest="new", required=True, help="新前缀")
    ap.add_argument("--zip-dir", help="顺带核对 zip 是否真在本地（发布前的自检）")
    args = ap.parse_args()

    src = Path(args.index) if Path(args.index).is_absolute() else ROOT / args.index
    out = Path(args.out) if args.out else src
    if not out.is_absolute():
        out = ROOT / out

    index = json.loads(src.read_text(encoding="utf-8"))
    index, changed = rewrite(index, args.old, args.new)

    # 自检：新地址指向的 zip 必须在本地存在，否则发布出去就是个死链
    if args.zip_dir:
        from urllib.parse import unquote

        zdir = Path(args.zip_dir) if Path(args.zip_dir).is_absolute() else ROOT / args.zip_dir
        # ⚠️ 索引里的地址是**百分号编码**的（中文包名要编码才安全）——
        # 拿编码后的字符串去拼文件路径当然找不到，必须先 unquote 回中文。
        missing = [n for n, u in changed
                   if not (zdir / unquote(u.rsplit("/", 1)[-1])).is_file()]
        if missing:
            print(f"❌ 这些包的 zip 不在 {zdir}：{'、'.join(missing)}", file=sys.stderr)
            return 1
        print(f"✓ {len(changed)} 个包的 zip 都在 {zdir}")

    out.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
    print(f"✓ 已改写 {len(changed)} 条下载地址 → {out}")
    for name, url in changed:
        print(f"    {name} → {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
