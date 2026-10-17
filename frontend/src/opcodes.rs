//! 基石字节码指令集（R5）—— **必须与 `jishi/opcodes.py` 逐项相同**。
//!
//! ⚠️ 这是这份编号的第**三**份拷贝（另外两份：`rust/src/lib.rs` 的 `mod op`、
//! `node/vm.js` 的 `Op`）。它们都是**宿主**（吃字节码跑），这里的是**前端**
//! （产字节码）—— 谁也不能少。防漂移靠两件事：
//!
//! 1. 产物里带着 `opcode_names` 表，宿主加载时自己比一遍（M13 起）；
//! 2. `tests/test_m62_compiler_rust.py` 拿 `opcode_table()` 与 Python 侧对拍
//!    —— 与 R2 对关键字/运算符表用的是同一个套路。
//!
//! 指令格式：每条 6 个字段 `(op, a, b, c, line, col)`。

// ---------------------------------------------------------------------------
// 指令号
// ---------------------------------------------------------------------------

pub const HALT: i64 = 0;
pub const LOAD_CONST: i64 = 1;
pub const LOAD_GLOBAL: i64 = 2;
pub const STORE_GLOBAL: i64 = 3;
pub const LOAD_FAST: i64 = 4;
pub const STORE_FAST: i64 = 5;
pub const LOAD_DEREF: i64 = 6;
pub const STORE_DEREF: i64 = 7;
pub const LOAD_CELL: i64 = 8;
pub const POP_TOP: i64 = 9;
pub const DUP_TOP: i64 = 10;
pub const ROT_TWO: i64 = 11;
pub const ROT_THREE: i64 = 12;
pub const BIN_OP: i64 = 13;
pub const UNARY_OP: i64 = 14;
pub const COMPARE: i64 = 15;
pub const JUMP: i64 = 16;
pub const POP_JUMP_IF_FALSE: i64 = 17;
pub const POP_JUMP_IF_TRUE: i64 = 18;
pub const JUMP_IF_FALSE_OR_POP: i64 = 19;
pub const CALL: i64 = 20;
pub const RETURN: i64 = 21;
pub const MAKE_FUNCTION: i64 = 22;
pub const BUILD_LIST: i64 = 23;
pub const BUILD_DICT: i64 = 24;
pub const GET_ATTR: i64 = 25;
pub const SET_ATTR: i64 = 26;
pub const GET_ITEM: i64 = 27;
pub const SET_ITEM: i64 = 28;
pub const GET_ITER: i64 = 29;
pub const FOR_ITER: i64 = 30;
pub const IMPORT: i64 = 31;
pub const DUP_TWO: i64 = 32;
pub const LOOP_SETUP: i64 = 33;
pub const SETUP_LOOP: i64 = 34;
pub const POP_BLOCK: i64 = 35;
pub const BREAK_LOOP: i64 = 36;
pub const CONTINUE_LOOP: i64 = 37;
pub const STORE_LAST: i64 = 38;
pub const SETUP_TRY: i64 = 39;
pub const POP_TRY: i64 = 40;
pub const THROW: i64 = 41;
pub const CHECK_SIGNAL: i64 = 42;
pub const MATCH_EXC: i64 = 43;
pub const END_FINALLY: i64 = 44;
pub const BUILD_CLASS: i64 = 45;
pub const UNPACK: i64 = 46;
pub const LIST_APPEND: i64 = 47;
pub const BUILD_SLICE: i64 = 48;
pub const METHOD_CALL: i64 = 49;

