#!/usr/bin/env bash
# 基石 jishi 卸载脚本（Linux / macOS）
# 用法：bash uninstall.sh

set -e
INSTALL_DIR="$HOME/.jishi"

echo "== 卸载基石 jishi =="

# 1. 从 shell rc 移除 PATH 行
for RC_FILE in "$HOME/.bashrc" "$HOME/.zshrc"; do
  if [ -f "$RC_FILE" ] && grep -qF "$INSTALL_DIR/bin" "$RC_FILE"; then
    # 删除包含安装目录的那一行
    sed -i.bak "\#$INSTALL_DIR/bin#d" "$RC_FILE"
    rm -f "$RC_FILE.bak"
    echo "  已从 $RC_FILE 移除 PATH 行"
  fi
done

# 2. 删除桌面项（Linux，M35）
APPS_DIR="$HOME/.local/share/applications"
if [ -f "$APPS_DIR/jishi.desktop" ]; then
  rm -f "$APPS_DIR/jishi.desktop"
  command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
  echo "  已删除桌面项 jishi.desktop"
fi

# 3. 删除安装目录
if [ -d "$INSTALL_DIR" ]; then
  rm -rf "$INSTALL_DIR"
  echo "  已删除 $INSTALL_DIR"
else
  echo "  未找到安装目录（可能已卸载）"
fi

echo ""
echo "[OK] 卸载完成。"
