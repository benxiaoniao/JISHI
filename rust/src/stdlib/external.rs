//! 外部调用（M236 / L-7）—— **主路径的逃生口**。
//!
//! ```jishi
//! 导入 外部
//! 令 核 = 外部.加载("kernel32.dll")
//! 打印(外部.调用(核, "GetTickCount64", [], []))
//! 外部.释放(核)
//! ```
//!
//! ## 一条必须记死的判断：能力上限 ≠ 沙箱边界
//!
//! 🔴 **基石是全平台语言，正常使用时根本没有沙箱**（2026-11-26 用户再次强调）。
//! `--sandbox` 是一个**显式开关**（给 playground / 托管执行那种「不信任代码」的场景），
//! wasm 页面是另一条物理上不存在的路径（浏览器里没有动态库这东西）。
//!
//! | 路径 | `外部` |
//! |---|---|
//! | **主路径**（命令行 / 桌面 / 服务）—— **默认就是这个** | ✅ 完全可用 |
//! | `--sandbox`（显式开） | ❌ 明确拒绝（见 `crate::sandbox`） |
//! | wasm 页面 | ❌ 明确报错（页面里没有动态库，不是「沙箱限制」） |
//!
//! 0.3.6 的 §14.6 用「等于把沙箱拆了」拒过这件事，**那个理由站不住**（D130）：
//! 沙箱只限制那两条路径；主路径本来就能 `系统.执行`、读写任意文件、连任意网络。
//! **能力在，路径有限制** —— 别把「沙箱里不行」当成「语言不支持」，
//! 也别把沙箱当默认。零依赖也不冲突：`LoadLibraryW` / `GetProcAddress` /
//! `dlopen` / `dlsym` 都是系统自带。
//!
//! ## 为什么能「一个通用函数指针调一切」—— 这是本模块最需要解释的设计
//!
//! 参数类型只有四种，而它们在 **ABI 层面都是一个机器字**：
//!
//! | 基石类型 | 传什么 | 宽度 |
//! |---|---|---|
//! | `"整数"` | `i64` 直接当机器字 | 8 |
//! | `"指针"` | `isize`（用户自己算好的地址） | 8 |
//! | `"文本"` | 平台原生字符串的指针（Win32 UTF-16 / POSIX UTF-8） | 8 |
//! | `"文本UTF8"` | 一律 UTF-8 的指针（跨平台库用这个） | 8 |
//! | `"空"` | 0 | 8 |
//!
//! 所以可以声明**一个**「收 8 个机器字、回 1 个机器字」的函数指针，
//! 把所有实参拍平成 `usize` 丢进去 —— 不用为每种类型组合生成一个签名
//! （4 种类型 × 8 个参数 = 65536 种组合，那才是真正做不下去的地方）。
//!
//! ✅ 安全性依据（都是调用约定保证的，不是猜的）：
//! - **x86-64**：前 4 个整数参数走 `rcx/rdx/r8/r9`，第 5 个起走**栈上 8 字节槽**；
//! - **AArch64**：前 8 个整数参数走 `x0–x7`；
//! - 多传的参数只是**没人看**的寄存器/栈槽 —— 不会破坏调用（cdecl 由被调方忽略）。
//!
//! 🔴 **只在 64 位成立**：32 位上 `i64` 参数要占**两个**栈槽，而 `usize` 是 4 字节
//! ⇒ 会错位。所以 32 位直接报错，不硬撑（见 `调用` 里的 cfg）。
//!
//! 🔴 **不支持浮点参数**：`double` 走 XMM 寄存器，不在这套拍平规则里。
//! 传浮点会「看着能跑、数值是垃圾」—— 所以在类型表里**明确拒绝** `"小数"`。
//!
//! ## 崩溃归用户（文档必须说这句）
//!
//! 类型表给错了（把 `int` 写成 `"指针"`、少给一个参数、拿 `void` 当返回值）会让
//! **整个进程打崩**，不是抛异常 —— 这是 FFI 的本性，宿主管不了。
//! 所以这是**逃生口，不是日常 API**：能用标准库就别用它。
//! 本模块能保证的只有两件：① **参数个数对不上时自己先报错**（不真调）；
//! ② **类型名写错时当场报并列出可用写法**（不会带着错表去调）。
//!
//! ## 平台
//!
//! | 平台 | 加载 | 调用 | 分配 |
//! |---|---|---|---|
//! | Windows | `LoadLibraryW` | `GetProcAddress` | `Vec<u8>` → `Box<[u8]>` 泄漏后交出去 |
//! | Linux / macOS | `dlopen(RTLD_LAZY\|RTLD_LOCAL)` | `dlsym` | 同上 |
//! | wasm（页面） | ❌ 页面没有动态库 | | |

