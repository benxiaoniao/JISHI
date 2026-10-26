//! R8.4 前置：`医生` / `新项目` / `教程` 三个子命令搬进 Rust。
//!
//! ## 为什么这三个必须先搬
//!
//! 发行包已经改走 **cargo 单二进制**（R8.3），而这三个命令以前**只有 Python 版有**
//! —— 不补就换包 = **用户可见的功能倒退**（`jishi 医生` 正是安装说明里让用户跑的
//! 第一个命令）。REPL（`-i`）另算，见 `docs/路线图.md` §11.4。
//!
//! ## 与 Python 侧的对齐口径
//!
//! | 命令 | 口径 |
//! |---|---|
//! | `新项目` | **逐字对齐**（含产物文件的换行 —— Windows 上 Python 文本模式写 `\r\n`） |
//! | `教程` | **逐字对齐**（10 章索引） |
//! | `医生` | 🔴 **有意重写，不逐字对齐** —— 见下 |
//!
//! ### `医生` 为什么不能照搬
//!
//! Python 那版报的是「Python：3.13」「C VM：已加载」「MCP Server：可用」——
//! **单二进制世界里这三样根本不存在**。照搬等于给用户看三条假的检查项。
//! 现在它报的是**这个二进制真正该关心的事**：版本 / 可执行文件在哪 / 在不在 PATH 上 /
//! 用户目录能不能写 / 终端是不是交互式 / 包索引通不通。

use std::io::IsTerminal;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use crate::langdata::LANG_VERSION;

// ---------------------------------------------------------------------------
// 教程
// ---------------------------------------------------------------------------

/// 教程 10 章。**逐字对齐** Python 侧 `cli._cmd_tutorial`（`(编号, 标题, 说明)`）。
const CHAPTERS: [(&str, &str, &str); 10] = [
    ("01", "你好基石", "运行基石、打印、变量、注释"),
    ("02", "数与文本", "运算、字符串方法、输入、列表"),
    ("03", "判断与循环", "如果/否则、遍历/循环/当、链式比较"),
    ("04", "函数", "定义函数、作用域、递归"),
    ("05", "字典与结构化数据", "键值对、嵌套、计数器"),
    ("06", "文件与标准库", "文件读写、数学/时间/表格模块"),
    ("07", "综合实战", "记账程序：需求→设计→实现"),
    ("08", "语法糖", "多赋值解包/默认参数/推导式/文本插值"),
    ("09", "沙箱与 MCP", "沙箱三道保险/统一协议/MCP Server"),
    ("10", "包管理", "安装/卸载/列表/依赖解析"),
];

pub fn run_tutorial() -> ExitCode {
    println!("== 基石教程（10 章）==");
    for (num, title, desc) in CHAPTERS {
        println!("  第{num}章 {title}：{desc}");
    }
    println!();
    println!("完整教程见 docs/tutorial/ 目录，或访问项目 README 的教程索引。");
    ExitCode::SUCCESS
}

// ---------------------------------------------------------------------------
// 新项目
// ---------------------------------------------------------------------------

/// 按 Python 的**文本模式**写文件：Windows 上 `\n` 要变成 `\r\n`。
///
/// ⚠️ 这一条不起眼但会让对拍红：Python 的 `open(path, "w")` 在 Windows 上是
/// 文本模式（换行翻译），Rust 的 `fs::write` 是**原样字节**。差一个 `\r`
/// 就是逐字节不一致。
fn write_text_like_python(path: &Path, content: &str) -> std::io::Result<()> {
    #[cfg(windows)]
    let content = content.replace('\n', "\r\n");
    std::fs::write(path, content)
}

