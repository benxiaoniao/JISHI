/* jsvm.c —— 基石 C 字节码虚拟机（M4b）
 *
 * 设计规格：cvm/bytecode.md
 * 指令号：  cvm/include/opcodes.h（由 jishi/opcodes.py 自动生成）
 *
 * 本文件不链接 libpython：一切复杂语义（打印/文本/列表/字典/导入/
 * Python 生态/报错文案）经 JsHost 回调桥回 Python，由 jishi/cvm_bind.py
 * 实现，与树遍历解释器共用 jishi/runtime.py 的措辞，
 * 由 tests/test_cvm.py 的对拍测试保证三执行器逐字节一致。
 *
 * C 侧保留的热路径（无回调）：
 *   整数/浮点算术（+ - * / 与整数的 // %，含 Python 语义修正与溢出回落）、
 *   比较、跳转、局部槽/全局槽读写、栈操作、基石函数调用与返回、
 *   「循环 n 次」计数循环、闭包单元读写。
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "jsvm.h"

/* 值表示 JVal / 宿主回调 JsHost / 导出函数声明见 jsvm.h（M9.4 公开 ABI） */

static JVal make_unset(void) {
    JVal x; memset(&x, 0, sizeof x); x.tag = JS_TAG_UNSET; return x;
}
static JVal make_int(int64_t i) {
    JVal x; memset(&x, 0, sizeof x); x.tag = JS_TAG_INT; x.v.i = i; return x;
}
static JVal make_float(double d) {
    JVal x; memset(&x, 0, sizeof x); x.tag = JS_TAG_FLOAT; x.v.d = d; return x;
}
static JVal make_bool(int b) {
    JVal x; memset(&x, 0, sizeof x); x.tag = b ? JS_TAG_TRUE : JS_TAG_FALSE;
    return x;
}
static JVal make_null(void) {
    JVal x; memset(&x, 0, sizeof x); x.tag = JS_TAG_NULL; return x;
}
static JVal make_cell(struct JsCell *c) {
    JVal x; memset(&x, 0, sizeof x); x.tag = JS_TAG_CELL;
    x.v.u = (uint64_t)(uintptr_t)c; return x;
}
static JVal make_func(struct JsFunc *f) {
    JVal x; memset(&x, 0, sizeof x); x.tag = JS_TAG_FUNC;
    x.v.u = (uint64_t)(uintptr_t)f; return x;
}

/* ------------------------------------------------------------------ */
/* 闭包单元 / 函数对象                                                  */
/* ------------------------------------------------------------------ */

typedef struct JsCell {
    JVal v;       /* JS_TAG_UNSET 表示未初始化 */
    int refcnt;
} JsCell;

typedef struct JsFunc {
    int code_idx;
    int ncells;
    JsCell **cells;   /* 与代码对象 freevars 顺序一致 */
    int ndefaults;
    JVal *defaults;   /* 默认参数值（定义处求值，M7）；归函数对象持有 */
    int refcnt;
} JsFunc;

/* C 侧迭代器：批量预取缓冲 + 宿主迭代器句柄 */
#define JS_ITER_BUF 256
typedef struct JsIterC {
    int refcnt;
    uint32_t host_id;      /* 宿主侧迭代器对象（_Iter）的句柄 */
    JVal buf[JS_ITER_BUF];
    int nbuf, pos;
    int exhausted;
    int batchable;         /* 0：逐个向宿主要；1：批量取 */
} JsIterC;

static JVal make_iter(JsIterC *it) {
    JVal x; memset(&x, 0, sizeof x); x.tag = JS_TAG_ITER;
    x.v.u = (uint64_t)(uintptr_t)it; return x;
}

/* ------------------------------------------------------------------ */
/* 宿主回调表（全部返回 0 成功 / -1 出错；出错细节由宿主存 pending）      */
/* ------------------------------------------------------------------ */



/* ------------------------------------------------------------------ */
/* 代码对象（load 时从扁平数组构建）                                     */
/* ------------------------------------------------------------------ */

typedef struct {
    int instr_off;    /* 在扁平 instrs 里的起始（以指令计） */
    int ninstrs;
    int nlocals;
    int ncellvars;    /* 本作用域绑定、被内层引用 */
    int nfreevars;    /* 引用外层 */
    int nparams;
    int params_off;   /* 在 code_params 里的起始（3*nparams 个 int32） */
} JsCode;

/* ------------------------------------------------------------------ */
/* 帧与虚拟机                                                           */
/* ------------------------------------------------------------------ */

#define JS_MAX_LOOPS 64
#define JS_STACK_CAP (1 << 16)

typedef struct {
    int depth, cont, brk;
    int seq;             /* 与处理器共用登记序号（越大越内层） */
} JsLoop;

/* 异常处理器（SETUP_TRY 登记）。seq 越大越内层（越近）。 */
typedef struct {
    int catch_ip;      /* 异常分发入口（栈顶压异常对象后跳转） */
    int finally_ip;    /* finally 代码入口（-1 表示无 finally） */
    int sp;            /* 登记时的栈深度（分发时截栈用） */
    int seq;
} JsHandler;

#define JS_MAX_HANDLERS 64

typedef struct {
    int code_idx;
    int ip;
    int base;              /* 帧在值栈上的栈底 */
    JVal *locals;
    int nlocals;
    JsCell **cells;        /* cellvars 部分自建，freevars 部分借用（各自持引用） */
    int ncells;
    JsLoop loops[JS_MAX_LOOPS];
    int nloops;
    JsHandler handlers[JS_MAX_HANDLERS];
    int nhandlers;
    int block_seq;         /* 循环/处理器共用登记序号 */
    int pending_return;    /* 被 finally 挂起的返回值：0=无，1=有 */
    JVal ret_val;          /* 挂起的返回值 */
} JsFrame;

typedef struct JsVm {
    JsHost *host;
    JVal *consts; int nconsts;
    int nnames;
    JVal *globals;         /* 按名字下标索引；UNSET = 未定义 */
    JsCode *codes; int ncodes;
    int32_t *instrs;       /* 扁平：每指令 6 个 int32 */
    int32_t *code_params;  /* 扁平：每参数 3 个 int32（idx/local/cell） */
    int32_t *kw_flat;      /* 关键字名表（名字下标） */
    int32_t *kw_off;       /* 前缀和，长度 nkw+1 */
    int nkw;
    int main_idx;
    JVal *stack; int sp;   /* 预分配 JS_STACK_CAP，不再扩容 */
    JsFrame *frames; int nframes;
    uint32_t *hrefcnt; int hcap;   /* HOST 句柄引用计数 */
    JVal last_value;
    JVal pending_err;      /* 宿主回调构造的异常对象（HOST 句柄），UNSET=无 */
    int halted;
    /* run_loop 的帧边界：RETURN/HALT 弹到这一层就停（重入支持）。
     * jsvm_run 设 0；jsvm_call_function 设为进入时的帧深。 */
    int stop_depth;
} JsVm;

/* ------------------------------------------------------------------ */
/* 引用计数                                                             */
/* ------------------------------------------------------------------ */

static int ensure_hcap(JsVm *vm, uint32_t id) {
    if ((int)id < vm->hcap) return 0;
    int ncap = vm->hcap ? vm->hcap : 64;
    while (ncap <= (int)id) ncap *= 2;
    uint32_t *p = (uint32_t *)realloc(vm->hrefcnt,
                                      (size_t)ncap * sizeof *p);
    if (!p) return -1;
    memset(p + vm->hcap, 0, (size_t)(ncap - vm->hcap) * sizeof *p);
    vm->hrefcnt = p;
    vm->hcap = ncap;
    return 0;
}

/* 新句柄登记（宿主刚创建对象，refcnt = 1，所有权即将交给 C 侧某个槽位） */
JSVM_API int jsvm_track_handle(JsVm *vm, uint32_t id) {
    if (ensure_hcap(vm, id) != 0) return -1;
    vm->hrefcnt[id] = 1;
    return 0;
}

static void retain_val(JsVm *vm, JVal v);
static void release_val(JsVm *vm, JVal v);

static void retain_cell(JsCell *c) { if (c) c->refcnt++; }

static void release_cell(JsVm *vm, JsCell *c) {
    if (!c || --c->refcnt > 0) return;
    release_val(vm, c->v);
    free(c);
}

static void retain_func(JsFunc *f) { if (f) f->refcnt++; }

static void release_func(JsVm *vm, JsFunc *f) {
    if (!f || --f->refcnt > 0) return;
    for (int i = 0; i < f->ncells; i++) release_cell(vm, f->cells[i]);
    free(f->cells);
    for (int i = 0; i < f->ndefaults; i++)
        release_val(vm, f->defaults[i]);
    free(f->defaults);
    free(f);
}

