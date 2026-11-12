//! 标准库 `Word`（M194）。与 Python 侧 `oracle/jishi/stdlib/Word.py` 同行为。
//!
//! `.docx` = **zip 包 + 几份 XML**。真正要紧的只有 `word/document.xml`（正文），
//! 可选 `word/header*.xml` / `word/footer*.xml`。zip 容器复用 `crate::zip`，
//! XML 用 M194 新写的 `crate::xml`。
//!
//! ## 文档对象是个字典
//!
//! `{"块们": [块, …]}`，块也是字典。三宿主都用「字典 + 列表」表示，不必各造类型。
//!
//! ## 写出去的为什么 Word 能打开
//!
//! 三份部件缺一不可：`[Content_Types].xml`（说明这是 docx）、`_rels/.rels`
//! （从包根找到正文）、`word/document.xml`（正文）。**外加 `w:sectPr`** ——
//! 少了它 Word 报「文件已损坏」，而其余内容看着都对，最难查。

use crate::BuiltinHost;
use std::collections::HashMap;

use super::tables::Table;
use super::{as_text, need, need_range, table, to_vec, R};
use crate::xml::{self, 元素};
use crate::{err, display, Val};

const W: &str = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
const R_NS: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";

/// 对齐：中文名 → Word 内部值。
fn 对齐值(名: &str) -> Option<&'static str> {
    match 名 {
        "左" => Some("left"),
        "居中" => Some("center"),
        "右" => Some("right"),
        "两端" => Some("both"),
        _ => None,
    }
}

const 对齐们: [&str; 4] = ["左", "居中", "右", "两端"];

/// 样式 ID ↔ 中文名（读的时候翻过来，写的时候翻过去）。
fn 样式中文(id: &str) -> &str {
    match id {
        "Heading1" => "标题1",
        "Heading2" => "标题2",
        "Heading3" => "标题3",
        "Heading4" => "标题4",
        "Heading5" => "标题5",
        "Heading6" => "标题6",
        "Title" => "标题",
        "Normal" => "正文",
        "Subtitle" => "副标题",
        _ => id,
    }
}

fn 样式ID(名: &str) -> &str {
    match 名 {
        "标题1" => "Heading1",
        "标题2" => "Heading2",
        "标题3" => "Heading3",
        "标题4" => "Heading4",
        "标题5" => "Heading5",
        "标题6" => "Heading6",
        "标题" => "Title",
        "正文" => "Normal",
        "副标题" => "Subtitle",
        _ => 名,
    }
}

fn 字(s: &str) -> Val {
    Val::Str(s.into())
}

fn 要文(v: &Val) -> Result<(), crate::JishiError> {
    let ok = match v {
        Val::Dict(d) => d.borrow().iter().any(|(k, _)| as_text(k) == "块们"),
        _ => false,
    };
    if !ok {
        return Err(err(
            "类型错误",
            format!(
                "这里要一个文档对象（`Word.新建()` 的返回值），给过来的却是「{}」",
                display(v)
            ),
        ));
    }
    Ok(())
}

/// 往 `块们` 里追加一块。
fn 加块(文档: &Val, 块: Val) -> Result<(), crate::JishiError> {
    要文(文档)?;
    if let Val::Dict(d) = 文档 {
        for (k, v) in d.borrow().iter() {
            if as_text(k) == "块们" {
                if let Val::List(l) = v {
                    l.borrow_mut().push(块);
                    return Ok(());
                }
            }
        }
    }
    Err(err("类型错误", "文档对象里的「块们」不是列表"))
}

fn 段落块(文本: &str, 样式: &str, 选项: Vec<(String, Val)>) -> Val {
    crate::dict_new(vec![
        (字("种类"), 字("段落")),
        (字("文本"), 字(文本)),
        (字("样式"), 字(样式)),
        (字("选项"), crate::dict_new(选项.into_iter().map(|(k, v)| (字(&k), v)).collect())),
    ])
}

// ---------------------------------------------------------------------------
// 读
// ---------------------------------------------------------------------------

