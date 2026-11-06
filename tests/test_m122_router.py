# -*- coding: utf-8 -*-
"""M122：`HTTP路由` —— 路由匹配。

**匹配语义**的判据主要在语料 `tests/cases/40_标准库_M122.jsh` 里（那样才走五引擎对拍，
三个宿主逐字节一致）。这个文件只补两件语料管不到的事：

1. **示例站能不能编译** —— `examples/projects/` 下的东西**没有**被 `test_cvm` 扫到
   （它只 `glob("*.jsh")` 顶层），所以示例语法错了没人知道。示例是给用户抄的，
   抄到一个跑不起来的例子比没有例子更糟（M41 的教训：few-shot 里躺过 4 段真错）。
2. **模块门面**：4 个函数都在 `__all__` 里（否则语言卡上看不见）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from oracle.jishi.parser import parse_source  # noqa: E402


def test_示例网站能编译():
    """`examples/projects/小网站/网站.jsh` 至少要能过解析。

    ⚠️ 只编译不执行 —— 它最后一行是 `HTTP服务端.启动(…)`，跑起来会**阻塞**。
    """
    文件 = ROOT / "examples" / "projects" / "小网站" / "网站.jsh"
    assert 文件.is_file(), 文件
    parse_source(文件.read_text(encoding="utf-8"), str(文件))


def test_模块门面():
    """4 个函数都要在 `__all__` 里（`--lang-spec` / `--ai-card` 读的就是它）。"""
    import importlib

    m = importlib.import_module("oracle.jishi.stdlib.HTTP路由")
    assert set(m.__all__) == {"路由表", "加路由", "匹配", "路径们"}


def test_匹配的边界语义():
    """几条最容易写错、又**只在边界上**体现的规则（语料里也有，这里快一步红）。"""
    import importlib

    R = importlib.import_module("oracle.jishi.stdlib.HTTP路由")

    表 = R.路由表()
    R.加路由(表, "get", "/用户/:id", lambda q, p: "参数")
    R.加路由(表, "GET", "/用户/新建", lambda q, p: "静态")

    # ① 方法不分大小写
    assert R.匹配(表, "GeT", "/用户/新建") is not None
    # ② 静态优先于参数（即使参数那条先注册）
    assert R.匹配(表, "GET", "/用户/新建")["处理"](None, None) == "静态"
    # ③ 参数段抽得出来
    assert R.匹配(表, "GET", "/用户/7")["参数"] == {"id": "7"}
    # ④ 末尾斜杠严格区分：`/用户/` 的 id 是空的 ⇒ 不算匹配
    assert R.匹配(表, "GET", "/用户/") is None
    # ⑤ 段数不同就是不匹配
    assert R.匹配(表, "GET", "/用户/1/2") is None
    # ⑥ 没命中返回 空（None），**不抛错** —— 404 是正常流程
    assert R.匹配(表, "GET", "/没有") is None
