# -*- coding: utf-8 -*-
"""TEMP 调试：CALL 内部细分打点。"""
import io

p = "cvm/src/jsvm.c"
src = io.open(p, encoding="utf-8").read()

old = """            case JS_OP_CALL: {
                int nargs = a;
                const int *knames = NULL;
                int nkw = 0;
                if (b >= 0) {
                    knames = vm->kw_flat + vm->kw_off[b];
                    nkw = vm->kw_off[b + 1] - vm->kw_off[b];
                }
                int fn_base = vm->sp - (1 + nargs + nkw);
                JVal fn = vm->stack[fn_base];

                if (fn.tag == JS_TAG_FUNC) {
                    JsFunc *jfn = (JsFunc *)(uintptr_t)fn.v.u;
                    JsCode *sub = &vm->codes[jfn->code_idx];"""
new = """            case JS_OP_CALL: {
                int nargs = a;
                const int *knames = NULL;
                int nkw = 0;
                if (b >= 0) {
                    knames = vm->kw_flat + vm->kw_off[b];
                    nkw = vm->kw_off[b + 1] - vm->kw_off[b];
                }
                int fn_base = vm->sp - (1 + nargs + nkw);
                JVal fn = vm->stack[fn_base];
                { FILE *dbg = fopen("cvm_debug.log", "a");
                  if (dbg) { fprintf(dbg, "CALL-b sp=%d nargs=%d fntag=%d\\n",
                                     vm->sp, nargs, fn.tag); fclose(dbg); } }

                if (fn.tag == JS_TAG_FUNC) {
                    JsFunc *jfn = (JsFunc *)(uintptr_t)fn.v.u;
                    { FILE *dbg = fopen("cvm_debug.log", "a");
                      if (dbg) { fprintf(dbg, "CALL-f code=%d ndef=%d\\n",
                                         jfn->code_idx, jfn->ndefaults); fclose(dbg); } }
                    JsCode *sub = &vm->codes[jfn->code_idx];"""
assert old in src, "CALL head"
src = src.replace(old, new)

# freevars 循环后打点
old = """                    for (int i = 0; i < nfreevars; i++) {
                        cells[ncellvars + i] = jfn->cells[i];
                        retain_cell(jfn->cells[i]);
                    }

                    SAVE_IP();"""
new = """                    for (int i = 0; i < nfreevars; i++) {
                        cells[ncellvars + i] = jfn->cells[i];
                        retain_cell(jfn->cells[i]);
                    }
                    { FILE *dbg = fopen("cvm_debug.log", "a");
                      if (dbg) { fprintf(dbg, "CALL-cells\\n"); fclose(dbg); } }

                    SAVE_IP();"""
assert old in src, "cells"
src = src.replace(old, new)

# bind_args 成功后打点
old = """                    free(args); free(kwv);"""
cnt = src.count(old)
# 只改 CALL 内那处（bind 之后的第一处）
idx = src.find(old, src.find("bind_args(vm, jfn->code_idx, args"))
assert idx > 0, "bind-after"
src = src[:idx] + """                    { FILE *dbg = fopen("cvm_debug.log", "a");
                      if (dbg) { fprintf(dbg, "CALL-bound\\n"); fclose(dbg); } }
""" + src[idx:]
io.open(p, "w", encoding="utf-8", newline="\n").write(src)
print("CALL 细分打点完成")
