# 基石字节码与虚拟机设计（M4）

> 本文是 M4 的**规格说明**。`jishi/opcodes.py` 是这套规格的唯一事实来源，
> `cvm/include/opcodes.h` 由它自动生成（`python cvm/build.py --gen-header`），
> 两侧指令号永远同步。

---

## 1. 总体架构

```
源码 ──tokenizer──> token ──parser──> AST ──┬─> Interpreter  树遍历（M0 参考实现）
                                           └─> Compiler     字节码（M4）
                                                   │
                                        CompiledModule（常量池/名字表/代码对象）
                                                   │
                                        ┌──────────┴──────────┐
                                     vm.py                cvm (C)
                                   Python 参考 VM         C 栈式 VM
                                     （M4a）              （M4b）
                                                   │
                                              宿主回调桥接
                                         （打印/字符串/列表/Python 生态）
```

三个执行器共用 `jishi/runtime.py` 的语义实现，由 `tests/test_cvm.py`
的对拍测试保证「同一份源码，三个执行器输出逐字节一致」。

---

## 2. 值表示（JVal）

```c
typedef struct {
    uint8_t  tag;        /* 见下表 */
    uint8_t  pad[7];
    union { int64_t i; double d; uint64_t u; } v;
} JVal;                  /* 16 字节，按值传递 */
```

| tag | 名称    | 含义                                                  |
|-----|---------|-------------------------------------------------------|
| 0   | UNSET   | 空槽位（全局未赋值、单元未初始化）                     |
| 1   | NULL    | 基石的「空」                                           |
| 2   | FALSE   | 假                                                     |
| 3   | TRUE    | 真                                                     |
| 4   | INT     | `v.i` 为 64 位整数                                     |
| 5   | FLOAT   | `v.d` 为双精度浮点                                     |
| 6   | HOST    | `v.u` 为宿主句柄号（字符串/列表/字典/模块/Python 对象） |
| 7   | CELL    | `v.u` 为闭包单元号                                     |
| 8   | FUNC    | `v.u` 为基石函数对象号                                 |

**为什么整数/浮点/布尔做成原生 tag**：M4 的性能收益几乎全部来自
「数值循环与递归不再经过 Python 对象层」。整型加减乘、比较、跳转全部
在 C 里完成，只在溢出时回落宿主（交给 Python 大整数，语义不丢失）。

**句柄表（handle table）在 C 侧**，带引用计数：
`refcnt[idx]`、`gen[idx]`。
- `INCREF/DECREF` 是纯 C 的加减法，热路径无回调开销；
- 引用计数归零时调用 `host.release_handle(idx)`，宿主丢掉 Python 对象并把槽位还回空闲链表；
- `gen` 是代际号，防止悬垂句柄被误用（double free / use-after-free 立刻暴露成内部错误而非静默错值）。

`HOST` 句柄承载一切非数值：文本、列表、字典、模块、内建函数、Python 生态对象。
对这些值的运算通过**宿主回调**完成。

---

## 3. 指令格式

每条指令 6 个 32 位字段：`op, a, b, c, line, col`。

- `line/col` 来自 AST 节点，报错时原样回传宿主 → 中文报错的行列号与树遍历解释器一致。
- `a/b/c` 最多三个操作数，够用（`IMPORT` 需要 名字/是否来自python/别名 三个）。

### 指令集

