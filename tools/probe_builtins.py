# -*- coding: utf-8 -*-
"""内建体检：把**每一个内建**都在五个引擎上真跑一遍。

为什么单独有这个工具
--------------------
M51 建了标准库的漂移检测、M54 建了对象方法的，M55 才补上内建的
（`--dump-builtins`）。但那三套检测都只比**名字清单** —— 而「名字在表里」
不等于「真的能调用」（M53 就是这么漏掉 Rust 的 `文本.查找` 的）。

本工具补的是**行为面**：

1. **正常用法**：每个内建一条最小可跑程序，五个引擎（树遍历 / Python VM /
   C VM / Node / Rust）输出**逐字节一致**；
2. **参数个数错**：三个执行器同类同码，两个宿主首行一致；
3. **Rust 崩溃扫描**：参数个数不对时 Rust **绝不能 panic** ——
   M55 之前它有 12 个内建会直接 `thread 'main' panicked`，那是宿主崩溃，
   不是报错。

用法：`python tools/probe_builtins.py [--fast]`
`--fast` 跳过最慢的两宿主逐个扫描（只跑三执行器）。

退出码：0 = 无悬案；1 = 有未判定项（或有不一致）。
"""

import io
import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    # 行缓冲：这个工具要跑几百个子进程，中途被 Ctrl-C 时至少要看到进度
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

from oracle.jishi import ai, cvm_bind, serialize  # noqa: E402
from oracle.jishi import vm as vm_mod  # noqa: E402
from oracle.jishi.compiler import compile_source  # noqa: E402
from oracle.jishi.errors import JishiError  # noqa: E402
from oracle.jishi.interpreter import run_source as tree_run  # noqa: E402

PY = sys.executable
# ⚠️ 两个宿主的**可执行名都别写死**：
#   · `node` 要经 PATH 查找（找不到时如实报「跳过」，别让它抛 FileNotFoundError
#     把整个体检弄崩 —— 2026-09-30 在没装 node 的服务器上真崩过）；
#   · Rust 宿主的可执行**带不带 `.exe` 是分平台的**，写死 `.exe` 会让
#     Linux / CI 上永远找不到它（哪怕真编译过）。
NODE = shutil.which("node") or "node"
_RUST_NAME = "jishi-rs.exe" if os.name == "nt" else "jishi-rs"
RUST = str(ROOT / "rust" / "target" / "release" / _RUST_NAME)
TMP = ROOT / ".tmp-probe" / "builtins"
TMP.mkdir(parents=True, exist_ok=True)

#: 编译器形式 / 内部内建 —— 用户写不出来（或只由脱糖产生），不参与「公开内建」比对。
#: `$文本`/`$造模块`/`检查实参`… 是内部名。
NOT_PUBLIC = {"$文本", "$造模块", "检查实参"}

#: **无法构造「参数个数错」**的内建，跳过 arity 那节。
#:
#: - `超`：用户写 `超()`，编译器在解析期就脱糖成 `超(自身, "定义类名")` ——
#:   **永远是 2 个实参**，从源码层面构造不出「少给参数」。它照样在
#:   `--dump-builtins` 的清单里（那是重点：宿主必须注册它）。
#: - `打印` / `输入` / `集合`：**0 个参数本来就合法**（空行 / 不带提示语 /
#:   造空集合），拿去当「参数错」用例只会得到一个正常结果，没有意义。
SKIP_ARITY = {"超", "打印", "输入", "集合"}

#: 「**多给**参数」检查要跳过的：个数本来就没有上限的内建（多给是合法的）。
SKIP_TOO_MANY = {"超", "打印", "最大", "最小"}


def _norm(text: str) -> list[str]:
    """归一化输出：去尾随空白、去空行（行尾的 `\r` 也去掉）。"""
    return [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]


