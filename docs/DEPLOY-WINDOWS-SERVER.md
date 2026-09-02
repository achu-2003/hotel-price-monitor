# Deploying to a Windows Server

Target: a Windows Server box reached over RDP, running the stack in **WSL2 +
Docker Compose**, with the dashboard published on a public domain over HTTPS,
carrying the **existing data** across from `D:\CodeBase\hotel-price-monitor`.

Why WSL2 rather than installing Python, Postgres and Redis natively on Windows:

- `gunicorn`, the production API server in `docker/Dockerfile.api`, does not run
  on Windows at all.
- Celery's prefork pool needs `fork()`. Native Windows forces `--pool=solo`,
  which is one task at a time — a 40-second browser fetch blocks every alert
  queued behind it. Thirty hotels on 30-minute intervals does not fit.
- Redis has no supported native Windows build.
- The browser worker needs a specific set of Chromium system libraries that
  `mcr.microsoft.com/playwright/python` already carries.

Inside WSL2 the containers are ordinary Linux, so this is the same code path
that already works, and `docker-compose.yml` is the deployment description.

---

## 0. Check the server can actually run WSL2 — do this first

WSL2 is a Hyper-V virtual machine. If the Windows Server is itself a VM, the
host must allow **nested virtualization**, and not every provider does.

```powershell
systeminfo | Select-String "OS Name","OS Version","Total Physical Memory"
Get-ComputerInfo -Property "HyperVRequirementVirtualizationFirmwareEnabled","HyperVisorPresent"
```

| Requirement | Needed | Why |
|---|---|---|
| Windows Server | **2022 or 2025** | WSL2 is not available on 2019 |
| Nested virtualization | enabled | WSL2 will not start without it |
| RAM | **8 GB minimum, 16 GB comfortable** | 3 Chromium x ~400 MB, plus Postgres, Redis, API and WSL overhead |
| vCPU | 4 | 2 API workers + 3 browser workers |
| Disk | 60 GB free | images ~2.5 GB, Postgres, artifacts, backups |

Known blockers by provider:

- **Azure**: nested virtualization works on v3 sizes and newer. Fine.
- **AWS EC2**: nested virtualization is **not** supported on ordinary instance
  types — only `.metal`. A normal Windows EC2 instance cannot run WSL2.
- **Hyper-V / VMware on-prem**: enable nested virtualization on the VM first.

> If nested virtualization is unavailable, stop here — the native-Windows path
> is the fallback, with the concurrency limits listed above.

---

## 1. Install WSL2 and Ubuntu

In an **elevated** PowerShell on the server:

```powershell
wsl --install -d Ubuntu-24.04
```

Reboot when asked. After the reboot Ubuntu opens and asks for a UNIX username
and password — these are local to WSL and unrelated to the Windows account.

Confirm it is version 2, not 1:

```powershell
wsl --status
wsl --list --verbose      # VERSION column must read 2
```

### Cap what WSL is allowed to take

Without this, WSL2 grows to half the server's RAM and never gives it back.
Create `C:\Users\<you>\.wslconfig`:

```ini
[wsl2]
memory=10GB
processors=4
swap=4GB
kernelCommandLine=vm.max_map_count=262144
```

Then `wsl --shutdown` and reopen Ubuntu for it to take effect.

---

## 2. Install Docker Engine inside Ubuntu

Docker **Engine**, not Docker Desktop: Desktop needs a paid subscription for
companies over the free-tier threshold, wants a logged-in desktop session, and
adds nothing here.

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
                        docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
