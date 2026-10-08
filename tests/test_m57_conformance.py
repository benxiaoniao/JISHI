# -*- coding: utf-8 -*-
"""R0 一致性夹具的守门测试（新 M57）。

R0 立的是「**前端行为不许悄悄变**」这条规矩，所以这个文件的重点不是
「夹具跑得通」，而是**夹具本身可不可信**：

- 基线真的全绿吗？（前端没被人改坏）
- **检测真的能报红吗？**（改一个 token 的列号、改一行语料、加一个语料、
  删一个语料 —— 四种情形必须都能被抓到）
- 前端抛**非基石错误**时，夹具会不会把 bug 冻成标准？（必须拒绝冻结）
- 产物是确定性的吗？（同一语料跑两次逐字节相同 —— 否则夹具自己就是噪声）
- 语料覆盖面有没有缩水？（防止有人为了让测试变绿而删语料）
"""

import ast
import dataclasses
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import conformance as C                                            # noqa: E402


# ---------------------------------------------------------------------------
# 一、基线全绿（前端没被改坏）
# ---------------------------------------------------------------------------

def test_基线全绿():
    """四阶段产物与基线逐项一致。

    这条一旦红了，先别急着 `freeze` —— 先看 `git diff` 弄清**是谁改的、
    改对没有**：`freeze` 是「把当前行为升格为标准」，它不判断对错。
    """
    mismatches, others, details = C.compare_all()
    assert not mismatches, (
        "产物与基线不一致（改对了就重冻，改错了就改回来）：\n  "
        + "\n  ".join(mismatches + details))
    assert not others, "语料/清单侧有问题：\n  " + "\n  ".join(others)


def test_语料覆盖面没有缩水():
    """**不许为了让测试变绿而删语料。**

    夹具的价值随覆盖面增长；悄悄少掉几个语料，测试照样全绿，但保护范围
    已经小了。所以这里把「至少要有多少」钉住（数量下降就得改这条断言，
    那就必须是有意识的决定）。
    """
    corpus = C.collect_corpus()
    counts: dict[str, int] = {}
    for cat, _ in corpus:
        counts[cat] = counts.get(cat, 0) + 1

    assert counts.get("lang", 0) >= 40, f"语言语料只剩 {counts.get('lang', 0)} 个"
    assert counts.get("examples", 0) >= 20, f"示例语料只剩 {counts.get('examples', 0)} 个"
    assert counts.get("stdlib", 0) >= 4, f"基石库层语料只剩 {counts.get('stdlib', 0)} 个"
    assert len(corpus) >= 70, f"语料总数只剩 {len(corpus)} 个"


def test_诊断阶段真的有内容():
    """**四个阶段都要被覆盖到**：如果语料全是「正常程序」，
    `diagnostics` 这一格永远是 null —— 那它等于没测。"""
    man = json.loads(C.MANIFEST.read_text(encoding="utf-8"))
    stages: dict[str, int] = {}
    for e in man["语料"]:
        d = json.loads((C.BASELINE_DIR / f"{e['id']}.json").read_text(encoding="utf-8"))
        if d["diagnostics"]:
            stages[d["diagnostics"]["阶段"]] = stages.get(d["diagnostics"]["阶段"], 0) + 1
    assert sum(stages.values()) >= 10, f"报错语料太少：{stages}"
    assert stages.get("词法", 0) >= 3, f"词法阶段没被覆盖：{stages}"
    assert stages.get("语法", 0) >= 3, f"语法阶段没被覆盖：{stages}"


# ---------------------------------------------------------------------------
# 二、检测真的能报红（四种情形）
# ---------------------------------------------------------------------------

@pytest.fixture
def mini(tmp_path, monkeypatch):
    """一个只含两个语料的迷你工程（把工具的根目录指过去）。

    为什么不拿真实基线做「破坏实验」：真基线是仓库里的文件，测试改它
    有留脏风险。指到 tmp 里，怎么折腾都不影响真仓库。
    """
    proj = tmp_path / "proj"
    (proj / "tests" / "cases").mkdir(parents=True)
    monkeypatch.setattr(C, "ROOT", proj)
    monkeypatch.setattr(C, "CONF_DIR", proj / "conformance")
    monkeypatch.setattr(C, "BASELINE_DIR", proj / "conformance" / "baseline")
    monkeypatch.setattr(C, "MANIFEST", proj / "conformance" / "MANIFEST.json")
    monkeypatch.setattr(C, "CORPUS_SOURCES", (("lang", "tests/cases/*.jsh"),))
    return proj


