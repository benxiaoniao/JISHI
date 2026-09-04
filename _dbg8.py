# -*- coding: utf-8 -*-
"""TEMP 调试 v2：CALL 逐语句打点。"""
import io

p = "cvm/src/jsvm.c"
src = io.open(p, encoding="utf-8").read()

TRACE = '''                    { FILE *d2 = fopen("cvm_trace.log", "a");
                      if (d2) { fprintf(d2, "[CALL-step __LBL__]\\n"); fclose(d2); } }
'''

steps = [
    ("JsFunc *jfn = (JsFunc *)(uintptr_t)fn.v.u;", "read-jfn"),
    ("JsCode *sub = &vm->codes[jfn->code_idx];", "read-code"),
    ("JVal *locals = (JVal *)malloc(", "before-locals"),
    ("if (vm->nframes >= JS_MAX_FRAMES) {", "max-frames"),
    ("/* 帧单元：cellvars 新建；freevars 借用函数对象的 */", "before-cells"),
    ("for (int i = 0; i < nfreevars; i++) {", "before-freevars"),
    ("SAVE_IP();\n                    if (bind_args(vm, jfn->code_idx, args, nargs,", "before-bind"),
    ("/* 清掉栈上的 fn+args+kw（所有权已转移） */", "after-bind"),
    ("push_frame(vm, jfn->code_idx, fn_base, locals,", "before-push"),
]

for anchor, label in steps:
    if anchor not in src:
        raise SystemExit(f"锚点缺失: {label}")
    mark = TRACE.replace("__LBL__", label)
    src = src.replace(anchor, mark + anchor, 1)

io.open(p, "w", encoding="utf-8", newline="\n").write(src)
print("CALL 逐语句打点完成")
