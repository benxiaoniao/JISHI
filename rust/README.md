# 基石 Rust 引擎

在 **Rust** 里独立执行基石字节码：消费 `--dump-bytecode` 产出的 JSON 字节码，
脱离 Python 运行。**零第三方依赖**（`Cargo.toml` 的 `[dependencies]` 是空的）。

## 用法

```bash
# 1. 用 Python 编译出字节码
jishi 你的脚本.jsh --dump-bytecode > 脚本.json

# 2. 用 Rust 执行（脚本参数直接跟在后面，会进 `系统.参数()`）
cargo run --release -- 脚本.json 参数1 参数2
```

## 结构

```
rust/
├── Cargo.toml
└── src/
    ├── lib.rs        # jishi_ffi crate：load_module + VM
    ├── main.rs       # jishi-rs CLI 入口
    ├── json.rs       # 零依赖 JSON 解析器（给字节码用）
    ├── decimal.rs    # 精确小数（自写 BigInt：base 10^9 小端）
    ├── crypto.rs     # 加密：自写 md5 / sha1 / sha256 / sha512
    ├── regex.rs      # 正则：自写回溯引擎
    ├── inflate.rs    # DEFLATE 解压（三种块都支持）
    ├── zip.rs        # 压缩：手写 zip 容器（写 stored + 读 deflate）
    ├── http.rs       # 网络：Windows 绑 WinHTTP，其余平台裸 TCP
    └── stdlib/       # 标准库其余模块（每模块一个文件）
        ├── mod.rs        # 分发 + 共享辅助（参数检查、取值转换）
        ├── math.rs       # 数学
        ├── text.rs       # 文本
        ├── rand.rs       # 随机
        ├── path.rs       # 路径
        ├── files.rs      # 文件
        ├── datetime.rs   # 日期 / 时间（含本地时区 FFI 与 strftime 子集）
        ├── tables.rs     # 表格（自写 CSV）
        ├── jsonmod.rs    # json（自写序列化 + 独立解析器）
        └── system.rs     # 系统
```

## 能力现状（按实测，不按印象）

| 能力 | 状态 |
|------|------|
| 加载 JSON 字节码 + 版本/格式校验 | ✅ |
| 算术 / 比较 / 跳转 / 内建函数 / 列表字典集合字面量 | ✅ |
| 函数 / 递归 / 闭包 / 匿名函数 / 默认参数 / 变长参数 / 星号解包 | ✅ 对拍过 |
| 类定义 / 方法调用 / 继承覆写 / 自定义对象打印 | ✅ 对拍过 |
| 可变容器语义（`列表.追加` 反映回原变量） | ✅ `Rc<RefCell<…>>` |
| 精确小数（28 位有效数字、科学计数法显示） | ✅ 对拍过 |
| 异常（`尝试/捕获/最终`）、资源清理（`用 … 为`） | ✅ 对拍过 |
| **标准库 14 个模块**（与 Python 侧同宽） | ✅ 对拍过（M33） |
| 切片 `a[1:3]` / `a[::-1]` / 切片赋值 | ✅ 对拍过（M33） |
| `输入()` 交互读入 | ✅ 对拍过（M33） |
| 列表排序 / `最大` / `最小`（含字符串与「列表当键」） | ✅ 对拍过（M33） |
| **类变量**（`类 A：x = 1` 与 `A.x = 5`，含继承） | ✅ 对拍过（M34 补） |
| 模式匹配 `匹配` / `情形`（解析期脱糖，宿主侧零改动） | ✅ 对拍过（M34） |
| 枚举 `枚举`（同上） | ✅ 对拍过（M34） |
| `类型()` 对实例/类/方法给带名字的结果（`A 实例` / `类 A` / `方法`） | ✅ 对拍过（M34 补） |

