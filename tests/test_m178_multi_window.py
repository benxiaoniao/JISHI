# -*- coding: utf-8 -*-
"""M178：多窗口（单事件循环 + 多窗口）。

## 这一站改了三个地方

1. **`native_window.rs`**：`static 全: Option<全局>` → 按号索引的
   `窗们` / `网们` / `就绪们` / `待发们`，事件带窗口号；菜单改成每窗口一套。
2. **`窗口` 的脚本层**：记录里记住「宿主那边的窗号」；`设标题` / `设菜单` /
   `设右键菜单` 多一个**末尾可选**的句柄；`等` 允许不写句柄。
3. **三种宿主的签名对齐**（Python / Node 是参考实现，Rust 是真跑的那个）：
   `等(句柄=空)`、`设标题(新标题, 句柄=空)`、`设菜单(定义, 句柄=空)`。

## 判据怎么做到「不靠人点」

- **结构 / 签名 / 报错文案**这几条不需要桌面，CI 上照跑。
- **真开三个窗口**的那几条会真的弹窗（每个几百毫秒），用
  `EnumWindows` + `GetWindowThreadProcessId` 按**进程**找窗口（不按标题 ——
  标题正是被测的东西），再看标题、发 `WM_COMMAND` 点菜单、发 `WM_CLOSE` 关窗。
  CI 上没有桌面 ⇒ `JISHI_NO_GUI=1` 显式跳过，**跳过不是通过**（如实记在 CHANGELOG）。

🔴 这一站最容易「看起来对」的坑是**事件串窗**：三个窗口共用一个事件循环，
菜单动作、拖放、页面 IPC 只要有一处漏了窗口号，就会打到别的窗口上 ——
而且看起来像随机（取决于哪个页面的处理函数先跑）。所以 `test_点A的菜单只到A`
是这一站最值钱的一条。
"""

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import rust_exe_path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WASM = ROOT / "site" / "jishi_wasm.wasm"

#: 没有桌面就跳过「真开窗口」的那几条。CI 里设 `JISHI_NO_GUI=1`。
无界面 = bool(os.environ.get("JISHI_NO_GUI"))

需要原生 = pytest.mark.skipif(
    无界面 or not sys.platform.startswith("win"),
    reason="真开原生窗口要桌面环境（CI 设 JISHI_NO_GUI=1）",
)


# ---------------------------------------------------------------------------
# 一、结构：状态必须按号存（没有桌面也能查）
# ---------------------------------------------------------------------------

def _源码() -> str:
    return (ROOT / "rust" / "src" / "native_window.rs").read_text(encoding="utf-8")


def test_窗口状态是按号存的():
    """`窗们` / `网们` / `就绪们` / `待发们` 都得是「按窗口号索引」的容器。

    这条是**文本比对**，看着土，但它挡的是最贵的一类回归：把某张表悄悄改回
    「进程里一份」（`Option<...>`），代码照样编过、单窗口测试照样全绿，
    多窗口就退化成「第二个窗口串到第一个上」。
    """
    s = _源码()
    for 名字, 类型 in [
        ("窗们", "RefCell<HashMap<u64, 窗件>>"),
        ("网们", "RefCell<HashMap<u64, wry::WebView>>"),
        ("待发们", "RefCell<HashMap<u64, Vec<(String, String)>>>"),
    ]:
        assert f"static {名字}: {类型}" in s, f"`{名字}` 不再是按号索引的表了：{类型}"
    assert "就绪们: RefCell<std::collections::HashSet<u64>>" in s, "「页面就绪」没按窗口分开"


def test_事件带窗口号():
    """`事件::关窗(u64)` 与 `事件::页面(u64, 名字, 参数)`。

    事件不带号的话，「关窗」就变成「关掉随便哪一个」—— 多窗口下第一件事就是
    点 A 的 × 却关了 B。
    """
    s = _源码()
    assert "关窗(u64)," in s or "关窗(u64)" in s, "关窗事件没有窗口号"
    assert "页面(u64, String, String)," in s, "页面事件没有窗口号"


def test_菜单是每窗口一套_托盘仍然只有一个():
    """菜单栏 / 右键菜单按窗口存；**托盘是进程级的**（系统里没有每窗口一个图标）。

    这条防的是「顺手把托盘也改成 HashMap」—— 那会让 `设托盘` 在三个窗口里
    建出三个图标，而 Windows 通知区只有一个位置给同一个程序。
    """
    s = _源码()
    assert "菜单栏: HashMap<u64, muda::Menu>" in s, "菜单栏没有按窗口分开"
    assert "右键: HashMap<u64, muda::Menu>" in s, "右键菜单没有按窗口分开"
    assert "托盘: Option<tray_icon::TrayIcon>" in s, "托盘不该按窗口分开（它本来就是进程级）"


