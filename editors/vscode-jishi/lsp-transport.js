'use strict';
/**
 * 最小 LSP 传输层：spawn 一个语言服务器进程，按 Content-Length 分帧收发 JSON-RPC。
 *
 * **为什么不用 `vscode-languageclient`**：那是个几百 KB 的依赖，而我们要的只是
 * 「分帧 + 请求应答 + 通知分发」三件事（本项目在 C 内核、正则引擎上都是同一套
 * 取舍：能自己写清楚的就别引依赖）。这个文件**不依赖 vscode**，所以可以脱离
 * 编辑器单跑自检（见 `verify-lsp.js`）。
 */

const cp = require('child_process');

class LspTransport {
  /**
   * @param {string} command 可执行文件（默认 `jishi`）
   * @param {string[]} args 参数（默认 `['lsp']`）
   * @param {object} [opts] `{cwd, env}`；`cwd` 决定工作区根目录
   */
  constructor(command, args, opts = {}) {
    this.command = command;
    this.args = args;
    this.opts = opts;
    this.proc = null;
    this._seq = 0;
    this._pending = new Map();      // id -> {resolve, reject}
    this._buffer = Buffer.alloc(0);
    this._notificationHandlers = [];
    this._requestHandlers = [];
    this._exitHandlers = [];
    //: 收发消息的观察者（`{方向, 消息}`）——`jishi.server.trace` 打开时
    //: 接到输出面板，排查「编辑器里没反应」这类问题时用。
    this.onMessage = null;
  }

  start() {
    this.proc = cp.spawn(this.command, this.args, {
      cwd: this.opts.cwd || undefined,
      env: this.opts.env || process.env,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });
    this.proc.stdout.on('data', (chunk) => this._onData(chunk));
    this.proc.stderr.on('data', () => { /* 服务器日志：不往用户界面灌 */ });
    this.proc.on('exit', (code, signal) => {
      const err = new Error(`基石语言服务已退出（code=${code} signal=${signal}）`);
      for (const { reject } of this._pending.values()) reject(err);
      this._pending.clear();
      for (const h of this._exitHandlers) h(err);
    });
    this.proc.on('error', (e) => {
      for (const { reject } of this._pending.values()) reject(e);
      this._pending.clear();
      for (const h of this._exitHandlers) h(e);
    });
    return this;
  }

  onNotification(handler) { this._notificationHandlers.push(handler); return this; }
  onRequest(handler) { this._requestHandlers.push(handler); return this; }
  onExit(handler) { this._exitHandlers.push(handler); return this; }

  /** 发一条请求，返回 Promise。`timeoutMs` 到了就 reject（默认 15s）。 */
  request(method, params, timeoutMs = 15000) {
    const id = ++this._seq;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (this._pending.delete(id)) {
          reject(new Error(`请求超时：${method}`));
        }
      }, timeoutMs);
      this._pending.set(id, {
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
      this._send({ jsonrpc: '2.0', id, method, params: params || {} });
    });
  }

  notify(method, params) {
    this._send({ jsonrpc: '2.0', method, params: params || {} });
  }

  _send(obj) {
    if (!this.proc || !this.proc.stdin.writable) return;
    if (this.onMessage) {
      try { this.onMessage('→', obj); } catch (e) { /* 观察者不该影响主流程 */ }
    }
    const body = Buffer.from(JSON.stringify(obj), 'utf8');
    this.proc.stdin.write(`Content-Length: ${body.length}\r\n\r\n`);
    this.proc.stdin.write(body);
  }

  _onData(chunk) {
    this._buffer = Buffer.concat([this._buffer, chunk]);
    for (;;) {
      const sep = this._buffer.indexOf('\r\n\r\n');
      if (sep < 0) return;
      const header = this._buffer.slice(0, sep).toString('ascii');
      const m = /Content-Length:\s*(\d+)/i.exec(header);
      if (!m) {                       // 头坏了：丢掉这一段，别死循环
        this._buffer = this._buffer.slice(sep + 4);
        continue;
      }
      const len = parseInt(m[1], 10);
      const start = sep + 4;
      if (this._buffer.length < start + len) return;   // 还没收全
      const body = this._buffer.slice(start, start + len).toString('utf8');
      this._buffer = this._buffer.slice(start + len);
      let msg;
      try {
        msg = JSON.parse(body);
      } catch (e) {
        continue;
      }
      if (this.onMessage) {
        try { this.onMessage('←', msg); } catch (e) { /* 同上 */ }
      }
      this._dispatch(msg);
    }
  }

  _dispatch(msg) {
    if (msg.id !== undefined && msg.method === undefined) {
      const slot = this._pending.get(msg.id);
      if (!slot) return;
      this._pending.delete(msg.id);
      if (msg.error) slot.reject(new Error(msg.error.message || '语言服务返回错误'));
      else slot.resolve(msg.result);
      return;
    }
    if (msg.id !== undefined && msg.method) {
      // 服务器反过来问客户端（如 workspace/configuration）：必须应答，
      // 不然服务器会一直等，表现成「编辑器里没反应」。
      let result = null;
      for (const h of this._requestHandlers) {
        const r = h(msg.method, msg.params);
        if (r !== undefined) { result = r; break; }
      }
      this._send({ jsonrpc: '2.0', id: msg.id, result });
      return;
    }
    if (msg.method) {
      for (const h of this._notificationHandlers) h(msg.method, msg.params);
    }
  }

  dispose() {
    if (!this.proc) return;
    try { this.notify('exit'); } catch (e) { /* 已经没了 */ }
    this.proc.kill();
    this.proc = null;
  }
}

module.exports = { LspTransport };
