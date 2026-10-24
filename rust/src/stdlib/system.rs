//! 标准库 `系统`（Python 侧 `jishi/stdlib/系统.py`，13 个函数）。
//!
//! 薄封装 `std::env` / `std::process`。两处要绕一下：
//!
//! - **`系统版本`**：`std` 没有这个 API。Windows 上绑 `RtlGetVersion`
//!   （`GetVersionEx` 会撒谎，说自己是 6.2），Unix 上取 `uname -v`。
//!   仍然是 FFI / 系统命令，**不是第三方 crate**。
//! - **`执行` 的超时**：`std::process::Child` 没有带超时的 `wait`，
//!   所以自己轮询 `try_wait`，超时就 `kill` 并报中文错。
//!
//! `执行` 的输出按 Python 的顺序拼：**先 stdout、再 stderr**，都用
//! UTF-8（非法字节替换成 `\u{FFFD}`，与 Python 的 `errors="replace"` 同）。

use crate::BuiltinHost;
use std::collections::HashMap;
use std::io::Read;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use super::{as_num, as_text, need, need_range, script_args, table, to_vec, R};
use crate::{err, Val};

// ---------------------------------------------------------------------------
// 默认程序打开（`系统.打开`）
// ---------------------------------------------------------------------------

/// 用系统默认程序打开一个文件 / 网址。**目标不存在（且不是网址）直接返回
/// `false`、什么都不启动** —— 这条规矩与另外四个执行器一致（见 Python 侧的
/// 说明），既挡掉「路径打错弹空白窗」，又让行为可判。
fn 打开(目标: &str) -> bool {
    if !目标.contains("://") && !Path::new(目标).exists() {
        return false;
    }
    启动(目标)
}

#[cfg(windows)]
fn 启动(目标: &str) -> bool {
    // `cmd /C start "" <目标>`：第一个空串是**窗口标题占位**（不占位的话，
    // 带引号的路径会被当成标题）。`start` 就是 Windows 上「用默认程序打开」。
    Command::new("cmd")
        .args(["/C", "start", "", 目标])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .is_ok()
}

#[cfg(target_os = "macos")]
fn 启动(目标: &str) -> bool {
    Command::new("open")
        .arg(目标)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .is_ok()
}

#[cfg(not(any(windows, target_os = "macos")))]
fn 启动(目标: &str) -> bool {
    Command::new("xdg-open")
        .arg(目标)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .is_ok()
}

// ---------------------------------------------------------------------------
// 系统版本
// ---------------------------------------------------------------------------

#[cfg(windows)]
mod ver {
    #[repr(C)]
    struct OsVersionInfoW {
        size: u32,
        major: u32,
        minor: u32,
        build: u32,
        platform_id: u32,
        csd: [u16; 128],
    }

    #[link(name = "ntdll")]
    extern "system" {
        fn RtlGetVersion(info: *mut OsVersionInfoW) -> i32;
    }

    /// 与 Python 的 `platform.version()` 对齐（`10.0.22631` 这种）。
    pub fn version() -> String {
        let mut v = OsVersionInfoW {
            size: std::mem::size_of::<OsVersionInfoW>() as u32,
            major: 0,
            minor: 0,
            build: 0,
            platform_id: 0,
            csd: [0u16; 128],
        };
        let ok = unsafe { RtlGetVersion(&mut v) } == 0;
        if ok {
            format!("{}.{}.{}", v.major, v.minor, v.build)
        } else {
            String::new()
        }
    }
}

#[cfg(not(windows))]
mod ver {
    pub fn version() -> String {
        // Python 的 `platform.version()` 在 Unix 上就是 `uname -v`
        match std::process::Command::new("uname").arg("-v").output() {
            Ok(o) => String::from_utf8_lossy(&o.stdout).trim().to_string(),
            Err(_) => String::new(),
        }
    }
}

