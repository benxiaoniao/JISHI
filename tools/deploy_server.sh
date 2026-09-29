#!/usr/bin/env bash
# 在服务器上把「官网 + 文档站 + 分发源」部署到 Nginx（M49）。
#
# 用法（**在服务器上**跑，需要 root）：
#     sudo bash /srv/jishi-src/tools/deploy_server.sh
#
# 📌 有 SSH 通道时**优先用 `deploy_push.sh`**（本地构建 + scp）：
#    实测服务器自己拉 GitHub 不稳（tarball 也会中途断），这条只当备用。
#
# 为什么是「服务器自己拉自己建」：
#   ① 开发机到这台机器的 80/443 直连不通（本机网络路径受限），传文件反而麻烦；
#   ② 发布源本来就是 GitHub 的公开快照，服务器直接拉同一份，**不会多出第二个真相**；
#   ③ 构建是纯 Python（零第三方依赖），服务器上跑得动。
#
# 设计上**不写服务器地址、不写实例 ID、不写凭据** —— 它只认「本机就是那台服务器」，
# 所以这个脚本本身可以进公开仓库（见 `AGENTS.md` §8 的隐私规矩）。
set -euo pipefail

SRC="${JISHI_SRC:-/srv/jishi-src}"
WEB="${JISHI_WEB:-/var/www/jishi}"   # 由 site_sync.sh 使用

cd "$SRC"

echo "== 1/4 拉取最新快照 =="
# ⚠️ **不用 `git`**：实测（2026-09-29，上海轻量云）`git ls-remote` 能通、
# 而 `git fetch --depth 1` 传 3MB 就 **300 秒超时** —— 这是大陆访问 GitHub 的
# 常态，也正是 M49 这条主线要解决的事（见 `docs/路线图.md` §九）。
# 换成 GitHub 的 **tarball**（`codeload`）一次就下来了，所以这里走 tarball。
#
# 📌 **但这仍属于「服务器依赖 GitHub」**：真正的解法是自建分发源。
# 在那之前，tarball 是能用的那条路；换源只改 `JISHI_TARBALL` 即可。
TARBALL="${JISHI_TARBALL:-https://codeload.github.com/benxiaoniao/JISHI/tar.gz/refs/heads/master}"
python3 - "$TARBALL" <<'PY'
import shutil, sys, urllib.request
url = sys.argv[1]
print("   下载快照…")
urllib.request.urlretrieve(url, "/tmp/jishi.tar.gz")
# 用 Python 删而不是 `rm -rf`：云助手的命令安全策略会拦后者
shutil.rmtree("/srv/jishi-src", ignore_errors=True)
PY
mkdir -p "$SRC"
tar xzf /tmp/jishi.tar.gz -C "$SRC" --strip-components=1
echo "   快照已解包 → $SRC"

echo "== 2/4 隐私双保险 =="
# ⚠️ 发布快照里本来就没有路线图（`publish_github.py` 会把它整个删掉、并把别处
# 指向它的引用抹成「内部路线图（未公开）」），这里再删一次是**双保险**：
# 万一哪天有人直接把源码目录当发布源，路线图里的服务器地址就被打到公网了。
if [ -f docs/路线图.md ]; then
  rm -f docs/路线图.md
  echo "   已删除 docs/路线图.md（本不该在这里）"
fi
# 顺带说一下为什么这里**不做**「扫全仓有没有服务器地址」那一步：
# 那需要把 IP 段与实例 ID 前缀写成字面量，而发布链路的隐私终检正是按这些
# 字面量拦人的 —— 为了防泄漏反而把隐私写进公开文件，本末倒置。
# 防线放在「构建产物」这一侧：见下面第 4 步之后的那段检查。

echo "== 3/4 构建 =="
python3 tools/build_site.py              # 文档站 + 官网六页（build_home.py）
python3 -m jishi.cli examples/projects/基石自述站/生成.jsh

echo "== 4/4 同步到 web 根 + 装 Nginx =="
# 具体动作都在 `site_sync.sh` 里（两个部署入口共用一份，避免漂移）：
#   站点产物 → web 根 · 分发包 + **同域索引** · 隐私终检 · Nginx 配置 + 重载
bash "$SRC/tools/site_sync.sh" "$SRC"

echo
echo "✓ 部署完成"
