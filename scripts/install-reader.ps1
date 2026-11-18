# Start the reader (scripts\remote_reader.py) every time this user signs in.
#
#     .\scripts\install-reader.ps1             # register and start it now
#     .\scripts\install-reader.ps1 -Remove     # stop it and take it out again
#
# A scheduled task "at log on", for the signed-in user, with no admin rights.
# Not a Windows service on purpose: Goibibo is read in a real Chrome WINDOW,
# and a service has no desktop to open one on. So the PC has to be switched
# on AND signed in; set Windows to sign in automatically if it must survive a
# power cut by itself.
#
# Before running: put REMOTE_READER_URL and REMOTE_READER_KEY in .env here.
# Output goes to .local\reader.log.
param([switch]$Remove)

$ErrorActionPreference = "Stop"
$name = "HotelMonitor-Reader"
$proj = Split-Path -Parent $PSScriptRoot
$python = Join-Path $proj ".venv312\Scripts\python.exe"
$log = Join-Path $proj ".local\reader.log"

$existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "removed the old '$name' task" -ForegroundColor DarkGray
}
if ($Remove) { return }

if (-not (Test-Path $python)) { throw "No Python at $python - create the .venv312 environment first." }
New-Item -ItemType Directory -Force (Split-Path -Parent $log) | Out-Null

# Through cmd so the output can be appended to a file: a scheduled task has
# no console of its own to read it from.
$run = "/c `"`"$python`" -u `"$proj\scripts\remote_reader.py`" >> `"$log`" 2>&1`""
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $run -WorkingDirectory $proj
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
# Restarted if it ever exits, never stopped for running long, and not held
# back on a laptop that is on battery.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description "Reads Goibibo from this PC for the hotel price monitor server." | Out-Null
Start-ScheduledTask -TaskName $name

Write-Host "reader    : registered as '$name' and started" -ForegroundColor Green
Write-Host "            it starts by itself at every sign-in"
Write-Host "  Log   : $log"
Write-Host "  Remove: .\scripts\install-reader.ps1 -Remove"
