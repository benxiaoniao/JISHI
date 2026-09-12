# 基石 Rust 引擎（M13.3）

在 **Rust** 里独立执行基石字节码，消费 M13.1 的 JSON 字节码，脱离 Python 执行。

## 用法

```bash
# 1. 用 Python 编译出字节码
jishi 你的脚本.jsh --dump-bytecode > 脚本.json

# 2. 用 Rust 执行
cargo run --release -- 脚本.json
```

## 结构

```
rust/
├── Cargo.toml
└── src/
    ├── lib.rs    # jishi_ffi crate：load_module + VM
    └── main.rs   # jishi-rs CLI 入口
```

## 实现说明

与 Node 版（`node/`）对称：消费 JSON 字节码，纯 Rust 解释指令。
自研零依赖 JSON 解析器（`src/json.rs`，只解析字节码格式需要的数据类型），
`enum Val` 表达值。

**当前状态（诚实记录）**：

| 能力 | 状态 |
|------|------|
| 加载 JSON 字节码 + 版本/格式校验 | ✅ |
| 算术 / 比较 / 跳转 / 内建函数 / 列表字典字面量 | ✅ |
| 类定义 / 异常分发框架 | ✅（部分） |
| **可变容器语义**（`列表.追加`、`字典[k]=v`、原地排序等） | ❌ 需 `Rc<RefCell>` 重构 |

**为什么 Rust 版停在「最小可运行」**：Rust 是**值语义**，而基石（同 Python）
的列表/字典是**可变引用语义**。`列表.追加(x)` 在 Rust 里若用 `Val::List(Vec)` 按值
传递，`call_bound` 拿到的是副本，`push` 不反映回原变量。正确实现需把
`Val::List`/`Val::Dict` 改为 `Rc<RefCell<Vec>>`/`Rc<RefCell<Vec<(Val,Val)>>>`，
这是一次较大的类型重构。

Node 版无此问题（JS 对象天然引用语义），这正是「纯 JS 解释器比 Rust 版更容易
完整对齐」的原因——也是 M13.2 先做 Node 的价值所在。

**验收状态**：Rust 工具链已装（1.98.1）、crate 编译通过、零依赖 JSON 解析器
自研完成、能跑纯算术/内建/列表字典字面量脚本。这证明了 Rust 宿主的可行性；
完整可变容器语义留作后续迭代（需 Rc<RefCell> 重构）。
