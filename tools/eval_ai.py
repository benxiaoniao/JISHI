# -*- coding: utf-8 -*-
"""M45 目标一：AI 跑分表 —— 度量「模型能不能用基石写对程序」。

## 度量什么

对每道题，让模型只看**语言卡 / 语言规格**（不看任何本仓库源码、不看题解），
写一段基石代码；然后**真跑基石解释器**，比对标准输出。跑错就把**中文报错**
回灌给模型让它改，最多 `--最大轮数` 轮。

三个指标：

| 指标 | 含义 |
|---|---|
| **一次通过率** | 第 1 轮就输出正确的题目占比 |
| **最终通过率** | 允许回灌报错改 N 轮后正确的占比 |
| **修复轮数** | 通过的题目平均用了几轮（1.0 = 全都一次过） |

## 为什么要「真跑」

模型说「我写好了」不算数，编译通过也不算数——**只有输出逐字对得上才算对**。
判据同时看退出码：程序崩了但崩溃前恰好输出了正确内容（例如「先打印了 3 行
再越界」）**不算通过**。这条不是洁癖：M45 写题集时就撞见过一次
（`当 i <= 长度(数据)` 那道题输出对了、但退出码 1）。

## 用法

    # 先看题集里有什么、自己能跑通吗（不花钱）
    python tools/eval_ai.py --自检

    # 跑分（需要 API key）
    python tools/eval_ai.py --模型 deepseek-v4-flash-0731
    python tools/eval_ai.py --模型 deepseek-v4-flash-0731 --模型 glm-5.3-flash

    # 只跑某一类题、只看前 3 题（省额度）
    python tools/eval_ai.py --类型 语法 --限制 3

    # 只评题集本身、不调用模型 → 用于 CI
    python tools/eval_ai.py --校验题集

key 走环境变量 `JISHI_EVAL_KEY`（**不落盘、不打印、不进 git**），
也支持 `--key` 显式传入（仅调试用，会被 shell 历史记录，别在正式跑分时用）。
接口地址走 `JISHI_EVAL_BASE`，默认 `https://tokenrhythm.studio/v1`。

## 诚实约定

- **没跑就说没跑**：不调模型时不产出跑分表，只产出「未度量」。
- **额度不够不猜**：某题因网络/额度失败，记为 `跳过` 并计入报告，**不算通过也不算失败**。
- 题集本身的自洽性由 `--校验题集` 保证（缺陷代码确实会失败、重构原始代码确实能跑对）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
import urllib.error
from concurrent.futures import ThreadPoolExecutor
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
TASKS_DIR = ROOT / "evals" / "tasks"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
if not PYTHON.is_file():  # 非 Windows / 未建 venv 时退回当前解释器
    PYTHON = Path(sys.executable)

DEFAULT_BASE = os.environ.get("JISHI_EVAL_BASE", "https://tokenrhythm.studio/v1")

#: 题集文件（顺序 = 报告里的分节顺序）
# ⚠️ **不要写成固定清单**（2026-10-24 踩到）：原来这里是四个文件名的元组，
#    于是新增 `05_测试与包.json` 之后 —— 判据（`test_m45`）认得它，**跑分不认**，
#    结果「题库 80 题」实际只跑了 **76 题**，而报告里写着「80 题」也没人发现。
#    ⇒ 改成**扫目录**：加文件就自动算进来（`test_m45` 仍然钉着「该有哪几个文件」，
#      那个清单是用来抓「文件被误删/误加」的，两件事分工不同）。
def task_files() -> list[tuple[str, str]]:
    """题库目录下的所有 `*.json`（按文件名排序，顺序稳定）。"""
    return [(p.name, "") for p in sorted(TASKS_DIR.glob("*.json")) if p.is_file()]


# ---------------------------------------------------------------------------
# 题集装载
# ---------------------------------------------------------------------------

@dataclass
class Task:
    """一道题。`起始代码` 为空表示「从零写」；非空表示「改这段代码」。"""

    id: str
    类型: str
    题目: str
    标准输出: str
    考点: str = ""
    起始代码: str = ""      # 调试题=有缺陷代码；重构题=原始代码
    #: 跑之前要铺在**工作目录**里的文件（名字 → 内容）。
    #: 🔴 为什么需要它（2026-10-24）：有题目要读一个 `包.json`，
    #:    而评测器原来把答案跑在仓库根、什么都不铺 ⇒ 那题**根本无法解答**，
    #:    模型再对也拿不到分，跑分表上看起来像「模型不会做」。**题错了不能算模型头上。**
    前置: dict = field(default_factory=dict)
    额外: dict = field(default_factory=dict)


def load_tasks(only_type: Optional[str] = None,
               limit: Optional[int] = None,
               只跑: Optional[list[str]] = None) -> list[Task]:
    tasks: list[Task] = []
    for fname, kind in task_files():
        path = TASKS_DIR / fname
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for raw in data.get("题集", []):
            t = Task(
                id=raw["id"], 类型=raw.get("类型", kind),
                题目=raw["题目"], 标准输出=raw["标准输出"],
                考点=raw.get("考点", raw.get("缺陷", "")),
                前置=dict(raw.get("前置") or {}),
            )
            t.起始代码 = (raw.get("有缺陷代码") or raw.get("原始代码") or "")
            t.额外 = {k: v for k, v in raw.items()
                      if k not in ("id", "类型", "题目", "标准输出", "考点",
                                   "有缺陷代码", "原始代码", "缺陷", "修法说明",
                                   "目标", "评分口径")}
            if only_type and t.类型 != only_type:
                continue
            # 补跑用：题号里含任一子串才跑（通道抖动跳了几道，不必整轮重跑 20 分钟）
            if 只跑 and not any(子 in t.id for 子 in 只跑):
                continue
            tasks.append(t)
    if limit:
        tasks = tasks[:limit]
    return tasks


# ---------------------------------------------------------------------------
# 跑基石（判分的唯一依据）
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False

    @property
    def 错误摘要(self) -> str:
        """给模型看的报错文本（优先 stderr 里的中文报错块）。"""
        text = self.stderr.strip()
        if not text:
            text = self.stdout.strip()
        # 截断，别把整屏都灌回去
        return text[:1200]


def run_jishi(code: str, timeout: float = 20.0,
              tag: str = "eval", 前置: Optional[dict] = None) -> RunResult:
    """把代码写成临时文件跑一遍。退出码非 0 一律算失败（见模块 docstring）。"""
    tmp = ROOT / ".tmp-eval"
    tmp.mkdir(exist_ok=True)
    # 有前置文件就**单独开一个工作目录**（答案里的相对路径 `包.json` 才找得到）；
    # 没有前置时仍然跑在仓库根 —— 别改默认行为，老题目可能依赖仓库根。
    cwd = ROOT
    目录 = None
    if 前置:
        目录 = tmp / _safe_name(tag or "eval")
        if 目录.exists():
            shutil.rmtree(目录, ignore_errors=True)
        目录.mkdir(parents=True)
        for 名, 内容 in 前置.items():
            (目录 / 名).write_text(str(内容), encoding="utf-8")
        cwd = 目录
    f = (目录 or tmp) / f"{_safe_name(tag)}.jsh"
    f.write_text(code, encoding="utf-8")
    try:
        p = subprocess.run(
            [str(PYTHON), "-m", "oracle.jishi.cli", str(f)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=cwd, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return RunResult(False, "", f"执行超时（{timeout} 秒）", -1, True)
    finally:
        try:
            f.unlink()
        except OSError:
            pass
        if 目录 is not None:
            shutil.rmtree(目录, ignore_errors=True)
    return RunResult(p.returncode == 0, p.stdout, p.stderr, p.returncode)


def _safe_name(s: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff-]+", "_", s)[:80]


def judge(code: str, want: str, timeout: float = 20.0,
          tag: str = "eval", 前置: Optional[dict] = None) -> tuple[bool, RunResult]:
    """判一题：输出逐字相同 **且** 退出码 0 才算通过。"""
    r = run_jishi(code, timeout, tag, 前置=前置)
    got = r.stdout.strip()
    want_s = want.strip()
    return (r.ok and got == want_s), r


# ---------------------------------------------------------------------------
# 题集自检（不花钱）
# ---------------------------------------------------------------------------

def self_check(tasks: list[Task], verbose: bool = True) -> list[str]:
    """校验题集自洽：缺陷代码确实失败、重构原始代码确实输出标准输出。

    返回问题列表（空 = 全通过）。这道检查防的是**题目本身写错**——
    比如「给一段有 bug 的代码让模型修」，而那段代码其实跑对了。
    """
    problems: list[str] = []
    for t in tasks:
        if not t.起始代码:
            continue
        ok, r = judge(t.起始代码, t.标准输出, tag=f"chk_{t.id}")
        if t.类型 == "调试":
            # 调试题：起始代码**必须**失败，否则题目无效
            if ok:
                problems.append(f"{t.id}：缺陷代码竟然直接跑出正确输出，题目无效")
        elif t.类型 == "重构":
            # 重构题：起始代码**必须**能跑出标准输出（题目才自洽）
            if not ok:
                problems.append(
                    f"{t.id}：原始代码跑不出标准输出（实际 {r.stdout.strip()[:60]!r}"
                    f"，rc={r.returncode}）")
        if verbose:
            flag = "✓" if not (t.类型 == "调试") == ok else "✓"
            print(f"    {flag} {t.id}")
    return problems


# ---------------------------------------------------------------------------
# LLM 调用（OpenAI 兼容 /chat/completions）
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_TEMPLATE = """你是基石（jishi）中文编程语言的程序员。

