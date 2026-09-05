# 第 9 章：安全执行——沙箱与 MCP（M9）

前面学的都是「自己写代码自己跑」。这一章讲基石的**另一面**：
它生来就被设计成「能让大模型安全地调用」——通过沙箱和 MCP。

## 9.1 为什么要沙箱

大模型（LLM）生成的代码**不能完全信任**：可能死循环、可能读你不想让它读的
文件、可能联网。沙箱给执行加了三道保险：

| 保险 | 作用 |
|------|------|
| **超时** | 默认 5 秒没跑完就强制终止（防死循环） |
| **输出上限** | stdout 超过 10000 字符就报错（防刷屏） |
| **模块白名单** | 默认只放行无副作用的模块，禁文件写/网络/系统 |

## 9.2 用命令行开沙箱

```bash
jishi 脚本.jsh --sandbox          # 沙箱执行
jishi 脚本.jsh --json-result      # 输出统一协议 JSON（成功失败都是 JSON）
jishi 脚本.jsh --timeout 10       # 自定义超时（秒）
```

`--json-result` 输出长这样：

```json
{"ok": true, "stdout": "你好\n", "duration_ms": 3}
```

失败时：

```json
{"ok": false, "error": {"code": "E2000", "message": "...", "line": 1, "col": 2}}
```

这个统一协议（`jishi/protocol.py`）让 agent 能**程序化读取结果**，不用解析
人话报错。

## 9.3 沙箱默认拦什么

写一段会「越界」的代码，沙箱里跑会直接拒绝：

```jsh
导入 网络                          # 沙箱默认拒绝：涉及网络访问
导入 文件                          # 沙箱默认拒绝：涉及文件读写
导入 系统                          # 沙箱默认拒绝：涉及执行外部命令
导入 os 从 python                  # 沙箱默认拒绝：Python 桥接默认禁
```

放行的话，需要显式开启对应开关（在 `jishi/sandbox.py` 的 `SandboxOpts` 里）。

## 9.4 MCP Server：让 AI 直接调用基石

MCP（Model Context Protocol）是 AI 应用调用外部工具的通用协议。基石提供了
一个 MCP Server，让任意 MCP 宿主（如 WorkBuddy）能直接跑基石代码：

- `run_script`：跑一段基石脚本（subprocess 隔离，真正超时 kill）
- `eval_expr`：求一个表达式的值
- `describe_language`：返回语言规格（供 AI 校验代码）

入口命令是 `jishi-mcp`（stdio 传输）。配置好 MCP 宿主后，AI 就能把基石当成
一个「安全执行环境」来调用。

## 9.5 小结

- 沙箱 = 超时 + 输出上限 + 模块白名单，三道保险让不可信代码跑不坏环境
- `--json-result` 输出统一协议，agent 程序化读结果
- MCP Server 是基石进入 AI 生态的入口：`run_script` / `eval_expr` / `describe_language`

下一章讲「包管理」——让基石代码可复用、可分享。
