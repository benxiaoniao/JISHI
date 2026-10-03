# -*- coding: utf-8 -*-
"""M50：标准库加强（6 个新模块 + 3 个既有模块补缺）。

这一批的验收标准是「**真实项目里少了几处绕道写法**」，所以测试分两层：

1. **行为测试**（本文件）：每个函数的主要路径 + 边界 + 报错措辞；
2. **三执行器对拍**：由 `tests/test_cvm.py` 的机制保证——本文件里凡是有
   「结果可能不确定」的（`时间.计时` 的秒数、`标识` 的随机值）都做了归一化
   或只断言形状，**可复现的部分一律断言精确值**。

⚠️ 有一条**语言语义**的坑值得写在这里，免得后来人重踩：
`类型(x) 是 小数` **恒为假**——`类型()` 返回的是**文本**（`"小数"`），
拿它跟「小数」这个**类型名**比当然不等。要判类型得用 `类型(x) == "小数"`
或直接判值（`x > 0`）。M50 写测试时踩过一次。
"""
import io
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jishi.interpreter import run_source  # noqa: E402


def run_capture(src: str) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        run_source(src, filename="<测试>")
    return out.getvalue()


def run_err(src: str) -> str:
    try:
        run_capture(src)
    except BaseException as e:          # noqa: BLE001
        return str(e)
    raise AssertionError("这段程序本该报错，却跑通了")


# -- 统计 -------------------------------------------------------------------

def test_统计_集中趋势():
    out = run_capture(
        "导入 统计\n"
        '令 数据 = [1, 2, 3, 4, 1000]\n'
        "打印(统计.求和(数据), 统计.平均(数据))\n"
        "打印(统计.中位数(数据))\n"
        "打印(统计.中位数([3, 1, 2]))\n")
    assert out.split() == ["1010", "202.0", "3", "2"]


def test_统计_离散程度():
    out = run_capture(
        "导入 统计\n"
        "打印(统计.极差([1, 5, 3]))\n"
        "打印(统计.方差([1, 2, 3, 4]))\n"
        "打印(统计.方差([1, 2, 3, 4], 真))\n")
    lines = out.split()
    assert lines[0] == "4"
    assert abs(float(lines[1]) - 1.25) < 1e-9      # 总体方差
    assert abs(float(lines[2]) - 1.6666666666666667) < 1e-9   # 样本方差


def test_统计_众数与加权():
    out = run_capture(
        "导入 统计\n"
        "打印(统计.众数([3, 1, 3, 2]))\n"
        "打印(统计.加权平均([80, 90], [1, 3]))\n"
        "打印(统计.去极值平均([9.5, 8.0, 9.0, 7.5, 9.9]))\n")
    lines = out.split()
    assert lines[0] == "3"
    assert lines[1] == "87.5"
    assert abs(float(lines[2]) - 8.833333333333334) < 1e-9


def test_统计_分位数线性插值():
    out = run_capture(
        "导入 统计\n"
        "打印(统计.分位数([1, 2, 3, 4], 0.25))\n"
        "打印(统计.分位数([1, 2, 3, 4], 0.5))\n"
        "打印(统计.分位数([1, 2, 3, 4], 1))\n")
    assert out.split() == ["1.75", "2.5", "4"]


def test_统计_众数并列取最先出现():
    """并列最多时返回**最先出现**的——保证结果可复现，不随字典顺序漂。"""
    out = run_capture("导入 统计\n打印(统计.众数([9, 9, 1, 1]))\n")
    assert out.strip() == "9"


def test_统计_空数据与坏输入给中文报错():
    assert "至少有一个数字" in run_err("导入 统计\n打印(统计.平均([]))\n")
    assert "不是数字" in run_err('导入 统计\n打印(统计.平均([1, "甲"]))\n')
    assert "不能传文本" in run_err('导入 统计\n打印(统计.平均("abc"))\n')
    assert "至少要 2 个数" in run_err(
        "导入 统计\n打印(统计.方差([1], 真))\n")
    assert "不能是负数" in run_err(
        "导入 统计\n打印(统计.去极值平均([1, 2], -1))\n")
    assert "就没数据了" in run_err(
        "导入 统计\n打印(统计.去极值平均([1, 2], 1))\n")


