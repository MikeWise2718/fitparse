# Deploying fitmon to munchlax

Follows the fleet guide (`D:\hw\pokeflute\docs\deploying-a-new-munchlax-service.md`); this file
only records what is specific to fitmon. Two ports are reserved in pokeflute's `ports.json`:
**8640** for the app and **8641** for its HTTPS front (`fitmon-https`).

Status: **deployed 2026-09-21.** Live at **https://munchlax.taild34695.ts.net:8641** (tailnet
only; it was `:8640` until 2026-09-23 — see "HTTPS front door" for why it moved). All four units run as system LaunchDaemons: web, worker, nightly sync, nightly backup.

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

`tailscale` is **not on the PATH** on munchlax; the binary is inside the app bundle. It needs
no sudo:

```bash
ssh -t munchlax
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg --https 8641 8640
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve status
```

**Use an explicit HTTPS port, not the root path.** munchlax already serves klefki on `/` and
ytsum on `:8443`; taking the root would have broken klefki. `ProxyFix` reads Tailscale's
`X-Forwarded-Proto`, so the app knows the connection is secure — verify by POSTing to
`/api/auth/login` through the HTTPS name: a **401** (wrong password) means it works, a **400**
means the app still thinks it is plain HTTP.

**The front must never share the app's port number.** It used to: `serve --https 8640 8640`,
chosen "to match its own port". That only works if fitmon binds its wildcard address *before*
Tailscale binds the tailnet ones — macOS allows specific-after-wildcard, not the reverse. For
50 days of uptime fitmon happened to win; the 2026-09-23 reboot started Tailscale first, and
fitmon's web daemon crash-looped on `Address already in use` until the front was moved. The
same sharing had already caused a false DOWN on the landing page (a plain-HTTP probe of
`munchlax:8640` reaching the HTTPS listener). ytsum is the pattern: front `:8443`, app `:5010`.

To recover if this ever recurs: `serve --https=8640 off`, wait for the web daemon's KeepAlive
to bind 8640 (seconds), then add the front back on 8641.

Then in the app: **Admin → Server → Public base URL** = that `https://…ts.net:8641` address, so
invite links point at it. Auth cookies are always `Secure`: signing in over `http://munchlax:8640`
deliberately does not stick (the login page says so).

**Both the card link and the health probe name the HTTPS address**, so they test the door
people actually use. `deploy/pokeflute-service.json` pins both:

```json
"health_url":    "https://munchlax.taild34695.ts.net:8641/api/ping",
"url_tailscale": "https://munchlax.taild34695.ts.net:8641"
```

It also carries `"category": "Apps"` for the landing page. `deploy.sh` copies this file over
the live registry entry, so a category set by hand on munchlax is lost on the next deploy — it
has to live here.

Without `url_tailscale`, landing and pokeflute derive the tailnet link by swapping the host into
`url` and keep its `http://`, producing a link that cannot load. Support for that field is
landing >= 0.10.1 and pokeflute >= 0.25.2.

**Still to verify (spec task 5.3):** that a guest who reaches munchlax through a Tailscale *node
share* can open the `tailscale serve` HTTPS name. Do this with one real guest before inviting anyone.

## The snorlax mount, for backups

`~/snorlax-homes` is the fleet's SMB mount point on munchlax and **homeseg owns the tooling**:
`~/scripts/snorlax-mount-retry.sh` (credentials in `~/.snorlax-smb-password`, log in
`~/homeseg/logs/snorlax-mount.log`), plus `com.mike.snorlax-mount` to mount it at boot.

**The mount drops silently.** In September 2026 it was found unmounted since 4 August — seven
weeks — with nothing noticing. So the backup script calls that same retry script when the mount
is missing, rather than duplicating the credential handling, and only gives up if that fails.
Verified by unmounting and running the backup: it remounted and completed.

Check it: `ssh munchlax 'mount | grep snorlax'`, and the backups themselves at
`~/snorlax-homes/mike/backups/fitmon/` — seven rotating DB snapshots plus an rsync of the FIT
originals. Garmin tokens are excluded by design.

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
