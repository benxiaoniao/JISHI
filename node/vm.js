// -*- coding: utf-8 -*-
// 基石字节码虚拟机的指令分发（M13.2）。
//
// 消费 JSON 字节码（M13.1 反序列化后的结构），逐条解释指令。
// 语义委托 runtime.js，与 Python 侧 jishi/vm.py 严格对齐。

'use strict';

const R = require('./runtime');
const fs = require('fs');

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
  BUILD_SLICE: 48,
};

// 二元运算号 → 符号
const BINOP = { 0: '+', 1: '-', 2: '*', 3: '/', 4: '//', 5: '%', 6: '**' };
// 比较号 → 符号
// 6–9 是 M23.2 的语义比较（成员测试 / 同一性）。此前只映射了 0–5，
// 于是 `在`/`不是` 这类会变成 `CMPOP[6] === undefined` 再报「不支持的比较」
// ——和 Rust 侧 M28 暴露的问题一模一样（M30 补上）。
const CMPOP = {
  0: '==', 1: '!=', 2: '<', 3: '>', 4: '<=', 5: '>=',
  6: '在', 7: '不在', 8: '是', 9: '不是',
};

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
    const kinds = code.paramKind || [];
    // 形参种类（M25）：0=普通 1=*参数 2=**选项
    const starPos = kinds.indexOf(1);
    const dstarPos = kinds.indexOf(2);
    // 能吃普通位置实参的个数 = *参数 之前的那些
    let npos = params.length;
    if (starPos >= 0) npos = starPos;
    else if (dstarPos >= 0) npos = dstarPos;

    if (args.length > npos && starPos < 0) {
      throw R.typeErr(`函数「${code.name}」需要 ${npos} 个参数，但传了 ${args.length + Object.keys(kwargs).length} 个`, line, col);
    }
    const bound = new Map();
    for (let i = 0; i < Math.min(args.length, npos); i++) bound.set(params[i], args[i]);
    if (starPos >= 0) bound.set(params[starPos], args.slice(npos));
    const extra = new Map();
    for (const [k, v] of Object.entries(kwargs)) {
      if (params.slice(0, npos).includes(k)) bound.set(k, v);
      else if (dstarPos >= 0) extra.set(k, v);
      else throw R.typeErr(`函数「${code.name}」没有叫「${k}」的参数`, line, col);
    }
    if (dstarPos >= 0) bound.set(params[dstarPos], extra);
    if (defaults) {
      params.forEach((p, i) => {
        if (i < npos && !bound.has(p) && defaults[i] != null) bound.set(p, defaults[i]);
      });
    }
    const missing = params.slice(0, npos).filter(p => !bound.has(p));
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
              // 默认值只在「普通参数」尾部，*参数/**选项 不参与（M25）
              const sub = codes[ins.a];
              const subKinds = sub.paramKind || [];
              let subNpos = sub.params.length;
              for (let i = 0; i < subKinds.length; i++) {
                if (subKinds[i] !== 0) { subNpos = i; break; }
              }
              const pad = subNpos - ndefaults;
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
            const items = R.unpackValues(v, n, ins.b, ins.line, ins.col);
            for (let i = items.length - 1; i >= 0; i--) stack.push(items[i]);
          } else if (op === Op.BUILD_SLICE) {
            // 切片（M30）：按位标志从栈上取 start/stop/step 构造切片对象，
            // 交给后面的 GET_ITEM / SET_ITEM 消费（与 Python 侧一致）
            const flags = ins.a;
            const step = (flags & 4) ? stack.pop() : null;
            const stop = (flags & 2) ? stack.pop() : null;
            const start = (flags & 1) ? stack.pop() : null;
            stack.push(R.makeSlice(start, stop, step));
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
/** Python `format()` 的 half-even 舍入（`{:.1f}` 用它，不是四舍五入）。 */
function roundHalfEven(x, p) {
  const f = 10 ** p;
  const v = x * f;
  const t = Math.trunc(v);
  if (Math.abs(v - t) === 0.5) {
    return (t % 2 === 0 ? t : t + Math.sign(v)) / f;
  }
  return Math.round(v) / f;
}

/**
 * 把一个值按格式说明符渲染（`{}` 里 `:` 之后那一段）。
 * 支持 `{:.1f}`、`{:>8}`、`{:<8}`、`{:^8}`、`{:08.2f}`、`{:d}`、`{:,}`。
 * 认不出的说明符就退回普通显示——宁可不格式化，也不要给错。
 */
function fmtOne(v, fmt) {
  if (fmt === '') return R.pyStr(v);          // 无说明符 → 与「文本()」同一套显示
  const m = /^(?:(.)?([<>=^]))?([+\- ])?(#)?(0)?(\d+)?(,)?(?:\.(\d+))?([a-zA-Z%])?$/.exec(fmt);
  if (!m) return R.pyStr(v);
  const fill = m[1], align = m[2], sign = m[3], zero = m[5];
  const width = m[6] === undefined ? 0 : Number(m[6]);
  const comma = m[7];
  const prec = m[8] === undefined ? undefined : Number(m[8]);
  const type = m[9];
  const num = (typeof v === 'number') ? v
    : (R.isFloatBox(v) ? v.value : Number(v));

  let s;
  if (type === 'f' || type === 'F') {
    const q = prec === undefined ? 6 : prec;
    s = roundHalfEven(num, q).toFixed(q);
  } else if (type === 'd') {
    s = String(Math.trunc(num));
  } else if (type === 'e' || type === 'E') {
    s = num.toExponential(prec === undefined ? 6 : prec);
    if (type === 'E') s = s.toUpperCase();
  } else if (type === '%') {
    s = (num * 100).toFixed(prec === undefined ? 6 : prec) + '%';
  } else {
    s = R.pyStr(v);
    if (prec !== undefined) s = [...s].slice(0, prec).join('');
  }
  if (comma) {
    const parts = s.split('.');
    const neg = parts[0].startsWith('-');
    const body = neg ? parts[0].slice(1) : parts[0];
    s = (neg ? '-' : '') + body.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
      + (parts.length > 1 ? '.' + parts[1] : '');
  }
  if (sign === '+' && !s.startsWith('-') && !s.startsWith('+')) s = '+' + s;
  if (width > s.length) {
    const f = fill !== undefined ? fill : (zero ? '0' : ' ');
    const al = align !== undefined ? align : (zero ? '=' : '>');
    const total = width - s.length;
    if (al === '<') s = s + f.repeat(total);
    else if (al === '^') {
      s = f.repeat(Math.floor(total / 2)) + s
        + f.repeat(total - Math.floor(total / 2));
    } else if (al === '=') {
      const neg = s.startsWith('-') ? '-' : '';
      s = neg + f.repeat(total) + s.slice(neg.length);
    } else s = f.repeat(total) + s;
  }
  return s;
}

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
  } else if (R.isFile(obj)) {
    // 文件对象按行迭代（M26）：真句柄，大文件不必整个读进内存
    it = obj[Symbol.iterator]();
  } else if (obj instanceof R.JishiSet) {
    // 集合迭代自己的元素（M30）。JishiSet 的迭代器直接来自内部 Map，
    // 顺序就是插入顺序，与 Python 侧一致。
    it = obj[Symbol.iterator]();
  } else if (obj instanceof R.JishiInstance) {
    // 迭代协议（M24/M30）：自定义对象定义 `迭代(自身)` 就能用于遍历。
    // 与 Python 侧 `JishiInstance.__iter__` 同一套规则。
    const fn = obj.cls.methods.get('迭代');
    if (!fn) {
      throw R.typeErr(`「${R.display(obj)}」不能遍历，遍历需要列表、文本、集合、`
        + `字典或文件；自定义对象可以定义「迭代()」方法`, line, col);
    }
    const got = fn.vm.callFunction(fn, [obj], null, line, col);
    // 返回自身会无限循环——Python 侧也拦
    if (got === obj) {
      throw R.runErr('「迭代()」返回了对象自身，这样遍历会无限循环', line, col);
    }
    it = makeIterator(got, line, col);
  } else {
    throw R.typeErr(`「${R.display(obj)}」不能遍历，遍历需要列表、文本、集合、`
      + `字典或文件；自定义对象可以定义「迭代()」方法`, line, col);
  }
  return it;
}

