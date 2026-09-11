#!/usr/bin/env bash
# 基石 jishi 安装脚本（Linux / macOS）
# 用法：bash install.sh
# 默认装到用户目录 ~/.jishi，把 bin 加入 shell 配置的 PATH。

set -e

VERSION="__VERSION__"   # 构建发行包时由 tools/build_release.py 注入实际版本
INSTALL_DIR="$HOME/.jishi"
BIN_DIR="$INSTALL_DIR/bin"
# 脚本所在目录（发行包根目录）
SRC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== 基石 jishi $VERSION 安装 =="

# 1. 复制 bin 与 share
mkdir -p "$INSTALL_DIR"
cp -R "$SRC_ROOT/bin" "$INSTALL_DIR/"
[ -d "$SRC_ROOT/share" ] && cp -R "$SRC_ROOT/share" "$INSTALL_DIR/"

# 2. 把 bin 加入 PATH（追加到 shell rc，幂等）
RC_FILE=""
case "$SHELL" in
  */zsh) RC_FILE="$HOME/.zshrc" ;;
  */bash) RC_FILE="$HOME/.bashrc" ;;
  *) RC_FILE="$HOME/.bashrc" ;;
esac

EXPORT_LINE="export PATH=\"\$HOME/.jishi/bin:\$PATH\""
if ! grep -qF "$INSTALL_DIR/bin" "$RC_FILE" 2>/dev/null; then
  echo "$EXPORT_LINE" >> "$RC_FILE"
  echo "  已把 $INSTALL_DIR/bin 加入 $RC_FILE"
else
  echo "  $RC_FILE 已包含安装目录"
fi

# 3. 自检
echo ""
echo "== 自检 =="
"$BIN_DIR/jishi" --version || { echo "  自检失败"; exit 1; }

echo ""
echo "[OK] 安装完成！"
echo "  下一步："
echo "  1. 运行  source $RC_FILE  或新开终端（让 PATH 生效）"
echo "  2. 运行  jishi 新项目 你好  创建第一个项目"
echo "  3. 或运行  jishi --ai-card  查看语言卡"
