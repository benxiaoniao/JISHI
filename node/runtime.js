// -*- coding: utf-8 -*-
// 基石字节码虚拟机的 JavaScript 实现（M13.2）。
//
// 消费 M13.1 产出的 JSON 字节码，在 Node.js 里直接执行基石代码，
// 无需 Python、无需 C VM、无需任何第三方依赖。
//
// 语义对齐 Python 侧的 jishi/vm.py + jishi/runtime.py，
// 由对拍测试保证「同一份字节码，Python VM 与 JS VM 输出一致」。
//
// 与 Python 版的一个关键差异：JS 的异常没有 Python 那种 isinstance 继承链，
// 这里用「异常对象上的 typeName 字段」来模拟 `捕获 值错误` 的匹配。

'use strict';

// ---------------------------------------------------------------------------
// 哨兵
// ---------------------------------------------------------------------------
const UNSET = Symbol('unset');
const PENDING_NONE = Symbol('pending-none');

// ---------------------------------------------------------------------------
// 异常
// ---------------------------------------------------------------------------
class JishiError extends Error {
  constructor(typeName, title, message, line, col) {
    super(message);
    this.isJishiError = true;
    //: 基石异常类型名（「值错误」「除零错误」…），用于 `捕获` 匹配
    this.typeName = typeName;
    this.title = title;
    this.line = line ?? null;
    this.col = col ?? null;
  }
}

// 循环控制信号（穿透函数调用，由最近的循环接住）
class RunBreak extends JishiError {
  constructor(line, col) { super('中断', '中断', '中断', line, col); }
}
class RunContinue extends JishiError {
  constructor(line, col) { super('继续', '继续', '继续', line, col); }
}

function typeErr(msg, line, col) { return new JishiError('类型错误', '类型错误', msg, line, col); }
function valueErr(msg, line, col) { return new JishiError('值错误', '数值不对', msg, line, col); }
function nameErr(msg, line, col) { return new JishiError('异常', '找不到名字', msg, line, col); }
function zeroDiv(line, col) { return new JishiError('除零错误', '不能除以零', '不能除以零', line, col); }
function keyErr(msg, line, col) { return new JishiError('键错误', '找不到这个键', msg, line, col); }
function indexErr(msg, line, col) { return new JishiError('索引错误', '索引越界', msg, line, col); }
function runErr(msg, line, col) { return new JishiError('运行期错误', '运行期错误', msg, line, col); }
function fileErr(msg, line, col) { return new JishiError('文件错误', '文件错误', msg, line, col); }

// 循环信号集合
function isLoopSignal(e) { return e instanceof RunBreak || e instanceof RunContinue; }

// ---------------------------------------------------------------------------
// 类型名（中文）
// ---------------------------------------------------------------------------
function typeName(v) {
  if (v === null || v === undefined) return '空';
  if (typeof v === 'boolean') return '布尔';
  if (typeof v === 'number') {
    return Number.isInteger(v) ? '整数' : '小数';
  }
  if (isFloatBox(v)) return '小数';
  if (typeof v === 'string') return '文本';
  if (Array.isArray(v)) return '列表';
  if (v instanceof Map) return '字典';
  if (isDecimal(v)) return '精确小数';
  if (isFile(v)) return '文件';
  if (v instanceof Module) return '模块';
  if (v instanceof Builtin) return '内建函数';
  if (v instanceof ExcType) return '异常类型';
  if (v instanceof JishiClass) return '类 ' + v.name;
  if (v instanceof JishiInstance) return v.cls.name + ' 实例';
  if (v instanceof VmFunction) return '函数';
  if (v instanceof JishiError) return '异常';
  return v === Object(v) ? v.constructor.name : '未知';
}

function truthy(v) {
  if (v === null || v === undefined) return false;
  if (isDecimal(v)) return v.coef !== 0n;    // 精确小数的「零」是假（M26）
  return Boolean(v);
}

// ---------------------------------------------------------------------------
// 显示（与 Python 的 str / _display 对齐）
// ---------------------------------------------------------------------------

// 浮点装箱：JS 的 number 无法区分 5 与 5.0，
// 用 FloatBox 包装「小数」运算的结果，保证 pyStr 显示 .0 与 Python 一致。
class FloatBox {
  constructor(v) { this.value = v; }
  valueOf() { return this.value; }
}

function isFloatBox(v) { return v instanceof FloatBox; }
function unwrap(v) { return v instanceof FloatBox ? v.value : v; }

// ---------------------------------------------------------------------------
// 精确小数（M26，JS 侧实现）
// ---------------------------------------------------------------------------
// 有它是因为二进制浮点表示不了 0.1：`0.1 + 0.2 == 0.3` 是假，金额场景最烦人。
//
// 内部用「系数(BigInt) × 10^exp」，加减乘都算得准；除法算到 28 位有效数字
// （与 Python 的 decimal 默认上下文一致），舍入用「四舍六入五成双」
// （ROUND_HALF_EVEN，也是 Python Decimal 的默认）——这样两边逐字节对得上。
const DEC_PRECISION = 28;

class JishiDecimal {
  constructor(coef, exp) {
    this.coef = coef;      // BigInt
    this.exp = exp;        // number：值 = coef × 10^exp
    // 刻意**不**去掉尾随零：Python 的 Decimal 保留标度，
    // `精确("1.50")` 显示就是 1.50、`精确("1.50") * 2` 是 3.00。
    // 归一化会让两边对不上，所以标度如实保留。
  }

  static fromString(s) {
    s = String(s).trim();
    const m = /^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(s);
    if (!m || (m[2] === '' && (m[3] === undefined || m[3] === ''))) {
      throw valueErr(`不能把「${s}」当作精确小数`);
    }
    const sign = m[1] === '-' ? -1n : 1n;
    const intPart = m[2] || '0';
    const fracPart = m[3] || '';
    const e = m[4] ? parseInt(m[4], 10) : 0;
    const digits = (intPart + fracPart) || '0';
    const coef = sign * BigInt(digits);
    return new JishiDecimal(coef, e - fracPart.length);
  }