def test_统计_加权平均个数不符():
    err = run_err("导入 统计\n打印(统计.加权平均([1, 2], [1]))\n")
    assert "个数不一样" in err
    err = run_err("导入 统计\n打印(统计.加权平均([1], [0]))\n")
    assert "权重之和是 0" in err


# -- 编码 -------------------------------------------------------------------

def test_编码_base64往返():
    out = run_capture(
        "导入 编码\n"
        '令 原 = "中文abc"\n'
        "令 码 = 编码.base64编码(原)\n"
        "打印(码)\n"
        "打印(编码.字节文本(编码.base64解码(码)))\n")
    assert out.split() == ["5Lit5paHYWJj", "中文abc"]


def test_编码_十六进制宽容输入():
    """从别处抄来的十六进制常带空格 / `0x` / 逗号——都认。"""
    out = run_capture(
        "导入 编码\n"
        '打印(编码.十六进制编码("中文"))\n'
        '打印(编码.十六进制编码("abc", 真))\n'
        # 中文字的 UTF-8 字节是 e4b8ade69687（4e2d 是「N」，不是中文）
        '打印(编码.字节文本(编码.十六进制解码("e4b8ad e69687")))\n'
        '打印(编码.十六进制解码("0xabc"))\n'
        '打印(编码.十六进制解码("a,b"))\n')
    lines = out.split("\n")
    assert lines[0] == "e4b8ade69687"
    assert lines[1] == "616263"
    assert lines[2] == "中文"
    # "0xabc" → 去掉 0x 后是 "abc"，**奇数长度前面补 0** → 0abc
    assert lines[3] == "[10, 188]"
    # "a,b" → 去掉逗号是 "ab" → [171]（不是 "a","b" 两个半字节）
    assert lines[4] == "[171]"


def test_编码_url与斜杠保留():
    out = run_capture(
        "导入 编码\n"
        '打印(编码.url编码("中文/路径"))\n'
        '打印(编码.url编码("a b", 假))\n'
        '打印(编码.url解码("%E4%B8%AD%E6%96%87"))\n')
    lines = out.split("\n")
    assert lines[0] == "%E4%B8%AD%E6%96%87/%E8%B7%AF%E5%BE%84"   # 保留 /
    assert lines[1] == "a%20b"
    assert lines[2] == "中文"


def test_编码_文本与字节互转():
    out = run_capture(
        "导入 编码\n"
        '令 bs = 编码.文本字节("中")\n'
        "打印(bs, 编码.字节文本(bs))\n")
    assert out.split("\n")[0] == "[228, 184, 173] 中"


def test_编码_坏输入报错不返回乱码():
    """解不出来就明确报错——**返回乱码比报错更难查**。"""
    # 中文 → 先撞上「非 ASCII」这条（比「不是 base64」更准确）
    assert "非 ASCII" in run_err(
        '导入 编码\n打印(编码.base64解码("中文"))\n')
    # 纯 ASCII 但内容非法 → 才轮到「不是合法的 base64」
    assert "不是合法的 base64" in run_err(
        '导入 编码\n打印(编码.base64解码("!!!!"))\n')
    assert "不是合法的 UTF-8" in run_err(
        "导入 编码\n打印(编码.字节文本([228, 184]))\n")
    assert "超出范围" in run_err("导入 编码\n打印(编码.base64编码([256]))\n")
    assert "不是 0-255" in run_err('导入 编码\n打印(编码.base64编码(["a"]))\n')
    assert "不是合法的十六进制" in run_err(
        '导入 编码\n打印(编码.十六进制解码("zz"))\n')


# -- html -------------------------------------------------------------------

