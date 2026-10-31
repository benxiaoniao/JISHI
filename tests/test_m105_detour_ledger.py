# -*- coding: utf-8 -*-
"""M105 绕道台账：**量尺本身**要有人守。

三件事：

1. `docs/绕道台账.md` 必须与探针 README 里的条目**逐字一致** ——
   台账是**扫出来的投影**，谁手改了就会红。
2. 自检的「过期报红」真的会红：函数已经补上了却还标着「缺」、模块名写错、
   `增强` 指向一个不存在的函数…… 都要报出来（否则这条纪律就是纸面上的）。
3. 每个探针项目**真的能跑**（不是死代码），且**至少有一条绕道**（否则量尺失效）。

⚠️ 纪律（`AGENTS.md` §1）：跑探针一律 `chdir` 到 `tmp_path`，绝不在仓库根读写真实文件
—— 探针会在当前目录写 `账目.json` / `待办.html` / `样本/`。
"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import detour_ledger as dl  # noqa: E402
from oracle.jishi.interpreter import run_source  # noqa: E402

PROBES = ROOT / "examples" / "绕道探针"


def _条目(**kw) -> dict:
    base = {"编号": "T1", "类型": "缺", "目标": "文件.原子写", "位置": "",
            "说明": "测试用", "项目": "记账本", "来源": "测试"}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# 一、台账是投影：手改就对不上
# ---------------------------------------------------------------------------

def test_台账与探针里的条目一致():
    entries = dl.collect()
    if not entries:
        # 🔴 **0 条是目标状态**（M119 起三条都销完），不是异常 —— 前提是
        # **探针都还在、且每一个都标了「状态：已毕业」**。
        # 这条以前写成「必须至少一条」，那是「总有缺口」时期的假设；
        # 沿用下去会让「全部毕业」这个好结果报错。
        全毕业 = all(
            any(dl.GRADUATED_RE.match(x) for x in
                r.read_text(encoding="utf-8").splitlines())
            for r in dl.PROBES.glob("*/README.md"))
        assert 全毕业, (
            "一条绕道都没扫到 —— 要么 examples/绕道探针/*/README.md 没了，"
            "要么漏标「状态：已毕业」")
    assert dl.render(entries) == dl.LEDGER.read_text(encoding="utf-8"), (
        "docs/绕道台账.md 与探针里的条目对不上。"
        "跑 `python tools/detour_ledger.py` 重新生成 —— **不要手改台账**")


def test_台账自检现在是过的():
    problems = dl.check(dl.collect())
    assert problems == [], "台账自检没过：\n  - " + "\n  - ".join(problems)


def test_编号全仓库唯一():
    编号 = [e["编号"] for e in dl.collect()]
    重复 = {x for x in 编号 if 编号.count(x) > 1}
    assert not 重复, f"编号重复：{重复}（编号是台账的引用锚点）"


def test_每个探针项目都至少有一条绕道():
    """**量尺失效的判据**：一个探针项目若一条绕道都撞不出来，它就不该叫探针
    （多半是写得太浅，或者不涉及新能力）。

    ⚠️ **例外只有一个：标了「状态：已毕业」的探针** —— 那是「缺口后来都补上了」
    的项目（M112 起《记账本》就是第一个）。例外也要守规矩：**标了就必须一条都没有**
    （下面那条判据盯着），免得「已毕业」变成免检牌。
    """
    毕业的 = set(dl.graduated())
    有 = {e["项目"] for e in dl.collect()}
    全部 = {p.name for p in PROBES.iterdir() if p.is_dir()}
    缺 = 全部 - 有 - 毕业的
    assert not 缺, f"这些探针项目一条绕道都没记（要毕业得在 README 里写明）：{缺}"


def test_已毕业的探针不能还留着绕道():
    """「已毕业」不是免检牌：标了就得**真的**一条不剩。

    这条与上一条是一对 —— 上一条防「探针写得太浅还挂着名」，这条防
    「缺口没消干净就自称毕业」。
    """
    多余 = [名 for 名 in dl.graduated()
            if any(e["项目"] == 名 for e in dl.collect())]
    assert not 多余, f"这些项目标着「已毕业」却还留着绕道条目：{多余}"


def test_已毕业名单进了台账():
    """台账是投影：已毕业的探针要能在那儿看见（不然「毕业」只是 README 里一句话）。"""
    台账 = dl.LEDGER.read_text(encoding="utf-8")
    for 名 in dl.graduated():
        assert f"- `{名}`" in 台账, f"台账里没有已毕业的「{名}」—— 重跑 tools/detour_ledger.py"


# ---------------------------------------------------------------------------
# 二、「已经能做」与「还做不了」必须分得开（过期报红）
# ---------------------------------------------------------------------------

def test_补上了的函数不许还标着缺():
    p = dl.check([_条目(类型="缺", 目标="文本.左对齐")])
    assert p and "已经有了" in p[0], p


def test_新增模块已经存在要报红():
    p = dl.check([_条目(类型="新增模块", 目标="文本")])
    assert p and "已经有" in p[0], p


def test_增强指向不存在的函数要报红():
    # ⚠️ 用一个**永远不存在**的函数名（`文件` 模块在、`某不存在的函数` 不在），
    # 别用真的待补函数（如 `文件.原子写` 这种）—— 它一旦被补上，这条测试就自己红了。
    p = dl.check([_条目(类型="增强", 目标="文件.某不存在的函数")])
    assert p and "还不存在" in p[0], p


def test_模块名写错要报红():
    p = dl.check([_条目(类型="缺", 目标="文本t.左对齐")])
    assert p and "不存在" in p[0], p


def test_位置指到不存在的文件要报红():
    p = dl.check([_条目(位置="没有这个.jsh:1")])
    assert p and "不存在" in p[0], p


def test_行号超出文件长度要报红():
    p = dl.check([_条目(位置="记账.jsh:99999")])
    assert p and "超出" in p[0], p


# ---------------------------------------------------------------------------
# 三、探针真的能跑（不是摆着看的死代码）
# ---------------------------------------------------------------------------

def _跑(项目: str, 脚本: str, 参数们: list[str], monkeypatch, tmp_path) -> str:
    """在 tmp_path 里跑一个探针，返回 stdout。"""
    import oracle.jishi.stdlib.系统 as 系统模块

    系统模块._设参数(参数们)
    src = (PROBES / 项目 / 脚本).read_text(encoding="utf-8")
    buf = io.StringIO()
    monkeypatch.chdir(tmp_path)
    try:
        with redirect_stdout(buf):
            run_source(src)
    finally:
        系统模块._设参数([])
    return buf.getvalue()


def test_探针_记账本_记入并汇总(monkeypatch, tmp_path):
    out = _跑("记账本", "记账.jsh", ["记", "25.5", "餐饮", "午饭"], monkeypatch, tmp_path)
    assert "已记：25.50 元 / 餐饮 / 午饭" in out, out

    out = _跑("记账本", "记账.jsh", ["记", "100", "交通", "打车"], monkeypatch, tmp_path)
    assert "已记：100.00 元 / 交通 / 打车" in out, out

    out = _跑("记账本", "记账.jsh", ["汇总"], monkeypatch, tmp_path)
    assert "餐饮" in out and "25.50" in out, out
    assert "合计" in out and "125.50" in out, out

    out = _跑("记账本", "记账.jsh", ["查", "餐饮"], monkeypatch, tmp_path)
    assert "「餐饮」共 1 笔" in out, out

    out = _跑("记账本", "记账.jsh", [], monkeypatch, tmp_path)
    assert "共 2 笔" in out, out          # 数据真的落在磁盘上了


def test_探针_并发批量_能算摘要(monkeypatch, tmp_path):
    样本 = tmp_path / "样本"
    样本.mkdir()
    for i in range(3):
        (样本 / f"数据{i}.bin").write_bytes(bytes([i]) * 1024)

    out = _跑("并发批量", "批量.jsh", ["样本"], monkeypatch, tmp_path)
    assert "共 3 个文件" in out, out
    # M114 起它顺序、并发两档都跑，并核对「并发没改语义」
    assert "两档结果一致：真" in out, out
    assert "顺序" in out and "并发" in out, out


def test_探针_本地小服务_无参只打用法(monkeypatch, tmp_path):
    """⚠️ **M115 起这条特别重要**：探针现在会起一个**阻塞**的服务端，
    所以「无参数」必须是「打用法就退出」—— 否则 check_engines / conformance
    自动跑它的时候会**一直挂住**（判据跑不完）。"""
    out = _跑("本地小服务", "服务.jsh", [], monkeypatch, tmp_path)
    assert "用法：jishi 服务.jsh <端口>" in out, out
    # 不能留下任何文件（以前那版会写 待办.html）
    assert not list(tmp_path.iterdir()), "无参跑不该产出文件"


def test_探针_本地小服务_真能起服务(tmp_path):
    """M115 的验收在探针上的样子：**起得来 / 能访问 / 能干净关闭**。

    ⚠️ 必须走**子进程**：服务端是阻塞的，在测试进程里跑会把自己挂住。
    ⚠️ 请求路径里的中文要**百分号编码**（HTTP 客户端只发 ASCII）；
    并且**绕开环境代理**（沙箱里配了 http_proxy，会把 127.0.0.1 也劫走）。
    """
    import os as _os
    import socket as _socket
    import subprocess as _sp
    import sys as _sys
    import time as _time
    import urllib.request as _ur

    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    端口 = s.getsockname()[1]
    s.close()
    脚本 = PROBES / "本地小服务" / "服务.jsh"
    env = {**_os.environ, "PYTHONPATH": str(ROOT)}
    p = _sp.Popen([_sys.executable, "-m", "oracle.jishi.cli", str(脚本), str(端口), "3"],
                  cwd=str(tmp_path), env=env, stdout=_sp.PIPE, stderr=_sp.PIPE,
                  text=True, encoding="utf-8")
    # ⚠️ **不跟跳转**：`/加` 回 303 + Location，urllib 默认会自己再发一次
    #    （那一下也要计进「最多请求数」）—— 让测试**自己**数请求个数，
    #    不然 3 个请求会变成 4 个，服务提前退出、最后一个请求被拒。
    class _不跳(_ur.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    开 = _ur.build_opener(_ur.ProxyHandler({}), _不跳())
    try:
        通 = False
        for _ in range(150):
            try:
                with _socket.create_connection(("127.0.0.1", 端口), timeout=0.5):
                    通 = True
                    break
            except OSError:
                _time.sleep(0.1)
        assert 通, "服务没起来"
        with 开.open(f"http://127.0.0.1:{端口}/", timeout=10) as r:
            体 = r.read().decode("utf-8")
        assert r.status == 200 and "待办清单" in 体 and "买菜" in 体, 体[:300]
        # 点一下「加上」（表单走 GET，浏览器地址栏里就是这条）
        try:
            with 开.open(f"http://127.0.0.1:{端口}/%E5%8A%A0?%E5%90%8D%E5%AD%97=%E4%B9%B0%E7%89%9B%E5%A5%B6",
                         timeout=10) as r:
                码, 位置 = r.status, r.headers.get("Location")
        except _ur.HTTPError as e:               # 3xx 在没有跳转处理器时走这里
            码, 位置 = e.code, e.headers.get("Location")
        assert (码, 位置) == (303, "/"), (码, 位置)
        with 开.open(f"http://127.0.0.1:{端口}/", timeout=10) as r:
            assert "买牛奶" in r.read().decode("utf-8")
        # 干净关闭：正好 3 个请求，退码 0，并且**落盘了**
        出, _错 = p.communicate(timeout=30)
        assert p.returncode == 0, _错[:300]
        assert "处理了 3 个请求" in 出, 出
        assert "买牛奶" in (tmp_path / "待办.json").read_text(encoding="utf-8")
    finally:
        if p.poll() is None:
            p.kill()


# ---------------------------------------------------------------------------
# 四、台账要**过得了发布脚本的「抹引用」**（M111 发版时踩到的第二个坑）
# ---------------------------------------------------------------------------

def test_台账经发布脚本抹引用后一个字不变(tmp_path):
    """🔴 为什么单独立一条（2026-10-14，M111 发版时逮到）：

    发布脚本 `publish_github._sanitize()` 会把**指向内部文档的引用**抹成中性标签
    （目的是「公开仓库里不留死链」）。而台账是**生成物**，它里面原本写着
    ``docs/路线图.md`` ⇒ 在**发布快照**上被抹成「内部路线图（未公开）」，
    于是 `test_台账与探针里的条目一致`（拿**生成器的新鲜输出**去比）在 CI 上必失败。

    📌 **它躲过了本地所有检查**：源码树里**没有抹引用这一步**，所以本地永远绿 ——
    「CI 红、本地绿」的经典形态（同族：M104 的 `前端契约.md` 那次）。
    而且 CI 自己红了 **7 轮没人看**（Release 只在打 tag 时跑，CI 的结果没人盯）。

    ⇒ 修法：**生成器原样就写出中性表述**，别写指向未公开文件的路径。
    这条判据把这个不变量钉住：**台账过一遍抹引用，必须一个字节都不变**。
    """
    import publish_github as pg

    snap = tmp_path / "snap"
    (snap / "docs").mkdir(parents=True)
    # `_sanitize` 会**无条件**读这两份，缺了它自己会崩（那不是本判据要测的东西）
    (snap / "README.md").write_text("# 基石\n", encoding="utf-8")
    (snap / "docs" / "设计决策.md").write_text("# 设计决策\n", encoding="utf-8")
    生成 = dl.render(dl.collect())
    (snap / "docs" / "绕道台账.md").write_text(生成, encoding="utf-8", newline="\n")

    pg._sanitize(snap)

    台账 = snap / "docs" / "绕道台账.md"
    assert 台账.exists(), "台账被发布脚本删了？它是**要公开**的文档"
    assert 台账.read_text(encoding="utf-8") == 生成, (
        "台账过一遍发布脚本的「抹引用」就变了 —— 生成器里又写上了指向内部文档的"
        "路径（`docs/路线图.md` 之类）。改成原样写中性表述"
        "（例如「内部路线图（未公开）」），两边就一致了")


def test_发布脚本确实会抹掉内部文档的引用(tmp_path):
    """反向的「别测了个空」：确认 `_sanitize` 真的会动这种引用。

    否则哪天抹引用被改坏了，上面那条会**变成永远绿**而没人发现。
    """
    import publish_github as pg

    snap = tmp_path / "snap"
    (snap / "docs").mkdir(parents=True)
    (snap / "README.md").write_text("# 基石\n", encoding="utf-8")
    (snap / "docs" / "设计决策.md").write_text("# 设计决策\n", encoding="utf-8")
    (snap / "docs" / "某文档.md").write_text(
        "见 `docs/路线图.md` §12。\n", encoding="utf-8")

    pg._sanitize(snap)

    改后 = (snap / "docs" / "某文档.md").read_text(encoding="utf-8")
    assert "路线图.md" not in 改后, f"抹引用没生效？{改后!r}"
    assert "内部路线图（未公开）" in 改后, 改后
