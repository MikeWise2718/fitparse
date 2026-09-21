# Deploying fitmon to munchlax

Follows the fleet guide (`D:\hw\pokeflute\docs\deploying-a-new-munchlax-service.md`); this file
only records what is specific to fitmon. Port **8640** is reserved in pokeflute's `ports.json`.

Status: **deployed 2026-09-21.** Live at **https://munchlax.taild34695.ts.net:8640** (tailnet
only). Web, worker and the nightly sync run as system LaunchDaemons; the backup timer is not
installed because snorlax is not mounted on munchlax (see *What runs*).

## What runs

| Unit | Script | Restart needed after a deploy? |
|---|---|---|
| `com.fitmon.web` | `deploy/run-fitmon-web.sh` → `fitmon-web` (Flask, reloader on, **debugger off**) | no — the reloader picks up `git pull` |
| `com.fitmon.worker` | `deploy/run-fitmon-worker.sh` → `fitmon-worker` | **yes**, when parsing / import / sync code changed |
| `com.fitmon.sync` (04:10) | `deploy/run-fitmon-sync.sh` → `fitmon-sync run --queue --health` | – |
| `com.fitmon.backup` (04:50) | `deploy/run-fitmon-backup.sh` → snapshot + rsync to snorlax | **not installed**: needs snorlax mounted at `~/snorlax-homes` on munchlax, which it is not |

Code: `~/projects/fitparse/`. Runtime data: `~/fitmon/` (db, FIT originals, logs, keys, per-user
settings, encrypted Garmin tokens). Optional overrides: `~/.fitmon/env`. Logs of the launchd
units: `~/.fitmon/*.log`. The app generates its own `SECRET_KEY` and token-encryption key under
`~/fitmon/auth/` on first start — nothing secret has to be written by hand.

## First install

```bash
# 1. code + venv
ssh munchlax 'mkdir -p ~/projects ~/.fitmon ~/fitmon && cd ~/projects && git clone https://github.com/MikeWise2718/fitparse.git'
ssh munchlax 'cd ~/projects/fitparse && /opt/homebrew/bin/uv sync --no-dev'

# 2. first account (interactive: prompts for the password)
ssh -t munchlax 'cd ~/projects/fitparse && /opt/homebrew/bin/uv run fitmon-admin create-user -u mike -a'

# 3. smoke test before launchd
ssh munchlax 'cd ~/projects/fitparse && ./deploy/run-fitmon-web.sh' &
ssh munchlax 'curl -s http://localhost:8640/api/ping'

# 4. launchd: web + worker via pokeflute's installer (needs a TTY for sudo)
for c in fitmon fitmon-worker; do
  ssh munchlax "cat > ~/admin/services/$c.conf" < deploy/$c.conf
  ssh -t munchlax "~/admin/install-launchd-daemon.sh $c"
done
#    (the worker conf has no PORT; if the installer insists on probing one, install the worker
#     plist by hand from the generated web plist - check the installer's README first)

# 5. timers
for j in sync backup; do
  ssh munchlax "cat > /tmp/com.fitmon.$j.plist" < deploy/com.fitmon.$j.plist
  ssh -t munchlax "sudo cp /tmp/com.fitmon.$j.plist /Library/LaunchDaemons/ && sudo launchctl bootstrap system /Library/LaunchDaemons/com.fitmon.$j.plist"
done

# 6. register with pokeflute + verify
deploy/deploy.sh
```

## HTTPS front door: Tailscale

`tailscale` is **not on the PATH** on munchlax; the binary is inside the app bundle. It needs
no sudo:

```bash
ssh -t munchlax
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg --https 8640 8640
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve status
```

**Use an explicit HTTPS port, not the root path.** munchlax already serves klefki on `/` and
ytsum on `:8443`; taking the root would have broken klefki. Fitmon uses `:8640` to match its own
port. `ProxyFix` reads Tailscale's `X-Forwarded-Proto`, so the app knows the connection is
secure — verify by POSTing to `/api/auth/login` through the HTTPS name: a **401** (wrong
password) means it works, a **400** means the app still thinks it is plain HTTP.

Then in the app: **Admin → Server → Public base URL** = that `https://…ts.net:8640` address, so
invite links point at it. Auth cookies are always `Secure`: signing in over `http://munchlax:8640`
deliberately does not stick (the login page says so). The LAN port stays open only so
pokeflute's probe reaches `/api/ping`.

**Still to verify (spec task 5.3):** that a guest who reaches munchlax through a Tailscale *node
share* can open the `tailscale serve` HTTPS name. Do this with one real guest before inviting anyone.

## Moving an existing instance to another host

Copy the runtime data rather than re-importing: it preserves trims, exclusions and the
derived index, and costs Garmin nothing. From the source host, with the app running:

```bash
# consistent snapshot - NOT a copy of the live .db file
python -c "import sqlite3; sqlite3.connect(r'~/fitmon/data/fitmon.db').execute('VACUUM INTO ?', ('/tmp/snap.db',))"
scp /tmp/snap.db munchlax:~/fitmon/data/fitmon.db
tar czf /tmp/users.tgz -C ~/fitmon users/ && scp /tmp/users.tgz munchlax:/tmp/
ssh munchlax 'cd ~/fitmon && tar xzf /tmp/users.tgz'
```

**The Garmin token will not survive the copy, and should not.** `users/<id>/garmin/tokens.enc`
is encrypted with `auth/garmin_token_key`, which is per-host and generated on first use. Copying
the token without the key leaves it undecryptable and the first sync stops with
`login_required` — which is the encryption doing its job. Log in again on the new host:

```bash
ssh -t munchlax
cd ~/projects/fitparse && /opt/homebrew/bin/uv run fitmon-sync login -u mike
```

Then remove the old host's token so two machines are not syncing the same account: Garmin
rate-limits by network, not by host (munchlax hit a 429 on its very first login attempt).

## Getting the data in

```bash
# the hand-assembled archive, once (run on a box that can see D:\fit, or copy it over first)
uv run fitmon-import -u mike -d D:/fit

# Garmin Connect: connect once, then the nightly timer keeps it current
ssh -t munchlax 'cd ~/projects/fitparse && /opt/homebrew/bin/uv run fitmon-sync login -u mike'
ssh munchlax    'cd ~/projects/fitparse && /opt/homebrew/bin/uv run fitmon-sync run -u mike -n 5 -v'   # smoke test
```

## Inviting someone

1. Share the munchlax node with their Tailscale account (Tailscale admin console → Machines → Share).
2. Admin tab → *Create invite link* → send it. Valid 7 days, single use.
3. They upload files or a Garmin export zip. Connecting Garmin from the browser is **off** until
   you switch it on for them in Admin → Users.

## Routine

| Task | Command |
|---|---|
| Deploy | `git push`, then `deploy/deploy.sh` |
| Restart worker | `ssh -t munchlax 'sudo launchctl kickstart -k system/com.fitmon.worker'` |
| Forgotten password | `ssh -t munchlax 'cd ~/projects/fitparse && /opt/homebrew/bin/uv run fitmon-admin reset-password -u NAME'` |
| Who is connected to Garmin | `uv run fitmon-sync status` |
| What happened | `~/fitmon/logs/events.jsonl` (also Admin → Events) |
