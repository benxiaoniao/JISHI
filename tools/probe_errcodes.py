# -*- coding: utf-8 -*-
"""前端错误码体检（R4）：**表里有这个码** ≠ **这个码真的会出现**。

## 为什么要做这件事

错误码表是对用户和模型的**承诺**：`jishi 错误 E0205` 会给出「什么意思 / 常见成因 /
怎么改」，`--ai-card` 里也会列。但如果某个码**永远不会被抛出**，那这份承诺就是空的 ——
用户照着查一个不可能发生的错，模型照着一份描述去猜一个不存在的故障。

以前**从来没人查过这件事**。第一次跑（R4）就抓到两笔：

* **E0205「括号没有闭合」是死码** —— `ParseUnclosedError` 定义在 `errors.py`、
  说明写在 `errcodes.py`，但**全仓库没有任何地方抛出它**（括号没闭合其实报的是
  E0201）。一个对外承诺、却永远不会出现的错误码。
  → ✅ **2026-10-01 拍板接上**：期望右括号而**已到文件末尾** ⇒ E0205，
  而且报错位置指向**那个没闭合的开括号**（EOF 的位置是「末行第 1 列」、
  连源码行都显示不出来，指着它没用）。它现在有语料了。
* **E0204「这里需要一个名字」可达、却一个语料都没有** —— 八个形状能触发它
  （`令 = 1` / `甲.()` / `函数 ()：` …），此前一个都没进夹具。

## 判据（三档，缺一档就报缺口）

| 档 | 判据 | 说明 |
|---|---|---|
| ① 语料钉住 | 该码在 `tests/conformance/baseline/` 的某个语料里出现过 | **最硬**：它被冻结进基线了，两个前端必须逐字一致 |
| ② 已登记为不可达 | 在下面的 `UNREACHABLE` 表里，且**原因经得起复核** | 登记不是许愿：`verify_unreachable()` 会去静态复核「是不是真没人引用」 |
| ③ 缺口 | 都不满足 | **报出来**，出口码 1 |

⚠️ **登记表要能被复核**（本项目的老规矩）。所以「不可达」不是写一句就算：
* `基类` 类登记 —— 复核「它有子类，且每个子类的码不在本表里」；
* `死码` 类登记 —— 复核「实现代码里搜不到它」（见 `_code_mentions()`），
  哪天有人把 E0205 接上了，这条登记就会**报红提醒复核**，而不是悄悄过期。

用法：
    python tools/probe_errcodes.py            # 体检
    python tools/probe_errcodes.py -v         # 连「每个码的触发语料」一起列
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

#: 前端错误码的号段（**前端** = 词法 + 语法；语义/运行期不在这里）
FRONT_PREFIXES = ("E01", "E02")

#: 「结构上不可达」的登记表：码 → `(类别, 原因)`。
#:
#: ⚠️ 只允许两种类别：
#:   * `基类`  —— 家族根，只作 `isinstance` 用，从不抛出；
#:   * `死码`  —— 本该能抛、但实现里没有触发点（**要么接上，要么删掉**）。
#: `死码` 是要**报给作者拍板**的，别在这里长期挂着当挡箭牌。
UNREACHABLE: dict[str, tuple[str, str]] = {
    "E0100": ("基类", "词法错误的家族根（LexError），只作 isinstance 用"),
    "E0200": ("基类", "语法错误的家族根（ParseError），只作 isinstance 用"),
}

#: 静态复核「死码」时扫的实现目录（**不含 tests / docs**：
#: 测试里列一份码表不算「有人抛出它」）。
_IMPL_DIRS = ("oracle/jishi", "core/src", "frontend/src", "rust/src", "node", "cvm")
_IMPL_SKIP = {"oracle/jishi/errors.py", "oracle/jishi/errcodes.py"}


def front_codes() -> "dict[str, type]":
    """枚举前端（词法 + 语法）的全部错误码 → 异常类。"""
    import inspect

    from oracle.jishi import errors as E

    out: "dict[str, type]" = {}
    for obj in vars(E).values():
        if (isinstance(obj, type) and issubclass(obj, E.JishiError)
                and obj is not E.JishiError):
            code = getattr(obj, "code", "")
            if code.startswith(FRONT_PREFIXES):
                out.setdefault(code, obj)
    return out


def corpus_hits() -> "dict[str, list[str]]":
    """`{错误码: [语料…]}` —— 从**冻结基线**里读（不是现跑，读的就是被钉住的那份）。"""
    import json

    base = ROOT / "tests" / "conformance" / "baseline"
    hits: "dict[str, list[str]]" = {}
    for p in sorted(base.glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        dg = data.get("diagnostics")
        if dg and dg.get("码"):
            name = p.stem.split("__")[-1]
            hits.setdefault(dg["码"], []).append(name)
    return hits


def _code_mentions(code: str) -> "list[str]":
    """实现代码里**引用过**这个码的文件（排除错误定义表本身）。

    只认码字面量（`E0205`）—— 类名引用是「定义/派生」，不算触发点；
    真正的触发一定会走到某个 `raise …` 上，而实现里写的是码或类名。
    所以这里把码**和类名**都查一遍，由调用方判断。
    """
    import oracle.jishi.errors as E

    names = {code}
    for obj in vars(E).values():
        if isinstance(obj, type) and getattr(obj, "code", None) == code:
            names.add(obj.__name__)

    found: "list[str]" = []
    for d in _IMPL_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.suffix not in (".py", ".rs", ".js"):
                continue
            rel = p.relative_to(ROOT).as_posix()
            if rel in _IMPL_SKIP or "/target/" in rel or "/dist/" in rel:
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if any(n in text for n in names):
                found.append(rel)
    return found


def verify_unreachable(codes: "dict[str, type]") -> "list[str]":
    """复核登记表，返回问题列表（空 = 全部经得起复核）。"""
    import oracle.jishi.errors as E

    problems: "list[str]" = []
    for code, (kind, _why) in UNREACHABLE.items():
        if code not in codes:
            problems.append(f"{code}：登记为「{kind}」，但实现里已经没有这个码了 —— "
                            f"删掉这行登记")
            continue
        cls = codes[code]
        if kind == "基类":
            subs = [o for o in vars(E).values()
                    if isinstance(o, type) and issubclass(o, cls)
                    and o is not cls]
            if not subs:
                problems.append(f"{code}：登记为「基类」，但它一个子类都没有 —— "
                                f"那就不是基类，是死码")
        elif kind == "死码":
            used = _code_mentions(code)
            if used:
                problems.append(
                    f"{code}：登记为「死码」，但实现里已经有人引用它了 "
                    f"（{', '.join(used[:3])}）—— 复核：是接上了，还是只借了名字？")
        else:
            problems.append(f"{code}：登记类别「{kind}」不认识（只允许 基类 / 死码）")
    return problems


def main(argv: "list[str] | None" = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="probe_errcodes", description="R4：前端错误码体检（可达性 + 语料覆盖）")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="连每个码的触发语料一起列")
    args = ap.parse_args(argv)

    codes = front_codes()
    hits = corpus_hits()

    print(f"前端错误码体检（词法 E01xx + 语法 E02xx）：共 {len(codes)} 个码\n")
    print(f"  {'码':<7} {'标题':<16} {'触发'}")
    print("  " + "-" * 66)

    gaps: "list[str]" = []
    for code in sorted(codes):
        cls = codes[code]
        title = getattr(cls, "title", "?")
        if code in hits:
            mark = f"语料 {len(hits[code])} 个"
            if args.verbose:
                mark += "：" + "、".join(hits[code])
        elif code in UNREACHABLE:
            kind, why = UNREACHABLE[code]
            mark = f"登记不可达（{kind}）：{why}"
        else:
            mark = "❌ **既没语料、也没登记**"
            gaps.append(code)
        print(f"  {code:<7} {title:<16} {mark}")

    problems = verify_unreachable(codes)
    print()
    reachable = [c for c in codes if c not in UNREACHABLE]
    covered = [c for c in reachable if c in hits]
    print(f"  可达码 {len(reachable)} 个，其中 {len(covered)} 个有语料钉住")
    print(f"  登记不可达 {len(UNREACHABLE)} 个："
          f"{'、'.join(sorted(UNREACHABLE))}")

    if gaps:
        print(f"\n❌ 有 {len(gaps)} 个码既没有语料、也没登记：{'、'.join(gaps)}")
    if problems:
        print(f"\n❌ 不可达登记有 {len(problems)} 处经不起复核：")
        for p in problems:
            print(f"   · {p}")
    if gaps or problems:
        print("\n（`tests/test_m61_frontend_errors.py` 也盯着这两件事，会一并报红）")
        return 1

    print("\n✅ 每个前端错误码：要么有语料钉住，要么是不可达且登记得经得起复核")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