use std::collections::HashMap;
use std::rc::Rc;

use crate::{err, err_with_hint, JishiError, Val};

/// 本模块的错误类型名（`类型()` 也用它）。
const 类型名: &str = "外部";

pub(crate) fn module() -> HashMap<String, Val> {
    let mut m = HashMap::new();
    m.insert("加载".to_string(), crate::builtin("加载", |args, _vm| 加载(args)));
    m.insert("调用".to_string(), crate::builtin("调用", |args, _vm| 模块调用(args)));
    m.insert("释放".to_string(), crate::builtin("释放", |args, _vm| 模块释放(args)));
    m.insert("分配".to_string(), crate::builtin("分配", |args, _vm| 分配(args)));
    m.insert("释放块".to_string(), crate::builtin("释放块", |args, _vm| 模块释放块(args)));
    m.insert("写整数".to_string(), crate::builtin("写整数", |args, _vm| 模块写整数(args)));
    m.insert("读整数".to_string(), crate::builtin("读整数", |args, _vm| 模块读整数(args)));
    m
}

// ---------------------------------------------------------------------------
// 句柄与内存块
// ---------------------------------------------------------------------------

/// 动态库句柄（活在 `NativeObj.state` 里）。
pub(crate) struct 库状态 {
    /// `HMODULE` / `dlopen` 的返回值；已释放时恒为 0。
    句柄: isize,
    已释放: bool,
    /// 名字 → 地址。**同一个函数反复调**（渲染循环里很常见）不必每次 `dlsym`。
    缓存: HashMap<String, usize>,
}

/// 一块「原生代码能读写」的内存。
pub(crate) struct 块状态 {
    指针: usize,
    长度: usize,
}

fn 借库(对象: &Rc<crate::NativeObj>) -> std::cell::Ref<'_, 库状态> {
    std::cell::Ref::map(对象.state.borrow(), |s| {
        s.downcast_ref::<库状态>().expect("NativeObj 应是外部.加载的句柄")
    })
}

fn 借库改(对象: &Rc<crate::NativeObj>) -> std::cell::RefMut<'_, 库状态> {
    std::cell::RefMut::map(对象.state.borrow_mut(), |s| {
        s.downcast_mut::<库状态>().expect("NativeObj 应是外部.加载的句柄")
    })
}

fn 借块(对象: &Rc<crate::NativeObj>) -> std::cell::Ref<'_, 块状态> {
    std::cell::Ref::map(对象.state.borrow(), |s| {
        s.downcast_ref::<块状态>().expect("NativeObj 应是外部.分配的内存块")
    })
}

fn 认库(v: &Val, 谁: &str) -> Result<Rc<crate::NativeObj>, JishiError> {
    match v {
        Val::Native(o) if o.type_name == 类型名 && o.state.borrow().is::<库状态>() => Ok(o.clone()),
        _ => Err(err_with_hint(
            "类型错误",
            format!("「{谁}」的库参数要 `外部.加载` 拿到的句柄（现在是「{}」）", crate::type_label(v)),
            "先 `令 核 = 外部.加载(\"kernel32.dll\")`，再把「核」传进来".into(),
        )),
    }
}

fn 认块(v: &Val, 谁: &str) -> Result<Rc<crate::NativeObj>, JishiError> {
    match v {
        Val::Native(o) if o.type_name == 类型名 && o.state.borrow().is::<块状态>() => Ok(o.clone()),
        _ => Err(err_with_hint(
            "类型错误",
            format!("「{谁}」的块参数要 `外部.分配` 拿到的块（现在是「{}」）", crate::type_label(v)),
            "`令 块 = 外部.分配(64)`，再把它传给 `写整数` / `读整数`".into(),
        )),
    }
}

// ---------------------------------------------------------------------------
// 加载 / 释放
// ---------------------------------------------------------------------------

