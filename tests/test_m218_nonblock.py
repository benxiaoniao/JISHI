# -*- coding: utf-8 -*-
"""M218 · 非阻塞服务 + 宿主叫页面。

## 这一站补的是什么

磐石 IDE（用基石写的另一个项目）要在一个进程里同时做「HTTP 服务 + 窗口」，
结果撞墙：`HTTP服务端.启动` 是**阻塞**的，一进去窗口消息泵就饿死 ⇒ 窗口白着、
页面永远加载不出来（L-3 / L-10）。同一批需求里还有「宿主没法主动叫页面，
只能 200ms 轮询」（L-2）。

## 三件东西

1. **`HTTP服务端.启动非阻塞(端口, 处理函数)`** → 服务句柄，
   主程序自己循环里调 `服务.处理一轮(最多)`（**事件循环式，不开线程** ——
   单 VM 单线程，处理函数还跑在你的线程上，状态语义不变）。
2. **`窗口.调用页面(句柄, 事件名, 载荷)`**：宿主主动叫页面
   （页面里 `窗口.当(事件名, 函数)` 登记过）。
3. **拖放载荷改 JSON 文本**：以前是「换行分隔的路径串」，现在
   `{"路径们": [...]}` —— 多个文件本来就是一个列表。

## 判据为什么这么写

| # | 判据 | 抓的是 |
|---|---|---|
| ① | 真发一个 HTTP 请求：脚本在**自己的循环**里收，客户端拿到正文 | 「非阻塞」不是摆设 —— 收完就返回，循环还在转 |
| ② | 1000 次空转 `处理一轮` **< 50 ms** | 一轮是**一拍**（`accept` 立刻 `WouldBlock`），不是「等一会」 |
| ③ | 页面侧 `__jishiWin` 真被调到、载荷是 JSON | 宿主→页面这条通道（真 DOM 要窗口，这里验协议） |
| ④ | 拖放 3 个文件 → 页面拿到 3 个路径 | 载荷**结构化**了，不用再自己 split |
| ⑤ | 阻塞 `启动` 仍然照旧 | 老写法不许被这次改动带坏 |
| ⑥ | Python/Node 上 `调用页面` 明确报「这个宿主做不到」 | 能力缺失要**说出来**，不能静默返回假 |

🔴 ①⑤ 在本机要真起 socket（打 `计时`，进串行节）；③④ 是 Node + 假 DOM
（与 `test_m167_browser` 同一套路），跑在并行节。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path  # noqa: E402

RUST_EXE = rust_exe_path()

#: 服务脚本：先把端口写到文件（测试靠它连上来），然后**在自己的循环里**收请求
_服务脚本 = '''导入 HTTP服务端
导入 时间
导入 文件
导入 文本 为 文本库
导入 json

函数 处理(请求)：
    如果 请求["路径"] == "/数据":
        返回 {"正文": json.转文本({"好": 真, "回答": 42}), "内容类型": "application/json"}
    返回 {"状态": 404, "正文": "没有这一页"}

令 服 = HTTP服务端.启动非阻塞(0, 处理)
打印("类型 " + 类型(服))
文件.写文本("__端口文件__", 文本(服.端口()))

# ② 空转一拍：1000 次「没有连接」的 处理一轮 应该在几十毫秒内跑完
令 起空 = 时间.高精度时间()
遍历 i 在 范围(1000):
    服.处理一轮(4)
打印("空转毫秒 " + 文本库.小数位((时间.高精度时间() - 起空) * 1000, 1))

# ① 真收：等客户端来（最多 8 秒），收到 2 个就停
令 起 = 时间.高精度时间()
当 服.请求数() < 2 与 (时间.高精度时间() - 起) < 8:
    服.处理一轮(8)
打印("请求数 " + 文本(服.请求数()))
打印("关闭 " + 文本(服.关闭()))
'''


def _等文件(p: Path, 秒: float) -> bool:
    截止 = time.time() + 秒
    while time.time() < 截止:
        if p.is_file() and p.read_text(encoding="utf-8").strip():
            return True
        time.sleep(0.05)
    return False


def _跑服务(tmp_path: Path, 宿主: str):
    """起服务脚本（不阻塞），等它就绪 → 返回 `(进程, 端口)`。"""
    端口文件 = tmp_path / f"port-{os.getpid()}-{宿主}.txt"
    # 🔴 用**绝对路径**：python 宿主的 cwd 是仓库根，相对名会把文件写到那儿去
    源 = _服务脚本.replace("__端口文件__", str(端口文件).replace("\\", "/"))
    脚本 = tmp_path / f"srv-{os.getpid()}-{宿主}.jsh"
    脚本.write_text(源, encoding="utf-8", newline="\n")
    if 宿主 == "rust":
        命 = [str(RUST_EXE), str(脚本)]
        cwd = str(tmp_path)
    else:
        命 = [sys.executable, "-m", "oracle.jishi.cli", str(脚本)]
        cwd = str(ROOT)
    进 = subprocess.Popen(命, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    assert _等文件(端口文件, 15), "服务没起来（端口文件没出现）"
    端口 = int(端口文件.read_text(encoding="utf-8").strip())
    return 进, 端口


def _取(端口: int, 路径: str) -> str:
    """裸 socket 发一个 GET（不用 urllib：它会在 4xx 上抛异常，看不到正文）。"""
    # 路径要**百分号编码**（HTTP 请求行只认 ASCII）；`解析请求` 那边会解回来
    from urllib.parse import quote

    with socket.create_connection(("127.0.0.1", 端口), timeout=5) as s:
        s.sendall(
            f"GET {quote(路径)} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
            .encode("ascii"))
        buf = b""
        while True:
            块 = s.recv(65536)
            if not 块:
                break
            buf += 块
    return buf.decode("utf-8", "replace")


@pytest.mark.计时
@pytest.mark.parametrize("宿主", ["rust", "python"])
def test_非阻塞服务真收请求(tmp_path, 宿主):
    """① + ②：脚本在自己的循环里收请求，客户端拿到正文；一轮是一拍，不是等一会。"""
    进, 端口 = _跑服务(tmp_path, 宿主)
    try:
        一 = _取(端口, "/数据")
        assert "200 OK" in 一.splitlines()[0], 一[:200]
        assert '{"好": true, "回答": 42}' in 一, 一[:400]
        # 再来一个**走不到的分支**：404 也要回（且不把服务带走）
        二 = _取(端口, "/别的")
        assert "404 Not Found" in 二.splitlines()[0], 二[:200]
        出, _ = 进.communicate(timeout=30)
    finally:
        if 进.poll() is None:
            进.kill()
    行 = {l.split(" ")[0]: l.split(" ", 1)[1] for l in 出.strip().splitlines() if " " in l}
    assert 行.get("类型") == "服务", 出
    # ② 1000 次空转：judgement 要 < 50 ms（本机实测个位数）
    空转 = float(行["空转毫秒"])
    assert 空转 < 50, f"1000 次空转用了 {空转} ms —— 「处理一轮」在等什么？"
    assert 行.get("请求数") == "2", 出
    assert 行.get("关闭") == "真", 出


#: 阻塞版：**必须先知道端口**（它自己不会 print 出端口 —— `启动` 阻塞着）
_阻塞脚本 = """导入 HTTP服务端

