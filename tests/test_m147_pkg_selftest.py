# -*- coding: utf-8 -*-
"""M147 —— 第三方包**自带的测试**要能直接跑（不用先把自己装一遍）。

## 这条钉的是什么

包里的测试写着 `导入 图片格式转换 从 本地包`（自引用）—— 因为包内部就是按
「别人怎么用它」来测的。但直接对包目录跑 `jishi 测试` 时，这个包**并没有装**：

    $ jishi 测试 examples/packages/图片格式转换
    图片格式转换_test.jsh
      ⚠️ 不是断言失败，是程序出错了（E0302 找不到这个函数）
         没有找到本地包「图片格式转换」（第 9 行）

「要跑一个包的测试，先得把它装一遍」不但别扭，CI 上还会往工程里写 `.jishi/`
（装完还得清）。⇒ 跑测试时把**这些包所在的父目录**临时当成包根
（`JISHI_PKG_ROOTS`），四端（Python 编译期 / Python 运行时 / Rust 宿主 / 前端）
都读同一份。

⚠️ 用**临时复制出来的工程**跑，不碰仓库根 —— 仓库根下 `~/.jishi` 或 `.jishi/`
里可能真装着这个包，那样「有没有装」这个前提就不成立了。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_m52_jishilib import RUST                        # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
包名 = "图片格式转换"
包源 = ROOT / "examples" / "packages" / 包名


@pytest.fixture()
def 工程(tmp_path):
    """把包复制到一个干净工程里（`packages/<包名>/`，**不**装进 `.jishi/`）。"""
    assert 包源.is_dir(), f"包目录不见了：{包源}"
    目标 = tmp_path / "packages" / 包名
    目标.parent.mkdir(parents=True)
    shutil.copytree(包源, 目标)
    return tmp_path


def _跑(宿主, 参数, cwd):
    """宿主 = "py" 或 "rust"。返回 (returncode, 合并输出)。"""
    if 宿主 == "py":
        命令 = [sys.executable, "-m", "oracle.jishi.cli"]
    else:
        if not RUST.exists():
            pytest.skip("没有编出 jishi-rs")
        命令 = [str(RUST)]
    r = subprocess.run(命令 + 参数, cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


@pytest.mark.parametrize("宿主", ["py", "rust"])
def test_包目录的自带测试能直接跑(工程, 宿主):
    码, 出 = _跑(宿主, ["测试", str(工程 / "packages" / 包名)], 工程)
    assert 码 == 0, 出
    assert "全部通过" in 出 and "11 个用例" in 出, 出
    # 跑测试**不该**顺手把包装进工程（那会让「没装也能跑」这个前提消失）
    assert not (工程 / ".jishi").exists(), "跑测试时把包装进去了 —— 那是副作用"


@pytest.mark.parametrize("宿主", ["py", "rust"])
def test_整个包目录一次跑完(工程, 宿主):
    """`jishi 测试 <包们的父目录>` 也成立（CI 上就是一条命令扫全部包）。"""
    码, 出 = _跑(宿主, ["测试", str(工程 / "packages")], 工程)
    assert 码 == 0, 出
    assert "全部通过" in 出, 出


def test_普通目录不受影响(tmp_path):
    """不是包目录 ⇒ **不**往搜索路径里塞东西（免得顺手 `测试 .` 时找到一堆包）。

    判据是负面的：一个普通目录里的测试，**导入本地包应当失败** ——
    说明没给它加包根。加了的话这里就会「意外成功」。
    """
    工程 = tmp_path / "普通工程" / "某目录"
    工程.mkdir(parents=True)
    (工程 / "用_test.jsh").write_text(
        "导入 测试\n导入 不存在的包 从 本地包\n测试.相等(1, 1)\n",
        encoding="utf-8", newline="\n")
    码, 出 = _跑("py", ["测试", str(工程)], 工程.parent)
    assert 码 != 0, 出
    assert "没有找到本地包" in 出, 出


def test_包根候选只认三种位置(tmp_path):
    """`_包根候选` 的口径：`根/包.json`、`根/*/包.json`、`根/*/*/包.json`。

    写死这三种而不是「递归整棵树」，是为了顺手 `测试 .` 时不把 node_modules
    之类翻一遍；这条把口径钉住，免得以后被人改成 rglob。
    """
    from oracle.jishi import cli as CLI

    # ① 根自己就是包 → 父目录
    单包 = tmp_path / "a" / "包甲"
    (单包).mkdir(parents=True)
    (单包 / "包.json").write_text("{}", encoding="utf-8")
    assert CLI._包根候选(单包) == [str((tmp_path / "a").resolve())]

    # ② 根的下一层是包 → 根自己
    assert CLI._包根候选(tmp_path / "a") == [str((tmp_path / "a").resolve())]

    # ③ 普通目录 → 空
    空 = tmp_path / "空"
    空.mkdir()
    assert CLI._包根候选(空) == []


# ---------------------------------------------------------------------------
# 二、`jishi 检查 <包目录>` 也得能用（M147 的另一半）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("宿主", ["py", "rust"])
def test_检查能吃包目录(工程, 宿主):
    """`jishi 检查 <包目录>`：按**目录里真实的包名**去查依赖冲突。

    以前传目录会被当成**包名** —— 那个「包」当然找不到，于是照样打
    「检查了 1 个包，未发现依赖冲突」：**看着像检查过了**。
    """
    码, 出 = _跑(宿主, ["检查", str(工程 / "packages")], 工程)
    assert 码 == 0, 出
    # 复制进去的只有一个包（图片格式转换），所以这里应当是 1 个
    assert "检查了 1 个包" in 出, 出


@pytest.mark.parametrize("宿主", ["py", "rust"])
def test_检查找不到的包要报错(工程, 宿主):
    """名字找不到 ⇒ **非 0 退出**，而不是用绿字盖过去。"""
    码, 出 = _跑(宿主, ["检查", "根本没有这个包"], 工程)
    assert 码 == 1, 出
    assert "找不到这些包" in 出, 出
    assert "jishi 检查 examples/packages/" in 出, "提示里要给可走的下一步"


def test_检查两端文案一致(工程):
    """两宿主对同一条错误说同一句话（对拍的意义就在这里）。"""
    py = _跑("py", ["检查", "根本没有这个包"], 工程)
    rs = _跑("rust", ["检查", "根本没有这个包"], 工程)
    assert py == rs, f"两宿主不一致\n--- python ---\n{py}\n--- rust ---\n{rs}"
