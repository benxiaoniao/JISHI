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
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_HTTPS = "https://github.com/benxiaoniao/JISHI.git"
BRANCH = "master"

#: **不上传的内部文档**：`(文件名, 抹引用时用的中性标签)`。
#: 为什么不上传：它们含未定稿的规划与决策，过早公开会绑住手脚；
#: 其中路线图还含自建分发服务器的实例 ID / 公网 IP。
#: ⚠️ **按名字长度降序**排（长的先替换），否则短的会把长名字切掉一半。
#: ⚠️ **按文件名匹配、且递归查找**（2026-10-08 起这些文档归档在
#: `docs/版本历史/`）—— 改了名字或再搬位置，这里必须跟着改，否则会推到公开仓库。
INTERNAL_DOCS = (
    ("架构迁移评估.md", "内部架构迁移评估（未公开）"),
    ("架构迁移路径.md", "内部架构迁移路径（未公开）"),
    ("架构评估.md", "内部架构评估（未公开）"),
    # 夹具文档与迁移路径同族：都在讲「未定稿的迁移方法」，且其中提到的
    # 「CI 尚未修复的失败」不适合对外。工具本身（tools/conformance.py）与
    # 基线（tests/conformance/）是公开的 —— 内部的是**这份说明**。
    ("一致性夹具.md", "内部一致性夹具说明（未公开）"),
    # 前端契约与迁移路径同族：它规定的是「R 线的接口」，R 线定型前不外发。
    # （内容是纯技术规格、不含隐私；纯粹是「未定稿」的问题。）
    ("前端契约.md", "内部前端契约（未公开）"),
)
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

#: 证书校验过不去时的可操作提示（实测踩过的两种）
_TLS_HINTS = (
    "CRYPT_E_NO_REVOCATION_CHECK", "吊销",
    "unable to get local issuer certificate", "SSL certificate problem",
    "CONNECT tunnel failed", "Connection was reset",
)


def _insecure() -> bool:
    """是否关掉 git 的 TLS 校验（`JISHI_GIT_INSECURE=1`）。"""
    return os.environ.get("JISHI_GIT_INSECURE", "").strip().lower() in (
        "1", "true", "yes", "on")


# --------------------------------------------------------------------------
# git 的网络通道：**直连优先，代理兜底**
#
# 2026-10-09 起本机的实情（实测）：
#   * **只有 GitHub 各域名**被编辑器/连接器的透明代理劫持到 `127.0.0.1`，
#     其它域名解析正常 —— 而那个本地代理**对 GitHub 一律 502**；
#   * **直连 GitHub 真实 IP 是通的**（`curl --resolve github.com:443:<真IP>` → 200）；
#   * 麻烦在于 `git` 不支持 `curl --resolve`，也没有 `curloptResolve` 配置项，
#     hosts 又不可写 —— 所以**直连这条路要替 git 补一层**（见 `tools/github_relay.py`）。
#
# 于是分三档，按优先级：
#   ① `JISHI_GIT_PROXY` 显式给了代理 → 用它（**备案通道**：将来网络走代理时启用）；
#   ② 系统解析正常（不是 127.x）→ **什么都不做，直接连**（最干净）；
#   ③ 被劫持 → 自动起本地中继（`github_relay`），把 git 的代理指过去。
#      ⚠️ 中继只搬字节，**TLS 端到端**（SNI 仍是 github.com），不是「关证书校验」。
# --------------------------------------------------------------------------

#: 进程内复用的中继：(server, port)。
_RELAY: tuple[object, int] | None = None
#: 追加给 git 子进程的网络环境变量（由 `_setup_git_transport()` 填）。
_PROXY_ENV: dict[str, str] = {}
#: 本次用的通道名（打印用）。
_TRANSPORT = "直连"


def _is_loopback(ip: str) -> bool:
    return ip.startswith("127.") or ip == "::1"


def _github_hijacked() -> bool:
    """GitHub 域名是否被劫持到回环地址。"""
    try:
        return _is_loopback(socket.gethostbyname("github.com"))
    except OSError:
        return False


