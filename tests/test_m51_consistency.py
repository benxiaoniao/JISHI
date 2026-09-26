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