fn os_name() -> String {
    match std::env::consts::OS {
        "windows" => "Windows".to_string(),
        "linux" => "Linux".to_string(),
        "macos" => "Darwin".to_string(),
        other => other.to_string(),
    }
}

/// `platform.machine()` 的名字 —— **五执行器必须逐字一致**（`probe_stdlib.py` 会比）。
///
/// 坑都在「Rust 的名字与宿主 `uname -m` 不一样」上：
/// * Windows：Python 给 `AMD64`，Rust 的 `ARCH` 是 `x86_64` ⇒ 要翻；
/// * **macOS arm64**：Apple 的 `uname -m` 给 **`arm64`**，而 Rust 的 `ARCH` 是
///   **`aarch64`** ⇒ 也要翻（2026-10-10 在 CI 的 macos-arm64 上抓到，
///   Windows / Linux 都复现不了）；
/// * Linux：两边都叫 `x86_64` / `aarch64` ⇒ 原样即可。
fn machine() -> String {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("windows", "x86_64") => "AMD64".to_string(),
        ("windows", "x86") => "x86".to_string(),
        ("windows", "aarch64") => "ARM64".to_string(),
        ("macos", "aarch64") => "arm64".to_string(),
        (_, arch) => arch.to_string(),
    }
}

/// `platform.processor()`：Windows 上是 `PROCESSOR_IDENTIFIER`，
/// 其它平台 Python 一般给空串（于是模块给「未知」）。
fn processor() -> String {
    if cfg!(windows) {
        if let Ok(v) = std::env::var("PROCESSOR_IDENTIFIER") {
            if !v.is_empty() {
                return v;
            }
        }
    }
    String::new()
}

fn hostname() -> String {
    if let Ok(v) = std::env::var("COMPUTERNAME") {
        if !v.is_empty() {
            return v;
        }
    }
    if let Ok(v) = std::env::var("HOSTNAME") {
        if !v.is_empty() {
            return v;
        }
    }
    for f in ["/proc/sys/kernel/hostname", "/etc/hostname"] {
        if let Ok(s) = std::fs::read_to_string(f) {
            let s = s.trim().to_string();
            if !s.is_empty() {
                return s;
            }
        }
    }
    String::new()
}

// ---------------------------------------------------------------------------
// 执行外部命令
// ---------------------------------------------------------------------------

/// 一条命令的**纯文本表示**（不含任何 `Val`）。
///
/// 🔴 为什么要多这一层（M134，2026-10-24）：`Val` 里含 `Rc`（字符串/精确小数/
/// 日期/切片都是），而 **`Rc` 不是 `Send`** —— 拿 `Val` 去多线程直接编译不过。
/// 这正是 M114 那条规矩：**线程里一个 `Val` 都不跨**。
/// ⇒ 在主线程里把 `Val` 拆成纯 `String`，线程里只碰 `String`，回来再包成 `Val`。
pub(crate) enum 命令形 {
    /// 列表形式：程序 + 参数（**不经过 shell**，参数边界原样保留）。
    数组(Vec<String>),
    /// 文本形式：交给 shell 解析。
    文本(String),
}

/// 主线程里做：把 `Val` 拆成 `命令形`（这是**唯一**碰 `Val` 的地方）。
fn 拆命令(cmd: &Val) -> Result<命令形, crate::JishiError> {
    if matches!(cmd, Val::List(_)) {
        let parts = to_vec(cmd)?;
        if parts.is_empty() {
            return Err(err("值错误", "命令列表不能是空的"));
        }
        Ok(命令形::数组(parts.iter().map(as_text).collect()))
    } else {
        Ok(命令形::文本(as_text(cmd)))
    }
}

/// 线程里做：只碰 `命令形`（纯 String）。
fn 造命令(形: &命令形) -> Command {
    match 形 {
        命令形::数组(parts) => {
            let mut c = Command::new(&parts[0]);
            for a in &parts[1..] {
                c.arg(a);
            }
            c
        }
        命令形::文本(line) => {
            if cfg!(windows) {
                let mut c = Command::new("cmd");
                c.arg("/C").arg(line);
                c
            } else {
                let mut c = Command::new("sh");
                c.arg("-c").arg(line);
                c
            }
        }
    }
}

