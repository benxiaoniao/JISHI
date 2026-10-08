# -*- coding: utf-8 -*-
"""R7.4：CLI 主入口搬进 Rust（`jishi-rs <文件>` / `--stdin` / `--ast` /
`--json-errors` / `--sandbox` / `--load-bytecode` / `--dump-bytecode`）。

验收原文（`docs/路线图.md` §11.8 的 R7.4）：
① 补齐 `<文件>` / `--stdin` / `--json-errors` / `--ast` / `--sandbox`
（**超时真 kill**）。

本文件照 R7.1 起的规矩分两半：

* **对拍**：同一份输入喂 `jishi-rs` 与 `jishi`，比 stdout / stderr / 退出码。
* **独立判据**：不 import oracle.jishi、不 spawn Python —— 把「宿主自己该输出什么」
  **写死**。R8 之后 Python 退场，这一半仍要绿。

⚠️ **位置参数的语义在 R7.4 变了**：`<文件>` 从「字节码 JSON」变成 **源码**
（与 `jishi <文件>` 对齐），字节码要走 `--load-bytecode`。所以 `tests/` 与
`tools/` 里原先直接喂字节码的调用点都补了那个开关（35 处）。

⚠️ **两条已知边界**（都在下面钉着，改了两边一起改）：

1. **超时**：本机的 Python 侧「沙箱超时」会**丢输出**（`--sandbox` 跑死循环时
   stdout 全空、退出码 1，且 0.5 秒的超时要跑十几秒 —— 那个泄漏的守护线程一直
   在抢 GIL）。所以超时这一格**不进对拍**，只钉宿主自己的行为：它在
   `timeout + ε` 内带回一条 `执行超时（… 秒）` 的协议 JSON 并退出。
2. **`--dump-bytecode` 里没有「基石库层」的展开**（宿主用原生标准库实现），
   所以只有用到 `参数/容器/统计/迭代` 的程序与 Python 不同 —— 判据用的是
   **往返**（dump → load 跑出来一样），不依赖 Python。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _exe() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


EXE = _exe()
need_rust = pytest.mark.skipif(
    not EXE.exists(),
    reason="Rust 宿主未构建（cd rust && cargo build --release）"
           "—— 与其假装通过，不如明确跳过")

#: `duration_ms` 是**墙上时间**，两边永远不可能相等 —— 比之前先归一。
DUR = re.compile(rb'duration_ms[^0-9]*[-0-9][0-9.eE+-]*')
DUR_ZERO = b'duration_ms": 0'


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    # ⚠️ 语料在**私有 cwd** 里跑（tutorial 那几个程序会往 cwd 写真实文件，
    # 留在仓库根就是给下一轮 `check_engines` 埋雷）。所以 Python 侧用 PYTHONPATH
    # 指回仓库根，`-m oracle.jishi.cli` 才找得到包。
    env["PYTHONPATH"] = str(ROOT)
    return env


def _run(cmd, stdin: bytes = b"", cwd=None, timeout: float = 120.0):
    r = subprocess.run(cmd, input=stdin, capture_output=True, env=_env(),
                       cwd=str(cwd or ROOT), timeout=timeout)
    return r.stdout, r.stderr, r.returncode


def _rs(args, stdin: bytes = b"", cwd=None, timeout: float = 120.0):
    return _run([str(EXE)] + list(args), stdin, cwd, timeout)


def _py(args, stdin: bytes = b"", cwd=None, timeout: float = 120.0):
    return _run([sys.executable, "-m", "oracle.jishi.cli"] + list(args), stdin, cwd,
                timeout)


def _norm(t: bytes) -> bytes:
    return DUR.sub(DUR_ZERO, t)


@pytest.fixture(scope="module")
def workdir(tmp_path_factory):
    """私有 cwd：语料里的程序写文件时别落在仓库里。"""
    return tmp_path_factory.mktemp("cli_rust")


# ---------------------------------------------------------------------------
# 一、对拍：`<文件>` / `--stdin` / `--ast` / `--json-errors`
# ---------------------------------------------------------------------------

#: 挑的都是**会正常结束、不读标准输入**的语料（读输入的喂空 stdin 两边行为不同，
#: 那是另一件事）。error_cases 那几条专门覆盖「语法错 / 词法错」两条路。
FILES = [
    "tests/cases/01_算术.jsh",
    "tests/cases/02_判断.jsh",
    "tests/cases/03_循环.jsh",
    "tests/cases/04_函数.jsh",
    "tests/cases/10_复合赋值与逻辑.jsh",
    "tests/cases/11_emoji与码点.jsh",
    "tests/cases/18_集合.jsh",
    "tests/cases/26_遍历中改列表.jsh",
    "tests/error_cases/01_未定义名字.jsh",
    "tests/error_cases/02_除以零.jsh",
    "tests/error_cases/05_字符串没结束.jsh",
    "tests/error_cases/06_关键字粘连.jsh",
    "tests/frontend_cases/01_超范围整数.jsh",
]


@need_rust
@pytest.mark.parametrize("rel", FILES)
def test_对拍_跑源码(rel, workdir):
    """`jishi-rs <文件.jsh>` 与 `jishi <文件.jsh>` 的 stdout/stderr/退出码一致。"""
    p = str(ROOT / rel)
    a, b = _rs([p], cwd=workdir), _py([p], cwd=workdir)
    assert a == b, f"{rel}\n  rust={a}\n  py  ={b}"


@need_rust
@pytest.mark.parametrize("rel", FILES)
def test_对拍_ast(rel, workdir):
    """`--ast` 的缩进树逐字一致（这是**调试**输出，但也不能两边长得不一样）。"""
    p = str(ROOT / rel)
    assert _rs([p, "--ast"], cwd=workdir) == _py([p, "--ast"], cwd=workdir)


@need_rust
@pytest.mark.parametrize("rel", FILES)
def test_对拍_json报错(rel, workdir):
    """`--json-errors` 逐字节一致（含 `fix` 那个替换建议）。"""
    p = str(ROOT / rel)
    assert _rs([p, "--json-errors"], cwd=workdir) \
        == _py([p, "--json-errors"], cwd=workdir)


@need_rust
def test_对拍_stdin(workdir):
    """`--stdin` 从标准输入读源码 —— 两边的文件名都该是 `<stdin>`。"""
    src = "打印(\"你好\")\n".encode("utf-8")
    assert _rs(["--stdin"], src, workdir) == _py(["--stdin"], src, workdir)
    bad = "print(1)\n".encode("utf-8")
    assert _rs(["--stdin", "--json-errors"], bad, workdir) \
        == _py(["--stdin", "--json-errors"], bad, workdir)


#: 沙箱用例：正常输出 / 白名单内外的导入 / 语法与运行期错 / 输出超限。
#: ⚠️ **超时那一格故意不在里面**（见模块头「两条已知边界」）。
SANDBOX = [
    '打印("你好")\n',
    '导入 数学\n打印(数学.开方(4))\n',
    '导入 文件\n',
    '导入 网络\n',
    '导入 不存在的模块\n',
    '导入 os 从 python\n',
    '导入 x 从 本地包\n',
    '打印(1 / 0)\n',
    '打印(没有这个名)\n',
    '如果 真\n',
    'print(1)\n',
    '令 列 = [1, 2, 3]\n遍历 x 在 列：\n    打印(x)\n',
    '遍历 i 在 范围(3000)：\n    打印("一二三四五")\n',
    '抛出 值错误("自己抛的")\n',
    '函数 甲()：\n    返回 1 / 0\n甲()\n',
]


@need_rust
@pytest.mark.parametrize("src", SANDBOX, ids=range(len(SANDBOX)))
def test_对拍_沙箱协议(src, workdir):
    """`--sandbox --json-result` 的协议 JSON 一致（`duration_ms` 归一后）。"""
    args = ["--stdin", "--sandbox", "--json-result", "--timeout", "1"]
    a, b = _rs(args, src.encode("utf-8"), workdir), _py(args, src.encode("utf-8"), workdir)
    assert (_norm(a[0]), a[1], a[2]) == (_norm(b[0]), b[1], b[2]), \
        f"{src!r}\n  rust={a}\n  py  ={b}"


# ---------------------------------------------------------------------------
# 二、独立判据（**不 import oracle.jishi、不 spawn Python**）
# ---------------------------------------------------------------------------

@need_rust
def test_独立_跑源码(workdir):
    """写死期望：宿主自己就该产出这些字。"""
    src = ("令 甲 = 1\n如果 甲 > 0：\n    打印(\"正数\", 甲)\n"
           "函数 加(a, b)：\n    返回 a + b\n打印(加(1, 2))\n")
    f = workdir / "t1.jsh"
    f.write_text(src, encoding="utf-8")
    out, err, rc = _rs([str(f)], cwd=workdir)
    assert (out, err, rc) == ("正数 1\n3\n".encode("utf-8"), b"", 0)


@need_rust
def test_独立_ast逐字(workdir):
    """`--ast` 的输出**整串写死**（Python 的 `dump_ast` 就是长这样）。"""
    f = workdir / "t2.jsh"
    f.write_text("令 甲 = 1\n", encoding="utf-8")
    want = (
        "Program(\n"
        "  body:\n"
        "  Assign(\n"
        "  target: Name(\n"
        "  id: '甲'\n"
        "    )\n"
        "  op: '='\n"
        "  value: Num(\n"
        "  value: 1\n"
        "    )\n"
        "  )\n"
        ")\n"
    )
    out, err, rc = _rs([str(f), "--ast"], cwd=workdir)
    assert (out.decode("utf-8"), err, rc) == (want, b"", 0)


@need_rust
def test_独立_json报错_含fix(workdir):
    """`print(1)` 的 `--json-errors` 整串写死 —— `fix` 那段以前宿主**给不出来**。"""
    f = workdir / "t3.jsh"
    f.write_text("print(1)\n", encoding="utf-8")
    out, err, rc = _rs([str(f), "--json-errors"], cwd=workdir)
    want = ('{"ok": false, "errors": [{"code": "E0201", '
            '"title": "这里出现了意外的内容", '
            '"message": "「print」是 Python 的内建函数，基石里叫「打印」", '
            '"line": 1, "col": 1, '
            '"hint": "把 print(…) 改成 打印(…)", '
            '"fix": {"old": "print", "new": "打印", "line": 1, "col": 1}}]}')
    assert out == b""
    assert (err.decode("utf-8").strip(), rc) == (want, 1)


@need_rust
def test_独立_沙箱协议(workdir):
    """沙箱的协议 JSON 整串写死（`duration_ms` 归一）—— 这是 MCP 的原语。"""
    cases = {
        '打印("你好")\n': '{"ok": true, "value": null, "stdout": "你好\\n", "duration_ms": 0}',
        '导入 文件\n': '{"ok": false, "error": {"code": "E2000", "title": "运行期错误", '
                     '"message": "沙箱禁止导入「文件」（涉及文件读写）", "line": 1, "col": 1, '
                     '"hint": "如需文件读写，请显式开启 allow_write"}, "duration_ms": 0}',
        '导入 不存在的模块\n': '{"ok": false, "error": {"code": "E2000", "title": "运行期错误", '
                          '"message": "沙箱不允许导入模块「不存在的模块」", "line": 1, "col": 1, '
                          '"hint": null}, "duration_ms": 0}',
    }
    for src, want in cases.items():
        out, err, rc = _rs(["--stdin", "--sandbox", "--json-result"], src.encode("utf-8"),
                           workdir)
        assert err == b"", (src, err)
        assert _norm(out).decode("utf-8").strip() == want, (src, out)
        assert rc == (0 if '"ok": true' in want else 1)


@need_rust
@pytest.mark.计时
def test_独立_沙箱超时_真kill(workdir):
    """**沙箱超时的验收就在这一条**：宿主必须在 `timeout + ε` 内带着
    协议 JSON **退出**（不是挂在那里）。

    ⚠️ 这是本机 Python 侧**做不到**的那一格（它的守护线程抢 GIL：0.5 秒的
    超时要跑十几秒，而且收尾时把输出整个丢掉）—— 所以它不进对拍，只钉宿主。
    """
    src = "令 i = 0\n当 真：\n    i = i + 1\n".encode("utf-8")
    t0 = time.perf_counter()
    out, err, rc = _rs(["--stdin", "--sandbox", "--json-result", "--timeout", "0.5"],
                       src, workdir, timeout=30)
    dt = time.perf_counter() - t0
    text = _norm(out).decode("utf-8").strip()
    assert rc == 1 and err == b"", (out, err, rc)
    assert '"message": "执行超时（0.5 秒）"' in text, text
    assert '"hint": "可能是有死循环，检查循环条件或递归结束条件"' in text, text
    # 真 kill 的判据：**别拖**（Python 侧同一格要十几秒）。
    assert dt < 8.0, f"沙箱超时用了 {dt:.1f}s —— 没做到「到点就退」"


@need_rust
def test_独立_字节码往返(workdir):
    """`--dump-bytecode` → `--load-bytecode` 跑出来与直接跑**一样**。

    这一条**完全不依赖 Python**（编译、序列化、执行全在宿主里）——
    正是 R8 之后 Python 退场时要守住的那条线。
    """
    src = "函数 平方(x)：\n    返回 x * x\n打印(平方(7))\n"
    f = workdir / "rt.jsh"
    f.write_text(src, encoding="utf-8")
    direct = _rs([str(f)], cwd=workdir)
    assert direct[2] == 0, direct

    bc, err, rc = _rs([str(f), "--dump-bytecode"], cwd=workdir)
    assert (err, rc) == (b"", 0) and bc.startswith(b'{"format":"jishi-bytecode"'), bc[:80]
    bcf = workdir / "rt.json"
    bcf.write_bytes(bc)
    assert _rs([str(bcf), "--load-bytecode"], cwd=workdir) == direct


@need_rust
def test_独立_位置参数是源码不是字节码(workdir):
    """R7.4 的**语义改动**要有钉子：喂一份字节码 JSON 给位置参数，应当是
    「拿 JSON 当源码」的语法/词法错，而**不是**把它当字节码跑起来。
    """
    f = workdir / "s.jsh"
    f.write_text("打印(1)\n", encoding="utf-8")
    bc, _, _ = _rs([str(f), "--dump-bytecode"], cwd=workdir)
    bcf = workdir / "s.json"
    bcf.write_bytes(bc)
    out, _, rc = _rs([str(bcf)], cwd=workdir)
    assert rc == 1 and out == b"", (out, rc)          # 没跑起来
    out2, _, rc2 = _rs([str(bcf), "--load-bytecode"], cwd=workdir)
    assert (out2, rc2) == (b"1\n", 0)                # 加开关才当字节码


@need_rust
def test_独立_没有PATH也能跑(workdir):
    """清空环境也要能出结果 —— 证明运行期不 shell out（`--ai-card` 同理）。"""
    env = {} if os.name != "nt" else {"SystemRoot": os.environ.get("SystemRoot", "C:\\Windows")}
    f = workdir / "p.jsh"
    f.write_text("打印(6 * 7)\n", encoding="utf-8")
    r = subprocess.run([str(EXE), str(f)], capture_output=True, input=b"", env=env,
                       cwd=str(workdir), timeout=60)
    assert (r.stdout, r.returncode) == (b"42\n", 0), (r.stdout, r.stderr)


@need_rust
def test_独立_版本号():
    """`-v` 与 Python 侧的 `基石 jishi <版本>` 同形（版本号本身另有漂移检测）。"""
    out, _, rc = _rs(["-v"])
    assert rc == 0 and out.decode("utf-8").startswith("基石 jishi "), out


# ---------------------------------------------------------------------------
# 闸门照出来的三处显示/语义差异（R7.4 修，钉住不许回去）
# ---------------------------------------------------------------------------

_EMPTY_LINE_SRC = "如果 真：\n\n"
"""第 2 行是**空行**。Python 的 `render()` 用 `if self.source_line:` 把关，
空串为假 → 连源码行带 `^` 指示行**一起跳过**。宿主以前只看 `get_line` 有没有
返回 `Some`，于是多印两行（`tests/error_cases/41_块首后空行.jsh` 就是这个形状）。"""


@need_rust
def test_对拍_空源码行的报错渲染(workdir):
    f = workdir / "空行.jsh"
    f.write_text(_EMPTY_LINE_SRC, encoding="utf-8")
    a = _rs([str(f)], cwd=workdir)
    b = _py([str(f)], cwd=workdir)
    assert a == b, (a[1].decode("utf-8", "replace"), b[1].decode("utf-8", "replace"))
    # 独立钉子：不该出现「  2 │ 」那一行（源码行是空的，整段不印）
    text = a[1].decode("utf-8")
    assert "  2 │" not in text and "│ ^" not in text, text


@need_rust
def test_对拍_ast含super_ok(workdir):
    """`_super_ok` 在 Python 侧是 `setattr(node, "_super_ok", True)` 动态挂的，
    排在 `vars()` 的最后；Rust 做成了结构体字段（不在 `fields` 里），
    于是 `--ast` 以前**少印这一行**。"""
    f = workdir / "超.jsh"
    f.write_text(
        "类 父：\n"
        "    函数 初始化(自身)：\n"
        "        自身.名 = 1\n"
        "类 子 继承 父：\n"
        "    函数 初始化(自身)：\n"
        "        超().初始化()\n",
        encoding="utf-8")
    a = _rs([str(f), "--ast"], cwd=workdir)
    b = _py([str(f), "--ast"], cwd=workdir)
    assert a == b, a[0].decode("utf-8", "replace")
    assert "_super_ok: True" in a[0].decode("utf-8"), a[0].decode("utf-8")


_JOIN_SRC = """导入 文件
打印(文件.路径拼接(".", "pyproject.toml"))
打印(文件.路径拼接("./x", "y"))
打印(文件.路径拼接("a//b", "c"))
打印(文件.路径拼接("a", ".."))
打印(文件.路径拼接("/a", "b"))
打印(文件.路径拼接("a", "/b"))
打印(文件.路径拼接("", ""))
打印(文件.路径拼接("a", "."))
打印(文件.路径拼接())
打印(文件.路径拼接("x", ".", "y"))
打印(文件.路径拼接("./", "x"))
打印(文件.路径拼接("/", ""))
"""

_JOIN_WANT = (
    "pyproject.toml\nx/y\na/b/c\na/..\n/a/b\n/b\n.\na\n.\nx/y\nx\n/\n"
)
"""写死的期望 —— 就是 `PurePosixPath(*parts)` 的语义（`pathlib` 的实测结果）。

