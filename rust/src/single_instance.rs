//! 单实例（M175）：`窗口.单实例("名字")` —— 「再点一次图标，别开第二个窗口」。
//!
//! ## 为什么不用「命名互斥量 / flock」
//!
//! 那两个是平台专属 API（Win32 `CreateMutexW` / unix `flock`），而且**只能回答
//! 「有没有人在跑」**——叫不醒它、也带不了参数。而「单实例唤醒」要的正是后者：
//! 第二个进程把命令行参数交给第一个，然后自己退出。
//!
//! ## 做法：锁文件 + 本地端口（零依赖，三平台同一份代码）
//!
//! ```text
//!   首个实例    抢到 `<临时目录>/jishi-<名字的散列>.instance`（create_new 是原子的）
//!              → bind 127.0.0.1:0（端口由系统分配）
//!              → 把端口写进那个文件
//!              → 起一个线程 accept，收到正文就回调
//!   第二个实例  读文件拿端口 → connect → 把参数发过去 → 自己退出
//!   陈旧文件    连不上 ⇒ 说明上一个进程死了（崩了 / 被 kill）⇒ 删掉重抢
//! ```
//!
//! ## 三个坑（都踩过或推演过）
//!
//! 1. 🔴 **`create_new` 才是锁**。先 `path.exists()` 再创建有竞态：两个进程同时
//!    启动会都看到「不存在」，于是**两个都当首个**。`create_new` 由内核保证原子。
//! 2. 🔴 **端口要「写完再让读的人看到」**。刚 `create_new` 出来的是空文件，
//!    这时另一个进程读到的是空串。所以：读不到端口时**等一会儿再读**
//!    （最多 2 秒），而不是立刻判定「陈旧」。
//! 3. 🔴 **只监听 `127.0.0.1`**。这只是同机进程打个招呼，不该对外网开口子。
//!
//! ## 名字怎么变成文件名
//!
//! 名字是用户给的中文/任意文本，直接当文件名在三个平台上都不安全（分隔符、大小写、
//! 长度）。这里取 `sha256` 前 16 个十六进制字符 —— 同一个名字永远映射到同一个文件，
//! 又不用操心转义。

use std::fs::OpenOptions;
use std::io::{Read, Write};
use std::net::{Ipv4Addr, SocketAddr, TcpListener, TcpStream};
use std::path::PathBuf;
use std::time::{Duration, Instant};

fn 实例文件(名: &str) -> PathBuf {
    let 印 = crate::sha256_hex(名.as_bytes());
    std::env::temp_dir().join(format!("jishi-{}.instance", &印[..16]))
}

fn 读端口(文件: &PathBuf) -> Option<u16> {
    let 文 = std::fs::read_to_string(文件).ok()?;
    let 数 = 文.trim().parse::<u16>().ok()?;
    if 数 == 0 {
        None
    } else {
        Some(数)
    }
}

/// 把正文送给已经在跑的那个实例。成功 = 有人收下了。
fn 送(端口: u16, 正文: &str) -> bool {
    let 址 = SocketAddr::from((Ipv4Addr::LOCALHOST, 端口));
    // 连接超时要短：这是本机 loopback，连不上就是没人在听。
    let Ok(mut 流) = TcpStream::connect_timeout(&址, Duration::from_millis(400)) else {
        return false;
    };
    let _ = 流.write_all(正文.as_bytes());
    // 关掉写端，让对方读到 EOF（它用的 read_to_string）
    let _ = 流.shutdown(std::net::Shutdown::Write);
    true
}

/// 抢首个实例的身份。
///
/// - 抢到 → `Ok(true)`，并且已经起好后台线程；有人来叫时执行 `回调(正文)`。
/// - 没抢到（别人在跑，已把 `参数` 交过去）→ `Ok(false)`。
/// - 出错（临时目录不可写等）→ `Err(能照着改的一句话)`。
pub fn 抢(
    名: &str,
    参数: &[String],
    回调: impl Fn(String) + Send + 'static,
) -> Result<bool, String> {
    let 文件 = 实例文件(名);
    let 正文 = 参数.join("\n");

    // 两轮：第一轮失败多半是「陈旧文件」（持有者崩了），删掉重来一次。
    for 轮 in 0..2 {
        if let Some(端口) = 读端口(&文件) {
            if 送(端口, &正文) {
                return Ok(false); // 叫醒了那个，自己该退
            }
            // 🔴 连不上 ≠ 一定陈旧：也可能是「刚 create_new、端口还没写进去」。
            //    给它 2 秒的宽限（见模块头坑 2）。
            if 等它写好(&文件, &正文) {
                return Ok(false);
            }
            let _ = std::fs::remove_file(&文件);
        }

        match OpenOptions::new().write(true).create_new(true).open(&文件) {
            Ok(mut f) => {
                let 监听 = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
                    .map_err(|e| format!("单实例监听起不来（127.0.0.1 随机端口）：{e}"))?;
                let 端口 = 监听
                    .local_addr()
                    .map_err(|e| format!("拿不到单实例监听的端口：{e}"))?
                    .port();
                f.write_all(端口.to_string().as_bytes())
                    .map_err(|e| format!("写不了实例文件 {}：{e}", 文件.display()))?;
                drop(f);

                // 后台线程**拥有**监听器 —— 它活着，端口就活着；线程随进程退出而消失，
                // 所以不需要额外的清理（陈旧文件由下一个启动者负责识别并删掉）。
                std::thread::spawn(move || {
                    for 流 in 监听.incoming() {
                        let Ok(mut 流) = 流 else { continue };
                        let mut 收 = String::new();
                        let _ = 流.read_to_string(&mut 收);
                        if !收.is_empty() {
                            回调(收);
                        }
                    }
                });
                return Ok(true);
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                // 有人刚抢到，但可能还没把端口写进去 —— 等它一下。
                if 等它写好(&文件, &正文) {
                    return Ok(false);
                }
                if 轮 == 1 {
                    return Err(format!(
                        "没法确认另一个实例是否在跑：实例文件一直没写出端口（{}）\n\
                         删掉它再试一次；如果反复出现，说明上一个进程卡在建窗口那一步",
                        文件.display()
                    ));
                }
                let _ = std::fs::remove_file(&文件);
            }
            Err(e) => {
                return Err(format!(
                    "建不了实例文件 {}：{e}（临时目录不可写？）",
                    文件.display()
                ))
            }
        }
    }
    Err("单实例没抢成（理论上到不了这里）".into())
}

/// 等文件里出现端口，最多 2 秒；等到了就把正文送过去。
fn 等它写好(文件: &PathBuf, 正文: &str) -> bool {
    let 截止 = Instant::now() + Duration::from_secs(2);
    while Instant::now() < 截止 {
        std::thread::sleep(Duration::from_millis(50));
        if let Some(端口) = 读端口(文件) {
            if 送(端口, 正文) {
                return true;
            }
        }
    }
    false
}
