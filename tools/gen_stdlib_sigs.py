# -*- coding: utf-8 -*-
"""标准库函数签名表生成器（M51）。

## 为什么要有它

宿主要支持**命名参数**调用（`加密.摘要("abc", 算法="md5")`），就必须知道
「这个函数的第几个参数叫什么名字」。Node / Rust 两宿主都做不到运行时反射
（JS 拿得到形参名，但那是**宿主自己写的** `s` / `width`，与 Python 侧
的 `文本` / `宽度` 对不上；Rust 的函数指针根本没有参数名）。

三条路：

1. 在两宿主源码里**手抄**一张参数名表 —— 那就成了「同一件事写三遍」，
   正是 M51–M53 要消灭的东西，而且会漂移。
2. 让语言侧把签名编进字节码 —— 要改字节码格式，工程量远超「一致性止血」。
3. **从 Python 侧的标准库源码生成**两张表（本脚本）—— 事实源仍然是
   `jishi/stdlib/*.py`（与 `jishi --lang-spec` 同源），两宿主只是消费生成物。
   **零手抄、零漂移**。

选 3。

## 键为什么是「模块.函数」而不是「函数」

全库有 19 组**跨模块同名函数**（`解析` 在 json / 参数 / 日期 都有，
`计数` 在 容器 / 文本 / 迭代 都有……）。只按函数名查会歧义，
所以生成物一律用 `模块.函数` 限定。

## 产物

- `node/stdlib_sigs.js`      → `module.exports = { '模块': { '函数': [必填数, [参数名…]] } }`
- `rust/src/stdlib_sigs.rs`  → `pub fn sigof("模块.函数") -> Option<(usize, &'static [&'static str])>`

两宿主在收到命名参数时按同一张表把名字映射回位置参数，
**五引擎因此给出同一结果**（而不是像以前那样静默丢掉 kwargs）。

## 用法

    python tools/gen_stdlib_sigs.py            # 重新生成两个产物
    python tools/gen_stdlib_sigs.py --check    # 只校验产物是否与源码同步（不写盘）

`--check` 供 `tests/test_m51_consistency.py` 使用：改了 Python 侧标准库
却忘了重新生成，测试会直接报红 —— 这就是「漂移检测」的一部分。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Python 侧有、但**宿主侧故意没有**的模块。
#:
#: `测试` 是 Python 侧的测试框架自身（`导入 测试` 用来写测试用例），
#: 它依赖宿主的进程模型，跨语言宿主没有对应物，属**显式豁免**。
#: 豁免必须写在这里，不许让检测测试静默略过 —— 新增模块忘了做宿主时，
#: 报红才是对的（M50 第一批就是这么翻车的）。
HOST_EXEMPT: frozenset[str] = frozenset({"测试"})

NODE_OUT = ROOT / "node" / "stdlib_sigs.js"
RUST_OUT = ROOT / "rust" / "src" / "stdlib_sigs.rs"


def collect() -> dict[str, dict[str, list]]:
    """扫描 `jishi/stdlib/*.py`，返回 `{模块: {函数: [必填数, [参数名…]]}}`。"""
    from jishi import ai  # 延迟导入：保证 sys.path 已就绪

    pkg_dir = Path(ai._stdlib_dir())
    out: dict[str, dict[str, list]] = {}
    for fn in sorted(pkg_dir.glob("*.py")):
        if fn.name.startswith("_"):
            continue
        mod = fn.stem
        try:
            tree = ast.parse(fn.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - 源码坏了自然有别的测试报
            continue
        funcs: dict[str, list] = {}
        # 复用 ai._收集函数：它会往 `__基石新建__` 里再看一层
        # （日志 / 测试 把函数定义在那个工厂函数内部）。签名事实源必须
        # 与 `jishi --lang-spec` 完全一致，所以不另写一套 AST 遍历。
        for f in _collect_sigs(tree.body):
            funcs[f.name] = f.as_entry()
        if funcs:
            out[mod] = funcs
    return out


class _Sig:
    """一个公开函数的签名要点。"""

    __slots__ = ("name", "required", "params")

    def __init__(self, node: ast.FunctionDef) -> None:
        a = node.args
        # 位置参数 + 仅关键字参数，顺序即调用时的位置顺序
        names = [x.arg for x in a.args] + [x.arg for x in a.kwonlyargs]
        # 只统计**位置参数里的必填段**：宿主是按位置调用的，而「跳过前面的
        # 可选参数、只给后面的」不能靠补 None 糊弄（`None` 未必等价于「不给」），
        # 映射时会单独报错（见 vm.js / lib.rs）。
        n_req = len(a.args) - len(a.defaults)
        self.name = node.name
        self.required = n_req
        self.params = names

    def as_entry(self) -> list:
        return [self.required, self.params]


def _collect_sigs(nodes: list) -> list[_Sig]:
    """与 `ai._收集函数` 同规则：非下划线开头 + 钻进 `__基石新建__`。"""
    out: list[_Sig] = []
    for node in nodes:
        if not isinstance(node, ast.FunctionDef):
            continue
        # 顺序要紧：`__基石新建__` 也以下划线开头，必须先认它
        if node.name == "__基石新建__":
            out.extend(_collect_sigs(node.body))
        elif not node.name.startswith("_"):
            out.append(_Sig(node))
    return out


# ---------------------------------------------------------------------------
# 产出：Node
# ---------------------------------------------------------------------------

NODE_HEAD = """\
// -*- coding: utf-8 -*-
// 自动生成，请勿手改 —— 由 `python tools/gen_stdlib_sigs.py` 从
// jishi/stdlib/*.py 的公开函数签名生成。
//
// 用途：宿主收到**命名参数**时（`加密.摘要("abc", 算法="md5")`），按
// 「模块.函数」查这张表，把名字映射回位置参数 —— 五引擎因此给出同一结果。
// 以前宿主是**静默丢掉** kwargs 的（`算法="md5"` 会悄悄用默认的 sha256）。
//
// 键带模块前缀是必须的：全库有 19 组跨模块同名函数（`解析` 在 json / 参数 /
// 日期 都有），只按函数名查会歧义。
//
// 值 = [必填参数个数, [参数名…]]；参数名顺序即位置参数顺序。

