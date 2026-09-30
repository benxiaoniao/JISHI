#!/usr/bin/env bash
# 从开发机把站点推到服务器并部署（M49）。
#
# 用法：
#     JISHI_SSH_KEY="/path/to/key.pem" JISHI_SSH_HOST="ubuntu@例子.com" \
#         bash tools/deploy_push.sh
#
# 环境变量：
#     JISHI_SSH_KEY     私钥文件路径（**必填**）
#     JISHI_SSH_HOST    SSH 目标，如 ubuntu@例子.com（**必填**）
#     JISHI_REMOTE_DIR  服务器上的落地目录，默认 /srv/upload
#
# 发行包（安装程序等，`dist/release/`）**只在服务器缺失时才传**：
# 它们上百 MB，每次部署都推一遍没人受得了。内容由
# `tools/fetch_release_mirror.py` 从 GitHub Releases 取（同一份字节）。
#     JISHI_PYTHON      本机 Python（默认 python3，找不到就用 python）
#
# ⚠️ 密钥路径与服务器地址**只能从环境变量读**，绝不写进这个文件 ——
#    它在公开仓库里（见 `AGENTS.md` §8 的隐私规矩）。
#
# 为什么是「本地构建 + scp」而不是「服务器自己拉 GitHub」：
#   实测（2026-09-29，上海轻量云）服务器拉 GitHub **不稳** ——
#   `git fetch` 传 3MB 就 300 秒超时；tarball 一次成功、一次 `IncompleteRead`
#   中途断掉。本地构建是一次成功的，再整包传上去，比在服务器上赌网络可靠。
#   （「服务器自己拉」那条路仍在 `deploy_server.sh`，供没有 SSH 通道时用。）
set -euo pipefail

: "${JISHI_SSH_KEY:?请设置 JISHI_SSH_KEY（私钥路径）}"
: "${JISHI_SSH_HOST:?请设置 JISHI_SSH_HOST（例如 ubuntu@例子.com）}"
REMOTE_DIR="${JISHI_REMOTE_DIR:-/srv/upload}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${JISHI_PYTHON:-}"
if [ -z "$PY" ]; then
  if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
fi

SSHOPT=(-i "$JISHI_SSH_KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)

echo "== 1/5 构建站点（$PY）=="
"$PY" tools/build_site.py

echo "== 2/5 打包（站点）=="
TAR=/tmp/jishi-deploy.tar.gz
rm -f "$TAR"
tar czf "$TAR" site packages \
    tools/build_index_mirror.py tools/site_sync.sh \
    tools/nginx-jishi.conf tools/nginx-jishi-tls.conf tools/nginx-jishi-locations.conf
echo "   $(tar tzf "$TAR" | wc -l) 个条目 · $(( $(wc -c < "$TAR") / 1024 )) KB"

echo "== 3/5 发行包（只在服务器缺失时才传）=="
# ⚠️ 发行包上百 MB（自带 Python 运行时）。**每次部署都推一遍是不可接受的** ——
# 改一行文案要等十几分钟，就没人愿意部署了。所以先问服务器「你已经有哪些」，
# 只补缺的（按「文件名 + 字节数」比，不只看文件名：同名的半截文件要能重传）。
MIRROR_TAR=/tmp/jishi-mirror.tar.gz
MIRROR_N=0
rm -f "$MIRROR_TAR"
if [ -d dist/release ] && [ -n "$(ls -A dist/release 2>/dev/null)" ]; then
  remote_list=$(ssh "${SSHOPT[@]}" "$JISHI_SSH_HOST" \
    "for f in '$REMOTE_DIR/download'/*; do [ -f \"\$f\" ] && echo \"\$(basename \"\$f\") \$(wc -c < \"\$f\" | tr -d ' ')\"; done 2>/dev/null || true")
  need=()
  for f in dist/release/*; do
    [ -f "$f" ] || continue
    b="$(basename "$f")"
    sz="$(wc -c < "$f" | tr -d ' ')"
    if ! printf '%s\n' "$remote_list" | grep -qx "$b $sz"; then
      need+=("dist/release/$b")
    fi
  done
  if [ "${#need[@]}" -gt 0 ]; then
    tar czf "$MIRROR_TAR" "${need[@]}"
    MIRROR_N="${#need[@]}"
    echo "   需补传 $MIRROR_N 个（共 $(( $(wc -c < "$MIRROR_TAR") / 1024 / 1024 )) MB）"
  else
    echo "   服务器上已是最新，跳过"
  fi
else
  echo "   （本机没有 dist/release，跳过 —— 先跑 tools/fetch_release_mirror.py）"
fi

echo "== 4/5 上传 =="
scp "${SSHOPT[@]}" "$TAR" "$JISHI_SSH_HOST:/tmp/jishi-deploy.tar.gz"
if [ "$MIRROR_N" -gt 0 ]; then
  scp "${SSHOPT[@]}" "$MIRROR_TAR" "$JISHI_SSH_HOST:/tmp/jishi-mirror.tar.gz"
fi

echo "== 5/5 远端部署 =="
ssh "${SSHOPT[@]}" "$JISHI_SSH_HOST" "
set -euo pipefail
sudo mkdir -p '$REMOTE_DIR'
sudo tar xzf /tmp/jishi-deploy.tar.gz -C '$REMOTE_DIR'
# 发行包单独一个 tar（路径是 dist/release/xxx），去掉两层前缀解到 mirror/
if [ -f /tmp/jishi-mirror.tar.gz ]; then
  sudo mkdir -p '$REMOTE_DIR/mirror'
  sudo tar xzf /tmp/jishi-mirror.tar.gz -C '$REMOTE_DIR/mirror' --strip-components=2
fi
sudo bash '$REMOTE_DIR/tools/site_sync.sh' '$REMOTE_DIR'
"
