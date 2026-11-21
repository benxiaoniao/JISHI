//! 常驻 VM 的「帧循环登记表」（D110 / M186）。
//!
//! ## 这里只放三样东西
//!
//! 1. **谁在跑帧循环**（`初始化` / `每帧` 两个函数值）；
//! 2. **现在是不是在事件里**（决定 `图形.运行` 允不允许登记，D110 §三.2）；
//! 3. **画布的逻辑尺寸**（`图形.准备` 之后缓存下来，`宽度` / `高度` 直接读，
//!    不必每帧往宿主问一次）。
//!
//! ## 为什么用 `thread_local` 而不是让 VM 自己带
//!
//! `图形.运行` 是**内建函数**，手里只有 `&mut dyn BuiltinHost` —— 拿不到 `VM`，
//! 也就没法把「这两个函数」塞进 VM 的字段。而 `thread_local` 在 wasm 上等价于
//! 「这个实例的静态变量」（wasm 单线程），在原生宿主上也只影响当前线程。
//!
//! ⚠️ **函数值里握着 `Rc`**（`Val::Func(Rc<Func>)`），所以登记表与 VM 必须
//! 同生共死：`图形.停` / 页面重跑 / 刷新实例时都要 `清()`。谁也不许把这里的
//! 值搬去别的线程 —— 类型系统已经拦住了（`Rc` 不是 `Send`），这里只写一句让人别试。
//!
//! 📌 设计全文见 `docs/设计决策.md` **D110**。

use std::cell::{Cell, RefCell};
use std::collections::VecDeque;

use crate::{JishiError, Val};

/// 一次登记 = 谁在跑帧循环。
///
/// ⚠️ **`pub(crate)`**：里面装的是 `Val`（crate 内部类型）。wasm 那个 crate
/// 只该看到「跑一帧 / 调处理函数」这种**不提 `Val`** 的窄接口 —— 见文件末尾。
pub(crate) struct 登记 {
    /// 初始化函数（可以没有 —— 写个空函数也行，但允许 `空` 更省事）。
    pub 初始化: Option<Val>,
    /// 每帧函数：`每帧(帧号, 间隔毫秒)`。
    pub 每帧: Val,
}

thread_local! {
    static 登记表: RefCell<Option<登记>> = const { RefCell::new(None) };
    /// 当前这次执行是不是**事件回调**（`jishi_call` 进来）。
    ///
    /// 为什么要有它：D110 §三.2 规定 `图形.运行` 只能在**顶层**登记。
    /// 事件处理时顶层**已经重跑过一遍**（状态是刚重建的），那时再登记等于
    /// 让游戏从「跑了一半的程序」开始 —— 与其猜，不如报错说清。
    static 事件中: Cell<bool> = const { Cell::new(false) };
    /// 画布的逻辑尺寸（`(宽, 高)`），`图形.准备` 成功后填。
    static 画布: Cell<Option<(f64, f64)>> = const { Cell::new(None) };
}

/// 登记一个帧循环。**覆盖式**（调用方负责先查 `有()`）。
pub(crate) fn 登记(初始化: Option<Val>, 每帧: Val) {
    登记表.with(|t| *t.borrow_mut() = Some(登记 { 初始化, 每帧 }));
}

/// 取登记（克隆一份 `Val` —— 它们是 `Rc`，很便宜）。
pub(crate) fn 取() -> Option<(Option<Val>, Val)> {
    登记表.with(|t| t.borrow().as_ref().map(|r| (r.初始化.clone(), r.每帧.clone())))
}

/// 有没有在跑。
pub fn 有() -> bool {
    登记表.with(|t| t.borrow().is_some())
}

/// 清掉登记（`图形.停` / 出错 / 页面重跑）。
pub fn 清() {
    登记表.with(|t| *t.borrow_mut() = None);
}

/// 现在是不是在事件回调里。
pub fn 在事件里() -> bool {
    事件中.with(|c| c.get())
}

/// 记下画布的逻辑尺寸（`图形.准备` 返回时调）。
pub fn 记画布(宽: f64, 高: f64) {
    画布.with(|c| c.set(Some((宽, 高))));
}

