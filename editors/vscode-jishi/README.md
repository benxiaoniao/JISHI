# 基石语言 · VSCode 扩展

为 **基石（jishi）** —— 中文编程语言提供编辑器支持：

- `.jsh` 文件**语法高亮**（关键字 / 字符串 / 注释 / 数字 / 函数与类名 / 内建函数）
- 常用结构**代码片段**（如果 / 遍历 / 函数 / 类 / 尝试等，输前缀回车即出）
- 中文标点成对补全（`（）``「」` 等）、`#` 注释、缩进规则

## 本地加载（F5 调试方式）

1. 用 VSCode 打开本目录 `editors/vscode-jishi`
2. 按 `F5`（或 运行 → 启动调试）
3. 弹出「扩展开发宿主」窗口，打开任意 `.jsh` 文件即可看到高亮

## 打包成 vsix

```bash
npm install -g @vscode/vsce
vsce package          # 生成 vscode-jishi-0.1.0.vsix
code --install-extension vscode-jishi-0.1.0.vsix
```

## 语法高亮示例

```python
# 注释用井号
函数 折扣价(原价, 折扣):
    如果 折扣 <= 0 或 折扣 > 1:
        抛出 值错误("折扣必须合法")
    返回 原价 * 折扣

类 账户:
    函数 初始化(自身, 余额):
        自身.余额 = 余额
```

## 目录

```
package.json                  扩展清单（语言注册 .jsh）
language-configuration.json   括号/注释/缩进规则
syntaxes/jishi.tmLanguage.json TextMate 语法（高亮核心）
snippets/jishi.code-snippets   代码片段
```

> 语法以 `jishi/tokenizer.py` 的 KEYWORDS 表为准；语言变更时同步本文件。
