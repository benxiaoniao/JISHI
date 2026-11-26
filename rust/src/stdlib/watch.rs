//! `路径.监视`（M233 / L-6）：目录变了叫我一声。
//!
//! ## 为什么要有它
//!
//! 《磐石 IDE》改了文件要让预览跟着刷新。以前靠「每 500ms 全目录扫一遍比 mtime」——
//! 大项目下纯烧 CPU，而扫描本身比"等事件"慢一个数量级。这一站接上系统原生监视。
//!
//! ## API
//!
//! ```text
//! 令 表 = 路径.监视(目录, 处理函数, 选项=空)   → 句柄   （选项：{"递归": 真}）
//! 路径.等事件(表, 超时=空)                     → 处理了几条 / 超时给 `空`
//! 路径.停止监视(表)
//! ```
//!
//! 形参说明见 `docs/语言规格.md` 的「`路径`」一节。回调收一个字典：
//! `{"路径": …, "动作": "增"/"改"/"删"/"改名"/"溢出"}`，改名时多一个 `"旧路径"`。
//!
//! ## 🔴 为什么还要一个 `等事件`（不能"来事件就自动调"）
//!
//! 基石是**单线程解释器**，回调只能在解释器线程上跑；而系统事件是从**另一个线程**
//! 来的。所以分工是：
//!
//! - `监视` 起一个后台线程，只做「收事件 → 塞进队列」（**不碰解释器**）；
//! - `等事件` 在解释器线程上把队列取空、**逐条调处理函数**。
//!
//! ⇒ 处理函数**永远不会**在后台线程上被调 —— 这是安全性，不是限制。
//! 想「一边等一边干别的」就把 `等事件(表, 0)` 塞进 `窗口.常驻` 的每拍里。
//!
//! ## 三条必须写进文档的口径
//!
//! 1. **监视是提示，不是审计**。系统缓冲区有上限，来不及取就会丢事件 ——
//!    所以**不许**拿它当"文件有没有被改"的唯一依据。
//! 2. **默认不递归**，`选项={"递归": 真}` 才递归（Linux 要每层一个 watch，成本实打实）。
//! 3. `等事件` **超时不是错误**：到点给 `空`（与 `进程.读行` 同一个口径）。
//!
//! ## 平台
//!
//! | 平台 | 机制 | 平台自己的粗糙处（如实说） |
//! |---|---|---|
//! | Windows | `ReadDirectoryChangesW`（同步 + 独立线程） | 缓冲区溢出**会丢事件**；这里给 `动作="溢出"` 明确报出来，不静默 |
//! | Linux | `inotify`（递归时每层一个 watch） | 用 `IN_CLOSE_WRITE` 不用 `IN_MODIFY`（后者一次写触发好几次）；新建子目录要**补 watch**（已处理） |
//! | macOS | `FSEvents`（`kFSEventStreamCreateFlagFileEvents`） | 天然递归（非递归时按路径深度过滤）；0.1s 延迟合并；**改名常常只给新路径**（旧路径拿不到，这时只报一条 `改名`） |
//! | wasm（页面） | ❌ 页面没有文件系统 | 报一句明确的平台错 |

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};

use crate::{err, err_with_hint, BuiltinHost, JishiError, Val};

/// 句柄上的类型名（`类型()` 也用它）。
const 类型名: &str = "监视";

/// 一条事件。**只放纯数据** —— 它要从后台线程穿到解释器线程。
#[derive(Clone, Debug)]
pub(crate) struct 事件 {
    pub 动作: &'static str,
    pub 路径: String,
    pub 旧路径: Option<String>,
}

/// 队列 + 唤醒信号（`Condvar` 让 `等事件` 不用空转）。
type 队列 = Arc<(Mutex<VecDeque<事件>>, Condvar)>;

fn 新队列() -> 队列 {
    Arc::new((Mutex::new(VecDeque::new()), Condvar::new()))
}

/// 队列上限：用户不取事件时（忘了循环）不能把内存吃光。
const 队列上限: usize = 100_000;

fn 推(队: &队列, e: 事件) {
    let (锁, 信号) = &**队;
    if let Ok(mut q) = 锁.lock() {
        if q.len() >= 队列上限 {
            q.push_back(事件 { 动作: "溢出", 路径: String::new(), 旧路径: None });
        } else {
            q.push_back(e);
        }
    }
    信号.notify_all();
}

// ---------------------------------------------------------------------------
// 句柄
// ---------------------------------------------------------------------------

use plat::控制;

pub(crate) struct 状态 {
    队: 队列,
    停: Arc<AtomicBool>,
    线程: Option<std::thread::JoinHandle<()>>,
    已停: bool,
    /// 用户给的处理函数（在解释器线程上调）。
    处理: Val,
    控制: 控制,
}

fn 借(对象: &std::rc::Rc<crate::NativeObj>) -> std::cell::Ref<'_, 状态> {
    std::cell::Ref::map(对象.state.borrow(), |s| {
        s.downcast_ref::<状态>().expect("NativeObj 应是监视句柄")
    })
}

fn 借改(对象: &std::rc::Rc<crate::NativeObj>) -> std::cell::RefMut<'_, 状态> {
    std::cell::RefMut::map(对象.state.borrow_mut(), |s| {
        s.downcast_mut::<状态>().expect("NativeObj 应是监视句柄")
    })
}

fn 可调用(v: &Val) -> bool {
    matches!(
        v,
        Val::Func(_)
            | Val::WalkFn(_)
            | Val::Builtin(_)
            | Val::BoundMethod(_)
            | Val::UserMethod { .. }
    )
}

// ---------------------------------------------------------------------------
// 路径.监视
// ---------------------------------------------------------------------------

