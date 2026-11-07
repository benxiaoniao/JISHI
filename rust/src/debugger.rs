//! 基石调试器（`jishi/debugger.py` 的移植）—— R7.5。
//!
//! ## 会话与执行器分开
//!
//! 调试会话里真正需要「执行器知识」的只有五件事：**当前帧的深度、名字、变量、
//! 调用栈，以及怎么求一个表达式的值**。其余（断点表、单步模式判定、源码显示、
//! 出错现场）与执行器无关。Python 侧为此把 `_Session` 抽出来，两个执行器
//! （树遍历 / 字节码）共用；这里照搬这个形状：
//!
//! * [`SessionState`]：执行器无关的会话状态与命令；
//! * [`Debugger`]：挂在 Rust **树遍历参考执行器**（`walk.rs`）上的那一份，
//!   实现 [`crate::walk::DebugHooks`] 的三个钩子。
//!
//! 📌 **R7.5 只支持树遍历**（拍板：DAP 落在树遍历参考执行器上 —— 它已经是
//! 「规范」、按节点走，加单步比动 VM 指令循环便宜）。字节码执行器（`lib.rs`
//! 的 VM）暂时不接调试钩子；要接的话，缺的只是「语句起始标记」那一层
//! （Python 侧是编译器打的 `Code.stmt_marks`）。
//!
//! ## 「暂停时谁来读命令」- [`PauseDriver`]
//!
//! 命令行会话从终端读一行；DAP 会话要**一边等命令、一边把编辑器的
//! `stackTrace` / `variables` / `evaluate` 答掉**。两者的差别全收在
//! [`PauseDriver`] 里 —— 会话本身不知道命令从哪来。这也是 Rust 里唯一能做对
//! 这件事的形状：执行状态（`Walker` + `SessionState`）**全在一个线程上**，
//! 所以「暂停期间读状态」不会有跨线程共享的问题（Python 侧靠 GIL + 只读约定）。
//!
//! ## 「退出」为什么是一条专用信号
//!
//! 用户在暂停时按「退出」是想**立刻结束**，而不是让程序里的 `尝试/捕获` 把它
//! 接住 —— 一句 `捕获：` 就能吞掉「退出」的话，用户会觉得调试器失灵。信号本身
//! 用一个不会与用户错误重名的哨兵（`lib.rs::DEBUG_QUIT`），由 `walk.rs` 的
//! `exec_try` / `snapshot_trace` / `fill_loc` 三处显式放行，与 Python 的
//! `_do_try` 逐条对应。效果一样：**`最终` 块照跑，`捕获` 接不住**。
//!
//! ## 已知边界（不粉饰）
//!
//! * **只有树遍历执行器**：`jishi-rs dap --执行器 vm/cvm` 明确报错，
//!   不静默降级（Python 侧三个都有，那是另一条实现）。
//! * **空块 `:` 那一行停不住**：它不是语句（`statement_lines` 里没有它），
//!   与 Python 侧字节码执行器的口径一致。
//! * **块关键字行不能设断点**（`否则：` / `捕获：` / `最终：`）：它们是块的
//!   一部分，不是独立语句。
//! * **从 Python 侧回调进来的函数不会在其中暂停**：Rust 这边不存在这种情况
//!   （没有 Python 回调），但 `列表.映射` 这类**内建回调**也一样不进帧
//!   —— 与 Python 的 M17 限制同形。

use std::cell::RefCell;
use std::collections::BTreeSet;
use std::rc::Rc;

use jishi_frontend::parser::parse_expression;

use crate::json::Json;
use crate::walk::{DebugHooks, Scope, Walker};
use crate::{debug_quit, display, BuiltinHost, JishiError, Val};

/// 暂停时显示的主提示符（与 Python 侧同一串）
pub const PROMPT: &str = "调试 > ";

/// 「这一行为什么停不住」的说明（与 Python 侧**同一句**，逐字）。
pub const DEAD_LINE_HINT: &str =
    "空行 / 注释 / 「否则」「捕获」「最终」这类起头的行——它们是块的一部分，不是独立语句";

/// `帮助` 命令打出来的那张表（与 Python 的 `_HELP_LINES` **逐行同字**）。
pub const HELP_LINES: &[&str] = &[
    "命令（中文或英文别名都行）：",
    "  继续 / c                跑到下一个断点（没有断点就跑完）",
    "  单步 / s                执行下一条语句（会进入被调用的函数）",
    "  下一步 / n              执行到本帧的下一条语句（不进入函数）",
    "  跳出 / o                跑完当前函数，回到调用它的地方",
    "  断点 / b                列出断点；「断点 12」或「断点 12,15」添加",
    "  取消断点 12             删掉某个断点；「清空断点」全删",
    "  变量 / v                看当前帧的变量（「变量 全部」连内建与临时变量）",
    "  查看 表达式 / p         在当前帧里求值，例如「查看 单价 * 数量」",
    "  栈 / bt                 看调用栈（由内到外，带行号）",
    "  源码 [行号] / l         看当前行（或指定行）附近的源码",
    "  帮助 / h                显示这张表",
    "  退出 / q                结束调试会话（程序不再往下跑）",
    "  （直接回车 = 重复上一条命令）",
];

// ---------------------------------------------------------------------------
// 恢复模式
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Mode {
    /// 只用于「停在入口」那一次
    Entry,
    Run,
    Step,
    Next,
    Out,
}

/// 调试器的调用帧：函数名 + **那一帧的作用域**（看变量用）+ 调用点。
#[derive(Clone)]
pub struct DebugFrame {
    pub name: String,
    pub scope: Rc<RefCell<Scope>>,
    pub call_line: i64,
    pub call_col: i64,
}

/// 出错现场的快照（对应 Python 的 `_Snapshot`）。
///
/// 为什么要专门留一份：**错误是语句执行到一半时冒出来的**，等它传到
/// `run()` 时调用帧已经弹干净了 —— 那时再去看「当前帧」，看到的是模块顶层、
/// 变量也可能是上一帧的。所以每执行一条语句就复制一份留底。
/// `vars` 是**当场抄下来的变量表**（帧句柄在弹帧后就失效了）。
struct Snapshot {
    line: i64,
    label: String,
    chain: Vec<(i64, String)>,
    vars: Vec<(String, Val)>,
    /// 出错时是不是停在**模块顶层**那一帧 —— 决定「看变量」要不要把内建也列进去
    /// （见 `Walker::builtin_entries`）。
    at_global: bool,
}

