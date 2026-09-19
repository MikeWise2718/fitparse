#!/bin/bash
# Nightly: queue a Garmin sync (activities + health) for every connected user. The worker does
# the work; this only enqueues, so it is safe to run while the app is busy.
set -a
[ -f ~/.fitmon/env ] && source ~/.fitmon/env
set +a
cd ~/projects/fitparse
exec /opt/homebrew/bin/uv run fitmon-sync run --queue --health