static void retain_val(JsVm *vm, JVal v) {
    (void)vm;
    switch (v.tag) {
    case JS_TAG_HOST: {
        uint32_t id = (uint32_t)v.v.u;
        if ((int)id < vm->hcap && vm->hrefcnt[id] > 0)
            vm->hrefcnt[id]++;
        break;
    }
    case JS_TAG_CELL:  retain_cell((JsCell *)(uintptr_t)v.v.u); break;
    case JS_TAG_FUNC:  retain_func((JsFunc *)(uintptr_t)v.v.u); break;
    case JS_TAG_ITER:  ((JsIterC *)(uintptr_t)v.v.u)->refcnt++; break;
    default: break;   /* 数值/UNSET 无需计数 */
    }
}

static void release_val(JsVm *vm, JVal v) {
    switch (v.tag) {
    case JS_TAG_HOST: {
        uint32_t id = (uint32_t)v.v.u;
        if ((int)id >= vm->hcap || vm->hrefcnt[id] == 0) break; /* 防御 */
        if (--vm->hrefcnt[id] == 0)
            vm->host->release_handle(id);
        break;
    }
    case JS_TAG_CELL:  release_cell(vm, (JsCell *)(uintptr_t)v.v.u); break;
    case JS_TAG_FUNC:  release_func(vm, (JsFunc *)(uintptr_t)v.v.u); break;
    case JS_TAG_ITER: {
        JsIterC *it = (JsIterC *)(uintptr_t)v.v.u;
        if (--it->refcnt > 0) break;
        for (int i = it->pos; i < it->nbuf; i++)
            release_val(vm, it->buf[i]);
        vm->host->release_handle(it->host_id);
        free(it);
        break;
    }
    default: break;
    }
}

/* ------------------------------------------------------------------ */
/* 创建 / 装载 / 销毁                                                    */
/* ------------------------------------------------------------------ */

JsVm *jsvm_create(void) {
    JsVm *vm = (JsVm *)calloc(1, sizeof *vm);
    if (!vm) return NULL;
    vm->stack = (JVal *)malloc((size_t)JS_STACK_CAP * sizeof(JVal));
    vm->frames = (JsFrame *)malloc(
        (size_t)JS_MAX_FRAMES * sizeof(JsFrame));
    if (!vm->stack || !vm->frames) {
        free(vm->stack); free(vm->frames); free(vm);
        return NULL;
    }
    vm->last_value = make_unset();
    vm->pending_err = make_unset();
    return vm;
}

JSVM_API void jsvm_set_host(JsVm *vm, JsHost *host) { vm->host = host; }

/*
 * 装载模块。code_meta 每个代码对象 6 个 int32：
 *   instr_off, ninstrs, nlocals, ncellvars, nparams, params_off
 * code_params 每个参数 3 个 int32：param_idx, param_local, param_cell
 * globals_in：初始全局（内建函数），长度 nnames，UNSET 表示无；
 *   值所有权转移给 C（句柄引用计数归 C 管）。
 * consts 值所有权转移给 C；数组本身的内存仍归宿主持有，
 *   宿主必须保证这些数组在 jsvm_destroy 之前存活。
 */
JSVM_API int jsvm_load_module(JsVm *vm,
                     JVal *consts, int nconsts,
                     int nnames,
                     JVal *globals_in,
                     int32_t *instrs, int ninstrs,
                     int32_t *code_meta, int ncodes,
                     int32_t *code_params,
                     int32_t *kw_flat, int32_t *kw_off, int nkw,
                     int main_idx) {
    (void)ninstrs;
    vm->consts = consts; vm->nconsts = nconsts;
    vm->nnames = nnames;
    vm->globals = globals_in;   /* 长度 nnames */
    vm->instrs = instrs;
    vm->code_params = code_params;
    vm->kw_flat = kw_flat; vm->kw_off = kw_off; vm->nkw = nkw;
    vm->main_idx = main_idx;

    vm->codes = (JsCode *)calloc((size_t)ncodes, sizeof(JsCode));
    if (!vm->codes) return -1;
    vm->ncodes = ncodes;
    for (int i = 0; i < ncodes; i++) {
        const int32_t *m = code_meta + (size_t)i * 6;
        vm->codes[i].instr_off = m[0];
        vm->codes[i].ninstrs   = m[1];
        vm->codes[i].nlocals   = m[2];
        vm->codes[i].ncellvars = m[3];
        vm->codes[i].nparams   = m[4];
        vm->codes[i].params_off = m[5];
    }
    return 0;
}

static void frame_teardown(JsVm *vm, JsFrame *f) {
    /* 清值栈：帧内残留（RETURN/HALT 已自行清完的不会重复） */
    while (vm->sp > f->base) {
        release_val(vm, vm->stack[--vm->sp]);
    }
    if (f->locals) {
        for (int i = 0; i < f->nlocals; i++) {
            if (f->locals[i].tag != JS_TAG_UNSET)
                release_val(vm, f->locals[i]);
        }
        free(f->locals);
    }
    if (f->cells) {
        for (int i = 0; i < f->ncells; i++)
            release_cell(vm, f->cells[i]);
        free(f->cells);
    }
}

JSVM_API void jsvm_destroy(JsVm *vm) {
    if (!vm) return;
    for (int i = 0; i < vm->nframes; i++)
        frame_teardown(vm, &vm->frames[i]);
    while (vm->sp > 0)
        release_val(vm, vm->stack[--vm->sp]);
    for (int i = 0; i < vm->nconsts; i++)
        if (vm->consts && vm->consts[i].tag != JS_TAG_UNSET)
            release_val(vm, vm->consts[i]);
    for (int i = 0; i < vm->nnames; i++)
        if (vm->globals && vm->globals[i].tag != JS_TAG_UNSET)
            release_val(vm, vm->globals[i]);
    if (vm->last_value.tag != JS_TAG_UNSET)
        release_val(vm, vm->last_value);
    if (vm->pending_err.tag != JS_TAG_UNSET)
        release_val(vm, vm->pending_err);
    /* consts/globals/instrs/code_params/kw_flat/kw_off 由宿主（Python）
     * 分配并持有，这里绝不 free —— 跨运行时释放会损坏堆。 */
    free(vm->codes); free(vm->stack); free(vm->frames);
    free(vm->hrefcnt);
    free(vm);
}

/* ------------------------------------------------------------------ */
/* 栈操作                                                               */
/* ------------------------------------------------------------------ */

#define PUSH(vm, val) do { \
        if ((vm)->sp >= JS_STACK_CAP) { \
            (vm)->host->raise_internal("值栈溢出"); \
            return -1; \
        } \
        (vm)->stack[(vm)->sp++] = (val); \
    } while (0)

/* 转移语义：POP 取出的值由调用者负责（写槽 / DECREF） */
#define POP(vm) ((vm)->stack[--(vm)->sp])

/* ------------------------------------------------------------------ */
/* 帧创建                                                               */
/* ------------------------------------------------------------------ */

static int push_frame(JsVm *vm, int code_idx, int base,
                      JVal *locals, int nlocals) {
    if (vm->nframes >= JS_MAX_FRAMES) return -1;
    JsFrame *f = &vm->frames[vm->nframes++];
    memset(f, 0, sizeof *f);
    f->code_idx = code_idx;
    f->ip = 0;
    f->base = base;
    f->locals = locals;
    f->nlocals = nlocals;
    /* cells 由调用者随后填充（f->cells / f->ncells） */
    return 0;
}

/* ------------------------------------------------------------------ */
/* 参数绑定（CALL 与重入共用）                                            */
/* ------------------------------------------------------------------ */

/*
 * 绑定实参到局部槽与单元，返回 0；出错时宿主已抛错，返回 -1。
 *
 * 所有权（M25 调整为**借用**语义）：args/kwvals 由调用者持有并负责释放，
 * 本函数把值写进 locals/units 时会 retain 一次。这么改是为了让
 * `*参数` 走通——同一个实参可能「既被打包进列表、又留在数组里等待统一
 * 释放」，借用语义下不需要再区分「这个值到底进没进槽位」，不会双释或漏释。
 *
 * 调用点必须已 SAVE_IP()（本函数内会调用宿主回调，可能抛错）。
 * nfree_cells：函数对象的单元（freevars 部分）。
 */
