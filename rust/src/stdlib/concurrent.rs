//! 标准库 `并发`（M114）—— 把**一批互相独立的 IO** 同时做，结果顺序与输入一致。
//!
//! ⚠️ **这不是「语言级的线程 / 锁 / 队列」** —— 基石**不做**那个：本文件里
//! **没有一个 `Val` 跨线程**（线程里跑的只有 `String` / `Vec<u8>` 这类纯数据）。
//! 为什么只能这么做、代价是什么，见 `docs/设计决策.md` **D80**。
//!
//! ## 三条硬规矩（与 Python 侧 `并发._并行做` 逐条对应）
//!
//! 1. **返回顺序 = 输入顺序**：把项切成最多 `线程数` 个**连续段**，
//!    一段一个线程，再按段序拼回来 —— 顺序天然保持，`join` 先来后到不影响输出。
//! 2. **首个错胜出**：段按输入顺序扫，段内也按输入顺序，所以报的永远是
//!    **输入顺序里最靠前**的那个失败。
//! 3. `线程数 <= 1`、或**平台没有线程**（`wasm32`，浏览器试玩页）⇒ **顺序执行**：
//!    结果一模一样，只是不重叠等待。⚠️ wasm 那份是**另一个函数定义**
//!    （`#[cfg]` 分开），不是运行时判断 —— 这样 wasm 上**根本不会引用**
//!    `std::thread`（它在那儿会用不了）。
//!
//! ## 为什么线程数默认 4
//!
//! 这里等的是 **IO**，不是算力：4 路足够把「读一个文件的等待」跟另一个文件重叠
//! 起来，再大只是白加调度与内存。固定下来也让「同机同参数」可比。
//! 上限 32 是**护栏**（`并发.批量读(路径们, 100000)` 会当场把机器打死）。

use std::collections::HashMap;
use std::path::Path;

use super::{as_text, need_range, table, R};
use crate::{err, BuiltinHost, JishiError, Val};

/// 线程数上限（护栏）。
const 最大线程数: usize = 32;

fn 线程数(v: &Val) -> Result<usize, JishiError> {
    let n = match v {
        Val::Int(i) => *i,
        Val::Float(f) => *f as i128,
        other => {
            return Err(err(
                "类型错误",
                format!("线程数要是正整数，得到了「{}」", crate::type_label(other)),
            ))
        }
    };
    if n < 1 {
        return Err(err("值错误", format!("线程数要是正整数，现在是 {n}")));
    }
    Ok((n as usize).min(最大线程数))
}

/// 取「一串东西」成 `Vec<Val>`。文本/集合不收 —— 顺序这事必须说得清。
fn 列出(v: &Val, 谁: &str) -> Result<Vec<Val>, JishiError> {
    match v {
        Val::List(items) => Ok(items.borrow().clone()),
        Val::Str(_) => Err(err("类型错误", format!("{谁}要给一个列表，不能直接给文本"))),
        other => Err(err(
            "类型错误",
            format!(
                "{谁}要给一个列表，得到了「{}」",
                crate::type_label(other)
            ),
        )),
    }
}

/// 分段 + 多线程版（原生平台）。
///
/// ⚠️ `thread::scope` 让线程借用外层的 `项们` / `做一项`（不用 `'static`、
/// 不用 `Arc`）—— 这正是本项目要的：**零第三方依赖**、也不动值表示。
#[cfg(not(target_arch = "wasm32"))]
fn 并行做<T, R, F>(项们: Vec<T>, 线程数: usize, 做一项: F) -> Result<Vec<R>, JishiError>
where
    T: Sync,
    R: Send,
    F: Fn(&T) -> Result<R, JishiError> + Sync,
{
    let n = 项们.len();
    if n == 0 {
        return Ok(Vec::new());
    }
    let k = 线程数.max(1).min(n);
    if k <= 1 {
        return 项们.iter().map(|x| 做一项(x)).collect();
    }
    // 连续段边界：第 w 段是 `[n*w/k, n*(w+1)/k)`
    let 边界: Vec<usize> = (0..=k).map(|w| n * w / k).collect();
    let 项们 = &项们;
    let 做一项 = &做一项;
    let mut 段结果: Vec<Result<Vec<R>, JishiError>> = Vec::new();
    std::thread::scope(|s| {
        let mut 句柄们 = Vec::new();
        for w in 0..k {
            let (a, b) = (边界[w], 边界[w + 1]);
            if a == b {
                continue;
            }
            // ⚠️ 按段序 `push`，所以下面按序 `join` 就是按输入顺序收
            句柄们.push(s.spawn(move || -> Result<Vec<R>, JishiError> {
                let mut 出 = Vec::with_capacity(b - a);
                for x in &项们[a..b] {
                    出.push(做一项(x)?);
                }
                Ok(出)
            }));
        }
        for h in 句柄们 {
            // 线程 panic（真出了的话）不能把整个进程带走 —— 换成一句清楚的报错
            段结果.push(h.join().unwrap_or_else(|_| {
                Err(err("运行期错误", "并发任务异常退出（线程 panic）"))
            }));
        }
    });
    // 段按输入顺序扫 ⇒ 报的永远是输入顺序里最靠前的那个错
    let mut 出 = Vec::with_capacity(n);
    for r in 段结果 {
        出.extend(r?);
    }
    Ok(出)
}

