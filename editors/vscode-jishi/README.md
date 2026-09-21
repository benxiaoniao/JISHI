# 基石语言 · VSCode 扩展

为 **基石（jishi）** —— 中文编程语言提供编辑器支持。

**语法层**（扩展自带，不需要语言服务）：

- `.jsh` 文件**语法高亮**（关键字 / 字符串与插值文本 / 注释 / 数字 / 函数与类名 /
  内建函数 / 标准库模块名 / 异常类型）
- 常用结构**代码片段**（如果 / 遍历 / 遍历解包 / 匹配 / 用 / 函数 / 类 / 尝试 /
  导入 / 插值文本 …，输前缀回车即出）
- 中文标点成对补全（`（）``「」` 等）、`#` 注释、缩进规则

**语义层**（由 `jishi lsp` 提供，M42 起接入）：

| 能力 | 在编辑器里的样子 |
|------|------------------|
| 实时诊断 | 有错的地方画波浪线，中文报错 + 错误码 |
| 补全 | 关键字 / 内建 / 标准库模块与函数 / 本文件符号；`随机.` 之后给模块函数 |
| 悬停文档 | 鼠标停名字上，出中文说明与签名 |
| 跳转定义 | `F12`；本文件找不到会去**工作区别的文件**里找一个唯一的同名符号 |
| 大纲 / 文档符号 | `Ctrl+Shift+O` 按符号跳转 |
| 工作区符号 | `Ctrl+T` 全工作区搜符号 |
| 格式化 | `Shift+Alt+F`（与 `jishi 格式化` 同一份实现） |
| 重命名 | `F2`（详见下面的「已知边界」） |
| 快速修复 | 报错处点灯泡：把 `if` 改成 `如果`、`len` 改成 `长度`… |
| 签名提示 | 打 `(` 时出形参表，并高亮当前是第几个实参 |
| 语义高亮 | 区分关键字 / 标准库 / 用户函数与变量（比纯语法高亮更准） |

**调试**（由 `jishi dap` 提供，M43 起接入）：

| 能力 | 在编辑器里的样子 |
|------|------------------|
| 断点 | 行号左边点一下；**写在空行/注释行上会显示成灰圈**并说明原因 |
| 单步 | `F10` 下一步（不钻进函数）/ `F11` 单步（钻进去）/ `Shift+F11` 跳出 |
| 继续 / 暂停 | `F5` 继续；运行中也可以「暂停」，会在下一条语句边界停下 |
| 变量 | 左侧「变量」面板，**点调用栈的任意一帧**看那一帧的变量（带中文类型名） |
| 调用栈 | 左侧「调用栈」面板，显示到某一帧的完整链路 |
| 监视 / 悬停求值 | 「监视」面板加表达式；鼠标悬停也能求值（C 虚拟机除外，见下） |
| 调试控制台 | 程序输出与调试器提示都在这里；`output` 事件按 stdout / console 分类着色 |

## 先决条件

语言服务与调试适配器都由基石自带，**扩展本身零运行时依赖**（高亮与片段不用
任何外部程序；语言服务那条路也没引 `vscode-languageclient`，传输层是自己写的
`lsp-transport.js`。调试那条路用的是 VSCode 内置的 `DebugAdapterExecutable`，
它负责起子进程并转发 DAP 消息）。你需要能跑 `jishi`：

```bash
jishi 医生          # 看环境是否就绪
jishi lsp           # 编辑器就是靠这条命令起语言服务（一般不用手动跑）
jishi dap           # 调试时靠这条命令起调试适配器（同上，不用手动跑）
```

`jishi` 不在 PATH 上时，在设置里指明（语言服务与调试都用这一项）：

```json
{
  "jishi.server.command": "D:/jishi/jishi.exe"
}
```

## 安装与试用

### 方式一：从 `.vsix` 安装（**推荐**）

`.vsix` 是构建产物，**没有进版本库**（`.gitignore` 里排掉了），先在本目录打一个
（需要 Node；命令会自己拉 `@vscode/vsce`）：

```bash
npx @vscode/vsce package        # 产出 vscode-jishi-0.3.0.vsix，约 26KB
code --install-extension vscode-jishi-0.3.0.vsix
```

或在 VSCode 里：扩展面板 → 右上角 `...` → 「从 VSIX 安装」。
装完之后**任何窗口、任何文件夹**打开 `.jsh` 都能用，不需要开发宿主窗口。

### 方式二：F5 开发方式（改扩展代码时用）

1. 用 VSCode 打开本目录 `editors/vscode-jishi`
2. 按 `F5`（或 运行 → 启动调试）——`.vscode/launch.json` 已备好，不用选环境
3. 弹出「扩展开发宿主」窗口：在里面**打开单个 `.jsh` 文件**（用「文件 → 打开文件」，
   别用「打开文件夹」——从宿主窗口打开新文件夹会另开一个**普通窗口**，
   而普通窗口不加载开发中的扩展）

此时打开文件就能看到诊断、补全、格式化、重命名、快速修复、签名提示与语义高亮；
按 `F5` 还能调试脚本。右下角状态栏有「基石 LSP：就绪」，点它可以重启服务。

**开发宿主窗口第一次打开时**，如果 `jishi` 不在 PATH 上，会弹一条提示——
点「设置 jishi 路径」把 `jishi.server.command` 指到可执行文件即可。

