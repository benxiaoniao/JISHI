# -*- coding: utf-8 -*-
"""发布公开快照到 GitHub（单次干净提交，force 覆盖）。

**这是本项目唯一的发布通道**（2026-09-20 起 Gitee 已弃用，相关脚本与配置
一并删除）。

为什么要「快照 + 屏蔽」而不是直接 push 本仓库：本地 git 历史里有早期交接
文档、工作日志等隐私文件，公开仓库只应收到**当前状态的屏蔽版快照**。
本脚本因此不改动本地仓库 —— 它在临时目录里重新 `git init` 并造一个
**孤儿提交**，再 force push 到远端。副作用是远端 master 与本地 master
**不是同一个提交哈希**（设计如此，不是问题）。

用法：
    python tools/publish_github.py             # 导出 + 屏蔽 + 推送
    python tools/publish_github.py --dry-run   # 只导出 + 屏蔽 + 本地提交，不推送
    python tools/publish_github.py --tag v0.2.0
                                               # 同步并在快照上打 tag（触发
                                               # .github/workflows/release.yml
                                               # 构建三平台发行包 + 建 Release）
    python tools/publish_github.py -m "自定义提交信息"   # 覆盖默认提交信息

自动屏蔽项（隐私界定，2026-09-04 与作者确认；2026-09-23 更新）：
- `cvm/build.py` / `docs/设计决策.md` / `README.md`：个人机器路径（CLion 等）
  按行屏蔽或通用化；
- 令牌等敏感串绝不入库；
- **`docs/路线图.md` 整个不上传**（2026-09-23 作者定）：里面有自建分发服务器的
  实例 ID / 公网 IP / 地域与规格等运行细节。**文件直接删掉**，其余文档里指向它的
  链接与提及一并抹成中性表述（否则公开仓库里全是死链）；
- 终检：快照里若仍出现 `CLion` / 个人路径片段 / 令牌环境变量名 / 服务器地址，直接中止发布。
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_HTTPS = "https://github.com/benxiaoniao/JISHI.git"
BRANCH = "master"
AUTHOR_NAME = "陈柏林"
AUTHOR_EMAIL = "benxiaoniao@outlook.com"


# ---------------------------------------------------------------------------
# 提交信息：从「版本号 + CHANGELOG 那一版的标题」生成
# ---------------------------------------------------------------------------

def read_version(root: Path = ROOT) -> str:
    """读版本号（唯一来源 = `pyproject.toml` 的 `[project] version`，见 D21）。"""
    m = re.search(r'^version\s*=\s*"([^"]+)"',
                  (root / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else "0.0.0"


def changelog_headline(version: str, root: Path = ROOT) -> str:
    """取 CHANGELOG 里这一版的**粗体小标题**（拿不到就返回空串）。

    只读现成的事实，不猜也不编：解析不出来就退化成「只写版本号」。
    """
    p = root / "CHANGELOG.md"
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8")
    m = re.search(rf"^## \[{re.escape(version)}\].*?$", text, re.M)
    if not m:
        return ""
    for line in text[m.end():].split("\n")[:12]:
        line = line.strip()
        if line.startswith("**"):
            m2 = re.match(r"\*\*(.+?)\*\*", line)
            if m2:
                head = m2.group(1).strip()
                return head if len(head) <= 60 else ""
    return ""


def default_commit_msg(version: "str | None" = None) -> str:
    """公开快照的提交信息：**显示版本阶段即可**（作者 2026-09-19 定）。

    以前 GitHub 侧写的是「sync: 公开快照（屏蔽版）」——那是**同步机制**的说明，
    对看仓库的人没有信息量；Gitee 侧更糟：写死的里程碑列表停在 M16（早就过时）。
    现在由「版本号 + CHANGELOG 那一版的标题」生成：

        基石（jishi）v0.1.23 —— M41 已交付：标准库第一批 + `遍历` 解包 + …

    想换个说法就用 `--message "…"` 覆盖。
    """
    v = version or read_version()
    head = changelog_headline(v)
    return f"基石（jishi）v{v}" + (f" —— {head}" if head else "")


def cli_message(argv: "list[str]") -> "str | None":
    """从命令行取 `--message/-m`（覆盖快照提交信息）。"""
    for flag in ("--message", "-m"):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                return argv[i + 1].strip() or None
    return None


# ---------------------------------------------------------------------------
# git 与隐私屏蔽
# ---------------------------------------------------------------------------

def _git(*args: str, cwd: Path | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args[:2])} 失败：\n{r.stderr}")
    return r.stdout


def _sanitize(snapshot: Path) -> None:
    """隐私屏蔽（每步幂等，重复运行安全）。

    2026-09-23 调整：**`docs/路线图.md` 不再上传**（作者定）——删文件 + 抹掉
    指向它的引用（理由见模块 docstring）。个人机器路径 / CLion / 令牌 /
    服务器地址等照旧屏蔽。
    """
    # 1. build.py：移除个人机器的 CLion gcc 路径条目
    p = snapshot / "cvm" / "build.py"
    lines = io.open(p, encoding="utf-8").read().split("\n")
    lines = [l for l in lines if "CLion" not in l]
    io.open(p, "w", encoding="utf-8", newline="\n").write("\n".join(lines))

    # 2. 设计决策.md：CLion 表述通用化
    p = snapshot / "docs" / "设计决策.md"
    src = io.open(p, encoding="utf-8").read()
    src = src.replace("C 内核（gcc 15.2.0，CLion 自带 MinGW）是为了长期性能",
                      "C 内核（MinGW-w64 gcc）是为了长期性能")
    io.open(p, "w", encoding="utf-8", newline="\n").write(src)

    # 3. README：行级过滤个人路径（CLion / 本机受管路径）
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
    io.open(p, "w", encoding="utf-8", newline="\n").write("\n".join(out))

    # 4. 内部规划文档不上传（2026-09-23 / 2026-09-25 作者定）：整个文件删掉
    #    + 抹掉指向它的引用。删文件会让其余文档里的引用变成死链，
    #    所以一并换成中性表述。
    #    - `docs/路线图.md`（2026-09-23）：含自建分发服务器的实例 ID / 公网 IP /
    #      地域与规格。
    #    - `docs/架构评估.md`（2026-09-25）：含里程碑编号规划（M51–M53）与
    #      「哪些不砍、哪些要迁移」的**未定稿**技术决策——属内部讨论材料，
    #      对外过早公开会绑住手脚（用户还没拍板）。
    rm = snapshot / "docs" / "路线图.md"
    if rm.exists():
        rm.unlink()
    _arch = snapshot / "docs" / "架构评估.md"
    if _arch.exists():
        _arch.unlink()
    _link = re.compile(r"\[[^\]]*?路线图[^\]]*?\]\((?:\.\./|docs/)?路线图\.md\)")
    _arch_link = re.compile(r"\[[^\]]*?架构评估[^\]]*?\]\((?:\.\./|docs/)?架构评估\.md\)")
    _arch_code = re.compile(r"`+(?:\.\./|docs/)?架构评估\.md`+")
    _arch_plain = re.compile(r"(?:\.\./|docs/)?架构评估\.md")
    _code = re.compile(r"`+(?:\.\./|docs/)?路线图\.md`+")
    _plain = re.compile(r"(?:\.\./|docs/)?路线图\.md")
    for f in sorted(snapshot.rglob("*")):
        if not f.is_file() or f.name.startswith("publish_"):
            continue
        if f.suffix.lower() not in (".md", ".py", ".json", ".txt", ".jsh", ".rst"):
            continue
        try:
            s = io.open(f, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        # 顺序：先整条链接 → 再去内联代码 → 最后兜底裸路径
        new = _plain.sub("内部路线图（未公开）",
                         _code.sub("内部路线图（未公开）",
                                   _link.sub("内部路线图（未公开）", s)))
        new = _arch_plain.sub("内部架构评估（未公开）",
                              _arch_code.sub("内部架构评估（未公开）",
                                             _arch_link.sub("内部架构评估（未公开）", new)))
        if new != s:
            io.open(f, "w", encoding="utf-8", newline="\n").write(new)

    # 4.1 README「项目结构」是**文件清单**：路线图那一条要整条摘掉
    #     （否则清单里混进一句「内部路线图（未公开）」，读着像文件名）
    p = snapshot / "README.md"
    if p.exists():
        s = io.open(p, encoding="utf-8").read()
        s2 = s.replace("设计决策.md、内部路线图（未公开）、调试器.md",
                       "设计决策.md、调试器.md")
        if s2 != s:
            io.open(p, "w", encoding="utf-8", newline="\n").write(s2)

    # 5. 终检：快照里不允许再出现任何隐私模式
    bad = []
    if rm.exists():
        bad.append("docs/路线图.md 仍在快照里（本文件不上传）")
    if _arch.exists():
        bad.append("docs/架构评估.md 仍在快照里（本文件不上传）")
    for f in snapshot.rglob("*"):
        if not f.is_file():
            continue
        # 发布工具自身会提到这些名字（publish_*.py）
        if f.name.startswith("publish_"):
            continue
        try:
            text = io.open(f, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        for pat in ("17719", "CLion", "GITEE_TOKEN", "gitee.com",
                    "lhins-", "111.229."):
            if pat in text:
                bad.append(f"{f} 含 {pat!r}")
    if bad:
        raise SystemExit("隐私终检未通过：\n" + "\n".join(bad))


# ---------------------------------------------------------------------------
# 推送前的提示音（2026-09-24 用户要求）
# ---------------------------------------------------------------------------

def _notify_push() -> None:
    """推送**之前**响一声 + 打一行醒目提示。

    背景（2026-09-24 用户提）：往 GitHub 推送时用户机器上会弹确认框，
    用户希望**推之前先有个声音**，好知道该去看那个弹窗了。

    做法是「双保险」，因为任何单一路径都可能被静音/关掉：
    1. **终端响铃**（`\\a`）——有没有声音取决于终端设置；
    2. **系统提示音**（Windows 用标准库 `winsound.Beep`）——独立于终端。
    两步都包在 try 里：**响不了也绝不挡推送**，那只是提醒手段，不是前置条件。
    """
    print("\n🔔 即将推送 GitHub —— 若弹出确认框请点允许")
    try:                                    # 1. 终端响铃
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:                       # noqa: BLE001
        pass
    if sys.platform == "win32":            # 2. 系统提示音
        try:
            import winsound
            for _ in range(2):
                winsound.Beep(880, 180)
        except Exception:                   # noqa: BLE001
            pass


# ---------------------------------------------------------------------------

def main() -> int:
    dry_run = "--dry-run" in sys.argv
    tag = None
    if "--tag" in sys.argv:
        i = sys.argv.index("--tag")
        if i + 1 < len(sys.argv):
            tag = sys.argv[i + 1].strip()
        if not tag:
            raise SystemExit("--tag 后面要跟版本号，例如：--tag v0.2.0")

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
    commit_msg = cli_message(sys.argv) or default_commit_msg(
        tag.lstrip("v") if tag else None)
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

    _notify_push()
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