def test_html_转义五个字符():
    out = run_capture('导入 html\n打印(html.转义("<a href=\'x\'>" + "&"))\n')
    assert out.strip() == "&lt;a href=&#x27;x&#x27;&gt;&amp;"


def test_html_标签与属性():
    """⚠️ 属性用**字典**传（M50 第二批改的 API）。

    最初是关键字参数（`标签("p", "内容", 类名="提示")`），但**两个宿主的
    「内建函数」类型都没有放关键字的位置**（Node `fn(...args)`、
    Rust `fn(&[Val], &mut VM)`）——用关键字在那边会**静默丢失全部属性**。
    改成字典后五引擎一致（跨宿主对拍在 `tests/test_m50_hosts.py`）。
    """
    out = run_capture(
        "导入 html\n"
        '打印(html.标签("p", "内容", {"类名": "提示", "编号": 3}))\n'
        '打印(html.标签("button", "按", {"disabled": 空}))\n'
        '打印(html.空元素("img", {"src": "a.png", "alt": "图"}))\n')
    lines = out.split("\n")
    assert lines[0] == '<p class="提示" 编号="3">内容</p>'
    assert lines[1] == "<button disabled>按</button>"
    assert lines[2] == '<img src="a.png" alt="图" />'


def test_html_内容与属性都转义():
    out = run_capture(
        "导入 html\n"
        '打印(html.标签("div", "<script>坏</script>"))\n'
        '打印(html.标签("a", "x", {"href": "a\\"b"}))\n'
        '打印(html.链接("点我", "https://x.cn/?a=1&b=2"))\n')
    lines = out.split("\n")
    assert lines[0] == "<div>&lt;script&gt;坏&lt;/script&gt;</div>"
    assert lines[1] == '<a href="a&quot;b">x</a>'
    assert lines[2] == '<a href="https://x.cn/?a=1&amp;b=2">点我</a>'


def test_html_原样不转义():
    out = run_capture(
        "导入 html\n"
        '打印(html.标签("div", html.原样(html.标签("b", "粗"))))\n')
    assert out.strip() == "<div><b>粗</b></div>"


