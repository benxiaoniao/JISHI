# -*- coding: utf-8 -*-
"""M173 · `窗口`：把程序开成一个窗口。

## 这一组在防什么

1. **窗口真的开在「我们自己起的服务」上**。应用模式（`--app=`）靠 URL，
   而 wasm 页面在 `file://` 下读不了 wasm ⇒ 服务这一段错了，窗口里就是一张白页。
   所以判据不看「有没有起进程」，看**那个 URL 能不能取到页面和引擎素材**。
2. **URL 是合法的**：中文文件名必须百分号编码（HTTP 请求行只认 ASCII）。
   这条踩过 —— 浏览器自己会编码，但别的客户端会当场报错。
3. **生命周期对得上**：`在运行` / `等` / `关闭` / `全部关闭`，以及**窗口关了
   服务要跟着停**（不然脚本跑完还占着端口）。
4. **计算器的逻辑真在 wasm 里跑**：拿 Node + 假 DOM 把页面里那段基石源码跑一遍，
   验 `7+3=10`、连算、小数、除零。不是「生成了一个 HTML 就算完」。

⚠️ 判据**不真开浏览器窗口**（CI 上没有图形界面，也不该在用户屏幕上弹窗）：
`浏览器` 选项传一个「假浏览器」脚本，它会把自己收到的参数写进文件 ——
于是「窗口参数对不对」成了可断言的东西。真浏览器验过一次（见 CHANGELOG）。
"""

import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile  # noqa: F401  （保持与其它判据一样的导入习惯）
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                            # noqa: E402

WASM = ROOT / "site" / "jishi_wasm.wasm"
SHIM = ROOT / "wasm" / "jishi-wasm.js"

#: 只连本机，别走沙箱/系统的代理（否则 127.0.0.1 的请求会被转出去）
直连 = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _假浏览器(tmp_path):
    """一个「浏览器」：把收到的参数写成文件，然后睡 2 秒退出。"""
    p = tmp_path / "假浏览器.py"
    p.write_text(
        "import sys, time, pathlib\n"
        "pathlib.Path(__file__).with_name('参数.txt').write_text(\n"
        "    '\\n'.join(sys.argv[1:]), encoding='utf-8')\n"
        "time.sleep(2)\n",
        encoding="utf-8")
    return p


def _等参数(tmp_path, 秒=8):
    p = tmp_path / "参数.txt"
    for _ in range(int(秒 * 10)):
        if p.is_file() and "--app=" in p.read_text(encoding="utf-8"):
            return p.read_text(encoding="utf-8")
        import time
        time.sleep(0.1)
    raise AssertionError("假浏览器没被启动（没有参数文件）")


def _网址(参数文本):
    return [ln for ln in 参数文本.splitlines() if ln.startswith("--app=")][0][6:]


def _跑(程序, tmp_path, exe=None, 期望=0):
    (tmp_path / "程序.jsh").write_text(程序, encoding="utf-8", newline="\n")
    命令 = ([str(exe)] if exe else [sys.executable, "-m", "oracle.jishi.cli"])
    r = subprocess.run(命令 + ["程序.jsh"], cwd=str(tmp_path),
                       capture_output=True, timeout=300, input=b"")
    出 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode == 期望, f"退出码 {r.returncode}：{出[:600]}"
    return 出


# ---------------------------------------------------------------------------
# 一、五个函数，三宿主都在
# ---------------------------------------------------------------------------

def test_六个函数三宿主都在():
    # M173 的六个基本盘（M174 加了 `设标题`，M175 又加了八个桌面集成的 ——
    # 那些在 `test_m175_desktop.py` 里钉，这里只守「老的那六个别丢」）。
    名们 = ["打开", "关闭", "设标题", "等", "在运行", "全部关闭"]
    py = (ROOT / "oracle" / "jishi" / "stdlib" / "窗口.py").read_text(encoding="utf-8")
    for 名 in 名们:
        assert f"def {名}(" in py, f"Python 侧缺 `{名}`"
    rs = (ROOT / "rust" / "src" / "stdlib" / "window.rs").read_text(encoding="utf-8")
    for 名 in 名们:
        assert f'"{名}" =>' in rs, f"Rust 侧缺 `{名}`"
    js = (ROOT / "oracle" / "node" / "vm.js").read_text(encoding="utf-8")
    for 名 in 名们:
        assert f"'{名}': new R.Builtin('{名}'" in js, f"Node 侧缺 `{名}`"


