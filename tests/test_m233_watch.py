# -*- coding: utf-8 -*-
"""M233 · `路径.监视`（L-6）—— 三平台原生文件监视。

## 判据怎么设计的

监视是**异步 + 跨进程**的东西：引擎在后台线程收系统事件，测试在另一个进程里改文件。
所以这里不玩"函数返回了什么"，而是：

1. 起一个**引擎子进程**，让它监视一个临时目录、把收到的事件逐条打印出来；
2. 测试**在子进程外面**建 / 改 / 改名 / 删文件（真实的其他进程行为）；
3. 最后丢一个 `done.txt` 当"收工信号"，引擎自己退出；
4. 回来核对打印出来的事件清单。

这样验的是**真事**：真线程、真系统 API、真跨进程的目录变化。

## 三平台的差别（判据里显式处理，不假装一样）

| 差别 | 判据怎么处理 |
|---|---|
| macOS 的 FSEvents **改名常常只给新路径**，有时干脆拆成"删 + 增" | 改名那条放宽成「有 `改名`，或者同时有 `删` 和 `增`」 |
| Windows 会把**子目录的 mtime 变**报成 `改 <子目录>` | 只断言"目标文件出现了预期的动作"，不禁止多出来的事件 |
| 事件可能合并 / 迟到 | 每步之间留 0.3 秒，最后统一核对**集合**而不是顺序 |

⚠️ **`监视` 是提示，不是审计** —— 系统缓冲区满了会丢事件，判据也不假装它不会。
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oracle.jishi.tokenizer import rust_available                    # noqa: E402

EXE = ROOT / "rust" / "target" / "release" / (
    "jishi-rs.exe" if os.name == "nt" else "jishi-rs")

_需要引擎 = pytest.mark.skipif(
    not (rust_available() and EXE.is_file()),
    reason="没编 jishi-rs（先 `cargo build --release`）")

_三平台 = pytest.mark.skipif(
    not (sys.platform.startswith("win")
         or sys.platform.startswith("linux")
         or sys.platform == "darwin"),
    reason="只有 Windows / Linux / macOS 有文件监视实现")

#: 引擎侧脚本：监视当前目录，把事件打成 `动作|路径` 一行一条，见到 done.txt 就收工。
#: 写成一份常量是为了让三条判据共用同一段程序（改口径只改一处）。
_脚本 = """导入 路径
导入 文件

令 记录 = []
令 状态 = {"完": 假}

函数 记(项)：
    记录.追加(项["动作"] + "|" + 项["路径"])
    如果 文件.文件名(项["路径"]) == "done.txt"：
        状态["完"] = 真

令 表 = 路径.监视(".", 记, {"递归": 真})
令 轮 = 0
当 (非 状态["完"]) 与 (轮 < 20)：
    路径.等事件(表, 1)
    轮 = 轮 + 1
路径.停止监视(表)

遍历 条 在 记录：
    打印(条)
