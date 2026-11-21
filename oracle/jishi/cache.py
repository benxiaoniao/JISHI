# -*- coding: utf-8 -*-
"""AST 缓存（M47 ③）：源文件没改就直接加载，省掉分词与语法分析。

**为什么缓存 AST 而不是字节码**：默认执行器是**树遍历**解释器，它要的是 AST、
不是字节码；缓存 AST 两个执行器都受益（字节码路径只需在 AST 上再 emit 一遍，
比重新分词 + 语法分析便宜得多）。实测一个 360 行的真实项目：分词 + 分析约
**14ms**，AST 反序列化约 **2ms**。

**失效判据用「源码内容哈希」，不用 mtime**：mtime 粒度粗（同一秒内改两次就漏），
那会变成「改了代码却没生效」——最难查的一类问题。哈希一遍 360 行的源码约
0.1ms，相对省下的 14ms 完全可以忽略。键里还带上**基石版本**与**缓存格式版本**，
升级语言就自动全部失效。

**缓存放哪**：`~/.jishi/cache/`（与其他用户级文件同一处，见 `lineedit.py` 的
`~/.jishi/repl_history.txt`）。**刻意不放进项目目录**——那样缓存文件会跟着仓库
走，既脏了仓库、又给了「仓库里的缓存被替换成恶意 pickle」的机会。

用 `JISHI_NO_CACHE=1` 可整块关掉（调试/对照用）。
"""

from __future__ import annotations

import hashlib
import os
import pickle
from pathlib import Path
from typing import Any, Optional

#: 缓存格式版本。**改动 AST 结构 或 这里承载的内容时手动 +1**，让旧缓存全部失效。
FORMAT = 3

#: 缓存目录（可用 `JISHI_CACHE_DIR` 覆盖，测试用得上）。
def cache_dir() -> Path:
    env = os.environ.get("JISHI_CACHE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".jishi" / "cache"


def enabled() -> bool:
    """缓存是否启用（`JISHI_NO_CACHE=1` 关闭）。"""
    return os.environ.get("JISHI_NO_CACHE", "") not in ("1", "true", "yes")


def _version() -> str:
    """基石版本（进缓存键：升级语言就让缓存失效）。"""
    try:
        from . import __version__
        return __version__
    except Exception:                                    # pragma: no cover
        return "0"


def _key(source: str, filename: str) -> str:
    parts = [str(FORMAT), _version(), filename, source]
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")                                # 分隔，避免拼接歧义
    return h.hexdigest()


def load(source: str, filename: str) -> Optional[Any]:
    """按源码内容取缓存的 AST；没有/坏了/版本不符 一律返回 None。

    **任何异常都吞掉**：缓存只是加速手段，坏了就当没缓存重编译——
    绝不能让一个坏缓存把程序跑挂。
    """
    if not enabled():
        return None
    p = cache_dir() / (_key(source, filename) + ".pkl")
    try:
        with open(p, "rb") as f:
            blob = pickle.load(f)                        # noqa: S301（自家缓存目录）
    except Exception:                                    # noqa: BLE001
        return None
    if not isinstance(blob, tuple) or len(blob) != 2:
        return None
    fmt, program = blob
    if fmt != FORMAT:
        return None
    return program


def store(source: str, filename: str, program: Any) -> None:
    """把 AST 写进缓存。**失败静默**（盘满 / 只读 / 无权限都不该影响运行）。"""
    if not enabled():
        return
    try:
        d = cache_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = d / (_key(source, filename) + ".pkl")
        # 先写临时文件再 rename：避免并发/中断留下半个文件（半个文件会让
        # 下一次的 pickle.load 抛异常——虽然上面吞掉了，但白读一次）
        tmp = p.with_suffix(".tmp%d" % os.getpid())
        with open(tmp, "wb") as f:
            pickle.dump((FORMAT, program), f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, p)
    except Exception:                                    # noqa: BLE001
        pass


def clear() -> int:
    """清空缓存，返回删掉的条目数（`jishi 清缓存` 用）。"""
    n = 0
    d = cache_dir()
    if not d.exists():
        return 0
    for f in d.glob("*.pkl"):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n