def _write(proj: Path, name: str, src: str) -> None:
    (proj / "tests" / "cases" / name).write_text(src, encoding="utf-8")


def _baseline(proj: Path, name: str) -> Path:
    return proj / "conformance" / "baseline" / f"tests__cases__{name}.json"


def test_检测能发现_产物被改动(mini, capsys):
    """改了产物（哪怕只是**一个 token 的列号 +1**）必须报红。"""
    _write(mini, "a.jsh", "令 a = 1\n打印(a)\n")
    assert C.main(["freeze"]) == 0
    assert C.main(["check"]) == 0

    p = _baseline(mini, "a")
    d = json.loads(p.read_text(encoding="utf-8"))
    d["tokens"][0]["col"] += 1
    p.write_text(C.dumps(d), encoding="utf-8")

    capsys.readouterr()
    assert C.main(["check"]) == 1, "改了一个 token 的列号却没报红"
    assert "tokens[0].col" in capsys.readouterr().out, "报红时没指出具体位置"


def test_检测能发现_语料被改动(mini):
    """改了语料内容却没重冻 → 基线记录的是「另一份源码」的输出，必须报红。"""
    _write(mini, "a.jsh", "令 a = 1\n打印(a)\n")
    assert C.main(["freeze"]) == 0

    _write(mini, "a.jsh", "令 a = 2\n打印(a)\n")          # 内容变了
    assert C.main(["check"]) == 1, "语料改了却没报红"
    assert C.main(["freeze"]) == 0                        # 重冻后恢复
    assert C.main(["check"]) == 0


def test_检测能发现_新增语料未冻结(mini):
    _write(mini, "a.jsh", "令 a = 1\n")
    assert C.main(["freeze"]) == 0

    _write(mini, "b.jsh", "打印(2)\n")                    # 新语料，没进基线
    assert C.main(["check"]) == 1, "新增语料没冻结却没报红"


def test_检测能发现_孤儿基线(mini):
    _write(mini, "a.jsh", "令 a = 1\n")
    _write(mini, "b.jsh", "打印(2)\n")
    assert C.main(["freeze"]) == 0

    (mini / "tests" / "cases" / "b.jsh").unlink()         # 删语料，基线还在
    assert C.main(["check"]) == 1, "语料删了、基线成了孤儿，却没报红"


# ---------------------------------------------------------------------------
# 三、不许把 bug 冻成标准
# ---------------------------------------------------------------------------

def test_前端抛非基石错误时拒绝冻结(mini, monkeypatch):
    """**这条是整套夹具的底线。**

    工具第一次跑就撞上过：我把 `serialize.dump_json` 写成了 `dumps_json`，
    73 个语料的 bytecode 全变成「编译阶段：<内部错误：ImportError>」。
    如果 freeze 照单全收，以后 Rust 版「忠实复刻这个 ImportError」才算一致。

    所以：非基石错误 = 实现有 bug = **拒绝冻结**，且**不留半成品清单**
    （否则会得到一个「一半是标准、一半是 bug」的基线）。
    """
    _write(mini, "a.jsh", "令 a = 1\n")

    import oracle.jishi.compiler as Comp

    def boom(*_a, **_k):
        raise RuntimeError("假装编译器崩了")

    monkeypatch.setattr(Comp, "compile_source", boom)
    assert C.main(["freeze"]) == 1, "前端抛非基石错误，居然冻结成功了"
    assert not C.MANIFEST.exists(), "拒绝冻结时不该留下清单"


def test_check_遇到实现_bug_也报红(mini, monkeypatch):
    _write(mini, "a.jsh", "令 a = 1\n")
    assert C.main(["freeze"]) == 0

    import oracle.jishi.compiler as Comp

    def boom(*_a, **_k):
        raise RuntimeError("假装编译器崩了")

    monkeypatch.setattr(Comp, "compile_source", boom)
    assert C.main(["check"]) == 1, "实现崩了却报「一致」"


# ---------------------------------------------------------------------------
# 四、产物必须可确定性复现
# ---------------------------------------------------------------------------

def test_产物两次跑逐字节相同():
    """同一语料跑两次必须完全一样 —— 否则夹具自己就是噪声源。

    容易引入不确定性的地方：集合/字典顺序、浮点格式化、时间戳、绝对路径。
    所以基线里的 `filename` 用的是**相对路径**（绝对路径会随机器变）。
    """
    for _cat, rel in C.collect_corpus()[:8] + C.collect_corpus()[-4:]:
        a = C.dumps(C.run_stages(rel))
        b = C.dumps(C.run_stages(rel))
        assert a == b, f"{rel} 两次跑出来的产物不同 —— 有不确定性"


