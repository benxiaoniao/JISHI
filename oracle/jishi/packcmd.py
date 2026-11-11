# -*- coding: utf-8 -*-
"""`jishi 打包` —— 把一个目录打成**四类可交付产物**（M172）。

## 为什么要一条命令出四样

「打包」在不同平台上是四件不同的事：

| 类型 | 产物 | 谁认 |
|---|---|---|
| `便携` | `<名字>-<版本>.zip` | 哪儿都能解，解压即用 |
| `安装包` | `<名字>-<版本>-windows-x64-setup.exe` | Windows：双击装、应用设置里能卸 |
| `应用` | `<名字>.app/` + `<名字>-<版本>-macos-arm64.tar.gz` | macOS：`.app` 是**目录**，双击就开 |
| `压缩包` | `<名字>-<版本>-linux-x64.tar.gz`（内含 `install.sh`） | Linux：解开跑 `./install.sh` |

一句 `jishi 打包` 全出 —— 不是为了「四样显得全」，而是**同一个程序本来就该
在哪都能装上**。用户写完一个计算器，不该再去学四套打包工具。

## 三条规矩

1. **可复现**：同样的输入、两次打包，**字节完全相同**（沿用 `打包.生成` 那套：
   时间/顺序/权限全定死；tar.gz 的 gzip 头也 `mtime=0`、不留文件名）。
2. **产物名从清单来**：`名字`/`版本`/`平台` 都写在包里的 `清单.json` 上，
   产物名照着它取 —— 而不是命令行说一套、包里写另一套。
3. **不碰源目录**：中间包全在系统临时目录里做，最后只把成品拷进 `--出`。

## 配置

目录里放一个 `打包.json` 就不用每次敲参数（命令行优先）：

```json
{"名字": "记账", "版本": "1.0.0", "入口": "记账.exe"}
```

`打包.json` 与 `--出` 目录**不进包**（它们是打包的输入，不是程序的一部分）。
"""

import gzip
import io
import json
import os
import shutil
import struct
import sys
import tarfile
import tempfile
import zipfile

from .stdlib import 打包 as 打包模块

#: 认的产物类型（顺序 = 产物打印顺序）。
全部类型 = ("便携", "安装包", "应用", "压缩包")

