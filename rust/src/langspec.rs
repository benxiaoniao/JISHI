//! 语言元数据 → JSON（`jishi --lang-spec` 的 Rust 侧，R7.2）。
//!
//! 输出要**逐字节**对齐 Python 的 `ai.lang_spec_json()`：
//! `json.dumps(spec, ensure_ascii=False, indent=2)`，且 `content_hash` 是
//! `sha256(json.dumps(spec, ensure_ascii=False, sort_keys=True))` 的前 8 位。
//! 所以这里自带一个**保序**的 JSON 值类型 + 两个序列化器（缩进版 / 紧凑排序版）。
//!
//! 📌 键顺序是**内容的一部分**（Python 的 dict 保序、`json.dumps` 照序输出），
//! 所以 `build()` 里 push 的顺序必须与 `ai.build_lang_spec()` 逐条对应。

use jishi_frontend::sha256;

use crate::jsonw::{dump_compact_sorted, dump_indent, Jv};
use crate::langdata as D;

fn s(x: &str) -> Jv {
    Jv::Str(x.to_string())
}

fn str_arr(items: &[&str]) -> Jv {
    Jv::Arr(items.iter().map(|x| s(x)).collect())
}

fn pairs(pairs: Vec<(&str, &str)>) -> Jv {
    Jv::Obj(pairs.into_iter().map(|(k, v)| (k.to_string(), s(v))).collect())
}

/// 组装 `build_lang_spec()`（**不含** `content_hash`，键顺序逐条对应）。
fn build_spec() -> Vec<(String, Jv)> {
    let mut out: Vec<(String, Jv)> = Vec::new();
    let mut push = |k: &str, v: Jv| out.push((k.to_string(), v));

    push("version", s(D::LANG_VERSION));

    push("keywords", pairs(D::KEYWORDS.to_vec()));

    let binary = Jv::Arr(D::BIN_OPS.iter().map(|(op, prec, name)| {
        Jv::Obj(vec![
            ("op".to_string(), s(op)),
            ("precedence".to_string(), Jv::Int(*prec)),
            ("name".to_string(), s(name)),
        ])
    }).collect());
    push("operators", Jv::Obj(vec![
        ("binary".to_string(), binary),
        ("compare".to_string(), str_arr(D::CMP_OPS)),
        ("assign".to_string(), str_arr(D::ASSIGN_OPS)),
        ("unary".to_string(), str_arr(D::UNARY_OPS)),
    ]));

    push("builtins", Jv::Arr(D::BUILTINS.iter().map(|(n, doc)| {
        Jv::Obj(vec![("name".to_string(), s(n)), ("doc".to_string(), s(doc))])
    }).collect()));

    push("methods", Jv::Obj(D::METHODS.iter().map(|(k, v)| {
        (k.to_string(), str_arr(v))
    }).collect()));

    push("stdlib", Jv::Arr(D::STDLIB.iter().map(|(m, fns)| {
        Jv::Obj(vec![
            ("module".to_string(), s(m)),
            ("functions".to_string(), Jv::Arr(fns.iter().map(|(n, sig, doc)| {
                Jv::Obj(vec![
                    ("name".to_string(), s(n)),
                    ("sig".to_string(), s(sig)),
                    ("doc".to_string(), s(doc)),
                ])
            }).collect())),
        ])
    }).collect()));

    push("exceptions", str_arr(D::EXCEPTIONS));

    push("error_codes", Jv::Arr(D::ERROR_CODES.iter().map(|(code, cls, title, internal)| {
        Jv::Obj(vec![
            ("code".to_string(), s(code)),
            ("class".to_string(), s(cls)),
            ("title".to_string(), s(title)),
            ("internal".to_string(), Jv::Bool(*internal)),
        ])
    }).collect()));

    push("check_codes", Jv::Arr(D::CHECK_CODES.iter().map(|(code, level, name, doc)| {
        Jv::Obj(vec![
            ("code".to_string(), s(code)),
            ("level".to_string(), s(level)),
            ("name".to_string(), s(name)),
            ("doc".to_string(), s(doc)),
        ])
    }).collect()));

    push("python_equiv", Jv::Obj(vec![
        ("keywords".to_string(), pairs(D::PY_KEYWORDS.to_vec())),
        ("operators".to_string(), pairs(D::PY_OPERATORS.to_vec())),
        ("builtins".to_string(), pairs(D::PY_BUILTINS.to_vec())),
    ]));

    out
}


