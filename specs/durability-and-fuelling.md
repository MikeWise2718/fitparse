# Fitmon: durability and fuelling

Status: **specced 2026-10-20, not started.**

## 1. Why

Mike's 70.3 (21 June 2026) took 7 h 35 min:

| Leg | Start (CEST) | Time | Avg HR |
|---|---|---|---|
| swim, 1.9 km | 07:26 | 41 min | 138 |
| T1 | 08:07 | 14 min | |
| bike, 91.2 km | 08:21 | 3 h 21 min | 134 |
| T2 | 11:42 | 8 min | |
| run, 21.3 km | 11:50 | 3 h 10 min | 131 |

He was fit (CTL around 50). His capacity collapsed after about 5 hours, which was about 35 minutes
into the run. The run in 15-minute pieces:

| From minute | Pace /km | Walking | HR | Cadence |
|---|---|---|---|---|
| 0 | 6:46 | 0 % | 138 | 170 |
| 30 | 8:11 | 5 % | 135 | 163 |
| 60 | 9:01 | 16 % | 134 | 157 |
| 90 | 9:29 | 24 % | 129 | 151 |
| 135 | 10:08 | 34 % | 123 | 151 |
| 165 | 9:58 | 46 % | 127 | 147 |

**Heart rate fell while pace collapsed.** That is the pattern of muscles or fuel giving out, not of
heat or the cardiovascular system, where heart rate drifts up at the same pace.

**The build-up had no long days.** From March to the race: 78 rides and runs, one over 3 hours
(3 h 24 min), none over 4. The race was more than twice his longest training day. CTL averages
daily load, so it could not show this.

Two things were missing: **training for duration**, and **fuelling practice**. fitmon tracks
neither. This spec adds both.

## 2. Durability tracking (fitmon only)

### 2.1 Long sessions
- **Per week:** the longest session, and the time spent in sessions longer than 2, 3 and 4 hours,
  per sport and in total. A chart in Trends, with an optional race marker (date and expected
  duration) to show how far the long days still are from the race.
- **Brick sessions:** a run that starts within 30 minutes of the end of a ride, in one file or two.
  Shown as one unit, with its total duration.
- **Readiness hint** (when a race is set): the longest session or brick in the last 8 weeks, as a
  share of the expected race duration.

### 2.2 How a long session holds up
For sessions over 90 minutes, per hour (or per 30 minutes):
- pace (run) or speed (bike) at the heart rate actually held, i.e. efficiency per heartbeat;
- heart rate, cadence, and the share of time walking (run) or freewheeling (bike);
- **the fade**: efficiency in the last hour against the second hour, in %. The first hour is left
  out because of the warm-up.

Shown on the activity page as a small table, the same shape as section 1. In Trends: the fade
against session length, for runs and rides.

## 3. Fuelling record

### 3.1 The entry (works on any watch)
Per activity, filled in afterwards: **carbohydrate (g), fluid (ml), sodium (mg, optional), and a
note** ("4 gels + 2 × 750 ml drink mix"). fitmon works out **grams of carbohydrate per hour** and
fluid per hour, and shows them next to the fade (2.2). The entry is a per-activity correction, so
it is keyed by `(file_id, idx)` like `SessionTrim`, and survives a re-index.

A small list of the gels and drinks he uses, with their carbohydrate and sodium, so that "2 gels +
1 bottle" can fill in the grams.

### 3.2 Lap presses as fuel marks (no code on the watch)
On the watch, a separate activity profile for long sessions (e.g. "Long run", "Long ride") with
**auto-lap off**. **Press Lap at every gel or drink.** fitmon reads the lap times as fuel marks:
- which activities count: a per-user setting listing activity profile names. The FIT `sport` message
  carries the profile's name; **check that Mike's files hold it** (W1 below). Failing that, a
  per-activity switch "laps are fuel marks".
- shown as markers on the time-series graphs and the map, with their times; the entry (3.1)
  then only needs "what was each mark": all gels, or a mix.

Limit: a lap cannot say whether it was a gel or a drink.

### 3.3 Connect IQ fuel counter (later, if the watch allows it)
A Connect IQ **data field** (one of the activity's data screens, inside Garmin's own recording, so
the triathlon mode, running dynamics and training effect stay). It writes the counts into the
activity file as **developer fields** (`FitContributor`), which fitparse already reads:
- tap once for a gel, twice for a drink; a running count on the screen;
- in the file: per-record counters, and per-session totals.

