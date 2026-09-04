# -*- coding: utf-8 -*-
"""同步公开快照到 Gitee（单次干净提交，force 覆盖）。

为什么不是直接 push 本仓库：本地 git 历史含早期交接文档/工作日志等
隐私文件，公开仓库只应收到「当前状态的屏蔽版快照」。

用法（令牌从环境变量读，绝不写入任何文件）：
    GITEE_TOKEN=你的令牌 python tools/publish_gitee.py

自动屏蔽项（隐私界定，2026-09-04 与作者确认）：
- docs/路线图.md 不上传（内部规划）；
- README：个人机器路径行删除、CLion 行通用化、「路线图」节泛化防断链；
- cvm/build.py：个人 CLion gcc 路径条目移除；
- docs/设计决策.md：CLion 表述通用化。
"""

import io
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_HTTPS = "https://stupid-bird123:{token}@gitee.com/stupid-bird123/jishi.git"
BRANCH = "master"
AUTHOR_NAME = "陈柏林"
AUTHOR_EMAIL = "benxiaoniao@outlook.com"

COMMIT_MSG = """基石（jishi）v0.1：中文编程语言——M0-M6 全部里程碑

- 词法/语法/AST/中文报错（精确到行列 + 修正建议）
- 树遍历 / Python 字节码 VM / C 字节码 VM 三执行器，107 项对拍逐字节一致
- 异常处理（尝试/捕获/最终/抛出）+ 面向对象（类/继承/新建）
- Python 生态桥接：导入任意 Python 库、关键字参数、异常中文化
- 标准库 6 模块（随机/数学/时间/文件/表格/文本）+ 教程全 7 章
- 工程化：CI（一键全检 + GitHub Actions）、VSCode 扩展、文档站、wheel 打包
- 365 项测试全部通过"""


def _git(*args: str, cwd: Path | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args[:2])} 失败：\n{r.stderr}")
    return r.stdout


def _sanitize(snapshot: Path) -> None:
    """隐私屏蔽（每步幂等，重复运行安全）。"""
    # 1. 路线图不上传
    roadmap = snapshot / "docs" / "路线图.md"
    if roadmap.exists():
        roadmap.unlink()

    # 2. build.py：移除个人机器的 CLion gcc 路径条目
    p = snapshot / "cvm" / "build.py"
    lines = io.open(p, encoding="utf-8").read().split("\n")
    lines = [l for l in lines if "CLion" not in l]
    io.open(p, "w", encoding="utf-8", newline="\n").write("\n".join(lines))

    # 3. 设计决策.md：CLion 表述通用化
    p = snapshot / "docs" / "设计决策.md"
    src = io.open(p, encoding="utf-8").read()
    src = src.replace("C 内核（gcc 15.2.0，CLion 自带 MinGW）是为了长期性能",
                      "C 内核（MinGW-w64 gcc）是为了长期性能")
    io.open(p, "w", encoding="utf-8", newline="\n").write(src)

    # 4. README：行级过滤个人路径 + 路线图节泛化
    p = snapshot / "README.md"
    out = []
    for l in io.open(p, encoding="utf-8").read().split("\n"):
        if "原环境受管路径" in l:                 # 个人路径行 → 删
            continue
        if "原环境用 CLion 自带的 MinGW" in l:     # CLion 行 → 通用化
            out.append("- **C 编译器**（仅 M4+ 需要）：MinGW-w64 gcc"
                       "（15.2.0 验证通过）。")
            continue
        if "CLion" in l:                          # 其余 CLion 路径行 → 删
            continue
        out.append(l)
    src = "\n".join(out)
    head, sep, rest = src.partition("## 路线图")
    if sep:
        _, _s2, tail = rest.partition("## 设计原则")
        src = head + (
            "## 路线图\n\n"
            "M0–M6 已完成（见「当前进度」）。后续版本将持续打磨语法表达力、\n"
            "扩充标准库生态、优化执行性能，并加强对 AI 编程助手场景的支持。\n\n"
        ) + "## 设计原则" + tail
    io.open(p, "w", encoding="utf-8", newline="\n").write(src)

    # 5. 终检：快照里不允许再出现任何隐私模式
    bad = []
    for f in snapshot.rglob("*"):
        if f.is_file():
            try:
                text = io.open(f, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue
            for pat in ("17719", "CLion", "GITEE_TOKEN"):
                if pat in text:
                    bad.append(f"{f} 含 {pat!r}")
    if bad:
        raise SystemExit("隐私终检未通过：\n" + "\n".join(bad))


def main() -> int:
    token = os.environ.get("GITEE_TOKEN", "").strip()
    if not token:
        raise SystemExit("请先设置环境变量 GITEE_TOKEN（Gitee 私人令牌）")

    snapshot = Path(tempfile.mkdtemp(prefix="jishi-public-"))
    print(f"[1/4] 导出快照 → {snapshot}")
    r = subprocess.run(["git", "archive", "HEAD", "--format=tar"],
                       cwd=ROOT, capture_output=True)
    if r.returncode != 0:
        raise SystemExit(f"git archive 失败：{r.stderr.decode('utf-8', 'replace')}")
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
        tf.extractall(snapshot, filter="data")  # 自家 git archive 产物

    print("[2/4] 隐私屏蔽 + 终检")
    _sanitize(snapshot)

    print("[3/4] 快照内初始化提交")
    _git("init", "-q", "-b", BRANCH, cwd=snapshot)
    _git("add", "-A", cwd=snapshot)
    _git("-c", f"user.name={AUTHOR_NAME}", "-c", f"user.email={AUTHOR_EMAIL}",
         "commit", "-q", "-m", COMMIT_MSG, cwd=snapshot)

    print("[4/4] force push 到 Gitee")
    r = subprocess.run(
        ["git", "push", "-f", REPO_HTTPS.format(token=token),
         f"{BRANCH}:{BRANCH}"],
        cwd=snapshot, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"推送失败：\n{r.stderr}")

    import shutil
    shutil.rmtree(snapshot, ignore_errors=True)
    print("✓ 已同步（含屏蔽）并清理临时快照")
    return 0


if __name__ == "__main__":
    sys.exit(main())
