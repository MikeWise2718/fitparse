# Fitmon: route library and route planner (design proposal)

Status: **proposal, 2026-10-16, not yet approved.** Nothing is built. Sections 9 and 10 are
the decisions Mike needs to make before work starts.

Mike's request (2026-10-16): *plan routes of a particular length, usually by modifying,
lengthening or shortening a route I have tried already*, and keep a **library of running and
cycling routes** to make that manageable.

## 1. The idea in one picture

```
   your past activities            the route library               the planner
  ┌────────────────────┐        ┌──────────────────────┐       ┌──────────────────────────┐
  │ 7 May 10.0 km run  │──save─▶│ "Langen Wald loop"   │─edit─▶│ "...loop, 12 km"         │
  │ 2 Jun 10.1 km run  │  ▲     │  10.0 km · run · ×23 │       │  target 12.0 km          │
  │ 9 Jun  9.9 km run  │──┘     │ "Main gravel 60"     │       │  → 3 suggestions         │
  │ ...                │ match  │  61 km · gravel · ×4 │◀─save─│  → pick, tweak, save     │
  └────────────────────┘        └──────────────────────┘       └──────────────────────────┘
                                          │
                                          └── export GPX / FIT course → watch
```

- **Routes come mostly from what you have already done.** Any GPS activity becomes a route with
  one click, and fitmon offers the routes you repeat often ("you have run this 23 times").
- **Each route keeps its history:** which activities followed it, best and last time, and how your
  pace or heart rate on it changed. That is useful in its own right, a fair test of fitness
  on the same ground.
- **The planner changes a route's length** to a target you set. It offers a few alternatives, drawn
  as faint lines on the map with their lengths, and you pick one. It also does ordinary
  drag-to-reroute editing.

## 2. Routing engine: BRouter, run on munchlax (recommended)

Lengthening or shortening a route needs a road and path network to route over. Options:

| Engine | Where it runs | For | Against |
|---|---|---|---|
| **BRouter** | self-hosted (Java), one process | Built for bikes and walking/running. Profiles for trekking, gravel, MTB, road and foot. Returns elevation and surface for each segment. No key and no limits. Routes never leave the house. | Needs Java, plus map tiles downloaded and refreshed now and then (§2.1) |
| openrouteservice (public API) | their servers | Has a "round trip of N km" mode built in | API key, 2,000 requests a day, and every route (that is, where you live) goes to a third party |
| GraphHopper (self-hosted) | self-hosted (Java) | Fast, good profiles | Heavier to set up: it builds its own graph from an OSM extract and needs a lot of memory for it |
| OSRM | self-hosted | Very fast | Made for cars; its foot and bike profiles are weak, and every profile needs its own preprocessing |

**Recommendation: BRouter.** It is the standard for this kind of planning (brouter-web and
several Garmin tools use it). It lets the planner make dozens of routing calls for each suggestion
cheaply, and it keeps home locations private, in line with the rest of fitmon.

### 2.1 Running it
- BRouter server on munchlax, on a port **claimed in pokeflute's registry first** (Flask
  conventions). LAN-only, bound to `127.0.0.1`, since only fitmon calls it.
- Map data: BRouter `.rd5` segment tiles, 5°×5° each. **Langen (≈ 50.0° N, 8.7° E) sits on a
  tile boundary**, so it needs `E5_N45` and `E5_N50`. Together that is a few hundred MB, kept in
  `~/brouter/segments4/`, refreshed monthly by a launchd job. Wider trips can add tiles later.
- `app/routing/brouter.py` is the only code that talks to it (the same rule as `garminconnect`),
  so the engine can be swapped.
- Fitmon profiles: **run** (prefers paths and tracks, avoids main roads), **road bike**, **gravel**
  (prefers unpaved), mapped onto BRouter's shipped profiles and lightly tuned.

## 3. The route library

### 3.1 What a route is
- Name, sport (run / ride / gravel / MTB / hike), **geometry** (a line with elevation), the
  **control points** it was planned with (for editing), distance, climb, % unpaved, loop or A→B,
  notes and tags.
- Where it came from: an activity (`session` file_id+idx), drawn by hand, or derived from another
  route (a **parent** link). "Wald loop 12 km" stays tied to "Wald loop" as a variant.
- **Storage follows the fitmon rule that the DB is a rebuildable index:** each route is a GeoJSON
  file in `~/fitmon/users/<id>/routes/<uuid>.geojson`, the original. The `routes` table
  indexes it. A re-index rebuilds the table from the files, so routes survive a `SCHEMA_VERSION`
  bump.
- Private to its owner, like everything else: `auth.scoped(Route)`, and new routes are added to the
  cross-user test.

