# 基石 JS 引擎（M13.2）

在 **Node.js** 里独立执行基石字节码，**无需 Python、无需 C VM、零第三方依赖**。

## 原理

```
基石源码 ──(Python: jishi --dump-bytecode)──> 字节码 JSON ──(Node)──> 输出
```

- Python 侧用 `jishi --dump-bytecode` 把源码编译成平台无关的 JSON 字节码（M13.1）；
- Node 侧 `index.js` 加载字节码，`vm.js` 逐条解释指令，`runtime.js` 提供语义
  （内建函数 / 列表字典方法 / 类与继承 / 异常匹配 / 浮点装箱）。
- 语义与 Python VM（`jishi/vm.py`）逐字节对齐，由 `tests/test_m13_node.py` 对拍保证。

## 用法

```bash
# 1. 用 Python 编译出字节码（一次性，可分发）
jishi 你的脚本.jsh --dump-bytecode > 脚本.json

# 2. 用 Node 执行（无需 Python 环境）
node node/index.js 脚本.json

# 自测
node node/index.js --self-test
```

## 文件结构

```
node/
├── index.js       # 入口：加载 JSON 字节码 + 解码 + 执行
├── vm.js          # 指令分发（48 条指令）
├── runtime.js     # 语义层（内建/方法/类/异常/浮点装箱）
└── package.json
```

## 支持范围

**已支持**（与 Python VM 对拍一致，71 个 SNIPPETS + 10 个 cases 全通过）：

- 全部 48 条指令（算术/比较/跳转/调用/闭包/类/异常/解包/推导式…）
- 12 个内建函数（打印/长度/范围/总和/最大/最小/类型/反转/整数/小数/文本/输入）
- 列表 10 方法、字典 7 方法、字符串 11 方法
- 类与继承（`类 X 继承 Y`、方法覆盖）
- 异常（抛出/捕获/最终、基类捕获抓子类）
- 闭包、默认参数、多赋值解包、文本插值
- 标准库子集：数学（开方/幂/阶乘/最大公约数…）、文本（拼接/重复/判断）、随机（随机整数/洗牌…）

**暂不支持**（诚实边界）：

- `导入 x 从 python`（Python 生态桥接）—— JS 引擎无 Python 运行时，报中文错误；
- `导入 x 从 本地包`（本地包）—— 同上；
- 浮点尾零显示：JS 的 number 是单精度语义，靠 `FloatBox` 装箱区分整数/浮点，
  已对齐 Python 的 `5.0` 显示，但极端的浮点精度/大整数行为与 Python 有差异。

## 为什么是「纯 JS 解释器」而非「ffi 加载 jsvm.dll」

路线图 M13.2 原文写的是「Node 用 ffi-napi 绑 jsvm.dll」。但调研后发现
jsvm.dll 是**宿主驱动**架构（不链接 libpython，全部 ~40 个语义回调都要宿主实现），
ffi 方案需要把 `runtime.py` 的语义整个翻译成 JS，工作量更大、更易错。

而 JSON 字节码是纯数据（常量池只含标量），JS 直接解释它只需翻译指令分发 +
12 个内建 + 方法表，工作量约 1/3，且真正零依赖。这是更贴合「跨语言字节码分发」
本意的做法。
