# -*- coding: utf-8 -*-
"""M51 · 一致性止血：**漂移检测** + 宿主**命名参数**缺口。

## 这个文件防的是什么

M50 把标准库从 19 个模块扩到 25 个、分了两批做。做完之后有两个洞：

1. **宿主静默缺模块 / 缺函数**。M50 第一批只做了 Python 侧，Node / Rust 上
   `导入 统计` 直接报「没有找到标准库模块」——**是手工去测才发现的**，
   没有一条测试报红。同一批还漏了更早的：`数学.正弦` / `文本.截断` /
   `迭代.排列` 等 13 个函数在 Node 上压根没有。
2. **命名参数被静默丢掉**。`加密.摘要("abc", 算法="md5")` 在 Python 上给
   md5、在两个宿主上**悄悄给 sha256**（kwargs 到了宿主就被扔了，一声不响）。
   对拍测试抓不到，因为没人写这条用例。

所以本文件做两件事：

- **漂移检测**：由宿主自己 `--dump-stdlib` 报家底，断言它覆盖 Python 侧
  的全部模块与函数。比让测试去正则解析宿主源码靠谱（解析一改就崩）。
- **命名参数一致**：同一段代码在五个引擎上要么给同一结果，要么**以同一类
  异常报错**（绝不静默）。

## 三个执行器 + 两个宿主 = 五引擎

树遍历 / Python VM / C VM（三执行器）+ Node / Rust（两跨语言宿主）。
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import rust_exe_path, tmp_bc_path      # noqa: E402

from jishi import ai                                 # noqa: E402
from jishi import serialize                          # noqa: E402
from jishi import vm as vm_mod                       # noqa: E402
from jishi.compiler import compile_source            # noqa: E402
from jishi.errors import JishiError                  # noqa: E402
from jishi.interpreter import run_source as tree_run  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
RUST = rust_exe_path()


# ---------------------------------------------------------------------------
# 显式豁免 / 已知缺口
# ---------------------------------------------------------------------------

#: Python 侧有、**宿主故意没有**的模块（豁免必须写出来，不许静默跳过）。
#:
#: - `测试`：基石自带的测试框架，靠 Python 的进程模型跑用例，
#:   跨语言宿主没有对应物。
HOST_EXEMPT: frozenset[str] = frozenset({"测试"})

#: **已知缺口**：检测出来但本阶段不补的。断言要求「实际缺口**恰好等于**这些」——
#: 补掉一个就得来删一行，冒出新的立刻报红。
#:
#: `时间.计时` 返回的是一个**带方法的对象**（`耗时()` / `停()`），还要支持
#: 上下文协议（`用 时间.计时("x") 为 t：` 脱糖成 `进入` / `退出`）。
#: 而两宿主的 `ctxMethod` 只认「Jishi 实例」和「内建容器」，标准库直接
#: 返回的宿主对象走不通 —— 这不是补一个函数的事，是**宿主对象模型**的缺口。
#:
#: 归属 **M52/M53「基石库层」**：把 `计时` 改写成**基石**模块之后它就是
#: 普通的 Jishi 实例，五引擎都能跑，这类缺口会结构性地消失。
KNOWN_GAPS: dict[str, set[str]] = {
    "Node": {"时间.计时"},
    "Rust": {"时间.计时"},
}


# ---------------------------------------------------------------------------
# 各引擎的标准库家底
# ---------------------------------------------------------------------------

def _py_stdlib() -> dict[str, list[str]]:
    """Python 侧的事实源：`jishi --lang-spec` 的 stdlib 节（**不含豁免模块**）。"""
    spec = ai.build_lang_spec()
    return {
        m["module"]: sorted(f["name"] for f in m["functions"])
        for m in spec["stdlib"]
        if m["module"] not in HOST_EXEMPT
    }


def _node_stdlib() -> dict[str, list[str]]:
    if not NODE:
        pytest.skip("没有 node")
    r = subprocess.run([NODE, str(ROOT / "node" / "index.js"), "--dump-stdlib"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"Node --dump-stdlib 失败：{r.stderr[:300]}"
    return json.loads(r.stdout)


def _rust_stdlib() -> dict[str, list[str]]:
    if not RUST.exists():
        pytest.skip(f"没有 Rust 宿主：{RUST}（先 cargo build --release）")
    r = subprocess.run([str(RUST), "--dump-stdlib"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"Rust --dump-stdlib 失败：{r.stderr[:300]}"
    return json.loads(r.stdout)


# ---------------------------------------------------------------------------
# 差异计算（本身也被测 —— 见 test_检测本身真的能报红）
# ---------------------------------------------------------------------------

def _missing(expected: dict[str, list[str]],
             actual: dict[str, list[str]]) -> set[str]:
    """宿主**缺**什么。键是 `模块.函数`，整个模块都缺时键就是 `模块`。"""
    out: set[str] = set()
    for mod, fns in expected.items():
        if mod not in actual:
            out.add(mod)
            continue
        for fn in fns:
            if fn not in actual[mod]:
                out.add(f"{mod}.{fn}")
    return out


def _extra(expected: dict[str, list[str]],
           actual: dict[str, list[str]]) -> set[str]:
    """宿主**多**什么（Python 侧没有的）。多出来的同样是漂移 —— 用户写不出来。"""
    out: set[str] = set()
    for mod, fns in actual.items():
        if mod not in expected:
            out.add(mod)
            continue
        for fn in fns:
            if fn not in expected[mod]:
                out.add(f"{mod}.{fn}")
    return out


def _report(host: str, expected: dict, actual: dict) -> str:
    miss, extra = _missing(expected, actual), _extra(expected, actual)
    lines = []
    if miss:
        lines.append(f"  {host} 缺：{'、'.join(sorted(miss))}")
    if extra:
        lines.append(f"  {host} 多（Python 侧没有）：{'、'.join(sorted(extra))}")
    return "\n".join(lines) or "（无差异）"


# ---------------------------------------------------------------------------
# 一、漂移检测
# ---------------------------------------------------------------------------

def test_检测本身真的能报红():
    """给「漂移检测」自己上保险。

    没有这条，`_missing` 写错一行（比如永远返回空集）测试照样全绿 ——
    那就是个空壳。所以拿人工构造的例子把四条路都钉住：
    缺模块、缺函数、多模块、多函数。
    """
    exp = {"数学": ["开方", "平方"], "文本": ["拼接"]}
    assert _missing(exp, dict(exp)) == set()
    assert _missing(exp, {"数学": ["开方"], "文本": ["拼接"]}) == {"数学.平方"}
    assert _missing(exp, {"数学": ["开方", "平方"]}) == {"文本"}
    assert _missing(exp, {"数学": ["开方", "平方"], "文本": ["拼接"], "统计": []}) == set()

    assert _extra(exp, dict(exp)) == set()
    assert _extra(exp, {"数学": ["开方", "平方", "多余"], "文本": ["拼接"]}) == {"数学.多余"}
    assert _extra(exp, {"数学": ["开方", "平方"], "文本": ["拼接"], "统计": []}) == {"统计"}


def test_Node_覆盖_python_侧标准库():
    expected, actual = _py_stdlib(), _node_stdlib()
    assert actual, "Node 报了个空家底？"
    assert _missing(expected, actual) == KNOWN_GAPS["Node"], (
        "Node 宿主与 Python 侧标准库有漂移：\n" + _report("Node", expected, actual))
    assert _extra(expected, actual) == set(), (
        "Python 侧没有、Node 却有的符号：\n" + _report("Node", expected, actual))


def test_Rust_覆盖_python_侧标准库():
    expected, actual = _py_stdlib(), _rust_stdlib()
    assert actual, "Rust 报了个空家底？"
    assert _missing(expected, actual) == KNOWN_GAPS["Rust"], (
        "Rust 宿主与 Python 侧标准库有漂移：\n" + _report("Rust", expected, actual))
    assert _extra(expected, actual) == set(), (
        "Python 侧没有、Rust 却有的符号：\n" + _report("Rust", expected, actual))


def test_两个宿主彼此也不漂移():
    """Python 侧是共同基准，但两宿主之间也要互相对得上（免得只有一边修）。

    这条**不含 `KNOWN_GAPS`**：已知缺口是「两宿主对 Python 的」，
    而这里问的是「Node 和 Rust 是否一样」—— 一样才说明没有单边脱落。
    """
    nd, rs = _node_stdlib(), _rust_stdlib()
    assert _extra(nd, rs) == set(), _report("Rust 比 Node 多", nd, rs)
    assert _extra(rs, nd) == set(), _report("Node 比 Rust 多", rs, nd)


def test_签名表与标准库源码同步():
    """命名参数的映射表是从 `jishi/stdlib/*.py` 生成的，改了库就要重新生成。

    忘了重新生成 → 宿主的 kwargs 映射就会用**旧参数名**，这条直接报红。
    """
    r = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "gen_stdlib_sigs.py"), "--check"],
        capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")


# ---------------------------------------------------------------------------
# 一之三、**内建函数**的漂移检测（M55 新建）
# ---------------------------------------------------------------------------
#
# 为什么又开一节：M51 比标准库函数、M54 比对象方法，**内建函数一条都没比**。
# 而「加一个内建要同时动 Python / Node / Rust 三处」（详见 `AGENTS.md` §1），
# 漏一处的症状是「在某个引擎上「没有定义过这个名字」」—— 最该被自动抓住的事。
#
# M55 建起检测后**当场抓到两笔**（都在 Node 与 Rust 上同时缺）：
#   - `断言(条件, 消息?)`   —— M23.1 就有了，宿主一直没跟上；
#   - `是实例(值, 类)`      —— 同上。
# 顺带还带出「`断言错误` 这个**捕获类型**在宿主上也不存在」。
# 三笔都当场补掉了（Rust 的 `是实例` 还因此给 `Class` 加了祖先链 `base_ids`
# —— 那边建类时把基类方法表展开合并后就**把基类指针丢了**，没法沿链判断）。

#: 不参与内建比对的**排除项**。
#:
#: ⚠️ M55 建这个检测时，这里曾经是 `frozenset({"超"})`，理由是「`超` 是编译器
#: 形式而不是可当值的内建，宿主不需要有它」。**那个判断是错的**，而且代价很实在：
#: 编译器把 `超().方法(x)` 脱糖成 `超(自身, "定义类名")`，字节码里就是
#: `LOAD_GLOBAL 超` + `CALL` —— 它就是个**运行期内建**。于是两个跨语言宿主
#: **一直没注册它**，`超()` 在 Node 与 Rust 上报「找不到名字「超」」，
#: 而三个 Python 执行器一切正常。
#:
#: 是 `tools/probe_builtins.py`（M56 的「内建体检」）把它抓出来的 ——
#: **教训：豁免必须能被复核，否则它就是藏 bug 的地方。**
#: M56 已给两宿主补上 `超`（Rust 还为它给 `Class` 加了 `base` 字段），
#: 所以现在没有需要排除的项，但**保留这个常量**：以后真要豁免谁，
#: 必须在这里写清理由。
BUILTIN_EXEMPT: frozenset[str] = frozenset()

#: 内建的**已知缺口**（M55 建检测时抓到两笔，都已补齐，所以是空的）。
#: 与 `KNOWN_GAPS` / `METHOD_KNOWN_GAPS` 同样是**带断言的显式账**。
BUILTIN_KNOWN_GAPS: dict[str, set[str]] = {"Node": set(), "Rust": set()}


def _py_builtins() -> set[str]:
    """Python 侧的事实源：`--lang-spec` 的 `builtins` 节。"""
    return {b["name"] for b in ai.build_lang_spec()["builtins"]} - BUILTIN_EXEMPT


def _node_builtins() -> set[str]:
    if not NODE:
        pytest.skip("没有 node")
    r = subprocess.run([NODE, str(ROOT / "node" / "index.js"), "--dump-builtins"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"Node --dump-builtins 失败：{r.stderr[:300]}"
    return set(json.loads(r.stdout))


def _rust_builtins() -> set[str]:
    if not RUST.exists():
        pytest.skip(f"没有 Rust 宿主：{RUST}（先 cargo build --release）")
    r = subprocess.run([str(RUST), "--dump-builtins"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"Rust --dump-builtins 失败：{r.stderr[:300]}"
    return set(json.loads(r.stdout))


def test_Node_覆盖_python_侧内建():
    expected, actual = _py_builtins(), _node_builtins()
    assert len(actual) > 20, "Node 报了个可疑的内建清单？"
    assert (expected - actual) == BUILTIN_KNOWN_GAPS["Node"], (
        "Node 缺这些内建：" + "、".join(sorted(expected - actual)))
    assert (actual - expected) == set(), (
        "Python 侧没有、Node 却有的内建：" + "、".join(sorted(actual - expected)))


def test_Rust_覆盖_python_侧内建():
    expected, actual = _py_builtins(), _rust_builtins()
    assert len(actual) > 20, "Rust 报了个可疑的内建清单？"
    assert (expected - actual) == BUILTIN_KNOWN_GAPS["Rust"], (
        "Rust 缺这些内建：" + "、".join(sorted(expected - actual)))
    assert (actual - expected) == set(), (
        "Python 侧没有、Rust 却有的内建：" + "、".join(sorted(actual - expected)))


def test_内建两个宿主彼此也不漂移():
    nd, rs = _node_builtins(), _rust_builtins()
    assert nd == rs, ("两宿主的内建清单不同 —— Node 有而 Rust 没有："
                      + "、".join(sorted(nd - rs))
                      + "；Rust 有而 Node 没有：" + "、".join(sorted(rs - nd)))


def test_M55_补的内建五引擎都能用():
    """`断言` / `是实例` 在**五个引擎**上都真能用（不是只在清单里）。

    这三笔（两个内建 + `断言错误` 捕获类型）就是内建漂移检测当场抓到的：
    M55 之前，Node 与 Rust 上 `断言(...)` 直接报「找不到名字「断言」」。
    """
    src = ('类 动物：\n'
           '    函数 初始化(自身, 名)：\n'
           '        自身.名 = 名\n'
           '类 猫 继承 动物：\n'
           '    函数 叫(自身)：\n'
           '        返回 "喵"\n'
           '令 c = 猫("咪")\n'
           '打印(是实例(c, 猫), 是实例(c, 动物), 是实例(1, 猫))\n'
           '断言(真)\n'
           '打印("过")\n')
    base_label, base = None, None
    for label, run in _engines():
        ok, out = run(src)
        assert ok, f"{label} 跑不通：{out[:200]}"
        if base is None:
            base_label, base = label, out
        else:
            assert out == base, f"{label} 与 {base_label} 不一致：{out[:120]!r}"
    assert base == "真 真 假\n过\n"


def test_捕获断言错误在五引擎都能接住():
    """`捕获 断言错误` 也得能用 —— M55 之前**宿主的异常类型表里没这一项**。

    ⚠️ **断言分两档（如实**）：
    - **三个执行器**（Python 侧）：必须接住、且拿到的异常**带消息**
      （`错误 E2008：断言没通过（故意失败）`）；
    - **两个跨语言宿主**：只断言「接住了、类型是 `断言错误`」。它们的**异常对象
      模型**都不完整 —— Node 打出来是 `Error: 故意失败`（没有错误码与标题），
      **Rust 更弱：`捕获 为 e` 拿到的只是异常**类型**，消息直接丢了**
      （`打印(e)` 给「断言错误」）。这条属 M54 记过的「宿主没有错误码/标题
      目录」那条边界，**已记进 M56 的清单**（`docs/路线图.md` §九）。
    """
    src = ('尝试：\n'
           '    断言(假, "故意失败")\n'
           '捕获 断言错误 为 e：\n'
           '    打印("接住", e)\n')
    for label, run in _engines():
        ok, out = run(src)
        assert ok, f"{label} 没接住断言错误：{out[:200]}"
        assert "接住" in out, f"{label} 没接住：{out[:160]!r}"
        if label in ("树遍历", "Python VM", "C VM"):
            # Python 侧渲染成「错误 E2008：断言没通过（故意失败）」——
            # 打的是**标题**而不是类型名，所以这里查消息
            assert "故意失败" in out, (
                f"{label} 接住了但丢了消息（Python 侧不该丢）：{out[:160]!r}")
        elif label == "Node":
            # 宿主没有错误码与标题，渲染成「错误（断言错误）：故意失败」
            assert "断言错误" in out and "故意失败" in out, f"Node：{out[:160]!r}"
        else:
            # ⚠️ **Rust 的已知缺口**：`捕获 为 e` 拿到的只是异常**类型**，
            # 消息在 `Val::ExcType` 这一步就丢了（`打印(e)` 给「断言错误」）。
            # 属宿主异常对象模型的缺口，已记进 M56（见 docstring）。
            assert "断言错误" in out, f"Rust 类型不对：{out[:160]!r}"


# ---------------------------------------------------------------------------
# 一之二、**对象方法**的漂移检测（M54 新建）
# ---------------------------------------------------------------------------
#
# 为什么单开一节：M51 的漂移检测只比**标准库的模块与函数名**，而
# `列表` / `字典` / `文本` / `集合` / `文件` / `小数` 的**方法表同样是五份实现**，
# 却一条都没测。代价是两笔漏网：
#   - **Rust 缺 `文本.查找`** —— M53 靠五引擎对拍偶然撞见；
#   - **Node 缺 `文件.位置/定位/刷新`** —— M54 建检测时才发现（Python 侧 9 个，
#     Node 只有 6 个，静默缺了三个）。
# 两笔都是「宿主少个方法」这种最该被自动抓住的事。现在两边都补齐了。

def _py_methods() -> dict[str, list[str]]:
    """Python 侧的事实源：`--lang-spec` 的 `methods` 节（M54 起是 6 类）。"""
    return {t: sorted(ms) for t, ms in ai.build_lang_spec()["methods"].items()}


def _node_methods() -> dict[str, list[str]]:
    if not NODE:
        pytest.skip("没有 node")
    r = subprocess.run([NODE, str(ROOT / "node" / "index.js"), "--dump-methods"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"Node --dump-methods 失败：{r.stderr[:300]}"
    return json.loads(r.stdout)


def _rust_methods() -> dict[str, list[str]]:
    if not RUST.exists():
        pytest.skip(f"没有 Rust 宿主：{RUST}（先 cargo build --release）")
    r = subprocess.run([str(RUST), "--dump-methods"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"Rust --dump-methods 失败：{r.stderr[:300]}"
    return json.loads(r.stdout)


#: 对象方法的**已知缺口**。M54 建这套检测时抓到的两条**当场就补了**，
#: 所以这里是空的 —— 但结构留着：缺口在就写出来（**带断言**，修好会报红提醒删），
#: 绝不允许「宿主悄悄少一个方法」。
#:
#: ⚠️ 这里只管**方法名**。已知的**语义**差异（不是缺口、是还没统一的账）另记：
#: `文件.位置` 在**非 ASCII** 上三方不同 —— Python 给的是它 `tell()` 的
#: 字节式 cookie（Windows 上还叠加了 `\n`→`\r\n` 的翻译），Node 给**字符数**、
#: Rust 给**字节偏移**。ASCII 下三方一致（`test_M54_补的方法真的能跑` 钉的是这个），
#: 要彻底统一得把 `位置/定位` 的口径定成「字符位置」并改 Python 与 Rust 两侧 ——
#: 属单独一笔账，**不塞进 M54**（详见 `docs/路线图.md` §九）。
METHOD_KNOWN_GAPS: dict[str, set[str]] = {"Node": set(), "Rust": set()}


def test_Node_覆盖_python_侧对象方法():
    expected, actual = _py_methods(), _node_methods()
    assert actual, "Node 报了个空方法表？"
    assert _missing(expected, actual) == METHOD_KNOWN_GAPS["Node"], (
        "Node 与 Python 侧的**对象方法**有漂移：\n" + _report("Node", expected, actual))
    assert _extra(expected, actual) == set(), (
        "Python 侧没有、Node 却有的方法：\n" + _report("Node", expected, actual))


def test_Rust_覆盖_python_侧对象方法():
    expected, actual = _py_methods(), _rust_methods()
    assert actual, "Rust 报了个空方法表？"
    assert _missing(expected, actual) == METHOD_KNOWN_GAPS["Rust"], (
        "Rust 与 Python 侧的**对象方法**有漂移：\n" + _report("Rust", expected, actual))
    assert _extra(expected, actual) == set(), (
        "Python 侧没有、Rust 却有的方法：\n" + _report("Rust", expected, actual))


def test_对象方法两个宿主彼此也不漂移():
    """与 `test_两个宿主彼此也不漂移` 同一个道理：别只有一边被补上。"""
    nd, rs = _node_methods(), _rust_methods()
    assert _extra(nd, rs) == set(), _report("Rust 比 Node 多", nd, rs)
    assert _extra(rs, nd) == set(), _report("Node 比 Rust 多", rs, nd)


def test_方法表本身不是空的():
    """给检测上保险：只有「6 张表、每张都非空」时，上面几条才是在说事。

    没有这条，`_py_methods()` 万一返回 `{}`（比如规格的字段改了名），
    上面那些断言会**全绿**——那才是真正的危险。
    """
    expected = _py_methods()
    assert set(expected) == {"列表", "字典", "文本", "集合", "文件", "小数"}, (
        f"Python 侧的方法表类目变了：{sorted(expected)}——"
        "改了就得同步 `jishi/ai.py`、两个宿主的 dump、以及这里的断言")
    for t, ms in expected.items():
        assert ms, f"「{t}」的方法表是空的？"


def test_M54_补的方法真的能跑(tmp_path):
    """**表里有名字 ≠ 真的能用**。

    上一条只比名字；这条把 M54 补/修的那几个方法真跑一遍，五个引擎逐字节一致 ——
    免得出现「名字报上去了、调用还是崩」这种自欺。
    只碰 M54 动过的那几个：`文本.查找`（Rust 补）、`文件.刷新/位置/定位`（Node 补）、
    `小数.舍入/绝对值/转文本`（三侧都在）。
    """
    p = str(tmp_path / "m54.txt").replace("\\", "/")
    src = ('打印("你好吗".查找("吗"), "abc".查找("z"))\n'
           '令 d = 精确("1.2345")\n'
           '打印(d.舍入(2), d.绝对值(), d.转文本())\n'
           f'用 打开("{p}", "写") 为 f：\n'
           '    f.写("abcdef")\n'
           '    f.刷新()\n'
           f'用 打开("{p}") 为 f：\n'
           '    f.读(2)\n'
           '    打印(f.位置())\n'
           '    f.定位(0)\n'
           '    打印(f.读(1))\n')
    base_label, base = None, None
    for label, run in _engines():
        ok, out = run(src)
        assert ok, f"{label} 跑不通 M54 补的方法：{out[:250]}"
        if base is None:
            base_label, base = label, out
        else:
            assert out == base, (
                f"{label} 与 {base_label} 输出不一致\n"
                f"{base_label}: {base!r}\n{label}: {out!r}")
    assert base == "2 -1\n1.23 1.2345 1.2345\n2\na\n"


# ---------------------------------------------------------------------------
# 二、命名参数（kwargs）五个引擎一致
# ---------------------------------------------------------------------------

def _capture(fn, *a) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*a)
    return buf.getvalue()


def _py_vm_run(src: str) -> str:
    return _capture(lambda: vm_mod.VM(compile_source(src, "<t>"), "<t>").run())


def _cvm_run(src: str) -> str:
    from jishi import cvm_bind
    return _capture(cvm_bind.run_source_c, src, "<t>")


def _host_run(exe: list[str], src: str, tag: str) -> tuple[bool, str]:
    """跑一个跨语言宿主。返回 `(是否正常退出, 输出+报错)`。"""
    bc = tmp_bc_path(ROOT / f"_tmp_m51_{tag}.json")
    bc.write_text(serialize.dumps(compile_source(src, "<t>")), encoding="utf-8")
    r = subprocess.run(exe + [str(bc)], capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def _engines():
    """五引擎的统一视图：`(名字, 跑一段源码 → (是否正常退出, 输出或错误文本))`。"""
    def tree(src):
        try:
            return True, _capture(tree_run, src)
        except JishiError as e:
            return False, f"{getattr(e, 'title', '')} {e}"

    def pyvm(src):
        try:
            return True, _py_vm_run(src)
        except JishiError as e:
            return False, f"{getattr(e, 'title', '')} {e}"

    def cvm(src):
        try:
            return True, _cvm_run(src)
        except JishiError as e:
            return False, f"{getattr(e, 'title', '')} {e}"

    out = [("树遍历", tree), ("Python VM", pyvm), ("C VM", cvm)]
    if NODE:
        out.append(("Node", lambda s: _host_run(
            [NODE, str(ROOT / "node" / "index.js")], s, "node")))
    if RUST.exists():
        out.append(("Rust", lambda s: _host_run([str(RUST)], s, "rust")))
    return out


def _five_agree(src: str) -> str:
    """五个引擎的 stdout **逐字节一致**，且都正常退出。返回那份输出。"""
    base_label, base = None, None
    for label, run in _engines():
        ok, out = run(src)
        assert ok, f"{label} 本该正常退出却报错：{out[:300]}"
        if base is None:
            base_label, base = label, out
        else:
            assert out == base, (
                f"{label} 与 {base_label} 输出不一致\n"
                f"{base_label}: {base!r}\n{label}: {out!r}")
    return base


#: `abc` 的公开测试向量（md5 / sha256 各一份）
MD5_ABC = "900150983cd24fb0d6963f7d28e17f72"
SHA256_ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_命名参数在五引擎给同一结果():
    """**这条就是 M51 要治的病**。

    以前宿主把 kwargs 静默丢掉，`算法="md5"` 会悄悄用默认的 sha256 ——
    所以这里只要断言结果是 md5（不是 sha256），就等于钉住了「不再静默」。
    """
    out = _five_agree('导入 加密\n打印(加密.摘要("abc", 算法="md5"))\n')
    assert out.strip() == MD5_ABC, f"命名参数没生效？拿到了 {out.strip()!r}"


def test_命名参数与位置参数等价():
    """`算法="md5"` 与把 md5 摆在位置上，五个引擎都得给同一结果。"""
    kw = _five_agree('导入 加密\n打印(加密.摘要("abc", 算法="md5"))\n')
    pos = _five_agree('导入 加密\n打印(加密.md5("abc"))\n')
    assert kw == pos


def test_命名参数只给可选参数时用宿主默认值():
    """不传 `算法` 就走默认值 —— 尾部空缺要截掉，不能补成 `空`。"""
    out = _five_agree('导入 加密\n打印(加密.摘要("abc"))\n')
    assert out.strip() == SHA256_ABC


def test_命名参数可用于多个模块():
    _five_agree(
        '导入 文本\n'
        '打印(文本.截断("这是一段很长的话", 宽度=6))\n'
        '打印(文本.填充("abc", 宽度=7, 填充字符="-"))\n'
        '导入 统计\n'
        '打印(统计.方差([1,2,3,4], 样本=真))\n'
        '导入 编码\n'
        '打印(编码.十六进制编码("abc", 大写=真))\n')


@pytest.mark.parametrize("src,why", [
    ('导入 加密\n加密.摘要("abc", 错名="md5")\n', "参数名对不上"),
    ('导入 加密\n加密.摘要(算法="md5")\n', "缺必填参数"),
    ('导入 文本\n文本.居中("a", 填充="-")\n', "命名参数跳过了前面的可选参数"),
])
def test_命名参数出错时五引擎都得响亮(src, why):
    """不能静默。

    以前宿主对 kwargs 的态度是「收下、忘掉」：调用看着成功了，参数根本没生效。
    现在要么五个引擎都给同一结果，要么**都以「类型错误」报出来**
    （异常类型与 Python 侧一致 —— 对拍才比得上）。
    """
    for label, run in _engines():
        ok, text = run(src)
        assert not ok, f"{label}（{why}）本该报错却正常退出：{text[:200]!r}"
        assert "类型错误" in text, (
            f"{label}（{why}）报的不是「类型错误」：{text[:200]!r}")


def test_不支持命名参数的目标也要响亮():
    """宿主独有内建（签名表里没有）收到 kwargs，同样不能静默丢掉。"""
    src = '打印(长度([1,2,3], 随便=1))\n'
    for label, run in _engines():
        ok, text = run(src)
        assert not ok, f"{label} 本该报错却正常退出：{text[:200]!r}"
        assert "类型错误" in text, f"{label} 报的不是「类型错误」：{text[:200]!r}"


# ---------------------------------------------------------------------------
# 四、M56 的「内建体检」抓到的：`超()` 两个宿主根本没有
# ---------------------------------------------------------------------------

def _超用例_不报错() -> str:
    """多级继承 + `超()` 调 `初始化`：三处 `超()` 各查一层，答案必须一致。"""
    return (
        "类 甲：\n"
        "    函数 初始化(自身, 名)：\n"
        "        自身.名 = 名\n"
        "    函数 说(自身)：\n"
        '        返回 "甲"\n'
        "类 乙 继承 甲：\n"
        "    函数 说(自身)：\n"
        '        返回 超().说() + "乙"\n'
        "类 丙 继承 乙：\n"
        "    函数 初始化(自身, 名)：\n"
        "        超().初始化(名)\n"
        "    函数 说(自身)：\n"
        '        返回 超().说() + "丙"\n'
        '令 p = 丙("小明")\n'
        "打印(p.说(), p.名)\n"
    )


def test_M56_超在五引擎都能用():
    """⚠️ **`超()` 在 M56 之前，两个跨语言宿主上根本不存在。**

    这是 M55 留下的一个**判断错误**：当时建「内建漂移检测」，
    把 `超` 当成「编译器形式、宿主不需要有它」而从比对里排除了
    （旧 `BUILTIN_EXEMPT = {"超"}`）。可字节码里它就是
    `LOAD_GLOBAL 超` + `CALL` —— 编译器把 `超().方法(x)` 脱糖成
    `超(自身, "定义类名")`，**是个货真价实的运行期内建**。

    于是 Node / Rust 一直报「找不到名字「超」」，三个 Python 执行器却全对；
    而**五引擎对拍覆盖不到它**（因为被显式排除了）。

    由 `tools/probe_builtins.py`（内建体检）抓出来。修法：两宿主补 `超`
    （Rust 还为它给 `Class` 补了 `base` 字段 —— 那边建类时把基类展开合并后
    **把基类指针丢了**）。教训写在 `BUILTIN_EXEMPT` 的注释里。

    这条按**真实用法**跑：多级继承（`丙 → 乙 → 甲`）+ `超().初始化(...)` 链。
    """
    src = _超用例_不报错()
    base_label, base = None, None
    for label, run in _engines():
        ok, out = run(src)
        assert ok, f"{label} 用不了「超()」：{out[:250]}"
        if base is None:
            base_label, base = label, out
        else:
            assert out == base, (
                f"{label} 与 {base_label} 输出不一致\n"
                f"{base_label}: {base!r}\n{label}: {out!r}")
    assert base == "甲乙丙 小明\n", f"多级继承的结果不对：{base!r}"


def test_M56_超出错时五引擎都报类型错误():
    """`超()` 用在**没有基类**的类里 → 三执行器给 `E2002 类型错误`，
    两宿主首行彼此一致（宿主没有错误码/标题目录，是 M54 记过的边界）。"""
    src = (
        "类 孤儿：\n"
        "    函数 试(自身)：\n"
        "        返回 超().说()\n"
        "孤儿().试()\n"
    )
    outs: dict[str, str] = {}
    for label, run in _engines():
        ok, out = run(src)
        assert not ok, f"{label} 本该报错却跑通了：{out[:200]!r}"
        outs[label] = out
    for label in ("树遍历", "Python VM", "C VM"):
        assert "E2002" in outs[label] and "没有基类" in outs[label], (
            f"{label} 报的不是 E2002「没有基类」：{outs[label][:200]!r}")
    hosts = {k: v for k, v in outs.items() if k in ("Node", "Rust")}
    firsts = {k: v.strip().splitlines()[0] for k, v in hosts.items()}
    if len(firsts) == 2:
        assert firsts["Node"] == firsts["Rust"], (
            f"两宿主报错首行不同\nNode: {firsts['Node']}\nRust: {firsts['Rust']}")
    assert all("没有基类" in t for t in firsts.values()), (
        f"宿主报错里没提「没有基类」：{firsts}")


# ---------------------------------------------------------------------------
# 四、M56 的「内建体检」抓到的：`超()` 两个宿主根本没有
# ---------------------------------------------------------------------------

def _超用例_不报错() -> str:
    """多级继承 + `超()` 调 `初始化`：三处 `超()` 各查一层，答案必须一致。"""
    return (
        "类 甲：\n"
        "    函数 初始化(自身, 名)：\n"
        "        自身.名 = 名\n"
        "    函数 说(自身)：\n"
        '        返回 "甲"\n'
        "类 乙 继承 甲：\n"
        "    函数 说(自身)：\n"
        '        返回 超().说() + "乙"\n'
        "类 丙 继承 乙：\n"
        "    函数 初始化(自身, 名)：\n"
        "        超().初始化(名)\n"
        "    函数 说(自身)：\n"
        '        返回 超().说() + "丙"\n'
        '令 p = 丙("小明")\n'
        "打印(p.说(), p.名)\n"
    )


def test_M56_超在五引擎都能用():
    """⚠️ **`超()` 在 M56 之前，两个跨语言宿主上根本不存在。**

    这是 M55 留下的一个**判断错误**：当时建「内建漂移检测」，
    把 `超` 当成「编译器形式、宿主不需要有它」而从比对里排除了
    （旧 `BUILTIN_EXEMPT = {"超"}`）。可字节码里它就是
    `LOAD_GLOBAL 超` + `CALL` —— 编译器把 `超().方法(x)` 脱糖成
    `超(自身, "定义类名")`，**是个货真价实的运行期内建**。

    于是 Node / Rust 一直报「找不到名字「超」」，三个 Python 执行器却全对；
    而**五引擎对拍覆盖不到它**（因为被显式排除了）。

    由 `tools/probe_builtins.py`（内建体检）抓出来。修法：两宿主补 `超`
    （Rust 还为它给 `Class` 补了 `base` 字段 —— 那边建类时把基类展开合并后
    **把基类指针丢了**）。教训写在 `BUILTIN_EXEMPT` 的注释里。

    这条按**真实用法**跑：多级继承（`丙 → 乙 → 甲`）+ `超().初始化(...)` 链。
    """
    src = _超用例_不报错()
    base_label, base = None, None
    for label, run in _engines():
        ok, out = run(src)
        assert ok, f"{label} 用不了「超()」：{out[:250]}"
        if base is None:
            base_label, base = label, out
        else:
            assert out == base, (
                f"{label} 与 {base_label} 输出不一致\n"
                f"{base_label}: {base!r}\n{label}: {out!r}")
    assert base == "甲乙丙 小明\n", f"多级继承的结果不对：{base!r}"


def test_M56_超出错时五引擎都报类型错误():
    """`超()` 用在**没有基类**的类里 → 三执行器给 `E2002 类型错误`，
    两宿主首行彼此一致（宿主没有错误码/标题目录，是 M54 记过的边界）。"""
    src = (
        "类 孤儿：\n"
        "    函数 试(自身)：\n"
        "        返回 超().说()\n"
        "孤儿().试()\n"
    )
    outs: dict[str, str] = {}
    for label, run in _engines():
        ok, out = run(src)
        assert not ok, f"{label} 本该报错却跑通了：{out[:200]!r}"
        outs[label] = out
    for label in ("树遍历", "Python VM", "C VM"):
        assert "E2002" in outs[label] and "没有基类" in outs[label], (
            f"{label} 报的不是 E2002「没有基类」：{outs[label][:200]!r}")
    hosts = {k: v for k, v in outs.items() if k in ("Node", "Rust")}
    firsts = {k: v.strip().splitlines()[0] for k, v in hosts.items()}
    if len(firsts) == 2:
        assert firsts["Node"] == firsts["Rust"], (
            f"两宿主报错首行不同\nNode: {firsts['Node']}\nRust: {firsts['Rust']}")
    assert all("没有基类" in t for t in firsts.values()), (
        f"宿主报错里没提「没有基类」：{firsts}")
