# -*- coding: utf-8 -*-
"""M235 · 0.3.7 反馈清单里被连挪两次的三条欠账（D8 / W18 / D10）。

## 这一站为什么存在

路线图 §16.3 拍板时算的是「性能站后移成 M234，收口成 M235」；正文按 M225–M238
排站之后，收口落在 M238，**M235 成了空号**。按「不跳号」把反馈清单里
**被挪过两次、至今没做**的三条填进来：

| 条目 | 谁撞的 | 原来怎么坏 |
|---|---|---|
| **D8** 包内模块顺序 | 桌面营造 | 多个 `.jsh` 按**文件名码点序**拼接 ⇒ 「文件名序 = 依赖序」成了隐含要求：`乙.jsh`(U+4E59) 排在 `甲.jsh`(U+7532) **前面**，`乙` 顶层用 `甲` 的常量就报「找不到这个名字」，**而且提示指向完全错误的方向** |
| **W18** 头值非 ASCII | 营造 + 桌面营造 | `构造响应` 的头值带中文报 E2002，只能把中文塞进正文让前端拆 |
| **D10** 鸭子类型 | 桌面营造 | `字典.甲` 报「没有方法「甲」」，提示只说「可用的方法：…」，**不说该怎么读字段** |

## D8 的两个真相（都实测过，别再猜）

1. **「报错指错文件」那一半早就好了**（M199 段表 + M220 ⑧）：包内语法错报的是
   `乙.jsh:5`（文件内行号），不是拼接后的全局行号。这次只需修**顺序**那一半。
2. **本地包有三条独立实现**，改一处不够（这次踩的）：
   - `frontend/src/localpkg.rs` —— **Rust 前端/发行版**（`jishi-rs.exe` 走这条）
   - `oracle/jishi/localpkg.py` —— Python 前端的**编译期**展开
   - `oracle/jishi/runtime.py::_import_local_package` —— **树遍历/解释执行**
     （CLI 默认路径！`Interpreter` 只展开基石库，本地包靠运行期导入）
   ⇒ 只改第一条，`jishi-rs` 绿而 CLI 红（反之亦然）。

## 排序的口径（写死在这里，免得以后「顺手」「也把函数体算进来」）

**只算顶层语句的跨文件引用**。函数体是**延迟求值**的，`乙.jsh` 里
`函数 丁()：返回 丙()` 不要求 `丙` 的定义文件排在前面。保守地把函数体也算进来
会**平白造出环**（两个文件互相调用函数），于是拓扑失败、退回文件名序 —— 等于没改。
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


def _跑两宿主(源: str, tmp: Path, 名: str) -> tuple[str, str]:
    """树遍历（CLI 默认）与 Rust 发行版各跑一遍 → 两份输出。

    ⚠️ D8 必须**两条都测**：它们是三份独立实现里的两份，只测一边会漏。
    """
    文件 = tmp / f"{名}-{os.getpid()}.jsh"
    文件.write_text(源, encoding="utf-8", newline="\n")
    a = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", str(文件)],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", timeout=300)
    b = subprocess.run([str(RUST_EXE), str(文件)], cwd=str(tmp),
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert a.returncode == 0, "树遍历：" + (a.stdout or "") + (a.stderr or "")
    assert b.returncode == 0, "Rust：" + (b.stdout or "") + (b.stderr or "")
    return ((a.stdout or "").replace("\r\n", "\n"),
            (b.stdout or "").replace("\r\n", "\n"))


def _建包(tmp: Path, 包名: str, 文件们: dict, 元: dict | None = None) -> None:
    """在 `<tmp>/.jishi/packages/<包名>/` 下铺一个包。"""
    目录 = tmp / ".jishi" / "packages" / 包名
    目录.mkdir(parents=True, exist_ok=True)
    元 = 元 or {"名字": 包名, "版本": "0.1.0"}
    import json
    (目录 / "包.json").write_text(json.dumps(元, ensure_ascii=False),
                                 encoding="utf-8")
    for 名, 内容 in 文件们.items():
        (目录 / 名).write_text(内容, encoding="utf-8", newline="\n")


def _跑错(源: str, tmp: Path, 名: str) -> tuple[str, str]:
    """跑一个**应该失败**的程序，返回两边的 stderr（都不许 0 退出）。"""
    文件 = tmp / f"{名}-{os.getpid()}.jsh"
    文件.write_text(源, encoding="utf-8", newline="\n")
    a = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", str(文件)],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", timeout=300)
    b = subprocess.run([str(RUST_EXE), str(文件)], cwd=str(tmp),
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert a.returncode != 0, "树遍历居然没报错：" + (a.stdout or "")
    assert b.returncode != 0, "Rust 居然没报错：" + (b.stdout or "")
    return ((a.stderr or "").replace("\r\n", "\n"),
            (b.stderr or "").replace("\r\n", "\n"))


# ---------------------------------------------------------------------------
# 一、D8：包内模块按顶层依赖排序
# ---------------------------------------------------------------------------

@_需要引擎
def test_包内文件按依赖排序_不用管文件名(tmp_path):
    """`乙.jsh` 顶层用 `甲.jsh` 的常量 —— 文件名序反着也**不许**断。

    🔴 这是 D8 的原始症状：`乙`(U+4E59) < `甲`(U+7532)，纯文件名排序会把 `乙`
    排在前面，于是 `令 加倍 = 基数 * 2` 在 `基数` 定义之前执行。旧报错还是
    「名字「基数」在赋值之前被用到了」+「被赋过值所以它是局部变量」——
    把人带向完全错误的方向（以为是作用域问题，其实只是文件顺序）。
    """
    _建包(tmp_path, "试包", {
        "甲.jsh": "令 基数 = 100\n函数 丙()：\n    返回 基数\n",
        "乙.jsh": "令 加倍 = 基数 * 2\n函数 丁()：\n    返回 加倍\n",
    })
    源 = '导入 试包 从 本地包\n打印(试包.丁())\n'
    遍历, rust = _跑两宿主(源, tmp_path, "pkg_order")
    assert 遍历 == "200\n", f"树遍历给了「{遍历}」—— 包内顺序没修好"
    assert rust == "200\n", f"Rust 给了「{rust}」—— Rust 侧的 按依赖排序 没生效"


@_需要引擎
def test_三文件依赖链也要排对(tmp_path):
    """`一 → 二 → 三` 的链条，而文件名序是 `一, 三, 二`（码点序）。

    三文件才验得出「拓扑排序」而不只是「两两交换」。
    """
    _建包(tmp_path, "链", {
        "一.jsh": "令 系数 = 3\n",
        "二.jsh": "令 中间 = 系数 * 10\n",
        "三.jsh": "令 结果 = 中间 + 7\n函数 算()：\n    返回 结果\n",
    })
    源 = '导入 链 从 本地包\n打印(链.算())\n'
    遍历, rust = _跑两宿主(源, tmp_path, "pkg_chain")
    assert 遍历 == "37\n", f"树遍历给「{遍历}」（要 3*10+7=37）"
    assert rust == "37\n", f"Rust 给「{rust}」"


@_需要引擎
def test_互相递归的函数不会被当成环(tmp_path):
    """两个文件里的函数**互相调用** —— 不许因此退回文件名序、更不许报循环依赖。

    这一条守着排序的**口径**：函数体是延迟求值的，里面的引用**不算**跨文件顺序
    约束。要是简化成「扫整棵树的名字」，这里就会成环 ⇒ 退回文件名序 ⇒ 断。
    """
    _建包(tmp_path, "互", {
        "甲.jsh": ("函数 甲函数(n)：\n"
                   "    如果 n <= 0：\n        返回 0\n"
                   "    返回 乙函数(n - 1) + 1\n"),
        "乙.jsh": ("函数 乙函数(n)：\n"
                   "    如果 n <= 0：\n        返回 100\n"
                   "    返回 甲函数(n - 1)\n"),
    })
    源 = '导入 互 从 本地包\n打印(互.甲函数(3))\n打印(互.乙函数(2))\n'
    遍历, rust = _跑两宿主(源, tmp_path, "pkg_mutual")
    assert 遍历 == "102\n101\n", f"树遍历给「{遍历}」"
    assert rust == "102\n101\n", f"Rust 给「{rust}」"


@_需要引擎
def test_顶层互相引用还是要报错(tmp_path):
    """真·循环（两边**顶层**互相引用）⇒ 退回文件名序，照旧报 E0301。

    排序**不吞错**：这种包本来就是错的，报出具体位置比在排序阶段猜一句
    「有循环依赖」有用得多。这条守的是「环就原样返回」那个分支。
    """
    _建包(tmp_path, "环", {
        "甲.jsh": "令 左 = 右 + 1\n",
        "乙.jsh": "令 右 = 左 + 1\n",
    })
    源 = '导入 环 从 本地包\n打印(1)\n'
    遍历, rust = _跑错(源, tmp_path, "pkg_cycle")
    assert "E0301" in 遍历, f"树遍历没报找不到名字：\n{遍历[:300]}"
    assert "E0301" in rust, f"Rust 没报找不到名字：\n{rust[:300]}"


@_需要引擎
def test_声明入口的包不受排序影响(tmp_path):
    """`包.json` 有 `入口` ⇒ 只编那一个文件，排序逻辑根本不参与。"""
    _建包(tmp_path, "有入口", {
        "主.jsh": "令 值 = 42\n函数 取()：\n    返回 值\n",
        "内部.jsh": "令 垃圾 = 1 / 0\n",       # 不编它 ⇒ 不许炸
    }, 元={"名字": "有入口", "版本": "0.1.0", "入口": "主"})
    源 = '导入 有入口 从 本地包\n打印(有入口.取())\n'
    遍历, rust = _跑两宿主(源, tmp_path, "pkg_entry")
    assert 遍历 == "42\n", f"树遍历给「{遍历}」"
    assert rust == "42\n", f"Rust 给「{rust}」"


# ---------------------------------------------------------------------------
# 二、W18：响应头值支持非 ASCII（头名仍限 ASCII token）
# ---------------------------------------------------------------------------

@_需要引擎
def test_中文头值能发出去_而且是UTF8字节(tmp_path):
    """头值写中文不许报错，**而且发出去的真的是 UTF-8 字节**（不是 latin-1）。

    ⚠️ 只断言「文本里有中文」是不够的 —— 那条在 Python 侧永远为真（Python 字符串
    本来就是 Unicode）。所以让脚本把响应**写进文件**，判据读**原始字节**。
    """
    源 = '''导入 HTTP服务端 为 服
导入 文件
令 响 = 服.构造响应(200, "好", "text/plain; charset=utf-8", {"X-Tip": "余额不足"})
文件.写文本("出.txt", 响)
'''
    遍历, rust = _跑两宿主(源, tmp_path, "w18")
    assert 遍历 == rust == "", f"输出不为空：{遍历!r} / {rust!r}"
    字节 = (tmp_path / "出.txt").read_bytes()
    期望 = "X-Tip: 余额不足".encode("utf-8")
    assert 期望 in 字节, (
        "响应头里没有 UTF-8 编码的「X-Tip: 余额不足」——\n"
        f"  期望字节：{期望!r}\n  实际：{字节[:200]!r}")


@_需要引擎
def test_头值里的换行被拒(tmp_path):
    """🔴 **响应头注入**：头值里塞 `\\r\\n` 能伪造任意响应头，必须拒绝。

    旧代码是 `!值.is_ascii()` —— 而 `\\r`/`\\n` **本身就在 ASCII 范围里**，
    于是这个洞一直在（`X-甲: 好\\r\\nSet-Cookie: 偷=1` 原样飞出去）。
    """
    源 = '''导入 HTTP服务端 为 服
尝试：
    令 响 = 服.构造响应(200, "好", "text/plain", {"X-甲": "好\\r\\nSet-Cookie: 偷=1"})
    打印("居然通过了")
捕获 异常 为 e：
    打印("拒绝")
'''
    遍历, rust = _跑两宿主(源, tmp_path, "w18_inject")
    assert 遍历 == rust == "拒绝\n", f"换行没被拒：{遍历!r} / {rust!r}"


@_需要引擎
def test_头名必须是ASCII且不含空格(tmp_path):
    """头名仍是 `tchar`：中文 / 空格 / 空串一律拒（中文请放头**值**里）。"""
    源 = '''导入 HTTP服务端 为 服

函数 试(标签, 头)：
    尝试：
        服.构造响应(200, "好", "text/plain", 头)
        打印(标签, "通过")
    捕获 异常 为 e：
        打印(标签, "拒绝")

试("中文名", {"提示": "值"})
试("带空格", {"X 甲": "值"})
试("空名字", {"": "值"})
试("正常名", {"X-Tip": "值"})
'''
    遍历, rust = _跑两宿主(源, tmp_path, "w18_name")
    assert 遍历 == rust, f"两宿主不一致：\n{遍历}\n---\n{rust}"
    assert 遍历.splitlines() == ["中文名 拒绝", "带空格 拒绝", "空名字 拒绝", "正常名 通过"], 遍历


# ---------------------------------------------------------------------------
# 三、D10：`.` 是方法不是字段 —— 报错必须说清怎么读字段
# ---------------------------------------------------------------------------

@_需要引擎
def test_字典属性读取的提示说清怎么办(tmp_path):
    """`字典.甲` 报错时，hint 里必须告诉用户**读字段用下标**、容错用 `获取`。

    D10 的真相：语言能力早就够了（`获取(键, 默认)` / `包含(键)`），
    缺的是「用户不知道」—— 而用户看到的是「没有方法「甲」」+「可用的方法：…」，
    只会以为拼错了方法名，永远想不到该用 `字典["甲"]`。

    ⚠️ 这条**同时对拍两宿主的 hint 逐字**：文案差一个字都算不一致（M55 起的老规矩）。
    """
    源 = '令 d = {"甲": 1}\n打印(d.甲)\n'
    遍历, rust = _跑错(源, tmp_path, "d10")
    assert 遍历 == rust, f"两宿主的报错不一致：\n--- 树遍历 ---\n{遍历}\n--- Rust ---\n{rust}"
    assert "没有方法「甲」" in rust, rust[:300]
    assert "读字段用下标" in rust, (
        "提示里没有「读字段用下标」—— D10 又退回去了：\n" + rust[:400])
    assert "获取" in rust and "包含" in rust, (
        "提示里没提 `获取` / `包含`（鸭子类型的两件套）：\n" + rust[:400])


@_需要引擎
def test_获取与包含就是鸭子类型的两件套(tmp_path):
    """把「语言能力已经够」这句话钉成判据，免得以后有人又去加 `有属性` 别名。

    - `字典.包含(键)` → 真/假，用来判「有没有这个字段」；
    - `字典.获取(键, 默认)` → 有就给值、没有给默认，用来「容错读」；
    - `字典["键"]` 在键不在时**报错**（这是对的，别改）。
    """
    源 = '''令 d = {"甲": 1}
令 有甲 = d.包含("甲")
令 有乙 = d.包含("乙")
令 读乙 = d.获取("乙", "默认值")
打印(有甲, 有乙, 读乙)
'''
    遍历, rust = _跑两宿主(源, tmp_path, "d10_duck")
    assert 遍历 == rust, f"两宿主不一致：\n{遍历}\n---\n{rust}"
    assert 遍历.strip() == "真 假 默认值", 遍历