static int bind_args(JsVm *vm, int code_idx,
                     JVal *args, int nargs,
                     const int *kwname_idx, JVal *kwvals, int nkw,
                     JVal *locals_out, JsCell **free_cells, int nfree,
                     const JVal *defaults, int ndefaults,
                     int line, int col) {
    JsCode *code = &vm->codes[code_idx];
    int nparams = code->nparams;
    const int32_t *pp = vm->code_params + code->params_off;
    (void)nfree;

    /* bound[i] 的 UNSET 表示未绑定 */
    JVal *bound = (JVal *)malloc(
        (size_t)(nparams > 0 ? nparams : 1) * sizeof(JVal));
    if (!bound) return -1;
    for (int i = 0; i < nparams; i++) bound[i] = make_unset();

    /* 形参种类（M25）：code_params 每个参数的第 4 个 int32。 */
    int star_pos = -1, dstar_pos = -1;
    for (int i = 0; i < nparams; i++) {
        int kind = pp[i * 4 + 3];
        if (kind == JS_PARAM_VARARGS) star_pos = i;
        else if (kind == JS_PARAM_VARKW) dstar_pos = i;
    }
    /* 能吃「普通位置实参」的参数个数 = *参数 之前的那些；
       没有 *参数 时，**选项 之前的也是普通的。 */
    int npos = nparams;
    if (star_pos >= 0) npos = star_pos;
    else if (dstar_pos >= 0) npos = dstar_pos;

    /* 1) 位置实参：前 npos 个进普通参数（借用 → retain） */
    int ntake = nargs < npos ? nargs : npos;
    for (int i = 0; i < ntake; i++) {
        bound[i] = args[i];
        retain_val(vm, args[i]);
    }

    /* 2) 多出来的位置实参：有 *参数 就打包成列表，否则报参数过多 */
    if (nargs > npos) {
        if (star_pos < 0) {
            goto fail_argc;
        }
        JVal lst;
        if (vm->host->build_list(args + npos, nargs - npos, &lst,
                                 line, col) != 0)
            goto fail;
        bound[star_pos] = lst;      /* 新构造值，本体已持引用 */
    }

    /* 3) 关键字实参：先匹配普通参数名，剩下的进 **选项 */
    {
        int *extra_names = (int *)malloc(
            (size_t)(nkw > 0 ? nkw : 1) * sizeof(int));
        JVal *extra_vals = (JVal *)malloc(
            (size_t)(nkw > 0 ? nkw : 1) * sizeof(JVal));
        if (!extra_names || !extra_vals) {
            free(extra_names); free(extra_vals);
            goto fail;
        }
        int nextra = 0;

        for (int k = 0; k < nkw; k++) {
            int name = kwname_idx[k];
            int found = -1;
            for (int i = 0; i < npos; i++)
                if (pp[i * 4 + 0] == name) { found = i; break; }
            if (found >= 0) {
                bound[found] = kwvals[k];
                retain_val(vm, kwvals[k]);
            } else if (dstar_pos >= 0) {
                extra_names[nextra] = name;
                extra_vals[nextra] = kwvals[k];
                nextra++;
            } else {
                free(extra_names); free(extra_vals);
                vm->host->raise_bad_kw(code_idx, name, line, col);
                goto fail;
            }
        }

        if (dstar_pos >= 0) {
            JVal dct;
            if (vm->host->pack_kwargs(extra_names, extra_vals, nextra,
                                      &dct, line, col) != 0) {
                free(extra_names); free(extra_vals);
                goto fail;
            }
            bound[dstar_pos] = dct;
        }
        free(extra_names);
        free(extra_vals);
    }

    /* 4) 缺省参数补默认值（defaults 为紧凑尾部数组，借用 → retain，M7）。
       默认值只属于「普通参数」，*参数 / **选项 不参与。 */
    if (ndefaults > 0 && npos > 0) {
        int first_def = npos - ndefaults;
        for (int i = 0; i < npos; i++) {
            if (bound[i].tag == JS_TAG_UNSET && i >= first_def
                    && defaults[i - first_def].tag != JS_TAG_UNSET) {
                bound[i] = defaults[i - first_def];
                retain_val(vm, defaults[i - first_def]);
            }
        }
    }

    /* 5) *参数 / **选项 没拿到东西时给空容器（Python 语义：0 个也成立） */
    if (star_pos >= 0 && bound[star_pos].tag == JS_TAG_UNSET) {
        JVal lst;
        if (vm->host->build_list(args, 0, &lst, line, col) != 0) goto fail;
        bound[star_pos] = lst;
    }
    if (dstar_pos >= 0 && bound[dstar_pos].tag == JS_TAG_UNSET) {
        JVal dct;
        if (vm->host->pack_kwargs(NULL, NULL, 0, &dct, line, col) != 0)
            goto fail;
        bound[dstar_pos] = dct;
    }

    /* 6) 缺参检查（只看普通参数；*参数 / **选项 已保证有值） */
    {
        int miss[64]; int nmiss = 0;
        for (int i = 0; i < npos && nmiss < 64; i++)
            if (bound[i].tag == JS_TAG_UNSET) miss[nmiss++] = i;
        if (nmiss > 0) {
            vm->host->raise_missing(code_idx, miss, nmiss, line, col);
            goto fail;
        }
    }

    /* 7) 写局部槽与单元 */
    for (int i = 0; i < nparams; i++) {
        int local = pp[i * 4 + 1];
        int cell = pp[i * 4 + 2];
        if (local >= 0) {
            locals_out[local] = bound[i];
        } else if (cell >= 0) {
            /* cellvars 部分的单元由帧创建者分配 */
            free_cells[cell]->v = bound[i];
        }
    }
    free(bound);
    return 0;

fail_argc:
    vm->host->raise_argc(code_idx, nargs + nkw, line, col);
    /* fall through */
fail:
    /* 统一清理：已 retain 的（借用来源）在这里撤销，
       新构造的（列表/字典）在这里还回本体那一份。两类都只需 release 一次。 */
    for (int i = 0; i < nparams; i++)
        if (bound[i].tag != JS_TAG_UNSET) release_val(vm, bound[i]);
    free(bound);
    return -1;
}

/* ------------------------------------------------------------------ */
/* 主循环                                                               */
/* ------------------------------------------------------------------ */

#define INSN(f) (instrs[(size_t)(ip) * 6 + (f)])

/* 弹帧：清栈/局部/单元。RETURN 已把栈清到 base 并放好返回值的情形
 * 由 RETURN 自身处理，此处只做 locals/cells 的释放。 */
static void pop_frame(JsVm *vm, int full_stack_clear) {
    JsFrame *f = &vm->frames[vm->nframes - 1];
    if (full_stack_clear) {
        while (vm->sp > f->base)
            release_val(vm, vm->stack[--vm->sp]);
    }
    if (f->locals) {
        for (int i = 0; i < f->nlocals; i++)
            if (f->locals[i].tag != JS_TAG_UNSET)
                release_val(vm, f->locals[i]);
        free(f->locals);
        f->locals = NULL;
    }
    if (f->cells) {
        for (int i = 0; i < f->ncells; i++)
            release_cell(vm, f->cells[i]);
        free(f->cells);
        f->cells = NULL;
    }
    vm->nframes--;
}

/* 判断异常是否是「中断/继续」信号（穿透到循环） */
static int exc_is_signal(JsVm *vm, JVal *exc, int *out) {
    return vm->host->is_signal(exc, out);
}

/* 从宿主取 pending 异常对象（转成 HOST 句柄，C 侧持引用）。
 * 返回 0 成功。vm->pending_err 是该异常唯一的 C 侧引用。 */
static int take_pending_err(JsVm *vm, JVal *out) {
    if (vm->pending_err.tag == JS_TAG_UNSET) {
        /* 首次：向宿主要（error_handle 返回的句柄引用归 C 侧） */
        if (vm->host->error_handle(out) != 0) return -1;
        if (out->tag == JS_TAG_UNSET) return -1;
        vm->pending_err = *out;   /* 引用归 pending_err */
        return 0;
    }
    *out = vm->pending_err;
    return 0;
}

static void clear_pending_err(JsVm *vm) {
    if (vm->pending_err.tag != JS_TAG_UNSET) {
        release_val(vm, vm->pending_err);
        vm->pending_err = make_unset();
    }
}

/* 循环内的 try 里的中断/继续是否需要走异常分发（触发 finally）。
 * 只有「最近的处理器比最近的循环更内层」时才需要（与 vm.py 一致）。 */
static int signal_needs_dispatch(JsFrame *f) {
    if (!f->nhandlers) return 0;
    if (!f->nloops) return 1;
    return f->handlers[f->nhandlers - 1].seq > f->loops[f->nloops - 1].seq;
}

/* 异常分发：从当前帧向外找处理器（try）或（信号的）循环。
 * 找到 → 清栈、放好恢复点、异常对象压栈，返回 1（主循环 SYNC_FRAME 后继续）；
 * 找不到 → 帧弹净，返回 0（调用方把 pending 抛给用户）。 */
static int dispatch_exception(JsVm *vm, JVal *exc) {
    int sig = 0;
    if (exc_is_signal(vm, exc, &sig) != 0) return 0;
    while (vm->nframes > vm->stop_depth) {
        JsFrame *f = &vm->frames[vm->nframes - 1];
        JsHandler *h = f->nhandlers ? &f->handlers[f->nhandlers - 1] : NULL;
        JsLoop *lp = f->nloops ? &f->loops[f->nloops - 1] : NULL;
        if (sig) {
            /* 信号：循环与处理器谁更内层谁先接（与 vm.py 一致） */
            if (lp && (!h || lp->seq > h->seq)) {
                while (vm->sp > lp->depth)
                    release_val(vm, vm->stack[--vm->sp]);
                f->ip = sig == 1 ? lp->brk : lp->cont;
                clear_pending_err(vm);
                return 1;
            }
            if (h) {
                while (vm->sp > h->sp)
                    release_val(vm, vm->stack[--vm->sp]);
                retain_val(vm, *exc);
                PUSH(vm, *exc);
                f->ip = h->catch_ip;
                f->nhandlers--;
                clear_pending_err(vm);
                return 1;
            }
        } else if (h) {
            while (vm->sp > h->sp)
                release_val(vm, vm->stack[--vm->sp]);
            retain_val(vm, *exc);
            PUSH(vm, *exc);
            f->ip = h->catch_ip;
            f->nhandlers--;
            clear_pending_err(vm);
            return 1;
        }
        /* 无处理器：弹帧继续向外 */
        pop_frame(vm, 1);
    }
    return 0;
}


