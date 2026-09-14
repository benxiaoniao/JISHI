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
2. **测试命令**：`python -m pytest tests/`（当前 1417 项全绿）。
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
- **「三执行器语义分歧」的检查要主动做**（M35 踩到）：类体里写非法语句时，
  树遍历报错、而两个字节码执行器**静默忽略**（`x += 5` 甚至被当成 `x = 5`
  静默算错）。凡是「只在某一个执行器里才出现的行为」，都值得写个小脚本
  三路各跑一次对比。**修法是放到解析期**：三个执行器都要过 parser，
  一处校验就全一致（与「解析期脱糖」同一思路）。
- **空块在基石里是单独一行 `:`**，没有 Python 的 `pass` 关键字
  ——`通过` 只是个普通名字（曾把它误当关键字，见 M35）。
- **测试里不许出现「开发机事实」**（M35：CI 长期全红的唯一根因就是这个）：
  Rust 可执行名（Windows 是 `jishi-rs.exe`，别处是 `jishi-rs`——用
  `conftest.rust_exe_path()`）、`build/` 目录（CI 全新检出没有它，conftest 负责建）、
  平台名（别写 `"Windows"`）、**时区**（别写 UTC+8 的 `08:00:00`，runner 是 UTC）。
  写测试时凡遇「我这台机器上是什么」，都要问一句「CI 上是什么」。
- **CI 失败怎么查**：两个工作流里有「失败摘要 → `::error` 注解」+「环境诊断」，
  注解可由公开 API 读（`check-runs/annotations`），不必登录网页下 job 日志
  （那要管理员权限）。注意注解步骤需 `set +e`，且要抓 pytest 的 `E ` 行才有
  完整消息。
- **解析期脱糖优先**（M26.2）：新语法如果能写成「既有语句的组合」，就让它
  在 parser 里脱糖、返回**多条语句**（`_parse_statement` 可以返回 list，
  `_parse_statements` 会展平），三执行器零改动、行为必然一致。
  `超()`（M23）、集合字面量（M24）、`用 … 为`（M26）都是这个手法。
- **脱糖时别把「绑定后的值」当成原对象**（M26 踩到）：`用 X 为 v：` 里
  `v` 是 `进入()` 的返回值，可能不是 `X`；清理必须针对 `X`，所以要留一个
  临时变量 `__用_N__`。这类错误**静默**（清理不报错，只是没生效），
  只能靠「对照 Python 语义写测试」发现。
- **`cargo` 什么都不做 → 先看 shim 是不是 0 字节**（2026-09-13 查明）：
  rustup 在 Windows 上把 `~/.cargo/bin/cargo.exe` / `rustc.exe` 等做成指向
  `rustup.exe` 的**硬链接**。链接一旦坏掉就退化成 **0 字节空文件**，
  执行它「成功且无输出且 `rc=0`」——极像「命令被沙箱吞了」，很容易误判成环境限制。
  诊断：`ls -la ~/.cargo/bin/`（真身 14MB，坏的 0 字节）；
  真身在 `~/.rustup/toolchains/<toolchain>/bin/`。
  修复：`ln -f rustup.exe cargo.exe`（每个名字来一次，等价于复制）。
  另外 `cargo build` 的产物时间戳是判断「到底编没编」的硬依据：
  `touch` 源文件后重新 build，exe 时间戳不变就说明根本没编译。
- **Rust 宿主（`rust/`）的验证方式**：`windows-gnu` target 用 gcc 当链接器，
  构建前把提供 gcc 的目录加进 PATH（或用 `JISHI_MINGW` 指定）。
  跑 `tests/test_m27_rust_host.py` 做跨宿主对拍（无 cargo 时自动跳过）。
  **不写死本机路径**——沿用「README/文档不写个人路径」的约定，
  否则 `tools/publish_github.py` 的隐私终检会拦下发布。
- **Rust 侧可变容器已修（M28）**：容器是 `Rc<RefCell<Vec<…>>>`，方法里用
  `borrow_mut()` 改共享数据（以前是裸 `Vec`、改的是副本 → **静默算错**）。
  配套要注意两点：① 「列表自身当迭代器」的实现必须**显式快照**
  （`make_iterator` 里 `list_new(l.borrow().clone())`），否则 `遍历` 会把
  原列表搬空；② 类/函数是值类型、**没有对象身份**，对它们用
  `是`/`不是` 会明确报错（不做「按内容相等」的静默降级）。
  改动渲染前先看 `docs/embed.md` 的支持程度表。
