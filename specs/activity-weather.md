# Fitmon: weather for every activity (Open-Meteo)

Status: **specced 2026-10-16, not started.** Mike asked for it to go on the backlog.

## 1. Why

Mike's current watch (Garmin product 4312, since May 2024) writes **no temperature into run
files**: 0 of 156 runs have one, summer or winter. It only writes one for open-water swims,
triathlon bike legs and (since 11 May 2026) strength sessions. The watch before it (3589) wrote
one into everything, but that was the wrist sensor, which reads several degrees warm from
skin heat. **No FIT file holds humidity.** Investigated 2026-10-16 on munchlax's data.

The watch's *Heat Adaptation* figure comes, as far as is known, from weather looked up for
where you are (via the phone), not from that sensor. Garmin Connect stores that weather on its
own servers, not in the file.

So fitmon should look the weather up itself: temperature, humidity, dew point, feels-like,
wind, rain, cloud, for the time and place of each activity. That answers "was it hot?" for
every run, and it lets Trends show what heat does to pace and heart rate.

## 2. Source: Open-Meteo, not Garmin Connect (decided with Mike, 2026-10-16)

| | Open-Meteo | Garmin Connect weather |
|---|---|---|
| Covers | every GPS activity, however it arrived | only activities synced from Garmin (it needs a Garmin activity id) |
| Access | free for non-commercial use, no key, ~10,000 requests a day; data CC BY 4.0 | the unofficial `garminconnect` API (`get_activity_weather`), which may break, and uses Garmin's request budget |
| Values | a weather model's grid cell (~1–2 km for recent years, ~25 km for ERA5 back to 1940) | a nearby station; what Garmin Connect shows |
| Cost of a back-fill | one request covers a place over a date range | one request per activity |

The trade-off: the values will not match Garmin Connect to the degree. For "how did heat affect
this run", that does not matter.

## 3. What is fetched

- **Where:** the activity's start point, rounded to 0.01° (about 1 km). Never the route, so no
  more of the home address than the grid already blurs. For rides over 2 h, also the finish
  point at the finish hour.
- **When:** the hours the activity covered (UTC), interpolated to its middle, plus the start
  and end hours for long ones.
- **Fields (hourly):** `temperature_2m`, `relative_humidity_2m`, `dew_point_2m`,
  `apparent_temperature`, `precipitation`, `cloud_cover`, `wind_speed_10m`,
  `wind_direction_10m`, `weather_code`.
- **Indoor activities** (treadmill, indoor cycling, pool, strength) get none: they have no GPS
  start, and the weather outside is not what they were done in.

### Endpoints (check while building; the request shapes are from memory)
- Back-fill: the historical (ERA5) archive, `archive-api.open-meteo.com/v1/archive`, with
  `start_date` / `end_date`. It lags real time by about five days.
- Recent days: the forecast API's `past_days`, or the historical-forecast API, which keeps the
  high-resolution models from about 2022 on. Pick whichever gives the best resolution for a
  given date. Record which one each value came from.
- Batch: group activities by rounded location and month, so one request returns hourly data for
  many activities. Mike's ~520 GPS activities should take tens of requests.

## 4. Storage

A rebuildable index row per activity, like the rest. It is keyed by `(file_id, idx)`, because a
re-index gives sessions new ids (the rule in CLAUDE.md).

| Table `session_weather` | |
|---|---|
| file_id, idx, user_id | key, owner |
| temp_c, apparent_c, humidity_pct, dew_point_c, wind_ms, wind_dir_deg, precip_mm, cloud_pct, weather_code | at the activity's midpoint |
| temp_start_c, temp_end_c | for long activities |
| source | `era5` / `forecast` / … , and the grid resolution |
| fetched_at | |

The raw hourly responses are cached on disk (`~/fitmon/cache/weather/`, keyed by rounded
location and date). A schema change or a lost DB is then rebuilt without asking Open-Meteo
again. The cache is not user data; it can be deleted any time.

## 5. When it runs

- **New activities:** the job worker, after an import or a sync adds GPS activities. This is
  network I/O, so it never runs in a request. The worker does not reload on munchlax, so it
  needs a restart once after deploying.
- **Back-fill:** a `weather` job, queued from the Import tab ("Fetch weather for past
  activities") and resumable. It does activities without a row, newest first, and respects a
  daily request cap (global setting, default 2,000).
- **Failure:** a row stays missing and the next run retries it. No error reaches the activity
  page; the tile just says "weather not fetched yet".

## 6. UI

- **Activity page:** a *Weather* tile, e.g. "18 °C, feels 17 · 64 % humidity · dew point 11 °C ·
  wind 3 m/s from W · light rain". It sits next to the watch's own temperature where there is
  one, labelled "wrist" vs "air". Times follow the 24-hour rule in the global CLAUDE.md.
- **Activities table:** optional columns: air temperature, dew point.
- **Trends:** heat vs performance. Pace (or power) per heartbeat against dew point or
  feels-like temperature, runs only, and the running-economy chart coloured by temperature.
  Dew point is the usual measure of how muggy a run felt.
- **Attribution:** "Weather data by Open-Meteo.com (CC BY 4.0)" under the tile and in About.
- Statute units: °F, mph.

## 7. Privacy and politeness

- Only the rounded start (and finish) point and the date leave the server: no route, no user
  name, no heart rate.
- A `User-Agent` naming fitmon, gentle request pacing, and the on-disk cache, so nothing is
  fetched twice.
- Non-commercial use only. If fitmon ever stopped being a private family app, this would need
  Open-Meteo's commercial terms.

## 8. Tasks

| # | Task | Status |
|---|---|---|
| W.1 | Verify the Open-Meteo endpoints, fields, ERA5 lag and the recent-days option. Record findings in `docs/open-meteo-research.md` | ☐ |
| W.2 | `app/weather/client.py` (the only module that talks to Open-Meteo) + disk cache; tests against recorded replies | ☐ |
| W.3 | `session_weather` table; midpoint interpolation; indoor and no-GPS skipped; tests | ☐ |
| W.4 | Worker: after imports and syncs; `weather` back-fill job with a daily cap; Import-tab button and progress | ☐ |
| W.5 | Activity-page Weather tile (wrist vs air), attribution, statute units | ☐ |
| W.6 | Activities table columns; Trends: heat vs efficiency (dew point / feels-like) | ☐ |
| W.7 | Back-fill Mike's activities on munchlax; restart the worker; check a few against Garmin Connect's weather | ☐ |
| W.8 | Docs: CLAUDE.md rule, deploy doc (outbound HTTPS to open-meteo.com), About | ☐ |

## 9. Open questions
- Should the back-fill also cover activities from before GPS, or indoor ones, using the home
  location? Proposed: no, it would be guessing.
- Should Trends adjust pace for heat? A known rule of thumb is about 1–2 % per 5 °C of dew point
  above about 10 °C. Proposed: show the relationship first; adjust later, if at all.
