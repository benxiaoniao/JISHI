//! 基石标准库：游戏工具箱（M187）—— 精灵 / 瓦片 / 地图 / 存档 / 场景。
//!
//! ## 为什么五个模块放一个文件
//!
//! 它们是一条线上的东西（做 2D 游戏要用的那几件），而且共享同一批小工具
//! （读字典字段、收数字、拼选项 JSON）。分五个文件就是五份一模一样的样板，
//! 改一处要改五遍 —— 合在一起更实在。
//!
//! ## 边界
//!
//! | 模块 | 命令行 / Node | 页面 / 窗口 |
//! |---|---|---|
//! | `精灵` `瓦片` | 纯逻辑部分能用；`画` / `载入` 报**画布**平台错 | ✅ |
//! | `地图` | ✅（`读` 读文件） | ✅（传字典，不传路径 —— wasm 没有文件系统） |
//! | `存档` | ❌ 报**页面**平台错（要 `localStorage`） | ✅ |
//! | `场景` | ✅（纯状态机） | ✅ |
//!
//! 🔴 **和路线图原计划不一样的一处**：原计划这五个**用基石自己写**（`stdlib-jishi/*.jsh`）。
//! 实际做的时候发现那条路走不通 —— Rust 侧和 wasm 沙箱**都不展开库层**
//! （`sandbox.rs` 里 `lib_dir = None`），而 `精灵` / `瓦片` 的全部价值就在
//! **页面里能画**（它们要调 `图形.画子图`）。写进 `.jsh` 的话，浏览器试玩页
//! （M188 的核心交付）与 CLI 都用不了。⇒ 按既有模块的老路走：三宿主原生 + 对拍。
//! 取舍记在 `docs/设计决策.md` **D111**。

use std::cell::RefCell;
use std::collections::HashMap;

use crate::{dict_get, dict_new, dict_set, err, err_with_hint, BuiltinHost, JishiError, Val};
use super::as_text;

use super::graphics::调;
//: `browser::esc` 只转义**不加引号**（键名要自己补引号）；`jsonw::esc` 自带引号。
//: 两者混淆过一次，见下面「精灵选项」那段注释。
use super::browser::esc;
use super::{need, need_range, table, R};

// ---------------------------------------------------------------------------
// 小工具（五个模块共用）
// ---------------------------------------------------------------------------

/// 收字典（不是字典就报清楚）。
fn 字典<'a>(v: &'a Val, 名: &str, 参: &str) -> Result<&'a std::rc::Rc<RefCell<crate::Dict>>, JishiError> {
    match v {
        Val::Dict(d) => Ok(d),
        other => Err(err(
            "类型错误",
            format!("「{名}」的「{参}」要字典，给的是{}", crate::type_label(other)),
        )),
    }
}

/// 读一个字段。
fn 取(d: &crate::Dict, 键: &str) -> Option<Val> {
    dict_get(d, &Val::Str(键.into()))
}

/// 写一个字段。
fn 设(d: &mut crate::Dict, 键: &str, v: Val) {
    dict_set(d, Val::Str(键.into()), v);
}

/// 从瓦片图字典取它的二维行们（M197：碰撞 / 斜坡 / 取格都要用）。
fn 图的格们(d: &std::rc::Rc<RefCell<crate::Dict>>) -> Result<Vec<Vec<i64>>, JishiError> {
    let v = {
        let dd = d.borrow();
        取(&dd, "行们").unwrap_or(Val::None_)
    };
    二维(&v, "瓦片", "行们")
}

/// 从瓦片图读一格（越界 / 空 = -1，与 `瓦片.取` 同口径）。
fn 格号(d: &std::rc::Rc<RefCell<crate::Dict>>, 列: i64, 行: i64) -> Result<i64, JishiError> {
    let 行们 = 图的格们(d)?;
    if 列 < 0 || 行 < 0 {
        return Ok(-1);
    }
    Ok(行们
        .get(行 as usize)
        .and_then(|r| r.get(列 as usize))
        .copied()
        .unwrap_or(-1))
}

/// 从字典里取一个**必有**的数（缺了就报「缺 xxx」）。
///
/// 与 `取必` + `数` 分开：矩形 `{x,y,宽,高}` 缺一项时要说清**缺哪一项**，
/// 而不是笼统的「参数不对」—— 少写 `高` 而默认成 0 的话，碰撞区会是空的。
fn 字典里的数(d: &crate::Dict, 键: &str, 谁: &str) -> Result<f64, JishiError> {
    match 取(d, 键) {
        Some(v) => 数(&v, 谁, 键),
        None => Err(err(
            "值错误",
            format!("「{谁}」的矩形里缺「{键}」"),
        )),
    }
}