// ---------------------------------------------------------------------------
// 会话（执行器无关）
// ---------------------------------------------------------------------------

/// 一个调试会话的全部状态。
pub struct SessionState {
    pub filename: String,
    pub lines: Vec<String>,
    pub breakpoints: BTreeSet<i64>,
    /// 程序里「可停的语句行」（设断点时用来提示「这行停不住」）
    pub stmt_lines: BTreeSet<i64>,
    pub stop_on_entry: bool,
    /// 前端是不是「敲命令的终端」；false（DAP）时省掉命令行专属的寒暄
    pub interactive: bool,
    /// 「运行中请求暂停」——在下一个语句边界生效
    pub pause_requested: bool,
    pub quit_requested: bool,
    /// 暂停次数（会话结束时报给用户，也是判据里判断「停了几次」的依据）
    pub stops: usize,
    /// 暂停时的回调（DAP 用它发 `stopped` 事件）；命令行会话不用
    pub on_pause: Option<Box<dyn FnMut(&str)>>,
    /// 会话自己的话往哪写（DAP 变成 `output` 事件；命令行写到 stdout）
    pub write_line: Box<dyn FnMut(&str)>,

    frames: Vec<DebugFrame>,
    /// 「当前作用域」——每次 `before_stmt` 更新
    env: Rc<RefCell<Scope>>,
    globals: Rc<RefCell<Scope>>,
    /// 语言自带的全局名字（内建函数 + 异常类型 + `空` + `$` 内部名）。
    ///
    /// **照抄 Python 的 `_builtin_names = set(new_builtins())`** —— 判定「这个名字
    /// 要不要在「看变量」里藏起来」用的是**整套** `new_builtins()` 的键，不是
    /// 只有「内建函数」那一批（`crate::builtin_names()` 把 `$` 内部名、异常类型
    /// 与 `空` 都排掉了，拿它当噪声表会让 `值错误`、`$文本` 这些漏出来）。
    builtin_names: std::collections::HashSet<String>,
    line: i64,
    mode: Mode,
    paused_depth: i64,
    last_cmd: Option<(String, String)>,
    help_shown: bool,
    snapshot: Option<Snapshot>,
}

impl SessionState {
    pub fn new(filename: String, lines: Vec<String>,
               stmt_lines: BTreeSet<i64>, globals: Rc<RefCell<Scope>>,
               breakpoints: Vec<i64>, stop_on_entry: bool, interactive: bool,
               write_line: Box<dyn FnMut(&str)>) -> SessionState {
        let mut s = SessionState {
            filename,
            lines,
            breakpoints: BTreeSet::new(),
            stmt_lines,
            stop_on_entry,
            interactive,
            pause_requested: false,
            quit_requested: false,
            stops: 0,
            on_pause: None,
            write_line,
            frames: Vec::new(),
            env: Rc::clone(&globals),
            globals,
            builtin_names: {
                // `new_builtins()` 的键就是 Python `_builtin_names` 那一套；再补上
                // `空`（Python 把它也放进了 `new_builtins()`，宿主当字面量处理）。
                let mut s: std::collections::HashSet<String> =
                    crate::new_builtins().into_keys().collect();
                s.insert("空".to_string());
                s
            },
            line: 1,
            mode: if stop_on_entry { Mode::Entry } else { Mode::Run },
            paused_depth: 0,
            last_cmd: None,
            help_shown: false,
            snapshot: None,
        };
        s.add_breakpoints(&breakpoints);
        s
    }

    // -- 断点 ---------------------------------------------------------------

    /// 加断点；返回 `(能停, 停不住)` 两组行号。
    ///
    /// 「停不住」不是错误（行号可能写在空行、注释行、`否则` 行上），但必须
    /// **说出来** —— 否则用户看着断点列表里明明有第 12 行、程序却从不停。
    pub fn add_breakpoints(&mut self, nums: &[i64]) -> (Vec<i64>, Vec<i64>) {
        let (mut usable, mut dead) = (Vec::new(), Vec::new());
        for n in nums {
            self.breakpoints.insert(*n);
            if self.stmt_lines.contains(n) {
                usable.push(*n);
            } else {
                dead.push(*n);
            }
        }
        // Python 的 `add_breakpoints` 返回的是**排过序**的（`sorted(usable)`）——
        // 命令行的「已设断点：第 3 行、第 12 行」要照这个顺序印。
        usable.sort_unstable();
        dead.sort_unstable();
        (usable, dead)
    }

    pub fn clear_breakpoints(&mut self) {
        self.breakpoints.clear();
    }

    // -- 执行器要知道的几件事（由 `Debugger` 的钩子喂进来）-------------------

    /// 记录「此刻在第几行、哪个帧、有哪些变量」。
    fn set_statement(&mut self, line: i64, scope: &Rc<RefCell<Scope>>) {
        self.line = line;
        self.env = Rc::clone(scope);
        let snap = Snapshot {
            line,
            label: self.frame_label(),
            chain: self.live_chain(),
            vars: self.vars_of(scope),
            at_global: Rc::ptr_eq(scope, &self.globals),
        };
        self.snapshot = Some(snap);
    }

    pub fn depth(&self) -> i64 {
        self.frames.len() as i64
    }

    pub fn frame_label(&self) -> String {
        match self.frames.last() {
            Some(f) => format!("函数「{}」", f.name),
            None => "模块顶层".to_string(),
        }
    }

    /// 调用栈：由内到外的 `(行号, 在哪)`。与 Python 的 `_live_chain` 逐句对应。
    pub fn live_chain(&self) -> Vec<(i64, String)> {
        if self.frames.is_empty() {
            return vec![(self.line, "模块顶层".to_string())];
        }
        let n = self.frames.len();
        let mut chain = vec![(self.line, format!("函数「{}」", self.frames[n - 1].name))];
        // `frames` 是**由外到内**存的，而每帧只记了自己的调用点，所以相邻两层
        // 要错位配对；最外层函数的调用点在模块顶层。
        for j in (1..n).rev() {
            chain.push((self.frames[j].call_line,
                        format!("函数「{}」", self.frames[j - 1].name)));
        }
        chain.push((self.frames[0].call_line, "模块顶层".to_string()));
        chain
    }

