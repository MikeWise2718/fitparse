# Fix the GitHub Actions `test` workflow

Written 2026-11-01 from the `D:\hw\mail` session (Outlook clean-up), for a session in this repo
to pick up.

## Why

Every push to `main` fails CI and sends Mike a "Run failed" mail. **82 of the last 84 runs
failed; the last success was 2026-09-19.** GitHub is already set to mail only failed
workflows, so a green CI means no mail at all. fitparse produced ~170 of the ~500 GitHub
notification mails in Mike's Outlook over the last 90 days (CI failures + push mails).

## What is wrong (checked 2026-11-01, run on `e16269d`)

`.github/workflows/test.yml` is the old upstream fitparse workflow and no longer fits the
project:

1. **The matrix is Python 3.7–3.11 + 3.x**, but `pyproject.toml` says
   `requires-python = ">=3.12"` (garminconnect 0.3.x). `actions/setup-python` no longer
   ships 3.7/3.8 for `ubuntu-latest` (`Version 3.8 with arch x64 not found`), and
   fail-fast then cancels every other job, so nothing gets tested at all.
2. **Actions are on v2** (`actions/checkout@v2`, `actions/setup-python@v2`, Node 20,
   deprecated; GitHub warns on every run).
3. **It installs with pip**, from `requirements.txt` (which no longer exists) and
   `requirements-test.txt`, while the project uses `uv` + `pyproject.toml`.
4. `sudo apt install -y libgirepository1.0-dev`: check whether anything still needs it
   (PyGObject?); on ubuntu-24.04 the package may be `libgirepository-2.0-dev`.
5. It runs on `[push, pull_request]` for every path, including docs-only commits.

Unknown: whether the tests themselves pass on 3.12+ in CI (they never got that far).
Run them locally first: `uv run python -m unittest discover -s tests` and
`uv run coverage run run_tests.py`.

## Plan

| # | Task | Status |
|---|---|---|
| 1 | Run the test suite locally with `uv`; note what fails and whether any test needs network, Garmin credentials, local data (`~/fitparse`, snorlax) or a display | todo |
| 2 | Rewrite `test.yml`: `actions/checkout@v4`, `astral-sh/setup-uv` (pinned major), matrix `3.12` + `3.13` (or just 3.12), `uv sync`, then the tests; drop `requirements.txt`, apt only if still needed | todo |
| 3 | Skip or mark tests that can't run in CI (network, credentials, local files) rather than leaving CI red | todo |
| 4 | Keep flake8's hard-error pass (E9, F63, F7, F82), or switch to `ruff check --select E9,F63,F7,F82` via `uvx ruff` | todo |
| 5 | Optional: `paths-ignore: ['**.md', 'docs/**', 'specs/**']` so docs commits don't run CI | todo |
| 6 | Push, confirm the run is green with `gh run list -L 3`, commit the spec status | todo |

**Done when:** a push to `main` gives a green run and no mail.

If fixing it properly turns out not to be worth it, the fallback is to delete the workflow
(or set it to `workflow_dispatch` only). Ask Mike before choosing that.
