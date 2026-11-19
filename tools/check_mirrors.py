# -*- coding: utf-8 -*-
"""三份版本清单摆在一起比（M209）。

发版时跑一次：

    python tools/check_mirrors.py            # 默认：比「最新」与「最新那一版的记录」
    python tools/check_mirrors.py --严格      # 再加一条：整份文件必须逐字节相同

## 为什么不是「整份字节一致」（2026-11-17 实测）

三份 `jishi-release.json` 现在**整份不一样**，而且这是有意的：

| 源 | 内容 |
|---|---|
| 官网 | 生成器产出的**全量历史**（12 个版本，13,190 字节） |
| CNB / GitHub | 发布资产，**只带最新那一版**（1,157 字节） |

对升级来说，只有「最新」与「那一版的记录」有用（`挑资产` 只查这一版）；
历史留档多少不影响任何行为。所以默认口径是：

1. 三份**都取得到**；
2. `最新` 相同；
3. `版本[最新]` 那一段**完全一样**（逐字段比，比 JSON 文本更严 —— 排版不同不算差异）。

`--严格` 才要求整份字节相同 —— 哪天决定把三份统一成同一份，就用它当验收。

退出码：0 = 通过；1 = 不通过（发版时**必须**是 0）。

⚠️ 与 `jishi 升级 --看源 --探` 是同一件事的两种外壳：那条给用户排查（走 Rust 宿主、
带防缓存时间戳），这条给发布流程（纯 Python、能单独跑、输出适合贴日志）。
两边的口径由 `tests/test_m209_mirrors.py` 用离线夹具同时钉住。
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.request

#: 与 `rust/src/selfupdate.rs` 的 `DEFAULT_RELEASE_INDEX_URLS` **同一个顺序**。
#: 改一处必须改另一处 —— 判据（`test_m209_mirrors`）会从源码里读出来对账。
SOURCES = [
    ("cnb", "https://cnb.cool/xumijiandeshanshuo/JISHI/-/releases/latest/download/jishi-release.json"),
    ("github", "https://github.com/benxiaoniao/JISHI/releases/latest/download/jishi-release.json"),
    ("官网", "https://xn--3jsy75e.cn/download/jishi-release.json"),
]

TIMEOUT = 20.0


def 取(url: str) -> bytes:
    """取一份清单。**带防缓存时间戳**（与 `升级` 同一条理由：文件名固定、内容每版都变）。"""
    分 = "&" if "?" in url else "?"
    真 = f"{url}{分}t={int(time.time())}"
    with urllib.request.urlopen(真, timeout=TIMEOUT) as r:
        return r.read()


def 规范化(v) -> str:
    """把一个 JSON 子树写成**与排版无关**的规范文本（键排序、无多余空白）。"""
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def 判定(
    结果: list[tuple[str, int, str, str, str]],
    坏: list[tuple[str, str]],
    严格: bool = False,
) -> int:
    """给一组「取到的」与「取不到的」，判该不该发版。

    `结果` 每项 = `(名, 字节数, sha256, 最新版本, 版本[最新] 的规范文本)`。

    📌 抽成函数是为了**能离线判**：判据拿构造的数据直接调它，不必真连三个源 ——
    真正取数只有 `取()` 那一层。
    """
    if 坏:
        print()
        print("⚠️ 有取不到的源 —— 发版前必须三份都能取到（镜像 / 站点挂了也要修）：")
        for 名, e in 坏:
            print(f"     {名}：{e}")
        return 1

    if len(结果) < 2:
        print("⚠️ 只取到一份，比不了（至少要两份）")
        return 1

    首版 = 结果[0][3]
    首录 = 结果[0][4]
    首和 = 结果[0][2]

    if len({x[3] for x in 结果}) > 1:
        print()
        print("❌ 「最新」不一致 —— 镜像没同步到同一版：")
        for 名, n, 和, 版, _ in 结果:
            print(f"     {名:<6} 最新 {版}（{n} 字节，sha256 {和[:12]}…）")
        return 1

    if len({x[4] for x in 结果}) > 1:
        print()
        print(f"❌ 「最新」都是 {首版}，但**那一版的记录不一样** —— 清单被改过 / 只同步了一半：")
        for 名, n, 和, _, 录 in 结果:
            print(f"     {名:<6} {n} 字节，sha256 {和[:12]}…，记录 {len(录)} 字符")
        return 1

    if 严格 and len({x[2] for x in 结果}) > 1:
        print()
        print("❌ --严格：整份文件不是逐字节相同：")
        for 名, n, 和, _, _ in 结果:
            print(f"     {名:<6} {n} 字节，sha256 {和[:12]}…")
        print("   （默认口径只要求「最新」与「那一版记录」一致 —— 官网留全史、镜像只带最新）")
        return 1

    print()
    if len({x[2] for x in 结果}) == 1:
        print(f"✅ 三份逐字节一致（最新 {首版}，sha256 {首和[:12]}…）")
    else:
        print(f"✅ 最新 {首版} 与它的记录三份一致（整份字节不同：历史留档数量不一样，不影响升级）")
    return 0


def main(argv: list[str]) -> int:
    严格 = "--严格" in argv
    print("== 三份版本清单比对 ==")
    结果: list[tuple[str, int, str, str, str]] = []
    坏: list[tuple[str, str]] = []
    for 名, url in SOURCES:
        try:
            体 = 取(url)
        except Exception as e:            # noqa: BLE001 —— 网络错什么类型都有
            坏.append((名, str(e)[:120]))
            print(f"  {名:<6} 取不到：{e}")
            continue
        try:
            d = json.loads(体.decode("utf-8"))
            最新 = str(d.get("最新", ""))
            录 = 规范化((d.get("版本") or {}).get(最新))
        except Exception as e:            # noqa: BLE001
            坏.append((名, f"不是合法清单：{e}"))
            print(f"  {名:<6} 取到了，但不是合法清单：{e}")
            continue
        和 = hashlib.sha256(体).hexdigest()
        结果.append((名, len(体), 和, 最新, 录))
        print(f"  {名:<6} {len(体)} 字节  sha256 {和[:12]}…  最新 {最新}")

    return 判定(结果, 坏, 严格)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
