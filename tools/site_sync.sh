#!/usr/bin/env bash
# 在服务器上把「已构建的站点 + 分发包」装到 Nginx（M49）。
#
# 用法（**在服务器上**跑，需要 root）：
#     sudo bash tools/site_sync.sh [源码树目录]
#
# 它做四件事：
#   ① 站点产物 → web 根（并把 `基石自述/` 改名成 `about/`，中文路径在 URL 里要编码）
#   ② 分发包 + **同域索引** → web 根（索引里的下载地址改写成这台机器自己的域名）
#   ③ 隐私终检（web 根里绝不能有路线图页面）
#   ④ 装 Nginx 配置并重载（含 443，见下面注释）
#
# 设计上**不写服务器 IP、不写实例 ID、不写凭据** —— 只认「本机就是那台服务器」，
# 所以这份脚本本身可以进公开仓库（见 `AGENTS.md` §8）。
set -euo pipefail

SRC="${1:-${JISHI_SRC:-/srv/jishi-src}}"
WEB="${JISHI_WEB:-/var/www/jishi}"
# URL 里必须用 punycode —— 中文域名的 ASCII 形式（**域名是公开信息，可以写**）
DOMAIN="${JISHI_DOMAIN:-xn--3jsy75e.cn}"
GH_BASE="https://raw.githubusercontent.com/benxiaoniao/JISHI/master/packages/"
SNIPPETS=/etc/nginx/snippets
AVAIL=/etc/nginx/sites-available
ENABLED=/etc/nginx/sites-enabled

if [ "$(id -u)" -ne 0 ]; then
  echo "需要 root（要写 /var/www 与 /etc/nginx），请用 sudo 跑" >&2
  exit 1
fi

cd "$SRC"

echo "== 1/5 站点产物 → web 根 =="
mkdir -p "$WEB/packages/下载"
cp -r site/. "$WEB/"
# 中文目录名在 URL 里要百分号编码，换成 ASCII 更好用
if [ -d "$WEB/基石自述" ]; then
  rm -rf "$WEB/about"
  mv "$WEB/基石自述" "$WEB/about"
fi
echo "   web 根现有 $(find "$WEB" -type f | wc -l) 个文件"

echo "== 2/5 分发包（zip + 同域索引 + 发行包镜像）=="
n_zip=0
for z in packages/下载/*.zip; do
  [ -f "$z" ] || continue
  cp "$z" "$WEB/packages/下载/"
  n_zip=$((n_zip + 1))
done
echo "   同步 $n_zip 个 zip"

# 发行包镜像（安装程序 / 各平台压缩包）：`deploy_push.sh` 传上来的 `mirror/`。
# ⚠️ 目录名用 **ASCII 的 `download/`**（中文标题在 URL 里要百分号编码，对下载不友好）。
n_rel=0
if [ -d "$SRC/mirror" ]; then
  mkdir -p "$WEB/download"
  for f in "$SRC/mirror"/*; do
    [ -f "$f" ] || continue
    cp "$f" "$WEB/download/"
    n_rel=$((n_rel + 1))
  done
fi
echo "   发行包（本站在线下载）：$n_rel 个文件"
# ⚠️ 索引要**改写**成同域：zip 从哪台机器拿，索引里的地址就指哪台机器。
#    两份索引各自自洽（仓库那份指 GitHub、这份指自建源），
#    这样「自建源故障 → 退到 GitHub」是整条链路一起退，不会半路断掉。
python3 tools/build_index_mirror.py \
  "$SRC/packages/索引.json" "$WEB/packages/索引.json" \
  --from "$GH_BASE" --to "https://$DOMAIN/packages/" \
  --zip-dir "$SRC/packages/下载"

echo "== 3/5 隐私终检 =="
# 路线图是**唯一**含服务器信息的文档，绝不能出现在 web 根里。
# 发布链路（publish_github.py）已经把它整个删掉了，这里是第二道。
if [ -f "$WEB/docs/路线图.html" ]; then
  rm -f "$WEB/docs/路线图.html"
  echo "   ⚠️ 产物里出现了路线图页面，已删除（构建源本该没有它）" >&2
fi
rm -f "$WEB/.well-known/acme-challenge/probe_test"
echo "   通过（web 根里没有路线图页面）"

echo "== 4/5 Nginx 配置 =="
mkdir -p "$SNIPPETS"
# 站点规则单独成文件：80 与 443 块 include 同一份，避免两处漂移
cp tools/nginx-jishi-locations.conf "$SNIPPETS/jishi-locations.conf"
cp tools/nginx-jishi.conf "$AVAIL/jishi"
ln -sf "$AVAIL/jishi" "$ENABLED/jishi"

CERT="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
if [ -f "$CERT" ]; then
  cp tools/nginx-jishi-tls.conf "$AVAIL/jishi-tls"
  ln -sf "$AVAIL/jishi-tls" "$ENABLED/jishi-tls"
  # 续期后必须 reload，否则 nginx 一直用旧证书（到期就报错，且很难联想到原因）
  hook=/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
  mkdir -p "$(dirname "$hook")"
  printf '#!/bin/sh\nnginx -t && systemctl reload nginx\n' > "$hook"
  chmod +x "$hook"
  echo "   HTTPS：已启用（证书 + 续期后自动重载已装好）"
else
  # 证书还没申请时先别 enable —— 否则 `nginx -t` 会因为读不到证书而失败
  rm -f "$ENABLED/jishi-tls"
  echo "   HTTPS：跳过（没有 $CERT）"
  echo "     想开 HTTPS 就跑：certbot certonly --webroot -w $WEB -d $DOMAIN"
fi
rm -f "$ENABLED/default"
nginx -t

echo "== 5/5 重载 Nginx =="
systemctl reload nginx

echo
echo "✓ 部署完成 → $WEB"
echo "  官网：https://$DOMAIN/"
echo "  分发：https://$DOMAIN/packages/索引.json"
