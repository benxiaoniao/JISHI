# -*- coding: utf-8 -*-
"""M175：桌面集成 —— 菜单 / 托盘 / 右键菜单 / 拖放 / 单实例 / 文件对话框。

## 这一站补的是「窗口是自己的之后，才谈得上的那些东西」

M174 把外壳换成了真窗口（wry）。M175 接着把**桌面程序该有的**四件事补上：

| 能力 | 脚本侧 | 页面侧 |
|---|---|---|
| 菜单栏 / 右键菜单 / 托盘 | `窗口.设菜单` / `设右键菜单` / `设托盘` / `关托盘` | `窗口.当("菜单", 函数)` 接点击 |
| 拖文件进窗口 | —（wry 直接接住） | `窗口.当("拖放", 函数)` |
| 单实例 | `窗口.单实例(名)` | `窗口.当("再次启动", 函数)` |
| 文件对话框 | `窗口.选文件` / `存文件` | —（页面里用 `<input type=file>`） |

## 判据怎么做到「不靠人点菜单」

`win32` 那两条用 **ctypes 发 `WM_COMMAND`**：这是 Explorer/用户点菜单时
系统真正发的那条消息，走的是 muda 的窗口子类 → `MenuEvent` → 事件循环 →
`派发` → 页面 `window.__jishiWin` → wasm 里的基石函数。**绕过去测等于没测**。

## 没被自动判据覆盖的

- **真实拖放**（用鼠标把文件拖进来）：wry 在 Windows 上实现的是 OLE 的
  `IDropTarget`，ctypes 造一个真的数据对象不现实。这里只钉「接线在」+
  「页面侧收到路径后怎么走」（后者与菜单是同一条 `派发`）。
- **文件对话框**：它是模态的，弹出来就等人点。只钉参数校验与「没有能力就报错」。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import rust_exe_path  # noqa: E402
from test_m174_native_window import _跑, _在wasm里跑  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WASM = ROOT / "site" / "jishi_wasm.wasm"

无界面 = bool(os.environ.get("JISHI_NO_GUI"))
需要原生 = pytest.mark.skipif(
    无界面 or not sys.platform.startswith("win"),
    reason="真开原生窗口要桌面环境（CI 设 JISHI_NO_GUI=1）",
)

#: M175 加的八个函数。**顺序按语言卡**（模块里的声明顺序）。
新函数 = ["设菜单", "设右键菜单", "设托盘", "关托盘", "单实例", "选文件", "存文件", "当"]


# ---------------------------------------------------------------------------
# 一、结构：三宿主都有、依赖是可选特性
# ---------------------------------------------------------------------------

def test_八个新函数三宿主都在():
    """三个宿主的函数表都要有这八个。

    少一个的后果不是「报错」，而是**静默的「没有这个函数」**——
    Python/Node 侧按模块属性找，找不到就说「没有找到标准库函数」，
    而那和「这个宿主不支持」不是一回事。
    """
    import importlib

    py = importlib.import_module("oracle.jishi.stdlib.窗口")
    有 = set(getattr(py, "__all__", []))
    for 名 in 新函数:
        assert 名 in 有, f"Python 侧缺「窗口.{名}」"
        assert callable(getattr(py, 名)), f"Python 侧「窗口.{名}」不是函数"

    js = (ROOT / "oracle" / "node" / "vm.js").read_text(encoding="utf-8")
    for 名 in 新函数:
        assert f"'{名}': new R.Builtin('{名}'" in js, f"Node 侧缺「窗口.{名}」"

    from oracle.jishi.ai import build_lang_spec

    卡 = build_lang_spec()
    窗口 = next(m for m in 卡["stdlib"] if m["module"] == "窗口")
    名字 = [f["name"] for f in 窗口["functions"]]
    for 名 in 新函数:
        assert 名 in 名字, f"Rust 侧（语言卡）缺「窗口.{名}」"
    assert len(名字) == 14, f"窗口模块应当是 14 个函数，实际 {len(名字)}：{名字}"


def test_新依赖是可选特性wasm躲得开():
    """`muda` / `tray-icon` / `rfd` / `png` 必须 optional 且挂在 native-window 下。

    这条是防「以后有人把 GUI 依赖弄成硬依赖」—— 那会让 **wasm 直接编不过**
    （`wasm/Cargo.toml` 用的是 `default-features = false`，只能躲开可选特性）。
    """
    cargo = (ROOT / "rust" / "Cargo.toml").read_text(encoding="utf-8")
    for 名 in ("muda", "tray-icon", "rfd", "png"):
        片段 = [ln for ln in cargo.splitlines() if ln.startswith(f"{名} =")]
        assert 片段, f"Cargo.toml 里没有 `{名}`"
        断言 = 片段[0]
        assert "optional = true" in 断言, f"`{名}` 不是可选依赖：{断言}"
        assert f'"dep:{名}"' not in cargo.split("native-window = ")[0].split("default = ")[1]

    assert '"dep:muda"' in cargo and '"dep:tray-icon"' in cargo, "native-window 特性没带上菜单/托盘"
    assert '"dep:rfd"' in cargo and '"dep:png"' in cargo, "native-window 特性没带上对话框/图标解码"

    wasm = (ROOT / "wasm" / "Cargo.toml").read_text(encoding="utf-8")
    assert "default-features = false" in wasm, "wasm 必须关掉默认特性，否则会去编 sqlite/GUI"


def test_rfd不许拉ashpd():
    """`rfd` 必须关掉默认特性，Linux 走 `gtk3`。

    默认的 `xdg-portal` 会拉 `ashpd` + `zbus`（一整套异步 D-Bus，几十个 crate）。
    而 GTK3 本来就在（webkit2gtk 依赖它）—— 不这么写等于白背一整个 D-Bus 栈。
    """
    cargo = (ROOT / "rust" / "Cargo.toml").read_text(encoding="utf-8")
    行们 = [ln for ln in cargo.splitlines() if ln.startswith("rfd =")]
    assert 行们, "Cargo.toml 里没有 rfd"
    assert any("default-features = false" in ln for ln in 行们), f"rfd 没关默认特性：{行们}"
    assert "gtk3" in cargo, "Linux 上 rfd 应当走 gtk3 而不是 xdg-portal"


def test_拖放接线在源码里():
    """拖放靠 wry 的 handler；这里钉住「接线还在、事件名对得上」。

    ⚠️ 这条是**源码级**的，很弱 —— 真实的鼠标拖放没有被自动判据覆盖
    （wry 在 Windows 上走 OLE 的 IDropTarget，造不出真的数据对象）。
    页面侧收到路径之后怎么走，由 `test_窗口当能在页面里收到宿主事件` 覆盖。
    """
    rs = (ROOT / "rust" / "src" / "native_window.rs").read_text(encoding="utf-8")
    assert ".with_drag_drop_handler(" in rs, "打开窗口时没接拖放"
    assert '事件::页面("拖放"' in rs, "拖放进来的路径没有转成页面事件"
    # 多个文件用换行分隔：wasm 那条通道只能传一个字符串
    assert 'join("\\n")' in rs, "多个拖进来的文件应当用换行分隔"


# ---------------------------------------------------------------------------
# 二、没有能力就要说（不许静默返回假）
# ---------------------------------------------------------------------------

_做不到 = "没有原生窗口能力"


def test_Python宿主八个调用全都明确报错(tmp_path):
    """Python 宿主只有应用模式，这八个一个都做不到 —— 必须报错。

    🔴 为什么不许「返回假 / 什么都不做」：用户会以为设上了，跑起来才发现
    右键没反应 —— 与 M174 那条「白窗口、零报错」是同一类坑。
    """
    # 每个函数单独 尝试/捕获：一条报错不该掩盖后面那些（要一次看全八个）
    源 = "导入 窗口\n"
    for 名 in 新函数:
        参数 = "{}" if 名 in ("设菜单", "设右键菜单", "设托盘") else (
            '"甲", "乙"' if 名 == "当" else ('"x"' if 名 == "单实例" else ""))
        源 += (f"尝试：\n    窗口.{名}({参数})\n捕获 异常 为 e：\n"
               f'    打印("{名}: " + 文本(e))\n')
    (tmp_path / "程序.jsh").write_text(源, encoding="utf-8", newline="\n")
    码, 出 = _跑([sys.executable, "-m", "oracle.jishi.cli", "程序.jsh"], cwd=tmp_path)
    assert 码 == 0, 出[:800]
    for 名 in 新函数:
        assert f"{名}: " in 出, f"「窗口.{名}」在 Python 宿主上没报错（静默了）：{出[:900]}"
    assert 出.count(_做不到) == len(新函数), f"报错文案不一致：{出[:900]}"


def test_Node宿主八个调用全都明确报错(tmp_path):
    """Node 宿主同理。三宿主的报错口径要一样（都是「没有原生窗口能力」）。"""
    源 = "导入 窗口\n"
    for 名 in 新函数:
        参数 = "{}" if 名 in ("设菜单", "设右键菜单", "设托盘") else (
            '"甲", "乙"' if 名 == "当" else ('"x"' if 名 == "单实例" else ""))
        源 += (f"尝试：\n    窗口.{名}({参数})\n捕获 异常 为 e：\n"
               f'    打印("{名}: " + 文本(e))\n')
    (tmp_path / "程序.jsh").write_text(源, encoding="utf-8", newline="\n")
    # ⚠️ Node 宿主的入口收的是**字节码**（`jishi --dump-bytecode` 的产物），
    #    直接给它 .jsh 会去 `JSON.parse` 源码然后报「不是合法 JSON」。
    字节 = tmp_path / "程序.bc.json"
    r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", "--dump-bytecode", "程序.jsh"],
                       cwd=str(tmp_path), capture_output=True)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    字节.write_bytes(r.stdout)
    码, 出 = _跑(["node", str(ROOT / "oracle" / "node" / "index.js"), 字节], cwd=tmp_path)
    assert 码 == 0, 出[:800]
    for 名 in 新函数:
        assert f"{名}: " in 出, f"「窗口.{名}」在 Node 宿主上没报错：{出[:900]}"
    assert 出.count(_做不到) == len(新函数), f"报错文案不一致：{出[:900]}"


# ---------------------------------------------------------------------------
# 三、页面侧：`窗口.当` 登记与派发（Node + 真 wasm + 假 DOM）
# ---------------------------------------------------------------------------

def test_窗口当能在页面里收到宿主事件(tmp_path):
    """`窗口.当` 登记后，宿主叫 `window.__jishiWin(事件, 参数)` 就调到那个函数。

    这条覆盖的是**整条派发链的页面那头**（菜单/托盘/拖放/再次启动共用它）：
    `__jishiWin` → shim 查表 → `call(函数名, 参数)` → wasm 里跑函数。
    """
    源 = (
        "导入 窗口\n"
        "函数 接菜单(动作)：\n"
        "    窗口.设标题(\"菜单→\" + 动作)\n"
        "窗口.当(\"菜单\", 接菜单)\n"
    )
    结果 = _在wasm里跑(源, tmp_path)
    assert 结果["ok"], 结果["错误"]

    # 页面要主动告诉宿主「我可以接事件了」—— 否则「第二个实例来了」这种
    # 发生在页面加载之前的事件就只能排队等一个永远不会来的冲刷。
    消息 = [json.loads(m) for m in 结果["ipc"]]
    assert {"op": "宿主事件就绪"} in 消息, f"页面没上报就绪：{消息}"

    # 再叫一次（模拟宿主派发）
    (tmp_path / "源2.jsh").write_text(源, encoding="utf-8", newline="\n")
    (tmp_path / "派.js").write_text(_派发探针, encoding="utf-8", newline="\n")
    if not WASM.is_file():
        pytest.skip("还没构建 wasm（跑 tools/build_wasm.py）")
    码, 出 = _跑(["node", tmp_path / "派.js", ROOT / "wasm" / "jishi-wasm.js", WASM,
                  tmp_path / "源2.jsh"], timeout=180)
    assert 码 == 0, 出[:800]
    行 = [ln for ln in 出.splitlines() if ln.strip().startswith("{")]
    assert 行, f"探针没有输出 JSON：{出[:600]}"
    出 = json.loads(行[-1])
    assert 出["有__jishiWin"] is True, "登记之后页面里没有 __jishiWin"
    assert 出["页面标题"] == "菜单→新建", f"派发没调到函数：{出['页面标题']}"
    assert 出["没登记的"] is None, "没登记的事件不该被当成有处理函数"


_派发探针 = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const 假页 = { title: "", addEventListener() {}, querySelector: () => null };
(async () => {
  globalThis.ipc = { postMessage: () => {} };
  const j = await JishiWasm.load(fs.readFileSync(process.argv[3]), { doc: 假页 });
  j.run(fs.readFileSync(process.argv[4], "utf8"));
  const 有 = typeof globalThis.__jishiWin === "function";
  if (有) {
    globalThis.__jishiWin("菜单", "新建");
    // 没登记的事件：调用应当什么都不做（不抛、不改标题）
    globalThis.__jishiWin("没登记的", "x");
  }
  console.log(JSON.stringify({ 有__jishiWin: 有, 页面标题: 假页.title, 没登记的: 有 ? 假页.title === "菜单→新建" ? null : "改了" : null }));
})();
'''


def test_页面里设菜单要报错(tmp_path):
    """页面（wasm）里挂菜单没有意义 —— 要明确报错，而不是静默返回假。"""
    结果 = _在wasm里跑('导入 窗口\n窗口.设菜单([])\n', tmp_path)
    assert not 结果["ok"], "页面里 `窗口.设菜单` 应当报错"
    assert "页面里不能用" in 结果["错误"], 结果["错误"]


def test_当只认四个事件(tmp_path):
    """`窗口.当` 的事件名有限：写错了要报错并列出能用的四个。

    否则用户写 `窗口.当("菜单点击", f)` 会静默成功、永远不触发。
    """
    结果 = _在wasm里跑(
        '导入 窗口\n函数 处理菜单(动作)：\n    打印(动作)\n窗口.当("菜单点击", 处理菜单)\n',
        tmp_path)
    assert not 结果["ok"], "不认识的事件名应当报错"
    assert "没有「菜单点击」这个宿主事件" in 结果["错误"], 结果["错误"]
    # 提示里要列出能用的四个（探针只回标题+正文，提示在 error.hint 里）
    rs = (ROOT / "rust" / "src" / "stdlib" / "window.rs").read_text(encoding="utf-8")
    assert "能接的是：菜单 / 拖放 / 托盘 / 再次启动" in rs, "报错提示没列出可用的事件名"


def test_当要具名函数(tmp_path):
    结果 = _在wasm里跑('导入 窗口\n窗口.当("菜单", 1)\n', tmp_path)
    assert not 结果["ok"], "传数字应当报错"
    assert "具名函数" in 结果["错误"], 结果["错误"]


# ---------------------------------------------------------------------------
# 四、真机：菜单点击（Windows + 桌面）
# ---------------------------------------------------------------------------

_M175页面 = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>M175</title></head><body>
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
    const 源码 = [
      "导入 窗口",
      "函数 接菜单(动作)：",
      "    窗口.设标题(\\"菜单→\\" + 动作)",
      "窗口.当(\\"菜单\\", 接菜单)",
      "窗口.当(\\"再次启动\\", 接菜单)",
    ].join("\\n");
    const r = j.run(源码);
    报(r.ok ? "引擎就绪" : "引擎失败", r.ok ? "" : JSON.stringify(r.error));
  } catch (e) {
    报("引擎失败", String((e && e.message) || e));
  }
})();
</script></body></html>
"""

