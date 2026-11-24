//! `进程` 模块（M217）：子进程**句柄** —— 起一个长期跑的子进程，边写边读。
//!
//! ## 与 `系统.执行` 的分工
//!
//! `系统.执行` 是「跑完拿输出」；这里要的是**交互**：喂一行、读一行、看错误行、
//! 半路终止。`系统.执行` 那边做不到这些，因为它的管道读写都在同一条同步路径上
//! （写满了就互锁）。
//!
//! ## D80 的「语言级没有线程」和这里不矛盾
//!
//! 用户脚本里仍然**没有**线程 / 锁 / 调度 —— 这里的两个读线程是**宿主实现细节**，
//! 目的只是「别让 `读行` 和 `写行` 互相等死」。它们对语言不可见：脚本只能
//! 阻塞地 `读行(超时)`。
//!
//! ## 🔴 孙进程不许拖住我们
//!
//! 子进程再起一个孙进程、孙进程继承了 stdout 时，**管道永远不会 EOF**。
//! 所以：
//!
//! - `等待` 只 `child.try_wait/wait`（等**直接子进程**），绝不去「读到 EOF」；
//! - 读线程是**游离的**（`JoinHandle` 直接丢掉），程序退出不等它 ——
//!   否则「0.2 秒的活」会被拖成 8.4 秒（老坑）。
//!
//! ## 按行 vs 按字节（M230）
//!
//! | 要读什么 | 用哪个 | 什么时候用 |
//! |---|---|---|
//! | **一行一行**的文本输出 | `读行` / `读错误行` | 普通命令行程序（`git`、`npm`、自写的脚本）：它们按行 flush |
//! | **一帧一帧**的二进制 / 分帧协议 | `读块` / `读可用` | LSP / DAP（`Content-Length: N\r\n\r\n{…}`）、自己定长分帧的协议 |
//!
//! 🔴 **为什么要分两种**：`读行` 会把 `\n` 当分隔符 —— 而分帧协议的**正文里
//! 完全可能有 `\n`**（LSP 的 JSON 报文一般单行，但 DAP 的正文是 JSON，
//! 而且二进制协议里 `\n` 到处都是）。按行读会把一帧切碎，症状是「报文解析
//! 到一半就报格式错」，而且**看日志是完整的**（因为日志按行打）。
//!
//! ⚠️ **一个句柄只用一种读法**：两个视图是**独立消费**的（同一段字节会被两边
//! 各看到一次），混用不会崩但你会重复读到同一段输出。
//!
//! ## wasm
//!
//! 浏览器里没有子进程。所有函数都在 wasm 上报同一句平台错，**模块照样在表里**
//! （与 `HTTP服务端` / `浏览器` 同一口径：`导入` 不炸，用的时候才说）。

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Condvar, Mutex};

use crate::stdlib::{need, need_range, table, R};
use crate::{err, native_obj, BuiltinHost, JishiError, Val};

/// 一条管道（stdout 或 stderr）的「行队列 + 是否已结束」。
///
/// `完` 和队列**放在同一把锁里**：分开的话「先看队列空、再看完」之间有一个窗口，
/// 恰好那会儿读线程把最后一行推进来并置了 `完` —— 我们就既没拿到行、也没等到信号，
/// `读行(超时省略)` 会永远挂住。这类「两个变量必须一起变」的场景，一把锁最省心。
struct 管线 {
    状态: Mutex<管线状态>,
    信号: Condvar,
    /// **M230**：`读块` / `读可用` 要不要攒字节。
    ///
    /// 🔴 **默认就攒**（不是「第一次读块才开」）。为什么：泵线程从 `启动` 那一刻
    /// 就在读，而 `读块` 是用户后面才调的 —— 懒开的话，那之前到达的字节已经被
    /// **读走并丢掉**了（M230 实测：直连 `jishi lsp` 收到的永远是空字节）。
    /// 代价是所有子进程输出多存一份字节副本；对「交互式子进程」这个量级可以接受。
    要字节: std::sync::atomic::AtomicBool,
}

struct 管线状态 {
    行们: VecDeque<String>,
    /// 读到 EOF（或管道出错）了 —— 此后「没有行」是**终局**，不是「再等等」。
    完: bool,
    /// **M230**：原始字节（`读块` / `读可用` 的视图）。
    字节: VecDeque<u8>,
}

