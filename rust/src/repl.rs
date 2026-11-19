//! R8.4b：交互式 REPL（`jishi -i`）—— `jishi/repl.py` 的移植。
//!
//! ## 为什么它必须补
//!
//! 发行包已经改走单二进制（R8.3），而 `-i` 以前只有 Python 版有 —— 不留缺口才能换包。
//!
//! ## 与 Python 侧的口径
//!
//! | 能力 | 口径 |
//! |---|---|
//! | 顶层表达式回显 | **逐字一致**（走 `py_repr`，与 `打印` 分工同 Python） |
//! | 多行续行判定 | **逐字一致**（冒号结尾 / 括号未闭合 / 引号未闭合 / 块未闭合） |
//! | 命令（退出/帮助/清空/历史） | **逐字一致**（含别名表与帮助正文） |
//! | 报错 | 同一个渲染器（`render_err`），源码行 + `^` 都在 |
//! | 历史文件 | 同一份 `~/.jishi/repl_history.txt`（`JISHI_HISTORY` 可覆盖）、同样的去重与上限 |
//!
//! ### ⚠️ 一处**如实标注的差异**：没有行编辑
//!
//! Python 版在真终端里自己实现了逐键编辑（`jishi/lineedit.py`，500 行）：
//! `↑/↓` 翻历史、`Tab` 补全、退格删汉字。**Rust 这版没有** —— 只按行读
//! （标准输入/终端自己的行缓冲）。
//!
//! ⇒ 影响：交互时**用不了箭头翻历史与 Tab 补全**；`历史` 命令照样能看。
//! ⇒ 为什么不硬上：那要自己进 raw 模式、处理 Windows/POSIX 两套按键与宽字符，
//! 是**另一件独立的事**（与「退役 Python」这条主线无关），硬塞进来只会两头不讨好。
//!
//! ⚠️ 还有一处**行为不同但不影响脚本**：Python 版在管道里也会打印提示符
//! （`read_line` 自己 print）；这里**同样打印**（保持一致，对拍也靠它）。

use std::io::{BufRead, IsTerminal, Write};
use std::path::PathBuf;
use std::process::ExitCode;

use crate::langdata::LANG_VERSION;

/// 历史上限（与 `lineedit.HISTORY_LIMIT` 一致）。
const HISTORY_LIMIT: usize = 500;
/// 「历史」命令默认显示多少条（与 `repl._HISTORY_SHOW` 一致）。
const HISTORY_SHOW: usize = 20;

fn banner() -> String {
    // ⚠️ 结尾要留一个 `\n`：Python 的 `BANNER` 是三引号字符串、末尾本来就带换行，
    //    `print(BANNER)` 之后再补一个 ⇒ 屏幕上**有一行空行**。少这个就与 Python 差一行。
    format!(
        "基石 jishi {LANG_VERSION} —— 一门像 Python 一样简单易用的中文编程语言\n\
         输入「退出」离开；冒号结尾的行会继续输入（多行代码块）。\n"
    )
}

/// 帮助正文 —— **逐字照抄** `repl.HELP_TEXT`。
const HELP_TEXT: &str = "\
可用命令：
  退出 / quit / q        离开基石
  帮助 / help / h        显示这份帮助
  清空 / clear           清空当前多行输入（用于放弃写了一半的代码块）
  历史 / history         看看最近用过哪些输入

编辑：
  Tab                    补全（关键字 / 内建 / 标准库 / 已定义的变量；`模块.` 给函数）
  ↑ / ↓                  翻历史（真终端里才可用；历史会存到 ~/.jishi/repl_history.txt）
  Ctrl-C                 放弃当前输入，回到干净的一行
  多行块                 冒号结尾、括号或引号没闭合时自动续行，空行结束并执行

内建函数：
  打印(...)  输入(...)  整数(...)  小数(...)  文本(...)  长度(...)
  范围(起, 止, 步长)  最大(列表)  最小(列表)  总和(列表)
  类型(...)  反转(...)

对象方法（举例）：
  列表：追加 / 插入 / 移除 / 弹出 / 排序 / 反转 / 清空 / 索引 / 计数 / 包含
  字典：获取 / 键 / 值 / 包含 / 更新 / 合并 / 弹出 / 清空
  文本：拆分 / 替换 / 查找 / 大写 / 小写 / 去空白 / 开头是 / 结尾是 / 包含 / 转整数 / 转小数

标准库：导入 随机（随机整数 / 随机小数 / 洗牌）
";

const EXIT_WORDS: [&str; 4] = ["退出", "quit", "exit", "q"];
const HELP_WORDS: [&str; 4] = ["帮助", "help", "h", "？"];
const CLEAR_WORDS: [&str; 2] = ["清空", "clear"];
const HISTORY_WORDS: [&str; 2] = ["历史", "history"];