pub(crate) fn 监视(args: &[Val], _vm: &mut dyn BuiltinHost) -> Result<Val, JishiError> {
    if args.len() < 2 || args.len() > 3 {
        return Err(err(
            "类型错误",
            format!(
                "「监视」要 2 到 3 个参数（目录, 处理函数, 选项=空），实际给了 {} 个",
                args.len()
            ),
        ));
    }
    let 目录 = match &args[0] {
        Val::Str(s) => s.to_string(),
        other => {
            return Err(err(
                "类型错误",
                format!(
                    "「监视」的第一个参数要目录路径（文本），现在是「{}」",
                    crate::type_label(other)
                ),
            ))
        }
    };
    // 回调**现在就验**，别等第一条事件才发现它不是函数 —— 那时程序早跑起来了，
    // 报错位置跟用户写的那行差着十万八千里。
    let 处理 = args[1].clone();
    if !可调用(&处理) {
        return Err(err(
            "类型错误",
            format!(
                "「监视」的第二个参数要一个**函数**（现在是「{}」）",
                crate::type_label(&处理)
            ),
        ));
    }
    let 递归 = match args.get(2) {
        None | Some(Val::None_) => false,
        Some(Val::Dict(m)) => {
            // 只认一个键「递归」；多给的键**不报错也不理**（选项以后会加，
            // 现在不许因为它们没实现就让已经写好的程序炸）
            let 值 = m
                .borrow()
                .iter()
                .find(|(k, _)| matches!(k, Val::Str(s) if &**s == "递归"))
                .map(|(_, v)| v.clone());
            match 值 {
                None => false,
                Some(Val::Bool(b)) => b,
                Some(other) => {
                    return Err(err(
                        "类型错误",
                        format!(
                            "选项里的「递归」要 真/假，现在是「{}」",
                            crate::type_label(&other)
                        ),
                    ))
                }
            }
        }
        Some(other) => {
            return Err(err(
                "类型错误",
                format!(
                    "「监视」的第三个参数要字典（比如 {{\"递归\": 真}}），现在是「{}」",
                    crate::type_label(other)
                ),
            ))
        }
    };

    let 根 = std::path::Path::new(&目录);
    if !根.exists() {
        return Err(err_with_hint(
            "文件错误",
            format!("要监视的目录「{目录}」不存在"),
            "监视只能给**目录**；相对路径是相对当前工作目录的".into(),
        ));
    }
    if !根.is_dir() {
        return Err(err_with_hint(
            "文件错误",
            format!("「{目录}」不是目录（是文件），监视只能给目录"),
            "要盯一个文件就监视它的**父目录**，然后在回调里比 `项[\"路径\"]`".into(),
        ));
    }

    let 队 = 新队列();
    let 停 = Arc::new(AtomicBool::new(false));
    let (控制, 线程) = plat::启动(&目录, 递归, &队, &停)?;

    Ok(crate::native_obj(
        类型名,
        方法们,
        std::rc::Rc::new(std::cell::RefCell::new(状态 {
            队,
            停,
            线程: Some(线程),
            已停: false,
            处理,
            控制,
        })),
    ))
}

// ---------------------------------------------------------------------------
// 路径.等事件
// ---------------------------------------------------------------------------

/// 超时（**秒**，整数/小数都行）→ 毫秒。`空` = 一直等（返回 `None`）。
///
/// 🔴 **整数字面量也是秒**：`等事件(表, 5)` 是 5 **秒**，不是 5 毫秒。
/// （2026-11-26 踩过：这里曾把整数当毫秒、小数乘 1000，于是 `等事件(表, 5)`
///   只等 5ms 就返回 `空` —— 表现为「跨进程事件一条都收不到」，查了半天。）
fn 超时毫秒(v: Option<&Val>) -> Result<Option<u32>, JishiError> {
    match v {
        None | Some(Val::None_) => Ok(None),
        Some(值) => {
            let 秒 = crate::stdlib::as_num(值)
                .map_err(|_| err("类型错误", "超时要给秒数（省略或给 `空` = 一直等）"))?;
            if 秒 < 0.0 {
                return Err(err_with_hint(
                    "值错误",
                    format!("超时不能是负数（给的是 {秒}）"),
                    "要「马上看一眼」就给 0；要一直等就省略这个参数".into(),
                ));
            }
            Ok(Some((秒 * 1000.0).min(u32::MAX as f64) as u32))
        }
    }
}

