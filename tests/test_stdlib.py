# -*- coding: utf-8 -*-
"""M2：标准库模块测试（数学/时间/文本/文件/表格）。"""
import sys
import io
from contextlib import redirect_stdout

import pytest

sys.path.insert(0, ".")

from jishi.interpreter import run_source  # noqa: E402
from jishi.errors import JishiError  # noqa: E402


def run_capture(src: str) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        run_source(src)
    return out.getvalue()


def _posix(p) -> str:
    """Windows 路径转成正斜杠形式，便于安全地写进基石源码字符串。"""
    return str(p).replace("\\", "/")


# -- 数学 -------------------------------------------------------------------

def test_math_sqrt_and_power():
    out = run_capture("导入 数学\n打印(数学.开方(16))\n打印(数学.平方(5))\n"
                      "打印(数学.幂(2, 10))\n")
    assert out.strip().splitlines() == ["4.0", "25", "1024"]


def test_math_round_and_floor():
    out = run_capture("导入 数学\n打印(数学.四舍五入(3.14159, 2))\n"
                      "打印(数学.向上取整(2.1))\n打印(数学.向下取整(2.9))\n")
    assert out.strip().splitlines() == ["3.14", "3", "2"]


def test_math_abs_gcd_lcm_factorial():
    out = run_capture("导入 数学\n打印(数学.绝对值(-7))\n打印(数学.最大公约数(12, 18))\n"
                      "打印(数学.最小公倍数(4, 6))\n打印(数学.阶乘(5))\n")
    assert out.strip().splitlines() == ["7", "6", "12", "120"]


def test_math_constants():
    out = run_capture("导入 数学\n打印(数学.四舍五入(数学.圆周率, 2))\n")
    assert out.strip() == "3.14"


def test_math_trig_with_degree_conversion():
    out = run_capture("导入 数学\n令 弧度 = 数学.角度转弧度(90)\n"
                      "打印(数学.四舍五入(数学.正弦(弧度)))\n")
    assert out.strip() == "1"


def test_math_log():
    out = run_capture("导入 数学\n打印(数学.常用对数(100))\n")
    assert out.strip() == "2.0"


def test_math_negative_sqrt_error():
    with pytest.raises(JishiError) as ei:
        run_source("导入 数学\n打印(数学.开方(-1))\n")
    assert "不能开平方" in str(ei.value)


# -- 时间 -------------------------------------------------------------------

def test_time_now_and_today():
    out = run_capture('导入 时间\n令 t = 时间.现在()\n打印(长度(t))\n'
                      '打印(长度(时间.今天()))\n')
    assert out.strip().splitlines() == ["19", "10"]


def test_time_moment_dict():
    out = run_capture("导入 时间\n令 d = 时间.此刻()\n打印(d[\"年\"] > 2020)\n"
                      '打印(长度(d))\n')
    assert out.strip().splitlines() == ["真", "7"]


def test_time_timestamp_and_sleep():
    out = run_capture("导入 时间\n打印(时间.时间戳() > 1700000000)\n"
                      "时间.睡眠(0.01)\n打印(\"醒了\")\n")
    assert out.strip().splitlines() == ["真", "醒了"]


def test_time_weekday():
    out = run_capture('导入 时间\n打印(长度(时间.星期名("2026-09-01")))\n')
    assert out.strip() == "1"


def test_time_add_days():
    out = run_capture('导入 时间\n打印(时间.加天数(30, "2026-01-01"))\n')
    assert out.strip() == "2026-01-31"


def test_time_high_precision():
    out = run_capture("导入 时间\n令 a = 时间.高精度时间()\n时间.睡眠(0.01)\n"
                      "令 b = 时间.高精度时间()\n打印(b > a)\n")
    assert out.strip() == "真"


# -- 文本 -------------------------------------------------------------------

def test_text_join():
    out = run_capture('导入 文本\n打印(文本.拼接(["a", "b", "c"], "-"))\n')
    assert out.strip() == "a-b-c"


def test_text_align_and_zfill():
    out = run_capture('导入 文本\n打印(文本.居中("ab", 6, "*"))\n'
                      '打印(文本.右对齐("7", 3))\n打印(文本.补零(7, 3))\n')
    assert out.splitlines() == ["**ab**", "  7", "007"]


def test_text_predicates():
    out = run_capture('导入 文本\n打印(文本.是数字("123"))\n打印(文本.是数字("12a"))\n'
                      '打印(文本.是字母("基石"))\n打印(文本.是空白("  "))\n')
    assert out.strip().splitlines() == ["真", "假", "真", "真"]