def _start_relay() -> tuple[object, int]:
    """起本地 CONNECT 中继（`tools/github_relay.py`）。"""
    global _RELAY
    if _RELAY is not None:
        return _RELAY
    sys.path.insert(0, str(ROOT / "tools"))
    import github_relay                                    # noqa: PLC0415
    server, port = github_relay.start()
    _RELAY = (server, port)
    return _RELAY


def _setup_git_transport(*, quiet: bool = False) -> None:
    """决定 git 走哪条路，并备好环境变量（幂等）。"""
    global _TRANSPORT
    env = os.environ

    explicit = (env.get("JISHI_GIT_PROXY") or "").strip()
    if explicit:
        _PROXY_ENV.update({
            "http_proxy": explicit, "https_proxy": explicit,
            "HTTP_PROXY": explicit, "HTTPS_PROXY": explicit,
            "all_proxy": explicit, "ALL_PROXY": explicit,
        })
        _TRANSPORT = f"代理（JISHI_GIT_PROXY={explicit}）"
        if not quiet:
            print(f"   网络通道：{_TRANSPORT}")
        return

    if not _github_hijacked():
        _TRANSPORT = "直连"
        if not quiet:
            print("   网络通道：直连（GitHub 域名解析正常）")
        return

    # 被劫持：起本地中继 —— 本质仍是**直连真实 IP**，只是替 git 补上 --resolve
    _, port = _start_relay()
    url = f"http://127.0.0.1:{port}"
    _PROXY_ENV.update({
        "http_proxy": url, "https_proxy": url,
        "HTTP_PROXY": url, "HTTPS_PROXY": url,
        "all_proxy": url, "ALL_PROXY": url,
    })
    # 清掉继承来的 no_proxy，免得连同 127.0.0.1 一起被排除
    for k in ("no_proxy", "NO_PROXY"):
        _PROXY_ENV[k] = ""
    _TRANSPORT = f"本地中继（直连 GitHub 真 IP，段 {port}）"
    if not quiet:
        print(f"   网络通道：{_TRANSPORT}")


def _stop_relay() -> None:
    if _RELAY is None:
        return
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import github_relay                                # noqa: PLC0415
        github_relay.stop(_RELAY[0])                       # type: ignore[arg-type]
    except Exception:                                      # noqa: BLE001
        pass


def _git_env() -> dict:
    """git 子进程的环境。

    ⚠️ **两件独立的事，别混**：

    1. **网络通道**（`_PROXY_ENV`，由 `_setup_git_transport()` 决定）——
       直连 / 本地中继 / `JISHI_GIT_PROXY` 代理，三档之一。
    2. **TLS 校验**（`JISHI_GIT_INSECURE=1`）—— 本机走中转代理时 schannel 的
       吊销检查过不去（`CRYPT_E_NO_REVOCATION_CHECK`），换 openssl 后端则报
       「找不到签发者」；实测 `GIT_SSL_NO_VERIFY=1` 能通。**默认不开**
       （关校验 = 降安全性，不该是默认；CI/Linux 也不需要）。

    ⚠️ 中继那档**不需要**关校验：TLS 是端到端的，证书仍然正常校验
    （实测中继档下 `git ls-remote` 不带 `sslVerify=false` 也通）。
    """
    env = dict(os.environ)
    env.update(_PROXY_ENV)
    if _insecure():
        env["GIT_SSL_NO_VERIFY"] = "true"
    return env


def _push_hint(stderr: str) -> str:
    """把 git 的英文报错翻成「下一步做什么」。"""
    net = ("CONNECT tunnel failed", "Could not connect to server",
           "Failed to connect", "502", "Connection was reset")
    if any(h in stderr for h in net):
        return ("\n💡 看着像**网络通道**问题。三档按优先级，任选其一："
                "\n   ① 直连（默认；GitHub 域名解析正常时就是它）；"
                "\n   ② 本地中继（域名被劫持到 127.0.0.1 时脚本会自动起，"
                "也可单独跑 `python tools/github_relay.py --port 8899` 排错）；"
                "\n   ③ 走代理：`JISHI_GIT_PROXY=http://127.0.0.1:<端口> "
                "python tools/publish_github.py`（**备案通道**，网络得走代理时启用）。")
    if any(h in stderr for h in _TLS_HINTS):
        return ("\n💡 看着像**证书**问题（中转代理做 TLS 时的常见坑）。"
                "试：JISHI_GIT_INSECURE=1 python tools/publish_github.py"
                "\n   —— 它只是关掉证书校验，快照与屏蔽逻辑不变。")
    return ""


