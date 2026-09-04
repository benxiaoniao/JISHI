# -*- coding: utf-8 -*-
"""TEMP 调试：defaults 数组加哨兵魔数，检测越界写。"""
import io

p = "cvm/src/jsvm.c"
src = io.open(p, encoding="utf-8").read()

old = """                jfn->ndefaults = 0;
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
                }"""
new = """                jfn->ndefaults = 0;
                jfn->defaults = NULL;      /* 先安全化：任何路径都可 free */
                if (ndefaults) {
                    /* TEMP-DEBUG: 多分配哨兵槽（前后各 1 个 JVal） */
                    JVal *raw = (JVal *)malloc(
                        (size_t)(ndefaults + 2) * sizeof(JVal));
                    if (!raw) { free(jfn);
                        vm->host->raise_internal("内存不足"); return -1; }
                    raw[0].tag = 0x7C7C; raw[0].v.u = 0xDEAD0001;
                    raw[ndefaults + 1].tag = 0x7C7C; raw[ndefaults + 1].v.u = 0xDEAD0002;
                    jfn->defaults = raw + 1;
                    for (int i = 0; i < ndefaults; i++)
                        jfn->defaults[i] = vm->stack[vm->sp - ndefaults + i];
                    vm->sp -= ndefaults;
                    jfn->ndefaults = ndefaults;
                }"""
assert old in src, "make"
src = src.replace(old, new)

# release_func：free 前检查哨兵
old = """    for (int i = 0; i < f->ndefaults; i++)
        release_val(vm, f->defaults[i]);
    free(f->defaults);"""
new = """    for (int i = 0; i < f->ndefaults; i++)
        release_val(vm, f->defaults[i]);
    if (f->defaults) {
        JVal *raw = f->defaults - 1;   /* TEMP-DEBUG: 哨兵回读 */
        if (raw[0].tag != 0x7C7C) {
            FILE *dbg = fopen("cvm_debug.log", "a");
            if (dbg) { fprintf(dbg, "SENTINEL-LEFT-BROKEN tag=%d\\n", raw[0].tag); fclose(dbg); }
        }
        free(raw);
    } else {
        free(f->defaults);
    }"""
assert old in src, "release"
src = src.replace(old, new)
io.open(p, "w", encoding="utf-8", newline="\n").write(src)
print("哨兵版完成")
