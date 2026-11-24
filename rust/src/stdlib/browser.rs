//! 标准库 `浏览器`（M167）—— **只在 wasm（网页）里能用**。
//!
//! ## 平台边界（照旧诚实）
//!
//! 命令行 / Node 上**每一个函数**都报同一条中文错，不是「悄悄不做事」：
//! 同步 VM 没有事件循环，浏览器那套异步 API 塞不进来（D81 就是因为这条
//! 把 `HTTP服务端` 在 Node 上判成不支持的）。
//!
//! ⇒ 所以这里**只有一张表**，两种目标共用；非 wasm 靠 [`host_call`] 的
//! 假实现统一报错。这样「哪些函数存在」在两个目标上一模一样（`test_m51`
//! 的一致性检测不会说「宿主缺模块」），差别只在**调用时**。
//!
//! ## 同步 / 异步怎么缝（这一站的核心设计）
//!
//! > **一次事件 = 一次完整执行。**
//!
//! 程序在启动时用 [`绑定`] 把「哪个函数处理哪个事件」告诉 JS 侧；事件来时
//! JS 调 wasm 的 `jishi_call(函数名)`，那一侧**重新跑一遍程序**（重建全局状态），
//! 再调那个函数。
//!
//! ⇒ **必然的推论：状态要自己持久化**（`存本地` / 从 DOM 读），不能指望
//! 「上次跑的那个变量还在」。这正是「刷新页面数据还在」自然成立的原因 ——
//! 同一套写法既满足事件，又满足刷新。
//!
//! 代价说清楚：**每次点击都会重跑一遍顶层代码**（包括 `重绘()`）。
//! 这不是 bug，是这个模式的形状；真要做到「只跑处理函数」，得让 VM 常驻 ——
//! 那与「wasm 不能阻塞主线程」直接冲突。
//!
//! ## ABI
//!
//! 所有 DOM 操作走一条通道：`jishi_host_call(请求JSON) -> 响应JSON`。
//!
//! | 方向 | 形状 |
//! |---|---|
//! | 请求 | `{"op":"设文本","args":["#a","你好"]}` |
//! | 成功 | `{"ok":true,"value":…}` |
//! | 失败 | `{"ok":false,"message":"…"}` |
//!
//! 一条通道而不是十几个 import：JS 那边的 shim 只要实现一个函数，
//! 加新操作**不用动 ABI**（`ABI_VERSION` 只在通道形状变了才 +1）。

use std::collections::HashMap;

use crate::{err, err_with_hint, BuiltinHost, JishiError, Val};

use super::{need_range, table, R};

/// 平台错文案。**三个宿主各写一份，必须逐字相同**（`test_m167_browser.py` 盯着）。
pub(crate) const 无浏览器: &str = "这个平台没有浏览器：「浏览器」模块只在网页（wasm）里可用。\
命令行 / Node 上要抓网页请用「html」模块解析、要发请求请用「网络」模块。";

// ---------------------------------------------------------------------------
// 宿主通道
// ---------------------------------------------------------------------------

#[cfg(target_arch = "wasm32")]
mod host {
    use std::cell::RefCell;

    /// JS 侧实现：读请求、执行、把响应写回来（走 [`super::回填响应`]）。
    ///
    /// ⚠️ `link(wasm_import_module = "jishi")` **不能省**：不写的话 rust-lld 会把
    /// `jishi_host_call` 当成「未定义符号」直接链接失败（报
    /// `undefined symbol: jishi_host_call`）—— 它不知道这是个 **import**。
    /// 写了之后 JS 侧的 `importObject.jishi.jishi_host_call` 就是它的实现。
    #[link(wasm_import_module = "jishi")]
    extern "C" {
        fn jishi_host_call(ptr: *const u8, len: usize) -> i32;
    }

    thread_local! {
        /// 上一次宿主调用的响应（JSON 文本），由 JS 侧回填。
        static RESP: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
    }

    /// 把 JS 侧的执行结果塞进来。**导出给 wasm shim 调**（见 `wasm/src/lib.rs`）。
    pub fn 回填响应(bytes: &[u8]) {
        RESP.with(|r| *r.borrow_mut() = bytes.to_vec());
    }

    pub fn 调用(req: &str) -> Vec<u8> {
        RESP.with(|r| r.borrow_mut().clear());
        let bytes = req.as_bytes();
        unsafe { jishi_host_call(bytes.as_ptr(), bytes.len()) };
        RESP.with(|r| r.borrow().clone())
    }
}

#[cfg(target_arch = "wasm32")]
pub use host::回填响应;

