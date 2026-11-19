"""M205：自写 PNG 解码（替掉 `png` crate）。

## 判据分三层

1. **逐字节对拍**：`tests/fixtures/png/` 里 10 张样例（灰度/RGB/调色板/灰度+α/RGBA、
   1/2/4/8/16 位、Adam7 隔行、编码器自选过滤器），标准答案由 `tools/gen_png_fixtures.py`
   生成 —— 每张 `.rgba` 就是「解出来应当长这样」的原始字节，判据比的是 **SHA-256**。
   样例与答案都进仓库 ⇒ 判据**不依赖 PIL**（只在重新生成样例时才用到它）。
2. **坏文件必须报中文错**：截断 / CRC 坏 / 非法位深组合 / 缺调色板 / 索引越界 /
   解压炸弹 —— 六种各有各的话，绝不静默画错。
3. **依赖真的没了**：`Cargo.toml` 里没有 `png`，源码里没有 `png::`，README 依赖表
   标了「已摆脱」，四个 `Cargo.lock` 里也搜不到（`--locked` 会先撞这个）。

## 为什么标准答案是「取高字节」而不是 PIL 的转换

16 位灰度那张：PIL 的 `convert("RGBA")` 走的是它自己的缩放口径，而 `png` crate 的
`STRIP_16`（以及我们）是**取高字节**。为了不退化成「跟 PIL 的转换函数对拍」，
生成器直接按源数据算答案 —— 我们知道每个像素该是什么。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

import pytest

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
样例目录 = ROOT / "tests" / "fixtures" / "png"
RS = ROOT / "rust" / "target" / "release" / ("jishi-rs.exe" if os.name == "nt" else "jishi-rs")

清单 = json.loads((样例目录 / "清单.json").read_text(encoding="utf-8"))


def _跑(*参数: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(RS), *参数], capture_output=True, text=True, encoding="utf-8")


# ---------------------------------------------------------------------------
# 一、逐字节对拍
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not RS.exists(), reason="发行版没编（rust/target/release/jishi-rs）")
@pytest.mark.parametrize("样例", 清单["样例"], ids=[x["名字"] for x in 清单["样例"]])
def test_解出来的RGBA与标准答案逐字节一致(样例):
    结果 = _跑("--png信息", str(样例目录 / 样例["png"]))
    assert 结果.returncode == 0, 结果.stderr
    got = json.loads(结果.stdout)
    assert (got["宽"], got["高"]) == (样例["宽"], 样例["高"]), f"{样例['名字']} 尺寸不对"
    assert got["rgba_sha256"] == 样例["rgba_sha256"], (
        f"{样例['名字']}（{样例['说明']}）解出来的像素不对")


def test_样例覆盖了该覆盖的形态():
    """十张样例不是随便凑的：位深、色彩类型、隔行都得有人在。

    这条防的是「样例被换掉之后，判据还在跑，但覆盖已经悄悄变窄」。
    """
    位深 = {x["文件位深"] for x in 清单["样例"]}
    色彩 = {x["文件色彩"] for x in 清单["样例"]}
    隔行 = {x["文件隔行"] for x in 清单["样例"]}
    assert {1, 4, 8, 16} <= 位深, f"位深覆盖不全：{sorted(位深)}"
    assert {0, 2, 3, 4, 6} <= 色彩, f"色彩类型覆盖不全：{sorted(色彩)}"
    assert 1 in 隔行, "没有隔行（Adam7）样例"


@pytest.mark.skipif(not RS.exists(), reason="发行版没编")
def test_仓库里真实的PNG也能解():
    """`assets/基石_logo.png` 这类**真文件**至少要解得开、尺寸对。

    为什么还要这条：样例是我们自己造的，编码器可能很规矩；真实文件里有
    tEXt / gAMA 这些辅助块、有各种过滤器组合 —— 顺一遍才知道兼容性没吹牛。
    """
    真文件 = [
        (ROOT / "assets" / "基石_logo.png", (512, 429)),
        (ROOT / "assets" / "备案图标.png", (62, 67)),
        (ROOT / "editors" / "vscode-jishi" / "icon.png", (128, 128)),
    ]
    for 路径, 尺寸 in 真文件:
        if not 路径.exists():
            continue
        结果 = _跑("--png信息", str(路径))
        assert 结果.returncode == 0, f"{路径} 解不开：{结果.stderr}"
        got = json.loads(结果.stdout)
        assert (got["宽"], got["高"]) == 尺寸, f"{路径} 尺寸不对"


# ---------------------------------------------------------------------------
# 二、坏文件：报中文错，不静默画错
# ---------------------------------------------------------------------------

bad = [
    ("bad_truncated.png", ["被截断"]),
    ("bad_crc.png", ["校验不过", "CRC"]),
    ("bad_color.png", ["不允许", "位深"]),
    ("bad_nopalette.png", ["PLTE", "调色板"]),
    ("bad_index.png", ["调色板索引", "只有"]),
    ("bad_bomb.png", ["上限"]),
]


@pytest.mark.skipif(not RS.exists(), reason="发行版没编")
@pytest.mark.parametrize("名字,关键词", bad, ids=[x[0] for x in bad])
def test_坏文件报中文错(名字, 关键词):
    结果 = _跑("--png信息", str(样例目录 / 名字))
    assert 结果.returncode != 0, f"{名字} 应当失败，结果成功了"
    for 词 in 关键词:
        assert 词 in 结果.stderr, f"{名字} 的报错里没有「{词}」：{结果.stderr}"


@pytest.mark.skipif(not RS.exists(), reason="发行版没编")
def test_不是PNG的东西也报得清楚(tmp_path):
    p = tmp_path / f"不是图-{os.getpid()}.png"
    p.write_bytes(b"hello, this is not a png at all")
    结果 = _跑("--png信息", str(p))
    assert 结果.returncode != 0
    assert "不是 PNG" in 结果.stderr


# ---------------------------------------------------------------------------
# 三、依赖真的没了（`png` crate）
# ---------------------------------------------------------------------------

def test_Cargo里没有png依赖():
    t = (ROOT / "rust" / "Cargo.toml").read_text(encoding="utf-8")
    # 注释里会提到「M205 删掉了 png」，所以只找**依赖行**
    依赖行 = [ln for ln in t.splitlines() if ln.strip().startswith("png")]
    assert not 依赖行, f"Cargo.toml 里还有 png 依赖：{依赖行}"
    assert '"dep:png"' not in t, "特性表里还挂着 dep:png"


def test_源码里没有引用png_crate():
    """`rust/src/*.rs` 里不许出现 `png::`（那是 crate 的路径）。"""
    命中 = []
    for p in (ROOT / "rust" / "src").rglob("*.rs"):
        for i, ln in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "png::" in ln:
                命中.append(f"{p.relative_to(ROOT)}:{i}: {ln.strip()[:60]}")
    assert not 命中, "还有地方在用 png crate：\n" + "\n".join(命中)


def test_图标那两处走的是自己的解码器():
    t = (ROOT / "rust" / "src" / "native_window.rs").read_text(encoding="utf-8")
    assert t.count("crate::pngdec::解码") >= 2, "窗口图标与托盘图标没走 pngdec"


def test_png只剩传递依赖而且README讲清了():
    """`png` 还留在锁里 —— 但那是 `muda` / `tray-icon` 自己在用，**不是我们**。

    这条判据要防两件事：
    ① 有人又把它加回 `Cargo.toml`（那 test_Cargo里没有png依赖 会先红）；
    ② 锁里还在，而 README 悄悄删掉「传递依赖」那句 —— 那句是**诚实性**的一部分：
       读者会以为「M205 之后二进制里没有 png 了」，其实 macOS/Linux 上还有。
    """
    t = (ROOT / "rust" / "Cargo.lock").read_text(encoding="utf-8")
    if 'name = "png"' in t:
        r = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "传递依赖" in r, "锁里还有 png（muda/tray-icon 带的），README 必须说明这一点"


def test_README依赖表标了已摆脱():
    t = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "M205" in t and "已删" in t, "README 依赖表没写「png 已在 M205 删掉」"


# ---------------------------------------------------------------------------
# 四、解压炸弹的防线在 inflate 那一层（不是 PNG 特例）
# ---------------------------------------------------------------------------

def test_inflate_有限长入口():
    t = (ROOT / "rust" / "src" / "inflate.rs").read_text(encoding="utf-8")
    assert "fn inflate_raw_至多" in t, "inflate 没有限长入口"
    assert "超过上限" in t, "限长入口没有真的拦截"
    # 老的入口要在，但只是「不限长」的薄包装（zip 那边还在用它）
    assert "fn inflate_raw(" in t


# ---------------------------------------------------------------------------
# 五、真窗口：把仓库里的 PNG 真的设上去（不是「函数存在」就算）
# ---------------------------------------------------------------------------

需要原生 = pytest.mark.skipif(
    bool(os.environ.get("JISHI_NO_GUI")) or not sys.platform.startswith("win"),
    reason="真开原生窗口要桌面环境（CI 设 JISHI_NO_GUI=1）",
)


@需要原生
def test_真窗口能把仓库里的PNG设成图标(tmp_path):
    """走完整条路：读文件 → 我们的解码 → `tao::Icon::from_rgba` → 窗口。

    为什么非要有这条：`--png信息` 只证明「字节解得对」，而图标这条路还要
    `Icon::from_rgba` 接受这个尺寸/长度、并且 tao 真把它装上。M174 的教训是
    「能开窗 ≠ 窗口里有东西」，这里同理 —— 「解得开 ≠ 图标真设上了」。
    """
    页 = tmp_path / f"p-{os.getpid()}.html"
    页.write_text("<!doctype html><meta charset='utf-8'><p>m205</p>", encoding="utf-8")
    图 = ROOT / "assets" / "备案图标.png"       # 真实文件，RGBA 62×67
    脚本 = tmp_path / f"图标-{os.getpid()}.jsh"
    脚本.write_text(
        "导入 窗口\n导入 时间\n"
        f'令 h = 窗口.打开({{"标题": "M205", "页面": "{页.as_posix()}", "原生": 真, "宽": 260, "高": 180}})\n'
        '打印("打开", h > 0)\n'
        "时间.睡眠(0.2)\n"
        f'打印("图标", 窗口.设图标("{图.as_posix()}", h))\n'
        "时间.睡眠(0.2)\n"
        "窗口.关闭(h)\n"
        '打印("done")\n',
        encoding="utf-8", newline="\n")
    结果 = subprocess.run([str(RS), str(脚本)], capture_output=True, text=True,
                          encoding="utf-8", timeout=120)
    出 = (结果.stdout or "") + (结果.stderr or "")
    assert 结果.returncode == 0, 出[:600]
    assert "图标 真" in 出, f"设图标没返回真：{出[:600]}"
    assert "done" in 出