**测试**：`tests/test_m27_rust_host.py`（语言语义 101 项）、
`tests/test_m33_rust_stdlib.py`（标准库 58 项）、
`tests/test_m32_boundaries.py`（边界 40 项）、
`tests/test_m34_pattern_enum.py`（新语法 37 项，五路对拍）——全部与 Python VM
**逐字节对拍**。
另有 `examples/projects/` 三个真实项目的端到端对比（成绩分析 / 日志统计 / 网页采集）。

## 零依赖意味着要自己写什么

| 模块 | 别人有的 | 这里怎么做 |
|---|---|---|
| `正则` | `re` / `RegExp` / `regex` crate | **自写回溯引擎**（`regex.rs`）：字面量、字符类、`*+?{m,n}` 与非贪婪、分组、`(?=)`/`(?!`、锚点、`\b`、反向引用、`\d\w\s`（按 Unicode 判） |
| `加密` | `hashlib` | **自写四种摘要**（`crypto.rs`）。规范很死（常数表都是标准里的），拿 `hashlib` 当裁判逐字节比 |
| `压缩` | `zipfile`（zlib） | 打包写 **stored** 条目（没有 deflate 压缩器）；解压自写 **inflate**（三种块都支持，读得了 Python/Node 打的包） |
| `网络` | `urllib` / `fetch` | Windows 绑 `winhttp.dll`（`extern "system"`，**不是 crate**）→ https 也能用；其余平台裸 `TcpStream`（只有 `http://`，https 明确报错） |
| `表格` | `csv` | 自写 CSV 引号转义 + BOM（对齐 Python 默认方言：`\r\n` 行分隔） |
| `日期`/`时间` | `strftime` | 自写子集（`%U`/`%W` 照 CPython 公式）；本地时区绑 `GetLocalTime` / `localtime_r` |
| `系统版本` | `platform.version()` | Windows 绑 `RtlGetVersion`（`GetVersionEx` 会撒谎，说自己是 6.2）；Unix 取 `uname -v` |

## 踩过的坑（都在代码注释里，这里列索引）

- **值语义 vs 引用语义**：容器与**实例**都必须 `Rc<RefCell<…>>`。实例不共享时
  `自身.字段 = 值` 改的是副本，**方法里的赋值全部静默失效**（M28/M29）。
  「容器当迭代器」还要显式快照，否则 `遍历 x 在 a` 会把 `a` 逐元素吃掉。
- **异常与 finally**：`execute` 里 `Err(e)` 必须走 `dispatch_exception`；`返回`
  要先跑 finally（`frame.pending_return`）；循环信号穿过 finally 时借
  `Val::LoopSignal` 走值栈，`block_seq` 决定「最近循环 vs 最近 try」谁先接（M29）。
- **整数溢出**：`i64` → `i128` + `checked_*`，到边界**报错**而不是回绕（M32）。
- **`partial_cmp` 漏了字符串与列表** → `列表.排序` 把「不可比」当成「相等」，
  排序**静默失效**（M33 拿真实项目跑端到端才撞出来）。
- **`Val` 的比较/显示/类型名是「新增一支就得到处补」的**：M33 加了
  `Date` / `Table` / `Slice` 三支，每支都要在 `display` / `type_label` /
  `identity_key` / `PartialEq` 里各接一次——漏一处就是「静默给错」。
  这条在 M34 又验证了一次（`类型()` 对实例/类给的名字与 Python 不一样）。
- **`中断`/`继续` 时不要弹循环块**（M34 修）：弹的动作归 `POP_BLOCK`。
  `dispatch_signal` 里多弹一次，会导致「`继续` 之后再也 `中断` 不了」
  （报「中断/继续没有对应的循环」）。这个 bug 单支/两支 `如果` 里看不出来，
  要有「`继续` 过了还要用循环」才暴露——`情形` 脱糖恰好生成了那种形状。
- **类变量要 `Rc<RefCell<…>>`**（M34）：`Instance` 里存的是 `Class` 的**值拷贝**，
  用普通 `HashMap` 的话 `类名.计数 = 5` 只改到类自己那份，已造出的实例看不到。
  与 M28/M29 给容器、实例换共享是同一个理由。