pub fn run_new_project(name: &str) -> ExitCode {
    let cwd = match std::env::current_dir() {
        Ok(d) => d,
        Err(e) => {
            eprintln!("拿不到当前目录：{e}");
            return ExitCode::from(1);
        }
    };
    let target = cwd.join(name);
    if target.exists() {
        // 与 Python 同一句话、同一个出口（stderr），退码 1
        eprintln!("目录「{name}」已存在，换个名字吧");
        return ExitCode::from(1);
    }
    if let Err(e) = std::fs::create_dir(&target) {
        eprintln!("建目录失败：{e}");
        return ExitCode::from(1);
    }

    let hello = format!(
        "# {name} —— 你的第一个基石项目\n\n\
         令 名字 = \"世界\"\n\
         打印(\"你好，\" + 名字)\n\
         打印(`这是 {name} 项目的第一行代码`)\n"
    );
    let readme = format!("# {name}\n\n运行 `jishi hello.jsh` 查看效果。\n");

    for (file, text) in [("hello.jsh", hello), ("说明.md", readme)] {
        if let Err(e) = write_text_like_python(&target.join(file), &text) {
            eprintln!("写文件失败：{e}");
            return ExitCode::from(1);
        }
    }

    // ⚠️ 打印的路径与 Python **逐字一致**：`os.path.join(cwd, name)` 用系统分隔符
    //    （Windows 是 `\`），后面接的是**字面量** `/hello.jsh`。
    println!("已创建项目「{name}」：");
    println!("  {}/hello.jsh", target.display());
    println!("  {}/说明.md", target.display());
    println!();
    println!("下一步：cd {name} && jishi hello.jsh");
    ExitCode::SUCCESS
}

// ---------------------------------------------------------------------------
// 医生
// ---------------------------------------------------------------------------

fn exe_path() -> Option<PathBuf> {
    std::env::current_exe().ok()
}

/// 把混了两种分隔符的路径显示得整齐一点。
///
/// `packages::package_dirs()` 是 `join(cwd, ".jishi/packages")` —— 字符串拼接的
/// 结果在 Windows 上长这样：`D:\code\基石\.jishi/packages`。包管理那边不能动
/// （它要与 Python 的 `os.path.join` 逐字一致），**只在显示时**归一。
fn pretty_path(s: &str) -> String {
    let sep = std::path::MAIN_SEPARATOR;
    s.replace(['\\', '/'], &sep.to_string())
}

/// 在 PATH 里找 `jishi`（Windows 认 `jishi.exe`），返回命中的完整路径。
fn find_on_path() -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    let names: &[&str] = if cfg!(windows) {
        &["jishi.exe", "jishi.cmd", "jishi.bat"]
    } else {
        &["jishi"]
    };
    for dir in std::env::split_paths(&path) {
        for n in names {
            let cand = dir.join(n);
            if cand.is_file() {
                return Some(cand);
            }
        }
    }
    None
}