  static fromNumber(n) {
    if (!Number.isFinite(n)) throw typeErr(`「${n}」不能变成精确小数（不是有限数）`);
    // 先转字符串再解析：Number(0.1) 的二进制真值不是 0.1，
    // 但 String(0.1) 是 "0.1"——这一步是「精确」能不能名副其实的关键
    return JishiDecimal.fromString(String(n));
  }

  /** 有效数字个数（用于按 28 位精度舍入）。 */
  _digits() {
    const s = (this.coef < 0n ? -this.coef : this.coef).toString();
    return s.length;
  }

  /**
   * 把系数放大到 targetExp（target 必须 ≤ 本值 exp）。
   * 值 = coef × 10^exp，把 exp 从 0 降到 -2 需要 coef × 10^(0-(-2)) = ×100，
   * 所以是「exp 逐次减一、系数逐次乘十」——方向弄反的话
   * `精确("19.99") - 精确("20")` 会算成 20.19 而不是 -0.01。
   */
  _alignTo(targetExp) {
    let c = this.coef;
    let e = this.exp;
    while (e > targetExp) { c *= 10n; e -= 1; }
    return { c, e };
  }

  add(o) {
    const e = Math.min(this.exp, o.exp);
    const a = this._alignTo(e), b = o._alignTo(e);
    return new JishiDecimal(a.c + b.c, e);
  }

  sub(o) {
    const e = Math.min(this.exp, o.exp);
    const a = this._alignTo(e), b = o._alignTo(e);
    return new JishiDecimal(a.c - b.c, e);
  }

  mul(o) {
    return new JishiDecimal(this.coef * o.coef, this.exp + o.exp);
  }

  /** 除法：算到 28 位有效数字，ROUND_HALF_EVEN（与 Python Decimal 一致）。 */
  div(o) {
    if (o.coef === 0n) throw new JishiError('除零错误', '不能除以零', '不能除以零');
    // 目标：商的有效数字达到 28 位。被除数放大后做整数除法，再按余数决定进位。
    const extra = DEC_PRECISION + 2;
    const shift = BigInt(10) ** BigInt(extra);
    // a / b = (a.coef × 10^shift) / b.coef × 10^(a.exp - b.exp - shift)
    const num = this.coef * shift;
    let q = num / o.coef;
    const rem = num % o.coef;
    // ROUND_HALF_EVEN：余数刚好一半时，让商变成偶数
    const twice = (rem < 0n ? -rem : rem) * 2n;
    const absDen = o.coef < 0n ? -o.coef : o.coef;
    if (twice > absDen || (twice === absDen && (q % 2n !== 0n))) {
      q += (this.coef < 0n) === (o.coef < 0n) ? 1n : -1n;
    }
    let res = new JishiDecimal(q, this.exp - o.exp - extra);
    // 若商的有效数字超过 28 位，按 10 的幂截断（同样 half-even）
    res = res._roundToSignificant(DEC_PRECISION);
    // 除法结果去掉尾随零：Python 的 `Decimal("10")/Decimal("4")` 是 `2.5`
    // 而不是 `2.500000000000000000000000000`（加减乘则保留标度）
    return res._stripZeros();
  }

  /** 去掉尾随零（只在小数位上做，不改变值的形态如 25 → 25.0）。 */
  _stripZeros() {
    if (this.coef === 0n) return new JishiDecimal(0n, 0);
    let c = this.coef, e = this.exp;
    while (e < 0 && c % 10n === 0n) { c /= 10n; e += 1; }
    return new JishiDecimal(c, e);
  }

  /** 舍入到 n 位有效数字（half-even）。 */
  _roundToSignificant(n) {
    const d = this._digits();
    if (d <= n) return new JishiDecimal(this.coef, this.exp);
    return this.quantize10(this.exp + (d - n), 'half-even');
  }

  /**
   * 舍到某个 10 的幂位：targetExp=0 → 个位，-2 → 百分位，2 → 百位。
   * mode: 'half-even'（Python 默认）或 'half-up'（金额场景的「四舍五入」）
   */
  quantize10(targetExp, mode) {
    if (this.exp >= targetExp) return new JishiDecimal(this.coef, this.exp);
    const drop = targetExp - this.exp;
    const p = BigInt(10) ** BigInt(drop);
    const abs = this.coef < 0n ? -this.coef : this.coef;
    let q = abs / p;
    const rem = abs % p;
    if (mode === 'half-up') {
      if (rem * 2n >= p) q += 1n;
    } else {                                   // half-even
      const twice = rem * 2n;
      if (twice > p || (twice === p && q % 2n !== 0n)) q += 1n;
    }
    const signed = this.coef < 0n ? -q : q;
    return new JishiDecimal(signed, targetExp);
  }

  neg() { return new JishiDecimal(-this.coef, this.exp); }
  abs() { return new JishiDecimal(this.coef < 0n ? -this.coef : this.coef, this.exp); }

  /** 普通（非科学计数法）写法。 */
  toString() {
    let c = this.coef < 0n ? -this.coef : this.coef;
    const sign = this.coef < 0n ? '-' : '';
    let digits = c.toString();
    if (this.exp >= 0) {
      return sign + digits + '0'.repeat(this.exp);
    }
    const point = digits.length + this.exp;
    if (point > 0) {
      return sign + digits.slice(0, point) + '.' + digits.slice(point);
    }
    return sign + '0.' + '0'.repeat(-point) + digits;
  }

  valueOf() { return Number(this.toString()); }
}

function isDecimal(v) { return v instanceof JishiDecimal; }

