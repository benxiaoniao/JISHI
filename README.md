<div align="center">

<img src="assets/基石_logo.png" alt="基石 logo" width="200">

# 基石（jishi）

一门像 Python 一样简单易用的**中文编程语言**。

</div>

> **技术栈声明（公开透明）**
> - 前端（词法/语法/AST/中文报错）由 **Python 实现**
> - 三个执行器：**树遍历解释器**（默认，Python）/ **Python 字节码 VM** /
>   **C 字节码 VM**（`cvm/`，自研 C 栈式虚拟机，gcc 编成 DLL/SO，
>   纯计算场景提速最高 47x，wheel 内自带无需本机 gcc）
> - 通过桥接层**可调用任意 Python 库**，标准库为中文薄封装
> - 不 fork、不 patch CPython，也不宣称"完全自主"，以上技术栈如实标注
>
> 已是一条「拿到就能写」的完整通用语言：模块系统、面向对象、异常、包管理雏形、
> AI 原生工具链（语言卡 / 结构化报错 / 评测集）、沙箱 + MCP 集成、wheel 内带 C VM。
> 定位不是教学玩具，长期目标是可维护的开源项目。

## 安装

**Windows**：下载 `jishi-<版本>-windows-x64-setup.exe`，双击安装即可
（用户级安装、免管理员；简体中文向导，含开始菜单快捷方式、可选加入 PATH、
标准卸载程序）。

**Linux / macOS**：下载对应压缩包，解压后运行 `install.sh`：

```bash
tar -xzf jishi-<版本>-linux-x64.tar.gz
cd jishi-<版本>-linux-x64 && bash install.sh
```

**pip 安装**（需本机 Python 3.10+）：

```bash
pip install jishi
```

