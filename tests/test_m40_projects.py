# -*- coding: utf-8 -*-
"""M40 主线 C：五个场景的真实项目 + 行数比基线。

分三层：

1. **新项目能真跑**（待办清单 / 排序跑分）：端到端跑一遍，断言关键输出与
   出错路径的「人话」提示；
2. **行数比工具可信**：口径（有效行 = 去空行去注释）可复核，
   且**每个项目都配了 Python 参考实现**（`--check`）——不然基线可以挑数据；
3. **参考实现真的同功能**：Python 版跑一遍不能崩（否则比值是拿坏实现比出来的）。
"""

from __future__ import annotations

import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from jishi.interpreter import run_source                    # noqa: E402
from jishi.stdlib import 系统 as sys_mod                     # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT / "examples" / "projects"


def _run_jsh(path: Path, args: list[str], cwd: Path) -> str:
    """按 CLI 的方式跑一个基石脚本（参数经 `系统.参数()` 递进去）。"""
    old = Path.cwd()
    sys_mod._设参数(args)
    buf = io.StringIO()
    try:
        import os
        os.chdir(cwd)
        with redirect_stdout(buf):
            run_source(path.read_text(encoding="utf-8"), str(path))
    finally:
        os.chdir(old)
        sys_mod._设参数([])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1. 待办清单（CLI 工具）
# ---------------------------------------------------------------------------

TODO = PROJECTS / "待办清单" / "待办.jsh"


def test_todo_add_list_and_complete(tmp_path):
    assert "已添加第 1 条：买牛奶" in _run_jsh(TODO, ["添加", "买牛奶"], tmp_path)
    assert "已添加第 2 条：写周报 修自行车" in _run_jsh(
        TODO, ["添加", "写周报", "修自行车"], tmp_path)

    out = _run_jsh(TODO, ["完成", "2"], tmp_path)
    assert "第 2 条现在：写周报 修自行车（完成）" in out

    out = _run_jsh(TODO, ["列出"], tmp_path)
    assert "== 待办清单 ==" in out
    assert "[ ] 1. 买牛奶" in out
    assert "[√] 2. 写周报 修自行车" in out
    assert "共 2 条：已完成 1，未完成 1" in out


def test_todo_persists_to_json(tmp_path):
    """数据落盘：换个进程再读还在（这才是「持久化」）。"""
    _run_jsh(TODO, ["添加", "持久化测试"], tmp_path)
    saved = (tmp_path / "待办.json").read_text(encoding="utf-8")
    assert "持久化测试" in saved
    assert '"完成": false' in saved or '"完成":false' in saved


def test_todo_delete_and_clear(tmp_path):
    _run_jsh(TODO, ["添加", "甲"], tmp_path)
    _run_jsh(TODO, ["添加", "乙"], tmp_path)
    _run_jsh(TODO, ["完成", "1"], tmp_path)

    assert "已删除：乙" in _run_jsh(TODO, ["删除", "2"], tmp_path)
    out = _run_jsh(TODO, ["清空已完成"], tmp_path)
    assert "清掉了 1 条已完成，还剩 0 条" in out


def test_todo_uncomplete(tmp_path):
    _run_jsh(TODO, ["添加", "甲"], tmp_path)
    _run_jsh(TODO, ["完成", "1"], tmp_path)
    assert "（取消完成）" in _run_jsh(TODO, ["取消完成", "1"], tmp_path)
    assert "[ ] 1. 甲" in _run_jsh(TODO, ["列出"], tmp_path)


def test_todo_error_messages_are_human(tmp_path):
    """出错给人话：不是序号、序号越界、命令不认识——三条都要能读懂。"""
    _run_jsh(TODO, ["添加", "甲"], tmp_path)
    assert "「abc」不是序号" in _run_jsh(TODO, ["完成", "abc"], tmp_path)
    assert "清单里没有第 9 条" in _run_jsh(TODO, ["完成", "9"], tmp_path)
    assert "不认识这个命令：「胡来」" in _run_jsh(TODO, ["胡来"], tmp_path)


def test_todo_broken_json_is_tolerated(tmp_path):
    """数据文件坏了不该让工具崩：按空清单处理并说一声。"""
    (tmp_path / "待办.json").write_text("{ 这不是 JSON", encoding="utf-8")
    out = _run_jsh(TODO, ["列出"], tmp_path)
    assert "读不动，先按空清单处理" in out
    assert "（清单是空的" in out


def test_todo_usage_without_args(tmp_path):
    out = _run_jsh(TODO, [], tmp_path)
    assert "用法：jishi 待办.jsh 命令" in out
    assert "添加 任务内容" in out


# ---------------------------------------------------------------------------
# 2. 排序跑分（算法练习）
# ---------------------------------------------------------------------------

SORT = PROJECTS / "排序跑分" / "排序.jsh"