/** 把值转成 JishiDecimal；不是数字/数字文本就抛中文错（对齐 _to_decimal）。 */
function toDecimal(v, line, col) {
  if (isDecimal(v)) return v;
  if (typeof v === 'boolean') {
    throw typeErr(`布尔值「${v ? '真' : '假'}」不能当精确小数`, line, col);
  }
  if (typeof v === 'number') return JishiDecimal.fromNumber(v);
  if (isFloatBox(v)) return JishiDecimal.fromNumber(v.value);
  if (typeof v === 'string') {
    try { return JishiDecimal.fromString(v); }
    catch (e) { throw typeErr(`不能把「${v}」当作精确小数`, line, col); }
  }
  throw typeErr(`不能把「${typeName(v)}」转成精确小数`, line, col);
}

/** 比较两个精确小数：-1 / 0 / 1（对齐指数后比系数）。 */
function compareDecimals(a, b) {
  const e = Math.min(a.exp, b.exp);
  const x = a._alignTo(e).c, y = b._alignTo(e).c;
  return x < y ? -1 : (x > y ? 1 : 0);
}

// ---------------------------------------------------------------------------
// 文件对象（M26，JS 侧实现）
// ---------------------------------------------------------------------------
// 真句柄（不是整读进内存），所以大文件能按行遍历，「用完要关」也才有意义。
const fs = require('fs');

const FILE_MODES = { '读': 'r', '写': 'w', '追加': 'a', '读写': 'r+' };
const FILE_MODES_EN = ['r', 'w', 'a', 'r+', 'w+', 'a+'];

class JishiFile {
  constructor(fd, path, mode) {
    this.fd = fd;           // 打开的 fd；关闭后置 -1
    this.path = path;
    this.mode = mode;
    this.closed = false;
    this._buf = '';         // 按行读的缓冲
    this._eof = false;
  }

  toString() {
    return `<文件 ${this.path}（${this.closed ? '已关闭' : this.mode}）>`;
  }

  _check(what) {
    if (this.closed) {
      throw valueErr(`文件「${this.path}」已经关闭了，不能再「${what}」`);
    }
  }

  /** 读全部剩余内容（或前 n 个字符） */
  readAll(n) {
    this._check('读');
    if (this._eof && this._buf === '') return '';
    let data;
    if (this._buf !== '' || this._eof) {
      data = this._buf;
      this._buf = '';
    } else {
      const size = n === undefined || n === -1 ? 65536 : n;
      data = fs.readFileSync(this.fd, { encoding: 'utf-8' });
      this._eof = true;
    }
    if (n === undefined || n === -1) return data;
    return data.slice(0, n);
  }

  readLine() {
    this._check('读行');
    if (this._buf === '' && !this._eof) {
      this._buf = fs.readFileSync(this.fd, { encoding: 'utf-8' });
      this._eof = true;
    }
    const idx = this._buf.indexOf('\n');
    if (idx < 0) {
      if (this._buf === '') return null;
      const rest = this._buf;
      this._buf = '';
      return rest.replace(/\r$/, '');
    }
    const line = this._buf.slice(0, idx);
    this._buf = this._buf.slice(idx + 1);
    return line.replace(/\r$/, '');
  }

  readAllLines() {
    const out = [];
    for (;;) {
      const ln = this.readLine();
      if (ln === null) break;
      out.push(ln);
    }
    return out;
  }

  write(text) {
    this._check('写');
    if (typeof text !== 'string') {
      throw typeErr(`「写」需要文本，得到了「${typeName(text)}」`);
    }
    fs.writeSync(this.fd, text);
  }

  writeLine(text) {
    this._check('写行');
    if (typeof text !== 'string') {
      throw typeErr(`「写行」需要文本，得到了「${typeName(text)}」`);
    }
    fs.writeSync(this.fd, text + '\n');
  }

  close() {
    if (!this.closed) { fs.closeSync(this.fd); this.closed = true; }
  }

  /** 按行迭代（`遍历 行 在 f`） */
  [Symbol.iterator]() {
    const self = this;
    return {
      next() {
        if (self.closed) throw valueErr(`文件「${self.path}」已经关闭了，不能再遍历`);
        const line = self.readLine();
        return line === null ? { done: true } : { value: line, done: false };
      },
    };
  }
}

function isFile(v) { return v instanceof JishiFile; }

function display(v) {
  if (v === null || v === undefined) return '空';
  if (v === true) return '真';
  if (v === false) return '假';
  if (typeof v === 'string') return v;
  if (v instanceof Map) return dictRepr(v);
  if (Array.isArray(v)) return '[' + v.map(display).join(', ') + ']';
  if (v instanceof JishiError) return v.message;
  if (isFloatBox(v)) return String(v.value);
  return String(v);
}

// 打印用的 str()：字符串不带引号，列表/字典用 repr 风格（字符串带引号）。
// 空/真/假 用中文——与 Python 侧 jishi_repr(top=True) 逐字对齐（M27）。
function pyStr(v) {
  if (v === null || v === undefined) return '空';
  if (v === true) return '真';
  if (v === false) return '假';
  if (typeof v === 'string') return v;
  // 列表/字典内部元素用 repr（字符串带引号，与 Python 一致）
  if (Array.isArray(v)) return '[' + v.map(pyRepr).join(', ') + ']';
  if (v instanceof Map) return dictPyRepr(v);
  if (isFloatBox(v)) {
    const x = v.value;
    return Number.isInteger(x) ? x.toFixed(1) : String(x);
  }
  return String(v);
}

function pyRepr(v) {
  if (typeof v === 'string') {
    // Python repr：单引号 + 转义
    return "'" + v.replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n') + "'";
  }
  if (v === true) return '真';
  if (v === false) return '假';
  if (v === null || v === undefined) return '空';
  if (Array.isArray(v)) return '[' + v.map(pyRepr).join(', ') + ']';
  if (v instanceof Map) return dictPyRepr(v);
  if (isFloatBox(v)) {
    const x = v.value;
    return Number.isInteger(x) ? x.toFixed(1) : String(x);
  }
  return String(v);
}

function dictRepr(m) {
  const parts = [];
  for (const [k, val] of m) parts.push(display(k) + ': ' + display(val));
  return '{' + parts.join(', ') + '}';
}

