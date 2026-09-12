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

// 打印用的 str()：字符串不带引号，列表/字典用 Python repr 风格（单引号）
function pyStr(v) {
  if (v === null || v === undefined) return 'None';
  if (v === true) return 'True';
  if (v === false) return 'False';
  if (typeof v === 'string') return v;
  // 列表/字典内部元素用 repr（字符串带单引号，与 Python 一致）
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
  if (v === true) return 'True';
  if (v === false) return 'False';
  if (v === null || v === undefined) return 'None';
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
  for (const [k, val] of m) parts.push(display(k) + '：' + display(val));
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
      return Math.max(...v);
    }),
    '最小': new Builtin('最小', (v) => {
      if (!Array.isArray(v) || v.length === 0) throw typeErr(`「${display(v)}」不能求最小值`);
      return Math.min(...v);
    }),
    '总和': new Builtin('总和', (v) => {
      if (!Array.isArray(v)) throw typeErr(`「${display(v)}」不能求和`);
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
  if (op === '-') return -v;
  if (op === '非') return !truthy(v);
  throw runErr(`不支持的一元运算符「${op}」`, line, col);
}

function applyCompare(op, l, r, line, col) {
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
function unpackValues(value, n, line, col) {
  if (!Array.isArray(value)) {
    throw typeErr(`解包赋值需要列表，得到了「${typeName(value)}」`, line, col);
  }
  if (value.length !== n) {
    throw valueErr(`解包需要 ${n} 个值，实际有 ${value.length} 个`, line, col);
  }
  return [...value];
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
  Builtin, ExcType, EXCEPTION_TYPES, newBuiltins,
  Module, JishiClass, JishiInstance, VmFunction, Cell,
  applyBinop, applyUnary, applyCompare,
  methodTable, getAttr, setAttr, getItem, setItem,
  BoundMethod, BoundUserMethod, makeInstance,
  unpackValues, buildDict, lookupName,
  isLoopSignal, exceptionMatches, makeException,
  nameErr, typeErr, valueErr, runErr,
};
