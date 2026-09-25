# -*- coding: utf-8 -*-
"""M8 AI 原生工具链：机器可读语言规格、AI 语言卡、结构化错误。

数据源统一从代码里动态收集（tokenizer/parser/runtime/stdlib），
避免手写与实现脱节。语言卡 / llms.txt / JSON 报错都从这里派生。
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from typing import Any, Optional

from . import parser as _parser
from . import tokenizer as _tokenizer
from .errors import EXCEPTION_TYPES, JishiError
from .runtime import (
    DICT_METHODS,
    LIST_METHODS,
    STR_METHODS,
    is_internal_builtin,
    new_builtins,
)

from .version import VERSION

# 关键字身份 → 中文说明（语言卡 / 语言规格共用）
KEYWORD_DESC = {
    "令": "声明/赋值变量", "如果": "条件判断", "否则": "否则分支",
    "否则如果": "否则如果分支", "遍历": "for 循环", "循环": "循环 N 次",
    "当": "while 循环", "中断": "跳出循环", "继续": "跳过本次循环",
    "函数": "定义函数", "返回": "返回值", "导入": "导入模块",
    "在": "遍历 x 在 列表", "从": "从 python 导入", "为": "导入别名",
    "真": "布尔真", "假": "布尔假", "空": "空值 None",
    "与": "逻辑与", "或": "逻辑或", "非": "逻辑非", "次": "循环次数助词",
    "类": "定义类", "继承": "类继承", "新建": "实例化",
    "尝试": "异常处理", "捕获": "捕获异常", "最终": "finally", "抛出": "抛出异常",
}

# 运算符 → 中文说明
OP_NAME = {
    "+": "加", "-": "减", "*": "乘", "/": "除", "//": "整除",
    "%": "取余", "**": "乘方", "==": "等于", "!=": "不等于",
    "<": "小于", ">": "大于", "<=": "小于等于", ">=": "大于等于",
    "或": "逻辑或", "与": "逻辑与",
}

# ---------------------------------------------------------------------------
# 等价 Python 写法（M44）
#
# 为什么要有这块：设计核心二要求「语言事实机器可读」。模型学过的 Python 是它
# 最大的先验，把基石写法**显式映射**到 Python 心智模型，比让它自己猜准得多
# （`与`/`或`/`非` 不是 and/or/not 的话，它一定会猜错）。
#
# 只列**确实对应**的；对应关系有差异的一律在文本里点明（如 `反转`、`集合`），
# 不写成「等于 Python 的 X」这种半真话。
# ---------------------------------------------------------------------------

#: 关键字 → 等价 Python
PY_KEYWORD = {
    "令": "=", "如果": "if", "否则": "else", "否则如果": "elif",
    "遍历": "for", "循环": "for _ in range(N)", "当": "while",
    "中断": "break", "继续": "continue",
    "次": "（助词：`循环 N 次` → range(N)）",
    "函数": "def", "返回": "return", "导入": "import", "从": "from",
    "在": "in（`遍历 x 在 列表` → for x in 列表）", "为": "as",
    "真": "True", "假": "False", "空": "None",
    "与": "and", "或": "or", "非": "not",
    "类": "class", "继承": "class 名(基类)", "新建": "类名(...)",
    "尝试": "try", "捕获": "except", "最终": "finally", "抛出": "raise",
}

#: 运算符 → 等价 Python（符号运算符 + 关键字形式的比较）
PY_OP = {
    "+": "+", "-": "-", "*": "*", "/": "/", "//": "//", "%": "%", "**": "**",
    "==": "==", "!=": "!=", "<": "<", ">": ">", "<=": "<=", ">=": ">=",
    "=": "=", "+=": "+=", "-=": "-=", "*=": "*=", "/=": "/=",
    "//=": "//=", "%=": "%=", "**=": "**=",
    "在": "in", "不在": "not in", "是": "is", "不是": "is not",
    "与": "and", "或": "or", "非": "not",
}

#: 内建 → 等价 Python。只列**有一一对应**的：`进入上下文` / `退出上下文` 是
#: `with` 协议的一部分，Python 里没有同名内建，所以不列（列了就是在编）。
PY_BUILTIN = {
    "打印": "print", "输入": "input", "整数": "int", "小数": "float",
    "文本": "str", "精确": "decimal.Decimal", "打开": "open",
    "长度": "len", "范围": "range", "带下标": "enumerate", "配对": "zip",
    "最大": "max", "最小": "min", "总和": "sum", "类型": "type",
    "是实例": "isinstance", "超": "super()", "断言": "assert",
    # 下面两条与 Python 有语义差异，点明它：
    "反转": "reversed(列表) / 文本[::-1]（Python 的 reversed 不吃文本）",
    "集合": "set(可迭代)（Python 的 set 无序；基石这个保持首次出现顺序）",
    "位与": "&", "位或": "|", "位异或": "^", "位取反": "~",
    "左移": "<<", "右移": ">>",
}


def _error_codes() -> list[dict]:
    """错误码表（M44）：**从 `errors.py` 上收集**，不另抄一份。

    含少数「运行期临时挂上」的码（`errors.EXTRA_CODES`，它们没有独立异常类）。
    `internal` 为真的是执行器内部信号（E299x：中断/继续/迭代结束）——不展示给
    用户，但工具做协议转换时真的会碰到（DAP 与 MCP 都在转发），所以照实列出来。

    每条只给 `code / class / title`：**更细的修法建议不在这里**，它是随错误发生
    的上下文来的（`--json-errors` 的 `hint` 与 `fix`），硬写在表里只会写成空话。
    """
    import inspect

    from . import errors as _errors

    rows: list[dict] = []
    for cls_name, obj in vars(_errors).items():
        if not inspect.isclass(obj) or not issubclass(obj, Exception):
            continue
        if getattr(obj, "__module__", "") != _errors.__name__:
            continue                      # 只认本模块定义的，别把 import 进来的算上
        code = getattr(obj, "code", None)
        if not code:
            continue
        title = getattr(obj, "title", "") or ""
        rows.append({
            "code": code,
            "class": cls_name,
            "title": title,
            "internal": title.startswith("（内部）"),
        })
    for code, title in _errors.EXTRA_CODES.items():
        rows.append({"code": code, "class": "", "title": title, "internal": False})
    rows.sort(key=lambda r: r["code"])
    return rows


def _check_codes() -> list[dict]:
    """静态检查的问题码表（M44）——目录在 `checker.CHECK_CATALOG`，这里只做投影。"""
    from .checker import CHECK_CATALOG
    return [dict(item) for item in CHECK_CATALOG]


def _stdlib_dir() -> str:
    """stdlib 源码目录。打包后（PyInstaller）源文件在 _MEIPASS，源码树在包内。"""
    import os
    from . import stdlib as _stdlib_pkg
    # PyInstaller 单文件：解压到 sys._MEIPASS
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = os.path.join(meipass, "jishi", "stdlib")
        if os.path.isdir(cand):
            return cand
    return os.path.dirname(_stdlib_pkg.__file__)


def _函数签名(node: ast.FunctionDef) -> dict:
    params = [a.arg for a in node.args.args]
    sig = f"{node.name}({', '.join(params)})"
    doc = (ast.get_docstring(node) or "").strip().split("\n")[0]
    return {"name": node.name, "sig": sig, "doc": doc}


def _收集函数(节点们) -> list[dict]:
    """从一组语句里收「公开函数」。

    会**往 `__基石新建__` 里再看一层**：有状态模块（`日志` / `测试`）把函数
    定义在那个工厂函数内部，好让每次 `导入` 都拿到一份独立状态（否则同进程里
    跑第二个执行器会串——M41 对拍暴露过）。规格扫描不能因此漏掉它们。
    """
    funcs: list[dict] = []
    for node in 节点们:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            funcs.append(_函数签名(node))
        elif (isinstance(node, ast.FunctionDef)
              and node.name == "__基石新建__"):
            funcs.extend(_收集函数(node.body))
    return funcs


def _stdlib_api() -> list[dict]:
    """扫描 jishi/stdlib/*.py，提取每个模块导出的函数签名与说明。"""
    import os

    pkg_dir = _stdlib_dir()
    mods = []
    for fn in sorted(os.listdir(pkg_dir)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        name = fn[:-3]
        with open(os.path.join(pkg_dir, fn), encoding="utf-8") as f:
            src = f.read()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        funcs = _收集函数(tree.body)
        mods.append({"module": name, "functions": funcs})
    return mods


def build_lang_spec() -> dict:
    """收集语言元数据，产出机器可读规格（关键字/运算符/内建/标准库/异常）。"""
    keywords = {
        k: KEYWORD_DESC.get(k, _tokenizer.KEYWORDS[k])
        for k in _tokenizer.KEYWORDS
    }
    # 二元运算符（按优先级升序 = 从松到紧）
    binops = sorted(_parser._BIN_PREC.items(), key=lambda kv: kv[1])
    builtins = new_builtins()
    builtin_list = [
        {"name": name, "doc": b.doc}
        for name, b in builtins.items()
        # 内部名（`$文本`、`检查实参`…）不进规格：它们是编译器发射的，
        # 写不出来也不该被推荐（M40，判据见 `runtime.is_internal_builtin`）
        if b is not None and hasattr(b, "doc")
        and not is_internal_builtin(name, b)
    ]
    methods = {
        "列表": list(LIST_METHODS.keys()),
        "字典": list(DICT_METHODS.keys()),
        "文本": list(STR_METHODS.keys()),
    }
    return {
        "version": VERSION,
        "keywords": keywords,
        "operators": {
            "binary": [
                {"op": op, "precedence": prec, "name": OP_NAME.get(op, op)}
                for op, prec in binops
            ],
            "compare": list(_parser._COMPARE_OPS),
            # ⚠️ `_ASSIGN_OPS` 是 set，`list(set)` 的顺序取决于 PYTHONHASHSEED，
            # 每个进程都不一样 —— 这会让 `content_hash()` 每次算出的指纹都不同，
            # 而指纹的整个意义就是「同一指纹下跑分可比」。必须排序后再放进规格。
            # （M45 收尾时实测抓到：同一份代码连跑三次得到三个指纹。）
            "assign": sorted(_parser._ASSIGN_OPS),
            "unary": ["-", "非"],
        },
        "builtins": builtin_list,
        "methods": methods,
        "stdlib": _stdlib_api(),
        "exceptions": list(EXCEPTION_TYPES.keys()),
        # --- M44 补的三块 -------------------------------------------------
        # 错误码表：模型/工具拿到报错里的 E 码后，能在这里查到中文含义
        "error_codes": _error_codes(),
        # 静态检查的问题码表（`jishi 源码静态检查` 会输出哪些码）
        "check_codes": _check_codes(),
        # 等价 Python 写法：把基石写法映射到模型最熟的那套心智模型
        "python_equiv": {
            "keywords": PY_KEYWORD,
            "operators": PY_OP,
            "builtins": PY_BUILTIN,
        },
    }


def content_hash(spec: dict) -> str:
    """对规格内容做 sha256，取前 8 位（语言卡版本化用）。"""
    blob = json.dumps(spec, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]


def lang_spec_json() -> str:
    """`--lang-spec --format json` 的输出。"""
    spec = build_lang_spec()
    spec["content_hash"] = content_hash(spec)
    return json.dumps(spec, ensure_ascii=False, indent=2)


def error_to_json(err: JishiError) -> dict:
    """把 JishiError 序列化成 agent 可读的 JSON（M8.3）。"""
    out: dict[str, Any] = {
        "code": err.code,
        "title": err.title,
        "message": err.message,
        "line": err.line,
        "col": err.col,
        "hint": err.hint,
    }
    # 调用栈回溯（M17.2）：把调用链一并给 agent，便于定位
    if getattr(err, "trace", None):
        out["trace"] = [
            {"func": f.func, "line": f.line, "col": f.col}
            for f in err.trace
        ]
    if err.fix:
        out["fix"] = {**err.fix, "line": err.line, "col": err.col}
    return out


def render_ai_card() -> str:
    """生成 ~3K token 的 Markdown 语言卡（M8.1）。

    结构：版本头 → 语法全表 → 内建/标准库 API → 对象方法 → 5 个惯用法
    → 常见错误对照。可直接贴进 LLM 系统提示词。
    """
    spec = build_lang_spec()
    h = content_hash(spec)
    kw = " ".join(spec["keywords"].keys())

    # 内建函数
    builtins_lines = [
        f"- `{b['name']}`：{b['doc']}" for b in spec["builtins"]
    ]
    # 标准库
    stdlib_lines = []
    for m in spec["stdlib"]:
        names = "、".join(f["name"] for f in m["functions"])
        stdlib_lines.append(f"- `导入 {m['module']}`：{names}")

    methods_lines = [
        f"- {k}：{'、'.join(v)}" for k, v in spec["methods"].items()
    ]

    return f"""# 基石（jishi）语言卡 v{VERSION}（{h}）

基石是一门**中文编程语言**，语法对标 Python 的易用性。以下是完整速查，
请严格遵守——**只写中文关键字，不要用英文关键字（def/if/print/True…）**。

## 关键字（全部）
{kw}

## 内建函数
{chr(10).join(builtins_lines)}

## 标准库（`导入 模块名`）
{chr(10).join(stdlib_lines)}

## 对象方法
{chr(10).join(methods_lines)}

## 运算符
- 算术：`+ - * / // % **`
- 比较：`== != < > <= >=`（可链式：`1 < x < 10`）
- 逻辑：`与 或 非`
- 赋值：`= += -= *= /=`

## 语法速览
- 变量：`令 分数 = 60`；多赋值：`令 a, b = [1, 2]`
- 条件：`如果 分数 >= 90：` / `否则如果 分数 >= 60：` / `否则：`
- 循环：`遍历 x 在 [1, 2, 3]：` / `循环 3 次：` / `当 条件：`
- 函数：`函数 求和(a, b)：`（默认参数 `函数 f(a, b = 10)：`）
- 返回：`返回 a + b`；多值返回 `返回 a, b`（即返回列表）
- 导入：`导入 随机`；Python 桥接 `导入 math 从 python`
- 类：`类 面积：` + `函数 算(自身)：`；实例化 `新建 面积()`
- 异常：`尝试：` / `捕获 值错误 为 e：` / `最终：` / `抛出 值错误("…")`
- 推导式：`[x*2 遍历 x 在 列表 如果 x>2]`、`{{k: v 遍历 ...}}`
- 插值：`` `你好 {{名字}}，今年 {{年龄}} 岁` ``
- 块缩进：**用空格（4 格），禁止 Tab**

## 惯用法示例
1. 遍历统计：`令 总和 = 0` / `遍历 x 在 数据：` / `总和 += x`
2. 条件过滤：`[x 遍历 x 在 数据 如果 x > 0]`
3. 函数+默认参数：`函数 打招呼(名字, 语气 = "你好")：` / `打印(语气, 名字)`
4. 字典构建：`{{x: x*x 遍历 x 在 [1, 2, 3]}}`
5. 异常兜底：`尝试：` … `捕获 文件错误 为 e：` `打印(e.消息)`

## 常见错误对照（写中文，别写英文）
- `def` → `函数`；`if` → `如果`；`for` → `遍历`；`while` → `当`
- `return` → `返回`；`import` → `导入`；`class` → `类`；`try` → `尝试`
- `print` → `打印`；`len` → `长度`；`range` → `范围`；`int` → `整数`
- `True` → `真`；`False` → `假`；`None` → `空`
- `and` → `与`；`or` → `或`；`not` → `非`
"""


if __name__ == "__main__":  # pragma: no cover
    cmd = sys.argv[1] if len(sys.argv) > 1 else "card"
    if cmd == "spec":
        print(lang_spec_json())
    else:
        print(render_ai_card())
