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

/// 「这一行为什么停不住」的说明（与 Python 侧**同一句**）。
pub const DEAD_LINE_HINT: &str =
    "空行 / 注释 / 「否则」「捕获」「最终」这类起头的行——他们是块的一部分，不是独立语句";

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
    /// * 内建函数与异常类型在 Python 侧的模块环境里也在，一次冒出三十多个会把
    ///   用户的变量挤到屏幕外。⚠️ Rust 树遍历执行器的**内建不在模块作用域里**
    ///   （`Walker::builtins` 是另一张表），所以这一条在这里是空操作 —— 留着是
    ///   为了让两边的**判定口径**长得一样，将来谁把内建挪进作用域也不会漏。
    pub fn is_noise(&self, name: &str) -> bool {
        name.starts_with("__") || crate::builtin_names().iter().any(|b| b == name)
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

    /// 一条命令 → 新的恢复模式（`None` = 继续待命）。
    ///
    /// ⚠️ R7.5 只实现**恢复类**命令（继续 / 单步 / 下一步 / 跳出 / 退出）——
    /// 那是 DAP 唯一会发过来的东西（编辑器用按钮表达单步，没有「敲命令」这回事）。
    /// 命令行专用的那些（断点 / 变量 / 查看 / 栈 / 源码 / 帮助）等
    /// `jishi-rs 调试` 那一轮再补，登记在 `docs/路线图.md`。
    pub fn apply_command(&mut self, raw: &str) -> Option<Mode> {
        let text = raw.trim();
        let (head, _arg) = if text.is_empty() {
            match self.last_cmd.clone() {
                Some((c, a)) => (c, a),
                None => {
                    (self.write_line)("（还没有上一条命令）");
                    return None;
                }
            }
        } else {
            let mut it = text.splitn(2, char::is_whitespace);
            let h = it.next().unwrap_or("").to_string();
            let a = it.next().unwrap_or("").trim().to_string();
            let (h, a) = (alias(&h), a);
            self.last_cmd = Some((h.clone(), a.clone()));
            (h, a)
        };
        match head.as_str() {
            "继续" => Some(Mode::Run),
            "单步" => Some(Mode::Step),
            "下一步" => Some(Mode::Next),
            "跳出" => {
                if self.depth() == 0 {
                    (self.write_line)("（已经在最外层了，按「继续」处理）");
                    Some(Mode::Run)
                } else {
                    Some(Mode::Out)
                }
            }
            "退出" => {
                self.quit_requested = true;
                Some(Mode::Run)      // 由 `Debugger::before_stmt` 转成调试退出信号
            }
            other => {
                (self.write_line)(&format!("不认识这个命令：「{other}」——输入「帮助」看有哪些"));
                None
            }
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
        self.dump_var_rows(walker, &snap.vars, &snap.label);
        self.dump_stack(Some(&snap.chain));
        (self.write_line)("（想复盘就改脚本、带上同一组断点再跑一次）");
    }

    /// 打一串变量（`report_error` 用；DAP 与命令行共用同一份行格式）。
    fn dump_var_rows(&mut self, walker: &mut Walker,
                     vars: &[(String, Val)], label: &str) {
        (self.write_line)(&format!("当前帧（{label}）的变量："));
        if vars.is_empty() {
            (self.write_line)("  （没有）");
            return;
        }
        let mut lines: Vec<String> = Vec::new();
        let mut hidden = 0usize;
        for (name, value) in vars {
            if self.is_noise(name) {
                hidden += 1;
                continue;
            }
            lines.push(format!("  {name} = {}", Debugger::fmt_value(walker, value)));
        }
        if lines.is_empty() {
            (self.write_line)("  （没有）");
        }
        for l in lines {
            (self.write_line)(&l);
        }
        if hidden > 0 {
            // 不报具体条数：各执行器「看得见的全局」本来就不同，报数字会让记录
            // 对不上，而这个数字对用户没有意义（M40）
            (self.write_line)("  （另有内建/临时变量没显示，要看用「变量 全部」）");
        }
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

/// 命令别名 → 规范名（让 `c` / `s` / `n` 这些 gdb 习惯也能用）。
fn alias(head: &str) -> String {
    match head {
        "c" | "cont" | "continue" | "r" | "run" => "继续",
        "s" | "step" => "单步",
        "n" | "next" => "下一步",
        "o" | "out" | "finish" => "跳出",
        "q" | "quit" | "exit" => "退出",
        other => other,
    }
    .to_string()
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
        if let Some(list) = self.script.take() {
            if self.script_pos < list.len() {
                let cmd = list[self.script_pos].trim().to_string();
                self.script_pos += 1;
                (self.out)(&format!("{PROMPT}{cmd}"));
                self.script = Some(list);
                return cmd;
            }
            (self.out)(&format!("{PROMPT}（命令用完了，自动继续）"));
            self.script = Some(list);
            return "继续".to_string();
        }
        // 真终端：打提示符、读一行（管道输入自己回显一遍 —— 终端会替用户回显，
        // 管道不会，不回显的话记录里只剩提示符和输出挤在一行，读起来前言不搭后语）
        (self.out)(PROMPT);
        use std::io::{BufRead, IsTerminal, Write};
        let mut line = String::new();
        let stdin = std::io::stdin();
        let n = stdin.lock().read_line(&mut line).unwrap_or(0);
        if n == 0 {
            (self.out)("（输入结束了）");
            return "退出".to_string();
        }
        let text = line.trim_end_matches(['\n', '\r']).to_string();
        if !stdin.is_terminal() {
            let mut so = std::io::stdout();
            let _ = writeln!(so, "{text}");
            let _ = so.flush();
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
            let mode = s.apply_command(&raw);
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
