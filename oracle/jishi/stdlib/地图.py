# -*- coding: utf-8 -*-
"""基石标准库：地图（M187）—— 读我们自己的 `地图.json`。

格式（就这么多）：

```json
{"瓦片宽": 32, "瓦片高": 32, "图集": "图集.png",
 "层们": [{"名": "地面", "行们": [[0, 1], [-1, 2]]}]}
```

📌 **页面里传字典、命令行里传路径**：wasm 没有文件系统，所以 `读` 两种输入都收。

## M197：与 Tiled 互通

`从Tiled` / `存Tiled` 换的是 **TMX**（Tiled Map Editor 的格式，2D 关卡的事实标准）。
换了之后还是上面那个地图字典 —— **Tiled 只是另一个入口，不是另一套数据**。

两处换算要知道：

| Tiled 里 | 基石里 | 为什么 |
|---|---|---|
| `gid = 0` | `-1` | Tiled 用 0 表示空格，我们用 -1（0 是合法的图集第一格） |
| `gid = n` | `n - firstgid` | Tiled 的 gid 从 `firstgid` 起（通常 1），我们从 0 数 |

⚠️ 只认 `<data encoding="csv">`（Tiled 的默认）。base64 / zlib 压缩的 `<data>`
**明确报错**，不猜 —— 猜错了整张地图会静默错位。
"""

import json

from ..errors import RunFileError, RunTypeError, RunValueError

__all__ = ["读", "层", "从Tiled", "存Tiled", "寻路"]


def _规整(d):
    if not isinstance(d, dict):
        raise RunTypeError(f"「地图」的内容要字典，给的是{type(d).__name__}")
    for 键 in ("瓦片宽", "瓦片高"):
        if 键 not in d:
            raise RunValueError(f"地图里缺「{键}」")
    层们 = d.get("层们")
    if not isinstance(层们, (list, tuple)):
        raise RunValueError("地图里缺「层们」（应当是列表）")
    return {"瓦片宽": d["瓦片宽"], "瓦片高": d["瓦片高"],
            "图集": d.get("图集"), "层们": list(层们)}


def 读(路径或字典):
    """M187：读地图（给路径就读文件，给字典就直接用）。"""
    if isinstance(路径或字典, dict):
        return _规整(路径或字典)
    if not isinstance(路径或字典, str):
        raise RunTypeError(f"「地图.读」要路径或字典，给的是{type(路径或字典).__name__}")
    try:
        with open(路径或字典, encoding="utf-8") as f:
            文 = f.read()
    except OSError as e:
        raise RunFileError(f"读不了地图「{路径或字典}」：{e.strerror or e}",
                           hint="路径相对于**当前工作目录**；页面里没有文件系统，请直接传字典")
    try:
        d = json.loads(文)
    except json.JSONDecodeError as e:
        raise RunValueError(f"地图「{路径或字典}」不是合法 JSON：{e}")
    return _规整(d)


def 层(地图, 名):
    """M187：按名字取一层（没有这层给 `空`）。"""
    for l in 地图.get("层们", []):
        if isinstance(l, dict) and l.get("名") == 名:
            return l
    return None


# ---------------------------------------------------------------------------
# M197：Tiled TMX 互通
# ---------------------------------------------------------------------------

def _本地(tag: str) -> str:
    """`{ns}map` → `map`。Tiled 有时带命名空间，有时不带。"""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _子(el, 名: str):
    for c in el:
        if _本地(c.tag) == 名:
            return c
    return None


