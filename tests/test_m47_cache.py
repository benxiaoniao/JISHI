# -*- coding: utf-8 -*-
"""M47 ③：AST 缓存的测试。

缓存这东西的风险全在「**该失效的没失效**」——那会变成「改了代码却没生效」，
是最难查的一类问题。所以这里重点钉失效判据，而不只是钉「能存能取」。
"""

import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jishi import cache
from jishi.parser import parse
from jishi.tokenizer import tokenize

SRC = '令 甲 = 1\n打印(甲 + 1)\n'


@pytest.fixture()
def 隔离缓存(tmp_path, monkeypatch):
    """每个用例一个独立缓存目录，互不干扰。"""
    monkeypatch.setenv("JISHI_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("JISHI_NO_CACHE", raising=False)
    return tmp_path


def _解析(src=SRC, name="<t>"):
    lines = src.split("\n")
    return parse(tokenize(src, name), lines, name)


def test_存了能取回来(隔离缓存):
    程序 = _解析()
    assert cache.load(SRC, "<t>") is None          # 还没存
    cache.store(SRC, "<t>", 程序)
    assert cache.load(SRC, "<t>") is not None


def test_源码改了就取不到(隔离缓存):
    """**这条是缓存最要紧的失效判据**：内容变了必须重编译。"""
    cache.store(SRC, "<t>", _解析())
    assert cache.load(SRC, "<t>") is not None
    assert cache.load(SRC + "打印(2)\n", "<t>") is None


def test_文件名不同不串味(隔离缓存):
    cache.store(SRC, "<甲.jsh>", _解析())
    assert cache.load(SRC, "<甲.jsh>") is not None
    assert cache.load(SRC, "<乙.jsh>") is None


def test_坏缓存不炸只是没命中(隔离缓存):
    """缓存只是加速手段——坏了就当没缓存，**绝不能让程序跑挂**。"""
    cache.store(SRC, "<t>", _解析())
    for f in 隔离缓存.glob("*.pkl"):
        f.write_bytes("这不是 pickle".encode("utf-8"))
    assert cache.load(SRC, "<t>") is None


def test_格式版本不符要失效(隔离缓存, monkeypatch):
    cache.store(SRC, "<t>", _解析())
    monkeypatch.setattr(cache, "FORMAT", cache.FORMAT + 1)
    assert cache.load(SRC, "<t>") is None


def test_可以整块关掉(隔离缓存, monkeypatch):
    monkeypatch.setenv("JISHI_NO_CACHE", "1")
    assert cache.enabled() is False
    cache.store(SRC, "<t>", _解析())
    assert cache.load(SRC, "<t>") is None
    assert list(隔离缓存.glob("*.pkl")) == []


def test_清缓存(隔离缓存):
    cache.store(SRC, "<t>", _解析())
    cache.store(SRC, "<u>", _解析())
    assert len(list(隔离缓存.glob("*.pkl"))) == 2
    assert cache.clear() == 2
    assert cache.load(SRC, "<t>") is None


def test_cli_冷热两次跑出一样的结果(隔离缓存, tmp_path):
    """端到端：第一次冷（写缓存）、第二次热（读缓存），输出必须**逐字节相同**。

    这是「缓存有没有把 AST 弄坏」的总验收——包装错了字段，
    程序可能还能跑但结果不对。
    """
    from jishi import cli

    源 = tmp_path / "示例.jsh"
    源.write_text('令 甲 = []\n'
                  '遍历 i 在 范围(30)：\n'
                  '    甲.追加(i * i)\n'
                  '打印(长度(甲), 甲[29])\n',
                  encoding="utf-8")

    def 跑():
        b = io.StringIO()
        with redirect_stdout(b):
            rc = cli.run_file(str(源))
        return rc, b.getvalue()

    冷 = 跑()
    assert list(隔离缓存.glob("*.pkl")), "第一次跑完应该留下缓存"
    热 = 跑()
    assert 冷 == 热, f"冷/热结果不一致：{冷!r} vs {热!r}"


def test_cli_no_cache_开关不写缓存(隔离缓存, tmp_path):
    from jishi import cli

    源 = tmp_path / "示例.jsh"
    源.write_text('打印(1)\n', encoding="utf-8")
    b = io.StringIO()
    with redirect_stdout(b):
        cli.main(["--no-cache", str(源)])
    assert list(隔离缓存.glob("*.pkl")) == []
