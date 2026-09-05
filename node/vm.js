// -*- coding: utf-8 -*-
// 基石字节码虚拟机的指令分发（M13.2）。
//
// 消费 JSON 字节码（M13.1 反序列化后的结构），逐条解释指令。
// 语义委托 runtime.js，与 Python 侧 jishi/vm.py 严格对齐。

'use strict';

const R = require('./runtime');

const MAX_FRAMES = 1000;

// 指令号（与 jishi/opcodes.py 的 Op 完全一致）
const Op = {
  HALT: 0, LOAD_CONST: 1, LOAD_GLOBAL: 2, STORE_GLOBAL: 3,
  LOAD_FAST: 4, STORE_FAST: 5, LOAD_DEREF: 6, STORE_DEREF: 7, LOAD_CELL: 8,
  POP_TOP: 9, DUP_TOP: 10, ROT_TWO: 11, ROT_THREE: 12,
  BIN_OP: 13, UNARY_OP: 14, COMPARE: 15, JUMP: 16,
  POP_JUMP_IF_FALSE: 17, POP_JUMP_IF_TRUE: 18, JUMP_IF_FALSE_OR_POP: 19,
  CALL: 20, RETURN: 21, MAKE_FUNCTION: 22, BUILD_LIST: 23, BUILD_DICT: 24,
  GET_ATTR: 25, SET_ATTR: 26, GET_ITEM: 27, SET_ITEM: 28,
  GET_ITER: 29, FOR_ITER: 30, IMPORT: 31, DUP_TWO: 32, LOOP_SETUP: 33,
  SETUP_LOOP: 34, POP_BLOCK: 35, BREAK_LOOP: 36, CONTINUE_LOOP: 37,
  STORE_LAST: 38, SETUP_TRY: 39, POP_TRY: 40, THROW: 41, CHECK_SIGNAL: 42,
  MATCH_EXC: 43, END_FINALLY: 44, BUILD_CLASS: 45, UNPACK: 46, LIST_APPEND: 47,
};

// 二元运算号 → 符号
const BINOP = { 0: '+', 1: '-', 2: '*', 3: '/', 4: '//', 5: '%', 6: '**' };
// 比较号 → 符号
const CMPOP = { 0: '==', 1: '!=', 2: '<', 3: '>', 4: '<=', 5: '>=' };

class Handler {
  constructor(catchIp, finallyIp, sp, seq) {
    this.catchIp = catchIp; this.finallyIp = finallyIp;
    this.sp = sp; this.seq = seq;
  }
}

class Frame {
  constructor(code, ip, base, locals, cells) {
    this.code = code;
    this.ip = ip;
    this.base = base;
    this.locals = locals;
    this.cells = cells;
    this.loops = [];       // [depth, cont, brk, seq]
    this.handlers = [];    // Handler[]
    this.pendingReturn = R.PENDING_NONE;
    this.blockSeq = 0;
  }
}

class VM {
  constructor(payload) {
    this.consts = payload.consts;
    this.names = payload.names;
    this.kwNames = payload.kw_names;
    this.codes = payload.codes;
    this.main = payload.main;
    this.filename = payload.filename || '<字节码>';
    this.globals = R.newBuiltins();
    this.stack = [];
    this.frames = [];
    this.lastValue = null;
    // 捕获输出（供对拍测试）
    this._output = '';
  }

  run() {
    const main = this.codes[this.main];
    const frame = new Frame(main, 0, 0, new Array(main.nlocals).fill(R.UNSET),
                            new Array(main.ncells).fill(null).map(() => new R.Cell()));
    this.frames.push(frame);
    try {
      this._execute(null);
    } finally {
      this.frames.length = 0;
    }
    return this.lastValue;
  }

  // 外部回调（Python 高阶函数场景对齐；JS 里较少用，但保留）
  callFunction(fn, args, kwargs, line, col) {
    const code = fn.code;
    const { locals, values } = this._bind(code, args, kwargs || {}, line, col, fn.defaults);
    const frameCells = code.cellvars.map(() => new R.Cell()).concat(fn.cells);
    this._storeCells(code, frameCells, values);
    const base = this.stack.length;
    this.stack.push(null);
    const frame = new Frame(code, 0, base, locals, frameCells);
    this.frames.push(frame);
    try {
      this._execute(frame);
    } finally {
      if (this.frames.length && this.frames[this.frames.length - 1] === frame) {
        this.frames.pop();
      }
    }
    const value = this.stack[base];
    this.stack.length = base;
    return value;
  }