**基石是一门中文编程语言**，语法与 Python 接近，但**关键字、内建函数、标准库
全部是中文**。请严格遵守语言规格：**只写中文关键字**，绝不写 def/if/print/True
这类英文关键字。

== 语言规格开始 ==
{spec}
== 语言规格结束 ==

输出要求：
- **只输出基石代码本身**，不要 Markdown 代码围栏（不要 ```），不要任何解释。
- 代码要能直接保存成 .jsh 文件运行。
- 需要输出时用 打印(...)。"""


def build_system_prompt(spec_text: str) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(spec=spec_text)


def build_user_prompt(t: Task) -> str:
    lines = [f"任务：{t.题目}"]
    if t.起始代码:
        verb = "下面这段代码有缺陷，请修复它" if t.类型 == "调试" else "下面这段代码请按目标重写"
        lines.append(f"\n{verb}：\n{t.起始代码}")
    lines.append(f"\n要求：程序的输出必须逐字等于：\n{t.标准输出}")
    if t.考点:
        lines.append(f"\n（提示：{t.考点}）")
    return "\n".join(lines)


def build_fix_prompt(t: Task, code: str, err: str) -> str:
    return (
        f"你上一次写的代码没有通过。\n\n"
        f"上一次的代码：\n{code}\n\n"
        f"运行后的报错 / 输出：\n{err}\n\n"
        f"期望输出（必须逐字相同）：\n{t.标准输出}\n\n"
        f"请给出修正后的完整代码（只输出代码，不要解释、不要代码围栏）。"
    )


@dataclass
class LlmReply:
    text: str
    ok: bool
    error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


class _限速器:
    """按「每分钟最多多少次请求」把调用**铺开**（M121）。

    🔴 为什么需要它（实测）：单次请求约 **7.8 秒**，74 题串行光模型生成就要
    9.6 分钟起。而服务商的限额是 **RPM 50** —— 串行只跑得到 **7.7 次/分钟**，
    额度全浪费在「等模型」上。

    更糟的是以前只有「**题目之间** sleep(题间间隔)」：同一题的**第 2/3 轮是连发**的
    （回灌报错让模型改），一撞 429 就退避 3/6/12/24 秒 —— **越重试越慢**，
    而且等待还白占额度。

    ⇒ 两件事一起做：
    - **并发**（`--并发`）：把 RPM 额度用满；
    - **全局令牌桶**（本类）：每次**真实请求前**掐一下间隔，重试也管住。

    ⚠️ 锁里 sleep 是**故意**的：后面排队的线程拿到的「下次可发时刻」必须
    在醒来之后重新算，否则所有线程会同时醒、同时发（限速器的经典写错法）。
    """

    def __init__(self, 每分钟: float):
        # 0 或负数 = 不限速（本地脚本 / 限额很宽松时用）
        self.间隔 = 60.0 / 每分钟 if 每分钟 and 每分钟 > 0 else 0.0
        self._下次 = 0.0
        self._锁 = threading.Lock()

    def 等(self) -> None:
        if self.间隔 <= 0:
            return
        with self._锁:
            now = time.monotonic()
            if now < self._下次:
                time.sleep(self._下次 - now)
                now = time.monotonic()
            self._下次 = now + self.间隔


#: 遇到这些信号就退避重试（额度/频率类问题，等一等通常能过）
RETRYABLE = ("429", "RATE_LIMITED", "503", "SERVICE_BUSY", "502", "504",
             "timed out", "TimeoutError", "URLError", "ConnectionReset")


#: 退避的**上限**（秒）。指数退避原本是给 429 设计的（服务商让等），
#: 但 `HTTP 526` 这类 CDN 边缘错误**等多久都不会好** —— 26 次重试里有 8 次
#: 撞在同一个 526 上，指数退避一口气排到 **384 秒**，6 道补跑题烧了 10 分钟
#: 还一道没跑完（2026-10-29 实测）。⇒ 封顶 30 秒：多试几次比「等很久再试一次」
#: 更适合这类抖动。
退避上限秒 = 30


def _retryable(err: str) -> bool:
    """这个错误值不值得再试一次。

    🔴 **任何 5xx 都算**（2026-10-24 踩到）：原来只列了 502/503/504，
    漏掉 **526**（Cloudflare 的「源站证书无效」）—— 那是最典型的**边缘节点抖动**，
    等几秒重试通常就好了。漏掉的后果很隐蔽：那一批题被记成「跳过」，
    而「跳过」会混进跑分表，让人以为**模型不会做**（实际是通道打嗝）。
    📌 判据：**5xx 一定是对方的问题，不是模型的问题** ⇒ 一律重试。
    """
    if any(sig in err for sig in RETRYABLE):
        return True
    # 形如「HTTP 526」「HTTP 500」…… 一律重试（4xx 不重试：那是我们请求写错了）
    import re as _re
    return bool(_re.search(r"\bHTTP\s*5\d\d\b", err))


#: 真实请求计数（**含重试**）。
#:
#: 🔴 为什么要记这个（2026-10-24）：跑分慢的时候，得先分清是哪一种慢 ——
#:    **「请求次数多」**（题目要 2–3 轮修复，这是评测设计，不该砍）
#:    还是**「每次请求慢」**（通道 / 长 prompt）。
#:    没这个数就只能凭感觉调 `--并发` / `--每分钟`，调完也不知道有没有用。
#:    📌 经验值：全量 80 题 × 3 模型 × 平均 1.5–2 轮 ≈ **350–480 次**请求。
_计数 = {"请求": 0, "重试": 0}


#: 网关把「上游故障」**当成正文**塞回来的特征词（2026-10-29 加）。
#:
#: 真实踩到：GitCode AI 那边 Qwen3-4B 的上游挂了，它回的却是
#: **HTTP 200 + `{"content": "三方请求失败: 502 <html>…"}`** —— 上层看到
#: `ok=True`，就把这段 HTML 当成模型的代码去跑，必然跑不通，于是记成
#: 「模型不会做」。这比「跳过」坏得多：**会把通道故障算成模型的分数**。
#:
#: ⇒ 判据是「**短**且含特征词」：正常答案是一段基石代码，不会几十个字就
#:   只写一句 Bad Gateway（放宽到 400 字是为了容纳带说明的前言）。
通道故障特征 = ("三方请求失败", "Bad Gateway", "Authentication Error",
                "invalid_api_key", "token rejected")


def 网关故障(text: str) -> str:
    """这段「回答」其实是网关的错误页吗？是就返回原因（非空），否则空串。

    ⚠️ **判据是「开头」而不是「出现过」**：网关总是把错误放在**最前面**；
    而一段正常代码里完全可能（也应该）出现「`Bad Gateway` 按 502 处理」这种
    注释 —— 按「出现过」判会把**正确回答**当成通道故障跳过，那是更坏的错
    （静默吃掉真分数）。所以只认开头，外加一条「短且是 HTML 错误页」。
    """
    t = (text or "").strip()
    if not t:
        return ""
    for w in 通道故障特征:
        if t.startswith(w):
            return f"通道故障（网关把上游错误当正文返回）：{t[:120]}"
    if "<html" in t.lower() and len(t) <= 400:
        return f"通道故障（返回的是 HTML 错误页）：{t[:120]}"
    return ""


def call_llm(base: str, key: str, model: str, messages: list[dict],
             max_tokens: int = 4096, timeout: float = 75.0,
             retries: int = 4, quiet: bool = False,
             限速: "_限速器 | None" = None) -> LlmReply:
    """调一次 chat/completions，遇限流/临时故障按指数退避重试。

    跑一次全量题集要几十次调用，中途撞上 429 是常态（实测第一轮全量跑丢了
    22/45 题）。**把重试做进这一层**而不是让上层记「跳过」——否则跑分表里
    「跳过」的成因会混进「模型不行」的结论里，那是错的。
    """
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )

    last_err = ""
    for attempt in range(retries + 1):
        # ⚠️ **每一次**真实请求（含重试）都过令牌桶 —— 这才是「不撞 429」的关键。
        if 限速 is not None:
            限速.等()
        _计数["请求"] += 1
        if attempt > 0:
            _计数["重试"] += 1
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            last_err = f"HTTP {e.code}：{detail}"
            if _retryable(last_err) and attempt < retries:
                wait = min(2 ** attempt * 3, 退避上限秒)
                if not quiet:
                    print(f"      ⏳ 限流/故障，{wait}s 后重试"
                          f"（第 {attempt + 1}/{retries} 次）", flush=True)
                time.sleep(wait)
                continue
            return LlmReply("", False, last_err)
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}：{e}"
            if _retryable(last_err) and attempt < retries:
                wait = min(2 ** attempt * 3, 退避上限秒)
                if not quiet:
                    print(f"      ⏳ 网络异常，{wait}s 后重试", flush=True)
                time.sleep(wait)
                continue
            return LlmReply("", False, last_err)

        try:
            choice = body["choices"][0]["message"]
            text = choice.get("content") or ""
            usage = body.get("usage") or {}
        except (KeyError, IndexError, TypeError) as e:
            return LlmReply("", False, f"响应格式意外（{e}）：{str(body)[:200]}")

        if not text.strip():
            return LlmReply("", False,
                            "模型返回了空内容（可能是 max_tokens 太小或纯推理模型）")

        # ⚠️ 网关可能把上游故障当正文返回（HTTP 200 + 一段错误页）。这**不是**
        # 模型的回答，不能拿去判分 —— 否则「通道坏了」会变成「模型不行」。
        # 当作临时故障重试（与 429 同一套退避），重试完还这样就记「跳过」。
        故障 = 网关故障(text)
        if 故障:
            last_err = 故障
            if attempt < retries:
                wait = min(2 ** attempt * 3, 退避上限秒)
                if not quiet:
                    print(f"      ⏳ {故障[:40]}…，{wait}s 后重试"
                          f"（第 {attempt + 1}/{retries} 次）", flush=True)
                time.sleep(wait)
                continue
            return LlmReply("", False, 故障)

        return LlmReply(text, True,
                        prompt_tokens=usage.get("prompt_tokens", 0),
                        completion_tokens=usage.get("completion_tokens", 0))
    return LlmReply("", False, last_err or "重试耗尽")


def strip_code_fence(text: str) -> str:
    """模型偶尔还是加围栏，宽容一点剥掉（不剥的话判分必错）。"""
    s = text.strip()
    m = re.match(r"^```[a-zA-Z\u4e00-\u9fff]*\s*\n(.*?)\n?```$", s, re.S)
    if m:
        return m.group(1).strip()
    # 只有开头半截围栏的情况
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


# ---------------------------------------------------------------------------
# 跑分主循环
# ---------------------------------------------------------------------------

@dataclass
class TaskOutcome:
    task_id: str
    类型: str
    通过: bool
    轮数: int          # 第几轮通过（1 = 一次过）；未通过 = 最大轮数+1
    跳过: bool = False
    跳过原因: str = ""
    期望输出: str = ""
    实际输出: str = ""
    末次报错: str = ""
    代码: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


def eval_one(t: Task, base: str, key: str, model: str,
             spec_text: str, max_rounds: int,
             max_tokens: int, verbose: bool,
             interval: float = 0.0,
             retries: int = 4,
             eval_timeout: float = 75.0,
             限速: "_限速器 | None" = None,
             quiet: bool = False) -> TaskOutcome:
    """跑一道题：写 → 跑 → 错就回灌报错 → 再跑，最多 max_rounds 轮。"""
    messages = [
        {"role": "system", "content": build_system_prompt(spec_text)},
        {"role": "user", "content": build_user_prompt(t)},
    ]
    p_tok = c_tok = 0
    last_code = ""
    last_out = ""
    last_err = ""

    for rnd in range(1, max_rounds + 1):
        reply = call_llm(base, key, model, messages, max_tokens=max_tokens,
                         retries=retries, timeout=eval_timeout,
                         限速=限速, quiet=quiet)
        p_tok += reply.prompt_tokens
        c_tok += reply.completion_tokens
        if not reply.ok:
            return TaskOutcome(t.id, t.类型, False, rnd, True, reply.error,
                               t.标准输出, last_out, last_err, last_code,
                               p_tok, c_tok)

        code = strip_code_fence(reply.text)
        last_code = code
        ok, run = judge(code, t.标准输出, tag=f"{model}_{t.id}_r{rnd}",
                       前置=getattr(t, "前置", None))
        last_out = run.stdout.strip()
        last_err = run.错误摘要

        if ok:
            if verbose:
                print(f"    ✓ {t.id}（第 {rnd} 轮）")
            if interval:
                time.sleep(interval)
            return TaskOutcome(t.id, t.类型, True, rnd, False, "",
                               t.标准输出, last_out, "", code, p_tok, c_tok)

        if verbose:
            tag = "报错" if not run.ok else "输出不符"
            print(f"    ✗ {t.id} 第 {rnd} 轮（{tag}）")
        if rnd < max_rounds:
            messages.append({"role": "assistant", "content": reply.text})
            messages.append({"role": "user",
                             "content": build_fix_prompt(t, code, last_err)})

    if interval:
        time.sleep(interval)
    return TaskOutcome(t.id, t.类型, False, max_rounds + 1, False, "",
                       t.标准输出, last_out, last_err, last_code, p_tok, c_tok)


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def render_report(model: str, outcomes: list[TaskOutcome],
                  spec_hash: str, max_rounds: int,
                  started: str, elapsed: float) -> str:
    """产出 Markdown 跑分表。"""
    done = [o for o in outcomes if not o.跳过]
    skipped = [o for o in outcomes if o.跳过]
    passed = [o for o in done if o.通过]
    first_try = [o for o in done if o.通过 and o.轮数 == 1]

    def rate(a: int, b: int) -> str:
        return f"{a / b * 100:.1f}%（{a}/{b}）" if b else "—"

    lines = [
        f"## 模型：`{model}`",
        "",
        f"- **一次通过率**：{rate(len(first_try), len(done))}",
        f"- **最终通过率**（最多 {max_rounds} 轮）：{rate(len(passed), len(done))}",
        f"- **平均修复轮数**："
        + (f"{sum(o.轮数 for o in passed) / len(passed):.2f}" if passed else "—"),
    ]
    tok_p = sum(o.prompt_tokens for o in outcomes)
    tok_c = sum(o.completion_tokens for o in outcomes)
    if tok_p or tok_c:
        lines.append(f"- token 用量：prompt {tok_p} / completion {tok_c}")
    else:
        lines.append("- token 用量：未记录（本次由日志重建，日志不含 token 计数）")
    if skipped:
        lines.append(f"- ⚠️ **跳过 {len(skipped)} 题**（不算通过也不算失败）："
                     + "、".join(f"`{o.task_id}`（{o.跳过原因[:40]}）" for o in skipped))
    lines += ["", "| 题号 | 类型 | 结果 | 轮数 |", "|---|---|---|---|"]
    for o in outcomes:
        if o.跳过:
            cell = "跳过"
        elif o.通过:
            cell = "✅ 通过"
        else:
            cell = "❌ 未通过"
        rounds = "—" if o.跳过 else str(o.轮数)
        lines.append(f"| {o.task_id} | {o.类型} | {cell} | {rounds} |")

    # 失败详情（模型/题集改进的线索）
    fails = [o for o in done if not o.通过]
    if fails:
        lines += ["", "<details><summary>未通过详情</summary>", ""]
        for o in fails:
            lines += [
                f"**{o.task_id}**", "",
                f"- 期望输出：`{_oneline(o.期望输出)}`",
                f"- 实际输出：`{_oneline(o.实际输出)}`",
                f"- 报错 / 输出：`{_oneline(o.末次报错)}`",
                "", "<details><summary>模型最后写的代码</summary>", "",
                "```jsh", o.代码, "```", "", "</details>", "",
            ]
        lines += ["</details>", ""]

    lines += [
        "", "---", "",
        f"- 语言规格指纹：`{spec_hash}`（同一指纹下结果才可比）",
    ]
    # 从日志重建报告时耗时拿不到（日志没记）——如实省略，不印「0.0 秒」当数据。
    if elapsed > 0:
        lines.append(f"- 开始时间：{started}；本模型耗时 {elapsed:.1f} 秒")
    else:
        lines.append(f"- 开始时间：{started}；本模型耗时未记录")
    lines += [
        f"- 判分口径：输出逐字相同 **且** 退出码为 0",
        "",
    ]
    return "\n".join(lines)

def _oneline(s: str) -> str:
    s = (s or "").replace("\n", "\\n")
    return s[:120] + ("…" if len(s) > 120 else "")


def summarize_table(all_results: dict[str, list[TaskOutcome]],
                    max_rounds: int) -> str:
    """多模型横向对比表。"""
    lines = ["## 跑分汇总", "",
             "| 模型 | 一次通过率 | 最终通过率 | 平均修复轮数 | 跳过 |",
             "|---|---|---|---|---|"]
    for model, outs in all_results.items():
        done = [o for o in outs if not o.跳过]
        passed = [o for o in done if o.通过]
        first = [o for o in done if o.通过 and o.轮数 == 1]
        rate = lambda a, b: f"{a / b * 100:.1f}%（{a}/{b}）" if b else "—"  # noqa: E731
        avg = (f"{sum(o.轮数 for o in passed) / len(passed):.2f}"
               if passed else "—")
        lines.append(
            f"| `{model}` | {rate(len(first), len(done))} | "
            f"{rate(len(passed), len(done))} | {avg} | "
            f"{len(outs) - len(done)} |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="基石 AI 跑分表（M45）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--模型", action="append", default=[],
                    help="模型 id，可重复传多个（如 deepseek-v4-flash-0731）")
    ap.add_argument("--类型", default=None,
                    help="只跑某一类：语法 / 标准库 / 调试 / 重构")
    ap.add_argument("--限制", type=int, default=None, help="只跑前 N 题")
    ap.add_argument("--只跑", default=None,
                    help="只跑题号里含这些子串的题（逗号分隔）。用来**补跑**被通道"
                         "抖动跳掉的那几道，不必整轮重来")
    ap.add_argument("--最大轮数", type=int, default=3,
                    help="失败后回灌报错最多改几轮（默认 3）")
    ap.add_argument("--最大令牌", type=int, default=8192,
                    help="单次调用 max_tokens（推理模型要给够，默认 8192）")
    # 🔴 M121 把「避限流」的方式换了（实测数据见 `_限速器` 的文档）：
    #    单次请求 ~8 秒 ⇒ **串行根本用不到 RPM 额度**，而题内第 2/3 轮又是连发的
    #    ⇒ 一撞 429 就退避 3/6/12/24 秒，越重试越慢。
    #    现在由 `--每分钟`（全局令牌桶，连重试都管）+ `--并发`（把额度用满）负责，
    #    `--题间间隔` 退化成「每题之间额外歇一下」，默认 0。
    ap.add_argument("--并发", type=int, default=1,
                    help="同时跑几道题（默认 1=串行）。配合 --每分钟 用：服务商按"
                         "「每分钟请求数」限流，串行时单次就要 ~8 秒，额度用不上")
    ap.add_argument("--每分钟", type=float, default=45.0,
                    help="**全局**请求速率上限（请求/分钟，默认 45）。按服务商的 RPM "
                         "填、留一成余量；0 = 不限（会撞 429 退避，反而更慢）")
    ap.add_argument("--题间间隔", type=float, default=0.0,
                    help="每题结束后额外再等几秒（默认 0）。限流由 --每分钟 管，"
                         "这个只在对方要求「慢一点」时才用")
    ap.add_argument("--重试", type=int, default=4,
                    help="撞限流/临时故障时的退避重试次数（默认 4，退避 3/6/12/24 秒）")
    ap.add_argument("--超时", type=float, default=75.0,
                    help="单次调用的秒数上限（沙箱里偶发挂起，等满 180 秒太亏；默认 75）")
    ap.add_argument("--规格", choices=("json", "card"), default="json",
                    help="喂给模型的「语言事实」形态：json = 完整语言规格（默认，与 "
                         "M45 那组可比）；card = 语言卡（M116 起带签名，可配 --卡分片）")
    ap.add_argument("--卡分片", default=None,
                    help="配 --规格 card：只带某一个模块（如 文本）或一个任务包"
                         "（如 表格）—— 用来量「分片卡够不够用」")
    ap.add_argument("--key", default=None, help="API key（默认读 JISHI_EVAL_KEY）")
    ap.add_argument("--base", default=DEFAULT_BASE, help="接口地址")
    ap.add_argument("--节标题", default=None,
                    help="插进跑分表时那一节的标题（默认 `## 日期（N 题）`）")
    ap.add_argument("--写报告", action="store_true",
                    help="局部跑（--限制/--类型）也写正式跑分表。默认不写 —— "
                         "探路的 2 题通过率会覆盖掉历史记录，那是毁档案")
    ap.add_argument("--输出", default=None, help="报告写到哪个文件")
    ap.add_argument("--自检", action="store_true",
                    help="只校验题集自洽性，不调用模型（不花钱）")
    ap.add_argument("--校验题集", action="store_true", help="同 --自检")
    ap.add_argument("-v", "--verbose", action="store_true", help="逐题打印进度")
    args = ap.parse_args(argv)

    只跑们 = [x.strip() for x in (args.只跑 or "").split(",") if x.strip()]
    tasks = load_tasks(args.类型, args.限制, 只跑们 or None)
    if not tasks:
        print("题集为空——检查 evals/tasks/*.json 是否存在", file=sys.stderr)
        return 2

    print(f"题集：{len(tasks)} 题"
          + (f"（类型={args.类型}）" if args.类型 else "")
          + (f"（前 {args.限制} 题）" if args.限制 else ""))

    # ---- 题集自检（--自检 / --校验题集）----
    if args.自检 or args.校验题集:
        print("\n校验题集自洽性（不调用模型）：")
        problems = self_check(tasks, verbose=args.verbose)
        if problems:
            print(f"\n✗ 发现 {len(problems)} 个问题：")
            for p in problems:
                print(f"  - {p}")
            return 1
        print("\n✓ 题集自洽：缺陷代码确实失败、重构原始代码确实跑通")
        return 0

    # ---- 跑分 ----
    key = args.key or os.environ.get("JISHI_EVAL_KEY", "")
    if not key:
        print("缺少 API key：设环境变量 JISHI_EVAL_KEY，或用 --key 传入。\n"
              "（只想校验题集就用 --自检，不需要 key）", file=sys.stderr)
        return 2
    if not args.模型:
        print("没有指定模型：用 --模型 传至少一个。", file=sys.stderr)
        return 2

    from oracle.jishi.ai import build_lang_spec, content_hash, render_ai_card
    spec = build_lang_spec()
    spec_hash = content_hash(spec)
    if args.规格 == "card":
        # M116：语言卡这一路 —— 它是「贴进系统提示词」的紧凑形态，
        # 与规格 JSON 是**两条通道**；用同一个指纹标注，便于对照。
        模块 = 任务 = None
        if args.卡分片:
            if args.卡分片 in __import__("oracle.jishi.ai", fromlist=["x"]).卡_任务表:
                任务 = args.卡分片
            else:
                模块 = args.卡分片
        spec_text = render_ai_card(模块=模块, 任务=任务)
        print(f"语言规格指纹：{spec_hash}（语言卡 {len(spec_text)} 字符"
              f"{'，分片=' + args.卡分片 if args.卡分片 else ''}）")
    else:
        spec_text = json.dumps(spec, ensure_ascii=False)
        print(f"语言规格指纹：{spec_hash}（{len(spec_text)} 字符）")

    all_results: dict[str, list[TaskOutcome]] = {}
    all_elapsed: dict[str, float] = {}
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.time()
    for model in args.模型:
        print(f"\n=== 跑分：{model} ===")
        t_model = time.time()
        outs: list[TaskOutcome] = []
        基准次数 = dict(_计数)          # 本模型开始前的计数（用来算这个模型花了多少次请求）
        限速 = _限速器(args.每分钟)
        完成 = [0]
        锁 = threading.Lock()
        # 并发时 `eval_one` 内部的逐题打印会乱序 ⇒ 关掉它，统一在下面按**完成**打。
        逐题打印 = args.verbose and args.并发 <= 1

        def 跑一题(t: Task) -> TaskOutcome:
            o = eval_one(t, args.base, key, model, spec_text,
                         args.最大轮数, args.最大令牌, 逐题打印,
                         interval=args.题间间隔, retries=args.重试,
                         eval_timeout=args.超时, 限速=限速,
                         quiet=args.并发 > 1)
            with 锁:
                完成[0] += 1
                if args.verbose and args.并发 > 1:
                    标记 = "✓" if o.通过 else ("跳过" if o.跳过 else "✗")
                    print(f"  [{完成[0]}/{len(tasks)}] {o.task_id} {标记}"
                          f"（第 {o.轮数} 轮）", flush=True)
                if o.跳过:
                    print(f"    ⚠ 跳过（{o.跳过原因[:60]}）", flush=True)
            return o

        if args.并发 > 1:
            # `ex.map` 按**提交顺序**返回 ⇒ 汇总表的顺序与串行完全一致，
            # 报告可以直接跟上一版对比（别用 as_completed，那会打乱顺序）。
            with ThreadPoolExecutor(max_workers=args.并发) as ex:
                outs = list(ex.map(跑一题, tasks))
        else:
            for i, t in enumerate(tasks, 1):
                if args.verbose:
                    print(f"  [{i}/{len(tasks)}] {t.id}", flush=True)
                outs.append(跑一题(t))
        all_results[model] = outs
        all_elapsed[model] = time.time() - t_model
        # 耗时与 token 总量也打进日志：报告若需从日志重建（如重跑只为修格式），
        # 这两个数是重建不出来的——不记就等于丢。
        # 🔴 **请求次数与「实际速率」**（2026-10-24）：这两个数是调 `--并发` / `--每分钟`
        #    的唯一依据。只报耗时的话，慢的时候分不清是「题要改 3 轮」（次数多）
        #    还是「通道慢」（每次久）—— 调参会变成瞎猜。
        次数 = _计数["请求"] - 基准次数["请求"]
        重试数 = _计数["重试"] - 基准次数["重试"]
        每分钟实际 = 次数 / (all_elapsed[model] / 60.0) if all_elapsed[model] > 0 else 0.0
        print(f"  [汇总] {model}：耗时 {all_elapsed[model]:.1f} 秒，"
              f"token prompt {sum(o.prompt_tokens for o in outs)}"
              f" / completion {sum(o.completion_tokens for o in outs)}"
              f"；请求 {次数} 次（重试 {重试数}）"
              f"，实际 {每分钟实际:.0f} 次/分钟"
              f"／上限 {args.每分钟:g}")
        if 重试数 > 次数 * 0.2:
            print(f"    ⚠️ 重试占 {重试数 / max(次数, 1):.0%} —— 已经撞限流了，"
                  f"把 `--每分钟` 调低，或 `--并发` 调低（撞 429 退避反而更慢）")
        elif 每分钟实际 < args.每分钟 * 0.6 and args.并发 > 1:
            print(f"    ⚠️ 实际速率只有上限的 {每分钟实际 / max(args.每分钟, 1):.0%} —— "
                  f"瓶颈不在限流，在通道延迟；可以再抬 `--并发`")
    elapsed = time.time() - t0

    分类计数: dict[str, int] = {}
    for t in tasks:
        分类计数[t.类型] = 分类计数.get(t.类型, 0) + 1
    分类串 = " / ".join(f"{k} {v}" for k, v in 分类计数.items())
    parts = ["# 基石 AI 跑分表", "",
             f"- 题集：{len(tasks)} 题（{分类串}）",
             f"- 最大修复轮数：{args.最大轮数}",
             f"- 语言规格指纹：`{spec_hash}`", ""]
    parts.append(summarize_table(all_results, args.最大轮数))
    parts.append("---\n")
    for model, outs in all_results.items():
        parts.append(render_report(model, outs, spec_hash,
                                   args.最大轮数, started,
                                   all_elapsed[model]))
    report = "\n".join(parts)

    # 🔴 **局部跑不写正式跑分表**（2026-10-24，我自己踩到的）：
    #    测延迟时跑了 `--限制 2`，它把 `evals/跑分表.md` **整份覆盖**成「2 题 50%」——
    #    那是历史跑分的档案，被一次探路冲掉就等于把记录毁了。
    #    ⇒ `--限制` / `--类型` 这种**局部跑**默认不写；要写就显式 `--写报告`。
    #    ⚠️ 局部跑的结果**也不该进跑分表**：2 题的通过率没有统计意义，
    #      留着会让人误以为「这一版掉到 50%」。
    写报告 = bool(getattr(args, "写报告", False))
    局部 = bool(args.限制 or args.类型 or 只跑们)
    out_path = Path(args.输出) if args.输出 else (ROOT / "evals" / "跑分表.md")
    if 局部 and not 写报告:
        print(f"\n（这是局部跑：--限制/--类型 ⇒ 不写 {out_path.name}；"
              f"要写请加 --写报告 或 --输出 <别的路径>）")
        if not args.输出:
            return 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 🔴 **跑分表是累积的，新的一节要插在最前**（2026-10-24 修正）：
    #    文件开头自己写着「新的一节放最前；旧的留着，方便对照」——
    #    而这里原来是 `write_text`，**整份覆盖**。也就是说：跑一次分，
    #    前面所有历史跑分全没了（我第一次探路就那么干的，`git checkout` 才救回来）。
    #    ⇒ 默认目标（跑分表）改成「插到最前」；`--输出` 指定别的路径时还是整份写
    #      （那是「导出这一份」，不是「记一笔账」）。
    if (not args.输出) and out_path.is_file():
        旧文 = out_path.read_text(encoding="utf-8")
        新节 = report
        # 去掉新报告自己的文件头（`# 标题` 那段引用块），只留正文各节
        行们 = 新节.splitlines()
        for i, ln in enumerate(行们):
            if ln.startswith("## "):
                新节 = "\n".join(行们[i:])
                break
        # 旧文件里第一个 `## ` 之前是文件头，插在它前面
        旧行 = 旧文.splitlines()
        头止 = next((i for i, ln in enumerate(旧行) if ln.startswith("## ")), len(旧行))
        # 节标题：`## 2026-10-24（80 题）` —— 历史各节都是这个形状，
        # 缺了它，插进来的那一节就**没有日期**（我第一次插就漏了）。
        标题 = getattr(args, "节标题", None) or (
            f"## {datetime.now().strftime('%Y-%m-%d')}（{len(tasks)} 题）")
        头 = "\n".join(旧行[:头止]).rstrip()
        余 = "\n".join(旧行[头止:]).rstrip()
        out_path.write_text(头 + "\n\n" + 标题 + "\n\n" + 新节.rstrip() + "\n\n" + 余 + "\n",
                            encoding="utf-8")
        print(f"\n报告已**插到最前**：{out_path}（历史各节保留）")
        return 0
    out_path.write_text(report, encoding="utf-8")
    print(f"\n报告已写入：{out_path}")
    print("\n" + summarize_table(all_results, args.最大轮数))
    return 0


if __name__ == "__main__":
    sys.exit(main())
