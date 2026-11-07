# -*- coding: utf-8 -*-
"""基石标准库：窗口。把程序**开成一个窗口**，而不是一个浏览器标签页。

## 两代外壳（M173 应用模式 / M174 原生窗口）

```text
   页面（html 构件） + wasm 引擎（`浏览器` 模块的事件模型）
          ↑ 一行不改
   外壳 v1：浏览器 --app=（本文件就是这条）  →  外壳 v2：wry 原生窗口（Rust 侧）
```

**本文件是 v1（应用模式）**：起一个只监听 `127.0.0.1` 的小服务，把页面喂给
系统的 Chromium 内核浏览器，用 `--app=` 打开 —— 没有地址栏、没有后退键、
任务栏里是独立图标，看起来就是桌面程序，关闭窗口 = 进程退出。

v2（原生窗口）在 **Rust 侧的 `rust/src/native_window.rs`**，`窗口.打开` 的
`原生` 选项选它。⚠️ **Python / Node 两个宿主只有 v1** —— 它们没有窗口能力
（`原生: 真` 会明确报错，不静默降级）。

## 为什么必须起服务，不能直接 `file://`

页面里跑的是基石的 **wasm 引擎**（M167），而 `file://` 下浏览器**不让 fetch wasm**
（同源策略）。所以窗口模块自带一个小服务：页面目录挂在 `/`，
引擎素材（`jishi-wasm.js` + `jishi_wasm.wasm`）挂在 `/__jishi/`。

⚠️ 只监听 `127.0.0.1`，端口由系统分配 —— **不对外网开放**，也不用担心端口冲突。

两个外壳**共用同一个服务**（页面拿到的 URL 一模一样），所以「换外壳」不影响页面。

## 六个函数

```jsh
导入 窗口
令 窗 = 窗口.打开({"标题": "计算器", "页面": "计算器.html", "宽": 420, "高": 620})
窗口.等(窗)          # 阻塞到窗口关掉（没有它，脚本跑完就退、窗口一闪没了）
窗口.在运行(窗)      # 真 / 假
窗口.关闭(窗)
窗口.全部关闭()
```

## 多窗口（M178）

一个进程可以开**多个**窗口（Rust 侧原生窗口；M178 之前是「只能一个」）：

```jsh
令 主 = 窗口.打开({"标题": "主窗口", "页面": "主.html"})
令 侧 = 窗口.打开({"标题": "侧栏", "页面": "侧.html", "宽": 320})
窗口.设标题("主窗口 2.0", 主)      # 只改主窗口的标题
窗口.设菜单([...], 侧)              # 侧栏自己的一套菜单
窗口.关闭(侧)                       # 只关卡槽那个，主窗口照常
窗口.等(主)                         # 🔴 等的是**所有**窗口都关掉，不是「主窗关掉」
```

🔴 **`等` 的语义**：一个进程只有一个事件循环，跑起来之后所有窗口一起活着。
所以「等某个窗口关掉就返回」**做不到** —— 返回了循环就停，其它窗口会假死
（画面还在、点了没反应）。定成「**跑到最后一个窗口关闭才返回**」，传哪个句柄都
一样；`等()` 不写句柄也是同一个意思。这是唯一一处已知的不完美。

📌 可选的 `句柄` 一律放**末尾**（`设标题(新标题, 句柄)` / `设菜单(定义, 句柄)`），
不写 = **主窗口**（最先开的那个还活着的）。老写法 `设标题("新标题")` 一行不改，
行为跟以前一样。

## 如实写的边界（两代外壳差在哪）

| 能力 | 应用模式（v1） | 原生窗口（v2，Rust） |
|---|---|---|
| **改窗口标题** | ❌ 只认页面的 `<title>` | ✅ `窗口.设标题` / ABI 3 |
| **多窗口** | ✅ 每个窗口一个浏览器进程 | ✅ 单事件循环 + 多窗口（M178） |
| **每窗口不同菜单** | ❌ | ✅ Windows / Linux；⚠️ macOS 菜单栏归应用，只有最后设的那套 |
| 无地址栏、独立任务栏图标 | ✅ | ✅ |
| 原生菜单 / 托盘 | ❌ 浏览器进程的，不归我们管 | ⏳ M175 |
| 拖文件进窗口 | ❌ 算「网页能力」不算「窗口能力」 | ⏳ M175 |

⚠️ 换外壳时**只换外壳**：页面、wasm 引擎、`浏览器` 模块的事件模型都不动。

🔴 **页面里的代码怎么碰到窗口**（M174 的 ABI 3）：`窗口.设标题` / `窗口.关闭()`
在 wasm 里会走 `jishi_native` 导入 → shim → 宿主。两个外壳都认：
原生窗口改真标题，应用模式改 `document.title`（`--app` 窗口的标题栏显示的就是它）。

🔴 **分发时别漏 `WebView2Loader.dll`**（Windows）：原生窗口依赖它，
少了它 `jishi.exe` **一启动就挂**（`0xC0000135`，双击没反应、也没有报错）。
正式安装包会自动带上；手工拷 `jishi.exe` 给别人的话，把同目录那份 DLL 一起拷。

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

__all__ = [
    "打开", "关闭", "设标题", "等", "在运行", "全部关闭",
    # M175：桌面集成（Python 宿主一个都做不到，见文件末尾 —— 会明确报错）
    "设菜单", "设右键菜单", "设托盘", "关托盘", "单实例", "选文件", "存文件", "当",
]

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
    | `原生` | **三态**：不写 = 自动（能原生就原生）；`真` = 一定要原生（本宿主做不到就报错）；`假` = 一定要应用模式 |
    """
    if not isinstance(选项, dict):
        raise RunValueError('「窗口.打开」要一个字典，例如 {"标题": "计算器", "页面": "计算器.html"}')
    # M174：`原生` 三态。
    #
    # | 写法 | 意思 |
    # |---|---|
    # | 不写（缺省） | **自动**：宿主有原生窗口就用，没有就退回应用模式 |
    # | `原生: 真` | 一定要原生 —— 做不到就**报错**，不静默降级 |
    # | `原生: 假` | 一定要应用模式（判据都这么写：不弹真窗口） |
    #
    # 「自动」是为了让同一段用户代码在三个宿主上都能跑：Rust 发行版给原生窗口，
    # Python / Node 没有那个能力，退回应用模式。**能力差异如实标**，
    # 而不是让缺省值在两边不一样（那才是真正的分叉）。
    原生 = 选项.get("原生")
    if 原生 is not None and not isinstance(原生, bool):
        raise RunValueError(
            f"「原生」要传真或假，给的是「{原生}」",
            hint="不写就是自动：宿主能开原生窗口就开，不能就用浏览器应用模式",
        )
    if 原生 is True:
        # 用「运行期错误」而不是「数值不对」：这不是参数给错了 —— 参数完全合法，
        # 是这个**宿主**没有那个能力。错误码要让人一眼看出该换宿主还是换写法。
        raise RunError(
            "这个宿主没有原生窗口能力（只有 Rust 发行版有）",
            hint='去掉「原生: 真」，或写「原生: 假」用浏览器应用模式',
        )
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
        # 🔴 起服务**之前**就把素材查清楚：找不到就别开窗。
        #
        # 为什么在这查：素材缺失时窗口照样能打开，但页面里的 `import
        # "/__jishi/jishi-wasm.js"` 会 404 ⇒ **白窗口、一行报错都没有**。
        # 那种「看起来开了、其实什么都没跑」是最难查的故障。
        # （M173 起就是坏的：发行包没带素材，用户装了基石也开不出能用的窗口。）
        if not _找引擎():
            raise RunError(
                "找不到 wasm 引擎素材（jishi-wasm.js + jishi_wasm.wasm）——"
                "窗口里的基石代码跑不起来",
                hint="它们应当和 jishi 可执行文件在**同一目录**（发行包会自动带上）；"
                     "在源码仓库里先跑 `python tools/build_wasm.py` 生成到 site/",
            )
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


