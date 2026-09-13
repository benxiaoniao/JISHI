//! 标准库 `系统`（Python 侧 `jishi/stdlib/系统.py`，12 个函数）。
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

use std::collections::HashMap;
use std::io::Read;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use super::{as_num, as_text, need, need_range, script_args, table, to_vec, R};
use crate::{err, Val, VM};

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

/// `platform.machine()` 的名字。Windows 上 Python 给 `AMD64`，
/// 而 Rust 的 `ARCH` 一律是 `x86_64`——所以要翻译一次。
fn machine() -> String {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("windows", "x86_64") => "AMD64".to_string(),
        ("windows", "x86") => "x86".to_string(),
        ("windows", "aarch64") => "ARM64".to_string(),
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

/// 造一个命令：列表按「程序 + 参数」走（不经过 shell），文本按 shell 走
/// ——与 Python 侧 `subprocess.run(list)` / `shell=True` 的分工一致。
fn build_command(cmd: &Val) -> Result<Command, crate::JishiError> {
    if matches!(cmd, Val::List(_)) {
        let parts = to_vec(cmd)?;
        if parts.is_empty() {
            return Err(err("值错误", "命令列表不能是空的"));
        }
        let prog = as_text(&parts[0]);
        let mut c = Command::new(&prog);
        for a in &parts[1..] {
            c.arg(as_text(a));
        }
        Ok(c)
    } else {
        let line = as_text(cmd);
        if cfg!(windows) {
            let mut c = Command::new("cmd");
            c.arg("/C").arg(&line);
            Ok(c)
        } else {
            let mut c = Command::new("sh");
            c.arg("-c").arg(&line);
            Ok(c)
        }
    }
}

/// 跑命令并收集输出。超时（秒）到就杀进程并报错。
fn run_capture(cmd: &Val, timeout: f64) -> Result<(i32, String, String), crate::JishiError> {
    let mut command = build_command(cmd)?;
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
    table! {
        "系统名" => |args: &[Val], _vm: &mut VM| -> R {
            need("系统名", args, 0)?;
            Ok(Val::Str(os_name()))
        },
        "系统版本" => |args: &[Val], _vm: &mut VM| -> R {
            need("系统版本", args, 0)?;
            Ok(Val::Str(ver::version()))
        },
        "机器架构" => |args: &[Val], _vm: &mut VM| -> R {
            need("机器架构", args, 0)?;
            Ok(Val::Str(machine()))
        },
        "处理器" => |args: &[Val], _vm: &mut VM| -> R {
            need("处理器", args, 0)?;
            let p = processor();
            Ok(Val::Str(if p.is_empty() { "未知".to_string() } else { p }))
        },
        "当前目录" => |args: &[Val], _vm: &mut VM| -> R {
            need("当前目录", args, 0)?;
            let d = std::env::current_dir()
                .map_err(|e| err("运行期错误", format!("取当前目录失败：{e}")))?;
            Ok(Val::Str(d.to_string_lossy().to_string()))
        },
        "改目录" => |args: &[Val], _vm: &mut VM| -> R {
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
        "环境变量" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("环境变量", args, 1, 2)?;
            let name = as_text(&args[0]);
            let default = if args.len() > 1 { as_text(&args[1]) } else { String::new() };
            Ok(Val::Str(std::env::var(&name).unwrap_or(default)))
        },
        "设环境变量" => |args: &[Val], _vm: &mut VM| -> R {
            need("设环境变量", args, 2)?;
            std::env::set_var(as_text(&args[0]), as_text(&args[1]));
            Ok(Val::None_)
        },
        "执行" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("执行", args, 1, 2)?;
            let t = timeout_of(args.get(1))?;
            let (_code, out, errout) = run_capture(&args[0], t)?;
            // Python 侧是 `(r.stdout or "") + (r.stderr or "")`
            Ok(Val::Str(format!("{out}{errout}")))
        },
        "退出码" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("退出码", args, 1, 2)?;
            let t = timeout_of(args.get(1))?;
            let (code, _, _) = run_capture(&args[0], t)?;
            Ok(Val::Int(code as i128))
        },
        "主机名" => |args: &[Val], _vm: &mut VM| -> R {
            need("主机名", args, 0)?;
            Ok(Val::Str(hostname()))
        },
        "参数" => |args: &[Val], _vm: &mut VM| -> R {
            need("参数", args, 0)?;
            let items: Vec<Val> = script_args().into_iter().map(Val::Str).collect();
            Ok(crate::list_new(items))
        },
    }
}
