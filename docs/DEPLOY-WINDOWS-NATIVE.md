# Running natively on a Windows Server

No Docker, no WSL, no nested virtualization. Postgres, Redis, Python and
Chromium all run as ordinary processes out of `.local\` and `.venv312\`, the
same way this project already runs on the development machine — see *Running
without Docker* in `RUNBOOK.md`.

Written for someone who has RDP'd into a fresh Windows Server, installed Git,
and cloned the repository. Every step is here, in order.

## Know this before you start

**Celery cannot use its prefork pool here.** Prefork needs `fork()`, which
Windows does not have, so every worker runs `--pool=solo` — one task at a time,
in-process.

`dev-start.ps1` runs a **single** solo worker serving `browser,http,notify`
together. On a development machine that is fine. On a server it means a
30-second browser fetch sits in front of every price alert waiting to go out,
and at twenty hotels that is thirteen minutes of a WhatsApp message being late.

**The fix is not the pool, it is the process count.** `--pool=solo` limits one
*process* to one task; nothing stops you running several processes against the
same Redis broker. Step 11 runs four workers instead of one:

| Service | Queue | Why |
|---|---|---|
| `Worker-Browser-1` | `browser` | a page load, ~20-40s |
| `Worker-Browser-2` | `browser` | the second one, so two hotels fetch at once |
| `Worker-Notify` | `notify` | **never waits behind a browser** — this is the one that sends the WhatsApp message |
| `Worker-Http` | `http` | dispatch sweeps and light work |

Two browser workers give twenty hotels roughly seven minutes of fetching per
30-minute cycle, and alerts go out immediately because nothing shares their
queue. That is the concurrency the Linux deployment gets from prefork, built
out of separate processes instead.

**One thing is genuinely lost**: `--max-tasks-per-child`, which only exists for
prefork and is what stops Chromium leaking a process per recycle. Step 11
substitutes a nightly restart, which is safe here — `celery_app.py` sets
`task_acks_late=True`, so a task interrupted by a restart is redelivered rather
than dropped.

**Nothing here survives you logging off until step 11.** Everything
`dev-start.ps1` launches is a plain user process tied to your RDP session.
Step 11 turns them into Windows services. Do not skip it and call this
deployed.

---

## Step 1 — Install Python 3.12

**3.12 specifically.** Not 3.13, not 3.14. `requirements.txt` pins
`rapidfuzz==3.11.0`, which has no wheel above 3.12 and fails to build from
source, and `psycopg-binary==3.2.3` has no 3.14 wheel at all.

Download from <https://www.python.org/downloads/release/python-31210/> —
"Windows installer (64-bit)".

In the installer:

- tick **Add python.exe to PATH**
- **Install Now** is fine

Verify:

```powershell
py -3.12 --version        # Python 3.12.10
```

If `py` is not recognised, use the full path to the interpreter in step 2
instead: `C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe`.

---

## Step 2 — Create the virtual environment and install dependencies

From the repository root. **The folder must be named `.venv312`** —
`dev-start.ps1` looks for `.venv312\Scripts` by that exact name.

```powershell
cd C:\path\to\hotel-price-monitor
py -3.12 -m venv .venv312
.\.venv312\Scripts\python.exe -m pip install --upgrade pip
.\.venv312\Scripts\python.exe -m pip install -r requirements.txt
```

Ten to fifteen minutes. Verify:

```powershell
.\.venv312\Scripts\python.exe -c "import fastapi, celery, playwright; print('ok')"
```

---

## Step 3 — Install Chromium for Playwright

```powershell
.\.venv312\Scripts\python.exe -m playwright install chromium
```

~150 MB into `%LOCALAPPDATA%\ms-playwright\`. On Windows it needs no extra
system libraries — that is a Linux-only concern.

```powershell
.\.venv312\Scripts\python.exe -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(); print('chromium ok'); b.close(); p.stop()"
```

---

## Step 4 — PostgreSQL 17, portable

Not the installer. The **binaries zip** — it needs no admin rights, registers
no service, and keeps everything inside the project.

1. Go to <https://www.enterprisedb.com/download-postgresql-binaries>
2. Download **Version 17.x → Windows x86-64**
3. The zip contains a `pgsql\` folder. Extract it so you end up with exactly:

```
<project>\.local\pgsql\bin\pg_ctl.exe
```

`dev-start.ps1` looks for that path literally.

### Initialise the cluster

Pick a strong password and keep it — it goes into `.env` in step 6.

```powershell
cd C:\path\to\hotel-price-monitor
New-Item -ItemType Directory -Force .local | Out-Null

