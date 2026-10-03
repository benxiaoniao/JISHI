// -*- coding: utf-8 -*-
// 基石 JS 引擎入口（M13.2）。
//
// 用法：
//   node node/index.js 字节码.json      执行 M13.1 产出的 JSON 字节码
//   node node/index.js --self-test      自测（跑内置样例）
//
// 零第三方依赖，仅需 Node.js。

'use strict';

const fs = require('fs');
const Runtime = require('./runtime');
const { VM, STDLIB } = require('./vm');

//: 宿主认的字节码格式版本 —— **与 `jishi/serialize.py` 的 `FORMAT_VERSION`、
//: `core/src/serialize.rs` 的 `FORMAT_VERSION` 三处同步**，改一处就要改三处。
//: 3: R6.6 起 Code 增加 `local_hints`（按槽位的「赋值前读局部」提示）
const FORMAT_VERSION = 3;

function decodeConst(c) {
  if (c.t === 'none') return null;
  if (c.t === 'true') return true;
  if (c.t === 'false') return false;
  if (c.t === 'int') {
    // M32：超出 JS 安全整数范围的整数在字节码里是**十进制字符串**
    // （`9223372036854775807` 直接写数字会被 JSON.parse 静默变成
    // `9223372036854776000`），用 BigInt 还原成任意精度整数。
    // 旧产物（v 是数字）走原路，仍然兼容。
    return typeof c.v === 'string' ? BigInt(c.v) : c.v;
  }
  if (c.t === 'float') return new (require('./runtime').FloatBox)(c.v);
  if (c.t === 'str') return c.v;
  throw new Error('未知常量类型 ' + c.t);
}

// 把 payload 里的扁平指令流解码成「指令对象数组」
function decodePayload(payload) {
  const consts = payload.consts.map(decodeConst);
  const codes = payload.codes.map((cd) => {
    const flat = cd.instrs;
    const instrs = [];
    for (let i = 0; i < flat.length; i += 6) {
      instrs.push({ op: flat[i], a: flat[i + 1], b: flat[i + 2], c: flat[i + 3], line: flat[i + 4], col: flat[i + 5] });
    }
    // param_kind（M25）：形参种类 0=普通 1=*参数 2=**选项；
    // 旧产物没这个字段时按「全普通」处理
    // local_hints（v3）：按槽位的报错提示（「赋值前读局部」那句具体说法）。
    // JSON 的键是**字符串**，JS 对象照样用数字索引取（`hints[slot]`）。
    return {
      ...cd,
      paramKind: cd.param_kind ?? [],
      localHints: cd.local_hints ?? {},
      instrs,
    };
  });
  return { ...payload, consts, codes };
}

function loadPayload(text) {
  const body = JSON.parse(text);
  if (body.format !== 'jishi-bytecode') {
    throw new Error('不是基石字节码格式');
  }
  if (body.version !== FORMAT_VERSION) {
    throw new Error(`字节码版本 ${body.version} 与 JS 引擎 ${FORMAT_VERSION} 不兼容`);
  }
  return decodePayload(body.payload);
}

function runFile(path) {
  const text = fs.readFileSync(path, 'utf-8');
  const payload = loadPayload(text);
  const vm = new VM(payload);
  vm.run();
  return 0;
}

function selfTest() {
  // 内置样例：直接构造一个最小 payload（求和 1+2）
  // 说明：正式用法是 python 侧 jishi --dump-bytecode 产出字节码，
  // 这里自测只验证 VM 核心链路（用 Node 侧手写的等价字节码）。
  const payload = {
    consts: [{ t: 'int', v: 1 }, { t: 'int', v: 2 }],
    names: ['打印'],
    kw_names: [],
    main: 0,
    filename: '<自测>',
    codes: [{
      name: '<模块>',
      params: [], param_idx: [], param_local: [], param_cell: [],
      nlocals: 0, local_names: [], cellvars: [], freevars: [],
      firstlineno: 1,
      // 扁平指令流（每 6 个 int32 一条）：LOAD_CONST 1; LOAD_CONST 2;
      // BIN_OP +; LOAD_GLOBAL 打印; ROT_TWO; CALL 1; POP_TOP; HALT
      instrs: [
        1, 0, 0, 0, 1, 1,
        1, 1, 0, 0, 1, 1,
        13, 0, 0, 0, 1, 1,
        2, 0, 0, 0, 1, 1,
        11, 0, 0, 0, 1, 1,
        20, 1, -1, 0, 1, 1,
        9, 0, 0, 0, 1, 1,
        0, 0, 0, 0, 1, 1,
      ],
    }],
  };
  const vm = new VM(decodePayload(payload));
  vm.run();
  console.log('[自测] 求和 1+2 执行完成');
  return 0;
}

