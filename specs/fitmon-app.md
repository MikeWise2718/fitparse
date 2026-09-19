# Fitmon — fitness monitoring app on top of python-fitparse

Successor to `specs/flask-ui.md` (the VO2-max-only v1, which is built and working).
Goal: surface **everything the FIT files and the fitparse library can give**, organised around
the question "how is my fitness developing?", not around file parsing.

Working name / app-id: **`fitmon`**. Python package stays `app/` (already tracked, no churn).

---

## 1. Where we are (findings, 2026-09-19)

### 1.1 What v1 does
Upload / directory-scan of `.fit` files → one `activities` row per file holding only
sport, start, duration and VO2 max min/max → dashboard, cycling + running list with a VO2 max
line chart, activity detail, delete, reparse, CSV export. Bootstrap 5 + Chart.js from CDN, no build step.

### 1.2 What is actually in the data
Surveyed all 111 files under `D:\fit` + `mike/` (pure-python parse, 64 s total, ~0.6 s/file):

| Finding | Detail |
|---|---|
| Span | 2022-07-27 → 2024-07-28, all `file_id.type = activity` (no monitoring / sleep / HRV files) |
| Devices | garmin_product 4312 (73 files, Epix Pro 42), 3943, 3589, 2158 |
| Sports | gravel cycling 33, running 28, lap swim 18, transition 16, strength 9, indoor cycling 9, open water 8, treadmill 7, cycling generic 5, cardio 1 |
| Multisport | 137 `session` rows in 108 files — triathlons hold several sessions per file |
| `record` | 311,531 rows: HR, speed, distance, GPS, altitude, cadence, **power**, L/R balance, torque effectiveness, pedal smoothness, **running dynamics** (vertical oscillation, stance time, step length, vertical ratio, stance balance), temperature, grade |
| `session` / `lap` | distance, timer/elapsed time, calories, avg/max HR, speed, **aerobic + anaerobic training effect**, ascent/descent, avg/max/normalized power, total work, power phase, bounding box |
| `length` (18 files) | per-pool-length stroke type, stroke count, cadence, speed → SWOLF |
| `set` (9 files) | strength sets: exercise category/subtype, reps, weight, duration |
| `user_profile` | weight, height, resting HR, activity class, sleep/wake time — a snapshot **per activity**, i.e. a free weight / RHR time series |
| `zones_target` | max HR, threshold HR, **FTP** — likewise a per-activity snapshot |
| `device_info` | every sensor seen (HRM, power meter, …) with firmware, battery status/voltage, cumulative operating time |
| `hr` (5 files) | beat-level HR from a strap during swims |
| `unknown_140` | field 7 × 3.5 / 65536 = VO2 max (source: intervals.icu forum thread linked in `fitparse.py`). 45 other populated fields, unmapped |
| Other unknowns | 22, 79, 104, 113, 125, 141, 147, 216, 233 (349k rows), 288, 312, 313, 324–327, 394 … — `fitparse/profile.py` is an old SDK; 216 is very likely `time_in_zone` in current SDKs |
| Developer fields | none in this archive (library supports them; explorer must still show them) |
| Garmin export zips | 14 `*.zip` in `D:\fit` root — import should accept zips |

### 1.3 Bugs / gaps found in v1
1. **`subsport` is always NULL** — parser reads `vals.get('subsport')`, the field is `sub_sport`. All 16 DB rows have NULL; the subsport filter can never match. (Same typo in the old `fitparse.py` CLI.)
2. **Triathlon file fails**: `2024-07-28-09-44-53.fit` (3 copies) → `FitParseError: Got data message with invalid local message type 14`. Already diagnosed in `D:\hw\epix2gen\CLAUDE.md`: the file is **genuinely corrupt** (an independent parser, `fitdecode`, dies at the same spot after ~34,681 frames), not a library bug. The totals survive. (Correction from the build, §8.1: not because `session` is written near the end - this Epix Pro multisport file writes its five sessions *first*; only the tail after the last sample is damaged, so the file is effectively complete.) → tolerant `partial` import; and the sync module (§6) can re-fetch a clean copy from Garmin Connect.
3. Activity bounds come from min/max `event` timestamp instead of `session.start_time` / `total_timer_time`.
4. Multisport files collapse to one row with the *last* `sport` message winning.
5. Duplicate detection is filename + date; should be content hash (and `serial_number` + `time_created`).
6. Top-level `fitparse.py` script shadows-by-name the `fitparse/` package (works only because packages win). Retire it once the explorer + CLI replace it.
7. No `pyproject.toml`; deps in `requirements.txt` only.

### 1.4 Divergence from the global Flask conventions
| Rule | v1 | Plan |
|---|---|---|
| Port reserved | binds **5000 = pokeflute's port**, unreserved | claim **8640** on **munchlax** (decided, §10) |
| Version in header | none | `app/__init__.py: __version__`, shown in `<h1>`, bump per commit |
| `GET /api/ping` | none | add |
| REST/JSON for every action | form POSTs + `flash()` + redirects | all actions → `/api/...`, JS renders |
| Runtime data outside repo | `uploads/`, `instance/` inside repo | `~/fitmon/{fit,data,logs}/` |
| `events.jsonl` | none | add `app/logging.py` event logger |
| Server-side settings | none | `~/fitmon/data/settings.json`, `GET/POST /api/settings` |
| Bind 0.0.0.0, debug reloader | 127.0.0.1 | fix in `run.py` |
| Auth | none | **multi-user with login** — friends and family will use it (§7). Remember-me device tokens, 90 days |
| uv / rich / rich-argparse / short flags | no | `pyproject.toml`; import CLI uses rich + rich-argparse |

