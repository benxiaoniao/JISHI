//! M174：把 `WebView2Loader.dll` 拷到可执行文件旁边。
//!
//! ## 为什么需要这个（这不是洁癖，是会当场挂）
//!
//! wry 在 Windows 上依赖 `webview2-com-sys`，而它链的是 **`WebView2Loader.dll.lib`
//! （导入库）** —— 也就是说 `jishi-rs.exe` 里有一条对 `WebView2Loader.dll` 的
//! **动态**依赖，而这个 DLL 既不在 exe 旁边、也不在 `System32`。
//!
//! 症状：同目录没有它时，exe **一启动就挂**（`0xC0000135 STATUS_DLL_NOT_FOUND`），
//! 连 `--version` 都出不来。开发机上能跑只是因为 PATH 里恰好有别的软件带了这份
//! DLL —— 那种「偶然可用」在用户机器上必然不成立。
//!
//! 判据 `test_m90::test_独立判据_没有PATH也能跑` 就是拿**清空 PATH** 的方式
//! 把这件事抓出来的（那条判据本来是为了「Rust 不 shell out」而写的，正好逮住了它）。
//!
//! ## 做法
//!
//! `webview2-com-sys` 的 build 脚本已经把三个架构的 DLL 复制到了
//! `target/<profile>/build/webview2-com-sys-*/out/<arch>/`。这里做完本 crate 的
//! 构建之后，把匹配当前架构的那份复制到 `target/<profile>/`（也就是 exe 旁边）。
//!
//! ⚠️ **发行包也要带它**：`tools/build_release.py` 会把同目录的 `*.dll` 一起收进去
//! （见那里的注释）。少了它，用户装完双击就是「没有任何反应 + 没有报错」。

use std::env;
use std::fs;
use std::path::{Path, PathBuf};

fn main() {
    println!("cargo:rerun-if-changed=build.rs");

    // 只在 Windows 上做；别的平台 wry 用的是系统框架，没有这份 DLL。
    if env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows") {
        return;
    }

    let 架构 = match env::var("CARGO_CFG_TARGET_ARCH").as_deref() {
        Ok("x86_64") => "x64",
        Ok("x86") => "x86",
        Ok("aarch64") => "arm64",
        _ => return, // 没见过的架构：交给 wry 自己，别猜
    };

    // OUT_DIR = target/<profile>/build/jishi-ffi-<hash>/out
    let out = PathBuf::from(env::var("OUT_DIR").expect("OUT_DIR 没有？"));
    let Some(形目标) = out.ancestors().nth(3) else {
        return;
    };
    let 目标 = 形目标.to_path_buf(); // target/<profile>/

    let 源 = 找dll(&目标, 架构);
    let Some(源) = 源 else {
        // 找不到不算错（可能是 `--no-default-features` 的构建，压根没编 wry）。
        println!("cargo:warning=没找到 WebView2Loader.dll（{架构}），若这个构建包含原生窗口，运行时会缺 DLL");
        return;
    };
    let 目的 = 目标.join("WebView2Loader.dll");
    if 目的.is_file() {
        // 已经有就不用每次盖（省一次写盘，也让增量构建不受影响）
        return;
    }
    if let Err(e) = fs::copy(&源, &目的) {
        println!("cargo:warning=复制 WebView2Loader.dll 失败：{e}");
    }
}

/// 在 `target/<profile>/build/webview2-com-sys-*/out/<arch>/` 里找那份 DLL。
///
/// 名字里带 hash（`-145475bfed389ae9`），所以只能按前缀扫。
fn 找dll(目标: &Path, 架构: &str) -> Option<PathBuf> {
    let build = 目标.join("build");
    let 条目们 = fs::read_dir(&build).ok()?;
    for 条 in 条目们.flatten() {
        let 名 = 条.file_name();
        let 名 = 名.to_string_lossy();
        if !名.starts_with("webview2-com-sys-") {
            continue;
        }
        let 候选 = 条.path().join("out").join(架构).join("WebView2Loader.dll");
        if 候选.is_file() {
            return Some(候选);
        }
    }
    None
}
