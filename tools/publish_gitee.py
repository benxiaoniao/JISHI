# -*- coding: utf-8 -*-
"""同步公开快照到 Gitee（单次干净提交，force 覆盖）。

为什么不是直接 push 本仓库：本地 git 历史含早期交接文档/工作日志等
隐私文件，公开仓库只应收到「当前状态的屏蔽版快照」。

用法（令牌从环境变量读，绝不写入任何文件）：
    GITEE_TOKEN=你的令牌 python tools/publish_gitee.py

自动屏蔽项（隐私界定，2026-09-04 与作者确认；2026-09-12 更新）：
- README / cvm/build.py / docs/设计决策.md：个人机器路径（CLion 等）屏蔽；
- 令牌等敏感串绝不入库；
- docs/路线图.md 已恢复上传（2026-09-12 作者确认）。
"""

import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_HTTPS = "https://stupid-bird123:{token}@gitee.com/stupid-bird123/jishi.git"
BRANCH = "master"
AUTHOR_NAME = "陈柏林"
AUTHOR_EMAIL = "benxiaoniao@outlook.com"

#: Gitee API（v5）仓库坐标
OWNER = "stupid-bird123"
REPO = "jishi"
API_BASE = f"https://gitee.com/api/v5/repos/{OWNER}/{REPO}"


# ---------------------------------------------------------------------------
# Gitee Release（M16.4）：创建 release + 上传发行包附件
# ---------------------------------------------------------------------------

def _http_post_json(url: str, fields: dict) -> dict:
    """POST 表单字段，返回解析后的 JSON。"""
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise SystemExit(f"Gitee API 返回 {e.code}：{body[:600]}")


def _http_post_file(url: str, fields: dict, file_field: str,
                    file_path: Path) -> dict:
    """multipart/form-data POST：若干普通字段 + 一个文件字段。"""
    boundary = f"----jishi{uuid.uuid4().hex}"
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        buf.write(f"{v}\r\n".encode())
    fname = file_path.name.encode("utf-8")
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(
        f'Content-Disposition: form-data; name="{file_field}"; '
        f'filename="{fname.decode("utf-8")}"\r\n'.encode())
    buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
    with open(file_path, "rb") as f:
        buf.write(f.read())
    buf.write(f"\r\n--{boundary}--\r\n".encode())

    req = urllib.request.Request(url, data=buf.getvalue(), method="POST")
    req.add_header(
        "Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise SystemExit(f"上传附件失败（{e.code}）：{body[:600]}")


def create_release(token: str, tag: str, name: str, body: str) -> dict:
    """在 Gitee 创建一个 release（tag 不存在时由 API 基于分支创建）。"""
    print(f"[release] 创建 {tag} ...")
    rel = _http_post_json(
        f"{API_BASE}/releases",
        {"access_token": token, "tag_name": tag, "name": name,
         "body": body, "target_commitish": BRANCH})
    print(f"[release] 已创建 id={rel.get('id')} tag={rel.get('tag_name')}")
    return rel


def upload_release_assets(token: str, release_id: int,
                          files: list) -> None:
    """往 release 上传发行包附件（zip + SHA256SUMS）。"""
    for p in files:
        print(f"[release] 上传附件 {p.name}（{p.stat().st_size / 1048576:.1f} MB）")
        _http_post_file(
            f"{API_BASE}/releases/{release_id}/attach_files",
            {"access_token": token}, "file", p)
        print(f"[release]   ✓ {p.name} 上传完成")

def read_version(root: Path = ROOT) -> str:
    """读版本号（唯一来源 = pyproject.toml 的 [project] version，见 D21）。"""
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
    对看仓库的人没有信息量；Gitee 侧更糟：写死的里程碑列表停在 M16（早就过时，
    一直没人发现）。    现在两侧统一由「版本号 + CHANGELOG 那一版的标题」生成：

        基石（jishi）v0.1.20 —— M38 主线 B 收尾：LSP 增量同步 / REPL 增强 / 格式化器边界

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



def _git(*args: str, cwd: Path | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args[:2])} 失败：\n{r.stderr}")
    return r.stdout


