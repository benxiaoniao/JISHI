# -*- coding: utf-8 -*-
"""M113：`字节` —— 一个新的值类型。

语料（`tests/cases/32_标准库_M113.jsh` + 四条 `error_cases`）管「五引擎逐字节一致」；
这一份管**语料管不到**的那些点：

1. **D79 §三那张语义表逐条**（类型 / 显示 / 长度 / 下标 / 切片 / 拼接 / 重复 /
   遍历 / 真值 / 相等 / 比较 / 包含 / 集合 / 排序 / 字典键）；
2. **显示是有损的**（> 16 个字节要截断）—— 边界 16 / 17 各测一遍；
3. **🔴 加值类型时编译器只抓得住 1 处**（D79 §四）：剩下的语义助手全走 `_ =>`
   兜底臂，**编译器一声不响**。所以这里拿一份「必须显式接字节」的清单，
   按函数名把 `rust/src/lib.rs` 里的函数体抠出来、逐个体检 ——
   这是「别拿「检查是绿的」当「都实现了」」的一条**可复核**的钉子；
4. **标准库的字节面**（编码 / 加密 / 文件 / 数据库 / json）；
5. **语义确实是「一个值一种写法」**（顶层 == 容器内 == `文本()`）。

⚠️ 纪律：临时文件一律在 `tmp_path` 里；不在仓库根读写真实文件。
"""

import io
import os
import re
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from shutil import which

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from conftest import rust_exe_path, tmp_bc_path   # noqa: E402

from oracle.jishi import cvm_bind                 # noqa: E402
from oracle.jishi import serialize                # noqa: E402
from oracle.jishi import vm as vm_mod             # noqa: E402
from oracle.jishi.compiler import compile_source  # noqa: E402
from oracle.jishi.errors import JishiError        # noqa: E402
from oracle.jishi.interpreter import run_source   # noqa: E402

RUST_EXE = rust_exe_path()

语料 = ROOT / "tests" / "cases" / "32_标准库_M113.jsh"
期望 = (ROOT / "tests" / "cases" / "32_标准库_M113.out").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 三种进程内执行器 + 两个宿主（与 test_default_args.py 同一套写法）
# ---------------------------------------------------------------------------