impl 管线 {
    fn new() -> Arc<管线> {
        Arc::new(管线 {
            状态: Mutex::new(管线状态 {
                行们: VecDeque::new(),
                完: false,
                字节: VecDeque::new(),
            }),
            信号: Condvar::new(),
            要字节: std::sync::atomic::AtomicBool::new(true),
        })
    }

    /// 取一行。`超时` 为 `None` 就一直等；返回 `None` = 超时或已结束。
    #[cfg(not(target_arch = "wasm32"))]
    fn 取(&self, 超时: Option<f64>) -> Option<String> {
        let mut st = self.状态.lock().unwrap_or_else(|e| e.into_inner());
        match 超时 {
            None => loop {
                if let Some(行) = st.行们.pop_front() {
                    return Some(行);
                }
                if st.完 {
                    return None;
                }
                st = self.信号.wait(st).unwrap_or_else(|e| e.into_inner());
            },
            Some(秒) => {
                let 截 = std::time::Instant::now()
                    + std::time::Duration::from_secs_f64(秒.max(0.0));
                loop {
                    if let Some(行) = st.行们.pop_front() {
                        return Some(行);
                    }
                    if st.完 {
                        return None;
                    }
                    let 现在 = std::time::Instant::now();
                    if 现在 >= 截 {
                        return None;
                    }
                    let (新, _) = self
                        .信号
                        .wait_timeout(st, 截 - 现在)
                        .unwrap_or_else(|e| e.into_inner());
                    st = 新;
                }
            }
        }
    }

    /// **M230**：取最多 `最多` 个字节。`超时` 为 `None` 就一直等。
    ///
    /// 🔴 **超时给空字节，不报错** —— 与 `网络.获取流` 同一个口径：读超时是
    /// 「这一拍没东西」，不是「出错了」；LSP/DAP 那种「发了请求等回话」的循环
    /// 全靠这条能一直转下去。
    #[cfg(not(target_arch = "wasm32"))]
    fn 取字节(&self, 最多: usize, 超时: Option<f64>) -> Vec<u8> {
        let mut st = self.状态.lock().unwrap_or_else(|e| e.into_inner());
        let 有货 = |st: &mut 管线状态| -> Option<Vec<u8>> {
            if st.字节.is_empty() {
                return None;
            }
            let n = 最多.min(st.字节.len());
            Some(st.字节.drain(..n).collect())
        };
        match 超时 {
            None => loop {
                if let Some(块) = 有货(&mut st) {
                    return 块;
                }
                if st.完 {
                    return Vec::new();
                }
                st = self.信号.wait(st).unwrap_or_else(|e| e.into_inner());
            },
            Some(秒) => {
                let 截 = std::time::Instant::now()
                    + std::time::Duration::from_secs_f64(秒.max(0.0));
                loop {
                    if let Some(块) = 有货(&mut st) {
                        return 块;
                    }
                    if st.完 {
                        return Vec::new();
                    }
                    let 现在 = std::time::Instant::now();
                    if 现在 >= 截 {
                        return Vec::new();
                    }
                    let (新, _) = self
                        .信号
                        .wait_timeout(st, 截 - 现在)
                        .unwrap_or_else(|e| e.into_inner());
                    st = 新;
                }
            }
        }
    }
}

/// 句柄的状态（活在 `NativeObj.state` 里）。
pub(crate) struct 进程状态 {
    #[cfg(not(target_arch = "wasm32"))]
    子: Mutex<std::process::Child>,
    #[cfg(not(target_arch = "wasm32"))]
    入: Mutex<Option<std::process::ChildStdin>>,
    出: Arc<管线>,
    错: Arc<管线>,
}

fn 借(对象: &crate::NativeObj) -> std::cell::Ref<'_, 进程状态> {
    std::cell::Ref::map(对象.state.borrow(), |s| {
        s.downcast_ref::<进程状态>().expect("NativeObj 应是进程")
    })
}

/// 从 `Val` 里认出句柄（不是句柄就报一句**可操作**的错）。
fn 句柄(v: &Val, 名: &str) -> Result<std::rc::Rc<crate::NativeObj>, JishiError> {
    match v {
        Val::Native(o) if o.type_name == "进程" => Ok(o.clone()),
        _ => Err(err(
            "类型错误",
            format!(
                "「{名}」要传 `进程.启动` 拿到的句柄（现在是「{}」）",
                crate::type_label(v)
            ),
        )),
    }
}

