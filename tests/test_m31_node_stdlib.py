# -*- coding: utf-8 -*-
"""M31：JS（Node）宿主的标准库补齐到与 Python 侧同宽。

M30 把 JS 宿主补到「5 个常用模块」（表格 / 文本 / json / 正则 / 系统），
这一轮把剩下 7 个补齐：**路径 / 文件 / 日期 / 时间 / 加密 / 压缩 / 网络**。
补完的直接结果：`examples/projects/` 里的**全部**项目都能在 JS 宿主上跑。

三块各有各的坑，都是「照着 Python 写反而会错」的类型：

- **压缩**：Node 没有内置 zip。`zlib` 只管 deflate 流，本地文件头、中央目录、
  EOCD、CRC-32 都得按 PKWARE 规范自己拼——所以这里还测了**跨宿主互操作**
  （Node 打的 zip 用 Python 的 `zipfile` 读，反过来也一样）。
- **网络**：Node 没有同步 HTTP（`fetch` 是异步的，而基石没有 `await`）。
  方案是 `spawnSync` 起一个自身的子进程执行异步请求，结果经 stdout 回传。
  测试用本地 `http.server`，不依赖外网。
- **文件**：Python 的文本模式在 Windows 上会把 `\n` 悄悄写成 `\r\n`，
  于是「同一份程序在两个平台产出的文件字节不同」，也与 Node 的 `fs` 对不上。
  这一轮统一改成**字节保真**（显式 `newline=""`）。

动态值（`时间.现在()`、`时间.时间戳()`）没法逐字节对拍，改断言格式与范围。
"""

from __future__ import annotations

import http.server
import io
import shutil
import subprocess
import sys
import time
import threading
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import tmp_bc_path   # noqa: E402

from jishi import cvm_bind                        # noqa: E402
from jishi import serialize                       # noqa: E402
from jishi import vm as vm_mod                    # noqa: E402
from jishi.compiler import compile_source         # noqa: E402
from jishi.errors import JishiError               # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="本机无 Node.js，跳过 JS VM 对拍")


# ---------------------------------------------------------------------------
# 对拍脚手架
# ---------------------------------------------------------------------------

