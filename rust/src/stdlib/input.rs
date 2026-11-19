//! 基石标准库：输入映射与手柄（M208）。
//!
//! ## 为什么要有「动作」这一层
//!
//! 没有它，游戏里到处都是 `如果 图形.按着("a") 或 图形.按着("ArrowLeft") …`，
//! 想加个手柄或改个键就得满篇改；而且「这一帧按过」这种边沿语义在
//! `或` 里会**多消费**（`按过("a") 或 按过("b")` 两个都被清掉）。
//!
//! 有了 `操控`：
//!
//! ```jishi
//! 操控.绑定("左", ["a", "ArrowLeft"])
//! 操控.绑定("右", ["d", "ArrowRight"])
//! 如果 操控.按下("左")： …
//! 令 方向 = 操控.轴("左", "右")     # -1 / 0 / 1
//! ```
//!
//! ## 🔴 为什么不叫「输入」
//!
//! `输入()` 是**内建函数**（从命令行读一行）。同名模块一导入就会打一句
//! 「内建 `输入(...)` 会被模块「输入」遮蔽」—— 每个游戏文件都刷一行提示，
//! 而那句提示对写游戏的人毫无用处。所以模块叫 `操控`（键位 / 手柄的「操控」），
//! 函数名照旧（`绑定` / `按下` / `按过` / `轴` / `手柄`）。
//! 📌 路线图里写的 `输入.绑定` 在实现时改成了 `操控.绑定`，理由就是这条。
//!
//! ## 边界：**只在页面 / 窗口里有**
//!
//! | 宿主 | 状态 |
//! |---|---|
//! | 操作 | 网页 / 窗口 | 命令行 / Node |
//! |---|---|---|
//! | `绑定` / `解绑` / `绑定表` | ✅ | ✅（**纯逻辑**：绑完不查键，随时能读回来） |
//! | `按下` / `按过` / `轴` / `手柄` | ✅ 真读设备 | ❌ 报**同一句**平台错 |
//!
//! 与 `图形` / `声音` 同一条边界：不假装「按键永远是松开」——那会让同一段
//! 游戏代码在两个宿主上行为不同，而且**看不出是平台的问题**。
//!
//! ## 键名口径
//!
//! 与 `图形.按着` **完全同一套**（就是浏览器的 `KeyboardEvent.key`）：
//! `"a"` / `"空格"` / `"ArrowLeft"` / `"Enter"` / `"Escape"`，数字键 `"1"`。
//! 单个字符不分大小写（`图形` 侧统一转小写）；功能键按原名写。
//!
//! 📌 绑定表存在 **Rust 侧**（不是 shim）：它是纯逻辑，改了立刻生效，
//! 也方便 `绑定表()` 原样读回来。shim 那边只负责「某个键现在按着吗」。

use std::cell::RefCell;
use std::collections::HashMap;

use crate::{err, err_with_hint, BuiltinHost, JishiError, Val};

use super::browser::host_call;
use super::{need, need_range, table, R};

/// 平台错。**三个宿主各写一份，必须逐字相同**（判据盯着）。
pub(crate) const 无输入: &str =
    "这个平台没有键盘 / 手柄：「操控」模块只在网页 / 窗口里有（命令行和 Node 上没有输入设备）。";

fn 要输入() -> Result<(), JishiError> {
    #[cfg(target_arch = "wasm32")]
    {
        Ok(())
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        Err(err("运行期错误", 无输入))
    }
}

thread_local! {
    /// 动作 → 键名们。**按登记顺序**保留（`绑定表()` 读回来的顺序才稳定）。
    ///
    /// 用 `Vec` 而不是 `HashMap`：一个游戏的动作数是个位数，顺序稳定比查找
    /// 快慢重要得多（`绑定表()` 的输出要能对拍）。
    static 绑定表: RefCell<Vec<(String, Vec<String>)>> = const { RefCell::new(Vec::new()) };
}

/// 收动作名（非空文本）。
fn 动作(v: &Val, 名: &str) -> Result<String, JishiError> {
    let s = match v {
        Val::Str(s) => s.to_string(),
        other => {
            return Err(err(
                "类型错误",
                format!(
                    "「{名}」的动作名要文本（例如 \"左\"），给的是{}",
                    crate::type_label(other)
                ),
            ))
        }
    };
    if s.trim().is_empty() {
        return Err(err("值错误", format!("「{名}」的动作名不能是空文本")));
    }
    Ok(s)
}

/// 收「键们」：一个文本，或一串文本。
fn 键们(v: &Val, 名: &str) -> Result<Vec<String>, JishiError> {
    let 收一个 = |x: &Val| -> Result<String, JishiError> {
        match x {
            Val::Str(s) => {
                if s.trim().is_empty() {
                    Err(err("值错误", format!("「{名}」的键名不能是空文本")))
                } else {
                    Ok(s.to_string())
                }
            }
            other => Err(err(
                "类型错误",
                format!(
                    "「{名}」的键名要文本（例如 \"a\" / \"空格\" / \"ArrowLeft\"），给的是{}",
                    crate::type_label(other)
                ),
            )),
        }
    };
    match v {
        Val::Str(_) => Ok(vec![收一个(v)?]),
        Val::List(items) => {
            let 表 = items.borrow();
            let mut 出 = Vec::with_capacity(表.len());
            for it in 表.iter() {
                出.push(收一个(it)?);
            }
            Ok(出)
        }
        other => Err(err(
            "类型错误",
            format!(
                "「{名}」的键们要给文本或文本列表（例如 [\"a\", \"ArrowLeft\"]），给的是{}",
                crate::type_label(other)
            ),
        )),
    }
}

