#!/usr/bin/env bash
# 基石 jishi 安装脚本（Linux / macOS）
# 用法：bash install.sh
# 默认装到用户目录 ~/.jishi，把 bin 加入 shell 配置的 PATH。

set -e

VERSION="__VERSION__"   # 构建发行包时由 tools/build_release.py 注入实际版本
# 安装目录：默认 ~/.jishi，可用 JISHI_HOME 覆盖（自动化 / 多版本并存时用）
INSTALL_DIR="${JISHI_HOME:-$HOME/.jishi}"
BIN_DIR="$INSTALL_DIR/bin"
# 脚本所在目录（发行包根目录）
SRC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== 基石 jishi $VERSION 安装 =="

# 1. 复制 bin 与 share
mkdir -p "$INSTALL_DIR"
cp -R "$SRC_ROOT/bin" "$INSTALL_DIR/"
[ -d "$SRC_ROOT/share" ] && cp -R "$SRC_ROOT/share" "$INSTALL_DIR/"

# 1.5 记下装的是哪个版本（`jishi 升级` 靠它判断「本机现在是哪一版」；
#     在 Inno 版里这行不存在也没关系 —— 那时直接问正在跑的那个可执行文件）
echo "$VERSION" > "$INSTALL_DIR/VERSION"

# 2. 把 bin 加入 PATH（追加到 shell rc，幂等）
#    ⚠️ `JISHI_NO_PATH=1` 时**跳过改 shell 配置** —— 自动化（CI / 升级演练）里
#    不该动用户的 rc 文件。
RC_FILE=""
case "$SHELL" in
  */zsh) RC_FILE="$HOME/.zshrc" ;;
  */bash) RC_FILE="$HOME/.bashrc" ;;
  *) RC_FILE="$HOME/.bashrc" ;;
esac

EXPORT_LINE="export PATH=\"$INSTALL_DIR/bin:\$PATH\""
if [ -n "${JISHI_NO_PATH:-}" ]; then
  echo "  （JISHI_NO_PATH=1：跳过改 PATH）"
elif ! grep -qF "$INSTALL_DIR/bin" "$RC_FILE" 2>/dev/null; then
  echo "$EXPORT_LINE" >> "$RC_FILE"
  echo "  已把 $INSTALL_DIR/bin 加入 $RC_FILE"
else
  echo "  $RC_FILE 已包含安装目录"
fi

# 3. 桌面项（Linux，M35）：让语言图标出现在应用菜单里
#    macOS 走 .app 包，桌面项是 freedesktop 规范的东西，只对 Linux 有意义。
if [ "$(uname -s)" = "Linux" ] && [ -f "$INSTALL_DIR/share/基石.desktop" ]; then
  APPS_DIR="$HOME/.local/share/applications"
  mkdir -p "$APPS_DIR"
  # 桌面项要求绝对路径，所以把占位符换成实际安装目录
  sed "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
    "$INSTALL_DIR/share/基石.desktop" > "$APPS_DIR/jishi.desktop"
  chmod +x "$APPS_DIR/jishi.desktop"
  # 刷新桌面数据库（没有 update-desktop-database 也不影响文件生效）
  command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
  echo "  已安装桌面项：$APPS_DIR/jishi.desktop（应用菜单里可搜「基石」）"
fi

# 4. 自检
echo ""
echo "== 自检 =="
"$BIN_DIR/jishi" --version || { echo "  自检失败"; exit 1; }

echo ""
echo "[OK] 安装完成！"
echo "  下一步："
echo "  1. 运行  source $RC_FILE  或新开终端（让 PATH 生效）"
echo "  2. 运行  jishi 新项目 你好  创建第一个项目"
echo "  3. 或运行  jishi --ai-card  查看语言卡"