/// `ai.content_hash(spec)` 的等价物：对**不含 `content_hash`** 的 spec 做
/// 「紧凑 + 键排序」的 sha256，取前 8 位十六进制。
pub fn content_hash_of(spec: &[(String, Jv)]) -> String {
    let mut blob = String::new();
    dump_compact_sorted(&Jv::Obj(spec.to_vec()), &mut blob);
    sha256::hex(blob.as_bytes())[..8].to_string()
}

/// `ai.content_hash(build_lang_spec())` —— 语言卡的版本指纹。
pub fn content_hash() -> String {
    content_hash_of(&build_spec())
}

/// `ai.lang_spec_json()` 的等价物（**不含**末尾换行 —— 与 Python 的
/// `json.dumps` 一致；`print` 那一步由调用方加）。
pub fn lang_spec_json() -> String {
    let mut spec = build_spec();
    let hash = content_hash_of(&spec);
    spec.push(("content_hash".to_string(), s(&hash)));
    let mut out = String::new();
    dump_indent(&Jv::Obj(spec), 0, &mut out);
    out
}

/// `ai.render_ai_card(模块, 任务)` 的等价物 —— Markdown 语言卡（贴进 LLM 系统提示词用）。
///
/// M116 起与 Python 侧**同样支持分片**：
/// * `(None, None)` → 全量卡（「一贴就能用」）；
/// * `(Some("文本"), None)` → 单模块分片卡；
/// * `(None, Some("表格"))` → 按任务的分片卡（若干模块 + 该任务惯用法）。
///
/// ⚠️ 卡片里的**中文文本**（语法段 / 错误对照段 / 任务表 / 任务惯用法）不是写在这里的
/// —— 它们在 `langdata.rs` 里（由 `tools/gen_langdata_rs.py` 从 Python 侧**投影**过来）。
/// 手抄两遍必漂，而 `tests/test_m90_langspec_rust.py` 盯着「逐字节一致」。
// ---------------------------------------------------------------------------
// 已装的包（M149）—— **与 Python `oracle/jishi/ai.py` 逐条一致**
// ---------------------------------------------------------------------------

/// 一个包的对外函数：`(签名, 文档首行)`。
pub(crate) type 包函数 = (String, String);

/// 从包的入口 `.jsh` 源码里扫出**公开函数**（与 Python `扫包里的公开函数` 同规则）：
///
/// - **顶格**的 `函数 名(参数)：`（前面不能有空格）—— 缩进的是内部函数；
/// - 名字以 `_` 开头的不算（包的约定：那类是实现细节）；
/// - 文档串取「紧跟函数行、缩进、以三引号开头」那一段的**第一行**。
///
/// ⚠️ 为什么扫源码而不是让包在 `包.json` 里报一份 API：写两处就要改两处，
/// 迟早对不上。代价是这条**行式扫描**两边各写一遍 —— 判据
/// `tests/test_m149_pkg_card.py` 拿同一份源码比两边结果。
pub(crate) fn 扫包里的公开函数(源码: &str) -> Vec<包函数> {
    // CRLF 归一（Python 是文本模式读的，天然没有 `\r`；这里显式对齐）
    let 文本 = 源码.replace("\r\n", "\n").replace('\r', "\n");
    let 行们: Vec<&str> = 文本.split('\n').collect();
    let 引 = "\"\"\"";
    let mut 出: Vec<包函数> = Vec::new();
    let mut i = 0usize;
    while i < 行们.len() {
        let 行 = 行们[i];
        if !行.starts_with("函数 ") {
            i += 1;
            continue;
        }
        let 体 = &行["函数 ".len()..];
        let 左 = match 体.find('(') {
            Some(x) if x > 0 => x,
            _ => { i += 1; continue; }
        };
        let 名 = 体[..左].trim();
        // 参数括号要配对（默认值里可能有括号：`底色 = 空图(1)`）
        let mut 深 = 0i32;
        let mut 右: Option<usize> = None;
        for (j, ch) in 体.char_indices() {
            if j < 左 { continue; }
            if ch == '(' { 深 += 1; }
            else if ch == ')' {
                深 -= 1;
                if 深 == 0 { 右 = Some(j); break; }
            }
        }
        let 右 = match 右 {
            Some(r) => r,
            None => { i += 1; continue; }
        };
        if !名.is_empty() && !名.starts_with('_') {
            let sig = format!("{}{}", 名, &体[左..=右]).trim().to_string();
            出.push((sig, 紧跟的文档首行(&行们, i + 1)));
        }
        i += 1;
    }
    出
}

