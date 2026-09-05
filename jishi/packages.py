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
