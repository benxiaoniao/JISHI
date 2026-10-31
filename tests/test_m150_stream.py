# -*- coding: utf-8 -*-
"""M150 · 输出要**立刻**看得见：脚本模式下 `打印` 不再攒到退出。

## 这条判据在验什么

**时机**，不是内容。内容对不对由 `check_engines`（五引擎逐字节）管 ——
但那个对拍**天生看不见这个问题**：它比的是「最终收到的文本」，不比
「什么时候收到的」。同一个程序，一秒钟出一行和退出时一次吐完，对拍结果
**完全一样**。

真实后果（2026-10-27 一台干净机器上写 860 行计算器时撞的）：
`打印("请输入")` 后面接 `输入()`，用户看到的是一个不动的屏幕，
敲回车也没反应，退出时全冒出来 —— 与死机无法区分。最后只能改成
「外部脚本驱动 + 一次一进程」绕过。

## 判据怎么写

起一个进程 → **往 stdin 写一行但故意不关 stdin**（程序会停在 `输入()` 上）
→ 在 N 秒内从 stdout 收到东西 ⇒ 说明输出真的出去了。
关掉 stdin 再比就没意义了（那时进程本来就该退出）。

## 边界（同样要钉住，别为了「看得见」把别的路弄坏）

- `--json-result` / `--sandbox`：输出要塞进 JSON 的 `stdout` 字段，
  **不能**再直接写到标准输出（否则 stdout 上既有 JSON 又有裸输出，没法解析）；
- DAP：stdio 是协议通道。判据在最后一条（源码级：只允许 CLI 那条路开流式）。
"""

import json
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                       # noqa: E402

#: 交互程序：先打一行、再等输入。**故意不带 `中断`** —— 它要一直停在 `输入()` 上，
#: 我们才有机会在「进程还活着」的时候看输出。
交互程序 = """打印("=== 欢迎 ===")
令 行 = 输入("> ")
打印("你说了：", 行)
"""

C_VM_驱动 = (
    "import sys; sys.path.insert(0, r'{root}');"
    "from oracle.jishi import cvm_bind;"
    "cvm_bind.run_source_c(open('交互.jsh', encoding='utf-8').read(),"
    " filename='交互.jsh')"
)


def _写(tmp_path: Path, 源码: str = 交互程序) -> Path:
    p = tmp_path / "交互.jsh"
    p.write_text(源码, encoding="utf-8", newline="\n")
    return p


def _收(p, 秒: float, 要几行: int) -> list[str]:
    """收 stdout 直到「够几行」或超时。**全程不碰 stdin**。"""
    q: "queue.Queue[bytes]" = queue.Queue()

    def 读() -> None:
        for 行 in p.stdout:
            q.put(行)

    threading.Thread(target=读, daemon=True).start()
    出: list[str] = []
    截止 = 秒
    while len(出) < 要几行 and 截止 > 0:
        try:
            行 = q.get(timeout=截止)
        except queue.Empty:
            break
        出.append(行.decode("utf-8", "replace"))
    return 出


def _跑交互(命令: list, cwd: Path, 秒: float = 20.0) -> list[str]:
    """起进程 → 喂一行（**不关 stdin**）→ 收输出 → 杀掉。"""
    p = subprocess.Popen(命令, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, cwd=str(cwd))
    try:
        p.stdin.write("你好\n".encode("utf-8"))
        p.stdin.flush()
        return _收(p, 秒, 要几行=2)
    finally:
        p.kill()
        p.wait(timeout=10)


def _py(参数: list[str]) -> list[str]:
    return [sys.executable, "-m", "oracle.jishi.cli"] + 参数


def _字节码(tmp_path: Path, 源码: str = 交互程序) -> str:
    from oracle.jishi import serialize
    from oracle.jishi.compiler import compile_source

    (tmp_path / "程序.json").write_text(
        serialize.dumps(compile_source(源码, "程序.jsh")), encoding="utf-8")
    return "程序.json"


def _执行器们(tmp_path: Path) -> dict:
    """五个执行器各自的「启动命令」（都跑 `tmp_path/交互.jsh`）。"""
    表 = {
        "树遍历": lambda: _py(["--tree", "交互.jsh"]),
        "Python VM": lambda: _py(["交互.jsh"]),
        "C VM": lambda: [sys.executable, "-c",
                         C_VM_驱动.format(root=str(ROOT).replace("\\", "\\\\"))],
    }
    rs = rust_exe_path()
    if rs.is_file():
        表["Rust 宿主"] = lambda: [str(rs), "交互.jsh"]
    node = shutil.which("node")
    if node:
        表["Node 宿主"] = lambda: [node, str(ROOT / "oracle" / "node" / "index.js"),
                                   _字节码(tmp_path)]
    return 表


