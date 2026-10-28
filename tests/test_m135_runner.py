# -*- coding: utf-8 -*-
"""M135：**用基石自己写的**黑盒测试运行器（`tools/自测.jsh`）的判据。

这个文件盯的**不是「能跑」**，而是「**它是不是一把准的尺子**」——
所以每条都是故意造一个该红／该绿的小用例，看它有没有说对：

| 造的情况 | 它必须说什么 |
|---|---|
| 输出一致 | 通过，退出码 0 |
| 输出不一致 | **点名到第几行** + 期望/实际，退出码 1 |
| 该报错却成功了 | 红（不是「静默通过」） |
| 报错文案只是**包含**就够 | 绿（与 `test_error_snapshot` 同口径） |
| 没有 `.out` / `.err` | **跳过**并点名（不是当通过） |
| `.out` 是 CRLF 而程序输出 LF | 绿（不归一行末 ⇒ 每条都假红） |
| 登记了豁免 | **豁免 N**，且**不算通过** |
| 豁免了但它过了 | 红（豁免过期要清） |
| 豁免没写原因 | 红（没有原因的豁免等于没有） |

还有两条更硬的：

- **三宿主报告必须逐字一致**（除「用时」）：运行器自己是基石程序，
  在 Python / Rust / Node 三个宿主下跑出的报告不能有差别；
- **真语料全量**（`tests/cases` + `tests/error_cases` = 126 条）必须 0 失败 ——
  这条同时钉住「默认参数能直接用」。
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
RUNNER = ROOT / "tools" / "自测.jsh"
RUST = ROOT / "rust" / "target" / "release" / "jishi-rs.exe"
被测 = [PY, "-m", "oracle.jishi.cli"]


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _跑(用例目录, 额外=(), 被测命令=None, 宿主="python", cwd=None, 标签="python"):
    """跑一次运行器，返回 (退出码, stdout, stderr)。

    🔴 **默认带 `--标签 python`**（2026-10-25 CI 红过一次之后加的）：
    标签默认取自**被测程序的文件名** —— Windows 上是 `python.exe`（→ `python`），
    macOS / Linux 上是 `python3.13`（→ `python3.13`）。测试里写死的豁免表按
    `python` 记，于是**本地全绿、CI 全红**。显式给标签，判据才与平台无关。
    """
    命令 = _宿主命令(宿主)
    if 宿主 != "node":
        # node 宿主的「程序」是编译好的字节码文件，已经在上面的命令里了
        命令 += [str(RUNNER)]
    命令 += ["--用例", str(用例目录)]
    if 被测命令 is None:
        被测命令 = 被测
    命令 += ["--被测"] + list(被测命令)
    if 标签 is not None:
        命令 += ["--标签", 标签]
    命令 += list(额外)
    r = subprocess.run(命令, cwd=str(cwd or ROOT), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=300)
    return r.returncode, r.stdout, r.stderr


def _宿主命令(宿主):
    if 宿主 == "python":
        return [PY, "-m", "oracle.jishi.cli"]
    if 宿主 == "rust":
        return [str(RUST)]
    if 宿主 == "node":
        # ⚠️ 字节码落到**系统临时目录 + pid 后缀**：并行时固定名会被别的 worker 覆写
        #    （AGENTS §1 第 3 条那类坑，症状是「单跑必过、并行偶红」）。
        字节 = Path(tempfile.gettempdir()) / f"m135_{os.getpid()}_自测.bc.json"
        r = subprocess.run([PY, "-m", "oracle.jishi.cli", "--dump-bytecode", str(RUNNER)],
                           cwd=str(ROOT), capture_output=True)
        assert r.returncode == 0, r.stderr
        字节.write_bytes(r.stdout)
        return ["node", "oracle/node/index.js", str(字节)]
    raise AssertionError(宿主)


def _写(目录: Path, 名: str, 源: str, 期望=None, 后缀=".out"):
    目录.mkdir(parents=True, exist_ok=True)
    (目录 / f"{名}.jsh").write_text(源, encoding="utf-8", newline="\n")
    if 期望 is not None:
        (目录 / f"{名}{后缀}").write_text(期望, encoding="utf-8", newline="")


def _汇总(out: str) -> str:
    for 行 in out.splitlines():
        if 行.startswith("汇总"):
            return 行
    raise AssertionError(f"报告里没有汇总行：\n{out}")


def _去掉用时(out: str) -> str:
    return "\n".join(
        行.split("· 用时")[0].rstrip() if "· 用时" in 行 else 行
        for 行 in out.splitlines())


# ---------------------------------------------------------------------------
# 判得对不对
# ---------------------------------------------------------------------------

def test_输出一致就通过_退出码0(tmp_path):
    _写(tmp_path, "好", "打印(1 + 1)\n", "2\r\n")   # ⚠️ 故意用 CRLF：仓库里就是 CRLF
    码, 出, 错 = _跑(tmp_path)
    assert 码 == 0, 出 + 错
    assert "通过 1 · 失败 0" in _汇总(出), 出


def test_输出不一致_要点名到第几行_退出码1(tmp_path):
    _写(tmp_path, "差", "打印(1)\n打印(3)\n", "1\n2\n")
    码, 出, 错 = _跑(tmp_path)
    assert 码 == 1, "输出不一致必须退 1"
    assert "✗ 差" in 出
    assert "第 2 行不一样" in 出, 出        # 必须指到具体行，不能只说「不一致」
    assert "期望：2" in 出 and "实际：3" in 出, 出


def test_该报错却成功了_要红(tmp_path):
    _写(tmp_path, "该报错", "打印(1)\n", "错误 E0301：找不到这个名字", 后缀=".err")
    码, 出, 错 = _跑(tmp_path)
    assert 码 == 1
    assert "该报错却成功了" in 出, 出


def test_报错文案是包含_不要求逐字(tmp_path):
    """与 `test_error_snapshot` 同口径：`.err` 只要求**出现在**错误输出里。"""
    _写(tmp_path, "该报错", "打印(不存在这个名字)\n", "找不到这个名字", 后缀=".err")
    码, 出, 错 = _跑(tmp_path)
    assert 码 == 0, 出 + 错
    assert "通过 1 · 失败 0" in _汇总(出), 出


def test_没有期望文件算跳过_不算通过(tmp_path):
    _写(tmp_path, "没期望", "打印(1)\n")
    码, 出, 错 = _跑(tmp_path)
    assert 码 == 0
    汇 = _汇总(出)
    assert "跳过 1" in 汇 and "通过 0" in 汇, 汇
    assert "⏭ 没期望" in 出, 出


def test_期望文件CRLF_程序输出LF_不能假红(tmp_path):
    """`\r\n` → `\n` 的归一是**必须的**：仓库里的 `.out` 全是 CRLF。

    不归一，43 条黄金语料会**整批假红**，而红的原因看起来像「程序输出变了」。
    """
    _写(tmp_path, "混", "打印(\"甲\")\n打印(\"乙\")\n", "甲\r\n乙\r\n")
    码, 出, 错 = _跑(tmp_path)
    assert 码 == 0, 出 + 错


def test_一条用例都没找到_也要红(tmp_path):
    """「0 条 · 通过 0 · 失败 0」看起来完全正常 —— 那是**最坏的假绿**。

    目录写错、语料被挪走都会长成这样。所以它必须红。
    """
    (tmp_path / "空的子目录").mkdir()
    码, 出, 错 = _跑(tmp_path)
    assert 码 == 1, "0 条用例不许当绿"
    assert "一条用例都没找到" in (出 + 错), 出 + 错


@pytest.mark.parametrize("换行", ["\n", "\r\n"])
def test_多行报错快照的换行要和程序吐的一致(tmp_path, 换行):
    """`.err` 里有**多行**快照（错误首行 + `┌─ 文件:行:列` + 源码行 + `^`）。

    🔴 **2026-10-26 被 CI 的 Windows 抓到**：仓库里 `.err` 在 Windows 检出时是 **CRLF**，
    而程序吐 **LF**。运行器原来只把「实际输出」归一、**没归一「期望」** ——
    单行的快照「包含」照样过，**多行的永远对不上**（`42_类外超` / `43_整行插值字符串`）。
    而本地是 LF 检出，所以**永远绿**。

    ⇒ 判据自己造一份「你给什么换行都行」的 `.err`，与检出方式无关：
    两种换行都要能对上（这条在任何平台上都成立，不靠运气）。
    """
    目 = tmp_path / f"快照{换了名字(换行)}"
    目.mkdir()
    (目 / "超.jsh").write_text("超().叫()\n", encoding="utf-8", newline="\n")
    # ⚠️ 多行「包含」要求**每一行都完整对上**（不是逐行前缀匹配）——
    #    第一行写半句就会对不上，所以这里抄的是**完整**首行。
    两行 = 换行.join([
        "错误 E0201：这里出现了意外的内容（「超()」只能在类的方法里用（用它调基类的同名方法））",
        "  ┌─ 超.jsh:1:1",
    ]) + 换行
    (目 / "超.err").write_text(两行, encoding="utf-8", newline="")

    码, 出, 错 = _跑(目)
    assert 码 == 0, f"多行 .err（{换行!r}）没对上：\n{出}{错}"
    assert "失败 0" in 出, 出


def 换了名字(换行: str) -> str:
    return "CRLF" if "\r" in 换行 else "LF"


def test_标签默认取程序文件名_所以跨平台的表必须显式给(tmp_path):
    """🔴 **这条是 CI 红过一次之后补的**（2026-10-25）。

    默认标签 = **被测程序的文件名去掉扩展名**。而「Python 参考实现」在三个系统上
    叫得不一样：Windows `python.exe` → `python`，macOS / Linux `python3.13`
    → `python3.13`。于是「`python | 用例 | 原因`」这种豁免表在本地绿、在 CI 上一片红。

    判据钉两件事：
    1. 默认标签是**从文件名派生的短名字**（不含路径分隔符）—— 不去猜具体值，
       因为它还受**符号链接**影响：macOS 上 `.../bin/python` 是指向 `python3.13`
       的符号链接，运行器会把 `--被测` 绝对化（`realpath`）⇒ 标签变成 `python3`
       （`无后缀名` 只去掉最后一个 `.` 后面那段；**这条就是我在 CI 上踩的第二个坑，
       第一版这里写的是 `Path(sys.executable).stem`，在 macOS 上根本对不上**）；
    2. 给了 `--标签` 就**以给的为准**（这才是跨平台表的正确写法）。
    """
    _写(tmp_path, "好", "打印(1)\n", "1\n")

    码, 出, 错 = _跑(tmp_path, 标签=None)
    assert 码 == 0, 出 + 错
    assert "（标签 " in 出, f"报告里没有标签：\n{出}"
    默认标签 = 出.split("（标签 ")[1].split("）")[0]
    assert 默认标签, f"默认标签是空的：\n{出}"
    assert "/" not in 默认标签 and "\\" not in 默认标签, (
        f"默认标签该是文件名派生出来的短名字，不该带路径：{默认标签!r}")

    码, 出, 错 = _跑(tmp_path, 标签="随便起的名字")
    assert 码 == 0, 出 + 错
    assert "（标签 随便起的名字）" in 出, f"--标签 没生效：\n{出}"


# ---------------------------------------------------------------------------
# 豁免：显式、可复核、会过期
# ---------------------------------------------------------------------------

def test_豁免生效_但不算通过(tmp_path):
    _写(tmp_path, "差的", "打印(3)\n", "2\n")
    表 = tmp_path / "豁免.txt"
    表.write_text("# 注释行\npython | 差的.jsh | 造出来的边界\n", encoding="utf-8")
    码, 出, 错 = _跑(tmp_path, 额外=["--豁免", str(表)])
    汇 = _汇总(出)
    assert 码 == 0, 出 + 错
    assert "豁免 1" in 汇 and "失败 0" in 汇, 汇
    assert "通过 0" in 汇, "豁免**不能**算进通过 —— 那是把边界伪装成绿"
    assert "⚠ 差的" in 出, 出


def test_豁免过期要红(tmp_path):
    """它过了 ⇒ 那行必须在红着喊你删掉，否则会一直遮着这条用例。"""
    _写(tmp_path, "好的", "打印(2)\n", "2\n")
    表 = tmp_path / "豁免.txt"
    表.write_text("python | 好的.jsh | 这条其实没问题\n", encoding="utf-8")
    码, 出, 错 = _跑(tmp_path, 额外=["--豁免", str(表)])
    assert 码 == 1, "豁免过期必须红"
    assert "豁免过期" in 出, 出


def test_豁免表没写原因要报错(tmp_path):
    _写(tmp_path, "好的", "打印(2)\n", "2\n")
    表 = tmp_path / "豁免.txt"
    表.write_text("python | 好的.jsh |\n", encoding="utf-8")
    码, 出, 错 = _跑(tmp_path, 额外=["--豁免", str(表)])
    assert 码 == 1
    assert "没写原因" in (出 + 错), 出 + 错


def test_豁免表格式不对要报错(tmp_path):
    _写(tmp_path, "好的", "打印(2)\n", "2\n")
    表 = tmp_path / "豁免.txt"
    表.write_text("python | 好的.jsh\n", encoding="utf-8")
    码, 出, 错 = _跑(tmp_path, 额外=["--豁免", str(表)])
    assert 码 == 1
    assert "要写成" in (出 + 错), 出 + 错


# ---------------------------------------------------------------------------
# 运行器自己是基石程序 ⇒ 三个宿主必须说一样的话
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not RUST.is_file(), reason="没构建 jishi-rs（cd rust && cargo build --release）")
@pytest.mark.skipif(shutil.which("node") is None, reason="PATH 上没有 node")
def test_三宿主报告逐字一致(tmp_path):
    """同一批用例，运行器在 Python / Rust / Node 三个宿主下跑出来的报告要一样。

    只允许「用时」不同 —— 那个数本来就随机器变。

    ⚠️ 这条抓到过一个真的宿主分叉（2026-10-25）：程序先 `打印` 一整份报告、
    最后 `抛出` 时，**Python 宿主的 stdout 是块缓冲** ⇒ 错误横幅跑到报告前面，
    而 Rust / Node 顺序是正的。修在 `cli.py` 的 `_装好错误流`（代理 stderr，
    写之前先冲 stdout），一处覆盖所有写点。
    """
    _写(tmp_path, "甲", "打印(\"甲\")\n", "甲\r\n")
    _写(tmp_path, "乙", "打印(1 + 1)\n", "2\r\n")

    报告们 = {}
    for 宿主 in ("python", "rust", "node"):
        码, 出, 错 = _跑(tmp_path, 宿主=宿主, 被测命令=[str(RUST)])
        assert 码 == 0, f"{宿主}：{出}{错}"
        报告们[宿主] = _去掉用时(出)

    基 = 报告们["python"]
    for 宿主 in ("rust", "node"):
        assert 报告们[宿主] == 基, (
            f"{宿主} 宿主的报告和 python 不一样（底下一行起是差异）：\n"
            + "\n".join(f"  {a!r} vs {b!r}"
                        for a, b in zip(基.splitlines(), 报告们[宿主].splitlines())
                        if a != b))


# ---------------------------------------------------------------------------
# 真语料：默认参数就能用
# ---------------------------------------------------------------------------

@pytest.mark.计时
def test_真语料全量_默认参数跑通():
    """`jishi tools/自测.jsh` 不带参数，就该跑完 126 条且 0 失败。

    ⚠️ 这条**故意不给 `--用例` / `--被测`**：默认值才是人真正会敲的那条命令，
    默认值写错（目录名、被测定位顺序）只有这条能抓到。

    ⚠️ 打 `计时` 是因为它 CPU 重（126 个子进程）：用 Rust 被测 ~3 秒、
    没构建时退回 Python 参考实现 ~30 秒。留在并行节会制造 CPU 饥荒
    （AGENTS §1 第 3 条那类）。
    """
    被测命令 = [str(RUST)] if RUST.is_file() else 被测
    r = subprocess.run([PY, "-m", "oracle.jishi.cli", str(RUNNER), "--被测"] + 被测命令,
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    assert r.returncode == 0, r.stdout + r.stderr
    汇 = _汇总(r.stdout)
    assert "126 条" in 汇, 汇
    assert "失败 0" in 汇, 汇
    if RUST.is_file():
        # Rust 侧的「从 python 导入」是**宣称过的边界**，已登记在豁免表里。
        # 它如果是 0，说明豁免那行就该删了 —— 这里钉住「确实有且只有 1 条」。
        assert "豁免 1" in 汇, 汇