/// 函数定义后面那一段文档串的**第一行**（没有就返回空串）。
fn 紧跟的文档首行(行们: &[&str], 起: usize) -> String {
    let 引 = "\"\"\"";
    let mut i = 起;
    while i < 行们.len() && 行们[i].trim().is_empty() {
        i += 1;
    }
    if i >= 行们.len() {
        return String::new();
    }
    let 行 = 行们[i].trim();
    if !行.starts_with(引) {
        return String::new();
    }
    let 余 = &行[引.len()..];
    match 余.find(引) {
        Some(k) => 余[..k].trim().to_string(),
        None => 余.trim().to_string(),
    }
}

/// 一个包的对外函数（顺序 = 文件里出现的顺序）。
///
/// ⚠️ 规则必须与**导入**时一致（`packages::module_files`）：声明了「入口」就只扫
/// 入口那一个文件；**没声明就按文件名排序把全部 `.jsh` 都扫一遍** —— 导入时就是
/// 这么把它们编进同一个作用域的。（老包如 `基础库` 没写「入口」，只扫
/// `<包名>.jsh` 会一个字都扫不到。）
fn 扫一个包(pkg_dir: &std::path::Path, meta: &crate::json::Json) -> Vec<包函数> {
    let 取 = |k: &str| -> String {
        match meta.get(k) {
            Some(v) if crate::packages::truthy(Some(v)) => crate::packages::py_str(v),
            _ => String::new(),
        }
    };
    let 全部: Vec<String> = match std::fs::read_dir(pkg_dir) {
        Ok(rd) => {
            let mut v: Vec<String> = rd.flatten()
                .filter(|e| e.path().is_file())
                .map(|e| e.file_name().to_string_lossy().to_string())
                .filter(|n| n.ends_with(".jsh"))
                .collect();
            v.sort();
            v
        }
        Err(_) => return Vec::new(),
    };
    let mut 入口 = 取("入口").trim().to_string();
    let 文件们: Vec<String> = if !入口.is_empty() {
        if !入口.ends_with(".jsh") { 入口.push_str(".jsh"); }
        if 全部.iter().any(|f| *f == 入口) { vec![入口] } else { Vec::new() }
    } else {
        全部
    };
    let mut 出: Vec<包函数> = Vec::new();
    for f in 文件们 {
        if let Ok(text) = std::fs::read_to_string(pkg_dir.join(&f)) {
            出.extend(扫包里的公开函数(&text));
        }
    }
    出
}

/// **本机能导入的**本地包（名字排序）。
///
/// 「能导入的」= `localpkg::package_roots()` 那些目录（额外根 → 项目 → 用户）；
/// 同名包**先出现的赢** —— 与 `导入 X 从 本地包` 的查找顺序一致，卡片说的必须是
/// 「你真正 import 得到的那个」。
///
/// ⚠️ 读不了 / 没有 `包.json` 的目录**跳过**（不猜、不报错）：卡片是给人贴进
/// 提示词的东西，不能因为某个装坏了的包就打不出来。
fn 已装的包() -> Vec<(String, String, String, Vec<包函数>)> {
    let mut 看到: Vec<String> = Vec::new();
    let mut 出: Vec<(String, String, String, Vec<包函数>)> = Vec::new();
    for 根 in jishi_frontend::localpkg::package_roots() {
        let 根p = std::path::Path::new(&根);
        if !根p.is_dir() { continue; }
        let mut 名们: Vec<String> = match std::fs::read_dir(根p) {
            Ok(rd) => rd.flatten()
                .filter(|e| e.path().is_dir())
                .map(|e| e.file_name().to_string_lossy().to_string())
                .collect(),
            Err(_) => continue,
        };
        名们.sort();
        for 名 in 名们 {
            if 看到.contains(&名) { continue; }
            let 目录 = 根p.join(&名);
            let 清单 = 目录.join("包.json");
            if !清单.is_file() { continue; }
            看到.push(名.clone());
            let text = match std::fs::read_to_string(&清单) {
                Ok(t) => t,
                Err(_) => continue,
            };
            let meta = match crate::json::parse(&text) {
                Ok(m) => m,
                Err(_) => continue,
            };
            if meta.as_obj().is_none() { continue; }
            let 取 = |k: &str| -> String {
                match meta.get(k) {
                    Some(v) if crate::packages::truthy(Some(v)) => crate::packages::py_str(v),
                    _ => String::new(),
                }
            };
            let 真名 = {
                let n = 取("名字");
                if n.trim().is_empty() { 名.clone() } else { n.trim().to_string() }
            };
            let 版 = {
                let v = 取("版本");
                if v.trim().is_empty() { "未知".to_string() } else { v.trim().to_string() }
            };
            出.push((真名, 版, 取("描述").trim().to_string(), 扫一个包(&目录, &meta)));
        }
    }
    出.sort_by(|a, b| a.0.cmp(&b.0));
    出
}

