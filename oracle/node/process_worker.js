// -*- coding: utf-8 -*-
// 基石的 `进程` 模块在 Node 宿主上的**读线程**（M217）。
//
// 为什么要单独一个 Worker：VM 跑在同步主循环里，`child.stdout` 的 'data'
// 事件永远轮不到（事件循环被 VM 占着）。M161 的「同步 HTTP」踩过同一条，
// 结论一样：Worker + SharedArrayBuffer + Atomics.wait。
//
// 与其他宿主对齐的三条口径（见 `oracle/jishi/stdlib/进程.py` 的头注）：
//   ① 读行超时就回「空」，读到 EOF 也回「空」；
//   ② 等待只等**直接子进程**，绝不等管道 EOF（孙进程攥着 stdout 也不会拖住）；
//   ③ 一切文本按 UTF-8 解码，坏字节给替换符（`StringDecoder` 干这个）。
//
// 协议（Int32Array，前 9 格）：
//   [0] 响应就绪（主线程等它）  [1] 响应类型  [2] 响应字节数  [3] 响应整数
//   [4] 请求码                  [5] 请求整数  [6] 请求字节数
//   [8] 请求序号（Worker 看它变）
// 响应类型：1 文本 / 2 空 / 3 真 / 4 假 / 5 整数 / 6 错误
// 请求码：0 写文本 / 1 读行 / 2 读错误行 / 3 在运行 / 4 终止 / 5 等待 / 6 退出码
'use strict';

const { workerData } = require('worker_threads');
const { spawn } = require('child_process');
const { StringDecoder } = require('string_decoder');

const { sab, 命令, argv, 目录, 环境, 合并 } = workerData;
const i32 = new Int32Array(sab, 0, 16);
const 命令区 = new Uint8Array(sab, 64, 65536);
const 回区 = new Uint8Array(sab, 64 + 65536, 4 * 1024 * 1024);

function 应(型, 文本, 整数) {
  const b = Buffer.from(文本 === undefined || 文本 === null ? '' : String(文本), 'utf8');
  const n = Math.min(b.length, 回区.length);
  回区.set(b.subarray(0, n));
  i32[1] = 型;
  i32[2] = n;
  i32[3] = 整数 | 0;
  Atomics.store(i32, 0, 1);
  Atomics.notify(i32, 0);
}

const 出队 = [];
const 错队 = [];
let 出完 = false;
let 错完 = false;
let 退出码 = null;
let 起不来 = null;

let child = null;
try {
  child = spawn(命令, argv, {
    cwd: 目录 || undefined,
    env: 环境 || undefined,
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
  });
} catch (e) {
  起不来 = String((e && e.message) || e);
}

function 收(流, 队, 置完) {
  const dec = new StringDecoder('utf8');
  let 余 = '';
  流.on('data', (d) => {
    余 += dec.write(d);
    let k;
    while ((k = 余.indexOf('\n')) >= 0) {
      let 行 = 余.slice(0, k);
      余 = 余.slice(k + 1);
      if (行.endsWith('\r')) 行 = 行.slice(0, -1);
      队.push(行);
    }
  });
  const 收尾 = () => {
    const 尾 = 余 + dec.end();
    if (尾.length) 队.push(尾.endsWith('\r') ? 尾.slice(0, -1) : 尾);
    置完();
  };
  流.on('end', 收尾);
  流.on('error', 收尾);
}

if (child) {
  // ENOENT 这类是**异步**报的（'error' 事件），而 `spawn` 成功会给 'spawn' 事件。
  // 🔴 握手必须等这两个事件之一 —— 不能 spawn 完就回「起来了」：
  //    那样一个不存在的程序会被当成起成功了（M217 实测：Node 上「起不来」不报错）。
  child.on('spawn', () => { 应(3, '', 0); });
  child.on('error', (e) => {
    起不来 = String((e && e.message) || e);
    退出码 = -1;
    出完 = true;
    错完 = true;
    应(6, 起不来, 0);
  });
  child.on('exit', (码, 信号) => {
    // 🔴 三宿主要对得上（同一台机器上）：
    //   * Windows：`terminate`/`kill` 走 TerminateProcess，Python 与 Rust 都拿到 **1**；
    //     Node 拿到的是 `code=null, signal='SIGTERM'` —— 这里补成 1。
    //   * POSIX：Python 给 **-15**（SIGTERM），Rust 给 **-1**（`code()` 是 None）——
    //     统一成 **-1**（「不是正常退出」）。
    退出码 = 码 === null ? (process.platform === 'win32' ? 1 : -1) : 码;
    if (信号 && 码 === null && process.platform !== 'win32') 退出码 = -1;
  });
  // 往已经关掉的 stdin 写会异步报 EPIPE：吞掉它（调用方那边已经按「写不进去」处理）
  if (child.stdin) child.stdin.on('error', () => {});
  if (child.stdout) 收(child.stdout, 出队, () => { 出完 = true; });
  else 出完 = true;
  if (child.stderr) {
    if (合并) 收(child.stderr, 出队, () => { 出完 = true; });
    else 收(child.stderr, 错队, () => { 错完 = true; });
  } else {
    错完 = true;
  }
  // 握手由上面的 'spawn' / 'error' 发（见那段注释）
} else {
  出完 = true;
  错完 = true;
  退出码 = -1;
  应(6, 起不来 || '起不来这个进程', 0);
}