/// `外部.加载(路径) -> 句柄`
fn 加载(args: &[Val]) -> Result<Val, JishiError> {
    crate::stdlib::need_range("加载", args, 1, 1)?;
    let 名 = crate::stdlib::as_text(&args[0]);
    if 名.trim().is_empty() {
        return Err(err("值错误", "「加载」的库名不能是空串".to_string()));
    }
    #[cfg(any(windows, all(unix, any(target_os = "linux", target_os = "macos"))))]
    {
        let 句柄 = plat::打开库(&名)?;
        let 状态 = 库状态 { 句柄, 已释放: false, 缓存: HashMap::new() };
        Ok(crate::native_obj(类型名, 库方法们, Rc::new(std::cell::RefCell::new(状态))))
    }
    #[cfg(not(any(windows, all(unix, any(target_os = "linux", target_os = "macos")))))]
    {
        let _ = 名;
        Err(平台说明("加载"))
    }
}

/// `外部.释放(句柄) -> 真/假`（幂等：第一次真、再放假）
fn 释放(对象: &Rc<crate::NativeObj>) -> Result<Val, JishiError> {
    let mut st = 借库改(对象);
    if st.已释放 {
        return Ok(Val::Bool(false));
    }
    #[cfg(any(windows, all(unix, any(target_os = "linux", target_os = "macos"))))]
    {
        if st.句柄 != 0 {
            plat::关闭库(st.句柄);
        }
    }
    st.句柄 = 0;
    st.已释放 = true;
    st.缓存.clear();
    Ok(Val::Bool(true))
}

fn 模块释放(args: &[Val]) -> Result<Val, JishiError> {
    crate::stdlib::need_range("释放", args, 1, 1)?;
    let o = 认库(&args[0], "释放")?;
    释放(&o)
}

// ---------------------------------------------------------------------------
// 调用
// ---------------------------------------------------------------------------

/// 支持的类型名（报错时原样列给用户 —— 猜错类型名是这里最常见的错）。
const 类型名们: &[&str] = &["整数", "指针", "文本", "文本UTF8", "空"];

fn 规范化类型(原: &str) -> Result<String, JishiError> {
    let t = 原.trim();
    if 类型名们.contains(&t) {
        return Ok(t.to_string());
    }
    // 浮点单独给一句「为什么不支持」—— 只说「不认识」用户会以为我们漏了
    if t == "小数" || t == "浮点" || t == "f64" {
        return Err(err_with_hint(
            "值错误",
            "「外部.调用」不支持浮点参数".to_string(),
            "浮点走 XMM/FP 寄存器，不在「所有参数都是一个机器字」这套里；\
             要传浮点就先在基石里换算成定点整数（比如乘以 1000 传整数）".into(),
        ));
    }
    Err(err_with_hint(
        "值错误",
        format!("不认识的外部类型「{原}」"),
        format!("只有这几种：{}", 类型名们.join(" / ")),
    ))
}

fn 模块调用(args: &[Val]) -> Result<Val, JishiError> {
    crate::stdlib::need_range("调用", args, 4, 5)?;
    let 库 = 认库(&args[0], "调用")?;
    let 名 = crate::stdlib::as_text(&args[1]);
    let 类型们 = 取文本列表(&args[2], "参数类型")?;
    let 实参们 = match &args[3] {
        Val::List(l) => l.borrow().clone(),
        other => {
            return Err(err(
                "类型错误",
                format!("「调用」的第 4 个参数要**实参列表**，现在给了「{}」", crate::type_label(other)),
            ))
        }
    };
    let 返回 = match args.get(4) {
        None | Some(Val::None_) => "整数".to_string(),
        Some(v) => 规范化类型(&crate::stdlib::as_text(v))?,
    };
    调用(&库, &名, 类型们, 实参们, 返回)
}

fn 取文本列表(v: &Val, 谁: &str) -> Result<Vec<String>, JishiError> {
    match v {
        Val::List(l) => {
            let mut 出 = Vec::new();
            for x in l.borrow().iter() {
                出.push(规范化类型(&crate::stdlib::as_text(x))?);
            }
            Ok(出)
        }
        other => Err(err(
            "类型错误",
            format!("「调用」的「{谁}」要是一个**列表**（如 [\"整数\", \"文本\"]），现在给了「{}」",
                    crate::type_label(other)),
        )),
    }
}

/// 调用期间要把字符串**留住**（指针一交给原生代码，Rust 侧就不能释放）。
enum 保活 {
    宽(Vec<u16>),
    窄(Vec<u8>),
}

impl 保活 {
    fn 指针(&self) -> usize {
        match self {
            // `as_ptr` 拿到的是「已经以 0 结尾」的那块的地址
            保活::宽(v) => v.as_ptr() as usize,
            保活::窄(v) => v.as_ptr() as usize,
        }
    }
}

