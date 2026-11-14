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
//! ## 第一版的范围（如实记）
//!
//! 点「安装」→ 窗口关 → 后台装 → 完成后弹**系统消息框**说结果。
//! 没有「装的过程中显示进度条」：那要求安装跑在后台线程、进度经
//! `EventLoopProxy` 回主线程再 `evaluate_script`，是**下一刀**。
//! 为什么要先这样：进度条做不好比没有更糟（卡在 90% 不动会让人以为死机），
//! 而「装完弹一句人话」已经比控制台闪一下强得多。
//!
//! ⚠️ **机器上没有 WebView2 时**（Win7 / 精简系统 / 被组策略禁掉）：
//! `打开` 会失败，退回落控制台；再不行就报一句「请用 /S 静默安装」。
//!
//! 📌 **这一版没有「桌面快捷方式」这个勾**：它要在 `安装()` 里多建一个 `.lnk`、
//! 卸载时还要记得删 —— 那是**另一条独立的路径**（装/卸两头都得改，还得防重名）。
//! 先不做，比做一个「勾了但卸不干净」的好。下一刀补，CHANGELOG 里记着。

use std::path::{Path, PathBuf};

/// 用户在向导里做的选择。
#[derive(Debug, Clone)]
pub struct 选择 {
    pub 目录: PathBuf,
    pub 加PATH: bool,
}

/// 用户点「安装」后填进去；`None` = 取消或直接关窗。
static 结果: std::sync::Mutex<Option<选择>> = std::sync::Mutex::new(None);