/// 取画布的逻辑尺寸；没准备过就是 `None`。
pub fn 画布尺寸() -> Option<(f64, f64)> {
    画布.with(|c| c.get())
}

/// 忘掉画布尺寸（页面重跑 / 换画布）。
pub fn 清画布() {
    画布.with(|c| c.set(None));
}

/// 把「现在在事件里」做成一个 RAII 标记 —— 中途 `?` 早退也不会漏掉复位。
///
/// 📌 为什么需要：`jishi_call` 里那条路有好几个 `return`，手写 `进入事件()` /
/// `退出事件()` 迟早漏一个；漏了之后 `图形.运行` 会在**顶层**也报「不能在事件里
/// 登记」，那种 bug 极难查（现象是「第一次能跑，第二次不能」）。
///
/// ⚠️ 旧值存在**结构体里**，不存全局：嵌套两个守卫时，各自还原各自那一层，
/// 用一个共享格子会把外层还原成内层的值（这个坑写在这里，免得有人「优化」）。
pub struct 事件守卫 {
    旧: bool,
}

impl 事件守卫 {
    pub fn 新() -> Self {
        let 旧 = 事件中.with(|c| c.replace(true));
        Self { 旧 }
    }
}

impl Drop for 事件守卫 {
    fn drop(&mut self) {
        let 旧 = self.旧;
        事件中.with(|c| c.set(旧));
    }
}

// ---------------------------------------------------------------------------
// 帧耗时直方图（M208）
// ---------------------------------------------------------------------------
//
// ## 为什么要它
//
// 「这一版卡不卡」在真机上只有两种说法：「感觉还行」和「有时候一顿」。前者没用，
// 后者定位不了。有了直方图就能回答**具体**问题：卡顿占多少帧、最坏那一帧多长、
// 是 16→33 的掉帧还是 500ms 的长卡（后者通常是 GC 或资源加载）。
//
// ## 数据从哪来（不用读时钟）
//
// `jishi_frame(ts)` 的 `ts` 是 rAF 给的时间戳；`dt` 就是它与上一帧的差。
// 那个差**已经是**「这一帧的实际间隔」，我们只是把它记下来 ——
// 不需要在 Rust 里再取一次时钟（wasm 上也没有可靠的高精度时钟，见 datetime.rs）。
//
// 📌 只留最近 **240 帧**（60fps ≈ 4 秒）：游戏的卡顿是「现在卡不卡」，
// 不是「三分钟前卡过一次」。更长的历史由调用方自己攒。

/// 环形窗口大小（帧）。
const 帧窗口: usize = 240;

thread_local! {
    static 耗时们: RefCell<VecDeque<f64>> = const { RefCell::new(VecDeque::new()) };
}

/// 记一帧的间隔（毫秒）。**0 与负数不记**（第一帧固定是 0，那是「没有上一帧」）。
pub fn 记帧(毫秒: f64) {
    if !(毫秒 > 0.0) || !毫秒.is_finite() {
        return;
    }
    耗时们.with(|q| {
        let mut q = q.borrow_mut();
        if q.len() >= 帧窗口 {
            q.pop_front();
        }
        q.push_back(毫秒);
    });
}

/// 清空（`图形.清帧统计`；换场景时想重新开始量就用它）。
pub fn 清帧耗时() {
    耗时们.with(|q| q.borrow_mut().clear());
}

/// 一次统计的结果。
pub struct 帧统计 {
    pub 帧数: usize,
    pub 平均: f64,
    pub 最小: f64,
    pub 最大: f64,
    pub p95: f64,
    pub 卡顿: usize,
    /// `(起, 止, 帧数)`；最后一桶的 `止` 是 `f64::INFINITY`。
    pub 桶们: Vec<(f64, f64, usize)>,
}