/* 数值快速路径：成功时填 *out 返回 1；需要回落宿主返回 0；
 * 已报错返回 -1 */
static int binop_fast(int op, JVal l, JVal r, JVal *out, JsVm *vm,
                      int line, int col) {
    int li = l.tag == JS_TAG_INT;
    int ri = r.tag == JS_TAG_INT;
    int lf = l.tag == JS_TAG_FLOAT;
    int rf = r.tag == JS_TAG_FLOAT;

    /* 整数 × 整数 */
    if (li && ri) {
        int64_t a = l.v.i, b = r.v.i, res;
        switch (op) {
        case JS_BIN_ADD:
            if (__builtin_add_overflow(a, b, &res)) return 0;
            *out = make_int(res); return 1;
        case JS_BIN_SUB:
            if (__builtin_sub_overflow(a, b, &res)) return 0;
            *out = make_int(res); return 1;
        case JS_BIN_MUL:
            if (__builtin_mul_overflow(a, b, &res)) return 0;
            *out = make_int(res); return 1;
        case JS_BIN_DIV:                       /* 真除法 → float */
            if (b == 0) {
                vm->host->raise_zero_div(line, col);
                return -1;
            }
            *out = make_float((double)a / (double)b); return 1;
        case JS_BIN_FLOORDIV:                  /* Python 向下取整 */
            if (b == 0) {
                vm->host->raise_zero_div(line, col);
                return -1;
            }
            res = a / b;
            if ((a % b != 0) && ((a < 0) != (b < 0))) res -= 1;
            *out = make_int(res); return 1;
        case JS_BIN_MOD:                       /* 符号跟除数 */
            if (b == 0) {
                vm->host->raise_zero_div(line, col);
                return -1;
            }
            res = a % b;
            if (res != 0 && ((res < 0) != (b < 0))) res += b;
            *out = make_int(res); return 1;
        case JS_BIN_POW:
            return 0;                          /* 语义复杂，回落宿主 */
        default: return 0;
        }
    }
    /* 浮点（含混合）的 + - * / */
    if ((li || lf) && (ri || rf)) {
        double a = li ? (double)l.v.i : l.v.d;
        double b = ri ? (double)r.v.i : r.v.d;
        switch (op) {
        case JS_BIN_ADD: *out = make_float(a + b); return 1;
        case JS_BIN_SUB: *out = make_float(a - b); return 1;
        case JS_BIN_MUL: *out = make_float(a * b); return 1;
        case JS_BIN_DIV: *out = make_float(a / b); return 1;  /* inf/nan 同 Python */
        default: return 0;                      /* // % ** 回落宿主 */
        }
    }
    return 0;
}

static int compare_fast(int op, JVal l, JVal r, JVal *out) {
    int li = l.tag == JS_TAG_INT, ri = r.tag == JS_TAG_INT;
    int lf = l.tag == JS_TAG_FLOAT, rf = r.tag == JS_TAG_FLOAT;
    if (!((li || lf) && (ri || rf))) return 0;
    /* 混合比较用 double 会丢大整数精度 → INT-INT 走整型，
       其余转 double（与 CPython 的 int/float 比较有细微差别，
       但 CPython 内部也是精确比较；超大整数与浮点比较罕见，先接受） */
    if (li && ri) {
        int64_t a = l.v.i, b = r.v.i;
        switch (op) {
        case JS_CMP_EQ: *out = make_bool(a == b); return 1;
        case JS_CMP_NE: *out = make_bool(a != b); return 1;
        case JS_CMP_LT: *out = make_bool(a <  b); return 1;
        case JS_CMP_GT: *out = make_bool(a >  b); return 1;
        case JS_CMP_LE: *out = make_bool(a <= b); return 1;
        case JS_CMP_GE: *out = make_bool(a >= b); return 1;
        }
    } else {
        double a = li ? (double)l.v.i : l.v.d;
        double b = ri ? (double)r.v.i : r.v.d;
        switch (op) {
        case JS_CMP_EQ: *out = make_bool(a == b); return 1;
        case JS_CMP_NE: *out = make_bool(a != b); return 1;
        case JS_CMP_LT: *out = make_bool(a <  b); return 1;
        case JS_CMP_GT: *out = make_bool(a >  b); return 1;
        case JS_CMP_LE: *out = make_bool(a <= b); return 1;
        case JS_CMP_GE: *out = make_bool(a >= b); return 1;
        }
    }
    return 0;
}

static int truthy_fast(JVal v, JsVm *vm, int *out) {
    switch (v.tag) {
    case JS_TAG_TRUE:  *out = 1; return 1;
    case JS_TAG_FALSE: case JS_TAG_NULL: case JS_TAG_UNSET: *out = 0; return 1;
    case JS_TAG_INT:   *out = v.v.i != 0; return 1;
    case JS_TAG_FLOAT: *out = v.v.d != 0.0; return 1;
    default:
        return vm->host->truthy(&v, out, 0, 0) == 0 ? 1 : -1;
    }
}

/* ------------------------------------------------------------------ */
/* 执行引擎                                                             */
/* ------------------------------------------------------------------ */