function dictPyRepr(m) {
  const parts = [];
  for (const [k, val] of m) parts.push(pyRepr(k) + ': ' + pyRepr(val));
  return '{' + parts.join(', ') + '}';
}

// ---------------------------------------------------------------------------
// 内建函数
// ---------------------------------------------------------------------------
class Builtin {
  constructor(name, fn) { this.name = name; this.fn = fn; }
}

// ---------------------------------------------------------------------------
// 异常类型（`值错误("…")` 的构造器 + `捕获 值错误` 的匹配）
// ---------------------------------------------------------------------------
class ExcType {
  constructor(name) { this.name = name; }
  // 构造异常实例
  make(message, line, col) {
    if (this.name === '除零错误') return new JishiError('除零错误', '不能除以零', '不能除以零', line, col);
    if (this.name === '类型错误') return new JishiError('类型错误', '类型错误', message, line, col);
    if (this.name === '值错误') return new JishiError('值错误', '数值不对', message, line, col);
    if (this.name === '索引错误') return new JishiError('索引错误', '索引越界', message, line, col);
    if (this.name === '键错误') return new JishiError('键错误', '找不到这个键', message, line, col);
    if (this.name === '文件错误') return new JishiError('文件错误', '文件错误', message, line, col);
    return new JishiError(this.name, this.name, message, line, col);
  }
}

// 异常类型注册表（与 Python EXCEPTION_TYPES 对齐）
const EXCEPTION_TYPES = {
  '异常': new ExcType('异常'),
  '运行期错误': new ExcType('运行期错误'),
  '类型错误': new ExcType('类型错误'),
  '值错误': new ExcType('值错误'),
  '索引错误': new ExcType('索引错误'),
  '键错误': new ExcType('键错误'),
  '除零错误': new ExcType('除零错误'),
  '文件错误': new ExcType('文件错误'),
};

function newBuiltins() {
  return {
    '打印': new Builtin('打印', (...args) => {
      process.stdout.write(args.map(pyStr).join(' ') + '\n');
      return null;
    }),
    '输入': new Builtin('输入', (prompt = '') => {
      // 同步读一行（仅交互场景用；非交互则返回空串避免阻塞）
      return '';
    }),
    '整数': new Builtin('整数', (v) => {
      const n = Math.trunc(Number(unwrap(v)));
      if (Number.isNaN(n)) throw typeErr(`不能把「${display(v)}」转成整数`);
      return n;
    }),
    '小数': new Builtin('小数', (v) => {
      const n = Number(v);
      if (Number.isNaN(n)) throw typeErr(`不能把「${display(v)}」转成小数`);
      return new FloatBox(n);
    }),
    '文本': new Builtin('文本', (v) => pyStr(v)),
    '精确': new Builtin('精确', (v) => toDecimal(v)),
    '打开': new Builtin('打开', (path, mode = '读') => {
      if (typeof path !== 'string') {
        throw typeErr(`「打开」的第一个参数要是文件路径，得到了「${typeName(path)}」`);
      }
      if (typeof mode !== 'string') {
        throw typeErr(`「打开」的第二个参数（模式）要是文本，得到了「${typeName(mode)}」`);
      }
      let pyMode = FILE_MODES[mode];
      if (pyMode === undefined && FILE_MODES_EN.includes(mode)) pyMode = mode;
      if (pyMode === undefined) {
        throw typeErr(`不认识的打开模式「${mode}」`);
      }
      let fd;
      try {
        fd = fs.openSync(path, pyMode);
      } catch (e) {
        throw fileErr(`找不到文件或目录：${path}`);
      }
      return new JishiFile(fd, path, mode);
    }),
    '进入上下文': new Builtin('进入上下文', (obj) => enterContext(obj)),
    '退出上下文': new Builtin('退出上下文', (obj) => exitContext(obj)),
    '长度': new Builtin('长度', (v) => {
      if (typeof v === 'string' || Array.isArray(v)) return v.length;
      if (v instanceof Map) return v.size;
      throw typeErr(`「${display(v)}」没有长度`);
    }),
    '范围': new Builtin('范围', (...args) => {
      if (args.length === 1) return range(0, args[0], 1);
      if (args.length === 2) return range(args[0], args[1], 1);
      if (args.length === 3) return range(args[0], args[1], args[2]);
      throw typeErr('「范围」需要 1 到 3 个整数参数');
    }),
    '最大': new Builtin('最大', (v) => {
      if (!Array.isArray(v) || v.length === 0) throw typeErr(`「${display(v)}」不能求最大值`);
      if (v.some(isDecimal)) {
        let best = toDecimal(v[0]);
        for (const x of v.slice(1)) {
          const d = toDecimal(x);
          if (compareDecimals(d, best) > 0) best = d;
        }
        return best;
      }
      return Math.max(...v);
    }),
    '最小': new Builtin('最小', (v) => {
      if (!Array.isArray(v) || v.length === 0) throw typeErr(`「${display(v)}」不能求最小值`);
      if (v.some(isDecimal)) {
        let best = toDecimal(v[0]);
        for (const x of v.slice(1)) {
          const d = toDecimal(x);
          if (compareDecimals(d, best) < 0) best = d;
        }
        return best;
      }
      return Math.min(...v);
    }),
    '总和': new Builtin('总和', (v) => {
      if (!Array.isArray(v)) throw typeErr(`「${display(v)}」不能求和`);
      // 精确小数要走十进制起点（与 Python 侧一致：sum 从整数 0 起算会出错）
      if (v.some(isDecimal)) {
        let acc = new JishiDecimal(0n, 0);
        for (const x of v) acc = acc.add(toDecimal(x));
        return acc;
      }
      const s = v.reduce((a, b) => a + unwrap(b), 0);
      // 含小数则返回 FloatBox
      return v.some(isFloatBox) ? new FloatBox(s) : s;
    }),
    '类型': new Builtin('类型', (v) => typeName(v)),
    '反转': new Builtin('反转', (v) => {
      if (typeof v === 'string') return v.split('').reverse().join('');
      if (Array.isArray(v)) return [...v].reverse();
      throw typeErr(`「${display(v)}」不能反转，需要列表或文本`);
    }),
    // 异常类型：既是构造器也可用于捕获匹配
    ...Object.fromEntries(Object.entries(EXCEPTION_TYPES).map(([k, v]) => [k, v])),
  };
}