  _bind(code, args, kwargs, line, col, defaults) {
    const params = code.params;
    if (args.length > params.length) {
      throw R.typeErr(`函数「${code.name}」需要 ${params.length} 个参数，但传了 ${args.length + Object.keys(kwargs).length} 个`, line, col);
    }
    const bound = new Map();
    params.forEach((p, i) => { if (i < args.length) bound.set(p, args[i]); });
    for (const [k, v] of Object.entries(kwargs)) {
      if (!params.includes(k)) throw R.typeErr(`函数「${code.name}」没有叫「${k}」的参数`, line, col);
      bound.set(k, v);
    }
    if (defaults) {
      params.forEach((p, i) => {
        if (!bound.has(p) && defaults[i] != null) bound.set(p, defaults[i]);
      });
    }
    const missing = params.filter(p => !bound.has(p));
    if (missing.length) throw R.typeErr(`函数「${code.name}」缺少参数：${missing.join('、')}`, line, col);

    const locals = new Array(code.nlocals).fill(R.UNSET);
    const values = [];
    params.forEach((p, i) => {
      values.push(bound.get(p));
      const slot = code.param_local[i];
      if (slot >= 0) locals[slot] = bound.get(p);
    });
    return { locals, values };
  }

  _storeCells(code, cells, values) {
    values.forEach((v, i) => {
      const ci = code.param_cell[i];
      if (ci >= 0) cells[ci].value = v;
    });
  }

  _cellName(code, idx) {
    if (idx < code.cellvars.length) return code.cellvars[idx];
    const j = idx - code.cellvars.length;
    if (j >= 0 && j < code.freevars.length) return code.freevars[j];
    return `?单元${idx}`;
  }

