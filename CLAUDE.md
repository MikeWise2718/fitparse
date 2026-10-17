# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

python-fitparse is a Python library for parsing ANT/Garmin `.FIT` (Flexible and Interoperable Data Transfer) binary files used by fitness devices (Garmin watches, bike computers, Strava, etc.).

## Common Commands

### Running Tests
```bash
# Run all tests
python -m unittest discover -s tests

# Run a single test file
python -m unittest tests.test

# Run a specific test case
python -m unittest tests.test.FitFileTestCase.test_basic_file_with_one_record

# Run with coverage
coverage run run_tests.py && coverage report -m
```

### Linting
```bash
# Check for syntax errors and undefined names (blocking)
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics

# Full lint with warnings (non-blocking)
flake8 . --count --exit-zero --max-complexity=10 --max-line-length=127 --statistics
```

### Updating the FIT Profile
`fitparse/profile.py` is generated (currently from FIT SDK **21.218**, 2026-10-17). The SDK zip
with `Profile.xlsx` sits behind a licence click-through on developer.garmin.com, so generate it
from Garmin's open Python SDK instead, which carries the same profile as a dict (same FIT Protocol
License):
```bash
curl -L -o sdk.tar.gz https://github.com/garmin/fit-python-sdk/archive/refs/tags/<version>.tar.gz
tar xzf sdk.tar.gz
python scripts/generate_profile_from_sdk.py fit-python-sdk-<version>/garmin_fit_sdk/profile.py fitparse/profile.py
```
It reuses `generate_profile.py`'s renderer, so the output has the same shape. Run both test suites
afterwards, and check that `app/services/fit_parser.py` still finds what it reads by name: it
relies on `unknown_140` (VO2 max), which 21.218 still leaves unnamed. The old route still works
with an SDK zip: `python scripts/generate_profile.py /path/to/fit_sdk.zip fitparse/profile.py`
(needs `xlrd`).

### CLI Tool
```bash
# Dump FIT file to readable format
python scripts/fitdump my_activity.fit

# Output as JSON
python scripts/fitdump -t json my_activity.fit
```

## Architecture

### Class Hierarchy (base.py)

The parsing system uses a mixin-based architecture with progressive enhancement:

```
FitFileDecoder          - Low-level binary parser, handles FIT protocol
    ↓ (inherits)
DeveloperDataMixin      - Adds support for custom developer-defined fields
    ↓ (inherits)
UncachedFitFile         - Adds data processing (type conversion, scaling)
    ↓ (inherits)
CacheMixin              - Adds message caching
    ↓ (inherits)
FitFile                 - Full-featured parser (public API)
```

### Data Processing Pipeline (processors.py)

Field values flow through a processor chain using Django-style method naming:
1. `process_type_<type_name>` - Type conversion (bool, date_time, etc.)
2. `process_field_<field_name>` - Field-specific processing
3. `process_units_<unit_name>` - Unit conversion
4. `process_message_<mesg_name>` - Message-level post-processing

Custom processors extend `FitFileDataProcessor`. Use `StandardUnitsDataProcessor` for km/km/h instead of m/m/s.

### Profile System (profile.py)

Auto-generated from ANT FIT SDK (~12K lines). Contains:
- `MESSAGE_TYPES` - All 85+ FIT message type definitions
- `FIELD_TYPES` - Field type definitions with enums
- Field definitions with scale/offset factors

### Key Data Structures (records.py)

- `DataMessage` - Parsed message with fields, iterable to get `FieldData` objects
- `FieldData` - Single field with `name`, `value`, `raw_value`, `units`
- `DefinitionMessage` - Message structure definition from FIT file

### Error Types (utils.py)

- `FitParseError` - General parsing errors
- `FitCRCError` - CRC validation failures
- `FitEOFError` - Unexpected end of file
- `FitHeaderError` - Invalid FIT header

## Test Files

Test FIT files are located in `tests/files/`. Many tests compare parsed output against reference CSV files for validation.

---

# Fitmon (the web app in `app/`)

Multi-user fitness-monitoring Flask app built on the library above. **Spec and task tracker:
`specs/fitmon-app.md`** — read it first; deployment is in `docs/fitmon-deploy.md`. The route
library and planner (Routes tab) have their own spec: `specs/route-planner.md`.

## Code vs. runtime data
- **Code**: this repo (`app/`, `deploy/`, `tests/fitmon/`). On munchlax: `~/projects/fitparse/`.
- **Runtime data**: `~/fitmon/` (override with `FITMON_HOME`) — never inside the repo.
  `data/fitmon.db` · `users/<id>/fit/` original FIT files · `users/<id>/settings.json` ·
  `users/<id>/garmin/tokens.enc` · `users/<id>/health/` · `users/<id>/routes/<uuid>.geojson` (saved
  routes: the originals) · `logs/events.jsonl` · `auth/` (generated secret key + token-encryption key).
