//! 标准库 `日期` 与 `时间`（Python 侧 `日期.py` 13 个 + `时间.py` 9 个）。
//!
//! 三块要自己写的东西：
//!
//! 1. **本地时区**。`std::time` 只给 UTC，没有「本地时间」的概念。
//!    这里直接绑系统 API：Windows 用 `GetLocalTime` /
//!    `FileTimeToLocalFileTime`，Unix 用 `localtime_r`。**是 FFI，
//!    不是第三方 crate**——`Cargo.toml` 的 `[dependencies]` 仍然是空的。
//! 2. **`strftime` 子集**。`%U`/`%W` 的周号照 CPython 的公式
//!    （新年第一个周日/周一之前的日期算第 0 周），`%c`/`%x` 的空白填充
//!    也与 CPython 一致（`%c` 的日期是**空格填充**：`Sep  5`）。
//! 3. **公历换算**。日期加减走 Howard Hinnant 的
//!    `days_from_civil` / `civil_from_days`（纯整数，不碰浮点与时区）。
//!
//! `日期.解析` 的返回值是个 **datetime**（Python 侧就是 `datetime`），
//! 所以 `Val` 里加了 `Date` 这一支——显示成 `2026-09-01 00:00:00`、
//! `类型()` 给 `datetime`，与另外两个宿主一致。

use std::collections::HashMap;
use std::rc::Rc;
use std::sync::{Mutex, OnceLock};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use super::{as_intish, as_text, need, need_range, table, R};
use crate::{err, JishiError, Val, VM};

// ---------------------------------------------------------------------------
// 本地时间
// ---------------------------------------------------------------------------

/// 一个本地日期时间（`weekday` 按 Python 的约定：0 = 周一）。
#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) struct Dt {
    pub(crate) year: i32,
    pub(crate) month: u32,
    pub(crate) day: u32,
    pub(crate) hour: u32,
    pub(crate) minute: u32,
    pub(crate) second: u32,
    pub(crate) weekday: u32,
}

impl Dt {
    /// 与 Python 的 `str(datetime)` 一致：`2026-09-01 00:00:00`。
    pub(crate) fn to_display(&self) -> String {
        format!(
            "{:04}-{:02}-{:02} {:02}:{:02}:{:02}",
            self.year, self.month, self.day, self.hour, self.minute, self.second
        )
    }

    /// 一年中的第几天（**从 0 起**，算 `%U`/`%W` 用；`%j` 要 +1）。
    fn yday(&self) -> i64 {
        days_from_civil(self.year, self.month, self.day) - days_from_civil(self.year, 1, 1)
    }
}

#[cfg(windows)]
mod plat {
    #[repr(C)]
    #[derive(Clone, Copy, Default)]
    pub struct SystemTime {
        pub year: u16,
        pub month: u16,
        pub dow: u16,
        pub day: u16,
        pub hour: u16,
        pub minute: u16,
        pub second: u16,
        pub ms: u16,
    }

    #[repr(C)]
    #[derive(Clone, Copy, Default)]
    pub struct FileTime {
        pub low: u32,
        pub high: u32,
    }

    #[link(name = "kernel32")]
    extern "system" {
        pub fn GetLocalTime(t: *mut SystemTime) -> ();
        pub fn FileTimeToLocalFileTime(ft: *const FileTime, out: *mut FileTime) -> i32;
        pub fn FileTimeToSystemTime(ft: *const FileTime, st: *mut SystemTime) -> i32;
    }

    /// 当前本地时间。
    pub fn local_now() -> (i32, u32, u32, u32, u32, u32, u32) {
        let mut st = SystemTime::default();
        unsafe { GetLocalTime(&mut st) };
        (
            st.year as i32,
            st.month as u32,
            st.day as u32,
            st.hour as u32,
            st.minute as u32,
            st.second as u32,
            st.dow as u32,
        )
    }

    /// Unix 时间戳 → 本地时间。
    pub fn local_from_unix(secs: f64) -> Option<(i32, u32, u32, u32, u32, u32, u32)> {
        // FILETIME 是「1601-01-01 起的 100 纳秒数」
        let ticks = (secs * 10_000_000.0) as i64 + 116_444_736_000_000_000;
        if ticks < 0 {
            return None;
        }
        let utc = FileTime {
            low: ticks as u32,
            high: ((ticks as u64) >> 32) as u32,
        };
        let mut local = FileTime::default();
        let mut st = SystemTime::default();
        let ok = unsafe {
            FileTimeToLocalFileTime(&utc, &mut local) != 0
                && FileTimeToSystemTime(&local, &mut st) != 0
        };
        if !ok {
            return None;
        }
        Some((
            st.year as i32,
            st.month as u32,
            st.day as u32,
            st.hour as u32,
            st.minute as u32,
            st.second as u32,
            st.dow as u32,
        ))
    }
}

