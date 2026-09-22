# -*- coding: utf-8 -*-
"""生成浏览器试玩页（Playground，M10.3）。

把基石「树遍历解释器」的纯 Python 源码内嵌进一个自包含 HTML，
用 pyodide（WASM Python）在浏览器内直接跑基石代码（不依赖 C VM）。
运行：python tools/build_playground.py  →  生成 site/playground.html
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "site" / "playground.html"

# 树遍历解释器依赖的纯 Python 模块（不含 C 扩展）
MODULES = [
    "jishi/__init__.py",
    "jishi/ast_nodes.py",
    "jishi/errors.py",
    "jishi/tokenizer.py",
    "jishi/parser.py",
    "jishi/runtime.py",
    "jishi/interpreter.py",
    "jishi/stdlib/__init__.py",
]


def _collect() -> dict[str, str]:
    """收集模块源码：路径（FS 内相对 /home/pyodide）→ 源码。"""
    src: dict[str, str] = {}
    for rel in MODULES:
        p = ROOT / rel
        if p.exists():
            src[rel] = p.read_text(encoding="utf-8")
    src["jishi/__init__.py"] = "# 基石 —— 中文编程语言\n"
    # 标准库模块
    stdlib = ROOT / "jishi" / "stdlib"
    for p in sorted(stdlib.glob("*.py")):
        src[f"jishi/stdlib/{p.name}"] = p.read_text(encoding="utf-8")
    return src


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>基石 Playground —— 浏览器里跑中文代码</title>
<script src="https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.js"></script>
<style>
  :root { --bg:#f7f7f8; --card:#fff; --fg:#1f2328; --muted:#656d76;
          --accent:#0969da; --border:#d0d7de; --mono:ui-monospace,SFMono-Regular,Consolas,monospace; }
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
  .bar { margin:12px 0; display:flex; gap:8px; align-items:center; }
  button { background:var(--accent); color:#fff; border:none; padding:8px 18px;
           border-radius:6px; cursor:pointer; font-size:14px; }
  button:disabled { opacity:.5; cursor:default; }
  #status { color:var(--muted); font-size:13px; }
  pre { background:#0d1117; color:#c9d1d9; padding:14px; border-radius:8px;
        min-height:80px; font-family:var(--mono); font-size:13px;
        white-space:pre-wrap; word-break:break-all; margin:0; }
  .hint { color:var(--muted); font-size:12px; margin-top:8px; }
</style>
</head>
<body>
<header>
  <h1>基石 Playground</h1>
  <p>在浏览器里直接运行基石（中文编程语言）代码 —— 用中文关键字，别用英文。</p>
</header>
<div class="wrap">
  <textarea id="code">令 名字 = "基石"
打印(`你好，{名字}！`)

令 平方 = [x * x 遍历 x 在 [1, 2, 3, 4, 5]]
打印(`前五个平方：{平方}`)
</textarea>
  <div class="bar">
    <button id="run" disabled>运行（加载中…）</button>
    <span id="status">正在加载 pyodide…</span>
  </div>
  <pre id="out"></pre>
  <div class="hint">提示：Pyodide 首次加载需联网（约 10MB）。这里跑的是树遍历解释器，
  支持基石全部语法与标准库，不含 C VM。</div>
</div>

<script>
const MODULES = __MODULES__;

async function init() {
  const pyodide = await loadPyodide();
  for (const [path, code] of Object.entries(MODULES)) {
    const parts = path.split("/");
    let dir = "/home/pyodide";
    for (let i = 0; i < parts.length - 1; i++) {
      dir += "/" + parts[i];
      try { pyodide.FS.mkdir(dir); } catch (e) { /* 已存在 */ }
    }
    pyodide.FS.writeFile("/home/pyodide/" + path, code);
  }
  document.getElementById("run").disabled = false;
  document.getElementById("status").textContent = "就绪，可以运行了";
  return pyodide;
}

async function main() {
  let pyodide;
  try {
    pyodide = await init();
  } catch (e) {
    document.getElementById("status").textContent = "加载失败：" + e;
    document.getElementById("out").textContent = "无法加载 pyodide（需联网）。错误：" + e;
    return;
  }
  const runBtn = document.getElementById("run");
  const outEl = document.getElementById("out");
  runBtn.addEventListener("click", async () => {
    const code = document.getElementById("code").value;
    pyodide.globals.set("__jishi_code", code);
    try {
      const result = pyodide.runPython(`
import sys
sys.path.insert(0, "/home/pyodide")
from jishi.interpreter import run_source
import io
from contextlib import redirect_stdout
_buf = io.StringIO()
with redirect_stdout(_buf):
    run_source(__jishi_code, "<试玩>")
_buf.getvalue()
`);
      outEl.textContent = result;
    } catch (e) {
      outEl.textContent = String(e);
    }
  });
}
main();
</script>
</body>
</html>
"""


def build() -> None:
    modules = _collect()
    html = HTML_TEMPLATE.replace(
        "__MODULES__", json.dumps(modules, ensure_ascii=False))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"生成 {OUT}（内嵌 {len(modules)} 个模块）")


if __name__ == "__main__":
    build()