    /// 按调用栈下标取作用域（0 = 当前帧）。`None` = 没有这一层。
    pub fn scope_at(&self, index: usize) -> Option<Rc<RefCell<Scope>>> {
        if index < self.frames.len() {
            return Some(Rc::clone(&self.frames[self.frames.len() - 1 - index].scope));
        }
        if index == self.frames.len() {
            return Some(Rc::clone(&self.globals));   // 模块顶层
        }
        None
    }

    /// 某一层的变量表（名字 → 值）。
    pub fn vars_of(&self, scope: &Rc<RefCell<Scope>>) -> Vec<(String, Val)> {
        let mut out = scope.borrow().own_vars();
        // 稳定顺序：DAP 那边也是按名字排序输出，先排好省得两处各排一遍
        out.sort_by(|a, b| a.0.cmp(&b.0));
        out
    }

    /// 这个全局名字要不要在「看变量」里默认藏起来（**与 Python 侧同一口径**）。
    ///
    /// * `__` 开头的是**脱糖留下的临时变量**（`用 … 为` 的 `__用_N__`、
    ///   `匹配` 的 `__匹配_N__`），对用户没有意义；
    /// * 内建函数与异常类型（`长度`、`打印`、`值错误`…）在 Python 侧的模块环境
    ///   里也在，一次冒出三十多个会把用户的变量挤到屏幕外；想看用「变量 全部」。
    ///
    /// ⚠️ Rust 树遍历执行器的**内建不在模块作用域里**（`Walker::builtins` 是
    /// 另一张表），所以这一条在本机多半是空操作 —— 但在**模块顶层**那一帧，
    /// `write_var_block` 会把内建并进显示列表（见 `Walker::builtin_entries`），
    /// 于是这条判定在那儿真的起作用，与 Python 的模块环境同形。
    pub fn is_noise(&self, name: &str) -> bool {
        name.starts_with("__") || self.builtin_names.contains(name)
    }

    // -- 暂停与命令 ---------------------------------------------------------

    /// 该不该停？返回暂停原因（`None` = 继续跑）。
    fn should_pause(&mut self) -> Option<String> {
        if self.pause_requested {
            // 「暂停」请求优先：用户刚点的按钮，就地停下最符合预期
            self.pause_requested = false;
            return Some("暂停".to_string());
        }
        match self.mode {
            Mode::Entry => Some("停在入口".to_string()),
            Mode::Step => Some("单步".to_string()),
            Mode::Next if self.depth() <= self.paused_depth =>
                Some("下一步".to_string()),
            Mode::Out if self.depth() < self.paused_depth =>
                Some("跳出".to_string()),
            Mode::Run if self.breakpoints.contains(&self.line) =>
                Some(format!("命中第 {} 行的断点", self.line)),
            _ => None,
        }
    }

    /// 停下来的前半段：报事件 + 打三行（Python 的 `_Session._pause` 前半）。
    fn begin_pause(&mut self, reason: &str) {
        self.stops += 1;
        self.paused_depth = self.depth();
        if let Some(cb) = self.on_pause.as_mut() {
            cb(reason);
        }
        (self.write_line)("");
        let label = self.frame_label();
        (self.write_line)(&format!("暂停（{reason}） 第 {} 行 · {label}", self.line));
        let src = self.source_display_line(self.line);
        (self.write_line)(&src);
        if self.interactive && !self.help_shown {
            self.help_shown = true;
            (self.write_line)("（输入「帮助」看命令）");
        }
    }

    /// 解析一条命令 → `(规范名, 参数)`；`None` = 不认识（**已经报过一句**）。
    ///
    /// 空输入返回 `"重复"`，由 [`SessionState::apply_command`] 去查上一条命令 ——
    /// 与 Python 的 `_parse_command` 逐步对应（别名先归一，再查命令表）。
    fn parse_command(&mut self, raw: &str) -> (Option<String>, String) {
        let text = raw.trim();
        if text.is_empty() {
            return (Some("重复".to_string()), String::new());
        }
        // `split(None, 1)` 会把全角空格（中文输入法下很常见）也当分隔符 ——
        // Rust 的 `splitn(2, char::is_whitespace)` 认 Unicode 空白，口径一致。
        let mut it = text.splitn(2, char::is_whitespace);
        let head = alias(it.next().unwrap_or(""));
        let arg = it.next().unwrap_or("").trim().to_string();
        if is_command(&head) {
            (Some(head), arg)
        } else {
            (self.write_line)(&format!("不认识这个命令：「{head}」——输入「帮助」看有哪些"));
            (None, String::new())
        }
    }

    /// 一条命令 → 新的恢复模式（`None` = 继续待命）。
    ///
    /// 命令表与 Python 的 `_COMMANDS` **同宽**（继续 / 单步 / 下一步 / 跳出 /
    /// 断点 / 取消断点 / 清空断点 / 变量 / 查看 / 栈 / 源码 / 帮助 / 退出）：
    /// DAP 只会发前五个与「退出」，其余是命令行专用的（`jishi-rs 调试`）。
    pub fn apply_command(&mut self, raw: &str, walker: &mut Walker) -> Option<Mode> {
        let cmd = match self.parse_command(raw) {
            (Some(c), arg) => {
                if c == "重复" {
                    match self.last_cmd.clone() {
                        Some(prev) => prev,
                        None => {
                            (self.write_line)("（还没有上一条命令）");
                            return None;
                        }
                    }
                } else {
                    self.last_cmd = Some((c.clone(), arg.clone()));
                    (c, arg)
                }
            }
            (None, _) => return None,
        };
        match self.run_command(&cmd.0, &cmd.1, walker) {
            Ok(mode) => mode,
            // 命令本身出错（多半是「查看」里的表达式）不该把调试会话掀翻：
            // 报一句中文错误，继续待命（与 Python 的 `except JishiError` 同口径）。
            Err(e) => {
                let mut line = format!("错误：{}", crate::error_title(&e.type_name));
                if !e.message.is_empty() {
                    line.push_str(&format!("（{}）", e.message));
                }
                (self.write_line)(&line);
                if let Some(h) = &e.hint {
                    (self.write_line)(&format!("  提示：{h}"));
                }
                None
            }
        }
    }

