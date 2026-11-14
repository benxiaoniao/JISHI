//! 自研安装向导（M200）：**不依赖 Inno** 的图形界面。
//!
//! ## 为什么复用 `native_window` 而不是自己起 tao/wry
//!
//! 窗口 + 事件循环 + WebView 这套东西，项目里已经有一份经过 M174/M178 验证的
//! 实现（含 Windows 上 WebView2 用户数据目录那个坑：默认落在 exe 旁边，
//! 装到 Program Files 时就起不来）。安装器再写一遍，等于把那些坑重踩一次，
//! 而且**两边会各自修**。
//!
//! 所以只借底层，界面和协议归安装器：`native_window::设外部处理器` 是唯一的洞
//! （语言侧永远是 `None`，见那里的说明）。
//!
//! ## 这一版做了什么（M200 第二轮，按用户实测反馈补）
//!
//! * **进度条**：安装跑在后台线程，进度经 `后台执行脚本` 回主线程注入页面。
//!   为什么一定要后台线程：装 3000 个文件要几秒，在主线程装 = 窗口白屏、
//!   鼠标转圈、用户以为死了（Windows 超过 5 秒不响应就会变「未响应」）。
//! * **桌面快捷方式**：勾了才建，卸载时按同一个路径删。
//! * **装完不弹消息框**：页面自己说「装好了」，停 1.2 秒再关窗 —— 比
//!   「窗口突然消失 + 弹一个框」连贯。
//! * **可以自动化**（`JISHI_SETUP_AUTO=<目录>[|path][|desk]`）：判据要能跑完
//!   整条 UI 路径。做法是页面**真的点一下**那个按钮（不是绕过 UI 直接调安装），
//!   所以「按钮 → IPC → 安装」这整段也在判据覆盖里。
//!
//! ⚠️ **机器上没有 WebView2 时**（Win7 / 精简系统 / 被组策略禁掉）：
//! `打开` 会失败，返回 `结局::开不了窗`，主程序弹一句「请用 /S 静默安装」。

use std::path::{Path, PathBuf};
use std::sync::Mutex;

/// 用户的选择 + 装的结果 —— 页面与主线程之间就靠这一个槽。
struct 槽 {
    /// 用户点了「安装」时填进来（`None` = 还没点）。
    选择: Option<选择>,
    /// 后台线程装完填进来。
    结果: Option<Result<(), String>>,
}

static 共享: Mutex<槽> = Mutex::new(槽 {
    选择: None,
    结果: None,
});

/// 用户在向导里做的选择。
#[derive(Debug, Clone)]
pub struct 选择 {
    pub 目录: PathBuf,
    pub 加PATH: bool,
    pub 要桌面: bool,
}

/// 向导的结局。
pub enum 结局 {
    /// 用户点了取消 / 直接关窗（没装）。
    取消,
    /// 装好了。
    装好,
    /// 装了，但失败了（页面已经给用户看过原因，调用方再弹一次兜底）。
    失败(String),
    /// 窗口根本起不来（没有 WebView2 / 没有桌面）。
    开不了窗(String),
}

/// 后台线程要执行的那件事：`(目录, 加PATH, 要桌面, 进度回调) -> 结果`。
///
/// `'static`：它会被 `std::thread::spawn` 送到后台线程 —— 捕获住的东西必须是
/// 自己拥有的（`Arc` 之类），不能是借用（借用活不过那个线程）。
pub type 任务 = dyn Fn(&Path, bool, bool, &进度报) -> Result<(), String> + Send + 'static;

/// 进度回调：`(0..=1, 一句话)`。
type 进度报<'a> = dyn Fn(f32, &str) + 'a;

