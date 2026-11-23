# -*- coding: utf-8 -*-
"""M220 判据：语言小账与诊断。

| # | 这一站修的 | 判据 |
|---|---|---|
| ① | `--ai-card --统计` 只出尺寸表（Rust 侧以前打印整张卡） | 两宿主输出逐字一致、且每行都是「名字<TAB>数字」 |
| ② | 裸 `抛出` = 重抛当前异常（类型/消息/起因链都保住） | 四执行器逐字一致 + 「捕获之外裸抛」报**同一条**运行期错误 |
| ③ | `窗口.打开` 的宽高收浮点（内部取整） | 三个宿主都**接受** `640.5`，走到同一句后续错误 |
| ⑥ | `是实例(x, "字典")` 收类型名文本 | 内建类型名全表可认；错字给**值错误**（不静默给假） |
| ⑦ | `写字节` 收 0–255 整数列表；`不` 在条件位置给定向提示 | 三宿主逐字一致；越界逐项点名 |
| ⑧ | 包内**语法错**报包里的文件与行（错误码 E0204，不是 E0302） | 最小包：两宿主渲染逐字节相同 |
| ⑨ | `获取详情` 的响应头键**小写 + 排序**（三宿主以前大小写/顺序都不同） | 三宿主逐字一致 |
| ⑩ | 语言卡补：三元 / `不` / `匹配` / 包隔离 / 变量别跟模块同名 | `audit_ai_card` + 卡里逐条在 |

⚠️ 这里**不**重复 M213b 已经钉住的东西（W3/W4/W21/W17 那四条：赋值目标里的名字
收集、函数值 `==`）—— 它们在 `test_m213b_patch.py` 与语料 `52_函数相等` 里。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUST_EXE = ROOT / "rust" / "target" / "release" / ("jishi-rs.exe" if os.name == "nt" else "jishi-rs")
PY_EXE = sys.executable

pytestmark = pytest.mark.skipif(
    not RUST_EXE.is_file(),
    reason="没有发行版二进制（先 cargo build --release）",
)


def _跑(zhu: str, 源: str, cwd: Path) -> str:
    脚本 = cwd / f"m220_{zhu}_{os.getpid()}.jsh"
    脚本.write_text(源, encoding="utf-8", newline="\n")
    if zhu == "rust":
        r = subprocess.run([str(RUST_EXE), str(脚本)], cwd=str(cwd),
                           capture_output=True, text=True, encoding="utf-8", timeout=240)
    elif zhu == "python":
        r = subprocess.run([PY_EXE, "-m", "oracle.jishi.cli", str(脚本)], cwd=str(ROOT),
                           capture_output=True, text=True, encoding="utf-8", timeout=240)
    elif zhu == "cvm":
        r = subprocess.run([PY_EXE, "-m", "oracle.jishi.cli", str(脚本)], cwd=str(ROOT),
                           capture_output=True, text=True, encoding="utf-8", timeout=240,
                           env={**os.environ, "JISHI_ENGINE": "cvm"})
    else:
        raise AssertionError(f"没有这个宿主：{zhu}")
    assert r.returncode == 0, f"{zhu} 跑挂了：{r.stdout[-800:]}\n{r.stderr[-2000:]}"
    return r.stdout


# ---------------------------------------------------------------------------
# ① --ai-card --统计
# ---------------------------------------------------------------------------

def test_统计只出尺寸表且两宿主一致():
    """① `--ai-card --统计` 要**只**打尺寸表。

    Rust 侧以前**漏了这条分支**，照旧打印整张 797 行的卡 —— 与 Python 两个答案。
    """
    py = subprocess.run([PY_EXE, "-m", "oracle.jishi.cli", "--ai-card", "--统计"],
                        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
    rs = subprocess.run([str(RUST_EXE), "--ai-card", "--统计"],
                        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
    assert py.returncode == 0 and rs.returncode == 0, (py.stderr, rs.stderr)
    assert rs.stdout == py.stdout, (rs.stdout[:400], py.stdout[:400])
    行们 = [ln for ln in rs.stdout.strip().splitlines() if ln.strip()]
    assert 行们, "统计输出是空的"
    for 行 in 行们:
        名, _, 数 = 行.partition("\t")
        assert 数.isdigit(), f"这一行不是「名字<TAB>数字」：{行!r}"
    assert 行们[0].startswith("全量\t"), 行们[:3]
    # 整张卡是几百行 —— 只出表就不该有这么长
    assert len(行们) < 40, f"统计输出有 {len(行们)} 行，像是把整张卡打出来了"


# ---------------------------------------------------------------------------
# ② 裸 抛出
# ---------------------------------------------------------------------------

_重抛源 = '''函数 测()：
    尝试：
        令 x = [1][5]
    捕获 异常 为 e：
        打印("内层", e.类型)
        抛出

尝试：
    测()
捕获 异常 为 e：
    打印("外层", e.类型, e.消息)

尝试：
    抛出
捕获 异常 为 e：
    打印("裸抛", e.类型)
'''


def test_裸抛出重抛当前异常(tmp_path):
    """② 裸 `抛出` = 重抛当前异常：**类型与消息都保住**（不是包成「异常」）。

    Python 的 `raise` 就是这样；模板里「捕获了、记一笔、再抛出去」是常规写法，
    以前基石必须 `捕获 异常 为 e:` 才能 `抛出 e` —— 不绑定就没法重抛。
    """
    期望 = [
        "内层 索引错误",
        "外层 索引错误 列表下标越界",
        "裸抛 运行期错误",
    ]
    for zhu in ("rust", "python", "cvm"):
        出 = _跑(zhu, _重抛源, tmp_path).strip().splitlines()
        assert 出 == 期望, (zhu, 出)


def test_裸抛出没有可重抛的异常时说清楚(tmp_path):
    """②b 捕获之外裸 `抛出` ⇒ **运行期错误**，消息里说清该写在哪、该怎么写。

    四个执行器逐字一致（C VM 为此加了一条 `raise_runtime` 宿主回调 ——
    以前只有「内部错误」那一档，那是给宿主自己的 bug 用的，混用会让人以为语言坏了）。
    """
    源 = '''尝试：
    抛出
捕获 异常 为 e：
    打印(e.类型, e.消息)
'''
    出 = []
    for zhu in ("rust", "python", "cvm"):
        行 = _跑(zhu, 源, tmp_path).strip()
        出.append(行)
    assert len(set(出)) == 1, 出
    assert 出[0].startswith("运行期错误 没有正在处理的异常可以重抛"), 出[0]
    assert "捕获" in 出[0] and "最终" in 出[0], 出[0]


# ---------------------------------------------------------------------------
# ③ 窗口.打开 的宽高
# ---------------------------------------------------------------------------

def test_窗口打开收浮点宽高(tmp_path):
    """③ `宽` / `高` 给小数字面量 ⇒ **接受**（内部四舍五入），不是类型错误。

    比例换算（`宽 = 屏宽 * 0.8`）出来一定是小数，而窗口尺寸的物理单位是整像素。
    三个宿主都该走到**同一句**后续错误（这里故意不给 `页面`/`服务`）。
    """
    源 = '''导入 窗口
尝试：
    窗口.打开({"标题": "t", "宽": 640.5, "高": 480.2})
    打印("开了")
捕获 异常 为 e：
    打印(e.类型, e.消息)
'''
    出 = {}
    for zhu in ("rust", "python"):
        出[zhu] = _跑(zhu, 源, tmp_path).strip()
    assert 出["rust"] == 出["python"], (出["rust"], 出["python"])
    assert "要指定「页面」或「服务」" in 出["rust"], 出["rust"]


# ---------------------------------------------------------------------------
# ⑥ 是实例 收类型名
# ---------------------------------------------------------------------------

_是实例源 = '''打印(是实例({}, "字典"), 是实例([], "列表"), 是实例("a", "文本"), 是实例(1, "文本"))
令 名们 = ["空", "布尔", "整数", "小数", "文本", "字节", "精确小数", "datetime",
            "表格", "切片", "原样片段", "列表", "字典", "集合", "文件", "函数",
            "方法", "内建函数", "模块", "异常类型", "异常", "内建方法", "其他"]
令 认 = 0
遍历 名 在 名们：
    令 r = 是实例(1, 名)
    如果 类型(r) == "布尔":
        认 = 认 + 1
打印("认得的类型名", 认)
尝试：
    是实例({}, "字典字")
捕获 异常 为 e：
    打印("错字", e.类型)
尝试：
    是实例({}, 1)
捕获 异常 为 e：
    打印("非类", e.类型)
'''


def test_是实例收类型名文本(tmp_path):
    """⑥ `是实例(x, "字典")` —— 名字加引号就按 `类型(x)` 比；**错字要报错**。

    为什么不静默给假：`是实例(x, "字典字")` 永远为假，用户会以为是「类型判断
    写错了」，而真正的原因是打错字 —— 「不报错的错答案」最费时间。
    内建类型名那张表三个宿主逐字一致（少一个就有人写不出判断）。
    """
    期望 = [
        "真 真 真 假",
        "认得的类型名 23",
        "错字 值错误",
        "非类 类型错误",
    ]
    for zhu in ("rust", "python", "cvm"):
        出 = _跑(zhu, _是实例源, tmp_path).strip().splitlines()
        assert 出 == 期望, (zhu, 出)


# ---------------------------------------------------------------------------
# ⑦ 写字节 收整数列表 / `不` 的定向提示
# ---------------------------------------------------------------------------

def test_写字节收零到255的整数列表(tmp_path):
    """⑦ `文件.写字节(路径, [1, 2, 255])` —— 以前只认真「字节」，得绕一圈编码。

    越界/非整数要**逐项点名**（第几项、值是什么）：静默截断成 `&0xFF`
    是最难查的那类错。
    """
    路径 = (tmp_path / f"wb_{os.getpid()}.bin").as_posix()
    源 = f'''导入 文件
导入 编码
令 p = "{路径}"
文件.写字节(p, [1, 2, 255])
文件.追加字节(p, [0, 128])
打印("字节", 编码.十六进制编码(文件.读字节(p)))
尝试：
    文件.写字节(p, [1, 300])
捕获 异常 为 e：
    打印("越界", e.类型, e.消息)
尝试：
    文件.写字节(p, "abc")
捕获 异常 为 e：
    打印("文本", e.类型, e.消息)
文件.写字节(p, [])
打印("空", 长度(文件.读字节(p)))
'''
    出 = {}
    for zhu in ("rust", "python", "cvm"):
        出[zhu] = _跑(zhu, 源, tmp_path).strip().splitlines()
    assert 出["rust"] == [
        "字节 0102ff0080",
        "越界 文件错误 「写字节」的列表里第 2 项不是 0–255 的整数：300",
        "文本 文件错误 「写字节」要传字节或 0–255 的整数列表，得到的是「文本」；文本请用 `写文本`",
        "空 0",
    ], 出["rust"]
    for zhu in ("python", "cvm"):
        assert 出[zhu] == 出["rust"], (zhu, 出[zhu])


def test_不在条件位置给定向提示(tmp_path):
    """⑦b `如果 不 成立：` ⇒ 报「少了冒号」时**点名「不」**并给出正确写法。

    `营造框架` W9 的原话是「报错指到冒号，不点名」—— 1000 行文件里找一轮。
    ⚠️ **不新增关键字**（语法面冻结，M36）：只在报错里指路。
    """
    源 = "如果 不 1 == 2：\n    打印(\"不该\")\n"
    脚本 = tmp_path / f"not_{os.getpid()}.jsh"
    脚本.write_text(源, encoding="utf-8", newline="\n")
    for 命令 in ([str(RUST_EXE), str(脚本)],
                 [PY_EXE, "-m", "oracle.jishi.cli", str(脚本)]):
        r = subprocess.run(命令, cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", timeout=120)
        assert r.returncode != 0
        全 = r.stdout + r.stderr
        assert "E0203" in 全, 全
        assert "「不」不是关键字" in 全, 全
        assert "如果 非 甲" in 全, 全


# ---------------------------------------------------------------------------
# ⑧ 包内语法错
# ---------------------------------------------------------------------------

def test_包内语法错指到包里的文件和行(tmp_path):
    """⑧ 包里的 `.jsh` 有语法错 ⇒ 报**那个文件**的第几行，错误码是 `E0204`。

    以前 Rust 侧把它包成「找不到这个函数」（E0302）并指到 `导入` 那一行 ——
    错误码错了（E0204 的修法提示全丢），位置也指到了用户没写错的地方。
    """
    包 = tmp_path / ".jishi" / "packages" / "坏包"
    包.mkdir(parents=True)
    (包 / "包.json").write_text('{"名字": "坏包", "版本": "1.0.0"}', encoding="utf-8")
    (包 / "main.jsh").write_text(
        "函数 好()：\n    返回 1\n\n函数 坏(\n    返回 2\n", encoding="utf-8")
    主 = tmp_path / f"主_{os.getpid()}.jsh"
    主.write_text("导入 坏包 从 本地包\n打印(坏包.好())\n", encoding="utf-8")

    出 = {}
    for zhu, 命令 in (
        ("rust", [str(RUST_EXE), str(主)]),
        ("python", [PY_EXE, "-m", "oracle.jishi.cli", str(主)]),
    ):
        r = subprocess.run(命令, cwd=str(tmp_path), capture_output=True, text=True,
                           encoding="utf-8", timeout=120)
        assert r.returncode != 0, f"{zhu} 竟然跑成功了"
        出[zhu] = (r.stdout or "") + (r.stderr or "")
    # 两宿主**逐字相同**（含路径、行列、提示）
    assert 出["rust"] == 出["python"], (出["rust"], 出["python"])
    assert "E0204" in 出["rust"], 出["rust"][:400]
    assert "main.jsh:5:5" in 出["rust"].replace("\\", "/"), 出["rust"][:400]
    assert "E0302" not in 出["rust"], "语法错不该被包成「找不到这个函数」"


# ---------------------------------------------------------------------------
# ⑨ 获取详情 的响应头
# ---------------------------------------------------------------------------

class _服务:
    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._停 = False
        threading.Thread(target=self._跑, daemon=True).start()

    def _跑(self):
        while not self._停:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._一条, args=(c,), daemon=True).start()

    def _一条(self, c):
        try:
            c.recv(65536)
            # 故意用**三种大小写**、并且顺序不是字母序
            c.sendall(b"HTTP/1.1 200 OK\r\nX-Zeta: 3\r\nContent-Type: text/plain\r\n"
                      b"X-Alpha: 1\r\nContent-Length: 2\r\nConnection: close\r\n\r\nhi")
        finally:
            c.close()

    def 停(self):
        self._停 = True
        try:
            self.sock.close()
        except OSError:
            pass


@pytest.fixture
def 服务():
    s = _服务()
    yield s
    s.停()


def test_获取详情的响应头小写且排序(服务, tmp_path):
    """⑨ `响应头` 的键**小写 + 按键排序**，三宿主逐字一致。

    以前三个宿主三个样：Rust 保留原始大小写、Python 也是、Node 全小写；
    顺序还各按自家网络的到达顺序 —— `打印(响应头)` 是三行不同的字。
    头顺序在 HTTP 里没有语义（`Set-Cookie` 已在 `头表` 里按 `, ` 合并）。
    """
    源 = f'''导入 网络
令 d = 网络.获取详情("http://127.0.0.1:{服务.port}/x")
打印(d["状态"])
打印(d["响应头"])
'''
    for zhu in ("rust", "python"):
        出 = _跑(zhu, 源, tmp_path).strip().splitlines()
        assert 出[0] == "200", (zhu, 出)
        assert 出[1].startswith("{'connection'"), (zhu, 出[1])
        # 键必须小写、并且按字母序
        键们 = [段.split("'")[1] for 段 in 出[1].strip("{}").split(", ") if "'" in 段]
        assert 键们 == sorted(键们), (zhu, 键们)
        assert all(k == k.lower() for k in 键们), (zhu, 键们)


# ---------------------------------------------------------------------------
# ⑩ 语言卡
# ---------------------------------------------------------------------------

def test_语言卡补了这几条():
    """⑩ 卡里必须写清这五件（模型照着卡写代码，卡漏了就等于没有）：

    - 三元 `A 如果 条件 否则 B`
    - `不` 不是逻辑非（写 `非`）
    - `匹配` 是关键字、不能当函数名
    - 本地包是隔离命名空间（要互相用就各自 `导入`）
    - 变量 / 参数别跟模块同名（局部遮蔽模块）
    """
    r = subprocess.run([str(RUST_EXE), "--ai-card"], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0
    卡 = r.stdout
    for 要 in ("如果 分数 >= 60 否则", "「不」不是逻辑非", "`匹配`",
               "隔离命名空间", "别跟模块同名"):
        assert 要 in 卡, f"卡里没有「{要}」"