/// 读一个**必有**的字段。
fn 取必(d: &crate::Dict, 键: &str, 谁: &str) -> Result<Val, JishiError> {
    取(d, 键).ok_or_else(|| {
        err_with_hint(
            "值错误",
            format!("「{谁}」要的字典里缺「{键}」"),
            "这个字典应当由本模块自己的构造函数生成".into(),
        )
    })
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

/// 收一个整数。
fn 整数(v: &Val, 名: &str, 参: &str) -> Result<i64, JishiError> {
    match v {
        Val::Int(i) => i64::try_from(*i)
            .map_err(|_| err("值错误", format!("「{名}」的「{参}」超出整数范围"))),
        Val::Float(f) if f.fract() == 0.0 => Ok(*f as i64),
        other => Err(err(
            "类型错误",
            format!("「{名}」的「{参}」要整数，给的是{}", crate::type_label(other)),
        )),
    }
}

/// 收一个文本。
fn 文本(v: &Val, 名: &str, 参: &str) -> Result<String, JishiError> {
    match v {
        Val::Str(s) => Ok(s.to_string()),
        other => Err(err(
            "类型错误",
            format!("「{名}」的「{参}」要文本，给的是{}", crate::type_label(other)),
        )),
    }
}

/// 收一个布尔（`真` / `假`，也收 0/1）。
fn 布尔(v: &Val, 名: &str, 参: &str) -> Result<bool, JishiError> {
    match v {
        Val::Bool(b) => Ok(*b),
        Val::Int(i) => Ok(*i != 0),
        other => Err(err(
            "类型错误",
            format!("「{名}」的「{参}」要 `真` / `假`，给的是{}", crate::type_label(other)),
        )),
    }
}

/// 二维整数数组 → `Vec<Vec<i64>>`（`瓦片` 的地图数据）。
fn 二维(v: &Val, 名: &str, 参: &str) -> Result<Vec<Vec<i64>>, JishiError> {
    let Val::List(rows) = v else {
        return Err(err(
            "类型错误",
            format!("「{名}」的「{参}」要二维列表（每行一个列表），给的是{}",
                crate::type_label(v)),
        ));
    };
    let 行们 = rows.borrow();
    let mut 出 = Vec::with_capacity(行们.len());
    for (i, r) in 行们.iter().enumerate() {
        let Val::List(cols) = r else {
            return Err(err(
                "值错误",
                format!("「{名}」的「{参}」第 {} 行不是列表（是{}）", i + 1, crate::type_label(r)),
            ));
        };
        let mut 行 = Vec::with_capacity(cols.borrow().len());
        for (j, c) in cols.borrow().iter().enumerate() {
            match c {
                Val::Int(n) => 行.push(i64::try_from(*n).unwrap_or(i64::MAX)),
                // `空` 当「没有瓦片」—— 手写地图数据时比写 `-1` 自然
                Val::None_ => 行.push(-1),
                other => {
                    return Err(err(
                        "值错误",
                        format!("「{名}」的「{参}」第 {} 行第 {} 列要整数（-1 = 空），给的是{}",
                            i + 1, j + 1, crate::type_label(other)),
                    ))
                }
            }
        }
        出.push(行);
    }
    Ok(出)
}

/// 二维整数数组 → `Val::List`（构造返回值用）。
fn 二维值(行们: &[Vec<i64>]) -> Val {
    crate::list_new(
        行们
            .iter()
            .map(|r| crate::list_new(r.iter().map(|n| Val::Int(*n as i128)).collect()))
            .collect(),
    )
}

// ---------------------------------------------------------------------------
// 精灵（7）
// ---------------------------------------------------------------------------

pub(crate) fn sprite_module() -> HashMap<String, Val> {
    table! { "精灵";
        // 新建(图, {"宽","高","列数"?,"行数"?})
        "新建" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("新建", args, 2)?;
            let 图 = 数(&args[0], "新建", "图")?;
            let d = 字典(&args[1], "新建", "选项")?;
            let 宽 = 数(&取必(&d.borrow(), "宽", "新建")?, "新建", "宽")?;
            let 高 = 数(&取必(&d.borrow(), "高", "新建")?, "新建", "高")?;
            if 宽 <= 0.0 || 高 <= 0.0 {
                return Err(err("值错误", "「精灵.新建」的宽高必须大于 0"));
            }
            let 列 = match 取(&d.borrow(), "列数") {
                Some(v) => 整数(&v, "新建", "列数")?,
                None => 1,
            };
            let 行 = match 取(&d.borrow(), "行数") {
                Some(v) => 整数(&v, "新建", "行数")?,
                None => 1,
            };
            Ok(dict_new(vec![
                (Val::Str("图".into()), Val::Float(图)),
                (Val::Str("帧宽".into()), Val::Float(宽)),
                (Val::Str("帧高".into()), Val::Float(高)),
                (Val::Str("列数".into()), Val::Int(列.max(1) as i128)),
                (Val::Str("行数".into()), Val::Int(行.max(1) as i128)),
                (Val::Str("当前".into()), Val::Int(0)),
                (Val::Str("在播".into()), Val::Str("".into())),
                (Val::Str("累计".into()), Val::Float(0.0)),
                (Val::Str("动画".into()), dict_new(Vec::new())),
                // M197：锚点（比例）与「非循环播完了」的标志
                (Val::Str("原点x".into()), Val::Float(0.0)),
                (Val::Str("原点y".into()), Val::Float(0.0)),
                (Val::Str("完成".into()), Val::Bool(false)),
            ]))
        },
        // 播(精灵, 名, {"帧们"?,"速度"?,"循环"?})
        "播" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("播", args, 2, 3)?;
            let d = 字典(&args[0], "播", "精灵")?;
            let 名 = 文本(&args[1], "播", "动画名")?;
            let (mut 帧们, 速度, 循环) = match args.get(2) {
                None | Some(Val::None_) => (Vec::new(), 12.0, true),
                Some(o) => {
                    let od = 字典(o, "播", "选项")?;
                    let od = od.borrow();
                    let mut f = Vec::new();
                    if let Some(v) = 取(&od, "帧们") {
                        let Val::List(l) = &v else {
                            return Err(err("类型错误", "「播」的「帧们」要列表"));
                        };
                        for x in l.borrow().iter() {
                            f.push(整数(x, "播", "帧们")?);
                        }
                    }
                    let s = match 取(&od, "速度") {
                        Some(v) => 数(&v, "播", "速度")?,
                        None => 12.0,
                    };
                    let c = match 取(&od, "循环") {
                        Some(v) => 布尔(&v, "播", "循环")?,
                        None => true,
                    };
                    (f, s, c)
                }
            };
            if 速度 <= 0.0 {
                return Err(err("值错误", "「播」的速度必须大于 0（单位：帧/秒）"));
            }
            {
                let dd = d.borrow();
                if 帧们.is_empty() {
                    // 没给帧们 ⇒ 整个图集按顺序播
                    let n = match 取(&dd, "列数") {
                        Some(Val::Int(c)) => c as i64,
                        _ => 1,
                    } * match 取(&dd, "行数") {
                        Some(Val::Int(r)) => r as i64,
                        _ => 1,
                    };
                    帧们 = (0..n.max(1)).collect();
                }
            }
            let mut dd = d.borrow_mut();
            let 表 = dict_new(vec![
                (Val::Str("帧们".into()), crate::list_new(帧们.iter().map(|n| Val::Int(*n as i128)).collect())),
                (Val::Str("速度".into()), Val::Float(速度)),
                (Val::Str("循环".into()), Val::Bool(循环)),
            ]);
            if let Some(Val::Dict(动画)) = 取(&dd, "动画") {
                dict_set(&mut 动画.borrow_mut(), Val::Str(名.as_str().into()), 表);
            }
            设(&mut dd, "在播", Val::Str(名.as_str().into()));
            设(&mut dd, "累计", Val::Float(0.0));
            设(&mut dd, "当前", Val::Int(帧们[0] as i128));
            // M197：重新播就把「播完了」清掉
            设(&mut dd, "完成", Val::Bool(false));
            Ok(args[0].clone())
        },
        // 停(精灵)
        "停" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("停", args, 1)?;
            let d = 字典(&args[0], "停", "精灵")?;
            let mut dd = d.borrow_mut();
            设(&mut dd, "在播", Val::Str("".into()));
            Ok(Val::Bool(true))
        },
        // 设帧(精灵, 号)
        "设帧" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("设帧", args, 2)?;
            let d = 字典(&args[0], "设帧", "精灵")?;
            let 号 = 整数(&args[1], "设帧", "号")?;
            let mut dd = d.borrow_mut();
            设(&mut dd, "当前", Val::Int(号 as i128));
            Ok(Val::Bool(true))
        },
        // 更新(精灵, 间隔毫秒) —— 推进动画
        "更新" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("更新", args, 2)?;
            let d = 字典(&args[0], "更新", "精灵")?;
            let 间隔 = 数(&args[1], "更新", "间隔")?;
            let mut dd = d.borrow_mut();
            let 在播 = match 取(&dd, "在播") {
                Some(Val::Str(s)) => s.to_string(),
                _ => String::new(),
            };
            if 在播.is_empty() || 间隔 <= 0.0 {
                return Ok(Val::Bool(false));
            }
            let (帧们, 速度, 循环) = {
                let 动画 = match 取(&dd, "动画") {
                    Some(Val::Dict(a)) => a,
                    _ => return Ok(Val::Bool(false)),
                };
                let a = 动画.borrow();
                let Some(Val::Dict(项)) = dict_get(&a, &Val::Str(在播.as_str().into())) else {
                    return Ok(Val::Bool(false));
                };
                let 项 = 项.borrow();
                let mut 帧们: Vec<i64> = Vec::new();
                if let Some(Val::List(l)) = 取(&项, "帧们") {
                    for x in l.borrow().iter() {
                        if let Val::Int(n) = x {
                            帧们.push(*n as i64);
                        }
                    }
                }
                let 速度 = match 取(&项, "速度") {
                    Some(Val::Float(f)) => f,
                    Some(Val::Int(i)) => i as f64,
                    _ => 12.0,
                };
                let 循环 = matches!(取(&项, "循环"), Some(Val::Bool(true)));
                (帧们, 速度, 循环)
            };
            if 帧们.is_empty() || 速度 <= 0.0 {
                return Ok(Val::Bool(false));
            }
            let 每帧 = 1000.0 / 速度;
            let mut 累计 = match 取(&dd, "累计") {
                Some(Val::Float(f)) => f,
                _ => 0.0,
            } + 间隔;
            let mut 当前 = match 取(&dd, "当前") {
                Some(Val::Int(i)) => i as i64,
                _ => 帧们[0],
            };
            let mut 位 = 帧们.iter().position(|n| *n == 当前).unwrap_or(0);
            let mut 变 = false;
            // ⚠️ 上限 1000 次：间隔特别大（比如卡了一秒）时不要把整段动画一口气跑完
            let mut 次 = 0;
            while 累计 >= 每帧 && 次 < 1000 {
                累计 -= 每帧;
                次 += 1;
                if 位 + 1 < 帧们.len() {
                    位 += 1;
                } else if 循环 {
                    位 = 0;
                } else {
                    // 非循环：停在最后一帧，并记下「播完了」——`动画完成` 读它
                    累计 = 0.0;
                    设(&mut dd, "完成", Val::Bool(true));
                    break;
                }
                当前 = 帧们[位];
                变 = true;
            }
            设(&mut dd, "累计", Val::Float(累计));
            if 变 {
                设(&mut dd, "当前", Val::Int(当前 as i128));
            }
            Ok(Val::Bool(变))
        },
        // 画(精灵, x, y, 选项?)
        "画" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("画", args, 3, 4)?;
            let d = 字典(&args[0], "画", "精灵")?;
            let (图, 帧宽, 帧高, 列数, 当前) = {
                let dd = d.borrow();
                (
                    数(&取必(&dd, "图", "精灵")?, "画", "图")?,
                    数(&取必(&dd, "帧宽", "精灵")?, "画", "帧宽")?,
                    数(&取必(&dd, "帧高", "精灵")?, "画", "帧高")?,
                    整数(&取必(&dd, "列数", "精灵")?, "画", "列数")?,
                    整数(&取必(&dd, "当前", "精灵")?, "画", "当前")?,
                )
            };
            let x = 数(&args[1], "画", "x")?;
            let y = 数(&args[2], "画", "y")?;
            // M197：原点按**比例**偏移（默认 0,0 = 左上角，与 M187 行为一致）
            let (ox, oy) = {
                let dd = d.borrow();
                (
                    取(&dd, "原点x").and_then(|v| 数(&v, "画", "原点x").ok()).unwrap_or(0.0),
                    取(&dd, "原点y").and_then(|v| 数(&v, "画", "原点y").ok()).unwrap_or(0.0),
                )
            };
            let x = x - ox * 帧宽;
            let y = y - oy * 帧高;
            let 列 = if 列数 > 0 { 当前.rem_euclid(列数) } else { 0 };
            let 行 = if 列数 > 0 { 当前.div_euclid(列数) } else { 0 };
            let sx = 列 as f64 * 帧宽;
            let sy = 行 as f64 * 帧高;
            let 选 = match args.get(3) {
                Some(Val::Str(s)) if s.is_empty() => "{}".to_string(),
                Some(v) => 选json(Some(v))?,
                None => "{}".to_string(),
            };
            调("画子图", &[
                Val::Float(图),
                Val::Float(sx),
                Val::Float(sy),
                Val::Float(帧宽),
                Val::Float(帧高),
                Val::Float(x),
                Val::Float(y),
                Val::Float(帧宽),
                Val::Float(帧高),
                Val::Str(选.into()),
            ])
        },
        // 当前帧(精灵)
        "当前帧" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("当前帧", args, 1)?;
            let d = 字典(&args[0], "当前帧", "精灵")?;
            let dd = d.borrow();
            Ok(取必(&dd, "当前", "精灵")?)
        },

        // ---- M197：图集切分 / 动画完成 / 原点 ------------------------------
        "图集切分" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("图集切分", args, 4)?;
            let 图宽 = 数(&args[0], "图集切分", "图宽")?;
            let 图高 = 数(&args[1], "图集切分", "图高")?;
            let 帧宽 = 数(&args[2], "图集切分", "帧宽")?;
            let 帧高 = 数(&args[3], "图集切分", "帧高")?;
            for (名, v) in [("图宽", 图宽), ("图高", 图高), ("帧宽", 帧宽), ("帧高", 帧高)] {
                if v <= 0.0 {
                    return Err(err(
                        "值错误",
                        format!("「图集切分」的{名}必须大于 0，现在是 {v}"),
                    ));
                }
            }
            if 帧宽 > 图宽 || 帧高 > 图高 {
                return Err(err(
                    "值错误",
                    format!("「图集切分」的帧（{帧宽}×{帧高}）比图（{图宽}×{图高}）还大"),
                ));
            }
            // 除不尽的残条丢掉 —— 半帧的图放出来是错的
            let 列数 = (图宽 / 帧宽).floor() as i64;
            let 行数 = (图高 / 帧高).floor() as i64;
            let 帧数 = 列数 * 行数;
            Ok(dict_new(vec![
                (Val::Str("列数".into()), Val::Int(列数 as i128)),
                (Val::Str("行数".into()), Val::Int(行数 as i128)),
                (Val::Str("帧数".into()), Val::Int(帧数 as i128)),
                (
                    Val::Str("帧们".into()),
                    crate::list_new((0..帧数).map(|n| Val::Int(n as i128)).collect()),
                ),
            ]))
        },
        "动画完成" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("动画完成", args, 1)?;
            let d = 字典(&args[0], "动画完成", "精灵")?;
            let dd = d.borrow();
            Ok(Val::Bool(matches!(取(&dd, "完成"), Some(Val::Bool(true)))))
        },
        "设原点" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("设原点", args, 1, 3)?;
            let d = 字典(&args[0], "设原点", "精灵")?;
            let ox = match args.get(1) {
                None | Some(Val::None_) => 0.5,
                Some(v) => 数(v, "设原点", "x")?,
            };
            let oy = match args.get(2) {
                None | Some(Val::None_) => 0.5,
                Some(v) => 数(v, "设原点", "y")?,
            };
            if !(0.0..=1.0).contains(&ox) || !(0.0..=1.0).contains(&oy) {
                return Err(err(
                    "值错误",
                    format!("「设原点」的两个比例要在 0–1 之间，现在是 {ox} / {oy}"),
                ));
            }
            {
                let mut dd = d.borrow_mut();
                设(&mut dd, "原点x", Val::Float(ox));
                设(&mut dd, "原点y", Val::Float(oy));
            }
            Ok(args[0].clone())
        },
    }
}