| 操作码 | 操作数 | 栈效果 | 说明 |
|--------|--------|--------|------|
| `HALT` | — | — | 结束（模块代码末尾） |
| `LOAD_CONST` | `a`=常量号 | → 值 | 常量池取值 |
| `LOAD_GLOBAL` | `a`=名字号 | → 值 | 全局槽位，未赋值则回调宿主报「找不到名字」 |
| `STORE_GLOBAL` | `a`=名字号 | 值 → | |
| `LOAD_FAST` | `a`=局部槽 | → 值 | 函数局部（含参数） |
| `STORE_FAST` | `a`=局部槽 | 值 → | |
| `LOAD_DEREF` | `a`=单元号 | → 值 | 闭包变量（单元变量或自由变量） |
| `STORE_DEREF` | `a`=单元号 | 值 → | |
| `LOAD_CELL` | `a`=单元号 | → 单元 | 把单元本身压栈，供 `MAKE_FUNCTION` 组装闭包 |
| `POP_TOP` | — | 值 → | |
| `DUP_TOP` | — | x → x x | |
| `ROT_TWO` | — | a b → b a | |
| `ROT_THREE` | — | a b c → c a b | 链式比较用 |
| `BIN_OP` | `a`=运算号 | l r → 结果 | `0+ 1- 2* 3/ 4// 5% 6**` |
| `UNARY_OP` | `a`=运算号 | v → 结果 | `0` 取负、`1` 非 |
| `COMPARE` | `a`=比较号 | l r → 布尔 | `0== 1!= 2< 3> 4<= 5>=` |
| `JUMP` | `a`=目标 | — | |
| `POP_JUMP_IF_FALSE` | `a`=目标 | 值 → | |
| `POP_JUMP_IF_TRUE` | `a`=目标 | 值 → | |
| `JUMP_IF_FALSE_OR_POP` | `a`=目标 | 值 → 值 | 假则跳（保留栈顶），真则弹出 |
| `CALL` | `a`=位置参数数 `b`=关键字名表号 | f args… kwvals… → 结果 | |
| `RETURN` | — | 值 → | 帧返回 |
| `MAKE_FUNCTION` | `a`=代码号 `b`=单元数 | 单元… → 函数 | 弹出 `b` 个单元组装闭包 |
| `BUILD_LIST` | `a`=元素数 | x1…xn → 列表 | |
| `BUILD_DICT` | `a`=键值对数 | k1 v1…kn vn → 字典 | |
| `GET_ATTR` | `a`=名字号 | 对象 → 属性 | 模块属性 → 基石中文方法 → Python 属性 |
| `SET_ATTR` | `a`=名字号 | 对象 值 → | |
| `GET_ITEM` | — | 对象 下标 → 值 | |
| `SET_ITEM` | — | 对象 下标 值 → | |
| `GET_ITER` | — | 可迭代 → 迭代器 | |
| `FOR_ITER` | `a`=目标 | 迭代器 → 迭代器 [元素] | 耗尽则弹出迭代器并跳 `a` |
| `IMPORT` | `a`=名字号 `b`=是否来自python `c`=别名号(-1 无) | → 模块 | |

---

## 4. 模块结构（CompiledModule）

一个模块编译出**扁平共享**的表 + 若干代码对象，C 侧因此只需一份数组：

```
consts[]    常量池（数值原生编码；文本等由宿主预先分配句柄）
names[]     名字表（去重；参数名与关键字名同表 → 关键字绑定退化为整数比较）
kw_names[]  关键字名表（CALL 的 b 指向其中一项）
codes[]     代码对象数组，codes[main] 是模块主代码
```

### 代码对象（Code）

```
name         函数名（模块代码为 "<模块>"）
params       参数名列表
param_idx    参数名在 names[] 中的下标（关键字绑定用）
instrs       指令流
nlocals      局部槽位数（含隐藏临时槽，如「循环 n 次」的计数器）
local_names  槽位名（调试/反汇编用）
cellvars     本作用域绑定、被内层引用的名字（有序）
freevars     本作用域引用、由外层绑定的名字（有序）
```

### 单元（cell）布局

帧的单元数组 = `sorted(cellvars) ++ sorted(freevars)`。
- 代码内部访问：单元号 = 下标的拼接结果（上表同一布局）。
- `MAKE_FUNCTION` 时，按被创建函数的 `freevars` 顺序，逐个 `LOAD_CELL`
  从**当前帧**的单元数组里取，压栈后再由 `MAKE_FUNCTION` 消费。

这样闭包不需要额外的映射表，指令只携带整数下标。

---

## 5. 作用域分析

树遍历解释器用的是「作用域链 + 词法闭包」。字节码要保住同样的语义，
同时让局部变量走快路径，必须在**编译期**判定每个名字的种类：

1. **收集**（一次 AST 遍历）：每个作用域的 `bound`（被绑定的名字：
   赋值目标 / 遍历目标 / 参数 / 函数定义名 / 导入名）与 `used`（被读取的名字）。
2. **定种**（作用域树后序遍历，最深的子作用域先算）：
   对函数作用域 `S` 中每个被读取且非本作用域绑定的名字，
   向外找最近的「绑定了它」的函数作用域 `E`：
   - 找到且 `E` 是函数 → `E.cellvars += name`，`S` 与 `E` 之间的所有作用域 `freevars += name`；
   - 找到但 `E` 是模块 → 全局；
   - 都没找到 → 全局（内建函数、`导入` 进来的名字都走这条）。