// ---------------------------------------------------------------------------
// JS 内建标准库（子集：数学/文本/随机/表格）
// ---------------------------------------------------------------------------

// -- 系统（M30）--
// 与 Python 侧 `jishi/stdlib/系统.py` 对齐。**注意**：系统信息类的返回值
// 两个平台本来就不一样（`platform.processor()` vs `os.cpus()[0].model`），
// 那属于平台差异、不是语义差异；`执行` 在 Node 侧只返回标准输出
// （不合并标准错误——`execSync` 没有保序合并的能力）。
const _os = require('os');
const _cp = require('child_process');

const SYS_FUNCS = {
  '系统名': new R.Builtin('系统名', () => {
    const p = _os.platform();
    if (p === 'win32') return 'Windows';
    if (p === 'darwin') return 'Darwin';
    return 'Linux';
  }),
  '系统版本': new R.Builtin('系统版本', () => _os.release()),
  '机器架构': new R.Builtin('机器架构', () => {
    const a = _os.arch();
    if (a === 'x64') return 'AMD64';
    if (a === 'ia32') return 'x86';
    return a;
  }),
  '处理器': new R.Builtin('处理器', () => {
    const cpus = _os.cpus();
    return cpus.length > 0 ? cpus[0].model : '';
  }),
  '当前目录': new R.Builtin('当前目录', () => process.cwd()),
  '改目录': new R.Builtin('改目录', (p) => {
    const dir = String(p);
    let st = null;
    try { st = fs.statSync(dir); } catch (e) { st = null; }
    if (!st || !st.isDirectory()) throw R.valueErr(`「${dir}」不是目录`);
    process.chdir(dir);
    return process.cwd();
  }),
  '环境变量': new R.Builtin('环境变量', (name, dflt = '') => {
    const k = String(name);
    return process.env[k] !== undefined ? process.env[k] : dflt;
  }),
  '设环境变量': new R.Builtin('设环境变量', (name, value) => {
    process.env[String(name)] = String(value);
    return null;
  }),
  '执行': new R.Builtin('执行', (cmd, timeout = 10.0) => {
    const line = Array.isArray(cmd) ? cmd.map((x) => String(x)).join(' ')
                                    : String(cmd);
    try {
      return String(_cp.execSync(line, {
        timeout: Number(timeout) * 1000,
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'ignore'],
      }));
    } catch (e) {
      throw R.runErr(`执行命令失败：${e && e.message ? e.message : e}`);
    }
  }),
  '退出码': new R.Builtin('退出码', (cmd, timeout = 10.0) => {
    const line = Array.isArray(cmd) ? cmd.map((x) => String(x)).join(' ')
                                    : String(cmd);
    const r = _cp.spawnSync(line, {
      shell: true, timeout: Number(timeout) * 1000,
      stdio: ['ignore', 'ignore', 'ignore'],
    });
    return r.status === null ? -1 : r.status;
  }),
  '主机名': new R.Builtin('主机名', () => _os.hostname()),
  // 脚本收到的命令行参数（`node index.js 字节码.json 参数1 参数2`）：
  // argv[0]=node、argv[1]=index.js、argv[2]=字节码文件，所以从第 3 个起
  '参数': new R.Builtin('参数', () => process.argv.slice(3)),
};

