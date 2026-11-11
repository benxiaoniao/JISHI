"""收口演练：**装 → 用 → 升 → 打包 → 卸**，用**真的要发的那个安装器**。

为什么单独一个脚本而不是塞进 pytest：跑一次要一两分钟，会真的写注册表卸载项、
真的开窗、真的往开始菜单里放快捷方式，属「发版前的一次性验收」而不是每次提交都该跑。
它输出一份「验到了什么 / 没验到什么」的清单 —— 那份清单要抄进 CHANGELOG。

用法：

    python tools/drill_install.py                  # 0.3.1 → 0.3.2（缺省）
    python tools/drill_install.py 0.3.2 0.3.2      # 同版本覆盖装

⚠️ 只支持 Windows（要 Inno 的 `/VERYSILENT`）。macOS / Linux 走 tar.gz + install.sh，
那条路只有 M172 的判据覆盖，**没有真机装过** —— 如实记在路线图里。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]      # 本文件在 tools/ 下
REL = ROOT / "dist" / "release"
#: 0.3.0 的安装器在**官网仓库**的 dist 里（本仓库 dist 只留最新的）
#: 旧版 / 新版安装器：命令行给，缺省用「上一个已发布版 → 当前版」。
_旧版 = sys.argv[1] if len(sys.argv) > 1 else "0.3.1"
_新版 = sys.argv[2] if len(sys.argv) > 2 else "0.3.2"
OLD = REL / f"jishi-{_旧版}-windows-x64-setup.exe"
if not OLD.is_file():
    #: 旧版通常已经不在本仓库的 dist 里了（dist 只留最新），去官网镜像里找
    OLD = Path("D:/code/基石官网/dist/release") / f"jishi-{_旧版}-windows-x64-setup.exe"
NEW = REL / f"jishi-{_新版}-windows-x64-setup.exe"

结果 = []


def 记(项, 通过, 说明="", 已知旧版缺陷=False):
    """`已知旧版缺陷`：这一条在**旧版**上本来就该失败（比如 0.3.0 的窗口是坏的）。

    它不是演练失败 —— 恰恰是演练**要照出来的事实**。单独记，不计入失败数。
    """
    结果.append((项, 通过, 说明, 已知旧版缺陷))
    标 = "⚠️ 已知旧版缺陷" if 已知旧版缺陷 and not 通过 else ""
    print(("  ✅ " if 通过 else ("  ⚠️ " if 已知旧版缺陷 else "  ❌ ")) + 项
          + (f" —— {说明}" if 说明 else "") + (f" [{标}]" if 标 else ""))


def 跑(命令, timeout=120, cwd=None):
    r = subprocess.run([str(c) for c in 命令], capture_output=True, timeout=timeout,
                       input=b"", cwd=str(cwd) if cwd else None)
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def 装(setup, 目录):
    """Inno 的静默安装。`/DIR` 指定目录，`/NORESTART` 防它自作主张重启。"""
    return 跑([setup, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
               "/SP-", f"/DIR={目录}"], timeout=300)


def 卸(目录):
    for 名 in ("unins000.exe", "uninstall.exe"):
        p = Path(目录) / 名
        if p.is_file():
            return 跑([p, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], timeout=300)
    return (1, "找不到卸载器")


def 版本(目录):
    exe = Path(目录) / "bin" / "jishi.exe"
    if not exe.is_file():
        return None
    码, 出 = 跑([exe, "--version"])
    return 出.strip().splitlines()[0] if 码 == 0 else f"（退出码 {码}）{出[:80]}"


def main():
    if not OLD.is_file() or not NEW.is_file():
        print(f"缺安装器：{OLD.name} 或 {NEW.name}")
        return 1

    工作 = Path(tempfile.mkdtemp(prefix="jishi-drill-"))
    装目录 = 工作 / "装"
    print(f"演练目录：{装目录}")

    # ── 1. 装（0.3.0，已经在用户手里的那一版） ────────────────────────
    print(f"\n[1] 装 {_旧版}")
    码, 出 = 装(OLD, 装目录)
    记("安装器退出码 0", 码 == 0, 出[-200:] if 码 else "")
    记("装完有 bin/jishi.exe", (装目录 / "bin" / "jishi.exe").is_file())
    v = 版本(装目录)
    记(f"装的是 {_旧版}", v is not None and _旧版 in v, str(v))
    # 引擎素材必须跟着走：缺了就是「窗口开着、页面白着、零报错」（M174 的老坑）
    for 名 in ("jishi-wasm.js", "jishi_wasm.wasm", "WebView2Loader.dll"):
        记(f"bin/ 里有 {名}", (装目录 / "bin" / 名).is_file(), 已知旧版缺陷=True)

    # ── 2. 用 ─────────────────────────────────────────────────────────
    print("\n[2] 用（跑真程序）")
    程序 = 工作 / "hello.jsh"
    程序.write_text('导入 文本 为 文字\n打印("装了就能跑：" + 文字.重复("好", 2))\n',
                  encoding="utf-8", newline="\n")
    码, 出 = 跑([装目录 / "bin" / "jishi.exe", 程序])
    记("跑脚本能出结果", 码 == 0 and "装了就能跑：好好" in 出, 出.strip()[:80])

    码, 出 = 跑([装目录 / "bin" / "jishi.exe", "医生"])
    记("jishi 医生 能跑", 码 == 0, 出.strip().splitlines()[0] if 出.strip() else "")

    # 窗口：装完之后**页面素材真的在**才算「能用」。开 8 秒再关。
    页 = 工作 / "页.html"
    页.write_text('<!DOCTYPE html><meta charset="utf-8"><h1>演练</h1>'
                  '<script>if(window.ipc)window.ipc.postMessage(JSON.stringify({op:"页面已加载"}));</script>',
                  encoding="utf-8", newline="\n")
    窗口程序 = 工作 / "窗.jsh"
    窗口程序.write_text(
        '导入 窗口\n令 窗 = 窗口.打开({"标题": "M176 演练", "页面": "页.html"})\n窗口.等(窗)\n',
        encoding="utf-8", newline="\n")
    日志 = 工作 / "win.log"
    with open(日志, "w", encoding="utf-8") as f:
        进程 = subprocess.Popen([str(装目录 / "bin" / "jishi.exe"), str(窗口程序)],
                               cwd=str(工作), stdout=f, stderr=subprocess.STDOUT,
                               env={**os.environ, "JISHI_WINDOW_DEBUG": "1"})
    time.sleep(8)
    起了 = 进程.poll() is None
    if 起了:
        进程.kill()
        进程.wait(timeout=20)
    文 = 日志.read_text(encoding="utf-8", errors="replace")
    记("装完能开原生窗口（进程还活着）", 起了, 已知旧版缺陷=True)
    记("页面真的到了（不是白窗口）", "页面到了" in 文,
      文.strip().splitlines()[-1][:80] if 文.strip() else "无输出", 已知旧版缺陷=True)

    # ── 3. 升 ─────────────────────────────────────────────────────────
    print(f"\n[3] 升级到 {_新版}（覆盖装）")
    旧exe = (装目录 / "bin" / "jishi.exe").stat().st_size
    码, 出 = 装(NEW, 装目录)
    记("升级安装退出码 0", 码 == 0, 出[-200:] if 码 else "")
    v = 版本(装目录)
    记(f"升完版本是 {_新版}", v is not None and _新版 in v, str(v))
    新exe = (装目录 / "bin" / "jishi.exe").stat().st_size
    记("bin/jishi.exe 被替换（大小变了或至少还在）", 新exe > 0, f"{旧exe} → {新exe} 字节")
    for 名 in ("jishi-wasm.js", "jishi_wasm.wasm", "WebView2Loader.dll"):
        记(f"升级后 {名} 仍在", (装目录 / "bin" / 名).is_file())

    程序2 = 工作 / "hello2.jsh"
    程序2.write_text('打印("升级后还能跑")\n', encoding="utf-8", newline="\n")
    码, 出 = 跑([装目录 / "bin" / "jishi.exe", 程序2])
    记("升级后还能跑程序", 码 == 0 and "升级后还能跑" in 出, 出.strip()[:60])

    # 升级后**再开一次窗**：这才是 0.3.1 要证明的事（0.3.0 那版窗口是坏的，
    # 因为发行包里没带 wasm 素材 —— 这个演练正好把它照出来了）。
    日志2 = 工作 / "win2.log"
    with open(日志2, "w", encoding="utf-8") as f:
        进程2 = subprocess.Popen([str(装目录 / "bin" / "jishi.exe"), str(窗口程序)],
                                cwd=str(工作), stdout=f, stderr=subprocess.STDOUT,
                                env={**os.environ, "JISHI_WINDOW_DEBUG": "1"})
    time.sleep(8)
    起了2 = 进程2.poll() is None
    if 起了2:
        进程2.kill()
        进程2.wait(timeout=20)
    文2 = 日志2.read_text(encoding="utf-8", errors="replace")
    记("升级后能开原生窗口", 起了2)
    记("升级后页面真的到了（不是白窗口）", "页面到了" in 文2,
      文2.strip().splitlines()[-1][:80] if 文2.strip() else "无输出")

    # ── 4. 打包（自带运行时）：把「给别人的机器」那条路也走一遍 ────────────
    #
    # 为什么演练里还要做（`test_m177` 已经做了一遍）：那条判据用的是**开发树里拼出来**
    # 的引擎目录，而这里用的是**真的装完的发行版**。两者差在最容易出错的地方 ——
    # 引擎素材到底跟没跟着走、快捷方式到底指向哪儿、装到空目录之后还认不认路。
    print("\n[4] 打包（自带运行时）→ 装到空目录 → 双击风格地启动")
    用户包出 = 工作 / "用户包"
    用户包出.mkdir(exist_ok=True)
    源 = 工作 / "我的程序"
    源.mkdir(exist_ok=True)
    (源 / "页.html").write_text(
        '<!DOCTYPE html><meta charset="utf-8"><h1>用户包</h1>'
        '<script>if(window.ipc)window.ipc.postMessage(JSON.stringify({op:"页面已加载"}));</script>',
        encoding="utf-8", newline="\n")
    (源 / "程序.jsh").write_text(
        '导入 窗口\n令 窗 = 窗口.打开({"标题": "用户包演练", "页面": "页.html"})\n窗口.等(窗)\n',
        encoding="utf-8", newline="\n")
    码, 出 = 跑([装目录 / "bin" / "jishi.exe", "打包", 源, "--出", 用户包出,
                 "--名字", "演练程序", "--版本", "1.0.0", "--入口", "程序.jsh",
                 "--自带运行时", "--引擎", 装目录 / "bin"], timeout=300)
    记("jishi 打包 --自带运行时 退出码 0", 码 == 0, 出[-300:] if 码 else "")
    用户安装 = 用户包出 / "演练程序-1.0.0-windows-x64-setup.exe"
    记("出了用户安装包", 用户安装.is_file(),
      f"{用户安装.stat().st_size // 1024} KB" if 用户安装.is_file() else str(用户包出))

    用户装 = 工作 / "用户装"
    if 用户安装.is_file():
        # ⚠️ 不能 `capture_output=True`：自解压壳会拉起子进程并继承管道，
        #    主进程退出后读端还在等 EOF ⇒ 挂满超时（M176 的教训）。
        壳日志 = 工作 / "用户装.log"
        with open(壳日志, "w", encoding="utf-8") as f:
            码 = subprocess.run([str(用户安装), "--目标", str(用户装)],
                                stdout=f, stderr=subprocess.STDOUT, timeout=300).returncode
        记("用户包装到空目录 退出码 0", 码 == 0,
          壳日志.read_text(encoding="utf-8", errors="replace")[-200:] if 码 else "")
        记("用户包里引擎在 bin/", (用户装 / "bin" / "jishi.exe").is_file())
        记("用户包里的程序在 app/", (用户装 / "app" / "程序.jsh").is_file())
        记("用户包里有 运行时.json", (用户装 / "运行时.json").is_file())

        # 快捷方式：目标必须是**引擎**、程序文件当**参数**（M177 的核心）
        快捷 = (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
                / "Start Menu" / "Programs" / "演练程序.lnk")
        记("开始菜单里有快捷方式", 快捷.is_file(), str(快捷))
        解析 = ""
        if (用户装 / "uninstall.exe").is_file() and 快捷.is_file():
            _, 解析 = 跑([用户装 / "uninstall.exe", "--解析快捷方式", 快捷], timeout=120)
        记("快捷方式指向 bin 里的引擎", "bin\\jishi.exe" in 解析 or "bin/jishi.exe" in 解析,
          解析.strip()[:120] or "（读不出来）")
        记("快捷方式带上了 app 里的程序文件",
          "app\\程序.jsh" in 解析 or "app/程序.jsh" in 解析, 解析.strip()[:120])

        # 按快捷方式写的那样跑一次 —— 窗口要真的起来
        if (用户装 / "bin" / "jishi.exe").is_file():
            日志3 = 工作 / "win3.log"
            with open(日志3, "w", encoding="utf-8") as f:
                进程3 = subprocess.Popen(
                    [str(用户装 / "bin" / "jishi.exe"), str(用户装 / "app" / "程序.jsh")],
                    cwd=str(用户装), stdout=f, stderr=subprocess.STDOUT,
                    env={**os.environ, "JISHI_WINDOW_DEBUG": "1"})
            time.sleep(8)
            起了3 = 进程3.poll() is None
            if 起了3:
                进程3.kill()
                进程3.wait(timeout=20)
            文3 = 日志3.read_text(encoding="utf-8", errors="replace")
            记("用户包里的程序能开窗（进程还活着）", 起了3)
            记("用户包的页面真的到了（不是白窗口）", "页面到了" in 文3,
              文3.strip().splitlines()[-1][:80] if 文3.strip() else "无输出")

        # 卸干净（顺带验「卸载会删掉开始菜单里那条」）
        _, 卸出 = 跑([用户装 / "uninstall.exe", "--卸载", "--静默"], timeout=300)
        time.sleep(1)
        记("用户包能卸掉（bin/ 没了）", not (用户装 / "bin").exists(), 卸出[-160:])

    # ── 4.5 游戏包（M189 加）：真游戏能不能装、能不能动 ──────────────────
    #
    # 为什么单列一段：游戏包与用户包的区别不在打包（同一条路），而在**跑起来之后**：
    # 它靠 `图形.运行` 进常驻 VM，一帧一帧地跑。窗口开出来、页面到了，
    # **不代表帧循环在跑**（白画布也是「页面到了」）。
    # `打印` 在窗口模式下不会回到宿主 stdout（实测一行都没有），所以这里只能看**画面**：
    # 抓两次窗口截图，比校验和 —— 变了就说明有东西在动。
    print("\n[4.5] 游戏包（真游戏：装 → 开窗 → 画面真的在动 → 卸）")
    游戏源 = ROOT / "examples" / "desktop"
    游戏出 = 工作 / "游戏包"
    游戏出.mkdir(exist_ok=True)
    打砖块源 = 工作 / "打砖块源"
    打砖块源.mkdir(exist_ok=True)
    for 名 in ("打砖块.html", "打砖块.jsh", "打砖块_游戏.jsh", "游戏启动.js", "碰.wav"):
        shutil.copyfile(游戏源 / 名, 打砖块源 / 名)
    码, 出 = 跑([装目录 / "bin" / "jishi.exe", "打包", 打砖块源, "--出", 游戏出,
                 "--名字", "演练游戏", "--版本", "1.0.0", "--入口", "打砖块.jsh",
                 "--自带运行时", "--引擎", 装目录 / "bin"], timeout=300)
    记("游戏打包 退出码 0", 码 == 0, 出[-300:] if 码 else "")
    游戏安装 = 游戏出 / "演练游戏-1.0.0-windows-x64-setup.exe"
    记("出了游戏安装包", 游戏安装.is_file(),
      f"{游戏安装.stat().st_size // 1024} KB" if 游戏安装.is_file() else str(游戏出))

    if 游戏安装.is_file():
        游戏装 = 工作 / "游戏装"
        壳日志 = 工作 / "游戏装.log"
        with open(壳日志, "w", encoding="utf-8") as f:
            码 = subprocess.run([str(游戏安装), "--目标", str(游戏装)],
                                stdout=f, stderr=subprocess.STDOUT, timeout=300).returncode
        记("游戏包装到空目录 退出码 0", 码 == 0,
          壳日志.read_text(encoding="utf-8", errors="replace")[-200:] if 码 else "")
        for 名 in ("打砖块.html", "打砖块.jsh", "打砖块_游戏.jsh", "游戏启动.js", "碰.wav"):
            记(f"游戏包的 app/ 里有 {名}", (游戏装 / "app" / 名).is_file())
        记("游戏包里有 运行时.json", (游戏装 / "运行时.json").is_file())

        游戏日志 = 工作 / "win_game.log"
        with open(游戏日志, "w", encoding="utf-8") as f:
            进程4 = subprocess.Popen(
                [str(游戏装 / "bin" / "jishi.exe"), str(游戏装 / "app" / "打砖块.jsh")],
                cwd=str(游戏装), stdout=f, stderr=subprocess.STDOUT,
                env={**os.environ, "JISHI_WINDOW_DEBUG": "1"})
        time.sleep(8)
        活着 = 进程4.poll() is None
        文4 = 游戏日志.read_text(encoding="utf-8", errors="replace")
        记("游戏包能开窗（进程还活着）", 活着)
        记("游戏包页面真的到了", "页面到了" in 文4,
          文4.strip().splitlines()[-1][:80] if 文4.strip() else "无输出")
        记("游戏包的 wasm 引擎起来了", "引擎起来了" in 文4,
          "（页面里的游戏源码真跑了）" if "引擎起来了" in 文4 else "日志里没有这一行")

        if 活着:
            进程4.kill()
            进程4.wait(timeout=20)

        # 🔴 帧循环真在跑 —— 用**会动的探针**，不拿游戏自己当探针。
        #
        # 为什么不能拿打砖块：球掉下去之后是「结束」页，画面就不动了；
        # 8 秒足够一颗球掉下去（实测就是这样：6 次抓取同一张图）。
        # 探针只有一个职责：每帧把方块往右挪一点，永远不停 ⇒ 画面必须一直在变。
        try:
            sys.path.insert(0, str(ROOT / "tools"))
            import 窗口指纹 as 指纹

            探针 = 工作 / "探针"
            探针.mkdir(exist_ok=True)
            shutil.copyfile(游戏源 / "游戏启动.js", 探针 / "游戏启动.js")
            (探针 / "页面.html").write_text(
                '<!DOCTYPE html><html><head><meta charset="utf-8"><title>帧循环探针</title>'
                '</head><body data-游戏="探针.jsh"><canvas id="画布"></canvas>'
                '<script src="游戏启动.js"></script></body></html>',
                encoding="utf-8", newline="\n")
            (探针 / "探针.jsh").write_text(
                '导入 图形\n'
                '令 状态 = {"x": 0.0}\n'
                '函数 初始化()：\n'
                '    图形.准备({"宽": 400, "高": 240, "背景": "#101820"})\n'
                '函数 每帧(号, 间隔毫秒)：\n'
                '    状态["x"] = 状态["x"] + 4.0\n'
                '    如果 状态["x"] > 360：\n'
                '        状态["x"] = 0.0\n'
                '    图形.清屏("#101820")\n'
                '    图形.矩形(状态["x"], 100, 60, 60, {"填充": "#fde047"})\n'
                '图形.运行(初始化, 每帧)\n',
                encoding="utf-8", newline="\n")
            (探针 / "入口.jsh").write_text(
                '导入 窗口\n\n令 窗 = 窗口.打开({"标题": "帧循环探针", "页面": "页面.html"})\n'
                '窗口.等(窗)\n',
                encoding="utf-8", newline="\n")

            探针日志 = 工作 / "win_probe.log"
            with open(探针日志, "w", encoding="utf-8") as f:
                探针进程 = subprocess.Popen(
                    [str(游戏装 / "bin" / "jishi.exe"), str(探针 / "入口.jsh")],
                    cwd=str(探针), stdout=f, stderr=subprocess.STDOUT,
                    env={**os.environ, "JISHI_WINDOW_DEBUG": "1"})
            time.sleep(6)
            hwnd = 指纹.找窗口("帧循环探针")
            指纹.跳到前台(hwnd)      # 后台窗口的 WebView2 会停渲染
            time.sleep(0.6)
            样本 = []
            for _ in range(6):
                if hwnd:
                    样本.append(指纹.指纹(hwnd))
                time.sleep(0.4)
            样本 = [x for x in 样本 if x]
            记("帧循环真在跑（画面在变）", len(set(样本)) >= 2,
              f"{len(set(样本))} 种画面 / {len(样本)} 次抓取" if 样本 else "抓不到窗口")
            探针进程.kill()
            探针进程.wait(timeout=20)
        except Exception as e:                       # noqa: BLE001
            记("帧循环真在跑（画面在变）", False, f"探针失败：{e}")

        _, 卸出 = 跑([游戏装 / "uninstall.exe", "--卸载", "--静默"], timeout=300)
        time.sleep(1)
        记("游戏包能卸掉（bin/ 没了）", not (游戏装 / "bin").exists(), 卸出[-160:])

    # ── 5. 卸 ─────────────────────────────────────────────────────────
    print("\n[5] 卸载")
    码, 出 = 卸(装目录)
    记("卸载器退出码 0", 码 == 0, 出[-200:] if 码 else "")
    time.sleep(2)
    剩 = [p for p in 装目录.rglob("*")] if 装目录.exists() else []
    记("装目录已清空（或整个删掉）", not 剩, f"还剩 {len(剩)} 项：{[str(x.name) for x in 剩[:5]]}")
    记("bin/jishi.exe 没了", not (装目录 / "bin" / "jishi.exe").exists())

    # ── 汇总 ─────────────────────────────────────────────────────────
    坏 = [项 for 项, 过, _, 是旧版缺陷 in 结果 if not 过 and not 是旧版缺陷]
    过 = sum(1 for _, p, _, _ in 结果 if p)
    已知 = sum(1 for _, p, _, d in 结果 if d and not p)
    print(f"\n{'=' * 60}\n{过}/{len(结果)} 项通过"
          + (f"，另有 {已知} 项是**旧版已知缺陷**（新版已修）" if 已知 else ""))
    if 坏:
        print("没过：", 坏)
    print(f"工作目录（保留了现场，可手工看一眼）：{工作}")
    return 0 if not 坏 else 1


if __name__ == "__main__":
    sys.exit(main())