// ---------------------------------------------------------------------------
// 续行判定（逐条对齐 `repl._needs_more`）
// ---------------------------------------------------------------------------

/// 括号是否未闭合（**忽略字符串里**的括号）。
fn unclosed_parens(text: &str) -> bool {
    let mut depth: i32 = 0;
    let mut in_str: Option<char> = None;
    for ch in text.chars() {
        if let Some(q) = in_str {
            if ch == q {
                in_str = None;
            }
            continue;
        }
        if "\"'“”".contains(ch) {
            in_str = Some(ch);
        } else if ch == '(' {
            depth += 1;
        } else if ch == ')' {
            depth -= 1;
        }
    }
    depth > 0
}

/// 引号（含三引号 / 插值字符串）是否没写完 —— **问真词法器**，不自己数引号。
///
/// 口径与 Python 的 `except LexStringError:` 一致：那边 `LexStringError` 的码是
/// `E0104`，Rust 词法器把同一族（三引号没闭合 / 字符串没结束 / 插值没闭合 /
/// 末尾转义符没内容）**也**都记在 `E0104` 上 ⇒ 认码即可，不必认文案。
fn unclosed_string(text: &str) -> bool {
    matches!(jishi_frontend::tokenizer::tokenize(text, "<交互>"),
             Err(e) if e.code == "E0104")
}

/// 当前打开的缩进块层数（模拟缩进栈）。
fn block_depth(lines: &[String]) -> usize {
    let mut stack: Vec<i32> = vec![0];
    for line in lines {
        let s = line.trim();
        if s.is_empty() {
            continue;
        }
        let indent = line.chars().take_while(|c| *c == ' ' || *c == '\t').count() as i32;
        while stack.len() > 1 && indent < *stack.last().unwrap() {
            stack.pop();
        }
        if s.ends_with('：') || s.ends_with(':') {
            if indent >= *stack.last().unwrap() {
                stack.push(indent);
            }
        } else if indent > *stack.last().unwrap() {
            stack.push(indent);
        }
    }
    stack.len() - 1
}

/// 判断是否需要继续读入下一行（逐条对齐 Python）。
fn needs_more(buffer: &[String]) -> bool {
    if buffer.is_empty() {
        return false;
    }
    let last = buffer.last().unwrap();
    let stripped = last.trim();
    if stripped.is_empty() {
        return false;
    }
    // 冒号结尾 → 块还没写内容
    if stripped.ends_with('：') || stripped.ends_with(':') {
        return true;
    }
    let joined = buffer.join("\n");
    if unclosed_parens(&joined) {
        return true;
    }
    if unclosed_string(&joined) {
        return true;
    }
    // 处于未闭合的缩进块内（块体行有缩进）→ 允许继续输入块内语句
    block_depth(buffer) > 0
}

// ---------------------------------------------------------------------------
// 历史（与 `lineedit.History` 同规则、同一份文件）
// ---------------------------------------------------------------------------

fn history_path() -> PathBuf {
    if let Some(p) = std::env::var_os("JISHI_HISTORY") {
        if !p.is_empty() {
            return PathBuf::from(p);
        }
    }
    let home = std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    home.join(".jishi").join("repl_history.txt")
}

#[derive(Default)]
struct History {
    entries: Vec<String>,
}

impl History {
    /// 从文件读（读不到就当空的，**不打扰用户**）。
    fn load() -> History {
        let mut h = History::default();
        if let Ok(text) = std::fs::read_to_string(history_path()) {
            for line in text.split('\n') {
                if !line.trim().is_empty() {
                    h.append(line);
                }
            }
        }
        h
    }

    /// 相邻重复不重复记、空行不记、超上限丢最旧的（与 `_append` 一致）。
    fn append(&mut self, line: &str) {
        let line = line.trim_end_matches(['\r', '\n']);
        if line.trim().is_empty() {
            return;
        }
        if self.entries.last().map(|s| s.as_str()) == Some(line) {
            return;
        }
        self.entries.push(line.to_string());
        while self.entries.len() > HISTORY_LIMIT {
            self.entries.remove(0);
        }
    }

    /// 写回；**失败不报错**（只读目录、无权限都很常见）。
    fn save(&self) {
        let path = history_path();
        if let Some(dir) = path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let _ = std::fs::write(&path, format!("{}\n", self.entries.join("\n")));
    }

    fn show(&self) {
        if self.entries.is_empty() {
            println!("（还没有历史）");
            return;
        }
        let start = self.entries.len().saturating_sub(HISTORY_SHOW);
        let tail = &self.entries[start..];
        for (i, line) in tail.iter().enumerate() {
            println!("{:>4}  {}", start + i + 1, line);
        }
        if self.entries.len() > tail.len() {
            println!("（只显示最近 {} 条，共 {} 条）", tail.len(), self.entries.len());
        }
    }
}