fn 调用(
    库: &Rc<crate::NativeObj>,
    名: &str,
    类型们: Vec<String>,
    实参们: Vec<Val>,
    返回: String,
) -> Result<Val, JishiError> {
    if 名.trim().is_empty() {
        return Err(err("值错误", "「调用」的函数名不能是空串".to_string()));
    }
    // 🔴 **参数个数对不上就先报**（这是本模块唯一能替用户兜住的错）：
    //    真按错个数调过去，轻则读到一个垃圾返回值，重则把进程打崩。
    if 类型们.len() != 实参们.len() {
        return Err(err_with_hint(
            "值错误",
            format!("「{名}」的参数类型有 {} 个，实参给了 {} 个", 类型们.len(), 实参们.len()),
            "两个列表必须一一对应：`[\"整数\", \"文本\"]` 配 `[1, \"甲\"]`".into(),
        ));
    }
    if 类型们.len() > 8 {
        return Err(err_with_hint(
            "值错误",
            format!("「{名}」有 {} 个参数，本模块最多支持 8 个", 类型们.len()),
            "需要更多参数的 C 函数，通常该包一层自己的库；或者把结构体指针传进来\
             （用 `外部.分配` + `写整数`）".into(),
        ));
    }
    #[cfg(target_pointer_width = "32")]
    {
        let _ = (库, 名, 类型们, 实参们, 返回);
        return Err(err_with_hint(
            "运行期错误",
            "`外部.调用` 只在 **64 位**平台上支持".to_string(),
            "32 位上 8 字节整数参数要占两个栈槽，拍平成机器字会错位 —— 与其悄悄给错结果，\
             不如明确说不支持".into(),
        ));
    }
    #[cfg(not(target_pointer_width = "32"))]
    {
        let 地址 = 查函数(库, 名)?;

        // 参数拍平成机器字；文本要同时把它们**留住**
        let mut 持有: Vec<保活> = Vec::new();
        let mut 字们 = [0usize; 8];
        for (i, (t, v)) in 类型们.iter().zip(实参们.iter()).enumerate() {
            字们[i] = match t.as_str() {
                "整数" => 取整数(v, 名, i)? as usize,
                "指针" => 取整数(v, 名, i)? as usize,
                "空" => 0,
                "文本" => {
                    let s = crate::stdlib::as_text(v);
                    持有.push(造文本(&s));
                    持有.last().unwrap().指针()
                }
                "文本UTF8" => {
                    let s = crate::stdlib::as_text(v);
                    let mut b = s.as_bytes().to_vec();
                    b.push(0);
                    持有.push(保活::窄(b));
                    持有.last().unwrap().指针()
                }
                other => {
                    return Err(err("值错误", format!("内部错误：未处理的类型「{other}」")))
                }
            };
        }

        let 原始 = unsafe { 调(地址, 字们) };
        match 返回.as_str() {
            "空" => Ok(Val::None_),
            "指针" => Ok(Val::Int(crate::jint::JInt::new(原始 as i64 as i128))),
            _ => Ok(Val::Int(crate::jint::JInt::new(原始 as i64 as i128))),
        }
    }
}

fn 取整数(v: &Val, 名: &str, 第几个: usize) -> Result<i64, JishiError> {
    match v {
        Val::Int(i) => Ok(i.get() as i64),
        Val::Bool(b) => Ok(if *b { 1 } else { 0 }),
        Val::None_ => Ok(0),
        other => Err(err(
            "类型错误",
            format!("「{名}」的第 {} 个参数类型写了「整数」，但实参是「{}」",
                    第几个 + 1, crate::type_label(other)),
        )),
    }
}

fn 查函数(库: &Rc<crate::NativeObj>, 名: &str) -> Result<usize, JishiError> {
    // 先读缓存（读锁时间极短，不持锁去查库）
    {
        let st = 借库(库);
        if st.已释放 {
            return Err(err("运行期错误", "这个库已经 `外部.释放` 了，不能再调用".to_string()));
        }
        if let Some(a) = st.缓存.get(名) {
            return Ok(*a);
        }
    }
    let 句柄 = 借库(库).句柄;
    #[cfg(any(windows, all(unix, any(target_os = "linux", target_os = "macos"))))]
    {
        let 地址 = plat::查符号(句柄, 名)?;
        if 地址 == 0 {
            return Err(err_with_hint(
                "运行期错误",
                format!("库里没有「{名}」这个函数"),
                "名字要**区分大小写且一字不差**（`GetTickCount64` 不是 `gettickcount64`）；\
                 Win32 的 `A`/`W` 两个版本是两个不同的导出名".into(),
            ));
        }
        借库改(库).缓存.insert(名.to_string(), 地址);
        Ok(地址)
    }
    #[cfg(not(any(windows, all(unix, any(target_os = "linux", target_os = "macos")))))]
    {
        let _ = 句柄;
        Err(平台说明("调用"))
    }
}

