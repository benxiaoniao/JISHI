//! 基石标准库：声音（M187）—— 用 Web Audio 放声音。
//!
//! ## 边界：**只在页面 / 窗口里有**
//!
//! | 宿主 | 状态 |
//! |---|---|
//! | 网页（wasm）· 原生窗口 · 应用模式窗口 | ✅ 真放（shim 侧 `Audio`） |
//! | 命令行 · Node | ❌ 报**同一句**中文平台错 |
//!
//! 与 `图形` / `浏览器` 同一条边界：不给「假装能放」的降级实现（那会让同一段游戏
//! 代码在两个宿主上行为不同）。
//!
//! ## 🔴 浏览器的自动播放策略（用的人一定会撞）
//!
//! 页面加载后、用户**还没点过/按过键盘之前**，`AudioContext` 是 `suspended` 的，
//! 这时播放**不会响**（也不报错）。所以：
//!
//! - `播放` 在没解锁时**返回假**（不是报错）—— 调用方据此知道「等用户动一下」；
//! - shim 在**第一次按键 / 点击**里 `resume()`（那一次手势就是解锁的钥匙）；
//! - 文档写清「先进游戏按一下」。
//!
//! 📌 这是浏览器定的规矩，不是我们的限制 —— 假装能放才是最坏的。

use std::collections::HashMap;

use crate::{err, err_with_hint, BuiltinHost, JishiError, Val};

use super::browser::{esc, host_call};
use super::{need, need_range, table, R};

/// 平台错。**三个宿主各写一份，必须逐字相同**（判据盯着）。
pub(crate) const 无声音: &str = "这个平台没有声音：「声音」模块只在网页 / 窗口里有\
（命令行和 Node 上没有音频输出）。";

fn 要声音() -> Result<(), JishiError> {
    #[cfg(target_arch = "wasm32")]
    {
        Ok(())
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        Err(err("运行期错误", 无声音))
    }
}

/// 收一个数（整数 / 浮点都收）。
fn 数(v: &Val, 名: &str, 参: &str) -> Result<f64, JishiError> {
    match v {
        Val::Int(i) => Ok(*i as f64),
        Val::Float(f) => Ok(*f),
        other => Err(err(
            "类型错误",
            format!("「{名}」的「{参}」要数字，给的是{}", crate::type_label(other)),
        )),
    }
}

/// 收一个句柄（`载入` 的返回值）。
fn 句柄(v: &Val, 名: &str) -> Result<f64, JishiError> {
    match v {
        Val::Int(i) => Ok(*i as f64),
        Val::Float(f) => Ok(*f),
        Val::Bool(b) => Ok(if *b { 1.0 } else { 0.0 }),
        other => Err(err_with_hint(
            "类型错误",
            format!("「{名}」的第一个参数要声音句柄（`声音.载入` 的返回值），给的是{}",
                crate::type_label(other)),
            "先 `令 音 = 声音.载入(\"点击.wav\")`，再把 `音` 传进来".into(),
        )),
    }
}

/// 选项字典 → JSON（只认 `循环` / `音量` / `速度`）。
fn 选项json(v: Option<&Val>) -> Result<String, JishiError> {
    let Some(v) = v else { return Ok("{}".to_string()) };
    if matches!(v, Val::None_) {
        return Ok("{}".to_string());
    }
    let Val::Dict(d) = v else {
        return Err(err(
            "类型错误",
            format!("「声音」的选项要字典，给的是{}", crate::type_label(v)),
        ));
    };
    let mut 段: Vec<String> = Vec::new();
    for (k, val) in d.borrow().to_items() {
        let k = crate::display(&k);
        match k.as_str() {
            "循环" => 段.push(format!("\"{}\":{}", esc(&k), 是非(&val)?)),
            "音量" | "速度" => 段.push(format!("\"{}\":{}", esc(&k), 数(&val, "声音", &k)?)),
            other => {
                return Err(err_with_hint(
                    "值错误",
                    format!("声音选项里没有「{other}」这一项"),
                    "能用的是：循环（真/假）/ 音量（0~1）/ 速度（1 = 原速）".into(),
                ))
            }
        }
    }
    Ok(format!("{{{}}}", 段.join(",")))
}

/// 宽松地收一个布尔（`真` / `假`）。
fn 是非(v: &Val) -> Result<String, JishiError> {
    match v {
        Val::Bool(b) => Ok((if *b { "true" } else { "false" }).to_string()),
        Val::Int(i) => Ok((if *i != 0 { "true" } else { "false" }).to_string()),
        other => Err(err(
            "类型错误",
            format!("这个选项要 `真` / `假`，给的是{}", crate::type_label(other)),
        )),
    }
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "声音";
        "载入" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("载入", args, 1)?;
            要声音()?;
            let 路径 = match &args[0] {
                Val::Str(s) => s.to_string(),
                other => {
                    return Err(err(
                        "类型错误",
                        format!("「声音.载入」要文件路径（文本），给的是{}", crate::type_label(other)),
                    ))
                }
            };
            // shim 回一个整数句柄（**立刻可用**：真正的解码是异步的，
            // 没解完之前 `播放` 返回假，见 `无声音` 上面那段）
            host_call("声音载入", &[Val::Str(路径.into())])
        },
        "播放" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("播放", args, 1, 2)?;
            要声音()?;
            let 号 = 句柄(&args[0], "播放")?;
            let 选 = 选项json(args.get(1))?;
            // 返回 `真` = 真在放了；`假` = 还没解锁 / 还没解码完（**不报错**）
            host_call("声音播放", &[Val::Float(号), Val::Str(选.into())])
        },
        "停止" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("停止", args, 1)?;
            要声音()?;
            let 号 = 句柄(&args[0], "停止")?;
            host_call("声音停止", &[Val::Float(号)])
        },
        "在放" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("在放", args, 1)?;
            要声音()?;
            let 号 = 句柄(&args[0], "在放")?;
            host_call("声音在放", &[Val::Float(号)])
        },
        "设音量" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("设音量", args, 2)?;
            要声音()?;
            let 号 = 句柄(&args[0], "设音量")?;
            let 量 = 数(&args[1], "设音量", "音量")?;
            host_call("声音音量", &[Val::Float(号), Val::Float(量)])
        },
        "主音量" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("主音量", args, 0, 1)?;
            要声音()?;
            match args.first() {
                // 不给参数 = 读当前主音量
                None | Some(Val::None_) => host_call("声音读主音量", &[]),
                Some(v) => {
                    let 量 = 数(v, "主音量", "音量")?;
                    host_call("声音主音量", &[Val::Float(量)])
                }
            }
        },
    }
}