def _git(*args: str, cwd: Path | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       env=_git_env())
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args[:2])} 失败：\n"
                         f"{r.stderr}{_push_hint(r.stderr)}")
    return r.stdout


def _sanitize(snapshot: Path) -> None:
    """隐私屏蔽（每步幂等，重复运行安全）。

    2026-09-23 调整：**`docs/路线图.md` 不再上传**（作者定）——删文件 + 抹掉
    指向它的引用（理由见模块 docstring）。个人机器路径 / CLion / 令牌 /
    服务器地址等照旧屏蔽。
    """
    # 1. 构建脚本：移除个人机器的 CLion gcc 路径条目
    #    ⚠️ **在这里集中列出**（2026-10-01 R2 起）：下次新加一个 crate
    #    又要写一份 gcc/dlltool 候选路径时，**只改这一行** —— 漏了的话
    #    终检会以「含 'CLion'」为由直接中止发布，等于白跑一趟。
    for _rel in ("cvm/build.py", "core/build.py"):
        p = snapshot / _rel
        if not p.exists():
            continue
        lines = [l for l in io.open(p, encoding="utf-8").read().split("\n")
                 if "CLion" not in l]
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
    #    - `docs/路线图*.md`（2026-09-23 定，2026-09-29 扩成通配）：含自建分发
    #      服务器的实例 ID / 公网 IP / 地域与规格。**按版本期归档的
    #      `路线图-归档-*.md` 同样含这些信息**，所以这里用通配一网打尽，
    #      以后再加归档文件也不会漏。
    #    - `docs/架构评估.md`（2026-09-25）：含里程碑编号规划（M51–M53）与
    #      「哪些不砍、哪些要迁移」的**未定稿**技术决策——属内部讨论材料，
    #      对外过早公开会绑住手脚（用户还没拍板）。
    # ⚠️ 2026-10-08：内部文档**归档进了 `docs/版本历史/`**，所以匹配一律要**递归**
    #    （`rglob`）—— 否则排不掉，而排不掉就是**把服务器实例信息推到公开仓库**。
    for rm in sorted(snapshot.rglob("路线图*.md")):
        rm.unlink()
    # ⚠️ 内部文档一律**两手**：删文件 + 抹掉别处指向它的引用（否则公开仓库里全是
    #    死链）。以后新增内部文档**只改这一处**，别再散落着写正则。
    #    路径前缀同时允许 `docs/`、`../`、`docs/版本历史/` 三种写法。
    _PREFIX = r"(?:\.\./|docs/)?(?:版本历史/)?"
    _targets = []
    for _name, _label in INTERNAL_DOCS:
        for _p in sorted(snapshot.rglob(_name)):
            _p.unlink()
        _stem = _name[:-3]
        _targets.append((
            re.compile(r"\[[^\]]*?" + _stem + r"[^\]]*?\]\(" + _PREFIX + _stem + r"\.md\)"),
            re.compile(r"`+" + _PREFIX + _stem + r"\.md`+"),
            re.compile(_PREFIX + _stem + r"\.md"),
            _label))
    # ⚠️ 字符类里**必须允许 `.`**：归档文件名是 `路线图-归档-0.1.md`，
    #    只写 `[\w\-]*` 匹配不到 `0.1` 那一段 ⇒ 引用抹不掉，公开仓库里留死链。
    _R = r"路线图[\w.\-]*\.md"
    _link = re.compile(r"\[[^\]]*?路线图[^\]]*?\]\(" + _PREFIX + _R + r"\)")
    _code = re.compile(r"`+" + _PREFIX + _R + r"`+")
    _plain = re.compile(_PREFIX + _R)
    for f in sorted(snapshot.rglob("*")):
        if not f.is_file() or f.name.startswith("publish_"):
            continue
        # 🔴 **只改文档（`.md`）**。以前这里把 `.py`/`.json`/`.txt` 也算进来，
        #    结果**把测试里的断言字符串也改了** —— 典型：
        #    `tests/test_m58_frontend_contract.py` 断言「`前端契约.md` 在屏蔽名单里」，
        #    快照里被抹成中性标签后断言**必失败**，而**本地源码树没有这一步、
        #    所以永远绿** —— CI 红了很久都查不出原因（2026-10-09 才逮到）。
        #    抹引用的目的是「公开文档里不留死链」，那是 `.md` 的事；
        #    源码/测试里的路径字面量不是链接，也不该被改。
        if f.suffix.lower() != ".md":
            continue
        try:
            s = io.open(f, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        # 顺序：先整条链接 → 再去内联代码 → 最后兜底裸路径
        new = _plain.sub("内部路线图（未公开）",
                         _code.sub("内部路线图（未公开）",
                                   _link.sub("内部路线图（未公开）", s)))
        # _targets 按名字长度降序（长的先替换，避免短的把长名字切走）
        for _link_re, _code_re, _plain_re, _label in _targets:
            new = _plain_re.sub(_label, _code_re.sub(_label, _link_re.sub(_label, new)))
        if new != s:
            io.open(f, "w", encoding="utf-8", newline="\n").write(new)

    # 4.1 README「项目结构」是**文件清单**：里面不该出现「（未公开）」这种标签
    #     （清单里混进一句替代语，读着像文件名）。
    #     2026-10-08 起 README 的项目结构**只列公开文档**，所以这里从「修补」
    #     改成**检查** —— 见下面第 5 步的终检（`README 里出现了（未公开）`）。

    # 5. 终检：快照里不允许再出现任何隐私模式
    bad = []
    # 5.0 内部文档**一个都不能留**（含归档目录）—— 这回是**递归**找
    for _name, _label in INTERNAL_DOCS:
        for _p in sorted(snapshot.rglob(_name)):
            bad.append(f"{_p.relative_to(snapshot)} 仍在快照里（本文件不上传）")
    for _p in sorted(snapshot.rglob("路线图*.md")):
        bad.append(f"{_p.relative_to(snapshot)} 仍在快照里（含服务器实例信息，不上传）")
    # 5.0b README 的「项目结构」是文件清单，不该混进替代标签
    _readme = snapshot / "README.md"
    if _readme.exists() and "（未公开）" in io.open(_readme, encoding="utf-8").read():
        bad.append("README.md 里出现了「（未公开）」标签"
                   "（项目结构清单混进了内部文档名）")
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
    _setup_git_transport()
    if _insecure():
        print("⚠️ JISHI_GIT_INSECURE=1：本次**关掉了 git 的证书校验**"
              "（代理做 TLS 中转时的应急手段；不影响快照与屏蔽逻辑）")
    r = subprocess.run(
        ["git", "push", "-f", REPO_HTTPS, f"{BRANCH}:{BRANCH}"],
        cwd=snapshot, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=_git_env())
    if r.returncode != 0:
        raise SystemExit(f"推送失败：\n{r.stderr}{_push_hint(r.stderr)}")
    print("    ✓ 代码已同步")

    # 可选：在屏蔽版快照的孤儿提交上打 tag，触发 Release 工作流
    if tag:
        _git("tag", "-f", tag, cwd=snapshot)
        r = subprocess.run(
            ["git", "push", "-f", REPO_HTTPS, f"refs/tags/{tag}"],
            cwd=snapshot, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=_git_env())
        if r.returncode != 0:
            raise SystemExit(f"推送 tag 失败：\n{r.stderr}"
                             f"{_push_hint(r.stderr)}")
        print(f"    ✓ 已打 tag {tag}（将触发 GitHub Actions 构建三平台发行包）")

    import shutil
    shutil.rmtree(snapshot, ignore_errors=True)
    _stop_relay()
    print("✓ 已同步（含屏蔽）并清理临时快照")
    return 0


if __name__ == "__main__":
    sys.exit(main())
