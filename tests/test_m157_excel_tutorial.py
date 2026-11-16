# -*- coding: utf-8 -*-
"""M157 · 教程第 12 章（`Excel`）连同它撞出来的两个真 bug。

## 一、教程示例必须真能跑

`examples/tutorial/12_成绩单Excel.jsh`：导出成绩单 → 读回来 → 三科平均 →
总分排行（对齐报表）→ 缺考（报错 + 补零）→ 日期序号换算 → 清理。
要求**五个执行器输出逐字相同**，跑完**不留 `成绩单.xlsx`**。

## 二、写教程撞出来的两个东西

### ① `表格.汇总` / `表格.分组汇总` 碰到 `空`：三种行为

| 执行器 | 改前 |
|---|---|
| Python / 树遍历 / C VM | 报错，但文案漏出 Python 的 `None` |
| Rust | 报错，文案是「空」（对的） |
| **Node** | **静默成功**：`Number(null)` 是 0，于是「缺考」按 0 分算进平均 |

⇒ Python 的文案改成基石的显示（`空`），Node 补上 `null` 判断并报同一句话。
**「静默算错」比报错坏得多** —— 这是这一站最值钱的一条。

### ② 含 `空` 的列，排序顺序在三个宿主上不一样

`排序按` 的非数字那一支按**字符串**比大小，而各宿主把 `空` 写成不同东西：
Python 的 `None`、Node 的 `''`、Rust 的 `空` —— 三种写法排出来的顺序自然不同。
现在统一用**基石的显示**（`空` / `真`）。

📌 这条也说明一件事：**「显示口径」不只是打印的事** ——
只要有「按文本比大小」的地方，它就会渗进去。
"""

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path, 去掉遮蔽提示          # noqa: E402

CVM驱动 = '''\
import sys
sys.path.insert(0, r"{root}")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(newline="\\n")
    except Exception:
        pass
from oracle.jishi import cvm_bind, errors as E

src = open("程序.jsh", encoding="utf-8").read()
try:
    cvm_bind.run_source_c(src, filename="程序.jsh")
except E.JishiError as e:
    e.with_source(src.replace("\\r\\n", "\\n").split("\\n"))
    sys.stderr.write(e.render() + "\\n")
    sys.exit(1)
'''


def _五份(源码: str, tmp_path: Path) -> dict:
    from oracle.jishi import serialize
    from oracle.jishi.compiler import compile_source

    (tmp_path / "程序.jsh").write_text(源码, encoding="utf-8", newline="\n")
    (tmp_path / "驱动.py").write_text(
        CVM驱动.format(root=str(ROOT).replace("\\", "\\\\")), encoding="utf-8")
    表 = {
        "树遍历": [sys.executable, "-m", "oracle.jishi.cli", "--tree", "程序.jsh"],
        "Python VM": [sys.executable, "-m", "oracle.jishi.cli", "程序.jsh"],
        "C VM": [sys.executable, "驱动.py"],
    }
    rs = rust_exe_path()
    if rs.is_file():
        表["Rust 宿主"] = [str(rs), "程序.jsh"]
    node = shutil.which("node")
    if node:
        (tmp_path / "程序.json").write_text(
            serialize.dumps(compile_source(源码, "程序.jsh")), encoding="utf-8")
        表["Node 宿主"] = [node, str(ROOT / "oracle" / "node" / "index.js"),
                           "程序.json"]
    出 = {}
    for 名字, 命令 in 表.items():
        r = subprocess.run(命令, cwd=str(tmp_path), capture_output=True,
                           timeout=120, input=b"")
        出[名字] = 去掉遮蔽提示((r.stdout + r.stderr).decode("utf-8", "replace"))
    return 出


def _一致(源码: str, tmp_path: Path) -> str:
    出 = _五份(源码, tmp_path)
    值 = set(出.values())
    assert len(值) == 1, "五个执行器说法不一致：\n" + "\n".join(
        f"—— {k} ——\n{v}" for k, v in 出.items())
    return 值.pop()