后序遍历是关键：先处理内层，才能在外层发出任何指令**之前**把
「这个变量要变成单元」的决定定下来，避免出现「前半段用 `STORE_FAST`、
后半段用 `LOAD_DEREF`」的分裂。

---

## 6. 栈帧与调用

所有帧共享一条值栈，帧只记录自己的栈底：

```c
typedef struct { int code_idx; int ip; int base; JVal *locals; uint32_t *cells; } JsFrame;
```

`CALL` 到 `FUNC` 时：
1. 参数已在栈顶，按 `param_idx` 与关键字名编号做绑定（同名即同号，整数比较即可）；
2. 参数过多 / 缺少 / 未知关键字 → 回调宿主抛中文错误；
3. 压入新帧，`base = 原 sp - (1 + 位置参数数 + 关键字数)`；
4. `RETURN`：把返回值留在 `base`，清掉其上的一切，弹帧。

帧深度上限 1000，超出回调宿主抛「递归层数太深了，是不是函数忘了写结束条件？」。

**为什么 `打印` 必须走宿主**：C 的 `printf` 写的是 CRT 的 stdout，
不走 Python 的 `sys.stdout`，测试里的 `redirect_stdout` 抓不到。
所以输出一律回调宿主，对拍测试才能成立。

---

## 7. 宿主回调协议

C 侧不链接 libpython，只持有一张函数指针表（`JsHost`），由 Python 通过
ctypes 传入。所有跨边界的参数都用**指针**传递（结构体按值传参在 ctypes
上不可靠），返回值统一是 `int` 状态码（0 成功 / -1 出错）。

```c
typedef struct JsHost {
    /* 语义回调全部带 (line, col)：中文报错行列与树遍历一致 */
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
    /* 迭代协议：batchable 表示可安全批量预取（list/范围/文本/字典）。
     * iter_next_batch 一次填最多 cap 个元素进 C 侧缓冲（n==0 表示耗尽），
     * 循环体因此零回调；生成器等保持 iter_next 逐个拉取。 */
    int (*iter_new)        (JVal *obj, JVal *out, int *batchable,
                            int line, int col);
    int (*iter_next)       (JVal *it, JVal *out, int *done, int line, int col);
    int (*iter_next_batch) (JVal *it, JVal *out, int cap, int *n,
                            int line, int col);
    int (*do_import)  (int name_idx, int from_python, JVal *out,
                       int line, int col);
    int (*to_int)     (JVal *a, JVal *out, int line, int col);
    /* 生命周期与错误（raise_* 家族补全见 jsvm.c） */
    int (*release_handle)   (uint32_t handle);
    int (*raise_zero_div)   (int line, int col);
    int (*raise_name)       (int name_idx, int line, int col);
    int (*raise_recursion)  (int line, int col);
    int (*raise_unbound_local) (int code_idx, int slot, int line, int col);
    int (*raise_unbound_cell)  (int code_idx, int cell_idx, int line, int col);
    int (*raise_not_iter)   (JVal *obj, int line, int col);
    int (*raise_break)      (int line, int col);
    int (*raise_continue)   (int line, int col);
    int (*raise_argc)       (int code_idx, int got, int line, int col);
    int (*raise_bad_kw)     (int code_idx, int name_idx, int line, int col);
    int (*raise_missing)    (int code_idx, const int *missing, int nmissing,
                             int line, int col);
    int (*raise_internal)   (const char *msg);
} JsHost;
```

**迭代器表示**：GET_ITER 在 C 侧包一层 `JsIterC`（标签 `T_ITER`）：
内含 256 槽预取缓冲、宿主迭代器句柄与耗尽标志。可批量类型
（list/tuple/range/文本/字典键）一次回调填满缓冲，循环体零回调；
其余类型逐元素回调 `iter_next`。缓冲残留元素在迭代器引用计数归零时释放。

**STORE_GLOBAL 不回调宿主**：全局影子表仅供报错建议使用，
宿主在 `raise_name` 时经 `jsvm_global_set(vm, idx)` 惰性查询 C 侧
全局槽是否已赋值，避免热循环里每次赋值都跨边界。

**重入与帧边界**：`JsVm.stop_depth` 记录当前 `run_loop` 的最浅帧层；
RETURN/HALT 弹到这一层即返回。`jsvm_run` 设 0；
`jsvm_call_function`（Python 高阶函数回调基石函数）设为进入时帧深
并在返回后恢复 —— 保证重入的内层循环不会「劫持」外层暂停的帧。