def test_不再有只能开一个的限制():
    """老的那句 `一个程序只能有一个原生窗口` 必须消失 —— 它是 M174 的临时限制。

    留着它 = 第二个 `窗口.打开` 还是被拒；而这行字在源码里很容易被复制到别处。
    """
    s = _源码()
    assert "一个程序只能有一个原生窗口" not in s, "旧限制还在（第二个窗口会被拒）"


def test_三宿主的窗口签名一致():
    """`等` 可无参、`设标题` / `设菜单` / `设右键菜单` 第二个参数是可选句柄。

    签名表由 Python 侧生成（`tools/gen_stdlib_sigs.py`），这里**读三份产物**
    比对：Python 的函数定义、Rust 的 `stdlib_sigs.rs`、Node 的 `stdlib_sigs.js`。
    少了任何一边，`窗口.设标题("x", 窗)` 就会在某个宿主上报「参数太多」。
    """
    import inspect
    import re

    from oracle.jishi.stdlib import 窗口

    期 = {
        "等": (0, ["句柄"]),
        "设标题": (1, ["新标题", "句柄"]),
        "设菜单": (1, ["定义", "句柄"]),
        "设右键菜单": (1, ["定义", "句柄"]),
    }

    # ① Python（事实源）
    for 名, (必填, 参名) in 期.items():
        参 = inspect.signature(getattr(窗口, 名)).parameters
        assert list(参) == 参名, f"Python 侧 `{名}` 的参数名对不上：{list(参)}"
        有默认 = sum(1 for p in 参.values() if p.default is not inspect.Parameter.empty)
        assert len(参) - 有默认 == 必填, f"Python 侧 `{名}` 的必填个数不对"

    # ② Rust
    rs = (ROOT / "rust" / "src" / "stdlib_sigs.rs").read_text(encoding="utf-8")
    for 名, (必填, 参名) in 期.items():
        m = re.search(rf'"窗口\.{名}" => \((\d+), &\[([^\]]*)\]', rs)
        assert m, f"Rust 签名表里没有 `窗口.{名}`"
        assert int(m.group(1)) == 必填, f"Rust 侧 `{名}` 必填个数不对：{m.group(1)}"
        得 = re.findall(r'"([^"]+)"', m.group(2))
        assert 得 == 参名, f"Rust 侧 `{名}` 的参数名对不上：{得}"

    # ③ Node
    js = (ROOT / "oracle" / "node" / "stdlib_sigs.js").read_text(encoding="utf-8")
    窗口段 = js.split("'窗口': {", 1)[1].split("},", 1)[0]
    for 名, (必填, 参名) in 期.items():
        m = re.search(rf"'{名}': \[(\d+), \[([^\]]*)\]", 窗口段)
        assert m, f"Node 签名表里没有 `窗口.{名}`"
        assert int(m.group(1)) == 必填, f"Node 侧 `{名}` 必填个数不对：{m.group(1)}"
        得 = re.findall(r"'([^']+)'", m.group(2))
        assert 得 == 参名, f"Node 侧 `{名}` 的参数名对不上：{得}"


# ---------------------------------------------------------------------------
# 二、不需要桌面：无窗口时的行为与报错
# ---------------------------------------------------------------------------

def _跑基石(源, tmp_path, exe=None, 环境=None, timeout=300):
    import subprocess

    (tmp_path / "程序.jsh").write_text(源, encoding="utf-8", newline="\n")
    命令 = ([str(exe)] if exe else [sys.executable, "-m", "oracle.jishi.cli"])
    env = dict(os.environ)
    if 环境:
        env.update(环境)
    r = subprocess.run(命令 + ["程序.jsh"], cwd=str(tmp_path), capture_output=True,
                       timeout=timeout, input=b"", env=env)
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def test_等可以不写句柄_没有窗口时立刻返回(tmp_path):
    """`窗口.等()` 在三种宿主上都合法（M178 新允许无参），没有窗口时立刻返回 0。

    为什么先验这一条：签名放开之后最容易出的事是「无参进去拿不到句柄 →
    报类型错」或者「死等一个不存在的窗口」。两头都很难在生产里发现。
    """
    for 引擎 in ("python", "rust"):
        exe = None
        if 引擎 == "rust":
            exe = rust_exe_path()
            if not exe.is_file():
                continue
        码, 出 = _跑基石('''导入 窗口
打印("码", 窗口.等())
''', tmp_path, exe=exe, timeout=120)
        assert 码 == 0, f"[{引擎}] 退出码 {码}：{出[:400]}"
        assert "码 0" in 出, f"[{引擎}] 没有窗口时 等() 应当立刻返回 0：{出[:400]}"