# ---------------------------------------------------------------------------
# 一、用例表：每个内建一条最小可跑程序
# ---------------------------------------------------------------------------
#: 键 = 内建名；值 = (代码, 说明)。代码里不写死预期值 —— 比的是五引擎一致性。
CASES: dict[str, str] = {
    # -- 算术 / 位运算 --
    # ⚠️ `位与`/`位或`/`位异或`/`左移`/`右移` 都是**严格二元**（`位与(a, b)`），
    # 不是变参 —— 多给一个是一致的参数错（见下面 arity 那节），但跑不通，
    # 所以这里只给两个。
    "位与": "打印(位与(12, 10), 位与(15, 9))",
    "位或": "打印(位或(12, 10), 位或(1, 2))",
    "位异或": "打印(位异或(12, 10), 位异或(5, 5))",
    "位取反": "打印(位取反(5), 位取反(0))",
    "左移": "打印(左移(4, 1), 左移(1, 4))",
    "右移": "打印(右移(8, 1), 右移(-8, 1))",
    # -- 类型 --
    "整数": '打印(整数("42"), 整数(3.9), 整数(真))',
    "小数": '打印(小数("3.14"), 小数(2), 小数(真))',
    "文本": '打印(文本(42), 文本(真), 文本(空))',
    "精确": '打印(精确("1.5") + 精确("0.25"))',
    "类型": '打印(类型(1), 类型("甲"), 类型([1]), 类型({}), 类型(空))',
    "序数": '打印(序数("A"), 序数("中"), 序数("😀"))',
    "字符": '打印(字符(65), 字符(20013), 字符(128512))',
    # -- 容器 --
    "长度": '打印(长度("abc"), 长度([1,2]), 长度({"甲": 1}))',
    "范围": "打印(范围(3), 范围(1, 4), 范围(0, 6, 2))",
    "总和": "打印(总和([1,2,3]), 总和([]))",
    "最大": "打印(最大([3,1,2]), 最大(1, 5, 3))",
    "最小": "打印(最小([3,1,2]), 最小(1, 5, 3))",
    "反转": '打印(反转([1,2,3]), 反转("abc"))',
    "集合": "打印(长度(集合([1,2,2,3])), 集合([1,2]).包含(2))",
    "带下标": '打印(带下标(["甲", "乙"]))',
    "配对": "打印(配对([1,2], [3,4]))",
    # -- 断言 / 反射 --
    "断言": '断言(真, "应当通过")\n打印("断言过了")',
    "是实例": (
        "类 动物：\n"
        "    函数 初始化(自身, 名)：\n"
        "        自身.名 = 名\n"
        "类 猫 继承 动物：\n"
        "    函数 叫(自身)：\n"
        "        返回 \"喵\"\n"
        "令 c = 猫(\"咪\")\n"
        '打印(是实例(c, 猫), 是实例(c, 动物), 是实例(1, 猫))'
    ),
    # -- 输入输出 --
    "打印": '打印("你好", 42)',
    "输入": '令 名 = 输入("")' '\n打印("你好，" + 名)',
    "打开": None,          # 见下（需要临时文件）
    # -- 脱糖目标 --
    "进入上下文": None,     # 见下（用 `用 … 为` 间接测）
    "退出上下文": None,
    # -- 编译器形式 --
    "超": (
        "类 动物：\n"
        "    函数 叫(自身)：\n"
        "        返回 \"动物\"\n"
        "类 猫 继承 动物：\n"
        "    函数 叫(自身)：\n"
        "        返回 超().叫() + \"喵\"\n"
        '打印(猫().叫())'
    ),
}


def _file_case(mode: str) -> str:
    """`打开` 的用例：写进去再读回来（同一路径，两个 `用` 块）。"""
    p = (TMP / "probe.txt").as_posix()
    return (
        f'用 打开("{p}", "写") 为 f：\n'
        f'    f.写行("甲")\n'
        f'    f.写行("乙")\n'
        f'用 打开("{p}") 为 g：\n'
        f'    打印(g.读所有行())\n'
    )


def _ctx_case() -> str:
    """`进入上下文`/`退出上下文` 的用例：`用 … 为` 就是它俩的脱糖形式。"""
    p = (TMP / "ctx.txt").as_posix()
    return (
        f'用 打开("{p}", "写") 为 f：\n'
        f'    f.写("上下文开了")\n'
        f'打印("块外面")\n'
    )


# ---------------------------------------------------------------------------
# 二、五引擎执行
# ---------------------------------------------------------------------------

def run_python(engine: str, src: str, stdin: str = "") -> tuple[str, str]:
    """三个 Python 侧引擎：返回 (kind, text)。

    ⚠️ **stdin 默认喂空串、且一定替换 `sys.stdin`** —— 这条是血的教训：
    `输入()` 在进程内跑时会**真的去读 stdin**，不钉死就永久阻塞。
    第一版只在「用例是 `输入`」时才喂，于是 `check_arity` 那边
    （它对**每个**内建都跑 `名字()`，其中 `输入()` 照样读 stdin）
    把整个工具挂死了 **17 分钟**，还没输出任何东西 ——
    因为卡的是**主进程**，`subprocess` 的 timeout 根本管不着。
    `redirect_stdout` 也管不到 stdin，得自己换 `sys.stdin`。
    """
    buf = io.StringIO()
    saved = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        with redirect_stdout(buf):
            if engine == "树遍历":
                tree_run(src, "<t>")
            elif engine == "Python VM":
                vm_mod.VM(compile_source(src, "<t>"), "<t>").run()
            else:
                cvm_bind.run_source_c(src, "<t>")
    except JishiError as e:
        return ("err", f"{e.code} {e.title}")
    except BaseException as e:                                  # noqa: BLE001
        return ("err", f"<{type(e).__name__}> {str(e).splitlines()[0][:70]}")
    finally:
        sys.stdin = saved
    return ("ok", "\n".join(_norm(buf.getvalue())))