#[cfg(unix)]
mod plat {
    #[repr(C)]
    struct Tm {
        sec: i32,
        min: i32,
        hour: i32,
        mday: i32,
        mon: i32,
        year: i32,
        wday: i32,
        yday: i32,
        isdst: i32,
        gmtoff: i64,
        zone: *const i8,
    }

    extern "C" {
        fn localtime_r(t: *const i64, out: *mut Tm) -> *mut Tm;
    }

    fn convert(t: &Tm) -> (i32, u32, u32, u32, u32, u32, u32) {
        // libc 的 `tm_wday` 是 0=周日，Python 的 `weekday()` 是 0=周一
        (
            t.year + 1900,
            (t.mon + 1) as u32,
            t.mday as u32,
            t.hour as u32,
            t.min as u32,
            t.sec as u32,
            ((t.wday + 6) % 7) as u32,
        )
    }

    pub fn local_now() -> (i32, u32, u32, u32, u32, u32, u32) {
        let secs = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0);
        local_from_unix(secs as f64).unwrap_or((1970, 1, 1, 0, 0, 0, 3))
    }

    pub fn local_from_unix(secs: f64) -> Option<(i32, u32, u32, u32, u32, u32, u32)> {
        let t = secs as i64;
        let mut tm = Tm {
            sec: 0,
            min: 0,
            hour: 0,
            mday: 0,
            mon: 0,
            year: 0,
            wday: 0,
            yday: 0,
            isdst: 0,
            gmtoff: 0,
            zone: std::ptr::null(),
        };
        let ok = unsafe { !localtime_r(&t, &mut tm).is_null() };
        if ok {
            Some(convert(&tm))
        } else {
            None
        }
    }
}

#[cfg(not(any(windows, unix)))]
mod plat {
    pub fn local_now() -> (i32, u32, u32, u32, u32, u32, u32) {
        (1970, 1, 1, 0, 0, 0, 3)
    }
    pub fn local_from_unix(_secs: f64) -> Option<(i32, u32, u32, u32, u32, u32, u32)> {
        None
    }
}

fn make_dt(t: (i32, u32, u32, u32, u32, u32, u32)) -> Dt {
    Dt {
        year: t.0,
        month: t.1,
        day: t.2,
        hour: t.3,
        minute: t.4,
        second: t.5,
        weekday: t.6,
    }
}

pub(crate) fn local_now() -> Dt {
    make_dt(plat::local_now())
}

pub(crate) fn local_from_unix(secs: f64) -> Dt {
    match plat::local_from_unix(secs) {
        Some(t) => make_dt(t),
        // 平台不支持本地时区时退回 UTC（`not(any(windows, unix))` 才会走到）
        None => utc_from_unix(secs),
    }
}

/// Unix 时间戳 → UTC 公历（纯整数运算，不用 FFI）。
fn utc_from_unix(secs: f64) -> Dt {
    let days = (secs / 86400.0).floor() as i64;
    let rem = secs - (days as f64) * 86400.0;
    let (y, m, d) = civil_from_days(days);
    Dt {
        year: y,
        month: m,
        day: d,
        hour: (rem / 3600.0).floor() as u32 % 24,
        minute: (rem / 60.0).floor() as u32 % 60,
        second: rem as u32 % 60,
        weekday: weekday_of(days),
    }
}

// ---------------------------------------------------------------------------
// 公历换算（Howard Hinnant 的算法，纯整数、与时区无关）
// ---------------------------------------------------------------------------

fn days_from_civil(y: i32, m: u32, d: u32) -> i64 {
    let y = if m <= 2 { y - 1 } else { y } as i64;
    let m = m as i64;
    let d = d as i64;
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = if m > 2 { m - 3 } else { m + 9 };
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

fn civil_from_days(z: i64) -> (i32, u32, u32) {
    let z = z + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    ((if m <= 2 { y + 1 } else { y }) as i32, m, d)
}

/// 1970-01-01 是周四 → 0=周一时它是 3。
fn weekday_of(days: i64) -> u32 {
    (days + 3).rem_euclid(7) as u32
}

fn days_in_year(y: i32) -> i64 {
    if (y % 4 == 0 && y % 100 != 0) || y % 400 == 0 {
        366
    } else {
        365
    }
}

// ---------------------------------------------------------------------------
// strftime 子集
// ---------------------------------------------------------------------------

const WD_CN: [&str; 7] = ["一", "二", "三", "四", "五", "六", "日"];
const WD_ABBR: [&str; 7] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const WD_FULL: [&str; 7] = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
];
const MO_ABBR: [&str; 12] = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];
const MO_FULL: [&str; 12] = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
];