# 准备一张「成绩表」，后面几条用例共用
准备 = ('导入 Excel\n'
      '导入 表格\n'
      '令 成绩们 = [\n'
      '    {"姓名": "小明", "语文": 92, "数学": 88},\n'
      '    {"姓名": "小红", "语文": 85, "数学": 97},\n'
      '    {"姓名": "小刚", "语文": 78, "数学": 66},\n'
      ']\n'
      'Excel.写工作簿("表.xlsx", 表格.从记录(成绩们), "成绩")\n'
      '令 簿 = Excel.读工作簿("表.xlsx")\n'
      '令 成绩 = Excel.读工作表(簿, "成绩")\n')


# ---------------------------------------------------------------------------
# 一、教程示例
# ---------------------------------------------------------------------------

def test_教程示例能跑且自己清理(tmp_path):
    源码 = (ROOT / "examples" / "tutorial" / "12_成绩单Excel.jsh").read_text(
        encoding="utf-8")
    文本 = _一致(源码, tmp_path)
    for 片段 in ("已写出 成绩单.xlsx",
               "工作簿里有： ['成绩']",
               "语文 平均 85.0 分（最高 92，最低 78）",
               "---- 总分排行 ----",
               "直接汇总 → 列「英语」里有不是数字的值「空」，没法汇总",
               "缺考按 0 分算 → 39.5 分",
               "Excel 里的 45000 指的是： 2023-03-15",
               "演示完成（成绩单.xlsx 已清理）"):
        assert 片段 in 文本, f"缺「{片段}」：\n{文本}"
    assert not (tmp_path / "成绩单.xlsx").exists(), "示例没把 xlsx 删掉"


def test_写出去再读回来_值不变(tmp_path):
    """数字给整数、文本给文本、空还是空 —— 这是 xlsx 往返的最低保证。

    ⚠️ 两个小数（`1.5` 和 `精确("1.50")`）以前在 Node 上会写进 `[object Object]`
    （`FloatBox` 是对象，`String()` 一化就成这串英文）；Python 那边则把精确小数
    写成**文本** —— xlsx 里那样就「不能算」了。现在三边都写数字。
    """
    文本 = _一致('导入 Excel\n'
              '导入 表格\n'
              'Excel.写工作簿("t.xlsx", 表格.从记录([\n'
              '    {"名": "甲", "数": 42, "小": 1.5, "精": 精确("1.50"),\n'
              '     "文": "A&B <公司>", "缺": 空},\n'
              ']), "S")\n'
              '令 t = Excel.读工作表(Excel.读工作簿("t.xlsx"), "S")\n'
              '打印(表格.数据行(t))\n', tmp_path)
    assert 文本.strip() == "[['甲', 42, 1.5, 1.5, 'A&B <公司>', 空]]", 文本


def test_多表_标签页顺序按字典书写顺序(tmp_path):
    文本 = _一致('导入 Excel\n'
              '导入 表格\n'
              'Excel.写多表("m.xlsx", {\n'
              '    "甲表": 表格.从记录([{"x": 1}]),\n'
              '    "乙表": 表格.从记录([{"y": 2}]),\n'
              '})\n'
              '令 簿 = Excel.读工作簿("m.xlsx")\n'
              '打印(簿["工作表们"])\n'
              '打印(表格.数据行(Excel.读工作表(簿, "乙表")))\n', tmp_path)
    assert 文本.strip().splitlines() == ["['甲表', '乙表']", "[[2]]"], 文本


# ---------------------------------------------------------------------------
# 二、`空` 在汇总里必须**报错**（M157 修：Node 以前当 0 分静默算完）
# ---------------------------------------------------------------------------

def test_汇总遇空报错且文案是空不是None(tmp_path):
    文本 = _一致('导入 表格\n'
              '令 t = 表格.从记录([{"姓名": "甲", "分数": 90},\n'
              '                   {"姓名": "乙", "分数": 空}])\n'
              '表格.汇总(t, "分数")\n', tmp_path)
    assert "列「分数」里有不是数字的值「空」，没法汇总" in 文本, 文本
    assert "None" not in 文本, f"漏出宿主表示：{文本!r}"