function range(a, b, step) {
  const out = [];
  if (step > 0) { for (let i = a; i < b; i += step) out.push(i); }
  else if (step < 0) { for (let i = a; i > b; i += step) out.push(i); }
  return out;
}

// ---------------------------------------------------------------------------
// 模块
// ---------------------------------------------------------------------------
class Module {
  constructor(name, attrs) { this.name = name; this.attrs = attrs; }
}

// ---------------------------------------------------------------------------
// 类与实例
// ---------------------------------------------------------------------------
class JishiClass {
  constructor(name, methods, base = null) {
    this.name = name;
    this.base = base;
    // 继承：合并基类方法表，子类覆盖
    const merged = new Map();
    if (base) for (const [k, v] of base.methods) merged.set(k, v);
    for (const [k, v] of methods) merged.set(k, v);
    this.methods = merged;
  }
}

class JishiInstance {
  constructor(cls) { this.cls = cls; this.fields = new Map(); }

  /**
   * 自定义对象打印（M26）：类里定义了 `文本(自身)` 就用它的返回值。
   *
   * 放在 `toString()` 里，`String(实例)`、`pyStr`、`display`、容器嵌套
   * 就全都自动走同一条路——与 Python 侧把逻辑放进 `__repr__` 是同一个手法。
   * 没定义时回落到 `<类名 实例>`。
   */
  toString() {
    const fn = this.cls.methods.get('文本');
    if (!fn) return `<${this.cls.name} 实例>`;
    let result;
    try {
      result = fn.vm.callFunction(fn, [this], null, undefined, undefined);
    } catch (e) {
      if (e instanceof JishiError) throw e;
      return `<${this.cls.name} 实例>`;
    }
    if (typeof result !== 'string') {
      throw typeErr(`类「${this.cls.name}」的「文本」方法返回了`
        + `「${typeName(result)}」，要返回文本`);
    }
    return result;
  }
}

// ---------------------------------------------------------------------------
// 函数对象
// ---------------------------------------------------------------------------
class VmFunction {
  constructor(name, code, cells, vm, defaults = null) {
    this.name = name;
    this.code = code;
    this.cells = cells;       // 闭包单元数组（Cell）
    this.vm = vm;
    this.defaults = defaults; // 与 code.params 对齐，null 表示无默认
  }
}

// 闭包单元
class Cell {
  constructor(value = UNSET) { this.value = value; }
}

// ---------------------------------------------------------------------------
// 运算
// ---------------------------------------------------------------------------
function applyBinop(op, l, r, line, col) {
  // 精确小数优先：任一边是它就整个走十进制（与 Python 侧一致）
  if (isDecimal(l) || isDecimal(r)) {
    const a = toDecimal(l, line, col), b = toDecimal(r, line, col);
    switch (op) {
      case '+': return a.add(b);
      case '-': return a.sub(b);
      case '*': return a.mul(b);
      case '/': return a.div(b);
      case '//': {
        const q = a.div(b);
        return q.quantize10(q.exp, 'half-even');   // 向下取整到整数位
      }
      case '%': {
        const q = a.div(b);
        const intPart = q.quantize10(q.exp, 'half-even');
        return a.sub(b.mul(new JishiDecimal(BigInt(Math.trunc(Number(intPart.toString()))), 0)));
      }
      case '**': {
        const n = Number(b.toString());
        if (!Number.isInteger(n) || n < 0) {
          throw typeErr(`精确小数的幂要是非负整数，得到了「${b.toString()}」`, line, col);
        }
        let acc = new JishiDecimal(1n, 0);
        for (let i = 0; i < n; i++) acc = acc.mul(a);
        return acc;
      }
      default: throw runErr(`不支持的运算符「${op}」`, line, col);
    }
  }
  // 先把 FloatBox 拆开，算出结果后再按 Python 语义决定是否包回 FloatBox
  const fl = isFloatBox(l), fr = isFloatBox(r);
  const lv = unwrap(l), rv = unwrap(r);
  let result;
  let isFloat = fl || fr;   // 参与运算的是小数 → Python 结果一般是 float
  switch (op) {
    case '+': result = lv + rv; break;
    case '-': result = lv - rv; break;
    case '*': result = lv * rv; break;
    case '/':
      if (rv === 0) throw zeroDiv(line, col);
      result = lv / rv; isFloat = true; break;   // / 永远产 float
    case '//':
      if (rv === 0) throw zeroDiv(line, col);
      result = Math.floor(lv / rv); isFloat = false; break;  // // 产 int
    case '%':
      if (rv === 0) throw zeroDiv(line, col);
      result = lv % rv; break;
    case '**': result = lv ** rv; break;
    default: throw runErr(`不支持的运算符「${op}」`, line, col);
  }
  // 若 result 是 NaN/Infinity（溢出/无效），Python 会抛或得 inf，这里简单放行
  return isFloat ? new FloatBox(result) : result;
}

function applyUnary(op, v, line, col) {
  if (op === '-') {
    if (isDecimal(v)) return v.neg();
    return -v;
  }
  if (op === '非') return !truthy(v);
  throw runErr(`不支持的一元运算符「${op}」`, line, col);
}

