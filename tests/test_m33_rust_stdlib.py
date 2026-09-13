# -*- coding: utf-8 -*-
"""M33：Rust 宿主标准库与 Python 侧逐字节对拍。

这一套的来历：Rust 宿主从 M13.3 起标准库只有 `数学`/`文本`/`随机` 三个模块，
而且函数不全。M33 把它补齐到与 Python 侧同宽（14 个模块、约 130 个函数），
零第三方依赖——`正则` / `加密` / `压缩` / `网络` 全是自己写的。

对拍时有两处**必须注意**，不是随便写写就行的：

1. **Rust 侧的输出要按字节收**（`capture_output=True` 后自己 `.decode()`），
   **不能**用 `text=True`：它默认做通用换行转换，会把输出里的 `\\r\\n` 悄悄
   改成 `\\n`。M33 就是靠这一点才发现「Python 侧输出 CRLF、Node/Rust 输出 LF」
   （见 `jishi/cli.py` 的 `_setup_io`）。
2. **两个宿主共用一个文件系统**，所以写文件的用例必须**可重入**：
   同一个临时目录下，Python 先跑、Rust 后跑，用例自己不能依赖「上一轮留下的
   状态」（M31 在这里栽过一次）。
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import time
import threading
import zipfile
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import rust_exe_path       # noqa: E402

from jishi import serialize                       # noqa: E402
from jishi import vm as vm_mod                    # noqa: E402
from jishi.compiler import compile_source         # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXE = rust_exe_path()   # 平台感知：Windows 是 .exe，别处没有扩展名
CARGO = shutil.which("cargo")


def _build_env() -> dict:
    """构建环境：`windows-gnu` 目标用 gcc 当链接器（不写死本机路径）。"""
    env = dict(os.environ)
    mingw = env.get("JISHI_MINGW")
    if mingw and Path(mingw).is_dir():
        env["PATH"] = mingw + os.pathsep + env.get("PATH", "")
    return env


@pytest.fixture(scope="session")
def rust_exe() -> Path:
    if not CARGO:
        pytest.skip("本机无 cargo，跳过 Rust 宿主对拍")
    if not EXE.exists():
        r = subprocess.run(
            [CARGO, "build", "--manifest-path", str(ROOT / "rust/Cargo.toml"),
             "--release"],
            capture_output=True, text=True, encoding="utf-8",
            cwd=ROOT, env=_build_env())
        if r.returncode != 0:
            pytest.skip(f"cargo build 失败，跳过 Rust 对拍：{r.stderr[-300:]}")
    return EXE


# ---------------------------------------------------------------------------
# 对拍脚手架
# ---------------------------------------------------------------------------

def _py_run(src: str, feed: str = "") -> tuple[int, str, str]:
    buf = io.StringIO()
    old = sys.stdin
    sys.stdin = io.StringIO(feed)
    try:
        with redirect_stdout(buf):
            vm_mod.VM(compile_source(src, "<t>"), "<t>").run()
        return 0, buf.getvalue(), ""
    except Exception as e:                        # noqa: BLE001
        return 1, buf.getvalue(), f"{type(e).__name__}: {e}"
    finally:
        sys.stdin = old


def _rs_run(exe: Path, src: str, feed: str = "",
            args: list[str] | None = None) -> tuple[int, str, str]:
    tmp = ROOT / "build" / f"_m33_bc_{os.getpid()}.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(serialize.dumps(compile_source(src, "<t>")), encoding="utf-8")
    try:
        r = subprocess.run([str(exe), str(tmp), *(args or [])],
                           capture_output=True, input=feed.encode("utf-8"))
        # 按字节收再自己解码——`text=True` 会做换行转换，见文件头说明
        return (r.returncode,
                r.stdout.decode("utf-8", errors="replace"),
                r.stderr.decode("utf-8", errors="replace"))
    finally:
        tmp.unlink(missing_ok=True)


def _agree(src: str, exe: Path, feed: str = "") -> str:
    """两边都必须成功，且输出逐字节相同。"""
    prc, pout, perr = _py_run(src, feed)
    assert prc == 0, f"Python 侧就失败了：{perr[:300]}"
    rrc, rout, rerr = _rs_run(exe, src, feed)
    assert rrc == 0, f"Rust 宿主非零退出：rc={rrc}\nstderr={rerr[:300]}"
    assert rout == pout, f"输出不一致\nPython: {pout!r}\nRust:   {rout!r}"
    return pout


def _both_fail(src: str, exe: Path) -> None:
    """两边都该失败（错误文案不要求一样，但都不许静默给值）。"""
    prc, pout, _perr = _py_run(src)
    rrc, rout, _rerr = _rs_run(exe, src)
    assert prc != 0, f"Python 侧居然成功了：{pout!r}"
    assert rrc != 0, f"Rust 宿主居然成功了：{rout!r}"


def _dir(tmp_path: Path) -> str:
    """临时目录的「基石友好」写法（正斜杠，Windows 也认）。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    return str(tmp_path).replace("\\", "/")


# ---------------------------------------------------------------------------
# 数学
# ---------------------------------------------------------------------------