pub(crate) fn 等事件(
    对象: &std::rc::Rc<crate::NativeObj>,
    args: &[Val],
    vm: &mut dyn BuiltinHost,
) -> Result<Val, JishiError> {
    if args.len() > 1 {
        return Err(err(
            "类型错误",
            format!("「等事件」最多 1 个参数（超时，秒），实际给了 {} 个", args.len()),
        ));
    }
    let 限 = 超时毫秒(args.first())?;
    let 期限 = 限.map(|ms| std::time::Instant::now() + std::time::Duration::from_millis(ms as u64));

    let (队, 停, 处理) = {
        let st = 借(对象);
        st.活着()?;
        (st.队.clone(), st.停.clone(), st.处理.clone())
    };

    // ① 取一批 —— **不持锁回调**（回调里可能调 `停止监视`，那要同一把锁）
    //
    // 🔴 **这里用「短间隔轮询」而不是 `Condvar`**（2026-11-26 实测换的）：
    //    用条件变量等的时候，后台线程那条 `ReadDirectoryChangesW` 在 Windows 上
    //    **迟迟不返回**（别的进程刚写完文件也不返回），而主线程只要在睡眠 / 不等待，
    //    同一份代码就正常收事件。轮询把这件事变简单可预测：最多 5ms 延迟，
    //    代价是一次 wake-up —— 文件监视这点频率完全无所谓。
    let 批 = {
        let (锁, _) = &*队;
        loop {
            {
                let mut q = 锁.lock().unwrap();
                if !q.is_empty() {
                    break q.drain(..).collect::<Vec<事件>>();
                }
            }
            if 停.load(Ordering::Relaxed) {
                return Ok(Val::Int(crate::jint::JInt::new(0)));   // 停了：没有更多事件
            }
            if let Some(t) = 期限 {
                if std::time::Instant::now() >= t {
                    return Ok(Val::None_);            // 超时 = 这一拍没东西，不是错误
                }
            }
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
    };

    // ② 逐条派发（此刻没有锁）
    let 条数 = 批.len();
    for e in 批 {
        vm.call_value(处理.clone(), vec![事件字典(&e)])?;
    }
    Ok(Val::Int(crate::jint::JInt::new(条数 as i128)))
}

fn 事件字典(e: &事件) -> Val {
    let mut 对 = vec![
        (Val::Str("路径".into()), Val::Str(e.路径.clone().into())),
        (Val::Str("动作".into()), Val::Str(e.动作.into())),
    ];
    if let Some(旧) = &e.旧路径 {
        对.push((Val::Str("旧路径".into()), Val::Str(旧.clone().into())));
    }
    crate::dict_new(对)
}

// ---------------------------------------------------------------------------
// 路径.停止监视
// ---------------------------------------------------------------------------

pub(crate) fn 停止(
    对象: &std::rc::Rc<crate::NativeObj>,
    _args: &[Val],
    _vm: &mut dyn BuiltinHost,
) -> Result<Val, JishiError> {
    let (线程, 控制) = {
        let mut st = 借改(对象);
        if st.已停 {
            return Ok(Val::Bool(false));      // 幂等：重复停不报错，如实说「本来就停了」
        }
        st.已停 = true;
        st.停.store(true, Ordering::Relaxed);
        {
            let (_, 信号) = &*st.队;
            信号.notify_all();                 // 叫醒正在 `等事件` 里等的人
        }
        (st.线程.take(), st.控制.clone())
    };
    plat::叫醒(&控制);                         // 让后台线程从阻塞里醒过来
    if let Some(t) = 线程 {
        let _ = t.join();
    }
    Ok(Val::Bool(true))
}

impl 状态 {
    fn 活着(&self) -> Result<(), JishiError> {
        if self.已停 {
            // 停了之后 `等事件` 要能正常返回 0（好让用户的循环自己退出），
            // 所以这里**不报错** —— 位置留着，将来要加"停止后不能再取"再说。
        }
        Ok(())
    }
}

fn 认句柄(v: &Val, 名: &str) -> Result<std::rc::Rc<crate::NativeObj>, JishiError> {
    match v {
        Val::Native(o) if o.type_name == 类型名 => Ok(o.clone()),
        _ => Err(err(
            "类型错误",
            format!(
                "「{名}」要传 `路径.监视` 拿到的句柄（现在是「{}」）",
                crate::type_label(v)
            ),
        )),
    }
}

/// `路径.等事件(句柄, 超时=空)` —— 句柄在前，其余照抄句柄方法。
pub(crate) fn 模块等事件(args: &[Val], vm: &mut dyn BuiltinHost) -> Result<Val, JishiError> {
    if args.is_empty() || args.len() > 2 {
        return Err(err(
            "类型错误",
            format!(
                "「等事件」要 1 到 2 个参数（句柄, 超时=空），实际给了 {} 个",
                args.len()
            ),
        ));
    }
    let 对象 = 认句柄(&args[0], "等事件")?;
    等事件(&对象, &args[1..], vm)
}

/// `路径.停止监视(句柄)`
pub(crate) fn 模块停止(args: &[Val], vm: &mut dyn BuiltinHost) -> Result<Val, JishiError> {
    if args.len() != 1 {
        return Err(err(
            "类型错误",
            format!("「停止监视」要 1 个参数（句柄），实际给了 {} 个", args.len()),
        ));
    }
    let 对象 = 认句柄(&args[0], "停止监视")?;
    停止(&对象, &[], vm)
}

/// 方法表（句柄上的两个动作）。`路径.等事件(表)` / `表.等事件()` 两种写法都通。
static 方法们: &[(&str, crate::NativeFn)] = &[("等事件", 等事件), ("停止", 停止)];

// ---------------------------------------------------------------------------
// 平台：Windows
// ---------------------------------------------------------------------------

#[cfg(windows)]
mod plat {
    use super::*;

    type Handle = isize;

    /// 控制块：**目录句柄在线程里打开**，这里只放「打断用的线程句柄」。
    ///
    /// ⚠️ 为什么句柄要在后台线程里打开：本机实测（2026-11-26）在**主线程**开
    /// 目录句柄、拿去**子线程**读，`ReadDirectoryChangesW` 收不到别的进程写文件
    /// （同一个目录、同样的参数，独立小程序在线程里开就能收到）。把开句柄挪进
    /// 线程之后一切正常 —— 这条差异记在这里，免得以后有人"顺手"挪回主线程。
    #[derive(Clone)]
    pub(crate) struct 控制 {
        线程: std::sync::Arc<std::sync::Mutex<Handle>>,
    }

    const FILE_LIST_DIRECTORY: u32 = 1;
    const FILE_SHARE_ALL: u32 = 0x7;
    const OPEN_EXISTING: u32 = 3;
    /// 打开**目录**必须有这一位（`FILE_FLAG_BACKUP_SEMANTICS`）
    const FILE_FLAG_BACKUP_SEMANTICS: u32 = 0x0200_0000;
    const DUPLICATE_SAME_ACCESS: u32 = 2;
    /// 名字变 / 目录名变 / 大小变 / 最后写时间变 —— 覆盖「增删改名 + 内容改」
    const 过滤: u32 = 0x1 | 0x2 | 0x8 | 0x10;
    const INVALID: Handle = -1;

    #[repr(C)]
    struct NotifyInfo {
        next: u32,
        action: u32,
        name_len: u32,
        name: [u16; 1],
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn CreateFileW(路径: *const u16, 访问: u32, 共享: u32, 安全: *mut std::ffi::c_void,
                       创建: u32, 标志: u32, 模板: Handle) -> Handle;
        fn ReadDirectoryChangesW(句柄: Handle, 缓冲: *mut std::ffi::c_void, 长: u32,
                                 递归: i32, 过滤模式: u32, 字节: *mut u32,
                                 重叠: *mut std::ffi::c_void,
                                 完成例程: *mut std::ffi::c_void) -> i32;
        /// 🔴 打断另一个线程里**阻塞的同步 IO** 的正规手段。
        /// 从外面 `CloseHandle` 目录句柄**不管用** —— 阻塞中的
        /// `ReadDirectoryChangesW` 不会因此返回，`停止监视` 就永远等下去。
        fn CancelSynchronousIo(线程: Handle) -> i32;
        fn GetCurrentThread() -> Handle;
        fn GetCurrentProcess() -> Handle;
        fn DuplicateHandle(源进程: Handle, 源: Handle, 目标进程: Handle,
                           结果: *mut Handle, 权限: u32, 可继承: i32, 选项: u32) -> i32;
        fn CloseHandle(句柄: Handle) -> i32;
    }

    pub(crate) fn 叫醒(c: &控制) {
        let h = *c.线程.lock().unwrap();
        if h != 0 {
            unsafe {
                CancelSynchronousIo(h);
            }
        }
    }

    pub(crate) fn 启动(
        目录: &str,
        递归: bool,
        队: &队列,
        停: &Arc<AtomicBool>,
    ) -> Result<(控制, std::thread::JoinHandle<()>), JishiError> {
        let 控制 = 控制 {
            线程: std::sync::Arc::new(std::sync::Mutex::new(0)),
        };
        let 控制2 = 控制.clone();
        let 目录2 = 目录.to_string();
        let 队2 = 队.clone();
        let 停2 = 停.clone();
        // 「线程起来了没有、开句柄成不成」要在 `监视()` 这一行就报出来，
        // 所以用一个握手：线程把结果塞进去，主线程等它（最多 5 秒）。
        let 就绪: Arc<(Mutex<Option<Result<(), JishiError>>>, Condvar)> =
            Arc::new((Mutex::new(None), Condvar::new()));
        let 就绪2 = 就绪.clone();
        let 线程 = std::thread::spawn(move || 循环(&控制2, &目录2, 递归, &队2, &停2, &就绪2));
        {
            let (锁, 信号) = &*就绪;
            let mut g = 锁.lock().unwrap();
            let 期限 = std::time::Instant::now() + std::time::Duration::from_secs(5);
            while g.is_none() {
                let 剩 = 期限.saturating_duration_since(std::time::Instant::now());
                if 剩.is_zero() {
                    break;
                }
                let (ng, _) = 信号.wait_timeout(g, 剩).unwrap();
                g = ng;
            }
        }
        let 结果 = {
            let mut g = 就绪.0.lock().unwrap();
            g.take()
        };
        match 结果 {
            Some(Ok(())) => Ok((控制, 线程)),
            Some(Err(e)) => Err(e),
            None => Err(err("运行期错误", "监视线程没起来（等了 5 秒没有回应）")),
        }
    }

    /// 后台线程：**同步** `ReadDirectoryChangesW` 一直读，收一批推一批。
    ///
    /// 🔴 为什么是同步而不是 OVERLAPPED：异步那套要么得等 `ERROR_IO_PENDING`、
    /// 要么得管事件对象，出错面大；「停止」用 `CancelSynchronousIo` 一行就解决。
    fn 循环(
        控制: &控制,
        目录: &str,
        递归: bool,
        队: &队列,
        停: &Arc<AtomicBool>,
        就绪: &Arc<(Mutex<Option<Result<(), JishiError>>>, Condvar)>,
    ) {
        // 复制一份自己的线程句柄给「停止」用（`GetCurrentThread()` 是伪句柄，
        // 只在本线程有效，跨线程用必须复制）
        let mut 我: Handle = 0;
        unsafe {
            DuplicateHandle(
                GetCurrentProcess(),
                GetCurrentThread(),
                GetCurrentProcess(),
                &mut 我,
                0,
                0,
                DUPLICATE_SAME_ACCESS,
            );
        }
        *控制.线程.lock().unwrap() = 我;
        let h = 开目录(目录);
        // 握手：把「成不成」交回去
        {
            let (锁, 信号) = &**就绪;
            let mut g = 锁.lock().unwrap();
            *g = Some(if h == INVALID {
                Err(打不开(目录))
            } else {
                Ok(())
            });
            信号.notify_all();
        }
        if h == INVALID {
            if 我 != 0 {
                unsafe {
                    CloseHandle(我);
                }
            }
            *控制.线程.lock().unwrap() = 0;
            return;
        }

        let mut 缓冲 = vec![0u8; 64 * 1024];
        while !停.load(Ordering::Relaxed) {
            let mut 读到: u32 = 0;
            let ok = unsafe {
                ReadDirectoryChangesW(
                    h,
                    缓冲.as_mut_ptr() as *mut std::ffi::c_void,
                    缓冲.len() as u32,
                    if 递归 { 1 } else { 0 },
                    过滤,
                    &mut 读到,
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                )
            };
            if 停.load(Ordering::Relaxed) {
                break;                                   // 被 `CancelSynchronousIo` 打断的
            }
            if ok == 0 {
                break;                                   // 目录没了 / 真出错
            }
            if 读到 == 0 {
                // 🔴 **缓冲区溢出**：Windows 用「成功 + 0 字节」表示"事件太多，我丢了"。
                //    不报的话用户会以为「文件没变过」—— 那是最坏的静默失败。
                推(队, 事件 { 动作: "溢出", 路径: 目录.to_string(), 旧路径: None });
                continue;
            }
            解析(缓冲.as_ptr(), 读到 as usize, 目录, 队);
        }
        unsafe {
            if 我 != 0 {
                CloseHandle(我);
            }
            CloseHandle(h);
        }
        *控制.线程.lock().unwrap() = 0;
    }

    fn 开目录(目录: &str) -> Handle {
        let 宽: Vec<u16> = 目录.encode_utf16().chain(std::iter::once(0)).collect();
        unsafe {
            CreateFileW(
                宽.as_ptr(),
                FILE_LIST_DIRECTORY,
                FILE_SHARE_ALL,
                std::ptr::null_mut(),
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS,
                INVALID,
            )
        }
    }

    fn 打不开(目录: &str) -> JishiError {
        let 码 = std::io::Error::last_os_error().raw_os_error().unwrap_or(0);
        err_with_hint(
            "文件错误",
            format!("打不开目录「{目录}」（系统错误码 {码}）"),
            "检查路径是否存在、有没有读权限".into(),
        )
    }

    fn 全路径(目录: &str, 名: &[u16]) -> String {
        let 名文 = String::from_utf16_lossy(名);
        if 名文.is_empty() {
            目录.to_string()
        } else {
            format!("{}\\{}", 目录.trim_end_matches('\\'), 名文)
        }
    }

    fn 解析(buf: *const u8, 长: usize, 目录: &str, 队: &队列) {
        let mut 偏移 = 0usize;
        // 改名是**两条挨着的记录**（OLD 然后 NEW）—— 头一条只记下来，配对后再报一条
        let mut 待改旧: Option<String> = None;
        while 偏移 + 12 <= 长 {
            let info = unsafe { &*(buf.add(偏移) as *const NotifyInfo) };
            let 名字节 = info.name_len as usize;
            if 偏移 + 12 + 名字节 > 长 {
                break;
            }
            let 名: Vec<u16> = unsafe {
                std::slice::from_raw_parts(buf.add(偏移 + 12) as *const u16, 名字节 / 2)
            }
            .to_vec();
            match info.action {
                1 => 推(队, 事件 { 动作: "增", 路径: 全路径(目录, &名), 旧路径: None }),
                2 => 推(队, 事件 { 动作: "删", 路径: 全路径(目录, &名), 旧路径: None }),
                3 => 推(队, 事件 { 动作: "改", 路径: 全路径(目录, &名), 旧路径: None }),
                4 => 待改旧 = Some(全路径(目录, &名)),
                5 => {
                    let 旧 = 待改旧.take();
                    推(队, 事件 { 动作: "改名", 路径: 全路径(目录, &名), 旧路径: 旧 });
                }
                _ => {}
            }
            if info.next == 0 {
                break;
            }
            偏移 += info.next as usize;
        }
    }
}

// ---------------------------------------------------------------------------
// 平台：Linux（inotify）
// ---------------------------------------------------------------------------

#[cfg(target_os = "linux")]
mod plat {
    use super::*;

    /// inotify 的 fd
    pub(crate) type 控制 = i32;

    /// `IN_NONBLOCK | IN_CLOEXEC`（数值同 `O_NONBLOCK | O_CLOEXEC`）
    const IN_NONBLOCK: i32 = 0o4000;
    const IN_CLOEXEC: i32 = 0o2_000_000;

    const IN_CLOSE_WRITE: u32 = 0x8;
    const IN_MOVED_FROM: u32 = 0x40;
    const IN_MOVED_TO: u32 = 0x80;
    const IN_CREATE: u32 = 0x100;
    const IN_DELETE: u32 = 0x200;
    const IN_DELETE_SELF: u32 = 0x400;
    const IN_MOVE_SELF: u32 = 0x800;
    const IN_IGNORED: u32 = 0x8000;
    const IN_ISDIR: u32 = 0x4000_0000;

    /// 只看这几样：写完了 / 建 / 删 / 移入 / 移出。
    /// 🔴 **不用 `IN_MODIFY`**：一次保存会触发好几次（写头部、写尾部、flush），
    /// 用户看到的是同一秒里好几条「改」。`IN_CLOSE_WRITE` 是"写完关文件"，一次一条。
    const 掩码: u32 = IN_CLOSE_WRITE | IN_CREATE | IN_DELETE | IN_MOVED_FROM | IN_MOVED_TO
        | IN_DELETE_SELF | IN_MOVE_SELF | 0x0100_0000;  // 最后一位 = IN_ONLYDIR

    #[repr(C)]
    struct 事件头 {
        wd: i32,
        mask: u32,
        cookie: u32,
        len: u32,
    }

    #[repr(C)]
    struct PollFd {
        fd: i32,
        events: i16,
        revents: i16,
    }
    const POLLIN: i16 = 1;

    extern "C" {
        fn inotify_init1(标志: i32) -> i32;
        fn inotify_add_watch(fd: i32, 路径: *const u8, 掩码: u32) -> i32;
        fn poll(fds: *mut PollFd, 个数: u64, 等毫秒: i32) -> i32;
    }

    /// 关掉裸 fd（**不声明 `close` 符号** —— unix 上它是 std 运行时符号，
    /// 自己定义会直接报错；让 `File` 的析构去关）。
    fn 关闭fd(fd: i32) {
        use std::os::unix::io::FromRawFd;
        unsafe {
            drop(std::fs::File::from_raw_fd(fd));
        }
    }

    /// 用 std 的 `File` 包一层裸 fd 来读 —— **不自己声明 `read` 这个符号**
    /// （unix 上会撞标准库运行时那份，CI 直接报错；见 `串口` 里同一段注释）。
    fn 读字节(fd: i32, 缓冲: &mut [u8]) -> std::io::Result<usize> {
        use std::io::Read;
        use std::mem::ManuallyDrop;
        use std::os::unix::io::FromRawFd;
        let 文件 = ManuallyDrop::new(unsafe { std::fs::File::from_raw_fd(fd) });
        let mut 借: &std::fs::File = &文件;
        借.read(缓冲)
    }

    pub(crate) fn 叫醒(_fd: &控制) {
        // 与 Windows 同一个口径：**fd 只归线程管**，线程每 200ms 看一次 `停`。
        // 从外面 `close` 一个正在 `poll` 的 fd 不是好主意（fd 号可能立刻被别处复用）。
    }

    pub(crate) fn 启动(
        目录: &str,
        递归: bool,
        队: &队列,
        停: &Arc<AtomicBool>,
    ) -> Result<(控制, std::thread::JoinHandle<()>), JishiError> {
        let fd = unsafe { inotify_init1(IN_NONBLOCK | IN_CLOEXEC) };
        if fd < 0 {
            let 码 = std::io::Error::last_os_error().raw_os_error().unwrap_or(0);
            return Err(err("运行期错误", format!("inotify 起不来（系统错误码 {码}）")));
        }
        let mut 表: std::collections::HashMap<i32, String> = std::collections::HashMap::new();
        if 加一层(fd, 目录, &mut 表).is_err() {
            关闭fd(fd);
            return Err(err_with_hint(
                "文件错误",
                format!("加不上监视「{目录}」"),
                "Linux 上单进程 watch 数量有上限（`/proc/sys/fs/inotify/max_user_watches`）；\
                 递归监视大目录可能撞到，调大它或缩小范围"
                    .into(),
            ));
        }
        if 递归 {
            遍历加(fd, 目录, &mut 表, 0);
        }
        let 队2 = 队.clone();
        let 停2 = 停.clone();
        let 线程 = std::thread::spawn(move || 循环(fd, 表, &队2, &停2));
        Ok((fd, 线程))
    }

    fn 加一层(
        fd: i32,
        目录: &str,
        表: &mut std::collections::HashMap<i32, String>,
    ) -> Result<(), ()> {
        let c = std::ffi::CString::new(目录).map_err(|_| ())?;
        let wd = unsafe { inotify_add_watch(fd, c.as_ptr() as *const u8, 掩码) };
        if wd < 0 {
            return Err(());
        }
        表.insert(wd, 目录.trim_end_matches('/').to_string());
        Ok(())
    }

    /// 递归加 watch。深度上限 24：防符号链接成环 / 病态目录结构（正常项目远不到）。
    fn 遍历加(fd: i32, 目录: &str, 表: &mut std::collections::HashMap<i32, String>, 深: usize) {
        if 深 > 24 {
            return;
        }
        let Ok(项们) = std::fs::read_dir(目录) else { return };
        for 项 in 项们.flatten() {
            if 项.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                let 子 = 项.path().to_string_lossy().to_string();
                let _ = 加一层(fd, &子, 表);
                遍历加(fd, &子, 表, 深 + 1);
            }
        }
    }

    fn 循环(
        fd: i32,
        mut 表: std::collections::HashMap<i32, String>,
        队: &队列,
        停: &Arc<AtomicBool>,
    ) {
        let mut 缓冲 = vec![0u8; 64 * 1024];
        // 改名配对：cookie → 旧路径（同一批里 FROM/TO 成对出现）
        let mut 待改: std::collections::HashMap<u32, String> = std::collections::HashMap::new();
        loop {
            if 停.load(Ordering::Relaxed) {
                关闭fd(fd);                       // fd 由线程自己关（见 `叫醒` 的说明）
                return;
            }
            let mut p = PollFd { fd, events: POLLIN, revents: 0 };
            let rc = unsafe { poll(&mut p, 1, 200) };
            if rc < 0 {
                if 最后错误() == 4 {
                    continue;                                  // EINTR
                }
                关闭fd(fd);
                return;
            }
            if rc == 0 {
                continue;                                      // 200ms 没动静，继续等
            }
            let (n, 码) = match 读字节(fd, &mut 缓冲) {
                Ok(n) => (n as isize, 0),
                Err(e) => (-1, e.raw_os_error().unwrap_or(0)),
            };
            if n <= 0 {
                if 码 == 11 || 码 == 4 {
                    continue;                                  // EAGAIN / EINTR
                }
                关闭fd(fd);
                return;                                        // fd 坏了（被外面关了）
            }
            let 长 = n as usize;
            let mut 偏移 = 0usize;
            while 偏移 + std::mem::size_of::<事件头>() <= 长 {
                let 头 = unsafe { &*(缓冲.as_ptr().add(偏移) as *const 事件头) };
                let 名起 = 偏移 + std::mem::size_of::<事件头>();
                let 名长 = 头.len as usize;
                if 名起 + 名长 > 长 {
                    break;
                }
                let 名 = if 名长 == 0 {
                    String::new()
                } else {
                    let 片 = &缓冲[名起..名起 + 名长];
                    let 止 = 片.iter().position(|b| *b == 0).unwrap_or(片.len());
                    String::from_utf8_lossy(&片[..止]).to_string()
                };
                let 基 = 表.get(&头.wd).cloned().unwrap_or_default();
                let 全 = if 名.is_empty() {
                    基.clone()
                } else {
                    format!("{}/{}", 基.trim_end_matches('/'), 名)
                };

                // 新建目录 ⇒ **补 watch**。不补的话递归监视在新建子目录处就断了，
                // 用户会以为「新建目录里改文件不通知」是 bug —— 这是 inotify 最常踩的坑。
                if 头.mask & IN_CREATE != 0 && 头.mask & IN_ISDIR != 0 && !基.is_empty() {
                    let _ = 加一层(fd, &全, &mut 表);
                    遍历加(fd, &全, &mut 表, 0);
                }
                if 头.mask & IN_IGNORED != 0 {
                    表.remove(&头.wd);
                }

                if 头.mask & IN_CLOSE_WRITE != 0 {
                    推(队, 事件 { 动作: "改", 路径: 全.clone(), 旧路径: None });
                }
                if 头.mask & IN_CREATE != 0 {
                    推(队, 事件 { 动作: "增", 路径: 全.clone(), 旧路径: None });
                }
                if 头.mask & IN_DELETE != 0 {
                    推(队, 事件 { 动作: "删", 路径: 全.clone(), 旧路径: None });
                }
                if 头.mask & IN_MOVED_FROM != 0 {
                    待改.insert(头.cookie, 全.clone());
                }
                if 头.mask & IN_MOVED_TO != 0 {
                    match 待改.remove(&头.cookie) {
                        Some(旧) => 推(队, 事件 { 动作: "改名", 路径: 全.clone(), 旧路径: Some(旧) }),
                        None => 推(队, 事件 { 动作: "增", 路径: 全.clone(), 旧路径: None }),
                    }
                }
                if 头.mask & (IN_DELETE_SELF | IN_MOVE_SELF) != 0 {
                    推(队, 事件 { 动作: "删", 路径: 全.clone(), 旧路径: None });
                }
                偏移 = 名起 + 名长;
            }
            // 没配对上的 FROM（跨批了）最多留 2 秒，之后清掉，别让表无限长
            if 待改.len() > 1024 {
                待改.clear();
            }
        }
    }

    fn 最后错误() -> i32 {
        std::io::Error::last_os_error().raw_os_error().unwrap_or(0)
    }
}

