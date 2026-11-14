//! 标准库 `PDF`（M195）。与 Python 侧 `oracle/jishi/stdlib/PDF.py` 同行为、
//! 同字节输出。
//!
//! PDF 不是 zip 包，而是**一串对象 + 一张交叉引用表（xref）**。所以写出去的要害
//! 不是拼 XML，而是**把每个字节数准**：`xref` 里存的是每个对象在文件里的字节
//! 偏移，差一个字节，严格的阅读器就说文件坏了。
//!
//! ## 坐标
//!
//! PDF 规范原点在左下角、y 向上；屏幕与 `图形` 模块都是左上角、y 向下。
//! 这里**对外统一用屏幕坐标**，内部 `y` 翻成 `页高 - y`。
//!
//! ## 为什么只能写英文
//!
//! 只用 PDF 自带的 14 种标准字体（不嵌进文件、任何阅读器都认得），代价是
//! 字符串只能放 Latin-1 里的字。中文要嵌字体子集（文件 +1 MB + OFL 声明），
//! 这一版不做 ⇒ `写文本` 遇到 ASCII 以外的字符**直接报错**并让用户改用 `Word`。
//!
//! ## 存档格式（三宿主必须逐字节一致）
//!
//! 内容流**不压缩**（`stored`）：deflate 的输出在不同 zlib 版本上可能不同字节，
//! 不压缩才能保证判据可复现。

use crate::BuiltinHost;
use std::collections::HashMap;
use std::rc::Rc;

use super::{as_text, need_range, table, R};
use crate::{display, err, err_with_hint, Val};

const A4宽: f64 = 595.0;
const A4高: f64 = 842.0;

const 字体族们: [&str; 3] = ["Helvetica", "Times", "Courier"];
/// 索引 = `粗体 * 2 + 斜体`，正好对应 PDF 的四种后缀。
const 字体表: [[&str; 4]; 3] = [
    ["Helvetica", "Helvetica-Oblique", "Helvetica-Bold", "Helvetica-BoldOblique"],
    ["Times-Roman", "Times-Italic", "Times-Bold", "Times-BoldItalic"],
    ["Courier", "Courier-Oblique", "Courier-Bold", "Courier-BoldOblique"],
];

fn 字(s: &str) -> Val {
    Val::Str(s.into())
}

fn 取字典(v: &Val, 键: &str) -> Option<Val> {
    if let Val::Dict(d) = v {
        for (k, val) in d.borrow().iter() {
            if as_text(k) == 键 {
                return Some(val.clone());
            }
        }
    }
    None
}

fn 到数(v: &Val) -> Option<f64> {
    match v {
        Val::Int(i) => Some(*i as f64),
        Val::Float(f) => Some(*f),
        _ => None,
    }
}

fn 真值(v: &Val) -> bool {
    matches!(v, Val::Bool(true))
}

/// 文档的三件套：宽、高、页列表（页列表是共享的可变引用，写完还要原地改）。
fn 文档部件(v: &Val) -> Result<(f64, f64, Rc<std::cell::RefCell<Vec<Val>>>), crate::JishiError> {
    if let Val::Dict(d) = v {
        let 宽 = 取字典(v, "宽").as_ref().and_then(到数);
        let 高 = 取字典(v, "高").as_ref().and_then(到数);
        let 页们 = match 取字典(v, "页们") {
            Some(Val::List(l)) => Some(l),
            _ => None,
        };
        drop(d);
        if let (Some(宽), Some(高), Some(页们)) = (宽, 高, 页们) {
            return Ok((宽, 高, 页们));
        }
    }
    Err(err_with_hint(
        "类型错误",
        format!(
            "这里要一个 PDF 文档（`PDF.新建()` 的返回值），给过来的却是「{}」",
            display(v)
        ),
        "先 令 文 = PDF.新建()，再往它里面写".into(),
    ))
}

/// 当前页（最后一页）的「内容」列表。
fn 当前内容(v: &Val) -> Result<Rc<std::cell::RefCell<Vec<Val>>>, crate::JishiError> {
    let (_, _, 页们) = 文档部件(v)?;
    let 末 = 页们.borrow().last().cloned();
    let 末 = match 末 {
        Some(x) => x,
        None => return Err(err("值错误", "文档里一页都没有")),
    };
    match 取字典(&末, "内容") {
        Some(Val::List(l)) => Ok(l),
        _ => Err(err("值错误", "这一页没有「内容」")),
    }
}

fn 要数(v: &Val, 谁: &str, 名: &str) -> Result<f64, crate::JishiError> {
    match v {
        Val::Int(i) => Ok(*i as f64),
        Val::Float(f) => {
            if !f.is_finite() {
                return Err(err("值错误", format!("「{谁}」的 {名} 不能是 NaN 或无穷")));
            }
            Ok(*f)
        }
        other => Err(err(
            "类型错误",
            format!("「{谁}」的 {名} 要给一个数字，给的是「{}」", display(other)),
        )),
    }
}

/// `[红, 绿, 蓝]`，每个 **0–255**（不用 0–1：`[1,0,0]` 在两种口径下差太远）。
fn 要颜色(v: Option<&Val>, 谁: &str) -> Result<(f64, f64, f64), crate::JishiError> {
    let v = match v {
        None | Some(Val::None_) => return Ok((0.0, 0.0, 0.0)),
        Some(x) => x,
    };
    let 项 = match v {
        Val::List(l) => l.borrow().clone(),
        _ => {
            return Err(err(
                "类型错误",
                format!(
                    "「{谁}」的「颜色」要给三个数 [红, 绿, 蓝]（每个 0–255），现在是「{}」",
                    display(v)
                ),
            ))
        }
    };
    if 项.len() != 3 {
        return Err(err(
            "类型错误",
            format!("「{谁}」的「颜色」要给三个数，给的是 {} 个", 项.len()),
        ));
    }
    let mut 出 = [0.0f64; 3];
    for (i, x) in 项.iter().enumerate() {
        let f = match x {
            Val::Int(v) => *v as f64,
            Val::Float(v) => *v,
            other => {
                return Err(err(
                    "类型错误",
                    format!("「{谁}」的颜色分量要是数字，给的是「{}」", display(other)),
                ))
            }
        };
        if !(0.0..=255.0).contains(&f) {
            return Err(err(
                "值错误",
                format!("「{谁}」的颜色分量要在 0–255 之间，现在是 {}", display(x)),
            ));
        }
        出[i] = f / 255.0;
    }
    Ok((出[0], 出[1], 出[2]))
}