def test_math_basic(rust_exe):
    assert _agree(
        '导入 数学\n'
        '打印(数学.开方(16), 数学.开方(2))\n'
        '打印(数学.平方(3), 数学.平方(1.5), 数学.平方(-4))\n'
        '打印(数学.幂(2, 10), 数学.幂(2, 0.5), 数学.幂(3, 3))\n'
        '打印(数学.绝对值(-3), 数学.绝对值(-2.5), 数学.绝对值(4))\n',

        rust_exe) == "4.0 1.4142135623730951\n9 2.25 16\n1024 1.4142135623730951 27\n3 2.5 4\n"


def test_math_rounding(rust_exe):
    """`四舍五入` 与 Python 的 `round` 一样是**银行家舍入**（半值取偶）。"""
    assert _agree(
        '导入 数学\n'
        '打印(数学.四舍五入(2.5), 数学.四舍五入(3.5), 数学.四舍五入(2.675, 2))\n'
        '打印(数学.四舍五入(3.14159, 2), 数学.四舍五入(1234.5, -2))\n'
        '打印(数学.四舍五入(1250, -2), 数学.向上取整(2.1), 数学.向下取整(-2.1))\n',

        rust_exe) == "2 4 2.67\n3.14 1200\n1200 3 -3\n"


def test_math_transcendental(rust_exe):
    assert _agree(
        '导入 数学\n'
        '打印(数学.圆周率, 数学.自然常数)\n'
        '打印(数学.正弦(0), 数学.余弦(0), 数学.正切(0))\n'
        '打印(数学.角度转弧度(180))\n'
        '打印(数学.对数(100, 10), 数学.常用对数(1000))\n',

        rust_exe) == ("3.141592653589793 2.718281828459045\n0.0 1.0 0.0\n"
                      "3.141592653589793\n2.0 3.0\n")


def test_math_integer_helpers(rust_exe):
    assert _agree(
        '导入 数学\n'
        '打印(数学.最大公约数(12, 18), 数学.最小公倍数(4, 6))\n'
        '打印(数学.最大公约数(0, 5), 数学.最小公倍数(0, 5))\n'
        '打印(数学.阶乘(0), 数学.阶乘(5), 数学.阶乘(20))\n',

        rust_exe) == "6 12\n5 0\n1 120 2432902008176640000\n"


def test_math_errors(rust_exe):
    _both_fail('导入 数学\n打印(数学.开方(-1))\n', rust_exe)
    _both_fail('导入 数学\n打印(数学.阶乘(-1))\n', rust_exe)


# ---------------------------------------------------------------------------
# 文本
# ---------------------------------------------------------------------------

def test_text_padding_and_align(rust_exe):
    """`居中` 的偏置是 CPython 的 `marg // 2 + (marg & width & 1)`，不是「左右各一半」。"""
    assert _agree(
        '导入 文本\n'
        '打印("[" + 文本.居中("ab", 5) + "]", "[" + 文本.居中("abc", 6) + "]", '
        '"[" + 文本.居中("a", 4, "*") + "]")\n'
        '打印("[" + 文本.左对齐("ab", 5, "*") + "]", "[" + 文本.右对齐("ab", 5, "*") + "]")\n'
        '打印(文本.补零(7, 3), 文本.补零(-7, 4), 文本.补零("ab", 5), 文本.补零(12345, 3))\n',

        rust_exe) == "[  ab ] [ abc  ] [*a**]\n[ab***] [***ab]\n007 -007 000ab 12345\n"


def test_text_misc(rust_exe):
    assert _agree(
        '导入 文本\n'
        '打印(文本.重复("ab", 3), "[" + 文本.重复("x", 0) + "]")\n'
        '打印(文本.计数("abab", "a"), 文本.计数("aaa", "aa"), 文本.计数("ab", ""))\n'
        '打印(文本.首字母大写("aBC"), 文本.去前缀("abc", "a"), 文本.去后缀("abc", "z"))\n'
        '打印(文本.切成三段("a=b", "="), 文本.切成三段("ab", "="))\n'
        '打印(文本.按行拆分("a\\nb\\nc"), 文本.按行拆分(""))\n',

        rust_exe) == ("ababab []\n2 1 3\nAbc bc abc\n['a', '=', 'b'] ['ab', '', '']\n"
                      "['a', 'b', 'c'] []\n")


def test_text_predicates_unicode(rust_exe):
    """`是数字` 只认十进制数字字符——`一` 不算（Python 的 `str.isdigit`）。"""
    assert _agree(
        '导入 文本\n'
        '打印(文本.是数字("12"), 文本.是数字("1a"), 文本.是数字("一二三"))\n'
        '打印(文本.是字母("abc"), 文本.是字母("中文"), 文本.是空白("  "), 文本.是空白(" a"))\n'
        '打印(文本.是大写("AB"), 文本.是大写("A1"), 文本.是大写("Ab"), 文本.是小写("ab"))\n',

        rust_exe) == "真 假 假\n真 真 真 假\n真 真 假 真\n"


def test_text_format(rust_exe):
    """`格式化` 要支持格式说明符，且布尔/空走中文显示。"""
    assert _agree(
        '导入 文本\n'
        '打印(文本.格式化("{}今年{}岁", "小明", 12))\n'
        '打印(文本.格式化("平均 {:.1f} 分", 83.75), "[" + 文本.格式化("{:>5}", "甲") + "]")\n'
        '打印("[" + 文本.格式化("{:08.2f}", 3.14159) + "]", 文本.格式化("{1}-{0}", "甲", "乙"))\n'
        '打印(文本.格式化("{} {}", 真, 空))\n',

        rust_exe) == ("小明今年12岁\n平均 83.8 分 [    甲]\n[00003.14] 乙-甲\n真 空\n")