def test_分组汇总遇空同样报错(tmp_path):
    文本 = _一致('导入 表格\n'
              '令 t = 表格.从记录([{"班": "一班", "分": 90},\n'
              '                   {"班": "二班", "分": 空}])\n'
              '表格.分组汇总(t, "班", "分")\n', tmp_path)
    assert "列「分」里有不是数字的值「空」，没法汇总" in 文本, 文本
    assert "None" not in 文本, f"漏出宿主表示：{文本!r}"


def test_文本数字能参与汇总(tmp_path):
    """别人的表里数字常常是文本（`"92"`）—— 这一条是**允许**的，别收紧成报错。"""
    文本 = _一致('导入 表格\n'
              '令 t = 表格.从记录([{"分数": "90"}, {"分数": 80}])\n'
              '打印(表格.汇总(t, "分数")["平均"])\n', tmp_path)
    assert 文本.strip() == "85.0", 文本


# ---------------------------------------------------------------------------
# 三、含 `空` 的列排序：三宿主要给同一个顺序（M157 修）
# ---------------------------------------------------------------------------

def test_含空的列排序三宿主一致(tmp_path):
    文本 = _一致('导入 表格\n'
              '令 t = 表格.从记录([{"名": "甲", "分": 92},\n'
              '                   {"名": "乙", "分": 空},\n'
              '                   {"名": "丙", "分": 85}])\n'
              '遍历 行 在 表格.数据行(表格.排序按(t, "分"))：\n'
              '    打印(行)\n'
              '打印("---")\n'
              '遍历 行 在 表格.数据行(表格.排序按(t, "分", 真))：\n'
              '    打印(行)\n', tmp_path)
    行 = 文本.strip().splitlines()
    # 升序：数字从小到大，`空` 排在数字之后（它落到「按文本比」那一支）
    assert 行[0] == "['丙', 85]", 行
    assert 行[1] == "['甲', 92]", 行
    assert 行[2] == "['乙', 空]", 行
    # 降序：正好反过来
    assert 行[4] == "['乙', 空]", 行
    assert 行[5] == "['甲', 92]", 行
    assert 行[6] == "['丙', 85]", 行


def test_文本列含空排序也一致(tmp_path):
    文本 = _一致('导入 表格\n'
              '令 t = 表格.从记录([{"名": "甲"}, {"名": 空}, {"名": "丙"}])\n'
              '遍历 行 在 表格.数据行(表格.排序按(t, "名", 真))：\n'
              '    打印(行)\n', tmp_path)
    assert 文本.strip().splitlines() == ["[空]", "['甲']", "['丙']"], 文本


# ---------------------------------------------------------------------------
# 四、读别人的表：三条要命的话（教程 §12.5）
# ---------------------------------------------------------------------------

def test_读不了的格式说清怎么办(tmp_path):
    (tmp_path / "旧.xls").write_bytes(b"not a real xls")
    文本 = _一致('导入 Excel\nExcel.读工作簿("旧.xls")\n', tmp_path)
    assert "不是一个 .xlsx" in 文本, 文本
    assert "另存为" in 文本, f"没教人怎么办：{文本!r}"


def test_表名写错时列出有哪些表(tmp_path):
    文本 = _一致(准备 + 'Excel.读工作表(簿, "没有的表")\n', tmp_path)
    assert "没有叫「没有的表」的工作表" in 文本, 文本
    assert "这个工作簿里有：「成绩」" in 文本, f"没给可选项：{文本!r}"


def test_日期序号按1899年12月30日换算(tmp_path):
    文本 = _一致('导入 日期\n'
              '打印(日期.加天数(45000, "1899-12-30"))\n'
              '打印(日期.加天数(1, "1899-12-30"))\n', tmp_path)
    assert 文本.strip().splitlines() == ["2023-03-15", "1899-12-31"], 文本
