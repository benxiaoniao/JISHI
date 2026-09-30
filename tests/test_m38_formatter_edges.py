# -*- coding: utf-8 -*-
"""M38 B4：格式化器的边界打磨。

来由：M21 的格式化器已经有「幂等 + token 流等价」的语料对拍，但语料只覆盖了
`examples/` 与 `tests/cases/` 里**写得规规矩矩**的代码。这里补的是一批
「用户真会写、但语料里没有」的边界形状，实测挖出四个真问题（都已在
`jishi/formatter.py` 修掉）：

1. **字符串里的 `#` 被当成注释**——`令 甲 = "含 # 号"` 会被从字符串中间劈开，
   格式化直接报「字符串没有正常结束」（插值字符串同理）。修法：注释识别改成
   走状态机扫（`_scan_code`），不再用 `line.find("#")`。
2. **括号续行里的注释被吃掉**——多行调用的 `1,  # 注释` 那一行，注释直接消失
   （与「保留注释」的设计承诺相悖）。修法：续行分支先拆「代码 / 注释」再排版。
3. **带 BOM 的文件直接崩**——Windows 记事本存「UTF-8 带 BOM」时，词法器把
   U+FEFF 判成「无法识别的字符」，连跑都跑不起来。修法：词法器只剥**开头那一个**
   BOM；格式化器保留它（否则 `--check` 会把每个带 BOM 的文件都判成没格式化）。
4. **`jishi 格式化 <目录>` 甩英文 traceback**——README 里写的就是
   `jishi 格式化 --check examples/`，而 `_read_source` 遇到目录会抛
   `PermissionError`。修法：目录走 `_format_tree`（递归 + 区分「不合风格」
   与「解析不了」），`_read_source` 也给中文提示。

另外记一条同轮踩到的**实现坑**：`body = raw[len(lead):]` 原本写在「普通代码行」
分支里，续行分支要用它时就拿到了**上一轮循环的旧值**——续行被整段替换成上一行的
内容，输出直接语法错。这类 bug 只在多行结构上暴露，单行用例永远看不见。
"""

from __future__ import annotations

import io
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from jishi import cli                            # noqa: E402
from jishi.formatter import format_source        # noqa: E402
from jishi.parser import parse                   # noqa: E402
from jishi.tokenizer import tokenize             # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

#: 边界语料：(名字, 源码)。每条都要过「幂等 + token 流等价 + 解析结果不变」。
EDGE_CASES: list[tuple[str, str]] = [
    ("条件表达式", '令 等级="成年" 如果 年龄 >= 18 否则 "未成年"\n'),
    ("条件表达式-右结合", '令 甲 ="一" 如果 a==1 否则 "二" 如果 a==2 否则 "其他"\n'),
    ("条件表达式-实参", "打印(最大(1, 5 如果 真 否则 9))\n"),
    ("字符串含井号", '令 甲 = "含 # 号"\n打印(甲)\n'),
    ("字符串含井号+行尾注释", '令 甲 = "a # b"  # 真注释\n'),
    ("注释含引号", '令 甲 = 1  # 他说"你好"\n'),
    ("插值字符串含井号", "令 甲 = 1\n打印(`井号 {甲} # 还是插值`)\n"),
    ("插值字符串含括号", "令 甲 = 1\n打印(`值({甲})`)\n"),
    ("块注释", "#- 块注释\n如果 甲：  # 原样\n-#\n令 乙 = 2\n"),
    ("三引号跨行", '令 甲 = """\n  if x:\n    打印(1)\n"""\n'),
    ("行内三引号", '令 甲 = """单行三引号"""\n'),
    ("切片与字典冒号", '令 甲 = 乙[1:3]\n令 丙 = {"甲":1,"乙":2}\n令 丁 = 乙[ 1 : 3 ]\n'),
    ("类型标注与返回标注", "函数 加(甲:整数,乙:整数) -> 整数：\n    返回 甲+乙\n"),
    ("默认参数（等号两侧留空格）", '函数 打(名字, 语气 = "你好")：\n    打印(名字, 语气)\n'),
    ("关键字实参", '打印(1, 2, sep = "、")\n'),
    ("匿名函数", "令 双 = 函数(x)：x*2\n打印(双(3))\n"),
    ("解包星号", "令 甲,*余 = [1,2,3]\n函数 f(*参数)：\n    返回 参数\n"),
    ("匹配与守卫", '匹配 90：\n    情形 90 如果 真：\n        打印("甲")\n'
                   '    情形 其他：\n        打印("乙")\n'),
    ("枚举", "枚举 颜色：\n    红\n    绿\n"),
    ("用 … 为", '用 打开("x.txt") 为 f：\n    打印(f)\n'),
    ("集合字面量", "令 甲 = {1,2,3}\n"),
    ("超()", "类 甲：\n    函数 问(自身)：\n        返回 1\n"
             "类 乙(甲)：\n    函数 问(自身)：\n        返回 超().问()\n"),
    ("空块占位冒号", "类 甲：\n    :\n"),
    ("一元负号三种位置", "令 甲 = -1\n令 乙 = -甲\n令 丙 = 2 * -3\n"
                         "令 丁 = 列表[-1]\n令 戊 = -2**2\n"),
    ("加减号的二元与一元连用", "打印(甲 - -乙)\n打印(甲+-乙)\n"),
    ("在/不在/是/不是", "打印(1 在 [1] 或 2 不在 [2])\n打印(甲 是 空)\n打印(甲 不是 空)\n"),
    ("括号内多行续行", "令 甲 = 最大(\n    1,\n    2,\n)\n令 乙 = [\n    1,\n    2,\n]\n"),
    ("续行里的注释", "令 甲 = 最大(\n    1,  # 注释\n    2,\n)\n"),
    ("链式调用", "令 甲 = [1].追加(2).追加(3)\n"),
    ("行尾空白与连续空行", "令 甲 = 1   \n\n\n\n令 乙 = 2\n"),
    ("tab 缩进", "如果 真：\n\t打印(1)\n\t如果 真：\n\t\t打印(2)\n"),
    ("注释行缩进归位", "如果 真：\n        # 注释\n    打印(1)\n"),
    ("尾随逗号", '令 甲 = [1,2,]\n令 乙 = {"a":1,}\n'),
    ("链式比较", "打印(1 < 甲 < 10)\n"),
    ("位运算内建", "打印(位与(12,10), 左移(1,4))\n"),
    ("字典合并方法", "令 全 = 甲.合并(乙)\n"),
    ("只有注释", "# 只有注释\n# 第二行\n"),
    ("末行无换行", "令 甲 = 1\n令 乙 = 2"),
    ("CRLF 输入", "令 甲 = 1\r\n令 乙 = 2\r\n"),
    ("BOM 输入", "\ufeff令 甲 = 1\n"),
    ("别名导入", "导入 数学 为 数\n打印(数.平方(4))\n"),
    ("字符串里的未闭合括号", '令 甲 = "(未闭合"\n打印(甲)\n'),
]


