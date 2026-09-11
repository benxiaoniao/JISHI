/* hello.c —— 基石 C VM 最小嵌入示例（M9.4）
 *
 * 演示第三方 C 宿主如何用 jsvm.h 头文件链接 jsvm.dll，
 * 加载一段最小字节码（LOAD_CONST 42; STORE_LAST; HALT）并运行。
 *
 * 编译（MinGW）：
 *   gcc hello.c -I ../../cvm/include ../../cvm/bin/jsvm.dll -o hello.exe
 * 运行：
 *   ./hello.exe
 */

#include <stdio.h>
#include "jsvm.h"

int main(void) {
    JsVm *vm = jsvm_create();
    if (!vm) { fprintf(stderr, "创建 VM 失败\n"); return 1; }

    /* 最小字节码：LOAD_CONST 42; STORE_LAST; HALT
     * 每条指令 6 个 int32：(op, a, b, c, line, col) */
    JVal consts[1] = { jsvm_int(42) };
    int32_t instrs[18] = {
        1, 0, 0, 0, 1, 1,    /* LOAD_CONST  consts[0] = 42 */
        38, 0, 0, 0, 1, 1,   /* STORE_LAST */
        0, 0, 0, 0, 1, 1,    /* HALT */
    };
    /* 每个代码对象 6 个 int32：(instr_off, ninstrs, nlocals,
     *                           ncellvars, nparams, params_off) */
    int32_t code_meta[6] = { 0, 3, 0, 0, 0, 0 };
    int32_t kw_off[1] = { 0 };   /* 无关键字参数 */

    if (jsvm_load_module(vm, consts, 1, 0, NULL,
                         instrs, 3, code_meta, 1, NULL,
                         NULL, kw_off, 0, 0) != 0) {
        fprintf(stderr, "加载字节码失败\n");
        jsvm_destroy(vm);
        return 1;
    }

    if (jsvm_run(vm) != 0) {
        fprintf(stderr, "运行失败\n");
        jsvm_destroy(vm);
        return 1;
    }

    JVal out;
    jsvm_get_last(vm, &out);
    if (out.tag == JS_TAG_INT && out.v.i == 42) {
        printf("OK: last_value = %lld (ABI v%d)\n",
               (long long)out.v.i, JSVM_ABI_VERSION);
        jsvm_destroy(vm);
        return 0;
    }
    printf("FAIL: tag=%d\n", out.tag);
    jsvm_destroy(vm);
    return 1;
}