"""


def _起监视(tmp_path: Path, 递归: bool = True, 脚本: str = _脚本):
    """在 `tmp_path` 里起一个监视子进程，返回 `Popen`。"""
    名 = f"监视_{os.getpid()}_{int(time.time() * 1000) % 100000}.jsh"
    (tmp_path / 名).write_text(脚本, encoding="utf-8", newline="\n")
    return subprocess.Popen(
        [str(EXE), 名],
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _收工(tmp_path: Path, 进程, 等: float = 40.0):
    """丢一个 `done.txt` 让引擎自己退出，返回 `(输出行, 退出码)`。"""
    (tmp_path / "done.txt").write_text("x", encoding="utf-8")
    try:
        输出, _ = 进程.communicate(timeout=等)
    except subprocess.TimeoutExpired:
        进程.kill()
        输出, _ = 进程.communicate()
        raise AssertionError(f"监视进程没在 {等} 秒内退出（事件泵卡住了？）")
    文本 = 输出.decode("utf-8", "replace")
    return [行 for 行 in 文本.splitlines() if "|" in 行], 进程.returncode, 文本


@_需要引擎
@_三平台
def test_增改删都能收到(tmp_path):
    """建 / 改 / 删一个文件 —— 三种动作都要真的收到（这是这一站的核心判据）。"""
    子 = tmp_path / "数据"
    子.mkdir()
    进程 = _起监视(子) if False else _起监视(tmp_path, 脚本=_脚本.replace('"."', '"数据"'))
    子.mkdir(exist_ok=True)
    time.sleep(1.5)                                   # 等后台线程把 watch 挂上

    (子 / "甲.txt").write_text("1", encoding="utf-8")
    time.sleep(0.4)
    (子 / "甲.txt").write_text("22", encoding="utf-8")
    time.sleep(0.4)
    (子 / "甲.txt").unlink()
    time.sleep(0.4)

    事件, 码, 全文 = _收工(tmp_path, 进程)
    assert 码 == 0, f"引擎异常退出：\n{全文[:800]}"

    行们 = set(事件)
    有甲 = [行 for 行 in 行们 if 行.endswith("甲.txt")]
    动作 = {行.split("|", 1)[0] for 行 in 有甲}
    assert "增" in 动作, f"没收到「增」（收到的是 {动作}）：\n{全文[:800]}"
    assert "改" in 动作, f"没收到「改」（收到的是 {动作}）：\n{全文[:800]}"
    assert "删" in 动作, f"没收到「删」（收到的是 {动作}）：\n{全文[:800]}"


@_需要引擎
@_三平台
def test_递归能看到子目录(tmp_path):
    """`选项={"递归": 真}` 时，深一层的文件变化也要报出来。"""
    子 = tmp_path / "数据"
    深 = 子 / "子"
    深.mkdir(parents=True)
    进程 = _起监视(tmp_path, 脚本=_脚本.replace('"."', '"数据"'))
    time.sleep(1.5)

    (深 / "乙.txt").write_text("x", encoding="utf-8")
    time.sleep(0.5)

    事件, 码, 全文 = _收工(tmp_path, 进程)
    assert 码 == 0, f"引擎异常退出：\n{全文[:800]}"
    命中 = [行 for 行 in 事件 if "乙.txt" in 行]
    assert 命中, f"递归没看到子目录里的文件：\n{全文[:800]}"
    assert any(行.startswith("增") for 行 in 命中), f"动作不对：{命中}"


@_需要引擎
@_三平台
def test_改名有改名动作(tmp_path):
    """改名要有 `改名`（Windows / Linux）；macOS 的 FSEvents 允许拆成「删 + 增」。"""
    子 = tmp_path / "数据"
    子.mkdir()
    (子 / "旧名.txt").write_text("x", encoding="utf-8")
    进程 = _起监视(tmp_path, 脚本=_脚本.replace('"."', '"数据"'))
    time.sleep(1.5)

    os.replace(子 / "旧名.txt", 子 / "新名.txt")
    time.sleep(0.6)

    事件, 码, 全文 = _收工(tmp_path, 进程)
    assert 码 == 0, f"引擎异常退出：\n{全文[:800]}"
    文本 = "\n".join(事件)
    有改名 = any(行.startswith("改名") and "新名.txt" in 行 for 行 in 事件)
    有删 = any(行.startswith("删") and "旧名.txt" in 行 for 行 in 事件)
    有增 = any(行.startswith("增") and "新名.txt" in 行 for 行 in 事件)
    assert 有改名 or (有删 and 有增), f"改名没报出来：\n{文本[:800]}"


@_需要引擎
@_三平台
def test_没动静时超时给空(tmp_path):
    """`等事件(表, 0.3)` 在没人动目录时**返回 `空`**（超时不是错误）。

    📌 与 `进程.读行` 同一个口径 —— 到点没东西就给 `空`，不抛异常。
    """
    (tmp_path / "空目录").mkdir()
    脚本 = """导入 路径
令 表 = 路径.监视("空目录", 函数(项)：打印("不该有事件"))
打印("前")
令 结果 = 路径.等事件(表, 0.3)
打印("后", 类型(结果))
路径.停止监视(表)
"""
    文件 = tmp_path / f"超时_{os.getpid()}.jsh"
    文件.write_text(脚本, encoding="utf-8", newline="\n")
    r = subprocess.run([str(EXE), 文件.name], cwd=str(tmp_path),
                       capture_output=True, timeout=60)
    出 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode == 0, f"等事件超时报错了：\n{出[:600]}"
    assert "不该有事件" not in 出, f"空目录居然有事件：\n{出[:600]}"
    assert "后 空" in 出, f"超时没给 `空`：\n{出[:600]}"


@_需要引擎
@_三平台
def test_句柄方法形式也能用(tmp_path):
    """`表.等事件(0.2)` / `表.停止()` 与模块函数形式等价（句柄方法那条路）。"""
    (tmp_path / "空目录").mkdir()
    脚本 = """导入 路径