  _execute(stopAt) {
    const stack = this.stack;
    const frames = this.frames;
    const consts = this.consts;
    const names = this.names;
    const kwNames = this.kwNames;
    const codes = this.codes;
    const globs = this.globals;
    const filename = this.filename;

    while (frames.length) {
      const frame = frames[frames.length - 1];
      const code = frame.code;
      const instrs = code.instrs;
      const frameLocals = frame.locals;
      const frameCells = frame.cells;
      const base = frame.base;
      const localNames = code.local_names;
      let ip = frame.ip;

      try {
        for (;;) {
          const ins = instrs[ip];
          const op = ins.op;
          ip += 1;

          if (op === Op.LOAD_FAST) {
            const v = frameLocals[ins.a];
            if (v === R.UNSET) throw R.nameErr(`找不到名字「${ins.a < localNames.length ? localNames[ins.a] : '$' + ins.a}」`, ins.line, ins.col);
            stack.push(v);
          } else if (op === Op.LOAD_CONST) {
            stack.push(consts[ins.a]);
          } else if (op === Op.STORE_FAST) {
            frameLocals[ins.a] = stack.pop();
          } else if (op === Op.BIN_OP) {
            const r = stack.pop(); const l = stack.pop();
            stack.push(R.applyBinop(BINOP[ins.a], l, r, ins.line, ins.col));
          } else if (op === Op.COMPARE) {
            const r = stack.pop(); const l = stack.pop();
            stack.push(R.applyCompare(CMPOP[ins.a], l, r, ins.line, ins.col));
          } else if (op === Op.JUMP) {
            ip = ins.a;
          } else if (op === Op.POP_JUMP_IF_FALSE) {
            if (!R.truthy(stack.pop())) ip = ins.a;
          } else if (op === Op.POP_JUMP_IF_TRUE) {
            if (R.truthy(stack.pop())) ip = ins.a;
          } else if (op === Op.POP_TOP) {
            stack.pop();
          } else if (op === Op.LOAD_GLOBAL) {
            const name = names[ins.a];
            if (!(name in globs)) throw R.nameErr(`找不到名字「${name}」`, ins.line, ins.col);
            stack.push(globs[name]);
          } else if (op === Op.STORE_GLOBAL) {
            globs[names[ins.a]] = stack.pop();
          } else if (op === Op.LOAD_DEREF) {
            const v = frameCells[ins.a].value;
            if (v === R.UNSET) throw R.nameErr(`找不到名字「${this._cellName(code, ins.a)}」`, ins.line, ins.col);
            stack.push(v);
          } else if (op === Op.STORE_DEREF) {
            frameCells[ins.a].value = stack.pop();
          } else if (op === Op.LOAD_CELL) {
            stack.push(frameCells[ins.a]);
          } else if (op === Op.DUP_TOP) {
            stack.push(stack[stack.length - 1]);
          } else if (op === Op.DUP_TWO) {
            stack.push(stack[stack.length - 2], stack[stack.length - 1]);
          } else if (op === Op.ROT_TWO) {
            const a = stack.pop(); const b = stack.pop();
            stack.push(a, b);
          } else if (op === Op.ROT_THREE) {
            const a = stack.pop(); const b = stack.pop(); const c = stack.pop();
            stack.push(a, c, b);
          } else if (op === Op.UNARY_OP) {
            const v = stack.pop();
            stack.push(R.applyUnary(ins.a === 0 ? '-' : '非', v, ins.line, ins.col));
          } else if (op === Op.JUMP_IF_FALSE_OR_POP) {
            if (R.truthy(stack[stack.length - 1])) stack.pop();
            else ip = ins.a;
          } else if (op === Op.CALL) {
            const nargs = ins.a;
            const kwi = ins.b;
            const knames = kwi >= 0 ? kwNames[kwi] : [];
            const nkw = knames.length;
            const fnBase = stack.length - (1 + nargs + nkw);
            const ret = this._doCall(frame, fnBase, nargs, knames, nkw, ins);
            if (ret === null) { frame.ip = ip; break; }
          } else if (op === Op.RETURN) {
            const value = stack.pop();
            const nxt = this._returnViaFinally(frame, value);
            if (nxt >= 0) {
              ip = nxt;
            } else {
              stack.length = base;
              stack.push(value);
              frames.pop();
              if (frame === stopAt || frames.length === 0) return;
              break;
            }
          } else if (op === Op.MAKE_FUNCTION) {
            const ncells = ins.b;
            const ndefaults = ins.c;
            let defaults = null;
            if (ndefaults) {
              const tailValues = stack.splice(stack.length - ndefaults, ndefaults);
              const pad = codes[ins.a].params.length - ndefaults;
              defaults = new Array(pad).fill(null).concat(tailValues);
            }
            const cells = ncells ? stack.splice(stack.length - ncells, ncells) : [];
            stack.push(new R.VmFunction(codes[ins.a].name, codes[ins.a], cells, this, defaults));
          } else if (op === Op.BUILD_CLASS) {
            const n = ins.a;
            const hasBase = Boolean(ins.b);
            const name = stack.pop();
            const methods = new Map();
            if (n) {
              const fns = stack.splice(stack.length - n, n);
              for (const fn of fns) methods.set(fn.name, fn);
            }
            const baseCls = hasBase ? stack.pop() : null;
            stack.push(new R.JishiClass(name, methods, baseCls));
          } else if (op === Op.UNPACK) {
            const n = ins.a;
            const v = stack.pop();
            const items = R.unpackValues(v, n, ins.line, ins.col);
            for (let i = items.length - 1; i >= 0; i--) stack.push(items[i]);
          } else if (op === Op.BUILD_LIST) {
            const n = ins.a;
            stack.push(n ? stack.splice(stack.length - n, n) : []);
          } else if (op === Op.BUILD_DICT) {
            const n = ins.a;
            const flat = n ? stack.splice(stack.length - 2 * n, 2 * n) : [];
            const pairs = [];
            for (let i = 0; i < flat.length; i += 2) pairs.push([flat[i], flat[i + 1]]);
            stack.push(R.buildDict(pairs, ins.line, ins.col));
          } else if (op === Op.GET_ATTR) {
            stack.push(R.getAttr(stack.pop(), names[ins.a], ins.line, ins.col));
          } else if (op === Op.LIST_APPEND) {
            const val = stack.pop();
            const obj = stack.pop();
            const m = R.getAttr(obj, '追加', ins.line, ins.col);
            this._callBoundMethod(m, [val], ins.line, ins.col);
            stack.push(null);
          } else if (op === Op.SET_ATTR) {
            const val = stack.pop();
            R.setAttr(stack.pop(), names[ins.a], val, ins.line, ins.col);
          } else if (op === Op.GET_ITEM) {
            const idx = stack.pop();
            stack.push(R.getItem(stack.pop(), idx, ins.line, ins.col));
          } else if (op === Op.SET_ITEM) {
            const val = stack.pop();
            const idx = stack.pop();
            R.setItem(stack.pop(), idx, val, ins.line, ins.col);
          } else if (op === Op.GET_ITER) {
            const obj = stack.pop();
            stack.push(makeIterator(obj, ins.line, ins.col));
          } else if (op === Op.FOR_ITER) {
            const it = stack[stack.length - 1];
            const next = it.next();
            if (next.done) { stack.pop(); ip = ins.a; }
            else stack.push(next.value);
          } else if (op === Op.SETUP_LOOP) {
            frame.blockSeq += 1;
            frame.loops.push([stack.length, ins.a, ins.b, frame.blockSeq]);
          } else if (op === Op.POP_BLOCK) {
            if (frame.loops.length) frame.loops.pop();
          } else if (op === Op.BREAK_LOOP) {
            if (this._signalNeedsDispatch(frame)) throw new R.RunBreak(ins.line, ins.col);
            if (!frame.loops.length) throw new R.RunBreak(ins.line, ins.col);
            const lp = frame.loops[frame.loops.length - 1];
            stack.length = lp[0];
            ip = lp[2];
          } else if (op === Op.CONTINUE_LOOP) {
            if (this._signalNeedsDispatch(frame)) throw new R.RunContinue(ins.line, ins.col);
            if (!frame.loops.length) throw new R.RunContinue(ins.line, ins.col);
            const lp = frame.loops[frame.loops.length - 1];
            stack.length = lp[0];
            ip = lp[1];
          } else if (op === Op.LOOP_SETUP) {
            const n = stack.pop();
            const iv = Number(n);
            if (!Number.isInteger(iv)) throw R.typeErr(`「${R.display(n)}」不是有效的循环次数`, ins.line, ins.col);
            frameLocals[ins.a] = iv;
          } else if (op === Op.IMPORT) {
            const src = ins.b;
            stack.push(this._doImport(names[ins.a], src, ins.line, ins.col));
          } else if (op === Op.STORE_LAST) {
            this.lastValue = stack.pop();
          } else if (op === Op.SETUP_TRY) {
            frame.blockSeq += 1;
            frame.handlers.push(new Handler(ins.a, ins.b, stack.length, frame.blockSeq));
          } else if (op === Op.POP_TRY) {
            if (frame.handlers.length) frame.handlers.pop();
          } else if (op === Op.THROW) {
            const v = stack.pop();
            frame.ip = ip;
            throw R.makeException(v, ins.line, ins.col);
          } else if (op === Op.CHECK_SIGNAL) {
            if (R.isLoopSignal(stack[stack.length - 1])) ip = ins.a;
          } else if (op === Op.MATCH_EXC) {
            const cond = stack.pop();
            const err = stack[stack.length - 1];
            stack.push(R.exceptionMatches(err, cond, ins.line, ins.col));
          } else if (op === Op.END_FINALLY) {
            const pr = frame.pendingReturn;
            if (pr !== R.PENDING_NONE) {
              frame.pendingReturn = R.PENDING_NONE;
              const nxt = this._returnViaFinally(frame, pr);
              if (nxt >= 0) {
                ip = nxt;
              } else {
                stack.length = base;
                stack.push(pr);
                frames.pop();
                if (frame === stopAt || frames.length === 0) return;
                break;
              }
            }
          } else if (op === Op.HALT) {
            frames.pop();
            return;
          } else {
            throw R.runErr(`未知指令 ${op}`, ins.line, ins.col);
          }

          frame.ip = ip;
        }
      } catch (exc) {
        if (!(exc instanceof R.JishiError)) {
          // JS 原生异常：包装成基石异常
          exc = R.runErr(String(exc && exc.message ? exc.message : exc), frame.ip >= 0 ? instrs[Math.min(frame.ip, instrs.length - 1)]?.line : null, null);
        }
        frame.ip = ip;
        if (this._dispatchException(exc, stopAt)) continue;
        throw exc;
      }
    }
  }

