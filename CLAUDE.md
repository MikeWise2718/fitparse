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