### 3.2 Matching activities to routes
- An activity **follows** a route if, after resampling both every 25 m, ≥ 90% of the activity's
  points lie within 30 m of the route, and ≥ 90% of the route lies within 30 m of the activity.
  Reverse direction counts too, and is shown as "↺ reversed".
- Matching runs in the job worker on import, and when a route is saved (against all past GPS
  activities of that sport). The result is a `route_matches` table (route_id, file_id, idx,
  direction) that is rebuildable.
- **Suggested routes:** a weekly job clusters GPS activities that match each other and offers any
  cluster of ≥ 3 not yet in the library ("Save as route?" on the Routes tab). Nothing is
  saved without a click.

### 3.3 Routes tab

```
 Fitmon v0.6.0   Dashboard  Activities  Trends  Routes  Body  Gear  Explorer  ...    ⟳ Sync
┌────────────────────────────────────────────────────────────────────────────────────────────┐
│ Routes                       [ Run ▾ ] [ all lengths ▾ ]  search: [_________]  [+ New route] │
├───────────────────────────────────────────┬────────────────────────────────────────────────┤
│  ★ Langen Wald loop        10.0 km   ×23  │                                                │
│    run · loop · 45 m ↑ · last 7 May       │        ╭──────╮                                │
│      └ Wald loop 12 km     12.1 km    ×2  │     ╭──╯      ╰───╮       (all routes shown,   │
│      └ Wald loop short      7.9 km    ×0  │     │   ●start    │        the selected one    │
│    Egelsbach out & back     8.4 km    ×6  │     ╰──╮       ╭──╯        drawn bold)         │
│    run · A→B→A · 12 m ↑ · last 2 Jun      │        ╰───────╯                               │
│    Main gravel 60          61.3 km    ×4  │                                                │
│    gravel · loop · 38% unpaved · 210 m ↑  │                                                │
├───────────────────────────────────────────┴────────────────────────────────────────────────┤
│ Suggested from your activities:  ◌ 6.2 km run you did 5 times (last 3 Oct)   [Save] [Hide]  │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

Variants indent under their parent. ×N is how many activities followed the route.

### 3.4 Route page

```
┌────────────────────────────────────────────────────────────────────────────────────────────┐
│ ← Routes   Langen Wald loop   ✎         run · 10.0 km · 45 m ↑ · 12% unpaved · loop        │
│ [ Edit in planner ]  [ Make it … km ]  [ GPX ⤓ ]  [ FIT course ⤓ ]  [ ⋯ ]                   │
├──────────────────────────────────────────────┬─────────────────────────────────────────────┤
│                 map, route + elevation       │ Times on this route (23)                    │
│                                              │   pace   5:48 ┤      •                      │
│   ▁▂▃▅▆▅▃▂▁▁▂▃▄▃▂▁   elevation profile       │          5:30 ┤  •  • ••   •                │
│                                              │          5:12 ┤        •  •• • •  •••       │
│                                              │               └───────────────────────      │
│                                              │                2024       2025      2026    │
│                                              │ best 52:10 (12 Apr 2026) · last 54:03       │
├──────────────────────────────────────────────┴─────────────────────────────────────────────┤
│ Activities on this route:  7 May 2026 54:03 · 148 bpm   │  2 Jun 2026 55:40 …  (open each)  │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

From an activity page, a new **"Save as route"** action is added next to ✂ Trim and Change sport.
If the activity already matches routes, a line shows them ("On route: Langen Wald loop").

## 4. The planner

### 4.1 Layout

