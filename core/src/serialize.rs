//! 字节码的跨语言 JSON 序列化（R5）—— `jishi/serialize.py` 的 Rust 版。
//!
//! ⚠️ **这不是「差不多」就行的地方**：`compare --stage bytecode` 比的正是这段
//! JSON（连 `checksum` 一起），差一个字节就是红。所以这里手工拼 JSON 而不是
//! 用序列化库 —— 键序、转义、浮点写法都得与 Python 的 `json.dumps` 逐字相同。
//!
//! 三条容易翻车的：
//! 1. **键序**：Python 的 `json.dumps` 按 **dict 插入序** 输出，不是按字母序；
//! 2. **转义**：`ensure_ascii=False` —— 中文原样出去，控制字符才转义；
//! 3. **浮点**：JSON 里的数字文本 = `repr(float)`，而 Rust 的 `{}` 不写指数
//!    （`1e30` 会印成 30 个 0），所以 `py_repr_float` 得自己拼。

use crate::compiler::Module;
use crate::opcodes;
use crate::sha256;

/// 序列化格式版本。**与 `jishi/serialize.py` 的 `FORMAT_VERSION` 同步**，
/// 格式一变两边一起动，宿主据此拒绝不兼容产物。
pub const FORMAT_VERSION: i64 = 3;

/// JavaScript 的安全整数上界（2^53 - 1）：超出就写成十进制字符串，
/// 免得宿主侧 JSON 解析**静默丢精度**。
const JS_MAX_SAFE_INT: i64 = 9007199254740991;

// ---------------------------------------------------------------------------
// Python 的浮点 repr
// ---------------------------------------------------------------------------

/// `repr(float)` 的逐字复刻。
///
/// 规则（CPython `float_repr_style_short`）：先取**最短往返**的数字串，
/// 然后 **`decpt <= -4 || decpt > 16` 用科学计数法，否则定点**；
/// 定点且没有小数部分时补 `.0`；科学计数的指数**至少两位**（`e-05`）。
pub fn py_repr_float(f: f64) -> String {
    if f.is_nan() {
        return "nan".to_string();
    }
    if f.is_infinite() {
        return if f > 0.0 { "inf".into() } else { "-inf".into() };
    }
    // Rust 的 `{:e}` 给的就是最短往返表示（如 `1.2345678901234568e17`）
    let s = format!("{:e}", f);
    let (mant, exp) = s.split_once('e').expect("`{:e}` 一定有 e");
    let exp: i32 = exp.parse().expect("指数是整数");
    let (neg, mant) = match mant.strip_prefix('-') {
        Some(r) => (true, r),
        None => (false, mant),
    };
    let raw: String = mant.chars().filter(|c| *c != '.').collect();
    let trimmed = raw.trim_end_matches('0');
    let digits = if trimmed.is_empty() { "0" } else { trimmed };
    // 小数点的位置（digits[0] 在 10^exp，所以小数点在第 exp+1 位之前）
    let decpt = exp + 1;
    let body = if decpt <= -4 || decpt > 16 {
        let mut o = String::new();
        o.push_str(&digits[..1]);
        if digits.len() > 1 {
            o.push('.');
            o.push_str(&digits[1..]);
        }
        o.push('e');
        o.push(if exp < 0 { '-' } else { '+' });
        o.push_str(&format!("{:02}", exp.abs()));
        o
    } else if decpt <= 0 {
        format!("0.{}{}", "0".repeat((-decpt) as usize), digits)
    } else if decpt as usize >= digits.len() {
        format!("{}{}.0", digits, "0".repeat(decpt as usize - digits.len()))
    } else {
        let (a, b) = digits.split_at(decpt as usize);
        format!("{}.{}", a, b)
    };
    if neg { format!("-{}", body) } else { body }
}

/// JSON 里的浮点文本。与 `repr` 的区别只有三个特殊值
/// （`json.dumps` 默认 `allow_nan=True`，写的是 `NaN`/`Infinity`/`-Infinity`）。
fn json_float(f: f64) -> String {
    if f.is_nan() {
        return "NaN".to_string();
    }
    if f.is_infinite() {
        return if f > 0.0 { "Infinity".into() } else { "-Infinity".into() };
    }
    py_repr_float(f)
}

// ---------------------------------------------------------------------------
// JSON 拼装
// ---------------------------------------------------------------------------

/// 字符串 → JSON 字面量（`ensure_ascii=False` + 与 Python 相同的转义集）。
pub fn json_str(s: &str) -> String {
    let mut o = String::with_capacity(s.len() + 2);
    o.push('"');
    for c in s.chars() {
        match c {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            '\n' => o.push_str("\\n"),
            '\r' => o.push_str("\\r"),
            '\t' => o.push_str("\\t"),
            '\u{08}' => o.push_str("\\b"),
            '\u{0c}' => o.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                o.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => o.push(c),
        }
    }
    o.push('"');
    o
}