/// 开向导，**阻塞**到装完 / 用户取消。
///
/// 真正的安装不在这里写：调用方把 `任务` 传进来。界面与副作用分开，
/// 以后换成命令行问答也不用动安装逻辑。
pub fn 问(
    名字: &str,
    版本: &str,
    字节数: i64,
    默认目录: &Path,
    已装版本: Option<&str>,
    任务: Box<任务>,
) -> 结局 {
    use jishi_ffi::native_window;

    if let Ok(mut g) = 共享.lock() {
        *g = 槽 {
            选择: None,
            结果: None,
        };
    }

    //: `JISHI_SETUP_AUTO=<目录>[|path][|desk]` —— 判据用（见模块文档）。
    let 自动 = std::env::var("JISHI_SETUP_AUTO").ok().filter(|s| !s.is_empty());
    let 网页 = 页面(名字, 版本, 字节数, 默认目录, 已装版本);

    // 🔴 **`with_html` 直接把页面喂给 WebView**，不走 `file://` 临时文件：
    //    2026-11-14 实测，`file://` 页面只要发一次 IPC，wry 就会在
    //    `Request::builder().uri(Source).unwrap()` 上 panic
    //    （`http::Error(InvalidUri(InvalidFormat))`）——**按钮一点就崩**，
    //    而窗口看起来完全正常（所以「截图能过」不等于「能用」）。
    //    代价：`document.URL` 是 `about:blank`（这个页面不依赖它）。
    let 号 = match native_window::打开HTML(&format!("安装 {名字}"), 660.0, 560.0, &网页) {
        Ok(v) => v,
        Err(e) => {
            jishi_ffi::installer::日志(&format!("开窗失败：{e}"));
            return 结局::开不了窗(e);
        }
    };

    // 页面 → 宿主。**在主线程被调**（WebView2 的 IPC 回调就在 UI 线程），
    // 所以这里可以放心开系统对话框、执行脚本、请求关闭。
    let 任务 = Mutex::new(Some(任务));
    native_window::设外部处理器(Some(Box::new(move |正文: &str| -> bool {
        let Ok(j) = jishi_ffi::json::parse(正文) else {
            return false;
        };
        let 动作 = j.get("动作").and_then(|v| v.as_str()).unwrap_or("");
        match 动作 {
            // 系统目录选择框。**必须在主线程调**（IPC 回调就在主线程）——
            // 后台线程开这种模态框在 Windows 上会挂住事件循环。
            "选目录" => {
                if let Some(d) = rfd::FileDialog::new().set_title("装到哪儿").pick_folder() {
                    let js = format!(
                        "__安装.收到({{\"事件\":\"目录\",\"值\":{}}})",
                        引号(&d.display().to_string())
                    );
                    native_window::执行脚本(号, &js);
                }
                true
            }
            // 页面每 250ms 问一次「你在了吗」。为什么用轮询而不是页面 onload 发一次：
            // 宿主是在 `打开()` **之后**才装上处理器的，页面可能已经加载完 ——
            // 那一次就丢了，而窗口是好的、按钮也能点，只有自动化那条路径永远不触发。
            // 轮询 + 宿主回一句「在」= 不管谁先谁后都能接上，代价只是几次 IPC。
            "就绪" => {
                let _ = native_window::执行脚本(号, "__安装.在 = true;");
                if let Some(值) = &自动 {
                    // 页面自己填表 + **真的点一下**「安装」按钮：
                    // 走的是 onclick → IPC → 安装，和用户手点完全同一条路。
                    let 片: Vec<&str> = 值.split('|').collect();
                    let 目录 = 片.first().copied().unwrap_or("");
                    let js = format!(
                        "__安装.自动({{目录:{}, path:{}, desk:{}}});",
                        引号(目录),
                        片.iter().any(|x| *x == "path"),
                        片.iter().any(|x| *x == "desk"),
                    );
                    native_window::执行脚本(号, &js);
                }
                true
            }
            "安装" => {
                let 目录 = j
                    .get("目录")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .trim()
                    .to_string();
                let 加PATH = j.get("path").and_then(|v| v.as_bool()).unwrap_or(false);
                let 要桌面 = j.get("桌面").and_then(|v| v.as_bool()).unwrap_or(false);
                if 目录.is_empty() {
                    let _ = native_window::执行脚本(
                        号,
                        "__安装.收到({\"事件\":\"错误\",\"值\":\"先选一个安装目录\"})",
                    );
                    return true;
                }
                if let Ok(mut g) = 共享.lock() {
                    // 双击两下「安装」不该装两遍（页面也会 disable 按钮，
                    // 但两条 IPC 可能都在 disable 之前就发出去了）
                    if g.选择.is_some() {
                        return true;
                    }
                    g.选择 = Some(选择 {
                        目录: PathBuf::from(目录),
                        加PATH,
                        要桌面,
                    });
                }
                let 选 = {
                    let g = 共享.lock().ok();
                    g.and_then(|x| x.选择.clone())
                };
                let Some(选) = 选 else { return true };
                let Some(任务) = 任务.lock().ok().and_then(|mut g| g.take()) else {
                    return true;
                };
                // ① 页面切到「安装中」
                let _ = native_window::执行脚本(
                    号,
                    "__安装.开始();",
                );
                // ② 装 —— **后台线程**（见模块文档：主线程装 = 窗口未响应）
                let 号2 = 号;
                std::thread::spawn(move || {
                    let 进度 = |x: f32, s: &str| {
                        // 记一行：界面是不是真在动，事后只能靠日志看
                        // （截图只能证明「某一刻」，证明不了「走完了整条」）。
                        jishi_ffi::installer::日志(&format!("进度 {:3.0}% {s}", x * 100.0));
                        native_window::后台执行脚本(
                            号2,
                            &format!("__安装.进度({x}, {});", 引号(s)),
                        );
                    };
                    let r = 任务(&选.目录, 选.加PATH, 选.要桌面, &进度);
                    let 错 = match &r {
                        Ok(()) => None,
                        Err(e) => Some(e.clone()),
                    };
                    if let Ok(mut g) = 共享.lock() {
                        g.结果 = Some(r);
                    }
                    match 错 {
                        None => {
                            // 让「✓ 装好了」停一下再关窗 —— 立刻消失用户会以为崩了
                            let _ = native_window::后台执行脚本(
                                号2,
                                "__安装.成功(\"装好了。去开始菜单里找它就能打开。\");",
                            );
                            std::thread::sleep(std::time::Duration::from_millis(1200));
                            native_window::请求关闭(号2);
                        }
                        Some(e) => {
                            // 失败**不关窗**：用户得看见原因。按钮变成「关闭」。
                            let _ = native_window::后台执行脚本(
                                号2,
                                &format!("__安装.失败({});", 引号(&e)),
                            );
                        }
                    }
                });
                true
            }
            // 「取消」与「关闭」是同一个动作：还没装 = 取消；装完失败 = 关窗。
            "取消" | "关闭" => {
                let _ = native_window::请求关闭(号);
                true
            }
            _ => false,
        }
    })));

    jishi_ffi::installer::日志(&format!("向导窗口已开：{号}（内嵌页面）"));
    // `等()` 阻塞到**所有**窗口关掉。安装器进程里只会开这一个。
    native_window::等();
    jishi_ffi::installer::日志("向导窗口已关（等() 返回）");
    native_window::设外部处理器(None);

    let (选过, 结果) = match 共享.lock() {
        Ok(mut g) => (g.选择.take().is_some(), g.结果.take()),
        Err(_) => (false, None),
    };
    match (选过, 结果) {
        (false, _) => 结局::取消,
        // 装完失败后用户又点了关窗：结果还在，别当成「取消」
        (true, Some(Ok(()))) => 结局::装好,
        (true, Some(Err(e))) => 结局::失败(e),
        // 选了但结果还没填上 —— 只可能是窗口在安装途中被强杀（任务管理器）
        (true, None) => 结局::失败("安装窗口被关掉了，没装完。".to_string()),
    }
}