def _py_run(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        vm_mod.VM(compile_source(src, "<t>"), "<t>").run()
    return buf.getvalue()


def _node_run(src: str) -> tuple[int, str, str]:
    bc = tmp_bc_path(ROOT / "_tmp_m31_bc.json")
    bc.write_text(serialize.dumps(compile_source(src, "<t>")), encoding="utf-8")
    r = subprocess.run([NODE, str(ROOT / "node" / "index.js"), str(bc)],
                       capture_output=True, text=True, encoding="utf-8")
    return r.returncode, r.stdout, r.stderr


def _agree(src: str) -> str:
    """Python VM 与 Node 必须逐字节一致（Node 不得非零退出）。"""
    py = _py_run(src)
    rc, out, err = _node_run(src)
    assert rc == 0, f"Node 非零退出：rc={rc}\n{err[:400]}"
    assert out == py, f"输出不一致\nPython: {py!r}\nNode:   {out!r}"
    return out


def _agree_all(src: str) -> str:
    """关键语义再加一路 C VM（树遍历由 test_m23 系列覆盖，这里省一层开销）。"""
    p = _py_run(src)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cvm_bind.run_source_c(src, "<t>")
    assert buf.getvalue() == p, "C VM 与 Python VM 不一致"
    return _agree(src)


def _both_fail(src: str) -> None:
    """两边都必须报错（错误文案不要求逐字相同，但都得是基石异常）。"""
    with pytest.raises(JishiError):
        _py_run(src)
    rc, _out, err = _node_run(src)
    assert rc != 0, f"这段代码本该报错，但 Node 正常退出了：{_out!r}"
    assert "JishiError" not in err.splitlines()[0], f"错误没被接住：{err[:300]}"


def _p(**kw) -> str:
    """把参数拼成一段「令 …」的基石代码（值是 Python 侧的字面量）。"""
    return "\n".join(f"{k} = {v}" for k, v in kw.items())


# ---------------------------------------------------------------------------
# 1. 日期
# ---------------------------------------------------------------------------

def test_node_date_format_directives():
    """strftime 子集：字母、数字、英文周月名、%U/%W 的周号公式。"""
    out = _agree(
        '导入 日期\n'
        '打印(日期.格式化("2026-09-05", "%Y|%y|%m|%d|%H|%M|%S|%j|%w|%u"))\n'
        '打印(日期.格式化("2026-09-05", "%a|%A|%b|%B|%p"))\n'
        '打印(日期.格式化("2026-09-05", "%U|%W|%x|%X|%%"))\n'
        '打印(日期.格式化("2026-01-01", "%U|%W|%j|%a"))\n'   # 新年第一个周几之前算第 0 周
        '打印(日期.格式化("2024-12-31", "%U|%W|%j"))\n'
    )
    assert "%" in out and "Sat" in out


def test_node_date_format_chinese_and_c_locale():
    """格式串里的中文照原样留在结果里（Python 侧为此做过摘出/放回）。"""
    assert _agree(
        '导入 日期\n'
        '打印(日期.格式化("2026-09-05", "%Y年%m月%d日"))\n'
        '打印(日期.格式化("2026-09-05", "%c"))\n'
    ) == "2026年09月05日\nSat Sep  5 00:00:00 2026\n"


def test_node_date_parse_and_type():
    """`解析` 的显示与类型名要与 Python 的 datetime 一致。"""
    assert _agree(
        '导入 日期\n'
        '打印(日期.解析("2026-09-01"))\n'
        '打印(类型(日期.解析("2026-09-01")))\n'
    ) == "2026-09-01 00:00:00\ndatetime\n"


def test_node_date_arithmetic_and_compare():
    assert _agree(
        '导入 日期\n'
        '打印(日期.加天数(1, "2024-02-28"), 日期.加天数(1, "2023-02-28"))\n'
        '打印(日期.减天数(2, "2026-01-01"), 日期.加天数(1, "2026/09/01"))\n'
        '打印(日期.相差天数("2026-03-01", "2026-02-01"),'
        ' 日期.相差天数("2026-01-01", "2026-03-01"))\n'
        '打印(日期.早于("2026-01-01", "2026-06-01"),'
        ' 日期.晚于("2026-01-01", "2026-06-01"),'
        ' 日期.相等("2026-01-01", "2026-01-01"))\n'
    ) == "2024-02-29 2023-03-01\n2025-12-30 2026-09-02\n28 -59\n真 假 真\n"


def test_node_date_weekday_sequence():
    """连着一周看星期名（跨月边界也走一遍）。"""
    assert _agree(
        '导入 日期\n'
        '令 d = "2026-08-30"\n'
        '遍历 i 在 范围(9)：\n'
        '    打印(日期.星期名(d))\n'
        '    d = 日期.加天数(1, d)\n'
    ) == "日\n一\n二\n三\n四\n五\n六\n日\n一\n"


def test_node_date_now_helpers():
    """动态值不做逐字节，比「形状」与范围。"""
    assert _agree(
        '导入 日期\n'
        '打印(长度(日期.今天()) == 10)\n'
        '打印(日期.今年() > 2020, 日期.本月() >= 1, 日期.本月() <= 12)\n'
        '打印(日期.本年天数() == 365 或 日期.本年天数() == 366)\n'
    ) == "真\n真 真 真\n真\n"


def test_node_date_invalid():
    """非法日期（含不存在的 2 月 30 日）两边都报错。"""
    _both_fail('导入 日期\n日期.解析("昨天")\n')
    _both_fail('导入 日期\n日期.解析("2026-02-30")\n')
    _both_fail('导入 日期\n日期.加天数(1, "2026-13-01")\n')


# ---------------------------------------------------------------------------
# 2. 时间
# ---------------------------------------------------------------------------

def test_node_time_now_shape():
    assert _agree(
        '导入 时间\n'
        '打印(长度(时间.今天()), 长度(时间.现在()))\n'
        '令 d = 时间.此刻()\n'
        '打印(d["年"] > 2020, d["月"] >= 1, d["日"] >= 1, 长度(d["星期"]) == 1)\n'
        '打印(时间.时间戳() > 1700000000)\n'
    ) == "10 19\n真 真 真 真\n真\n"


def test_node_time_format_timestamp_is_local():
    """时间戳格式化按**本地时区**（与 Python 的 `fromtimestamp` 一致）。

    期望值由本机时区算出来，**不能写死**：开发机 UTC+8 给 `08:00:00`，
    CI 的 runner 是 UTC 给 `00:00:00`（M35 实测两处 CI 全红就是这个）。
    语义本身（与 Python 本地时间一致）仍被校验，`_agree` 还额外保证
    两个宿主逐字节相同。
    """
    want = "\n".join(
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        for ts in (0, 1757000000)) + "\n"
    assert _agree(
        '导入 时间\n'
        '打印(时间.格式化时间戳(0))\n'
        '打印(时间.格式化时间戳(1757000000))\n'
    ) == want


def test_node_time_sleep_blocks_synchronously():
    """`睡眠` 靠 `Atomics.wait` 同步阻塞——这是基石没有 await 时的唯一办法。"""
    assert _agree(
        '导入 时间\n'
        '令 a = 时间.高精度时间()\n'
        '时间.睡眠(0.02)\n'
        '令 b = 时间.高精度时间()\n'
        '打印(b > a, b - a > 0.015)\n'
    ) == "真 真\n"


def test_node_time_weekday_and_add_days():
    assert _agree(
        '导入 时间\n'
        '打印(时间.星期名("2026-09-05"), 时间.加天数(1, "2026-09-05"),'
        ' 时间.加天数(30, "2026-01-01"))\n'
    ) == "六 2026-09-06 2026-01-31\n"


# ---------------------------------------------------------------------------
# 3. 路径
# ---------------------------------------------------------------------------

def test_node_path_suffix_matches_pathlib():
    """`后缀` 要按 pathlib 的规则：`a.` 与 `.bashrc` 都算无后缀。

    （JS 的 `path.extname('a.')` 给 `'.'`，直接用就对不上。）
    """
    assert _agree(
        '导入 路径\n'
        '遍历 p 在 ["a.txt", "a.tar.gz", "a.", ".bashrc", "无后缀", "a.b/c"]：\n'
        '    打印(路径.后缀(p), 路径.无后缀名(p))\n'
    ) == ".txt a\n.gz a.tar\n a.\n .bashrc\n 无后缀\n c\n"


def test_node_path_name_and_parent():
    """Windows 上 Python 的 `Path.__str__` 总吐反斜杠，JS 的 path 会保留原样。"""
    assert _agree(
        '导入 路径\n'
        '遍历 p 在 ["a/b/c.txt", "c.txt", "a/b/", "c:/x/y.txt"]：\n'
        '    打印(路径.文件名(p), 路径.父目录(p))\n'
    ).count("\n") == 4


def test_node_path_join_resets_on_absolute():
    """pathlib 的 `/`：右段是绝对路径就**重置**，不是拼接。"""
    assert _agree(
        '导入 路径\n'
        '打印(路径.连接("a", "b", "c"), 路径.连接("a", ""), 路径.连接())\n'
        '打印(路径.连接("a/b", "c"), 路径.连接("a", "c:/d"))\n'
    ).count("\n") == 2


def test_node_path_exists_and_kind():
    assert _agree(
        '导入 路径\n'
        '打印(路径.存在("不存在的路径xyz"), 路径.是文件("不存在的路径xyz"),'
        ' 路径.是目录("不存在的路径xyz"))\n'
        '打印(路径.存在("jishi"), 路径.是目录("jishi"),'
        ' 路径.是文件("pyproject.toml"))\n'
    ) == "假 假 假\n真 真 真\n"


def test_node_path_cwd_and_home():
    assert _agree(
        '导入 路径\n'
        '打印(路径.当前目录() == 路径.绝对路径("."))\n'
        '打印(长度(路径.绝对路径("jishi")) > 0, 长度(路径.主目录()) > 0)\n'
    ) == "真\n真 真\n"


def test_node_path_mutating_ops(tmp_path):
    """建目录 / 列出 / 大小 / 重命名 / 删除。"""
    base = (tmp_path / "p").as_posix()
    assert _agree(
        f'导入 路径\n'
        f'导入 文件\n'
        f'文件.创建目录("{base}/p1/p2")\n'
        f'打印(路径.是目录("{base}/p1/p2"))\n'
        f'文件.写文本("{base}/p1/f.txt", "内容")\n'
        f'打印(路径.列出("{base}/p1"))\n'
        f'打印(路径.大小("{base}/p1/f.txt"))\n'
        f'路径.重命名("{base}/p1/f.txt", "{base}/p1/g.txt")\n'
        f'打印(路径.列出("{base}/p1"))\n'
        f'打印(路径.删除("{base}/p1/g.txt"), 路径.删除("{base}/p1/g.txt"))\n'
    ) == "真\n['f.txt', 'p2']\n6\n['g.txt', 'p2']\n真 假\n"


def test_node_path_errors():
    _both_fail('导入 路径\n路径.大小("不存在的路径xyz")\n')
    _both_fail('导入 路径\n路径.列出("不存在的路径xyz")\n')
    _both_fail('导入 路径\n路径.重命名("不存在的路径xyz", "x")\n')


# ---------------------------------------------------------------------------
# 4. 文件
# ---------------------------------------------------------------------------

def test_node_file_write_read_roundtrip(tmp_path):
    """写 / 读 / 追加 / 写行 / 按行读——**含字节数**（换行保真的检验点）。"""
    base = tmp_path.as_posix()
    assert _agree(
        f'导入 文件\n'
        f'文件.创建目录("{base}/d")\n'
        f'文件.写文本("{base}/d/a.txt", "你好\\n世界\\n")\n'
        f'打印(文件.读文本("{base}/d/a.txt"))\n'
        f'打印(文件.按行读("{base}/d/a.txt"))\n'
        f'文件.追加文本("{base}/d/a.txt", "末尾")\n'
        f'打印(文件.按行读("{base}/d/a.txt"))\n'
        f'文件.写行("{base}/d/b.txt", ["甲", "乙", 3, 真])\n'
        f'打印(文件.按行读("{base}/d/b.txt"))\n'
        f'打印(文件.文件大小("{base}/d/b.txt"))\n'
    ) == ("你好\n世界\n\n['你好', '世界']\n['你好', '世界', '末尾']\n"
          "['甲', '乙', '3', '真']\n14\n")


def test_node_file_bytes_are_newline_faithful(tmp_path):
    """Windows 上 Python 的文本模式会把 `\\n` 变 `\\r\\n`——两边都必须是保真的。"""
    f = tmp_path / "nl.txt"
    src = ('导入 文件\n'
           f'文件.写文本("{tmp_path.as_posix()}/nl.txt", "a\\nb\\n")\n'
           f'打印(文件.文件大小("{tmp_path.as_posix()}/nl.txt"))\n')
    # 4 字节 = a \n b \n（要是有 \r\n 就是 6）
    assert _agree(src) == "4\n"
    # 直接看盘上字节，确认不是「两边都转换了」
    assert f.read_bytes() == b"a\nb\n"


def test_node_file_empty_and_blank_lines(tmp_path):
    base = tmp_path.as_posix()
    assert _agree(
        f'导入 文件\n'
        f'文件.写文本("{base}/empty.txt", "")\n'
        f'打印(文件.按行读("{base}/empty.txt"))\n'
        f'文件.写文本("{base}/nl.txt", "a\\n\\n")\n'
        f'打印(文件.按行读("{base}/nl.txt"))\n'
    ) == "[]\n['a', '']\n"


def test_node_file_kind_copy_delete(tmp_path):
    base = tmp_path.as_posix()
    assert _agree(
        f'导入 文件\n'
        f'文件.创建目录("{base}/d")\n'
        f'文件.写文本("{base}/d/a.txt", "x")\n'
        f'打印(文件.文件存在("{base}/d/a.txt"), 文件.是文件("{base}/d/a.txt"),'
        f' 文件.是目录("{base}/d"))\n'
        f'文件.复制("{base}/d/a.txt", "{base}/d/c.txt")\n'
        f'打印(文件.列出目录("{base}/d"))\n'
        f'文件.删除文件("{base}/d/c.txt")\n'
        f'打印(文件.文件存在("{base}/d/c.txt"))\n'
    ) == "真 真 真\n['a.txt', 'c.txt']\n假\n"


def test_node_file_path_helpers():
    """`路径拼接` 永远是 posix（Python 侧用的是 PurePosixPath）。"""
    assert _agree(
        '导入 文件\n'
        '打印(文件.路径拼接("a", "b", "c.txt"), 文件.路径拼接())\n'
        '打印(文件.文件名("a\\\\b\\\\c.txt"), 文件.扩展名("a/b/c.jsh"),'
        ' 文件.扩展名("无后缀"))\n'
    ) == "a/b/c.txt .\nc.txt .jsh \n"


def test_node_file_errors():
    _both_fail('导入 文件\n文件.读文本("不存在的文件xyz.txt")\n')
    _both_fail('导入 文件\n文件.列出目录("不存在的目录xyz")\n')
    _both_fail('导入 文件\n文件.删除文件("不存在的文件xyz")\n')


def test_node_file_delete_directory_is_explicit_error(tmp_path):
    """删目录要明确报「暂未提供」，不能静默删掉（那是数据丢失风险）。"""
    base = tmp_path.as_posix()
    with pytest.raises(JishiError) as ei:
        _py_run(f'导入 文件\n文件.创建目录("{base}/d")\n文件.删除文件("{base}/d")\n')
    assert "目录" in str(ei.value)
    rc, _out, err = _node_run(
        f'导入 文件\n文件.创建目录("{base}/d")\n文件.删除文件("{base}/d")\n')
    assert rc != 0 and "目录" in err


# ---------------------------------------------------------------------------
# 5. 加密
# ---------------------------------------------------------------------------

def test_node_crypto_digests():
    assert _agree(
        '导入 加密\n'
        '打印(加密.摘要("你好"), 加密.摘要("你好", "md5"))\n'
        '打印(加密.md5("abc"), 加密.sha1("abc"), 加密.sha512("abc"))\n'
        '打印(加密.支持的算法())\n'
    ).startswith("670d9743542cae3ea7ebe36af56bd53648b0a1126162e78d81a32934a711302e")


def test_node_crypto_algo_aliases():
    """算法名大小写 / `-` / `_` 都要被归一化掉。"""
    assert _agree(
        '导入 加密\n'
        '打印(加密.摘要("x", "SHA-256"), 加密.摘要("x", "MD5"),'
        ' 加密.摘要("x", "sha_1"))\n'
    ).count(" ") == 2


def test_node_crypto_non_text_input():
    """非文本参数走「值→文本」的统一显示（布尔给 `真`，不是 `True`）。"""
    assert _agree(
        '导入 加密\n'
        '打印(加密.摘要(123), 加密.摘要(真))\n'
        '打印(加密.摘要(1.5))\n'
    ).count("\n") == 2


def test_node_crypto_file_digest(tmp_path):
    """`文件摘要` 分块读（大文件不整个载入内存）。"""
    base = tmp_path.as_posix()
    assert _agree(
        f'导入 加密\n'
        f'导入 文件\n'
        f'文件.写文本("{base}/h.txt", "abc")\n'
        f'打印(加密.文件摘要("{base}/h.txt"), 加密.文件摘要("{base}/h.txt", "md5"))\n'
    ) == (f"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
          f" 900150983cd24fb0d6963f7d28e17f72\n")


def test_node_crypto_bad_algo():
    _both_fail('导入 加密\n加密.摘要("x", "sha3")\n')


# ---------------------------------------------------------------------------
# 6. 压缩（Node 没有内置 zip：容器格式是手写的）
# ---------------------------------------------------------------------------

def test_node_zip_roundtrip(tmp_path):
    base = tmp_path.as_posix()
    assert _agree(
        f'导入 压缩\n'
        f'导入 文件\n'
        f'文件.创建目录("{base}/z")\n'
        f'文件.写文本("{base}/z/x.txt", "压缩内容")\n'
        f'文件.写文本("{base}/z/y.txt", "第二份")\n'
        f'打印(压缩.列出内容(压缩.打包("{base}/z/t.zip",'
        f' ["{base}/z/x.txt", "{base}/z/y.txt"])))\n'
        f'打印(压缩.解压("{base}/z/t.zip", "{base}/z/out") != "")\n'
        f'打印(文件.列出目录("{base}/z/out"))\n'
        f'打印(文件.读文本("{base}/z/out/x.txt"),'
        f' 文件.按行读("{base}/z/out/y.txt"))\n'
    ) == "['x.txt', 'y.txt']\n真\n['x.txt', 'y.txt']\n压缩内容 ['第二份']\n"


def test_node_zip_directory_prefixes_and_slash(tmp_path):
    """目录递归收录；条目名按 zip 规范用 `/`（Python 的 zipfile 也会转）。"""
    base = tmp_path.as_posix()
    assert _agree(
        f'导入 压缩\n'
        f'导入 文件\n'
        f'文件.创建目录("{base}/dir/sub")\n'
        f'文件.写文本("{base}/dir/a.txt", "A")\n'
        f'文件.写文本("{base}/dir/sub/b.txt", "B")\n'
        f'打印(压缩.列出内容(压缩.打包("{base}/dir.zip", "{base}/dir")))\n'
        f'压缩.解压("{base}/dir.zip", "{base}/dir_out")\n'
        f'打印(文件.列出目录("{base}/dir_out/dir"),'
        f' 文件.列出目录("{base}/dir_out/dir/sub"))\n'
    ) == "['dir/a.txt', 'dir/sub/b.txt']\n['a.txt', 'sub'] ['b.txt']\n"


def test_node_zip_larger_than_one_block(tmp_path):
    """2000 行数据（过 deflate 的分块边界），内容要能原样回来。"""
    base = tmp_path.as_posix()
    assert _agree(
        f'导入 压缩\n'
        f'导入 文件\n'
        f'文件.写行("{base}/big.txt", 范围(2000))\n'
        f'压缩.解压(压缩.打包("{base}/big.zip", "{base}/big.txt"), "{base}/big_out")\n'
        f'令 行 = 文件.按行读("{base}/big_out/big.txt")\n'
        f'打印(长度(行), 行[0], 行[1999])\n'
    ) == "2000 0 1999\n"


def test_js_written_zip_is_readable_by_python_zipfile(tmp_path):
    """**跨宿主互操作**：Node 打的 zip 要能被 Python 的 `zipfile` 正确解开。

    只对拍「两边输出一样」是不够的——万一两边都写坏了呢。这一条用
    Python 标准库当裁判。
    """
    base = tmp_path.as_posix()
    rc, _out, err = _node_run(
        f'导入 压缩\n'
        f'导入 文件\n'
        f'文件.创建目录("{base}/seed")\n'
        f'文件.写文本("{base}/seed/中文名.txt", "内容一二三")\n'
        f'文件.写文本("{base}/seed/ascii.txt", "hello")\n'
        f'打印(压缩.打包("{base}/from_node.zip", "{base}/seed"))\n')
    assert rc == 0, err[:400]
    with zipfile.ZipFile(tmp_path / "from_node.zip") as zf:
        names = zf.namelist()
        assert "seed/中文名.txt" in names, names
        assert zf.read("seed/中文名.txt").decode("utf-8") == "内容一二三"
        assert zf.read("seed/ascii.txt").decode("utf-8") == "hello"
        # CRC 也要对（zipfile.read 会自己校验，这里再确认一次没坏）
        assert zf.testzip() is None


def test_python_written_zip_is_readable_by_node(tmp_path):
    """反过来：Python 打的 zip 要能被 Node 的 `压缩.解压` 解开。"""
    base = tmp_path.as_posix()
    seed = tmp_path / "seed2"
    seed.mkdir()
    (seed / "甲.txt").write_text("来自 Python", encoding="utf-8")
    with zipfile.ZipFile(tmp_path / "from_py.zip", "w",
                         zipfile.ZIP_DEFLATED) as zf:
        zf.write(seed / "甲.txt", "seed2/甲.txt")
    out = _agree(
        f'导入 压缩\n'
        f'导入 文件\n'
        f'打印(压缩.列出内容("{base}/from_py.zip"))\n'
        f'压缩.解压("{base}/from_py.zip", "{base}/out_py")\n'
        f'打印(文件.读文本(文件.路径拼接("{base}/out_py/seed2", "甲.txt")))\n')
    assert out == "['seed2/甲.txt']\n来自 Python\n"


def test_node_zip_errors():
    _both_fail('导入 压缩\n压缩.列出内容("不存在的.zip")\n')
    _both_fail('导入 压缩\n压缩.打包("x.zip")\n')


# ---------------------------------------------------------------------------
# 7. 网络（本地 http.server，不依赖外网）
# ---------------------------------------------------------------------------

PAGE = ('<html><body>'
        '<a href="http://a.example.com/1">第一个<b>链接</b></a>'
        '<a href="https://b.example.com/2">第二个链接</a>'
        '<a href="http://a.example.com/3">第三个</a>'
        '<a href="/relative">相对链接应被过滤</a>'
        '<a href="http://c.example.org/x">第四个</a>'
        '</body></html>')


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/json":
            body = '{"名称": "基石", "数量": 3, "ok": true}'.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
        else:
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(n)
        body = "收到:".encode("utf-8") + data
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def http_port():
    """起一个本地 HTTP 服务，返回端口。

    **让 HTTPServer 自己 bind 端口 0**（而不是先探一个空端口再关掉重绑）：
    后者是典型的 TOCTOU 竞态——`close()` 到重新 `bind()` 之间端口可能被
    别的进程抢走，于是偶发 `OSError: [WinError 10048] 每个套接字地址只允许
    使用一次`。这个竞态只在全量跑（大量子进程同时抢临时端口）时暴露，
    单跑必过，正是 M32 那类「假失败」的形态。
    """
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)   # 原子占端口
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_node_network_get_and_post(http_port):
    assert _agree(
        f'导入 网络\n'
        f'打印(长度(网络.获取("http://127.0.0.1:{http_port}/")) > 0)\n'
        f'打印(网络.提交("http://127.0.0.1:{http_port}/", "a=1&b=2"))\n'
    ) == "真\n收到:a=1&b=2\n"


