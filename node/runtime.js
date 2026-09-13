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
  if (typeof v === 'bigint') return '整数';    // 大整数也是「整数」（M32）
  if (typeof v === 'number') {
    return Number.isInteger(v) ? '整数' : '小数';
  }
  if (isFloatBox(v)) return '小数';
  if (typeof v === 'string') return '文本';
  if (Array.isArray(v)) return '列表';
  if (v instanceof Map) return '字典';
  if (v instanceof JishiSet) return '集合';
  if (isSlice(v)) return '切片';
  if (isDecimal(v)) return '精确小数';
  if (isFile(v)) return '文件';
  if (v instanceof Module) return '模块';
  if (v instanceof Builtin) return '内建函数';
  if (v instanceof ExcType) return '异常类型';
  if (v instanceof JishiClass) return '类 ' + v.name;
  if (v instanceof JishiInstance) return v.cls.name + ' 实例';
  if (v instanceof VmFunction) return '函数';
  if (v instanceof JishiError) return '异常';
  // 宿主内部对象可以自报类型名（M31）：`日期.解析` 的返回值要报 'datetime'，
  // 好与 Python 侧 `类型(datetime 对象)` 一致（见 vm.js 的 JishiDate）。
  if (v !== null && typeof v === 'object' && typeof v.__jishiType === 'string') {
    return v.__jishiType;
  }
  return v === Object(v) ? v.constructor.name : '未知';
}

// ---------------------------------------------------------------------------
// 大整数（M32）
// ---------------------------------------------------------------------------
// JS 的 number 是双精度：`9223372036854775807` 会变成 `9223372036854776000`、
// `2 ** 64` 给 `18446744073709552000`——**不报错但结果错**。
// 这些小整数仍是 number（性能不变），只在必要时切到 BigInt：
//   ① 任一边已经是 BigInt；② number 算出来的整数结果超出安全范围。
// BigInt 是任意精度，与 Python 的 int 完全对齐。

/** 是不是大整数。 */
function isBig(v) { return typeof v === 'bigint'; }

/** 整数类值：整数 number / 大整数 / 布尔（Python 里布尔也是整数）。 */
function isIntLike(v) {
  return typeof v === 'bigint' || typeof v === 'boolean'
    || (typeof v === 'number' && Number.isInteger(v));
}

/** 转成 BigInt（布尔按 0/1）。 */
function asBig(v) {
  if (typeof v === 'bigint') return v;
  if (typeof v === 'boolean') return v ? 1n : 0n;
  return BigInt(v);
}

/**
 * 大整数 → number（下标、范围步长、重复次数这类「要个小整数」的地方用）。
 *
 * 超出安全范围时不走 `Number()`（那会**静默丢精度**给出一个看似合法的数），
 * 而是给一个「必然越界」的值，让调用方的边界检查照常报错——对齐
 * Python 那边 `a[2 ** 62]` 的 IndexError。
 */
function toIndexNum(v) {
  if (typeof v !== 'bigint') return v;
  const MAX = BigInt(Number.MAX_SAFE_INTEGER);
  if (v > MAX) return Number.MAX_SAFE_INTEGER + 1;
  if (v < -MAX) return -(Number.MAX_SAFE_INTEGER + 1);
  return Number(v);
}

/**
 * 整数运算的任意精度路径。
 *
 * 返回 `null` 表示「这次不该用 BigInt」（`**` 的指数是负数或非整数——
 * Python 那边给的是小数；除零留给调用方报错），调用方照旧走双精度。
 */
function bigIntArith(op, lv, rv) {
  const a = asBig(lv); const b = asBig(rv);
  let r;
  switch (op) {
    case '+': r = a + b; break;
    case '-': r = a - b; break;
    case '*': r = a * b; break;
    case '//':
      if (b === 0n) return null;
      r = a / b;
      // BigInt 的 `/` 是向零截断，Python 的 `//` 是向下取整
      if (a % b !== 0n && (a % b < 0n) !== (b < 0n)) r -= 1n;
      break;
    case '%': {
      if (b === 0n) return null;
      r = a % b;
      if (r !== 0n && (r < 0n) !== (b < 0n)) r += b;   // 符号跟除数（Python 语义）
      break;
    }
    case '**':
      if (b < 0n) return null;              // `2 ** -1` 在 Python 里是 0.5
      r = a ** b;
      break;
    default: return null;
  }
  // 结果回到安全范围就还原成 number：`(2 ** 62) // (2 ** 62)` 该给 `1` 而不是 `1`，
  // 更不能因为「中间碰巧走了一次大数」就让 `类型()` 变样。
  const MAX = BigInt(Number.MAX_SAFE_INTEGER);
  if (r >= -MAX && r <= MAX) return Number(r);
  return r;
}