/// 给宿主发一条**没有返回值**的操作（M186：`开帧` / `停帧`）。
///
/// 为什么单独一个入口：`host_call` 会把响应读回来并解析，而「开始/停止帧循环」
/// 这件事**没有回话**。用 `host_call` 也能跑（回一个 `null`），但那要求 JS 侧
/// 每个 case 都写 `return null`——多一处能写错的地方。
///
/// ⚠️ **非 wasm 上什么都不做**：调用方（`图形.运行` / `图形.停`）在更外层就已经
/// 报过平台错了，这里再报一次只是噪音。
pub(crate) fn 通知宿主(op: &str) {
    #[cfg(target_arch = "wasm32")]
    {
        let _ = host::调用(&format!("{{\"op\":\"{}\",\"args\":[]}}", esc(op)));
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        let _ = op;
    }
}

/// 调一次宿主。**非 wasm 上永远报平台错** —— 这就是「一张表两种目标」的落点。
#[cfg(target_arch = "wasm32")]
pub(crate) fn host_call(op: &str, args: &[Val]) -> Result<Val, JishiError> {
    let resp = host::调用(&build_req(op, args));
    parse_resp(&resp)
}

#[cfg(not(target_arch = "wasm32"))]
pub(crate) fn host_call(_op: &str, _args: &[Val]) -> Result<Val, JishiError> {
    Err(err("运行期错误", 无浏览器))
}

// ---------------------------------------------------------------------------
// 请求 / 响应的 JSON（只有这几种形状，手拼够了）
// ---------------------------------------------------------------------------