令 表 = 路径.监视("空目录", 函数(项)：打印("事件"))
令 a = 表.等事件(0.2)
打印("方法形式:", 类型(a))
打印("停:", 表.停止())
打印("再停:", 表.停止())
"""
    文件 = tmp_path / f"方法_{os.getpid()}.jsh"
    文件.write_text(脚本, encoding="utf-8", newline="\n")
    r = subprocess.run([str(EXE), 文件.name], cwd=str(tmp_path),
                       capture_output=True, timeout=60)
    出 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode == 0, f"句柄方法形式失败：\n{出[:600]}"
    assert "方法形式: 空" in 出, f"等事件没给空：\n{出[:600]}"
    assert "停: 真" in 出, f"停止没返回真：\n{出[:600]}"
    assert "再停: 假" in 出, f"重复停止应当返回假（幂等）：\n{出[:600]}"


@_需要引擎
@_三平台
@pytest.mark.parametrize(
    "脚本, 期望码, 期望话",
    [
        ('导入 路径\n路径.监视("绝对不存在的目录xyz", 函数(项)：打印(1))\n',
         "E2007", "不存在"),
        ('导入 路径\n导入 文件\n文件.写文本("是个文件.txt", "x")\n'
         '路径.监视("是个文件.txt", 函数(项)：打印(1))\n',
         "E2007", "不是目录"),
        ('导入 路径\n路径.监视(".", 123)\n', "E2002", "要一个**函数**"),
        ('导入 路径\n路径.监视(".", 函数(项)：打印(1), {"递归": "是"})\n',
         "E2002", "要 真/假"),
        ('导入 路径\n路径.监视(".", 函数(项)：打印(1), 5)\n', "E2002", "要字典"),
    ],
    ids=["目录不存在", "给的是文件", "回调不是函数", "递归给了文本", "选项给了整数"],
)
def test_参数写错要说人话(脚本, 期望码, 期望话, tmp_path):
    """参数错 → 有码、有位置、说清怎么改（与串口那边同一套口径）。"""
    文件 = tmp_path / f"参数_{os.getpid()}_{abs(hash(脚本)) % 1000}.jsh"
    文件.write_text(脚本, encoding="utf-8", newline="\n")
    r = subprocess.run([str(EXE), 文件.name], cwd=str(tmp_path),
                       capture_output=True, timeout=60)
    出 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode != 0, f"居然没报错：\n{出[:400]}"
    assert 期望码 in 出, f"错误码不对（期望 {期望码}）：\n{出[:400]}"
    assert 期望话 in 出, f"没说清原因（期望含「{期望话}」）：\n{出[:400]}"


@_需要引擎
def test_沙箱里导入路径被拒(tmp_path):
    """`--sandbox` 下 `导入 路径` 一律被拒 —— 监视跟着一起关掉，不用单独拦。

    🔴 这是**故意的**：沙箱给 wasm 页面 / playground 用，放行目录监视等于让
    网页能探测用户机器上有什么文件。`路径` 本来就在「文件系统访问」那一组里。
    """
    文件 = tmp_path / f"沙箱_{os.getpid()}.jsh"
    文件.write_text('导入 路径\n路径.监视(".", 函数(项)：打印(1))\n',
                    encoding="utf-8", newline="\n")
    assert 文件.is_file(), f"脚本没写出来：{文件}"
    r = subprocess.run([str(EXE), str(文件), "--sandbox"],
                       capture_output=True, timeout=60)
    出 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode != 0, f"沙箱里居然放行了：\n{出[:400]}"
    assert "沙箱" in 出 and "路径" in 出, f"拒了但话不对：\n{出[:400]}"


# ---------------------------------------------------------------------------
# ⑦ 超时的单位（**这条是给踩过的坑立的碑**）
# ---------------------------------------------------------------------------

@pytest.mark.计时
@_需要引擎
@_三平台
def test_整数超时按秒算(tmp_path):
    """`等事件(表, 1)` 等的是 **1 秒**，不是 1 毫秒。

    🔴 2026-11-26 踩过：超时换算把**整数**当毫秒、只有小数乘 1000，
    于是 `等事件(表, 5)` 只等 5ms 就返回 `空`。症状是「跨进程写的文件一条事件
    都收不到」，而用手写的独立小程序（等 2 秒）却正常 —— 查了很久才发现是单位。

    📌 打 `计时`：这条断言的是墙上耗时，并行负载会把它抬高（假红）。
    """
    (tmp_path / "空目录").mkdir()
    脚本 = """导入 路径
导入 时间
令 表 = 路径.监视("空目录", 函数(项)：打印("不该有事件"))
令 开始 = 时间.时间戳()
路径.等事件(表, 1)
令 用了 = 时间.时间戳() - 开始
打印("用时:", 用了)
路径.停止监视(表)
"""
    文件 = tmp_path / f"计时_{os.getpid()}.jsh"
    文件.write_text(脚本, encoding="utf-8", newline="\n")
    r = subprocess.run([str(EXE), str(文件)], cwd=str(tmp_path),
                       capture_output=True, timeout=60)
    出 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode == 0, f"跑挂了：\n{出[:600]}"
    行 = [ln for ln in 出.splitlines() if ln.startswith("用时:")]
    assert 行, f"没打印用时：\n{出[:600]}"
    秒 = float(行[0].split(":", 1)[1])
    assert 秒 >= 0.9, (
        f"`等事件(表, 1)` 只等了 {秒:.3f} 秒 —— 整数超时又被当成毫秒了"
        f"（正是 2026-11-26 那个 bug）")


@_需要引擎
def test_两处超时换算都走as_num():
    """静态护栏：`串口` 与 `路径` 的超时换算都必须走 `as_num`（统一按秒）。

    为什么要有这条：这两份 `超时毫秒` 是**各写一遍**的，2026-11-26 的「整数当毫秒」
    就是同时坏在两份里的。断言实现方式而不是行为，是因为串口那条要真硬件才能测。
    """
    根 = ROOT / "rust" / "src" / "stdlib"
    for 名 in ("serial.rs", "watch.rs"):
        源 = (根 / 名).read_text(encoding="utf-8")
        i = 源.find("fn 超时毫秒")
        assert i > 0, f"{名} 里找不到 超时毫秒（改名了？）"
        段 = 源[i:i + 900]
        assert "as_num" in 段, (
            f"{名} 的 超时毫秒 没走 `as_num` —— 整数会被当成毫秒"
            "（2026-11-26 那个 bug 的复发点）")
