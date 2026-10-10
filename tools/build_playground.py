# -*- coding: utf-8 -*-
"""生成浏览器试玩页（Playground）。

**R8.2（2026-10-07）**：引擎从 **Pyodide（浏览器里的 Python 实现）**换成
**R8.1 做好的 wasm** —— 那才是基石本体（Rust 前端 + 字节码 VM + 原生标准库）
编译到 `wasm32-unknown-unknown` 的产物。

为什么值得换：

* 页面从 **432 KB** 缩到 **~10 KB**：以前要把 8 个 Python 模块的源码整个内嵌进
  HTML，现在只有一段胶水。
* 不再从 CDN 拉 **Pyodide（约 10 MB）**：只 fetch 本站那份 **1.47 MB** 的 .wasm，
  而且浏览器会缓存它。
* 跑的是**真引擎**，与 `jishi-rs` / 编辑器 / agent 是同一份代码 —— 试玩页的行为
  不再是「另一条实现路径」。

运行：`python tools/build_playground.py` → `site/playground.html`

⚠️ 本脚本**只产出 HTML**。另外两个文件是它的运行时依赖，由别处负责落到 `site/`：

| 文件 | 谁放到 `site/` | 是什么 |
|---|---|---|
| `jishi-wasm.js` | `tools/build_wasm.py` / `tools/build_site.py` | 手写胶水（**源码**在 `wasm/jishi-wasm.js`） |
| `jishi_wasm.wasm` | `tools/build_wasm.py`（需 cargo + wasm 目标） | 引擎本体（**构建产物**） |

本脚本**只检查、不替人构建**（构建要 cargo，不该藏在页面生成里），缺了就明确报警。
"""

from __future__ import annotations

import html as _html
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "site" / "playground.html"

#: 胶水（源码那份）与引擎产物（构建那份）—— 都落在 `site/`。
SHIM = ROOT / "wasm" / "jishi-wasm.js"
WASM = ROOT / "site" / "jishi_wasm.wasm"