/// `jishi 医生` —— 环境自检。
///
/// 🔴 **有意与 Python 版不同**（见模块头）：单二进制里没有「Python 版本」「C VM」，
/// 报那两条等于给用户看假的检查项。这里只查**这个二进制真正依赖的东西**。
///
/// ⚠️ **两条判据的分寸**（都不算「环境异常」，不翻退码）：
/// * 「PATH 里没有 `jishi`」—— 直接用完整路径照样能跑，**提示即可**；
///   否则开发时从 `target/release/jishi-rs.exe` 跑一下就报警，纯噪音。
/// * 「单个索引源不通」—— 备用源在大陆常常不通，只要有一个可用就能装包。
///   把它们报成「异常」，用户会去修一个**本来就不需要修**的东西。
pub fn run_doctor(no_net: bool) -> ExitCode {
    let mut ok = true;
    println!("== 基石环境自检 ==");

    println!("  版本：{LANG_VERSION}");
    println!("  引擎：Rust 单二进制（零第三方依赖，**不需要 Python**）");

    match exe_path() {
        Some(p) => {
            println!("  可执行文件：{}", p.display());
            if let Some(dir) = p.parent() {
                println!("  安装位置：{}", dir.display());
            }
        }
        None => {
            ok = false;
            println!("  可执行文件：拿不到路径 [异常]");
        }
    }

    // PATH：装完却敲不出 `jishi`，是新手最常撞的一堵墙 —— 单独拿出来说。
    match find_on_path() {
        Some(p) => println!("  PATH：已找到 [OK]（{}）", p.display()),
        None => {
            println!("  PATH：没找到 `jishi` [提示]");
            println!("    把可执行文件所在目录加进 PATH 就能直接敲 `jishi`；\
                      不加也行，用完整路径一样跑");
        }
    }

    // 包目录：**只报告，不创建**（诊断命令不该有副作用）。
    // 顺序与 `jishi 安装` 的搜索顺序一致：项目 `.jishi/packages/` 优先，用户目录兜底。
    println!("  包目录（搜索顺序）：");
    for (i, d) in crate::packages::package_dirs().iter().enumerate() {
        let exists = Path::new(d).is_dir();
        println!("    {}. {}（{}）", i + 1, pretty_path(d),
                 if exists { "已存在" } else { "首次安装时创建" });
    }

    // 终端：交互式还是被重定向。影响的是「提示符/补全能不能用」这类体验
    let tty = std::io::stdout().is_terminal();
    println!("  标准输出：{}", if tty { "交互式终端 [OK]" } else { "被重定向（管道/文件）" });
    if cfg!(windows) && tty {
        println!("    提示：Windows 控制台若是旧代码页，中文可能显示成乱码\
                  —— 执行 `chcp 65001` 切到 UTF-8");
    }

    // 包索引可达性：`jishi 安装` 能不能用全看这一步
    if no_net {
        println!("  包索引：跳过联网检查（--no-net）");
    } else {
        let urls = crate::packages::default_index_urls();
        let mut good = 0usize;
        for (i, url) in urls.iter().enumerate() {
            match crate::packages::read_index_text(url)
                .and_then(|t| crate::packages::parse_index(&t))
            {
                Ok(idx) => {
                    good += 1;
                    let n = idx.get("包").and_then(|b| b.as_obj()).map(|m| m.len()).unwrap_or(0);
                    println!("  索引源 {}：可达 [OK]（{} 个包）", i + 1, n);
                }
                Err(e) => {
                    println!("  索引源 {}：不可达 [警告]（{}）", i + 1, e);
                }
            }
            println!("      {url}");
        }
        if good == 0 {
            ok = false;
            println!("    建议：检查网络/代理；或用 `--index <索引地址>` 指定别的源");
        } else if good < urls.len() {
            println!("    提示：{}/{} 个源可用 —— 主源不通时安装会变慢\
                      （会先等一个超时再去试下一个）", good, urls.len());
        }
    }

    println!();
    if ok {
        println!("[OK] 环境正常，可以开始写基石代码了。");
        ExitCode::SUCCESS
    } else {
        println!("[警告] 发现环境问题，请按上方建议修复。");
        ExitCode::from(1)
    }
}

// ---------------------------------------------------------------------------
// `jishi 测试`（M127）—— 扫 `*_test.jsh` 并跑
// ---------------------------------------------------------------------------
//
// ⚠️ **和 Python 侧同口径**（`oracle/jishi/cli.py::_cmd_test`）：只负责
//    「找文件 + 调度 + 总览」，**明细交给 `测试.汇总()` 自己报**（它知道每个用例的名字）。
//
// ⚠️ 两类失败必须分开：断言没通过（E2008）是**你的测试**写错；
//    别的错是**你的代码**崩了。混成一句，用户会去翻断言，可实际是代码更早就崩了。

/// 递归收集 `*_test.jsh`（跳过 `.jishi/` 装好的包）。
///
/// ⚠️ 不引 `walkdir` 这种依赖 —— 就为了一次遍历不值得；手写 20 行更省事，
///    也少一处得跟宿主的 `fs` 语义对齐的地方。
fn collect_tests(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    let mut 条目们: Vec<_> = rd.flatten().map(|e| e.path()).collect();
    条目们.sort();
    for p in 条目们 {
        let 名 = p.file_name().and_then(|s| s.to_str()).unwrap_or("");
        if p.is_dir() {
            if 名 == ".jishi" || 名 == "target" || 名 == "node_modules" {
                continue;
            }
            collect_tests(&p, out);
        } else if 名.ends_with("_test.jsh") {
            out.push(p);
        }
    }
}