fn 字体名(选项: &Val, 谁: &str) -> Result<&'static str, crate::JishiError> {
    let 原名 = 取字典(选项, "字体")
        .map(|v| as_text(&v))
        .unwrap_or_else(|| "Helvetica".to_string());
    let 名 = match 原名.as_str() {
        "无衬线" | "黑体" => "Helvetica".to_string(),
        "衬线" | "宋体" => "Times".to_string(),
        "等宽" | "打字机" => "Courier".to_string(),
        _ => 原名.clone(),
    };
    let 粗 = 取字典(选项, "粗体").map(|v| 真值(&v)).unwrap_or(false);
    let 斜 = 取字典(选项, "斜体").map(|v| 真值(&v)).unwrap_or(false);
    for (i, 族) in 字体族们.iter().enumerate() {
        if 族.eq_ignore_ascii_case(&名) {
            return Ok(字体表[i][(粗 as usize) * 2 + (斜 as usize)]);
        }
    }
    Err(err_with_hint(
        "值错误",
        format!(
            "「{谁}」不认识的字体「{名}」—— 只能是：Helvetica / Times / Courier\
             （也可以写 无衬线 / 衬线 / 等宽）"
        ),
        "PDF 自带的标准字体只有这几种；要别的字体得嵌进文件，这一版不做".into(),
    ))
}

