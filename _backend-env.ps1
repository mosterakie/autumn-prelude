$projectRoot = $PSScriptRoot
$backendDir = Join-Path $projectRoot "backend"

$backendPython = Join-Path $projectRoot "..\work\backend-audit-venv\Scripts\python.exe"
$backendPython = [System.IO.Path]::GetFullPath($backendPython)

if (-not (Test-Path $backendPython)) {
    throw "Python environment not found: $backendPython"
}

$env:PYTHONPATH = Join-Path $backendDir "src"
$env:AUTUMN_ENVIRONMENT = "local"
$env:AUTUMN_CHECKPOINT_SCHEMA = "autumn_checkpoints_test_h"

# 密码交互输入，不保存在脚本中
$securePassword = Read-Host "PostgreSQL password" -AsSecureString
$plainPassword = [System.Net.NetworkCredential]::new("", $securePassword).Password
$encodedPassword = [Uri]::EscapeDataString($plainPassword)

$env:AUTUMN_DATABASE_URL = "postgresql+asyncpg://postgres:${encodedPassword}@127.0.0.1:5442/autumn_test_develop_c1b2bd0023d2"

Remove-Variable plainPassword, encodedPassword, securePassword

Set-Location $backendDir

Write-Host "[Backend] Python: $backendPython"
Write-Host "[Backend] Environment: $env:AUTUMN_ENVIRONMENT"
Write-Host "[Backend] Database: autumn_test_develop_c1b2bd0023d2"