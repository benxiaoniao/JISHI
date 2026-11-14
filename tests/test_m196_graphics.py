# -*- coding: utf-8 -*-
"""M196 · 图形深化：`图形` 26 → 45 函数（离屏 / 裁剪 / 排版 / 路径 / 渐变 / 混合）。

## 这一站解决的是什么

M186 只能画「一个一个图元」。真要出画面还差六件事：

| 缺什么 | 谁撞出来的 |
|---|---|
| **离屏画布** | 视差背景、小地图、预渲染静态层 —— 每帧重画 200 个瓦片太浪费 |
| **裁剪** | 滚动列表、地图出界 |
| **文字折行** | 长句只能按字数硬切，中英文混排必然难看 |
| **路径** | 箭头、选中框、不规则形状；`多边形` 只能画封闭的 |
| **渐变** | 天空、光晕 —— 也让「不会 PS 的人」能出能看的东西 |
| **混合 / 透明度** | 阴影与淡出 |

## 判据怎么验（这一节最要紧）

**「离屏再贴」和「直接画」必须等价** —— 但「等价」不能靠看像素（判据跑在 CI 上，
没有真 canvas）。所以用**假 canvas 记录指令序列**：

> 直接画一组图元 → 记下主画布的指令序列 A；
> 同样的图元画到离屏上再 `贴到` → 记下离屏画布的指令序列 B；
> **A == B**。

这条能抓住「离屏上画出来的东西和直接画不一样」的所有实现错（坐标系忘了算、
样式没生效、相机没跟过去……）。而 `贴到` 本身有没有把画贴上去，
由「主画布收到了 `drawImage`」这一条单独钉。

⚠️ 与 M186 同一条纪律：**读 `wasm/jishi-wasm.js` 源码那份，不读 `site/` 的拷贝**；
wasm 是构建产物 ⇒ `skipif` 掉（`tools/build_wasm.py` 才产）。
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

#: M186 的 26 个 + M196 的 19 个（**三宿主一个都不能少** —— `test_m51` 从另一头盯着）。
函数们 = [
    "准备", "宽度", "高度",
    "清屏", "矩形", "圆", "椭圆", "线", "多边形", "文本", "测文本",
    "读图", "画图", "画子图", "取像素",
    "设相机", "读相机",
    "按着", "按过", "鼠标", "滚轮",
    "碰到", "点到", "距离",
    "运行", "停",
    # M196
    "离屏", "用画布", "贴到", "离屏像素",
    "设裁剪", "清裁剪",
    "文字折行", "写段落",
    "路径", "移到", "线到", "弧到", "闭合路径", "填充路径", "描边路径",
    "线性渐变", "径向渐变",
    "设混合", "设透明度",
]

#: 与 Rust `graphics::无画布` / Node `GRAPHICS_无` / Python `_无画布` **逐字相同**。
平台错 = ("这个平台没有画布：「图形」模块只在网页 / 窗口里有"
          "（命令行和 Node 上没有画布）。要做图表请用「表格」，要出网页请用「站点」。")


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


# ---------------------------------------------------------------------------
# 一、三宿主同宽 + 同一条平台错
# ---------------------------------------------------------------------------

def test_四十五个函数三宿主都在():
    """Python / Rust / Node 三份实现里都得有这 45 个函数。

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


def test_命令行上报平台错_新增的十九个也报同一句(tmp_path):
    """M196 的函数在命令行 / Node 上也要报**同一条**中文错（不能报别的）。"""
    片段 = [
        "图形.离屏(10, 10)",
        '图形.用画布(空)',
        "图形.贴到(1, 0, 0)",
        "图形.离屏像素(1, 0, 0)",
        '图形.设裁剪({"x": 0, "y": 0, "宽": 1, "高": 1})',
        "图形.清裁剪()",
        '图形.文字折行("abc", 10)',
        '图形.写段落("abc", 0, 0, 10)',
        "图形.路径()",
        "图形.移到(1, 0, 0)",
        "图形.线到(1, 0, 0)",
        "图形.弧到(1, 0, 0, 1, 1, 2)",
        "图形.闭合路径(1)",
        "图形.填充路径(1)",
        "图形.描边路径(1)",
        '图形.线性渐变(0, 0, 0, 1, ["#000", "#fff"])',
        '图形.径向渐变(0, 0, 0, 1, ["#000", "#fff"])',
        '图形.设混合("相乘")',
        "图形.设透明度(0.5)",
    ]
    for i, 句 in enumerate(片段):
        源 = "导入 图形\n" + 句 + "\n"
        出 = {名: _跑(源, tmp_path, 名) for 名 in ("Python", "Rust", "Node") if
              (名 != "Rust" or rust_exe_path().is_file())}
        assert len(出) >= 2, "一个宿主都没跑起来"
        for 名, 文 in 出.items():
            assert 平台错 in 文, f"{名} 跑 `{句}` 没报平台错：{文[:300]}"