fn json_str_list(items: &[String]) -> String {
    let parts: Vec<String> = items.iter().map(|s| json_str(s)).collect();
    format!("[{}]", parts.join(","))
}

fn json_int_list(items: &[i64]) -> String {
    let parts: Vec<String> = items.iter().map(|v| v.to_string()).collect();
    format!("[{}]", parts.join(","))
}

/// `local_hints`（v3）：按槽位的报错提示 → JSON **对象**（键是槽位号）。
///
/// ⚠️ 与 Python 的 `serialize._payload_json` 逐字对应 —— 那边是
/// `sorted(code.local_hints.items())`（**按槽位排序**），这里也要排，
/// 否则同一份源码两个前端产出的 JSON 文本不同，一致性夹具立马报红。
fn json_local_hints(items: &[(i64, String)]) -> String {
    let mut sorted: Vec<&(i64, String)> = items.iter().collect();
    sorted.sort_by_key(|(k, _)| *k);
    let parts: Vec<String> = sorted
        .iter()
        .map(|(k, v)| format!("{}:{}", json_str(&k.to_string()), json_str(v)))
        .collect();
    format!("{{{}}}", parts.join(","))
}

/// 常量池里的一项 → JSON（与 `serialize._const_to_json` 逐字对应）。
fn const_json(c: &crate::compiler::Const) -> String {
    use crate::compiler::Const;
    match c {
        Const::None => "{\"t\":\"none\"}".to_string(),
        Const::Bool(true) => "{\"t\":\"true\"}".to_string(),
        Const::Bool(false) => "{\"t\":\"false\"}".to_string(),
        // 超出宿主安全整数的写成字符串（M32）
        Const::Int(i) => {
            if -JS_MAX_SAFE_INT <= *i && *i <= JS_MAX_SAFE_INT {
                format!("{{\"t\":\"int\",\"v\":{}}}", i)
            } else {
                format!("{{\"t\":\"int\",\"v\":\"{}\"}}", i)
            }
        }
        Const::Big(s) => format!("{{\"t\":\"int\",\"v\":{}}}", json_str(s)),
        Const::Float(f) => format!("{{\"t\":\"float\",\"v\":{}}}", json_float(*f)),
        Const::Str(s) => format!("{{\"t\":\"str\",\"v\":{}}}", json_str(s)),
    }
}

/// payload 的规范 JSON —— **序列化与校验和共用同一份**（同 `_payload_json`）。
pub fn payload_json(m: &Module) -> String {
    let consts: Vec<String> = m.consts.iter().map(const_json).collect();
    let mut codes: Vec<String> = Vec::with_capacity(m.codes.len());
    for c in &m.codes {
        let instrs: Vec<String> = c
            .instrs
            .iter()
            .flat_map(|i| {
                [i.op, i.a, i.b, i.c, i.line, i.col]
                    .iter()
                    .map(|v| v.to_string())
                    .collect::<Vec<_>>()
            })
            .collect();
        codes.push(format!(
            concat!(
                "{{\"name\":{},\"params\":{},\"param_idx\":{},\"param_local\":{},",
                "\"param_cell\":{},\"param_kind\":{},\"nlocals\":{},\"local_names\":{},",
                "\"cellvars\":{},\"freevars\":{},\"firstlineno\":{},\"local_hints\":{},",
                "\"instrs\":[{}]}}"
            ),
            json_str(&c.name),
            json_str_list(&c.params),
            json_int_list(&c.param_idx),
            json_int_list(&c.param_local),
            json_int_list(&c.param_cell),
            json_int_list(&c.param_kind),
            c.nlocals,
            json_str_list(&c.local_names),
            json_str_list(&c.cellvars),
            json_str_list(&c.freevars),
            c.firstlineno,
            json_local_hints(&c.local_hints),
            instrs.join(","),
        ));
    }
    let kw: Vec<String> = m.kw_names.iter().map(|v| json_str_list(v)).collect();
    format!(
        concat!(
            "{{\"consts\":[{}],\"names\":{},\"kw_names\":[{}],\"codes\":[{}],",
            "\"main\":{},\"filename\":{}}}"
        ),
        consts.join(","),
        json_str_list(&m.names),
        kw.join(","),
        codes.join(","),
        m.main,
        json_str(&m.filename),
    )
}

/// 完整产物 JSON（`serialize.dump_json` 的等价物）。
pub fn dump_json(m: &Module) -> String {
    let payload = payload_json(m);
    let sum = sha256::hex(payload.as_bytes());
    let op_names: Vec<String> = opcodes::OP_NAMES.iter().map(|s| json_str(s)).collect();
    format!(
        "{{\"format\":\"jishi-bytecode\",\"version\":{},\"opcode_names\":[{}],\
         \"checksum\":{},\"payload\":{}}}",
        FORMAT_VERSION,
        op_names.join(","),
        json_str(&sum),
        payload,
    )
}