/// 图像选项（与 `图形.画图` 同一套键）。
fn 选json(v: Option<&Val>) -> Result<String, JishiError> {
    let Some(v) = v else { return Ok("{}".to_string()) };
    if matches!(v, Val::None_) {
        return Ok("{}".to_string());
    }
    let d = 字典(v, "画", "选项")?;
    let mut 段: Vec<String> = Vec::new();
    for (k, val) in d.borrow().to_items() {
        let k = crate::display(&k);
        match k.as_str() {
            // ⚠️ 键名用 `esc`（**不加引号**，这里自己补），值里的文本用
            //    `jsonw::esc`（**自带引号**）。
            //    🔴 M188 这里原来两处都写成了 `jsonw::esc`，于是拼出
            //    `{""翻转"":"横"}` —— JSON 解析在**位置 3** 就炸，
            //    报「Expected ':' after property name」，而且**看不出跟翻转有关**。
            //    （和 M187 存档索引那条是同一类错：esc 到底带不带引号。）
            "旋转" | "缩放" | "透明度" => {
                段.push(format!("\"{}\":{}", esc(&k), 数(&val, "画", &k)?))
            }
            "翻转" => 段.push(format!("\"{}\":{}", esc(&k), crate::jsonw::esc(&文本(&val, "画", &k)?))),
            other => {
                return Err(err_with_hint(
                    "值错误",
                    format!("精灵的选项里没有「{other}」这一项"),
                    "能用的是：旋转 / 缩放 / 翻转 / 透明度".into(),
                ))
            }
        }
    }
    Ok(format!("{{{}}}", 段.join(",")))
}

// ---------------------------------------------------------------------------
// 瓦片（4）
// ---------------------------------------------------------------------------

