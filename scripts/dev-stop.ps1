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
$all = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='uvicorn.exe' OR Name='celery.exe'" -ErrorAction SilentlyContinue

$ours = @($all | Where-Object {
    ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($proj, [StringComparison]::OrdinalIgnoreCase)) -or
    ($_.CommandLine -and $_.CommandLine.IndexOf($proj, [StringComparison]::OrdinalIgnoreCase) -ge 0)
})
$ourIds = @($ours | ForEach-Object { $_.ProcessId })

# CHILDREN FIRST, AND MATCHED BY PARENT.
#
# uvicorn --reload runs the app in a child launched as
#
#     python.exe -c "from multiprocessing.spawn import spawn_main; ..."
#
# whose command line names no path at all -- not the project, not even the
# venv, because it is started with the SYSTEM interpreter. Neither test above
# can see it, so it survived every stop, and the port sweep below could not
# find it either: Windows keeps reporting the socket's ORIGINAL owner, which
# is dead the moment the parent is killed, while the live child holds an
# inherited handle under a different pid. The sweep dutifully killed a pid
# that no longer existed and reported success.
#
# The child is only identifiable while its parent is still alive, so it has to
# be taken first. What that cost, three times: "api: already listening on
# 8000", nothing started, and hours-old code serving the dashboard.
$children = @($all | Where-Object {
    $ourIds -contains $_.ParentProcessId -and $ourIds -notcontains $_.ProcessId
})

$stopped = 0
foreach ($p in @($children) + @($ours)) {
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; $stopped++ } catch {}
}
Write-Host "api/worker/beat : stopped ($stopped processes)" -ForegroundColor Green

# Backstop, for a child orphaned by an EARLIER stop: its parent is long gone,
# so the pass above cannot match it either. Narrow on purpose -- an orphaned
# spawn_main child whose parent no longer exists, and only while our port is
# still held. Broadening this to every spawn_main process on the machine would
# reach into unrelated software.
Start-Sleep -Milliseconds 500
if (Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue) {
    $live = @((Get-CimInstance Win32_Process -ErrorAction SilentlyContinue).ProcessId)
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine.Contains("multiprocessing.spawn") -and
            $live -notcontains $_.ParentProcessId
        } |
        ForEach-Object {
            try {
                Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
                Write-Host "port 8000       : freed (orphaned reload child, pid $($_.ProcessId))" -ForegroundColor Yellow
            } catch {}
        }
}

# Last resort: whatever still holds the port, by the port itself.
foreach ($port in 8000) {
    $owners = (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue).OwningProcess
    foreach ($owner in @($owners | Where-Object { $_ })) {
        try {
            Stop-Process -Id $owner -Force -ErrorAction Stop
            Write-Host "port $port      : freed (pid $owner still held it)" -ForegroundColor Yellow
        } catch {}
    }
}

if (Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue) {
    # Said out loud, because dev-start reads a busy port as "already running"
    # and starts nothing -- which is how old code ends up serving a dashboard
    # that looks freshly restarted.
    Write-Host "port 8000       : STILL HELD -- dev-start will skip the API" -ForegroundColor Red
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
    # Same pipe hazard as the start side, so the output goes to a file and the
    # wait is on pg_ctl itself. Here we DO want to wait: a fast stop is what
    # keeps the cluster from needing crash recovery next time.
    $out = Join-Path $local "pg_ctl-stop.log"
    Start-Process -FilePath "$local\pgsql\bin\pg_ctl.exe" `
        -ArgumentList @("-D", "$local\pgdata", "-m", "fast", "stop") `
        -Wait -WindowStyle Hidden `
        -RedirectStandardOutput $out -RedirectStandardError "$out.err"
    Write-Host "postgres        : stopped cleanly" -ForegroundColor Green
}

Write-Host ""
Write-Host "Data kept in .local\pgdata and .local\redis-data."
Write-Host "Start again with .\scripts\dev-start.ps1"
