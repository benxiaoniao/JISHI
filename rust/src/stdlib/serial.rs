//! `串口`（M232 / L-4）：和硬件说几句话。
//!
//! ## 为什么要有它
//!
//! 《磐石 IDE》为了连开发板，在中间挂了套 PowerShell 的 `System.IO.Ports.SerialPort`
//! 助手 —— 一个字都不可控（起进程、等输出、编码还可能错），而且**读不到原始字节**。
//! 这一站把它做进语言，那个助手可以退役。
//!
//! ## API
//!
//! ```text
//! 串口.列出()                                              → [{口, 描述}]
//! 串口.打开(口, 波特率=115200, 数据位=8, 停止位=1, 校验="无")  → 句柄
//! 句柄.写文本(文本) / 写字节(字节)
//! 句柄.读行(超时=空) / 读可用()
//! 句柄.关闭()
//! ```
//!
//! ## 🔴 三条硬事实（写在最前面，因为都是「不知道就会卡半天」的）
//!
//! 1. **一次打开独占**。同一个口被别的程序（或上一次没关的句柄）占着，`CreateFile`
//!    直接给 `ERROR_ACCESS_DENIED`。所以 `关闭()` 不是可选的礼貌，是必须做的收尾。
//! 2. **`COM10` 及以上必须写 `\\.\COM10`**。裸 `COM10` 会被当成文件名去打开
//!    （Windows 的历史包袱）。这里**一律加前缀**，用户写 `COM3` 就行。
//! 3. **超时是三条一起设的**。`COMMTIMEOUTS` 里只设一条、另外两条留 0 的话，
//!    `ReadFile` 会「读到一半永远返回」—— 这是串口编程最经典的坑。
//!    这里的口径是：`ReadIntervalTimeout = MAXDWORD`（读到多少立刻返回多少）、
//!    `ReadTotalTimeoutMultiplier = 0`、`ReadTotalTimeoutConstant = 要等的毫秒数`。
//!
//! ## 平台
//!
//! | 平台 | 怎么做的 |
//! |---|---|
//! | Windows | 裸 Win32（`CreateFileW` + `SetCommState` + `SetCommTimeouts`），零第三方依赖 |
//! | Linux | 裸 termios（`open` + `ioctl(TCGETS/TCSETS)` + `poll`），零第三方依赖 |
//! | macOS | 裸 termios（`TIOCGETA/TIOCSETA`），零第三方依赖 |
//! | wasm（浏览器/页面） | 物理上不存在，报平台错 |
//!
//! 🔴 **POSIX 两端看着像、其实到处不一样**，所以常量全部按 `target_os` 分开写，
//! 一行都不共用（写错的表现是「能打开、收到乱码」或者「静默用默认参数」）：
//!
//! | 东西 | Linux | macOS |
//! |---|---|---|
//! | `struct termios` | 4×u32 + `c_line` + `c_cc[32]` + 两个 u32（60 字节） | 4×u64 + `c_cc[20]` + 两个 u64（72 字节） |
//! | 取/设设置 | `ioctl(TCGETS/TCSETS)` 0x5401 / 0x5402 | `ioctl(TIOCGETA/TIOCSETA)` 0x40487413 / 0x80487414 |
//! | 波特率放哪 | **`c_cflag` 里的位**（`CBAUD` 段） | **`c_ispeed`/`c_ospeed` 两个字段** |
//! | 波特率常量 | `B9600 = 13`、`B115200 = 0x1002`（**不是数字本身**） | `B9600 = 9600`、`B115200 = 115200`（就是数字本身） |
//! | `CSIZE` / `CS8` | 0o60 / 0o60 | 0x300 / 0x300 |
//! | `CREAD` / `CSTOPB` | 0o200 / 0o100 | 0x800 / 0x400 |
//! | `PARENB` / `PARODD` | 0o400 / 0o1000 | 0x1000 / 0x2000 |
//! | `O_NOCTTY` / `O_NONBLOCK` | 0o400 / 0o4000 | 0x20000 / 0x4 |
//! | `EAGAIN` | 11 | **35** |
//! | `FIONREAD` | 0x541B | 0x4004667F |
//!
//! 还有一条**不是常量表能提醒的**：macOS 上要打开 `/dev/cu.*`（callout），
//! **不要 `/dev/tty.*`** —— `tty.*` 会等 DCD 载波，插拔时读会直接挂住。
//!
//! 📌 共用的口径（三平台一致）：`读行` 到点给 `空`（超时不是错误）、
//! `关闭()` 幂等、句柄一次独占、参数写错**当场**报且列出可用写法。

#[allow(unused_imports)]
use std::collections::HashMap;

use crate::{err, err_with_hint, JishiError, Val};

/// 本模块的错误类型名（`类型()` 也用它）。
const 类型名: &str = "串口";

pub(crate) fn module() -> HashMap<String, Val> {
    let mut m = HashMap::new();
    m.insert(
        "列出".to_string(),
        crate::builtin("列出", |_args, _vm| 列出()),
    );
    m.insert(
        "打开".to_string(),
        crate::builtin("打开", |args, _vm| 打开(args)),
    );
    m
}

// ---------------------------------------------------------------------------
// 句柄
// ---------------------------------------------------------------------------

/// 句柄的状态（活在 `NativeObj.state` 里）。
pub(crate) struct 串口状态 {
    /// 平台句柄：Windows 是 `HANDLE`（`isize`），POSIX 是 `fd`（`i32` 塞进 `isize`）。
    /// 已关闭时恒为 -1。
    句柄: isize,
    已关: bool,
    /// 按行读时的**跨次残留**：一次 `ReadFile` 常常读到好几行（甚至半行）。
    /// 不缓冲的话，`读行` 每调一次就从设备读一次，行会被切碎。
    缓冲: Vec<u8>,
}

impl 串口状态 {
    fn 活着(&self) -> Result<(), JishiError> {
        if self.已关 {
            return Err(err("运行期错误", "这个串口已经关闭了（`关闭()` 之后不能再读写）"));
        }
        Ok(())
    }
}

fn 借(对象: &std::rc::Rc<crate::NativeObj>) -> std::cell::Ref<'_, 串口状态> {
    std::cell::Ref::map(对象.state.borrow(), |s| {
        s.downcast_ref::<串口状态>().expect("NativeObj 应是串口")
    })
}

fn 借改(对象: &std::rc::Rc<crate::NativeObj>) -> std::cell::RefMut<'_, 串口状态> {
    std::cell::RefMut::map(对象.state.borrow_mut(), |s| {
        s.downcast_mut::<串口状态>().expect("NativeObj 应是串口")
    })
}

// ---------------------------------------------------------------------------
// 参数
// ---------------------------------------------------------------------------

fn 文本(v: &Val, 名: &str) -> Result<String, JishiError> {
    match v {
        Val::Str(s) => Ok(s.to_string()),
        _ => Err(err(
            "类型错误",
            format!("「{名}」要传文本（现在是「{}」）", crate::type_label(v)),
        )),
    }
}

fn 整数(v: &Val, 名: &str) -> Result<i128, JishiError> {
    match v {
        Val::Int(i) => Ok(i.get()),
        _ => Err(err(
            "类型错误",
            format!("「{名}」要传整数（现在是「{}」）", crate::type_label(v)),
        )),
    }
}

/// 超时（秒）→ 毫秒。`空` = 一直等（返回 `None`）。
///
/// ⚠️ 与 `进程.读行` 同一个口径：**超时不是错误**，到了就给 `空`。
fn 超时毫秒(v: Option<&Val>) -> Result<Option<u32>, JishiError> {
    match v {
        None | Some(Val::None_) => Ok(None),
        Some(Val::Float(f)) => Ok(Some(((*f).max(0.0) * 1000.0) as u32)),
        Some(Val::Int(i)) => Ok(Some((i.get().max(0) as u64).min(u32::MAX as u64) as u32)),
        Some(other) => Err(err(
            "类型错误",
            format!("超时要传数字（秒，`空` = 一直等）；现在是「{}」", crate::type_label(other)),
        )),
    }
}

/// 校验方式：中文名 → DCB 的 `Parity` 常量。
///
/// 认不出来**当场报错并列出可用的** —— 串口参数写错最恶心的一种是
/// 「静默用默认值」，症状是收到乱码，人会先去怀疑波特率、线、板子，最后才想到校验。
fn 校验码(名: &str) -> Result<u8, JishiError> {
    Ok(match 名 {
        "无" | "none" | "None" | "N" => 0,      // NOPARITY
        "奇" | "odd" | "Odd" | "O" => 1,        // ODDPARITY
        "偶" | "even" | "Even" | "E" => 2,      // EVENPARITY
        "标记" | "mark" | "Mark" | "M" => 3,    // MARKPARITY
        "空格" | "space" | "Space" | "S" => 4,  // SPACEPARITY
        other => {
            return Err(err_with_hint(
                "值错误",
                format!("认不出校验方式「{other}」"),
                "可用的写法：「无」（默认）/「奇」/「偶」/「标记」/「空格」".into(),
            ))
        }
    })
}