fn 转义(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

/// 一个元素下所有 `t` 的文字（`tab` → 制表符、`br` → 换行）。
fn 文字(el: &元素) -> String {
    el.全部文字()
}

fn 段落样式(p: &元素) -> String {
    let mut 出 = String::new();
    p.遍历(&mut |x| {
        if x.名 == "pStyle" && 出.is_empty() {
            出 = 样式中文(x.取属性("val")).to_string();
        }
    });
    出
}

fn 格子们(tbl: &元素) -> Vec<Vec<String>> {
    let mut 数据 = Vec::new();
    for tr in tbl.子们叫("tr") {
        let mut 行: Vec<String> = Vec::new();
        for tc in tr.子们叫("tc") {
            let 文本 = 文字(tc);
            let mut 跨 = 1usize;
            tc.遍历(&mut |x| {
                if x.名 == "gridSpan" {
                    if let Ok(n) = x.取属性("val").parse::<usize>() {
                        跨 = n.max(1);
                    }
                }
            });
            for _ in 0..跨 {
                行.push(文本.clone());
            }
        }
        数据.push(行);
    }
    数据
}

fn 表头们(数据: &[Vec<String>]) -> Vec<String> {
    let Some(首) = 数据.first() else {
        return Vec::new();
    };
    let mut 见: HashMap<String, usize> = HashMap::new();
    let mut 出 = Vec::with_capacity(首.len());
    for 名 in 首 {
        let 基 = if 名.is_empty() { "列".to_string() } else { 名.clone() };
        match 见.get_mut(&基) {
            Some(n) => {
                *n += 1;
                出.push(format!("{基}{}", *n + 1));
            }
            None => {
                见.insert(基.clone(), 0);
                出.push(基);
            }
        }
    }
    出
}

fn 取部件(成员们: &[(String, Vec<u8>)], 名: &str) -> Option<Vec<u8>> {
    let 低 = 名.to_lowercase();
    成员们
        .iter()
        .find(|(n, _)| n.to_lowercase() == 低)
        .map(|(_, b)| b.clone())
}

fn 读成员(路径: &Val) -> Result<Vec<(String, Vec<u8>)>, crate::JishiError> {
    let p = as_text(路径);
    let 字节 = std::fs::read(&p).map_err(|_| {
        err(
            "文件错误",
            format!("找不到文件「{p}」"),
        )
    })?;
    crate::zip::read_all(&字节).map_err(|_| {
        err(
            "类型错误",
            format!("「{p}」不是 .docx（它连 zip 包都不是）"),
        )
    })
}

fn 读文档(路径: &Val) -> Result<Val, crate::JishiError> {
    let 成员们 = 读成员(路径)?;
    let 正文字节 = 取部件(&成员们, "word/document.xml").ok_or_else(|| {
        err(
            "类型错误",
            "这个 zip 里没有 word/document.xml —— 它不是 Word 文档",
        )
    })?;
    let 文 = String::from_utf8_lossy(&正文字节).to_string();
    let 根 = xml::解析(&文);

    // 段落只收 body 的**直接子** p：表格里的 p 属于表格
    let mut 段落: Vec<Val> = Vec::new();
    let mut 表格们: Vec<Val> = Vec::new();
    let mut 图片数 = 0i128;
    根.遍历(&mut |x| {
        if x.名 == "drawing" || x.名 == "pict" {
            图片数 += 1;
        }
    });
    for body in 根.子们叫("body") {
        for el in &body.子们 {
            match el.名.as_str() {
                "p" => {
                    段落.push(crate::dict_new(vec![
                        (字("文本"), 字(&文字(el))),
                        (字("样式"), Val::Str(段落样式(el).as_str().into())),
                    ]));
                }
                "tbl" => {
                    let 数据 = 格子们(el);
                    let headers = 表头们(&数据);
                    let rows: Vec<Vec<Val>> = 数据
                        .iter()
                        .skip(1)
                        .map(|r| r.iter().map(|c| Val::Str(c.as_str().into())).collect())
                        .collect();
                    let hs: Vec<Val> = headers.iter().map(|h| Val::Str(h.as_str().into())).collect();
                    表格们.push(Val::Table(std::rc::Rc::new(Table { headers: hs, rows })));
                }
                _ => {}
            }
        }
    }

    let mut 页眉 = String::new();
    let mut 页脚 = String::new();
    for (n, b) in &成员们 {
        let 低 = n.to_lowercase();
        if 低.starts_with("word/header") && 页眉.is_empty() {
            页眉 = 文字(&xml::解析(&String::from_utf8_lossy(b)));
        } else if 低.starts_with("word/footer") && 页脚.is_empty() {
            页脚 = 文字(&xml::解析(&String::from_utf8_lossy(b)));
        }
    }

    Ok(crate::dict_new(vec![
        (字("段落"), crate::list_new(段落)),
        (字("表格"), crate::list_new(表格们)),
        (字("图片数"), Val::Int(图片数)),
        (字("页眉"), Val::Str(页眉.as_str().into())),
        (字("页脚"), Val::Str(页脚.as_str().into())),
    ]))
}

// ---------------------------------------------------------------------------
// 写：生成 XML
// ---------------------------------------------------------------------------

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

fn 段落XML(块: &Val) -> String {
    let 样式 = 取字典(块, "样式").map(|v| as_text(&v)).unwrap_or_default();
    let 文本 = 取字典(块, "文本").map(|v| as_text(&v)).unwrap_or_default();
    let 选项 = 取字典(块, "选项");

    let mut 段属性 = String::new();
    if !样式.is_empty() && 样式 != "正文" {
        段属性.push_str(&format!("<w:pStyle w:val=\"{}\"/>", 式ID(&样式)));
    }
    if let Some(opt) = &选项 {
        if let Some(Val::Str(对)) = 取字典(opt, "对齐") {
            if let Some(w) = 对齐值(&对) {
                段属性.push_str(&format!("<w:jc w:val=\"{w}\"/>"));
            }
        }
    }
    let mut r属性 = String::new();
    if let Some(opt) = &选项 {
        if matches!(取字典(opt, "粗体"), Some(Val::Bool(true))) {
            r属性.push_str("<w:b/>");
        }
        if matches!(取字典(opt, "斜体"), Some(Val::Bool(true))) {
            r属性.push_str("<w:i/>");
        }
        match 取字典(opt, "字号") {
            Some(Val::Float(f)) => r属性.push_str(&format!("<w:sz w:val=\"{}\"/>", (f * 2.0).round() as i64)),
            Some(Val::Int(i)) => r属性.push_str(&format!("<w:sz w:val=\"{}\"/>", i * 2)),
            _ => {}
        }
    }
    let mut x = String::from("<w:p>");
    if !段属性.is_empty() {
        x.push_str(&format!("<w:pPr>{段属性}</w:pPr>"));
    }
    x.push_str("<w:r>");
    if !r属性.is_empty() {
        x.push_str(&format!("<w:rPr>{r属性}</w:rPr>"));
    }
    x.push_str(&format!(
        "<w:t xml:space=\"preserve\">{}</w:t></w:r></w:p>",
        转义(&文本)
    ));
    x
}

/// 样式中文名 → ID（`标题1` → `Heading1`）。
fn 式ID(名: &str) -> &str {
    样式ID(名)
}

fn 表格XML(块: &Val) -> String {
    let 表头: Vec<String> = match 取字典(块, "表头") {
        Some(v) => to_vec(&v).map(|l| l.iter().map(as_text).collect()).unwrap_or_default(),
        None => return String::new(),
    };
    let 行们: Vec<Vec<String>> = match 取字典(块, "行们") {
        Some(v) => to_vec(&v)
            .map(|l| {
                l.iter()
                    .map(|r| to_vec(r).map(|c| c.iter().map(as_text).collect()).unwrap_or_default())
                    .collect()
            })
            .unwrap_or_default(),
        None => Vec::new(),
    };
    let 列数 = 表头.len();
    if 列数 == 0 {
        return String::new();
    }
    let mut out = String::from(
        "<w:tbl><w:tblPr><w:tblW w:w=\"0\" w:type=\"auto\"/></w:tblPr><w:tblGrid>",
    );
    for _ in 0..列数 {
        out.push_str("<w:gridCol w:w=\"2400\"/>");
    }
    out.push_str("</w:tblGrid>");
    let mut 所有行: Vec<&Vec<String>> = vec![&表头];
    for r in &行们 {
        所有行.push(r);
    }
    for 行 in 所有行 {
        out.push_str("<w:tr>");
        for i in 0..列数 {
            let 格 = 行.get(i).cloned().unwrap_or_default();
            out.push_str(&format!(
                "<w:tc><w:p><w:r><w:t xml:space=\"preserve\">{}</w:t></w:r></w:p></w:tc>",
                转义(&格)
            ));
        }
        out.push_str("</w:tr>");
    }
    out.push_str("</w:tbl>");
    out
}

fn 内容类型XML() -> String {
    format!(
        concat!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>",
            "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">",
            "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>",
            "<Default Extension=\"xml\" ContentType=\"application/xml\"/>",
            "<Override PartName=\"/word/document.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml\"/>",
            "</Types>"
        )
    )
}

