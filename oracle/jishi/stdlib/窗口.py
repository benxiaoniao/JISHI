# -*- coding: utf-8 -*-
"""基石标准库：窗口。把程序**开成一个窗口**，而不是一个浏览器标签页。

## 它到底是什么

不是原生窗口（那是 0.4.0 的事，要 wry + 新 ABI）。这一版是**应用模式窗口**：
起一个只监听 `127.0.0.1` 的小服务，把页面喂给系统的 Chromium 内核浏览器，
用 `--app=` 打开 —— **没有地址栏、没有后退键、任务栏里是独立图标**。
看起来就是桌面程序，关闭窗口 = 进程退出。

## 为什么必须起服务，不能直接 `file://`

页面里跑的是基石的 **wasm 引擎**（M167），而 `file://` 下浏览器**不让 fetch wasm**
（同源策略）。所以窗口模块自带一个小服务：页面目录挂在 `/`，
引擎素材（`jishi-wasm.js` + `jishi_wasm.wasm`）挂在 `/__jishi/`。

⚠️ 只监听 `127.0.0.1`，端口由系统分配 —— **不对外网开放**，也不用担心端口冲突。

## 五个函数

```jsh
导入 窗口
令 窗 = 窗口.打开({"标题": "计算器", "页面": "计算器.html", "宽": 420, "高": 620})
窗口.等(窗)          # 阻塞到窗口关掉（没有它，脚本跑完就退、窗口一闪没了）
窗口.在运行(窗)      # 真 / 假
窗口.关闭(窗)
窗口.全部关闭()
```

## 如实写的三条边界（应用模式的固有短板）

| 做不到 | 为什么 |
|---|---|
| **改窗口标题** | 标题是页面的 `<title>`，系统窗口用的是它；`--app` 没有改标题的参数 |
| **原生菜单 / 托盘** | 那是浏览器进程的，不归我们管 |
| **拖文件进窗口** | 同上（网页能接 `drop` 事件，但那算「网页能力」不是「窗口能力」） |

这三条正是 0.4.0 换 **wry 原生窗口**要补的。⚠️ 换的时候**只换外壳**：
页面、wasm 引擎、`浏览器` 模块的事件模型都不动 —— 否则等于重做。

## 数据放在哪

浏览器给每个 `--user-data-dir` 一份独立的本地存储，所以 `浏览器.存本地` 的数据
按**程序**分开存，位置在 `~/.jishi/windows/<页面路径的指纹>/`。
同一个程序再开一次，数据还在；换个程序互不干扰。
"""

import hashlib
import http.server
import os
import shutil
import subprocess
import sys
import threading
from urllib.parse import quote

from ..errors import RunError, RunValueError

__all__ = ["打开", "关闭", "等", "在运行", "全部关闭"]

#: 引擎素材的挂载点（页面里写 `/__jishi/jishi-wasm.js` 就对了）。
引擎前缀 = "/__jishi/"

_句柄表 = {}          # 序号 -> 窗口记录
_下一个 = [1]
_服务表 = {}          # 规范化目录 -> (port, server, thread)
_锁 = threading.Lock()


# ---------------------------------------------------------------------------
# 引擎素材
# ---------------------------------------------------------------------------

def _找引擎():
    """`jishi-wasm.js` 与 `jishi_wasm.wasm` 在哪个目录（找不到返回空串）。

    顺序：装好的发行布局（可执行文件旁边）→ 开发时的 `site/` → `wasm/` 构建产物。
    """
    这里 = os.path.dirname(os.path.abspath(__file__))
    仓库 = os.path.abspath(os.path.join(这里, "..", "..", ".."))
    候选 = [
        os.path.dirname(os.path.abspath(sys.executable)),
        os.path.join(仓库, "site"),
        os.path.join(仓库, "wasm"),
    ]
    for d in 候选:
        两个 = (os.path.join(d, "jishi-wasm.js"), os.path.join(d, "jishi_wasm.wasm"))
        if all(os.path.isfile(p) for p in 两个):
            return d
    return ""


# ---------------------------------------------------------------------------
# 小服务
# ---------------------------------------------------------------------------

