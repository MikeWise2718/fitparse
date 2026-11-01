#!/bin/bash
# One-time (and after a change to the plist): install fitmon's root backup job and run it once.
#
#   ssh -t munchlax 'sudo ~/projects/fitparse/deploy/install-backup-job.sh'
#
# Why root and SSH: see run-fitmon-backup.sh. Safe to run again.
set -euo pipefail

if [[ $(id -u) -ne 0 ]]; then
    echo "run it with sudo: sudo $0" >&2
    exit 1
fi

SRC=/Users/mike/projects/fitparse/deploy/com.fitmon.backup.plist
DST=/Library/LaunchDaemons/com.fitmon.backup.plist
LOG=/Users/mike/.fitmon/fitmon-backup.log

launchctl bootout system/com.fitmon.backup 2>/dev/null || true
install -m 644 -o root -g wheel "$SRC" "$DST"
plutil -lint "$DST"
launchctl bootstrap system "$DST"
echo "installed com.fitmon.backup; first backup now (takes ~1 min):"
launchctl kickstart system/com.fitmon.backup
for _ in $(seq 1 24); do
    sleep 5
    if tail -1 "$LOG" | grep -q "^backup ok\|^Error\|failed\|denied"; then break; fi
done
tail -6 "$LOG"
