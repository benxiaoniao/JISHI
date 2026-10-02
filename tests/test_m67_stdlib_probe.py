# -*- coding: utf-8 -*-
"""R6.4 · 标准库体检（`tools/probe_stdlib.py`）+ 它抓出来的那几笔。

## 缘起

到 R6.4 为止，标准库只有**名字面**的检测（`--dump-stdlib` 报「函数在不在」），
行为面靠 `test_m31_node_stdlib.py` / `test_m33_rust_stdlib.py` 那批**手写用例**
—— 而那正是 R6.4 学到的教训：**手写用例覆盖到的地方才有人管**。

新探针把 141 条用例合成一个程序、五个执行器各跑一次、逐行比。第一次跑就抓到：

* `数学.四舍五入(2.5)` —— Node 给 `3`，其余四个给 `2`（**半值向上 vs 银行家舍入**）；
* `html.属性文本("a","b")` / `日期.今年(1)` —— **Node 静默接受多余参数**。

本文件钉三件事：

1. **探针本身**：跑一遍，要求「**没有未登记的不一致**」（登记会过期报红）；
2. **四舍五入**：Node 走银行家舍入（与 Python 的 `round` 一致）；
3. **参数个数错**：Python 侧标准库那条路**不再漏英文**（内建那条 M55 修过）。
"""

import io
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

NODE = shutil.which("node")
HAS_NODE = NODE is not None


def _py(src: str) -> tuple[bool, str]:
    """树遍历跑一遍 → `(是否跑通, stdout 或 报错全文)`。"""
    from jishi.interpreter import run_source as run_tree
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            run_tree(src, "标准库.jsh")
        return True, buf.getvalue()
    except BaseException as e:                                     # noqa: BLE001
        return False, str(e)


def _node(src: str) -> tuple[bool, str]:
    """Node 宿主跑一遍 → `(是否跑通, stdout + stderr)`。"""
    import tempfile

    from jishi.compiler import compile_to_json
    tmp = Path(tempfile.gettempdir()) / "_m67.json"
    tmp.write_text(compile_to_json(src, "标准库.jsh"), encoding="utf-8")
    r = subprocess.run([NODE, str(ROOT / "node/index.js"), str(tmp)],
                       capture_output=True, input=b"", cwd=str(ROOT))
    return (r.returncode == 0,
            r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# 一、探针本身：不许有「未登记的不一致」
# ---------------------------------------------------------------------------

def test_标准库体检没有未登记的不一致():
    """跑 `tools/probe_stdlib.py`，要求退出码 0。

    ⚠️ 退出码 1 有两种含义，都要管：**有未登记的不一致**（新问题），
    或**登记过期**（登记的那条已经好了，去删掉）—— 后者是本项目「豁免必须能复核」
    的老规矩：修好了不能静悄悄留着。
    """
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "probe_stdlib.py")],
                       capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"标准库体检没过：\n{out[-3000:]}"
    assert "不一致 0 条" in out, out[-1500:]


def test_探针覆盖了足够多的用例():
    """⚠️ 防回退：用例表被误删/清空时，上面那条会「轻松通过」。

    用例覆盖 24 个模块里的 23 个（`网络` 要外网、`测试` 是宿主豁免）；
    以后只应该**涨**，掉了就该有人解释。
    """
    from probe_stdlib import CASES
    assert len(CASES) >= 130, f"用例只剩 {len(CASES)} 条了"
    mods = {k.split(".")[0] for k in CASES}
    assert len(mods) >= 23, f"覆盖面只剩 {len(mods)} 个模块：{sorted(mods)}"