function truthy(v) {
  if (v === null || v === undefined) return false;
  if (typeof v === 'bigint') return v !== 0n;
  if (isDecimal(v)) return v.coef !== 0n;    // 精确小数的「零」是假（M26）
  // 空容器为假——与 Python 的 bool([])/bool({})/bool(set()) 一致（M30）。
  // JS 的 Boolean([]) 是 true，不特判就会让 `如果 列表：` 判断反掉。
  if (Array.isArray(v)) return v.length > 0;
  if (v instanceof Map) return v.size > 0;
  if (v instanceof JishiSet) return v.size > 0;
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
  //
  // 但**加减乘的结果要按 28 位有效数字舍入**（M32）：Python 的 Decimal
  // 每次运算都走 context（prec=28, ROUND_HALF_EVEN），
  // `Decimal("9999999999999999999999999999") + 1` 给的是
  // `1.000000000000000000000000000E+28` 而不是 29 位的精确值。
  // 以前只给 div 加了舍入，于是「加减乘超 28 位」时三个宿主给不同的数
  // ——那属于「静默算错」，比报错更糟。
  // 注意：构造（`精确("…")`）**不**舍入，与 Python 一样（Decimal(str) 是精确构造）。
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
    return new JishiDecimal(a.c + b.c, e)._roundToSignificant(DEC_PRECISION);
  }

  sub(o) {
    const e = Math.min(this.exp, o.exp);
    const a = this._alignTo(e), b = o._alignTo(e);
    return new JishiDecimal(a.c - b.c, e)._roundToSignificant(DEC_PRECISION);
  }

  mul(o) {
    return new JishiDecimal(this.coef * o.coef, this.exp + o.exp)
      ._roundToSignificant(DEC_PRECISION);
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
    if (this.exp > targetExp) {
      // 系数要「补零」到自己更粗的标度上：Python 的 `quantize` 是**精确**
      // 调整到给定指数，不只是舍入（`Decimal("1234").quantize(Decimal("0.01"))`
      // 给 `1234.00`）。少了这一步，`舍入(2)` 会原地返回 `1234`（M32）。
      return new JishiDecimal(this.coef * 10n ** BigInt(this.exp - targetExp),
                              targetExp);
    }
    if (this.exp === targetExp) return new JishiDecimal(this.coef, this.exp);
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

  /**
   * 定点写法 —— 对应 Python 的 `format(v, "f")`：`1.2E+3` → `1200`。
   *
   * `舍入` 的最后一步要用它：Python 侧是
   * `Decimal(format(rounded, "f"))`，把科学计数法换回普通写法
   * （金额场景没人想看 `1.2E+3`）。
   */
  toFixedString() {
    const abs = this.coef < 0n ? -this.coef : this.coef;
    const sign = this.coef < 0n ? '-' : '';
    const digits = abs === 0n
      ? '0'.repeat(Math.max(1, -this.exp))
      : abs.toString();
    if (this.exp >= 0) return sign + digits + '0'.repeat(this.exp);
    const point = digits.length + this.exp;
    if (point > 0) {
      return sign + digits.slice(0, point) + '.' + digits.slice(point);
    }
    return sign + '0.' + '0'.repeat(-point) + digits;
  }

  /**
   * `str(Decimal)` 的写法（对齐 CPython 的 `decimal._pydecimal.__str__`）。
   *
   * 规则是：`leftdigits = exp + 系数位数`；**只有 `exp <= 0` 且
   * `leftdigits > -6` 时才用定点**，否则用科学计数法（点前留 1 位、
   * 指数带正负号、大写 `E`）。
   *
   * M32 才补上科学计数法分支——以前无条件补零，于是
   * `精确("9999999999999999999999999999") + 1` 打出 29 位数字串，
   * 而 Python 给 `1.000000000000000000000000000E+28`。
   */
  toString() {
    const neg = this.coef < 0n;
    const abs = neg ? -this.coef : this.coef;
    // 系数为 0 时按标度补足位数：Python 的 `_int` 对 `Decimal("0.00")` 是 '000'
    const digits = abs === 0n
      ? '0'.repeat(Math.max(1, -this.exp))
      : abs.toString();
    const leftdigits = this.exp + digits.length;
    let dotplace;
    if (this.exp <= 0 && leftdigits > -6) dotplace = leftdigits;
    else dotplace = 1;                          // 科学计数法：点前 1 位
    let intpart;
    let fracpart;
    if (dotplace <= 0) {
      intpart = '0';
      fracpart = '.' + '0'.repeat(-dotplace) + digits;
    } else if (dotplace >= digits.length) {
      intpart = digits + '0'.repeat(dotplace - digits.length);
      fracpart = '';
    } else {
      intpart = digits.slice(0, dotplace);
      fracpart = '.' + digits.slice(dotplace);
    }
    let expPart = '';
    if (leftdigits !== dotplace) {
      const e = leftdigits - dotplace;
      expPart = 'E' + (e >= 0 ? '+' : '') + e;
    }
    return (neg ? '-' : '') + intpart + fracpart + expPart;
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

/**
 * 从标准输入同步读一行（与 Python 的 `input()` 对齐，M30）。
 *
 * 逐字节读到换行、最后一次性按 UTF-8 解码——**不能逐字节 decode**，
 * 否则中文（多字节）会被切坏。管道与终端都能用；读到末尾给空串。
 */
function stdinLine() {
  const bytes = [];
  const buf = Buffer.alloc(1);
  let got = false;
  let eof = false;
  try {
    for (;;) {
      const n = fs.readSync(0, buf, 0, 1, null);
      if (n === 0) { eof = true; break; }
      got = true;
      if (buf[0] === 0x0a) break;              // \n
      if (buf[0] !== 0x0d) bytes.push(buf[0]); // 跳过 \r
    }
  } catch (e) {
    eof = true;                                 // EAGAIN / EOF：当作结束
  }
  // 一个字节都没读到就到末尾：与 Python 的 `input()` 一样报「输入结束」，
  // 而不是悄悄给个空串（那样程序会拿着空路径乱跑）
  if (eof && !got) {
    throw new JishiError('输入结束', '输入结束',
                         '输入已经结束，程序没有更多内容可读了');
  }
  return Buffer.from(bytes).toString('utf8');
}

// ---------------------------------------------------------------------------
// 集合（M30，JS 侧实现）
// ---------------------------------------------------------------------------
// 与 Python 侧 `JishiSet` 完全对齐：**按插入顺序**去重，打印结果稳定可复现
// （三执行器 / 跨宿主对拍才不会随机失败）。
//
// 为什么不用内建 `Set`：它的元素相等用的是 SameValueZero，与 Python 的
// `hash/eq` 语义不一致——Python 里 `{1, 1.0, True}` 只留一个元素
// （三者 hash 相同、两两相等），而 JS 的 `new Set([1, 1.0, true])` 会留三个
// （`1 === true` 是 false）。所以这里自己维护「规范化键 → 原值」的 Map。
//
// 另一条与 Python 一致：**列表 / 字典 / 集合 / 精确小数不能进集合**
// （Python 侧它们 `__hash__ = None`）。这条必须拦下来——静默塞进去会让
// `2 在 集合` 出现「有时查得到有时查不到」。

//: 对象身份池（WeakMap：不阻止回收，且 id 不会复用）
let _objIdSeq = 1;
const _objIds = new WeakMap();

function objId(o) {
  let id = _objIds.get(o);
  if (id === undefined) { id = _objIdSeq++; _objIds.set(o, id); }
  return id;
}

/** 数字的规范化文本（`-0` 与 `0` 视为同一个键）。 */
function numKey(x) {
  if (Object.is(x, -0)) return '0';
  return String(x);
}

/** 集合元素的「哈希键」——模拟 Python 的 hash/eq 语义。 */
function setKey(v) {
  if (v === null || v === undefined) return 'n';
  // Python 里 True == 1、False == 0，所以布尔按数字归键
  if (typeof v === 'boolean') return 'd:' + (v ? '1' : '0');
  if (typeof v === 'number') return 'd:' + numKey(v);
  // 大整数与同值的整数要归到同一个键（`String(1n)` 就是 `'1'`）
  if (typeof v === 'bigint') return 'd:' + v.toString();
  if (isFloatBox(v)) return 'd:' + numKey(v.value);
  if (typeof v === 'string') return 's:' + v;
  return 'o:' + objId(v);
}

/** 能不能放进集合（对齐 Python 侧「元素必须可哈希」）。 */
function hashable(v) {
  if (Array.isArray(v)) return false;
  if (v instanceof Map) return false;
  if (v instanceof JishiSet) return false;
  if (isDecimal(v)) return false;
  return true;
}

class JishiSet {
  constructor(items = null) {
    //: 规范化键 → 原值（Map 保持插入顺序）
    this.map = new Map();
    if (items) for (const it of items) this.add(it);
  }

  add(v) {
    if (!hashable(v)) {
      throw typeErr(`「${display(v)}」不能放进集合，集合的元素要是不可变的`
        + `（数字、文本…）；列表、字典和集合本身放不进去`);
    }
    const k = setKey(v);
    if (!this.map.has(k)) this.map.set(k, v);   // 保留首次出现的那个
    return null;
  }

  /** 删除元素；返回是否真的删掉了。 */
  delete(v) {
    if (!hashable(v)) return false;
    return this.map.delete(setKey(v));
  }

  has(v) {
    // Python 侧 `__contains__` 遇到不可哈希的元素也是吞掉 TypeError 给 False
    if (!hashable(v)) return false;
    return this.map.has(setKey(v));
  }

  get size() { return this.map.size; }

  /** 按插入顺序取出元素（快照）。 */
  values() { return [...this.map.values()]; }

  [Symbol.iterator]() { return this.map.values(); }
}

/** 集合 → 文本。`useRepr` 为真时元素带引号（打印路径），否则不带（错误消息里用）。 */
function setRepr(s, useRepr) {
  const items = s.values();
  if (items.length === 0) return '集合()';   // 空集合不写 {} ——那是空字典
  const f = useRepr ? pyRepr : display;
  return '{' + items.map(f).join(', ') + '}';
}

// ---------------------------------------------------------------------------
// 切片（M18 / M30）
// ---------------------------------------------------------------------------
// 编译器把 `列表[起:止:步长]` 编译成 BUILD_SLICE（按位标志：1=有起、2=有止、
// 4=有步长）→ 一个 slice 对象 → 由 GET_ITEM / SET_ITEM 消费。
// 语义对齐 Python 的 `slice.indices()`：负索引、负步长、越界裁剪都一样。

const SLICE = Symbol('slice');

function makeSlice(start, stop, step) {
  if (typeof step === 'number' && step === 0) {
    throw valueErr('切片的步长不能为 0');
  }
  return { [SLICE]: true, start, stop, step };
}

function isSlice(v) {
  return v !== null && typeof v === 'object' && v[SLICE] === true;
}

/** 把切片分量规范成整数（省略 → null）。 */
function sliceInt(v, what) {
  if (v === null || v === undefined) return null;
  if (typeof v === 'number' && Number.isInteger(v)) return v;
  throw typeErr(`切片的${what}要是整数，得到了「${typeName(v)}」`);
}

/**
 * 把切片换算成实际下标 `[start, stop, step]`——对齐 CPython 的
 * `PySlice_AdjustIndices`（负值先加长度、再按方向裁到边界）。
 */
function sliceIndices(sl, len) {
  const step = sl.step === null || sl.step === undefined ? 1 : sliceInt(sl.step, '步长');
  if (step === 0) throw valueErr('切片的步长不能为 0');
  const lower = step > 0 ? 0 : -1;
  const upper = step > 0 ? len : len - 1;

  let start = sliceInt(sl.start, '起点');
  if (start === null) {
    start = step > 0 ? lower : upper;
  } else if (start < 0) {
    start += len;
    if (start < lower) start = lower;
  } else if (start > upper) {
    start = upper;
  }

  let stop = sliceInt(sl.stop, '终点');
  if (stop === null) {
    stop = step > 0 ? upper : lower;
  } else if (stop < 0) {
    stop += len;
    if (stop < lower) stop = lower;
  } else if (stop > upper) {
    stop = upper;
  }
  return [start, stop, step];
}

/** 取出切片覆盖的下标序列（用于步长 ≠ 1 的赋值，需要逐个对位）。 */
function slicePositions(start, stop, step) {
  const out = [];
  for (let i = start; step > 0 ? i < stop : i > stop; i += step) out.push(i);
  return out;
}

/** 切片的显示：正常情况下不会露到用户面前，但打印出来不该是 [object Object]。 */
function sliceRepr(sl) {
  const f = (x) => (x === null || x === undefined ? '空' : String(x));
  return `切片(${f(sl.start)}, ${f(sl.stop)}, ${f(sl.step)})`;
}

/**
 * 把一个值当序列展开（用于 `长度`/`最大`/`最小`/`总和` 这类「按序列处理」的内建）。
 *
 * `text` 为假时不把文本当序列——`最大("abc")` 在 Python 侧是「那一个文本」，
 * 不是三个字符（对齐 `_single_iterable_arg`）。
 * 不是可迭代则返回 null。
 */
function seqOf(v, text, line, col) {
  if (Array.isArray(v)) return [...v];
  if (v instanceof JishiSet) return v.values();
  if (v instanceof Map) return [...v.keys()];
  if (text && typeof v === 'string') return [...v];
  if (v instanceof JishiInstance) {
    // 自定义对象定义了「迭代」就能按序列处理（M24/M30）
    const fn = v.cls.methods.get('迭代');
    if (fn) return seqOf(fn.vm.callFunction(fn, [v], null, line, col), text, line, col);
  }
  return null;
}

/**
 * 当成可迭代对象取元素（对齐 Python 侧 `_as_iterable`）；不行就报中文错。
 * 文本在这里**算**可迭代（与 `seqOf(…, false)` 相反）——`集合("甲乙")` 是
 * 两个字符，与 Python 一致。
 */
function asIterable(v, what, line, col) {
  const got = seqOf(v, true, line, col);
  if (got === null) {
    throw typeErr(`「${typeName(v)}」不能当${what}用，需要列表、文本、集合或字典`, line, col);
  }
  return got;
}

/**
 * 最值（`最大` / `最小`）：单参数是序列就按元素求，多参数就按参数求
 * ——与 Python 侧 `_b_max` / `_single_iterable_arg` 同一套规则。
 * 文本刻意当**标量**（`最大("abc")` 是那一个文本，不是三个字符）。
 */
function extremum(args, wantMax) {
  const seq = args.length === 1 ? seqOf(args[0], false, undefined, undefined) : null;
  const items = seq !== null ? seq : args;
  const who = wantMax ? '最大' : '最小';
  if (items.length === 0) {
    throw typeErr(`「${who}」需要一个非空列表或若干数字`);
  }
  let best = items[0];
  for (let i = 1; i < items.length; i++) {
    const x = items[i];
    if (!comparable(x, best)) {
      throw typeErr(`「${who}」里混了不能互相比较的值——数字和文本不能比大小`);
    }
    const c = cmpVals(x, best);
    if (wantMax ? c > 0 : c < 0) best = x;
  }
  return best;
}

/** 值相等（`==`）——容器按值递归，数字与布尔之间按数值（`1 == 真` 为真）。 */function valueEq(a, b) {
  if (isDecimal(a) || isDecimal(b)) {
    try {
      return compareDecimals(toDecimal(a), toDecimal(b)) === 0;
    } catch (e) {
      if (e instanceof JishiError) return false;
      throw e;
    }
  }
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) if (!valueEq(a[i], b[i])) return false;
    return true;
  }
  if (a instanceof Map && b instanceof Map) {
    if (a.size !== b.size) return false;
    for (const [k, v] of a) {
      let hit = false;
      for (const [k2, v2] of b) {
        if (valueEq(k, k2)) { if (!valueEq(v, v2)) return false; hit = true; break; }
      }
      if (!hit) return false;
    }
    return true;
  }
  if (a instanceof JishiSet && b instanceof JishiSet) return setEq(a, b);
  const av = unwrap(a), bv = unwrap(b);
  // 数字与布尔在 Python 里可互相比较（True == 1）
  const an = typeof av === 'boolean' ? (av ? 1 : 0) : av;
  const bn = typeof bv === 'boolean' ? (bv ? 1 : 0) : bv;
  const anNum = typeof an === 'number' || typeof an === 'bigint';
  const bnNum = typeof bn === 'number' || typeof bn === 'bigint';
  // 用 `==` 而不是 `===`：BigInt 与 number 的宽松相等在 JS 里是**精确**
  // 比较（`1n == 1` 为真、`9007199254740993n == 9007199254740993` 也为真），
  // 正好对上 Python 的 `int == float` 语义
  if (anNum && bnNum) return an == bn;      // eslint-disable-line eqeqeq
  return av === bv;
}

