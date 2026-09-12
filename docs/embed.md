# 基石引擎嵌入指南（M13）

> 本文介绍如何把基石引擎嵌入**任意宿主**，脱离 Python 编译前端独立执行基石代码。
> 核心思路：**字节码是平台无关的中间格式**，各语言各自实现执行引擎。

## 1. 架构总览

```
基石源码 ──(Python 编译前端)──> JSON 字节码（M13.1）──┬─> Python VM（jishi/vm.py）
                                                        ├─> C VM（jsvm.dll，宿主驱动）
                                                        ├─> JS VM（node/，零依赖）
                                                        └─> Rust VM（rust/）
```

字节码格式（M13.1）是唯一契约：

- **format** = `jishi-bytecode`，**version** = 1，附 `sha256` 校验和；
- **opcode_names**：指令号 → 名字表（48 条指令）；
- **payload**：常量池（只含标量：文本/整数/浮点/空/布尔）、名字表、
  关键字名表、代码对象数组（每条指令 6 个 int32：op/a/b/c/line/col）。

产出一份字节码：

```bash
jishi 你的脚本.jsh --dump-bytecode > 脚本.json
```

## 2. C 宿主（M9.4）

C 宿主直接链接 `jsvm.dll`（`cvm/include/jsvm.h`），这是**宿主驱动**架构：
C 侧不链接 libpython，所有字符串/列表/字典/打印/异常通过 `JsHost` 回调表
回传给宿主实现。

最小示例见 `examples/embed/hello.c`，要点：

```c
#include "jsvm.h"
// 1. 创建 VM、传入 JsHost 回调表
JsVm *vm = jsvm_create();
jsvm_set_host(vm, &host);
// 2. 加载字节码（扁平数组）
jsvm_load_module(vm, consts, nconsts, nnames, globals_in,
                 instrs, ninstrs, code_meta, ncodes, code_params,
                 kw_flat, kw_off, nkw, main_idx);
// 3. 运行
jsvm_run(vm);
```

C 宿主的约束：常量池里的字符串等非数值对象，须由宿主预先分配句柄；
所有 I/O 与 Python 生态对象操作都要宿主回调实现。适合对性能有极致要求、
愿意实现完整回调表的宿主。

## 3. Node.js 宿主（M13.2）

Node 宿主用**纯 JS 解释器**（`node/` 目录），零第三方依赖，消费同一份 JSON
字节码：

```bash
node node/index.js 脚本.json
```

详见 `node/README.md`。这是最省事的跨语言方案——JS 的 `Map` 天然对应字典、
`Array` 对应列表、`BigInt` 可扩展大整数。

**为何不是 ffi 加载 jsvm.dll**：jsvm.dll 是宿主驱动架构（~40 个语义回调），
ffi 方案要把 `runtime.py` 整个翻译成 JS，工作量约 3 倍。纯 JS 解释器只需
翻译指令分发 + 12 内建 + 方法表。

## 4. Rust 宿主（M13.3）

Rust 宿主同样消费 JSON 字节码，用 `serde_json` 解析后自行解释执行（或
ffi 加载 jsvm.dll，二选一）。纯 Rust 解释器方案与 Node 版对称，`rust/` 目录
提供 `jishi-ffi` crate。

## 5. 支持范围与边界（诚实记录）

三个引擎（Python / C / JS）语义一致的部分由 `tests/test_cvm.py` 对拍保证。
各宿主的能力边界：

| 能力 | Python VM | C VM | JS VM |
|------|-----------|------|-------|
| 核心语法 + 内建 + 方法 + 类/异常/闭包 | ✅ | ✅ | ✅ |
| `导入 x 从 python`（Python 生态桥接） | ✅ | ✅（宿主实现） | ❌ |
| `导入 x 从 本地包` | ✅ | ✅（宿主实现） | ❌ |
| 浮点尾零显示 `5.0` | ✅ | ✅ | ✅（FloatBox 装箱） |
| 大整数（任意精度） | ✅ | ✅ | ⚠️ 需 BigInt 扩展 |

## 6. 性能对比（基准数据）

| 场景 | 树遍历 | Python VM | C VM | JS VM |
|------|--------|-----------|------|-------|
| 整数循环（20000 次） | 1x | ~0.6x | **~9x** | **~2x** |
| 递归斐波那契（n=22） | 1x | ~1x | **~18x** | ~1x |

> 数据来源：`tools/benchmark.py`（前三列，M20 后）与 JS VM 补测
> （Node v22，已扣除约 143ms 的 Node 进程启动基线；20000 次整数循环净
> 执行约 15.7ms，斐波那契 n=22 约 57.6ms）。
> JS VM 定位于「跨语言分发 / 语义对齐」，性能与树遍历同量级；
> **C VM 是性能天花板**（纯计算场景比树遍历快 9–46 倍）。