/// 分桶边界（毫秒）。**按「能不能看出人眼差别」划**：
///
/// | 区间 | 意思 |
/// |---|---|
/// | < 8 | 比 120Hz 还快（不用管） |
/// | 8–16 | 60fps 的正常区间 |
/// | 16–33 | 掉到 30~60fps（能感觉到） |
/// | 33–50 | 20~30fps（明显卡） |
/// | 50–100 | 10~20fps（玩不下去） |
/// | ≥ 100 | 长卡：多半是 GC / 加载 / 一帧里干了太多事 |
const 桶界: [f64; 5] = [8.0, 16.0, 33.0, 50.0, 100.0];

/// 算统计。**没有数据给 `None`**（调用方据此报「还没跑过帧」）。
pub fn 帧统计(卡顿阈值: f64) -> Option<帧统计> {
    let 样: Vec<f64> = 耗时们.with(|q| q.borrow().iter().copied().collect());
    if 样.is_empty() {
        return None;
    }
    let 帧数 = 样.len();
    let 和: f64 = 样.iter().sum();
    let 平均 = 和 / 帧数 as f64;
    let 最小 = 样.iter().copied().fold(f64::INFINITY, f64::min);
    let 最大 = 样.iter().copied().fold(0.0, f64::max);
    let mut 排 = 样.clone();
    排.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    // 最近秩法：p95 = 第 ceil(0.95·n) 个（n=20 时就是最大值，比插值好解释）
    let 位 = ((0.95 * 帧数 as f64).ceil() as usize).clamp(1, 帧数) - 1;
    let p95 = 排[位];
    let 卡顿 = 样.iter().filter(|x| **x > 卡顿阈值).count();
    let mut 桶们: Vec<(f64, f64, usize)> = Vec::with_capacity(桶界.len() + 1);
    let mut 下 = 0.0;
    for 上 in 桶界 {
        桶们.push((下, 上, 样.iter().filter(|x| **x >= 下 && **x < 上).count()));
        下 = 上;
    }
    桶们.push((下, f64::INFINITY, 样.iter().filter(|x| **x >= 下).count()));
    Some(帧统计 { 帧数, 平均, 最小, 最大, p95, 卡顿, 桶们 })
}

// ---------------------------------------------------------------------------
// 给 wasm 那两个入口用的窄接口（**不提 `Val`**）
// ---------------------------------------------------------------------------
//
// 为什么绕这一层：`Val` 是 crate 内类型（`pub(crate) enum Val`），wasm 那个
// crate 构造不出、也不该构造。而「跑一帧」「调处理函数」这两件事的**全部知识**
// 都在本 crate 里 —— 与其把 `Val` 放出去，不如把这两件事做成函数。

/// 跑一帧：取登记的 `每帧`，用 `(帧号, 间隔毫秒)` 调它。
///
/// 没登记 ⇒ 报一句「没有帧循环」的错误（调用方据此把返回码设成 3）。
pub fn 跑一帧(vm: &mut crate::VM, 帧号: i64, 间隔毫秒: f64) -> Result<(), JishiError> {
    let Some((_, 每帧)) = 取() else {
        return Err(crate::err("运行期错误", "没有在跑的帧循环"));
    };
    crate::调用值(
        vm,
        每帧,
        vec![
            crate::Val::Int(crate::jint::JInt::new(帧号 as i128)),
            crate::Val::Float(间隔毫秒),
        ],
    )?;
    Ok(())
}

/// 常驻模式下的一次事件：**不重跑源码**，直接在这个 VM 上调 `名字`。
///
/// 📌 实参口径与重放那条路**必须一致**：空文本 ⇒ 不传参（零参函数）；
/// 非空 ⇒ 传一个文本参数。两条路不一致的话，同一个处理函数在两种模式下
/// 行为不同 —— 那比「不支持」还糟。
pub fn 调处理函数(vm: &mut crate::VM, 名字: &str, 实参: &str) -> Result<(), JishiError> {
    let f = crate::按名字取全局(vm, 名字).ok_or_else(|| {
        crate::err_with_hint(
            "名称错误",
            format!("找不到函数「{名字}」"),
            "常驻模式（图形.运行）下事件只能调**顶层命名函数**".into(),
        )
    })?;
    let args = if 实参.is_empty() {
        Vec::new()
    } else {
        vec![crate::Val::Str(实参.into())]
    };
    crate::调用值(vm, f, args)?;
    Ok(())
}