// ---------------------------------------------------------------------------
// 平台：macOS（FSEvents）
// ---------------------------------------------------------------------------

#[cfg(target_os = "macos")]
mod plat {
    use super::*;

    /// FSEvents 的流 + 跑它的那个 run loop（停止时两个都要用）。
    #[derive(Clone, Copy)]
    pub(crate) struct 控制 {
        流: usize,
        环: usize,
    }

    const UTF8: u32 = 0x0800_0100;
    /// `kFSEventStreamCreateFlagUseCFTypes | kFSEventStreamCreateFlagNoDefer
    /// | kFSEventStreamCreateFlagFileEvents`
    ///
    /// 🔴 **`UseCFTypes`(0x1) 不能少**：不开它，回调里的 `eventPaths` 是
    /// `char **`（C 字符串数组），而我们是按 `CFArray` 取元素的 ——
    /// 取第一个就把引擎**直接 SIGSEGV**（2026-11-26 macOS CI 上就是这样崩的，
    /// 而且崩得没输出，只剩 exit -11）。
    const 标志: u32 = 0x1 | 0x2 | 0x10;
    /// `kFSEventStreamEventIdSinceNow`
    const 从此刻: u64 = u64::MAX;
    const 延迟: f64 = 0.1;

    const 项新建: u32 = 0x100;
    const 项移除: u32 = 0x200;
    const 项改名: u32 = 0x800;
    const 项修改: u32 = 0x1000;