// 挂起的读 / 等：Worker 不能阻塞（阻塞了就收不到 'data'），用 5 ms 定时器轮询。
let 待读 = null;
let 待等 = null;

function 试读() {
  if (!待读) return;
  if (待读.队.length) {
    const 行 = 待读.队.shift();
    待读 = null;
    应(1, 行, 0);
    return;
  }
  if (待读.完()) {
    待读 = null;
    应(2, '', 0);
    return;
  }
  if (待读.截止 !== null && Date.now() >= 待读.截止) {
    待读 = null;
    应(2, '', 0);
  }
}

function 试等() {
  if (!待等) return;
  if (退出码 !== null) {
    const 码 = 退出码;
    待等 = null;
    应(5, '', 码);
    return;
  }
  if (待等.截止 !== null && Date.now() >= 待等.截止) {
    待等 = null;
    应(2, '', 0);
  }
}

let 上次序号 = 0;
//: 已叫过 kill：`在运行` 要立刻变假，不能等 'exit' 事件（那要再等一个事件循环）
let 已叫停 = false;
//: 终止还没回：等 'exit' 或 2 秒截止（Python / Rust 两边都是「kill 之后收尸再返回」）
let 待终止 = null;

setInterval(() => {
  if (child && 退出码 === null && child.exitCode !== null) 退出码 = child.exitCode;
  if (待终止 && (退出码 !== null || Date.now() >= 待终止.截止)) {
    待终止 = null;
    应(3, '', 0);
  }
  试读();
  试等();
  const 序 = Atomics.load(i32, 8);
  if (序 === 上次序号) return;
  上次序号 = 序;
  const 码 = i32[4];
  const 数值 = i32[5];
  const 长 = i32[6];
  const 文本 = Buffer.from(命令区.subarray(0, 长)).toString('utf8');
  if (!child || 起不来) {
    应(6, 起不来 || '起不来这个进程', 0);
    return;
  }
  switch (码) {
    case 0: // 写文本
      // ⚠️ Node 的 `stdin.write()` 对已经关掉的管道**不抛错**（EPIPE 是异步
      //    给的），所以必须自己先看「子进程还在不在」—— 否则「写进一个已经
      //    退出的子进程」会静默成功，而 Python / Rust 两边都报错。
      if (退出码 !== null || 已叫停) {
        应(6, '写不进这个进程：它的标准输入已经关了（子进程退出了？）', 0);
        break;
      }
      try {
        child.stdin.write(文本);
        应(3, '', 0);
      } catch (e) {
        应(6, '写不进这个进程：它的标准输入已经关了（子进程退出了？）', 0);
      }
      break;
    case 1:
    case 2: { // 读行 / 读错误行
      const 是错 = 码 === 2;
      // 上一次的读还没回（正常情况不会发生：主线程在等到回应前是阻塞的）
      if (待读) { 应(6, '上一个「读行」还没结束', 0); break; }
      待读 = {
        队: 是错 ? 错队 : 出队,
        完: 是错 ? (() => 错完) : (() => 出完),
        截止: 数值 < 0 ? null : Date.now() + 数值,
      };
      试读();
      break;
    }
    case 3: // 在运行
      应((退出码 === null && !已叫停) ? 3 : 4, '', 0);
      break;
    case 4: { // 终止
      if (退出码 !== null || 已叫停) { 应(4, '', 0); break; }
      try {
        child.kill();
      } catch (e) {
        /* 已经没了 */
      }
      已叫停 = true;
      // 🔴 等 `exit` 再回：Python / Rust 都是「kill → 收尸 → 返回」，
      //    立刻返回的话紧接着 `在运行` / `退出码` 在 Node 上还是旧值。
      //    等不到（子进程抗 SIGTERM 且没有后续信号）就 2 秒后放弃。
      待终止 = { 截止: Date.now() + 2000 };
      if (退出码 !== null) {
        待终止 = null;
        应(3, '', 0);
      }
      break;
    }
    case 5: // 等待
      if (待等) { 应(6, '上一个「等待」还没结束', 0); break; }
      待等 = { 截止: 数值 < 0 ? null : Date.now() + 数值 };
      试等();
      break;
    case 6: // 退出码
      if (退出码 === null) 应(2, '', 0);
      else 应(5, '', 退出码);
      break;
    default:
      应(6, '不认识的进程请求', 0);
  }
}, 5);