fn 文本(v: &Val, 名: &str) -> Result<String, JishiError> {
    match v {
        Val::Str(s) => Ok(s.to_string()),
        _ => Err(err(
            "类型错误",
            format!(
                "「{名}」要传文本（现在是「{}」）",
                crate::type_label(v)
            ),
        )),
    }
}

fn 超时(args: &[Val], 名: &str) -> Result<Option<f64>, JishiError> {
    match args.get(1) {
        None | Some(Val::None_) => Ok(None),
        Some(v) => {
            let 秒 = crate::stdlib::as_num(v).map_err(|_| {
                err("类型错误", format!("「{名}」的超时要给秒数（省略 = 一直等）"))
            })?;
            if 秒 < 0.0 {
                return Err(err(
                    "值错误",
                    format!("「{名}」的超时不能是负数（给的是 {}）", crate::display(v)),
                ));
            }
            Ok(Some(秒))
        }
    }
}

/// wasm 上的统一回绝（模块在表里，用的时候才说）。
#[cfg(target_arch = "wasm32")]
fn 平台不支持() -> JishiError {
    err(
        "运行期错误",
        "这个平台起不了子进程（浏览器里没有进程；`进程` 只能在命令行宿主里用）",
    )
}

pub(crate) fn module() -> std::collections::HashMap<String, Val> {
    table! { "进程";
        "启动" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("启动", args, 1, 3)?;
            启动(args)
        },
        "写文本" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("写文本", args, 2)?;
            写(args, false)
        },
        "写行" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("写行", args, 1, 2)?;
            写(args, true)
        },
        "读行" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("读行", args, 1, 2)?;
            读(args, false)
        },
        "读错误行" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("读错误行", args, 1, 2)?;
            读(args, true)
        },
        // **M230（L-13）**：字节级读（LSP / DAP 的 Content-Length 分帧要用）
        "读块" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("读块", args, 1, 3)?;
            读块(args)
        },
        "读可用" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("读可用", args, 1)?;
            读可用(args)
        },
        "在运行" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("在运行", args, 1)?;
            在运行(args)
        },
        "终止" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("终止", args, 1)?;
            终止(args)
        },
        "等待" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("等待", args, 1, 2)?;
            等待(args)
        },
        "退出码" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("退出码", args, 1)?;
            退出码(args)
        },
        // 📌 M217 顺带补的两个（W13 要用）：
        // `进程.进程号()` 给本进程 PID；`进程.存在(号)` 问「那个号还活着吗」。
        // 有了这一对，`.lock` 里才能记属主、才能在强杀之后**自动回收**陈旧锁。
        "进程号" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("进程号", args, 0)?;
            进程号()
        },
        "存在" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("存在", args, 1)?;
            存在(args)
        },
    }
}

// ---------------------------------------------------------------------------
// 各函数
// ---------------------------------------------------------------------------