static int run_loop(JsVm *vm) {
    while (vm->nframes > 0) {
        JsFrame *f = &vm->frames[vm->nframes - 1];
        JsCode *code = &vm->codes[f->code_idx];
        int32_t *instrs = vm->instrs + (size_t)code->instr_off * 6;
        int ip = f->ip;

#define SAVE_IP() do { f->ip = ip; } while (0)
#define SYNC_FRAME() do { SAVE_IP(); f = &vm->frames[vm->nframes-1]; \
        code = &vm->codes[f->code_idx]; \
        instrs = vm->instrs + (size_t)code->instr_off * 6; ip = f->ip; } while (0)

        for (;;) {
            int op = INSN(0);
            int a = INSN(1), b = INSN(2), c = INSN(3);
            int line = INSN(4), col = INSN(5);
            ip++;

            switch (op) {
            case JS_OP_LOAD_CONST:
                /* 常量槽是共享的：压栈前必须 retain（HOST/FUNC/CELL） */
                retain_val(vm, vm->consts[a]);
                PUSH(vm, vm->consts[a]);
                break;

            case JS_OP_LOAD_FAST: {
                JVal v = f->locals[a];
                if (v.tag == JS_TAG_UNSET) {
                    SAVE_IP();
                    vm->host->raise_unbound_local(f->code_idx, a, line, col);
                    goto dispatch_err;
                }
                retain_val(vm, v);
                PUSH(vm, v);
                break;
            }

            case JS_OP_STORE_FAST: {
                JVal v = POP(vm);
                if (f->locals[a].tag != JS_TAG_UNSET)
                    release_val(vm, f->locals[a]);
                f->locals[a] = v;
                break;
            }

            case JS_OP_LOAD_GLOBAL: {
                JVal v = vm->globals[a];
                if (v.tag == JS_TAG_UNSET) {
                    SAVE_IP();
                    vm->host->raise_name(a, line, col);
                    goto dispatch_err;
                }
                retain_val(vm, v);
                PUSH(vm, v);
                break;
            }

            case JS_OP_STORE_GLOBAL: {
                JVal v = POP(vm);
                if (vm->globals[a].tag != JS_TAG_UNSET)
                    release_val(vm, vm->globals[a]);
                vm->globals[a] = v;
                break;
            }

            case JS_OP_LOAD_DEREF: {
                JsCell *cell = f->cells[a];
                if (cell->v.tag == JS_TAG_UNSET) {
                    SAVE_IP();
                    vm->host->raise_unbound_cell(f->code_idx, a, line, col);
                    goto dispatch_err;
                }
                retain_val(vm, cell->v);
                PUSH(vm, cell->v);
                break;
            }

            case JS_OP_STORE_DEREF: {
                JVal v = POP(vm);
                JsCell *cell = f->cells[a];
                if (cell->v.tag != JS_TAG_UNSET)
                    release_val(vm, cell->v);
                cell->v = v;
                break;
            }

            case JS_OP_LOAD_CELL: {
                JVal cv = make_cell(f->cells[a]);
                retain_cell(f->cells[a]);
                PUSH(vm, cv);
                break;
            }

            case JS_OP_POP_TOP: {
                JVal v = POP(vm);
                release_val(vm, v);
                break;
            }

            case JS_OP_DUP_TOP: {
                JVal t = vm->stack[vm->sp - 1];
                retain_val(vm, t);
                PUSH(vm, t);
                break;
            }

            case JS_OP_DUP_TWO: {
                /* [...,x,y] → [...,x,y,x,y] */
                JVal y = vm->stack[vm->sp - 1];
                JVal x = vm->stack[vm->sp - 2];
                retain_val(vm, x);
                retain_val(vm, y);
                PUSH(vm, x);
                PUSH(vm, y);
                break;
            }

            case JS_OP_ROT_TWO: {
                JVal t = vm->stack[vm->sp - 1];
                vm->stack[vm->sp - 1] = vm->stack[vm->sp - 2];
                vm->stack[vm->sp - 2] = t;
                break;
            }

            case JS_OP_ROT_THREE: {
                /* a b c → c a b（与 vm.py 一致） */
                JVal c3 = vm->stack[vm->sp - 1];
                vm->stack[vm->sp - 1] = vm->stack[vm->sp - 2];
                vm->stack[vm->sp - 2] = vm->stack[vm->sp - 3];
                vm->stack[vm->sp - 3] = c3;
                break;
            }

            case JS_OP_BIN_OP: {
                JVal r = POP(vm), l = POP(vm), out;
                int fast = binop_fast(a, l, r, &out, vm, line, col);
                if (fast < 0) { release_val(vm, l); release_val(vm, r); goto dispatch_err; }
                if (fast) {
                    release_val(vm, l); release_val(vm, r);
                    PUSH(vm, out);
                } else {
                    SAVE_IP();
                    if (vm->host->binop(a, &l, &r, &out, line, col) != 0) {
                        release_val(vm, l); release_val(vm, r);
                        goto dispatch_err;
                    }
                    release_val(vm, l); release_val(vm, r);
                    PUSH(vm, out);
                }
                break;
            }

            case JS_OP_UNARY_OP: {
                JVal v = POP(vm), out;
                int done = 0;
                if (a == JS_UN_NEG && v.tag == JS_TAG_INT) {
                    if (v.v.i != INT64_MIN) {
                        out = make_int(-v.v.i); done = 1;
                    }
                } else if (a == JS_UN_NEG && v.tag == JS_TAG_FLOAT) {
                    out = make_float(-v.v.d); done = 1;
                } else if (a == JS_UN_NOT) {
                    int t;
                    int ok = truthy_fast(v, vm, &t);
                    if (ok < 0) { release_val(vm, v); goto dispatch_err; }
                    out = make_bool(!t); done = 1;
                }
                if (done) {
                    release_val(vm, v);
                    PUSH(vm, out);
                } else {
                    SAVE_IP();
                    if (vm->host->unary(a, &v, &out, line, col) != 0) {
                        release_val(vm, v); goto dispatch_err;
                    }
                    release_val(vm, v);
                    PUSH(vm, out);
                }
                break;
            }

            case JS_OP_COMPARE: {
                JVal r = POP(vm), l = POP(vm), out;
                if (compare_fast(a, l, r, &out)) {
                    release_val(vm, l); release_val(vm, r);
                    PUSH(vm, out);
                } else {
                    SAVE_IP();
                    if (vm->host->compare(a, &l, &r, &out, line, col) != 0) {
                        release_val(vm, l); release_val(vm, r);
                        goto dispatch_err;
                    }
                    release_val(vm, l); release_val(vm, r);
                    PUSH(vm, out);
                }
                break;
            }

            case JS_OP_JUMP:
                ip = a;
                break;

            case JS_OP_POP_JUMP_IF_FALSE:
            case JS_OP_POP_JUMP_IF_TRUE: {
                JVal v = POP(vm);
                int t;
                if (truthy_fast(v, vm, &t) < 0) {
                    release_val(vm, v); goto dispatch_err;
                }
                release_val(vm, v);
                if (op == JS_OP_POP_JUMP_IF_FALSE ? !t : t) ip = a;
                break;
            }

            case JS_OP_JUMP_IF_FALSE_OR_POP: {
                JVal v = vm->stack[vm->sp - 1];
                int t;
                if (truthy_fast(v, vm, &t) < 0) goto dispatch_err;
                if (t) {
                    release_val(vm, POP(vm));
                } else {
                    ip = a;
                }
                break;
            }

            case JS_OP_CALL: {
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
                    JsCode *sub = &vm->codes[jfn->code_idx];
                    /* 参数从栈上取出（转移所有权） */
                    JVal *args = (JVal *)malloc(
                        (size_t)(nargs > 0 ? nargs : 1) * sizeof(JVal));
                    JVal *kwv = (JVal *)malloc(
                        (size_t)(nkw > 0 ? nkw : 1) * sizeof(JVal));
                    if (!args || !kwv) { free(args); free(kwv);
                        vm->host->raise_internal("内存不足"); return -1; }
                    for (int i = 0; i < nargs; i++)
                        args[i] = vm->stack[fn_base + 1 + i];
                    for (int k = 0; k < nkw; k++)
                        kwv[k] = vm->stack[fn_base + 1 + nargs + k];

                    JVal *locals = (JVal *)malloc(
                        (size_t)(sub->nlocals > 0 ? sub->nlocals : 1)
                        * sizeof(JVal));
                    if (!locals) { free(args); free(kwv);
                        vm->host->raise_internal("内存不足"); return -1; }
                    for (int i = 0; i < sub->nlocals; i++)
                        locals[i] = make_unset();

                    if (vm->nframes >= JS_MAX_FRAMES) {
                        SAVE_IP();
                        vm->host->raise_recursion(line, col);
                        /* 栈上的 fn/args 已被取出，需释放（先释放值再释放数组） */
                        for (int i = 0; i < nargs; i++)
                            release_val(vm, args[i]);
                        for (int k = 0; k < nkw; k++)
                            release_val(vm, kwv[k]);
                        release_val(vm, fn);
                        free(args); free(kwv); free(locals);
                        vm->sp = fn_base;
                        return -1;
                    }

                    /* 帧单元：cellvars 新建；freevars 借用函数对象的 */
                    int ncellvars = sub->ncellvars;
                    int nfreevars = jfn->ncells;
                    JsCell **cells = (JsCell **)malloc(
                        (size_t)(ncellvars + nfreevars > 0
                                 ? ncellvars + nfreevars : 1)
                        * sizeof(JsCell *));
                    if (!cells) { free(args); free(kwv); free(locals);
                        vm->host->raise_internal("内存不足"); return -1; }
                    for (int i = 0; i < ncellvars; i++) {
                        cells[i] = (JsCell *)malloc(sizeof(JsCell));
                        if (!cells[i]) { vm->host->raise_internal("内存不足"); return -1; }
                        cells[i]->v = make_unset();
                        cells[i]->refcnt = 1;   /* 帧持有 */
                    }
                    for (int i = 0; i < nfreevars; i++) {
                        cells[ncellvars + i] = jfn->cells[i];
                        retain_cell(jfn->cells[i]);
                    }

                    SAVE_IP();
                    if (bind_args(vm, jfn->code_idx, args, nargs,
                                  knames, kwv, nkw, locals,
                                  cells, ncellvars + nfreevars,
                                  jfn->defaults, jfn->ndefaults,
                                  line, col) != 0) {
                        for (int i = 0; i < nargs; i++)
                            release_val(vm, args[i]);
                        for (int k = 0; k < nkw; k++)
                            release_val(vm, kwv[k]);
                        release_val(vm, fn);
                        free(args); free(kwv); free(locals);
                        for (int i = 0; i < ncellvars + nfreevars; i++)
                            release_cell(vm, cells[i]);
                        free(cells);
                        vm->sp = fn_base;
                        return -1;
                    }
                    /* bind_args 采用**借用**语义（M25）：槽位里那份已由它
                       retain，这里释放实参自身的引用，再清栈。 */
                    for (int i = 0; i < nargs; i++)
                        release_val(vm, args[i]);
                    for (int k = 0; k < nkw; k++)
                        release_val(vm, kwv[k]);
                    free(args); free(kwv);

                    /* 清掉栈上的 fn+args+kw（它们的引用已在上一步释放）。
                     * 注意：release_val(fn) 可能 free 掉函数对象（jfn），
                     * 因此必须先保存 code_idx 再释放，避免 use-after-free。 */
                    int fn_code_idx = jfn->code_idx;
                    vm->sp = fn_base;
                    release_val(vm, fn);

                    push_frame(vm, fn_code_idx, fn_base, locals,
                               sub->nlocals);
                    {
                        JsFrame *nf = &vm->frames[vm->nframes - 1];
                        nf->cells = cells;
                        nf->ncells = ncellvars + nfreevars;
                    }
                    SYNC_FRAME();
                    break;   /* 进入新帧 */
                }

                /* 宿主函数：内建/标准库/Python 生态 */
                {
                    JVal out;
                    SAVE_IP();
                    /* 回调借用栈上的参数 */
                    if (vm->host->call(&fn,
                                       &vm->stack[fn_base + 1], nargs,
                                       knames,
                                       &vm->stack[fn_base + 1 + nargs], nkw,
                                       &out, line, col) != 0) {
                        /* 出错：清掉整个调用段 */
                        while (vm->sp > fn_base)
                            release_val(vm, vm->stack[--vm->sp]);
                        goto dispatch_err;
                    }
                    while (vm->sp > fn_base)
                        release_val(vm, vm->stack[--vm->sp]);
                    PUSH(vm, out);
                }
                break;
            }

            case JS_OP_RETURN: {
                JVal value = POP(vm);
                /* try 块内的返回：先找最近的 finally（挂起返回值） */
                if (f->nhandlers > 0) {
                    JsHandler *h = &f->handlers[f->nhandlers - 1];
                    if (h->finally_ip >= 0) {
                        int hsp = h->sp;
                        while (vm->sp > hsp)
                            release_val(vm, vm->stack[--vm->sp]);
                        f->pending_return = 1;
                        f->ret_val = value;
                        f->nhandlers--;   /* 弹出该 handler */
                        ip = h->finally_ip;
                        break;
                    }
                }
                int base = f->base;
                while (vm->sp > base)
                    release_val(vm, vm->stack[--vm->sp]);
                pop_frame(vm, 0);   /* 栈已清，只释放 locals/cells */
                PUSH(vm, value);
                if (vm->nframes <= vm->stop_depth) return 0;
                SYNC_FRAME();
                break;
            }

            case JS_OP_BUILD_CLASS: {
                int nmethods = a;
                int has_base = b;

                JVal name = POP(vm);
                JVal *methods = &vm->stack[vm->sp - nmethods];
                JVal base = make_unset();
                if (has_base) {
                    base = vm->stack[vm->sp - nmethods - 1];
                }
                JVal out;
                SAVE_IP();
                if (vm->host->make_class(&name, &base, methods, nmethods,
                                         &out, line, col) != 0) {
                    release_val(vm, name);
                    goto dispatch_err;
                }
                /* 清掉方法/基类/类名 */
                int pop_n = nmethods + (has_base ? 1 : 0);
                while (pop_n-- > 0)
                    release_val(vm, POP(vm));
                release_val(vm, name);
                PUSH(vm, out);
                break;
            }

            case JS_OP_MAKE_FUNCTION: {
                int ncells = b;
                int ndefaults = c;
                JsFunc *jfn = (JsFunc *)malloc(sizeof *jfn);
                if (!jfn) { vm->host->raise_internal("内存不足"); return -1; }
                jfn->code_idx = a;
                jfn->ncells = ncells;
                jfn->ndefaults = ndefaults;
                /* 默认值在栈顶（后压），先弹出（所有权转移，M7） */
                jfn->defaults = (JVal *)malloc(
                    (size_t)(ndefaults > 0 ? ndefaults : 1) * sizeof(JVal));
                if (!jfn->defaults) { free(jfn);
                    vm->host->raise_internal("内存不足"); return -1; }
                for (int i = 0; i < ndefaults; i++)
                    jfn->defaults[i] = vm->stack[vm->sp - ndefaults + i];
                vm->sp -= ndefaults;
                jfn->cells = (JsCell **)malloc(
                    (size_t)(ncells > 0 ? ncells : 1) * sizeof(JsCell *));
                if (!jfn->cells) { free(jfn->defaults); free(jfn);
                    vm->host->raise_internal("内存不足"); return -1; }
                for (int i = 0; i < ncells; i++) {
                    JVal cv = vm->stack[vm->sp - ncells + i];
                    jfn->cells[i] = (JsCell *)(uintptr_t)cv.v.u;
                }
                vm->sp -= ncells;   /* 所有权转移（LOAD_CELL 时已 retain） */
                jfn->refcnt = 1;
                PUSH(vm, make_func(jfn));
                break;
            }

            case JS_OP_UNPACK: {
                int n = a;
                int star = b;   /* 带星号解包的元素位置（M25）；-1 = 无 */
                JVal v = POP(vm);
                JVal *items = (JVal *)malloc(
                    (size_t)(n > 0 ? n : 1) * sizeof(JVal));
                if (!items) { release_val(vm, v);
                    vm->host->raise_internal("内存不足"); return -1; }
                SAVE_IP();
                if (vm->host->unpack(&v, n, star, items, line, col) != 0) {
                    release_val(vm, v); free(items);
                    goto dispatch_err;
                }
                release_val(vm, v);
                /* 第一个元素最后压 -> 栈顶（与 vm.py 一致） */
                for (int i = n - 1; i >= 0; i--) {
                    PUSH(vm, items[i]);
                }
                free(items);
                break;
            }

            case JS_OP_LIST_APPEND: {
                JVal val = POP(vm);
                JVal obj = POP(vm);
                SAVE_IP();
                if (vm->host->list_append(&obj, &val, line, col) != 0) {
                    release_val(vm, obj); release_val(vm, val);
                    goto dispatch_err;
                }
                release_val(vm, obj); release_val(vm, val);
                PUSH(vm, make_null());   /* 追加返回空，保持 CALL 栈语义 */
                break;
            }

            case JS_OP_BUILD_SLICE: {
                /* M18：按 a 的位标志（bit0=start,bit1=stop,bit2=step）从栈上
                 * 取分量，缺省位置补 UNSET，交给宿主 make_slice 构造 slice。 */
                JVal start = jsvm_unset(), stop = jsvm_unset(),
                     step = jsvm_unset(), out;
                if (a & 4) step = POP(vm);
                if (a & 2) stop = POP(vm);
                if (a & 1) start = POP(vm);
                SAVE_IP();
                if (vm->host->make_slice(&start, &stop, &step, &out,
                                         line, col) != 0) {
                    /* 出错：释放已取出的分量（UNSET 的 release 是 no-op） */
                    release_val(vm, start); release_val(vm, stop);
                    release_val(vm, step);
                    goto dispatch_err;
                }
                release_val(vm, start); release_val(vm, stop);
                release_val(vm, step);
                PUSH(vm, out);
                break;
            }

            case JS_OP_BUILD_LIST: {
                JVal out;
                SAVE_IP();
                if (vm->host->build_list(&vm->stack[vm->sp - a], a,
                                         &out, line, col) != 0)
                    goto dispatch_err;
                vm->sp -= a;
                for (int i = 0; i < a; i++)
                    release_val(vm, vm->stack[vm->sp + i]);
                PUSH(vm, out);
                break;
            }

            case JS_OP_BUILD_DICT: {
                JVal out;
                SAVE_IP();
                if (vm->host->build_dict(&vm->stack[vm->sp - 2 * a], a,
                                         &out, line, col) != 0)
                    goto dispatch_err;
                int n = 2 * a;
                vm->sp -= n;
                for (int i = 0; i < n; i++)
                    release_val(vm, vm->stack[vm->sp + i]);
                PUSH(vm, out);
                break;
            }

            case JS_OP_GET_ATTR: {
                JVal obj = POP(vm), out;
                SAVE_IP();
                if (vm->host->getattr(&obj, a, &out, line, col) != 0) {
                    release_val(vm, obj); goto dispatch_err;
                }
                release_val(vm, obj);
                PUSH(vm, out);
                break;
            }

            case JS_OP_SET_ATTR: {
                JVal val = POP(vm), obj = POP(vm);
                SAVE_IP();
                if (vm->host->setattr(&obj, a, &val, line, col) != 0) {
                    release_val(vm, obj); release_val(vm, val); goto dispatch_err;
                }
                release_val(vm, obj); release_val(vm, val);
                break;
            }

            case JS_OP_GET_ITEM: {
                JVal idx = POP(vm), obj = POP(vm), out;
                SAVE_IP();
                if (vm->host->getitem(&obj, &idx, &out, line, col) != 0) {
                    release_val(vm, obj); release_val(vm, idx); goto dispatch_err;
                }
                release_val(vm, obj); release_val(vm, idx);
                PUSH(vm, out);
                break;
            }

            case JS_OP_SET_ITEM: {
                JVal val = POP(vm), idx = POP(vm), obj = POP(vm);
                SAVE_IP();
                if (vm->host->setitem(&obj, &idx, &val, line, col) != 0) {
                    release_val(vm, obj); release_val(vm, idx);
                    release_val(vm, val); goto dispatch_err;
                }
                release_val(vm, obj); release_val(vm, idx);
                release_val(vm, val);
                break;
            }

            case JS_OP_GET_ITER: {
                JVal obj = POP(vm), out;
                int batchable = 0;
                SAVE_IP();
                if (vm->host->iter_new(&obj, &out, &batchable,
                                       line, col) != 0) {
                    release_val(vm, obj); goto dispatch_err;
                }
                release_val(vm, obj);
                /* out 是宿主句柄（引用归新迭代器所有），包一层 C 侧缓冲 */
                JsIterC *it = (JsIterC *)malloc(sizeof *it);
                if (!it) {
                    vm->host->release_handle((uint32_t)out.v.u);
                    vm->host->raise_internal("内存不足"); return -1;
                }
                memset(it, 0, sizeof *it);
                it->refcnt = 1;
                it->host_id = (uint32_t)out.v.u;
                it->batchable = batchable;
                PUSH(vm, make_iter(it));
                break;
            }

            case JS_OP_FOR_ITER: {
                JVal itv = vm->stack[vm->sp - 1];   /* 借用 */
                if (itv.tag == JS_TAG_ITER) {
                    JsIterC *it = (JsIterC *)(uintptr_t)itv.v.u;
                    if (it->pos < it->nbuf) {           /* 缓冲命中：零回调 */
                        JVal v = it->buf[it->pos++];
                        retain_val(vm, v);
                        PUSH(vm, v);
                        break;
                    }
                    if (it->exhausted) {
                        release_val(vm, POP(vm));
                        ip = a;
                        break;
                    }
                    SAVE_IP();
                    int n = 0;
                    JVal hj; memset(&hj, 0, sizeof hj);
                    hj.tag = JS_TAG_HOST; hj.v.u = it->host_id;
                    if (it->batchable) {
                        if (vm->host->iter_next_batch(
                                &hj, it->buf, JS_ITER_BUF, &n,
                                line, col) != 0)
                            goto dispatch_err;
                    } else {
                        JVal out; int done = 0;
                        if (vm->host->iter_next(&hj, &out, &done,
                                                line, col) != 0)
                            goto dispatch_err;
                        if (!done) { it->buf[0] = out; n = 1; }
                    }
                    if (n == 0) {
                        it->exhausted = 1;
                        release_val(vm, POP(vm));
                        ip = a;
                        break;
                    }
                    it->nbuf = n; it->pos = 1;
                    retain_val(vm, it->buf[0]);
                    PUSH(vm, it->buf[0]);
                    break;
                }
                /* 防御路径：裸 HOST 迭代器（正常不会出现） */
                JVal out;
                int done = 0;
                SAVE_IP();
                if (vm->host->iter_next(&itv, &out, &done, line, col) != 0)
                    goto dispatch_err;
                if (done) {
                    release_val(vm, POP(vm));
                    ip = a;
                } else {
                    PUSH(vm, out);
                }
                break;
            }

            case JS_OP_SETUP_LOOP: {
                if (f->nloops >= JS_MAX_LOOPS) {
                    vm->host->raise_internal("循环嵌套太深");
                    return -1;
                }
                f->block_seq++;
                JsLoop *lp = &f->loops[f->nloops++];
                lp->depth = vm->sp;
                lp->cont = a;
                lp->brk = b;
                lp->seq = f->block_seq;
                break;
            }

            case JS_OP_POP_BLOCK:
                if (f->nloops > 0) f->nloops--;
                break;

            case JS_OP_BREAK_LOOP: {
                SAVE_IP();
                if (f->nloops == 0 || signal_needs_dispatch(f)) {
                    /* 循环外的中断 / 循环内 try 里的中断：走异常分发，
                     * 触发 finally，最终仍由循环接住（或冒给用户） */
                    vm->host->raise_break(line, col);
                    goto dispatch_err;
                }
                JsLoop *lp = &f->loops[f->nloops - 1];
                while (vm->sp > lp->depth)
                    release_val(vm, vm->stack[--vm->sp]);
                ip = lp->brk;
                break;
            }

            case JS_OP_CONTINUE_LOOP: {
                SAVE_IP();
                if (f->nloops == 0 || signal_needs_dispatch(f)) {
                    vm->host->raise_continue(line, col);
                    goto dispatch_err;
                }
                JsLoop *lp = &f->loops[f->nloops - 1];
                while (vm->sp > lp->depth)
                    release_val(vm, vm->stack[--vm->sp]);
                ip = lp->cont;
                break;
            }

            case JS_OP_LOOP_SETUP: {
                JVal n = POP(vm), out;
                if (n.tag == JS_TAG_INT) {
                    /* 快路径 */
                } else {
                    SAVE_IP();
                    if (vm->host->to_int(&n, &out, line, col) != 0) {
                        release_val(vm, n); goto dispatch_err;
                    }
                    release_val(vm, n);
                    n = out;
                }
                if (f->locals[a].tag != JS_TAG_UNSET)
                    release_val(vm, f->locals[a]);
                f->locals[a] = n;
                break;
            }

            case JS_OP_IMPORT: {
                JVal out;
                SAVE_IP();
                if (vm->host->do_import(a, b, &out, line, col) != 0) goto dispatch_err;
                PUSH(vm, out);
                break;
            }

            case JS_OP_STORE_LAST: {
                JVal v = POP(vm);
                if (vm->last_value.tag != JS_TAG_UNSET)
                    release_val(vm, vm->last_value);
                vm->last_value = v;
                break;
            }

            /* === 异常处理（M5a） === */
            case JS_OP_SETUP_TRY: {
                if (f->nhandlers >= JS_MAX_HANDLERS) {
                    vm->host->raise_internal("异常嵌套太深");
                    goto dispatch_err;
                }
                f->block_seq++;
                JsHandler *h = &f->handlers[f->nhandlers++];
                h->catch_ip = a;
                h->finally_ip = b;
                h->sp = vm->sp;
                h->seq = f->block_seq;
                break;
            }

            case JS_OP_POP_TRY:
                if (f->nhandlers > 0) f->nhandlers--;
                break;

            case JS_OP_THROW: {
                JVal v = POP(vm), exc;
                SAVE_IP();
                /* 让宿主把值规范化成异常对象（make_exception） */
                if (vm->host->make_error(&v, line, col, &exc) != 0) {
                    release_val(vm, v);
                    goto dispatch_err;
                }
                release_val(vm, v);   /* 值所有权已转移给 exc */
                clear_pending_err(vm);
                retain_val(vm, exc);
                vm->pending_err = exc;
                goto dispatch_err;
            }

            case JS_OP_CHECK_SIGNAL: {
                JVal *top = &vm->stack[vm->sp - 1];
                int sig = 0;
                if (exc_is_signal(vm, top, &sig) != 0) goto dispatch_err;
                if (sig) ip = a;
                break;
            }

            case JS_OP_MATCH_EXC: {
                JVal cond = POP(vm);
                JVal *err = &vm->stack[vm->sp - 1];
                int matched = 0;
                if (vm->host->match_error(err, &cond, &matched,
                                          line, col) != 0) {
                    release_val(vm, cond);
                    goto dispatch_err;
                }
                release_val(vm, cond);
                PUSH(vm, make_bool(matched));
                break;
            }

            case JS_OP_END_FINALLY: {
                if (f->pending_return) {
                    /* finally 执行完，继续挂起的返回流程 */
                    f->pending_return = 0;
                    JVal value = f->ret_val;
                    int base = f->base;
                    while (vm->sp > base)
                        release_val(vm, vm->stack[--vm->sp]);
                    pop_frame(vm, 0);
                    PUSH(vm, value);
                    if (vm->nframes <= vm->stop_depth) return 0;
                    SYNC_FRAME();
                    break;
                }
                /* 无挂起返回：fall through */
                break;
            }

            case JS_OP_HALT: {
                pop_frame(vm, 1);
                vm->halted = 1;
                if (vm->nframes <= vm->stop_depth) return 0;
                SYNC_FRAME();
                break;
            }

            default: {
                char buf[64];
                snprintf(buf, sizeof buf, "未知指令 %d", op);
                vm->host->raise_internal(buf);
                goto dispatch_err;
            }
            }
            continue;

dispatch_err: {
            /* 宿主回调出错：取 pending 异常对象，走结构化分发 */
            JVal exc;
            if (take_pending_err(vm, &exc) != 0) return -1;
            if (dispatch_exception(vm, &exc)) {
                /* 分发成功：帧/ip 已由 dispatch_exception 改好，
                 * 只需重新同步局部（不做 SAVE_IP，避免覆盖新 ip） */
                f = &vm->frames[vm->nframes-1];
                code = &vm->codes[f->code_idx];
                instrs = vm->instrs + (size_t)code->instr_off * 6;
                ip = f->ip;
                continue;
            }
            /* 无人接住：异常对象留在 pending_err，返回错误状态 */
            return -1;
        }
        }
