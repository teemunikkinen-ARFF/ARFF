"""Synteettinen demodata ohjelman kokeiluun ilman Garmin-tiliä.

Sisältää tarkoituksella heikentyneen jakson viimeisinä päivinä (HRV laskee, leposyke nousee,
uni lyhenee), jotta mukautuva ohjelma näkyy toiminnassa.
"""
from __future__ import annotations

import math
import random
from datetime import date, timedelta

from ..models import Activity, DailyMetrics


def generate(end: date, days: int = 90, seed: int = 7, stressed_days: int = 4) -> tuple[list[DailyMetrics], list[Activity]]:
    rng = random.Random(seed)
    metrics, acts = [], []
    for i in range(days):
        d = end - timedelta(days=days - 1 - i)
        stress = max(0, stressed_days - (days - 1 - i))  # 0 normaalisti, kasvaa loppua kohti
        ln_hrv = math.log(62) + rng.gauss(0, 0.09) - 0.10 * stress
        sleep = max(4.0, rng.gauss(7.6, 0.6) - 0.5 * stress)
        metrics.append(DailyMetrics(
            day=d,
            resting_hr=round(rng.gauss(48, 1.5) + 2.0 * stress),
            hrv_ms=round(math.exp(ln_hrv), 1),
            sleep_hours=round(sleep, 2),
            sleep_score=round(min(100, max(30, 55 + (sleep - 6) * 15 + rng.gauss(0, 4)))),
        ))
        wd = d.weekday()
        if wd in (1, 3, 6) or (wd == 4 and rng.random() < 0.5):
            km = {1: 7, 3: 8, 4: 6, 6: 13}[wd] * rng.uniform(0.85, 1.15)
            pace = rng.uniform(5.6, 6.2)
            hr = 148 if wd == 3 else 140
            acts.append(Activity(
                activity_id=f"demo-{d.isoformat()}", day=d, sport="running",
                duration_min=round(km * pace, 1), distance_km=round(km, 2), avg_hr=hr + rng.randint(-4, 4),
            ))
    return metrics, acts