def test_node_network_get_json(http_port):
    assert _agree(
        f'导入 网络\n'
        f'令 d = 网络.获取JSON("http://127.0.0.1:{http_port}/json")\n'
        f'打印(d["名称"], d["数量"], d["ok"])\n'
    ) == "基石 3 真\n"


def test_node_network_error_is_chinese():
    """连不上要报中文错，且不能把 JS 的堆栈甩给用户。"""
    with pytest.raises(JishiError):
        _py_run('导入 网络\n网络.获取("http://127.0.0.1:1/none", 1)\n')
    rc, _out, err = _node_run('导入 网络\n网络.获取("http://127.0.0.1:1/none", 1)\n')
    assert rc != 0
    assert "错误（" in err and "127.0.0.1" in err


# ---------------------------------------------------------------------------
# 8. 真实项目端到端
# ---------------------------------------------------------------------------

def _run_project(name: str, script: str, arg: str, feed, tmp_path: Path):
    src_path = ROOT / "examples" / "projects" / name / script
    src = src_path.read_text(encoding="utf-8")
    bc = tmp_bc_path(ROOT / "_tmp_m31_proj.json")
    bc.write_text(serialize.dumps(compile_source(src, str(src_path))),
                  encoding="utf-8")
    cmd_py = [sys.executable, "-m", "jishi.cli", str(src_path)]
    cmd_node = [NODE, str(ROOT / "node" / "index.js"), str(bc)]
    if arg:
        cmd_py.append(arg)
        cmd_node.append(arg)
    r_py = subprocess.run(cmd_py, capture_output=True, text=True,
                          encoding="utf-8", cwd=str(tmp_path), input=feed)
    r_node = subprocess.run(cmd_node, capture_output=True, text=True,
                            encoding="utf-8", cwd=str(tmp_path), input=feed)
    assert r_node.returncode == r_py.returncode, (
        f"退出码不同：py={r_py.returncode} node={r_node.returncode}\n"
        f"node stderr={r_node.stderr[:300]}")
    assert r_node.stdout == r_py.stdout, (
        f"输出不同\nPython: {r_py.stdout[:400]!r}\nNode:   {r_node.stdout[:400]!r}")
    return r_py.stdout