fn 关系XML() -> String {
    format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?><Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\"><Relationship Id=\"rId1\" Type=\"{R_NS}/officeDocument\" Target=\"word/document.xml\"/></Relationships>"
    )
}

fn 正文XML(文档: &Val) -> String {
    let mut out = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?><w:document xmlns:w=\"{W}\"><w:body>"
    );
    if let Some(Val::List(l)) = 取字典(文档, "块们") {
        for 块 in l.borrow().iter() {
            let 种 = 取字典(块, "种类").map(|v| as_text(&v)).unwrap_or_default();
            match 种.as_str() {
                "段落" => out.push_str(&段落XML(块)),
                "表格" => out.push_str(&表格XML(块)),
                "分页" => out.push_str("<w:p><w:r><w:br w:type=\"page\"/></w:r></w:p>"),
                _ => {}
            }
        }
    }
    out.push_str(
        "<w:sectPr><w:pgSz w:w=\"11906\" w:h=\"16838\"/><w:pgMar w:top=\"1440\" w:right=\"1440\" w:bottom=\"1440\" w:left=\"1440\" w:header=\"851\" w:footer=\"992\" w:gutter=\"0\"/></w:sectPr>",
    );
    out.push_str("</w:body></w:document>");
    out
}

fn 要docx路径(v: &Val) -> Result<String, crate::JishiError> {
    let p = as_text(v);
    if p.is_empty() {
        return Err(err("值错误", "保存路径不能是空的"));
    }
    if !p.to_lowercase().ends_with(".docx") {
        return Err(err(
            "值错误",
            format!("保存的文件名要以 .docx 结尾，现在是「{p}」"),
        ));
    }
    Ok(p)
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "Word";
        "读文档" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("读文档", args, 1, 1)?;
            读文档(&args[0])
        },
        "新建" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("新建", args, 0, 0)?;
            Ok(crate::dict_new(vec![(
                字("块们"),
                crate::list_new(Vec::new()),
            )]))
        },
        "加标题" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("加标题", args, 2, 3)?;
            let 级 = match args.get(2) {
                None | Some(Val::None_) => 1i128,
                Some(Val::Int(v)) => *v,
                Some(o) => {
                    return Err(err(
                        "值错误",
                        format!("标题级别只能是 1 / 2 / 3，给的是「{}」", display(o)),
                    ))
                }
            };
            if !(1..=3).contains(&级) {
                return Err(err(
                    "值错误",
                    format!("标题级别只能是 1 / 2 / 3，给的是 {级}"),
                ));
            }
            let 文本 = as_text(&args[1]);
            加块(&args[0], 段落块(&文本, &format!("标题{级}"), Vec::new()))?;
            Ok(Val::None_)
        },
        "加段落" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("加段落", args, 2, 3)?;
            let 文本 = as_text(&args[1]);
            let mut 选项: Vec<(String, Val)> = Vec::new();
            if let Some(opt) = args.get(2) {
                if !matches!(opt, Val::None_) {
                    let 项 = match opt {
                        Val::Dict(d) => d.borrow().clone(),
                        _ => {
                            return Err(err(
                                "类型错误",
                                format!("「选项」要给一个字典，给过来的却是「{}」", display(opt)),
                            ))
                        }
                    };
                    for (k, v) in 项 {
                        let 名 = as_text(&k);
                        match 名.as_str() {
                            "粗体" | "斜体" => {
                                let b = matches!(v, Val::Bool(true));
                                选项.push((名, Val::Bool(b)));
                            }
                            "字号" => {
                                let f = match v {
                                    Val::Int(i) => i as f64,
                                    Val::Float(f) => f,
                                    _ => {
                                        return Err(err(
                                            "类型错误",
                                            format!("「字号」要给一个数字（磅），给的是「{}」", display(&v)),
                                        ))
                                    }
                                };
                                if !(1.0..=200.0).contains(&f) {
                                    return Err(err(
                                        "值错误",
                                        format!("「字号」要在 1 到 200 磅之间，现在是 {f}"),
                                    ));
                                }
                                选项.push((名, Val::Float(f)));
                            }
                            "对齐" => {
                                let s = as_text(&v);
                                if 对齐值(&s).is_none() {
                                    return Err(err(
                                        "值错误",
                                        format!(
                                            "「对齐」只能是 {}，给的是「{s}」",
                                            对齐们.join(" / ")
                                        ),
                                    ));
                                }
                                选项.push((名, Val::Str(s.as_str().into())));
                            }
                            _ => {
                                return Err(err(
                                    "值错误",
                                    format!("「选项」里没有「{名}」这一项"),
                                ))
                            }
                        }
                    }
                }
            }
            加块(&args[0], 段落块(&文本, "正文", 选项))?;
            Ok(Val::None_)
        },
        "加表格" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("加表格", args, 2, 2)?;
            let t = match &args[1] {
                Val::Table(t) => t.clone(),
                o => {
                    return Err(err(
                        "类型错误",
                        format!(
                            "「加表格」要一个表格（`表格.读表格` 的返回值），给过来的却是「{}」",
                            display(o)
                        ),
                    ))
                }
            };
            let headers: Vec<Val> = t.headers.clone();
            let rows: Vec<Val> = t
                .rows
                .iter()
                .map(|r| {
                    crate::list_new(r.iter().map(|c| Val::Str(display(c).as_str().into())).collect())
                })
                .collect();
            加块(
                &args[0],
                crate::dict_new(vec![
                    (字("种类"), 字("表格")),
                    (字("表头"), crate::list_new(headers.iter().map(|h| Val::Str(display(h).as_str().into())).collect())),
                    (字("行们"), crate::list_new(rows)),
                ]),
            )?;
            Ok(Val::None_)
        },
        "加分页" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("加分页", args, 1, 1)?;
            加块(&args[0], crate::dict_new(vec![(字("种类"), 字("分页"))]))?;
            Ok(Val::None_)
        },
        "保存" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("保存", args, 2, 2)?;
            要文(&args[0])?;
            let p = 要docx路径(&args[1])?;
            let 成员们: Vec<(String, Vec<u8>)> = vec![
                ("[Content_Types].xml".to_string(), 内容类型XML().into_bytes()),
                ("_rels/.rels".to_string(), 关系XML().into_bytes()),
                ("word/document.xml".to_string(), 正文XML(&args[0]).into_bytes()),
            ];
            let 字节 = crate::zip::build_stored(&成员们);
            std::fs::write(&p, &字节).map_err(|_| {
                err("文件错误", format!("没法写入文件「{p}」"))
            })?;
            Ok(Val::None_)
        },
        "填充模板" => |args: &[Val], vm: &mut dyn BuiltinHost| -> R {
            need_range("填充模板", args, 3, 3)?;
            let p = 要docx路径(&args[1])?;
            let 数据 = match &args[2] {
                Val::Dict(_) => args[2].clone(),
                o => {
                    return Err(err(
                        "类型错误",
                        format!("「数据」要给一个字典（键 → 值），给过来的却是「{}」", display(o)),
                    ))
                }
            };
            let 成员们 = 读成员(&args[0])?;
            let 正文字节 = 取部件(&成员们, "word/document.xml").ok_or_else(|| {
                err("类型错误", "这个 zip 里没有 word/document.xml —— 它不是 Word 文档")
            })?;
            let 正文 = String::from_utf8_lossy(&正文字节).to_string();

            // 只替换 `{{…}}` 里出现过的键；没给的留着
            let mut 新正文 = String::with_capacity(正文.len());
            let 字符: Vec<char> = 正文.chars().collect();
            let mut i = 0usize;
            while i < 字符.len() {
                if 字符[i] == '{' && i + 1 < 字符.len() && 字符[i + 1] == '{' {
                    if let Some(尾) = 找双右(字符.as_slice(), i + 2) {
                        let 键: String = 字符[i + 2..尾].iter().collect();
                        if !键.contains('{') && !键.contains('}') {
                            if let Some(v) = 取字典(&数据, &键) {
                                let _ = vm;
                                新正文.push_str(&转义(&display(&v)));
                                i = 尾 + 2;
                                continue;
                            }
                        }
                    }
                }
                新正文.push(字符[i]);
                i += 1;
            }

            let mut 新成员们: Vec<(String, Vec<u8>)> = Vec::with_capacity(成员们.len());
            for (n, b) in &成员们 {
                if n.to_lowercase() == "word/document.xml" {
                    新成员们.push((n.clone(), 新正文.clone().into_bytes()));
                } else {
                    新成员们.push((n.clone(), b.clone()));
                }
            }
            let 字节 = crate::zip::build_stored(&新成员们);
            std::fs::write(&p, &字节).map_err(|_| {
                err("文件错误", format!("没法写入文件「{p}」"))
            })?;
            Ok(Val::None_)
        },
    }
}

/// 找 `}}`，返回 `}` 里第一个的下标。
fn 找双右(b: &[char], 起: usize) -> Option<usize> {
    let mut i = 起;
    while i + 1 < b.len() {
        if b[i] == '}' && b[i + 1] == '}' {
            return Some(i);
        }
        i += 1;
    }
    None
}