/**
 * `--dump-stdlib`：把这个宿主**实际带的标准库模块与函数**打成 JSON。
 *
 * M51 加的，用途只有一个：给 `tests/test_m51_consistency.py` 当事实源。
 * 由宿主自己报家底，比让测试去正则解析源码靠谱——解析一改就崩，
 * 还会把注释里的名字也算进去。漂移检测（「Python 侧有、宿主没有」）靠它。
 */
function dumpStdlib() {
  const out = {};
  for (const name of Object.keys(STDLIB)) {
    const m = STDLIB[name];
    out[m.name] = Object.keys(m.attrs)
      .filter((k) => m.attrs[k] instanceof Runtime.Builtin)
      .sort();
  }
  process.stdout.write(JSON.stringify(out));
  return 0;
}

/**
 * `--dump-methods`：这个宿主**实际带的「对象方法」**，打成 JSON（M54）。
 *
 * 与 `--dump-stdlib` 同一个道理：以前「宿主缺方法」完全没人管
 * （`列表`/`字典`/`文本`/`集合`/`文件`/`小数` 的方法表在五个引擎里各写一份），
 * Rust 缺 `文本.查找`、Node 缺 `文件.位置/定位/刷新` 都是**偶然撞见**的。
 * 数据源见 `Runtime.dumpMethods`（与分派同源，不是另抄的清单）。
 */
function dumpMethods() {
  process.stdout.write(JSON.stringify(Runtime.dumpMethods()));
  return 0;
}

/**
 * `--dump-builtins`：本宿主**实际带的「内建函数」**，打成 JSON（M55）。
 * 数据源见 `Runtime.dumpBuiltins`（过滤规则与 Python 侧 `is_internal_builtin`
 * 对齐：去 `$` 开头的、去内部用的、只留真正的内建函数）。
 */
function dumpBuiltins() {
  process.stdout.write(JSON.stringify(Runtime.dumpBuiltins()));
  return 0;
}

function main() {
  const args = process.argv.slice(2);
  if (args.length === 0 || args.includes('--help') || args.includes('-h')) {
    console.log('用法：node node/index.js 字节码.json');
    console.log('      node node/index.js --self-test');
    console.log('      node node/index.js --dump-stdlib');
    console.log('      node node/index.js --dump-methods');
    console.log('      node node/index.js --dump-builtins');
    return 0;
  }
  try {
    if (args.includes('--self-test')) return selfTest();
    if (args.includes('--dump-stdlib')) return dumpStdlib();
    if (args.includes('--dump-methods')) return dumpMethods();
    if (args.includes('--dump-builtins')) return dumpBuiltins();
    return runFile(args[0]);
  } catch (e) {
    // 基石错误打印成人话（此前是直接抛给 Node，用户看到的是 JS 堆栈，
    // 既看不出是哪种错误也看不到行列——M30 规范化，与 Rust 宿主同风格）
    if (e && e.isJishiError) {
      // R6.4：渲染与 Python 的 `JishiError.render()` **逐字相同**（标题 + 位置 +
      // 源码行 + `^` + 调用链 + 提示）。以前这里是「一行标题 + 一行位置」，
      // 用户**看不到自己写的那一行** —— 而宿主是 R7/R8 之后**唯一的形态**，
      // 那样等于没报错。
      //
      // ⚠️ 源码行**只在出口补**（对应 Python CLI 的 `with_source`）：按
      // `filename` 现读一次源文件；读不到就退回「没有源码行」，不崩。
      if (e.attachSource) e.attachSource();
      console.error(e.render ? e.render() : `错误（${e.typeName}）：${e.message}`);
      return 1;
    }
    console.error(`内部错误：${(e && e.stack) || e}`);
    return 1;
  }
}

if (require.main === module) {
  process.exit(main());
}

module.exports = { loadPayload, runFile, VM, dumpStdlib };