# ---------------------------------------------------------------------------
# 二、wasm + 假 canvas：真指令序列
# ---------------------------------------------------------------------------

_JS = r'''
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const BYTES = fs.readFileSync(process.argv[3]);

/** 一套假 canvas：**每块画布各自一份记录**，指令名与参数都留证。
 *
 * 与 M186 的区别：`createElement` 每次造**新的**（离屏画布必须是独立的），
 * 并且补上了 M196 要用的 `createLinearGradient` / `createRadialGradient` /
 * `clip` / `rect` / `arcTo`。
 */
function 造世界() {
  const 画布们 = [];
  let 序 = 0;

  function 新画布(名) {
    const 本名 = 名 || ("cv#" + (序++));
    const 记录 = [];
    function 记(op, ...a) { 记录.push([op].concat(a.map(简))); }
    function 简(v) {
      if (v && typeof v === "object" && v.__grad) return "渐变";
      if (typeof v === "number") return Math.round(v * 1000) / 1000;
      return v;
    }

    const c = {
      fillStyle: "", strokeStyle: "", lineWidth: 1, font: "", textAlign: "",
      globalAlpha: 1, globalCompositeOperation: "source-over",
      setTransform: (...a) => 记("setTransform", ...a),
      translate: (...a) => 记("translate", ...a),
      rotate: (...a) => 记("rotate", ...a),
      scale: (...a) => 记("scale", ...a),
      save: () => 记("save"), restore: () => 记("restore"),
      fillRect: (...a) => 记("fillRect", c.fillStyle, c.globalAlpha,
                              c.globalCompositeOperation, ...a),
      strokeRect: (...a) => 记("strokeRect", c.strokeStyle, ...a),
      clearRect: () => {},
      beginPath: () => 记("beginPath"), closePath: () => 记("closePath"),
      rect: (...a) => 记("rect", ...a),
      clip: () => 记("clip"),
      arc: (...a) => 记("arc", ...a),
      ellipse: (...a) => 记("ellipse", ...a),
      arcTo: (...a) => 记("arcTo", ...a),
      moveTo: (...a) => 记("moveTo", ...a),
      lineTo: (...a) => 记("lineTo", ...a),
      fill: () => 记("fill"),
      stroke: () => 记("stroke"),
      fillText: (t, x, y) => 记("fillText", t, x, y, c.globalAlpha,
                                c.globalCompositeOperation),
      strokeText: () => {},
      measureText: (t) => ({ width: String(t).length * 8,
                             actualBoundingBoxAscent: 12,
                             actualBoundingBoxDescent: 3 }),
      getImageData: (x, y) => ({ data: [x & 255, y & 255, 7, 255] }),
      drawImage: (cv, x, y) => 记("drawImage", cv && cv.__名, x, y),
      roundRect: (...a) => 记("roundRect", ...a),
      createLinearGradient: (...a) => {
        记("linearGradient", ...a);
        const g = { __grad: true, 号: "lg", 色标们: [] };
        g.addColorStop = (p, col) => { g.色标们.push([p, col]); 记("色标", p, col); };
        return g;
      },
      createRadialGradient: (...a) => {
        记("radialGradient", ...a);
        const g = { __grad: true, 号: "rg", 色标们: [] };
        g.addColorStop = (p, col) => { g.色标们.push([p, col]); 记("色标", p, col); };
        return g;
      },
    };
    const cv = {
      width: 0, height: 0, style: {}, clientWidth: 800, clientHeight: 600,
      getContext: () => c, 记录: 记录, __名: 本名,
    };
    画布们.push(cv);
    return cv;
  }

  const 主 = 新画布("主");
  const doc = {
    querySelector: (s) => (s === "#画布" ? 主 : null),
    createElement: () => 新画布(),
    body: { appendChild: () => {} },
    title: "",
  };
  return { doc, 主, 画布们 };
}

async function 跑一段(源) {
  const w = 造世界();
  const j = await JishiWasm.load(BYTES, { doc: w.doc, raf: () => 0 });
  const r = j.run(源);
  return {
    ok: r.ok,
    错: r.error && (r.error.title + " / " + r.error.message),
    出: r.stdout || "",
    主: w.主.记录,
    画布们: w.画布们.map((c) => ({ 名: c.__名, 记录: c.记录 })),
  };
}

const S1_直接 = `导入 图形
图形.准备({"宽": 200, "高": 100})
图形.矩形(10, 10, 50, 30, {"填充": "#f00"})
图形.圆(100, 50, 20, {"填充": "#0f0"})
`;

const S2_离屏 = `导入 图形
图形.准备({"宽": 200, "高": 100})
令 层 = 图形.离屏(200, 100)
图形.用画布(层)
图形.矩形(10, 10, 50, 30, {"填充": "#f00"})
图形.圆(100, 50, 20, {"填充": "#0f0"})
图形.用画布(空)
图形.贴到(层, 0, 0)
`;

const S3_折行 = `导入 图形
图形.准备({"宽": 400, "高": 200})
令 行们 = 图形.文字折行("这是一段很长的中文文本需要折行显示才能放得下这个宽度", 80)
遍历 行 在 行们：
    打印("[" + 行 + "]")
令 二 = 图形.文字折行("hello world foo bar baz qux", 80)
打印("行数 " + 文本(长度(二)))
遍历 行 在 二：
    打印("<" + 行 + ">")
`;

const S4_像素 = `导入 图形
图形.准备({"宽": 100, "高": 100})
令 层 = 图形.离屏(50, 40)
图形.用画布(层)
图形.清屏("#123")
图形.用画布(空)
打印(文本(图形.离屏像素(层, 3, 7)))
`;

const S5_路径 = `导入 图形
图形.准备({"宽": 200, "高": 100})
令 p = 图形.路径()
图形.移到(p, 10, 10)
图形.线到(p, 50, 10)
图形.线到(p, 50, 40)
图形.闭合路径(p)
图形.填充路径(p, {"填充": "#08f"})
图形.描边路径(p, {"描边": "#fff", "线宽": 2})
令 圆角 = 图形.路径()
图形.移到(圆角, 0, 0)
图形.弧到(圆角, 100, 0, 100, 100, 10)
图形.描边路径(圆角, {"描边": "#999"})
`;

const S6_渐变 = `导入 图形
图形.准备({"宽": 200, "高": 100})
令 g = 图形.线性渐变(0, 0, 0, 100, [[0, "#00f"], [1, "#0ff"]])
图形.矩形(0, 0, 200, 100, {"填充": g})
令 r = 图形.径向渐变(100, 50, 0, 60, ["#fff", "#000"])
图形.圆(100, 50, 40, {"填充": r})
`;

const S7_裁剪 = `导入 图形
图形.准备({"宽": 200, "高": 100})
图形.设裁剪({"x": 0, "y": 0, "宽": 50, "高": 50})
图形.矩形(0, 0, 200, 100, {"填充": "#f00"})
图形.清裁剪()
图形.矩形(0, 0, 10, 10, {"填充": "#0f0"})
`;

const S8_混合 = `导入 图形
图形.准备({"宽": 200, "高": 100})
图形.设混合("相乘")
图形.设透明度(0.5)
图形.矩形(0, 0, 50, 50, {"填充": "#f00"})
图形.圆(60, 60, 10, {"填充": "#0f0"})
`;

const S9_段落 = `导入 图形
图形.准备({"宽": 400, "高": 200})
图形.写段落("aaaa bbbb cccc dddd eeee", 10, 30, 80, {"字号": 16, "行距": 20})
`;

const S10_句柄错 = `导入 图形
图形.准备({"宽": 100, "高": 100})
图形.贴到(99, 0, 0)
`;

const S11_越界像素 = `导入 图形
图形.准备({"宽": 100, "高": 100})
令 层 = 图形.离屏(10, 10)
图形.离屏像素(层, 50, 5)
`;

const S12_多清裁剪 = `导入 图形
图形.准备({"宽": 100, "高": 100})
图形.清裁剪()
`;

(async () => {
  const 出 = {};
  出.s1 = await 跑一段(S1_直接);
  出.s2 = await 跑一段(S2_离屏);
  出.s3 = await 跑一段(S3_折行);
  出.s4 = await 跑一段(S4_像素);
  出.s5 = await 跑一段(S5_路径);
  出.s6 = await 跑一段(S6_渐变);
  出.s7 = await 跑一段(S7_裁剪);
  出.s8 = await 跑一段(S8_混合);
  出.s9 = await 跑一段(S9_段落);
  出.s10 = await 跑一段(S10_句柄错);
  出.s11 = await 跑一段(S11_越界像素);
  出.s12 = await 跑一段(S12_多清裁剪);
  console.log(JSON.stringify(出));
})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
'''


