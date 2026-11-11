//! 基石标准库：图形（M186）—— 2D 画布、输入、帧循环。
//!
//! ## 边界：**只在页面 / 窗口里有**
//!
//! | 宿主 | 状态 |
//! |---|---|
//! | 网页（wasm）· 原生窗口 · 应用模式窗口 | ✅ 真画（`<canvas>` + 2D 上下文） |
//! | 命令行 · Node | ❌ 报**同一句**中文平台错 |
//!
//! 命令行上要出图请用 `表格` / `站点`，或者自己拼 SVG —— 这里不给「假装能画」的
//! 降级实现（那会让同一段游戏代码在两个宿主上表现不同，最难查）。
//!
//! ## 通道：不加新协议，只有几个新 `op`
//!
//! 与 `浏览器` 走**同一条** `jishi_host_call(请求JSON)`，所有绘制都是
//! `{"op":"矩形","args":[x,y,宽,高,"{...样式...}"]}` 这种形状。
//! ⇒ 通道 ABI 没变；加新图元只是 shim 里多一个 `case`。
//!
//! ⚠️ **样式 / 点列 / 选项都序列化成 JSON 文本再传**：通道的 `args` 只认
//! 「文本 / 数字 / 布尔 / 空」四种标量（`browser::build_req` 定的）。要么改成
//! 递归序列化（动通道），要么在这里先拼好 —— 选后者，通道不动。
//!
//! ## 坐标系：**一律逻辑像素**
//!
//! `准备({宽:800, 高:600})` 说的是「看起来 800×600」。Retina / 125% 缩放下
//! shim 会按 `devicePixelRatio` 把**位图**放大、CSS 尺寸不变、再 `setTransform`
//! 一次性换算 ⇒ **游戏代码永远不用乘 DPR**（否则每个示例都要自己乘一遍，
//! 而且漏了就是「糊」）。
//!
//! ## 帧循环
//!
//! `运行(初始化, 每帧)` 进**常驻 VM**，由 shim 的 `requestAnimationFrame` 驱动
//! （设计见 `docs/设计决策.md` **D110**）。三条必须记住的：
//!
//! 1. `运行` **之后的代码只在启动时跑一次**，不会被每帧重跑；
//! 2. 常驻模式下**刷新页面状态不回来**（VM 随页面重建）⇒ 要存就 `浏览器.存本地`；
//! 3. 常驻期间**不能再开第二个窗口**（一个 wasm 实例一个 VM 一个循环）。

use std::collections::HashMap;

use crate::{err, err_with_hint, frame, BuiltinHost, JishiError, Val};

use super::browser::{esc, host_call, 通知宿主};
use super::{need, need_range, table, R};

/// 平台错。**三个宿主各写一份，必须逐字相同**（判据盯着）。
pub(crate) const 无画布: &str = "这个平台没有画布：「图形」模块只在网页 / 窗口里有\
（命令行和 Node 上没有画布）。要做图表请用「表格」，要出网页请用「站点」。";

/// 给同 crate 的其它模块（`精灵` / `瓦片`）用：先过平台检查，再走同一条通道。
///
/// 📌 为什么要过 `要画布()`：那两个模块画的图元就是这个模块的图元 ——
/// 命令行上必须报**同一句**平台错（不能报一句 `浏览器` 的）。
pub(crate) fn 调(op: &str, args: &[Val]) -> Result<Val, JishiError> {
    要画布()?;
    host_call(op, args)
}

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------

/// 平台检查：非 wasm 一律报同一句话。
pub(crate) fn 要画布() -> Result<(), JishiError> {
    #[cfg(target_arch = "wasm32")]
    {
        Ok(())
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        Err(err("运行期错误", 无画布))
    }
}

/// 收一个数（`整数` 或 `浮点` 都收）。
fn 数(v: &Val, 名: &str, 参: &str) -> Result<f64, JishiError> {
    match v {
        Val::Int(i) => Ok(*i as f64),
        Val::Float(f) => Ok(*f),
        Val::Bool(b) => Ok(if *b { 1.0 } else { 0.0 }),
        other => Err(err(
            "类型错误",
            format!(
                "「{名}」的「{参}」要数字，给的是{}",
                crate::type_label(other)
            ),
        )),
    }
}