// ---------------------------------------------------------------------------
// 内存块
// ---------------------------------------------------------------------------

/// `外部.分配(字节) -> 块`
fn 分配(args: &[Val]) -> Result<Val, JishiError> {
    crate::stdlib::need_range("分配", args, 1, 1)?;
    let n = match &args[0] {
        Val::Int(i) => i.get() as i64,
        other => {
            return Err(err(
                "类型错误",
                format!("「分配」要一个字节数（整数），现在给了「{}」", crate::type_label(other)),
            ))
        }
    };
    if n <= 0 {
        return Err(err("值错误", format!("「分配」的字节数要大于 0，现在是 {n}")));
    }
    if n > 64 * 1024 * 1024 {
        return Err(err_with_hint(
            "值错误",
            format!("一次分配 {n} 字节太多了（上限 64 MB）"),
            "大块内存请让原生库自己分配，把指针用 `外部.调用` 拿回来".into(),
        ));
    }
    // `Box<[u8]>` 泄漏成裸指针：原生代码能读写，`释放块` 时按长度重建 Box 收回
    let mut 块 = vec![0u8; n as usize].into_boxed_slice();
    let 指针 = 块.as_mut_ptr() as usize;
    std::mem::forget(块);
    let 状态 = 块状态 { 指针, 长度: n as usize };
    Ok(crate::native_obj(类型名, 块方法们, Rc::new(std::cell::RefCell::new(状态))))
}

/// `外部.释放块(块) -> 真/假`
fn 释放块(对象: &Rc<crate::NativeObj>) -> Result<Val, JishiError> {
    let mut st = 对象.state.borrow_mut();
    let s = st.downcast_mut::<块状态>().expect("NativeObj 应是块");
    if s.指针 == 0 {
        return Ok(Val::Bool(false));       // 幂等
    }
    let 指针 = s.指针;
    let 长度 = s.长度;
    s.指针 = 0;
    s.长度 = 0;
    drop(st);
    // SAFETY：指针与长度都是 `分配` 时记下的，且只在这里重建一次（之后置 0）
    unsafe {
        let 切片 = std::slice::from_raw_parts_mut(指针 as *mut u8, 长度);
        drop(Box::from_raw(切片 as *mut [u8]));
    }
    Ok(Val::Bool(true))
}

fn 模块释放块(args: &[Val]) -> Result<Val, JishiError> {
    crate::stdlib::need_range("释放块", args, 1, 1)?;
    释放块(&认块(&args[0], "释放块")?)
}

fn 写整数(对象: &Rc<crate::NativeObj>, args: &[Val]) -> Result<Val, JishiError> {
    crate::stdlib::need_range("写整数", args, 2, 3)?;
    let 偏移 = 取偏移(&args[0])?;
    let 值 = match &args[1] {
        Val::Int(i) => i.get() as i64,
        Val::Bool(b) => if *b { 1 } else { 0 },
        other => {
            return Err(err(
                "类型错误",
                format!("「写整数」要写一个整数，现在给了「{}」", crate::type_label(other)),
            ))
        }
    };
    let 宽度 = if args.len() > 2 { 取宽度(&args[2])? } else { 8 };
    let s = 借块(对象);
    if s.指针 == 0 {
        return Err(err("运行期错误", "这块内存已经 `释放块` 了".to_string()));
    }
    if 偏移 + 宽度 > s.长度 {
        return Err(err_with_hint(
            "值错误",
            format!("写越界：偏移 {偏移} + {宽度} 字节 > 块长 {}", s.长度),
            "偏移是从**块首**算的字节数；块够不够大用 `外部.分配` 时就该算好".into(),
        ));
    }
    // SAFETY：偏移与宽度都校验过落在块内；`write_unaligned` 允许非对齐写入
    unsafe {
        let p = (s.指针 + 偏移) as *mut u8;
        match 宽度 {
            1 => std::ptr::write_unaligned(p, 值 as u8),
            2 => std::ptr::write_unaligned(p as *mut u16, 值 as u16),
            4 => std::ptr::write_unaligned(p as *mut u32, 值 as u32),
            _ => std::ptr::write_unaligned(p as *mut u64, 值 as u64),
        }
    }
    Ok(Val::None_)
}

