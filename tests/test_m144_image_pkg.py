# -*- coding: utf-8 -*-
"""M144：第一个第三方包 `图片格式转换`（纯基石实现的格式转换）。

判据分三层，**每层都用不同的裁判**：

1. **行为**：用基石自己跑包，看往返一致（这是「包能不能用」）；
2. **产物**：把写出来的 BMP / PNG 交给 **Python 的 zlib + struct** 去独立解析
   —— 自己写的代码验自己写的代码，两边同时错是看不出来的；
3. **产品版**：`jishi-rs` 从 M145 起也能导入包（本地包在编译期展开）——
   这条原来钉的是反面（「导入不了」），修好后按设计报红，已换成正向判据。
"""

import io
import struct
import sys
import zlib
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

from oracle.jishi import runtime                       # noqa: E402
from oracle.jishi.interpreter import run_source        # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
包目录 = ROOT / "examples" / "packages" / "图片格式转换"


@pytest.fixture
def 装了图片格式转换包(monkeypatch):
    """把「本地包」的搜索目录指向 `examples/packages`（与 `test_m10` 同一套手法）。"""
    assert (包目录 / "包.json").is_file(), "包目录不见了"
    monkeypatch.setattr(runtime, "_package_dirs",
                        lambda: [str(包目录.parent)])
    return 包目录


def _跑(源码: str) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        run_source(源码)
    return out.getvalue()


def test_包能被导入并且函数可用(装了图片格式转换包):
    出 = _跑(
        "导入 图片格式转换 从 本地包\n"
        "打印(图片格式转换.支持格式())\n"
        "令 图 = 图片格式转换.空图(2, 2, [1, 2, 3])\n"
        "打印(图片格式转换.取像素(图, 1, 1))\n"
    )
    assert "bmp" in 出 and "png" in 出
    assert "[1, 2, 3]" in 出