/// 收一个文本。
fn 文本(v: &Val, 名: &str, 参: &str) -> Result<String, JishiError> {
    match v {
        Val::Str(s) => Ok(s.to_string()),
        other => Err(err(
            "类型错误",
            format!(
                "「{名}」的「{参}」要文本，给的是{}",
                crate::type_label(other)
            ),
        )),
    }
}

/// 收一个可选文本：`空` ⇒ `None`。
fn 可选文本(args: &[Val], i: usize, 名: &str, 参: &str) -> Result<Option<String>, JishiError> {
    match args.get(i) {
        None | Some(Val::None_) => Ok(None),
        Some(v) => Ok(Some(文本(v, 名, 参)?)),
    }
}

/// 字典 → `(键, 值)` 列表。不是字典就报清楚。
fn 字典<'a>(v: &'a Val, 名: &str, 参: &str) -> Result<Vec<(String, Val)>, JishiError> {
    match v {
        Val::Dict(d) => Ok(d
            .borrow()
            .to_items()
            .into_iter()
            .map(|(k, v)| (crate::display(&k), v))
            .collect()),
        other => Err(err(
            "类型错误",
            format!(
                "「{名}」的「{参}」要字典，给的是{}",
                crate::type_label(other)
            ),
        )),
    }
}

/// 把一个「标量」写成 JSON（样式里的值只能是这几种）。
fn 标量json(v: &Val) -> Result<String, JishiError> {
    Ok(match v {
        Val::Str(s) => format!("\"{}\"", esc(s)),
        Val::Int(i) => i.to_string(),
        Val::Float(f) => {
            if f.is_finite() {
                format!("{f}")
            } else {
                return Err(err("值错误", "样式里的数字不能是 NaN / 无穷"));
            }
        }
        Val::Bool(b) => (if *b { "true" } else { "false" }).to_string(),
        Val::None_ => "null".to_string(),
        other => {
            return Err(err(
                "类型错误",
                format!(
                    "样式里的值只能是文本 / 数字 / 布尔 / 空，给的是{}",
                    crate::type_label(other)
                ),
            ))
        }
    })
}

/// 样式字典 → JSON 文本。`空` ⇒ `"{}"`。
///
/// 认这几个键（认不出的**报错**，不静默忽略 —— 打错一个键名却「看起来没效果」
/// 是最难查的一类）：
///
/// `填充` `描边` `线宽` `字号` `对齐` `透明度` `圆角`
fn 样式json(v: Option<&Val>) -> Result<String, JishiError> {
    let Some(v) = v else { return Ok("{}".to_string()) };
    if matches!(v, Val::None_) {
        return Ok("{}".to_string());
    }
    let mut 段: Vec<String> = Vec::new();
    for (k, val) in 字典(v, "图形", "样式")? {
        match k.as_str() {
            "填充" | "描边" | "对齐" | "字号" | "线宽" | "透明度" | "圆角" => {
                段.push(format!("\"{}\":{}", esc(&k), 标量json(&val)?));
            }
            other => {
                return Err(err_with_hint(
                    "值错误",
                    format!("样式里没有「{other}」这一项"),
                    "能用的是：填充 / 描边 / 线宽 / 字号 / 对齐 / 透明度 / 圆角".into(),
                ))
            }
        }
    }
    Ok(format!("{{{}}}", 段.join(",")))
}

/// 图像选项 → JSON（`画图` / `画子图` 用）。
///
/// 与 [`样式json`] **分开**：这边认 `旋转 / 缩放 / 翻转 / 透明度`，那边认
/// `填充 / 描边 / …`。共用一张表的话，`图形.矩形(…, {"旋转": 30})` 会「静默无效」
/// —— 而这里的策略是**打错键就报错**。
fn 选项json(v: Option<&Val>) -> Result<String, JishiError> {
    let Some(v) = v else { return Ok("{}".to_string()) };
    if matches!(v, Val::None_) {
        return Ok("{}".to_string());
    }
    let mut 段: Vec<String> = Vec::new();
    for (k, val) in 字典(v, "图形", "选项")? {
        match k.as_str() {
            "旋转" | "缩放" | "翻转" | "透明度" => {
                段.push(format!("\"{}\":{}", esc(&k), 标量json(&val)?));
            }
            other => {
                return Err(err_with_hint(
                    "值错误",
                    format!("图像选项里没有「{other}」这一项"),
                    "能用的是：旋转（度）/ 缩放 / 翻转（「横」或「纵」）/ 透明度".into(),
                ))
            }
        }
    }
    Ok(format!("{{{}}}", 段.join(",")))
}

