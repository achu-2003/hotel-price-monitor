# Hotel Price Monitor

Monitors the room prices of ~20–30 hotels near Yelagiri, Tamil Nadu, detects
real price changes, and notifies the person assigned to each hotel.

Python throughout: FastAPI (API + dashboard), Celery (scheduling), Playwright
(price collection), PostgreSQL (history), Redis (queue + locks).

---

## The one idea that shapes everything

A price is **not** "Hotel X costs ₹2,500". It is an **offer**:

```
offer_key = sha256(hotel | source | room_type | check_in | check_out
                   | adults | children | meal_plan | refundable | currency)
```

Two prices are comparable **only** when their `offer_key` matches. Because that
key is the primary key of `price_series`, comparing a 2-guest weekend rate
against a 3-guest weekday rate is *structurally impossible* rather than merely
discouraged. See `app/services/offer_key.py`.

---

## Quick start

```bash
python scripts/bootstrap_env.py     # generates .env with real random secrets
docker compose up --build
```

| Service | URL |
|---|---|
| Dashboard / API | http://localhost:8000 |
| API docs | http://localhost:8000/docs |
| Flower (task monitor) | http://localhost:5555 |
| Mailhog (catches dev email) | http://localhost:8025 |

Run the tests:

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python -m pytest
```

---

## Phase 0 first: find out what is actually collectable

**Do this before any adapter is written.** It tells you which of your 30 hotels
can be automated, and at what quality.

```bash
python scripts/probe_site.py https://somehotel.example/booking
python scripts/probe_site.py --file hotels.txt      # one URL per line
```

For each site it reports:

1. Whether `robots.txt` permits us (a "no" ends it — there is no workaround)
2. Whether a bot wall or CAPTCHA is present (also ends it)
3. **Whether the site exposes a JSON availability endpoint** — the best
   possible outcome, because JSON survives redesigns and CSS selectors do not
4. What the prices and room names look like in the DOM
5. A screenshot and an HTML fixture for the test suite

Results are written to `docs/SOURCES.md` as a table you can hand to whoever
reviews Terms of Service.

---

## How prices are collected

Source priority, highest quality first:

| Priority | Source | Gives you |
|---|---|---|
| 1 | Your own PMS / channel manager | Your rates. Never scrape yourself. |
| 2 | **Competitor's own booking site** | Real room names, meal plans, refundability. The primary target. |
| 3 | OTA listing page | Fallback only, after ToS review. Noisier and less stable. |
| 4 | Manual entry in the dashboard | Anything no adapter can cover. |

### Boundaries, enforced in code

- `robots.txt` is checked before every fetch (`app/adapters/robots.py`)
- A CAPTCHA or bot wall raises `BlockedError`, opens the circuit, and stops.
  There is no evasion path in this codebase, by design.
- Per-source rate limiting (default 6 req/min) plus dispatch jitter
- Requests identify themselves honestly, with a contact URL in the User-Agent
- A source cannot be fetched until a named human has recorded a ToS review
  (`sources.tos_reviewed_at`)

---

## How a change is decided

`app/services/comparison.py`, a pure state machine with no I/O:

1. **First sighting** → record it, tell nobody. Adding a hotel must not spam.
2. **Sold out** → its own event type. *Never* "price dropped to ₹0".
3. **Below threshold** → recorded, not alerted. Controlled by
   `DEFAULT_MIN_DELTA_ABS` and `DEFAULT_MIN_DELTA_PCT`, which a move must clear
   *both* of. **Shipped at `0`/`0`: every confirmed move is reported.**
   The code defaults (₹50 and 2%) are the conservative choice and are still
   what `app/config.py` falls back to with no env set.

   Raising them costs more than it looks: because the baseline only resets on a
   move that clears the rule, a drop that misses it leaves the *next* check
   comparing against a price the hotel already stopped charging. That is how a
   real ₹32.50 (2.8%) drop went unreported — it cleared the percentage and
   missed the money.
4. **Above threshold** → must persist across N consecutive checks
   (`DEFAULT_CONFIRM_CHECKS`, shipped at 2) before it counts. Dynamic pricing
   and A/B tests produce one-off blips; without this the alerts become noise
   and get ignored, which is the real failure mode. With the thresholds at
   zero this is the *only* thing standing between a ₹1 flicker and somebody's
   inbox, so it is load-bearing — see `tests/unit/test_comparison.py`.

---

## Layout

```
app/
  main.py            FastAPI factory: routers, RFC7807 errors, security headers
  config.py          settings from env / Docker secrets — no secret defaults
  core/              logging (auto-redacting), errors, crypto, security, ratelimit
  db/models/         18 tables; price_observations is monthly-partitioned
  services/          offer_key, comparison, dates, room_matching  ← pure logic
                     ingest (adapter output → history), monitoring (dispatch)
  adapters/          the only place that knows about websites
  notifications/     digest + quiet hours, pluggable email / WhatsApp providers
  workers/           Celery app, beat schedule, fetch / notify / maintenance tasks
  api/v1/            auth, hotels, sources, targets, prices, notifications, ops
  dashboard/         Jinja2 routes; templates/ and static/ alongside
