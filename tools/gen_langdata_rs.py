# -*- coding: utf-8 -*-
"""把语言元数据从 Python 侧搬到 Rust **显式表**（R7.2）—— **一次性搬运工具**。

用法：`python tools/gen_langdata_rs.py`（跑完看 diff，然后 `cargo build`）。
**不是构建步骤** —— 产物 `rust/src/langdata.rs` 已签入，之后两侧由
`tests/test_m90_langspec_rust.py` 的漂移检测钉住（R7.2 拍板①）。

为什么用「生成」而不是「手写」：这份元数据里有 ~230 条标准库函数的**说明文本**、
30 条内建说明、关键字说明…… 手抄必错，而且改一次漏一次。生成一次、之后靠测试
盯「两侧一致」，与 `tools/gen_errcodes_rs.py` / `tools/gen_stdlib_sigs.py` 同一套路。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oracle.jishi.ai import build_lang_spec  # noqa: E402
from oracle.jishi.version import VERSION  # noqa: E402


def rs_str(s: str) -> str:
    """把一个 Python 字符串写成 Rust 字符串字面量（含转义）。"""
    out = ['"']
    for ch in s:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append("\\u{%x}" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def rs_strs(items, indent: str) -> str:
    """`&["a", "b"]`（一个列表字面量，超长自动换行）。"""
    one = "&[" + ", ".join(rs_str(x) for x in items) + "]"
    if len(one) <= 88:
        return one
    inner = (",\n" + indent + "     ").join(rs_str(x) for x in items)
    return "&[\n" + indent + "    " + inner + ",\n" + indent + "]"


def main() -> None:
    spec = build_lang_spec()
    L: list[str] = []
    add = L.append

    add("//! 语言元数据（`--lang-spec` / `--ai-card` 的事实源）—— **自动生成，请勿手改**。")
    add("//!")
    add("//! 生成器：`python tools/gen_langdata_rs.py`（**一次性搬运**，不是构建步骤；")
    add("//! 跑完看 diff）。产物签入后，由 `tests/test_m90_langspec_rust.py` 的**漂移")
    add("//! 检测**钉住「两侧一致」—— 这就是 R7.2 拍板①（Rust 侧显式表 + 两侧各")
    add("//! `--dump` 自报家底来比）。改 Python 侧元数据后**必须重跑生成器**，否则会红。")
    add("")
    add("/// 语言版本（与 `oracle/jishi/version.py` 的 `VERSION` 同步）。")
    add(f"pub const LANG_VERSION: &str = {rs_str(VERSION)};")
    add("")

    # ---- 关键字（有序：就是比较/查表的顺序） ----
    add("/// 关键字 → 中文说明（**有序**，与 `tokenizer.KEYWORDS` 的键顺序一致）。")
    add("pub const KEYWORDS: &[(&str, &str)] = &[")
    for k, v in spec["keywords"].items():
        add(f"    ({rs_str(k)}, {rs_str(v)}),")
    add("];")
    add("")

    # ---- 运算符 ----
    add("/// 二元运算符（**按优先级升序**；同级保持原表顺序）：(运算符, 优先级, 中文名)。")
    add("pub const BIN_OPS: &[(&str, i64, &str)] = &[")
    for it in spec["operators"]["binary"]:
        add(f"    ({rs_str(it['op'])}, {it['precedence']}, {rs_str(it['name'])}),")
    add("];")
    add("")
    add("/// 比较运算符（有序）。")
    add(f"pub const CMP_OPS: &[&str] = {rs_strs(spec['operators']['compare'], '')};")
    add("")
    add("/// 赋值运算符（已排序 —— set 的迭代顺序不稳定，别改成「原顺序」）。")
    add(f"pub const ASSIGN_OPS: &[&str] = {rs_strs(spec['operators']['assign'], '')};")
    add("")
    add("/// 一元运算符。")
    add(f"pub const UNARY_OPS: &[&str] = {rs_strs(spec['operators']['unary'], '')};")
    add("")

    # ---- 内建 ----
    add("/// 内建函数：(名字, 说明)，**按内建表注册顺序**（顺序进 `--ai-card`）。")
    add("pub const BUILTINS: &[(&str, &str)] = &[")
    for b in spec["builtins"]:
        add(f"    ({rs_str(b['name'])}, {rs_str(b['doc'])}),")
    add("];")
    add("")

    # ---- 方法 ----
    add("/// 对象方法：6 类 → 方法名（有序）。")
    add("pub const METHODS: &[(&str, &[&str])] = &[")
    for k, v in spec["methods"].items():
        add(f"    ({rs_str(k)}, {rs_strs(v, '    ')}),")
    add("];")
    add("")

    # ---- 标准库 ----
    add("/// 标准库：模块 → [(函数名, 签名, 说明)]（函数按**源码定义顺序**，非字母序）。")
    add("pub const STDLIB: &[(&str, &[(&str, &str, &str)])] = &[")
    for m in spec["stdlib"]:
        add(f"    ({rs_str(m['module'])}, &[")
        for f in m["functions"]:
            add(f"        ({rs_str(f['name'])}, {rs_str(f['sig'])}, {rs_str(f['doc'])}),")
        add("    ]),")
    add("];")
    add("")

    # ---- 异常 ----
    add("/// 异常类型（有序；`捕获 X 为 e` 用）。")
    add(f"pub const EXCEPTIONS: &[&str] = {rs_strs(spec['exceptions'], '')};")
    add("")

    # ---- 错误码 ----
    add("/// 错误码：(码, 类名, 标题, 是否执行器内部信号)。**按码升序**。")
    add("pub const ERROR_CODES: &[(&str, &str, &str, bool)] = &[")
    for e in spec["error_codes"]:
        add(f"    ({rs_str(e['code'])}, {rs_str(e['class'])}, {rs_str(e['title'])}, "
            f"{'true' if e['internal'] else 'false'}),")
    add("];")
    add("")

    # ---- 静态检查码 ----
    add("/// 静态检查问题码：(码, 级别, 名称, 说明)。")
    add("pub const CHECK_CODES: &[(&str, &str, &str, &str)] = &[")
    for c in spec["check_codes"]:
        add(f"    ({rs_str(c['code'])}, {rs_str(c['level'])}, {rs_str(c['name'])}, "
            f"{rs_str(c['doc'])}),")
    add("];")
    add("")

    # ---- 等价 Python 写法 ----
    eq = spec["python_equiv"]
    add("/// 等价 Python 写法：关键字（有序）。")
    add("pub const PY_KEYWORDS: &[(&str, &str)] = &[")
    for k, v in eq["keywords"].items():
        add(f"    ({rs_str(k)}, {rs_str(v)}),")
    add("];")
    add("")
    add("/// 等价 Python 写法：运算符（有序）。")
    add("pub const PY_OPERATORS: &[(&str, &str)] = &[")
    for k, v in eq["operators"].items():
        add(f"    ({rs_str(k)}, {rs_str(v)}),")
    add("];")
    add("")
    add("/// 等价 Python 写法：内建（有序）。")
    add("pub const PY_BUILTINS: &[(&str, &str)] = &[")
    for k, v in eq["builtins"].items():
        add(f"    ({rs_str(k)}, {rs_str(v)}),")
    add("];")
    add("")

    out = ROOT / "rust" / "src" / "langdata.rs"
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"已写出 {out}（{len(L)} 行）")


if __name__ == "__main__":
    main()
