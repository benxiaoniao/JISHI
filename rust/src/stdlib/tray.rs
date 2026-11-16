//! 基石标准库：托盘（M185）。
//!
//! 0.3.1 的托盘只能在 `窗口.设托盘({...})` 那一次给全 —— 换菜单、改提示都得
//! 先关再建（图标会闪一下，事件也可能丢）。这个模块就两件事：**改菜单**、
//! **改提示**。
//!
//! ## 三路分派
//!
//! | 场景 | 行为 |
//! |---|---|
//! | 页面里（wasm） | 报「托盘是宿主的事」—— 页面代码摸不到托盘 |
//! | 有 `native-window` | 真改（`tray-icon` 的 `set_menu` / `set_tooltip`） |
//! | 没有 `native-window` | 报「这个构建没有原生窗口」 |
//!
//! ⚠️ **每条分支都得自己 `return`**：wasm 构建里 `native_window` 模块整个不存在
//! （`default-features = false`），若把返回只写在 `#[cfg]` 块里，另一边就只剩
//! `()` —— M185 第一版就是这么错的，**三平台 CI 全卡在「构建 wasm 引擎」**。
//!
//! ⚠️ **还没设托盘就改**：报一句「先 窗口.设托盘」—— 不静默返回假，
//! 那看起来像「改失败了」，其实是用错了顺序。

use std::collections::HashMap;

use crate::BuiltinHost;
use crate::Val;

use super::{need, table, R};

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "托盘";
        "设菜单" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("设菜单", args, 1)?;
            // 菜单定义（列表）先序列化成 JSON 再交给宿主侧 —— **直接复用
            // `窗口.设菜单` 那条路**（`菜单json`），格式与行为完全一致：
            // 托盘菜单和菜单栏本来就是同一套格式，抄两份迟早会漂。
            let 文 = super::window::菜单json(&args[0])?;
            #[cfg(all(not(target_arch = "wasm32"), feature = "native-window"))]
            {
                return crate::native_window::托盘设菜单(&文)
                    .map(|_| Val::Bool(true))
                    .map_err(|e| {
                        crate::err_with_hint("运行期错误", e, "先 窗口.设托盘({...})".into())
                    });
            }
            #[cfg(not(all(not(target_arch = "wasm32"), feature = "native-window")))]
            {
                let _ = 文;
                Err(做不到("设菜单"))
            }
        },
        "设提示" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("设提示", args, 1)?;
            let 文 = match &args[0] {
                Val::Str(s) => s.to_string(),
                other => {
                    return Err(crate::err(
                        "类型错误",
                        format!("「托盘.设提示」要文本，给的是{}", crate::type_label(other)),
                    ))
                }
            };
            #[cfg(all(not(target_arch = "wasm32"), feature = "native-window"))]
            {
                return crate::native_window::托盘设提示(&文)
                    .map(|_| Val::Bool(true))
                    .map_err(|e| {
                        crate::err_with_hint("运行期错误", e, "先 窗口.设托盘({...})".into())
                    });
            }
            #[cfg(not(all(not(target_arch = "wasm32"), feature = "native-window")))]
            {
                let _ = 文;
                Err(做不到("设提示"))
            }
        },
    }
}

/// 两条**明确的**「做不到」：页面里 / 这个构建没有原生窗口。
///
/// 分开说，因为它们是两件不同的事 —— 混成一句的话，页面里调的人会去翻
/// 「怎么开原生窗口」的文档，那不是他要找的。
#[cfg(not(all(not(target_arch = "wasm32"), feature = "native-window")))]
fn 做不到(名: &str) -> crate::JishiError {
    #[cfg(target_arch = "wasm32")]
    {
        crate::err_with_hint(
            "运行期错误",
            format!("「托盘.{名}」在页面里没有意义（托盘是宿主的事）"),
            "托盘要在**脚本**里建和改，不是页面代码里".into(),
        )
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        crate::err_with_hint(
            "运行期错误",
            format!("这个构建没有原生窗口，「托盘.{名}」用不了"),
            "用默认构建（带 native-window）".into(),
        )
    }
}
