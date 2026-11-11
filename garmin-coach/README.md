# Garmin Coach

A command-line tool that reads Garmin heart-rate, HRV and sleep data, works out daily **readiness**,
and builds a **training plan that updates itself** for your goal (5 km, 10 km, half marathon,
marathon or general fitness). Every time the plan is updated, the program writes down what
changed and why, and generates an HTML report that also works on a phone.

All data stays local (SQLite, default `~/.garmin_coach/data.db`).

## Installation

```bash
cd garmin-coach
pip install -e ".[garmin]"      # Garmin Connect sync included
# or without the Garmin dependency:  pip install -e .
```

## Quick start (demo data, no Garmin account)

```bash
garmin-coach profile --max-hr 188
garmin-coach goal --type half --date 2027-03-14 --days 4 --weekly-km 25 --long-day su
garmin-coach sync demo --days 90
garmin-coach plan
garmin-coach report -o report.html
```

## With your own Garmin data

```bash
read -p "Email: " GARMIN_EMAIL; read -s -p "Password: " GARMIN_PASSWORD; echo
export GARMIN_EMAIL GARMIN_PASSWORD           # only needed for the first login
garmin-coach check-garmin                     # test the connection: shows one day's data, saves nothing
unset GARMIN_PASSWORD
garmin-coach sync garmin --days 90            # first time: enough history for the baselines
garmin-coach daily garmin                     # after that, daily: sync 3 days + plan + report
```

After the first login the tokens are stored in `~/.garminconnect`, so the password is no longer needed.
For daily automation, e.g. cron: `15 7 * * * garmin-coach daily garmin -o ~/coach.html`.

If the Garmin sync doesn't work, import CSV files instead (see `examples/`):

```bash
garmin-coach sync csv --daily examples/daily.csv --activities examples/activities.csv
```

## What it tracks

| Signal | Method |
|---|---|
| HRV | 7-day average of ln(rMSSD) compared with your personal 60-day baseline ± 0.5 SD. A single-night drop below −1.5 SD is flagged separately. Needs ≥ 14 nights of history. |
| Resting HR | Today vs. 30-day median. +4 bpm = slightly elevated, +7 bpm = elevated. |
| Sleep | Last night vs. target, 7-day average, sleep debt accumulated over the week. |
| Load | Garmin training load, or Banister TRIMP from average HR if missing. ATL (7 d), CTL (42 d), ACWR. |

These combine into a readiness score of 0–100: green ≥ 75, yellow 50–74, red < 50.

## How the plan updates

1. **Base plan from the goal:** base → build → peak → taper → race. Weekly volume rises by
   at most 8 %, every 4th week is lighter. Quality sessions are never back-to-back or next to the long run.
2. **Today's readiness:** red → today becomes rest and any missed quality session moves to a free day
   later in the week (if it fits safely). Yellow → quality session swapped for easy running, long run shortened 20 %.
3. **HRV trend low** → this week's quality sessions become easy runs.
4. **Load spiking (ACWR > 1.5)** → next 3 days easy and shorter.
5. **Completion:** if the last two weeks fell short of 80 % of planned, future volume rebuilds
   from the level actually run instead of jumping back to the plan's level. If the goal then looks out of reach, the program says so.
6. **Goal change** → the plan is rebuilt from scratch. Past history is kept.

Running more than planned never raises the plan: volume is never set above the base plan.

## Limitations — read these

- **Garmin has no open API for individuals.** The sync uses the unofficial `garminconnect` library,
  which can break when Garmin changes its login (requires version ≥ 0.3.20). The field names have been
  checked against the library's own type definitions, but not against a live account —
  `check-garmin` is the first real test. CSV import is the fallback.
- The readiness weights are **heuristic**, not a validated medical model. If you have symptoms
  of illness, don't train no matter what the score says.
- The training plan is running-focused. Other sports are counted in the load but not planned.
- The plan doesn't know your paces; intensity is guided by heart-rate zones (Karvonen). A measured
  max HR is much better than an age-based estimate (`--age`).

## Development

```bash
pip install -e ".[dev]" && pytest
```
