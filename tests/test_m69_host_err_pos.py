# -*- coding: utf-8 -*-
"""R6.6 · 宿主报错的**位置**与 `local_hints` —— 把 R6.3/R6.4 剩下的 5 条登记收掉。

## 一、那 4 条「`^` 落在哪一列」

R6.3/R6.4 对齐宿主报错时，剩下最后四条语料**只差一个字符**：

```
错误 E2002：类型错误（方法「追加」需要 1 个参数，但传了 0 个）
  ┌─ tests/error_cases/15_方法参数个数.jsh:2:2      ← Python
  ┌─ tests/error_cases/15_方法参数个数.jsh:2:5      ← 宿主（旧）
  │
  2 │ a.追加()
    │  ^                                            ← `X.方法` 那一处（`.`）
```

根因不是「谁少抄了一行」，而是**两边手里握着的位置不是同一个东西**：

* Python 侧报的是**取属性**的位置 —— 方法内部的错由 `_BoundMethod.__call__` 用
  绑定时记下的行列补（`runtime._BoundMethod.line/col`）；
* 宿主一路用的是**指令**的位置（`CALL`，也就是 `(` 那一列）。

修法照 Python 的机制：**让「绑定方法值」随身带着取属性的位置**，调用报错时用它。
Node 的 `BoundMethod` 本来就有 `line/col`（只是没接上）；Rust 的
`BoundMethod::Builtin` 补了两个字段。

⚠️ **模块属性（`数.开方`）不走这条路** —— 它是裸 `Builtin`、不带位置，两边都用
调用点，本来就一致。「带点的名字一律用得属性位置」是错的（试过）。

## 二、那 1 条 `local_hints`

`Code.local_hints`（按槽位的「外层也有同名变量…」）**不在字节码 JSON 里**，
宿主只能说通用那句 —— 类型与消息都对，只差那一句提示。

用户拍板**动格式**：字节码升 **v3**，`Code` 多一个 `local_hints`。
四个地方一起改（Python 前端 / Rust 前端 / 两个宿主），版本号有测试钉住
（`test_m25_varargs.py::test_字节码格式版本四处同步`）。

## 三、这个文件钉什么

1. 那 5 条语料在 **Python CLI / Rust 宿主 / Node 宿主**三处的 `stderr` **逐字节相同**；
2. 位置具体落在**取属性那一处**（不只是「一致」，还要**对**）；
3. 字节码 v3 里真的有 `local_hints`，且两个前端都产得出、`loads` 往返不丢。
"""

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jishi import serialize, tokenizer                        # noqa: E402
from jishi.compiler import compile_to_json, compile_source     # noqa: E402


def _rust_frontend_ok() -> bool:
    """Rust 前端（pyo3 扩展）装没装 —— 没装就**明确跳过**，不假装通过。"""
    return tokenizer.rust_available()


NODE = shutil.which("node")
HAS_NODE = NODE is not None
need_node = pytest.mark.skipif(not HAS_NODE, reason="本机无 Node.js，跳过宿主对拍")


def _rust_exe() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


RUST = _rust_exe()
need_rust = pytest.mark.skipif(
    not RUST.exists(),
    reason="Rust 宿主未构建（cd rust && cargo build --release）")


def _write(src: str, tag: str) -> str:
    """写到 `build/` 下，返回**相对仓库根**的路径。

    ⚠️ 必须是相对路径：宿主是按字节码里的 `filename` 原样读源码来画源码行的，
    测试的 cwd 也是仓库根 —— 两边得看到**同一个字符串**（绝对 / 相对混用，
    方框里的 `┌─ …` 就会不一样，这条踩过）。
    """
    rel = f"build/_m69_{tag}_{os.getpid()}.jsh"
    (ROOT / rel).parent.mkdir(parents=True, exist_ok=True)
    (ROOT / rel).write_text(src, encoding="utf-8")
    return rel


def _bc(src: str, rel: str) -> Path:
    """编出同一份字节码给两个宿主（**同一个文件**，两边读到的路径才一样）。"""
    bc = ROOT / "build" / (Path(rel).stem + ".json")
    bc.write_text(compile_to_json(src, rel), encoding="utf-8")
    return bc