/// 把 COM 口名规整成 Win32 认的形状。
///
/// 🔴 `COM10` 及以上必须带 `\\.\` 前缀，否则 `CreateFile` 会把它当**文件名**
/// （Windows 为兼容 DOS 保留的行为）。这里一律加，用户写 `COM3` 就对。
#[cfg(windows)]
fn 设备路径(口: &str) -> Vec<u16> {
    let 净 = 口.trim();
    let 带前缀 = if 净.starts_with("\\\\.\\") || 净.starts_with("//./") {
        净.to_string()
    } else {
        format!("\\\\.\\{净}")
    };
    带前缀.encode_utf16().chain(std::iter::once(0)).collect()
}

// ---------------------------------------------------------------------------
// 串口.列出
// ---------------------------------------------------------------------------

fn 列出() -> Result<Val, JishiError> {
    #[cfg(windows)]
    {
        return win32::列出();
    }
    #[cfg(all(unix, any(target_os = "linux", target_os = "macos")))]
    {
        return posix::列出();
    }
    #[cfg(not(any(windows, all(unix, any(target_os = "linux", target_os = "macos")))))]
    {
        Err(平台未实现("列出"))
    }
}

// ---------------------------------------------------------------------------
// 串口.打开
// ---------------------------------------------------------------------------

fn 打开(args: &[Val]) -> Result<Val, JishiError> {
    if args.is_empty() || args.len() > 5 {
        return Err(err(
            "类型错误",
            format!("「打开」要 1 到 5 个参数（口, 波特率=115200, 数据位=8, 停止位=1, 校验=\"无\"），实际给了 {} 个", args.len()),
        ));
    }
    let 口 = 文本(&args[0], "打开")?;
    let 波特率 = match args.get(1) {
        None | Some(Val::None_) => 115200,
        Some(v) => {
            let n = 整数(v, "打开")?;
            if n <= 0 {
                return Err(err("值错误", format!("波特率要是正整数，给的是 {n}（常见：9600 / 115200）")));
            }
            n as u32
        }
    };
    let 数据位 = match args.get(2) {
        None | Some(Val::None_) => 8u8,
        Some(v) => {
            let n = 整数(v, "打开")?;
            if !(5..=8).contains(&n) {
                return Err(err("值错误", format!("数据位只能是 5 到 8，给的是 {n}（绝大多数设备是 8）")));
            }
            n as u8
        }
    };
    let 停止位 = match args.get(3) {
        None | Some(Val::None_) => 1u8,
        Some(v) => {
            let n = 整数(v, "打开")?;
            // 原样带下去（1 / 2 / 3），**每个平台自己翻** —— Win32 的 1.5 位是
            // `ONE5STOPBITS = 0`，POSIX 干脆没有 1.5 位。在这里折成 0 会让
            // POSIX 侧把「1.5 位」当成「1 位」**静默接受**。
            match n {
                1..=3 => n as u8,
                _ => return Err(err("值错误", format!("停止位只能是 1 / 2 / 3（3 = 1.5 位），给的是 {n}"))),
            }
        }
    };
    let 校验 = match args.get(4) {
        None | Some(Val::None_) => 0,
        Some(v) => 校验码(&文本(v, "打开")?)?,
    };

    #[cfg(windows)]
    {
        return win32::打开(&口, 波特率, 数据位, 停止位, 校验);
    }
    #[cfg(all(unix, any(target_os = "linux", target_os = "macos")))]
    {
        return posix::打开(&口, 波特率, 数据位, 停止位, 校验);
    }
    #[cfg(not(any(windows, all(unix, any(target_os = "linux", target_os = "macos")))))]
    {
        let _ = (口, 波特率, 数据位, 停止位, 校验);
        Err(平台未实现("打开"))
    }
}

// ---------------------------------------------------------------------------
// 句柄方法
// ---------------------------------------------------------------------------

fn 写文本(对象: &std::rc::Rc<crate::NativeObj>, args: &[Val], _vm: &mut dyn crate::BuiltinHost) -> Result<Val, JishiError> {
    if args.len() != 1 {
        return Err(err("类型错误", format!("「写文本」要 1 个参数（文本），实际给了 {} 个", args.len())));
    }
    let 文 = 文本(&args[0], "写文本")?;
    写原始(对象, 文.as_bytes())
}

fn 写字节(对象: &std::rc::Rc<crate::NativeObj>, args: &[Val], _vm: &mut dyn crate::BuiltinHost) -> Result<Val, JishiError> {
    if args.len() != 1 {
        return Err(err("类型错误", format!("「写字节」要 1 个参数（字节 或 0..255 的整数列表），实际给了 {} 个", args.len())));
    }
    let 原始: Vec<u8> = match &args[0] {
        Val::Bytes(b) => b.to_vec(),
        Val::List(l) => {
            let mut out = Vec::new();
            for (i, v) in l.borrow().iter().enumerate() {
                match v {
                    Val::Int(n) => {
                        let x = n.get();
                        if !(0..=255).contains(&x) {
                            return Err(err("值错误", format!(
                                "「写字节」第 {} 项是 {x} —— 一个字节只能是 0 到 255（要发更大的数就拆成几个字节）", i + 1)));
                        }
                        out.push(x as u8);
                    }
                    other => {
                        return Err(err("类型错误", format!(
                            "「写字节」的列表第 {} 项要是整数，现在是「{}」", i + 1, crate::type_label(other))));
                    }
                }
            }
            out
        }
        other => {
            return Err(err("类型错误", format!(
                "「写字节」要传字节或整数列表（现在是「{}」）；要发文本用 `写文本`",
                crate::type_label(other))));
        }
    };
    写原始(对象, &原始)
}

/// 真正的写（`写文本` / `写字节` 最后都走这里）。
fn 写原始(对象: &std::rc::Rc<crate::NativeObj>, 数据: &[u8]) -> Result<Val, JishiError> {
    let 句 = {
        let st = 借(对象);
        st.活着()?;
        st.句柄
    };
    #[cfg(windows)]
    {
        win32::写(句, 数据)?;
        return Ok(Val::Int(crate::jint::JInt::new(数据.len() as i128)));
    }
    #[cfg(all(unix, any(target_os = "linux", target_os = "macos")))]
    {
        posix::写(句 as i32, 数据)?;
        return Ok(Val::Int(crate::jint::JInt::new(数据.len() as i128)));
    }
    #[cfg(not(any(windows, all(unix, any(target_os = "linux", target_os = "macos")))))]
    {
        let _ = (句, 数据);
        Err(平台未实现("写文本"))
    }
}

fn 读行(对象: &std::rc::Rc<crate::NativeObj>, args: &[Val], _vm: &mut dyn crate::BuiltinHost) -> Result<Val, JishiError> {
    if args.len() > 1 {
        return Err(err("类型错误", format!("「读行」最多 1 个参数（超时，秒），实际给了 {} 个", args.len())));
    }
    let 限 = 超时毫秒(args.first())?;
    let 截止 = 限.map(|ms| std::time::Instant::now() + std::time::Duration::from_millis(ms as u64));
    loop {
        // ① 缓冲里已经有整行？
        {
            let mut st = 借改(对象);
            st.活着()?;
            if let Some(位) = st.缓冲.iter().position(|b| *b == b'\n') {
                let mut 行: Vec<u8> = st.缓冲.drain(..=位).collect();
                行.pop();                                  // 去掉 `\n`
                if 行.last() == Some(&b'\r') {
                    行.pop();                              // 去掉 `\r`（串口设备常给 CRLF）
                }
                return Ok(Val::Str(
                    String::from_utf8_lossy(&行).into_owned().into(),
                ));
            }
        }
        // ② 没有就去读一段
        let 还剩 = match 截止 {
            None => 200,                                   // 「一直等」也按 200ms 一片轮询
            Some(t) => {
                let now = std::time::Instant::now();
                if now >= t {
                    return Ok(Val::None_);                 // 超时 = 这一拍没东西，不是错误
                }
                ((t - now).as_millis() as u32).min(200).max(1)
            }
        };
        let 收到 = 读一段(对象, 还剩)?;
        if 收到 == 0 && 截止.is_some() {
            // 空读：由循环顶上的截止判断决定是继续等还是给 `空`
            continue;
        }
    }
}

fn 读可用(对象: &std::rc::Rc<crate::NativeObj>, _args: &[Val], _vm: &mut dyn crate::BuiltinHost) -> Result<Val, JishiError> {
    // 先把「现在已经在路上」的字节吸进缓冲，再报数 —— 与 `进程.读可用` 同一口径。
    读一段(对象, 0)?;
    let st = 借(对象);
    st.活着()?;
    Ok(Val::Int(crate::jint::JInt::new(st.缓冲.len() as i128)))
}

