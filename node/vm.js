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
      // 内建 / 标准库是**按位置**调用的 JS 函数：命名参数在这里映射回位置
      // 参数（M51）。以前是 `fn.fn(...args)` 直接把 kwargs 丢掉 ——
      // `加密.摘要("abc", 算法="md5")` 会悄悄用默认的 sha256。
      return fn.fn(...R.mapKwargs(fn.name, args, kwargs, line, col));
    }
    if (fn instanceof R.Module) {
      throw R.typeErr(`模块「${fn.name}」不能直接调用`, line, col);
    }
    if (fn instanceof R.JishiClass) {
      this._rejectKwargs(`类「${fn.name}」`, kwargs, line, col);
      return R.makeInstance(fn, args, line, col);
    }
    if (fn instanceof R.BoundMethod) {
      this._rejectKwargs(`「${R.display(fn.obj)}」的方法`, kwargs, line, col);
      return fn.impl(fn.obj, args, line, col);
    }
    if (fn instanceof R.BoundUserMethod) {
      return this.callFunction(fn.fn, [fn.instance, ...args], kwargs, line, col);
    }
    if (fn instanceof R.ExcType) {
      this._rejectKwargs(`异常类型「${fn.name}」`, kwargs, line, col);
      return fn.make(args[0] !== undefined ? args[0] : '', line, col);
    }
    throw R.runErr(`「${R.display(fn)}」不能调用`, line, col);
  }

  /** 宿主暂不支持命名参数时**明确报错**（M51）。以前 kwargs 会被静默丢掉，
   *  调用「成功了」但参数根本没生效 —— 那比报错难查一百倍。 */
  _rejectKwargs(target, kwargs, line, col) {
    const keys = kwargs ? Object.keys(kwargs) : [];
    if (keys.length === 0) return;
    throw R.typeErr(
      `${target}暂不支持命名参数（${keys.join('、')}）—— 请改用位置参数`,
      line, col);
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
    // 大整数按数字字面量写（`String(1n)` 就是 `"1"`，不带那个 `n`）（M32）
    if (typeof x === 'bigint') return x.toString();
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
    // `类型()` 用它（见 runtime 的 typeName 钩子）——不然会给 `Object`
    __jishiType: '表格',
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

// ---------------------------------------------------------------------------
// 路径 / 文件 / 日期 / 时间 / 加密 / 压缩 / 网络（M31）
// ---------------------------------------------------------------------------
// 与 Python 侧 `jishi/stdlib/` 下的同名模块一一对齐 —— M31 把 JS 宿主从
// 「5 个常用模块」补到与 Python 侧同宽。写这段守三条：
//   ① 「值 → 文本」统一走 `pText`（布尔/空给中文，与 `文本()` 同一套显示）；
//   ② 平台差异（路径分隔符、系统信息）**不算语义差异**，但同机跑出来的
//      结果仍要与 Python 侧逐字节相同（由对拍测试锁住）；
//   ③ 动态值（现在 / 时间戳）没法逐字节对拍，测试改断言格式与范围。

const _path = require('path');
const _crypto = require('crypto');
const _zlib = require('zlib');

/** 值 → 文本（路径、文件内容、摘要输入等参数用）。对齐 `jishi_repr(…, top=True)`。 */
function pText(v) {
  if (typeof v === 'string') return v;
  return R.pyStr(v);
}

/** 按 Unicode 码点比较（Python 的 `sorted()` 就是这个序，JS 默认是 UTF-16 码元序）。 */
function cmpCodepoint(a, b) {
  const A = [...a]; const B = [...b];
  const n = Math.min(A.length, B.length);
  for (let i = 0; i < n; i++) {
    const x = A[i].codePointAt(0); const y = B[i].codePointAt(0);
    if (x !== y) return x - y;
  }
  return A.length - B.length;
}

function statOf(p) {
  try { return fs.statSync(pText(p)); } catch (e) { return null; }
}

/**
 * 后缀 —— 对齐 pathlib 的规则，**不能用 `path.extname`**：
 * pathlib 对 `a.` 与 `.bashrc` 都给空，而 `path.extname('a.')` 给 `'.'`。
 * pathlib 的真规则是「最后一个点既不在开头、也不在末尾」。
 */
function suffixOf(p) {
  const base = _path.basename(pText(p));
  const i = base.lastIndexOf('.');
  return (i > 0 && i < base.length - 1) ? base.slice(i) : '';
}

/** Python 的 `str.splitlines()` 语义（含 \v \f \x1c-\x1e \x85 \u2028 \u2029）。 */
function splitLines(text) {
  if (text === '') return [];
  const parts = text.split(/\r\n|[\n\r\v\f\x1c-\x1e\u0085\u2028\u2029]/);
  if (parts.length > 0 && parts[parts.length - 1] === '') parts.pop();
  return parts;
}

/**
 * Windows 上把 `/` 规范成 `\`。
 *
 * Python 的 `Path.__str__` 在 Windows 上**总是**吐反斜杠（`str(Path("a/b"))`
 * 给 `a\b`），而 JS 的 `path` 会保留输入里原有的分隔符。不对齐的话
 * `路径.父目录("a/b/c.txt")` 一边给 `a\b`、一边给 `a/b`。
 */
function normSep(p) {
  return _path.sep === '\\' ? String(p).replace(/\//g, '\\') : String(p);
}

// -- 路径（M31）--
// 薄封装平台的 `path` + `fs`，语义对齐 Python 的 pathlib。
// 平台差异说明：`连接` 的结果在 Windows 上是 `a\b`（pathlib 与 `path` 一样），
// 这是**平台一致**而不是语义偏差。
const PATH_FUNCS = {
  '连接': new R.Builtin('连接', (...parts) => {
    if (parts.length === 0) return '.';
    let acc = pText(parts[0]);
    for (let i = 1; i < parts.length; i++) {
      const seg = pText(parts[i]);
      // pathlib 的 `/` 运算符：右段是绝对路径时**重置**，不是拼接
      if (_path.isAbsolute(seg)) acc = seg;
      else acc = acc === '' ? seg : _path.join(acc, seg);
    }
    return normSep(acc);
  }),
  '存在': new R.Builtin('存在', (p) => fs.existsSync(pText(p))),
  '是文件': new R.Builtin('是文件', (p) => {
    const st = statOf(p); return !!st && st.isFile();
  }),
  '是目录': new R.Builtin('是目录', (p) => {
    const st = statOf(p); return !!st && st.isDirectory();
  }),
  '绝对路径': new R.Builtin('绝对路径', (p) => _path.resolve(pText(p))),
  '父目录': new R.Builtin('父目录', (p) => normSep(_path.dirname(pText(p)))),
  '文件名': new R.Builtin('文件名', (p) => _path.basename(pText(p))),
  '后缀': new R.Builtin('后缀', (p) => suffixOf(p)),
  '无后缀名': new R.Builtin('无后缀名', (p) => {
    const base = _path.basename(pText(p));
    const suf = suffixOf(p);
    return suf === '' ? base : base.slice(0, base.length - suf.length);
  }),
  '当前目录': new R.Builtin('当前目录', () => process.cwd()),
  '主目录': new R.Builtin('主目录', () => require('os').homedir()),
  '创建目录': new R.Builtin('创建目录', (p, recursive = false) => {
    const dir = pText(p);
    try {
      fs.mkdirSync(dir, { recursive: recursive === true });
    } catch (e) {
      // 已存在不算错（对齐 Python 的 `exist_ok=True`）
      if (!e || e.code !== 'EEXIST') {
        throw R.fileErr(`创建目录「${dir}」失败：${e && e.message ? e.message : e}`);
      }
    }
    return normSep(dir);
  }),
  '删除': new R.Builtin('删除', (p) => {
    const target = pText(p);
    const st = statOf(target);
    if (!st) return false;                       // 不存在给假（对齐 Python）
    if (st.isFile()) { fs.unlinkSync(target); return true; }
    fs.rmdirSync(target);                        // 只删空目录（Path.rmdir 语义）
    return true;
  }),
  '列出': new R.Builtin('列出', (p) => {
    const dir = pText(p);
    const st = statOf(dir);
    if (!st || !st.isDirectory()) throw R.valueErr(`「${dir}」不是目录`);
    return fs.readdirSync(dir).sort(cmpCodepoint);
  }),
  '递归列出': new R.Builtin('递归列出', (p, suffix = '') => {
    // 与 Python 侧 路径.py 的 `递归列出` 对齐：返回**相对路径**、已排序、
    // 只列文件（目录本身不算）。后缀判定走 `suffixOf`（对齐 pathlib 规则，
    // 不能用 path.extname——见上面 `后缀` 的注释）。
    const dir = pText(p);
    const st = statOf(dir);
    if (!st || !st.isDirectory()) throw R.valueErr(`「${dir}」不是目录`);
    const want = String(suffix);
    const out = [];
    const walk = (base, rel) => {
      for (const name of fs.readdirSync(base).sort(cmpCodepoint)) {
        const full = _path.join(base, name);
        const st2 = statOf(full);
        if (st2 && st2.isDirectory()) { walk(full, _path.join(rel, name)); continue; }
        if (want && suffixOf(name) !== want) continue;
        out.push(_path.join(rel, name));
      }
    };
    walk(dir, '');
    out.sort(cmpCodepoint);
    return out;
  }),
  '重命名': new R.Builtin('重命名', (from, to) => {
    const src = pText(from); const dst = pText(to);
    if (!fs.existsSync(src)) throw R.valueErr(`「${src}」不存在`);
    fs.renameSync(src, dst);
    return dst;
  }),
  '大小': new R.Builtin('大小', (p) => {
    const target = pText(p);
    const st = statOf(target);
    if (!st || !st.isFile()) throw R.valueErr(`「${target}」不是文件`);
    return st.size;
  }),
  '修改时间': new R.Builtin('修改时间', (p) => {
    const target = pText(p);
    const st = statOf(target);
    if (!st) throw R.valueErr(`「${target}」不存在`);
    return strftimeLocal(new Date(st.mtimeMs), '%Y-%m-%d %H:%M:%S');
  }),
};

// -- 文件（M31）--
// 薄封装 `fs`，对齐 Python 侧 `jishi/stdlib/文件.py`。
// `路径拼接` 走 **posix**（Python 侧用的是 PurePosixPath，永远给 `/`），
// 而 `文件名`/`扩展名` 会把 `\` 也当分隔符（Python 侧先把 `\` 换成 `/`）。
const FILE_FUNCS = {
  '写文本': new R.Builtin('写文本', (p, content) => {
    fs.writeFileSync(pText(p), pText(content), 'utf8');
    return null;
  }),
  '读文本': new R.Builtin('读文本', (p) => {
    const target = pText(p);
    const st = statOf(target);
    if (!st || !st.isFile()) throw R.fileErr(`找不到文件「${target}」，没法读取`);
    return fs.readFileSync(target, 'utf8');
  }),
  '追加文本': new R.Builtin('追加文本', (p, content) => {
    fs.appendFileSync(pText(p), pText(content), 'utf8');
    return null;
  }),
  '写行': new R.Builtin('写行', (p, lines) => {
    const arr = R.asIterable(lines, '要写入的行');
    fs.writeFileSync(pText(p), arr.map((x) => pText(x) + '\n').join(''), 'utf8');
    return null;
  }),
  '按行读': new R.Builtin('按行读', (p) => splitLines(FILE_FUNCS['读文本'].fn(p))),
  '文件存在': new R.Builtin('文件存在', (p) => fs.existsSync(pText(p))),
  '是文件': new R.Builtin('是文件', (p) => {
    const st = statOf(p); return !!st && st.isFile();
  }),
  '是目录': new R.Builtin('是目录', (p) => {
    const st = statOf(p); return !!st && st.isDirectory();
  }),
  '列出目录': new R.Builtin('列出目录', (p = '.') => {
    const dir = pText(p);
    const st = statOf(dir);
    if (!st || !st.isDirectory()) {
      throw R.fileErr(`目录「${dir}」不存在，没法列出内容`);
    }
    return fs.readdirSync(dir).sort(cmpCodepoint);
  }),
  '创建目录': new R.Builtin('创建目录', (p) => {
    const dir = pText(p);
    try { fs.mkdirSync(dir, { recursive: true }); } catch (e) {
      throw R.fileErr(`创建目录「${dir}」失败：${e && e.message ? e.message : e}`);
    }
    return null;
  }),
  '删除文件': new R.Builtin('删除文件', (p) => {
    const target = pText(p);
    const st = statOf(target);
    if (!st) throw R.fileErr(`找不到文件「${target}」，没法删除`);
    if (st.isDirectory()) {
      throw R.fileErr(`「${target}」是目录，请先清空再删里面的文件；`
        + '删目录的功能暂未提供');
    }
    fs.unlinkSync(target);
    return null;
  }),
  '复制': new R.Builtin('复制', (from, to) => {
    const src = pText(from); const dst = pText(to);
    try { fs.copyFileSync(src, dst); } catch (e) {
      throw R.fileErr(`无法把「${src}」复制到「${dst}」`);
    }
    return null;
  }),
  '文件大小': new R.Builtin('文件大小', (p) => {
    const target = pText(p);
    const st = statOf(target);
    if (!st || !st.isFile()) throw R.fileErr(`找不到文件「${target}」，没法获取大小`);
    return st.size;
  }),
  '路径拼接': new R.Builtin('路径拼接', (...parts) => {
    if (parts.length === 0) return '.';
    let acc = pText(parts[0]);
    for (let i = 1; i < parts.length; i++) {
      const seg = pText(parts[i]);
      if (seg.startsWith('/')) acc = seg;         // PurePosixPath 遇绝对段重置
      else acc = acc === '' ? seg : _path.posix.join(acc, seg);
    }
    return acc;
  }),
  '文件名': new R.Builtin('文件名', (p) =>
    _path.posix.basename(pText(p).replace(/\\/g, '/'))),
  '扩展名': new R.Builtin('扩展名', (p) => suffixOf(pText(p).replace(/\\/g, '/'))),
};

// -- 日期 / 时间的公用工具（M31）--
// 基石里日期用「年-月-日」文本表示。这里实现 `strftime` 的一个子集：
// Python 默认 C locale 下 `%a`/`%A`/`%b`/`%B` 给英文，所以照英文写。
// （JS 没有等价的格式化 API——`toLocaleString` 依赖系统 locale。）
const _WD_CN = ['一', '二', '三', '四', '五', '六', '日'];          // 0=周一
const _WD_ABBR = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const _WD_FULL = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday',
                  'Saturday', 'Sunday'];
const _MO_ABBR = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug',
                  'Sep', 'Oct', 'Nov', 'Dec'];