function applyCompare(op, l, r, line, col) {
  // 精确小数：算术比较走十进制（混合 0.3 也能相等）；成员测试/同一性
  // （在·不在·是·不是）在更上层处理，不走这里——与 Python 侧一致
  if ((isDecimal(l) || isDecimal(r)) && ['==', '!=', '<', '>', '<=', '>='].includes(op)) {
    const a = toDecimal(l, line, col), b = toDecimal(r, line, col);
    const cmp = compareDecimals(a, b);
    switch (op) {
      case '==': return cmp === 0;
      case '!=': return cmp !== 0;
      case '<': return cmp < 0;
      case '>': return cmp > 0;
      case '<=': return cmp <= 0;
      case '>=': return cmp >= 0;
    }
  }
  const lv = unwrap(l), rv = unwrap(r);
  switch (op) {
    case '==': return lv === rv;
    case '!=': return lv !== rv;
    case '<': return lv < rv;
    case '>': return lv > rv;
    case '<=': return lv <= rv;
    case '>=': return lv >= rv;
  }
  throw runErr(`不支持的比较「${op}」`, line, col);
}

// ---------------------------------------------------------------------------
// 属性 / 下标
// ---------------------------------------------------------------------------
function methodTable(obj) {
  if (Array.isArray(obj)) return LIST_METHODS;
  if (obj instanceof Map) return DICT_METHODS;
  if (typeof obj === 'string') return STR_METHODS;
  if (isFile(obj)) return FILE_METHODS;
  if (isDecimal(obj)) return DECIMAL_METHODS;
  return null;
}

function getAttr(obj, attr, line, col) {
  if (obj instanceof JishiError) {
    if (attr === '类型') return obj.typeName;
    if (attr === '消息') return obj.message;
    throw typeErr(`「异常」没有属性「${attr}」`, line, col);
  }
  if (obj instanceof Module) {
    if (attr in obj.attrs) return obj.attrs[attr];
    throw nameErr(`模块「${obj.name}」里没有「${attr}」`, line, col);
  }
  if (obj instanceof JishiInstance) {
    if (obj.fields.has(attr)) return obj.fields.get(attr);
    if (obj.cls.methods.has(attr)) {
      return new BoundUserMethod(obj.cls.methods.get(attr), obj, attr, line, col);
    }
    throw typeErr(`「${typeName(obj)}」没有属性「${attr}」`, line, col);
  }
  if (obj instanceof JishiClass) {
    if (obj.methods.has(attr)) return obj.methods.get(attr);
    throw typeErr(`「类 ${obj.name}」没有方法「${attr}」`, line, col);
  }
  const table = methodTable(obj);
  if (table && attr in table) {
    return new BoundMethod(table[attr], obj, attr, line, col);
  }
  throw typeErr(`「${typeName(obj)}」没有属性「${attr}」`, line, col);
}

function setAttr(obj, attr, value, line, col) {
  if (obj instanceof Module) { obj.attrs[attr] = value; return; }
  if (obj instanceof JishiInstance) { obj.fields.set(attr, value); return; }
  throw typeErr(`不能给「${display(obj)}」的属性「${attr}」赋值`, line, col);
}

function getItem(obj, index, line, col) {
  if (Array.isArray(obj)) {
    const i = index;
    if (!Number.isInteger(i) || i < 0 || i >= obj.length) {
      throw indexErr(`下标「${display(index)}」越界`, line, col);
    }
    return obj[i];
  }
  if (obj instanceof Map) {
    if (!obj.has(index)) throw keyErr(`字典里没有键「${display(index)}」`, line, col);
    return obj.get(index);
  }
  if (typeof obj === 'string') {
    const i = index;
    if (!Number.isInteger(i) || i < 0 || i >= obj.length) {
      throw indexErr(`下标「${display(index)}」越界`, line, col);
    }
    return obj[i];
  }
  throw typeErr(`「${typeName(obj)}」不能取下标`, line, col);
}

function setItem(obj, index, value, line, col) {
  if (Array.isArray(obj)) {
    obj[index] = value;
    return;
  }
  if (obj instanceof Map) { obj.set(index, value); return; }
  throw typeErr(`「${typeName(obj)}」不能按下标赋值`, line, col);
}

// 绑定方法（列表/字典/字符串的中文方法）
class BoundMethod {
  constructor(impl, obj, name, line, col) { this.impl = impl; this.obj = obj; this.name = name; this.line = line; this.col = col; }
}

// 绑定到实例的用户方法（自身 首参）
class BoundUserMethod {
  constructor(fn, instance, name, line, col) { this.fn = fn; this.instance = instance; this.name = name; this.line = line; this.col = col; }
}

// ---------------------------------------------------------------------------
// 调用
// ---------------------------------------------------------------------------
// 上下文管理器协议（M26）：`用 X 为 f：` 脱糖成
//   __用_N__ = X；f = 进入上下文(__用_N__)；尝试…最终 退出上下文(__用_N__)
// 所以这里只需要「找方法并调用」——与 Python 侧 _b_enter_context 同一套规则。
function ctxMethod(obj, name, line, col) {
  if (obj instanceof JishiInstance) {
    const fn = obj.cls.methods.get(name);
    if (fn) return () => fn.vm.callFunction(fn, [obj], null, line, col);
    return null;
  }
  const table = methodTable(obj);
  if (table && name in table) return () => table[name](obj, [], line, col);
  return null;
}

function enterContext(obj, line, col) {
  const enter = ctxMethod(obj, '进入', line, col);
  if (enter) return enter();
  if (ctxMethod(obj, '退出', line, col) || ctxMethod(obj, '关闭', line, col)) {
    return obj;          // 文件/锁这类「进入即自身」的写法不必硬写「进入」
  }
  const name = typeName(obj);
  const extra = (Array.isArray(obj) || obj instanceof Map)
    ? `「${name}」用完不需要收拾，所以不能这样写；只是想在块里用一下的话，直接写普通语句就行`
    : '上下文管理器要有「退出」或「关闭」方法（离开时自动调用），也可以再定义「进入」来决定绑定什么值。最常见的用法：用 打开("数据.txt") 为 f：';
  throw typeErr(`「${name}」不能当上下文管理器用（${extra}）`, line, col);
}

function exitContext(obj, line, col) {
  for (const name of ['退出', '关闭']) {
    const fn = ctxMethod(obj, name, line, col);
    if (fn) { fn(); return null; }
  }
  return null;
}