---

## 2. Data model (SQLite, WAL, at `~/fitmon/data/fitmon.db`)

Original FIT files are kept (`~/fitmon/users/<user_id>/fit/<sha256[:2]>/<sha256>.fit`) — the DB is a **rebuildable
index**; `reparse all` must always be able to regenerate it after a schema change. So: no
migrations framework, just a `schema_version` and "drop + reparse".

**Every row belongs to a user** (§7). `files.user_id` is the ownership root — sessions, laps,
records, lengths, sets, devices, best efforts and zone time inherit it through `file_id`;
`profile_snapshots`, `garmin_activities` and `daily_health` carry `user_id` directly. This goes
into the schema in task 1.1, not later: retrofitting tenancy onto a populated single-user schema
means touching every query twice.

| Table | Grain | Key columns |
|---|---|---|
| `users`, `device_tokens`, `invites` | see §7.2 | |
| `files` | one FIT file | **user_id**, sha256 (unique **per user**), original name, size, device product/serial, `time_created`, utc_offset (from `activity.local_timestamp`), parse_status (`ok` / `partial` / `failed`), parse_error, message-type counts (JSON) |
| `sessions` | one sport leg (**the unit every view works on**) | file_id, index, sport, sub_sport, start, timer/elapsed time, distance, calories, avg/max HR, avg/max speed, ascent/descent, avg/max/normalized power, total work, aerobic TE, anaerobic TE, avg cadence, pool length, bbox, **vo2max**, derived: TRIMP / load score, IF, TSS (when FTP known) |
| `laps` | lap | session_id + the lap fields above |
| `records` | 1 Hz sample | session_id, t, hr, speed, distance, lat, lon, alt, cadence, power, lr_balance, temp, grade, running-dynamics cols, pedal cols. ~3k rows/activity, 311k today — trivial for SQLite. Index `(session_id, t)` |
| `lengths` | swim length | stroke, strokes, time, speed, swolf |
| `sets` | strength set | category, subtype, reps, weight, duration, set_type |
| `devices` | device seen in a file | file_id, device_index, manufacturer, product, serial, type, sw version, battery status/voltage, operating time |
| `profile_snapshots` | per file | weight, height, resting HR, max HR, threshold HR, FTP, activity class |
| `best_efforts` | per session | kind (`power` / `pace`), window (5 s, 1 min, 5 min, 20 min, 60 min / 400 m, 1 k, 5 k, 10 k, HM), value, offset |
| `zone_time` | per session | zone kind (hr/power), zone no., seconds — computed from records + the zones valid at that date |

Raw/unknown messages are **not** stored; the explorer (§4.7) reads the FIT file on demand
(0.6 s, LRU-cached).

Times: FIT timestamps are UTC. Store UTC, keep `utc_offset` per file, render local activity time.

---

## 3. Services (`app/services/`)

| Module | Responsibility |
|---|---|
| `fit_parser.py` | rewrite: one pass over `FitFile.get_messages()` → `ParsedFile` dataclass tree (sessions → laps/records/lengths/sets, devices, profile, vo2max). Records are assigned to sessions by time window. **Tolerant**: on `FitParseError` mid-file keep what was read, mark `partial`. |
| `importer.py` | hash → dedupe → store file → parse → write DB in one transaction → event log. Sources: upload, directory scan (recursive), **zip**, watch folder. |
| `metrics.py` | derived numbers: time-in-zone, best efforts (rolling max over records), NP/IF/TSS, TRIMP, SWOLF, aerobic decoupling (pace-or-power : HR, first vs second half), efficiency factor. |
| `fitness.py` | cross-activity series: daily load → CTL (42 d EWMA) / ATL (7 d) / TSB; weekly + monthly volume; VO2 max per sport; weight / RHR / FTP series. |
| `explorer.py` | library surface: message-type counts, message rows with name / value / raw_value / units / def_num, developer fields, header + CRC info, `StandardUnitsDataProcessor` toggle, `check_crc` toggle, JSON dump (same shape as `scripts/fitdump -t json`). |
| `settings.py`, `logging.py` | per conventions (pokeflute reference). |

---

## 4. UI — single page, tabbed, vanilla JS + Chart.js + Leaflet (CDN), no build step

Header: `Fitmon v{{ version }}` top-left. Global controls: date range, sport filter, units.

### 4.1 Dashboard — "am I getting fitter?"
- KPI tiles: current VO2 max (run / bike) with 90-day delta · CTL / ATL / TSB (fitness / fatigue / form) · this week vs. 4-week average (hours, km, load) · FTP · resting HR · weight.
- Fitness–fatigue chart (CTL/ATL/TSB over time).
- Weekly volume stacked by sport (hours; toggle distance / load / calories).
- Calendar heat-map of training days; recent activities list.

### 4.2 Activities
All sports in one sortable, filterable table (sport, sub-sport, date, distance, time, avg HR, avg power, TE, VO2 max, load). Multisport files shown as a parent row with its legs. Bulk reparse / delete / export.

### 4.3 Activity (session) detail
- Summary tiles from `session`; map (Leaflet polyline, coloured by HR / speed / power) when GPS exists.
- Synced time-series: HR, speed-or-pace, power, cadence, altitude, temperature (x-axis time or distance).
- Laps table; time-in-zone bars (HR, power); best efforts for this session.
- Sport panels, shown only when the data exists:
  - **Run**: vertical oscillation, vertical ratio, stance time + L/R balance, step length, cadence; decoupling.
  - **Bike**: NP / IF / TSS, L/R balance, torque effectiveness, pedal smoothness, power phase, power curve vs. all-time curve.
  - **Swim**: lengths table, stroke mix, SWOLF, pace / 100 m per length; strap HR from `hr` messages.
  - **Strength**: sets grouped by exercise — reps × weight, volume.
