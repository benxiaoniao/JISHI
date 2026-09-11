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
  ISCC 查找覆盖 Program Files / %LOCALAPPDATA%\Programs（`/CURRENTUSER` 装法）。
- **推 tag 不要推本地仓库**：本地 git 历史含旧版个人路径，用
  `python tools/publish_github.py --tag vX.Y.Z` 在屏蔽版快照上打 tag。
- 更多见 README「已踩过的坑」一节。

---

## 3. 当前任务与状态（各工具协作更新这一段）

- ✅ **M0–M19 全部完成**（语言核心 → AI 工具链 → 沙箱/MCP → 生态 →
  性能分发 → 评测闭环 → 跨语言分发 → 包管理 → 语言收敛 1.0 → 发行版 →
  排错体验补全 → 表达便利 → 发行改进）。
- 📌 **下一里程碑 M20：性能**（详见 `docs/技术路线_v1.0之后.md`）：
  短字符串内联原型（ROI 不达标则明确放弃）、热路径下沉 C VM、
  Rust 宿主可变容器语义（Rc<RefCell>）、JS VM 性能补测。
- 后续：M21 开发者工具（LSP 优先）→ M22 生态（按需）。

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