pub(crate) fn tile_module() -> HashMap<String, Val> {
    table! { "瓦片";
        // 载入({"图集","瓦片宽","瓦片高","行们"})
        "载入" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("载入", args, 1)?;
            let d = 字典(&args[0], "载入", "选项")?;
            let (图集, 宽, 高, 行们) = {
                let dd = d.borrow();
                (
                    数(&取必(&dd, "图集", "瓦片")?, "载入", "图集")?,
                    数(&取必(&dd, "瓦片宽", "瓦片")?, "载入", "瓦片宽")?,
                    数(&取必(&dd, "瓦片高", "瓦片")?, "载入", "瓦片高")?,
                    match 取(&dd, "行们") {
                        Some(v) => 二维(&v, "载入", "行们")?,
                        None => Vec::new(),
                    },
                )
            };
            if 宽 <= 0.0 || 高 <= 0.0 {
                return Err(err("值错误", "「瓦片.载入」的瓦片宽高必须大于 0"));
            }
            let 列数 = 行们.iter().map(|r| r.len()).max().unwrap_or(0) as i128;
            Ok(dict_new(vec![
                (Val::Str("图集".into()), Val::Float(图集)),
                (Val::Str("瓦片宽".into()), Val::Float(宽)),
                (Val::Str("瓦片高".into()), Val::Float(高)),
                (Val::Str("行们".into()), 二维值(&行们)),
                (Val::Str("列数".into()), Val::Int(列数)),
                (Val::Str("行数".into()), Val::Int(行们.len() as i128)),
            ]))
        },
        // 画(瓦片图, 相机?) —— 相机 {"x","y","宽","高"}（世界坐标 + 视口大小）
        "画" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("画", args, 1, 2)?;
            let d = 字典(&args[0], "画", "瓦片图")?;
            let (图集, 宽, 高, 行们, 列数) = {
                let dd = d.borrow();
                (
                    数(&取必(&dd, "图集", "瓦片图")?, "画", "图集")?,
                    数(&取必(&dd, "瓦片宽", "瓦片图")?, "画", "瓦片宽")?,
                    数(&取必(&dd, "瓦片高", "瓦片图")?, "画", "瓦片高")?,
                    match 取(&dd, "行们") {
                        Some(v) => 二维(&v, "画", "行们")?,
                        None => Vec::new(),
                    },
                    整数(&取必(&dd, "列数", "瓦片图")?, "画", "列数")?,
                )
            };
            let _ = 列数;
            let (vx, vy, vw, vh) = match args.get(1) {
                None | Some(Val::None_) => (0.0, 0.0, f64::INFINITY, f64::INFINITY),
                Some(c) => {
                    let cd = 字典(c, "画", "相机")?;
                    let cd = cd.borrow();
                    (
                        match 取(&cd, "x") { Some(v) => 数(&v, "画", "相机x")?, None => 0.0 },
                        match 取(&cd, "y") { Some(v) => 数(&v, "画", "相机y")?, None => 0.0 },
                        match 取(&cd, "宽") { Some(v) => 数(&v, "画", "相机宽")?, None => f64::INFINITY },
                        match 取(&cd, "高") { Some(v) => 数(&v, "画", "相机高")?, None => f64::INFINITY },
                    )
                }
            };
            let mut 画了 = 0i64;
            for (r, 行) in 行们.iter().enumerate() {
                for (c, 号) in 行.iter().enumerate() {
                    if *号 < 0 {
                        continue;      // -1 = 空
                    }
                    let x = c as f64 * 宽;
                    let y = r as f64 * 高;
                    // 视口裁剪：屏幕外的瓦片一条都不发（大地图上这是主要开销）
                    if x + 宽 < vx || x > vx + vw || y + 高 < vy || y > vy + vh {
                        continue;
                    }
                    let 列 = (*号 as f64) % 列数.max(1) as f64;
                    let 源行 = (*号 as f64 / 列数.max(1) as f64).floor();
                    调("画子图", &[
                        Val::Float(图集),
                        Val::Float(列 * 宽),
                        Val::Float(源行 * 高),
                        Val::Float(宽),
                        Val::Float(高),
                        Val::Float(x),
                        Val::Float(y),
                        Val::Float(宽),
                        Val::Float(高),
                        Val::Str("{}".into()),
                    ])?;
                    画了 += 1;
                }
            }
            Ok(Val::Int(画了 as i128))
        },
        // 碰(瓦片图, {"x","y","宽","高"}) → 命中的瓦片号（去重、升序）
        "碰" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("碰", args, 2)?;
            let d = 字典(&args[0], "碰", "瓦片图")?;
            let (宽, 高, 行们) = {
                let dd = d.borrow();
                (
                    数(&取必(&dd, "瓦片宽", "瓦片图")?, "碰", "瓦片宽")?,
                    数(&取必(&dd, "瓦片高", "瓦片图")?, "碰", "瓦片高")?,
                    match 取(&dd, "行们") {
                        Some(v) => 二维(&v, "碰", "行们")?,
                        None => Vec::new(),
                    },
                )
            };
            let bd = 字典(&args[1], "碰", "矩形")?;
            let bd = bd.borrow();
            let bx = 数(&取必(&bd, "x", "矩形")?, "碰", "x")?;
            let by = 数(&取必(&bd, "y", "矩形")?, "碰", "y")?;
            let bw = 数(&取必(&bd, "宽", "矩形")?, "碰", "宽")?;
            let bh = 数(&取必(&bd, "高", "矩形")?, "碰", "高")?;
            let c0 = (bx / 宽).floor().max(0.0) as usize;
            let c1 = ((bx + bw) / 宽).floor().max(0.0) as usize;
            let r0 = (by / 高).floor().max(0.0) as usize;
            let r1 = ((by + bh) / 高).floor().max(0.0) as usize;
            let mut 中: Vec<i64> = Vec::new();
            for r in r0..=r1 {
                for c in c0..=c1 {
                    if let Some(行) = 行们.get(r) {
                        if let Some(号) = 行.get(c) {
                            if *号 >= 0 && !中.contains(号) {
                                中.push(*号);
                            }
                        }
                    }
                }
            }
            中.sort_unstable();
            Ok(crate::list_new(中.into_iter().map(|n| Val::Int(n as i128)).collect()))
        },
        // 取(瓦片图, 列, 行) → 号（越界 / 空 = -1）
        "取" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("取", args, 3)?;
            let d = 字典(&args[0], "取", "瓦片图")?;
            let 列 = 整数(&args[1], "取", "列")?;
            let 行 = 整数(&args[2], "取", "行")?;
            let dd = d.borrow();
            let 号 = match 取(&dd, "行们") {
                Some(v) => {
                    let 行们 = 二维(&v, "取", "行们")?;
                    行们
                        .get(行.max(0) as usize)
                        .and_then(|r| r.get(列.max(0) as usize))
                        .copied()
                        .unwrap_or(-1)
                }
                None => -1,
            };
            Ok(Val::Int(号 as i128))
        },

        // ---- M197：碰撞层 / 斜坡 / 矩形取格 / 最近可走格 --------------------
        //
        // 三条规则（与 Python / Node 逐字一致）：
        // 1. **碰撞层是「哪些瓦片号算实心」的名单**，不改原图 —— 同一个图可以
        //    建好几层（地面一层、水一层）；
        // 2. **斜坡只记方向**（左/右），不做物理；键用**文本** `"列,行"`，
        //    这是三宿主唯一能一致的表示；
        // 3. **`最近可走格` 一圈圈向外找**，最多 16 圈，找不到给 `空`
        //    （不给「假装找到的」坐标 —— 那会把角色瞬移进墙里）。
        "碰撞层" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("碰撞层", args, 2)?;
            let d = 字典(&args[0], "碰撞层", "瓦片图")?;
            let 项们 = match &args[1] {
                Val::List(l) => l.borrow().clone(),
                other => {
                    return Err(err(
                        "类型错误",
                        format!(
                            "「碰撞层」的「实心号们」要整数列表（比如 [0, 1, 2]），给的是{}",
                            crate::type_label(other)
                        ),
                    ))
                }
            };
            let mut 号们: Vec<i64> = Vec::new();
            for x in &项们 {
                let n = 整数(x, "碰撞层", "实心号")?;
                if !号们.contains(&n) {
                    号们.push(n);
                }
            }
            号们.sort_unstable();
            Ok(crate::dict_new(vec![
                (Val::Str("图".into()), Val::Dict(d.clone())),
                (
                    Val::Str("实心".into()),
                    crate::list_new(号们.into_iter().map(|n| Val::Int(n as i128)).collect()),
                ),
            ]))
        },
        "是实心" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("是实心", args, 3)?;
            let c = 字典(&args[0], "是实心", "碰撞层")?;
            let cc = c.borrow();
            let 图 = match 取(&cc, "图") {
                Some(Val::Dict(d)) => d,
                _ => {
                    return Err(err(
                        "类型错误",
                        "「是实心」的第一个参数要碰撞层（`瓦片.碰撞层` 造的）",
                    ))
                }
            };
            let 实心: Vec<i64> = match 取(&cc, "实心") {
                Some(Val::List(l)) => l
                    .borrow()
                    .iter()
                    .filter_map(|v| match v {
                        Val::Int(n) => Some(*n as i64),
                        _ => None,
                    })
                    .collect(),
                _ => Vec::new(),
            };
            drop(cc);
            let 列 = 整数(&args[1], "是实心", "列")?;
            let 行 = 整数(&args[2], "是实心", "行")?;
            let 号 = 格号(&图, 列, 行)?;
            Ok(Val::Bool(实心.contains(&号)))
        },
        "斜坡" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("斜坡", args, 4)?;
            let d = 字典(&args[0], "斜坡", "瓦片图")?;
            let 向 = as_text(&args[3]);
            if 向 != "左" && 向 != "右" {
                return Err(err(
                    "值错误",
                    format!("「斜坡」的方向只能是 左 / 右，给的是「{向}」"),
                ));
            }
            let 列 = 整数(&args[1], "斜坡", "列")?;
            let 行 = 整数(&args[2], "斜坡", "行")?;
            // 越界要报错（与 Python 同口径）—— 悄悄记在一个不存在的格子上，
            // 之后 `是斜坡` 永远问不到，那种「设了没效果」最难查
            let 行们0 = 图的格们(&d)?;
            let 列数0 = 行们0.iter().map(|r| r.len()).max().unwrap_or(0);
            let 越界 = 列 < 0
                || 行 < 0
                || 行 as usize >= 行们0.len()
                || 列 as usize >= 行们0.get(行 as usize).map(|r| r.len()).unwrap_or(0);
            if 越界 {
                return Err(err(
                    "值错误",
                    format!(
                        "「斜坡」的格子 ({列}, {行}) 不在地图里（地图是 {列数0} 列 × {} 行）",
                        行们0.len()
                    ),
                ));
            }
            let mut dd = d.borrow_mut();
            // 没有就建一个空字典
            if !matches!(取(&dd, "斜坡们"), Some(Val::Dict(_))) {
                设(&mut dd, "斜坡们", crate::dict_new(Vec::new()));
            }
            if let Some(Val::Dict(坡们)) = 取(&dd, "斜坡们") {
                crate::dict_set(
                    &mut 坡们.borrow_mut(),
                    Val::Str(format!("{列},{行}").into()),
                    Val::Str(向.into()),
                );
            }
            drop(dd);
            Ok(Val::Dict(d.clone()))
        },
        "是斜坡" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("是斜坡", args, 3)?;
            let d = 字典(&args[0], "是斜坡", "瓦片图")?;
            let dd = d.borrow();
            let 列 = 整数(&args[1], "是斜坡", "列")?;
            let 行 = 整数(&args[2], "是斜坡", "行")?;
            match 取(&dd, "斜坡们") {
                Some(Val::Dict(坡们)) => {
                    let k = Val::Str(format!("{列},{行}").into());
                    Ok(crate::dict_get(&坡们.borrow(), &k).unwrap_or(Val::None_))
                }
                _ => Ok(Val::None_),
            }
        },
        "取矩形内瓦片" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("取矩形内瓦片", args, 2)?;
            let d = 字典(&args[0], "取矩形内瓦片", "瓦片图")?;
            let dd = d.borrow();
            let 宽 = 数(&取(&dd, "瓦片宽").unwrap_or(Val::Int(1)), "取矩形内瓦片", "瓦片宽")?;
            let 高 = 数(&取(&dd, "瓦片高").unwrap_or(Val::Int(1)), "取矩形内瓦片", "瓦片高")?;
            let 行们 = 二维(&取(&dd, "行们").unwrap_or(Val::None_), "取矩形内瓦片", "行们")?;
            drop(dd);
            let 矩 = 字典(&args[1], "取矩形内瓦片", "矩形")?;
            let mr = 矩.borrow();
            let bx = 字典里的数(&mr, "x", "取矩形内瓦片")?;
            let by = 字典里的数(&mr, "y", "取矩形内瓦片")?;
            let bw = 字典里的数(&mr, "宽", "取矩形内瓦片")?;
            let bh = 字典里的数(&mr, "高", "取矩形内瓦片")?;
            drop(mr);
            if bw <= 0.0 || bh <= 0.0 {
                return Err(err("值错误", "「取矩形内瓦片」的矩形宽高要大于 0"));
            }
            let c0 = ((bx / 宽).floor() as i64).max(0);
            let c1 = ((bx + bw) / 宽).floor() as i64;
            let r0 = ((by / 高).floor() as i64).max(0);
            let r1 = ((by + bh) / 高).floor() as i64;
            let mut 出: Vec<Val> = Vec::new();
            for r in r0..=r1 {
                let 行 = match 行们.get(r as usize) {
                    Some(x) => x,
                    None => break,
                };
                for c in c0..=c1 {
                    match 行.get(c as usize) {
                        Some(号) => 出.push(crate::dict_new(vec![
                            (Val::Str("列".into()), Val::Int(c as i128)),
                            (Val::Str("行".into()), Val::Int(r as i128)),
                            (Val::Str("号".into()), Val::Int(*号 as i128)),
                        ])),
                        None => break,
                    }
                }
            }
            Ok(crate::list_new(出))
        },
        "最近可走格" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("最近可走格", args, 4)?;
            let d = 字典(&args[0], "最近可走格", "瓦片图")?;
            let c = 字典(&args[1], "最近可走格", "碰撞层")?;
            let 宽 = 数(&取(&d.borrow(), "瓦片宽").unwrap_or(Val::Int(1)), "最近可走格", "瓦片宽")?;
            let 高 = 数(&取(&d.borrow(), "瓦片高").unwrap_or(Val::Int(1)), "最近可走格", "瓦片高")?;
            let 行们 = 二维(
                &取(&d.borrow(), "行们").unwrap_or(Val::None_),
                "最近可走格",
                "行们",
            )?;
            let 实心: Vec<i64> = match 取(&c.borrow(), "实心") {
                Some(Val::List(l)) => l
                    .borrow()
                    .iter()
                    .filter_map(|v| match v {
                        Val::Int(n) => Some(*n as i64),
                        _ => None,
                    })
                    .collect(),
                _ => Vec::new(),
            };
            let x = 数(&args[2], "最近可走格", "x")?;
            let y = 数(&args[3], "最近可走格", "y")?;
            let 起列 = (x / 宽).floor() as i64;
            let 起行 = (y / 高).floor() as i64;
            for 圈 in 0..=16i64 {
                // 行优先遍历这一圈 —— 与 Python 同序，结果才可复现
                for dr in -圈..=圈 {
                    for dc in -圈..=圈 {
                        if dr.abs().max(dc.abs()) != 圈 {
                            continue;
                        }
                        let r = 起行 + dr;
                        let cc = 起列 + dc;
                        if r < 0 || cc < 0 {
                            continue;
                        }
                        let 号 = match 行们.get(r as usize).and_then(|行| 行.get(cc as usize)) {
                            Some(v) => *v,
                            None => continue,
                        };
                        if 实心.contains(&号) {
                            continue;
                        }
                        return Ok(crate::dict_new(vec![
                            (Val::Str("列".into()), Val::Int(cc as i128)),
                            (Val::Str("行".into()), Val::Int(r as i128)),
                            (Val::Str("x".into()), Val::Float((cc as f64 + 0.5) * 宽)),
                            (Val::Str("y".into()), Val::Float((r as f64 + 0.5) * 高)),
                        ]));
                    }
                }
            }
            Ok(Val::None_)
        },
    }
}