/// 从设备读一段进缓冲，返回**这次读到几个字节**（0 = 没读到）。
///
/// `超时毫秒 = 0` ⇒ 立刻返回（有多少给多少）。
fn 读一段(对象: &std::rc::Rc<crate::NativeObj>, 超时毫秒: u32) -> Result<usize, JishiError> {
    let 句 = {
        let st = 借(对象);
        st.活着()?;
        st.句柄
    };
    #[cfg(windows)]
    {
        let 片 = win32::读(句, 超时毫秒)?;
        let n = 片.len();
        if n > 0 {
            借改(对象).缓冲.extend_from_slice(&片);
        }
        Ok(n)
    }
    #[cfg(all(unix, any(target_os = "linux", target_os = "macos")))]
    {
        let 片 = posix::读(句 as i32, 超时毫秒)?;
        let n = 片.len();
        if n > 0 {
            借改(对象).缓冲.extend_from_slice(&片);
        }
        Ok(n)
    }
    #[cfg(not(any(windows, all(unix, any(target_os = "linux", target_os = "macos")))))]
    {
        let _ = (句, 超时毫秒);
        Err(平台未实现("读行"))
    }
}

fn 关闭(对象: &std::rc::Rc<crate::NativeObj>, _args: &[Val], _vm: &mut dyn crate::BuiltinHost) -> Result<Val, JishiError> {
    let mut st = 借改(对象);
    if st.已关 {
        return Ok(Val::Bool(false));       // 幂等：重复关闭不报错，如实说「本来就没开」
    }
    #[cfg(windows)]
    {
        if st.句柄 >= 0 {
            win32::关闭(st.句柄);
        }
    }
    #[cfg(all(unix, any(target_os = "linux", target_os = "macos")))]
    {
        if st.句柄 >= 0 {
            posix::关闭(st.句柄 as i32);
        }
    }
    st.句柄 = -1;
    st.已关 = true;
    st.缓冲.clear();
    Ok(Val::Bool(true))
}

/// 方法表（编译期常量，`NativeObj` 要求的形状）。
static 方法们: &[(&str, crate::NativeFn)] = &[
    ("写文本", 写文本),
    ("写字节", 写字节),
    ("读行", 读行),
    ("读可用", 读可用),
    ("关闭", 关闭),
];

/// 只剩 **wasm（浏览器/页面）** 走这里 —— 物理上没有串口。
///
/// 📌 Windows / Linux / macOS 现在**都有实现**（M232 收尾：原来 Linux/mac 是
/// 「故意留白」，2026-11-25 用户定「补上，免得少一部分东西」）。
#[cfg(not(any(windows, all(unix, any(target_os = "linux", target_os = "macos")))))]
fn 平台未实现(操作: &str) -> JishiError {
    err_with_hint(
        "运行期错误",
        format!("`串口.{操作}` 在浏览器里用不了（网页上没有串口设备）"),
        "串口要跑在桌面宿主（Windows / Linux / macOS 的 Rust 发行版）上；\
         页面上连硬件的正规做法是 Web Serial API，不是这个模块"
            .into(),
    )
}

// ---------------------------------------------------------------------------
// Windows 裸 Win32
// ---------------------------------------------------------------------------

#[cfg(windows)]
mod win32 {
    use super::*;

    type Handle = isize;

    const GENERIC_READ: u32 = 0x8000_0000;
    const GENERIC_WRITE: u32 = 0x4000_0000;
    const OPEN_EXISTING: u32 = 3;
    const FILE_ATTRIBUTE_NORMAL: u32 = 0x80;

    /// `DCB` 的 flags 位（winbase.h）。
    const F_BINARY: u32 = 1 << 0;
    const F_PARITY: u32 = 1 << 1;
    /// `fDtrControl = DTR_CONTROL_ENABLE`（bit4-5 = 01）
    const F_DTR_ENABLE: u32 = 1 << 4;
    /// `fRtsControl = RTS_CONTROL_ENABLE`（bit12-13 = 01）
    const F_RTS_ENABLE: u32 = 1 << 12;

    /// `PURGE_TXCLEAR | PURGE_RXCLEAR`
    const PURGE_ALL: u32 = 0x0004 | 0x0008;

    const MAXDWORD: u32 = 0xFFFF_FFFF;

    /// `DCB`（设备控制块）。手写布局，**字段顺序与 `winbase.h` 逐条对应** ——
    /// 错一个字段，`GetCommState` 填出来的东西就是乱的（而且不会报错）。
    #[repr(C)]
    #[derive(Default)]
    pub(super) struct Dcb {
        pub dcb_length: u32,
        pub baud_rate: u32,
        /// 位域（`fBinary:1` … `fDummy2:1`）—— 整 32 位当 `u32` 用。
        pub flags: u32,
        pub w_reserved: u16,
        pub xon_lim: u16,
        pub xoff_lim: u16,
        pub byte_size: u8,
        pub parity: u8,
        pub stop_bits: u8,
        pub xon_char: i8,
        pub xoff_char: i8,
        pub error_char: i8,
        pub eof_char: i8,
        pub evt_char: i8,
        pub w_reserved1: u16,
    }

    /// `COMMTIMEOUTS`。
    #[repr(C)]
    #[derive(Default)]
    pub(super) struct CommTimeouts {
        pub read_interval_timeout: u32,
        pub read_total_multiplier: u32,
        pub read_total_constant: u32,
        pub write_total_multiplier: u32,
        pub write_total_constant: u32,
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn CreateFileW(
            路径: *const u16,
            访问: u32,
            共享: u32,
            安全: *mut std::ffi::c_void,
            创建: u32,
            标志: u32,
            模板: Handle,
        ) -> Handle;
        fn CloseHandle(句柄: Handle) -> i32;
        fn ReadFile(句柄: Handle, 缓冲: *mut u8, 要读: u32, 读到: *mut u32,
                    重叠: *mut std::ffi::c_void) -> i32;
        fn WriteFile(句柄: Handle, 数据: *const u8, 要写: u32, 写到: *mut u32,
                     重叠: *mut std::ffi::c_void) -> i32;
        fn GetCommState(句柄: Handle, dcb: *mut Dcb) -> i32;
        fn SetCommState(句柄: Handle, dcb: *const Dcb) -> i32;
        fn SetCommTimeouts(句柄: Handle, t: *const CommTimeouts) -> i32;
        fn PurgeComm(句柄: Handle, 标志: u32) -> i32;
        fn SetupComm(句柄: Handle, 入: u32, 出: u32) -> i32;
    }

    #[link(name = "advapi32")]
    extern "system" {
        fn RegOpenKeyExW(根: Handle, 子键: *const u16, 选项: u32, 权限: u32,
                         结果: *mut Handle) -> i32;
        fn RegEnumValueW(键: Handle, 序: u32, 名: *mut u16, 名长: *mut u32,
                         保留: *mut u32, 类型: *mut u32, 数据: *mut u8,
                         数据长: *mut u32) -> i32;
        fn RegCloseKey(键: Handle) -> i32;
    }

    const HKEY_LOCAL_MACHINE: Handle = 0x8000_0002u32 as Handle;
    const KEY_READ: u32 = 0x0002_0019;
    const ERROR_NO_MORE_ITEMS: i32 = 259;
    const ERROR_SUCCESS: i32 = 0;

    /// `ERROR_ACCESS_DENIED`（打开「存在的口」时被占；**也可能是口不存在**，
    /// 所以判存不存在要查 `口们()`，不能只看这个码）
    const DENIED: i32 = 5;

    fn 最后错误() -> i32 {
        std::io::Error::last_os_error().raw_os_error().unwrap_or(0)
    }

    /// 打开失败 → **人话**。
    ///
    /// 🔴 **先查「这个口到底存不存在」，不要猜错误码**（2026-11-25 实测踩到）：
    /// 打开 `\\.\COM99` 时 Windows 给的是 `ERROR_ACCESS_DENIED (5)`，**不是**
    /// `FILE_NOT_FOUND` —— 于是最初的版本把「没这个口」报成了「被别的程序占着」，
    /// 而用户照那句提示去关串口助手，永远关不出结果。
    /// 注册表 `SERIALCOMM` 是 Windows 自己维护的口名表，问它比猜码准。
    fn 打开错误(口: &str, 码: i32) -> JishiError {
        let 净 = 口.trim().trim_start_matches("\\\\.\\");
        let 存在 = 口们().iter().any(|(名, _)| 名.eq_ignore_ascii_case(净));
        if !存在 {
            return err_with_hint(
                "运行期错误",
                format!("没有这个串口「{口}」（系统里找不到它）"),
                "先 `打印(串口.列出())` 看看有哪些口；USB 转串口的线插上要装驱动才会出现".into(),
            );
        }
        if 码 == DENIED {
            return err_with_hint(
                "运行期错误",
                format!("串口「{口}」打不开 —— 被别的程序占着（也可能是上一次没 `关闭()`）"),
                "串口是**一次只能一个程序打开**的：关掉串口助手 / 上次跑的程序，再试".into(),
            );
        }
        let 系统 = std::io::Error::from_raw_os_error(码);
        err_with_hint(
            "运行期错误",
            format!("打开串口「{口}」失败：{系统}（系统错误码 {码}）"),
            "要是参数（波特率 / 数据位）被驱动拒了，换个波特率试试；其余情况看设备管理器".into(),
        )
    }

