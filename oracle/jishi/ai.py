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
    DECIMAL_METHODS,
    DICT_METHODS,
    FILE_METHODS,
    LIST_METHODS,
    SET_METHODS,
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


#: 默认值里**能从 AST 直接读出来**的字面量写法 → 基石说法。
#: ⚠️ Python 的 `True` / `False` / `None` 在基石里是 `真` / `假` / `空` ——
#:    签名是从 AST 反解的，不翻译就会**教模型写 `True`**（M116 抓到过 12 处）。
_默认翻译 = {"True": "真", "False": "假", "None": "空"}


def _默认文本(节点: ast.AST) -> str:
    """一个默认值参数怎么写进签名。

    | 情况 | 写成 | 为什么 |
    |---|---|---|
    | 字面量（数 / 字符串 / `1e-09`） | 原样 | 模型能直接用 |
    | `True` / `False` / `None` | `真` / `假` / `空` | 基石的说法，别教 Python 写法 |
    | 负数、其它一元运算 | 原样 | 还是字面量 |
    | **认不出来**（模块常量、属性，如 `_DEFAULT_TIMEOUT`、`_py_math.e`） | `…` | **不猜**：既表明「有默认值、可以省」，又不把内部名字漏给模型 |
    """
    if isinstance(节点, ast.Constant):
        if 节点.value is True:
            return "真"
        if 节点.value is False:
            return "假"
        if 节点.value is None:
            return "空"
        try:
            return ast.unparse(节点)
        except Exception:            # noqa: BLE001
            return "…"
    if isinstance(节点, ast.Name) and 节点.id in _默认翻译:
        return _默认翻译[节点.id]
    if (isinstance(节点, ast.UnaryOp) and isinstance(节点.operand, ast.Constant)
            and isinstance(节点.op, (ast.USub, ast.UAdd))):
        try:
            return ast.unparse(节点)
        except Exception:            # noqa: BLE001
            return "…"
    return "…"


