#Requires -RunAsAdministrator
<#
    Install the stack as Windows services. Step 11 of
    docs/DEPLOY-WINDOWS-NATIVE.md, as something you run instead of retype.

    THIS IS THE STEP THAT TURNS A DEV STACK INTO A DEPLOYMENT. Everything
    dev-start.ps1 launches is a child of the console session: log off and the
    price checks stop, with no error anywhere.

    Safe to run twice. A service that already exists is left alone rather than
    reconfigured, so this can never half-rewrite a working deployment; remove
    it with `nssm remove <name> confirm` first if you genuinely want it rebuilt.

        .\scripts\install-services.ps1
        .\scripts\install-services.ps1 -Nssm D:\tools\nssm.exe -WhatIf
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    # Where nssm.exe lives. The deploy doc puts it here.
    [string] $Nssm = "C:\nssm\nssm.exe",

    # Where Playwright's browsers live, shared rather than per-user.
    #
    # Services run as LocalSystem, whose profile is
    # C:\Windows\system32\config\systemprofile -- NOT the account that ran
    # `playwright install`. Left to the default the worker looks there, finds
    # nothing, and fails with "Executable doesn't exist" naming a path no one
    # recognises. Install once into this location and point every worker at it.
    [string] $BrowsersPath = "C:\ms-playwright",

    # Install the services but do not start them.
    [switch] $NoStart
)

$ErrorActionPreference = "Stop"

$proj = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$py   = Join-Path $proj ".venv312\Scripts"

# Fail here, with the path in hand, rather than inside a service that starts
# cleanly and then cannot find its interpreter.
foreach ($required in @(
    @{ Path = $Nssm;                                    What = "nssm.exe (https://nssm.cc/download)" }
    @{ Path = (Join-Path $py "celery.exe");             What = "the 3.12 virtualenv" }
    @{ Path = (Join-Path $py "uvicorn.exe");            What = "the 3.12 virtualenv" }
    @{ Path = (Join-Path $proj ".local\pgsql\bin\pg_ctl.exe"); What = "the Postgres binaries" }
    @{ Path = (Join-Path $proj ".local\redis\redis-server.exe"); What = "the Redis binaries" }
    @{ Path = (Join-Path $proj ".local\redis.conf");     What = "the Redis config" }
    @{ Path = (Join-Path $proj ".local\pgdata");         What = "the Postgres cluster" }
    @{ Path = (Join-Path $proj ".env");                  What = "the environment file" }
)) {
    if (-not (Test-Path $required.Path)) {
        throw "Missing $($required.What): $($required.Path)"
    }
}

if (-not (Test-Path (Join-Path $BrowsersPath "chromium_headless_shell-*"))) {
    Write-Warning @"
No headless Chromium under $BrowsersPath. The browser workers will install
cleanly and then fail every fetch. Fix it before or after this script with:

  `$env:PLAYWRIGHT_BROWSERS_PATH='$BrowsersPath'; .\.venv312\Scripts\python.exe -m playwright install chromium
"@
}

Write-Host "Project : $proj"
Write-Host "NSSM    : $Nssm"
Write-Host "Browsers: $BrowsersPath"
Write-Host ""

# Celery workers get a node name each. Without one they collide, and Celery's
# control channel starts answering for the wrong process -- which reads as a
# worker that looks alive in `celery inspect` and is consuming nothing.
# %h is Celery's own placeholder for the hostname; NSSM passes the argument
# string straight through with no shell, so it needs no escaping here.
$workerArgs = "-A app.workers.celery_app worker --pool=solo --loglevel INFO"

$pgService = "HotelMonitor-Postgres"

$services = @(
    @{
        Name = "HotelMonitor-Redis"
        Exe  = Join-Path $proj ".local\redis\redis-server.exe"
        Args = "`"$proj\.local\redis.conf`""
    }
    @{
        Name      = "HotelMonitor-API"
        Exe       = Join-Path $py "uvicorn.exe"
        Args      = "app.main:app --host 127.0.0.1 --port 8000"
        DependsOn = @("HotelMonitor-Postgres", "HotelMonitor-Redis")
    }
    # FOUR workers, not one. A 30-second browser fetch on a single solo worker
    # blocks the notify queue behind it, so an alert waits on a page load.
    @{
        Name      = "HotelMonitor-Worker-Browser1"
        Exe       = Join-Path $py "celery.exe"
        Args      = "$workerArgs -Q browser -n browser1@%h"
        Env       = "PLAYWRIGHT_BROWSERS_PATH=$BrowsersPath"
        DependsOn = @("HotelMonitor-Redis", "HotelMonitor-Postgres")
    }
    @{
        Name      = "HotelMonitor-Worker-Browser2"
        Exe       = Join-Path $py "celery.exe"
        Args      = "$workerArgs -Q browser -n browser2@%h"
        Env       = "PLAYWRIGHT_BROWSERS_PATH=$BrowsersPath"
        DependsOn = @("HotelMonitor-Redis", "HotelMonitor-Postgres")
    }
    @{
        Name      = "HotelMonitor-Worker-Notify"
        Exe       = Join-Path $py "celery.exe"
        Args      = "$workerArgs -Q notify -n notify1@%h"
        Env       = "PLAYWRIGHT_BROWSERS_PATH=$BrowsersPath"
        DependsOn = @("HotelMonitor-Redis", "HotelMonitor-Postgres")
    }
    @{
        Name      = "HotelMonitor-Worker-Http"
        Exe       = Join-Path $py "celery.exe"
        Args      = "$workerArgs -Q http -n http1@%h"
        Env       = "PLAYWRIGHT_BROWSERS_PATH=$BrowsersPath"
        DependsOn = @("HotelMonitor-Redis", "HotelMonitor-Postgres")
    }
    # EXACTLY ONE beat, always. Beat is the scheduler, not a worker: a second
    # one does not share the load, it duplicates it -- every hotel checked
    # twice, every alert sent twice.
    @{
        Name      = "HotelMonitor-Beat"
        Exe       = Join-Path $py "celery.exe"
        Args      = "-A app.workers.celery_app beat --loglevel INFO"
        DependsOn = @("HotelMonitor-Redis")
    }
)