#: 预置的示例程序：**每一条都在本机对着宿主沙箱跑过**（`--stdin --sandbox`）。
#: 里面故意覆盖了三处容易出错的语法：插值字符串（反引号）、推导式、递归。
SAMPLE = """令 名字 = "基石"
打印(`你好，{名字}！`)

令 平方 = [x * x 遍历 x 在 [1, 2, 3, 4, 5]]
打印(`前五个平方：{平方}`)

函数 斐(数)：
    如果 数 < 2：
        返回 数
    返回 斐(数 - 1) + 斐(数 - 2)
打印(`斐波那契第 10 项：{斐(10)}`)
"""

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>基石 Playground —— 浏览器里跑中文代码</title>
<style>
  :root { --bg:#f7f7f8; --card:#fff; --fg:#1f2328; --muted:#656d76;
          --accent:#0969da; --border:#d0d7de; --err:#cf222e;
          --mono:ui-monospace,SFMono-Regular,Consolas,monospace; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
         background:var(--bg); color:var(--fg); }
  header { background:var(--card); border-bottom:1px solid var(--border);
           padding:16px 24px; }
  header h1 { margin:0; font-size:20px; }
  header p { margin:4px 0 0; color:var(--muted); font-size:13px; }
  .wrap { max-width:980px; margin:20px auto; padding:0 16px; }
  textarea { width:100%; height:220px; font-family:var(--mono); font-size:14px;
             padding:12px; border:1px solid var(--border); border-radius:8px;
             resize:vertical; background:var(--card); color:var(--fg); }
  .bar { margin:12px 0; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  button { background:var(--accent); color:#fff; border:none; padding:8px 18px;
           border-radius:6px; cursor:pointer; font-size:14px; }
  button:disabled { opacity:.5; cursor:default; }
  button.ghost { background:transparent; color:var(--muted);
                 border:1px solid var(--border); }
  #status { color:var(--muted); font-size:13px; }
  pre { background:#0d1117; color:#c9d1d9; padding:14px; border-radius:8px;
        min-height:80px; font-family:var(--mono); font-size:13px;
        white-space:pre-wrap; word-break:break-word; margin:0; }
  pre.err { color:#ff7b72; }
  .hint { color:var(--muted); font-size:12px; margin-top:8px; line-height:1.7; }
  .pg-foot { border-top:1px solid var(--border); margin-top:26px; padding:16px 0 30px;
             display:flex; gap:16px; flex-wrap:wrap; align-items:center;
             justify-content:center; font-size:12.5px; color:var(--muted); }
  .pg-foot a { color:var(--muted); display:inline-flex; align-items:center; gap:5px;
               text-decoration:none; }
  .pg-foot a:hover { text-decoration:underline; }
  .pg-foot img { height:16px; width:auto; display:block; }
  code { font-family:var(--mono); background:rgba(127,127,127,.14);
         padding:1px 4px; border-radius:4px; }
</style>
</head>
<body>
<header>
  <h1>基石 Playground</h1>
  <p>在浏览器里直接运行基石（中文编程语言）代码 —— 用中文关键字，别用英文。</p>
</header>
<div class="wrap">
  <textarea id="code" spellcheck="false">__SAMPLE__</textarea>
  <div class="bar">
    <button id="run" disabled>运行（加载中…）</button>
    <button id="clear" class="ghost">清空输出</button>
    <span id="status">正在加载引擎…</span>
  </div>
  <pre id="out"></pre>
  <div class="hint">
    提示：这里的引擎是 <b>编译成 WebAssembly 的基石本体</b>（与命令行
    <code>jishi</code>、编辑器插件同一份代码：Rust 前端 + 字节码 VM + 原生标准库），
    不是另一套简化实现。<br>
    首次加载约 <b>1.5 MB</b>，之后由浏览器缓存；死循环会被步数预算拦下，
    不会冻住页面。快捷键：<code>Ctrl↵</code> 运行。
  </div>
  <footer class="pg-foot">
    <a href="__ICP_URL__" target="_blank" rel="noopener noreferrer">__ICP__</a>
    <a href="__POLICE_URL__" target="_blank" rel="noreferrer"><img src="assets/备案图标.png" alt="公安备案图标">__POLICE__</a>
  </footer>
</div>

<script src="jishi-wasm.js"></script>
<script id="jishi-page">
(function () {
  "use strict";
  var outEl = document.getElementById("out");
  var runBtn = document.getElementById("run");
  var clearBtn = document.getElementById("clear");
  var statusEl = document.getElementById("status");
  var codeEl = document.getElementById("code");

  // 测试/自托管可以把 wasm 指到别处；默认与页面同目录。
  var WASM_URL = (typeof window !== "undefined" && window.JISHI_WASM_URL)
    || "jishi_wasm.wasm";

  var engine = null;

  /** 把引擎给的结果对象渲染成一段给人看的文本。
   *
   * 形状就是原生 `jishi-rs --sandbox --json-result` 的那份协议：
   *   成功 `{ok:true, value, stdout, duration_ms}`
   *   失败 `{ok:false, error:{code,title,message,line,col,hint}}`
   * ⚠️ 胶水自己兜的两种意外（wasm trap / 返回不是 JSON）**也是同一形状** ——
   * 所以这里只认一个形状，不再到处判分支。
   */
  function render(r) {
    if (r && r.ok) {
      var text = (r.stdout == null ? "" : String(r.stdout));
      if (r.value !== null && r.value !== undefined) {
        if (text && text.charAt(text.length - 1) !== "\\n") text += "\\n";
        text += "→ " + JSON.stringify(r.value);
      }
      return text === "" ? "（程序没有输出）" : text;
    }
    var e = (r && r.error) || {};
    var head = "【" + (e.code ? e.code + " " : "") + (e.title || "出错了") + "】";
    var lines = [head];
    if (e.message) lines.push(e.message);
    if (e.line) {
      lines.push("位置：第 " + e.line + " 行"
                 + (e.col ? "第 " + e.col + " 列" : ""));
    }
    if (e.hint) lines.push("提示：" + e.hint);
    return lines.join("\\n");
  }

  function show(text, isError) {
    outEl.textContent = text;
    outEl.className = isError ? "err" : "";
  }

  function run() {
    if (!engine) return;
    var r = engine.run(codeEl.value);
    var extra = (typeof r.durationMs === "number")
      ? "（" + r.durationMs.toFixed(1) + " ms）" : "";
    show(render(r), !r.ok);
    statusEl.textContent = r.ok ? "运行完成 " + extra : "运行出错 " + extra;
  }

  // 引擎加载：失败要说清楚是什么、怎么办，别让按钮永远灰着。
  if (typeof JishiWasm === "undefined") {
    statusEl.textContent = "引擎胶水没加载上";
    show("找不到 jishi-wasm.js —— 它与本页必须在同一个目录下。", true);
  } else {
    JishiWasm.load(WASM_URL).then(function (inst) {
      engine = inst;
      runBtn.disabled = false;
      runBtn.textContent = "运行";
      statusEl.textContent = "就绪";
    }).catch(function (err) {
      var why = String((err && err.message) || err);
      // 最常见的两种：wasm 没部署（404）、或者用 file:// 直接打开被 CORS 挡了。
      statusEl.textContent = "引擎加载失败";
      show("加载 jishi_wasm.wasm 失败：" + why
           + "\\n如果是从本地磁盘直接双击打开的，请改用本地服务器"
           + "（例如 python -m http.server），file:// 下浏览器不允许读 wasm。",
           true);
    });
  }

  runBtn.addEventListener("click", run);
  clearBtn.addEventListener("click", function () { show("", false); });
  codeEl.addEventListener("keydown", function (ev) {
    if ((ev.ctrlKey || ev.metaKey) && (ev.key === "Enter" || ev.keyCode === 13)) {
      ev.preventDefault();
      run();
    }
  });
})();
</script>
</body>
</html>
"""


def build() -> None:
    """把模板写进 `OUT`（模块级变量，测试可 monkeypatch）。"""
    #: 备案信息与官网页脚**共用一处**（法定要求：主页最下方展示）。
    from build_home import ICP_BEIAN, ICP_URL, POLICE_BEIAN, POLICE_URL
    page = (HTML_TEMPLATE
            .replace("__SAMPLE__", _html.escape(SAMPLE, quote=False))
            .replace("__ICP_URL__", ICP_URL)
            .replace("__ICP__", _html.escape(ICP_BEIAN))
            .replace("__POLICE_URL__", POLICE_URL)
            .replace("__POLICE__", _html.escape(POLICE_BEIAN)))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page, encoding="utf-8")
    try:
        shown = OUT.relative_to(ROOT)       # 测试会把 OUT 指到别处
    except ValueError:
        shown = OUT
    print(f"生成 {shown}（{len(page):,} 字节，wasm 引擎）")
    if not SHIM.exists():
        print(f"⚠️ 缺 {SHIM.relative_to(ROOT)} —— 页面跑不起来。"
              "它是源码，应随仓库一起在。")
    if not WASM.exists():
        print("⚠️ 缺 site/jishi_wasm.wasm —— playground 跑不起来。"
              "先跑：python tools/build_wasm.py（需要 cargo 与 wasm32 目标）")


if __name__ == "__main__":
    build()
