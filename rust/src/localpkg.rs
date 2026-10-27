//! 本地包展开的**宿主编排**（M145）。
//!
//! 真正的展开逻辑在 `jishi_frontend::localpkg`（纯 Rust、零依赖，`core` 的
//! pyo3 扩展也用它）。这个文件只做两件**宿主才有的事**：
//!
//! 1. 把前端返回的 `PkgErr` 翻成 [`JishiError`] —— 类型名与错误码要跟 Python
//!    参考实现**逐字一致**（那边抛 `SemanticFuncError`，`code = E0302`、
//!    `title = 找不到这个函数`）；
//! 2. 让三条命令行路径（直接跑 / `--dump-bytecode` / 新项目模板）**都调到同一份**，
//!    漏一条就会出现「跑得起来、dump 出来的字节码里没有包」这种鬼事。
//!
//! ## 为什么是「编译期展开」而不是「运行时加载」
//!
//! 见 `jishi_frontend::localpkg` 的模块文档 —— 头一版运行时加载在 Rust 上一调用
//! 包里的函数就 `index out of bounds`（函数值只记本模块的代码下标，跨模块必崩）。
//!
//! 代价（与基石库层同一套，写明白免得以后奇怪）：
//! - **「运行时才拼出来的」导入不行** —— 展开只发生在编译期。语言现在没有
//!   动态执行那种内建，所以这条代价目前为零；`IMPORT` 分支留着兜底。
//! - 改了包要**重编译**（包是编进程序的）。

use crate::JishiError;

/// 把程序里所有 `导入 … 从 本地包` 展开掉，返回新的 AST。
///
/// `filename` 只用于兜底（前端返回的错误已带行列）。
pub fn expand(program: &jishi_frontend::parser::Node, filename: &str)
    -> Result<jishi_frontend::parser::Node, JishiError>
{
    jishi_frontend::localpkg::expand(program).map_err(|e| {
        let mut err = crate::err("找不到这个函数", e.message);
        err.line = Some(if e.line > 0 { e.line } else { 1 });
        err.col = Some(if e.col > 0 { e.col } else { 1 });
        err.hint = e.hint;
        // 位置由上面的行列决定；`filename` 交给渲染层（它本来就吃这一份）
        let _ = filename;
        err
    })
}
