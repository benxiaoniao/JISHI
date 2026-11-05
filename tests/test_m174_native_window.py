# -*- coding: utf-8 -*-
"""M174：原生窗口（wry）+ ABI 3。

## 这一站的两个东西

1. **原生窗口**（`rust/src/native_window.rs`）：`窗口.打开({原生: 真})` 开的是
   wry 的真窗口（系统 WebView），不再是浏览器 `--app=`。能改标题、关窗由我们说了算。
2. **ABI 3**（`rust/src/abi3.rs` + `wasm/jishi-wasm.js`）：页面里的基石代码
   （跑在 wasm 里）→ `jishi_native` 导入 → shim → 宿主。**单向**命令：
   `窗口.设标题` / `窗口.关闭()`。

## 判据怎么做到「不靠人点」

- **真开窗口的那几条**会真的弹一个窗（几百毫秒），页面自己发 IPC 关掉。
  这种判据在没有桌面的环境（CI）上必然失败 ⇒ 用 `JISHI_NO_GUI=1` 显式跳过。
  ⚠️ **跳过不是通过**：CI 上这些能力是**没验**的，如实记在 CHANGELOG 里。
- **ABI 3 那几条**用 Node + 真 wasm + 假 DOM + 假 `ipc`：不弹窗，但走的是
  与浏览器**完全相同**的那条链（`jishi_native` → shim `_native`）。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import rust_exe_path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WASM = ROOT / "site" / "jishi_wasm.wasm"
SHIM = ROOT / "wasm" / "jishi-wasm.js"

#: 没有桌面就跳过「真开窗口」的那几条。CI 里设 `JISHI_NO_GUI=1`。
无界面 = bool(os.environ.get("JISHI_NO_GUI"))

需要原生 = pytest.mark.skipif(
    无界面 or not sys.platform.startswith("win"),
    reason="真开原生窗口要桌面环境（CI 设 JISHI_NO_GUI=1）",
)


def _跑(cmd, cwd=None, timeout=300, 环境=None):
    env = dict(os.environ)
    if 环境:
        env.update(环境)
    r = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                       capture_output=True, timeout=timeout, input=b"", env=env)
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def _跑基石(源, tmp_path, exe=None, 环境=None, 期望=0, timeout=300):
    (tmp_path / "程序.jsh").write_text(源, encoding="utf-8", newline="\n")
    命令 = ([str(exe)] if exe else [sys.executable, "-m", "oracle.jishi.cli"])
    码, 出 = _跑(命令 + ["程序.jsh"], cwd=tmp_path, 环境=环境, timeout=timeout)
    assert 码 == 期望, f"退出码 {码}（要 {期望}）：{出[:800]}"
    return 出


def _自动关页(tmp_path, 等待毫秒=400):
    """一个「自己关自己」的页面：过一会儿发 IPC 改标题 + 关窗。

    这样判据不用人工点关闭 —— 窗口存在几百毫秒，然后就没了。
    """
    p = tmp_path / "自动关.html"
    p.write_text(f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>原生窗口自检</title></head><body><h1>自检</h1>
<script>
setTimeout(function () {{
  window.ipc.postMessage(JSON.stringify({{ op: "标题", arg: "已改名" }}));
  setTimeout(function () {{
    window.ipc.postMessage(JSON.stringify({{ op: "关闭" }}));
  }}, 200);
}}, {等待毫秒});
</script></body></html>
""", encoding="utf-8", newline="\n")
    return p


# ---------------------------------------------------------------------------
# 一、结构：wry 必须是「可选特性」，wasm 必须关掉它
# ---------------------------------------------------------------------------

def test_原生窗口是可选特性():
    """wry / tao **必须**是 optional + 走 feature。

    不这么写，wasm 构建（`default-features = false`）会把 wry 一起拖进来 ——
    那边根本链不了 WebView，整条 `build_wasm.py` 直接红。
    """
    toml = (ROOT / "rust" / "Cargo.toml").read_text(encoding="utf-8")
    for 依赖 in ("wry", "tao"):
        行 = [ln for ln in toml.splitlines() if ln.startswith(f"{依赖} = ")]
        assert 行, f"Cargo.toml 里没有 `{依赖}` 依赖"
        assert "optional = true" in 行[0], f"`{依赖}` 不是可选依赖：{行[0]}"
    assert "native-window = [" in toml, "没有 `native-window` 特性"
    assert 'dep:wry' in toml and 'dep:tao' in toml, \
        "`native-window` 特性没有把 wry/tao 挂进去（缺 `dep:wry` / `dep:tao`）"