def test_text_concat_uses_chinese_display(rust_exe):
    """`拼接` 的「值→文本」与 `文本()` 同一套（`真` 而不是 `True`）。"""
    assert _agree('导入 文本\n打印(文本.拼接([1, "甲", 真, 空], "-"))\n',
                  rust_exe) == "1-甲-真-空\n"


# ---------------------------------------------------------------------------
# 随机（结果随机，只能验性质）
# ---------------------------------------------------------------------------

def test_random_properties(rust_exe):
    assert _agree(
        '导入 随机\n'
        '令 v = 随机.随机整数(1, 6)\n打印(v >= 1 与 v <= 6)\n'
        '令 f = 随机.随机小数()\n打印(f >= 0.0 与 f < 1.0)\n'
        '令 a = [3, 1, 2]\n令 s = 随机.随机选择(a)\n打印(s 在 a)\n'
        '令 b = 随机.洗牌(a)\n打印(长度(b), a == [3, 1, 2])\n',

        rust_exe) == "真\n真\n真\n3 真\n"


# ---------------------------------------------------------------------------
# 路径 / 文件
# ---------------------------------------------------------------------------

def test_path_parts(rust_exe):
    """`连接` / `文件名` / `后缀` / `父目录` 与 pathlib 一致。"""
    assert _agree(
        '导入 路径\n'
        '打印(路径.连接("a", "b", "c.txt"))\n'
        '打印(路径.文件名("a/b/c.txt"), 路径.父目录("a/b/c.txt"), 路径.父目录("a"))\n'
        '打印(路径.后缀("a.tar.gz"), 路径.无后缀名("a.tar.gz"))\n'
        '打印("[" + 路径.后缀("a.") + "]", "[" + 路径.无后缀名(".bashrc") + "]", '
        '"[" + 路径.后缀(".bashrc") + "]")\n',

        rust_exe)


def test_path_fs_ops(rust_exe, tmp_path):
    """目录创建/列出/删除。用例可重入：先删掉上一次可能的残留。"""
    d = _dir(tmp_path / "p1")
    assert _agree(
        f'导入 路径\n'
        f'打印(路径.创建目录("{d}/sub", 真))\n'
        f'打印(路径.存在("{d}/sub"), 路径.是目录("{d}/sub"))\n'
        f'打印(路径.列出("{d}"))\n'
        f'打印(路径.删除("{d}/sub"), 路径.删除("{d}/sub"))\n',

        rust_exe)   # 不硬编码期望值：临时目录的绝对路径与分隔符随平台变


def test_path_errors(rust_exe, tmp_path):
    d = _dir(tmp_path)
    _both_fail(f'导入 路径\n打印(路径.列出("{d}/没有这个"))\n', rust_exe)


def test_file_read_write_newline_faithful(rust_exe, tmp_path):
    """写 `\\n` 就是 `\\n`（换行保真），`写行`/`按行读` 成对。"""
    d = _dir(tmp_path / "f1")
    assert _agree(
        f'导入 文件\n'
        f'文件.写文本("{d}/a.txt", "你好\\n世界")\n'
        f'打印(文件.读文本("{d}/a.txt"))\n'
        f'文件.写行("{d}/b.txt", ["甲", 3, 真])\n'
        f'打印(文件.文件大小("{d}/b.txt"))\n'
        f'打印(文件.按行读("{d}/b.txt"))\n',

        rust_exe) == "你好\n世界\n10\n['甲', '3', '真']\n"


def test_file_copy_and_tools(rust_exe, tmp_path):
    d = _dir(tmp_path / "f2")
    assert _agree(
        f'导入 文件\n'
        f'文件.写文本("{d}/c.txt", "x")\n'
        f'文件.追加文本("{d}/c.txt", "y")\n'
        f'文件.复制("{d}/c.txt", "{d}/d.txt")\n'
        f'打印(文件.读文本("{d}/d.txt"))\n'
        f'打印(文件.文件存在("{d}/d.txt"), 文件.是文件("{d}/d.txt"), 文件.是目录("{d}"))\n'
        f'打印(文件.列出目录("{d}"))\n'
        f'打印(文件.路径拼接("a", "b", "c.txt"))\n'
        f'打印(文件.文件名("a/b/c.txt"), 文件.扩展名("a/b/c.txt"))\n',

        rust_exe) == ("xy\n真 真 真\n['c.txt', 'd.txt']\na/b/c.txt\nc.txt .txt\n")


def test_file_errors(rust_exe, tmp_path):
    d = _dir(tmp_path)
    _both_fail(f'导入 文件\n打印(文件.读文本("{d}/没有这个.txt"))\n', rust_exe)


# ---------------------------------------------------------------------------
# 系统
# ---------------------------------------------------------------------------

def test_system_basic(rust_exe):
    """`系统名` 与 `主机名` 跨平台都要有值；`机器架构` 不比对
    （Python 在 macOS 给 `arm64`，Rust 的 `ARCH` 是 `aarch64`，属于平台命名差异）。"""
    assert _agree(
        '导入 系统\n'
        '打印(系统.系统名() == 系统.系统名())\n'
        '打印(长度(系统.主机名()) >= 0, 长度(系统.当前目录()) > 0)\n',

        rust_exe) == "真\n真 真\n"