    pub(super) fn 打开(口: &str, 波特率: u32, 数据位: u8, 停止位: u8, 校验: u8)
        -> Result<Val, JishiError>
    {
        let 路径 = super::设备路径(口);
        // ⚠️ **同步句柄**（不加 `FILE_FLAG_OVERLAPPED`）：超时由 `COMMTIMEOUTS`
        //    精确控制，而同步读写比重叠 IO 少一层事件/等待，行为更好预测。
        //    📌 路线图原文写的是「要 OVERLAPPED 才不阻塞」—— 那句只在**没设超时**
        //    时成立；这里三条超时都设了，`ReadFile` 到点就返回（见文件头第 3 条）。
        let h = unsafe {
            CreateFileW(
                路径.as_ptr(),
                GENERIC_READ | GENERIC_WRITE,
                0,                                  // 共享模式 0 = 独占（串口就该独占）
                std::ptr::null_mut(),
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                -1,
            )
        };
        if h == -1 {
            return Err(打开错误(口, 最后错误()));
        }

        // 收发缓冲给大一点（默认 4KB，115200 下一个突发容易顶住）
        unsafe {
            SetupComm(h, 65536, 65536);
        }

        unsafe {
            let mut dcb = Dcb::default();
            dcb.dcb_length = std::mem::size_of::<Dcb>() as u32;
            if GetCommState(h, &mut dcb) == 0 {
                let 码 = 最后错误();
                CloseHandle(h);
                return Err(err("运行期错误", format!("读串口「{口}」的当前设置失败（系统错误码 {码}）")));
            }
            dcb.baud_rate = 波特率;
            dcb.byte_size = 数据位;
            dcb.parity = 校验;
            // 用户口径 1 / 2 / 3（3 = 1.5 位）→ DCB 口径 `ONESTOPBIT=0` /
            // `ONE5STOPBITS=1` / `TWOSTOPBITS=2`。**翻译只在这一层做**。
            dcb.stop_bits = match 停止位 {
                2 => 2,
                3 => 1,
                _ => 0,
            };
            // ⚠️ **在原有 flags 上改**，不要 `from_bits(0)` 重头造 ——
            //    有些驱动依赖 `fBinary` 之类的默认值，抹掉会出现「能打开但不收数据」。
            dcb.flags |= F_BINARY | F_DTR_ENABLE | F_RTS_ENABLE;
            if 校验 != 0 {
                dcb.flags |= F_PARITY;
            } else {
                dcb.flags &= !F_PARITY;
            }
            if SetCommState(h, &dcb) == 0 {
                let 码 = 最后错误();
                CloseHandle(h);
                return Err(err("运行期错误", format!(
                    "串口「{口}」不接受这套参数（波特率 {波特率} / 数据位 {数据位} / 校验 {校验}）：系统错误码 {码}")));
            }
        }

        设超时(h, 100);                                 // 打开时的默认读超时
        unsafe {
            PurgeComm(h, PURGE_ALL);                    // 丢掉驱动里可能残留的旧字节
        }

        Ok(crate::native_obj(
            类型名,
            super::方法们,
            std::rc::Rc::new(std::cell::RefCell::new(super::串口状态 {
                句柄: h,
                已关: false,
                缓冲: Vec::new(),
            })),
        ))
    }

    /// 三条超时**一起**设（见文件头第 3 条）。
    fn 设超时(h: Handle, 等毫秒: u32) {
        let t = CommTimeouts {
            read_interval_timeout: MAXDWORD,        // 读到多少立刻返回多少
            read_total_multiplier: 0,
            read_total_constant: 等毫秒,            // 没数据就等这么久
            write_total_multiplier: 0,
            write_total_constant: 5000,             // 写最多等 5 秒（不设会永久阻塞）
        };
        unsafe {
            SetCommTimeouts(h, &t);
        }
    }

    pub(super) fn 读(h: Handle, 等毫秒: u32) -> Result<Vec<u8>, JishiError> {
        设超时(h, 等毫秒);
        let mut 片 = vec![0u8; 8192];
        let mut 读到: u32 = 0;
        let ok = unsafe {
            ReadFile(h, 片.as_mut_ptr(), 片.len() as u32, &mut 读到,
                     std::ptr::null_mut())
        };
        if ok == 0 {
            let 码 = 最后错误();
            // 超时/被中断都不算错（这是「这一拍没数据」）
            if 码 == 0 || 码 == 121 || 码 == 995 {
                return Ok(Vec::new());
            }
            return Err(err("运行期错误", format!("从串口读失败（系统错误码 {码}）")));
        }
        片.truncate(读到 as usize);
        Ok(片)
    }

    pub(super) fn 写(h: Handle, 数据: &[u8]) -> Result<(), JishiError> {
        if 数据.is_empty() {
            return Ok(());
        }
        let mut 写到: u32 = 0;
        let ok = unsafe {
            WriteFile(h, 数据.as_ptr(), 数据.len() as u32, &mut 写到,
                      std::ptr::null_mut())
        };
        if ok == 0 {
            let 码 = 最后错误();
            return Err(err("运行期错误", format!("往串口写失败（系统错误码 {码}）")));
        }
        if (写到 as usize) < 数据.len() {
            return Err(err("运行期错误", format!(
                "往串口写只写出去 {} / {} 字节（对方可能没准备好，或者线没接对）", 写到, 数据.len())));
        }
        Ok(())
    }

    pub(super) fn 关闭(h: Handle) {
        unsafe {
            CloseHandle(h);
        }
    }

    /// `串口.列出`：读注册表 `HKLM\HARDWARE\DEVICEMAP\SERIALCOMM`。
    ///
    /// 这张表是 Windows 自己维护的「设备 → COM 名」映射，**真实串口和虚拟串口
    /// （com0com 之类）都在里面**，比枚举 `COM1..COM256` 试打开快得多、也不会
    /// 因为「打开失败」而影响别人的设备状态。
    ///
    /// ⚠️ **拿不到友好名**（「USB-SERIAL CH340 (COM3)」那种）。要友好名得上
    /// SetupAPI（`SetupDiGetClassDevs` + 注册表 `PortName`），常量几十个、
    /// 还要处理设备集的生命周期 —— 等真有人需要「区分两个 CH340」再做。
    /// 现在 `描述` 给空文本，**如实**表示「这个平台拿不到」。
    /// 注册表里的 `(口名, 设备键名)` 列表。
    ///
    /// `列出` 与「打开失败时判断这个口到底存不存在」**共用这一份** ——
    /// 后者是必需的：Windows 对不存在的 COM 名给的是 `ACCESS_DENIED`，
    /// 光看错误码会把「没这个口」误报成「被占用」（见 `打开错误` 的注释）。
    pub(super) fn 口们() -> Vec<(String, String)> {
        let 子键: Vec<u16> = r"HARDWARE\DEVICEMAP\SERIALCOMM"
            .encode_utf16()
            .chain(std::iter::once(0))
            .collect();
        let mut 键: Handle = 0;
        let rc = unsafe {
            RegOpenKeyExW(HKEY_LOCAL_MACHINE, 子键.as_ptr(), 0, KEY_READ, &mut 键)
        };
        if rc != ERROR_SUCCESS {
            // 一个串口都没有的机器上，这个键**可能压根不存在** —— 那是「空列表」，
            // 不是错误（把「没有」报成「出错」会让调用方以为环境坏了）。
            return Vec::new();
        }

        let mut 结果: Vec<(String, String)> = Vec::new();
        let mut 序: u32 = 0;
        loop {
            let mut 名 = [0u16; 512];
            let mut 名长: u32 = 名.len() as u32;
            let mut 值 = [0u8; 512];
            let mut 值长: u32 = 值.len() as u32;
            let rc = unsafe {
                RegEnumValueW(键, 序, 名.as_mut_ptr(), &mut 名长,
                              std::ptr::null_mut(), std::ptr::null_mut(),
                              值.as_mut_ptr(), &mut 值长)
            };
            if rc == ERROR_NO_MORE_ITEMS || rc != ERROR_SUCCESS {
                break;
            }
            序 += 1;
            // 值是 REG_SZ（UTF-16），长度按字节给 —— 转成 `u16` 切片再解码
            let 字们: Vec<u16> = 值[..值长 as usize]
                .chunks_exact(2)
                .map(|c| u16::from_le_bytes([c[0], c[1]]))
                .take_while(|c| *c != 0)
                .collect();
            let 口 = String::from_utf16_lossy(&字们);
            // `设备` 是注册表里的键名（`\Device\Serial0` / `\Device\VCP0`）——
            // 排查「两个口分不清」时有用，多给一个字段不碍事。
            let 键名 = String::from_utf16_lossy(
                &名[..名长 as usize].iter().copied().take_while(|c| *c != 0).collect::<Vec<_>>(),
            );
            结果.push((口, 键名));
        }
        unsafe {
            RegCloseKey(键);
        }
        结果
    }

