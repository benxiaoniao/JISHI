# -*- coding: utf-8 -*-
"""一致性夹具（R0）——把**前端每个阶段**的输出冻结成基线 JSON。

## 这东西解决什么问题

R 线要把前端从 Python 换成 Rust。换的过程中最容易出的错不是「崩了」，
而是「**悄悄变了**」：某个 token 的列号差 1、某条指令少了个字段、某句报错的
提示语措辞不同。这类差异**跑得通、看不出来**，等用户发现时早就扩散了。

所以先立规矩：**把 Python 前端现在的行为，逐字冻结成结构化基线**。
Rust 版每写一步，就拿基线与它比 —— 一致才叫「这一步做完了」。

## 冻结哪四个阶段

| 阶段 | 产物 | 谁产生 | 将来谁验收 |
|---|---|---|---|
| `tokens` | 词法单元序列（类型 / 值 / 行列 / 结束列 / 所在行） | `tokenizer.tokenize` | R2 分词器 |
| `ast` | 语法树（节点类型 + 全部字段，递归） | `parser.parse` | R3 语法 |
| `bytecode` | 字节码（常量池 / 名字表 / 指令 / 行号表） | `compiler.compile_source` | R5 编译器 |
| `diagnostics` | 第一个错误（**码 / 行列 / 消息 / 提示**） | 三个阶段都可能 | R4 报错 |

⚠️ **四个阶段各自独立冻结**：某语料在词法就失败，那它的 `tokens` 为 `null`
而 `diagnostics` 有内容 —— 不是「跳过」，是「如实记录它失败在哪一步」。

## 三条设计规矩

1. **确定性**：JSON 一律 `sort_keys` + `ensure_ascii=False` + 两格缩进。
   同一份语料在任何机器、任何一次跑，产物必须**逐字节相同**（否则夹具本身
   就成了噪声源）。
2. **逐语料分文件**：`baseline/<扁平路径>.json`。这样 `git diff` 能直接指出
   「哪个语料的哪个阶段变了」，而不是给一个几 MB 的大 JSON 让人猜。
3. **语料内容也要钉住**：`MANIFEST.json` 记每个语料的 sha256。**改了语料却没
   重冻基线**时，`check` 必须报红 —— 否则基线会悄悄变成「另一份程序的输出」，
   而那正是夹具要防的事。

## 怎么用

```bash
python tools/conformance.py list          # 看语料清单与统计
python tools/conformance.py check         # 比对基线（默认，CI 用；不一致退 1）
python tools/conformance.py freeze        # 重新冻结（改语料/改前端后跑，**要看 diff**）
python tools/conformance.py check -v      # 不一致时多打几处差异
```

📌 **冻结前先看 `git diff`**：`freeze` 是「把当前行为升格为标准」的动作，
它自己不会判断这个行为对不对。**改错了也照冻**，所以这一步必须有人看。
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

CONF_DIR = ROOT / "tests" / "conformance"
BASELINE_DIR = CONF_DIR / "baseline"
MANIFEST = CONF_DIR / "MANIFEST.json"

#: 基线格式版本。**产物结构**变了才动它（比如给 tokens 加一个字段），
#: 动了之后所有旧基线都算过期 —— `check` 会提示重冻。
#:
#: | 版本 | 变更 |
#: |---|---|
#: | 1 | R0 起：tokens / ast / bytecode / diagnostics（六个事实字段） |
#: | **2** | **R4**：`diagnostics` 补 `下划线` / `修正` / `渲染` —— 见下表 |
#: | **3** | **R6.6**：`bytecode` 的 `Code` 补 `local_hints`（字节码内部版本 2→3） |
FORMAT_VERSION = 3

#: 语料来源：`(类别, 相对 glob)`。顺序即报告顺序。
#:
#: - `lang`   语言特性与诊断（最核心：语法/语义的各种写法都在这里）
#: - `frontend` **只给前端看**的语料（R2 起）：这些程序**编译得过**，
#:   但某个执行器可能有意拒绝（如超 i64 的整数 ⇒ C VM 报 E2005）。
#:   放 `tests/cases/` 会破坏「三执行器都能跑」那条约定，所以单列一类；
#:   它们对 **R3/R5 的产物比对**照样有效（而且是不可替代的：大整数常量
#:   按字符串编码那条分支，只有这种语料才走得到）。
#: - `examples` 真实程序（含 6 个完整项目 —— 光靠小样例测不出「真项目会不会崩」）
#: - `stdlib` 基石库层源码（用基石写的标准库，前端必须能编译它）
CORPUS_SOURCES: tuple[tuple[str, str], ...] = (
    ("lang", "tests/cases/*.jsh"),
    ("lang", "tests/error_cases/*.jsh"),
    ("frontend", "tests/frontend_cases/*.jsh"),
    ("examples", "examples/**/*.jsh"),
    ("stdlib", "jishi/stdlib-jishi/*.jsh"),
)


# ---------------------------------------------------------------------------
# 一、JSON 化（确定性）
# ---------------------------------------------------------------------------

def jsonable(obj: Any) -> Any:
    """把任意对象转成**可确定性序列化**的 JSON 结构。

    规则：
    - dataclass → `{"类型": 类名, 字段...}`（递归）
    - list/tuple → list；dict → 键转字符串
    - 浮点特殊值（NaN/±Inf）→ 字符串标记（**JSON 里没有这些字面量**，
      跨语言解析器会拒收；Rust 的 serde_json 就不接受）
    - 其它 → `repr`（并标成 `"<不可序列化>"`，免得悄悄丢数据）
    """
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        if obj != obj:                      # NaN
            return "@NaN"
        if obj == float("inf"):
            return "@Infinity"
        if obj == float("-inf"):
            return "@-Infinity"
        return obj
    if isinstance(obj, (list, tuple)):
        return [jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out: dict[str, Any] = {"类型": type(obj).__name__}
        for f in dataclasses.fields(obj):
            out[f.name] = jsonable(getattr(obj, f.name))
        return out
    if isinstance(obj, (set, frozenset)):
        # 集合无序 —— 排个序让产物稳定（元素先转成可哈希的 JSON 文本）
        items = [jsonable(x) for x in obj]
        return sorted(items, key=lambda x: json.dumps(x, ensure_ascii=False,
                                                     sort_keys=True))
    return {"<不可序列化>": f"{type(obj).__name__}: {obj!r}"}


def dumps(obj: Any) -> str:
    """全仓统一的 JSON 写法（缩进两格、键排序、中文原样）。"""
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def corpus_id(rel: str) -> str:
    """相对路径 → 扁平 ID（`tests/cases/01_算术.jsh` → `tests__cases__01_算术`）。"""
    return rel.replace("\\", "/").removesuffix(".jsh").replace("/", "__")


# ---------------------------------------------------------------------------
# 二、跑四个阶段
# ---------------------------------------------------------------------------

def _normalize_lines(src: str) -> list[str]:
    """与 `compiler.compile_source` 一致的行拆分（夹具不许多一套口径）。"""
    return src.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _err_json(err: Any, stage: str,
              lines: "list[str] | None" = None) -> dict[str, Any]:
    """把一个**基石错误**记成结构化诊断 —— 这是错误对外的**全部事实**。

    | 字段 | 是什么 | v |
    |---|---|---|
    | `阶段` / `码` / `标题` / `消息` / `行` / `列` / `提示` | 基础事实 | 1 |
    | `下划线` | `^` 指示的列区间 `[起, 止]`（1 起、闭区间），无则 null | **2** |
    | `修正` | 机器可读的替换建议 `{"old","new"}`（给模型自愈用），无则 null | **2** |
    | `渲染` | **用户真正看到的那段多行文本**（`render()` 的结果） | **2** |

    ⚠️ **为什么要 `渲染`**：它是「事实」的下游产物，看着冗余，其实是**兜底**——
    哪天给错误加了一个事实字段却忘了列进上面这张表，两者的渲染立刻不一样，
    而「字段没进契约」这件事本身是**看不出来**的。（R3 的教训：漏了一个字段，
    两个前端就会各渲染各的，而比对仍然是绿的。）
    """
    if lines is not None:
        err.with_source(list(lines))        # 就地补全；`render()` 才有源码行
    return {
        "阶段": stage,
        "码": err.code,
        "标题": err.title,
        "消息": err.message,
        "行": err.line,
        "列": err.col,
        "提示": err.hint,
        "下划线": list(err.underline) if err.underline else None,
        "修正": err.fix,
        "渲染": err.render(),
    }


class ImplementationBug(RuntimeError):
    """语料让前端抛了**非基石错误** —— 那是实现有 bug，不是语言行为。

    ⚠️ 这条闸门是必须的：**把 bug 冻进基线，等于把 bug 升格成标准**。
    本工具第一次跑就撞上了 —— 我把 `serialize.dump_json` 写成了
    `dumps_json`，于是 73 个语料的 bytecode 全变成
    「编译阶段：<内部错误：ImportError>」。如果 freeze 照单全收，
    以后 Rust 版「忠实复刻」这个 ImportError 才算一致 —— 荒谬。
    所以：**遇到非基石错误一律拒绝冻结**，报出来让人去修实现。
    """

    def __init__(self, rel: str, stage: str, err: BaseException):
        super().__init__(f"{rel} 在「{stage}」阶段抛了 {type(err).__name__}：{err}")
        self.rel, self.stage, self.err = rel, stage, err


#: 四个阶段的固定顺序（报告、`--stage` 取值都按它）
ALL_STAGES: tuple[str, ...] = ("tokens", "ast", "bytecode", "diagnostics")


def run_stages(rel: str, stages: tuple[str, ...] = ALL_STAGES) -> dict[str, Any]:
    """跑一个语料的四个阶段，返回待冻结的产物。

    阶段内抛 `JishiError` → 记进 `diagnostics`（这是语言行为，要冻结）；
    抛别的 → 抛 `ImplementationBug`（这是实现 bug，不许冻结）。

    `stages` 用来**只跑某个实现已经做到的那几步**（R2 只做分词、R3 只做到语法）：
    没要的阶段**连算都不算**，键由调用方抹掉 —— 「这一阶段没提供」必须在产物里
    看得出来（键不存在），不能拿另一份实现算出来的值顶上。
    ⚠️ 注意 `stages` 里**没有 `bytecode` 时不会跑编译器**，所以 `diagnostics`
    自然只会是词法/语法那两种 —— R3 的 `--stage diagnostics` 因此**覆盖面是
    「语法及以前」**，不是「全部报错」（R4 的范围因此缩小，见 内部前端契约（未公开） §10.4）。
    """
    from jishi import compiler as C
    from jishi import parser as P
    from jishi import tokenizer as T
    from jishi.errors import JishiError

    path = ROOT / rel
    src = path.read_text(encoding="utf-8")
    lines = _normalize_lines(src)

    out: dict[str, Any] = {
        "语料": rel,
        "内容哈希": sha256_text(src),
        "tokens": None,
        "ast": None,
        "bytecode": None,
        "diagnostics": None,
    }

    # ---- 阶段 1：词法 ----
    try:
        tokens = T.tokenize(src, rel)
        out["tokens"] = [jsonable(t) for t in tokens]
    except JishiError as e:
        out["diagnostics"] = _err_json(e, "词法", lines)
        return out
    except Exception as e:                                      # noqa: BLE001
        raise ImplementationBug(rel, "词法", e) from e

    # ---- 阶段 2：语法 ----
    try:
        program = P.parse_source(src, rel)
        out["ast"] = jsonable(program)
    except JishiError as e:
        out["diagnostics"] = _err_json(e, "语法", lines)
        return out
    except Exception as e:                                      # noqa: BLE001
        raise ImplementationBug(rel, "语法", e) from e

    # ---- 阶段 3：编译 ----
    if "bytecode" in stages:
        try:
            # 走 `compile_to_json`：rust 前端下**全程原生**（不经 Python 对象），
            # Python 前端下是「编译成对象再序列化」。两条路产出的都是同一份契约。
            body = json.loads(C.compile_to_json(src, rel))
            # `opcode_names` 是引擎级常量表，73 个文件各抄一份纯属浪费 ——
            # 它在 MANIFEST 里记**一份**（加指令时那里会变，照样能被发现）。
            body.pop("opcode_names", None)
            out["bytecode"] = body
        except JishiError as e:
            out["diagnostics"] = _err_json(e, "编译", lines)
            return out
        except Exception as e:                                  # noqa: BLE001
            raise ImplementationBug(rel, "编译", e) from e

    return out


# ---------------------------------------------------------------------------
# 三、语料清单
# ---------------------------------------------------------------------------

def collect_corpus() -> list[tuple[str, str]]:
    """扫描语料，返回 `[(类别, 相对路径)]`（排序稳定）。"""
    seen: dict[str, str] = {}
    for category, pattern in CORPUS_SOURCES:
        for p in sorted(ROOT.glob(pattern)):
            if not p.is_file():
                continue
            rel = p.relative_to(ROOT).as_posix()
            seen.setdefault(rel, category)
    return [(seen[rel], rel) for rel in sorted(seen)]


# ---------------------------------------------------------------------------
# 四、差异定位
# ---------------------------------------------------------------------------

def first_diff(a: Any, b: Any, path: str = "") -> tuple[str, Any, Any] | None:
    """递归找第一处不同，返回 `(路径, 期望, 实际)`；相同返回 None。

    路径形如 `ast.body[3].kind` / `tokens[7].col` —— **必须能定位到具体一项**：
    只说「不一致」等于没说，「第 8 个 token 的列号差 1」才是能直接去改的信息。
    """
    if type(a) is not type(b):
        # 类型不同就算不同（`1` 与 `"1"`、`1` 与 `True` 都不是同一回事）
        return (path or "<根>", a, b)
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            sub = f"{path}.{k}" if path else str(k)
            if k not in a:
                return (sub, "<缺>", b[k])
            if k not in b:
                return (sub, a[k], "<缺>")
            d = first_diff(a[k], b[k], sub)
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return (f"{path}.长度" if path else "长度", len(a), len(b))
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_diff(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    if a != b:
        return (path or "<根>", a, b)
    return None


def _short(v: Any, limit: int = 60) -> str:
    s = json.dumps(v, ensure_ascii=False, sort_keys=True)
    return s if len(s) <= limit else s[:limit] + "…"


# ---------------------------------------------------------------------------
# 五、freeze / check / list
# ---------------------------------------------------------------------------

def _load_manifest() -> dict[str, Any]:
    if not MANIFEST.exists():
        return {}
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def cmd_freeze(_args) -> int:
    corpus = collect_corpus()
    if not corpus:
        print("❌ 一个语料都没扫到 —— 检查 CORPUS_SOURCES 的 glob", file=sys.stderr)
        return 1

    # ---- 先全跑一遍，**全成功才落盘**（免得中途失败留下一半新一半旧的基线）----
    staged: list[tuple[str, str, dict[str, Any]]] = []
    bugs: list[str] = []
    for category, rel in corpus:
        try:
            staged.append((category, rel, run_stages(rel)))
        except ImplementationBug as b:
            bugs.append(str(b))

    if bugs:
        print(f"🚫 拒绝冻结：{len(bugs)} 个语料让前端抛了**非基石错误** —— "
              f"那是实现有 bug，不是语言行为。**冻进基线等于把 bug 升格成标准**。\n",
              file=sys.stderr)
        for b in bugs[:12]:
            print(f"  · {b}", file=sys.stderr)
        if len(bugs) > 12:
            print(f"  …还有 {len(bugs) - 12} 个", file=sys.stderr)
        print("\n先修实现，再重跑 freeze。", file=sys.stderr)
        return 1

    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    entries, changed, added = [], [], []

    for category, rel, data in staged:
        cid = corpus_id(rel)
        dst = BASELINE_DIR / f"{cid}.json"
        text = dumps(data)

        old = dst.read_text(encoding="utf-8") if dst.exists() else None
        if old is None:
            added.append(rel)
        elif old != text:
            changed.append(rel)
        # ⚠️ `newline="\n"`：**显式钉死行尾**。夹具的命根子是「逐字节确定」，
        # 而 Windows 上 `write_text` 默认会把 `\n` 翻成 `\r\n` —— 于是同一份
        # 基线在两个平台的工作区里是不同的字节（M56 在文件读写上刚吃过这个亏）。
        dst.write_text(text, encoding="utf-8", newline="\n")

        entries.append({
            "id": cid,
            "路径": rel,
            "类别": category,
            "内容哈希": data["内容哈希"],
            "产物哈希": sha256_text(text),
        })

    # 孤儿：baseline 里躺着但语料已不在的（删语料后没重冻）
    keep = {corpus_id(rel) for _, rel, _ in staged}
    orphans = [p.stem for p in sorted(BASELINE_DIR.glob("*.json"))
               if p.stem not in keep]
    for o in orphans:
        (BASELINE_DIR / f"{o}.json").unlink()

    from jishi import opcodes as O
    from jishi.version import VERSION
    manifest = {
        "格式版本": FORMAT_VERSION,
        "生成环境": {"python": sys.version.split()[0], "jishi": VERSION},
        "语料数": len(entries),
        "类别计数": {c: sum(1 for e in entries if e["类别"] == c)
                     for c in dict.fromkeys(cat for cat, _ in corpus)},
        # 指令集常量表：全局一份就够（每个基线里都抄一份纯浪费）。
        # 加指令/改指令名时这里会变 —— 那是 R5 要盯的信号。
        "指令集": O.OP_NAMES,
        "语料": entries,
    }
    MANIFEST.write_text(dumps(manifest), encoding="utf-8", newline="\n")

    print(f"✅ 已冻结 {len(entries)} 个语料 → {BASELINE_DIR.relative_to(ROOT)}")
    for c, n in manifest["类别计数"].items():
        print(f"   {c:<10} {n:>3} 个")
    print(f"   新增 {len(added)} 个" + (f"：{'、'.join(added[:4])}…" if len(added) > 4
                                       else (f"：{'、'.join(added)}" if added else "")))
    print(f"   变更 {len(changed)} 个" + (f"：{'、'.join(changed[:4])}…" if len(changed) > 4
                                         else (f"：{'、'.join(changed)}" if changed else "")))
    if orphans:
        print(f"   清理孤儿 {len(orphans)} 个：{'、'.join(orphans[:4])}"
              + ("…" if len(orphans) > 4 else ""))
    print("\n📌 **现在去看 `git diff`**：freeze 只是「把当前行为升格为标准」，"
          "它不会判断这个行为对不对。")
    return 0


def compare_all() -> tuple[list[str], list[str], list[str]]:
    """比对全部语料，返回 `(产物不一致, 语料/清单侧问题, 差异明细)`。

    抽出来是为了让**测试与 CLI 用同一套判据** —— 否则「测试绿、CI 红」
    这种事迟早发生（判据分叉了）。
    """
    manifest = _load_manifest()
    if not manifest:
        return [], ["没有 MANIFEST.json —— 先跑 `python tools/conformance.py freeze`"], []
    if manifest.get("格式版本") != FORMAT_VERSION:
        return [], [f"基线格式版本 {manifest.get('格式版本')} ≠ 当前 {FORMAT_VERSION}"
                    f"（需要重新冻结）"], []

    corpus = collect_corpus()
    by_rel = {e["路径"]: e for e in manifest["语料"]}

    mismatches: list[str] = []
    others: list[str] = []
    details: list[str] = []

    # ---- 1) 语料侧 ----
    for _category, rel in corpus:
        if rel not in by_rel:
            others.append(f"新增语料未冻结：{rel}")
            continue
        dst = BASELINE_DIR / f"{corpus_id(rel)}.json"
        if not dst.exists():
            others.append(f"基线文件缺失：{dst.relative_to(ROOT).as_posix()}")
            continue

        want = by_rel[rel]
        try:
            data = run_stages(rel)
        except ImplementationBug as b:
            others.append(f"实现有 bug：{b}")
            continue

        # 语料内容变了吗（变了说明基线记录的是「另一份源码」的输出）
        if data["内容哈希"] != want["内容哈希"]:
            others.append(
                f"语料内容已改动：{rel}（{want['内容哈希'][7:15]} → "
                f"{data['内容哈希'][7:15]}）—— 有意改的就重冻基线")
            continue

        base = json.loads(dst.read_text(encoding="utf-8"))
        d = first_diff(base, data)
        if d:
            path, exp, got = d
            mismatches.append(f"产物不一致：{rel} 在 `{path}`")
            ctx = _context(base, path)
            details.append(
                f"    {rel}\n"
                f"      「{path}」" + (f" —— {ctx}" if ctx else "") + "\n"
                f"        基线：{_short(exp)}\n"
                f"        当前：{_short(got)}")

    # ---- 2) 基线侧：孤儿 ----
    live = {r for _, r in corpus}
    for rel in by_rel:
        if rel not in live:
            others.append(f"孤儿基线（语料已不存在）：{rel}")

    return mismatches, others, details


def cmd_check(args) -> int:
    total = len(collect_corpus())
    mismatches, others, details = compare_all()
    problems = mismatches + others

    if problems:
        bad = len(mismatches)
        print(f"❌ 一致性夹具：{total - bad}/{total} 个语料一致；"
              f"共 {len(problems)} 处问题（其中产物不一致 {bad} 个）\n")
        limit = 10_000 if args.verbose else 12
        for p in problems[:limit]:
            print(f"  · {p}")
        if len(problems) > limit:
            print(f"  …还有 {len(problems) - limit} 处")
        if details:
            show = len(details) if args.verbose else 4
            print(f"\n差异明细（前 {min(show, len(details))} 处）：")
            for d in details[:show]:
                print(d)
        return 1

    manifest = _load_manifest()
    print(f"✅ 一致性夹具：{total}/{total} 个语料在四个阶段上全部一致")
    print(f"   基线冻结于 Python {manifest['生成环境']['python']} · "
          f"jishi {manifest['生成环境']['jishi']}")
    return 0


# ---------------------------------------------------------------------------
# 四之二、外部产物比对（R1）——「拿别人的产物来比」
# ---------------------------------------------------------------------------

_PARTS = re.compile(r"([^.\[\]]+)|\[(\d+)\]")
_MISS = object()


def _walk(root: Any, path: str) -> Any:
    """按 `a.b[0].c` 的形式取值；取不到返回 `_MISS`。"""
    cur = root
    for m in _PARTS.finditer(path):
        key, idx = m.group(1), m.group(2)
        try:
            cur = cur[key] if key is not None else cur[int(idx)]
        except Exception:                                       # noqa: BLE001
            return _MISS
    return cur


def _context(base: Any, path: str) -> str:
    """给出「出错的是哪个元素」的可读说明 —— 拿不到就返回空串（不硬凑）。

    只报 `tokens[2].col 基线 5 / 当前 6` 还不够：人还得自己去数第 3 个 token
    是什么。这里直接把它点出来：

        tokens[2].col —— NUMBER「3」（第 2 行第 5 列）
    """
    parent = path.rsplit(".", 1)[0] if "." in path else ""
    obj = _walk(base, parent) if parent else base
    if obj is _MISS:
        return ""
    if isinstance(obj, list):
        return f"{len(obj)} 项"
    if isinstance(obj, dict):
        if "语料" in obj:
            return str(obj["语料"])
        # ⚠️ token 要先判：它也有 `类型`（值是 "Token"），但**带行列的说明更有用**。
        # 放在后面会被通用分支吃掉，输出退化成「Token「=」」—— 虽然也能定位，
        # 但少了「第几行第几列」这条最直观的信息。
        if "type" in obj and "value" in obj:     # token
            return (f"{obj['type']}「{obj['value']}」"
                    f"（第 {obj.get('line')} 行第 {obj.get('col')} 列）")
        if "类型" in obj:                       # AST 节点 / 其它带类型标记的
            extra = ""
            for k in ("名字", "kind", "op", "value"):
                v = obj.get(k)
                if isinstance(v, (str, int, float, bool)):
                    extra = f"「{v}」"
                    break
            return f"{obj['类型']}{extra}"
        if "name" in obj:                        # 函数 / 代码块
            return f"函数「{obj['name']}」"
    return ""


def cmd_compare(args) -> int:
    """拿**外部实现**产出的产物，与基线逐项比（R1）。

    为什么单独有这条：`check` 是「跑 **Python 前端** → 与基线比」——
    那只验证「Python 还是老样子」。从 R2 起要验的是
    **「Rust 产出的产物，与 Python 的行为一致吗」**，所以需要一个
    「拿**任意来源**的产物来比」的入口。

    外部产物的约定（见 内部前端契约（未公开））：
    - 目录里每个语料一个 JSON，文件名 = 基线那份的 id（`<相对路径>.jsh` →
      `__` 连接、去掉后缀）；
    - 结构与基线同构；**只提供部分阶段也可以**（R2 只做分词时就只给 `tokens`），
      但**没提供的阶段会如实报「未提供」**，绝不静默当通过。

    ⚠️ **「没测」与「测过了」必须能分开** —— 这是本项目反复踩过的坑
    （M49 的校验被静默跳过、R0 的 bug 差点被冻成标准）。
    """
    src = Path(args.source)
    if not src.is_dir():
        print(f"❌ 找不到产物目录：{src}", file=sys.stderr)
        return 1

    manifest = _load_manifest()
    if not manifest:
        print("❌ 没有基线 —— 先跑 `python tools/conformance.py freeze`",
              file=sys.stderr)
        return 1

    stage_filter = [args.stage] if args.stage else None
    all_stages = ["tokens", "ast", "bytecode", "diagnostics"]
    want_stages = stage_filter or all_stages

    corpus = collect_corpus()
    mism: list[str] = []
    missing: list[str] = []
    details: list[str] = []
    compared = 0

    for _cat, rel in corpus:
        cid = corpus_id(rel)
        ext_file = src / f"{cid}.json"
        if not ext_file.is_file():
            missing.append(f"外部未产出：{rel}")
            continue
        try:
            got = json.loads(ext_file.read_text(encoding="utf-8"))
        except Exception as e:                                  # noqa: BLE001
            mism.append(f"外部产物读不动：{rel} → {type(e).__name__}: {e}")
            continue
        base = json.loads((BASELINE_DIR / f"{cid}.json").read_text(encoding="utf-8"))
        compared += 1

        for st in want_stages:
            if st not in got:
                missing.append(f"外部未提供「{st}」：{rel}")
                continue
            d = first_diff(base.get(st), got[st], st)
            if d:
                path, exp, gv = d
                mism.append(f"产物不一致：{rel} 在 `{path}`")
                ctx = _context(base, path)
                details.append(
                    f"    {rel}\n"
                    f"      「{path}」" + (f" —— {ctx}" if ctx else "") + "\n"
                    f"        基线：{_short(exp)}\n"
                    f"        外部：{_short(gv)}")

    problems = mism + missing
    total = len(corpus)
    if problems:
        print(f"❌ 外部产物 vs 基线：{compared - len(mism)}/{total} 个语料一致；"
              f"共 {len(problems)} 处问题\n")
        limit = 10_000 if args.verbose else 12
        for x in problems[:limit]:
            print(f"  · {x}")
        if len(problems) > limit:
            print(f"  …还有 {len(problems) - limit} 处")
        if details:
            show = len(details) if args.verbose else 4
            print(f"\n差异明细（前 {min(show, len(details))} 处）：")
            for d in details[:show]:
                print(d)
        return 1

    stages_desc = "、".join(want_stages)
    print(f"✅ 外部产物 vs 基线：{compared}/{total} 个语料在「{stages_desc}」上全部一致")
    print(f"   基线冻结于 Python {manifest['生成环境']['python']} · "
          f"jishi {manifest['生成环境']['jishi']}")
    return 0


# ---------------------------------------------------------------------------
# 四之三、产出「外地产物」（R2 起）—— 让 Rust 前端自己交出 round 的产物
# ---------------------------------------------------------------------------

def cmd_emit(args) -> int:
    """按契约产出一份「外地产物目录」，供 `compare --from` 比对。

    为什么要有它：R2 之后，被验的是 **Rust 前端**的产物。让 Rust 那边自己写
    JSON 的话，「格式」就有两份实现（Python 一份、Rust 一份），一旦不一样，
    `compare` 报出来的差异里会混着「实现错了」与「格式本来就不一样」——
    分不开。所以这里让 **Rust 只负责算 token**，序列化仍旧走本文件这套
    确定性 JSON（`jsonable` + `dumps`），**格式永远只有一份**。

    前端**已经做到哪几个阶段**由 `FRONTEND_STAGES` 决定（R2 交付时只有 `tokens`，
    R3 之后加 `ast` + `diagnostics`）。在词法/语法阶段失败的语料写出
    `null` + `diagnostics`，与基线**同形**。

    ⚠️ **只产出该前端真正做到了的阶段，其余键直接不写** —— 「没提供」与
    「提供了但值不对」必须分得开（契约 §3）：前者 `compare` 会报「未提供」，
    后者才是不一致。
    """
    from jishi import tokenizer as T
    from jishi.errors import JishiError

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # ⚠️ **先清空**：本工具中途失败时若留下上一次的产物，紧接着的 `compare`
    # 会拿**旧产物**比出一个绿 —— 「没跑成功」就伪装成了「通过」。
    # 这正是 R0 那条「跳过校验与校验通过必须可区分」的同一个坑的另一副面孔。
    for stale in out.glob("*.json"):
        stale.unlink()

    if args.frontend == "rust" and not T.rust_available():
        print("❌ 要 Rust 前端，但扩展模块不在 —— 先跑 `python core/build.py`",
              file=sys.stderr)
        return 1

    # ⚠️ 开关只在这段里生效，**退出前还原**。不还原的话，在 pytest 里跑过一次
    #    `emit --frontend rust` 之后，**同一进程里后面所有测试**都会悄悄改走
    #    Rust 前端 —— 那等于把「Python 侧仍是默认」这件事一并测掉了。
    saved = os.environ.get(T.FRONTEND_ENV)
    os.environ[T.FRONTEND_ENV] = args.frontend
    try:
        return _emit_files(T, out, args.frontend)
    finally:
        if saved is None:
            os.environ.pop(T.FRONTEND_ENV, None)
        else:
            os.environ[T.FRONTEND_ENV] = saved


#: 各前端**已经做到**的阶段。R 线每推进一步，这里加一格 ——
#: 产物里不写没做到的阶段，于是 `compare` 会如实报「未提供」而不是假通过。
FRONTEND_STAGES: dict[str, tuple[str, ...]] = {
    "python": ALL_STAGES,
    # R2 分词 + R3 语法；编译器（bytecode）是 R5 的事
    "rust": ("tokens", "ast", "bytecode", "diagnostics"),
}


def _emit_files(T, out: Path, frontend: str) -> int:
    """真正干活的半截（开关与环境还原留给了 `cmd_emit`）。"""
    stages = FRONTEND_STAGES[frontend]
    wrote = 0
    parse_errors = 0
    for _cat, rel in collect_corpus():
        try:
            data = run_stages(rel, stages)
        except ImplementationBug as b:
            # 与 freeze 同一条闸门：非基石错误 = 实现有 bug，不许当成「产物」交出去
            print(f"🚫 {b}", file=sys.stderr)
            return 1
        for st in ALL_STAGES:
            if st not in stages:
                data.pop(st, None)
        if data.get("diagnostics") and data["diagnostics"]["阶段"] == "语法":
            parse_errors += 1
        (out / f"{corpus_id(rel)}.json").write_text(
            dumps(data), encoding="utf-8", newline="\n")
        wrote += 1

    print(f"✅ 已产出 {wrote} 个语料的「{frontend}」前端产物 → {out.as_posix()}")
    print(f"   阶段：{'、'.join(stages)}（**其余阶段不写键**，compare 会报「未提供」）")
    if parse_errors:
        print(f"   其中 {parse_errors} 个在语法阶段报错")
    print(f"   下一步：python tools/conformance.py compare --from {out.as_posix()}"
          f" --stage tokens")
    return 0


def cmd_list(_args) -> int:
    manifest = _load_manifest()
    corpus = collect_corpus()
    counts: dict[str, int] = {}
    for category, _rel in corpus:
        counts[category] = counts.get(category, 0) + 1

    print(f"语料 {len(corpus)} 个（{MANIFEST.relative_to(ROOT).as_posix()} "
          f"{'有' if manifest else '**缺**'}）\n")
    for category in dict.fromkeys(c for c, _ in CORPUS_SOURCES):
        if category in counts:
            print(f"  {category:<10} {counts[category]:>3} 个")
    print()
    for _category, rel in corpus:
        cid = corpus_id(rel)
        dst = BASELINE_DIR / f"{cid}.json"
        state = "已冻结" if dst.exists() else "**未冻结**"
        print(f"  [{state:<10}] {rel}")
    print(f"\n四个阶段：tokens / ast / bytecode / diagnostics")
    return 0


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="conformance",
        description="一致性夹具：冻结/比对前端四个阶段的产物（R0）")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("freeze", help="重新冻结基线（改语料/改前端后跑，**要看 diff**）")
    p.set_defaults(func=cmd_freeze)

    p = sub.add_parser("check", help="比对基线（默认；不一致退 1）")
    p.add_argument("-v", "--verbose", action="store_true", help="多打几处差异")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("list", help="列出语料与冻结状态")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser(
        "emit",
        help="按契约产出一份「外地产物目录」（R2 起：让 Rust 前端自己交产物）")
    p.add_argument("--frontend", choices=["python", "rust"], default="rust",
                   help="用哪个前端产出（默认 rust）")
    p.add_argument("--out", required=True, metavar="目录",
                   help="产物写到哪里（**不要**写进 tests/conformance/）")
    p.set_defaults(func=cmd_emit)

    p = sub.add_parser(
        "compare",
        help="拿**外部实现**的产物与基线比（R1；Rust 前端每写一步都用它验收）")
    p.add_argument("--from", dest="source", required=True, metavar="目录",
                   help="外地产物目录（每个语料一个 JSON，文件名 = 基线的 id）")
    p.add_argument("--stage", choices=["tokens", "ast", "bytecode", "diagnostics"],
                   help="只比这一阶段（如 R2 只做分词时就只比 tokens）")
    p.add_argument("-v", "--verbose", action="store_true", help="多打几处差异")
    p.set_defaults(func=cmd_compare)

    # 不写子命令时默认 check（CI 里少写一个词）；`-v` 这类选项也照常透传。
    # ⚠️ 但 `-h/--help` 例外：要看的是**顶层**帮助（三个子命令），
    # 塞进 check 之后就只看得见 check 自己的选项了。
    raw = list(argv) if argv is not None else sys.argv[1:]
    if not raw or (raw[0] not in ("freeze", "check", "list", "compare", "emit")
                   and raw[0] not in ("-h", "--help")):
        raw = ["check"] + raw
    args = ap.parse_args(raw)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