/** 集合相等：与顺序无关（对齐 Python 侧 `JishiSet.__eq__`）。 */
function setEq(a, b) {
  if (a.map.size !== b.map.size) return false;
  for (const k of a.map.keys()) if (!b.map.has(k)) return false;
  return true;
}

/** 成员测试（`item 在 container`）——对齐 Python 侧 `runtime.contains`。 */
function contains(container, item, line, col) {
  if (Array.isArray(container)) return container.some((x) => valueEq(x, item));
  if (container instanceof Map) return [...container.keys()].some((k) => valueEq(k, item));
  if (container instanceof JishiSet) return container.has(item);
  if (typeof container === 'string') {
    // Python 的 `1 in "abc"` 是 TypeError（不是把 1 转成文本），这里保持一致
    if (typeof item !== 'string') {
      throw typeErr(`「${display(item)}」不能在「文本」里找`, line, col);
    }
    return container.includes(item);
  }
  throw typeErr(
    `「${display(item)}」不能在「${typeName(container)}」里找；`
    + `「在」只能用于列表、字典（按「键」找）、文本（找子串）和集合`, line, col);
}

/** 「是/不是」的同一性指纹（对齐 Python 侧 `_identity_snapshot`）。 */
function identityKey(v) {
  if (v === null || v === undefined) return 'n';
  if (typeof v === 'boolean') return 'b:' + (v ? '1' : '0');
  if (typeof v === 'number') return 'v:' + typeName(v) + ':' + numKey(v);
  if (typeof v === 'bigint') return 'v:整数:' + v.toString();   // 大整数按值（M32）
  if (isFloatBox(v)) return 'v:小数:' + numKey(v.value);
  if (typeof v === 'string') return 'v:文本:' + v;
  // 其余（列表/字典/集合/实例/函数/精确小数…）按对象身份
  return 'i:' + objId(v);
}