def test_system_env_and_params(rust_exe):
    assert _agree(
        '导入 系统\n'
        '打印(系统.环境变量("一定不存在_变量名", "默认值"))\n'
        '系统.设环境变量("JISHI_M33_TEST", "值")\n'
        '打印(系统.环境变量("JISHI_M33_TEST"))\n'
        '打印(系统.参数())\n',

        rust_exe) == "默认值\n值\n[]\n"


def test_system_run_and_exit_code(rust_exe):
    """`执行` / `退出码` 走子进程（用 `cmd`/`sh` 各自可用的命令）。"""
    if os.name == "nt":
        cmd = '["cmd", "/C", "exit 3"]'
        cmd2 = '["cmd", "/C", "echo 你好"]'
    else:
        cmd = '["sh", "-c", "exit 3"]'
        cmd2 = '["sh", "-c", "echo 你好"]'
    assert _agree(
        f'导入 系统\n'
        f'打印(系统.退出码({cmd}))\n'
        f'打印(系统.执行({cmd2}))\n',

        rust_exe)   # 只比一致性：命令输出换个平台就未必一样


# ---------------------------------------------------------------------------
# 日期 / 时间
# ---------------------------------------------------------------------------

def test_date_parse_and_type(rust_exe):
    """`日期.解析` 给的是 datetime（显示与 `类型()` 都要对）。"""
    assert _agree(
        '导入 日期\n'
        '打印(日期.解析("2026-09-01"), 类型(日期.解析("2026-09-01")))\n'
        '打印(日期.解析("2026-09-01") == 日期.解析("2026/09/01"))\n',

        rust_exe) == "2026-09-01 00:00:00 datetime\n真\n"


def test_date_strftime(rust_exe):
    """`strftime` 子集：`%U`/`%W` 照 CPython 的公式，`%c` 的日期是空格填充。"""
    assert _agree(
        '导入 日期\n'
        '打印(日期.格式化("2026-09-05", "%Y年%m月%d日"))\n'
        '打印(日期.格式化("2026-09-05", "%a %A %b %B %j %u %w %p %U %W %x %X"))\n'
        '打印(日期.格式化("2026-09-05", "%c"))\n',

        rust_exe) == ("2026年09月05日\n"
                      "Sat Saturday Sep September 248 6 6 AM 35 35 09/05/26 00:00:00\n"
                      "Sat Sep  5 00:00:00 2026\n")


def test_date_arithmetic(rust_exe):
    assert _agree(
        '导入 日期\n'
        '打印(日期.加天数(10, "2026-09-01"), 日期.减天数(10, "2026-09-01"))\n'
        '打印(日期.加天数(100, "2026-12-31"), 日期.加天数(1, "2024-02-28"))\n'
        '打印(日期.相差天数("2026-09-11", "2026-09-01"), 日期.相差天数("2026-09-01", "2026-09-11"))\n'
        '打印(日期.星期名("2026-09-13"), 日期.早于("2026-01-01", "2026-06-01"), '
        '日期.晚于("2026-01-01", "2026-06-01"))\n',

        rust_exe) == ("2026-09-11 2026-08-22\n2027-04-10 2024-02-29\n10 -10\n日 真 假\n")


def test_date_errors(rust_exe):
    _both_fail('导入 日期\n打印(日期.解析("2026-02-30"))\n', rust_exe)
    _both_fail('导入 日期\n打印(日期.解析("2026年9月"))\n', rust_exe)


def test_time_functions(rust_exe):
    """不比对「现在」这类非确定值——比**格式化**与**差值**。

    两个格式化结果按**本机时区**算期望（开发机 UTC+8、CI runner 是 UTC，
    写死会让 CI 必挂——M35 实测）；其余部分是时区无关的。
    """
    want = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(0)) + " "
            + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(1700000000)) + "\n"
            "日 2026-01-31\n真 真\n")
    assert _agree(
        '导入 时间\n'
        '打印(时间.格式化时间戳(0), 时间.格式化时间戳(1700000000))\n'
        '打印(时间.星期名("2026-09-13"), 时间.加天数(30, "2026-01-01"))\n'
        '令 a = 时间.高精度时间()\n时间.睡眠(0.02)\n令 b = 时间.高精度时间()\n'
        '打印(b > a, b - a > 0.015)\n',

        rust_exe) == want


# ---------------------------------------------------------------------------
# 加密（拿标准向量当裁判）
# ---------------------------------------------------------------------------

def test_crypto_known_vectors(rust_exe):
    """`abc` / 空串是各算法的公开测试向量，逐字节比才有意义。"""
    assert _agree(
        '导入 加密\n'
        '打印(加密.md5("abc"), 加密.sha1("abc"))\n'
        '打印(加密.sha256("abc"))\n'
        '打印(加密.sha512("abc"))\n'
        '打印(加密.sha256(""), 加密.md5(""))\n',

        rust_exe) == (
        "900150983cd24fb0d6963f7d28e17f72 a9993e364706816aba3e25717850c26c9cd0d89d\n"
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad\n"
        "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a"
        "2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f\n"
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 "
        "d41d8cd98f00b204e9800998ecf8427e\n")


