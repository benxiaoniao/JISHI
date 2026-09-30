# -*- coding: utf-8 -*-
"""M35：发布前的健壮性收尾。

三件事，各自对应一个**真实踩过**的问题：

1. **类体校验搬到解析期**。M35 实测发现类体里写错东西时，三个执行器给的
   答案各不相同，一个比一个糟：

   | 类体里写了 | 树遍历 | Python VM / C VM |
   |---|---|---|
   | `打印("…")` | 报「不支持 ExprStmt」 | **静默忽略**（副作用没发生） |
   | `x += 5` | 报「不支持 Assign」 | 当成 `x = 5` **静默算错** |
   | `x[0] = 1` | 报「不支持 Assign」 | 抛英文 `'Subscript' object has no attribute 'id'` |
   | `:`（空类占位） | 报「不支持 Pass」 | 静默忽略（**但空类就该这么写**） |

   修法是在**解析期**统一校验（一处覆盖所有执行器，它们都要过 parser），
   顺便让空类占位 `:` 在三个执行器上都合法。

2. **测试临时文件带进程号**。原先往项目根写固定名（`_tmp_bc.json` …），
   同进程内是串行的所以单跑没事，但两个进程同时跑就会互相覆写，
   产生**假失败**（M32 连续三次撞到：几十上百个 `SystemExit: 1`、
   位置每次不同、单跑必过）。现在用 `conftest.tmp_bc_path`。

3. **语言图标要真的能生成、格式要合法**。安装程序与快捷方式都指着它，
   之前一版没有图标（作者提供了 logo 但没接进来）。

   ⚠️ 这里**不测**「图标好不好看」——那是人的判断。只测「文件在、格式对、
   尺寸档位齐」，因为那才是构建会依赖的部分。
"""

from __future__ import annotations

import importlib.util
import io
import struct
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import tmp_bc_path                  # noqa: E402
from jishi import cvm_bind                        # noqa: E402
from jishi import vm as vm_mod                    # noqa: E402
from jishi.compiler import compile_source         # noqa: E402
from jishi.errors import JishiError               # noqa: E402
from jishi.interpreter import Interpreter         # noqa: E402
from jishi.parser import parse                    # noqa: E402
from jishi.tokenizer import tokenize              # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 脚手架
# ---------------------------------------------------------------------------