def _sig(text: str):
    return [(t.type, t.value) for t in tokenize(text, "<边界>")
            if t.type not in ("INDENT", "DEDENT", "NEWLINE", "EOF")]


def _parses(text: str) -> bool:
    src = text.replace("\r\n", "\n").replace("\r", "\n")
    try:
        parse(tokenize(text, "<边界>"), src.split("\n"), "<边界>")
        return True
    except Exception:                                        # noqa: BLE001
        return False


@pytest.mark.parametrize("name,src", EDGE_CASES, ids=[c[0] for c in EDGE_CASES])
def test_edge_case_is_idempotent_and_semantics_preserved(name, src):
    out = format_source(src, "<边界>")
    assert format_source(out, "<边界>") == out, f"{name}：格式化不幂等"
    assert _sig(out) == _sig(src), f"{name}：token 流变了"
    assert _parses(out) == _parses(src), f"{name}：解析结果变了"


def test_hash_inside_string_is_not_a_comment():
    """回归钉：字符串里的 `#` 不是注释（曾把字符串劈开、直接报「没正常结束」）。"""
    assert format_source('令 甲 = "含 # 号"\n') == '令 甲 = "含 # 号"\n'
    assert format_source("令 甲 = 1\n打印(`井号 {甲} # 还是插值`)\n") == \
        "令 甲 = 1\n打印(`井号 {甲} # 还是插值`)\n"


def test_string_with_hash_keeps_trailing_comment():
    out = format_source('令 甲 = "a # b"  #  真注释\n')
    assert out == '令 甲 = "a # b"  #  真注释\n'
    assert out.count("#") == 2


def test_comment_inside_bracket_continuation_survives():
    """回归钉：多行调用里那一行的注释不能被吃掉（与「保留注释」的承诺相悖）。"""
    src = "令 甲 = 最大(\n    1,  # 注释\n    2,\n)\n"
    assert format_source(src) == src


def test_continuation_lines_are_not_replaced_by_previous_line():
    """回归钉：续行的内容不能被上一行覆盖（`body` 用了旧值那个坑）。"""
    out = format_source("令 甲 = 最大(\n    1,\n    2,\n)\n")
    assert out.split("\n")[:4] == ["令 甲 = 最大(", "    1,", "    2,", ")"]
    assert out.count("令 甲 = 最大(") == 1


def test_bom_is_kept_by_formatter():
    """带 BOM 的文件格式化后仍带 BOM（否则 `--check` 会把它们全判成没格式化）。"""
    out = format_source("\ufeff令 甲=1\n")
    assert out.startswith("\ufeff")
    assert out == "\ufeff令 甲 = 1\n"


def test_bom_only_file():
    assert format_source("\ufeff") == "\ufeff"