- **Rust 宿主：凡是「改了就希望调用方看见」的东西都要 `Rc<RefCell<…>>`**（M29）。
  实例也不例外——`Instance` 一开始是值类型，于是 `自身.字段 = 值` 走 `SET_ATTR`
  改的是**副本**，方法里的赋值全部**静默失效**。换成 `JInst = Rc<RefCell<Instance>>`
  之后，`是`/`不是` 也顺带有了对象身份。
- **Rust 宿主：内建要回调用户函数，靠 `VM::call_value`**（M29）。
  做法是压一个帧、在**自己的帧深**上停下（`execute(Some(depth))`），返回值留在栈顶。
  两个必须注意的点：① 回调前先**快照**容器内容——拿着 `RefCell` 的借用去回调，
  回调里又改同一个容器就会撞上运行时借用冲突；② 异常不会漏到外层帧，
  因为 `dispatch_exception` 见到 `stop_at` 就返回。
- **Rust 宿主：异常与 finally 的三处关键实现**（M29 补齐，此前全是坏的）：
  ① `execute` 里 `Err(e)` 必须走 `dispatch_exception`——直接 `return Err(e)`
     会让 `尝试/捕获/最终` 对**内置错误**完全失效；
  ② `返回` 要经过 `step_return` 先跑 finally（返回值挂进 `frame.pending_return`，
     在 `END_FINALLY` 收尾），对应 Python 侧的 `_return_via_finally`；
  ③ 循环信号（`中断`/`继续`）要穿过 finally 时，用 `Val::LoopSignal` 借道值栈，
     `CHECK_SIGNAL` 判它、末尾的 `THROW` 还原成信号；「最近的循环 vs 最近的 try」
     谁先接，由登记序号 `block_seq` 比较决定（对齐 Python 的 `_Handler.seq`）。
- **加关键字的检查清单**（M34）：① 它是不是常用词**前缀**？是的话加进
  `tokenizer._GLUE_CHECK_SKIP`（否则 `匹配组`、`枚举值` 这类标识符会被
  粘连检查拦下）；② 有没有**既有 API 用了这个词当属性名**？（`正则.匹配`
  被撞过——`.` 后面的属性名已放开接受关键字，但新写的库函数仍要留心）。
- **「脱糖」时不要把两种语义不同的写法塞进同一个分支**（M34）：模式匹配里
  「通配兜底」（必须最后）与「只有条件的守卫」（可放中间）长得像、语义不同，
  混在一起就堵死了守卫链写法。**写原型时先把每种写法的语义分开列一遍**。
- **宿主侧「值→文本」这类映射表是漏点重灾区**（M33/M34 各一次）：
  `类型()` 的名字在三个宿主里三个样（`_BoundMethod` / `BoundMethod` /
  「内建方法」）。加新类型时三处都要对一遍。
- **`Val` 多一支就要到处补**（M33）：Rust 侧新增 `Date`/`Table`/`Slice`
  三支时，每支都得在 `display` / `type_label` / `identity_key` / `PartialEq`
  里各接一次——漏一处就是「静默给错」。
- **`partial_cmp` 返回 `None` 会被排序当成「相等」**（M33）：
  `列表.排序` 用的是 `partial_cmp().unwrap_or(Equal)`，所以**漏实现一种类型的
  比较 = 那种类型的排序静默失效**（字符串、列表都栽过）。
  新增类型时要同时想「它能不能比大小」。
- **对拍脚本里不要用 `text=True` 收子进程输出**（M33）：它默认做通用换行，
  会把 `\r\n` 悄悄改成 `\n`，于是「输出换行不一致」这类问题**永远看不出来**
  （M30/M31 的端到端对拍就是这样漏掉的）。要按字节收再显式解码。
- **不要在 ctypes 边界上相信「赋给 c_int64 会自动检查范围」**（M32）：
  ctypes 是**静默截断**——`2 ** 64` 变成 `0`、`i64_MAX + 1` 变成负数。
  C 侧即使做了 `__builtin_*_overflow` 回落，回落算完之后仍会在边界丢。
  所有 Python→ctypes 的整数转换都要自己检查范围。