def test_wasm构建关掉了原生窗口():
    """`wasm/Cargo.toml` 必须 `default-features = false`。

    否则 `native-window`（在 default 里）会被打开，wry 进 wasm ⇒ 编不过。
    """
    toml = (ROOT / "wasm" / "Cargo.toml").read_text(encoding="utf-8")
    assert "default-features = false" in toml, \
        "wasm 侧没关默认特性：`native-window` 会把 wry 拖进 wasm"


def test_原生窗口模块被cfg挡住():
    """`native_window` 模块必须 `#[cfg(feature = "native-window")]`。

    漏了 cfg ⇒ `--no-default-features` 的构建（也就是 wasm）直接编不过。
    """
    lib = (ROOT / "rust" / "src" / "lib.rs").read_text(encoding="utf-8")
    行们 = lib.splitlines()
    位 = [i for i, ln in enumerate(行们) if "pub mod native_window" in ln]
    assert 位, "lib.rs 里没注册 native_window"
    # 往前找最近的 cfg（中间只许有注释行）
    上文 = []
    for ln in reversed(行们[:位[0]]):
        if ln.strip().startswith("//") or not ln.strip():
            上文.append(ln)
            continue
        上文.append(ln)
        break
    assert any('cfg(feature = "native-window")' in ln for ln in 上文), \
        "`pub mod native_window;` 上方没有 `#[cfg(feature = \"native-window\")]`"


# ---------------------------------------------------------------------------
# 二、ABI 3：版本号与 shim 的一致性
# ---------------------------------------------------------------------------

def test_ABI版本三处一致():
    """shim 的 `ABI_VERSION`、`wasm/src/lib.rs` 的常量、模块文档三者要对得上。

    对不上的后果**不是报错**而是静默错位：shim 按旧协议读内存，wasm 按新协议写。
    """
    shim = SHIM.read_text(encoding="utf-8")
    行 = [ln for ln in shim.splitlines() if "var ABI_VERSION" in ln]
    assert 行, "shim 里没有 ABI_VERSION"
    shim版本 = int(行[0].split("=")[1].split(";")[0].strip())

    rs = (ROOT / "wasm" / "src" / "lib.rs").read_text(encoding="utf-8")
    行2 = [ln for ln in rs.splitlines() if "const ABI_VERSION" in ln]
    assert 行2, "wasm/src/lib.rs 里没有 ABI_VERSION"
    rs版本 = int(行2[0].split("=")[1].split(";")[0].strip())

    assert shim版本 == rs版本 == 3, (
        f"ABI 版本不一致：shim={shim版本} rust={rs版本}（M174 应当是 3）")


def test_shim提供了jishi_native():
    """wasm 那边 `#[link(wasm_import_module = "jishi")]` 导入了 `jishi_native`。

    shim 少给这一项 ⇒ **实例化直接失败**（不是运行时才炸），所以这条要钉住。
    而且必须挂在 `jishi` 模块名下 —— 挂到 `env` 上同样找不到。
    """
    shim = SHIM.read_text(encoding="utf-8")
    assert "jishi_native: function" in shim, "shim 没提供 `jishi_native`（wasm 会实例化失败）"
    # 它必须在 `jishi: { ... }` 这个对象里
    ipc位置 = shim.index("jishi_host_call: function")
    native位置 = shim.index("jishi_native: function")
    assert native位置 > ipc位置, "`jishi_native` 跑到 imports 对象外面去了"


# ---------------------------------------------------------------------------
# 三、ABI 3 真跑：Node + 真 wasm + 假 DOM + 假 ipc
# ---------------------------------------------------------------------------