/// JSON 字符串转义。
pub(crate) fn esc(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

fn build_req(op: &str, args: &[Val]) -> String {
    let mut s = format!("{{\"op\":\"{}\",\"args\":[", esc(op));
    for (i, a) in args.iter().enumerate() {
        if i > 0 {
            s.push(',');
        }
        match a {
            Val::Str(t) => s.push_str(&format!("\"{}\"", esc(t))),
            Val::Int(n) => s.push_str(&n.to_string()),
            Val::Float(f) => {
                if f.fract() == 0.0 && f.is_finite() {
                    s.push_str(&format!("{f:.1}"));
                } else {
                    s.push_str(&format!("{f}"));
                }
            }
            Val::Bool(b) => s.push_str(if *b { "true" } else { "false" }),
            Val::None_ => s.push_str("null"),
            // 其余（函数/容器…）当文本传 —— `绑定` 要传函数名，走这里之前
            // 已经在 `取函数名` 里转成文本了，正常到不了这个分支。
            other => s.push_str(&format!("\"{}\"", esc(&crate::display(other)))),
        }
    }
    s.push_str("]}");
    s
}

/// 解析响应。只认三种形状：`{"ok":true,"value":…}` / `{"ok":true}` / `{"ok":false,"message":"…"}`。
fn parse_resp(bytes: &[u8]) -> Result<Val, JishiError> {
    let text = String::from_utf8_lossy(bytes);
    let json = crate::json::parse(&text)
        .map_err(|e| err("运行期错误", format!("浏览器宿主返回了看不懂的东西：{e}")))?;
    let ok = json.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
    if !ok {
        let msg = json
            .get("message")
            .and_then(|v| v.as_str())
            .unwrap_or("（宿主没说原因）");
        return Err(err("运行期错误", msg.to_string()));
    }
    Ok(match json.get("value") {
        None | Some(crate::json::Json::Null) => Val::None_,
        Some(v) => 值_of(v),
    })
}

/// JSON → `Val`（M186：`图形` 要拿 `{"宽","高"}` / 鼠标状态这种**结构化**回话）。
///
/// 以前这里对数组 / 对象直接报错 —— 那时 DOM 操作的返回值只有文本 / 数字 /
/// 布尔 / 空，报错是「别悄悄转成一个看起来像的东西」。现在画布接口要返回
/// 结构（尺寸、鼠标位置、像素），所以把这两条补上：对象 → 字典、数组 → 列表。
///
/// ⚠️ **数字按 JSON 文本里的写法分整数 / 浮点**：`320` 进来是 `整数`、
/// `320.5` 进来是 `浮点`（解析器已经替我们分好了）。调用方（`图形`）用
/// `数()` 两者都收 —— 所以「画布宽 800」不会因为宿主写成 `800.0` 就变成浮点。
pub(crate) fn 值_of(j: &crate::json::Json) -> Val {
    use crate::json::Json;
    match j {
        Json::Null => Val::None_,
        Json::Str(s) => Val::Str(s.as_str().into()),
        Json::Int(n) => Val::Int(crate::jint::JInt::new(*n)),
        Json::Float(f) => Val::Float(*f),
        Json::Bool(b) => Val::Bool(*b),
        Json::Arr(a) => crate::list_new(a.iter().map(值_of).collect()),
        Json::Obj(m) => crate::dict_new(
            m.iter()
                .map(|(k, v)| (Val::Str(k.as_str().into()), 值_of(v)))
                .collect(),
        ),
    }
}

// ---------------------------------------------------------------------------
// 函数表
// ---------------------------------------------------------------------------

/// 一个「选择器 + 文本」这类**两参数**的简单操作：检查完就交给宿主。
macro_rules! 两步 {
    ($名:literal) => {
        |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range($名, args, 2, 2)?;
            host_call($名, args)
        }
    };
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "浏览器";
        // -- 基石 → 页面 --
        "设文本" => 两步!("设文本"),
        "设HTML" => 两步!("设HTML"),
        "取值" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("取值", args, 1, 1)?;
            host_call("取值", args)
        },
        "设值" => 两步!("设值"),
        "加类" => 两步!("加类"),
        "删类" => 两步!("删类"),
        "切类" => 两步!("切类"),
        "点击" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("点击", args, 1, 1)?;
            host_call("点击", args)
        },
        "显示" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("显示", args, 2, 2)?;
            host_call("显示", args)
        },
        // M223：`浏览器.匹配("(prefers-color-scheme: dark)")` → 真/假。
        // 判据靠它验「窗口.设主题 之后页面配色真的变了」；响应式布局也用得上。
        "匹配" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("匹配", args, 1, 1)?;
            host_call("匹配", args)
        },
        // -- 页面 → 基石 --
        // `绑定(选择器, 事件, 函数)`：**函数要传名字**（字符串），不是函数值。
        // 理由见模块头「一次事件 = 一次完整执行」—— 事件来时是**另一个**
        // 执行回合，手里只有名字，没有上次那个 VM 里的函数对象。
        "绑定" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            // M229（D12）：第 4 个参数 `带事件信息`（默认假）—— 为真时回调收的是
            // **一段 JSON**（`序`/`类型`/`键`/`shift`/`ctrl`/`alt`），而不是光秃秃的序号。
            //
            // ⚠️ 与路线图原文有一处差异，如实记：原文写「回调收**第二个参数**」，
            //    但 wasm 的 `jishi_call` 只带得动一个字符串参数（ABI 3 定死的）。
            //    为一个可选特性去改 ABI 不值当 —— 改成一个 JSON 里带 `序`，
            //    信息一点没少，存量回调（不带信息）一个字节都没变。
            need_range("绑定", args, 3, 4)?;
            let 名 = 函数名(&args[2])?;
            // **M229（D4）**：事件名收中文，映射成 DOM 的英文名。
            // 认不出来的**当场报错**（列出可用的）—— 以前直接把中文丢给
            // `addEventListener`，浏览器不认、**静默不触发**，症状是「点了没反应」。
            let 事件 = match &args[1] {
                Val::Str(s) => 事件英文名(s)?,
                other => {
                    return Err(err(
                        "类型错误",
                        format!("「绑定」的第二个参数要事件名（文本），给的是{}",
                                crate::type_label(other)),
                    ))
                }
            };
            let 带信息 = match args.get(3) {
                None => false,
                Some(Val::Bool(b)) => *b,
                Some(other) => {
                    return Err(err(
                        "类型错误",
                        format!("「绑定」的第四个参数要 真/假（要不要带事件信息），给的是{}",
                                crate::type_label(other)),
                    ))
                }
            };
            let mut 转 = vec![
                args[0].clone(),
                Val::Str(事件.into()),
                Val::Str(名.into()),
                Val::Bool(带信息),
            ];
            转.truncate(if args.len() >= 4 { 4 } else { 3 });
            host_call("绑定", &转)
        },
        // ---- **M229（D3）**：元素查询（桌面营造要的「Qt 式控页」）----
        // `取们` 给的是每个匹配元素的 **`数据_序`**（与 `绑定` 的回调实参同一个口径），
        // 不是 DOM 下标 —— 宿主拿到序号去对**自己的**数据表，不用再猜页面结构。
        "有元素" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("有元素", args, 1, 1)?;
            host_call("有元素", args)
        },
        "取们" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("取们", args, 1, 1)?;
            host_call("取们", args)
        },
        "取属性" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("取属性", args, 2, 2)?;
            host_call("取属性", args)
        },
        "设属性" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("设属性", args, 3, 3)?;
            host_call("设属性", args)
        },
        "焦点" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("焦点", args, 1, 1)?;
            host_call("焦点", args)
        },
        // -- 存储 --
        "存本地" => 两步!("存本地"),
        "读本地" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("读本地", args, 1, 1)?;
            host_call("读本地", args)
        },
        "删本地" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("删本地", args, 1, 1)?;
            host_call("删本地", args)
        },
        // -- 请求（异步回调） --
        "请求" => |args: &[Val], vm: &mut dyn BuiltinHost| -> R {
            // **M229（D11）**：`请求(网址, 函数名, 方法="GET", 正文=空, 头=空)`。
            // 以前只有 GET —— 桌面营造要 POST 一个表单就得绕去起一个本地服务。
            need_range("请求", args, 2, 5)?;
            let 名 = 函数名(&args[1])?;
            let 方法 = match args.get(2) {
                None | Some(Val::None_) => "GET".to_string(),
                Some(Val::Str(s)) => s.to_uppercase(),
                Some(other) => {
                    return Err(err(
                        "类型错误",
                        format!("「请求」的第三个参数要方法名（文本），给的是{}",
                                crate::type_label(other)),
                    ))
                }
            };
            // 头给字典（`{"内容类型": "application/json"}`）；序列化成 JSON 交给页面
            let 头 = match args.get(4) {
                None | Some(Val::None_) => String::new(),
                Some(Val::Dict(_)) => {
                    let 文 = crate::stdlib::jsonmod::to_text(&args[4], None)?;
                    文
                }
                Some(other) => {
                    return Err(err(
                        "类型错误",
                        format!("「请求」的第五个参数要字典（头），给的是{}",
                                crate::type_label(other)),
                    ))
                }
            };
            let 转 = vec![
                args[0].clone(),
                Val::Str(名.into()),
                Val::Str(方法.into()),
                args.get(3).cloned().unwrap_or(Val::None_),
                Val::Str(头.into()),
            ];
            let _ = vm;
            host_call("请求", &转)
        },
    }
}

