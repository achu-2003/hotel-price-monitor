# Start the whole stack natively on Windows -- no Docker, no admin rights.
#
#     .\scripts\dev-start.ps1
#
# Everything it starts is a plain user process. Nothing is registered as a
# Windows service, and all state lives under .local\ -- delete that folder and
# the machine is exactly as it was.
#
# Stop everything with .\scripts\dev-stop.ps1
#
# WHY --pool=solo FOR CELERY
# ==========================
# Celery's default prefork pool relies on fork(), which Windows does not have.
# solo runs one task at a time in-process: fine for developing against one or
# two hotels, but it means a 30-second browser fetch blocks the notify queue
# behind it. Production (Linux/Docker) uses prefork with real concurrency.

# NOT "Stop". PowerShell 5.1 wraps any stderr from a native .exe in an
# ErrorRecord, and pg_ctl writes its perfectly normal "waiting for server to
# start..." to stderr. With ErrorActionPreference=Stop that terminated this
# script after Postgres came up but before Redis and Celery did, leaving a
# half-started stack that still looked alive on port 8000.
$ErrorActionPreference = "Continue"
$proj = Split-Path -Parent $PSScriptRoot
$local = Join-Path $proj ".local"
$py = Join-Path $proj ".venv312\Scripts"

function Test-Port($port) {
    $null -ne (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
}

Set-Location $proj

# -- PostgreSQL ------------------------------------------------------
if (Test-Port 5432) {
    Write-Host "postgres  : already listening on 5432" -ForegroundColor DarkGray
} else {
    & "$local\pgsql\bin\pg_ctl.exe" -D "$local\pgdata" -l "$local\pg.log" `
        -o "-p 5432 -c listen_addresses=127.0.0.1" start | Out-Null
    Start-Sleep -Seconds 3
    if (Test-Port 5432) { Write-Host "postgres  : started" -ForegroundColor Green }
    else { Write-Host "postgres  : FAILED - see .local\pg.log" -ForegroundColor Red }
}

# -- Redis -----------------------------------------------------------
if (Test-Port 6379) {
    Write-Host "redis     : already listening on 6379" -ForegroundColor DarkGray
} else {
    Start-Process -FilePath "$local\redis\redis-server.exe" `
        -ArgumentList "`"$local\redis.conf`"" -WindowStyle Hidden
    Start-Sleep -Seconds 3
    if (Test-Port 6379) { Write-Host "redis     : started" -ForegroundColor Green }
    else { Write-Host "redis     : FAILED to bind 6379" -ForegroundColor Red }
}

# -- API -------------------------------------------------------------
# --reload watches app\ and restarts on save, so editing a route or a template
# needs nothing from you.
if (Test-Port 8000) {
    Write-Host "api       : already listening on 8000" -ForegroundColor DarkGray
} else {
    Start-Process -FilePath "$py\uvicorn.exe" `
        -ArgumentList "app.main:app","--host","127.0.0.1","--port","8000","--reload","--reload-dir","app" `
        -WorkingDirectory $proj -WindowStyle Hidden `
        -RedirectStandardOutput "$local\api.log" -RedirectStandardError "$local\api.err.log"
    Write-Host "api       : started" -ForegroundColor Green
}

# -- Celery worker + beat --------------------------------------------
# Workers do NOT auto-reload. After changing a task, an adapter, or anything
# under app\services\, re-run this script (it restarts them) or stop and start.
$worker = Get-CimInstance Win32_Process -Filter "Name='celery.exe'" -ErrorAction SilentlyContinue
if ($worker) {
    Write-Host "celery    : already running ($($worker.Count) processes)" -ForegroundColor DarkGray
} else {
    Start-Process -FilePath "$py\celery.exe" `
        -ArgumentList "-A","app.workers.celery_app","worker","-Q","browser,http,notify","--pool=solo","--loglevel","INFO" `
        -WorkingDirectory $proj -WindowStyle Hidden `
        -RedirectStandardOutput "$local\worker.log" -RedirectStandardError "$local\worker.err.log"
    Start-Process -FilePath "$py\celery.exe" `
        -ArgumentList "-A","app.workers.celery_app","beat","--loglevel","INFO" `
        -WorkingDirectory $proj -WindowStyle Hidden `
        -RedirectStandardOutput "$local\beat.log" -RedirectStandardError "$local\beat.err.log"
    Write-Host "worker    : started (solo pool)" -ForegroundColor Green
    Write-Host "beat      : started (dispatches every 60s)" -ForegroundColor Green
}

Start-Sleep -Seconds 6

# -- Confirm it actually works, rather than just claiming it did -----
try {
    $ready = Invoke-RestMethod "http://127.0.0.1:8000/api/v1/health/ready" -TimeoutSec 20
    Write-Host ""
    Write-Host ("readiness : database=" + $ready.database + "  redis=" + $ready.redis) -ForegroundColor Cyan
    if ($ready.status -ne "ready") { Write-Host "  detail: $($ready.detail)" -ForegroundColor Yellow }
} catch {
    Write-Host "readiness : API not answering yet -- check .local\api.err.log" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  Dashboard  http://127.0.0.1:8000" -ForegroundColor White
Write-Host "  API docs   http://127.0.0.1:8000/docs" -ForegroundColor White
Write-Host ""
Write-Host "  Logs: .local\api.err.log  .local\worker.err.log  .local\beat.err.log"
Write-Host "  Stop: .\scripts\dev-stop.ps1"