    /// 各命令的实现（恢复类返回模式，诊断类返回 `None` 表示继续待命）。
    fn run_command(&mut self, head: &str, arg: &str,
                   walker: &mut Walker) -> Result<Option<Mode>, JishiError> {
        match head {
            "继续" => return Ok(Some(Mode::Run)),
            "单步" => return Ok(Some(Mode::Step)),
            "下一步" => return Ok(Some(Mode::Next)),
            "跳出" => {
                if self.depth() == 0 {
                    (self.write_line)("（已经在最外层了，按「继续」处理）");
                    return Ok(Some(Mode::Run));
                }
                return Ok(Some(Mode::Out));
            }
            "退出" => {
                self.quit_requested = true;
                // 由 `Debugger::before_stmt` 转成调试退出信号
                return Ok(Some(Mode::Run));
            }
            "断点" => self.cmd_break(arg),
            "取消断点" => self.cmd_unbreak(arg),
            "清空断点" => self.cmd_clear(),
            "变量" => self.cmd_vars(arg, walker),
            "查看" => self.cmd_eval(arg, walker)?,
            "栈" => self.dump_stack(None),
            "源码" => self.cmd_source(arg),
            "帮助" => {
                for l in HELP_LINES {
                    (self.write_line)(l);
                }
            }
            _ => {}
        }
        Ok(None)
    }

    fn cmd_break(&mut self, arg: &str) {
        if arg.is_empty() {
            if self.breakpoints.is_empty() {
                (self.write_line)("（还没有断点，用「断点 行号」加）");
                return;
            }
            (self.write_line)("断点：");
            for n in self.breakpoints.iter().copied().collect::<Vec<_>>() {
                if self.stmt_lines.contains(&n) {
                    (self.write_line)(&format!("  第 {n} 行"));
                } else {
                    (self.write_line)(&format!("  第 {n} 行  ← 这一行没有可停的语句，不会停"));
                }
            }
            return;
        }
        let nums = match parse_lines(std::slice::from_ref(&arg.to_string())) {
            Ok(n) => n,
            Err(bad) => {
                (self.write_line)(&format!("看不懂这个行号：「{bad}」\
                     ——写成「断点 12」，多个用逗号或空格隔开"));
                return;
            }
        };
        if nums.is_empty() {
            (self.write_line)("（没写行号）");
            return;
        }
        let (usable, dead) = self.add_breakpoints(&nums);
        if !usable.is_empty() {
            (self.write_line)(&format!("已设断点：{}", nums_join(&usable)));
        }
        if !dead.is_empty() {
            (self.write_line)(&format!(
                "提示：{} 上没有可停的语句（{DEAD_LINE_HINT}）——这个断点不会触发",
                nums_join(&dead)));
        }
    }

    fn cmd_unbreak(&mut self, arg: &str) {
        if arg.is_empty() {
            (self.write_line)("用法：取消断点 行号（一次删一个，多个用逗号隔开）");
            return;
        }
        let nums = match parse_lines(std::slice::from_ref(&arg.to_string())) {
            Ok(n) => n,
            Err(bad) => {
                (self.write_line)(&format!("看不懂这个行号：「{bad}」"));
                return;
            }
        };
        let gone: Vec<i64> = nums.iter().copied()
            .filter(|n| self.breakpoints.contains(n)).collect();
        for n in &gone {
            self.breakpoints.remove(n);
        }
        if gone.is_empty() {
            (self.write_line)("（这几个行号上本来就没有断点）");
        } else {
            (self.write_line)(&format!("已删除：{}", nums_join(&gone)));
        }
    }

    fn cmd_clear(&mut self) {
        let n = self.breakpoints.len();
        self.clear_breakpoints();
        (self.write_line)(&format!("已清空 {n} 个断点"));
    }

    fn cmd_vars(&mut self, arg: &str, walker: &mut Walker) {
        let show_all = matches!(arg.trim(), "全部" | "all" | "-a");
        let vars = self.vars_of(&Rc::clone(&self.env));
        let label = self.frame_label();
        let at_global = Rc::ptr_eq(&self.env, &self.globals);
        self.write_var_block(&label, &vars, show_all, at_global, walker);
    }

    fn cmd_eval(&mut self, arg: &str, walker: &mut Walker) -> Result<(), JishiError> {
        let text = arg.trim();
        if text.is_empty() {
            (self.write_line)("用法：查看 表达式（例如 查看 单价 * 数量）");
            return Ok(());
        }
        let node = parse_expr(text)?;
        let scope = Rc::clone(&self.env);
        let value = walker.eval_in_scope(&node, &scope)?;
        let shown = Debugger::fmt_value(walker, &value);
        (self.write_line)(&format!("  {text} = {shown}"));
        Ok(())
    }

    fn cmd_source(&mut self, arg: &str) {
        let mut center = self.line;
        let a = arg.trim();
        if !a.is_empty() {
            match a.parse::<i64>() {
                Ok(n) => center = n,
                Err(_) => {
                    (self.write_line)("用法：源码 [行号]（不写就按当前行）");
                    return;
                }
            }
        }
        let span = 5;
        let lo = 1.max(center - span);
        let hi = (self.lines.len() as i64).min(center + span);
        (self.write_line)(&format!("源码（第 {lo}–{hi} 行，标记的是第 {center} 行）："));
        if hi < lo {
            return;
        }
        let width = self.lines.len().max(1).to_string().len();
        for n in lo..=hi {
            let text = self.lines[(n - 1) as usize].clone();
            (self.write_line)(&number_line(n, &text, width, center));
        }
    }