# ---------------------------------------------------------------------------
# 二、开窗：URL / 服务 / 参数
# ---------------------------------------------------------------------------

def test_开窗时URL能取到页面和引擎素材(tmp_path):
    """窗口里显示的东西是**这个 URL** 给的 —— 所以先验它。"""
    假 = _假浏览器(tmp_path)
    (tmp_path / "页.html").write_text("<!doctype html><title>演示</title><h1>你好</h1>",
                                     encoding="utf-8")
    程序 = f'''导入 窗口
导入 系统
令 窗 = 窗口.打开({{"页面": "页.html", "宽": 420, "高": 300,
    "浏览器": [系统.环境变量("PYTHON"), "{假.as_posix()}"]}})
打印("句柄", 窗, "在运行", 窗口.在运行(窗))
# 等它自己退出（假浏览器睡 2 秒）—— 别急着关，否则它还没来得及写参数文件
窗口.等(窗)
'''
    env_旧 = os.environ.get("PYTHON")
    os.environ["PYTHON"] = sys.executable
    try:
        出 = _跑(程序, tmp_path)
    finally:
        if env_旧 is None:
            os.environ.pop("PYTHON", None)
        else:
            os.environ["PYTHON"] = env_旧
    assert "在运行 真" in 出, 出[:400]

    参数 = _等参数(tmp_path)
    网址 = _网址(参数)
    assert 网址.startswith("http://127.0.0.1:"), 网址
    assert "%E9%A1%B5.html" in 网址 or "%e9%a1%b5.html" in 网址, f"中文文件名没编码：{网址}"
    assert "--window-size=420,300" in 参数, 参数
    assert "--user-data-dir=" in 参数, 参数


@pytest.mark.skipif(not (ROOT / "site" / "jishi_wasm.wasm").is_file(),
                    reason="还没构建 wasm（跑 tools/build_wasm.py）—— CI 会先构建它，"
                           "所以那边这条**真的跑了**")
def test_服务能把页面和引擎素材喂出去(tmp_path):
    """这一条才是「窗口里不会是白页」的真正判据。

    ⚠️ 它依赖 `site/jishi_wasm.wasm`（`build_wasm.py` 的产出）。按 `AGENTS.md` §1 ④
    的口径，构建产物一般不许硬依赖 —— 但这一份是**发行必需的**（`窗口` 靠它喂页面），
    CI 两个 workflow 都会先构建，所以这里允许；本地没构建时跳过而不是报错。
    """
    import time

    假 = _假浏览器(tmp_path)
    (tmp_path / "页.html").write_text("<!doctype html><title>演示</title><h1>你好</h1>",
                                     encoding="utf-8")
    # 起窗、趁假浏览器还活着的时候去取
    import importlib
    sys.path.insert(0, ".")
    窗口 = importlib.import_module("oracle.jishi.stdlib.窗口")
    窗 = 窗口.打开({"页面": str(tmp_path / "页.html"), "浏览器": [sys.executable, str(假)]})
    try:
        参数 = _等参数(tmp_path)
        网址 = _网址(参数)
        基 = 网址.rsplit("/", 1)[0]
        正文 = 直连.open(网址, timeout=5).read().decode("utf-8")
        assert "你好" in 正文, 正文[:200]
        r = 直连.open(基 + "/__jishi/jishi-wasm.js", timeout=10)
        assert r.status == 200 and "javascript" in r.headers.get("Content-Type", ""), r.headers
        r = 直连.open(基 + "/__jishi/jishi_wasm.wasm", timeout=30)
        assert r.status == 200, r.headers
        assert r.headers.get("Content-Type") == "application/wasm", r.headers
        assert len(r.read()) > 100000, "wasm 没取全"
    finally:
        窗口.全部关闭()
        窗口.等(窗)
        time.sleep(0.3)