_M175脚本 = """导入 窗口
函数 跑()：
    令 窗 = 窗口.打开({"标题": "%(标题)s", "页面": "页.html"})
    窗口.设菜单([{"标签": "文件", "子项": [
        {"标签": "新建", "动作": "新建"},
        {"类型": "分隔"},
        {"标签": "退出", "动作": "退出"}]}])
    窗口.设右键菜单([{"标签": "复制", "动作": "复制"}])
    打印("在等窗口")
    窗口.等(窗)

如果 非 窗口.单实例("%(名)s")：
    打印("已经有一个在跑了")
否则：
    跑()
"""


def _win32():
    import ctypes
    import ctypes.wintypes as wt

    u = ctypes.windll.user32
    u.FindWindowW.restype = wt.HWND
    u.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
    u.GetMenu.restype = wt.HMENU
    u.GetMenu.argtypes = [wt.HWND]
    u.GetMenuItemCount.restype = ctypes.c_int
    u.GetMenuItemCount.argtypes = [wt.HMENU]
    u.GetSubMenu.restype = wt.HMENU
    u.GetSubMenu.argtypes = [wt.HMENU, ctypes.c_int]
    u.GetMenuItemID.restype = ctypes.c_uint
    u.GetMenuItemID.argtypes = [wt.HMENU, ctypes.c_int]
    u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    return u


