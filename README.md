# Office Attendance — Backend (Phase 1)

Roster-driven attendance with **WiFi (public-IP) check-in and a GPS fallback**. The **server
decides everything** (off-day / before-shift / after-shift / late / wifi vs gps), so rules
change without shipping a new client.

FastAPI · SQLAlchemy 2.0 async · asyncpg · Alembic · APScheduler · Neon Postgres.

---

## Two things that will silently break everything

1. **`TRUST_PROXY=true` in production.** uvicorn sits behind Nginx/Caddy, so with
   `TRUST_PROXY=false` `get_client_ip()` returns the *proxy's* IP for every user — everyone
   appears to be on the same network and IP matching becomes meaningless.
   Verify **from your phone, not the VM**:
   ```bash
   curl -s https://api.ipify.org                  # your real public IP
   curl -s https://<api-domain>/api/debug/my-ip   # must be the SAME
   ```

   **The proxy must not let the client dictate its own IP.** nginx's
   `$proxy_add_x_forwarded_for` *preserves* whatever the client sent and appends the real IP,
   so the **leftmost** `X-Forwarded-For` entry is attacker-controlled — a spoofed
   `X-Forwarded-For: <office-ip>` would check someone in via the WiFi path from anywhere.
   Two defences, both required:

   ```nginx
   proxy_set_header X-Real-IP        $remote_addr;   # unforgeable: the TCP source
   proxy_set_header X-Forwarded-For  $remote_addr;   # OVERWRITE, do not append
   ```

   and `get_client_ip()` trusts, in order: `X-Real-IP` → **rightmost** `X-Forwarded-For` →
   `request.client.host`. It never trusts the leftmost entry. There is exactly one trusted
   proxy and uvicorn binds `127.0.0.1`, so there is no legitimate upstream chain to preserve.

   Confirm nothing bypasses the proxy: `ss -ltnp | grep :8000` must show `127.0.0.1:8000`
   (not `0.0.0.0`), and port 8000 must be closed in the cloud security group / firewall.
   Otherwise an attacker connects directly and `request.client.host` becomes their IP.

2. **The API must serve HTTPS.** `navigator.geolocation` is blocked outside a secure
   context, and an HTTPS frontend refuses a plain-HTTP API (mixed content). No TLS ⇒ no GPS
   fallback at all.

---

## Run locally

```bash
python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt                     # add -r requirements-dev.txt for tests
cp .env.example .env                                # fill DATABASE_URL / ADMIN_* / JWT_SECRET

alembic upgrade head                                # creates the schema
cd .. && python -m backend.seed                     # admin + 1 office + 3 rosters + 3 employees
cd backend && uvicorn app.main:app --reload --port 8000
```

Seeded employees use password `test1234` (`aisha@` active/Morning, `bilal@` active/Late,
`sana@` **pending** — approve her to exercise the approval flow).
The seeded office has **placeholder coordinates**: set the real lat/long in
**Admin → More → Offices**, and add the office public IP there too.

`python -m backend.seed` is idempotent — safe to re-run.

---

## The scheduler runs as its own process

Two jobs: **idle-checkout** (every 15 min) and **nightly resolution** (23:55 local).

```bash
RUN_SCHEDULER=true python -m app.scheduler_main
```

**Exactly one** of these should run across the deployment. API workers must have
`RUN_SCHEDULER` unset/false, otherwise every worker fires the jobs.

### If it is started twice anyway — three layers of safety

1. **One process** (systemd single unit + `RUN_SCHEDULER` gate).

2. **`pg_try_advisory_xact_lock`**, taken as the first statement of the job's transaction.
   The second scheduler gets `false` and skips. It is **transaction-scoped**, so it releases
   on COMMIT/ROLLBACK — there is no `pg_advisory_unlock` that could leak, even on crash.
   This works through Neon's **pooled** endpoint (a transaction pins its server backend, so
   xact-scoped advisory locks are safe there; a *session*-scoped lock would not be). No
   direct/non-pooled connection string is required.