/**
 * 从内建/方法里**同步**调用一个基石可调用对象。
 *
 * 「数据流水线」（映射/过滤/排序按/归约）靠它才能存在——在这之前内建
 * 没有任何办法回过头去跑用户函数。JS 侧比 Rust 侧省一层：`VmFunction`
 * 自带 `.vm`，不必把 VM 传进方法表。
 */
function callValue(f, args, line, col) {
  if (f instanceof VmFunction) return f.vm.callFunction(f, args, null, line, col);
  if (f instanceof Builtin) return f.fn(...args);
  if (f instanceof BoundUserMethod) return f.fn.vm.callFunction(f.fn, [f.instance, ...args], null, line, col);
  if (f instanceof BoundMethod) return f.impl(f.obj, args, line, col);
  if (f instanceof JishiClass) return makeInstance(f, args, line, col);
  throw typeErr(`「${display(f)}」不能当函数用`, line, col);
}

/** 检查一个参数确实可调用（报错要说清是哪个方法的参数）。 */
function ensureCallable(f, name, line, col) {
  if (f instanceof VmFunction || f instanceof Builtin || f instanceof BoundUserMethod
      || f instanceof BoundMethod || f instanceof JishiClass) {
    return f;
  }
  throw typeErr(`「${name}」需要一个函数作为参数，但给的是「${typeName(f)}」`, line, col);
}

/** 值的「可比族」：数字族含布尔与精确小数（Python 里它们可同台比较）。 */
function valKind(v) {
  if (isDecimal(v)) return 'num';
  const x = unwrap(v);
  if (typeof x === 'number' || typeof x === 'boolean') return 'num';
  if (typeof x === 'string') return 'str';
  return 'other';
}

