#!/usr/bin/env bash
# 在服务器上把「官网 + 文档站 + 分发源」部署到 Nginx（M49）。
#
# 用法（**在服务器上**跑）：
#     bash /srv/jishi-src/tools/deploy_server.sh
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
WEB="${JISHI_WEB:-/var/www/jishi}"

cd "$SRC"

echo "== 1/5 拉取最新快照 =="
git fetch --depth 1 origin master -q
git checkout -f master -q
git log --oneline -1

echo "== 2/5 隐私双保险 =="
# ⚠️ 发布快照里本来就没有路线图（`publish_github.py` 会把它整个删掉并抹掉引用），
# 这里再删一次是**双保险**：万一哪天有人直接把源码目录当发布源，
# 路线图里的服务器 IP 与实例 ID 就会被打到公网。
if [ -f docs/路线图.md ]; then
  rm -f docs/路线图.md
  echo "   已删除 docs/路线图.md（本不该在这里）"
fi
# 万一有别的文件把服务器地址抄了过去，也拦住
leaks=$(grep -rIl --exclude-dir=.git --exclude-dir=site -E \
        "111\.229\.[0-9]+\.[0-9]+|lhins-[a-z0-9]{6,}" . 2>/dev/null || true)
if [ -n "$leaks" ]; then
  echo "   ⚠️ 下面这些文件里有服务器地址 / 实例 ID，请先清掉再部署：" >&2
  echo "$leaks" >&2
  exit 1
fi
echo "   未发现服务器地址 / 实例 ID"

echo "== 3/5 构建 =="
python3 tools/build_site.py              # 文档站 + 官网六页（build_home.py）
python3 -m jishi.cli examples/projects/基石自述站/生成.jsh

echo "== 4/5 同步到 web 根 =="
rm -rf "$WEB"
mkdir -p "$WEB/packages"
cp -r site/. "$WEB/"
# 中文目录名在 URL 里要百分号编码，换成 ASCII 更好用
[ -d "$WEB/基石自述" ] && mv "$WEB/基石自述" "$WEB/about"
# 分发包：索引 + 各版本的 zip
[ -f packages/索引.json ] && cp packages/索引.json "$WEB/packages/"
find packages -name '*.zip' -exec cp {} "$WEB/packages/" \; 2>/dev/null || true

echo "== 5/5 重载 Nginx =="
nginx -t
systemctl reload nginx

echo
echo "✓ 部署完成 → $WEB"
echo "  官网：https://xn--3jsy75e.cn/"
echo "  分发：https://xn--3jsy75e.cn/packages/索引.json"