fn pad(n: i64, w: usize, no_pad: bool) -> String {
    if no_pad {
        n.to_string()
    } else {
        format!("{:0width$}", n, width = w)
    }
}

/// `strftime` 子集：`%Y %y %m %d %H %I %M %S %j %w %u %a %A %b %B %p %U %W
/// %c %x %X %%` 与「去零」修饰 `%-d`。认不出的指令**原样返回**——
/// 宁可不格式化，也不给错。
pub(crate) fn strftime(dt: &Dt, fmt: &str) -> String {
    let yday = dt.yday();
    let mut out = String::new();
    let chars: Vec<char> = fmt.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        if chars[i] != '%' || i + 1 >= chars.len() {
            out.push(chars[i]);
            i += 1;
            continue;
        }
        let mut j = i + 1;
        let mut no_pad = false;
        if matches!(chars[j], '-' | '_' | '0') {
            no_pad = chars[j] == '-';
            j += 1;
            if j >= chars.len() {
                out.extend(&chars[i..]);
                break;
            }
        }
        let c = chars[j];
        let wd = dt.weekday as usize;
        let piece = match c {
            'Y' => dt.year.to_string(),
            'y' => pad((dt.year % 100) as i64, 2, no_pad),
            'm' => pad(dt.month as i64, 2, no_pad),
            'd' => pad(dt.day as i64, 2, no_pad),
            'H' => pad(dt.hour as i64, 2, no_pad),
            'I' => pad((((dt.hour + 11) % 12) + 1) as i64, 2, no_pad),
            'M' => pad(dt.minute as i64, 2, no_pad),
            'S' => pad(dt.second as i64, 2, no_pad),
            'j' => pad(yday + 1, 3, false),
            'w' => ((wd + 1) % 7).to_string(), // 0=周日
            'u' => (wd + 1).to_string(),
            'a' => WD_ABBR[wd].to_string(),
            'A' => WD_FULL[wd].to_string(),
            'b' | 'h' => MO_ABBR[(dt.month - 1) as usize].to_string(),
            'B' => MO_FULL[(dt.month - 1) as usize].to_string(),
            'p' => {
                if dt.hour < 12 {
                    "AM".to_string()
                } else {
                    "PM".to_string()
                }
            }
            // CPython 的公式：%U 以周日为一周之始、%W 以周一为始；
            // 新年第一个周几之前的日期都算第 0 周
            'U' => (((yday + 7 - (((wd as i64) + 1) % 7)) / 7)).to_string(),
            'W' => (((yday + 7 - wd as i64) / 7)).to_string(),
            'c' => format!(
                "{} {} {:2} {:02}:{:02}:{:02} {}",
                WD_ABBR[wd],
                MO_ABBR[(dt.month - 1) as usize],
                dt.day,
                dt.hour,
                dt.minute,
                dt.second,
                dt.year
            ),
            'x' => format!(
                "{:02}/{:02}/{:02}",
                dt.month,
                dt.day,
                dt.year % 100
            ),
            'X' => format!("{:02}:{:02}:{:02}", dt.hour, dt.minute, dt.second),
            '%' => "%".to_string(),
            _ => {
                // 认不出就原样保留（含 `%-` 之前那个 `%`）
                let mut raw: String = chars[i..=j].iter().collect();
                raw = raw.replace('\u{0}', "");
                raw
            }
        };
        out.push_str(&piece);
        i = j + 1;
    }
    out
}

// ---------------------------------------------------------------------------
// 日期解析
// ---------------------------------------------------------------------------

