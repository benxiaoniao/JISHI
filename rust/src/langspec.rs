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
         ## 对象方法\n\
         {methods}\n\
         \n\
         {语法}\n\
         \n\
         {惯用}\n\
         \n\
         {错误}\n",
        ver = D::LANG_VERSION, hash = hash, 说明 = 说明, kw = kw,
        builtins = builtins, 标准库 = 标准库, methods = methods,
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
