# -*- coding: utf-8 -*-
"""M170 · SQL 参数化入口（`执行` / `执行查询` 的第三个参数）。

## 为什么要补

`数据库.执行(库, SQL)` 只有拼字符串这一条路。用户一旦写

    数据库.执行查询(库, "SELECT * FROM 账 WHERE 名字 = '" + 名字 + "'")

就把外部输入变成 SQL 的一部分了 —— 而 `audit_language` 之前只能把它记成 🟡
「引导缺口」：**安全的路根本不存在**，不能怪用户。

M170 补上 `?` 占位 + `参数们`：

    数据库.执行查询(库, "SELECT * FROM 账 WHERE 名字 = ?", ["x' OR '1'='1"])

## 判据为什么盯「注入串返回 0 条」

因为这是**唯一能区分「按值绑」和「拼字符串」的行为差异**：拼进去的话
`x' OR '1'='1` 会让条件恒真、返回全部；按值绑就老老实实一条也不匹配。
"""

import json
import sys

import pytest

sys.path.insert(0, ".")

from conftest import rust_exe_path                            # noqa: E402

源 = """\
导入 数据库
令 库 = 数据库.打开("账.jshdb")
数据库.建表(库, "账", ["名字", "金额"])
数据库.执行(库, "INSERT INTO 账 (名字, 金额) VALUES (?, ?)", ["小明", 90])
数据库.执行(库, "INSERT INTO 账 (名字, 金额) VALUES (?, ?)", ["小红", 85])
令 全 = 数据库.执行查询(库, "SELECT 名字, 金额 FROM 账 ORDER BY 金额")
令 恶 = 数据库.执行查询(库, "SELECT * FROM 账 WHERE 名字 = ?", ["x' OR '1'='1"])
打印("全部", 长度(全))
打印("注入", 长度(恶))
打印(全[1])
数据库.关闭(库)
"""

期望 = "全部 2\n注入 0\n{'名字': '小明', '金额': 90}\n"


def _清库(tmp_path):
    """每次跑之前删掉库文件 —— 否则上一次的 2 行会被算进来（假失败）。"""
    (tmp_path / "账.jshdb").unlink(missing_ok=True)


def _跑树遍历(tmp_path):
    import subprocess

    _清库(tmp_path)
    (tmp_path / "程序.jsh").write_text(源, encoding="utf-8", newline="\n")
    r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", "程序.jsh"],
                       cwd=str(tmp_path), capture_output=True, timeout=180, input=b"")
    return (r.stdout + r.stderr).decode("utf-8", "replace")


def _跑Rust(tmp_path):
    import subprocess

    _清库(tmp_path)
    (tmp_path / "程序.jsh").write_text(源, encoding="utf-8", newline="\n")
    r = subprocess.run([str(rust_exe_path()), "程序.jsh"], cwd=str(tmp_path),
                       capture_output=True, timeout=180, input=b"")
    return (r.stdout + r.stderr).decode("utf-8", "replace")


def _跑Node(tmp_path):
    import subprocess

    _清库(tmp_path)

    from oracle.jishi import serialize
    from oracle.jishi.compiler import compile_source

    p = tmp_path / "程序.json"
    p.write_text(serialize.dumps(compile_source(源, "程序.jsh")), encoding="utf-8")
    r = subprocess.run(["node", str(rust_exe_path().parents[3] / "oracle" / "node" / "index.js"),
                        str(p)], cwd=str(tmp_path), capture_output=True, timeout=180)
    return (r.stdout + r.stderr).decode("utf-8", "replace")


def test_树遍历_参数按值绑(tmp_path):
    """注入串当**值**匹配 ⇒ 一条也不命中；插入也能带参数。"""
    出 = _跑树遍历(tmp_path)
    assert 出 == 期望, 出


@pytest.mark.skipif(not rust_exe_path().is_file(), reason="没编 jishi-rs")
def test_Rust_与树遍历一致(tmp_path):
    assert _跑Rust(tmp_path) == _跑树遍历(tmp_path)


def test_参数不是列表时给中文提示(tmp_path):
    """给个字符串当「参数们」→ 必须说清「要列表」，而不是让它当字符序列绑进去。"""
    import subprocess

    (tmp_path / "程序.jsh").write_text(
        '导入 数据库\n令 库 = 数据库.打开("账2.jshdb")\n'
        '数据库.建表(库, "账", ["名字"])\n'
        '数据库.执行查询(库, "SELECT * FROM 账 WHERE 名字 = ?", "小明")\n',
        encoding="utf-8", newline="\n")
    r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", "程序.jsh"],
                       cwd=str(tmp_path), capture_output=True, timeout=180, input=b"")
    出 = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert "参数们" in 出 and "列表" in 出, 出[:400]


def test_审计里的_SQL_一条已经转绿():
    """`audit_language` 的 SQL 那条：从 🟡「引导缺口」变成 ✅。

    ⚠️ 这条判据是**跨模块的**：它保证「审计脚本认得出新入口」。
    只补实现不改审计的话，报告会一直骗人说「没有安全的路」。
    """
    import subprocess

    脚本 = rust_exe_path().parents[3] / "tools" / "audit_language.py"
    r = subprocess.run([sys.executable, str(脚本)], capture_output=True, timeout=600)
    出 = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
    行 = [l for l in 出.splitlines() if "SQL 注入" in l]
    assert 行, 出[-800:]
    assert "✅" in 行[0], f"SQL 那条还是缺口：{行[0]}"
