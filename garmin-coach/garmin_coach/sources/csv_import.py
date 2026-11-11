"""CSV-tuonti. Varareitti, jos Garmin-synkronointi ei toimi, tai dataa tulee muualta.

Päivittäiset mittarit (otsikkorivi pakollinen, tyhjä solu = ei dataa):
    date,resting_hr,hrv_ms,sleep_hours,sleep_score
    2026-11-01,48,62,7.6,81

Harjoitukset:
    date,sport,duration_min,distance_km,avg_hr,load,id
    2026-11-01,running,45,8.2,142,,
"""
from __future__ import annotations

import csv
import hashlib
from datetime import date
from pathlib import Path

from ..models import Activity, DailyMetrics

METRIC_COLUMNS = ("resting_hr", "hrv_ms", "sleep_hours", "sleep_score", "deep_sleep_hours", "rem_sleep_hours")


def _num(v: str | None) -> float | None:
    v = (v or "").strip().replace(",", ".")
    return float(v) if v else None


def read_daily(path: str | Path) -> list[DailyMetrics]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    out = []
    for i, r in enumerate(rows, start=2):
        try:
            out.append(DailyMetrics(day=date.fromisoformat(r["date"].strip()),
                                    **{c: _num(r.get(c)) for c in METRIC_COLUMNS}))
        except (KeyError, ValueError) as e:
            raise ValueError(f"{path}: rivi {i} virheellinen ({e})") from e
    return out


def read_activities(path: str | Path) -> list[Activity]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    out = []
    for i, r in enumerate(rows, start=2):
        try:
            day = date.fromisoformat(r["date"].strip())
            aid = (r.get("id") or "").strip() or "csv-" + hashlib.sha1(
                f"{day}|{r.get('sport')}|{r.get('duration_min')}|{r.get('distance_km')}".encode()).hexdigest()[:12]
            out.append(Activity(
                activity_id=aid, day=day, sport=(r.get("sport") or "running").strip(),
                duration_min=_num(r.get("duration_min")) or 0.0, distance_km=_num(r.get("distance_km")),
                avg_hr=_num(r.get("avg_hr")), load=_num(r.get("load")),
            ))
        except (KeyError, ValueError) as e:
            raise ValueError(f"{path}: rivi {i} virheellinen ({e})") from e
    return out