def test_bom_file_runs_and_checks_clean(tmp_path):
    """带 BOM 的文件要能**跑起来**、也要 `--check` 通过（词法器剥 BOM）。"""
    f = tmp_path / "带BOM.jsh"
    f.write_text("\ufeff令 甲 = 1\n打印(甲)\n", encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "jishi.cli", str(f)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(ROOT))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "1"
    code, out, err = _run_cli(["格式化", str(f), "--check"])
    assert code == 0, err


def test_middle_bom_is_still_an_error():
    """只剥开头的 BOM：藏在代码中间的不可见字符仍要报错（别把问题放过去）。"""
    with pytest.raises(Exception) as ei:                     # noqa: B017
        tokenize("令 甲\ufeff = 1\n", "<边界>")
    assert "无法识别的字符" in str(ei.value)


# ---------------------------------------------------------------------------
# CLI：目录（README 里写了却一直是崩的）
# ---------------------------------------------------------------------------

def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "项目"
    (root / "子目录").mkdir(parents=True)
    (root / "好的.jsh").write_text("令 甲 = 1\n", encoding="utf-8")
    (root / "乱的.jsh").write_text("令  乙=2\n", encoding="utf-8")
    (root / "子目录" / "也乱.jsh").write_text("令 丙  =  3\n", encoding="utf-8")
    return root


def test_dir_check_lists_unformatted_files(tmp_path):
    root = _tree(tmp_path)
    code, out, err = _run_cli(["格式化", str(root), "--check"])
    assert code == 1
    assert "2/3 个文件没有按统一风格书写" in err
    assert "乱的.jsh" in err and "也乱.jsh" in err
    assert "好的.jsh" not in err


def test_dir_write_formats_all_and_returns_zero(tmp_path):
    root = _tree(tmp_path)
    code, out, err = _run_cli(["格式化", str(root), "--write"])
    assert code == 0, err
    assert "共 3 个文件，改写了 2 个" in out
    assert (root / "乱的.jsh").read_text(encoding="utf-8") == "令 乙 = 2\n"
    assert (root / "子目录" / "也乱.jsh").read_text(
        encoding="utf-8") == "令 丙 = 3\n"
    # 再检查一次应当全绿
    assert _run_cli(["格式化", str(root), "--check"])[0] == 0


def test_dir_without_flags_explains_what_to_do(tmp_path):
    root = _tree(tmp_path)
    code, out, err = _run_cli(["格式化", str(root)])
    assert code == 2
    assert "--check" in err and "--write" in err


def test_dir_reports_unformattable_files_separately(tmp_path):
    """处理不了的文件要**单独报**，不能混进「不合风格」里（两者修法完全不同）。"""
    root = tmp_path / "项目"
    root.mkdir()
    (root / "坏的.jsh").write_text('令 甲 = "没结束\n', encoding="utf-8")
    (root / "乱的.jsh").write_text("令 甲=1\n", encoding="utf-8")
    code, out, err = _run_cli(["格式化", str(root), "--write"])
    assert code == 1                       # 有没处理掉的文件
    assert "跳过" in err
    assert "改写了 1 个" in out
    assert (root / "乱的.jsh").read_text(encoding="utf-8") == "令 甲 = 1\n"


def test_formatter_only_needs_the_lexer(tmp_path):
    """**格式化器只依赖词法，不做完整语法分析**（有意为之）。

    `如果 真`（漏了冒号）这种语法错文件照样能排版——用户可能正写到一半，
    格式化器不该因为「还没写完」就拒绝服务。所以「处理不了」只对应词法层
    的错误（未闭合字符串、非法字符等）。
    """
    src = "如果 真\n    令 甲=1\n"
    assert format_source(src) == "如果 真\n    令 甲 = 1\n"


def test_dir_without_jsh_files(tmp_path):
    root = tmp_path / "空"
    root.mkdir()
    code, out, err = _run_cli(["格式化", str(root), "--check"])
    assert code == 2
    assert "没有 .jsh 文件" in err


def test_running_a_directory_says_it_is_a_directory(tmp_path):
    """`jishi <目录>` 要给中文提示而不是英文 traceback。"""
    root = _tree(tmp_path)
    code, out, err = _run_cli([str(root)])
    assert code == 2
    assert "是个目录" in err
    assert "PermissionError" not in err and "Traceback" not in err


def test_corpus_dir_is_already_formatted():
    """仓库自己的 examples/ 与 tests/cases/ 必须**本来就合规**。

    这条是「格式化器的风格 = 项目自己的风格」的验收：要是它俩打起来，
    说明规则定得与代码库里实际写法不一致。
    """
    for target in ("examples", "tests/cases"):
        code, out, err = _run_cli(["格式化", str(ROOT / target), "--check"])
        assert code == 0, f"{target} 有文件不合格式化风格：\n{err}"
