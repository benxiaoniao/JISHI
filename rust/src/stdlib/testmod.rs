//! 标准库 `测试`（Python 侧 `jishi/stdlib/测试.py`，11 个函数）。
//!
//! 极简的「用例 + 断言 + 汇总」三件套。内建 `断言` 够用但有两个缺口：
//! ① 报错只说「条件不成立」，看不出**期望什么、实际什么**；② 一个断言失败就
//! 中断整个脚本，跑不了下一组。
//!
//! ```jsh
//! 导入 测试
//! 测试.用例("加法", 函数()：测试.相等(1 + 1, 2))
//! 测试.汇总()          # 有失败就报错（CLI 退出码 1）
//! ```
//!
//! ## 状态「按每次导入隔离」
//!
//! Python 侧把 `记录` / `当前` 做成 `__基石新建__()` 里的闭包变量；宿主这边
//! `Val::Builtin` **只能存无捕获函数指针**（`Val` 被结构钉在 32 字节，见
//! `test_m82_val_size`），所以状态放 `thread_local`，并在 `module()` 里**重置**
//! —— 每次 `导入 测试` 都拿到干净的一份。Rust 宿主一个进程跑一段程序，
//! 这与 Python 侧的隔离效果一致。
//!
//! ## 已知边界（如实记，别当没有）
//!
//! * `用例` 的**返回值**：Python 返回那条记录 dict，里面 `失败` 是 **Python 元组**
//!   的列表；宿主的 `Val` 没有元组类型，这里用**二元列表**代替。正常用法
//!   （`测试.用例(...)` 当语句写）看不到差别。
//! * 「用例里出错」的**摘要文案**：Python 会把**原生异常**的类名与英文消息原样
//!   带进摘要（`TypeError（'<=' not supported …）`），宿主一律给中文基石错误
//!   （`类型错误（…）`）。两者都是「这个用例错了」，措辞不同。
//!   `tests/test_m88_testmod_rust.py` 把这两条各自钉住（不许悄悄变）。

use crate::BuiltinHost;
use std::cell::RefCell;
use std::collections::HashMap;

use super::{need_range, table, R};
use crate::errcodes;
use crate::{binop, compare, dict_new, err, list_new, truthy, ExcValue, JishiError, Val};

/// 一条用例的记录。
#[derive(Clone, Default)]
struct 用例记录 {
    名字: String,
    失败: Vec<(String, String)>,
    异常: Option<String>,
}

/// 模块状态（每次导入一份，见文件头说明）。
#[derive(Default)]
struct 状态 {
    记录: Vec<用例记录>,
    /// 当前正在跑的用例在 `记录` 里的下标；不在用例里时 `None`。
    当前: Option<usize>,
}

thread_local! {
    static 状态单元: RefCell<状态> = RefCell::new(状态::default());
}

fn 取<R>(f: impl FnOnce(&mut 状态) -> R) -> R {
    状态单元.with(|s| f(&mut s.borrow_mut()))
}

/// 记录一次失败：**在用例里**就记进用例（不中断），否则直接抛（独立用断言时）。
fn 记失败(说明: &str, 详: String) -> Result<(), JishiError> {
    let 下标 = 取(|s| s.当前.filter(|i| *i < s.记录.len()));
    match 下标 {
        Some(i) => {
            取(|s| {
                if let Some(条) = s.记录.get_mut(i) {
                    条.失败.push((说明.to_string(), 详));
                }
            });
            Ok(())
        }
        // 不在用例里：**直接抛**（独立用断言时就是这个口径）
        None => Err(err("断言错误", format!("{说明}：{详}"))),
    }
}

/// 一次断言的完整动作：条件不成立时按上面的口径记一笔。
fn 断言(成立: bool, 说明: &str, 详: String) -> Result<(), JishiError> {
    if 成立 {
        return Ok(());
    }
    记失败(说明, 详)
}