/// 顺序版（`wasm32`：浏览器试玩页**没有线程**）。
///
/// ⚠️ 与上面那份**结果完全一样**（同样保序、同样首个错胜出），只是不重叠等待。
#[cfg(target_arch = "wasm32")]
fn 并行做<T, R, F>(项们: Vec<T>, _线程数: usize, 做一项: F) -> Result<Vec<R>, JishiError>
where
    F: Fn(&T) -> Result<R, JishiError>,
{
    项们.iter().map(|x| 做一项(x)).collect()
}

/// 一个文件的摘要（与 `加密.文件摘要` 同一句报错）。
fn 摘要一个(路径: &str, 算法: &str) -> Result<String, JishiError> {
    let data = std::fs::read(路径)
        .map_err(|e| err("值错误", format!("读文件「{路径}」失败：{e}")))?;
    Ok(crate::crypto::digest(算法, &data))
}

/// 一个文件的内容（与 `文件.读字节` 同一句报错）。
///
/// ⚠️ 返回的是**裸字节** `Vec<u8>`，不是 `Val` —— 线程里**不许出现 `Val`**
/// （它内含 `Rc`，不是 `Send`）。包成 `Val::Bytes` 由主线程做（见 D80）。
fn 读一个(路径: &str) -> Result<Vec<u8>, JishiError> {
    if !Path::new(路径).is_file() {
        return Err(err("文件错误", format!("找不到文件「{路径}」，没法读取")));
    }
    std::fs::read(路径).map_err(|e| err("文件错误", format!("读文件「{路径}」失败：{e}")))
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "并发";
        "批量摘要" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            // 参数顺序：路径们, 算法, 线程数（后两个可省）
            need_range("批量摘要", args, 1, 3)?;
            let 算法 = match args.get(1) {
                None | Some(Val::None_) => "sha256".to_string(),
                Some(v) => crate::crypto::归一算法(&as_text(v))?,
            };
            // ⚠️ 先验算法再动列表：**空列表也要报同样的错**（与 Python 侧同）
            let k = match args.get(2) {
                None | Some(Val::None_) => 4,
                Some(v) => 线程数(v)?,
            };
            let 项们 = 列出(&args[0], "「批量摘要」的第一个参数（路径们）")?;
            let 路径们: Vec<String> = 项们.iter().map(as_text).collect();
            let 结果 = 并行做(路径们, k, |p| 摘要一个(p, &算法))?;
            Ok(Val::List(std::rc::Rc::new(std::cell::RefCell::new(
                结果.into_iter().map(Val::Str).collect(),
            ))))
        },
        "批量读" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("批量读", args, 1, 2)?;
            let k = match args.get(1) {
                None | Some(Val::None_) => 4,
                Some(v) => 线程数(v)?,
            };
            let 项们 = 列出(&args[0], "「批量读」的第一个参数（路径们）")?;
            let 路径们: Vec<String> = 项们.iter().map(as_text).collect();
            let 结果 = 并行做(路径们, k, |p| 读一个(p))?;
            Ok(Val::List(std::rc::Rc::new(std::cell::RefCell::new(
                结果.into_iter()
                    .map(|b| Val::Bytes(std::rc::Rc::new(b)))
                    .collect(),
            ))))
        },
        "并行取" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("并行取", args, 1, 2)?;
            let k = match args.get(1) {
                None | Some(Val::None_) => 4,
                Some(v) => 线程数(v)?,
            };
            let 项们 = 列出(&args[0], "「并行取」的第一个参数（网址们）")?;
            let 网址们: Vec<String> = 项们.iter().map(as_text).collect();
            let 结果 = 并行做(网址们, k, |u| crate::http::取正文(u))?;
            Ok(Val::List(std::rc::Rc::new(std::cell::RefCell::new(
                结果.into_iter().map(Val::Str).collect(),
            ))))
        },
    }
}
