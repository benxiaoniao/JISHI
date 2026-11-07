# -*- coding: utf-8 -*-
"""M116：语言卡**带签名 + 可分片**（路线图 §12.2.1，原计划在 0.2.5）。

为什么单独立一条：语言卡是模型**唯一**会看到的那段事实（贴进系统提示词），
它错了模型就照错的写。M45 起卡片的标准库段**只列函数名** —— 模型得猜参数顺序，
还看不出哪个参数能省；标准库一补大，这个洞同比放大。

这一份盯五件事：

1. **每个函数都带签名**（含默认值）—— 且签名里的默认值**不许漏内部常量名**
   （`内容类型=_默认内容类型` 这种，模型看了只会一脸问号）；
2. **分片真的分得开**：单模块卡只有那一个模块、任务卡有那几个模块 + 该任务惯用法；
3. **它是 `build_lang_spec()` 的投影**（加模块/加函数不用改渲染代码）；
4. **告诉模型「别跳过中间的可选参数」** —— 那是**既有的五引擎分叉**
   （`AGENTS.md` §3.3 ①：宿主只认必填个数），卡片不能教一个在自己宿主上会报错的写法；
5. 命令行三个形态（`--ai-card` / `--ai-card 模块` / `--任务` / `--统计`）+ 手滑保护。
"""

import io
import json
import re
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oracle.jishi.ai import (build_lang_spec, content_hash, 卡_任务表,  # noqa: E402
                             render_ai_card)

PY = sys.executable


def _卡(**kw) -> str:
    return render_ai_card(**kw)


# ---------------------------------------------------------------------------
# 一、签名：带默认值、不露内部名
# ---------------------------------------------------------------------------

def test_每个函数都有签名():
    卡 = _卡()
    spec = build_lang_spec()
    总数 = sum(len(m["functions"]) for m in spec["stdlib"])
    行 = [x for x in 卡.splitlines() if x.startswith("- `") and "(" in x]
    # 关键字/内建那些也是 `- \`` 开头，但内建行是 `- \`名字\`：说明`，没有 `(`
    assert len([x for x in 行 if x.startswith("- `") and ")`" in x]) >= 总数, (
        f"标准库 {总数} 个函数，卡片里只找到 {len(行)} 条带签名的行")


def test_签名带默认值():
    """没有默认值时模型不知道哪个参数能省 —— 这是本条的**全部意义**。"""
    卡 = _卡()
    assert "`居中(文本, 宽度, 填充=' ')`" in 卡
    assert "`文件摘要(文件路径, 算法='sha256')`" in 卡
    assert "`启动(端口=8000, 处理函数=空, 最多请求数=0)`" in 卡


def test_签名不露内部常量名():
    """🔴 默认值是从 AST 反解的 ⇒ 写成常量名会把**内部变量名**漏进卡片。

    踩到过：`构造响应(状态=200, 正文='', 内容类型=_默认内容类型, 头=None)`、
    `口令摘要(口令, 盐, 迭代=PBKDF2_默认迭代, 算法='sha256')` —— 模型看到
    只会一脸问号。标准库那边已改写字面量，这条钉住别写回去。
    """
    spec = build_lang_spec()
    坏 = []
    for m in spec["stdlib"]:
        for f in m["functions"]:
            for 默认 in re.findall(r"=([^,)]+)", f["sig"]):
                默认 = 默认.strip()
                # 合法默认值：数字（含 1e-09）/ 引号字符串 / 真 / 假 / 空 / `…`
                # `…` = 「有默认值，但值认不出来」—— 不猜，也不漏内部名字
                if re.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?|'[^']*'|\"[^\"]*\"|真|假|空|…", 默认):
                    continue
                坏.append(f"{m['module']}.{f['sig']}")
    assert not 坏, f"这些签名的默认值是**名字**（内部常量漏出来了）：{坏}"


def test_语言卡_与_规格_同一份指纹():
    卡 = _卡()
    assert content_hash(build_lang_spec()) in 卡, "卡片头上要带语言规格指纹"


def test_告诉模型别跳可选参数():
    """⚠️ 宿主只认「必填个数」（`AGENTS.md` §3.3 ①）⇒ `f(1, c=3)` 在
    Python 侧能跑、在**发行版上报错**。卡片**不能**教这种写法。"""
    卡 = _卡()
    assert "按顺序**传" in 卡 or "按顺序" in 卡
    assert "别跳" in 卡


