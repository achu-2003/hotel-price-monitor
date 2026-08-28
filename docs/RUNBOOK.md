# Runbook — first run and daily operation

Written for a Windows 11 machine with the repository at
`D:\CodeBase\hotel-price-monitor`.

> **This machine runs natively, not in Docker.** Postgres, Redis, Python 3.12
> and Chromium are already installed and working. To start it:
>
> ```powershell
> .\scripts\dev-start.ps1
> ```
>
> Jump to [Running without Docker](#running-without-docker-this-machines-setup)
> for that setup. Sections 0-2 below describe the Docker path, which is what
> production uses; sections 3-8 (creating an admin, configuring a hotel,
> alerts) apply to both.

---

## 0. Install Docker Desktop (one time)

Docker is not currently installed on this machine, and it is the only real
prerequisite — Postgres, Redis, Chromium and the Python environment all live
inside the containers.

1. Download Docker Desktop for Windows:
   <https://www.docker.com/products/docker-desktop/>
2. Install it, accepting the **WSL 2** backend when prompted.
3. Reboot if asked, then start Docker Desktop and wait for the whale icon in
   the system tray to stop animating.
4. Confirm:

   ```powershell
   docker --version
   docker compose version
   ```

Both must print a version. If `docker` is "not recognised", Docker Desktop is
not running or its CLI was not added to `PATH` — restart the app.

> **Why Docker rather than installing things directly?** Redis has no
> supported native Windows build, and the browser worker needs a specific set
> of Chromium system libraries. Both are solved problems inside the images.

---

## 1. Check your `.env` — do not regenerate it

`.env` **already exists** and already contains real generated secrets.

```powershell
Select-String -Path .env -Pattern "^ADMIN_USERNAME=|^ADMIN_PASSWORD="
```

Note both values — they are your first login.

⚠️ **Do not re-run `scripts/bootstrap_env.py`.** It rewrites every secret,
including `CREDENTIAL_KEK`. Regenerating that key makes any stored source
credential permanently unreadable. Only run it on a machine with no `.env` yet.

Back up `CREDENTIAL_KEK` somewhere **other than** your database backups. Stored
alongside the database, it defeats the point of encrypting anything.

---

## 2. Start the stack

```powershell
docker compose up --build
```

First run pulls images and installs Playwright's Chromium — expect **5–15
minutes**. Later runs take seconds.

Startup order is enforced by the compose file: Postgres and Redis become
healthy, then `migrate` runs `alembic upgrade head` once and exits, and only
then do the API, workers and beat start. The API can therefore never come up
against an out-of-date schema.

**This is the first time the migration has ever run against a live
PostgreSQL.** Watch for it in the log:

```
migrate-1  | INFO  [alembic.runtime.migration] Running upgrade  -> 0001, initial schema
migrate-1 exited with code 0
```

`exited with code 0` is success. Anything else — stop and read the error
before continuing.

Leave this window running. It is your log stream. To run it in the background
instead, use `docker compose up --build -d` and follow with
`docker compose logs -f`.

### Check it came up

In a second PowerShell window:

```powershell
docker compose ps
curl http://localhost:8000/health
```

Every service should be `running` (`migrate` will show `exited (0)`, which is
correct). Health returns `{"status":"ok",...}`.

| Service | URL |
|---|---|
| Dashboard | http://localhost:8000 |
| API docs | http://localhost:8000/docs |
| Flower (task monitor) | http://localhost:5555 |
| Mailhog (catches all dev email) | http://localhost:8025 |

---

## 3. Create your administrator

```powershell
docker compose run --rm api python scripts/create_admin.py
```

It reads `ADMIN_USERNAME` / `ADMIN_PASSWORD` from `.env` and prints
`Created administrator ...`.

This is a deliberate one-off step rather than something the API does at
start-up: two API replicas booting together would race to create the same
user, and a "create an admin if none exists" path in a running web app is a
privilege-escalation hole waiting for the day someone truncates the table.

---

## 4. Sign in

Open <http://localhost:8000>, log in with the email and password from `.env`.

You are redirected straight to **Change password** and cannot navigate
anywhere else until you do. That password is also sitting in a plaintext file
on disk, which is exactly why the change is forced.

You should now see the Overview screen with every counter at zero. That is
correct — nothing has been configured yet.

---

## 5. Configure the first hotel

Nothing is collected until a source, a hotel and a monitor target exist. Do
these in order; each step depends on the one before it.

### 5a. Create a source, and record its ToS review

A *source* is a place prices can be read from — one hotel's booking engine, an
OTA, or manual entry.

```powershell
$body = @'
{"code":"acme-direct","display_name":"Acme Resort booking engine",
 "adapter_key":"playwright_direct_site","base_domain":"book.acmeresort.example",
 "rate_limit_per_min":6}
'@
curl -X POST http://localhost:8000/api/v1/sources -H "Content-Type: application/json" -b cookies.txt -d $body
```

Easier: use the interactive docs at <http://localhost:8000/docs> — you are
already logged in there via the session cookie, so `Try it out` just works.

A source is created **disabled**. Enabling it requires a recorded Terms of
Service review:

```
POST /api/v1/sources/{id}/tos-review
{"reviewed_by": "Your Name", "approve": true, "notes": "Checked 2026-08-18"}
```

The dispatcher's query filters on `sources.tos_reviewed_at`, so a source with
no review on file is *structurally* unfetchable — not merely discouraged. If a
review comes back negative, record it with `"approve": false`: that documents
the decision so nobody proposes the same source again in six months.

### 5b. Add the hotel and attach the source

- `POST /api/v1/hotels` — `{"name": "Acme Resort", "location": "Yelagiri"}`
- `POST /api/v1/hotels/{id}/sources` — attach the source, with the booking URL
  and the `adapter_config` (see 5d).

### 5c. Add room types

`POST /api/v1/hotels/{id}/rooms` — `{"name": "Deluxe Room", "capacity": 2}`

Add only the rooms you already know about. **You do not need to guess.**
Anything the site lists that you have not mapped shows up under **Unmatched**,
where you map it once and it resolves forever after. The system never guesses
a room mapping: a wrong one corrupts a price series silently and indefinitely,
while an unmatched row is a visible gap that gets fixed.

### 5d. Where `adapter_config` comes from — run Phase 0 first

**Do this before writing any adapter config.** The probe tells you which of
your ~30 hotels are automatable at all, and at what quality:

```powershell
docker compose run --rm worker-browser python scripts/probe_site.py https://book.acmeresort.example/rooms
```

For each site it reports whether `robots.txt` permits us, whether a bot wall is
present, **whether the site exposes a JSON availability endpoint** (the best
outcome — JSON survives redesigns, CSS selectors do not), and what the prices
and room names look like in the DOM. Results go to `docs/SOURCES.md`.

A minimal DOM config, put on the hotel-source row:

```json
{
  "url_template": "https://book.acmeresort.example/rooms?checkin={check_in}&checkout={check_out}&adults={adults}",
  "room_card": ".room-result",
  "selectors": {
    "room_name": ".room-result__title",
    "price": ".room-result__price",
    "meal_plan": ".room-result__board"
  },
  "sold_out_markers": ["No rooms available"]
}
```

If the probe found a JSON endpoint, prefer that shape instead — see the
docstring at the top of `app/adapters/http_json.py`.

`adapter_config` lives in the database precisely so a site redesign at 5 PM is
a config edit that the next scheduled check picks up, not a code change and a
deploy.

### 5e. Create a monitor target

```
POST /api/v1/monitor-targets
{"hotel_source_id": 1, "date_strategy": "rolling",
 "lead_time_days": 7, "length_of_stay_nights": 1,
 "adults": 2, "interval_minutes": 30}
```

A target says *which hotel, on which source, for which stay, how often*. It
deliberately does not name rooms — the site decides what its rooms are called,
and they are discovered by the fetch.

---

## 6. Prove it works

On the hotel's page (or **Targets**), press **Run now**.

The button returns immediately with a run id and polls for the result — a
browser fetch takes 20–40 seconds, so an HTTP request must never block on one.
Expect one of:

| Result | Meaning |
|---|---|
| `N offers, M changes` | Working. First run reports 0 changes by design. |
| `N offers, 0 changes, K unmatched` | Working; map the rooms under **Unmatched**. |
| `failed` | Open **Health** → the error carries a screenshot and the HTML. |
| `skipped — already running` | Normal. A previous run of the same hotel and stay window still held the lock. |

**The first run never alerts anyone.** With no previous price there is no
baseline, so nothing can have "changed" — otherwise adding a hotel would spam
whoever is assigned to it.

A confirmed change also needs to clear the significance threshold (₹50 *and*
2% by default) and then persist across 2 consecutive checks. That debounce is
what keeps the alerts worth reading.

---

## 7. Set up alerts

1. `POST /api/v1/recipients` — name plus an email and/or an E.164 phone
   (`+919876543210`).
2. `POST /api/v1/hotels/{id}/recipients` — assign them, with channels and
   their own sensitivity thresholds.
3. On **Notifications**, press **Send test**. It calls the provider
   synchronously and shows you the real verdict, because the point is to see
   the failure now rather than discover it at the first real price move.

In development, mail goes to Mailhog — open <http://localhost:8025> to read it.
Nothing reaches a real inbox until `EMAIL_PROVIDER`/`SMTP_HOST` are pointed at
a real relay.

WhatsApp needs a Meta-approved *utility* template and stays off until
`WHATSAPP_ENABLED=true`. Approval takes hours to days — submit it on day one
of wanting WhatsApp. Email needs no approval and works immediately.

---

## 8. Leaving it running

Once a target exists, Celery Beat fires `dispatch_due_checks` every 60 seconds
and everything proceeds on its own. Useful commands:

```powershell
docker compose logs -f beat worker-browser   # watch the scheduler and fetches
docker compose ps                            # what is up
docker compose restart worker-browser        # after changing browser settings
docker compose down                          # stop (data survives in volumes)
docker compose down -v                       # stop AND delete all data
```

`docker compose down -v` destroys the price history. There is no undo — but
see **Backups** below, which is what makes that sentence survivable.

### Backups

The `backup` service dumps the database to `./backups` every night at 02:30
UTC, and once at start-up so a fresh deployment is covered immediately. Seven
daily dumps are kept, plus four Sunday dumps on a weekly ladder.

Each dump is read back with `pg_restore --list` before it replaces yesterday's,
so a dump truncated by a restart or a full disk is discarded rather than kept
under a valid-looking name.

```powershell
docker compose logs backup            # did last night's run succeed?
ls backups/daily                      # what is on hand
```

**These live on this machine.** They survive `docker compose down -v`, a bad
migration, and a wrong `DELETE`. They do not survive the machine dying. Copy
`./backups` somewhere else on a schedule if the history matters more than the
hardware — and keep `CREDENTIAL_KEK` somewhere different again, or a stolen
backup arrives with its own keys.

### Proving a backup would actually restore

An untested backup is a guess. Run this monthly:

```bash
./scripts/restore_db.sh --verify
```

It restores the newest dump into a throwaway database, prints the row counts
and the newest observation timestamp, and drops it again. The timestamp is the
number that matters: it is how much history a real restore would give back.

To recover for real:

```bash
./scripts/restore_db.sh --into hotelmonitor_recovered backups/daily/<file>.dump
```

Restoring over the live database needs `--force-into`, spelled differently on
purpose.

### What to watch

Open the **Health** tab and look at **"Gone quiet"** before the error list. A
target that stopped checking *without failing* produces no errors at all — the
dashboard keeps showing yesterday's prices as though they were current. That
silent case is the one that actually costs money; an erroring target is
already visible.

You should not have to remember to look. Two things push instead:

**Ops alerts.** Tick *"Tell them when monitoring breaks"* on somebody's row on
the **Alerts** page. `maintenance.alert_on_silence` then emails them when a
target has missed three consecutive intervals, naming the hotel and how long it
has been quiet. At most one message per person per day for the same set of
stale targets — a new target going quiet does interrupt again. With nobody
ticked, this system cannot tell you it has stopped working.

**The dead-man's switch.** Set `HEARTBEAT_URL` to a ping URL from
healthchecks.io or similar. Beat pings it every five minutes, but only after
touching the database — a process that is alive with a dead database must not
report itself healthy. The watchdog alerts when the pings stop, which is the
only alarm that still works when this box is the thing that failed.

### A hotel that is full is not a hotel that is broken

A check whose page says "No available rooms on the selected dates" is a
**success**, and shows on the run log as a grey `sold out` pill rather than
`0 offers`. Hover it for the sentence: which night was full, and which night
was priced instead.

Because a last-minute window that comes back full also gets checked one night
later — tonight rolls to tomorrow, tomorrow rolls to the night after, nothing
further out moves. The prices found there are recorded against **that** night,
never against the night that was asked for. Set `SOLD_OUT_ROLLOVER_DAYS=0` to
turn the extra read off.

If a run like this shows as `failed` with `parse_schema_drift` instead, the
page is announcing its unavailability in wording the marker list has not seen.
Add the phrase to `_SOLD_OUT_MARKERS` in `app/adapters/parsing.py` — one line,
and every hotel on that engine benefits — rather than to one hotel's
`sold_out_markers`. Keep it a phrase that *announces* unavailability: a marker
that can appear in ordinary interface copy will report a hotel with rooms for
sale as sold out, which is much the worse mistake.

---

## Development workflow

`docker-compose.override.yml` is merged automatically by `docker compose up`,
and it is what makes the stack usable while developing. It bind-mounts your
source over the copy baked into the image and runs the API under
`uvicorn --reload`.

The base file bakes the source in on purpose — a deployed image should be a
complete, immutable artifact. That is exactly wrong while developing, where
every one-line edit would need a rebuild first.

### The daily loop

```powershell
docker compose up              # start everything; leave it running
```

| You changed | What to do |
|---|---|
| Anything under `app/api/`, `app/dashboard/`, `app/templates/`, `app/schemas/` | **Nothing.** Save the file; uvicorn has already reloaded. |
| A task, adapter, or `app/services/` file used by a worker | `docker compose restart worker-browser` (or `worker-http` / `worker-notify` / `beat`) |
| `app/static/*.css` or `*.js` | Hard-refresh the browser — Ctrl+F5 |
| `requirements.txt` | `docker compose up --build` |
| A model, or anything under `alembic/` | `docker compose run --rm migrate alembic upgrade head` |

Celery deliberately does not hot-reload: restarting a worker mid-fetch would
abandon a live browser session. The restart is a second, not a rebuild,
because the source is mounted.

### Running the tests

The unit suite needs no services at all and runs natively on Windows, which
makes it far faster than going through a container:

```powershell
.venv\Scripts\python.exe -m pytest
```

257 tests, about five seconds. This is the tight loop — use it for anything in
`app/services/`, `app/adapters/mapping.py`, `app/adapters/parsing.py` or
`app/notifications/`, all of which are pure functions with no database.

The integration suite needs the containers running. Postgres creates a
separate `hotelmonitor_test` database on first boot precisely so a test run
cannot destroy the prices you have been collecting:

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://hotelmonitor_app:PASSWORD@localhost:5432/hotelmonitor_test"
.venv\Scripts\python.exe -m pytest -m integration
```

Use the `POSTGRES_PASSWORD` from `.env`. If Postgres was first started before
the init script existed, create the database by hand once:

```powershell
docker compose exec postgres psql -U hotelmonitor_app -d hotelmonitor -c "CREATE DATABASE hotelmonitor_test OWNER hotelmonitor_app;"
```

### Watching what happens

```powershell
docker compose logs -f api                 # requests
docker compose logs -f beat worker-browser # the scheduler and the fetches
```

`LOG_LEVEL=DEBUG` is set for every service in the override file, and logs are
rendered human-readably rather than as JSON whenever `APP_ENV=development`.

Flower at <http://localhost:5555> shows queued, running and failed tasks —
the fastest way to see whether a fetch was picked up at all.

### Poking at the database

```powershell
docker compose exec postgres psql -U hotelmonitor_app -d hotelmonitor
```

Useful once prices exist:

```sql
SELECT h.name, r.name, s.last_price, s.is_available, s.last_checked_at
FROM price_series s
JOIN hotels h ON h.id = s.hotel_id
JOIN room_types r ON r.id = s.room_type_id
ORDER BY s.last_checked_at DESC LIMIT 20;
```

### One browser, not three

The override sets the browser worker to concurrency 1. Three Chromiums at
~400MB each is what makes an editor and your own browser feel slow on the same
machine. Production uses 3.

---

## Troubleshooting

**`docker: command not found`** — Docker Desktop is not installed or not
running. See step 0.

**`migrate` exits non-zero** — read its log:
`docker compose logs migrate`. Nothing else will have started, so the database
is in a known state; fix the error and re-run `docker compose up`.

**`ports are not available: 8000`** — something else holds the port. Find it
with `netstat -ano | findstr :8000`, or change the mapping in
`docker-compose.yml`.

**Dashboard loads but every count is zero** — expected until a source, hotel
and target exist and one check has succeeded. Work through step 5.

**A target shows `open` / paused** — five consecutive failures pause it for an
hour; a robots.txt refusal or a bot wall pauses it immediately. Fix the cause,
then press **Resume** on the Health tab, which also clears the failure counter.

**Everything unmatched** — the adapter is reading room names the alias table
does not know. That is the designed behaviour, not a bug. Map them once under
**Unmatched**.

**A fetch raises `SchemaDriftError`** — the page loaded but did not contain
what the adapter expected, almost always a redesign. The error row carries a
screenshot and the saved HTML; update `adapter_config` on the hotel-source
row. No price is ever written from a guess.

**A fetch raises `BlockedError` or `RobotsDisallowedError`** — the site has
said no. There is no evasion path in this codebase by design. The circuit
opens and the source needs a human decision, which may be "move this hotel to
manual entry".

---

## Running without Docker (this machine's setup)

Already done and working. Postgres and Redis run as **portable, user-level
processes** out of `.local\` — no Windows service, no admin rights, nothing in
the registry. Deleting `.local\` and `.venv312\` returns the machine to how it
was.

### Every day

```powershell
.\scripts\dev-start.ps1     # postgres, redis, api, worker, beat
.\scripts\dev-stop.ps1      # stops all of it, keeps your data
```

`dev-start` is safe to re-run: it skips anything already listening, and ends by
calling `/health/ready` so it reports whether the API can genuinely reach
Postgres and Redis rather than just claiming it started something.

### What is where

| | |
|---|---|
| PostgreSQL 17.2 | `.local\pgsql\` (binaries), `.local\pgdata\` (your data) |
| Redis 5.0.14 | `.local
edis\`, data in `.local
edis-data\` |
| Python 3.12.10 + deps | `.venv312\` |
| Chromium 131 | `%LOCALAPPDATA%\ms-playwright\` |
| Logs | `.localpi.err.log`, `worker.err.log`, `beat.err.log`, `pg.log` |

Python 3.12 specifically: `requirements.txt` cannot install on 3.14, which is
what the system Python is. `rapidfuzz==3.11.0` has no 3.14 wheel and fails to
build from source, and `psycopg-binary==3.2.3` has no 3.14 wheel at all.

### Reloading after a change

The API runs under `uvicorn --reload`, so routes, templates and schemas take
effect on save. Celery does not reload — after changing a task, an adapter, or
anything under `app\services\`, restart the workers:

```powershell
.\scripts\dev-stop.ps1 ; .\scripts\dev-start.ps1
```

### The one real limitation

Celery runs with `--pool=solo` because its default prefork pool needs `fork()`,
which Windows does not have. One task at a time: a 30-second browser fetch
blocks the notify queue behind it. Fine for developing against one or two
hotels, not enough for thirty on 30-minute intervals. Production on Linux uses
prefork with real concurrency, which is the same code path either way.

### Talking to the database directly

```powershell
$env:PGPASSWORD = (Select-String -Path .env -Pattern '^POSTGRES_PASSWORD=(.*)$').Matches.Groups[1].Value
.\.local\pgsqlin\psql.exe -h 127.0.0.1 -U hotelmonitor_app -d hotelmonitor
```

### Integration tests

The cluster already has a second database, `hotelmonitor_test`, so a test run
cannot destroy collected prices:

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://hotelmonitor_app:PASSWORD@127.0.0.1:5432/hotelmonitor_test"
.venv312\Scripts\python.exe -m pytest -m integration
```

### Not available natively

Mailhog is not installed, so `SMTP_HOST` still points at `mailhog` and test
emails will fail to connect. Either leave it (notifications are a later phase)
or set `EMAIL_PROVIDER=resend` with a real API key.

---

## If you later want Docker instead

Possible but not recommended on Windows, and unnecessary for the pure-logic
test suite, which needs no services at all:

```powershell
.venv\Scripts\python.exe -m pytest        # 257 tests, no database required
```

For the full stack you would need PostgreSQL 16 installed natively, a Redis
substitute (Memurai, or Redis inside WSL2 — there is no supported native
Windows build), `pip install -r requirements.txt`, `playwright install
chromium`, and `POSTGRES_HOST`/`REDIS_HOST` changed from the Docker service
names to `localhost`. Then run `alembic upgrade head`, `uvicorn app.main:app`,
and each Celery worker and beat in its own terminal. Docker Desktop is less
work.
