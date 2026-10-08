# -*- coding: utf-8 -*-
"""R8.1：wasm playground 的引擎 —— 与原生宿主**同一份结果** + 独立判据。

## 这一件在做什么

R8 的目标之一是「playground 改成 wasm」（现在跑的是 **Pyodide**，即 Python 实现）。
R8.1 把**引擎那半边**做完：`wasm/` crate（裸 C ABI，**不上 wasm-bindgen**）+
`site/jishi-wasm.js`（手写 shim）。

⚠️ 站点页面本身（`site/playground.html` 从 Pyodide 换成 wasm）是 **R8.2**，
不在本文件里。

## 判据

* **对拍**：同一批语料，`wasm` 里的结果 JSON 与 `jishi-rs --sandbox --json-result`
  **逐字节一致**（`duration_ms` 归一 —— wasm 上没有时钟，见下）。
* **独立判据**：写死期望（不 import oracle.jishi），R8.4 之后 Python 退场仍然要绿。

## 两条**wasm 特有**的坑（都是实测踩出来的，写在这里免得重踩）

1. **`std::time::Instant::now()` 在 wasm 上会 panic**（
   `time not implemented on this platform`）—— 而 `panic = "abort"` 下它表现成
   一句英文的 `RuntimeError: unreachable`，看着完全不像时钟问题。
   ⇒ `sandbox.rs` 的计时在 wasm 上恒 0，**真实耗时由 shim 用 `performance.now()` 量**。
2. **wasm 没有线程**：`sandbox` 的超时（另起线程 + `recv_timeout`）用不了，
   `std::thread::spawn` 直接 panic。⇒ 走 `run_sandboxed_direct` + **步数预算**。

⚠️ 步数预算只在 `wasm/` 这个 crate 开的 `budget` 特征里编译进来（原生宿主不开：
实测打开会让指令循环的回跳处 +6%，但**关掉时与加这个功能之前逐位同效**，1.000x）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))     # 页面生成器（build_playground）

#: 构建产物（`site/` 是产物目录、被 gitignore；`tools/build_wasm.py` 负责生成）。
WASM = ROOT / "site" / "jishi_wasm.wasm"
#: ⚠️ 胶水用**源码那份**（`wasm/jishi-wasm.js`），不用 `site/` 里的拷贝 ——
#: 免得「拷贝过期」把判据糊过去。
SHIM = ROOT / "wasm" / "jishi-wasm.js"
TMP_SUFFIX = f"{os.getpid()}"


def _exe() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


EXE = _exe()

need_wasm = pytest.mark.skipif(
    not (WASM.is_file() and EXE.is_file()),
    reason="缺 site/jishi_wasm.wasm（跑 `python tools/build_wasm.py`）"
           "或 jishi-rs（跑 `cd rust && cargo build --release`）"
           "—— 与 `need_rust` 同一口径：**跳过而不是替人构建**")

need_node = pytest.mark.skipif(
    subprocess.run(["node", "--version"], capture_output=True).returncode != 0,
    reason="本机没有 node，跳过 wasm 对拍")


#: `duration_ms` 是墙上时间，两边永远不等 —— 比之前先归一。
#: ⚠️ wasm 上**没有时钟**：那个 0.0 是引擎给的，shim 会用 `performance.now()` 覆盖掉。
DUR = re.compile(rb'"duration_ms": [0-9.eE+-]+')


def _norm(t: bytes) -> bytes:
    return DUR.sub(b'"duration_ms": 0', t)


# ---------------------------------------------------------------------------
# Node 侧的小驱动（临时生成，不进仓库）
# ---------------------------------------------------------------------------

_HARNESS = r"""
const fs = require('fs');
const JishiWasm = require(process.argv[2]);
const wasmPath = process.argv[3];
const srcs = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
const budget = process.argv[5] ? Number(process.argv[5]) : undefined;
(async () => {
  const j = await JishiWasm.load(fs.readFileSync(wasmPath));
  if (budget !== undefined) j.setBudget(budget);
  const out = [];
  for (const s of srcs) {
    const r = j.run(s);
    delete r.durationMs;   // shim 加的墙上时间（camelCase）—— 协议本身没有它
    out.push(r);
  }
  process.stdout.write(JSON.stringify({ abi: JishiWasm.ABI_VERSION,
                                        budget: j.getBudget(), out: out }));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def _node(sources, *, budget=None, tmp=None):
    """在 wasm 里跑一批源码，返回 `{abi, budget, out}`。"""
    tmp = Path(tmp)
    (tmp / f"harness_{TMP_SUFFIX}.js").write_text(_HARNESS, encoding="utf-8")
    (tmp / f"srcs_{TMP_SUFFIX}.json").write_text(
        json.dumps(sources, ensure_ascii=False), encoding="utf-8")
    cmd = ["node", str(tmp / f"harness_{TMP_SUFFIX}.js"), str(SHIM), str(WASM),
           str(tmp / f"srcs_{TMP_SUFFIX}.json")]
    if budget is not None:
        cmd.append(str(budget))
    r = subprocess.run(cmd, capture_output=True, cwd=str(ROOT), timeout=180)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:1500]
    return json.loads(r.stdout.decode("utf-8"))


def _host(source) -> bytes:
    """原生宿主的沙箱结果（协议 JSON，stdout）。"""
    r = subprocess.run(
        [str(EXE), "--stdin", "--sandbox", "--json-result"],
        input=source.encode("utf-8"), capture_output=True,
        cwd=str(ROOT), timeout=120)
    assert r.stdout, r.stderr.decode("utf-8", "replace")[:400]
    return r.stdout


# ---------------------------------------------------------------------------
# 一、对拍：同一批语料，wasm 与原生宿主的结果 JSON 逐字节一致
# ---------------------------------------------------------------------------

#: 挑的都是**会正常结束、不读标准输入**的语料（读输入的两边都会停在那儿）。
FILES = [
    "tests/cases/01_算术.jsh",
    "tests/cases/02_判断.jsh",
    "tests/cases/03_循环.jsh",
    "tests/cases/04_函数.jsh",
    "tests/cases/10_复合赋值与逻辑.jsh",
    "tests/cases/11_emoji与码点.jsh",
    "tests/cases/18_集合.jsh",
    "tests/cases/26_遍历中改列表.jsh",
    "tests/error_cases/01_未定义名字.jsh",
    "tests/error_cases/02_除以零.jsh",
    "tests/error_cases/05_字符串没结束.jsh",
    "tests/error_cases/06_关键字粘连.jsh",
]


@need_wasm
@need_node
def test_对拍_语料结果与原生宿主一致(tmp_path):
    srcs = [(ROOT / f).read_text(encoding="utf-8") for f in FILES
            if (ROOT / f).is_file()]
    assert len(srcs) >= 8, "语料没找到，路径写错了？"
    got = _node(srcs, tmp=tmp_path)["out"]
    assert len(got) == len(srcs)

    bad = []
    for f, src, g in zip(FILES, srcs, got):
        # wasm 那边给的是解析后的对象；序列化成与宿主同形的 JSON 再比
        # （两边都是 `ensure_ascii=False` 的紧凑 JSON，键序也一样）
        # ⚠️ 宿主是 CLI，最后那个 `println!` 会多一个 `\n`；wasm 返回的是裸 JSON。
        # 那一个换行不是「结果」，去掉再比。
        mine = _norm(json.dumps(g, ensure_ascii=False,
                                separators=(", ", ": ")).rstrip("\n").encode("utf-8"))
        theirs = _norm(_host(src).rstrip(b"\n"))
        if mine != theirs:
            bad.append(f"{f}\n  wasm: {mine[:300]!r}\n  宿主: {theirs[:300]!r}")
    assert not bad, "结果对不上：\n" + "\n".join(bad)


# ---------------------------------------------------------------------------
# 二、独立判据（不 import oracle.jishi；R8.4 之后 Python 退场仍要绿）
# ---------------------------------------------------------------------------

@need_wasm
@need_node
def test_独立_基本求值与报错(tmp_path):
    """写死期望：算术 / 递归 / 标准库 / 语法错 / 运行期错。"""
    srcs = [
        "令 甲 = 1\n令 乙 = 2\n打印(甲 + 乙)\n",
        "函数 斐(数)：\n    如果 数 < 2：\n        返回 数\n"
        "    返回 斐(数 - 1) + 斐(数 - 2)\n打印(斐(15))\n",
        "导入 数学\n打印(数学.开方(16.0))\n",
        "打印(甲 +)\n",
        "打印(1 / 0)\n",
    ]
    got = _node(srcs, tmp=tmp_path)["out"]
    assert got[0]["ok"] and got[0]["stdout"] == "3\n", got[0]
    assert got[1]["ok"] and got[1]["stdout"] == "610\n", got[1]
    assert got[2]["ok"] and got[2]["stdout"] == "4.0\n", got[2]
    # 语法错：码 / 标题 / 行列都在（与原生同形）
    assert got[3]["ok"] is False
    assert got[3]["error"]["code"] == "E0201"
    assert got[3]["error"]["line"] == 1 and got[3]["error"]["col"] == 7
    # 运行期错（除零）
    assert got[4]["ok"] is False
    assert got[4]["error"]["code"] == "E2001", got[4]
    assert got[4]["error"]["title"] == "不能除以零", got[4]


@need_wasm
@need_node
def test_独立_步数预算兜住死循环(tmp_path):
    """死循环**必须停下来**并给一句中文 —— 页面不能冻住。

    wasm 上没有线程，`sandbox` 的超时用不了 ⇒ 靠**步数预算**（回跳计数）。
    """
    got = _node(["令 甲 = 0\n当 真：\n    甲 = 甲 + 1\n"], budget=20000,
                tmp=tmp_path)
    r = got["out"][0]
    assert got["budget"] == 20000, got["budget"]
    assert r["ok"] is False, r
    msg = json.dumps(r, ensure_ascii=False)
    assert "执行步数超过上限" in msg, msg


@need_wasm
@need_node
def test_独立_默认预算是有限值(tmp_path):
    """**默认就是有限预算**（站点忘了设也不会冻住页面）；`0` 才是「不限」。"""
    got = _node(["打印(1)\n"], tmp=tmp_path)
    assert got["budget"] > 0, "默认不该是「不限」—— 那是会让页面冻住的配置"
    got0 = _node(["打印(1)\n"], budget=0, tmp=tmp_path)
    assert got0["budget"] == 0, got0["budget"]


@need_wasm
@need_node
def test_独立_无限递归被接住(tmp_path):
    """无限递归会撞 wasm 的栈上限 —— 要给中文，不能把英文 trap 甩给用户。"""
    got = _node(["函数 递归(数)：\n    返回 递归(数 + 1)\n打印(递归(0))\n"],
                tmp=tmp_path)
    r = got["out"][0]
    assert r["ok"] is False, r
    msg = json.dumps(r, ensure_ascii=False)
    assert "_threw" not in msg, f"trap 没被 shim 接住：{msg}"
    assert "程序跑飞了" in msg, msg


@need_wasm
def test_独立_wasm体积记一笔():
    """体积是 R8 的验收线之一（宿主 ≤5MB）—— wasm 这份也看得见。

    这条**不做阈值断言**（R8 的线是给**宿主二进制**定的，wasm 没有既定线），
    只保证「它还在一个能上网页的量级」。
    """
    n = WASM.stat().st_size
    assert n < 5 * 1024 * 1024, f"wasm 模块 {n:,} 字节，超过 5MB 了"


# ---------------------------------------------------------------------------
# 三、R8.2：**生成出来的那个页面**真能跑（端到端）
# ---------------------------------------------------------------------------

#: 在 Node 里用 **DOM 桩**把页面的内联脚本跑起来（真 shim + 真 wasm），
#: 点「运行」，看输出区拿到什么。
#:
#: 为什么值得写：只断言「HTML 里有 `JishiWasm.load` 这个串」是糊不住错的 ——
#: 页面的渲染逻辑、错误分支、事件绑定全都不在字符串里。这一条才是「页面能用」
#: 的判据。
_PAGE_HARNESS = r"""
const fs = require('fs');
const [shimPath, wasmPath, pagePath, srcsPath] = process.argv.slice(2);

const html = fs.readFileSync(pagePath, 'utf8');
const m = html.match(/<script id="jishi-page">([\s\S]*?)<\/script>/);
if (!m) { console.error('页面里找不到 <script id="jishi-page">'); process.exit(2); }

// ---- DOM 桩：页面只用到 getElementById + textContent/value/disabled/className/addEventListener
const els = {};
function mk(id) {
  return {
    id: id, textContent: '', value: '', disabled: false, className: '',
    _h: {},
    addEventListener(t, f) { (this._h[t] = this._h[t] || []).push(f); },
    fire(t) { (this._h[t] || []).forEach(f => f({ preventDefault() {} })); },
  };
}
['out', 'run', 'clear', 'status', 'code'].forEach(id => { els[id] = mk(id); });
els.run.disabled = true;              // 页面里这个按钮初始是 disabled（等引擎）
els.status.textContent = '正在加载引擎…';

globalThis.window = globalThis;
globalThis.document = { getElementById(id) { return els[id] || (els[id] = mk(id)); } };
globalThis.JishiWasm = require(shimPath);
// fetch 桩：不管 URL 是什么，都从磁盘读那份 wasm（Node 里没有同源策略）
globalThis.fetch = () => Promise.resolve({
  ok: true, arrayBuffer: () => Promise.resolve(fs.readFileSync(wasmPath)),
});

(new Function(m[1]))();               // 跑页面脚本

function ready() {
  return new Promise((res, rej) => {
    const t0 = Date.now();
    (function tick() {
      if (els.run.disabled === false) return res();
      if (/失败/.test(els.status.textContent)) return rej(new Error('引擎加载失败：' + els.status.textContent));
      if (Date.now() - t0 > 30000) return rej(new Error('等引擎就绪超时：' + els.status.textContent));
      setTimeout(tick, 20);
    })();
  });
}

(async () => {
  await ready();
  const readyStatus = els.status.textContent;   // 跑之前的状态（跑完会被覆盖）
  const srcs = JSON.parse(fs.readFileSync(srcsPath, 'utf8'));
  const out = [];
  for (const s of srcs) {
    els.code.value = s;
    els.run.fire('click');
    out.push({ text: els.out.textContent, cls: els.out.className });
  }
  process.stdout.write(JSON.stringify({ readyStatus, out }));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def _page(sources, tmp, page):
    """在 Node + DOM 桩里跑页面，返回 `{status, out:[{text, cls}]}`。"""
    tmp = Path(tmp)
    (tmp / f"page_{TMP_SUFFIX}.js").write_text(_PAGE_HARNESS, encoding="utf-8")
    (tmp / f"pagesrc_{TMP_SUFFIX}.json").write_text(
        json.dumps(sources, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run(
        ["node", str(tmp / f"page_{TMP_SUFFIX}.js"), str(SHIM), str(WASM),
         str(page), str(tmp / f"pagesrc_{TMP_SUFFIX}.json")],
        capture_output=True, cwd=str(ROOT), timeout=180)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:1500]
    return json.loads(r.stdout.decode("utf-8"))


@need_wasm
@need_node
def test_页面_在Node里真跑一遍(tmp_path, monkeypatch):
    """R8.2 的端到端判据：**生成出来的页面**点「运行」真能出结果。

    三条：预置示例（写死期望，不 import oracle.jishi）/ 语法错（码 + 行列 + 出错样式）/
    运行期错（码 + 标题）。
    """
    import build_playground

    page = tmp_path / "playground.html"
    monkeypatch.setattr(build_playground, "OUT", page)
    build_playground.build()

    got = _page([build_playground.SAMPLE, "打印(甲 +)\n", "打印(1 / 0)\n"],
                tmp_path, page)
    assert got["readyStatus"] == "就绪", got["readyStatus"]

    # ① 预置示例：写死期望的**输出文本**（不是「有没有报错」）
    one = got["out"][0]
    assert one["cls"] == "", one
    assert "你好，基石！" in one["text"], one
    assert "前五个平方：[1, 4, 9, 16, 25]" in one["text"], one
    assert "斐波那契第 10 项：55" in one["text"], one

    # ② 语法错：错误码 + 行列都要在，而且是「出错」样式
    two = got["out"][1]
    assert "E0201" in two["text"], two
    assert "第 1 行第 7 列" in two["text"], two
    assert two["cls"] == "err", two

    # ③ 运行期错
    three = got["out"][2]
    assert "E2001" in three["text"] and "不能除以零" in three["text"], three
    assert three["cls"] == "err", three