```
┌────────────────────────────────────────────────────────────────────────────────────────────┐
│ Planner: Langen Wald loop (variant)       profile [ Run ▾ ]    length 10.0 km → [ 12.0 ] km │
│ [ Suggest ]  [ ↶ Undo ] [ ↷ ]   [ Reverse ]  [ Close loop ]      [ Save as new ] [ Save ]   │
├────────────────────────────────────────────────────────┬───────────────────────────────────┤
│                                                        │ Suggestions for 12.0 km           │
│            ┌ ─ ─ ─ ┐  B: +1.9 km                       │                                   │
│            ╎       ╎                                   │ ● A  11.9 km  extend at the far   │
│        ╭───┴───────┴────╮                              │      end, a 1 km lollipop · 86%   │
│        │                ╰─╮ ┄┄┄┄┄┄┄┐  A: +1.9 km       │      on ground you know           │
│   ●────╯   (current: bold) ├┄┄┄┄┄┄┄┘                   │ ○ B  12.1 km  wider through the   │
│   start                  ╭─╯                           │      north woods · 71% known      │
│        ╰─────────────────╯   · · · · · C: +2.0 km      │ ○ C  12.0 km  extra lap of the    │
│                               ·  (lap) ·               │      pond loop · 100% known       │
│                               · · · · ·                │                                   │
│                                                        │ [ Use A ]   ±2% tolerance [ 2 ]%  │
├────────────────────────────────────────────────────────┴───────────────────────────────────┤
│ ▁▂▃▅▆▅▃▂▁▁▂▃▄▃▂▁▁▁▂▂   elevation · 52 m ↑ · 14% unpaved              12.0 km  (+2.0 km)  │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

- **Bold line:** the current route. **Faint dashed lines:** the suggestions, each with its own
  colour and a label. Hover a list entry to highlight its line, click to preview, "Use" to take it.
- Everything is editable by hand as well:
  - drag the line to add a via point;
  - drag a via point to move it;
  - click a via point to delete it;
  - click the map to append a point (for A→B routes).

  Each change re-routes only the two segments next to it.
- Undo/redo for every step. The length readout shows the difference from the target as you edit.
- "Known ground" means the share of the suggestion that lies on paths you have run or ridden
  before. Many people prefer that, so it is shown and used to rank the suggestions. A setting can
  favour new ground instead.

### 4.2 How lengthening works

Every option keeps your start and finish. Each one searches over a single knob until the total
comes within tolerance of the target (bisection, ~6 routing calls). Then the candidates are ranked.

```
 1. Push out a segment            2. Lollipop at the far end      3. Add a known loop
    (one new via point,              (out and back to a new          (a route in your library
     moved outward)                   point, then the loop)           that touches this one)

       ·  ·  ·  · ◆                      ◆                            ╭─────╮
      ·            ·                     ┆                            │ +2km│
   ●─────────────────●                   ┆                     ●──────┴─────┴──────●
   │     original    │            ●──────┴──────●              │                   │
   ●─────────────────●            │             │              ●───────────────────●
                                  ●─────────────●
   knob: how far ◆ moves         knob: how far ◆ is            knob: which loop, how many laps
   out from the segment          from the turning point
```

1. **Push out a segment:** for each of ~8 evenly spaced segments, put a via point at its middle
   and move it outward, at right angles to the route, until the length fits. Most of the
   suggestions come from this.
2. **Lollipop:** add an out-and-back from the farthest point of the route. The extra ground is
   all new, and it is the simplest change to remember while running.
3. **Add a known loop:** if another route in your library starts or passes within 100 m, add one
   or more laps of it. All of it is ground you know.

### 4.3 How shortening works

```
 4. Shortcut                               5. Turn back early (A→B→A routes)

   ●─────────────────────╮                  ●══════════════════◆┄┄┄┄┄┄┄┄┄┄○
   │                     │                  start          turn here    old turn
   ●┄┄┄┄┄ new ┄┄┄┄┄┄┄┄┄┄┄●
   │       shortcut      │                  knob: where to turn
   ●─────────────────────╯
   knob: which pair of points to join