// -- json（M30）--
// **不能直接用 `JSON.stringify`**：Python 的 `json.dumps` 默认分隔符是
// `', '` 和 `': '`，而 JS 的没有空格（`{"a":1}`）——不自己写就会与
// Python 侧输出不一致。
function jsonEncode(v, indent) {
  const compact = (indent === null || indent === undefined);
  const pad = (n) => ' '.repeat(n);
  const enc = (x, depth) => {
    if (x === null || x === undefined) return 'null';
    if (typeof x === 'boolean') return x ? 'true' : 'false';
    if (typeof x === 'number') return String(x);
    if (typeof x === 'string') return JSON.stringify(x);
    if (R.isDecimal(x)) return x.toString();
    // 有缩进时：元素前换行缩进一层、收尾换行回到本层（Python 的 indent 规则）；
    // 无缩进时：`', '` 与 `': '`——Python 的默认分隔符就是带空格的
    const nl = compact ? '' : '\n' + pad((depth + 1) * Number(indent));
    const cl = compact ? '' : '\n' + pad(depth * Number(indent));
    const isep = compact ? ', ' : ',' + nl;
    if (Array.isArray(x)) {
      if (x.length === 0) return '[]';
      return '[' + nl + x.map((e) => enc(e, depth + 1)).join(isep) + cl + ']';
    }
    if (x instanceof Map) {
      if (x.size === 0) return '{}';
      const items = [...x.entries()].map(
        ([k, val]) => JSON.stringify(String(k)) + ': ' + enc(val, depth + 1));
      return '{' + nl + items.join(isep) + cl + '}';
    }
    return JSON.stringify(String(x));
  };
  return enc(v, 0);
}