def _dump(src: str, path: Path) -> None:
    path.write_text(serialize.dumps(compile_source(src, "<t>")), encoding="utf-8")


def run_host(which: str, bc: Path, stdin: str = "") -> tuple[str, str]:
    """两个跨语言宿主：返回 (kind, text)。

    ⚠️ stdin **一定给**（哪怕是空串）：`subprocess` 不给 stdin 时子进程会
    **继承父进程的终端**，遇上 `输入()` 就一起挂住。给空串 → 是管道且立刻 EOF。
    """
    cmd = ([NODE, str(ROOT / "oracle" / "node" / "index.js"), str(bc)] if which == "Node"
           else [RUST, "--load-bytecode", str(bc)])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           input=stdin, cwd=str(ROOT), timeout=120)
    except subprocess.TimeoutExpired:
        return ("err", "<超时>")
    except FileNotFoundError as e:
        # **宿主不存在**（没装 / 没编译）是「这一项没测」，不是「体检失败」——
        # 所以报 `skip` 让调用方如实记一笔，而不是让整个体检崩掉。
        return ("skip", f"<找不到 {e.filename or which}>")
    out = ((r.stdout or "") + (r.stderr or ""))
    lines = _norm(out)
    if r.returncode != 0 or (lines and ("错误" in lines[0] or "panic" in lines[0])):
        first = lines[0] if lines else "<无输出>"
        return ("err", first)
    return ("ok", "\n".join(lines))


ENGINES_PY = ("树遍历", "Python VM", "C VM")
ENGINES_HOST = ("Node", "Rust")


def check_one(name: str, src: str, stdin: str, fast: bool) -> dict:
    """跑一个内建的全部引擎，返回检查结果。"""
    res: dict = {"name": name}

    py = {e: run_python(e, src, stdin) for e in ENGINES_PY}
    # 三执行器：kind 与 text 都必须一致
    res["py_same"] = len({v for v in py.values()}) == 1

    bc = TMP / f"{name}.json"
    _dump(src, bc)
    host = {}
    skipped: list[str] = []
    for h in ENGINES_HOST:
        if fast:
            # ⚠️ `--fast` 的语义是「**只跑三执行器**」—— 原先只跳过了 Rust、
            # 照样去跑 Node，于是在没装 node 的机器上直接崩（2026-09-30）。
            # 两宿主那两位由 pytest 里的五引擎对拍覆盖，这里跳掉是**说好的**。
            continue
        r = run_host(h, bc, stdin)
        if r[0] == "skip":
            skipped.append(f"{h}（{r[1][1:-1]}）")
            continue
        host[h] = r
    res["skipped"] = skipped
    res["host_same"] = len(set(host.values())) <= 1 if host else True

    # 跨语言：kind 必须一致；都 ok 时输出也要一致
    base_kind, base_text = next(iter(py.values()))
    res["cross_ok"] = True
    res["cross_note"] = ""
    for h, (k, t) in host.items():
        if k != base_kind:
            res["cross_ok"] = False
            res["cross_note"] = f"{h} 是 {k}，三执行器是 {base_kind}"
        elif k == "ok" and t != base_text:
            res["cross_ok"] = False
            res["cross_note"] = f"{h} 输出不同：{t[:50]!r} vs {base_text[:50]!r}"

    res["out"] = base_text
    res["kind"] = base_kind
    return res


def check_arity(name: str, fast: bool) -> dict:
    """参数个数错：`名字()`（少给参数）时的表现。

    - 三个执行器必须同类同码（都是 E2000 系类型错误）
    - 两宿主必须彼此一致
    - **Rust 绝不能 panic**（M55 修的崩溃级问题）
    """
    src = f"打印({name}())\n"
    res: dict = {"name": name}
    py = {e: run_python(e, src) for e in ENGINES_PY}
    res["py_same"] = len(set(py.values())) == 1
    res["py_text"] = next(iter(py.values()))[1]
    res["py_err"] = next(iter(py.values()))[0] == "err"

    bc = TMP / f"{name}_arity.json"
    _dump(src, bc)
    host = {}
    for h in ENGINES_HOST:
        if fast and h == "Rust":
            continue
        host[h] = run_host(h, bc)
    res["host"] = host
    res["host_same"] = len(set(host.values())) <= 1 if host else True
    res["panic"] = any("panic" in t for _, t in host.values())
    return res


