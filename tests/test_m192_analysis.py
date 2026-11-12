# -*- coding: utf-8 -*-
"""M192 · 数据分析：`表格` 的透视 / 多列分组 / 宽长表 / 缺失值（6 个函数）。

## 判据怎么定的

1. **三宿主逐字一致**：同一段脚本在 Python / Rust / Node 上输出必须一样 ——
   这是本项目的立身规矩（`check_engines` 的口径），表格尤其容易差在
   **浮点显示**（`95.0` vs `95`）和**缺失表示**（`空` vs 空串）上；
2. **数值**：透视的交叉格、分组的聚合值都手算核对；
3. **边界**：缺失 ≠ 0 / 假；找不到列 / 不认识的聚合方式要报中文错；
4. **宽长互逆**：`融化` 再 `展开列` 回到原表（这一对最容易各写一半就歪）。

## 这一版只做了 6 个

路线图 §12.2.3 给的 12 条里，先落「透视 + 多列分组 + 宽长表 + 缺失值」这一组
（切分线 D 允许这么切）。剩下 6 个（转换列 / 切片行 / 取列 / 合并 / 去重 /
按时间重采样）与 `统计` 那 8 个在下一批。
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                        # noqa: E402


def _跑(源: str, tmp_path: Path, 名="脚本.jsh") -> tuple:
    脚本 = tmp_path / 名
    脚本.write_text(源, encoding="utf-8", newline="\n")
    r = subprocess.run([str(rust_exe_path()), str(脚本)], capture_output=True,
                       timeout=180, cwd=str(tmp_path))
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def _跑py(源: str, tmp_path: Path, 名="脚本p.jsh") -> tuple:
    脚本 = tmp_path / 名
    脚本.write_text(源, encoding="utf-8", newline="\n")
    r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", str(脚本)],
                       capture_output=True, timeout=180, cwd=str(ROOT))
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


@pytest.fixture(scope="module", autouse=True)
def _要有宿主():
    if not rust_exe_path().is_file():
        pytest.skip("没有 Rust 宿主（cargo build --release）")


销售表 = (
    "令 表 = 表格.从记录([\n"
    '    {"地区": "华东", "季度": "一", "额": 10},\n'
    '    {"地区": "华东", "季度": "二", "额": 20},\n'
    '    {"地区": "华南", "季度": "一", "额": 30},\n'
    '    {"地区": "华东", "季度": "一", "额": 5}\n'
    "])\n"
)


def test_透视把同一格的多行合并(tmp_path):
    """`(华东, 一)` 有两行（10 和 5）⇒ 求和 15、平均 7.5。"""
    源 = "导入 表格\n" + 销售表 + (
        '打印(表格.转TSV(表格.透视(表, "地区", "季度", "额", "求和")))\n'
        '打印(表格.转TSV(表格.透视(表, "地区", "季度", "额", "平均")))\n'
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-600:]
    assert "地区\t一\t二" in 出, 出
    assert "华东\t15.0\t20.0" in 出, 出
    assert "华东\t7.5\t20.0" in 出, 出
    assert "华南\t30.0\t" in 出, 出          # 华南没有「二」这一季 ⇒ 空格子


def test_透视默认按求和(tmp_path):
    源 = "导入 表格\n" + 销售表 + (
        '打印(表格.转TSV(表格.透视(表, "地区", "季度", "额")))\n'
    )
    _, 出 = _跑(源, tmp_path)
    assert "华东\t15.0\t20.0" in 出, 出


def test_按列分组能多列(tmp_path):
    """多列分组是 `分组汇总`（只能一列）给不了的。"""
    源 = "导入 表格\n" + (
        "令 表 = 表格.从记录([\n"
        '    {"班": "一班", "性别": "男", "分": 90},\n'
        '    {"班": "一班", "性别": "女", "分": 80},\n'
        '    {"班": "一班", "性别": "男", "分": 70},\n'
        "])\n"
        '打印(表格.转TSV(表格.按列分组(表, ["班", "性别"], "分", "计数")))\n'
        '打印(表格.转TSV(表格.按列分组(表, "班", "分", "最大")))\n'
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-600:]
    assert "班\t性别\t分" in 出, 出
    assert "一班\t男\t2" in 出, 出          # 计数是整数，不带小数点
    assert "一班\t女\t1" in 出, 出
    assert "一班\t90.0" in 出, 出


def test_融化与展开列互逆(tmp_path):
    """宽 → 长 → 宽 必须回到原样（这一对最容易各写一半就歪）。"""
    源 = "导入 表格\n" + (
        '令 宽 = 表格.从记录([{"姓名": "小明", "语文": 90, "数学": 85}])\n'
        '令 长 = 表格.融化(宽, "姓名")\n'
        "打印(表格.转TSV(长))\n"
        '令 回 = 表格.展开列(长, "变量", "值")\n'
        "打印(表格.转TSV(回))\n"
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-600:]
    行们 = 出.strip().splitlines()
    assert 行们[0] == "姓名\t变量\t值", 出
    assert 行们[1] == "小明\t语文\t90", 出
    assert 行们[2] == "小明\t数学\t85", 出
    assert 行们[3] == "姓名\t语文\t数学", 出
    assert 行们[4] == "小明\t90\t85", 出


def test_缺失不等于零(tmp_path):
    """🔴 最容易搞错的一条：`0` 和 `假` 是**真值**，不能被当成缺失丢掉。"""
    源 = "导入 表格\n" + (
        "令 表 = 表格.从记录([\n"
        '    {"名": "甲", "数": 0},\n'
        '    {"名": "乙", "数": ""},\n'
        '    {"名": "丙", "数": 假},\n'
        "])\n"
        "打印(表格.转TSV(表格.丢弃缺失(表)))\n"
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-600:]
    assert "甲\t0" in 出, 出
    assert "丙\t假" in 出, 出
    assert "乙" not in 出, 出


def test_填充缺失三种方式(tmp_path):
    源 = "导入 表格\n" + (
        "令 表 = 表格.从记录([\n"
        '    {"日": 1, "量": 10},\n'
        '    {"日": 2, "量": ""},\n'
        '    {"日": 3, "量": 30},\n'
        "])\n"
        '打印(表格.转TSV(表格.填充缺失(表, "量", "前向")))\n'
        '打印(表格.转TSV(表格.填充缺失(表, "量", "平均")))\n'
        '打印(表格.转TSV(表格.填充缺失(表, "量", 0)))\n'
        "打印(表格.转TSV(表))\n"          # 原表不能被改
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-600:]
    assert "2\t10" in 出, 出             # 前向
    assert "2\t20.0" in 出, 出           # 平均 (10+30)/2
    assert "2\t0" in 出, 出              # 常数
    # 最后一段是原表，第 2 行还是空
    assert 出.rstrip().endswith("3\t30"), 出


def test_丢弃缺失只看指定的列(tmp_path):
    源 = "导入 表格\n" + (
        "令 表 = 表格.从记录([\n"
        '    {"名": "甲", "备": ""},\n'
        '    {"名": "", "备": "x"},\n'
        "])\n"
        '打印(表格.转TSV(表格.丢弃缺失(表, "名")))\n'
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-600:]
    assert "甲" in 出, 出
    assert 行数(出) == 2, 出             # 表头 + 甲


def 行数(s: str) -> int:
    return len([x for x in s.strip().splitlines() if x])


def test_找不到列与不认识的聚合都要报中文错(tmp_path):
    for 源, 关键词 in [
        ('表格.透视(表, "没有这列", "季度", "额")', "找不到列"),
        ('表格.透视(表, "地区", "季度", "额", "中位数")', "不认识的聚合方式"),
        ('表格.融化(表, [])', "至少要给一个列名"),
    ]:
        文 = "导入 表格\n" + 销售表 + 源 + "\n"
        码, 出 = _跑(文, tmp_path, "错.jsh")
        assert 码 != 0, f"这条应当报错：{源}"
        assert 关键词 in 出, f"{源} → {出[-400:]}"


def test_三宿主逐字一致(tmp_path):
    """Python / Rust / Node 跑同一段，输出必须一模一样。

    ⚠️ Node 走的是「字节码 JSON + index.js」那条路（与 `check_engines` 一样），
    不是直接跑源码。
    """
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location("ce", str(ROOT / "tools" / "check_engines.py"))
    ce = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ce)
    if ce._node_exe() is None:
        pytest.skip("没有 node")

    源 = "导入 表格\n" + 销售表 + (
        '打印(表格.转TSV(表格.透视(表, "地区", "季度", "额", "平均")))\n'
        '打印(表格.转TSV(表格.按列分组(表, ["地区"], "额", "计数")))\n'
        '打印(表格.转TSV(表格.融化(表, "地区")))\n'
        '打印(表格.转TSV(表格.填充缺失(表, "额", "平均")))\n'
        '打印(表格.转TSV(表格.丢弃缺失(表)))\n'
    )
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    脚本 = tmp_path / "三.jsh"
    脚本.write_text(源, encoding="utf-8", newline="\n")

    r = subprocess.run([str(rust_exe_path()), str(脚本)], capture_output=True,
                       timeout=180, cwd=str(tmp_path))
    rs = (r.stdout + r.stderr).decode("utf-8", "replace")

    p = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", str(脚本)],
                       capture_output=True, timeout=180, cwd=str(ROOT))
    py = (p.stdout + p.stderr).decode("utf-8", "replace")

    nd, nd错 = ce._run_node_vm(源, str(脚本), tmp_path)

    assert rs == py, f"Rust 与 Python 不一致：\n--- py ---\n{py}\n--- rust ---\n{rs}"
    assert nd == py, f"Node 与 Python 不一致：\n--- py ---\n{py}\n--- node ---\n{nd}\n{nd错[:400]}"