function comparable(a, b) {
  const ka = valKind(a), kb = valKind(b);
  return ka === kb && (ka === 'num' || ka === 'str');
}

function cmpVals(a, b) {
  if (isDecimal(a) || isDecimal(b)) return compareDecimals(toDecimal(a), toDecimal(b));
  const x = unwrap(a), y = unwrap(b);
  const xn = typeof x === 'boolean' ? (x ? 1 : 0) : x;
  const yn = typeof y === 'boolean' ? (y ? 1 : 0) : y;
  return xn < yn ? -1 : xn > yn ? 1 : 0;
}

function display(v) {
  if (v === null || v === undefined) return '空';
  if (v === true) return '真';
  if (v === false) return '假';
  if (typeof v === 'string') return v;
  if (v instanceof Map) return dictRepr(v);
  if (v instanceof JishiSet) return setRepr(v, false);
  if (isSlice(v)) return sliceRepr(v);
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
  if (v instanceof JishiSet) return setRepr(v, true);
  if (isSlice(v)) return sliceRepr(v);
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
  if (v instanceof JishiSet) return setRepr(v, true);
  if (isSlice(v)) return sliceRepr(v);
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

  // 与 Python 侧的包装器 `__repr__` 对齐（M32）：以前这里没有 toString，
  // 于是 `打印(打印)` 会走 `String(内建)` 给 `[object Object]`。
  toString() { return `<内建 ${this.name}>`; }
}

//: 类型标注里认识的类型名 → 判定函数（对齐 Python 侧 `_TYPE_PREDICATES`）。
//: **只检查认识的名字**：不认识的一律跳过（例如作者自定义的 `商品`、`订单`），
//: 这样标注既能当文档用，又不会因为名字不在表里就误报。
const TYPE_PREDICATES = {
  '整数': (v) => typeof v === 'number' && Number.isInteger(v),
  '小数': (v) => isFloatBox(v) || (typeof v === 'number' && !Number.isInteger(v)),
  '文本': (v) => typeof v === 'string',
  '列表': (v) => Array.isArray(v),
  '字典': (v) => v instanceof Map,
  '集合': (v) => v instanceof JishiSet,
  '布尔': (v) => typeof v === 'boolean',
  '空': (v) => v === null || v === undefined,
  '函数': (v) => v instanceof VmFunction || v instanceof Builtin
    || v instanceof BoundMethod || v instanceof BoundUserMethod
    || v instanceof JishiClass || v instanceof ExcType,
  '任意': () => true,
  '值': () => true,
};

/**
 * 按类型标注校验实参（M24.3）。参数由编译器发射的前导字节码提供：
 *   desc      扁平描述 `[参数名, 类型名, 参数名, 类型名, …]`
 *   values    实参值列表（与 desc 里的参数一一对应）
 *   funcName  函数名（只为拼出好读的报错）
 */
function checkArgs(desc, values, funcName) {
  for (let i = 0; i + 1 < desc.length; i += 2) {
    const pname = desc[i], tname = desc[i + 1];
    const pred = TYPE_PREDICATES[tname];
    if (!pred) continue;                    // 不认识的标注：只作文档
    const idx = i / 2;
    if (idx >= values.length) continue;
    const value = values[idx];
    if (!pred(value)) {
      throw typeErr(`函数「${funcName}」的参数「${pname}」要求是「${tname}」，`
        + `但传进来的是「${typeName(value)}」`);
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// 异常类型（`值错误("…")` 的构造器 + `捕获 值错误` 的匹配）
// ---------------------------------------------------------------------------
class ExcType {
  constructor(name) { this.name = name; }

  toString() { return `<异常类型 ${this.name}>`; }
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
      // 用 fs.writeSync 而不是 process.stdout.write：后者对管道是**异步**的，
      // 与「输入()」的同步读 stdin 混用时，进程退出会丢掉没 flush 的字符
      // （实测开头两个汉字被吞）——同步写没有这个问题（M30）
      fs.writeSync(1, args.map(pyStr).join(' ') + '\n');
      return null;
    }),
    '输入': new Builtin('输入', (prompt = '') => {
      // 提示语写到 stdout（不换行），与 Python 侧的 input(prompt) 一致（M30）
      if (prompt !== '' && prompt !== null && prompt !== undefined) {
        fs.writeSync(1, pyStr(prompt));
      }
      return stdinLine();
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
    '集合': new Builtin('集合', (...args) => {
      if (args.length > 1) {
        throw typeErr(`「集合」最多接受 1 个参数，但传了 ${args.length} 个`,
          undefined, undefined);
      }
      if (args.length === 0) return new JishiSet();
      return new JishiSet(asIterable(args[0], '集合的初始内容', undefined, undefined));
    }),
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
      if (v instanceof JishiSet) return v.size;
      // 定义了「迭代」的自定义对象也能求长度（M24）：既然能遍历，
      // 数一数有几个元素是自然的事，不必再要求作者额外写一个方法
      const seq = seqOf(v, false, undefined, undefined);
      if (seq !== null) return seq.length;
      throw typeErr(`「${display(v)}」没有长度`);
    }),
    '范围': new Builtin('范围', (...args) => {
      if (args.length === 1) return range(0, args[0], 1);
      if (args.length === 2) return range(args[0], args[1], 1);
      if (args.length === 3) return range(args[0], args[1], args[2]);
      throw typeErr('「范围」需要 1 到 3 个整数参数');
    }),
    '最大': new Builtin('最大', (...args) => extremum(args, true)),
    '最小': new Builtin('最小', (...args) => extremum(args, false)),
    '总和': new Builtin('总和', (v) => {
      const seq = seqOf(v, false, undefined, undefined);
      if (seq === null) throw typeErr(`「${display(v)}」不能求和，需要一个全是数字的列表`);
      // 精确小数要走十进制起点（与 Python 侧一致：sum 从整数 0 起算会出错）
      if (seq.some(isDecimal)) {
        let acc = new JishiDecimal(0n, 0);
        for (const x of seq) acc = acc.add(toDecimal(x));
        return acc;
      }
      // 大整数（M32）：整段走 BigInt 累加，避免双精度在第二个元素起就失真
      // （`总和([2 ** 62, 2 ** 62])` 该给 18446744073709551616）。
      // 混了小数就退回双精度——Python 那边 `sum([2**62, 0.5])` 也是 float。
      if (!seq.some(isFloatBox) && seq.some((x) => typeof unwrap(x) === 'bigint')) {
        let acc = 0n;
        for (const x of seq) acc += asBig(unwrap(x));
        const MAX = BigInt(Number.MAX_SAFE_INTEGER);
        return (acc >= -MAX && acc <= MAX) ? Number(acc) : acc;
      }
      const s = seq.reduce((a, b) => a + Number(unwrap(b)), 0);
      // 含小数则返回 FloatBox
      return seq.some(isFloatBox) ? new FloatBox(s) : s;
    }),
    '类型': new Builtin('类型', (v) => typeName(v)),
    // 类型标注的校验（M24.3）：编译器把校验发射成函数自己的前导字节码，
    // 调用这个内建。所以这里实现它，JS 侧就自动获得了类型标注支持（M30）。
    '检查实参': new Builtin('检查实参', (desc, values, funcName) =>
      checkArgs(desc, values, funcName)),
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
  // 大整数参数收敛成 number（M32）：`范围(2 ** 62)` 这种写法在 Python 那边
  // 也会因为内存而失败，这里按「大到必然超出实际需要」处理，不会静默算错
  a = toIndexNum(a); b = toIndexNum(b); step = toIndexNum(step);
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

  toString() { return `<模块 ${this.name}>`; }
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
    // 类变量（M34 补上 M23.3）：与 Python 侧一致——继承时先合并基类的，
    // 子类的同名规则由「先合基类、再合自己」自然覆盖。
    //
    // 为什么现在才补：以前只差这一块，但没被真实需求逼到；M34 的「枚举」
    // 脱糖成「类 + 类体外给类设属性」，一写枚举就撞上了。
    const vars = new Map();
    if (base) for (const [k, v] of base.classVars) vars.set(k, v);
    this.classVars = vars;
  }

  toString() { return `<类 ${this.name}>`; }
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

  toString() { return `<函数 ${this.name}>`; }
}

