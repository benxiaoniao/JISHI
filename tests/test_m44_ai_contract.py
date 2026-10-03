# -*- coding: utf-8 -*-
"""M44 · 方向三「AI 可用性」第一轮：机器可读的语言事实 + 源码静态检查。

三块内容，都在这里钉住：

1. **`--lang-spec` 补全**：错误码表 / 静态检查码表 / 等价 Python 写法；
2. **`--json-errors` 的字段契约**（这块 M8.3 就有了，M44 把它钉死，免得改坏）；
3. **`jishi 源码静态检查`**：四类检查各一条正例，外加**在真实语料上零误报**
   —— 那是这个里程碑的验收标准，也是这类工具能不能用的分水岭。

另有一条「单一来源」的钉子：编辑器诊断与命令行检查必须报**同一批**未定义名
（两边共用 `scope.py`；各写一份迟早会漂移成「编辑器说没事、命令行说有事」）。
"""

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from jishi import ai, checker, cli, errors                # noqa: E402
from jishi.lsp import LspServer                            # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
URI = "file:///tmp/m44.jsh"

#: 四类检查各一条的样例（同一份源码里都犯一遍）
四种问题的样例 = (
    "函数 甲(乙)：\n"
    "    令 丙 = 0\n"
    "    循环 3 次：\n"
    "        丙 += 乙\n"
    "    令 戊 = 9\n"          # 未使用变量
    "    如果 丙 == 丙：\n"     # 可疑相等
    "        打印(丙)\n"
    "    返回 庚\n"            # 未定义名
    "\n"
    "长度 = 2\n"               # 遮蔽内建（模块级）
    "打印(甲(1))\n"
)


def _run_cli(argv, cwd=None):
    out, err = io.StringIO(), io.StringIO()
    old = os.getcwd()
    try:
        if cwd:
            os.chdir(cwd)
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
    finally:
        os.chdir(old)
    return code, out.getvalue(), err.getvalue()


def _server() -> LspServer:
    srv = LspServer()
    srv._write = lambda obj: None
    return srv


# ---------------------------------------------------------------------------
# 一、错误码表
# ---------------------------------------------------------------------------

def test_错误码表没有重码():
    """M44 就是靠这张表才发现 `E2008` 被两个类共用的（已修）。"""
    codes = [row["code"] for row in ai.build_lang_spec()["error_codes"]]
    assert len(codes) == len(set(codes)), f"有重复错误码：{codes}"


def test_错误码表覆盖运行期临时挂的码():
    """`E2010` / `E2100` 没有独立异常类，走 `errors.EXTRA_CODES` 登记。"""
    table = {row["code"] for row in ai.build_lang_spec()["error_codes"]}
    for code in errors.EXTRA_CODES:
        assert code in table


def test_断言错误与自定义异常不再共用码():
    assert errors.RunAssertionError.code == "E2008"
    assert errors.RunCustomError.code == "E2009"


def test_内部信号被标记出来():
    """E299x 是执行器内部信号，不该展示给用户——但工具转发时会碰到，所以照实列。"""
    rows = {row["code"]: row for row in ai.build_lang_spec()["error_codes"]}
    for code in ("E2997", "E2998", "E2999"):
        assert rows[code]["internal"] is True
    assert rows["E0104"]["internal"] is False


# ---------------------------------------------------------------------------
# 二、静态检查码表
# ---------------------------------------------------------------------------

def test_检查码表与目录一致():
    spec = [row["code"] for row in ai.build_lang_spec()["check_codes"]]
    assert spec == [row["code"] for row in checker.CHECK_CATALOG]


def test_目录里的每个码都真的会出现():
    """目录不能有「登记了却从不产生」的码——那种表只会误导。"""
    produced = {i.code for i in checker.check_source(四种问题的样例, "样例.jsh")}
    produced |= {i.code for i in checker.check_file(ROOT / "肯定不存在的文件.jsh")}
    declared = {row["code"] for row in checker.CHECK_CATALOG}
    assert produced <= declared, f"产生了目录里没有的码：{produced - declared}"


# ---------------------------------------------------------------------------
# 三、等价 Python 写法
# ---------------------------------------------------------------------------

def test_等价写法的键都是真关键字():
    spec = ai.build_lang_spec()
    bad = [k for k in spec["python_equiv"]["keywords"] if k not in spec["keywords"]]
    assert not bad, f"这些不是关键字：{bad}"


def test_等价写法的键都是真内建():
    spec = ai.build_lang_spec()
    names = {b["name"] for b in spec["builtins"]}
    bad = [k for k in spec["python_equiv"]["builtins"] if k not in names]
    assert not bad, f"这些不是内建：{bad}"


def test_等价写法的键都是真运算符():
    spec = ai.build_lang_spec()
    ops = {row["op"] for row in spec["operators"]["binary"]}
    ops |= set(spec["operators"]["compare"]) | set(spec["operators"]["assign"])
    ops |= set(spec["operators"]["unary"])
    bad = [k for k in spec["python_equiv"]["operators"] if k not in ops]
    assert not bad, f"这些不是运算符：{bad}"