函数 处理(请求)：
    返回 "旧写法还在"

令 结果 = HTTP服务端.启动(__端口__, 处理, 最多请求数=1)
打印("请求数 " + 文本(结果["请求数"]))
"""


@pytest.mark.计时
def test_阻塞启动照旧(tmp_path):
    """⑤ 老写法（阻塞 + 最多请求数）不许被这次改动带坏。

    ⚠️ 阻塞版**没法先报端口**（`启动` 一进去就不回来），所以这里先用系统分配
    探一个空闲端口再喂给它 —— 直接写死 8000 之类会跟别人的机器撞。
    """
    import socket as _s

    with _s.socket() as 探:
        探.bind(("127.0.0.1", 0))
        端口 = 探.getsockname()[1]
    脚本 = tmp_path / f"blk-{os.getpid()}.jsh"
    脚本.write_text(_阻塞脚本.replace("__端口__", str(端口)), encoding="utf-8", newline="\n")
    进 = subprocess.Popen([str(RUST_EXE), str(脚本)], cwd=str(tmp_path),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8")
    try:
        # 连上为止（服务可能在端口探测后才 ready）
        截止 = time.time() + 15
        应答 = ""
        while time.time() < 截止:
            try:
                应答 = _取(端口, "/")
                break
            except OSError:
                time.sleep(0.1)
        assert "旧写法还在" in 应答, f"阻塞服务没回：{应答[:200]!r}"
        assert 进.wait(timeout=15) == 0
    finally:
        if 进.poll() is None:
            进.kill()
    assert "请求数 1" in 进.stdout.read()


# ---------------------------------------------------------------------------
# ③ ④ 页面侧：宿主叫页面 + 拖放载荷是 JSON（Node + 假 DOM，与 m167 同一套路）
# ---------------------------------------------------------------------------

_页面JS = r'''
const fs = require("fs");
// 🔴 胶水的 IIFE 是 `(function(root){…})(typeof self !== "undefined" ? self : this)`：
//    Node 里没有 `self`，`this` 是模块的 exports ⇒ `__jishiWin` 会挂到 exports 上。
//    这里先造一个 `self`，让 root 落到 globalThis，判据就能拿到那个入口。
globalThis.self = globalThis;
const JishiWasm = require(process.argv[2]);

// 假 DOM + 假 ipc：只验「宿主 ↔ 页面」这条**协议**，与真 DOM 无关
const els = new Map();
function makeEl() {
  return { textContent: "", value: "", style: {}, classList: {
    add() {}, remove() {}, toggle() {}, has() { return false; } },
    addEventListener() {}, };
}
const doc = { querySelector: (s) => { if (!els.has(s)) els.set(s, makeEl()); return els.get(s); } };
const 发出 = [];
// ⚠️ 胶水自己的 `root` 是 `self`（浏览器里的 window），所以 `ipc` 要挂在 globalThis 上 ——
//    挂在 opts 里那个对象上是**看不见**的（M218 实测：排空数组，白等）。
globalThis.ipc = { postMessage: (t) => 发出.push(JSON.parse(t)) };
const root = { document: doc, location: { href: "http://127.0.0.1/" } };

// ⚠️ 基石**没有函数提升**：两个函数都得写在 `窗口.当` 之前（M217 踩过一次）
const SRC = `导入 窗口
导入 json
导入 浏览器

函数 处理拖放(载荷)：
    令 数 = json.解析(载荷)
    令 路径们 = 数["路径们"]
    浏览器.设文本("#结果", "拖进来 " + 文本(长度(路径们)) + " 个")

函数 处理调用(载荷)：
    令 数 = json.解析(载荷)
    浏览器.设文本("#命令", "收到:" + 数["函数"] + "=" + 数["载荷"])

窗口.当("拖放", 处理拖放)
窗口.当("调用", 处理调用)
`;

(async () => {
  const bytes = fs.readFileSync(process.argv[3]);
  const j = await JishiWasm.load(bytes, { doc, store: null, root });
  // 装的时候把假 window 上的东西带过去（wasm 胶水从 root 取 ipc/document）
  j.run(SRC);
  const 跑 = j.run(SRC);
  if (!跑.ok) { console.error("程序报错：" + JSON.stringify(跑.error)); process.exit(2); }
  const 入口 = globalThis.__jishiWin;
  const 出 = { 有入口: typeof 入口 === "function", 发出: 发出.slice() };
  // ③ 宿主叫页面：传一段**文本载荷**
  入口("调用", JSON.stringify({ 函数: "计算", 载荷: "1+1" }));
  出.命令 = els.get("#命令").textContent;
  // ④ 拖放：载荷是 JSON 文本，里面三个路径
  入口("拖放", JSON.stringify({ 路径们: ["C:/a.txt", "C:/b.txt", "C:/c.txt"] }));
  出.拖放 = els.get("#结果").textContent;
  console.log(JSON.stringify(出));
})();
'''


def test_宿主叫页面与拖放载荷是JSON(tmp_path):
    """③ 页面注册的回调真被 `__jishiWin` 调到；④ 拖放载荷是 JSON（三个路径都在）。

    用**假 DOM** 跑真 wasm：真窗口要桌面，CI 上没有；而「宿主 ↔ 页面」是协议，
    与 DOM 实现无关。`__jishiWin` 正是宿主 `窗口.调用页面` 调的那个入口
    （见 `native_window.rs` 的 `派发` 与 `wasm/jishi-wasm.js`）。
    """
    if not shutil.which("node"):
        pytest.skip("没有 node")
    wasm = ROOT / "site" / "jishi_wasm.wasm"
    js = ROOT / "site" / "jishi-wasm.js"
    if not wasm.is_file() or not js.is_file():
        pytest.skip("还没构建 wasm（tools/build_wasm.py）")
    脚本 = tmp_path / f"page-{os.getpid()}.js"
    脚本.write_text(_页面JS, encoding="utf-8", newline="\n")
    r = subprocess.run(["node", str(脚本), str(js), str(wasm)],
                       capture_output=True, text=True, encoding="utf-8",
                       timeout=180, cwd=str(ROOT))
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    出 = json.loads(r.stdout.strip().splitlines()[-1])
    assert 出["有入口"] is True, 出          # 页面侧真有 `__jishiWin`
    assert 出["命令"] == "收到:计算=1+1", 出    # ③ 回调被调到、函数名与载荷都到了
    assert 出["拖放"] == "拖进来 3 个", 出     # ④ JSON 载荷里三个路径都在
    # 壳里登记事件之后会上报一次「我可以接事件了」（宿主靠它决定何时开始派发）
    assert any(m.get("op") == "宿主事件就绪" for m in 出["发出"]), 出["发出"]


# ---------------------------------------------------------------------------
# ⑥ 做不到的宿主要说出来
# ---------------------------------------------------------------------------

_调用页面 = '''导入 窗口
尝试：
    窗口.调用页面(1, "命令", "载荷")
    打印("没报错")
捕获 异常 为 e：
    打印(e.类型)
    如果 e.消息.包含("这个宿主做不到"):
        打印("说了原因")
'''


def test_Python宿主调用页面明确报错(tmp_path):
    """⑥ 能力缺失要**说出来**：Python/Node 宿主没有原生窗口，报错而不是静默返回假。

    静默的话人会去查「页面处理函数是不是写错了」—— 完全跑偏的方向。
    """
    脚本 = tmp_path / f"call-{os.getpid()}.jsh"
    脚本.write_text(_调用页面, encoding="utf-8", newline="\n")
    r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", str(脚本)],
                       capture_output=True, text=True, encoding="utf-8",
                       timeout=120, cwd=str(ROOT))
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    行 = r.stdout.strip().splitlines()
    assert 行[0] == "运行期错误", 行
    assert 行[1] == "说了原因", 行