/// 造一个命令：列表按「程序 + 参数」走（不经过 shell），文本按 shell 走
/// ——与 Python 侧 `subprocess.run(list)` / `shell=True` 的分工一致。
fn build_command(cmd: &Val) -> Result<Command, crate::JishiError> {
    Ok(造命令(&拆命令(cmd)?))
}

/// 跑命令并收集输出。超时（秒）到就杀进程并报错。
fn run_capture(cmd: &Val, timeout: f64) -> Result<(i32, String, String), crate::JishiError> {
    let mut command = build_command(cmd)?;
    run_command(&mut command, timeout)
}

/// 同上，但接受已经拆好的 `命令形`（**并行时线程里走这条**，不碰 `Val`）。
fn run_capture_parts(形: &命令形, timeout: f64) -> Result<(i32, String, String), crate::JishiError> {
    let mut command = 造命令(形);
    run_command(&mut command, timeout)
}

fn run_command(command: &mut Command, timeout: f64) -> Result<(i32, String, String), crate::JishiError> {
    command.stdout(Stdio::piped()).stderr(Stdio::piped()).stdin(Stdio::null());
    let mut child = command
        .spawn()
        .map_err(|e| err("运行期错误", format!("执行命令失败：{e}")))?;

    let deadline = Instant::now() + Duration::from_secs_f64(timeout.max(0.1));
    let status = loop {
        match child.try_wait() {
            Ok(Some(s)) => break s,
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(err(
                        "运行期错误",
                        format!("命令执行超过 {timeout} 秒，已终止"),
                    ));
                }
                std::thread::sleep(Duration::from_millis(5));
            }
            Err(e) => return Err(err("运行期错误", format!("执行命令失败：{e}"))),
        }
    };

    let mut out = String::new();
    let mut errout = String::new();
    if let Some(mut h) = child.stdout.take() {
        let mut buf = Vec::new();
        let _ = h.read_to_end(&mut buf);
        out = String::from_utf8_lossy(&buf).to_string();
    }
    if let Some(mut h) = child.stderr.take() {
        let mut buf = Vec::new();
        let _ = h.read_to_end(&mut buf);
        errout = String::from_utf8_lossy(&buf).to_string();
    }
    Ok((status.code().unwrap_or(-1), out, errout))
}

