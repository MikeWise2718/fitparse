#!/bin/bash
# Nightly backup to snorlax: a consistent SQLite snapshot (VACUUM INTO - safe while the app is
# writing, unlike copying the .db file) plus the original FIT files, settings and health JSON.
# Garmin tokens are deliberately NOT backed up: they are re-creatable and are credentials.
#
# Runs as a ROOT LaunchDaemon (deploy/com.fitmon.backup.plist) and sends over SSH, not through
# the ~/snorlax-homes SMB mount. Until 2026-11-01 it ran as mike and wrote to the mount, and
# failed EVERY night from 2026-09-21 with "Operation not permitted": macOS lets a launchd job
# use a network volume only after someone clicks Allow on a prompt shown on munchlax's screen
# (nobody sees it), and charges the grant to /bin/bash. Going over SSH avoids the volume, and
# root avoids the other trap: Local Network Privacy blocks a launchd job running as mike from
# LAN-only hosts like snorlax ("No route to host"); root is exempt. Same pattern as
# agent-mine's backup. pokeflute checks the result on snorlax hourly (docs/backup-monitoring.md
# there), so a failure is a problem + an email, not a log line.
#
# Local files are read and written as mike (no root-owned files in mike's dirs); only the
# transfer runs as root, with mike's key ~/.ssh/fitmon_backup (alias snorlax-fitmon for mike).
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

MIKE=mike
MHOME=/Users/mike
SRC=$MHOME/fitmon
STAGE=$MHOME/.fitmon/backup-stage
REMOTE=mike@snorlax
DEST=backups/fitmon                      # on snorlax: /share/homes/mike/backups/fitmon
# Root does not read mike's ~/.ssh/config, so everything is spelled out. The host key is
# checked against mike's known_hosts (snorlax is already in it).
SSH="ssh -i $MHOME/.ssh/fitmon_backup -o IdentitiesOnly=yes -o BatchMode=yes \
 -o ConnectTimeout=15 -o UserKnownHostsFile=$MHOME/.ssh/known_hosts -o StrictHostKeyChecking=yes"

as_mike() { if [ "$(id -u)" -eq 0 ]; then sudo -u "$MIKE" "$@"; else "$@"; fi; }

echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') fitmon backup (uid $(id -u)) ==="
SNAP_NAME="fitmon-$(date +%u).db"          # one per weekday: a rolling week of snapshots
as_mike mkdir -p "$STAGE"
as_mike rm -f "$STAGE"/fitmon-*.db
as_mike sqlite3 "$SRC/data/fitmon.db" "VACUUM INTO '$STAGE/$SNAP_NAME'"

$SSH "$REMOTE" "mkdir -p $DEST/db $DEST/users"
# The snapshot: written whole to a temp name by rsync, then renamed, so a half-sent file
# never replaces a good one. Its mtime is now (VACUUM just wrote it): that is what
# pokeflute's freshness check reads.
rsync -a -e "$SSH" "$STAGE/$SNAP_NAME" "$REMOTE:$DEST/db/$SNAP_NAME"
rsync -a --delete --exclude 'garmin/' --exclude 'incoming/' -e "$SSH" "$SRC/users/" "$REMOTE:$DEST/users/"
if [ -f "$SRC/data/settings.json" ]; then
    rsync -a -e "$SSH" "$SRC/data/settings.json" "$REMOTE:$DEST/"
fi
as_mike rm -f "$STAGE/$SNAP_NAME"
echo "backup ok: snorlax:$DEST/db/$SNAP_NAME ($(date '+%H:%M:%S %Z'))"