def _tree(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        Interpreter(filename="<t>").run(
            parse(tokenize(src, "<t>"), src.split("\n"), "<t>"))
    return buf.getvalue()


def _py(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        vm_mod.VM(compile_source(src, "<t>"), "<t>").run()
    return buf.getvalue()


def _c(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        cvm_bind.run_source_c(src, "<t>")
    return buf.getvalue()


def _outcomes(src: str) -> dict:
    """三个执行器各自的结果：正常则给出输出，失败则给出错误第一行。"""
    out = {}
    for label, fn in (("树遍历", _tree), ("Python VM", _py), ("C VM", _c)):
        try:
            out[label] = ("ok", fn(src))
        except JishiError as e:
            out[label] = ("err", str(e).splitlines()[0])
        except Exception as e:                    # noqa: BLE001
            out[label] = ("crash", f"{type(e).__name__}: {e}")
    return out


# ---------------------------------------------------------------------------
# 1. 类体校验：三执行器必须给同一种「答案」
# ---------------------------------------------------------------------------

def test_class_body_placeholder_ok():
    """空类占位（单独一行冒号）三个执行器都要能用。"""
    src = "类 空类：\n    :\n打印(新建 空类())\n"
    assert _tree(src) == _py(src) == _c(src) == "<空类 实例>\n"


def test_class_body_methods_and_vars_ok():
    src = ('类 甲：\n'
           '    计数 = 0\n'
           '    函数 加(自身)：\n'
           '        甲.计数 += 1\n'
           '        返回 甲.计数\n'
           '令 a = 新建 甲()\n'
           'a.加()\n'
           'a.加()\n'
           '打印(甲.计数)\n')
    assert _tree(src) == _py(src) == _c(src) == "2\n"


@pytest.mark.parametrize("body,expect_kw", [
    ('    打印("副作用")\n', "表达式语句"),
    ('    "说明文字"\n', "表达式语句"),
    ('    x = 1\n    x += 5\n', "名字 = 值"),
    ('    x[0] = 1\n', "下标或属性"),
    ('    x.y = 1\n', "下标或属性"),
])
def test_class_body_bad_statement_rejected_everywhere(body, expect_kw):
    """类体里写错东西：**三个执行器都要在解析期报错**，且说的是同一件事。

    修之前这里是「树遍历报错、字节码执行器静默忽略或静默算错」——
    后者对一个把「出问题能快速找到」当北极星的语言来说是不能接受的。
    """
    src = f"类 甲：\n{body}打印(1)\n"
    got = _outcomes(src)
    assert all(kind == "err" for kind, _ in got.values()), got
    for kind, msg in got.values():
        assert expect_kw in msg, got
    # 报错是**解析期**的：连编译都过不去（不是跑到一半才炸）
    with pytest.raises(JishiError):
        compile_source(src, "<t>")


def test_class_body_error_points_at_the_line():
    """错误要标在出错那一行，并且给出「怎么改」的示例。"""
    src = '类 甲：\n    计数 = 0\n    打印("副作用")\n    函数 m(自身)：\n        返回 1\n'
    with pytest.raises(JishiError) as ei:
        compile_source(src, "<t>")
    rendered = str(ei.value)
    assert "3" in rendered                       # 第 3 行（不是类体后面某行）
    assert "表达式语句" in rendered
    # 提示要给可照抄的正确写法，而不是只说「不允许」
    assert "计数 = 0" in rendered, rendered      # 类变量示例
    assert "函数" in rendered, rendered          # 方法示例
    assert "#" in rendered                       # 说明文字该用注释


def test_empty_class_in_all_three_engines_consistent():
    """空类 + 继承 + 方法：一份稍复杂的脚本，三执行器逐字节一致。"""
    src = ('类 基：\n'
           '    :\n'
           '类 子 继承 基：\n'
           '    函数 打招呼(自身)：\n'
           '        返回 "你好"\n'
           '打印(新建 子().打招呼())\n'
           '打印(是实例(新建 子(), 基))\n')
    assert _tree(src) == _py(src) == _c(src) == "你好\n真\n"


# ---------------------------------------------------------------------------
# 2. 测试临时文件：必须带进程号（跨进程不撞）
# ---------------------------------------------------------------------------

def test_tmp_bc_path_is_process_unique():
    """`tmp_bc_path` 要真的带上进程号——这是 M32 那三次假失败的根因。"""
    p = tmp_bc_path(ROOT / "_tmp_probe.json")
    assert p.name == f"_tmp_probe_{__import__('os').getpid()}.json"
    assert p.parent == ROOT


def test_tmp_bc_path_keeps_extension():
    p = tmp_bc_path(ROOT / "x.tar.gz")
    assert p.name.endswith(".gz"), p.name


def test_no_fixed_temp_names_left():
    """全仓不许再有往项目根写**固定名**临时字节码的地方（M35 一次性修完）。

    允许 `tmp_bc_path(...)` 包装的形式；直接 `ROOT / "_tmp_x.json"` 不许。
    """
    offenders = []
    for f in sorted((ROOT / "tests").glob("*.py")):
        for i, line in enumerate(
                f.read_text(encoding="utf-8").split("\n"), 1):
            stripped = line.strip()
            if "_tmp_" not in stripped or stripped.startswith("#"):
                continue
            if "tmp_bc_path" in stripped or "assert" in stripped:
                continue
            if 'ROOT / "_tmp' in stripped or "'_tmp" in stripped and "=" in stripped:
                offenders.append(f"{f.name}:{i}  {stripped}")
    assert not offenders, "还有固定名临时文件：\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# 3. 语言图标：文件在、格式合法、尺寸档位齐
# ---------------------------------------------------------------------------

def test_ico_is_valid_multisize():
    """`assets/基石.ico` 要真的含多档尺寸。

    为什么较真：Windows 在列表视图用 16/24/32、大图标用 256。少哪一档，
    资源管理器就把 256 硬缩下来，边缘发虚——而这是**用户第一眼看到的东西**。
    """
    ico = ROOT / "assets" / "基石.ico"
    assert ico.exists(), "缺 assets/基石.ico——跑 python tools/make_logo.py"
    raw = ico.read_bytes()
    assert raw[:4] == b"\x00\x00\x01\x00", "不是合法的 ICO 头"
    count = struct.unpack_from("<H", raw, 4)[0]
    sizes = set()
    for i in range(count):
        off = 6 + i * 16
        w = raw[off] or 256          # ICO 里 0 表示 256
        sizes.add(w)
    assert {16, 32, 48, 256}.issubset(sizes), f"缺关键尺寸，实际 {sorted(sizes)}"
    assert count >= 5, f"尺寸档位太少：{count}"


def test_icns_is_valid():
    """`assets/基石.icns` 要有合法的 icns 头（macOS 包用）。"""
    icns = ROOT / "assets" / "基石.icns"
    assert icns.exists(), "缺 assets/基石.icns——跑 python tools/make_logo.py"
    raw = icns.read_bytes()
    assert raw[:4] == b"icns"
    total = struct.unpack_from(">I", raw, 4)[0]
    assert total == len(raw), f"icns 长度字段 {total} != 实际 {len(raw)}"


def test_logo_png_present():
    logo = ROOT / "assets" / "基石_logo.png"
    assert logo.exists()
    assert logo.stat().st_size > 1024


def test_installer_references_icon():
    """安装脚本要真的引用图标（不然图标白做）。"""
    iss = (ROOT / "tools" / "installer.iss").read_text(encoding="utf-8")
    assert "SetupIconFile=" in iss, "安装程序没有图标"
    assert "IconFilename:" in iss, "快捷方式没有图标"
    assert "基石.ico" in iss


def test_build_release_wires_platform_icons():
    """发行脚本要按平台挑图标，并把图标资产放进 share/。"""
    src = (ROOT / "tools" / "build_release.py").read_text(encoding="utf-8")
    assert "_icon_args()" in src
    assert src.count("_icon_args()") >= 3        # 定义 1 次 + 两处调用
    assert "基石.ico" in src and "基石.icns" in src
    # Linux 的图标靠桌面项
    assert "基石.desktop" in src


def test_make_logo_generates_all_three(tmp_path):
    """`tools/make_logo.py` 跑一遍要能产出 png/ico/icns（依赖 Pillow）。

    Pillow 只是**开发期**依赖（CI 只装 pytest，见 .github/workflows/ci.yml），
    所以没有就跳过——图标资产本身就是提交进仓库的，结构另有不依赖 Pillow 的
    用例（`test_ico_is_valid_multisize` / `test_icns_is_valid`）把关。
    """
    if importlib.util.find_spec("PIL") is None:
        pytest.skip("本机没装 Pillow（仅开发期依赖），跳过 make_logo 实跑")
    # 产物写进 pytest 的临时目录，不覆盖仓库里的资产
    r = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "make_logo.py"),
         str(ROOT / "assets" / "基石_logo.png"), str(tmp_path / "o.png")],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
    assert r.returncode == 0, r.stderr[-400:]
    assert (tmp_path / "o.png").exists()
    assert (tmp_path / "o.png").stat().st_size > 1024


def test_class_body_pass_like_word_gets_pointed_hint():
    """`通过` 不是关键字——报错要点名说清楚，并给出正确写法（M35）。"""
    with pytest.raises(JishiError) as ei:
        compile_source("类 甲：\n    通过\n", "<t>")
    rendered = str(ei.value)
    assert "通过" in rendered and "不是关键字" in rendered
    # 提示要给正确的空类写法
    assert "冒号" in rendered


def test_class_body_other_exprstmt_does_not_mention_pass_word():
    """「通过」是特判：别的表达式语句不该被误认为它（防止特判外溢）。"""
    with pytest.raises(JishiError) as ei:
        compile_source('类 甲：\n    打印("副作用")\n', "<t>")
    rendered = str(ei.value)
    assert "表达式语句" in rendered
    assert "不是关键字" not in rendered


def test_installer_iss_is_utf8_with_bom():
    """`installer.iss` 必须带 UTF-8 BOM（否则英文 Windows 上中文路径变乱码）。"""
    raw = (ROOT / "tools" / "installer.iss").read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf", "installer.iss 缺 UTF-8 BOM"


def test_build_release_passes_icon_abs_path():
    """图标绝对路径要由构建脚本以 /D 传入，不依赖 .iss 自身编码。"""
    src = (ROOT / "tools" / "build_release.py").read_text(encoding="utf-8")
    assert "/DMyIconFile=" in src


def test_installer_iss_really_compiles(tmp_path):
    """**真的调用 ISCC 编一次** `installer.iss`——语法错必须在这里暴露。

    来由（M35 实测踩到）：引入图标时 `#define MyAppExeName "jishi.exe"` 前
    多了一个引号，于是 `MyAppExeName` 根本没定义；而当时的测试只做**字符串
    断言**（`"SetupIconFile=" in iss`），全绿通过——是本机真跑
    `build_release.py` 才炸出 `Undeclared identifier`。也就是说：
    **只查「文件里有没有这几个字」是抓不到语法错的**。

    没装 Inno Setup 就跳过（CI 的 Windows runner 装了，会真编）。
    """
    from tools.build_release import _find_iscc          # noqa: PLC0415
    iscc = _find_iscc()
    if iscc is None:
        pytest.skip("本机没有 Inno Setup（ISCC.exe），跳过安装脚本编译校验")

    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "占位.txt").write_text("x", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    args = [str(iscc),
            f"/DSourceDir={payload}",
            "/DAppVersion=0.0.0-test",
            f"/DOutputDir={out}"]
    icon = ROOT / "assets" / "基石.ico"
    if icon.exists():
        args.append(f"/DMyIconFile={icon}")
    args.append(str(ROOT / "tools" / "installer.iss"))
    r = subprocess.run(args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(ROOT))
    assert r.returncode == 0, (
        "installer.iss 编译失败：\n" + (r.stdout or "")[-1200:]
        + (r.stderr or "")[-600:])
    assert list(out.glob("*.exe")), "编译成功却没产出安装程序？"