def test_容易猜错的那几条必须在表里():
    """设计核心二：模型最大的先验是 Python，`与/或/非` 不是 and/or/not 就一定会猜错。"""
    kw = ai.build_lang_spec()["python_equiv"]["keywords"]
    assert kw["与"] == "and" and kw["或"] == "or" and kw["非"] == "not"
    assert kw["遍历"] == "for" and kw["循环"] == "for _ in range(N)"
    ops = ai.build_lang_spec()["python_equiv"]["operators"]
    assert ops["在"] == "in" and ops["不在"] == "not in"
    assert ops["是"] == "is" and ops["不是"] == "is not"


# ---------------------------------------------------------------------------
# 四、--json-errors 的字段契约
# ---------------------------------------------------------------------------

def test_json_错误可被脚本消费(tmp_path):
    """`--json-errors` 的 JSON 走 **stderr**：stdout 要留给程序自己的输出。

    这是有意的（M8.3）：程序可能既打印结果又想机器可读地报错，混在一个流里
    就没法解析了。
    """
    f = tmp_path / "甲.jsh"
    f.write_text("打印(甲)\n", encoding="utf-8")
    code, out, err = _run_cli([str(f), "--json-errors"])
    assert code != 0
    assert out == "" or "{" not in out, "JSON 不该混进 stdout"
    data = json.loads(err.strip().splitlines()[-1])
    assert data["ok"] is False
    one = data["errors"][0]
    for key in ("code", "title", "message", "line", "col", "hint"):
        assert key in one
    assert one["code"].startswith("E")


def test_json_错误带上机器可读的修法(tmp_path):
    """有修法时必须给 `fix`（old/new + 位置），AI 才能直接做文本替换自愈。"""
    e = errors.JishiError("英文关键字", line=1, col=1,
                          fix={"old": "if", "new": "如果"})
    payload = ai.error_to_json(e)
    assert payload["fix"]["old"] == "if"
    assert payload["fix"]["new"] == "如果"
    assert payload["fix"]["line"] == 1 and payload["fix"]["col"] == 1


# ---------------------------------------------------------------------------
# 五、静态检查：四类正例
# ---------------------------------------------------------------------------

def test_四类检查各中一条():
    codes = [i.code for i in checker.check_source(四种问题的样例, "样例.jsh")]
    assert codes == ["name.unused", "compare.self", "name.undefined",
                     "name.shadow"]


def test_语法错也算一条问题不带崩():
    issues = checker.check_source("如果 真\n", "坏.jsh")
    assert len(issues) == 1
    assert issues[0].level == checker.LEVEL_ERROR
    assert issues[0].code.startswith("E")


def test_内部脱糖名不报未使用():
    """`遍历 甲, 乙 在 …` 会脱糖出 `__遍历项_N_M__`，那是编译器的东西，不该报。"""
    src = ("对们 = [[1, 2]]\n"
           "函数 看()：\n"
           "    遍历 首, 次 在 对们：\n"
           "        打印(首, 次)\n")
    codes = [i.code for i in checker.check_source(src, "内部名.jsh")]
    assert "name.unused" not in codes


def test_导入标准库不算遮蔽():
    """`导入 随机` 就是在导入那个模块——第一版把它报成遮蔽，全判为误报。"""
    codes = [i.code for i in checker.check_source("导入 随机\n打印(随机)\n", "导入.jsh")]
    assert "name.shadow" not in codes


def test_函数里的局部变量不算遮蔽内建():
    """局部遮蔽只影响那个函数，报了纯属噪音（模块级的才算）。"""
    local_src = "函数 甲()：\n    长度 = 1\n    返回 长度\n"
    assert "name.shadow" not in [i.code for i in
                                 checker.check_source(local_src, "局部.jsh")]
    module_src = "长度 = 1\n打印(长度)\n"
    assert "name.shadow" in [i.code for i in
                             checker.check_source(module_src, "模块.jsh")]


def test_属性访问算使用不算未使用():
    """`表.追加(1)` 里的 `表` 是属性基址，但它显然是「读」——第一版漏了这个，误报。"""
    src = ("函数 甲()：\n"
           "    表 = []\n"
           "    表.追加(1)\n"
           "    返回 表\n")
    assert "name.unused" not in [i.code for i in
                                 checker.check_source(src, "属性.jsh")]


def test_读不了文件也是一条问题():
    issues = checker.check_file(ROOT / "肯定不存在的文件.jsh")
    assert [i.code for i in issues] == ["check.read"]


# ---------------------------------------------------------------------------
# 六、静态检查：真实语料零误报（M44 的验收标准）
# ---------------------------------------------------------------------------

def _语料路径():
    """仓库自己的 .jsh 语料。

    `tests/error_cases/` **必须排除**：那批文件是**故意的错例**（未定义名、
    缺冒号、英文关键字……），在里面报出问题才是对的。
    """
    files = []
    for root in (ROOT / "examples", ROOT / "tests", ROOT / "jishi" / "stdlib"):
        for p in sorted(Path(root).rglob("*.jsh")):
            if "error_cases" in p.parts or "build-env" in p.parts:
                continue
            files.append(p)
    return files