- Devices used (sensor, firmware, battery) · events timeline (timer start/stop, etc.) · "Open in explorer" · download original `.fit`.

### 4.4 Trends
VO2 max per sport (replaces v1 cycling/running pages, now with working sub-sport filter) ·
all-time + per-season power curve and run best-efforts with PR log · efficiency factor and
decoupling over time · aerobic vs. anaerobic TE distribution · swim pace / SWOLF trend ·
strength volume per exercise category · year-over-year volume.

### 4.5 Body & zones
Weight, resting HR, max HR, threshold HR, FTP over time (from `profile_snapshots`), W/kg, and the
zone table in force on any date.

### 4.6 Gear
Every device/sensor seen: first/last seen, activity count, firmware history, battery status
trend, cumulative operating time — "power-meter battery is low" without opening Garmin Connect.

### 4.7 Explorer (the library, surfaced — replaces `fitparse.py` list / dumptypes / vo2max and the `.bat` files)
Pick any imported file → header + CRC status → message-type counts → click a type → paged table
of rows with field name, value, units, raw value, def num; unknown messages and fields included;
developer fields flagged; toggles for standard-units processor and CRC check; column picker →
quick chart of any numeric field; download JSON / CSV of a message type. Cross-file mode:
"which files contain message X / field Y" (the survey script, as a feature). This is also the
workbench for decoding the `unknown_*` messages.

### 4.8 Import
Drag-drop `.fit` / `.zip`, scan a directory (recursive), optional watch folder; per-file result
list (imported / duplicate / partial / failed + reason) streamed via polling a job endpoint;
"reparse all". Failed and partial files stay visible with their error.
**Garmin sync panel** (§6): auth state, last run, "Sync now", per-activity failures with retry.

### 4.9 Settings
Units (metric / statute), week start, default date range, zone overrides (else from
`zones_target`), load model (TE-based vs. TRIMP vs. TSS), watch-folder path, which sports count
toward load.

---

## 5. API (everything the UI does; all JSON)

```
GET  /api/ping                                   hostname, status, timestamp, version
GET  /api/settings            POST /api/settings
POST /api/import/upload       POST /api/import/scan      GET /api/import/jobs/<id>
POST /api/import/reparse-all
GET  /api/activities?sport=&from=&to=&q=&sort=   GET /api/files/<id>
GET  /api/sessions/<id>                          summary + laps + zones + best efforts + devices
GET  /api/sessions/<id>/records?fields=hr,power&downsample=1000
GET  /api/sessions/<id>/track                    GeoJSON
GET  /api/sessions/<id>/lengths | /sets
POST /api/files/<id>/reparse  POST /api/files/<id>/delete   GET /api/files/<id>/download
GET  /api/dashboard                              KPI tiles
GET  /api/fitness?from=&to=                      CTL / ATL / TSB series
GET  /api/trends/vo2max?sport=  /volume?bucket=week  /power-curve  /best-efforts  /efficiency  /swim  /strength
GET  /api/body                                   weight / RHR / FTP / zones series
GET  /api/gear
GET  /api/explorer/<file_id>/messages            type counts + header/CRC
GET  /api/explorer/<file_id>/messages/<name>?page=&units=standard
GET  /api/explorer/search?message=&field=
GET  /api/export/activities.csv | sessions.json
```

---

## 6. Garmin Connect sync module (`app/sync/`)

**Why:** the archive stops at 2024-07-28 and was assembled by hand (per-activity zips, USB copies
into `D:\fit\EpixPro42`). Trends are meaningless with a two-year hole, and nobody keeps up manual
exports. This module back-fills the gap once and then keeps the app current unattended.

### 6.1 Prior art — `D:\hw\epix2gen\tools\gcsync.py`
(Not in `D:\trips\baltic` — that repo has no Garmin code; the exporter lives in the epix2gen project.)
A 260-line CLI on the unofficial **`python-garminconnect`** client: cached-token login with MFA
prompt, pages the activity list 100 at a time, downloads `ActivityDownloadFormat.ORIGINAL`,
unwraps the zip Garmin returns to a flat `<activityId>_ACTIVITY.fit`, skips IDs already on disk,
sleeps between downloads, **stops on HTTP 429** and resumes on re-run, rich progress + summary.
That design is right and is what we port. State of it as found (2026-09-19):

| Observation | Consequence for the port |
|---|---|
| **Never run**: no token cache at `~/.config/garminconnect`, no `data/fit/` output | treat it as a draft, not a proven tool — first task is a live smoke test |
| Written against an older API: line 91 calls `client.garth.dump(tokenstore)`, but the installed `garminconnect 0.3.11` has **no `garth`** (native login; `login(tokenstore)` loads/refreshes tokens itself) | first full login would raise `AttributeError` *after* authenticating. Fix: follow the 0.3.x token flow; pin the version |
| Untracked in its repo (`?? tools/gcsync.py`), and `garminconnect` is missing from that `pyproject.toml` (only hand-installed in the venv) | fitmon declares the dep properly; flag the untracked file to Mike separately |
| Dedupe by filename prefix | replace with DB-backed dedupe (below) |
| Official API is business-only → unofficial client, authenticates *as the user* | conservative pacing, no parallelism, stop-on-429, never retry-loop a login |

### 6.2 Design

