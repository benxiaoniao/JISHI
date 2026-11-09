# -*- coding: utf-8 -*-
"""M186 · 图形底层：`图形` 模块（26 函数）+ 常驻 VM 帧循环（D110）。

## 这一站解决的是什么

0.3.3 之前，基石能开窗、能操作 DOM，但**画不了一个像素**。这一站给 2D 画布：
准备 / 清屏 / 矩形 / 圆 / 椭圆 / 线 / 多边形 / 文本 / 测文本 / 读图 / 画图 /
画子图 / 取像素 / 相机 / 输入 / 碰撞 / **帧循环**。

## 为什么「帧循环」是这一站最重的一块

原来那条路是「**一次事件 = 一次完整执行**」（`jishi_call` 把源码 + 处理函数拼起来
重跑）。点一次按钮没问题，**60 帧/秒重跑**不行 —— 全局状态每帧重建、`重绘()` 白跑。
所以有了**常驻 VM**（`docs/设计决策.md` D110）。

## 判据怎么验（这是本节最要紧的部分）

**「常驻」和「重放」必须能被区分开** —— 只验「画了 N 个矩形」的话，一个每帧重跑的
假常驻也能全绿（M176 的教训：判据要有回路）。所以核心那条是：

> 第 1 帧把计数加 1，**第 60 帧读回来必须是 60**（重放的话永远是 1）。

配套：事件改全局之后下一帧读到新值（证明事件没重放）、没调 `图形.运行` 的程序
`call` 之后状态**照样重置**（证明旧路没被改坏）。

## 为什么用 Node + 假 canvas

真 canvas 只有在浏览器里才验得了（判据跑在 CI 上）。但「基石 ↔ 宿主」是**协议**，
与 canvas 的具体实现无关 —— 假对象足够验它，而且**完全确定性**（判据自己按固定
时间戳喂 `jishi_frame`，不看墙钟）。

⚠️ 与 M167 同一条纪律：**读源码那份 shim，不读 `site/` 的拷贝**（那是构建产物，
CI 上没有）。wasm 是构建产物，所以 `skipif` 掉。
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
SHIM = ROOT / "wasm" / "jishi-wasm.js"

#: `图形` 的 26 个函数（**三宿主一个都不能少** —— `test_m51` 从另一头盯着）。
函数们 = [
    "准备", "宽度", "高度",
    "清屏", "矩形", "圆", "椭圆", "线", "多边形", "文本", "测文本",
    "读图", "画图", "画子图", "取像素",
    "设相机", "读相机",
    "按着", "按过", "鼠标", "滚轮",
    "碰到", "点到", "距离",
    "运行", "停",
]

#: 与 Rust `graphics::无画布` / Node `GRAPHICS_无` **逐字相同**。
平台错 = ("这个平台没有画布：「图形」模块只在网页 / 窗口里有"
          "（命令行和 Node 上没有画布）。要做图表请用「表格」，要出网页请用「站点」。")


# ---------------------------------------------------------------------------
# 一、平台边界：三宿主同一条错
# ---------------------------------------------------------------------------

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
    """`图形.矩形(...)` 在三个宿主上都要报**同一条**中文错。

    为什么强调「同一条」：这三个宿主上都不存在真画布，文案一漂，用户在不同引擎
    上看到的解释就不一样 —— 而「如实告知平台边界」的全部价值就在这里。
    """
    源 = '导入 图形\n图形.矩形(0, 0, 10, 10)\n'
    出 = {名: _跑(源, tmp_path, 名) for 名 in ("Python", "Rust", "Node") if
          (名 != "Rust" or rust_exe_path().is_file())}
    assert len(出) >= 2, "一个宿主都没跑起来"
    for 名, 文 in 出.items():
        assert 平台错 in 文, f"{名} 没报平台错：{文[:400]}"


def test_二十六个函数三宿主都在():
    """`图形` 的 26 个函数在 Python / Rust / Node 三份实现里都得有。

    `test_m51_consistency` 从「宿主自报家底」那头盯着同一件事；这条直接查源码，
    报错时能**指出缺的是哪个宿主、哪个函数**。
    """
    py = (ROOT / "oracle" / "jishi" / "stdlib" / "图形.py").read_text(encoding="utf-8")
    rs = (ROOT / "rust" / "src" / "stdlib" / "graphics.rs").read_text(encoding="utf-8")
    js = (ROOT / "oracle" / "node" / "vm.js").read_text(encoding="utf-8")
    for 名 in 函数们:
        assert f"def {名}(" in py, f"Python 侧缺 `{名}`"
        assert f'"{名}" =>' in rs, f"Rust 侧缺 `{名}`"
        assert f"'{名}': _noGraphics('{名}')" in js, f"Node 侧缺 `{名}`"


# ---------------------------------------------------------------------------
# 二、wasm 里真能画 / 真常驻（Node + 假 canvas）
# ---------------------------------------------------------------------------

_JS = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const BYTES = fs.readFileSync(process.argv[3]);

/** 一套假 canvas：记录每一次绘制调用，元素名 / 参数都留证。 */
function 造() {
  const 记录 = [];
  function ctx() {
    const c = {
      fillStyle: "", strokeStyle: "", lineWidth: 1, font: "", textAlign: "",
      globalAlpha: 1,
      setTransform: (...a) => 记录.push(["setTransform", ...a]),
      translate: (...a) => 记录.push(["translate", ...a]),
      rotate: (...a) => 记录.push(["rotate", ...a]),
      scale: (...a) => 记录.push(["scale", ...a]),
      save: () => 记录.push(["save"]), restore: () => 记录.push(["restore"]),
      fillRect: (...a) => 记录.push(["fillRect", c.fillStyle, ...a]),
      strokeRect: (...a) => 记录.push(["strokeRect", c.strokeStyle, ...a]),
      clearRect: () => {}, beginPath: () => {}, closePath: () => {},
      arc: (...a) => 记录.push(["arc", ...a]),
      ellipse: (...a) => 记录.push(["ellipse", ...a]),
      moveTo: (...a) => 记录.push(["moveTo", ...a]),
      lineTo: (...a) => 记录.push(["lineTo", ...a]),
      fill: () => 记录.push(["fill"]), stroke: () => 记录.push(["stroke"]),
      fillText: (...a) => 记录.push(["fillText", ...a]),
      strokeText: () => {},
      measureText: (t) => ({
        width: String(t).length * 8,
        actualBoundingBoxAscent: 12, actualBoundingBoxDescent: 3,
      }),
      getImageData: (x, y) => ({ data: [x & 255, y & 255, 7, 255] }),
      drawImage: (...a) => 记录.push(["drawImage", ...a]),
      roundRect: (...a) => 记录.push(["roundRect", ...a]),
    };
    return c;
  }
  const cv = {
    width: 0, height: 0, style: {}, clientWidth: 800, clientHeight: 600,
    getContext: () => ctx(),
  };
  const doc = {
    querySelector: (s) => (s === "#画布" ? cv : null),
    createElement: () => cv,
    body: { appendChild: () => {} },
    title: "",
  };
  return { doc, 记录, cv };
}

async function 新实例() {
  const w = 造();
  // `raf: () => 0`：给一个「永不回调」的调度器 ⇒ 帧完全由判据手动喂
  const j = await JishiWasm.load(BYTES, { doc: w.doc, raf: () => 0 });
  return { j, 记录: w.记录, cv: w.cv };
}

// ---- ① 画 + 状态保持 + dt + 事件不重放 ----
const SRC_画 = `导入 图形

令 游戏 = {"计数": 0, "分": 0, "间隔们": [], "帧号们": []}

函数 初始化()：
    图形.准备({"宽": 320, "高": 200, "背景": "#123"})

函数 每帧(号, 间隔)：
    游戏["计数"] = 游戏["计数"] + 1
    游戏["间隔们"].追加(间隔)
    游戏["帧号们"].追加(号)
    图形.清屏("#000")
    图形.矩形(10, 10, 50, 30, {"填充": "#f00"})
    图形.圆(100, 60, 12, {"填充": "#0f0", "描边": "#fff", "线宽": 2})
    图形.文本("分数 " + 文本(游戏["分"]), 5, 150, {"字号": 20})

函数 报告()：
    令 n = 长度(游戏["间隔们"])
    打印(游戏["计数"], "|", 游戏["分"], "|", 游戏["间隔们"][n - 1], "|", 游戏["帧号们"][n - 1])

函数 加分()：
    游戏["分"] = 游戏["分"] + 7

函数 收工()：
    打印(图形.停())

图形.运行(初始化, 每帧)
`;

// ---- ② 没调 `图形.运行` ⇒ 重放那条路没被改坏 ----
const SRC_重放 = `导入 图形
令 状态 = {"次数": 0}
函数 点一下()：
    状态["次数"] = 状态["次数"] + 1
    打印("第 ", 状态["次数"], " 次")
`;

// ---- ③ 每帧报错：要停、且只报一次 ----
const SRC_出错 = `导入 图形
令 状态 = {"n": 0}
函数 初始化()：
    图形.准备({"宽": 100, "高": 100})
函数 每帧(号, 间隔)：
    状态["n"] = 状态["n"] + 1
    如果 号 >= 3：
        令 坏 = 1 // 0
图形.运行(初始化, 每帧)
`;

// ---- ④ 帧内死循环：步数预算要掐掉 ----
const SRC_死循环 = `导入 图形
函数 初始化()：
    图形.准备({"宽": 100, "高": 100})
函数 每帧(号, 间隔)：
    令 i = 0
    当 i < 1000000000：
        i = i + 1
图形.运行(初始化, 每帧)
`;

// ---- ⑤ 纯计算：碰撞 / 距离 / 相机 / 像素 / 测文本 / 鼠标 / 滚轮 ----
const SRC_算 = `导入 图形
图形.准备({"宽": 200, "高": 100})
令 a = {"x": 0, "y": 0, "宽": 10, "高": 10}
令 b = {"x": 5, "y": 0, "宽": 10, "高": 10}
令 c = {"x": 50, "y": 0, "宽": 10, "高": 10}
打印(图形.碰到(a, b), 图形.碰到(a, c))
打印(图形.碰到({"x": 0, "y": 0, "半径": 10}, {"x": 15, "y": 0, "半径": 10}))
打印(图形.碰到({"x": 0, "y": 0, "半径": 5}, {"x": 100, "y": 100, "宽": 10, "高": 10}))
打印(图形.点到({"x": 5, "y": 5}, a), 图形.点到({"x": 50, "y": 50}, a))
打印(图形.距离({"x": 0, "y": 0}, {"x": 3, "y": 4}))
令 尺 = 图形.测文本("abc", {"字号": 16})
打印(尺["宽"], 尺["高"])
令 像 = 图形.取像素(3, 4)
打印(像[0], 像[1], 像[2], 像[3])
图形.设相机(100, 200)
令 相 = 图形.读相机()
打印(相["x"], 相["y"])
图形.矩形(0, 0, 5, 5, {"填充": "#abc"})
令 鼠 = 图形.鼠标()
打印(鼠["x"], 鼠["y"], 鼠["左键"])
令 滚 = 图形.滚轮()
打印(滚["dy"])
`;

// ---- ⑥ 样式键打错：要报错，不许静默无效 ----
const SRC_错键 = `导入 图形
图形.准备({"宽": 100, "高": 100})
图形.矩形(0, 0, 5, 5, {"填冲": "#f00"})
`;

// ---- ⑦ 在事件里 图形.运行：要报错（D110 §三.2）----
const SRC_事件里跑 = `导入 图形
令 状态 = {"x": 0}
函数 初始化()：
    图形.准备({"宽": 100, "高": 100})
函数 每帧(号, 间隔)：
    状态["x"] = 状态["x"] + 1
函数 重开()：
    图形.运行(初始化, 每帧)
图形.运行(初始化, 每帧)
`;

// ---- ⑧ 顶层连调两次 图形.运行：要报「已经在跑」----
const SRC_两次 = `导入 图形
函数 初始化()：
    图形.准备({"宽": 100, "高": 100})
函数 每帧(号, 间隔)：
    打印("x")
图形.运行(初始化, 每帧)
图形.运行(初始化, 每帧)
`;

(async () => {
  const 出 = {};

  // ①
  {
    const { j, 记录, cv } = await 新实例();
    const r = j.run(SRC_画);
    出.abi = j._ex.jishi_version();
    出.画_ok = r.ok;
    出.画_错 = r.error && (r.error.title + " / " + r.error.message);
    出.画_帧活 = j.framesActive();
    出.画_画布 = [cv.width, cv.height];
    出.画_码 = [];
    for (let i = 0; i < 60; i++) 出.画_码.push(j.frame(i * 16).code);
    // ⚠️ 计数要在**刚跑完 60 帧**时采：下面还会再喂一帧（验事件），
    //    晚一步采就会数出 61 —— 那不是 bug，是采晚了。
    出.画_fillRect红 = 记录.filter((x) => x[0] === "fillRect" && x[1] === "#f00").length;
    出.画_arc = 记录.filter((x) => x[0] === "arc").length;
    出.画_fillText = 记录.filter((x) => x[0] === "fillText").length;
    出.画_清屏 = 记录.filter((x) => x[0] === "fillRect" && x[1] === "#000").length;
    出.画_六十帧后 = j.call("报告").stdout.trim();
    // 事件改状态 ⇒ 下一帧要读到新值（**事件没有重放**）
    j.call("加分");
    j.frame(60 * 16);
    出.画_加分后 = j.call("报告").stdout.trim();
    // 停：`图形.停()` 之后再来一帧，应当返回 3（没有会话）
    出.停_返回 = j.call("收工").stdout.trim();
    出.停_帧活 = j.framesActive();
    出.停_后一帧 = j.frame(2000).code;
  }

  // ②
  {
    const { j } = await 新实例();
    j.run(SRC_重放);
    出.重放_1 = j.call("点一下").stdout.trim();
    出.重放_2 = j.call("点一下").stdout.trim();
    出.重放_帧活 = j.framesActive();
  }

  // ③
  {
    const { j } = await 新实例();
    j.run(SRC_出错);
    出.错_码 = [];
    for (let i = 0; i < 8; i++) {
      const f = j.frame(i * 16);
      出.错_码.push(f.code);
      if (f.code === 2 && !出.错_文) 出.错_文 = f.out;
    }
  }

  // ④
  {
    const { j } = await 新实例();
    j.setBudget(400000);        // 小一点，别让判据等太久
    j.run(SRC_死循环);
    const f = j.frame(0);
    出.死_码 = f.code;
    出.死_文 = f.out;
  }

  // ⑤
  {
    const { j } = await 新实例();
    const r = j.run(SRC_算);
    出.算_ok = r.ok;
    出.算_错 = r.error && (r.error.title + " / " + r.error.message);
    出.算_出 = r.stdout;
  }

  // ⑥
  {
    const { j } = await 新实例();
    const r = j.run(SRC_错键);
    出.键_ok = r.ok;
    出.键_文 = r.error && (r.error.title + " / " + r.error.message);
  }

  // ⑦
  {
    const { j } = await 新实例();
    j.run(SRC_事件里跑);
    const r = j.call("重开");
    出.事件_ok = r.ok;
    出.事件_文 = r.error && (r.error.title + " / " + r.error.message);
  }

  // ⑧
  {
    const { j } = await 新实例();
    const r = j.run(SRC_两次);
    出.两次_ok = r.ok;
    出.两次_文 = r.error && (r.error.title + " / " + r.error.message);
  }

  console.log(JSON.stringify(出));
})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
'''


def _跑wasm(tmp_path: Path) -> dict:
    if shutil.which("node") is None:
        pytest.skip("没有 node")
    if not WASM.is_file():
        pytest.skip("没构建 wasm（跑 python tools/build_wasm.py）")
    脚本 = tmp_path / "m186.js"
    脚本.write_text(_JS, encoding="utf-8", newline="\n")
    r = subprocess.run(["node", str(脚本), str(SHIM), str(WASM)],
                       capture_output=True, timeout=300)
    assert r.returncode == 0, (r.stdout + r.stderr).decode("utf-8", "replace")[-1500:]
    return json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])


@pytest.fixture(scope="module")
def 跑(tmp_path_factory):
    return _跑wasm(tmp_path_factory.mktemp("m186"))


def test_ABI是4且程序跑得起来(跑):
    """`jishi_frame` 是 ABI 4 加的 —— shim 靠版本号判断「这份 wasm 有没有帧循环」。"""
    assert 跑["abi"] == 4, f"ABI 应当是 4，实际 {跑['abi']}"
    assert 跑["画_ok"], f"程序没跑起来：{跑['画_错']}"


def test_真常驻_第60帧读到60(跑):
    """🔴 **这一条是「常驻」的定义本身**。

    第 1 帧把计数加 1，跑满 60 帧之后读回来必须是 **60**。
    如果走的是「每帧重放整个程序」，读回来永远是 **1** —— 而那种假常驻
    照样能画出 60 个矩形（所以光验图元是验不出来的）。
    """
    计数 = 跑["画_六十帧后"].split("|")[0].strip()
    assert 计数 == "60", f"第 60 帧读回来是 {计数}（重放的话会是 1）：{跑['画_六十帧后']}"
    assert 跑["画_帧活"], "跑完 60 帧，帧循环应当还活着"


def test_六十帧每一帧都跑成功了(跑):
    """返回码必须全是 `0`；出现 `2` 说明某一帧报错了（而报错会被藏起来）。"""
    assert set(跑["画_码"]) == {0}, f"有帧没跑成功：{跑['画_码']}"


def test_每帧真的画了东西(跑):
    """**可核对的事实**，不是「没报错」：60 帧 ⇒ 60 个红矩形 / 60 个圆 / 60 段文本。"""
    assert 跑["画_fillRect红"] == 60, f"红矩形应当 60 个，实际 {跑['画_fillRect红']}"
    assert 跑["画_arc"] == 60, f"圆应当 60 个，实际 {跑['画_arc']}"
    assert 跑["画_fillText"] == 60, f"文本应当 60 段，实际 {跑['画_fillText']}"
    # 清屏是整块 fillRect，颜色不同 —— 单独数，别和红矩形混在一起
    assert 跑["画_清屏"] == 60, f"清屏应当 60 次，实际 {跑['画_清屏']}"


def test_画布按逻辑像素建_位图按DPR放大(跑):
    """`准备({宽:320, 高:200})` ⇒ 位图 320×200（假环境 DPR = 1）。"""
    assert 跑["画_画布"] == [320, 200], f"画布位图尺寸不对：{跑['画_画布']}"


def test_常驻下事件不重放_状态是活的(跑):
    """🔴 点一下按钮不能把游戏重置。

    `加分()` 把 `游戏["分"]` 加 7，下一帧的文本里就该出现 7、计数变成 61。
    如果事件走了「重跑顶层」，分会被打回 0、计数回到 1 —— 那正是「点一下开始
    游戏，分数归零」那个 bug。
    """
    段 = [" ".join(x.split()) for x in 跑["画_加分后"].split("|")]
    assert 段[0] == "61", f"事件之后计数应当是 61，实际 {段[0]}"
    assert 段[1] == "7", f"事件改的分丢了（被重放了？）：{跑['画_加分后']}"


def test_dt_按时间戳算_并且第一帧是0(跑):
    """第 60 帧的 `间隔` 必须是 16（判据喂的 `i*16`），第 1 帧是 0（没有上一帧）。"""
    段 = [x.strip() for x in 跑["画_六十帧后"].split("|")]
    assert float(段[2]) == 16.0, f"间隔应当是 16，实际 {段[2]}"
    assert 段[3] == "59", f"帧号应当从 0 数到 59，实际 {段[3]}"


def test_没调运行的程序_事件照样重放(跑):
    """常驻是 **opt-in**：没调 `图形.运行` 的程序，`call` 之后状态必须重置。

    这条守的是「旧路一个字没改」。它绿了，才说明常驻没有偷偷变成全局行为。
    """
    assert " ".join(跑["重放_1"].split()) == "第 1 次", f"第一次调用：{跑['重放_1']!r}"
    assert " ".join(跑["重放_2"].split()) == "第 1 次", (
        f"第二次调用应当又是「第 1 次」（重放会重建状态），实际 {跑['重放_2']!r}")
    assert 跑["重放_帧活"] is False, "没调 图形.运行 就不该有帧循环"


def test_每帧出错就停_而且只报一次(跑):
    """出错那一帧返回 `2`，之后一律 `3`（帧循环已经停了）。

    「只报一次」不是洁癖：60 次/秒往控制台刷同一个错误，等于没有报错。
    """
    码 = 跑["错_码"]
    assert 码[0] == 0 and 码[1] == 0 and 码[2] == 0, f"前 3 帧应当正常：{码}"
    assert 码[3] == 2, f"第 4 帧应当报错（2），实际 {码[3]}"
    assert set(码[4:]) == {3}, f"出错之后应当全是 3（没会话），实际 {码[4:]}"
    assert "不能除以零" in 跑["错_文"], f"错误文不对：{跑['错_文'][:300]}"


def test_帧内死循环被步数预算掐掉(跑):
    """一帧跑飞了不能把页面冻住 —— wasm 上靠步数预算（不是线程超时）。"""
    assert 跑["死_码"] == 2, f"死循环那一帧应当返回 2，实际 {跑['死_码']}"
    assert "执行步数超过上限" in 跑["死_文"], f"错误文不对：{跑['死_文'][:300]}"


def test_碰撞与距离算得对(跑):
    """矩形 × 矩形 / 圆 × 圆 / 圆 × 矩形 / 点到 / 距离 —— 纯算术，不问宿主。"""
    assert 跑["算_ok"], f"程序没跑起来：{跑['算_错']}"
    行 = 跑["算_出"].strip().splitlines()
    assert 行[0] == "真 假", f"矩形相交/分离判错：{行[0]!r}"
    assert 行[1] == "真", f"圆相交（距离 15 < 半径和 20）应判真：{行[1]!r}"
    assert 行[2] == "假", f"圆与远处的矩形不该相交：{行[2]!r}"
    assert 行[3] == "真 假", f"点到判错：{行[3]!r}"
    assert 行[4] == "5.0", f"3-4-5 的距离应当是 5.0：{行[4]!r}"
    assert 行[5] == "24 15", f"测文本应当是 24×15：{行[5]!r}"
    assert 行[6] == "3 4 7 255", f"取像素应当原样回来：{行[6]!r}"
    assert 行[7] == "100 200", f"相机读回来不对：{行[7]!r}"
    assert 行[8] == "0 0 假", f"鼠标初值不对：{行[8]!r}"
    assert 行[9] == "0", f"滚轮初值不对：{行[9]!r}"


def test_样式键打错要报错(跑):
    """打错一个键名却「看起来没效果」是最难查的一类 —— 所以这里**报错**。"""
    assert 跑["键_ok"] is False, "打错样式键名的程序不该跑成功"
    assert "填冲" in 跑["键_文"], f"应当指出是哪个键写错了：{跑['键_文']}"


def test_事件里不许进常驻(跑):
    """D110 §三.2：`图形.运行` 只能在顶层。

    事件处理时顶层**已经重跑过一遍**，那时再登记等于让游戏从「跑了一半」开始。
    """
    assert 跑["事件_ok"] is False, "在事件处理函数里 图形.运行 应当报错"
    assert "只能写在顶层" in 跑["事件_文"], f"错误文不对：{跑['事件_文']}"


def test_不许两个循环(跑):
    """已经在跑时再登记 ⇒ 报「已经在跑」，并告诉用户先 `图形.停()`。"""
    assert 跑["两次_ok"] is False, "连调两次 图形.运行 应当报错"
    assert "已经在跑" in 跑["两次_文"], f"错误文不对：{跑['两次_文']}"


def test_停之后帧循环真的停了(跑):
    """`图形.停()` 之后：`jishi_frame` 返回 `3`（没会话），且 shim 那边不再排帧。"""
    assert 跑["停_返回"] == "真", f"停 应当返回真（之前有在跑），实际 {跑['停_返回']!r}"
    assert 跑["停_帧活"] is False, "停 之后 shim 不该还认为帧循环在跑"
    assert 跑["停_后一帧"] == 3, f"停 之后再喂一帧应当返回 3，实际 {跑['停_后一帧']}"
