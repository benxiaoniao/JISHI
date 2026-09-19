/* opcodes.h —— 自动生成，勿手改。
 * 来源：jishi/opcodes.py（唯一事实来源）。
 * 重新生成：python cvm/build.py --gen-header
 */
#ifndef JS_OPCODES_H
#define JS_OPCODES_H

enum {
    JS_OP_HALT = 0,
    JS_OP_LOAD_CONST = 1,
    JS_OP_LOAD_GLOBAL = 2,
    JS_OP_STORE_GLOBAL = 3,
    JS_OP_LOAD_FAST = 4,
    JS_OP_STORE_FAST = 5,
    JS_OP_LOAD_DEREF = 6,
    JS_OP_STORE_DEREF = 7,
    JS_OP_LOAD_CELL = 8,
    JS_OP_POP_TOP = 9,
    JS_OP_DUP_TOP = 10,
    JS_OP_ROT_TWO = 11,
    JS_OP_ROT_THREE = 12,
    JS_OP_BIN_OP = 13,
    JS_OP_UNARY_OP = 14,
    JS_OP_COMPARE = 15,
    JS_OP_JUMP = 16,
    JS_OP_POP_JUMP_IF_FALSE = 17,
    JS_OP_POP_JUMP_IF_TRUE = 18,
    JS_OP_JUMP_IF_FALSE_OR_POP = 19,
    JS_OP_CALL = 20,
    JS_OP_RETURN = 21,
    JS_OP_MAKE_FUNCTION = 22,
    JS_OP_BUILD_LIST = 23,
    JS_OP_BUILD_DICT = 24,
    JS_OP_GET_ATTR = 25,
    JS_OP_SET_ATTR = 26,
    JS_OP_GET_ITEM = 27,
    JS_OP_SET_ITEM = 28,
    JS_OP_GET_ITER = 29,
    JS_OP_FOR_ITER = 30,
    JS_OP_IMPORT = 31,
    JS_OP_DUP_TWO = 32,
    JS_OP_LOOP_SETUP = 33,
    JS_OP_SETUP_LOOP = 34,
    JS_OP_POP_BLOCK = 35,
    JS_OP_BREAK_LOOP = 36,
    JS_OP_CONTINUE_LOOP = 37,
    JS_OP_STORE_LAST = 38,
    JS_OP_SETUP_TRY = 39,
    JS_OP_POP_TRY = 40,
    JS_OP_THROW = 41,
    JS_OP_CHECK_SIGNAL = 42,
    JS_OP_MATCH_EXC = 43,
    JS_OP_END_FINALLY = 44,
    JS_OP_BUILD_CLASS = 45,
    JS_OP_UNPACK = 46,
    JS_OP_LIST_APPEND = 47,
    JS_OP_BUILD_SLICE = 48,
};

enum {
    JS_BIN_ADD = 0,
    JS_BIN_SUB = 1,
    JS_BIN_MUL = 2,
    JS_BIN_DIV = 3,
    JS_BIN_FLOORDIV = 4,
    JS_BIN_MOD = 5,
    JS_BIN_POW = 6,
};

enum {
    JS_CMP_EQ = 0,
    JS_CMP_NE = 1,
    JS_CMP_LT = 2,
    JS_CMP_GT = 3,
    JS_CMP_LE = 4,
    JS_CMP_GE = 5,
    JS_CMP_IN = 6,
    JS_CMP_NOT_IN = 7,
    JS_CMP_IS = 8,
    JS_CMP_IS_NOT = 9,
};

/* 注意：JS_CMP_IN/NOT_IN/IS/IS_NOT（6-9）没有 C 侧快路径实现。
 * compare_fast 只处理 0-5，其余自动回落 host->compare —— 这是
 * 「加语义比较运算符零 C 改动」的前提，见 jishi/opcodes.py 的 CmpOp。 */

enum {
    JS_UN_NEG = 0,
    JS_UN_NOT = 1,
};

enum {
    JS_PARAM_NORMAL = 0,
    JS_PARAM_VARARGS = 1,
    JS_PARAM_VARKW = 2,
};

enum {
    JS_TAG_UNSET = 0,
    JS_TAG_NULL = 1,
    JS_TAG_FALSE = 2,
    JS_TAG_TRUE = 3,
    JS_TAG_INT = 4,
    JS_TAG_FLOAT = 5,
    JS_TAG_HOST = 6,
    JS_TAG_CELL = 7,
    JS_TAG_FUNC = 8,
    JS_TAG_ITER = 9,
};

/* 帧深度上限（与 jishi/vm.py 的 MAX_FRAMES 一致） */
#define JS_MAX_FRAMES 1000

#endif /* JS_OPCODES_H */