```
app/sync/
├── client.py     login / token handling, thin wrapper over garminconnect (the only file that imports it)
├── activities.py list → diff → download ORIGINAL → unwrap → hand to services/importer.py
├── health.py     daily health metrics (phase B, §6.4)
├── job.py        single-flight runner: lock file, progress, SyncStats, event log
└── cli.py        `fitmon-sync` (rich + rich-argparse)
```

- **Only `client.py` touches the third-party library.** The API is unofficial and breaks; one
  seam to patch, and everything else is testable against a fake client.
- **Pipeline:** `list_activities` (newest first) → stop paging after *K* = 50 consecutive IDs
  already known (incremental runs cost 1 request; `--full` pages everything) → for each new ID
  download → unwrap → `importer.import_bytes(raw, source="garmin", garmin_id=…)`. The importer
  already does hash-dedupe, storage under `~/fitmon/fit/`, parse, DB write, event log — sync adds
  no second code path into the DB.
- **New table `garmin_activities`**: `activity_id` PK, name, type, start, list-JSON, `file_id`
  (nullable), status (`new` / `imported` / `duplicate` / `no_original` / `failed`), error, attempts,
  fetched_at. This is the resume state (replaces "what's on disk") *and* supplies what FIT files
  lack: the **activity name**, location name, and Garmin's own computed VO2 max / load for cross-checking ours.
- **Dedupe against the hand-assembled archive:** `ORIGINAL` is the byte-identical file the watch
  uploaded, so sha256 should match the USB copies in `D:\fit\EpixPro42`; verify this in task S.3.
  Fallback key: `file_id.serial_number` + `time_created`. `<id>_ACTIVITY.fit` names in the old
  archive pre-seed `garmin_activities`.
- **Corrupt local copy** (the triathlon): if a file is `partial` and Garmin has the same
  activity, fetch it and replace when the fresh copy parses `ok`.
- **Activities without an original** (manual entries, some third-party syncs) → `no_original`,
  never retried automatically.
- **Pacing:** 1 req/s default, jittered; on 429 or auth failure stop the job, record it, and
  do not run again until the next scheduled slot. Back-fill is ~2 years + everything before 2022
  — at 1 s each even 1,500 activities is under half an hour, so no need to be clever.

### 6.3 Auth, secrets, scheduling
> Written for the single-user case. With multiple users (§7) the token path becomes per-user and
> encrypted, and a web Connect flow is added for people who can't SSH — **§7.5 overrides this
> section where they differ.** The CLI path below remains how Mike/admin connects. Likewise
> "Sync now" and the nightly timer **enqueue into the job worker** (§7.8) rather than spawning
> their own process; the worker's queue replaces the lock file.

- **Tokens** in `~/fitmon/auth/garmin/` (runtime-data side of the split, `chmod 700`), never in
  the repo, never synced between hosts — each host logs in once.
- **Password is never stored** and never accepted by the web UI. First login is interactive:
  `ssh -t munchlax '/opt/homebrew/bin/uv run fitmon-sync login'` → email, password (getpass or
  `$GARMIN_PASSWORD`), MFA code. After that only refresh tokens are used.
- **Token expiry is a visible state, not a silent failure:** `GET /api/sync/status` reports
  `auth: ok | login_required`; the Import tab shows a banner with the exact command to run.
- **Schedule:** a launchd timer (`tools/munchlax/com.fitmon.sync.plist`, daily ~04:00) runs
  `fitmon-sync run`. Not an in-process thread — the Flask debug reloader would run it twice, and
  a sync must survive app restarts. The "Sync now" button (`POST /api/sync/run`) launches the same
  CLI as a subprocess. A lock file in `~/fitmon/data/sync.lock` makes it single-flight
  (also satisfies the single-writer rule for SQLite).
- **Events:** `sync.started`, `sync.activity_imported`, `sync.rate_limited`,
  `sync.auth_required`, `sync.finished` (with counts) → `events.jsonl`.