'use strict';

module.exports = {
"""


def render_node(table: dict[str, dict[str, list]]) -> str:
    lines = [NODE_HEAD]
    for mod in sorted(table):
        lines.append(f"  {_js_str(mod)}: {{\n")
        for name in sorted(table[mod]):
            req, params = table[mod][name]
            plist = ", ".join(_js_str(p) for p in params)
            lines.append(f"    {_js_str(name)}: [{req}, [{plist}]],\n")
        lines.append("  },\n")
    lines.append("};\n")
    return "".join(lines)


def _js_str(s: str) -> str:
    """JS 字符串字面量。模块/参数名都是中文标识符，直接单引号即可。"""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


# ---------------------------------------------------------------------------
# 产出：Rust
# ---------------------------------------------------------------------------

RUST_HEAD = """\
//! 标准库函数签名表（**自动生成，请勿手改** —— `python tools/gen_stdlib_sigs.py`）。
//!
//! 用途：宿主收到**命名参数**时（`加密.摘要("abc", 算法="md5")`），按
//! 「模块.函数」查这张表把名字映射回位置参数 —— 五引擎因此给出同一结果。
//! 以前 `Val::Builtin` 只是把 kwargs 丢掉（`算法="md5"` 会悄悄用默认的 sha256）。
//!
//! 键带模块前缀是必须的：全库有 19 组跨模块同名函数（`解析` 在 json / 参数 /
//! 日期 都有），只按函数名查会歧义。
//!
//! 值 = (必填参数个数, 参数名切片)；参数名顺序即位置参数顺序。

/// 查「模块.函数」的签名。返回 `(必填参数个数, 参数名)`，没收录则 `None`
/// （没收录 = 纯 `*参数` / `**选项` 形式，或该函数不存在；两种情况宿主都会
/// 在有命名参数时报错，不会静默丢）。
pub(crate) fn sigof(qualified: &str) -> Option<(usize, &'static [&'static str])> {
    Some(match qualified {
"""


def render_rust(table: dict[str, dict[str, list]]) -> str:
    lines = [RUST_HEAD]
    for mod in sorted(table):
        for name in sorted(table[mod]):
            req, params = table[mod][name]
            plist = ", ".join('"' + p + '"' for p in params)
            lines.append(f'        "{mod}.{name}" => ({req}, &[{plist}]),\n')
    lines.append("        _ => return None,\n")
    lines.append("    })\n}\n")
    return "".join(lines)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    check = "--check" in argv
    table = collect()

    generated = {NODE_OUT: render_node(table), RUST_OUT: render_rust(table)}

    n_mod = len(table)
    n_fn = sum(len(v) for v in table.values())
    n_opt = sum(1 for m in table.values() for _, (req, p) in m.items() if req < len(p))

    if check:
        stale = []
        for path, text in generated.items():
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(path.relative_to(ROOT).as_posix())
        if stale:
            print("签名表与 jishi/stdlib/*.py 不同步：")
            for s in stale:
                print(f"  - {s}")
            print("跑 `python tools/gen_stdlib_sigs.py` 重新生成。")
            return 1
        print(f"标准库签名表已同步（{n_mod} 模块 / {n_fn} 函数，其中 {n_opt} 个带可选参数）")
        return 0

    for path, text in generated.items():
        path.write_text(text, encoding="utf-8")
        print(f"写出 {path.relative_to(ROOT).as_posix()}（{len(text.splitlines())} 行）")
    print(f"共 {n_mod} 模块 / {n_fn} 函数，其中 {n_opt} 个带可选参数可被命名调用")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