_探针 = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const 假页 = { title: "(初始)" };
const 收到 = [];
globalThis.ipc = { postMessage: (m) => 收到.push(m) };
(async () => {
  // ⚠️ 传**字节**而不是路径：shim 拿到字符串会走 fetch，Node 的 fetch 不认 D:\ 这种 scheme
  const j = await JishiWasm.load(fs.readFileSync(process.argv[3]), { doc: 假页 });
  const r = j.run(fs.readFileSync(process.argv[4], "utf8"));
  console.log(JSON.stringify({
    ok: r.ok,
    stdout: r.stdout,
    错误: r.ok ? null : (r.error.title + "/" + r.error.message),
    页面标题: 假页.title,
    ipc: 收到,
  }));
})();
'''


def _在wasm里跑(源, tmp_path):
    """在 Node + 假 DOM 上跑真 wasm 引擎。返回解析后的结果字典。"""
    (tmp_path / "探.js").write_text(_探针, encoding="utf-8", newline="\n")
    (tmp_path / "源.jsh").write_text(源, encoding="utf-8", newline="\n")
    node = "node"
    if not WASM.is_file():
        pytest.skip("还没构建 wasm（跑 tools/build_wasm.py）")
    码, 出 = _跑([node, tmp_path / "探.js", SHIM, WASM, tmp_path / "源.jsh"], timeout=180)
    assert 码 == 0, f"探针跑挂了：{出[:600]}"
    行 = [ln for ln in 出.splitlines() if ln.strip().startswith("{")]
    assert 行, f"探针没有输出 JSON：{出[:600]}"
    return json.loads(行[-1])


def test_wasm里设标题真的改了页面标题并发了IPC(tmp_path):
    """`窗口.设标题` 在页面里：改 `document.title` + 发一条 IPC。

    两件事都要做 —— 应用模式的窗口标题就是 `document.title`，原生窗口靠 IPC。
    少做哪一半，就有一代外壳不生效。
    """
    结果 = _在wasm里跑('导入 窗口\n打印(窗口.设标题("新的标题"))\n', tmp_path)
    assert 结果["ok"], 结果["错误"]
    assert 结果["stdout"].strip() == "真", 结果["stdout"]
    assert 结果["页面标题"] == "新的标题", f"document.title 没改：{结果['页面标题']}"
    消息 = [json.loads(m) for m in 结果["ipc"]]
    assert {"op": "标题", "arg": "新的标题"} in 消息, f"没发改标题的 IPC：{消息}"


def test_wasm里关闭不带句柄是IPC(tmp_path):
    """页面里 `窗口.关闭()` = 请宿主关窗（那条路径只有页面里成立）。"""
    结果 = _在wasm里跑('导入 窗口\n打印(窗口.关闭())\n', tmp_path)
    assert 结果["ok"], 结果["错误"]
    assert 结果["stdout"].strip() == "真", 结果["stdout"]
    assert {"op": "关闭"} in [json.loads(m) for m in 结果["ipc"]], 结果["ipc"]


def test_wasm里开窗口要明确报错(tmp_path):
    """页面里不能再开窗口 —— 而且要说人话，不是「起服务失败」。"""
    结果 = _在wasm里跑('导入 窗口\n窗口.打开({"页面": "x.html"})\n', tmp_path)
    assert not 结果["ok"], "页面里开窗口居然成功了"
    assert "已经在窗口里" in 结果["错误"], 结果["错误"]


def test_wasm里带句柄的关闭要明确报错(tmp_path):
    """页面里没有窗口句柄这回事 —— 传个数字进去要报错，不是静默关失败。"""
    结果 = _在wasm里跑('导入 窗口\n窗口.关闭(1)\n', tmp_path)
    assert not 结果["ok"], "页面里 `窗口.关闭(1)` 居然成功了"
    assert "窗口" in 结果["错误"], 结果["错误"]


# ---------------------------------------------------------------------------
# 三·四、引擎素材：发行包必须带，缺了要**明确报错**（不能白窗口）
# ---------------------------------------------------------------------------

_缺素材 = "找不到 wasm 引擎素材（jishi-wasm.js + jishi_wasm.wasm）"


def test_缺引擎素材时两个宿主都给同一句中文报错():
    """素材缺失时必须在**开窗之前**报错。

    🔴 这是 M174 抓出来的产品 bug（M173 起就存在）：`窗口` 打开页面时要把
    `jishi-wasm.js` + `jishi_wasm.wasm` 喂给浏览器/WebView，而**发行包没带**
    这两份 ⇒ 页面里的 `import "/__jishi/…"` 全部 404 ⇒ **白窗口、一行报错都没有**。

    修了两处：
    1. `build_release.py` 现编 wasm 并放进 `bin/`（和 jishi 可执行文件同目录，
       也正是 `窗口` 找素材的第一站）；
    2. 两宿主的 `打开` 里，**起服务之前**就检查素材。

    这条判据钉第 2 点，而且要求两边**文案逐字相同**（用户不该看到两个版本的话）。
    """
    py = (ROOT / "oracle" / "jishi" / "stdlib" / "窗口.py").read_text(encoding="utf-8")
    assert _缺素材 in py, "Python 侧的 `窗口.打开` 没在起服务前检查引擎素材"

    rs = (ROOT / "rust" / "src" / "stdlib" / "window.rs").read_text(encoding="utf-8")
    assert _缺素材 in rs, "Rust 侧的 `窗口.打开` 没在起服务前检查引擎素材"

    # 提示里要点明「和可执行文件同目录」—— 这是用户唯一能自己动手的地方
    for 源 in (py, rs):
        assert "同一目录" in 源, "提示没说素材该放哪"


def test_发行包会把引擎素材带上():
    """`build_release.py` 必须把两份素材放进 `bin/`。

    这条是纯源码判据 —— 但少了它，上面那条「明确报错」反而会把**用户挡在门外**：
    报错说「找不到素材」，可发行包本来就没带，用户没有任何办法。**两条必须成对**。
    """
    源 = (ROOT / "tools" / "build_release.py").read_text(encoding="utf-8")
    assert "_build_wasm_into" in 源, "build_release.py 没有收集 wasm 素材的步骤"
    assert "_build_wasm_into(bin_dir)" in 源, "收集步骤没有被调用"
    for 名 in ("jishi-wasm.js", "jishi_wasm.wasm"):
        assert 名 in 源, f"收集逻辑里没有 `{名}`"
    # 缺 wasm target 是最常见的原因，错误信息要能指过去
    assert "rustup target add" in 源, "构建 wasm 失败时没提示 `rustup target add`"


def test_CI会构建wasm素材():
    """CI 的两个 workflow 都得装 wasm target 并构建素材。

    `site/` 是 gitignore 的构建产物，干净检出里没有 —— 不现编的话，
    「服务能把页面和引擎素材喂出去」那条判据会 404（M173 起 CI 上一直红着）。
    """
    for 名 in ("ci.yml", "release.yml"):
        t = (ROOT / ".github" / "workflows" / 名).read_text(encoding="utf-8")
        assert "wasm32-unknown-unknown" in t, f"{名} 没装 wasm target"
        assert "tools/build_wasm.py" in t, f"{名} 没构建 wasm 素材"


# ---------------------------------------------------------------------------
# 三·五、示例页面的脚本必须真的能被解析
# ---------------------------------------------------------------------------

def test_示例页面的脚本语法是对的():
    """`examples/**/*.html` 里那段 `<script>` 必须能被 JS 解析。

    🔴 这条是 M174 抓出来的**真 bug**（M173 遗留）：计算器页面里
    模板字符串的**注释**里用了反引号（正则在说明里写不出来，见 CHANGELOG），
    于是字符串提前结束 ⇒ 整段 `<script>` 有语法错 ⇒ **一行 JS 都不执行**。

    后果不是报错，是「窗口开着、页面白着、点了没反应」——
    M173 当时的验证是「服务收到了 html / js / wasm 三个 200」，
    那只证明**资源被请求了**，没证明脚本跑起来了。所以补这条。

    做法：去掉 HTML 注释 → 取最后一个 `<script>` → 丢给 `new Function`
    （**只解析不执行**，所以不需要 DOM）。
    """
    import re

    页面们 = sorted((ROOT / "examples").rglob("*.html"))
    assert 页面们, "examples 里一个 html 都没有？"

    js = r"""
const fs = require("fs");
const 路径 = process.argv[2];
let t = fs.readFileSync(路径, "utf8");
// 去掉 HTML 注释（注释里可能提到 `<script>`，那会骗到下面的正则）
t = t.replace(/<!--[\s\S]*?-->/g, "");
const 块们 = [...t.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (!块们.length) { console.log("没有 script"); process.exit(0); }
try {
  // 只解析：`new Function` 不执行函数体
  new Function(块们[块们.length - 1]);
  console.log("ok");
} catch (e) {
  console.log("语法错：" + ((e && e.message) || e));
  process.exit(1);
}
"""
    探 = ROOT / "build" / "_m174_jsck.js"
    探.parent.mkdir(parents=True, exist_ok=True)
    探.write_text(js, encoding="utf-8", newline="\n")
    坏 = []
    try:
        for 页 in 页面们:
            码, 出 = _跑(["node", 探, 页], timeout=120)
            if 码 != 0:
                坏.append(f"{页.relative_to(ROOT)}：{出.strip()[:200]}")
    finally:
        探.unlink(missing_ok=True)
    assert not 坏, "这些页面的 <script> 有语法错（在浏览器里会一行都不执行）：\n" + "\n".join(坏)


# ---------------------------------------------------------------------------
# 四、真开窗口（要桌面）
# ---------------------------------------------------------------------------

@需要原生
def test_原生窗口能开能自动关(tmp_path):
    """开一个原生窗口，页面自己发 IPC 关掉，`等` 正常返回。

    这一条把**整条链**走了一遍：建 EventLoop → 建窗口 → 建 WebView → 加载页面
    → 页面跑 JS → IPC → 事件循环退出 → `等` 返回。
    """
    exe = rust_exe_path()
    if not exe.is_file():
        pytest.skip("没有 jishi-rs（先 cargo build --release）")
    页 = _自动关页(tmp_path)
    出 = _跑基石(f'''导入 窗口
