# -*- coding: utf-8 -*-
"""M160 · 解压安全：一个根因，两处修补。

## 根因

解压的写法是「拿 zip 里的**条目名**当相对路径，拼到目标目录后面」。
条目名是**包里写的**，不是用户写的 —— 一个 `../../.ssh/authorized_keys`
就能写到目标目录之外。这是包管理器里最经典的一类漏洞（PyPI 上出现过多次）。

## 修法（三处、一套规则）

| 位置 | 现在 |
|---|---|
| `压缩.解压`（Python / Rust / Node） | 解压前逐条校验 + 算解开总量 |
| 包安装 `install_package_zip`（Rust） | **复用同一个校验函数**（别写两遍，两遍迟早分叉） |

配套：解开总量 > 512 MB 就**不解**，提示用 `压缩.解压成员` 挑着取（压缩炸弹那条）。

## 为什么不是「静默改名」

Python 的 `zipfile.extractall` 本来就会把 `../` 规范化掉（不会真穿越），
但那是**默默把文件放到别处** —— 用户以为解对了，其实内容散在别的地方，
比报错更难查；而 Rust / Node 压根没拦。三个宿主三种行为，正是本项目最忌的分叉。
⇒ 统一成「**显式拒绝 + 说清是哪个条目**」。

## 判据

- 五个执行器对同一个恶意包**报同一句话**；
- 目标目录之外**一个文件都没多**；
- 合法包照常能解（别为了安全把正常用法弄残）。
"""

import os
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                            # noqa: E402

CVM驱动 = '''\
import sys
sys.path.insert(0, r"{root}")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(newline="\\n")
    except Exception:
        pass
from oracle.jishi import cvm_bind, errors as E

src = open("程序.jsh", encoding="utf-8").read()
try:
    cvm_bind.run_source_c(src, filename="程序.jsh")
except E.JishiError as e:
    e.with_source(src.replace("\\r\\n", "\\n").split("\\n"))
    sys.stderr.write(e.render() + "\\n")
    sys.exit(1)
'''

#: 上限（与三处实现里的常量一致；测试**只核对文案里的数字**，不重复维护阈值逻辑）
上限MB = 512


def _五份(源码: str, tmp_path: Path) -> dict:
    """把同一段程序交给五个执行器，返回 {引擎: 输出文本}。"""
    from oracle.jishi import serialize
    from oracle.jishi.compiler import compile_source

    (tmp_path / "程序.jsh").write_text(源码, encoding="utf-8", newline="\n")
    (tmp_path / "驱动.py").write_text(
        CVM驱动.format(root=str(ROOT).replace("\\", "\\\\")), encoding="utf-8")
    表 = {
        "树遍历": [sys.executable, "-m", "oracle.jishi.cli", "--tree", "程序.jsh"],
        "Python VM": [sys.executable, "-m", "oracle.jishi.cli", "程序.jsh"],
        "C VM": [sys.executable, "驱动.py"],
    }
    rs = rust_exe_path()
    if rs.is_file():
        表["Rust 宿主"] = [str(rs), "程序.jsh"]
    node = shutil.which("node")
    if node:
        (tmp_path / "程序.json").write_text(
            serialize.dumps(compile_source(源码, "程序.jsh")), encoding="utf-8")
        表["Node 宿主"] = [node, str(ROOT / "oracle" / "node" / "index.js"),
                           "程序.json"]

    出 = {}
    for 名字, 命令 in 表.items():
        r = subprocess.run(命令, cwd=str(tmp_path), capture_output=True,
                           timeout=180, input=b"")
        文 = (r.stdout + r.stderr).decode("utf-8", "replace")
        出[名字] = 文
    return 出


def _首行(文: str) -> str:
    """只取「错误 E2000：运行期错误（…）」那一行 —— 位置框各引擎可能排版不同。"""
    for line in 文.splitlines():
        line = line.strip()
        if line.startswith("错误 "):
            return line
    return (文.strip().splitlines() or [""])[0].strip()


def _造包(目录: Path, 条目们: dict, 名: str = "包.zip") -> Path:
    p = 目录 / 名
    with zipfile.ZipFile(p, "w") as z:
        for 名_, 内容 in 条目们.items():
            z.writestr(名_, 内容)
    return p