# ---------------------------------------------------------------------------
# 二、`数学.四舍五入`：银行家舍入（半值向偶数）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_NODE, reason="本机无 Node.js")
@pytest.mark.parametrize("src,want", [
    # 半值向偶数：2.5→2、3.5→4、-2.5→-2、-3.5→-4
    ("导入 数学\n打印(数学.四舍五入(2.5))", "2\n"),
    ("导入 数学\n打印(数学.四舍五入(3.5))", "4\n"),
    ("导入 数学\n打印(数学.四舍五入(-2.5))", "-2\n"),
    ("导入 数学\n打印(数学.四舍五入(-3.5))", "-4\n"),
    # 非半值照常四舍五入
    ("导入 数学\n打印(数学.四舍五入(2.6))", "3\n"),
    ("导入 数学\n打印(数学.四舍五入(2.4))", "2\n"),
    # 带位数
    ("导入 数学\n打印(数学.四舍五入(1.25, 1))", "1.2\n"),
    ("导入 数学\n打印(数学.四舍五入(1.35, 1))", "1.4\n"),
])
def test_四舍五入是银行家舍入(src, want):
    """Node 以前用 `Math.round(x * 10**n) / 10**n`（**半值向上**）——
    `四舍五入(2.5)` 给 `3`，而 Python 的 `round` 给 `2`。

    📌 二进制浮点下「先加 0.5 再取整」会**系统性偏大**，所以要专门走
    `roundHalfEven`（f-string 那边一直用的就是它）。
    """
    ok, out = _py(src)
    assert ok and out == want, f"Python 侧：{out!r}（期望 {want!r}）"
    ok, out = _node(src)
    assert ok and out == want, f"Node 侧：{out!r}（期望 {want!r}）"


# ---------------------------------------------------------------------------
# 三、参数个数错：不再漏英文、不再静默
# ---------------------------------------------------------------------------

def test_标准库参数个数错的文案是中文():
    """⚠️ 标准库函数是**真 Python 函数**，靠 Python 自己抛 `TypeError` ——
    于是这两句**一直漏成英文**（内建那条路 M55 已经走 `check_arity` 了）：

    * `属性文本() takes 1 positional argument but 2 were given`
    * `写表格() missing 1 required positional argument: '行列表'`

    现在翻译成中文，与 Rust 的 `need` / `need_range` 对齐（M55 那条文案口径）。
    """
    ok, out = _py('导入 html\nhtml.属性文本("a", "b")\n')
    assert not ok, "本该报错"
    assert "函数「属性文本」需要 1 个参数，但传了 2 个" in out, out[:200]
    assert "positional argument" not in out, f"还漏着英文：{out[:200]}"
    assert "takes" not in out, f"还漏着英文：{out[:200]}"

    ok, out = _py('导入 表格\n表格.写表格()\n')
    assert not ok, "本该报错"
    # ⚠️ 改成**主动检查**之后，「少给」也能说出「一共传了几个」
    # （以前靠 Python 的 `missing 1 required positional argument` 只说得出缺几个）
    assert "函数「写表格」需要 2 个参数，但传了 0 个" in out, out[:200]
    assert "missing" not in out and "required positional" not in out, out[:200]


def test_内建的参数个数错也还是中文():
    """⚠️ 这条是**守 M55 的旧账**：内建走 `_Builtin.check_arity`，
    文案是「「长度」需要 1 个参数，但传了 0 个」（**不带「函数」前缀**）。

    内建与标准库的称呼本来就不同（内建是语言的一部分，用户直接写 `长度(x)`；
    标准库要写 `模块.函数(...)`），**别为了「统一」把其中一边改坏**。
    """
    ok, out = _py("长度()\n")
    assert not ok
    assert "「长度」需要 1 个参数，但传了 0 个" in out, out[:200]