  _doCall(frame, fnBase, nargs, knames, nkw, ins) {
    const stack = this.stack;
    const fn = stack[fnBase];
    const args = stack.slice(fnBase + 1, fnBase + 1 + nargs);
    const kwargs = {};
    if (nkw) {
      for (let i = 0; i < nkw; i++) kwargs[knames[i]] = stack[fnBase + 1 + nargs + i];
    }

    if (fn instanceof R.VmFunction) {
      const sub = fn.code;
      const { locals, values } = this._bind(sub, args, kwargs, ins.line, ins.col, fn.defaults);
      if (this.frames.length >= MAX_FRAMES) throw R.runErr('递归层数太深了，是不是函数忘了写结束条件？', ins.line, ins.col);
      stack.length = fnBase;
      const subCells = sub.cellvars.map(() => new R.Cell()).concat(fn.cells);
      this._storeCells(sub, subCells, values);
      this.frames.push(new Frame(sub, 0, fnBase, locals, subCells));
      return null;
    }

    stack.length = fnBase;
    stack.push(this._callValue(fn, args, kwargs, ins.line, ins.col));
    return true;
  }

  _callValue(fn, args, kwargs, line, col) {
    if (fn instanceof R.Builtin) {
      return fn.fn(...args);
    }
    if (fn instanceof R.Module) {
      throw R.typeErr(`模块「${fn.name}」不能直接调用`, line, col);
    }
    if (fn instanceof R.JishiClass) {
      return R.makeInstance(fn, args, line, col);
    }
    if (fn instanceof R.BoundMethod) {
      return fn.impl(fn.obj, args, line, col);
    }
    if (fn instanceof R.BoundUserMethod) {
      return this.callFunction(fn.fn, [fn.instance, ...args], kwargs, line, col);
    }
    if (fn instanceof R.ExcType) {
      return fn.make(args[0] !== undefined ? args[0] : '', line, col);
    }
    throw R.runErr(`「${R.display(fn)}」不能调用`, line, col);
  }