```

Log out of the Ubuntu shell and back in, then:

```bash
docker --version
docker compose version
docker run --rm hello-world
```

### Make Docker start with WSL

Ubuntu 24.04 under WSL runs systemd, so enable the service properly rather
than starting the daemon by hand:

```bash
sudo systemctl enable --now docker
```

Confirm `/etc/wsl.conf` contains:

```ini
[boot]
systemd=true
```

---

## 3. Make the whole thing survive a reboot and a logoff

This is the single biggest difference from your desktop. **Nothing started
from an RDP session survives you logging off** — WSL shuts down with the last
user session, and the price checks stop silently.

Two things are needed:

1. Every service in `docker-compose.yml` already has `restart: unless-stopped`,
   so containers come back when the Docker daemon does.
2. WSL itself must be started at boot, without anyone logging in. Register a
   Task Scheduler job running as **SYSTEM**, trigger **At startup**:

```powershell
$action  = New-ScheduledTaskAction -Execute "C:\Windows\System32\wsl.exe" `
                                   -Argument "-d Ubuntu-24.04 -u root /bin/true"
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest
Register-ScheduledTask -TaskName "Start-WSL-HotelMonitor" `
    -Action $action -Trigger $trigger -Principal $principal
```

Verify by rebooting the server, **not logging in**, and checking the dashboard
from your own machine. A deploy that only works while someone is logged in is
the failure this whole step exists to prevent.

---

## 4. Put the code on the server

Clone inside the WSL filesystem (`~/`), **not** under `/mnt/c/`. Docker builds
against a Windows path through the 9p translation layer are slow enough to
matter and lose file permissions.

```bash
cd ~
git clone <your repo url> hotel-price-monitor
cd hotel-price-monitor
git checkout main
```

---

## 5. Carry the secrets across — the irreversible step

`.env` on the old machine holds `CREDENTIAL_KEK`, the key that wraps every
stored source credential. **Do not run `scripts/bootstrap_env.py` on the
server.** It generates a fresh KEK, and every credential in the migrated
database becomes permanently unreadable.

On your Windows machine, from the project directory:

```powershell
Copy-Item .env "\\wsl.localhost\Ubuntu-24.04\home\<user>\hotel-price-monitor\.env"
```

Back up `CREDENTIAL_KEK` somewhere that is **not** your database backups —
stored beside the data it protects, it protects nothing.

### Edit these values for production

| Key | Set to | Why |
|---|---|---|
| `APP_ENV` | `production` | currently `development` |
| `SMTP_HOST` | your real provider | dev pointed at mailhog / gmail |
| `HEARTBEAT_URL` | a healthchecks.io ping URL | the only alarm that still fires when beat is what died |
| `SENTRY_DSN` | your DSN | optional, but this is where you stop watching logs by hand |
| `ARTIFACT_DIR` | leave `/data/artifacts` | correct inside the containers; it was only wrong for the native Windows run |

`POSTGRES_HOST` and `REDIS_HOST` need no edit — `docker-compose.yml` overrides
them to the compose service names.

---

## 6. Migrate the database

**Version mismatch, read this first.** Your local Postgres is **17.2**;
`docker-compose.yml` runs `postgres:16-alpine`. A 17 dump restored into a 16
server is not reliable. Pin the server to 17 so the versions match:

```yaml
image: postgres:17-alpine
```

The `backup` service uses the same image on purpose, so pg_dump's version
always matches the server's — change both or neither.

### Dump on the old machine

Stop the old stack first (`.\scripts\dev-stop.ps1` — then restart Postgres
alone, or dump before stopping) so nothing writes during the dump.

```powershell
$env:PGPASSWORD = (Select-String -Path .env -Pattern '^POSTGRES_PASSWORD=(.*)$').Matches.Groups[1].Value
.\.local\pgsql\bin\pg_dump.exe -h 127.0.0.1 -U hotelmonitor_app -d hotelmonitor `
    -Fc --no-owner --no-privileges -f hotelmonitor.dump
```

### Restore on the server

```bash
# Bring up only the database, and load into it BEFORE migrate runs.
docker compose -f docker-compose.yml up -d postgres
docker compose -f docker-compose.yml cp ../hotelmonitor.dump postgres:/tmp/
docker compose -f docker-compose.yml exec postgres \
  pg_restore -U hotelmonitor_app -d hotelmonitor --no-owner --clean --if-exists /tmp/hotelmonitor.dump
```

Then confirm the schema is at the revision the code expects:

```bash
docker compose -f docker-compose.yml run --rm migrate alembic current
docker compose -f docker-compose.yml run --rm migrate alembic upgrade head
```