/// 已装包那一段；一个包都没装就返回空串（**整段不出现**）。
///
/// ⚠️ 没装包时返回空串是要紧的：干净环境里的卡片必须与 M149 之前**逐字节相同**。
fn 已装的包段() -> String {
    let 包们 = 已装的包();
    if 包们.is_empty() {
        return String::new();
    }
    let mut 块们: Vec<String> = Vec::new();
    for (名字, 版本, 描述, 函数们) in 包们 {
        let mut 头 = format!("### `{名字}`（{版本}）");
        if !描述.is_empty() {
            头.push_str(&format!(" —— {描述}"));
        }
        let mut 行 = vec![头];
        for (sig, doc) in 函数们 {
            行.push(if doc.is_empty() {
                format!("- `{sig}`")
            } else {
                format!("- `{sig}` —— {doc}")
            });
        }
        块们.push(行.join("\n"));
    }
    format!("## 已装的包（本机；先 `导入 包名 从 本地包`，再用 `包名.函数(参数)`）\n{}",
            块们.join("\n"))
}

pub fn render_ai_card(模块: Option<&str>, 任务: Option<&str>) -> Result<String, String> {
    if 模块.is_some() && 任务.is_some() {
        return Err("「模块」与「任务」只能给一个".to_string());
    }
    let hash = content_hash();
    let kw = D::KEYWORDS.iter().map(|(k, _)| *k).collect::<Vec<_>>().join(" ");

    let builtins = D::BUILTINS.iter()
        .map(|(n, doc)| format!("- `{n}`：{doc}"))
        .collect::<Vec<_>>().join("\n");
    let methods = D::METHODS.iter()
        .map(|(k, v)| format!("- {k}：{}", v.join("、")))
        .collect::<Vec<_>>().join("\n");

    // 要带哪些模块：None = 全部
    let mut 模块们: Option<Vec<&str>> = None;
    if let Some(t) = 任务 {
        模块们 = Some(D::CARD_任务表.iter()
            .find(|(名, _)| *名 == t)
            .map(|(_, 列)| 列.to_vec())
            .ok_or_else(|| format!(
                "没有「{t}」这个任务包。可选：{}",
                D::CARD_任务表.iter().map(|(名, _)| *名).collect::<Vec<_>>().join("、")))?);
    } else if let Some(m) = 模块 {
        if !D::STDLIB.iter().any(|(名, _)| *名 == m) {
            return Err(format!("没有「{m}」这个标准库模块"));
        }
        模块们 = Some(vec![m]);
    }

    // ---- 标准库段（**每个函数带完整签名**）----
    let mut 块们: Vec<String> = Vec::new();
    for (名, fns) in D::STDLIB.iter() {
        if let Some(列) = 模块们.as_ref() {
            if !列.iter().any(|x| x == 名) {
                continue;
            }
        }
        let mut 行 = vec![format!("### `{名}`")];
        for (_, sig, doc) in fns.iter() {
            行.push(if doc.is_empty() {
                format!("- `{sig}`")
            } else {
                format!("- `{sig}` —— {doc}")
            });
        }
        块们.push(行.join("\n"));
    }
    let 标准库 = if 块们.is_empty() {
        String::new()
    } else {
        format!(
            "## 标准库（先 `导入 模块名`，再用 `模块.函数(参数)`；\
             **签名里带 `=` 的参数可以省略**）\n\
             ⚠️ 可选参数请**按顺序**传：跳过中间那个只在部分平台支持 —— \
             **别跳**，省哪个就从哪个开始省起。\n{}",
            块们.join("\n"))
    };

    // ---- 惯用段 ----
    let mut 惯用: Vec<String> = Vec::new();
    let 起点;
    if let Some(t) = 任务 {
        if let Some((_, 行们)) = D::CARD_任务惯用.iter().find(|(名, _)| *名 == t) {
            惯用.push(format!("## 「{t}」这类活的惯用法"));
            for (i, x) in 行们.iter().enumerate() {
                惯用.push(format!("{}. {x}", i + 1));
            }
            惯用.push(String::new());
            惯用.push("## 通用惯用法".to_string());
        }
        起点 = 1;
    } else {
        惯用.push("## 惯用法示例".to_string());
        起点 = 1;
    }
    惯用.push(format!("{}. 遍历统计：`令 总和 = 0` / `遍历 x 在 数据：` / `总和 += x`", 起点));
    惯用.push(format!("{}. 条件过滤：`[x 遍历 x 在 数据 如果 x > 0]`", 起点 + 1));
    惯用.push(format!("{}. 函数+默认参数：`函数 打招呼(名字, 语气 = \"你好\")：` / `打印(语气, 名字)`", 起点 + 2));
    惯用.push(format!("{}. 字典构建：`{{x: x*x 遍历 x 在 [1, 2, 3]}}`", 起点 + 3));
    惯用.push(format!("{}. 异常兜底：`尝试：` … `捕获 文件错误 为 e：` `打印(e.消息)`", 起点 + 4));

    // ---- 已装的包（M149）：全量卡与任务卡带，**单模块分片不带** ----
    // 与 Python `render_ai_card` 里那条 `if 模块 is None` 逐条对应；
    // 空格子也要一起判断（少一个 `\n` 两边的卡片就不等了）。
    let 包段 = if 模块.is_none() {
        let s = 已装的包段();
        if s.is_empty() { String::new() } else { format!("{s}\n\n") }
    } else {
        String::new()
    };

    // ---- 说明那两行（分片形态不同）----
    let 头 = "基石是一门**中文编程语言**，语法对标 Python 的易用性。";
    let 说明 = if let Some(t) = 任务 {
        format!("{头}这是「**{t}**」这类活的分片卡（只带常用模块）。")
    } else if let Some(m) = 模块 {
        format!("{头}这是「**{m}**」模块的分片卡。")
    } else {
        format!("{头}以下是完整速查，")
    };

    Ok(format!(
        "# 基石（jishi）语言卡 v{ver}（{hash}）\n\
         {说明}\n\
         请严格遵守——**只写中文关键字，不要用英文关键字（def/if/print/True…）**。\n\
         \n\
         ## 关键字（全部）\n\
         {kw}\n\
         \n\
         ## 内建函数\n\
         {builtins}\n\
         \n\
         {标准库}\n\
         \n\
         {包段}\
         ## 对象方法\n\
         {methods}\n\
         \n\
         {语法}\n\
         \n\
         {惯用}\n\
         \n\
         {错误}\n",
        ver = D::LANG_VERSION, hash = hash, 说明 = 说明, kw = kw,
        builtins = builtins, 标准库 = 标准库, 包段 = 包段, methods = methods,
        语法 = D::CARD_语法段, 惯用 = 惯用.join("\n"), 错误 = D::CARD_错误段,
    ))
}

/// `ai.卡统计()` 的等价物 —— 各种卡片的大小对比（`--ai-card --统计`）。
///
/// ⚠️ 报的是**字符数**，不是精确 token —— 两边都按这个口径比，不假装知道 tokenizer。
pub fn 卡统计() -> Vec<(String, usize)> {
    let mut 出 = vec![("全量".to_string(), render_ai_card(None, None).unwrap_or_default().chars().count())];
    for (名, _) in D::STDLIB.iter() {
        if ["文本", "表格", "数据库"].contains(名) {
            出.push((format!("模块:{名}"),
                     render_ai_card(Some(名), None).unwrap_or_default().chars().count()));
        }
    }
    for (名, _) in D::CARD_任务表.iter() {
        出.push((format!("任务:{名}"),
                 render_ai_card(None, Some(名)).unwrap_or_default().chars().count()));
    }
    出
}