    #[repr(C)]
    struct 流上下文 {
        version: i64,
        info: *mut std::ffi::c_void,
        retain: Option<unsafe extern "C" fn(*const std::ffi::c_void) -> *const std::ffi::c_void>,
        release: Option<unsafe extern "C" fn(*const std::ffi::c_void)>,
        copy_description:
            Option<unsafe extern "C" fn(*const std::ffi::c_void) -> *const std::ffi::c_void>,
    }

    /// 回调里需要的东西：队列 + 根目录（非递归时按深度过滤）。
    struct 现场 {
        队: 队列,
        根: String,
        递归: bool,
    }

    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFStringCreateWithCString(alloc: *const std::ffi::c_void, s: *const u8, enc: u32)
            -> *const std::ffi::c_void;
        fn CFArrayCreate(alloc: *const std::ffi::c_void, values: *const *const std::ffi::c_void,
                         num: i64, callbacks: *const std::ffi::c_void) -> *const std::ffi::c_void;
        fn CFArrayGetValueAtIndex(arr: *const std::ffi::c_void, i: i64) -> *const std::ffi::c_void;
        fn CFStringGetLength(s: *const std::ffi::c_void) -> i64;
        fn CFStringGetMaximumSizeForEncoding(n: i64, enc: u32) -> i64;
        fn CFStringGetCString(s: *const std::ffi::c_void, buf: *mut u8, size: i64, enc: u32) -> u8;
        fn CFRelease(cf: *const std::ffi::c_void);
        fn CFRunLoopGetCurrent() -> *mut std::ffi::c_void;
        fn CFRunLoopRun();
        fn CFRunLoopStop(rl: *mut std::ffi::c_void);
        static kCFRunLoopDefaultMode: *const std::ffi::c_void;
        /// `CFArrayCallBacks` 结构体（我们只把它的地址传进去，不当结构体读）
        static kCFTypeArrayCallBacks: u8;
    }

    #[link(name = "CoreServices", kind = "framework")]
    extern "C" {
        fn FSEventStreamCreate(
            alloc: *const std::ffi::c_void,
            callback: extern "C" fn(*const std::ffi::c_void, *mut std::ffi::c_void, usize,
                                     *mut std::ffi::c_void, *const u32, *const u64),
            context: *const 流上下文,
            paths: *const std::ffi::c_void,
            since: u64,
            latency: f64,
            flags: u32,
        ) -> *mut std::ffi::c_void;
        fn FSEventStreamScheduleWithRunLoop(stream: *mut std::ffi::c_void,
                                            rl: *mut std::ffi::c_void,
                                            mode: *const std::ffi::c_void);
        fn FSEventStreamStart(stream: *mut std::ffi::c_void) -> u8;
        fn FSEventStreamStop(stream: *mut std::ffi::c_void);
        fn FSEventStreamInvalidate(stream: *mut std::ffi::c_void);
        fn FSEventStreamRelease(stream: *mut std::ffi::c_void);
    }

    extern "C" fn 回调(
        _流: *const std::ffi::c_void,
        info: *mut std::ffi::c_void,
        事件数: usize,
        路径们: *mut std::ffi::c_void,
        flags: *const u32,
        _ids: *const u64,
    ) {
        if info.is_null() || 路径们.is_null() || flags.is_null() {
            return;
        }
        let 现场 = unsafe { &*(info as *const 现场) };
        for i in 0..事件数 as i64 {
            let 路径 = unsafe { 取文本(CFArrayGetValueAtIndex(路径们, i)) };
            let f = unsafe { *flags.add(i as usize) };
            let 动作 = if f & 项新建 != 0 {
                "增"
            } else if f & 项移除 != 0 {
                "删"
            } else if f & 项改名 != 0 {
                "改名"
            } else if f & 项修改 != 0 {
                "改"
            } else {
                continue;
            };
            if !现场.递归 && 相对深度(&现场.根, &路径) > 1 {
                continue;                                  // 非递归：只报第一层
            }
            推(&现场.队, 事件 { 动作, 路径, 旧路径: None });
        }
    }

    /// 相对根目录的深度：直接子项 = 1，孙辈 = 2 …（根自己 = 0）
    fn 相对深度(根: &str, 路径: &str) -> usize {
        let 根 = 根.trim_end_matches('/');
        match 路径.strip_prefix(根) {
            Some(剩) => 剩.trim_start_matches('/').split('/').filter(|s| !s.is_empty()).count(),
            None => 99,                                    // 不在根下面（理论上不会）
        }
    }

    unsafe fn 取文本(cf: *const std::ffi::c_void) -> String {
        if cf.is_null() {
            return String::new();
        }
        let 长 = CFStringGetLength(cf);
        let 要 = CFStringGetMaximumSizeForEncoding(长, UTF8) + 1;
        let mut 缓 = vec![0u8; 要.max(1) as usize];
        if CFStringGetCString(cf, 缓.as_mut_ptr(), 要, UTF8) == 0 {
            return String::new();
        }
        let 止 = 缓.iter().position(|b| *b == 0).unwrap_or(缓.len());
        String::from_utf8_lossy(&缓[..止]).to_string()
    }

    pub(crate) fn 叫醒(c: &控制) {
        unsafe {
            FSEventStreamStop(c.流 as *mut std::ffi::c_void);
            FSEventStreamInvalidate(c.流 as *mut std::ffi::c_void);
            FSEventStreamRelease(c.流 as *mut std::ffi::c_void);
            CFRunLoopStop(c.环 as *mut std::ffi::c_void);
        }
    }

    pub(crate) fn 启动(
        目录: &str,
        递归: bool,
        队: &队列,
        停: &Arc<AtomicBool>,
    ) -> Result<(控制, std::thread::JoinHandle<()>), JishiError> {
        // FSEvents **天生递归**（监视一棵树）—— 非递归时在回调里按深度过滤。
        let 根 = 目录.trim_end_matches('/').to_string();
        let 队2 = 队.clone();
        let 停2 = 停.clone();
        // 流必须在**跑它的那个线程**里建（ScheduleWithRunLoop 用的是当前 run loop），
        // 建好之后把控制块交回来；建失败要在 `监视()` 那一行就报出来。
        let 收到: Arc<Mutex<Option<Result<控制, JishiError>>>> = Arc::new(Mutex::new(None));
        let 收2 = 收到.clone();
        let 就绪 = Arc::new((Mutex::new(false), Condvar::new()));
        let 就绪2 = 就绪.clone();
        let 线程 = std::thread::spawn(move || {
            // 🔴 上下文**故意不释放**（`Box::into_raw` 之后再也不 free）：
            //    FSEvents 在 `FSEventStreamInvalidate/Release` 期间**仍可能回调**，
            //    而那时我们已经在收尾线程上了 —— 释放了就是悬垂指针（崩在回调里，
            //    没有任何错误信息）。一个监视句柄泄漏几十字节，换掉一整类崩溃，值。
            let 现场 = Box::into_raw(Box::new(现场 { 队: 队2, 根, 递归 }));
            let 信息 = 现场 as *mut std::ffi::c_void;
            let mut 结果: Result<控制, JishiError> =
                Err(err("运行期错误", "FSEvents 没起来（不知道是哪一步）"));
            let 根文 = unsafe { (*现场).根.clone() };
            if let Ok(c) = std::ffi::CString::new(根文) {
                unsafe {
                    let cf = CFStringCreateWithCString(std::ptr::null(), c.as_ptr() as *const u8, UTF8);
                    if !cf.is_null() {
                        let 值: [*const std::ffi::c_void; 1] = [cf];
                        let arr = CFArrayCreate(
                            std::ptr::null(),
                            值.as_ptr(),
                            1,
                            &kCFTypeArrayCallBacks as *const u8 as *const std::ffi::c_void,
                        );
                        if !arr.is_null() {
                            let ctx = 流上下文 {
                                version: 0,
                                info: 信息,
                                retain: None,
                                release: None,
                                copy_description: None,
                            };
                            let 流 = FSEventStreamCreate(
                                std::ptr::null(), 回调, &ctx, arr, 从此刻, 延迟, 标志,
                            );
                            if !流.is_null() {
                                let 环 = CFRunLoopGetCurrent();
                                FSEventStreamScheduleWithRunLoop(流, 环, kCFRunLoopDefaultMode);
                                if FSEventStreamStart(流) != 0 {
                                    结果 = Ok(控制 { 流: 流 as usize, 环: 环 as usize });
                                } else {
                                    FSEventStreamInvalidate(流);
                                    FSEventStreamRelease(流);
                                    结果 = Err(err("运行期错误", "FSEvents 拒绝启动这个流"));
                                }
                            } else {
                                结果 = Err(err("运行期错误", "FSEventStreamCreate 返回空"));
                            }
                            CFRelease(arr);
                        } else {
                            结果 = Err(err("运行期错误", "CFArrayCreate 返回空"));
                        }
                        CFRelease(cf);
                    } else {
                        结果 = Err(err("运行期错误", "CFStringCreateWithCString 返回空"));
                    }
                }
            }
            *收2.lock().unwrap() = Some(结果);
            {
                let (锁, 信号) = &*就绪2;
                let mut g = 锁.lock().unwrap();
                *g = true;
                信号.notify_all();
            }
            if 停2.load(Ordering::Relaxed) {
                return;
            }
            unsafe { CFRunLoopRun() };       // 跑到 `叫醒` 里 CFRunLoopStop
            // 现场**不 drop**（见上面 `Box::into_raw` 那段说明）
        });
        // 等流建好（最多 5 秒）
        {
            let (锁, 信号) = &*就绪;
            let mut g = 锁.lock().unwrap();
            let 期限 = std::time::Instant::now() + std::time::Duration::from_secs(5);
            while !*g {
                let 剩 = 期限.saturating_duration_since(std::time::Instant::now());
                if 剩.is_zero() {
                    break;
                }
                let (ng, _) = 信号.wait_timeout(g, 剩).unwrap();
                g = ng;
            }
        }
        // ⚠️ 先把锁放了再 match：`收到的.lock().unwrap().take()` 直接写在 match 上，
        //    那把锁的临时值会活到整个 match 结束（借用检查器会拦）。
        let 结果 = {
            let mut g = 收到.lock().unwrap();
            g.take()
        };
        match 结果 {
            Some(Ok(c)) => Ok((c, 线程)),
            Some(Err(e)) => Err(e),
            None => Err(err("运行期错误", "FSEvents 起不来（等了 5 秒没有回应）")),
        }
    }
}

// ---------------------------------------------------------------------------
// 平台：wasm 与其它（报一句明确的错）
// ---------------------------------------------------------------------------

#[cfg(not(any(windows, target_os = "linux", target_os = "macos")))]
mod plat {
    use super::*;

    #[derive(Clone, Copy)]
    pub(crate) struct 控制;

    pub(crate) fn 叫醒(_c: &控制) {}

    pub(crate) fn 启动(
        _目录: &str,
        _递归: bool,
        _队: &队列,
        _停: &Arc<AtomicBool>,
    ) -> Result<(控制, std::thread::JoinHandle<()>), JishiError> {
        Err(err_with_hint(
            "运行期错误",
            "`路径.监视` 在浏览器里用不了（页面没有文件系统）",
            "文件监视要跑在桌面宿主（Windows / Linux / macOS 的 Rust 发行版）上".into(),
        ))
    }
}
