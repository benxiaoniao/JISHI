# -*- coding: utf-8 -*-
"""M193 · 数据库：事务 / 迁移 / 查询直出表格 / 注入审计。

## 这一站的四条判据

1. **回滚后一行不多**：`开始 → 改 → 回滚`，值必须回到原样；
2. **`用事务` 异常自动回滚**：这是「别忘写回滚」的糖，糖必须真管用；
3. **迁移跑两遍版本不变**：第二遍返回空列表，表也没被重复建；
4. **注入样本无害**：把 `'; DROP TABLE 账; --` 当**参数**传进去，表还在
   —— 参数化是这一站的底线，拼字符串的写法不该存在。

## 类型往返（如实说明，不含糊）

日期存进去是 **ISO 文本**（`2026-09-01 00:00:00`），取回来也是同样文本。
**不是整数秒** —— 路线图原来想统一存整数秒，但基石的 `Val` 里没有「日期」
这一支（日期是 `日期` 模块的对象，进库前已经被文本化），改成整数秒要动
库文件的既有格式与读取路径，那是单独一站的活。这里把**现状**钉住：
同一段文本存进去、取出来不变，别悄悄变。
"""

import shutil
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


@pytest.fixture(scope="module", autouse=True)
def _要有宿主():
    if not rust_exe_path().is_file():
        pytest.skip("没有 Rust 宿主（cargo build --release）")


#: 建表 + 两行初始数据（每个用例自己一份内存库）
开头 = (
    "导入 数据库\n"
    '令 库 = 数据库.打开(":memory:")\n'
    '数据库.执行(库, "CREATE TABLE 账 (姓名 TEXT PRIMARY KEY, 余额 INTEGER)")\n'
    '数据库.插入(库, "账", {"姓名": "甲", "余额": 100})\n'
    '数据库.插入(库, "账", {"姓名": "乙", "余额": 100})\n'
)


def test_回滚一行不多(tmp_path):
    源 = 开头 + (
        "数据库.开始(库)\n"
        '数据库.修改(库, "账", {"姓名": "甲"}, {"余额": 999})\n'
        '数据库.插入(库, "账", {"姓名": "丙", "余额": 1})\n'
        "数据库.回滚(库)\n"
        '打印(数据库.查询(库, "账", {"姓名": "甲"})[0]["余额"])\n'
        '打印(数据库.条数(库, "账"))\n'
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-600:]
    assert 出.split() == ["100", "2"], f"回滚不干净：{出!r}"


def test_提交才落盘(tmp_path):
    源 = 开头 + (
        "数据库.开始(库)\n"
        '数据库.修改(库, "账", {"姓名": "甲"}, {"余额": 500})\n'
        '打印("事务中 ", 数据库.查询(库, "账", {"姓名": "甲"})[0]["余额"])\n'
        "数据库.提交(库)\n"
        '打印("提交后 ", 数据库.查询(库, "账", {"姓名": "甲"})[0]["余额"])\n'
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-600:]
    assert "500" in 出 and "事务中" in 出 and "提交后" in 出, 出
    assert 出.count("500") == 2, f"事务中 / 提交后都该是 500：{出!r}"


def test_用事务异常自动回滚(tmp_path):
    源 = (
        "导入 数据库\n"
        '令 库 = 数据库.打开(":memory:")\n'
        '数据库.执行(库, "CREATE TABLE 账 (姓名 TEXT PRIMARY KEY, 余额 INTEGER)")\n'
        '数据库.插入(库, "账", {"姓名": "甲", "余额": 100})\n'
        "函数 会失败()：\n"
        '    数据库.修改(库, "账", {"姓名": "甲"}, {"余额": 1})\n'
        '    抛出 值错误("故意失败")\n'
        "尝试：\n"
        "    数据库.用事务(库, 会失败)\n"
        "捕获 值错误：\n"
        '    打印("接住了")\n'
        '打印(数据库.查询(库, "账", {"姓名": "甲"})[0]["余额"])\n'
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-600:]
    assert "接住了" in 出, 出
    assert 出.strip().endswith("100"), f"异常之后没回滚：{出[-200:]!r}"