@pytest.mark.skipif(not HAS_NODE, reason="本机无 Node.js")
def test_Node的标准库也会检查参数个数了():
    """⚠️ 这条测试**上一版是反着写的** —— 它当时断言「缺口还在」。

    R6.4 建这个探针时，Node 的标准库不检查参数个数（`html.属性文本("a","b")`
    静默返回 `a`、`日期.今年(1)` 静默返回年份），于是登记在 `KNOWN_GAPS` 里。
    修好之后**探针自己报了「登记过期」**，就把这条测试翻了过来 ——
    这正是「登记必须能过期」的用法：修好了不会静悄悄留着。

    修法：签名表补记 `*参数`（`tools/gen_stdlib_sigs.py`）→
    `needStdlibArgs` 在**调用点**统一查（230 个函数逐个加检查既写不完也会漂）。
    """
    # 多给：以前静默忽略
    ok, out = _node('导入 html\n打印(html.属性文本("a", "b"))\n')
    assert not ok, f"本该报错却跑通了：{out!r}"
    assert "函数「属性文本」需要 1 个参数，但传了 2 个" in out, out[:200]

    # 少给：Node 的固定形参会拿到 `undefined` 一路算下去（`数学.开方()` 给 NaN）
    ok, out = _node('导入 数学\n打印(数学.开方())\n')
    assert not ok, f"本该报错却跑通了：{out!r}"
    assert "函数「开方」需要 1 个参数，但传了 0 个" in out, out[:200]


@pytest.mark.skipif(not HAS_NODE, reason="本机无 Node.js")
def test_变参函数不会被参数个数检查拦住():
    """⚠️ **这条是那次修复最容易踩的坑**：签名表里 `路径.连接` 一个参数名都
    没有（`连接(*部分)`），拿「参数名个数」当上限就会把**合法调用**拦下来
    —— 比不检查更糟。所以表里要记 `*参数`（`sig[2]`）。
    """
    import os
    for src, want in (
        # ⚠️ 分隔符是**平台相关**的（Windows `\`、其它 `/`）—— 别写死
        ('导入 路径\n打印(路径.连接("甲", "乙", "丙", "丁"))\n',
         os.sep.join(["甲", "乙", "丙", "丁"]) + "\n"),
        ('导入 压缩\n打印(压缩.打包)\n', None),      # 只要求「能取到属性、不报参数错」
    ):
        ok, out = _node(src)
        assert ok, f"变参调用被拦了：{out!r}"
        if want is not None:
            assert out.strip() == want.strip(), out


def test_命名参数调用不该被参数个数检查误拦():
    """⚠️ 这条来自一次**真实的假报** —— 新加的参数个数检查**只数了位置参数**：

    `文本.截断("这是一段很长的话", 宽度=6)` 只给了 1 个**位置**参数，
    但函数收的是 2 个（另一个是命名参数）→ 检查判成「只传了 1 个」→ 误报。
    被 `test_m51_consistency.py::test_命名参数可用于多个模块` 抓住。

    📌 教训：**新加的检查必须过一遍「命名参数」那条路** ——
    「位置参数个数」和「实参总数」在这里不是一回事。
    """
    src = ('导入 文本 为 T\n'
           '打印(T.截断("这是一段很长的话", 宽度=6))\n'
           '打印(T.填充("abc", 宽度=7, 填充字符="-"))\n')
    ok, out = _py(src)
    assert ok, f"Python 侧误报：{out[:200]}"
    want = out
    if HAS_NODE:
        ok, out = _node(src)
        assert ok, f"Node 侧误报：{out[:200]}"
        assert out == want, f"两侧不一致\npy={want!r}\nnd={out!r}"


def test_命名参数里keyword_only的不算进位置个数():
    """⚠️ **与上一条相对的另一种形状** —— 两次假报是**同一条检查**的两个方向：

    * `文本.截断("很长的话", 宽度=6)`：`宽度` 是 **positional-or-keyword**，
      按名字传**等价于**按位置传 ⇒ **要算进个数**；
    * `json.dumps(对象, 缩进=2)`：`缩进` 是 **keyword-only**，
      **落不到位置参数上** ⇒ **不能算**（算进去就成「传了 2 个、只要 1 个」，
      被 `test_bridge.py` 抓住）。

    判据是「这个命名参数的名字**在不在位置参数表里**」。
    ⚠️ 桥接（`从 python 导入`）两个宿主都不支持，所以这条只测 Python 侧。
    """
    src = ('导入 json 从 python\n'
           '打印(json.dumps({"甲": 1}, ensure_ascii=假))\n')
    ok, out = _py(src)
    assert ok, f"keyword-only 的命名参数被误算成位置参数了：{out[:200]}"