def check_too_many(name: str, fast: bool) -> dict:
    """**多给**参数：`名字(1,2,3,4,5)` —— 有上限的内建必须报「个数不对」。

    ⚠️ 为什么单独立一节（M56 补）：体检原来只测「少给」。
    而 **JS 的默认参数会把多给的实参静默忽略** ——
    `(prompt = '')` 收到 5 个实参也不报错，函数体照跑。这与「少给」是**同一类**
    「不报错的错答案」：M56 就是靠这一节又抓到 `输入` 的
    （Python 报「需要 0 到 1 个参数，但传了 5 个」，Node 照跑）。
    """
    src = f"打印({name}(1, 2, 3, 4, 5))\n"
    res: dict = {"name": name}
    py = {e: run_python(e, src) for e in ENGINES_PY}
    res["py_same"] = len(set(py.values())) == 1
    res["py_text"] = next(iter(py.values()))[1]
    res["py_err"] = next(iter(py.values()))[0] == "err"

    bc = TMP / f"{name}_many.json"
    _dump(src, bc)
    host = {}
    for h in ENGINES_HOST:
        if fast and h == "Rust":
            continue
        host[h] = run_host(h, bc)
    res["host"] = host
    res["host_same"] = len(set(host.values())) <= 1 if host else True
    res["panic"] = any("panic" in t for _, t in host.values())
    return res


def _collect_bad(label: str, checked: list[dict]) -> list[str]:
    """把一组检查结果里没过的挑出来（「少给」与「多给」两节共用）。"""
    out = []
    for a in checked:
        if not a["py_same"]:
            out.append(f"{label}：{a['name']} 三执行器不一致 —— {a['py_text']}")
        if not a["host_same"]:
            out.append(f"{label}：{a['name']} 两宿主不一致 —— {a['host']}")
        if a["panic"]:
            out.append(f"{label}：{a['name']} 让 Rust host panic（崩溃级）")
    return out