fn 读整数(对象: &Rc<crate::NativeObj>, args: &[Val]) -> Result<Val, JishiError> {
    crate::stdlib::need_range("读整数", args, 1, 2)?;
    let 偏移 = 取偏移(&args[0])?;
    let 宽度 = if args.len() > 1 { 取宽度(&args[1])? } else { 8 };
    let s = 借块(对象);
    if s.指针 == 0 {
        return Err(err("运行期错误", "这块内存已经 `释放块` 了".to_string()));
    }
    if 偏移 + 宽度 > s.长度 {
        return Err(err_with_hint(
            "值错误",
            format!("读越界：偏移 {偏移} + {宽度} 字节 > 块长 {}", s.长度),
            "偏移是从**块首**算的字节数".into(),
        ));
    }
    // SAFETY：同 `写整数`
    let 值 = unsafe {
        let p = (s.指针 + 偏移) as *const u8;
        match 宽度 {
            1 => std::ptr::read_unaligned(p) as i64,
            2 => std::ptr::read_unaligned(p as *const u16) as i64,
            4 => std::ptr::read_unaligned(p as *const u32) as i64,
            _ => std::ptr::read_unaligned(p as *const u64) as i64,
        }
    };
    Ok(Val::Int(crate::jint::JInt::new(值 as i128)))
}

fn 取偏移(v: &Val) -> Result<usize, JishiError> {
    match v {
        Val::Int(i) => {
            let n = i.get() as i64;
            if n < 0 {
                return Err(err("值错误", format!("偏移不能是负数（给的是 {n}）")));
            }
            Ok(n as usize)
        }
        other => Err(err(
            "类型错误",
            format!("偏移要一个整数，现在给了「{}」", crate::type_label(other)),
        )),
    }
}

/// 宽度只认 1 / 2 / 4 / 8（`char` / `short` / `int` / `指针`）—— 其它宽度没有对应
/// 的 C 类型，给了就是用户算错了。
fn 取宽度(v: &Val) -> Result<usize, JishiError> {
    let n = match v {
        Val::Int(i) => i.get() as i64,
        other => {
            return Err(err(
                "类型错误",
                format!("宽度要一个整数，现在给了「{}」", crate::type_label(other)),
            ))
        }
    };
    match n {
        1 | 2 | 4 | 8 => Ok(n as usize),
        _ => Err(err_with_hint(
            "值错误",
            format!("宽度只能是 1 / 2 / 4 / 8（给的是 {n}）"),
            "对应 C 的 `char` / `short` / `int` / 指针；其它宽度没有对应的基本类型".into(),
        )),
    }
}

fn 模块写整数(args: &[Val]) -> Result<Val, JishiError> {
    if args.is_empty() {
        return Err(err("类型错误", "「写整数」至少要 2 个参数（块, 偏移, 值？）".to_string()));
    }
    let o = 认块(&args[0], "写整数")?;
    写整数(&o, &args[1..])
}

fn 模块读整数(args: &[Val]) -> Result<Val, JishiError> {
    if args.is_empty() {
        return Err(err("类型错误", "「读整数」至少要 1 个参数（块, 偏移？）".to_string()));
    }
    let o = 认块(&args[0], "读整数")?;
    读整数(&o, &args[1..])
}

// ---------------------------------------------------------------------------
// 句柄 / 块上的方法（两种写法都通，与 `路径.监视` 同一口径）
// ---------------------------------------------------------------------------

/// `句柄.调用(名字, 类型们, 实参们, 返回="整数")`
fn 方法调用(
    对象: &Rc<crate::NativeObj>,
    args: &[Val],
    _vm: &mut dyn crate::BuiltinHost,
) -> Result<Val, JishiError> {
    let mut 全 = vec![Val::Native(对象.clone())];
    全.extend_from_slice(args);
    模块调用(&全)
}

fn 方法释放(
    对象: &Rc<crate::NativeObj>,
    _args: &[Val],
    _vm: &mut dyn crate::BuiltinHost,
) -> Result<Val, JishiError> {
    释放(对象)
}

fn 方法写整数(
    对象: &Rc<crate::NativeObj>,
    args: &[Val],
    _vm: &mut dyn crate::BuiltinHost,
) -> Result<Val, JishiError> {
    写整数(对象, args)
}