def test_html_原样身份在三个执行器上都保得住():
    """⚠️ **这条是本模块最关键的一个回归**。

    `原样` 标记**不能**用 `str` 子类实现：M47 把文本改成 C 侧原生字符串之后，
    `c2py` 是直接读字节再 `decode` 成普通 `str`，子类的身份在 **C VM 下会丢**
    （树遍历/Python VM 认得、C VM 不认得）——那会让 C VM 把本该原样输出的片段
    **又转义一遍**（`<b>` 变成 `&lt;b&gt;`），而且**不报错**。
    所以这里**必须**用三执行器对拍，单测树遍历是抓不到的。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from jishi import cvm_bind
    from jishi import vm as vm_mod

    src = ('导入 html\n'
           '令 r = html.原样("<i>x</i>")\n'
           '打印(html.标签("div", r))\n'
           '打印(html.标签("p", html.原样(html.标签("b", "粗"))))\n')
    outs = []
    for fn in (run_source, vm_mod.run_source, cvm_bind.run_source_c):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn(src, filename="<原样对拍>")
        outs.append(buf.getvalue())
    assert outs[0] == outs[1] == outs[2], f"三执行器不一致：{outs!r}"
    assert outs[0].split("\n")[0] == "<div><i>x</i></div>"


def test_html_列表元素也被转义():
    out = run_capture(
        '导入 html\n打印(html.无序列表(["甲", "<乙>"]))\n'
        "打印(html.有序列表([1, 2]))\n")
    lines = out.split("\n")
    assert lines[0] == "<ul><li>甲</li><li>&lt;乙&gt;</li></ul>"
    assert lines[1] == "<ol><li>1</li><li>2</li></ol>"


def test_html_空标签名报错():
    assert "要是标签名" in run_err('导入 html\n打印(html.标签("", "x"))\n')


# -- 对比 -------------------------------------------------------------------

def test_对比_统一格式():
    out = run_capture('导入 对比\n打印(对比.统一("甲\\n乙\\n丙", "甲\\n丁\\n丙"))\n')
    assert out.rstrip("\n").split("\n") == [
        "--- 旧", "+++ 新", "@@ -1,3 +1,3 @@", " 甲", "-乙", "+丁", " 丙"]


def test_对比_逐行结构():
    out = run_capture(
        '导入 对比\n打印(对比.逐行(["a", "b"], ["a", "c"], 真))\n')
    assert out.strip() == "[[' ', 1, 'a'], ['-', 2, 'b'], ['+', 2, 'c']]"


def test_对比_相似度与最相似():
    out = run_capture(
        "导入 对比\n"
        '打印(对比.相似度("甲\\n乙", "甲\\n乙"))\n'
        '打印(对比.最相似("aple", ["apple", "banana", "grape"]))\n')
    assert out.split("\n")[0] == "1.0"
    assert out.split("\n")[1] == "apple"


def test_对比_并排():
    out = run_capture('导入 对比\n打印(对比.并排("甲\\n乙", "甲\\n丙", 6))\n')
    lines = out.rstrip("\n").split("\n")
    assert len(lines) == 2
    assert "|" in lines[0] and "~" in lines[1]     # 相同行不带 ~，不同行带


def test_对比_文本列表混用与报错():
    out = run_capture('导入 对比\n打印(对比.统一(["x"], "x\\ny"))\n')
    assert "+y" in out
    assert "要传文本或文本列表" in run_err("导入 对比\n打印(对比.统一(1, 2))\n")
    assert "不是文本" in run_err(
        '导入 对比\n打印(对比.统一(["a", 1], ["b"]))\n')
    assert "0 或正整数" in run_err(
        '导入 对比\n打印(对比.统一("a", "b", -1))\n')


# -- 配置 -------------------------------------------------------------------

def test_配置_JSON往返(tmp_path):
    p = str(tmp_path / "c.json").replace("\\", "/")
    out = run_capture(
        "导入 配置\n"
        f'令 p = "{p}"\n'
        '配置.写JSON(p, {"名": "测", "嵌套": {"a": 1}})\n'
        "令 d = 配置.读JSON(p)\n"
        '打印(d["名"], d["嵌套"]["a"])\n')
    assert out.split() == ["测", "1"]
    # 中文必须是**原样**写进去的（不是 \uXXXX）
    assert "测" in (tmp_path / "c.json").read_text(encoding="utf-8")


def test_配置_读不到时给默认或报错():
    out = run_capture(
        '导入 配置\n打印(配置.读JSON("没有这个.json", {"默认": 真}))\n')
    assert out.strip() == "{'默认': 真}"
    assert "不存在" in run_err('导入 配置\n打印(配置.读JSON("没有这个.json"))\n')


def test_配置_坏JSON总是报错不静默取默认():
    """内容坏了**不能**静默返回默认值——那会把「配置写错了」变成「配置没生效」。"""
    import tempfile
    from pathlib import Path as _P
    with tempfile.TemporaryDirectory() as td:
        bad = _P(td) / "bad.json"
        bad.write_text("{不是JSON", encoding="utf-8")
        p = str(bad).replace("\\", "/")
        err = run_err(f'导入 配置\n打印(配置.读JSON("{p}", {{}}))\n')
        assert "不是合法的 JSON" in err


def test_配置_点号路径取设合并展开():
    out = run_capture(
        "导入 配置\n"
        '令 c = {"数据库": {"端口": 5432}}\n'
        '打印(配置.取(c, "数据库.端口"), 配置.取(c, "数据库.缺", 1))\n'
        '令 a = 配置.设({}, "甲.乙", 1)\n'
        "打印(a)\n"
        '打印(配置.合并({"a": 1, "b": {"x": 1}}, {"b": {"y": 2}}))\n'
        '打印(配置.展开({"a": {"b": 1}, "c": 2, "d": {}}))\n')
    lines = out.split("\n")
    assert lines[0] == "5432 1"
    assert lines[1] == "{'甲': {'乙': 1}}"
    assert lines[2] == "{'a': 1, 'b': {'x': 1, 'y': 2}}"
    assert lines[3] == "{'a.b': 1, 'c': 2, 'd': {}}"


def test_配置_取不到时报错指出缺在哪一段():
    err = run_err('导入 配置\n打印(配置.取({"甲": 1}, "甲.乙"))\n')
    assert "甲.乙" in err
    # 值是标量时说明「是什么类型」；是字典时才列「这些键」
    assert "int 类型" in err
    err2 = run_err('导入 配置\n打印(配置.取({"甲": {"丙": 1}}, "甲.丁"))\n')
    assert "这些键" in err2 and "丙" in err2


def test_配置_键值文本往返与注释(tmp_path):
    p = str(tmp_path / "c.conf").replace("\\", "/")
    out = run_capture(
        "导入 配置\n"
        f'令 p = "{p}"\n'
        '配置.写键值(p, {"主机": "本地", "注释": "a # b"})\n'
        "打印(配置.读键值(p))\n")
    assert "'注释': 'a # b'" in out


def test_配置_键值坏行报错():
    import tempfile
    from pathlib import Path as _P
    with tempfile.TemporaryDirectory() as td:
        f = _P(td) / "bad.conf"
        f.write_text("这行没有等号\n", encoding="utf-8")
        p = str(f).replace("\\", "/")
        assert "没有「=」" in run_err(f'导入 配置\n打印(配置.读键值("{p}"))\n')


# -- 标识 -------------------------------------------------------------------

def test_标识_形状与唯一性():
    out = run_capture(
        "导入 标识\n"
        "令 a = 标识.唯一标识()\n"
        "打印(长度(a), 长度(a.拆分(\"-\")))\n"
        "令 b = 标识.唯一标识(真)\n"
        '打印(长度(b), "-" 在 b)\n'
        "打印(长度(标识.唯一标识短()))\n"
        "令 bs = 标识.唯一标识字节()\n"
        "打印(长度(bs), bs[0] >= 0, bs[0] <= 255)\n"
        "打印(标识.唯一标识() != 标识.唯一标识())\n")
    lines = out.split("\n")
    assert lines[0] == "36 5"
    assert lines[1] == "32 假"
    assert lines[2] == "22"
    assert lines[3] == "16 真 真"
    assert lines[4] == "真"


def test_标识_短标识是URL安全的():
    """base64url 用 `-` `_`，不带 `+` `/` `=`——放进 URL 不用再编码。"""
    out = run_capture(
        "导入 标识\n"
        "令 s = 标识.唯一标识短()\n"
        '打印("+" 在 s, "/" 在 s, "=" 在 s)\n')
    assert out.strip() == "假 假 假"


# -- 既有模块补缺 -----------------------------------------------------------

def test_迭代_补的四个函数():
    out = run_capture(
        "导入 迭代\n"
        "打印(迭代.累积([1, 2, 3]), 迭代.累积([1, 2], 10))\n"
        "打印(迭代.组合([1, 2, 3], 2), 迭代.组合([1], 2))\n"
        "打印(迭代.排列([1, 2]))\n"
        "打印(迭代.笛卡尔积([1, 2], \"甲乙\"))\n")
    lines = out.split("\n")
    assert lines[0] == "[1, 3, 6] [11, 13]"
    assert lines[1] == "[[1, 2], [1, 3], [2, 3]] []"
    assert lines[2] == "[[1, 2], [2, 1]]"
    assert lines[3] == "[[1, '甲'], [1, '乙'], [2, '甲'], [2, '乙']]"


def test_迭代_组合的负数报错与排列的全排列约定():
    assert "不能是负数" in run_err("导入 迭代\n打印(迭代.组合([1], -1))\n")
    # ⚠️ `排列` 的 `取几个` 默认是 -1，**表示「全排列」**——所以 -1 是合法值
    out = run_capture("导入 迭代\n打印(迭代.排列([1, 2]))\n"
                      "打印(迭代.排列([1, 2], -1))\n")
    assert out.split("\n")[0] == out.split("\n")[1] == "[[1, 2], [2, 1]]"
    assert "不能是负数" in run_err("导入 迭代\n打印(迭代.排列([1], -2))\n")


def test_文本_截断含省略号占位():
    out = run_capture(
        "导入 文本 为 T\n"
        '打印(T.截断("这是一段很长的话", 6))\n'
        '打印(T.截断("短", 6))\n'
        '打印(T.截断("abc", 2, ".."))\n'
        '打印(T.截断("abc", 1, ".."))\n')
    lines = out.split("\n")
    assert lines[0] == "这是一段很…"          # 宽度 6 含省略号
    assert lines[1] == "短"
    assert lines[2] == ".."                   # 宽度正好等于省略号
    assert lines[3] == "."                    # 宽度比省略号还短 → 截省略号


def test_文本_填充是居中():
    out = run_capture('导入 文本 为 T\n打印("[" + T.填充("abc", 7, "-") + "]")\n')
    assert out.strip() == "[--abc--]"
    # ⚠️ 这条顺带钉住 `文本.py` 曾漏导入 RunTypeError（报出来是 NameError）
    assert "一个字符" in run_err('导入 文本 为 T\n打印(T.填充("a", 5, "--"))\n')


def test_时间_给名字才打印_不给就安静计时():
    """**给名字** → 离开块时打印一行；**不给名字** → 安静，只能用 `t.耗时()`。

    这个区分是实测逼出来的：`排序跑分` 项目把 `计时()` 接进「耗时(排序函数, 数据)」
    里想拿秒数算进跑分表，结果每次调用都多打一行 `耗时 X 秒`，把表格冲乱了。
    """
    out = run_capture(
        "导入 时间\n"
        '用 时间.计时("建表") 为 t：\n'
        "    令 和 = 0\n"
        "    遍历 i 在 范围(10)：\n"
        "        和 = 和 + i\n")
    assert re.fullmatch(r"建表 耗时 \d+\.\d{3} 秒\n", out)

    quiet = run_capture(
        "导入 时间\n"
        "用 时间.计时() 为 t：\n"
        "    令 x = 1\n")
    assert quiet == "", f"不给名字时不该有任何输出，实得 {quiet!r}"


def test_时间_异常时也给名字打印():
    out = run_capture(
        "导入 时间\n"
        "尝试：\n"
        '    用 时间.计时("Z") 为 t：\n'
        '        抛出 值错误("x")\n'
        "捕获 值错误：\n"
        '    打印("接住")\n')
    assert re.fullmatch(r"Z 耗时 \d+\.\d{3} 秒\n接住\n", out)


def test_时间_计时块内可读秒数():
    out = run_capture(
        "导入 时间\n"
        "用 时间.计时() 为 t：\n"
        "    令 秒 = t.耗时()\n"
        "    打印(秒 >= 0)\n")
    assert out.split("\n")[0] == "真"


def test_上下文管理器认得宿主对象():
    """⚠️ **M50 修的一个语言级缺口**（不是新模块的问题，但由它暴露）。

    `用 … 为 …：` 找 `进入`/`退出` 时，原先只认「基石类的实例」与
    「内建类型的方法表」——**标准库返回的宿主对象两条都不沾**，于是
    `用 时间.计时() 为 t：` 会报「不能当上下文管理器用」。
    已给 `runtime._ctx_lookup` 补第三条路（宿主对象上的同名方法）。
    三个执行器共用那一个函数，所以三边一起好。
    """
    from jishi import cvm_bind
    from jishi import vm as vm_mod

    src = ('导入 时间\n'
           '用 时间.计时("A") 为 t：\n'
           '    令 x = 1\n')
    for fn in (run_source, vm_mod.run_source, cvm_bind.run_source_c):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn(src, filename="<宿主上下文>")
        assert re.fullmatch(r"A 耗时 \d+\.\d{3} 秒\n", buf.getvalue())
