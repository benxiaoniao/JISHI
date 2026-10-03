//! 标准库 `对比`（Python 侧 `jishi/stdlib/对比.py`，5 个函数）。
//!
//! 行级 diff。**零第三方依赖**——LCS + 分块全自己写，输出格式对齐
//! Python 的 `difflib`。
//!
//! ⚠️ **手写「与标准库等价」的算法时，要对齐它的输出结构，不只是结果**：
//! - `difflib` 把「一行换一行」给成**一个 `replace` 块**，不是 `delete`+`insert`
//!   两块——不合并会让 `并排` 把一行拆成两行、让 `统一` 的 `@@` 计数算错；
//! - `@@` 的行数是**整组的跨度**（含上下文行），**不扣末尾插入**；
//! - 跨度为 1 时省略 `,1`（`@@ -2 +2 @@`）。
//!
//! 上面三条都是 Node 侧实现时踩出来的（那边先做，教训搬过来）。

use std::collections::HashMap;

use super::{need_range, table, to_vec, R};
use crate::{err, list_new, Val, VM};

type Op = (String, usize, usize, usize, usize); // tag, i1, i2, j1, j2

fn lines(name: &str, v: &Val) -> Result<Vec<String>, crate::JishiError> {
    match v {
        Val::Str(s) => {
            // splitlines：`\n` / `\r\n` / `\r` 都认；末尾换行不产生空行
            let mut out: Vec<String> = Vec::new();
            let mut cur = String::new();
            let mut chars = s.chars().peekable();
            while let Some(c) = chars.next() {
                match c {
                    '\n' => {
                        out.push(std::mem::take(&mut cur));
                    }
                    '\r' => {
                        if chars.peek() == Some(&'\n') {
                            chars.next();
                        }
                        out.push(std::mem::take(&mut cur));
                    }
                    _ => cur.push(c),
                }
            }
            if !cur.is_empty() {
                out.push(cur);
            }
            Ok(out)
        }
        _ => {
            let items = to_vec(v).map_err(|_| {
                err("类型错误", format!("「{name}」要传文本或文本列表"))
            })?;
            let mut out = Vec::with_capacity(items.len());
            for (i, x) in items.iter().enumerate() {
                match x {
                    Val::Str(s) => out.push(s.clone()),
                    other => {
                        return Err(err(
                            "类型错误",
                            format!("「{name}」的第 {} 项不是文本：{}", i + 1, crate::display(other)),
                        ))
                    }
                }
            }
            Ok(out)
        }
    }
}

/// 行级差异的 opcode 列表（等价 `difflib.SequenceMatcher.get_opcodes`，
/// 并把相邻的 `delete`+`insert` 合并成 `replace`）。
fn opcodes(a: &[String], b: &[String]) -> Vec<Op> {
    let n = a.len();
    let m = b.len();
    // dp[i][j] = a[i..] 与 b[j..] 的 LCS 长度
    let mut dp = vec![vec![0usize; m + 1]; n + 1];
    for i in (0..n).rev() {
        for j in (0..m).rev() {
            dp[i][j] = if a[i] == b[j] {
                dp[i + 1][j + 1] + 1
            } else {
                dp[i + 1][j].max(dp[i][j + 1])
            };
        }
    }
    let mut raw: Vec<Op> = Vec::new();
    let push = |raw: &mut Vec<Op>, tag: &str, i1: usize, i2: usize, j1: usize, j2: usize| {
        if let Some(last) = raw.last_mut() {
            if last.0 == tag && last.2 == i1 && last.4 == j1 {
                last.2 = i2;
                last.4 = j2;
                return;
            }
        }
        raw.push((tag.to_string(), i1, i2, j1, j2));
    };
    let (mut i, mut j) = (0usize, 0usize);
    while i < n && j < m {
        if a[i] == b[j] {
            push(&mut raw, "equal", i, i + 1, j, j + 1);
            i += 1;
            j += 1;
        } else if dp[i + 1][j] >= dp[i][j + 1] {
            push(&mut raw, "delete", i, i + 1, j, j);
            i += 1;
        } else {
            push(&mut raw, "insert", i, i, j, j + 1);
            j += 1;
        }
    }
    while i < n {
        push(&mut raw, "delete", i, i + 1, j, j);
        i += 1;
    }
    while j < m {
        push(&mut raw, "insert", i, i, j, j + 1);
        j += 1;
    }
    // 合并 delete+insert → replace（difflib 的输出结构）
    let mut out: Vec<Op> = Vec::new();
    let mut k = 0;
    while k < raw.len() {
        if raw[k].0 == "delete" && k + 1 < raw.len() && raw[k + 1].0 == "insert" {
            let cur = &raw[k];
            let nxt = &raw[k + 1];
            out.push(("replace".to_string(), cur.1, cur.2, nxt.3, nxt.4));
            k += 2;
        } else {
            out.push(raw[k].clone());
            k += 1;
        }
    }
    out
}

