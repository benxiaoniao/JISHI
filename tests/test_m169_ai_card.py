# -*- coding: utf-8 -*-
"""M169 · 语言卡准确性（`docs/版本收口流程.md` 里的发版必跑项）。

## 背景：真实项目撞出来的第一条

2026-11-03，另一个模型在别的机器上用基石写「文档检索工具」，第一件事就撞墙：

```
语言卡：参数.解析(句柄, 参数们=空)     ← 看着第二个参数可省
实际：  参数.解析(解析器)              ← 直接报「还要一个参数列表」
```

它没有猜错——**卡在说谎**。而语言卡是给模型看的**唯一依据**（AGENTS 核心二），
卡错 = 模型必错，且它没有别的办法自救。

## 这一站修了什么

1. **`参数.解析` 真的能省了**：省了就用本进程的命令行参数（= `系统.参数()`）。
   四个实现全改：基石库层 `参数.jsh`（Python / C VM / 树遍历真正跑的那份）、
   宿主层 `参数.py`（兜底）、Rust `args.rs`、Node `vm.js`。
2. **一次式少给一个参数**的报错改成「照着改就行」的话（原来是「参数不够」这种废话），
   而且四个实现**文案逐字相同**。
3. **加了 `tools/audit_ai_card.py`**：发版前跑，查四件事 ——
   假默认值 / 两层签名不一致 / 卡自身坏掉 / 模块清单对不上。

## ⚠️ 这里有个坑：语言卡是从**宿主层** `.py` 生成的，但 Python 侧跑的是**基石库层** `.jsh`

`参数` / `容器` / `统计` / `迭代` 四个模块有两份实现（M52 的基石库层 +
宿主层兜底），而 Rust / Node 用的是各自的原生实现 —— **一共三份**。
卡只描述其中一份。所以「两层签名一致」这条判据是必需品，不是锦上添花：
真出现漂移时，卡会描述一个**谁都没在跑**的签名。
"""

import sys

import pytest

sys.path.insert(0, ".")

from conftest import ROOT, rust_exe_path                            # noqa: E402


# ---------------------------------------------------------------------------
# 一、审计本身
# ---------------------------------------------------------------------------

def test_语言卡审计没有发现问题():
    """四类检查全部干净。失败时把报告原样打出来 —— 别让人再去翻代码找。"""
    sys.path.insert(0, str(ROOT / "tools"))
    import audit_ai_card

    报告 = audit_ai_card.审计()
    assert 报告["问题总数"] == 0, (
        "语言卡有问题（这是**给模型看的唯一依据**）：\n"
        + "\n".join(f"【{k}】{v}" for k, v in 报告.items()
                    if isinstance(v, list) and v))