# ---------------------------------------------------------------------------
# 一、恶意条目：五个执行器报同一句话，且目标目录外不留东西
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("条目", [
    "../逃出.txt",
    "子目录/../../逃出.txt",
    "../../双层.txt",
    "/绝对.txt",
    "C:/盘符.txt",
    "反斜杠\\..\\逃出.txt",
])
def test_恶意条目被拒_五引擎逐字一致(条目, tmp_path):
    包 = _造包(tmp_path, {"正常.txt": "ok\n", 条目: "pwn\n"})
    目标 = tmp_path / "目标"
    源 = (
        "导入 压缩\n"
        f'压缩.解压("{包.as_posix()}", "{目标.as_posix()}")\n'
        '打印("解完了")\n')
    出 = _五份(源, tmp_path)

    # ① 每个执行器都要失败
    for 引擎, 文 in 出.items():
        assert "解完了" not in 文, f"{引擎} 竟然解成功了：{文[:200]}"
        assert "不合法" in 文, f"{引擎} 没报「不合法」：{文[:200]}"

    # ② 五个执行器报**同一句话**（分叉就要在这条上红）
    首们 = {引擎: _首行(文) for 引擎, 文 in 出.items()}
    第一 = next(iter(首们.values()))
    for 引擎, 首 in 首们.items():
        assert 首 == 第一, f"{引擎} 的说法和别家不一样：\n  它={首}\n  别家={第一}"

    # ③ 最硬的一条：目标目录之外**没有**新文件
    逃出 = [p for p in tmp_path.rglob("*")
            if p.is_file() and p.name not in ("程序.jsh", "程序.json", "驱动.py",
                                              "包.zip")]
    assert 逃出 == [], f"弹到包外面了：{[str(p) for p in 逃出]}"
    assert not (tmp_path / "逃出.txt").exists()


def test_正常包照常能解(tmp_path):
    """别为了安全把正常用法弄残 —— 合法条目要能解出内容。"""
    包 = _造包(tmp_path, {"a.txt": "甲\n", "子/b.txt": "乙\n"})
    目标 = tmp_path / "出"
    源 = (
        "导入 压缩\n"
        "导入 文件\n"
        f'压缩.解压("{包.as_posix()}", "{目标.as_posix()}")\n'
        f'打印(文件.读文本("{(目标 / "a.txt").as_posix()}").去空白())\n'
        f'打印(文件.读文本("{(目标 / "子" / "b.txt").as_posix()}").去空白())\n')
    出 = _五份(源, tmp_path)
    for 引擎, 文 in 出.items():
        assert "甲" in 文 and "乙" in 文, f"{引擎} 没解出内容：{文[:300]}"


# ---------------------------------------------------------------------------
# 二、压缩炸弹：把中央目录里的「解压后大小」改大，不真造 512 MB
# ---------------------------------------------------------------------------

def _把解压后大小改大(zip_path: Path, 字节数: int) -> None:
    """找到中央目录条目（`PK\\x01\\x02`），把 usize 字段改成指定值。

    zip 中央目录条目（从签名算起）：签名 4 + 版本 4 + 标志 2 + 方法 2 +
    时间 2 + 日期 2 + crc 4 + 压缩后 4 + **解压后 4**（偏移 24）。
    这样文件仍然只有几百字节，但「声称解开后是 520 MB」——
    用来测**解压前**的总量判断，不会真去解。
    """
    b = bytearray(zip_path.read_bytes())
    起点 = b.find(b"PK\x01\x02")
    assert 起点 >= 0, "调试用的小包本身不是合法 zip"
    struct.pack_into("<I", b, 起点 + 24, 字节数)
    zip_path.write_bytes(bytes(b))


def test_炸弹包在解压前就被拦(tmp_path):
    """520 MB 的「声称大小」⇒ 报总量、提示用 `解压成员`，且**不建目标目录**。"""
    包 = _造包(tmp_path, {"大.txt": "小\n"})
    _把解压后大小改大(包, 520 * 1024 * 1024)
    目标 = tmp_path / "目标"
    源 = (
        "导入 压缩\n"
        f'压缩.解压("{包.as_posix()}", "{目标.as_posix()}")\n'
        '打印("解完了")\n')
    出 = _五份(源, tmp_path)
    for 引擎, 文 in 出.items():
        assert "解完了" not in 文, f"{引擎} 竟然解了：{文[:200]}"
        assert f"超过上限 {上限MB} MB" in 文, f"{引擎} 没说上限：{文[:300]}"
        assert "解压成员" in 文, f"{引擎} 没给出路：{文[:300]}"
    assert not 目标.exists(), "报错了却还是把目录建出来了"


# ---------------------------------------------------------------------------
# 三、包安装：同一个校验，另一处生效
# ---------------------------------------------------------------------------

def test_包安装拒绝恶意包(tmp_path):
    """包安装走的是同一套校验 —— 恶意包装不进来，且报错说清是哪个条目。"""
    包 = _造包(tmp_path, {
        "坏包/包.json": '{"名字": "坏包", "版本": "1.0.0", "描述": "探针"}',
        "坏包/主.jsh": '打印("我是坏包")\n',
        "坏包/../../逃出.txt": "pwn\n",
    }, 名="坏包.zip")
    家 = tmp_path / "家"
    rs = rust_exe_path()
    if not rs.is_file():
        pytest.skip("没有 Rust 宿主（单二进制才是包管理的实现）")
    env = dict(os.environ)
    env["JISHI_HOME"] = str(家)
    r = subprocess.run(
        [str(rs), "安装", "坏包", "--local-zip", str(包), "--force"],
        cwd=str(tmp_path), capture_output=True, timeout=120, env=env, input=b"")
    文 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode != 0, f"竟然装上了：{文[:200]}"
    assert "不合法" in 文 and ".." in 文, f"报错没说清哪个条目：{文[:300]}"
    assert not (tmp_path / "逃出.txt").exists()