const _MO_FULL = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
                  'August', 'September', 'October', 'November', 'December'];

const pad2 = (n) => String(n).padStart(2, '0');
const pad4 = (n) => String(n).padStart(4, '0');

/** 本地时间字段（取本地时区，与 Python 的 `datetime.now()` 一致）。 */
function localParts(d) {
  const y = d.getFullYear(); const mo = d.getMonth(); const da = d.getDate();
  return {
    year: y, month: mo + 1, day: da,
    hour: d.getHours(), minute: d.getMinutes(), second: d.getSeconds(),
    weekday: (d.getDay() + 6) % 7,   // 0=周一，对齐 Python 的 weekday()
    // 一年中的第几天（**从 0 起**，算 %U/%W 用；%j 要 +1）
    yday: Math.floor((Date.UTC(y, mo, da) - Date.UTC(y, 0, 1)) / 86400000),
  };
}

/**
 * `strftime` 子集。
 *
 * 支持 `%Y %y %m %d %H %I %M %S %j %w %u %a %A %b %B %p %U %W %c %x %X %%`
 * 与「去零」修饰 `%-d`。认不出的指令原样返回——宁可不格式化，也不给错。
 */
function strftimeLocal(d, fmt) {
  const p = localParts(d);
  const jsDow = d.getDay();                    // 0=周日（%w 用）
  const P = (n, w, noPad) => (noPad ? String(n) : String(n).padStart(w, '0'));
  return String(fmt).replace(/%([-_0]?)([A-Za-z%])/g, (whole, flag, c) => {
    const noPad = flag === '-';
    switch (c) {
      case 'Y': return String(p.year);
      case 'y': return P(p.year % 100, 2, noPad);
      case 'm': return P(p.month, 2, noPad);
      case 'd': return P(p.day, 2, noPad);
      case 'H': return P(p.hour, 2, noPad);
      case 'I': return P(((p.hour + 11) % 12) + 1, 2, noPad);
      case 'M': return P(p.minute, 2, noPad);
      case 'S': return P(p.second, 2, noPad);
      case 'j': return P(p.yday + 1, 3, false);
      case 'w': return String(jsDow);
      case 'u': return String(p.weekday + 1);
      case 'a': return _WD_ABBR[p.weekday];
      case 'A': return _WD_FULL[p.weekday];
      case 'b':
      case 'h': return _MO_ABBR[p.month - 1];
      case 'B': return _MO_FULL[p.month - 1];
      case 'p': return p.hour < 12 ? 'AM' : 'PM';
      // CPython 的公式：%U 以周日为一周之始、%W 以周一为始；
      // 新年第一个周几之前的日期都算第 0 周
      case 'U':
        return P(Math.floor((p.yday + 7 - ((p.weekday + 1) % 7)) / 7), 2, false);
      case 'W':
        return P(Math.floor((p.yday + 7 - p.weekday) / 7), 2, false);
      case 'c':
        return `${_WD_ABBR[p.weekday]} ${_MO_ABBR[p.month - 1]} `
          + `${String(p.day).padStart(2)} ${P(p.hour, 2, false)}`
          + `:${P(p.minute, 2, false)}:${P(p.second, 2, false)} ${p.year}`;
      case 'x':
        return `${P(p.month, 2, false)}/${P(p.day, 2, false)}`
          + `/${P(p.year % 100, 2, false)}`;
      case 'X':
        return `${P(p.hour, 2, false)}:${P(p.minute, 2, false)}`
          + `:${P(p.second, 2, false)}`;
      case '%': return '%';
      default: return whole;
    }
  });
}

/** 解析「2026-09-01」/「2026/09/01」；格式不对或日期不存在都报中文错（对齐 Python）。 */
function parseDate(text) {
  const s = pText(text).trim();
  const m = /^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$/.exec(s);
  if (m) {
    const y = Number(m[1]); const mo = Number(m[2]); const da = Number(m[3]);
    const d = new Date(0);
    d.setFullYear(y, mo - 1, da);
    d.setHours(0, 0, 0, 0);
    // Python 的 strptime 会拒绝「2026-02-30」这类不存在的日期，这里也要拒
    if (d.getFullYear() === y && d.getMonth() === mo - 1 && d.getDate() === da) {
      return d;
    }
  }
  throw R.valueErr(`「${text}」不是有效的日期，请写成 2026-09-01 这样的格式`);
}

/** `日期.解析` 的返回值。显示与 Python 的 datetime 一致（`2026-09-01 00:00:00`）。 */
class JishiDate {
  constructor(y, m, d) { this.y = y; this.m = m; this.d = d; this.__jishiType = 'datetime'; }

  toString() { return `${pad4(this.y)}-${pad2(this.m)}-${pad2(this.d)} 00:00:00`; }
}

function daysInYear(y) {
  return ((y % 4 === 0 && y % 100 !== 0) || y % 400 === 0) ? 366 : 365;
}

// -- 日期（M31）--
const DATE_FUNCS = {
  '今天': new R.Builtin('今天', () => strftimeLocal(new Date(), '%Y-%m-%d')),
  '解析': new R.Builtin('解析', (text) => {
    const d = parseDate(text);
    return new JishiDate(d.getFullYear(), d.getMonth() + 1, d.getDate());
  }),
  '格式化': new R.Builtin('格式化', (text, fmt = '%Y-%m-%d') =>
    strftimeLocal(parseDate(text), fmt)),
  '加天数': new R.Builtin('加天数', (n, text) => {
    const d = parseDate(text);
    d.setDate(d.getDate() + Math.trunc(Number(n)));
    return strftimeLocal(d, '%Y-%m-%d');
  }),
  '减天数': new R.Builtin('减天数', (n, text) => {
    const d = parseDate(text);
    d.setDate(d.getDate() - Math.trunc(Number(n)));
    return strftimeLocal(d, '%Y-%m-%d');
  }),
  '相差天数': new R.Builtin('相差天数', (a, b) =>
    Math.round((parseDate(a) - parseDate(b)) / 86400000)),
  '早于': new R.Builtin('早于', (a, b) => parseDate(a) < parseDate(b)),
  '晚于': new R.Builtin('晚于', (a, b) => parseDate(a) > parseDate(b)),
  '相等': new R.Builtin('相等', (a, b) => parseDate(a).getTime() === parseDate(b).getTime()),
  '星期名': new R.Builtin('星期名', (text) =>
    _WD_CN[localParts(parseDate(text)).weekday]),
  '今年': new R.Builtin('今年', () => new Date().getFullYear()),
  '本月': new R.Builtin('本月', () => new Date().getMonth() + 1),
  '本年天数': new R.Builtin('本年天数', () => daysInYear(new Date().getFullYear())),
};

// -- 时间（M31）--
/**
 * 同步睡眠。
 *
 * JS 主线程没有 `sleep`，用 `Atomics.wait` 阻塞——这是**唯一**不引入
 * 异步语义的办法（基石没有 `await`，标准库不能变成异步函数）。
 * 浏览器里主线程禁用 `Atomics.wait`，但 Node 里可用。
 */
function sleepSync(seconds) {
  const ms = Math.max(0, Number(seconds) * 1000);
  if (ms <= 0) return;
  const sab = new SharedArrayBuffer(4);
  Atomics.wait(new Int32Array(sab), 0, 0, ms);
}

