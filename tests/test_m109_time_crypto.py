# -*- coding: utf-8 -*-
"""M109：时间 + 余项（日期加减月/年 + ISO 解析 / HMAC + 口令摘要 + 安全随机 / gzip）。

从「备份归档」探针（`examples/绕道探针/备份归档/`）撞出来的缺口：
`日期.加月` / `加年` / `解析ISO`、`加密.HMAC` / `口令摘要` / `安全随机字节`、
`压缩.压缩文件` / `解压文件`。

⚠️ **加密这三件必须与 Python 标准库逐字节一致**（`hmac` / `hashlib.pbkdf2_hmac`）——
Rust 侧是**自己写的**（HMAC 的 ipad/opad、PBKDF2 的迭代），拿 Python 当裁判。
⚠️ **gzip 的字节各引擎不同**（Rust 只写 `stored` 块，见 `docs/embed.md`），
所以判据只比「解压结果」与「互相能不能解开」。
"""

import gzip
import hashlib
import hmac
import io
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from conftest import rust_exe_path, tmp_bc_path          # noqa: E402
from oracle.jishi import serialize                        # noqa: E402
from oracle.jishi.interpreter import run_source           # noqa: E402

RUST_EXE = rust_exe_path()


def _跑(src: str) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        run_source(src, filename="<t>")
    return buf.getvalue()


def _字节码(src: str):
    bc = tmp_bc_path(ROOT / "_tmp_m109_bc.json")
    from oracle.jishi.compiler import compile_source
    bc.write_text(serialize.dumps(compile_source(src, "<t>")), encoding="utf-8")
    return bc


# ---------------------------------------------------------------------------
# 一、日期：加月 / 加年 / 解析ISO
# ---------------------------------------------------------------------------

def test_加月_按日历月推且月末收口():
    assert _跑(
        '导入 日期\n'
        '打印(日期.加月(1, "2026-01-31"))\n'
        '打印(日期.加月(1, "2026-10-12"))\n'
        '打印(日期.加月(-1, "2026-03-31"))\n'
        '打印(日期.加月(13, "2026-10-12"))\n'
        '打印(日期.加月(0, "2026-02-28"))\n'
    ) == "2026-02-28\n2026-11-12\n2026-02-28\n2027-11-12\n2026-02-28\n"


def test_加年_闰日落到平年收口():
    assert _跑(
        '导入 日期\n'
        '打印(日期.加年(1, "2024-02-29"))\n'
        '打印(日期.加年(4, "2024-02-29"))\n'
        '打印(日期.加年(-1, "2026-01-01"))\n'
    ) == "2025-02-28\n2028-02-29\n2025-01-01\n"


def test_加月不是加30天():
    """这条是「为什么要有 `加月`」：1 月 31 日加「一个月」该落在 2 月末，
    而加 30 天会跑到 3 月 2 日 —— 天数加法在月末越错越远。"""
    assert _跑(
        '导入 日期\n打印(日期.加月(1, "2026-01-31"))\n'
        '打印(日期.加天数(30, "2026-01-31"))\n'
    ) == "2026-02-28\n2026-03-02\n"


def test_加月_非法日期要报错():
    from oracle.jishi.errors import JishiError
    with pytest.raises(JishiError) as ei:
        _跑('导入 日期\n日期.加月(1, "2026-02-30")\n')
    assert "不是有效的日期" in str(ei.value)


def test_解析ISO_各种写法():
    assert _跑(
        '导入 日期\n'
        '打印(日期.解析ISO("2026-10-12"))\n'
        '打印(日期.解析ISO("2026-10-12T08:30:00"))\n'
        '打印(日期.解析ISO("2026-10-12T23:00:00+08:00"))\n'
        '打印(日期.解析ISO("2026-10-12T08:30:00.123Z"))\n'
        '打印(日期.解析ISO("2026-10-12 08:30:00"))\n'
        '打印(日期.解析ISO("2026-10-12T08:30"))\n'
        '打印(日期.解析ISO("  2026-10-12T00:00:00Z  "))\n'
    ) == "2026-10-12\n" * 7


def test_解析ISO_只取字面日期不做时区换算():
    """**定死的规则**：`2026-10-12T23:00:00Z` 在东八区其实已是 10-13，
    但这里按**字面**给 10-12 —— 时区换算要靠整套时区库，标准库不做。"""
    assert _跑('导入 日期\n打印(日期.解析ISO("2026-10-12T23:59:59Z"))\n') \
        == "2026-10-12\n"