def _等窗口(u, 标题, 秒=30):
    import time
    from ctypes import create_unicode_buffer

    截止 = time.time() + 秒
    while time.time() < 截止:
        h = u.FindWindowW(None, 标题)
        if h:
            return h
        time.sleep(0.2)
    buf = create_unicode_buffer(256)
    return 0


@需要原生
def test_真点菜单能把动作送到页面里的基石函数(tmp_path):
    """端到端：菜单栏 → 点击（真 `WM_COMMAND`）→ 页面基石函数 → 改窗口标题。

    为什么这条必须走系统消息：菜单点击的整条链是
    `WM_COMMAND` → muda 的窗口子类 → `MenuEvent` → 事件循环 → `派发` →
    `window.__jishiWin` → shim `call` → wasm。**任何一环断了，用户看到的就是
    「点了没反应」** —— 而在代码里直接调处理函数，这些环全都不测。
    """
    import time

    exe = rust_exe_path()
    if not exe.is_file():
        pytest.skip("没有 jishi-rs（先 cargo build --release）")
    if not WASM.is_file():
        pytest.skip("还没构建 wasm（tools/build_wasm.py）")

    标题 = f"M175 菜单 {os.getpid()}"
    名 = f"m175-菜单-{os.getpid()}"
    (tmp_path / "页.html").write_text(_M175页面, encoding="utf-8", newline="\n")
    (tmp_path / "程序.jsh").write_text(_M175脚本 % {"标题": 标题, "名": 名},
                                      encoding="utf-8", newline="\n")

    env = {**os.environ, "JISHI_WINDOW_DEBUG": "1"}
    日志 = tmp_path / "run.log"
    with open(日志, "w", encoding="utf-8") as f:
        进程 = subprocess.Popen([str(exe), "程序.jsh"], cwd=str(tmp_path),
                                stdout=f, stderr=subprocess.STDOUT, env=env)
    try:
        u = _win32()
        hwnd = _等窗口(u, 标题)
        assert hwnd, "窗口没起来"
        # 等页面里的 wasm 引擎起来并登记处理函数
        time.sleep(6.0)

        hmenu = u.GetMenu(hwnd)
        assert hmenu, "窗口上没挂菜单栏"
        assert u.GetMenuItemCount(hmenu) == 1, "菜单栏顶层应当有 1 项"
        文件 = u.GetSubMenu(hmenu, 0)
        assert 文件, "没有「文件」子菜单"
        assert u.GetMenuItemCount(文件) == 3, "「文件」里应当是 新建/分隔/退出 三项"
        新建 = u.GetMenuItemID(文件, 0)
        assert 新建, "拿不到「新建」的菜单项 id"

        u.PostMessageW(hwnd, 0x0111, 新建, 0)     # WM_COMMAND
        deadline = time.time() + 10
        import ctypes
        buf = ctypes.create_unicode_buffer(256)
        while time.time() < deadline:
            u.GetWindowTextW(hwnd, buf, 256)
            if buf.value == "菜单→新建":
                break
            time.sleep(0.2)
        assert buf.value == "菜单→新建", f"点了菜单但标题没变（现在是「{buf.value}」）"
    finally:
        if 进程.poll() is None:
            u = _win32()
            h = u.FindWindowW(None, 标题)
            if h:
                u.PostMessageW(h, 0x0010, 0, 0)   # WM_CLOSE
            try:
                进程.wait(timeout=10)
            except subprocess.TimeoutExpired:
                进程.kill()
    日志文 = 日志.read_text(encoding="utf-8", errors="replace")
    assert "菜单点了：新建" in 日志文, 日志文[:1200]
    assert "系统里现在是「菜单→新建」" in 日志文, 日志文[:1200]