def _sanitize(snapshot: Path) -> None:
    """隐私屏蔽（每步幂等，重复运行安全）。

    2026-09-12 调整：路线图恢复上传（用户确认），不再屏蔽 docs/路线图.md；
    仍保留个人机器路径 / CLion / 令牌等真正的隐私项屏蔽。
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

    # 4. 终检：快照里不允许再出现任何隐私模式（路线图已恢复上传，不检查死链）
    bad = []
    for f in snapshot.rglob("*"):
        if not f.is_file():
            continue
        # 发布工具自身会提到这些名字（publish_gitee.py / publish_github.py）
        if f.name.startswith("publish_"):
            continue
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
    dry_run = "--dry-run" in sys.argv
    if not dry_run:
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
    commit_msg = cli_message(sys.argv) or default_commit_msg()
    print(f"   提交信息：{commit_msg}")
    _git("init", "-q", "-b", BRANCH, cwd=snapshot)
    _git("add", "-A", cwd=snapshot)
    _git("-c", f"user.name={AUTHOR_NAME}", "-c", f"user.email={AUTHOR_EMAIL}",
         "commit", "-q", "-m", commit_msg, cwd=snapshot)

    if dry_run:
        print(f"\n--dry-run：快照保留在 {snapshot}")
        print("   已做：导出 + 屏蔽 + 终检 + 本地提交（未推送）")
        return 0

    print("[4/4] force push 到 Gitee")
    r = subprocess.run(
        ["git", "push", "-f", REPO_HTTPS.format(token=token),
         f"{BRANCH}:{BRANCH}"],
        cwd=snapshot, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"推送失败：\n{r.stderr}")
    print("    ✓ 代码已同步")

    import shutil
    shutil.rmtree(snapshot, ignore_errors=True)

    # 可选：创建 Gitee Release 并上传发行包（M16.4）
    if "--release" in sys.argv:
        _do_release(token)

    print("✓ 已同步（含屏蔽）并清理临时快照")
    return 0


def _do_release(token: str) -> None:
    """创建 Gitee Release + 上传 dist/release 下的发行包。"""
    sys.path.insert(0, str(ROOT))
    from jishi.version import VERSION

    tag = f"v{VERSION}"
    name = f"基石 jishi {VERSION}"
    body = _release_body(VERSION)

    rel = create_release(token, tag, name, body)

    # 上传发行包：zip（或 tar.gz）+ SHA256SUMS
    dist = ROOT / "dist" / "release"
    files = sorted(
        [p for p in dist.glob(f"jishi-{VERSION}-*")
         if p.suffix in (".zip", ".gz")]
        + ([dist / "SHA256SUMS"] if (dist / "SHA256SUMS").exists() else []))
    if not files:
        print(f"[release] 警告：dist/release 下没有 jishi-{VERSION}-* 发行包，"
              f"跳过附件上传（先跑 python tools/build_release.py）")
        return
    upload_release_assets(token, rel["id"], files)
    print(f"[release] ✅ Release 完成：{tag}")


def _release_body(version: str) -> str:
    """Release 说明（从 CHANGELOG 摘取 + 下载安装指引）。"""
    return f"""# 基石 jishi {version}

一门**像 Python 一样简单易用的中文编程语言**：报错全中文、
能直用 Python 生态、能被大模型安全调用。

这是首个对外发行版，M0–M16 全部里程碑完成。

## 下载与安装

| 平台 | 文件 |
|------|------|
| Windows x64 | `jishi-{version}-windows-x64.zip` |

1. 下载上面的 zip 与 `SHA256SUMS`（校验完整性）
2. 解压后运行安装脚本：
   ```
   powershell -ExecutionPolicy Bypass -File install.ps1
   ```
   （默认装到 `~/.jishi`，自动把 `bin` 加入用户 PATH）
3. **新开终端**，验证：`jishi --version`
4. 写第一个程序：
   ```
   令 名字 = "世界"
   打印("你好，" + 名字)
   ```
   保存为 `hello.jsh`，运行 `jishi hello.jsh`

开发者也可走 pip：`pip install jishi`

## 主要特性

- **中文语法**：缩进分块，报错精确到行列 +「你是不是想写…」修正建议
- **三执行器**：树遍历 / Python 字节码 VM / C 字节码 VM（107 项对拍护航）
- **Python 生态桥接**：`导入 x 从 python` 直用整个 Python 生态，关键字参数保中文
- **14 个标准库**：随机/数学/时间/文件/表格/文本/json/日期/正则/网络/路径/加密/压缩/系统
- **AI 原生（北极星）**：语言卡（`--ai-card`）/ 机器可读语言规格（`--lang-spec`）/
  结构化报错（`--json-errors`，带 fix 自愈）/ 沙箱 / MCP Server
- **包管理**：`jishi 安装/卸载/列表`，依赖递归解析 + 版本约束 + 循环检测
- **跨语言分发**：`--dump-bytecode` 产出 JSON 字节码，Node/Rust 宿主直嵌
- **首次体验**：`jishi 新项目 名字` 脚手架 / `jishi 教程` / `jishi 医生` 环境自检

## 已知限制（诚实声明）

- 发行包体积约 16MB（内含 Python 运行时），大于 Go/Rust 编写的语言
- PyInstaller 产物可能被部分杀软误报，故同时提供 wheel / 源码两条途径
- 无切片语法（`[::-1]` 请用 `反转()`）、`最小`/`最大` 只收单列表、无 argv
- Rust 宿主为「最小可运行」；Linux/macOS 发行包依赖 CI，本次仅提供 Windows x64

559 项测试全部通过。详见仓库 README.md 与 CHANGELOG.md。
"""


if __name__ == "__main__":
    sys.exit(main())