def _跑wasm(tmp_path: Path) -> dict:
    if shutil.which("node") is None:
        pytest.skip("没有 node")
    if not WASM.is_file():
        pytest.skip("没构建 wasm（跑 python tools/build_wasm.py）")
    脚本 = tmp_path / "m196.js"
    脚本.write_text(_JS, encoding="utf-8", newline="\n")
    r = subprocess.run(["node", str(脚本), str(SHIM), str(WASM)],
                       capture_output=True, timeout=180)
    if r.returncode != 0:
        pytest.fail(f"驱动脚本挂了：{r.stderr.decode('utf-8', 'replace')[:1500]}")
    文本 = r.stdout.decode("utf-8", "replace").strip()
    try:
        return json.loads(文本)
    except json.JSONDecodeError:
        pytest.fail(f"驱动没输出 JSON：{文本[:1500]}")


@pytest.fixture(scope="module")
def 结果(tmp_path_factory):
    return _跑wasm(tmp_path_factory.mktemp("m196"))


# ---------------------------------------------------------------------------
# 三、离屏 == 直接画（这一站的核心判据）
# ---------------------------------------------------------------------------

def test_离屏画完再贴_指令序列与直接画逐条一致(结果):
    """🔴 **同一条绘制路径，画在主画布上和画在离屏上必须产生一样的指令序列。**

    这条能抓住所有「离屏上画得不一样」的实现错：坐标系忘了算、样式没生效、
    相机没跟过去、`_needCtx` 没走目标…… 而且它是**确定性**的（不看像素、不看时钟）。
    """
    s1, s2 = 结果["s1"], 结果["s2"]
    assert s1["ok"], f"直接画失败：{s1['错']}"
    assert s2["ok"], f"离屏失败：{s2['错']}"

    # 🔴 要排掉 `_g准备` 给**主画布**设的那条 `setTransform(dpr,…)`：
    # 离屏画布按逻辑像素建（`width = 宽`，不乘 DPR），本来就不需要它 ——
    # 这是设计上正确的差异，不是「画得不一样」。
    直 = [x for x in s1["主"] if x[0] != "setTransform"]
    # 离屏那条：主画布上只有「贴到」，图元都在离屏画布上
    离 = None
    for c in s2["画布们"]:
        if c["名"] != "主" and c["记录"]:
            离 = c["记录"]
            break
    assert 离, f"离屏画布上一条指令都没有：{s2['画布们']}"

    assert 直 == 离, ("离屏与直接画的指令序列不同\n--- 直接 ---\n"
                      + json.dumps(直, ensure_ascii=False, indent=1)
                      + "\n--- 离屏 ---\n"
                      + json.dumps(离, ensure_ascii=False, indent=1))

    # `贴到` 本身：主画布要真的收到 drawImage
    贴 = [x for x in s2["主"] if x[0] == "drawImage"]
    assert len(贴) == 1, f"主画布应当收到一条 drawImage，实际 {s2['主']}"
    assert 贴[0][2] == 0 and 贴[0][3] == 0, f"贴的位置不对：{贴[0]}"


