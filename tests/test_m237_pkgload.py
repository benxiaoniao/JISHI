# -*- coding: utf-8 -*-
"""M237 · `包`（运行期加载本地包）—— **本轮只在 Python 侧落地**。

## 这一站做了什么、没做什么（先看这里）

| 执行器 | 状态 |
|---|---|
| 树遍历 `Interpreter` | ✅ 真跑（`跑源码` 共用 `self.globals`，同一条调用栈） |
| Python `VM` | ✅ 真跑（`O.合并模块` 把新模块四张表追加进来，`Code` 记基址） |
| Rust 宿主 | ⚠️ **明确报错**（不是静默错），原因见 `rust/src/stdlib/pkgload.rs` 文件头 |
| C VM | ⚠️ 同 Rust（它更进一步：`consts`/`instrs` 是宿主持有的扁平数组） |

**为什么 Rust/C 没做**（一句话）：`包.加载` 要**改正在跑的那个模块的表**，
而 Rust 的 `execute_frame` 为了让几千行的指令循环能长期借用 `instrs`，把
`module` 提升成 `Rc<Module>` 并一直借着；C 侧的表是宿主持有的定长数组。
两边都要么动**最热的循环**（有性能代价，得重量基线），要么改**跨语言字节码契约** ——
那是单独一轮的事，不在收口前顺手改。

📌 这与 `串口` / `路径.监视` 在 **Python 侧**的处理是同一个口径：
「另一个宿主暂时没有，就把话说清楚」，而不是静默给假值。

## 实现要点

- **必须编进调用者的执行器**：包的导出是**函数**，函数绑定在创建它的执行器上
  （`Code` 里的常量/名字下标都是相对本模块的）。换一个执行器代劳 ⇒
  「拿到错误的常量表」（M145 老坑，`localpkg.rs` 文件头记着）。
- **表基址**（`consts_base` / `names_base` / `kw_base` / `codes_base`）：
  合并时给新模块的每个 `Code` 记一个起点，取表处加一次加法。
  🔴 **不重写指令里的下标** —— 那需要一张「每条指令的 a/b/c 哪个是下标」的语义表，
  没人维护它，漏一个就是**静默取错常量**。
- **命名空间隔离**：包被包在一个外壳函数里跑（与编译期 `导入` 展开同一套），
  顶层名字出函数就没了，只有导出的进字典。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.conftest import rust_exe_path  # noqa: E402

RUST_EXE = rust_exe_path()
_需要引擎 = pytest.mark.skipif(not RUST_EXE.is_file(),
                              reason="没有 Rust 发行版（cargo build --release）")


def _建包(tmp: Path, 包名: str, 文件们: dict, 元: dict | None = None) -> None:
    目录 = tmp / ".jishi" / "packages" / 包名
    目录.mkdir(parents=True, exist_ok=True)
    import json
    (目录 / "包.json").write_text(
        json.dumps(元 or {"名字": 包名, "版本": "0.1.0"}, ensure_ascii=False),
        encoding="utf-8")
    for 名, 内容 in 文件们.items():
        (目录 / 名).write_text(内容, encoding="utf-8", newline="\n")


def _跑树遍历(源: str, tmp: Path, 名: str) -> str:
    文件 = tmp / f"{名}-{os.getpid()}.jsh"
    文件.write_text(源, encoding="utf-8", newline="\n")
    r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", str(文件)],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", timeout=180)
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    return (r.stdout or "").replace("\r\n", "\n")


def _跑VM(源: str, tmp: Path, 名: str) -> str:
    """Python 字节码 VM（不经 CLI 的树遍历）。"""
    文件 = tmp / f"{名}-vm-{os.getpid()}.jsh"
    文件.write_text(源, encoding="utf-8", newline="\n")
    码 = (
        "import sys, io, contextlib\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from oracle.jishi.compiler import compile_source\n"
        "from oracle.jishi.vm import VM\n"
        f"src = open({str(文件)!r}, encoding='utf-8').read()\n"
        f"cmod = compile_source(src, {str(文件)!r})\n"
        f"vm = VM(cmod, {str(文件)!r})\n"
        "vm.run()\n"
    )
    r = subprocess.run([sys.executable, "-c", 码], cwd=str(tmp),
                       capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    return (r.stdout or "").replace("\r\n", "\n")


# ---------------------------------------------------------------------------
# 一、两条 Python 路径都要真跑，而且逐字一致
# ---------------------------------------------------------------------------

def test_树遍历能运行期加载包(tmp_path):
    _建包(tmp_path, "插件", {
        "主.jsh": ('令 前缀 = "处理结果："\n'
                   '函数 处理(输入)：\n    返回 前缀 + 输入\n'
                   '函数 加和(a, b)：\n    返回 a + b\n'),
    })
    源 = '''导入 包
令 插件 = 包.加载("插件")
打印(类型(插件), 插件["处理"]("甲"), 插件["加和"](3, 4))
令 再 = 包.加载("插件")
打印("同一份", 再 == 插件)
'''
    出 = _跑树遍历(源, tmp_path, "pkgload")
    assert 出.strip().splitlines() == ["字典 处理结果：甲 7", "同一份 真"], 出


def test_字节码VM也一致(tmp_path):
    """同样的程序走 Python 字节码 VM —— 输出必须**逐字**相同。

    🔴 这条是「合并模块 + Code 基址」方案的真正验收：VM 里包的函数是用
    **合并后的表**建出来的，调用时靠 `consts_base` 才算对常量。
    基址算错的话，`前缀` 会取到别的常量（不报错、结果错）。
    """
    _建包(tmp_path, "插件", {
        "主.jsh": ('令 前缀 = "处理结果："\n'
                   '函数 处理(输入)：\n    返回 前缀 + 输入\n'
                   '函数 加和(a, b)：\n    返回 a + b\n'),
    })
    源 = '''导入 包
令 插件 = 包.加载("插件")
打印(类型(插件), 插件["处理"]("甲"), 插件["加和"](3, 4))
令 再 = 包.加载("插件")
打印("同一份", 再 == 插件)
'''
    甲 = _跑树遍历(源, tmp_path, "pkgload")
    乙 = _跑VM(源, tmp_path, "pkgload")
    assert 甲 == 乙, f"树遍历与 VM 不一致：\n--- 树遍历 ---\n{甲}\n--- VM ---\n{乙}"


def test_包内顶层名字不污染主程序(tmp_path):
    """包里的 `令 内部 = …` 出了包就没了（外壳函数隔离，与编译期导入同一套规则）。"""
    _建包(tmp_path, "隔离", {
        "主.jsh": '令 内部 = 42\n函数 取值()：\n    返回 内部\n',
    })
    源 = '''导入 包
令 m = 包.加载("隔离")
打印(m["取值"]())
尝试：
    打印(内部)
捕获 异常 为 e：
    打印("主程序看不到", e.类型)
'''
    出 = _跑树遍历(源, tmp_path, "isolation")
    assert 出.strip().splitlines() == ["42", "主程序看不到 找不到这个名字"], 出


def test_包内多文件也按依赖排序(tmp_path):
    """M235 的 `_按依赖排序` 在运行期这条路上同样生效（不是只修了编译期）。"""
    _建包(tmp_path, "多文件", {
        "甲.jsh": "令 基数 = 100\n",
        "乙.jsh": '令 加倍 = 基数 * 2\n函数 取()：\n    返回 加倍\n',
    })
    源 = '''导入 包
令 m = 包.加载("多文件")
打印(m["取"]())
'''
    assert _跑树遍历(源, tmp_path, "order").strip() == "200"


# ---------------------------------------------------------------------------
# 二、错误路径：说清、不崩
# ---------------------------------------------------------------------------

def test_包不存在时列出已装清单(tmp_path):
    _建包(tmp_path, "真有的包", {"主.jsh": "函数 f()：\n    返回 1\n"})
    源 = '''导入 包
尝试：
    包.加载("根本不存在的包")
捕获 异常 为 e：
    打印(e.类型)
'''
    文件 = tmp_path / f"miss-{os.getpid()}.jsh"
    文件.write_text(源, encoding="utf-8", newline="\n")
    r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", str(文件)],
                       cwd=str(tmp_path), capture_output=True, text=True,
                       encoding="utf-8", timeout=180)
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    assert "值错误" in r.stdout, r.stdout


def test_带依赖的包明确报错而不是静默少东西(tmp_path):
    """`包.json` 里有 `依赖` ⇒ 明确说「运行期加载不支持」，指向编译期那条路。

    🔴 这条是「不许留半成品」的具体落点：如果悄悄忽略依赖，包里用到依赖的
    那一行会在**深处**报「找不到名字」，用户根本想不到是依赖没加载。
    """
    _建包(tmp_path, "带依赖", {"主.jsh": "函数 f()：\n    返回 1\n"},
          元={"名字": "带依赖", "版本": "0.1.0", "依赖": {"别的包": "^1.0"}})
    源 = '''导入 包
尝试：
    包.加载("带依赖")
    打印("居然通过了")
捕获 异常 为 e：
    打印("拒绝", e.类型)
'''
    出 = _跑树遍历(源, tmp_path, "deps")
    assert 出.strip() == "拒绝 值错误", 出


@_需要引擎
def test_Rust发行版也能加载包且与Python逐字一致(tmp_path):
    """Rust 字节码执行器（发行版）**也真跑** `包.加载`，输出必须与树遍历逐字相同。

    🔴 这条是「表基址」方案在 Rust 侧的验收：包的代码块被**追加**进本 VM，
    靠 `consts_base` / `names_base` / `codes_base` 才算对常量、名字、函数。
    任何一处基址漏加，症状都是**静默取错值**（不是报错）—— 所以必须逐字比。
    """
    _建包(tmp_path, "插件", {
        "主.jsh": ('令 前缀 = "处理结果："\n'
                   '函数 处理(输入)：\n    返回 前缀 + 输入\n'
                   '函数 加和(a, b)：\n    返回 a + b\n'),
    })
    源 = '''导入 包
令 插件 = 包.加载("插件")
打印(类型(插件), 插件["处理"]("甲"), 插件["加和"](3, 4))
令 再 = 包.加载("插件")
打印("同一份", 再 == 插件)
'''
    文件 = tmp_path / f"rust-{os.getpid()}.jsh"
    文件.write_text(源, encoding="utf-8", newline="\n")
    r = subprocess.run([str(RUST_EXE), str(文件)], cwd=str(tmp_path),
                       capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    出 = (r.stdout or "").replace("\r\n", "\n")
    甲 = _跑树遍历(源, tmp_path, "pkgload")
    assert 出 == 甲, f"Rust 与树遍历不一致：\n--- Rust ---\n{出}\n--- 树遍历 ---\n{甲}"


def _C可用() -> bool:
    try:
        from oracle.jishi import cvm_bind
        cvm_bind.load_lib()
        return True
    except Exception:
        return False


_HAS_CVM = _C可用()


@pytest.mark.skipif(not _HAS_CVM, reason="C 虚拟机动态库不可用（先跑 python cvm/build.py）")
def test_C字节码VM也一致(tmp_path):
    """C VM（`cvm/bin/jsvm.dll`）也真跑，且与树遍历逐字相同。

    🔴 这条验的是 C 侧的「基址入帧 + append_module + run_code」三件事：
    基址错 ⇒ 取错常量（不报错、值错）；`run_code` 的 stop_depth 没抬 ⇒
    会把外层程序一起带走；旧数组被 free ⇒ 悬垂指针（崩溃而非报错）。
    所以必须真跑 + 逐字比。
    """
    import json as _json
    _建包(tmp_path, "插件", {
        "主.jsh": ('令 前缀 = "处理结果："\n'
                   '函数 处理(输入)：\n    返回 前缀 + 输入\n'
                   '函数 加和(a, b)：\n    返回 a + b\n'),
    })
    源 = '''导入 包
令 插件 = 包.加载("插件")
打印(类型(插件), 插件["处理"]("甲"), 插件["加和"](3, 4))
令 再 = 包.加载("插件")
打印("同一份", 再 == 插件)
'''
    文件 = tmp_path / f"cvm-{os.getpid()}.jsh"
    文件.write_text(源, encoding="utf-8", newline="\n")
    码 = (
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from oracle.jishi.cvm_bind import run_source_c\n"
        f"src = open({str(文件)!r}, encoding='utf-8').read()\n"
        f"run_source_c(src, {str(文件)!r})\n"
    )
    r = subprocess.run([sys.executable, "-c", 码], cwd=str(tmp_path),
                       capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")[-800:]
    出 = (r.stdout or "").replace("\r\n", "\n")
    甲 = _跑树遍历(源, tmp_path, "pkgload")
    assert 出 == 甲, f"C VM 与树遍历不一致：\n--- C VM ---\n{出}\n--- 树遍历 ---\n{甲}"


@_需要引擎
def test_Rust树遍历Walker也一致(tmp_path):
    """Rust 的**树遍历**（`--walk AST.json`）也真跑，且与发行版逐字相同（M239 G1）。

    Walker 的入口要 **AST JSON**（不是源码）。这里用 `tools/conformance.jsonable`
    把 AST 转成 JSON —— 这也是「源码 → AST JSON」唯一现成的通道
    （正式 CLI 开关 `--ast --format json` 归 0.3.8 的 H3/M241 补）。

    🔴 这条同时钉住两件事：① `包.加载` 在 Walker 上不再报错；
    ② **同名只建一次**（`包.py` 的缓存口径）—— 不缓存的话两次会各建一份门面，
    而字典里的值是函数、函数按**身份**比较，`再 == 插件` 会得「假」。
    """
    import json as _json
    sys.path.insert(0, str(ROOT / "tools"))
    import conformance as _C                      # noqa: PLC0415
    from oracle.jishi.parser import parse_source  # noqa: PLC0415

    _建包(tmp_path, "插件", {
        "主.jsh": ('令 前缀 = "处理结果："\n'
                   '函数 处理(输入)：\n    返回 前缀 + 输入\n'
                   '函数 加和(a, b)：\n    返回 a + b\n'),
    })
    源 = '''导入 包
令 插件 = 包.加载("插件")
打印(类型(插件), 插件["处理"]("甲"), 插件["加和"](3, 4))
令 再 = 包.加载("插件")
打印("同名同一份", 再 == 插件)
'''
    ast = _C.jsonable(parse_source(源, "主.jsh"))
    ast文件 = tmp_path / f"ast-{os.getpid()}.json"
    ast文件.write_text(_json.dumps(ast, ensure_ascii=False), encoding="utf-8")

    r = subprocess.run([str(RUST_EXE), "--walk", str(ast文件)], cwd=str(tmp_path),
                       capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    出 = (r.stdout or "").replace("\r\n", "\n")

    # 与同一条源码走发行版（字节码 VM）的结果比 —— 两棵树必须逐字相同
    源文件 = tmp_path / f"vm-{os.getpid()}.jsh"
    源文件.write_text(源, encoding="utf-8", newline="\n")
    r2 = subprocess.run([str(RUST_EXE), str(源文件)], cwd=str(tmp_path),
                        capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert r2.returncode == 0, (r2.stdout or "") + (r2.stderr or "")
    assert 出 == (r2.stdout or "").replace("\r\n", "\n"), (
        f"Walker 与字节码 VM 不一致：\n--- Walker ---\n{出}\n--- VM ---\n{r2.stdout}")


@_需要引擎
def test_包加载不再报不支持(tmp_path):
    """M239 G1 之后，Rust 侧两条路（VM / Walker）都**不再**报「不支持 `包.加载`」。

    这条留着当**反向守卫**：如果哪天有人把实现回退成桩（或者条件编译把它排除掉），
    那句明确报错会重新出现 —— 那时应该看 `test_Rust树遍历Walker也一致` 红在哪。
    """
    _建包(tmp_path, "插件", {"主.jsh": '函数 加和(a, b)：\n    返回 a + b\n'})
    源 = '导入 包\n令 插件 = 包.加载("插件")\n打印(插件["加和"](1, 2))\n'
    文件 = tmp_path / f"nostub-{os.getpid()}.jsh"
    文件.write_text(源, encoding="utf-8", newline="\n")
    r = subprocess.run([str(RUST_EXE), str(文件)], cwd=str(tmp_path),
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    全 = (r.stdout or "") + (r.stderr or "")
    assert "不支持" not in 全, 全
    assert r.returncode == 0, 全

def test_模块与函数在表里():
    """`包` 与 `加载` 都在语言事实里（Python 侧是契约来源）。"""
    from oracle.jishi.ai import build_lang_spec

    规格 = build_lang_spec()
    模块 = {m["module"]: m for m in 规格["stdlib"]}
    assert "包" in 模块, "`包` 没注册进标准库表"
    assert any(f["name"] == "加载" for f in 模块["包"]["functions"]), 模块["包"]
