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
import subprocess
import sys
import time
import urllib.error
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
TASK_FILES = (
    ("01_syntax.json", "语法"),
    ("02_stdlib.json", "标准库"),
    ("03_debug.json", "调试"),
    ("04_refactor.json", "重构"),
)


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
    额外: dict = field(default_factory=dict)


def load_tasks(only_type: Optional[str] = None,
               limit: Optional[int] = None) -> list[Task]:
    tasks: list[Task] = []
    for fname, kind in TASK_FILES:
        path = TASKS_DIR / fname
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for raw in data.get("题集", []):
            t = Task(
                id=raw["id"], 类型=raw.get("类型", kind),
                题目=raw["题目"], 标准输出=raw["标准输出"],
                考点=raw.get("考点", raw.get("缺陷", "")),
            )
            t.起始代码 = (raw.get("有缺陷代码") or raw.get("原始代码") or "")
            t.额外 = {k: v for k, v in raw.items()
                      if k not in ("id", "类型", "题目", "标准输出", "考点",
                                   "有缺陷代码", "原始代码", "缺陷", "修法说明",
                                   "目标", "评分口径")}
            if only_type and t.类型 != only_type:
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
              tag: str = "eval") -> RunResult:
    """把代码写成临时文件跑一遍。退出码非 0 一律算失败（见模块 docstring）。"""
    tmp = ROOT / ".tmp-eval"
    tmp.mkdir(exist_ok=True)
    f = tmp / f"{_safe_name(tag)}.jsh"
    f.write_text(code, encoding="utf-8")
    try:
        p = subprocess.run(
            [str(PYTHON), "-m", "jishi.cli", str(f)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=ROOT, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return RunResult(False, "", f"执行超时（{timeout} 秒）", -1, True)
    finally:
        try:
            f.unlink()
        except OSError:
            pass
    return RunResult(p.returncode == 0, p.stdout, p.stderr, p.returncode)


def _safe_name(s: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff-]+", "_", s)[:80]


def judge(code: str, want: str, timeout: float = 20.0,
          tag: str = "eval") -> tuple[bool, RunResult]:
    """判一题：输出逐字相同 **且** 退出码 0 才算通过。"""
    r = run_jishi(code, timeout, tag)
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


#: 遇到这些信号就退避重试（额度/频率类问题，等一等通常能过）
RETRYABLE = ("429", "RATE_LIMITED", "503", "SERVICE_BUSY", "502", "504",
             "timed out", "TimeoutError", "URLError", "ConnectionReset")


def _retryable(err: str) -> bool:
    return any(sig in err for sig in RETRYABLE)


def call_llm(base: str, key: str, model: str, messages: list[dict],
             max_tokens: int = 4096, timeout: float = 180.0,
             retries: int = 4, quiet: bool = False) -> LlmReply:
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
                wait = 2 ** attempt * 3      # 3s, 6s, 12s, 24s
                if not quiet:
                    print(f"      ⏳ 限流/故障，{wait}s 后重试"
                          f"（第 {attempt + 1}/{retries} 次）", flush=True)
                time.sleep(wait)
                continue
            return LlmReply("", False, last_err)
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}：{e}"
            if _retryable(last_err) and attempt < retries:
                wait = 2 ** attempt * 3
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
             retries: int = 4) -> TaskOutcome:
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
                         retries=retries)
        p_tok += reply.prompt_tokens
        c_tok += reply.completion_tokens
        if not reply.ok:
            return TaskOutcome(t.id, t.类型, False, rnd, True, reply.error,
                               t.标准输出, last_out, last_err, last_code,
                               p_tok, c_tok)

        code = strip_code_fence(reply.text)
        last_code = code
        ok, run = judge(code, t.标准输出, tag=f"{model}_{t.id}_r{rnd}")
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
    ap.add_argument("--最大轮数", type=int, default=3,
                    help="失败后回灌报错最多改几轮（默认 3）")
    ap.add_argument("--最大令牌", type=int, default=8192,
                    help="单次调用 max_tokens（推理模型要给够，默认 8192）")
    ap.add_argument("--题间间隔", type=float, default=1.5,
                    help="每题之间的等待秒数，用于避开接口限流（默认 1.5）")
    ap.add_argument("--重试", type=int, default=4,
                    help="撞限流/临时故障时的退避重试次数（默认 4，退避 3/6/12/24 秒）")
    ap.add_argument("--key", default=None, help="API key（默认读 JISHI_EVAL_KEY）")
    ap.add_argument("--base", default=DEFAULT_BASE, help="接口地址")
    ap.add_argument("--输出", default=None, help="报告写到哪个文件")
    ap.add_argument("--自检", action="store_true",
                    help="只校验题集自洽性，不调用模型（不花钱）")
    ap.add_argument("--校验题集", action="store_true", help="同 --自检")
    ap.add_argument("-v", "--verbose", action="store_true", help="逐题打印进度")
    args = ap.parse_args(argv)

    tasks = load_tasks(args.类型, args.限制)
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

    from jishi.ai import build_lang_spec, content_hash
    spec = build_lang_spec()
    spec_text = json.dumps(spec, ensure_ascii=False)
    spec_hash = content_hash(spec)
    print(f"语言规格指纹：{spec_hash}（{len(spec_text)} 字符）")

    all_results: dict[str, list[TaskOutcome]] = {}
    all_elapsed: dict[str, float] = {}
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.time()
    for model in args.模型:
        print(f"\n=== 跑分：{model} ===")
        t_model = time.time()
        outs: list[TaskOutcome] = []
        for i, t in enumerate(tasks, 1):
            if args.verbose:
                print(f"  [{i}/{len(tasks)}] {t.id}")
            o = eval_one(t, args.base, key, model, spec_text,
                         args.最大轮数, args.最大令牌, args.verbose,
                         interval=args.题间间隔, retries=args.重试)
            outs.append(o)
            if o.跳过:
                print(f"    ⚠ 跳过（{o.跳过原因[:60]}）")
        all_results[model] = outs
        all_elapsed[model] = time.time() - t_model
        # 耗时与 token 总量也打进日志：报告若需从日志重建（如重跑只为修格式），
        # 这两个数是重建不出来的——不记就等于丢。
        print(f"  [汇总] {model}：耗时 {all_elapsed[model]:.1f} 秒，"
              f"token prompt {sum(o.prompt_tokens for o in outs)}"
              f" / completion {sum(o.completion_tokens for o in outs)}")
    elapsed = time.time() - t0

    parts = ["# 基石 AI 跑分表（M45）", "",
             f"- 题集：{len(tasks)} 题"
             f"（语法 12 / 标准库 15 / 调试 10 / 重构 8）",
             f"- 最大修复轮数：{args.最大轮数}",
             f"- 语言规格指纹：`{spec_hash}`", ""]
    parts.append(summarize_table(all_results, args.最大轮数))
    parts.append("---\n")
    for model, outs in all_results.items():
        parts.append(render_report(model, outs, spec_hash,
                                   args.最大轮数, started,
                                   all_elapsed[model]))
    report = "\n".join(parts)

    out_path = Path(args.输出) if args.输出 else (ROOT / "evals" / "跑分表.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(f"\n报告已写入：{out_path}")
    print("\n" + summarize_table(all_results, args.最大轮数))
    return 0


if __name__ == "__main__":
    sys.exit(main())
