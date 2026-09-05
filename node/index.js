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
const { VM } = require('./vm');

function decodeConst(c) {
  if (c.t === 'none') return null;
  if (c.t === 'true') return true;
  if (c.t === 'false') return false;
  if (c.t === 'int') return c.v;
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
    return { ...cd, instrs };
  });
  return { ...payload, consts, codes };
}

function loadPayload(text) {
  const body = JSON.parse(text);
  if (body.format !== 'jishi-bytecode') {
    throw new Error('不是基石字节码格式');
  }
  if (body.version !== 1) {
    throw new Error(`字节码版本 ${body.version} 与 JS 引擎 1 不兼容`);
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

function main() {
  const args = process.argv.slice(2);
  if (args.length === 0 || args.includes('--help') || args.includes('-h')) {
    console.log('用法：node node/index.js 字节码.json');
    console.log('      node node/index.js --self-test');
    return 0;
  }
  if (args.includes('--self-test')) return selfTest();
  return runFile(args[0]);
}

if (require.main === module) {
  process.exit(main());
}

module.exports = { loadPayload, runFile, VM };