# initdb will not take a password on the command line, so hand it a file.
$pw = "PUT_YOUR_LONG_RANDOM_PASSWORD_HERE"
Set-Content -Path .local\pw.txt -Value $pw -Encoding ascii -NoNewline

.\.local\pgsql\bin\initdb.exe -D .local\pgdata -U hotelmonitor_app `
    --pwfile=.local\pw.txt -E UTF8 --auth=scram-sha-256

Remove-Item .local\pw.txt
```

The cluster's superuser is `hotelmonitor_app`, which is the user the app
connects as. That avoids a second role to create and grant.

### Start it and create the databases

The `-o` value must stay **one quoted string**. `dev-start.ps1` carries a
comment about this: split into separate tokens, `pg_ctl` reads the port number
as its operation and fails with `unrecognized operation mode "5432"`.

```powershell
.\.local\pgsql\bin\pg_ctl.exe -D .local\pgdata -l .local\pg.log `
    -o "-p 5432 -c listen_addresses=127.0.0.1" start

$env:PGPASSWORD = $pw
.\.local\pgsql\bin\createdb.exe -h 127.0.0.1 -U hotelmonitor_app hotelmonitor
.\.local\pgsql\bin\createdb.exe -h 127.0.0.1 -U hotelmonitor_app hotelmonitor_test
```

`hotelmonitor_test` exists so an integration test run cannot destroy collected
prices. Verify:

```powershell
.\.local\pgsql\bin\psql.exe -h 127.0.0.1 -U hotelmonitor_app -d hotelmonitor -c "\l"
```

---

## Step 5 — Redis

There is no official Redis for Windows. The build this project uses is the
community Windows port, and it is what `.local\redis\` holds on the dev
machine: **Redis 5.0.14.1**.

1. <https://github.com/tporadowski/redis/releases>
2. Download `Redis-x64-5.0.14.1.zip`
3. Extract so you have:

```
<project>\.local\redis\redis-server.exe
```

### Write its config

`dev-start.ps1` passes `.local\redis.conf`, and **the `dir` line is an
absolute path** — the dev machine's copy points at `D:\CodeBase\...`, so it
cannot be copied across unchanged. Generate it with this server's own path:

```powershell
$proj = (Get-Location).Path
New-Item -ItemType Directory -Force .local\redis-data | Out-Null
@"
port 6379
bind 127.0.0.1 ::1
appendonly yes
dir $proj\.local\redis-data
maxmemory 512mb
maxmemory-policy noeviction
"@ | Set-Content -Path .local\redis.conf -Encoding ascii
```

`bind 127.0.0.1` is deliberate. Redis has no authentication here; it must not
be reachable from the network.

---

## Step 6 — The `.env` file

### If this is a fresh installation

```powershell
.\.venv312\Scripts\python.exe scripts\bootstrap_env.py
```

It copies `.env.example` and replaces every `CHANGE_ME` with a real random
value. It refuses to overwrite an existing `.env`.

### If you are migrating an existing database — read this first

`bootstrap_env.py` generates a **new** `CREDENTIAL_KEK`. That key wraps every
stored source credential, so a fresh KEK against a restored database makes
every one of them permanently unreadable. There is no recovery.

Copy the `.env` from wherever the database you are restoring actually runs —
and note that if that is the Coolify deployment, the secrets live in
**Coolify's environment variables**, not in any `.env` file on the development
machine.

### Edit these four values

`.env.example` describes the Docker layout, where the database is reachable
at the hostname `postgres`. Nothing here is in Docker.

| Key | Set to | Why |
|---|---|---|
| `POSTGRES_HOST` | `127.0.0.1` | there is no `postgres` hostname |
| `REDIS_HOST` | `127.0.0.1` | there is no `redis` hostname |
| `ARTIFACT_DIR` | `C:\hotel-monitor\artifacts` | the default `/data/artifacts` is a POSIX path; on Windows it lands wherever the current drive takes it |
| `APP_ENV` | `production` | see the warning below |
| `POSTGRES_PASSWORD` | the password from step 4 | must match the cluster you just built |

```powershell
New-Item -ItemType Directory -Force C:\hotel-monitor\artifacts | Out-Null
```

> **`APP_ENV=production` requires HTTPS.** `app/api/v1/auth.py` sets the session
> cookie with `secure=settings.is_production`, so over plain HTTP the browser
> silently discards it: login returns 200, the next page bounces back to
> `/login`, and it reads as a wrong password. Until you have TLS in front of
> this (step 12), leave `APP_ENV=development` or you will not be able to log in.

---

## Step 7 — Create the schema

```powershell
.\.venv312\Scripts\python.exe -m alembic upgrade head
```

Verify:

```powershell
.\.venv312\Scripts\python.exe -m alembic current
```

If you are restoring a dump instead, restore it **before** running this, then
run `alembic upgrade head` anyway — it is a no-op on an already-current
database.

---

## Step 8 — Create the administrator

Only for a fresh database. A restored dump brings its users with it.

The script reads `ADMIN_USERNAME` and `ADMIN_PASSWORD` from the environment,
so no password is ever typed into a shell history:

```powershell
.\.venv312\Scripts\python.exe scripts\create_admin.py
```

It always forces a password change at first login. Every account after this
one is made with `scripts\create_account.py` — nothing in the API or the
dashboard can create a user.

---

## Step 9 — Start everything

```powershell
.\scripts\dev-start.ps1
```

It starts Postgres, Redis, uvicorn, the Celery worker (`--pool=solo`) and
beat, skipping anything already listening, and finishes by calling
`/health/ready`. Stop it all with `.\scripts\dev-stop.ps1`.

---

## Step 10 — Verify it is genuinely working

```powershell
# Both must report healthy -- this asks the API to actually reach them.
curl.exe http://127.0.0.1:8000/api/v1/health/ready