pub fn run_test(root: &str) -> ExitCode {
    let 根 = Path::new(root);
    if !根.is_dir() {
        eprintln!("❌ 找不到目录：{root}");
        return ExitCode::from(2);
    }

    let mut 文件们 = Vec::new();
    collect_tests(根, &mut 文件们);
    if 文件们.is_empty() {
        eprintln!("没找到 *_test.jsh（在 {root} 下）—— 测试文件名要以 `_test.jsh` 结尾。");
        return ExitCode::from(2);
    }

    let mut 过 = 0usize;
    let mut 断言败 = 0usize;
    let mut 崩了 = 0usize;

    for f in &文件们 {
        let 相对 = f.strip_prefix(根).unwrap_or(f);
        println!("\n{}", 相对.display());
        // flush：`测试.汇总()` 也往 stdout 打，不刷新会让「哪个文件」跑到明细后面
        use std::io::Write as _;
        let _ = std::io::stdout().flush();

        let 名 = f.to_string_lossy().to_string();
        let 结果 = run_one(&名);
        match 结果 {
            Ok(()) => 过 += 1,
            Err(Some(码)) if 码 == "E2008" => {
                断言败 += 1;
                println!("  ↑ 上面是用例的失败明细；本文件有断言没通过");
            }
            Err(_) => 崩了 += 1,
        }
    }

    println!("\n{}", "=".repeat(48));
    let mut 尾 = String::new();
    if 断言败 > 0 {
        尾.push_str(&format!("，{断言败} 个有断言没通过"));
    }
    if 崩了 > 0 {
        尾.push_str(&format!("，{崩了} 个程序出错"));
    }
    println!("汇总：{}/{} 个测试文件通过{尾}", 过, 文件们.len());

    if 断言败 == 0 && 崩了 == 0 {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    }
}

/// 跑一个文件。`Ok(())` 表示通过；`Err(Some(码))` 里带错误码（用来分 E2008）。
///
/// ⚠️ **不走 `run::main_cli`**：那条路渲染完错误就转成 `ExitCode`，
///    错误码丢了 —— 就没法分「断言没过」和「崩了」。
fn run_one(名: &str) -> Result<(), Option<String>> {
    let 文本 = std::fs::read_to_string(名).map_err(|e| {
        eprintln!("  ⚠️ 读不了文件：{e}");
        None::<String>
    })?;

    let prog = match jishi_frontend::parser::parse(&文本, 名) {
        Ok(p) => p,
        Err(e) => {
            let je = crate::sandbox::parse_err_to_jishi(&e);
            eprintln!("  ⚠️ 不是断言失败，是语法错误（{} {}）", je.code.as_deref().unwrap_or("?"), je.message);
            return Err(je.code);
        }
    };
    let module = jishi_frontend::compiler::compile_program(&prog, 名, None, false);
    let bytecode = jishi_frontend::serialize::dump_json(&module);
    crate::set_args(Vec::new());
    let mut vm = match crate::load_module(&bytecode) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("  ⚠️ 字节码加载失败：{e}");
            return Err(None);
        }
    };
    match vm.run() {
        Ok(()) => {
            print!("{}", crate::get_output(&vm));
            eprint!("{}", crate::get_output_err(&vm));
            Ok(())
        }
        Err(e) => {
            eprint!("{}", crate::get_output_err(&vm));
            print!("{}", crate::get_output(&vm));
            let 码 = e.code.clone();
            let 是断言 = 码.as_deref() == Some("E2008");
            if !是断言 {
                eprintln!(
                    "  ⚠️ 不是断言失败，是程序出错了（{} {}）",
                    码.as_deref().unwrap_or("?"),
                    e.message
                );
            }
            Err(码)
        }
    }
}