function jsonDecode(text, line, col) {
  let got;
  try {
    got = JSON.parse(String(text));
  } catch (e) {
    throw R.valueErr(`不是有效的 JSON 文本`
      + `（${e && e.message ? e.message : e}）`, line, col);
  }
  return jsonWrap(got);
}

/** JSON.parse 出来的普通对象 → 基石的字典（Map）。 */
function jsonWrap(v) {
  if (Array.isArray(v)) return v.map(jsonWrap);
  if (v !== null && typeof v === 'object') {
    const m = new Map();
    for (const [k, val] of Object.entries(v)) m.set(k, jsonWrap(val));
    return m;
  }
  return v;
}

const JSON_FUNCS = {
  '转文本': new R.Builtin('转文本', (obj, indent = null) =>
    jsonEncode(obj, indent === null || indent === undefined ? null : Number(indent))),
  '解析': new R.Builtin('解析', (text) => jsonDecode(text)),
  '读文件': new R.Builtin('读文件', (path) => {
    let text;
    try { text = fs.readFileSync(String(path), 'utf8'); } catch (e) {
      throw R.fileErr(`找不到文件或目录：${path}`);
    }
    return jsonDecode(text);
  }),
  '写文件': new R.Builtin('写文件', (path, obj, indent = null) => {
    try {
      fs.writeFileSync(String(path),
        jsonEncode(obj, indent === null || indent === undefined ? null : Number(indent)),
        'utf8');
    } catch (e) {
      throw R.fileErr(`没法写入文件「${path}」`);
    }
    return null;
  }),
};

// -- 正则（M30）--
// 直接映射到 JS 的 RegExp。**边界**：JS 与 Python 的正则语法有差异
// （命名分组、部分转义、以及 `查找全部` 在「有分组」时的返回形态），
// 这里按常见用法对齐：无分组时返回匹配串列表，有分组时返回分组列表。
function reOf(pattern, line, col) {
  try {
    return new RegExp(String(pattern));
  } catch (e) {
    throw R.valueErr(`正则表达式写错了：${e && e.message ? e.message : e}`, line, col);
  }
}

const RE_FUNCS = {
  '匹配': new R.Builtin('匹配', (p, text) => {
    const re = reOf(p);
    const anchored = new RegExp('^(?:' + re.source + ')');
    return anchored.test(String(text));
  }),
  '搜索': new R.Builtin('搜索', (p, text) => {
    const m = reOf(p).exec(String(text));
    return m ? m[0] : '';
  }),
  '查找全部': new R.Builtin('查找全部', (p, text) => {
    const re = reOf(p);
    const out = [];
    const all = new RegExp(re.source, re.flags.includes('g') ? re.flags : re.flags + 'g');
    let m;
    while ((m = all.exec(String(text))) !== null) {
      out.push(m.length > 2 ? m.slice(1) : (m.length === 2 ? m[1] : m[0]));
      if (m.index === all.lastIndex) all.lastIndex += 1;   // 防零宽匹配死循环
    }
    return out;
  }),
  '替换': new R.Builtin('替换', (p, to, text) => {
    const re = reOf(p);
    const all = new RegExp(re.source, re.flags.includes('g') ? re.flags : re.flags + 'g');
    return String(text).replace(all, String(to));
  }),
  '拆分': new R.Builtin('拆分', (p, text) => String(text).split(reOf(p))),
  '分组': new R.Builtin('分组', (p, text) => {
    const m = reOf(p).exec(String(text));
    return m ? m.slice(1) : [];
  }),
};