/// JSON 字符串字面量（带引号、转义）。
fn 引号(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
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
    out.push('"');
    out
}

fn 人类大小(n: i64) -> String {
    const 单位: [&str; 5] = ["B", "KB", "MB", "GB", "TB"];
    let mut v = n as f64;
    let mut i = 0;
    while v >= 1024.0 && i + 1 < 单位.len() {
        v /= 1024.0;
        i += 1;
    }
    if i == 0 {
        format!("{n} B")
    } else {
        format!("{v:.1} {}", 单位[i])
    }
}

fn 转义网页(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

fn 页面(名字: &str, 版本: &str, 字节数: i64, 默认目录: &Path, 已装: Option<&str>) -> String {
    let 升级说明 = match 已装 {
        Some(旧) => format!(
            r#"<div class="up">这台机器上装着 <b>{}</b>，这次会把它升到 <b>{}</b>。你放在安装目录里的文件不会被删。</div>"#,
            转义网页(旧),
            转义网页(版本)
        ),
        None => String::new(),
    };
    模板
        .replace("{名字}", &转义网页(名字))
        .replace("{版本}", &转义网页(版本))
        .replace("{大小}", &人类大小(字节数))
        .replace("{默认目录}", &转义网页(&默认目录.display().to_string()))
        .replace("{升级说明}", &升级说明)
}

const 模板: &str = r#"<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>安装</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin:0; height:100vh; display:flex; flex-direction:column;
         font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;
         background:#f5f6f8; color:#1a1a1a; font-size:14px; }
  header { padding:22px 28px 18px; background:#fff; border-bottom:1px solid #e5e7ea; }
  h1 { margin:0 0 6px; font-size:20px; font-weight:600; }
  .sub { color:#6b7280; font-size:13px; }
  main { flex:1; padding:20px 28px; overflow:auto; }
  .card { background:#fff; border:1px solid #e5e7ea; border-radius:10px; padding:16px 18px; }
  .up { margin-top:8px; padding:8px 10px; background:#fff7ed; border:1px solid #fed7aa;
        border-radius:6px; color:#9a3412; font-size:13px; }
  .row { display:flex; gap:10px; align-items:center; margin-top:12px; }
  input[type=text] { flex:1; padding:9px 11px; border:1px solid #d1d5db; border-radius:6px;
                     font-size:13px; font-family:inherit; }
  button { padding:9px 18px; border-radius:6px; border:1px solid #d1d5db; background:#fff;
           font-size:13px; font-family:inherit; cursor:pointer; }
  button:hover { background:#f9fafb; }
  button.primary { background:#c0392b; border-color:#c0392b; color:#fff; }
  button.primary:hover { background:#a93226; }
  button:disabled { opacity:.55; cursor:default; }
  footer { padding:14px 28px; background:#fff; border-top:1px solid #e5e7ea;
           display:flex; justify-content:flex-end; gap:10px; }
  label { display:flex; gap:8px; align-items:center; cursor:pointer; }
  .hint { color:#9ca3af; font-size:12px; margin-top:8px; }
  .err { color:#b91c1c; font-size:13px; margin-top:10px; min-height:18px; white-space:pre-wrap; }
  /* 进度：只有点过「安装」才显示 */
  #进度区 { display:none; margin-top:18px; }
  .bar { height:8px; background:#e5e7eb; border-radius:99px; overflow:hidden; }
  #bar { height:100%; width:0%; background:#c0392b; border-radius:99px;
         transition:width .18s ease-out; }
  #阶段 { margin-top:8px; color:#6b7280; font-size:12px; }
  .ok { color:#15803d; font-weight:600; }
</style></head><body>
<header>
  <h1>安装 {名字}</h1>
  <div class="sub">版本 {版本} · 约 {大小}</div>
  {升级说明}
</header>
<main>
  <div class="card">
    <div id="表单">
      <div style="font-weight:600">装到哪儿</div>
      <div class="row">
        <input id="dir" type="text" value="{默认目录}" spellcheck="false">
        <button id="browse">浏览…</button>
      </div>
      <div class="hint">装在当前用户下，不需要管理员权限；不想要了就在「设置 → 应用」里卸。</div>
      <div style="font-weight:600; margin-top:18px">顺带做这些</div>
      <div class="row"><label><input id="desk" type="checkbox" checked> 在桌面放一个快捷方式</label></div>
      <div class="row"><label><input id="path" type="checkbox"> 加进 PATH（新开的终端里能直接敲程序名）</label></div>
    </div>
    <div id="进度区">
      <div style="font-weight:600" id="进度标题">正在安装…</div>
      <div class="bar"><div id="bar"></div></div>
      <div id="阶段">准备中…</div>
    </div>
    <div class="err" id="err"></div>
  </div>
</main>
<footer>
  <button id="cancel">取消</button>
  <button id="go" class="primary">安装</button>
</footer>
<script>
  var $ = function (id) { return document.getElementById(id); };
  function 发(o) {
    var s = JSON.stringify(o);
    if (window.ipc && window.ipc.postMessage) window.ipc.postMessage(s);
  }
  window.__安装 = {
    在: false,
    收到: function (t) {
      var d; try { d = typeof t === "string" ? JSON.parse(t) : t; } catch (e) { return; }
      if (d.事件 === "目录") $("dir").value = d.值;
      if (d.事件 === "错误") $("err").textContent = d.值;
    },
    自动: function (o) {
      $("dir").value = o.目录 || $("dir").value;
      $("path").checked = !!o.path;
      $("desk").checked = !!o.desk;
      $("go").click();
    },
    开始: function () {
      $("表单").style.display = "none";
      $("进度区").style.display = "block";
      $("cancel").style.display = "none";
      $("go").disabled = true;
      $("go").textContent = "正在安装…";
      $("err").textContent = "";
    },
    进度: function (x, s) {
      $("bar").style.width = Math.max(2, Math.min(100, x * 100)).toFixed(1) + "%";
      $("阶段").textContent = s;
    },
    成功: function (消息) {
      $("bar").style.width = "100%";
      $("进度标题").className = "ok";
      $("进度标题").textContent = "✓ " + 消息;
      $("阶段").textContent = "";
    },
    失败: function (e) {
      $("进度标题").textContent = "没装上";
      $("阶段").textContent = "";
      $("err").textContent = e;
      var c = $("cancel");
      c.style.display = "";
      c.textContent = "关闭";
      c.onclick = function () { 发({动作: "关闭"}); };
    }
  };
  $("browse").onclick = function () { 发({动作: "选目录"}); };
  $("cancel").onclick = function () { 发({动作: "取消"}); };
  $("go").onclick = function () {
    var dir = $("dir").value.trim();
    if (!dir) { $("err").textContent = "先选一个安装目录"; return; }
    $("go").disabled = true;
    $("go").textContent = "正在安装…";
    发({动作: "安装", 目录: dir,
        path: $("path").checked, 桌面: $("desk").checked});
  };
  // 「你在了吗」轮询：宿主是在开窗**之后**才装上处理器的，一次性的 onload
  // 可能早于那一刻（那一次就丢了，只有自动化路径会永远不触发）。
  (function 握手() {
    if (window.__安装.在) return;
    发({动作: "就绪"});
    setTimeout(握手, 250);
  })();
</script></body></html>"#;