def test_离屏画布是独立的一块(结果):
    """离屏必须是**独立的画布**，不能和主画布共用一块（共用就会互相擦掉）。"""
    s2 = 结果["s2"]
    名们 = [c["名"] for c in s2["画布们"]]
    assert "主" in 名们
    assert len([n for n in 名们 if n != "主"]) >= 1, f"没建出离屏画布：{名们}"


# ---------------------------------------------------------------------------
# 四、折行 / 像素
# ---------------------------------------------------------------------------

def test_文字折行在宽度内且行数正确(结果):
    """假 canvas 的 `measureText` 是**每字 8px** ⇒ 宽 80 正好放 10 个字。

    这条同时验两件事：① 每行都不超宽；② 内容没丢（拼起来等于原文）。
    """
    s3 = 结果["s3"]
    assert s3["ok"], s3["错"]
    行 = [x for x in s3["出"].splitlines() if x.startswith("[")]
    assert 行, f"一行都没折出来：{s3['出']}"
    文们 = [x[1:-1] for x in 行]
    assert len(文们) == 3, f"29 个字按每行 10 个应当 3 行，实际 {len(文们)}：{文们}"
    for 段 in 文们[:-1]:
        assert len(段) == 10, f"非末行应当正好 10 个字：{段!r}"
    assert "".join(文们) == "这是一段很长的中文文本需要折行显示才能放得下这个宽度"


