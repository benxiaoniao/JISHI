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

/// `ai.render_ai_card()` 的等价物 —— Markdown 语言卡（贴进 LLM 系统提示词用）。
///
/// 结构与 Python 侧逐字对应：版本头 → 关键字 → 内建 → 标准库 → 对象方法 →
/// 运算符 → 语法速览 → 惯用法 → 常见错误对照 → 报错怎么读。
pub fn render_ai_card() -> String {
    let hash = content_hash();
    let kw = D::KEYWORDS.iter().map(|(k, _)| *k).collect::<Vec<_>>().join(" ");

    let builtins = D::BUILTINS.iter()
        .map(|(n, doc)| format!("- `{n}`：{doc}"))
        .collect::<Vec<_>>().join("\n");
    let stdlib = D::STDLIB.iter()
        .map(|(m, fns)| {
            let names = fns.iter().map(|(n, _, _)| *n).collect::<Vec<_>>().join("、");
            format!("- `导入 {m}`：{names}")
        })
        .collect::<Vec<_>>().join("\n");
    let methods = D::METHODS.iter()
        .map(|(k, v)| format!("- {k}：{}", v.join("、")))
        .collect::<Vec<_>>().join("\n");

    format!(
        r#"# 基石（jishi）语言卡 v{ver}（{hash}）

基石是一门**中文编程语言**，语法对标 Python 的易用性。以下是完整速查，
请严格遵守——**只写中文关键字，不要用英文关键字（def/if/print/True…）**。

## 关键字（全部）
{kw}

## 内建函数
{builtins}

## 标准库（`导入 模块名`）
{stdlib}

## 对象方法
{methods}

## 运算符
- 算术：`+ - * / // % **`
- 比较：`== != < > <= >=`（可链式：`1 < x < 10`）
- 逻辑：`与 或 非`
- 赋值：`= += -= *= /=`

## 语法速览
- 变量：`令 分数 = 60`；多赋值：`令 a, b = [1, 2]`
- 条件：`如果 分数 >= 90：` / `否则如果 分数 >= 60：` / `否则：`
- 循环：`遍历 x 在 [1, 2, 3]：` / `循环 3 次：` / `当 条件：`
- 函数：`函数 求和(a, b)：`（默认参数 `函数 f(a, b = 10)：`）
- 返回：`返回 a + b`；多值返回 `返回 a, b`（即返回列表）
- 导入：`导入 随机`；Python 桥接 `导入 math 从 python`
- 类：`类 面积：` + `函数 算(自身)：`；实例化 `新建 面积()`
- 异常：`尝试：` / `捕获 值错误 为 e：` / `最终：` / `抛出 值错误("…")`
- 推导式：`[x*2 遍历 x 在 列表 如果 x>2]`、`{{k: v 遍历 ...}}`
- 插值：`` `你好 {{名字}}，今年 {{年龄}} 岁` ``
- 块缩进：**用空格（4 格），禁止 Tab**

## 惯用法示例
1. 遍历统计：`令 总和 = 0` / `遍历 x 在 数据：` / `总和 += x`
2. 条件过滤：`[x 遍历 x 在 数据 如果 x > 0]`
3. 函数+默认参数：`函数 打招呼(名字, 语气 = "你好")：` / `打印(语气, 名字)`
4. 字典构建：`{{x: x*x 遍历 x 在 [1, 2, 3]}}`
5. 异常兜底：`尝试：` … `捕获 文件错误 为 e：` `打印(e.消息)`

## 常见错误对照（写中文，别写英文）
- `def` → `函数`；`if` → `如果`；`for` → `遍历`；`while` → `当`
- `return` → `返回`；`import` → `导入`；`class` → `类`；`try` → `尝试`
- `print` → `打印`；`len` → `长度`；`range` → `范围`；`int` → `整数`
- `True` → `真`；`False` → `假`；`None` → `空`
- `and` → `与`；`or` → `或`；`not` → `非`

## 报错怎么读、怎么查（M56）
- 报错形态：`错误 E0301：找不到这个名字（找不到名字「长渡」）` + 行列 + `提示：你是不是想写「长度」？`
- **报错里的 E 码可以直接查详情**：跑 `jishi 错误 E0301`
  —— 给「什么意思 / 常见成因 / 怎么改」三段。不写码则列出全部错误码。
- 常用异常类型（`捕获 X 为 e` 用，`e.消息` / `e.类型` 可读）：
  `异常`（基类）、`运行期错误`、`类型错误`、`值错误`、`索引错误`、`键错误`、
  `除零错误`、`文件错误`、`断言错误`
- 程序化读错误：加 `--json-errors`，错误会以 JSON 输出（含码、行列、修法建议）。
"#,
        ver = D::LANG_VERSION,
        hash = hash,
        kw = kw,
        builtins = builtins,
        stdlib = stdlib,
        methods = methods,
    )
}
