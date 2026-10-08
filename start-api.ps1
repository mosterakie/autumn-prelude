. "$PSScriptRoot\_backend-env.ps1"

Write-Host "[API] Starting FastAPI..."

& $backendPython -m uvicorn autumn_backend.app:app `
    --host 127.0.0.1 `
    --port 8000 `
    --reload