def test_text_count_repeat_capitalize():
    out = run_capture('导入 文本\n打印(文本.计数("banana", "a"))\n'
                      '打印(文本.重复("ab", 3))\n打印(文本.首字母大写("hELLO"))\n')
    assert out.strip().splitlines() == ["3", "ababab", "Hello"]


def test_text_strip_affixes():
    out = run_capture('导入 文本\n打印(文本.去前缀("基石语言", "基石"))\n'
                      '打印(文本.去后缀("data.csv", ".csv"))\n')
    assert out.strip().splitlines() == ["语言", "data"]


def test_text_format_template():
    out = run_capture('导入 文本\n打印(文本.格式化("{}今年{}岁", "小明", 12))\n')
    assert out.strip() == "小明今年12岁"


def test_text_split_lines():
    out = run_capture('导入 文本\n打印(文本.按行拆分("a\\nb"))\n')
    assert out.strip() == "['a', 'b']"


# -- 文件 -------------------------------------------------------------------

def test_file_write_read(tmp_path):
    p = tmp_path / "测试.txt"
    out = run_capture(
        f'导入 文件\n文件.写文本("{_posix(p)}", "你好，基石")\n'
        f'打印(文件.读文本("{_posix(p)}"))\n打印(文件.文件存在("{_posix(p)}"))\n')
    assert out.strip().splitlines() == ["你好，基石", "真"]


def test_file_append_and_lines(tmp_path):
    p = tmp_path / "行.txt"
    out = run_capture(
        f'导入 文件\n文件.写行("{_posix(p)}", ["第一行", "第二行"])\n'
        f'文件.追加文本("{_posix(p)}", "第三行\\n")\n'
        f'打印(文件.按行读("{_posix(p)}"))\n打印(文件.文件大小("{_posix(p)}") > 0)\n')
    assert out.strip().splitlines() == [
        "['第一行', '第二行', '第三行']", "真"]


def test_file_dir_operations(tmp_path):
    d = tmp_path / "子目录"
    out = run_capture(
        f'导入 文件\n文件.创建目录("{_posix(d)}")\n打印(文件.是目录("{_posix(d)}"))\n'
        f'文件.写文本("{_posix(d)}/a.txt", "x")\n打印(文件.列出目录("{_posix(d)}"))\n'
        f'文件.删除文件("{_posix(d)}/a.txt")\n打印(文件.文件存在("{_posix(d)}/a.txt"))\n')
    assert out.strip().splitlines() == ["真", "['a.txt']", "假"]


def test_file_path_helpers(tmp_path):
    out = run_capture(
        '导入 文件\n打印(文件.路径拼接("a", "b", "c.txt"))\n'
        '打印(文件.文件名("/x/y/z.jsh"))\n打印(文件.扩展名("a.jsh"))\n')
    lines = out.strip().splitlines()
    assert lines[1] == "z.jsh"
    assert lines[2] == ".jsh"
    assert lines[0].replace("\\", "/") == "a/b/c.txt"


def test_file_missing_error(tmp_path):
    with pytest.raises(JishiError) as ei:
        run_source(f'导入 文件\n文件.读文本("{_posix(tmp_path)}/不存在.txt")\n')
    assert "找不到文件" in str(ei.value)


# -- 表格 -------------------------------------------------------------------

CSV_TEXT = "姓名,分数,班级\n小明,85,1班\n小红,92,2班\n小刚,78,1班\n"


def _write_csv(tmp_path, name="成绩.csv"):
    p = tmp_path / name
    p.write_text(CSV_TEXT, encoding="utf-8")
    return p


def test_table_read(tmp_path):
    p = _write_csv(tmp_path)
    out = run_capture(f'导入 表格\n令 t = 表格.读表格("{_posix(p)}")\n'
                      f'打印(表格.表头(t))\n打印(长度(表格.数据行(t)))\n')
    assert out.strip().splitlines() == ["['姓名', '分数', '班级']", "3"]


def test_table_pick_column(tmp_path):
    p = _write_csv(tmp_path)
    out = run_capture(f'导入 表格\n令 t = 表格.读表格("{_posix(p)}")\n'
                      f'打印(表格.挑选(t, "姓名"))\n')
    assert out.strip() == "['小明', '小红', '小刚']"


def test_table_pick_multiple_columns(tmp_path):
    p = _write_csv(tmp_path)
    out = run_capture(f'导入 表格\n令 t = 表格.读表格("{_posix(p)}")\n'
                      f'打印(表格.挑选(t, ["姓名", "分数"]))\n')
    assert out.strip() == "[['小明', '85'], ['小红', '92'], ['小刚', '78']]"


