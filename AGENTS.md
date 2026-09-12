# 基石（jishi）编程主轴 —— 所有 AI 编程工具共用

> 这是本项目的**单一编程主轴**。任何 AI 编程工具（WorkBuddy / Cursor /
> Claude Code / GitHub Copilot / CodeBuddy / Codex 等）在介入本项目前，
> **必读本文件**。
>
> 详细论证在 README.md（对外主文档）与 docs/ 下各文件，这里只放
> **工具最需要、最高频、必须遵守**的硬约束与当前任务。本文件是普通 Markdown，
> 任何工具都能读、都能写（尤其「当前任务」节由各工具协作更新）。

---

## 0. 一句话定位

基石 = 一门**中文编程语言**，对标 Python 的易用性。

**北极星（2026-09-05 调整）**：健全语言自身能力，让编程者**方便地实现意图**、
**出问题能快速定位**。「AI 友好」是辅助线，不是主线。

---

## 1. 硬约束（违反会坏事，必须遵守）

1. **三执行器语义必须一致**：树遍历 / Python VM / C VM。
   任何改动语义（语法/内建/方法/异常），**必须先跑 `tests/test_cvm.py` 对拍**。
2. **测试命令**：`python -m pytest tests/`（当前 559 项全绿）。
   改完代码必须跑全量回归，不许留红。
3. **诚实不吹牛**：指标没度量就写「未度量」，绝不用估计值充数；
   架构边界（如字符串是 HOST 句柄）如实标注。
4. **README.md 是唯一对外主文档**：里程碑推进后，必须同步更新它的
   「技术栈声明 / 性能数据 / 能力清单 / 项目结构」四块，否则会「总结偏差」。
5. **README 不写个人路径**（用户名 / 本机 IDE 安装路径 / 受管 Python 路径），
   保持本地与公开版一致，发布时无需反复屏蔽。
6. **新特性准入三关**：真实需求驱动 / 可度量验收 / 投入产出比。
   说不出「怎么算完成」就不开工。

---

## 2. 关键技术备忘（踩过的坑，别重踩）

- **新增宿主回调时**：C 侧 `JsHost` struct 与 Python 侧 `JsHost._fields_`
  字段顺序必须**逐字段对齐**（曾因插错位置导致 ctypes 回调错位崩溃）。
- **异常穿透重入边界**：vm.py 用 `stop_at`、C VM 用 `stop_depth`，两边要一致。
- **Windows venv 路径**是 `Scripts/` 不是 `bin/`。
- **PyInstaller 打包**：入口必须用包外的 `tools/entry_cli.py` 绝对导入，
  直接打包 `jishi/cli.py` 会因相对导入报「no known parent package」。
- **改 C 代码后必须同步 DLL**：重新编译 `python cvm/build.py` 后，还要
  `cp cvm/bin/jsvm.dll jishi/_native/jsvm.dll`——运行时优先加载 `_native/`
  里的 DLL，否则跑的是旧 DLL（曾因此报「未知指令」）。
- **CI 的 Windows 上装 gcc**：不要用 `choco install mingw`（路径不稳定）；
  用 `msys2/setup-msys2@v2`（`msystem: MINGW64` + `install: mingw-w64-x86_64-gcc`
  + `path-type: inherit`）。GitHub runner 预装 MSYS2 但不在 PATH、且不含 gcc 包。
- **Windows 安装程序**：由 Inno Setup 编译（`tools/installer.iss`）；
  ISCC 查找覆盖 Program Files / %LOCALAPPDATA%\Programs（`/CURRENTUSER` 装法），
  优先用较新版本（7 优于 6）。
- **Windows 上打印中文**：CI/Cmd 的 Python stdout 默认非 UTF-8，会
  `UnicodeEncodeError`；脚本入口 `sys.stdout.reconfigure(encoding="utf-8")`，
  workflow 另设 `PYTHONUTF8=1`。同理 `strftime` 传含中文的格式串在 Windows
  Python 3.10 上会崩（见 `jishi/stdlib/日期.py` 的摘出/放回处理）。
- **C VM 性能优化思路（M20 经验）**：先 benchmark + cProfile 定位，别猜。
  真正的瓶颈往往在 **ctypes 边界的数据转换**（逐个构造 `JVal` ≈1µs/值），
  而不是 C 侧循环本身。批量优化手法：整批同类型时一次 `struct.pack` +
  `memmove` 写入 C 数组（见 `cvm_bind._cb_iter_next_batch`），比逐个快 ~9x。
