# -*- coding: utf-8 -*-
"""M148：异常链 —— `文件读不了 → 解析失败 → 业务报错` 三层能串起来看。

## 这一版在解决什么

真实程序的错误几乎都是**一层包一层**：底层是「文件没有」，中间那层换成
「配置读不了」，最外层换成「启动失败」。M148 之前，只要中间任何一层写了
`捕获 … 为 e: 抛出 值错误("…")`，底层那个错就**彻底消失**了 —— 用户只看到
业务层那句话，不知道根子是什么、在哪一行。

现在：**正在处理某个异常时又抛出的新异常，自动把它挂成「起因」**，渲染时
逐层摊开（`起因：` 分隔），程序里也能读（`e.原因` / `e.根因`）。

## 语义（三宿主 + 树遍历必须一致）

`抛出` 的那一刻，如果执行点落在某个「捕获区」里（`捕获` 体或同一条 `尝试`
的 `最终` 块，含**捕获体里调用的函数**），就把正在被处理的那个异常挂成起因。
两条例外：

1. `抛出 e` 而 e 就是正在被处理的那个（原样重抛）→ **不挂**（否则自环）；
2. 异常自己已经带着起因（重抛一个带链的异常）→ 保留原来的，别把中间层盖掉。

## 判据分两层

* **行为层**：同一段程序在 Python 字节码 VM / 树遍历 / Rust 宿主 / Node 宿主
  上输出**逐字一致**（含 `起因：` 链、位置、深度上限）；
* **结构层**：编译器把「捕获区右边界」写进 `SETUP_TRY` 的 c —— 删了它链会
  **静默消失**（不报错、只是没了），所以必须有源码级判据钉住。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT, rust_exe_path


# ---------------------------------------------------------------------------
# 跑法：四个执行器各跑一遍同一段源码
# ---------------------------------------------------------------------------

def _写程序(src: str, tmp_path: Path) -> Path:
    """把源码写成临时工程里的 `程序.jsh`（四个执行器都用它）。

    ⚠️ **必须落成真文件**：报错渲染里的「源码行 + `^`」是出口按 `filename`
    现读的，`<t>` 这种假文件名读不到源码 ⇒ 少两行，四个执行器就没法逐字比。
    """
    p = tmp_path / "程序.jsh"
    p.write_text(src, encoding="utf-8", newline="\n")
    return p


def _跑_python(src: str, *, tree: bool, tmp_path: Path) -> str:
    """Python 两个执行器：走**真正的 CLI**（`--tree` 是树遍历）。

    为什么不用进程内调用：进程内跑拿不到「报错时补源码行」那一步
    （那是 CLI 出口做的），渲染会比宿主少两行 —— 比的就不是同一件事了。
    """
    import os

    _写程序(src, tmp_path)
    命令 = [sys.executable, "-m", "oracle.jishi.cli"]
    if tree:
        命令.append("--tree")
    命令.append("程序.jsh")
    r = subprocess.run(命令, cwd=str(tmp_path), capture_output=True,
                       text=True, encoding="utf-8",
                       env={**os.environ, "PYTHONPATH": str(ROOT)})
    return (r.stdout or "") + (r.stderr or "")


def _跑_rust(src: str, tmp_path: Path) -> str:
    """Rust 宿主：**不依赖 Python**（写 .jsh 给它自己跑）。"""
    rs = rust_exe_path()
    if not rs.is_file():
        pytest.skip("没有编出 jishi-rs")
    _写程序(src, tmp_path)
    r = subprocess.run([str(rs), "程序.jsh"], cwd=str(tmp_path),
                       capture_output=True, text=True, encoding="utf-8")
    return (r.stdout or "") + (r.stderr or "")


def _跑_node(src: str, tmp_path: Path) -> str:
    """Node 宿主：吃**字节码 JSON**（与 Python 同一份编译产物）。"""
    from shutil import which

    node = which("node")
    if node is None:
        pytest.skip("环境里没有 node")
    from oracle.jishi import serialize
    from oracle.jishi.compiler import compile_source

    bc = tmp_path / "程序.json"
    bc.write_text(serialize.dumps(compile_source(src, "程序.jsh")), encoding="utf-8")
    r = subprocess.run([node, str(ROOT / "oracle" / "node" / "index.js"), bc.name],
                       cwd=str(tmp_path), capture_output=True, text=True,
                       encoding="utf-8")
    return (r.stdout or "") + (r.stderr or "")


def _跑_cvm(src: str, tmp_path: Path) -> str:
    """C 虚拟机（`cvm_bind.run_source_c`）：进程内跑，出口按 CLI 的做法补源码行。

    ⚠️ 补源码行那两步（`with_source` + `render`）是 CLI 出口干的，这里照着做一遍
    —— 不补的话它比别的执行器少两行，比的就不是同一件事了。
    """
    import contextlib
    import io
    import os

    from oracle.jishi import cvm_bind, errors as E

    路径 = _写程序(src, tmp_path)
    buf = io.StringIO()
    旧 = os.getcwd()
    os.chdir(str(tmp_path))
    try:
        with contextlib.redirect_stdout(buf):
            try:
                cvm_bind.run_source_c(src, filename="程序.jsh")
            except E.JishiError as e:
                e.with_source(src.replace("\r\n", "\n").split("\n"))
                buf.write(e.render() + "\n")
    finally:
        os.chdir(旧)
    return buf.getvalue()


def _五执行器(src: str, tmp_path: Path) -> dict:
    """五个执行器各跑一遍 —— 返回 {名字: 输出}。"""
    return {
        "树遍历": _跑_python(src, tree=True, tmp_path=tmp_path),
        "字节码 VM": _跑_python(src, tree=False, tmp_path=tmp_path),
        "C VM": _跑_cvm(src, tmp_path),
        "Rust 宿主": _跑_rust(src, tmp_path),
        "Node 宿主": _跑_node(src, tmp_path),
    }


#: 三层包装的真实形状：读文件 → 解析 → 业务。每层都换一次说法。
三层程序 = '''导入 文件
导入 json

函数 读文本(路径)：
    返回 文件.读文本(路径)

函数 解析(内容)：
    尝试：
        返回 json.解析(内容)
    捕获 值错误 为 e：
        抛出 值错误("格式不对")

函数 加载(路径)：
    尝试：
        返回 解析(读文本(路径))
    捕获 异常 为 e：
        抛出 值错误("加载配置失败")

函数 启动()：
    尝试：
        加载("没有这个文件.json")
    捕获 异常 为 e：
        抛出 值错误("启动失败")

启动()
'''


# ---------------------------------------------------------------------------
# 一、四个执行器逐字一致
# ---------------------------------------------------------------------------

def test_三层链四执行器逐字一致(tmp_path):
    """**核心判据**：同一个未捕获的三层错误，四个执行器渲染完全一样。

    比的是「整段文本」—— 标题、`┌─ 文件:行:列`、源码行、`^`、`起因：` 链、
    链上每一层的位置，全都要一样。任何一处只在某个宿主上不同，这条就红。
    """
    出 = _五执行器(三层程序, tmp_path)

    基准 = 出["树遍历"]
    # 链确实摊开了：三层都在（启动 → 加载 → 文件）。
    # ⚠️ 这里是 **3** 层不是 4 层：文件根本不存在，`解析` 那层没机会跑
    #    —— 「配置内容格式不对」得等文件读得出来才会发生。
    assert 基准.count("起因：") == 2, 基准
    for 关键词 in ("启动失败", "加载配置失败", "找不到文件"):
        assert 关键词 in 基准, f"链里缺了「{关键词}」：\n{基准}"

    for 名, 文本 in 出.items():
        assert 文本 == 基准, f"{名} 与树遍历不一致：\n--- 树遍历 ---\n{基准}\n--- {名} ---\n{文本}"


def test_起因层只印位置不印调用链(tmp_path):
    """起因层**不印调用链**（只有最外层印）—— 三宿主同一个口径。

    为什么：四层链每层都带一遍「从外到内」，真正的信息就被淹了。
    这条钉的是「口径」，不是「少印了」：`调用链` 在整段输出里必须只出现一次。
    """
    出 = _五执行器(三层程序, tmp_path)
    for 名, 文本 in 出.items():
        assert 文本.count("调用链（从外到内）：") == 1, f"{名}：\n{文本}"


# ---------------------------------------------------------------------------
# 二、程序里读得到（`e.原因` / `e.根因`）
# ---------------------------------------------------------------------------

读链 = '''导入 文件

函数 里层()：
    尝试：
        返回 文件.读文本("没有这个文件.txt")
    捕获 文件错误 为 e：
        抛出 值错误("里层包装")

尝试：
    里层()
捕获 值错误 为 外层：
    打印("消息", 外层.消息)
    打印("原因", 外层.原因.消息)
    打印("根因", 外层.根因.消息)
    打印("根因类型", 外层.根因.类型)
    打印("根因没有上一层", 外层.根因.原因)
'''


def test_原因与根因可读(tmp_path):
    """`e.原因`（上一层）/ `e.根因`（链最底下）/ 「最底下没有上一层」= `空`。"""
    出 = _五执行器(读链, tmp_path)
    基准 = 出["树遍历"]
    assert "消息 里层包装" in 基准, 基准
    assert "原因 找不到文件" in 基准, 基准
    assert "根因 找不到文件" in 基准, 基准
    assert "根因类型 文件错误" in 基准, 基准
    assert "根因没有上一层 空" in 基准, 基准
    for 名, 文本 in 出.items():
        assert 文本 == 基准, f"{名}：\n{文本}"


def test_没有原因时是空():
    """没在任何 `捕获` 体里抛出 → `e.原因` 是 `空`（不是 `None`、不是报错）。"""
    from oracle.jishi import runtime as R

    e = R.make_exception(R.RunValueError("单独的错"))
    assert e.原因 is None
    assert e.根因 is e, "没有原因时 `根因` 该是自己"


# ---------------------------------------------------------------------------
# 三、两条例外：不挂自环 / 不盖掉已有的链
# ---------------------------------------------------------------------------

自环 = '''函数 转一圈()：
    尝试：
        抛出 值错误("原样")
    捕获 值错误 为 e：
        抛出 e

尝试：
    转一圈()
捕获 值错误 为 外：
    打印("消息", 外.消息)
    打印("原因", 外.原因)
'''


def test_原样重抛不挂成自己的原因(tmp_path):
    """`捕获 为 e: 抛出 e` —— 原因必须是 `空`，不能自环。

    Python 侧 `raise e` 会把自己挂成 `__context__`（打印时会检测自环不打），
    我们**在挂的时候就挡住**：链里出现「自己」是纯粹的噪音。
    """
    出 = _五执行器(自环, tmp_path)
    基准 = 出["树遍历"]
    assert "消息 原样" in 基准, 基准
    assert "原因 空" in 基准, 基准
    for 名, 文本 in 出.items():
        assert 文本 == 基准, f"{名}：\n{文本}"


两层重抛 = '''导入 文件

函数 甲()：
    尝试：
        返回 文件.读文本("没有.txt")
    捕获 文件错误 为 e：
        抛出 值错误("甲包装")

函数 乙()：
    尝试：
        甲()
    捕获 值错误 为 e：
        抛出 e          # 原样往上丢：链不能断，也不能多一层

尝试：
    乙()
捕获 值错误 为 外：
    打印("消息", 外.消息)
    打印("原因", 外.原因.消息)
    打印("原因的原因", 外.原因.原因)
'''


def test_重抛不盖掉已有的链(tmp_path):
    """中间层原样重抛：链保持「乙包装 → 找不到文件」两条，不多不少。"""
    出 = _五执行器(两层重抛, tmp_path)
    基准 = 出["树遍历"]
    assert "消息 甲包装" in 基准, 基准
    assert "原因 找不到文件" in 基准, 基准
    assert "原因的原因 空" in 基准, 基准
    for 名, 文本 in 出.items():
        assert 文本 == 基准, f"{名}：\n{文本}"


# ---------------------------------------------------------------------------
# 四、捕获区的边界（跨函数 / 最终块 / 出了语句就不算）
# ---------------------------------------------------------------------------

跨函数 = '''函数 里头抛()：
    抛出 值错误("里头的错")

尝试：
    尝试：
        抛出 值错误("外面的错")
    捕获 值错误 为 e：
        里头抛()          # 捕获体里调别的函数、那个函数抛 —— 起因仍要挂上
捕获 值错误 为 外：
    打印("消息", 外.消息)
    打印("原因", 外.原因.消息)
'''


def test_捕获体里调用的函数抛出也能挂上(tmp_path):
    """跨帧也要挂：调用者的 ip 停在**调用点**上，而调用点在捕获区里。"""
    出 = _五执行器(跨函数, tmp_path)
    基准 = 出["树遍历"]
    assert "原因 外面的错" in 基准, 基准
    for 名, 文本 in 出.items():
        assert 文本 == 基准, f"{名}：\n{文本}"


最终块抛 = '''尝试：
    抛出 值错误("原本的错")
捕获 值错误 为 e：
    打印("接住", e.消息)
最终：
    尝试：
        抛出 值错误("最终块里的错")
    捕获 值错误 为 e2：
        打印("最终块接住", e2.消息)
        打印("它的原因", e2.原因.消息)
'''


def test_最终块里的抛出也挂在同一条捕获区里(tmp_path):
    """捕获区右边界取的是**整条 `尝试` 结束**（含 `最终`），不是捕获体末尾。

    这条钉住编译器那个 `c` 的取值：如果取 `fin_start`（捕获体结束），
    `最终` 里的抛出就查不到原因了 —— 那正是「两处 finally 副本口径不一」的坑。
    """
    出 = _五执行器(最终块抛, tmp_path)
    基准 = 出["树遍历"]
    assert "接住 原本的错" in 基准, 基准
    assert "它的原因 原本的错" in 基准, 基准
    for 名, 文本 in 出.items():
        assert 文本 == 基准, f"{名}：\n{文本}"


出了语句 = '''尝试：
    抛出 值错误("被接住的")
捕获 值错误 为 e：
    打印("接住", e.消息)

尝试：
    抛出 值错误("后来的错")
捕获 值错误 为 后：
    打印("后来的原因", 后.原因)
'''


def test_出了尝试语句就不算正在处理(tmp_path):
    """捕获体跑完之后再抛的错，**不该**挂上一个早就处理完的异常。"""
    出 = _五执行器(出了语句, tmp_path)
    基准 = 出["树遍历"]
    assert "后来的原因 空" in 基准, 基准
    for 名, 文本 in 出.items():
        assert 文本 == 基准, f"{名}：\n{文本}"


循环 = '''令 轮 = 0
当 轮 < 3：
    尝试：
        抛出 值错误("第" + 文本(轮) + "次")
    捕获 值错误 为 e：
        打印(e.消息, "原因", e.原因)
    轮 = 轮 + 1
'''


def test_循环里反复捕获不串味(tmp_path):
    """同一个 `尝试` 在循环里跑很多遍：每次的原因都该是 `空`（不是上一轮的）。

    这条防的是「记下正在处理的异常」那张表**只压不剪** ⇒ 第一轮的原因会
    粘到第二轮以后（而且表会一直涨）。
    """
    出 = _五执行器(循环, tmp_path)
    基准 = 出["树遍历"]
    for i in range(3):
        assert f"第{i}次 原因 空" in 基准, 基准
    for 名, 文本 in 出.items():
        assert 文本 == 基准, f"{名}：\n{文本}"


# ---------------------------------------------------------------------------
# 五、结构层判据（删了就会静默失效的那几处）
# ---------------------------------------------------------------------------

def test_编译器把捕获区右边界写进_SETUP_TRY_c():
    """`SETUP_TRY` 的 c = 整条 `尝试` 结束的位置 —— **链全靠它**。

    ⚠️ 这个字段没有它，一切照跑：`抛出` 照样抛、捕获照样接，只是**起因链
    永远为空**（`_note_handled` 看到 `region_end <= 0` 就跳过）。这种
    「静默少个功能」正是本项目最需要判据的地方。
    """
    from oracle.jishi import opcodes as O
    from oracle.jishi.compiler import compile_source

    src = "尝试：\n    抛出 值错误(\"x\")\n捕获 值错误 为 e：\n    打印(e)\n"
    cmod = compile_source(src, "<t>")
    找到 = []
    for code in cmod.codes:
        for ins in code.instrs:
            if ins.op == O.Op.SETUP_TRY:
                找到.append(ins)
    assert 找到, "编译产物里没有 SETUP_TRY"
    for ins in 找到:
        assert ins.c > ins.a, (
            f"SETUP_TRY 的 c（捕获区右边界）没写对：a={ins.a} c={ins.c}")
        assert ins.c <= len(cmod.codes[0].instrs), "c 越界了（指向不存在的指令）"


def test_三宿主的上限是同一个数():
    """`MAX_CAUSE_DEPTH` 三个宿主必须一样 —— 不一样，同一个错的渲染长度就不同。"""
    import re

    py = (ROOT / "oracle" / "jishi" / "errors.py").read_text(encoding="utf-8")
    rs = (ROOT / "rust" / "src" / "lib.rs").read_text(encoding="utf-8")
    js = (ROOT / "oracle" / "node" / "runtime.js").read_text(encoding="utf-8")

    py_n = int(re.search(r"^MAX_CAUSE_DEPTH\s*=\s*(\d+)", py, re.M).group(1))
    rs_n = int(re.search(r"const MAX_CAUSE_DEPTH: usize = (\d+)", rs).group(1))
    js_n = int(re.search(r"const MAX_CAUSE_DEPTH = (\d+)", js).group(1))
    assert py_n == rs_n == js_n, f"Python {py_n} / Rust {rs_n} / Node {js_n}"


def test_深度上限真会截断():
    """链超过上限就只摊到上限，并明说「还更深」。

    ⚠️ 不截断的话，「捕获里包捕获」写十层就印十层 —— 刷屏且没人看。
    """
    from oracle.jishi import errors as E

    最底层 = E.RunValueError("第0层")
    当前 = 最底层
    for i in range(1, 9):           # 9 层，超过上限 5
        当前 = E.RunValueError(f"第{i}层", cause=当前)

    文本 = 当前.render()
    assert 文本.count("起因：\n") == E.MAX_CAUSE_DEPTH
    assert "还更深，只摊到这里" in 文本, 文本