def test_sort_benchmark_self_checks(tmp_path):
    out = _run_jsh(SORT, ["60"], tmp_path)
    assert "== 排序跑分 ==" in out
    assert "三个排序的结果都与内建排序一致 ✅" in out
    assert "冒泡(毫秒)" in out and "快速(毫秒)" in out


def test_sort_benchmark_reports_bad_size(tmp_path):
    out = _run_jsh(SORT, ["这不是数字"], tmp_path)
    assert "不是规模数字，用默认规模" in out


def test_sort_algorithms_are_actually_correct(tmp_path):
    """把三个排序单独拎出来对拍内建排序（含重复值、已有序、逆序）。"""
    src = (SORT.read_text(encoding="utf-8").split("函数 主程序()")[0]
           + """
令 用例们 = [[], [1], [2, 1], [3, 3, 3], [5, 4, 3, 2, 1], [1, 2, 3, 4, 5],
            [9, 1, 8, 2, 7, 3]]
遍历 用例 在 用例们:
    令 期望 = 用例[:]
    期望.排序()
    如果 冒泡(用例) != 期望 或 插入(用例) != 期望 或 快速(用例) != 期望:
        抛出 断言错误(`排序不对：{用例}`)
打印("三个排序在 7 组边界输入上都对")
""")
    buf = io.StringIO()
    with redirect_stdout(buf):
        run_source(src, "<排序对拍>")
    assert "三个排序在 7 组边界输入上都对" in buf.getvalue()


# ---------------------------------------------------------------------------
# 3. 行数比基线
# ---------------------------------------------------------------------------

sys.path.insert(0, str(ROOT / "tools"))


def _loc():
    import importlib
    return importlib.import_module("loc_ratio")


def test_effective_lines_counting_rule():
    """口径要经得起复核：空行与纯注释不算有效行。"""
    loc = _loc()
    tmp = Path(__file__).parent / "_loc_probe.jsh"
    tmp.write_text("令 甲 = 1\n"          # 有效
                   "\n"                    # 空行
                   "# 纯注释\n"             # 纯注释
                   "    # 缩进的纯注释\n"     # 纯注释
                   "令 乙 = 2  # 行尾注释\n",  # 有效（行尾注释不算纯注释行）
                   encoding="utf-8")
    try:
        total, useful = loc.count_lines(tmp)
        assert total == 5 and useful == 2
    finally:
        tmp.unlink()


def test_every_project_has_python_reference():
    """**基线完整性**：每个项目都要有 Python 参考实现，缺了就报错。"""
    loc = _loc()
    rows = loc.collect()
    assert rows, "至少要有几个项目"
    missing = [r["项目"] for r in rows if not r["有参考"]]
    assert missing == [], f"这些项目缺 Python 参考实现：{missing}"


def test_loc_ratio_check_passes():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "loc_ratio.py"),
                        "--check"], capture_output=True, cwd=str(ROOT))
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")


def test_loc_ratio_numbers_are_reproducible():
    """表里的数字要能复算：拿工具的内部数据自己算一遍，与打印的一致。"""
    loc = _loc()
    rows = loc.collect()
    for r in rows:
        assert r["基石有效"] > 0 and r["py有效"] > 0, r
        # 比值口径：基石有效行 ÷ Python 有效行
        assert loc.ratio(r["基石有效"], r["py有效"]) == \
            f"{r['基石有效'] / r['py有效']:.2f}"


def test_python_reference_implementations_run(tmp_path):
    """Python 参考实现要真能跑——拿坏实现比出来的比值没有意义。

    `网页采集` 的参考实现要联网，**不在这里跑**（测试不该依赖网络）；
    它的存在性由 `test_every_project_has_python_reference` 保证。
    """
    import os

    #: 需要显式数据的项目 → 传哪份样例文件
    data_arg = {"成绩分析": "成绩.csv", "日志统计": "访问.log"}
    done = []
    for project in sorted(PROJECTS.iterdir()):
        ref = project / "参考实现.py"
        if not ref.exists() or project.name == "网页采集":
            continue
        args = [sys.executable, str(ref)]
        if project.name == "基石自述站":
            args += [str(tmp_path / "站点"), str(ROOT)]
        elif project.name in data_arg:
            args += [str(project / data_arg[project.name])]
        elif project.name == "待办清单":
            args += ["列出"]
        elif project.name == "排序跑分":
            args += ["30"]
        old = Path.cwd()
        try:
            os.chdir(tmp_path)          # 别把产物写到仓库里
            r = subprocess.run(args, capture_output=True, timeout=120)
        finally:
            os.chdir(old)
        assert r.returncode == 0, (project.name,
                                   r.stderr.decode("utf-8", "replace")[-400:])
        done.append(project.name)
    assert len(done) >= 5, done
