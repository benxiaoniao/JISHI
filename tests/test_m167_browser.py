# -*- coding: utf-8 -*-
"""M167 · 浏览器交互：`浏览器` 模块 + wasm 回调（ABI 2）。

## 这一站解决的是什么

基石的 VM 是**同步**的，浏览器事件是**异步**的（D81 已经因为这条，把
`HTTP服务端` 在 Node 上判成不支持）。硬缝会两边都变形。

设计定死一句话：

> **一次事件 = 一次完整执行。**

程序启动时用 `绑定` 把「哪个函数处理哪个事件」告诉页面；事件来的时候
JS 调 wasm 的 `jishi_call(函数名)`，那边**重跑一遍程序**（重建全局状态），
再执行那个函数。

⚠️ **必然的推论：状态要自己持久化**（`存本地` / 读 DOM）。这不是缺陷 ——
「刷新页面数据还在」本来就是同一个要求，一套写法两个都满足。

## 判据分三层

1. **平台边界**：命令行 / Node 上 `浏览器.*` 报**同一条**中文错（三宿主逐字相同）；
2. **wasm 里真能用**：Node 装 wasm + **假 DOM**，跑「读本地→加一→存本地→设文本」，
   跑三遍看它是不是 1/2/3（这条同时验了「无状态重放」这个设计真的成立）；
3. **事件链路**：`绑定` 之后触发事件，回调要执行、DOM 要变、**监听不能重复挂**
   （重跑程序会再执行一次顶层 `绑定` —— 不幂等的话点一次挂两个）。

## 为什么不进语料

`tests/cases/` 的语料是**五引擎逐字比**。而 `浏览器` 在命令行 / Node 上报错，
错误里的**位置**两边不一样（Node 走字节码，`0:1`；Python 是真实的 `2:8`）——
那是既有的、与本站无关的差异。所以平台错用**文案比对**（判据 1）而不是塞进语料。
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                            # noqa: E402

WASM = ROOT / "site" / "jishi_wasm.wasm"
#: ⚠️ **读源码那份，不读 `site/` 的拷贝**（M169 CI 红过一次）：
#: `site/` 是 `.gitignore` 掉的**构建产物**，CI 上根本没有 ⇒ 判据会
#: `FileNotFoundError`。判据要跑在「刚 clone 下来」的仓库上，只许依赖**被跟踪的文件**。
SHIM = ROOT / "wasm" / "jishi-wasm.js"

#: `浏览器` 的 13 个函数（**三宿主一个都不能少** —— `test_m51` 从另一头盯着）。
函数们 = [
    "设文本", "取值", "设值", "加类", "删类", "切类", "点击", "显示",
    "绑定", "存本地", "读本地", "删本地", "请求",
]


# ---------------------------------------------------------------------------
# 一、平台边界：三宿主同一条错
# ---------------------------------------------------------------------------

平台错 = ("这个平台没有浏览器：「浏览器」模块只在网页（wasm）里可用。"
          "命令行 / Node 上要抓网页请用「html」模块解析、要发请求请用「网络」模块。")


def _跑(源码: str, tmp_path: Path, 引擎: str) -> str:
    from oracle.jishi import serialize
    from oracle.jishi.compiler import compile_source

    (tmp_path / "程序.jsh").write_text(源码, encoding="utf-8", newline="\n")
    if 引擎 == "Rust":
        命令 = [str(rust_exe_path()), "程序.jsh"]
    elif 引擎 == "Node":
        (tmp_path / "程序.json").write_text(
            serialize.dumps(compile_source(源码, "程序.jsh")), encoding="utf-8")
        命令 = ["node", str(ROOT / "oracle" / "node" / "index.js"), "程序.json"]
    else:
        命令 = [sys.executable, "-m", "oracle.jishi.cli", "程序.jsh"]
    r = subprocess.run(命令, cwd=str(tmp_path), capture_output=True, timeout=120, input=b"")
    return (r.stdout + r.stderr).decode("utf-8", "replace")


def test_命令行上报平台错_三宿主逐字相同(tmp_path):
    """`浏览器.设文本(...)` 在三个宿主上都要报**同一条**中文错。

    为什么强调「同一条」：这些函数在三个宿主上**都不存在真实现**，
    文案一漂，用户在不同引擎上看到的解释就不一样 —— 而这正是「如实告知
    平台边界」的全部价值。
    """
    源 = '导入 浏览器\n浏览器.设文本("#标题", "你好")\n'
    出 = {名: _跑(源, tmp_path, 名) for 名 in ("Python", "Rust", "Node") if
          (名 != "Rust" or rust_exe_path().is_file())}
    for 名, 文 in 出.items():
        assert 平台错 in 文, f"{名} 没报平台错：{文[:300]}"


def test_十三个函数三宿主都在():
    """`浏览器` 的 13 个函数在 Python / Rust / Node 三份实现里都得有。

    `test_m51_consistency` 从「宿主自报家底」那头盯着同一件事；这条直接查源码，
    报错时能**指出缺的是哪个宿主、哪个函数**。
    """
    py = (ROOT / "oracle" / "jishi" / "stdlib" / "浏览器.py").read_text(encoding="utf-8")
    rs = (ROOT / "rust" / "src" / "stdlib" / "browser.rs").read_text(encoding="utf-8")
    js = (ROOT / "oracle" / "node" / "vm.js").read_text(encoding="utf-8")
    for 名 in 函数们:
        assert f"def {名}(" in py, f"Python 侧缺 `{名}`"
        assert f'"{名}"' in rs, f"Rust 侧缺 `{名}`"
        assert f"'{名}': _noBrowser('{名}')" in js, f"Node 侧缺 `{名}`"


# ---------------------------------------------------------------------------
# 二、wasm 里真能用（Node + 假 DOM）
# ---------------------------------------------------------------------------

#: Node 侧的测试脚本：装 wasm、给一套**假 DOM / localStorage**、跑两轮。
#:
#: 为什么用假 DOM：真 DOM 的取值逻辑只有在浏览器里才验得了（判据跑在 CI 上）。
#: 但「基石 ↔ 宿主」这条通道是**协议**，跟 DOM 具体实现无关 —— 假对象足够验它。
_JS = r'''
const fs = require("fs"), path = require("path");
const JishiWasm = require(process.argv[2]);

const els = new Map();
function makeEl() {
  const cls = new Set(), hs = {};
  return {
    textContent: "", value: "", style: {}, __hs: hs,
    classList: {
      add: (c) => cls.add(c), remove: (c) => cls.delete(c),
      toggle: (c) => (cls.has(c) ? cls.delete(c) : cls.add(c)),
      has: (c) => cls.has(c),
    },
    addEventListener(ev, h) { (hs[ev] = hs[ev] || []).push(h); },
    fire(ev) { (hs[ev] || []).forEach((h) => h({ type: ev })); },
  };
}
const doc = { querySelector: (s) => {
  if (!els.has(s)) els.set(s, makeEl());
  return els.get(s);
} };
const kv = new Map();
const store = {
  getItem: (k) => (kv.has(k) ? kv.get(k) : null),
  setItem: (k, v) => kv.set(k, String(v)),
  removeItem: (k) => kv.delete(k),
};

const SRC = `导入 浏览器
函数 加一()：
    令 原 = 浏览器.读本地("n")
    令 n = 0
    如果 原 != 空：
        n = 整数(原)
    n = n + 1
    浏览器.存本地("n", n)
    浏览器.设文本("#显示", "点了 " + 文本(n) + " 次")
    打印("跑了 " + 文本(n) + " 次")
浏览器.绑定("#按钮", "点击", 加一)
浏览器.设文本("#显示", "还没点")
`;

(async () => {
  const bytes = fs.readFileSync(process.argv[3]);
  const j = await JishiWasm.load(bytes, { doc, store });
  const 出 = { abi: j._ex.jishi_version(), 跑: [], 点: [], 监听: 0, 类: null };

  // ⓪ 先跑一遍（顶层只做「绑定 + 设文本」，不会调 加一）
  j.run(SRC);
  出.初始 = els.get("#显示").textContent;

  // ① 无状态重放：`call` 三次 = 三次「完整执行 + 调处理函数」，
  //    数应该 1 / 2 / 3（靠 localStorage 活着）
  for (let i = 0; i < 3; i++) {
    const r = j.call("加一");
    出.跑.push(r.ok ? r.stdout.trim() : "ERR:" + r.error.message);
  }
  出.显示 = els.get("#显示").textContent;

  // ② 事件链路：点两下（走的是 DOM 事件 → 回调 → call，与浏览器同一条路）
  const btn = els.get("#按钮");
  出.监听 = (btn.__hs["点击"] || []).length;
  btn.fire("点击");
  出.点.push(els.get("#显示").textContent);
  btn.fire("点击");
  出.点.push(els.get("#显示").textContent);
  // ③ 幂等：点两下之后监听数不该变（每次回调都会重跑顶层那句 `绑定`）
  出.监听2 = (btn.__hs["点击"] || []).length;

  // ④ 类操作
  j.run('导入 浏览器\n浏览器.加类("#盒子", "亮")\n浏览器.切类("#盒子", "亮")\n');
  出.类 = els.get("#盒子").classList.has("亮");

  console.log(JSON.stringify(出));
})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
'''


@pytest.mark.skipif(not WASM.is_file(), reason="没构建 wasm（跑 python tools/build_wasm.py）")
@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_wasm里真能用(tmp_path):
    """**这一条是本里程碑的硬指标**：wasm 里 `浏览器` 模块真的能操作页面。

    验了四件事：
    ① `ABI_VERSION == 2`（`jishi_call` 是这一版加的）；
    ② **无状态重放成立**：同一个源码跑三遍，localStorage 里的数 1 → 2 → 3
       —— 这正是「一次事件 = 一次完整执行」这个设计的可行性证明；
    ③ **事件链路通**：`绑定` 后触发 `点击`，回调执行、DOM 跟着变；
    ④ **绑定幂等**：点两下之后监听数不变（回调会重跑顶层 `绑定`，
       不幂等就会越挂越多，越点越慢）。
    """
    脚本 = tmp_path / "t.js"
    脚本.write_text(_JS, encoding="utf-8", newline="\n")
    r = subprocess.run(["node", str(脚本), str(SHIM), str(WASM)],
                       capture_output=True, timeout=180)
    assert r.returncode == 0, (r.stdout + r.stderr).decode("utf-8", "replace")[-1200:]
    出 = json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])

    # ⚠️ 这个数字**跟着 `wasm/src/lib.rs` 的 `ABI_VERSION` 走**：
    #    M167 加的（=2），M174 加 `jishi_native` 之后 =3。改 ABI 要一起改这里。
    assert 出["abi"] == 3, f"wasm 的 ABI 版本是 {出['abi']}，应当是 3（M174 之后）"
    assert 出["初始"] == "还没点"
    assert 出["跑"] == ["跑了 1 次", "跑了 2 次", "跑了 3 次"], (
        f"无状态重放没成立：{出['跑']}（localStorage 该累加，说明「状态没持久化」"
        "或「每次都是新实例」）")
    assert 出["显示"] == "点了 3 次"
    assert 出["监听"] == 1, f"绑定了 {出['监听']} 个监听，应当是 1"
    assert 出["点"] == ["点了 4 次", "点了 5 次"], f"点击回调没生效：{出['点']}"
    assert 出["监听2"] == 1, (
        f"点两下之后监听变成 {出['监听2']} 个 —— **绑定不幂等**，"
        "而回调每次都会重跑顶层那句 `绑定`，所以这个数必须一直是 1")
    assert 出["类"] is False, "`加类` 之后 `切类` 应当把它去掉"


@pytest.mark.skipif(not (ROOT / "site" / "jishi-wasm.js").is_file(),
                    reason="还没构建 wasm（`site/` 是构建产物，CI 上没有）")
def test_shim的语法自检():
    """`wasm/jishi-wasm.js` 与 `site/` 那份必须**同源**（构建时是拷贝）。

    改了一边忘了另一边，站点上跑的就是旧逻辑 —— 那类问题在浏览器里极难查。

    📌 **只在构建过（`site/` 存在）时才比**：`site/` 是 `.gitignore` 掉的产物，
    CI 的干净 checkout 里没有它 —— 早先这里无条件读，CI 直接
    `FileNotFoundError`（M169 修）。判据的口径是「**只要拷过去了，就必须一样**」，
    而不是「每个环境都必须有一份拷贝」。
    """
    源 = (ROOT / "wasm" / "jishi-wasm.js").read_text(encoding="utf-8")
    站点 = (ROOT / "site" / "jishi-wasm.js").read_text(encoding="utf-8")
    assert 源 == 站点, "`wasm/jishi-wasm.js` 与 `site/jishi-wasm.js` 不一致（跑 build_wasm.py）"


# ---------------------------------------------------------------------------
# 三、交付物：`examples/browser/待办.html` 里的基石源码真能加、能删、能持久化
# ---------------------------------------------------------------------------

_示例JS = r'''
const fs = require("fs"), path = require("path");
const JishiWasm = require(process.argv[2]);
const src = fs.readFileSync(process.argv[4], "utf8");

const els = new Map();
function makeEl() {
  const hs = {}, cls = new Set();
  return {
    textContent: "", innerHTML: "", value: "", style: {}, dataset: {}, __hs: hs,
    classList: { add: (c) => cls.add(c), remove: (c) => cls.delete(c),
                 toggle: (c) => (cls.has(c) ? cls.delete(c) : cls.add(c)),
                 has: (c) => cls.has(c) },
    addEventListener(ev, h) { (hs[ev] = hs[ev] || []).push(h); },
    fire(ev, target) { (hs[ev] || []).forEach((h) => h({ type: ev, target: target || this })); },
  };
}
const doc = { querySelector: (s) => {
  if (!els.has(s)) els.set(s, makeEl());
  return els.get(s);
} };
const kv = new Map();
const store = { getItem: (k) => (kv.has(k) ? kv.get(k) : null),
                setItem: (k, v) => kv.set(k, String(v)),
                removeItem: (k) => kv.delete(k) };

(async () => {
  const bytes = fs.readFileSync(process.argv[3]);
  const j = await JishiWasm.load(bytes, { doc, store });
  const 出 = {};
  出.首次 = j.run(src).ok;
  出.空清单 = els.get("#清单").innerHTML;
  doc.querySelector("#输入").value = "买牛奶";
  出.加一 = j.call("加一条").ok;
  doc.querySelector("#输入").value = "写代码";
  j.call("加一条");
  出.两条 = kv.get("待办");
  出.清单 = els.get("#清单").innerHTML;
  // 点第一行的【删】：模拟点到的那个 button（带 data-序）
  els.get("#清单").fire("点击", { dataset: { "序": "0" } });
  出.删后 = kv.get("待办");
  出.监听 = (els.get("#清单").__hs["点击"] || []).length;
  console.log(JSON.stringify(出));
})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
'''


@pytest.mark.skipif(not WASM.is_file(), reason="没构建 wasm")
@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_示例待办能加能删(tmp_path):
    """`examples/browser/待办.html` 里那段基石源码 —— **加、删、持久化**都要真成立。

    这条是 M167 的「做完是什么样」：一个待办清单网页，点按钮加一条、
    点叉删一条、刷新还在。判据把它整段源码扒出来放到假 DOM 上跑一遍。

    ⚠️ 主程序那段源码在 HTML 里是**模板字符串**，改 HTML 结构时别把它弄断
    （这条判据也会因为「扒不到源码」而红）。
    """
    import re
    html = (ROOT / "examples" / "browser" / "待办.html").read_text(encoding="utf-8")
    m = re.search(r"const 源码 = `(.*?)`;", html, re.S)
    assert m, "`待办.html` 里找不到 `const 源码 = \\`…\\`;` 那段"
    源文件 = tmp_path / "待办.jsh"
    源文件.write_text(m.group(1), encoding="utf-8", newline="\n")
    脚本 = tmp_path / "t.js"
    脚本.write_text(_示例JS, encoding="utf-8", newline="\n")

    r = subprocess.run(["node", str(脚本), str(SHIM), str(WASM), str(源文件)],
                       capture_output=True, timeout=180)
    assert r.returncode == 0, (r.stdout + r.stderr).decode("utf-8", "replace")[-1200:]
    出 = json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])

    assert 出["首次"] is True, "示例源码第一次跑就失败了"
    assert 'class="空"' in 出["空清单"], f"空清单该显示提示文案：{出['空清单']}"
    assert 出["加一"] is True, "`加一条` 失败"
    assert 出["两条"] == '["买牛奶", "写代码"]', f"没存对：{出['两条']}"
    assert "买牛奶" in 出["清单"] and "写代码" in 出["清单"]
    assert "data-序=" in 出["清单"], (
        "删除按钮上要有 `data-序` —— 事件回调靠它知道删第几条。"
        "⚠️ 属性名要写 `data_序`（基石把 `_` 换成 `-`），写 `数据_序` 会变成无效属性")
    assert 出["删后"] == '["写代码"]', f"点第一条的删之后应当是剩下第二条：{出['删后']}"
    assert 出["监听"] == 1, "事件委托挂在 #清单 上，重画列表后监听数必须还是 1"