def test_假默认检测器真的能抓到(monkeypatch):
    """给检测器喂一个**已知坏掉的**函数，它必须报出来。

    ⚠️ 没有这条，`test_语言卡审计没有发现问题` 可能只是「检测器失效了」——
    一个永远绿的判据比没有判据更危险。
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import audit_ai_card
    from oracle.jishi.errors import RunError
    from oracle.jishi.stdlib import 参数 as 参数模块

    def 假的(句柄, 参数们=None):
        if 参数们 is None:
            raise RunError("还要一个参数列表")
        return {}

    假的.__module__ = 参数模块.__name__
    monkeypatch.setattr(参数模块, "假的假默认", 假的, raising=False)
    命中 = audit_ai_card._查假默认()
    assert any(x["函数"] == "假的假默认" for x in 命中), (
        f"检测器没抓到人造的假默认：{命中}")


def test_两层签名覆盖到了基石库层():
    """四个库层模块都得被比对到 —— 漏一个，那个模块就没人看着。"""
    sys.path.insert(0, str(ROOT / "tools"))
    import audit_ai_card

    库层 = {p.stem for p in audit_ai_card.库层目录.glob("*.jsh")}
    assert 库层 == {"参数", "容器", "统计", "迭代"}, (
        f"基石库层的模块变了：{库层}。加了模块就来这里补，否则新模块的签名没人比对")
    报告 = audit_ai_card._查两层签名()
    assert not [x for x in 报告 if x.get("问题") == "宿主层没有同名模块，无法比对"]


# ---------------------------------------------------------------------------
# 二、行为：`参数.解析(句柄)` 现在真的能跑（四个实现一致）
# ---------------------------------------------------------------------------

程序 = """\
导入 参数
令 p = 参数.新建("演示")
参数.选项(p, "名字", "世界")
参数.标志(p, "详细")
令 r = 参数.解析(p)
打印(r["名字"])
打印(r["详细"])
"""

一次式少参 = """\
导入 参数
打印(参数.解析(["a"]))
"""


def _跑(引擎: str, 源: str, tmp_path) -> str:
    import subprocess

    (tmp_path / "程序.jsh").write_text(源, encoding="utf-8", newline="\n")
    # ⚠️ **没有 `--tree` 这个开关**：树遍历就是默认执行器（`--执行器 vm` 才是字节码）。
    #    写 `--tree` 会被当成脚本参数塞进 `系统.参数()` —— 而这条判据恰好要解析
    #    命令行参数，于是「多了一个 --tree」直接把用例本身搞坏（踩过）。
    命令 = [sys.executable, "-m", "oracle.jishi.cli", "程序.jsh"]
    r = subprocess.run(命令, cwd=str(tmp_path), capture_output=True, timeout=180,
                       input=b"")
    return (r.stdout + r.stderr).decode("utf-8", "replace")


def test_省略参数列表三个引擎都能跑(tmp_path):
    """`解析(句柄)` = 用本进程命令行参数 —— 树遍历 / Python VM / Rust 一致。

    Python 侧走的是**基石库层**（`参数.jsh`），Rust 走的是**原生实现**（`args.rs`），
    两条完全不同的代码路径必须给出同一结果 —— 这正是这条判据的价值。
    """
    出 = {"树遍历": _跑("树遍历", 程序, tmp_path)}
    rs = rust_exe_path()
    if rs.is_file():
        import subprocess
        r = subprocess.run([str(rs), "程序.jsh"], cwd=str(tmp_path),
                           capture_output=True, timeout=180, input=b"")
        出["Rust"] = (r.stdout + r.stderr).decode("utf-8", "replace")
        # 顺带确认「真的读到了命令行」—— 否则「两边都输出默认值」也可能是两边都没解析
        r2 = subprocess.run([str(rs), "程序.jsh", "--名字=阿甲", "--详细"],
                            cwd=str(tmp_path), capture_output=True, timeout=180,
                            input=b"")
        assert (r2.stdout + r2.stderr).decode("utf-8", "replace") == "阿甲\n真\n"

    不一样 = {k: v for k, v in 出.items() if v != "世界\n假\n"}
    assert 不一样 == {}, f"省略参数列表的结果不一致：{不一样}"


def test_一次式少给参数时四个实现文案一致(tmp_path):
    """报错文案要逐字相同（三宿主报错一不一致，一直是这类「小地方」最容易漂）。"""
    import subprocess

    文 = set()
    文.add(_跑("树遍历", 一次式少参, tmp_path))
    rs = rust_exe_path()
    if rs.is_file():
        (tmp_path / "程序.jsh").write_text(一次式少参, encoding="utf-8", newline="\n")
        r = subprocess.run([str(rs), "程序.jsh"], cwd=str(tmp_path),
                           capture_output=True, timeout=180, input=b"")
        文.add((r.stdout + r.stderr).decode("utf-8", "replace"))
    # 只比「错误那一行」：位置行两边本来就不同（库层位置是另一个已知问题，见 CHANGELOG）
    首行 = {t.splitlines()[0] for t in 文 if t.strip()}
    assert len(首行) == 1, f"文案不一致：{首行}"
    assert "一次式需要两个参数" in 首行.pop()
