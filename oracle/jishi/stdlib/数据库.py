# -*- coding: utf-8 -*-
"""基石标准库：数据库。SQLite 的薄封装 —— 存得下、查得到、能分组汇总，**不用写 SQL**。

* **一个文件一个库**：`数据库.打开("账目.db")`；文件就是标准 SQLite 格式，
  **任何 SQLite 工具都能打开**。
* **句柄就是一个路径文本**：`打开` 给你的那个值原样传给后面每个函数
  （同一个路径开两次拿到的是同一个连接）。
* 要写 SQL 也可以：`执行` / `执行查询` 是逃生口。

## 结果顺序是**确定**的（这条不是细节，是硬要求）

五个执行器（树遍历 / Python VM / C VM / Rust VM / Node）要**逐字节一致**，
所以本模块不依赖任何「碰巧」的顺序：

* `查询` 默认按**插入顺序**（`rowid`）；给了 `排序` 就是 `<字段>, rowid` ——
  **永远拿 `rowid` 兜底**，否则排序字段等值的行之间顺序不定；
* `汇总` 的分组按**首次出现的顺序**（同 `表格.分组汇总`）；
* **求和不走 SQL 的 `SUM`**：浮点加法不满足结合律，累加顺序会改末几位，而累加顺序
  由查询计划定 —— 所以本模块把行按确定顺序取回来，在本地左到右累加。

⚠️ 代价如实说：大表比 SQL 的 `SUM` 慢（还在解释器里跑）。要 SQL 那种速度，
用 `执行查询(库, "SELECT 类别, SUM(金额) ... GROUP BY 类别 ORDER BY MIN(rowid)")`。

## 类型（SQLite 只有五种存储类）

| 基石 | 存进去 | 读回来 |
|---|---|---|
| 整数 / 浮点 / 文本 | 原样 | 原样 |
| 真 / 假 | 1 / 0 | **整数 1 / 0**（SQLite 没有布尔） |
| 空 | NULL | 空 |
| 日期时间 | 文本（`2026-09-01 00:00:00`） | 文本 |
| 精确小数 | 浮点 | 浮点（**会丢精度**） |
| **字节** | BLOB | **字节**（原样往返） |
| 列表 / 字典 / 别的 | **报错** | — |

* ⚠️ **整数上限 ±9.2e18**（SQLite 的 `INTEGER` 是 8 字节）：再大就**明确报错**，
  不会静默截断。

⚠️ **钱要一分不差就别用浮点**：把金额存成**整数分**（`2550` 而不是 `25.50`），
显示时再除 100。浮点存一两笔看着没事，几千笔累加就会差几分。
"""

from __future__ import annotations

from ..errors import RunError, RunFileError, RunTypeError, RunValueError
from ..runtime import JishiDecimal, JishiBytes, type_name

__all__ = [
    "打开", "关闭", "表们", "有表", "建表", "建索引",
    "插入", "批量插入", "查询", "条数", "汇总",
    "修改", "删除", "执行", "执行查询",
]

#: 查询条件里能用的比较符号（写成一个单元素字典，如 `{">": 100}`）。
_符号表 = {
    ">": ">", "<": "<", ">=": ">=", "<=": "<=", "!=": "!=",
}