`alembic upgrade head` on an already-current database is a no-op, which is why
it is safe to run either way.

---

## 7. Start the stack

`docker-compose.override.yml` is **development** — bind-mounted source,
`--reload`, one browser worker, DEBUG logging. Compose merges it automatically,
so a bare `docker compose up` on the server gets the wrong thing. Always name
the files explicitly:

```bash
docker compose -f docker-compose.yml up -d --build
docker compose -f docker-compose.yml ps
```

First build pulls the Playwright image and installs Chromium — 5-15 minutes.

```bash
curl http://localhost:8000/api/v1/health/ready
```

`database` and `redis` must both report healthy. Create the administrator only
if this is a fresh database — if you restored a dump, your users came with it:

```bash
docker compose -f docker-compose.yml run --rm api python scripts/create_admin.py
```

---

## 8. Publish it on a domain over HTTPS

Three separate hops have to line up, and each is a place this commonly
half-works.

### 8a. TLS terminator

There is no `docker-compose.prod.yml` in this repo yet, although several
comments reference one. It needs to exist before the public path is complete:
it should drop `mailhog`, drop the `8000:8000` host binding, and add a Caddy
service on 80/443 that terminates TLS and proxies to `api:8000`. Caddy gets a
Let's Encrypt certificate automatically from the domain name alone.

Until it exists, do **not** open port 8000 to the internet — the app expects
to sit behind a proxy.

### 8b. WSL2 to Windows port forwarding

WSL2 has its own network. A container publishing `:443` inside WSL is not
reachable from outside the server, and `localhostForwarding` only covers
`localhost` from the Windows side.

- **Windows Server 2025**: use mirrored networking, which removes the problem
  entirely. In `.wslconfig`:

  ```ini
  [wsl2]
  networkingMode=mirrored
  ```

- **Windows Server 2022**: no mirrored mode — forward explicitly:

  ```powershell
  $ip = (wsl -d Ubuntu-24.04 hostname -I).Trim().Split()[0]
  netsh interface portproxy add v4tov4 listenport=80  listenaddress=0.0.0.0 connectport=80  connectaddress=$ip
  netsh interface portproxy add v4tov4 listenport=443 listenaddress=0.0.0.0 connectport=443 connectaddress=$ip
  ```

  **The WSL IP changes on every reboot**, so this must be re-run at startup —
  fold it into the Task Scheduler job from step 3, resetting the old rules
  first with `netsh interface portproxy reset`.

### 8c. Firewall and DNS

```powershell
New-NetFirewallRule -DisplayName "HTTP"  -Direction Inbound -Protocol TCP -LocalPort 80  -Action Allow
New-NetFirewallRule -DisplayName "HTTPS" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow
```

Also open 80/443 in the cloud provider's security group — the Windows firewall
is only the second of two gates. Point an A record at the server's public IP
and wait for it to resolve before starting Caddy, or the first certificate
request fails.

Never open 5432 (Postgres), 6379 (Redis) or 5555 (Flower). Flower has no
authentication of its own; reach it through a tunnel.

---

## 9. Before you call it done

- [ ] Reboot the server, do **not** log in, confirm the dashboard answers.
- [ ] `docker compose -f docker-compose.yml logs -f beat` shows dispatches.
- [ ] The heartbeat check at healthchecks.io has gone green.
- [ ] `./backups` is filling — the `backup` service bind-mounts a host path,
      so verify the directory exists in WSL and a dump lands there nightly.
- [ ] `CREDENTIAL_KEK` is backed up somewhere separate from those dumps.
- [ ] Windows Update is not set to reboot at a time that lands mid-check.
- [ ] Admin password changed from whatever `.env` says.

---

## Daily operation

```bash
cd ~/hotel-price-monitor
docker compose -f docker-compose.yml ps
docker compose -f docker-compose.yml logs -f worker-browser
docker compose -f docker-compose.yml restart worker-browser   # after an adapter change
git pull && docker compose -f docker-compose.yml up -d --build
```

Everything from section 3 onwards of `docs/RUNBOOK.md` — creating sources,
recording the ToS review, configuring hotels and alerts — applies unchanged.
