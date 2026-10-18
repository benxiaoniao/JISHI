# -*- coding: utf-8 -*-
"""M121：报错要**可操作**（核心二），以及「模块门面」只能是 `__all__`。

这一轮改了两件事，表面看不相干，其实是一个：

1. **`模块.名字` 找不到时，提示要能照做。** 以前那句是
   「可以看看模块里有哪些函数，例如：打印(dir(模块名)) 在后续版本可用」——
   `dir` 根本还没实现，等于**空头承诺**。现在分两种：
   - 这个名字其实是某个**类型的方法**（`文本.去空白` / `容器.追加`）⇒ 点出来。
     这是最坑的一类错：函数确实「存在」，只是不能这么调；
   - 真的没有 ⇒ 把模块里**有的**列出来（排好序）。

2. **模块成员 == `__all__`。** 以前是 `vars(mod)` 去掉下划线开头，于是模块里
   `import` 进来的实现细节**全都成了模块成员** —— 实测 **28 个模块无一例外**都漏
   （`文本.RunTypeError`、`json.annotations`、`表格.显示宽度`、`HTTP服务端.socket`…）。

   两处后果：① `--lang-spec` / `--ai-card`（读 `__all__`）说 21 个函数、
   运行期能拿 24 个 —— **投影与实现不一致**；② 上面那条提示会列出 `annotations`。
   ⇒ 两件事在同一个入口上，一起修。

判据的分布：
- **语料**（`error_cases/76~79`）钉**文案**，并进五引擎对拍（三边逐字一致）；
- **本文件**钉**契约**（每个模块都声明 `__all__`、里面没有笔误、运行期不超出）——
  这些语料抓不到，因为语料只用到了一部分模块。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from oracle.jishi import runtime  # noqa: E402

标准库目录 = ROOT / "oracle" / "jishi" / "stdlib"


def _模块们() -> list[str]:
    return sorted(
        p.stem for p in 标准库目录.glob("*.py") if p.stem != "__init__"
    )


def test_每个标准库模块都声明了_all():
    """每个模块都必须有非空的 `__all__`（它是**唯一**的门面来源）。

    ⚠️ 这条是给「以后新加模块」守的：漏写 `__all__` 时 `do_import` 会退回
    老行为（`vars` 去下划线），泄漏**不会报错**、只会静默回来。
    """
    缺 = []
    for 名 in _模块们():
        mod = importlib.import_module(f"oracle.jishi.stdlib.{名}")
        导出 = getattr(mod, "__all__", None)
        if not 导出:
            缺.append(名)
    assert not 缺, f"这些模块没声明 `__all__`（或为空）：{缺}"


def test_有状态模块走钩子而不是_vars():
    """`__基石新建__` 的返回值是这三个模块的**真身**，不是 `vars(mod)`。

    ⚠️ 这条是给「按 `__all__` 过滤」那个改动守的**边界**：
    `数据库` / `日志` / `测试` 的 `__all__` 里那些名字（`打开`、`信息`、`用例`…）
    在 `vars(mod)` 里**根本不存在** —— 它们在 `__基石新建__()` 内部定义，
    每次导入新建一份（否则同一进程连跑两个执行器会串，M41 栽过）。

    ⇒ 光按 `__all__` 去 `vars(mod)` 里取，这三个模块会变成**空模块**；
       实际能跑，是因为 `do_import` 里钩子**在 attrs 之后**覆盖了它。
       这条判据把「钩子必须还在、且 `__all__` 与它一致」钉住 ——
       顺序一旦被谁调换，这里立刻红。
    """
    钩子模块 = []
    for 名 in _模块们():
        mod = importlib.import_module(f"oracle.jishi.stdlib.{名}")
        hook = getattr(mod, "__基石新建__", None)
        if hook is None:
            continue
        钩子模块.append(名)
        应 = set(getattr(mod, "__all__", None) or [])
        实 = set(hook())
        assert 应 == 实, (
            f"{名}：`__基石新建__()` 给的键与 `__all__` 不一致 —— "
            f"多 {sorted(实 - 应)} / 少 {sorted(应 - 实)}")
    # 这三个是已知的（M41 起按导入隔离状态）；少了说明钩子被人删了
    assert set(钩子模块) == {"数据库", "日志", "测试"}, 钩子模块


def test_运行期能拿到的与_all_完全一致():
    """`do_import` 造出来的模块成员 == `__all__`（一个不多、一个不少）。

    这是「投影与实现一致」的**运行期**判据：`--lang-spec` / `--ai-card` 读的是
    `__all__`，用户能访问的是 `Module.attrs` —— 两边必须相同。
    """
    坏 = []
    for 名 in _模块们():
        mod = importlib.import_module(f"oracle.jishi.stdlib.{名}")
        应 = set(getattr(mod, "__all__", None) or [])
        # `alias` 非空 ⇒ 不打「模块名遮蔽内建」那条提示（否则 28 个模块刷一屏）
        实 = set(runtime.do_import(名, alias="__m121_别名").attrs)
        if 应 != 实:
            多 = sorted(实 - 应)
            少 = sorted(应 - 实)
            坏.append(f"{名}: 多出来 {多} / 少了 {少}")
    assert not 坏, (
        "模块成员与 `__all__` 不一致（`do_import` 该按 `__all__` 过滤）：\n  - "
        + "\n  - ".join(坏)
    )


@pytest.mark.parametrize("名, 期望类型", [
    ("去空白", "文本"),
    ("拆分", "文本"),
    ("追加", "列表"),
    ("排序", "列表"),
    ("获取", "字典"),
    ("读", "文件"),
    ("舍入", "小数"),
])
def test_方法归属表认得出来(名, 期望类型):
    """`_找方法归属` 要能把方法名对回类型（提示就是靠它）。

    ⚠️ 这份映射是 `do_import` 那条提示的**唯一数据源**，Rust / Node 各有自己的
    一份（`langdata::METHODS` / `LIST_METHODS…`），三边必须给同一个答案 ——
    那层由语料 `76/77` 走五引擎对拍钉住；这里钉 Python 侧这张表本身。
    """
    assert runtime._找方法归属(名) == 期望类型


def test_不是方法的名字归属为空():
    """模块函数名不该被误判成方法 —— 否则提示会指错路。

    `长度` 既是内建函数，也没在任何类型的方法表里 —— 拿它当反例最稳。
    """
    for 名 in ("长度", "打印", "压根没有这个"):
        assert runtime._找方法归属(名) == "", 名
