/*!
 * M188：三个示例共用的「启动器」—— 页面里只写一行 `<script src="游戏启动.js">`。
 *
 * 它做三件事，**每个页面都一样**：
 *   1. 找到 wasm 引擎（两种宿主两个路径，按顺序试）；
 *   2. `fetch` 页面指定的那份游戏源码（`<body data-游戏="xxx.jsh">`）；
 *   3. 交给引擎跑；出错就把错误**显示在页面上**（`#提示`）。
 *
 * 🔴 **为什么抽出来**：M188 写第一个页面时踩了「动态 import 少写 `./`、
 *    被当成裸模块名」的坑 —— 三个页面各抄一份，这种错就得改三处，而且新加的
 *    那个一定忘。**启动逻辑只写一遍。**
 *
 * 页面约定（缺了就会在页面上看到一句明确的中文错，不是白屏）：
 *   · `<body data-游戏="打砖块_游戏.jsh">` —— 要跑哪份源码（相对本页面）
 *   · `<div id="提示"></div>` —— 出错时显示在这儿
 *   · 可选 `<body data-标题="打砖块">` —— 只用于报错文案
 *
 * ⚠️ 这个文件是**普通脚本**（不是 module）：里面用动态 `import()`，
 *    这样两种宿主都能走「先试绝对路径、再试同级」。
 */
(function () {
  "use strict";

  /** 给原生窗口（wry）报一句进度：宿主在 JISHI_WINDOW_DEBUG=1 下会打日志，
   *  判据靠它区分「页面没跑」和「引擎没起来」。官网静态站没有 ipc，自动跳过。 */
  function 报给宿主(什么) {
    if (window.ipc && window.ipc.postMessage) {
      window.ipc.postMessage(JSON.stringify({ op: 什么 }));
    }
  }

  /** 载入引擎。两代宿主两个路径：
   *   · 原生窗口：`/__jishi/…`（`窗口.打开` 起的服务挂的挂载点）
   *   · 官网静态站：与页面同级
   *  连 **wasm 也要跟着同一个前缀**（窗口里页面目录下没有 jishi_wasm.wasm）。
   *  🔴 同级那条**必须带 `./`**：不带就是裸模块名，浏览器直接报
   *     `Failed to resolve module specifier`（M188 截图验的时候踩到）。 */
  async function 载引擎() {
    const 候选 = [
      { 胶水: "/__jishi/jishi-wasm.js", 引擎: "/__jishi/jishi_wasm.wasm" },
      { 胶水: "./jishi-wasm.js", 引擎: "./jishi_wasm.wasm" },
    ];
    let 最后错 = null;
    for (const 套 of 候选) {
      try {
        const m = await import(套.胶水);
        const J = m.default || m.JishiWasm || window.JishiWasm;
        if (J) return { J, 引擎路径: 套.引擎 };
      } catch (e) { 最后错 = e; }
    }
    throw 最后错 || new Error("找不到 jishi-wasm.js");
  }

  function 显示错误(文) {
    const 框 = document.getElementById("提示");
    if (!框) {
      // 页面漏了 `#提示`：至少别让它静默 —— 打到控制台
      try { console.error("[基石] " + 文); } catch (_) {}
      return;
    }
    框.style.display = "block";
    框.textContent = 文;
  }

  (async () => {
    const 体 = document.body;
    const 源码名 = 体.getAttribute("data-游戏") || "";
    const 标题 = 体.getAttribute("data-标题") || "这个示例";
    报给宿主("页面已加载");

    if (!源码名) {
      显示错误("这个页面没写 `data-游戏`（要跑哪份 .jsh）—— 页面写错了，不是引擎的问题。");
      报给宿主("引擎失败");
      return;
    }

    try {
      const { J, 引擎路径 } = await 载引擎();
      const 引擎 = await J.load(引擎路径);
      const 响应 = await fetch(源码名);
      if (!响应.ok) throw new Error(`取不到 ${源码名}（HTTP ${响应.status}）`);
      const 源码 = await 响应.text();
      const r = 引擎.run(源码);
      if (!r.ok) {
        报给宿主("引擎失败");
        const e = r.error || {};
        显示错误("基石报错：" + (e.title || "出错") + " —— " + (e.message || "")
          + (e.line ? "（第 " + e.line + " 行）" : "")
          + (e.hint ? "\n" + e.hint : ""));
        return;
      }
      报给宿主("引擎就绪");
    } catch (e) {
      报给宿主("引擎失败");
      显示错误("加载失败：" + ((e && e.message) || e)
        + "\n要从命令行打开（`jishi examples/desktop/" + 标题
        + ".jsh`，它自带一个本机小服务），或者把它放在站点上；"
        + "直接双击 .html 是不行的（`file://` 下既不能 fetch 相邻文件，也读不了 wasm）。");
    }
  })();
})();
