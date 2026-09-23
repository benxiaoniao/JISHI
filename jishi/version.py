# -*- coding: utf-8 -*-
"""版本号单一来源。

版本号只在 `pyproject.toml` 里维护一份（`[project] version`），
这里通过 importlib.metadata 读取，供 CLI、构建脚本、发布说明共用，
杜绝「cli.py 硬编码 0.1.0、pyproject 写 0.1.0」这类多处不一致（M16.1 / D21）。

打包成 PyInstaller 单文件后（无包元数据），回退到内置常量 __FALLBACK__。
"""

from __future__ import annotations

#: 兜底版本：仅当 importlib.metadata 查不到（PyInstaller 单文件 / 源码直跑）时用
__FALLBACK__ = "0.2.0"


def _read_version() -> str:
    try:
        from importlib.metadata import version as _pkg_version
        return _pkg_version("jishi")
    except Exception:  # noqa: BLE001 —— 打包后无元数据 / 未安装
        return __FALLBACK__


VERSION = _read_version()
