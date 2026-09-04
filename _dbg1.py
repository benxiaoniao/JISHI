# -*- coding: utf-8 -*-
"""TEMP 调试：MAKE_FUNCTION ndefaults=0 时不 malloc defaults，二分闭包崩溃。"""
import io

p = "cvm/src/jsvm.c"
src = io.open(p, encoding="utf-8").read()
old = """                jfn->ndefaults = ndefaults;
                /* 默认值在栈顶（后压），先弹出（所有权转移，M7） */
                jfn->defaults = (JVal *)malloc(
                    (size_t)(ndefaults > 0 ? ndefaults : 1)
                    * sizeof(JVal));
                if (!jfn->defaults) { free(jfn);
                    vm->host->raise_internal("内存不足"); return -1; }
                for (int i = 0; i < ndefaults; i++)
                    jfn->defaults[i] = vm->stack[vm->sp - ndefaults + i];
                for (int i = ndefaults; i < 1; i++)
                    jfn->defaults[i] = make_unset();   /* 防未初始化读 */
                vm->sp -= ndefaults;"""
new = """                if (ndefaults) {
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
                jfn->ndefaults = ndefaults;"""
assert old in src, "anchor"
src = src.replace(old, new)
io.open(p, "w", encoding="utf-8", newline="\n").write(src)
print("done")