    pub(super) fn 列出() -> Result<Val, JishiError> {
        let 项们: Vec<Val> = 口们()
            .into_iter()
            .map(|(口, 设备)| {
                crate::dict_new(vec![
                    (Val::Str("口".into()), Val::Str(口.into())),
                    // ⚠️ 注册表方案拿不到友好名 —— 给空文本**如实**表示「这个平台拿不到」，
                    //    而不是把 `口` 再抄一遍冒充描述。
                    (Val::Str("描述".into()), Val::Str("".into())),
                    (Val::Str("设备".into()), Val::Str(设备.into())),
                ])
            })
            .collect();
        Ok(Val::List(std::rc::Rc::new(std::cell::RefCell::new(项们))))
    }

    #[allow(dead_code)]
    pub(super) fn 说明() -> &'static str {
        "Windows 串口：CreateFileW + SetCommState + SetCommTimeouts（全同步，超时三条一起设）"
    }
}

// ---------------------------------------------------------------------------
// POSIX（Linux / macOS）裸 termios
// ---------------------------------------------------------------------------
//
// 🔴 **两平台的常量在这里一律分写**，共用一行都不行 —— 见文件头那张对照表。
// 这里的数值全部来自 `asm-generic/termbits.h`（Linux）与 `sys/termios.h`
// （Darwin），不是猜的；改动前先对照头文件。

#[cfg(all(unix, any(target_os = "linux", target_os = "macos")))]
mod posix {
    use super::*;

    type Fd = i32;

    // ---- 平台常量 ---------------------------------------------------------
    //
    // Linux（asm-generic/termbits.h）：
    //   CSIZE=0o60 CS6=0o20 CS7=0o40 CS8=0o60 CSTOPB=0o100 CREAD=0o200
    //   PARENB=0o400 PARODD=0o1000 CLOCAL=0o4000 CMSPAR=0o100000000 CBAUD=0o10017
    // macOS（sys/termios.h，tcflag_t 是 unsigned long）：
    //   CSIZE=0x300 CS6=0x100 CS7=0x200 CS8=0x300 CSTOPB=0x400 CREAD=0x800
    //   PARENB=0x1000 PARODD=0x2000 CLOCAL=0x8000

    #[cfg(target_os = "linux")]
    mod 位 {
        pub const CSIZE: u32 = 0o60;
        pub const CS5: u32 = 0;
        pub const CS6: u32 = 0o20;
        pub const CS7: u32 = 0o40;
        pub const CS8: u32 = 0o60;
        pub const CSTOPB: u32 = 0o100;
        pub const CREAD: u32 = 0o200;
        pub const PARENB: u32 = 0o400;
        pub const PARODD: u32 = 0o1000;
        pub const CLOCAL: u32 = 0o4000;
        /// `CMSPAR`（mark/space 校验）
        pub const CMSPAR: u32 = 0o100_000_000;
        /// `CBAUD`：`c_cflag` 里放波特率的那一段
        pub const CBAUD: u32 = 0o10017;
        /// `CRTSCTS`：硬流控（我们不开 —— 与 Win32 侧 `fRtsControl=ENABLE` 同口径）。
        /// 不显式清掉的话，设备默认开着它，`write` 会在对方不拉 CTS 时**卡住**。
        pub const CRTSCTS: u32 = 0o200_0000_0000;
        pub const BOTHER: u32 = 0o10000;
    }

    #[cfg(target_os = "macos")]
    mod 位 {
        pub const CSIZE: u64 = 0x300;
        pub const CS5: u64 = 0;
        pub const CS6: u64 = 0x100;
        pub const CS7: u64 = 0x200;
        pub const CS8: u64 = 0x300;
        pub const CSTOPB: u64 = 0x400;
        pub const CREAD: u64 = 0x800;
        pub const PARENB: u64 = 0x1000;
        pub const PARODD: u64 = 0x2000;
        pub const CLOCAL: u64 = 0x8000;
        /// 硬流控（RTS/CTS 两个方向）—— 同 Linux，我们不开
        pub const CCTS_OFLOW: u64 = 0x1_0000;
        pub const CRTS_IFLOW: u64 = 0x2_0000;
    }

    /// `open(2)` 的标志位。🔴 **两平台不同**：Linux 的 `O_NOCTTY=0o400`，
    /// Darwin 是 `0x20000`；`O_NONBLOCK` 是 0o4000 vs 0x4。
    #[cfg(target_os = "linux")]
    const O_RDWR: i32 = 2;
    #[cfg(target_os = "linux")]
    const O_NOCTTY: i32 = 0o400;
    #[cfg(target_os = "linux")]
    const O_NONBLOCK: i32 = 0o4000;
    #[cfg(target_os = "linux")]
    const O_CLOEXEC: i32 = 0o2_000_000;

    #[cfg(target_os = "macos")]
    const O_RDWR: i32 = 2;
    #[cfg(target_os = "macos")]
    const O_NOCTTY: i32 = 0x20000;
    #[cfg(target_os = "macos")]
    const O_NONBLOCK: i32 = 0x4;
    #[cfg(target_os = "macos")]
    const O_CLOEXEC: i32 = 0x100_0000;

    /// `ioctl` 的请求号（`_IOR`/`_IOW` 算出来的，不是随手编的数）。
    #[cfg(target_os = "linux")]
    const TCGETS: u64 = 0x5401;
    #[cfg(target_os = "linux")]
    const TCSETS: u64 = 0x5402;
    #[cfg(target_os = "linux")]
    const TCSETS2: u64 = 0x402C_542A;
    #[cfg(target_os = "linux")]
    const TIOCEXCL: u64 = 0x540C;

    #[cfg(target_os = "macos")]
    const TIOCGETA: u64 = 0x4048_7413;
    #[cfg(target_os = "macos")]
    const TIOCSETA: u64 = 0x8048_7414;
    #[cfg(target_os = "macos")]
    const TIOCEXCL: u64 = 0x2000_740D;

    /// `poll(2)` 的 `nfds_t`：Linux `unsigned long`(u64)，Darwin `unsigned int`(u32)。
    #[cfg(target_os = "linux")]
    type Nfds = u64;
    #[cfg(target_os = "macos")]
    type Nfds = u32;

    const POLLIN: i16 = 1;
    const POLLOUT: i16 = 4;
    const POLLERR: i16 = 8;
    const POLLHUP: i16 = 16;
    const POLLNVAL: i16 = 32;

    /// `tcflush(fd, TCIOFLUSH)`
    const TCIOFLUSH: i32 = 2;

    /// `errno`。🔴 `EAGAIN` 两平台**不一样**（11 / 35）。
    const EINTR: i32 = 4;
    #[cfg(target_os = "linux")]
    const EAGAIN: i32 = 11;
    #[cfg(target_os = "macos")]
    const EAGAIN: i32 = 35;
    /// `EACCES`：没权限（Linux 上多半是当前用户不在 dialout 组）
    const EACCES: i32 = 13;
    /// `EBUSY`：被占（我们自己开的时候设了 `TIOCEXCL`，第二次打开就撞它）
    const EBUSY: i32 = 16;
    /// `ENODEV`：设备没了（USB 转串口被拔）
    const ENODEV: i32 = 19;

    #[repr(C)]
    struct PollFd {
        fd: i32,
        events: i16,
        revents: i16,
    }

    /// `struct termios` —— **手写布局，逐字段对应平台的 `<termios.h>`**。
    ///
    /// 📌 错一个字段/一个位宽，`TCGETS` 拿回来的东西就是乱的，而且**不会报错**，
    /// 表现是「能打开、收到乱码」或者「参数根本没生效」。
    #[cfg(target_os = "linux")]
    #[repr(C)]
    #[derive(Clone, Copy)]
    struct Termios {
        c_iflag: u32,
        c_oflag: u32,
        c_cflag: u32,
        c_lflag: u32,
        c_line: u8,
        c_cc: [u8; 32],
        c_ispeed: u32,
        c_ospeed: u32,
    }