function makeInstance(cls, args, line, col) {
  const inst = new JishiInstance(cls);
  const init = cls.methods.get('初始化');
  if (init) {
    init.vm.callFunction(init, [inst, ...args], null, line, col);
  } else if (args.length > 0) {
    throw typeErr(`类「${cls.name}」没有构造方法「初始化」，不能传参数`, line, col);
  }
  return inst;
}

// ---------------------------------------------------------------------------
// 容器构造
// ---------------------------------------------------------------------------
function unpackValues(value, n, star, line, col) {
  // star >= 0 时是星号解包（M25）：该位置收「其余」成列表
  let seq;
  if (Array.isArray(value)) seq = value;
  else if (typeof value === 'string') seq = [...value];
  else if (value instanceof Map) seq = [...value.keys()];
  else {
    throw typeErr(`解包赋值需要列表，得到了「${typeName(value)}」`, line, col);
  }
  if (star == null || star < 0) {
    if (seq.length !== n) {
      throw valueErr(`解包需要 ${n} 个值，实际有 ${seq.length} 个`, line, col);
    }
    return [...seq];
  }
  const nhead = star;
  const ntail = n - 1 - star;
  if (seq.length < nhead + ntail) {
    throw valueErr(`解包至少需要 ${nhead + ntail} 个值，实际有 ${seq.length} 个`, line, col);
  }
  const head = seq.slice(0, nhead);
  const tail = ntail > 0 ? seq.slice(seq.length - ntail) : [];
  const mid = ntail > 0 ? seq.slice(nhead, seq.length - ntail) : seq.slice(nhead);
  return [...head, mid, ...tail];
}

function buildDict(pairs, line, col) {
  const out = new Map();
  for (const [k, v] of pairs) {
    // JS 里 Map 键可为任意值；Python 里不可哈希的键会报错，这里只拦 function
    if (typeof k === 'function') throw typeErr(`「${display(k)}」不能作为字典的键`, line, col);
    out.set(k, v);
  }
  return out;
}

// ---------------------------------------------------------------------------
// 名字查找（含"你是不是想写"建议）
// ---------------------------------------------------------------------------
function lookupName(name, pool, line, col) {
  if (name in pool) return pool[name];
  throw nameErr(`找不到名字「${name}」`, line, col);
}

// ---------------------------------------------------------------------------
// 方法实现
// ---------------------------------------------------------------------------
function needArgs(name, args, lo, hi, line, col) {
  if (args.length < lo || args.length > hi) {
    const need = lo === hi ? `${lo} 个` : `${lo} 到 ${hi} 个`;
    throw typeErr(`方法「${name}」需要 ${need}参数，但传了 ${args.length} 个`, line, col);
  }
}

const LIST_METHODS = {
  '追加': (obj, args, line, col) => { needArgs('追加', args, 1, 1, line, col); obj.push(args[0]); return null; },
  '插入': (obj, args, line, col) => { needArgs('插入', args, 2, 2, line, col); obj.splice(args[0], 0, args[1]); return null; },
  '移除': (obj, args, line, col) => {
    needArgs('移除', args, 1, 1, line, col);
    const i = obj.indexOf(args[0]);
    if (i < 0) throw valueErr(`列表里没有「${display(args[0])}」，没法移除`, line, col);
    obj.splice(i, 1); return null;
  },
  '弹出': (obj, args, line, col) => {
    needArgs('弹出', args, 0, 1, line, col);
    if (obj.length === 0) throw valueErr('列表是空的，没有东西可以弹出', line, col);
    return args.length ? obj.splice(args[0], 1)[0] : obj.pop();
  },
  '排序': (obj, args, line, col) => { needArgs('排序', args, 0, 0, line, col); obj.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0)); return null; },
  '反转': (obj, args, line, col) => { needArgs('反转', args, 0, 0, line, col); obj.reverse(); return null; },
  '清空': (obj, args, line, col) => { needArgs('清空', args, 0, 0, line, col); obj.length = 0; return null; },
  '索引': (obj, args, line, col) => {
    needArgs('索引', args, 1, 1, line, col);
    const i = obj.indexOf(args[0]);
    if (i < 0) throw valueErr(`列表里没有「${display(args[0])}」，找不到它的位置`, line, col);
    return i;
  },
  '计数': (obj, args, line, col) => { needArgs('计数', args, 1, 1, line, col); return obj.filter(x => x === args[0]).length; },
  '包含': (obj, args, line, col) => { needArgs('包含', args, 1, 1, line, col); return obj.includes(args[0]); },
};

const DICT_METHODS = {
  '获取': (obj, args, line, col) => {
    needArgs('获取', args, 1, 2, line, col);
    const dflt = args.length === 2 ? args[1] : null;
    return obj.has(args[0]) ? obj.get(args[0]) : dflt;
  },
  '键': (obj, args, line, col) => { needArgs('键', args, 0, 0, line, col); return [...obj.keys()]; },
  '值': (obj, args, line, col) => { needArgs('值', args, 0, 0, line, col); return [...obj.values()]; },
  '包含': (obj, args, line, col) => { needArgs('包含', args, 1, 1, line, col); return obj.has(args[0]); },
  '更新': (obj, args, line, col) => {
    needArgs('更新', args, 1, 1, line, col);
    if (!(args[0] instanceof Map)) throw typeErr(`「更新」需要一个字典参数`, line, col);
    for (const [k, v] of args[0]) obj.set(k, v);
    return null;
  },
  '弹出': (obj, args, line, col) => {
    needArgs('弹出', args, 1, 2, line, col);
    if (obj.has(args[0])) {
      const v = obj.get(args[0]); obj.delete(args[0]); return v;
    }
    if (args.length === 2) return args[1];
    throw keyErr(`字典里没有键「${display(args[0])}」`, line, col);
  },
  '清空': (obj, args, line, col) => { needArgs('清空', args, 0, 0, line, col); obj.clear(); return null; },
};

