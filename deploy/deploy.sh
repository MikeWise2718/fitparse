#!/bin/bash
# Deploy fitmon to munchlax. Idempotent; run from the repo root on a dev box after `git push`.
#   deploy/deploy.sh            pull + uv sync + register with pokeflute + ping
# The web process runs Flask's reloader, so a code-only deploy needs no restart and no sudo.
# The WORKER does not reload: after changes to parsing/import/sync code restart it (needs a TTY):
#   ssh -t munchlax 'sudo launchctl kickstart -k system/com.fitmon.worker'
set -euo pipefail
HOST=${FITMON_HOST:-munchlax}
APP_DIR='~/projects/fitparse'
UV=/opt/homebrew/bin/uv          # uv is not on PATH over non-interactive SSH

ssh -o BatchMode=yes "$HOST" "cd $APP_DIR && git pull --ff-only && $UV sync --no-dev"
ssh -o BatchMode=yes "$HOST" "mkdir -p ~/services-registry ~/.fitmon ~/fitmon && cat > ~/services-registry/fitmon.json" \
    < deploy/pokeflute-service.json
sleep 3
ssh -o BatchMode=yes "$HOST" "curl -s -m 5 http://localhost:8640/api/ping" && echo