    /// 打一块变量（`变量` 命令与「出错现场」共用一份行格式）。
    ///
    /// `at_global` = 这一帧是**模块顶层**：Python 的模块环境里放着整套内建/异常
    /// 类型，树遍历的内建在另一张表里 —— 这里**只为了显示**把它们并进来
    /// （不往作用域里塞东西，见 `Walker::builtin_entries`）。
    fn write_var_block(&mut self, label: &str, vars: &[(String, Val)],
                       show_all: bool, at_global: bool, walker: &mut Walker) {
        (self.write_line)(&format!("当前帧（{label}）的变量："));
        let mut all: Vec<(String, Val)> = vars.to_vec();
        if at_global {
            // 用户名字与内建重名时（`令 打印 = 1`）只留用户那个 —— 与 Python 的
            // 模块环境「后写覆盖」一致，不会出现两个同名条目。
            let have: std::collections::HashSet<String> =
                vars.iter().map(|(k, _)| k.clone()).collect();
            for (k, v) in walker.builtin_entries() {
                if !have.contains(&k) {
                    all.push((k, v));
                }
            }
            // ⚠️ Python 的模块环境里还有一个 `空`（`new_builtins()` 把字面量也放进去了）。
            // 宿主把 `空` 当**字面量**处理、不进内建表，所以这里显式补上 —— 纯显示，
            // 不改任何作用域（不补的话 `变量 全部` 会少一行 `空 = 空`）。
            if !have.contains("空") {
                all.push(("空".to_string(), Val::None_));
            }
        }
        all.sort_by(|a, b| a.0.cmp(&b.0));
        let mut shown: Vec<(&String, &Val)> = Vec::new();
        let mut hidden = false;
        for (name, value) in &all {
            if !show_all && self.is_noise(name) {
                hidden = true;
            } else {
                shown.push((name, value));
            }
        }
        if shown.is_empty() {
            (self.write_line)("  （没有）");
        }
        for (name, value) in shown {
            let s = Debugger::fmt_value(walker, value);
            (self.write_line)(&format!("  {name} = {s}"));
        }
        if hidden {
            // 不报具体条数：各执行器「看得见的全局」本来就不同，报数字会让记录
            // 对不上，而这个数字对用户没有意义（M40）。
            (self.write_line)("  （另有内建/临时变量没显示，要看用「变量 全部」）");
        }
    }

    // -- 输出片段 -----------------------------------------------------------

    /// 当前行（或指定行）带行号的源码，`→` 标出当前行。
    fn source_display_line(&self, n: i64) -> String {
        let text = if n >= 1 && (n as usize) <= self.lines.len() {
            self.lines[(n - 1) as usize].clone()
        } else {
            String::new()
        };
        let width = self.lines.len().max(1).to_string().len();
        format!("→ {n:>width$} │ {text}", width = width)
    }

    pub fn line(&self) -> i64 {
        self.line
    }

    /// 出错时按**调试视角**打一份现场快照（出错行 + 当前帧变量 + 调用栈）。
    ///
    /// 与 `JishiError.render()` 的分工：那个是给所有人看的「错误 + 源码 + ^」，
    /// 这里补的是程序员此刻最想知道的那句「**变量是什么、我是从哪儿调过来的**」。
    pub fn report_error(&mut self, walker: &mut Walker, err: &JishiError) {
        let line = err.line.unwrap_or(self.line);
        (self.write_line)("");
        let snap = self.snapshot.take();
        let where_ = match &snap {
            Some(s) => format!("（最后执行到 第 {} 行 · {}）", s.line, s.label),
            None => String::new(),
        };
        (self.write_line)(&format!("程序在第 {line} 行出错了 —— 出错时的现场{where_}："));
        let Some(snap) = snap else {
            (self.write_line)("（错误发生在任何语句之前，没有变量快照）");
            return;
        };
        // `frozen` 是「出错现场」抄下来的那份变量（弹帧后就失效了，见 `Snapshot`）。
        self.write_var_block(&snap.label, &snap.vars, false, snap.at_global, walker);
        self.dump_stack(Some(&snap.chain));
        (self.write_line)("（想复盘就改脚本、带上同一组断点再跑一次）");
    }

    fn dump_stack(&mut self, chain: Option<&Vec<(i64, String)>>) {
        (self.write_line)("调用栈（由内到外）：");
        let owned;
        let chain = match chain {
            Some(c) => c,
            None => {
                owned = self.live_chain();
                &owned
            }
        };
        for (i, (line, label)) in chain.iter().enumerate() {
            (self.write_line)(&format!("  #{i} 第 {line:>4} 行  {label}"));
        }
    }
}

/// 命令别名 → 规范名（让 `c` / `s` / `n` / `p` / `bt` 这些 gdb 习惯也能用）。
/// 与 Python 的 `_ALIASES` **逐条同字**（含「求值」→「查看」、「调用栈」→「栈」）。
fn alias(head: &str) -> String {
    match head {
        "c" | "cont" | "continue" | "r" | "run" => "继续",
        "s" | "step" => "单步",
        "n" | "next" => "下一步",
        "o" | "out" | "finish" => "跳出",
        "b" | "break" => "断点",
        "v" | "local" | "locals" => "变量",
        "p" | "print" | "求值" => "查看",
        "bt" | "w" | "where" | "调用栈" => "栈",
        "l" | "list" => "源码",
        "h" | "?" | "help" => "帮助",
        "q" | "quit" | "exit" => "退出",
        "d" | "delete" => "取消断点",
        other => other,
    }
    .to_string()
}

/// 这个（已归一的）名字是不是一条真命令。与 Python 的 `_COMMANDS` 同宽。
fn is_command(head: &str) -> bool {
    matches!(head,
        "继续" | "单步" | "下一步" | "跳出" | "断点" | "取消断点" | "清空断点"
        | "变量" | "查看" | "栈" | "源码" | "帮助" | "退出")
}

/// 把「12,15」「12 15」这类行号写法解析成整数列表（中文逗号也认）。
///
/// ⚠️ 与 Python 的 `parse_lines` 同口径：**非数字段报错**（返回那一段原文，由
/// 调用方拼中文提示），不是静默跳过。全角数字（`１２`）在 Python 侧 `isdigit()`
/// 为真、`int()` 也认 —— 这里先把全角归一成半角再解析，保住这条。
pub fn parse_lines(spec: &[String]) -> Result<Vec<i64>, String> {
    let mut out = Vec::new();
    for item in spec {
        let text: String = item.chars().map(fullwidth_digit).collect::<String>()
            .replace('，', ",").replace(',', " ");
        for piece in text.split_whitespace() {
            if piece.is_empty() || !piece.chars().all(|c| c.is_ascii_digit()) {
                return Err(piece.to_string());
            }
            match piece.parse::<i64>() {
                Ok(n) => out.push(n),
                Err(_) => return Err(piece.to_string()),
            }
        }
    }
    Ok(out)
}