  _callBoundMethod(m, args, line, col) {
    m.impl(m.obj, args, line, col);
  }

  _dispatchException(exc, stopAt) {
    const frames = this.frames;
    const stack = this.stack;
    while (frames.length) {
      const frame = frames[frames.length - 1];
      if (frame === stopAt) return false;
      const h = frame.handlers.length ? frame.handlers[frame.handlers.length - 1] : null;
      const lp = frame.loops.length ? frame.loops[frame.loops.length - 1] : null;
      if (R.isLoopSignal(exc)) {
        if (lp && (h === null || lp[3] > h.seq)) {
          stack.length = lp[0];
          frame.ip = exc instanceof R.RunBreak ? lp[2] : lp[1];
          return true;
        }
        if (h !== null) {
          frame.handlers.pop();
          stack.length = h.sp;
          stack.push(exc);
          frame.ip = h.catchIp;
          return true;
        }
      } else if (h !== null) {
        frame.handlers.pop();
        stack.length = h.sp;
        stack.push(exc);
        frame.ip = h.catchIp;
        return true;
      }
      const callee = frames.pop();
      stack.length = callee.base;
    }
    return false;
  }

  _signalNeedsDispatch(frame) {
    if (!frame.handlers.length) return false;
    if (!frame.loops.length) return true;
    return frame.handlers[frame.handlers.length - 1].seq > frame.loops[frame.loops.length - 1][3];
  }

  _returnViaFinally(frame, value) {
    const stack = this.stack;
    while (frame.handlers.length) {
      const h = frame.handlers.pop();
      if (h.finallyIp >= 0) {
        stack.length = h.sp;
        frame.pendingReturn = value;
        return h.finallyIp;
      }
    }
    return -1;
  }

