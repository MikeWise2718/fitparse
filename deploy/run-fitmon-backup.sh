#!/bin/bash
# Nightly backup to snorlax: a consistent SQLite snapshot (VACUUM INTO - safe while the app is
# writing, unlike copying the .db file) plus the original FIT files, settings and health JSON.
# Garmin tokens are deliberately NOT backed up: they are re-creatable and are credentials.
set -euo pipefail
SRC=~/fitmon
DEST=~/snorlax-homes/mike/backups/fitmon      # SMB mount under $HOME, not /Volumes (see fleet notes)
if [ ! -d ~/snorlax-homes/mike ]; then
    echo "snorlax share is not mounted at ~/snorlax-homes - skipping backup" >&2
    exit 1
fi
mkdir -p "$DEST/db" "$DEST/users"
SNAP="$DEST/db/fitmon-$(date +%u).db"            # one per weekday: a rolling week of snapshots
rm -f "$SNAP"
sqlite3 "$SRC/data/fitmon.db" "VACUUM INTO '$SNAP'"
rsync -a --delete --exclude 'garmin/' --exclude 'incoming/' "$SRC/users/" "$DEST/users/"
rsync -a "$SRC/data/settings.json" "$DEST/" 2>/dev/null || true
echo "backup ok: $SNAP"