#[cfg(not(target_arch = "wasm32"))]
fn 启动(args: &[Val]) -> R {
    use std::process::{Command, Stdio};

    let 命令 = 文本(&args[0], "启动")?;
    // 参数们：文本列表（argv 形态，**不过 shell**）
    let mut argv: Vec<String> = Vec::new();
    match args.get(1) {
        None | Some(Val::None_) => {}
        Some(Val::List(l)) => {
            for v in l.borrow().iter() {
                argv.push(文本(v, "启动")?);
            }
        }
        Some(_) => return Err(err("类型错误", "「启动」的参数们要给文本列表")),
    }

    let mut cmd = Command::new(&命令);
    cmd.args(&argv);

    // 选项：{"目录", "环境", "合并错误"}
    //
    // ⚠️ 每个键都**先取出值再判断**（`let 值 = dict_get(…)`）：
    // 写成 `if let Some(x) = dict_get(&d.borrow(), …)` 时，那个临时的 `Ref`
    // 会活到整个 `if let` 结束，编译器会报「`d` 活得不够久」。
    let mut 合并 = false;
    if let Some(选项) = args.get(2) {
        if !matches!(选项, Val::None_) {
            let d = match 选项 {
                Val::Dict(d) => d.clone(),
                _ => return Err(err("类型错误", "「启动」的选项要给字典（可省略）")),
            };
            let 目录值 = crate::dict_get(&d.borrow(), &Val::Str("目录".into()));
            if let Some(Val::Str(路径)) = 目录值 {
                if !路径.as_str().is_empty() {
                    cmd.current_dir(路径.as_str());
                }
            }
            let 环境值 = crate::dict_get(&d.borrow(), &Val::Str("环境".into()));
            if let Some(Val::Dict(环境)) = 环境值 {
                for (k, v) in crate::dict_items(&环境.borrow()) {
                    let 名 = 文本(&k, "启动")?;
                    let 值 = 文本(&v, "启动")?;
                    cmd.env(名, 值);
                }
            }
            let 合并值 = crate::dict_get(&d.borrow(), &Val::Str("合并错误".into()));
            if let Some(v) = 合并值 {
                合并 = crate::truthy(v);
            }
        }
    }

    cmd.stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(if 合并 { Stdio::piped() } else { Stdio::piped() });

    let mut 子 = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            let 原因 = match e.kind() {
                std::io::ErrorKind::NotFound => "找不到这个程序".to_string(),
                _ => e.to_string(),
            };
            return Err(err("运行期错误", format!("起不来进程「{命令}」：{原因}")));
        }
    };

    let 出pip = 子.stdout.take();
    let 错pip = 子.stderr.take();
    let 入 = 子.stdin.take();

    let 出 = 管线::new();
    let 错 = 管线::new();
    if let Some(pipe) = 出pip {
        起读线程(pipe, 出.clone());
    } else {
        出.状态.lock().unwrap().完 = true;
    }
    // `合并错误`：stderr 那条管道也喂进**同一个**队列（`读错误行` 立刻就是空）。
    //
    // ⚠️ 为什么不在 `Command` 上让 stderr 复用 stdout 的 fd：`std` 没有
    //    「把子进程的两个 Stdio 指到同一根管道」的 API（`Stdio::dup` 不存在），
    //    要自己开管道就得引第三方。两个读线程喂一个队列已经够用 ——
    //    **跨管道的先后顺序**本来也不保证（Python 的 `stderr=STDOUT` 是真合并，
    //    这里退一步：每条管道内部有序，管道之间按到货顺序）。
    match 错pip {
        Some(pipe) => 起读线程(pipe, if 合并 { 出.clone() } else { 错.clone() }),
        None => 错.状态.lock().unwrap().完 = true,
    }
    if 合并 {
        错.状态.lock().unwrap().完 = true;
    }

    Ok(native_obj(
        "进程",
        &[],
        std::rc::Rc::new(std::cell::RefCell::new(进程状态 {
            子: Mutex::new(子),
            入: Mutex::new(入),
            出,
            错,
        })),
    ))
}

/// 起一个**游离**的读线程（`JoinHandle` 直接丢）。
///
/// ⚠️ 丢句柄不是偷懒：程序退出时不该为「子进程的孙进程攥着管道」干等。
/// 线程会在读到 EOF 时自己结束；读不到就跟着进程一起消失。
#[cfg(not(target_arch = "wasm32"))]
fn 起读线程(pipe: impl std::io::Read + Send + 'static, 线: Arc<管线>) {
    use std::io::Read;
    std::thread::spawn(move || {
        // 📌 M230：以前是 `BufRead::split(b'\n')`（只会切行），现在改成
        // **按块读一遍、两个视图各存一份**：
        //   ① 切行 → `行们`（`读行` 照旧）；
        //   ② 原样字节 → `字节`（`读块` / `读可用`，LSP/DAP 的 Content-Length 分帧要用）。
        // 两个视图是**独立消费**的：混用不会崩，但同一段输出会两边各看到一次
        // —— 一个句柄只用一种读法（模块头写明了）。
        let mut 读 = std::io::BufReader::new(pipe);
        let mut 缓冲 = [0u8; 8192];
        let mut 尾: Vec<u8> = Vec::new();
        loop {
            let n = match 读.read(&mut 缓冲) {
                Ok(0) => break,
                Ok(n) => n,
                Err(_) => break,
            };
            let 片 = &缓冲[..n];
            let mut st = 线.状态.lock().unwrap_or_else(|e| e.into_inner());
            if 线
                .要字节
                .load(std::sync::atomic::Ordering::Relaxed)
            {
                st.字节.extend(片.iter().copied());
            }
            尾.extend_from_slice(片);
            while let Some(i) = 尾.iter().position(|&b| b == b'\n') {
                let mut 行: Vec<u8> = 尾.drain(..=i).collect();
                行.pop();                                    // 去 `\n`
                if 行.last() == Some(&b'\r') {
                    行.pop();                                // 去 Windows 的 `\r`
                }
                st.行们
                    .push_back(String::from_utf8_lossy(&行).into_owned());
            }
            drop(st);
            线.信号.notify_all();
        }
        let mut st = 线.状态.lock().unwrap_or_else(|e| e.into_inner());
        st.完 = true;
        线.信号.notify_all();
    });
}

