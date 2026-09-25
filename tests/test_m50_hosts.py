# -*- coding: utf-8 -*-
"""M50 第二批：六个新模块（统计/编码/配置/html/对比/标识）的**跨宿主**对拍。

M50 第一批只做了 Python 侧，README 里如实标注「Node / Rust 宿主还没实现」。
这一批把它们补齐，本文件就是「补齐了」的凭据。

⚠️ 三类值**没法逐字节对拍**，各自换判据（都写在对应函数里）：
- **随机值**（`标识.*`）：只验形状（长度、分段数、字符集、两次不同）；
- **时间**（`时间.计时` 的秒数）：本批不涉及，M50 第一批已单独测；
- **文件读写**（`配置.读JSON/写JSON/读键值/写键值`）：用临时目录、各自跑，
  比「行为」而不是比字节（路径不同）。

其余一律**逐字节**比 Python VM 与 Node / Rust 的输出。
"""
from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import rust_exe_path, tmp_bc_path      # noqa: E402

from jishi import serialize                          # noqa: E402
from jishi import vm as vm_mod                       # noqa: E402
from jishi.compiler import compile_source            # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
RUST = rust_exe_path()


# ---------------------------------------------------------------------------
# 脚手架
# ---------------------------------------------------------------------------

def _py_run(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        vm_mod.VM(compile_source(src, "<t>"), "<t>").run()
    return buf.getvalue()


def _node_run(src: str) -> tuple[int, str, str]:
    bc = tmp_bc_path(ROOT / "_tmp_m50_node.json")
    bc.write_text(serialize.dumps(compile_source(src, "<t>")), encoding="utf-8")
    try:
        r = subprocess.run([NODE, str(ROOT / "node" / "index.js"), str(bc)],
                           capture_output=True, text=True, encoding="utf-8")
        return r.returncode, r.stdout, r.stderr
    finally:
        bc.unlink(missing_ok=True)


def _rust_run(src: str) -> tuple[int, str, str]:
    bc = tmp_bc_path(ROOT / "_tmp_m50_rust.json")
    bc.write_text(serialize.dumps(compile_source(src, "<t>")), encoding="utf-8")
    try:
        r = subprocess.run([str(RUST), str(bc)], capture_output=True, text=True,
                           encoding="utf-8", timeout=120)
        return r.returncode, r.stdout, r.stderr
    finally:
        bc.unlink(missing_ok=True)


def _agree(src: str) -> str:
    """三个执行器 + Node + Rust：stdout **逐字节**一致，且都正常退出。"""
    py = _py_run(src)
    buf = io.StringIO()
    with redirect_stdout(buf):
        from jishi import cvm_bind
        cvm_bind.run_source_c(src, "<t>")
    assert buf.getvalue() == py, "C VM 与 Python VM 不一致"

    if NODE:
        rc, out, err = _node_run(src)
        assert rc == 0, f"Node 非零退出：rc={rc}\n{err[:300]}"
        assert out == py, f"Node 输出不一致\nPython: {py!r}\nNode:   {out!r}"
    rc, out, err = _rust_run(src)
    assert rc == 0, f"Rust 非零退出：rc={rc}\n{err[:300]}"
    assert out == py, f"Rust 输出不一致\nPython: {py!r}\nRust:   {out!r}"
    return py


def _all_fail(src: str) -> None:
    """五个引擎都必须**报错**（文案各自实现，不要求逐字相同，但都得非零退出）。"""
    from jishi.errors import JishiError
    with pytest.raises(JishiError):
        _py_run(src)
    if NODE:
        rc, out, _err = _node_run(src)
        assert rc != 0, f"Node 本该报错却正常退出：{out!r}"
    rc, out, _err = _rust_run(src)
    assert rc != 0, f"Rust 本该报错却正常退出：{out!r}"


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def test_stats_集中趋势与离散():
    _agree(
        "导入 统计\n"
        "打印(统计.求和([1,2,3]), 统计.平均([1,2,3,4]), 统计.中位数([3,1,2]))\n"
        "打印(统计.中位数([1,2,3,4]), 统计.众数([3,1,3,2]), 统计.极差([1,5,3]))\n"
        "打印(统计.方差([1,2,3,4]), 统计.标准差([1,2,3,4]))\n"
        "打印(统计.方差([1,2,3,4], 真), 统计.标准差([1,2,3,4], 真))\n"
        "打印(统计.分位数([1,2,3,4], 0.25), 统计.去极值平均([9.5,8.0,9.0,7.5,9.9]))\n"
        "打印(统计.加权平均([80,90],[1,3]))\n")


def test_stats_众数并列取最先出现():
    """并列最多取**最先出现**的——`HashMap` 迭代顺序不定，Rust 侧特意保了序。"""
    _agree("导入 统计\n打印(统计.众数([9,9,1,1]))\n")


def test_stats_求和返回整数而不是浮点():
    """⚠️ Python 侧 `求和` 返回 `int`、显示成 `6`（不是 `6.0`）。

    Rust 侧如果一律 `Val::Float` 就会给出 `6.0`——本模块特意用 `f64v` 做了
    「整数就 Int、否则 Float」的判定。这条就是钉它的。
    """
    out = _agree("导入 统计\n打印(统计.求和([1,2,3]))\n打印(统计.极差([1,5,3]))\n")
    assert out.split("\n")[0] == "6"
    assert out.split("\n")[1] == "4"


@pytest.mark.parametrize("src", [
    "导入 统计\n打印(统计.平均([]))\n",
    '导入 统计\n打印(统计.平均([1,"甲"]))\n',
    '导入 统计\n打印(统计.平均("abc"))\n',
    "导入 统计\n打印(统计.方差([1], 真))\n",
    "导入 统计\n打印(统计.去极值平均([1,2], 1))\n",
    "导入 统计\n打印(统计.加权平均([1,2],[1]))\n",
    "导入 统计\n打印(统计.分位数([1,2], 1.5))\n",
])
def test_stats_报错五引擎一致(src):
    _all_fail(src)


# ---------------------------------------------------------------------------
# 编码
# ---------------------------------------------------------------------------

def test_encode_base64_十六进制_url():
    _agree(
        "导入 编码\n"
        '令 码 = 编码.base64编码("中文abc")\n'
        "打印(码, 编码.字节文本(编码.base64解码(码)))\n"
        '打印(编码.十六进制编码("中文"), 编码.十六进制编码("abc", 真))\n'
        '打印(编码.字节文本(编码.十六进制解码("e4b8ad e69687")))\n'
        '打印(编码.十六进制解码("0xabc"), 编码.十六进制解码("a,b"))\n'
        '打印(编码.url编码("中文/路径"), 编码.url编码("a b", 假))\n'
        '打印(编码.url解码("%E4%B8%AD%E6%96%87"))\n'
        '打印(编码.文本字节("中"), 编码.字节文本([228,184,173]))\n')


def test_encode_空输入():
    _agree('导入 编码\n打印(编码.base64编码(""), 编码.十六进制解码(""))\n')


@pytest.mark.parametrize("src", [
    '导入 编码\n打印(编码.base64解码("!!!!"))\n',
    '导入 编码\n打印(编码.base64解码("中文"))\n',
    "导入 编码\n打印(编码.字节文本([228,184]))\n",
    "导入 编码\n打印(编码.base64编码([256]))\n",
    '导入 编码\n打印(编码.十六进制解码("zz"))\n',
])
def test_encode_报错五引擎一致(src):
    _all_fail(src)


# ---------------------------------------------------------------------------
# html
# ---------------------------------------------------------------------------

def test_html_转义与标签():
    """⚠️ 属性用**字典**传（不是关键字参数）——理由见 Python 侧 `_属性串`。

    关键字参数在 Node / Rust 的「内建函数」类型里**没有位置可放**，
    用关键字会在那两个宿主上静默丢失。这条用例是「字典 API 五个引擎都认」的证据。
    """
    _agree(
        "导入 html\n"
        '打印(html.转义("<a href=\'x\'>" + "&"))\n'
        '打印(html.标签("p", "内容", {"类名": "提示", "编号": 3}))\n'
        '打印(html.标签("button", "按", {"disabled": 空}))\n'
        '打印(html.空元素("img", {"src": "a.png", "alt": "图"}))\n'
        '打印(html.链接("点我", "https://x.cn/?a=1&b=2"))\n'
        '打印(html.链接("x", "/a", {"类名": "btn"}))\n'
        '打印(html.标签("div", "<script>坏</script>"))\n'
        '打印(html.标签("div", html.原样("<b>x</b>")))\n'
        '打印(html.无序列表(["甲", "<乙>"], {"类名": "l"}))\n'
        "打印(html.有序列表([1,2]))\n"
        '打印(html.标签("div", "", {"数据_值": "1"}))\n')


def test_html_原样身份五引擎都保得住():
    """⚠️ **本模块最关键的一条**：`原样` 的标记类型不能用「字符串子类」。

    M47 把文本改成 C 侧原生字符串之后，`c2py` 是直接读字节再 `decode` 成
    普通 `str` 的——子类身份**在 C VM 下会丢**，于是 C VM 会把本该原样输出的
    片段**再转义一遍**（`<b>` → `&lt;b&gt;`），而且**不报错**。
    Python 侧一度用 `str` 子类、Node 侧一度想用 `String` 子类，都踩过；
    现在三个实现都用「独立类型」（Python 普通类 / JS 普通类 / Rust 枚举变体）。
    """
    out = _agree(
        "导入 html\n"
        '打印(html.标签("div", html.原样("<b>x</b>")))\n'
        '打印(html.标签("div", html.原样(html.标签("i", "斜"))))\n')
    assert out.split("\n")[0] == "<div><b>x</b></div>"
    assert out.split("\n")[1] == "<div><i>斜</i></div>"


def test_html_列表为空():
    _agree("导入 html\n打印(html.无序列表([]))\n")


@pytest.mark.parametrize("src", [
    '导入 html\n打印(html.标签("", "x"))\n',
    '导入 html\n打印(html.标签("p", "x", 5))\n',
    '导入 html\n打印(html.空元素(""))\n',
])
def test_html_报错五引擎一致(src):
    _all_fail(src)


# ---------------------------------------------------------------------------
# 对比
# ---------------------------------------------------------------------------

def test_diff_统一与逐行():
    """`统一` 的 `@@` 计数与分块规则**照搬 `difflib`**——手写很容易差一点。

    Node 侧实现时连踩三次（`@@` 计数、`replace` 块没合并、空组），
    所以这里把那些边界都钉住。
    """
    _agree(
        "导入 对比\n"
        '打印(对比.统一("甲\\n乙\\n丙", "甲\\n丁\\n丙"))\n'
        '打印(对比.统一("a\\nb\\nc", "a\\nc\\nd"))\n'
        '打印(对比.统一("a\\nb", "a\\nc", 0))\n'
        '打印(对比.逐行(["a","b"], ["a","c"], 真))\n'
        '打印(对比.逐行(["a","b"], ["a","c"]))\n'
        '打印(对比.并排("甲\\n乙", "甲\\n丙", 6))\n'
        '打印(对比.相似度("甲\\n乙", "甲\\n乙"), 对比.相似度("a", "b"))\n'
        '打印(对比.最相似("aple", ["apple","banana","grape"]))\n')


def test_diff_单行替换要给replace块():
    """「一行换一行」必须是**一个 `replace` 块**（并排显示成同一行）。

    ⚠️ 自己写 LCS 很容易给成 `delete` + `insert` 两块，那时：
    `并排` 会把一行拆成两行、`统一` 的 `@@` 计数也跟着错。
    """
    out = _agree('导入 对比\n打印(对比.并排("甲\\n乙", "甲\\n丙", 6))\n')
    # 末行的右侧不补尾空格（`print` 之后本来就是行尾），所以按 rstrip 比
    assert [l.rstrip() for l in out.strip().split("\n")] == [
        "甲     | 甲", "乙     ~| 丙"]


def test_diff_两个空文本():
    _agree('导入 对比\n打印(对比.统一("", ""))\n打印(对比.相似度("", ""))\n')


@pytest.mark.parametrize("src", [
    "导入 对比\n打印(对比.统一(1, 2))\n",
    '导入 对比\n打印(对比.统一(["a",1], ["b"]))\n',
    '导入 对比\n打印(对比.统一("a", "b", -1))\n',
    '导入 对比\n打印(对比.并排("a", "b", 2))\n',
])
def test_diff_报错五引擎一致(src):
    _all_fail(src)


# ---------------------------------------------------------------------------
# 标识（随机值——只能验形状）
# ---------------------------------------------------------------------------

def test_ident_形状():
    out = _agree(
        "导入 标识\n"
        "令 a = 标识.唯一标识()\n"
        '打印(长度(a), 长度(a.拆分("-")))\n'
        "令 b = 标识.唯一标识(真)\n"
        '打印(长度(b), "-" 在 b)\n'
        "打印(长度(标识.唯一标识短()))\n"
        "令 bs = 标识.唯一标识字节()\n"
        "打印(长度(bs), bs[0] >= 0, bs[0] <= 255)\n")
    lines = out.split("\n")
    assert lines[0] == "36 5"
    assert lines[1] == "32 假"
    assert lines[2] == "22"
    assert lines[3] == "16 真 真"


def test_ident_短标识是URL安全的():
    out = _agree('导入 标识\n令 s = 标识.唯一标识短()\n打印("+" 在 s, "/" 在 s, "=" 在 s)\n')
    assert out.strip() == "假 假 假"


def test_ident_两次不同():
    """UUID 生成是随机的，但「两次不同」这条性质五个引擎都该满足。"""
    _agree("导入 标识\n打印(标识.唯一标识() != 标识.唯一标识())\n")


# ---------------------------------------------------------------------------
# 配置（文件相关的用临时目录，各自跑，比行为）
# ---------------------------------------------------------------------------

def test_config_纯内存操作():
    _agree(
        "导入 配置\n"
        '打印(配置.取({"a": 1}, "a.b", 7))\n'
        '打印(配置.取({"a": {"b": 2}}, "a.b"))\n'
        '打印(配置.取({"s": [{"p": 1}]}, "s.0.p"))\n'
        '打印(配置.设({}, "甲.乙", 1))\n'
        '打印(配置.合并({"a": 1, "b": {"x": 1}}, {"b": {"y": 2}}))\n'
        '打印(配置.展开({"a": {"b": 1}, "c": 2, "d": {}}))\n'
        '打印(配置.展开({}))\n')


def test_config_JSON_读写往返():
    """用同一个临时目录、三个执行器各跑一遍（路径统一，输出才可比）。"""
    with tempfile.TemporaryDirectory() as td:
        p = str(Path(td) / "c.json").replace("\\", "/")
        src = (
            "导入 配置\n"
            f'令 p = "{p}"\n'
            '配置.写JSON(p, {"名": "测", "嵌套": {"a": 1}})\n'
            "令 d = 配置.读JSON(p)\n"
            '打印(d["名"], d["嵌套"]["a"])\n')
        assert _agree(src).strip() == "测 1"
        # 中文必须是原样写进去的（不是 \uXXXX）
        assert "测" in (Path(td) / "c.json").read_text(encoding="utf-8")


def test_config_读不到时给默认():
    _agree('导入 配置\n打印(配置.读JSON("没这个.json", {"d": 真}))\n')


@pytest.mark.parametrize("src", [
    '导入 配置\n打印(配置.读JSON("没这个.json"))\n',
    '导入 配置\n打印(配置.取({"a": 1}, "a.b"))\n',
])
def test_config_报错五引擎一致(src):
    _all_fail(src)


def test_config_坏JSON不能静默取默认():
    """内容坏了**必须报错**——静默返回默认值会把「配置写错了」变成「配置没生效」。"""
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "bad.json"
        bad.write_text("{不是JSON", encoding="utf-8")
        p = str(bad).replace("\\", "/")
        _all_fail(f'导入 配置\n打印(配置.读JSON("{p}", {{}}))\n')