def test_node_real_project_web_crawl(http_port, tmp_path):
    """网页采集：`网络` + `正则` + `表格`（M31 补完网络模块后才跑得起来）。

    验收标准与 M30 一致：与 Python 侧**逐字节相同**，连产出的 CSV 也一样。
    """
    out = _run_project("网页采集", "采集.jsh", "", 
                       f"http://127.0.0.1:{http_port}/\n", tmp_path)
    assert "共提取到 4 个链接" in out
    assert "a.example.com：2 个" in out
    csv = tmp_path / "链接.csv"
    assert csv.exists()
    text = csv.read_bytes().decode("utf-8-sig")
    assert "a.example.com/1" in text and "相对链接应被过滤" not in text
    # BOM 也要在（Excel 打开不乱码）
    assert csv.read_bytes().startswith(b"\xef\xbb\xbf")


def test_node_real_project_grade_and_log(tmp_path):
    """M30 的两个项目在本轮改动（文件换行保真）后仍要一致。"""
    _run_project("成绩分析", "分析.jsh", "成绩.csv", None, tmp_path)
    _run_project("日志统计", "统计.jsh", "", "\n", tmp_path)


# ---------------------------------------------------------------------------
# 9. Python 侧的配套改动
# ---------------------------------------------------------------------------

def test_python_stdlib_text_conversion_is_chinese():
    """标准库把值转文本时要用统一显示（布尔给「真」，不是 `True`）。"""
    from jishi.stdlib import 加密 as crypto_mod
    assert crypto_mod.摘要(True) == crypto_mod.摘要("真")
    assert crypto_mod.摘要(3) == crypto_mod.摘要("3")


def test_python_file_writing_is_byte_faithful(tmp_path):
    """写文件不做换行转换，且布尔/空按中文写（与 JS 侧对齐）。"""
    from jishi.stdlib import 文件 as file_mod
    p = tmp_path / "x.txt"
    file_mod.写文本(p, "a\nb\n")
    assert p.read_bytes() == b"a\nb\n"
    file_mod.写行(tmp_path / "y.txt", [True, None, 3])
    assert (tmp_path / "y.txt").read_bytes() == "真\n空\n3\n".encode("utf-8")