- The DB is a **rebuildable index** over the original files. There are no migrations: bump
  `SCHEMA_VERSION` in `app/models.py` and re-index (Import tab, or a `reindex` job).

## Commands
```bash
uv sync                                   # Python 3.13 (.python-version); 3.12+ required
uv run pytest                             # tests/fitmon (the library's own tests: python -m unittest discover -s tests)
uv run fitmon-web -w                      # web + inline job worker on :8640 (dev)
uv run fitmon-web  /  uv run fitmon-worker    # as deployed: two processes, exactly one worker
uv run fitmon-admin create-user -u NAME -a
uv run fitmon-admin reindex -u NAME [-s]   # re-index in this process, with this code (-s: only stale files)
uv run fitmon-import -u NAME -d D:/fit
uv run fitmon-sync login|run|status|disconnect
uv run --with playwright --with pillow python tools/screenshot.py -af <run.fit> -mh 170   # landing pictures
```
`tools/screenshot.py` makes the three public landing pictures (`app/static/screenshot.png`,
`app/static/screenshots/`): two from an invented "Demo athlete" year, one animated playback of a
real run Mike chose (privacy-cut: first/last 500 m off the map). The FIT file is passed in, never
committed. Re-run it when the dashboard, trends or activity page change visibly.
On spearow prefix with `env -u PYTHONHOME` (Bash) if Python fails with `SRE module mismatch`.

The planner needs the BRouter engine on `127.0.0.1:8642` (`FITMON_BROUTER_URL` overrides). On a dev
box run `deploy/brouter/setup-brouter.sh` there too (it fetches a JRE and the tiles into `~/brouter`;
on Windows use Git Bash, or unpack them by hand), then `deploy/brouter/run-brouter.sh`. Tests never
need it: they set `app.config['FITMON_ROUTER']` to `routing.brouter.StraightRouter()`.

## Rules that are easy to break
- **Every query on user data starts with `auth.scoped(Model)` / `auth.get_owned(Model, id)`.**
  Another user's id is a 404. `tests/fitmon/test_scoping.py` walks every id-bearing route as the
  wrong user and **fails on any new `<arg>` it has no test id for** — add one, don't skip it.
- Routes are private by default (`auth.PUBLIC_ENDPOINTS` is the allow-list); writes need `X-CSRF-Token`.
- Bulk parsing/import/sync run in the **job worker**, never in a request. Single-file re-indexes
  run inline: "Re-parse", a trim (incl. "End here"), and a sport change. `importer.import_bytes()`
  is the only way data enters the index.
- **Per-activity corrections are keyed by `(file_id, idx)`, never by session id**:
  `SessionTrim`, `SessionExclusion`, `SessionSport`. A re-index deletes and recreates session
  rows (new ids), so anything stored on the row is lost; the importer re-applies these before
  computing metrics. In the UI, navigate with `openDetail(newId)` after one - reloading the old
  id 404s and bounces to the list.
- The JavaScript has **no automated tests**; check UI changes in a real browser (Playwright
  against a local copy, as `tools/screenshot.py` does). The Routes views are in
  `static/js/routes.js`, loaded before `app.js`, which hands it its helpers (`window.fitmonRoutes`).
- Only `app/sync/client.py` imports `garminconnect` (unofficial API, pinned). Passwords are never
  stored or logged; `events.py` redacts credential-looking fields.
- Only `app/routing/brouter.py` talks to the routing engine. Planner code (`routing/plan.py`,
  `routing/fit.py`) takes a router object and never knows which; keep it free of Flask and the DB.
- Routes: the GeoJSON file is the original, the `routes` table an index of it
  (`routelib.reindex_routes`). `RouteMatch` is keyed by `(file_id, idx)` like the trims.
  `SessionShape` (each GPS activity resampled every 25 m) is filled **lazily** by
  `routelib.refresh()` when the Routes tab asks, not by the importer - so matching works whatever
  code the (non-reloading) worker is running.
- **Body weight in activity files can be someone else's** (a family scale). `weight_min_kg` (user
  setting) drops such weights, and `importer.apply_weight_fix` rescales running power and calories
  to the nearest own weight before metrics run; the watch's figures go to `session_weight_fixes`.
  Never rescale bike power: a power meter measures it.
- Never turn on the Werkzeug debugger (`debug=True`): other people can reach this app.
- Bump `app/__init__.py: __version__` in every commit that changes code.
- The top-level `fitparse.py` script is a legacy CLI that shares its name with the `fitparse/`
  package (the package wins on import). The Explorer tab replaces it.
