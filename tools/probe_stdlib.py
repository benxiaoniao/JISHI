# -*- coding: utf-8 -*-
"""标准库体检：**每个标准库函数都在五个执行器上真跑一遍**（R6.4 建）。

## 为什么要有它

到 R6.4 为止，标准库的检测只有**名字面**的：
`--dump-stdlib` 报「函数在不在」（`tests/test_m51_consistency.py` 的漂移检测），
行为面则靠 `tests/test_m31_node_stdlib.py` / `test_m33_rust_stdlib.py` 那批
**手写用例** —— 而那正是 R6.4 学到的教训：**手写用例覆盖到的地方才有人管**。

第一次跑这个探针（141 条用例 × 五个执行器）就抓到三笔：

| 形状 | 症状 |
|---|---|
| `数学.四舍五入(2.5)` | Node 给 `3`，其余四个给 `2` —— Node 用了**半值向上**，而 Python 的 `round` 是**银行家舍入**（已在 R6.4 修掉） |
| `html.属性文本("a","b")` | 三 Python 执行器 + Rust 都报「函数「属性文本」需要 1 个参数，但传了 2 个」，**Node 静默返回 `a`** |
| `日期.今年(1)` | 同上（`今年()` 本来是 0 个参数） |

后两条是**同一族**：**Node 的标准库不检查参数个数**（见 `KNOWN_GAPS`）。

## 判据

把全部用例合成**一个程序**（每条独立 `尝试/捕获`，打印 `名字|值` 一行），
五个执行器各跑一次，**逐行比** —— 一次进程启动就能比完全部用例。

⚠️ 两个实现细节：
* **所有模块都用别名导入**（`导入 文本 为 文本模块`）—— `文本` 既是模块名又是
  内建函数名，直接导入会**遮蔽内建**，程序里就没法用 `文本(x)` 了；
* 每条用例一个独立的变量名（`结果0` / `结果1`…）：模块顶层重复 `令` 同名会报
  「这个名字不能重复定义」；
* 值/消息里可能带换行 —— 解析时按「名字|」前缀**归并**，别让一行裂成几行。

用法：
    python tools/probe_stdlib.py            # 全部用例
    python tools/probe_stdlib.py -v         # 每条都打一行

退出码：0 = 无悬案；1 = 有未登记的不一致（或登记过期）。
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oracle.jishi import cvm_bind                                        # noqa: E402
from oracle.jishi.compiler import compile_source, compile_to_json        # noqa: E402
from oracle.jishi.interpreter import run_source as run_tree              # noqa: E402
from oracle.jishi.vm import VM                                           # noqa: E402

REL = "build/_probe_stdlib_src.jsh"
CSV = "build/_probe_stdlib.csv"
ZIP = "build/_probe_stdlib.zip"

#: `模块.函数` → 求值表达式（会打印它的 `文本()` 形态）。
CASES: dict[str, str] = {
    "html.转义": 'html.转义("<a>&\\"\'")',
    "html.标签": 'html.标签("p", "内容")',
    "html.空元素": 'html.空元素("br")',
    # ⚠️ 参数顺序照 Python 的签名：`链接(文字, 地址)`
    "html.链接": 'html.链接("点我", "/a")',
    "html.有序列表": 'html.有序列表(["甲", "乙"])',
    "html.无序列表": 'html.无序列表(["甲", "乙"])',
    "json.转文本": 'json.转文本({"甲": [1, 2], "乙": 真})',
    "json.解析": 'json.解析("{\\"a\\": 1, \\"b\\": [2, 3]}")',
    "加密.md5": '加密.md5("abc")',
    "加密.sha1": '加密.sha1("abc")',
    "加密.sha256": '加密.sha256("abc")',
    "加密.sha512": '加密.sha512("abc")',
    "加密.摘要": '加密.摘要("abc", 算法="sha256")',
    "加密.支持的算法": '加密.支持的算法()',
    "容器.计数": '容器.计数([1, 1, 2, 3, 3, 3])',
    "容器.最多": '容器.最多([1, 1, 2, 3, 3, 3])',
    "容器.按值排序": '容器.按值排序({"乙": 2, "甲": 1})',
    "容器.取前": '容器.取前([1, 2, 3, 4], 2)',
    "容器.取后": '容器.取后([1, 2, 3, 4], 2)',
    "容器.分块": '容器.分块([1, 2, 3, 4, 5], 2)',
    "对比.逐行": '对比.逐行("甲乙", "甲丙")',
    "对比.统一": '对比.统一("甲乙", "甲丙")',
    "对比.相似度": '对比.相似度("甲乙丙", "甲乙丁")',
    "对比.最相似": '对比.最相似("甲", ["乙", "甲丙", "丁"])',
    "数学.开方": '数学.开方(16)',
    "数学.平方": '数学.平方(5)',
    "数学.幂": '数学.幂(2, 10)',
    "数学.绝对值": '数学.绝对值(-3)',
    "数学.向上取整": '数学.向上取整(1.2)',
    "数学.向下取整": '数学.向下取整(1.8)',
    "数学.四舍五入": '数学.四舍五入(2.5)',
    "数学.正弦": '数学.正弦(0)',
    "数学.余弦": '数学.余弦(0)',
    "数学.正切": '数学.正切(0)',
    "数学.角度转弧度": '数学.角度转弧度(180)',
    "数学.对数": '数学.对数(8, 2)',
    "数学.常用对数": '数学.常用对数(100)',
    "数学.最大公约数": '数学.最大公约数(12, 18)',
    "数学.最小公倍数": '数学.最小公倍数(4, 6)',
    "数学.阶乘": '数学.阶乘(5)',
    "文本.拼接": '文本.拼接(["甲", "乙"], "、")',
    "文本.居中": '文本.居中("甲", 5)',
    "文本.左对齐": '文本.左对齐("甲", 5)',
    "文本.右对齐": '文本.右对齐("甲", 5)',
    "文本.补零": '文本.补零(7, 3)',
    "文本.重复": '文本.重复("甲", 3)',
    "文本.计数": '文本.计数("甲乙甲", "甲")',
    "文本.是数字": '文本.是数字("123")',
    "文本.是字母": '文本.是字母("abc")',
    "文本.是空白": '文本.是空白("  ")',
    "文本.是大写": '文本.是大写("AB")',
    "文本.是小写": '文本.是小写("ab")',
    "文本.首字母大写": '文本.首字母大写("abc")',
    "文本.去前缀": '文本.去前缀("甲乙丙", "甲")',
    "文本.去后缀": '文本.去后缀("甲乙丙", "丙")',
    "文本.格式化": '文本.格式化("{}={}", "甲", 1)',
    "文本.切成三段": '文本.切成三段("甲=乙", "=")',
    "文本.截断": '文本.截断("甲乙丙丁", 3)',
    "文本.填充": '文本.填充("甲", 5, "·")',
    "日期.解析": '日期.解析("2026-10-02")',
    "日期.格式化": '日期.格式化(日期.解析("2026-10-02"), "%Y/%m/%d")',
    "日期.加天数": '日期.加天数(5, 日期.解析("2026-10-02"))',
    "日期.减天数": '日期.减天数(5, 日期.解析("2026-10-02"))',
    "日期.相差天数": '日期.相差天数(日期.解析("2026-10-02"), 日期.解析("2026-10-12"))',
    "日期.早于": '日期.早于(日期.解析("2026-10-02"), 日期.解析("2026-10-03"))',
    "日期.晚于": '日期.晚于(日期.解析("2026-10-02"), 日期.解析("2026-10-03"))',
    "日期.相等": '日期.相等(日期.解析("2026-10-02"), 日期.解析("2026-10-02"))',
    "日期.星期名": '日期.星期名(日期.解析("2026-10-02"))',
    "日期.今年": '日期.今年()',
    "日期.本月": '日期.本月()',
    "日期.本年天数": '日期.本年天数()',
    "正则.匹配": '正则.匹配("[0-9]+", "abc123")',
    "正则.搜索": '正则.搜索("[0-9]+", "abc123")',
    "正则.查找全部": '正则.查找全部("[0-9]+", "a1b22c333")',
    "正则.替换": '正则.替换("[0-9]", "a1b2", "#")',
    "正则.拆分": '正则.拆分("[,;]", "a,b;c")',
    "正则.分组": '正则.分组("([0-9])([0-9])", "12")',
    "统计.求和": '统计.求和([1, 2, 3])',
    "统计.平均": '统计.平均([1, 2, 3])',
    "统计.中位数": '统计.中位数([1, 2, 3, 4])',
    "统计.众数": '统计.众数([1, 1, 2])',
    "统计.方差": '统计.方差([1, 2, 3])',
    "统计.标准差": '统计.标准差([1, 2, 3])',
    "统计.极差": '统计.极差([1, 5, 3])',
    "统计.分位数": '统计.分位数([1, 2, 3, 4], 0.5)',
    "统计.去极值平均": '统计.去极值平均([1, 2, 3, 100])',
    "统计.加权平均": '统计.加权平均([1, 2], [1, 3])',
    "编码.base64编码": '编码.base64编码("abc")',
    "编码.base64解码": '编码.base64解码("YWJj")',
    "编码.十六进制编码": '编码.十六进制编码("abc")',
    "编码.十六进制解码": '编码.十六进制解码("616263")',
    "编码.url编码": '编码.url编码("a b&c")',
    "编码.url解码": '编码.url解码("a%20b%26c")',
    "编码.文本字节": '编码.文本字节("abc")',
    "编码.字节文本": '编码.字节文本([97, 98, 99])',
    # ⚠️ 参数顺序照 Python 的签名：`写表格(路径, 行列表)`（**路径在前**）。
    # 我第一版写反了 —— 于是每个引擎都把「行列表」当路径，写出一个名字叫
    # `[['甲', '乙'], [1, 2]]` 的文件；而**写文件不打印** → 五个引擎 stdout
    # 都空 → 探针反而「一致」。📌 **有副作用的用例一定要有可观察的输出**：
    # 所以这里写成「写 → 读 → 打印」的往返（`表格.读表格` 那条接着读）。
    "表格.写表格": f'表格.写表格("{CSV}", [["甲", "乙"], [1, 2]])',
    "表格.读表格": f'表格.读表格("{CSV}")',
    "表格.表头": f'表格.表头(表格.读表格("{CSV}"))',
    "表格.数据行": f'表格.数据行(表格.读表格("{CSV}"))',
    "表格.挑选": f'表格.挑选(表格.读表格("{CSV}"), ["甲"])',
    "表格.转置": '表格.转置([[1, 2], [3, 4]])',
    "表格.汇总": f'表格.汇总(表格.读表格("{CSV}"), "甲")',
    "路径.连接": '路径.连接("甲", "乙", "丙")',
    "路径.文件名": '路径.文件名("甲/乙/丙.txt")',
    "路径.后缀": '路径.后缀("丙.txt")',
    "路径.无后缀名": '路径.无后缀名("丙.txt")',
    "路径.父目录": '路径.父目录("甲/乙/丙.txt")',
    "迭代.分组": '迭代.分组([1, 2, 3, 4], 2)',
    "迭代.滑窗": '迭代.滑窗([1, 2, 3, 4], 2)',
    "迭代.去重": '迭代.去重([1, 1, 2])',
    "迭代.展开": '迭代.展开([[1, 2], [3]])',
    "迭代.取前": '迭代.取前([1, 2, 3], 2)',
    "迭代.取后": '迭代.取后([1, 2, 3], 2)',
    "迭代.求和": '迭代.求和([1, 2, 3])',
    "迭代.计数": '迭代.计数([1, 2, 3])',
    "迭代.累积": '迭代.累积([1, 2, 3])',
    "迭代.组合": '迭代.组合([1, 2, 3], 2)',
    "迭代.排列": '迭代.排列([1, 2], 2)',
    "迭代.笛卡尔积": '迭代.笛卡尔积([1, 2], ["甲"])',
    "配置.取": '配置.取({"甲": 1}, "甲")',
    "配置.合并": '配置.合并({"甲": 1}, {"乙": 2})',
    "配置.展开": '配置.展开({"甲": {"乙": 1}})',
    "标识.唯一标识长度": '长度(标识.唯一标识())',
    "标识.唯一标识短长度": '长度(标识.唯一标识短())',
    "标识.唯一标识字节长度": '长度(标识.唯一标识字节())',
    "参数.用法": '参数.用法(参数.新建("工具", "说明"))',
    "文件.文件存在": '文件.文件存在("不存在的文件.txt")',
    "文件.路径拼接": '文件.路径拼接("甲", "乙")',
    "文件.文件名": '文件.文件名("甲/乙/丙.txt")',
    "文件.扩展名": '文件.扩展名("丙.txt")',
    "压缩.打包": f'压缩.打包("{ZIP}", "{CSV}")',
    "压缩.列出内容": f'压缩.列出内容("{ZIP}")',
    "随机.随机整数范围": '随机.随机整数(1, 10) >= 1 与 随机.随机整数(1, 10) <= 10',
    "系统.系统名": '系统.系统名()',
    "系统.机器架构": '系统.机器架构()',
    "日志.取级别": '日志.取级别()',
    "日志.级别们": '日志.级别们()',
    "时间.高精度时间非负": '时间.高精度时间() >= 0',
    "html.属性文本": 'html.属性文本("a")',
    "html.属性文本·多给参数": 'html.属性文本("a", "b")',
    "html.属性文本·少给参数": 'html.属性文本()',
    "数学.开方·少给参数": '数学.开方()',
    # ⚠️ 变参函数**不能被参数个数检查拦住**：签名表里 `路径.连接` 一个参数名
    # 都没有（`连接(*部分)`），拿「参数名个数」当上限会把它拦下来。
    "路径.连接·变参多给": '路径.连接("甲", "乙", "丙", "丁")',
    "日期.今年·多给参数": '日期.今年(1)',
}


#: ⚠️ **已知缺口**：检测出来但本阶段不补的，键是用例名。
#: 照本项目规矩：登记要**写清原因、能被复核、修好了会报「过期」**。
#:
#: 📌 **当前是空的 —— 而且刚空过一次**：R6.4 建这个探针时登记过两条
#: （`html.属性文本·多给参数` / `日期.今年·多给参数`，都是「Node 标准库不检查
#: 参数个数」），随后就修掉了（`node/stdlib_sigs.js` 补 `*参数` 记录 →
#: `needStdlibArgs` 在调用点统一查）—— **工具第二天就报「登记过期」，
#: 逼着人来删这两行**。这就是「登记必须能过期」的意义。
KNOWN_GAPS: dict[str, str] = {}


def _alias(mod: str) -> str:
    return mod + "模块"


def build() -> str:
    mods = sorted({k.split(".")[0] for k in CASES})
    head = "".join(f"导入 {m} 为 {_alias(m)}\n" for m in mods)
    body = []
    for i, (name, expr) in enumerate(CASES.items()):
        mod = name.split(".")[0]
        e = expr.replace(mod + ".", _alias(mod) + ".")
        parts = e.splitlines()
        e_ind = parts[0] + "".join("\n    " + p for p in parts[1:])
        body.append(
            "尝试：\n"
            f"    令 结果{i} = {e_ind}\n"
            f'    打印("{name}|" + 文本(结果{i}))\n'
            "捕获 异常 为 错误：\n"
            f'    打印("{name}|【错误】" + 错误.类型 + ":" + 错误.消息)\n'
        )
    return head + "\n" + "".join(body)


def _cap(fn) -> str:
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn()
    except BaseException as e:                                    # noqa: BLE001
        return buf.getvalue() + f"\n【没人接住】{type(e).__name__}:{str(e).splitlines()[0]}\n"
    return buf.getvalue()


def split(text: str) -> dict[str, str]:
    """按「名字|」前缀归并 —— 值/消息里可能带换行，别让一行裂成几行。"""
    d: dict[str, str] = {}
    cur = None
    for ln in text.splitlines():
        head, sep, rest = ln.partition("|")
        if sep and head in CASES:
            cur = head
            d[cur] = rest
        elif cur is not None:
            d[cur] += "\n" + ln
    return d


def main(argv: "list[str] | None" = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="标准库体检（五执行器真跑）")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="每条用例都打印一行")
    args = ap.parse_args(argv)

    src = build()
    (ROOT / REL).write_text(src, encoding="utf-8")
    (ROOT / CSV).unlink(missing_ok=True)
    (ROOT / ZIP).unlink(missing_ok=True)
    outs: dict[str, str] = {
        "树遍历": _cap(lambda: run_tree(src, REL)),
        "Python VM": _cap(lambda: VM(compile_source(src, REL), REL).run()),
        "C VM": _cap(lambda: cvm_bind.run_source_c(src, REL)),
    }
    tmp = Path(tempfile.gettempdir()) / "_probe_stdlib.json"
    tmp.write_text(compile_to_json(src, REL), encoding="utf-8")
    # ⚠️ 名字要**分平台**：Windows 是 `jishi-rs.exe`，Linux/macOS 是 `jishi-rs`。
    #    以前这里写死 `.exe` ⇒ **CI（Linux）上查不到 Rust 宿主**，静默少一个执行器
    #    （五执行器对拍变成四执行器，还只在打印里提一句）。
    rust = ROOT / "rust/target/release" / (
        "jishi-rs.exe" if os.name == "nt" else "jishi-rs")
    if rust.exists():
        outs["Rust"] = subprocess.run([str(rust), "--load-bytecode", str(tmp)], capture_output=True,
                                      input=b"", cwd=str(ROOT)).stdout.decode("utf-8", "replace")
    else:
        print("（注：Rust 宿主没构建，本次少一个执行器；`cd rust && cargo build --release`）")
    node = shutil.which("node")
    if node:
        outs["Node"] = subprocess.run([node, str(ROOT / "oracle/node/index.js"), str(tmp)],
                                      capture_output=True, input=b"",
                                      cwd=str(ROOT)).stdout.decode("utf-8", "replace")
    else:
        print("（注：PATH 上没有 node，本次少一个执行器）")

    parsed = {k: split(v) for k, v in outs.items()}
    bad: list[tuple] = []
    registered: list[str] = []
    stale: list[str] = []
    for name in CASES:
        vals = {eng: parsed[eng].get(name, "<没这一行>") for eng in parsed}
        same = len(set(vals.values())) == 1
        if name in KNOWN_GAPS:
            # 登记在案的缺口：不当作失败，但**修好了会报「过期」**（逼人来删一行）
            (stale if same else registered).append(name)
        elif not same:
            bad.append((name, vals))
        if args.verbose or not same:
            mark = "✅ " if same else ("登记 " if name in KNOWN_GAPS else "❌ ")
            line = mark + name
            if args.verbose:
                # ⚠️ `-v` 要**把值打出来**：只看「绿的」看不出它到底测出了什么 ——
                # `表格.写表格` 那条第一版参数顺序写反、五个引擎**一致地错**，
                # 就是靠看值才发现的（写文件不打印，探针本身看不见副作用）。
                line += " = " + next(iter(vals.values()))[:70]
            print(line)

    print(f"\n共 {len(CASES)} 条用例 · {len(parsed)} 个执行器 · "
          f"**不一致 {len(bad)} 条** · 登记 {len(registered)} 条"
          f"（共登记 {len(KNOWN_GAPS)} 条）")
    for name, vals in bad:
        print(f"\n❌ {name}")
        for eng, v in vals.items():
            print(f"     {eng:9} {v[:130]}")
    for name in registered:
        print(f"\n（登记）{name} —— {KNOWN_GAPS[name]}")
    for eng, d in parsed.items():
        missing = [k for k in CASES if k not in d]
        if missing:
            print(f"\n⚠️ {eng} 少了 {len(missing)} 行：{missing[:10]}")
    if stale:
        print(f"\n⚠️ **登记过期**（这些都一致了，去 `KNOWN_GAPS` 里删掉）：{stale}")
    return 1 if (bad or stale) else 0


if __name__ == "__main__":
    sys.exit(main())