def _run(cmd, cwd=ROOT) -> tuple[str, str]:
    r = subprocess.run(cmd, capture_output=True, input=b"", cwd=str(cwd))
    d = lambda b: b.decode("utf-8", "replace")                 # noqa: E731
    return d(r.stdout), d(r.stderr)


def _three(src: str, tag: str):
    """Python CLI / Rust / Node 各跑一遍 → `{名字: (stdout, stderr)}`。"""
    rel = _write(src, tag)
    bc = _bc(src, rel)
    out = {"Python": _run([sys.executable, "-m", "jishi.cli", rel])}
    if RUST.exists():
        out["Rust"] = _run([str(RUST), str(bc.relative_to(ROOT))])
    if HAS_NODE:
        out["Node"] = _run([NODE, "node/index.js", str(bc.relative_to(ROOT))])
    return out


# ---------------------------------------------------------------------------
# 一、五条登记的语料：三处渲染逐字节相同
# ---------------------------------------------------------------------------

#: 前四条是「`^` 落在哪一列」，最后一条是 `local_hints`。
_REGISTERED_CASES = {
    "方法参数个数": '令 a = [1]\na.追加()\n',
    "移除不存在": '令 a = [1, 2]\na.移除(9)\n',
    "文本转整数": '"abc".转整数()\n',
    "字典弹出缺键": '令 d = {"a"：1}\nd.弹出("b")\n',
    "赋值前读局部": (
        '令 全局 = 99\n\n'
        '函数 读(值)：\n'
        '    打印(全局)\n'
        '    令 全局 = 值\n\n'
        '读(1)\n'),
}


@pytest.mark.parametrize("tag", sorted(_REGISTERED_CASES))
def test_登记语料的报错渲染三处逐字相同(tag):
    """Python CLI / Rust / Node —— 同一个程序、同一份字节码，`stderr` 一个字节都不差。

    ⚠️ 这 5 条语料以前**登记在 `tools/check_engines.py` 的 `HOST_KNOWN_GAPS` 里**
    （「已知缺口」）。R6.6 修掉之后它们被那条登记机制报成「过期」→ 按规矩删掉。
    这条测试就是删掉登记的**替代物**：不登记，但要有人盯着。
    """
    outs = _three(_REGISTERED_CASES[tag], tag)
    base_name, (base_out, base_err) = "Python", outs["Python"]
    assert base_err, f"{tag}：基准本该报错 —— {base_out!r}"
    for name, (out, err) in outs.items():
        if name == base_name:
            continue
        assert out == base_out, f"{tag}：{name} 的 stdout 不同\n{out!r}"
        assert err == base_err, (
            f"{tag}：{name} 的报错与 Python 不一致\n"
            f"--- Python ---\n{base_err}\n--- {name} ---\n{err}")


@pytest.mark.parametrize("tag,col", [
    # `a.追加()` —— 报在 `X.方法` 那一处（第 2 列那个 `.`），不是 `(`（第 5 列）
    ("方法参数个数", ":2:2"),
    # `"abc".转整数()` —— 第 6 列那个 `.`
    ("文本转整数", ":1:6"),
    # `d.弹出("b")`
    ("字典弹出缺键", ":2:2"),
    # `赋值前读局部` —— 这个名字的读点在第 4 行第 8 列
    ("赋值前读局部", ":4:8"),
])
def test_位置落在取属性那一处(tag, col):
    """**不只是「一致」，还要落在对的地方**。

    ⚠️ 只断言「两边一样」是不够的：当初两边**一起**错成 `CALL` 的位置也算「一致」。
    方法调用的错要指向 `X.方法`（用户写方法名的地方），不是 `(`。
    """
    outs = _three(_REGISTERED_CASES[tag], f"pos_{tag}")
    for name, (_out, err) in outs.items():
        first = err.splitlines()[1] if len(err.splitlines()) > 1 else err
        assert col in first, f"{tag}：{name} 的位置不是 {col} —— {first!r}"