```

4. **Shortcut:** look for pairs of points that are close as the crow flies but far apart along the
   route, and route straight between them. Pick the pairs whose saving is closest to what is
   needed. A loop stays a loop.
5. **Turn back early** (out-and-backs): move the turning point back along the route. The length
   comes exactly right with no new routing.
6. **Pull in a segment:** the reverse of 1. The via point moves inward.

The planner offers 3–5 candidates from different methods, ordered by: within tolerance → least
new ground (or most, per the setting) → least extra climb → no main roads. Reasonable limits:
lengthening by up to ~50%, shortening down to ~50%. Beyond that, start from a different route.

### 4.4 Starting from nothing
"New route" with a start point and a target: "10 km loop from here". The planner places 3–4
via points on a rough circle with a circumference of the target length, routes it, and then
treats it like any other route (methods 1 and 6 to fit the length). With the same rotation
knob as in brouter-web, "another one" gives a different loop.

## 5. Export to the watch
- **GPX** (track + route points) is downloadable; Garmin Connect and every other tool imports it.
- **FIT course**, so the watch gives turn-by-turn and the course's elevation: this needs a FIT
  *writer*. fitparse only reads, and writing FIT is listed as out of scope in the main spec, so
  this needs your decision (§9). The usual route is the `fit-tool` package, kept in one module.
- Getting it onto the watch: download it and copy it to `GARMIN/NewFiles` over USB, or import the
  GPX in Garmin Connect. Sending it to Garmin Connect automatically would write to Garmin, which the
  main spec also rules out. Not proposed.

## 6. Data model additions (SCHEMA_VERSION 2; re-index rebuilds them from the files)

| Table | Columns |
|---|---|
| `routes` | id, user_id, uuid (file name), name, sport, kind (loop / a_b / out_back), distance_m, ascent_m, unpaved_pct, start_lat/lon, parent_id, source (activity / drawn / planned), source_file_id, source_idx, created, updated |
| `route_matches` | route_id, file_id, idx, direction (fwd / rev), match_pct, user_id |
| `route_suggestions` | user_id, cluster key, n_activities, sample file_id+idx, hidden |

The GeoJSON file holds the full line (lon, lat, ele), the control points with the profile used
for each leg, name, notes and tags. That way the file alone can rebuild its row.

## 7. API (all JSON, all scoped, writes need CSRF)

```
GET    /api/routes                       list (filters: sport, length range, q)
POST   /api/routes                       create (from activity, from planner result, drawn)
GET    /api/routes/<id>                  route + matches + stats
PATCH  /api/routes/<id>                  rename, notes, tags, new geometry
DELETE /api/routes/<id>
GET    /api/routes/<id>.gpx              export   ·   GET /api/routes/<id>.fit  (if §9.2 = yes)
POST   /api/routes/from-session/<id>     save activity as route (snaps + simplifies the GPS line)
POST   /api/plan/segment                 route between control points (one leg) → line + stats
POST   /api/plan/fit-length              {route, target_m, tolerance, prefer} → candidates[]
POST   /api/plan/loop                    {start, target_m, sport, rotation} → candidate
GET    /api/routes/suggestions           ·  POST /api/routes/suggestions/<key>/hide
```

`fit-length` makes ~30–60 BRouter calls. Locally that is a second or two, so it runs **in the
request** with a hard time limit (8 s) and returns whatever candidates are done by then. It is
interactive, like the single-file re-index, and not bulk work. The matching and clustering jobs
go to the worker.

## 8. Phases and task tracker

| # | Task | Phase | Status |
|---|---|---|---|
| R.0 | Decisions in §9 | 0 | ☐ waiting for Mike |
| R.1 | Claim BRouter port; Java + BRouter server + `E5_N45`/`E5_N50` tiles on munchlax; launchd service + monthly tile refresh; doc in `docs/fitmon-deploy.md` | 1 Engine | ☐ |
| R.2 | `app/routing/brouter.py` client + fitmon profiles (run / road / gravel); tests against recorded responses | 1 | ☐ |
| R.3 | `routes` table + GeoJSON files + re-index; CRUD API; scoping test entries | 2 Library | ☐ |
| R.4 | "Save as route" from an activity (simplify, keep the GPS line); Routes tab list + map; route page | 2 | ☐ |
| R.5 | Activity↔route matching (worker job on import and on route save); stats and the times-on-route chart | 2 | ☐ |
| R.6 | Suggested routes (clustering repeated activities), weekly job | 2 | ☐ |
| R.7 | GPX export | 2 | ☐ |
| R.8 | Planner editor: control points, drag-to-reroute, undo/redo, elevation strip, save / save as variant | 3 Planner | ☐ |
| R.9 | `fit-length`: methods 1, 2, 4, 5, 6 + ranking; suggestion list + ghost lines | 3 | ☐ |
| R.10 | Method 3 (known loops) and the "known ground" score from the user's own tracks | 3 | ☐ |
| R.11 | New loop from a start point + target length (§4.4) | 3 | ☐ |
| R.12 | FIT course export (if §9.2 = yes) | 4 | ☐ |
| R.13 | Landing screenshot of the planner with demo data (invented route, nowhere near home) | 4 | ☐ |

Phase 2, the library, is useful without the engine. It could ship first while BRouter is being
set up.

## 9. Decisions needed from Mike
1. **BRouter on munchlax** (Java on the Mac mini, a few hundred MB of map tiles), or the public
   openrouteservice API (simpler, but routes leave the house and it needs a key)?
2. **FIT course export**: allow a FIT *writer* dependency (`fit-tool`), an exception to "the
   library is read-only"? Or is GPX enough, imported via Garmin Connect?
3. **Sharing routes between users** (e.g. with Louise): the main spec rules out sharing for now.
   Keep routes strictly private in v1?
4. **"Known ground" by default**: prefer suggestions on paths you have used before, or prefer
   new ground?

## 10. Open questions (to check during the work)
- Is a Java runtime on munchlax acceptable? Homebrew `openjdk` is the easy path, and BRouter needs
  Java 11 or later.
- How well BRouter's foot profile handles forest tracks around Langen. Compare its line with a
  few of Mike's real runs before tuning the run profile.
- Whether the ≥ 90% / 30 m match thresholds hold up on track runs and on GPS drift under trees.
  Tune them on Mike's history.