令 窗 = 窗口.打开({{"页面": "{页.name}", "标题": "自检", "宽": 420, "高": 320, "原生": 真}})
打印("开着", 窗口.在运行(窗))
令 码 = 窗口.等(窗)
打印("关了", 窗口.在运行(窗))
''', tmp_path, exe=exe, timeout=180)
    assert "开着 真" in 出, 出[:500]
    assert "关了 假" in 出, 出[:500]


@需要原生
def test_页面改标题时系统里真的变了(tmp_path):
    """ABI 3 的闭环验证：**问系统要标题**，而不是「我们觉得改了」。

    `JISHI_WINDOW_DEBUG=1` 让宿主把 `Window::title()`（从系统查回来的那个）
    打到 stderr。断言里比的是它 —— 只看「收到了命令」是自说自话。
    """
    exe = rust_exe_path()
    if not exe.is_file():
        pytest.skip("没有 jishi-rs（先 cargo build --release）")
    页 = _自动关页(tmp_path)
    出 = _跑基石(f'''导入 窗口
令 窗 = 窗口.打开({{"页面": "{页.name}", "原生": 真}})
窗口.等(窗)
''', tmp_path, exe=exe, 环境={"JISHI_WINDOW_DEBUG": "1"}, timeout=180)
    assert "系统里现在是「已改名」" in 出, f"标题没真的变：{出[:800]}"


@需要原生
def test_原生窗口里wasm引擎真的跑起来了(tmp_path):
    """端到端：原生窗口 → 小服务 → 页面 → 真 wasm 引擎 → 回报宿主。

    为什么这条比「窗口建出来了」值钱：`窗口` 的两半（外壳 + 页面/引擎）要**一起**
    工作才算数。页面加载失败、wasm 拿不到、引擎报错 —— 都会让窗口开着但什么都不对，
    而只看 `在运行` 是看不出来的。
    """
    exe = rust_exe_path()
    if not exe.is_file():
        pytest.skip("没有 jishi-rs（先 cargo build --release）")
    页 = tmp_path / "引擎自检.html"
    # ⚠️ **必须是 raw string**：这段 HTML 里 `j.run('…\n…')` 的 `\n` 要原样留在
    #    JS 字符串里当转义。用普通 `"""…"""` 时 Python 会把它变成**真换行**，
    #    于是 JS 单引号字符串里有了换行 ⇒ 语法错误 ⇒ 整个 <script> 不执行 ⇒
    #    页面永不关窗 ⇒ 判据挂到 180 秒超时（踩过，症状是「手动跑没事、判据超时」）。
    页.write_text(r"""<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<script>
(async () => {
  function 报(op) {
    if (window.ipc && window.ipc.postMessage) {
      window.ipc.postMessage(JSON.stringify({ op: op }));
    }
  }
  try {
    const JishiWasm = (await import("/__jishi/jishi-wasm.js")).default || window.JishiWasm;
    const j = await JishiWasm.load("/__jishi/jishi_wasm.wasm");
    // ⚠️ `文本` 是内建（转字符串），要调模块函数必须先导入 —— 且要起别名，
    //    否则会把内建 `文本(...)` 遮住（M39 的老坑，这里又踩了一次）。
    const r = j.run('导入 文本 为 文字\n打印(1 + 1)\n打印(文字.重复("甲", 2))');
    报(r.ok ? "引擎就绪" : "引擎失败");
  } catch (e) {
    报("引擎失败");
  }
  setTimeout(() => 报("关闭"), 300);
})();
</script></body></html>
""", encoding="utf-8", newline="\n")
    出 = _跑基石(f'''导入 窗口