const TIME_FUNCS = {
  '现在': new R.Builtin('现在', () => strftimeLocal(new Date(), '%Y-%m-%d %H:%M:%S')),
  '今天': new R.Builtin('今天', () => strftimeLocal(new Date(), '%Y-%m-%d')),
  '此刻': new R.Builtin('此刻', () => {
    const p = localParts(new Date());
    const m = new Map();
    m.set('年', p.year); m.set('月', p.month); m.set('日', p.day);
    m.set('时', p.hour); m.set('分', p.minute); m.set('秒', p.second);
    m.set('星期', _WD_CN[p.weekday]);
    return m;
  }),
  '时间戳': new R.Builtin('时间戳', () => new R.FloatBox(Date.now() / 1000)),
  '格式化时间戳': new R.Builtin('格式化时间戳', (ts) =>
    strftimeLocal(new Date(Number(ts) * 1000), '%Y-%m-%d %H:%M:%S')),
  '睡眠': new R.Builtin('睡眠', (seconds) => { sleepSync(seconds); return null; }),
  '高精度时间': new R.Builtin('高精度时间', () =>
    new R.FloatBox(Number(process.hrtime.bigint()) / 1e9)),
  '星期名': DATE_FUNCS['星期名'],
  '加天数': DATE_FUNCS['加天数'],
};

// -- 加密（M31）--
// 薄封装 `crypto`。算法名先归一化（去 `-`/`_`、转小写），与 Python 侧一致。
const HASH_ALGOS = ['md5', 'sha1', 'sha256', 'sha512'];

function normAlgo(algo) {
  const name = pText(algo).toLowerCase().replace(/-/g, '').replace(/_/g, '');
  if (!HASH_ALGOS.includes(name)) {
    throw R.valueErr(`不支持的摘要算法「${algo}」，可选：md5/sha1/sha256/sha512`);
  }
  return name;
}

function digestOf(text, algo) {
  return _crypto.createHash(normAlgo(algo))
    .update(Buffer.from(pText(text), 'utf8')).digest('hex');
}

const CRYPTO_FUNCS = {
  '摘要': new R.Builtin('摘要', (text, algo = 'sha256') => digestOf(text, algo)),
  'md5': new R.Builtin('md5', (text) => digestOf(text, 'md5')),
  'sha1': new R.Builtin('sha1', (text) => digestOf(text, 'sha1')),
  'sha256': new R.Builtin('sha256', (text) => digestOf(text, 'sha256')),
  'sha512': new R.Builtin('sha512', (text) => digestOf(text, 'sha512')),
  '文件摘要': new R.Builtin('文件摘要', (p, algo = 'sha256') => {
    const name = normAlgo(algo);
    const target = pText(p);
    const h = _crypto.createHash(name);
    let fd = null;
    try {
      fd = fs.openSync(target, 'r');
      const buf = Buffer.alloc(65536);          // 分块读，不整个载入内存
      for (;;) {
        const n = fs.readSync(fd, buf, 0, buf.length, null);
        if (n === 0) break;
        h.update(buf.subarray(0, n));
      }
    } catch (e) {
      throw R.valueErr(`读文件「${target}」失败：${e && e.message ? e.message : e}`);
    } finally {
      if (fd !== null) { try { fs.closeSync(fd); } catch (e) { /* 关不上就算了 */ } }
    }
    return h.digest('hex');
  }),
  '支持的算法': new R.Builtin('支持的算法', () => HASH_ALGOS.slice().sort(cmpCodepoint)),
};

// -- 压缩（M31）--
// **Node 没有内置 zip**，所以这一块是手写 zip 容器：
// `zlib.deflateRawSync` / `inflateRawSync` 只负责 deflate 流，本地文件头、
// 中央目录、EOCD、CRC-32 都得自己拼。格式照 PKWARE APPNOTE 的 4.3.6/4.3.12/4.3.16。
let _crcTable = null;

/** CRC-32（IEEE 802.3，zip 用）。自己写，不依赖 `zlib.crc32` 的版本可用性。 */
function crc32(buf) {
  if (_crcTable === null) {
    _crcTable = new Int32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
      _crcTable[n] = c;
    }
  }
  let crc = -1;
  for (let i = 0; i < buf.length; i++) {
    crc = (crc >>> 8) ^ _crcTable[(crc ^ buf[i]) & 0xff];
  }
  return (crc ^ -1) >>> 0;
}