#undef SAVE_IP
#undef SYNC_FRAME
    }
    return 0;
}

/* ------------------------------------------------------------------ */
/* 对外 API                                                             */
/* ------------------------------------------------------------------ */

JSVM_API int jsvm_run(JsVm *vm) {
    if (vm->halted) return 0;
    vm->stop_depth = 0;
    JsCode *main = &vm->codes[vm->main_idx];
    JVal *locals = (JVal *)malloc(
        (size_t)(main->nlocals > 0 ? main->nlocals : 1) * sizeof(JVal));
    if (!locals) return -1;
    for (int i = 0; i < main->nlocals; i++) locals[i] = make_unset();

    int ncellvars = main->ncellvars;
    JsCell **cells = NULL;
    if (ncellvars > 0) {
        cells = (JsCell **)malloc(
            (size_t)ncellvars * sizeof(JsCell *));
        if (!cells) { free(locals); return -1; }
        for (int i = 0; i < ncellvars; i++) {
            cells[i] = (JsCell *)malloc(sizeof(JsCell));
            if (!cells[i]) { free(locals); return -1; }
            cells[i]->v = make_unset();
            cells[i]->refcnt = 1;
        }
    }
    if (push_frame(vm, vm->main_idx, 0, locals, main->nlocals) != 0) {
        free(locals);
        for (int i = 0; i < ncellvars; i++) free(cells[i]);
        free(cells);
        return -1;
    }
    JsFrame *f = &vm->frames[vm->nframes - 1];
    f->cells = cells;
    f->ncells = ncellvars;
    return run_loop(vm);
}