3. **Every write is a compare-and-set**, so even simultaneous execution converges:
   - closing a day: `UPDATE … WHERE id = :id AND check_out IS NULL` — the loser matches 0
     rows and does not double-count. No lost update.
   - flagging: `INSERT … ON CONFLICT (user_id, date) DO UPDATE … WHERE check_in IS NULL`
     — `UNIQUE(user_id, date)` prevents duplicates; the guard prevents clobbering a row that
     has since acquired a `check_in`.
   - off-day: `INSERT … ON CONFLICT DO NOTHING`.

   Verified: two simultaneous nightly runs → no exception, one row per (user, date), status
   deterministic. Two simultaneous idle-checkouts on the same open day → exactly one reports
   closing it, `check_out` written once.

> **APScheduler needs an always-on host.** Free/sleeping tiers (e.g. Render free) will never
> fire these jobs.

## Deploy (systemd)

The VM previously ran a bare `nohup uvicorn …`, which does **not** survive a reboot.

```bash
sudo cp deploy/attendance-api.service deploy/attendance-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now attendance-api attendance-scheduler
sudo journalctl -u attendance-api -f
```

Update after a code change:
```bash
git pull
source venv/bin/activate && pip install -r requirements.txt
alembic upgrade head          # REQUIRED whenever migrations changed
sudo systemctl restart attendance-api attendance-scheduler
```

---

## Environment

See [.env.example](.env.example). The ones that matter most:

| Var | Meaning |
|---|---|
| `TRUST_PROXY` | **true in prod.** Read the client IP from `X-Forwarded-For`. |
| `DEV_MODE` | Mounts `/api/debug/*`; allows private IPs as office IPs. **Never true in prod.** |
| `RUN_SCHEDULER` | Only the dedicated scheduler process sets this true. |
| `MAX_GPS_ACCURACY_M` (100) | Reject GPS fixes worse than this. |
| `GPS_REVERIFY_MINUTES` (15) | How often a GPS day re-proves it is still in radius. |
| `CHECKOUT_IDLE_MINUTES` (60) | Idle threshold for auto check-out. |
| `DISPLAY_TIMEZONE` | Job scheduling tz (`DISPLAY_TZ` also accepted). Per-office tz lives on `offices`. |

---

## How check-in works

`POST /api/attendance/checkin-attempt` (auth + `X-Device-Token`, no body). Guard order:

1. No assigned roster → `no_roster`
2. Not a working day → `off_day` (writes an `off_day` attendance row)
3. `now < start_time` → `before_shift` (+ `minutes_remaining`)
4. `now > end_time` → `after_shift`
5. Already checked in → `already_checked_in`
6. Client IP ∈ any active `office.public_ips` → **checked in**, `method=wifi`,
   `verification=verified`; `late` if `now > start + grace`
7. Otherwise → `need_location` (no check-in, **no error**)

`POST /api/attendance/verify-location` `{latitude, longitude, accuracy}` **re-runs steps
2–5** (never trust client timing), then: `accuracy > MAX_GPS_ACCURACY_M` → `low_accuracy`;
Haversine to the nearest office ≤ `radius_meters` → **checked in**, `method=gps`,
`verification=gps_pending`; else `outside_radius` (+ distance).

Every attempt — success or not — appends a row to `checkin_attempts`.

**GPS is never auto-trusted.** A browser cannot detect a mock location
(`isFromMockProvider()` belongs to the Capacitor phase), so GPS check-ins stay
`gps_pending` and are highlighted for admin Approve/Query.

**Check-out is never a button.** The idle job closes anyone whose `last_seen` is stale, at
`min(last_seen, shift_end)`. On a GPS day `last_seen` only advances on a **fresh in-radius
fix** — which is what stops "go home and leave the tab open" from crediting unverified time.

**Nobody is ever auto-marked `absent`.** A missed day becomes `flagged`; only an admin sets
`absent` or `leave`.