- **JS 双精度是「静默算错」的大户**（M32）：`2 ** 64` 给
  `18446744073709552000`、`9223372036854775807` 显示成 `...776000`。
  对策是**按需切 BigInt**（任一边是 BigInt，或 number 结果超出
  `Number.isSafeInteger`），而不是「一律用 BigInt」——后者会拖慢所有整数运算。
  另外字节码里的 int 常量超出安全范围时要按**字符串**编码（JSON 数字在 JS 里
  解析时就丢精度，reviver 也救不回来）。
- **跨宿主的三条「平台默认行为」坑**（M31 一次踩齐）：
  ① **Node 没有内置 zip**——`zlib` 只有 deflate 流，容器要手拼（见 vm.js 的
     `writeZipEntry`/`centralEntry`/`readZipEntries`）；zip 条目名必须用 `/`
     （Python 的 zipfile 也会把 `\` 换掉）。
  ② **Node 没有同步 HTTP**——用 `spawnSync(process.execPath, ['-e', 脚本])`
     起自身子进程跑 `fetch`，stdout 回传；这是基石没有 `await` 时的唯一解法。
  ③ **Python 的文本模式会转换换行**（Windows 上 `
`→`\r\n`）——
     基石标准库统一 `newline=""` 保真，否则「同一份程序到哪都一样」就成了空话。
- **跨宿主的「码值映射表」是漏点重灾区**（M28/M30 各撞一次）：加了一个语法
  特性后，Python 侧改了 `opcodes.py`，但宿主侧的表往往只写到旧范围——
  Rust 的 `compare(op)`、JS 的 `CMPOP` 都只处理 0–5，于是 `在/不在/是/不是`
  （6–9）静默落空。**改完这类表要跑横向探测脚本**，别信文档里的 ✅。
- **JS 宿主的三个「看着能跑其实不对」**（M30 实测出来的）：
  ① `Boolean([])` 是 `true`，而 Python `bool([])` 是 `False` → 空容器判断反了；
  ② `===` 是引用比较，容器 `==` 得自己递归按值比；
  ③ 内建 `Set` 的相等是 SameValueZero，与 Python 的 `hash/eq` 不同
     （`{1, 1.0, 真}` 在 Python 里只留一个）→ 集合要自己维护规范化键。
- **Rust 精确小数是零依赖手写 BigInt**（`rust/src/decimal.rs`，M29）：
  系数 base 10^9 小端、28 位有效数字、ROUND_HALF_EVEN。**`**` 只支持整数指数**——
  非整数次幂是近似运算（Python 的 `Decimal` 走 `context.power`），各执行器必然漂移，
  所以报中文错而不是给近似值。
- 更多见 README「已踩过的坑」一节。

---

## 3. 当前任务与状态（各工具协作更新这一段）

- ✅ **M0–M34 全部完成**（语言核心 → AI 工具链 → 沙箱/MCP → 生态 →
  性能分发 → 评测闭环 → 跨语言分发 → 包管理 → 语言收敛 1.0 → 发行版 →
  排错体验补全 → 表达便利 → 发行改进 → 性能优化 → 开发者工具 → 包生态 →
  语言功能 A 档 → 语言功能 B 档 → 变长参数与星号解包 → 语言功能 C 档 →
  值→文本统一 → Rust 可变容器 → Rust 宿主补齐 → JS 宿主补齐 →
  JS 标准库补齐到与 Python 同宽 → 边界清单核实与静默算错清零 →
  Rust 标准库补齐到与 Python 同宽 → D 档第一批：模式匹配 + 枚举）。
- ✅ **M0–M35 全部完成**（语言核心 → AI 工具链 → 沙箱/MCP → 生态 →
  性能分发 → 评测闭环 → 跨语言分发 → 包管理 → 语言收敛 1.0 → 发行版 →
  排错体验补全 → 表达便利 → 发行改进 → 性能优化 → 开发者工具 → 包生态 →
  语言功能 A 档 / B 档 / C 档 → 变长参数与星号解包 → 值→文本统一 →
  Rust 宿主补齐（含标准库同宽）→ JS 宿主补齐（含标准库同宽）→
  边界清单核实与静默算错清零 → D 档第一批：模式匹配 + 枚举 →
  发布前验证 + 语言图标进安装包 + 三平台 Release + 遗留收尾）。
  **里程碑明细（M30–M34 及各轮教训）见 `PROGRESS.md` 末尾「里程碑明细」；
  详细论证见 README 与 `docs/`。**
- 📌 **M35 已交付（发布与收尾）**：
  - **语言图标进发行物**：`tools/make_logo.py` 从作者 PNG 生成多尺寸
    `assets/基石.ico`（16/24/32/48/64/128/256 七档）与 `assets/基石.icns`；
    `build_release.py` 给 PyInstaller 传 `--icon`（Win/macOS）并把图标
    复制进发行目录、写出 Linux 桌面项 `share/基石.desktop`；
    `installer.iss` 用 `SetupIconFile` + 快捷方式/卸载项 `IconFilename`。
  - **类体改为解析期统一校验**：原先三执行器对类体里的非法语句**各不相同**
    ——树遍历报错、两个字节码执行器**静默忽略甚至静默算错**
    （`x += 5` 被当成 `x = 5`）。现在 parser 一处拦下，三执行器自动一致；
    顺带让空类占位（**单独一行 `:`**，基石没有 `pass` 关键字）三处都合法。
  - **测试临时文件固定名改为带进程号**（曾造成三次「假失败」），
    并行跑同一测试文件不再互相覆写。
  - **文档一致性**：清理 README 路线图节的重复残段与旧数字（27/43、9/38）；
    本文件瘦身——里程碑明细迁入 `PROGRESS.md`；更正
    「本机 cargo 无法运行」的过时记录（cargo 1.98.1 可用，M34 后已实际运行验证）。
- 📌 **M36 起的方向（重要）**：语法面已基本收敛，主线从「扩语法」转为
  **收敛（把探针做成「无悬案」→ 语法冻结）+ 可信工具链（调试器 / LSP）
  + 真实项目验证**。完整规划见 **`docs/路线图.md`**（唯一路线图）。
- 📌 **遗留项已逐条判定**（做 / 有替代 / 不做，没有含糊搁置），清单见
  `docs/路线图.md`「遗留项清单」。「短字符串内联」暂不做（无实测瓶颈
  就不优化）；「调试器」「LSP 增量同步」纳入 M36+ 工具链主线。
- **铁律**：能用内建/标准库实现的，不碰语法；语法特性要三执行器对拍。
- **度量**：`python tools/probe_capabilities.py`（当前 **42/50 可用**；缺口 8 项**全部已判定**，探针汇总行会直接说「无悬案」）。
  **语法面已冻结**（M36，2026-09-14）：新增语法要走准入三关 + 关 0，
  默认先问「能不能用内建或标准库实现」。
- 可按需补的遗留项（不阻塞）：短字符串内联、调试器、LSP 增量同步、
  树遍历的类体 `通过`、非 Windows 平台的 https。
- 当前版本 **0.1.17**。

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
| `docs/设计决策.md` | 设计决策 D1–D27 |
| `docs/路线图.md` | **唯一的路线图**（M0–M35 总表 + M36 起规划 + 不做清单 + 度量） |
| `docs/tutorial/` | 教程 10 章 |
| `docs/embed.md` | 跨语言嵌入（C/Node/Rust）指南 |
| `examples/projects/` | 真实项目 + 反哺的语法别扭点记录 |
| `PROGRESS.md` | **进度日志主轴**（跨工具，里程碑级进度；末尾是里程碑明细与三份旧路线图的历史归档） |

---

## 6. 协作约定

- 本文件是「约束主轴」，只放高频硬约束和当前任务，不重复 README 细节。
- **进度日志写进 `PROGRESS.md`**（里程碑级，跨工具共享）；详细决策写入
  `docs/设计决策.md`；琐碎过程不写进 PROGRESS.md。
- 「当前任务」节与 PROGRESS.md 的「当前进行中/下一步」在每完成一个里程碑后同步更新。
- 各工具的入口文件（CLAUDE.md / .cursorrules / copilot-instructions.md）
  只是一行指针，**不要**在里面另起炉灶写约束，避免多处维护产生偏差。