def _树(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        run_source(src, filename="<t>")
    return buf.getvalue()


def _pvm(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        vm_mod.VM(compile_source(src, "<t>"), "<t>").run()
    return buf.getvalue()


def _cvm(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        cvm_bind.run_source_c(src, "<t>")
    return buf.getvalue()


def _三一致(src: str) -> str:
    got = {"树遍历": _树(src), "Python VM": _pvm(src), "C VM": _cvm(src)}
    assert len(set(got.values())) == 1, f"三执行器输出不一致：{got}"
    return next(iter(got.values()))


#: `编码.十六进制解码` 是「在源码里造一串字节」的唯一写法（**故意没有字面量**，
#: 见 D79 §一）。测试里到处要用，抽个前缀。
前缀 = "导入 编码\n导入 json\n"


def _跑(src: str) -> str:
    """跑一小段基石源码（自动补上 `导入 编码 / json`）。"""
    return _三一致(前缀 + src)


# ---------------------------------------------------------------------------
# 一、D79 §三：语义表逐条
# ---------------------------------------------------------------------------

def test_类型是字节():
    assert _跑('令 b = 编码.十六进制解码("0CFF00")\n打印(类型(b))\n') == "字节\n"


def test_显示形态():
    assert _跑('打印(编码.十六进制解码("0CFF00"))\n') == "字节(0C FF 00)\n"


def test_空字节的显示与类型():
    assert _跑('令 无 = 编码.十六进制解码("")\n'
               '打印(无, 长度(无), 类型(无))\n') == "字节() 0 字节\n"


def test_长度与下标():
    # 下标给的是**整数** 0-255（不是再切出一串单字节）
    assert _跑('令 b = 编码.十六进制解码("0CFF00")\n'
               '打印(长度(b), b[0], b[1], b[2], b[-1])\n') == "3 12 255 0 0\n"


def test_切片给字节():
    assert _跑('令 b = 编码.十六进制解码("0CFF0041")\n'
               '打印(b[1:3], 类型(b[1:3]))\n') == "字节(FF 00) 字节\n"


def test_拼接只能字节加字节():
    assert _跑('打印(编码.十六进制解码("0C") + 编码.十六进制解码("FF"))\n') \
        == "字节(0C FF)\n"


def test_拼接别的类型明确报错():
    """D79：字节只能和字节拼 —— 报错比「猜用户想干嘛」强。"""
    with pytest.raises(JishiError) as ei:
        _树(前缀 + '打印(编码.十六进制解码("0C") + "甲")\n')
    assert "字节" in str(ei.value)


def test_重复():
    assert _跑('令 b = 编码.十六进制解码("0CFF")\n'
               '打印(b * 2, 2 * b, b * 0, b * -3)\n') \
        == ("字节(0C FF 0C FF) 字节(0C FF 0C FF) 字节() 字节()\n")


def test_遍历给整数():
    assert _跑('令 出 = []\n'
               '遍历 x 在 编码.十六进制解码("414243"):\n    出.追加(x)\n'
               '打印(出)\n') == "[65, 66, 67]\n"


def test_真值_空字节是假():
    assert _跑('令 无 = 编码.十六进制解码("")\n'
               '如果 无:\n    打印("真")\n否则:\n    打印("假")\n'
               '如果 编码.十六进制解码("00"):\n    打印("非空为真")\n') \
        == "假\n非空为真\n"


def test_相等_跨类型给假不报错():
    """D79：`字节 == 文本` 给**假**（Python 的 bytes 也这样），不是报错。"""
    assert _跑('令 b = 编码.十六进制解码("0C")\n'
               '打印(b == 编码.十六进制解码("0C"), b == "甲", b != 编码.十六进制解码("FF"))\n') \
        == "真 假 真\n"


def test_比大小_只在字节之间():
    assert _跑('令 小 = 编码.十六进制解码("00")\n令 大 = 编码.十六进制解码("FF")\n'
               '打印(小 < 大, 大 > 小, 小 <= 小)\n') == "真 真 真\n"

    with pytest.raises(JishiError) as ei:
        _树(前缀 + '打印(编码.十六进制解码("0C") < "文本")\n')
    assert "不能比较" in str(ei.value)


def test_成员测试():
    # 字节在字节 = 子串；整数在字节 = 有没有这个字节值（与 Python 一致）
    assert _跑('令 b = 编码.十六进制解码("0CFF00")\n'
               '打印(编码.十六进制解码("0CFF") 在 b, 255 在 b, 1 在 b)\n') \
        == "真 真 假\n"


def test_可以当集合成员与字典键():
    """不可变 ⇒ 可哈希。两个内容相同的字节要落到同一个集合元素上。"""
    assert _跑('令 b = 编码.十六进制解码("0CFF")\n'
               '令 c = 编码.十六进制解码("0CFF")\n'
               '令 集 = 集合([b, c])\n'
               '打印(长度(集), b 在 集)\n') == "1 真\n"


def test_排序():
    assert _跑('令 列 = [编码.十六进制解码("FF"), 编码.十六进制解码(""), 编码.十六进制解码("00")]\n'
               '列.排序()\n打印(列)\n') == "[字节(), 字节(00), 字节(FF)]\n"


def test_是同一个_按值():
    """不可变标量按值判同一性（与「文本」「数字」同一条规矩）。"""
    assert _跑('令 b = 编码.十六进制解码("0CFF")\n'
               '令 c = 编码.十六进制解码("0CFF")\n'
               '打印(b 是 c)\n') == "真\n"


# ---------------------------------------------------------------------------
# 二、显示是有损的（> 16 个字节截断）—— 边界要测两边
# ---------------------------------------------------------------------------

def _十六进制(n: int) -> str:
    return "".join(f"{i:02X}" for i in range(n))


def test_显示_十六个不截断():
    期望文本 = "字节(" + " ".join(f"{i:02X}" for i in range(16)) + ")\n"
    assert _跑(f'打印(编码.十六进制解码("{_十六进制(16)}"))\n') == 期望文本


def test_显示_十七个才截断():
    头 = " ".join(f"{i:02X}" for i in range(16))
    assert _跑(f'打印(编码.十六进制解码("{_十六进制(17)}"))\n') \
        == f"字节({头} … 共 17 个字节)\n"


def test_截断后仍然要能拿到全量():
    源码 = f'令 b = 编码.十六进制解码("{_十六进制(20)}")\n'
    assert _跑(源码 + '打印(长度(编码.十六进制编码(b)))\n') == "40\n"


# ---------------------------------------------------------------------------
# 三、🔴 D79 §四：编译器只抓得住 1 处 —— 拿清单把其余助手钉住
# ---------------------------------------------------------------------------

#: `rust/src/lib.rs` 里**必须显式接字节**的语义助手。
#:
#: 实测：给 `Val` 加一个变体，`cargo check --release` **只报 1 处**（`display`，
#: 它没有兜底臂）；其余全走 `_ =>`，编译器**一声不响**。漏掉的后果全是静默的：
#: `truthy` 漏了 ⇒ 空字节被当成真；`type_label` 漏了 ⇒ `类型()` 给「其他」；
#: `set_key` 漏了 ⇒ 字典键退化成线性扫。
#:
#: ⚠️ 这份清单就是 D79 §四要的那条「可复核的钉子」。**加新的语义助手时也加进来。**
_必须显式接字节 = [
    "display",            # 显示
    "type_label",         # 类型()
    "truthy",             # 真值
    "dict_key",           # 字典键归一
    "set_key",            # 集合元素归一
    "contains",           # 在 / 不在
    "identity_key",       # 是 / 不是
    "container_len",      # 遍历护栏 / 长度
    "as_iterable",        # 总和 / 最大 / 最小 取序列
    "make_iterator",      # 遍历
    "get_item",           # 下标 / 切片
    "numeric_add",        # +
    "numeric_mul",        # *
    "try_cmp_vals",       # 排序 / 最值
    "compare",            # < > <= >=
]


def _函数体(源: str, 名字: str) -> str:
    """按函数名抠出**自由函数**的函数体（跳过同名方法；花括号配平）。

    ⚠️ 必须跳过 `fn 名字(&self, …)`：`contains` 这类名字既有自由函数
    （`contains(容器, 项)`）又有同名的内部方法，`re.search` 会先撞上方法。
    """
    m = re.search(rf"\bfn {re.escape(名字)}\s*\(\s*(?!&?self\b)", 源)
    if not m:
        return ""
    i = 源.find("{", m.end() - 1)
    if i < 0:
        return ""
    深 = 0
    for j in range(i, len(源)):
        if 源[j] == "{":
            深 += 1
        elif 源[j] == "}":
            深 -= 1
            if 深 == 0:
                return 源[i:j + 1]
    return ""


@pytest.mark.parametrize("名字", _必须显式接字节)
def test_Rust语义助手都显式接了字节(名字):
    源 = (ROOT / "rust" / "src" / "lib.rs").read_text(encoding="utf-8")
    体 = _函数体(源, 名字)
    assert 体, f"rust/src/lib.rs 里找不到函数 {名字}（改名了？清单要跟着改）"
    assert "Bytes" in 体, (
        f"`{名字}` 没有显式接 `Val::Bytes` —— 它会走 `_ =>` 兜底臂，"
        f"**编译器抓不住**，行为会静默出错（见 docs/设计决策.md D79 §四）")


def test_Rust的相等与比较也接了字节():
    源 = (ROOT / "rust" / "src" / "lib.rs").read_text(encoding="utf-8")
    assert re.search(r"\(Val::Bytes\(a\), Val::Bytes\(b\)\)", 源), \
        "`impl PartialEq for Val` 少了 `(Bytes, Bytes)` 分支"


# ---------------------------------------------------------------------------
# 四、标准库的字节面
# ---------------------------------------------------------------------------

def test_编码_解码方向一律给字节():
    assert _跑('打印(类型(编码.文本字节("甲")), 类型(编码.十六进制解码("0C")),'
               '类型(编码.base64解码("DAA=")))\n') == "字节 字节 字节\n"


def test_编码_往返():
    assert _跑('打印(编码.十六进制编码(编码.文本字节("中"), 真))\n') == "E4B8AD\n"
    assert _跑('打印(编码.字节文本(编码.base64解码(编码.base64编码("中文"))))\n') \
        == "中文\n"


def test_编码_解不出UTF8要明确报错():
    with pytest.raises(JishiError) as ei:
        _树(前缀 + '打印(编码.字节文本(编码.十六进制解码("FF")))\n')
    文 = str(ei.value)
    assert "不是合法的 UTF-8 文本" in 文
    # ⚠️ 不许把宿主的 codec 细节（英文）漏出来 —— 文案要与 Rust / Node 逐字相同
    assert "codec" not in 文 and "0xff" not in 文


def test_加密_安全随机字节给字节():
    out = _三一致(前缀 + '导入 加密\n'
                  '令 盐 = 加密.安全随机字节(16)\n打印(类型(盐), 长度(盐))\n')
    assert out == "字节 16\n"


def test_加密_口令摘要吃字节盐():
    """盐是字节时，口令摘要要拿**原始字节**去算 —— 不是拿它的显示文本。

    这条是 M113 改 `安全随机字节` 的返回类型时最容易漏的地方：
    `_字节()` 若不接字节，`jishi_repr` 会把「字节(0C FF…)」那串字拿去当盐。
    """
    源码 = ('导入 加密\n导入 编码\n'
            '令 盐 = 编码.十六进制解码("00010203040506070809")\n'
            '打印(加密.口令摘要("pw", 盐, 1000) == 加密.口令摘要("pw", 盐, 1000))\n'
            '打印(加密.口令摘要("pw", 盐, 1000) == 加密.口令摘要("pw", "别的", 1000))\n')
    assert _三一致(源码) == "真\n假\n"


def test_文件_读写字节往返(tmp_path):
    buf = io.StringIO()
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        源码 = ('导入 编码\n导入 文件\n导入 路径\n'
                '令 b = 编码.十六进制解码("89504E470D0A1A0A")\n'
                '文件.写字节("a.bin", b)\n'
                '打印(文件.读字节("a.bin") == b, 文件.文件大小("a.bin"))\n')
        with redirect_stdout(buf):
            run_source(源码, filename="<t>")
    finally:
        os.chdir(cwd)
    assert buf.getvalue() == "真 8\n"


def test_数据库_BLOB往返(tmp_path):
    buf = io.StringIO()
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        源码 = ('导入 编码\n导入 数据库\n'
                '令 库 = 数据库.打开(":memory:")\n'
                '数据库.建表(库, "图", ["内容"])\n'
                '令 b = 编码.十六进制解码("00FF7F80")\n'
                '数据库.插入(库, "图", {"内容": b})\n'
                '令 行 = 数据库.查询(库, "图")[0]\n'
                '打印(类型(行["内容"]), 行["内容"] == b)\n')
        with redirect_stdout(buf):
            run_source(源码, filename="<t>")
    finally:
        os.chdir(cwd)
    assert buf.getvalue() == "字节 真\n"


def test_json_放不了字节():
    with pytest.raises(JishiError) as ei:
        _树(前缀 + '打印(json.转文本(编码.十六进制解码("0C")))\n')
    assert "JSON 里放不了字节" in str(ei.value)


def test_数值聚合不许吃字节():
    """字节是可迭代的、逐个给整数 —— 不拦的话 `总和(字节)` 会**静默**算字节值之和。"""
    for 调用 in ("总和(编码.十六进制解码(\"0102\"))",
                 "最大(编码.十六进制解码(\"0102\"))",
                 "最小(编码.十六进制解码(\"0102\"))"):
        with pytest.raises(JishiError) as ei:
            _树(前缀 + f"打印({调用})\n")
        assert "字节" in str(ei.value)


def test_下标越界文案():
    with pytest.raises(JishiError) as ei:
        _树(前缀 + '打印(编码.十六进制解码("0102")[9])\n')
    assert "字节下标越界" in str(ei.value)


# ---------------------------------------------------------------------------
# 五、一个值一种写法（§7.15）：顶层 == 容器内 == `文本()`
# ---------------------------------------------------------------------------

def test_三种写法一致():
    源码 = ('令 b = 编码.十六进制解码("0CFF00")\n'
            '打印(b)\n'
            '打印(文本(b))\n'
            '打印([b])\n'
            '打印([b, 编码.十六进制解码("")])\n')
    出 = _跑(源码).splitlines()
    assert 出[0] == 出[1] == "字节(0C FF 00)"
    assert 出[2] == "[字节(0C FF 00)]"
    # 空字节在容器里也是 `字节()`（不像文本那样加引号）
    assert 出[3] == "[字节(0C FF 00), 字节()]"


# ---------------------------------------------------------------------------
# 六、跨引擎：语料在 Rust / Node 上也逐字节一致
# ---------------------------------------------------------------------------

def _跑宿主(命令: list[str]) -> tuple[str, str]:
    r = subprocess.run(命令, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    return r.stdout, r.stderr


@pytest.mark.skipif(not RUST_EXE.exists(), reason="没有 Rust 引擎产物")
def test_Rust引擎与黄金一致():
    out, _ = _跑宿主([str(RUST_EXE), str(语料)])
    assert out == 期望


def test_Node引擎与黄金一致():
    node = which("node")
    if node is None:
        pytest.skip("环境里没有 node")
    bc = tmp_bc_path(ROOT / "_tmp_m113_bc.json")
    bc.write_text(serialize.dumps(compile_source(语料.read_text(encoding="utf-8"),
                                                 str(语料))),
                  encoding="utf-8")
    node_index = ROOT / "oracle" / "node" / "index.js"
    out, _ = _跑宿主([node, str(node_index), str(bc)])
    assert out == 期望
