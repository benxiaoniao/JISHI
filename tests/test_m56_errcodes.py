# -*- coding: utf-8 -*-
"""M56 第 7 条：错误码索引（`jishi 错误 <码>`）的守门测试。

钉住两个「不许各写一份」的约束：

- `errors.py` 里的**每个码**都要在 `errcodes.INFO` 里有说明；
- `errcodes.INFO` 里**不许有** `errors.py` 没有的码。

于是「加了错误类型忘了写说明」和「删了错误类型忘了删说明」都会报红 ——
与项目里别的「两份会漂移的事实」用测试钉住是同一个手法（M51/M54/M55）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from oracle.jishi import errcodes, errors  # noqa: E402
from oracle.jishi.cli import _all_error_codes, main  # noqa: E402


def test_每个错误码都有说明():
    declared = dict(_all_error_codes())
    missing = sorted(set(declared) - set(errcodes.INFO))
    assert not missing, (
        f"这些错误码还没有说明（补 oracle/jishi/errcodes.py）：{'、'.join(missing)}")


def test_说明里没有多余的码():
    declared = dict(_all_error_codes())
    extra = sorted(set(errcodes.INFO) - set(declared))
    assert not extra, (
        f"这些说明对应的错误码在 errors.py 里不存在（删掉它们）：{'、'.join(extra)}")


def test_每条说明三段齐全():
    """「什么意思 / 常见成因 / 怎么改」缺一不可 —— 缺了就等于没查。"""
    for code, info in sorted(errcodes.INFO.items()):
        assert info.get("what", "").strip(), f"{code} 少了「什么意思」"
        why = info.get("why") or []
        assert why, f"{code} 少了「常见成因」"
        assert all(str(x).strip() for x in why), f"{code} 的成因有空项"
        assert info.get("fix", "").strip(), f"{code} 少了「怎么改」"


def test_说明不该夹带_markdown():
    """CLI 输出**不渲染 markdown** —— `**强调**` 会字面显示，很难看。"""
    bad = []
    for code, info in sorted(errcodes.INFO.items()):
        blob = " ".join([info.get("what", ""), info.get("fix", ""),
                         *[str(x) for x in info.get("why") or []]])
        if "**" in blob:
            bad.append(code)
    assert not bad, f"这些说明里有 markdown 加粗（改成「」）：{'、'.join(bad)}"


def test_总览列出所有码(capsys):
    assert main(["错误"]) == 0
    out = capsys.readouterr().out
    for code, title in _all_error_codes():
        assert code in out, f"总览里没有 {code}"
        assert title in out, f"总览里没有 {code} 的标题"


def test_查单个码给三段(capsys):
    assert main(["错误", "E0301"]) == 0
    out = capsys.readouterr().out
    assert "E0301" in out and "找不到这个名字" in out
    for section in ("什么意思", "常见成因", "怎么改"):
        assert section in out, f"少了「{section}」那节"


def test_码的写法容错(capsys):
    """用户从报错里抄的时候可能只抄数字、或漏了前导零 / 大小写不同。"""
    outs = []
    for arg in ("E0301", "e0301", "301", "E301", "  E0301  "):
        assert main(["错误", arg]) == 0, f"「{arg}」查不到"
        outs.append(capsys.readouterr().out)
    assert len(set(outs)) == 1, "几种写法给的内容应当完全一样"


def test_不存在的码给非零退出(capsys):
    assert main(["错误", "E9999"]) == 2
    err = capsys.readouterr().err
    assert "E9999" in err and "jishi 错误" in err


def test_内部信号被标成内部():
    """`E2997/8/9` 是解释器内部的信号，说明里要明说「正常不该看到」。"""
    for code in ("E2997", "E2998", "E2999"):
        info = errcodes.INFO[code]
        assert "内部" in info["what"], f"{code} 的说明没提「内部」"
