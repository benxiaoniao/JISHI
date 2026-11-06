# -*- coding: utf-8 -*-
"""M176：收口前的两条「卫生」判据 —— 锁文件、以及「外部依赖清单不许烂掉」。

## 为什么要专门测这两件

1. **`Cargo.lock` 以前当编译产物忽略掉了**，于是 CI 与发行包每次可能拉到不同的
   补丁版：同一份源码在两台机器上装到的依赖不一样，出了问题无法复现。加了
   wry / muda / tray-icon / rfd 之后（0.47.x 的补丁版也可能改 API）这不再能接受。
2. **README 的「外部依赖（全部清单）」是给人看、也给以后自己重写当靶子的**。
   清单最怕的不是写得不好，是**烂掉**：代码加了依赖、清单没跟；或者反过来，
   清单上留着早就删掉的库。所以让判据去和 `Cargo.toml` 对账。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 本仓库自己的 crate（路径依赖）—— 不是第三方。
自家 = {"jishi-frontend", "jishi-ffi", "jishi-core"}


def _依赖名们(相对路径):
    """从 Cargo.toml 里挖出第三方依赖名（跳过 `path =` 的自家 crate）。

    只做行级解析：这个仓库的 `Cargo.toml` 全是「一行一个依赖」的写法，
    上 toml 解析器要引依赖，不值当。写法变了这条会红 —— 那也是提醒。
    """
    s = (ROOT / 相对路径).read_text(encoding="utf-8")
    名们 = set()
    在依赖段 = False
    for 行 in s.splitlines():
        行 = 行.strip()
        if 行.startswith("["):
            # 只认依赖段：`[dependencies]` / `[dev-dependencies]` /
            # `[target.'cfg(...)'.dependencies]`。⚠️ 不能全文件扫 ——
            # `[package]` 里的 `name = "..."`、`[profile]` 里的 `strip = ...`
            # 会被当成依赖（第一版就这么错的）。
            在依赖段 = 行.rstrip("]").endswith("dependencies")
            continue
        if not 在依赖段 or not 行 or 行.startswith("#"):
            continue
        m = re.match(r"^([a-zA-Z0-9_-]+)\s*=", 行)
        if not m:
            continue
        名 = m.group(1)
        if 名 in 自家 or "path =" in 行:      # 路径依赖 = 自家 crate
            continue
        名们.add(名)
    return 名们


def test_四个lock都进仓库():
    """四个 crate 的 `Cargo.lock` 必须在仓库里、且不被 .gitignore 挡住。

    少了任何一份，那个 crate 的依赖版本就没被钉住 —— 而 `--locked` 构建会直接失败。
    """
    for 名 in ("rust", "wasm", "core", "frontend"):
        锁 = ROOT / 名 / "Cargo.lock"
        assert 锁.is_file(), f"{名}/Cargo.lock 不在仓库里（`--locked` 构建会失败）"

    忽略 = (ROOT / ".gitignore").read_text(encoding="utf-8")
    有效行 = [ln.strip() for ln in 忽略.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    for 名 in ("rust", "wasm", "core", "frontend"):
        规则 = f"{名}/Cargo.lock"
        assert 规则 not in 有效行, f".gitignore 又把它挡住了：{规则}"


def test_构建一律带locked():
    """CI 与两个构建脚本都要 `--locked`。

    不加的话：`cargo build` 会**悄悄更新**锁文件，CI 与发行包跑去追新的补丁版 ——
    仓库里那份锁就成了摆设。
    """
    for 相对 in (".github/workflows/ci.yml", ".github/workflows/release.yml"):
        文 = (ROOT / 相对).read_text(encoding="utf-8")
        assert "--locked" in 文, f"{相对} 的 cargo build 没带 --locked"

    for 相对 in ("tools/build_release.py", "tools/build_wasm.py"):
        文 = (ROOT / 相对).read_text(encoding="utf-8")
        assert '"--locked"' in 文, f"{相对} 的 cargo build 没带 --locked"


def _清单段():
    """README 里「外部依赖」那两节的正文（表格）。"""
    文 = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## 外部依赖（全部清单）" in 文, "README 里没有「外部依赖（全部清单）」这一节"
    # ⚠️ 按**行首**的 `## ` 切：`### 一、` 里也含 `## `，naive 的 split("## ")
    #    会把这一节在第一张小标题处就截断（实测踩过）。
    段 = 文.split("\n## 外部依赖（全部清单）", 1)[1]
    return 段.split("\n## ", 1)[0]


def test_README的依赖清单与Cargo对得上():
    """README 那一节必须是**全的**：代码里的第三方依赖一个都不能漏。

    漏写的后果是「清单看起来很美」—— 而它正是以后决定「先摆脱哪个」的依据。
    """
    段 = _清单段()
    # 表里的第一列：`| \`名字\` | …`
    列出来的 = set(re.findall(r"^\|\s*`([a-zA-Z0-9_-]+)`", 段, re.M))
    assert 列出来的, "清单表里一个依赖都没解析出来（表格格式变了？）"

    真有的 = _依赖名们("rust/Cargo.toml") | _依赖名们("core/Cargo.toml")

    漏 = 真有的 - 列出来的
    assert not 漏, f"这些依赖在 Cargo.toml 里有，README 清单里没有：{sorted(漏)}"

    # 反方向：**第一张表**（随发行版走的那张）里写着、Cargo 里已经没有的
    # —— 说明删了依赖忘了改文档。
    # ⚠️ 反向只比第一张表：第二张是 pytest / pillow 这类**不在 Cargo.toml 里**的
    #    开发期工具，拿它们去和 Cargo 对账永远「多」。
    一 = 段.split("### 二、", 1)[0]
    发行的 = set(re.findall(r"^\|\s*`([a-zA-Z0-9_-]+)`", 一, re.M))
    多 = 发行的 - 真有的
    assert not 多, f"README 清单上还留着已经不用的依赖：{sorted(多)}"


def test_清单写清了摆脱难度():
    """每个依赖都得有「为什么不自研 / 摆脱难度」—— 那是这份清单存在的理由。

    只列名字的清单没有用：以后要决定「先自己写哪个」时，缺的正是这两列。
    """
    段 = _清单段()
    # 只看「一、随发行版走」那张表（第二张表是构建期工具，列不同）
    一 = 段.split("### 二、", 1)[0]
    表 = [ln for ln in 一.splitlines() if ln.startswith("| `")]
    assert len(表) >= 9, f"清单表只有 {len(表)} 行，太少（是不是被删了？）"
    for 行 in 表:
        格 = [x.strip() for x in 行.strip("|").split("|")]
        assert len(格) >= 6, f"这一行少了几列（要有 用在哪/干什么/为什么不自研/摆脱难度）：{行[:60]}"
        assert 格[-1] and 格[-1] != "摆脱难度", f"「摆脱难度」那列是空的：{行[:60]}"