/// 指令号 → 名字（按号排序，与 `opcodes.OP_NAMES` 同序同内容）。
pub const OP_NAMES: [&str; 50] = [
    "HALT",
    "LOAD_CONST",
    "LOAD_GLOBAL",
    "STORE_GLOBAL",
    "LOAD_FAST",
    "STORE_FAST",
    "LOAD_DEREF",
    "STORE_DEREF",
    "LOAD_CELL",
    "POP_TOP",
    "DUP_TOP",
    "ROT_TWO",
    "ROT_THREE",
    "BIN_OP",
    "UNARY_OP",
    "COMPARE",
    "JUMP",
    "POP_JUMP_IF_FALSE",
    "POP_JUMP_IF_TRUE",
    "JUMP_IF_FALSE_OR_POP",
    "CALL",
    "RETURN",
    "MAKE_FUNCTION",
    "BUILD_LIST",
    "BUILD_DICT",
    "GET_ATTR",
    "SET_ATTR",
    "GET_ITEM",
    "SET_ITEM",
    "GET_ITER",
    "FOR_ITER",
    "IMPORT",
    "DUP_TWO",
    "LOOP_SETUP",
    "SETUP_LOOP",
    "POP_BLOCK",
    "BREAK_LOOP",
    "CONTINUE_LOOP",
    "STORE_LAST",
    "SETUP_TRY",
    "POP_TRY",
    "THROW",
    "CHECK_SIGNAL",
    "MATCH_EXC",
    "END_FINALLY",
    "BUILD_CLASS",
    "UNPACK",
    "LIST_APPEND",
    "BUILD_SLICE",
    "METHOD_CALL",
];

// ---------------------------------------------------------------------------
// 运算符码
// ---------------------------------------------------------------------------

pub const BINOP_ADD: i64 = 0;
pub const BINOP_SUB: i64 = 1;
pub const BINOP_MUL: i64 = 2;
pub const BINOP_DIV: i64 = 3;
pub const BINOP_FLOORDIV: i64 = 4;
pub const BINOP_MOD: i64 = 5;
pub const BINOP_POW: i64 = 6;

/// 源码符号 → 二元运算码（`compiler._c_assign` 里用 `op[:-1]` 去掉 `=`）。
pub fn symbol_to_binop(sym: &str) -> Option<i64> {
    Some(match sym {
        "+" => BINOP_ADD,
        "-" => BINOP_SUB,
        "*" => BINOP_MUL,
        "/" => BINOP_DIV,
        "//" => BINOP_FLOORDIV,
        "%" => BINOP_MOD,
        "**" => BINOP_POW,
        _ => return None,
    })
}

pub const UNOP_NEG: i64 = 0;
pub const UNOP_NOT: i64 = 1;

pub fn symbol_to_unop(sym: &str) -> Option<i64> {
    Some(match sym {
        "-" => UNOP_NEG,
        "非" => UNOP_NOT,
        _ => return None,
    })
}

pub const CMPOP_EQ: i64 = 0;
pub const CMPOP_NE: i64 = 1;
pub const CMPOP_LT: i64 = 2;
pub const CMPOP_GT: i64 = 3;
pub const CMPOP_LE: i64 = 4;
pub const CMPOP_GE: i64 = 5;
pub const CMPOP_IN: i64 = 6;
pub const CMPOP_NOT_IN: i64 = 7;
pub const CMPOP_IS: i64 = 8;
pub const CMPOP_IS_NOT: i64 = 9;

pub fn symbol_to_cmpop(sym: &str) -> Option<i64> {
    Some(match sym {
        "==" => CMPOP_EQ,
        "!=" => CMPOP_NE,
        "<" => CMPOP_LT,
        ">" => CMPOP_GT,
        "<=" => CMPOP_LE,
        ">=" => CMPOP_GE,
        "在" => CMPOP_IN,
        "不在" => CMPOP_NOT_IN,
        "是" => CMPOP_IS,
        "不是" => CMPOP_IS_NOT,
        _ => return None,
    })
}

// ---------------------------------------------------------------------------
// 形参种类（M25）
// ---------------------------------------------------------------------------

pub const PARAM_NORMAL: i64 = 0;
pub const PARAM_VARARGS: i64 = 1;
pub const PARAM_VARKW: i64 = 2;