- **推 tag 不要推本地仓库**：本地 git 历史含旧版个人路径，用
  `python tools/publish_github.py --tag vX.Y.Z` 在屏蔽版快照上打 tag。
- **词法器 Token 的 `end_col` 约定不一致（易踩）**：OP 类 token 的区间是
  `[col-1, end_col-1)`，而 NAME/STRING/NUMBER/FSTRING 是 `[col-1, end_col)`。
  按列切片取原文时必须分类处理（见 `jishi/formatter.py` 的 `_respacify`）。
- **格式化器/新工具尽量复用既有分析器**：格式化器用词法器算缩进层级，
  LSP 的关键字/内建/标准库数据取自 `ai.build_lang_spec()`——不要在工具里
  另抄一份语言元数据，否则会与语言本体漂移。
- **URL 里的中文要编码，但别二次编码**：包名是中文，下载地址生成时就得
  编码好（域名走 IDNA，路径走 `quote(path, safe="/%")`）。safe 里必须保留
  `%`，否则已编码的前缀（如 `Path.as_uri()` 的产出）会被再编成 `%25`。
- **`detect_conflicts` 判环只能看「链上是否出现过包名」**：用
  `(名字, 整条链)` 当访问键在环里永不重复，会无限递归（写 M22.3 时踩到）。
- **Windows 下读 stdin 必须按字节 + UTF-8**：打包版不继承 `PYTHONUTF8`，
  `sys.stdin.read()` 会按 GBK 解码，中文源码变乱码（`打印`→`鎵撳嵃`）。
  统一用 `cli._read_stdin()`（字节层读 + 显式 UTF-8 解码），MCP 的
  JSON-RPC 读取与子进程传参同理；入口脚本三个流都要 `reconfigure`。
- **打包版找同级可执行文件**：PyInstaller 冻结后 `sys.executable` 是自己，
  `-m jishi.cli` 会失败；MCP 需改调发行目录同级的 `jishi`（见
  `mcp_server._cli_command`）。发行版布局是 `bin/jishi/` 与 `bin/jishi-mcp/`
  互为兄弟目录。
- **`cvm/include/opcodes.h` 是自动生成的**（`cvm/build.py` 从
  `jishi/opcodes.py` 生成），**手改会被覆盖**。要加常量先加到 `opcodes.py`
  的对应类（如 `ParamKind` / `CmpOp`），再跑 `python cvm/build.py`。
- **改 C 代码的完整流程**：改 `cvm/src/jsvm.c` → `python cvm/build.py`
  → **务必** `cp cvm/bin/jsvm.dll jishi/_native/jsvm.dll`（运行时优先加载
  `_native/`，不拷就是跑旧 DLL）。
- **加语义比较运算符 / 新指令参数可以不动 C**：比较码放 6–9（0–5 是 C VM
  快路径，6 以上回落宿主 `host->compare`）；`UNPACK` 的 `b` 操作数闲置，
  直接拿来传星号位置（M25）。
- **`bind_args` 的所有权语义是「借用」**（M25 起）：实参由调用者释放，
  进槽位的值由 `bind_args` retain。因为 `*参数` 会出现「同一个实参既被打包
  进列表、又留在数组里等统一释放」，借用语义下只需统一 release 一次。
- **新增宿主回调必须追加在 `JsHost` 末尾**（C 与 Python 两侧逐字段对齐）：
  插在中间会让后面所有字段错位，ctypes 直接崩（项目历史上踩过）。
- 更多见 README「已踩过的坑」一节。

---

## 3. 当前任务与状态（各工具协作更新这一段）

- ✅ **M0–M25 全部完成**（语言核心 → AI 工具链 → 沙箱/MCP → 生态 →
  性能分发 → 评测闭环 → 跨语言分发 → 包管理 → 语言收敛 1.0 → 发行版 →
  排错体验补全 → 表达便利 → 发行改进 → 性能优化 → 开发者工具 → 包生态 →
  语言功能 A 档 → 语言功能 B 档 → 变长参数与星号解包）。