/// 全角数字 `０-９` → 半角 `0-9`（其余原样）。
fn fullwidth_digit(c: char) -> char {
    match c {
        '\u{FF10}'..='\u{FF19}' => char::from_u32(c as u32 - 0xFF10 + '0' as u32).unwrap_or(c),
        _ => c,
    }
}

/// `第 3 行、第 12 行`（断点提示两处共用一份措辞）。
fn nums_join(nums: &[i64]) -> String {
    nums.iter().map(|n| format!("第 {n} 行")).collect::<Vec<_>>().join("、")
}

/// 带行号的一行源码：`→  9 │ 令 总额 = 单价 * 数量`（当前行用 `→` 标出）。
fn number_line(n: i64, text: &str, width: usize, current: i64) -> String {
    let mark = if n == current { "→" } else { " " };
    format!("{mark} {n:>width$} │ {text}", width = width)
}

// ---------------------------------------------------------------------------
// 「暂停时谁来读命令」
// ---------------------------------------------------------------------------

/// 暂停期间取命令的来源。会话本身不知道命令从哪来 —— 命令行是终端，
/// DAP 是协议报文（而且要顺手把编辑器的读请求答掉）。
pub trait PauseDriver {
    /// 程序**跑着**的时候顺手处理已排队的协议消息（非阻塞）。
    ///
    /// 命令行会话不需要（返回即可）；DAP 靠它收 `pause` / `disconnect` ——
    /// 它们在**下一个语句边界**生效（这本就是调试器按语句停的语义）。
    fn poll(&mut self, sess: &mut SessionState, walker: &mut Walker);

    /// 暂停时取一条命令（**阻塞**，直到拿到为止）。
    fn next_command(&mut self, sess: &mut SessionState, walker: &mut Walker) -> String;
}

/// 命令行驱动：从一段预置脚本或标准输入取命令。
///
/// `out` 是**原样写出**（不自带换行）—— 提示符在真终端模式下不带换行、命令行
/// 回显才带，两种粒度不同（与 Python 的 `_default_read_line` / `_read_command`
/// 逐个对应）。`jishi-rs 调试` 的 `--命令` 就是用 `script` 这条路的。
pub struct LineDriver {
    script: Option<Vec<String>>,
    script_pos: usize,
    out: Box<dyn FnMut(&str)>,
}

impl LineDriver {
    /// `script` 给 `Some` 时按顺序自动执行（用完自动「继续」）—— 判据就是这么
    /// 跑整场会话的，不需要真终端。
    pub fn new(script: Option<Vec<String>>, out: Box<dyn FnMut(&str)>) -> LineDriver {
        LineDriver { script, script_pos: 0, out }
    }
}

impl PauseDriver for LineDriver {
    fn poll(&mut self, _sess: &mut SessionState, _walker: &mut Walker) {}

    fn next_command(&mut self, _sess: &mut SessionState, _walker: &mut Walker) -> String {
        if self.script.is_some() {
            if let Some(cmd) = self.script.as_ref().and_then(|l| l.get(self.script_pos)) {
                let cmd = cmd.trim().to_string();
                self.script_pos += 1;
                (self.out)(&format!("{PROMPT}{cmd}\n"));
                return cmd;
            }
            (self.out)(&format!("{PROMPT}（命令用完了，自动继续）\n"));
            return "继续".to_string();
        }
        // 真终端 / 管道：打提示符（**不带换行**，与 Python 同）、读一行。
        // 管道输入读到就自己回显一遍：终端会替用户回显，管道不会，不回显的话
        // 记录里只剩提示符和输出挤在一行，读起来前言不搭后语（M37 实测）。
        (self.out)(PROMPT);
        use std::io::{BufRead, IsTerminal};
        let mut line = String::new();
        let stdin = std::io::stdin();
        let n = stdin.lock().read_line(&mut line).unwrap_or(0);
        if n == 0 {
            (self.out)("（输入结束了）\n");
            return "退出".to_string();
        }
        let text = line.trim_end_matches(['\n', '\r']).to_string();
        if !stdin.is_terminal() {
            (self.out)(&format!("{text}\n"));
        }
        text
    }
}

// ---------------------------------------------------------------------------
// 挂在树遍历执行器上的调试器
// ---------------------------------------------------------------------------

/// 会话状态 + 命令来源 —— 这两半合起来才是挂在 `Walker` 上的那个钩子。
pub struct Debugger {
    /// ⚠️ `Rc<RefCell<_>>` 而不是直接持有：DAP 适配器在**执行之外**也要读会话
    /// （程序跑完后报「暂停了几次」、`setBreakpoints` 要在启动前就登记断点）。
    pub sess: Rc<RefCell<SessionState>>,
    driver: Box<dyn PauseDriver>,
}

impl Debugger {
    pub fn new(sess: Rc<RefCell<SessionState>>, driver: Box<dyn PauseDriver>) -> Debugger {
        Debugger { sess, driver }
    }

    /// 取值的顶层文本（走执行器那份 `format_value`，自定义对象的「文本()」也在）。
    fn fmt_value(walker: &mut Walker, v: &Val) -> String {
        // 注意：只为了让「变量」那一行显示得与 Python 一样。格式化失败（极少）
        // 就退回 `display`，绝不因为「显示不出来」把会话掀翻。
        walker.format_value(v, true).unwrap_or_else(|_| display(v))
    }

    /// 暂停的后半段：取命令 → 应用到拿到恢复模式为止。
    fn pause_loop(&mut self, walker: &mut Walker, reason: &str) {
        self.sess.borrow_mut().begin_pause(reason);
        loop {
            // 「退出」走的是**信号**而不是普通恢复模式：用户按它是想要立刻结束，
            // 一句 `捕获：` 不该把它吞掉（见 `lib.rs::DEBUG_QUIT`）。
            let raw = {
                let mut s = self.sess.borrow_mut();
                self.driver.next_command(&mut *s, walker)
            };
            let mut s = self.sess.borrow_mut();
            let mode = s.apply_command(&raw, walker);
            if s.quit_requested {
                return;
            }
            if let Some(m) = mode {
                s.mode = m;
                return;
            }
        }
    }
}