    #[cfg(target_os = "macos")]
    #[repr(C)]
    #[derive(Clone, Copy)]
    struct Termios {
        c_iflag: u64,
        c_oflag: u64,
        c_cflag: u64,
        c_lflag: u64,
        c_cc: [u8; 20],
        c_ispeed: u64,
        c_ospeed: u64,
    }

    /// `struct termios2`（只有 Linux 有）：**能设任意波特率**（`BOTHER`）。
    /// `c_cc` 在这里是 **19** 个（不是 32）—— 这是内核真正的内部结构。
    #[cfg(target_os = "linux")]
    #[repr(C)]
    #[derive(Clone, Copy)]
    struct Termios2 {
        c_iflag: u32,
        c_oflag: u32,
        c_cflag: u32,
        c_lflag: u32,
        c_line: u8,
        c_cc: [u8; 19],
        c_ispeed: u32,
        c_ospeed: u32,
    }

    // 🔴 **编译期断言**：手写布局最容易错的地方（字段顺序 / 位宽 / 对齐）
    // 在这里全部钉死 —— 一旦有人改错结构体，**交叉编译就过不去**，
    // 不用等真机上表现成「能打开、收到乱码」。
    //
    // 参照值（C 侧 `sizeof(struct termios)`）：
    //   Linux x86_64 / aarch64 = **60**；macOS (Darwin 64 位) = **72**；
    //   Linux `struct termios2` = **44**（`c_cc` 只有 19 个）。
    #[cfg(target_os = "linux")]
    const _: () = {
        use std::mem::{offset_of, size_of};
        assert!(size_of::<Termios>() == 60, "struct termios 布局与 Linux 头文件不符");
        assert!(offset_of!(Termios, c_iflag) == 0);
        assert!(offset_of!(Termios, c_oflag) == 4);
        assert!(offset_of!(Termios, c_cflag) == 8);
        assert!(offset_of!(Termios, c_lflag) == 12);
        assert!(offset_of!(Termios, c_line) == 16);
        assert!(offset_of!(Termios, c_cc) == 17);
        assert!(offset_of!(Termios, c_ispeed) == 52);
        assert!(offset_of!(Termios, c_ospeed) == 56);

        assert!(size_of::<Termios2>() == 44, "struct termios2 布局与内核头文件不符");
        assert!(offset_of!(Termios2, c_cc) == 17);
        assert!(offset_of!(Termios2, c_ispeed) == 36);
        assert!(offset_of!(Termios2, c_ospeed) == 40);

        assert!(size_of::<PollFd>() == 8);
    };

    #[cfg(target_os = "macos")]
    const _: () = {
        use std::mem::{offset_of, size_of};
        assert!(size_of::<Termios>() == 72, "struct termios 布局与 Darwin <termios.h> 不符");
        assert!(offset_of!(Termios, c_iflag) == 0);
        assert!(offset_of!(Termios, c_oflag) == 8);
        assert!(offset_of!(Termios, c_cflag) == 16);
        assert!(offset_of!(Termios, c_lflag) == 24);
        assert!(offset_of!(Termios, c_cc) == 32);
        assert!(offset_of!(Termios, c_ispeed) == 56);
        assert!(offset_of!(Termios, c_ospeed) == 64);

        assert!(size_of::<PollFd>() == 8);
    };

    extern "C" {
        fn open(路径: *const u8, 标志: i32, 模式: u32) -> i32;
        fn close(fd: i32) -> i32;
        fn read(fd: i32, 缓冲: *mut u8, 要读: usize) -> isize;
        fn write(fd: i32, 数据: *const u8, 要写: usize) -> isize;
        fn poll(fds: *mut PollFd, 个数: Nfds, 等毫秒: i32) -> i32;
        fn ioctl(fd: i32, 请求: u64, ...) -> i32;
        fn tcflush(fd: i32, 队列: i32) -> i32;
    }

    fn 最后错误() -> i32 {
        std::io::Error::last_os_error().raw_os_error().unwrap_or(0)
    }

    /// 波特率 → `Bxxx` 码。
    ///
    /// 🔴 **Linux 的码不是数字本身**（`B9600 = 13`、`B115200 = 0x1002`），
    /// 而 **macOS 就是数字本身**（`B9600 = 9600`）。抄错的后果是设了个
    /// 完全不相干的速率，表现为「收到乱码」—— 比报错难查得多。
    #[cfg(target_os = "linux")]
    fn 波特率码(率: u32) -> Option<u32> {
        Some(match 率 {
            50 => 1,
            75 => 2,
            110 => 3,
            134 => 4,
            150 => 5,
            200 => 6,
            300 => 7,
            600 => 8,
            1200 => 9,
            1800 => 10,
            2400 => 11,
            4800 => 12,
            9600 => 13,
            19200 => 14,
            38400 => 15,
            57600 => 0o10_001,
            115200 => 0o10_002,
            230400 => 0o10_003,
            460800 => 0o10_004,
            500000 => 0o10_005,
            576000 => 0o10_006,
            921600 => 0o10_007,
            1_000_000 => 0o10_010,
            1_152_000 => 0o10_011,
            1_500_000 => 0o10_012,
            2_000_000 => 0o10_013,
            2_500_000 => 0o10_014,
            3_000_000 => 0o10_015,
            3_500_000 => 0o10_016,
            4_000_000 => 0o10_017,
            _ => return None,
        })
    }

    #[cfg(target_os = "macos")]
    fn 波特率码(率: u32) -> Option<u32> {
        Some(match 率 {
            50 | 75 | 110 | 134 | 150 | 200 | 300 | 600 | 1200 | 1800 | 2400 | 4800 | 7200
            | 9600 | 14400 | 19200 | 28800 | 38400 | 57600 | 76800 | 115200 | 230400 => 率,
            _ => return None,
        })
    }

    /// 打开失败 → **人话**（与 Win32 侧同一个教训：**先看路径在不在**，别猜错误码）。
    fn 打开错误(口: &str, 码: i32) -> JishiError {
        if !std::path::Path::new(口).exists() {
            return err_with_hint(
                "运行期错误",
                format!("没有这个串口「{口}」（路径不存在）"),
                if cfg!(target_os = "linux") {
                    "先 `打印(串口.列出())` 看看有哪些口；Linux 上通常是 /dev/ttyUSB0（CH340 之类）\
                     或 /dev/ttyACM0（板子自带 USB）；插上设备要等驱动认出来（/dev 下出现节点）"
                } else {
                    "先 `打印(串口.列出())` 看看有哪些口；macOS 上要写 /dev/cu.*（**不要 /dev/tty.***）"
                }
                .into(),
            );
        }
        match 码 {
            EACCES => err_with_hint(
                "运行期错误",
                format!("没有权限打开串口「{口}」"),
                if cfg!(target_os = "linux") {
                    "把当前用户加进串口组，然后**重新登录**（组权限要重登才生效）：\
                     ① Debian/Ubuntu：`sudo usermod -aG dialout $USER`；\
                     ② Arch：`sudo usermod -aG uucp $USER`；\
                     ③ 临时验证可以用 `sudo`（但别在正式脚本里这么干）"
                        .to_string()
                } else {
                    format!("检查 /dev 节点的属主与权限（`ls -l {口}`），也别用 sudo 跑过一次紧接着普通跑")
                },
            ),
            EBUSY => err_with_hint(
                "运行期错误",
                format!("串口「{口}」打不开 —— 被别的程序占着（也可能是上一次没 `关闭()`）"),
                "串口是**一次只能一个程序打开**的（这里打开时会设 TIOCEXCL 独占）：\
                 关掉串口助手 / 上次跑的程序，再试"
                    .into(),
            ),
            ENODEV => err_with_hint(
                "运行期错误",
                format!("串口「{口}」已经不在了（设备被拔掉或驱动卸载）"),
                "USB 转串口重新插一下，再 `打印(串口.列出())` 确认口名（有时候会换号）".into(),
            ),
            _ => {
                let 系统 = std::io::Error::from_raw_os_error(码);
                err_with_hint(
                    "运行期错误",
                    format!("打开串口「{口}」失败：{系统}（系统错误码 {码}）"),
                    "Linux/macOS 上串口是**设备文件**，路径要写全（/dev/ttyUSB0、/dev/cu.usbserial-1410）".into(),
                )
            }
        }
    }

    // ---- 打开 -------------------------------------------------------------