def _函数签名(node: ast.FunctionDef) -> dict:
    """一个公开函数的 `sig` 与 doc 首行。

    🔴 **`sig` 必须带默认值**（M116 补）。以前只拼参数名 —— 模型拿到
    `居中(文本, 宽度, 填充)` 时**不知道哪个可以省**，只能猜；而标准库一补大，
    这个洞就同比放大（路线图 §12.2.1 说的「补得越多、猜错的越多」）。
    默认值本来就在 AST 里（`args.defaults` 对齐到参数表**尾部**），拿得到。
    """
    params = [a.arg for a in node.args.args]
    默认们 = list(node.args.defaults)
    if 默认们:
        起 = len(params) - len(默认们)
        for i, d in enumerate(默认们):
            params[起 + i] = f"{params[起 + i]}={_默认文本(d)}"
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
    """扫描 oracle/jishi/stdlib/*.py，提取每个模块导出的函数签名与说明。"""
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
    # 对象方法表（M54 补齐到 6 类）。
    #
    # ⚠️ 这**不只是给模型看的**：它是「对象方法」这件事在 Python 侧的**单一来源**，
    # `tests/test_m51_consistency.py` 拿它当基准，去比 Node / Rust 宿主报上来的
    # 方法表 —— M53 之前这里只有 3 类，害得「宿主缺方法」只能靠对拍偶然撞见
    # （Rust 缺 `文本.查找` 就是这么漏过去的）。
    methods = {
        "列表": list(LIST_METHODS.keys()),
        "字典": list(DICT_METHODS.keys()),
        "文本": list(STR_METHODS.keys()),
        "集合": list(SET_METHODS.keys()),
        "文件": list(FILE_METHODS.keys()),
        "小数": list(DECIMAL_METHODS.keys()),
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


#: 按任务打包的模块清单（`--ai-card --任务 X`）。
#: ⚠️ 这里只列「这类活常用到的」——模块本身还是**语言事实的投影**，
#:    模块名走 `build_lang_spec()` 校验，写错了当场报错（不静默给空卡）。
卡_任务表: dict = {
    "表格": ["表格", "文件", "文本", "统计"],
    "文本": ["文本", "正则", "编码"],
    "文件": ["文件", "路径", "压缩"],
    "网络": ["网络", "json", "html", "编码"],
    "数据": ["数据库", "json", "表格", "统计"],
    "并发": ["并发", "文件", "网络"],
    "测试": ["测试", "容器", "日志"],
}

#: 每个任务附一段惯用法（M116 起；按**跑分里的失败模式**持续补 —— 见 §12.2.2）。
#: 写的都是「模型容易写错的形状」的**唯一正确写法**。
卡_任务惯用: dict = {
    "表格": [
        "读表：`令 表 = 表格.读表格(路径)` —— 返回的是**表格对象**（不是行列表）",
        "要「字典列表」用 `表格.读字典表格(路径)`；反过来 `表格.从记录(记录们)`",
        "汇总一列：`表格.汇总(表, \"分数\")` 直接给「个数/总和/平均/最大/最小」",
        "按组汇总：`表格.分组汇总(表, \"班级\", \"分数\")`",
        "别手写累加循环 —— 有 `表格.汇总` / `统计.求和`",
    ],
    "文本": [
        "切分：`文本.切成三段(s, 分隔符)` 给 `[前, 分隔符, 后]`；要全部用 `文本.拆分`",
        "去掉两端空白**没有** `去空白` —— 用 `文本.去前缀`/`去后缀` 或自己 `如果 文本.是空白(字)`",
        "按行拆：`文本.按行拆分(s)`（与 Python 的 `splitlines()` 同一套）",
        "一个汉字算 **2** 列宽：对齐用 `文本.左对齐(s, 宽度)`，不要自己数 `长度`",
    ],
    "文件": [
        "读文本：`文件.读文本(路径)`；读二进制：`文件.读字节(路径)`（给 `字节`）",
        "路径要拼就 `路径.连接(目录, 名字)` —— 别手写 `/`（Windows 上是 `\\`）",
        "临时文件：`路径.临时目录()`；要写就 `文件.写文本(p, 内容)`",
    ],
    "网络": [
        "取网页：`网络.获取(网址)`；带请求头：`网络.获取详情(网址, 请求头={...})`",
        "网址里有中文**要自己编码**：`编码.url编码(片段)`",
        "JSON 接口：`网络.获取JSON(网址)` 直接给字典/列表",
    ],
    "数据": [
        "建库：`令 库 = 数据库.打开(\"账.db\")`，用完 `数据库.关闭(库)`",
        "建表给**字段名列表**（不写类型）：`数据库.建表(库, \"账\", [\"类别\", \"金额\"])`",
        "插入给字典：`数据库.插入(库, \"账\", {\"类别\": \"餐饮\", \"金额\": 25.5})`",
        "汇总：`数据库.汇总(库, 表名, 按, 求和)` —— 别把整表读出来自己累加",
        "存二进制用 `字节`（列类型 `BLOB`），读回来还是 `字节`",
    ],
    "并发": [
        "一批**小** IO 才用它：`并发.批量摘要(路径们)` / `并发.批量读(路径们)` / `并发.并行取(网址们)`",
        "**结果顺序与输入一致** —— 第 3 个结果就是第 3 个输入的",
        "大文件、纯计算**别用**（线程只是白加调度）",
    ],
    "测试": [
        "断言：`测试.断言相等(甲, 乙)` / `测试.断言为真(条件)`",
        "成组跑：`令 结果 = 测试.运行(函数们)`，末尾 `测试.汇总(结果)`",
    ],
}

_语法段 = """## 运算符
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
- 推导式：`[x*2 遍历 x 在 列表 如果 x>2]`、`{k: v 遍历 ...}`
- 插值：`` `你好 {名字}，今年 {年龄} 岁` ``
- 块缩进：**用空格（4 格），禁止 Tab**
"""

_错误对照段 = """## 常见错误对照（写中文，别写英文）
- `def` → `函数`；`if` → `如果`；`for` → `遍历`；`while` → `当`
- `return` → `返回`；`import` → `导入`；`class` → `类`；`try` → `尝试`
- `print` → `打印`；`len` → `长度`；`range` → `范围`；`int` → `整数`
- `True` → `真`；`False` → `假`；`None` → `空`
- `and` → `与`；`or` → `或`；`not` → `非`

## 报错怎么读、怎么查（M56）
- 报错形态：`错误 E0301：找不到这个名字（找不到名字「长渡」）` + 行列 + `提示：你是不是想写「长度」？`
- **报错里的 E 码可以直接查详情**：跑 `jishi 错误 E0301`
  —— 给「什么意思 / 常见成因 / 怎么改」三段。不写码则列出全部错误码。
- 常用异常类型（`捕获 X 为 e` 用，`e.消息` / `e.类型` 可读）：
  `异常`（基类）、`运行期错误`、`类型错误`、`值错误`、`索引错误`、`键错误`、
  `除零错误`、`文件错误`、`断言错误`
- 程序化读错误：加 `--json-errors`，错误会以 JSON 输出（含码、行列、修法建议）。
"""


def _标准库段(规格: dict, 模块们: list | None) -> str:
    """标准库段 —— **每个函数带完整签名**（M116 起）。

    以前只列函数名，模型得**猜参数顺序**；顺带连「哪个参数能省」也看不出来。
    """
    块们 = []
    for m in 规格["stdlib"]:
        if 模块们 is not None and m["module"] not in 模块们:
            continue
        行 = [f"### `{m['module']}`"]
        for f in m["functions"]:
            说明 = f["doc"].strip() or ""
            行.append(f"- `{f['sig']}`" + (f" —— {说明}" if 说明 else ""))
        块们.append("\n".join(行))
    if not 块们:
        return ""
    头 = ("## 标准库（先 `导入 模块名`，再用 `模块.函数(参数)`；"
          "**签名里带 `=` 的参数可以省略**）\n"
          "⚠️ 可选参数请**按顺序**传：跳过中间那个只在部分平台支持 —— "
          "**别跳**，省哪个就从哪个开始省起。\n")
    return 头 + "\n".join(块们)


def _惯用段(规格: dict, 任务: str | None) -> str:
    if 任务 is not None and 任务 in 卡_任务惯用:
        行 = [f"## 「{任务}」这类活的惯用法"]
        行 += [f"{i}. {x}" for i, x in enumerate(卡_任务惯用[任务], 1)]
        行.append("")
        行.append("## 通用惯用法")
        起 = 1
    else:
        行 = ["## 惯用法示例"]
        起 = 1
    行 += [
        f"{起 + 0}. 遍历统计：`令 总和 = 0` / `遍历 x 在 数据：` / `总和 += x`",
        f"{起 + 1}. 条件过滤：`[x 遍历 x 在 数据 如果 x > 0]`",
        f'{起 + 2}. 函数+默认参数：`函数 打招呼(名字, 语气 = "你好")：` / `打印(语气, 名字)`',
        f"{起 + 3}. 字典构建：`{{x: x*x 遍历 x 在 [1, 2, 3]}}`",
        f"{起 + 4}. 异常兜底：`尝试：` … `捕获 文件错误 为 e：` `打印(e.消息)`",
    ]
    return "\n".join(行)


def render_ai_card(模块: str | None = None, 任务: str | None = None) -> str:
    """生成 Markdown 语言卡（M8.1；M116 起**带签名 + 可分片**）。

    | 形态 | 用途 | 大致 token |
    |---|---|---|
    | `render_ai_card()` | 全量（「一贴就能用」） | 见 `--ai-card --统计` |
    | `render_ai_card(模块="文本")` | 单模块（写表格类活时只带它） | 小得多 |
    | `render_ai_card(任务="表格")` | 按任务（若干模块 + 该任务惯用法） | 中 |

    ⚠️ 它**永远是 `build_lang_spec()` 的投影** —— 不另写一套说辞
    （`AGENTS.md` §0.5 核心二）。加模块 / 加函数**不用改这里**。
    """
    if 模块 is not None and 任务 is not None:
        raise ValueError("「模块」与「任务」只能给一个")
    规格 = build_lang_spec()
    指纹 = content_hash(规格)
    已知模块 = {m["module"] for m in 规格["stdlib"]}
    模块们: list | None = None
    if 任务 is not None:
        if 任务 not in 卡_任务表:
            raise ValueError(
                f"没有「{任务}」这个任务包。可选：{'、'.join(卡_任务表)}")
        模块们 = 卡_任务表[任务]
    elif 模块 is not None:
        if 模块 not in 已知模块:
            raise ValueError(f"没有「{模块}」这个标准库模块")
        模块们 = [模块]

    内建行 = [f"- `{b['name']}`：{b['doc']}" for b in 规格["builtins"]]
    方法行 = [f"- {k}：{'、'.join(v)}" for k, v in 规格["methods"].items()]

    段 = [f"# 基石（jishi）语言卡 v{VERSION}（{指纹}）"]
    说明 = "基石是一门**中文编程语言**，语法对标 Python 的易用性。"
    if 任务 is not None:
        段.append(f"{说明}这是「**{任务}**」这类活的分片卡（只带常用模块）。")
    elif 模块 is not None:
        段.append(f"{说明}这是「**{模块}**」模块的分片卡。")
    else:
        段.append(f"{说明}以下是完整速查，")
    段.append("请严格遵守——**只写中文关键字，不要用英文关键字（def/if/print/True…）**。")
    段.append("")
    段.append("## 关键字（全部）")
    段.append(" ".join(规格["keywords"].keys()))
    段.append("")
    段.append("## 内建函数")
    段.extend(内建行)
    段.append("")
    段.append(_标准库段(规格, 模块们))
    段.append("")
    段.append("## 对象方法")
    段.extend(方法行)
    段.append("")
    段.append(_语法段.rstrip())
    段.append("")
    段.append(_惯用段(规格, 任务))
    段.append("")
    段.append(_错误对照段.rstrip())
    return "\n".join(段) + "\n"


def 卡统计() -> dict:
    """给「分片到底省了多少」用（`--ai-card --统计`）。**字符数**，不是精确 token。"""
    全 = render_ai_card()
    出 = {"全量": len(全)}
    for m in build_lang_spec()["stdlib"]:
        if m["module"] in ("文本", "表格", "数据库"):
            出[f"模块:{m['module']}"] = len(render_ai_card(模块=m["module"]))
    for t in 卡_任务表:
        出[f"任务:{t}"] = len(render_ai_card(任务=t))
    return 出


if __name__ == "__main__":  # pragma: no cover
    cmd = sys.argv[1] if len(sys.argv) > 1 else "card"
    if cmd == "spec":
        print(lang_spec_json())
    else:
        print(render_ai_card())
