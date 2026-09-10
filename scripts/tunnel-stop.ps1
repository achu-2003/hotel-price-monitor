# Close the public tunnel opened by .\scripts\tunnel-start.ps1
#
#     .\scripts\tunnel-stop.ps1
#
# WORTH ACTUALLY RUNNING. While the tunnel is open this machine's whole API is
# on the public internet -- the dashboard login included, not only the read-only
# comparison page. It is fine for an afternoon of testing and it should not be
# left up because somebody forgot.
#
# Nothing else is affected. The API, the workers, beat, Postgres and Redis go on
# exactly as before; only reaching them from outside stops. Links already sent
# in WhatsApp messages stop opening, which is the reason a quick tunnel is a
# testing tool and not the production answer.

$ErrorActionPreference = "Continue"
$proj = Split-Path -Parent $PSScriptRoot

$procs = Get-Process cloudflared -ErrorAction SilentlyContinue
if ($procs) {
    $procs | Stop-Process -Force
    Write-Host "tunnel    : stopped ($($procs.Count) process(es))" -ForegroundColor Green
} else {
    Write-Host "tunnel    : not running" -ForegroundColor DarkGray
}

# The address is dead, so a stale PUBLIC_BASE_URL in .env now points nowhere.
# Said rather than edited: .env is the operator's file, and a script that
# rewrites it is a script that quietly reverts a production value somebody set
# on purpose.
$envFile = Join-Path $proj ".env"
if ((Test-Path $envFile) -and (Select-String -Path $envFile -Pattern '^PUBLIC_BASE_URL=.*trycloudflare' -Quiet)) {
    Write-Host ""
    Write-Host "  .env still has PUBLIC_BASE_URL pointing at the closed tunnel." -ForegroundColor Yellow
    Write-Host "  Clear it (PUBLIC_BASE_URL=) or the alerts will carry a dead link." -ForegroundColor Yellow
    Write-Host "  Empty is safe: no link is added at all." -ForegroundColor Yellow
    Write-Host "  Restart the workers afterwards -- they read it at boot." -ForegroundColor Yellow
}