// 与 Python 侧 `jishi/stdlib/表格.py` 对齐：`读表格(路径)` 返回一个表格对象，
// 再用 `表格.表头(t)` / `表格.挑选(t, 列)` 这类函数操作它。
// 编码用 utf-8-sig（带 BOM），与 Python 侧一致——不然 Excel 打开会乱码。
// -- 表格（CSV）-----------------------------------------------------------
// 与 Python 侧 `jishi/stdlib/表格.py` 对齐：`读表格(路径)` 返回一个表格对象，
// 再用 `表格.表头(t)` / `表格.挑选(t, 列)` 这类函数操作它。
// 编码用 utf-8-sig（带 BOM），与 Python 侧一致——不然 Excel 打开会乱码。
const TABLE = Symbol('table');

function makeTable(headers, rows) {
  return {
    [TABLE]: true,
    headers: [...headers],
    rows: rows.map((r) => [...r]),
    // 与 Python 侧 `_Table.__repr__` 一致：打印表格对象不该是 [object Object]
    toString() {
      return `<表格 ${this.rows.length} 行 x ${this.headers.length} 列>`;
    },
  };
}

function asTable(v) {
  if (v !== null && typeof v === 'object' && v[TABLE] === true) return v;
  throw R.typeErr(`「${R.typeName(v)}」不是表格，先用 表格.读表格(路径) 读一个`);
}

function colIndex(t, name) {
  const n = String(name);
  const i = t.headers.indexOf(n);
  if (i < 0) {
    throw R.typeErr(`表格里没有「${n}」这一列`
      + `（现有的列：${t.headers.join('、')}）`);
  }
  return i;
}

/** 解析 CSV（RFC 4180 的常见子集：引号包裹、双引号转义、CRLF）。 */
function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = '';
  let inQ = false;
  let i = 0;
  const pushField = () => { row.push(field); field = ''; };
  while (i < text.length) {
    const c = text[i];
    if (inQ) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i += 2; continue; }
        inQ = false; i += 1; continue;
      }
      field += c; i += 1; continue;
    }
    if (c === '"') { inQ = true; i += 1; continue; }
    if (c === ',') { pushField(); i += 1; continue; }
    if (c === '\r') { i += 1; continue; }
    if (c === '\n') {
      if (field !== '' || row.length > 0) pushField();
      rows.push(row);
      row = [];
      i += 1; continue;
    }
    field += c; i += 1;
  }
  if (field !== '' || row.length > 0) { pushField(); rows.push(row); }
  return rows;
}