const STR_METHODS = {
  '拆分': (obj, args, line, col) => {
    needArgs('拆分', args, 0, 1, line, col);
    const sep = args.length ? args[0] : undefined;
    return sep === undefined ? obj.trim().split(/\s+/) : obj.split(sep);
  },
  '替换': (obj, args, line, col) => { needArgs('替换', args, 2, 2, line, col); return obj.split(args[0]).join(args[1]); },
  '查找': (obj, args, line, col) => { needArgs('查找', args, 1, 1, line, col); return obj.indexOf(args[0]); },
  '大写': (obj, args, line, col) => { needArgs('大写', args, 0, 0, line, col); return obj.toUpperCase(); },
  '小写': (obj, args, line, col) => { needArgs('小写', args, 0, 0, line, col); return obj.toLowerCase(); },
  '去空白': (obj, args, line, col) => { needArgs('去空白', args, 0, 0, line, col); return obj.trim(); },
  '开头是': (obj, args, line, col) => { needArgs('开头是', args, 1, 1, line, col); return obj.startsWith(args[0]); },
  '结尾是': (obj, args, line, col) => { needArgs('结尾是', args, 1, 1, line, col); return obj.endsWith(args[0]); },
  '包含': (obj, args, line, col) => { needArgs('包含', args, 1, 1, line, col); return obj.includes(args[0]); },
  '转整数': (obj, args, line, col) => {
    needArgs('转整数', args, 0, 0, line, col);
    const n = Number(obj);
    if (!Number.isInteger(n)) throw valueErr(`「${obj}」不能转成整数`, line, col);
    return n;
  },
  '转小数': (obj, args, line, col) => {
    needArgs('转小数', args, 0, 0, line, col);
    const n = Number(obj);
    if (Number.isNaN(n)) throw valueErr(`「${obj}」不能转成小数`, line, col);
    return n;
  },
};

// -- 文件方法（M26）--------------------------------------------------------
// 方法签名统一为 (obj, args, line, col)，与上面的列表/字典/文本一致。
const FILE_METHODS = {
  '读': (obj, args, line, col) => {
    needArgs('读', args, 0, 1, line, col);
    const n = args.length ? (isFloatBox(args[0]) ? args[0].value : args[0]) : -1;
    return obj.readAll(n);
  },
  '读行': (obj, args, line, col) => {
    needArgs('读行', args, 0, 0, line, col);
    return obj.readLine();
  },
  '读所有行': (obj, args, line, col) => {
    needArgs('读所有行', args, 0, 0, line, col);
    return obj.readAllLines();
  },
  '写': (obj, args, line, col) => {
    needArgs('写', args, 1, 1, line, col);
    obj.write(args[0]);
    return null;
  },
  '写行': (obj, args, line, col) => {
    needArgs('写行', args, 1, 1, line, col);
    obj.writeLine(args[0]);
    return null;
  },
  '关闭': (obj, args, line, col) => {
    needArgs('关闭', args, 0, 0, line, col);
    obj.close();              // 重复关闭不报错，与 Python 侧一致
    return null;
  },
};

// -- 精确小数方法（M26）----------------------------------------------------
const DECIMAL_METHODS = {
  '舍入': (obj, args, line, col) => {
    needArgs('舍入', args, 0, 1, line, col);
    const digits = args.length ? args[0] : 2;
    if (typeof digits !== 'number' || !Number.isInteger(digits)) {
      throw typeErr(`「舍入」的位数要是整数，得到了「${typeName(digits)}」`, line, col);
    }
    // 金额场景按「四舍五入」（half-up），不是 Python 默认的银行家舍入
    return obj.quantize10(-digits, 'half-up');
  },
  '绝对值': (obj, args, line, col) => {
    needArgs('绝对值', args, 0, 0, line, col);
    return obj.abs();
  },
  '转文本': (obj, args, line, col) => {
    needArgs('转文本', args, 0, 0, line, col);
    return obj.toString();
  },
};

// ---------------------------------------------------------------------------
// 异常匹配（`捕获 值错误`）
// ---------------------------------------------------------------------------

// 异常继承关系（子类名 → 父类名），用于「基类捕获抓子类」
const EXC_HIERARCHY = {
  '除零错误': '运行期错误',
  '类型错误': '运行期错误',
  '索引错误': '运行期错误',
  '键错误': '运行期错误',
  '值错误': '运行期错误',
  '文件错误': '运行期错误',
  '运行期错误': '异常',
};

function excIsInstance(excTypeName, condName) {
  if (condName === '异常') return true;
  let t = excTypeName;
  while (t) {
    if (t === condName) return true;
    t = EXC_HIERARCHY[t];
  }
  return false;
}

function exceptionMatches(exc, cond, line, col) {
  if (cond instanceof ExcType) {
    return excIsInstance(exc.typeName, cond.name);
  }
  if (cond instanceof JishiError) return excIsInstance(exc.typeName, cond.typeName);
  throw typeErr(`「${display(cond)}」不能用于捕获`, line, col);
}

// make_exception：把 `抛出` 的值规范化成异常对象
function makeException(value, line, col) {
  if (value instanceof JishiError) return value;
  if (value instanceof ExcType) return value.make('', line, col);
  return new JishiError('异常', '异常', display(value), line, col);
}

module.exports = {
  UNSET, PENDING_NONE,
  JishiError, RunBreak, RunContinue,
  typeName, truthy, display, pyStr, pyRepr,
  FloatBox, isFloatBox, unwrap,
  JishiDecimal, isDecimal, toDecimal, compareDecimals,
  JishiFile, isFile, enterContext, exitContext,
  Builtin, ExcType, EXCEPTION_TYPES, newBuiltins,
  Module, JishiClass, JishiInstance, VmFunction, Cell,
  applyBinop, applyUnary, applyCompare,
  methodTable, getAttr, setAttr, getItem, setItem,
  BoundMethod, BoundUserMethod, makeInstance,
  unpackValues, buildDict, lookupName,
  isLoopSignal, exceptionMatches, makeException,
  nameErr, typeErr, valueErr, runErr, fileErr,
};