### 6.4 Phase B — daily health metrics (the other half of "monitor my fitness")
The archive has **no monitoring / sleep / HRV FIT files**, and the watch only keeps recent ones,
but Garmin Connect has the whole history and `garminconnect 0.3.11` exposes it as JSON:
`get_rhr_daily`, `get_hrv_data_range`, `get_sleep_daily`, `get_body_battery`,
`get_max_metrics_range` (Garmin's own VO2 max), `get_training_status`,
`get_training_readiness`, `get_weigh_ins`.
- Table `daily_health` (date PK; resting HR, HRV overnight avg + status, sleep duration + score,
  body battery min/max, stress avg, weight, VO2 max run/bike, training status, readiness,
  acute load) + the raw JSON per day on disk under `~/fitmon/users/<id>/health/YYYY/` so columns can be added
  later without re-fetching.
- Range endpoints first (one request per month, not per day); back-fill oldest-last so the
  dashboard is useful immediately.
- Surfaces in: Dashboard tiles (HRV, sleep, readiness), Body tab (true daily RHR / weight instead
  of per-activity snapshots), and a new **Recovery** section in Trends — load (ours) against
  HRV / RHR / sleep (Garmin's) is the actual overtraining early-warning.
- Own research note first (`docs/garmin-connect-health-endpoints.md`): response shapes are
  undocumented; capture one real sample of each before designing columns.

### 6.5 CLI and API

```
fitmon-sync login                      interactive, MFA-capable
fitmon-sync run   [-n/--limit N] [-f/--full] [-d/--delay S] [-y/--dry-run] [-hl/--health] [-v]
fitmon-sync status                     auth state, last run, counts by status

GET  /api/sync/status                  auth, running?, last run summary, counts by status
POST /api/sync/run      {full, health} start job (409 if one is running)
GET  /api/sync/jobs/<id>               progress for the UI poller
GET  /api/sync/activities?status=failed
POST /api/sync/activities/<id>/retry
```

### 6.6 When the unofficial API breaks (it will)
The importer stays source-agnostic, so the fallbacks need no new code: **USB/MTP** copy of
`\GARMIN\ACTIVITY\` with the watch plugged into spearow (copy only — never delete from the watch,
Garmin Connect still needs to sync them), Garmin's **account data export** zip, or per-activity
zips — all through the existing upload / scan / zip import. `sync.auth_required` /
repeated `sync.failed` events make the breakage obvious on the dashboard rather than showing up
as "no new activities" weeks later.

---

## 7. Users, auth and multi-tenancy

**Why:** friends and family will want to try it. That is not "add a login page" — it turns a
personal tool into a small multi-tenant service holding *other people's* health data and, via
§6, credentials to their Garmin accounts. Three consequences drive everything below: data is
scoped per user everywhere, several v1 features become dangerous and must be restricted, and
the Garmin login can no longer be "SSH in and run a CLI".

Pokeflute's `auth.py` is the reference for the *shape* (Remember-me device tokens, 90 days) but
not the implementation: it is single-user JSON with salted SHA-256, which is not adequate here.

### 7.1 Model
- **Invite-only.** No open registration. An admin creates an invite → one-time link, 7-day
  expiry → invitee picks username + password. Bootstrap: `fitmon-admin create-user -u mike -a`.
- **Roles:** `admin`, `user`. Admin = user management, invites, system status, import-from-server-path,
  global reparse. Admin screens never show another user's fitness data — but the About box says
  plainly that the operator of the box *can* read the database. Don't promise privacy the
  architecture doesn't provide.
- **No sharing between users in v1** (no "follow", no shared dashboards). Schema doesn't preclude it.

### 7.2 Tables
| Table | Columns |
|---|---|
| `users` | id, username (unique, case-folded), display_name, password_hash (werkzeug **scrypt**), role, is_active, created_at, last_login_at |
| `device_tokens` | id, user_id, **token_sha256** (the raw token exists only in the cookie), device_name (from UA), created_at, last_used_at, expires_at (= now + 90 d, slides on use), revoked_at |
| `invites` | token_sha256, role, created_by, expires_at, used_by, used_at |
| per-user settings | `~/fitmon/users/<id>/settings.json` via `/api/settings` (server-side rule still holds; now per user) + a global admin settings file |

### 7.3 Mechanics
- **Login:** `POST /api/auth/login {username, password, remember}`. Unchecked → signed session
  cookie (browser session). Checked → 32-byte random device token, 90-day cookie, `HttpOnly`,
  `SameSite=Lax`, `Secure` when served over HTTPS. Settings → Devices lists and revokes them;
  "log out everywhere"; password change revokes all tokens.
- **Throttling:** failed logins limited per username *and* per IP with growing back-off;
  identical error for unknown user / wrong password. `auth.login_failed` events make abuse visible.
- **`SECRET_KEY`:** generated on first run into `~/fitmon/auth/secret_key`. The
  `'dev-key-change-in-production'` default in `app/config.py` goes away — a guessable key lets
  anyone forge a session.
- **CSRF:** cookie auth + JSON API → every mutating request needs `X-CSRF-Token` (issued by
  `GET /api/auth/me`); multipart uploads included.
- **Scoping, enforced in one place:** a `scoped(Model)` query helper keyed on `g.user`; routes
  never build unscoped queries. Another user's id returns **404, not 403**. A test walks every
  `/api/...<id>...` route as user B against user A's ids — this test is the real guarantee.
- **Unauthenticated surface:** `/api/ping`, login, invite redemption, static assets. Nothing else.

### 7.4 v1/plan features that change because users are no longer trusted
| Feature | Problem | Change |
|---|---|---|
| Directory scan (`/api/import/scan`) and watch folder | reads arbitrary server paths | **admin only** |
| Zip import | zip bombs, path traversal in member names | cap member count + total uncompressed size, read `.fit` members only, never extract to disk by name |
| Upload | disk fill | per-user quota; keep `MAX_CONTENT_LENGTH` |
| FIT parse (pure python, attacker-controlled input) | CPU / memory exhaustion | parse in the job worker with a time + row limit, not in the request |
| Explorer, file download | path from DB | only ever by scoped `file_id`; never a path parameter |
| `debug=True` | **Werkzeug interactive debugger = remote code execution** for anyone who can reach it | `app.run(debug=False, use_reloader=True)` — keeps the "deploy = `git pull`" reloader the fleet convention wants, drops the debugger |
| Error responses | tracebacks leak paths / data | generic JSON error + event-log id |

### 7.5 Impact on Garmin sync (§6)
- Tokens move to `~/fitmon/users/<id>/garmin/` (700), **encrypted at rest** (Fernet, key in
  `~/fitmon/auth/`) — this protects backups and stray copies, not against root on the box.
- Friends can't SSH, so §6.3's "password never accepted by the web UI" holds only for the CLI.
  Add a **Connect Garmin** flow: `POST /api/sync/login {email, password}` → if `needs_mfa`, a second
  step `POST /api/sync/login/mfa {code}` (pending login held server-side for ≤ 5 min; *their*
  accounts may well have MFA even though Mike's doesn't). The password lives for that one request:
  never stored, never logged, scrubbed from event fields and error reports. **Only offered over
  HTTPS** (§7.7) **and only for users an admin has enabled it for** (off by default, §10.8);
  otherwise the UI hides it and points to the alternative.
- **The no-credentials alternative stays first-class:** upload Garmin's account-export zip or
  individual files. The Connect screen states the trade-off honestly — connecting means this
  server holds a token with full access to your Garmin account until you disconnect.
- **Disconnect** deletes the tokens. **Delete my account** removes all rows, files, tokens and
  health JSON for that user; **Download my data** (zip of original FITs + CSV/JSON) comes first.
- Scheduler iterates users sequentially; one user's auth failure skips that user, but a **429
  stops the whole run** — all accounts share one source IP, and several accounts from one IP on
  an unofficial API is exactly what gets throttled. Keep the user count small and the pacing slow.

### 7.6 API additions
```
POST /api/auth/login | /logout | /logout-all     GET /api/auth/me
POST /api/auth/password                          GET /api/auth/devices   POST /api/auth/devices/<id>/revoke
GET  /api/invite/<token>                         POST /api/invite/<token>/accept
GET  /api/admin/users    POST /api/admin/users/<id>/disable | /enable | /role
GET  /api/admin/invites  POST /api/admin/invites         POST /api/admin/invites/<id>/revoke
POST /api/sync/login     POST /api/sync/login/mfa        POST /api/sync/disconnect
GET  /api/account/export                         POST /api/account/delete
```
CLI: `fitmon-admin create-user | reset-password | list-users | disable-user` (rich, short flags)
— the recovery path when the only admin forgets their password.

### 7.7 Reachability: Tailscale (decided)
- `tailscale serve --bg 8640` on munchlax → `https://munchlax.<tailnet>.ts.net` with a real
  certificate, reachable only from the tailnet and from people the node is **shared** with. Each
  guest installs Tailscale and accepts a node share — that is the onboarding cost, and it is also
  the outer security layer: the login page is not on the internet.
- The app still binds `0.0.0.0:8640` so pokeflute's probe from togepi reaches `/api/ping` over
  the LAN. Everything else is effectively HTTPS-only: auth cookies are **always `Secure`**, so a
  login over plain LAN HTTP simply doesn't stick, and the login page shows the ts.net URL instead.
- `ProxyFix` (one hop) so Flask sees `X-Forwarded-Proto: https` from `tailscale serve`.
- `tailscale serve` adds `Tailscale-User-Login`; record it in auth events as an audit trail. It
  is **not** used for authentication — app accounts are, so a shared laptop ≠ shared identity.
- Invite links are generated against the ts.net base URL (admin setting `public_base_url`).
- The Flask dev server with reloader stays (not internet-facing; ≤ 10 users); `threaded=True`.

### 7.8 Sizing — expect 4–5 users, design for 10
Basis: Mike's archive is 108 files → 311k `record` rows (~2.9k rows and ~100 KB of FIT per
activity, 0.6 s parse). A long-time Garmin user back-filling a whole account is ~1,500–2,500
activities.

| Resource | Per heavy user | × 10 | Design response |
|---|---|---|---|
| `records` rows | ~6 M | **~60 M worst case**, realistically 15–25 M | `records` as `WITHOUT ROWID`, PK `(session_id, t)` — clustered, so a session's series is one sequential range read and there is no second index to store. Narrow types (ints, scaled ints for lat/lon). Every chart endpoint reads one session or pre-aggregated tables, **never scans `records` across sessions** |
| DB size | ~0.5 GB | ~5 GB | fine for SQLite; WAL, `busy_timeout=5000`, `synchronous=NORMAL`. Nightly `VACUUM INTO` backup to snorlax |
| FIT originals | ~250 MB | ~2.5 GB | default quota 5 GB / 5,000 files per user, admin-adjustable |
| Cross-activity analytics (CTL, volume, PRs, power curve) | — | — | computed **at import** into `sessions` / `best_efforts` / `zone_time` / a `daily_load` table, so dashboards are indexed lookups regardless of user count |
| Parse CPU | ~20 min per full back-fill | — | **one job worker process, one queue**: imports, reparses and syncs are serialized. It is also the only bulk writer, so SQLite's single-writer limit never shows up as `database is locked` in a web request |
| Garmin requests | back-fill ~35 min at 1 req/s; nightly incremental 1–2 requests | one source IP for all | **one back-fill at a time**, queued; nightly run does incrementals for everyone first, then continues at most one pending back-fill; global daily request budget (config, start at 3,000); a 429 ends the night |
| Web concurrency | — | a few simultaneous | threaded dev server is enough; all heavy work is in the worker |
| Reparse-all after a schema change | 20 min | ~3.5 h | per-user, resumable, background; the app stays usable and shows "re-indexing n/m" |

Beyond ~10 users or any public exposure the answers change (Postgres, waitress/gunicorn, a real
queue, per-user Garmin egress) — explicitly not designed for.

---

## 8. Phases and task tracker

| # | Task | Phase | Status |
|---|---|---|---|
| 0.1 | Claim port 8640 / munchlax in `D:\hw\pokeflute\data\ports.json`, commit + push there | 0 Groundwork | ✅ 8640 claimed, pushed (pokeflute e837494) |
| 0.2 | `pyproject.toml` (uv), deps: flask, flask-sqlalchemy, rich, rich-argparse, `cryptography`, `garminconnect` (pinned) | 0 | ✅ |
| 0.3 | `__version__`, header, `/api/ping`, bind 0.0.0.0 + new port, **debugger off / reloader on**, event logger, settings service | 0 | ✅ |
| 0.4 | Move runtime data to `~/fitmon/` (per-user layout `users/<id>/`); document split in `CLAUDE.md`; migrate the 16 uploaded files to user 1; check free disk on munchlax against §7.8 | 0 | ◐ runtime data is in `~/fitmon/`; munchlax disk not checked yet; v1 `uploads/` not migrated (all 16 are also in `D:it`) |
| 0.5 | Fix `sub_sport` typo (quick win, independent of rewrite) | 0 | ✅ (fixed by the parser rewrite) |
| A.1 | `users` / `device_tokens` / `invites` tables; scrypt hashing; generated `SECRET_KEY`; `fitmon-admin` CLI (bootstrap + password reset) | A Auth | ✅ |
| A.2 | `/api/auth/*`: login, logout, remember-me 90-day device tokens (hashed at rest, sliding), devices list + revoke, password change; login page; login throttling; auth events | A | ✅ |
| A.3 | `scoped()` query helper + `login_required` on everything but ping/login/invite; CSRF token; generic error responses; **cross-user access test over every id-bearing route** (extended as routes are added — it is part of each later task's definition of done) | A | ✅ |
| A.4 | Invites + Admin tab (users, invites, disable); scan-directory / watch-folder gated to admin | A | ✅ |
| A.5 | Untrusted-input hardening: zip limits, per-user quota, parse time/row limits in the job worker | A | ✅ |
| A.6 | Account: download my data, delete my account | A | ✅ |
| 1.1 | New schema (§2) **with `user_id` ownership from the start** + `schema_version` + drop-and-reparse; `records` `WITHOUT ROWID` clustered on `(session_id, t)`; WAL + busy_timeout pragmas (§7.8) | 1 Data | ✅ |
| 1.1b | Job worker process + queue (imports, reparses, syncs serialized; progress rows the UI polls); resumable per-user reparse | 1 | ✅ |
| 1.2 | Parser rewrite → `ParsedFile`; sessions/laps/records/lengths/sets/devices/profile | 1 | ✅ |
| 1.3 | Tolerant `partial` import for corrupt files (tri file = test case; keep messages read before the error) | 1 | ✅ |
| 1.4 | Importer: `import_bytes()` core, hash dedupe, recursive scan, zip, job status; `fitmon-import` CLI (rich, `-d/--dir`, `-v`) | 1 | ✅ |
| 1.5 | Tests: parser against `tests/files/` + `mike/*.fit`; importer dedupe; partial file | 1 | ✅ 63 tests |
| 1.6 | Import all of `D:\fit` (111 files + 14 zips); sanity-check counts against the survey | 1 | ◐ verified on a scratch instance (103 files → 115 sessions, 270k records, 23 MB, 47 s); real import waits for Mike's account |
| S.1 | Live smoke test on spearow: `garminconnect` pinned, login + MFA + token reload under the 0.3.x flow (fix the `client.garth` call), list 5, download 1. Record findings in `docs/garmin-connect-sync-research.md` | S Sync | ☐ needs Mike's Garmin login - **the sync has never talked to Garmin** |
| S.2 | `app/sync/client.py` + `activities.py` + `garmin_activities` table; fake-client tests (zip unwrap, bare FIT, resume, stop-on-429, K-known early stop, `no_original`) | S | ✅ (against a fake client) |
| S.3 | `fitmon-sync` CLI; verify sha256 of a Garmin `ORIGINAL` equals the USB copy in `D:\fit\EpixPro42`; pre-seed IDs from `<id>_ACTIVITY.fit` names | S | ◐ CLI + pre-seed done; sha256-equality check needs a live download |
| S.4 | **Back-fill**: full sync of the account; re-fetch the corrupt triathlon; reconcile counts | S | ☐ after S.1 |
| S.5 | `/api/sync/*`, Import-tab sync panel, auth-required banner, events; **per-user encrypted token store; web "Connect Garmin" flow incl. MFA step, HTTPS-only; disconnect** (§7.5) | S | ✅ code + tests; live flow unverified |
| S.6 | launchd timer on munchlax iterating all connected users (429 stops the run); Mike's first login there (after 5.2) | S | ◐ plist + script in `deploy/`, not installed |
| S.7 | Phase B: capture real samples of each health endpoint → `docs/garmin-connect-health-endpoints.md` → `daily_health` + back-fill | S | ◐ written against garminconnect's typed models; **no real sample captured yet** - do that before trusting the columns |
| S.8 | Phase B UI: dashboard tiles, Body tab daily series, Trends → Recovery | S | ✅ code |
| 2.1 | SPA shell + tabs; Activities table; Import tab (REST, no form POSTs) | 2 Core UI | ✅ code - **UI not yet seen in a browser** |
| 2.2 | Session detail: summary, time-series, laps, map | 2 | ✅ code - unseen |
| 2.3 | Explorer tab + API (retire `fitparse.py`, `*.bat`, `tri2025fit.out`) | 2 | ◐ Explorer done; legacy `fitparse.py` / `*.bat` / `tri2025fit.out` not removed yet |
| 3.1 | `metrics.py`: zones, best efforts, NP/IF/TSS, TRIMP, SWOLF, decoupling + tests | 3 Analytics | ✅ |
| 3.2 | Sport panels: run dynamics, bike pedalling/power curve, swim lengths, strength sets | 3 | ✅ code - unseen |
| 3.3 | `fitness.py`: CTL/ATL/TSB, volume buckets; Dashboard | 3 | ✅ |
| 3.4 | Trends tab; Body & zones tab; Gear tab | 3 | ✅ code - unseen |
| 4.1 | Regenerate `fitparse/profile.py` from the current FIT SDK; re-run survey, see which `unknown_*` get names (expect `time_in_zone`) | 4 Depth | ☐ |
| 4.2 | Research remaining Garmin-private messages (140, 79, 141, 233 …) → `docs/garmin-unknown-messages-research.md` with citations, *before* coding against them | 4 | ☐ |
| 4.3 | Surface what 4.1/4.2 unlock (recovery time, training status, device time-in-zone …) | 4 | ☐ |
| 5.1 | Settings tab complete; mobile `@media (max-width: 480px)` pass | 5 Ship | ◐ written, not checked on a phone |
| 5.2 | Deploy per `D:\hw\pokeflute\docs\deploying-a-new-munchlax-service.md`; `~/services-registry/fitmon.json`; `tools/deploy.sh`; launchd units for web + job worker | 5 | ◐ `deploy/` + `docs/fitmon-deploy.md` prepared, not run |
| 5.3 | `tailscale serve` HTTPS front on munchlax, `ProxyFix`, `public_base_url`, always-`Secure` cookies; **verify a shared-node guest can reach it**; document guest onboarding in `docs/guest-onboarding.md` | 5 | ☐ |
| 5.4 | Nightly `VACUUM INTO` DB backup + FIT originals rsync to snorlax (`tools/munchlax/`) | 5 | ◐ script prepared, not installed |
Each task = one commit with a version bump, pushed.

**Ordering:** S.1–S.4 run right after phase 1, *before* the analytics phases — CTL/ATL, trends and
PR logs built on a two-year-stale archive can't be sanity-checked. S.5 lands with the Import tab
(2.1); S.6 after deploy; S.7–S.8 after phase 3.
Auth: A.1–A.3 come **before phase 1's importer and before any UI work**, so no route is ever
written unscoped; A.4–A.6 can trail until just before anyone else is invited. Nobody but Mike
gets an account until A.1–A.5 and the Tailscale HTTPS front (5.3) are done.

---

### 8.1 Build log - 2026-09-19 (v0.2.0)
Everything except the items marked ☐/◐ above was built in one pass. What real data taught us
(each is now covered by a test):

| Finding | Consequence |
|---|---|
| An Epix Pro **multisport file writes all five `session` messages first**, each stamped with the file's start time - `session.timestamp` is not an end time | records/laps are assigned to legs by `start_time` only. Before this, all 7,383 samples of the triathlon landed in the run leg |
| The "corrupt" triathlon is complete apart from its tail: 5 legs, all samples | `partial` files are fully usable |
| `fit_bad_tri/` holds outputs of FIT **repair tools**: they parse cleanly but one collapses the race into a single 57 km "run", another claims 1,093 km | same-activity detection (same serial+created **or** same start second) keeps **one** copy, ranked by legs-with-duration → clean parse → samples; implausible leg totals are dropped and the file flagged `partial`; distance jumps are cut out of best-effort windows. Without this the PR table showed 10 km in 18 min |
| Watches report **running power**; scoring it against cycling FTP gave a 63-min run TSS 285 | TSS and power zones are cycling-only; power curves are per sport, default cycling |
| `resting_heart_rate` = 0 means "not set"; anonymous `device_info` rows are the watch's internal parts | stored as NULL; filtered out of Gear |
| Garmin product ids (4312 = Epix Pro 42 mm) are numeric with this old SDK profile | task 4.1 will name them |

Deviations from the plan: no `daily_load` table (sessions carry `load`; CTL is one grouped
query over an indexed column - add the table only if that ever shows up in a profile);
`/api/import/reparse-all` became `/api/import/reindex`; runtime scaffolding lives in `deploy/`
(the fleet convention) rather than `tools/`.

**Not verified:** the UI has not been looked at in a browser (the agent may not type passwords
into one), nothing has touched Garmin's servers, nothing is deployed.

---

## 9. Out of scope (for now)
Monitoring / sleep / HRV **FIT files** (none in the archive; the same data comes as JSON via
§6.4; `files.type` leaves room) · the official Garmin Health API (business-only) · writing
anything back to Garmin Connect · sharing / social features between users · open registration,
email verification, password-reset-by-email (admin resets via CLI) · OAuth / SSO · editing
activities · writing FIT files (the library is read-only) · Strava or other sources (the importer
is source-agnostic if that changes).

## 10. Decisions (Mike, 2026-09-19)
1. **Host: munchlax:8640**, developed on spearow. Archive goes in via `fitmon-import` / upload; ongoing data via the sync module.
2. **Load model:** TSS where power + FTP exist, else HR-TRIMP; Garmin TE displayed but not used for CTL.
3. **Stale archive** is solved by automating export (§6), not by another manual pull.
4. **Keep Flask-SQLAlchemy**; bulk-insert `records` with `executemany`.
5. **Garmin MFA: off** (confirmed by Mike). So login can run non-interactively from `$GARMIN_EMAIL` / `$GARMIN_PASSWORD` for that one invocation — no TTY needed over SSH to munchlax. The MFA prompt path stays in the code (Garmin can turn it on), but re-login is a one-liner, not a chore.
6. **User auth, multi-user** — friends and family will try it (§7). Supersedes the earlier "no auth, LAN-only" stance and answers the phase-B privacy question.
7. **Access is via Tailscale** (§7.7) — `tailscale serve` on munchlax for HTTPS; no public URL, no Funnel.
8. **Web "Connect Garmin" is built but off by default per user**; an admin enables it per person. Upload / export-zip is what a new guest gets.
9. **Sizing: expect 4–5 users, design for 10** (§7.8).

## 11. Open questions
None blocking. To verify during the work rather than decide now: that a Tailscale *shared-node*
guest can reach the `tailscale serve` HTTPS name (5.3), and free disk on munchlax against the
§7.8 estimate (0.4).
