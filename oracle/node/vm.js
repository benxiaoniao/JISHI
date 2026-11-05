// -*- coding: utf-8 -*-
// 基石字节码虚拟机的指令分发（M13.2）。
//
// 消费 JSON 字节码（M13.1 反序列化后的结构），逐条解释指令。
// 语义委托 runtime.js，与 Python 侧 jishi/vm.py 严格对齐。

'use strict';

const R = require('./runtime');
const fs = require('fs');
// M119：GBK **编码**表（生成的，见 tools/gen_gbk_table.py）。
// Node 只有 TextDecoder('gbk') 能解；编码方向没有——`new TextEncoder('gbk')`
// 不报错但永远按 UTF-8 编，那是**静默错**，比报错还坏。
const _gbk表 = require('./gbk_table');
let _gbk反查 = null;
function gbk反查() {
  if (_gbk反查) return _gbk反查;
  const m = new Map();
  for (let i = 0; i < _gbk表.串.length; i++) {
    const c = _gbk表.串[i];
    if (c !== '\u0000') m.set(c, _gbk表.槽位码号(i));
  }
  _gbk反查 = m;
  return m;
}

const MAX_FRAMES = 1000;

// 「宽字符」/「零宽」区间 —— 必须与 Python 侧 `_宽区间` 和 Rust 的 `WIDE_RANGES`
// 逐条一致（判据 tests/test_m106_display_width.py 会比对三处实现）。
const WIDE_RANGES = [
  [0x1100, 0x115f], [0x2e80, 0x303e], [0x3041, 0x33ff], [0x3400, 0x4dbf],
  [0x4e00, 0x9fff], [0xa000, 0xa4cf], [0xac00, 0xd7a3], [0xf900, 0xfaff],
  [0xfe30, 0xfe6f], [0xff00, 0xff60], [0xffe0, 0xffe6],
  [0x1f300, 0x1f64f], [0x1f900, 0x1f9ff],
  [0x20000, 0x2fffd], [0x30000, 0x3fffd],
];
const ZERO_RANGES = [
  [0x0300, 0x036f], [0x0483, 0x0489], [0x200b, 0x200f], [0xfe00, 0xfe0f],
];

function charWidth(cp) {
  for (const [lo, hi] of ZERO_RANGES) if (lo <= cp && cp <= hi) return 0;
  for (const [lo, hi] of WIDE_RANGES) if (lo <= cp && cp <= hi) return 2;
  return 1;
}

/** 这段文本在等宽终端里占几列 —— 一个汉字 2 列、组合符 0 列。 */
function displayWidth(s) {
  let n = 0;
  for (const ch of String(s)) n += charWidth(ch.codePointAt(0));
  return n;
}

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
  constructor(catchIp, finallyIp, sp, seq, regionEnd) {
    this.catchIp = catchIp; this.finallyIp = finallyIp;
    this.sp = sp; this.seq = seq;
    //: 捕获区右边界（M148）：整条 `尝试` 语句结束的位置（`SETUP_TRY` 的 c）。
    this.regionEnd = regionEnd;
  }
}

class Frame {
  constructor(code, ip, base, locals, cells, callLine, callCol, inTrace) {
    this.code = code;
    this.ip = ip;
    this.base = base;
    this.locals = locals;
    this.cells = cells;
    this.loops = [];       // [depth, cont, brk, seq]
    this.handlers = [];    // Handler[]
    //: 「正在处理的异常」（M148）：`[异常, 左界, 右界]`。判有效性只看 ip。
    this.handled = [];
    this.pendingReturn = R.PENDING_NONE;
    this.blockSeq = 0;
    //: **本帧是在哪被调用的**（R6.4）—— 调用链要的是这个，不是「当前跑到第几行」。
    //: 口径照 Python `vm._frames_to_trace`（每帧记 `call_line`）；模块主帧没有
    //: 调用点，也不该进链。见 `_snapshotTrace`。
    this.callLine = callLine ?? null;
    this.callCol = callCol ?? null;
    //: **这帧进不进调用链**（D74 §三收口）：只有直接函数调用才进；方法 / 类实例化
    //: / 内建回调不进 —— 对齐 Python 侧「只有 UserFunction 直接调用进 _call_stack」。
    this.inTrace = inTrace === true;
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
    const frame = new Frame(code, 0, base, locals, frameCells, line, col, false);
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
      // ⚠️ 带一句「它的参数是：…」（R6.4，照 Python 的 `interpreter._do_call` /
      // `vm`）：只列**普通位置参数**（`*参数` 之前的那些），别把 `*余`/`**选项`
      // 也列进去 —— 那不是用户能按名字传的。
      else throw R.typeErr(`函数「${code.name}」没有叫「${k}」的参数`, line, col,
                           `它的参数是：${params.slice(0, npos).join('、')}`);
    }
    if (dstarPos >= 0) bound.set(params[dstarPos], extra);
    // 默认值：**紧凑尾部** —— 尾部 `defaults.length` 个「普通参数」有默认值，
    // 按下标折算。⚠️ 不能写 `defaults[i] != null` 那套（也就是**别**拿 `null`
    // 当哨兵）：默认值本身可能就是 `空`，会被误判成「没有默认值」而报缺少参数
    // （2026-10-11 修；C VM 一直按下标算）。
    if (defaults) {
      const firstDef = npos - defaults.length;
      for (let i = Math.max(firstDef, 0); i < npos; i++) {
        const p = params[i];
        if (!bound.has(p)) bound.set(p, defaults[i - firstDef]);
      }
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
            // 未赋值的局部槽：走**统一文案**（R6.4，Python 侧是
            // `vm._unbound` → `runtime.unbound_local_error`，三个执行器共用）。
            // 以前这里报「找不到名字「X」」+ 类型「异常」，两处都不对。
            if (v === R.UNSET) {
              // ⚠️ **按槽位的具体提示**（R6.6，字节码 v3）：外层也有同名变量时，
              // Python 侧给的是「外层也有一个叫「X」的变量…」（编译器按槽位
              // 存进 `Code.local_hints`）—— 以前这份数据**不在字节码 JSON 里**，
              // 宿主只能说通用那句，同一个错误两处提示不同。
              throw R.unboundErr(
                ins.a < localNames.length ? localNames[ins.a] : '$' + ins.a,
                ins.line, ins.col,
                (code.localHints || {})[ins.a]);
            }
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
            // M56：找不到时带上「你是不是想写「X」？」（只在报错路径上算候选，
            // 正常路径仍是 `name in globs` 一次哈希查找，零开销）
            if (!(name in globs)) {
              throw R.nameErr(`找不到名字「${name}」`, ins.line, ins.col,
                              R.nameHint(name, globs));
            }
            stack.push(globs[name]);
          } else if (op === Op.STORE_GLOBAL) {
            globs[names[ins.a]] = stack.pop();
          } else if (op === Op.LOAD_DEREF) {
            const v = frameCells[ins.a].value;
            if (v === R.UNSET) {
              throw R.unboundErr(this._cellName(code, ins.a), ins.line, ins.col);
            }
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
              // 🔴 存**紧凑尾部**，**不要** pad 成与形参表等长 —— 以前用
              // `null` 占位，与「默认值是 `空`」撞车（`f(a, b = 空)` 省略 b
              // 会误报缺少参数）。绑定侧按 `npos - defaults.length` 折算。
              defaults = stack.splice(stack.length - ndefaults, ndefaults);
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
            // ins.c = 别名下标（-1 = 没写 `为 别的名字`），见 `_doImport` 的说明
            stack.push(this._doImport(names[ins.a], src, ins.line, ins.col, ins.c));
          } else if (op === Op.STORE_LAST) {
            this.lastValue = stack.pop();
          } else if (op === Op.SETUP_TRY) {
            frame.blockSeq += 1;
            frame.handlers.push(new Handler(ins.a, ins.b, stack.length, frame.blockSeq, ins.c));
          } else if (op === Op.POP_TRY) {
            if (frame.handlers.length) frame.handlers.pop();
          } else if (op === Op.THROW) {
            const v = stack.pop();
            frame.ip = ip;
            // 「正在处理的异常」（M148）：ip 此刻已指向这条 THROW，拿它比捕获区
            throw R.makeException(v, ins.line, ins.col, this._findHandled());
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
        // 出错的就是**刚跑过的那条**指令：`ip` 进入本轮时已 `+1`，所以是 `ip - 1`。
        // （原来这里用的是上一轮写回帧里的 `frame.ip`，会偏一条。）
        const badIns = instrs[Math.max(0, Math.min(ip - 1, instrs.length - 1))];
        if (!(exc instanceof R.JishiError)) {
          // JS 原生异常：包装成基石异常
          exc = R.runErr(String(exc && exc.message ? exc.message : exc),
                         badIns ? badIns.line : null, badIns ? badIns.col : null);
        }
        this._fillErrorLoc(exc, badIns);
        // 报错渲染要打 `┌─ 文件:行:列`，还要按它去读源文件画源码行 —— 宿主
        // 得知道自己在跑哪个文件（R6.4，与 Rust 的 `Module.filename` 同义）。
        if (!exc.filename) exc.filename = this.filename;
        frame.ip = ip;
        // 拍调用链的时机与 Python 侧一样：**错误第一次冒出来那一刻**
        // （再往外抛的路上帧早就被弹干净了）。见 `_snapshotTrace`。
        this._snapshotTrace(exc);
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
      this.frames.push(new Frame(sub, 0, fnBase, locals, subCells, ins.line, ins.col, true));
      return null;
    }