JSVM_API void jsvm_get_last(JsVm *vm, JVal *out) { *out = vm->last_value; }

/* 基石函数回流：Python 侧持有/释放 */
JSVM_API int jsvm_func_retain(JsVm *vm, JVal *fv) {
    (void)vm;
    if (fv->tag != JS_TAG_FUNC) return -1;
    retain_func((JsFunc *)(uintptr_t)fv->v.u);
    return 0;
}

JSVM_API int jsvm_func_release(JsVm *vm, JVal *fv) {
    (void)vm;
    if (fv->tag != JS_TAG_FUNC) return -1;
    release_func(vm, (JsFunc *)(uintptr_t)fv->v.u);
    return 0;
}

/* Python 高阶函数回调基石函数（重入）。参数为借用；
 * 出错时宿主已存 pending。 */
JSVM_API int jsvm_call_function(JsVm *vm, JVal *fv,
                       JVal *args, int nargs,
                       const int *kwname_idx, JVal *kwvals, int nkw,
                       JVal *out) {
    if (fv->tag != JS_TAG_FUNC) return -1;
    JsFunc *jfn = (JsFunc *)(uintptr_t)fv->v.u;
    JsCode *sub = &vm->codes[jfn->code_idx];

    /* 借用参数 → 复制并 retain（bind 后再统一 release） */
    JVal *cargs = (JVal *)malloc(
        (size_t)(nargs > 0 ? nargs : 1) * sizeof(JVal));
    JVal *ckwv = (JVal *)malloc(
        (size_t)(nkw > 0 ? nkw : 1) * sizeof(JVal));
    if (!cargs || !ckwv) { free(cargs); free(ckwv); return -1; }
    for (int i = 0; i < nargs; i++) { cargs[i] = args[i]; retain_val(vm, cargs[i]); }
    for (int k = 0; k < nkw; k++) { ckwv[k] = kwvals[k]; retain_val(vm, ckwv[k]); }

    JVal *locals = (JVal *)malloc(
        (size_t)(sub->nlocals > 0 ? sub->nlocals : 1) * sizeof(JVal));
    if (!locals) { free(cargs); free(ckwv); return -1; }
    for (int i = 0; i < sub->nlocals; i++) locals[i] = make_unset();

    /* 帧边界：内层 run_loop 只跑到本函数那一层 */
    int saved_depth = vm->stop_depth;
    vm->stop_depth = vm->nframes;

    if (vm->nframes >= JS_MAX_FRAMES) {
        vm->host->raise_recursion(0, 0);
        goto fail;
    }

    int ncellvars = sub->ncellvars;
    int nfreevars = jfn->ncells;
    JsCell **cells = (JsCell **)malloc(
        (size_t)(ncellvars + nfreevars > 0
                 ? ncellvars + nfreevars : 1) * sizeof(JsCell *));
    if (!cells) goto fail;
    for (int i = 0; i < ncellvars; i++) {
        cells[i] = (JsCell *)malloc(sizeof(JsCell));
        if (!cells[i]) goto fail;
        cells[i]->v = make_unset();
        cells[i]->refcnt = 1;
    }
    for (int i = 0; i < nfreevars; i++) {
        cells[ncellvars + i] = jfn->cells[i];
        retain_cell(jfn->cells[i]);
    }

    if (bind_args(vm, jfn->code_idx, cargs, nargs,
                  kwname_idx, ckwv, nkw, locals,
                  cells, ncellvars + nfreevars,
                  jfn->defaults, jfn->ndefaults,
                  0, 0) != 0)
        goto fail;

    int base = vm->sp;
    PUSH(vm, make_null());    /* 返回值占位 */
    if (push_frame(vm, jfn->code_idx, base, locals, sub->nlocals) != 0)
        goto fail;
    {
        JsFrame *nf = &vm->frames[vm->nframes - 1];
        nf->cells = cells;
        nf->ncells = ncellvars + nfreevars;
    }

    /* 记住进入前的帧数，跑回到该层即结束 */
    int entry = vm->nframes - 1;
    if (run_loop(vm) != 0) {
        vm->stop_depth = saved_depth;   /* 出错也要恢复边界 */
        return -1;
    }

    /* run_loop 会把 RETURN 值留在 base；帧已弹 */
    vm->stop_depth = saved_depth;
    while (vm->nframes > entry) pop_frame(vm, 1);
    *out = vm->stack[base];
    vm->sp = base;            /* 值所有权转移给调用者（out） */
    /* 释放借用的参数 */
    for (int i = 0; i < nargs; i++) release_val(vm, cargs[i]);
    for (int k = 0; k < nkw; k++) release_val(vm, ckwv[k]);
    free(cargs); free(ckwv);
    return 0;

fail:
    vm->stop_depth = saved_depth;
    for (int i = 0; i < nargs; i++) release_val(vm, cargs[i]);
    for (int k = 0; k < nkw; k++) release_val(vm, ckwv[k]);
    free(cargs); free(ckwv); free(locals);
    return -1;
}

