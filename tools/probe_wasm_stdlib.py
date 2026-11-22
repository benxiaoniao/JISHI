# -*- coding: utf-8 -*-
"""发版闸门：**wasm 引擎的标准库必须和 Rust 宿主逐条一致**（M224）。

## 为什么有这个闸门（用户实测撞出来的）

0.3.5 的发行包里，CLI（Rust 宿主）是 569 个函数，而 `bin/jishi_wasm.wasm`
（页面 / 窗口模式都靠它）停在 0.3.4 —— `操控` / `粒子` 导不进去、
`地图.寻路` 报「找不到这个函数」。写游戏的用户全部 API 只能退回 0.3.4。

根因是**流程漏洞**：发版脚本 `_build_wasm_into` 只验「两份素材在不在」，
验不出「编进去的是哪一版标准库」。窗口打开白屏的判据同理 ——
文件在、内容旧，一切看起来都对。

## 它怎么查

wasm 引擎自己导出 `jishi_dump_stdlib()`（M224 加的），返回的 JSON 与
`jishi --dump-stdlib` **同一个格式**。两边逐模块、逐函数名对比：

    python tools/probe_wasm_stdlib.py            # 对拍（不一致 → 退出码 1）
    python tools/probe_wasm_stdlib.py --查看      # 只打印两边各多少

`tools/build_release.py` 打完 wasm 会调它；退出码非 0 就中止发版。
`tests/test_m224_wasm_stdlib.py` 也用它（CI 上跑）。

⚠️ 依赖 `node`（wasm 无头运行器，M186 起就有）。没有 node 时**跳过**
（打印一句），不当作通过 —— 但发版脚本里它是硬要求（发行包不能赌）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
WASM = SITE / "jishi_wasm.wasm"
SHIM = SITE / "jishi-wasm.js"
RUST = ROOT / "rust" / "target" / "release" / (
    "jishi-rs.exe" if os.name == "nt" else "jishi-rs")

探针 = r"""
// M224：读 wasm 自己的标准库清单（与 jishi --dump-stdlib 同格式）。
const fs = require("fs");
const JishiWasm = require(process.argv[2]);
const 假页 = { title: "", addEventListener() {}, querySelector: () => null };
globalThis.ipc = { postMessage: () => {} };
(async () => {
  const j = await JishiWasm.load(fs.readFileSync(process.argv[3]), { doc: 假页 });
  const 文 = j.标准库清单 ? j.标准库清单() : "";
  if (!文) {
    console.error("这个 wasm 没有导出 标准库清单 —— 是 0.3.5 之前的旧引擎？");
    process.exit(3);
  }
  process.stdout.write(文);
})();
"""


def _清单(原文: str) -> dict[str, list[str]]:
    """`{"模块": ["函数名", …]}` → 同形（保持顺序）。

    🔴 实际格式就是**一层字典**（`--dump-stdlib` 与 wasm 导出同一个）——
    不是「模块对象数组」，别自作聪明去解 `functions`。
    """
    d = json.loads(原文)
    if isinstance(d, dict) and all(isinstance(v, list) for v in d.values()):
        return {k: [str(x) for x in v] for k, v in d.items()}
    # 容错：万一是 [{module, functions:[{name}]}] 形态
    if isinstance(d, list):
        return {m["module"]: [f["name"] for f in m["functions"]] for m in d}
    raise SystemExit("认不出清单格式（既不是 {模块: [函数]} 也不是数组）")


def 读_rust() -> dict[str, list[str]]:
    if not RUST.is_file():
        raise SystemExit(f"没有 Rust 发行版二进制：{RUST}（先 cargo build --release）")
    r = subprocess.run([str(RUST), "--dump-stdlib"], capture_output=True,
                       text=True, encoding="utf-8")
    if r.returncode != 0:
        raise SystemExit(f"`jishi --dump-stdlib` 跑失败：{r.stderr[-500:]}")
    return _清单(r.stdout)


def 读_wasm() -> dict[str, list[str]] | None:
    if not WASM.is_file() or not SHIM.is_file():
        raise SystemExit("没有 wasm 素材（先 python tools/build_wasm.py）")
    if shutil.which("node") is None:
        return None
    with tempfile.TemporaryDirectory() as 临时:
        p = Path(临时) / "探针.js"
        p.write_text(探针, encoding="utf-8", newline="\n")
        r = subprocess.run(["node", str(p), str(SHIM), str(WASM)],
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=180)
    if r.returncode != 0:
        msg = (r.stdout or "") + (r.stderr or "")
        raise SystemExit(f"wasm 探针跑失败（退出码 {r.returncode}）：{msg[-800:]}")
    return _清单(r.stdout)


def 对拍() -> int:
    rust = 读_rust()
    wasm = 读_wasm()
    if wasm is None:
        print("⚠️  没有 node —— 跳过了 wasm 对拍（发版不能忽略这条）")
        return 0

    rust_函 = sum(len(v) for v in rust.values())
    wasm_函 = sum(len(v) for v in wasm.values())
    print(f"  Rust 宿主：{len(rust)} 模块 / {rust_函} 函数")
    print(f"  wasm 引擎：{len(wasm)} 模块 / {wasm_函} 函数")

    问题们: list[str] = []
    缺模块 = [m for m in rust if m not in wasm]
    if 缺模块:
        问题们.append(f"wasm 少了 {len(缺模块)} 个模块：{'、'.join(缺模块)}")
    for m in rust:
        if m not in wasm:
            continue
        缺 = [f for f in rust[m] if f not in wasm[m]]
        if 缺:
            问题们.append(f"「{m}」少了 {len(缺)} 个函数：{'、'.join(缺)}")
    # 反向：wasm 有、Rust 没有（说明两边不是同一份源码，同样要报）
    多模块 = [m for m in wasm if m not in rust]
    if 多模块:
        问题们.append(f"wasm 有 Rust 没有的模块：{'、'.join(多模块)}")
    for m in wasm:
        if m not in rust:
            continue
        多 = [f for f in wasm[m] if f not in rust[m]]
        if 多:
            问题们.append(f"「{m}」有多余函数（Rust 没有）：{'、'.join(多)}")

    if 问题们:
        print("❌ wasm 引擎的标准库与 Rust 宿主不一致：", file=sys.stderr)
        for q in 问题们:
            print(f"   · {q}", file=sys.stderr)
        print("   ⇒ 先 python tools/build_wasm.py 重编；还是不一致就说明 wasm crate "
              "漏了 #[cfg] 或模块没注册", file=sys.stderr)
        return 1
    print("✅ wasm 与 Rust 的标准库逐条一致")
    return 0


def main(argv: list[str]) -> int:
    if "--查看" in argv or "--list" in argv:
        读_wasm()  # 只为了触发「有 node / 有素材」的检查
        return 对拍()
    return 对拍()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