function Invoke-Nssm {
    param([string[]] $NssmArgs)
    # No `2>&1` here on purpose. Windows PowerShell 5.1 wraps a native
    # command's stderr in ErrorRecords, so with ErrorActionPreference = Stop a
    # perfectly successful nssm call that happened to write a line of chatter
    # would throw. The exit code is the truth; let stderr go to the console.
    $output = & $Nssm @NssmArgs
    if ($LASTEXITCODE -ne 0) {
        throw "nssm $($NssmArgs -join ' ') failed (exit $LASTEXITCODE): $output"
    }
}

# Postgres is deliberately NOT an NSSM service.
#
# `pg_ctl start` launches the server and then exits, having done its job. A
# wrapper that supervises the process it started reads that exit as a crash and
# restarts it, which loops: the service flickers between Running and Stopped,
# and anything with DependOnService against it is refused at random. That is
# not hypothetical -- it is how this script failed on its first real run.
#
# There is a second reason no wrapper substitutes for this. postgres.exe
# refuses to run as a user with administrative permissions, and a service
# account is exactly that. `pg_ctl register` builds the restricted token that
# makes the server willing to start at all, so Postgres's own service support
# is not a convenience here; it is the only thing that works.
if (Get-Service -Name $pgService -ErrorAction SilentlyContinue) {
    Write-Host "$pgService : already installed, left alone" -ForegroundColor DarkGray
} elseif ($PSCmdlet.ShouldProcess($pgService, "register postgres service")) {
    & (Join-Path $proj ".local\pgsql\bin\pg_ctl.exe") register `
        -N $pgService -D (Join-Path $proj ".local\pgdata") -S auto
    if ($LASTEXITCODE -ne 0) {
        throw "pg_ctl register failed (exit $LASTEXITCODE)"
    }
    Write-Host "$pgService : registered" -ForegroundColor Green
}

foreach ($svc in $services) {
    $name = $svc.Name

    if (Get-Service -Name $name -ErrorAction SilentlyContinue) {
        Write-Host "$name : already installed, left alone" -ForegroundColor DarkGray
        continue
    }

    if (-not $PSCmdlet.ShouldProcess($name, "install service")) { continue }

    Invoke-Nssm @("install", $name, $svc.Exe, $svc.Args)

    # AppDirectory matters more than it looks: the worker resolves .env,
    # alembic.ini and ARTIFACT_DIR relative to its working directory, and a
    # service with the wrong one starts cleanly and then behaves as though the
    # configuration is empty.
    Invoke-Nssm @("set", $name, "AppDirectory", $proj)

    if ($svc.Type) {
        Invoke-Nssm @("set", $name, "Type", $svc.Type)
    }
    if ($svc.Env) {
        # Per-service, not `setx /M`: the Service Control Manager keeps the
        # environment block it read at boot, so a new machine-wide variable is
        # invisible to services until the next restart. This is applied at
        # service start, so it works immediately.
        Invoke-Nssm @("set", $name, "AppEnvironmentExtra", $svc.Env)
    }
    if ($svc.DependsOn) {
        # Order matters on boot: the workers need the broker.
        Invoke-Nssm (@("set", $name, "DependOnService") + $svc.DependsOn)
    }

    Write-Host "$name : installed" -ForegroundColor Green
}

if ($NoStart) {
    Write-Host "`nInstalled but not started (-NoStart)." -ForegroundColor Yellow
    exit 0
}

Write-Host ""
# Postgres first, and by name: every other service declares a dependency on
# it, and a dependency that is not yet Running is refused, not waited for.
foreach ($name in @($pgService) + $services.Name) {
    if (-not $PSCmdlet.ShouldProcess($name, "start service")) { continue }
    try {
        Start-Service -Name $name -ErrorAction Stop
        Write-Host "$name : started" -ForegroundColor Green
    } catch {
        Write-Host "$name : FAILED to start - $($_.Exception.Message)" -ForegroundColor Red
    }
}

Write-Host @"

Now prove it. Log OUT of RDP entirely, wait five minutes, log back in and run:

  .local\pgsql\bin\psql.exe -h 127.0.0.1 -U hotelmonitor_app -d hotelmonitor -c "select max(started_at) from check_runs;"

If that timestamp advanced while you were logged out, this is a deployment.
If it did not, the services are installed but not working, and the logs are in
.local\ and in Event Viewer under each service name.
"@