def test_句柄不存在时设标题要报错(tmp_path):
    """`设标题("x", 9)`：9 号窗口不存在 ⇒ 报错说清「没有这个句柄」。

    🔴 **不能静默当成主窗口**：那样用户以为改的是 9 号窗，其实改的是 1 号，
    而且屏幕上确实有一个标题变了 —— 这种「看起来成功」的错最难查。
    """
    exe = rust_exe_path()
    if not exe.is_file():
        pytest.skip("没有 jishi-rs（先 cargo build --release）")
    码, 出 = _跑基石('''导入 窗口
窗口.设标题("随便", 9)
''', tmp_path, exe=exe, timeout=120)
    assert 码 != 0, f"应当报错：{出[:600]}"
    assert "没有窗口句柄 9" in 出, f"报错没说清是哪个句柄：{出[:600]}"


def test_句柄参数要整数(tmp_path):
    """`设标题("x", "不是数字")` ⇒ 类型错误（而不是悄悄忽略第二个参数）。"""
    exe = rust_exe_path()
    if not exe.is_file():
        pytest.skip("没有 jishi-rs（先 cargo build --release）")
    码, 出 = _跑基石('''导入 窗口
窗口.设标题("随便", "窗口一")
''', tmp_path, exe=exe, timeout=120)
    assert 码 != 0, f"应当报错：{出[:600]}"
    assert "窗口句柄" in 出, f"报错没提句柄：{出[:600]}"


# ---------------------------------------------------------------------------
# 三、真开窗口（要桌面）
# ---------------------------------------------------------------------------

常驻页 = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>常驻</title></head>
<body><h1>常驻</h1></body></html>
"""

#: 会跑真 wasm 引擎、登记「菜单 → 改自己标题」的页面。
#: 每个窗口一份**自己的** wasm 实例，所以「谁改的标题」直接看得出来。
菜单页 = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>接菜单</title></head>
<body><h1>接菜单</h1>
<script>
(async () => {
  function 报(op, arg) {
    if (window.ipc && window.ipc.postMessage) {
      window.ipc.postMessage(JSON.stringify({ op: op, arg: arg || "" }));
    }
  }
  try {
    const JishiWasm = (await import("/__jishi/jishi-wasm.js")).default || window.JishiWasm;
    const j = await JishiWasm.load("/__jishi/jishi_wasm.wasm");
    // 处理函数里**只改自己这个窗口**的标题（页面不知道别的窗口存在）
    const 源码 = [
      "导入 窗口",
      "函数 接菜单(动作)：",
      "    窗口.设标题(\"菜单→\" + 动作)",
      "窗口.当(\"菜单\", 接菜单)",
    ].join("\n");
    const r = j.run(源码);
    报(r.ok ? "引擎就绪" : "引擎失败", r.ok ? "" : JSON.stringify(r.error));
  } catch (e) {
    报("引擎失败", String((e && e.message) || e));
  }
})();
</script></body></html>
"""