/// 「期望 / 实际」里那个值的写法 —— 与 Python 的 `jishi_repr(v, top=True)` 同口径
/// （顶层文本**不加引号**、空/真/假中文化）。
fn 文本(vm: &mut dyn BuiltinHost, v: &Val) -> String {
    vm.format_value(v, true).unwrap_or_else(|_| crate::display(v))
}

/// 可选的第 3 个参数（说明），缺省空串。
fn 说明参数(args: &[Val]) -> String {
    match args.get(2) {
        Some(Val::Str(s)) => s.clone(),
        _ => String::new(),
    }
}

/// 出错摘要：`名（消息）`；没有消息就只给名。
///
/// ⚠️ 这个「名」取的是 **`JishiError.title`**（Python 侧 `_摘要` 用的就是它），
/// **不是** `捕获 X` 匹配的那个类型名 —— 两者在 Python 里是分开的：
/// `RunIndexError` 的类型名是「索引错误」、而 title 是「索引越界」。
/// 宿主这边 type_name 是「可匹配的名字」，title 得靠错误码查
/// `errcodes::title`（那张表就是从 `errors.py` 搬来的，m85 钉着）。
fn 摘要(e: &JishiError) -> String {
    let 名 = match e.code.as_deref().and_then(errcodes::title) {
        Some(t) => t.to_string(),
        None => e.type_name.clone(),
    };
    if e.message.is_empty() {
        名
    } else {
        format!("{名}（{}）", e.message)
    }
}

/// 把算出来的数字取绝对值（`abs`）。
fn 取绝对值(v: Val) -> Val {
    match v {
        Val::Int(i) => Val::Int(i.abs()),
        Val::Float(f) => Val::Float(f.abs()),
        其它 => 其它,
    }
}