def test_基线文件用的是规范序列化():
    """基线必须是 `dumps` 的输出（缩进/键序统一）。

    这条同时挡住「手改基线」：手改过的文件格式多半与 `dumps` 不一致，
    而且**手改天然绕过了「产物由程序产生」这个前提**。
    """
    files = sorted(C.BASELINE_DIR.glob("*.json"))
    assert files, "一个基线文件都没有"
    for p in files:
        raw = p.read_text(encoding="utf-8")
        assert raw == C.dumps(json.loads(raw)), f"{p.name} 不是规范序列化（手改过？）"


def test_基线文件行尾是_LF():
    """**行尾也要钉死。**

    夹具的命根子是「逐字节确定」，而 Windows 上 `write_text` 默认把 `\\n`
    翻成 `\\r\\n` —— 那样同一份基线在两个平台的工作区里就是不同的字节
    （M56 在文件读写上刚吃过这个亏：`newline=None` 让同程序在两个平台
    产出不同字节）。所以写入时显式 `newline="\\n"`，这里钉住。
    """
    files = sorted(C.BASELINE_DIR.glob("*.json"))
    assert files, "一个基线文件都没有"
    for p in files[:25]:
        assert p.read_bytes().count(b"\r\n") == 0, \
            f"{p.name} 里有 CRLF —— 行尾必须显式写 \\n"
    assert C.MANIFEST.read_bytes().count(b"\r\n") == 0, "MANIFEST.json 里有 CRLF"


def test_语料里没有绝对路径泄漏():
    """基线里**不许出现本机绝对路径** —— 那会让夹具换台机器就全红，
    而且会把个人目录写进仓库。"""
    for p in sorted(C.BASELINE_DIR.glob("*.json"))[:20]:
        t = p.read_text(encoding="utf-8")
        assert "D:\\code" not in t and "D:/code" not in t, f"{p.name} 里有本机路径"
        assert str(Path.home()) not in t, f"{p.name} 里有用户主目录"


# ---------------------------------------------------------------------------
# 五、工具本身的小单元
# ---------------------------------------------------------------------------

def test_jsonable_处理_dataclass与特殊值():
    @dataclasses.dataclass
    class N:
        a: int
        b: list
        c: str

    assert C.jsonable(N(1, [2, 3], "x")) == {
        "类型": "N", "a": 1, "b": [2, 3], "c": "x"}
    # JSON 里没有 NaN/Infinity 字面量（Rust 的 serde_json 会拒收）→ 转字符串标记
    assert C.jsonable(float("nan")) == "@NaN"
    assert C.jsonable(float("inf")) == "@Infinity"
    assert C.jsonable(float("-inf")) == "@-Infinity"
    assert C.jsonable(1.5) == 1.5
    # 集合无序 → 排好序（否则每次跑产物都不同）
    assert C.jsonable({3, 1, 2}) == [1, 2, 3]
    assert C.jsonable((1, "a")) == [1, "a"]
    assert C.jsonable(None) is None
    assert C.jsonable(True) is True


def test_first_diff_定位到具体一项():
    """只报「不一致」等于没报 —— 必须指到 `路径.字段[下标]`。"""
    assert C.first_diff(1, 2) == ("<根>", 1, 2)
    assert C.first_diff({"a": 1}, {"a": 2}) == ("a", 1, 2)
    assert C.first_diff({"a": {"b": [1, 2]}}, {"a": {"b": [1, 9]}}) == ("a.b[1]", 2, 9)
    assert C.first_diff({"a": 1}, {"a": 1, "b": 2}) == ("b", "<缺>", 2)
    assert C.first_diff({"a": 1, "b": 2}, {"a": 1}) == ("b", 2, "<缺>")
    assert C.first_diff([1, 2], [1, 2, 3]) == ("长度", 2, 3)
    assert C.first_diff({"x": [1, 2]}, {"x": [1]}) == ("x.长度", 2, 1)
    assert C.first_diff({"a": 1}, {"a": 1}) is None
    # 类型不同也算不同（`1` 与 `True` 不是一回事）
    assert C.first_diff({"a": 1}, {"a": True}) == ("a", 1, True)


def test_corpus_id_可还原成路径():
    assert C.corpus_id("tests/cases/01_算术.jsh") == "tests__cases__01_算术"
    assert C.corpus_id("examples/projects/待办清单/待办.jsh") == \
        "examples__projects__待办清单__待办"