  // 标准库导入：JS 里只支持内建的核心模块（数学/文本/随机等轻量实现）
  _doImport(name, src, line, col) {
    // src: 0=标准库 1=python 2=本地包
    if (src === 1) throw R.runErr(`JS 引擎不支持「从 python」导入「${name}」`, line, col);
    if (src === 2) throw R.runErr(`JS 引擎不支持「从 本地包」导入「${name}」`, line, col);
    const mod = STDLIB[name];
    if (!mod) throw R.nameErr(`没有找到标准库模块「${name}」`, line, col);
    return mod;
  }
}

// ---------------------------------------------------------------------------
// 迭代器
// ---------------------------------------------------------------------------
function makeIterator(obj, line, col) {
  let it;
  if (Array.isArray(obj)) {
    let i = 0;
    it = { next: () => i < obj.length ? { value: obj[i++], done: false } : { done: true } };
  } else if (obj instanceof Map) {
    const keys = [...obj.keys()];
    let i = 0;
    it = { next: () => i < keys.length ? { value: keys[i++], done: false } : { done: true } };
  } else if (typeof obj === 'string') {
    const chars = [...obj];
    let i = 0;
    it = { next: () => i < chars.length ? { value: chars[i++], done: false } : { done: true } };
  } else {
    throw R.typeErr(`「${R.display(obj)}」不能遍历，遍历需要列表、文本等`, line, col);
  }
  return it;
}

// ---------------------------------------------------------------------------
// JS 内建标准库（轻量子集：数学/文本）
// ---------------------------------------------------------------------------
const STDLIB = {
  '数学': new R.Module('数学', {
    '开方': new R.Builtin('开方', (x) => new R.FloatBox(Math.sqrt(x))),
    '平方': new R.Builtin('平方', (x) => x * x),
    '幂': new R.Builtin('幂', (a, b) => a ** b),
    '绝对值': new R.Builtin('绝对值', (x) => Math.abs(x)),
    '向上取整': new R.Builtin('向上取整', (x) => Math.ceil(x)),
    '向下取整': new R.Builtin('向下取整', (x) => Math.floor(x)),
    '四舍五入': new R.Builtin('四舍五入', (x, d = 0) => {
      const f = 10 ** d; return Math.round(x * f) / f;
    }),
    '圆周率': Math.PI,
    '自然常数': Math.E,
    '最大公约数': new R.Builtin('最大公约数', (a, b) => {
      while (b) { [a, b] = [b, a % b]; } return a;
    }),
    '最小公倍数': new R.Builtin('最小公倍数', (a, b) => a * b / gcd(a, b)),
    '阶乘': new R.Builtin('阶乘', (n) => {
      let r = 1; for (let i = 2; i <= n; i++) r *= i; return r;
    }),
  }),
  '文本': new R.Module('文本', {
    '拼接': new R.Builtin('拼接', (seq, sep = '') => seq.join(sep)),
    '重复': new R.Builtin('重复', (s, n) => s.repeat(n)),
    '计数': new R.Builtin('计数', (s, sub) => s.split(sub).length - 1),
    '是数字': new R.Builtin('是数字', (s) => !Number.isNaN(Number(s)) && s.trim() !== ''),
    '是字母': new R.Builtin('是字母', (s) => /^[a-zA-Z]+$/.test(s)),
  }),
  '随机': new R.Module('随机', {
    '随机整数': new R.Builtin('随机整数', (lo, hi) => lo + Math.floor(Math.random() * (hi - lo + 1))),
    '随机小数': new R.Builtin('随机小数', () => Math.random()),
    '随机选择': new R.Builtin('随机选择', (seq) => seq[Math.floor(Math.random() * seq.length)]),
    '洗牌': new R.Builtin('洗牌', (seq) => {
      const a = [...seq];
      for (let i = a.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [a[i], a[j]] = [a[j], a[i]];
      }
      return a;
    }),
  }),
};

function gcd(a, b) { while (b) { [a, b] = [b, a % b]; } return a; }

module.exports = { VM, Op };