impl DebugHooks for Debugger {
    fn before_stmt(&mut self, walker: &mut Walker, stmt: &Json,
                   scope: &Rc<RefCell<Scope>>) -> Result<(), JishiError> {
        // ① 跑着的时候也要能收 `pause` / `disconnect`（非阻塞）
        self.driver.poll(&mut self.sess.borrow_mut(), walker);
        let line = stmt.get("line").and_then(|v| v.as_i64()).unwrap_or(1);
        // ② 留一份现场（错误可能在**这条语句执行到一半**时冒出来）
        self.sess.borrow_mut().set_statement(line, scope);
        // ③ 判定
        let reason = match self.sess.borrow_mut().should_pause() {
            Some(r) => r,
            None => return Ok(()),
        };
        self.pause_loop(walker, &reason);
        if self.sess.borrow().quit_requested {
            return Err(debug_quit());
        }
        Ok(())
    }

    fn push_frame(&mut self, name: &str, scope: &Rc<RefCell<Scope>>,
                  call_line: i64, call_col: i64) {
        self.sess.borrow_mut().frames.push(DebugFrame {
            name: name.to_string(),
            scope: Rc::clone(scope),
            call_line,
            call_col,
        });
    }

    fn pop_frame(&mut self) {
        self.sess.borrow_mut().frames.pop();
    }
}

// ---------------------------------------------------------------------------
// 跑起来
// ---------------------------------------------------------------------------

/// 跑一场调试会话；返回 `Some(err)` = 程序出错（调用方自己决定怎么呈现）。
///
/// ⚠️ **调试器不改语义**：钩子只读（读语句、读作用域），暂停时也不动任何状态。
/// 所以「带调试器跑」与「不带调试器跑」的输出必须逐字节相同 —— 有测试钉住。
pub fn run(walker: &mut Walker, ast: &Json, dbg: Debugger) -> Option<JishiError> {
    let sess = Rc::clone(&dbg.sess);
    walker.set_debugger(Some(Box::new(dbg)));
    let r = walker.run(ast);
    walker.set_debugger(None);
    match r {
        Ok(()) => {
            let mut s = sess.borrow_mut();
            if s.quit_requested {
                (s.write_line)("（调试会话已结束，程序没有跑完）");
            } else {
                let n = s.stops;
                (s.write_line)(&format!("（程序正常结束，本次共暂停 {n} 次）"));
            }
            None
        }
        Err(e) if e.is_debug_quit() => {
            let mut s = sess.borrow_mut();
            s.quit_requested = true;
            (s.write_line)("（调试会话已结束，程序没有跑完）");
            None
        }
        Err(e) => {
            sess.borrow_mut().report_error(walker, &e);
            Some(e)
        }
    }
}

/// 程序里所有**语句**的起始行（只有这些行能设断点）。
///
/// 与 Python 的 `statement_lines` 同一件事：**值可以是节点、列表或普通值**，
/// 递归遍历一棵 AST，收下语句类节点的 `line`。
///
/// 📌 **不写死「每种节点有哪些子节点」是有意的**：AST 每加一种节点，写死的
/// 遍历就得跟着改，漏一处就成了「某些行的断点永远不停」—— 一个非常难查的问题。
/// 这里是按 JSON 的每个字段递归，与 Python 按 dataclass 字段递归等价。
pub fn statement_lines(ast: &Json) -> BTreeSet<i64> {
    let mut out = BTreeSet::new();
    collect_stmt_lines(ast, &mut out);
    out
}

fn collect_stmt_lines(v: &Json, out: &mut BTreeSet<i64>) {
    match v {
        Json::Obj(m) => {
            let kind = m.get("类型").and_then(|k| k.as_str());
            if let Some(k) = kind {
                if is_stmt_kind(k) {
                    if let Some(Json::Int(line)) = m.get("line") {
                        if *line > 0 {
                            out.insert(*line as i64);
                        }
                    }
                }
            }
            for (_, val) in m.iter() {
                collect_stmt_lines(val, out);
            }
        }
        Json::Arr(items) => {
            for it in items {
                collect_stmt_lines(it, out);
            }
        }
        _ => {}
    }
}

/// 只有语句才有「可停的行」。表达式节点也有 line/col，但断点落在它们上面是
/// 没有意义的（一个语句里可能有好几个表达式、而且不一定会被求值）。
///
/// ⚠️ 这张清单必须与 Python 的 `debugger._STMT_TYPES` **同宽**：多一个会让
/// 「这一行停不住」的提示少报，少一个会让断点静默失效。
fn is_stmt_kind(kind: &str) -> bool {
    matches!(kind,
        "Assign" | "ExprStmt" | "If" | "For" | "Loop" | "While" | "Break"
        | "Continue" | "FuncDef" | "ClassDef" | "Return" | "Raise" | "Try"
        | "Import" | "Pass")
}

/// 调试器「查看 表达式」：解析一小段表达式（**不是**一整段程序）。
///
/// 与 Python 的 `parser.parse_expression` 同一个入口（前端就有这个 API，
/// 不是「伸手进解析器的私有方法」—— 那种写法改解析器时容易漏掉它）。
/// 多余的内容（`查看 x y`）由解析器明确报错：静默只看前半截会让人以为整句都算过了。
pub fn parse_expr(text: &str) -> Result<Json, JishiError> {
    let node = parse_expression(text, "<调试>")
        .map_err(|e| crate::sandbox::parse_err_to_jishi(&e))?;
    Ok(crate::ast_node_to_json(&node))
}

// ---------------------------------------------------------------------------
// `jishi-rs 调试`（交互式命令行）
// ---------------------------------------------------------------------------

/// 往标准输出**原样**写一段文本（不加换行）—— 命令行提示符与回显用。
fn raw_stdout(text: &str) {
    use std::io::Write;
    let mut so = std::io::stdout();
    let _ = so.write_all(text.as_bytes());
    let _ = so.flush();
}

/// 往标准输出写一行（自带换行）—— 会话的 `write_line`（与 Python 的
/// `_default_write` 同口径：写一行并 flush，保证「调试器的话」与程序输出顺序正确）。
fn line_stdout(text: &str) {
    raw_stdout(&format!("{text}\n"));
}

