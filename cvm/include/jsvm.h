/* jsvm.h —— 基石 C 字节码虚拟机的公开 C ABI（M9.4 嵌入模式）
 *
 * 第三方宿主（C / Node / C# / Rust …）通过本头文件嵌入基石脚本引擎，
 * 无需链接 libpython：一切复杂语义经 JsHost 回调桥回宿主。
 *
 * ABI 稳定性：JSVM_ABI_VERSION 描述结构布局与函数签名的兼容版本。
 * 一旦发布，跨版本不得改动已有字段/签名，只能追加。
 */

#ifndef JSVM_H
#define JSVM_H

#include <stdint.h>
#include <string.h>

#include "opcodes.h"   /* JS_TAG_* / JS_OP_* / JS_MAX_FRAMES */

#ifdef __cplusplus
extern "C" {
#endif

/* ABI 版本：结构布局或函数签名不兼容时递增 */
#define JSVM_ABI_VERSION 1

/* 导出宏：Windows 用 dllexport/dllimport；其余用 visibility */
#if defined(_WIN32)
  #if defined(JSVM_BUILD)
    #define JSVM_API __declspec(dllexport)
  #else
    #define JSVM_API __declspec(dllimport)
  #endif
#else
  #define JSVM_API __attribute__((visibility("default")))
#endif

/* ------------------------------------------------------------------ */
/* 值表示                                                              */
/* ------------------------------------------------------------------ */

/* 16 字节的带标签值：tag 见 opcodes.h 的 JS_TAG_* */
typedef struct JVal {
    uint8_t tag;
    uint8_t pad[7];
    union {
        int64_t  i;   /* JS_TAG_INT */
        double   d;   /* JS_TAG_FLOAT */
        uint64_t u;   /* HOST=句柄号 / CELL=JsCell* / FUNC=JsFunc* */
    } v;
} JVal;

/* 不完整类型：内部结构对宿主不可见，仅作指针 */
struct JsCell;
struct JsFunc;
typedef struct JsVm JsVm;

/* 值构造辅助（inline，宿主构造字面量/句柄用） */
static inline JVal jsvm_unset(void) {
    JVal x; memset(&x, 0, sizeof x); x.tag = JS_TAG_UNSET; return x;
}
static inline JVal jsvm_null(void) {
    JVal x; memset(&x, 0, sizeof x); x.tag = JS_TAG_NULL; return x;
}
static inline JVal jsvm_bool(int b) {
    JVal x; memset(&x, 0, sizeof x);
    x.tag = b ? JS_TAG_TRUE : JS_TAG_FALSE; return x;
}
static inline JVal jsvm_int(int64_t i) {
    JVal x; memset(&x, 0, sizeof x); x.tag = JS_TAG_INT; x.v.i = i; return x;
}
static inline JVal jsvm_float(double d) {
    JVal x; memset(&x, 0, sizeof x); x.tag = JS_TAG_FLOAT; x.v.d = d; return x;
}
static inline JVal jsvm_host(uint32_t id) {
    JVal x; memset(&x, 0, sizeof x); x.tag = JS_TAG_HOST; x.v.u = id; return x;
}
static inline JVal jsvm_cell(struct JsCell *c) {
    JVal x; memset(&x, 0, sizeof x);
    x.tag = JS_TAG_CELL; x.v.u = (uint64_t)(uintptr_t)c; return x;
}
static inline JVal jsvm_func(struct JsFunc *f) {
    JVal x; memset(&x, 0, sizeof x);
    x.tag = JS_TAG_FUNC; x.v.u = (uint64_t)(uintptr_t)f; return x;
}

/* ------------------------------------------------------------------ */
/* 宿主回调接口（JsHost）：宿主实现，把语义桥回自己的运行时            */
/* ------------------------------------------------------------------ */

typedef struct JsHost {
    /* 语义回调都带 (line, col)：中文报错的行列号必须与树遍历一致 */
    int (*binop)      (int op, JVal *a, JVal *b, JVal *out, int line, int col);
    int (*unary)      (int op, JVal *a, JVal *out, int line, int col);
    int (*compare)    (int op, JVal *a, JVal *b, JVal *out, int line, int col);
    int (*truthy)     (JVal *a, int *out, int line, int col);
    int (*call)       (JVal *fn, JVal *args, int nargs,
                       const int *kwname_idx, JVal *kwvals, int nkw,
                       JVal *out, int line, int col);
    int (*getattr)    (JVal *obj, int name_idx, JVal *out, int line, int col);
    int (*setattr)    (JVal *obj, int name_idx, JVal *val, int line, int col);
    int (*getitem)    (JVal *a, JVal *b, JVal *out, int line, int col);
    int (*setitem)    (JVal *a, JVal *b, JVal *val, int line, int col);
    int (*build_list) (JVal *items, int n, JVal *out, int line, int col);
    int (*build_dict) (JVal *flat_kv, int n, JVal *out, int line, int col);
    int (*iter_new)   (JVal *obj, JVal *out, int *batchable,
                       int line, int col);
    int (*iter_next)  (JVal *it, JVal *out, int *done, int line, int col);
    /* 批量预取：list/范围/文本等安全类型一次取一批，循环体零回调。
     * 填 out[0..n-1]，n==0 表示耗尽。
     *
     * ⚠️ **所有权**：每个 out[i] 必须是**本宿主新持有的一份引用**（宿主交给
     * C 侧；C 侧负责释放）。**不许**把同一个值的引用重复填进多个槽位，
     * 也不许填「借来的」值——C 侧会为缓冲里每个槽位各放一次。
     * （C 侧不变式见 `jsvm.c` 的 `release_buf`。） */
    int (*iter_next_batch)(JVal *it, JVal *out, int cap, int *n,
                           int line, int col);
    int (*do_import)  (int name_idx, int from_python, JVal *out,
                       int line, int col);
    int (*to_int)     (JVal *a, JVal *out, int line, int col);
    int (*release_handle)(uint32_t handle);
    /* 异常协议（M5a）：
     *   error_handle：宿主把 pending 异常转成 HOST 句柄交给 C 侧
     *   （C 侧持引用，分发时压栈；无人接住时由宿主重新抛出）。
     *   make_error：把 `抛出` 的值规范化成异常对象，
     *   原值所有权转移给返回的异常句柄。
     *   match_error：判断捕获条件 cond 是否接得住异常 exc。
     *   is_signal：exc 是否「中断/继续」信号（需穿透到循环）。 */
    int (*error_handle)(JVal *out);
    int (*make_error)(JVal *value, int line, int col, JVal *out);
    /* 类定义：name=类名，base=基类（UNSET 表示无），methods=方法函数数组 */
    int (*make_class)(JVal *name, JVal *base, JVal *methods, int nmethods,
                      JVal *out, int line, int col);
    /* 解包（M7）：把栈顶可迭代解成 n 个元素（out 为预分配数组，所有权归 C）。
       star >= 0 时是带星号解包（M25）：位置 star 的元素收集「其余」成列表，
       其余 n-1 个位置取首尾对应的单个元素。 */
    int (*unpack)(JVal *value, int n, int star, JVal *out,
                  int line, int col);
    int (*match_error)(JVal *exc, JVal *cond, int *out, int line, int col);
    int (*is_signal)(JVal *exc, int *out);
    /* 报错族：宿主抛出与树遍历逐字一致的中文错误 */
    int (*raise_name)       (int name_idx, int line, int col);
    int (*raise_unbound_local)(int code_idx, int slot, int line, int col);
    int (*raise_unbound_cell)(int code_idx, int cell_idx, int line, int col);
    int (*raise_zero_div)   (int line, int col);
    int (*raise_recursion)  (int line, int col);
    int (*raise_not_iter)   (JVal *obj, int line, int col);
    int (*raise_break)      (int line, int col);
    int (*raise_continue)   (int line, int col);
    int (*raise_argc)       (int code_idx, int got, int line, int col);
    int (*raise_bad_kw)     (int code_idx, int name_idx, int line, int col);
    int (*raise_missing)    (int code_idx, const int *missing, int nmissing,
                             int line, int col);
    int (*raise_internal)   (const char *msg);
    /* 列表追加（M11）：obj.追加(val)，省一次 getattr 回调 */
    int (*list_append)      (JVal *obj, JVal *val, int line, int col);
    /* 切片（M18）：start/stop/step 三个分量（UNSET 表示省略）→ slice HOST 句柄 */
    int (*make_slice)       (JVal *start, JVal *stop, JVal *step, JVal *out,
                             int line, int col);
    /* 变长关键字参数（M25）：把未匹配的关键字收成字典。
       name_idxs/vals 各 n 个（并行数组），所有权归调用方；
       out 是新字典，C 侧持有其引用。 */
    int (*pack_kwargs)      (const int *name_idxs, JVal *vals, int n,
                             JVal *out, int line, int col);
    /* 报错现场（M40）：C 侧把「出错那一刻的帧栈」交给宿主，宿主记到异常对象上
       （`JishiError.trace`），于是 C VM 的函数内报错也能印出「调用链（从外到内）」——
       与树遍历 / Python VM 的报错长得一样。

       depth 是**帧栈里除主帧以外的层数**；每层三个 int32 写在 flat 里
       （code_idx、call_line、call_col 顺序）。调用点取「该帧被调进来时的行列」，
       与 errors.Frame 的 line 语义一致（那是调用点，不是当前执行行）。

       宿主实现只需把 flat 翻成 Frame 列表挂到 pending 异常上；返回 0 表示
       「不用记」（例如已经记过）。**必须在帧被弹掉之前调**。 */
    int (*note_frames)      (const int *flat, int depth);
    /* 调试：把一帧的变量交给宿主（M40）。
       `slot` 是槽位号（local 在前、单元在后，与 Code.local_names/cellvars
       的排布一致），`is_cell` 标明它属于哪一段，`name_idx` 是名字表下标
       （C 侧没有「下标 → 字符串」的能力，翻译交给宿主）。
       值以**借用**形式传入：宿主转换后须自己持有结果，不得保存 `val` 指针。
       返回 0 继续，非 0 让 C 侧中止（宿主报错时用）。 */
    int (*debug_local)      (int slot, int is_cell, int name_idx,
                             const JVal *val);
    /* 批量追加（M46 阶段一③）：把 C 侧攒下的 n 个值**一次**落盘到 handle。
       C 侧「延迟合并写」的落盘口——把「循环里追加 N 次」的 N 次回调压成 1 次
       （实测每次宿主回调固定约 2.4µs，是 C VM 最大的固定开销）。

       `vals` 是 C 侧数组（**借用**：回调返回后由 C 侧释放并回收各值的引用），
       顺序就是用户的追加顺序。宿主实现：目标是 list 就直接 extend，
       否则按「obj.追加(v)」逐个走通用分发（语义必须与单条 list_append 一致）。

       返回 0 成功；非 0 表示已经报错（走 error_handle 那一套）。
       ⚠️ 新回调**必须追加在末尾**（C 与 Python 两侧逐字段对齐）。 */
    int (*list_append_many) (uint32_t handle, JVal *vals, int n);
    /* 批量字典写入（M46 阶段一④）：与 `list_append_many` 同一套「延迟合并写」，
       把「循环里 字[键] = 值 N 次」压成 1 次宿主回调。

       `kvs` 每项 2 个 JVal（键、值）；`lc` 每项 2 个 int32（行、列）——
       **行列是按项给的**：字典写入可能失败（键不可哈希），报错行号必须精确，
       不能拿整批里第一条的行号糊。两者都是 C 侧数组（**借用**），
       顺序就是用户的写入顺序（同键后者覆盖，与逐条写入一致）。

       返回 0 成功；非 0 表示已经报错（走 error_handle 那一套）。 */
    int (*dict_setitem_many) (uint32_t handle, JVal *kvs,
                              const int32_t *lc, int n);
    /* 取方法并立刻调用，**一次**回调完成（M48 阶段三②）。

       为什么需要：`X.方法(实参)` 编译成 `GET_ATTR` + `CALL` 两条指令，
       于是每次方法调用要付**两次**宿主回调（实测 `_cb_getattr` 4.5µs +
       `_cb_call` 4.3µs，而一次回调的**必要工作**只有 2–3µs，其余是 ctypes
       编组的固定开销）。生成.jsh 里「开头是」被调 88286 次，光这一处就
       占该项目 47% 的运行时间。

       ⚠️ 触发条件由**编译器**标出（`GET_ATTR.b == 1`），C 侧只负责在
       `ip == a` 时调本回调并跳过那条 `CALL`。**这不是新指令**——`GET_ATTR`
       的 `b` 字段历史上没有任何执行器读过（Python VM / Node 都只读 `a`），
       所以借它当提示不需要改字节码格式、也不影响其它宿主（它们忽略 `b`，
       照常走两次回调，只是慢一点）。

       语义必须与「先 getattr 再 call」**逐字一致**，包括：
       - 取属性失败 → 报错位置是 **GET_ATTR 的行列**（`gline/gcol`）
       - 调用失败   → 报错位置是 **CALL 的行列**（`line/col`）
       宿主实现：`r = get_attr(obj, name); call(r, args)`。 */
    int (*method_call)  (JVal *obj, int name_idx, JVal *args, int nargs,
                         JVal *out, int gline, int gcol, int line, int col);
} JsHost;

/* ------------------------------------------------------------------ */
/* 生命周期与执行                                                      */
/* ------------------------------------------------------------------ */

JSVM_API JsVm *jsvm_create(void);
JSVM_API void  jsvm_destroy(JsVm *vm);
JSVM_API void  jsvm_set_host(JsVm *vm, JsHost *host);

/* 加载已编译的字节码模块。各数组由宿主持有，生命周期须覆盖 VM 存活期。
 * instrs/code_meta/code_params/kw_flat/kw_off 的扁平格式见 cvm/bytecode.md */
JSVM_API int jsvm_load_module(
    JsVm *vm,
    JVal *consts, int nconsts,
    int nnames, JVal *globals_in,
    int32_t *instrs, int ninstrs,
    int32_t *code_meta, int ncodes,
    int32_t *code_params,
    int32_t *kw_flat, int32_t *kw_off, int nkw,
    int main_idx);

/* 运行 main 代码对象，返回 0 成功 / -1 出错（异常经宿主回调抛出） */
JSVM_API int jsvm_run(JsVm *vm);
JSVM_API void jsvm_get_last(JsVm *vm, JVal *out);

/* 语句起始表（M40）：与 instrs 等长，第 i 项是「第 i 条指令所属语句的行号」，
 * 0 表示它不是语句的第一条。调试器靠它认出「语句边界」——栈机没有这种事件，
 * 但编译器知道。
 *
 * 单独一个 setter 而不是加进 jsvm_load_module 的参数表：**不改已有 ABI**，
 * 而且「要不要调试」与「能不能装载」本来就是两件事（不设表 = 不调试）。
 * 数组内存仍归宿主持有，须覆盖 VM 存活期；传 NULL 表示关掉。 */
JSVM_API void jsvm_set_stmt_marks(JsVm *vm, int32_t *marks, int nmarks);

/* 调试钩子（M40）：非 NULL 时，**每条语句的第一条指令**执行前回调一次。
 * 回调返回非 0 表示请求中止（宿主用 -1 让 jsvm_run 返回错误）。
 * 这是**只读**钩子：C 侧不因它改变任何执行状态（不调它时开销为零）。 */
typedef int (*JsStmtHook)(void *ud, int code_idx, int line, int nframes);
JSVM_API void jsvm_set_stmt_hook(JsVm *vm, JsStmtHook hook, void *ud);

/* 帧查询（M40，调试器用）。`depth` 从**最内层**数起：0 = 当前帧。

 *   jsvm_frame_meta：取一帧的「代码下标 + 调用点行列」（三个 int32 写进 out3）。
 *                     只回整数、不碰值，所以随时可调。
 *   jsvm_dump_locals：取一帧的局部槽与单元，经宿主回调 `debug_locals` 转换。
 *   jsvm_dump_globals：取模块全局里**已赋值**的那些，同样经 `debug_locals`
 *                      （`is_cell == 2`，`slot` 就是名字表下标）。
 *                     值以**借用**形式传给宿主（宿主不得持有），这样不必为
 *                     调试专门发明一套「值数组」的所有权规则。
 *
 * 都返回 0 成功 / -1 表示参数越界。 */
JSVM_API int jsvm_frame_meta(JsVm *vm, int depth, int *out3);
JSVM_API int jsvm_dump_locals(JsVm *vm, int depth);
JSVM_API int jsvm_dump_globals(JsVm *vm);

/* 句柄与引用计数 */
JSVM_API int jsvm_track_handle(JsVm *vm, uint32_t id);
JSVM_API int jsvm_retain(JsVm *vm, JVal *v);
JSVM_API int jsvm_release(JsVm *vm, JVal *v);

/* 原生字符串（M47 阶段二①）：`JS_TAG_STR` 是**不可变的 UTF-8 字符串**，
 * 字节与引用计数都由 C 侧持有（`甲 + "x"` 因此不必回调宿主）。
 *
 * 为什么需要它：字符串若只以 HOST 句柄表示，C 侧就看不见内容，于是每次
 * 拼接都要一次宿主回调（实测每次固定约 2.4µs，是 C VM 最大的固定开销），
 * 而树遍历直接调 Python 的 C 级 `str.__add__`（约 0.05µs）——差 48 倍。
 * 字符串**不可变**，所以没有列表/字典那种「别名 + 逃逸」的问题
 * （M46 阶段一③④ 就是被那个坑逼着换了方案），这是它与容器的关键区别。
 *
 * 宿主不必了解内部布局，用下面三个函数读写（**不要**手解 `v.u`）：
 *   jsvm_str_new   把一段 UTF-8 字节交给 C 侧（**复制**一份）→ 得到一个
 *                  JS_TAG_STR 值，refcnt = 1，所有权交给调用者；
 *                  返回 0 成功 / -1 内存不足（此时 out 未改动）
 *   jsvm_str_len   字节数（非原生字符串返回 -1）
 *   jsvm_str_bytes 字节首地址（非原生字符串返回 NULL；生命周期同该值） */
JSVM_API int jsvm_str_new(JsVm *vm, const char *utf8, int32_t blen, JVal *out);
JSVM_API int32_t jsvm_str_len(const JVal *v);
JSVM_API const char *jsvm_str_bytes(const JVal *v);

/* 批量构造原生字符串（M47 阶段二①）：`buf` 里依次放着 n 段 UTF-8 字节，
 * 第 i 段是 `buf[offsets[i] .. offsets[i+1])`（所以 offsets 有 n+1 项）。
 * 结果写进 `out[0..n-1]`（调用方须提供 n 个 JVal 的位置），refcnt 都是 1。
 *
 * 一次调用建整批：宿主侧「遍历一段文本」时每批最多 256 个元素，逐个
 * `jsvm_str_new` 光 FFI 往返就要 0.87µs × 256 —— 这是文本迭代的主要成本。
 * 返回 0 成功；-1 失败（内存不足 / 参数非法），**失败时已建的会被释放干净**，
 * 不会留半成品给调用方。 */
JSVM_API int jsvm_str_new_batch(JsVm *vm, const char *buf,
                                const int32_t *offsets, int n, JVal *out);

/* 函数对象操作（重入调用 / 从原型复制 / 取元数据） */
JSVM_API int jsvm_func_retain(JsVm *vm, JVal *fv);
JSVM_API int jsvm_func_release(JsVm *vm, JVal *fv);
JSVM_API int jsvm_call_function(JsVm *vm, JVal *fv,
                                JVal *args, int nargs,
                                const int *kwname_idx, JVal *kwvals, int nkw,
                                JVal *out, int call_line, int call_col);
JSVM_API int jsvm_new_func_from(JsVm *vm, JVal *proto, JVal *out);
JSVM_API void jsvm_func_raw(JsVm *vm, JVal *fv, JVal *out);
JSVM_API int jsvm_func_code(JsVm *vm, JVal *fv);

/* 杂项 */
JSVM_API int jsvm_global_set(JsVm *vm, int idx);
JSVM_API void jsvm_take_pending_err(JsVm *vm, JVal *out);

#ifdef __cplusplus
}
#endif

#endif /* JSVM_H */