// ---------------------------------------------------------------------------
// 地图（2）
// ---------------------------------------------------------------------------

/// 我们自己的地图格式（见 `docs/地图格式.md`）：
///
/// ```json
/// {"瓦片宽": 32, "瓦片高": 32, "图集": "图集.png",
///  "层们": [{"名": "地面", "行们": [[0, 1], [-1, 2]]}]}
/// ```
pub(crate) fn map_module() -> HashMap<String, Val> {
    table! { "地图";
        // 读(路径 或 字典)
        "读" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("读", args, 1)?;
            let 原 = match &args[0] {
                // 传字典：直接用（**wasm 里只能这样** —— 那边没有文件系统）
                Val::Dict(_) => args[0].clone(),
                Val::Str(p) => {
                    let 文 = std::fs::read_to_string(p.as_ref()).map_err(|e| {
                        err_with_hint(
                            "文件错误",
                            format!("读不了地图「{p}」：{e}"),
                            "路径相对于**当前工作目录**；页面里没有文件系统，请直接传字典".into(),
                        )
                    })?;
                    let j = crate::json::parse(&文)
                        .map_err(|e| err("值错误", format!("地图「{p}」不是合法 JSON：{e}")))?;
                    super::browser::值_of(&j)
                }
                other => {
                    return Err(err(
                        "类型错误",
                        format!("「地图.读」要路径或字典，给的是{}", crate::type_label(other)),
                    ))
                }
            };
            规整地图(&原)
        },
        // 层(地图, 名) → 层字典；没有这层给 `空`
        "层" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("层", args, 2)?;
            let d = 字典(&args[0], "层", "地图")?;
            let 名 = 文本(&args[1], "层", "层名")?;
            let dd = d.borrow();
            if let Some(Val::List(层们)) = 取(&dd, "层们") {
                for l in 层们.borrow().iter() {
                    if let Val::Dict(ld) = l {
                        if let Some(Val::Str(n)) = 取(&ld.borrow(), "名") {
                            if n.as_ref() == 名 {
                                return Ok(l.clone());
                            }
                        }
                    }
                }
            }
            Ok(Val::None_)
        },

        // ---- M197：Tiled TMX 互通 ------------------------------------------
        //
        // 复用 M194 的 `crate::xml`（按本地名认标签，正好对付 Tiled 的命名空间）。
        // 两处换算写死：`gid 0 → -1`（Tiled 用 0 表示空格，我们 0 是合法瓦片）、
        // `gid n → n - firstgid`（Tiled 的 gid 从 firstgid 起）。
        "从Tiled" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("从Tiled", args, 1)?;
            let p = 文本(&args[0], "从Tiled", "路径")?;
            let 文 = std::fs::read_to_string(&p).map_err(|e| {
                if e.kind() == std::io::ErrorKind::NotFound {
                    err("文件错误", format!("找不到文件「{p}」"))
                } else {
                    err("文件错误", format!("读不了「{p}」：{e}"))
                }
            })?;
            let 根 = crate::xml::解析(&文);
            if 根.名 != "map" {
                return Err(err(
                    "值错误",
                    format!(
                        "「{p}」的根标签是 <{}>，不是 <map> —— 这大概不是 Tiled 地图",
                        根.名
                    ),
                ));
            }
            let 宽: i64 = 根.取属性("width").parse().unwrap_or(0);
            let 高: i64 = 根.取属性("height").parse().unwrap_or(0);
            let 瓦宽: f64 = 根.取属性("tilewidth").parse().unwrap_or(0.0);
            let 瓦高: f64 = 根.取属性("tileheight").parse().unwrap_or(0.0);
            if 宽 <= 0 || 高 <= 0 {
                return Err(err(
                    "值错误",
                    format!("「{p}」的 map 缺 width / height（现在是 {宽} × {高}）"),
                ));
            }
            if 瓦宽 <= 0.0 || 瓦高 <= 0.0 {
                return Err(err(
                    "值错误",
                    format!("「{p}」的 map 缺 tilewidth / tileheight"),
                ));
            }
            // 图集：第一个 tileset 的 <image source>
            let mut 图集 = Val::None_;
            let mut 首gid: i64 = 1;
            for c in &根.子们 {
                if c.名 != "tileset" {
                    continue;
                }
                首gid = c.取属性("firstgid").parse().unwrap_or(1);
                for k in &c.子们 {
                    if k.名 == "image" {
                        图集 = Val::Str(k.取属性("source").into());
                    }
                }
                break;
            }
            let mut 层们: Vec<Val> = Vec::new();
            for c in &根.子们 {
                if c.名 != "layer" {
                    continue;
                }
                let 名 = c.取属性("name").to_string();
                let 数据 = c.子们.iter().find(|k| k.名 == "data");
                let 数据 = match 数据 {
                    Some(x) => x,
                    None => {
                        let 空行: Vec<Val> = (0..高)
                            .map(|_| crate::list_new(vec![Val::Int(-1); 宽 as usize]))
                            .collect();
                        层们.push(dict_new(vec![
                            (Val::Str("名".into()), Val::Str(名.as_str().into())),
                            (Val::Str("行们".into()), crate::list_new(空行)),
                        ]));
                        continue;
                    }
                };
                let 编 = 数据.取属性("encoding").to_lowercase();
                if 编 != "csv" {
                    return Err(err(
                        "值错误",
                        format!(
                            "「{p}」的层「{名}」用的是 {} 编码，这一版只认 csv\
                             （Tiled 里「地图 → 地图属性 → 图块层格式」改成 CSV 再存）",
                            if 编.is_empty() { "未标注" } else { 编.as_str() }
                        ),
                    ));
                }
                let mut gid们: Vec<i64> = Vec::new();
                for 片 in 数据.文本.replace('\n', ",").split(',') {
                    let 片 = 片.trim();
                    if 片.is_empty() {
                        continue;
                    }
                    match 片.parse::<i64>() {
                        Ok(n) => gid们.push(n),
                        Err(_) => {
                            return Err(err(
                                "值错误",
                                format!("「{p}」的层「{名}」里有个格子不是整数：「{片}」"),
                            ))
                        }
                    }
                }
                if gid们.len() != (宽 * 高) as usize {
                    return Err(err(
                        "值错误",
                        format!(
                            "「{p}」的层「{名}」有 {} 个格子，应当是 {宽} × {高} = {}",
                            gid们.len(),
                            宽 * 高
                        ),
                    ));
                }
                let mut 行们: Vec<Val> = Vec::with_capacity(高 as usize);
                for r in 0..高 as usize {
                    let mut 行: Vec<Val> = Vec::with_capacity(宽 as usize);
                    for c0 in 0..宽 as usize {
                        let g = gid们[r * 宽 as usize + c0];
                        行.push(Val::Int(if g == 0 { -1 } else { g - 首gid } as i128));
                    }
                    行们.push(crate::list_new(行));
                }
                层们.push(dict_new(vec![
                    (Val::Str("名".into()), Val::Str(名.as_str().into())),
                    (Val::Str("行们".into()), crate::list_new(行们)),
                ]));
            }
            Ok(dict_new(vec![
                (Val::Str("瓦片宽".into()), Val::Float(瓦宽)),
                (Val::Str("瓦片高".into()), Val::Float(瓦高)),
                (Val::Str("图集".into()), 图集),
                (Val::Str("层们".into()), crate::list_new(层们)),
            ]))
        },
        "存Tiled" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("存Tiled", args, 2)?;
            let 规整 = 规整地图(&args[0])?;
            let p = 文本(&args[1], "存Tiled", "路径")?;
            let 地 = 字典(&规整, "存Tiled", "地图")?;
            let (瓦宽, 瓦高, 图集, 层们) = {
                let dd = 地.borrow();
                let w = 数(&取必(&dd, "瓦片宽", "地图")?, "存Tiled", "瓦片宽")?;
                let h = 数(&取必(&dd, "瓦片高", "地图")?, "存Tiled", "瓦片高")?;
                let t = match 取(&dd, "图集") {
                    Some(Val::Str(s)) if !s.is_empty() => s.to_string(),
                    _ => "tiles.png".to_string(),
                };
                let ls = match 取(&dd, "层们") {
                    Some(Val::List(l)) => l.borrow().clone(),
                    _ => Vec::new(),
                };
                (w, h, t, ls)
            };
            if 层们.is_empty() {
                return Err(err("值错误", "「存Tiled」的地图里一层都没有"));
            }
            // 先量出整张地图有多大（层之间可能长短不一，取最大）
            let mut 宽: usize = 0;
            let mut 高: usize = 0;
            for l in &层们 {
                if let Val::Dict(ld) = l {
                    if let Some(Val::List(行们)) = 取(&ld.borrow(), "行们") {
                        let 行们 = 行们.borrow().clone();
                        高 = 高.max(行们.len());
                        for r in &行们 {
                            if let Val::List(cs) = r {
                                宽 = 宽.max(cs.borrow().len());
                            }
                        }
                    }
                }
            }
            if 宽 == 0 || 高 == 0 {
                return Err(err("值错误", "「存Tiled」的地图是空的（没有格子）"));
            }
            let 瓦宽 = 瓦宽.round() as i64;
            let 瓦高 = 瓦高.round() as i64;
            let mut 出 = String::new();
            出.push_str("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n");
            出.push_str(&format!(
                "<map version=\"1.10\" tiledversion=\"1.10.2\" orientation=\"orthogonal\"\
                 renderorder=\"right-down\" width=\"{宽}\" height=\"{高}\"\
                 tilewidth=\"{瓦宽}\" tileheight=\"{瓦高}\" infinite=\"0\"\
                 nextlayerid=\"{}\" nextobjectid=\"1\">\n",
                层们.len() + 1
            ));
            出.push_str(&format!(
                " <tileset firstgid=\"1\" name=\"tiles\" tilewidth=\"{瓦宽}\" tileheight=\"{瓦高}\">\n"
            ));
            出.push_str(&format!("  <image source=\"{}\"/>\n", xml转义(&图集)));
            出.push_str(" </tileset>\n");
            for (i, l) in 层们.iter().enumerate() {
                let (名, 行们) = match l {
                    Val::Dict(ld) => {
                        let dd = ld.borrow();
                        let n = match 取(&dd, "名") {
                            Some(v) => as_text(&v),
                            None => format!("层{}", i + 1),
                        };
                        let rs = match 取(&dd, "行们") {
                            Some(Val::List(x)) => x.borrow().clone(),
                            _ => Vec::new(),
                        };
                        (n, rs)
                    }
                    _ => (format!("层{}", i + 1), Vec::new()),
                };
                let 名 = xml转义(&名);
                出.push_str(&format!(
                    " <layer id=\"{}\" name=\"{名}\" width=\"{宽}\" height=\"{高}\">\n",
                    i + 1
                ));
                出.push_str("  <data encoding=\"csv\">\n");
                for r in 0..高 {
                    let mut 片: Vec<String> = Vec::with_capacity(宽);
                    let 本行 = 行们.get(r).and_then(|x| match x {
                        Val::List(cs) => Some(cs.borrow().clone()),
                        _ => None,
                    });
                    for c in 0..宽 {
                        let 号 = 本行
                            .as_ref()
                            .and_then(|cs| cs.get(c))
                            .and_then(|v| match v {
                                Val::Int(n) => Some(*n as i64),
                                _ => None,
                            })
                            .unwrap_or(-1);
                        片.push(if 号 < 0 {
                            "0".to_string()
                        } else {
                            (号 + 1).to_string()
                        });
                    }
                    出.push_str(&片.join(","));
                    if r + 1 < 高 {
                        出.push(',');
                    }
                    出.push('\n');
                }
                出.push_str("</data>\n </layer>\n");
            }
            出.push_str("</map>\n");
            std::fs::write(&p, 出)
                .map_err(|e| err("文件错误", format!("写不了「{p}」：{e}")))?;
            Ok(Val::None_)
        },
    }
}