fn timeout_of(v: Option<&Val>) -> Result<f64, crate::JishiError> {
    match v {
        Some(x) => Ok(as_num(x)?.max(0.1)),
        None => Ok(10.0),
    }
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "系统";
        "系统名" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("系统名", args, 0)?;
            Ok(Val::Str(os_name()))
        },
        "系统版本" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("系统版本", args, 0)?;
            Ok(Val::Str(ver::version()))
        },
        "机器架构" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("机器架构", args, 0)?;
            Ok(Val::Str(machine()))
        },
        // M120：逻辑核数。用 `available_parallelism`（**不是**自己数 `/proc`）——
        // 它会尊重 cgroup / 亲和性掩码，也就是「真能并行的路数」。
        "CPU核数" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("CPU核数", args, 0)?;
            let n = std::thread::available_parallelism()
                .map(|x| x.get())
                .unwrap_or(1);
            Ok(Val::Int(n as i128))
        },
        "处理器" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("处理器", args, 0)?;
            let p = processor();
            Ok(Val::Str(if p.is_empty() { "未知".to_string() } else { p }))
        },
        "当前目录" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("当前目录", args, 0)?;
            let d = std::env::current_dir()
                .map_err(|e| err("运行期错误", format!("取当前目录失败：{e}")))?;
            Ok(Val::Str(d.to_string_lossy().to_string()))
        },
        "改目录" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("改目录", args, 1)?;
            let p = as_text(&args[0]);
            if !std::path::Path::new(&p).is_dir() {
                return Err(err("值错误", format!("「{p}」不是目录")));
            }
            std::env::set_current_dir(&p)
                .map_err(|e| err("运行期错误", format!("切换到「{p}」失败：{e}")))?;
            let d = std::env::current_dir()
                .map_err(|e| err("运行期错误", format!("取当前目录失败：{e}")))?;
            Ok(Val::Str(d.to_string_lossy().to_string()))
        },
        "环境变量" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("环境变量", args, 1, 2)?;
            let name = as_text(&args[0]);
            let default = if args.len() > 1 { as_text(&args[1]) } else { String::new() };
            Ok(Val::Str(std::env::var(&name).unwrap_or(default)))
        },
        "设环境变量" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("设环境变量", args, 2)?;
            std::env::set_var(as_text(&args[0]), as_text(&args[1]));
            Ok(Val::None_)
        },
        "执行" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("执行", args, 1, 2)?;
            let t = timeout_of(args.get(1))?;
            let (_code, out, errout) = run_capture(&args[0], t)?;
            // Python 侧是 `(r.stdout or "") + (r.stderr or "")`
            Ok(Val::Str(format!("{out}{errout}")))
        },
        // M133：**两条流分开**返回。`执行` 是老接口（stdout+stderr 拼一起），
        // 对拍要分开比 —— 「用户看到什么」是两条流加起来，合并了就只比了一半。
        "执行详细" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("执行详细", args, 1, 2)?;
            let t = timeout_of(args.get(1))?;
            let (code, out, errout) = run_capture(&args[0], t)?;
            Ok(crate::dict_new(vec![
                (Val::Str("输出".into()), Val::Str(out)),
                (Val::Str("错误".into()), Val::Str(errout)),
                (Val::Str("退出码".into()), Val::Int(code as i128)),
            ]))
        },
        // M134：**同时**跑一批命令，**结果顺序 = 输入顺序**（沿用 M114 那条）。
        // 复用 `concurrent::并行做` —— 它保序、首个错误胜出，wasm 上自动降级为顺序。
        "并行执行" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("并行执行", args, 1, 3)?;
            let 路数 = match args.get(1) {
                Some(v) => crate::stdlib::concurrent::线程数(v)?,
                None => 4,
            };
            let t = timeout_of(args.get(2))?;
            let 项们 = crate::stdlib::concurrent::列出(
                &args[0], "「并行执行」的第一个参数（命令们）")?;
            // 🔴 **进线程前先拆成 **： 含 、不是 ，
            //    直接传进  编译都过不去（M114 的规矩：线程里不跨 Val）。
            let 形们: Vec<命令形> = 项们.iter().map(拆命令).collect::<Result<_, _>>()?;
            let 结果们 = crate::stdlib::concurrent::并行做(形们, 路数, |形: &命令形| {
                run_capture_parts(形, t)
            })?;
            // 回来再包成 Val（主线程）
            let 列表: Vec<Val> = 结果们.into_iter().map(|(code, out, errout)| {
                crate::dict_new(vec![
                    (Val::Str("输出".into()), Val::Str(out)),
                    (Val::Str("错误".into()), Val::Str(errout)),
                    (Val::Str("退出码".into()), Val::Int(code as i128)),
                ])
            }).collect();
            Ok(crate::list_new(列表))
        },
        "退出码" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("退出码", args, 1, 2)?;
            let t = timeout_of(args.get(1))?;
            let (code, _, _) = run_capture(&args[0], t)?;
            Ok(Val::Int(code as i128))
        },
        "主机名" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("主机名", args, 0)?;
            Ok(Val::Str(hostname()))
        },
        "打开" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("打开", args, 1)?;
            let 目标 = as_text(&args[0]);
            Ok(Val::Bool(打开(&目标)))
        },
        "参数" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("参数", args, 0)?;
            let items: Vec<Val> = script_args().into_iter().map(Val::Str).collect();
            Ok(crate::list_new(items))
        },
    }
}