Concurrency: check-in is a single atomic upsert guarded by `check_in IS NULL`, so a
double-tap yields exactly one `checked_in` + one `already_checked_in`, and one row.

Timezone: `attendance_days.date` is always the **office-local** date, never the UTC date.

Presence detection sits behind a `PresenceSource` seam (`app/presence.py`): `wifi` and `gps`
are two implementations producing the same `PresenceEvent`. A future MikroTik/UniFi/RADIUS
agent can `POST /api/agent/presence` and build the same event — attendance logic won't change.

---

## Manual test walkthrough

Needs `DEV_MODE=true`. `POST /api/debug/set-time {"iso": "...+00:00"}` moves the clock for
**both** the API and the scheduler (it is stored in `app_state`, not process memory);
`{"iso": null}` clears it. Times below are office-local (PKT = UTC+5).

Setup: Admin → More → Offices → set lat/long and **Add my current public IP**.

| # | Do this | Expect |
|---|---|---|
| 1 | set-time to a **Saturday** 11:00, then `checkin-attempt` | `off_day` |
| 2 | set-time Monday 09:00 | `before_shift` + `minutes_remaining` |
| 3 | Monday 10:05, on the office IP | `checked_in`, `wifi`, `verified`, `present` |
| 4 | Tap again | `already_checked_in` |
| 5 | Clear the day; Monday 10:30 | `late`, `late_minutes = 30` |
| 6 | Off the office IP | `need_location` (no error) |
| 7 | `verify-location` at office coords | `checked_in`, `gps`, `gps_pending` |
| 8 | `verify-location` ~1 km away | `outside_radius` + distance |
| 9 | `verify-location` with `accuracy: 150` | `low_accuracy` |
| 10 | Heartbeat in-radius, then out-of-radius, then `POST /api/debug/run-idle-checkout` | `check_out` = last **verified** `last_seen`, and `check_out > check_in` |
| 11 | Working day, no check-in, `POST /api/debug/run-nightly` | `flagged` — **never** `absent` |
| 12 | set-time Monday 20:00 | `after_shift` |

Timezone rule: set-time `2026-07-14T21:30:00+00:00` (= 02:30 PKT on Jul 15) — any row
created carries date **2026-07-15**, not `07-14`.

`/api/debug/run-nightly` runs the job **body** in the API process; it does not exercise
APScheduler's cron trigger. To test the scheduler itself, start
`attendance-scheduler.service` and watch `journalctl -u attendance-scheduler -f`.

---

## API surface

| Method | Path | Who |
|---|---|---|
| POST | `/api/auth/signup` · `/login` · `/refresh` | public |
| GET | `/api/auth/me` | any |
| GET | `/api/rosters` | **public** (signup form needs it before login) |
| POST | `/api/attendance/checkin-attempt` · `/verify-location` · `/heartbeat` | employee + device |
| GET | `/api/attendance/history` | employee |
| GET/POST/PATCH/DELETE | `/api/rosters*` | admin |
| GET/POST/PATCH/DELETE | `/api/admin/offices*` (+ `/current-ip`, `/add-current-ip`) | admin |
| GET | `/api/admin/employees` · `/dashboard` · `/flagged-days` | admin |
| POST | `/api/admin/users/{id}/decision` | admin |
| POST | `/api/admin/attendance/{id}/verify` | admin |
| POST | `/api/admin/flagged-days/{id}/resolve` | admin |
| GET | `/api/admin/reports` · `/reports/export` (CSV) | admin |
| GET/POST | `/api/debug/*` | DEV_MODE only |

Interactive docs at `/docs`.

## Out of scope in Phase 1

- **Background auto check-in.** A closed PWA cannot detect a WiFi change or wake itself.
  Real zero-touch needs a native shell (Capacitor foreground service) — Phase 1.5.
- **Mock-GPS detection** (native-only) — hence `gps_pending`.
- **Overnight shifts.** Rosters enforce `end_time > start_time`.
- **Leave requests.** Admin sets `leave` when resolving a flagged day; there is no
  `leaves` table yet.
