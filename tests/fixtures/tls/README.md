# 判据专用的自签证书（**不是**生产密钥）

这两份文件只给 `tests/test_m206_http_hardening.py` 用：起一个本地 HTTPS 服务、
验证「自签证书必须被拒」和「给了 `JISHI_TLS_CA` 就能连自签」。

- `CN=127.0.0.1`，SAN = `IP:127.0.0.1, DNS:localhost`，有效期 10 年
- **私钥是公开的**（就在这个文件旁边），任何人在任何机器上都能拿它签同样的证书
  —— 所以它**证明不了任何身份**，只适合本机回环地址上的判据。

重新生成（证书过期时）：

```bash
openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem \
  -days 3650 -subj "/CN=127.0.0.1" \
  -addext "subjectAltName=IP:127.0.0.1,DNS:localhost"
```

⚠️ **不要**把这份证书装进系统信任存储（判据也不这么做 ——
`test_Rust宿主的TLS也认JISHI_TLS_CA` 因此在 Windows 上是跳过的）。
