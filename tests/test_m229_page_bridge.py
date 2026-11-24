# -*- coding: utf-8 -*-
"""M229 · 页面 ↔ 宿主双向通道 + 浏览器工具集。

## 这一站解决的是什么

《桌面营造》想把基石的窗口当 Qt 用，撞了五处 —— 它们**是同一层能力**：

| 编号 | 症状 |
|---|---|
| D2 | 页面里拿到用户操作后，**没法告诉宿主**（只能绕 HTTP） |
| D4 | `绑定("#x", "点击", f)` 静默不触发 —— 中文事件名被原样丢给 `addEventListener` |
| D3 | 没有元素查询（`有元素` / `取们` / `取属性` / `设属性` / `焦点`） |
| D12 | 事件回调只有一个 `data-序` 文本，拿不到「按了哪个键」 |
| D11 | `浏览器.请求` 只有 GET，没有方法/正文/头 |

## 判据分三层

1. **协议层**（Node + 假 DOM）：中文事件名真的落到 `addEventListener("click")`、
   认不出来的名字**当场报错**、元素查询五个函数按口径给值、带事件信息给 JSON；
2. **通道层**（真窗口）：页面 `窗口.送宿主(…)` → 宿主 `窗口.当("页面消息", …)`
   真的收到那段文本；
3. **口径层**：新加的函数在 Python / Rust 两份清单里都在（`test_m51` 从另一头盯）。

⚠️ **D11 不在这个文件里**：`浏览器.请求` 的方法/正文/头改的是 wasm 侧 `_request`，
它的判据在 `test_m167_browser.py` 那套里补（同一张假 DOM 表）。
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                            # noqa: E402

WASM = ROOT / "site" / "jishi_wasm.wasm"
#: ⚠️ 读**源码**那份，不读 `site/` 的拷贝 —— `site/` 是 `.gitignore` 掉的构建产物，
#: CI 是干净 checkout（M169 踩过）。`WASM` 是例外：它是发行必需的，CI 会现编。
SHIM = ROOT / "wasm" / "jishi-wasm.js"

#: 假 DOM 探针：装 wasm、给一套**够用的假页面**、跑一段基石源码、把结果报出来。
#:
#: 为什么用假 DOM：真 DOM 只在浏览器里有，而判据要跑在 CI 上。但
#: 「基石 ↔ 页面」这条是**协议**，与 DOM 的具体实现无关 —— 假对象足够验它。
_探针 = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const SRC = fs.readFileSync(process.argv[3], "utf8");
const WASM = process.argv[4];

function 元(序) {
  const o = {
    dataset: 序 === undefined ? {} : { 序: String(序) },
    textContent: "", value: "", style: {}, __attr: {}, __h: {}, __focused: false,
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener(ev, h) { (o.__h[ev] = o.__h[ev] || []).push(h); },
    getAttribute(n) { return Object.prototype.hasOwnProperty.call(o.__attr, n) ? o.__attr[n] : null; },
    setAttribute(n, v) { o.__attr[n] = String(v); },
    focus() { o.__focused = true; },
    fire(ev, 事件) { (o.__h[ev] || []).forEach((h) => h(事件 || { type: ev })); },
  };
  return o;
}

const 池 = { "#甲": 元("a"), "#乙": 元("b"), "#丙": 元("c") };
const 组 = [池["#甲"], 池["#乙"], 池["#丙"]];
const doc = {
  title: "",
  querySelector: (s) => 池[s] || null,
  querySelectorAll: (s) => (s === ".项" ? 组 : []),
};
const kv = new Map();
const store = {
  getItem: (k) => (kv.has(k) ? kv.get(k) : null),
  setItem: (k, v) => kv.set(k, String(v)),
  removeItem: (k) => kv.delete(k),
};

(async () => {
  const 出 = { run: "", stdout: "", 触发后: {}, 请求们: [] };
  // **M229（D11）**：假 `fetch` —— 把「方法/正文/头」全记下来，判据靠它断言。
  const 假fetch = (url, opt) => {
    出.请求们.push({ url: String(url), 方法: (opt && opt.method) || "GET",
                     正文: (opt && opt.body) || "", 头: (opt && opt.headers) || null });
    return Promise.resolve({ text: () => Promise.resolve("回话") });
  };
  try {
    // ⚠️ 传**字节**＋ `{doc, store}`：Node 里没有 `fetch`，也不会自己找 `document`
    const bytes = fs.readFileSync(WASM);
    const j = await JishiWasm.load(bytes, { doc, store, fetch: 假fetch });
    const r = j.run(SRC);
    // 报错时把 hint 也带上 —— 「认不出来」那条判据要检查**可用名字有没有列出来**
    出.run = r.ok ? "ok"
      : ("报错：" + (r.error && r.error.message) + " | " + (r.error && r.error.hint));
    出.stdout = (r.stdout || "").trim();
    // 触发每个元素上可能挂的事件（判据靠它验「中文名真的映射成了英文名」）
    for (const s of Object.keys(池)) {
      池[s].fire("click", { type: "click", key: "Enter", shiftKey: true,
                            ctrlKey: false, altKey: false, target: 池[s] });
      出.触发后[s] = { 文本: 池[s].textContent, 属性: Object.assign({}, 池[s].__attr),
                       焦点: 池[s].__focused };
    }
    出.触发后[".项"] = 组.map((o) => ({ 文本: o.textContent }));
    // 请求是异步的：给一拍再收，否则断言跑在 fetch 之前
    await new Promise((r) => setTimeout(r, 80));
  } catch (e) {
    出.run = "炸了：" + ((e && e.message) || e);
  }
  console.log(JSON.stringify(出));
})();
'''


def _能跑() -> bool:
    return shutil.which("node") is not None and WASM.is_file() and SHIM.is_file()


need_wasm = pytest.mark.skipif(
    not _能跑(),
    reason="没有 node 或没构建 wasm（跑 `python tools/build_wasm.py`）")


def _跑(源码: str, tmp_path: Path) -> dict:
    """把源码丢进假 DOM 里跑一遍，返回探针报的字典。"""
    探针 = tmp_path / f"探针_{os.getpid()}.js"
    探针.write_text(_探针, encoding="utf-8", newline="\n")
    源 = tmp_path / f"程序_{os.getpid()}.jsh"
    源.write_text(源码, encoding="utf-8", newline="\n")
    r = subprocess.run(["node", str(探针), str(SHIM), str(源), str(WASM)],
                       capture_output=True, timeout=180)
    out = r.stdout.decode("utf-8", "replace").strip()
    assert out, f"探针没输出：{r.stderr.decode('utf-8', 'replace')[:800]}"
    return json.loads(out.splitlines()[-1])


# ---------------------------------------------------------------------------
# 一、D4：中文事件名
# ---------------------------------------------------------------------------

@need_wasm
def test_中文事件名落到DOM英文名(tmp_path):
    """`绑定("#甲", "点击", 处理)` ⇒ 页面挂的是 `click`，点了要真的回调。

    🔴 这条钉的正是 D4 那个 bug 的形状：中文名被原样丢给 `addEventListener`，
    浏览器不认、**静默不触发**（点了没反应、没有日志、没有报错）。
    """
    出 = _跑('''导入 浏览器
函数 处理(序)：
    浏览器.设文本("#甲", "点到了")
浏览器.绑定("#甲", "点击", 处理)
''', tmp_path)
    assert 出["run"] == "ok", 出
    assert 出["触发后"]["#甲"]["文本"] == "点到了", f"`点击` 没落到 click 上：{出}"


@need_wasm
def test_不认识的事件名当场报错(tmp_path):
    """写错的事件名不许静默 —— 报错还要把能用的名字列出来。"""
    出 = _跑('''导入 浏览器
函数 处理(序)：
    打印(序)
浏览器.绑定("#甲", "点按", 处理)
''', tmp_path)
    assert 出["run"].startswith("报错"), 出
    assert "不认识事件名" in 出["run"], 出
    assert "点击" in 出["run"], f"没列出可用名字：{出['run']}"


# ---------------------------------------------------------------------------
# 二、D3：元素查询
# ---------------------------------------------------------------------------

@need_wasm
def test_元素查询五个函数(tmp_path):
    """`有元素` / `取们` / `取属性` / `设属性` / `焦点` 按口径给值。

    `取们` 给的是每个元素的 **`数据_序`**（与 `绑定` 回调的实参同一个口径），
    不是 DOM 下标 —— 宿主拿它去对自己的数据表。
    """
    出 = _跑('''导入 浏览器
打印(浏览器.有元素("#甲"))
打印(浏览器.有元素("#没有"))
打印(浏览器.取们(".项"))
打印(浏览器.取属性("#甲", "数据-x"))
浏览器.设属性("#甲", "数据-x", "7")
打印(浏览器.取属性("#甲", "数据-x"))
打印(浏览器.焦点("#甲"))
打印(浏览器.焦点("#没有"))
''', tmp_path)
    assert 出["run"] == "ok", 出
    行们 = 出["stdout"].splitlines()
    assert 行们[0] == "真", f"有元素(#甲) 应为真：{行们}"
    assert 行们[1] == "假", f"有元素(#没有) 应为假：{行们}"
    # 基石的列表按 Python 风格打印（单引号），别按 JSON 写期望
    assert 行们[2] == "['a', 'b', 'c']", f"取们 应为三个 数据_序：{行们}"
    assert 行们[3] == "空", f"没设过的属性应为空：{行们}"
    assert 行们[4] == "7", f"设完再读应为 7：{行们}"
    assert 行们[5] == "真", f"焦点(#甲) 应为真：{行们}"
    assert 出["触发后"]["#甲"]["焦点"] is True, f"`focus()` 没真调到：{出}"


# ---------------------------------------------------------------------------
# 三、D12：结构化事件（默认关，开了才给 JSON）
# ---------------------------------------------------------------------------

@need_wasm
def test_带事件信息时回调收JSON(tmp_path):
    """`绑定(…, 真)` ⇒ 回调收到一段 JSON（`序` / `类型` / `键` / `shift`…）。"""
    出 = _跑('''导入 浏览器
函数 处理(信息)：
    浏览器.设文本("#甲", 信息)
浏览器.绑定("#甲", "点击", 处理, 真)
''', tmp_path)
    assert 出["run"] == "ok", 出
    文 = 出["触发后"]["#甲"]["文本"]
    单 = json.loads(文)
    assert 单["序"] == "a", f"JSON 里的 `序` 不对：{单}"
    assert 单["类型"] == "click", f"`类型` 不对：{单}"
    assert 单["键"] == "Enter", f"`键` 不对：{单}"
    assert 单["shift"] is True, f"`shift` 不对：{单}"


@need_wasm
def test_不带事件信息时仍是老的序号文本(tmp_path):
    """**默认关**：不写第 4 个参数就是老行为（回调收 `数据_序` 文本）。"""
    出 = _跑('''导入 浏览器
函数 处理(序)：
    浏览器.设文本("#甲", "序=" + 序)
浏览器.绑定("#甲", "点击", 处理)
''', tmp_path)
    assert 出["run"] == "ok", 出
    assert 出["触发后"]["#甲"]["文本"] == "序=a", f"默认行为变了：{出}"


@need_wasm
def test_请求支持方法正文头(tmp_path):
    """**M229（D11）**：`请求(网址, 函数名, 方法, 正文, 头)` —— 以前只有 GET。

    桌面营造要 POST 一个表单就得绕去起一个本地服务，那是冤的。
    """
    # 🔴 **必须用 `存本地` 做个「只请求一次」的闸**：回调是「重跑整份源码再调函数」，
    # 顶层那句 `请求` 会被再执行一遍 —— 不拦就是无限请求（判据当场超时）。
    出 = _跑('''导入 浏览器
函数 收到(文)：
    浏览器.存本地("请求过", "是")
    浏览器.设文本("#甲", 文)
如果 浏览器.读本地("请求过") == 空：
    浏览器.请求("http://例子.test/提交", 收到, "POST", "甲=1", {"内容类型": "text/plain"})
''', tmp_path)
    assert 出["run"] == "ok", 出
    # ⚠️ **不假设只请求一次**：回调是「重跑整份源码再调函数」，而重跑时那句
    # `如果 读本地 == 空` 在**上一次**回调把标记存下来之前还是真的 —— 所以
    # 头一拍会发两次，之后稳定。这是「一次事件 = 一次完整执行」的必然结果，
    # 判据要验的是**第一次请求的参数**，不是请求次数。
    assert 出["请求们"], f"没发出请求：{出}"
    单 = 出["请求们"][0]
    assert 单["方法"] == "POST", f"方法没传下去：{单}"
    assert 单["正文"] == "甲=1", f"正文没传下去：{单}"
    assert 单["头"] == {"内容类型": "text/plain"}, f"头没传下去：{单}"
    # ⚠️ **不验回调链路**：那是 M167 起就有的行为，当时就没有判据；这次只改
    # 「方法/正文/头」三样。本机实测回调能跑，但在假 DOM 探针里它没触发
    # （探针的 fetch 是同步 resolve 的，`call` 的时机抓不住）—— 与其写一条
    # 时灵时不灵的断言，不如如实标「这里没验」。


@need_wasm
def test_请求不写方法就是GET(tmp_path):
    """不给第 3 个参数 ⇒ 还是 GET（存量代码一个字节都没变）。"""
    出 = _跑('''导入 浏览器
函数 收到(文)：
    浏览器.存本地("请求过", "是")
    打印(文)
如果 浏览器.读本地("请求过") == 空：
    浏览器.请求("http://例子.test/读", 收到)
''', tmp_path)
    assert 出["run"] == "ok", 出
    单 = 出["请求们"][0]
    assert 单["方法"] == "GET" and 单["正文"] == "", f"默认不是 GET：{单}"


# ---------------------------------------------------------------------------
# 四、D2：页面 → 宿主（真窗口，需要桌面）
# ---------------------------------------------------------------------------

#: 真开窗的判据在无桌面 / 非 Windows 上必红（与 M174 同一个口径）
需要桌面 = pytest.mark.skipif(
    os.environ.get("JISHI_NO_GUI") == "1" or not sys.platform.startswith("win"),
    reason="要真开窗口（CI 上没有桌面）")


@需要桌面
def test_页面消息真的到宿主(tmp_path):
    """页面 `窗口.送宿主("…")` → 宿主 `窗口.当("页面消息", 函数)` 收到那段**原样文本**。

    ⚠️ 这条**必须真开窗**：IPC 是 wry 的、只存在于真窗口里。
       CI 上是空白（记在 CHANGELOG 的「没被 CI 覆盖」那条）。
    """
    exe = rust_exe_path()
    if not exe.is_file():
        pytest.skip("没编 jishi-rs")

    (tmp_path / "页.html").write_text('''<!doctype html><meta charset="utf-8"><title>M229</title>
<script>
const 源码 = `导入 窗口
窗口.送宿主("你好，宿主")`;
(async () => {
  const M = (await import("/__jishi/jishi-wasm.js")).default || window.JishiWasm;
  const j = await M.load("/__jishi/jishi_wasm.wasm");
  j.run(源码);
  window.ipc && window.ipc.postMessage(JSON.stringify({op:"引擎就绪"}));
})();
</script>
''', encoding="utf-8", newline="\n")

    (tmp_path / "主机.jsh").write_text('''导入 窗口
函数 收到(文)：
    打印("宿主收到：" + 文)
    窗口.全部关闭()
窗口.当("页面消息", 收到)
窗口.打开({"页面": "页.html", "标题": "M229", "原生": 真, "宽": 520, "高": 320})
窗口.等()
''', encoding="utf-8", newline="\n")

    # ⚠️ 不能用 capture_output：WebView2 子进程继承管道会挂满超时（M174 的教训）
    日志 = tmp_path / "输出.txt"
    with 日志.open("wb") as f:
        subprocess.run([str(exe), "主机.jsh"], cwd=str(tmp_path), stdout=f,
                       stderr=subprocess.STDOUT, timeout=180, stdin=subprocess.DEVNULL)
    文 = 日志.read_text(encoding="utf-8", errors="replace")
    assert "宿主收到：你好，宿主" in 文, f"页面消息没到宿主：{文[-1200:]}"


# ---------------------------------------------------------------------------
# 五、口径层：新函数两份清单里都在
# ---------------------------------------------------------------------------

def test_新函数三处都在():
    """`浏览器` 的新五个 + `窗口.送宿主` —— Python / Rust / 卡三处都要有。"""
    py浏览 = (ROOT / "oracle" / "jishi" / "stdlib" / "浏览器.py").read_text(encoding="utf-8")
    rs浏览 = (ROOT / "rust" / "src" / "stdlib" / "browser.rs").read_text(encoding="utf-8")
    for 名 in ("有元素", "取们", "取属性", "设属性", "焦点"):
        assert f"def {名}(" in py浏览, f"Python 侧缺 `浏览器.{名}`"
        assert f'"{名}"' in rs浏览, f"Rust 侧缺 `浏览器.{名}`"

    py窗 = (ROOT / "oracle" / "jishi" / "stdlib" / "窗口.py").read_text(encoding="utf-8")
    rs窗 = (ROOT / "rust" / "src" / "stdlib" / "window.rs").read_text(encoding="utf-8")
    assert "def 送宿主(" in py窗, "Python 侧缺 `窗口.送宿主`"
    assert '"送宿主"' in rs窗, "Rust 侧缺 `窗口.送宿主`"
    assert '"页面消息"' in rs窗, "Rust 侧缺「页面消息」事件名"