class _处理器(http.server.SimpleHTTPRequestHandler):
    """把页面目录挂在 `/`，把引擎素材挂在 `/__jishi/`。"""

    引擎目录 = ""

    def translate_path(self, path):  # noqa: N802（Python 标准库的命名）
        纯 = path.split("?", 1)[0].split("#", 1)[0]
        if self.引擎目录 and 纯.startswith(引擎前缀):
            相对 = 纯[len(引擎前缀):].lstrip("/")
            return os.path.join(self.引擎目录, 相对.replace("/", os.sep))
        return super().translate_path(path)

    def log_message(self, *args):   # 安静：别把每个请求都打到用户的终端上
        pass


def _起服务(目录: str):
    """在一个目录上起（或复用）服务，返回端口。"""
    键 = os.path.normcase(os.path.abspath(目录))
    with _锁:
        有 = _服务表.get(键)
        if 有:
            return 有[0]
        引擎 = _找引擎()
        处理器 = type("处理器", (_处理器,), {"引擎目录": 引擎})
        # 端口给 0 = 让系统分配一个空闲的；地址绑 127.0.0.1 = 不对外
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                              lambda *a, **k: 处理器(*a, directory=目录, **k))
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        _服务表[键] = (srv.server_address[1], srv, t)
        return srv.server_address[1]


def _停服务(目录: str):
    键 = os.path.normcase(os.path.abspath(目录))
    with _锁:
        有 = _服务表.pop(键, None)
    if 有:
        try:
            有[1].shutdown()
            有[1].server_close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 浏览器
# ---------------------------------------------------------------------------

def _找浏览器(指定) -> list:
    """找出一个 Chromium 内核的浏览器（应用模式只有它认 `--app=`）。

    `指定` 可以是**文本**（可执行文件路径），也可以是**列表**（命令 + 参数）——
    后者给「包在启动器里的浏览器」用（`flatpak run …`、自己的包装脚本），
    窗口参数会接在这个命令后面。判据也是靠它注入一个假浏览器来验的。
    """
    if isinstance(指定, (list, tuple)):
        if not 指定:
            raise RunValueError("「浏览器」给了个空列表")
        命令 = [str(x) for x in 指定]
        if not 命令[0].strip():
            raise RunValueError("「浏览器」的第一项不能是空的（那应该是可执行文件或命令）")
        return 命令
    if 指定:
        if os.path.isfile(指定):
            return [指定]
        raise RunValueError(f"「浏览器」指向的文件不存在：{指定}")
    候选 = []
    if sys.platform == "win32":
        基 = [os.environ.get("PROGRAMFILES", r"C:\Program Files"),
              os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
              os.environ.get("LOCALAPPDATA", "")]
        for b in 基:
            if not b:
                continue
            候选 += [os.path.join(b, "Microsoft", "Edge", "Application", "msedge.exe"),
                     os.path.join(b, "Google", "Chrome", "Application", "chrome.exe")]
    elif sys.platform == "darwin":
        候选 = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    else:
        for 名 in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
            p = shutil.which(名)
            if p:
                候选.append(p)
    for p in 候选:
        if p and os.path.isfile(p):
            return [p]
    raise RunValueError(
        "找不到能开应用窗口的浏览器（要 Edge / Chrome / Chromium 这类）",
        hint="装一个 Chromium 内核的浏览器，或者用「浏览器」选项指定它的可执行文件路径")


def _配置目录(页面: str) -> str:
    """这个程序自己的浏览器数据目录（`存本地` 就落在这里）。"""
    印 = hashlib.sha256(os.path.abspath(页面).encode("utf-8")).hexdigest()[:16]
    基 = os.path.join(os.path.expanduser("~"), ".jishi", "windows", 印)
    os.makedirs(基, exist_ok=True)
    return 基


# ---------------------------------------------------------------------------
# 五个函数
# ---------------------------------------------------------------------------

def _取文本(选项: dict, 键: str, 默认: str = "") -> str:
    v = 选项.get(键)
    if v is None:
        return 默认
    if not isinstance(v, str):
        raise RunValueError(f"「{键}」要传文本")
    return v


def _取数(选项: dict, 键: str, 默认: int) -> int:
    v = 选项.get(键)
    if v is None:
        return 默认
    if isinstance(v, bool) or not isinstance(v, int):
        raise RunValueError(f"「{键}」要传整数（像素）")
    return v


