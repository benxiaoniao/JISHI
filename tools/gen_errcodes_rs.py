# -*- coding: utf-8 -*-
"""把错误码的两张表从 Python **搬运**成 Rust 显式表（R7.1-b，一次性工具）。

用法：`python tools/gen_errcodes_rs.py`

⚠️ **这不是构建步骤** —— 表已经落在 `rust/src/errcodes.rs` 里并**签入仓库**，
两侧由 `tests/test_m85_errcodes_rust.py` 钉住一致（谁改一侧都报红）。
只有在**故意要同步一大批文案**（改口径、批量润色）时才跑它，
**跑完务必看 `git diff`** —— 它会直接覆盖那份 Rust 文件。

两张表各有单一来源（照 Python 侧既有约定，别合并）：

* `(码, 标题)` ← **`oracle/jishi/errors.py`** 里各 `JishiError` 子类的类属性
  （`cli._all_error_codes()` 也是这么抽的）；
* `码 → 什么意思 / 常见成因 / 怎么改` ← `oracle/jishi/errcodes.py` 的 `INFO`。

Rust 侧是**零依赖** crate（自己拼字符串，不用 serde），所以渲染逻辑也一起写在
生成的文件里，且**逐字照 Python 侧 `cli._cmd_errcode`**。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oracle.jishi import errcodes as EC            # noqa: E402
from oracle.jishi import errors as E               # noqa: E402

OUT = ROOT / "rust" / "src" / "errcodes.rs"


def all_codes() -> list[tuple[str, str]]:
    """与 `cli._all_error_codes()` 同一套抽法：从 `errors.py` 的类属性抽。"""
    out: dict[str, str] = {}
    for obj in vars(E).values():
        if (inspect.isclass(obj) and issubclass(obj, E.JishiError)
                and obj is not E.JishiError):
            out.setdefault(obj.code, obj.title)
    return sorted(out.items())


def rs_str(s: str) -> str:
    """Python 文本 → Rust 字符串字面量（转义 `\\` 与 `"`；中文原样）。"""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


#: 文件头的注释 + `render` / `dump_json` / 两个查表函数。用**原始字符串**写，
#: 这样里面的 `\n`（Rust 字符串转义）原样落地、不被 Python 先解释掉。
TEMPLATE = r'''// 基石错误码表（R7.1-b，2026-10-05）—— `jishi 错误 [码]` 查「什么意思 /
// 常见成因 / 怎么改」。
//
// ⚠️ **本文件是「显式表」，不是构建产物**：内容由
// `tools/gen_errcodes_rs.py` **一次性搬运**自 Python 侧，之后签入仓库；
// 两侧一致性由 `tests/test_m85_errcodes_rust.py` 钉住（谁改一侧都报红）。
//
// 两张表各有单一来源（与 Python 侧同一条约定，别合并）：
//   * `(码, 标题)` ← `oracle/jishi/errors.py` 各 `JishiError` 子类的类属性；
//   * `码 → 说明`   ← `oracle/jishi/errcodes.py` 的 `INFO`。
//
// 📌 为什么值得搬（北极星第二半「出问题能快速定位」）：错误码在报错里已经给了，
// 但「E0301 是什么、我该怎么办」以前只能翻源码 —— 对用户是门槛，对模型是
// 「猜一个修法」。

/// 分段：只用于「总览」的排版（码本身按数字排序）。`(前缀, 名字, 一句话)`。
pub const SECTIONS: &[(&str, &str, &str)] = &[
__SECTIONS__];

/// `(码, 标题)` —— 按码排序（与 Python 侧 `cli._all_error_codes()` 同序）。
pub const CODES: &[(&str, &str)] = &[
__CODES__];

/// 一个码的说明。
pub struct Info {
    pub what: &'static str,
    pub why: &'static [&'static str],
    pub fix: &'static str,
}

/// `码 → 说明`。**顺序与 Python 侧 `errcodes.INFO` 一致**（便于核对）。
pub const INFO: &[(&str, Info)] = &[
__INFO__];

/// 按码查标题（`None` = 没有这个码）。
pub fn title(code: &str) -> Option<&'static str> {
    CODES.iter().find(|(c, _)| *c == code).map(|(_, t)| *t)
}

/// 按码查说明。
pub fn info(code: &str) -> Option<&'static Info> {
    INFO.iter().find(|(c, _)| *c == code).map(|(_, i)| i)
}

/// `jishi 错误 [码]` 的全部输出逻辑（**逐字照 Python 侧 `cli._cmd_errcode`**）。
///
/// 返回 `(stdout, stderr, 退出码)`：分开返回是因为 Python 那边
/// 「正常输出走 stdout、『没有这个码』走 stderr 且退码 2」。
pub fn render(want: &str) -> (String, String, i32) {
    let raw = want.trim().to_uppercase();

    if raw.is_empty() {
        let mut s = format!("基石错误码总览（{} 个）\n\n", CODES.len());
        for (prefix, name, desc) in SECTIONS {
            let mut n = 0;
            for (code, title) in CODES {
                if !code.starts_with(*prefix) {
                    continue;
                }
                if n == 0 {
                    s.push_str(&format!("【{prefix}xx · {name}】{desc}\n"));
                }
                n += 1;
                s.push_str(&format!("  {code}  {title}\n"));
            }
            if n > 0 {
                s.push('\n');
            }
        }
        s.push_str("看某个码的详情：jishi 错误 E0301\n");
        s.push_str("（报错里给的那串 `E0301` 就是可以直接贴进来的码）\n");
        return (s, String::new(), 0);
    }

    // 允许只写数字（用户从报错里抄的时候常常只抄了 0301）
    let mut want = if raw.starts_with('E') { raw } else { format!("E{raw}") };
    let digits = want[1..].to_string();
    if !digits.is_empty() && digits.chars().all(|c| c.is_ascii_digit())
        && want.len() < 5
    {
        want = format!("E{digits:0>4}");
    }

    let title = match title(&want) {
        Some(t) => t,
        None => {
            return (
                String::new(),
                format!("没有错误码「{want}」—— 跑 `jishi 错误` 看全部 {} 个。\n",
                        CODES.len()),
                2,
            );
        }
    };
    let mut s = format!("{want}  {title}\n\n");
    match info(&want) {
        None => s.push_str("（这个码还没有写说明 —— 请补 oracle/jishi/errcodes.py）\n"),
        Some(i) => {
            s.push_str("什么意思\n");
            s.push_str(&format!("  {}\n\n", i.what));
            s.push_str("常见成因\n");
            for reason in i.why {
                s.push_str(&format!("  · {reason}\n"));
            }
            s.push('\n');
            s.push_str("怎么改\n");
            s.push_str(&format!("  {}\n", i.fix));
        }
    }
    (s, String::new(), 0)
}

/// 自报家底（给漂移检测用）：把两张表导出成 JSON 文本。
///
/// ⚠️ 手拼 JSON 是**故意的**：`rust/` 是零依赖 crate，不引 serde。
/// 结构与 Python 侧 `tools/gen_errcodes_rs.py --dump` 一致。
pub fn dump_json() -> String {
    let mut s = String::from("{\"sections\":[");
    for (i, (p, n, d)) in SECTIONS.iter().enumerate() {
        if i > 0 {
            s.push(',');
        }
        s.push('[');
        s.push_str(&jstr(p));
        s.push(',');
        s.push_str(&jstr(n));
        s.push(',');
        s.push_str(&jstr(d));
        s.push(']');
    }
    s.push_str("],\"codes\":[");
    for (i, (c, t)) in CODES.iter().enumerate() {
        if i > 0 {
            s.push(',');
        }
        s.push('[');
        s.push_str(&jstr(c));
        s.push(',');
        s.push_str(&jstr(t));
        s.push(']');
    }
    s.push_str("],\"info\":[");
    for (i, (c, inf)) in INFO.iter().enumerate() {
        if i > 0 {
            s.push(',');
        }
        s.push('[');
        s.push_str(&jstr(c));
        s.push_str(",{\"what\":");
        s.push_str(&jstr(inf.what));
        s.push_str(",\"why\":[");
        for (k, r) in inf.why.iter().enumerate() {
            if k > 0 {
                s.push(',');
            }
            s.push_str(&jstr(r));
        }
        s.push_str("],\"fix\":");
        s.push_str(&jstr(inf.fix));
        s.push_str("}]");
    }
    s.push_str("]}");
    s
}

/// JSON 字符串转义（够用即可：控制字符走 \u）。
fn jstr(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}
'''


def main() -> int:
    codes = all_codes()
    info = EC.INFO

    sections = "".join(
        f"    ({rs_str(p)}, {rs_str(n)}, {rs_str(d)}),\n"
        for p, n, d in EC.SECTIONS)
    codes_src = "".join(f"    ({rs_str(c)}, {rs_str(t)}),\n" for c, t in codes)
    info_src = []
    for code, d in info.items():
        info_src.append(f"    ({rs_str(code)}, Info {{\n")
        info_src.append(f"        what: {rs_str(d['what'])},\n")
        info_src.append("        why: &[\n")
        info_src.extend(f"            {rs_str(r)},\n" for r in d["why"])
        info_src.append("        ],\n")
        info_src.append(f"        fix: {rs_str(d['fix'])},\n")
        info_src.append("    }),\n")

    text = (TEMPLATE
            .replace("__SECTIONS__", sections)
            .replace("__CODES__", codes_src)
            .replace("__INFO__", "".join(info_src)))
    OUT.write_text(text, encoding="utf-8")
    print(f"已写出 {OUT.relative_to(ROOT)}："
          f"{len(codes)} 个码 / {len(info)} 条说明 / {len(EC.SECTIONS)} 个分段")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