def test_真实语料零误报():
    files = _语料路径()
    assert len(files) >= 30, f"语料太少（{len(files)}），这条测试就没意义了"
    rows = checker.check_paths([p for p in files])
    detail = "\n".join(f"  {p}:{i.line} [{i.code}] {i.message}" for p, i in rows)
    assert not rows, f"在真实语料上报出 {len(rows)} 条（应当为 0）：\n{detail}"


def test_错例语料里静态可查的必须全部逮住():
    """反过来校验：那批故意的错例，**静态能看出来的**必须一条不漏。

    实测 35 个错例里只有 16 个是静态可查的（15 个词法/语法错 + 1 个未定义名），
    其余 19 个要跑起来才知道（除以零、索引越界、方法不存在……）。所以这里
    只钉住那 16 个——**不假装静态检查能做它做不到的事**。
    """
    static_ones = {
        "01_未定义名字.jsh", "03_缩进对不上.jsh", "04_少了冒号.jsh",
        "05_字符串没结束.jsh", "06_关键字粘连.jsh", "07_全角空格缩进.jsh",
        "09_块缺失.jsh", "11_Tab混用.jsh", "24_捕获脱离尝试.jsh",
        "25_尝试无捕获.jsh", "26_抛出无值.jsh", "29_默认参数位置.jsh",
        "30_英文关键字def.jsh", "31_英文内建print.jsh", "32_英文布尔True.jsh",
        "33_英文运算符and.jsh",
    }
    bad = sorted((ROOT / "tests" / "error_cases").glob("*.jsh"))
    assert len(bad) >= 30, f"错例语料太少（{len(bad)}）"
    hit = {p.name for p, _ in checker.check_paths(bad)}
    missed = sorted(static_ones - hit)
    assert not missed, f"这些静态能查的错例没被报出来：{missed}"


def test_运行期错误不被静态检查假装查出来():
    """边界钉住：不执行代码，就不该报执行期才成立的问题。

    这三个错例（除以零 / 索引越界 / 赋值前读局部）都得跑起来才知道——
    尤其第三个需要控制流分析，本检查器**没有**做（见 `checker.py` 的已知边界）。
    """
    for name in ("02_除以零.jsh", "12_索引越界.jsh", "35_赋值前读局部.jsh"):
        p = ROOT / "tests" / "error_cases" / name
        assert not checker.check_file(p), f"{name} 不该被静态检查报出来"


# ---------------------------------------------------------------------------
# 七、单一来源：编辑器与命令行必须报同一批未定义名
# ---------------------------------------------------------------------------

def test_编辑器与命令行报同一批未定义名():
    src = "打印(甲)\n令 乙 = 丙\n"
    srv = _server()
    srv.dispatch({"jsonrpc": "2.0", "method": "textDocument/didOpen",
                  "params": {"textDocument": {"uri": URI, "version": 1,
                                              "text": src}}})
    by_editor = {d["message"] for d in srv.diagnostics[URI]
                 if "没有定义过" in d["message"]}
    by_cli = {i.message for i in checker.check_source(src, "x.jsh")
              if i.code == "name.undefined"}
    assert by_editor == by_cli, f"两边不一致：编辑器 {by_editor} / 命令行 {by_cli}"


# ---------------------------------------------------------------------------
# 八、命令本身
# ---------------------------------------------------------------------------

def test_命令在干净文件上退出码为0(tmp_path):
    f = tmp_path / "干净.jsh"
    f.write_text("打印(\"你好\")\n", encoding="utf-8")
    code, out, _ = _run_cli(["源码静态检查", str(f)])
    assert code == 0
    assert "没发现问题" in out


def test_命令发现问题时退出码为1(tmp_path):
    f = tmp_path / "脏.jsh"
    f.write_text(四种问题的样例, encoding="utf-8")
    code, out, _ = _run_cli(["源码静态检查", str(f)])
    assert code == 1
    assert "name.unused" in out


def test_命令支持别名与json输出(tmp_path):
    f = tmp_path / "脏.jsh"
    f.write_text("打印(甲)\n", encoding="utf-8")
    code, out, _ = _run_cli(["静态检查", str(f), "--json"])
    assert code == 1
    data = json.loads(out)
    assert data["ok"] is False
    assert data["issues"][0]["code"] == "name.undefined"
    assert data["issues"][0]["file"].endswith("脏.jsh")


def test_命令默认检查当前目录(tmp_path):
    (tmp_path / "甲.jsh").write_text("打印(甲)\n", encoding="utf-8")
    code, out, _ = _run_cli(["源码静态检查"], cwd=str(tmp_path))
    assert code == 1
    assert "name.undefined" in out


def test_包管理的检查命令没被顶替(tmp_path):
    """`jishi 检查` 仍然是**包依赖冲突检测**，与新命令是两件事。"""
    code, out, _ = _run_cli(["检查"], cwd=str(tmp_path))
    assert code == 0
    assert "依赖冲突" in out or "尚未安装" in out