_存本地页 = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<script>
(async () => {
  function 报(op, arg) { if (window.ipc && window.ipc.postMessage) window.ipc.postMessage(JSON.stringify({op: op, arg: arg || ""})); }
  try {
    const JishiWasm = (await import("/__jishi/jishi-wasm.js")).default || window.JishiWasm;
    const j = await JishiWasm.load("/__jishi/jishi_wasm.wasm");
    const 源码 = [
      "导入 浏览器",
      "导入 窗口",
      "令 旧 = 浏览器.读本地(\\"计数\\")",
      "如果 旧 == 空：",
      "    窗口.设标题(\\"第一次\\")",
      "否则：",
      "    窗口.设标题(\\"上次读到:\\" + 旧)",
      "浏览器.存本地(\\"计数\\", \\"有值\\")",
    ].join("\\n");
    const r = j.run(源码);
    报(r.ok ? "引擎就绪" : "引擎失败", r.ok ? "" : JSON.stringify(r.error));
  } catch (e) { 报("引擎失败", String((e && e.message) || e)); }
  setTimeout(() => 报("关闭"), 900);
})();
</script></body></html>
"""


@需要原生
def test_存本地跨次还在_同一个程序永远同一个端口(tmp_path):
    """`浏览器.存本地` 写的东西，**下次启动还读得到**。

    🔴 M176 演练抓到的真缺陷：页面的 origin 是 `http://127.0.0.1:<端口>`，
    而浏览器按 **origin** 隔离 localStorage。端口每次随机 ⇒ 每次都是新 origin
    ⇒ 上次存的读不到。用户看到的是「计算器每次启动都回到 0」。

    修法：按页面目录散列出一个固定端口（39001–40989），占了才退回系统分配。
    这条判据开两次窗：第一次写、第二次读，**第二次必须读到**。
    """
    import time

    exe = rust_exe_path()
    if not exe.is_file():
        pytest.skip("没有 jishi-rs（先 cargo build --release）")
    if not WASM.is_file():
        pytest.skip("还没构建 wasm（tools/build_wasm.py）")

    (tmp_path / "页.html").write_text(_存本地页, encoding="utf-8", newline="\n")
    标题 = f"M176 存本地 {os.getpid()}"
    (tmp_path / "程序.jsh").write_text(
        f'导入 窗口\n令 窗 = 窗口.打开({{"标题": "{标题}", "页面": "页.html"}})\n窗口.等(窗)\n',
        encoding="utf-8", newline="\n")

    def 跑一次(序号):
        日志 = tmp_path / f"run{序号}.log"
        with open(日志, "w", encoding="utf-8") as f:
            码 = subprocess.call([str(exe), "程序.jsh"], cwd=str(tmp_path),
                                stdout=f, stderr=subprocess.STDOUT, timeout=180,
                                env={**os.environ, "JISHI_WINDOW_DEBUG": "1"})
        assert 码 == 0, 日志.read_text(encoding="utf-8", errors="replace")[:800]
        return 日志.read_text(encoding="utf-8", errors="replace")

    一 = 跑一次(1)
    assert "第一次" in 一, f"第一次应当是空的：{一[:600]}"
    # 端口必须**两次一样**，那是「同一个 origin」的前提
    端口1 = [ln for ln in 一.splitlines() if "窗口指向" in ln]
    二 = 跑一次(2)
    端口2 = [ln for ln in 二.splitlines() if "窗口指向" in ln]
    assert 端口1 and 端口2, f"没拿到端口日志：{一[:400]} / {二[:400]}"
    址1 = 端口1[0].split("窗口指向")[-1].strip()
    址2 = 端口2[0].split("窗口指向")[-1].strip()
    assert 址1.split("/")[2] == 址2.split("/")[2], f"两次端口不一样：{址1} vs {址2}"
    assert "上次读到:有值" in 二, f"第二次没读到上次存的值（存本地丢了）：{二[:600]}"


@需要原生
def test_第二个实例立刻退出并唤醒第一个(tmp_path):
    """单实例：第二个进程**不开第二个窗口**，把参数交给第一个，自己退出。

    正常用法是在 `窗口.打开` **之前**就问 —— 这一站为此把「页面还没准备好」
    的宿主事件排了队（见 `native_window::待发`），所以第一个实例的页面
    是在**引擎起来之后**才收到「再次启动」的。
    """
    import time

    exe = rust_exe_path()
    if not exe.is_file():
        pytest.skip("没有 jishi-rs（先 cargo build --release）")
    if not WASM.is_file():
        pytest.skip("还没构建 wasm（tools/build_wasm.py）")

    标题 = f"M175 单实例 {os.getpid()}"
    名 = f"m175-单实例-{os.getpid()}"
    (tmp_path / "页.html").write_text(_M175页面, encoding="utf-8", newline="\n")
    (tmp_path / "程序.jsh").write_text(_M175脚本 % {"标题": 标题, "名": 名},
                                      encoding="utf-8", newline="\n")
    env = {**os.environ, "JISHI_WINDOW_DEBUG": "1"}

    日志1 = tmp_path / "one.log"
    with open(日志1, "w", encoding="utf-8") as f1:
        一 = subprocess.Popen([str(exe), "程序.jsh"], cwd=str(tmp_path),
                              stdout=f1, stderr=subprocess.STDOUT, env=env)
    try:
        u = _win32()
        hwnd = _等窗口(u, 标题)
        assert hwnd, "第一个实例的窗口没起来"
        time.sleep(6.0)

        日志2 = tmp_path / "two.log"
        with open(日志2, "w", encoding="utf-8") as f2:
            二 = subprocess.Popen([str(exe), "程序.jsh", "--第二个"], cwd=str(tmp_path),
                                  stdout=f2, stderr=subprocess.STDOUT, env=env)
        try:
            码 = 二.wait(timeout=30)
        except subprocess.TimeoutExpired:
            二.kill()
            pytest.fail("第二个实例没有退出 —— 它可能又开了一个窗口")
        assert 码 == 0, f"第二个实例退出码 {码}：{日志2.read_text(encoding='utf-8', errors='replace')[:600]}"

        出2 = 日志2.read_text(encoding="utf-8", errors="replace")
        assert "已经有一个在跑了" in 出2, f"第二个实例没认出第一个：{出2[:600]}"

        # 第一个实例：页面里的「再次启动」处理函数应当把标题改掉
        import ctypes
        buf = ctypes.create_unicode_buffer(256)
        截止 = time.time() + 12
        while time.time() < 截止:
            u.GetWindowTextW(hwnd, buf, 256)
            if buf.value == "菜单→--第二个":
                break
            time.sleep(0.2)
        assert buf.value == "菜单→--第二个", f"第一个实例没被唤醒（标题「{buf.value}」）"
    finally:
        if 一.poll() is None:
            u = _win32()
            h = u.FindWindowW(None, 标题)
            if h:
                u.PostMessageW(h, 0x0010, 0, 0)
            try:
                一.wait(timeout=10)
            except subprocess.TimeoutExpired:
                一.kill()


def test_菜单定义写错要报错(tmp_path):
    """菜单项缺「动作」/ 定义不是列表 ⇒ 明确报错，并指出是哪一项。

    🔑 **这条不需要开窗口**：菜单定义是在**挂到窗口之前**就解析的，所以
    没有原生窗口时，坏定义报的是定义本身的错（而不是「现在没有原生窗口」）。
    这既是更好的用户体验（先看你写对没有），也让判据不用碰 WebView2 ——
    真开窗的判据在 Windows 上会拉起 WebView2 子进程，跑一整套时容易互相拖累。
    """
    exe = rust_exe_path()
    if not exe.is_file():
        pytest.skip("没有 jishi-rs（先 cargo build --release）")
    源 = (
        "导入 窗口\n"
        "尝试：\n"
        "    窗口.设菜单([{\"标签\": \"没动作\"}])\n"
        "捕获 异常 为 e：\n"
        "    打印(\"A: \" + 文本(e))\n"
        "尝试：\n"
        "    窗口.设菜单({\"标签\": \"不是列表\"})\n"
        "捕获 异常 为 e：\n"
        "    打印(\"B: \" + 文本(e))\n"
        "尝试：\n"
        "    窗口.设托盘([\"不是字典\"])\n"
        "捕获 异常 为 e：\n"
        "    打印(\"C: \" + 文本(e))\n"
    )
    (tmp_path / "程序.jsh").write_text(源, encoding="utf-8", newline="\n")
    码, 出 = _跑([exe, "程序.jsh"], cwd=tmp_path, timeout=120)
    assert 码 == 0, 出[:800]
    assert "A: " in 出 and "缺「动作」" in 出, f"缺动作的菜单项没报清楚：{出[:800]}"
    assert "B: " in 出 and "列表" in 出, f"非列表的菜单定义没报清楚：{出[:800]}"
    assert "C: " in 出 and "要一个字典" in 出, f"托盘选项不是字典时没报清楚：{出[:800]}"


def test_文件对话框的选项校验(tmp_path):
    """`选文件` / `存文件` 是模态的，无法自动点；但**参数校验**要能自动验。

    这条用 Python 宿主跑不了（那边一律报「没有原生窗口能力」），所以走 Rust 宿主；
    真正的对话框不会弹出来 —— 参数在弹之前就被拒了。
    """
    exe = rust_exe_path()
    if not exe.is_file():
        pytest.skip("没有 jishi-rs（先 cargo build --release）")
    源 = (
        "导入 窗口\n"
        "尝试：\n"
        "    窗口.选文件(\"不是字典\")\n"
        "捕获 异常 为 e：\n"
        "    打印(\"A: \" + 文本(e))\n"
        "尝试：\n"
        "    窗口.存文件(\"不是字典\")\n"
        "捕获 异常 为 e：\n"
        "    打印(\"B: \" + 文本(e))\n"
    )
    (tmp_path / "程序.jsh").write_text(源, encoding="utf-8", newline="\n")
    码, 出 = _跑([exe, "程序.jsh"], cwd=tmp_path, timeout=120)
    assert 码 == 0, 出[:800]
    assert "A: " in 出 and "要一个字典" in 出, f"选文件的参数校验没报清楚：{出[:800]}"
    assert "B: " in 出 and "要一个字典" in 出, f"存文件的参数校验没报清楚：{出[:800]}"


def test_单实例的实例文件在临时目录且名字被散列():
    """名字是用户给的任意中文 —— 直接当文件名在三平台都不安全。

    这里钉住「映射是确定的、只含 ASCII」：同一个名字永远同一个文件（否则
    第二个实例就找不到第一个），换个名字就是另一个文件。
    """
    源 = (ROOT / "rust" / "src" / "single_instance.rs").read_text(encoding="utf-8")
    assert "sha256_hex" in 源, "实例文件名应当用名字的散列（不能直接拼中文）"
    assert "create_new(true)" in 源, "抢锁必须用 create_new（exists 再建有竞态）"
    assert "127.0.0.1" in 源 or "Ipv4Addr::LOCALHOST" in 源, "只该监听本机"