// 闭包单元
class Cell {
  constructor(value = UNSET) { this.value = value; }
}

// ---------------------------------------------------------------------------
// 运算
// ---------------------------------------------------------------------------
/// 序列重复（列表 / 文本 × 整数）：造新值，不动原值（与 Python 一致）。
function repeatSeq(seq, n) {
  const times = Math.max(0, n);
  if (typeof seq === 'string') return seq.repeat(times);
  const out = [];
  for (let i = 0; i < times; i++) out.push(...seq);
  return out;
}

/** 序列的乘数必须是整数（FloatBox 说明它是运算得到的小数，Python 会报错）。 */
function intMulOf(v, line, col) {
  if (isFloatBox(v) || !isIntLike(v)) {
    throw typeErr(`序列只能乘整数，不能乘「${typeName(v)}」`, line, col);
  }
  return toIndexNum(v);      // 大整数乘数由 toIndexNum 收敛（M32）
}

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

  // 先把 FloatBox 拆开，再按 Python 的类型规则分派。**不能直接用 JS 的
  // 运算符兜底**：`[1] + [2]` 在 JS 里会变成字符串 "12"、`1 + 空` 会当 0、
  // `-7 % 3` 符号也和 Python 相反——全是「不报错但结果错」（M30 修）。
  const fl = isFloatBox(l), fr = isFloatBox(r);
  const lv = unwrap(l), rv = unwrap(r);
  const lList = Array.isArray(l), rList = Array.isArray(r);
  const lStr = typeof lv === 'string', rStr = typeof rv === 'string';
  const lSet = l instanceof JishiSet, rSet = r instanceof JishiSet;

  // -- 列表：+ 拼接、* 重复 --
  if (lList || rList) {
    if (op === '+' && lList && rList) return [...l, ...r];
    if (op === '*' && lList) return repeatSeq(l, intMulOf(rv, line, col));
    if (op === '*' && rList) return repeatSeq(r, intMulOf(lv, line, col));
    if (op === '+') {
      const other = lList ? r : l;
      throw typeErr(`「列表」只能和「列表」相加，不能和「${typeName(other)}」相加`, line, col);
    }
    throw typeErr(`「${typeName(l)}」和「${typeName(r)}」不能做「${op}」运算`, line, col);
  }
  // -- 文本：+ 拼接、* 重复 --
  if (lStr || rStr) {
    if (op === '+' && lStr && rStr) return lv + rv;
    if (op === '*' && lStr) return repeatSeq(lv, intMulOf(rv, line, col));
    if (op === '*' && rStr) return repeatSeq(rv, intMulOf(lv, line, col));
    if (op === '+') {
      const other = lStr ? r : l;
      throw typeErr(`「文本」只能和「文本」相加，不能和「${typeName(other)}」相加`, line, col);
    }
    throw typeErr(`「${typeName(l)}」和「${typeName(r)}」不能做「${op}」运算`, line, col);
  }
  // -- 集合：Python 侧没定义算术运算，直接报错（不要静默给出别扭结果）--
  if (lSet || rSet) {
    throw typeErr(`「${typeName(l)}」和「${typeName(r)}」不能做「${op}」运算`, line, col);
  }
  // -- 数字（布尔在 Python 里也算数字：`真 + 1` 是 2）--
  const numOk = (x) => typeof x === 'number' || typeof x === 'boolean'
    || typeof x === 'bigint';
  if (!numOk(lv) || !numOk(rv)) {
    throw typeErr(`「${typeName(l)}」和「${typeName(r)}」不能做「${op}」运算`, line, col);
  }

  let result;
  let isFloat = fl || fr;   // 参与运算的是小数 → Python 结果一般是 float
  const intPair = isIntLike(lv) && isIntLike(rv);
  // 任一边已经是大整数就必须走 BigInt：JS 不允许 BigInt 与 number 混做
  // `/` 和 `**`（会直接抛 TypeError），而 `+`/`*` 混算虽合法，
  // 结果也不该再退回双精度（M32）
  if (intPair && (typeof lv === 'bigint' || typeof rv === 'bigint')) {
    const r = bigIntArith(op, lv, rv);
    if (r !== null) return r;
  }
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
    case '%': {
      if (rv === 0) throw zeroDiv(line, col);
      // Python 的 `%` 结果符号跟**除数**一致（JS 的是截断语义）：
      // `-7 % 3` 在 Python 是 2，在 JS 是 -1——不修就是静默算错
      let m = lv % rv;
      if (m !== 0 && (m < 0) !== (rv < 0)) m += rv;
      result = m;
      break;
    }
    case '**': {
      const p = lv ** rv;
      result = p;
      // `2 ** -1` 是 0.5、`2 ** 2` 是 4 —— 按结果是否为整数决定要不要装箱
      isFloat = isFloat || !Number.isInteger(p);
      break;
    }
    default: throw runErr(`不支持的运算符「${op}」`, line, col);
  }
  // 双精度的整数结果一旦超出安全范围就不再精确（`2 ** 64` 会变成
  // 18446744073709552000），这时用 BigInt 重算一遍（M32）。
  // 放在这里而不是 switch 前面，是为了让普通小整数走原本的快路径。
  if (intPair && !isFloat && typeof result === 'number'
      && !Number.isSafeInteger(result)) {
    const r = bigIntArith(op, lv, rv);
    if (r !== null) return r;
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
  // （在·不在·是·不是）不走这里——`精确("1") 在 [1]` 该按元素比较，
  // 而不是让「精确」拒绝参加（与 Python 侧 apply_compare 的分工一致）
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
  // 成员测试（M30）：列表按值、字典按「键」、文本找子串、集合按元素
  if (op === '在') return contains(r, l, line, col);
  if (op === '不在') return !contains(r, l, line, col);
  // 同一性（M30）
  if (op === '是') return identityKey(l) === identityKey(r);
  if (op === '不是') return identityKey(l) !== identityKey(r);
  const lv = unwrap(l), rv = unwrap(r);
  // 大整数与 number 的大小比较在 JS 里是**精确**的（`2n ** 62n > 1` 不会因
  // 双精度而失真），直接用就行；`==`/`!=` 走 valueEq（M32）
  switch (op) {
    // `==` 走值相等：列表/字典/集合按内容递归比，数字与布尔按数值比
    // （`[1, 2] == [1, 2]` 为真、`1 == 真` 为真，与 Python 一致）
    case '==': return valueEq(l, r);
    case '!=': return !valueEq(l, r);
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
  if (obj instanceof JishiSet) return SET_METHODS;
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
    // 查找顺序与 Python 一致：实例字段 → 类变量（含基类）→ 方法（绑定实例）
    if (obj.fields.has(attr)) return obj.fields.get(attr);
    if (obj.cls.classVars.has(attr)) return obj.cls.classVars.get(attr);
    if (obj.cls.methods.has(attr)) {
      return new BoundUserMethod(obj.cls.methods.get(attr), obj, attr, line, col);
    }
    throw typeErr(`「${typeName(obj)}」没有属性「${attr}」`, line, col);
  }
  if (obj instanceof JishiClass) {
    if (obj.classVars.has(attr)) return obj.classVars.get(attr);   // 类变量（M23.3）
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
  if (obj instanceof JishiClass) {
    // 类变量（M23.3）：`类名.计数 = 5`。写在类自己的类变量表里。
    // 编译器的「类体里写 x = 1」也走这里（发射的是 DUP_TOP + SET_ATTR）。
    obj.classVars.set(attr, value);
    return;
  }
  throw typeErr(`不能给「${display(obj)}」的属性「${attr}」赋值`, line, col);
}

function getItem(obj, index, line, col) {
  // 大整数下标（`a[2 ** 62]`）先收敛成 number，越界由下面的检查照常报错（M32）
  if (typeof index === 'bigint') index = toIndexNum(index);
  if (isSlice(index)) {
    // 切片（M30）：列表按元素切、文本按字符切（与 Python 的 str 索引一致）
    if (Array.isArray(obj)) {
      const [s, e, st] = sliceIndices(index, obj.length);
      return slicePositions(s, e, st).map((i) => obj[i]);
    }
    if (typeof obj === 'string') {
      const chars = [...obj];                 // 按 code point，代理对不会切坏
      const [s, e, st] = sliceIndices(index, chars.length);
      return slicePositions(s, e, st).map((i) => chars[i]).join('');
    }
    throw typeErr(`「${typeName(obj)}」不能切片，切片需要列表或文本`, line, col);
  }
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
  if (typeof index === 'bigint') index = toIndexNum(index);   // M32
  if (isSlice(index)) {
    // 切片赋值（M24/M30）：步长为 1 时可以变长（替换整个区间），
    // 步长不为 1 时长度必须精确匹配——与 Python 一致。
    if (!Array.isArray(obj)) {
      throw typeErr(`「${typeName(obj)}」不能按下标赋值`, line, col);
    }
    const vals = asIterable(value, '切片赋值的值', line, col);
    const [s, e, st] = sliceIndices(index, obj.length);
    if (st === 1) {
      obj.splice(s, Math.max(0, e - s), ...vals);
      return;
    }
    const idxs = slicePositions(s, e, st);
    if (idxs.length !== vals.length) {
      throw valueErr(`切片赋值长度不匹配：目标有 ${idxs.length} 个位置，`
        + `却给了 ${vals.length} 个值`, line, col);
    }
    idxs.forEach((i, k) => { obj[i] = vals[k]; });
    return;
  }
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

  toString() { return `<方法 ${this.name}>`; }

  // `类型()` 用它（见 runtime 的 typeName 钩子）——不然会给 `BoundMethod`
  get __jishiType() { return '内建方法'; }
}

// 绑定到实例的用户方法（自身 首参）
class BoundUserMethod {
  constructor(fn, instance, name, line, col) { this.fn = fn; this.instance = instance; this.name = name; this.line = line; this.col = col; }

  toString() { return `<方法 ${this.name}>`; }

  get __jishiType() { return '方法'; }
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
  // 用 seqOf 而不是逐类型判断：列表/文本/字典（键）/集合/自定义可迭代
  // 都能解包，与 Python 侧 `_as_iterable` 取同一套口径（M30）
  const seq = seqOf(value, true, line, col);
  if (seq === null) {
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
    // 用 valueEq（不是 ===）：Python 的 `list.remove` 用 `==`，
    // 所以 `[1].移除(真)` 能删掉 1，这里要保持一致
    const i = obj.findIndex((x) => valueEq(x, args[0]));
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
    const i = obj.findIndex((x) => valueEq(x, args[0]));
    if (i < 0) throw valueErr(`列表里没有「${display(args[0])}」，找不到它的位置`, line, col);
    return i;
  },
  '计数': (obj, args, line, col) => { needArgs('计数', args, 1, 1, line, col); return obj.filter((x) => valueEq(x, args[0])).length; },
  '包含': (obj, args, line, col) => { needArgs('包含', args, 1, 1, line, col); return obj.some((x) => valueEq(x, args[0])); },

  // -- 数据流水线（M30）--
  // 内建要能反过来**同步调用用户函数**，靠 callValue。JS 数组的
  // map/filter 天然返回新数组，不会动原列表（与 Python 侧一致）。
  '映射': (obj, args, line, col) => {
    needArgs('映射', args, 1, 1, line, col);
    ensureCallable(args[0], '映射', line, col);
    return obj.map((x) => callValue(args[0], [x], line, col));
  },
  '过滤': (obj, args, line, col) => {
    needArgs('过滤', args, 1, 1, line, col);
    ensureCallable(args[0], '过滤', line, col);
    return obj.filter((x) => truthy(callValue(args[0], [x], line, col)));
  },
  '排序按': (obj, args, line, col) => {
    needArgs('排序按', args, 1, 1, line, col);
    ensureCallable(args[0], '排序按', line, col);
    const keyed = obj.map((x) => [callValue(args[0], [x], line, col), x]);
    if (keyed.length > 0) {
      // 键必须能两两比较：Python 侧混着数字和文本会 TypeError，
      // 这里也要报中文错，不能用「相等」糊过去（那会悄悄给出错顺序）
      const first = keyed[0][0];
      for (const [k] of keyed) {
        if (!comparable(k, first)) {
          throw typeErr('「排序按」算出来的排序键没法互相比较'
            + '——排序键要都是数字，或都是文本，不能混着来', line, col);
        }
      }
    }
    keyed.sort((a, b) => cmpVals(a[0], b[0]));
    return keyed.map((p) => p[1]);
  },
  '归约': (obj, args, line, col) => {
    needArgs('归约', args, 2, 2, line, col);
    ensureCallable(args[0], '归约', line, col);
    let acc = args[1];
    for (const x of obj) acc = callValue(args[0], [acc, x], line, col);
    return acc;
  },
};

// 集合方法（M30）：与 Python 侧 SET_METHODS 同名同语义。
// 改内容的（添加/移除/丢弃/清空）直接落在共享的 Map 上，原变量能看见；
// 返回集合的（并集/交集/差集/对称差/复制）都造**新**集合。
const SET_METHODS = {
  '添加': (obj, args, line, col) => {
    needArgs('添加', args, 1, 1, line, col);
    try {
      obj.add(args[0]);
    } catch (e) {
      if (e instanceof JishiError) throw typeErr(e.message, line, col);
      throw e;
    }
    return null;
  },
  '移除': (obj, args, line, col) => {
    needArgs('移除', args, 1, 1, line, col);
    if (!obj.has(args[0])) {
      throw valueErr(`集合里没有「${display(args[0])}」，没法移除`, line, col);
    }
    obj.delete(args[0]); return null;
  },
  '丢弃': (obj, args, line, col) => {
    needArgs('丢弃', args, 1, 1, line, col);
    obj.delete(args[0]); return null;
  },
  '包含': (obj, args, line, col) => {
    needArgs('包含', args, 1, 1, line, col);
    return obj.has(args[0]);
  },
  '并集': (obj, args, line, col) => {
    needArgs('并集', args, 1, 1, line, col);
    return new JishiSet([...obj.values(),
      ...asIterable(args[0], '「并集」的另一个集合', line, col)]);
  },
  '交集': (obj, args, line, col) => {
    needArgs('交集', args, 1, 1, line, col);
    const other = asIterable(args[0], '「交集」的另一个集合', line, col);
    return new JishiSet(obj.values().filter(
      (x) => other.some((y) => valueEq(y, x))));
  },
  '差集': (obj, args, line, col) => {
    needArgs('差集', args, 1, 1, line, col);
    const other = asIterable(args[0], '「差集」的另一个集合', line, col);
    return new JishiSet(obj.values().filter(
      (x) => !other.some((y) => valueEq(y, x))));
  },
  '对称差': (obj, args, line, col) => {
    needArgs('对称差', args, 1, 1, line, col);
    const other = asIterable(args[0], '「对称差」的另一个集合', line, col);
    const mine = obj.values();
    const theirs = [...new JishiSet(other).values()];   // 先去重，顺序取首次出现
    return new JishiSet([
      ...mine.filter((x) => !theirs.some((y) => valueEq(y, x))),
      ...theirs.filter((x) => !mine.some((y) => valueEq(y, x))),
    ]);
  },
  '清空': (obj, args, line, col) => {
    needArgs('清空', args, 0, 0, line, col);
    obj.map.clear(); return null;
  },
  '转列表': (obj, args, line, col) => {
    needArgs('转列表', args, 0, 0, line, col);
    return obj.values();
  },
  '复制': (obj, args, line, col) => {
    needArgs('复制', args, 0, 0, line, col);
    return new JishiSet(obj.values());
  },
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
    const r = obj.quantize10(-digits, 'half-up');
    // 再按定点写法重新构造一遍：Python 侧最后一步是
    // `Decimal(format(rounded, "f"))`，舍到十位/百位时会得到 `1.2E+3`，
    // 那在金额场景里没人想看——换回 `1200`（M32）
    return JishiDecimal.fromString(r.toFixedString());
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
  JishiSet, setRepr, asIterable, seqOf,
  makeSlice, isSlice,
  JishiFile, isFile, enterContext, exitContext,
  Builtin, ExcType, EXCEPTION_TYPES, newBuiltins,
  Module, JishiClass, JishiInstance, VmFunction, Cell,
  applyBinop, applyUnary, applyCompare,
  contains, identityKey, valueEq, callValue, ensureCallable,
  methodTable, getAttr, setAttr, getItem, setItem,
  BoundMethod, BoundUserMethod, makeInstance,
  unpackValues, buildDict, lookupName,
  isLoopSignal, exceptionMatches, makeException,
  nameErr, typeErr, valueErr, runErr, fileErr,
};