/// XML 文本转义（只转这五个 —— 多转会把合法内容弄坏）。
fn xml转义(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

/// 校验 / 补齐地图字典（缺字段就说清楚缺哪个，别让它在 `瓦片.载入` 里才炸）。
fn 规整地图(v: &Val) -> Result<Val, JishiError> {
    let d = 字典(v, "地图", "内容")?;
    let (宽原, 高原, 图集, 层们) = {
        let dd = d.borrow();
        let 宽v = 取必(&dd, "瓦片宽", "地图")?;
        let 高v = 取必(&dd, "瓦片高", "地图")?;
        // 先过一遍「是不是数字」，再**原样**带回去
        let _ = 数(&宽v, "地图", "瓦片宽")?;
        let _ = 数(&高v, "地图", "瓦片高")?;
        (
            宽v,
            高v,
            取(&dd, "图集"),
            取(&dd, "层们"),
        )
    };
    let 层们 = match 层们 {
        Some(Val::List(l)) => l,
        _ => return Err(err_with_hint(
            "值错误",
            "地图里缺「层们」（应当是列表）",
            "一层长这样：{\"名\": \"地面\", \"行们\": [[0, -1], [1, 2]]}".into(),
        )),
    };
    Ok(dict_new(vec![
        // ⚠️ **保留原来的数值类型**（`16` 就是整数，别转成 `16.0`）——
        // Python 侧也是原样带过去，三个宿主打印出来才会一致。
        (Val::Str("瓦片宽".into()), 宽原),
        (Val::Str("瓦片高".into()), 高原),
        (Val::Str("图集".into()), 图集.unwrap_or(Val::None_)),
        (Val::Str("层们".into()), Val::List(层们)),
    ]))
}

// ---------------------------------------------------------------------------
// 存档（4）
// ---------------------------------------------------------------------------

/// 存档的键前缀 —— 与用户自己的 `浏览器.存本地` 分开，免得撞名。
const 前缀: &str = "基石存档/";
/// 索引键（`清()` 要知道有哪些键；`localStorage` 没有「列出所有键」的 JS 接口）。
const 索引键: &str = "基石存档/索引";
/// 格式版本。**改它 = 老存档读不出来**（这是有意的：宁可当没存过，也别读错数据）。
const 格式版本: i64 = 1;
/// M197：「自动存」写进这个槽（固定名）。用户读它就跟读别的槽一样。
const 自动槽: &str = "自动";

fn 页面检查() -> Result<(), JishiError> {
    #[cfg(target_arch = "wasm32")]
    {
        Ok(())
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        Err(err_with_hint(
            "运行期错误",
            "这个平台没有存档：「存档」要页面的 localStorage（命令行和 Node 上没有）。",
            "在页面 / 窗口里跑，或者自己用「文件」模块存成 JSON".into(),
        ))
    }
}

/// 调 `浏览器` 的那三个存储操作（走同一条通道）。
fn 存本地(键: &str, 值: &str) -> Result<Val, JishiError> {
    super::browser::host_call("存本地", &[Val::Str(键.into()), Val::Str(值.into())])
}

fn 读本地(键: &str) -> Result<Option<String>, JishiError> {
    match super::browser::host_call("读本地", &[Val::Str(键.into())])? {
        Val::Str(s) => Ok(Some(s.to_string())),
        // 「没存过」= `空`（与 浏览器.读本地 同口径）
        _ => Ok(None),
    }
}

fn 索引() -> Result<Vec<String>, JishiError> {
    match 读本地(索引键)? {
        None => Ok(Vec::new()),
        Some(t) => match crate::json::parse(&t) {
            Ok(crate::json::Json::Arr(a)) => Ok(a
                .iter()
                .filter_map(|x| match x {
                    crate::json::Json::Str(s) => Some(s.clone()),
                    _ => None,
                })
                .collect()),
            // 索引坏了就当空的（不影响「存/读」本身）
            _ => Ok(Vec::new()),
        },
    }
}

fn 写索引(名们: &[String]) -> Result<(), JishiError> {
    let 文 = format!(
        "[{}]",
        名们
            .iter()
            .map(|n| crate::jsonw::esc(n))
            .collect::<Vec<_>>()
            .join(",")
    );
    存本地(索引键, &文)?;
    Ok(())
}

pub(crate) fn save_module() -> HashMap<String, Val> {
    table! { "存档";
        // 存(名, 值)
        "存" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("存", args, 2)?;
            页面检查()?;
            let 名 = 文本(&args[0], "存", "名")?;
            let jv = crate::sandbox::val_to_jv(&args[1]).map_err(|t| {
                err_with_hint(
                    "类型错误",
                    format!("存不了：值里有「{t}」这类东西，转不成 JSON"),
                    "存档只收 数字 / 文本 / 布尔 / 空 / 列表 / 字典".into(),
                )
            })?;
            let 文 = crate::jsonw::dump_compact(&jv);
            let 包 = format!("{{\"版本\":{},\"值\":{}}}", 格式版本, 文);
            存本地(&format!("{前缀}{名}"), &包)?;
            // 维护索引（同一个名字只记一次）
            let mut 名们 = 索引()?;
            if !名们.iter().any(|n| *n == 名) {
                名们.push(名.clone());
                写索引(&名们)?;
            }
            Ok(Val::Bool(true))
        },
        // 读(名, 默认?)
        "读" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("读", args, 1, 2)?;
            页面检查()?;
            let 名 = 文本(&args[0], "读", "名")?;
            let 默认 = args.get(1).cloned().unwrap_or(Val::None_);
            let Some(文) = 读本地(&format!("{前缀}{名}"))? else {
                return Ok(默认);
            };
            // 🔴 **存档坏了不该让游戏起不来**：解析不出 / 版本不对 ⇒ 返回默认，**不抛错**。
            let Ok(crate::json::Json::Obj(o)) = crate::json::parse(&文) else {
                return Ok(默认);
            };
            match o.get("版本") {
                Some(crate::json::Json::Int(v)) if *v == 格式版本 as i128 => {}
                _ => return Ok(默认),
            }
            match o.get("值") {
                Some(j) => Ok(super::browser::值_of(j)),
                None => Ok(默认),
            }
        },
        // 删(名)
        "删" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("删", args, 1)?;
            页面检查()?;
            let 名 = 文本(&args[0], "删", "名")?;
            super::browser::host_call("删本地", &[Val::Str(format!("{前缀}{名}").into())])?;
            let 名们: Vec<String> = 索引()?.into_iter().filter(|n| *n != 名).collect();
            写索引(&名们)?;
            Ok(Val::Bool(true))
        },
        // 清()
        "清" => |_args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            页面检查()?;
            let 名们 = 索引()?;
            for 名 in &名们 {
                super::browser::host_call(
                    "删本地",
                    &[Val::Str(format!("{前缀}{名}").into())],
                )?;
            }
            super::browser::host_call("删本地", &[Val::Str(索引键.into())])?;
            Ok(Val::Int(名们.len() as i128))
        },

        // ---- M197：槽们 / 自动存 -------------------------------------------
        "槽们" => |_args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            页面检查()?;
            let mut 名们 = 索引()?;
            名们.sort();
            Ok(crate::list_new(
                名们.into_iter().map(|n| Val::Str(n.as_str().into())).collect(),
            ))
        },
        // 自动存(值, 间隔毫秒?) —— 只把「要存什么」交给宿主，落盘由宿主的定时器做
        // （每帧写 localStorage 是同步 IO，会把主线程卡住）
        "自动存" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("自动存", args, 1, 2)?;
            页面检查()?;
            let 毫 = match args.get(1) {
                None | Some(Val::None_) => 30000.0,
                Some(v) => 数(v, "自动存", "间隔毫秒")?,
            };
            if 毫 < 16.0 {
                return Err(err(
                    "值错误",
                    format!("「自动存」的间隔至少 16 毫秒（一帧），现在是 {毫}"),
                ));
            }
            // 与 `存` 用同一套序列化 + 同一个包格式（版本 + 值）——
            // 这样 `读("自动")` 读得出来
            let jv = crate::sandbox::val_to_jv(&args[0]).map_err(|t| {
                err_with_hint(
                    "类型错误",
                    format!("「自动存」存不了：值里有「{t}」这类东西，转不成 JSON"),
                    "存档只收 数字 / 文本 / 布尔 / 空 / 列表 / 字典".into(),
                )
            })?;
            let 文 = crate::jsonw::dump_compact(&jv);
            let 包 = format!("{{\"版本\":{},\"值\":{}}}", 格式版本, 文);
            super::browser::host_call(
                "自动存",
                &[
                    Val::Str(包.into()),
                    Val::Float(毫),
                    Val::Str(format!("{前缀}{}", 自动槽).into()),
                    Val::Str(索引键.into()),
                ],
            )
        },
    }
}