def test_写出的BMP是合法文件(tmp_path, 装了图片格式转换包):
    """BMP 交给 Python 的 struct 独立解析：签名、尺寸、像素都要对。"""
    路 = tmp_path / "甲.bmp"
    源 = (
        "导入 图片格式转换 从 本地包\n"
        "令 图 = 图片格式转换.空图(5, 3, [10, 20, 30])\n"          # 宽 5 会触发行补齐
        "图片格式转换.设像素(图, 4, 2, [200, 100, 50])\n"
        f'图片格式转换.写(图, "{路.as_posix()}")\n'
    )
    _跑(源)

    数据 = 路.read_bytes()
    assert 数据[:2] == b"BM"
    偏移, = struct.unpack_from("<I", 数据, 10)
    宽, 高 = struct.unpack_from("<ii", 数据, 18)
    位深, = struct.unpack_from("<H", 数据, 28)
    压缩, = struct.unpack_from("<I", 数据, 30)
    assert (宽, 高, 位深, 压缩) == (5, 3, 24, 0)
    每行 = ((宽 * 3 + 3) // 4) * 4
    assert len(数据) == 偏移 + 每行 * 3, "文件长度该正好等于头 + 每行补齐 × 行数"

    def 读(x, y):        # BMP 自下而上、BGR
        基 = 偏移 + (高 - 1 - y) * 每行 + x * 3
        return (数据[基 + 2], 数据[基 + 1], 数据[基])

    assert 读(0, 0) == (10, 20, 30)
    assert 读(4, 2) == (200, 100, 50)


def test_写出的PNG是合法文件(tmp_path, 装了图片格式转换包):
    """PNG 交给 Python 的 zlib 独立解压：块顺序、每个块的 CRC、像素都要对。"""
    路 = tmp_path / "乙.png"
    源 = (
        "导入 图片格式转换 从 本地包\n"
        "令 图 = 图片格式转换.空图(3, 2, [7, 8, 9])\n"
        "图片格式转换.设像素(图, 2, 1, [1, 2, 3])\n"
        f'图片格式转换.写(图, "{路.as_posix()}")\n'
    )
    _跑(源)

    数据 = 路.read_bytes()
    assert 数据[:8] == b"\x89PNG\r\n\x1a\n"
    位, 块们 = 8, []
    while 位 < len(数据):
        长, = struct.unpack_from(">I", 数据, 位)
        类型 = 数据[位 + 4:位 + 8]
        内容 = 数据[位 + 8:位 + 8 + 长]
        校验, = struct.unpack_from(">I", 数据, 位 + 8 + 长)
        assert 校验 == zlib.crc32(类型 + 内容) & 0xFFFFFFFF, f"{类型} 的 CRC 不对"
        块们.append((类型, 内容))
        位 += 12 + 长
    assert [t for t, _ in 块们] == [b"IHDR", b"IDAT", b"IEND"]

    宽, 高, 深, 色型 = struct.unpack_from(">IIBB", 块们[0][1], 0)
    assert (宽, 高, 深, 色型) == (3, 2, 8, 2)

    原 = zlib.decompress(b"".join(c for t, c in 块们 if t == b"IDAT"))
    assert len(原) == 高 * (1 + 宽 * 3)
    行首 = [原[y * (1 + 宽 * 3)] for y in range(高)]
    assert 行首 == [0, 0], "每行的过滤字节该是 0"
    像素 = {(x, y): tuple(原[y * (1 + 宽 * 3) + 1 + x * 3:][:3])
            for y in range(高) for x in range(宽)}
    assert 像素[(0, 0)] == (7, 8, 9)
    assert 像素[(2, 1)] == (1, 2, 3)


def test_读回自己写的BMP像素一致(tmp_path, 装了图片格式转换包):
    路 = tmp_path / "丙.bmp"
    出 = _跑(
        "导入 图片格式转换 从 本地包\n"
        "令 图 = 图片格式转换.空图(2, 2, [0, 0, 0])\n"
        "图片格式转换.设像素(图, 1, 0, [11, 22, 33])\n"
        f'图片格式转换.写(图, "{路.as_posix()}")\n'
        f'令 回 = 图片格式转换.读("{路.as_posix()}")\n'
        "打印(图片格式转换.取像素(回, 1, 0))\n"
    )
    assert "[11, 22, 33]" in 出


def test_PNG读不了要说清缺什么(tmp_path, 装了图片格式转换包):
    路 = tmp_path / "丁.png"
    _跑(
        "导入 图片格式转换 从 本地包\n"
        "图片格式转换.写(图片格式转换.空图(1, 1), \"" + 路.as_posix() + "\")\n"
    )
    with pytest.raises(Exception) as 捕:
        _跑("导入 图片格式转换 从 本地包\n图片格式转换.读(\"" + 路.as_posix() + "\")\n")
    消息 = str(捕.value)
    assert "PNG 读不了" in 消息
    assert "inflate" in 消息, "要说明缺的是「解压」这项能力，不能只说「出错了」"
    assert "bmp" in 消息.lower(), "该给出替代办法"


def test_包自带的测试全过(装了图片格式转换包):
    """包自己那 11 个用例用 `测试` 模块写的 —— 有失败 `汇总()` 会抛，这里就红。"""
    源码 = (包目录 / "图片格式转换_test.jsh").read_text(encoding="utf-8")
    out = io.StringIO()
    with redirect_stdout(out):
        run_source(源码, filename="图片格式转换_test.jsh")
    assert "全部通过" in out.getvalue(), out.getvalue()


def test_产品版也能跑这个包(tmp_path):
    """🔴 **M145 之后**：本地包在**编译期展开**，产品版（`jishi-rs`）也能导入并跑。

    📌 这条原来钉的是**反面** ——「`jishi-rs` 能装不能导入，修好了这条就红」。
    M145 把它修好后，它按设计红了（⚠️ 那次回归**漏跑了这个文件**，红了一整轮
    才被发现 —— 教训：`-k` 挑测试时别按「我觉得相关的」挑，按**改动的语义**挑）。
    现在换成正向判据：两个宿主跑同一段程序，**输出逐字一致**。
    """
    import shutil
    import subprocess

    from conftest import rust_exe_path

    rs = rust_exe_path()
    if not rs.exists():
        pytest.skip("没有编出 jishi-rs")

    工程 = tmp_path / "工程"
    (工程 / ".jishi" / "packages").mkdir(parents=True)
    shutil.copytree(包目录, 工程 / ".jishi" / "packages" / "图片格式转换")
    (工程 / "用.jsh").write_text(
        "导入 图片格式转换 从 本地包\n"
        "打印(图片格式转换.支持格式())\n"
        "令 图 = 图片格式转换.空图(2, 2, [1, 2, 3])\n"
        "打印(图片格式转换.取像素(图, 1, 1))\n",
        encoding="utf-8", newline="\n")

    py = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", "用.jsh"],
                        cwd=str(工程), capture_output=True, text=True, encoding="utf-8")
    jr = subprocess.run([str(rs), "用.jsh"], cwd=str(工程),
                        capture_output=True, text=True, encoding="utf-8")
    assert py.returncode == 0, (py.stdout or "") + (py.stderr or "")
    assert jr.returncode == 0, (jr.stdout or "") + (jr.stderr or "")
    assert jr.stdout == py.stdout, f"两宿主不一致\n{py.stdout!r}\n{jr.stdout!r}"
    assert "bmp" in jr.stdout.lower(), jr.stdout