/// 有没有哪个键**按着**（任何一个真就算真）。
fn 任按着(键s: &[String]) -> Result<bool, JishiError> {
    let mut 有 = false;
    // ⚠️ **不许短路**：`按着` 没有副作用，看起来短路无所谓，但同一段逻辑
    //    在 `任按过` 里必须全查一遍（那边会清边沿）—— 两处保持一个写法，
    //    以后改一处不会只改半边。
    for k in 键s {
        if crate::truthy(host_call("按着", &[Val::Str(k.as_str().into())])?) {
            有 = true;
        }
    }
    Ok(有)
}

/// 有没有哪个键**这一帧按过**（边沿；查过的键都会被清）。
fn 任按过(键s: &[String]) -> Result<bool, JishiError> {
    let mut 有 = false;
    for k in 键s {
        if crate::truthy(host_call("按过", &[Val::Str(k.as_str().into())])?) {
            有 = true;
        }
    }
    Ok(有)
}

/// 取某动作的键们；没绑定 ⇒ 报一句「先绑定」。
fn 键s_of(动: &str, 谁: &str) -> Result<Vec<String>, JishiError> {
    let 找 = 绑定表.with(|t| {
        t.borrow()
            .iter()
            .find(|(k, _)| k == 动)
            .map(|(_, v)| v.clone())
    });
    match 找 {
        Some(v) => Ok(v),
        None => Err(err_with_hint(
            "名称错误",
            format!("「{谁}」的动作「{动}」还没绑定键"),
            format!("先写 `输入.绑定(\"{动}\", [\"a\"])`（`输入.绑定表()` 能看现在有哪些动作）"),
        )),
    }
}

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "操控";
        // 绑定(动作, 键们)
        "绑定" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("绑定", args, 2)?;
            let 动 = 动作(&args[0], "绑定")?;
            let ks = 键们(&args[1], "绑定")?;
            // 去重但保序：同一动作写两次 `"a"` 不该变成两次宿主动作
            let mut 清: Vec<String> = Vec::with_capacity(ks.len());
            for k in ks {
                if !清.iter().any(|x| x == &k) {
                    清.push(k);
                }
            }
            let 空 = 清.is_empty();
            绑定表.with(|t| {
                let mut 表 = t.borrow_mut();
                表.retain(|(k, _)| k != &动);
                if !空 {
                    表.push((动.clone(), 清.clone()));
                }
            });
            Ok(Val::Bool(!空))
        },
        // 解绑(动作) —— 没绑过也当成功（幂等）
        "解绑" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("解绑", args, 1)?;
            let 动 = 动作(&args[0], "解绑")?;
            let 有 = 绑定表.with(|t| {
                let 前 = t.borrow().len();
                t.borrow_mut().retain(|(k, _)| k != &动);
                前 != t.borrow().len()
            });
            Ok(Val::Bool(有))
        },
        "按下" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("按下", args, 1)?;
            要输入()?;
            let 动 = 动作(&args[0], "按下")?;
            let ks = 键s_of(&动, "按下")?;
            Ok(Val::Bool(任按着(&ks)?))
        },
        "按过" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("按过", args, 1)?;
            要输入()?;
            let 动 = 动作(&args[0], "按过")?;
            let ks = 键s_of(&动, "按过")?;
            Ok(Val::Bool(任按过(&ks)?))
        },
        // 轴(负动作, 正动作) → -1 / 0 / 1
        "轴" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("轴", args, 2)?;
            要输入()?;
            let 负 = 动作(&args[0], "轴")?;
            let 正 = 动作(&args[1], "轴")?;
            let k负 = 键s_of(&负, "轴")?;
            let k正 = 键s_of(&正, "轴")?;
            let a = 任按着(&k负)?;
            let b = 任按着(&k正)?;
            // 两边都按着 ⇒ 0（不是「都算」，那会让角色原地抖）；与手柄的死区同口径
            Ok(Val::Int(match (a, b) {
                (true, true) => 0,
                (true, false) => -1,
                (false, true) => 1,
                (false, false) => 0,
            }))
        },
        // 绑定表() → {动作: [键们], …}（顺序 = 绑定顺序）
        "绑定表" => |_a: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            let 项 = 绑定表.with(|t| {
                t.borrow()
                    .iter()
                    .map(|(k, v)| {
                        (
                            Val::Str(k.as_str().into()),
                            crate::list_new(
                                v.iter().map(|x| Val::Str(x.as_str().into())).collect(),
                            ),
                        )
                    })
                    .collect::<Vec<_>>()
            });
            Ok(crate::dict_new(项))
        },
        // 手柄(序号 = 0) → {连接, 左x, 左y, 右x, 右y, 按钮们, 按过们}
        "手柄" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("手柄", args, 0, 1)?;
            要输入()?;
            let 号 = match args.first() {
                None | Some(Val::None_) => 0.0,
                Some(v) => super::as_num(v)?,
            };
            if 号 < 0.0 {
                return Err(err("值错误", format!("「手柄」的序号不能是负数，现在是 {号}")));
            }
            // shim 那边读 `navigator.getGamepads()`；**没连手柄也给一个完整的字典**
            // （`连接: 假`），不要返回 `空` —— 那样 `手柄()` 后面每一处都要判空。
            host_call("手柄", &[Val::Float(号)])
        },
    }
}
