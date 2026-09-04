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

COMMIT_MSG = """基石（jishi）：中文编程语言——M0-M11 全部里程碑

语言核心
- 中文词法/语法/AST，中文报错精确到行列 +「你是不是想写…」修正建议
- 异常处理（尝试/捕获/最终/抛出）+ 面向对象（类/继承/新建）
- M7 表达力：多赋值解包、默认参数、列表/字典推导式、反引号文本插值
- M7.5 容错解析：误写英文关键字/内建（def、print、True…）→ 给中文对应建议

三执行器（语义逐字节一致，107 项对拍护航）
- 树遍历解释器 / Python 字节码 VM / C 字节码 VM（gcc 编译的 jsvm 内核）
- 纯计算场景 C VM 提速 1.6x–47x（字符串拼接为 HOST 句柄架构边界，如实标注）

AI 原生（北极星：最容易被大模型正确生成、安全执行、自我修复）
- jishi --ai-card 语言卡 / --lang-spec 机器可读语言规格 / --json-errors 结构化报错
- 报错带 fix 字段，agent 可程序化文本替换自愈
- 沙箱运行器（超时/输出上限/模块白名单）+ MCP Server（run_script/eval_expr）
- llms.txt + few-shot 示例库 + 官方系统提示词 + JishiEval 评测集

生态与工程
- Python 生态桥接：导入任意 Python 库，关键字参数保中文，异常中文化
- 标准库 10 模块（随机/数学/时间/文件/表格/文本/json/日期/正则/网络）+ 教程 7 章
- 本地包管理雏形、浏览器 Playground（pyodide）、VSCode 扩展、零依赖文档站
- C VM 公开 ABI（cvm/include/jsvm.h）+ wheel 内带预编译 DLL，pip 装即含 C VM

499 项测试全部通过"""


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

    # 4. README：行级过滤个人路径 + 路线图节泛化 + 去除路线图文件引用
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
        if "路线图.md" in l:                      # 内部规划文件引用 → 删
            continue
        out.append(l)
    src = "\n".join(out)
    head, sep, rest = src.partition("## 路线图")
    if sep:
        _, _s2, tail = rest.partition("## 设计原则")
        src = head + (
            "## 路线图\n\n"
            "M0–M11 已全部完成（见「当前进度」）。后续版本将持续打磨语法表达力、\n"
            "扩充标准库生态、优化执行性能，并加强对 AI 编程助手场景的支持——\n"
            "让基石更容易被大模型正确生成、安全执行、自我修复。\n\n"
        ) + "## 设计原则" + tail
    io.open(p, "w", encoding="utf-8", newline="\n").write(src)

    # 5. 文档站生成器：移除路线图页（文件不上传，否则 build_site.py 会缺文件报错）
    p = snapshot / "tools" / "build_site.py"
    if p.exists():
        s = io.open(p, encoding="utf-8").read()
        s = s.replace('    (Path("docs/路线图.md"), "路线图（M7–M11）", "参考"),\n', "")
        s = s.replace('    "docs/路线图.md": "M7–M11 后续规划",\n', "")
        io.open(p, "w", encoding="utf-8", newline="\n").write(s)

    # 6. 终检：快照里不允许再出现任何隐私模式 / 失效引用
    bad = []
    for f in snapshot.rglob("*"):
        if not f.is_file():
            continue
        if f.name == "publish_gitee.py":      # 发布工具自身会提到这些名字
            continue
        try:
            text = io.open(f, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        for pat in ("17719", "CLion", "GITEE_TOKEN"):
            if pat in text:
                bad.append(f"{f} 含 {pat!r}")
        if f.suffix in (".md", ".py") and "路线图.md" in text:
            bad.append(f"{f} 引用了未发布的 docs/路线图.md（会成死链）")
    if bad:
        raise SystemExit("隐私/引用终检未通过：\n" + "\n".join(bad))


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
    _git("init", "-q", "-b", BRANCH, cwd=snapshot)
    _git("add", "-A", cwd=snapshot)
    _git("-c", f"user.name={AUTHOR_NAME}", "-c", f"user.email={AUTHOR_EMAIL}",
         "commit", "-q", "-m", COMMIT_MSG, cwd=snapshot)

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

    import shutil
    shutil.rmtree(snapshot, ignore_errors=True)
    print("✓ 已同步（含屏蔽）并清理临时快照")
    return 0


if __name__ == "__main__":
    sys.exit(main())