def test_迁移跑两遍版本不变(tmp_path):
    目录 = tmp_path / "迁移"
    目录.mkdir()
    (目录 / "001_建表.sql").write_text(
        "CREATE TABLE 账 (姓名 TEXT PRIMARY KEY, 余额 INTEGER);\n"
        "INSERT INTO 账 VALUES ('甲', 100);\n", encoding="utf-8", newline="\n")
    (目录 / "002_增列.sql").write_text(
        "ALTER TABLE 账 ADD COLUMN 备注 TEXT;\n", encoding="utf-8", newline="\n")
    脚本 = tmp_path / "迁移.jsh"
    脚本.write_text(
        "导入 数据库\n"
        '令 库 = 数据库.打开(":memory:")\n'
        '打印(数据库.迁移(库, "迁移"))\n'
        '打印(数据库.迁移(库, "迁移"))\n'
        '打印(数据库.条数(库, "账"))\n',
        encoding="utf-8", newline="\n")
    r = subprocess.run([str(rust_exe_path()), str(脚本)], capture_output=True,
                       timeout=180, cwd=str(tmp_path))
    出 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert r.returncode == 0, 出[-800:]
    行 = [x for x in 出.strip().splitlines() if x]
    assert 行[0] == "['001_建表.sql', '002_增列.sql']", 出
    assert 行[1] == "[]", f"第二遍不该再跑：{出!r}"
    assert 行[2] == "1", f"表被重复建了？{出!r}"


def test_注入样本无害(tmp_path):
    """🔴 参数化审计：把一句「看起来像 SQL」的文本当参数传进去，表必须还在。"""
    注入 = "'; DROP TABLE 账; --"
    源 = 开头 + (
        f'数据库.插入(库, "账", {{"姓名": {注入!r}, "余额": 5}})\n'
        '令 记 = 数据库.执行查询(库, "SELECT 姓名 FROM 账 WHERE 姓名 = ?", [' + repr(注入) + '])\n'
        '打印(记[0]["姓名"] == ' + repr(注入) + ')\n'
        '打印(数据库.有表(库, "账"))\n'
        '打印(数据库.条数(库, "账"))\n'
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-800:]
    assert 出.split()[:3] == ["真", "真", "3"], f"注入伤了表：{出!r}"


def test_查询到表格(tmp_path):
    源 = 开头 + (
        '令 表 = 数据库.查询到表格(库, "SELECT 姓名, 余额 FROM 账 ORDER BY 姓名")\n'
        '打印(类型(表))\n'
        '打印(表头(表))\n'
        '打印(数据行(表))\n'
    ).replace("表头(表)", "表头(表)").replace("数据行(表)", "数据行(表)")
    # 表格模块的函数要走 表格. 前缀
    源 = 源.replace("打印(表头(表))", "打印(表头(表))")
    码, 出 = _跑(源, tmp_path)
    assert 码 != 0 or "表格" in 出, 出        # 没导入 表格 时至少不能崩得莫名其妙

    源2 = 开头 + (
        "导入 表格\n"
        '令 表 = 数据库.查询到表格(库, "SELECT 姓名, 余额 FROM 账 ORDER BY 姓名")\n'
        '打印("类型 ", 类型(表))\n'
        '打印(表格.转TSV(表))\n'
    )
    码2, 出2 = _跑(源2, tmp_path, "t2.jsh")
    assert 码2 == 0, 出2[-600:]
    assert "类型  表格" in 出2, 出2
    assert "姓名\t余额" in 出2, 出2
    assert "甲\t100" in 出2, 出2


def test_空值进表格是空文本(tmp_path):
    源 = "导入 数据库\n导入 表格\n" + (
        '令 库 = 数据库.打开(":memory:")\n'
        '数据库.执行(库, "CREATE TABLE t (a TEXT, b TEXT)")\n'
        '数据库.插入(库, "t", {"a": "x"})\n'
        '令 表 = 数据库.查询到表格(库, "SELECT a, b FROM t")\n'
        '打印(表格.转TSV(表))\n'
        '打印(长度(表格.数据行(表格.丢弃缺失(表))))\n'
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-600:]
    assert "x\t\n" in 出, f"空值应当是空文本：{出!r}"
    assert 出.strip().endswith("0"), f"丢弃缺失应当把这一行删掉：{出!r}"


def test_日期往返是文本(tmp_path):
    """如实钉住现状：日期存的是 ISO **文本**，取回来一字不差。"""
    源 = 开头 + (
        '数据库.执行(库, "ALTER TABLE 账 ADD COLUMN 日 TEXT")\n'
        '数据库.修改(库, "账", {"姓名": "甲"}, {"日": "2026-09-01 00:00:00"})\n'
        '令 记 = 数据库.查询(库, "账", {"姓名": "甲"})[0]\n'
        '打印(记["日"] == "2026-09-01 00:00:00")\n'
        '打印(类型(记["日"]))\n'
    )
    码, 出 = _跑(源, tmp_path)
    assert 码 == 0, 出[-600:]
    assert 出.split()[0] == "真", 出
    assert "文本" in 出, 出