#[cfg(not(target_arch = "wasm32"))]
fn 写(args: &[Val], 补换行: bool) -> R {
    use std::io::Write;
    let obj = 句柄(&args[0], if 补换行 { "写行" } else { "写文本" })?;
    let 名 = if 补换行 { "写行" } else { "写文本" };
    let mut 内容 = match args.get(1) {
        None | Some(Val::None_) if 补换行 => String::new(),
        Some(v) => 文本(v, 名)?,
        None => String::new(),
    };
    if 补换行 {
        内容.push('\n');
    }
    let st = 借(&obj);
    let mut 守 = st.入.lock().unwrap_or_else(|e| e.into_inner());
    match 守.as_mut() {
        Some(pipe) => {
            let r = pipe.write_all(内容.as_bytes()).and_then(|_| pipe.flush());
            if r.is_err() {
                return Err(err(
                    "运行期错误",
                    "写不进这个进程：它的标准输入已经关了（子进程退出了？）",
                ));
            }
            Ok(Val::Bool(true))
        }
        None => Err(err(
            "运行期错误",
            "写不进这个进程：它的标准输入已经关了（子进程退出了？）",
        )),
    }
}

#[cfg(not(target_arch = "wasm32"))]
fn 读(args: &[Val], 读错误: bool) -> R {
    let 名 = if 读错误 { "读错误行" } else { "读行" };
    let obj = 句柄(&args[0], 名)?;
    let 限 = 超时(args, 名)?;
    let st = 借(&obj);
    let 线 = if 读错误 { st.错.clone() } else { st.出.clone() };
    drop(st);
    match 线.取(限) {
        Some(行) => Ok(Val::Str(行.as_str().into())),
        None => Ok(Val::None_),
    }
}

/// **M230（L-13）**：`进程.读块(句柄, 最多=65536, 超时=空)` → 字节。
///
/// 🔴 要这个的理由：LSP / DAP 的报文是 `Content-Length: 42\r\n\r\n{…}` ——
/// **正文里可能带 `\n`**（中文 JSON 会转义，但别赌），按行读会被切碎。
/// 磐石 IDE 之前为此在中间挂了个 Node 的 `lsp行桥.js`，这一条做完那个桥可以删。
///
/// ⚠️ **超时给空字节，不报错** —— 与 `网络.获取流` 同一个口径。
#[cfg(not(target_arch = "wasm32"))]
fn 读块(args: &[Val]) -> R {
    let obj = 句柄(&args[0], "读块")?;
    let 最多 = match args.get(1) {
        None | Some(Val::None_) => 65536usize,
        Some(Val::Int(i)) => {
            let n = i.get();
            if n <= 0 {
                return Err(err(
                    "值错误",
                    "「读块」的第二个参数（最多几个字节）要大于 0".to_string(),
                ));
            }
            (n as usize).min(64 * 1024 * 1024)
        }
        Some(other) => {
            return Err(err(
                "类型错误",
                format!(
                    "「读块」的第二个参数要整数（最多几个字节），给的是{}",
                    crate::type_label(other)
                ),
            ))
        }
    };
    let 限 = 超时(args, "读块")?;
    let 线 = {
        let st = 借(&obj);
        st.出.clone()
    };
    // 第一次读块时才开字节视图（普通用途不多存一个字节）
    线.要字节
        .store(true, std::sync::atomic::Ordering::Relaxed);
    let 块 = 线.取字节(最多, 限);
    Ok(Val::Bytes(std::rc::Rc::new(块)))
}