# ---------------------------------------------------------------------------
# 二、分片
# ---------------------------------------------------------------------------

def test_单模块卡只有那个模块():
    卡 = _卡(模块="加密")
    块 = re.findall(r"^### `(.+?)`$", 卡, flags=re.M)
    assert 块 == ["加密"], 块
    # ⚠️ 别拿子串判「没别的模块」——「长度」也是 `安全随机字节(长度)` 的一部分！
    #    判据就看模块标题，那是唯一的真相。
    assert "文件摘要" in 卡
    assert "### `文本`" not in 卡 and "### `表格`" not in 卡


def test_任务卡带那几个模块与惯用法():
    卡 = _卡(任务="表格")
    块 = set(re.findall(r"^### `(.+?)`$", 卡, flags=re.M))
    assert 块 == set(卡_任务表["表格"]), 块
    assert "「表格」这类活的惯用法" in 卡
    assert "表格.汇总(表," in 卡          # 惯用法真进了卡里


def test_分片确实更小():
    全 = len(_卡())
    模 = len(_卡(模块="文本"))
    任 = len(_卡(任务="表格"))
    assert 模 < 全 and 任 < 全
    assert 模 < 全 * 0.5, f"单模块卡该小得多：{模} vs 全 {全}"


def test_每个任务包都能出卡():
    for 名 in 卡_任务表:
        卡 = _卡(任务=名)
        assert "「" + 名 + "」这类活的惯用法" in 卡
        assert "## 关键字（全部）" in 卡        # 分片也要带语法，否则模型没法写


def test_模块与任务不能同时给():
    with pytest.raises(ValueError):
        _卡(模块="文本", 任务="表格")


def test_写错了当场说清():
    with pytest.raises(ValueError, match="没有.*这个标准库模块"):
        _卡(模块="没这个模块")
    with pytest.raises(ValueError, match="没有.*这个任务包"):
        _卡(任务="没这个任务")


# ---------------------------------------------------------------------------
# 三、是投影，不是另写一套
# ---------------------------------------------------------------------------

def test_卡片里的模块与函数都来自语言规格():
    """`AGENTS.md` §0.5 核心二：`--ai-card` 永远是 `--lang-spec` 的投影。"""
    spec = build_lang_spec()
    卡 = _卡()
    for m in spec["stdlib"]:
        assert f"### `{m['module']}`" in 卡, m["module"]
        for f in m["functions"]:
            assert f"- `{f['sig']}`" in 卡, f["sig"]
    for b in spec["builtins"]:
        assert f"- `{b['name']}`：" in 卡, b["name"]


# ---------------------------------------------------------------------------
# 四、命令行
# ---------------------------------------------------------------------------

def _cli(*参数) -> tuple:
    r = subprocess.run([PY, "-m", "oracle.jishi.cli", *参数],
                       capture_output=True, text=True, encoding="utf-8",
                       cwd=ROOT, timeout=180)
    return r.stdout, r.stderr, r.returncode


def test_命令行三种形态():
    出, _, 码 = _cli("--ai-card")
    assert 码 == 0 and "## 标准库" in 出
    出2, _, 码2 = _cli("--ai-card", "加密")
    assert 码2 == 0
    assert len(出2) < len(出) and "### `加密`" in 出2
    出3, _, 码3 = _cli("--ai-card", "--任务", "网络")
    assert 码3 == 0 and "「网络」这类活的惯用法" in 出3


def test_命令行统计():
    出, _, 码 = _cli("--ai-card", "--统计")
    assert 码 == 0
    行 = [x for x in 出.splitlines() if "\t" in x]
    assert any(x.startswith("全量\t") for x in 行)
    大小 = {x.split("\t")[0]: int(x.split("\t")[1]) for x in 行}
    assert 大小["全量"] > 大小["任务:表格"] > 大小["模块:文本"]


def test_命令行手滑保护():
    """`--ai-card 脚本.jsh` 里的位置会被吃成「模块名」—— 必须**说清**，
    不能只报一句「没有这个模块」让人以为卡坏了。"""
    出, 错, 码 = _cli("--ai-card", "某脚本.jsh")
    assert 码 == 2
    assert "模块名" in 错 and "jishi" in 错


def test_命令行写错模块有中文提示():
    出, 错, 码 = _cli("--ai-card", "没这个模块")
    assert 码 == 2 and "没有「没这个模块」这个标准库模块" in 错
