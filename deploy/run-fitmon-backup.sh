#!/bin/bash
# Nightly backup to snorlax: a consistent SQLite snapshot (VACUUM INTO - safe while the app is
# writing, unlike copying the .db file) plus the original FIT files, settings and health JSON.
# Garmin tokens are deliberately NOT backed up: they are re-creatable and are credentials.
set -euo pipefail
SRC=~/fitmon
DEST=~/snorlax-homes/mike/backups/fitmon      # SMB mount under $HOME, not /Volumes (see fleet notes)
# The SMB mount drops silently - it was found unmounted for seven weeks in Sept 2026, with
# nothing noticing. homeseg owns the mount tooling; reuse it rather than duplicating the
# credentials handling, and only then give up.
if [ ! -d ~/snorlax-homes/mike ] && [ -x ~/scripts/snorlax-mount-retry.sh ]; then
    echo "snorlax not mounted - attempting mount via homeseg's script" >&2
    ~/scripts/snorlax-mount-retry.sh || true
fi
if [ ! -d ~/snorlax-homes/mike ]; then
    echo "snorlax share is not mounted at ~/snorlax-homes and could not be mounted - no backup taken" >&2
    exit 1
fi
mkdir -p "$DEST/db" "$DEST/users"
SNAP="$DEST/db/fitmon-$(date +%u).db"            # one per weekday: a rolling week of snapshots
rm -f "$SNAP"
sqlite3 "$SRC/data/fitmon.db" "VACUUM INTO '$SNAP'"
rsync -a --delete --exclude 'garmin/' --exclude 'incoming/' "$SRC/users/" "$DEST/users/"
rsync -a "$SRC/data/settings.json" "$DEST/" 2>/dev/null || true
echo "backup ok: $SNAP"