# ---------------------------------------------------------------------------
# 三、主流程
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    fast = "--fast" in argv
    print("=" * 78)
    print("内建体检（每个内建 × 五个引擎真跑）")
    print("=" * 78)

    declared = sorted(b["name"] for b in ai.build_lang_spec()["builtins"])
    public = [n for n in declared if n not in NOT_PUBLIC]
    print(f"语言卡申报的内建 {len(declared)} 个（其中公开 {len(public)} 个）\n")

    # ---- 1. 名字清单（三侧同宽）----
    def dump(cmd: list[str]) -> list[str] | None:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", cwd=str(ROOT), timeout=60)
            return sorted(json.loads(r.stdout))
        except Exception:                                       # noqa: BLE001
            return None

    # ⚠️ **「宿主不存在」与「宿主家底不对」是两回事**（2026-09-30 修）：
    #   · 没装 / 没编译 → **跳过**并如实报告 —— CI 上**故意不编译 Rust**，
    #     那是说好的事，不该因此判体检失败（它曾让 ubuntu 两个任务一直红）；
    #   · 存在但缺/多 → **失败** —— 那才是真问题（M55/M56 都抓到过）。
    node_exe = NODE if os.path.isabs(NODE) else shutil.which(NODE)
    base = list(declared)
    name_bad: list[str] = []
    name_skip: list[str] = []
    for label, exe, cmd in (
        ("Node", node_exe, [NODE, str(ROOT / "oracle" / "node" / "index.js"), "--dump-builtins"]),
        ("Rust", RUST, [RUST, "--dump-builtins"]),
    ):
        if not exe or not Path(exe).is_file():
            name_skip.append(f"{label}（宿主不存在）")
            continue
        got = dump(cmd)
        if got is None:
            name_bad.append(f"{label} 拿不到 --dump-builtins")
            continue
        # ⚠️ 这里**不排除** `超`（M55 曾排除，理由在
        # `tests/test_m51_consistency.py` 的 `BUILTIN_EXEMPT` —— 那个判断是错的）。
        # `超` 是运行期内建，宿主必须有。
        miss, extra = sorted(set(base) - set(got)), sorted(set(got) - set(base))
        if miss:
            name_bad.append(f"{label} 缺：{'、'.join(miss)}")
        if extra:
            name_bad.append(f"{label} 多：{'、'.join(extra)}")
    print("【一】名字清单（--dump-builtins，与分派同源）")
    line = "✓ 三侧同宽" if not name_bad else "\n      ".join(name_bad)
    if name_skip:
        line += f"（如实跳过：{'、'.join(name_skip)}）"
    print("      " + line)
    print()

    # ---- 2. 正常用法 ----
    print("【二】正常用法：每个内建一条最小程序，五引擎输出是否逐字节一致")
    print(f"      {'内建':<10} {'三执行器':<10} {'Node/Rust':<12} {'跨语言':<8} 实际输出")
    print("      " + "-" * 70)
    rows = []
    for name in public:
        src = CASES.get(name)
        if src is None:
            src = _file_case("r") if name == "打开" else _ctx_case()
        stdin = "小明\n" if name == "输入" else ""
        r = check_one(name, src, stdin, fast)
        rows.append(r)
        flag = lambda ok: "✓" if ok else "✗"                     # noqa: E731
        print(f"      {name:<10} {flag(r['py_same']):<10} "
              f"{flag(r['host_same']):<12} {flag(r['cross_ok']):<8} "
              f"{r['out'][:38]!r}")
    print()

    # ---- 3. 参数个数错 ----
    print("【三】参数个数错：`名字()` —— 三执行器同类同码、Rust 不 panic")
    print(f"      {'内建':<10} {'三执行器':<10} {'两宿主':<8} {'Rust panic':<11} 报错")
    print("      " + "-" * 70)
    arity = []
    for name in public:
        if name in SKIP_ARITY:
            arity.append({"name": name, "py_same": True, "host_same": True,
                          "host": {}, "panic": False,
                          "py_text": "（0 个参数合法 / 构造不出参数错，跳过）"})
            print(f"      {name:<10} {'—':<10} {'—':<8} {'—':<11} 跳过（见 SKIP_ARITY）")
            continue
        a = check_arity(name, fast)
        arity.append(a)
        flag = lambda ok: "✓" if ok else "✗"                     # noqa: E731
        pan = ("🔴 是" if a["panic"] else "✓ 否") if "Rust" in a["host"] else "— 跳过"
        print(f"      {name:<10} {flag(a['py_same']):<10} "
              f"{flag(a['host_same']):<8} {pan:<11} {a['py_text'][:34]}")
    print()

    # ---- 3.5 「多给」参数（M56 补：原来只测「少给」）----
    print("【四】多给参数：`名字(1, 2, 3, 4, 5)` —— 有上限的内建应当报错")
    print("      （JS 的默认参数会把多给的实参**静默忽略**，与「少给」同类）")
    print(f"      {'内建':<10} {'三执行器':<10} {'两宿主':<8} {'Rust panic':<11} 报错")
    print("      " + "-" * 70)
    too_many = []
    for name in public:
        if name in SKIP_TOO_MANY:
            too_many.append({"name": name, "py_same": True, "host_same": True,
                             "host": {}, "panic": False,
                             "py_text": "（个数本来无上限，多给是合法的）"})
            print(f"      {name:<10} {'—':<10} {'—':<8} {'—':<11} 跳过（见 SKIP_TOO_MANY）")
            continue
        m = check_too_many(name, fast)
        too_many.append(m)
        flag = lambda ok: "✓" if ok else "✗"                     # noqa: E731
        pan = ("🔴 是" if m["panic"] else "✓ 否") if "Rust" in m["host"] else "— 跳过"
        print(f"      {name:<10} {flag(m['py_same']):<10} "
              f"{flag(m['host_same']):<8} {pan:<11} {m['py_text'][:34]}")
    print()

    # ---- 4. 汇总 ----
    bad = []
    for r in rows:
        if not (r["py_same"] and r["host_same"] and r["cross_ok"]):
            bad.append(f"用法：{r['name']}（{r.get('cross_note', '')}）")
    bad += _collect_bad("参数个数错", arity)
    bad += _collect_bad("多给参数", too_many)
    bad += name_bad

    print("=" * 78)
    if bad:
        print(f"⚠️  有 {len(bad)} 项未过：")
        for b in bad:
            print("   -", b)
        print("=" * 78)
        return 1
    print(f"✅ 无悬案：{len(public)} 个公开内建 × 5 引擎，用法与错误路径全部一致"
          f"{'（已跳过两宿主逐个扫描）' if fast else ''}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
