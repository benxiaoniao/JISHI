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
    return { ...cd, paramKind: cd.param_kind ?? [], instrs };
  });
  return { ...payload, consts, codes };
}

function loadPayload(text) {
  const body = JSON.parse(text);
  if (body.format !== 'jishi-bytecode') {
    throw new Error('不是基石字节码格式');
  }
  if (body.version !== 2) {
    throw new Error(`字节码版本 ${body.version} 与 JS 引擎 2 不兼容`);
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

function main() {
  const args = process.argv.slice(2);
  if (args.length === 0 || args.includes('--help') || args.includes('-h')) {
    console.log('用法：node node/index.js 字节码.json');
    console.log('      node node/index.js --self-test');
    console.log('      node node/index.js --dump-stdlib');
    return 0;
  }
  try {
    if (args.includes('--self-test')) return selfTest();
    if (args.includes('--dump-stdlib')) return dumpStdlib();
    return runFile(args[0]);
  } catch (e) {
    // 基石错误打印成人话（此前是直接抛给 Node，用户看到的是 JS 堆栈，
    // 既看不出是哪种错误也看不到行列——M30 规范化，与 Rust 宿主同风格）
    if (e && e.isJishiError) {
      console.error(`错误（${e.typeName}）：${e.message}`);
      if (e.line != null) console.error(`  ← 第 ${e.line} 行第 ${e.col} 列`);
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