/* 宿主把 Python 侧的基石函数值重新包装成 FUNC（如列表里存着回调） */
JSVM_API int jsvm_new_func_from(JsVm *vm, JVal *proto, JVal *out) {
    /* proto 必须是 FUNC（由宿主从 CvmFunction 还原） */
    if (proto->tag != JS_TAG_FUNC) return -1;
    JsFunc *src = (JsFunc *)(uintptr_t)proto->v.u;
    JsFunc *jfn = (JsFunc *)malloc(sizeof *jfn);
    if (!jfn) return -1;
    jfn->code_idx = src->code_idx;
    jfn->ncells = src->ncells;
    jfn->cells = (JsCell **)malloc(
        (size_t)(src->ncells > 0 ? src->ncells : 1) * sizeof(JsCell *));
    if (!jfn->cells) { free(jfn); return -1; }
    for (int i = 0; i < src->ncells; i++) {
        jfn->cells[i] = src->cells[i];
        retain_cell(jfn->cells[i]);
    }
    jfn->ndefaults = src->ndefaults;
    jfn->defaults = (JVal *)malloc(
        (size_t)(src->ndefaults > 0 ? src->ndefaults : 1) * sizeof(JVal));
    if (!jfn->defaults) { free(jfn->cells); free(jfn); return -1; }
    for (int i = 0; i < src->ndefaults; i++) {
        jfn->defaults[i] = src->defaults[i];
        retain_val(vm, jfn->defaults[i]);
    }
    jfn->refcnt = 1;
    *out = make_func(jfn);
    return 0;
}

/* 宿主读取 FUNC 的原始 JVal（CvmFunction 保活用） */
JSVM_API void jsvm_func_raw(JsVm *vm, JVal *fv, JVal *out) {
    (void)vm;
    *out = *fv;
}

/* 供宿主在回调里对借用值做 retain/release（如转换后延长生命） */
JSVM_API int jsvm_retain(JsVm *vm, JVal *v) { retain_val(vm, *v); return 0; }
JSVM_API int jsvm_release(JsVm *vm, JVal *v) { release_val(vm, *v); return 0; }

/* FUNC → 代码对象下标（宿主取名/报错用）；非 FUNC 返回 -1 */
JSVM_API int jsvm_func_code(JsVm *vm, JVal *fv) {
    (void)vm;
    if (fv->tag != JS_TAG_FUNC) return -1;
    return ((JsFunc *)(uintptr_t)fv->v.u)->code_idx;
}

/* 全局槽是否已赋值（宿主报错时惰性构建建议池用） */
JSVM_API int jsvm_global_set(JsVm *vm, int idx) {
    if (idx < 0 || idx >= vm->nnames) return 0;
    return vm->globals[idx].tag != JS_TAG_UNSET;
}

/* 取回无人接住的异常对象（宿主重新抛出用）。取出后 C 侧不再持有。 */
JSVM_API void jsvm_take_pending_err(JsVm *vm, JVal *out) {
    *out = vm->pending_err;
    vm->pending_err = make_unset();
}