def test_窗口关了服务要停(tmp_path):
    """不停的话：脚本跑完还占着端口，而且下一次「同一个目录」会复用到一个死服务。"""
    import time

    import importlib
    sys.path.insert(0, ".")
    窗口 = importlib.import_module("oracle.jishi.stdlib.窗口")
    假 = _假浏览器(tmp_path)
    (tmp_path / "页.html").write_text("<!doctype html><p>x</p>", encoding="utf-8")
    窗 = 窗口.打开({"页面": str(tmp_path / "页.html"), "浏览器": [sys.executable, str(假)]})
    参数 = _等参数(tmp_path)
    网址 = _网址(参数)
    窗口.全部关闭()
    窗口.等(窗)
    time.sleep(0.5)
    with pytest.raises(Exception):
        直连.open(网址, timeout=3).read()


# ---------------------------------------------------------------------------
# 三、把话说清楚
# ---------------------------------------------------------------------------

def test_页面不存在要报中文错(tmp_path):
    出 = _跑('导入 窗口\n窗口.打开({"页面": "没有这个.html"})\n', tmp_path, 期望=1)
    assert "找不到页面" in 出 and "没有这个.html" in 出, 出[:400]


def test_页面与服务只能给一个(tmp_path):
    出 = _跑('导入 窗口\n窗口.打开({"页面": "a.html", "服务": "http://x/"})\n',
            tmp_path, 期望=1)
    assert "只能给一个" in 出, 出[:400]


def test_什么也没给要说清楚(tmp_path):
    出 = _跑('导入 窗口\n窗口.打开({"标题": "x"})\n', tmp_path, 期望=1)
    assert "要指定「页面」或「服务」" in 出, 出[:400]


def test_浏览器指向不存在的文件要报错(tmp_path):
    (tmp_path / "页.html").write_text("<p>x</p>", encoding="utf-8")
    出 = _跑('导入 窗口\n窗口.打开({"页面": "页.html", "浏览器": "没有这个浏览器.exe"})\n',
            tmp_path, 期望=1)
    assert "不存在" in 出, 出[:400]


def test_等一个已经关掉的窗口立刻返回0(tmp_path):
    假 = _假浏览器(tmp_path)
    (tmp_path / "页.html").write_text("<p>x</p>", encoding="utf-8")
    程序 = f'''导入 窗口
导入 系统
令 窗 = 窗口.打开({{"页面": "页.html", "浏览器": [系统.环境变量("PY") , "{假.as_posix()}"]}})
打印("关了几个", 窗口.全部关闭())
打印("等回来的", 窗口.等(窗))
'''
    env_旧 = os.environ.get("PY")
    os.environ["PY"] = sys.executable
    try:
        出 = _跑(程序, tmp_path)
    finally:
        if env_旧 is None:
            os.environ.pop("PY", None)
        else:
            os.environ["PY"] = env_旧
    assert "关了几个 1" in 出, 出[:400]
    assert "等回来的 0" in 出, 出[:400]


# ---------------------------------------------------------------------------
# 四、计算器：逻辑真在 wasm 里跑
# ---------------------------------------------------------------------------

def _计算器源码():
    h = (ROOT / "examples" / "desktop" / "计算器.html").read_text(encoding="utf-8")
    return h.split("const 源码 = `", 1)[1].split("`;", 1)[0]


@pytest.mark.skipif(not WASM.is_file(), reason="没构建 wasm（跑 python tools/build_wasm.py）")
@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_计算器在wasm里真能算(tmp_path):
    """把页面里那段基石源码放到**真 wasm 引擎 + 假 DOM** 上按一遍键。

    假 DOM 里给每个「按钮」带上 `dataset.序`，事件回调就走的与浏览器**同一条路**
    （`data_序` → shim → `call(函数名, 键)`）。
    """
    js = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const SRC = fs.readFileSync(process.argv[4], "utf8");

