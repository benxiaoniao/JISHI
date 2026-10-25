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
    /// `测试.跳过(...)` 给的原因；`Some` 就是跳过（**不算通过、也不算失败**）。
    跳过: Option<String>,
}

/// 模块状态（每次导入一份，见文件头说明）。
#[derive(Default)]
struct 状态 {
    记录: Vec<用例记录>,
    /// 当前正在跑的用例在 `记录` 里的下标；不在用例里时 `None`。
    当前: Option<usize>,
    /// `测试.每例前` / `测试.每例后` 设的函数（没设就是 None）。
    前: Option<Val>,
    后: Option<Val>,
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

/// 把「后置出错」这类**附加**错误并进该用例（保留已有那条）。
fn 追加用例错(下标: usize, 摘: String) {
    取(|s| {
        if let Some(条) = s.记录.get_mut(下标) {
            条.异常 = match 条.异常.take() {
                None => Some(摘),
                Some(旧) => Some(format!("{旧}；{摘}")),
            };
        }
    });
}

/// 把「用例里抛出来的错」按三种口径记下来：跳过 / 用例崩了 / 断言失败（已记过）。
fn 记用例抛错(下标: usize, e: &JishiError, 前缀: &str) {
    if e.type_name == "跳过用例" {
        let 因 = e.message.clone();
        取(|s| {
            if let Some(条) = s.记录.get_mut(下标) {
                条.跳过 = Some(因);
            }
        });
    } else if e.type_name != "断言错误" {
        let 摘 = format!("{前缀}{}", 摘要(e));
        追加用例错(下标, 摘);
    }
}

/// 跑**一条**用例（含前后置）—— `用例` 和 `用例们` 共用这一份。
///
/// 与 Python 侧 `_跑一条` 逐条对齐：
/// - 前置出错/跳过 ⇒ **函数体不跑**，但后置**照跑**（finally 语义）；
/// - 后置出错 ⇒ **算这条用例出错**（它污染的是后面的用例，不能吞）。
fn 跑一条(vm: &mut dyn BuiltinHost, 名字: String, 函数: Val, 参数们: Vec<Val>) -> Val {
    let 下标 = 取(|s| {
        s.记录.push(用例记录 { 名字, ..Default::default() });
        s.记录.len() - 1
    });
    let 旧 = 取(|s| s.当前.replace(下标));

    let mut 能跑 = true;
    if let Some(f) = 取(|s| s.前.clone()) {
        if let Err(e) = vm.call_value(f, Vec::new()) {
            记用例抛错(下标, &e, "前置出错 —— ");
            能跑 = false;
        }
    }
    if 能跑 {
        if let Err(e) = vm.call_value(函数, 参数们) {
            记用例抛错(下标, &e, "");
        }
    }
    if let Some(f) = 取(|s| s.后.clone()) {
        if let Err(e) = vm.call_value(f, Vec::new()) {
            追加用例错(下标, format!("后置出错 —— {}", 摘要(&e)));
        }
    }
    取(|s| s.当前 = 旧);

    let 条 = 取(|s| s.记录.get(下标).cloned().unwrap_or_default());
    let 失败: Vec<Val> = 条.失败.iter()
        .map(|(a, b)| list_new(vec![Val::Str(a.clone()), Val::Str(b.clone())]))
        .collect();
    dict_new(vec![
        (Val::Str("名字".to_string()), Val::Str(条.名字)),
        (Val::Str("失败".to_string()), list_new(失败)),
        (Val::Str("异常".to_string()), match 条.异常 {
            Some(s) => Val::Str(s),
            None => Val::None_,
        }),
        (Val::Str("跳过".to_string()), match 条.跳过 {
            Some(s) => Val::Str(s),
            None => Val::None_,
        }),
    ])
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
            Ok(跑一条(vm, 名字, 函数, Vec::new()))
        },
        // M136：同一件检查喂**多组数据**各跑一遍。
        // 每组**整组**作为一个参数传进去（不展开）—— 展开的话
        // 「一组两个元素」和「两个参数」就分不清了。
        "用例们" => |args: &[Val], vm: &mut dyn BuiltinHost| -> R {
            need_range("用例们", args, 2, 3)?;
            let 名字 = 文本(vm, &args[0]);
            let 函数 = match args.get(2) {
                Some(v) if !matches!(v, Val::None_) => v.clone(),
                _ => {
                    return Err(err("运行期错误",
                        "「测试.用例们」要一个函数，写成 测试.用例们(\"名字\", 数据们, 函数)"));
                }
            };
            let 组们 = match &args[1] {
                Val::List(l) => l.borrow().clone(),
                _ => {
                    return Err(err("运行期错误",
                        "「测试.用例们」的第二个参数（数据们）要是一个列表"));
                }
            };
            let mut 结果们 = Vec::new();
            for (i, 组) in 组们.into_iter().enumerate() {
                结果们.push(跑一条(vm, format!("{名字}[{}]", i + 1), 函数.clone(),
                                  vec![组]));
            }
            Ok(list_new(结果们))
        },
        // M136：这个用例不适用 —— 记「跳过」，后面的断言别再跑。
        "跳过" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("跳过", args, 0, 1)?;
            let 在用例里 = 取(|s| s.当前.is_some());
            if !在用例里 {
                return Err(err("运行期错误",
                    "「测试.跳过」只能在用例里用（在用例外面调它等于把整份脚本静默跳过，那不是它的意思）"));
            }
            let 因 = match args.first() {
                Some(v) => 文本(_vm, v),
                None => String::new(),
            };
            // ⚠️ 用**独立的类型名**当控制流标记：`AssertionError`（断言错误）不能被
            //    误认成跳过，`运行期错误` 也不行 —— 用户程序里可能真有那种错误。
            Err(err("跳过用例", 因))
        },
        "每例前" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("每例前", args, 1, 1)?;
            let f = args[0].clone();
            取(|s| s.前 = Some(f));
            Ok(Val::None_)
        },
        "每例后" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("每例后", args, 1, 1)?;
            let f = args[0].clone();
            取(|s| s.后 = Some(f));
            Ok(Val::None_)
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
            let (用例, 通过, 失败, 出错, 跳过) = 取(|s| {
                let 失败: usize = s.记录.iter().map(|t| t.失败.len()).sum();
                let 出错 = s.记录.iter().filter(|t| t.异常.is_some()).count();
                let 跳过 = s.记录.iter()
                    .filter(|t| t.跳过.is_some() && t.异常.is_none())
                    .count();
                let 通过 = s.记录.iter()
                    .filter(|t| t.失败.is_empty() && t.异常.is_none()
                             && t.跳过.is_none())
                    .count();
                (s.记录.len(), 通过, 失败, 出错, 跳过)
            });
            Ok(dict_new(vec![
                (Val::Str("用例".to_string()), Val::Int(用例 as i128)),
                (Val::Str("通过".to_string()), Val::Int(通过 as i128)),
                (Val::Str("失败".to_string()), Val::Int(失败 as i128)),
                (Val::Str("出错".to_string()), Val::Int(出错 as i128)),
                (Val::Str("跳过".to_string()), Val::Int(跳过 as i128)),
            ]))
        },
        "汇总" => |args: &[Val], vm: &mut dyn BuiltinHost| -> R {
            need_range("汇总", args, 0, 0)?;
            let 全部 = 取(|s| s.记录.clone());
            let 用例数 = 全部.len();
            let 失败数: usize = 全部.iter().map(|t| t.失败.len()).sum();
            let 出错数 = 全部.iter().filter(|t| t.异常.is_some()).count();
            let 跳过数 = 全部.iter()
                .filter(|t| t.跳过.is_some() && t.异常.is_none()).count();
            let 通过数 = 全部.iter()
                .filter(|t| t.失败.is_empty() && t.异常.is_none()
                         && t.跳过.is_none()).count();
            // 失败明细走**错误流**、汇总行走**输出流**（Python 侧就是这个口径）。
            for 条 in &全部 {
                if let Some(异) = &条.异常 {
                    vm.write_stderr(&format!("✗ {}：用例里出错 —— {异}\n", 条.名字));
                }
                for (说明, 详) in &条.失败 {
                    vm.write_stderr(&format!("✗ {} · {说明}：{详}\n", 条.名字));
                }
            }
            // ⏭ 明细走**输出流**（跳过不是错误），且在汇总行之前
            for 条 in &全部 {
                if let (Some(因), None) = (&条.跳过, &条.异常) {
                    let 尾 = if 因.is_empty() { String::new() } else { format!("：{因}") };
                    vm.write_output(&format!("⏭ {}{}\n", 条.名字, 尾));
                }
            }
            let 全过 = 失败数 == 0 && 出错数 == 0;
            let 标记 = if 全过 { "✅" } else { "❌" };
            let 尾巴 = if 跳过数 == 0 {
                format!("（{用例数} 个用例）")
            } else {
                format!("（{用例数} 个用例，跳过 {跳过数}）")
            };
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