def 关闭(句柄=None) -> bool:
    """关掉这个窗口（结束它的浏览器进程）。已经关了返回 `假`。

    **省略句柄**（M174）是给**页面里的代码**用的：`窗口.关闭()` 表示「关掉
    我自己所在的窗口」。那条路径走 ABI 3，只有 wasm 里（真在窗口里的代码）
    才有意义 —— 所以在脚本里省略句柄会明确报错，而不是关掉一个猜出来的窗口。
    """
    if 句柄 is None:
        # 用 E2000（运行期错误）而不是 E2005（数值不对）：参数并没有「数值不对」，
        # 是**这种形态**只在页面里成立。错误码要能让人一眼看出该改哪里。
        raise RunError(
            "「窗口.关闭()」省略句柄是给**页面里的代码**用的（关掉自己所在的窗口）",
            hint="脚本里请写 窗口.关闭(句柄)；页面里（跑在 wasm 里）才用无参形式",
        )
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


def 设标题(新标题, 句柄=None) -> bool:
    """改窗口标题。**这个宿主做不到**（M174，如实报错而不是静默无效）。

    改标题要能碰到系统的窗口对象 —— 只有 Rust 侧的原生窗口（wry）有那个能力。
    应用模式（浏览器 `--app`）下窗口标题就是页面的 `<title>`：
    页面里改 `document.title` 就行，从外面改不了。

    页面里的基石代码可以调它 —— 那条路径走 ABI 3（wasm → shim → 宿主），
    这个 Python 模块**不是**那条路径（页面代码不在这里跑）。

    📌 M178：多了个可选的 `句柄`（指定改哪个窗口的标题），但这里**照样报错**
    —— 报错的原因是能力缺失（应用模式碰不到窗口对象），跟传不传句柄无关。
    签名对齐是为了让三种宿主的「参数个数」一致，代码在 Python 上跑的时候
    能在同一处失败、给出同一句话。
    """
    raise RunError(
        "这个宿主改不了窗口标题（只有 Rust 侧的原生窗口能。）",
        hint='页面里改 document.title 即可（--app 窗口的标题栏显示的就是它）；'
             "或者在 Rust 发行版里用 窗口.打开({原生: 真})",
    )


