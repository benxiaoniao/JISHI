//! 基石标准库：通知（M221）。
//!
//! 与 `窗口.提示` 的区别，一句话：**提示点了没回调，通知点了有回调**。
//!
//! | 场景 | 行为 |
//! |---|---|
//! | Rust 原生窗口（Windows） | 托盘气泡 + 点击派发「通知点击」（`Shell_NotifyIconW`） |
//! | Rust 原生窗口（mac / Linux） | `osascript` / `notify-send` —— **只发，不回调** |
//! | 页面里（wasm） | 返回假 + 一句说明（页面要弹提示用浏览器自己的 Notification） |
//! | Python / Node 宿主 | 返回假 + 一句说明（没有原生窗口） |
//!
//! ## 为什么「发」不报错、只返回假
//!
//! 通知是**锦上添花**：没有通知能力时程序该照常跑完，不该崩。所以口径是
//! 「有能力就发、没有返回假」，但**不静默** —— 往 stderr 写一句为什么。
//! 这与 `窗口.提示` 一致（它也返回真假）。
//!
//! 📌 `当("点击")` 则相反：它**只能写在页面里**（脚本那时正阻塞在 `窗口.等`
//! 上，接不了事件），写错了必须报错 —— 静默不生效的订阅最难查。
//!
//! 📌 为什么 `发` 不返回「句柄」（路线图原话是「→ 句柄」）：Windows 上
//! `Shell_NotifyIconW` 一个 uID 只有一条气泡，新的一条只能顶掉旧的，句柄
//! 区分不了什么；给个假句柄比给真假更糟。`关闭()` 撤当前那条就够。

use std::collections::HashMap;

use crate::BuiltinHost;
use crate::Val;

use super::{need, table, R};

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "通知";
        "发" => |args: &[Val], vm: &mut dyn BuiltinHost| -> R {
            need("发", args, 2)?;
            let 标题 = 文本参数(&args[0], "标题")?;
            let 正文 = 文本参数(&args[1], "正文")?;
            #[cfg(all(not(target_arch = "wasm32"), feature = "native-window"))]
            {
                return match crate::native_window::通知发(&标题, &正文) {
                    Ok(()) => Ok(Val::Bool(true)),
                    Err(原因) => {
                        vm.write_stderr(&format!("通知没发出去：{原因}\n"));
                        Ok(Val::Bool(false))
                    }
                };
            }
            #[cfg(not(all(not(target_arch = "wasm32"), feature = "native-window")))]
            {
                let _ = (标题, 正文);
                vm.write_stderr(&format!("{}\n", 没有能力()));
                Ok(Val::Bool(false))
            }
        },
        // `通知.关闭()` —— 撤掉当前那条（Windows 上是托盘里的临时图标）。
        "关闭" => |args: &[Val], vm: &mut dyn BuiltinHost| -> R {
            need("关闭", args, 0)?;
            #[cfg(all(not(target_arch = "wasm32"), feature = "native-window"))]
            {
                return Ok(Val::Bool(crate::native_window::通知关()));
            }
            #[cfg(not(all(not(target_arch = "wasm32"), feature = "native-window")))]
            {
                let _ = &vm;
                Ok(Val::Bool(false))
            }
        },
        // `通知.当("点击", 函数)` —— 点了通知之后跑哪个函数。**页面里**登记。
        "当" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("当", args, 2)?;
            let 名 = match &args[0] {
                Val::Str(s) => s.to_string(),
                other => {
                    return Err(crate::err(
                        "类型错误",
                        format!(
                            "「通知.当」的事件名要文本（现在只有 \"点击\"），给的是{}",
                            crate::type_label(other)
                        ),
                    ))
                }
            };
            if 名 != "点击" {
                return Err(crate::err_with_hint(
                    "值错误",
                    format!("「通知.当」只认「点击」，给的是「{名}」"),
                    "要「通知发出去之后」做点什么，直接写在 通知.发 下一行".into(),
                ));
            }
            let 函数 = crate::stdlib::window::当函数名(&args[1])?;
            #[cfg(target_arch = "wasm32")]
            {
                let 文 = format!(
                    "{{\"事件\":{},\"函数\":{}}}",
                    crate::jsonw::esc("通知点击"),
                    crate::jsonw::esc(&函数)
                );
                return Ok(Val::Bool(crate::abi3::发("当", &文) > 0));
            }
            #[cfg(not(target_arch = "wasm32"))]
            {
                let _ = 函数;
                Err(crate::err_with_hint(
                    "运行期错误",
                    "「通知.当」只在**页面里**有效 —— 脚本这会儿正阻塞在 窗口.等 上，接不了事件",
                    "把 通知.当 写进页面的基石代码里".into(),
                ))
            }
        },
    }
}

fn 文本参数(v: &Val, 名: &str) -> Result<String, crate::JishiError> {
    match v {
        Val::Str(s) => Ok(s.to_string()),
        other => Err(crate::err(
            "类型错误",
            format!(
                "「通知.发」的{名}要文本，给的是{}",
                crate::type_label(other)
            ),
        )),
    }
}

/// 「这个宿主没有通知能力」的统一说法。
///
/// ⚠️ 文案**不**随宿主变：判据要对拍 stderr，而且用户在哪台机器上都该
/// 看到同一句「为什么没发出去」。
fn 没有能力() -> &'static str {
    #[cfg(target_arch = "wasm32")]
    {
        return "通知没发出去：页面里没有宿主通知能力（要弹提示用浏览器的 Notification API）";
    }
    #[cfg(all(not(target_arch = "wasm32"), not(feature = "native-window")))]
    {
        return "通知没发出去：这个构建没有原生窗口（只有 Rust 发行版带原生窗口）";
    }
    #[cfg(all(not(target_arch = "wasm32"), feature = "native-window"))]
    {
        "通知没发出去：这个平台没有可用的通知程序"
    }
}
