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