    pub(super) fn 打开(
        口: &str,
        波特率: u32,
        数据位: u8,
        停止位: u8,
        校验: u8,
    ) -> Result<Val, JishiError> {
        let 净 = 口.trim();
        let 路径 = std::ffi::CString::new(净)
            .map_err(|_| err("值错误", format!("口名里不能有 0 字节：「{口}」")))?;
        // ⚠️ `O_NONBLOCK` 是**必须的**：不设的话 `open` 会等 DCD 载波，
        //    macOS 的 /dev/tty.* 上就是「插着板子也打不开、一拔就返回」。
        //    读的超时不用 termios 的 VMIN/VTIME，改用 `poll` 自己控（更好预测）。
        let fd = unsafe {
            open(
                路径.as_ptr() as *const u8,
                O_RDWR | O_NOCTTY | O_NONBLOCK | O_CLOEXEC,
                0,
            )
        };
        if fd < 0 {
            return Err(打开错误(净, 最后错误()));
        }
        // 独占标记：第二次打开撞 `EBUSY` —— 与 Windows 的「共享模式 0」对齐。
        // 失败不要紧（有些驱动不认），所以不检查返回值。
        unsafe {
            ioctl(fd, TIOCEXCL);
        }
        if let Err(e) = 设参数(fd, 波特率, 数据位, 停止位, 校验) {
            unsafe {
                close(fd);
            }
            return Err(e);
        }
        unsafe {
            tcflush(fd, TCIOFLUSH);            // 丢掉驱动里可能残留的旧字节
        }
        Ok(crate::native_obj(
            类型名,
            super::方法们,
            std::rc::Rc::new(std::cell::RefCell::new(super::串口状态 {
                句柄: fd as isize,
                已关: false,
                缓冲: Vec::new(),
            })),
        ))
    }

    // ---- 设参数 -----------------------------------------------------------

    /// 数据位 / 停止位 / 校验 → 校验位组合（两平台位值不同，**翻译各写一份**）。
    #[cfg(target_os = "linux")]
    fn 校验位(校验: u8) -> Result<u32, JishiError> {
        Ok(match 校验 {
            0 => 0,
            1 => 位::PARENB | 位::PARODD,          // 奇
            2 => 位::PARENB,                        // 偶
            3 => 位::PARENB | 位::PARODD | 位::CMSPAR,  // 标记
            4 => 位::PARENB | 位::CMSPAR,           // 空格
            _ => 0,
        })
    }

    #[cfg(target_os = "macos")]
    fn 校验位(校验: u8) -> Result<u64, JishiError> {
        Ok(match 校验 {
            0 => 0,
            1 => 位::PARENB | 位::PARODD,          // 奇
            2 => 位::PARENB,                        // 偶
            3 => 位::PARENB | 位::PARODD,           // 标记（Darwin 就靠 PARODD 表达）
            4 => {
                return Err(err_with_hint(
                    "值错误",
                    "macOS 的 termios 没有「空格」校验".to_string(),
                    "改用「无」/「奇」/「偶」/「标记」；或者换 Windows / Linux".into(),
                ))
            }
            _ => 0,
        })
    }

    /// 通用校验：停止位 1.5 位 POSIX 没有（那是 Windows 的 `ONE5STOPBITS`）。
    fn 查停止位(停止位: u8) -> Result<(), JishiError> {
        if 停止位 == 3 {
            return Err(err_with_hint(
                "值错误",
                "Linux / macOS 的 termios 没有「1.5 位」停止位（那是 Windows 专有的）",
                "改用 1 或 2；1.5 位只在极老的设备上才需要".into(),
            ));
        }
        Ok(())
    }

    /// 数据位 → `CSx`（🔴 两平台的位值不同：Linux `CS8=0o60`，macOS `CS8=0x300`）。
    #[cfg(target_os = "linux")]
    fn 数据位标志(数据位: u8) -> u32 {
        match 数据位 {
            5 => 位::CS5,
            6 => 位::CS6,
            7 => 位::CS7,
            _ => 位::CS8,
        }
    }

    #[cfg(target_os = "macos")]
    fn 数据位标志(数据位: u8) -> u64 {
        match 数据位 {
            5 => 位::CS5,
            6 => 位::CS6,
            7 => 位::CS7,
            _ => 位::CS8,
        }
    }

    #[cfg(target_os = "linux")]
    fn 设参数(fd: Fd, 率: u32, 数据位: u8, 停止位: u8, 校验: u8) -> Result<(), JishiError> {
        查停止位(停止位)?;
        let mut t: Termios = unsafe { std::mem::zeroed() };
        if unsafe { ioctl(fd, TCGETS, &mut t as *mut Termios) } != 0 {
            let 码 = 最后错误();
            return Err(err_with_hint(
                "运行期错误",
                format!("读串口当前设置失败（系统错误码 {码}）"),
                "这个设备可能根本不是串口 —— 拿普通文件 / 管道 / 终端来打开就会出现这个".into(),
            ));
        }
        // **原始模式**：关掉所有字符加工（回显 / 行缓冲 / 换行翻译 / 信号键）。
        // 不关的话设备发来的 0x03 会被当成 Ctrl-C、0x0D 会被翻成 0x0A。
        t.c_iflag = 0;
        t.c_oflag = 0;
        t.c_lflag = 0;
        t.c_cflag &= !(位::CSIZE | 位::CSTOPB | 位::PARENB | 位::PARODD | 位::CMSPAR | 位::CRTSCTS);
        t.c_cflag |= 位::CLOCAL | 位::CREAD;       // 忽略调制解调器控制线
        t.c_cflag |= 数据位标志(数据位);
        if 停止位 == 2 {
            t.c_cflag |= 位::CSTOPB;
        }
        t.c_cflag |= 校验位(校验)?;

        match 波特率码(率) {
            Some(码) => {
                t.c_cflag = (t.c_cflag & !位::CBAUD) | 码;
                if unsafe { ioctl(fd, TCSETS, &t as *const Termios) } != 0 {
                    let 码 = 最后错误();
                    return Err(err("运行期错误", format!(
                        "串口不接受这套参数（波特率 {率} / 数据位 {数据位} / 停止位 {停止位}）：系统错误码 {码}")));
                }
                Ok(())
            }
            None => {
                // 非标准速率（74880 / 31250 这类）走 `termios2` + `BOTHER`。
                // 这条路比标准表脆一点，所以失败时**明确说**是「任意波特率」这一步不支持。
                let mut t2 = Termios2 {
                    c_iflag: t.c_iflag,
                    c_oflag: t.c_oflag,
                    c_cflag: (t.c_cflag & !位::CBAUD) | 位::BOTHER,
                    c_lflag: t.c_lflag,
                    c_line: 0,
                    c_cc: [0u8; 19],
                    c_ispeed: 率,
                    c_ospeed: 率,
                };
                t2.c_cc.copy_from_slice(&t.c_cc[..19]);
                if unsafe { ioctl(fd, TCSETS2, &t2 as *const Termios2) } != 0 {
                    let 码 = 最后错误();
                    return Err(err_with_hint(
                        "运行期错误",
                        format!("这个串口（或驱动）不支持 {率} 这个波特率：系统错误码 {码}"),
                        "标准速率（9600 / 19200 / 38400 / 57600 / 115200 / 230400 / 460800 / 921600）\
                         走的是老路，最稳；非标准速率要驱动也支持 `BOTHER` —— CH340 之类的常见 \
                         USB 转串口多数支持，板载 UART 不一定"
                            .into(),
                    ));
                }
                Ok(())
            }
        }
    }

    #[cfg(target_os = "macos")]
    fn 设参数(fd: Fd, 率: u32, 数据位: u8, 停止位: u8, 校验: u8) -> Result<(), JishiError> {
        查停止位(停止位)?;
        let mut t: Termios = unsafe { std::mem::zeroed() };
        if unsafe { ioctl(fd, TIOCGETA, &mut t as *mut Termios) } != 0 {
            let 码 = 最后错误();
            return Err(err_with_hint(
                "运行期错误",
                format!("读串口当前设置失败（系统错误码 {码}）"),
                "这个设备可能根本不是串口 —— 拿普通文件 / 管道 / 终端来打开就会出现这个".into(),
            ));
        }
        t.c_iflag = 0;
        t.c_oflag = 0;
        t.c_lflag = 0;
        t.c_cflag &= !(位::CSIZE | 位::CSTOPB | 位::PARENB | 位::PARODD
            | 位::CCTS_OFLOW | 位::CRTS_IFLOW);
        t.c_cflag |= 位::CLOCAL | 位::CREAD;
        t.c_cflag |= 数据位标志(数据位);
        if 停止位 == 2 {
            t.c_cflag |= 位::CSTOPB;
        }
        t.c_cflag |= 校验位(校验)?;

        // 🔴 macOS 的速率在**两个独立字段**里（值就是数字本身），不在 `c_cflag`。
        let 码 = match 波特率码(率) {
            Some(码) => 码,
            None => {
                return Err(err_with_hint(
                    "值错误",
                    format!("macOS 这一版只认标准波特率，{率} 不在里面"),
                    "可用的：50 / 75 / 110 / 134 / 150 / 200 / 300 / 600 / 1200 / 1800 / 2400 / \
                     4800 / 7200 / 9600 / 14400 / 19200 / 28800 / 38400 / 57600 / 76800 / \
                     115200 / 230400。要别的速率得上 `IOSSIOSPEED`（本版没做，见路线图）"
                        .into(),
                ))
            }
        };
        t.c_ispeed = 码 as u64;
        t.c_ospeed = 码 as u64;

        if unsafe { ioctl(fd, TIOCSETA, &t as *const Termios) } != 0 {
            let 码 = 最后错误();
            return Err(err("运行期错误", format!(
                "串口不接受这套参数（波特率 {率} / 数据位 {数据位} / 停止位 {停止位}）：系统错误码 {码}")));
        }
        Ok(())
    }