def test_解析ISO_非法输入要报错():
    from oracle.jishi.errors import JishiError
    for 坏 in ("2026-2-30", "20261012", "2026/10/12", "2026-13-01", "2026-02-30",
               "不是日期"):
        with pytest.raises(JishiError):
            _跑(f'导入 日期\n日期.解析ISO({坏!r})\n')


# ---------------------------------------------------------------------------
# 二、加密：HMAC / 口令摘要 / 安全随机   —— 与 Python 标准库对拍
# ---------------------------------------------------------------------------

def test_HMAC与Python的hmac逐字节一致():
    """拿 Python 的 `hmac` 当裁判（Rust 侧是自己写的 ipad/opad）。"""
    用例 = [
        ("key", "msg", "sha256"),
        ("key", "msg", "md5"),
        ("key", "msg", "sha1"),
        ("key", "msg", "sha512"),
        ("", "msg", "sha256"),                       # 空密钥
        ("口令", "内容也中文", "sha256"),              # 中文
        ("k" * 200, "长密钥超块长", "sha256"),         # 密钥长于块长（先摘要一次）
        ("k" * 200, "长密钥", "sha512"),
    ]
    for k, m, algo in 用例:
        期望 = hmac.new(k.encode("utf-8"), m.encode("utf-8"), algo).hexdigest()
        got = _跑(f'导入 加密\n打印(加密.HMAC({k!r}, {m!r}, {algo!r}))\n').strip()
        assert got == 期望, (k, m, algo)


def test_HMAC默认算法是sha256且算法名可归一():
    assert _跑(
        '导入 加密\n'
        '打印(加密.HMAC("k", "m") == 加密.HMAC("k", "m", "sha256"))\n'
        '打印(加密.HMAC("k", "m", "SHA-256") == 加密.HMAC("k", "m"))\n'
    ) == "真\n真\n"


def test_口令摘要与Python的pbkdf2逐字节一致():
    用例 = [(("pw", "salt", 1000, "sha256")), (("pw", "盐", 3, "sha1")),
            (("", "salt", 10, "md5")), (("口令", "s", 7, "sha512"))]
    for pwd, salt, iters, algo in 用例:
        期望 = hashlib.pbkdf2_hmac(
            algo, pwd.encode("utf-8"), salt.encode("utf-8"), iters).hex()
        got = _跑(f'导入 加密\n打印(加密.口令摘要({pwd!r}, {salt!r}, {iters}, {algo!r}))\n').strip()
        assert got == 期望, (pwd, salt, iters, algo)


def test_口令摘要_盐不同结果就不同():
    assert _跑(
        '导入 加密\n'
        '打印(加密.口令摘要("pw", "盐一", 100) == 加密.口令摘要("pw", "盐一", 100))\n'
        '打印(加密.口令摘要("pw", "盐一", 100) == 加密.口令摘要("pw", "盐二", 100))\n'
        '打印(加密.口令摘要("pw", "盐一", 100) == 加密.口令摘要("pw", "盐一", 99))\n'
    ) == "真\n假\n假\n"


def test_安全随机字节_长度与不可预测():
    # M113 起 `安全随机字节` 返回**字节**（以前返回十六进制文本，长度是 2×）
    assert _跑(
        '导入 加密\n'
        '令 a = 加密.安全随机字节(16)\n'
        '令 b = 加密.安全随机字节(16)\n'
        '打印(长度(a))\n'
        '打印(长度(加密.安全随机字节(1)))\n'
        '打印(a == b)\n'
        '打印(类型(a))\n'
    ) == "16\n1\n假\n字节\n"


def test_安全随机字节_长度必须是正整数():
    from oracle.jishi.errors import JishiError
    for 坏 in ("0", "-3"):
        with pytest.raises(JishiError):
            _跑(f'导入 加密\n加密.安全随机字节({坏})\n')


# ---------------------------------------------------------------------------
# 三、压缩：gzip 单文件
# ---------------------------------------------------------------------------

def test_压缩文件与解压文件_往返(tmp_path):
    目标 = tmp_path / "a.gz"
    assert _跑(
        '导入 压缩\n'
        f'压缩.压缩文件({str(目标)!r}, "一行\\n" * 30)\n'
        f'打印(压缩.解压文件({str(目标)!r}) == "一行\\n" * 30)\n'
    ) == "真\n"
    # 产出的确实是一个标准 gzip：Python 的 gzip 读得出来
    assert gzip.open(目标, "rb").read().decode("utf-8") == "一行\n" * 30