    stack.length = fnBase;
    stack.push(this._callValue(fn, args, kwargs, ins.line, ins.col));
    return true;
  }

  _callValue(fn, args, kwargs, line, col) {
    if (fn instanceof R.Builtin) {
      // ⚠️ **参数个数检查**（R6.4）：标准库是固定形参，多给的实参以前被
      // 静默忽略、少给则拿到 `undefined`。查的是 `node/stdlib_sigs.js`
      // 那张由 Python 侧生成的签名表（内建不走这条路）。
      // ⚠️ 传的是**位置 + 命名参数的总数**：`文本.截断("很长的话", 宽度=6)` 只给了
      // 1 个**位置**参数，但 `宽度` 是 positional-or-keyword、按名字传等价于按位置传
      // ⇒ 实际收了 2 个。只数位置参数会误报（这条踩过）。
      // ⚠️ 与 Python 侧的口径**刻意不同**：那边要**排除 keyword-only** 的命名参数
      // （`json.dumps(对象, 缩进=2)` 的 `缩进` 落不到位置参数上）；宿主这边
      // `SIGS` 的参数名表本来就含 keyword-only 的名字、上限宽松，所以直接加即可。
      R.needStdlibArgs(fn.name, args.length + Object.keys(kwargs || {}).length,
                       line, col);
      // 内建 / 标准库是**按位置**调用的 JS 函数：命名参数在这里映射回位置
      // 参数（M51）。以前是 `fn.fn(...args)` 直接把 kwargs 丢掉 ——
      // `加密.摘要("abc", 算法="md5")` 会悄悄用默认的 sha256。
      return fn.fn(...R.mapKwargs(fn.name, args, kwargs, line, col));
    }
    if (fn instanceof R.Module) {
      // M152：与 Python `runtime.call_value` / Rust `CALL` 同一句话、同一个码
      // （`找不到这个函数` E0302）。以前 Node 报的是「类型错误」E2002，
      // 三个宿主三个码 —— 用户拿 A 机器的报错去查，在 B 上对不上。
      throw R.funcErr(
        `模块「${fn.name}」不能直接调用，要用「${fn.name}.函数名(...)」`,
        line, col,
        '模块名后面要接 `.函数名`；如果这个模块名原本是个内建函数'
        + '（比如 `文本`），用别名导入：导入 文本 为 文本库');
    }
    if (fn instanceof R.JishiClass) {
      this._rejectKwargs(`类「${fn.name}」`, kwargs, line, col);
      return R.makeInstance(fn, args, line, col);
    }
    if (fn instanceof R.BoundMethod) {
      this._rejectKwargs(`「${R.display(fn.obj)}」的方法`, kwargs, line, col);
      // ⚠️ **报错位置用方法值携带的那一对**（取属性时记下的，见 `getAttr`）——
      // 与 Python 的 `_BoundMethod.__call__` 同源：方法内部的错落在 `X.方法`
      // 上（`a.追加()` 指第 2 列那个 `.`），而不是调用点（`(` 那一列）。
      // 以前一路用的是指令位置 → 四种方法相关的错都比 Python 差三列（R6.6）。
      // 模块属性（`数.开方`）不是 `BoundMethod`，仍走指令位置 —— 那边 Python
      // 也用调用点，两边本来就一致。
      const ml = fn.line != null ? fn.line : line;
      const mc = fn.col != null ? fn.col : col;
      return fn.impl(fn.obj, args, ml, mc);
    }
    if (fn instanceof R.BoundUserMethod) {
      return this.callFunction(fn.fn, [fn.instance, ...args], kwargs, line, col);
    }
    if (fn instanceof R.ExcType) {
      this._rejectKwargs(`异常类型「${fn.name}」`, kwargs, line, col);
      // ⚠️ **不带位置**（M148 对齐）：Python 的 `ExcType.__call__` 是
      // `self.cls(message)` —— 造出来的实例没有行列，位置等 `抛出` 时才补。
      // 宿主以前把 CALL 指令的 line/col 塞进去，于是 `抛出 值错误("x")`
      // 报在**括号上**（如 8:15），Python 报在 `抛出`（8:9）。
      return fn.make(args[0] !== undefined ? args[0] : '');
    }
    // Python 侧是 `RunNotCallableError`（E2006「这个东西不能调用」）——
    // 以前这里报宽泛的「运行期错误」，`e.类型` 与错误码都跟着漂（R6.4）
    throw R.notCallableErr(`「${R.display(fn)}」不能调用`, line, col);
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

  /**
   * 给错误补上「出错位置」（R6.4）。
   *
   * 与 Python 的 `errors.py`/`vm` 同义：**只填还没填的** —— 内层若已给出更准的
   * 位置（内建/标准库手里有调用点），不要被外层覆盖。
   */
  _fillErrorLoc(exc, ins) {
    if (!ins || exc.line != null) return;
    exc.line = ins.line;
    exc.col = ins.col;
  }

  /**
   * 把当前帧栈拍成调用链（只拍一次；已经有链就不动）。
   *
   * ⚠️ 口径照 Python 的 `vm._frames_to_trace`（否则同一个错误在两个执行器里
   * 回溯长得不一样）：
   * * **跳过 frames[0]**（模块主帧 —— 它的名字是 `<模块>`，不是用户写的函数）；
   * * 每帧记「函数名 + **它的调用点**」（`callLine`），不是「当前跑到第几行」；
   * * 匿名函数（`函数(x)：…` 脱糖出来的）的 code 名以 `<` 开头，
   *   报错里显示成「匿名函数」更好认；
   * * 调用点是 0（没记到）就当 `null`（Python 是 `fr.call_line or None`）。
   */
  _snapshotTrace(exc) {
    if (exc.trace) return;
    const chain = [];
    for (let i = 1; i < this.frames.length; i++) {
      const fr = this.frames[i];
      if (!fr.inTrace) continue;   // 方法 / 类实例化 / 内建回调不进链（D74 §三收口）
      const nm = fr.code && fr.code.name;
      chain.push({
        func: (nm && nm.startsWith('<')) ? '匿名函数' : nm,
        line: fr.callLine > 0 ? fr.callLine : null,
      });
    }
    exc.trace = chain;
  }

  /** 记一笔「本帧正在处理 exc」（M148）。与 Python `_note_handled` 逐条对齐。 */
  _noteHandled(frame, h, exc) {
    const lo = h.catchIp;
    const hi = h.regionEnd;
    if (hi == null || hi <= 0) return;    // 老字节码没写 c：宁可不记，也不挂错
    frame.handled = frame.handled.filter(
      ([, l, hh]) => l <= lo && lo < hh && !(l === lo && hh === hi));
    frame.handled.push([exc, lo, hi]);
  }

  /** `抛出` 的那一刻，「正在被处理的异常」是谁（M148）：从最内层帧往外找。 */
  _findHandled() {
    for (let i = this.frames.length - 1; i >= 0; i--) {
      const frame = this.frames[i];
      const ip = frame.ip;
      for (let j = frame.handled.length - 1; j >= 0; j--) {
        const [exc, lo, hi] = frame.handled[j];
        if (lo <= ip && ip < hi) return exc;
      }
    }
    return null;
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
        this._noteHandled(frame, h, exc);
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
  _doImport(name, src, line, col, aliasIdx) {
    // src: 0=标准库 1=python 2=本地包
    if (src === 1) throw R.runErr(`JS 引擎不支持「从 python」导入「${name}」`, line, col);
    if (src === 2) throw R.runErr(`JS 引擎不支持「从 本地包」导入「${name}」`, line, col);
    const mod = STDLIB[name];
    if (!mod) {
      throw R.funcErr(`没有找到标准库模块「${name}」`, line, col,
                      '试试：导入 随机 / 导入 数学 / 导入 时间 / 导入 文本');
    }
    // 有的模块要求「每次导入都是干净的一份」（比如 `测试` 的用例记录）
    if (mod.onImport) mod.onImport();
    // `aliasIdx` 为空/-1 ⇒ 没写别名 ⇒ 才可能遮蔽内建（口径见 `_提示遮蔽内建`）
    if (aliasIdx === undefined || aliasIdx === null || aliasIdx < 0) {
      _提示遮蔽内建(name);
    }
    return mod;
  }
}

// ---------------------------------------------------------------------------
// 「模块名遮蔽内建」提示（M138）
//
// `导入 文本` 会把名字「文本」绑到模块上，内建转换函数 `文本(...)` 就用不了了。
// 命中范围极小：模块名 ∩ 内建函数名 = {文本}（其余 29 个都不撞）。
//
// ⚠️ 口径与 Python 侧 `runtime._warn_shadow_builtin`、Rust 侧 `提示遮蔽内建`
//    三者**逐字对齐**：一个进程里每个名字只打一次；用了别名导入就不打。
//    （`check_engines` 把这条提示当「进程模型的副产物」剥掉，不参与语义对拍；
//     但**用户看得见**，所以三个宿主都得有 —— 否则只有 Python 版会提醒。）
// ---------------------------------------------------------------------------
const _已提示遮蔽 = new Set();
let _内建名集 = null;

function _提示遮蔽内建(name) {
  if (_内建名集 === null) _内建名集 = new Set(Object.keys(R.newBuiltins()));
  if (!_内建名集.has(name)) return;          // 与 Python 一样：先看是不是内建
  if (_已提示遮蔽.has(name)) return;         // 一个进程里只打一次
  _已提示遮蔽.add(name);
  process.stderr.write(
    `提示：「${name}」既是标准库模块名，又是内建函数名。`
    + `导入后，内建的 ${name}(...) 会被模块「${name}」遮蔽。`
    + `如需两者都用，建议用别名导入：导入 ${name} 为 别的名字\n`);
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
 * 数模板要几个**位置参数**（`{}` / `{0}`）；数不清返回 `null`（M152）。
 * 规则与 Python `_占位符个数`、Rust `占位符个数` **必须一致**：
 * `{{` / `}}` 是字面花括号；命名占位符、花括号不配对都不做个数检查。
 */
function 占位符个数(tpl) {
  let n = 0;
  const s = String(tpl);
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (c === '{') {
      if (s[i + 1] === '{') { i += 1; continue; }
      let j = i + 1;
      while (j < s.length && s[j] !== '}') {
        if (s[j] === '{') return null;          // 嵌套 / 坏模板
        j += 1;
      }
      if (j >= s.length) return null;           // 没配对的 `{`
      const inner = s.slice(i + 1, j);
      const colon = inner.indexOf(':');
      const field = (colon >= 0 ? inner.slice(0, colon) : inner).trim();
      if (field === '') n += 1;
      else if (/^\d+$/.test(field)) n = Math.max(n, Number(field) + 1);
      else return null;                         // 命名占位符
      i = j;
    } else if (c === '}') {
      if (s[i + 1] === '}') { i += 1; continue; }
      return null;                              // 单个 `}`：模板坏了
    }
  }
  return n;
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
    // ⚠️ **大小护栏**（D77 §二）：字典在遍历中被改大小 → 规范（Python）会报错，
    // 而键快照会让这件事静默过去（悄悄用旧键继续跑）。每步核对一次 size。
    const expect = obj.size;
    let i = 0;
    it = { next: () => {
      if (obj.size !== expect) throw R.iterSizeErr(line, col);
      return i < keys.length ? { value: keys[i++], done: false } : { done: true };
    } };
  } else if (typeof obj === 'string') {
    const chars = [...obj];
    let i = 0;
    it = { next: () => i < chars.length ? { value: chars[i++], done: false } : { done: true } };
  } else if (R.isBytes(obj)) {
    // 字节（M113）：逐个给**整数** 0-255。不可变，快照即可（D79 §三）。
    const arr = [...obj.buf];
    let i = 0;
    it = { next: () => i < arr.length ? { value: arr[i++], done: false } : { done: true } };
  } else if (R.isFile(obj)) {
    // 文件对象按行迭代（M26）：真句柄，大文件不必整个读进内存
    it = obj[Symbol.iterator]();
  } else if (obj instanceof R.JishiSet) {
    // 集合迭代自己的元素（M30）。JishiSet 的迭代器直接来自内部 Map，
    // 顺序就是插入顺序，与 Python 侧一致。
    // ⚠️ **大小护栏**（D77 §二）：遍历中改集合大小 → 规范会报错（同字典）。
    const inner = obj[Symbol.iterator]();
    const expect = obj.map.size;
    it = { next: () => {
      if (obj.map.size !== expect) throw R.iterSizeErr(line, col);
      return inner.next();
    } };
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
// 🔴 M161：同步 HTTP 走 **Worker + Atomics**，不再 spawn 自己（见 `netRequest` 上方那段）。
const _worker = require('worker_threads');

/// 跑一条命令并**分开**收两条流（`执行详细` / `并行执行` 共用）。
///
/// 数组形式走 `spawnSync(程序, 参数)`（**不经过 shell**，参数边界原样保留），
/// 文本形式走 shell —— 与 Python 侧 `subprocess.run(list)` / `shell=True` 的分工一致。
function _跑详细(cmd, timeout) {
  const 毫秒 = Math.max(100, Number(timeout) * 1000);
  const 选项 = {
    timeout: 毫秒,
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  };
  const r = Array.isArray(cmd)
    ? _cp.spawnSync(String(cmd[0]), cmd.slice(1).map((x) => String(x)), 选项)
    : _cp.spawnSync(String(cmd), Object.assign({ shell: true }, 选项));
  if (r.error) {
    if (r.error.code === 'ETIMEDOUT') {
      // 文案与 Python / Rust 逐字一致（`10.0` 要打成 `10`，所以两边都用最短形式）
      throw R.runErr(`命令执行超过 ${Number(timeout)} 秒，已终止`);
    }
    throw R.runErr(`执行命令失败：${r.error.message}`);
  }
  return {
    输出: r.stdout || '',
    错误: r.stderr || '',
    退出码: r.status === null ? -1 : r.status,
  };
}


const SYS_FUNCS = {
  // M120：逻辑核数（含超线程）。容器里可能比宿主机少 —— 数到多少报多少。
  'CPU核数': new R.Builtin('CPU核数', () => {
    const n = _os.cpus().length;
    return n > 0 ? n : 1;
  }),
  '系统名': new R.Builtin('系统名', () => {
    const p = _os.platform();
    if (p === 'win32') return 'Windows';
    if (p === 'darwin') return 'Darwin';
    return 'Linux';
  }),
  '系统版本': new R.Builtin('系统版本', () => _os.release()),
  '机器架构': new R.Builtin('机器架构', () => {
    // 口径要与 Python 的 `platform.machine()` **逐字一致**（五执行器对拍会抓到）：
    //   * Windows：`platform.machine()` 取 `PROCESSOR_ARCHITECTURE` → `AMD64`；
    //   * POSIX：  取 `uname -m` → `x86_64`（macOS 上 `arm64` / `x86_64`）。
    // 🔴 **不能用 `os.arch()`**：它给的是 `x64` / `arm64`，在 Linux 上对不上
    //    `x86_64`（2026-10-10 被 `tools/probe_stdlib.py` 在 CI 上抓到）。
    // 🔴 **也不能在 Windows 上用 `os.machine()`**：Windows 上它给 `x86_64`，
    //    而 Python 给 `AMD64` —— 会把 Windows 弄坏（本机实测）。
    // `os.machine()` 需要 Node ≥ 18.9，POSIX 上取不到时退回 `os.arch()`。
    const a = _os.arch();
    if (process.platform === 'win32') {
      if (a === 'x64') return 'AMD64';
      if (a === 'ia32') return 'x86';
      if (a === 'arm64') return 'ARM64';
      return a;
    }
    return typeof _os.machine === 'function' ? _os.machine() : a;
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
  // M133：**两条流分开**返回（`执行` 是老接口，只给 stdout）。
  // ⚠️ 本宿主用 `spawnSync` 拿两个流，**不需要「保序合并」** —— 所以这里能
  //    和 Python/Rust 完全一致（`执行` 那条做不到，见上面模块注释）。
  '执行详细': new R.Builtin('执行详细', (cmd, timeout = 10.0) => {
    const r = _跑详细(cmd, timeout);
    const m = new Map();
    m.set('输出', r.输出); m.set('错误', r.错误); m.set('退出码', r.退出码);
    return m;
  }),
  // M134：**同时**跑一批命令，**结果顺序 = 输入顺序**。
  // ⚠️ 本宿主**顺序执行**（同步 VM 没有事件循环可以泵）—— 与 `并发` 模块同一条
  //    边界：结果一模一样，只是不重叠等待。语义照旧（保序、首个错误胜出）。
  '并行执行': new R.Builtin('并行执行', (cmds, threads = 4, timeout = 10.0) => {
    _并发线程数(threads);                     // 只为校验（本宿主顺序执行，用不到这个数）
    const 项们 = _并发列出(cmds, '「并行执行」的第一个参数（命令们）');
    return 项们.map((c) => {
      const r = _跑详细(c, timeout);
      const m = new Map();
      m.set('输出', r.输出); m.set('错误', r.错误); m.set('退出码', r.退出码);
      return m;
    });
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
  '打开': new R.Builtin('打开', (p) => {
    // 目标不存在（且不是网址）→ 假、什么都不启动（五个引擎一致的规矩）
    const 目标 = pText(p);
    if (!目标.includes('://') && !fs.existsSync(目标)) return false;
    const cmd = process.platform === 'win32' ? 'cmd'
      : (process.platform === 'darwin' ? 'open' : 'xdg-open');
    const args = process.platform === 'win32' ? ['/C', 'start', '', 目标] : [目标];
    try {
      const r = _cp.spawnSync(cmd, args, { stdio: 'ignore' });
      return !r.error;
    } catch (e) { return false; }
  }),
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
    if (R.isBytes(x)) {
      // M113：JSON 没有二进制，字节必须显式转文本（D79 §三）
      throw R.valueErr('JSON 里放不了字节', undefined, undefined,
        'JSON 只有文本，先把字节转成文本再放，比如 编码.base64编码(字节)');
    }
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
    // M155：与 Python / Rust 同一句话（各自的细节是英文/各家不一，统一去掉）。
    throw R.valueErr('不是有效的 JSON 文本', line, col);
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
  // M120：只问「是不是合法 JSON」。口径与 `解析` **同一个解码器**
  // ⇒ 不会出现「有效说假、解析却成功」。Node 的 JSON.parse 本来就不收
  // `NaN`/`Infinity`，但 Python 的 `json.loads` 收 —— M120 已把两边都改成拒绝。
  '有效': new R.Builtin('有效', (text) => {
    if (typeof text !== 'string') {
      throw R.typeErr(`「有效」要传文本（现在是「${R.pyStr(text)}」）`);
    }
    try {
      jsonDecode(text);
    } catch (e) {
      return false;
    }
    return true;
  }),
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
  // ⚠️ 空 / 布尔要用**基石的显示**（`空` / `真`）而不是 `''` / `'true'`（M157）：
  //    非数字那一支是按字符串比大小的，写法不同 ⇒ 同一列排出来的顺序就不同。
  const s = (v === null || v === undefined) ? '空' : String(v);
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
  // 🔴 M120：`转文本` 给人看（对齐表），`转TSV` 给程序读。两者末尾都**不带换行**。
  '转文本': new R.Builtin('转文本', (t) => {
    if (!(t !== null && typeof t === 'object' && t[TABLE] === true)) {
      throw R.typeErr(`「转文本」要传一个表格（现在是「${R.pyStr(t)}」）`
        + '—— 用 表格.读表格 或 表格.从记录 得到');
    }
    return 转对齐文本(t);
  }),
  '转TSV': new R.Builtin('转TSV', (t) => {
    if (!(t !== null && typeof t === 'object' && t[TABLE] === true)) {
      throw R.typeErr(`「转TSV」要传一个表格（现在是「${R.pyStr(t)}」）`
        + '—— 用 表格.读表格 或 表格.从记录 得到');
    }
    return 转TSV文本(t);
  }),
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
      // ⚠️ M157：`null`（空单元格）**必须挡**。`Number(null)` 是 0，
      //    以前的判断只看 `'' / undefined / NaN`，于是「缺考」被当成 0 分
      //    静默算进平均 —— 而另外四个执行器都报错。**静默的错答案**最坏。
      if (row[i] === null || row[i] === '' || row[i] === undefined || Number.isNaN(n)) {
        throw R.typeErr(`列「${列名}」里有不是数字的值「${R.display(row[i])}」，没法汇总`);
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
  // M106 补：`分组汇总` / `连接`（与 Python 侧 `表格.py` 同行为）
  '分组汇总': new R.Builtin('分组汇总', (t, 分组列, 值列) => {
    const tab = asTable(t);
    const ig = colIndex(tab, 分组列);
    const iv = colIndex(tab, 值列);
    const buckets = new Map();
    for (const row of tab.rows) {
      const cell = row[iv];
      const n = Number(cell);
      // ⚠️ M157：`null` 同 `汇总` —— 缺考的格子不能被当成 0 分。
      if (cell === null || cell === '' || cell === undefined || Number.isNaN(n)) {
        throw R.typeErr(`列「${值列}」里有不是数字的值「${R.display(cell)}」，没法汇总`);
      }
      const k = R.setKey(row[ig]);
      if (!buckets.has(k)) buckets.set(k, { k: row[ig], vals: [] });
      buckets.get(k).vals.push(n);
    }
    const out = new Map();
    for (const { k, vals } of buckets.values()) {
      const total = vals.reduce((a, b) => a + b, 0);
      out.set(k, new Map([
        ['个数', vals.length],
        ['总和', new R.FloatBox(total)],
        ['平均', new R.FloatBox(total / vals.length)],
        ['最大', new R.FloatBox(Math.max(...vals))],
        ['最小', new R.FloatBox(Math.min(...vals))],
      ]));
    }
    return out;
  }),
  '连接': new R.Builtin('连接', (a, b, 键甲, 键乙) => {
    const A = asTable(a);
    const B = asTable(b);
    const ia = colIndex(A, 键甲);
    const ib = colIndex(B, 键乙 === undefined ? 键甲 : 键乙);
    const idx = new Map();                 // 乙表按键索引（同一个键只留第一条）
    B.rows.forEach((row, j) => {
      const k = R.setKey(row[ib]);
      if (!idx.has(k)) idx.set(k, j);
    });
    const headers = [...A.headers];
    B.headers.forEach((h, j) => { if (j !== ib) headers.push(h); });
    const rows = [];
    for (const row of A.rows) {
      const k = R.setKey(row[ia]);
      if (idx.has(k)) {
        const other = B.rows[idx.get(k)];
        rows.push([...row, ...other.filter((_, m) => m !== ib)]);
      }
    }
    return makeTable(headers, rows);
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
  // M107：字典列表 ↔ 表格（JSON 接口回来的数据直接能做表格运算）
  '从记录': new R.Builtin('从记录', (记录们) => {
    const recs = R.asIterable(记录们, '「从记录」的内容');
    const cols = [];
    const dicts = [];
    for (const rec of recs) {
      if (!(rec instanceof Map)) {
        throw R.typeErr(`「从记录」的每一项都要是字典，遇到了「${R.display(rec)}」`);
      }
      for (const k of rec.keys()) {
        if (!cols.includes(k)) cols.push(k);
      }
      dicts.push(rec);
    }
    const rows = dicts.map((d) => cols.map((c) => (d.has(c) ? d.get(c) : null)));
    return makeTable(cols.map((c) => String(c)), rows);
  }),
  '转记录': new R.Builtin('转记录', (t) => {
    const tab = asTable(t);
    return tab.rows.map((row) => {
      const d = new Map();
      tab.headers.forEach((h, i) => d.set(h, row[i]));
      return d;
    });
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

/** 值 → Buffer（HMAC / 口令摘要 的键、消息、盐用；字节原样）。 */
function toBuf(v) {
  if (R.isBytes(v)) return v.buf;
  return Buffer.from(pText(v), 'utf8');
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
// -- 通配匹配 / 唯一名（M108：路径.按模式找 · 路径.临时目录 / 文件.原子写）--
//
// 通配规则**五个引擎共用同一套**（见 Python 侧 jishi/stdlib/路径.py 里
// `_解析集合` 上面那段）：`*` / `?` / `[abc]` / `[a-z]` / `[!abc]`（`^` 不是取反）；
// 未闭合的 `[` 按普通字符；其余按字面（`\` **不是**转义符）；**区分大小写**。
// 不套各语言的正则 / glob —— 它们在细节上对不齐，会分叉。
//
// ⚠️ 用 `Array.from` 取**码点**（不是 UTF-16 码元），与 Python / Rust 的
// 「按字符」口径一致（否则 emoji 这类代理对会被拆开）。
function globMatch(pattern, name) {
  const p = Array.from(pattern);
  const s = Array.from(name);
  let pi = 0;
  let si = 0;
  let star = -1;      // 最近一个 `*` 在模式里的位置
  let mark = 0;       // 那个 `*` 之后、名字里已经被吃掉的位置
  while (si < s.length) {
    let ate = false;
    if (pi < p.length) {
      const c = p[pi];
      if (c === '*') { star = pi; mark = si; pi += 1; ate = true; }
      else if (c === '?') { pi += 1; si += 1; ate = true; }
      else if (c === '[') {
        const set = parseSet(p, pi);
        if (set === null) {
          if (s[si] === '[') { pi += 1; si += 1; ate = true; }
        } else {
          const hit = set.items.some(([a, b]) => a <= s[si] && s[si] <= b);
          if (hit !== set.neg) { pi = set.next; si += 1; ate = true; }
        }
      } else if (c === s[si]) { pi += 1; si += 1; ate = true; }
    }
    if (ate) continue;
    if (star >= 0) { pi = star + 1; mark += 1; si = mark; }
    else return false;
  }
  while (pi < p.length && p[pi] === '*') pi += 1;
  return pi === p.length;
}

function parseSet(p, start) {
  const n = p.length;
  let i = start + 1;
  let neg = false;
  if (i < n && p[i] === '!') { neg = true; i += 1; }
  const items = [];
  if (i < n && p[i] === ']') { items.push([']', ']']); i += 1; }
  while (i < n && p[i] !== ']') {
    if (i + 2 < n && p[i + 1] === '-' && p[i + 2] !== ']') {
      items.push([p[i], p[i + 2]]); i += 3;
    } else {
      items.push([p[i], p[i]]); i += 1;
    }
  }
  if (i >= n) return null;                     // 没有配对的 `]`
  return { neg, items, next: i + 1 };
}

//: 由系统保证唯一的一小段名字（进程号 + 毫秒 + 自增计数）
let _唯一计数 = 0;
function uniqueName() {
  _唯一计数 += 1;
  return `${process.pid}-${Date.now()}-${_唯一计数}`;
}

const PATH_FUNCS = {
  // M120：值本身平台相关（Windows `\`、其余 `/`），语料只能钉「长度 1 且是这两种之一」。
  '分隔符': new R.Builtin('分隔符', () => _path.sep),
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
  '递归删除': new R.Builtin('递归删除', (p) => {
    // 与 Python 的 `shutil.rmtree` / Rust 的 `remove_dir_all` 对齐：
    // 文件直接删、目录连树一起删；不存在给假。
    const target = pText(p);
    const st = statOf(target);
    if (!st) return false;
    if (st.isFile()) { fs.unlinkSync(target); return true; }
    fs.rmSync(target, { recursive: true, force: true });
    return true;
  }),
  '按模式找': new R.Builtin('按模式找', (p, pattern = '*', recursive = true) => {
    const dir = pText(p);
    const st = statOf(dir);
    if (!st || !st.isDirectory()) throw R.valueErr(`「${dir}」不是目录`);
    const pat = String(pattern);
    const out = [];
    const walk = (base, rel) => {
      for (const name of fs.readdirSync(base).sort(cmpCodepoint)) {
        const full = _path.join(base, name);
        const st2 = statOf(full);
        if (st2 && st2.isDirectory()) {
          if (recursive === true) walk(full, _path.join(rel, name));
          continue;
        }
        if (globMatch(pat, name)) out.push(_path.join(rel, name));
      }
    };
    walk(dir, '');
    return out.sort(cmpCodepoint);
  }),
  '系统临时目录': new R.Builtin('系统临时目录', () => normSep(_os.tmpdir())),
  '临时目录': new R.Builtin('临时目录', (prefix = '') => {
    // 建一个**唯一**临时目录并返回路径（对齐 `tempfile.mkdtemp`）
    const dir = _path.join(_os.tmpdir(), `${String(prefix)}${uniqueName()}`);
    fs.mkdirSync(dir);
    return normSep(dir);
  }),
  '临时文件': new R.Builtin('临时文件', (suffix = '') => {
    const f = _path.join(_os.tmpdir(), `${uniqueName()}${String(suffix)}`);
    fs.writeFileSync(f, '');
    return normSep(f);
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
  // M113：读整个文件成**字节**
  '读字节': new R.Builtin('读字节', (p) => {
    const target = pText(p);
    const st = statOf(target);
    if (!st || !st.isFile()) throw R.fileErr(`找不到文件「${target}」，没法读取`);
    return new R.JishiBytes(fs.readFileSync(target));
  }),
  '写字节': new R.Builtin('写字节', (p, data) => {
    if (!R.isBytes(data)) {
      throw R.fileErr(`「写字节」要传字节，得到了「${R.display(data)}」`);
    }
    fs.writeFileSync(pText(p), data.buf);
    return null;
  }),
  // 🔴 M118：`追加字节`。只收**字节**（M113 的类型），文本走 `追加文本`。
  '追加字节': new R.Builtin('追加字节', (p, data) => {
    if (!R.isBytes(data)) {
      throw R.fileErr(`「追加字节」要的是**字节**（现在是「${R.display(data)}」）；`
        + '文本请用 `追加文本`');
    }
    fs.appendFileSync(pText(p), data.buf);
    return data.buf.length;
  }),
  '复制目录': new R.Builtin('复制目录', (from, to) => {
    const src = pText(from);
    const dst = pText(to);
    const st = statOf(src);
    if (!st || !st.isDirectory()) throw R.fileErr(`「${src}」不是目录，没法复制`);
    if (fs.existsSync(dst)) {
      throw R.fileErr(`「${dst}」已经存在，没法复制目录过去（先 \`路径.递归删除\` 它）`);
    }
    // 🔴 **不用 `fs.cpSync`**（2026-10-18 实测）：它在受限/沙箱环境里会直接
    //    **访问违例崩掉进程**（rc=0xC0000005，连 stderr 都没有）—— 而手动递归
    //    （readdir + mkdir + copyFile）在同样的环境里正常。递归本身没几行，
    //    自己走反而更可控（出错时能说清楚是哪一层）。
    try {
      递归复制(src, dst);
    } catch (e) {
      throw R.fileErr(`复制目录「${src}」到「${dst}」失败：${e && e.message ? e.message : e}`);
    }
    return dst;
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
  '原子写': new R.Builtin('原子写', (p, content) => {
    // 先写**同目录**的临时文件再改名（同盘 rename 才是原子的）。对齐
    // Python 的 `os.replace` / Rust 的 `fs::rename`。
    const target = pText(p);
    const dir = _path.dirname(target) || '.';
    fs.mkdirSync(dir, { recursive: true });
    const 临时 = _path.join(dir, `${_path.basename(target)}.${uniqueName()}.tmp`);
    try {
      fs.writeFileSync(临时, pText(content), 'utf8');
      fs.renameSync(临时, target);
    } catch (e) {
      try { fs.unlinkSync(临时); } catch (e2) { /* 清不掉就算了 */ }
      throw R.fileErr(`写「${target}」失败：${e && e.message ? e.message : e}`);
    }
    return null;
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

function daysInMonth(y, m) {
  if (m === 2) return daysInYear(y) === 366 ? 29 : 28;
  return [1, 3, 5, 7, 8, 10, 12].includes(m) ? 31 : 30;
}

/** `加月` / `加年` 的公共实现：按**日历月**推，月末**收**到该月最后一天。 */
function shiftMonths(delta, text) {
  const d = parseDate(text);
  const total = d.getFullYear() * 12 + d.getMonth() + Math.trunc(Number(delta));
  // JS 的 `/` 是浮点除法、`%` 会留负号 ⇒ 用 floorMod 拿到「向下取整」那份
  const y = Math.floor(total / 12);
  const m = ((total % 12) + 12) % 12 + 1;
  const day = Math.min(d.getDate(), daysInMonth(y, m));
  return `${pad4(y)}-${pad2(m)}-${pad2(day)}`;
}

/** `解析ISO`：只取**字面日期**，不按后面的时区偏移换算（与另两处同一条规则）。 */
function parseIsoDate(v) {
  const s = String(pText(v)).trim();
  if (!/^\d{4}-\d{2}-\d{2}([T ]|$)/.test(s)) {
    throw R.valueErr(`「${pText(v)}」不是有效的 ISO 日期，`
      + "例如 2026-10-12 或 2026-10-12T08:30:00Z");
  }
  const y = Number(s.slice(0, 4));
  const m = Number(s.slice(5, 7));
  const da = Number(s.slice(8, 10));
  if (m < 1 || m > 12 || da < 1 || da > daysInMonth(y, m)) {
    throw R.valueErr(`「${pText(v)}」里的日期不存在（${y} 年 ${m} 月没有 ${da} 日）`);
  }
  return `${pad4(y)}-${pad2(m)}-${pad2(da)}`;
}

// -- 日期（M31）--
const DATE_FUNCS = {
  // M120：闰年规则按公历（1900 不是、2000 是）。**自己算**而不是
  // `new Date(y, m, 0)` —— 后者把 0..99 的年份当 19xx，是个静默坑。
  '月天数': new R.Builtin('月天数', (y, m) => {
    const 年 = Math.trunc(Number(R.unwrap(y)));
    const 月 = Math.trunc(Number(R.unwrap(m)));
    if (!(月 >= 1 && 月 <= 12)) throw R.valueErr(`月份要在 1~12 之间（现在是 ${月}）`);
    return 月天数(年, 月);
  }),
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
  '加月': new R.Builtin('加月', (n, text) => shiftMonths(n, text)),
  '加年': new R.Builtin('加年', (n, text) => shiftMonths(Number(n) * 12, text)),
  '解析ISO': new R.Builtin('解析ISO', (v) => parseIsoDate(v)),
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
  // M120：**偏移**而不是时区名（名字三家口径完全不同，永远对不齐）。
  // ⚠️ JS 的 `getTimezoneOffset()` 是「UTC - 本地」，东八区给 -480 ⇒ 要**取负**。
  '时区偏移': new R.Builtin('时区偏移', () => 格式化偏移(-new Date().getTimezoneOffset())),
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
  // 🔴 M165d：补上这个一直缺的函数（语言卡里早就列着它）。
  // 返回「宿主对象」（`JishiNative`），能 `.耗时()` / `.停()`，也能当上下文管理器。
  '计时': new R.Builtin('计时', (名字) => {
    if (名字 !== undefined && typeof 名字 !== 'string') {
      throw R.typeErr('「计时」的名字要传文本', 0, 0);
    }
    return R.nativeObj('计时器', TIMER_METHODS, {
      名字: 名字 === undefined ? '' : 名字,
      起: process.hrtime.bigint(),
      止: null,
      已打印: false,
    });
  }),
};

// -- 计时器的方法表（M165d）--
// 与 Rust 的 `计时器方法` 一一对应：进入 / 耗时 / 停 / 退出。
// ⚠️ `进入` 必须返回**自身**（Python 的 `__enter__`）：`用 … 为 t：` 绑的是它的返回值。
// 参数个数检查：文案与 `runtime.js` 的 `needArgs` **逐字一致**
// （它没导出，vm.js 里另写一份 —— 但文案必须一样，否则对拍会红）
function _methodArity(name, args, lo, hi, line, col) {
  if (args.length < lo || args.length > hi) {
    const need = lo === hi ? `${lo} 个` : `${lo} 到 ${hi} 个`;
    throw R.typeErr(`方法「${name}」需要 ${need}参数，但传了 ${args.length} 个`, line, col);
  }
}

function _timerSeconds(obj) {
  const 止 = obj.state.止 === null ? process.hrtime.bigint() : obj.state.止;
  return Number(止 - obj.state.起) / 1e9;
}

const TIMER_METHODS = {
  '进入': (obj, args, line, col) => {
    _methodArity('进入', args, 0, 0, line, col);
    return obj;
  },
  '耗时': (obj, args, line, col) => {
    _methodArity('耗时', args, 0, 0, line, col);
    return new R.FloatBox(_timerSeconds(obj));
  },
  '停': (obj, args, line, col) => {
    _methodArity('停', args, 0, 0, line, col);
    if (obj.state.止 === null) obj.state.止 = process.hrtime.bigint();
    return new R.FloatBox(_timerSeconds(obj));
  },
  '退出': (obj, args, line, col) => {
    _methodArity('退出', args, 0, 0, line, col);
    if (obj.state.止 === null) obj.state.止 = process.hrtime.bigint();
    // 给了名字才打印（与 Python 侧同一条口径：想把耗时算进自己的表格时不该被打扰）
    if (obj.state.名字 && !obj.state.已打印) {
      obj.state.已打印 = true;
      process.stdout.write(`${obj.state.名字} 耗时 ${_timerSeconds(obj).toFixed(3)} 秒\n`);
    }
    return null;
  },
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

/** 摘要长度（字节）—— PBKDF2 的 dkLen 要对齐它，才与 Python 的默认一致。 */
const HASH_LEN = { md5: 16, sha1: 20, sha256: 32, sha512: 64 };

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
  'HMAC': new R.Builtin('HMAC', (key, msg, algo = 'sha256') => {
    const name = normAlgo(algo);
    return _crypto.createHmac(name, toBuf(key))
      .update(toBuf(msg))
      .digest('hex');
  }),
  '口令摘要': new R.Builtin('口令摘要', (pwd, salt, iters = 100000, algo = 'sha256') => {
    const name = normAlgo(algo);
    const n = Math.trunc(Number(iters));
    if (!(n >= 1)) throw R.valueErr(`迭代次数要是正整数，现在是 ${iters}`);
    // dkLen 取摘要长度，与 Python 的 `pbkdf2_hmac(algo, …)` 默认值一致
    return _crypto.pbkdf2Sync(toBuf(pwd), toBuf(salt), n, HASH_LEN[name], name).toString('hex');
  }),
  '安全随机字节': new R.Builtin('安全随机字节', (n) => {
    const k = Math.trunc(Number(n));
    if (!(k >= 1)) throw R.valueErr(`长度要是正整数，现在是 ${n}`);
    // M113：返回**字节**（以前给十六进制文本）
    return new R.JishiBytes(_crypto.randomBytes(k));
  }),
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

/** `full` 相对 `base` 的 zip 条目名（M171）。算不出就报错，不猜。 */
function _arcName(full, base) {
  const f = String(full).replace(/\\/g, '/');
  const b = String(base).replace(/\\/g, '/').replace(/\/+$/, '');
  if (f === b) return '';
  const pre = b + '/';
  if (!f.startsWith(pre)) {
    throw R.valueErr(`「${full}」不在基础目录「${base}」里面 —— 算不出条目名`);
  }
  return f.slice(pre.length);
}

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

/** 条目名安全检查（M160）。返回「问题的一句话」，空串表示没问题。
 *
 *  🔴 **不靠黑名单关键字，靠结构**：条目名只能是**相对路径**，且不能往上走。
 *  解压的写法是「拿条目名当相对路径拼到目标目录后面」，而条目名是**包里写的**、
 *  不是用户写的 —— 一个 `../../.ssh/authorized_keys` 就能写到目标目录之外。
 *
 *  ⚠️ 三个宿主（Python / Rust / Node）必须**逐字一致**：
 *  靠 `tests/test_m160_zip_slip.py` 钉住。
 */
function checkEntryName(name) {
  if (!name) return '条目名是空的';
  if (name.indexOf('\0') >= 0) return '条目名里有 NUL 字符';
  if (name.startsWith('/') || name.startsWith('\\')) {
    return '条目名以「/」开头（绝对路径）';
  }
  if (name.length >= 2 && name[1] === ':' && /[A-Za-z]/.test(name[0])) {
    return '条目名带盘符（如「C:」）';
  }
  const 段们 = name.replace(/\\/g, '/').split('/');
  for (const 段 of 段们) {
    if (段 === '..') return '条目名里有「..」（会往上跳到目标目录之外）';
  }
  return '';
}

/** 读 zip：从 EOCD 找中央目录，再逐条按偏移取本地头与数据。
 *
 *  `仅目录 = true` 时**只读中央目录**（返回 `{name, usize}`，不 inflate 数据）——
 *  M160 加它，是为了让「解压前的名字校验与总量判断」发生在**解数据之前**：
 *  否则一个压缩炸弹会先被解进内存，再谈「要不要拦」就晚了。
 *  ⚠️ 这也让 Node 与 Python（`infolist`）/ Rust（`read_entries`）的**判定口径一致**：
 *  三处都看「中央目录里声明的大小」，而不是「我实际解出来多少」。
 */
function readZipEntries(buf, 仅目录) {
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
    const usize = buf.readUInt32LE(p + 24);
    const nameLen = buf.readUInt16LE(p + 28);
    const extraLen = buf.readUInt16LE(p + 30);
    const commentLen = buf.readUInt16LE(p + 32);
    const localOff = buf.readUInt32LE(p + 42);
    const name = buf.subarray(p + 46, p + 46 + nameLen)
      .toString((flags & 0x0800) ? 'utf8' : 'latin1');
    p += 46 + nameLen + extraLen + commentLen;
    if (仅目录) { out.push({ name, usize }); continue; }
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
  // **可复现**的打包（M171）：同一个目录打两次，字节完全相同。
  // 差别：条目排序 + 时间定死 1980-01-01。⚠️ 跨引擎不保证逐字节相同
  // （这边用 deflate，Rust 侧是 stored）。
  '打包确定': new R.Builtin('打包确定', (zipPath, src, base = '') => {
    const out = pText(zipPath);
    const 基础 = base ? pText(base) : '';
    let files = [];                        // [full, arc]
    if (Array.isArray(src)) {
      if (!基础) {
        throw R.valueErr('传文件列表时必须给「基础」目录 —— 否则算不出 zip 里的条目名',
          undefined, undefined, '例如 压缩.打包确定("包.zip", ["bin/jishi.exe"], 基础="发布")');
      }
      files = src.map((one) => {
        const full = pText(one);
        return [full, _arcName(full, 基础)];
      });
    } else {
      const root = pText(src);
      if (!statOf(root) || !statOf(root).isDirectory()) {
        throw R.valueErr(`「${root}」既不是目录也不是列表，无法确定要打包什么`);
      }
      const b = 基础 || root;
      for (const full of walkFiles(root)) files.push([full, _arcName(full, b)]);
    }
    if (!files.length) throw R.valueErr(`「${pText(src)}」里没有文件，打出来的包会是空的`);
    files.sort((a, b) => (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0));
    const chunks = [];
    const central = [];
    let offset = 0;
    const push = (x) => { chunks.push(x); offset += x.length; };
    // 本地时间的 1980-01-01 00:00 —— dosDateTime 走的是本地时间，
    // 这样跨时区打出来的日期字段一样（Python 侧写的就是这个字面量）。
    const 定死 = new Date(1980, 0, 1, 0, 0, 0).getTime();
    for (const [full, arc] of files) {
      const localOff = offset;
      central.push(writeZipEntry(push, arc, fs.readFileSync(full), localOff, 定死));
    }
    const cd = Buffer.concat(central.map(centralEntry));
    const eocd = Buffer.alloc(22);
    eocd.writeUInt32LE(0x06054b50, 0);
    eocd.writeUInt16LE(0, 4);
    eocd.writeUInt16LE(0, 6);
    eocd.writeUInt16LE(central.length, 8);
    eocd.writeUInt16LE(central.length, 10);
    eocd.writeUInt32LE(cd.length, 12);
    eocd.writeUInt32LE(offset, 16);
    eocd.writeUInt16LE(0, 20);
    fs.writeFileSync(out, Buffer.concat([...chunks, cd, eocd]));
    return out;
  }),
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
    // 🔴 M160：**先只看中央目录**（不解数据）就把名字和总量判掉 ——
    //    否则压缩炸弹会先被解进内存，再谈「要不要拦」就晚了。
    const 包名 = _path.basename(pText(zipPath));
    let entries;
    let 目录;
    try {
      目录 = readZipEntries(buf, true);
    } catch (e) {
      throw R.runErr(`解压「${包名}」失败：${e && e.message ? e.message : e}`);
    }
    // 条目名是包里写的，不是用户写的：`../../x` 能写到目标目录之外。
    let 总量 = 0;
    for (const e of 目录) {
      const 问题 = checkEntryName(e.name);
      if (问题) {
        throw R.runErr(`解压「${包名}」失败：条目「${e.name}」不合法（${问题}）`);
      }
      总量 += (e.usize || 0);
    }
    const 上限 = 512 * 1024 * 1024;
    if (总量 > 上限) {
      throw R.runErr(
        `解压「${包名}」失败：解开需要 ${(总量 / (1024 * 1024)).toFixed(1)} MB，`
        + `超过上限 ${上限 / (1024 * 1024)} MB`
        + '（先用「压缩.列出内容」看看有哪些，再用「压缩.解压成员」只取需要的）');
    }
    try { entries = readZipEntries(buf); } catch (e) {
      throw R.runErr(`解压「${包名}」失败：${e && e.message ? e.message : e}`);
    }
    try {
      fs.mkdirSync(dst, { recursive: true });
      for (const e of entries) {
        const outPath = _path.join(dst, e.name);
        fs.mkdirSync(_path.dirname(outPath), { recursive: true });
        fs.writeFileSync(outPath, e.data);
      }
    } catch (e) {
      throw R.runErr(`解压「${包名}」失败：${e && e.message ? e.message : e}`);
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
  // 🔴 M118（台账 R301）：只取包里**一个成员**（不解整包）。
  '解压成员': new R.Builtin('解压成员', (zipPath, name) => {
    const buf = zipBytes(zipPath);
    const 名 = pText(name);
    let entries;
    try {
      entries = readZipEntries(buf);
    } catch (e) {
      throw R.runErr(`「${pText(zipPath)}」不是有效的 zip：`
        + `${e && e.message ? e.message : e}`);
    }
    const hit = entries.find((x) => x.name === 名);
    if (!hit) {
      // ⚠️ 只报**文件名**（与 Python / Rust 逐字一致）：完整路径对用户没价值，
      //    而且临时目录带随机串 —— 报错语料要跨引擎对拍。
      throw R.valueErr(`「${_path.basename(pText(zipPath))}」里没有「${名}」这个成员`
        + '（先用 `压缩.列出内容` 看看有哪些）');
    }
    return new R.JishiBytes(hit.data);
  }),
  '压缩文件': new R.Builtin('压缩文件', (gzPath, content) => {
    const target = pText(gzPath);
    const dir = _path.dirname(target);
    try {
      if (dir) fs.mkdirSync(dir, { recursive: true });
      // `mtime: 0`：头部不写当前时间，同一份内容每次压出来的字节相同
      // （与 Python 侧 `GzipFile(mtime=0)` 对齐）
      fs.writeFileSync(target, _zlib.gzipSync(
        Buffer.from(pText(content), 'utf8'), { level: 9, mtime: 0 }));
    } catch (e) {
      throw R.runErr(`压缩到「${target}」失败：${e && e.message ? e.message : e}`);
    }
    return target;
  }),
  '解压文件': new R.Builtin('解压文件', (gzPath) => {
    const target = pText(gzPath);
    const st = statOf(target);
    if (!st || !st.isFile()) throw R.valueErr(`「${target}」不是文件`);
    let raw = null;
    try {
      raw = _zlib.gunzipSync(fs.readFileSync(target));
    } catch (e) {
      throw R.runErr(`解压「${target}」失败：${e && e.message ? e.message : e}`);
    }
    const text = raw.toString('utf8');
    // Node 的解码会把非法字节替换成 U+FFFD；用回环检测判断「本来就不是 UTF-8」
    if (!Buffer.from(text, 'utf8').equals(raw)) {
      throw R.runErr(`「${target}」解出来的不是 UTF-8 文本，没法按文本返回`);
    }
    return text;
  }),
};

// -- 网络（M31；M107 补：请求头 / 提交JSON / 获取详情 / 下载）--
// **Node 没有同步 HTTP**（`fetch` 是异步的，而基石没有 await 语法）。
// 方案：`spawnSync` 起一个**自身的子进程**执行异步请求，结果经 stdout 回传。
// 零第三方依赖，也不要求系统装了 `curl`。代价是每次请求多一次进程启动
// （本机实测约 40ms），换来与 Python 侧 `urllib` 一样的**同步**调用形态。
//
// ⚠️ 子进程把**状态码 + 响应头 + 正文字节（base64）**一起回传成 JSON ——
// 这样 `获取详情` / `下载` 才拿得到状态码与原始字节（`fetch` 非 2xx 不抛）。
// 🔴 M161（2026-11-01）：**不再 spawn 自己**。
//
// 旧做法：`spawnSync(process.execPath, ['-e', 脚本, ...])` —— 让一个子 node 去发请求，
// 主线程同步等它。在 Windows 上**偶发 `EBUSY`**（父进程的可执行文件正被占用），
// 于是 3 个网络用例长期被当成「环境问题」忽略（M31 起）。
//
// 新做法：**Worker + SharedArrayBuffer + Atomics.wait** ——
//   ① 主线程先起一个 Worker（同一进程内的线程，**不碰 EXE**，没有 EBUSY 这回事）；
//   ② Worker 里照常 `fetch`（异步）；
//   ③ 结果写进共享内存，`Atomics.notify` 叫醒主线程；
//   ④ 主线程 `Atomics.wait` 阻塞到被叫醒或超时。
//
// ⚠️ 一条必须知道的限制：**主线程阻塞期间，同一进程里的 HTTP 服务没人 accept**。
//    所以「自己起服务、自己同步请求」在同一进程里必然死锁 —— 这不是 bug，
//    是「同步语言跑在单线程事件循环上」的固有形状（`HTTP服务端` 在 Node 上本来也只做协议核心）。
const 同步请求源码 = `
const { workerData } = require('worker_threads');
const { sab, method, url, body, timeout, headers } = workerData;
const i32 = new Int32Array(sab, 0, 8);
const view = new Uint8Array(sab, 128);
const BASE = 128;
const 叫醒 = (码, 状态, meta, 体) => {
  const metaBuf = Buffer.from(meta, 'utf8');
  const 体Buf = Buffer.isBuffer(体) ? 体 : Buffer.from(体 || '');
  const m = Math.min(metaBuf.length, view.length);
  view.set(metaBuf.subarray(0, m), 0);
  const rest = view.length - m;
  const n = Math.min(体Buf.length, Math.max(0, rest));
  if (n > 0) view.set(体Buf.subarray(0, n), m);
  i32[2] = m;
  i32[4] = n;
  i32[1] = 状态;
  Atomics.store(i32, 0, 码);
  Atomics.notify(i32, 0);
};
(async () => {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), Math.max(100, Number(timeout) * 1000));
  try {
    let extra = {};
    try { extra = JSON.parse(headers || '{}'); } catch (e) { extra = {}; }
    const opt = { method, signal: ctl.signal,
                  headers: { 'User-Agent': 'jishi/0.1', ...extra } };
    if (method === 'POST' || method === 'PUT' || method === 'PATCH') opt.body = body;
    const resp = await fetch(url, opt);
    const buf = Buffer.from(await resp.arrayBuffer());
    const h = {};
    resp.headers.forEach((v, k) => { h[k] = v; });
    叫醒(1, resp.status, JSON.stringify(h), buf);
  } catch (e) {
    叫醒(2, 0, String((e && e.message) ? e.message : e), null);
  } finally {
    clearTimeout(timer);
  }
})();
`;

//: 共享数据区大小。响应体超过它 ⇒ 明确报错（旧实现的 `maxBuffer` 是 64 MB，
//: 但那是把整包 base64 进 stdout；这里直接放原始字节，8 MB 够日常用）。
const 同步请求缓冲 = 8 * 1024 * 1024;

function netRequest(method, url, body, timeout, headers) {
  const site = 规范网址(pText(url));
  const secs = Number(timeout);
  const sab = new SharedArrayBuffer(128 + 同步请求缓冲);
  const i32 = new Int32Array(sab, 0, 8);
  const w = new _worker.Worker(同步请求源码, {
    eval: true,
    workerData: { sab, method, url: site, body,
                  timeout: secs, headers: JSON.stringify(headers || {}) },
  });
  let 退出;
  try {
    退出 = Atomics.wait(i32, 0, 0, Math.max(100, secs * 1000) + 5000);
  } finally {
    // 无论醒没醒都要收掉线程：超时后它可能还在跑，留在那儿会拖住进程退出
    w.terminate();
  }
  if (退出 === 'timed-out' || i32[0] === 0) {
    throw R.runErr(`访问「${site}」失败：超过 ${secs} 秒没有响应`);
  }
  const metaLen = i32[2] | 0;
  const bodyLen = i32[4] | 0;
  const 区 = new Uint8Array(sab, 128);
  const metaText = Buffer.from(区.subarray(0, metaLen)).toString('utf8');
  if (i32[0] === 2) {
    throw R.runErr(`访问「${site}」失败：${metaText || '未知原因'}`);
  }
  let hdr;
  try {
    hdr = JSON.parse(metaText || '{}');
  } catch (e) {
    hdr = {};
  }
  // 数据区被撑满 ⇒ 响应比缓冲大，**如实报错**（不许悄悄截断：截断的 JSON 更难查）
  if (metaLen + bodyLen >= 128 + 同步请求缓冲 - 1) {
    throw R.runErr(
      `访问「${site}」失败：响应超过 ${同步请求缓冲 / (1024 * 1024)} MB 缓冲`
      + '（改用 `网络.下载` 存到文件，或分段取）');
  }
  return {
    status: Number(i32[1]) || 0,
    headers: hdr,
    bodyBuf: Buffer.from(区.subarray(metaLen, metaLen + bodyLen)),
  };
}

// 请求头字典（Map）→ 普通对象（ASCII 校验；Content-Length/Host 不让用户写）
function netHeaders(请求头) {
  if (请求头 == null) return {};
  if (!(请求头 instanceof Map)) {
    throw R.typeErr('「请求头」要传一个字典，例如 {"X-Token": "abc"}');
  }
  const out = {};
  for (const [k, v] of 请求头) {
    const 名 = pText(k);
    const 值 = pText(v);
    if (!/^[\x00-\xff]*$/.test(名)) throw R.typeErr(`请求头**名字**「${名}」必须是 ASCII（英文数字）`);
    if (!/^[\x00-\xff]*$/.test(值)) throw R.typeErr(`请求头「${名}」的值里有中文，发不出去`);
    const 低 = 名.toLowerCase();
    if (低 === 'content-length' || 低 === 'host') continue;
    out[名] = 值;
  }
  return out;
}

/// R101：把网址里**非 ASCII 的字符**自动百分号编码。
///
/// `encodeURI` 已经做了大部分（它保留 `/ ? : @ & = + $ , #` 这些结构字符），
/// 但有一处与 Python / Rust 不一致：**它会把 `%` 编成 `%25`**
/// ⇒ 「已经是编码过的网址」会被**二次编码**（`%E4%B8%AD` → `%25E4%25B8%25AD`）。
/// 所以再把 `%25XX` 还原回 `%XX`，三边就逐字节一致了。
function 规范网址(u) {
  return encodeURI(u).replace(/%25([0-9A-Fa-f]{2})/g, '%$1');
}

// 非 2xx 按统一口径报错（各引擎措辞一致）
function net要成功(resp, 动作, 网址) {
  if (resp.status >= 200 && resp.status < 300) return resp;
  throw R.runErr(`${动作}「${网址}」失败：HTTP ${resp.status}`);
}

const NET_FUNCS = {
  '获取': new R.Builtin('获取', (url, timeout = 10.0, 请求头 = null) => {
    const r = netRequest('GET', url, null, timeout, netHeaders(请求头));
    return net要成功(r, '访问', pText(url)).bodyBuf.toString('utf8');
  }),
  '提交': new R.Builtin('提交', (url, data = '', timeout = 10.0, 请求头 = null) => {
    const h = netHeaders(请求头);
    if (!('content-type' in Object.fromEntries(
      Object.entries(h).map(([k, v]) => [k.toLowerCase(), v])))) {
      h['Content-Type'] = 'application/x-www-form-urlencoded';
    }
    const r = netRequest('POST', url, data, timeout, h);
    return net要成功(r, '提交到', pText(url)).bodyBuf.toString('utf8');
  }),
  '提交JSON': new R.Builtin('提交JSON', (url, 对象, timeout = 10.0, 请求头 = null) => {
    const h = netHeaders(请求头);
    if (!h['Content-Type']) h['Content-Type'] = 'application/json; charset=utf-8';
    const body = jsonEncode(对象, null);
    const r = netRequest('POST', url, body, timeout, h);
    return net要成功(r, '提交到', pText(url)).bodyBuf.toString('utf8');
  }),
  '获取JSON': new R.Builtin('获取JSON', (url, timeout = 10.0, 请求头 = null) => {
    const r = netRequest('GET', url, null, timeout, netHeaders(请求头));
    const text = net要成功(r, '访问', pText(url)).bodyBuf.toString('utf8');
    try { return jsonDecode(text); } catch (e) {
      throw R.valueErr(`「${pText(url)}」返回的不是有效 JSON：`
        + `${e && e.message ? e.message : e}`);
    }
  }),
  '获取详情': new R.Builtin('获取详情', (url, timeout = 10.0, 请求头 = null) => {
    const r = netRequest('GET', url, null, timeout, netHeaders(请求头));
    const 头表 = new Map(Object.entries(r.headers).map(([k, v]) => [k, v]));
    return new Map([
      ['状态', r.status],
      ['响应头', 头表],
      ['正文', r.bodyBuf.toString('utf8')],
    ]);
  }),
  '下载': new R.Builtin('下载', (url, 路径, timeout = 10.0, 请求头 = null) => {
    const r = netRequest('GET', url, null, timeout, netHeaders(请求头));
    net要成功(r, '下载', pText(url));
    fs.writeFileSync(pText(路径), r.bodyBuf);
    return r.bodyBuf.length;
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
  // M117：与基石库层 `容器.jsh` 同行为（`索引表` 保**首次**下标；`深拷贝` 递归复制）
  '索引表': new R.Builtin('索引表', (it) => {
    const 见 = new Map();
    let i = 0;
    for (const x of seqFor('索引表', it)) {
      const hk = R.setKey(x);
      if (!见.has(hk)) 见.set(hk, { v: x, i });
      i += 1;
    }
    const d = new Map();
    for (const { v, i: idx } of 见.values()) d.set(v, idx);
    return d;
  }),
  '深拷贝': new R.Builtin('深拷贝', (v) => deepCopyVal(v)),
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
  // M106 补：`归约` / `分区`（与基石库层 `stdlib-jishi/迭代.jsh` 同行为）
  '归约': new R.Builtin('归约', (it, f, ...init) => {
    const all = seqFor('归约', it);
    let acc; let start;
    if (init.length === 0) {
      if (all.length === 0) throw R.valueErr('「归约」拿到的序列是空的，又没有给初值');
      acc = all[0]; start = 1;
    } else { acc = init[0]; start = 0; }
    for (let i = start; i < all.length; i++) acc = R.callValue(f, [acc, all[i]]);
    return acc;
  }),
  '分区': new R.Builtin('分区', (it, cond) => {
    const yes = []; const no = [];
    for (const x of seqFor('分区', it)) {
      if (R.truthy(R.callValue(cond, [x]))) yes.push(x); else no.push(x);
    }
    return [yes, no];
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
      if (b === undefined) {
        // M169：一次式**必须给两个参数**。给能照着改的话，不是「参数不够」。
        throw R.runErr('「参数.解析」的一次式需要两个参数（参数列表 + 「选项 → 默认值」字典），'
          + '例如 参数.解析(系统.参数(), {"数据": "默认.json"})；'
          + '只有传句柄那种用法才能省略第二个参数');
      }
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
    // M169：`参数们` **真的可省**（语言卡写着 `解析(句柄, 参数们=空)`）。
    // 省了就用本进程的命令行参数 —— 与 `系统.参数()` 同一份（`argv.slice(3)`）。
    const 列表 = (b === undefined) ? process.argv.slice(3) : b;
    return _parseList(_parserOf(a).specs, 列表);
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

// ---------------------------------------------------------------------------
// 向量（M139）：一列数字上的逐元素运算与点积
//
// ⚠️ **必须与 Python 一样保留「整数 / 小数」**：JS 侧的小数是 `FloatBox` 包装，
//    而 `_numsFor` 会一律解包 —— 解包之后 `[1, 2] × 0.5` 会打成 `[0.5, 1]`，
//    而 Python 给的是 `[0.5, 1.0]`（对拍逐字节比，差一个 `.0` 就红）。
//    ⇒ 这里自己取数，每个元素记成 `{n: 数值, i: 是否整数, 原: 原值}`。
// ---------------------------------------------------------------------------
function _vecNums(name, v) {
  if (typeof v === 'string' || R.isBytes(v)) {
    throw R.typeErr(`「${name}」要传一列数字，不能传文本`);
  }
  const s = R.seqOf(v, false, undefined, undefined);
  if (s === null) throw R.typeErr(`「${name}」要传一列数字（列表）`);
  return s.map((原, i) => {
    const 数值 = R.unwrap(原);
    if (typeof 数值 === 'boolean' || typeof 数值 !== 'number') {
      throw R.typeErr(`「${name}」里第 ${i + 1} 个不是数字：${R.display(原)}`);
    }
    return { n: 数值, i: !R.isFloatBox(原), 原 };
  });
}
function _vec同长(a, b, name) {
  if (a.length !== b.length) {
    throw R.valueErr(`「${name}」要求两列数字**长度相同**，实际是 ${a.length} 和 ${b.length}`
      + '（长度不等时逐元素算会错位，所以这里直接报错）');
  }
}
function _vec非空(a, name) {
  if (a.length === 0) throw R.valueErr(`「${name}」要至少有一个数，现在是空的`);
  return a;
}
/** 两个数做一次算术：**都是整数就还整数**（与 Python 规则一致）。 */
function _vec算(x, y, f) { return { n: f(x.n, y.n), i: x.i && y.i }; }
function _vec出(c) { return c.i ? c.n : new R.FloatBox(c.n); }
function _vec整数(name, v, 哪) {
  const x = R.unwrap(v);
  if (R.isFloatBox(v) || typeof x !== 'number' || !Number.isInteger(x)) {
    throw R.typeErr(`「${name}」的${哪}要是整数，给的是 ${R.display(v)}`);
  }
  return x;
}

// ---------------------------------------------------------------------------
// 矩阵（M141）：构造 / 单位 / 行数 / 列数 / 转置 / 乘 / 行列式 / 解方程
//
// 与 Python 侧同一套数值决定：
//   · 整数矩阵的行列式**给精确整数**（无分数消元），整数方程组**给精确解**
//     （克莱姆法则）—— 否则 `[[1,2],[3,4]]` 会算出 -1.9999999999999993，
//     教学语言里不可接受；
//   · JS 的整数是 double 包装，所以精确路径内部一律用 **BigInt**，
//     出结果时再按「超过 2^53 就给浮点」统一口径（Rust 的 i128 用同一个界）。
// ---------------------------------------------------------------------------
const _安全整数上限 = 9007199254740992n;

function _matRows(name, v) {
  if (typeof v === 'string' || R.isBytes(v)) {
    throw R.typeErr(`「${name}」要传一个矩阵（列表的列表），不能传文本`);
  }
  const 行们 = R.seqOf(v, false, undefined, undefined);
  if (行们 === null) throw R.typeErr(`「${name}」要传一个矩阵（列表的列表）`);
  if (行们.length === 0) throw R.valueErr(`「${name}」要至少有一行，现在是空的`);
  let 宽 = null;
  return 行们.map((行, i) => {
    if (typeof 行 === 'string' || R.isBytes(行)) {
      throw R.typeErr(`「${name}」第 ${i + 1} 行不是列表（是文本）`);
    }
    const 列们 = R.seqOf(行, false, undefined, undefined);
    if (列们 === null) throw R.typeErr(`「${name}」第 ${i + 1} 行不是列表`);
    if (宽 === null) 宽 = 列们.length;
    else if (列们.length !== 宽) {
      throw R.valueErr(`「${name}」每行的长度要相同：第 1 行有 ${宽} 个，`
        + `第 ${i + 1} 行有 ${列们.length} 个`);
    }
    列们.forEach((x, j) => {
      const n = R.unwrap(x);
      if (typeof n !== 'number' || typeof n === 'boolean') {
        throw R.typeErr(`「${name}」第 ${i + 1} 行第 ${j + 1} 个不是数字：${R.display(x)}`);
      }
    });
    return 列们;
  });
}
function _matSquare(name, m) {
  if (m.length !== m[0].length) {
    throw R.valueErr(`「${name}」只对方阵有定义，这个矩阵是 ${m.length}×${m[0].length}`);
  }
}
function _matInt(name, v, 中文) {
  const x = R.unwrap(v);
  if (R.isFloatBox(v) || typeof x !== 'number' || !Number.isInteger(x)) {
    throw R.typeErr(`「${name}」的${中文}要是整数，给的是 ${R.display(v)}`);
  }
  return x;
}
function _matCell(v) {
  return { n: R.unwrap(v), i: !R.isFloatBox(v), 原: v };
}
/** 精确整数结果 → 值（超过 2^53 给浮点，与 Python / Rust 同一口径）。 */
function _bigOut(b) {
  if (b > _安全整数上限 || b < -_安全整数上限) return new R.FloatBox(Number(b));
  return Number(b);
}
/** Bareiss 无分数消元（BigInt 精确）。 */
function _bigDet(rows) {
  const n = rows.length;
  const m = rows.map((r) => r.slice());
  if (n === 1) return m[0][0];
  let 上一 = 1n; let 符号 = 1n;
  for (let k = 0; k < n - 1; k += 1) {
    if (m[k][k] === 0n) {
      let 换 = -1;
      for (let r = k + 1; r < n; r += 1) if (m[r][k] !== 0n) { 换 = r; break; }
      if (换 < 0) return 0n;
      const t = m[k]; m[k] = m[换]; m[换] = t;
      符号 = -符号;
    }
    const 主 = m[k][k];
    for (let i = k + 1; i < n; i += 1) {
      for (let j = k + 1; j < n; j += 1) {
        m[i][j] = (m[i][j] * 主 - m[i][k] * m[k][j]) / 上一;
      }
    }
    上一 = 主;
  }
  return 符号 * m[n - 1][n - 1];
}
function _floatDet(rows) {
  const n = rows.length;
  const m = rows.map((r) => r.slice());
  let 结果 = 1.0;
  for (let k = 0; k < n; k += 1) {
    let 主行 = k;
    for (let r = k + 1; r < n; r += 1) if (Math.abs(m[r][k]) > Math.abs(m[主行][k])) 主行 = r;
    if (Math.abs(m[主行][k]) < 1e-12) return 0.0;
    if (主行 !== k) { const t = m[k]; m[k] = m[主行]; m[主行] = t; 结果 = -结果; }
    结果 *= m[k][k];
    for (let r = k + 1; r < n; r += 1) {
      const 倍 = m[r][k] / m[k][k];
      for (let c = k; c < n; c += 1) m[r][c] -= 倍 * m[k][c];
    }
  }
  return 结果;
}
function _全整数(rows, 右) {
  const 都整 = rows.every((r) => r.every(
    (v) => !R.isFloatBox(v) && Number.isInteger(R.unwrap(v))));
  const 右整 = (右 === null)
    || 右.every((v) => !R.isFloatBox(v) && Number.isInteger(R.unwrap(v)));
  return 都整 && 右整;
}

const MATRIX_FUNCS = {
  '构造': new R.Builtin('构造', (行, 列, 初) => {
    const r = _matInt('构造', 行, '行数'); const c = _matInt('构造', 列, '列数');
    if (r <= 0 || c <= 0) {
      throw R.valueErr(`「构造」的行数和列数都要大于 0（给的是 ${r} 和 ${c}）`);
    }
    let 初值 = 0;
    if (初 !== undefined) {
      const x = R.unwrap(初);
      if (typeof x !== 'number' || typeof x === 'boolean') {
        throw R.typeErr(`「构造」的初值要是数字，给的是 ${R.display(初)}`);
      }
      初值 = R.isFloatBox(初) ? new R.FloatBox(x) : x;
    }
    return Array.from({ length: r }, () => Array.from({ length: c }, () => 初值));
  }),
  '单位': new R.Builtin('单位', (阶) => {
    const n = _matInt('单位', 阶, '阶');
    if (n <= 0) throw R.valueErr(`「单位」的阶要大于 0（给的是 ${n}）`);
    return Array.from({ length: n },
      (_, i) => Array.from({ length: n }, (_, j) => (i === j ? 1 : 0)));
  }),
  '行数': new R.Builtin('行数', (甲) => _matRows('行数', 甲).length),
  '列数': new R.Builtin('列数', (甲) => _matRows('列数', 甲)[0].length),
  '转置': new R.Builtin('转置', (甲) => {
    const m = _matRows('转置', 甲);
    return m[0].map((_, j) => m.map((r) => r[j]));
  }),
  '乘': new R.Builtin('乘', (甲, 乙) => {
    const a = _matRows('乘', 甲); const b = _matRows('乘', 乙);
    if (a[0].length !== b.length) {
      throw R.valueErr('「乘」要求左矩阵的列数等于右矩阵的行数，实际是 '
        + `${a.length}×${a[0].length} 乘 ${b.length}×${b[0].length}`);
    }
    return a.map((行) => b[0].map((_, j) => {
      let 合 = { n: 0, i: true };
      for (let k = 0; k < b.length; k += 1) {
        const 积 = _vec算(_matCell(行[k]), _matCell(b[k][j]), (p, q) => p * q);
        合 = _vec算(合, 积, (p, q) => p + q);
      }
      return _vec出(合);
    }));
  }),
  '行列式': new R.Builtin('行列式', (甲) => {
    const m = _matRows('行列式', 甲);
    _matSquare('行列式', m);
    if (_全整数(m, null)) {
      const 整 = m.map((r) => r.map((v) => BigInt(R.unwrap(v))));
      return _bigOut(_bigDet(整));
    }
    return new R.FloatBox(_floatDet(m.map((r) => r.map((v) => R.unwrap(v)))));
  }),
  '解方程': new R.Builtin('解方程', (甲, 乙) => {
    const a = _matRows('解方程', 甲);
    _matSquare('解方程', a);
    const n = a.length;
    if (typeof 乙 === 'string' || R.isBytes(乙)) {
      throw R.typeErr('「解方程」右边的常数项要传一列数，不能传文本');
    }
    const 右值 = R.seqOf(乙, false, undefined, undefined);
    if (右值 === null) throw R.typeErr('「解方程」右边的常数项要传一列数（列表）');
    if (右值.length !== n) {
      throw R.valueErr(`「解方程」右边要有 ${n} 个常数（与系数矩阵的行数一致），`
        + `实际有 ${右值.length} 个`);
    }
    右值.forEach((v, i) => {
      const x = R.unwrap(v);
      if (typeof x !== 'number' || typeof x === 'boolean') {
        throw R.typeErr(`「解方程」右边第 ${i + 1} 个不是数字：${R.display(v)}`);
      }
    });

    if (_全整数(a, 右值)) {
      const 整甲 = a.map((r) => r.map((v) => BigInt(R.unwrap(v))));
      const 整右 = 右值.map((v) => BigInt(R.unwrap(v)));
      const 底 = _bigDet(整甲);
      if (底 !== 0n && 底 <= _安全整数上限 && 底 >= -_安全整数上限) {
        const 解 = [];
        let 超界 = false;
        for (let i = 0; i < n; i += 1) {
          const 列 = 整甲.map((r, ri) => r.map((v, ci) => (ci === i ? 整右[ri] : v)));
          const 分子 = _bigDet(列);
          if (分子 > _安全整数上限 || 分子 < -_安全整数上限) { 超界 = true; break; }
          if (分子 % 底 === 0n) 解.push(Number(分子 / 底));
          else 解.push(new R.FloatBox(Number(分子) / Number(底)));
        }
        if (!超界) return 解;
      }
    }

    const m = a.map((r) => r.map((v) => R.unwrap(v)));
    const 右 = 右值.map((v) => R.unwrap(v));
    for (let k = 0; k < n; k += 1) {
      let 主行 = k;
      for (let r = k + 1; r < n; r += 1) if (Math.abs(m[r][k]) > Math.abs(m[主行][k])) 主行 = r;
      if (Math.abs(m[主行][k]) < 1e-12) {
        throw R.valueErr('「解方程」这个方程组没有唯一解（系数矩阵的行列式为 0）');
      }
      if (主行 !== k) {
        const t = m[k]; m[k] = m[主行]; m[主行] = t;
        const s = 右[k]; 右[k] = 右[主行]; 右[主行] = s;
      }
      for (let r = 0; r < n; r += 1) {
        if (r === k) continue;
        const 倍 = m[r][k] / m[k][k];
        if (倍 === 0) continue;
        for (let c = k; c < n; c += 1) m[r][c] -= 倍 * m[k][c];
        右[r] -= 倍 * 右[k];
      }
    }
    return Array.from({ length: n }, (_, i) => new R.FloatBox(右[i] / m[i][i]));
  }),
};

const VECTOR_FUNCS = {
  '构造': new R.Builtin('构造', (个, 初) => {
    const n = _vec整数('构造', 个, '个数');
    if (n < 0) throw R.valueErr(`「构造」的个数不能是负数（给的是 ${n}）`);
    let 初值 = 0;
    let 是整数 = true;
    if (初 !== undefined) {
      是整数 = !R.isFloatBox(初);
      const x = R.unwrap(初);
      if (typeof x !== 'number' || typeof x === 'boolean') {
        throw R.typeErr(`「构造」的初值要是数字，给的是 ${R.display(初)}`);
      }
      初值 = x;
    }
    return Array.from({ length: n }, () => (是整数 ? 初值 : new R.FloatBox(初值)));
  }),
  '加': new R.Builtin('加', (甲, 乙) => {
    const a = _vecNums('加', 甲); const b = _vecNums('加', 乙);
    _vec同长(a, b, '加');
    return a.map((x, k) => _vec出(_vec算(x, b[k], (p, q) => p + q)));
  }),
  '减': new R.Builtin('减', (甲, 乙) => {
    const a = _vecNums('减', 甲); const b = _vecNums('减', 乙);
    _vec同长(a, b, '减');
    return a.map((x, k) => _vec出(_vec算(x, b[k], (p, q) => p - q)));
  }),
  '标量乘': new R.Builtin('标量乘', (甲, 倍) => {
    const a = _vecNums('标量乘', 甲);
    const 倍值 = R.unwrap(倍);
    if (typeof 倍值 !== 'number' || typeof 倍值 === 'boolean') {
      throw R.typeErr(`「标量乘」的倍数要是数字，给的是 ${R.display(倍)}`);
    }
    const k = { n: 倍值, i: !R.isFloatBox(倍) };
    return a.map((x) => _vec出(_vec算(x, k, (p, q) => p * q)));
  }),
  '点积': new R.Builtin('点积', (甲, 乙) => {
    const a = _vecNums('点积', 甲); const b = _vecNums('点积', 乙);
    _vec同长(a, b, '点积');
    let 合 = { n: 0, i: true };
    for (let k = 0; k < a.length; k += 1) {
      合 = _vec算(合, _vec算(a[k], b[k], (p, q) => p * q), (p, q) => p + q);
    }
    return _vec出(合);
  }),
  '和': new R.Builtin('和', (甲) => {
    const a = _vecNums('和', 甲);
    let 合 = { n: 0, i: true };
    for (const x of a) 合 = _vec算(合, x, (p, q) => p + q);
    return _vec出(合);
  }),
  '均值': new R.Builtin('均值', (甲) => {
    const a = _vec非空(_vecNums('均值', 甲), '均值');
    // Python 里 `/` 是真除法 ⇒ 结果**一定是小数**
    return new R.FloatBox(a.reduce((t, x) => t + x.n, 0) / a.length);
  }),
  '最大': new R.Builtin('最大', (甲) => {
    const a = _vec非空(_vecNums('最大', 甲), '最大');
    let 最好 = a[0];
    for (const x of a) if (x.n > 最好.n) 最好 = x;
    return 最好.原;
  }),
  '最小': new R.Builtin('最小', (甲) => {
    const a = _vec非空(_vecNums('最小', 甲), '最小');
    let 最好 = a[0];
    for (const x of a) if (x.n < 最好.n) 最好 = x;
    return 最好.原;
  }),
  '维数': new R.Builtin('维数', (甲) => _vecNums('维数', 甲).length),
  '切片': new R.Builtin('切片', (甲, 起, 止) => {
    const a = _vecNums('切片', 甲);
    const 总数 = a.length;
    const 首 = _vec整数('切片', 起, '起');
    const 尾 = (止 === undefined || 止 === null) ? 总数 : _vec整数('切片', 止, '止');
    if (首 < 0 || 尾 < 0) {
      throw R.valueErr(`「切片」的起和止都不能是负数（给的是 ${首} 和 ${尾}）`);
    }
    if (首 > 尾) throw R.valueErr(`「切片」的起 ${首} 不能大于止 ${尾}`);
    if (尾 > 总数) throw R.valueErr(`「切片」的止 ${尾} 超出维数 ${总数}`);
    return a.slice(首, 尾).map((x) => x.原);
  }),
};

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
  if (R.isBytes(v)) return v.buf;
  if (Buffer.isBuffer(v)) return v;
  const s = R.seqOf(v, true, undefined, undefined);
  if (s === null) throw R.valueErr(`「${name}」要传文本、字节或 0-255 的整数列表`);
  const out = [];
  s.forEach((x, i) => {
    if (typeof x === 'boolean' || !_isInt(x)) {
      throw R.valueErr(`「${name}」里第 ${i + 1} 个不是 0-255 的整数：${x}`);
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
  if (R.isBytes(v) || Buffer.isBuffer(v)) return _toBytesFor(name, v);
  const s = R.seqOf(v, true, undefined, undefined);
  if (s === null) throw R.valueErr(`「${name}」要传文本或字节`);
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
    // M113：返回**字节**（以前给 0-255 的整数列表）
    return new R.JishiBytes(Buffer.from(s, 'base64'));
  }),
  '十六进制编码': new R.Builtin('十六进制编码', (v, upper = false) => {
    const h = _toBytesFor('十六进制编码', v).toString('hex');
    return upper ? h.toUpperCase() : h;
  }),
  '十六进制解码': new R.Builtin('十六进制解码', (v) => {
    const raw = _wantBytes('十六进制解码', v);
    const 原文 = raw.toString('ascii');
    let s = 原文.replace(/[ \t]/g, '')
      .replace(/0[xX]/g, '').replace(/,/g, '');
    if (s === '') return new R.JishiBytes(Buffer.alloc(0));
    if (s.length % 2) s = '0' + s;
    if (!/^[0-9a-fA-F]*$/.test(s)) {
      // ⚠️ 与 Rust / Python 一致：报**原文**（去空白前），不带宿主细节
      throw R.valueErr(`这不是合法的十六进制内容：${原文}`);
    }
    // M113：返回**字节**
    return new R.JishiBytes(Buffer.from(s, 'hex'));
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
      // 与 Rust / Python 一致：只报原文，不带宿主（JS）的错误细节
      throw R.valueErr(`这不是合法的 URL 编码内容：${v}`);
    }
  }),
  '文本字节': new R.Builtin('文本字节', (v) => {
    if (typeof v !== 'string') throw R.valueErr('「文本字节」要传文本');
    // M113：返回**字节**（以前给 0-255 的整数列表）
    return new R.JishiBytes(Buffer.from(v, 'utf8'));
  }),
  // 🔴 M119：GBK。解码走内置 `TextDecoder('gbk')`（fatal 让它对坏字节抛错），
  // 编码走生成的码表（见文件头的说明）。
  'GBK文本': new R.Builtin('GBK文本', (v) => {
    const raw = _toBytesFor('GBK文本', v);
    try {
      return new TextDecoder('gbk', { fatal: true }).decode(raw);
    } catch (e) {
      throw R.valueErr('这些字节不是合法的 GBK 文本（可能有截断，或其实是别的编码）');
    }
  }),
  '文本GBK': new R.Builtin('文本GBK', (v) => {
    const s = pText(v);
    const 反 = gbk反查();
    const 出 = [];
    for (const c of s) {
      const 码 = 反.get(c);
      if (码 === undefined) {
        const cp = c.codePointAt(0);
        if (cp < 0x80) { 出.push(cp); continue; }   // ASCII 与 GBK 兼容
        throw R.valueErr('这段文本里有 GBK 表示不了的字符（比如表情符号）');
      }
      出.push(码 >> 8, 码 & 0xff);
    }
    return new R.JishiBytes(Buffer.from(出));
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

// ---------------------------------------------------------------------------
// M107：html 解析方向（取文本 / 取链接 / 取表格）
// 规则与 Python 侧 `stdlib/html.py` 逐字对齐（那份是判据基准）—— 见那里的说明。
// ---------------------------------------------------------------------------
const _HTML_空白 = ' \t\n\r\u000b\u000c';
function htmlIsBlank(c) { return _HTML_空白.includes(c); }
function htmlIsNameChar(c) { return /^[A-Za-z0-9\-_:.]$/.test(c); }

function html解实体(t) {
  if (!t.includes('&')) return t;
  const chars = Array.from(t);
  let out = '';
  let i = 0;
  const 表 = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };
  while (i < chars.length) {
    if (chars[i] !== '&') { out += chars[i]; i += 1; continue; }
    const 分号 = chars.indexOf(';', i + 1);
    if (分号 < 0 || 分号 - i > 10) { out += '&'; i += 1; continue; }
    const 体 = chars.slice(i + 1, 分号).join('');
    let 码 = null;
    if (体.startsWith('#')) {
      const 数 = 体.slice(1);
      const 十 = (数[0] === 'x' || 数[0] === 'X')
        ? parseInt(数.slice(1), 16) : parseInt(数, 10);
      if (Number.isInteger(十)) 码 = 十;
    }
    let 认了 = false;
    if (码 !== null) {
      认了 = 码 >= 0 && 码 <= 0x10FFFF && !(码 >= 0xD800 && 码 <= 0xDFFF);
    } else {
      认了 = Object.prototype.hasOwnProperty.call(表, 体);
    }
    if (认了) {
      if (码 !== null) out += String.fromCodePoint(码);
      else out += 表[体];
      i = 分号 + 1;
      continue;
    }
    out += '&';
    i += 1;
  }
  return out;
}

function html压空白(t) {
  let out = '';
  let 上个是空白 = false;
  for (const c of t) {
    if (htmlIsBlank(c)) {
      if (!上个是空白) out += ' ';
      上个是空白 = true;
    } else {
      out += c;
      上个是空白 = false;
    }
  }
  return out.replace(/^ +| +$/g, '');
}

// 扫成 [类型, 名, 体]；类型 0=文本 1=开始 2=结束
function html扫(s) {
  const chars = Array.from(String(s));
  const n = chars.length;
  const out = [];
  let i = 0;
  while (i < n) {
    const lt = chars.indexOf('<', i);
    if (lt < 0) { out.push([0, '', chars.slice(i).join('')]); break; }
    if (lt > i) out.push([0, '', chars.slice(i, lt).join('')]);
    if (chars.slice(lt, lt + 4).join('') === '<!--') {
      let 末2 = -1;
      for (let k = lt + 4; k + 2 < n; k++) {
        if (chars[k] === '-' && chars[k + 1] === '-' && chars[k + 2] === '>') { 末2 = k + 2; break; }
      }
      i = 末2 < 0 ? n : 末2 + 1;
      continue;
    }
    if (lt + 1 < n && (chars[lt + 1] === '!' || chars[lt + 1] === '?')) {
      const 末 = chars.indexOf('>', lt + 1);
      i = 末 < 0 ? n : 末 + 1;
      continue;
    }
    const 末 = chars.indexOf('>', lt + 1);
    if (末 < 0) { out.push([0, '', chars.slice(lt).join('')]); break; }
    let 体 = chars.slice(lt + 1, 末).join('');
    i = 末 + 1;
    const 结束 = 体.startsWith('/');
    if (结束) 体 = 体.slice(1);
    let j = 0;
    while (j < 体.length && htmlIsBlank(体[j])) j += 1;
    const 起 = j;
    while (j < 体.length && htmlIsNameChar(体[j])) j += 1;
    const 名 = 体.slice(起, j).toLowerCase();
    if (!名) continue;
    if (名 === 'script' || 名 === 'style') {
      if (!结束) {
        const 针 = '</' + 名;
        let 找到 = -1;
        for (let k = i; k < n; k++) {
          if (chars.slice(k, k + 针.length).join('').toLowerCase() === 针) { 找到 = k; break; }
        }
        if (找到 < 0) i = n;
        else {
          const 尖 = chars.indexOf('>', 找到);
          i = 尖 < 0 ? n : 尖 + 1;
        }
      }
      continue;
    }
    out.push([结束 ? 2 : 1, 名, 体]);
  }
  return out;
}

function html文本节点(节点们) {
  let 块 = '';
  for (const [类型, , 体] of 节点们) if (类型 === 0) 块 += 体;
  return html压空白(html解实体(块));
}

function html取属性(标签体, 名) {
  const 低 = 标签体.toLowerCase();
  const body = Array.from(标签体);
  const 针 = 名.toLowerCase();
  let i = 0;
  while (i < 低.length) {
    const k = 低.indexOf(针, i);
    if (k < 0) return null;
    const 前好 = k === 0 || !htmlIsNameChar(低[k - 1]);
    let 后 = k + 针.length;
    while (后 < 低.length && htmlIsBlank(低[后])) 后 += 1;
    if (前好 && 后 < 低.length && 低[后] === '=') {
      后 += 1;
      while (后 < 低.length && htmlIsBlank(低[后])) 后 += 1;
      if (后 < 低.length && (低[后] === '"' || 低[后] === "'")) {
        const 引 = 低[后];
        const 末 = body.indexOf(引, 后 + 1);
        if (末 < 0) return html解实体(body.slice(后 + 1).join(''));
        return html解实体(body.slice(后 + 1, 末).join(''));
      }
      let 末 = 后;
      while (末 < 低.length && !htmlIsBlank(低[末]) && 低[末] !== '>') 末 += 1;
      return html解实体(body.slice(后, 末).join(''));
    }
    i = k + 针.length;
  }
  return null;
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

// M166：页面骨架用的内置样式。
//
// ⚠️ **这份字符串是从 Python 侧 `html._样式` 直接导出来的**（`json.dumps`），
// 不是手抄的 —— 手抄过一版，CSS 里 `"Segoe UI"` 的引号就漏了一个，
// 于是 Node 产出的页面跟另外两家不一样，而且是**静默**的（页面照样能看）。
const HTML_样式 = ":root{--底:#fff;--字:#1f2328;--弱:#6b7280;--线:#e5e7eb;--主:#2563eb;--成功:#16a34a;--警告:#d97706;--错误:#dc2626}*{box-sizing:border-box}body{margin:0;font:16px/1.6 system-ui,-apple-system,\"Segoe UI\",\"Noto Sans SC\",sans-serif;color:var(--字);background:var(--底)}header{padding:1rem 1.5rem;border-bottom:1px solid var(--线)}header h1{margin:0;font-size:1.25rem}nav{margin-top:.5rem}nav a{margin-right:1rem;color:var(--主);text-decoration:none}main{max-width:56rem;margin:0 auto;padding:1.5rem}footer{padding:1rem 1.5rem;color:var(--弱);border-top:1px solid var(--线);font-size:.875rem}.卡片{border:1px solid var(--线);border-radius:.5rem;padding:1rem;margin:1rem 0}.卡片 h2{margin:0 0 .5rem;font-size:1.1rem}.提示{padding:.75rem 1rem;border-radius:.375rem;margin:.75rem 0;border-left:4px solid;background:#f9fafb}.提示-成功{border-color:var(--成功);background:#f0fdf4}.提示-警告{border-color:var(--警告);background:#fffbeb}.提示-错误{border-color:var(--错误);background:#fef2f2}.基石表格{width:100%;border-collapse:collapse}.基石表格 th,.基石表格 td{padding:.5rem .75rem;border-bottom:1px solid var(--线);text-align:left}.基石表格 thead th{background:#f9fafb}.斑马纹 tbody tr:nth-child(even){background:#fafafa}.字段组{border:1px solid var(--线);border-radius:.375rem;margin:.5rem 0;padding:.5rem .75rem}label{margin:.5rem 0}label span{display:block;font-size:.875rem;color:var(--弱)}input,textarea,select{width:100%;padding:.5rem;border:1px solid var(--线);border-radius:.375rem;font:inherit}input[type=\"checkbox\"]{width:auto;margin-right:.5rem}button{padding:.5rem 1rem;border:0;border-radius:.375rem;background:var(--主);color:#fff;font:inherit;cursor:pointer}";

/// 要一个非空文本参数。
function _needText(name, v) {
  if (typeof v !== 'string' || v === '') {
    throw R.typeErr(`「${name}」要传非空文本`);
  }
}

/// 给控件套一层 `<label>`：有说明就带 `<span>`，没有就裸着。
function _labelWrap(说明, inner) {
  if (说明 === undefined || 说明 === null || 说明 === '') return inner;
  return `<label><span>${_escapeHtml(pText(说明))}</span>${inner}</label>`;
}

/// 空元素（`<input … />`）。值是 `null` = HTML 布尔属性（不输出 `=""`）。
function _voidEl(name, pairs) {
  let out = `<${name}`;
  for (const [k, v] of pairs) {
    out += v === null ? ` ${k}` : ` ${k}="${_escapeHtml(v)}"`;
  }
  return `${out} />`;
}

/// 内容位，但**列表会拼起来**（`表单([字段们], "/新建")` 这种写法），可嵌套。
function _htmlContentList(v) {
  if (Array.isArray(v)) return v.map(_htmlContentList).join('');
  return _htmlContent(v);
}

// M167：`浏览器` 模块 —— **只在 wasm（网页）里能用**。
//
// 命令行 / Node 上每个函数都报同一条中文错（平台边界，不是「占位」）：
// 同步 VM 没有事件循环，浏览器那套异步 API 塞不进来（见 D81）。
//
// 文案与 Python 侧 `浏览器.py` 的 `无浏览器` **逐字相同** —— 三个宿主一致。
const BROWSER_无 = '这个平台没有浏览器：「浏览器」模块只在网页（wasm）里可用。'
  + '命令行 / Node 上要抓网页请用「html」模块解析、要发请求请用「网络」模块。';

function _noBrowser(name) {
  return new R.Builtin(name, () => { throw R.runErr(BROWSER_无, 0, 0); });
}

const BROWSER_FUNCS = {
  '设文本': _noBrowser('设文本'), '设HTML': _noBrowser('设HTML'),
  '取值': _noBrowser('取值'), '设值': _noBrowser('设值'),
  '加类': _noBrowser('加类'), '删类': _noBrowser('删类'), '切类': _noBrowser('切类'),
  '点击': _noBrowser('点击'), '显示': _noBrowser('显示'), '绑定': _noBrowser('绑定'),
  '存本地': _noBrowser('存本地'), '读本地': _noBrowser('读本地'), '删本地': _noBrowser('删本地'),
  '请求': _noBrowser('请求'),
};

// ---------------------------------------------------------------------------
// M168：`站点` 模块 —— 把一堆页面写成一个能直接传服务器的静态站
//
// 四条规矩（可复现 / 资源指纹 / 不引框架 / 五引擎逐字一致）见 Python 侧模块头。
// 这里只说 Node 这份的两处实现选择：
//
// 1. 指纹用 **FNV-1a 32 位**（不是 SHA-256）：三份实现必须逐位一致，
//    FNV 三行写完，不会跑偏。
// 2. ⚠️ **默认样式表这份字符串是从 Python 侧 `json.dumps` 导出的**，不是手抄 ——
//    M166 踩过：手抄 CSS 漏一个引号，页面照样能看，只有逐字比才抓得住。
// ---------------------------------------------------------------------------

const SITE_默认样式 = "/* 基石站点的默认样式（由「站点」模块写出） */\n:root {\n  --bg: #f7f8fa; --fg: #1f2430; --muted: #5c6370; --line: #e4e7ec;\n  --card: #ffffff; --accent: #1a5fb4; --accent-soft: #eaf1fb;\n}\n* { box-sizing: border-box; }\nbody {\n  margin: 0; background: var(--bg); color: var(--fg);\n  font: 16px/1.75 \"Segoe UI\", \"Microsoft YaHei\", -apple-system, sans-serif;\n}\na { color: var(--accent); text-decoration: none; }\na:hover { text-decoration: underline; }\nheader.top {\n  background: var(--card); border-bottom: 1px solid var(--line);\n  padding: 14px 28px; display: flex; align-items: center; gap: 28px;\n  position: sticky; top: 0; z-index: 2;\n}\n.brand { font-size: 20px; font-weight: 700; letter-spacing: .04em; }\n.brand span { color: var(--muted); font-size: 13px; font-weight: 400; margin-left: 6px; }\nnav a { margin-right: 18px; color: var(--muted); font-size: 15px; }\nnav a.now { color: var(--accent); font-weight: 600; }\nmain { max-width: 960px; margin: 0 auto; padding: 32px 28px 64px; }\nh1 { font-size: 30px; margin: 8px 0 6px; }\nh2 { font-size: 20px; margin: 34px 0 12px; padding-bottom: 6px;\n     border-bottom: 1px solid var(--line); }\n.lead { color: var(--muted); font-size: 17px; margin: 0 0 26px; }\n.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));\n         gap: 14px; }\n.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px;\n        padding: 16px 18px; }\n.card .k { color: var(--muted); font-size: 13px; }\n.card .v { font-size: 26px; font-weight: 700; margin-top: 4px; }\ntable { width: 100%; border-collapse: collapse; background: var(--card);\n        border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }\nth, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--line);\n         vertical-align: top; font-size: 15px; }\nth { background: var(--accent-soft); font-weight: 600; }\ntr:last-child td { border-bottom: none; }\ntd.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }\ncode { background: #eef1f5; padding: 1px 5px; border-radius: 4px;\n       font-family: \"Cascadia Code\", Consolas, monospace; font-size: 14px; }\npre { background: #1f2430; color: #e6e9ef; padding: 14px 16px; border-radius: 8px;\n      overflow-x: auto; }\npre code { background: none; color: inherit; padding: 0; }\nfooter { border-top: 1px solid var(--line); color: var(--muted); font-size: 13px;\n         padding: 18px 28px 40px; max-width: 960px; margin: 0 auto; }\n";

/** 要一个文本参数（只收文本，不收数字 —— 三个宿主要说一样的话）。 */
function _siteStr(name, v) {
  if (typeof v === 'string') return v;
  throw R.typeErr(`「${name}」要传文本，但给的是${R.typeName(v)}（要拼数字请写 文本(值)）`);
}

/** 字典取键（没有就 undefined）。非字典直接报错。 */
function _siteGet(d, key) {
  if (!(d instanceof Map)) {
    throw R.typeErr(`要传字典，但给的是${R.typeName(d)}`);
  }
  return d.has(key) ? d.get(key) : undefined;
}

function _siteGetStr(d, key, 默认) {
  const v = _siteGet(d, key);
  if (v === undefined || v === null) return 默认;
  return _siteStr(key, v);
}

function _sitePairs(v, 谁) {
  const out = [];
  for (const it of v) {
    if (!Array.isArray(it) || it.length < 2) {
      throw R.typeErr(`「${谁}」要是 ["地址", "文字"] 这样的对，但有一项不是`);
    }
    out.push([_siteStr('导航项的地址', it[0]), _siteStr('导航项的文字', it[1])]);
  }
  return out;
}

const SITE_布局默认 = {
  '标题分隔': ' · ', '标题后缀': '', '品牌': '', '品牌副': '',
  '导航项': [], '脚注': '', '语言': 'zh-CN', '样式文件': 'style.css',
  '样式': SITE_默认样式, '网址前缀': '', '指纹': true,
};

function _site布局(opt) {
  if (opt !== undefined && opt !== null && !(opt instanceof Map)) {
    throw R.typeErr(`「布局」的选项要传字典，但给的是${R.typeName(opt)}`);
  }
  const 取 = (k) => (opt && opt.has(k) ? opt.get(k) : undefined);
  const 取文 = (k) => {
    const v = 取(k);
    if (v === undefined || v === null) return SITE_布局默认[k];
    return _siteStr(k, v);
  };
  const 项们 = 取('导航项');
  return new Map([
    ['标题分隔', 取文('标题分隔')],
    ['标题后缀', 取文('标题后缀')],
    ['品牌', 取文('品牌')],
    ['品牌副', 取文('品牌副')],
    ['导航项', (项们 === undefined || 项们 === null) ? [] : _sitePairs(项们, '导航项')],
    ['脚注', 取文('脚注')],
    ['语言', 取文('语言')],
    ['样式文件', 取文('样式文件')],
    ['样式', 取文('样式')],
    ['网址前缀', 取文('网址前缀')],
    ['指纹', (取('指纹') === undefined || 取('指纹') === null) ? true : R.truthy(取('指纹'))],
  ]);
}

function _siteNav(项们, 当前) {
  let out = '';
  for (const [址, 字] of _sitePairs(项们, '导航')) {
    const 类 = (址 === 当前) ? ' class="now"' : '';
    out += `<a${类} href="${_escapeHtml(址)}">${_escapeHtml(字)}</a>`;
  }
  return out;
}

function _sitePage(页, 表) {
  if (!(页 instanceof Map)) {
    throw R.typeErr(`「页面」要传字典，但给的是${R.typeName(页)}`);
  }
  const 标题 = _siteGetStr(页, '标题', '');
  const 正文 = _siteGetStr(页, '正文', '');
  const 路径 = _siteGetStr(页, '路径', '');
  const 页脚注 = _siteGet(页, '脚注');
  const 脚注 = (页脚注 === undefined || 页脚注 === null)
    ? _siteGetStr(表, '脚注', '') : _siteStr('脚注', 页脚注);
  const 后缀 = _siteGetStr(表, '标题后缀', '');
  const 分隔 = _siteGetStr(表, '标题分隔', ' · ');
  const 全标题 = 后缀 ? `${标题}${分隔}${后缀}` : 标题;
  const 品牌副 = _siteGetStr(表, '品牌副', '');
  const 牌子 = 品牌副 ? `<span>${_escapeHtml(品牌副)}</span>` : '';
  const 导航项 = _siteGet(表, '导航项') || [];
  return '<!DOCTYPE html>\n'
    + `<html lang="${_escapeHtml(_siteGetStr(表, '语言', 'zh-CN'))}">\n`
    + '<head>\n'
    + '<meta charset="utf-8">\n'
    + '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    + `<title>${_escapeHtml(全标题)}</title>\n`
    + `<link rel="stylesheet" href="${_escapeHtml(_siteGetStr(表, '样式文件', 'style.css'))}">\n`
    + '</head>\n'
    + '<body>\n'
    + '<header class="top">\n'
    + `  <div class="brand">${_escapeHtml(_siteGetStr(表, '品牌', ''))}${牌子}</div>\n`
    + `  <nav>${_siteNav(导航项, 路径)}</nav>\n`
    + '</header>\n'
    + '<main>\n'
    + `${正文}\n`
    + '</main>\n'
    + `<footer>${_escapeHtml(脚注)}</footer>\n`
    + '</body>\n'
    + '</html>\n';
}

/** FNV-1a 32 位，按 UTF-8 字节算 —— 与 Python / Rust 逐位一致。 */
function _siteFingerprint(s) {
  let h = 2166136261;
  const b = Buffer.from(s, 'utf8');
  for (let i = 0; i < b.length; i++) {
    h ^= b[i];
    h = Math.imul(h, 16777619) >>> 0;
  }
  return h.toString(16).padStart(8, '0');
}

function _siteUrl(前缀, 路径) {
  if (!前缀) return 路径;
  return `${前缀.replace(/\/+$/, '')}/${路径.replace(/^\/+/, '')}`;
}

function _siteSitemap(页们, 前缀) {
  const 址 = new Set();
  for (const it of 页们) {
    if (it instanceof Map) 址.add(_siteGetStr(it, '路径', ''));
    else 址.add(_siteStr('页的路径', it));
  }
  // 🔴 按 **UTF-8 字节** 排（不是 JS 默认的 UTF-16 单元序）：Python / Rust 那边
  // 也是按字节，三家口径必须一致 —— emoji 文件名在两种序下位置不同。
  const 排 = Array.from(址).sort((a, b) => Buffer.compare(Buffer.from(a, 'utf8'),
                                                          Buffer.from(b, 'utf8')));
  let out = '<?xml version="1.0" encoding="UTF-8"?>\n'
    + '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n';
  for (const 一 of 排) out += `  <url><loc>${_escapeHtml(_siteUrl(前缀, 一))}</loc></url>\n`;
  return out + '</urlset>\n';
}

const SITE_FUNCS = {
  '布局': new R.Builtin('布局', (opt) => _site布局(opt)),
  '导航': new R.Builtin('导航', (项们, 当前) => {
    if (!Array.isArray(项们)) throw R.typeErr(`「导航」要传列表，但给的是${R.typeName(项们)}`);
    return _siteNav(项们, 当前 === undefined || 当前 === null ? '' : _siteStr('当前地址', 当前));
  }),
  '页面': new R.Builtin('页面', (页, 表) => _sitePage(页, 表)),
  '资源指纹': new R.Builtin('资源指纹', (内容) => _siteFingerprint(_siteStr('内容', 内容))),
  '渲染': new R.Builtin('渲染', (页们, 表) => {
    if (!Array.isArray(页们)) throw R.typeErr(`「渲染」要传页列表，但给的是${R.typeName(页们)}`);
    return 页们.map((页) => [_siteGetStr(页, '路径', ''), _sitePage(页, 表)]);
  }),
  'sitemap': new R.Builtin('sitemap', (页们, 前缀) => _siteSitemap(
    页们, 前缀 === undefined || 前缀 === null ? '' : _siteStr('前缀', 前缀))),
  '写站': new R.Builtin('写站', (输出目录, 页们, 表) => {
    const 目录 = _siteStr('输出目录', 输出目录);
    if (!Array.isArray(页们)) throw R.typeErr(`「写站」要传页列表，但给的是${R.typeName(页们)}`);
    const 样式 = _siteGetStr(表, '样式', '');
    const 样式文件 = _siteGetStr(表, '样式文件', 'style.css');
    const 指针 = (() => {
      const v = _siteGet(表, '指纹');
      return (v === undefined || v === null) ? true : R.truthy(v);
    })();
    let 真名 = '';
    let 印 = '';
    if (样式) {
      印 = _siteFingerprint(样式);
      if (指针) {
        const i = 样式文件.lastIndexOf('.');
        真名 = (i > 0)
          ? `${样式文件.slice(0, i)}.${印}${样式文件.slice(i)}`
          : `${样式文件}.${印}`;
      } else {
        // 留在原文件名上（有人按固定名字引用它），见 `布局` 里 `指纹` 的说明
        真名 = 样式文件;
      }
    }
    fs.mkdirSync(目录, { recursive: true });
    let 页数 = 0;
    for (const 页 of 页们) {
      const 路径 = _siteGetStr(页, '路径', '');
      if (!路径) throw R.typeErr('「写站」的每一页都要有「路径」（如 index.html）');
      let 内容 = _sitePage(页, 表);
      if (真名 && 真名 !== 样式文件) {
        内容 = 内容.split(`href="${样式文件}"`).join(`href="${真名}"`);
      }
      const 全 = require('path').join(目录, 路径);
      fs.mkdirSync(require('path').dirname(全), { recursive: true });
      fs.writeFileSync(全, 内容, 'utf8');
      页数 += 1;
    }
    if (真名) fs.writeFileSync(require('path').join(目录, 真名), 样式, 'utf8');
    fs.writeFileSync(require('path').join(目录, 'sitemap.xml'),
      _siteSitemap(页们, _siteGetStr(表, '网址前缀', '')), 'utf8');
    return new Map([['页面数', 页数], ['样式文件', 真名], ['指纹', 印],
                    ['sitemap', 'sitemap.xml']]);
  }),
};

// ---------------------------------------------------------------------------
// M171：`打包` —— 把「一个程序」打成**一个带清单的 zip**（清单 / 生成 / 校验）
//
// 设计意图与三条硬规矩见 Python 侧 `jishi/stdlib/打包.py` 模块头。这里只说
// JS 这份的两处实现选择：
//
// 1. **`清单.json` 的文本是手写拼出来的**，不用 `JSON.stringify`：那个的键序
//    跟着对象走、转义规则也各有说法，而这份文本是**三宿主的契约**（同一个
//    目录在哪跑都得同字节）。
// 2. **zip 走 `writeZipEntry` + 定死的时间**（与 `压缩.打包确定` 同源）。
//    跨引擎本来就**不**逐字节相同（这边 deflate、Rust 侧 stored），口径是
//    「同一个引擎、同样输入 ⇒ 同样的字节」+「条目清单一致」。
// ---------------------------------------------------------------------------

const PACK_清单名 = '清单.json';

/** 选项里的一个文本键。没给（或空）就是空串。 */
function _packOpt(选项, 键) {
  if (选项 === undefined || 选项 === null) return '';
  if (!(选项 instanceof Map)) {
    throw R.valueErr('「选项」要传字典，例如 {"名字": "记账", "版本": "1.0.0"}');
  }
  const v = 选项.has(键) ? 选项.get(键) : undefined;
  if (v === undefined || v === null) return '';
  if (typeof v !== 'string') {
    throw R.valueErr(`选项「${键}」要传文本，给的是 ${R.pyStr(v)}`);
  }
  return v;
}

/** 把「来源」摊成 `[[条目名, 磁盘路径]]`。口径与 `压缩.打包确定` 相同。 */
function _packFiles(来源, 基础) {
  const out = [];
  if (Array.isArray(来源)) {
    if (!基础) {
      throw R.valueErr('传文件列表时必须给「基础」目录 —— 否则算不出包里的条目名',
        undefined, undefined,
        '例如 打包.清单(["发布/记账.exe"], {"名字": "记账", "基础": "发布"})');
    }
    for (const one of 来源) {
      const full = pText(one);
      out.push([_arcName(full, 基础), full]);
    }
  } else {
    const root = pText(来源);
    const st = statOf(root);
    if (!st || !st.isDirectory()) {
      throw R.valueErr(`「${root}」既不是目录也不是列表，无法确定要打包什么`);
    }
    const b = 基础 || root;
    for (const full of walkFiles(root)) out.push([_arcName(full, b), full]);
  }
  for (const [arc] of out) {
    if (!arc || arc.endsWith('/')) throw R.valueErr(`「${arc}」不是有效的包内路径`);
  }
  return out;
}

/** UTF-8 字节序比较（Python 默认按码点、JS 按 UTF-16 单元 ⇒ 遇 emoji 会分叉）。 */
function _packCmp(a, b) {
  return Buffer.compare(Buffer.from(a, 'utf8'), Buffer.from(b, 'utf8'));
}

/** 收集 + 摘要。返回 `{文件们, 总, 内容}`；`内容` 给 `生成` 写 zip 用（不重读）。 */
function _packScan(项们) {
  const 文件们 = [];
  const 内容 = new Map();
  let 总 = 0;
  for (const [arc, full] of 项们) {
    let data;
    try {
      data = fs.readFileSync(full);
    } catch (e) {
      throw R.runErr(`读「${full}」失败：${e.message}`);
    }
    总 += data.length;
    文件们.push({
      路径: arc,
      大小: data.length,
      sha256: _crypto.createHash('sha256').update(data).digest('hex'),
    });
    内容.set(arc, data);
  }
  文件们.sort((a, b) => _packCmp(a.路径, b.路径));
  return { 文件们, 总, 内容 };
}

function _packManifestValue(选, 文件们, 总) {
  return new Map([
    ['名字', 选['名字']],
    ['版本', 选['版本']],
    ['平台', 选['平台']],
    ['入口', 选['入口']],
    ['文件数', 文件们.length],
    ['字节数', 总],
    ['文件们', 文件们.map((f) => new Map([
      ['路径', f.路径], ['大小', f.大小], ['sha256', f.sha256],
    ]))],
  ]);
}

/** JSON 字符串转义 —— 与 Python `_转义` / Rust `转义` 逐字一致。 */
function _packEsc(s) {
  let out = '';
  for (const ch of String(s)) {
    const o = ch.codePointAt(0);
    if (ch === '"') out += '\\"';
    else if (ch === '\\') out += '\\\\';
    else if (o < 0x20) out += `\\u${o.toString(16).padStart(4, '0')}`;
    else out += ch;
  }
  return out;
}

/** 清单 → `清单.json` 的文本。**格式是契约**，改它等于换包格式。 */
function _packJsonText(选, 文件们, 总) {
  const L = ['{'];
  L.push(`  "名字": "${_packEsc(选['名字'])}",`);
  L.push(`  "版本": "${_packEsc(选['版本'])}",`);
  L.push(`  "平台": "${_packEsc(选['平台'])}",`);
  L.push(`  "入口": "${_packEsc(选['入口'])}",`);
  L.push(`  "文件数": ${文件们.length},`);
  L.push(`  "字节数": ${总},`);
  if (!文件们.length) {
    L.push('  "文件们": []');
  } else {
    L.push('  "文件们": [');
    for (let i = 0; i < 文件们.length; i += 1) {
      const f = 文件们[i];
      L.push(`    {"路径": "${_packEsc(f.路径)}", "大小": ${f.大小}, `
        + `"sha256": "${_packEsc(f.sha256)}"}${i < 文件们.length - 1 ? ',' : ''}`);
    }
    L.push('  ]');
  }
  L.push('}');
  return `${L.join('\n')}\n`;
}

function _packZipName(选) {
  if (!选['名字']) {
    throw R.valueErr('「名字」不能空 —— 它是产物的文件名，也是安装后在开始菜单里显示的名字');
  }
  for (const [段, 键] of [[选['名字'], '名字'], [选['版本'], '版本'], [选['平台'], '平台']]) {
    for (const bad of ['/', '\\', ':', '*', '?', '"', '<', '>', '|']) {
      if (段.includes(bad)) {
        throw R.valueErr(`选项「${键}」里不能有「${bad}」—— 它要当文件名用`);
      }
    }
  }
  const 部 = [选['名字']];
  if (选['版本']) 部.push(选['版本']);
  if (选['平台']) 部.push(选['平台']);
  return `${部.join('-')}.zip`;
}

/** 写一个 zip（条目顺序 = 给定顺序，时间定死 1980-01-01）—— 与 `打包确定` 同源。 */
function _packWriteZip(包, 条目) {
  const chunks = [];
  const central = [];
  let offset = 0;
  const push = (x) => { chunks.push(x); offset += x.length; };
  const 定死 = new Date(1980, 0, 1, 0, 0, 0).getTime();
  for (const [name, data] of 条目) {
    const localOff = offset;
    central.push(writeZipEntry(push, name, data, localOff, 定死));
  }
  const cd = Buffer.concat(central.map(centralEntry));
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(0, 4);
  eocd.writeUInt16LE(0, 6);
  eocd.writeUInt16LE(central.length, 8);
  eocd.writeUInt16LE(central.length, 10);
  eocd.writeUInt32LE(cd.length, 12);
  eocd.writeUInt32LE(offset, 16);
  eocd.writeUInt16LE(0, 20);
  fs.writeFileSync(包, Buffer.concat([...chunks, cd, eocd]));
}

function _packResult(通过, 问题们, 清单) {
  return new Map([['通过', 通过], ['问题们', 问题们], ['清单', 清单]]);
}

const PACK_FUNCS = {
  '清单': new R.Builtin('清单', (来源, 选项) => {
    const 选 = {
      名字: _packOpt(选项, '名字'),
      版本: _packOpt(选项, '版本'),
      平台: _packOpt(选项, '平台'),
      入口: _packOpt(选项, '入口'),
      基础: _packOpt(选项, '基础'),
    };
    const 项们 = _packFiles(来源, 选.基础);
    const { 文件们, 总 } = _packScan(项们);
    return _packManifestValue(选, 文件们, 总);
  }),

  '生成': new R.Builtin('生成', (来源, 输出目录, 选项) => {
    const 选 = {
      名字: _packOpt(选项, '名字'),
      版本: _packOpt(选项, '版本'),
      平台: _packOpt(选项, '平台'),
      入口: _packOpt(选项, '入口'),
      基础: _packOpt(选项, '基础'),
    };
    const 项们 = _packFiles(来源, 选.基础);
    if (!项们.length) {
      throw R.valueErr(`「${pText(来源)}」里没有文件，打出来的包会是空的`);
    }
    if (项们.some((x) => x[0] === PACK_清单名)) {
      throw R.valueErr(`「${PACK_清单名}」是打包自己用的名字，不能当程序文件`,
        undefined, undefined, '改个名（例如 说明.json）；包根上那份是清单，会被覆盖');
    }
    const { 文件们, 总, 内容 } = _packScan(项们);
    const 名 = _packZipName(选);
    const 出目录 = pText(输出目录);
    try {
      fs.mkdirSync(出目录, { recursive: true });
    } catch (e) {
      throw R.runErr(`建输出目录「${出目录}」失败：${e.message}`);
    }
    const 包 = _path.join(出目录, 名);
    const 条目 = [[PACK_清单名, Buffer.from(_packJsonText(选, 文件们, 总), 'utf8')]];
    for (const f of 文件们) 条目.push([f.路径, 内容.get(f.路径)]);
    try {
      _packWriteZip(包, 条目);
    } catch (e) {
      throw R.runErr(`写包「${包}」失败：${e.message}`);
    }
    return 包;
  }),

  '安装包': new R.Builtin('安装包', (包路径, 存根路径, 输出路径) => {
    const 包 = pText(包路径);
    const 存根 = pText(存根路径);
    let 包字节;
    let 存根字节;
    try {
      包字节 = fs.readFileSync(包);
      存根字节 = fs.readFileSync(存根);
    } catch (e) {
      throw R.runErr(`读文件失败：${e.message}`);
    }
    let 条目们;
    try {
      条目们 = readZipEntries(包字节, false);
    } catch (e) {
      throw R.valueErr(`「${包}」不是「打包.生成」造的包，接不成安装包：${e.message}`,
        undefined, undefined, '先 打包.生成(来源, 目录, 选项) 出一个包，再接安装包');
    }
    const 清 = 条目们.find((e) => e.name === PACK_清单名);
    if (!清) {
      throw R.valueErr(`「${包}」里没有「${PACK_清单名}」，接不成安装包`,
        undefined, undefined, '先 打包.生成(来源, 目录, 选项) 出一个包，再接安装包');
    }
    let m;
    try {
      m = JSON.parse(清.data.toString('utf8'));
    } catch (e) {
      throw R.valueErr(`「${PACK_清单名}」解析失败：${e.message}`);
    }
    const 名 = (typeof m['名字'] === 'string' && m['名字']) ? m['名字'] : '程序';
    let 出 = (输出路径 === undefined || 输出路径 === null) ? '' : pText(输出路径);
    if (!出) {
      const 部 = [名];
      for (const k of ['版本', '平台']) {
        if (typeof m[k] === 'string' && m[k]) 部.push(m[k]);
      }
      出 = _path.join(_path.dirname(包), `${部.join('-')}-setup.exe`);
    }
    const 尾 = Buffer.alloc(24);
    尾.writeUInt32LE(存根字节.length >>> 0, 0);
    尾.writeUInt32LE(Math.floor(存根字节.length / 4294967296), 4);
    尾.writeUInt32LE(包字节.length >>> 0, 8);
    尾.writeUInt32LE(Math.floor(包字节.length / 4294967296), 12);
    尾.write('JISHIPK1', 16, 'latin1');
    try {
      fs.mkdirSync(_path.dirname(出), { recursive: true });
      fs.writeFileSync(出, Buffer.concat([存根字节, 包字节, 尾]));
    } catch (e) {
      throw R.runErr(`写安装包「${出}」失败：${e.message}`);
    }
    return 出;
  }),

  '校验': new R.Builtin('校验', (包路径) => {
    const 路径 = pText(包路径);
    let buf;
    try {
      buf = fs.readFileSync(路径);
    } catch (e) {
      return _packResult(false, [`打不开包「${路径}」：${e.message}`], new Map());
    }
    let 条目们;
    try {
      条目们 = readZipEntries(buf, false);
    } catch (e) {
      return _packResult(false, [`打不开包「${路径}」：${e.message}`], new Map());
    }
    const 清 = 条目们.find((e) => e.name === PACK_清单名);
    if (!清) {
      return _packResult(false,
        [`包里没有「${PACK_清单名}」—— 这不是「打包.生成」造的包（或者是老包）`],
        new Map());
    }
    let m;
    try {
      m = JSON.parse(清.data.toString('utf8'));
    } catch (e) {
      return _packResult(false, [`「${PACK_清单名}」读不出来：${e.message}`], new Map());
    }
    if (!m || !Array.isArray(m['文件们'])) {
      return _packResult(false, [`「${PACK_清单名}」格式不对：缺「文件们」那一项`], new Map());
    }
    const 应有 = [];
    for (const f of m['文件们']) {
      if (!f || typeof f['路径'] !== 'string') {
        return _packResult(false, [`「${PACK_清单名}」里有一条不认识（缺「路径」）`], new Map());
      }
      应有.push([f['路径'], f['大小'], f['sha256']]);
    }
    应有.sort((a, b) => _packCmp(a[0], b[0]));

    const 实测 = new Map();
    for (const e of 条目们) {
      if (e.name === PACK_清单名 || e.name.endsWith('/')) continue;
      实测.set(e.name, e.data);
    }

    const 问题们 = [];
    for (const [名, 应该大小, 应该摘要] of 应有) {
      if (!实测.has(名)) { 问题们.push(`缺文件：${名}`); continue; }
      const data = 实测.get(名);
      if (typeof 应该大小 === 'number' && data.length !== 应该大小) {
        问题们.push(`大小不对：${名}（清单 ${应该大小}，实际 ${data.length}）`);
        continue;
      }
      if (typeof 应该摘要 === 'string'
          && _crypto.createHash('sha256').update(data).digest('hex') !== 应该摘要) {
        问题们.push(`内容不对：${名}（sha256 与清单不符）`);
      }
    }
    const 应有名 = new Set(应有.map((x) => x[0]));
    const 多出 = [...实测.keys()].filter((n) => !应有名.has(n))
      .sort((a, b) => _packCmp(a, b));
    for (const 名 of 多出) 问题们.push(`清单外多出：${名}`);

    const 清单值 = new Map([
      ['名字', typeof m['名字'] === 'string' ? m['名字'] : ''],
      ['版本', typeof m['版本'] === 'string' ? m['版本'] : ''],
      ['平台', typeof m['平台'] === 'string' ? m['平台'] : ''],
      ['入口', typeof m['入口'] === 'string' ? m['入口'] : ''],
      ['文件数', typeof m['文件数'] === 'number' ? m['文件数'] : 0],
      ['字节数', typeof m['字节数'] === 'number' ? m['字节数'] : 0],
      ['文件们', 应有.map((x) => new Map([
        ['路径', x[0]],
        ['大小', typeof x[1] === 'number' ? x[1] : 0],
        ['sha256', typeof x[2] === 'string' ? x[2] : ''],
      ]))],
    ]);
    return _packResult(问题们.length === 0, 问题们, 清单值);
  }),
};

// ---------------------------------------------------------------------------
// M173：`窗口` —— 把程序开成一个窗口（应用模式，无地址栏）
//
// 设计意图与三条边界见 Python 侧 `jishi/stdlib/窗口.py` 模块头。JS 这份有一个
// **宿主特有的问题**：本宿主的 VM 是同步的，`窗口.等` 必须**阻塞主线程**，
// 可阻塞了事件循环，同进程里的 HTTP 服务就再也 accept 不了 —— 页面根本加载不出来。
//
// ⇒ 服务放到**子进程**（`node -e <一段服务代码>`），主线程只负责等浏览器进程。
// 等效于别的宿主里的「服务线程」，只是这边用了进程。
// ---------------------------------------------------------------------------

const WINDOW_引擎前缀 = '/__jishi/';

//: 子进程里跑的那段服务代码。参数：`目录 引擎目录 端口文件`。
//: 端口由子进程自己挑（0 = 系统分配），写进端口文件，父进程轮询读。
const WINDOW_服务代码 = `
const http = require('http'), fs = require('fs'), path = require('path');
const dir = process.argv[1], eng = process.argv[2] || '', out = process.argv[3];
const MIME = { '.html': 'text/html; charset=utf-8', '.htm': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.wasm': 'application/wasm', '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
  '.gif': 'image/gif', '.ico': 'image/x-icon', '.woff2': 'font/woff2',
  '.txt': 'text/plain; charset=utf-8' };
const 根 = path.resolve(dir), 引擎根 = eng ? path.resolve(eng) : '';
const srv = http.createServer((req, res) => {
  let 路;
  try { 路 = decodeURIComponent(req.url.split('?')[0].split('#')[0]); }
  catch (e) { res.writeHead(400); res.end('路径解码失败'); return; }
  let 文件;
  if (引擎根 && 路.startsWith('${WINDOW_引擎前缀}')) {
    文件 = path.resolve(引擎根, 路.slice('${WINDOW_引擎前缀}'.length));
    if (!文件.startsWith(引擎根)) { res.writeHead(404); res.end('找不到'); return; }
  } else {
    const 相对 = 路.replace(/^\\/+/, '') || 'index.html';
    文件 = path.resolve(根, 相对);
    if (!文件.startsWith(根)) { res.writeHead(404); res.end('找不到'); return; }
  }
  fs.readFile(文件, (e, 数据) => {
    if (e) {
      if (文件.endsWith('favicon.ico')) { res.writeHead(204); res.end(); return; }
      res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end('找不到');
      return;
    }
    res.writeHead(200, {
      'Content-Type': MIME[path.extname(文件).toLowerCase()] || 'application/octet-stream',
      'Content-Length': 数据.length,
      'Cache-Control': 'no-store',
    });
    res.end(数据);
  });
});
srv.listen(0, '127.0.0.1', () => { fs.writeFileSync(out, String(srv.address().port)); });
`;

const _win服务 = new Map();     // 目录 -> { 子, 端口 }
const _win表 = [];              // 序号-1 -> { 子, 目录, 网址 }

function _win引擎() {
  const 候选 = [];
  try {
    候选.push(_path.join(_path.dirname(process.execPath), 'site'));
    候选.push(_path.join(_path.dirname(process.execPath), 'wasm'));
  } catch (e) { /* 忽略 */ }
  for (const d of 候选) {
    try {
      if (fs.statSync(_path.join(d, 'jishi-wasm.js')).isFile()
          && fs.statSync(_path.join(d, 'jishi_wasm.wasm')).isFile()) return d;
    } catch (e) { /* 继续找 */ }
  }
  return '';
}

function _win浏览器(指定) {
  if (Array.isArray(指定)) {
    if (!指定.length) throw R.valueErr('「浏览器」给了个空列表');
    const 命令 = 指定.map((x) => String(x));
    if (!命令[0].trim()) throw R.valueErr('「浏览器」的第一项不能是空的（那应该是可执行文件或命令）');
    return 命令;
  }
  if (typeof 指定 === 'string' && 指定.trim()) {
    if (!fs.existsSync(指定)) throw R.valueErr(`「浏览器」指向的文件不存在：${指定}`);
    return [指定];
  }
  const 候选 = [];
  if (process.platform === 'win32') {
    const 基 = [process.env.PROGRAMFILES || 'C:\\Program Files',
      process.env['PROGRAMFILES(X86)'] || 'C:\\Program Files (x86)',
      process.env.LOCALAPPDATA || ''];
    for (const b of 基) {
      if (!b) continue;
      候选.push(_path.join(b, 'Microsoft', 'Edge', 'Application', 'msedge.exe'));
      候选.push(_path.join(b, 'Google', 'Chrome', 'Application', 'chrome.exe'));
    }
  } else if (process.platform === 'darwin') {
    候选.push('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
      '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
      '/Applications/Chromium.app/Contents/MacOS/Chromium');
  } else {
    for (const 名 of ['google-chrome', 'chromium', 'chromium-browser', 'microsoft-edge']) {
      const p = whichBin(名);
      if (p) 候选.push(p);
    }
  }
  for (const p of 候选) {
    try { if (fs.statSync(p).isFile()) return [p]; } catch (e) { /* 下一个 */ }
  }
  throw R.valueErr('找不到能开应用窗口的浏览器（要 Edge / Chrome / Chromium 这类）',
    undefined, undefined,
    '装一个 Chromium 内核的浏览器，或者用「浏览器」选项指定它的可执行文件路径');
}

function whichBin(名) {
  const 路径 = (process.env.PATH || '').split(process.platform === 'win32' ? ';' : ':');
  for (const d of 路径) {
    if (!d) continue;
    const p = _path.join(d, 名);
    try { if (fs.statSync(p).isFile()) return p; } catch (e) { /* 继续 */ }
  }
  return '';
}

/** 起（或复用）一个目录上的服务，返回端口。服务跑在**子进程**里。 */
function _win起服务(目录) {
  const 键 = _path.resolve(目录).toLowerCase();
  const 有 = _win服务.get(键);
  if (有) return 有.端口;
  const 端口文件 = _path.join(_os.tmpdir(), `jishi-win-${process.pid}-${_win服务.size}.port`);
  try { fs.unlinkSync(端口文件); } catch (e) { /* 本来就没有 */ }
  const 子 = _cp.spawn(process.execPath,
    ['-e', WINDOW_服务代码, 目录, _win引擎(), 端口文件],
    { stdio: 'ignore', windowsHide: true });
  // 等子进程把端口写出来（同步宿主不能 await，只能小睡轮询）
  const 缓冲 = new Int32Array(new SharedArrayBuffer(4));
  for (let i = 0; i < 100; i += 1) {
    try {
      const 文 = fs.readFileSync(端口文件, 'utf8').trim();
      if (文) {
        const 端口 = Number(文);
        _win服务.set(键, { 子, 端口 });
        return 端口;
      }
    } catch (e) { /* 还没写出来 */ }
    Atomics.wait(缓冲, 0, 0, 50);
  }
  try { 子.kill(); } catch (e) { /* 已经没了 */ }
  throw R.runErr('本地服务起不来（子进程没给出端口）');
}

function _win停服务(目录) {
  const 键 = _path.resolve(目录).toLowerCase();
  const 有 = _win服务.get(键);
  if (!有) return;
  _win服务.delete(键);
  try { 有.子.kill(); } catch (e) { /* 已经没了 */ }
}

const WINDOW_FUNCS = {
  '打开': new R.Builtin('打开', (选项) => {
    if (!(选项 instanceof Map)) {
      throw R.typeErr('「窗口.打开」要一个字典', undefined, undefined,
        '例如 {"标题": "计算器", "页面": "计算器.html"}');
    }
    // M174：`原生` 三态。缺省 = 自动（本宿主永远没有原生能力 ⇒ 应用模式）；
    // `真` = 一定要原生 ⇒ 明确报错（不静默降级，那是欺骗）；`假` = 应用模式。
    if (选项.has('原生')) {
      const 原 = 选项.get('原生');
      if (原 !== undefined && 原 !== null && typeof 原 !== 'boolean') {
        throw R.valueErr(`「原生」要传真或假，给的是「${R.display(原)}」`,
          undefined, undefined,
          '不写就是自动：宿主能开原生窗口就开，不能就用浏览器应用模式');
      }
      if (原 === true) {
        throw R.runErr('这个宿主没有原生窗口能力（只有 Rust 发行版有）',
          undefined, undefined,
          '去掉「原生: 真」，或写「原生: 假」用浏览器应用模式');
      }
    }
    const 取文 = (k, 默认) => {
      const v = 选项.has(k) ? 选项.get(k) : undefined;
      if (v === undefined || v === null) return 默认;
      if (typeof v !== 'string') throw R.valueErr(`「${k}」要传文本`);
      return v;
    };
    const 取数 = (k, 默认) => {
      const v = 选项.has(k) ? 选项.get(k) : undefined;
      if (v === undefined || v === null) return 默认;
      if (typeof v !== 'number' || !Number.isInteger(v)) {
        throw R.valueErr(`「${k}」要传整数（像素）`);
      }
      return v;
    };
    const 页面 = 取文('页面', '');
    const 服务 = 取文('服务', '');
    const 宽 = 取数('宽', 900);
    const 高 = 取数('高', 640);
    if (页面 && 服务) {
      throw R.valueErr('「页面」和「服务」只能给一个：页面我们自己起服务，服务是你已经有的地址');
    }
    let 网址;
    let 目录 = '';
    if (服务) {
      网址 = 服务;
    } else if (页面) {
      const 全 = _path.resolve(页面);
      let 是文件 = false;
      try { 是文件 = fs.statSync(全).isFile(); } catch (e) { 是文件 = false; }
      if (!是文件) throw R.valueErr(`找不到页面：${页面}`);
      目录 = _path.dirname(全);
      const 端口 = _win起服务(目录);
      网址 = `http://127.0.0.1:${端口}/${encodeURIComponent(_path.basename(全))}`;
    } else {
      throw R.valueErr('「窗口.打开」要指定「页面」或「服务」');
    }
    const 命令 = _win浏览器(选项.has('浏览器') ? 选项.get('浏览器') : undefined);
    const 配置 = _path.join(_os.homedir(), '.jishi', 'windows',
      _crypto.createHash('sha256').update(页面 || 网址, 'utf8').digest('hex').slice(0, 16));
    try { fs.mkdirSync(配置, { recursive: true }); } catch (e) { /* 有就算了 */ }
    命令.push(`--app=${网址}`, `--window-size=${宽},${高}`, `--user-data-dir=${配置}`,
      '--no-first-run', '--no-default-browser-check', '--disable-features=TranslateUI');
    const 子 = _cp.spawn(命令[0], 命令.slice(1), { stdio: 'ignore', windowsHide: true });
    _win表.push({ 子, 目录, 网址 });
    return _win表.length;
  }),

  '关闭': new R.Builtin('关闭', (句柄) => {
    // M174：省略句柄 = 「关掉我自己所在的窗口」，那条路是给**页面里的代码**用的
    // （走 ABI 3）。脚本里省略就是误用 —— 报错比关掉一个猜出来的窗口好。
    if (句柄 === undefined || 句柄 === null) {
      throw R.runErr('「窗口.关闭()」省略句柄是给**页面里的代码**用的（关掉自己所在的窗口）',
        undefined, undefined, '脚本里请写 窗口.关闭(句柄)；页面里（跑在 wasm 里）才用无参形式');
    }
    const 记录 = _win记录(句柄);
    if (!记录) return false;
    try { 记录.子.kill(); } catch (e) { /* 已经没了 */ }
    return true;
  }),

  '设标题': new R.Builtin('设标题', () => {
    // 这个宿主做不到（M174）—— 明确报错，不静默返回假。
    throw R.runErr('这个宿主改不了窗口标题（只有 Rust 侧的原生窗口能。）',
      undefined, undefined,
      '页面里改 document.title 即可（--app 窗口的标题栏显示的就是它）；'
      + '或者在 Rust 发行版里用 窗口.打开({原生: 真})');
  }),

  // M175：桌面集成 —— **Node 宿主一个都做不到**（它连窗口都是借浏览器的）。
  //
  // 🔴 为什么不留空的（返回假 / 什么都不做）：那样用户会以为设上了，跑起来
  //    才发现右键没反应 —— 与 M174 那条「白窗口、零报错」是同一类坑。
  //    **没有能力就说没有，并告诉他去哪能有。**
  '设菜单': new R.Builtin('设菜单', () => _要原生('设菜单', '用 Rust 发行版（窗口.打开 默认就是原生窗口）')),
  '设右键菜单': new R.Builtin('设右键菜单', () => _要原生('设右键菜单', '用 Rust 发行版；页面里也可以自己处理 contextmenu 事件')),
  '设托盘': new R.Builtin('设托盘', () => _要原生('设托盘', '用 Rust 发行版')),
  '关托盘': new R.Builtin('关托盘', () => _要原生('关托盘', '用 Rust 发行版')),
  '单实例': new R.Builtin('单实例', () => _要原生('单实例', '用 Rust 发行版')),
  '选文件': new R.Builtin('选文件', () => _要原生('选文件', '用 Rust 发行版；页面里可以用 <input type="file">')),
  '存文件': new R.Builtin('存文件', () => _要原生('存文件', '用 Rust 发行版；页面里可以用 <a download>')),
  '当': new R.Builtin('当', () => _要原生('当', '它要写进页面的基石代码里（跑在 wasm 里的那份），不是脚本里')),

  '在运行': new R.Builtin('在运行', (句柄) => {
    const 记录 = _win记录(句柄);
    return !!记录 && _win活着(记录);
  }),

  '等': new R.Builtin('等', (句柄) => {
    const 记录 = _win记录(句柄);
    if (!记录) return 0;      // 已经关掉了：等一个关掉的窗口 = 立刻返回
    // 同步宿主只能**轮询 OS**：`process.kill(pid, 0)` 是系统调用，不依赖事件循环。
    // 服务在子进程里，所以主线程阻塞不影响页面加载。
    const 缓冲 = new Int32Array(new SharedArrayBuffer(4));
    while (_win活着(记录)) Atomics.wait(缓冲, 0, 0, 50);
    const 码 = 记录.子.exitCode === null ? 0 : Number(记录.子.exitCode);
    _win收尾(句柄);
    return 码;
  }),

  '全部关闭': new R.Builtin('全部关闭', () => {
    let 数 = 0;
    for (let i = 0; i < _win表.length; i += 1) {
      if (!_win表[i]) continue;
      if (WINDOW_FUNCS["关闭"].fn(i + 1)) 数 += 1;
      _win收尾(i + 1);
    }
    return 数;
  }),
};

/** M175：这个宿主没有原生窗口 —— 统一报错（不静默返回假）。 */
function _要原生(名, 提示) {
  throw R.runErr('这个宿主没有原生窗口能力，「窗口.' + 名 + '」用不了'
    + '（Node 宿主只能走浏览器应用模式）',
    undefined, undefined, 提示);
}

function _win记录(句柄) {
  if (typeof 句柄 !== 'number' || !Number.isInteger(句柄) || 句柄 < 1) return null;
  return _win表[句柄 - 1] || null;
}

function _win活着(记录) {
  try {
    // 信号 0 = 只检查进程在不在
    process.kill(记录.子.pid, 0);
    return true;
  } catch (e) {
    return false;
  }
}

function _win收尾(句柄) {
  const 记录 = _win记录(句柄);
  if (!记录) return;
  _win表[句柄 - 1] = null;
  if (记录.目录 && !_win表.some((x) => x && x.目录 === 记录.目录)) {
    _win停服务(记录.目录);
  }
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
  // M166：页面构件（表单 / 表格 / 布局 / 卡片）
  //
  // 📌 输出格式是**契约**（与 Python / Rust 逐字节相同）。
  // 参数一律「位置 + 默认值」，不用关键字 —— 宿主的内建签名里没有放关键字的位置。
  '输入框': new R.Builtin('输入框', (名字, 说明 = '', 类型 = 'text', 值 = '') => {
    _needText('输入框', 名字);
    const pairs = [['type', 类型 || 'text'], ['name', 名字]];
    if (值 !== '' && 值 !== undefined && 值 !== null) pairs.push(['value', pText(值)]);
    return new RawHtml(_labelWrap(说明, _voidEl('input', pairs)));
  }),
  '多行文本': new R.Builtin('多行文本', (名字, 说明 = '', 值 = '', 行数 = 4) => {
    _needText('多行文本', 名字);
    const 正文 = 值 === undefined || 值 === null ? '' : pText(值);
    const 行 = 行数 === undefined || 行数 === null ? '4' : pText(行数);
    const inner = `<textarea name="${_escapeHtml(名字)}" rows="${_escapeHtml(行)}"`
      + `>${_escapeHtml(正文)}</textarea>`;
    return new RawHtml(_labelWrap(说明, inner));
  }),
  '下拉': new R.Builtin('下拉', (名字, 选项, 说明 = '', 选中 = '') => {
    _needText('下拉', 名字);
    const 选中文 = 选中 === undefined || 选中 === null ? '' : pText(选中);
    const 片 = seqFor('下拉', 选项).map((项) => {
      const 值 = pText(项);
      const 标记 = 选中文 !== '' && 值 === 选中文 ? ' selected' : '';
      return `<option value="${_escapeHtml(值)}"${标记}>${_escapeHtml(值)}</option>`;
    }).join('');
    const inner = `<select name="${_escapeHtml(名字)}">${片}</select>`;
    // ⚠️ 说明是**第 3 个**参数（第 2 个是选项）—— 写成第 2 个会把整个选项列表
    // 当标签文字印出来（Rust 侧踩过同一个。
    return new RawHtml(_labelWrap(说明, inner));
  }),
  '多选': new R.Builtin('多选', (名字, 选项, 说明 = '', 选中们 = null) => {
    _needText('多选', 名字);
    const 已选 = (选中们 === null || 选中们 === undefined)
      ? []
      : seqFor('多选', 选中们).map(pText);
    const 片 = seqFor('多选', 选项).map((项) => {
      const 值 = pText(项);
      const pairs = [['type', 'checkbox'], ['name', 名字], ['value', 值]];
      // ⚠️ 只在勾了的时候才加 `checked`（布尔属性不带 `=""`）
      if (已选.includes(值)) pairs.push(['checked', null]);
      return `<label>${_voidEl('input', pairs)}${_escapeHtml(值)}</label>`;
    }).join('');
    const 题 = (说明 === undefined || 说明 === null || pText(说明) === '')
      ? '' : `<legend>${_escapeHtml(pText(说明))}</legend>`;
    return new RawHtml(`<fieldset class="字段组">${题}${片}</fieldset>`);
  }),
  '按钮': new R.Builtin('按钮', (文字, 类型 = 'submit') => {
    _needText('按钮', 文字);
    return new RawHtml(
      `<button type="${_escapeHtml(类型 || 'submit')}">${_escapeHtml(文字)}</button>`);
  }),
  '表单': new R.Builtin('表单', (内容, 提交到, 方法 = 'post') => {
    _needText('表单', 提交到);
    return new RawHtml(
      `<form action="${_escapeHtml(提交到)}" method="${_escapeHtml(方法 || 'post')}"`
      + `>${_htmlContentList(内容)}</form>`);
  }),
  '由表格': new R.Builtin('由表格', (表, 斑马纹 = false) => {
    const t = asTable(表);
    const 类 = R.truthy(斑马纹) ? '基石表格 斑马纹' : '基石表格';
    const 头 = t.headers.map((h) => `<th>${_htmlContent(h)}</th>`).join('');
    const 体 = t.rows.map((行) => `<tr>${行.map((c) => `<td>${_htmlContent(c)}</td>`).join('')}</tr>`).join('');
    return new RawHtml(
      `<table class="${类}"><thead><tr>${头}</tr></thead><tbody>${体}</tbody></table>`);
  }),
  '页面': new R.Builtin('页面', (标题, 内容, 导航 = '') => {
    _needText('页面', 标题);
    const 题 = _escapeHtml(标题);
    let 导航片 = '';
    if (Array.isArray(导航)) {
      const 链接们 = 导航.map((项) => {
        const 对 = seqFor('页面', 项);
        if (对.length < 2) return '';
        return `<a href="${_escapeHtml(pText(对[1]))}">${_escapeHtml(pText(对[0]))}</a>`;
      }).join('');
      导航片 = `<nav>${链接们}</nav>`;
    } else if (导航 !== '' && 导航 !== null && 导航 !== undefined) {
      const t = _htmlContent(导航);
      if (t !== '') 导航片 = `<nav>${t}</nav>`;
    }
    const out = '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
      + '<meta charset="utf-8" />\n'
      + '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
      + `<title>${题}</title>\n`
      + `<style>${HTML_样式}</style>\n`
      + '</head>\n<body>\n'
      + `<header><h1>${题}</h1>${导航片}</header>\n`
      + `<main>${_htmlContentList(内容)}</main>\n`
      + '<footer>由基石生成</footer>\n'
      + '</body>\n</html>';
    return new RawHtml(out);
  }),
  '卡片': new R.Builtin('卡片', (标题, 内容) => {
    _needText('卡片', 标题);
    return new RawHtml(
      `<section class="卡片"><h2>${_escapeHtml(标题)}</h2>${_htmlContentList(内容)}</section>`);
  }),
  '提示': new R.Builtin('提示', (级别, 文本) => {
    const 名 = pText(级别);
    if (名 !== '成功' && 名 !== '警告' && 名 !== '错误') {
      throw R.typeErr(`「提示」的级别只认成功、警告、错误，收到「${名}」`);
    }
    return new RawHtml(`<div class="提示 提示-${名}">${_htmlContentList(文本)}</div>`);
  }),
  // M107：解析方向
  '取文本': new R.Builtin('取文本', (片段) => html文本节点(html扫(pText(片段)))),
  '取链接': new R.Builtin('取链接', (页面) => {
    const 节点们 = html扫(pText(页面));
    const 出 = [];
    let i = 0;
    while (i < 节点们.length) {
      const [类型, 名, 体] = 节点们[i];
      if (类型 === 1 && 名 === 'a') {
        const 地址 = html取属性(体, 'href');
        if (地址 !== null) {
          const 块 = [];
          let j = i + 1;
          while (j < 节点们.length) {
            const [t2, n2] = 节点们[j];
            if (t2 === 2 && n2 === 'a') break;
            块.push(节点们[j]);
            j += 1;
          }
          出.push([html文本节点(块), 地址]);
          i = j + 1;
          continue;
        }
      }
      i += 1;
    }
    return 出;
  }),
  '取表格': new R.Builtin('取表格', (页面) => {
    const 节点们 = html扫(pText(页面));
    let i = 0;
    while (i < 节点们.length && !(节点们[i][0] === 1 && 节点们[i][1] === 'table')) i += 1;
    const 出 = [];
    let 行 = [];
    let 在一格 = false;
    let 格块 = [];
    while (i < 节点们.length) {
      const [类型, 名, 体] = 节点们[i];
      if (类型 === 2 && 名 === 'table') break;
      else if (类型 === 1 && 名 === 'tr') 行 = [];
      else if (类型 === 2 && 名 === 'tr') { if (行.length) 出.push(行); }
      else if (类型 === 1 && (名 === 'td' || 名 === 'th')) { 在一格 = true; 格块 = []; }
      else if (类型 === 2 && (名 === 'td' || 名 === 'th')) {
        if (在一格) 行.push(html文本节点(格块));
        在一格 = false;
      } else if (在一格 && 类型 === 0) 格块.push([0, '', 体]);
      i += 1;
    }
    return 出;
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

// ---------------------------------------------------------------------------
// M112：数据库（数据持久化）—— 与 Python 侧 `jishi/stdlib/数据库.py` 逐函数对齐
//
// 用 Node 内置的 `node:sqlite`（22.5+）。**注意三件事**（都是实测踩出来的）：
//
// 1. 它是**实验性 API**，`require` 时会往 **stderr** 打一条 ExperimentalWarning
//    —— 而 stderr 是五引擎对拍的一部分（报错渲染逐字比），绝不能混进去。
//    ⇒ 先临时换掉 `process.emitWarning` 再 require，随即换回来。
// 2. 它**把 JS `number` 一律绑成 REAL**，只有 `BigInt` 才是 INTEGER。
//    ⇒ 写整数必须传 `BigInt`，否则本机存进去的是浮点，与 Python / Rust 不同。
// 3. 读的时候要开 `setReadBigInts(true)`：这样 **bigint = INTEGER、number = REAL**
//    —— 这是 JS 侧唯一能分清两种数字的办法（`columns()` 的 `type` 是 null）。
//    REAL 还要包 `R.FloatBox`（不然 `100.0` 会显示成 `100`）。
// ---------------------------------------------------------------------------

let _DatabaseSync = null;
try {
  const _origEmitWarning = process.emitWarning;
  process.emitWarning = () => {};
  ({ DatabaseSync: _DatabaseSync } = require('node:sqlite'));
  process.emitWarning = _origEmitWarning;
} catch (e) {
  _DatabaseSync = null;      // 太老的 Node：下面「打开」时明确报错，不静默
}

/** 库句柄（= 打开时给的路径文本）→ 连接。 */
const _dbConns = new Map();

function _dbHandle(v) {
  const h = typeof v === 'string' ? v : R.display(v);
  if (h === '') throw R.valueErr('库句柄不能是空的');
  if (!_dbConns.has(h)) {
    throw R.valueErr(`这个库还没打开：「${h}」`, undefined, undefined,
      '先用 令 库 = 数据库.打开("路径") 打开，再把「库」传给后面的函数');
  }
  return h;
}

function _dbConn(v) { return _dbConns.get(_dbHandle(v)); }

/** 表名 / 字段名 → SQL 标识符（内部双引号翻倍，防注入）。 */
function _dbName(v) {
  const s = typeof v === 'string' ? v : R.display(v);
  if (s === '') throw R.valueErr('表名 / 字段名不能是空的');
  if (s.indexOf('\0') >= 0) throw R.valueErr(`表名 / 字段名里不能有 NUL 字符：「${s}」`);
  return '"' + s.replace(/"/g, '""') + '"';
}

function _dbRaw(v) { return typeof v === 'string' ? v : R.display(v); }

/** 「一个字段名」或「一串字段名」→ 裸名字数组（与 Python 侧 `_字段名单` 同口径）。 */
function _dbNames(v, who) {
  if (typeof v === 'string') return [v];
  if (!Array.isArray(v) || v.length === 0) {
    throw R.valueErr(`${who}要给一个非空的字段名列表`, undefined, undefined,
      '例如 ["日期", "类别", "金额"]');
  }
  return v.map(_dbRaw);
}

/** 基石的值 → 能进 SQLite 的值。**进不去就明确报错**，不静默。 */
function _dbStore(v, field) {
  if (v === null || v === undefined) return null;
  if (typeof v === 'boolean') return v ? 1n : 0n;         // 布尔 → INTEGER 1/0
  if (R.isFloatBox(v)) return v.value;                     // 浮点 → REAL
  if (typeof v === 'number') {
    if (!Number.isInteger(v)) return v;                    // 兜底：非整数的 number 当浮点
    // ⚠️ **整数必须给 BigInt**：node:sqlite 把 JS number 一律绑成 REAL
    if (!Number.isSafeInteger(v)) {
      throw R.valueErr(`「${field}」这个整数超出了能安全表示的范围（±2^53）`,
        undefined, undefined, '特别大的数存成文本（文本(值)），或者拆成两部分存');
    }
    return BigInt(v);
  }
  if (typeof v === 'string') return v;
  if (v && v.__jishiType === 'datetime') return R.display(v);   // 与其它引擎同口径
  if (R.isDecimal(v)) {
    return Number(R.display(v));                           // SQLite 只有浮点
  }
  if (R.isBytes(v)) return v.buf;                          // M113：字节 → BLOB
  if (Array.isArray(v)) {
    throw R.typeErr(`「${field}」这个值是「列表」，存不进数据库`, undefined, undefined,
      '用 json.文本化(值) 先转成文本再存；多个值就拆成多条记录');
  }
  if (v instanceof Map) {
    throw R.typeErr(`「${field}」这个值是「字典」，存不进数据库`, undefined, undefined,
      '用 json.文本化(值) 先转成文本再存；或者拆成几张表');
  }
  throw R.typeErr(`「${field}」这个值是「${R.typeName(v)}」，存不进数据库`,
    undefined, undefined, '数据库里只放 整数 / 浮点 / 文本 / 真假 / 空 / 日期 / 字节');
}

/** SQLite 的值 → 基石的值（`bigint` 说明是 INTEGER、`number` 说明是 REAL）。 */
function _dbRead(v, col) {
  if (v === null || v === undefined) return null;
  if (typeof v === 'bigint') {
    if (v > 9007199254740991n || v < -9007199254740991n) {
      throw R.valueErr(`「${col}」这一列是超过 2^53 的大整数，JS 侧读不出来`,
        undefined, undefined, '这个平台读不了这么大的数；别把它放进库里，或者存成文本');
    }
    return Number(v);
  }
  if (typeof v === 'number') return new R.FloatBox(v);     // REAL：包一层才显示 100.0
  if (typeof v === 'string') return v;
  if (v instanceof Uint8Array) return new R.JishiBytes(v);   // M113：BLOB → 字节
  return v;
}

/** 跑一句 SQL，返回 `{cols, rows}`（rows 是数组的数组）。 */
function _dbRun(conn, sql, params) {
  let st;
  try {
    st = conn.prepare(sql);
  } catch (e) {
    throw R.runErr(`数据库操作失败：${_dbMsg(e)}`);
  }
  st.setReadBigInts(true);
  let cols = null;
  try {
    cols = st.columns().map((c) => c.name);        // 保序、**允许重名**
  } catch (e) {
    cols = null;
  }
  let rows;
  try {
    rows = params && params.length ? st.all(...params) : st.all();
  } catch (e) {
    throw R.runErr(`数据库操作失败：${_dbMsg(e)}`);
  }
  if (!cols || cols.length === 0) cols = rows.length ? Object.keys(rows[0]) : [];
  // 🔴 node:sqlite 把每行给成**普通对象** —— 列名重复时后面的会盖掉前面的。
  //    这是 JS 侧的硬限制（Python 给元组、Rust 按下标取，都没这问题）。
  //    **不许静默**：发现重名就明确报错 + 给修法。
  if (rows.length && Object.keys(rows[0]).length < cols.length) {
    throw R.typeErr('这句 SQL 里有重名的列，JS 侧读不出来（同一行会被后面的盖掉）',
      undefined, undefined, '在 SQL 里给它们起别名，例如 SELECT 金额 AS 分组, 金额 AS 值');
  }
  const out = rows.map((r) => cols.map((c) => _dbRead(r[c], c)));
  return { cols, rows: out };
}

/** SQL 的**值绑定**（M170）：可选的第三个参数 → node:sqlite 的绑定值列表。
 *
 * 🔴 **整数必须给 BigInt**：`node:sqlite` 把 JS number 一律按 REAL 绑，
 *    于是 `WHERE 编号 = ?` 传 `1` 会绑成 `1.0`，跟 INTEGER 列比不上
 *    （MEMORY 里记着的 M112 三坑之一）。
 * 只认六种能被绑的值（空/布尔/整数/小数/文本/字节），别的**明确报错**，
 * 不静默转成字符串 —— 那就等于又把用户输入拼回 SQL 了。
 */
function _dbBind(参数们) {
  if (参数们 === undefined || 参数们 === null) return [];
  if (typeof 参数们 === 'string' || 参数们 instanceof Uint8Array) {
    throw R.typeErr('「参数们」要一个**列表**（每个 `?` 对应一项）', undefined, undefined,
      '例如 数据库.执行(库, "删除 由 成绩 当 名字 = ?", ["小明"])');
  }
  const 项们 = Array.from(参数们);
  return 项们.map((v, i) => {
    if (typeof v === 'boolean') return v ? 1n : 0n;
    if (typeof v === 'bigint') return v;
    if (typeof v === 'number') return Number.isInteger(v) ? BigInt(v) : v;
    if (v === null || v === undefined) return null;
    if (typeof v === 'string') return v;
    if (v instanceof Uint8Array) return v;
    throw R.typeErr(`「参数们」第 ${i + 1} 个值放不进 SQL（${R.pyStr(v)}）`, undefined, undefined,
      'SQL 参数只认 文本/整数/小数/布尔/空/字节；结构化数据请先 `json.转文本` 成文本');
  });
}

/** node:sqlite 的报错 → 那句 SQLite 自己的话（与另两个引擎尽量同口径）。 */
function _dbMsg(e) {
  const m = e && e.message ? String(e.message) : String(e);
  return m.replace(/^.*?: /, '');
}

/** 表的列名（按建表顺序）。表不在就报错，并把现有的表列出来。 */
function _dbCols(conn, table) {
  const q = _dbName(table);
  const r = _dbRun(conn, `PRAGMA table_info(${q})`, []);
  if (r.rows.length === 0) {
    const all = _dbRun(conn,
      "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
      []).rows.map((x) => x[0]);
    const hint = all.length ? `现在有这些表：${all.join('、')}` : '这个库还是空的（先 数据库.建表）';
    throw R.valueErr(`没有表「${_dbRaw(table)}」`, undefined, undefined, hint);
  }
  return r.rows.map((x) => x[1]);        // 第 2 列是 name
}

/** 「条件」字典 → `{sql, params}`。 */
function _dbCond(cond, cols, 表名) {
  if (cond === null || cond === undefined) return { sql: '', params: [] };
  if (!(cond instanceof Map)) {
    throw R.typeErr(`「条件」要给一个字典（字段 → 值），给过来的却是「${R.typeName(cond)}」`,
      undefined, undefined, '例如 {"类别": "餐饮"}；比大小写 {"金额": {">": 100}}');
  }
  if (cond.size === 0) return { sql: '', params: [] };
  const keys = [...cond.keys()].map(_dbRaw).sort();
  // ⚠️ **字段名要先对一遍表结构**（M156）：条件写错层级（`{">": 100}` 而不是
  //    `{"金额": {">": 100}}`）时，SQLite 对双引号里的未知名字会退化成
  //    「字符串字面量」（DQS）—— 三个执行器给空列表、Node 直接甩一句英文 SQL 错。
  const 缺 = keys.filter((k) => cols.indexOf(k) < 0);
  if (缺.length) {
    throw R.valueErr(`表「${_dbRaw(表名)}」里没有这些字段：${缺.join('、')}`,
      undefined, undefined,
      `它有：${cols.join('、')}。比大小要**嵌一层**：{"字段": {">": 100}}`);
  }
  const parts = [];
  const params = [];
  for (const kRaw of keys) {
    const k = [...cond.keys()].find((x) => _dbRaw(x) === kRaw);
    const v = cond.get(k);
    const field = _dbName(k);
    if (v instanceof Map) {
      if (v.size === 0) throw R.valueErr(`「${kRaw}」的条件是空字典，不知道要比什么`);
      for (const opRaw of [...v.keys()].map(_dbRaw).sort()) {
        const op = [...v.keys()].find((x) => _dbRaw(x) === opRaw);
        const x = v.get(op);
        if (['>', '<', '>=', '<=', '!='].indexOf(opRaw) >= 0) {
          parts.push(`${field} ${opRaw} ?`);
          params.push(_dbStore(x, kRaw));
        } else if (opRaw === '包含') {
          parts.push(`instr(${field}, ?) > 0`);
          params.push(String(x === null || x === undefined ? '' : (typeof x === 'string' ? x : R.display(x))));
        } else if (opRaw === '非空') {
          parts.push(`${field} IS ${R.truthy(x) ? 'NOT ' : ''}NULL`);
        } else {
          throw R.valueErr(`不认识的比较符号「${opRaw}」`, undefined, undefined,
            '能用：> < >= <= != 包含 非空');
        }
      }
    } else {
      parts.push(`${field} = ?`);
      params.push(_dbStore(v, kRaw));
    }
  }
  return { sql: ' WHERE ' + parts.join(' AND '), params };
}

/** 排序片段。**永远拿 rowid 兜底** —— 不然等值的行之间顺序不定。
 *
 * ⚠️ **方向要加在每一个排序字段上**（M156 修）：以前只拼在最后的 `rowid`
 * 后面，于是 `ORDER BY "金额", rowid DESC` ——「金额」还是**升序**，
 * 所谓的「倒序」只颠倒了「金额相等的那些行之间」的顺序。
 */
function _dbOrder(sort, desc) {
  const dir = desc ? ' DESC' : '';
  if (sort === null || sort === undefined || sort === '') return ` ORDER BY rowid${dir}`;
  const cols = _dbNames(sort, '排序字段').map(_dbName);
  return ` ORDER BY ${cols.map((c) => c + dir).join(', ')}, rowid${dir}`;
}

/** 记录数组 → 基石列表（每条是字典）。 */
function _dbRecords(r) {
  return r.rows.map((row) => {
    const m = new Map();
    r.cols.forEach((c, i) => m.set(c, row[i]));
    return m;
  });
}

const DB_FUNCS = {
  '打开': new R.Builtin('打开', (路径) => {
    const p = _dbRaw(路径);
    if (_dbConns.has(p)) return p;
    if (_DatabaseSync === null) {
      throw R.runErr('这个 Node 没有内置 SQLite（需要 22.5 以上）');
    }
    if (p !== ':memory:') {
      const path = require('node:path');
      const dir = path.dirname(path.resolve(p));
      if (!require('node:fs').existsSync(dir)) {
        throw R.fileErr(`目录不存在，建不了库：「${dir}」`);
      }
    }
    let c;
    try {
      c = new _DatabaseSync(p);
    } catch (e) {
      throw R.runErr(`打不开库「${p}」：${_dbMsg(e)}`);
    }
    c.exec('PRAGMA foreign_keys = ON');     // 三个引擎都显式设一次
    _dbConns.set(p, c);
    return p;
  }),
  '关闭': new R.Builtin('关闭', (库) => {
    const h = typeof 库 === 'string' ? 库 : R.display(库);
    const c = _dbConns.get(h);
    if (c) { try { c.close(); } catch (e) { /* 已经关了 */ } _dbConns.delete(h); }
    return null;
  }),
  '表们': new R.Builtin('表们', (库) => {
    const r = _dbRun(_dbConn(库),
      "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name", []);
    return r.rows.map((x) => x[0]);
  }),
  '有表': new R.Builtin('有表', (库, 表名) => {
    const r = _dbRun(_dbConn(库),
      "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", [_dbRaw(表名)]);
    return r.rows.length > 0;
  }),
  '建表': new R.Builtin('建表', (库, 表名, 字段们) => {
    const conn = _dbConn(库);
    const table = _dbName(表名);
    const raw = _dbNames(字段们, `表「${_dbRaw(表名)}」的字段们`);
    if (raw.indexOf('id') >= 0) {
      throw R.valueErr('字段名里不用写 id', undefined, undefined,
        '每张表自动带一个 id 主键，把 id 从字段列表里去掉就行');
    }
    try {
      conn.exec(`CREATE TABLE IF NOT EXISTS ${table} ` +
        `(id INTEGER PRIMARY KEY AUTOINCREMENT, ${raw.map(_dbName).join(', ')})`);
    } catch (e) {
      throw R.runErr(`数据库操作失败：${_dbMsg(e)}`);
    }
    return null;
  }),
  '建索引': new R.Builtin('建索引', (库, 表名, 字段们, 唯一) => {
    const conn = _dbConn(库);
    const cols = _dbCols(conn, 表名);
    const raw = _dbNames(字段们, '索引字段');
    const missing = raw.filter((x) => cols.indexOf(x) < 0);
    if (missing.length) {
      throw R.valueErr(`表「${_dbRaw(表名)}」里没有这些字段：${missing.join('、')}`,
        undefined, undefined, `它有：${cols.join('、')}`);
    }
    const idxName = '"' + _dbRaw(表名).replace(/"/g, '""') + '__' + raw.join('__') + '"';
    try {
      conn.exec(`CREATE ${R.truthy(唯一) ? 'UNIQUE ' : ''}INDEX IF NOT EXISTS ` +
        `${idxName} ON ${_dbName(表名)} (${raw.map(_dbName).join(', ')})`);
    } catch (e) {
      throw R.runErr(`数据库操作失败：${_dbMsg(e)}`);
    }
    return null;
  }),
  '插入': new R.Builtin('插入', (库, 表名, 记录) => {
    const conn = _dbConn(库);
    if (!(记录 instanceof Map)) {
      throw R.typeErr(`「记录」要给一个字典，给过来的却是「${R.typeName(记录)}」`,
        undefined, undefined, '例如 {"日期": "2026-10-15", "金额": 2550}');
    }
    const cols = _dbCols(conn, 表名);
    const items = [...记录.entries()];
    if (items.some(([k]) => _dbRaw(k) === 'id')) {
      throw R.valueErr('id 是自动给的，不用写', undefined, undefined, '把 id 从记录里去掉');
    }
    const bad = items.map(([k]) => _dbRaw(k)).filter((k) => cols.indexOf(k) < 0);
    if (bad.length) {
      throw R.valueErr(`表「${_dbRaw(表名)}」里没有这些字段：${bad.join('、')}`,
        undefined, undefined, `它有：${cols.join('、')}`);
    }
    items.sort((a, b) => (_dbRaw(a[0]) < _dbRaw(b[0]) ? -1 : _dbRaw(a[0]) > _dbRaw(b[0]) ? 1 : 0));
    const names = items.map(([k]) => _dbName(k)).join(', ');
    const marks = items.map(() => '?').join(', ');
    const params = items.map(([k, v]) => _dbStore(v, _dbRaw(k)));
    let out;
    try {
      out = conn.prepare(`INSERT INTO ${_dbName(表名)} (${names}) VALUES (${marks})`).run(...params);
    } catch (e) {
      throw R.runErr(`数据库操作失败：${_dbMsg(e)}`);
    }
    return Number(out.lastInsertRowid);
  }),
  '批量插入': new R.Builtin('批量插入', (库, 表名, 记录们) => {
    const conn = _dbConn(库);
    if (!Array.isArray(记录们)) {
      throw R.typeErr(`「记录们」要给一个列表，给过来的却是「${R.typeName(记录们)}」`);
    }
    if (记录们.length === 0) return 0;
    const cols = _dbCols(conn, 表名);
    const all = [];
    for (const it of 记录们) {
      if (!(it instanceof Map)) {
        throw R.typeErr(`记录们里混进了「${R.typeName(it)}」，应该每条都是字典`);
      }
      for (const k of it.keys()) {
        const s = _dbRaw(k);
        if (s === 'id') {
          throw R.valueErr('id 是自动给的，不用写', undefined, undefined, '把 id 从记录里去掉');
        }
        if (all.indexOf(s) < 0) all.push(s);
      }
    }
    all.sort();
    const bad = all.filter((k) => cols.indexOf(k) < 0);
    if (bad.length) {
      throw R.valueErr(`表「${_dbRaw(表名)}」里没有这些字段：${bad.join('、')}`,
        undefined, undefined, `它有：${cols.join('、')}`);
    }
    const st = conn.prepare(`INSERT INTO ${_dbName(表名)} ` +
      `(${all.map(_dbName).join(', ')}) VALUES (${all.map(() => '?').join(', ')})`);
    conn.exec('BEGIN');
    try {
      for (const it of 记录们) {
        const params = all.map((k) => {
          const key = [...it.keys()].find((x) => _dbRaw(x) === k);
          return key === undefined ? null : _dbStore(it.get(key), k);
        });
        st.run(...params);
      }
      conn.exec('COMMIT');
    } catch (e) {
      try { conn.exec('ROLLBACK'); } catch (e2) { /* 忽略 */ }
      throw R.runErr(`数据库操作失败：${_dbMsg(e)}`);
    }
    return 记录们.length;
  }),
  '查询': new R.Builtin('查询', (库, 表名, 条件, 排序, 倒序, 条数) => {
    const conn = _dbConn(库);
    const cols = _dbCols(conn, 表名);
    if (排序 !== undefined && 排序 !== null && 排序 !== '') {
      const want = _dbNames(排序, '排序字段');
      const missing = want.filter((x) => cols.indexOf(x) < 0);
      if (missing.length) {
        throw R.valueErr(`表「${_dbRaw(表名)}」里没有这些字段：${missing.join('、')}`,
          undefined, undefined, `它有：${cols.join('、')}`);
      }
    }
    const n = 条数 === undefined || 条数 === null ? 0 : Number(R.unwrap(条数));
    if (!Number.isInteger(n) || n < 0) {
      throw R.valueErr(`条数要给非负整数，给的是「${R.display(条数)}」`);
    }
    const c = _dbCond(条件, cols, 表名);
    const limit = n > 0 ? ` LIMIT ${n}` : '';
    const sql = `SELECT * FROM ${_dbName(表名)}${c.sql}${_dbOrder(排序, R.truthy(倒序))}${limit}`;
    return _dbRecords(_dbRun(conn, sql, c.params));
  }),
  '条数': new R.Builtin('条数', (库, 表名, 条件) => {
    const conn = _dbConn(库);
    const cols = _dbCols(conn, 表名);
    const c = _dbCond(条件, cols, 表名);
    const r = _dbRun(conn, `SELECT COUNT(*) FROM ${_dbName(表名)}${c.sql}`, c.params);
    return r.rows.length ? r.rows[0][0] : 0;
  }),
  '汇总': new R.Builtin('汇总', (库, 表名, 按, 求和, 条件) => {
    const conn = _dbConn(库);
    const cols = _dbCols(conn, 表名);
    const 分组裸 = _dbRaw(按);
    const 求和裸 = _dbRaw(求和);
    for (const x of [分组裸, 求和裸]) {
      if (cols.indexOf(x) < 0) {
        throw R.valueErr(`表「${_dbRaw(表名)}」里没有这些字段：${x}`,
          undefined, undefined, `它有：${cols.join('、')}`);
      }
    }
    const c = _dbCond(条件, cols, 表名);
    // ⚠️ 按 rowid 取回来（确定顺序），分组与累加都在本地做。
    //    **两列都起别名** —— 免得「按金额分组 + 对金额求和」这种写法撞上重名列
    //    （JS 侧行对象会把重名列盖掉，见 `_dbRun`）。
    const r = _dbRun(conn,
      `SELECT ${_dbName(按)} AS __g, ${_dbName(求和)} AS __v FROM ${_dbName(表名)}${c.sql} ORDER BY rowid`,
      c.params);
    const buckets = [];
    const index = new Map();
    for (const [g, v] of r.rows) {
      if (v === null) {
        throw R.typeErr(`「${求和裸}」这一列里有空值，没法汇总`, undefined, undefined,
          '先把空值补上，或者用条件把它们排除掉');
      }
      const num = R.isFloatBox(v) ? v.value : R.unwrap(v);
      if (typeof num !== 'number') {
        throw R.typeErr(`「${求和裸}」这一列里有不是数字的值「${R.display(v)}」，没法汇总`);
      }
      const k = R.setKey(g);
      if (!index.has(k)) { index.set(k, buckets.length); buckets.push({ g, vals: [] }); }
      buckets[index.get(k)].vals.push(num);
    }
    const out = new Map();
    for (const b of buckets) {
      const total = b.vals.reduce((a, x) => a + x, 0);
      // ⚠️ 字典的键要用**原值**（`R.setKey` 给的是内部哈希键，形如 `s:餐饮`，
      //    拿它当键会打印成 `s:餐饮`）—— 与 `表格.分组汇总` 同一写法。
      out.set(b.g, new Map([
        ['个数', b.vals.length],
        ['总和', new R.FloatBox(total)],
        ['平均', new R.FloatBox(total / b.vals.length)],
        ['最大', new R.FloatBox(Math.max(...b.vals))],
        ['最小', new R.FloatBox(Math.min(...b.vals))],
      ]));
    }
    return out;
  }),
  '修改': new R.Builtin('修改', (库, 表名, 条件, 新值) => {
    const conn = _dbConn(库);
    if (!(新值 instanceof Map) || 新值.size === 0) {
      throw R.typeErr('「新值」要给一个非空字典', undefined, undefined, '例如 {"金额": 3000}');
    }
    if (!(条件 instanceof Map) || 条件.size === 0) {
      throw R.valueErr('「修改」必须给条件', undefined, undefined,
        '要改整张表就用 数据库.执行(库, "UPDATE ...")；要改一部分就用 {"类别": "餐饮"} 这种条件');
    }
    const cols = _dbCols(conn, 表名);
    for (const k of 新值.keys()) {
      const s = _dbRaw(k);
      if (s === 'id') throw R.valueErr('id 是主键，不能改');
      if (cols.indexOf(s) < 0) {
        throw R.valueErr(`表「${_dbRaw(表名)}」里没有这些字段：${s}`,
          undefined, undefined, `它有：${cols.join('、')}`);
      }
    }
    const c = _dbCond(条件, cols, 表名);
    const ks = [...新值.keys()].map(_dbRaw).sort();
    const sets = ks.map((k) => `${_dbName(k)} = ?`).join(', ');
    const params = ks.map((k) => {
      const key = [...新值.keys()].find((x) => _dbRaw(x) === k);
      return _dbStore(新值.get(key), k);
    }).concat(c.params);
    let out;
    try {
      out = conn.prepare(`UPDATE ${_dbName(表名)} SET ${sets}${c.sql}`).run(...params);
    } catch (e) {
      throw R.runErr(`数据库操作失败：${_dbMsg(e)}`);
    }
    return Number(out.changes);
  }),
  '删除': new R.Builtin('删除', (库, 表名, 条件) => {
    const conn = _dbConn(库);
    if (!(条件 instanceof Map) || 条件.size === 0) {
      throw R.valueErr('「删除」必须给条件', undefined, undefined,
        '要清空整张表就用 数据库.执行(库, "DELETE FROM 表名")');
    }
    const cols = _dbCols(conn, 表名);
    const c = _dbCond(条件, cols, 表名);
    let out;
    try {
      out = conn.prepare(`DELETE FROM ${_dbName(表名)}${c.sql}`).run(...c.params);
    } catch (e) {
      throw R.runErr(`数据库操作失败：${_dbMsg(e)}`);
    }
    return Number(out.changes);
  }),
  '执行': new R.Builtin('执行', (库, SQL, 参数们) => {
    const conn = _dbConn(库);
    const sql = _dbRaw(SQL);
    if (_dbLikeSelect(sql)) {
      throw R.valueErr('「执行」只跑写操作的 SQL；你给的是 SELECT', undefined, undefined,
        '要取回记录用 数据库.执行查询(库, SQL)');
    }
    const 参 = _dbBind(参数们);
    let out;
    try {
      const st = conn.prepare(sql);
      out = 参.length ? st.run(...参) : st.run();
    } catch (e) {
      throw R.runErr(`数据库操作失败：${_dbMsg(e)}`);
    }
    return Number(out.changes);
  }),
  '执行查询': new R.Builtin('执行查询', (库, SQL, 参数们) => {
    const conn = _dbConn(库);
    const sql = _dbRaw(SQL);
    if (!_dbLikeSelect(sql)) {
      throw R.valueErr('「执行查询」只跑 SELECT；你给的不是 SELECT', undefined, undefined,
        '写操作用 数据库.执行(库, SQL)');
    }
    return _dbRecords(_dbRun(conn, sql, _dbBind(参数们)));
  }),
};

/** 是不是一句 SELECT（跳过开头的行注释）。 */
function _dbLikeSelect(sql) {
  let s = String(sql).replace(/^\s+/, '');
  while (s.indexOf('--') === 0) {
    const i = s.indexOf('\n');
    s = i < 0 ? '' : s.slice(i + 1).replace(/^\s+/, '');
  }
  return s.slice(0, 7).toLowerCase() === 'select ' ||
         s.slice(0, 7).toLowerCase() === 'select\n' ||
         s.slice(0, 7).toLowerCase() === 'select\r';
}

// -- 并发（M114）--
// 把**一批互相独立的 IO** 同时做，结果顺序与输入一致。
//
// ⚠️ **本宿主顺序执行**：Node 的 `fs` / `crypto` 在这一层都是**同步 API**，
//    没有「等待重叠」可言；要真并行得上 `worker_threads`（重，而且判据基准
//    不需要它）。**输出与其它引擎逐字节一致**（顺序、首个错胜出都一样），
//    只是不快 —— 如实标注，不假装。
//
// 三条规矩同 Python 侧 `并发._并行做`：
//   ① 顺序 = 输入顺序；② 首个错胜出；③ 线程数 <= 1 顺序执行（这里恒顺序）。
//
// 参数校验的**顺序也要一致**（算法 → 线程数 → 列表），否则「两处都不合法」
// 时三个引擎报的不是同一句。
function _并发线程数(v) {
  if (typeof v === 'boolean' || typeof v !== 'number' || !Number.isFinite(v)) {
    throw R.typeErr(`线程数要是正整数，得到了「${R.typeName(v)}」`);
  }
  const n = Math.trunc(v);
  if (n < 1) throw R.valueErr(`线程数要是正整数，现在是 ${n}`);
  return Math.min(n, 32);
}

function _并发列出(v, 谁) {
  if (typeof v === 'string') throw R.typeErr(`${谁}要给一个列表，不能直接给文本`);
  if (Array.isArray(v)) return v;
  throw R.typeErr(`${谁}要给一个列表，得到了「${R.typeName(v)}」`);
}

/** 顺序做一遍 —— 抛错自然就是「输入顺序里最靠前」的那个。 */
function _并发顺序做(项们, 做一项) {
  const 出 = [];
  for (const x of 项们) 出.push(做一项(x));
  return 出;
}

const CONCURRENT_FUNCS = {
  '批量摘要': new R.Builtin('批量摘要', (paths, algo = 'sha256', threads = 4) => {
    const 算法 = normAlgo(algo);      // 先验算法：**空列表也要报同样的错**
    _并发线程数(threads);              // 只为校验（本宿主顺序执行，用不到这个数）
    const 项们 = _并发列出(paths, '「批量摘要」的第一个参数（路径们）');
    return _并发顺序做(项们,
      (p) => CRYPTO_FUNCS['文件摘要'].fn(String(pText(p)), 算法));
  }),
  '批量读': new R.Builtin('批量读', (paths, threads = 4) => {
    _并发线程数(threads);
    const 项们 = _并发列出(paths, '「批量读」的第一个参数（路径们）');
    // 走 `文件.读字节` 同一个实现（连报错都一样）
    return _并发顺序做(项们, (p) => FILE_FUNCS['读字节'].fn(p));
  }),
  '并行取': new R.Builtin('并行取', (urls, threads = 4) => {
    _并发线程数(threads);
    const 项们 = _并发列出(urls, '「并行取」的第一个参数（网址们）');
    return _并发顺序做(项们, (u) => NET_FUNCS['获取'].fn(u));
  }),
};

// -- HTTP服务端（M115）--
// ⚠️ **本宿主只做「纯协议核心」那一半**（`解析请求` / `构造响应`）——
//    它们只是文本处理，五个引擎都能跑。
//    `启动` / `空闲端口` 要 socket，而 **Node 起不了阻塞式服务端**：
//    `node:http` 是**异步 + 事件循环**，基石的 VM 却是**同步**的 ——
//    没有「同步泵事件循环」的 API，真阻塞住了事件循环就跑不动，
//    处理函数永远等不到（除非把服务端塞进子进程，那又调不到你的基石函数了）。
//    ⇒ **明确报错**，不假装。取舍见 `docs/设计决策.md` D81。

const _HTTP默认内容类型 = 'text/plain; charset=utf-8';
const _HTTP原因 = {
  200: 'OK', 201: 'Created', 202: 'Accepted', 204: 'No Content',
  301: 'Moved Permanently', 302: 'Found', 303: 'See Other',
  304: 'Not Modified', 307: 'Temporary Redirect', 308: 'Permanent Redirect',
  400: 'Bad Request', 401: 'Unauthorized', 403: 'Forbidden',
  404: 'Not Found', 405: 'Method Not Allowed', 408: 'Request Timeout',
  409: 'Conflict', 413: 'Content Too Large', 415: 'Unsupported Media Type',
  429: 'Too Many Requests',
  500: 'Internal Server Error', 501: 'Not Implemented',
  502: 'Bad Gateway', 503: 'Service Unavailable', 504: 'Gateway Timeout',
};
const _HTTP自填 = ['content-length', 'content-type', 'connection'];

/** 去两端空白 —— **只去空格与制表符**（`String.trim` 按全 Unicode 空白去，跨语言对不齐）。 */
function _http去空白(s) { return String(s).replace(/^[ \t]+/, '').replace(/[ \t]+$/, ''); }

/** 按空格 / 制表符切（丢掉空段）。 */
function _http切段(s) { return _http去空白(s).split(/[ \t]+/).filter((p) => p !== ''); }

/** **只收整数**（布尔、浮点、文本都不收）—— 三宿主同一句文案。 */
function _http要整数(v, 谁) {
  if (typeof v === 'boolean' || typeof v !== 'number' || !Number.isInteger(v)) {
    throw R.typeErr(`${谁}要是整数`);
  }
  return v;
}

/** `%XX` 解码（UTF-8，不合法字节替换）。⚠️ `+` **不当作空格**。 */
function _http百分号解码(s) {
  const b = Buffer.from(String(s), 'utf8');
  const out = [];
  for (let i = 0; i < b.length; i++) {
    if (b[i] === 0x25 && i + 2 < b.length) {
      const h = parseInt(String.fromCharCode(b[i + 1]), 16);
      const l = parseInt(String.fromCharCode(b[i + 2]), 16);
      if (!Number.isNaN(h) && !Number.isNaN(l)) { out.push(h * 16 + l); i += 2; continue; }
    }
    out.push(b[i]);
  }
  return Buffer.from(out).toString('utf8');
}

// -- HTTP路由（M122）--
// 纯逻辑（列表 + 字典），五个引擎都能跑 ⇒ 进语料逐字节对拍。
// 匹配规则见 `oracle/jishi/stdlib/HTTP路由.py` 的模块文档（三边共用那一份）。

/** 切段：`/` → []；`/a/b` → ['a','b']；`/a/` → ['a','']（末尾斜杠严格区分）。 */
const _RT切段 = (路径) => (路径 === '/' ? [] : 路径.replace(/^\/+/, '').split('/'));

const ROUTE_FUNCS = {
  '路由表': new R.Builtin('路由表', () => []),

  '加路由': new R.Builtin('加路由', (表, 方法, 路径, 处理) => {
    if (!Array.isArray(表)) {
      throw R.typeErr(`「表」要是路由表（用 \`路由表()\` 造），现在是「${R.typeName(表)}」`);
    }
    const 方法上 = pText(方法).trim().toUpperCase();
    if (方法上 === '') {
      throw R.valueErr('「方法」不能是空文本（要「什么方法都收」请写 "*"）');
    }
    const 路径上 = pText(路径);
    if (!路径上.startsWith('/')) throw R.valueErr(`路径要以 / 开头（现在是「${路径上}」）`);
    R.ensureCallable(处理, '处理');
    for (const 段 of _RT切段(路径上)) {
      if (段 === '') throw R.valueErr(`路径「${路径上}」里有空段（是不是多写了一个 /）`);
      if (段 === ':') {
        throw R.valueErr(`路径「${路径上}」里有光秃秃的 \`:\`，参数要有名字（比如 \`:id\`）`);
      }
    }
    表.push(new Map([
      ['方法', 方法上],
      ['路径', 路径上],
      ['处理', 处理],
    ]));
    return 表;   // 返回表本身，方便连着写
  }),

  '匹配': new R.Builtin('匹配', (表, 方法, 路径) => {
    if (!Array.isArray(表)) {
      throw R.typeErr(`「表」要是路由表（用 \`路由表()\` 造），现在是「${R.typeName(表)}」`);
    }
    const 方法上 = pText(方法).trim().toUpperCase();
    const 段们 = _RT切段(pText(路径));

    let 最佳 = null;   // { 处理, 参数, 参数数 }
    for (const 路由 of 表) {
      const 路由方法 = 路由.get('方法');
      if (路由方法 !== '*' && 路由方法 !== 方法上) continue;
      const 模式们 = _RT切段(路由.get('路径'));
      if (模式们.length !== 段们.length) continue;

      const 参数 = new Map();
      let 能匹配 = true;
      for (let i = 0; i < 模式们.length; i++) {
        const 模式 = 模式们[i];
        const 实际 = 段们[i];
        if (模式.startsWith(':')) {
          // 空段不算参数值：`/a/` 不能匹配 `/a/:id`
          if (实际 === '') { 能匹配 = false; break; }
          参数.set(模式.slice(1), 实际);
        } else if (模式 !== 实际) { 能匹配 = false; break; }
      }
      if (!能匹配) continue;

      const 参数数 = 模式们.filter((s) => s.startsWith(':')).length;
      // 静态优先：参数段更少更确切；一样则保留**先注册**的
      if (最佳 === null || 参数数 < 最佳.参数数) {
        最佳 = { 处理: 路由.get('处理'), 参数, 参数数 };
      }
    }
    if (最佳 === null) return null;   // 没命中 ⇒ 空（404 是正常流程）

    return new Map([['处理', 最佳.处理], ['参数', 最佳.参数]]);
  }),

  '路径们': new R.Builtin('路径们', (表) => {
    if (!Array.isArray(表)) {
      throw R.typeErr(`「表」要是路由表（用 \`路由表()\` 造），现在是「${R.typeName(表)}」`);
    }
    return 表.map((r) => `${r.get('方法')} ${r.get('路径')}`);
  }),
};

const HTTP_FUNCS = {
  '解析请求': new R.Builtin('解析请求', (raw) => {
    const 原始 = pText(raw);
    const at = 原始.indexOf('\r\n\r\n');
    const 头部 = at >= 0 ? 原始.slice(0, at) : 原始;
    const 正文 = at >= 0 ? 原始.slice(at + 4) : '';
    const 行们 = 头部.split('\r\n');
    const 第一行 = 行们.length ? 行们[0] : '';
    if (_http去空白(第一行) === '') {
      throw R.valueErr('这不是一个 HTTP 请求：第一行是空的');
    }
    const 片段 = _http切段(第一行);
    if (片段.length < 2) {
      throw R.valueErr(
        `这不是一个 HTTP 请求：第一行该是「方法 路径 版本」，现在是「${第一行}」`);
    }
    const 目标 = 片段[1];
    const q = 目标.indexOf('?');
    const 路径 = q >= 0 ? 目标.slice(0, q) : 目标;
    const 查询串 = q >= 0 ? 目标.slice(q + 1) : '';
    // ⚠️ 基石的「字典」在 Node 宿主**就是 `Map`**（不是普通对象）——
    //    返回 `{}` 的话，外面 `请求["方法"]` 会报「「Object」不能取下标」
    const 查询 = new Map();
    for (const 段 of 查询串.split('&')) {
      if (段 === '') continue;
      const e = 段.indexOf('=');
      const k = e >= 0 ? 段.slice(0, e) : 段;
      const v = e >= 0 ? 段.slice(e + 1) : '';
      查询.set(_http百分号解码(k), _http百分号解码(v));
    }
    const 请求头 = new Map();
    for (let i = 1; i < 行们.length; i++) {
      const 行 = _http去空白(行们[i]);
      if (行 === '') continue;
      const c = 行.indexOf(':');
      if (c < 0) throw R.valueErr(`请求头这一行没有冒号：「${行}」`);
      请求头.set(_http去空白(行.slice(0, c)).toLowerCase(), _http去空白(行.slice(c + 1)));
    }
    return new Map([
      ['方法', 片段[0]],
      ['路径', _http百分号解码(路径)],
      ['查询', 查询],
      ['版本', 片段.length > 2 ? 片段[2] : 'HTTP/1.1'],
      ['请求头', 请求头],
      ['正文', 正文],
    ]);
  }),
  '构造响应': new R.Builtin('构造响应', (code = 200, body = '', ctype = _HTTP默认内容类型, extra = null) => {
    const 码 = _http要整数(code, '状态码');
    if (!(码 >= 100 && 码 <= 599)) throw R.valueErr(`状态码要在 100–599 之间，现在是 ${码}`);
    const 正文 = pText(body);
    const 类型 = pText(ctype);
    const 行们 = [`HTTP/1.1 ${码}` + (_HTTP原因[码] ? ` ${_HTTP原因[码]}` : '')];
    行们.push(`Content-Type: ${类型}`);
    if (extra !== null && extra !== undefined) {
      const 对 = extra instanceof Map
        ? [...extra.entries()]
        : (typeof extra === 'object' && !Array.isArray(extra)
            ? Object.entries(extra)
            : null);
      if (对 === null) {
        throw R.typeErr(`「头」要是一个字典，得到了「${R.typeName(extra)}」`);
      }
      for (const [k0, v0] of 对) {
        const k = pText(k0);
        if (_HTTP自填.includes(k.toLowerCase())) {
          throw R.valueErr(`「${k}」由框架自己填，不要在「头」里再写一遍`);
        }
        const v = pText(v0);
        // eslint-disable-next-line no-control-regex
        if (/[^\x00-\x7f]/.test(k) || /[^\x00-\x7f]/.test(v)) {
          throw R.typeErr(`请求头「${k}」的名字与值只能是 ASCII（英文数字）`);
        }
        行们.push(`${k}: ${v}`);
      }
    }
    const len = Buffer.byteLength(正文, 'utf8');
    行们.push(`Content-Length: ${len}`);
    行们.push('Connection: close');
    return 行们.join('\r\n') + '\r\n\r\n' + 正文;
  }),
  '空闲端口': new R.Builtin('空闲端口', () => {
    throw R.valueErr(
      '这个平台起不了 HTTP 服务端：Node 宿主是**同步**的，而 `node:http` 要事件循环'
      + '（没有「同步泵事件循环」的 API，阻塞住了处理函数永远等不到）');
  }),
  '启动': new R.Builtin('启动', () => {
    throw R.valueErr(
      '这个平台起不了 HTTP 服务端：Node 宿主是**同步**的，而 `node:http` 要事件循环'
      + '（没有「同步泵事件循环」的 API，阻塞住了处理函数永远等不到）');
  }),
};

/// M117：递归复制列表 / 字典（数字、文本不可变，原样返回）。
function deepCopyVal(v) {
  if (Array.isArray(v)) return v.map(deepCopyVal);
  if (v instanceof Map) {
    const m = new Map();
    for (const [k, x] of v) m.set(k, deepCopyVal(x));
    return m;
  }
  return v;
}

/// M118：递归复制目录树（**不用 `fs.cpSync`**，见 复制目录 里的说明）。
function 递归复制(src, dst) {
  fs.mkdirSync(dst, { recursive: true });
  for (const 名 of fs.readdirSync(src)) {
    const 从 = _path.join(src, 名);
    const 到 = _path.join(dst, 名);
    if (fs.statSync(从).isDirectory()) 递归复制(从, 到);
    else fs.copyFileSync(从, 到);
  }
}

// ---- M120：日期 / 时区 / 表格渲染的小助手 ---------------------------------
// ⚠️ 这几条都**跨引擎共用同一个算法**（Python / Rust 里是同一套），
//    否则「五引擎逐字节一致」这条核心判据就守不住了。

/** 公历闰年：1900 不是、2000 是（百年例外 + 四百年例外）。 */
function 闰年(y) {
  return (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0;
}

/** 某年某月有几天。 */
function 月天数(y, m) {
  if (m === 2) return 闰年(y) ? 29 : 28;
  return (m === 4 || m === 6 || m === 9 || m === 11) ? 30 : 31;
}

/** 整分钟数 → `+08:00`（半小时间区也能对）。 */
function 格式化偏移(分钟) {
  const 号 = 分钟 < 0 ? '-' : '+';
  const 绝对 = Math.abs(分钟);
  const 时 = String(Math.floor(绝对 / 60)).padStart(2, '0');
  const 分 = String(绝对 % 60).padStart(2, '0');
  return 号 + 时 + ':' + 分;
}

/** 按**显示宽度**补空格（汉字算 2 列，与 `文本.显示宽度` 同口径）。 */
/** 定点：固定小数位，**四舍五入（远离零）**，在十进制字符串上进位。
 *
 * 🔴 不用 `toFixed`：正好一半时 `(4.25).toFixed(1)` 给 `4.3`，而 Python 的
 * `format(4.25, ".1f")` 给 `4.2`（半偶）—— 三个宿主会各说各话。所以这里
 * 一律在**十进制字符串**上进位：先取 `display`（打印口径，已对拍），再按位取舍。
 */
function 定点文本(v, 位) {
  const s = R.display(v);
  if (s.includes('e') || s.includes('E')) return s;   // 科学计数法原样给
  const 负 = s.startsWith('-');
  const 纯 = 负 ? s.slice(1) : s;
  const 点 = 纯.indexOf('.');
  let 整数部 = 点 < 0 ? 纯 : 纯.slice(0, 点);
  let 小数部 = 点 < 0 ? '' : 纯.slice(点 + 1);
  if (!整数部) 整数部 = '0';
  while (小数部.length < 位) 小数部 += '0';
  const 保留 = 小数部.slice(0, 位);
  const 丢弃 = 小数部.slice(位);
  let 结果 = 位 > 0 ? `${整数部}.${保留}` : 整数部;
  if (丢弃.length > 0 && 丢弃.charCodeAt(0) >= 53) 结果 = 进一文本(结果);
  return (负 ? '-' : '') + 结果;
}

/** 十进制字符串加一（带小数点）：`9.9` → `10.0`。 */
function 进一文本(s) {
  const 位们 = s.split('');
  let 走 = 位们.length - 1;
  while (走 >= 0) {
    const c = 位们[走];
    if (c === '.') { 走 -= 1; continue; }
    if (c === '9') { 位们[走] = '0'; 走 -= 1; continue; }
    位们[走] = String.fromCharCode(c.charCodeAt(0) + 1);
    return 位们.join('');
  }
  return `1${s}`;
}

// ---------------------------------------------------------------------------
// 十进制字符串上的舍入 / 移位 / 千分位（M153）
// ---------------------------------------------------------------------------
//
// 与 Python `runtime.py`、Rust `stdlib/mod.rs` 的同名函数**逐字对齐**
// （`tests/test_m153_numbers.py` 盯五个执行器）。
//
// 为什么不在浮点上做：用户说「四舍五入」想的是「按我写的那个十进制数，
// 五就进一位」—— `1.005` 的二进制其实是 1.00499999…，任何 f64 舍入都给 1.00；
// 而且三个宿主各用一套浮点舍入，`2.675` 这种边界必然各说各话。

/** 数值 → 归一后的十进制文本（`2.0` → `2`）。 */
function 最短十进制(v) {
  const u = R.unwrap(v);
  if (typeof u === 'number') {
    if (Number.isInteger(u) && Math.abs(u) < 1e21) return String(u);
    return 去尾零(十进制展开(String(u)));
  }
  if (typeof u === 'bigint') return u.toString();
  return 去尾零(十进制展开(String(u)));      // 精确小数：它自己的十进制写法
}

/** `2.0` → `2`、`3.500` → `3.5`（只动小数部分）。 */
function 去尾零(s) {
  if (!s.includes('.')) return s;
  const i = s.indexOf('.');
  const 整 = s.slice(0, i);
  const 小 = s.slice(i + 1).replace(/0+$/, '');
  return 小 ? `${整}.${小}` : 整;
}

/** 科学计数法展开成纯小数写法：`1e-7` → `0.0000001`。 */
function 十进制展开(s) {
  const t = String(s).trim();
  if (!/[eE]/.test(t)) return t;
  const 负 = t.startsWith('-');
  const 体 = t.replace(/^[-+]/, '').toLowerCase();
  const [底, 指文] = 体.split('e');
  const 指 = parseInt(指文 || '0', 10);
  const [整, 小 = ''] = 底.split('.');
  const 数字 = 整 + 小;
  const 点位置 = 整.length + 指;
  let 结果;
  if (点位置 <= 0) 结果 = '0.' + '0'.repeat(-点位置) + 数字;
  else if (点位置 >= 数字.length) 结果 = 数字 + '0'.repeat(点位置 - 数字.length);
  else 结果 = 数字.slice(0, 点位置) + '.' + 数字.slice(点位置);
  return (负 ? '-' : '') + 结果;
}

/** `'-1.005'` → `{负:true, 数字:'1005', 位:3}`（值 = ±1005 × 10⁻³）。 */
function 十进制解析(s) {
  const 负 = s.startsWith('-');
  const 体 = s.replace(/^[-+]/, '');
  const i = 体.indexOf('.');
  const 整 = i < 0 ? 体 : 体.slice(0, i);
  const 小 = i < 0 ? '' : 体.slice(i + 1);
  const 数字 = (整 + 小).replace(/^0+/, '') || '0';
  return { 负, 数字, 位: 小.length };
}

/** 「有效数字 + 小数位数」拼回文本（`'1005', 3` → `1.005`）。 */
function 十进制拼(数字, 小数位) {
  if (小数位 <= 0) return (数字 + '0'.repeat(-小数位)) || '0';
  if (数字.length <= 小数位) return '0.' + '0'.repeat(小数位 - 数字.length) + 数字;
  const k = 数字.length - 小数位;
  return 数字.slice(0, k) + '.' + 数字.slice(k);
}

/** 有效数字串 +1（`999` → `1000`）。 */
function 十进制进一(数字) {
  const 位们 = 数字.split('');
  for (let i = 位们.length - 1; i >= 0; i--) {
    if (位们[i] === '9') { 位们[i] = '0'; continue; }
    位们[i] = String.fromCharCode(位们[i].charCodeAt(0) + 1);
    return 位们.join('');
  }
  return '1' + 位们.join('');
}

/** 按**半入（远离零）**舍到 `小数位` 位；负数位＝往整数高位舍。 */
function 十进制舍入(s, 小数位) {
  const 解 = 十进制解析(十进制展开(s));
  let 数字 = 解.数字;
  let 位 = 解.位;
  if (位 < 小数位) {
    数字 += '0'.repeat(小数位 - 位);
    位 = 小数位;
  } else if (位 > 小数位) {
    const 丢 = 位 - 小数位;
    let 看;
    if (丢 >= 数字.length) {
      看 = 数字.padStart(丢, '0')[0];
      数字 = '0';
    } else {
      看 = 数字[数字.length - 丢];
      数字 = 数字.slice(0, 数字.length - 丢);
    }
    if (看 >= '5') 数字 = 十进制进一(数字);
  }
  const 文本 = 十进制拼(数字, 小数位);
  if (/^[0.]+$/.test(文本)) return 文本;          // 全零不挂负号（-0.4 舍成 0）
  return (解.负 ? '-' : '') + 文本;
}

/** 十进制字符串 × 10^k（`百分比` 用；不经过浮点）。 */
function 十进制乘(s, k) {
  const 解 = 十进制解析(s);
  const 体 = 十进制拼(解.数字, 解.位 - k);
  return (解.负 ? '-' : '') + 体;
}

/** 整数部分每三位插一个逗号：`1234567.891` → `1,234,567.891`。 */
function 十进制千分位(s) {
  const t = 十进制展开(s);
  const 负 = t.startsWith('-');
  const 体 = t.replace(/^[-+]/, '');
  const i = 体.indexOf('.');
  const 整 = i < 0 ? 体 : 体.slice(0, i);
  const 小 = i < 0 ? null : 体.slice(i + 1);
  const 组 = [];
  let 余 = 整;
  while (余.length > 3) {
    组.unshift(余.slice(余.length - 3));
    余 = 余.slice(0, 余.length - 3);
  }
  组.unshift(余 || '0');
  return (负 ? '-' : '') + 组.join(',') + (小 === null ? '' : '.' + 小);
}

/** `文本.对齐表`（M142）：一列值 → 一行；列表的列表 → 一张对齐的表。 */
function 对齐表文本(行们, 小数位) {
  if (typeof 行们 === 'string' || R.isBytes(行们)) {
    throw R.typeErr('「对齐表」要传一列值或一张表（列表），不能传文本');
  }
  const 原始 = R.seqOf(行们, false, undefined, undefined);
  if (原始 === null) throw R.typeErr('「对齐表」要传一列值或一张表（列表）');
  if (原始.length === 0) throw R.valueErr('「对齐表」要至少有一个值，现在是空的');

  let 位 = null;
  if (小数位 !== undefined && 小数位 !== null) {
    const n = R.unwrap(小数位);
    if (R.isFloatBox(小数位) || typeof n !== 'number' || !Number.isInteger(n)) {
      throw R.typeErr(`「对齐表」的小数位要是不给或整数，给的是 ${R.display(小数位)}`);
    }
    if (n < 0 || n > 10) {
      throw R.valueErr(`「对齐表」的小数位要在 0 到 10 之间（给的是 ${n}）`);
    }
    位 = n;
  }

  const 第一 = 原始[0];
  const 多行 = Array.isArray(第一) || (第一 !== null && typeof 第一 === 'object'
    && !R.isFloatBox(第一) && !R.isBytes(第一) && typeof 第一 !== 'string'
    && R.seqOf(第一, false, undefined, undefined) !== null);
  let 表;
  if (多行) {
    表 = 原始.map((行) => {
      const 列们 = R.seqOf(行, false, undefined, undefined);
      if (列们 === null) throw R.typeErr('「对齐表」要传一列值或一张表（列表）');
      return 列们;
    });
    const 宽 = 表[0].length;
    表.forEach((行, i) => {
      if (行.length !== 宽) {
        throw R.valueErr(`「对齐表」每行的列数要相同：第 1 行有 ${宽} 列，`
          + `第 ${i + 1} 行有 ${行.length} 列`);
      }
    });
  } else {
    表 = [原始];
  }

  const 是不是数 = (x) => typeof R.unwrap(x) === 'number' && typeof x !== 'boolean';
  const 格子 = 表.map((行) => 行.map((v) => {
    if (位 !== null && 是不是数(v)) return 定点文本(v, 位);
    return R.display(v);
  }));

  const 列数 = 格子[0].length;
  const 列宽 = [];
  for (let j = 0; j < 列数; j += 1) {
    let 最宽 = 0;
    for (const 行 of 格子) 最宽 = Math.max(最宽, displayWidth(行[j]));
    列宽.push(最宽);
  }
  const 数字列 = [];
  for (let j = 0; j < 列数; j += 1) 数字列.push(表.every((行) => 是不是数(行[j])));

  return 格子.map((行) => 行
    .map((格, j) => 补齐(格, 列宽[j], 数字列[j]))
    .join('  ')
    .replace(/\s+$/, ''))
    .join('\n');
}

function 补齐(s, 宽, 靠右) {
  const 差 = 宽 - displayWidth(s);
  if (差 <= 0) return s;
  return 靠右 ? ' '.repeat(差) + s : s + ' '.repeat(差);
}

/** 表格 → 对齐文本：表头 + 分隔线 + 数据行；数字列右对齐。 */
function 转对齐文本(tab) {
  // ⚠️ 用 `pyStr`（打印口径）而不是 `display`：两者对**嵌套文本**的处理不同
  //    （`display` 给 `[甲, 乙]`、`pyStr` 给 `['甲', '乙']`），而 Python / Rust 那边
  //    用的都是打印口径 —— 用错了三边对不上（74 号报错语料当场抓出来）。
  const 头 = [...tab.headers].map((h) => R.pyStr(h));
  const 列数 = 头.length;
  if (列数 === 0) return '';
  const 行们 = tab.rows.map((r) => [...r].map((c) => R.pyStr(c)));
  const 宽 = 头.map((x) => displayWidth(x));
  const 靠右 = [];
  for (let i = 0; i < 列数; i++) {
    let 有值 = false; let 全数字 = true;
    for (const r of tab.rows) {
      if (i >= r.length) continue;
      const v = r[i];
      if (v === null || v === undefined) continue;
      if (typeof v === 'string' && v === '') continue;
      有值 = true;
      if (typeof v !== 'number') 全数字 = false;
    }
    靠右.push(有值 && 全数字);
  }
  for (const r of 行们) {
    for (let i = 0; i < Math.min(列数, r.length); i++) {
      宽[i] = Math.max(宽[i], displayWidth(r[i]));
    }
  }
  const 出 = [];
  出.push(头.map((x, i) => 补齐(x, 宽[i], 靠右[i])).join('  ').trimEnd());
  出.push(宽.map((w) => '-'.repeat(w)).join('  '));
  for (const r of 行们) {
    const 格 = [];
    for (let i = 0; i < 列数; i++) 格.push(补齐(r[i] === undefined ? '' : r[i], 宽[i], 靠右[i]));
    出.push(格.join('  ').trimEnd());
  }
  return 出.join('\n');
}

/** 表格 → TSV；单元格含制表符/换行时**报错**（不静默改内容）。 */
function 转TSV文本(tab) {
  const 出 = [];
  const 全行 = [[...tab.headers], ...tab.rows.map((r) => [...r])];
  for (let 号 = 0; 号 < 全行.length; 号++) {
    const 元 = [];
    for (let 列 = 0; 列 < 全行[号].length; 列++) {
      const s = R.pyStr(全行[号][列]);
      if (s.includes('\t') || s.includes('\n') || s.includes('\r')) {
        throw R.typeErr(`第 ${号 + 1} 行第 ${列 + 1} 格里含制表符或换行，TSV 装不下`
          + ' —— 要写这种内容请用 表格.写表格 存成 CSV');
      }
      元.push(s);
    }
    出.push(元.join('\t'));
  }
  return 出.join('\n');
}

// ---------------------------------------------------------------------------
// M136：`测试` 模块（**本宿主以前根本没有这个模块**，`导入 测试` 直接 E0302）
//
// 语义与 Python 侧 `jishi/stdlib/测试.py` 逐条对齐：用例 + 断言 + 总结，
// 加上 M136 的 `用例们`（多组数据）/ `跳过` / `每例前` / `每例后`。
//
// ⚠️ **状态要按「每次导入」重置**（Python 用 `__基石新建__`、Rust 在 `module()`
// 里重置）：本宿主的模块是**单例**，所以走 `Module` 的 `onImport` 钩子，
// 由 `_doImport` 在每次 `导入 测试` 时调一次。不重置的话，同一个脚本里
// `导入 测试` 两次会看到累积的记录 —— 那和另外两个宿主就对不上了。
// ---------------------------------------------------------------------------
const _测试状态 = { 记录: [], 当前: null, 前: null, 后: null };

function _重置测试状态() {
  _测试状态.记录 = [];
  _测试状态.当前 = null;
  _测试状态.前 = null;
  _测试状态.后 = null;
}

/** 出错摘要：`标题（消息）`；没有消息就只给标题（与 Python 的 `_摘要` 同口径）。 */
function _测试摘要(e) {
  if (!(e instanceof R.JishiError)) return String(e && e.message ? e.message : e);
  const 名 = e.title || e.typeName;
  return e.message ? `${名}（${e.message}）` : 名;
}

/** 断言类错误：**已经记过失败了**，`用例` 不该再把它记成「用例崩了」。 */
function _是断言(e) {
  return e instanceof R.JishiError && e.typeName === '断言错误';
}

/** 跳过：`测试.跳过()` 抛的内部标记（类型名与 Rust/Python 对齐）。 */
function _是跳过(e) {
  return e instanceof R.JishiError && e.typeName === '跳过用例';
}

/** 记一条用例的错（跳过 / 崩了 / 断言失败已记过）。 */
function _记用例抛错(下标, e, 前缀) {
  const 条 = _测试状态.记录[下标];
  if (!条) return;
  if (_是跳过(e)) {
    条.跳过 = e.message || '';
  } else if (!_是断言(e)) {
    const 摘 = 前缀 + _测试摘要(e);
    条.异常 = 条.异常 === null ? 摘 : `${条.异常}；${摘}`;
  }
}

/** 在用例里就记进用例；否则直接抛（独立用断言时）—— 与 Python `_失败` 同口径。 */
function _测试记失败(说明, 详) {
  if (_测试状态.当前 !== null) {
    const 条 = _测试状态.记录[_测试状态.当前];
    if (条) { 条.失败.push([说明, 详]); return null; }
  }
  // ⚠️ 用**断言错误**（E2008）而不是 runErr（E2000）：Python 侧抛的是
  //    `RunAssertionError`、Rust 侧 `err("断言错误", …)` —— 三边得同一个码。
  return R.EXCEPTION_TYPES['断言错误'].make(`${说明}：${详}`);
}

function _测试断言(成立, 说明, 详) {
  if (成立) return null;
  return _测试记失败(说明, 详);
}

/** 可选的第 3 个参数（说明），缺省空串。 */
function _说明(args, i) {
  return args[i] === undefined ? '' : R.display(args[i]);
}

/** 跑一条用例（含前后置）—— `用例` 与 `用例们` 共用。 */
function _跑一条(名字, 函数, 参数们) {
  const 条 = { 名字: String(名字), 失败: [], 异常: null, 跳过: null };
  _测试状态.记录.push(条);
  const 下标 = _测试状态.记录.length - 1;
  const 旧 = _测试状态.当前;
  _测试状态.当前 = 下标;
  try {
    let 能跑 = true;
    if (_测试状态.前) {
      try { R.callValue(_测试状态.前, []); } catch (e) {
        _记用例抛错(下标, e, '前置出错 —— ');
        能跑 = false;
      }
    }
    if (能跑) {
      try { R.callValue(函数, 参数们); } catch (e) {
        _记用例抛错(下标, e, '');
      }
    }
    if (_测试状态.后) {
      try { R.callValue(_测试状态.后, []); } catch (e) {
        // 后置出错**算这条用例出错**：它污染的是后面的用例，不能吞。
        _记用例抛错(下标, e, '后置出错 —— ');
      }
    }
  } finally {
    _测试状态.当前 = 旧;
  }
  return new Map([
    ['名字', 条.名字],
    ['失败', 条.失败.map(([a, b]) => [a, b])],
    ['异常', 条.异常],
    ['跳过', 条.跳过],
  ]);
}

const TEST_FUNCS = {
  '重置': new R.Builtin('重置', () => {
    _测试状态.记录 = [];
    return null;
  }),
  '用例': new R.Builtin('用例', (名字, 函数) => {
    if (函数 === undefined || 函数 === null) {
      throw R.runErr('「测试.用例」要一个函数，写成 测试.用例("名字", 函数()：…)');
    }
    return _跑一条(R.display(名字), 函数, []);
  }),
  '用例们': new R.Builtin('用例们', (名字, 数据们, 函数) => {
    if (函数 === undefined || 函数 === null) {
      throw R.runErr('「测试.用例们」要一个函数，写成 测试.用例们("名字", 数据们, 函数)');
    }
    if (!Array.isArray(数据们)) {
      throw R.runErr('「测试.用例们」的第二个参数（数据们）要是一个列表');
    }
    return 数据们.map((组, i) => _跑一条(`${R.display(名字)}[${i + 1}]`, 函数, [组]));
  }),
  '相等': new R.Builtin('相等', (实际, 期望, 说明) => {
    const 成 = R.valueEq(实际, 期望);
    return _测试断言(成, _说明([实际, 期望, 说明], 2) || '相等',
      `期望 ${R.display(期望)}，实际 ${R.display(实际)}`);
  }),
  '不等': new R.Builtin('不等', (实际, 不该等于, 说明) => {
    const 等 = R.valueEq(实际, 不该等于);
    return _测试断言(!等, _说明([实际, 不该等于, 说明], 2) || '不等',
      `不该等于 ${R.display(不该等于)}，但实际就是它`);
  }),
  '为真': new R.Builtin('为真', (条件, 说明) => {
    return _测试断言(R.truthy(条件), _说明([条件, 说明], 1) || '为真',
      `期望为真，实际是 ${R.display(条件)}`);
  }),
  '为假': new R.Builtin('为假', (条件, 说明) => {
    return _测试断言(!R.truthy(条件), _说明([条件, 说明], 1) || '为假',
      `期望为假，实际是 ${R.display(条件)}`);
  }),
  '近似': new R.Builtin('近似', (实际, 期望, 误差, 说明) => {
    const 容 = 误差 === undefined ? 1e-9 : Number(R.unwrap(误差));
    const 差 = Math.abs(Number(R.unwrap(实际)) - Number(R.unwrap(期望)));
    return _测试断言(差 <= 容, _说明([实际, 期望, 误差, 说明], 3) || '近似',
      `期望 ${R.display(期望)}（±${R.display(误差 === undefined ? 1e-9 : 误差)}），实际 ${R.display(实际)}`);
  }),
  '包含': new R.Builtin('包含', (容器, 元素, 说明) => {
    let 有 = false;
    try { 有 = R.contains(容器, 元素); } catch (e) {
      throw R.runErr(`「${R.display(容器)}」不能判包含`);
    }
    return _测试断言(有, _说明([容器, 元素, 说明], 2) || '包含',
      `${R.display(容器)} 里没有 ${R.display(元素)}`);
  }),
  '抛出': new R.Builtin('抛出', (函数, 说明) => {
    try {
      R.callValue(函数, []);
    } catch (e) {
      // 抛出来的错误**当成值返回**（可以再看它的类型）—— 与另外两个宿主同口径
      return e instanceof R.JishiError ? e : R.runErr(String(e && e.message ? e.message : e));
    }
    return _测试记失败(_说明([函数, 说明], 1) || '抛出', '期望报错，但它正常跑完了');
  }),
  '跳过': new R.Builtin('跳过', (原因) => {
    if (_测试状态.当前 === null) {
      throw R.runErr('「测试.跳过」只能在用例里用'
        + '（在用例外面调它等于把整份脚本静默跳过，那不是它的意思）');
    }
    // ⚠️ 用**独立的类型名**当控制流标记：不能被「断言错误」或「运行期错误」误认。
    throw new R.JishiError('跳过用例', '跳过用例',
      原因 === undefined ? '' : R.display(原因));
  }),
  '每例前': new R.Builtin('每例前', (函数) => {
    _测试状态.前 = 函数;
    return null;
  }),
  '每例后': new R.Builtin('每例后', (函数) => {
    _测试状态.后 = 函数;
    return null;
  }),
  '统计': new R.Builtin('统计', () => {
    const 记录 = _测试状态.记录;
    const 失败 = 记录.reduce((n, t) => n + t.失败.length, 0);
    const 出错 = 记录.filter((t) => t.异常 !== null).length;
    const 跳过 = 记录.filter((t) => t.跳过 !== null && t.异常 === null).length;
    const 通过 = 记录.filter(
      (t) => t.失败.length === 0 && t.异常 === null && t.跳过 === null).length;
    return new Map([
      ['用例', 记录.length], ['通过', 通过], ['失败', 失败],
      ['出错', 出错], ['跳过', 跳过],
    ]);
  }),
  '汇总': new R.Builtin('汇总', () => {
    const 记录 = _测试状态.记录;
    const 失败数 = 记录.reduce((n, t) => n + t.失败.length, 0);
    const 出错数 = 记录.filter((t) => t.异常 !== null).length;
    const 跳过数 = 记录.filter((t) => t.跳过 !== null && t.异常 === null).length;
    const 通过数 = 记录.filter(
      (t) => t.失败.length === 0 && t.异常 === null && t.跳过 === null).length;
    // 失败明细走**错误流**、汇总行走**输出流**（另两个宿主就是这个口径）
    for (const t of 记录) {
      if (t.异常 !== null) {
        process.stderr.write(`✗ ${t.名字}：用例里出错 —— ${t.异常}\n`);
      }
      for (const [说明, 详] of t.失败) {
        process.stderr.write(`✗ ${t.名字} · ${说明}：${详}\n`);
      }
    }
    // ⏭ 明细走**输出流**（跳过不是错误），且在汇总行之前
    for (const t of 记录) {
      if (t.跳过 !== null && t.异常 === null) {
        const 尾 = t.跳过 ? `：${t.跳过}` : '';
        process.stdout.write(`⏭ ${t.名字}${尾}\n`);
      }
    }
    const 全过 = 失败数 === 0 && 出错数 === 0;
    const 标记 = 全过 ? '✅' : '❌';
    const 尾巴 = 跳过数 === 0
      ? `（${记录.length} 个用例）`
      : `（${记录.length} 个用例，跳过 ${跳过数}）`;
    if (全过) {
      process.stdout.write(`${标记} 全部通过 ${尾巴}\n`);
    } else {
      process.stdout.write(
        `${标记} 通过 ${通过数} / 失败 ${失败数} / 出错 ${出错数} ${尾巴}\n`);
    }
    if (!全过) {
      // 抛断言错误表达失败 —— 脚本里设不了退出码，但抛出去 CLI 会以 1 退出。
      // （必须 **E2008**，与另外两个宿主一致）
      throw R.EXCEPTION_TYPES['断言错误'].make(
        `测试没全过：失败 ${失败数} 项、出错 ${出错数} 项`);
    }
    return true;
  }),
};

const STDLIB = {  '数学': new R.Module('数学', {
    '开方': new R.Builtin('开方', (x) => new R.FloatBox(Math.sqrt(x))),
    '平方': new R.Builtin('平方', (x) => x * x),
    '幂': new R.Builtin('幂', (a, b) => a ** b),
    '绝对值': new R.Builtin('绝对值', (x) => Math.abs(x)),
    // M117：口径与 Python / Rust 一致 —— `限制` **返回入参之一**（保类型），
    // 底 > 顶 报值错误；`符号` 永远给整数。
    '限制': new R.Builtin('限制', (v, lo, hi) => {
      if (Number(R.unwrap(lo)) > Number(R.unwrap(hi))) {
        throw R.valueErr(
          `「限制」的底（${R.display(lo)}）不能大于顶（${R.display(hi)}）`);
      }
      if (Number(R.unwrap(v)) < Number(R.unwrap(lo))) return lo;
      if (Number(R.unwrap(v)) > Number(R.unwrap(hi))) return hi;
      return v;
    }),
    '符号': new R.Builtin('符号', (v) => {
      const x = Number(R.unwrap(v));
      return x > 0 ? 1 : (x < 0 ? -1 : 0);
    }),
    '向上取整': new R.Builtin('向上取整', (x) => Math.ceil(x)),
    '向下取整': new R.Builtin('向下取整', (x) => Math.floor(x)),
    '四舍五入': new R.Builtin('四舍五入', (x, d = 0) => {
      // M153：**半入（远离零）、按十进制写法数**，与 Python / Rust 同一套：
      // `四舍五入(2.5)` = 3、`四舍五入(-2.5)` = -3、`四舍五入(1.005, 2)` = 1.01。
      // ⚠️ 与 Python 的 `round`（银行家舍入）**不同**，这是有意的破坏性变更 ——
      // 用户说「四舍五入」时想的就是「五就进一位」，金额场景也是这个口径（D90）。
      const n = Number(R.unwrap(d)) | 0;
      const 文本 = 十进制舍入(最短十进制(R.unwrap(x)), n);
      // 位数 > 0 要返回**小数**：`10.0` 与 `10` 在打印上不一样（前者是小数）。
      // JS 只有一种 number，所以用 `FloatBox` 标出「这是小数」——
      // 与 Python 的 `float` / Rust 的 `Val::Float` 对齐。
      return n <= 0 ? Math.trunc(Number(文本)) : new R.FloatBox(Number(文本));
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
    '反余弦': new R.Builtin('反余弦', (x) => {
      // M143：补上「比值 → 角度」，否则「两个向量的夹角」写不出来。
      const v = R.unwrap(x);
      if (typeof v !== 'number' || typeof v === 'boolean') {
        throw R.typeErr(`「反余弦」要传一个数，给的是 ${R.display(x)}`);
      }
      if (v < -1 || v > 1) {
        throw R.valueErr(`「反余弦」要传 -1 到 1 之间的数（给的是 ${R.display(x)}）`);
      }
      return new R.FloatBox(Math.acos(v));
    }),
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
    // 居中/左右对齐：Python 的 center/ljust/rjust 在**填不满**时原样返回；
    // ⚠️ 宽度按**显示宽度**算（一个汉字 2 列）—— M106 改的，中文表格才对得齐。
    '对齐表': new R.Builtin('对齐表', (行们, 小数位) => 对齐表文本(行们, 小数位)),
    '显示宽度': new R.Builtin('显示宽度', (s) => displayWidth(s)),
    '居中': new R.Builtin('居中', (s, width, fill = ' ') => {
      const text = String(s); const w = Number(width);
      const f = String(fill) || ' ';
      const n = displayWidth(text);
      if (n >= w) return text;
      const unit = charWidth(f.codePointAt(0)) || 1;
      const cnt = Math.floor((w - n) / unit);
      if (cnt === 0) return text;
      // CPython `str.center` 的偏置（**左多**）：`'ab'.center(5)` → `'  ab '`
      const left = Math.floor(cnt / 2) + (cnt & w & 1);
      return f.repeat(left) + text + f.repeat(cnt - left);
    }),
    '左对齐': new R.Builtin('左对齐', (s, width, fill = ' ') => {
      const text = String(s); const w = Number(width);
      const f = String(fill) || ' ';
      const n = displayWidth(text);
      if (n >= w) return text;
      const cnt = Math.floor((w - n) / (charWidth(f.codePointAt(0)) || 1));
      return cnt === 0 ? text : text + f.repeat(cnt);
    }),
    '右对齐': new R.Builtin('右对齐', (s, width, fill = ' ') => {
      const text = String(s); const w = Number(width);
      const f = String(fill) || ' ';
      const n = displayWidth(text);
      if (n >= w) return text;
      const cnt = Math.floor((w - n) / (charWidth(f.codePointAt(0)) || 1));
      return cnt === 0 ? text : f.repeat(cnt) + text;
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
      // M152：占位符与参数个数不符要**报错**（以前多给丢、少给填空串）。
      // 规则与 Python `_占位符个数`、Rust `占位符个数` 必须一致。
      const 需要 = 占位符个数(String(tpl));
      if (需要 !== null && 需要 !== rest.length) {
        throw R.valueErr(
          `「格式化」的模板里有 ${需要} 个占位符，但给了 ${rest.length} 个参数`,
          null, null,
          '模板里的 {} 与参数要一一对应；想输出真的花括号就写 {{ 或 }}');
      }
      // ⚠️ **不能再走 `String.replace(/\{[^{}]*\}/g)`**（M152 顺手修的既有分叉）：
      // 那条正则会跳过 `{{` 的转义语义，`格式化("{{}}")` 在宿主上给出 `{空}`、
      // 在 Python / Rust 上给出 `{}`（后者才对）。改成**手写扫描**，
      // 与 Rust 的 `format_template` 一步一步对齐。
      const s = String(tpl);
      let out = '';
      let auto = 0;
      for (let i = 0; i < s.length; i++) {
        const c = s[i];
        if (c === '{') {
          if (s[i + 1] === '{') { out += '{'; i += 1; continue; }
          let j = i + 1;
          while (j < s.length && s[j] !== '}') j += 1;
          if (j >= s.length) { out += c; continue; }   // 没配对：原样输出
          const inner = s.slice(i + 1, j);
          const colon = inner.indexOf(':');
          const idxPart = colon >= 0 ? inner.slice(0, colon) : inner;
          const fmt = colon >= 0 ? inner.slice(colon + 1) : '';
          const arg = idxPart === '' ? rest[auto++] : rest[Number(idxPart)];
          out += fmtOne(arg, fmt);
          i = j;
        } else if (c === '}') {
          if (s[i + 1] === '}') { out += '}'; i += 1; continue; }
          out += c;                                     // 单 `}`：原样输出
        } else {
          out += c;
        }
      }
      return out;
    }),
    // M153：数字 → 文本的三个日常写法（不用记 `:.2f` 这种暗号）
    '小数位': new R.Builtin('小数位', (v, 位 = 2) => {
      const n = Number(R.unwrap(位)) | 0;
      if (n < 0) throw R.valueErr(`「小数位」的位不能是负数（给的是 ${n}）`);
      return 十进制舍入(最短十进制(v), n);
    }),
    '百分比': new R.Builtin('百分比', (v, 位 = 0) => {
      const n = Number(R.unwrap(位)) | 0;
      if (n < 0) throw R.valueErr(`「百分比」的位不能是负数（给的是 ${n}）`);
      // 乘 100 在十进制字符串上做：`0.07 * 100` 在浮点里是 7.000000000000001
      return 十进制舍入(十进制乘(最短十进制(v), 2), n) + '%';
    }),
    '千分位': new R.Builtin('千分位', (v) => 十进制千分位(最短十进制(v))),
    '切成三段': new R.Builtin('切成三段', (s, sep) => {
      const t = String(s); const d = String(sep);
      const at = d === '' ? -1 : t.indexOf(d);
      if (at < 0) return [t, '', ''];
      return [t.slice(0, at), d, t.slice(at + d.length)];
    }),
    // ⚠️ 必须与 Python 的 `str.splitlines()` / Rust 的 `split_lines()` **同一套规则**：
    //    分隔符 = `\n` `\r`（含 `\r\n` 算一个）`\v` `\f` `\x1c`–`\x1e` `\x85` `\u2028` `\u2029`，
    //    且**末尾那一段为空就不保留**（`"甲\n\n"` → `['甲', '']`）。
    //
    // 🔴 **这是 M115 写语料时撞出来的既有分叉**（不是 M115 引入的）：旧实现用
    //    `/\r\n|\r|\n/` 切、再拿一个 filter 删空段 —— 两条都错：
    //    ① 少认了 `\v` `\f` `\x1c`–`\x1e` `\x85` `\u2028` `\u2029`；
    //    ② 那个 filter 会把**中间的**空段也删掉（它按「数组最后一个是不是空」
    //       判断，对每个元素都成立）⇒ `"甲\n\n"` 给 `['甲']`，而 Python / Rust 给 `['甲', '']`。
    '按行拆分': new R.Builtin('按行拆分', (s) => {
      const text = String(s);
      const out = [];
      let cur = '';
      for (let i = 0; i < text.length; i++) {
        const c = text[i];
        let boundary = false;
        if (c === '\r') {
          if (text[i + 1] === '\n') i++;
          boundary = true;
        } else if ('\n\v\f\x1c\x1d\x1e\x85\u2028\u2029'.indexOf(c) >= 0) {
          boundary = true;
        }
        if (boundary) { out.push(cur); cur = ''; } else { cur += c; }
      }
      if (cur !== '') out.push(cur);
      return out;
    }),
    // M51 补：`填充` / `截断` 是 M50 第一批加的（「既有模块补缺」），
    // 只做了 Python 侧，宿主静默缺失 —— 正是这个缺口促成 M51 的漂移检测。
    '填充': new R.Builtin('填充', (s, width, fill = ' ') => {
      const text = String(s); const w = Number(width); const f = String(fill);
      if ([...f].length !== 1) throw R.typeErr('「填充」的填充字符要传一个字符');
      const n = displayWidth(text);
      if (n >= w) return text;
      const cnt = Math.floor((w - n) / (charWidth(f.codePointAt(0)) || 1));
      if (cnt === 0) return text;
      const left = Math.floor(cnt / 2) + (cnt & w & 1);   // CPython：左多（不是各半）
      return f.repeat(left) + text + f.repeat(cnt - left);
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
    // M117：与 Python / Rust 同一套口径（字符集用中文短名，错误里列出可用的）。
    '随机文本': new R.Builtin('随机文本', (n, kind) => {
      const 池表 = {
        '字母数字': 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789',
        '字母': 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ',
        '小写字母': 'abcdefghijklmnopqrstuvwxyz',
        '大写字母': 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',
        '数字': '0123456789',
      };
      const 名 = kind === undefined || kind === null ? '字母数字' : String(R.unwrap(kind));
      const 池 = 池表[名];
      if (池 === undefined) {
        throw R.valueErr(
          `不认识的字符集「${名}」（可用：字母数字 / 字母 / 小写字母 / 大写字母 / 数字）`);
      }
      const m = Number(R.unwrap(n));
      if (m < 0) throw R.valueErr(`长度不能是负数（现在是 ${m}）`);
      let 出 = '';
      for (let i = 0; i < m; i++) 出 += 池[Math.floor(Math.random() * 池.length)];
      return 出;
    }),
    '按权重': new R.Builtin('按权重', (items, weights) => {
      if (!items.length) throw R.valueErr('不能从空序列里按权重挑元素');
      const 权 = [...weights].map((w) => Number(R.unwrap(w)));
      if (items.length !== 权.length) {
        throw R.valueErr(
          `选项有 ${items.length} 个，权重有 ${权.length} 个 —— 两者要一样多`);
      }
      if (权.some((x) => x < 0)) throw R.valueErr('权重不能是负数');
      const 总 = 权.reduce((a, b) => a + b, 0);
      if (!(总 > 0)) throw R.valueErr('权重加起来要大于 0（全 0 等于没有可选项）');
      const 落 = Math.random() * 总;
      let 累计 = 0;
      for (let i = 0; i < 权.length; i++) { 累计 += 权[i]; if (落 < 累计) return items[i]; }
      return items[items.length - 1];
    }),
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
  '向量': new R.Module('向量', VECTOR_FUNCS),
  '矩阵': new R.Module('矩阵', MATRIX_FUNCS),
  '编码': new R.Module('编码', ENCODE_FUNCS),
  '配置': new R.Module('配置', CFG_FUNCS),
  '浏览器': new R.Module('浏览器', BROWSER_FUNCS),
  '站点': new R.Module('站点', SITE_FUNCS),
  '打包': new R.Module('打包', PACK_FUNCS),
  '窗口': new R.Module('窗口', WINDOW_FUNCS),
  'html': new R.Module('html', HTML_FUNCS),
  '对比': new R.Module('对比', DIFF_FUNCS),
  '标识': new R.Module('标识', ID_FUNCS),
  // M112：数据持久化（走 Node 内置的 `node:sqlite`）
  '数据库': new R.Module('数据库', DB_FUNCS),
  // M114：把**一批互相独立的 IO** 同时做（M114）。⚠️ 本宿主**顺序执行**，见上面。
  '并发': new R.Module('并发', CONCURRENT_FUNCS),
  // M115：本地 HTTP 小服务。纯协议核心能用；`启动`/`空闲端口` 明确报边界，见上面。
  'HTTP服务端': new R.Module('HTTP服务端', HTTP_FUNCS),
  'HTTP路由': new R.Module('HTTP路由', ROUTE_FUNCS),
  // M136：测试（用例 / 断言 / 跳过 / 参数化 / 前后置）。
  // ⚠️ `onImport`：本宿主模块是单例，而 `测试` 要「按每次导入隔离」状态
  //    （Python 的 `__基石新建__`、Rust 在 `module()` 里重置）—— 见上面那段注释。
  '测试': new R.Module('测试', TEST_FUNCS, { onImport: _重置测试状态 }),
  // M123：Excel 读。内部工具从这里注入（zip 解析 / 表格构造 / 文本与类型）
  'Excel': new R.Module('Excel', require('./excel_mod')({
    zipBytes, readZipEntries, makeTable, pText, R, crc32,
  })),
};

function gcd(a, b) { while (b) { [a, b] = [b, a % b]; } return a; }

module.exports = { VM, Op, STDLIB };