def test_指令集与_MANIFEST_同步():
    """加了指令/改了指令名却没重冻基线 → 这里报红。

    指令集是**跨语言约定**（宿主靠它对表），它变了而基线没更新，
    R5 之后就会拿一份过时的指令表去验收。
    """
    from oracle.jishi import opcodes as O

    man = json.loads(C.MANIFEST.read_text(encoding="utf-8"))
    assert man["指令集"] == O.OP_NAMES, "指令集与基线清单不同步 —— 重冻基线"


# ---------------------------------------------------------------------------
# 六、跨 Python 版本（用户实测关心的那条）
# ---------------------------------------------------------------------------

def _find_other_python() -> str | None:
    """找一个「另一个 Python 大版本」的解释器（不写死个人路径）。

    优先环境变量 `JISHI_PY310`（CI 里可以指定），否则按常见位置与 PATH 探测。
    """
    cands = [os.environ.get("JISHI_PY310", "")]
    la = os.environ.get("LOCALAPPDATA", "")
    if la:
        for v in ("310", "311", "312"):
            cands.append(str(Path(la) / "Programs" / "Python"
                             / f"Python{v}" / "python.exe"))
    for name in ("python3.10", "python3.11", "python3.12"):
        cands.append(shutil.which(name) or "")
    for c in cands:
        if c and Path(c).is_file():
            return c
    return None


def test_前端行为不随_python_版本变():
    """**前端语义必须与 Python 大版本无关。**

    这条对 R 线有直接用处：它把「两个 Python 版本语义是否一致」变成一个
    几秒就能回答的问题（不必跑 20 分钟全量测试）。

    建设性背景（2026-09-30）：CI 里 Python 3.10 的任务一直失败，而本地
    用 3.10.9 跑这条夹具是**全绿**的 —— 说明那不是前端语义差异，
    排查方向应该转向测试基础设施/平台假设。
    """
    exe = _find_other_python()
    if not exe:
        pytest.skip("本机没有第二个 Python 大版本（可用 JISHI_PY310 指定）")

    r = subprocess.run([exe, str(ROOT / "tools" / "conformance.py"), "check"],
                       capture_output=True, text=True, encoding="utf-8",
                       cwd=str(ROOT), timeout=300)
    assert r.returncode == 0, (
        f"{exe} 跑出的前端产物与基线不同 —— 前端行为依赖了 Python 版本：\n"
        f"{(r.stdout or '')[-1200:]}\n{(r.stderr or '')[-400:]}")


# ---------------------------------------------------------------------------
# 七、写法约束：f-string 里不许有反斜杠（跨 Python 版本的坑）
# ---------------------------------------------------------------------------

#: 扫哪些目录找 `.py`（根目录下的 `*.py` 也扫）。
_SCAN_ROOTS = ("jishi", "tools", "tests", "bench")
_SKIP_PARTS = {".venv", "node_modules", "build", ".tmp-probe", "target",
               "dist", "site", "__pycache__", ".git"}


