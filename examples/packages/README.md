# 本地包演示（M10.2 + M14）

这是「包管理」的可演示样例，演示 `包.json` 的「依赖」字段、版本约束，
以及 M14 的 `jishi 安装` 命令。

## 包结构

```
examples/packages/
├── 基础库/                 # 被依赖的基础包（版本 1.0.0）
│   ├── 包.json
│   └── 工具.jsh            # 翻倍 / 平方
├── 数学工具/               # 依赖「基础库 >=1.0.0」的上层包（版本 2.0.0）
│   ├── 包.json
│   └── 计算.jsh            # 立方 / 平方和（直接用 基础库.平方）
├── 图片/                   # **第一个「真第三方包」**（0.2.9 · M144）：图片格式转换
│   ├── 包.json
│   ├── 图片.jsh            # BMP/PPM/PGM 读、BMP/PPM/PNG 写，纯基石实现
│   ├── 图片_test.jsh       # 用它自己的 `测试` 模块写的 11 个用例
│   └── README.md           # 用法 · 支持矩阵 · 已知边界 · **撞出来的三个缺口**
└── 索引.json               # 远端索引格式样例（先静态托管，不建站）
```

## 真第三方包：`图片`（M144）

前面两个是「演示依赖」的小样例；`图片` 是**真需求驱动的第一个包**，并且
**它的主要产出不是功能，是缺口清单**：

```bash
jishi 安装 图片 --local-dir examples/packages/图片
jishi 测试 examples/packages/图片
```

它撞出三条真问题（详见 `图片/README.md`），最硬的一条是
**产品版 `jishi-rs` 能装不能导入** ⇒ 发行版用户装得上、用不了。

⚠️ **所以这个包的示例只能用 Python 参考实现跑**：
`python -m oracle.jishi.cli 你的程序.jsh`。

## 方式一：`jishi 安装` 命令（M14.2，推荐）

```bash
jishi 安装 基础库   --local-dir examples/packages/基础库
jishi 安装 数学工具 --local-dir examples/packages/数学工具
jishi 列表          # 看到已装的两个包
```

然后写一段基石代码导入：

```
导入 数学工具 从 本地包
打印(数学工具.立方(3))        # 27（内部依赖了 基础库.平方）
打印(数学工具.平方和([1, 2, 3]))  # 14
```

卸载：

```bash
jishi 卸载 数学工具
jishi 卸载 基础库
```

## 方式二：手动复制到缓存目录

```bash
mkdir -p .jishi/packages
cp -r examples/packages/基础库 .jishi/packages/
cp -r examples/packages/数学工具 .jishi/packages/
```

## 依赖是怎么生效的

`数学工具` 的 `计算.jsh` 里直接用了 `基础库.平方`，无需再写 `导入 基础库`——
依赖在导入 `数学工具` 时被自动解析、校验版本（`基础库 >=1.0.0`）并注入命名空间。