pub(crate) fn module() -> HashMap<String, Val> {
    // 每次 `导入 测试` 重置 —— 对齐 Python 侧「按每次导入隔离」（`__基石新建__`）。
    取(|s| *s = 状态::default());

    table! { "测试";
        "重置" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("重置", args, 0, 0)?;
            取(|s| s.记录.clear());
            Ok(Val::None_)
        },
        "用例" => |args: &[Val], vm: &mut dyn BuiltinHost| -> R {
            need_range("用例", args, 1, 2)?;
            let 名字 = 文本(vm, &args[0]);
            let 函数 = match args.get(1) {
                Some(v) if !matches!(v, Val::None_) => v.clone(),
                _ => {
                    return Err(err("运行期错误",
                        "「测试.用例」要一个函数，写成 测试.用例(\"名字\", 函数()：…)"));
                }
            };
            let 下标 = 取(|s| {
                s.记录.push(用例记录 { 名字: 名字.clone(), ..Default::default() });
                s.记录.len() - 1
            });
            let 旧 = 取(|s| s.当前.replace(下标));
            // 用例里的**任何**错误都接住（断言失败已由 `相等` 等记过，这里只记
            // 「用例本身崩了」）——与 Python 侧 `except BaseException` 同口径。
            let 结果 = vm.call_value(函数, Vec::new());
            取(|s| s.当前 = 旧);
            if let Err(e) = 结果 {
                if e.type_name != "断言错误" {
                    let 摘 = 摘要(&e);
                    取(|s| {
                        if let Some(条) = s.记录.get_mut(下标) {
                            条.异常 = Some(摘);
                        }
                    });
                }
            }
            // 返回那条记录（Python 侧返回 dict；`失败` 里 Python 是元组，
            // 这里用二元列表 —— 见文件头「已知边界」）。
            let 条 = 取(|s| s.记录.get(下标).cloned().unwrap_or_default());
            let 失败: Vec<Val> = 条.失败.iter()
                .map(|(a, b)| list_new(vec![Val::Str(a.clone()), Val::Str(b.clone())]))
                .collect();
            Ok(dict_new(vec![
                (Val::Str("名字".to_string()), Val::Str(条.名字)),
                (Val::Str("失败".to_string()), list_new(失败)),
                (Val::Str("异常".to_string()), match 条.异常 {
                    Some(s) => Val::Str(s),
                    None => Val::None_,
                }),
            ]))
        },
        "相等" => |args: &[Val], vm: &mut dyn BuiltinHost| -> R {
            need_range("相等", args, 2, 3)?;
            let (实际, 期望) = (args[0].clone(), args[1].clone());
            let 成立 = truthy(compare(0, 实际.clone(), 期望.clone())?);
            断言(成立, &{
                let s = 说明参数(args);
                if s.is_empty() { "相等".to_string() } else { s }
            }, format!("期望 {}，实际 {}", 文本(vm, &期望), 文本(vm, &实际)))?;
            Ok(Val::None_)
        },
        "不等" => |args: &[Val], vm: &mut dyn BuiltinHost| -> R {
            need_range("不等", args, 2, 3)?;
            let (实际, 不该等于) = (args[0].clone(), args[1].clone());
            let 相等 = truthy(compare(0, 实际.clone(), 不该等于.clone())?);
            let 说明 = 说明参数(args);
            断言(!相等, if 说明.is_empty() { "不等" } else { &说明 },
                 format!("不该等于 {}，但实际就是它", 文本(vm, &不该等于)))?;
            Ok(Val::None_)
        },
        "为真" => |args: &[Val], vm: &mut dyn BuiltinHost| -> R {
            need_range("为真", args, 1, 2)?;
            let 条件 = args[0].clone();
            let 成立 = truthy(条件.clone());
            let 说明 = 说明参数(args);
            断言(成立, if 说明.is_empty() { "为真" } else { &说明 },
                 format!("期望为真，实际是 {}", 文本(vm, &条件)))?;
            Ok(Val::None_)
        },
        "为假" => |args: &[Val], vm: &mut dyn BuiltinHost| -> R {
            need_range("为假", args, 1, 2)?;
            let 条件 = args[0].clone();
            let 成立 = truthy(条件.clone());
            let 说明 = 说明参数(args);
            断言(!成立, if 说明.is_empty() { "为假" } else { &说明 },
                 format!("期望为假，实际是 {}", 文本(vm, &条件)))?;
            Ok(Val::None_)
        },
        "近似" => |args: &[Val], vm: &mut dyn BuiltinHost| -> R {
            need_range("近似", args, 2, 4)?;
            let (实际, 期望) = (args[0].clone(), args[1].clone());
            let 误差 = args.get(2).cloned().unwrap_or(Val::Float(1e-9));
            let 说明 = 说明参数(args);
            // ⚠️ 与 Python 同结构：**只有算差值那一步**的错才翻成「不能比大小」，
            // 后面比较那一步的错照原样往外抛（Python 侧 TypeError 也是这么分的）。
            let 差 = match binop(1, 实际.clone(), 期望.clone()) {
                Ok(v) => 取绝对值(v),
                Err(e) if e.type_name == "类型错误" => {
                    let 详 = format!("「{}」与「{}」不能比大小",
                                     文本(vm, &实际), 文本(vm, &期望));
                    记失败(if 说明.is_empty() { "近似" } else { &说明 }, 详)?;
                    return Ok(Val::None_);
                }
                Err(e) => return Err(e),
            };
            let 成立 = truthy(compare(4, 差, 误差.clone())?);
            断言(成立, if 说明.is_empty() { "近似" } else { &说明 },
                 format!("期望 {}（±{}），实际 {}",
                         文本(vm, &期望), 文本(vm, &误差), 文本(vm, &实际)))?;
            Ok(Val::None_)
        },
        "包含" => |args: &[Val], vm: &mut dyn BuiltinHost| -> R {
            need_range("包含", args, 2, 3)?;
            let (容器, 元素) = (args[0].clone(), args[1].clone());
            let 说明 = 说明参数(args);
            let 有 = match compare(6, 元素.clone(), 容器.clone()) {
                Ok(v) => truthy(v),
                Err(e) if e.type_name == "类型错误" => {
                    记失败(if 说明.is_empty() { "包含" } else { &说明 },
                           format!("「{}」不能判包含", 文本(vm, &容器)))?;
                    return Ok(Val::None_);
                }
                Err(e) => return Err(e),
            };
            断言(有, if 说明.is_empty() { "包含" } else { &说明 },
                 format!("{} 里没有 {}", 文本(vm, &容器), 文本(vm, &元素)))?;
            Ok(Val::None_)
        },
        "抛出" => |args: &[Val], vm: &mut dyn BuiltinHost| -> R {
            need_range("抛出", args, 1, 2)?;
            let 函数 = args[0].clone();
            let 说明 = 说明参数(args);
            match vm.call_value(函数, Vec::new()) {
                // 抛出来的错误**当成值返回**（可以再看它的类型）——
                // 与 Python 侧 `except BaseException as e: return e` 同口径。
                Err(e) => Ok(ExcValue::from_error(&e)),
                Ok(_) => {
                    记失败(if 说明.is_empty() { "抛出" } else { &说明 },
                           "期望报错，但它正常跑完了".to_string())?;
                    Ok(Val::None_)
                }
            }
        },
        "统计" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("统计", args, 0, 0)?;
            let (用例, 通过, 失败, 出错) = 取(|s| {
                let 失败: usize = s.记录.iter().map(|t| t.失败.len()).sum();
                let 出错 = s.记录.iter().filter(|t| t.异常.is_some()).count();
                let 通过 = s.记录.iter()
                    .filter(|t| t.失败.is_empty() && t.异常.is_none())
                    .count();
                (s.记录.len(), 通过, 失败, 出错)
            });
            Ok(dict_new(vec![
                (Val::Str("用例".to_string()), Val::Int(用例 as i128)),
                (Val::Str("通过".to_string()), Val::Int(通过 as i128)),
                (Val::Str("失败".to_string()), Val::Int(失败 as i128)),
                (Val::Str("出错".to_string()), Val::Int(出错 as i128)),
            ]))
        },
        "汇总" => |args: &[Val], vm: &mut dyn BuiltinHost| -> R {
            need_range("汇总", args, 0, 0)?;
            let 全部 = 取(|s| s.记录.clone());
            let 用例数 = 全部.len();
            let 失败数: usize = 全部.iter().map(|t| t.失败.len()).sum();
            let 出错数 = 全部.iter().filter(|t| t.异常.is_some()).count();
            let 通过数 = 全部.iter()
                .filter(|t| t.失败.is_empty() && t.异常.is_none()).count();
            // 失败明细走**错误流**、汇总行走**输出流**（Python 侧就是这个口径）。
            for 条 in &全部 {
                if let Some(异) = &条.异常 {
                    vm.write_stderr(&format!("✗ {}：用例里出错 —— {异}\n", 条.名字));
                }
                for (说明, 详) in &条.失败 {
                    vm.write_stderr(&format!("✗ {} · {说明}：{详}\n", 条.名字));
                }
            }
            let 全过 = 失败数 == 0 && 出错数 == 0;
            let 标记 = if 全过 { "✅" } else { "❌" };
            let 尾巴 = format!("（{用例数} 个用例）");
            if 全过 {
                vm.write_output(&format!("{标记} 全部通过 {尾巴}\n"));
            } else {
                vm.write_output(&format!(
                    "{标记} 通过 {通过数} / 失败 {失败数} / 出错 {出错数} {尾巴}\n"));
            }
            if !全过 {
                // 抛断言错误表达失败 —— 脚本里设不了退出码，但抛出去 CLI 会以 1 退出。
                return Err(err("断言错误",
                    format!("测试没全过：失败 {失败数} 项、出错 {出错数} 项")));
            }
            Ok(Val::Bool(true))
        },
    }
}