# The scheduler is the piece that fails silently.
Get-Content .local\beat.log -Tail 20
Get-Content .local\worker.err.log -Tail 20
```

Then open `http://127.0.0.1:8000/` in a browser **on the server** and log in.

A check should run within a few minutes. If nothing happens, beat is the first
place to look — an API that answers and a dashboard that renders prove nothing
about whether anything is being fetched.

---

## Step 11 — Make it survive logging off

**This is the step that turns a dev stack into a deployment.** Everything
above is tied to your RDP session; disconnect and the price checks stop, with
no error anywhere.

Install [NSSM](https://nssm.cc/download), extract `nssm.exe` to `C:\nssm\`,
then in an **elevated** PowerShell:

```powershell
$proj = "C:\path\to\hotel-price-monitor"
$py   = "$proj\.venv312\Scripts"

# PostgreSQL
C:\nssm\nssm.exe install HotelMonitor-Postgres "$proj\.local\pgsql\bin\pg_ctl.exe" `
    "-D `"$proj\.local\pgdata`" -l `"$proj\.local\pg.log`" start"
C:\nssm\nssm.exe set HotelMonitor-Postgres Type SERVICE_WIN32_OWN_PROCESS

# Redis
C:\nssm\nssm.exe install HotelMonitor-Redis "$proj\.local\redis\redis-server.exe" `
    "`"$proj\.local\redis.conf`""

# API
C:\nssm\nssm.exe install HotelMonitor-API "$py\uvicorn.exe" `
    "app.main:app --host 127.0.0.1 --port 8000"
C:\nssm\nssm.exe set HotelMonitor-API AppDirectory $proj

# Celery workers -- FOUR processes, not one. See "Know this before you start".
#
# -n gives each a unique node name. Without it they collide on the same name,
# and Celery's own control channel starts answering for the wrong process --
# which shows up as a worker that looks alive in `celery inspect` and is
# consuming nothing.
$w = "-A app.workers.celery_app worker --pool=solo --loglevel INFO"

C:\nssm\nssm.exe install HotelMonitor-Worker-Browser1 "$py\celery.exe" `
    "$w -Q browser -n browser1@%%h"
C:\nssm\nssm.exe install HotelMonitor-Worker-Browser2 "$py\celery.exe" `
    "$w -Q browser -n browser2@%%h"
C:\nssm\nssm.exe install HotelMonitor-Worker-Notify   "$py\celery.exe" `
    "$w -Q notify -n notify1@%%h"
C:\nssm\nssm.exe install HotelMonitor-Worker-Http     "$py\celery.exe" `
    "$w -Q http -n http1@%%h"

foreach ($s in "Browser1","Browser2","Notify","Http") {
    C:\nssm\nssm.exe set "HotelMonitor-Worker-$s" AppDirectory $proj
}

# Celery beat -- EXACTLY ONE, always.
#
# Beat is the scheduler, not a worker. A second one does not share the load,
# it duplicates it: every hotel is checked twice, every digest sent twice.
C:\nssm\nssm.exe install HotelMonitor-Beat "$py\celery.exe" `
    "-A app.workers.celery_app beat --loglevel INFO"
C:\nssm\nssm.exe set HotelMonitor-Beat AppDirectory $proj
```

Order matters on boot — the workers need the broker:

```powershell
foreach ($s in "Browser1","Browser2","Notify","Http") {
    C:\nssm\nssm.exe set "HotelMonitor-Worker-$s" DependOnService `
        HotelMonitor-Redis HotelMonitor-Postgres
}
C:\nssm\nssm.exe set HotelMonitor-Beat DependOnService HotelMonitor-Redis
C:\nssm\nssm.exe set HotelMonitor-API  DependOnService HotelMonitor-Postgres HotelMonitor-Redis
```

Start them, then **prove it**:

```powershell
Start-Service HotelMonitor-Postgres, HotelMonitor-Redis, HotelMonitor-API,
              HotelMonitor-Worker-Browser1, HotelMonitor-Worker-Browser2,
              HotelMonitor-Worker-Notify, HotelMonitor-Worker-Http,
              HotelMonitor-Beat
```

### Recycle the browser workers nightly

This stands in for `--max-tasks-per-child`, which prefork has and solo does
not. Without it, each Chromium that fails to shut down cleanly stays resident
until the server runs out of memory.

Safe to do while work is in flight: `celery_app.py` sets `task_acks_late=True`
and `task_reject_on_worker_lost=True`, so a fetch interrupted by the restart is
redelivered to the other worker rather than lost.

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument `
  "-Command Restart-Service HotelMonitor-Worker-Browser1,HotelMonitor-Worker-Browser2"
$trigger = New-ScheduledTaskTrigger -Daily -At 4am
Register-ScheduledTask -TaskName "HotelMonitor-Recycle-Browsers" `
    -Action $action -Trigger $trigger `
    -Principal (New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest)
```

Watch memory for the first week — `Get-Process chrome*, celery | Measure-Object
WorkingSet64 -Sum`. If it climbs across a day, move the recycle to twice daily.

> Reboot the server, **do not log in**, and check the dashboard from your own
> machine. A deployment that only works while somebody is logged in is exactly
> what this step exists to prevent, and the only way to know is to try it.

`AppDirectory` matters more than it looks: the worker resolves `.env`,
`alembic.ini` and `ARTIFACT_DIR` relative to its working directory, and a
service with the wrong one starts cleanly and then behaves as though the
configuration is empty.

---

## Step 12 — Reaching it from outside

The API binds `127.0.0.1` on purpose. Do **not** simply change that to
`0.0.0.0` and open port 8000: the application expects to sit behind a proxy
that terminates TLS, and `APP_ENV=production` will not issue a usable session
cookie over plain HTTP (step 6).

Put [Caddy](https://caddyserver.com/download) in front of it. A two-line
`Caddyfile` gets a Let's Encrypt certificate from the domain name alone:

```
monitor.yourdomain.com {
    reverse_proxy 127.0.0.1:8000
}
```

Install it as a service with `caddy.exe run --config Caddyfile` under NSSM the
same way, then:

```powershell
New-NetFirewallRule -DisplayName "HTTP"  -Direction Inbound -Protocol TCP -LocalPort 80  -Action Allow
New-NetFirewallRule -DisplayName "HTTPS" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow
```

Point the domain's A record at the server's public IP **before** starting
Caddy — it cannot get a certificate for a name that does not resolve to it.

Only once HTTPS answers should you set `APP_ENV=production` and restart the
API and worker services.

---

## Checklist

- [ ] `py -3.12 --version` reports 3.12
- [ ] `.venv312\` exists, not `.venv\`
- [ ] `.local\pgsql\bin\pg_ctl.exe` and `.local\redis\redis-server.exe` exist at those exact paths
- [ ] `.local\redis.conf` names **this** server's path, not `D:\CodeBase\...`
- [ ] `POSTGRES_HOST` and `REDIS_HOST` are `127.0.0.1`, not `postgres` / `redis`
- [ ] `CREDENTIAL_KEK` matches the database you restored, if you restored one
- [ ] `/api/v1/health/ready` reports database **and** redis healthy
- [ ] **Four** worker services running, each with its own `-n` node name
- [ ] Exactly **one** beat service — two means every hotel is checked twice
- [ ] A test price alert reached the configured WhatsApp number
- [ ] Rebooted, did not log in, dashboard still answers
- [ ] Nightly browser recycle registered, and memory watched for a week
- [ ] At least 8 GB RAM — two Chromiums at ~400 MB each, plus Postgres, Redis and the API
- [ ] `ARTIFACT_DIR` points somewhere that exists on a Windows drive
- [ ] Windows Update is not scheduled to reboot mid-check
