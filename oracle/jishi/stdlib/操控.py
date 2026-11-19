# -*- coding: utf-8 -*-
"""基石标准库：操控（M208）—— 动作 → 键的映射 + 手柄。

## 为什么要「动作」这一层

没有它，游戏里到处都是 `如果 图形.按着("a") 或 图形.按着("ArrowLeft") …`，
想加个手柄或改个键就得满篇改；而且「这一帧按过」这种边沿语义写在 `或` 里
会**多消费**（两个键的边沿都被清掉）。

```jishi
操控.绑定("左", ["a", "ArrowLeft"])
操控.绑定("右", ["d", "ArrowRight"])
如果 操控.按下("左")： …
令 方向 = 操控.轴("左", "右")     # -1 / 0 / 1
```

## 边界

| 操作 | 网页 / 窗口 | 命令行 / Node |
|---|---|---|
| `绑定` / `解绑` / `绑定表` | ✅ | ✅（**纯逻辑**：绑完不查键，随时能读回来） |
| `按下` / `按过` / `轴` / `手柄` | ✅ 真读设备 | ❌ 报**同一句**平台错 |

## 名字

模块叫 **`操控`** 而不是「输入」：`输入()` 是内建函数（读一行 stdin），
同名模块一导入就打一句「内建 输入(...) 会被模块遮蔽」—— 每个游戏文件刷一行，
对写游戏的人毫无用处。

真实现：`rust/src/stdlib/input.rs`（绑定表）+ `wasm/jishi-wasm.js`（读键盘 / 手柄）。
"""

from ..errors import RunError, RunTypeError, RunValueError

__all__ = ["绑定", "解绑", "按下", "按过", "轴", "绑定表", "手柄"]

_无输入 = ("这个平台没有键盘 / 手柄：「操控」模块只在网页 / 窗口里有"
           "（命令行和 Node 上没有输入设备）。")

# 动作 → 键名们。**有序**（`绑定表()` 的输出要能对拍）。
_表 = []


def _做不到(谁):
    raise RunError(_无输入)


def _动作(v, 名):
    if not isinstance(v, str):
        raise RunTypeError(
            f"「{名}」的动作名要文本（例如 \"左\"），给的是{type(v).__name__}")
    if not v.strip():
        raise RunValueError(f"「{名}」的动作名不能是空文本")
    return v


def _键们(v, 名):
    def 一个(x):
        if not isinstance(x, str):
            raise RunTypeError(
                f"「{名}」的键名要文本（例如 \"a\" / \"空格\" / \"ArrowLeft\"），"
                f"给的是{type(x).__name__}")
        if not x.strip():
            raise RunValueError(f"「{名}」的键名不能是空文本")
        return x

    if isinstance(v, str):
        return [一个(v)]
    if isinstance(v, (list, tuple)):
        return [一个(x) for x in v]
    raise RunTypeError(
        f"「{名}」的键们要给文本或文本列表（例如 [\"a\", \"ArrowLeft\"]），"
        f"给的是{type(v).__name__}")


def _键s_of(动, 谁):
    for k, v in _表:
        if k == 动:
            return v
    raise RunError(
        f"「{谁}」的动作「{动}」还没绑定键 —— 先写 `操控.绑定(\"{动}\", [\"a\"])`"
        "（`操控.绑定表()` 能看现在有哪些动作）")


def 绑定(动作, 键们):
    """M208：把一个动作绑到若干键上（`[]` = 解绑）。**纯逻辑，命令行也能用。**"""
    动 = _动作(动作, "绑定")
    ks = _键们(键们, "绑定")
    清 = []
    for k in ks:
        if k not in 清:
            清.append(k)
    _表[:] = [(k, v) for (k, v) in _表 if k != 动]
    if 清:
        _表.append((动, 清))
        return True
    return False


def 解绑(动作):
    """M208：解掉一个动作（没绑过也算成功）。**纯逻辑。**"""
    动 = _动作(动作, "解绑")
    前 = len(_表)
    _表[:] = [(k, v) for (k, v) in _表 if k != 动]
    return len(_表) != 前


def 按下(动作):
    """M208：这个动作的键**按着**吗。**只在页面 / 窗口里有。**"""
    _做不到("按下")


def 按过(动作):
    """M208：这个动作的键**这一帧按过**吗（边沿）。**只在页面 / 窗口里有。**"""
    _做不到("按过")


def 轴(负动作, 正动作):
    """M208：`-1 / 0 / 1`（两边都按着给 0）。**只在页面 / 窗口里有。**"""
    _做不到("轴")


def 绑定表():
    """M208：现在有哪些动作、各绑了什么键（按绑定顺序）。**纯逻辑。**"""
    return {k: list(v) for (k, v) in _表}


def 手柄(序号=0):
    """M208：读一个手柄 `{连接, 左x, 左y, 右x, 右y, 按钮们, 扳机们}`。**只在页面 / 窗口里有。**"""
    _做不到("手柄")
