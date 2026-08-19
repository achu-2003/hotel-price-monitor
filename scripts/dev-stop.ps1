# Stop everything dev-start.ps1 started.
#
#     .\scripts\dev-stop.ps1
#
# Postgres is stopped with pg_ctl in fast mode rather than by killing the
# process: a killed postmaster leaves the cluster needing crash recovery on
# next start, and in the worst case loses the last commits. Redis, uvicorn and
# celery hold nothing that a restart cannot rebuild, so those are simply
# stopped.
#
# Your data survives -- it lives in .local\pgdata and .local\redis-data.

$proj = Split-Path -Parent $PSScriptRoot
$local = Join-Path $proj ".local"

# -- Python processes: only OURS ------------------------------------
# Matched on the path into this project's venv, so a Python process belonging
# to something else on the machine is never touched.
#
# MATCHED ON THE COMMAND LINE TOO, NOT ONLY THE EXECUTABLE
# ========================================================
# uvicorn --reload runs the application in a CHILD process, and on this machine
# that child is launched with the system interpreter --
# C:\Users\...\Python312\python.exe -- not the one in .venv312. Matching only on
# ExecutablePath therefore skipped it, so:
#
#   * "stop" reported success while the child kept holding port 8000;
#   * "start" then saw the port busy, printed "api: already listening", and
#     started nothing;
#   * the dashboard stayed up throughout, serving the code it had loaded hours
#     earlier -- so edited adapters simply had no effect, and the same bug
#     appeared to survive every restart.
#
# The command line of that child contains this project's path, so it is used as
# the second test. Both tests are anchored to $proj: nothing outside this
# directory is ever matched.
$stopped = 0
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='uvicorn.exe' OR Name='celery.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
        ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($proj, [StringComparison]::OrdinalIgnoreCase)) -or
        ($_.CommandLine -and $_.CommandLine.IndexOf($proj, [StringComparison]::OrdinalIgnoreCase) -ge 0)
    } |
    ForEach-Object {
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $stopped++ } catch {}
    }
Write-Host "api/worker/beat : stopped ($stopped processes)" -ForegroundColor Green

# Whatever survived that -- an orphaned reload child whose parent is already
# gone shows neither path once it is reparented -- is found by what it is
# holding. A port is evidence; a name is a guess.
foreach ($port in 8000) {
    $owners = (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue).OwningProcess
    foreach ($owner in @($owners | Where-Object { $_ })) {
        try {
            Stop-Process -Id $owner -Force -ErrorAction Stop
            Write-Host "port $port      : freed (pid $owner still held it)" -ForegroundColor Yellow
        } catch {}
    }
}

# -- Redis -----------------------------------------------------------
if (Test-Path "$local\redis\redis-cli.exe") {
    # SHUTDOWN NOSAVE is safe here: appendonly is on, so the AOF already has
    # everything. Celery would rebuild the queue from the database regardless.
    & "$local\redis\redis-cli.exe" shutdown nosave 2>$null
}
Get-Process redis-server -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Write-Host "redis           : stopped" -ForegroundColor Green

# -- PostgreSQL ------------------------------------------------------
if (Test-Path "$local\pgsql\bin\pg_ctl.exe") {
    & "$local\pgsql\bin\pg_ctl.exe" -D "$local\pgdata" -m fast stop 2>&1 | Out-Null
    Write-Host "postgres        : stopped cleanly" -ForegroundColor Green
}

Write-Host ""
Write-Host "Data kept in .local\pgdata and .local\redis-data."
Write-Host "Start again with .\scripts\dev-start.ps1"