def __基石新建__() -> dict:
    """每次 `导入 数据库` 造一份独立状态（打开的连接不放模块级全局）。

    否则同一进程里跑第二个执行器（对拍、测试、REPL 连跑两段）会串 ——
    M41 的五路对拍就是这么暴露 `日志` / `测试` 这个问题的。
    """
    import sqlite3

    #: 句柄（打开时给的路径文本）→ sqlite3 连接
    库们: dict = {}

    # -- 内部小工具 --------------------------------------------------------

    def _名(名) -> str:
        """把表名/字段名包成 SQL 标识符（里面的双引号翻倍，防注入）。"""
        文本 = str(名)
        if not 文本:
            raise RunValueError("表名 / 字段名不能是空的")
        if "\x00" in 文本:
            raise RunValueError(f"表名 / 字段名里不能有 NUL 字符：「{文本}」")
        return '"' + 文本.replace('"', '""') + '"'

    def _字段名单(字段们, 谁: str) -> list:
        """把「一个字段名」或「一串字段名」统一成列表。"""
        if isinstance(字段们, str):
            return [字段们]
        if not isinstance(字段们, (list, tuple)) or not 字段们:
            raise RunValueError(
                f"{谁}要给一个非空的字段名列表",
                hint='例如 ["日期", "类别", "金额"]')
        return [str(x) for x in 字段们]

    def _存(值, 字段="值"):
        """基石的值 → 能进 SQLite 的值。**进不去就明确报错**，不静默。"""
        import datetime as _dt
        import decimal

        if 值 is None:
            return None
        if isinstance(值, bool):                 # ⚠️ 必须在 int 之前判
            return 1 if 值 else 0
        if isinstance(值, int):
            if not (-(2 ** 63) <= 值 < 2 ** 63):
                raise RunValueError(
                    f"「{字段}」这个整数超出了 SQLite 的范围（约 ±9.2e18）",
                    hint="特别大的数存成文本（文本(值)），或者拆成两部分存")
            return 值
        if isinstance(值, float):
            return 值
        if isinstance(值, str):
            return 值
        if isinstance(值, (_dt.date, _dt.datetime)):
            # 与其它引擎的显示口径一致：`2026-09-01 00:00:00` 这样的文本
            return str(值)
        if isinstance(值, decimal.Decimal):
            return float(值)                     # SQLite 只有浮点，见模块文档
        if isinstance(值, JishiDecimal):
            return float(值.value)               # 基石侧是包装类，见 runtime.py
        if isinstance(值, JishiBytes):
            return 值.value                      # M113：字节 ↔ BLOB 原样往返
        if isinstance(值, (bytes, bytearray, memoryview)):
            return bytes(值)
        if isinstance(值, dict):
            raise RunTypeError(
                f"「{字段}」这个值是「字典」，存不进数据库",
                hint="用 json.文本化(值) 先转成文本再存；或者拆成几张表")
        if isinstance(值, (list, tuple, set, frozenset)):
            raise RunTypeError(
                f"「{字段}」这个值是「{type_name(值)}」，存不进数据库",
                hint="用 json.文本化(值) 先转成文本再存；多个值就拆成多条记录")
        raise RunTypeError(
            f"「{字段}」这个值是「{type_name(值)}」，存不进数据库",
            hint="数据库里只放 整数 / 浮点 / 文本 / 真假 / 空 / 日期")

    def _取(值, 列="值"):
        """SQLite 的值 → 基石的值。BLOB 读成**字节**（M113）。"""
        if isinstance(值, (bytes, bytearray, memoryview)):
            return JishiBytes(值)
        return 值

    def _连接(库):
        """验一遍库句柄，返回真的连接。"""
        if not isinstance(库, str):
            raise RunTypeError(
                f"库句柄应该是 数据库.打开(...) 返回的那个文本，"
                f"给过来的却是 {type_name(库)}")
        if 库 not in 库们:
            raise RunValueError(
                f"这个库还没打开：「{库}」",
                hint='先用 令 库 = 数据库.打开("路径") 打开，再把「库」传给后面的函数')
        return 库们[库]

    def _列名们(连接, 表名) -> list:
        """表的列名（按建表顺序）。表不在就报错，并给出可操作的下一步。"""
        行们 = 连接.execute(f"PRAGMA table_info({_名(表名)})").fetchall()
        if not 行们:
            # 表不在：把现有的表列出来，省得用户猜
            现有 = [r[0] for r in 连接.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
            提示 = (f"现在有这些表：{'、'.join(现有)}" if 现有
                    else "这个库还是空的（先 数据库.建表）")
            raise RunValueError(f"没有表「{表名}」", hint=提示)
        return [r[1] for r in 行们]

    def _查条件(条件, 列, 表名) -> tuple:
        """把「条件」字典翻成 `(SQL 片段, 参数列表)`。片段以 ` WHERE ` 开头。

        ⚠️ **字段名要先对一遍表结构**（M156）：条件写错层级（`{">": 100}`
        而不是 `{"金额": {">": 100}}`）时，SQLite 对**双引号里的未知名字**
        会退化成「字符串字面量」（DQS），于是三个执行器给出**空列表**、
        Node 直接甩一句英文 SQL 报错 —— 「静默查不到」比报错难查得多。
        """
        if 条件 is None or 条件 == {}:
            return "", []
        if not isinstance(条件, dict):
            raise RunTypeError(
                f"「条件」要给一个字典（字段 → 值），给过来的却是 {type_name(条件)}",
                hint='例如 {"类别": "餐饮"}；比大小写 {"金额": {">": 100}}')
        缺 = [str(k) for k in 条件.keys() if str(k) not in 列]
        if 缺:
            raise RunValueError(
                f"表「{表名}」里没有这些字段：{'、'.join(sorted(缺))}",
                hint=f"它有：{'、'.join(列)}。比大小要**嵌一层**："
                     f'{{"字段": {{">": 100}}}}')
        片段: list = []
        参数: list = []
        for 字段 in sorted(条件.keys()):        # 排一下，生成的 SQL 才稳定
            值 = 条件[字段]
            if isinstance(值, dict):
                if not 值:
                    raise RunValueError(f"「{字段}」的条件是空字典，不知道要比什么")
                for 符号 in sorted(值.keys()):
                    x = 值[符号]
                    if 符号 in _符号表:
                        片段.append(f"{_名(字段)} {_符号表[符号]} ?")
                        参数.append(_存(x, 字段))
                    elif 符号 == "包含":
                        片段.append(f"instr({_名(字段)}, ?) > 0")
                        参数.append(str(x))
                    elif 符号 == "非空":
                        片段.append(f"{_名(字段)} IS NOT NULL" if x
                                    else f"{_名(字段)} IS NULL")
                    else:
                        raise RunValueError(
                            f"不认识的比较符号「{符号}」",
                            hint="能用：> < >= <= != 包含 非空")
            else:
                片段.append(f"{_名(字段)} = ?")
                参数.append(_存(值, 字段))
        return " WHERE " + " AND ".join(片段), 参数

    def _定序(排序, 倒序) -> str:
        """排序片段。**永远拿 rowid 兜底** —— 不然等值的行之间顺序不定。

        ⚠️ **方向要加在每一个排序字段上**（M156 修）：以前只拼在最后的 `rowid`
        后面，于是 `ORDER BY "金额", rowid DESC` ——「金额」还是**升序**，
        所谓的「倒序」只颠倒了「金额相等的那些行之间」的顺序。
        写教程时撞出来的：`倒序=真` 取最贵的几笔，拿到的却是**最便宜的几笔**。
        """
        方向 = " DESC" if 倒序 else ""
        if 排序 is None or 排序 == "":
            return f" ORDER BY rowid{方向}"
        if isinstance(排序, (list, tuple)):
            列 = [_名(x) for x in 排序] or ["rowid"]
        else:
            列 = [_名(排序)]
        带方向 = [f"{c}{方向}" for c in 列]
        return f" ORDER BY {', '.join(带方向)}, rowid{方向}"

    def _行(游标, 一行) -> dict:
        列 = [d[0] for d in 游标.description]
        # ⚠️ 列名重复时「记录」只能留一份 —— 不许静默丢列，明确报错并给修法
        if len(set(列)) != len(列):
            重 = sorted({x for x in 列 if 列.count(x) > 1})
            raise RunTypeError(
                f"这句 SQL 里有重名的列（{'、'.join(重)}），一条记录里放不下",
                hint="在 SQL 里给它们起别名，例如 SELECT 金额 AS 分组, 金额 AS 值")
        return {列[i]: _取(一行[i], 列[i]) for i in range(len(列))}

    def _翻(连接, 函数, *参数):
        """跑一句 SQL，把 sqlite3 的报错翻成中文的、可操作的基石错误。"""
        import sqlite3 as _sq
        try:
            return 函数(*参数)
        except _sq.IntegrityError as e:
            raise RunError(f"数据库不答应这次改动：{e}") from None
        except _sq.OperationalError as e:
            raise RunError(f"数据库操作失败：{e}") from None

    # -- 打开 / 关闭 -------------------------------------------------------

    def 打开(路径: str) -> str:
        """打开（没有就新建）一个 SQLite 库，返回**库句柄**。

        句柄就是你给的路径文本本身 —— 把它传给后面每个函数。同一个路径
        重复打开拿到的是**同一个连接**（不会开两次）。

        路径可以写 `":memory:"`：开一个只在内存里的临时库（跑完就没，测试常用）。
        """
        import os
        import sqlite3 as _sq

        文本 = str(路径)
        if 文本 in 库们:
            return 文本
        if 文本 != ":memory:":
            目录 = os.path.dirname(os.path.abspath(文本))
            if 目录 and not os.path.isdir(目录):
                raise RunFileError(f"目录不存在，建不了库：「{目录}」",
                                   hint="先 路径.建目录(...) 或换个路径")
        try:
            # `isolation_level=None` = 自动提交：每句立刻落盘。
            # （本模块只在 `批量插入` 里显式开事务，别的地方要「写完就在」。）
            连接 = _sq.connect(文本, isolation_level=None)
        except _sq.Error as e:
            raise RunFileError(f"打不开库「{文本}」：{e}") from None
        连接.execute("PRAGMA foreign_keys = ON")
        库们[文本] = 连接
        return 文本

    def 关闭(库: str) -> None:
        """关掉这个库（幂等：没打开过、或已经关过了，都不报错）。"""
        连接 = 库们.pop(str(库), None)
        if 连接 is not None:
            try:
                连接.close()
            except Exception:                     # noqa: BLE001
                pass

    # -- 表 -----------------------------------------------------------------

    def 表们(库: str) -> list:
        """列出这个库里所有表的名字（按名字排）。"""
        连接 = _连接(库)
        行们 = _翻(连接, 连接.execute,
                   "SELECT name FROM sqlite_master WHERE type='table' "
                   "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        return [r[0] for r in 行们]

    def 有表(库: str, 表名) -> bool:
        """表在不在。"""
        连接 = _连接(库)
        行 = _翻(连接, 连接.execute,
                 "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                 (str(表名),)).fetchone()
        return 行 is not None

    def 建表(库: str, 表名, 字段们) -> None:
        """建一张表（已经有了就不动它）。

        `字段们` 给字段名列表，例如 `["日期", "类别", "金额", "备注"]`。
        **不用写类型** —— SQLite 是动态类型，一个字段里可以放整数、文本都行。

        每张表自动带一个 `id`（整数主键，从 1 开始）：它既是行号，也是本模块
        定顺序用的锚。
        """
        连接 = _连接(库)
        列 = _字段名单(字段们, f"表「{表名}」的字段们")
        if "id" in 列:
            raise RunValueError(
                "字段名里不用写 id",
                hint="每张表自动带一个 id 主键，把 id 从字段列表里去掉就行")
        定义 = ", ".join(_名(x) for x in 列)
        _翻(连接, 连接.execute,
            f"CREATE TABLE IF NOT EXISTS {_名(表名)} "
            f"(id INTEGER PRIMARY KEY AUTOINCREMENT, {定义})")

    def 建索引(库: str, 表名, 字段们, 唯一: bool = False) -> None:
        """给字段建索引（已经有了就不动）—— 数据一多，按这些字段查就从「全表扫」
        变成「直接跳过去」。

        `唯一` 给 真 时同时要求「这些字段的组合不重复」（重了会报错）。
        """
        连接 = _连接(库)
        列 = _列名们(连接, 表名)
        索引列 = _字段名单(字段们, f"表「{表名}」的索引字段")
        缺 = [x for x in 索引列 if x not in 列]
        if 缺:
            raise RunValueError(
                f"表「{表名}」里没有这些字段：{'、'.join(缺)}",
                hint=f"它有：{'、'.join(列)}")
        索引名 = f"{表名}__{'__'.join(索引列)}"
        _翻(连接, 连接.execute,
            f"CREATE {'UNIQUE ' if 唯一 else ''}INDEX IF NOT EXISTS "
            f"{_名(索引名)} ON {_名(表名)} ({', '.join(_名(x) for x in 索引列)})")

    # -- 写 -----------------------------------------------------------------

    def 插入(库: str, 表名, 记录) -> int:
        """插一条记录（`记录` 是字典：字段 → 值），返回新行的 `id`。

        字典里没写的字段存成**空**。
        """
        连接 = _连接(库)
        列 = _列名们(连接, 表名)
        if not isinstance(记录, dict):
            raise RunTypeError(
                f"「记录」要给一个字典，给过来的却是 {type_name(记录)}",
                hint='例如 {"日期": "2026-10-15", "金额": 2550}')
        if "id" in 记录:
            raise RunValueError("id 是自动给的，不用写", hint="把 id 从记录里去掉")
        多 = [k for k in 记录 if k not in 列]
        if 多:
            raise RunValueError(
                f"表「{表名}」里没有这些字段：{'、'.join(sorted(多))}",
                hint=f"它有：{'、'.join(列)}")
        名们 = sorted(记录.keys())               # 排一下，生成的 SQL 才稳定
        占位 = ", ".join("?" for _ in 名们)
        列SQL = ", ".join(_名(x) for x in 名们)
        参数 = [_存(记录[k], k) for k in 名们]
        游标 = _翻(连接, 连接.execute,
                   f"INSERT INTO {_名(表名)} ({列SQL}) VALUES ({占位})", 参数)
        return 游标.lastrowid

    def 批量插入(库: str, 表名, 记录们) -> int:
        """一次插很多条（**同一个事务**），返回插了几条。

        比循环调 `插入` 快得多，而且**要么全成、要么一条都不进**（中途出错就回滚）。
        """
        连接 = _连接(库)
        if not isinstance(记录们, (list, tuple)):
            raise RunTypeError(
                f"「记录们」要给一个列表，给过来的却是 {type_name(记录们)}")
        if not 记录们:
            return 0
        列 = _列名们(连接, 表名)
        名们 = sorted({k for 记录 in 记录们 if isinstance(记录, dict) for k in 记录})
        if "id" in 名们:
            raise RunValueError("id 是自动给的，不用写", hint="把 id 从记录里去掉")
        多 = [k for k in 名们 if k not in 列]
        if 多:
            raise RunValueError(
                f"表「{表名}」里没有这些字段：{'、'.join(多)}",
                hint=f"它有：{'、'.join(列)}")
        列SQL = ", ".join(_名(x) for x in 名们)
        占位 = ", ".join("?" for _ in 名们)
        SQL = f"INSERT INTO {_名(表名)} ({列SQL}) VALUES ({占位})"
        连接.execute("BEGIN")
        try:
            for 记录 in 记录们:
                if not isinstance(记录, dict):
                    raise RunTypeError(
                        f"记录们里混进了 {type_name(记录)}，应该每条都是字典")
                连接.execute(SQL, [_存(记录.get(k), k) for k in 名们])
            连接.execute("COMMIT")
        except Exception:
            连接.execute("ROLLBACK")
            raise
        return len(记录们)

    def 修改(库: str, 表名, 条件, 新值) -> int:
        """把**满足条件**的行改成新值（`新值` 是字典：字段 → 新值），
        返回改了几行。

        ⚠️ **不给条件就是改整张表**（`条件` 传 `{}` 会有歧义，所以这里要求
        明说：要么给条件，要么用 `执行` 写 SQL）。
        """
        连接 = _连接(库)
        列 = _列名们(连接, 表名)
        if not isinstance(新值, dict) or not 新值:
            raise RunTypeError(
                f"「新值」要给一个非空字典，给过来的却是 {type_name(新值)}",
                hint='例如 {"金额": 3000}')
        if not isinstance(条件, dict) or not 条件:
            raise RunValueError(
                "「修改」必须给条件",
                hint='要改整张表就用 数据库.执行(库, "UPDATE ...")；'
                     '要改一部分就用 {"类别": "餐饮"} 这种条件')
        多 = [k for k in 新值 if k not in 列]
        if 多:
            raise RunValueError(
                f"表「{表名}」里没有这些字段：{'、'.join(sorted(多))}",
                hint=f"它有：{'、'.join(列)}")
        if "id" in 新值:
            raise RunValueError("id 是主键，不能改")
        片段, 参数 = _查条件(条件, 列, 表名)
        赋值 = ", ".join(f"{_名(k)} = ?" for k in sorted(新值.keys()))
        参数 = [_存(新值[k], k) for k in sorted(新值.keys())] + 参数
        游标 = _翻(连接, 连接.execute,
                   f"UPDATE {_名(表名)} SET {赋值}{片段}", 参数)
        return 游标.rowcount if 游标.rowcount and 游标.rowcount > 0 else 0

    def 删除(库: str, 表名, 条件) -> int:
        """删掉**满足条件**的行，返回删了几行。"""
        连接 = _连接(库)
        列 = _列名们(连接, 表名)
        if not isinstance(条件, dict) or not 条件:
            raise RunValueError(
                "「删除」必须给条件",
                hint='要清空整张表就用 数据库.执行(库, "DELETE FROM 表名")')
        片段, 参数 = _查条件(条件, 列, 表名)
        游标 = _翻(连接, 连接.execute,
                   f"DELETE FROM {_名(表名)}{片段}", 参数)
        return 游标.rowcount if 游标.rowcount and 游标.rowcount > 0 else 0

    # -- 读 -----------------------------------------------------------------

    def 查询(库: str, 表名, 条件=None, 排序=None, 倒序: bool = False,
             条数: int = 0) -> list:
        """查出**记录列表**（每条是一个字典）。

        * `条件`：不写就是全部；写法见模块文档（`{"类别": "餐饮"}`、
          `{"金额": {">": 100}}`）；
        * `排序`：按哪个（或哪几个）字段排；不写就按**插入顺序**；
        * `倒序` 给 真 从大到小；`条数` 给了就最多取这么多条（0 = 不限）。

        ⚠️ `倒序` 作用在 `排序` 给的那些字段上（多个字段时**每一个**都倒过来），
        没给 `排序` 时作用在 `id` 上 —— 也就是「**最新记的几笔在前**」。
        """
        连接 = _连接(库)
        列 = _列名们(连接, 表名)
        if 排序 is not None and 排序 != "":
            要排 = _字段名单(排序, "排序字段")
            缺 = [x for x in 要排 if x not in 列]
            if 缺:
                raise RunValueError(
                    f"表「{表名}」里没有这些字段：{'、'.join(缺)}",
                    hint=f"它有：{'、'.join(列)}")
        片段, 参数 = _查条件(条件, 列, 表名)
        限 = ""
        if 条数:
            if not isinstance(条数, int) or 条数 < 0:
                raise RunValueError(f"条数要给非负整数，给的是「{条数}」")
            限 = f" LIMIT {int(条数)}"
        SQL = (f"SELECT * FROM {_名(表名)}{片段}"
               f"{_定序(排序, 倒序)}{限}")
        游标 = _翻(连接, 连接.execute, SQL, 参数)
        return [_行(游标, r) for r in 游标.fetchall()]

    def 条数(库: str, 表名, 条件=None) -> int:
        """数一数有多少行（给了条件就只数满足的）。"""
        连接 = _连接(库)
        列 = _列名们(连接, 表名)
        片段, 参数 = _查条件(条件, 列, 表名)
        行 = _翻(连接, 连接.execute,
                 f"SELECT COUNT(*) FROM {_名(表名)}{片段}", 参数).fetchone()
        return int(行[0])

    def 汇总(库: str, 表名, 按, 求和, 条件=None) -> dict:
        """按「按」字段分组，对「求和」字段做统计，返回 `{组: 汇总字典}`。

        每组的汇总字典与 `表格.汇总` / `表格.分组汇总` **长得一模一样**：
        个数 / 总和 / 平均 / 最大 / 最小。学会一个就会另一个。

        ```
        令 各类 = 数据库.汇总(库, "账目", "类别", "金额")
        打印(各类["餐饮"]["总和"])
        ```

        ⚠️ 组按**首次出现**的顺序（不按组名排序 —— 键可能是文本也可能是数字）。
        """
        连接 = _连接(库)
        列 = _列名们(连接, 表名)
        for 谁 in (按, 求和):
            要 = _字段名单(谁, "字段")
            缺 = [x for x in 要 if x not in 列]
            if 缺:
                raise RunValueError(
                    f"表「{表名}」里没有这些字段：{'、'.join(缺)}",
                    hint=f"它有：{'、'.join(列)}")
        分组列 = _字段名单(按, "按")[0]
        求和列 = _字段名单(求和, "求和")[0]
        片段, 参数 = _查条件(条件, 列, 表名)
        # ⚠️ 按 rowid 取回来（**确定顺序**），分组与累加都在本地做：
        #    分组顺序 = 首次出现顺序；累加顺序 = 插入顺序 —— 五引擎逐字节一致。
        行们 = _翻(连接, 连接.execute,
                  f"SELECT {_名(分组列)}, {_名(求和列)} FROM {_名(表名)}"
                  f"{片段} ORDER BY rowid", 参数).fetchall()
        桶: dict = {}
        for 组, 值 in 行们:
            桶.setdefault(组, []).append(值)
        出: dict = {}
        for 组, 值们 in 桶.items():
            数值 = []
            for v in 值们:
                if v is None:
                    raise RunTypeError(
                        f"「{求和列}」这一列里有空值，没法汇总",
                        hint="先把空值补上，或者用条件把它们排除掉")
                try:
                    数值.append(float(v))
                except (TypeError, ValueError):
                    raise RunTypeError(
                        f"「{求和列}」这一列里有不是数字的值「{v}」，没法汇总") from None
            出[组] = {
                "个数": len(数值),
                "总和": sum(数值),
                "平均": sum(数值) / len(数值),
                "最大": max(数值),
                "最小": min(数值),
            }
        return 出

    # -- 逃生口：直接写 SQL ------------------------------------------------

    def _像查询(SQL: str) -> bool:
        s = SQL.lstrip()
        while s.startswith("--"):                 # 跳过行注释
            s = s.split("\n", 1)[1] if "\n" in s else ""
            s = s.lstrip()
        return s[:7].lower() in ("select ", "select\n", "select\r")

    def 执行(库: str, SQL: str) -> int:
        """直接跑**一句写操作的 SQL**（INSERT / UPDATE / DELETE / CREATE …），
        返回改动的行数。

        ⚠️ 这是逃生口：**结果顺序、类型转换都得你自己负责**。
        SELECT 请用 `执行查询`。
        """
        连接 = _连接(库)
        文本 = str(SQL)
        if _像查询(文本):
            raise RunValueError(
                "「执行」只跑写操作的 SQL；你给的是 SELECT",
                hint="要取回记录用 数据库.执行查询(库, SQL)")
        游标 = _翻(连接, 连接.execute, 文本)
        n = 游标.rowcount
        return n if n and n > 0 else 0

    def 执行查询(库: str, SQL: str) -> list:
        """直接跑一句 SELECT，返回**记录列表**。

        ⚠️ 这是逃生口：**顺序由你的 SQL 决定** —— 想让五个执行器结果一致，
        SQL 里必须自己写 `ORDER BY`。
        """
        连接 = _连接(库)
        文本 = str(SQL)
        if not _像查询(文本):
            raise RunValueError(
                "「执行查询」只跑 SELECT；你给的不是 SELECT",
                hint="写操作用 数据库.执行(库, SQL)")
        游标 = _翻(连接, 连接.execute, 文本)
        return [_行(游标, r) for r in 游标.fetchall()]

    return {
        "打开": 打开, "关闭": 关闭, "表们": 表们, "有表": 有表,
        "建表": 建表, "建索引": 建索引,
        "插入": 插入, "批量插入": 批量插入,
        "查询": 查询, "条数": 条数, "汇总": 汇总,
        "修改": 修改, "删除": 删除,
        "执行": 执行, "执行查询": 执行查询,
    }