// ---------------------------------------------------------------------------
// 场景（3）
// ---------------------------------------------------------------------------

thread_local! {
    /// 场景表：名字 → (初始化, 每帧)。两个都是可选的函数值。
    ///
    /// ⚠️ 与 `frame` 的登记表一样，里面握着 `Rc`（函数值）⇒ 只在本线程用。
    static 场景表: RefCell<HashMap<String, (Option<Val>, Option<Val>)>> =
        RefCell::new(HashMap::new());
    /// 当前场景名（空 = 还没切过）。
    static 现在: RefCell<String> = const { RefCell::new(String::new()) };
    /// M197 转场：`(效果, 总帧数, 已走帧数)`。空效果 = 没在转场。
    ///
    /// 📌 **按帧数不按毫秒**：毫秒要靠时钟，而 wasm 没有系统时钟（得由页面每帧喂），
    /// 三宿主的时钟不一致就是「同一段代码快慢不同」的最难查的坑。帧数是确定的。
    static 转场态: RefCell<(String, i64, i64)> =
        const { RefCell::new((String::new(), 0, 0)) };
}

pub(crate) fn scene_module() -> HashMap<String, Val> {
    table! { "场景";
        // 定(名, {"初始化"?,"每帧"?})
        "定" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("定", args, 2)?;
            let 名 = 文本(&args[0], "定", "名")?;
            let d = 字典(&args[1], "定", "选项")?;
            let (初, 帧) = {
                let dd = d.borrow();
                let 取函数 = |键: &str| -> Result<Option<Val>, JishiError> {
                    match 取(&dd, 键) {
                        None | Some(Val::None_) => Ok(None),
                        Some(v @ Val::Func(_)) => Ok(Some(v)),
                        Some(other) => Err(err(
                            "类型错误",
                            format!("「定」的「{键}」要函数，给的是{}", crate::type_label(&other)),
                        )),
                    }
                };
                (取函数("初始化")?, 取函数("每帧")?)
            };
            场景表.with(|t| t.borrow_mut().insert(名, (初, 帧)));
            Ok(Val::Bool(true))
        },
        // 切(名)
        "切" => |args: &[Val], vm: &mut dyn BuiltinHost| -> R {
            need("切", args, 1)?;
            let 名 = 文本(&args[0], "切", "名")?;
            let 有 = 场景表.with(|t| t.borrow().contains_key(&名));
            if !有 {
                return Err(err_with_hint(
                    "值错误",
                    format!("没有叫「{名}」的场景"),
                    "先 `场景.定(\"名字\", {\"初始化\": 某函数})` 再切".into(),
                ));
            }
            // 已经在这个场景：**什么都不做**（否则每帧切一次会反复跑初始化）
            if 现在.with(|n| n.borrow().as_str() == 名) {
                return Ok(Val::Bool(false));
            }
            现在.with(|n| *n.borrow_mut() = 名.clone());
            let 初 = 场景表.with(|t| {
                t.borrow()
                    .get(&名)
                    .and_then(|(c, _)| c.clone())
            });
            if let Some(f) = 初 {
                // 初始化跑**一次**（每次切进来一次）；它报错就照常抛出去
                vm.call_value(f, Vec::new())?;
            }
            Ok(Val::Bool(true))
        },
        // 现在()
        "现在" => |_args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            Ok(Val::Str(现在.with(|n| n.borrow().clone()).as_str().into()))
        },
        // ---- M197：转场（按帧数，见 `转场态` 的注释）----
        "转场" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("转场", args, 2)?;
            let 名 = 文本(&args[0], "转场", "效果")?;
            if 名 != "淡出" && 名 != "淡入" && 名 != "擦除" {
                return Err(err(
                    "值错误",
                    format!("「场景.转场」不认识的效果「{名}」—— 只能是：淡出 / 淡入 / 擦除"),
                ));
            }
            let 总 = 整数(&args[1], "转场", "帧数")?;
            if 总 <= 0 {
                return Err(err(
                    "值错误",
                    format!("「场景.转场」的帧数要大于 0，现在是 {总}"),
                ));
            }
            转场态.with(|t| *t.borrow_mut() = (名, 总, 0));
            Ok(Val::Bool(true))
        },
        "转场中" => |_args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            let (在, 已, 总) = 转场态.with(|t| {
                let mut b = t.borrow_mut();
                if b.2 >= b.1 {
                    return (false, b.2, b.1);
                }
                b.2 += 1;
                (true, b.2, b.1)
            });
            if 在 {
                // 页面里顺手画一层遮罩（走 `图形` 的通道）。命令行上没画布 ⇒ 静默跳过 ——
                // 它是**表现**，不该让「转场中」在命令行上报错。
                #[cfg(target_arch = "wasm32")]
                {
                    let 进度 = if 总 > 0 { 已 as f64 / 总 as f64 } else { 1.0 };
                    let 效 = 转场态.with(|t| t.borrow().0.clone());
                    let _ = 调(
                        "转场帧",
                        &[Val::Str(效.into()), Val::Float(进度)],
                    );
                }
                #[cfg(not(target_arch = "wasm32"))]
                let _ = (已, 总);
            }
            Ok(Val::Bool(在))
        },
    }
}

// ---------------------------------------------------------------------------
// 注册（`mod.rs` 从这里取五个）
// ---------------------------------------------------------------------------

pub(crate) use sprite_module as 精灵;
pub(crate) use tile_module as 瓦片;
pub(crate) use map_module as 地图;
pub(crate) use save_module as 存档;
pub(crate) use scene_module as 场景;
