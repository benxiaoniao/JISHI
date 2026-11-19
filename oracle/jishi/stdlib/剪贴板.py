# -*- coding: utf-8 -*-
"""基石标准库：剪贴板（M185）。

| 平台 | 状态 |
|---|---|
| Windows（Rust 宿主） | ✅ 真做（裸 Win32） |
| 其它平台 | ❌ 明确报「不支持」 |

⚠️ **本宿主一律报「做不到」**：Python 侧虽然能用 ctypes 摸 Win32，但它是
**判据基准**、不是发行版 —— 这里真做会让「同一段脚本在两个宿主上行为不同」
变成一件悄悄发生的事。函数存在（同宽），行为明确报错。
"""

from ..errors import RunValueError

__all__ = ["读文本", "写文本"]


def _做不到(名: str, 提示: str):
    raise RunValueError(
        f"这个宿主没有剪贴板能力，「剪贴板.{名}」用不了"
        "（剪贴板这一版只在 Windows 的 Rust 宿主上实现）",
        hint=提示)


def 读文本() -> str:
    """M185：读剪贴板里的文本（里面不是文本时给空文本）。**只有 Windows 的 Rust 宿主能做。**"""
    _做不到("读文本", "用 Windows 上的 Rust 发行版")


def 写文本(文) -> bool:
    """M185：把文本写进剪贴板。**只有 Windows 的 Rust 宿主能做。**"""
    _做不到("写文本", "用 Windows 上的 Rust 发行版")