// ---------------------------------------------------------------------------
// M137：`自测` —— 用「固定版」跑基石自带的黑盒运行器
// ---------------------------------------------------------------------------
//
// 语义**逐条对齐** Python 侧 `cli.py::_cmd_selfrun`（判据 `test_m137_selfrun.py`
// 拿同一批参数比两边的输出）。要读「为什么需要固定版」，见那边的文档串。

/// 固定版配置放在哪：`JISHI_HOME`（默认 `~/.jishi`）下的两个纯文本文件，**一行一个词元**。
///
/// 🔴 **为什么不用 JSON**（与 Python 侧同一条理由，两边都这么写是**故意的**）：
/// ① 本 crate 没有 serde_json —— 加第三方依赖要用户拍板，不能为一个小配置破例；
/// ② Windows 路径里的反斜杠在 JSON 里**必须转义**，手写读写最容易在这里出错。
///    「一行一个词元」**没有转义问题**（路径里不可能有换行）。
const 自测_运行者文件: &str = "自测-运行者.txt";
const 自测_被测文件: &str = "自测-被测.txt";

/// 基石的用户目录：`JISHI_HOME`，没有就 `~/.jishi`。
///
/// ⚠️ **不跟 `selfupdate::猜安装目录` 那一套**：那是**安装位置**，而固定版是
/// **开发机的偏好** —— 写进安装目录会被升级冲掉，而且 Inno 目录未必可写。
fn 自测用户目录() -> PathBuf {
    if let Ok(v) = std::env::var("JISHI_HOME") {
        let v = v.trim().to_string();
        if !v.is_empty() {
            return PathBuf::from(v);
        }
    }
    let home = std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .unwrap_or_default();
    PathBuf::from(home).join(".jishi")
}

fn 读命令(文件名: &str) -> Vec<String> {
    let p = 自测用户目录().join(文件名);
    match std::fs::read_to_string(&p) {
        Ok(文) => 文
            .split('\n')
            .filter(|行| !行.trim().is_empty())
            .map(|行| 行.trim_end_matches('\r').to_string())
            .collect(),
        Err(_) => Vec::new(),
    }
}

fn 写命令(文件名: &str, 命令: &[String]) -> std::io::Result<()> {
    let 目录 = 自测用户目录();
    std::fs::create_dir_all(&目录)?;
    std::fs::write(目录.join(文件名), format!("{}\n", 命令.join("\n")))
}

fn 删命令(文件名: &str) -> bool {
    let p = 自测用户目录().join(文件名);
    if p.is_file() {
        std::fs::remove_file(&p).is_ok()
    } else {
        false
    }
}

fn 自己命令() -> Vec<String> {
    match std::env::current_exe() {
        Ok(p) => vec![p.to_string_lossy().to_string()],
        Err(_) => vec!["jishi".to_string()],
    }
}

/// 在 PATH 上找可执行文件（std 没有 `which`，也不为这个引依赖）。
fn 找程序(名: &str) -> Option<PathBuf> {
    let 路径 = std::env::var_os("PATH")?;
    for 目录 in std::env::split_paths(&路径) {
        let 候选 = 目录.join(名);
        if 候选.is_file() {
            return Some(候选);
        }
        #[cfg(windows)]
        for 后缀 in ["exe", "cmd", "bat"] {
            let c = 目录.join(format!("{名}.{后缀}"));
            if c.is_file() {
                return Some(c);
            }
        }
    }
    None
}