⚠️ **关键不是「用 `/` 连起来」**：它会**吃掉空段与 `.` 段**（`./x` → `x`、
`a//b` → `a/b`），但**保留 `..`**（`a/..` 原样留着，`PurePath` 不做消解）；
整段都空给 `.`，绝对路径给 `/`。宿主以前只做拼接，于是
`文件.路径拼接(".", "pyproject.toml")` 给 `./pyproject.toml` —— 这个串还会被
塞进「找不到文件「X」，无法读取」这类**给用户看的报错**里。
"""


@need_rust
def test_对拍_路径拼接按PurePosixPath归一(workdir):
    f = workdir / "拼接.jsh"
    f.write_text(_JOIN_SRC, encoding="utf-8")
    a = _rs([str(f)], cwd=workdir)
    b = _py([str(f)], cwd=workdir)
    assert a == b, (a[0], b[0])
    assert a[0].decode("utf-8") == _JOIN_WANT, a[0].decode("utf-8")


#: `路径.连接` 的真值表 —— 期望由测试自己用 `pathlib` 现算（**平台原生**，
#: Windows 上是 `\`、别处是 `/`），所以它同时是一份「跨平台也成立」的对照。
_JOIN2_PARTS = [
    (), (".", "x"), ("./a", "b"), ("a", "b"), ("", "b"), ("a", ""),
    ("a//b", "c"), ("a", ".."), ("..", "a"), (".", "."), ("", ""),
    ("./", "x"), ("x", ".", "y"), ("/b",), ("a", "/b"),
    ("C:", "x"), ("C:\\", "x"), ("C:\a", "/b"), ("a/b/", "c"),
]


@need_rust
def test_对拍_路径连接按pathlib(workdir):
    """`路径.连接` 用的是 `pathlib.Path` 的 `a / b`（**不是** `PathBuf::push`）。

    ⚠️ 差在「`.` 与空段被吃掉」：`路径.连接(".", "x")` 必须是 `x`，
    而 Rust 的 `PathBuf::push` 会给 `./x`。还有一条更隐蔽的：
    **只带根不带盘符的段要保留已有盘符**（`C:\a` / `/b` → `C:\b`）。
    """
    from pathlib import Path as _P

    lines = ["导入 路径"]
    want = []
    for parts in _JOIN2_PARTS:
        args = ", ".join('"%s"' % p.replace("\\", "\\\\") for p in parts)
        lines.append(f"打印(路径.连接({args}))")
        p = None
        for seg in parts:
            p = _P(seg) if p is None else p / seg
        want.append(str(p if p is not None else _P(".")))
    f = workdir / "连接.jsh"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    a = _rs([str(f)], cwd=workdir)
    assert a[2] == 0, a[1].decode("utf-8", "replace")
    got = a[0].decode("utf-8").splitlines()
    assert got == want, "\n".join(
        f"{parts!r}: rust={g!r} py={w!r}"
        for parts, g, w in zip(_JOIN2_PARTS, got, want) if g != w)
    # 与 Python 侧 CLI 也逐字节对一遍（两条路都要绿）
    assert a == _py([str(f)], cwd=workdir)