def 从Tiled(路径):
    """M197：读一份 Tiled 的 `.tmx`，变成我们的地图字典。

    例::

        令 地 = 地图.从Tiled("关卡/第一层.tmx")
        令 层 = 地图.层(地, "地面")

    📌 **只认 CSV 数据**（`<data encoding="csv">`，Tiled 的默认存法）。
    base64 / zlib 压缩的 `<data>` 会明确报错 —— 猜错了整张地图会静默错位。
    📌 多个 `<layer>` 都读；对象层 / 图像层**跳过**（它们不是瓦片网格）。
    """
    import xml.etree.ElementTree as ET

    p = str(路径)
    try:
        with open(p, "rb") as f:
            根 = ET.fromstring(f.read())
    except FileNotFoundError:
        raise RunFileError(f"找不到文件「{p}」")
    except OSError as e:
        raise RunFileError(f"读不了「{p}」：{e.strerror or e}")
    except ET.ParseError as e:
        raise RunValueError(f"「{p}」不是合法的 TMX（XML 解析失败：{e}）")

    if _本地(根.tag) != "map":
        raise RunValueError(
            f"「{p}」的根标签是 <{_本地(根.tag)}>，不是 <map> —— 这大概不是 Tiled 地图")

    宽 = int(根.get("width", "0") or 0)
    高 = int(根.get("height", "0") or 0)
    瓦宽 = float(根.get("tilewidth", "0") or 0)
    瓦高 = float(根.get("tileheight", "0") or 0)
    if 宽 <= 0 or 高 <= 0:
        raise RunValueError(f"「{p}」的 map 缺 width / height（现在是 {宽} × {高}）")
    if 瓦宽 <= 0 or 瓦高 <= 0:
        raise RunValueError(f"「{p}」的 map 缺 tilewidth / tileheight")

    # 图集：第一个 tileset 的 <image source>
    图集 = None
    首gid = 1
    for c in 根:
        if _本地(c.tag) != "tileset":
            continue
        首gid = int(c.get("firstgid", "1") or 1)
        img = _子(c, "image")
        if img is not None:
            图集 = img.get("source")
        break

    层们 = []
    for c in 根:
        if _本地(c.tag) != "layer":
            continue
        名 = c.get("name", "")
        数据 = _子(c, "data")
        if 数据 is None:
            层们.append({"名": 名, "行们": [[-1] * 宽 for _ in range(高)]})
            continue
        编 = (数据.get("encoding") or "").lower()
        if 编 != "csv":
            raise RunValueError(
                f"「{p}」的层「{名}」用的是 {编 or '未标注'} 编码，这一版只认 csv"
                "（Tiled 里「地图 → 地图属性 → 图块层格式」改成 CSV 再存）")
        文 = (数据.text or "").strip()
        gid们 = []
        for 片 in 文.replace("\n", ",").split(","):
            片 = 片.strip()
            if 片:
                gid们.append(int(片))
        if len(gid们) != 宽 * 高:
            raise RunValueError(
                f"「{p}」的层「{名}」有 {len(gid们)} 个格子，应当是 {宽} × {高} = {宽 * 高}")
        行们 = []
        for r in range(高):
            行 = []
            for col in range(宽):
                g = gid们[r * 宽 + col]
                行.append(-1 if g == 0 else g - 首gid)
            行们.append(行)
        层们.append({"名": 名, "行们": 行们})

    return {"瓦片宽": 瓦宽, "瓦片高": 瓦高, "图集": 图集, "层们": 层们}


def 存Tiled(地图, 路径):
    """M197：把地图写成 Tiled 的 `.tmx`（Tiled 能直接打开）。

    ⚠️ 写出去的是**最小可用**的 TMX：只有一个 `<tileset>`（指向 `图集` 那张图，
    `firstgid=1`）、每层 CSV 数据。分区、对象层、动画瓦片这些不写。

    📌 `图集` 为 `空` 时写 `tiles.png` 占位（Tiled 要一个 image 才认这个 tileset）。
    """
    d = _规整(地图)
    p = str(路径)
    层们 = [l for l in d["层们"] if isinstance(l, dict)]
    if not 层们:
        raise RunValueError("「存Tiled」的地图里一层都没有")

    宽 = 0
    高 = len(层们[0].get("行们", []))
    for l in 层们:
        行们 = l.get("行们", [])
        高 = max(高, len(行们))
        for r in 行们:
            宽 = max(宽, len(r))
    if 宽 == 0 or 高 == 0:
        raise RunValueError("「存Tiled」的地图是空的（没有格子）")

    瓦宽 = int(round(float(d["瓦片宽"])))
    瓦高 = int(round(float(d["瓦片高"])))
    图 = d.get("图集") or "tiles.png"

    出 = ['<?xml version="1.0" encoding="UTF-8"?>']
    出.append(
        f'<map version="1.10" tiledversion="1.10.2" orientation="orthogonal"'
        f' renderorder="right-down" width="{宽}" height="{高}"'
        f' tilewidth="{瓦宽}" tileheight="{瓦高}" infinite="0"'
        f' nextlayerid="{len(层们) + 1}" nextobjectid="1">')
    出.append(
        f' <tileset firstgid="1" name="tiles" tilewidth="{瓦宽}" tileheight="{瓦高}">')
    出.append(f'  <image source="{_xml转义(str(图))}"/>')
    出.append(" </tileset>")
    for i, l in enumerate(层们, start=1):
        名 = _xml转义(str(l.get("名", f"层{i}")))
        行们 = l.get("行们", [])
        出.append(f' <layer id="{i}" name="{名}" width="{宽}" height="{高}">')
        出.append('  <data encoding="csv">')
        for r in range(高):
            行 = 行们[r] if r < len(行们) else []
            gid = []
            for c in range(宽):
                号 = 行[c] if c < len(行) else -1
                gid.append("0" if 号 is None or int(号) < 0 else str(int(号) + 1))
            出.append(",".join(gid) + ("," if r + 1 < 高 else ""))
        出.append("</data>")
        出.append(" </layer>")
    出.append("</map>")
    文 = "\n".join(出) + "\n"

    try:
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(文)
    except OSError as e:
        raise RunFileError(f"写不了「{p}」：{e.strerror or e}")
    return None