def test_table_filter(tmp_path):
    p = _write_csv(tmp_path)
    out = run_capture(f'导入 表格\n令 t = 表格.读表格("{_posix(p)}")\n'
                      f'令 一班 = 表格.筛选(t, "班级", "1班")\n'
                      f'打印(表格.挑选(一班, "姓名"))\n')
    assert out.strip() == "['小明', '小刚']"


def test_table_sort(tmp_path):
    p = _write_csv(tmp_path)
    out = run_capture(f'导入 表格\n令 t = 表格.读表格("{_posix(p)}")\n'
                      f'令 排好 = 表格.排序按(t, "分数")\n'
                      f'打印(表格.挑选(排好, "分数"))\n')
    assert out.strip() == "['78', '85', '92']"


def test_table_sort_desc(tmp_path):
    p = _write_csv(tmp_path)
    out = run_capture(f'导入 表格\n令 t = 表格.读表格("{_posix(p)}")\n'
                      f'令 排好 = 表格.排序按(t, "分数", 真)\n'
                      f'打印(表格.挑选(排好, "分数"))\n')
    assert out.strip() == "['92', '85', '78']"


def test_table_summary(tmp_path):
    p = _write_csv(tmp_path)
    out = run_capture(f'导入 表格\n令 t = 表格.读表格("{_posix(p)}")\n'
                      f'令 s = 表格.汇总(t, "分数")\n'
                      f'打印(s["个数"])\n打印(s["总和"])\n打印(s["最大"])\n')
    assert out.strip().splitlines() == ["3", "255.0", "92.0"]


def test_table_write_and_reread(tmp_path):
    p = tmp_path / "输出.csv"
    out = run_capture(
        f'导入 表格\n令 t = [["姓名", "分数"], ["小明", "85"], ["小红", "92"]]\n'
        f'表格.写表格("{_posix(p)}", t)\n令 读回 = 表格.读表格("{_posix(p)}")\n'
        f'打印(表格.挑选(读回, "分数"))\n')
    assert out.strip() == "['85', '92']"


def test_table_dict_roundtrip(tmp_path):
    p = tmp_path / "字典表.csv"
    out = run_capture(
        f'导入 表格\n令 数据 = [{{"姓名"："小明", "分数"："85"}}]\n'
        f'表格.写字典表格("{_posix(p)}", 数据)\n'
        f'令 读回 = 表格.读字典表格("{_posix(p)}")\n打印(读回[0]["姓名"])\n')
    assert out.strip() == "小明"


def test_table_transpose():
    out = run_capture('导入 表格\n令 t = [["a", "1"], ["b", "2"]]\n'
                      '打印(表格.转置(t))\n')
    assert out.strip() == "[['a', 'b'], ['1', '2']]"


def test_table_bad_column(tmp_path):
    p = _write_csv(tmp_path)
    with pytest.raises(JishiError) as ei:
        run_source(f'导入 表格\n令 t = 表格.读表格("{_posix(p)}")\n表格.挑选(t, "身高")\n')
    assert "没有" in str(ei.value)


def test_table_missing_file(tmp_path):
    with pytest.raises(JishiError) as ei:
        run_source(f'导入 表格\n表格.读表格("{_posix(tmp_path)}/没有.csv")\n')
    assert "找不到" in str(ei.value)


# -- 导入错误 ---------------------------------------------------------------

def test_import_unknown_module():
    with pytest.raises(JishiError) as ei:
        run_source("导入 量子计算\n")
    assert "没有找到标准库模块" in str(ei.value)


# -- json（M10） ------------------------------------------------------------

def test_json_dumps_chinese_not_escaped():
    out = run_capture('导入 json\n令 d = {"名字": "小明"}\n打印(json.转文本(d))\n')
    assert "小明" in out and "\\u" not in out


def test_json_dumps_and_parse_roundtrip():
    out = run_capture(
        '导入 json\n'
        '令 d = {"a": 1, "b": [1, 2, 3]}\n'
        '令 t = json.转文本(d)\n'
        '令 r = json.解析(t)\n'
        '打印(r["b"][2])\n')
    assert out == "3\n"


def test_json_parse_plain():
    out = run_capture('导入 json\n令 r = json.解析(\'{"x": 10}\')\n打印(r["x"])\n')
    assert out == "10\n"


def test_json_parse_error():
    with pytest.raises(JishiError) as ei:
        run_source('导入 json\njson.解析("不是json")\n')
    assert "JSON" in str(ei.value)


def test_json_file_roundtrip(tmp_path):
    p = _posix(tmp_path / "d.json")
    out = run_capture(
        f'导入 json\n'
        f'json.写文件("{p}", {{"键": "值"}})\n'
        f'令 r = json.读文件("{p}")\n'
        f'打印(r["键"])\n')
    assert out == "值\n"


