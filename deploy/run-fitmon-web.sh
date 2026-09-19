#!/bin/bash
# Launcher for the fitmon web process on munchlax (macOS + launchd).
# Runtime data lives in ~/fitmon/ (the app generates its own secret key there); the optional
# ~/.fitmon/env only carries overrides such as FITMON_PORT.
set -a
[ -f ~/.fitmon/env ] && source ~/.fitmon/env
set +a
cd ~/projects/fitparse
exec /opt/homebrew/bin/uv run fitmon-web
