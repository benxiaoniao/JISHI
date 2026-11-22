// 流式 GET 的 Worker（M219）—— `网络.获取流` / `网络.SSE` 在 Node 宿主上的实现。
//
// 🔴 **为什么必须是 Worker**：VM 跑在**同步**主循环里，`fetch` / `body.getReader()`
// 全是异步的 —— 主线程一阻塞（`Atomics.wait`），事件循环就停了，`reader.read()`
// 的 promise 永远轮不到兑现。M161（同步 HTTP）、M217（子进程）踩的都是同一条。
//
// 形状与 `process_worker.js` 一致：**常驻** Worker + SharedArrayBuffer + `Atomics.wait`。
// 主线程写「请求码 + 参数」→ 响铃 → 阻塞等答复；Worker 用 5ms 定时器轮询
// （它**不能** `Atomics.wait` —— 一阻塞就收不到 `reader.read()` 的结果）。
//
// ## 协议（Int32Array 前 16 格）
//
//   [0] 响应就绪（主线程等它；Worker 置 1 后 notify）
//   [1] 响应类型：1=数据 2=超时 3=完 4=错误 5=握手(状态+头)
//   [2] 响应数据字节数（写在数据区开头）
//   [3] 响应整数（握手时是状态码）
//   [4] 请求码：1=读块 2=关闭
//   [5] 请求整数：读块时是「想要多少字节」
//   [6] 请求整数：读块时是「等多久（毫秒，0=一直等）」
//   [7] 请求整数：关闭时的「已消费字节数」（用于回收数据区）
//   [8] 请求序号（Worker 看它变）
//
// 数据区从 128 字节处开始（1 MB）：Worker 只在**主线程取走之后**才往里写，
// 所以是「锁步」的 —— 不会覆盖还没读的数据。

const { workerData } = require('worker_threads');

const { sab, url, headers, connectTimeout } = workerData;
const i32 = new Int32Array(sab, 0, 16);
const 数据区 = new Uint8Array(sab, 128);

let 状态码 = 0;
let 头们 = {};
let 队列 = [];          // Buffer 列表（已收到、还没交出去）
let 组 = 0;             // 队列里一共多少字节
let 完 = false;
let 错 = null;
let reader = null;
let 控制器 = null;
let 已关 = false;

/** 把答复写进共享内存并叫醒主线程。 */
function 应(型, 数据, 数值) {
  const buf = 数据 ? Buffer.from(数据) : Buffer.alloc(0);
  const n = Math.min(buf.length, 数据区.length);
  数据区.set(buf.subarray(0, n), 0);
  i32[2] = n;
  i32[3] = 数值 === undefined ? 0 : 数值;
  i32[1] = 型;
  Atomics.store(i32, 0, 1);
  Atomics.notify(i32, 0);
}

/** 收数据：reader.read() 的结果塞进队列。 */
function 收(res) {
  if (res.done) {
    完 = true;
    return;
  }
  const b = Buffer.from(res.value);
  if (b.length > 0) {
    队列.push(b);
    组 += b.length;
  }
}

/** 从队列里取最多 n 字节（不够就有多少给多少）。 */
function 取(n) {
  const 出 = [];
  let 要 = n;
  while (要 > 0 && 队列.length > 0) {
    const 头 = 队列[0];
    if (头.length <= 要) {
      出.push(头);
      要 -= 头.length;
      组 -= 头.length;
      队列.shift();
    } else {
      出.push(头.subarray(0, 要));
      队列[0] = 头.subarray(要);
      组 -= 要;
      要 = 0;
    }
  }
  return Buffer.concat(出);
}

// ---------------------------------------------------------------------------
// 握手：先发请求、等到响应头（这一步失败要让主线程看见「为什么连不上」）
// ---------------------------------------------------------------------------
(async () => {
  控制器 = new AbortController();
  const 计时 = setTimeout(() => 控制器.abort(),
    Math.max(100, Number(connectTimeout) * 1000));
  try {
    const resp = await fetch(url, {
      method: 'GET',
      signal: 控制器.signal,
      headers: Object.assign({ 'User-Agent': 'jishi/0.1' }, headers || {}),
    });
    clearTimeout(计时);
    状态码 = resp.status;
    resp.headers.forEach((v, k) => { 头们[k] = v; });
    reader = resp.body ? resp.body.getReader() : null;
    if (!reader) 完 = true;
    应(5, JSON.stringify(头们), 状态码);
  } catch (e) {
    clearTimeout(计时);
    错 = String((e && e.message) ? e.message : e);
    应(4, 错, 0);
    return;
  }

  // 后台不停地把数据拉进队列（主线程只管从队列里取）
  (async () => {
    try {
      while (!已关 && !完) {
        const r = await reader.read();
        收(r);
        if (r.done) break;
      }
    } catch (e) {
      if (!已关) {
        错 = String((e && e.message) ? e.message : e);
        完 = true;
      }
    }
  })();

  // -------------------------------------------------------------------------
  // 请求循环：5ms 轮询（不能 Atomics.wait —— 一阻塞就收不到 read() 的结果）
  // -------------------------------------------------------------------------
  let 上次序号 = 0;
  let 等起 = 0;
  let 等多久 = 0;
  let 想多少 = 0;
  let 在等 = false;
  setInterval(() => {
    const 序号 = Atomics.load(i32, 8);
    if (序号 !== 上次序号) {
      上次序号 = 序号;
      const 码 = i32[4];
      if (码 === 2) {
        已关 = true;
        try { 控制器.abort(); } catch (e) { /* 已经结束 */ }
        应(3, '', 0);
        return;
      }
      // 码 1：读块
      想多少 = Math.max(1, i32[5] | 0);
      等多久 = Math.max(0, i32[6] | 0);
      在等 = true;
      等起 = Date.now();
    }
    if (!在等) return;
    if (错) {
      在等 = false;
      应(4, 错, 0);
      return;
    }
    if (组 > 0) {
      在等 = false;
      应(1, 取(想多少), 0);
      return;
    }
    if (完) {
      在等 = false;
      应(3, '', 0);
      return;
    }
    if (等多久 > 0 && Date.now() - 等起 >= 等多久) {
      在等 = false;
      应(2, '', 0);          // 超时：上层给「空」
    }
  }, 5);
})();
