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
When a new ANT FIT SDK is released:
```bash
python3 scripts/generate_profile.py /path/to/fit_sdk.zip fitparse/profile.py
```

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
`specs/fitmon-app.md`** — read it first; deployment is in `docs/fitmon-deploy.md`.

## Code vs. runtime data
- **Code**: this repo (`app/`, `deploy/`, `tests/fitmon/`). On munchlax: `~/projects/fitparse/`.
- **Runtime data**: `~/fitmon/` (override with `FITMON_HOME`) — never inside the repo.
  `data/fitmon.db` · `users/<id>/fit/` original FIT files · `users/<id>/settings.json` ·
  `users/<id>/garmin/tokens.enc` · `users/<id>/health/` · `logs/events.jsonl` · `auth/` (generated
  secret key + token-encryption key).
- The DB is a **rebuildable index** over the original files. There are no migrations: bump
  `SCHEMA_VERSION` in `app/models.py` and re-index (Import tab, or a `reindex` job).

## Commands
```bash
uv sync                                   # Python 3.13 (.python-version); 3.12+ required
uv run pytest                             # tests/fitmon (the library's own tests: python -m unittest discover -s tests)
uv run fitmon-web -w                      # web + inline job worker on :8640 (dev)
uv run fitmon-web  /  uv run fitmon-worker    # as deployed: two processes, exactly one worker
uv run fitmon-admin create-user -u NAME -a
uv run fitmon-import -u NAME -d D:/fit
uv run fitmon-sync login|run|status
```
On spearow prefix with `env -u PYTHONHOME` (Bash) if Python fails with `SRE module mismatch`.

## Rules that are easy to break
- **Every query on user data starts with `auth.scoped(Model)` / `auth.get_owned(Model, id)`.**
  Another user's id is a 404. `tests/fitmon/test_scoping.py` walks every id-bearing route as the
  wrong user and **fails on any new `<arg>` it has no test id for** — add one, don't skip it.
- Routes are private by default (`auth.PUBLIC_ENDPOINTS` is the allow-list); writes need `X-CSRF-Token`.
- Bulk parsing/import/sync run in the **job worker**, never in a request (only the single-file "Re-parse" button parses inline). `importer.import_bytes()` is
  the only way data enters the index.
- Only `app/sync/client.py` imports `garminconnect` (unofficial API, pinned). Passwords are never
  stored or logged; `events.py` redacts credential-looking fields.
- Never turn on the Werkzeug debugger (`debug=True`): other people can reach this app.
- Bump `app/__init__.py: __version__` in every commit that changes code.
- The top-level `fitparse.py` script is a legacy CLI that shares its name with the `fitparse/`
  package (the package wins on import). The Explorer tab replaces it.