def _自杀页(毫秒: int) -> str:
    """过一会儿**自己关自己**的页面（走 ABI 的 `窗口.关闭()`，不带句柄）。

    这条正是「页面 → 宿主：关我自己那个窗口」的验证路径：页面不知道自己在几号
    窗口里，宿主靠 IPC 来自哪个 WebView 来认定。
    """
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>自杀</title>
</head><body><h1>自杀</h1><script>
setTimeout(function () {{
  window.ipc.postMessage(JSON.stringify({{ op: "关闭" }}));
}}, {毫秒});
</script></body></html>
"""


def _win32():
    import ctypes
    import ctypes.wintypes as wt

    u = ctypes.windll.user32
    for 名, 返回, 参 in [
        ("FindWindowW", wt.HWND, [wt.LPCWSTR, wt.LPCWSTR]),
        ("GetMenu", wt.HMENU, [wt.HWND]),
        ("GetSubMenu", wt.HMENU, [wt.HMENU, ctypes.c_int]),
        ("GetWindowTextW", ctypes.c_int, [wt.HWND, wt.LPWSTR, ctypes.c_int]),
        ("IsWindow", wt.BOOL, [wt.HWND]),
    ]:
        f = getattr(u, 名)
        f.restype = 返回
        f.argtypes = 参
    u.GetMenuItemID.restype = ctypes.c_uint
    u.GetMenuItemID.argtypes = [wt.HMENU, ctypes.c_int]
    u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    return u


def _本进程的窗口(u, pid: int, 前缀: str) -> dict:
    """`{标题: hwnd}` —— 这个进程里、标题以 `前缀` 开头的**可见**顶层窗口。

    🔴 三个坑都踩过：
    - **按进程号过滤**（不按标题找）——标题正是被测的东西，拿它当入口就成了
      「先假设它没变」；
    - **必须 `IsWindowVisible`**：每个进程都有一堆隐藏的系统窗口
      （`MSCTFIME UI` / `Default IME`），它们**有标题**，混进来会让
      「应当有 3 个窗口」这种断言莫名其妙地过/不过；
    - 过滤空标题：WebView2 会建一些无标题的辅助窗口。
    """
    import ctypes
    import ctypes.wintypes as wt

    u.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM), wt.LPARAM]

    找到: dict = {}

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def 回调(hwnd, _):
        进程 = wt.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(进程))
        if 进程.value == pid and u.IsWindowVisible(hwnd):
            缓 = ctypes.create_unicode_buffer(512)
            u.GetWindowTextW(hwnd, 缓, 512)
            if 缓.value.startswith(前缀):
                找到[缓.value] = hwnd
        return True

    u.EnumWindows(回调, 0)
    return 找到


def _等窗口数(u, pid: int, 前缀: str, 个数: int, 秒=40.0) -> dict:
    """等到「标题以 `前缀` 开头的可见窗口」正好 `个数` 个（超时就返回最后一次结果）。

    为什么不是「等到 ≥ 个数」：关窗那几条要的正是「少了一个」，
    ≥ 判断会让它在关掉之前就返回（判据变成假通过）。
    """
    截止 = time.time() + 秒
    最后 = {}
    while time.time() < 截止:
        最后 = _本进程的窗口(u, pid, 前缀)
        if len(最后) == 个数:
            return 最后
        time.sleep(0.3)
    return 最后


def _菜单动作id(u, hwnd: int) -> int:
    """这个窗口菜单栏里第一项（「文件」）的第一个子项 id。"""
    栏 = u.GetMenu(hwnd)
    assert 栏, "窗口上没挂菜单栏"
    子 = u.GetSubMenu(栏, 0)
    assert 子, "没有「文件」子菜单"
    return u.GetMenuItemID(子, 0)


def _点菜单等标题(u, hwnd: int, 想要: str, 次=3, 每次=8.0) -> bool:
    """点这个窗口的第一项菜单，等标题变成 `想要`。返回成没成。

    🔴 **为什么要重试**（M178 实测）：页面刚登记好处理函数的那一两秒内，
    `PostMessage(WM_COMMAND)` 偶尔会**投递不到** muda 的子类过程 ——
    现象是「点了没反应」，而 `JISHI_WINDOW_DEBUG` 里连「菜单点了」都没有
    （说明消息压根没进 muda，不是我们派发错了）。反复投递就好，
    所以判据重试三次；真实用户点第二次也一样。

    ⚠️ 这不是「等久一点」能解决的：失败那几次等了 15 秒也没动静，
    只有**重发**才过。所以三轮各等 8 秒，而不是一轮等 24 秒。
    """
    for _ in range(次):
        u.PostMessageW(hwnd, 0x0111, _菜单动作id(u, hwnd), 0)
        截止 = time.time() + 每次
        while time.time() < 截止:
            if _标题(u, hwnd) == 想要:
                return True
            time.sleep(0.2)
    return False


def _标题(u, hwnd: int) -> str:
    import ctypes

    缓 = ctypes.create_unicode_buffer(512)
    u.GetWindowTextW(hwnd, 缓, 512)
    return 缓.value


class _跑着的程序:
    """起一个 jishi 进程，管好日志与收尾（失败时也要杀掉，别留一堆窗口）。"""

    def __init__(self, 源, tmp_path, 环境=None):
        import subprocess

        (tmp_path / "程序.jsh").write_text(源, encoding="utf-8", newline="\n")
        self.日志 = tmp_path / "跑.log"
        env = {**os.environ, "JISHI_WINDOW_DEBUG": "1"}
        if 环境:
            env.update(环境)
        # ⚠️ **不能 `capture_output=True`**：WebView2 的子进程继承管道，
        #    主进程退出后 `communicate()` 还在等 EOF ⇒ 判据挂满超时（M176 的教训）。
        self._f = open(self.日志, "w", encoding="utf-8")
        self.进程 = subprocess.Popen(
            [str(rust_exe_path()), "程序.jsh"], cwd=str(tmp_path),
            stdout=self._f, stderr=subprocess.STDOUT, env=env)

    def 文(self) -> str:
        """到现在为止的输出。**不关文件** —— 关了之后 `flush` 会抛
        `ValueError: I/O operation on closed file`（判据里踩过）。"""
        if not self._f.closed:
            self._f.flush()
        return self.日志.read_text(encoding="utf-8", errors="replace")

    def 收(self, 秒=20):
        """等进程退出（超时就杀掉），返回退出码。**只 flush、不 close**。"""
        try:
            self.进程.wait(timeout=秒)
        except Exception:
            self.进程.kill()
            self.进程.wait(timeout=30)
        if not self._f.closed:
            self._f.flush()
        return self.进程.returncode

    def 关(self):
        """最终收尾（`finally` 里调）：杀掉进程 + 关掉日志文件。"""
        if self.进程.poll() is None:
            try:
                self.进程.kill()
                self.进程.wait(timeout=30)
            except Exception:
                pass
        if not self._f.closed:
            self._f.close()


@需要原生
def test_三个窗口三个句柄_标题互不干扰(tmp_path):
    """开三个窗口 → 三个句柄不同 → 分别改标题 → **系统里问回来各不相同**。

    为什么不用「日志里说改了」当证据：M174 起就有一条口径「问系统要标题」
    （`GetWindowTextW`）。这里沿用 —— 三个窗口各改各的，系统的回答必须一一对应。
    """
    exe = rust_exe_path()
    if not exe.is_file():
        pytest.skip("没有 jishi-rs（先 cargo build --release）")
    (tmp_path / "常驻.html").write_text(常驻页, encoding="utf-8", newline="\n")

    名 = f"M178 三窗 {os.getpid()}"
    程序 = _跑着的程序(f'''导入 窗口
令 甲 = 窗口.打开({{"标题": "{名}甲", "页面": "常驻.html", "原生": 真}})
令 乙 = 窗口.打开({{"标题": "{名}乙", "页面": "常驻.html", "原生": 真}})
令 丙 = 窗口.打开({{"标题": "{名}丙", "页面": "常驻.html", "原生": 真}})
打印("句柄", 甲, 乙, 丙)
打印("改", 窗口.设标题("{名}一", 甲), 窗口.设标题("{名}二", 乙), 窗口.设标题("{名}三", 丙))
窗口.等()
''', tmp_path)
    try:
        u = _win32()
        窗们 = _等窗口数(u, 程序.进程.pid, 名, 3)
        assert len(窗们) == 3, f"应当有 3 个窗口，实际：{窗们}"
        # 三个标题 = 甲/乙/丙 各改各的（`设标题` 带句柄才做得到）
        等它 = time.time() + 20
        while time.time() < 等它 and set(窗们) != {f"{名}一", f"{名}二", f"{名}三"}:
            time.sleep(0.3)
            窗们 = _本进程的窗口(u, 程序.进程.pid, 名)
        assert set(窗们) == {f"{名}一", f"{名}二", f"{名}三"}, (
            f"三个窗口的标题不对：{set(窗们)}（应当是 一/二/三 各自被改过）")
        assert "句柄 1 2 3" in 程序.文(), 程序.文()[:400]
        assert "改 真 真 真" in 程序.文(), 程序.文()[:400]

        # 收尾：全部关闭 → 进程真的退出（`等` 返回）
        脚本收 = tmp_path / "收.jsh"
        for hwnd in 窗们.values():
            u.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE = 点 ×
        assert 程序.收(秒=30) == 0, "全部关掉之后进程应当正常退出（等 要返回）"
    finally:
        程序.关()


@需要原生
def test_点A的菜单只到A的页面(tmp_path):
    """**这一站最值钱的一条**：三个窗口各一套菜单，点 A 的菜单只有 A 的页面跑。

    菜单点击的整条链是
    `WM_COMMAND` → muda 子类 → `MenuEvent`（**它自己的线程**）→ 事件循环 →
    按动作里的**窗口号**派发 → `window.__jishiWin` → wasm。

    只要「动作表」里少了窗口号这一维，就会退化成「点 A 的菜单，B/C 也改标题」
    或者「只有第一个窗口收到」—— 而且看起来像随机。
    """
    exe = rust_exe_path()
    if not exe.is_file():
        pytest.skip("没有 jishi-rs（先 cargo build --release）")
    if not WASM.is_file():
        pytest.skip("还没构建 wasm（tools/build_wasm.py）")
    (tmp_path / "菜单页.html").write_text(菜单页, encoding="utf-8", newline="\n")

    名 = f"M178 菜单 {os.getpid()}"
    程序 = _跑着的程序(f'''导入 窗口

函数 菜单(动作)：
    返回 [{{"标签": "文件", "子项": [{{"标签": 动作, "动作": 动作}}]}}]

令 甲 = 窗口.打开({{"标题": "{名}甲", "页面": "菜单页.html", "原生": 真}})
令 乙 = 窗口.打开({{"标题": "{名}乙", "页面": "菜单页.html", "原生": 真}})
令 丙 = 窗口.打开({{"标题": "{名}丙", "页面": "菜单页.html", "原生": 真}})
打印("句柄", 甲, 乙, 丙)
窗口.设菜单(菜单("甲动作"), 甲)
窗口.设菜单(菜单("乙动作"), 乙)
窗口.设菜单(菜单("丙动作"), 丙)
窗口.等()
''', tmp_path)
    try:
        u = _win32()
        窗们 = _等窗口数(u, 程序.进程.pid, 名, 3)
        assert len(窗们) == 3, f"应当有 3 个窗口，实际：{窗们}"
        # 等三个页面的 wasm 引擎都起来并登记好处理函数
        环境等 = time.time() + 40
        while time.time() < 环境等:
            if 程序.文().count("wasm 引擎起来了") >= 3:
                break
            time.sleep(0.5)
        assert 程序.文().count("wasm 引擎起来了") >= 3, f"引擎没全起来：{程序.文()[:900]}"
        # 页面「报告就绪」到**稳定处理菜单**之间有个几百毫秒的窗口（见 `_点菜单等标题`），
        # 先给它落一下地再点，能少一轮重试。
        time.sleep(1.5)

        甲 = 窗们[f"{名}甲"]
        乙 = 窗们[f"{名}乙"]
        丙 = 窗们[f"{名}丙"]

        # 点「甲」的菜单
        assert _点菜单等标题(u, 甲, "菜单→甲动作"), f"甲的页面没跑起来：{_标题(u, 甲)}"
        # 🔴 另外两个**一个字都不能变**
        assert _标题(u, 乙) == f"{名}乙", f"乙被串到了：{_标题(u, 乙)}"
        assert _标题(u, 丙) == f"{名}丙", f"丙被串到了：{_标题(u, 丙)}"

        # 再点「丙」的菜单（顺序换一下，避免「只有第一个窗口能收到」的假通过）
        assert _点菜单等标题(u, 丙, "菜单→丙动作"), f"丙的页面没跑起来：{_标题(u, 丙)}"
        assert _标题(u, 甲) == "菜单→甲动作", "甲被丙的菜单串到了"
        assert _标题(u, 乙) == f"{名}乙", "乙被丙的菜单串到了"

        for hwnd in (甲, 乙, 丙):
            u.PostMessageW(hwnd, 0x0010, 0, 0)
        assert 程序.收(秒=30) == 0, "全部关掉之后进程应当正常退出"
    finally:
        程序.关()


@需要原生
def test_关中间那个_另外两个照样收事件(tmp_path):
    """关掉中间那个窗口 → 另外两个**还在**，而且**还能收菜单事件**。

    「关掉一个不影响别的」有真假两种：① 另外两个窗口还在（`IsWindow` 真）；
    ② 另外两个的事件循环还活着（点菜单页面真的跑了）。只验 ① 是不够的 ——
    M178 的第一版如果关窗时把事件循环也退了，窗口会留着但**全都不响应**。
    """
    exe = rust_exe_path()
    if not exe.is_file():
        pytest.skip("没有 jishi-rs（先 cargo build --release）")
    if not WASM.is_file():
        pytest.skip("还没构建 wasm（tools/build_wasm.py）")
    (tmp_path / "菜单页.html").write_text(菜单页, encoding="utf-8", newline="\n")

    名 = f"M178 关中 {os.getpid()}"
    程序 = _跑着的程序(f'''导入 窗口

函数 菜单(动作)：
    返回 [{{"标签": "文件", "子项": [{{"标签": 动作, "动作": 动作}}]}}]

令 甲 = 窗口.打开({{"标题": "{名}甲", "页面": "菜单页.html", "原生": 真}})
令 乙 = 窗口.打开({{"标题": "{名}乙", "页面": "菜单页.html", "原生": 真}})
令 丙 = 窗口.打开({{"标题": "{名}丙", "页面": "菜单页.html", "原生": 真}})
窗口.设菜单(菜单("甲动作"), 甲)
窗口.设菜单(菜单("乙动作"), 乙)
窗口.设菜单(菜单("丙动作"), 丙)
打印("关乙", 窗口.关闭(乙))
窗口.等()
打印("等返回")
''', tmp_path)
    try:
        u = _win32()
        窗们 = _等窗口数(u, 程序.进程.pid, 名, 3)
        assert len(窗们) == 3, f"应当有 3 个窗口，实际：{窗们}"
        截止 = time.time() + 40
        while time.time() < 截止:
            if 程序.文().count("wasm 引擎起来了") >= 3:
                break
            time.sleep(0.5)

        乙 = 窗们[f"{名}乙"]
        # 等到「关乙」真的生效（脚本一进事件循环就会处理它）
        截止 = time.time() + 25
        while time.time() < 截止 and u.IsWindow(乙):
            time.sleep(0.3)
        assert not u.IsWindow(乙), "脚本调 窗口.关闭(乙) 之后，乙的窗口还在"
        剩 = _等窗口数(u, 程序.进程.pid, 名, 2, 秒=15)
        assert set(剩) == {f"{名}甲", f"{名}丙"}, f"关掉乙之后应当是甲和丙：{set(剩)}"

        # 🔴 关键：另外两个**还能收菜单事件**（循环没退）
        甲 = 剩[f"{名}甲"]
        丙 = 剩[f"{名}丙"]
        assert _点菜单等标题(u, 甲, "菜单→甲动作"), (
            "关掉乙之后甲就不响应了（事件循环退了？）")
        assert _标题(u, 丙) == f"{名}丙", "丙被串到了"

        for hwnd in (甲, 丙):
            u.PostMessageW(hwnd, 0x0010, 0, 0)
        assert 程序.收(秒=40) == 0, "甲丙都关掉之后进程应当退出（等 要返回）"
        assert "等返回" in 程序.文(), f"`等` 没返回：{程序.文()[-800:]}"
    finally:
        程序.关()


@需要原生
def test_页面里的关闭只关自己那个窗口(tmp_path):
    """三个窗口共用同一份**自杀页**：只有一个先关，其它两个不动。

    这是「页面 → 宿主：关我自己」那条路的验证。页面（wasm）不知道自己在几号
    窗口里，宿主靠「IPC 来自哪个 WebView」认定 —— 认定错了就是「点页面上的关闭，
    关掉的是另一个窗口」，用户会以为程序乱了。
    """
    exe = rust_exe_path()
    if not exe.is_file():
        pytest.skip("没有 jishi-rs（先 cargo build --release）")
    (tmp_path / "常驻.html").write_text(常驻页, encoding="utf-8", newline="\n")
    (tmp_path / "自杀.html").write_text(_自杀页(1200), encoding="utf-8", newline="\n")

    名 = f"M178 自杀 {os.getpid()}"
    程序 = _跑着的程序(f'''导入 窗口
令 甲 = 窗口.打开({{"标题": "{名}甲", "页面": "常驻.html", "原生": 真}})
令 乙 = 窗口.打开({{"标题": "{名}乙", "页面": "自杀.html", "原生": 真}})
令 丙 = 窗口.打开({{"标题": "{名}丙", "页面": "常驻.html", "原生": 真}})
窗口.等()
''', tmp_path)
    try:
        u = _win32()
        窗们 = _等窗口数(u, 程序.进程.pid, 名, 3)
        assert len(窗们) == 3, f"应当有 3 个窗口，实际：{窗们}"
        乙 = 窗们[f"{名}乙"]

        截止 = time.time() + 30
        while time.time() < 截止 and u.IsWindow(乙):
            time.sleep(0.3)
        assert not u.IsWindow(乙), "乙的页面里发了「关闭」，但乙没关掉"
        剩 = _等窗口数(u, 程序.进程.pid, 名, 2, 秒=15)
        assert set(剩) == {f"{名}甲", f"{名}丙"}, (
            f"页面里的关闭只该关自己，实际剩：{set(剩)}")
        assert 剩[f"{名}甲"] and 剩[f"{名}丙"]

        for hwnd in 剩.values():
            u.PostMessageW(hwnd, 0x0010, 0, 0)
        assert 程序.收(秒=30) == 0
    finally:
        程序.关()


@需要原生
def test_等要等所有窗口(tmp_path):
    """`等(甲)` 在甲关了之后**不返回**，得等最后一个窗口也关掉。

    🔴 这是 M178 **唯一一处已知的不完美**（一个进程只有一个事件循环）。
    判据把它钉住，是为了两件事：
    ① 谁哪天改成「等那个句柄」时，这条会红 —— 那会让其它窗口假死；
    ② 文档里写的「等 = 等所有窗口」有机器证据，而不是口头约定。
    """
    exe = rust_exe_path()
    if not exe.is_file():
        pytest.skip("没有 jishi-rs（先 cargo build --release）")
    (tmp_path / "早.html").write_text(_自杀页(600), encoding="utf-8", newline="\n")
    (tmp_path / "晚.html").write_text(_自杀页(2600), encoding="utf-8", newline="\n")

    起 = time.time()
    程序 = _跑着的程序('''导入 窗口
令 早 = 窗口.打开({"标题": "M178 等-早", "页面": "早.html", "原生": 真})
令 晚 = 窗口.打开({"标题": "M178 等-晚", "页面": "晚.html", "原生": 真})
打印("开始等")
窗口.等(早)
打印("等返回")
''', tmp_path)
    try:
        码 = 程序.收(秒=60)
        用时 = time.time() - 起
        assert 码 == 0, f"退出码 {码}：{程序.文()[-600:]}"
        assert "等返回" in 程序.文(), "等 没返回"
        # 早窗口 0.6 秒就关了；等 必须撑到晚窗口（2.6 秒）之后
        assert 用时 >= 2.0, f"等 只用了 {用时:.1f} 秒就返回了 —— 它等的是「那个句柄」，不是所有窗口"
    finally:
        程序.关()


@需要原生
def test_单窗口的老写法一行不改(tmp_path):
    """老脚本（`设标题("新标题")` 不带句柄、`等(窗)` 带句柄）照样跑。

    多窗口**不许**让单窗口的写法变味：不写句柄 = 主窗口，行为跟 M174/M175 一样。
    这条是「向后兼容」的机器证据（`test_m173` / `m174` / `m175` 另有各自的角度）。
    """
    exe = rust_exe_path()
    if not exe.is_file():
        pytest.skip("没有 jishi-rs（先 cargo build --release）")
    # 页面 6 秒后自己关自己 —— 给判据留出「看标题」的时间窗。
    # （一开始写的是「打开后立刻 全部关闭」，结果窗口常常在枚举之前就没了。）
    (tmp_path / "晚关.html").write_text(_自杀页(6000), encoding="utf-8", newline="\n")

    名 = f"M178 老写法 {os.getpid()}"
    程序 = _跑着的程序(f'''导入 窗口
令 窗 = 窗口.打开({{"标题": "{名}", "页面": "晚关.html", "原生": 真}})
打印("设置", 窗口.设标题("老写法改的"))
打印("在运行", 窗口.在运行(窗))
窗口.等(窗)
打印("等返回")
''', tmp_path)
    try:
        u = _win32()
        # 前缀给空串：这条的标题一开始就被 `设标题("老写法改的")` 换掉了，
        # 没有稳定的前缀可用 ⇒ 只能「有变化就认」，不能用「正好几个窗口」判
        #（进程里还有别的可见窗口，数不准）。
        窗们: dict = {}
        截止 = time.time() + 20
        while time.time() < 截止 and "老写法改的" not in 窗们:
            窗们 = _本进程的窗口(u, 程序.进程.pid, "")
            if "老写法改的" in 窗们:
                break
            time.sleep(0.3)
        assert "老写法改的" in 窗们, f"不带句柄的设标题没生效：{set(窗们)}"

        assert 程序.收(秒=40) == 0, f"退出码非 0：{程序.文()[-600:]}"
        文 = 程序.文()
        assert "设置 真" in 文 and "在运行 真" in 文 and "等返回" in 文, 文[-600:]
    finally:
        程序.关()