scripts/
  bootstrap_env.py   generate .env with real secrets
  probe_site.py      Phase 0 site reconnaissance
```

`app/services/`, `app/adapters/parsing.py`, `app/adapters/mapping.py` and
`app/notifications/{digest,render}.py` are pure functions — no database, no
clock, no network — which is why they carry the bulk of the test suite.

---

## Security notes

- **No secret has a default.** The app refuses to start on a `CHANGE_ME`
  placeholder or a short key.
- **Credentials are envelope-encrypted** (`app/core/crypto.py`): a per-secret
  data key, wrapped by a KEK held only in the environment. A database dump
  alone is useless.
- **Redaction is enforced, not remembered** — a structlog processor scrubs
  `password|token|secret|cookie|api_key|...` recursively from every log line,
  every Sentry event, and every stored raw payload.
- **Back up `CREDENTIAL_KEK` separately from your database backups.** Losing it
  makes stored source credentials permanently unreadable; storing it alongside
  the database defeats the point of encrypting them.

---

## Status

| Area | State |
|---|---|
| Config, logging, redaction, crypto, security primitives | done, tested |
| Database schema + initial migration | done, SQL validated offline |
| Price identity, comparison, dates, room matching, parsing | done |
| Adapter contract, robots.txt, Playwright base | done |
| Phase 0 probe tool | done |
| **Adapters**: direct site, OTA, HTTP JSON, manual entry + registry | done |
| **Celery**: app, beat, dispatcher, fetch, notify, maintenance tasks | done |
| **Ingest pipeline**: observations, series, debounce, change rows | done |
| **Notifications**: email (SMTP/Resend), WhatsApp, digest, quiet hours | done |
| **API**: 69 routes — auth, CRUD, prices, ops, webhooks | done |
| **Dashboard**: overview, matrix, hotels, targets, changes, health, unmatched, notifications | done |
| Test suite | 257 unit tests passing, 13 integration tests awaiting a live Postgres |

**Not yet done — and each needs a real environment, not more code:**

1. `alembic upgrade head` has never run against a live PostgreSQL. Validate on
   first deploy.
2. No adapter has been pointed at a real hotel. Run
   `scripts/probe_site.py` (Phase 0) first — it tells you which of your thirty
   hotels are automatable and produces the `adapter_config` for each.
3. The WhatsApp template has not been submitted to Meta. Approval takes hours
   to days, so **submit it on the first day you want WhatsApp**; email works
   immediately and needs no approval.

### Running it

```bash
python scripts/bootstrap_env.py     # once — generates .env with real secrets
docker compose up --build
docker compose run --rm api python scripts/create_admin.py
```

Creating the administrator is a deliberate one-off step rather than something
the API does at start-up: two replicas booting together would race to create
the same user, and a "create an admin if none exists" path in a running web
app is a privilege-escalation hole waiting for the day someone truncates the
table.

Then, in order:

1. Sign in at http://localhost:8000 with `ADMIN_EMAIL` / `ADMIN_PASSWORD`. You
   are forced to change it immediately — that password is also sitting in your
   `.env` file.
2. Create a **source** and record its ToS review — nothing is ever fetched
   from a source with no review on file.
3. Add a **hotel**, attach the source with its `adapter_config`, add its
   **room types**.
4. Add a **monitor target** (which stay window, how often) and press
   **Run now** to see it work.
5. Whatever the site called a room that you had not mapped appears under
   **Unmatched** — map it once and it resolves forever after.
6. Add a **recipient**, assign them to the hotel, and use **Send test** to
   prove the channel before a real price move depends on it.

### Operating notes

- **Everything about a source lives in the database.** A site redesign is an
  `adapter_config` edit on the hotel-source row; the next scheduled check picks
  it up. No deploy.
- **Nothing is fetched without a recorded ToS review.** The dispatcher's query
  filters on `sources.tos_reviewed_at`, so this is structural rather than
  procedural.
- **A failure never spreads.** Each hotel's fetch is its own task with its own
  try/except; a failure writes a `monitoring_errors` row and returns normally.
- **Watch the Health tab's "Gone quiet" section, not just its errors.** A
  target that stopped checking without failing is the expensive failure — the
  dashboard keeps showing yesterday's prices as though they were current.