/// 点列表 → JSON：`[[x,y], …]`（`多边形` 用）。
fn 点列json(v: &Val) -> Result<String, JishiError> {
    let Val::List(items) = v else {
        return Err(err(
            "类型错误",
            format!(
                "「多边形」的点要列表（每项 `{{x, y}}` 或 `[x, y]`），给的是{}",
                crate::type_label(v)
            ),
        ));
    };
    let 表 = items.borrow();
    let mut 段: Vec<String> = Vec::new();
    for (i, it) in 表.iter().enumerate() {
        段.push(match it {
            Val::List(p) => {
                let p = p.borrow();
                if p.len() != 2 {
                    return Err(err(
                        "值错误",
                        format!("「多边形」第 {} 个点应当是 `[x, y]`（两个数），实际 {} 个", i + 1, p.len()),
                    ));
                }
                format!(
                    "[{},{}]",
                    数(&p[0], "多边形", "x")?,
                    数(&p[1], "多边形", "y")?
                )
            }
            Val::Dict(_) => {
                let d = 字典(it, "多边形", "点")?;
                let 取 = |名: &str| -> Result<f64, JishiError> {
                    d.iter()
                        .find(|(k, _)| k == 名)
                        .map(|(_, v)| 数(v, "多边形", 名))
                        .transpose()?
                        .ok_or_else(|| err("值错误", format!("「多边形」的点里缺「{名}」")))
                };
                format!("[{},{}]", 取("x")?, 取("y")?)
            }
            other => {
                return Err(err(
                    "类型错误",
                    format!(
                        "「多边形」第 {} 个点要是 `[x, y]` 或 `{{x, y}}`，给的是{}",
                        i + 1,
                        crate::type_label(other)
                    ),
                ))
            }
        });
    }
    Ok(format!("[{}]", 段.join(",")))
}

/// 从字典里取一个数（缺了就报「缺 xxx」）。
fn 取数(d: &[(String, Val)], 名: &str, 谁: &str) -> Result<f64, JishiError> {
    d.iter()
        .find(|(k, _)| k == 名)
        .map(|(_, v)| 数(v, 谁, 名))
        .transpose()?
        .ok_or_else(|| {
            err_with_hint(
                "值错误",
                format!("「{谁}」要的字典里缺「{名}」"),
                "矩形是 {x, y, 宽, 高}；圆是 {x, y, 半径}".into(),
            )
        })
}

/// 一个「形状」：矩形或圆。碰撞判定只认这两种。
struct 形状 {
    x: f64,
    y: f64,
    w: f64,
    h: f64,
    圆: bool,
}

impl 形状 {
    fn 读(v: &Val, 谁: &str) -> Result<Self, JishiError> {
        let d = 字典(v, "碰到", 谁)?;
        let x = 取数(&d, "x", "碰到")?;
        let y = 取数(&d, "y", "碰到")?;
        // 圆：给「半径」；矩形：给「宽」「高」
        if let Some((_, r)) = d.iter().find(|(k, _)| k == "半径") {
            let r = 数(r, "碰到", "半径")?;
            Ok(形状 { x, y, w: r, h: r, 圆: true })
        } else {
            Ok(形状 {
                x,
                y,
                w: 取数(&d, "宽", "碰到")?,
                h: 取数(&d, "高", "碰到")?,
                圆: false,
            })
        }
    }

    /// 圆心 / 左上角（矩形的 x,y 是左上角；圆的 x,y 是圆心）。
    fn 中心(&self) -> (f64, f64) {
        if self.圆 {
            (self.x, self.y)
        } else {
            (self.x + self.w / 2.0, self.y + self.h / 2.0)
        }
    }

    /// 点到这个形状的「是否有交叠」判定（点用零尺寸矩形表示时够用）。
    fn 含点(&self, px: f64, py: f64) -> bool {
        if self.圆 {
            let dx = px - self.x;
            let dy = py - self.y;
            return dx * dx + dy * dy <= self.w * self.w;
        }
        px >= self.x && px <= self.x + self.w && py >= self.y && py <= self.y + self.h
    }
}

// ---------------------------------------------------------------------------
// 模块
// ---------------------------------------------------------------------------

