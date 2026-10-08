$frontendDir = Join-Path $PSScriptRoot "frontend"

if (-not (Test-Path $frontendDir)) {
    throw "Frontend directory not found: $frontendDir"
}

Set-Location $frontendDir

$env:NEXT_PUBLIC_API_MODE = "api"
$env:FASTAPI_ORIGIN = "http://127.0.0.1:8000"

Write-Host "[Frontend] Starting..."
Write-Host "[Frontend] API: $env:FASTAPI_ORIGIN"

npm run dev -- --hostname 0.0.0.0