/// 把命令里的「程序」定成绝对路径。`Err` = 找不到 —— **绝不悄悄退回当前版**。
fn 定位自测命令(命令: &[String], 谁: &str) -> Result<Vec<String>, String> {
    let 首 = &命令[0];
    let 候选 = Path::new(首);
    if 候选.is_file() {
        // ⚠️ 用 `path::absolute`，**不用 `canonicalize`**：后者在 Windows 上会加上
        //    `\\?\` 前缀（写进配置就再也别想跨机器用），而且它会解析符号链接 ——
        //    与 Python 侧 `os.path.abspath` 同一口径。
        let 绝对 = std::path::absolute(候选).unwrap_or_else(|_| 候选.to_path_buf());
        let mut 出 = vec![绝对.to_string_lossy().to_string()];
        出.extend_from_slice(&命令[1..]);
        return Ok(出);
    }
    let 不像路径 = !首.contains('/')
        && !首.contains('\\')
        && !首.to_lowercase().ends_with(".exe")
        && !首.to_lowercase().ends_with(".cmd")
        && !首.to_lowercase().ends_with(".bat");
    if 不像路径 {
        if let Some(找到) = 找程序(首) {
            let mut 出 = vec![找到.to_string_lossy().to_string()];
            出.extend_from_slice(&命令[1..]);
            return Ok(出);
        }
    }
    Err(format!("{谁}找不到这个程序：{首}"))
}

#[derive(Default)]
struct 自测参数 {
    固定版: Option<Vec<String>>,
    被测: Option<Vec<String>>,
    运行器: Option<String>,
    只看: bool,
    忘掉: bool,
    帮助: bool,
    透传: Vec<String>,
}

/// 手工解析（**不用 clap**：`--被测 python -m ...` 里那个 `-m` 是**值**，
/// 任何正常的选项解析器都会把它当自己的参数咬掉）。规则与 `tools/自测.jsh` 一致。
fn 取自测参数(原始: &[String]) -> Result<自测参数, String> {
    let mut 参 = 自测参数::default();
    let mut i = 0usize;
    while i < 原始.len() {
        let a = 原始[i].as_str();
        if a == "--固定版" || a == "--被测" {
            i += 1;
            let mut 值: Vec<String> = Vec::new();
            while i < 原始.len() && !原始[i].starts_with("--") {
                值.push(原始[i].clone());
                i += 1;
            }
            if 值.is_empty() {
                return Err(format!("「{a}」后面要跟一个命令（比如 {a} C:/jishi/bin/jishi.exe）"));
            }
            if a == "--固定版" {
                参.固定版 = Some(值);
            } else {
                参.被测 = Some(值);
            }
        } else if a == "--运行器" {
            i += 1;
            if i >= 原始.len() {
                return Err("「--运行器」后面要跟一个 .jsh 路径".to_string());
            }
            参.运行器 = Some(原始[i].clone());
            i += 1;
        } else if a == "--看" {
            参.只看 = true;
            i += 1;
        } else if a == "--忘掉" {
            参.忘掉 = true;
            i += 1;
        } else if a == "--帮助" || a == "-h" {
            参.帮助 = true;
            i += 1;
        } else {
            参.透传 = 原始[i..].to_vec();
            break;
        }
    }
    Ok(参)
}

fn 命令文本(命令: &[String]) -> String {
    命令.join(" ")
}