pub(crate) fn module() -> HashMap<String, Val> {
    table! { "图形";
        // ---- 画布 ----------------------------------------------------------
        "准备" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("准备", args, 0, 1)?;
            要画布()?;
            let mut 宽 = String::from("null");
            let mut 高 = String::from("null");
            let mut 背景 = String::from("null");
            if let Some(o) = args.first() {
                if !matches!(o, Val::None_) {
                    let d = 字典(o, "准备", "选项")?;
                    for (k, v) in &d {
                        match k.as_str() {
                            "宽" => 宽 = 标量json(v)?,
                            "高" => 高 = 标量json(v)?,
                            "背景" => 背景 = 标量json(v)?,
                            other => {
                                return Err(err_with_hint(
                                    "值错误",
                                    format!("「准备」的选项里没有「{other}」"),
                                    "能用的是：宽 / 高 / 背景".into(),
                                ))
                            }
                        }
                    }
                }
            }
            // shim 回 `{宽, 高}`（逻辑像素，与它实际建出来的位图对应）
            let r = host_call(
                "准备",
                &[Val::Str(format!("[{宽},{高},{背景}]").into())],
            )?;
            let d = 字典(&r, "准备", "结果")?;
            let w = 取数(&d, "宽", "准备")?;
            let h = 取数(&d, "高", "准备")?;
            frame::记画布(w, h);
            Ok(r)
        },
        "宽度" => |_a: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            要画布()?;
            let (w, _) = 画布尺寸()?;
            Ok(Val::Float(w))
        },
        "高度" => |_a: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            要画布()?;
            let (_, h) = 画布尺寸()?;
            Ok(Val::Float(h))
        },

        // ---- 绘制 ----------------------------------------------------------
        "清屏" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("清屏", args, 0, 1)?;
            要画布()?;
            let 色 = 可选文本(args, 0, "清屏", "颜色")?.unwrap_or_else(|| "#000".into());
            host_call("清屏", &[Val::Str(色.into())])
        },
        "矩形" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("矩形", args, 4, 5)?;
            要画布()?;
            let 样 = 样式json(args.get(4))?;
            host_call("矩形", &[
                Val::Float(数(&args[0], "矩形", "x")?),
                Val::Float(数(&args[1], "矩形", "y")?),
                Val::Float(数(&args[2], "矩形", "宽")?),
                Val::Float(数(&args[3], "矩形", "高")?),
                Val::Str(样.into()),
            ])
        },
        "圆" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("圆", args, 3, 4)?;
            要画布()?;
            let 样 = 样式json(args.get(3))?;
            host_call("圆", &[
                Val::Float(数(&args[0], "圆", "x")?),
                Val::Float(数(&args[1], "圆", "y")?),
                Val::Float(数(&args[2], "圆", "半径")?),
                Val::Str(样.into()),
            ])
        },
        "椭圆" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("椭圆", args, 4, 5)?;
            要画布()?;
            let 样 = 样式json(args.get(4))?;
            host_call("椭圆", &[
                Val::Float(数(&args[0], "椭圆", "x")?),
                Val::Float(数(&args[1], "椭圆", "y")?),
                Val::Float(数(&args[2], "椭圆", "半宽")?),
                Val::Float(数(&args[3], "椭圆", "半高")?),
                Val::Str(样.into()),
            ])
        },
        "线" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("线", args, 4, 5)?;
            要画布()?;
            let 样 = 样式json(args.get(4))?;
            host_call("线", &[
                Val::Float(数(&args[0], "线", "x1")?),
                Val::Float(数(&args[1], "线", "y1")?),
                Val::Float(数(&args[2], "线", "x2")?),
                Val::Float(数(&args[3], "线", "y2")?),
                Val::Str(样.into()),
            ])
        },
        "多边形" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("多边形", args, 1, 2)?;
            要画布()?;
            let 点 = 点列json(&args[0])?;
            let 样 = 样式json(args.get(1))?;
            host_call("多边形", &[Val::Str(点.into()), Val::Str(样.into())])
        },
        "文本" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("文本", args, 3, 4)?;
            要画布()?;
            let 内 = 文本(&args[0], "文本", "内容")?;
            let 样 = 样式json(args.get(3))?;
            host_call("文本", &[
                Val::Str(内.into()),
                Val::Float(数(&args[1], "文本", "x")?),
                Val::Float(数(&args[2], "文本", "y")?),
                Val::Str(样.into()),
            ])
        },
        "测文本" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("测文本", args, 1, 2)?;
            要画布()?;
            let 内 = 文本(&args[0], "测文本", "内容")?;
            let 样 = 样式json(args.get(1))?;
            host_call("测文本", &[Val::Str(内.into()), Val::Str(样.into())])
        },

        // ---- 图像 ----------------------------------------------------------
        "读图" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("读图", args, 1)?;
            要画布()?;
            let 路径 = 文本(&args[0], "读图", "路径")?;
            host_call("读图", &[Val::Str(路径.into())])
        },
        "画图" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("画图", args, 3, 4)?;
            要画布()?;
            let 句 = 数(&args[0], "画图", "图")?;
            let 选 = 选项json(args.get(3))?;
            host_call("画图", &[
                Val::Float(句),
                Val::Float(数(&args[1], "画图", "x")?),
                Val::Float(数(&args[2], "画图", "y")?),
                Val::Str(选.into()),
            ])
        },
        "画子图" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need_range("画子图", args, 9, 10)?;
            要画布()?;
            let 句 = 数(&args[0], "画子图", "图")?;
            let 名 = ["源x", "源y", "源宽", "源高", "目标x", "目标y", "目标宽", "目标高"];
            let mut 数们: Vec<f64> = Vec::with_capacity(8);
            for (i, n) in 名.iter().enumerate() {
                数们.push(数(&args[i + 1], "画子图", n)?);
            }
            let 选 = 选项json(args.get(9))?;
            尺(句, &数们, &选)
        },

        // ---- 像素 ----------------------------------------------------------
        "取像素" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("取像素", args, 2)?;
            要画布()?;
            host_call("取像素", &[
                Val::Float(数(&args[0], "取像素", "x")?),
                Val::Float(数(&args[1], "取像素", "y")?),
            ])
        },

        // ---- 相机 ----------------------------------------------------------
        "设相机" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("设相机", args, 2)?;
            要画布()?;
            host_call("设相机", &[
                Val::Float(数(&args[0], "设相机", "x")?),
                Val::Float(数(&args[1], "设相机", "y")?),
            ])
        },
        "读相机" => |_a: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            要画布()?;
            host_call("读相机", &[])
        },

        // ---- 输入 ----------------------------------------------------------
        "按着" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("按着", args, 1)?;
            要画布()?;
            let 键 = 文本(&args[0], "按着", "键")?;
            host_call("按着", &[Val::Str(键.into())])
        },
        "按过" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("按过", args, 1)?;
            要画布()?;
            let 键 = 文本(&args[0], "按过", "键")?;
            host_call("按过", &[Val::Str(键.into())])
        },
        "鼠标" => |_a: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            要画布()?;
            host_call("鼠标", &[])
        },
        "滚轮" => |_a: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            要画布()?;
            host_call("滚轮", &[])
        },

        // ---- 碰撞（纯计算，不问宿主）----------------------------------------
        "碰到" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("碰到", args, 2)?;
            要画布()?;
            let 甲 = 形状::读(&args[0], "甲")?;
            let 乙 = 形状::读(&args[1], "乙")?;
            Ok(Val::Bool(有交叠(&甲, &乙)))
        },
        "点到" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("点到", args, 2)?;
            要画布()?;
            let d = 字典(&args[0], "点到", "点")?;
            let px = 取数(&d, "x", "点到")?;
            let py = 取数(&d, "y", "点到")?;
            let 形 = 形状::读(&args[1], "形状")?;
            Ok(Val::Bool(形.含点(px, py)))
        },
        "距离" => |args: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            need("距离", args, 2)?;
            要画布()?;
            let a = 字典(&args[0], "距离", "甲")?;
            let b = 字典(&args[1], "距离", "乙")?;
            let (x1, y1) = (取数(&a, "x", "距离")?, 取数(&a, "y", "距离")?);
            let (x2, y2) = (取数(&b, "x", "距离")?, 取数(&b, "y", "距离")?);
            Ok(Val::Float(((x2 - x1).powi(2) + (y2 - y1).powi(2)).sqrt()))
        },

        // ---- 帧循环 ---------------------------------------------------------
        "运行" => |args: &[Val], vm: &mut dyn BuiltinHost| -> R {
            need("运行", args, 2)?;
            要画布()?;
            if frame::在事件里() {
                return Err(err_with_hint(
                    "运行期错误",
                    "「图形.运行」只能写在顶层，不能放在事件处理函数里",
                    "事件来的时候顶层已经重跑过一遍了，那时再登记等于让程序从「跑了一半」开始。\
                     把 `图形.运行(初始化, 每帧)` 挪到脚本最外层。"
                        .into(),
                ));
            }
            if frame::有() {
                return Err(err_with_hint(
                    "运行期错误",
                    "帧循环已经在跑了",
                    "要换一套就用 `图形.停()`，或者刷新页面".into(),
                ));
            }
            let 初始化 = match &args[0] {
                Val::Func(_) => Some(args[0].clone()),
                Val::None_ => None,
                other => {
                    return Err(err(
                        "类型错误",
                        format!(
                            "「图形.运行」的第一个参数要函数（初始化），给的是{}",
                            crate::type_label(other)
                        ),
                    ))
                }
            };
            let 每帧 = match &args[1] {
                Val::Func(_) => args[1].clone(),
                other => {
                    return Err(err(
                        "类型错误",
                        format!(
                            "「图形.运行」的第二个参数要函数（每帧），给的是{}",
                            crate::type_label(other)
                        ),
                    ))
                }
            };
            frame::登记(初始化.clone(), 每帧);
            // 初始化**当场跑**（D110 §七）：它跑完才开始出帧。
            // 它报错就直接抛出去 —— 用「启动失败」比「跑到第 3 帧才崩」好查得多。
            if let Some(f) = 初始化 {
                vm.call_value(f, Vec::new())
                    .map_err(|e| crate::err("运行期错误", format!("初始化函数出错：{}", e.message)))?;
            }
            Ok(Val::Bool(true))
        },
        "停" => |_a: &[Val], _vm: &mut dyn BuiltinHost| -> R {
            要画布()?;
            let 有 = frame::有();
            frame::清();
            通知宿主("停帧");
            Ok(Val::Bool(有))
        },
    }
}

