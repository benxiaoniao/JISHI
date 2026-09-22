//! 极简 JSON 解析器（零依赖，M13.3）。
//!
//! 只解析基石字节码格式（M13.1）需要的数据类型：
//! 对象 / 数组 / 字符串 / 整数 / 浮点 / 布尔 / null。
//! 不支持数字里的科学计数法等花哨写法（字节码格式用不到）。

use std::collections::BTreeMap;

#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Int(i128),   // 大整数（M32）：i64 装不下 Python 的任意精度 int 字面量
    Float(f64),
    Str(String),
    Arr(Vec<Json>),
    Obj(BTreeMap<String, Json>),
}

impl Json {
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Obj(m) => m.get(key),
            _ => None,
        }
    }
    /// 取 128 位整数（大整数常量用，见 M32）。
    pub fn as_i128(&self) -> Option<i128> {
        match self {
            Json::Int(i) => Some(*i),
            _ => None,
        }
    }
    /// 取 64 位整数（指令流里的操作数都是小整数，装得下）。
    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Json::Int(i) => i64::try_from(*i).ok(),
            _ => None,
        }
    }
    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Json::Float(f) => Some(*f),
            Json::Int(i) => Some(*i as f64),
            _ => None,
        }
    }
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }
    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Json::Bool(b) => Some(*b),
            _ => None,
        }
    }
    pub fn as_array(&self) -> Option<&Vec<Json>> {
        match self {
            Json::Arr(a) => Some(a),
            _ => None,
        }
    }
}

pub fn parse(text: &str) -> Result<Json, String> {
    let mut p = Parser { bytes: text.as_bytes(), pos: 0 };
    let v = p.parse_value()?;
    p.skip_ws();
    if p.pos != p.bytes.len() {
        return Err(format!("第 {} 字节处有多余内容", p.pos));
    }
    Ok(v)
}

struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Parser<'a> {
    fn skip_ws(&mut self) {
        while self.pos < self.bytes.len() {
            match self.bytes[self.pos] {
                b' ' | b'\t' | b'\n' | b'\r' => self.pos += 1,
                _ => break,
            }
        }
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn parse_value(&mut self) -> Result<Json, String> {
        self.skip_ws();
        match self.peek() {
            Some(b'{') => self.parse_object(),
            Some(b'[') => self.parse_array(),
            Some(b'"') => self.parse_string().map(Json::Str),
            Some(b't') => self.parse_literal("true", Json::Bool(true)),
            Some(b'f') => self.parse_literal("false", Json::Bool(false)),
            Some(b'n') => self.parse_literal("null", Json::Null),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.parse_number(),
            Some(c) => Err(format!("第 {} 字节处有非法字符 {}", self.pos, c as char)),
            None => Err("意外到达输入末尾".to_string()),
        }
    }

    fn parse_literal(&mut self, lit: &str, val: Json) -> Result<Json, String> {
        if self.bytes[self.pos..].starts_with(lit.as_bytes()) {
            self.pos += lit.len();
            Ok(val)
        } else {
            Err(format!("第 {} 字节处解析失败", self.pos))
        }
    }

    fn parse_object(&mut self) -> Result<Json, String> {
        self.pos += 1; // {
        let mut map = BTreeMap::new();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(Json::Obj(map));
        }
        loop {
            self.skip_ws();
            let key = self.parse_string()?;
            self.skip_ws();
            if self.peek() != Some(b':') {
                return Err(format!("第 {} 字节处缺冒号", self.pos));
            }
            self.pos += 1;
            let val = self.parse_value()?;
            map.insert(key, val);
            self.skip_ws();
            match self.peek() {
                Some(b',') => { self.pos += 1; }
                Some(b'}') => { self.pos += 1; return Ok(Json::Obj(map)); }
                _ => return Err(format!("第 {} 字节处对象未闭合", self.pos)),
            }
        }
    }

    fn parse_array(&mut self) -> Result<Json, String> {
        self.pos += 1; // [
        let mut arr = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(Json::Arr(arr));
        }
        loop {
            let val = self.parse_value()?;
            arr.push(val);
            self.skip_ws();
            match self.peek() {
                Some(b',') => { self.pos += 1; }
                Some(b']') => { self.pos += 1; return Ok(Json::Arr(arr)); }
                _ => return Err(format!("第 {} 字节处数组未闭合", self.pos)),
            }
        }
    }

    fn parse_string(&mut self) -> Result<String, String> {
        self.pos += 1; // "
        let mut out = String::new();
        loop {
            let c = match self.peek() {
                Some(c) => c,
                None => return Err("字符串未闭合".to_string()),
            };
            match c {
                b'"' => { self.pos += 1; return Ok(out); }
                b'\\' => {
                    self.pos += 1;
                    let esc = self.peek().ok_or("转义未完成")?;
                    self.pos += 1;
                    match esc {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'n' => out.push('\n'),
                        b't' => out.push('\t'),
                        b'r' => out.push('\r'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'u' => {
                            // \uXXXX
                            let hex = &self.bytes[self.pos..self.pos + 4];
                            self.pos += 4;
                            let code = u32::from_str_radix(std::str::from_utf8(hex).unwrap_or(""), 16)
                                .map_err(|_| "非法 unicode 转义".to_string())?;
                            if let Some(c) = char::from_u32(code) {
                                out.push(c);
                            } else {
                                return Err("非法 unicode 码点".to_string());
                            }
                        }
                        _ => return Err("非法转义字符".to_string()),
                    }
                }
                _ => {
                    // UTF-8 多字节
                    let len = utf8_len(c);
                    let end = self.pos + len;
                    if end > self.bytes.len() {
                        return Err("字符串 UTF-8 截断".to_string());
                    }
                    let s = std::str::from_utf8(&self.bytes[self.pos..end])
                        .map_err(|_| "非法 UTF-8".to_string())?;
                    out.push_str(s);
                    self.pos = end;
                }
            }
        }
    }

    fn parse_number(&mut self) -> Result<Json, String> {
        let start = self.pos;
        if self.peek() == Some(b'-') { self.pos += 1; }
        let mut is_float = false;
        let mut seen_e = false;
        while let Some(c) = self.peek() {
            if c.is_ascii_digit() { self.pos += 1; }
            else if c == b'.' && !is_float { is_float = true; self.pos += 1; }
            // 注意 `1e+20` **没有小数点**，所以这里不能要求先见过 '.'
            else if (c == b'e' || c == b'E') && !seen_e {
                seen_e = true;
                // 科学计数法（M33 修）：Python 的 json.dumps 写浮点用的是
                // `repr`，`1e20` 会写成 `1e+20`、`1e-7` 写成 `1e-07`。
                // 以前这里不认 `e`，于是**带这类浮点常量的程序直接加载失败**
                // （报「对象未闭合」这种看不懂的错），不只是 `json` 模块的事。
                is_float = true;
                self.pos += 1;
                if matches!(self.peek(), Some(b'+') | Some(b'-')) { self.pos += 1; }
            }
            else { break; }
        }
        let s = std::str::from_utf8(&self.bytes[start..self.pos]).map_err(|_| "非法数字".to_string())?;
        if is_float {
            s.parse::<f64>().map(Json::Float).map_err(|_| "非法浮点".to_string())
        } else {
            // 用 i128：字节码里的整数常量可能超出 i64（M32）
            s.parse::<i128>().map(Json::Int).map_err(|_| "非法整数".to_string())
        }
    }
}

fn utf8_len(b: u8) -> usize {
    if b < 0x80 { 1 }
    else if b >> 5 == 0b110 { 2 }
    else if b >> 4 == 0b1110 { 3 }
    else if b >> 3 == 0b11110 { 4 }
    else { 1 }
}
