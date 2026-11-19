//! 极小的 JSON 写出器（**保序**）—— `--lang-spec` 与 LSP 共用（R7.2 / R7.3）。
//!
//! 为什么自己写而不是引一个库：外部 crate 的排版**不受控**，而这里两处消费方
//! 都要求**逐字节**对齐 Python 的 `json.dumps`：
//!
//! | 用法 | 对应 Python | 谁用 |
//! |---|---|---|
//! | [`dump_compact`] | `json.dumps(o, ensure_ascii=False)` | LSP 报文（`_write`） |
//! | [`dump_indent`] | `json.dumps(o, ensure_ascii=False, indent=2)` | `--lang-spec` |
//! | [`dump_compact_sorted`] | `json.dumps(o, ensure_ascii=False, sort_keys=True)` | `content_hash` |
//!
//! ⚠️ **键顺序是内容的一部分**：Python 的 `dict` 保序，`json.dumps` 照插入顺序
//! 输出 —— 所以 [`Jv::Obj`] 用的是 `Vec`（不是 `HashMap`），拼的时候按需要的
//! 次序 push。`sort_keys` 那一份才在**每一层**现排。

/// 保序 JSON 值。
#[derive(Clone)]
pub enum Jv {
    Null,
    Str(String),
    Int(i64),
    /// 超 `i64` 的整数（`eval_expr` 求 `2 ** 100` 这种）。
    /// 单独一个变体而不是把 `Int` 改成 `i128`：`Int` 的构造点有几十处，
    /// 而需要更宽的地方**只有沙箱的值序列化**。
    Int128(i128),
    /// 浮点（`eval_expr` 的值可能是小数）。渲染对齐 Python 的
    /// `json.dumps(2.0)` → `2.0`（用 `frontend::serialize::py_repr_float`）。
    Float(f64),
    Bool(bool),
    Arr(Vec<Jv>),
    Obj(Vec<(String, Jv)>),
}

/// 浮点的 JSON 文本 —— Python 的 `json.dumps` 用的是 `float.__repr__`，
/// 所以这里复用前端那份（`serialize::py_repr_float`），**只留一份实现**。
fn py_float(f: f64) -> String {
    jishi_frontend::serialize::py_repr_float(f)
}

/// JSON 字符串字面量 —— 与 Python `json.dumps(..., ensure_ascii=False)` 同口径：
/// 只转义 `"` `\` 与 C0 控制字符，非 ASCII **原样**输出。
pub fn esc(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 2);
    out.push('"');
    for ch in text.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// `json.dumps(v, ensure_ascii=False, indent=2)` —— 空容器写成 `[]` / `{}`。
pub fn dump_indent(v: &Jv, level: usize, out: &mut String) {
    match v {
        Jv::Null => out.push_str("null"),
        Jv::Str(x) => out.push_str(&esc(x)),
        Jv::Int(i) => out.push_str(&i.to_string()),
        Jv::Int128(i) => out.push_str(&i.to_string()),
        Jv::Float(f) => out.push_str(&py_float(*f)),
        Jv::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Jv::Arr(items) => {
            if items.is_empty() {
                out.push_str("[]");
                return;
            }
            let pad = "  ".repeat(level + 1);
            out.push_str("[\n");
            for (i, it) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(",\n");
                }
                out.push_str(&pad);
                dump_indent(it, level + 1, out);
            }
            out.push('\n');
            out.push_str(&"  ".repeat(level));
            out.push(']');
        }
        Jv::Obj(kv) => {
            if kv.is_empty() {
                out.push_str("{}");
                return;
            }
            let pad = "  ".repeat(level + 1);
            out.push_str("{\n");
            for (i, (k, val)) in kv.iter().enumerate() {
                if i > 0 {
                    out.push_str(",\n");
                }
                out.push_str(&pad);
                out.push_str(&esc(k));
                out.push_str(": ");
                dump_indent(val, level + 1, out);
            }
            out.push('\n');
            out.push_str(&"  ".repeat(level));
            out.push('}');
        }
    }
}

/// `json.dumps(v, ensure_ascii=False, sort_keys=True)` —— 默认分隔符 `", "` / `": "`。
/// （`sort_keys` 是**每一层**都排 —— `content_hash` 靠它才稳定。）
pub fn dump_compact_sorted(v: &Jv, out: &mut String) {
    match v {
        Jv::Null => out.push_str("null"),
        Jv::Str(x) => out.push_str(&esc(x)),
        Jv::Int(i) => out.push_str(&i.to_string()),
        Jv::Int128(i) => out.push_str(&i.to_string()),
        Jv::Float(f) => out.push_str(&py_float(*f)),
        Jv::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Jv::Arr(items) => {
            out.push('[');
            for (i, it) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                dump_compact_sorted(it, out);
            }
            out.push(']');
        }
        Jv::Obj(kv) => {
            let mut ks: Vec<&(String, Jv)> = kv.iter().collect();
            ks.sort_by(|a, b| a.0.cmp(&b.0));
            out.push('{');
            for (i, (k, val)) in ks.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                out.push_str(&esc(k));
                out.push_str(": ");
                dump_compact_sorted(val, out);
            }
            out.push('}');
        }
    }
}

/// `json.dumps(v, ensure_ascii=False)` —— 默认分隔符 `", "` / `": "`，**保序**。
/// （LSP 报文就是这一种。）
pub fn dump_compact(v: &Jv) -> String {
    match v {
        Jv::Null => "null".to_string(),
        Jv::Str(x) => esc(x),
        Jv::Int(i) => i.to_string(),
        Jv::Int128(i) => i.to_string(),
        Jv::Float(f) => py_float(*f),
        Jv::Bool(b) => (if *b { "true" } else { "false" }).to_string(),
        Jv::Arr(items) => {
            let mut out = String::from("[");
            for (i, it) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                out.push_str(&dump_compact(it));
            }
            out.push(']');
            out
        }
        Jv::Obj(kv) => {
            let mut out = String::from("{");
            for (i, (k, val)) in kv.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                out.push_str(&esc(k));
                out.push_str(": ");
                out.push_str(&dump_compact(val));
            }
            out.push('}');
            out
        }
    }
}
