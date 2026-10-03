# -*- coding: utf-8 -*-
"""M45 · AI 评测与工具面：题集自洽、跑分器契约、MCP 工具补齐。

三块，都在这里钉住：

1. **题集自洽性**（`evals/tasks/*.json`）——这是跑分表可信的前提。
   调试题的「缺陷代码」**必须真的失败**；重构题的「原始代码」**必须真的跑出标准输出**。
   任一条不成立，跑分表就是错的（M45 写题集时真撞见过：一道差一越界的题，
   报错前已经输出了正确内容，只看 stdout 会把无效题目当成有效）。

2. **跑分器契约**（`tools/eval_ai.py`）——判分口径、限流重试、围栏剥离、
   报告字段。这几条错了，跑出去的数字就不可信。

3. **MCP 工具面**（`jishi/mcp_server.py`）——从 3 个补到 7 个，每个都有正例与
   边界（查不到、参数缺、源码有语法错）。

**不联网**：跑分器里真正发请求的那条路不测（要 key、要钱、结果还不确定）；
测的是它**不调模型也能验**的部分。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from jishi import mcp_server                                    # noqa: E402
from jishi.errors import JishiError                             # noqa: E402
from jishi.parser import parse                                  # noqa: E402
from jishi.tokenizer import tokenize                            # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TASKS_DIR = ROOT / "evals" / "tasks"

sys.path.insert(0, str(ROOT / "tools"))
import eval_ai                                                   # noqa: E402


# ---------------------------------------------------------------------------
# 一、题集自洽性
# ---------------------------------------------------------------------------

def _all_tasks() -> list[dict]:
    out = []
    for path in sorted(TASKS_DIR.glob("*.json")):
        out.extend(json.loads(path.read_text(encoding="utf-8"))["题集"])
    return out


def test_题集文件都在():
    names = {p.name for p in TASKS_DIR.glob("*.json")}
    assert names == {"01_syntax.json", "02_stdlib.json",
                     "03_debug.json", "04_refactor.json"}, names


def test_题集四类题量与设计一致():
    tasks = _all_tasks()
    by_type: dict[str, int] = {}
    for t in tasks:
        by_type[t["类型"]] = by_type.get(t["类型"], 0) + 1
    assert by_type == {"语法": 12, "标准库": 15, "调试": 10, "重构": 8}, by_type


def test_题号唯一():
    ids = [t["id"] for t in _all_tasks()]
    assert len(ids) == len(set(ids)), "题号有重复"


def test_每道题都有判分必需字段():
    for t in _all_tasks():
        assert t["题目"].strip(), t["id"]
        assert t["标准输出"] != "", f"{t['id']} 缺标准输出"
        assert t.get("考点") or t.get("缺陷") or t.get("目标"), \
            f"{t['id']} 缺考点/缺陷/目标说明"
        if t["类型"] == "调试":
            assert t["有缺陷代码"].strip(), f"{t['id']} 缺缺陷代码"
        if t["类型"] == "重构":
            assert t["原始代码"].strip(), f"{t['id']} 缺原始代码"


@pytest.mark.慢
def test_题集自洽_缺陷代码确实失败_原始代码确实跑通():
    """跑分表可信的前提。这是**真的跑基石**，所以慢。

    判据同时看退出码：程序崩了但崩溃前恰好输出了正确内容 → 不算「跑通」。
    """
    problems = eval_ai.self_check(eval_ai.load_tasks(), verbose=False)
    assert problems == [], "\n".join(problems)


# ---------------------------------------------------------------------------
# 二、跑分器契约
# ---------------------------------------------------------------------------

def test_判分口径_输出相同且退出码为零才算过():
    """只输出对不行——程序崩了不算对。"""
    ok, r = eval_ai.judge("打印(1)", "1")
    assert ok and r.returncode == 0
    # 输出对，但程序崩了（引用一个不存在的名字）
    ok2, r2 = eval_ai.judge("打印(1)\n打印(根本没有这个名字)", "1")
    assert not ok2, "程序崩溃却判为通过——判分太松"
    assert r2.returncode != 0


def test_判分口径_末尾空白不算差异():
    ok, _ = eval_ai.judge("打印(1)\n打印(2)\n", "1\n2\n\n")
    assert ok


def test_判分口径_多输出一行就算错():
    ok, _ = eval_ai.judge("打印(1)\n打印(2)", "1")
    assert not ok, "多输出的行内容不算错——判分太松"


def test_剥围栏_带语言标记():
    assert eval_ai.strip_code_fence("```jsh\n打印(1)\n```") == "打印(1)"


def test_剥围栏_不带语言标记():
    assert eval_ai.strip_code_fence("```\n打印(1)\n```") == "打印(1)"


def test_剥围栏_没有围栏时原样返回():
    assert eval_ai.strip_code_fence("打印(1)") == "打印(1)"


def test_剥围栏_只有前半截围栏():
    assert eval_ai.strip_code_fence("```jsh\n打印(1)") == "打印(1)"


def test_限流会被识破并重试():
    """429 / RATE_LIMITED / 503 这些是「等一等就过」，不能记成模型失败。"""
    for sig in ("HTTP 429：{\"code\":\"RATE_LIMITED\"}", "HTTP 503", "timed out"):
        assert eval_ai._retryable(sig), sig
    # 参数错这种重试没用
    assert not eval_ai._retryable("HTTP 400：bad request")


def test_题集装载_按类型过滤():
    tasks = eval_ai.load_tasks(only_type="调试")
    assert len(tasks) == 10
    assert all(t.类型 == "调试" for t in tasks)


def test_题集装载_限制条数():
    assert len(eval_ai.load_tasks(limit=3)) == 3


def test_题集装载_起始代码来自正确的字段():
    by_id = {t.id: t for t in eval_ai.load_tasks()}
    调试题 = next(t for t in by_id.values() if t.类型 == "调试")
    重构题 = next(t for t in by_id.values() if t.类型 == "重构")
    语法题 = next(t for t in by_id.values() if t.类型 == "语法")
    assert 调试题.起始代码 and 重构题.起始代码
    assert 语法题.起始代码 == "", "语法题是「从零写」，不该有起始代码"


def test_提示词_要求只输出代码不要围栏():
    prompt = eval_ai.build_system_prompt("规格")
    assert "不要" in prompt and "围栏" in prompt


def test_提示词_带起始代码时说明是修还是重写():
    tasks = {t.id: t for t in eval_ai.load_tasks()}
    调试 = next(t for t in tasks.values() if t.类型 == "调试")
    重构 = next(t for t in tasks.values() if t.类型 == "重构")
    assert "修复" in eval_ai.build_user_prompt(调试)
    assert "重写" in eval_ai.build_user_prompt(重构)


def test_提示词_把期望输出写清楚():
    t = eval_ai.load_tasks(limit=1)[0]
    assert t.标准输出 in eval_ai.build_user_prompt(t)


def test_回灌报错时带上上次代码与期望输出():
    t = eval_ai.load_tasks(limit=1)[0]
    text = eval_ai.build_fix_prompt(t, "打印(1)", "错误 E0301：找不到这个名字")
    assert "打印(1)" in text
    assert t.标准输出 in text
    assert "E0301" in text


def test_报告含三个核心指标():
    outs = [
        eval_ai.TaskOutcome("甲", "语法", True, 1, 期望输出="1", 实际输出="1"),
        eval_ai.TaskOutcome("乙", "语法", False, 4, 期望输出="2", 实际输出="3"),
    ]
    report = eval_ai.render_report("试模型", outs, "abcd1234", 3, "2026-01-01", 1.0)
    assert "一次通过率" in report
    assert "最终通过率" in report
    assert "平均修复轮数" in report
    assert "abcd1234" in report, "报告要带规格指纹——不同指纹的结果不可比"


def test_报告如实写出跳过的题():
    outs = [
        eval_ai.TaskOutcome("甲", "语法", True, 1, 期望输出="1", 实际输出="1"),
        eval_ai.TaskOutcome("乙", "调试", False, 1, True, "HTTP 429 限流",
                            期望输出="2"),
    ]
    report = eval_ai.render_report("试模型", outs, "hash", 3, "t", 1.0)
    assert "跳过" in report
    assert "429" in report
    # 跳过的不算进分母：一次通过率分母应为 1（只有「甲」是有效题）
    assert "100.0%（1/1）" in report


def test_汇总表逐模型一行():
    results = {
        "甲模型": [eval_ai.TaskOutcome("a", "语法", True, 1, 期望输出="1", 实际输出="1")],
        "乙模型": [eval_ai.TaskOutcome("a", "语法", False, 4, 期望输出="1", 实际输出="2")],
    }
    table = eval_ai.summarize_table(results, 3)
    assert "甲模型" in table and "乙模型" in table
    assert "100.0%（1/1）" in table
    assert "0.0%（0/1）" in table


def test_报告里的输出换行被转义成一行():
    assert eval_ai._oneline("甲\n乙") == "甲\\n乙"


def test_题集自检能识别出无效的调试题():
    """反例：把一道「其实能跑对」的代码标成调试题，自检必须报出来。"""
    bad = eval_ai.Task(id="假调试题", 类型="调试", 题目="x",
                       标准输出="1", 起始代码="打印(1)")
    problems = eval_ai.self_check([bad], verbose=False)
    assert problems and "竟然直接跑出正确输出" in problems[0]


def test_题集自检能识别出不自洽的重构题():
    bad = eval_ai.Task(id="假重构题", 类型="重构", 题目="x",
                       标准输出="要跑出来的东西", 起始代码="打印(1)")
    problems = eval_ai.self_check([bad], verbose=False)
    assert problems and "跑不出标准输出" in problems[0]


# ---------------------------------------------------------------------------
# 三、MCP 工具面
# ---------------------------------------------------------------------------

def test_工具清单是七个():
    names = [t["name"] for t in mcp_server.TOOLS]
    assert names == ["run_script", "eval_expr", "describe_language",
                     "check_source", "lookup_error", "lookup_stdlib",
                     "format_source"]


def test_每个工具都有描述与输入模式():
    for t in mcp_server.TOOLS:
        assert t["description"].strip(), t["name"]
        assert t["inputSchema"]["type"] == "object", t["name"]


def test_未知工具给出可读错误():
    d = json.loads(mcp_server._dispatch("查无此工具", {}))
    assert d["ok"] is False
    assert "查无此工具" in d["error"]["message"]


def test_静态检查工具报出未定义名():
    d = json.loads(mcp_server._dispatch(
        "check_source", {"source": "打印(从来没定义过)"}))
    assert d["ok"] is True
    codes = [i["code"] for i in d["问题"]]
    assert "name.undefined" in codes


def test_静态检查工具在干净代码上不报问题():
    d = json.loads(mcp_server._dispatch(
        "check_source", {"source": "令 甲 = 1\n打印(甲)\n"}))
    assert d["ok"] is True
    assert d["问题"] == []


def test_静态检查工具语法错也当问题返回不带崩():
    d = json.loads(mcp_server._dispatch("check_source", {"source": "如果 真"}))
    assert d["ok"] is True
    assert d["问题数"] == 1
    assert d["问题"][0]["level"] == "error"


def test_查错误码_命中():
    d = json.loads(mcp_server._dispatch("lookup_error", {"code": "E2002"}))
    assert d["ok"] is True and d["kind"] == "错误码"
    assert d["title"] and d["class"]


def test_查错误码_大小写不敏感():
    a = json.loads(mcp_server._dispatch("lookup_error", {"code": "e2002"}))
    assert a["ok"] is True and a["code"] == "E2002"


def test_查错误码_也能查静态检查码():
    d = json.loads(mcp_server._dispatch("lookup_error",
                                        {"code": "name.undefined"}))
    assert d["ok"] is True and d["kind"] == "静态检查码"


def test_查错误码_未命中时列出可用码():
    d = json.loads(mcp_server._dispatch("lookup_error", {"code": "E9999"}))
    assert d["ok"] is False
    assert "E2002" in d["可用错误码"]


def test_查错误码_缺参数时如实报错():
    d = json.loads(mcp_server._dispatch("lookup_error", {}))
    assert d["ok"] is False and "code" in d["error"]["message"]


def test_查标准库_列整个模块():
    d = json.loads(mcp_server._dispatch("lookup_stdlib", {"module": "数学"}))
    assert d["ok"] is True
    assert d["用法"] == "导入 数学"
    names = [f["name"] for f in d["函数"]]
    assert "四舍五入" in names and "最大公约数" in names


def test_查标准库_查单个函数带签名():
    d = json.loads(mcp_server._dispatch(
        "lookup_stdlib", {"module": "日期", "function": "加天数"}))
    assert d["ok"] is True
    assert d["sig"] == "加天数(天数, 日期文本)", "参数顺序是考点，必须准确"
    assert d["doc"]


def test_查标准库_模块不存在时列出全部模块():
    d = json.loads(mcp_server._dispatch("lookup_stdlib", {"module": "查无此模块"}))
    assert d["ok"] is False
    assert "数学" in d["可用模块"]


def test_查标准库_函数不存在时列出该模块的函数():
    d = json.loads(mcp_server._dispatch(
        "lookup_stdlib", {"module": "数学", "function": "查无此函数"}))
    assert d["ok"] is False
    assert "四舍五入" in d["可用函数"]


def test_查标准库_覆盖全部十九个模块():
    from jishi.ai import build_lang_spec
    for mod in build_lang_spec()["stdlib"]:
        d = json.loads(mcp_server._dispatch("lookup_stdlib",
                                            {"module": mod["module"]}))
        assert d["ok"] is True, mod["module"]
        assert len(d["函数"]) == len(mod["functions"]), mod["module"]


def test_格式化工具_统一缩进宽度():
    d = json.loads(mcp_server._dispatch(
        "format_source", {"source": "如果 真:\n        打印(\"a\")\n"}))
    assert d["ok"] is True
    assert d["格式化后"] == "如果 真:\n    打印(\"a\")\n"
    assert d["是否已符合风格"] is False


def test_格式化工具_已符合风格时报true():
    d = json.loads(mcp_server._dispatch(
        "format_source", {"source": "如果 真:\n    打印(\"a\")\n"}))
    assert d["ok"] is True and d["是否已符合风格"] is True


def test_格式化工具_语法错时拒绝返回半成品():
    """格式化器本身不报语法错（只按缩进重排文本），工具层必须把它拦住。"""
    d = json.loads(mcp_server._dispatch("format_source", {"source": "如果 真"}))
    assert d["ok"] is False, "语法错却返回了「格式化结果」——agent 会以为拿到干净代码"
    assert "语法错" in d["error"]["message"]
    assert d["error"].get("line") == 1


def test_格式化工具_支持自定义缩进():
    d = json.loads(mcp_server._dispatch(
        "format_source", {"source": "如果 真:\n        打印(\"a\")\n",
                          "indent": 2}))
    assert d["ok"] is True
    assert d["格式化后"] == "如果 真:\n  打印(\"a\")\n"


def test_工具查到的错误码与语言规格一致():
    """工具不许另抄一份表：它查到的必须是 `build_lang_spec()` 里那一条。"""
    from jishi.ai import build_lang_spec
    spec_codes = {c["code"]: c for c in build_lang_spec()["error_codes"]}
    for code in ("E0000", "E2002", "E0203"):
        got = json.loads(mcp_server._dispatch("lookup_error", {"code": code}))
        assert got["title"] == spec_codes[code]["title"], code


def test_工具查到的标准库与语言规格一致():
    from jishi.ai import build_lang_spec
    spec_mods = {m["module"]: m for m in build_lang_spec()["stdlib"]}
    got = json.loads(mcp_server._dispatch("lookup_stdlib", {"module": "容器"}))
    规格函数 = [f["name"] for f in spec_mods["容器"]["functions"]]
    assert [f["name"] for f in got["函数"]] == 规格函数


# ---------------------------------------------------------------------------
# 三·补、语言规格指纹必须跨进程稳定
# ---------------------------------------------------------------------------
#
# 指纹的**唯一用途**就是「同一指纹下跑分结果才可比」。M45 收尾时实测抓到：
# 同一份代码连跑三次得到三个指纹（`b67363fb` / `1beb9fde` / `4be64793`）——
# 根因是 `_ASSIGN_OPS` 是 set，`list(set)` 的顺序取决于 PYTHONHASHSEED。
# 这条用**子进程**验证：同进程内算多少次都一样，只有换进程才暴露。

def _子进程算指纹() -> str:
    from jishi.ai import build_lang_spec, content_hash
    code = (
        "import sys; sys.path.insert(0, '.'); "
        "from jishi.ai import build_lang_spec, content_hash; "
        "print(content_hash(build_lang_spec()))"
    )
    import subprocess
    import sys as _sys
    r = subprocess.run([_sys.executable, "-c", code], capture_output=True,
                       text=True, encoding="utf-8", cwd=str(ROOT))
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_语言规格指纹跨进程稳定():
    """换进程重算指纹，必须还是同一个——否则「同一指纹才可比」是空话。"""
    指纹们 = {_子进程算指纹() for _ in range(3)}
    assert len(指纹们) == 1, f"同一份代码算出多个指纹：{指纹们}"


def test_赋值运算符在规格里按序排列():
    """回归钉子：`assign` 直接从 set 转 list，顺序随 PYTHONHASHSEED 抖动。"""
    from jishi.ai import build_lang_spec
    assign = build_lang_spec()["operators"]["assign"]
    assert assign == sorted(assign), f"assign 未排序：{assign}"
    assert set(assign) == {"=", "+=", "-=", "*=", "/=", "//=", "%=", "**="}


def test_规格整体可序列化且顺序确定():
    """逼一层：整个规格 `sort_keys` 序列化后，跨进程逐字节一致。"""
    import json as _json
    import subprocess
    import sys as _sys
    from jishi.ai import build_lang_spec

    local = _json.dumps(build_lang_spec(), ensure_ascii=False, sort_keys=True)
    code = (
        "import sys, json; sys.path.insert(0, '.'); "
        "from jishi.ai import build_lang_spec; "
        "print(json.dumps(build_lang_spec(), ensure_ascii=False, sort_keys=True))"
    )
    r = subprocess.run([_sys.executable, "-c", code], capture_output=True,
                       text=True, encoding="utf-8", cwd=str(ROOT))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == local, "规格序列化跨进程不一致"


# ---------------------------------------------------------------------------
# 四、写题集时抓到的真缺陷：复合下标赋值漏出裸 KeyError
# ---------------------------------------------------------------------------

#: 「统计词频」是最常见的写法之一——`字典[新键] += 1` 必然先读一个不存在的键。
#: 修之前它漏成「程序内部出现未预料的问题（KeyError）：'新词'」，
#: 既没有 E 码、也不是中文可操作提示，还盖住了「这里该先判断键在不在」这个真答案。
复合下标赋值读不存在的键 = '令 频次 = {}\n频次["新词"] += 1\n'


def _run_tree(src: str) -> str:
    import io
    from contextlib import redirect_stdout

    from jishi.interpreter import Interpreter
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            Interpreter(filename="<t>").run(
                parse(tokenize(src, "<t>"), src.split("\n"), "<t>"))
    except JishiError as e:
        return f"{e.code}|{e.title}"
    except Exception as e:  # noqa: BLE001
        return f"裸异常|{type(e).__name__}"
    return buf.getvalue()


def _run_pvm(src: str) -> str:
    import io
    from contextlib import redirect_stdout

    from jishi import vm as vm_mod
    from jishi.compiler import compile_source
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            vm_mod.VM(compile_source(src, "<t>"), "<t>").run()
    except JishiError as e:
        return f"{e.code}|{e.title}"
    except Exception as e:  # noqa: BLE001
        return f"裸异常|{type(e).__name__}"
    return buf.getvalue()


def _run_cvm(src: str) -> str:
    import io
    from contextlib import redirect_stdout

    from jishi import cvm_bind
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            cvm_bind.run_source_c(src, "<t>")
    except JishiError as e:
        return f"{e.code}|{e.title}"
    except Exception as e:  # noqa: BLE001
        return f"裸异常|{type(e).__name__}"
    return buf.getvalue()


def test_复合下标赋值读不存在的键_树遍历报中文键错误():
    got = _run_tree(复合下标赋值读不存在的键)
    assert got.startswith("E2004|"), got
    assert "找不到这个键" in got


def test_复合下标赋值读不存在的键_不出裸异常():
    """这条是回归钉子：修之前是 `裸异常|KeyError`。"""
    for name, run in (("树遍历", _run_tree), ("Python VM", _run_pvm),
                      ("C VM", _run_cvm)):
        got = run(复合下标赋值读不存在的键)
        assert not got.startswith("裸异常"), f"{name} 漏出裸异常：{got}"


def test_复合下标赋值读不存在的键_三执行器一致():
    """三执行器铁律：报错码与标题必须逐字相同。"""
    results = {name: run(复合下标赋值读不存在的键)
               for name, run in (("树遍历", _run_tree), ("Python VM", _run_pvm),
                                 ("C VM", _run_cvm))}
    assert len(set(results.values())) == 1, results


def test_复合下标赋值正常路径仍然对():
    """修的时候动了这条路径，得确认没把正常用法改坏。"""
    src = '令 频次 = {"甲": 1}\n频次["甲"] += 2\n打印(频次["甲"])\n'
    assert _run_tree(src).strip() == "3"
    assert _run_pvm(src).strip() == "3"
    assert _run_cvm(src).strip() == "3"