令 窗 = 窗口.打开({{"页面": "{页.name}", "原生": 真}})
窗口.等(窗)
''', tmp_path, exe=exe, 环境={"JISHI_WINDOW_DEBUG": "1"}, timeout=180)
    assert "wasm 引擎起来了" in 出, f"页面里的引擎没跑起来：{出[:900]}"
    assert "引擎失败了" not in 出, 出[:900]


@需要原生
def test_脚本侧设标题能用(tmp_path):
    """脚本里（不在页面里）也能改标题 —— 打开之后、等之前就改。"""
    exe = rust_exe_path()
    if not exe.is_file():
        pytest.skip("没有 jishi-rs（先 cargo build --release）")
    页 = _自动关页(tmp_path, 等待毫秒=600)
    出 = _跑基石(f'''导入 窗口
令 窗 = 窗口.打开({{"页面": "{页.name}", "原生": 真}})
打印("设置", 窗口.设标题("脚本改的"))
窗口.等(窗)
''', tmp_path, exe=exe, 环境={"JISHI_WINDOW_DEBUG": "1"}, timeout=180)
    assert "设置 真" in 出, 出[:500]
    # 页面后来又改了一次（`已改名`），两次都要在日志里
    assert "脚本改的" in 出 and "已改名" in 出, 出[:800]


# ---------------------------------------------------------------------------
# 五、没有原生能力的宿主：明确报错，不静默降级
# ---------------------------------------------------------------------------

def test_原生假走应用模式(tmp_path):
    """`原生: 假` 在 Rust 侧也走浏览器 —— 这是「没装 WebView2 时的退路」。"""
    假 = tmp_path / "假浏览器.py"
    假.write_text("import sys, time, pathlib\n"
                  "pathlib.Path(__file__).with_name('参数.txt').write_text("
                  "'\\n'.join(sys.argv[1:]), encoding='utf-8')\ntime.sleep(1)\n",
                  encoding="utf-8")
    (tmp_path / "页.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
    出 = _跑基石(f'''导入 窗口