def test_解压文件_认Python压出来的gz(tmp_path):
    """别人（Python / Node）用**真 deflate** 压的，Rust 的 `inflate` 也要认。"""
    目标 = tmp_path / "py.gz"
    with gzip.open(目标, "wb") as f:
        f.write("中文内容 repetitend ".encode("utf-8") * 200)
    assert _跑(
        f'导入 压缩\n打印(压缩.解压文件({str(目标)!r}) == '
        '"中文内容 repetitend " * 200)\n'
    ) == "真\n"


def test_压缩文件_不写时间戳所以字节可复现(tmp_path):
    """头里 MTIME 写 0 ⇒ 同一份内容两次压出来字节相同（否则产物永远不一样）。"""
    a, b = tmp_path / "a.gz", tmp_path / "b.gz"
    _跑(f'导入 压缩\n压缩.压缩文件({str(a)!r}, "同一份内容")\n')
    _跑(f'导入 压缩\n压缩.压缩文件({str(b)!r}, "同一份内容")\n')
    assert a.read_bytes() == b.read_bytes()
    assert a.read_bytes()[:4] == b"\x1f\x8b\x08\x00"   # gzip 魔数 + deflate + FLG=0


def test_解压文件_不是UTF8要报错(tmp_path):
    from oracle.jishi.errors import JishiError
    目标 = tmp_path / "bin.gz"
    with gzip.open(目标, "wb") as f:
        f.write(bytes([0xff, 0xfe, 0xfd, 0x00]))
    with pytest.raises(JishiError) as ei:
        _跑(f'导入 压缩\n压缩.解压文件({str(目标)!r})\n')
    assert "UTF-8" in str(ei.value)


def test_解压文件_坏文件要报错(tmp_path):
    from oracle.jishi.errors import JishiError
    目标 = tmp_path / "坏.gz"
    目标.write_bytes(b"not a gzip at all")
    with pytest.raises(JishiError) as ei:
        _跑(f'导入 压缩\n压缩.解压文件({str(目标)!r})\n')
    assert "解压" in str(ei.value)


# ---------------------------------------------------------------------------
# 四、五执行器一致
# ---------------------------------------------------------------------------

_脚本 = (
    '导入 日期\n导入 加密\n导入 压缩\n导入 路径\n'
    '打印(日期.加月(1, "2026-01-31"))\n'
    '打印(日期.加月(-1, "2026-03-31"))\n'
    '打印(日期.加年(1, "2024-02-29"))\n'
    '打印(日期.解析ISO("2026-10-12T23:00:00+08:00"))\n'
    '打印(加密.HMAC("key", "msg"))\n'
    '打印(加密.HMAC("key", "msg", "md5"))\n'
    '打印(加密.HMAC("key", "msg", "sha512"))\n'
    '打印(加密.口令摘要("pw", "salt", 1000))\n'
    '打印(加密.口令摘要("pw", "salt", 3, "sha1"))\n'
    '令 工作 = 路径.临时目录("M109-")\n'
    '令 gz = 路径.连接(工作, "a.gz")\n'
    '压缩.压缩文件(gz, "内容" * 100)\n'
    '打印(压缩.解压文件(gz) == "内容" * 100)\n'
    '打印(长度(加密.安全随机字节(16)))\n'
    '路径.递归删除(工作)\n'
    '打印(路径.存在(工作))\n'
)


def _期望() -> str:
    return (
        "2026-02-28\n"
        "2026-02-28\n"
        "2025-02-28\n"
        "2026-10-12\n"
        "2d93cbc1be167bcb1637a4a23cbff01a7878f0c50ee833954ea5221bb1b8c628\n"
        "18e3548c59ad40dd03907b7aeee71d67\n"
        "1e4b55b925ccc28ed90d9d18fc2393fcbe164c0d84e67e173cc5aa486b7afc10"
        "6633c66bdc309076f5f8d9fdbbb62456f894f2c23377fbcc12f4ab2940eb6d70\n"
        "0a38253555ce37f5c72a6b703f996814ebf241f203af146e93dcdeb031c5567e\n"
        "b095208e7496b1a485a5dbe9532b318591fa266c\n"
        "真\n"
        # M113 起 `安全随机字节(16)` 返回 16 个**字节**（以前是 32 个十六进制字符）
        "16\n"
        "假\n"
    )