/// 开向导，**阻塞**到用户点「安装」或关窗。
///
/// 返回 `Some(选择)` = 该真装了；`None` = 用户取消。真正的安装不在这里做 ——
/// 界面与副作用分开，以后换成命令行问答也不用动安装逻辑。
pub fn 问(
    名字: &str,
    版本: &str,
    字节数: i64,
    默认目录: &Path,
    已装版本: Option<&str>,
) -> Option<选择> {
    use jishi_ffi::native_window;

    if let Ok(mut g) = 结果.lock() {
        *g = None;
    }
    let 网页 = 页面(名字, 版本, 字节数, 默认目录, 已装版本);
    // 🔴 用**临时文件 + file://** 而不是 `data:` 网址（2026-11-14 实测）：
    //    `data:text/html,…` 在 WebView2 上「打开成功但页面空白/直接关」——
    //    最坏的一种：`打开` 返回 Ok，用户却什么都没看见。临时文件这条路
    //    多一份清理，但它是**确定能显示**的（M174 的窗口从第一天就是这么喂的）。
    let 临时 = std::env::temp_dir().join(format!("jishi-setup-{}.html", std::process::id()));
    if let Err(e) = std::fs::write(&临时, 网页.as_bytes()) {
        jishi_ffi::installer::日志(&format!("向导页面写不出来（{}）：{e}", 临时.display()));
        return None;
    }
    let 网址 = format!("file:///{}", 临时.display().to_string().replace('\\', "/"));
    let 号 = match native_window::打开(&format!("安装 {名字}"), 660.0, 540.0, &网址) {
        Ok(v) => v,
        Err(e) => {
            jishi_ffi::installer::日志(&format!("开窗失败：{e}"));
            let _ = std::fs::remove_file(&临时);
            return None;
        }
    };

    native_window::设外部处理器(Some(Box::new(move |正文: &str| -> bool {
        let Ok(j) = jishi_ffi::json::parse(正文) else {
            return false;
        };
        let 动作 = j.get("动作").and_then(|v| v.as_str()).unwrap_or("");
        match 动作 {
            // 系统目录选择框。**必须在主线程调**（IPC 回调就在主线程）——
            // 后台线程开这种模态框在 Windows 上会挂住事件循环。
            "选目录" => {
                if let Some(d) = rfd::FileDialog::new()
                    .set_title("装到哪儿")
                    .pick_folder()
                {
                    let js = format!(
                        "__安装.收到({{\"事件\":\"目录\",\"值\":{}}})",
                        引号(&d.display().to_string())
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
                if 目录.is_empty() {
                    let _ = native_window::执行脚本(
                        号,
                        "__安装.收到({\"事件\":\"错误\",\"值\":\"先选一个安装目录\"})",
                    );
                    return true;
                }
                if let Ok(mut g) = 结果.lock() {
                    *g = Some(选择 {
                        目录: PathBuf::from(目录),
                        加PATH,
                    });
                }
                // 关窗 = 告诉 `等()` 可以返回了（装由调用方接着做）
                let _ = native_window::请求关闭(号);
                true
            }
            "取消" => {
                let _ = native_window::请求关闭(号);
                true
            }
            _ => false,
        }
    })));

    jishi_ffi::installer::日志(&format!("向导窗口已开：{号}，网址 {网址}"));
    // `等()` 阻塞到**所有**窗口关掉。安装器进程里只会开这一个。
    native_window::等();
    jishi_ffi::installer::日志("向导窗口已关（等() 返回）");
    native_window::设外部处理器(None);
    let _ = std::fs::remove_file(&临时);
    结果.lock().ok().and_then(|mut g| g.take())
}

/// 把一段 HTML 变成 `data:` 网址。
///
/// 为什么不用临时文件：安装器是**一个 exe**，双击就该能跑 —— 写临时文件会多出
/// 「杀软拦了临时目录」「临时目录只读」这类失败模式，而且窗口关掉后还得清。
/// `data:` 网址没有这些副作用（WebView2 与 WKWebView 都支持）。
fn 数据网址(html: &str) -> String {
    let mut out = String::with_capacity(html.len() * 3 + 64);
    out.push_str("data:text/html;charset=utf-8,");
    for b in html.as_bytes() {
        match *b {
            // 保留字符与「在 URL 里会被吃掉的」一律转义；其余原样（省长度也好看）
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~'
            | b' ' => out.push(*b as char),
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
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
            r#"<div class="up">这台机器上装着 <b>{}</b>，这次会把它升到 <b>{}</b>。</div>"#,
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
  footer { padding:14px 28px; background:#fff; border-top:1px solid #e5e7ea;
           display:flex; justify-content:flex-end; gap:10px; }
  label { display:flex; gap:8px; align-items:center; cursor:pointer; }
  .hint { color:#9ca3af; font-size:12px; margin-top:8px; }
  .err { color:#b91c1c; font-size:13px; margin-top:10px; min-height:18px; }
</style></head><body>
<header>
  <h1>安装 {名字}</h1>
  <div class="sub">版本 {版本} · 约 {大小}</div>
  {升级说明}
</header>
<main>
  <div class="card">
    <div style="font-weight:600">装到哪儿</div>
    <div class="row">
      <input id="dir" type="text" value="{默认目录}" spellcheck="false">
      <button id="browse">浏览…</button>
    </div>
    <div class="hint">装在当前用户下，不需要管理员权限；不想要了就在「设置 → 应用」里卸。</div>
    <div style="font-weight:600; margin-top:18px">顺带做这些</div>
    <div class="row"><label><input id="path" type="checkbox"> 加进 PATH（新开的终端里能直接敲程序名）</label></div>
    <div class="err" id="err"></div>
  </div>
</main>
<footer>
  <button id="cancel">取消</button>
  <button id="go" class="primary">安装</button>
</footer>
<script>
  function 发(o) {
    const s = JSON.stringify(o);
    if (window.ipc && window.ipc.postMessage) window.ipc.postMessage(s);
  }
  window.__安装 = { 收到(t) {
    let d; try { d = typeof t === "string" ? JSON.parse(t) : t; } catch (e) { return; }
    if (d.事件 === "目录") document.getElementById("dir").value = d.值;
    if (d.事件 === "错误") document.getElementById("err").textContent = d.值;
  }};
  document.getElementById("browse").onclick = () => 发({动作:"选目录"});
  document.getElementById("cancel").onclick = () => 发({动作:"取消"});
  document.getElementById("go").onclick = () => {
    const dir = document.getElementById("dir").value.trim();
    if (!dir) { document.getElementById("err").textContent = "先选一个安装目录"; return; }
    document.getElementById("go").disabled = true;
    document.getElementById("go").textContent = "正在安装…";
    发({动作:"安装", 目录: dir,
        path: document.getElementById("path").checked});
  };
</script></body></html>"#;
