# Deploying fitmon to munchlax

Follows the fleet guide (`D:\hw\pokeflute\docs\deploying-a-new-munchlax-service.md`); this file
only records what is specific to fitmon. Port **8640** is reserved in pokeflute's `ports.json`.

Status: **not yet deployed** — everything below is prepared in `deploy/` but has not been run.

## What runs

| Unit | Script | Restart needed after a deploy? |
|---|---|---|
| `com.fitmon.web` | `deploy/run-fitmon-web.sh` → `fitmon-web` (Flask, reloader on, **debugger off**) | no — the reloader picks up `git pull` |
| `com.fitmon.worker` | `deploy/run-fitmon-worker.sh` → `fitmon-worker` | **yes**, when parsing / import / sync code changed |
| `com.fitmon.sync` (04:10) | `deploy/run-fitmon-sync.sh` → `fitmon-sync run --queue --health` | – |
| `com.fitmon.backup` (04:50) | `deploy/run-fitmon-backup.sh` → snapshot + rsync to snorlax | – |

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

```bash
ssh -t munchlax 'sudo tailscale serve --bg 8640'      # https://munchlax.<tailnet>.ts.net -> localhost:8640
ssh munchlax 'tailscale serve status'
```

Then in the app: **Admin → Server → Public base URL** = that `https://…ts.net` address, so invite
links point at it. Auth cookies are always `Secure`: signing in over `http://munchlax:8640`
deliberately does not stick (the login page says so). The LAN port stays open only so
pokeflute's probe reaches `/api/ping`.

**Still to verify (spec task 5.3):** that a guest who reaches munchlax through a Tailscale *node
share* can open the `tailscale serve` HTTPS name. Do this with one real guest before inviting anyone.

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