发行包下载见 [GitHub Releases](https://github.com/benxiaoniao/JISHI/releases)，
每个版本附 `SHA256SUMS` 便于校验完整性。

## 快速上手

```bash
# 运行一个 .jsh 文件
jishi examples/01_猜数字.jsh

# 交互式 REPL
jishi -i

# 查看 AST（调试用）
jishi examples/01_猜数字.jsh --ast
```

第一个程序 `你好.jsh`：

```
# 你好，基石
打印("你好，世界！")
令 名字 = 输入("你叫什么名字？")
打印("欢迎你，", 名字)
```

## 语言示例

```
# 猜数字小游戏（判断/循环/输入）
导入 随机
令 答案 = 随机.随机整数(1, 100)
循环 10 次：
    令 猜测 = 整数(输入("猜一个 1-100 的数："))
    如果 猜测 < 答案：
        打印("太小了")
    否则如果 猜测 > 答案：
        打印("太大了")
    否则：
        打印("猜对了！")
        中断
```

```
# 异常处理（M5a：尝试/捕获/最终/抛出）
函数 折扣价(原价, 折扣):
    如果 折扣 <= 0 或 折扣 > 1:
        抛出 值错误("折扣必须在 0 到 1 之间")
    返回 原价 * 折扣

尝试:
    打印(折扣价(100, 2))
捕获 值错误 为 e:          # 异常对象可读 e.消息 / e.类型
    打印("出错了：", e.消息)
```

```
# 面向对象（M5b：类/继承/新建）
类 动物:
    函数 初始化(自身, 名字):      # 「初始化」是构造方法
        自身.名字 = 名字           # 「自身」是实例本身
    函数 叫(自身):
        打印(自身.名字, "在叫")

类 猫 继承 动物:
    函数 叫(自身):                 # 覆盖基类方法
        打印(自身.名字, "喵喵")

令 咪咪 = 新建 猫("咪咪")
咪咪.叫()
```

## 当前进度

里程碑全部有测试护航，git 历史见 `git log`：

| 阶段 | 内容 | 状态 |
|------|------|------|
| M0 | 词法/语法/AST/中文报错/树遍历解释器/CLI+REPL | ✅ |
| M1 | 对象方法系统/链式比较/REPL增强/教程前3章 | ✅ |
| M2 | 标准库6模块/教程全7章/尾随逗号 | ✅ |
| M3 | Python 生态桥接 + 关键字参数 | ✅ |
| M4a | 字节码编译器 + Python 参考虚拟机 | ✅ |
| M4b | C 字节码虚拟机内核 + ctypes 绑定 | ✅ |
| M5a | 异常处理（尝试/捕获/最终/抛出） | ✅ |
| M5b | 面向对象（类/继承/新建） | ✅ |
| M6 | 工程化：CI/编辑器插件/文档站/打包 | ✅ |
| M7 | 表达力打磨 + 容错解析 | ✅ |
| M8 | AI 原生工具链（语言卡/JSON 报错/few-shot/评测集） | ✅ |
| M9 | 安全沙箱 + 统一协议 + MCP Server + C VM 嵌入 | ✅ |
| M10 | 生态扩展（json/日期/正则/网络标准库 + 本地包 + 浏览器试玩） | ✅ |
| M11 | 性能与分发（C VM 热路径下沉 + wheel 内带预编译 DLL） | ✅ |
| M12 | 评测驱动闭环（评测集扩到 50 题 + --llm 接入 + 自愈评测 + CI 基线） | ✅ |
| M13 | 跨语言字节码分发（JSON 字节码 + Node/Rust 绑定） | ✅ |
| M14 | 包管理与生态（安装命令 + 依赖解析 + 4 新标准库） | ✅ |
| M15 | 真实项目驱动 + 语言收敛 1.0 | ✅ |
| M16 | 发行版与安装体验（PyInstaller + 安装脚本 + 首次体验） | ✅ |
| M17 | 排错体验补全（递归翻译 + 调用栈回溯 + 协议补全 + REPL 源码行） | ✅ |
| M18 | 表达便利（切片 / 命令行参数 / 最小最大多参数 / 遮蔽治理） | ✅ |
| M19 | 发行改进（Windows 安装程序 + 三平台自动构建） | ✅ |

**测试现状：595 项全部通过**（`python -m pytest tests/`），其中 107 项是
三执行器对拍（树遍历 / Python VM / C VM 输出逐字节一致）。

### M19 已交付：发行改进（Windows 安装程序 + 三平台自动构建）

- **Windows 安装程序**：`tools/installer.iss`（Inno Setup）+ `build_release.py`
  集成，产出 `jishi-{版本}-windows-x64-setup.exe`——用户级安装（免管理员）、
  简体中文向导、开始菜单快捷方式、可选写入 PATH、注册标准卸载程序；
  不再只提供压缩包。
- **三平台自动构建**：`.github/workflows/release.yml`，打 tag（`v*`）触发
  windows / linux / macOS 矩阵构建，自动创建 Release 并附统一 SHA256SUMS。
- **发布脚本**：`tools/publish_github.py`（GitHub，复用 Gitee 的隐私屏蔽逻辑）、
  `tools/publish_gitee.py`（路线图恢复上传）。

### M18 已交付：表达便利（切片 / 命令行参数 / 最小最大多参数 / 遮蔽治理）

- **切片语法**：`列表[1:3]`、`[::-1]`、`[::2]`、`[:2]`、`[2:]`、`[:]`，
  字符串同样支持；起/止/步长都可省略或用表达式；三执行器同步（新增
  BUILD_SLICE 指令 + C VM make_slice 宿主回调）。
- **命令行参数 argv**：`jishi 脚本.jsh 参数1 参数2` → `系统.参数()` 返回
  `["参数1", "参数2"]`；`--` 之后全算脚本参数（与 Python 约定一致）。
- **`最小`/`最大` 多参数**：`最小(a, b)`、`最小(a, b, c)`；单列表调用保持兼容。
- **导入遮蔽治理**：`导入 文本` 遮蔽内建时打印一次中文提示；普通导入新增
  `为 别名`（`导入 文本 为 t`），彻底规避遮蔽。

### M17 已交付：排错体验补全（「出问题能快速定位」）

- **调用栈回溯**：深层调用出错时，报错下方显示「调用链（从外到内）」，
  例如 `外层 ← 第 8 行 → 中层 ← 第 7 行 → 里层 ← 第 5 行`；协议 JSON 里
  也带 `trace` 字段，agent 可程序化读取。
- **递归超限中文化**：修复了 `RecursionError` 绕过中文翻译、落到英文兜底的
  bug；现在输出「递归层数太深了」+ 调用链 + 「是不是忘了写结束条件」建议。
- **沙箱/协议错误补全**：超时错误带「可能是有死循环」提示；递归超限带
  中文翻译 + 调用链，`line`/`col`/`hint` 不再空。
- **REPL 报错显源码行**：交互式报错现在也显示源码行 + `^` 指示，与文件模式一致。

### M16 已交付：发行版与安装体验（「下载 → 安装 → 打开终端就能跑」）

- **独立可执行发行包**：`tools/build_release.py`（PyInstaller onedir）产出
  16MB zip + SHA256SUMS；打包产物实测跑通 hello/递归/语言卡/JSON 报错/C VM/标准库/MCP。
- **安装/卸载脚本**：`install.ps1`/`install.sh` + `uninstall`，装 `~/.jishi` + 加 PATH。
- **`jishi 医生`** 环境自检 / **`jishi 新项目`** 脚手架 / **`jishi 教程`** 索引。
- **版本号单一来源**：`jishi/version.py`（0.1.0），已发布 Gitee Release v0.1.0。

### M15 已交付：真实项目驱动 + 语言收敛 1.0

- **三个真实项目**（`examples/projects/`）：成绩分析 / 网页采集 / 日志统计，
  全部跑通；反哺 5 个语法别扭点（无切片 / 最小二参 / 导入文本遮蔽等）。
- **语言规格 v1.0 冻结**：`docs/语言规格.md` 标 v1.0，补「已知边界」节。
- **教程 10 章** + **few-shot 45 对**；设计决策补 D13–D16。

### M14 已交付：包管理与生态（「语言 → 生态」的最后一环）

- **`jishi 安装/卸载/列表` 命令**：`jishi 安装 包名 --local-dir/--local-zip/
  --from-index`，从远端索引「读索引→下载→解压→安装」全自动；重复安装报错，
  `--force` 覆盖。
- **包依赖解析**：`包.json` 的「依赖」字段（名字→版本约束），导入时递归加载
  依赖 + 版本校验（`*`/`==`/`>=`/`^` 等）+ 循环依赖检测，报错全中文。
- **包清单扩充**：「入口」（声明后只执行入口模块）/「许可」/「描述」字段。
- **标准库再扩 4 个**：`路径`（pathlib）/`加密`（hashlib）/`压缩`（zipfile）/
  `系统`（os/subprocess），延续中文薄壳、零第三方依赖、沙箱白名单默认安全。

### M13 已交付：跨语言字节码分发（脱离 Python 被任意宿主嵌入）

- **JSON 字节码格式**：`jishi 脚本.jsh --dump-bytecode` 产出版本化+校验和的
  平台无关字节码（常量池只含标量，纯数据可被任意语言消费）。
- **Node.js 绑定**：`node/` 纯 JS 解释器（零依赖），消费 JSON 字节码，
  71 个 SNIPPETS + 10 个 cases 与 Python VM 输出逐字节一致。
- **Rust 绑定**：`rust/` 零依赖 crate（自研 JSON 解析器），编译通过；
  当前「最小可运行」（可变容器语义待 Rc<RefCell> 重构）。
- **嵌入文档**：`docs/embed.md` 三宿主（C/Node/Rust）接入指南。

### M12 已交付：评测驱动闭环（「被大模型调用」有数据可证）

- 评测集从 12 题扩到 **50 题**（覆盖文本/列表/文件/类异常/字典/推导式/Python 桥接/标准库）；
- `--llm` 真实接入 + 自愈闭环评测 + CI 固化基线；唯一待办：跑一次真实模型出报告
  （需环境变量 `JISHI_EVAL_API_KEY`）。

### M11 已交付：性能与分发

- **C VM 热路径下沉**：新增 `LIST_APPEND` 指令，把「列表.追加」的两次回调
  （getattr+call）合并为一次；列表场景基准 **0.58x → 1.7x**。7 场景中 6 场景
  ≥1.5x（最高 47x）。字符串拼接是透明架构边界（HOST 句柄无法直接操作 Python
  str，详见 `tools/benchmark.py` 输出说明）。
- **wheel 内带预编译 DLL**：`tools/build_wheel.py` 编译 DLL 到包内并打包，
  `pip install jishi` 即含 C VM，无需本机 gcc；干净 venv 实测三执行器全可用。

### M10 已交付：生态扩展（「拿到就能写」的覆盖）

- **标准库扩充**（4 个中文模块）：`json`（中文不转义）、`日期`（格式化/加减/
  比较/解析）、`正则`（匹配/查找/替换/分组）、`网络`（获取/提交，urllib 薄封装
  零第三方依赖，默认 10s 超时）。
- **包管理雏形**：`导入 名字 从 本地包`，包 = `.jishi/packages/名字/`（含
  `包.json` + `.jsh` 模块），执行模块后导出顶层函数/变量。
- **浏览器试玩**：`tools/build_playground.py` 把树遍历解释器内嵌成自包含 HTML，
  pyodide（WASM Python）浏览器内直接跑基石；`docs/ai/prompt-guide.md` 教 LLM 写基石。

### M9 已交付：安全执行与 MCP 集成（「被大模型调用」的落地形态）

- **统一执行结果协议** `jishi/protocol.py`：`{ok,value,stdout,duration_ms}` /
  `{ok:false,error:{code,line,col,message,hint,fix}}`，agent 程序化读取。
- **沙箱运行器** `jishi/sandbox.py`：超时（默认 5s）、stdout 上限、禁文件写、
  模块白名单（标准库默认只放行无副作用模块，Python 桥接默认禁并拦截网络/危险模块）。
- **MCP Server** `jishi/mcp_server.py`（stdio 传输）：`run_script`（subprocess
  隔离，真正超时 kill）/ `eval_expr` / `describe_language`；入口 `jishi-mcp`。
  这是基石进入 WorkBuddy / 任意 MCP 宿主 agent 生态的入口。
- **C VM 嵌入模式** `cvm/include/jsvm.h`：公开 C ABI（`JSVM_ABI_VERSION=1`、
  `JVal` 值表示 + inline 构造器、`JsHost` 宿主回调表、导出函数声明），
  第三方 C/Node/C#/Rust 宿主可直嵌基石引擎（`examples/embed/hello.c` 实测通过）。
- CLI 加 `--sandbox` / `--json-result` / `--timeout` / `--stdin`。

### M8 已交付：AI 原生工具链（「被大模型调用」的差异化核心）

- **机器可读语言规格**：`jishi --lang-spec --format json` 输出关键字表/运算符/
  内建/标准库/异常的 JSON（版本+内容哈希），供 agent 校验代码。
- **AI 语言卡**：`jishi --ai-card` 输出 ~2K token Markdown 速查卡（语法全表 +
  API + 惯用法示例 + 英文错误对照），可直接贴进 LLM 系统提示词。
- **结构化 JSON 报错**：`jishi run x.jsh --json-errors` 输出
  `{"ok":false,"errors":[{code,line,col,message,hint,fix}]}`，`fix` 字段
  （如 `{"old":"def","new":"函数"}`）让 agent 可程序化文本替换自愈。
- **few-shot 示例库**：`examples/ai/few-shot.md` 20 个「任务→代码」对。
- **官方系统提示词**：`docs/ai/system-prompt.md`。
- **llms.txt**：文档站产出 `site/llms.txt` + `llms-full.txt`（遵循 llmstxt.org）。
- **基石评测集 JishiEval**：`bench/ai-eval/` 50 题 + 自测脚本，作为
  「语法改动不得使一次通过率下降」的回归基线。

### M7 已交付：表达力打磨（语法糖，三执行器同步）

- **多赋值与解包**：`令 a, b = [1, 2]`、`a, b = b, a`（交换）、`返回 a, b`
  → 返回列表；新增 `UNPACK` 指令，数目不符中文报错。
- **默认参数**：`函数 f(a, b = 10)`，定义处求值一次（与 Python 一致），
  有默认值的参数须在最后；支持方法/关键字参数配默认。
- **列表/字典推导式**：`[x*2 遍历 x 在 列表 如果 x>2]`、`{k: v 遍历 ...}`；
  编译器脱糖为迭代+追加，C VM 复用现有指令零改动。
- **文本插值**：反引号 `` `你好 {名字}，今年 {年龄} 岁` ``，`{表达式}` 求值
  插值、`{{`/`}}` 字面花括号；parser 脱糖为拼接链，三执行器零改动。
- **容错解析**：识别 LLM 写中文代码时 fallback 回英文的高频错误（`def`/`if`/
  `print`/`True`/`and`…），直接给出中文建议（「你是不是想写「函数」？」），
  英文普通变量名零误伤。
- 顺带修复 C VM 一个潜伏的 use-after-free（链式调用 `造()()` 必崩）。

### M6 已交付：工程化

- **CI 自动化**：`tools/ci.py` 一键全检（C VM 构建 → pytest → C 对拍确认 →
  基准冒烟）；`.github/workflows/ci.yml` 备用（ubuntu+windows × py3.10/3.13）。
- **编辑器插件**：`editors/vscode-jishi/` —— .jsh 语法高亮（含中文引号/中文
  关键字）、11 个代码片段、括号/注释/缩进配置；vsce 打包 vsix 已端到端验证。
- **文档站**：`tools/build_site.py` 零依赖 Markdown→HTML 生成器，把 README +
  语言规格 + 设计决策 + 教程 10 章渲染成带侧边栏的静态站（`site/`，已 gitignore）。
- **打包发布**：pyproject 已验证 pip 可编辑安装 + wheel 构建，干净 venv 里
  CLI/REPL/脚本全可用；无 C DLL 时 C VM 优雅降级、树遍历照常。

### M5a 已交付：异常处理（三执行器同步支持）

- 语法：`尝试:` / `捕获 类型 为 e:`（可多个）/ `最终:`（可选）/ `抛出 值`；
- 异常对象即 `JishiError` 实例，`e.消息` / `e.类型` 可读；
  类型名（值错误/类型错误/索引错误/键错误/除零错误/文件错误/运行期错误/异常）
  是内建可调用对象（`抛出 值错误("…")`），捕获按 isinstance 匹配，基类抓子类；
- 非异常值 `抛出` 会被包装成「异常」，原值留在 `e.消息`；
- `最终` 语义与 Python 一致（正常/异常/返回/中断都执行，返回可覆盖）；
- 树遍历靠 Python try/except；字节码 VM 与 C VM 都在帧内维护异常处理器栈，
  出错时逐层弹帧匹配（详见 cvm/bytecode.md 的异常协议）。

### M5b 已交付：面向对象（三执行器同步支持）

- 语法：`类 名字 继承 基类:`（方法即函数，构造方法约定名「初始化」，
  方法首参「自身」）、`新建 类(参数)`；
- `JishiClass`/`JishiInstance` 在 `runtime.py`，实例字段 + 绑定方法
  （`实例.方法()` 自动把实例塞进「自身」首参）；
- 继承合并基类方法表，子类同名覆盖；构造方法「初始化」自动调用；
- 类方法内 `抛出` 的异常能穿透调用栈、被外层 `尝试` 捕获，与 Python 一致
  （重入边界 stop_at/stop_depth 保证三执行器一致）。

### M4 已交付：字节码编译 + C 虚拟机

- `jishi/compiler.py` AST→字节码（作用域分析、闭包单元、指令带源码行列）；
- `jishi/vm.py` Python 参考虚拟机（M4a，语义参照与调试基线）；
- `cvm/src/jsvm.c` C 栈式 VM（数值/比较/跳转/调用/循环/闭包快路径），
  `cvm/build.py` 一条命令出 DLL，`jishi/cvm_bind.py` ctypes 绑定 + 宿主回调；
- `tests/test_cvm.py` 三执行器对拍（黄金用例/错误快照/示例全覆盖）；
- `tools/benchmark.py` 可复现基准：C VM 提速 1.6x–47x
  （当循环 47x、递归 17x、函数调用 4.3x、算术混合 5.1x、列表 1.6x）；
  字符串拼接是透明架构边界（字符串是 HOST 句柄，C 侧无法直接操作 Python str，
  详见 benchmark 输出说明）。

### M3 已交付：Python 生态桥接 + 关键字参数

`导入 x 从 python`、别名导入、Python 对象属性/方法/下标桥接、
基石函数作 Python 回调（map/sorted）、Python 异常中文化、**关键字参数**
（`json.dumps(x, ensure_ascii=假)` 保住中文）、`True/False/None` 中文写法提示。

## 已经能做什么

- **语法**：变量、算术/比较/逻辑、链式比较（`60 < 分数 <= 90`）、
  如果/否则如果/否则、遍历、循环 N 次、当、中断/继续、函数、导入、列表/字典
- **表达力（M7）**：多赋值解包（`令 a, b = [1, 2]`、`a, b = b, a`）、
  默认参数（`函数 f(a, b = 10)`）、列表/字典推导式
  （`[x*2 遍历 x 在 列表 如果 x>2]`）、文本插值（`` `你好 {名字}，今年 {年龄} 岁` ``）
- **异常处理（M5a）**：`尝试`/`捕获 类型 为 e`/`最终`/`抛出 值`；
  8 种内建错误类型（`值错误` 等）可抛可抓，基类抓子类，`e.消息`/`e.类型` 可读
- **面向对象（M5b）**：`类` 定义方法、`继承` 复用与覆盖、`新建` 实例化、
  自动构造（`初始化`）、实例字段、绑定方法（`自身`）
- **内建函数**：打印、输入、整数/小数/文本、长度、范围、最大、最小、总和、类型、反转
- **对象方法**：
  - 列表：追加 / 插入 / 移除 / 弹出 / 排序 / 反转 / 清空 / 索引 / 计数 / 包含
  - 字典：获取 / 键 / 值 / 包含 / 更新 / 弹出 / 清空
  - 文本：拆分 / 替换 / 查找 / 大写 / 小写 / 去空白 / 开头是 / 结尾是 / 包含 / 转整数 / 转小数
- **中文报错**：精确到行列 + "你是不是想写…"修正建议（错误快照护航）
- **REPL**：表达式回显、多行块输入、帮助/清空命令
- **标准库（M2 + M10 + M14，共 14 个中文模块）**：
  - 随机：随机整数 / 随机小数 / 随机选择 / 洗牌
  - 数学：开方 / 幂 / 四舍五入 / 最大公约数 / 最小公倍数 / 阶乘 / 圆周率 / 三角 / 对数
  - 时间：现在 / 今天 / 此刻 / 时间戳 / 星期名 / 加天数 / 高精度计时 / 睡眠
  - 文件：写文本 / 读文本 / 追加 / 写行 / 按行读 / 目录操作 / 路径工具
  - 表格：读表格 / 挑选 / 筛选 / 排序按 / 汇总 / 追加——数据分析入门钥匙
  - 文本：拼接 / 对齐 / 补零 / 判断 / 去前缀后缀 / 格式化
  - json（M10）：转文本 / 解析 / 读文件 / 写文件（中文不转义）
  - 日期（M10）：今天 / 格式化 / 加天数 / 减天数 / 相差天数 / 早于 / 晚于 / 相等 / 星期名
  - 正则（M10）：匹配 / 搜索 / 查找全部 / 替换 / 拆分 / 分组
  - 网络（M10）：获取 / 提交 / 获取JSON（urllib 薄封装，零第三方依赖，默认 10s 超时）
  - 路径（M14）：存在 / 是文件 / 是目录 / 绝对路径 / 父目录 / 文件名 / 后缀 / 连接 / 创建目录 / 列出 / 大小（pathlib）
  - 加密（M14）：摘要 / md5 / sha1 / sha256 / sha512 / 文件摘要（hashlib）
  - 压缩（M14）：打包 / 解压 / 列出内容（zipfile）
  - 系统（M14）：系统名 / 环境变量 / 执行 / 退出码 / 主机名（os/subprocess）
- **语法补充（M2）**：尾随逗号（`[1, 2, 3,]`）、括号内跨行书写列表/字典/参数
- **Python 生态桥接（M3）**：
  - `导入 math 从 python`、`导入 pandas 从 python 为 数据框`——直接用整个 Python 生态
  - Python 对象属性/方法直接访问：`datetime.datetime.now().year`
  - 基石函数可直接传给 Python 高阶函数（map/filter/sorted 回调）
  - Python 异常自动翻译成中文（文件找不到、权限不足、递归过深…）
  - **关键字参数**：`json.dumps(x, ensure_ascii=假)` —— 中文不转义的关键
    ```python
    导入 json 从 python
    打印(json.dumps({"姓名"： "小明"}, ensure_ascii=假))   # {"姓名": "小明"}
    ```
    基石函数同样支持：`介绍(姓名 = "小红", 年龄 = 8)`
- **教程**：`docs/tutorial/` **全 10 章**已完成（从你好世界到记账程序综合实战，
  再到语法糖/沙箱 MCP/包管理），配套示例在 `examples/tutorial/`，示例全可运行

## 教程目录

| 章节 | 内容 |
|------|------|
| [01 你好基石](docs/tutorial/01_你好基石.md) | 运行基石、打印、变量、注释 |
| [02 数与文本](docs/tutorial/02_数与文本.md) | 运算、字符串方法、输入、列表 |
| [03 判断与循环](docs/tutorial/03_判断与循环.md) | 如果/否则、遍历/循环/当、链式比较 |
| [04 函数](docs/tutorial/04_函数.md) | 定义函数、作用域、递归 |
| [05 字典与结构化数据](docs/tutorial/05_字典与结构化数据.md) | 键值对、嵌套、计数器 |
| [06 文件与标准库](docs/tutorial/06_文件与标准库.md) | 文件读写、数学/时间/表格模块 |
| [07 综合实战](docs/tutorial/07_综合实战.md) | 记账程序：需求→设计→实现 |
| [08 语法糖](docs/tutorial/08_语法糖.md) | 多赋值解包/默认参数/推导式/文本插值 |
| [09 沙箱与 MCP](docs/tutorial/09_沙箱与MCP.md) | 沙箱三道保险/统一协议/MCP Server |
| [10 包管理](docs/tutorial/10_包管理.md) | 安装/卸载/列表/依赖解析 |

## 项目结构

```
jishi/           前端：词法/语法/AST/语义/报错/执行内核/标准库
  tokenizer.py   词法分析（缩进栈、全角归一化、中文分词消歧、中文引号）
  parser.py      语法分析（递归下降+优先级爬升，链式比较、尾随逗号、容错解析）
  ast_nodes.py   AST 节点（dataclass, kw_only=True）
  errors.py      中文错误体系（E01xx词法/E02xx语法/E03xx语义/E2xxx运行）
  interpreter.py 树遍历解释器（作用域链/闭包/对象方法分派/内建函数，默认执行器）
  runtime.py     共享运行时语义（方法表/内建/属性/调用/导入，三执行器共用）
  opcodes.py     指令集（唯一事实来源，C 头文件由此生成）
  compiler.py    字节码编译器（作用域分析/闭包单元/指令带行列）
  vm.py          Python 参考字节码虚拟机（M4a）
  cvm_bind.py    C VM 的 ctypes 绑定 + 宿主回调实现（M4b）
  ai.py          AI 元数据统一数据源：语言卡 / 语言规格 / 错误序列化（M8）
  protocol.py    统一执行结果协议（M9.2）
  sandbox.py     沙箱运行器（超时/stdout 上限/模块白名单，M9.1）
  mcp_server.py  MCP Server（stdio 传输，run_script/eval_expr，M9.3）
  serialize.py   跨语言字节码序列化（M13.1，JSON 格式 + 版本 + 校验和）
  packages.py    包管理：远端索引格式 + 版本约束 + 安装/卸载（M10.2/M14）
  cli.py         CLI 入口（run/--ast/-i/--ai-card/--lang-spec/--sandbox/安装/卸载/列表…）
  repl.py        交互式 REPL（回显/多行块/帮助/清空）
  stdlib/        标准库 14 模块：随机/数学/时间/文件/表格/文本/json/日期/正则/网络/路径/加密/压缩/系统
cvm/             C 执行内核（M4b）
  bytecode.md    字节码设计文档（指令集/值表示/宿主协议/重入/异常协议）
  build.py       一键构建（生成 opcodes.h + gcc 出 DLL/SO，支持 --out）
  include/jsvm.h 公开 C ABI 头文件（M9.4，JSVM_ABI_VERSION=1）
  include/opcodes.h 指令码头文件（由 opcodes.py 生成，两侧同步）
  src/jsvm.c     C 虚拟机主循环
node/            Node.js 纯 JS 解释器（M13.2，零依赖，消费 JSON 字节码）
rust/            Rust 绑定（M13.3，jishi-ffi crate + 自研零依赖 JSON 解析器）
tests/           595 项测试（含 107 项三执行器对拍）
examples/        示例脚本（含 tutorial/ 10 个教程配套示例、ai/ few-shot 库、embed/ C 嵌入示例、packages/ 包管理示例）
docs/            语言规格.md、设计决策.md、路线图.md、embed.md、ai/（系统提示词/提示词指南）、tutorial/ 教程 10 章
assets/          品牌资产（基石_logo.png，源图 基石_logo.jpeg）
tools/           run_tests.py 测试 / benchmark.py 基准 / build_site.py 文档站 /
                 build_playground.py 浏览器试玩 / build_wheel.py wheel 打包 /
                 ci.py 一键全检 / publish_gitee.py 公开仓库同步
```

## 架构与数据流

```
源码 → tokenizer → parser → AST ──┬─→ Interpreter（树遍历，默认执行器）
                                  └─→ 编译器 → 字节码 ──┬─→ Python VM
                                                        └─→ C VM（jsvm.dll）
```

- 三个执行器（树遍历 / Python VM / C VM）共用 `jishi/runtime.py` 的语义，
  `tests/test_cvm.py` 对拍保证「换执行器不改变任何可观察行为」
  （stdout 与报错逐字节一致）。
- 默认仍走树遍历；`jishi/vm.py` / `jishi/cvm_bind.run_source_c` 可切换。
  性能：C VM 提速最高 47x（见 tools/benchmark.py，字符串拼接为架构边界）。
- **AI 原生能力**（M8/M9）架在 AST 与报错之上：`jishi/ai.py` 从
  tokenizer/parser/runtime/stdlib 动态收集元数据，产出语言卡、语言规格、
  JSON 结构化报错；`jishi/mcp_server.py` 把执行能力暴露给任意 MCP 宿主。

## 环境要求

- **Python ≥3.10**（运行时零第三方依赖；测试用 pytest，
  `tools/make_logo.py` 图像处理用 pillow——均为开发期工具，与运行时无关）
- **使用（pip 安装）**：`pip install jishi` 即含预编译 C VM，无需 gcc
  （`tools/build_wheel.py` 打出的 wheel 内带 DLL/SO）
- **从源码开发**：需要 C 编译器构建 C VM（`python cvm/build.py`），
  装 MinGW-w64（Windows）或 zig / gcc 均可；无编译器也能跑树遍历解释器，
  C VM 自动降级、其余功能照常
- Windows 注意：venv 可执行文件在 `Scripts/` 而非 `bin/`。

## 开发：运行与测试

```bash
# 运行脚本 / 交互式 REPL / 查看 AST
python -m jishi.cli examples/01_猜数字.jsh
python -m jishi.cli -i
python -m jishi.cli examples/01_猜数字.jsh --ast

# 全部测试
python -m pytest tests/

# 一键构建 C 虚拟机 DLL
python cvm/build.py

# 性能基准（树遍历 vs C VM）
python tools/benchmark.py
```

## 关键技术决策（详见 docs/设计决策.md）

- **D1 技术栈透明**：Python 前端 + C 内核 + Python 生态桥接，不 fork/patch CPython
- **D2 分词消歧**：关键字粘连检查只针对**多字关键字**（如果/循环/导入…），
  单字关键字（类/当/真/空/非/令）不做前缀检查，否则 `类型`/`当时`/`空间` 等日常词全被误伤。
- **D3 全角标点**：字符串与注释外做一对一归一化（`＝`→`=` 等）；全角空格 U+3000 在缩进中报错。
- **D4 执行模型**：树遍历 → 字节码 + C VM（M4），语法不变，对拍测试保证一致。
- **D5 标准库**：中文 API 是薄壳，绝不重造轮子，真实价值来自 Python 生态。
- **D7 对象方法分派**：方法表按类型分派返回闭包，列表方法原地修改、文本方法返回新值。
- **D8 REPL**：顶层表达式非空回显（真/假/空中文化）；空行强制结束多行块。
- **D11 异常对象模型（M5a）**：异常即 `JishiError` 实例，`e.消息`/`e.类型` 可读；
  捕获条件按类型名匹配，用 isinstance 判断（基类抓子类）。
- **D12 语法用中文助词（M5a）**：`捕获 值错误 为 e:`、`继承 基类:` 用「为/继承」
  等助词连接，不用冒号嵌套的类 Python 写法，读起来更像自然语言。

## 已踩过的坑（避免重踩）

1. `re.fullmatch` 对 `[...]` 单字符类多字符永远 False，要用 `[...]+` 运行正则。
2. dataclass 继承字段顺序 → 全部 `@dataclass(kw_only=True)`。
3. 全角引号需配对表（`“` 配 `”`），不是同字符闭合。
4. 块语句以 DEDENT 结束，parser 主循环不能要求 NEWLINE。
5. `与/或` 右操作数要用 `parse_expr(prec+1)` 而非 `parse_unary()`，否则比较运算符逃逸。
6. `dump_ast` 用 `vars()` 前先判类型（tuple/list 无 `__dict__`）。
7. Windows venv 路径 `Scripts/` 不是 `bin/`。
8. 尾随逗号：列表/字典/调用参数/函数参数四处循环都要支持（M2 已修）。
9. `数学.幂` 用 `math.pow` 返回 float，整数期望 int → 用 `**` 运算符。
10. `导入 文本` 会遮蔽内建 `文本()` 转换函数（与 Python 导入遮蔽一致，文档已注明）。
11. 调用括号内的 `名字 = 值` 一律按关键字参数处理（parser 调用上下文无赋值语义）。
12. `_lookup` 查英文 True/False/None 的提示要在"你是不是想写"建议**之前**抛出，
    否则编辑距离建议会给出误导性结果。
13. 三执行器共享 runtime 语义，但异常穿透调用栈的「重入边界」必须一致
    （vm.py 的 stop_at / C VM 的 stop_depth），否则类方法里 `抛出` 会被错误接住。

## 路线图

M0–M16 已全部完成（见「当前进度」）。

**定位（2026-09 调整）**：北极星从「最容易被大模型正确生成」务实调整为
**「健全语言自身能力，让编程者方便实现意图，出问题能快速定位」**；
「AI 友好」降为辅助线带着走。

后续规划详见 [docs/技术路线_v1.0之后.md](docs/技术路线_v1.0之后.md)：

| 阶段 | 主题 | 可演示成果 | 状态 |
|------|------|-----------|------|
| M17 | 排错体验补全：修递归翻译 bug + 调用栈回溯 + 协议补全 + REPL 源码行 | 报错可定位率 100% + 调用链可见 | ✅ 已完成 |
| M18 | 表达便利：切片 / argv / 最小二参 / 遮蔽治理 | 三个真实项目代码变短 | ✅ 已完成 |
| M19 | 三平台发行 + 自动化 | 一次 tag 出三平台包 | ✅ 已完成 |
| M20–M22 | 性能 / 开发者工具 / 生态 | 按需推进 | 待启动 |

每个阶段维持既有纪律：**可演示成果 + 测试护航 + git 提交**；
新特性准入三关：**真实需求驱动 / 可度量验收 / 投入产出比**。

## 设计原则

1. **像 Python 一样简洁**：缩进划分代码块，语法零冗余。
2. **中文是母语**：报错信息全中文，精确到行列，附"你是不是想写…"修正建议。
3. **生态即生命**：绝不重造标准库，薄封装 Python 生态。
4. **透明不吹牛**：技术栈如实标注，走长期演进路线。

## 声明

本人陈柏林完全放弃本语言的所有知识产权，只期望这个语言或是说是工具可以有所用途，毕竟做个中文的编程语言，是我一直以来的夙愿，但是一直受限于没有相关的技术能力，所以迟迟没有动手。也有幸在这个时代可以使用大模型来帮助自己塑造自己期望的软件产品，所以我就试着去实现自己的想法了，大家如果有什么建议也可以和我说，我的邮箱是benxiaoniao@outlook.com