fn 方法读整数(
    对象: &Rc<crate::NativeObj>,
    args: &[Val],
    _vm: &mut dyn crate::BuiltinHost,
) -> Result<Val, JishiError> {
    读整数(对象, args)
}

fn 方法释放块(
    对象: &Rc<crate::NativeObj>,
    _args: &[Val],
    _vm: &mut dyn crate::BuiltinHost,
) -> Result<Val, JishiError> {
    释放块(对象)
}

static 库方法们: &[(&str, crate::NativeFn)] = &[("调用", 方法调用), ("释放", 方法释放)];
static 块方法们: &[(&str, crate::NativeFn)] =
    &[("写整数", 方法写整数), ("读整数", 方法读整数), ("释放", 方法释放块)];

// ---------------------------------------------------------------------------
// 平台
// ---------------------------------------------------------------------------

/// 页面（wasm）上的说明 —— 这是**降级**，不是「不支持所以报错就算完」。
#[cfg(not(any(windows, all(unix, any(target_os = "linux", target_os = "macos")))))]
fn 平台说明(操作: &str) -> JishiError {
    err_with_hint(
        "运行期错误",
        format!("`外部.{操作}` 在浏览器里用不了（页面里没有动态库这种东西）"),
        "`外部` 要跑在桌面宿主（Windows / Linux / macOS 的 Rust 发行版）上。\
         想要浏览器里的原生能力，得靠 WebAssembly 或浏览器自带的 API（`浏览器` 模块）"
            .into(),
    )
}

/// 🔴 **一个通用签名调一切**（理由见模块文档）。
    ///
    /// 8 个机器字进、1 个机器字出：x86-64 前 4 个走寄存器、之后走 8 字节栈槽；
    /// AArch64 前 8 个走 `x0–x7`。多传的没人看，不破坏调用。
#[cfg(any(windows, all(unix, any(target_os = "linux", target_os = "macos"))))]
type 通用调用 = unsafe extern "system" fn(
        usize, usize, usize, usize, usize, usize, usize, usize) -> usize;

#[cfg(any(windows, all(unix, any(target_os = "linux", target_os = "macos"))))]
fn 调(地址: usize, 参数: [usize; 8]) -> usize {
        // SAFETY：调用方保证地址来自 `dlsym`/`GetProcAddress`；
        // 参数个数与类型由 `外部.调用` 的类型表决定 —— **签名叫错了的后果归用户**，
        // 这是 FFI 的本性（文档里写了「这是逃生口，不是日常 API」）。
        let f: 通用调用 = unsafe { std::mem::transmute(地址) };
        unsafe { f(参数[0], 参数[1], 参数[2], 参数[3], 参数[4], 参数[5], 参数[6], 参数[7]) }
    }

    /// 平台原生的字符串形态：Win32 是**UTF-16**（`MessageBoxW` 那一族要的就是它），
    /// POSIX 是 **UTF-8**（`printf` / `sqlite3_open` 那一族）。
    ///
    /// ⚠️ 这就是为什么本模块还有个 `"文本UTF8"`：在 Windows 上调 `printf` 这类
    /// **要 UTF-8 的**函数，`"文本"` 会给出宽字符指针 ⇒ 对方读到一个字节乱码。
    /// 跨平台的库（sqlite / curl / libpng）一律用 `"文本UTF8"`。
#[cfg(any(windows, all(unix, any(target_os = "linux", target_os = "macos"))))]
fn 造文本(s: &str) -> 保活 {
        #[cfg(windows)]
        {
            let mut v: Vec<u16> = s.encode_utf16().collect();
            v.push(0);
            保活::宽(v)
        }
        #[cfg(not(windows))]
        {
            let mut v = s.as_bytes().to_vec();
            v.push(0);
        保活::窄(v)
    }
}

// ---------------------------------------------------------------------------
// Windows 裸 Win32
// ---------------------------------------------------------------------------

#[cfg(windows)]
mod plat {
    use super::*;

    #[link(name = "kernel32")]
    extern "system" {
        fn LoadLibraryW(路径: *const u16) -> isize;
        fn GetProcAddress(库: isize, 名字: *const u8) -> usize;
        fn FreeLibrary(库: isize) -> i32;
        fn GetLastError() -> u32;
    }