/// 标准字体只装得下 ASCII 可打印字符（PDF 字符串是单字节的）。
fn 查ASCII(文本: &str, 谁: &str) -> Result<(), crate::JishiError> {
    for (序号, c) in 文本.chars().enumerate() {
        if c == '\n' {
            continue;
        }
        if c == '\t' {
            return Err(err(
                "值错误",
                format!(
                    "「{谁}」的文本里有制表符（第 {} 个字符）—— PDF 不会自动对齐，请换成空格",
                    序号 + 1
                ),
            ));
        }
        let o = c as u32;
        if o < 0x20 {
            return Err(err(
                "值错误",
                format!("「{谁}」的文本里有控制字符 U+{o:04X}（第 {} 个字符）", 序号 + 1),
            ));
        }
        if o > 0x7E {
            return Err(err_with_hint(
                "值错误",
                format!(
                    "「{谁}」只写得下英文和常见符号：第 {} 个字符是「{c}」（U+{o:04X}），\
                     PDF 的 14 种标准字体里没有它",
                    序号 + 1
                ),
                "要中文报告请用 Word（`Word.加段落`），再由 Word / LibreOffice 导出 PDF"
                    .into(),
            ));
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// 写
// ---------------------------------------------------------------------------

/// 数字 → PDF 里的写法：整数不带小数点，小数最多四位（去掉尾零）。
///
/// 📌 三宿主逐字节一致，所以格式规则写死：先 `round(4)` 再打印。
fn 数(v: f64) -> String {
    let r = (v * 10000.0).round() / 10000.0;
    if r == r.trunc() && r.abs() < 1e15 {
        return format!("{}", r as i64);
    }
    let s = format!("{:.4}", r);
    s.trim_end_matches('0').trim_end_matches('.').to_string()
}

fn 串(s: &str) -> String {
    let mut o = String::from("(");
    for c in s.chars() {
        if c == '(' || c == ')' || c == '\\' {
            o.push('\\');
        }
        o.push(c);
    }
    o.push(')');
    o
}

/// 读**已经存好的**颜色（0–1 的 f64 三元组）。
///
/// 🔴 不能拿 `要颜色` 去读它 —— 那个是按 0–255 输入的，会把 0.78 再除一次 255
/// 变成 0.003（第一次写出来就是这样，两个宿主的字节对不上才发现）。
fn 读颜色(操作: &Val, 键: &str) -> Option<(f64, f64, f64)> {
    let 项 = match 取字典(操作, 键) {
        Some(Val::List(l)) => l.borrow().clone(),
        _ => return None,
    };
    if 项.len() != 3 {
        return None;
    }
    let f = |v: &Val| 到数(v).unwrap_or(0.0);
    Some((f(&项[0]), f(&项[1]), f(&项[2])))
}

fn 内容流(页: &Val, 高: f64, 字体序: &HashMap<String, usize>) -> String {
    let 块们 = match 取字典(页, "内容") {
        Some(Val::List(l)) => l.borrow().clone(),
        _ => Vec::new(),
    };
    let mut 出: Vec<String> = Vec::new();
    for 操作 in &块们 {
        let 种 = 取字典(操作, "种").map(|v| as_text(&v)).unwrap_or_default();
        match 种.as_str() {
            "文本" => {
                let 文本 = 取字典(操作, "文本").map(|v| as_text(&v)).unwrap_or_default();
                let 字体 = 取字典(操作, "字体").map(|v| as_text(&v)).unwrap_or_default();
                let 字号 = 取字典(操作, "字号").as_ref().and_then(到数).unwrap_or(12.0);
                let 行距 = 取字典(操作, "行距").as_ref().and_then(到数).unwrap_or(16.8);
                let x = 取字典(操作, "x").as_ref().and_then(到数).unwrap_or(72.0);
                let y = 取字典(操作, "y").as_ref().and_then(到数).unwrap_or(72.0);
                let (r, g, b) = 读颜色(操作, "颜色").unwrap_or((0.0, 0.0, 0.0));
                let fi = 字体序.get(&字体).copied().unwrap_or(1);
                出.push("BT".into());
                出.push(format!("/F{fi} {} Tf", 数(字号)));
                出.push(format!("{} {} {} rg", 数(r), 数(g), 数(b)));
                出.push(format!("1 0 0 1 {} {} Tm", 数(x), 数(高 - y)));
                for (i, 行) in 文本.split('\n').enumerate() {
                    if i > 0 {
                        出.push(format!("0 {} Td", 数(-行距)));
                    }
                    if !行.is_empty() {
                        出.push(format!("{} Tj", 串(行)));
                    }
                }
                出.push("ET".into());
            }
            "线" => {
                let x1 = 取字典(操作, "x1").as_ref().and_then(到数).unwrap_or(0.0);
                let y1 = 取字典(操作, "y1").as_ref().and_then(到数).unwrap_or(0.0);
                let x2 = 取字典(操作, "x2").as_ref().and_then(到数).unwrap_or(0.0);
                let y2 = 取字典(操作, "y2").as_ref().and_then(到数).unwrap_or(0.0);
                let 宽 = 取字典(操作, "线宽").as_ref().and_then(到数).unwrap_or(1.0);
                let (r, g, b) = 读颜色(操作, "颜色").unwrap_or((0.0, 0.0, 0.0));
                出.push(format!("{} {} {} RG", 数(r), 数(g), 数(b)));
                出.push(format!("{} w", 数(宽)));
                出.push(format!(
                    "{} {} m {} {} l S",
                    数(x1),
                    数(高 - y1),
                    数(x2),
                    数(高 - y2)
                ));
            }
            "矩形" => {
                let x = 取字典(操作, "x").as_ref().and_then(到数).unwrap_or(0.0);
                let y = 取字典(操作, "y").as_ref().and_then(到数).unwrap_or(0.0);
                let w = 取字典(操作, "宽").as_ref().and_then(到数).unwrap_or(0.0);
                let h = 取字典(操作, "高").as_ref().and_then(到数).unwrap_or(0.0);
                let 线宽 = 取字典(操作, "线宽").as_ref().and_then(到数).unwrap_or(1.0);
                let 填充 = 读颜色(操作, "填充");
                if let Some((fr, fg, fb)) = 填充 {
                    出.push(format!("{} {} {} rg", 数(fr), 数(fg), 数(fb)));
                }
                let (r, g, b) = 读颜色(操作, "颜色").unwrap_or((0.0, 0.0, 0.0));
                出.push(format!("{} {} {} RG", 数(r), 数(g), 数(b)));
                出.push(format!("{} w", 数(线宽)));
                出.push(format!("{} {} {} {} re", 数(x), 数(高 - y - h), 数(w), 数(h)));
                出.push(if 填充.is_some() { "B" } else { "S" }.into());
            }
            _ => {}
        }
    }
    出.push(String::new()); // 保证末尾有换行
    出.join("\n")
}

fn 生成(v: &Val) -> Result<Vec<u8>, crate::JishiError> {
    let (宽, 高, 页们) = 文档部件(v)?;
    let 页们 = 页们.borrow().clone();
    let n = 页们.len();

    // 用到的字体：去重 + 排序（排序是为了逐字节可复现）
    let mut 用到: Vec<String> = Vec::new();
    for 页 in &页们 {
        if let Some(Val::List(l)) = 取字典(页, "内容") {
            for 操作 in l.borrow().iter() {
                if 取字典(操作, "种").map(|x| as_text(&x)).unwrap_or_default() == "文本" {
                    let f = 取字典(操作, "字体").map(|x| as_text(&x)).unwrap_or_default();
                    if !用到.contains(&f) {
                        用到.push(f);
                    }
                }
            }
        }
    }
    用到.sort();
    let mut 字体序: HashMap<String, usize> = HashMap::new();
    for (i, 名) in 用到.iter().enumerate() {
        字体序.insert(名.clone(), i + 1);
    }

    let mut 对象们: Vec<Vec<u8>> = Vec::new();
    // 1: Catalog
    对象们.push(b"<< /Type /Catalog /Pages 2 0 R >>".to_vec());
    // 2: Pages
    let 孩子: Vec<String> = (0..n).map(|i| format!("{} 0 R", 3 + i)).collect();
    对象们.push(
        format!("<< /Type /Pages /Kids [{}] /Count {} >>", 孩子.join(" "), n).into_bytes(),
    );
    // 3 …: 每页
    for (i, _) in 页们.iter().enumerate() {
        let 内容号 = 3 + n + i;
        let mut 字体项 = String::new();
        if !用到.is_empty() {
            字体项 = " /Font << ".to_string();
            let 段: Vec<String> = 用到
                .iter()
                .enumerate()
                .map(|(j, 名)| format!("/F{} {} 0 R", 字体序[名], 3 + 2 * n + j))
                .collect();
            字体项.push_str(&段.join(" "));
            字体项.push_str(" >>");
        }
        对象们.push(
            format!(
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {} {}] /Resources <<{} >> /Contents {} 0 R >>",
                数(宽),
                数(高),
                字体项,
                内容号
            )
            .into_bytes(),
        );
    }
    // 内容流（不压缩）
    for 页 in &页们 {
        let 流 = 内容流(页, 高, &字体序).into_bytes();
        let mut 体 = format!("<< /Length {} >>\nstream\n", 流.len()).into_bytes();
        体.extend_from_slice(&流);
        体.extend_from_slice(b"endstream");
        对象们.push(体);
    }
    // 字体
    for 名 in &用到 {
        对象们.push(
            format!(
                "<< /Type /Font /Subtype /Type1 /BaseFont /{} /Encoding /WinAnsiEncoding >>",
                名
            )
            .into_bytes(),
        );
    }

    let mut 出: Vec<u8> = Vec::new();
    出.extend_from_slice(b"%PDF-1.4\n");
    // 二进制注释：让 FTP / 邮件系统别把文件当文本改坏
    出.extend_from_slice(b"%\xe2\xe3\xcf\xd3\n");
    let mut 偏移们: Vec<usize> = Vec::new();
    for (i, 体) in 对象们.iter().enumerate() {
        偏移们.push(出.len());
        出.extend_from_slice(format!("{} 0 obj\n", i + 1).as_bytes());
        出.extend_from_slice(体);
        出.extend_from_slice(b"\nendobj\n");
    }
    let xref位置 = 出.len();
    // 每条正好 20 字节：10 位偏移 + 空格 + 5 位世代 + 空格 + n/f + 空格 + LF
    出.extend_from_slice(format!("xref\n0 {}\n", 对象们.len() + 1).as_bytes());
    出.extend_from_slice(b"0000000000 65535 f \n");
    for 偏 in &偏移们 {
        出.extend_from_slice(format!("{:010} 00000 n \n", 偏).as_bytes());
    }
    出.extend_from_slice(
        format!("trailer\n<< /Size {} /Root 1 0 R >>\n", 对象们.len() + 1).as_bytes(),
    );
    出.extend_from_slice(format!("startxref\n{}\n", xref位置).as_bytes());
    出.extend_from_slice(b"%%EOF\n");
    Ok(出)
}

// ---------------------------------------------------------------------------
// 读
// ---------------------------------------------------------------------------

fn 找(数据: &[u8], 干: &[u8], 起: usize) -> Option<usize> {
    if 起 >= 数据.len() || 干.is_empty() || 干.len() > 数据.len() {
        return None;
    }
    数据[起..]
        .windows(干.len())
        .position(|w| w == 干)
        .map(|p| p + 起)
}

/// 从 `i` 开始读一串十进制数字。
fn 整数(数据: &[u8], i: usize) -> Option<(u64, usize)> {
    let mut j = i;
    while j < 数据.len() && 数据[j] == b' ' {
        j += 1;
    }
    let s = j;
    while j < 数据.len() && 数据[j].is_ascii_digit() {
        j += 1;
    }
    if j == s {
        return None;
    }
    let v = std::str::from_utf8(&数据[s..j]).ok()?.parse().ok()?;
    Some((v, j))
}

fn 拆体(体: &[u8]) -> (Vec<u8>, Option<Vec<u8>>) {
    let k = match 找(体, b"stream", 0) {
        Some(x) => x,
        None => return (体.to_vec(), None),
    };
    let 字典 = 体[..k].to_vec();
    let mut 起 = k + 6;
    if 起 + 1 < 体.len() && &体[起..起 + 2] == b"\r\n" {
        起 += 2;
    } else if 起 < 体.len() && (体[起] == b'\n' || 体[起] == b'\r') {
        起 += 1;
    }
    // `/Length N`：后面紧跟 `0 R` 说明是间接引用，那种情况退回找 endstream
    let mut 终: Option<usize> = None;
    if let Some(p) = 找(&字典, b"/Length", 0) {
        if let Some((长, 尾)) = 整数(&字典, p + 7) {
            let 是引用 = {
                let mut q = 尾;
                while q < 字典.len() && 字典[q] == b' ' {
                    q += 1;
                }
                q + 1 < 字典.len() && 字典[q].is_ascii_digit() && 字典[q + 1] == b' '
            };
            if !是引用 && (长 as usize) <= 体.len().saturating_sub(起) {
                终 = Some(起 + 长 as usize);
            }
        }
    }
    let 终 = 终.unwrap_or_else(|| 找(体, b"endstream", 起).unwrap_or(体.len()));
    (字典, Some(体[起..终.min(体.len())].to_vec()))
}

/// FlateDecode → zlib（两字节头）→ 裸 deflate 交给 `inflate_raw`。
fn 解压(字典: &[u8], 流: &Option<Vec<u8>>) -> Result<Vec<u8>, crate::JishiError> {
    let 流 = match 流 {
        Some(x) => x,
        None => return Ok(Vec::new()),
    };
    if 找(字典, b"/FlateDecode", 0).is_none() {
        return Ok(流.clone());
    }
    if 流.len() > 2 && 流[0] == 0x78 {
        match crate::inflate::inflate_raw(&流[2..]) {
            Ok(v) => return Ok(v),
            Err(e) => return Err(err("值错误", format!("PDF 里的压缩流解不开（{e}）"))),
        }
    }
    crate::inflate::inflate_raw(流)
        .map_err(|e| err("值错误", format!("PDF 里的压缩流解不开（{e}）")))
}

fn 对象表(数据: &[u8]) -> HashMap<usize, Vec<u8>> {
    // 扫 `\d+ \d+ obj`
    let mut 起们: Vec<(usize, usize, usize)> = Vec::new();
    let mut i = 0usize;
    while i < 数据.len() {
        if 数据[i].is_ascii_digit() && (i == 0 || !数据[i - 1].is_ascii_digit()) {
            let s = i;
            let mut j = i;
            while j < 数据.len() && 数据[j].is_ascii_digit() {
                j += 1;
            }
            let 号: usize = std::str::from_utf8(&数据[s..j])
                .ok()
                .and_then(|x| x.parse().ok())
                .unwrap_or(0);
            let mut k = j;
            while k < 数据.len() && 数据[k] == b' ' {
                k += 1;
            }
            let g = k;
            while k < 数据.len() && 数据[k].is_ascii_digit() {
                k += 1;
            }
            if k > g {
                let mut m = k;
                while m < 数据.len() && 数据[m] == b' ' {
                    m += 1;
                }
                if 数据.len() >= m + 3 && &数据[m..m + 3] == b"obj" {
                    起们.push((s, m + 3, 号));
                    i = m + 3;
                    continue;
                }
            }
        }
        i += 1;
    }

    let mut 表: HashMap<usize, Vec<u8>> = HashMap::new();
    for (i, (_, 尾, 号)) in 起们.iter().enumerate() {
        let 终 = if i + 1 < 起们.len() { 起们[i + 1].0 } else { 数据.len() };
        表.entry(*号).or_insert_with(|| 数据[*尾..终].to_vec());
    }

    // 拆对象流（PDF 1.5+ 常把页对象压进 /ObjStm，不拆会「一页都找不到」）
    let mut 新增: Vec<(usize, Vec<u8>)> = Vec::new();
    for (_, 体) in 表.iter() {
        let (字典, 流) = 拆体(体);
        if 找(&字典, b"/ObjStm", 0).is_none() {
            continue;
        }
        let 对数 = 找(&字典, b"/N", 0)
            .and_then(|p| 整数(&字典, p + 2))
            .map(|(v, _)| v as usize);
        let 首 = 找(&字典, b"/First", 0)
            .and_then(|p| 整数(&字典, p + 6))
            .map(|(v, _)| v as usize);
        let (对数, 首) = match (对数, 首) {
            (Some(a), Some(b)) => (a, b),
            _ => continue,
        };
        let 解 = match 解压(&字典, &流) {
            Ok(v) => v,
            Err(_) => continue,
        };
        if 首 > 解.len() {
            continue;
        }
        // 头部是 `号1 偏1 号2 偏2 …`
        let mut 对们: Vec<(usize, usize)> = Vec::new();
        let mut p = 0usize;
        for _ in 0..对数 {
            let (a, q) = match 整数(&解[..首], p) {
                Some(x) => x,
                None => break,
            };
            let (b, r) = match 整数(&解[..首], q) {
                Some(x) => x,
                None => break,
            };
            对们.push((a as usize, b as usize));
            p = r;
        }
        for (k, (内号, 偏)) in 对们.iter().enumerate() {
            let 下偏 = if k + 1 < 对们.len() {
                对们[k + 1].1
            } else {
                解.len() - 首
            };
            let a = 首 + *偏;
            let b = (首 + 下偏).min(解.len());
            if a < b {
                新增.push((*内号, 解[a..b].to_vec()));
            }
        }
    }
    for (号, 体) in 新增 {
        // 🔴 对象流里的**覆盖**顶层的：打包的对象在顶层本来就不该有实体
        表.insert(号, 体);
    }
    表
}

/// 从字典里抠 `/键 12 0 R` 的对象号。
fn 引用(字典: &[u8], 键: &[u8]) -> Option<usize> {
    let p = 找(字典, 键, 0)?;
    let (v, 尾) = 整数(字典, p + 键.len())?;
    let (_, 尾2) = 整数(字典, 尾)?;
    let mut q = 尾2;
    while q < 字典.len() && 字典[q] == b' ' {
        q += 1;
    }
    if q < 字典.len() && 字典[q] == b'R' {
        Some(v as usize)
    } else {
        None
    }
}

fn 引用们(字典: &[u8], 键: &[u8]) -> Vec<usize> {
    if let Some(p) = 找(字典, 键, 0) {
        let mut q = p + 键.len();
        while q < 字典.len() && 字典[q] == b' ' {
            q += 1;
        }
        if q < 字典.len() && 字典[q] == b'[' {
            if let Some(尾) = 找(字典, b"]", q) {
                let 内 = &字典[q + 1..尾];
                let mut 出 = Vec::new();
                let mut i = 0usize;
                while let Some((v, 尾2)) = 整数(内, i) {
                    let (_, 尾3) = match 整数(内, 尾2) {
                        Some(x) => x,
                        None => break,
                    };
                    let mut r = 尾3;
                    while r < 内.len() && 内[r] == b' ' {
                        r += 1;
                    }
                    if r < 内.len() && 内[r] == b'R' {
                        出.push(v as usize);
                        i = r + 1;
                    } else {
                        i = r.max(尾3);
                        if i == 尾3 && 尾3 >= 内.len() {
                            break;
                        }
                    }
                }
                return 出;
            }
        }
    }
    引用(字典, 键).map(|x| vec![x]).unwrap_or_default()
}

fn 页序(表: &HashMap<usize, Vec<u8>>) -> Vec<usize> {
    let mut 根: Option<usize> = None;
    let mut 号们: Vec<usize> = 表.keys().copied().collect();
    号们.sort();
    for 号 in &号们 {
        let (字典, _) = 拆体(&表[号]);
        if 找(&字典, b"/Catalog", 0).is_some() {
            根 = 引用(&字典, b"/Pages");
            break;
        }
    }
    let 根 = match 根 {
        Some(r) if 表.contains_key(&r) => r,
        _ => {
            // 没有可用的目录：退回按对象号，至少不会一页不给
            return 号们
                .into_iter()
                .filter(|号| {
                    let (字典, _) = 拆体(&表[号]);
                    找(&字典, b"/Page", 0).is_some() && 找(&字典, b"/Pages", 0).is_none()
                })
                .collect();
        }
    };
    let mut 出 = Vec::new();
    fn 展开(
        号: usize,
        深: usize,
        表: &HashMap<usize, Vec<u8>>,
        出: &mut Vec<usize>,
    ) {
        if 深 > 12 {
            return;
        }
        let 体 = match 表.get(&号) {
            Some(b) => b,
            None => return,
        };
        let (字典, _) = 拆体(体);
        if 找(&字典, b"/Pages", 0).is_some() {
            for k in 引用们(&字典, b"/Kids") {
                展开(k, 深 + 1, 表, 出);
            }
        } else {
            出.push(号);
        }
    }
    展开(根, 0, 表, &mut 出);
    出
}

// -- 内容流词法 -------------------------------------------------------------

#[derive(Debug, Clone)]
enum 词 {
    数(f64),
    名(Vec<u8>),
    串(Vec<u8>),
    符(String),
    /// `[...]` 数组 —— **词法阶段就收好**，后面处理 `TJ` 才不用再管括号配对。
    组(Vec<词>),
    字典起,
    字典终,
}

const 分隔: [u8; 15] = *b" \t\r\n\x0c/[]<>(){}%";

fn 是分隔(c: u8) -> bool {
    分隔.contains(&c)
}

fn 去空白(数据: &[u8], mut i: usize) -> usize {
    let n = 数据.len();
    while i < n {
        let c = 数据[i];
        if c == b' ' || c == b'\t' || c == b'\r' || c == b'\n' || c == 0x0c || c == 0 {
            i += 1;
        } else if c == b'%' {
            i = 找(数据, b"\n", i).map(|p| p + 1).unwrap_or(n);
        } else {
            break;
        }
    }
    i
}

fn 读串(数据: &[u8], mut i: usize) -> (Vec<u8>, usize) {
    let mut 出: Vec<u8> = Vec::new();
    let n = 数据.len();
    i += 1;
    let mut 深 = 1i32;
    while i < n {
        let c = 数据[i];
        if c == b'\\' {
            i += 1;
            if i >= n {
                break;
            }
            let d = 数据[i];
            match d {
                b'n' => {
                    出.push(b'\n');
                    i += 1;
                }
                b'r' => {
                    出.push(b'\r');
                    i += 1;
                }
                b't' => {
                    出.push(b'\t');
                    i += 1;
                }
                b'b' => {
                    出.push(8);
                    i += 1;
                }
                b'f' => {
                    出.push(12);
                    i += 1;
                }
                b'0'..=b'7' => {
                    let mut v = 0u32;
                    let mut k = 0;
                    while k < 3 && i < n && (b'0'..=b'7').contains(&数据[i]) {
                        v = v * 8 + (数据[i] - b'0') as u32;
                        i += 1;
                        k += 1;
                    }
                    出.push((v & 0xFF) as u8);
                }
                b'\n' | b'\r' => {
                    if d == b'\r' && i + 1 < n && 数据[i + 1] == b'\n' {
                        i += 1;
                    }
                    i += 1;
                }
                _ => {
                    出.push(d);
                    i += 1;
                }
            }
            continue;
        }
        if c == b'(' {
            深 += 1;
        } else if c == b')' {
            深 -= 1;
            if 深 == 0 {
                i += 1;
                break;
            }
        }
        出.push(c);
        i += 1;
    }
    (出, i)
}

fn 十六进制(块: &[u8]) -> Vec<u8> {
    let mut 数位: Vec<u8> = Vec::new();
    for &c in 块 {
        if c.is_ascii_hexdigit() {
            数位.push(c);
        }
    }
    if 数位.len() % 2 == 1 {
        数位.push(b'0');
    }
    let s = String::from_utf8_lossy(&数位).to_string();
    let mut 出 = Vec::new();
    let b = s.as_bytes();
    for i in (0..b.len()).step_by(2) {
        let 高 = (b[i] as char).to_digit(16).unwrap_or(0);
        let 低 = (b[i + 1] as char).to_digit(16).unwrap_or(0);
        出.push(((高 * 16 + 低) & 0xFF) as u8);
    }
    出
}

fn 词们(数据: &[u8]) -> Vec<词> {
    let mut 出: Vec<词> = Vec::new();
    let n = 数据.len();
    let mut i = 0usize;
    loop {
        i = 去空白(数据, i);
        if i >= n {
            break;
        }
        let c = 数据[i];
        if c == b'(' {
            let (s, ni) = 读串(数据, i);
            出.push(词::串(s));
            i = ni;
            continue;
        }
        if c == b'<' {
            if i + 1 < n && 数据[i + 1] == b'<' {
                出.push(词::字典起);
                i += 2;
                continue;
            }
            let j = match 找(数据, b">", i) {
                Some(x) => x,
                None => break,
            };
            出.push(词::串(十六进制(&数据[i + 1..j])));
            i = j + 1;
            continue;
        }
        if c == b'>' {
            if i + 1 < n && 数据[i + 1] == b'>' {
                出.push(词::字典终);
                i += 2;
                continue;
            }
            i += 1;
            continue;
        }
        if c == b'[' {
            // 收到配对的 `]`，中间那段**递归**再词法一遍
            let mut 深 = 1i32;
            let mut j = i + 1;
            while j < n {
                if 数据[j] == b'[' {
                    深 += 1;
                } else if 数据[j] == b']' {
                    深 -= 1;
                    if 深 == 0 {
                        break;
                    }
                } else if 数据[j] == b'(' {
                    // 串里的 `[` `]` 不算括号：跳过整个串
                    let (_, nj) = 读串(数据, j);
                    j = nj;
                    continue;
                }
                j += 1;
            }
            出.push(词::组(词们(&数据[i + 1..j.min(n)])));
            i = j + 1;
            continue;
        }
        if c == b']' {
            i += 1;
            continue;
        }
        if c == b'{' || c == b'}' {
            i += 1;
            continue;
        }
        if c == b'/' {
            let mut j = i + 1;
            while j < n && !是分隔(数据[j]) {
                j += 1;
            }
            出.push(词::名(数据[i + 1..j].to_vec()));
            i = j;
            continue;
        }
        let mut j = i;
        while j < n && !是分隔(数据[j]) {
            j += 1;
        }
        if j == i {
            i += 1;
            continue;
        }
        let w = &数据[i..j];
        let s = String::from_utf8_lossy(w).to_string();
        match s.parse::<f64>() {
            Ok(v) => 出.push(词::数(v)),
            Err(_) => 出.push(词::符(s)),
        }
        i = j;
    }
    出
}

// -- ToUnicode CMap ---------------------------------------------------------

/// 从 `i` 处找 `<hex>`，返回 (hex 字符串, 新位置)。
fn 取十六进制串(s: &str, i: usize) -> Option<(String, usize)> {
    let b = s.as_bytes();
    let mut p = i;
    while p < b.len() && b[p] != b'<' {
        p += 1;
    }
    if p >= b.len() {
        return None;
    }
    let mut q = p + 1;
    while q < b.len() && b[q] != b'>' {
        if !b[q].is_ascii_hexdigit() {
            return None;
        }
        q += 1;
    }
    if q >= b.len() {
        return None;
    }
    Some((s[p + 1..q].to_string(), q + 1))
}

fn 定宽(v: u32, 宽: usize) -> Vec<u8> {
    let w = 宽.max(1);
    let mut 出 = vec![0u8; w];
    for i in 0..w {
        出[w - 1 - i] = ((v >> (8 * i)) & 0xFF) as u8;
    }
    出
}

fn utf16(h: &str) -> String {
    if h.is_empty() {
        return String::new();
    }
    let mut b = 十六进制(h.as_bytes());
    if b.len() % 2 == 1 {
        b.push(0);
    }
    let 单: Vec<u16> = b
        .chunks(2)
        .map(|c| ((c[0] as u16) << 8) | (c.get(1).copied().unwrap_or(0) as u16))
        .collect();
    String::from_utf16_lossy(&单)
}

fn 加一(s: &str, k: u32) -> String {
    if s.is_empty() {
        return s.to_string();
    }
    let mut chars: Vec<char> = s.chars().collect();
    let 末 = match chars.pop() {
        Some(c) => c,
        None => return s.to_string(),
    };
    let mut out: String = chars.into_iter().collect();
    out.push(char::from_u32(末 as u32 + k).unwrap_or(末));
    out
}

fn cmap(数据: &[u8]) -> HashMap<Vec<u8>, String> {
    let 文 = String::from_utf8_lossy(数据).to_string();
    let mut 出: HashMap<Vec<u8>, String> = HashMap::new();

    // beginbfchar … endbfchar：`<码> <值>` 一对一对
    let mut 找位 = 0usize;
    while let Some(a) = 文[找位..].find("beginbfchar") {
        let 起 = 找位 + a + "beginbfchar".len();
        let 终 = 文[起..].find("endbfchar").map(|x| 起 + x).unwrap_or(文.len());
        找位 = 终;
        let 块 = &文[起..终];
        let mut i = 0usize;
        loop {
            let (码, ni) = match 取十六进制串(块, i) {
                Some(x) => x,
                None => break,
            };
            let (值, ni2) = match 取十六进制串(块, ni) {
                Some(x) => x,
                None => break,
            };
            let k = 十六进制(码.as_bytes());
            if !k.is_empty() {
                出.insert(k, utf16(&值));
            }
            i = ni2;
        }
    }

    // beginbfrange … endbfrange：`<低> <高> <基>` 或 `<低> <高> [<a> <b>]`
    找位 = 0;
    while let Some(a) = 文[找位..].find("beginbfrange") {
        let 起 = 找位 + a + "beginbfrange".len();
        let 终 = 文[起..].find("endbfrange").map(|x| 起 + x).unwrap_or(文.len());
        找位 = 终;
        let 块 = &文[起..终];
        let mut i = 0usize;
        loop {
            let (低s, ni) = match 取十六进制串(块, i) {
                Some(x) => x,
                None => break,
            };
            let (高s, ni2) = match 取十六进制串(块, ni) {
                Some(x) => x,
                None => break,
            };
            let 宽 = (低s.len() + 1) / 2;
            let 低 = u32::from_str_radix(&低s, 16).unwrap_or(0);
            let 高 = u32::from_str_radix(&高s, 16).unwrap_or(0);
            // 看第三个位置是数组还是单个 hex
            let b = 块.as_bytes();
            let mut p = ni2;
            while p < b.len() && b[p].is_ascii_whitespace() {
                p += 1;
            }
            if p < b.len() && b[p] == b'[' {
                // `<低> <高> [<a> <b> …]`：第 k 个目标对应码 低+k
                let mut q = p + 1;
                let mut k = 0u32;
                loop {
                    // 先看是不是已经到 `]`
                    let mut r = q;
                    while r < b.len() && b[r].is_ascii_whitespace() {
                        r += 1;
                    }
                    if r >= b.len() || b[r] == b']' {
                        q = (r + 1).min(b.len());
                        break;
                    }
                    match 取十六进制串(块, r) {
                        Some((v, nq)) => {
                            出.insert(定宽(低 + k, 宽), utf16(&v));
                            k += 1;
                            q = nq;
                        }
                        None => break,
                    }
                }
                i = q;
            } else {
                let (基, ni3) = match 取十六进制串(块, p.saturating_sub(0)) {
                    Some(x) => x,
                    None => break,
                };
                if 高 >= 低 && 高 - 低 <= 65535 {
                    for k in 0..=(高 - 低) {
                        出.insert(定宽(低 + k, 宽), 加一(&utf16(&基), k));
                    }
                }
                i = ni3;
            }
        }
    }
    出
}

fn 解码串(字节: &[u8], cmap表: &HashMap<Vec<u8>, String>) -> String {
    if cmap表.is_empty() {
        return 字节.iter().map(|b| *b as char).collect();
    }
    let mut 出 = String::new();
    let mut i = 0usize;
    while i < 字节.len() {
        let mut 命中 = false;
        for 宽 in [2usize, 1] {
            if i + 宽 <= 字节.len() {
                if let Some(s) = cmap表.get(&字节[i..i + 宽].to_vec()) {
                    出.push_str(s);
                    i += 宽;
                    命中 = true;
                    break;
                }
            }
        }
        if !命中 {
            出.push(字节[i] as char);
            i += 1;
        }
    }
    出
}

fn 页的cmap(表: &HashMap<usize, Vec<u8>>, 页字典: &[u8]) -> HashMap<Vec<u8>, String> {
    // 扫 `/Fn 6 0 R`，取第一个带 /ToUnicode 的字体
    let mut i = 0usize;
    while let Some(p) = 找(&页字典[i..], b"/F", 0) {
        let 起 = i + p;
        let mut q = 起 + 2;
        while q < 页字典.len() && (页字典[q].is_ascii_alphanumeric()) {
            q += 1;
        }
        if let Some((号, 尾)) = 整数(页字典, q) {
            let (_, 尾2) = match 整数(页字典, 尾) {
                Some(x) => x,
                None => {
                    i = 尾;
                    continue;
                }
            };
            let mut r = 尾2;
            while r < 页字典.len() && 页字典[r] == b' ' {
                r += 1;
            }
            if r < 页字典.len() && 页字典[r] == b'R' {
                if let Some(体) = 表.get(&(号 as usize)) {
                    let (字, _) = 拆体(体);
                    if let Some(映) = 引用(&字, b"/ToUnicode") {
                        if let Some(体2) = 表.get(&映) {
                            let (字2, 流2) = 拆体(体2);
                            if let Ok(解) = 解压(&字2, &流2) {
                                return cmap(&解);
                            }
                        }
                    }
                }
                i = r + 1;
                continue;
            }
        }
        i = 起 + 2;
    }
    HashMap::new()
}

fn 文本行们(词表: &[词], cmap表: &HashMap<Vec<u8>, String>) -> Vec<String> {
    let mut 行们: Vec<String> = Vec::new();
    let mut 当前: Vec<String> = Vec::new();
    let mut 上y: Option<f64> = None;
    let mut x = 0.0f64;
    let mut y = 0.0f64;
    let mut 行距 = 12.0f64;
    let mut 操作数: Vec<词> = Vec::new();

    fn 收(
        字节: &[u8],
        cmap表: &HashMap<Vec<u8>, String>,
        y: f64,
        上y: &mut Option<f64>,
        当前: &mut Vec<String>,
        行们: &mut Vec<String>,
    ) {
        let 文 = 解码串(字节, cmap表);
        if 文.is_empty() {
            return;
        }
        if let Some(py) = *上y {
            if (y - py).abs() > 1.0 {
                let 合 = 当前.join("").trim().to_string();
                if !合.is_empty() {
                    行们.push(合);
                }
                当前.clear();
            }
        }
        *上y = Some(y);
        当前.push(文);
    }

    let mut i = 0usize;
    while i < 词表.len() {
        let w = &词表[i];
        if !matches!(w, 词::符(_)) {
            操作数.push(w.clone());
            i += 1;
            continue;
        }
        let op = match w {
            词::符(s) => s.clone(),
            _ => unreachable!(),
        };
        let 数们: Vec<f64> = 操作数
            .iter()
            .filter_map(|t| match t {
                词::数(v) => Some(*v),
                _ => None,
            })
            .collect();
        match op.as_str() {
            "Tm" if 数们.len() >= 6 => {
                x = 数们[数们.len() - 2];
                y = 数们[数们.len() - 1];
            }
            "Td" | "TD" if 数们.len() >= 2 => {
                x += 数们[数们.len() - 2];
                y += 数们[数们.len() - 1];
                if op == "TD" {
                    行距 = -数们[数们.len() - 1];
                }
            }
            "T*" => {
                y -= 行距;
            }
            "Tf" if !数们.is_empty() => {
                行距 = 数们[数们.len() - 1] * 1.2;
            }
            "Tj" => {
                for t in 操作数.iter().rev() {
                    if let 词::串(b) = t {
                        收(b, cmap表, y, &mut 上y, &mut 当前, &mut 行们);
                        break;
                    }
                }
            }
            "'" | "\"" => {
                y -= 行距;
                for t in 操作数.iter().rev() {
                    if let 词::串(b) = t {
                        收(b, cmap表, y, &mut 上y, &mut 当前, &mut 行们);
                        break;
                    }
                }
            }
            "TJ" => {
                // 数组在词法阶段就收成 `词::组` 了，直接遍历
                for t in 操作数.iter().rev() {
                    if let 词::组(组) = t {
                        for x in 组 {
                            match x {
                                词::串(b) => {
                                    收(b, cmap表, y, &mut 上y, &mut 当前, &mut 行们)
                                }
                                // TJ 里的负数是**字距加宽**，大到一定程度才当空格
                                词::数(v) if *v < -100.0 => 当前.push(" ".to_string()),
                                _ => {}
                            }
                        }
                        break;
                    }
                }
            }
            _ => {}
        }
        操作数.clear();
        i += 1;
    }
    let 合 = 当前.join("").trim().to_string();
    if !合.is_empty() {
        行们.push(合);
    }
    行们
}

fn 页们文(数据: &[u8]) -> Result<Vec<Vec<String>>, crate::JishiError> {
    let 表 = 对象表(数据);
    let 序 = 页序(&表);
    let mut 出: Vec<Vec<String>> = Vec::new();
    for 号 in 序 {
        let 体 = 表.get(&号).cloned().unwrap_or_default();
        let (字典, _) = 拆体(&体);
        let cmap表 = 页的cmap(&表, &字典);
        let mut 行们: Vec<String> = Vec::new();
        for 流号 in 引用们(&字典, b"/Contents") {
            let 体2 = match 表.get(&流号) {
                Some(x) => x.clone(),
                None => continue,
            };
            let (字, 流) = 拆体(&体2);
            let 解 = 解压(&字, &流)?;
            if !解.is_empty() {
                行们.extend(文本行们(&词们(&解), &cmap表));
            }
        }
        出.push(行们);
    }
    Ok(出)
}

fn 打开(路径: &str) -> Result<Vec<Vec<String>>, crate::JishiError> {
    // 三宿主的文案要一致：**不存在**单独一句（与 Python 的 FileNotFoundError 对齐）
    let 数据 = match std::fs::read(路径) {
        Ok(v) => v,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Err(err("文件错误", format!("找不到文件「{路径}」")))
        }
        Err(e) => return Err(err("文件错误", format!("读不了「{路径}」：{e}"))),
    };
    if 数据.len() < 5 || &数据[..5] != b"%PDF-" {
        return Err(err_with_hint(
            "值错误",
            format!("「{路径}」不是 PDF 文件（开头不是 %PDF-）"),
            "Word 文档要读就用 Word.读文档；.doc 是老格式，不是 PDF".into(),
        ));
    }
    let 页们 = 页们文(&数据)?;
    if 页们.is_empty() {
        return Err(err_with_hint(
            "值错误",
            format!("「{路径}」里一页都没找到 —— 文件可能不完整"),
            "用阅读器打开确认一下；能打开的话把文件发出来看".into(),
        ));
    }
    if !页们.iter().any(|p| !p.is_empty()) {
        return Err(err_with_hint(
            "值错误",
            format!(
                "这个 PDF 没有文本层（{} 页里一个字都读不出来）—— 大概是扫描件",
                页们.len()
            ),
            "扫描件要 OCR 才行，这一版不做；要提文字请先用别的工具转成文本".into(),
        ));
    }
    Ok(页们)
}

// ---------------------------------------------------------------------------
// 内建
// ---------------------------------------------------------------------------

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "PDF";
        "新建" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("新建", args, 0, 2)?;
            let 宽 = match args.first() {
                None | Some(Val::None_) => A4宽,
                Some(v) => {
                    let f = 要数(v, "新建", "宽")?;
                    if !(1.0..=20000.0).contains(&f) {
                        return Err(err("值错误", format!("页宽要在 1 到 20000 点之间，现在是 {}", display(v))));
                    }
                    f
                }
            };
            let 高 = match args.get(1) {
                None | Some(Val::None_) => A4高,
                Some(v) => {
                    let f = 要数(v, "新建", "高")?;
                    if !(1.0..=20000.0).contains(&f) {
                        return Err(err("值错误", format!("页高要在 1 到 20000 点之间，现在是 {}", display(v))));
                    }
                    f
                }
            };
            let 页 = crate::dict_new(vec![
                (字("内容"), crate::list_new(Vec::new())),
            ]);
            Ok(crate::dict_new(vec![
                (字("宽"), Val::Float(宽)),
                (字("高"), Val::Float(高)),
                (字("页们"), crate::list_new(vec![页])),
            ]))
        },
        "加页" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("加页", args, 1, 1)?;
            let (_, _, 页们) = 文档部件(&args[0])?;
            let 页 = crate::dict_new(vec![(字("内容"), crate::list_new(Vec::new()))]);
            页们.borrow_mut().push(页);
            let 数 = 页们.borrow().len();
            Ok(Val::Int(数 as i128))
        },
        "写文本" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("写文本", args, 2, 5)?;
            let 文本 = as_text(&args[1]);
            查ASCII(&文本, "写文本")?;
            let 选项 = match args.get(4) {
                None | Some(Val::None_) => crate::dict_new(Vec::new()),
                Some(v) => {
                    if !matches!(v, Val::Dict(_)) {
                        return Err(err(
                            "类型错误",
                            format!("「选项」要给一个字典（比如 {{\"字号\": 20}}），给的是「{}」", display(v)),
                        ));
                    }
                    v.clone()
                }
            };
            let 字号 = match 取字典(&选项, "字号") {
                None => 12.0,
                Some(v) => {
                    let f = 要数(&v, "字号", "字号")?;
                    if !(1.0..=400.0).contains(&f) {
                        return Err(err("值错误", format!("「字号」要在 1 到 400 点之间，现在是 {}", display(&v))));
                    }
                    f
                }
            };
            let 行距 = match 取字典(&选项, "行距") {
                None => 字号 * 1.4,
                Some(v) => {
                    let f = 要数(&v, "行距", "行距")?;
                    if f <= 0.0 {
                        return Err(err("值错误", "「行距」要大于 0"));
                    }
                    f
                }
            };
            let x = match args.get(2) {
                None | Some(Val::None_) => 72.0,
                Some(v) => 要数(v, "写文本", "x")?,
            };
            let y = match args.get(3) {
                None | Some(Val::None_) => 72.0,
                Some(v) => 要数(v, "写文本", "y")?,
            };
            let 字体 = 字体名(&选项, "写文本")?;
            let (r, g, b) = 要颜色(取字典(&选项, "颜色").as_ref(), "写文本")?;
            let 内容 = 当前内容(&args[0])?;
            内容.borrow_mut().push(crate::dict_new(vec![
                (字("种"), 字("文本")),
                (字("文本"), Val::Str(文本.into())),
                (字("x"), Val::Float(x)),
                (字("y"), Val::Float(y)),
                (字("字体"), 字(字体)),
                (字("字号"), Val::Float(字号)),
                (字("行距"), Val::Float(行距)),
                (字("颜色"), crate::list_new(vec![
                    Val::Float(r), Val::Float(g), Val::Float(b),
                ])),
            ]));
            Ok(Val::None_)
        },
        "画线" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("画线", args, 5, 6)?;
            let 选项 = 选项或空(args.get(5))?;
            let 线宽 = match 取字典(&选项, "线宽") {
                None => 1.0,
                Some(v) => {
                    let f = 要数(&v, "画线", "线宽")?;
                    if !(f > 0.0 && f <= 100.0) {
                        return Err(err("值错误", format!("「线宽」要在 0 到 100 点之间，现在是 {}", display(&v))));
                    }
                    f
                }
            };
            let (r, g, b) = 要颜色(取字典(&选项, "颜色").as_ref(), "画线")?;
            let 内容 = 当前内容(&args[0])?;
            内容.borrow_mut().push(crate::dict_new(vec![
                (字("种"), 字("线")),
                (字("x1"), Val::Float(要数(&args[1], "画线", "x1")?)),
                (字("y1"), Val::Float(要数(&args[2], "画线", "y1")?)),
                (字("x2"), Val::Float(要数(&args[3], "画线", "x2")?)),
                (字("y2"), Val::Float(要数(&args[4], "画线", "y2")?)),
                (字("线宽"), Val::Float(线宽)),
                (字("颜色"), crate::list_new(vec![
                    Val::Float(r), Val::Float(g), Val::Float(b),
                ])),
            ]));
            Ok(Val::None_)
        },
        "画矩形" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("画矩形", args, 5, 6)?;
            let 选项 = 选项或空(args.get(5))?;
            let 线宽 = match 取字典(&选项, "线宽") {
                None => 1.0,
                Some(v) => {
                    let f = 要数(&v, "画矩形", "线宽")?;
                    if !(f > 0.0 && f <= 100.0) {
                        return Err(err("值错误", format!("「线宽」要在 0 到 100 点之间，现在是 {}", display(&v))));
                    }
                    f
                }
            };
            let 宽 = 要数(&args[3], "画矩形", "宽")?;
            let 高 = 要数(&args[4], "画矩形", "高")?;
            if 宽 <= 0.0 || 高 <= 0.0 {
                return Err(err("值错误", "「画矩形」的宽和高要大于 0"));
            }
            let (r, g, b) = 要颜色(取字典(&选项, "颜色").as_ref(), "画矩形")?;
            let 填充 = match 取字典(&选项, "填充") {
                None | Some(Val::None_) => Val::None_,
                Some(v) => {
                    let (fr, fg, fb) = 要颜色(Some(&v), "画矩形")?;
                    crate::list_new(vec![Val::Float(fr), Val::Float(fg), Val::Float(fb)])
                }
            };
            let 内容 = 当前内容(&args[0])?;
            内容.borrow_mut().push(crate::dict_new(vec![
                (字("种"), 字("矩形")),
                (字("x"), Val::Float(要数(&args[1], "画矩形", "x")?)),
                (字("y"), Val::Float(要数(&args[2], "画矩形", "y")?)),
                (字("宽"), Val::Float(宽)),
                (字("高"), Val::Float(高)),
                (字("线宽"), Val::Float(线宽)),
                (字("颜色"), crate::list_new(vec![
                    Val::Float(r), Val::Float(g), Val::Float(b),
                ])),
                (字("填充"), 填充),
            ]));
            Ok(Val::None_)
        },
        "保存" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("保存", args, 2, 2)?;
            let p = as_text(&args[1]);
            if p.is_empty() {
                return Err(err("值错误", "保存路径不能是空的"));
            }
            if !p.to_lowercase().ends_with(".pdf") {
                return Err(err(
                    "值错误",
                    format!("保存的文件名要以 .pdf 结尾，现在是「{p}」"),
                ));
            }
            let 数据 = 生成(&args[0])?;
            std::fs::write(&p, &数据)
                .map_err(|e| err("文件错误", format!("写不了「{p}」：{e}")))?;
            Ok(Val::None_)
        },
        "页数" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("页数", args, 1, 1)?;
            Ok(Val::Int(打开(&as_text(&args[0]))?.len() as i128))
        },
        "读文本" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("读文本", args, 1, 1)?;
            let 页们 = 打开(&as_text(&args[0]))?;
            let 页值: Vec<Val> = 页们
                .into_iter()
                .map(|行们| {
                    crate::list_new(行们.into_iter().map(|s| Val::Str(s.into())).collect())
                })
                .collect();
            Ok(crate::dict_new(vec![
                (字("页数"), Val::Int(页值.len() as i128)),
                (字("页们"), crate::list_new(页值)),
            ]))
        },
    }
}

fn 选项或空(v: Option<&Val>) -> Result<Val, crate::JishiError> {
    match v {
        None | Some(Val::None_) => Ok(crate::dict_new(Vec::new())),
        Some(x) => {
            if matches!(x, Val::Dict(_)) {
                Ok(x.clone())
            } else {
                Err(err(
                    "类型错误",
                    format!("「选项」要给一个字典，给的是「{}」", display(x)),
                ))
            }
        }
    }
}