/// `画子图` 的参数太多，单独拼（`table!` 里塞 10 个 `Val::Float(...)` 读不下去）。
fn 尺(句: f64, 数们: &[f64], 选: &str) -> Result<Val, JishiError> {
    let mut a: Vec<Val> = Vec::with_capacity(10);
    a.push(Val::Float(句));
    for n in 数们 {
        a.push(Val::Float(*n));
    }
    a.push(Val::Str(选.to_string().into()));
    host_call("画子图", &a)
}

/// 没准备过画布就先准备一块（`宽度` / `高度` 用）。
fn 画布尺寸() -> Result<(f64, f64), JishiError> {
    if let Some(wh) = frame::画布尺寸() {
        return Ok(wh);
    }
    let r = host_call("准备", &[Val::Str("[null,null,null]".into())])?;
    let d = 字典(&r, "准备", "结果")?;
    let w = 取数(&d, "宽", "准备")?;
    let h = 取数(&d, "高", "准备")?;
    frame::记画布(w, h);
    Ok((w, h))
}

// ---------------------------------------------------------------------------
// 碰撞：两种形状（矩形 / 圆）两两组合
// ---------------------------------------------------------------------------
//
// 为什么不问宿主：这是**纯算术**，过一趟 JSON 只是白花钱（一帧可能判定几十次）。
// 而且放在 Rust 里意味着判据能在 Node 里**精确**验它，不依赖 canvas 实现。

/// 两个形状有没有交叠。矩形 × 矩形 / 圆 × 圆 / 矩形 × 圆。
fn 有交叠(a: &形状, b: &形状) -> bool {
    match (a.圆, b.圆) {
        (false, false) => {
            // 轴对齐矩形：两条轴的投影都重叠才算（贴着边算碰到 —— 与「格子游戏」的直觉一致）
            a.x <= b.x + b.w && b.x <= a.x + a.w && a.y <= b.y + b.h && b.y <= a.y + a.h
        }
        (true, true) => {
            let (ax, ay) = a.中心();
            let (bx, by) = b.中心();
            let r = a.w + b.w;
            (ax - bx).powi(2) + (ay - by).powi(2) <= r * r
        }
        _ => {
            // 一个圆一个矩形：把圆心「夹」到矩形里最近的点，比距离
            let (圆, 矩) = if a.圆 { (a, b) } else { (b, a) };
            let (cx, cy) = (圆.x, 圆.y);
            let 近x = cx.max(矩.x).min(矩.x + 矩.w);
            let 近y = cy.max(矩.y).min(矩.y + 矩.h);
            (cx - 近x).powi(2) + (cy - 近y).powi(2) <= 圆.w * 圆.w
        }
    }
}