fn int_arg(name: &str, v: &crate::Val) -> Result<usize, crate::JishiError> {
    match v {
        Val::Int(n) if *n >= 0 => Ok(*n as usize),
        _ => Err(err("类型错误", format!("「{name}」要传 0 或正整数"))),
    }
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "对比";
        "逐行" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("逐行", args, 2, 3)?;
            let a = lines("逐行", &args[0])?;
            let b = lines("逐行", &args[1])?;
            let numbered = matches!(args.get(2), Some(Val::Bool(true)));
            let mut out = Vec::new();
            for (tag, i1, i2, j1, j2) in opcodes(&a, &b) {
                let item = |mark: &str, no: usize, s: &str| {
                    list_new(vec![
                        Val::Str(mark.to_string()),
                        Val::Int(no as i128),
                        Val::Str(s.to_string()),
                    ])
                };
                match tag.as_str() {
                    "equal" => {
                        for k in i1..i2 {
                            out.push(item(" ", if numbered { j1 + k - i1 + 1 } else { 0 }, &a[k]));
                        }
                    }
                    "delete" => {
                        for k in i1..i2 {
                            out.push(item("-", if numbered { j1 + 1 } else { 0 }, &a[k]));
                        }
                    }
                    "insert" => {
                        for k in j1..j2 {
                            out.push(item("+", if numbered { k + 1 } else { 0 }, &b[k]));
                        }
                    }
                    _ => {
                        for k in i1..i2 {
                            out.push(item("-", if numbered { j1 + 1 } else { 0 }, &a[k]));
                        }
                        for k in j1..j2 {
                            out.push(item("+", if numbered { k + 1 } else { 0 }, &b[k]));
                        }
                    }
                }
            }
            Ok(list_new(out))
        },
        "统一" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("统一", args, 2, 3)?;
            let a = lines("统一", &args[0])?;
            let b = lines("统一", &args[1])?;
            let n = match args.get(2) {
                None => 3,
                Some(v) => int_arg("上下文", v)?,
            };
            let ops = opcodes(&a, &b);
            // 照搬 difflib 的分组：长串相同行只留头 n 行，尾 n 行留给下一组
            let mut groups: Vec<Vec<Op>> = Vec::new();
            let mut g: Vec<Op> = Vec::new();
            for op in &ops {
                if op.0 == "equal" && op.2 - op.1 > 2 * n {
                    if n > 0 {
                        g.push(("equal".into(), op.1, op.1 + n, op.3, op.3 + n));
                    }
                    if g.iter().any(|o| o.0 != "equal") {
                        groups.push(std::mem::take(&mut g));
                    } else {
                        g.clear();
                    }
                    g.push(("equal".into(), op.2 - n, op.2, op.4 - n, op.4));
                    continue;
                }
                g.push(op.clone());
            }
            if g.iter().any(|o| o.0 != "equal") {
                groups.push(g);
            }
            let mut out: Vec<String> = Vec::new();
            for g in groups {
                let first = &g[0];
                let last = &g[g.len() - 1];
                let span_i = last.2 - first.1;
                let span_j = last.4 - first.3;
                if out.is_empty() {
                    out.push("--- 旧".to_string());
                    out.push("+++ 新".to_string());
                }
                let rng = |start: usize, span: usize| {
                    if span == 1 { format!("{start}") } else { format!("{start},{span}") }
                };
                out.push(format!(
                    "@@ -{} +{} @@",
                    rng(first.1 + 1, span_i),
                    rng(first.3 + 1, span_j)
                ));
                for (tag, i1, i2, j1, j2) in &g {
                    if tag == "equal" {
                        for k in *i1..*i2 {
                            out.push(format!(" {}", a[k]));
                        }
                    } else {
                        if tag == "delete" || tag == "replace" {
                            for k in *i1..*i2 {
                                out.push(format!("-{}", a[k]));
                            }
                        }
                        if tag == "insert" || tag == "replace" {
                            for k in *j1..*j2 {
                                out.push(format!("+{}", b[k]));
                            }
                        }
                    }
                }
            }
            Ok(Val::Str(out.join("\n")))
        },
        "并排" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("并排", args, 2, 3)?;
            let a = lines("并排", &args[0])?;
            let b = lines("并排", &args[1])?;
            let width = match args.get(2) {
                None => 30,
                Some(v) => {
                    let w = int_arg("宽", v)?;
                    if w < 4 {
                        return Err(err("类型错误", "「宽」要传不小于 4 的整数"));
                    }
                    w
                }
            };
            let cut = |s: &str| -> String {
                let w: usize = s.chars().map(|c| if (c as u32) > 0x2e80 { 2 } else { 1 }).sum();
                if w <= width {
                    let mut o = s.to_string();
                    for _ in 0..(width - w) {
                        o.push(' ');
                    }
                    return o;
                }
                let mut acc = 0;
                let mut o = String::new();
                for c in s.chars() {
                    let cw = if (c as u32) > 0x2e80 { 2 } else { 1 };
                    if acc + cw > width - 1 {
                        break;
                    }
                    o.push(c);
                    acc += cw;
                }
                o.push('…');
                for _ in 0..(width.saturating_sub(acc + 1)) {
                    o.push(' ');
                }
                o
            };
            let mut out: Vec<String> = Vec::new();
            for (tag, i1, i2, j1, j2) in opcodes(&a, &b) {
                if tag == "equal" {
                    for k in i1..i2 {
                        out.push(format!("{} | {}", cut(&a[k]), cut(&b[j1 + k - i1])));
                    }
                } else {
                    let left_n = if tag == "insert" { 0 } else { i2 - i1 };
                    let right_n = if tag == "delete" { 0 } else { j2 - j1 };
                    for k in 0..left_n.max(right_n) {
                        let left = if k < left_n { a[i1 + k].clone() } else { String::new() };
                        let right = if k < right_n { b[j1 + k].clone() } else { String::new() };
                        let mark = if left == right { ' ' } else { '~' };
                        out.push(format!("{} {mark}| {}", cut(&left), cut(&right)));
                    }
                }
            }
            Ok(Val::Str(out.join("\n")))
        },
        "相似度" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("相似度", args, 2, 2)?;
            let a = lines("相似度", &args[0])?;
            let b = lines("相似度", &args[1])?;
            if a.is_empty() && b.is_empty() {
                return Ok(Val::Float(1.0));
            }
            let same: usize = opcodes(&a, &b)
                .iter()
                .filter(|(tag, ..)| tag == "equal")
                .map(|(_, i1, i2, ..)| i2 - i1)
                .sum();
            Ok(Val::Float(2.0 * same as f64 / (a.len() + b.len()) as f64))
        },
        "最相似" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("最相似", args, 2, 2)?;
            let target = match &args[0] {
                Val::Str(s) => s.clone(),
                _ => return Err(err("类型错误", "「最相似」的第一个参数要传文本")),
            };
            let items = lines("最相似", &args[1])?;
            if items.is_empty() {
                return Ok(Val::Str(String::new()));
            }
            // 按**字符**比（与 Python 侧一致：那边传进来的是字符串，按字符算）
            let tc: Vec<String> = target.chars().map(|c| c.to_string()).collect();
            let mut best = items[0].clone();
            let mut best_r = -1.0f64;
            for it in &items {
                let ic: Vec<String> = it.chars().map(|c| c.to_string()).collect();
                let same: usize = opcodes(&tc, &ic)
                    .iter()
                    .filter(|(tag, ..)| tag == "equal")
                    .map(|(_, i1, i2, ..)| i2 - i1)
                    .sum();
                let denom = (tc.len() + ic.len()) as f64;
                let r = if denom == 0.0 { 1.0 } else { 2.0 * same as f64 / denom };
                if r > best_r {
                    best = it.clone();
                    best_r = r;
                }
            }
            Ok(Val::Str(best))
        },
    }
}