/// `jishi-rs 调试 <文件> [--断点 N] [--不停在入口] [--命令 "..."] [--执行器 树遍历]`。
///
/// 与 Python 的 `_cmd_debug` 逐步对应（默认执行器就是**树遍历**，会话输出逐字相同）。
/// ⚠️ **只支持树遍历**：`--执行器 vm/cvm` 明确拒绝（退码 2），不静默降级
/// —— 字节码执行器的调试钩子还没搬过来（与 `jishi-rs dap` 同一条登记）。
pub fn run_debug(rest: &[String]) -> i32 {
    // ---- 解析参数（对齐 Python 的 argparse 默认值）--------------------------
    let mut file: Option<String> = None;
    let mut bp_specs: Vec<String> = Vec::new();
    let mut no_entry = false;
    let mut commands: Option<String> = None;
    let mut engine: Option<String> = None;
    let mut i = 0;
    while i < rest.len() {
        let a = rest[i].as_str();
        match a {
            "-b" | "--断点" => {
                i += 1;
                match rest.get(i) {
                    Some(v) => bp_specs.push(v.clone()),
                    None => { eprintln!("--断点 需要一个行号"); return 2; }
                }
            }
            "--不停在入口" => no_entry = true,
            "--命令" => {
                i += 1;
                match rest.get(i) {
                    Some(v) => commands = Some(v.clone()),
                    None => { eprintln!("--命令 需要一个命令串"); return 2; }
                }
            }
            "--执行器" => {
                i += 1;
                match rest.get(i) {
                    Some(v) => engine = Some(v.clone()),
                    None => { eprintln!("--执行器 需要一个值"); return 2; }
                }
            }
            _ => {
                if let Some(v) = a.strip_prefix("--断点=") {
                    bp_specs.push(v.to_string());
                } else if let Some(v) = a.strip_prefix("--命令=") {
                    commands = Some(v.to_string());
                } else if let Some(v) = a.strip_prefix("--执行器=") {
                    engine = Some(v.to_string());
                } else if file.is_none() {
                    file = Some(a.to_string());
                } else {
                    eprintln!("多余的参数：{a}");
                    return 2;
                }
            }
        }
        i += 1;
    }
    let Some(file) = file else {
        eprintln!("用法：jishi-rs 调试 脚本.jsh [--断点 12] [--不停在入口] \
                   [--命令 \"单步;变量;继续\"] [--执行器 树遍历]");
        return 2;
    };

    // ---- 执行器：Rust 只支持树遍历 -----------------------------------------
    let explicit_engine = engine.is_some();
    let engine = engine.unwrap_or_else(|| "树遍历".to_string());
    if engine != "树遍历" {
        eprintln!("Rust 版的调试器只支持「树遍历」执行器（给的是「{engine}」）");
        eprintln!("—— 字节码执行器的调试钩子还没搬过来；要用它请用 Python 版 \
`jishi 调试 --执行器 {engine}`");
        return 2;
    }

    // ---- 读源码（Python 文本模式：换行归一）--------------------------------
    let source = match std::fs::read_to_string(&file) {
        Ok(t) => t.replace("\r\n", "\n").replace('\r', "\n"),
        Err(e) => {
            eprintln!("读不了文件「{file}」：{e}");
            return 2;
        }
    };
    let lines: Vec<String> = source.split('\n').map(|s| s.to_string()).collect();
    let render = |e: &JishiError| -> String {
        let lines = lines.clone();
        crate::render_error_with(e, &file, &|l| lines.get((l - 1).max(0) as usize).cloned())
    };

    // ---- 解析 ---------------------------------------------------------------
    let ast = match jishi_frontend::parser::parse(&source, &file) {
        Ok(n) => crate::ast_node_to_json(&n),
        Err(e) => {
            eprint!("{}", render(&crate::sandbox::parse_err_to_jishi(&e)));
            return 1;
        }
    };

    // ---- 断点 ---------------------------------------------------------------
    let wanted = match parse_lines(&bp_specs) {
        Ok(n) => n,
        Err(bad) => {
            eprintln!("看不懂这个断点行号：「{bad}」——写法是 --断点 12 或 --断点 12,15");
            return 2;
        }
    };

    // ---- 命令脚本（中文分号也认 —— 中文输入法下最常见的写法）----------------
    let script: Option<Vec<String>> = commands.map(|c| {
        c.replace('；', ";").split(';')
            .map(|x| x.trim().to_string()).filter(|x| !x.is_empty()).collect()
    });

    // ---- 装配会话 -----------------------------------------------------------
    let mut walker = Walker::new();
    // 程序自己的 `打印` 走 stdout（**每写一次写一次** —— 与 CPython 的 `print`
    // 同粒度；这条粒度 R7.5 的 DAP 对拍踩过，命令行这边要一致）。
    walker.set_output_sink(Some(Box::new(|text: &str, is_err: bool| {
        use std::io::Write;
        if is_err {
            let _ = std::io::stderr().write_all(text.as_bytes());
        } else {
            raw_stdout(text);
        }
    })));
    let globals = walker.globals();
    let stmt_lines = statement_lines(&ast);
    let sess = SessionState::new(
        file.clone(), lines.clone(), stmt_lines, globals, wanted.clone(),
        !no_entry, true, Box::new(line_stdout),
    );
    let session = Rc::new(RefCell::new(sess));

    if explicit_engine {
        println!("（调试执行器：树遍历；默认是树遍历）");
    }

    // 断点写在空行/注释行上时**当场就说**（stderr）：不然用户看着断点加上了、
    // 程序却从不停，只会怀疑调试器坏了（这类「静默无效」是本项目最想避免的）。
    let mut dead: Vec<i64> = {
        let s = session.borrow();
        wanted.iter().copied().filter(|n| !s.stmt_lines.contains(n)).collect()
    };
    dead.sort_unstable();
    if !dead.is_empty() {
        eprintln!("提示：{} 上没有可停的语句（{DEAD_LINE_HINT}），这几个断点不会触发",
                  nums_join(&dead));
    }

    let driver = LineDriver::new(script, Box::new(raw_stdout));
    let dbg = Debugger::new(Rc::clone(&session), Box::new(driver));
    match crate::debugger::run(&mut walker, &ast, dbg) {
        Some(e) => {
            eprint!("{}", render(&e));
            1
        }
        None => 0,
    }
}
