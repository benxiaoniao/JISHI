"""下游回归（M201）：**拿真实下游项目跑一遍**当前这版语言。

## 为什么不能只靠语言自己的判据

语言仓库里的判据只测语言自己。真实项目（第一个是 `基石数据库`）是用**上一版**
语言写的 —— 改一点语义、改一句报错文案、删一个别名，它可能悄悄坏掉，
而本地全量依旧全绿。**唯一能发现的是「拿它跑一遍」**。

所以：清单（`docs/下游清单.toml`）+ 一条命令 + 一份报告。发版前跑，CI 也跑。

## 三条必须守住的规矩

1. **用当前仓库的引擎，不用系统装的**。清单里的 `jishi` 会被替换成本仓库的
   `rust/target/release/jishi-rs.exe`（或 `--引擎 python`）。拿系统那个跑，
   测的是「用户装的上一版」，正好什么都没测到。
2. **克隆只做一次**（`build/下游/<名字>`）。每次重新 clone 要几十秒且依赖网络，
   跑一次就够 —— 要最新代码加 `--更新`。
3. **期望输出写出来**。只看退出码不够：`对拍_test.jsh` 那种「100/100 一致」
   比「没崩」有用得多；而「跑完打印 50/100 又正常退出」这种半红，只有比对
   期望串才看得见。

## 用法

    python tools/下游回归.py                 # 全部下游（缓存已有的克隆）
    python tools/下游回归.py --更新          # 强制重新 clone
    python tools/下游回归.py --只 基石数据库
    python tools/下游回归.py --引擎 python   # 用 Python 前端跑（验两条前端都行）
    python tools/下游回归.py --清单          # 只校验清单结构，不联网（判据用这个）

退出码：0 = 全部通过（或清单为空）；1 = 有失败 / 清单写错。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
清单路径 = ROOT / "docs" / "下游清单.toml"
缓存根 = ROOT / "build" / "下游"


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------


def 读清单(路径: Path = None):
    """解析清单；结构不对就抛 `ValueError`（带位置说明）。"""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11
        raise SystemExit(
            "这个工具要 Python 3.11+（`tomllib`）。当前是 "
            f"{sys.version_info.major}.{sys.version_info.minor}"
        )
    路径 = 路径 or 清单路径
    if not 路径.is_file():
        raise ValueError(f"少了清单文件：{路径}")
    with 路径.open("rb") as f:
        数据 = tomllib.load(f)
    下游们 = 数据.get("downstream") or []
    if not isinstance(下游们, list):
        raise ValueError("清单里 `[[downstream]]` 要写成数组表")

    见过: set[str] = set()
    out = []
    for i, 项 in enumerate(下游们, 1):
        名 = str(项.get("name") or "").strip()
        if not 名:
            raise ValueError(f"第 {i} 个下游没写 `name`")
        if 名 in 见过:
            raise ValueError(f"下游名字重复：「{名}」（报告里分不清谁是谁）")
        见过.add(名)
        地址 = str(项.get("url") or "").strip()
        if not 地址:
            raise ValueError(f"下游「{名}」没写 `url`")
        步骤 = 项.get("steps") or []
        if not isinstance(步骤, list) or not 步骤:
            raise ValueError(f"下游「{名}」一个步骤都没有 —— 那跑它做什么？")
        步骤们 = []
        for j, s in enumerate(步骤, 1):
            步骤名 = str(s.get("name") or "").strip()
            if not 步骤名:
                raise ValueError(f"下游「{名}」第 {j} 个步骤没写 `name`")
            命令 = s.get("cmd")
            if not isinstance(命令, list) or not 命令:
                raise ValueError(f"下游「{名}」的步骤「{步骤名}」没写 `cmd`（要数组）")
            if not isinstance(命令[0], str) or not 命令[0].strip():
                raise ValueError(f"下游「{名}」的步骤「{步骤名}」cmd 第 1 项要是脚本路径")
            步骤们.append(
                {
                    "名": 步骤名,
                    "命令": [str(x) for x in 命令],
                    "期望": str(s.get("expect") or ""),
                    "超时秒": int(s.get("timeout") or 项.get("timeout") or 300),
                }
            )
        out.append(
            {
                "名字": 名,
                "地址": 地址,
                "说明": str(项.get("note") or ""),
                "本地包": [str(x) for x in (项.get("local_packages") or [])],
                "步骤": 步骤们,
            }
        )
    return out


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


def 找引擎(选: str):
    """返回 `(显示名, [命令前缀])`。**只用本仓库的产物。**"""
    if 选 == "python":
        return "Python 前端", [sys.executable, "-m", "oracle.jishi.cli"]
    exe = ROOT / "rust" / "target" / "release" / (
        "jishi-rs.exe" if sys.platform.startswith("win") else "jishi-rs"
    )
    if not exe.is_file():
        raise SystemExit(
            f"找不到 Rust 引擎：{exe}\n"
            "先编一个：`cd rust && cargo build --release`（或用 `--引擎 python`）"
        )
    return "Rust 宿主", [str(exe)]


# ---------------------------------------------------------------------------
# 克隆与本地包
# ---------------------------------------------------------------------------


def 准备(项, 更新: bool) -> Path:
    """把下游仓库弄到 `build/下游/<名字>`，返回它的路径。"""
    目录 = 缓存根 / 项["名字"]
    if 更新 and 目录.is_dir():
        shutil.rmtree(目录)
    if not 目录.is_dir():
        目录.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["git", "clone", "--depth", "1", 项["地址"], str(目录)],
            capture_output=True,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"clone 失败（{项['地址']}）：\n"
                + (r.stderr or r.stdout).decode("utf-8", "replace")[-800:]
            )
    else:
        # 有缓存就**不联网**：发版前跑的时候网络不该是变量
        pass

    # 本地包：把仓库里的同名目录复制到 `.jishi/packages/` 下。
    # 为什么复制而不是 junction：junction 只有 Windows 有，CI 上三个平台都要跑；
    # 而且 junction 指向源目录 ⇒ 源目录被改时行为会跟着变（「时好时坏」那类）。
    for 包 in 项["本地包"]:
        源 = 目录 / 包
        if not 源.is_dir():
            raise RuntimeError(f"清单里声明了本地包「{包}」，但仓库里没有这个目录：{源}")
        目标 = 目录 / ".jishi" / "packages" / 包
        if 目标.is_dir():
            shutil.rmtree(目标)
        目标.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(源, 目标)
    return 目录


# ---------------------------------------------------------------------------
# 跑
# ---------------------------------------------------------------------------


def 跑一步(项名: str, 目录: Path, 步: dict, 引擎: list, 显示名: str):
    """返回 `(通过?, 用时秒, 输出)`。"""
    # 清单里第 1 项写 `jishi` = 「用引擎跑这个脚本」，换成真实引擎
    命令 = list(引擎) + (步["命令"][1:] if 步["命令"][0] == "jishi" else 步["命令"])
    起 = time.time()
    try:
        r = subprocess.run(命令, cwd=str(目录), capture_output=True, timeout=步["超时秒"])
    except subprocess.TimeoutExpired:
        return False, time.time() - 起, f"⏱ 超过 {步['超时秒']} 秒没跑完（死循环？）"
    用时 = time.time() - 起
    文 = (r.stdout + r.stderr).decode("utf-8", "replace")
    期望 = 步["期望"].strip()
    if r.returncode != 0:
        return False, 用时, f"退出码 {r.returncode}\n{文}"
    if 期望 and 期望 not in 文:
        return False, 用时, f"输出里没有「{期望}」\n{文}"
    return True, 用时, 文


def 尾(文: str, 行数: int = 20) -> str:
    """失败的输出只留最后几行 —— 报告要能一眼看出问题在哪。"""
    行 = [x for x in 文.strip().splitlines() if x.strip()]
    if len(行) <= 行数:
        return "\n".join(行)
    return "…（前略）\n" + "\n".join(行[-行数:])


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="下游回归（M201）")
    ap.add_argument("--更新", action="store_true", help="强制重新 clone")
    ap.add_argument("--只", default="", help="只跑某个下游（按名字）")
    ap.add_argument("--引擎", choices=["rust", "python"], default="rust")
    ap.add_argument("--清单", action="store_true", help="只校验清单结构，不联网")
    ap.add_argument("--报告", default="", help="把 markdown 报告写到这个路径")
    # 判据要用临时清单验「写错了会指出来」，所以给一个显式入口（不放进 --help：
    # 正常用法只有一份清单）
    ap.add_argument("--清单路径", default="", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    try:
        下游们 = 读清单(Path(args.清单路径) if args.清单路径 else None)
    except ValueError as e:
        print(f"错误：清单有问题 —— {e}", file=sys.stderr)
        return 1

    if args.清单:
        if not 下游们:
            print("清单里一个下游都没有（这不算错，但回归就没东西可跑了）")
            return 0
        print(f"清单 OK：{len(下游们)} 个下游")
        for 项 in 下游们:
            print(f"  · {项['名字']}  {len(项['步骤'])} 步  {项['地址']}")
            for s in 项["步骤"]:
                print(f"      - {s['名']}：{s['命令']}")
        return 0

    if args.只:
        下游们 = [x for x in 下游们 if x["名字"] == args.只]
        if not 下游们:
            print(f"错误：清单里没有叫「{args.只}」的下游", file=sys.stderr)
            return 1
    if not 下游们:
        print("清单里一个下游都没有 —— 没什么可跑的")
        return 0

    try:
        显示名, 引擎 = 找引擎(args.引擎)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 1

    print(f"引擎：{显示名}（{引擎[0]}）")
    print(f"下游：{len(下游们)} 个" + ("，强制更新" if args.更新 else ""))
    报告 = ["# 下游回归报告", "", f"- 引擎：{显示名}", f"- 条目：{len(下游们)}", ""]
    失败数 = 0
    总起 = time.time()
    for 项 in 下游们:
        print(f"\n== {项['名字']} ==")
        报告 += [f"## {项['名字']}", ""]
        try:
            目录 = 准备(项, args.更新)
        except RuntimeError as e:
            print(f"  ❌ 准备失败：{e}")
            报告 += [f"❌ 准备失败：{e}", ""]
            失败数 += 1
            continue
        报告 += ["| 步骤 | 结果 | 用时 |", "|---|---|---|"]
        for 步 in 项["步骤"]:
            通过, 用时, 文 = 跑一步(项["名字"], 目录, 步, 引擎, 显示名)
            标记 = "✅" if 通过 else "❌"
            期望 = f"（期望含「{步['期望']}」）" if 步["期望"] else ""
            print(f"  {标记} {步['名']}  {用时:.1f}s{'' if 通过 else ''}")
            if not 通过:
                失败数 += 1
                print(尾(文, 12))
                报告 += [f"| {步['名']} | ❌ 失败 | {用时:.1f}s |", ""]
                报告 += ["```", 尾(文, 30), "```", ""]
            else:
                报告 += [f"| {步['名']} | ✅{期望} | {用时:.1f}s |"]
        报告.append("")

    总 = time.time() - 总起
    print(f"\n{'=' * 60}")
    if 失败数:
        print(f"❌ {失败数} 步失败（{总:.1f} 秒）—— 下游被这一版语言弄坏了，先别发")
        报告 = ["", f"**❌ {失败数} 步失败**（{总:.1f} 秒）"] + 报告
    else:
        print(f"✅ 全部通过（{总:.1f} 秒）")
        报告 = ["", f"**✅ 全部通过**（{总:.1f} 秒）"] + 报告

    if args.报告:
        出 = Path(args.报告)
        出.parent.mkdir(parents=True, exist_ok=True)
        出.write_text("\n".join(报告), encoding="utf-8")
        print(f"报告：{出}")
    return 1 if 失败数 else 0


if __name__ == "__main__":
    raise SystemExit(main())