/// **M230**：`进程.读可用(句柄)` → 现在有多少字节可读（**不阻塞**）。
///
/// 用途：写「读一批、处理一批、没了就干别的」那种循环，不必靠超时去探。
#[cfg(not(target_arch = "wasm32"))]
fn 读可用(args: &[Val]) -> R {
    let obj = 句柄(&args[0], "读可用")?;
    let 线 = {
        let st = 借(&obj);
        st.出.clone()
    };
    线.要字节
        .store(true, std::sync::atomic::Ordering::Relaxed);
    let n = {
        let st = 线.状态.lock().unwrap_or_else(|e| e.into_inner());
        st.字节.len()
    };
    Ok(Val::Int(crate::jint::JInt::new(n as i128)))
}

#[cfg(not(target_arch = "wasm32"))]
fn 在运行(args: &[Val]) -> R {
    let obj = 句柄(&args[0], "在运行")?;
    let st = 借(&obj);
    let mut 子 = st.子.lock().unwrap_or_else(|e| e.into_inner());
    match 子.try_wait() {
        Ok(None) => Ok(Val::Bool(true)),
        Ok(Some(_)) => Ok(Val::Bool(false)),
        // 取状态出错时，只能说「不在运行」——别把一个已经僵住的进程报成活的
        Err(_) => Ok(Val::Bool(false)),
    }
}

#[cfg(not(target_arch = "wasm32"))]
fn 终止(args: &[Val]) -> R {
    let obj = 句柄(&args[0], "终止")?;
    let st = 借(&obj);
    let mut 子 = st.子.lock().unwrap_or_else(|e| e.into_inner());
    match 子.try_wait() {
        Ok(Some(_)) => return Ok(Val::Bool(false)), // 已经结束了，幂等
        _ => {}
    }
    let _ = 子.kill();
    // 收尸：不然句柄会变成僵尸（Windows 上是「进程对象」不释放）
    let _ = 子.wait();
    Ok(Val::Bool(true))
}

#[cfg(not(target_arch = "wasm32"))]
fn 等待(args: &[Val]) -> R {
    let obj = 句柄(&args[0], "等待")?;
    let 限 = 超时(args, "等待")?;
    let st = 借(&obj);
    let mut 子 = st.子.lock().unwrap_or_else(|e| e.into_inner());
    match 限 {
        None => match 子.wait() {
            Ok(s) => Ok(Val::Int(crate::jint::JInt::new(码(s)))),
            Err(e) => Err(err("运行期错误", format!("等这个进程失败：{e}"))),
        },
        Some(秒) => {
            // `std` 没有 `wait_timeout`：轮询。间隔 5 ms —— 进程一般在毫秒级反应，
            // 轮询的 CPU 开销可以忽略，而比「起一个线程等 + 超时丢弃」简单得多
            // （后者会给每个句柄多一个线程，还得处理「超时后线程还在等」）。
            let 截 = std::time::Instant::now() + std::time::Duration::from_secs_f64(秒);
            loop {
                match 子.try_wait() {
                    Ok(Some(s)) => return Ok(Val::Int(crate::jint::JInt::new(码(s)))),
                    Ok(None) => {}
                    Err(e) => return Err(err("运行期错误", format!("等这个进程失败：{e}"))),
                }
                if std::time::Instant::now() >= 截 {
                    return Ok(Val::None_);
                }
                std::thread::sleep(std::time::Duration::from_millis(5));
            }
        }
    }
}

#[cfg(not(target_arch = "wasm32"))]
fn 退出码(args: &[Val]) -> R {
    let obj = 句柄(&args[0], "退出码")?;
    let st = 借(&obj);
    let mut 子 = st.子.lock().unwrap_or_else(|e| e.into_inner());
    match 子.try_wait() {
        Ok(Some(s)) => Ok(Val::Int(crate::jint::JInt::new(码(s)))),
        _ => Ok(Val::None_),
    }
}

#[cfg(not(target_arch = "wasm32"))]
fn 码(s: std::process::ExitStatus) -> i128 {
    s.code().unwrap_or(-1) as i128
}

// ---------------------------------------------------------------------------
// 进程号 / 存活查询（M217 顺带补：W13 的 `.lock` 要用）
// ---------------------------------------------------------------------------

#[cfg(not(target_arch = "wasm32"))]
fn 进程号() -> R {
    Ok(Val::Int(crate::jint::JInt::new(std::process::id() as i128)))
}

