# -*- coding: utf-8 -*-
"""修复根因：JsFunc.defaults 未初始化即被 free；并还原全部 TEMP 调试。"""
import io

p = "cvm/src/jsvm.c"
src = io.open(p, encoding="utf-8").read()

# 1. MAKE_FUNCTION：defaults 先置 NULL 再按需填充（修复根因）
old = """            case JS_OP_MAKE_FUNCTION: {
                { FILE *dbg = fopen("cvm_debug.log", "a");
                  if (dbg) { fprintf(dbg, "MAKE_FUNCTION sp=%d\\n", vm->sp); fclose(dbg); } }
                int ncells = b;
                int ndefaults = 0;   /* TEMP-DEBUG2 */
                JsFunc *jfn = (JsFunc *)malloc(sizeof *jfn);
                if (!jfn) { vm->host->raise_internal("内存不足"); return -1; }
                jfn->code_idx = a;
                jfn->ncells = ncells;
                if (0) {
                    jfn->defaults = (JVal *)malloc(
                        (size_t)ndefaults * sizeof(JVal));
                    if (!jfn->defaults) { free(jfn);
                        vm->host->raise_internal("内存不足"); return -1; }
                    for (int i = 0; i < ndefaults; i++)
                        jfn->defaults[i] = vm->stack[vm->sp - ndefaults + i];
                    vm->sp -= ndefaults;
                } else {
                    jfn->defaults = NULL;   /* TEMP-DEBUG: 0 时不 malloc */
                }
                jfn->ndefaults = ndefaults;
                jfn->cells = (JsCell **)malloc("""
new = """            case JS_OP_MAKE_FUNCTION: {
                int ncells = b;
                int ndefaults = c;
                JsFunc *jfn = (JsFunc *)malloc(sizeof *jfn);
                if (!jfn) { vm->host->raise_internal("内存不足"); return -1; }
                jfn->code_idx = a;
                jfn->ncells = ncells;
                jfn->ndefaults = 0;
                jfn->defaults = NULL;      /* 先安全化：任何路径都可 free */
                if (ndefaults) {
                    jfn->defaults = (JVal *)malloc(
                        (size_t)ndefaults * sizeof(JVal));
                    if (!jfn->defaults) { free(jfn);
                        vm->host->raise_internal("内存不足"); return -1; }
                    for (int i = 0; i < ndefaults; i++)
                        jfn->defaults[i] = vm->stack[vm->sp - ndefaults + i];
                    vm->sp -= ndefaults;
                    jfn->ndefaults = ndefaults;
                }
                jfn->cells = (JsCell **)malloc("""
assert old in src, "MAKE_FUNCTION"
src = src.replace(old, new)

# 2. 还原 CALL/RETURN 打点
old = """            case JS_OP_CALL: {
                { FILE *dbg = fopen("cvm_debug.log", "a");
                  if (dbg) { fprintf(dbg, "CALL sp=%d nargs=%d\\n", vm->sp, a); fclose(dbg); } }
                int nargs = a;"""
new = """            case JS_OP_CALL: {
                int nargs = a;"""
assert old in src, "CALL dbg"
src = src.replace(old, new)
old = """            case JS_OP_RETURN: {
                { FILE *dbg = fopen("cvm_debug.log", "a");
                  if (dbg) { fprintf(dbg, "RETURN sp=%d\\n", vm->sp); fclose(dbg); } }"""
new = """            case JS_OP_RETURN: {"""
assert old in src, "RETURN dbg"
src = src.replace(old, new)

# 3. 还原 UNPACK case（报错版 → 完整版）
old = """            case JS_OP_UNPACK: {
                vm->host->raise_internal("UNPACK 调试用禁用");
                return -1;
            }"""
new = """            case JS_OP_UNPACK: {
                int n = a;
                JVal v = POP(vm);
                JVal *items = (JVal *)malloc(
                    (size_t)(n > 0 ? n : 1) * sizeof(JVal));
                if (!items) { release_val(vm, v);
                    vm->host->raise_internal("内存不足"); return -1; }
                SAVE_IP();
                if (vm->host->unpack(&v, n, items, line, col) != 0) {
                    release_val(vm, v); free(items);
                    goto dispatch_err;
                }
                release_val(vm, v);
                /* 第一个元素最后压 → 栈顶（与 vm.py 一致） */
                for (int i = n - 1; i >= 0; i--) {
                    PUSH(vm, items[i]);
                }
                free(items);
                break;
            }"""
assert old in src, "UNPACK"
src = src.replace(old, new)

io.open(p, "w", encoding="utf-8", newline="\n").write(src)

# 4. cvm_bind.py：恢复 unpack 字段注册
p = "jishi/cvm_bind.py"
src = io.open(p, encoding="utf-8").read()
old = """        ("make_class", CB_MAKE_CLASS),
        ("match_error", CB_MATCH_ERROR),"""
assert old in src, "Py fields"
src = src.replace(old, """        ("make_class", CB_MAKE_CLASS),
        ("unpack", CB_UNPACK),
        ("match_error", CB_MATCH_ERROR),""")
old = """            make_class=CB_MAKE_CLASS(self._cb_make_class),
            match_error=CB_MATCH_ERROR(self._cb_match_error),"""
assert old in src, "Py register"
src = src.replace(old, """            make_class=CB_MAKE_CLASS(self._cb_make_class),
            unpack=CB_UNPACK(self._cb_unpack),
            match_error=CB_MATCH_ERROR(self._cb_match_error),""")
io.open(p, "w", encoding="utf-8", newline="\n").write(src)
print("根因修复 + 全部还原完成")
