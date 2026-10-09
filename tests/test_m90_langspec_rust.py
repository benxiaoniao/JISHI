# -*- coding: utf-8 -*-
"""R7.2：语言元数据搬进 Rust —— `jishi-rs --lang-spec` / `--ai-card`。

两半（用户 2026-10-05 定的纪律：**Rust 最终要替代 Python，判据不能只押在对拍上**）：

* **对拍**（§一~§二）：与 `jishi --lang-spec` / `jishi --ai-card` **逐字节一致**，
  外加一条**不经过 CLI 包装**的结构化对比（直接比 `oracle.jishi.ai.build_lang_spec()`）；
* **独立判据**（§三）：不 import oracle.jishi、不 spawn Python 的**写死期望** ——
  R8 之后 Python 退场，这一半仍要绿。

R7.2 的验收原文就是「**JSON 逐字节一致 + 漂移检测**」（拍板①：Rust 侧显式表 +
两侧各 `--dump` 自报家底来比）。
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _rust() -> Path:
    name = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
    return ROOT / "rust" / "target" / "release" / name


def _need_rust(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not _rust().exists():
            pytest.skip("Rust 宿主未构建（cd rust && cargo build --release）")
        return fn(*args, **kwargs)
    return wrapper


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    return env


def _rust_run(*argv: str) -> tuple[str, str, int]:
    r = subprocess.run([str(_rust()), *argv], capture_output=True, input=b"",
                       env=_env(), cwd=str(ROOT))
    return (r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"), r.returncode)


def _py_run(*argv: str) -> tuple[str, str, int]:
    r = subprocess.run([sys.executable, "-m", "oracle.jishi.cli", *argv],
                       capture_output=True, input=b"", env=_env(), cwd=str(ROOT))
    return (r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"), r.returncode)


def _first_diff(a: str, b: str) -> str:
    la, lb = a.split("\n"), b.split("\n")
    for i in range(max(len(la), len(lb))):
        x = la[i] if i < len(la) else "<无>"
        y = lb[i] if i < len(lb) else "<无>"
        if x != y:
            return f"第 {i + 1} 行不同：\n  rust: {x!r}\n  py  : {y!r}"
    return f"行数相同但整体不同：rust {len(a)} 字符 / py {len(b)} 字符"


# ---------------------------------------------------------------------------
# 一、`--lang-spec` 与 `--ai-card` 逐字节一致
# ---------------------------------------------------------------------------


@_need_rust
def test_lang_spec逐字节一致():
    a = _rust_run("--lang-spec")
    b = _py_run("--lang-spec")
    assert a[2] == 0 and b[2] == 0, (a[2], b[2], a[1][:200], b[1][:200])
    assert a[0] == b[0], _first_diff(a[0], b[0])
    assert a[0].startswith('{\n  "version": "'), a[0][:80]


@_need_rust
def test_ai_card逐字节一致():
    a = _rust_run("--ai-card")
    b = _py_run("--ai-card")
    assert a[2] == 0 and b[2] == 0, (a[2], b[2], a[1][:200], b[1][:200])
    assert a[0] == b[0], _first_diff(a[0], b[0])


# ---------------------------------------------------------------------------
# 二、漂移检测：不经过 CLI 包装，直接比 Python 侧的事实源
# ---------------------------------------------------------------------------


@_need_rust
def test_漂移检测_与build_lang_spec逐字段相等():
    """`jishi-rs --lang-spec` 解析出来必须**整份等于**
    `build_lang_spec() + content_hash`（含 `content_hash` 的算法）。

    比字符串那两条更强的一点：它不依赖 CLI 的包装形式 —— 就算哪天两边**都**改成了
    另一种排版，内容对不对仍然查得出来。
    """
    from oracle.jishi.ai import build_lang_spec, content_hash

    spec = build_lang_spec()
    spec["content_hash"] = content_hash(spec)

    r = _rust_run("--lang-spec")
    assert r[2] == 0, r[1]
    got = json.loads(r[0])
    assert got == spec, _diff_json(got, spec)


def _diff_json(got, want) -> str:
    """找出第一处不同的字段（便于定位，而不是甩一整份 JSON）。"""
    if isinstance(got, dict) and isinstance(want, dict):
        if list(got) != list(want):
            return f"键顺序不同：\n  rust: {list(got)}\n  py  : {list(want)}"
        for k in want:
            if got[k] != want[k]:
                return f"「{k}」不同：\n  rust: {str(got[k])[:200]}\n  py  : {str(want[k])[:200]}"
        return "（无差异）"
    if isinstance(got, list) and isinstance(want, list):
        if len(got) != len(want):
            return f"长度不同：{len(got)} vs {len(want)}"
        for i, (x, y) in enumerate(zip(got, want)):
            if x != y:
                return f"[{i}] 不同：\n  rust: {str(x)[:200]}\n  py  : {str(y)[:200]}"
        return "（无差异）"
    return f"不同：{str(got)[:200]} vs {str(want)[:200]}"


# ---------------------------------------------------------------------------
# 三、独立判据（**不 import oracle.jishi、不 spawn Python** —— R8 之后仍要绿）
# ---------------------------------------------------------------------------

#: 顶层键的**顺序**也是内容的一部分（Python 的 dict 保序）。
_TOP_KEYS = ["version", "keywords", "operators", "builtins", "methods", "stdlib",
             "exceptions", "error_codes", "check_codes", "python_equiv",
             "content_hash"]

#: 内建函数表（注册顺序 —— 顺序会进语言卡）。
_BUILTINS = ["打印", "输入", "整数", "小数", "文本", "精确", "序数", "字符", "打开",
             "进入上下文", "退出上下文", "长度", "范围", "带下标", "配对", "最大",
             "最小", "总和", "类型", "位与", "位或", "位异或", "位取反", "左移",
             "右移", "反转", "断言", "是实例", "超", "集合"]

_METHOD_CATS = ["列表", "字典", "文本", "集合", "文件", "小数"]

_STDLIB_MODULES = ["html", "json", "加密", "压缩", "参数", "容器", "对比", "数学",
                   "文件", "文本", "日志", "日期", "时间", "标识", "正则", "测试",
                   "系统", "统计", "编码", "网络", "表格", "路径", "迭代", "配置",
                   "随机"]

_EXCEPTIONS = ["异常", "运行期错误", "类型错误", "值错误", "索引错误", "键错误",
               "除零错误", "文件错误", "断言错误"]

_CHECK_CODES = ["name.undefined", "name.shadow", "name.unused", "compare.self",
                "check.read"]


@_need_rust
def test_独立判据_lang_spec结构与关键事实():
    r = _rust_run("--lang-spec")
    assert r[2] == 0 and r[1] == "", (r[2], r[1][:200])
    d = json.loads(r[0])

    assert list(d) == _TOP_KEYS, f"顶层键或顺序变了：{list(d)}"
    assert d["version"] == "0.2.2", d["version"]
    assert len(d["keywords"]) == 36 and list(d["keywords"])[0] == "令"
    assert d["keywords"]["令"] == "声明/赋值变量"
    assert [b["name"] for b in d["builtins"]] == _BUILTINS
    assert d["builtins"][0]["doc"] == "输出内容到屏幕"
    assert list(d["methods"]) == _METHOD_CATS
    assert [m["module"] for m in d["stdlib"]] == _STDLIB_MODULES
    assert sum(len(m["functions"]) for m in d["stdlib"]) == 229
    assert d["exceptions"] == _EXCEPTIONS
    assert [c["code"] for c in d["check_codes"]] == _CHECK_CODES
    assert len(d["error_codes"]) == 35
    assert d["error_codes"] == sorted(d["error_codes"], key=lambda e: e["code"])
    # content_hash 是 8 位小写十六进制（值本身随元数据变化，不写死 —— 写死了每次
    # 改文案都要回来改它，而它在这里起不到判据作用）。
    assert len(d["content_hash"]) == 8
    assert all(c in "0123456789abcdef" for c in d["content_hash"])


@_need_rust
def test_独立判据_lang_spec的content_hash对得上():
    """`content_hash` = `sha256(紧凑 + 键排序的 JSON)` 前 8 位 —— **在这里独立重算一遍**。

    ⚠️ 这条不 import oracle.jishi：用的是 Python 标准库的 `json` + `hashlib`（R8 之后
    Python 退场了，这个测试文件也就退休了；但在那之前它能独立验证摘要算法）。
    """
    import hashlib

    d = json.loads(_rust_run("--lang-spec")[0])
    got = d.pop("content_hash")
    blob = json.dumps(d, ensure_ascii=False, sort_keys=True)
    want = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]
    assert got == want, f"content_hash 对不上：{got} vs {want}"


@_need_rust
def test_独立判据_ai_card关键段():
    r = _rust_run("--ai-card")
    assert r[2] == 0 and r[1] == "", (r[2], r[1][:200])
    out = r[0]
    assert out.startswith("# 基石（jishi）语言卡 v0.2.2（"), out[:60]
    for seg in ["## 关键字（全部）", "## 内建函数", "## 标准库（`导入 模块名`）",
                "## 对象方法", "## 运算符", "## 语法速览", "## 惯用法示例",
                "## 常见错误对照（写中文，别写英文）", "## 报错怎么读、怎么查（M56）"]:
        assert seg in out, f"语言卡缺了「{seg}」"
    assert "- `打印`：输出内容到屏幕" in out
    assert "- `导入 数学`：" in out
    assert "- 列表：追加、插入、" in out
    # 模板自带的收尾换行 + `print` 那次换行 → 末尾是两个 `\n`（与 Python 侧同）。
    assert out.endswith("（含码、行列、修法建议）。\n\n"), repr(out[-40:])


@_need_rust
def test_独立判据_没有PATH也能跑():
    """清空环境也要能出元数据 —— 全靠二进制自己，不 shell out。"""
    env = {"SystemRoot": os.environ.get("SystemRoot", "C:\\Windows")} \
        if os.name == "nt" else {}
    r = subprocess.run([str(_rust()), "--lang-spec"], capture_output=True,
                       input=b"", env=env, cwd=str(ROOT))
    assert r.returncode == 0
    d = json.loads(r.stdout.decode("utf-8"))
    assert d["version"] and d["exceptions"] == _EXCEPTIONS