/// 把「函数」转成名字。
///
/// 收 `Val::Func` / `Val::WalkFn` / 直接给的名字文本三种：
/// - 给函数值 ⇒ 取它的 `name`（`函数 点击_提交()` 就写 `点击_提交`）；
/// - 给文本 ⇒ 原样用（`绑定("#a", "点击", "点击_提交")` 也行）。
///
/// **M229（D4）**：中文事件名 → DOM 的英文名。英文名原样放行（存量程序在用）。
///
/// 🔴 **为什么要在宿主侧做这个映射**：D4 那个 bug 的形状是「`绑定("#x", "点击", f)`
/// 被原样丢给 `addEventListener`，浏览器不认中文事件名 ⇒ **静默不触发**」——
/// 点了没反应、没有报错、没有日志。所以认不出来的名字必须**当场报错**，
/// 并把手边可用的名字列出来（别让人去翻文档）。
fn 事件英文名(名: &str) -> Result<String, JishiError> {
    let 英文 = match 名 {
        "点击" => "click",
        "双击" => "dblclick",
        "输入" => "input",
        "改变" => "change",
        "提交" => "submit",
        "按下" => "keydown",
        "松开" => "keyup",
        "按键" => "keypress",
        "移动" => "mousemove",
        "悬停" => "mouseenter",
        "离开" => "mouseleave",
        "聚焦" => "focus",
        "失焦" => "blur",
        "滚动" => "scroll",
        "加载" => "load",
        // 英文原样放行：M167 起就有人直接写 `"click"`，那条路不能断
        "click" | "dblclick" | "input" | "change" | "submit" | "keydown" | "keyup"
        | "keypress" | "mousemove" | "mouseenter" | "mouseleave" | "focus" | "blur"
        | "scroll" | "load" => 名,
        other => {
            return Err(err_with_hint(
                "值错误",
                format!("「绑定」不认识事件名「{other}」"),
                "能用中文名：点击 / 双击 / 输入 / 改变 / 提交 / 按下 / 松开 / 按键 / \
                 移动 / 悬停 / 离开 / 聚焦 / 失焦 / 滚动 / 加载\
                 （也可以直接写 DOM 英文名，比如 \"click\"）"
                    .into(),
            ))
        }
    };
    Ok(英文.to_string())
}

/// ⚠️ 匿名函数（`|x| …`）**没有名字**，这里直接报错 —— 事件回调必须是
/// 具名函数，否则 JS 侧没法「按名字重新找到它」。
fn 函数名(v: &Val) -> Result<String, JishiError> {
    match v {
        Val::Str(s) if !s.is_empty() => Ok(s.to_string()),
        Val::Func(f) => Ok(f.name.clone()),
        Val::WalkFn(f) => Ok(f.name.clone()),
        _ => Err(err(
            "类型错误",
            "「绑定」/「请求」的最后一个参数要传**具名函数**（比如 `点击_提交`）。\
匿名函数没有名字，事件来的时候找不到它 —— 请先 `函数 点击_提交()：…` 再传名字。",
        )),
    }
}
