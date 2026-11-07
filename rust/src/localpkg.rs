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
    // 🔴 **沙箱要在展开之前拦「从 本地包」**（M147 对拍撞出来的）：
    //    展开要读包源码（文件访问），而导用守卫是**运行时**才调的 —— 展开一做，
    //    运行时那条 IMPORT 就没了，守卫永远等不到，于是沙箱里报的是
    //    「没有找到本地包」（E0302），而 Python 那边报「沙箱禁止」（E2000）。
    //    两边对同一段源码给出不同的错，是最不该有的分叉。
    for (name, line, col) in jishi_frontend::localpkg::local_imports(program) {
        if let Err(mut e) = crate::check_import_guard(&name, false, true, line, col, filename) {
            // 守卫本身不记位置（运行时那条由 VM 在 IMPORT 指令处补上）——
            // 编译期没有那条指令，所以这里补：不然 `--json-result` 里
            // 同一件事 Python 给 1:1、Rust 给 null，对拍就红。
            if e.line.is_none() {
                e.line = Some(if line > 0 { line } else { 1 });
            }
            if e.col.is_none() {
                e.col = Some(if col > 0 { col } else { 1 });
            }
            return Err(e);
        }
    }
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
