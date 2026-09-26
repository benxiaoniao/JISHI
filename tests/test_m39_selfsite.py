# -*- coding: utf-8 -*-
"""M39 · 主线 C 的第一块：**用基石写的静态站生成器**（`examples/projects/基石自述站`）。

这个项目一箭三雕：

1. **真实项目验收**：它读仓库里的真数据（版本号、测试数、探针、里程碑、
   模块规模、示例数），生成一套介绍基石的网页——顺手把「真实项目说话」
   这条主线开了个头；
2. **跨执行器验收**：同一份源码在树遍历 / Python VM / C VM 下跑，产物必须
   **逐字节相同**（这是本项目最硬的那条纪律，用真实项目再验一遍）；
3. **调试器的靶子**：M39 给字节码执行器加调试器时，就是在这个项目上试的。

另外它还顺手暴露过两个语言缺陷（多行三引号字符串解析失败、
`导入 文本` 会打断 f-string），那两条各自在有专门用例的地方钉住了。
"""

from __future__ import annotations

import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from jishi.compiler import compile_source                    # noqa: E402
from jishi.interpreter import run_source as run_tree         # noqa: E402
from jishi.stdlib import 系统 as sys_mod                     # noqa: E402
from jishi.vm import VM                                      # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROJ = ROOT / "examples" / "projects" / "基石自述站"
SCRIPT = PROJ / "生成.jsh"


def _cvm_available() -> bool:
    try:
        from jishi import cvm_bind
        cvm_bind.load_lib()
        return True
    except Exception:                            # noqa: BLE001
        return False


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _pyproject_version() -> str:
    for line in _read(ROOT / "pyproject.toml").splitlines():
        if line.startswith("version"):
            return line.split('"')[1]
    raise AssertionError("pyproject.toml 里没有 version")


# ---------------------------------------------------------------------------
# 1. 真实项目：从仓库数据生成页面
# ---------------------------------------------------------------------------

def test_generator_writes_pages_from_real_repo_data(tmp_path):
    """跑一遍生成器：页面写出来了，而且页上的数字**来自仓库真文件**。"""
    out_dir = tmp_path / "站点"
    r = subprocess.run(
        [sys.executable, "-m", "jishi.cli", str(SCRIPT),
         str(out_dir), str(ROOT)],
        capture_output=True, cwd=str(ROOT))
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-500:]
    log = r.stdout.decode("utf-8", "replace")
    assert "正在读仓库数据" in log
    assert "已写出 4 个页面" in log

    names = {p.name for p in out_dir.glob("*")}
    assert names == {"index.html", "上手.html", "style.css",
                     "规模.html", "里程碑.html"}

    index = _read(out_dir / "index.html")
    # 版本号不是写死的：它来自 pyproject.toml
    assert _pyproject_version() in index
    # 骨架/编码都对
    assert index.startswith("<!DOCTYPE html>")
    assert "charset=\"utf-8\"" in index
    assert "基石" in index

    # 里程碑页的数量与 PROGRESS.md 里的时间线行数一致
    milestones = [l for l in _read(ROOT / "PROGRESS.md").splitlines()
                  if l.startswith("| **M")]
    page = _read(out_dir / "里程碑.html")
    assert f"全部 {len(milestones)} 个已完成里程碑" in page
    assert milestones[0].split("|")[1].strip() in page      # 最新那条在里面

    # 规模页里的模块名是当场扫 jishi/ 得到的
    scale = _read(out_dir / "规模.html")
    for name in ("vm.py", "debugger.py", "parser.py"):
        assert f"jishi/{name}" in scale
    assert "jishi_debugger.py" not in scale


def test_generator_reports_what_it_read(tmp_path):
    out_dir = tmp_path / "站点"
    r = subprocess.run(
        [sys.executable, "-m", "jishi.cli", str(SCRIPT),
         str(out_dir), str(ROOT)],
        capture_output=True, cwd=str(ROOT))
    log = r.stdout.decode("utf-8", "replace")
    assert "版本" in log and "测试" in log and "里程碑" in log
    assert "示例" in log


# ---------------------------------------------------------------------------
# 2. 跨执行器：产物逐字节相同
# ---------------------------------------------------------------------------

def _run_with(engine: str, out_dir: Path) -> tuple[str, str]:
    """用指定执行器跑生成器，返回 (屏幕输出, 产物内容拼接)。"""
    src = _read(SCRIPT)
    sys_mod._设参数([str(out_dir), str(ROOT)])
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            if engine == "树遍历":
                run_tree(src, str(SCRIPT))
            elif engine == "vm":
                VM(compile_source(src, str(SCRIPT)), str(SCRIPT)).run()
            else:
                from jishi import cvm_bind
                cvm_bind.run_source_c(src, filename=str(SCRIPT))
    finally:
        sys_mod._设参数([])
    blobs = "".join(_read(p) for p in sorted(out_dir.glob("*")))
    return buf.getvalue(), blobs


def test_same_output_in_tree_walk_and_bytecode_vm(tmp_path):
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir(), b_dir.mkdir()
    out_a, files_a = _run_with("树遍历", a_dir)
    out_b, files_b = _run_with("vm", b_dir)
    # 屏幕输出里有一行写着产物目录，两边的目录名不同，先归一化
    assert out_a.replace(str(a_dir), "") == out_b.replace(str(b_dir), ""),         "两个执行器的屏幕输出必须一致"
    assert files_a == files_b, "两个执行器的产物必须逐字节一致"
    assert files_a, "产物不能是空的"


@pytest.mark.skipif(not _cvm_available(), reason="C 虚拟机动态库不可用")
def test_same_output_in_c_vm(tmp_path):
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir(), b_dir.mkdir()
    _, files_a = _run_with("树遍历", a_dir)
    out_c, files_c = _run_with("cvm", b_dir)
    assert files_c == files_a, "C VM 的产物必须与树遍历逐字节一致"
    assert "已写出 4 个页面" in out_c


# ---------------------------------------------------------------------------
# 3. 生成器本身要「长得像个基石程序」
# ---------------------------------------------------------------------------

def test_generator_uses_alias_import_to_keep_builtin_text():
    """生成器用 `导入 文本 为 …`：模块「文本」会遮蔽内建 `文本(...)`，
    而 f-string 插值正是靠它把值转成文本（这是个真坑，代码里也有注释）。"""
    src = _read(SCRIPT)
    assert "导入 文本 为" in src
    assert "导入 文本\n" not in src


def test_generator_is_formatted_and_parses():
    """生成器自己也守项目风格：`jishi 格式化 --check` 必须过。"""
    r = subprocess.run(
        [sys.executable, "-m", "jishi.cli", "格式化", str(SCRIPT), "--check"],
        capture_output=True, cwd=str(ROOT))
    assert r.returncode == 0, r.stdout.decode("utf-8", "replace")