def test_文字折行_英文在空格处断(结果):
    """英文不能把单词劈开：断点回退到最近的空格。"""
    s3 = 结果["s3"]
    行 = [x for x in s3["出"].splitlines() if x.startswith("<")]
    assert 行, f"英文那组没折出行：{s3['出']}"
    文们 = [x[1:-1] for x in 行]
    for 段 in 文们:
        assert not 段.startswith(" "), f"行首有空格：{段!r}"
        assert not 段.endswith(" "), f"行尾有空格：{段!r}"
    # 每个词都要完整出现（没有被劈成两半）
    for 词 in ("hello", "world", "foo", "bar", "baz", "qux"):
        片段 = " ".join(文们)
        assert 词 in 片段, f"单词「{词}」被劈开了：{文们}"


def test_离屏像素读回的颜色(结果):
    """假 canvas 的 `getImageData(x, y)` 返回 `[x&255, y&255, 7, 255]`。

    这条验的是**读回链路**（句柄 → 那块画布的 ctx → 数组 → 基石列表），
    而不是颜色本身 —— 颜色要真 canvas 才验得了（判据跑在 CI 上）。
    """
    s4 = 结果["s4"]
    assert s4["ok"], s4["错"]
    assert "[3, 7, 7, 255]" in s4["出"], f"像素读回不对：{s4['出']}"


# ---------------------------------------------------------------------------
# 五、路径 / 渐变 / 裁剪 / 混合
# ---------------------------------------------------------------------------

def _主记录(结果键) -> list:
    return 结果键["主"]


def test_路径按顺序重放(结果):
    """`移到/线到/闭合` 存成数据，填充时按顺序重放 —— 顺序错了形状就不对。"""
    s5 = 结果["s5"]
    assert s5["ok"], s5["错"]
    名们 = [x[0] for x in _主记录(s5)]
    # 第一次填充路径
    起 = 名们.index("beginPath")
    段 = 名们[起:起 + 6]
    assert 段 == ["beginPath", "moveTo", "lineTo", "lineTo", "closePath", "fill"], 段
    # 描边那次也得重放一遍（路径是数据，可以反复用）
    assert 名们.count("moveTo") == 3, f"三条 moveTo（填充一次 + 描边两次）：{名们}"
    assert "arcTo" in 名们, f"`弧到` 没进指令序列：{名们}"


def test_渐变造出真渐变对象并上色标(结果):
    """`线性渐变` / `径向渐变` 要真的建 CanvasGradient 并加色标，
    而且能当 `填充` 用（矩形那次 fillStyle 应当是渐变对象）。"""
    s6 = 结果["s6"]
    assert s6["ok"], s6["错"]
    名们 = [x[0] for x in _主记录(s6)]
    assert "linearGradient" in 名们, f"没建线性渐变：{名们}"
    assert "radialGradient" in 名们, f"没建径向渐变：{名们}"
    assert 名们.count("色标") == 4, f"两次渐变各两个色标，实际 {名们.count('色标')}"
    填 = [x for x in _主记录(s6) if x[0] == "fillRect"]
    assert 填 and 填[0][1] == "渐变", f"矩形的填充不是渐变：{填}"