#: 各类型在清单里的「平台」值。**产物名与清单必须对得上**。
平台表 = {
    "便携": "",
    "安装包": "windows-x64",
    "应用": "macos-arm64",
    "压缩包": "linux-x64",
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _fnv1a32(文本: str) -> str:
    """FNV-1a 32 位（与 `站点.资源指纹` 同一个算法）。

    `CFBundleIdentifier` 只认 ASCII 反向域名，而名字多半是中文 ⇒ 拿指纹当后缀，
    同一个名字两次得到同一个 id（别用随机数：那会让两次打包的 `Info.plist` 不同）。
    """
    h = 0x811C9DC5
    for b in 文本.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return "%08x" % h


def _收集(源: str, 排除) -> list:
    """列出要进包的文件（跳过 `--出` 与 `打包.json`）。返回 `(绝对路径列表)`。"""
    排除 = {os.path.normcase(os.path.abspath(p)) for p in 排除}
    出 = []
    for dirpath, dirnames, filenames in os.walk(源):
        dirnames[:] = sorted(
            d for d in dirnames
            if os.path.normcase(os.path.abspath(os.path.join(dirpath, d))) not in 排除)
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            if os.path.normcase(os.path.abspath(full)) in 排除:
                continue
            if fn == "打包.json" and os.path.normcase(os.path.abspath(dirpath)) == \
                    os.path.normcase(os.path.abspath(源)):
                continue
            出.append(full)
    return 出


def _读配置(源: str) -> dict:
    路径 = os.path.join(源, "打包.json")
    if not os.path.isfile(路径):
        return {}
    try:
        with open(路径, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit(f"错误：读不了「{路径}」：{e}") from None
    if not isinstance(d, dict):
        raise SystemExit(f"错误：「{路径}」要是一个字典")
    return d


# ---------------------------------------------------------------------------
# 三份「平台专属的文本」—— 两边命令行必须逐字相同
# ---------------------------------------------------------------------------

def 安装脚本(名字: str, 入口: str) -> str:
    """Linux 的 `install.sh`。

    ⚠️ **变量名一律 ASCII**：POSIX sh 的标识符不保证认非 ASCII，中文只放注释和
    提示里。这是「跨平台复刻」最容易栽的地方。
    """
    入口名 = os.path.basename(入口.replace("\\", "/")) if 入口 else ""
    if 入口:
        链 = f'''if ln -sf "$DEST/{入口}" "$PREFIX/bin/{入口名}" 2>/dev/null; then
  :
else
  # 有些文件系统不让建软链（FAT / 受限账号 / Windows 上的 sh）——
  # 那就退化成一个两行的启动脚本，效果一样。装不上比装歪了好。
  printf '#!/bin/sh\\nexec "%s" "$@"\\n' "$DEST/{入口}" > "$PREFIX/bin/{入口名}"
  chmod +x "$PREFIX/bin/{入口名}"
fi'''
    else:
        链 = 'echo "这个包没标「入口」，没有建命令"'
    return f"""#!/bin/sh
# 由 `jishi 打包` 生成 —— 把一个程序装到 $PREFIX/opt/{名字}，入口软链到 $PREFIX/bin。
#
#   ./install.sh                    装到 ~/.local
#   ./install.sh --前缀 /usr/local   装到别处（要写权限）
#   ./install.sh --卸载              卸掉（只删自己装的那份）
set -eu
PREFIX="${{JISHI_PREFIX:-$HOME/.local}}"
UNINSTALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --前缀|--prefix) PREFIX="$2"; shift 2 ;;
    --卸载|--uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "看不懂这个参数：$1" >&2; exit 2 ;;
  esac
done
APP="{名字}"
HERE=$(cd "$(dirname "$0")" && pwd)
DEST="$PREFIX/opt/$APP"
if [ "$UNINSTALL" = 1 ]; then
  rm -f "$PREFIX/bin/{入口名}"
  rm -rf "$DEST"
  echo "已卸载：$DEST"
  exit 0
fi
mkdir -p "$DEST" "$PREFIX/bin"
for f in "$HERE"/*; do
  base=$(basename "$f")
  [ "$base" = "install.sh" ] && continue
  cp -R "$f" "$DEST/"
done
{链}
echo "已安装：$DEST"
"""


def 应用清单(名字: str, 版本: str, 入口: str) -> str:
    """macOS 的 `Contents/Info.plist`。

    `.app` 之所以「双击能开」，全靠这份 plist 里的 `CFBundleExecutable` 指向
    真正要跑的那个文件 —— 所以「入口」必须标，标不了就别出 `.app`。
    """
    可执行 = os.path.basename(入口.replace("\\", "/"))
    标识 = "cn.jishi." + _fnv1a32(名字)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>zh_CN</string>
    <key>CFBundleExecutable</key>
    <string>{可执行}</string>
    <key>CFBundleIdentifier</key>
    <string>{标识}</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>{名字}</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>{版本}</string>
    <key>CFBundleVersion</key>
    <string>{版本}</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
"""


# ---------------------------------------------------------------------------
# tar.gz（可复现）
# ---------------------------------------------------------------------------

def _写tar_gz(目录: str, 顶层名: str, 输出: str) -> None:
    """把 `目录` 打成 `顶层名/...` 的 tar.gz。

    **三样定死**才不会漂：条目按名字排序 / mtime 全 0 / uid.gid 与 uname.gname 全空。
    gzip 头也定死（`mtime=0` 且 `filename=""`）—— 否则同一次打两遍，字节都不一样。
    """
    项们 = []
    for dirpath, dirnames, filenames in os.walk(目录):
        dirnames.sort()
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            相对 = os.path.relpath(full, 目录).replace(os.sep, "/")
            项们.append((相对, full))
    项们.sort(key=lambda x: x[0].encode("utf-8"))

    with open(输出, "wb") as 原始:
        with gzip.GzipFile(filename="", mode="wb", fileobj=原始, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tf:
                目录信息 = tarfile.TarInfo(顶层名)
                目录信息.type = tarfile.DIRTYPE
                目录信息.mode = 0o755
                目录信息.mtime = 0
                目录信息.uid = 目录信息.gid = 0
                目录信息.uname = 目录信息.gname = ""
                tf.addfile(目录信息)
                for 相对, full in 项们:
                    数据 = open(full, "rb").read()
                    信息 = tarfile.TarInfo(f"{顶层名}/{相对}")
                    信息.size = len(数据)
                    信息.mtime = 0
                    信息.uid = 信息.gid = 0
                    信息.uname = 信息.gname = ""
                    信息.mode = 0o755 if (相对.endswith(".sh")) else 0o644
                    tf.addfile(信息, io.BytesIO(数据))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _找存根() -> str:
    """`jishi-pack-stub` 在哪。

    顺序：装好的发行布局 → 开发时的 `rust/target/release/`。找不到就返回空，
    由调用方决定「提示跳过」还是「报错」。
    """
    名 = "jishi-pack-stub.exe" if os.name == "nt" else "jishi-pack-stub"
    候选 = [os.path.join(os.path.dirname(sys.executable), 名)]
    这里 = os.path.dirname(os.path.abspath(__file__))
    仓库 = os.path.abspath(os.path.join(这里, "..", ".."))
    候选.append(os.path.join(仓库, "rust", "target", "release", 名))
    for p in 候选:
        if os.path.isfile(p):
            return p
    return ""


def _解压到(zip路径: str, 目录: str) -> None:
    os.makedirs(目录, exist_ok=True)
    with zipfile.ZipFile(zip路径) as zf:
        for 名 in zf.namelist():
            if 名.endswith("/"):
                continue
            全 = os.path.join(目录, 名.replace("/", os.sep))
            os.makedirs(os.path.dirname(全) or 目录, exist_ok=True)
            with open(全, "wb") as f:
                f.write(zf.read(名))


def 打包命令(argv: list) -> int:
    """`jishi 打包 [目录] [--出 目录] [--名字 X] [--版本 X] [--入口 X] [--存根 路径]
    [--目标 列表] [--安静]`。

    返回**成品路径列表**（打印出来让用户能直接点；`--安静` 只打印路径）。
    """
    目录 = "."
    出 = ""
    名字 = ""
    版本 = ""
    入口 = ""
    存根 = ""
    目标们 = ""
    安静 = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--出", "--out"):
            i += 1
            出 = argv[i] if i < len(argv) else ""
        elif a in ("--名字", "--name"):
            i += 1
            名字 = argv[i] if i < len(argv) else ""
        elif a in ("--版本", "--version"):
            i += 1
            版本 = argv[i] if i < len(argv) else ""
        elif a in ("--入口", "--entry"):
            i += 1
            入口 = argv[i] if i < len(argv) else ""
        elif a in ("--存根", "--stub"):
            i += 1
            存根 = argv[i] if i < len(argv) else ""
        elif a in ("--目标", "--targets"):
            i += 1
            目标们 = argv[i] if i < len(argv) else ""
        elif a in ("--安静", "-q", "--quiet"):
            安静 = True
        elif a in ("-h", "--help"):
            print("用法：jishi 打包 [目录] [--出 目录] [--名字 X] [--版本 X] "
                  "[--入口 X] [--存根 路径] [--目标 便携,安装包,应用,压缩包] [--安静]")
            print()
            # 前半段模块说明（到「## 三条规矩」为止）——那段正是「四类产物是什么」
            print(__doc__.split("## 三条规矩")[0].split("\n", 1)[1].strip())
            return 0
        elif a.startswith("-"):
            print(f"错误：不认识这个参数「{a}」", file=sys.stderr)
            return 2
        else:
            目录 = a
        i += 1

    源 = os.path.abspath(目录)
    if not os.path.isdir(源):
        print(f"错误：「{目录}」不是目录", file=sys.stderr)
        return 2

    配置 = _读配置(源)
    名字 = 名字 or str(配置.get("名字") or "").strip() or os.path.basename(源)
    版本 = 版本 or str(配置.get("版本") or "").strip() or "0.0.0"
    入口 = 入口 or str(配置.get("入口") or "").strip()
    出 = os.path.abspath(出 or os.path.join(源, "_出"))

    if 存根:
        if not os.path.isfile(存根):
            print(f"错误：找不到自解压存根「{存根}」", file=sys.stderr)
            return 2
    else:
        存根 = _找存根()

    要哪些 = [x.strip() for x in 目标们.split(",") if x.strip()] if 目标们 else list(全部类型)
    for t in 要哪些:
        if t not in 全部类型:
            print(f"错误：不认识的产物类型「{t}」（认：{'、'.join(全部类型)}）",
                  file=sys.stderr)
            return 2
    if "安装包" in 要哪些 and not 存根:
        print("错误：出 Windows 安装包要 `jishi-pack-stub`，没找到。"
              "用 --存根 指给它，或者 `--目标` 里去掉「安装包」。", file=sys.stderr)
        return 2
    警告 = []
    if "应用" in 要哪些 and not 入口:
        要哪些.remove("应用")
        警告.append("没有「入口」，出不了 .app（macOS 靠 Info.plist 里的入口启动）")
    if "安装包" in 要哪些 and not 存根:
        要哪些.remove("安装包")
        警告.append("没找到 jishi-pack-stub，跳过 Windows 安装包")

    文件们 = _收集(源, [出])
    if not 文件们:
        print(f"错误：「{源}」里没有文件", file=sys.stderr)
        return 2
    # 🔴 入口必须在**要打包的目录里**（M188 加，与 Rust 侧同一条）：
    #    入口写错（或者忘了把那个 .jsh 放进目录）时，包照样能出，
    #    装完双击的后果是「找不到文件」—— 用户看到的是「装了个坏东西」。
    if 入口 and not os.path.isfile(os.path.join(源, 入口)):
        文 = ("错误：入口「" + 入口 + "」不在「" + 源 + "」里 —— 装上之后双击会「找不到文件」。"
              + chr(10) + "　　 要么把它放进这个目录，要么把 --入口 写成目录里真有的那个文件。")
        print(文, file=sys.stderr)
        return 2

    os.makedirs(出, exist_ok=True)
    成品 = []          # [(类型, 路径)]
    临时 = tempfile.mkdtemp(prefix="jishi-pack-")
    try:
        # 每个产物自己一个包：清单里的「平台」要跟产物名对得上，
        # 而且 setup.exe 的名字就是从清单里取的。
        def 打一个包(平台: str) -> str:
            选项 = {"名字": 名字, "版本": 版本, "平台": 平台,
                    "入口": 入口, "基础": 源}
            return 打包模块.生成(文件们, 临时, 选项)

        if "便携" in 要哪些:
            包 = 打一个包("")
            目标 = os.path.join(出, os.path.basename(包))
            shutil.copyfile(包, 目标)
            成品.append(("便携", 目标))

        if "安装包" in 要哪些:
            包 = 打一个包(平台表["安装包"])
            目标 = os.path.join(出, f"{名字}-{版本}-windows-x64-setup.exe")
            打包模块.安装包(包, 存根, 目标)
            成品.append(("安装包", 目标))

        if "应用" in 要哪些:
            包 = 打一个包(平台表["应用"])
            束 = os.path.join(临时, f"{名字}.app")
            内容 = os.path.join(束, "Contents")
            _解压到(包, os.path.join(内容, "MacOS"))
            with open(os.path.join(内容, "Info.plist"), "w",
                      encoding="utf-8", newline="\n") as f:
                f.write(应用清单(名字, 版本, 入口))
            with open(os.path.join(内容, "PkgInfo"), "w",
                      encoding="utf-8", newline="\n") as f:
                f.write("APPL????")
            # `.app` 本身**就是产物**（macOS 上它是个目录，双击就开），
            # 所以要落在 `--出` 里，不能做完 tar 就跟着临时目录一起删掉。
            成品束 = os.path.join(出, f"{名字}.app")
            if os.path.isdir(成品束):
                shutil.rmtree(成品束)
            shutil.copytree(束, 成品束)
            tar = os.path.join(出, f"{名字}-{版本}-macos-arm64.tar.gz")
            _写tar_gz(成品束, f"{名字}.app", tar)
            成品.append(("应用", 成品束))
            成品.append(("应用", tar))

        if "压缩包" in 要哪些:
            包 = 打一个包(平台表["压缩包"])
            顶层 = f"{名字}-{版本}-linux-x64"
            暂存 = os.path.join(临时, 顶层)
            _解压到(包, 暂存)
            with open(os.path.join(暂存, "install.sh"), "w",
                      encoding="utf-8", newline="\n") as f:
                f.write(安装脚本(名字, 入口))
            os.chmod(os.path.join(暂存, "install.sh"), 0o755)
            tar = os.path.join(出, f"{顶层}.tar.gz")
            _写tar_gz(暂存, 顶层, tar)
            成品.append(("压缩包", tar))
    finally:
        shutil.rmtree(临时, ignore_errors=True)

    if not 安静:
        for w in 警告:
            print(f"⚠️  {w}", file=sys.stderr)
        print(f"打包「{名字} {版本}」→ {出}")
        for 类, 路径 in 成品:
            print(f"  {类}：{路径}")
    else:
        for _, 路径 in 成品:
            print(路径)
    return 0