**装了 jishi 但扩展说连不上时**，先看「输出 → 基石语言服务」面板；
把设置 `jishi.server.trace` 打开还能看到逐条 LSP 收发消息。

### 调试一个脚本

第一次按 `F5` 时会生成 `launch.json`，选「基石调试」即可。手写的话：

```json
{
  "type": "jishi",
  "request": "launch",
  "name": "调试基石脚本",
  "program": "${file}",
  "stopOnEntry": false
}
```

- `program` 不写就用当前打开的文件；
- `stopOnEntry: true` 让程序停在第一句上；
- 想换执行器加 `"执行器": "vm"`（可选 `树遍历` / `vm` / `cvm`，三者调试行为一致）。

## 不装编辑器也能自检

```bash
cd editors/vscode-jishi
node verify-lsp.js /path/to/jishi        # 语言服务：走完 25 项断言
node verify-tokenize.js                  # 词法层的老自检
```

`verify-lsp.js` 用**扩展同一份传输代码**真的起一个 `jishi lsp` 子进程，把每项
能力都点一遍。装进 VSCode 之前先跑这个，能把「点了没反应」这类问题挡在前面。
（它也是回归测试的一部分：`tests/test_m42_vscode_client.py`。）

调试那条路没有对应的 JS 自检——扩展用的是 VSCode 内置的 `DebugAdapterExecutable`，
没有「扩展自己的传输层」可复用；协议级验证在 `tests/test_m43_dap.py`（31 项）。

## 打包成 vsix

```bash
cd editors/vscode-jishi
npx @vscode/vsce package                 # 生成 vscode-jishi-0.3.0.vsix（约 25KB）
```

`.vscodeignore` 已排掉 `node_modules`（那是打包工具自己的依赖，99MB 不进包）。

## 上架 Marketplace（需要你本人操作两步）

1. 去 <https://dev.azure.com> 建一个 Personal Access Token（scope 选
   **Marketplace → Manage**）；
2. 用这个 token 注册一次发布者身份：

   ```bash
   npx @vscode/vsce login <发布者名>
   npx @vscode/vsce publish                 # 之后的每次上架
   ```

发布者名必须先在 <https://marketplace.visualstudio.com/manage> 建好
（`package.json` 里的 `publisher` 字段要与之相同）。

## 已知边界（不粉饰）

- **重命名只支持模块级符号**（函数 / 类 / 顶层变量 / 导入名）。
  函数形参与函数内的局部变量**不支持**——AST 不记形参位置，而且没有作用域
  分析就改名会把「另一个函数里同名的局部变量」一起改掉，那是在改坏用户的代码。
  做不到的位置按 `F2` 时 VSCode 会直接说「无法重命名」，不会给一个错的编辑。
- **跨文件跳转只在「唯一命中」时生效**：同名符号出现在多个文件里就放弃跳转
  （跳错地方比跳不过去更费时间）。
- **工作区符号索引有封顶**：最多 400 个 `.jsh` 文件、单文件 512KB，跳过
  `.jishi`（第三方包缓存）与隐藏目录。真要做全项目数据库得上后台任务，
  不该卡住编辑器。
- **签名提示在「文档解析不了」时靠 token 兜底**：从 `函数 甲(…)` 的 token
  序列里读形参。所以未闭合括号时也能用（那正是最需要它的时候）。
- **调试时的变量不展开**：列表/字典显示成一行文本（`[1, 2, 3]`），与命令行的
  「变量」命令同一口径——不做「编辑器里能展开、命令行里不能」这种分裂。
- **C 虚拟机不支持在暂停处求值**：那边没有「在任意帧上求一个表达式」的入口。
  监视与悬停求值会明确回一句「暂不支持就地求值」并给出替代方案（改用 `vm`
  执行器），而不是给个错值。断点、单步、看变量与调用栈照常可用。
- **没有条件断点 / 命中计数 / 日志断点**：命令行版也没有，这里不假装有
  （能力声明里一律写 `false`，VSCode 就不会给用户画出没用的输入框）。

## 目录

```
package.json                   扩展清单（语言注册 + 语言服务与调试配置项）
extension.js                   客户端：把编辑器 API 接到 LSP / DAP 上
lsp-transport.js               最小 LSP 传输层（分帧/请求应答/通知，不依赖 vscode）
verify-lsp.js                  语言服务的端到端自检（脱离 VSCode 也能跑）
verify-tokenize.js             词法层自检
.vscode/launch.json            F5 启动配置（打开扩展开发宿主）
language-configuration.json    括号/注释/缩进规则
syntaxes/jishi.tmLanguage.json TextMate 语法（高亮核心）
snippets/jishi.code-snippets   代码片段
```

> 语法以 `jishi/tokenizer.py` 的 KEYWORDS 表为准；语言变更时同步本文件。
> 语言能力以 `jishi/lsp.py` 的 `on_initialize` 声明为准；调试能力以
> `jishi/dap.py` 的 `_req_initialize` 与 `tests/test_m43_dap.py` 为准。
> 高亮表与片段和语言事实的对齐由 `tests/test_m43_highlight.py` 钉住——
> 新增内建/模块/关键字后如果忘了同步，那条测试会红。