pub fn run_selfrun(原始: &[String]) -> ExitCode {
    let 参 = match 取自测参数(原始) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("{e}");
            return ExitCode::from(2);
        }
    };

    if 参.帮助 {
        println!("用法：jishi 自测 [--固定版 <命令…>] [--被测 <命令…>] [--运行器 <路径>] [--看] [--忘掉] [其余参数透传给运行器]");
        println!("  与 `jishi 测试` 的区别：`测试` 跑你自己项目里的 *_test.jsh；`自测` 跑基石自己的 tools/自测.jsh（开发基石时用，带固定版）。");
        return ExitCode::SUCCESS;
    }

    if 参.忘掉 {
        let 删了 = [删命令(自测_运行者文件), 删命令(自测_被测文件)];
        if 删了.iter().any(|x| *x) {
            println!("已忘掉固定版。");
        } else {
            println!("本来就没记着什么，不用忘。");
        }
        return ExitCode::SUCCESS;
    }

    let 记住的运行者 = 读命令(自测_运行者文件);
    let 记住的被测 = 读命令(自测_被测文件);
    // ⚠️ 显示用原字符串、判存在才用 PathBuf（与 Python 侧同一口径：谁输入什么就显示什么）
    let 运行器文本 = 参.运行器.clone().unwrap_or_else(|| "tools/自测.jsh".to_string());
    let 运行器 = PathBuf::from(&运行器文本);

    // `--看` **只看不写**：它是「我有记着什么吗」，不是「改成这些」。
    if 参.只看 {
        let 运行者 = if 记住的运行者.is_empty() { 自己命令() } else { 记住的运行者.clone() };
        let 被测 = if 记住的被测.is_empty() { 自己命令() } else { 记住的被测.clone() };
        println!("自测 · 现在记着：");
        println!("   运行者：{}{}", 命令文本(&运行者),
                 if 记住的运行者.is_empty() { "（没记固定版 ⇒ 用当前这个 jishi）" } else { "" });
        println!("   被测：{}{}", 命令文本(&被测),
                 if 记住的被测.is_empty() { "（没指定 ⇒ 用当前这个 jishi）" } else { "" });
        println!("   运行器：{}{}", 运行器文本,
                 if 运行器.is_file() { "" } else { " ⚠️ 不存在" });
        return ExitCode::SUCCESS;
    }

    let mut 固定版 = 参.固定版.clone();
    if let Some(v) = &固定版 {
        match 定位自测命令(v, "固定版（运行者）") {
            Ok(命令) => {
                if let Err(e) = 写命令(自测_运行者文件, &命令) {
                    eprintln!("❌ 记不住固定版（写 {} 失败：{e}）", 自测用户目录().display());
                    return ExitCode::from(2);
                }
                固定版 = Some(命令);
            }
            Err(e) => {
                eprintln!("❌ {e}");
                return ExitCode::from(2);
            }
        }
    }
    let mut 被测参 = 参.被测.clone();
    if let Some(v) = &被测参 {
        match 定位自测命令(v, "被测") {
            Ok(命令) => {
                if let Err(e) = 写命令(自测_被测文件, &命令) {
                    eprintln!("❌ 记不住被测（写 {} 失败：{e}）", 自测用户目录().display());
                    return ExitCode::from(2);
                }
                被测参 = Some(命令);
            }
            Err(e) => {
                eprintln!("❌ {e}");
                return ExitCode::from(2);
            }
        }
    }

    let 有固定版 = 固定版.is_some() || !记住的运行者.is_empty();
    let 运行者 = 固定版
        .or_else(|| if 记住的运行者.is_empty() { None } else { Some(记住的运行者) })
        .unwrap_or_else(自己命令);
    let 有指定被测 = 被测参.is_some() || !记住的被测.is_empty();
    let 被测 = 被测参
        .or_else(|| if 记住的被测.is_empty() { None } else { Some(记住的被测) })
        .unwrap_or_else(自己命令);

    if !运行器.is_file() {
        eprintln!("❌ 找不到运行器脚本：{运行器文本}");
        eprintln!("   在仓库根跑，或者用 --运行器 <另一个 .jsh> 指定。");
        return ExitCode::from(2);
    }

    println!("自测 · 运行者：{}（{}）", 命令文本(&运行者), if 有固定版 { "固定版" } else { "当前" });
    println!("       被测：{}（{}）", 命令文本(&被测), if 有指定被测 { "指定" } else { "当前" });
    println!("       运行器：{运行器文本}");
    // 必须在起子进程之前刷（否则管道里顺序会倒 —— M135 踩过同一个坑）
    use std::io::Write as _;
    let _ = std::io::stdout().flush();

    let mut 命令行 = 运行者.clone();
    命令行.push(运行器文本.clone());
    命令行.push("--被测".to_string());
    命令行.extend(被测.iter().cloned());
    命令行.extend(参.透传.iter().cloned());

    let mut cmd = std::process::Command::new(&命令行[0]);
    cmd.args(&命令行[1..]);
    match cmd.status() {
        Ok(st) => ExitCode::from(st.code().unwrap_or(1).clamp(0, 255) as u8),
        Err(_) => {
            eprintln!("❌ 起不来：{}", 命令文本(&运行者));
            ExitCode::from(2)
        }
    }
}
