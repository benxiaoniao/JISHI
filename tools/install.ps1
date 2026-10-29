# 基石 jishi 安装脚本（Windows PowerShell）
# 用法：powershell -ExecutionPolicy Bypass -File install.ps1
# 默认装到用户目录 ~/.jishi，把 bin 加入用户 PATH。

$ErrorActionPreference = "Stop"

$version = "__VERSION__"   # 构建发行包时由 tools/build_release.py 注入实际版本
# 安装目录：默认 ~/.jishi，可用 JISHI_HOME 覆盖（自动化 / 多版本并存时用）
if ($env:JISHI_HOME) { $installDir = $env:JISHI_HOME }
else { $installDir = Join-Path $env:USERPROFILE ".jishi" }
$binDir = Join-Path $installDir "bin"

Write-Host "== 基石 jishi $version 安装 ==" -ForegroundColor Cyan

# 1. 定位发行包目录（脚本所在目录）
$srcRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

# 2. 复制 bin 与 share 到安装目录
if (Test-Path $installDir) {
    Write-Host "  已存在安装目录，覆盖更新..." -ForegroundColor Yellow
}
New-Item -ItemType Directory -Force -Path $installDir | Out-Null
Copy-Item -Recurse -Force (Join-Path $srcRoot "bin") $installDir
if (Test-Path (Join-Path $srcRoot "share")) {
    Copy-Item -Recurse -Force (Join-Path $srcRoot "share") $installDir
}

# 2.5 记下装的是哪个版本（`jishi 升级` 靠它判断「本机现在是哪一版」）
Set-Content -Path (Join-Path $installDir "VERSION") -Value $version -NoNewline -Encoding ascii

# 3. 把 bin 加入用户 PATH（幂等：已存在则不重复）
#    ⚠️ `JISHI_NO_PATH=1` 时**跳过改 PATH** —— 自动化（CI / 升级演练）里
#    不该动用户的环境变量。
$binPath = Join-Path $installDir "bin\jishi"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($env:JISHI_NO_PATH) {
    Write-Host "  （JISHI_NO_PATH=1：跳过改 PATH）" -ForegroundColor Yellow
} elseif ($userPath -notlike "*$installDir\bin*") {
    $newPath = "$userPath;$installDir\bin"
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "  已把 $installDir\bin 加入用户 PATH" -ForegroundColor Green
} else {
    Write-Host "  PATH 已包含安装目录" -ForegroundColor Green
}

# 4. 自检
Write-Host "`n== 自检 ==" -ForegroundColor Cyan
& "$binDir\jishi.exe" --version
if ($LASTEXITCODE -ne 0) {
    Write-Host "  自检失败，请检查安装" -ForegroundColor Red
    exit 1
}

Write-Host "`n[OK] 安装完成！" -ForegroundColor Green
Write-Host "  下一步："
Write-Host "  1. 新开一个终端窗口（让 PATH 生效）"
Write-Host "  2. 运行  jishi 新项目 你好  创建第一个项目"
Write-Host "  3. 或运行  jishi --ai-card  查看语言卡"
