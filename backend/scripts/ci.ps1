<#
.SYNOPSIS
Stage gate for the backend: full migration + test + static-check run on a brand new database.

.DESCRIPTION
Deliberately ASCII-only: Windows PowerShell reads BOM-less files using the system ANSI
code page, which mangles non-ASCII comments and can even break string parsing.
The Chinese rationale for this gate lives in README.md instead.

The implementation order requires real PostgreSQL (no SQLite, no mocks) and asks for a
minimal CI to exist already at stage A/C (review item D6). This script fixes that gate as
one repeatable command, used by both local runs and CI:

1. recreate the dedicated CI database and enable pgvector
2. base -> head on the empty database
3. alembic check (models and migrations must not drift)
4. full test run against the freshly built database (unit / integration / concurrency)
5. ruff check + ruff format --check + mypy
6. the constraint probe (raw-SQL independent re-verification)

Any failing step is reported and the script exits non-zero.

.NOTES
`alembic check` does NOT cover CHECK expressions or triggers (review item D7); that part is
covered by tests/integration/test_catalog_contract.py and scripts/probe_constraints.py,
so both run inside the test/probe steps below.
#>

[CmdletBinding()]
param(
    # Dedicated CI database name; it is dropped and recreated on every run.
    [string]$Database = "autumn_ci",
    # Reuse an existing database instead of rebuilding it (faster while debugging).
    [switch]$SkipRebuild
)

# Deliberately "Continue", not "Stop": every step of this gate is a *native* tool
# (alembic, pytest, ruff, mypy) that logs to stderr as a matter of course, and with
# ErrorActionPreference=Stop PowerShell 7.4+ turns that stderr output into a
# terminating NativeCommandError, aborting the gate on a *successful* command.
# Failures are detected explicitly through $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"

if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$backendRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $backendRoot ".venv\Scripts\python.exe"
$alembic = Join-Path $backendRoot ".venv\Scripts\alembic.exe"

if (-not (Test-Path $python)) {
    Write-Error "Virtualenv interpreter not found: $python"
}

# Keep temp files inside the workspace: guaranteed writable in the sandbox and in CI.
$env:TMPDIR = Join-Path $backendRoot ".venv-tmp"
New-Item -ItemType Directory -Force -Path $env:TMPDIR | Out-Null
$env:TEMP = $env:TMPDIR
$env:TMP = $env:TMPDIR
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Push-Location $backendRoot
$failures = [System.Collections.Generic.List[string]]::new()

function Invoke-Step {
    param([string]$Name, [scriptblock]$Body)
    Write-Host "`n=== $Name ===" -ForegroundColor Cyan
    $global:LASTEXITCODE = 0
    # Merge stderr into the host stream so tool logging is visible but never fatal.
    & $Body 2>&1 | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) {
        $script:failures.Add("$Name (exit code $LASTEXITCODE)")
        Write-Host "  -> FAILED" -ForegroundColor Red
    }
    else {
        Write-Host "  -> OK" -ForegroundColor Green
    }
}

try {
    if (-not $SkipRebuild) {
        Invoke-Step "recreate CI database $Database" {
            # The recreation logic lives in a Python script rather than an inline
            # here-string: embedding multi-line Python in PowerShell is fragile
            # (quoting, `$` escaping) and its failures are hard to read.
            & $python scripts\recreate_database.py $Database
        }
    }

    $ciUrl = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5442/$Database"
    $env:AUTUMN_DATABASE_URL = $ciUrl
    $env:AUTUMN_TEST_DATABASE_URL = $ciUrl

    Invoke-Step "empty database base -> head" { & $alembic upgrade head }
    Invoke-Step "alembic check" { & $alembic check }
    Invoke-Step "pytest" { & $python -m pytest -q -p no:cacheprovider }
    Invoke-Step "ruff check" { & $python -m ruff check src tests scripts alembic --output-format=concise }
    Invoke-Step "ruff format --check" { & $python -m ruff format --check src tests scripts alembic }
    Invoke-Step "mypy" { & $python -m mypy }
    Invoke-Step "constraint probe" { & $python scripts\probe_constraints.py }
}
finally {
    Pop-Location
}

Write-Host ""
if ($failures.Count -gt 0) {
    Write-Host "GATE FAILED, $($failures.Count) step(s):" -ForegroundColor Red
    foreach ($item in $failures) { Write-Host "  - $item" -ForegroundColor Red }
    exit 1
}
Write-Host "GATE PASSED." -ForegroundColor Green
exit 0