/// 解析 `2026-09-01` / `2026/09/01`（月日可不足两位，与 Python 的
/// `strptime` 一样宽松）。**不存在的日期（2026-02-30）要拒**。
fn parse_date(v: &Val) -> Result<(i32, u32, u32), JishiError> {
    let s = as_text(v).trim().to_string();
    let bad = || {
        err(
            "值错误",
            format!("「{}」不是有效的日期，请写成 2026-09-01 这样的格式", as_text(v)),
        )
    };
    // 手写解析：`^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$`
    let parts: Vec<&str> = if s.contains('/') {
        s.split('/').collect()
    } else if s.contains('-') {
        s.split('-').collect()
    } else {
        return Err(bad());
    };
    if parts.len() != 3 {
        return Err(bad());
    }
    if parts.iter().any(|p| p.is_empty() || !p.chars().all(|c| c.is_ascii_digit())) {
        return Err(bad());
    }
    if parts[0].len() != 4 || parts[1].len() > 2 || parts[2].len() > 2 {
        return Err(bad());
    }
    let y: i32 = parts[0].parse().map_err(|_| bad())?;
    let m: u32 = parts[1].parse().map_err(|_| bad())?;
    let d: u32 = parts[2].parse().map_err(|_| bad())?;
    if !(1..=12).contains(&m) || d < 1 || d as i64 > days_in_month(y, m) {
        return Err(bad());
    }
    Ok((y, m, d))
}

fn days_in_month(y: i32, m: u32) -> i64 {
    match m {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            if days_in_year(y) == 366 {
                29
            } else {
                28
            }
        }
        _ => 0,
    }
}

fn date_dt(y: i32, m: u32, d: u32) -> Dt {
    Dt {
        year: y,
        month: m,
        day: d,
        hour: 0,
        minute: 0,
        second: 0,
        weekday: weekday_of(days_from_civil(y, m, d)),
    }
}

fn shift_days(text: &Val, delta: i64) -> Result<String, JishiError> {
    let (y, m, d) = parse_date(text)?;
    let (ny, nm, nd) = civil_from_days(days_from_civil(y, m, d) + delta);
    Ok(format!("{:04}-{:02}-{:02}", ny, nm, nd))
}

/// `SystemTime` → 本地时间文本（`路径.修改时间` 用）。
pub(crate) fn format_system_time(t: SystemTime) -> String {
    let secs = t
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    strftime(&local_from_unix(secs), "%Y-%m-%d %H:%M:%S")
}

fn weekday_name(v: &Val) -> Result<String, JishiError> {
    let (y, m, d) = parse_date(v)?;
    Ok(WD_CN[weekday_of(days_from_civil(y, m, d)) as usize].to_string())
}

// ---------------------------------------------------------------------------
// 高精度计时（进程内单调时钟）
// ---------------------------------------------------------------------------

static T0: OnceLock<Instant> = OnceLock::new();

fn monotonic() -> f64 {
    let start = T0.get_or_init(Instant::now);
    start.elapsed().as_secs_f64()
}

/// `睡眠` 用的锁——不共享状态，只是让多个线程同时 sleep 时不互相干扰。
static SLEEP_GUARD: OnceLock<Mutex<()>> = OnceLock::new();

pub(crate) fn date_module() -> HashMap<String, Val> {
    table! {
        "今天" => |args: &[Val], _vm: &mut VM| -> R {
            need("今天", args, 0)?;
            let now = local_now();
            Ok(Val::Str(format!("{:04}-{:02}-{:02}", now.year, now.month, now.day)))
        },
        "解析" => |args: &[Val], _vm: &mut VM| -> R {
            need("解析", args, 1)?;
            let (y, m, d) = parse_date(&args[0])?;
            Ok(Val::Date(Rc::new(date_dt(y, m, d))))
        },
        "格式化" => |args: &[Val], _vm: &mut VM| -> R {
            need_range("格式化", args, 1, 2)?;
            let (y, m, d) = parse_date(&args[0])?;
            let fmt = if args.len() > 1 { as_text(&args[1]) } else { "%Y-%m-%d".to_string() };
            Ok(Val::Str(strftime(&date_dt(y, m, d), &fmt)))
        },
        "加天数" => |args: &[Val], _vm: &mut VM| -> R {
            need("加天数", args, 2)?;
            let n = as_intish(&args[0])?;
            Ok(Val::Str(shift_days(&args[1], n as i64)?))
        },
        "减天数" => |args: &[Val], _vm: &mut VM| -> R {
            need("减天数", args, 2)?;
            let n = as_intish(&args[0])?;
            Ok(Val::Str(shift_days(&args[1], -(n as i64))?))
        },
        "相差天数" => |args: &[Val], _vm: &mut VM| -> R {
            need("相差天数", args, 2)?;
            let (y1, m1, d1) = parse_date(&args[0])?;
            let (y2, m2, d2) = parse_date(&args[1])?;
            Ok(Val::Int(
                (days_from_civil(y1, m1, d1) - days_from_civil(y2, m2, d2)) as i128,
            ))
        },
        "早于" => |args: &[Val], _vm: &mut VM| -> R {
            need("早于", args, 2)?;
            let a = parse_date(&args[0])?;
            let b = parse_date(&args[1])?;
            Ok(Val::Bool(days_from_civil(a.0, a.1, a.2) < days_from_civil(b.0, b.1, b.2)))
        },
        "晚于" => |args: &[Val], _vm: &mut VM| -> R {
            need("晚于", args, 2)?;
            let a = parse_date(&args[0])?;
            let b = parse_date(&args[1])?;
            Ok(Val::Bool(days_from_civil(a.0, a.1, a.2) > days_from_civil(b.0, b.1, b.2)))
        },
        "相等" => |args: &[Val], _vm: &mut VM| -> R {
            need("相等", args, 2)?;
            let a = parse_date(&args[0])?;
            let b = parse_date(&args[1])?;
            Ok(Val::Bool(a == b))
        },
        "星期名" => |args: &[Val], _vm: &mut VM| -> R {
            need("星期名", args, 1)?;
            Ok(Val::Str(weekday_name(&args[0])?))
        },
        "今年" => |args: &[Val], _vm: &mut VM| -> R {
            need("今年", args, 0)?;
            Ok(Val::Int(local_now().year as i128))
        },
        "本月" => |args: &[Val], _vm: &mut VM| -> R {
            need("本月", args, 0)?;
            Ok(Val::Int(local_now().month as i128))
        },
        "本年天数" => |args: &[Val], _vm: &mut VM| -> R {
            need("本年天数", args, 0)?;
            Ok(Val::Int(days_in_year(local_now().year) as i128))
        },
    }
}

