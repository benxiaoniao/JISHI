# core/ —— 基石前端核心（R 线，Rust）

> **这是「前端换 Rust」的落脚点**（R 线，见内部路径文档）。目前有 **R2 分词器**
> 与 **R3 语法 → AST**。
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
python tools/bench_frontend.py          # 吞吐（分词 + 语法，含端到端天花板推导）
```

## 实测（2026-10-01，2.07 万行 / 735 KB）

| 项 | R2 分词 | R3 语法（源码→AST） |
|---|---|---|
| 与 Python 一致 | 逐 token **88/88**，另 **2 万组**随机对拍 0 分歧 | AST 逐项 **88/88**，另 **5 万组**随机对拍 0 分歧 |
| Python 基线 | 497.7 ms | 823.3 ms |
| 纯核心 | **77.0 ms（6.47x）** | **145.9 ms（5.64x）** |
| 端到端（含转对象） | **137.3 ms（3.63x）** | **270.6 ms（3.04x）** |
| ⚠️ 端到端**天花板** | **4.30x** | **4.73x** |

⚠️ **「端到端 ≥5 倍」是够不着的，接口决定的**（✅ 2026-10-01 拍板把这条验收线
**移到 R5**；本层只认「纯核心 ≥5x」）：把对象构造出来本身就有地板
（11.7 万个 `Token` ≈ 39 ms、7 万个 AST 节点 ≈ 28 ms），加上纯核心之后上限就是
`基线 ÷ (核心 + 地板)`。**真正的收益要等 R5**（下游不再需要 Python 对象）
与 **R8**（整条链路离开 Python）。推导每次都会由 `bench_frontend.py` 重算并打印。

## 下一步

**R5 编译器 → 字节码**（验收 = 字节码逐字节一致 **且
端到端（源码→可执行）≥5x** —— 这条验收线 2026-10-01 从 R2 挪来）。

⚠️ R4（前端报错）**已交付**：契约升 v2（`diagnostics` 补 下划线 / 修正 / **渲染**），
并新增 `tools/probe_errcodes.py` 做**错误码可达性体检** —— 它第一次跑就抓到
「`E0205` 是死码」和「`E0204` 没有语料」两笔。执行期/编译期的报错留给 R5/R6。