// ---------------------------------------------------------------------------
// 跑一段输入
// ---------------------------------------------------------------------------

/// 执行一段源码；成功时若顶层表达式有值就回显（返回是否成功）。
fn exec_source(walker: &mut crate::walk::Walker, source: &str) -> bool {
    let lines: Vec<String> = source.replace("\r\n", "\n").replace('\r', "\n")
        .split('\n').map(str::to_string).collect();
    match jishi_frontend::parser::parse(source, "<交互>") {
        Ok(prog) => {
            let ast = crate::ast_node_to_json(&prog);
            match walker.run_capture(&ast) {
                Ok(v) => {
                    if let Some(val) = v {
                        println!("{}", crate::py_repr(&val));
                    }
                    true
                }
                Err(e) => {
                    // ⚠️ **运行期**错误这里传 `<输入>`（不是 `<交互>`）—— 这是**照抄
                    //    Python 的现状**：那边 `JishiError` 的 `filename` 默认就是
                    //    `<输入>`，而运行期错误是**不带文件名**抛出来的；只有**语法**
                    //    错误会把 `parse_source(source, "<交互>")` 那个名字带上。
                    //    两条路的名字不一样是 Python 的既成行为，改了就对不上拍。
                    eprint!("{}", crate::run::render_err(&e, "<输入>", &lines));
                    false
                }
            }
        }
        Err(pe) => {
            let e = crate::sandbox::parse_err_to_jishi(&pe);
            eprint!("{}", crate::run::render_err(&e, "<交互>", &lines));
            false
        }
    }
}

/// `jishi -i` / `jishi-rs -i`。
pub fn run_repl() -> ExitCode {
    println!("{}", banner());
    let mut walker = crate::walk::Walker::new();
    // 输出**立刻打**（Python 的 `打印` 是直接写 sys.stdout），别攒到退出才吐。
    walker.set_output_sink(Some(Box::new(|s: &str, is_err: bool| {
        if is_err {
            let _ = std::io::stderr().write_all(s.as_bytes());
        } else {
            let _ = std::io::stdout().write_all(s.as_bytes());
            let _ = std::io::stdout().flush();
        }
    })));

    let mut history = History::load();
    let mut buffer: Vec<String> = Vec::new();
    let stdin = std::io::stdin();
    let tty = std::io::stdin().is_terminal();

    loop {
        // 提示符：顶层 `基石> `，续行 `    ... `（与 Python 逐字一致）
        let prompt = if buffer.is_empty() { "基石> " } else { "    ... " };
        print!("{prompt}");
        let _ = std::io::stdout().flush();

        let mut line = String::new();
        match stdin.lock().read_line(&mut line) {
            Ok(0) | Err(_) => {
                // EOF（或读失败）：与 Python 的 `except EOFError:` 一致 ——
                // **先打一个空行**（提示符后面接个换行），再把没闭合的多行输入跑掉。
                // ⚠️ 这一行不能省：管道喂输入时它是屏幕上的最后一个字符，
                //    少了就与 Python 差一个 `\n`（逐字节对拍当场现形）。
                println!();
                if !buffer.is_empty() {
                    exec_source(&mut walker, &buffer.join("\n"));
                }
                break;
            }
            Ok(_) => {}
        }
        // 交互式终端下 `read_line` 不会把用户敲的回车再吐出来；**管道**里
        // 同样不回显 —— 与 Python 的 `read_line` 一致（所以提示符后面直接跟结果）。
        let stripped = line.trim();
        let at_top = buffer.is_empty();

        // 命令：退出/帮助/历史**只在顶层**；清空随时可用（放弃写了一半的代码块）
        if at_top && EXIT_WORDS.contains(&stripped) {
            break;
        }
        if at_top && HELP_WORDS.contains(&stripped) {
            println!("{HELP_TEXT}");
            continue;
        }
        if at_top && HISTORY_WORDS.contains(&stripped) {
            history.show();
            continue;
        }
        if CLEAR_WORDS.contains(&stripped) {
            if at_top {
                println!("（没有正在输入的内容）");
            } else {
                println!("（已放弃当前输入）");
                buffer.clear();
            }
            continue;
        }

        // 空行：结束多行输入并执行
        if stripped.is_empty() {
            if !buffer.is_empty() {
                history.append(&buffer.join("\n"));   // 整块记一条，翻历史能取回来
                exec_source(&mut walker, &buffer.join("\n"));
                buffer.clear();
            }
            continue;
        }

        let raw = line.trim_end_matches(['\r', '\n']).to_string();
        buffer.push(raw.clone());
        history.append(&raw);
        if !needs_more(&buffer) {
            exec_source(&mut walker, &buffer.join("\n"));
            buffer.clear();
        }
    }

    history.save();
    let _ = tty;
    ExitCode::SUCCESS
}
