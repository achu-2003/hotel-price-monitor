# Publish this machine's API on a public HTTPS address, without a domain.
#
#     .\scripts\tunnel-start.ps1
#
# Downloads cloudflared if it is not here, opens a Cloudflare Quick Tunnel to
# 127.0.0.1:8000, waits for the address, and prints the one line to put in
# .env. Stop it with .\scripts\tunnel-stop.ps1
#
# WHY A TUNNEL AND NOT AN OPEN PORT
# =================================
# The API binds 127.0.0.1 on purpose and must keep doing so: it expects TLS to
# be terminated in front of it, and APP_ENV=production will not issue a usable
# session cookie over plain HTTP. cloudflared dials OUT to Cloudflare and
# forwards inbound requests to the loopback address, so nothing is opened, no
# firewall rule changes, and the app never learns it exists.
#
# WHAT THIS IS NOT
# ================
# It is not the production answer, for one reason that has nothing to do with
# security: THE HOSTNAME CHANGES EVERY TIME IT RESTARTS. A server reboot gives
# a new name, and every link already sent in a WhatsApp message goes dead. With
# a two-hourly summary that is a steady accumulation of dead links at a client.
#
# A second, smaller one: some resolvers refuse *.trycloudflare.com. Most open
# it fine -- this was tested on three handsets and two opened it untouched --
# but the one that did not needed Private DNS set to dns.google, which is not
# something a hotel client can be asked to do.
#
# The production answer is a domain plus either Caddy or a NAMED tunnel, which
# keeps its hostname across restarts. See docs/DEPLOY-WINDOWS-NATIVE.md step 12.
# This script is for getting the thing working and seen first.

$ErrorActionPreference = "Continue"
$proj = Split-Path -Parent $PSScriptRoot
$local = Join-Path $proj ".local"
$exe = Join-Path $local "cloudflared.exe"
$log = Join-Path $local "tunnel.err.log"

if (-not (Test-Path $local)) { New-Item -ItemType Directory -Force $local | Out-Null }

# -- the binary -------------------------------------------------------
if (-not (Test-Path $exe)) {
    Write-Host "cloudflared: downloading (about 55 MB)..." -ForegroundColor DarkGray
    $url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    try {
        Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
        Write-Host "cloudflared: downloaded" -ForegroundColor Green
    } catch {
        Write-Host "cloudflared: DOWNLOAD FAILED - $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "cloudflared: already here" -ForegroundColor DarkGray
}

# -- is the API even up? ----------------------------------------------
#
# Asked before the tunnel rather than after. A tunnel to a port nothing is
# listening on comes up perfectly happily and answers 502 to every visitor,
# which reads as the tunnel being broken when the API is the thing that is down.
$listening = $null -ne (Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue)
if (-not $listening) {
    Write-Host "api       : NOTHING IS LISTENING ON 8000." -ForegroundColor Red
    Write-Host "            Start it first (.\scripts\dev-start.ps1), or the" -ForegroundColor Red
    Write-Host "            tunnel will answer 502 to everyone." -ForegroundColor Red
    exit 1
}

if (Get-Process cloudflared -ErrorAction SilentlyContinue) {
    Write-Host "tunnel    : already running. Stop it first with .\scripts\tunnel-stop.ps1" -ForegroundColor Yellow
    exit 1
}

# -- open it ----------------------------------------------------------
Remove-Item $log -ErrorAction SilentlyContinue
Start-Process -FilePath $exe `
    -ArgumentList "tunnel","--url","http://127.0.0.1:8000" `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $local "tunnel.log") `
    -RedirectStandardError $log

# The address is not known until Cloudflare hands one back, so this polls the
# log rather than sleeping a flat guess -- the same reasoning as Wait-Port in
# dev-start.ps1, and it matters more here because the wait is a network round
# trip rather than a process start.
$deadline = (Get-Date).AddSeconds(60)
$url = $null
while ((Get-Date) -lt $deadline -and -not $url) {
    Start-Sleep -Milliseconds 1000
    $text = (Get-Content $log -ErrorAction SilentlyContinue) -join "`n"
    $match = [regex]::Match($text, 'https://[a-z0-9-]+\.trycloudflare\.com')
    if ($match.Success) { $url = $match.Value }
}

if (-not $url) {
    Write-Host "tunnel    : FAILED - no address after 60s. See $log" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "tunnel    : $url" -ForegroundColor Green
Write-Host ""
Write-Host "  Put this in .env, then restart the API, the workers AND beat:" -ForegroundColor White
Write-Host ""
Write-Host "      PUBLIC_BASE_URL=$url" -ForegroundColor Cyan
Write-Host ""
# Said every time, because it is the failure that looks like the feature not
# working: uvicorn reloads on its own and the workers do not, so the dashboard
# picks the new address up and the messages keep going out with the old one --
# or with no link at all.
Write-Host "  The workers read configuration at boot. Without restarting them" -ForegroundColor Yellow
Write-Host "  the alerts keep being built with the OLD address, or with none." -ForegroundColor Yellow
Write-Host ""
Write-Host "  Log:  $log"
Write-Host "  Stop: .\scripts\tunnel-stop.ps1"