def test_crypto_text_encoding(rust_exe):
    """文本先过「值→文本」（中文显示）再按 UTF-8 编码——`摘要(真)` 用的是 `真` 的字节。"""
    assert _agree(
        '导入 加密\n'
        '打印(加密.摘要("基石", "SHA-256"), 加密.md5("基石"))\n'
        '打印(加密.摘要(123), 加密.摘要(真), 加密.摘要(1.5))\n'
        '打印(加密.支持的算法())\n',

        rust_exe) == ("06c9cd84741f7b2a4cd064b256e5b655369a94610ee155839d5791704ddb19ea "
                      "17ea8b988acdaffd2d553092683b43aa\n"
                      "a665a45920422f9d417e4867efdc4fb8a04a1f3fff1fa07e998e86f7f7a27ae3 "
                      "827d018e1bf8a869745db69188429182e225481f53c6269b13c3f6a4c8338da7 "
                      "9f29a130438b81170b92a42650f9a94291ecad60bd47af2a3886e75f7f728725\n"
                      "['md5', 'sha1', 'sha256', 'sha512']\n")


def test_crypto_file_digest(rust_exe, tmp_path):
    d = _dir(tmp_path)
    assert _agree(
        f'导入 加密\n导入 文件\n'
        f'文件.写文本("{d}/h.txt", "abc")\n'
        f'打印(加密.文件摘要("{d}/h.txt", "md5"))\n'
        f'打印(加密.文件摘要("{d}/h.txt"))\n',

        rust_exe) == ("900150983cd24fb0d6963f7d28e17f72\n"
                      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad\n")


def test_crypto_bad_algo(rust_exe):
    _both_fail('导入 加密\n打印(加密.摘要("x", "sha3"))\n', rust_exe)


# ---------------------------------------------------------------------------
# json
# ---------------------------------------------------------------------------

def test_json_dumps_shapes(rust_exe):
    """无缩进的分隔符是 `", "` / `": "`；有缩进时每个元素前都换行。"""
    assert _agree(
        '导入 json\n'
        '打印(json.转文本({"甲": 1, "乙": [1, 2], "丙": 真, "丁": 空}))\n'
        '打印(json.转文本({"甲": [1, {"乙": 2}]}, 2))\n'
        '打印(json.转文本([]), json.转文本({}), json.转文本([], 2), json.转文本({}, 2))\n',

        rust_exe) == ('{"甲": 1, "乙": [1, 2], "丙": true, "丁": null}\n'
                      '{\n  "甲": [\n    1,\n    {\n      "乙": 2\n    }\n  ]\n}\n'
                      '[] {} [] {}\n')


def test_json_number_shapes(rust_exe):
    """浮点用 Python 的 `repr` 规则：`2.0` 留 `.0`、`1e+20` 用科学计数法。"""
    assert _agree(
        '导入 json\n'
        '打印(json.转文本([1, 2.0, 1.5, 0.1]))\n'
        '打印(json.转文本([100000000000000000000.0, 1e-7]))\n',

        rust_exe) == "[1, 2.0, 1.5, 0.1]\n[1e+20, 1e-07]\n"


def test_json_escapes(rust_exe):
    assert _agree('导入 json\n打印(json.转文本({"甲": "a\\"b\\\\nc\\td"}))\n',
                  rust_exe) == '{"甲": "a\\"b\\\\nc\\td"}\n'


def test_json_loads(rust_exe):
    """数字按有无小数点/指数分整数与小数；重复键保留首次位置 + 最后的值。"""
    assert _agree(
        '导入 json\n'
        '打印(json.解析("[1, 2.5, \\"甲\\", true, null]"))\n'
        '打印(json.解析("{\\"a\\": [1, {\\"b\\": null}], \\"c\\": 1e3}"))\n'
        '打印(json.解析("{\\"a\\": 1, \\"b\\": 2, \\"a\\": 3}"))\n'
        '打印(json.解析("\\"a\\\\u0041\\\\n\\""))\n',

        rust_exe)   # 只比一致性：输出形状太长，硬编码容易看错


def test_json_roundtrip_file(rust_exe, tmp_path):
    d = _dir(tmp_path)
    assert _agree(
        f'导入 json\n'
        f'json.写文件("{d}/j.json", {{"键": [1, 2]}}, 2)\n'
        f'打印(json.读文件("{d}/j.json"))\n',

        rust_exe) == "{'键': [1, 2]}\n"


def test_json_bad_text(rust_exe):
    _both_fail('导入 json\n打印(json.解析("{不是 json"))\n', rust_exe)


# ---------------------------------------------------------------------------
# 表格（CSV）
# ---------------------------------------------------------------------------

def test_table_roundtrip(rust_exe, tmp_path):
    """`写表格` 带 BOM + CRLF（CSV 规范），`读表格` 要能读回来。"""
    d = _dir(tmp_path / "t1")
    assert _agree(
        f'导入 表格\n'
        f'表格.写表格("{d}/t.csv", [["姓名", "分数"], ["小红", "92"], ["小明", "88"]])\n'
        f'令 t = 表格.读表格("{d}/t.csv")\n'
        f'打印(t, 类型(t))\n'
        f'打印(表格.表头(t), 表格.数据行(t))\n'
        f'打印(表格.挑选(t, "姓名"))\n'
        f'打印(表格.挑选(t, ["姓名", "分数"]))\n'
        f'打印(表格.筛选(t, "姓名", "小明"))\n'
        f'打印(表格.汇总(t, "分数"))\n'
        f'打印(表格.排序按(t, "分数", 真))\n',

        rust_exe) == (
        "<表格 2 行 x 2 列> 表格\n"
        "['姓名', '分数'] [['小红', '92'], ['小明', '88']]\n"
        "['小红', '小明']\n"
        "[['小红', '92'], ['小明', '88']]\n"
        "<表格 1 行 x 2 列>\n"
        "{'个数': 2, '总和': 180.0, '平均': 90.0, '最大': 92.0, '最小': 88.0}\n"
        "<表格 2 行 x 2 列>\n")


def test_table_quotes_and_dicts(rust_exe, tmp_path):
    """字段里有逗号/引号要按 CSV 规则加引号；字典表格的键作表头。"""
    d = _dir(tmp_path / "t2")
    assert _agree(
        f'导入 表格\n'
        f'表格.写表格("{d}/t.csv", [["a", "b"], ["带,逗号", "带\\"引号"]])\n'
        f'打印(表格.数据行(表格.读表格("{d}/t.csv")))\n'
        f'表格.写字典表格("{d}/d.csv", [{{"名": "甲", "值": 1}}, {{"名": "乙", "值": 2}}])\n'
        f'打印(表格.读字典表格("{d}/d.csv"))\n'
        f'表格.追加("{d}/d.csv", ["丙", "3"])\n'
        f'打印(表格.数据行(表格.读表格("{d}/d.csv")))\n'
        f'打印(表格.转置([[1, 2], [3, 4]]), 表格.转置([]))\n',

        rust_exe) == ("[['带,逗号', '带\"引号']]\n"
                      "[{'名': '甲', '值': '1'}, {'名': '乙', '值': '2'}]\n"
                      "[['甲', '1'], ['乙', '2'], ['丙', '3']]\n"
                      "[[1, 3], [2, 4]] []\n")


def test_table_errors(rust_exe, tmp_path):
    d = _dir(tmp_path / "t3")
    _both_fail(
        f'导入 表格\n'
        f'表格.写表格("{d}/t.csv", [["姓名"], ["甲"]])\n'
        f'打印(表格.挑选(表格.读表格("{d}/t.csv"), "没有这列"))\n',
        rust_exe)


# ---------------------------------------------------------------------------
# 压缩（与 Python 的 zipfile 互相可读）
# ---------------------------------------------------------------------------

def test_zip_roundtrip(rust_exe, tmp_path):
    d = Path(_dir(tmp_path / "z1"))
    (d / "一.txt").write_bytes("你好，世界\n".encode("utf-8"))
    (d / "二.txt").write_bytes("第二行\n".encode("utf-8"))
    # 按**文件**打包（不是目录）：arcname 就是文件名本身，好写断言。
    # 源文件用 write_bytes 写：`write_text` 在 Windows 上会把 `\n` 变成 `\r\n`。
    dd = _dir(d)
    assert _agree(
        f'导入 压缩\n导入 文件\n'
        f'压缩.打包("{dd}/a.zip", ["{dd}/一.txt", "{dd}/二.txt"])\n'
        f'打印(压缩.列出内容("{dd}/a.zip"))\n'
        f'压缩.解压("{dd}/a.zip", "{dd}/out")\n'
        f'打印(文件.读文本("{dd}/out/一.txt"))\n',

        # 文件内容本身就以 `\n` 结尾，`打印` 再加一个
        rust_exe) == "['一.txt', '二.txt']\n你好，世界\n\n"


def test_zip_reads_python_deflate(rust_exe, tmp_path):
    """**Python 打的包（deflate）Rust 必须解得开**——这条才验得了自写的 inflate。"""
    d = Path(_dir(tmp_path / "z2"))
    d.mkdir(parents=True, exist_ok=True)
    # 按字节写：`write_text` 在 Windows 上会把 `\n` 变成 `\r\n`
    (d / "x.txt").write_bytes("中文内容测试\n".encode("utf-8"))
    (d / "big.txt").write_bytes(("重复" * 5000).encode("utf-8"))   # 会被 deflate 压
    with zipfile.ZipFile(d / "py.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(d / "x.txt", "x.txt")
        zf.write(d / "big.txt", "big.txt")
    out = _agree(
        f'导入 压缩\n导入 文件\n'
        f'压缩.解压("{_dir(d)}/py.zip", "{_dir(d)}/out")\n'
        f'打印(文件.读文本("{_dir(d)}/out/x.txt"))\n'
        f'打印(文件.文件大小("{_dir(d)}/out/big.txt"))\n'
        f'打印(压缩.列出内容("{_dir(d)}/py.zip"))\n',
        rust_exe)
    # 「重复」×5000 是 10000 个**字符**，按 UTF-8 每个汉字 3 字节 = 30000
    # 文件内容以 `\n` 结尾，`打印` 再加一个
    assert out == "中文内容测试\n\n30000\n['x.txt', 'big.txt']\n"


def test_zip_readable_by_python(rust_exe, tmp_path):
    """反过来：Rust 打的包（stored）Python 的 `zipfile` 要能读，且 CRC 校验通过。"""
    d = Path(_dir(tmp_path / "z3"))
    d.mkdir(parents=True, exist_ok=True)
    (d / "y.txt").write_bytes("基石\n".encode("utf-8"))
    # 打包**文件**而不是目录：条目名就是 `y.txt`（打目录会带目录名作前缀）
    rrc, _rout, rerr = _rs_run(
        rust_exe, f'导入 压缩\n压缩.打包("{_dir(d)}/rs.zip", "{_dir(d)}/y.txt")\n')
    assert rrc == 0, rerr[:300]
    with zipfile.ZipFile(d / "rs.zip") as zf:
        assert zf.namelist() == ["y.txt"]
        assert zf.testzip() is None                # CRC 校验（内容没坏）
        assert zf.read("y.txt").decode("utf-8") == "基石\n"


def test_zip_missing_source(rust_exe, tmp_path):
    d = _dir(tmp_path)
    _both_fail(f'导入 压缩\n压缩.打包("{d}/c.zip", "{d}/没有.txt")\n', rust_exe)


# ---------------------------------------------------------------------------
# 正则
# ---------------------------------------------------------------------------

def test_regex_basic_ops(rust_exe):
    assert _agree(
        '导入 正则\n'
        '打印(正则.匹配("\\\\d+", "123abc"), 正则.匹配("\\\\d+", "abc123"), 正则.匹配("^abc$", "abc"))\n'
        '打印(正则.搜索("\\\\d+", "abc123def456"), "[" + 正则.搜索("\\\\d+", "没有") + "]")\n'
        '打印(正则.搜索("<.+>", "<a><b>"), 正则.搜索("<.+?>", "<a><b>"))\n',

        rust_exe) == "真 假 真\n123 []\n<a><b> <a>\n"


def test_regex_findall_shapes(rust_exe):
    """`查找全部` 的形状照 Python 的 `findall`：0 组给整段、1 组给分组、多组给列表。"""
    assert _agree(
        '导入 正则\n'
        '打印(正则.查找全部("\\\\d+", "a1b22c333"))\n'
        '打印(正则.查找全部("(\\\\d)\\\\d", "a12b34"))\n'
        '打印(正则.查找全部("(\\\\w)(\\\\d)", "a1b2"))\n'
        '打印(正则.查找全部("a*", "aba"))\n',

        rust_exe) == ("['1', '22', '333']\n['1', '3']\n[['a', '1'], ['b', '2']]\n"
                      "['a', '', 'a', '']\n")


def test_regex_chinese_and_classes(rust_exe):
    assert _agree(
        '导入 正则\n'
        '打印(正则.查找全部("[一-鿿]+", "你好 world 世界"))\n'
        '打印(正则.查找全部("[^0-9]+", "ab12cd34"))\n'
        '打印(正则.匹配("[a-c]x", "bx"), 正则.匹配("[-a]", "-"))\n'
        '打印(正则.匹配("a{2,3}", "aaa"), 正则.匹配("a{2,3}$", "aaaa"))\n'
        '打印(正则.查找全部("\\\\bcat\\\\b", "cat cats concat"))\n',

        rust_exe) == ("['你好', '世界']\n['ab', 'cd']\n真 真\n真 假\n['cat']\n")


def test_regex_groups_lookaround_backref(rust_exe):
    assert _agree(
        '导入 正则\n'
        '打印(正则.分组("(\\\\w+)-(\\\\d+)", "abc-123"), 正则.分组("(x)", "y"))\n'
        '打印(正则.查找全部("(?:ab)+", "ababab"))\n'
        '打印(正则.匹配("a(?=b)", "ab"), 正则.匹配("a(?!b)", "ac"))\n'
        '打印(正则.匹配("(\\\\w)\\\\1", "aa"), 正则.匹配("(\\\\w)\\\\1", "ab"))\n',

        rust_exe) == "['abc', '123'] []\n['ababab']\n真 真\n真 假\n"


def test_regex_replace_and_split(rust_exe):
    assert _agree(
        '导入 正则\n'
        '打印(正则.替换("\\\\d", "#", "a1b2"), 正则.替换("(\\\\w+)-(\\\\d+)", "\\\\2-\\\\1", "abc-123"))\n'
        '打印(正则.拆分("[,;]", "a,b;c"), 正则.拆分("\\\\s+", "a  b   c"))\n'
        '打印(正则.拆分("(,)", "a,b"))\n',

        rust_exe) == "a#b# 123-abc\n['a', 'b', 'c'] ['a', 'b', 'c']\n['a', ',', 'b']\n"


def test_regex_crawler_pattern(rust_exe):
    """真实项目「网页采集」用的那条模式（含非贪婪与 HTML 标签清理）。"""
    assert _agree(
        '导入 正则\n'
        '令 html = "<a href=\\"http://a.com\\">标题一</a><a href=\\"http://b.com\\">标题二</a>"\n'
        '令 组 = 正则.查找全部("<a[^>]+href=\\"([^\\"]+)\\"[^>]*>(.*?)</a>", html)\n'
        '打印(长度(组), 组[0], 组[1])\n'
        '打印(正则.替换("<[^>]+>", "", 组[0][1]), 正则.替换("^https?://", "", 组[0][0]))\n',

        rust_exe) == ("2 ['http://a.com', '标题一'] ['http://b.com', '标题二']\n"
                      "标题一 a.com\n")


def test_regex_errors(rust_exe):
    _both_fail('导入 正则\n打印(正则.匹配("(abc", "abc"))\n', rust_exe)
    _both_fail('导入 正则\n打印(正则.匹配("a**", "aa"))\n', rust_exe)
    _both_fail('导入 正则\n打印(正则.匹配("\\\\3", "x"))\n', rust_exe)


# ---------------------------------------------------------------------------
# 网络（本地 http.server，不依赖外网）
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):                    # 静音
        pass

    def do_GET(self):
        if self.path == "/json":
            body = '{"名称": "基石", "数量": 3, "ok": true}'.encode("utf-8")
        elif self.path == "/text":
            body = "你好，世界\n第二行\n".encode("utf-8")
        elif self.path == "/bad":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return
        else:
            body = b"hello"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(n)
        out = "收到:".encode("utf-8") + data
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture(scope="module")
def http_base():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_network_get_and_json(rust_exe, http_base):
    assert _agree(
        f'导入 网络\n'
        f'令 s = 网络.获取("{http_base}/text")\n'
        f'打印(长度(s) > 0, s.结尾是("第二行\\n"))\n'
        f'打印(网络.获取("{http_base}/"))\n'
        f'令 d = 网络.获取JSON("{http_base}/json")\n'
        f'打印(d["名称"], d["数量"], d["ok"])\n',

        rust_exe) == "真 真\nhello\n基石 3 真\n"


def test_network_post(rust_exe, http_base):
    assert _agree(
        f'导入 网络\n打印(网络.提交("{http_base}/echo", "名字=小明"))\n',
        rust_exe) == "收到:名字=小明\n"


def test_network_errors(rust_exe, http_base):
    """404 与连不上都要报错（两边都不许静默给空文本）。"""
    _both_fail(f'导入 网络\n打印(网络.获取("{http_base}/bad"))\n', rust_exe)
    _both_fail('导入 网络\n打印(网络.获取("http://127.0.0.1:2/x"))\n', rust_exe)
    _both_fail('导入 网络\n打印(网络.获取("ftp://a/b"))\n', rust_exe)


# ---------------------------------------------------------------------------
# 切片与输入（Rust 宿主原来是缺的，端到端跑真实项目才撞出来）
# ---------------------------------------------------------------------------

def test_slice_read(rust_exe):
    assert _agree(
        '令 a = [1, 2, 3, 4, 5]\n'
        '打印(a[1:3], a[:2], a[3:], a[-2:], a[:-2])\n'
        '打印(a[::2], a[1::2], a[::-1], a[3:0:-1])\n'
        '打印(a[10:20], a[-100:2], a[2:1])\n'
        '令 s = "你好世界"\n打印(s[1:3], s[::-1], s[::2])\n',

        rust_exe) == ("[2, 3] [1, 2] [4, 5] [4, 5] [1, 2, 3]\n"
                      "[1, 3, 5] [2, 4] [5, 4, 3, 2, 1] [4, 3, 2]\n"
                      "[] [1, 2] []\n好世 界世好你 你世\n")


def test_slice_write(rust_exe):
    assert _agree(
        '令 a = [1, 2, 3, 4, 5]\na[1:3] = [9, 9]\n打印(a)\n'
        '令 b = [1, 2, 3, 4]\nb[1:3] = [7]\n打印(b)\n'
        '令 c = [1, 4]\nc[1:1] = [2, 3]\n打印(c)\n'
        '令 d = [1, 2, 3]\nd[1:] = []\n打印(d)\n'
        '令 e = [1, 2, 3, 4, 5]\ne[::2] = [7, 8, 9]\n打印(e)\n'
        '令 f = [1, 2, 3, 4]\nf[::-1] = [9, 8, 7, 6]\n打印(f)\n'
        '令 g = [1, 2, 3]\ng[1:] += [99]\n打印(g)\n',

        rust_exe) == ("[1, 9, 9, 4, 5]\n[1, 7, 4]\n[1, 2, 3, 4]\n[1]\n"
                      "[7, 2, 8, 4, 9]\n[6, 7, 8, 9]\n[1, 2, 3, 99]\n")


def test_slice_errors(rust_exe):
    _both_fail('令 a = [1, 2, 3]\n打印(a[::0])\n', rust_exe)
    _both_fail('令 a = [1, 2, 3, 4, 5]\na[::2] = [1, 2]\n', rust_exe)
    _both_fail('令 d = {"a": 1}\n打印(d[1:2])\n', rust_exe)


def test_input_prompt_and_read(rust_exe):
    assert _agree(
        '令 x = 输入("提示：")\n打印("得到：" + x)\n',
        rust_exe, feed="小明\n") == "提示：得到：小明\n"


def test_input_eof_is_an_error(rust_exe):
    """输入到末尾要报「输入结束」，不能悄悄给空串。"""
    _both_fail('令 x = 输入()\n打印(x)\n', rust_exe)


# ---------------------------------------------------------------------------
# 排序（`Val` 的比较曾经漏了字符串与列表，排序**静默失效**）
# ---------------------------------------------------------------------------

def test_sort_strings_and_lists(rust_exe):
    """字符串排序、以及 `[[总分, 姓名], …]` 这种「列表当键」的排序。"""
    assert _agree(
        '令 a = ["banana", "apple", "cherry"]\na.排序()\n打印(a)\n'
        '令 b = [[275, "小明"], [293, "小丽"], [187, "小刚"]]\nb.排序()\n打印(b)\n'
        '打印(b[::-1])\n'
        '打印(最大([3, 1, 2]), 最小([3, 1, 2]), 最大(["b", "a"]))\n',

        rust_exe) == ("['apple', 'banana', 'cherry']\n"
                      "[[187, '小刚'], [275, '小明'], [293, '小丽']]\n"
                      "[[293, '小丽'], [275, '小明'], [187, '小刚']]\n"
                      "3 1 b\n")
