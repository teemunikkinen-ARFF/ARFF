"""Garmin Connect -synkronointi epävirallisen `garminconnect`-kirjaston kautta.

HUOM: Garminilla ei ole avointa rajapintaa yksityishenkilöille (Health API vaatii yrityssopimuksen).
`garminconnect` käyttää samaa rajapintaa kuin Garmin Connect -verkkosivu. Se toimii yleensä hyvin,
mutta voi rikkoutua Garminin muuttaessa kirjautumista. Siksi jäsennysfunktiot ovat erillään
verkkokutsuista ja ne käsittelevät puuttuvat kentät sallivasti.

Tunnukset: ympäristömuuttujat GARMIN_EMAIL ja GARMIN_PASSWORD. Ensimmäisen kirjautumisen jälkeen
tokenit tallennetaan hakemistoon GARMINTOKENS (oletus ~/.garminconnect), jolloin salasanaa ei enää tarvita.
Vaatii garminconnect >= 0.3 (vanhemmat garth-muotoiset tokenit eivät kelpaa – kirjaudu kerran uudelleen).
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..models import Activity, DailyMetrics


# --- jäsennys (testattavissa ilman verkkoa) ----------------------------------------

def _get(d: Any, *path, default=None):
    for p in path:
        if isinstance(d, dict):
            d = d.get(p)
        elif isinstance(d, list) and isinstance(p, int) and len(d) > p:
            d = d[p]
        else:
            return default
        if d is None:
            return default
    return d


def parse_hrv(payload: dict | None) -> float | None:
    v = _get(payload, "hrvSummary", "lastNightAvg")
    return float(v) if v else None


def parse_sleep(payload: dict | None) -> dict[str, float | None]:
    dto = _get(payload, "dailySleepDTO") or {}
    h = lambda k: (dto.get(k) / 3600) if dto.get(k) else None  # noqa: E731
    score = _get(dto, "sleepScores", "overall", "value")
    return {
        "sleep_hours": h("sleepTimeSeconds"),
        "deep_sleep_hours": h("deepSleepSeconds"),
        "rem_sleep_hours": h("remSleepSeconds"),
        "sleep_score": float(score) if score is not None else None,
    }


def parse_resting_hr(payload: dict | None) -> float | None:
    """Tukee sekä get_heart_rates- että get_rhr_day-vastauksia."""
    v = _get(payload, "restingHeartRate")
    if v is None:
        v = _get(payload, "allMetrics", "metricsMap", "WELLNESS_RESTING_HEART_RATE", 0, "value")
    return float(v) if v else None


def parse_activity(a: dict) -> Activity | None:
    start = a.get("startTimeLocal") or a.get("startTimeGMT")
    if not start or a.get("activityId") is None:
        return None
    day = datetime.fromisoformat(str(start).replace("T", " ")[:19]).date()
    dist = a.get("distance")
    return Activity(
        activity_id=f"garmin-{a['activityId']}",
        day=day,
        sport=_get(a, "activityType", "typeKey", default="other"),
        duration_min=float(a.get("duration") or 0) / 60,
        distance_km=float(dist) / 1000 if dist else None,
        avg_hr=float(a["averageHR"]) if a.get("averageHR") else None,
        load=float(a["activityTrainingLoad"]) if a.get("activityTrainingLoad") else None,
    )


# --- verkkokutsut ---------------------------------------------------------------------

def connect():
    """Kirjautuu. Tallennetut tokenit käytetään ensin; muuten kirjaudutaan tunnuksilla ja tokenit tallennetaan.

    garminconnect >= 0.3 hoitaa tokenien latauksen ja tallennuksen itse `login(tokenstore)`-kutsussa.
    """
    try:
        from garminconnect import Garmin, GarminConnectAuthenticationError
    except ImportError as e:  # pragma: no cover
        raise SystemExit("Asenna Garmin-tuki: pip install -e '.[garmin]'  (tai pip install -U garminconnect)") from e

    tokenstore = Path(os.environ.get("GARMINTOKENS", "~/.garminconnect")).expanduser()
    email, password = os.environ.get("GARMIN_EMAIL"), os.environ.get("GARMIN_PASSWORD")
    api = Garmin(email=email, password=password, prompt_mfa=lambda: input("Garmin MFA-koodi: ").strip())
    try:
        api.login(str(tokenstore))
    except GarminConnectAuthenticationError as e:
        if not (email and password):
            raise SystemExit(
                f"Kirjautuminen epäonnistui ({e}).\n"
                "Ensimmäisellä kerralla (tai kun tokenit ovat vanhentuneet) aseta GARMIN_EMAIL ja GARMIN_PASSWORD."
            ) from e
        raise SystemExit(f"Kirjautuminen epäonnistui: {e}") from e
    return api


def check(api, day: date) -> list[str]:
    """Hakee yhden päivän ja viikon harjoitukset tallentamatta mitään. Palauttaa raporttirivit."""
    ds = day.isoformat()
    out = []
    for label, call, parse in (
        ("Leposyke", lambda: api.get_heart_rates(ds), parse_resting_hr),
        ("HRV", lambda: api.get_hrv_data(ds), parse_hrv),
        ("Uni", lambda: api.get_sleep_data(ds), parse_sleep),
    ):
        try:
            raw = call()
            keys = ", ".join(list(raw.keys())[:8]) if isinstance(raw, dict) else type(raw).__name__
            out.append(f"  {label:<9} {parse(raw)}   [vastauksen kentät: {keys}]")
        except Exception as e:
            out.append(f"  {label:<9} VIRHE {type(e).__name__}: {e}")
    try:
        raw = api.get_activities_by_date((day - timedelta(days=7)).isoformat(), ds) or []
        acts = [a for a in (parse_activity(x) for x in raw) if a]
        out.append(f"  Harjoitukset 7 vrk: {len(raw)} haettu, {len(acts)} tulkittu")
        for a in acts[:5]:
            out.append(f"    {a.day} {a.sport:<20} {a.duration_min:5.0f} min  {a.distance_km or 0:5.1f} km  "
                       f"syke {a.avg_hr or '-'}  kuorma {a.load or '-'}")
    except Exception as e:
        out.append(f"  Harjoitukset VIRHE {type(e).__name__}: {e}")
    return out


def fetch(api, start: date, end: date, log=print) -> tuple[list[DailyMetrics], list[Activity]]:
    metrics: list[DailyMetrics] = []
    d = start
    while d <= end:
        ds = d.isoformat()
        m = DailyMetrics(day=d)
        for name, call in (
            ("leposyke", lambda: setattr(m, "resting_hr", parse_resting_hr(api.get_heart_rates(ds)))),
            ("HRV", lambda: setattr(m, "hrv_ms", parse_hrv(api.get_hrv_data(ds)))),
            ("uni", lambda: [setattr(m, k, v) for k, v in parse_sleep(api.get_sleep_data(ds)).items()]),
        ):
            try:
                call()
            except Exception as e:  # yksittäisen päivän virhe ei kaada koko synkronointia
                log(f"  {ds}: {name} epäonnistui ({type(e).__name__}: {e})")
        metrics.append(m)
        d += timedelta(days=1)

    raw = api.get_activities_by_date(start.isoformat(), end.isoformat()) or []
    activities = [a for a in (parse_activity(x) for x in raw) if a]
    return metrics, activities