    // ---- 读 / 写 ----------------------------------------------------------

    /// 等 fd 就绪。返回 `false` = 等超时了（**不是错误**）。
    fn 等就绪(fd: Fd, 事件: i16, 等毫秒: u32) -> Result<bool, JishiError> {
        let mut p = PollFd {
            fd,
            events: 事件,
            revents: 0,
        };
        loop {
            let rc = unsafe { poll(&mut p, 1, 等毫秒.min(i32::MAX as u32) as i32) };
            if rc < 0 {
                let 码 = 最后错误();
                if 码 == EINTR {
                    continue;
                }
                return Err(err("运行期错误", format!("等串口数据失败（系统错误码 {码}）")));
            }
            if rc == 0 {
                return Ok(false);
            }
            if p.revents & POLLNVAL != 0 {
                return Err(err("运行期错误", "串口句柄已经失效（可能被关了）"));
            }
            if p.revents & (POLLERR | POLLHUP) != 0 && p.revents & 事件 == 0 {
                return Err(err_with_hint(
                    "运行期错误",
                    "串口出错了（设备断开或线被拔）",
                    "USB 转串口重新插一下；有的驱动拔线时只会给 POLLHUP，读不到具体原因".into(),
                ));
            }
            return Ok(true);
        }
    }

    pub(super) fn 读(fd: Fd, 等毫秒: u32) -> Result<Vec<u8>, JishiError> {
        if 等毫秒 > 0 && !等就绪(fd, POLLIN, 等毫秒)? {
            return Ok(Vec::new());                 // 超时 = 这一拍没数据，不是错误
        }
        let mut 片 = vec![0u8; 8192];
        let n = unsafe { read(fd, 片.as_mut_ptr(), 片.len()) };
        if n < 0 {
            let 码 = 最后错误();
            // `EINTR`（被信号打断）/ `EAGAIN`（非阻塞下没数据）都不算错
            if 码 == EINTR || 码 == EAGAIN {
                return Ok(Vec::new());
            }
            return Err(err_with_hint(
                "运行期错误",
                format!("从串口读失败（系统错误码 {码}）"),
                "最常见的两种：① 设备被拔了（EBADF/EIO）；② 是 pty 的对端关掉了".into(),
            ));
        }
        片.truncate(n as usize);
        Ok(片)
    }

    pub(super) fn 写(fd: Fd, 数据: &[u8]) -> Result<(), JishiError> {
        if 数据.is_empty() {
            return Ok(());
        }
        let 截止 = std::time::Instant::now() + std::time::Duration::from_secs(5);
        let mut 已写 = 0usize;
        while 已写 < 数据.len() {
            let n = unsafe { write(fd, 数据[已写..].as_ptr(), 数据.len() - 已写) };
            if n > 0 {
                已写 += n as usize;
                continue;
            }
            let 码 = 最后错误();
            if 码 == EINTR {
                continue;
            }
            if 码 == EAGAIN {
                let 剩 = 截止.saturating_duration_since(std::time::Instant::now());
                if 剩.is_zero() {
                    return Err(err_with_hint(
                        "运行期错误",
                        "往串口写等太久了（5 秒还没写出去）",
                        "对方可能没在收（硬流控没接、线接反了、或者对端程序卡住）".into(),
                    ));
                }
                // 等一小片可写再重试（不给满 5 秒，好让循环能再看一次截止时间）
                等就绪(fd, POLLOUT, 剩.as_millis().min(500) as u32)?;
                continue;
            }
            return Err(err("运行期错误", format!("往串口写失败（系统错误码 {码}）")));
        }
        Ok(())
    }

    pub(super) fn 关闭(fd: Fd) {
        unsafe {
            close(fd);
        }
    }

    // ---- 列出 -------------------------------------------------------------

    /// Linux：扫 `/sys/class/tty`，**只留挂了驱动的**。
    ///
    /// `/sys/class/tty` 里头还有 `tty0`…`tty63`（虚拟控制台）、`ttyprintk` 之类，
    /// 它们全都不是串口 —— 判据是 `device/driver` 这个符号链接在不在。
    /// `描述` 给**驱动名**（`ch341-uart` / `ftdi_sio` / `pl2303` / `serial8250`），
    /// 这是真拿得到的信息，而且「两个 CH340 分不清」时正好用它。
    #[cfg(target_os = "linux")]
    fn 口们() -> Vec<(String, String, String)> {
        let mut 出: Vec<(String, String, String)> = Vec::new();
        let 目录 = match std::fs::read_dir("/sys/class/tty") {
            Ok(d) => d,
            Err(_) => return 出,                    // 没有 sysfs（极端环境）→ 空列表，不报错
        };
        for 项 in 目录.flatten() {
            let 名 = 项.file_name().to_string_lossy().to_string();
            let 设备 = 项.path().join("device");
            let 驱动 = 设备.join("driver");
            if !驱动.exists() {
                continue;                           // 没驱动 = 不是真串口（虚拟终端在这里被滤掉）
            }
            // 🔴 `serial8250` 驱动会给**所有**传统串口（ttyS0…ttyS31）都挂上
            // `device/driver`，哪怕机器上一个物理口都没有。`type` 文件是驱动
            // 自己报的端口类型，**0 = PORT_UNKNOWN = 没硬件**，靠它把幽灵口滤掉。
            // （没有 `type` 文件的驱动＝USB 转串口那类，一律保留。）
            let 类型 = std::fs::read_to_string(项.path().join("type")).ok();
            if let Some(文) = 类型 {
                if 文.trim() == "0" {
                    continue;
                }
            }
            let 驱动名 = std::fs::read_link(&驱动)
                .ok()
                .and_then(|p| p.file_name().map(|s| s.to_string_lossy().to_string()))
                .unwrap_or_default();
            let 真实 = std::fs::canonicalize(&设备)
                .map(|p| p.to_string_lossy().to_string())
                .unwrap_or_default();
            出.push((format!("/dev/{名}"), 驱动名, 真实));
        }
        出.sort();
        出
    }

    /// macOS：扫 `/dev` 里 `cu.*` 的条目。
    ///
    /// 🔴 **只要 `cu.*`（callout），不要 `tty.*`**：`tty.*` 打开后会等 DCD 载波，
    /// 板子没上电 / 拔线时 `open` 和 `read` 都会**挂住**，用户看到的是「程序卡死」。
    /// `cu.*` 不等载波，插拔时行为可预测。
    ///
    /// `描述` 给空文本 —— 友好名（「USB-Serial CH340」）要 IOKit 的属性查询，
    /// **如实**表示这个平台这一版拿不到，而不是把口名抄一遍冒充。
    #[cfg(target_os = "macos")]
    fn 口们() -> Vec<(String, String, String)> {
        let mut 出: Vec<(String, String, String)> = Vec::new();
        if let Ok(目录) = std::fs::read_dir("/dev") {
            for 项 in 目录.flatten() {
                let 名 = 项.file_name().to_string_lossy().to_string();
                if !名.starts_with("cu.") {
                    continue;
                }
                let 路 = format!("/dev/{名}");
                出.push((路.clone(), String::new(), 路));
            }
        }
        出.sort();
        出
    }

    pub(super) fn 列出() -> Result<Val, JishiError> {
        let 项们: Vec<Val> = 口们()
            .into_iter()
            .map(|(口, 描述, 设备)| {
                crate::dict_new(vec![
                    (Val::Str("口".into()), Val::Str(口.into())),
                    (Val::Str("描述".into()), Val::Str(描述.into())),
                    (Val::Str("设备".into()), Val::Str(设备.into())),
                ])
            })
            .collect();
        Ok(Val::List(std::rc::Rc::new(std::cell::RefCell::new(项们))))
    }

    #[allow(dead_code)]
    pub(super) fn 说明() -> &'static str {
        if cfg!(target_os = "linux") {
            "Linux 串口：open + ioctl(TCGETS/TCSETS) + poll（非标准速率走 termios2/BOTHER）"
        } else {
            "macOS 串口：open(/dev/cu.*) + ioctl(TIOCGETA/TIOCSETA) + poll"
        }
    }
}