@pytest.mark.parametrize("名字", ["树遍历", "Python VM", "C VM", "Rust 宿主", "Node 宿主"])
def test_进程没退出时输出就已经出现(名字, tmp_path):
    """核心判据：`输入()` 还卡着（stdin 没关）的时候，屏幕上已经有字了。"""
    _写(tmp_path)
    造 = _执行器们(tmp_path)
    if 名字 not in 造:
        pytest.skip(f"环境里没有 {名字}（没编出 / 没装 node）")

    出 = _跑交互(造[名字](), tmp_path)
    合并 = "".join(出)
    assert 出, ("stdin 还开着的时候一个字都没收到 —— 输出又攒到进程退出了"
                "（脚本模式写不了交互程序）")
    assert "=== 欢迎 ===" in 合并, f"收到东西了，但不是我们打的那行：{出!r}"
    assert ">" in 合并, (f"`输入(\"> \")` 的提示语没出来（提示语没有换行，"
                        f"最容易被漏）：{出!r}")


def test_json_result_只捕获不直写(tmp_path):
    """`--json-result` 的 stdout 必须**只有一个 JSON**：程序输出在 `stdout` 字段里。

    这条是 `stream` 开关存在的理由 —— 要是无脑直写，stdout 上会既有裸输出
    又有 JSON，调用方（agent / 编辑器）就解析不了了。
    """
    _写(tmp_path, '打印("A")\n打印("B")\n')
    命令们 = [("Python VM", _py(["--json-result", "交互.jsh"]))]
    if rust_exe_path().is_file():
        命令们.append(("Rust 宿主",
                       [str(rust_exe_path()), "--json-result", "交互.jsh"]))
    for 名字, 命令 in 命令们:
        r = subprocess.run(命令, cwd=str(tmp_path), capture_output=True,
                           text=True, encoding="utf-8")
        assert r.stdout.count("\n") == 1, f"{名字}：stdout 不止一个 JSON：{r.stdout!r}"
        数 = json.loads(r.stdout)
        assert 数["ok"] is True, 数
        assert 数["stdout"] == "A\nB\n", f"{名字}：程序输出没进 stdout 字段：{数!r}"


def test_沙箱模式也不直写(tmp_path):
    """`--sandbox` 同理（agent 那条路，结果必须是一份 JSON）。"""
    _写(tmp_path, '打印("沙箱里")\n')
    r = subprocess.run(_py(["--sandbox", "--json-result", "交互.jsh"]),
                       cwd=str(tmp_path), capture_output=True, text=True,
                       encoding="utf-8")
    assert json.loads(r.stdout)["stdout"] == "沙箱里\n", r.stdout


def test_出错之前打印的内容不会丢也不会重复(tmp_path):
    """错误路径：先打的那句要出现**恰好一次**，且排在报错前面。"""
    _写(tmp_path, '打印("先打一句")\n令 x = 1 / 0\n')
    命令们 = [("Python VM", _py(["交互.jsh"]))]
    if rust_exe_path().is_file():
        命令们.append(("Rust 宿主", [str(rust_exe_path()), "交互.jsh"]))
    for 名字, 命令 in 命令们:
        r = subprocess.run(命令, cwd=str(tmp_path), capture_output=True,
                           text=True, encoding="utf-8")
        合并 = (r.stdout or "") + (r.stderr or "")
        assert 合并.count("先打一句") == 1, f"{名字}：重复或丢了：{合并!r}"
        assert 合并.index("先打一句") < 合并.index("E2001"), \
            f"{名字}：打印的内容跑到报错后面去了：{合并!r}"


def test_只有CLI脚本那条路才开流式():
    """结构判据：`set_stream_output(true)` **只允许出现在 `run.rs`**。

    防的是「以后有人在 DAP / MCP / sandbox 里顺手也打开它」—— 那些路的
    stdout 是**协议通道**，多一个字节就把报文冲坏了，而且**跑起来不报错**
    （只有在编辑器里才发现调试器连不上）。反过来，删掉 `run.rs` 里那两行
    同样会让本测试红：那时交互程序又会退回「攒到退出」。
    """
    # 只认**真的打开**（第二个实参是 `true`）；函数签名本身也含这个子串，
    # 直接用 in 判会把定义处（lib.rs / walk.rs）也算成调用点。
    import re

    模式 = re.compile(r"set_stream_output\([^)]*,\s*true\)")
    调用点 = sorted({p.name for p in (ROOT / "rust" / "src").rglob("*.rs")
                    if 模式.search(p.read_text(encoding="utf-8"))})
    assert 调用点 == ["run.rs"], f"流式被打开的位置不对：{调用点}"
    run_rs = (ROOT / "rust" / "src" / "run.rs").read_text(encoding="utf-8")
    assert run_rs.count("set_stream_output(&mut vm, true)") == 2, \
        "脚本路径与字节码路径都该打开（两条都是「CLI 直接跑」）"
