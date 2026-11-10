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
                    累计 = 0.0;
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
    }
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