**错误处理**：宿主回调抛异常时，把异常对象存进 `vm->pending_error`、
返回 `-1`；C 主循环立刻停止并逐层返回错误状态；Python 绑定层再把它
重新抛出。这样中文报错（含行列号）完全由 Python 侧生成，
与树遍历解释器共用 `jishi/runtime.py` 的措辞。

**句柄创建**：宿主回调返回新值时调用 `jsvm_new_handle(vm, obj)`，
该函数在 C 的句柄表里分配槽位并把 `refcnt` 置 1。

**基石函数回流**：基石函数传给 Python 作回调（如 `sorted(key=f)`）时，
宿主用 `jsvm_func_code / jsvm_func_cell` 读取其代码号与单元，
包成 `VmFunction`；Python 反过来把 `VmFunction` 交还时，
宿主用 `jsvm_new_func` 重新造一个 `FUNC` 值。这条路径只影响高阶函数，
不影响主循环性能。

---

## 8. 性能策略

| 场景 | 走的路 |
|------|--------|
| 整数加减乘、比较、跳转 | 纯 C，无回调 |
| 整数除法/取模/整除 | C 快路径 + C 侧除零检查 |
| 整数溢出、浮点、混合类型 | 回落宿主（Python 语义，含大整数） |
| 局部/全局变量读写 | C 数组直接寻址 |
| 基石函数调用与返回 | C 帧栈 |
| 文本/列表/字典/模块/生态库 | 宿主回调 |

已知待优化项（M4 之后）：
- `遍历 i 于 范围(n)` 目前每次迭代一次回调，可加 `RANGE_ITER` 指令做纯 C 计数；
- 字符串拼接可加 `CONCAT` 快路径。
先把「语义一致」钉死，再谈这些。

---

## 9. 验收标准

1. `tests/cases/*.jsh` 与 `tests/error_cases/*.jsh` 三个执行器逐字节一致；
2. 报错仍是中文、行列号准确；
3. `tools/benchmark.py` 给出可复现的提速数据；
4. `python cvm/build.py` 一条命令产出动态库（Windows 优先，Linux/macOS 兼容）。

## 8. 异常处理协议（M5a）

C 侧不持有「异常对象」的原生表示 —— 异常对象是 `JishiError` 实例，
存为 HOST 句柄（`vm->pending_err`，标签 `T_HOST`，引用计数归 C 侧）。

**数据流**：
1. 宿主语义回调出错 → 把 `JishiError` 存进 Python 侧 `pending` 并返回 -1；
2. 主循环任何指令遇到回调 -1 → `goto dispatch_err`（不再立即 return）；
3. `dispatch_err` 里 `error_handle` 回调把 pending 异常转成 HOST 句柄；
4. `dispatch_exception` 从当前帧向外找异常处理器（try 块栈），
   信号（中断/继续）则找循环（就近原则，与 Python 块栈一致）：
   - 命中 handler → 截栈、压异常对象、跳 `catch_ip`；
   - 命中循环 → 截栈、跳循环的 brk/cont；
   - 都无 → 弹帧继续向外；弹到底 → return -1，由宿主重新抛出。

**指令**：
| 指令 | 作用 |
|------|------|
| `SETUP_TRY a, b` | 登记处理器：a=catch 入口，b=finally 入口（-1 无） |
| `POP_TRY` | 正常离开 try 块时注销处理器 |
| `THROW` | 弹出值，`make_error` 规范化成异常对象后分发 |
| `CHECK_SIGNAL a` | 栈顶是信号则跳 a（进未匹配路径） |
| `MATCH_EXC` | `[err, cond] → [err, 布尔]`，`match_error` 按 isinstance 匹配 |
| `END_FINALLY` | finally 末尾：有挂起返回则继续返回流程，否则 fall through |

**帧内结构**：`JsFrame` 增加 `JsHandler handlers[]`（catch_ip/finally_ip/sp/seq）
与 `block_seq`（循环与处理器共用登记序号，越大越内层，用于就近判断）。

**`最终` 与返回的交互**：RETURN 时若最近的 handler 有 finally，
先把返回值挂到 `f->pending_return`，跳 finally 入口；`END_FINALLY`
看到挂起返回就继续返回流程 —— 与 vm.py 的 `_return_via_finally` 一致。

**重入边界**：`dispatch_exception` 不越过 `vm->stop_depth`，
保证 Python 高阶函数回调基石函数时异常只在内层分发。