**The open question is input.** A data field gets no button events. It can get `onTap()`,
documented on Edge bike computers and reported to work on recent watches, but not documented
there. It can also see the Lap button (`onTimerLap`), but the watch then records a real lap, which
3.2 covers without code. Hence step C1: a throwaway test field, side-loaded over USB, that counts
taps during an activity on Mike's Epix Pro (Gen 2).

Other facts to keep in view:
- Connect IQ data fields are limited per activity (a developer reports **two** at a time; not in
  Garmin's documentation). One field can record any number of entries and several values.
- Developer fields reach the FIT file, but Garmin Connect and third-party services may not show
  them. That does not matter here, because fitmon reads the file.
- Writing it needs the free Connect IQ SDK (Monkey C) and Java; the build is side-loaded into
  `GARMIN/APPS` over USB, with no store listing.
- A full Connect IQ **app that records the activity itself** would get the buttons, but would
  replace Garmin's recording and lose the triathlon mode, structured workouts and running dynamics.
  **Not proposed.**

### 3.4 Why not Garmin's own nutrition logging
Checked 2026-10-20:
- Garmin's food logging (January 2026) is a paid **Connect+** feature (about $69 a year). It is
  native on the newer watches only (Fenix 8, Forerunner 570/970, Venu 4 and others); the
  **Epix Pro (Gen 2) is not on the list**.
- Even there, food is logged at a time of day, **not to an activity**, with no export or API.
- Fluid drunk during an activity can, according to a Garmin forum thread, be added afterwards in
  Garmin Connect; it stays there.
- The FIT profile (21.218) has no message for food or drink intake.

Sources: [BikeRadar](https://www.bikeradar.com/news/garmin-nutrition-tracking),
[DC Rainmaker](https://www.dcrainmaker.com/2026/01/garmin-connect-nutrition-logging-connect.html),
[Garmin Forums, fluid consumed](https://forums.garmin.com/outdoor-recreation/outdoor-recreation/f/fenix-8-series/382523/est-sweat-loss-fluid-consumed),
[Garmin Forums, datafield touch](https://forums.garmin.com/developer/connect-iq/f/discussion/404827/datafield-touch),
[Garmin Connect IQ, activity recording](https://developer.garmin.com/connect-iq/core-topics/activity-recording).

## 4. Fuelling against fading (the point of it all)
Once a few long sessions have entries: **grams per hour against the fade** (2.2), on a chart in
Trends, with heat once the weather work (`specs/activity-weather.md`) is in. The usual guidance
for races of this length is 60–90 g of carbohydrate per hour, which the gut has to be trained to
take; the chart shows what works for him.

## 5. Tasks

| # | Task | Status |
|---|---|---|
| D1 | Long-session stats per week (longest, time over 2/3/4 h); Trends chart; optional race marker | ☐ |
| D2 | Brick detection (run within 30 min after a ride) | ☐ |
| D3 | Per-hour hold-up table on the activity page; the fade figure; fade against length in Trends | ☐ |
| F1 | Fuelling entry per activity (`(file_id, idx)`), g/h and ml/h; list of his gels and drinks | ☐ |
| W1 | Check that the FIT `sport` message in Mike's files carries the activity profile name | ☐ |
| F2 | Lap presses as fuel marks (profile-name setting, or per-activity switch); markers on graphs and map | ☐ |
| F3 | Mike: a "Long run" / "Long ride" profile with auto-lap off and eat/drink alerts; try 3.2 on a long session | ☐ Mike |
| C1 | Connect IQ tap test: a throwaway data field that counts taps during an activity on the Epix Pro (Gen 2) | ☐ |
| C2 | If C1 works: the fuel-counter data field, writing developer fields; fitmon reads them into F1 | ☐ |
| A1 | Grams per hour against the fade, in Trends | ☐ |

Order: F1 and F3 first (usable on the next long session), then D1 to D3, then F2, then C1/C2.

## 6. Open questions
- What does "the fade" compare for rides without a power meter? Speed per heartbeat is affected
  by hills and wind; maybe compare only flat stretches.
- Should a brick spanning two files count as one session in the weekly totals?
- These are general training principles, not personal medical advice; the app shows data and
  rules of thumb, it does not prescribe.