/** 毫秒 → zip 的 DOS 时间/日期（1980 起算）。 */
function dosDateTime(ms) {
  const d = new Date(ms);
  const date = ((d.getFullYear() - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate();
  const time = (d.getHours() << 11) | (d.getMinutes() << 5)
    | Math.floor(d.getSeconds() / 2);
  return { date, time };
}

/** 按 Python `os.walk` 的顺序（先当前层的文件，再依次进子目录）收集文件。 */
function walkFiles(dir) {
  const out = [];
  const visit = (d) => {
    let entries;
    try { entries = fs.readdirSync(d, { withFileTypes: true }); } catch (e) { return; }
    const dirs = [];
    for (const ent of entries) {
      const full = _path.join(d, ent.name);
      if (ent.isDirectory()) dirs.push(full);
      else out.push(full);
    }
    for (const sub of dirs) visit(sub);
  };
  visit(dir);
  return out;
}

/** 写一条本地文件头 + 数据，返回中央目录项要用的元信息。 */
function writeZipEntry(push, rawName, data, localOffset, mtimeMs) {
  // zip 规范要求条目名用 `/` 分隔（Python 的 zipfile 也会把 `\` 换掉），
  // 而 Windows 上 `path.relative` 给的是 `\`——不换就会出现
  // `dir\a.txt` 这种按规范非法的名字。
  const arcName = String(rawName).replace(/\\/g, '/');
  const nameBuf = Buffer.from(arcName, 'utf8');
  const isAscii = /^[\x00-\x7f]*$/.test(arcName);
  const flags = isAscii ? 0 : 0x0800;            // bit 11 = 文件名是 UTF-8
  const method = 8;                             // deflate
  const comp = _zlib.deflateRawSync(data);
  const crc = crc32(data);
  const { date, time } = dosDateTime(mtimeMs);
  const hdr = Buffer.alloc(30);
  hdr.writeUInt32LE(0x04034b50, 0);             // 本地文件头签名
  hdr.writeUInt16LE(20, 4);                     // 需要的版本
  hdr.writeUInt16LE(flags, 6);
  hdr.writeUInt16LE(method, 8);
  hdr.writeUInt16LE(time, 10);
  hdr.writeUInt16LE(date, 12);
  hdr.writeUInt32LE(crc, 14);
  hdr.writeUInt32LE(comp.length, 18);
  hdr.writeUInt32LE(data.length, 22);
  hdr.writeUInt16LE(nameBuf.length, 26);
  hdr.writeUInt16LE(0, 28);                     // 扩展字段长度
  push(hdr); push(nameBuf); push(comp);
  return { nameBuf, flags, method, time, date, crc, csize: comp.length,
           usize: data.length, offset: localOffset };
}

function centralEntry(e) {
  const b = Buffer.alloc(46);
  b.writeUInt32LE(0x02014b50, 0);               // 中央目录项签名
  b.writeUInt16LE(20, 4);                       // 由什么版本创建
  b.writeUInt16LE(20, 6);                       // 需要的版本
  b.writeUInt16LE(e.flags, 8);
  b.writeUInt16LE(e.method, 10);
  b.writeUInt16LE(e.time, 12);
  b.writeUInt16LE(e.date, 14);
  b.writeUInt32LE(e.crc, 16);
  b.writeUInt32LE(e.csize, 20);
  b.writeUInt32LE(e.usize, 24);
  b.writeUInt16LE(e.nameBuf.length, 28);
  b.writeUInt16LE(0, 30);                       // 扩展字段
  b.writeUInt16LE(0, 32);                       // 注释
  b.writeUInt16LE(0, 34);                       // 起始磁盘
  b.writeUInt16LE(0, 36);                       // 内部属性
  b.writeUInt32LE(0, 38);                       // 外部属性
  b.writeUInt32LE(e.offset, 42);
  return Buffer.concat([b, e.nameBuf]);
}

/** 读 zip：从 EOCD 找中央目录，再逐条按偏移取本地头与数据。 */
function readZipEntries(buf) {
  let eocd = -1;
  const earliest = Math.max(0, buf.length - 65557);   // 注释最长 65535
  for (let i = buf.length - 22; i >= earliest; i--) {
    if (buf.readUInt32LE(i) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error('找不到 zip 结尾记录（EOCD）');
  const count = buf.readUInt16LE(eocd + 10);
  const cdOff = buf.readUInt32LE(eocd + 16);
  const out = [];
  let p = cdOff;
  for (let i = 0; i < count; i++) {
    if (buf.readUInt32LE(p) !== 0x02014b50) throw new Error('中央目录坏了');
    const flags = buf.readUInt16LE(p + 8);
    const method = buf.readUInt16LE(p + 10);
    const csize = buf.readUInt32LE(p + 20);
    const nameLen = buf.readUInt16LE(p + 28);
    const extraLen = buf.readUInt16LE(p + 30);
    const commentLen = buf.readUInt16LE(p + 32);
    const localOff = buf.readUInt32LE(p + 42);
    const name = buf.subarray(p + 46, p + 46 + nameLen)
      .toString((flags & 0x0800) ? 'utf8' : 'latin1');
    p += 46 + nameLen + extraLen + commentLen;
    if (buf.readUInt32LE(localOff) !== 0x04034b50) throw new Error('本地文件头坏了');
    const lNameLen = buf.readUInt16LE(localOff + 26);
    const lExtraLen = buf.readUInt16LE(localOff + 28);
    const dataStart = localOff + 30 + lNameLen + lExtraLen;
    const raw = buf.subarray(dataStart, dataStart + csize);
    out.push({ name, data: method === 0 ? Buffer.from(raw) : _zlib.inflateRawSync(raw) });
  }
  return out;
}

function zipBytes(zipPath) {
  const target = pText(zipPath);
  const st = statOf(target);
  if (!st || !st.isFile()) throw R.valueErr(`「${target}」不是文件`);
  try { return fs.readFileSync(target); } catch (e) {
    throw R.runErr(`读「${target}」失败：${e && e.message ? e.message : e}`);
  }
}

const ZIP_FUNCS = {
  '打包': new R.Builtin('打包', (zipPath, ...items) => {
    let list = items;
    if (list.length === 1 && Array.isArray(list[0])) list = list[0];
    if (list.length === 0) throw R.valueErr('打包至少需要提供一个文件或目录');
    const out = pText(zipPath);
    const chunks = [];
    const central = [];
    let offset = 0;
    const push = (b) => { chunks.push(b); offset += b.length; };
    for (const item of list) {
      const p = pText(item);
      const st = statOf(p);
      if (!st) throw R.valueErr(`「${p}」不存在，无法打包`);
      if (st.isDirectory()) {
        // 条目名以「目录名/…」为前缀（Python 侧用 relpath(dirname(p), full)）
        const parent = _path.dirname(p);
        for (const full of walkFiles(p)) {
          const arc = _path.relative(parent, full);
          const localOff = offset;
          const fst = statOf(full);
          central.push(writeZipEntry(push, arc, fs.readFileSync(full), localOff,
                                     fst ? fst.mtimeMs : Date.now()));
        }
      } else {
        const localOff = offset;
        central.push(writeZipEntry(push, _path.basename(p), fs.readFileSync(p),
                                   localOff, st.mtimeMs));
      }
    }
    const cd = Buffer.concat(central.map(centralEntry));
    const eocd = Buffer.alloc(22);
    eocd.writeUInt32LE(0x06054b50, 0);
    eocd.writeUInt16LE(0, 4);                   // 本磁盘号
    eocd.writeUInt16LE(0, 6);                   // 中央目录起始磁盘
    eocd.writeUInt16LE(central.length, 8);
    eocd.writeUInt16LE(central.length, 10);
    eocd.writeUInt32LE(cd.length, 12);
    eocd.writeUInt32LE(offset, 16);
    eocd.writeUInt16LE(0, 20);                  // 注释长度
    push(cd); push(eocd);
    try { fs.writeFileSync(out, Buffer.concat(chunks)); } catch (e) {
      throw R.runErr(`打包到「${out}」失败：${e && e.message ? e.message : e}`);
    }
    return out;
  }),
  '解压': new R.Builtin('解压', (zipPath, target = '') => {
    const dst = (target === '' || target === null || target === undefined)
      ? '.' : pText(target);
    const buf = zipBytes(zipPath);
    let entries;
    try { entries = readZipEntries(buf); } catch (e) {
      throw R.runErr(`解压「${pText(zipPath)}」失败：${e && e.message ? e.message : e}`);
    }
    try {
      fs.mkdirSync(dst, { recursive: true });
      for (const e of entries) {
        const outPath = _path.join(dst, e.name);
        fs.mkdirSync(_path.dirname(outPath), { recursive: true });
        fs.writeFileSync(outPath, e.data);
      }
    } catch (e) {
      throw R.runErr(`解压「${pText(zipPath)}」失败：${e && e.message ? e.message : e}`);
    }
    return dst;
  }),
  '列出内容': new R.Builtin('列出内容', (zipPath) => {
    const buf = zipBytes(zipPath);
    try { return readZipEntries(buf).map((e) => e.name); } catch (e) {
      throw R.runErr(`「${pText(zipPath)}」不是有效的 zip：`
        + `${e && e.message ? e.message : e}`);
    }
  }),
};

// -- 网络（M31）--
// **Node 没有同步 HTTP**（`fetch` 是异步的，而基石没有 await 语法）。
// 方案：`spawnSync` 起一个**自身的子进程**执行异步请求，结果经 stdout 回传。
// 零第三方依赖，也不要求系统装了 `curl`。代价是每次请求多一次进程启动
// （本机实测约 40ms），换来与 Python 侧 `urllib` 一样的**同步**调用形态。
const NET_CHILD = [
  "const [method, url, body, timeout] = process.argv.slice(1);",
  "(async () => {",
  "  const ctl = new AbortController();",
  "  const timer = setTimeout(() => ctl.abort(), Math.max(100, Number(timeout) * 1000));",
  "  try {",
  "    const opt = { method, signal: ctl.signal,",
  "                  headers: { 'User-Agent': 'jishi/0.1' } };",
  "    if (method === 'POST') {",
  "      opt.body = body;",
  "      opt.headers['Content-Type'] = 'application/x-www-form-urlencoded';",
  "    }",
  "    const resp = await fetch(url, opt);",
  "    process.stdout.write(await resp.text());",
  "  } catch (e) {",
  "    process.stderr.write(String((e && e.message) ? e.message : e));",
  "    process.exitCode = 1;",
  "  } finally {",
  "    clearTimeout(timer);",
  "  }",
  "})();",
].join('\n');

function netRequest(method, url, body, timeout) {
  const site = pText(url);
  const secs = Number(timeout);
  const args = ['-e', NET_CHILD, method, site,
                (body === null || body === undefined) ? '' : pText(body),
                String(secs)];
  const res = _cp.spawnSync(process.execPath, args, {
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024,
    timeout: Math.max(100, secs * 1000) + 5000,
  });
  if (res.error) throw R.runErr(`访问「${site}」失败：${res.error.message}`);
  if (res.status !== 0) {
    const lines = String(res.stderr || '').trim().split('\n');
    const why = lines[lines.length - 1] || '未知原因';
    throw R.runErr(`${method === 'POST' ? '提交到' : '访问'}「${site}」失败：${why}`);
  }
  return res.stdout;
}

const NET_FUNCS = {
  '获取': new R.Builtin('获取', (url, timeout = 10.0) =>
    netRequest('GET', url, null, timeout)),
  '提交': new R.Builtin('提交', (url, data = '', timeout = 10.0) =>
    netRequest('POST', url, data, timeout)),
  '获取JSON': new R.Builtin('获取JSON', (url, timeout = 10.0) => {
    const text = netRequest('GET', url, null, timeout);
    try { return jsonDecode(text); } catch (e) {
      throw R.valueErr(`「${pText(url)}」返回的不是有效 JSON：`
        + `${e && e.message ? e.message : e}`);
    }
  }),
};

// ---------------------------------------------------------------------------
// M41 新增模块：容器 / 迭代 / 参数 / 日志
//
// 与 Python 侧 jishi/stdlib/ 下同名文件逐函数对齐。四个模块都只用「列表 /
// 字典 / 文本」这些共同类型，所以不需要引擎特有能力。
// ---------------------------------------------------------------------------

/** 序列化：与 Python 侧 `_as_iterable` 同口径（列表/集合/字典键/文本）。 */
function seqFor(name, v) {
  const s = R.seqOf(v, true, undefined, undefined);
  if (s === null) throw R.typeErr(`「${R.display(v)}」不能当「${name}」的内容用`);
  return s;
}

/** 排序用的比较：与 Python 侧一样「保序」——平局按首次出现的先后。 */
function stableBy(entries, keyFn) {
  return entries
    .map((e, i) => [e, i])
    .sort((a, b) => {
      const ka = keyFn(a[0]); const kb = keyFn(b[0]);
      if (ka < kb) return -1;
      if (ka > kb) return 1;
      return a[1] - b[1];
    })
    .map(([e]) => e);
}

const CONTAINER_FUNCS = {
  '计数': new R.Builtin('计数', (it) => {
    const out = new Map();
    for (const x of seqFor('计数', it)) {
      const k = R.setKey(x);
      if (out.has(k)) out.get(k).n += 1;
      else out.set(k, { v: x, n: 1 });
    }
    const d = new Map();
    for (const { v, n } of out.values()) d.set(v, n);
    return d;
  }),
  '最多': new R.Builtin('最多', (it, count = 1) => {
    const n = Number(count);
    if (n <= 0) throw R.valueErr(`「最多」的个数要大于 0，得到了 ${count}`);
    const seq = seqFor('最多', it);
    const seen = new Map();
    for (const x of seq) {
      const k = R.setKey(x);
      if (seen.has(k)) seen.get(k).n += 1;
      else seen.set(k, { v: x, n: 1, i: seen.size });
    }
    const items = [...seen.values()].sort((a, b) => (b.n - a.n) || (a.i - b.i));
    const picked = items.slice(0, n).map((o) => [o.v, o.n]);
    if (n === 1) return picked.length ? picked[0][0] : null;
    return picked;
  }),
  '按值排序': new R.Builtin('按值排序', (d, desc = true) => {
    if (!(d instanceof Map)) {
      throw R.typeErr(`「按值排序」要一个字典，得到了「${R.typeName(d)}」`);
    }
    const items = [...d.entries()];
    const keyed = items.map((e, i) => [e, i]);
    keyed.sort((a, b) => {
      const ka = a[0][1]; const kb = b[0][1];
      const c = ka < kb ? -1 : (ka > kb ? 1 : 0);
      const r = desc ? -c : c;
      return r !== 0 ? r : a[1] - b[1];
    });
    return keyed.map(([e]) => [e[0], e[1]]);
  }),
  '分组': new R.Builtin('分组', (it, keyFn) => {
    const out = new Map();
    for (const x of seqFor('分组', it)) {
      const k = R.callValue(keyFn, [x]);
      const hk = R.setKey(k);
      if (out.has(hk)) out.get(hk).list.push(x);
      else out.set(hk, { k, list: [x] });
    }
    const d = new Map();
    for (const { k, list } of out.values()) d.set(k, list);
    return d;
  }),
  '取前': new R.Builtin('取前', (it, n) => seqFor('取前', it).slice(0, Math.max(0, Math.trunc(Number(n))))),
  '取后': new R.Builtin('取后', (it, n) => {
    const all = seqFor('取后', it);
    const k = Math.max(0, Math.trunc(Number(n)));
    return k ? all.slice(all.length - k) : [];
  }),
  '分块': new R.Builtin('分块', (it, n) => {
    const k = Math.trunc(Number(n));
    if (k <= 0) throw R.valueErr(`「分块」的每块个数要大于 0，得到了 ${n}`);
    const all = seqFor('分块', it);
    const out = [];
    for (let i = 0; i < all.length; i += k) out.push(all.slice(i, i + k));
    return out;
  }),
};

const ITER_FUNCS = {
  '分组': new R.Builtin('分组', (it, n) => {
    const k = Math.trunc(Number(n));
    if (k <= 0) throw R.valueErr(`「分组」的每组个数要大于 0，得到了 ${n}`);
    const all = seqFor('分组', it);
    const out = [];
    for (let i = 0; i < all.length; i += k) out.push(all.slice(i, i + k));
    return out;
  }),
  '滑窗': new R.Builtin('滑窗', (it, size, step = 1) => {
    const n = Math.trunc(Number(size)); const s = Math.trunc(Number(step));
    if (n <= 0) throw R.valueErr(`「滑窗」的窗口大小要大于 0，得到了 ${size}`);
    if (s <= 0) throw R.valueErr(`「滑窗」的步长要大于 0，得到了 ${step}`);
    const all = seqFor('滑窗', it);
    const out = [];
    for (let i = 0; i + n <= all.length; i += s) out.push(all.slice(i, i + n));
    return out;
  }),
  '去重': new R.Builtin('去重', (it) => {
    const out = []; const seen = new Set();
    for (const x of seqFor('去重', it)) {
      const k = R.setKey(x);
      if (seen.has(k)) continue;
      seen.add(k); out.push(x);
    }
    return out;
  }),
  '展开': new R.Builtin('展开', (it) => {
    const out = [];
    for (const seg of seqFor('展开', it)) {
      const s = R.seqOf(seg, true, undefined, undefined);
      if (s === null) out.push(seg); else out.push(...s);
    }
    return out;
  }),
  '取前': new R.Builtin('取前', (it, n) => seqFor('取前', it).slice(0, Math.max(0, Math.trunc(Number(n))))),
  '取后': new R.Builtin('取后', (it, n) => {
    const all = seqFor('取后', it);
    const k = Math.max(0, Math.trunc(Number(n)));
    return k ? all.slice(all.length - k) : [];
  }),
  '求和': new R.Builtin('求和', (it) => {
    let s = 0;
    for (const x of seqFor('求和', it)) s += R.unwrap(x);
    return s;
  }),
  '计数': new R.Builtin('计数', (it, cond) => {
    let n = 0;
    for (const x of seqFor('计数', it)) if (R.truthy(R.callValue(cond, [x]))) n += 1;
    return n;
  }),
  // M51 补：这四个是 M50 第一批加的（「既有模块补缺」），只做了 Python 侧。
  '累积': new R.Builtin('累积', (it, init = 0) => {
    const out = []; let acc = init;
    // 用基石自己的 `+`（文本拼接、列表相加都算数），不是 JS 的 `+`
    for (const x of seqFor('累积', it)) { acc = R.applyBinop('+', acc, x); out.push(acc); }
    return out;
  }),
  '组合': new R.Builtin('组合', (it, k) => {
    const n = Math.trunc(Number(k));
    if (n < 0) throw R.typeErr(`「组合」要取的个数不能是负数，得到了 ${R.display(k)}`);
    const all = seqFor('组合', it);
    if (n > all.length) return [];
    const out = [];
    const pick = (start, cur) => {
      if (cur.length === n) { out.push(cur.slice()); return; }
      for (let i = start; i < all.length; i++) {
        cur.push(all[i]); pick(i + 1, cur); cur.pop();
      }
    };
    pick(0, []);
    return out;                          // 顺序与 itertools.combinations 一致
  }),
  '排列': new R.Builtin('排列', (it, k = -1) => {
    const all = seqFor('排列', it);
    const n = Number(k) === -1 ? all.length : Math.trunc(Number(k));
    if (n < 0) throw R.typeErr(`「排列」要取的个数不能是负数，得到了 ${R.display(k)}`);
    if (n > all.length) return [];
    const out = []; const used = new Array(all.length).fill(false); const cur = [];
    const walk = () => {
      if (cur.length === n) { out.push(cur.slice()); return; }
      for (let i = 0; i < all.length; i++) {
        if (used[i]) continue;           // 按**位置**去重，与 itertools 一致
        used[i] = true; cur.push(all[i]); walk(); cur.pop(); used[i] = false;
      }
    };
    walk();
    return out;
  }),
  '笛卡尔积': new R.Builtin('笛卡尔积', (...seqs) => {
    if (seqs.length === 0) return [];
    const lists = seqs.map((s, i) => {
      const got = R.seqOf(s, true, undefined, undefined);
      if (got === null) {
        throw R.typeErr(`「${R.display(s)}」不能当「笛卡尔积」第 ${i + 1} 个序列用`);
      }
      return got;
    });
    if (lists.some((l) => l.length === 0)) return [];
    let out = [[]];
    for (const l of lists) {
      const next = [];
      for (const prefix of out) for (const x of l) next.push([...prefix, x]);
      out = next;
    }
    return out;
  }),
};

/** 参数解析：与 Python 侧 参数.py 同一套规则（句柄式）。
 *
 *  句柄式而不是 `解析器.选项(...)`：Rust 宿主的 `Val::Builtin` 只能存
 *  无捕获的函数指针，挂不了「每个解析器一套方法」。为了五路写法一致，
 *  统一「新建拿句柄 → 句柄当第一个参数传回去」。
 */
const _parsers = [];

class ArgParser {
  constructor(name, desc = '') {
    this.name = String(name); this.desc = String(desc); this.specs = [];
  }
  addOption(name, def, desc) {
    this.specs.push({ name: String(name), def, desc: String(desc), flag: false });
  }
  addFlag(name, desc) {
    this.specs.push({ name: String(name), def: false, desc: String(desc), flag: true });
  }
  usage() {
    const lines = [`用法：${this.name} [命令] [选项]`];
    if (this.desc) lines.push(`  ${this.desc}`);
    if (this.specs.length) {
      lines.push('选项：');
      for (const o of this.specs) {
        lines.push(o.flag
          ? `  --${o.name}         ${o.desc}`
          : `  --${o.name} <值>     ${o.desc}（默认 ${R.display(o.def)}）`);
      }
    }
    return lines.join('\n');
  }
}

function _parserOf(h) {
  const i = Number(h);
  if (!Number.isInteger(i) || i < 1 || i > _parsers.length) {
    throw R.runErr('参数解析器已失效（要用 参数.新建(...) 造一个）');
  }
  return _parsers[i - 1];
}

function _parseList(specs, list) {
  if (typeof list === 'string') {
    throw R.runErr('「参数.解析」要一个参数列表，通常写 参数.解析(解析器, 系统.参数())');
  }
  const arr = [...list];
  const known = new Map(specs.map((o) => [o.name, o]));
  const out = new Map();
  for (const o of specs) out.set(o.name, o.def);
  const positional = [];
  let command = '';
  let i = 0;
  while (i < arr.length) {
    const raw = arr[i];
    const t = typeof raw === 'string' ? raw : R.display(raw);
    if (t === '--') { positional.push(...arr.slice(i + 1)); break; }
    if (t.startsWith('--')) {
      const body = t.slice(2);
      if (body.includes('=')) {
        const [nm, ...rest] = body.split('=');
        if (!known.has(nm)) throw R.runErr(_unknownOpt(specs, nm));
        if (known.get(nm).flag) throw R.runErr(`「--${nm}」是个开关，不该给值`);
        out.set(nm, rest.join('='));
        i += 1; continue;
      }
      if (!known.has(body)) throw R.runErr(_unknownOpt(specs, body));
      if (known.get(body).flag) { out.set(body, true); i += 1; continue; }
      if (i + 1 >= arr.length) throw R.runErr(`「--${body}」后面要跟一个值`);
      out.set(body, arr[i + 1]);
      i += 2; continue;
    }
    if (!command && positional.length === 0) command = t;
    else positional.push(raw);
    i += 1;
  }
  out.set('命令', command);
  out.set('位置', positional);
  return out;
}

function _unknownOpt(specs, name) {
  const all = specs.map((o) => `--${o.name}`).join('、') || '（没有）';
  return `不认识选项「--${name}」。已声明的有：${all}`;
}

const ARG_FUNCS = {
  '新建': new R.Builtin('新建', (name, desc = '') => {
    _parsers.push(new ArgParser(name, desc));
    return _parsers.length;
  }),
  '选项': new R.Builtin('选项', (h, name, def = null, desc = '') => {
    _parserOf(h).addOption(name, def, desc);
    return h;
  }),
  '标志': new R.Builtin('标志', (h, name, desc = '') => {
    _parserOf(h).addFlag(name, desc);
    return h;
  }),
  '用法': new R.Builtin('用法', (h) => _parserOf(h).usage()),
  '解析': new R.Builtin('解析', (a, b) => {
    // 一次式：第一个参数不是句柄（是列表），第二个是「选项 → 默认值」字典
    if (typeof a !== 'number' || !Number.isInteger(a) || a < 1) {
      if (!(b instanceof Map)) {
        throw R.runErr('「参数.解析」的第二个参数要是字典（选项名 → 默认值）');
      }
      const specs = [];
      for (const [k, v] of b.entries()) {
        if (typeof v === 'boolean') specs.push({ name: String(k), def: false, desc: '', flag: true });
        else specs.push({ name: String(k), def: v, desc: '', flag: false });
      }
      return _parseList(specs, a);
    }
    if (b === undefined) {
      throw R.runErr('「参数.解析」还要一个参数列表，通常写 参数.解析(解析器, 系统.参数())');
    }
    return _parseList(_parserOf(a).specs, b);
  }),
};

/** 日志：级别 + 前缀（与 Python 侧 日志.py 对齐）。 */
const _logState = { level: '信息', prefix: '' };
const _LEVELS = { '调试': 10, '信息': 20, '警告': 30, '错误': 40, '静默': 99 };

function _logOut(level, parts) {
  if (_LEVELS[level] < _LEVELS[_logState.level]) return;
  const seg = [];
  if (_logState.prefix) seg.push(_logState.prefix);
  seg.push(`[${level}]`);
  seg.push(...parts.map((x) => R.display(x)));
  const line = seg.join(' ');
  if (level === '警告' || level === '错误') process.stderr.write(line + '\n');
  else process.stdout.write(line + '\n');
}

const LOG_FUNCS = {
  '级别们': new R.Builtin('级别们', () =>
    Object.keys(_LEVELS).sort((a, b) => _LEVELS[a] - _LEVELS[b])),
  '设级别': new R.Builtin('设级别', (lv) => {
    const k = String(lv);
    if (!(k in _LEVELS)) {
      throw R.valueErr(`不认识级别「${k}」，可用的有：${Object.keys(_LEVELS).join('、')}`);
    }
    _logState.level = k;
    return null;
  }),
  '取级别': new R.Builtin('取级别', () => _logState.level),
  '设前缀': new R.Builtin('设前缀', (p) => { _logState.prefix = String(p); return null; }),
  '调试': new R.Builtin('调试', (...a) => { _logOut('调试', a); return null; }),
  '信息': new R.Builtin('信息', (...a) => { _logOut('信息', a); return null; }),
  '警告': new R.Builtin('警告', (...a) => { _logOut('警告', a); return null; }),
  '错误': new R.Builtin('错误', (...a) => { _logOut('错误', a); return null; }),
};

// ---------------------------------------------------------------------------
// M50 新增的六个标准库模块（与 Python 侧 jishi/stdlib/ 下同名文件对齐）
//
// ⚠️ 对齐纪律（照 Python 侧的语义逐条搬，别「顺手改好」）：
//   · 报错措辞与 Python 侧**逐字一致**（对拍测试逐字节比 stdout 与报错文本）
//   · 顺序敏感的地方要**保序**（`众数` 并列取最先出现、`去重` 保首次出现顺序）
//   · 浮点算出来的值与 Python 的差可能在末位，对拍时用近似断言
// ---------------------------------------------------------------------------

/** 统计：集中趋势与离散程度（对齐 Python 侧 统计.py）。 */
function _numsFor(name, v) {
  if (typeof v === 'string') {
    throw R.typeErr(`「${name}」要传一组数字，不能传文本`);
  }
  const s = R.seqOf(v, true, undefined, undefined);
  if (s === null) throw R.typeErr(`「${name}」要传一组数字（列表）`);
  return s.map((x0, i) => {
    // ⚠️ 先 `R.unwrap`：JS 侧浮点是 `FloatBox` 包装的（见 runtime.js 的说明），
    //    不解包会 `typeof x !== 'number'` 而把 `9.5` 判成「不是数字」。
    const x = R.unwrap(x0);
    if (typeof x === 'boolean' || typeof x !== 'number') {
      throw R.typeErr(`「${name}」里第 ${i + 1} 个不是数字：${R.display(x0)}`);
    }
    return x;
  });
}
function _need(name, xs) {
  if (xs.length === 0) throw R.valueErr(`「${name}」要至少有一个数字，现在是空的`);
  return xs;
}
function _isInt(v) {
  const x = R.unwrap(v);
  return typeof x === 'number' && Number.isInteger(x);
}

const STATS_FUNCS = {
  '求和': new R.Builtin('求和', (d) => _numsFor('求和', d).reduce((a, b) => a + b, 0)),
  '平均': new R.Builtin('平均', (d) => {
    const xs = _need('平均', _numsFor('平均', d));
    return new R.FloatBox(xs.reduce((a, b) => a + b, 0) / xs.length);
  }),
  '中位数': new R.Builtin('中位数', (d) => {
    const xs = _need('中位数', _numsFor('中位数', d)).slice().sort((a, b) => a - b);
    const n = xs.length; const mid = Math.floor(n / 2);
    if (n % 2) return xs[mid];
    return new R.FloatBox((xs[mid - 1] + xs[mid]) / 2);
  }),
  '众数': new R.Builtin('众数', (d) => {
    const xs = _need('众数', _numsFor('众数', d));
    const counts = new Map(); const order = [];
    for (const v of xs) {
      const k = R.setKey(v);
      if (!counts.has(k)) { counts.set(k, { v, n: 0 }); order.push(k); }
      counts.get(k).n += 1;
    }
    let best = counts.get(order[0]);
    for (const k of order) { if (counts.get(k).n > best.n) best = counts.get(k); }
    return best.v;
  }),
  '方差': new R.Builtin('方差', (d, sample = false) => {
    const xs = _need('方差', _numsFor('方差', d));
    const n = xs.length;
    const m = xs.reduce((a, b) => a + b, 0) / n;
    const ss = xs.reduce((a, b) => a + (b - m) ** 2, 0);
    if (sample) {
      if (n < 2) throw R.valueErr('样本方差至少要 2 个数（要除以 n-1）');
      return new R.FloatBox(ss / (n - 1));
    }
    return new R.FloatBox(ss / n);
  }),
  '标准差': new R.Builtin('标准差', (d, sample = false) => {
    const xs = _need('标准差', _numsFor('标准差', d));
    const n = xs.length;
    const m = xs.reduce((a, b) => a + b, 0) / n;
    const ss = xs.reduce((a, b) => a + (b - m) ** 2, 0);
    if (sample) {
      if (n < 2) throw R.valueErr('样本方差至少要 2 个数（要除以 n-1）');
      return new R.FloatBox(Math.sqrt(ss / (n - 1)));
    }
    return new R.FloatBox(Math.sqrt(ss / n));
  }),
  '极差': new R.Builtin('极差', (d) => {
    const xs = _need('极差', _numsFor('极差', d));
    return Math.max(...xs) - Math.min(...xs);
  }),
  '分位数': new R.Builtin('分位数', (d, p0) => {
    // ⚠️ 数值参数要先 `R.unwrap`：JS 侧浮点是 `FloatBox` 包装的（为了
    //    `0.1 + 0.2 == 0.3` 这类语义），直接 `typeof p === 'number'` 会误判。
    const p = R.unwrap(p0);
    if (typeof p !== 'number' || typeof p === 'boolean') {
      throw R.typeErr('「分位数」的第二个参数要传 0 到 1 之间的小数');
    }
    if (!(p >= 0 && p <= 1)) throw R.valueErr(`分位数要在 0 到 1 之间，现在是 ${p}`);
    const xs = _need('分位数', _numsFor('分位数', d)).slice().sort((a, b) => a - b);
    if (xs.length === 1) return xs[0];
    const pos = p * (xs.length - 1);
    const lo = Math.floor(pos); const hi = Math.ceil(pos);
    if (lo === hi) return xs[lo];
    return new R.FloatBox(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo));
  }),
  '去极值平均': new R.Builtin('去极值平均', (d, k = 1) => {
    const kk = R.unwrap(k);
    if (typeof kk === 'boolean' || !_isInt(kk)) throw R.typeErr('「去掉个数」要传整数');
    if (kk < 0) throw R.valueErr('「去掉个数」不能是负数');
    const xs = _need('去极值平均', _numsFor('去极值平均', d)).slice().sort((a, b) => a - b);
    if (kk * 2 >= xs.length) {
      throw R.valueErr(`去掉 ${kk} 个最高和最低之后就没数据了（一共 ${xs.length} 个）`);
    }
    const kept = xs.slice(kk, xs.length - kk);
    return new R.FloatBox(kept.reduce((a, b) => a + b, 0) / kept.length);
  }),
  '加权平均': new R.Builtin('加权平均', (d, w) => {
    const xs = _need('加权平均', _numsFor('加权平均', d));
    const ws = _numsFor('加权平均的权重', w);
    if (xs.length !== ws.length) {
      throw R.valueErr(`数据和权重的个数不一样：${xs.length} 个数据、${ws.length} 个权重`);
    }
    const total = ws.reduce((a, b) => a + b, 0);
    if (total === 0) throw R.valueErr('权重之和是 0，没法算加权平均');
    let s = 0;
    for (let i = 0; i < xs.length; i++) s += xs[i] * ws[i];
    return new R.FloatBox(s / total);
  }),
};

/** 编码：base64 / 十六进制 / URL（对齐 Python 侧 编码.py）。 */
function _toBytesFor(name, v) {
  if (typeof v === 'string') return Buffer.from(v, 'utf8');
  if (Buffer.isBuffer(v)) return v;
  const s = R.seqOf(v, true, undefined, undefined);
  if (s === null) throw R.valueErr(`「${name}」要传文本、字节列表或 bytes`);
  const out = [];
  s.forEach((x, i) => {
    if (typeof x === 'boolean' || !_isInt(x)) {
      throw R.valueErr(`「${name}」里第 ${i + 1} 个不是 0-255 的整数`);
    }
    if (x < 0 || x > 255) {
      throw R.valueErr(`「${name}」里第 ${i + 1} 个超出范围（要 0-255）：${x}`);
    }
    out.push(x);
  });
  return Buffer.from(out);
}
function _wantBytes(name, v) {
  if (typeof v === 'string') {
    if (!/^[\x00-\x7f]*$/.test(v)) {
      throw R.valueErr(`「${name}」的内容应当是 base64 / 十六进制字符，但里面出现了非 ASCII 字符`);
    }
    return Buffer.from(v, 'ascii');
  }
  if (Buffer.isBuffer(v)) return v;
  const s = R.seqOf(v, true, undefined, undefined);
  if (s === null) throw R.valueErr(`「${name}」要传文本或字节列表`);
  return _toBytesFor(name, v);
}

const ENCODE_FUNCS = {
  'base64编码': new R.Builtin('base64编码', (v) =>
    _toBytesFor('base64编码', v).toString('base64')),
  'base64解码': new R.Builtin('base64解码', (v) => {
    const raw = _wantBytes('base64解码', v);
    const s = raw.toString('ascii');
    if (!/^[A-Za-z0-9+/]*={0,2}$/.test(s) || s.length % 4 !== 0) {
      throw R.valueErr(`这不是合法的 base64 内容：${s}`);
    }
    return [...Buffer.from(s, 'base64')];
  }),
  '十六进制编码': new R.Builtin('十六进制编码', (v, upper = false) => {
    const h = _toBytesFor('十六进制编码', v).toString('hex');
    return upper ? h.toUpperCase() : h;
  }),
  '十六进制解码': new R.Builtin('十六进制解码', (v) => {
    const raw = _wantBytes('十六进制解码', v);
    let s = raw.toString('ascii').replace(/[ \t]/g, '')
      .replace(/0[xX]/g, '').replace(/,/g, '');
    if (s === '') return [];
    if (s.length % 2) s = '0' + s;
    if (!/^[0-9a-fA-F]*$/.test(s)) {
      throw R.valueErr(`这不是合法的十六进制内容：${s}`);
    }
    return [...Buffer.from(s, 'hex')];
  }),
  'url编码': new R.Builtin('url编码', (v, keepSlash = true) => {
    const safe = keepSlash ? '/' : '';
    const s = encodeURIComponent(String(v));
    return safe ? s.replace(/%2F/g, '/') : s;
  }),
  'url解码': new R.Builtin('url解码', (v) => {
    if (typeof v !== 'string') throw R.valueErr('「url解码」要传文本');
    try {
      return decodeURIComponent(v);
    } catch (e) {
      throw R.valueErr(`这不是合法的 URL 编码内容：${e.message}`);
    }
  }),
  '文本字节': new R.Builtin('文本字节', (v) => {
    if (typeof v !== 'string') throw R.valueErr('「文本字节」要传文本');
    return [...Buffer.from(v, 'utf8')];
  }),
  '字节文本': new R.Builtin('字节文本', (v) => {
    const raw = _toBytesFor('字节文本', v);
    const s = raw.toString('utf8');
    if (Buffer.from(s, 'utf8').compare(raw) !== 0) {
      throw R.valueErr('这些字节不是合法的 UTF-8 文本（可能被截断或本来就是二进制）');
    }
    return s;
  }),
};

/** html：转义与生成片段（对齐 Python 侧 html.py）。 */
function _escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#x27;');
}
//: 「原样」标记：与 Python 侧一样用**普通对象**（不用 str/String 子类）——
//: 见 Python 侧 html.py 模块开头那段「实现上的一个坑」。
class RawHtml {
  constructor(s) { this.s = String(s); }
  toString() { return this.s; }
}
function _htmlContent(v) {
  return (v instanceof RawHtml) ? v.s : _escapeHtml(v);
}
const _ATTR_ALIAS = { '类名': 'class', '对应': 'for' };
/** 属性字典（基石字典 = Map）→ 属性串；`空` 值只输出属性名（布尔属性）。
 *  ⚠️ 用字典而不是关键字参数，理由见 Python 侧 html.py 的 `_属性串`。 */
function _attrStr(attrs) {
  if (attrs === null || attrs === undefined) return '';
  if (!(attrs instanceof Map)) {
    throw R.typeErr('属性要传字典，例如 标签("p", "内容", {"类名": "提示"})');
  }
  const parts = [];
  for (const [k0, v] of attrs) {
    const k = R.display(k0);
    const key = (_ATTR_ALIAS[k] || k).replace(/_/g, '-');
    if (v === null || v === undefined) parts.push(` ${key}`);
    else parts.push(` ${key}="${_escapeHtml(v)}"`);
  }
  return parts.join('');
}

const HTML_FUNCS = {
  '转义': new R.Builtin('转义', (v) => _escapeHtml(v)),
  '原样': new R.Builtin('原样', (v) => (v instanceof RawHtml ? v : new RawHtml(v))),
  '标签': new R.Builtin('标签', (tag, content = '', attrs = null) => {
    if (typeof tag !== 'string' || tag === '') {
      throw R.typeErr('「标签」的第一个参数要是标签名（非空文本）');
    }
    return `<${tag}${_attrStr(attrs)}>${_htmlContent(content)}</${tag}>`;
  }),
  '空元素': new R.Builtin('空元素', (tag, attrs = null) => {
    if (typeof tag !== 'string' || tag === '') {
      throw R.typeErr('「空元素」的第一个参数要是标签名（非空文本）');
    }
    return `<${tag}${_attrStr(attrs)} />`;
  }),
  '链接': new R.Builtin('链接', (text, href, attrs = null) => {
    const merged = new Map();
    merged.set('href', href);
    if (attrs instanceof Map) for (const [k, v] of attrs) merged.set(k, v);
    return HTML_FUNCS['标签'].fn('a', text, merged);
  }),
  '属性文本': new R.Builtin('属性文本', (v) => _escapeHtml(v)),
  '有序列表': new R.Builtin('有序列表', (items, attrs = null) => {
    const lis = seqFor('有序列表', items).map((x) => `<li>${_htmlContent(x)}</li>`).join('');
    return `<ol${_attrStr(attrs)}>${lis}</ol>`;
  }),
  '无序列表': new R.Builtin('无序列表', (items, attrs = null) => {
    const lis = seqFor('无序列表', items).map((x) => `<li>${_htmlContent(x)}</li>`).join('');
    return `<ul${_attrStr(attrs)}>${lis}</ul>`;
  }),
};

/** 对比：行级 diff（对齐 Python 侧 对比.py）。 */
function _linesFor(name, v) {
  if (typeof v === 'string') return v.split(/\r\n|\r|\n/).filter((_, i, a) => i < a.length - (v.endsWith('\n') || v.endsWith('\r') ? 1 : 0));
  const s = R.seqOf(v, true, undefined, undefined);
  if (s === null) throw R.typeErr(`「${name}」要传文本或文本列表`);
  return s.map((x, i) => {
    if (typeof x !== 'string') throw R.typeErr(`「${name}」的第 ${i + 1} 项不是文本：${R.display(x)}`);
    return x;
  });
}
/** 行级差异的 opcode 列表（等价 Python difflib.SequenceMatcher.get_opcodes）。 */
function _opcodes(a, b) {
  const n = a.length; const m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out = [];
  let i = 0; let j = 0;
  const push = (tag, i1, i2, j1, j2) => {
    const last = out[out.length - 1];
    if (last && last.tag === tag && last.i2 === i1 && last.j2 === j1) {
      last.i2 = i2; last.j2 = j2;
    } else out.push({ tag, i1, i2, j1, j2 });
  };
  while (i < n && j < m) {
    if (a[i] === b[j]) { push('equal', i, i + 1, j, j + 1); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { push('delete', i, i + 1, j, j); i++; }
    else { push('insert', i, i, j, j + 1); j++; }
  }
  while (i < n) { push('delete', i, i + 1, j, j); i++; }
  while (j < m) { push('insert', i, i, j, j + 1); j++; }
  // ⚠️ 相邻的 `delete` + `insert` 要合并成 `replace`——Python 的 `difflib`
  //    就是这么给的。不合并会让 `对比.并排` 把「一行换一行」拆成两行、
  //    让 `对比.统一` 的 `@@` 计数算错（实测踩到）。
  const merged = [];
  for (let k = 0; k < out.length; k++) {
    const cur = out[k];
    if (cur.tag === 'delete' && out[k + 1] && out[k + 1].tag === 'insert') {
      const nxt = out[k + 1];
      merged.push({ tag: 'replace', i1: cur.i1, i2: cur.i2, j1: nxt.j1, j2: nxt.j2 });
      k++;
    } else merged.push(cur);
  }
  return merged;
}

const DIFF_FUNCS = {
  '逐行': new R.Builtin('逐行', (oldV, newV, numbered = false) => {
    const a = _linesFor('逐行', oldV); const b = _linesFor('逐行', newV);
    const out = [];
    for (const { tag, i1, i2, j1, j2 } of _opcodes(a, b)) {
      if (tag === 'equal') {
        for (let k = i1; k < i2; k++) out.push([' ', numbered ? j1 + k - i1 + 1 : 0, a[k]]);
      } else if (tag === 'delete') {
        for (let k = i1; k < i2; k++) out.push(['-', numbered ? j1 + 1 : 0, a[k]]);
      } else if (tag === 'insert') {
        for (let k = j1; k < j2; k++) out.push(['+', numbered ? k + 1 : 0, b[k]]);
      } else {
        for (let k = i1; k < i2; k++) out.push(['-', numbered ? j1 + 1 : 0, a[k]]);
        for (let k = j1; k < j2; k++) out.push(['+', numbered ? k + 1 : 0, b[k]]);
      }
    }
    return out;
  }),
  '统一': new R.Builtin('统一', (oldV, newV, ctx0 = 3) => {
    const a = _linesFor('统一', oldV); const b = _linesFor('统一', newV);
    const n = R.unwrap(ctx0);
    if (typeof n === 'boolean' || !_isInt(n) || n < 0) {
      throw R.typeErr('「上下文」要传 0 或正整数');
    }
    // 照搬 Python `difflib.get_grouped_opcodes(n)` 的结构：
    // 它**已经把上下文行并进组里**，组与组之间不再补上下文。
    const ops = _opcodes(a, b);
    const groups = [];
    if (ops.length) {
      let g = [];
      // ⚠️ 空组要丢掉：n=0 时开头/结尾会出现「长度 0 的 equal」，
      //    difflib 不会为它单独出一个 @@（实测踩到）。
      const flush = () => {
        const real = g.filter((o) => o.tag !== 'equal' || o.i2 > o.i1);
        if (real.some((o) => o.tag !== 'equal')) groups.push(real);
        g = [];
      };
      for (let k = 0; k < ops.length; k++) {
        const op = ops[k];
        if (op.tag === 'equal' && op.i2 - op.i1 > 2 * n) {
          // 长串相同行：只留头 n 行；尾 n 行留给下一组当开头
          const head = { tag: 'equal', i1: op.i1, i2: op.i1 + n, j1: op.j1, j2: op.j1 + n };
          g.push(head); flush();
          const tail = { tag: 'equal', i1: op.i2 - n, i2: op.i2, j1: op.j2 - n, j2: op.j2 };
          g.push(tail);
          continue;
        }
        g.push(op);
      }
      flush();
    }
    const out = [];
    for (const g of groups) {
      const first = g[0]; const last = g[g.length - 1];
      const spanI = last.i2 - first.i1;
      const spanJ = last.j2 - first.j1;
      if (out.length === 0) { out.push('--- 旧'); out.push('+++ 新'); }
      const rng = (start, span) => (span === 1 ? `${start}` : `${start},${span}`);
      out.push(`@@ -${rng(first.i1 + 1, spanI)} +${rng(first.j1 + 1, spanJ)} @@`);
      for (const { tag, i1, i2, j1, j2 } of g) {
        if (tag === 'equal') {
          for (let x = i1; x < i2; x++) out.push(' ' + a[x]);
        } else {
          if (tag === 'delete' || tag === 'replace') {
            for (let x = i1; x < i2; x++) out.push('-' + a[x]);
          }
          if (tag === 'insert' || tag === 'replace') {
            for (let x = j1; x < j2; x++) out.push('+' + b[x]);
          }
        }
      }
    }
    return out.join('\n');
  }),
  '并排': new R.Builtin('并排', (oldV, newV, width0 = 30) => {
    const a = _linesFor('并排', oldV); const b = _linesFor('并排', newV);
    const width = R.unwrap(width0);
    if (typeof width === 'boolean' || !_isInt(width) || width < 4) {
      throw R.typeErr('「宽」要传不小于 4 的整数');
    }
    const cut = (s) => {
      let w = 0;
      for (const ch of s) w += ch.codePointAt(0) > 0x2e80 ? 2 : 1;
      if (w <= width) return s + ' '.repeat(width - w);
      let acc = 0; let res = '';
      for (const ch of s) {
        const cw = ch.codePointAt(0) > 0x2e80 ? 2 : 1;
        if (acc + cw > width - 1) break;
        res += ch; acc += cw;
      }
      return res + '…' + ' '.repeat(Math.max(0, width - acc - 1));
    };
    // ⚠️ 与 Python 侧一致：`replace` 块按「删 k 行 / 插 k 行」**成对**输出
    //    （第 k 对是「删的第 k 行 / 插的第 k 行」），不是先全列左边再全列右边。
    const out = [];
    for (const { tag, i1, i2, j1, j2 } of _opcodes(a, b)) {
      if (tag === 'equal') {
        for (let k = i1; k < i2; k++) out.push(`${cut(a[k])} | ${cut(b[j1 + k - i1])}`);
      } else {
        const leftN = (tag === 'insert') ? 0 : i2 - i1;
        const rightN = (tag === 'delete') ? 0 : j2 - j1;
        const n = Math.max(leftN, rightN);
        for (let k = 0; k < n; k++) {
          const left = k < leftN ? a[i1 + k] : '';
          const right = k < rightN ? b[j1 + k] : '';
          out.push(`${cut(left)} ${left === right ? ' ' : '~'}| ${cut(right)}`);
        }
      }
    }
    return out.join('\n');
  }),
  '相似度': new R.Builtin('相似度', (x, y) => {
    const a = _linesFor('相似度', x); const b = _linesFor('相似度', y);
    if (a.length === 0 && b.length === 0) return new R.FloatBox(1);
    const ops = _opcodes(a, b);
    let same = 0;
    for (const { tag, i1, i2 } of ops) if (tag === 'equal') same += i2 - i1;
    return new R.FloatBox((2 * same) / (a.length + b.length));
  }),
  '最相似': new R.Builtin('最相似', (target, candidates) => {
    if (typeof target !== 'string') throw R.typeErr('「最相似」的第一个参数要传文本');
    const items = _linesFor('最相似', candidates);
    if (items.length === 0) return '';
    let best = items[0]; let bestR = -1;
    for (const it of items) {
      const a = target.split(''); const b = it.split('');
      const ops = _opcodes(a, b);
      let same = 0;
      for (const { tag, i1, i2 } of ops) if (tag === 'equal') same += i2 - i1;
      const r = (2 * same) / (a.length + b.length);
      if (r > bestR) { best = it; bestR = r; }
    }
    return best;
  }),
};

/** 标识：UUID 式唯一标识（对齐 Python 侧 标识.py）。 */
const _B64URL = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
function _b64url(buf) {
  const s = buf.toString('base64');
  let out = '';
  for (const ch of s) {
    if (ch === '=') continue;
    const i = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'.indexOf(ch);
    out += _B64URL[i];
  }
  return out;
}

const ID_FUNCS = {
  '唯一标识': new R.Builtin('唯一标识', (noDash = false) => {
    const u = _crypto.randomUUID();
    return noDash ? u.replace(/-/g, '') : u;
  }),
  '唯一标识短': new R.Builtin('唯一标识短', () => _b64url(_crypto.randomBytes(16))),
  '唯一标识字节': new R.Builtin('唯一标识字节', () => [..._crypto.randomBytes(16)]),
};

/** 配置：JSON 与键值文本（对齐 Python 侧 配置.py）。 */
const _NOT_GIVEN = '\u0000未给\u0000';
const CFG_FUNCS = {
  '读JSON': new R.Builtin('读JSON', (path, dflt = _NOT_GIVEN) => {
    const p = String(path);
    if (!fs.existsSync(p)) {
      if (dflt !== _NOT_GIVEN) return dflt;
      throw R.fileErr(`配置文件「${p}」不存在`);
    }
    let text;
    try { text = fs.readFileSync(p, 'utf8'); }
    catch (e) { throw R.fileErr(`读配置文件「${p}」失败：${e.message}`); }
    // 复用 json 模块的 jsonDecode（它会把普通对象转成基石字典、报错也更准）
    try { return jsonDecode(text); }
    catch (e) { throw R.fileErr(`配置文件「${p}」不是合法的 JSON：${e.message}`); }
  }),
  '写JSON': new R.Builtin('写JSON', (path, data, indent = 2) => {
    const p = String(path);
    if (typeof indent === 'boolean' || !_isInt(indent) || indent < 0) {
      throw R.typeErr('「缩进」要传 0 或正整数');
    }
    // ⚠️ 复用 `json` 模块的 `jsonEncode`（它已经会处理 Map/集合/精确小数，
    //    并且缩进规则与 Python 侧一致）。别在这里用 `JSON.stringify` +
    //    自己写的转换——那个 `_jishiToPlain` 我一度写了但根本没定义。
    let text;
    try { text = jsonEncode(data, indent || 0); }
    catch (e) { throw R.valueErr(`这份数据没法写成 JSON：${e.message}`); }
    const dir = _path.dirname(p);
    if (dir) fs.mkdirSync(dir, { recursive: true });
    try { fs.writeFileSync(p, text + '\n', 'utf8'); }
    catch (e) { throw R.fileErr(`写配置文件「${p}」失败：${e.message}`); }
    return p;
  }),
  '读键值': new R.Builtin('读键值', (path, dflt = _NOT_GIVEN, sep = '=') => {
    const p = String(path);
    if (!fs.existsSync(p)) {
      if (dflt !== _NOT_GIVEN) return dflt;
      throw R.fileErr(`配置文件「${p}」不存在`);
    }
    if (typeof sep !== 'string' || sep === '') throw R.typeErr('「分隔符」要传非空文本');
    let text;
    try { text = fs.readFileSync(p, 'utf8'); }
    catch (e) { throw R.fileErr(`读配置文件「${p}」失败：${e.message}`); }
    const out = new Map();
    text.split(/\r\n|\r|\n/).forEach((raw, idx) => {
      const line = raw.trim();
      if (!line || line.startsWith('#')) return;
      if (!line.includes(sep)) {
        throw R.fileErr(`配置文件「${p}」第 ${idx + 1} 行没有「${sep}」：${line}`);
      }
      const at = line.indexOf(sep);
      const k = line.slice(0, at).trim();
      const v = _stripComment(line.slice(at + sep.length).trim());
      if (!k) throw R.fileErr(`配置文件「${p}」第 ${idx + 1} 行的键是空的`);
      out.set(k, v);
    });
    return out;
  }),
  '写键值': new R.Builtin('写键值', (path, cfg, sep = ' = ') => {
    const p = String(path);
    const d = cfg instanceof Map ? cfg : null;
    if (!d) throw R.typeErr('「写键值」的第二个参数要传字典');
    const lines = [];
    for (const [k, v] of d) {
      let s = R.display(v);
      if (s.includes('#') || s !== s.trim() || s.startsWith('"') || s.startsWith("'")) {
        s = '"' + s.replace(/"/g, '\\"') + '"';
      }
      lines.push(`${k}${sep}${s}`);
    }
    const dir = _path.dirname(p);
    if (dir) fs.mkdirSync(dir, { recursive: true });
    try { fs.writeFileSync(p, lines.join('\n') + (lines.length ? '\n' : ''), 'utf8'); }
    catch (e) { throw R.fileErr(`写配置文件「${p}」失败：${e.message}`); }
    return p;
  }),
  '取': new R.Builtin('取', (cfg, path, dflt = _NOT_GIVEN) => {
    if (typeof path !== 'string' || path === '') throw R.typeErr('「取」的路径要传非空文本');
    let cur = cfg; const walked = [];
    for (const seg of path.split('.')) {
      walked.push(seg);
      let ok = false;
      if (cur instanceof Map) {
        if (cur.has(seg)) { cur = cur.get(seg); ok = true; }
      } else if (Array.isArray(cur)) {
        if (/^\d+$/.test(seg) && Number(seg) < cur.length) { cur = cur[Number(seg)]; ok = true; }
      }
      if (!ok) {
        if (dflt !== _NOT_GIVEN) return dflt;
        const parent = walked.slice(0, -1).join('.') || '<根>';
        throw R.valueErr(`配置里没有「${walked.join('.')}」这一段（「${parent}」下面是${_shapeOf(cur)}）`);
      }
    }
    return cur;
  }),
  '设': new R.Builtin('设', (cfg, path, val) => {
    if (!(cfg instanceof Map)) throw R.typeErr('「设」的第一个参数要传字典');
    if (typeof path !== 'string' || path === '') throw R.typeErr('「设」的路径要传非空文本');
    const segs = path.split('.');
    const out = new Map(cfg);
    let cur = out;
    for (const seg of segs.slice(0, -1)) {
      let nxt = cur.get(seg);
      nxt = (nxt instanceof Map) ? new Map(nxt) : new Map();
      cur.set(seg, nxt); cur = nxt;
    }
    cur.set(segs[segs.length - 1], val);
    return out;
  }),
  '合并': new R.Builtin('合并', (base, over) => {
    if (!(base instanceof Map) || !(over instanceof Map)) {
      throw R.typeErr('「合并」的两个参数都要传字典');
    }
    const out = new Map(base);
    for (const [k, v] of over) {
      if (v instanceof Map && out.get(k) instanceof Map) out.set(k, CFG_FUNCS['合并'].fn(out.get(k), v));
      else out.set(k, v);
    }
    return out;
  }),
  '展开': new R.Builtin('展开', (cfg, prefix = '') => {
    if (!(cfg instanceof Map)) throw R.typeErr('「展开」的第一个参数要传字典');
    const out = new Map();
    for (const [k, v] of cfg) {
      const key = `${prefix}${k}`;
      if (v instanceof Map) {
        if (v.size) for (const [kk, vv] of CFG_FUNCS['展开'].fn(v, key + '.')) out.set(kk, vv);
        else out.set(key, new Map());
      } else out.set(key, v);
    }
    return out;
  }),
};

function _stripComment(s) {
  const q0 = s[0];
  if ((q0 === '"' || q0 === "'") && s[s.length - 1] === q0 && s.length >= 2) {
    return s.slice(1, -1);
  }
  let q = null;
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (q) { if (ch === q) q = null; }
    else if (ch === '"' || ch === "'") q = ch;
    else if (ch === '#') return s.slice(0, i).trim();
  }
  return s.trim();
}
function _shapeOf(v) {
  if (v instanceof Map) {
    const keys = [...v.keys()];
    if (!keys.length) return '空字典';
    const shown = keys.slice(0, 8).map((k) => R.display(k)).join('、');
    return `这些键：${shown}${keys.length > 8 ? `…等 ${keys.length} 个` : ''}`;
  }
  if (Array.isArray(v)) return `一个 ${v.length} 项的列表`;
  return `${R.typeName(v)} 类型`;
}

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
    // M51 补：这六个一直只在 Python 侧有，宿主静默缺失（写 `数学.正弦(1)`
    // 在 Python 上能跑、在宿主上报「模块里没有」）。三角函数收**弧度**，与
    // Python 的 `math.sin` 同口径。
    '正弦': new R.Builtin('正弦', (x) => new R.FloatBox(Math.sin(Number(x)))),
    '余弦': new R.Builtin('余弦', (x) => new R.FloatBox(Math.cos(Number(x)))),
    '正切': new R.Builtin('正切', (x) => new R.FloatBox(Math.tan(Number(x)))),
    '角度转弧度': new R.Builtin('角度转弧度',
      (x) => new R.FloatBox(Number(x) * Math.PI / 180)),
    '对数': new R.Builtin('对数', (x, base = Math.E) => {
      const v = Math.log(Number(x)) / Math.log(Number(base));
      // Python 的 math.log 在 x<=0 / 底不合法时抛 ValueError，宿主得自己判
      if (Number.isNaN(v) || !Number.isFinite(v)) {
        throw R.typeErr(`「${R.display(x)}」不能求以 ${R.display(base)} 为底的对数`);
      }
      return new R.FloatBox(v);
    }),
    '常用对数': new R.Builtin('常用对数', (x) => {
      const v = Math.log10(Number(x));
      if (Number.isNaN(v) || !Number.isFinite(v)) {
        throw R.typeErr(`「${R.display(x)}」不能求常用对数`);
      }
      return new R.FloatBox(v);
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
    // M51 补：`填充` / `截断` 是 M50 第一批加的（「既有模块补缺」），
    // 只做了 Python 侧，宿主静默缺失 —— 正是这个缺口促成 M51 的漂移检测。
    '填充': new R.Builtin('填充', (s, width, fill = ' ') => {
      const text = String(s); const w = Number(width); const f = String(fill);
      if ([...f].length !== 1) throw R.typeErr('「填充」的填充字符要传一个字符');
      const len = [...text].length;
      if (len >= w) return text;
      const total = w - len;
      const left = Math.floor(total / 2);          // 与 Python str.center 同侧
      return f.repeat(left) + text + f.repeat(total - left);
    }),
    '截断': new R.Builtin('截断', (s, width, ellipsis = '…') => {
      const text = String(s); const w = Math.trunc(Number(width)); const e = String(ellipsis);
      if (w < 0) throw R.typeErr(`「截断」的宽度不能是负数，得到了 ${R.display(width)}`);
      const chars = [...text];
      if (chars.length <= w) return text;
      const ech = [...e];
      if (w <= ech.length) return ech.slice(0, w).join('');
      return chars.slice(0, w - ech.length).join('') + e;
    }),
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
  '路径': new R.Module('路径', PATH_FUNCS),
  '文件': new R.Module('文件', FILE_FUNCS),
  '日期': new R.Module('日期', DATE_FUNCS),
  '时间': new R.Module('时间', TIME_FUNCS),
  '加密': new R.Module('加密', CRYPTO_FUNCS),
  '压缩': new R.Module('压缩', ZIP_FUNCS),
  '网络': new R.Module('网络', NET_FUNCS),
  // M41 新增的四个模块（与 Python 侧 jishi/stdlib/ 下同名文件对齐）。
  // 注意模块名是「容器」而不是「集合」——`集合` 是内建函数名，
  // 同名模块会把内建遮蔽掉（见 Python 侧 容器.py 的模块说明）。
  '容器': new R.Module('容器', CONTAINER_FUNCS),
  '迭代': new R.Module('迭代', ITER_FUNCS),
  '参数': new R.Module('参数', ARG_FUNCS),
  '日志': new R.Module('日志', LOG_FUNCS),
  // M50 新增的六个模块（与 Python 侧 jishi/stdlib/ 下同名文件对齐）
  '统计': new R.Module('统计', STATS_FUNCS),
  '编码': new R.Module('编码', ENCODE_FUNCS),
  '配置': new R.Module('配置', CFG_FUNCS),
  'html': new R.Module('html', HTML_FUNCS),
  '对比': new R.Module('对比', DIFF_FUNCS),
  '标识': new R.Module('标识', ID_FUNCS),
};

function gcd(a, b) { while (b) { [a, b] = [b, a % b]; } return a; }

module.exports = { VM, Op, STDLIB };
