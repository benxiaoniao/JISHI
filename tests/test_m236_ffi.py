# -*- coding: utf-8 -*-
"""M236 · `外部`（FFI）：调用动态库 —— **主路径的逃生口**。

## 先说清一件事：默认没有沙箱

基石是**全平台语言**，正常使用时**根本没有沙箱**。`--sandbox` 是一个**显式开关**
（给 playground / 托管执行那种「不信任代码」的场景用的），wasm 页面是另一条物理上
不存在的路径（浏览器里没有动态库这种东西）。

| 路径 | `外部` |
|---|---|
| **主路径**（命令行 / 桌面 / 服务）—— **默认就是这个** | ✅ 完全可用 |
| `--sandbox`（显式开） | ❌ 明确拒绝（没有开关能放行：FFI 是绕过一切限制的总闸） |
| wasm 页面 | ❌ 明确报错（页面里没有动态库，不是「沙箱限制」） |

📌 这条对应 D130：「**能力上限 ≠ 沙箱边界**」。别把「沙箱里不行」当成「语言不支持」，
也别把沙箱当默认 —— 判据 `test_默认路径不带沙箱就能用` 钉的就是这件事。

## 判据为什么只用**无参小函数**

`MessageBoxW` 会弹窗（CI 上必挂），所以换成三组「结果可验证」的：

| 平台 | 库 | 验什么 |
|---|---|---|
| Windows | `kernel32.dll` | `GetTickCount64`（无参→整数）、`GetCurrentProcessId`（与 `进程.进程号()` 对拍）、`lstrlenW`（UTF-16）/ `lstrlenA`（UTF-8） |
| Linux | `libc.so.6` | `getpid`（与 `进程.进程号()` 对拍）、`strlen`（UTF-8 字节数） |
| macOS | `/usr/lib/libSystem.B.dylib` | 同上 |

⚠️ **`getpid` 与 `进程.进程号()` 相等**是最强的交叉验证：一个走 FFI、一个走
宿主自己的系统调用，两条独立路径给出同一个数 —— 这才叫「真的调到了」。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.conftest import rust_exe_path  # noqa: E402

RUST_EXE = rust_exe_path()
_需要引擎 = pytest.mark.skipif(not RUST_EXE.is_file(),
                              reason="没有 Rust 发行版（cargo build --release）")

# 各平台的「库 + 取进程号 + 量文本长度」三件套
if sys.platform.startswith("win"):
    库 = "kernel32.dll"
    取号 = "GetCurrentProcessId"
    量长 = ("lstrlenW", "文本", 2)      # UTF-16：两个码元
    量长UTF8 = ("lstrlenA", "文本UTF8", 6)   # 六个字节
elif sys.platform == "darwin":
    库 = "/usr/lib/libSystem.B.dylib"
    取号 = "getpid"
    量长 = ("strlen", "文本UTF8", 6)
    量长UTF8 = 量长
else:
    库 = "libc.so.6"
    取号 = "getpid"
    量长 = ("strlen", "文本UTF8", 6)
    量长UTF8 = 量长


def _跑(源: str, tmp: Path, 名: str, 沙箱: bool = False) -> str:
    文件 = tmp / f"{名}-{os.getpid()}.jsh"
    文件.write_text(源, encoding="utf-8", newline="\n")
    参数 = [str(RUST_EXE), str(文件)] + (["--sandbox"] if 沙箱 else [])
    r = subprocess.run(参数, cwd=str(tmp), capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    return (r.stdout or "").replace("\r\n", "\n")


# ---------------------------------------------------------------------------
# 一、主路径真的能用（**不带沙箱**）—— 用户点名要的
# ---------------------------------------------------------------------------

@_需要引擎
def test_默认路径不带沙箱就能用(tmp_path):
    """不打 `--sandbox` 时 `导入 外部` 直接可用，且真的调到了原生函数。

    🔴 这条是给「基石是全平台语言、正常使用时没有沙箱」立的碑（D130 / D137）：
    `外部` 是**主路径能力**，只在显式 `--sandbox` 与 wasm 页面上降级。
    """
    源 = f'''导入 外部
令 库 = 外部.加载("{库}")
令 号 = 外部.调用(库, "{取号}", [], [])
打印("类型", 类型(号), "正数", 号 > 0)
外部.释放(库)
'''
    出 = _跑(源, tmp_path, "ffi_default")
    assert 出.strip() == "类型 整数 正数 真", 出


@_需要引擎
def test_FFI拿到的进程号与宿主一致(tmp_path):
    """`getpid` / `GetCurrentProcessId` 经 FFI 拿到的号 == `进程.进程号()`。

    两条**互相独立**的路径给同一个数，才算「真的调到了原生函数」
    （不然可能只是我们返回了一个编出来的 0）。
    """
    源 = f'''导入 外部
导入 进程
令 库 = 外部.加载("{库}")
令 号 = 外部.调用(库, "{取号}", [], [])
打印(号, 进程.进程号(), 号 == 进程.进程号())
外部.释放(库)
'''
    出 = _跑(源, tmp_path, "ffi_pid").strip().split()
    assert len(出) == 3, 出
    assert 出[2] == "真", f"FFI 与宿主的进程号不一致：{出}"


@_需要引擎
def test_文本按平台原生与UTF8两种传法(tmp_path):
    """`"文本"` 是平台原生（Win32 UTF-16 / POSIX UTF-8），`"文本UTF8"` 一律 UTF-8。

    「你好」= 2 个 UTF-16 码元 / 6 个 UTF-8 字节 —— 两个数不同，才验得出
    两种传法真的不一样（都返回 6 就说明 `"文本"` 在 Windows 上没用宽字符）。
    """
    名, 类型名, 期望 = 量长
    源 = f'''导入 外部
令 库 = 外部.加载("{库}")
打印(外部.调用(库, "{名}", ["{类型名}"], ["你好"]))
外部.释放(库)
'''
    出 = _跑(源, tmp_path, "ffi_text")
    assert 出.strip() == str(期望), f"「{名}」给「{出.strip()}」，期望 {期望}"


@_需要引擎
def test_UTF8传法在哪个平台都是字节数(tmp_path):
    名, 类型名, 期望 = 量长UTF8
    源 = f'''导入 外部
令 库 = 外部.加载("{库}")
打印(外部.调用(库, "{名}", ["{类型名}"], ["你好"]))
打印(外部.调用(库, "{名}", ["{类型名}"], [""]))
外部.释放(库)
'''
    出 = _跑(源, tmp_path, "ffi_utf8").strip().splitlines()
    assert 出[0] == str(期望), 出
    assert 出[1] == "0", f"空串的长度该是 0：{出}"


# ---------------------------------------------------------------------------
# 二、内存块：分配 / 写读 / 越界 / 释放
# ---------------------------------------------------------------------------

@_需要引擎
def test_内存块读写与释放(tmp_path):
    源 = '''导入 外部
令 块 = 外部.分配(32)
外部.写整数(块, 0, 12345)
外部.写整数(块, 8, 999, 4)
打印(外部.读整数(块, 0), 外部.读整数(块, 8, 4))
块.写整数(16, 7, 2)
打印(块.读整数(16, 2))
打印(外部.释放块(块), 外部.释放块(块))
'''
    出 = _跑(源, tmp_path, "ffi_mem").strip().splitlines()
    assert 出 == ["12345 999", "7", "真 假"], 出


@_需要引擎
def test_越界读写当场报错而不是悄悄踩内存(tmp_path):
    """块只有 8 字节，从偏移 4 写 8 字节 ⇒ 报错（不是写坏邻居的内存）。"""
    源 = '''导入 外部
令 块 = 外部.分配(8)
尝试：
    外部.写整数(块, 4, 1, 8)
    打印("居然通过了")
捕获 异常 为 e：
    打印("拒绝", e.类型)
'''
    出 = _跑(源, tmp_path, "ffi_oob")
    assert 出.strip() == "拒绝 值错误", 出


# ---------------------------------------------------------------------------
# 三、错误路径：每一条都要**报错，不许崩**
# ---------------------------------------------------------------------------

@_需要引擎
def test_错误路径全部报错且不崩溃(tmp_path):
    """八种错法逐条验 —— 这是 FFI 模块唯一能替用户兜住的东西。

    🔴 崩了的话这条用例**跑不到断言**（子进程 exit code 是负数/异常），
    所以「跑完并给出八行」本身就是「没崩」的证明。
    """
    源 = f'''导入 外部

函数 试(标签, f)：
    尝试：
        f()
        打印(标签, "通过了")
    捕获 异常 为 e：
        打印(标签, e.类型)

函数 坏库()：
    外部.加载("根本没有这个库.dll")

函数 坏函数()：
    令 库 = 外部.加载("{库}")
    外部.调用(库, "没有这个函数XYZ", [], [])

函数 个数不配()：
    令 库 = 外部.加载("{库}")
    外部.调用(库, "{取号}", ["整数", "整数"], [1])

函数 类型名错()：
    令 库 = 外部.加载("{库}")
    外部.调用(库, "{取号}", ["int"], [])

函数 浮点参数()：
    令 库 = 外部.加载("{库}")
    外部.调用(库, "{取号}", ["小数"], [1])

函数 不是句柄()：
    外部.调用("{库}", "{取号}", [], [])

函数 块越界()：
    令 块 = 外部.分配(8)
    外部.写整数(块, 4, 1, 8)

函数 释放后再调()：
    令 库 = 外部.加载("{库}")
    外部.释放(库)
    外部.调用(库, "{取号}", [], [])

试("库不存在", 坏库)
试("函数不存在", 坏函数)
试("个数不配", 个数不配)
试("类型名错", 类型名错)
试("浮点参数", 浮点参数)
试("不是句柄", 不是句柄)
试("块越界", 块越界)
试("释放后再调", 释放后再调)
'''
    出 = _跑(源, tmp_path, "ffi_errs").strip().splitlines()
    assert len(出) == 8, f"该有八行（每行都该报错）：{出}"
    for 行 in 出:
        assert "通过了" not in 行, f"这条不该通过：{行}"
    # 错误类型也钉住：这是「说得清哪儿错」的证据
    期望 = {
        "库不存在": "文件错误",
        "函数不存在": "运行期错误",
        "个数不配": "值错误",
        "类型名错": "值错误",
        "浮点参数": "值错误",
        "不是句柄": "类型错误",
        "块越界": "值错误",
        "释放后再调": "运行期错误",
    }
    for 行 in 出:
        标签, 类型 = 行.split()
        assert 类型 == 期望[标签], f"{标签}：期望 {期望[标签]}，实际 {类型}"


# ---------------------------------------------------------------------------
# 四、沙箱与页面：降级要**说清原因**，且两宿主逐字一致
# ---------------------------------------------------------------------------

@_需要引擎
def test_沙箱里拒绝且两宿主逐字一致(tmp_path):
    """`--sandbox` 下 `导入 外部` 被拒 —— 而且**没有开关能放行**。

    🔴 与「网络/系统」那种「给个开关」不同：FFI 能读文件、连网、执行命令，
    是绕过沙箱一切限制的总闸，所以它不属于「可开关」那一类。
    ⚠️ 但这条**只影响显式开沙箱的路径** —— 主路径完全不受影响（见上面第一条）。
    """
    文件 = tmp_path / f"ffi_sbox-{os.getpid()}.jsh"
    文件.write_text('导入 外部\n打印(1)\n', encoding="utf-8", newline="\n")
    a = subprocess.run([str(RUST_EXE), str(文件), "--sandbox"], cwd=str(tmp_path),
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    b = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", str(文件), "--sandbox"],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    # 沙箱路径是「输出 protocol JSON + 退出码 1」（`ok:false`），不是崩溃
    assert a.returncode == 1 and b.returncode == 1, (
        f"沙箱路径该是「正常给出 ok:false + 退出码 1」，实际 {a.returncode} / {b.returncode}")
    assert "沙箱禁止导入「外部」" in a.stdout, a.stdout
    # ⚠️ 不能整串比：`duration_ms` 天然不同（一个 0.8ms、一个 20ms）。
    #    **报错本身要逐字一致** —— 这才是「两边说法一样」的含义。
    import json
    甲 = json.loads(a.stdout)["error"]
    乙 = json.loads(b.stdout)["error"]
    assert 甲 == 乙, f"两宿主的沙箱报错不一致：\nRust: {甲}\nPy:   {乙}"


@_需要引擎
def test_模块与函数都在表里():
    """模块名 `外部`、七个函数一个不少（改签名/改名要同步卡与文档）。"""
    r = subprocess.run([str(RUST_EXE), "--dump-stdlib"],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0
    import json
    表 = json.loads(r.stdout)
    assert "外部" in 表, "`外部` 没注册进标准库表"
    有 = set(表["外部"])
    要 = {"加载", "调用", "释放", "分配", "写整数", "读整数", "释放块"}
    assert 要 <= 有, f"缺函数：{sorted(要 - 有)}"