导入 系统
令 窗 = 窗口.打开({{"页面": "页.html", "原生": 假,
    "浏览器": [系统.环境变量("PY"), "{假.as_posix()}"]}})
打印("开着", 窗口.在运行(窗))
窗口.等(窗)
''', tmp_path, 环境={"PY": sys.executable}, timeout=180)
    assert "开着 真" in 出, 出[:500]
    参数 = (tmp_path / "参数.txt").read_text(encoding="utf-8")
    assert "--app=" in 参数, f"没走应用模式：{参数[:200]}"


def test_没有原生能力的宿主明确报错(tmp_path):
    """Python / Node 只有应用模式：`原生: 真` 必须报错，**不许静默降级**。"""
    出 = _跑基石('导入 窗口\n窗口.打开({"页面": "x.html", "原生": 真})\n',
                 tmp_path, 期望=1)
    assert "没有原生窗口能力" in 出, 出[:500]

    # 同一个判断在 Node 宿主上也要有
    js = (ROOT / "oracle" / "node" / "vm.js").read_text(encoding="utf-8")
    段 = js[js.index("'打开': new R.Builtin('打开', (选项) => {", js.index("WINDOW_FUNCS")):
             ][:2000]
    assert "没有原生窗口能力" in 段, "Node 的 `窗口.打开` 没挡 `原生: 真`"


def test_脚本里关闭省略句柄要报错(tmp_path):
    """`窗口.关闭()` 无参是给**页面里的代码**用的 —— 脚本里省略就是误用。"""
    出 = _跑基石('导入 窗口\n窗口.关闭()\n', tmp_path, 期望=1)
    assert "页面里的代码" in 出, 出[:500]


def test_设标题在没原生能力时报错而不是返回假(tmp_path):
    """静默返回假最糟：用户会以为「改好了」，其实什么也没发生。"""
    出 = _跑基石('导入 窗口\n打印(窗口.设标题("x"))\n', tmp_path, 期望=1)
    assert "改不了窗口标题" in 出, 出[:500]
