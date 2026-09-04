# -*- coding: utf-8 -*-
"""TEMP 调试：CALL/MAKE_FUNCTION/RETURN 打点到文件，定位崩溃指令。"""
import io

p = "cvm/src/jsvm.c"
src = io.open(p, encoding="utf-8").read()

# CALL case 开头打点
old = """            case JS_OP_CALL: {
                int nargs = a;"""
new = """            case JS_OP_CALL: {
                { FILE *dbg = fopen("cvm_debug.log", "a");
                  if (dbg) { fprintf(dbg, "CALL sp=%d nargs=%d\\n", vm->sp, a); fclose(dbg); } }
                int nargs = a;"""
assert old in src, "CALL"
src = src.replace(old, new)

# MAKE_FUNCTION 打点
old = """            case JS_OP_MAKE_FUNCTION: {
                int ncells = b;
                int ndefaults = 0;   /* TEMP-DEBUG2 */"""
new = """            case JS_OP_MAKE_FUNCTION: {
                { FILE *dbg = fopen("cvm_debug.log", "a");
                  if (dbg) { fprintf(dbg, "MAKE_FUNCTION sp=%d\\n", vm->sp); fclose(dbg); } }
                int ncells = b;
                int ndefaults = 0;   /* TEMP-DEBUG2 */"""
assert old in src, "MAKE_FUNCTION"
src = src.replace(old, new)

# RETURN 打点（找 RETURN case）
old = """            case JS_OP_RETURN: {"""
if old in src:
    new = """            case JS_OP_RETURN: {
                { FILE *dbg = fopen("cvm_debug.log", "a");
                  if (dbg) { fprintf(dbg, "RETURN sp=%d\\n", vm->sp); fclose(dbg); } }"""
    src = src.replace(old, new)

io.open(p, "w", encoding="utf-8", newline="\n").write(src)
print("打点完成")
