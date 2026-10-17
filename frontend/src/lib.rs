//! 基石（jishi）前端核心 —— **纯 Rust、零依赖**。
//!
//! 整条前端链（源码 → 词法 → 语法 AST → 字节码）都在这一个 crate 里：
//!
//! | 模块 | 对应 Python 侧 | 职责 |
//! |---|---|---|
//! | [`tokenizer`] | `jishi/tokenizer.py` | 分词（含插值字符串、全角标点归一化…） |
//! | [`parser`] | `jishi/parser.py` | 语法 → AST（含全部解析期脱糖），AST 用 [`parser::J`] 表示 |
//! | [`compiler`] | `jishi/compiler.py` | AST → 字节码（[`compiler::Module`]） |
//! | [`serialize`] | `jishi/serialize.py` | 字节码 → JSON 文本（前端契约的 `bytecode` 阶段产物） |
//! | [`jishilib`] | `jishi/jishilib.py` | 基石库层（`jishi/stdlib-jishi/*.jsh`）的编译期展开 |
//! | [`opcodes`] / [`sha256`] | `jishi/opcodes.py` / `hashlib` | 指令常量表 / `checksum` 用的哈希 |
//!
//! ## 为什么单独一个 crate（R7.1-a，2026-10-05）
//!
//! 以前这些文件住在 `core/src/` 里 —— 而 `core` 是**编成 Python 扩展**（pyo3）的
//! crate。于是「能解析源码的东西」只有 Python 拿得到：独立宿主 `jishi-rs` 只能吃
//! **已经编译好的字节码 JSON**（R6 的 `--walk` 也是吃 AST JSON）。
//!
//! R7 的目标是「**编辑器不再依赖 Python**」，而 LSP / 格式化 / 静态检查**都必须
//! 自己拿到 AST** —— 所以前端得从 pyo3 扩展里**解耦**出来，让二进制也能用。
//!
//! 📌 **这次搬家是干净的**：这些文件**代码级零 pyo3 用法**（翻了 `tokenizer` /
//! `parser` / `serialize` / `jishilib` / `opcodes` 五个文件，命中的全是注释里的
//! 「与 Python 侧对应关系」说明）；跨模块引用本来就用 `crate::tokenizer::…`，
//! **整体搬到新 crate 根目录后一行引用都不用改**。
//!
//! ## 测试着落
//!
//! 两个消费端的**一致性判据**都在 Python 侧（前端契约四阶段：tokens / ast /
//! bytecode / diagnostics 逐字节比基线），见 `tools/conformance.py`。

pub mod compiler;
pub mod jishilib;
pub mod opcodes;
pub mod parser;
pub mod serialize;
pub mod sha256;
pub mod tokenizer;