def test_裁剪压栈与出栈(结果):
    """`设裁剪` 要 `rect + clip`；`清裁剪` 要 `restore`（否则裁剪一直在）。"""
    s7 = 结果["s7"]
    assert s7["ok"], s7["错"]
    记 = _主记录(s7)
    名们 = [x[0] for x in 记]
    起 = 名们.index("rect")
    assert 记[起] == ["rect", 0, 0, 50, 50], f"裁剪矩形不对：{记[起]}"
    assert 名们[起 + 1] == "clip", f"rect 之后没 clip：{名们}"
    # 清裁剪之后要有 restore，而且第二条矩形在它后面
    修 = 名们.index("restore")
    填们 = [i for i, x in enumerate(记) if x[0] == "fillRect"]
    assert len(填们) == 2, f"应当两次填充：{填们}"
    assert 填们[0] < 修 < 填们[1], f"restore 应当夹在两次填充之间：{名们}"


def test_混合与透明度持久生效(结果):
    """`设混合` / `设透明度` 是**持久状态** —— 后面每一条图元都要带上。

    📌 为什么这条重要：`_画` 每条图元都 `save/restore`，`restore` 会把
    `globalAlpha` / `globalCompositeOperation` 弹回去。所以必须每帧重新应用 ——
    漏了就是「设了只管一条」，而画面上只是「后几个不太透明」，很难看出来。
    """
    s8 = 结果["s8"]
    assert s8["ok"], s8["错"]
    记 = [x for x in _主记录(s8) if x[0] == "fillRect"]
    assert len(记) >= 1, f"应当至少一次填充：{_主记录(s8)}"
    for x in 记:
        assert x[2] == 0.5, f"透明度没生效（应 0.5）：{x}"
        assert x[3] == "multiply", f"混合模式没生效（应 multiply）：{x}"


def test_写段落逐行画出(结果):
    """`写段落` 折几行就画几次，而且 y 按行距递增。"""
    s9 = 结果["s9"]
    assert s9["ok"], s9["错"]
    文 = [x for x in _主记录(s9) if x[0] == "fillText"]
    assert len(文) >= 2, f"应当折成多行：{文}"
    ys = [x[3] for x in 文]
    assert ys == sorted(ys), f"行的 y 应当递增：{ys}"
    assert ys[1] - ys[0] == 20, f"行距应当是 20：{ys}"


# ---------------------------------------------------------------------------
# 六、错误要说人话
# ---------------------------------------------------------------------------

def test_句柄错了要报中文错(结果):
    s10 = 结果["s10"]
    assert not s10["ok"], "用了不存在的句柄却没报错"
    assert "离屏画布" in s10["错"], s10["错"]


def test_离屏像素越界要报错并说尺寸(结果):
    s11 = 结果["s11"]
    assert not s11["ok"], "越界读像素却没报错"
    assert "越界" in s11["错"] and "10" in s11["错"], s11["错"]


def test_多清一次裁剪要报错(结果):
    s12 = 结果["s12"]
    assert not s12["ok"], "没设裁剪就清裁剪，却静默通过"
    assert "配对" in s12["错"], s12["错"]


# ---------------------------------------------------------------------------
# 七、Rust 侧的参数校验（命令行跑得到）
# ---------------------------------------------------------------------------

def test_参数边界在Rust上报中文错(tmp_path):
    """这些在 Rust 侧就先挡住了（不用等宿主），文案要是中文且指出问题。"""
    for 句, 关键词 in [
        ("图形.离屏(0, 10)", "大于 0"),
        ("图形.离屏(9000, 10)", "8192"),
        ('图形.文字折行("x", 0)', "最大宽"),
        ('图形.设裁剪({"x": 0, "y": 0, "宽": 10})', "四项"),
        ('图形.设裁剪({"x": 0, "y": 0, "宽": 10, "高": 0})', "大于 0"),
        ('图形.设混合("闪瞎")', "不认识的模式"),
        ("图形.设透明度(1.5)", "0–1"),
        ('图形.线性渐变(0, 0, 1, 1, ["#000"])', "至少要两个色标"),
        ('图形.线性渐变(0, 0, 1, 1, [[1.5, "#000"], [1, "#fff"]])', "0–1"),
        ("图形.径向渐变(0, 0, 10, 5, [\"#000\", \"#fff\"])", "内半径"),
    ]:
        源 = "导入 图形\n" + 句 + "\n"
        if rust_exe_path().is_file():
            出 = _跑(源, tmp_path, "Rust")
            assert 关键词 in 出, f"`{句}` 没报「{关键词}」：{出[:400]}"
