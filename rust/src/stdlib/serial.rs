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
//! | 平台 | 状态 |
//! |---|---|
//! | Windows | ✅ 裸 Win32（`CreateFileW` + `SetCommState` + `SetCommTimeouts`），**零第三方依赖** |
//! | Linux / macOS | ❌ **这一版没做**，调用报一句明确的中文错 |
//! | wasm（浏览器/页面） | ❌ 物理上不存在，报平台错 |
//!
//! 📌 **Linux/mac 故意留白，不是忘了**：termios 的 `struct termios` 布局在
//! Linux 与 macOS 上**不一样**（字段顺序、`c_cflag` 里的位定义都不同），
//! 而本机只有 Windows —— 写一份**没法验证**的 FFI 属于「假装做了」，
//! 真出问题用户只看到「打开失败」四个字。等有 Linux 机器能实测再说。

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
    /// Win32 的 `HANDLE`（`isize`）；非 Windows 上恒为 -1。
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

/// 从 `Val` 里认出句柄（不是句柄就报一句**可操作**的错）。
fn 句柄(v: &Val, 名: &str) -> Result<std::rc::Rc<crate::NativeObj>, JishiError> {
    match v {
        Val::Native(o) if o.type_name == 类型名 => Ok(o.clone()),
        _ => Err(err(
            "类型错误",
            format!(
                "「{名}」要传 `串口.打开` 拿到的句柄（现在是「{}」）",
                crate::type_label(v)
            ),
        )),
    }
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

#[cfg(not(windows))]
fn 设备路径(_口: &str) -> Vec<u16> {
    Vec::new()
}

// ---------------------------------------------------------------------------
// 串口.列出
// ---------------------------------------------------------------------------

fn 列出() -> Result<Val, JishiError> {
    #[cfg(windows)]
    {
        return win32::列出();
    }
    #[cfg(not(windows))]
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
            match n {
                1 => 1,                          // ONESTOPBIT
                2 => 2,                          // TWOSTOPBITS
                3 => 0,                          // ONE5STOPBITS（1.5 位，DCB 里是 0）
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
    #[cfg(not(windows))]
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
    #[cfg(not(windows))]
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
    #[cfg(not(windows))]
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

fn 平台未实现(操作: &str) -> JishiError {
    err_with_hint(
        "运行期错误",
        format!("`串口.{操作}` 在这个平台上还没实现（M232 只做了 Windows）"),
        "Linux / macOS 的 termios 那一套见 `docs/路线图-0.3.x.md` M232 —— \
         这一版故意留白：本机只有 Windows，写一份验证不了的 FFI 等于假装做了"
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
            dcb.stop_bits = 停止位;
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