- 📌 **M25 已交付（B 档收尾）**：
  - 变长参数 `*参数`（收多余位置实参成列表）/ `**选项`（收未匹配关键字成字典）
  - 星号解包 `令 甲, *余 = …`（星号可在中间或末尾，只能一个）
  - 实现要点：`code_params` 每形参 3→4 个 int32（加 `kind`）；
    `JsHost` 末尾追加 `pack_kwargs`；`bind_args` 改借用语义；
    `UNPACK` 的 `b` 传星号位置；字节码格式版本 1→2
  - **同步了两个跨语言宿主**：`node/`（JS）与 `rust/`（Rust）都实现了
    变长参数与星号解包，保证字节码可移植性
  - **顺带修掉三个既有缺陷**：`//=` `**=` 复合赋值被词法器切错（一直不可用）；
    类方法的类型标注两端都没生效（树遍历漏传 annotations、编译器漏发射校验）；
    `长度/最大/最小` 对自定义可迭代对象不可用
- 📌 **明确边界**：不支持「星号展开」实参（Python 的 `f(*列表)`），
  传列表就是「一个参数」；不支持 `*参数` 之后再加普通参数。
- 📌 **Rust 宿主**：源码已同步（`unpack_values` 星号 + `bind_args` 变长 +
  `param_kind` 字段），但**本机 cargo 无法运行（环境限制）**，
  未做编译与运行验证——需要在有 cargo 的环境复核一次。
- 📌 **下一里程碑 M26：语言功能 C 档**——上下文管理器 `用 … 为` /
  精确小数 / 自定义对象打印（`文本()` 方法）。
- **铁律**：能用内建/标准库实现的，不碰语法；语法特性要三执行器对拍。
- **度量**：`python tools/probe_capabilities.py`（当前 **31/44 可用**）。
- 可按需补的遗留项（不阻塞）：短字符串内联、JS 宿主补齐集合类型、
  调试器、LSP 增量同步。
- 当前版本 **0.1.7**。

---

## 4. 常用命令

```bash
# 运行脚本 / REPL
python -m jishi.cli 文件.jsh
python -m jishi.cli -i

# 测试（必须全绿）
python -m pytest tests/

# 构建 C 虚拟机动态库（改 C 代码后）
python cvm/build.py

# 性能基准
python tools/benchmark.py

# 一键全检
python tools/ci.py

# 打发行包（PyInstaller；Windows 上会自动调 Inno Setup 出 setup.exe）
python tools/build_release.py

# 发布到 Gitee（令牌走环境变量，见 tools/publish_gitee.py 顶部说明）
python tools/publish_gitee.py            # 同步代码
python tools/publish_gitee.py --release  # 同步代码 + 建 Release + 传发行包

# 发布到 GitHub（用本机 Git Credential Manager 认证，无需令牌参数）
python tools/publish_github.py           # 同步代码（屏蔽版快照）
python tools/publish_github.py --dry-run # 只导出+屏蔽+本地提交，不推送

# 打 tag 触发三平台自动构建 + Release（GitHub Actions）
git tag v0.2.0 && git push origin v0.2.0
```

---

## 5. 文档地图（需要深入时按图索骥）

| 文件 | 用途 |
|------|------|
| `README.md` | 对外主文档：进度 / 能力 / 架构 / 决策 / 踩坑 |
| `docs/语言规格.md` | 语法语义规格（v1.0 已冻结） |
| `docs/设计决策.md` | 设计决策 D1–D23 |
| `docs/技术路线_v1.0之后.md` | **后续规划（M17 起）** |
| `docs/tutorial/` | 教程 10 章 |
| `docs/embed.md` | 跨语言嵌入（C/Node/Rust）指南 |
| `examples/projects/` | 真实项目 + 反哺的语法别扭点记录 |
| `PROGRESS.md` | **进度日志主轴**（跨工具，里程碑级进度） |

---

## 6. 协作约定

- 本文件是「约束主轴」，只放高频硬约束和当前任务，不重复 README 细节。
- **进度日志写进 `PROGRESS.md`**（里程碑级，跨工具共享）；详细决策写入
  `docs/设计决策.md`；琐碎过程不写进 PROGRESS.md。
- 「当前任务」节与 PROGRESS.md 的「当前进行中/下一步」在每完成一个里程碑后同步更新。
- 各工具的入口文件（CLAUDE.md / .cursorrules / copilot-instructions.md）
  只是一行指针，**不要**在里面另起炉灶写约束，避免多处维护产生偏差。