pub(crate) fn time_module() -> HashMap<String, Val> {
    table! {
        "现在" => |args: &[Val], _vm: &mut VM| -> R {
            need("现在", args, 0)?;
            let n = local_now();
            Ok(Val::Str(format!(
                "{:04}-{:02}-{:02} {:02}:{:02}:{:02}",
                n.year, n.month, n.day, n.hour, n.minute, n.second
            )))
        },
        "今天" => |args: &[Val], _vm: &mut VM| -> R {
            need("今天", args, 0)?;
            let n = local_now();
            Ok(Val::Str(format!("{:04}-{:02}-{:02}", n.year, n.month, n.day)))
        },
        "此刻" => |args: &[Val], _vm: &mut VM| -> R {
            need("此刻", args, 0)?;
            let n = local_now();
            let pairs = vec![
                (Val::Str("年".into()), Val::Int(n.year as i128)),
                (Val::Str("月".into()), Val::Int(n.month as i128)),
                (Val::Str("日".into()), Val::Int(n.day as i128)),
                (Val::Str("时".into()), Val::Int(n.hour as i128)),
                (Val::Str("分".into()), Val::Int(n.minute as i128)),
                (Val::Str("秒".into()), Val::Int(n.second as i128)),
                (Val::Str("星期".into()), Val::Str(WD_CN[n.weekday as usize].to_string())),
            ];
            Ok(crate::dict_new(pairs))
        },
        "时间戳" => |args: &[Val], _vm: &mut VM| -> R {
            need("时间戳", args, 0)?;
            let secs = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0);
            Ok(Val::Float(secs))
        },
        "格式化时间戳" => |args: &[Val], _vm: &mut VM| -> R {
            need("格式化时间戳", args, 1)?;
            let ts = super::as_num(&args[0])?;
            Ok(Val::Str(local_from_unix(ts).to_display()))
        },
        "睡眠" => |args: &[Val], _vm: &mut VM| -> R {
            need("睡眠", args, 1)?;
            let secs = super::as_num(&args[0])?.max(0.0);
            let guard = SLEEP_GUARD.get_or_init(|| Mutex::new(()));
            let _hold = guard.lock();
            std::thread::sleep(std::time::Duration::from_secs_f64(secs));
            Ok(Val::None_)
        },
        "高精度时间" => |args: &[Val], _vm: &mut VM| -> R {
            need("高精度时间", args, 0)?;
            Ok(Val::Float(monotonic()))
        },
        "星期名" => |args: &[Val], _vm: &mut VM| -> R {
            need("星期名", args, 1)?;
            Ok(Val::Str(weekday_name(&args[0])?))
        },
        "加天数" => |args: &[Val], _vm: &mut VM| -> R {
            need("加天数", args, 2)?;
            let n = as_intish(&args[0])?;
            Ok(Val::Str(shift_days(&args[1], n as i64)?))
        },
    }
}
