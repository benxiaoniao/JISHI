# -*- coding: utf-8 -*-
"""TEMP 调试：CALL 成功路径细分打点。"""
import io

p = "cvm/src/jsvm.c"
src = io.open(p, encoding="utf-8").read()

old = """                    free(args); free(kwv);

                    /* 清掉栈上的 fn+args+kw（所有权已转移） */
                    vm->sp = fn_base;
                    release_val(vm, fn);

                    push_frame(vm, jfn->code_idx, fn_base, locals,
                               sub->nlocals);"""
new = """                    free(args); free(kwv);
                    { FILE *dbg = fopen("cvm_debug.log", "a");
                      if (dbg) { fprintf(dbg, "CALL-freed-args\\n"); fclose(dbg); } }

                    /* 清掉栈上的 fn+args+kw（所有权已转移） */
                    vm->sp = fn_base;
                    release_val(vm, fn);
                    { FILE *dbg = fopen("cvm_debug.log", "a");
                      if (dbg) { fprintf(dbg, "CALL-released-fn\\n"); fclose(dbg); } }

                    push_frame(vm, jfn->code_idx, fn_base, locals,
                               sub->nlocals);
                    { FILE *dbg = fopen("cvm_debug.log", "a");
                      if (dbg) { fprintf(dbg, "CALL-pushed\\n"); fclose(dbg); } }"""
assert old in src, "anchor"
src = src.replace(old, new)
io.open(p, "w", encoding="utf-8", newline="\n").write(src)
print("done")