    pub(super) fn 打开库(名: &str) -> Result<isize, JishiError> {
        let mut 宽: Vec<u16> = 名.encode_utf16().collect();
        宽.push(0);
        let h = unsafe { LoadLibraryW(宽.as_ptr()) };
        if h != 0 {
            return Ok(h);
        }
        let 码 = unsafe { GetLastError() };
        // 「试过的路径」：`LoadLibraryW` 对**裸名字**用的是系统搜索规则
        // （exe 目录 → System32 → PATH），我们列不出来也不该假装知道。
        // 但**用户给了路径**时能明确说「这个文件不存在」—— 那才是他要的答案。
        let 含路径 = 名.contains('\\') || 名.contains('/');
        let hint = if 含路径 {
            format!("检查这个路径是否存在：{名}（相对路径按**当前工作目录**算，不是脚本目录）")
        } else {
            format!("「{名}」是**裸名字**，交给系统搜索规则找（程序目录 → System32 → PATH）——\
                     系统错误码 {码}；不放心就把**完整路径**传进来")
        };
        Err(err_with_hint(
            "文件错误",
            format!("加载不了动态库「{名}」（系统错误码 {码}）"),
            hint,
        ))
    }

    pub(super) fn 查符号(库: isize, 名: &str) -> Result<usize, JishiError> {
        // `GetProcAddress` 收 **ANSI** 名字（导出名本身就是字节串），ASCII 足够
        let mut b = 名.as_bytes().to_vec();
        b.push(0);
        Ok(unsafe { GetProcAddress(库, b.as_ptr()) })
    }

    pub(super) fn 关闭库(库: isize) {
        unsafe {
            FreeLibrary(库);
        }
    }
}

// ---------------------------------------------------------------------------
// POSIX（Linux / macOS）
// ---------------------------------------------------------------------------

#[cfg(all(unix, any(target_os = "linux", target_os = "macos")))]
mod plat {
    use super::*;

    const RTLD_LAZY: i32 = 1;
    /// macOS 上 `RTLD_LOCAL` 是 **4**，Linux 上是 **0** —— 又一个「看着一样其实不同」。
    #[cfg(target_os = "linux")]
    const RTLD_LOCAL: i32 = 0;
    #[cfg(target_os = "macos")]
    const RTLD_LOCAL: i32 = 4;

    extern "C" {
        fn dlopen(路径: *const u8, 标志: i32) -> isize;
        fn dlsym(库: isize, 名: *const u8) -> usize;
        fn dlclose(库: isize) -> i32;
        fn dlerror() -> *const u8;
    }

    fn 最后动态库错误() -> String {
        let p = unsafe { dlerror() };
        if p.is_null() {
            return String::new();
        }
        unsafe { std::ffi::CStr::from_ptr(p as *const std::ffi::c_char) }
            .to_string_lossy()
            .to_string()
    }

    pub(super) fn 打开库(名: &str) -> Result<isize, JishiError> {
        let mut b = 名.as_bytes().to_vec();
        b.push(0);
        // 先把上一个错误清掉（`dlerror` 是「读一次就清」的语义）
        unsafe { dlerror() };
        let h = unsafe { dlopen(b.as_ptr(), RTLD_LAZY | RTLD_LOCAL) };
        if h != 0 {
            return Ok(h);
        }
        let 错 = 最后动态库错误();
        let hint = if 名.contains('/') {
            format!("检查这个路径是否存在：{名}（相对路径按**当前工作目录**算，不是脚本目录）")
        } else {
            format!("「{名}」是**裸名字**，交给系统加载器找（`/etc/ld.so.conf`、\
                     macOS 的 dyld 路径与 `DYLD_LIBRARY_PATH`）—— 加载器说：{错}")
        };
        Err(err_with_hint(
            "文件错误",
            format!("加载不了动态库「{名}」"),
            hint,
        ))
    }

    pub(super) fn 查符号(库: isize, 名: &str) -> Result<usize, JishiError> {
        let mut b = 名.as_bytes().to_vec();
        b.push(0);
        unsafe { dlerror() };
        let p = unsafe { dlsym(库, b.as_ptr()) };
        if p == 0 {
            // ⚠️ `dlsym` 返回 0 **不一定**是失败（符号地址真的可以是 0），
            // 所以要用 `dlerror` 复核 —— 这是 dlsym 出名的坑。
            let 错 = 最后动态库错误();
            if !错.is_empty() {
                return Err(err_with_hint(
                    "运行期错误",
                    format!("库里没有「{名}」这个符号"),
                    format!("加载器说：{错}（名字区分大小写；C++ 导出名会是 mangled 的）"),
                ));
            }
        }
        Ok(p)
    }

    pub(super) fn 关闭库(库: isize) {
        unsafe {
            dlclose(库);
        }
    }
}
