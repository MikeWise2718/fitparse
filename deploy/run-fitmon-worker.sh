#!/bin/bash
# Launcher for the fitmon job worker on munchlax. Exactly one per host: it is the single bulk
# writer to the SQLite database (imports, re-indexes, Garmin syncs).
set -a
[ -f ~/.fitmon/env ] && source ~/.fitmon/env
set +a
cd ~/projects/fitparse
exec /opt/homebrew/bin/uv run fitmon-worker
