# -*- coding: utf-8 -*-
"""审计探针 1：包安装的 **zip slip**（路径穿越）。

做法：造一个「合法外观」的包 zip，其中一条的条目名带 `../`，
然后用 `jishi 安装 坏包 --local-zip 坏.zip` 装它，看能不能把文件写到
**包目录之外**。
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "rust" / "target" / "release" / "jishi-rs.exe"
#: 探针在一个**临时目录**里折腾：解压要能逃出包目录，逃出来的东西得看得见、还得收得干净
WORK = Path(tempfile.gettempdir()) / f"jishi_zipslip_{os.getpid()}"

def main() -> int:
    if WORK.exists():
        import shutil
        shutil.rmtree(WORK)
    (WORK / "home").mkdir(parents=True)

    目标 = WORK / "逃出1层.txt"          # 期望：不该出现
    zip_path = WORK / "坏包.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("坏包/包.json",
                   '{"名字": "坏包", "版本": "1.0.0", "描述": "审计探针"}')
        z.writestr("坏包/主.jsh", '打印("我是坏包")\n')
        # 条目名带 `../`：解压时若直接 join(dest, 名字) 就会写到包外
        z.writestr("坏包/../逃出1层.txt", "pwned\n")
        z.writestr("坏包/../../逃出2层.txt", "pwned\n")

    # 把「系统临时目录」指到我们自己的目录：解压发生在 std::env::temp_dir() 下，
    # 这样 `../` 逃出去会落在**可观测**的位置（否则落到系统 Temp 的上一级，看不见）
    (WORK / "tmp").mkdir(exist_ok=True)
    env = dict(os.environ)
    env["JISHI_HOME"] = str(WORK / "home")
    env["TEMP"] = str(WORK / "tmp")
    env["TMP"] = str(WORK / "tmp")
    env["TMPDIR"] = str(WORK / "tmp")
    r = subprocess.run(
        [str(EXE), "安装", "坏包", "--local-zip", str(zip_path), "--force"],
        cwd=str(WORK), capture_output=True, env=env, timeout=120,
        input=b"")
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    print("退出码：", r.returncode)
    print("输出：", out.strip()[:600] or "（空）")
    命中 = sorted(str(p.relative_to(WORK)) for p in WORK.rglob("逃出*层.txt"))
    print("逃出包目录的文件：", 命中)
    # 顺手收拾，别污染仓库
    for p in WORK.rglob("逃出*层.txt"):
        p.unlink()
    return 0 if not 命中 else 1


if __name__ == "__main__":
    sys.exit(main())