/** 一个字段写成 CSV 的形式（必要时加引号，与 Python csv.QUOTE_MINIMAL 一致）。 */
function csvField(v) {
  const s = (v === null || v === undefined) ? '' : String(v);
  if (/[",\n\r]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
  if (s === '' && arguments.length === 1) return '""';   // 单字段空行要写成 ""
  return s;
}

function csvRow(row) {
  if (row.length === 1) {
    const s = (row[0] === null || row[0] === undefined) ? '' : String(row[0]);
    if (s === '') return '""';                 // 单字段空行：写成 "" 免得不成为空行
    return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }
  return row.map((v) => csvField(v)).join(',');
}

function toCsvText(rows) {
  return rows.map(csvRow).join('\r\n') + '\r\n';
}

function readCsvFile(path) {
  let text;
  try {
    text = fs.readFileSync(path, 'utf8');
  } catch (e) {
    if (e && e.code === 'ENOENT') throw R.fileErr(`找不到文件「${path}」`);
    throw R.fileErr(`文件「${path}」读不出来`);
  }
  if (text.charCodeAt(0) === 0xFEFF) text = text.slice(1);   // 去掉 BOM
  return text;
}

function writeCsvFile(path, rows) {
  try {
    fs.writeFileSync(path, '\uFEFF' + toCsvText(rows), 'utf8');
  } catch (e) {
    throw R.fileErr(`没法写入文件「${path}」`);
  }
}

/** 数值感知排序键：能转数字按数字排，否则按文本排（同 Python 侧 `_sort_key`）。 */
function tableSortKey(row, i) {
  const v = row[i];
  const s = (v === null || v === undefined) ? '' : String(v);
  if (s.trim() !== '') {
    const n = Number(s);
    if (!Number.isNaN(n)) return [0, n];
  }
  return [1, s];
}

function tableCmp(a, b, i) {
  const ka = tableSortKey(a, i), kb = tableSortKey(b, i);
  if (ka[0] !== kb[0]) return ka[0] - kb[0];
  if (ka[0] === 0) return ka[1] - kb[1];
  return ka[1] < kb[1] ? -1 : (ka[1] > kb[1] ? 1 : 0);
}

const TABLE_FUNCS = {
  '读表格': new R.Builtin('读表格', (path) => {
    if (path === null || path === undefined) throw R.typeErr('需要提供文件路径');
    const rows = parseCsv(readCsvFile(String(path)));
    if (rows.length === 0) throw R.fileErr(`文件「${path}」是空的，没有表头`);
    return makeTable(rows[0], rows.slice(1));
  }),
  '写表格': new R.Builtin('写表格', (path, 行列表) => {
    if (path === null || path === undefined) throw R.typeErr('需要提供文件路径');
    const rows = R.asIterable(行列表, '要写的内容');
    writeCsvFile(String(path), rows.map((r) => R.asIterable(r, '表格的一行')));
    return null;
  }),
  '写字典表格': new R.Builtin('写字典表格', (path, 字典列表) => {
    if (path === null || path === undefined) throw R.typeErr('需要提供文件路径');
    const dicts = R.asIterable(字典列表, '要写的内容');
    if (dicts.length === 0) throw R.typeErr('「写字典表格」需要一个非空的字典列表');
    if (!(dicts[0] instanceof Map)) {
      throw R.typeErr(`列表里的元素应该是字典，但出现了「${R.display(dicts[0])}」`);
    }
    const headers = [...dicts[0].keys()];
    const rows = [headers];
    for (const d of dicts) {
      if (!(d instanceof Map)) {
        throw R.typeErr(`列表里的元素应该是字典，但出现了「${R.display(d)}」`);
      }
      rows.push(headers.map((h) => (d.has(h) ? d.get(h) : '')));
    }
    writeCsvFile(String(path), rows);
    return null;
  }),
  '读字典表格': new R.Builtin('读字典表格', (path) => {
    if (path === null || path === undefined) throw R.typeErr('需要提供文件路径');
    const rows = parseCsv(readCsvFile(String(path)));
    if (rows.length === 0) return [];
    const headers = rows[0];
    return rows.slice(1).map((r) => {
      const d = new Map();
      headers.forEach((h, i) => d.set(h, r[i] === undefined ? '' : r[i]));
      return d;
    });
  }),
  '追加': new R.Builtin('追加', (path, 行) => {
    if (path === null || path === undefined) throw R.typeErr('需要提供文件路径');
    let text = '';
    try { text = fs.readFileSync(String(path), 'utf8'); } catch (e) { text = ''; }
    let body = text;
    if (body.charCodeAt(0) === 0xFEFF) body = body.slice(1);
    // 末尾没有换行时先补一个，免得新行接到旧行屁股上
    if (body.length > 0 && !body.endsWith('\n')) body += '\r\n';
    const line = csvRow(R.asIterable(行, '要追加的一行'));
    try {
      fs.writeFileSync(String(path), '\uFEFF' + body + line + '\r\n', 'utf8');
    } catch (e) {
      throw R.fileErr(`没法写入文件「${path}」`);
    }
    return null;
  }),
  '表头': new R.Builtin('表头', (t) => [...asTable(t).headers]),
  '数据行': new R.Builtin('数据行', (t) => asTable(t).rows.map((r) => [...r])),
  '挑选': new R.Builtin('挑选', (t, 列) => {
    const tab = asTable(t);
    if (Array.isArray(列)) {
      const idx = 列.map((c) => colIndex(tab, c));
      return tab.rows.map((row) => idx.map((i) => row[i]));
    }
    const i = colIndex(tab, 列);
    return tab.rows.map((row) => row[i]);
  }),
  '筛选': new R.Builtin('筛选', (t, 列名, 值) => {
    const tab = asTable(t);
    const i = colIndex(tab, 列名);
    return makeTable(tab.headers, tab.rows.filter((row) => R.valueEq(row[i], 值)));
  }),
  '排序按': new R.Builtin('排序按', (t, 列名, 倒序 = false) => {
    const tab = asTable(t);
    const i = colIndex(tab, 列名);
    const rows = tab.rows.map((r) => [...r]);
    // 用「取负的比较器」实现倒序（而不是排完再 reverse）：
    // 这样相等的行保持原顺序，与 Python 的 `sorted(reverse=True)` 一致
    const dir = R.truthy(倒序) ? -1 : 1;
    rows.sort((a, b) => dir * tableCmp(a, b, i));
    return makeTable(tab.headers, rows);
  }),
  '汇总': new R.Builtin('汇总', (t, 列名) => {
    const tab = asTable(t);
    const i = colIndex(tab, 列名);
    const vals = [];
    for (const row of tab.rows) {
      const n = Number(row[i]);
      if (row[i] === '' || row[i] === undefined || Number.isNaN(n)) {
        throw R.typeErr(`列「${列名}」里有不是数字的值「${row[i]}」，没法汇总`);
      }
      vals.push(n);
    }
    if (vals.length === 0) throw R.typeErr(`表格没有数据行，没法汇总「${列名}」`);
    const total = vals.reduce((a, b) => a + b, 0);
    // 用 FloatBox 包住：Python 侧 `float` 求和给的是小数，
    // 打印成 98.0 而不是 98（成绩分析项目的输出就靠这个）
    return new Map([
      ['个数', vals.length],
      ['总和', new R.FloatBox(total)],
      ['平均', new R.FloatBox(total / vals.length)],
      ['最大', new R.FloatBox(Math.max(...vals))],
      ['最小', new R.FloatBox(Math.min(...vals))],
    ]);
  }),
  '转置': new R.Builtin('转置', (行列表) => {
    const rows = R.asIterable(行列表, '要转置的内容');
    if (rows.length === 0) return [];
    const len = Math.max(...rows.map((r) => R.asIterable(r, '一行的内容').length));
    const out = [];
    for (let c = 0; c < len; c++) {
      out.push(rows.map((r) => R.asIterable(r, '一行的内容')[c] ?? ''));
    }
    return out;
  }),
};

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
    '拼接': new R.Builtin('拼接', (seq, sep = '') =>
      R.asIterable(seq, '要拼接的内容').map((x) => R.pyStr(x)).join(String(sep))),
    // 居中/左右对齐：Python 的 center/ljust/rjust 在**填不满**时原样返回，
    // JS 的 padStart 走另一套（字符数按 code unit 算），所以自己按码点算
    '居中': new R.Builtin('居中', (s, width, fill = ' ') => {
      const text = String(s); const w = Number(width);
      const len = [...text].length; const f = String(fill) || ' ';
      if (len >= w) return text;
      const total = w - len;
      const left = Math.floor(total / 2);
      return f.repeat(left) + text + f.repeat(total - left);
    }),
    '左对齐': new R.Builtin('左对齐', (s, width, fill = ' ') => {
      const text = String(s); const w = Number(width);
      const len = [...text].length; const f = String(fill) || ' ';
      return len >= w ? text : text + f.repeat(w - len);
    }),
    '右对齐': new R.Builtin('右对齐', (s, width, fill = ' ') => {
      const text = String(s); const w = Number(width);
      const len = [...text].length; const f = String(fill) || ' ';
      return len >= w ? text : f.repeat(w - len) + text;
    }),
    '补零': new R.Builtin('补零', (s, width) => {
      const text = String(s); const w = Number(width);
      if ([...text].length >= w) return text;
      const neg = text.startsWith('-');
      const body = neg ? text.slice(1) : text;
      const pad = '0'.repeat(w - [...text].length);
      return (neg ? '-' : '') + pad + body;
    }),
    '重复': new R.Builtin('重复', (s, n) => String(s).repeat(Math.max(0, Number(n)))),
    '计数': new R.Builtin('计数', (s, sub) => {
      const text = String(s); const needle = String(sub);
      return needle === '' ? [...text].length + 1 : text.split(needle).length - 1;
    }),
    '是数字': new R.Builtin('是数字', (s) => /^[0-9]+$/.test(String(s))),
    '是字母': new R.Builtin('是字母', (s) => /^\p{L}+$/u.test(String(s))),
    '是空白': new R.Builtin('是空白', (s) => /^\s+$/.test(String(s))),
    '是大写': new R.Builtin('是大写', (s) => {
      const t = String(s);
      return /\p{Lu}/u.test(t) && t === t.toUpperCase();
    }),
    '是小写': new R.Builtin('是小写', (s) => {
      const t = String(s);
      return /\p{Ll}/u.test(t) && t === t.toLowerCase();
    }),
    '首字母大写': new R.Builtin('首字母大写', (s) => {
      const t = String(s);
      return t === '' ? t : [...t][0].toUpperCase() + [...t].slice(1).join('').toLowerCase();
    }),
    '去前缀': new R.Builtin('去前缀', (s, p) => {
      const t = String(s); const pre = String(p);
      return pre !== '' && t.startsWith(pre) ? t.slice(pre.length) : t;
    }),
    '去后缀': new R.Builtin('去后缀', (s, p) => {
      const t = String(s); const suf = String(p);
      return suf !== '' && t.endsWith(suf) ? t.slice(0, t.length - suf.length) : t;
    }),
    // `{}` 占位符依次替换，并支持常见的格式说明符：
    //   {:.1f}    保留一位小数（Python 用 half-even 舍入）
    //   {:>8} {:<8} {:^8}  右/左/居中对齐
    //   {:08.2f}  补零      {:d} 整数      {:,} 千分位
    '格式化': new R.Builtin('格式化', (tpl, ...rest) => {
      let auto = 0;
      return String(tpl).replace(/\{([^{}]*)\}/g, (_m, spec) => {
        let idxPart = spec;
        let fmt = '';
        const colon = spec.indexOf(':');
        if (colon >= 0) { idxPart = spec.slice(0, colon); fmt = spec.slice(colon + 1); }
        const arg = idxPart === '' ? rest[auto++] : rest[Number(idxPart)];
        return fmtOne(arg, fmt);
      });
    }),
    '切成三段': new R.Builtin('切成三段', (s, sep) => {
      const t = String(s); const d = String(sep);
      const at = d === '' ? -1 : t.indexOf(d);
      if (at < 0) return [t, '', ''];
      return [t.slice(0, at), d, t.slice(at + d.length)];
    }),
    '按行拆分': new R.Builtin('按行拆分', (s) =>
      String(s).split(/\r\n|\r|\n/).filter((x, _i, arr) =>
        !(x === '' && arr.length > 1 && arr[arr.length - 1] === ''))),
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
  '表格': new R.Module('表格', TABLE_FUNCS),
  '系统': new R.Module('系统', SYS_FUNCS),
  'json': new R.Module('json', JSON_FUNCS),
  '正则': new R.Module('正则', RE_FUNCS),
};

function gcd(a, b) { while (b) { [a, b] = [b, a % b]; } return a; }

module.exports = { VM, Op };