def 在运行(句柄: int) -> bool:
    """这个窗口还开着吗（浏览器进程还活着）。"""
    记录 = _取(句柄)
    return bool(记录) and 记录["进程"].poll() is None


def 等(句柄=None) -> int:
    """**阻塞到窗口关掉**，返回它的退出码。不写句柄 = 等**所有**窗口关掉。

    没有它，脚本执行完就退出了 —— 窗口会一闪而过（服务也跟着停）。

    📌 对一个**已经关掉**的窗口调用它**立刻返回 0**（不报错）：收尾时先
    `全部关闭()` 再 `等()` 是常见写法，那条路必须安全。

    📌 **M178 起可以不写句柄**。三种宿主的口径统一成「等所有窗口」：
    Rust 侧原生窗口是**一个事件循环管所有窗口**（等的是循环退出 = 全关），
    浏览器应用模式（本文件 / Node）每个窗口一个子进程，这里就逐个 `wait`。
    所以两种外壳下「等()」的**可观察行为一致**：所有窗口都没了才返回。
    """
    if 句柄 is None:
        with _锁:
            号们 = list(_句柄表)
        for 号 in 号们:
            记录 = _取(号)
            if 记录 is not None:
                try:
                    记录["进程"].wait()
                except KeyboardInterrupt:
                    关闭(号)
            _收尾(号)
        return 0
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


# ---------------------------------------------------------------------------
# M175：桌面集成 —— **Python 宿主一个都做不到**
# ---------------------------------------------------------------------------
#
# 这一站的东西（菜单 / 托盘 / 右键菜单 / 单实例 / 文件对话框 / 宿主事件）
# 全都要求「窗口是我们自己的进程」。Python 宿主只能用**浏览器应用模式**
# （M173）：窗口是浏览器进程的，菜单、托盘、对话框都不归我们管。
#
# 🔴 为什么不留空的（返回假 / 什么都不做）：那样用户会以为设上了，
#    跑起来才发现右键没反应 —— 与 M174 那条「白窗口、零报错」是同一类坑。
#    **没有能力就说没有，并告诉他去哪能有。**


def _做不到(名: str, 提示: str):
    raise RunValueError(
        f"这个宿主没有原生窗口能力，「窗口.{名}」用不了"
        "（Python 宿主只能走浏览器应用模式）",
        hint=提示)


def 设菜单(定义, 句柄=None) -> bool:
    """M175：挂菜单栏（脚本侧）。**只有 Rust 宿主 + 原生窗口能做。**"""
    _做不到("设菜单", "用 Rust 发行版（窗口.打开 默认就是原生窗口）")


def 设右键菜单(定义, 句柄=None) -> bool:
    """M175：设右键菜单（脚本侧）。**只有 Rust 宿主 + 原生窗口能做。**"""
    _做不到("设右键菜单", "用 Rust 发行版；页面里也可以自己处理 contextmenu 事件")


def 设托盘(选项) -> bool:
    """M175：托盘图标（脚本侧）。**只有 Rust 宿主 + 原生窗口能做。**"""
    _做不到("设托盘", "用 Rust 发行版")


def 关托盘() -> bool:
    """M175：收掉托盘图标。**只有 Rust 宿主 + 原生窗口能做。**"""
    _做不到("关托盘", "用 Rust 发行版")


def 单实例(名: str) -> bool:
    """M175：单实例（返回 真 = 我是第一个）。**只有 Rust 宿主 + 原生窗口能做。**"""
    _做不到("单实例", "用 Rust 发行版")


def 选文件(选项=None):
    """M175：原生「打开文件」对话框（脚本侧）。**只有 Rust 宿主 + 原生窗口能做。**"""
    _做不到("选文件", "用 Rust 发行版；页面里可以用 <input type=\"file\">")


def 存文件(选项=None):
    """M175：原生「保存文件」对话框（脚本侧）。**只有 Rust 宿主 + 原生窗口能做。**"""
    _做不到("存文件", "用 Rust 发行版；页面里可以用 <a download>")


def 当(事件, 函数) -> bool:
    """M175：页面里登记「宿主事件 → 基石函数」。**只在页面（wasm）里有意义。**"""
    _做不到("当", "它要写进页面的基石代码里（跑在 wasm 里的那份），不是脚本里")