const els = new Map();
function makeEl() {
  const hs = {};
  return {
    textContent: "", value: "", style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, has() { return false; } },
    addEventListener(ev, h) { (hs[ev] = hs[ev] || []).push(h); },
    fire(ev, target) { (hs[ev] || []).forEach((h) => h({ type: ev, target: target })); },
  };
}
const doc = { querySelector: (s) => { if (!els.has(s)) els.set(s, makeEl()); return els.get(s); } };
const kv = new Map();
const store = { getItem: (k) => (kv.has(k) ? kv.get(k) : null),
  setItem: (k, v) => kv.set(k, String(v)), removeItem: (k) => kv.delete(k) };

(async () => {
  const j = await JishiWasm.load(fs.readFileSync(process.argv[3]), { doc, store });
  const r = j.run(SRC);
  const 出 = { ok: r.ok, 错: r.ok ? "" : (r.error.title + "/" + r.error.message), 算: [], 点: "" };
  if (!r.ok) { console.log(JSON.stringify(出)); return; }

  const 敲 = (键们) => { 键们.forEach((k) => j.call("按键", k));
    return els.get("#显示").textContent; };

  出.算.push(["7+3=", 敲(["C", "7", "+", "3", "="])]);
  出.算.push(["连算 ×2", 敲(["×", "2", "="])]);
  出.算.push(["小数 1.5+2.5", 敲(["C", "1", ".", "5", "+", "2", ".", "5", "="])]);
  出.算.push(["除零 9÷0", 敲(["C", "9", "÷", "0", "="])]);

  // 走**事件**这条路：点一个带 data-序 的按钮（与浏览器同一条链）
  const 键盘 = els.get("#键盘");
  出.监听 = (键盘.__hs || 键盘.dataset ? 0 : 0) || 0;
  j.run(SRC);                    // 每次事件都会重跑整个程序，这里手动跑一次
  键盘.fire("点击", { dataset: { "序": "5" } });
  出.点 = els.get("#显示").textContent;
  console.log(JSON.stringify(出));
})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
'''
    p = tmp_path / "驱动.js"
    p.write_text(js, encoding="utf-8")
    源码 = tmp_path / "计算器源码.jsh"
    源码.write_text(_计算器源码(), encoding="utf-8", newline="\n")
    r = subprocess.run(["node", str(p), str(SHIM), str(WASM), str(源码)],
                       capture_output=True, timeout=180)
    出 = r.stdout.decode("utf-8", "replace").strip()
    assert r.returncode == 0, (r.stderr.decode("utf-8", "replace")[:800] or 出[:400])
    结果 = json.loads(出.splitlines()[-1])
    assert 结果["ok"], 结果["错"]
    assert dict(结果["算"]) == {
        "7+3=": "10",
        "连算 ×2": "20",
        "小数 1.5+2.5": "4",
        "除零 9÷0": "除数不能是 0",
    }, 结果["算"]
    assert 结果["点"] == "5", f"点带 data-序 的按钮没生效：{结果['点']}"


def test_计算器的基石源码能编译():
    """页面里嵌的那段源码必须过得了解析（它不会被 `jishi` 直接跑到，但语法要对）。"""
    import subprocess as sp
    p = ROOT / "build" / "ev" / "m173_calc_src.jsh"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_计算器源码(), encoding="utf-8", newline="\n")
    r = sp.run([sys.executable, "-m", "oracle.jishi.cli", "--ast", str(p)],
               capture_output=True, cwd=str(ROOT), timeout=120)
    assert r.returncode == 0, (r.stdout + r.stderr).decode("utf-8", "replace")[:600]


def test_计算器示例的两个文件都在():
    assert (ROOT / "examples" / "desktop" / "计算器.html").is_file()
    assert (ROOT / "examples" / "desktop" / "计算器.jsh").is_file()