# -- 日期（M10） ------------------------------------------------------------

def test_date_add_days():
    out = run_capture('导入 日期\n打印(日期.加天数(5, "2026-09-01"))\n')
    assert out == "2026-09-06\n"


def test_date_sub_days():
    out = run_capture('导入 日期\n打印(日期.减天数(1, "2026-01-01"))\n')
    assert out == "2025-12-31\n"


def test_date_diff_days():
    out = run_capture(
        '导入 日期\n打印(日期.相差天数("2026-09-06", "2026-09-01"))\n')
    assert out == "5\n"


def test_date_compare():
    out = run_capture(
        '导入 日期\n'
        '打印(日期.早于("2026-01-01", "2026-06-01"))\n'
        '打印(日期.晚于("2026-06-01", "2026-01-01"))\n'
        '打印(日期.相等("2026-01-01", "2026-01-01"))\n')
    assert out == "真\n真\n真\n"


def test_date_weekday():
    out = run_capture('导入 日期\n打印(日期.星期名("2026-09-05"))\n')
    assert out == "六\n"


def test_date_format():
    out = run_capture(
        '导入 日期\n打印(日期.格式化("2026-09-05", "%Y年%m月%d日"))\n')
    assert out == "2026年09月05日\n"


def test_date_parse_error():
    with pytest.raises(JishiError) as ei:
        run_source('导入 日期\n日期.加天数(1, "昨天")\n')
    assert "日期" in str(ei.value)


# -- 正则（M10） ------------------------------------------------------------

def test_regex_findall():
    out = run_capture('导入 正则\n打印(正则.查找全部("\\\\d+", "有3个45数字"))\n')
    assert out == "['3', '45']\n"


def test_regex_replace():
    out = run_capture('导入 正则\n打印(正则.替换("\\\\d+", "#", "a1b22c333"))\n')
    assert out == "a#b#c#\n"


def test_regex_match():
    out = run_capture('导入 正则\n打印(正则.匹配("^\\\\d+", "123abc"))\n')
    assert out == "真\n"


def test_regex_search():
    out = run_capture('导入 正则\n打印(正则.搜索("\\\\d+", "abc123def"))\n')
    assert out == "123\n"


def test_regex_split():
    out = run_capture('导入 正则\n打印(正则.拆分(",", "a,b,c"))\n')
    assert out == "['a', 'b', 'c']\n"


def test_regex_group():
    out = run_capture(
        '导入 正则\n打印(正则.分组("(\\\\d+)-(\\\\d+)", "电话 123-4567"))\n')
    assert out == "['123', '4567']\n"


def test_regex_bad_pattern():
    with pytest.raises(JishiError) as ei:
        run_source('导入 正则\n正则.查找全部("(", "abc")\n')
    assert "正则" in str(ei.value)


# -- 网络（M10，mock 避免真实网络） -----------------------------------------

def test_network_get(monkeypatch):
    import urllib.request as _ur
    from jishi import runtime as _rt

    class _Resp:
        def read(self):
            return "你好，世界".encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(_ur, "urlopen", lambda *a, **k: _Resp())
    out = run_capture('导入 网络\n打印(网络.获取("http://example.com"))\n')
    assert out == "你好，世界\n"


def test_network_get_error(monkeypatch):
    import urllib.error as _ue
    import urllib.request as _ur

    def _boom(*a, **k):
        raise _ue.URLError("连接失败")
    monkeypatch.setattr(_ur, "urlopen", _boom)
    with pytest.raises(JishiError) as ei:
        run_source('导入 网络\n网络.获取("http://example.com")\n')
    assert "失败" in str(ei.value)


def test_network_post(monkeypatch):
    import urllib.request as _ur

    class _Resp:
        def read(self):
            return "已提交".encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    captured = {}

    def _fake_urlopen(req, *a, **k):
        captured["data"] = req.data
        return _Resp()

    monkeypatch.setattr(_ur, "urlopen", _fake_urlopen)
    out = run_capture(
        '导入 网络\n打印(网络.提交("http://x.com", "a=1"))\n')
    assert out == "已提交\n"
    assert captured["data"] == b"a=1"


def test_network_get_json(monkeypatch):
    import urllib.request as _ur

    class _Resp:
        def read(self):
            return '{"ok": true, "值": 42}'.encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(_ur, "urlopen", lambda *a, **k: _Resp())
    out = run_capture(
        '导入 网络\n令 r = 网络.获取JSON("http://x.com/api")\n打印(r["值"])\n')
    assert out == "42\n"
