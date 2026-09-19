# -*- coding: utf-8 -*-
"""同步公开快照到 GitHub（单次干净提交，force 覆盖）。

与 publish_gitee.py 共用同一套隐私屏蔽逻辑（``_sanitize``），只是推送目标
不同：Gitee 用令牌，GitHub 用本机 Git Credential Manager 认证（无需令牌参数）。

目标仓库：https://github.com/benxiaoniao/JISHI

用法：
    python tools/publish_github.py             # 导出 + 屏蔽 + 推送
    python tools/publish_github.py --dry-run   # 只导出 + 屏蔽 + 本地提交，不推送
    python tools/publish_github.py --tag v0.1.1
                                               # 同步并打 tag（触发 Release 工作流）
    python tools/publish_github.py -m "自定义提交信息"   # 覆盖默认的版本信息

说明：tag 打在**屏蔽版快照的孤儿提交**上（不是本地仓库历史），
因此不会把本地 git 历史里可能残留的个人路径一并推上公开仓库。

屏蔽项与 Gitee 一致：个人机器路径（CLion 等）、令牌等敏感串；
docs/路线图.md 已恢复上传（2026-09-12 作者确认）。
"""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

# 复用 Gitee 脚本里的屏蔽逻辑，保证两个目标产出一致
TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
from publish_gitee import (  # noqa: E402
    _sanitize,
    cli_message,
    default_commit_msg,
)

ROOT = Path(__file__).resolve().parents[1]
REPO_HTTPS = "https://github.com/benxiaoniao/JISHI.git"
BRANCH = "master"
AUTHOR_NAME = "陈柏林"
AUTHOR_EMAIL = "benxiaoniao@outlook.com"


def _git(*args: str, cwd: Path | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args[:2])} 失败：\n{r.stderr}")
    return r.stdout


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    tag = None
    if "--tag" in sys.argv:
        i = sys.argv.index("--tag")
        if i + 1 < len(sys.argv):
            tag = sys.argv[i + 1].strip()
        if not tag:
            raise SystemExit("--tag 后面要跟版本号，例如：--tag v0.1.1")

    snapshot = Path(tempfile.mkdtemp(prefix="jishi-github-"))
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
    commit_msg = cli_message(sys.argv) or default_commit_msg(tag.lstrip("v") if tag else None)
    print(f"   提交信息：{commit_msg}")
    _git("init", "-q", "-b", BRANCH, cwd=snapshot)
    _git("add", "-A", cwd=snapshot)
    _git("-c", f"user.name={AUTHOR_NAME}", "-c", f"user.email={AUTHOR_EMAIL}",
         "commit", "-q", "-m", commit_msg, cwd=snapshot)

    if dry_run:
        if tag:
            _git("tag", "-f", tag, cwd=snapshot)
            print(f"   （dry-run 也已在快照内打 tag {tag}，但未推送）")
        print(f"\n--dry-run：快照保留在 {snapshot}")
        print("   已做：导出 + 屏蔽 + 终检 + 本地提交（未推送）")
        return 0

    print(f"[4/4] force push 到 GitHub（{REPO_HTTPS}）")
    r = subprocess.run(
        ["git", "push", "-f", REPO_HTTPS, f"{BRANCH}:{BRANCH}"],
        cwd=snapshot, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"推送失败：\n{r.stderr}")
    print("    ✓ 代码已同步")

    # 可选：在屏蔽版快照的孤儿提交上打 tag，触发 Release 工作流
    if tag:
        _git("tag", "-f", tag, cwd=snapshot)
        r = subprocess.run(
            ["git", "push", "-f", REPO_HTTPS, f"refs/tags/{tag}"],
            cwd=snapshot, capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise SystemExit(f"推送 tag 失败：\n{r.stderr}")
        print(f"    ✓ 已打 tag {tag}（将触发 GitHub Actions 构建三平台发行包）")

    import shutil
    shutil.rmtree(snapshot, ignore_errors=True)
    print("✓ 已同步（含屏蔽）并清理临时快照")
    return 0


if __name__ == "__main__":
    sys.exit(main())