def _xml转义(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# M208：A* 寻路
# ---------------------------------------------------------------------------

def _格坐标(v, 名, 参):
    if isinstance(v, (list, tuple)):
        if len(v) != 2:
            raise RunValueError(f"「{名}」的{参}要给 [列, 行]（两个整数），现在有 {len(v)} 项")
        return int(v[0]), int(v[1])
    if isinstance(v, dict):
        for k in ("列", "行"):
            if k not in v:
                raise RunValueError(f"「{名}」的字典里缺「{k}」")
        return int(v["列"]), int(v["行"])
    raise RunTypeError(
        f"「{名}」的{参}要给 [列, 行] 或 {{\"列\":…,\"行\":…}}，给的是{type(v).__name__}")


def _实心列(v, 名):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [int(x) for x in v]
    if isinstance(v, dict):
        x = v.get("实心")
        return [] if x is None else [int(n) for n in x]
    raise RunTypeError(f"「{名}」的实心要给列表或选项字典，给的是{type(v).__name__}")


def _格实心(号, 实心):
    """与 `物理` 同一口径：空表 ⇒ 非 -1 全实心。"""
    return 号 >= 0 and (not 实心 or 号 in 实心)


def 寻路(瓦片图, 起, 终, 选项=None):
    """M208：A* 寻路（8 向，对角不许穿墙角）。返回 `[[列, 行], …]`（含起终点）；走不通给 `[]`。

    找不到路是**正常的游戏状态**（桥断了），不是错误 —— 所以返回空列表而不是抛异常。
    起 / 终点落在实心格上才会报错（那通常是写错了坐标）。
    """
    import heapq

    if not isinstance(瓦片图, dict):
        raise RunTypeError(f"「寻路」的瓦片图要字典，给的是{type(瓦片图).__name__}")
    raw = 瓦片图.get("行们") or []
    行们 = [[-1 if c is None else int(c) for c in r] for r in raw]
    s = _格坐标(起, "寻路", "起")
    t = _格坐标(终, "寻路", "终")
    实心 = _实心列(选项, "寻路")
    行数 = len(行们)
    列数 = max((len(r) for r in 行们), default=0)
    for 名, p in (("起", s), ("终", t)):
        if p[0] < 0 or p[1] < 0 or p[1] >= 行数 or p[0] >= 列数:
            raise RunValueError(
                f"「寻路」的{名}点 ({p[0]}, {p[1]}) 出界了 —— 这张图是 {列数} 列 × {行数} 行")

    def 号at(p):
        if p[1] >= 行数 or p[1] < 0:
            return -1
        行 = 行们[p[1]]
        return 行[p[0]] if 0 <= p[0] < len(行) else -1

    for 名, p in (("起", s), ("终", t)):
        if _格实心(号at(p), 实心):
            raise RunValueError(
                f"「寻路」的{名}点 ({p[0]}, {p[1]}) 是实心瓦片（号 {号at(p)}）")

    def 通(p):
        if p[0] < 0 or p[1] < 0 or p[1] >= 行数 or p[0] >= 列数:
            return False
        return not _格实心(号at(p), 实心)

    if s == t:
        return [list(s)]
    if not 通(s) or not 通(t):
        return []

    def 启(p):
        dx = abs(p[0] - t[0])
        dy = abs(p[1] - t[1])
        return max(dx, dy) * 1000 + min(dx, dy) * 414

    向 = [(1, 0, 1000), (-1, 0, 1000), (0, 1, 1000), (0, -1, 1000),
          (1, 1, 1414), (1, -1, 1414), (-1, 1, 1414), (-1, -1, 1414)]
    已付 = {s: 0}
    父 = {}
    堆 = [(启(s), 0, s[0], s[1])]
    while 堆:
        _, g, c, r = heapq.heappop(堆)
        p = (c, r)
        if p == t:
            路 = [list(t)]
            q = t
            while q in 父:
                q = 父[q]
                路.append(list(q))
            路.reverse()
            return 路
        if 已付.get(p, 1 << 62) < g:
            continue
        for dc, dr, 价 in 向:
            新 = (c + dc, r + dr)
            if not 通(新):
                continue
            if dc != 0 and dr != 0:
                # 对角：两侧都得通，否则就是从墙角缝里穿过去
                if not 通((c + dc, r)) or not 通((c, r + dr)):
                    continue
            新g = g + 价
            if 新g < 已付.get(新, 1 << 62):
                已付[新] = 新g
                父[新] = p
                heapq.heappush(堆, (新g + 启(新), 新g, 新[0], 新[1]))
    return []
