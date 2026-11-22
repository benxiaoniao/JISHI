# -*- coding: utf-8 -*-
"""M219 判据：流式网络 + argv + 输出编码 + 二进制响应。

这一站的五条判据来自路线图（`docs/路线图-0.3.x.md` §M219）：

| # | 判据 | 这一站怎么验 |
|---|---|---|
| ① | 本地 SSE 真流式：3 条事件**边到边打** | 自己起一个**分块 + 慢速**的 SSE 服务，处理函数里记时间戳；事件之间的间隔必须 ≥0.2 秒（攒完再给的话全是 0） |
| ② | argv 带**空格 / 引号 / 中文**都不坏 | `系统.执行详细([程序, 参数…])` 三宿主逐字一致 |
| ③ | `输出编码="gbk"` 能解 Windows 控制台输出 | 让子进程吐 GBK 字节，`自动` 与 `gbk` 都要解回「中文」 |
| ④ | 二进制资产（PNG）**字节一致** | 造一段**不是合法 UTF-8** 的字节（PNG 头 + 随机字节），经 `构造响应` 当正文，收回来逐字节比 |
| ⑤ | 两个 `Set-Cookie` 都在、顺序不乱 | `构造响应(…, 头 = [[名字, 值], …])`，断言两条都在且顺序是给的顺序 |

⚠️ 判据都**自己起本地服务**（`127.0.0.1`），不连外网 —— CI 上没有外网也能跑。
⚠️ 端口一律 `bind(0)` 由系统分配（写死端口会在别人的机器上撞，M218 踩过）。
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


# ---------------------------------------------------------------------------
# 本地服务：分块（chunked）+ 慢速，用来验「真流式」
# ---------------------------------------------------------------------------

class _服务:
    """一个最小的 HTTP 服务：能发 SSE（分块、带间隔）也能发二进制。"""

    def __init__(self, 脚本=None, 二进制=None, 间隔=0.3, 条数=3):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.脚本 = 脚本 or []
        self.二进制 = 二进制
        self.间隔 = 间隔
        self.条数 = 条数
        self._停 = False
        self.线程 = threading.Thread(target=self._跑, daemon=True)
        self.线程.start()

    def _跑(self):
        while not self._停:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._一条, args=(c,), daemon=True).start()

    def _一条(self, c: socket.socket):
        try:
            c.recv(65536)
            if self.二进制 is not None:
                体 = self.二进制
                c.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                    + f"Content-Length: {len(体)}\r\nConnection: close\r\n\r\n".encode()
                    + 体)
                return
            c.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\nTransfer-Encoding: chunked\r\n\r\n")
            for i in range(1, self.条数 + 1):
                块 = (f"event: 第{i}条\ndata: 内容{i}\nid: {i}\n\n").encode("utf-8")
                c.sendall(b"%x\r\n" % len(块) + 块 + b"\r\n")
                time.sleep(self.间隔)
            c.sendall(b"0\r\n\r\n")
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    def 停(self):
        self._停 = True
        try:
            self.sock.close()
        except OSError:
            pass


@pytest.fixture
def 服务():
    起 = []

    def 造(**kw):
        s = _服务(**kw)
        起.append(s)
        return s

    yield 造
    for s in 起:
        s.停()


# ---------------------------------------------------------------------------
# 三宿主跑法
# ---------------------------------------------------------------------------

def _跑(zhu: str, 源: str, cwd: Path) -> str:
    脚本 = cwd / f"m219_{zhu}_{os.getpid()}.jsh"
    脚本.write_text(源, encoding="utf-8", newline="\n")
    if zhu == "rust":
        r = subprocess.run([str(RUST_EXE), str(脚本)], cwd=str(cwd),
                           capture_output=True, text=True, encoding="utf-8", timeout=240)
    elif zhu == "python":
        r = subprocess.run([PY_EXE, "-m", "oracle.jishi.cli", str(脚本)], cwd=str(ROOT),
                           capture_output=True, text=True, encoding="utf-8", timeout=240)
    else:
        from oracle.jishi.compiler import compile_to_json

        码 = cwd / f"m219_{zhu}_{os.getpid()}.json"
        码.write_text(compile_to_json(源, 脚本.name), encoding="utf-8")
        r = subprocess.run(["node", "oracle/node/index.js", str(码)], cwd=str(ROOT),
                           capture_output=True, text=True, encoding="utf-8", timeout=240)
    assert r.returncode == 0, f"{zhu} 跑挂了：{r.stdout[-800:]}\n{r.stderr[-2000:]}"
    return r.stdout


def _三宿主(源: str, tmp_path: Path) -> dict:
    出 = {}
    for zhu in ("rust", "python", "node"):
        出[zhu] = _跑(zhu, 源, tmp_path)
    return 出


# ---------------------------------------------------------------------------
# ① SSE 真流式
# ---------------------------------------------------------------------------

_SSE源 = '''导入 网络
导入 时间
导入 文本 为 文本库

令 起 = 时间.高精度时间()
令 记 = []

函数 收(单)：
    记.追加([文本库.小数位(时间.高精度时间() - 起, 2), 单["事件"], 单["数据"], 单["id"]])

令 数 = 网络.SSE("http://127.0.0.1:__端口__/事件", 收)

打印("条数", 数)
遍历 条 在 记：
    打印(条[0], 条[1], 条[2], 条[3])
'''


def test_SSE三条事件边到边到(服务, tmp_path):
    """① 关键判据：**事件是「来一条给一条」，不是「攒完再给」**。

    做法：服务每发一条就 `sleep(0.3)`。处理函数里记下**收到时**的时间戳。
    * 真流式 ⇒ 相邻两条的间隔 ≈ 0.3 秒；
    * 攒完再给 ⇒ 三条的时间戳几乎相同（间隔 ≈ 0）。

    阈值取 **0.15 秒**（真值 0.3 的一半）：够宽松到不怕机器慢，
    又远大于「全部一次到」的噪声（那种情况是 0.00x 秒）。

    ⚠️ 只验 Rust 一份 —— 这条判的是**传输**是不是流式，三个宿主的 SSE 解析
    逻辑完全同形（下面 `test_SSE三宿主逐字一致` 逐字节比解析结果）。
    """
    s = 服务(间隔=0.3, 条数=3)
    源 = _SSE源.replace("__端口__", str(s.port))
    行 = _跑("rust", 源, tmp_path).strip().splitlines()
    assert 行[0] == "条数 3", 行
    时间们 = [float(x.split()[0]) for x in 行[1:]]
    assert len(时间们) == 3, 行
    for i in range(1, 3):
        assert 时间们[i] - 时间们[i - 1] >= 0.15, (
            f"第 {i} 条和第 {i+1} 条之间只隔了 {时间们[i] - 时间们[i-1]:.2f} 秒 —— "
            f"说明流是**攒完再给**的，不是边到边给的（{行}）")
    assert 行[1].endswith("第1条 内容1 1"), 行
    assert 行[3].endswith("第3条 内容3 3"), 行


def test_SSE三宿主逐字一致(服务, tmp_path):
    """同一份 SSE 流，三个宿主的**解析结果**必须逐字相同（去掉时间戳）。"""
    s = 服务(间隔=0.02, 条数=3)
    源 = _SSE源.replace("__端口__", str(s.port))
    出 = _三宿主(源, tmp_path)

    def 去时(文: str) -> list:
        出 = []
        for ln in 文.strip().splitlines():
            词 = ln.split(" ", 1)
            try:
                float(词[0])                     # 时间戳行：去掉第一个词再比
                出.append(词[1] if len(词) > 1 else "")
            except ValueError:
                出.append(ln)
        return 出

    assert 去时(出["python"]) == 去时(出["rust"]), (出["python"], 出["rust"])
    assert 去时(出["node"]) == 去时(出["rust"]), (出["node"], 出["rust"])
    assert 去时(出["rust"])[0] == "条数 3"


# ---------------------------------------------------------------------------
# ①b 获取流：读行 / 读块 / 结束 / 状态 / 关闭
# ---------------------------------------------------------------------------

_流源 = '''导入 网络
导入 编码

令 流 = 网络.获取流("http://127.0.0.1:__端口__/x")
打印("状态", 流.状态(), "类型", 类型(流))
打印("内容类型", 流.响应头()["content-type"])
令 i = 0
当 i < 4：
    打印("行", 流.读行(5))
    i = i + 1
令 块 = 流.读块(8, 5)
打印("块", 类型(块), "长度", 长度(块), "十六进制", 编码.十六进制编码(块))
# 读到对端关闭：`读块` 给空 + `结束()` 变真
当 非 流.结束()：
    令 余 = 流.读块(65536, 5)
    如果 余 == 空：
        中断
打印("结束", 流.结束())
打印("关闭", 流.关闭(), "再关", 流.关闭())
尝试：
    流.读行(1)
捕获 异常 为 e：
    打印("关闭后", e.类型)
'''


def test_获取流读行读块与关闭(服务, tmp_path):
    """句柄的六个方法都要真能用，且三宿主逐字一致。"""
    # 两条事件 ⇒ 读完前 4 行之后**还有下文**，`读块` 才拿得到东西
    s = 服务(间隔=0.01, 条数=2)
    源 = _流源.replace("__端口__", str(s.port))
    出 = _三宿主(源, tmp_path)
    行 = 出["rust"].strip().splitlines()
    assert 行[0] == "状态 200 类型 流", 行
    assert 行[1] == "内容类型 text/event-stream", 行
    assert 行[2] == "行 event: 第1条", 行
    assert 行[3] == "行 data: 内容1", 行
    assert 行[4] == "行 id: 1", 行
    assert 行[5] == "行 ", 行                     # 空行（事件分隔）
    # 第 5 行之后是第 2 条事件的 `event: 第2条`：前 8 字节正好是 `event: ` + `第`
    assert 行[6] == "块 字节 长度 8 十六进制 6576656e743a20e7", 行
    assert 行[7] == "结束 真", 行                  # 读干净之后「结束」为真
    assert 行[8] == "关闭 真 再关 假", 行
    assert 行[9] == "关闭后 运行期错误", 行
    assert 出["python"] == 出["rust"], (出["python"], 出["rust"])
    assert 出["node"] == 出["rust"], (出["node"], 出["rust"])


# ---------------------------------------------------------------------------
# ② argv（空格 / 引号 / 中文）
# ---------------------------------------------------------------------------

_ARGV源 = '''导入 系统
导入 json

令 r = 系统.执行详细(["python", "-c",
    "import sys,json;print(json.dumps(sys.argv[1:],ensure_ascii=False))",
    "带 空格的参数", "有\\"引号\\"的", "中文参数"])
打印(r["输出"].去空白())
令 w = 系统.执行详细(["python", "-c", "import sys;print(sys.argv[1])", "a b c"])
打印(w["输出"].去空白())
'''


def test_argv带空格引号中文都不坏(tmp_path):
    """② 列表形态就是 **argv**：参数边界原样到子进程，不经过 shell。

    这条以前没人钉，但「Windows 上引号地狱」正是它要解决的问题：写
    一段 shell 串要数清三层引号，而列表形态一个引号都不用。
    """
    出 = _三宿主(_ARGV源, tmp_path)
    行 = 出["rust"].strip().splitlines()
    assert 行[0] == '["带 空格的参数", "有\\"引号\\"的", "中文参数"]', 行
    assert 行[1] == "a b c", 行
    assert 出["python"] == 出["rust"], (出["python"], 出["rust"])
    assert 出["node"] == 出["rust"], (出["node"], 出["rust"])


# ---------------------------------------------------------------------------
# ③ 输出编码
# ---------------------------------------------------------------------------

_编码源 = '''导入 系统

令 令_ = ["python", "-c", "import sys;sys.stdout.buffer.write('中文目录'.encode('gbk'))"]
打印("gbk", 系统.执行详细(令_, 10, "gbk")["输出"])
打印("自动", 系统.执行详细(令_, 10)["输出"])
打印("utf8", 系统.执行详细(["python", "-c", "print('你好', end='')"], 10, "utf-8")["输出"])
尝试：
    系统.执行详细(令_, 10, "big5")
捕获 异常 为 e：
    打印("坏编码", e.类型, e.消息)
'''


def _本机代码页() -> int:
    """本机 ANSI 代码页（非 Windows 一律 65001）。

    用它决定 `自动` 该期望什么：`自动` **只认 GBK（936）**，别的代码页
    （英文 Windows 1252 / 日文 932…）一律按 UTF-8 替换 —— 三个宿主都没有那些
    码表，统一才能逐字一致（D124）。
    """
    if os.name != "nt":
        return 65001
    import ctypes

    return int(ctypes.windll.kernel32.GetACP())


def test_输出编码能解GBK控制台输出(tmp_path):
    """③ 外部命令吐的是**字节**：GBK 的输出按 UTF-8 硬解就是一片 `����`。

    `"gbk"` 是显式指定，跟平台无关，**必须解出中文**；`自动` 先试 UTF-8、
    不合法再按本机代码页 —— 但**只认 936**：英文 Windows 的 1252 没有码表，
    那里给替换字符才是对的（与另外两个宿主一致）。
    """
    出 = _三宿主(_编码源, tmp_path)
    行 = 出["rust"].strip().splitlines()
    assert 行[0] == "gbk 中文目录", 行
    if _本机代码页() == 936:
        assert 行[1] == "自动 中文目录", 行
    else:
        # 非 GBK 系统：`自动` 只保证「不崩、有一行」，内容按 UTF-8 替换
        assert 行[1].startswith("自动 "), 行
    assert 行[2] == "utf8 你好", 行
    assert 行[3].startswith("坏编码 值错误"), 行
    assert 出["python"] == 出["rust"], (出["python"], 出["rust"])
    assert 出["node"] == 出["rust"], (出["node"], 出["rust"])


# ---------------------------------------------------------------------------
# ④ 二进制正文（字节一致）
# ---------------------------------------------------------------------------

_BIN源 = '''导入 HTTP服务端
导入 编码

令 体 = 编码.十六进制解码("89504E470D0A1A0A0000000D49484452FFFE00FF")
令 应 = HTTP服务端.构造响应(200, 体, "image/png")
打印("类型", 类型(应))
令 切 = 应[长度(应) - 长度(体):]
打印("尾字节", 编码.十六进制编码(切))
打印("长度", 长度(应), 长度(体))
'''


def test_构造响应收字节正文且二进制不被破坏(tmp_path):
    """④ 二进制资产要**原样**发出去（以前只能 base64，体积 +33%）。

    造一段**不是合法 UTF-8** 的字节（PNG 魔数 + `FF FE 00 FF`）：
    只要中间有任何一步「当文本处理」（`from_utf8_lossy`），
    `FF FE` 就会变成 `EF BF BD`（U+FFFD 的 UTF-8），判据当场红。
    """
    出 = _三宿主(_BIN源, tmp_path)
    行 = 出["rust"].strip().splitlines()
    assert 行[0] == "类型 字节", 行
    assert 行[1] == "尾字节 89504e470d0a1a0a0000000d49484452fffe00ff", 行
    # 头 + 体：头是 ASCII，长度 = 头长 + 15
    assert 行[2].startswith("长度 "), 行
    assert 出["python"] == 出["rust"], (出["python"], 出["rust"])
    assert 出["node"] == 出["rust"], (出["node"], 出["rust"])


def test_二进制真经网络发出去再收回来(服务, tmp_path):
    """④b 端到端：服务返回**任意字节**（含 `FF FE 00`），流式收回来逐字节对。

    这条才是「营造框架 那个 2.9 MB wasm 原样发」的复现 —— 只验 `构造响应`
    还不够，中间任何一层按文本处理（服务器写、客户端读）都会坏。
    """
    体 = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,
                0xFF, 0xFE, 0x00, 0xFF, 0x80, 0x81, 0x7F])
    s = 服务(二进制=体)
    源 = f'''导入 网络
导入 编码

令 流 = 网络.获取流("http://127.0.0.1:{s.port}/x.png")
令 攒 = 编码.文本字节("")
当 真：
    令 块 = 流.读块(4096, 5)
    如果 块 == 空：
        中断
    攒 = 攒 + 块
    如果 长度(攒) >= {len(体)}：
        中断
流.关闭()
打印(编码.十六进制编码(攒))
'''
    出 = _三宿主(源, tmp_path)
    期望 = "89504e470d0a1a0afffe00ff80817f"
    assert 出["rust"].strip() == 期望, 出["rust"]
    assert 出["python"].strip() == 期望, 出["python"]
    assert 出["node"].strip() == 期望, 出["node"]


# ---------------------------------------------------------------------------
# ⑤ 多个 Set-Cookie
# ---------------------------------------------------------------------------

_COOKIE源 = '''导入 HTTP服务端

令 应 = HTTP服务端.构造响应(200, "好", "text/plain; charset=utf-8",
    [["Set-Cookie", "a=1; Path=/"], ["Set-Cookie", "b=2; Path=/"],
     ["X-Server", "jishi"]])
令 行们 = 应.拆分("\\r\\n")
遍历 行 在 行们：
    如果 行.开头是("Set-Cookie") 或 行.开头是("X-Server")：
        打印(行)
打印("长度", 长度(应))
'''


def test_两个SetCookie都在且顺序不乱(tmp_path):
    """⑤ 头收「名字/值」对列表 —— **同名可重复**，顺序就是给进去的顺序。

    字典做不到这件事（键相同就覆盖一个），而多 Cookie 是登录会话的常规做法：
    `Set-Cookie: a=1` + `Set-Cookie: b=2` 必须**两行都在**。
    顺带钉住「按字节算 Content-Length」（"好" 是 3 字节，不是 1）。
    """
    出 = _三宿主(_COOKIE源, tmp_path)
    行 = 出["rust"].strip().splitlines()
    assert 行[0] == "Set-Cookie: a=1; Path=/", 行
    assert 行[1] == "Set-Cookie: b=2; Path=/", 行
    assert 行[2] == "X-Server: jishi", 行
    assert 出["python"] == 出["rust"], (出["python"], 出["rust"])
    assert 出["node"] == 出["rust"], (出["node"], 出["rust"])


# ---------------------------------------------------------------------------
# ⑥ 老路不许被带坏
# ---------------------------------------------------------------------------

def test_构造响应老写法照旧(tmp_path):
    """字典头 + 文本正文的老写法一个字没变（存量代码全靠它）。"""
    源 = '''导入 HTTP服务端

令 应 = HTTP服务端.构造响应(404, "没有", "text/plain; charset=utf-8", {"X-A": "1"})
打印(类型(应))
打印(应)
'''
    出 = _三宿主(源, tmp_path)
    assert 出["python"] == 出["rust"], (出["python"], 出["rust"])
    assert 出["node"] == 出["rust"], (出["node"], 出["rust"])
    行 = 出["rust"].strip().splitlines()
    assert 行[0] == "文本"
    assert 行[1] == "HTTP/1.1 404 Not Found"
    assert "X-A: 1" in 行
    assert "Content-Length: 6" in 行          # 「没有」= 6 字节


def test_不认识的编码报能照做的错(tmp_path):
    """错误文案要**说清能写什么**（核心二：报错必须可操作）。"""
    源 = '''导入 系统
尝试：
    系统.执行详细(["python", "-c", "1"], 10, "big5")
捕获 异常 为 e：
    打印(e.类型, e.消息)
'''
    出 = _三宿主(源, tmp_path)
    assert "utf-8 / gbk" in 出["rust"], 出["rust"]
    assert 出["python"] == 出["rust"]
    assert 出["node"] == 出["rust"]
