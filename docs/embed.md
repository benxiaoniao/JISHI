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

Rust 宿主同样消费 JSON 字节码，自行解析后解释执行（零第三方依赖，
`rust/Cargo.toml` 的 `[dependencies]` 是空的），`rust/` 目录提供
`jishi-ffi` crate。

### 构建与运行

```bash
cargo build --manifest-path rust/Cargo.toml --release
# 产物：rust/target/release/jishi-rs.exe
rust/target/release/jishi-rs.exe 字节码.json   # 由 jishi --dump-bytecode 产出
```

工具链是 `stable-x86_64-pc-windows-gnu`，链接器用 gcc（本机由 CLion 自带的
MinGW 提供，构建时把它加进 PATH 即可）。

> **踩过的坑（2026-09-13）**：`cargo` 命令一直「执行成功但什么都不做」。
> 原因是 `~/.cargo/bin/` 下 13 个文件（`cargo.exe` / `rustc.exe` …）
> 都是 **0 字节**——rustup 在 Windows 上把这些做成指向 `rustup.exe` 的硬链接，
> 链接坏掉后退化成空文件，于是执行它没有任何输出且 `rc=0`（很像「命令被沙箱
> 吞掉了」）。真身在 `~/.rustup/toolchains/<toolchain>/bin/`。
> 修复：`ln -f rustup.exe cargo.exe`（依次重建 13 个），
> 或用「把 `rustup.exe` 复制成这些名字」的等价做法。

### 曾经的「最小可运行」是夸大的（已如实修正）

修好 cargo、第一次真正跑起来后立刻发现：**函数调用整个是坏的**。
`Flow::Return` 被当成两种含义——「本帧结束请弹帧」和「刚压了新帧别弹」——
于是每次调用后新帧立刻被弹掉，外层去执行那个还没拿到实参的 `CALL`，
`fn_base` 下溢成 `usize::MAX` 直接 panic。修前连
`函数 加一(x)：返回 x + 1` + `打印(加一(1))` 都跑不了。

也就是说：M13.3 到 M27 之间，Rust 宿主**只有编译验证，没有运行验证**
（`tests/` 里只有 Node 的对拍）。现在补上了
`tests/test_m27_rust_host.py`（21 条对拍 + 5 条 xfail 记录缺口）。

## 5. 支持范围与边界（诚实记录）

三个引擎（Python / C / JS）语义一致的部分由 `tests/test_cvm.py` 对拍保证。
各宿主的能力边界：

| 能力 | Python VM | C VM | JS VM | Rust VM |
|------|-----------|------|-------|---------|
| 值→文本统一（空/真/假 中文，顶层文本不带引号） | ✅ | ✅ | ✅（对拍过） | ✅（对拍过） |
| 变长参数 `*` / `**`、星号解包 | ✅ | ✅ | ✅（对拍过） | ✅（对拍过） |
| 函数 / 递归 / 闭包 / 匿名函数 / 默认参数 | ✅ | ✅ | ✅ | ✅（对拍过） |
| 基础控制流（如果 / 遍历 / 循环 n 次 / 当） | ✅ | ✅ | ✅ | ✅（对拍过） |
| 赋值解包 `令 a, b = …`、文本插值 | ✅ | ✅ | ✅ | ✅（对拍过） |
| 集合类型 `{1, 2}` / `集合(…)` | ✅ | ✅ | ❌ | ❌ |
| 列表高阶方法 `映射` / `过滤` / `归约` | ✅ | ✅ | ❌ | ❌ |
| 类型标注的参数校验 | ✅ | ✅ | — | ❌ |
| 自定义对象打印（`文本()` 方法） | ✅ | ✅ | ✅（对拍过） | ❌ 落回 `<类名 实例>` |
| 上下文管理器 `用 … 为` | ✅ | ✅ | ✅（对拍过） | ❌ |
| 文件对象（`打开` + 读/写/行/关闭） | ✅ | ✅ | ✅（对拍过） | ❌ |
| 精确小数（`精确(…)`） | ✅ | ✅ | ✅（对拍过） | ❌ |
| 用户方法调用（`对象.方法()`） | ✅ | ✅ | ✅ | ❌ 报「需经 VM 帧」 |
| **可变容器语义**（`列表.追加` 反映回原变量） | ✅ | ✅ | ✅ | ⚠️ **静默算错** |
| `导入 x 从 python`（Python 生态桥接） | ✅ | ✅（宿主实现） | ❌ | ❌ |
| `导入 x 从 本地包` | ✅ | ✅（宿主实现） | ❌ | ❌ |
| 浮点尾零显示 `5.0` | ✅ | ✅ | ✅（FloatBox 装箱） | ✅ |
| 大整数（任意精度） | ✅ | ✅ | ⚠️ 需 BigInt 扩展 | ⚠️ 需 BigInt 扩展 |

> **为什么 Rust 宿主落后一档**：`rust/` 的定位是「最小可运行」（见
> `rust/Cargo.toml`：零第三方依赖），而上面几项要新增 `Val` 枚举变体并补
> 全部 `match` 分支，其中精确小数还需要任意精度整数——零依赖约束下得手写
> BigInt（数百行）。

> **可变容器是「静默算错」，优先级最高**：`Val` 是值类型（`Clone`），
> `get_attr` 对列表会**拷一份出来**再调方法，所以 `a.追加(3)` 改的是副本、
> 原变量纹丝不动——**不报错、结果就是错的**。推导式（内部走 `列表.追加`）
> 因此返回空列表。修它需要把 `List` / `Dict` 换成 `Rc<RefCell<…>>`，
> 是一次贯穿全文件的重构（`Val` 的 `Clone` 语义、模式匹配的解引用都要跟着改）。
> 在这之前，凡是用了可变容器的程序在 Rust 宿主上会**静默给出错结果**，
> 使用时务必留意——`tests/test_m27_rust_host.py` 里有 xfail 专门标着它。

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
