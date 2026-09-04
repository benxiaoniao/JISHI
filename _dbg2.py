# -*- coding: utf-8 -*-
"""二分实验 2 修正版：适配 _dbg1 后的文件状态。"""
import io

# C 侧：删 JsHost.unpack 字段 + UNPACK case 禁用 + MAKE_FUNCTION 强制旧行为
p = "cvm/src/jsvm.c"
src = io.open(p, encoding="utf-8").read()

old = """    /* 解包（M7）：把栈顶可迭代解成 n 个元素（out 为预分配数组，所有权归 C） */
    int (*unpack)(JVal *value, int n, JVal *out, int line, int col);
"""
assert old in src, "C JsHost field"
src = src.replace(old, "")

old = """            case JS_OP_UNPACK: {
                int n = a;
                JVal v = POP(vm);"""
new = """            case JS_OP_UNPACK_DISABLED: {
                int n = a;
                JVal v = POP(vm);"""
assert old in src, "C UNPACK case"
src = src.replace(old, new)

old = """                int ncells = b;
                int ndefaults = c;
                JsFunc *jfn = (JsFunc *)malloc(sizeof *jfn);
                if (!jfn) { vm->host->raise_internal("内存不足"); return -1; }
                jfn->code_idx = a;
                jfn->ncells = ncells;
                if (ndefaults) {"""
new = """                int ncells = b;
                int ndefaults = 0;   /* TEMP-DEBUG2 */
                JsFunc *jfn = (JsFunc *)malloc(sizeof *jfn);
                if (!jfn) { vm->host->raise_internal("内存不足"); return -1; }
                jfn->code_idx = a;
                jfn->ncells = ncells;
                if (0) {"""
assert old in src, "C MAKE_FUNCTION"
src = src.replace(old, new)
io.open(p, "w", encoding="utf-8", newline="\n").write(src)

# Python 侧
p = "jishi/cvm_bind.py"
src = io.open(p, encoding="utf-8").read()
old = """        ("make_class", CB_MAKE_CLASS),
        ("unpack", CB_UNPACK),
        ("match_error", CB_MATCH_ERROR),"""
assert old in src, "Py fields"
src = src.replace(old, """        ("make_class", CB_MAKE_CLASS),
        ("match_error", CB_MATCH_ERROR),""")
old = """            make_class=CB_MAKE_CLASS(self._cb_make_class),
            unpack=CB_UNPACK(self._cb_unpack),
            match_error=CB_MATCH_ERROR(self._cb_match_error),"""
assert old in src, "Py register"
src = src.replace(old, """            make_class=CB_MAKE_CLASS(self._cb_make_class),
            match_error=CB_MATCH_ERROR(self._cb_match_error),""")
io.open(p, "w", encoding="utf-8", newline="\n").write(src)
print("两侧 unpack 已临时移除 + MAKE_FUNCTION 强制旧行为")