/// 那个进程号还活着吗？
///
/// ⚠️ 语义上只能说「**看起来**还活着」：
/// - 号被复用了（旧进程死了、系统把号给了别人）⇒ 会给 真；
/// - 别的用户的进程（POSIX 上 `kill` 给 EPERM）⇒ 也是**活着**，不能当死。
///
/// 对 `.lock` 那个用途够用：判错的后果是「这次不给开，让用户看一眼」，
/// 而**反方向**（把活的判成死的）会两个进程同时写一个库 —— 那才是灾难。
#[cfg(all(not(target_arch = "wasm32"), windows))]
fn 存在(args: &[Val]) -> R {
    let 号 = crate::stdlib::as_intish(&args[0]).map_err(|_| {
        err("类型错误", "「存在」要传进程号（整数），比如 进程.进程号()")
    })? as u32;
    #[link(name = "kernel32")]
    extern "system" {
        fn OpenProcess(权限: u32, 继承: i32, 号: u32) -> isize;
        fn GetExitCodeProcess(句柄: isize, 码: *mut u32) -> i32;
        fn CloseHandle(句柄: isize) -> i32;
    }
    const PROCESS_QUERY_LIMITED_INFORMATION: u32 = 0x1000;
    const STILL_ACTIVE: u32 = 259;
    unsafe {
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, 号);
        if h == 0 {
            // 打不开有两种：**没有这个进程**（ERROR_INVALID_PARAMETER=87）与
            // **没权限**（ERROR_ACCESS_DENIED=5）。后者说明它**活着** ——
            // 把别人的进程判成死的，就可能两个进程同时写一个库。
            let 错 = std::io::Error::last_os_error().raw_os_error().unwrap_or(0);
            return Ok(Val::Bool(错 == 5));
        }
        let mut 退 = 0u32;
        let 好 = GetExitCodeProcess(h, &mut 退);
        CloseHandle(h);
        Ok(Val::Bool(好 != 0 && 退 == STILL_ACTIVE))
    }
}

#[cfg(all(not(target_arch = "wasm32"), unix))]
fn 存在(args: &[Val]) -> R {
    let 号 = crate::stdlib::as_intish(&args[0]).map_err(|_| {
        err("类型错误", "「存在」要传进程号（整数），比如 进程.进程号()")
    })? as i32;
    extern "C" {
        fn kill(号: i32, 信号: i32) -> i32;
    }
    // 信号 0 = 只做权限与存在性检查，不真发信号
    let r = unsafe { kill(号, 0) };
    if r == 0 {
        return Ok(Val::Bool(true));
    }
    // EPERM（1）= 有这个进程、只是不归我管 ⇒ **算活着**
    let 错 = std::io::Error::last_os_error().raw_os_error().unwrap_or(0);
    Ok(Val::Bool(错 == 1))
}

#[cfg(target_arch = "wasm32")]
fn 进程号() -> R {
    Err(平台不支持())
}
#[cfg(target_arch = "wasm32")]
fn 存在(_args: &[Val]) -> R {
    Err(平台不支持())
}

// ---------------------------------------------------------------------------
// wasm：模块在表里，用的时候统一回绝
// ---------------------------------------------------------------------------

#[cfg(target_arch = "wasm32")]
fn 启动(_args: &[Val]) -> R {
    Err(平台不支持())
}
#[cfg(target_arch = "wasm32")]
fn 写(_args: &[Val], _补换行: bool) -> R {
    Err(平台不支持())
}
#[cfg(target_arch = "wasm32")]
fn 读(_args: &[Val], _读错误: bool) -> R {
    Err(平台不支持())
}
// 📌 M230：wasm 侧也要有同名函数 —— 分派表里的闭包**照样过类型检查**，
// 少了这两个就是「wasm 构建失败」（而本机 native 构建是绿的，最容易漏）。
#[cfg(target_arch = "wasm32")]
fn 读块(_args: &[Val]) -> R {
    Err(平台不支持())
}
#[cfg(target_arch = "wasm32")]
fn 读可用(_args: &[Val]) -> R {
    Err(平台不支持())
}
#[cfg(target_arch = "wasm32")]
fn 在运行(_args: &[Val]) -> R {
    Err(平台不支持())
}
#[cfg(target_arch = "wasm32")]
fn 终止(_args: &[Val]) -> R {
    Err(平台不支持())
}
#[cfg(target_arch = "wasm32")]
fn 等待(_args: &[Val]) -> R {
    Err(平台不支持())
}
#[cfg(target_arch = "wasm32")]
fn 退出码(_args: &[Val]) -> R {
    Err(平台不支持())
}
