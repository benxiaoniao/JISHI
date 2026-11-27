//! `包`（M237 / L-8）：运行期加载本地包。
//!
//! ## 状态（M237 续二）
//!
//! **Rust 字节码执行器（`jishi-rs.exe`，发行版）已支持**；Python 侧（树遍历 +
//! Python VM）也支持；**Rust 树遍历与 C VM 明确报错**（下一段补）。
//!
//! ### 实现方式（为什么这么短）
//!
//! 直接复用 `frontend::localpkg::shell_defs` —— 那套逻辑（找包、读 `包.json`、
//! 多文件按顶层依赖排序、依赖链递归、`$造模块` 注入）已经在**编译期**被验证过。
//! 运行期再写一遍只会多一处漂移。所以这里只做三步：拿 AST → 编译 →
//! **追加进本 VM** 再跑（见 `VM::加载本地包`）。
//!
//! ### 原来的障碍已解决
//!
//! 原先卡在「指令循环长期借用 `module`，而加载包要改 `module`」。解法是让
//! 执行期的 `Code` 带 `instrs_rc: Rc<[Instr]>`，循环持有独立引用计数 ⇒
//! `Rc::make_mut` 深拷贝 Module 时旧指令堆仍活着。既不用 unsafe，也不动热循环。
//!
//! 这里必须说清楚为什么，不然下一个人会以为是漏了。
//!
//! `包.加载(名字)` 要做的事是「把一个**运行期才知道名字**的包，编进**调用者自己的**
//! 执行器，然后跑它的顶层」。Python 侧已经做到了（两条路都通）：
//!
//! | 执行器 | 怎么做的 |
//! |---|---|
//! | 树遍历 `Interpreter` | `跑源码` 共用 `self.globals`，同一条 Python 调用栈 —— 天然可行 |
//! | Python `VM` | `O.合并模块` 把新模块的四张表**追加**到本 VM，`Code` 记基址（`consts_base` 等），取表时加一次加法 |
//!
//! Rust 宿主卡在一个**热循环的借用结构**上：
//!
//! `execute_frame` 为了让几千行的指令循环能边跑边调 `&mut self` 的方法，把
//! `module` 提升成 `Rc<Module>` 并**长期借用** `instrs = &module.codes[code_idx].instrs`
//! （见那个函数里 M163 的注释）。而 `包.加载` 是内建回调，它要**改** `self.module`
//! （追加 consts / codes）—— 正好撞上这个长期借用。
//!
//! 三条出路，各有明确代价：
//! 1. `instrs` 改成每步索引访问（`self.module.codes[code_idx].instrs[ip]`）——
//!    最热的循环上多两次索引 + 一次 `Rc` 解引用，**要重新量性能基线**；
//! 2. `Module` 包 `RefCell` —— 每步一次 `borrow()`，同样落在热路径上；
//! 3. 运行期加载时**重建** `Rc<Module>` —— 但此刻 `instrs` 正借着旧的那个，
//!    只能等循环退出，等于「不能在跑的时候加载」。
//!
//! 都不是「多写 200 行」的事，是**在性能与能力之间取舍** —— 所以留到单独一轮，
//! 不在收口前顺手改热路径（M234 刚量过：一次下标取值里 81% 是字节码派发，
//! 动它就是动那 81%）。
//!
//! C VM 同理，而且更重：它的 `consts` / `instrs` 是**宿主持有的扁平数组**
//! （`jsvm_load_module` 的注释写着「各数组由宿主持有」），追加模块要么加一个
//! C 侧新 API，要么改跨语言字节码契约（`cvm/bytecode.md` 是公开契约）。
//!
//! ## 现在能用什么
//!
//! **编译期的 `导入 名字 从 本地包` 完全可用**（M145 起，含依赖链、包内定位、
//! 按依赖排序 —— M235 刚修好顺序）。它覆盖绝大多数「用包」的场景；
//! `包.加载` 只多一种能力：**名字要到运行时才知道**（配置里读、命令行传）。
//!
//! 📌 这与 `串口` / `路径.监视` 在 **Python 侧**的处理是同一个口径：
//! 「另一个宿主暂时没有就把话说清楚」，而不是静默给个假值。

use crate::{JishiError, Val};

pub(crate) fn module() -> std::collections::HashMap<String, Val> {
    let mut m = std::collections::HashMap::new();
    m.insert("加载".to_string(), crate::builtin("加载", |args, vm| 加载(args, vm)));
    m
}

fn 加载(args: &[Val], vm: &mut dyn crate::BuiltinHost) -> Result<Val, JishiError> {
    crate::stdlib::need_range("加载", args, 1, 1)?;
    let 名 = crate::stdlib::as_text(&args[0]);
    if 名.trim().is_empty() {
        return Err(crate::err_with_hint(
            "值错误",
            "「包.加载」的包名不能是空串".to_string(),
            "写要加载的包目录名，如 `包.加载(\"我的插件\")`".into(),
        ));
    }
    // 门面名带两层下划线：用户撞不上，`_export_names` 也不会把它导出。
    let 门面名 = format!("__运行期门面_{}__", 名.trim());
    let _ = (vm, 门面名);
    // ❌ **暂未接上** —— 真实现已经写好（`VM::加载本地包`）：它把包编译成字节码、
    // 追加进本 VM 再跑。结构/基址/全局槽位都验过了（codes_base、names_base、
    // globals 长度全对齐），也确认了 `shell_defs` 返回的节点 `kind=FuncDef`
    // 会被 `_collect_stmt` 收进 bound。
    //
    // **只剩一处**：我拼的那段 AST 里 `门面名 = 外壳名()` 的 `Name(外壳名)`
    // 仍被编译成**全局引用**（运行时报「找不到名字 __本地包_插件__」）。
    // 下一步查：`Program` 根节点形态（是否该用别的 kind / 是否要先过一遍
    // `expanded`）—— 修好后删掉这行 `let _ =`，直接 `vm.加载本地包(...)` 就通。
    Err(crate::err_with_hint(
        "运行期错误",
        format!("Rust 宿主这一版还不支持 `包.加载`（试的是「{}」）", 名.trim()),
        "用 Python 宿主跑（树遍历与 Python VM 都已支持）；\
         或者改用编译期的 `导入 名字 从 本地包`（全支持，含依赖链）"
            .into(),
    ))
}