def test_五执行器一致_M109():
    """树遍历 / 字节码 VM / C VM / Rust VM / Node 逐字节一致。"""
    import check_engines as ce
    from oracle.jishi.vm import VM
    from oracle.jishi.compiler import compile_source
    import oracle.jishi.cvm_bind as cvm_bind

    rel = "m109.jsh"
    run_dir = os.path.join(tempfile.gettempdir(), f"_m109_{os.getpid()}")
    shutil.rmtree(run_dir, ignore_errors=True)
    os.makedirs(run_dir, exist_ok=True)
    open(os.path.join(run_dir, rel), "w", encoding="utf-8").write(_脚本)
    lines = _脚本.split("\n")
    out = {}
    try:
        with ce._in_dir(run_dir):
            out["树遍历"] = ce._run(lambda: run_source(_脚本, rel), lines)[0]
            out["字节码VM"] = ce._run(
                lambda: VM(compile_source(_脚本, rel), rel).run(), lines)[0]
            out["C VM"] = ce._run(
                lambda: cvm_bind.run_source_c(_脚本, rel), lines)[0]
        if RUST_EXE.is_file():
            out["Rust VM"] = ce._run_rust_vm(_脚本, rel, run_dir)[0]
        out["Node"] = ce._run_node_vm(_脚本, rel, run_dir)[0]
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)

    期望 = _期望()
    for 名, 值 in out.items():
        assert 值 == 期望, f"{名} 不一致：\n{值!r}\n期望：\n{期望!r}"


def test_Rust口令摘要与Python一致():
    """Rust 的 PBKDF2 是自己写的（迭代异或），**单拎出来**跟 Python 比一次。"""
    if not RUST_EXE.is_file():
        pytest.skip("没有 rust 宿主产物")
    src = '导入 加密\n打印(加密.口令摘要("pw", "salt", 1000))\n'
    bc = _字节码(src)
    r = subprocess.run([str(RUST_EXE), "--load-bytecode", str(bc)],
                       capture_output=True)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:300]
    assert r.stdout.decode("utf-8", "replace").strip() == \
        hashlib.pbkdf2_hmac("sha256", b"pw", b"salt", 1000).hex()


# ---------------------------------------------------------------------------
# 五、探针真的能跑（备份归档）
# ---------------------------------------------------------------------------

def test_探针_备份归档(monkeypatch, tmp_path):
    样本 = tmp_path / "样本"
    样本.mkdir()
    日志一 = "2026-10-02T08:00:00Z 启动\n"
    日志二 = "2026-10-02T09:00:00Z 警告\n"
    # `newline=""`：不做换行转换，和基石侧 `文件.读文本` 一个口径
    (样本 / "运行.log").write_text(日志一, encoding="utf-8", newline="")
    (样本 / "警告.log").write_text(日志二, encoding="utf-8", newline="")
    (样本 / "说明.md").write_text("# 说明\n", encoding="utf-8", newline="")
    原长 = len(日志一) + len(日志二)

    探针 = ROOT / "examples" / "绕道探针" / "备份归档" / "备份.jsh"
    buf = io.StringIO()
    monkeypatch.chdir(tmp_path)
    with redirect_stdout(buf):
        run_source(探针.read_text(encoding="utf-8"), filename="备份.jsh")
    out = buf.getvalue()

    assert "已打包 备份-" in out, out
    assert f"已压日志 日志.gz（原文 {原长} 字符）" in out, out
    assert "已写 口令.txt" in out, out
    assert "下次备份" in out and "年度复盘" in out, out
    assert f"日志解压回来 {原长} 字符" in out, out
    assert "见绕道 R301" in out, out

    import datetime
    归档 = tmp_path / "归档"
    assert sorted(p.name for p in 归档.iterdir()) == sorted([
        "备份-%s.zip" % datetime.date.today().strftime("%Y-%m-%d"),
        "口令.txt", "日志.gz", "清单.txt"])
    # 口令文件里存的是「盐 + 口令摘要」，**不是口令本身**
    口令文 = (归档 / "口令.txt").read_text(encoding="utf-8")
    assert 口令文.startswith("盐 ") and "口令摘要 " in 口令文
    assert "示例口令" not in 口令文
    盐文本 = 口令文.splitlines()[0][2:]
    assert len(盐文本) == 32
    # ⚠️ M113 起盐是**字节**：口令摘要拿的是**原始字节**，不是那串十六进制文本
    # （`.txt` 里存成十六进制只是为了能放进文本文件）
    盐 = bytes.fromhex(盐文本)
    assert 口令文.splitlines()[1].endswith(
        hashlib.pbkdf2_hmac("sha256", "示例口令".encode("utf-8"),
                            盐, 1000).hex())
    # 清单每条带 sha256，签名是自己算得出来的
    清单 = (归档 / "清单.txt").read_text(encoding="utf-8")
    assert "sha256 " in 清单
    期望签名 = hmac.new("示例口令".encode("utf-8"), 清单.encode("utf-8"),
                        "sha256").hexdigest()
    assert 期望签名 in out
