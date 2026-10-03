# core/ —— 基石前端核心（R 线，Rust）

> **这是「前端换 Rust」的落脚点**（R 线，见内部路径文档）。已经有
> **R2 分词器** / **R3 语法 → AST** / **R4 前端报错** / **R5 编译器 → 字节码**
> —— **源码 → 词法 → 语法 → 字节码，整条前端链都能在这儿原生跑完**。
> 它**不是**替代 `rust/`（那是跨语言宿主，消费 JSON 字节码跑程序）；
> 这里是**产字节码之前的那半**。

## 怎么构建

```bash
python core/build.py          # 需要 Rust 工具链；产物复制成 jishi/_core.pyd
python core/build.py --check  # 只看看 cargo / dlltool / 扩展在不在
```

产物 `jishi/_core{,.pyd,.so,.dylib}` **不进仓库** —— 它是按**当前解释器的 ABI**
编出来的，换个 Python 就得重编。没有它时 `JISHI_FRONTEND=rust` 会**明确报错**
（绝不静默回落到 Python）。

⚠️ **别手敲 `cargo build`**：pyo3 有两个只在特定环境上才现形的坑，都由
`build.py` 处理掉了（生成 `PYO3_CONFIG_FILE`、找 `dlltool`）——
细节写在 `build.py` 的 docstring 里，看一眼能省一次排查。

## 怎么用

```bash
JISHI_FRONTEND=rust python -m jishi.cli 程序.jsh   # 用 Rust 前端跑（默认 python）
```

## 现在有什么

| 文件 | 内容 |
|---|---|
| `src/tokenizer.rs` | 词法分析器 —— `jishi/tokenizer.py` 的**逐行为移植**，目标是「**逐 token 一致**」而不是「写个更好的」 |
| `src/parser.rs` | 语法分析器 —— `jishi/parser.py` 的**逐行移植**（含全部解析期脱糖：条件表达式 / `超()` / `匹配` / `枚举` / 推导式 / f-string），目标是「**AST 逐项一致**」 |
| `src/compiler.rs` | 编译器 —— `jishi/compiler.py` 的**逐行移植**（作用域收集/定种/全部语句与表达式/脱糖/`fuse_method_call`），目标是「**字节码逐字节一致**」 |
| `src/jishilib.rs` | 基石库层展开（`导入 统计` → `统计 = $造模块("统计", __基石库_统计__())`）—— `jishi/jishilib.py` 的 Rust 版 |
| `src/serialize.rs` | 字节码 JSON（键序/转义/浮点写法都要与 Python 的 `json.dumps` 逐字相同）+ `repr(float)` 复刻 |
| `src/opcodes.rs` | 50 条指令的编号与名字（这份编号的**第三**份拷贝，靠 `opcode_table()` 对拍防漂） |
| `src/sha256.rs` | 算字节码的 `checksum`（零依赖，照 FIPS 180-4 写一份） |
| `src/lib.rs` | Python 扩展入口（pyo3）。**只做搬运**，不做任何语言判断 |

⚠️ **R3 起，Rust 的 `parse` 吃的是「源码文本」而不是 tokens**：AST 只由文本决定，
而 Python 那边的 `source_lines` 是个**有损**载体（增量分词的行列表会去掉末尾空行）。
生产入口是 `jishi.parser.parse_source(源码)`；`parse(tokens, lines, f)` 留给 LSP
增量分词，并带一条**精确守卫**（丢了内容当场报错）。来龙去脉见 `docs/设计决策.md` D56。

**验收判据不是「看起来对」，是 `tools/conformance.py`**：

```bash
python tools/conformance.py emit --frontend rust --out build/产物目录
python tools/conformance.py compare --from build/产物目录 --stage tokens   # R2
python tools/conformance.py compare --from build/产物目录 --stage ast      # R3
python tools/conformance.py compare --from build/产物目录 --stage bytecode # R5
python tools/bench_frontend.py          # 吞吐（分词 + 语法，含端到端天花板推导）
```

## 实测（2026-10-02，2.07 万行 / 735 KB）

| 项 | R2 分词 | R3 语法（源码→AST） | R5 编译（源码→字节码 JSON） |
|---|---|---|---|
| 与 Python 一致 | 逐 token **92/92**，另 **2 万组**随机对拍 0 分歧 | AST 逐项 **92/92**，另 **5 万组**随机对拍 0 分歧 | **字节码逐字节 92/92**（连 `checksum`），另 **3 万组**随机对拍 0 分歧 |
| Python 基线 | 484.3 ms | 860.8 ms | 2035.0 ms |
| 纯核心 | **71.9 ms（6.74x）** | **145.8 ms（5.90x）** | ——（产物是文本，没有「核心/对象」之分） |
| 端到端 | **134.5 ms（3.60x）** | **274.7 ms（3.13x）** | **378.9 ms（5.37x）** ✅ |
| ⚠️ 天花板 | **4.25x** | **4.86x** | **没有天花板** |

⚠️ **R2/R3 的「端到端 ≥5 倍」是够不着的，接口决定的**（✅ 2026-10-01 拍板把这条验收线
**移到 R5**；✅ **R5 已兑现：5.37x** —— 因为字节码的产物是**文本**，
「造对象」那一步不存在，所以没有地板。**不是实现更快了，是接口换了。**）
分词/语法那两层的道理：把对象构造出来本身就有地板
（11.7 万个 `Token` ≈ 39 ms、7 万个 AST 节点 ≈ 28 ms），加上纯核心之后上限就是
`基线 ÷ (核心 + 地板)`。**真正的收益要等 R5**（下游不再需要 Python 对象）
与 **R8**（整条链路离开 Python）。推导每次都会由 `bench_frontend.py` 重算并打印。

## 下一步

**R6 主体继续：VM + 标准库（Rust 原生）**（验收 = 426 项对拍全绿；
**实测后定 C VM 去向**）。

⚠️ **R6.2/R6.3 已把 R6.0 摸出的四个缺口全收掉**（`遍历` O(n²) / 基类接不住子类 /
报错时不吐 stdout / 报错渲染缺源码行），并顺带把「四个执行器的运行期报错」
对齐到 **0 差异**（探针 19/19）。宿主现在能打出与 Python `render()` **逐字相同**的报错。
⏳ **同一件事在 Node 宿主上还没做**（它的渲染仍是「一行标题 + 一行位置」）——
那是下一步的候选活。

⚠️ **R5（编译器 → 字节码）已交付**，四阶段全绿、字节码逐字节相同。
🔴 **移植时抓到一个既有 bug**（与 R5 无关）：编译器的名字收集没有 `Try` 分支，
「只在 `尝试` 块里赋过值的名字」会被编成全局 → **三执行器分岔**。
R5 逐字复刻，**R5.1 已全部修掉**，详见 内部前端契约（未公开） §12.4 与
`docs/设计决策.md` D59。