def 打开(选项) -> int:
    """开一个应用窗口，返回**句柄**（整数）。

    选项：

    | 键 | 说明 |
    |---|---|
    | `页面` | 一个 HTML 文件（会起个小服务喂给浏览器；wasm 要靠它） |
    | `服务` | 已经在跑的地址（`http://127.0.0.1:8000/`），二选一 |
    | `标题` | 只用于提示与数据目录；**应用模式改不了系统标题**（见模块说明） |
    | `宽` / `高` | 窗口尺寸（像素，默认 900×640） |
    | `浏览器` | 指定浏览器：可执行文件路径，或 `["命令", "参数"…]`（给了就不再自动找） |
    """
    if not isinstance(选项, dict):
        raise RunValueError('「窗口.打开」要一个字典，例如 {"标题": "计算器", "页面": "计算器.html"}')
    页面 = _取文本(选项, "页面")
    服务 = _取文本(选项, "服务")
    标题 = _取文本(选项, "标题") or "基石程序"
    宽 = _取数(选项, "宽", 900)
    高 = _取数(选项, "高", 640)

    if 页面 and 服务:
        raise RunValueError("「页面」和「服务」只能给一个：页面我们自己起服务，服务是你已经有的地址")
    if 服务:
        网址 = 服务
        目录 = ""
    elif 页面:
        全 = os.path.abspath(页面)
        if not os.path.isfile(全):
            raise RunValueError(f"找不到页面：{页面}")
        目录 = os.path.dirname(全)
        端口 = _起服务(目录)
        # ⚠️ 文件名要**百分号编码**：中文文件名直接塞进 URL 是非法请求行
        #    （HTTP 只认 ASCII），浏览器会自己编码、但别的东西（比如判据里
        #    用 urllib 去取）会当场报错。
        网址 = "http://127.0.0.1:%d/%s" % (端口, quote(os.path.basename(全)))
    else:
        raise RunValueError("「窗口.打开」要指定「页面」或「服务」")

    浏览器 = _找浏览器(选项.get("浏览器") if "浏览器" in 选项 else "")
    配置 = _配置目录(页面 or 网址)
    参数 = 浏览器 + [
        f"--app={网址}",
        f"--window-size={宽},{高}",
        f"--user-data-dir={配置}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=TranslateUI",
    ]
    try:
        进程 = subprocess.Popen(参数, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise RunError(f"起不来窗口（{浏览器[0]}）：{e}") from None

    with _锁:
        号 = _下一个[0]
        _下一个[0] += 1
        _句柄表[号] = {"进程": 进程, "目录": 目录, "网址": 网址, "标题": 标题}
    return 号


def 关闭(句柄: int) -> bool:
    """关掉这个窗口（结束它的浏览器进程）。已经关了返回 `假`。"""
    记录 = _取(句柄)
    if 记录 is None:
        return False
    进程 = 记录["进程"]
    if 进程.poll() is None:
        try:
            进程.terminate()
        except OSError:
            pass
    return True


def 在运行(句柄: int) -> bool:
    """这个窗口还开着吗（浏览器进程还活着）。"""
    记录 = _取(句柄)
    return bool(记录) and 记录["进程"].poll() is None


def 等(句柄: int) -> int:
    """**阻塞到窗口关掉**，返回它的退出码。

    没有它，脚本执行完就退出了 —— 窗口会一闪而过（服务也跟着停）。

    📌 对一个**已经关掉**的窗口调用它**立刻返回 0**（不报错）：收尾时先
    `全部关闭()` 再 `等()` 是常见写法，那条路必须安全。
    """
    记录 = _取(句柄)
    if 记录 is None:
        return 0
    try:
        码 = 记录["进程"].wait()
    except KeyboardInterrupt:
        关闭(句柄)
        码 = 130
    _收尾(句柄)
    return 码


def 全部关闭() -> int:
    """把所有窗口关掉，返回关了几个。"""
    with _锁:
        号们 = list(_句柄表)
    for 号 in 号们:
        关闭(号)
        _收尾(号)
    return len(号们)


def _取(句柄: int):
    if not isinstance(句柄, int) or isinstance(句柄, bool):
        return None
    with _锁:
        return _句柄表.get(句柄)


def _收尾(句柄: int):
    """窗口结束后：从表里摘掉，没人再用那个目录就停掉服务。"""
    with _锁:
        记录 = _句柄表.pop(句柄, None)
        if 记录 is None:
            return
        还有 = any(x["目录"] == 记录["目录"] for x in _句柄表.values())
    if 记录["目录"] and not 还有:
        _停服务(记录["目录"])