def test_模块属性与普通调用没被带偏():
    """⚠️ 修「方法」的位置时**不能顺手把所有调用都改掉** —— 这两类的基准不同：

    * `数.开方()`（标准库函数，模块属性）→ 两边都用**调用点**（`:2:5`）；
    * `甲()`（不能调用）→ 也用调用点（`:2:2`）。

    一句话：「名字里有 `.` 就用得属性位置」是**错的**（这条真试过、也真被测试拦住）。
    """
    outs = _three('导入 数学 为 数\n数.开方()\n', "mod")
    for name, (_o, err) in outs.items():
        assert ":2:5" in err.splitlines()[1], f"{name}：{err!r}"
    outs = _three('令 甲 = 5\n甲()\n', "notcallable")
    for name, (_o, err) in outs.items():
        assert ":2:2" in err.splitlines()[1], f"{name}：{err!r}"


# ---------------------------------------------------------------------------
# 二、字节码 v3：local_hints
# ---------------------------------------------------------------------------

def test_字节码里带着local_hints():
    """`local_hints` 必须进 JSON（v3）—— 这是「宿主给得出那句具体提示」的唯一来源。"""
    src = _REGISTERED_CASES["赋值前读局部"]
    body = json.loads(compile_to_json(src, "x.jsh"))
    assert body["version"] == serialize.FORMAT_VERSION == 3, body["version"]
    codes = {c["name"]: c for c in body["payload"]["codes"]}
    assert "读" in codes, list(codes)
    hints = codes["读"]["local_hints"]
    assert hints, "「读」这个函数有阴影局部，应该带上 local_hints"
    (slot, text), = hints.items()
    assert text == (
        "外层也有一个叫「全局」的变量。在这个函数里给它赋过值，"
        "它就成了这个函数的局部变量，赋值之前不能读取"), text
    # 槽位号与 `local_names` 对得上（不是瞎塞一个键）
    assert codes["读"]["local_names"][int(slot)] == "全局"


def test_local_hints往返不丢():
    """`loads(dumps(x))` 之后提示语还在 —— 序列化/反序列化两边都要改。

    ⚠️ 只改 `dump` 不改 `load` 是最容易漏的一半：JSON 里有、还原回对象时空掉。
    """
    src = _REGISTERED_CASES["赋值前读局部"]
    cmod = serialize.loads(serialize.dumps(compile_source(src, "x.jsh")))
    got = {c.name: c.local_hints for c in cmod.codes}
    assert got.get("读"), f"还原之后提示语没了：{got}"
    assert any("外层也有一个叫「全局」" in v for v in got["读"].values()), got


def test_没有阴影局部就不塞键():
    """空的时候给 `{}`，**不许**凭空造一个默认提示塞给每个函数。

    理由：那会让「有没有按槽位的提示」这件事实消失 —— 宿主分不出
    「这里确实有个具体说法」和「我编了一个通用的冒充」。
    """
    body = json.loads(compile_to_json('打印("你好")\n', "x.jsh"))
    for c in body["payload"]["codes"]:
        assert c["local_hints"] == {}, c


@pytest.mark.skipif(
    not _rust_frontend_ok(),
    reason="Rust 前端未构建（python core/build.py）")
def test_Rust前端也产出local_hints():
    """**两个前端都要产** —— 少一个方的 `conformance compare --stage bytecode` 就红。

    ⚠️ 这条曾经真的红过：Rust 前端把槽位号直接当 JSON 键写（`1:"…"`，没引号），
    产出的 JSON 根本不是合法 JSON —— 是 `conformance emit` 的「非基石错误拒绝
    冻结」那道闸门抓出来的。
    """
    src = _REGISTERED_CASES["赋值前读局部"]
    old = os.environ.get("JISHI_FRONTEND")
    os.environ["JISHI_FRONTEND"] = "rust"
    try:
        body = json.loads(compile_to_json(src, "x.jsh"))
    finally:
        if old is None:
            os.environ.pop("JISHI_FRONTEND", None)
        else:
            os.environ["JISHI_FRONTEND"] = old
    codes = {c["name"]: c for c in body["payload"]["codes"]}
    assert codes["读"]["local_hints"], "Rust 前端没产 local_hints"
