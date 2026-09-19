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
2. **Triathlon file fails**: `2024-07-28-09-44-53.fit` (3 copies) → `FitParseError: Got data message with invalid local message type 14`. `tri2025fit.out` shows ~300k lines were dumped before the failure, so most of the file is readable. Needs investigation (library bug vs. corrupt file) and tolerant import.
3. Activity bounds come from min/max `event` timestamp instead of `session.start_time` / `total_timer_time`.
4. Multisport files collapse to one row with the *last* `sport` message winning.
5. Duplicate detection is filename + date; should be content hash (and `serial_number` + `time_created`).
6. Top-level `fitparse.py` script shadows-by-name the `fitparse/` package (works only because packages win). Retire it once the explorer + CLI replace it.
7. No `pyproject.toml`; deps in `requirements.txt` only.

### 1.4 Divergence from the global Flask conventions
| Rule | v1 | Plan |
|---|---|---|
| Port reserved | binds **5000 = pokeflute's port**, unreserved | claim **8640** (next free in the 86xx run) — host TBD, see §8 |
| Version in header | none | `app/__init__.py: __version__`, shown in `<h1>`, bump per commit |
| `GET /api/ping` | none | add |
| REST/JSON for every action | form POSTs + `flash()` + redirects | all actions → `/api/...`, JS renders |
| Runtime data outside repo | `uploads/`, `instance/` inside repo | `~/fitmon/{fit,data,logs}/` |
| `events.jsonl` | none | add `app/logging.py` event logger |
| Server-side settings | none | `~/fitmon/data/settings.json`, `GET/POST /api/settings` |
| Bind 0.0.0.0, debug reloader | 127.0.0.1 | fix in `run.py` |
| Auth | none | none — LAN-only personal tool (decision recorded; revisit if exposed beyond LAN) |
| uv / rich / rich-argparse / short flags | no | `pyproject.toml`; import CLI uses rich + rich-argparse |

---

## 2. Data model (SQLite, WAL, at `~/fitmon/data/fitmon.db`)

Original FIT files are kept (`~/fitmon/fit/<sha256[:2]>/<sha256>.fit`) — the DB is a **rebuildable
index**; `reparse all` must always be able to regenerate it after a schema change. So: no
migrations framework, just a `schema_version` and "drop + reparse".

| Table | Grain | Key columns |
|---|---|---|
| `files` | one FIT file | sha256 (unique), original name, size, device product/serial, `time_created`, utc_offset (from `activity.local_timestamp`), parse_status (`ok` / `partial` / `failed`), parse_error, message-type counts (JSON) |
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

## 6. Phases and task tracker

| # | Task | Phase | Status |
|---|---|---|---|
| 0.1 | Decide host + claim port 8640 in `D:\hw\pokeflute\data\ports.json`, commit there | 0 Groundwork | ☐ blocked on §8 Q1 |
| 0.2 | `pyproject.toml` (uv), deps: flask, flask-sqlalchemy → or plain sqlite3 (decide in 1.1), rich, rich-argparse | 0 | ☐ |
| 0.3 | `__version__`, header, `/api/ping`, bind 0.0.0.0 + new port, event logger, settings service | 0 | ☐ |
| 0.4 | Move runtime data to `~/fitmon/`; document split in `CLAUDE.md`; migrate the 16 uploaded files | 0 | ☐ |
| 0.5 | Fix `sub_sport` typo (quick win, independent of rewrite) | 0 | ☐ |
| 1.1 | New schema (§2) + `schema_version` + drop-and-reparse | 1 Data | ☐ |
| 1.2 | Parser rewrite → `ParsedFile`; sessions/laps/records/lengths/sets/devices/profile | 1 | ☐ |
| 1.3 | Investigate tri-file `invalid local message type 14`; tolerant `partial` import | 1 | ☐ |
| 1.4 | Importer: hash dedupe, recursive scan, zip, job status; `fitmon-import` CLI (rich, `-d/--dir`, `-v`) | 1 | ☐ |
| 1.5 | Tests: parser against `tests/files/` + `mike/*.fit`; importer dedupe; partial file | 1 | ☐ |
| 1.6 | Import all of `D:\fit` (111 files + 14 zips); sanity-check counts against the survey | 1 | ☐ |
| 2.1 | SPA shell + tabs; Activities table; Import tab (REST, no form POSTs) | 2 Core UI | ☐ |
| 2.2 | Session detail: summary, time-series, laps, map | 2 | ☐ |
| 2.3 | Explorer tab + API (retire `fitparse.py`, `*.bat`, `tri2025fit.out`) | 2 | ☐ |
| 3.1 | `metrics.py`: zones, best efforts, NP/IF/TSS, TRIMP, SWOLF, decoupling + tests | 3 Analytics | ☐ |
| 3.2 | Sport panels: run dynamics, bike pedalling/power curve, swim lengths, strength sets | 3 | ☐ |
| 3.3 | `fitness.py`: CTL/ATL/TSB, volume buckets; Dashboard | 3 | ☐ |
| 3.4 | Trends tab; Body & zones tab; Gear tab | 3 | ☐ |
| 4.1 | Regenerate `fitparse/profile.py` from the current FIT SDK; re-run survey, see which `unknown_*` get names (expect `time_in_zone`) | 4 Depth | ☐ |
| 4.2 | Research remaining Garmin-private messages (140, 79, 141, 233 …) → `docs/garmin-unknown-messages-research.md` with citations, *before* coding against them | 4 | ☐ |
| 4.3 | Surface what 4.1/4.2 unlock (recovery time, training status, device time-in-zone …) | 4 | ☐ |
| 5.1 | Settings tab complete; mobile `@media (max-width: 480px)` pass | 5 Ship | ☐ |
| 5.2 | Deploy per `D:\hw\pokeflute\docs\deploying-a-new-munchlax-service.md`; `~/services-registry/fitmon.json`; `tools/deploy.sh` | 5 | ☐ |

Each task = one commit with a version bump, pushed.

---

## 7. Out of scope (for now)
Garmin Connect API sync (files arrive by manual export / watch folder) · monitoring, sleep, HRV
and body-battery files (none in the archive; the schema leaves room — `files.type`) · auth ·
editing activities · writing FIT files (the library is read-only).

## 8. Open questions
1. **Host.** Recommendation: **munchlax:8640** (always on, reachable from phone, fast single-thread parse), develop on spearow, archive imported through the upload/zip UI or a one-off `fitmon-import` over the SMB share. Alternative: spearow-only, since `D:\fit` lives there.
2. **Load model default.** Recommendation: TSS where power + FTP exist, else HR-TRIMP; Garmin TE shown alongside but not used for CTL.
3. **Archive is stale** — newest file is 2024-07-28. Is there a newer export to pull before building trends, and should the watch folder point at wherever those land?
4. Keep Flask-SQLAlchemy, or drop to plain `sqlite3`? Recommendation: keep — models are already there and the ORM cost is irrelevant at this size; bulk-insert `records` with `executemany`.
