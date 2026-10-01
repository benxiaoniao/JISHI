# core/ —— 基石前端核心（R 线，Rust）

> **这是「前端换 Rust」的落脚点**（R 线，见内部路径文档）。目前只有 **R2 分词器**。
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
| `src/lib.rs` | Python 扩展入口（pyo3）。**只做搬运**，不做任何语言判断 |

**验收判据不是「看起来对」，是 `tools/conformance.py`**：

```bash
python tools/conformance.py emit --frontend rust --out build/产物目录
python tools/conformance.py compare --from build/产物目录 --stage tokens
python tools/bench_tokenizer.py          # 吞吐（含端到端天花板推导）
```

## 实测（2026-10-01，R2）

| 项 | 结果 |
|---|---|
| 逐 token 一致 | **85/85 语料**，另 **2 万组随机源码对拍 0 分歧** |
| 分词纯核心 | 490.8 → **69.6 ms**（2.07 万行 / 734 KB，**7.05x**） |
| 端到端（含把 Token 交回 Python） | **132.0 ms**（**3.72x**） |
| ⚠️ 端到端**天花板** | **4.23x** —— 见下 |

⚠️ **「端到端 ≥5 倍」是够不着的，接口决定的**：把 11.7 万个 `Token` 对象构造出来
本身就要 46 ms（Python 侧的地板），加上纯核心 69.6 ms，上限是
`490.8 ÷ (69.6 + 46.4) = 4.23x`。**真正的收益要等 R3/R5**（下游不再需要 Python
对象）与 **R8**（整条链路离开 Python）。推导每次都会由 `bench_tokenizer.py` 重算并打印。

## 下一步

**R3 语法 → AST**（验收 = AST JSON 逐字节一致）。难点是**脱糖的时机与位置**
（`超()` / `匹配·情形` / 推导式 / 条件表达式）—— 结构一致不等于行为一致，
所以 R5 的字节码比对不能省。
