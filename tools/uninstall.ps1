# 基石 jishi 卸载脚本（Windows PowerShell）
# 用法：powershell -ExecutionPolicy Bypass -File uninstall.ps1

$ErrorActionPreference = "Stop"
$installDir = Join-Path $env:USERPROFILE ".jishi"

Write-Host "== 卸载基石 jishi ==" -ForegroundColor Cyan

# 1. 从用户 PATH 移除安装目录
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -like "*$installDir\bin*") {
    $newPath = ($userPath -split ";" | Where-Object { $_ -and $_ -ne "$installDir\bin" }) -join ";"
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "  已从用户 PATH 移除 $installDir\bin" -ForegroundColor Green
}

# 2. 删除安装目录
if (Test-Path $installDir) {
    Remove-Item -Recurse -Force $installDir
    Write-Host "  已删除 $installDir" -ForegroundColor Green
} else {
    Write-Host "  未找到安装目录（可能已卸载）" -ForegroundColor Yellow
}

Write-Host "`n[OK] 卸载完成。" -ForegroundColor Green
