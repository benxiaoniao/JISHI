# -*- coding: utf-8 -*-
"""基石标准库：地图（M187）—— 读我们自己的 `地图.json`。

格式（就这么多，不追 Tiled 的 TMX）：

```json
{"瓦片宽": 32, "瓦片高": 32, "图集": "图集.png",
 "层们": [{"名": "地面", "行们": [[0, 1], [-1, 2]]}]}
```

📌 **页面里传字典、命令行里传路径**：wasm 没有文件系统，所以 `读` 两种输入都收。
"""

import json

from ..errors import RunFileError, RunTypeError, RunValueError

__all__ = ["读", "层"]


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