def _fstring_backslashes(base: Path | None = None,
                         roots: tuple[str, ...] = _SCAN_ROOTS) -> list[str]:
    """找出「f-string 的表达式部分里含反斜杠」的位置。

    PEP 701（3.12+）才允许这么写，**3.10 / 3.11 直接 SyntaxError**。
    所以用 AST 主动找 —— 不能等解释器报错：开发机是 3.13，永远不报。
    """
    root = base or ROOT
    files = list(root.glob("*.py"))
    for r in roots:
        d = root / r
        if d.is_dir():
            files += list(d.rglob("*.py"))

    bad: list[str] = []
    for p in sorted(files):
        if any(part in _SKIP_PARTS for part in p.parts):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as e:
            # 3.10/3.11 跑到这里：含反斜杠的 f-string 直接解析失败
            bad.append(f"{p.relative_to(root)}:{e.lineno} 语法错误：{e.msg}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FormattedValue):
                try:
                    seg = ast.unparse(node)
                except Exception:                              # noqa: BLE001
                    continue
                if "\\" in seg:
                    bad.append(f"{p.relative_to(root)}:{node.lineno} "
                               f"f-string 表达式里含反斜杠")
    return bad


def test_没有在_fstring_表达式里写反斜杠():
    """**f-string 的表达式部分里不许出现反斜杠。**

    2026-09-30 的实况：`tools/build_home.py` 里有 **12 处**
    `{code('…\\n…')}`（字面反斜杠 n）。本地是 3.13，跑得好好的；
    一到 CI 的 3.10 就**整片测试红** —— `import build_home` 直接 SyntaxError，
    连带 7 个用到它的测试全挂。**这种错误开发机永远看不到。**

    改法：把含反斜杠的字符串提到 f-string **外面**取个名字，在里面引用它。
    """
    bad = _fstring_backslashes()
    assert not bad, (
        "f-string 的表达式部分里不许有反斜杠（Python 3.12+ / PEP 701 才允许，\n"
        "CI 里的 3.10 会直接 SyntaxError）：\n  " + "\n  ".join(bad) +
        "\n\n改法：把含反斜杠的字符串提到 f-string 外面，先赋值给一个名字。")


def test_fstring_检测本身能发现(tmp_path):
    """**检测要真的能报红**（否则它就是一条永远绿的摆设）。"""
    (tmp_path / "ok.py").write_text('x = f"{a}"\n', encoding="utf-8")
    (tmp_path / "bad.py").write_text('x = f"{a(\'p\\nq\')}"\n', encoding="utf-8")
    bad = _fstring_backslashes(base=tmp_path, roots=())
    assert len(bad) == 1, f"期望只报 1 处，实际：{bad}"
    assert "bad.py" in bad[0]


# ---------------------------------------------------------------------------
# 七、写法约束：f-string 里不许有反斜杠（跨 Python 版本的坑）
# ---------------------------------------------------------------------------

#: 扫哪些目录找 `.py`（根目录下的 `*.py` 也扫）。
_SCAN_ROOTS = ("jishi", "tools", "tests", "bench")
_SKIP_PARTS = {".venv", "node_modules", "build", ".tmp-probe", "target",
               "dist", "site", "__pycache__", ".git"}


def _fstring_backslashes(base: Path | None = None,
                         roots: tuple[str, ...] = _SCAN_ROOTS) -> list[str]:
    """找出「f-string 的表达式部分里含反斜杠」的位置。

    PEP 701（3.12+）才允许这么写，**3.10 / 3.11 直接 SyntaxError**。
    所以用 AST 主动找 —— 不能等解释器报错：开发机是 3.13，永远不报。
    """
    root = base or ROOT
    files = list(root.glob("*.py"))
    for r in roots:
        d = root / r
        if d.is_dir():
            files += list(d.rglob("*.py"))

    bad: list[str] = []
    for p in sorted(files):
        if any(part in _SKIP_PARTS for part in p.parts):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as e:
            # 3.10/3.11 跑到这里：含反斜杠的 f-string 直接解析失败
            bad.append(f"{p.relative_to(root)}:{e.lineno} 语法错误：{e.msg}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FormattedValue):
                try:
                    seg = ast.unparse(node)
                except Exception:                              # noqa: BLE001
                    continue
                if "\\" in seg:
                    bad.append(f"{p.relative_to(root)}:{node.lineno} "
                               f"f-string 表达式里含反斜杠")
    return bad


def test_没有在_fstring_表达式里写反斜杠():
    """**f-string 的表达式部分里不许出现反斜杠。**

    2026-09-30 的实况：`tools/build_home.py` 里有 **12 处**
    `{code('…\n…')}`（字面反斜杠 n）。本地是 3.13，跑得好好的；
    一到 CI 的 3.10 就**整片测试红** —— `import build_home` 直接 SyntaxError，
    连带 7 个用到它的测试全挂。**这种错误开发机永远看不到。**

    改法：把含反斜杠的字符串提到 f-string **外面**取个名字，在里面引用它。
    """
    bad = _fstring_backslashes()
    assert not bad, (
        "f-string 的表达式部分里不许有反斜杠（Python 3.12+ / PEP 701 才允许，\n"
        "CI 里的 3.10 会直接 SyntaxError）：\n  " + "\n  ".join(bad) +
        "\n\n改法：把含反斜杠的字符串提到 f-string 外面，先赋值给一个名字。")


def test_fstring_检测本身能发现(tmp_path):
    """**检测要真的能报红**（否则它就是一条永远绿的摆设）。"""
    (tmp_path / "ok.py").write_text('x = f"{a}"\n', encoding="utf-8")
    (tmp_path / "bad.py").write_text('x = f"{a(\'p\nq\')}"\n', encoding="utf-8")
    bad = _fstring_backslashes(base=tmp_path, roots=())
    assert len(bad) == 1, f"期望只报 1 处，实际：{bad}"
    assert "bad.py" in bad[0]
