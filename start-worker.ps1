. "$PSScriptRoot\_backend-env.ps1"

Write-Host "[Worker] Starting Worker..."

& $backendPython -m autumn_backend.workers