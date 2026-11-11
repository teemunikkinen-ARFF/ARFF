"""Palautumis- ja kuormitusanalyysi.

Menetelmät:
* HRV: ln(rMSSD):n 7 päivän liukuva keskiarvo verrataan henkilökohtaiseen perustasoon
  (60 vrk, ei viimeisintä viikkoa) ± 0,5 SD ("smallest worthwhile change"). Lähestymistapa
  perustuu mm. Kiviniemen ym. (2007) ja Plewsin ym. (2013) HRV-ohjattuun harjoitteluun.
* Leposyke: päivän arvo vs. 30 vrk mediaani.
* Uni: viime yö, 7 vrk keskiarvo ja univaje tavoitteeseen nähden.
* Kuorma: Garminin training load, tai jos puuttuu, Banisterin TRIMP keskisykkeestä.
  ATL (7 vrk) ja CTL (42 vrk) eksponentiaalisina keskiarvoina, ACWR = ATL / CTL.

Valmiuspisteiden painot ovat heuristisia, eivät validoitu lääketieteellinen malli.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

from .models import Activity, DailyMetrics, Profile

HRV_BASELINE_DAYS = 60
HRV_MIN_BASELINE_VALUES = 14
RHR_BASELINE_DAYS = 30
RHR_MIN_BASELINE_VALUES = 7


@dataclass
class HrvState:
    status: str  # low | normal | high | insufficient
    last_night_ms: float | None = None
    avg7_ms: float | None = None
    baseline_ms: float | None = None
    band_ms: tuple[float, float] | None = None
    acute_drop: bool = False


@dataclass
class RhrState:
    status: str  # elevated | slightly_elevated | normal | low | insufficient
    today: float | None = None
    baseline: float | None = None
    delta: float | None = None


@dataclass
class SleepState:
    status: str  # good | short | very_short | insufficient
    last_night_h: float | None = None
    avg7_h: float | None = None
    debt7_h: float = 0.0
    score: float | None = None


@dataclass
class LoadState:
    atl: float
    ctl: float
    tsb: float
    acwr: float | None
    daily: dict[date, float] = field(default_factory=dict)
    atl_series: dict[date, float] = field(default_factory=dict)
    ctl_series: dict[date, float] = field(default_factory=dict)


@dataclass
class Readiness:
    score: int
    level: str  # green | yellow | red
    reasons: list[str]
    confidence: str  # high | medium | low


@dataclass
class Analysis:
    as_of: date
    hrv: HrvState
    rhr: RhrState
    sleep: SleepState
    load: LoadState
    readiness: Readiness
    resting_hr_used: float


# --- apufunktiot ---------------------------------------------------------------

def _by_day(metrics: list[DailyMetrics]) -> dict[date, DailyMetrics]:
    return {m.day: m for m in metrics}


def _window(by_day: dict[date, DailyMetrics], attr: str, start: date, end: date) -> list[float]:
    out = []
    d = start
    while d <= end:
        m = by_day.get(d)
        v = getattr(m, attr) if m else None
        if v is not None and v > 0:
            out.append(float(v))
        d += timedelta(days=1)
    return out


def ewma_series(daily: dict[date, float], start: date, end: date, time_constant: float) -> dict[date, float]:
    """Banisterin eksponentiaalinen liukuva keskiarvo (puuttuvat päivät = 0 kuormaa)."""
    k = 1 - math.exp(-1 / time_constant)
    out: dict[date, float] = {}
    v = 0.0
    d = start
    while d <= end:
        v = v + k * (daily.get(d, 0.0) - v)
        out[d] = v
        d += timedelta(days=1)
    return out


def trimp(duration_min: float, avg_hr: float, max_hr: float, rest_hr: float, male: bool = True) -> float:
    """Banisterin TRIMP."""
    if max_hr <= rest_hr:
        return 0.0
    hrr = max(0.0, min(1.0, (avg_hr - rest_hr) / (max_hr - rest_hr)))
    b = 1.92 if male else 1.67
    return duration_min * hrr * 0.64 * math.exp(b * hrr)


def activity_load(a: Activity, max_hr: float, rest_hr: float) -> float:
    if a.load is not None and a.load > 0:
        return float(a.load)
    if a.avg_hr:
        return trimp(a.duration_min, a.avg_hr, max_hr, rest_hr)
    # Ei sykettä: oletetaan kevyt-kohtalainen teho (~TRIMP 1/min).
    return a.duration_min * 1.0


# --- osa-analyysit ---------------------------------------------------------------

def analyze_hrv(metrics: list[DailyMetrics], as_of: date) -> HrvState:
    by_day = _by_day(metrics)
    base = _window(by_day, "hrv_ms", as_of - timedelta(days=HRV_BASELINE_DAYS + 7), as_of - timedelta(days=7))
    last7 = _window(by_day, "hrv_ms", as_of - timedelta(days=6), as_of)
    today = by_day.get(as_of).hrv_ms if by_day.get(as_of) else None
    if len(base) < HRV_MIN_BASELINE_VALUES or len(last7) < 4:
        return HrvState(status="insufficient", last_night_ms=today,
                        avg7_ms=(statistics.fmean(last7) if last7 else None))
    ln_base = [math.log(v) for v in base]
    mean, sd = statistics.fmean(ln_base), statistics.stdev(ln_base)
    lo, hi = mean - 0.5 * sd, mean + 0.5 * sd
    avg7 = statistics.fmean(math.log(v) for v in last7)
    status = "low" if avg7 < lo else "high" if avg7 > hi else "normal"
    acute = bool(today) and math.log(today) < mean - 1.5 * sd
    return HrvState(
        status=status, last_night_ms=today, avg7_ms=math.exp(avg7), baseline_ms=math.exp(mean),
        band_ms=(math.exp(lo), math.exp(hi)), acute_drop=acute,
    )


def rhr_baseline(metrics: list[DailyMetrics], as_of: date) -> float | None:
    vals = _window(_by_day(metrics), "resting_hr", as_of - timedelta(days=RHR_BASELINE_DAYS), as_of - timedelta(days=1))
    return statistics.median(vals) if len(vals) >= RHR_MIN_BASELINE_VALUES else None


def analyze_rhr(metrics: list[DailyMetrics], as_of: date) -> RhrState:
    by_day = _by_day(metrics)
    base = rhr_baseline(metrics, as_of)
    today = by_day.get(as_of).resting_hr if by_day.get(as_of) else None
    if base is None or today is None:
        return RhrState(status="insufficient", today=today, baseline=base)
    delta = today - base
    status = "elevated" if delta >= 7 else "slightly_elevated" if delta >= 4 else "low" if delta <= -5 else "normal"
    return RhrState(status=status, today=today, baseline=base, delta=delta)


def analyze_sleep(metrics: list[DailyMetrics], as_of: date, target_h: float) -> SleepState:
    by_day = _by_day(metrics)
    m = by_day.get(as_of)
    last = m.sleep_hours if m else None
    week = _window(by_day, "sleep_hours", as_of - timedelta(days=6), as_of)
    if last is None and not week:
        return SleepState(status="insufficient")
    debt = sum(max(0.0, target_h - h) for h in week)
    if last is None:
        status = "insufficient"
    elif last < target_h - 1.5:
        status = "very_short"
    elif last < target_h - 0.75:
        status = "short"
    else:
        status = "good"
    return SleepState(
        status=status, last_night_h=last, avg7_h=(statistics.fmean(week) if week else None), debt7_h=debt,
        score=(m.sleep_score if m else None),
    )


def analyze_load(activities: list[Activity], as_of: date, max_hr: float, rest_hr: float, history_days: int = 120) -> LoadState:
    start = as_of - timedelta(days=history_days)
    daily: dict[date, float] = {}
    for a in activities:
        if start <= a.day <= as_of:
            daily[a.day] = daily.get(a.day, 0.0) + activity_load(a, max_hr, rest_hr)
    atl_s = ewma_series(daily, start, as_of, 7)
    ctl_s = ewma_series(daily, start, as_of, 42)
    atl, ctl = atl_s[as_of], ctl_s[as_of]
    acwr = atl / ctl if ctl >= 5 else None  # liian vähän historiaa -> ei suhdelukua
    return LoadState(atl=atl, ctl=ctl, tsb=ctl - atl, acwr=acwr, daily=daily, atl_series=atl_s, ctl_series=ctl_s)


# --- valmius ------------------------------------------------------------------------

def compute_readiness(hrv: HrvState, rhr: RhrState, sleep: SleepState, load: LoadState) -> Readiness:
    score = 100
    reasons: list[str] = []

    if hrv.status == "low":
        score -= 25
        reasons.append(f"HRV:n 7 vrk keskiarvo {hrv.avg7_ms:.0f} ms on alle normaalialueen "
                       f"({hrv.band_ms[0]:.0f}–{hrv.band_ms[1]:.0f} ms)")
    if hrv.acute_drop:
        score -= 15
        reasons.append(f"Viime yön HRV {hrv.last_night_ms:.0f} ms selvästi perustasoa ({hrv.baseline_ms:.0f} ms) matalampi")

    if rhr.status == "elevated":
        score -= 20
        reasons.append(f"Leposyke {rhr.today:.0f} on {rhr.delta:+.0f} lyöntiä perustasosta ({rhr.baseline:.0f})")
    elif rhr.status == "slightly_elevated":
        score -= 10
        reasons.append(f"Leposyke hieman koholla ({rhr.delta:+.0f} lyöntiä)")

    if sleep.status == "very_short":
        score -= 15
        reasons.append(f"Viime yön uni vain {sleep.last_night_h:.1f} h")
    elif sleep.status == "short":
        score -= 8
        reasons.append(f"Viime yön uni lyhyt ({sleep.last_night_h:.1f} h)")
    if sleep.debt7_h >= 5:
        score -= 10
        reasons.append(f"Univajetta kertynyt viikossa {sleep.debt7_h:.1f} h")

    if load.acwr is not None:
        if load.acwr > 1.5:
            score -= 15
            reasons.append(f"Akuutti kuorma kasvanut liian nopeasti (ACWR {load.acwr:.2f} > 1,5)")
        elif load.acwr > 1.3:
            score -= 8
            reasons.append(f"Akuutti kuorma koholla (ACWR {load.acwr:.2f})")

    available = sum(s != "insufficient" for s in (hrv.status, rhr.status, sleep.status))
    confidence = {3: "high", 2: "medium"}.get(available, "low")
    if confidence == "low":
        reasons.append("Vähän mittausdataa – arvio on epävarma")

    score = max(0, min(100, score))
    level = "green" if score >= 75 else "yellow" if score >= 50 else "red"
    if not reasons:
        reasons.append("Palautumismittarit normaalialueella")
    return Readiness(score=score, level=level, reasons=reasons, confidence=confidence)


def analyze(metrics: list[DailyMetrics], activities: list[Activity], profile: Profile, as_of: date,
            exclude_days: set[date] | None = None) -> Analysis:
    """`exclude_days`: päivät, joiden yön mittaukset eivät ole vertailukelpoisia (esim. 24 h vuoron jälkeinen aamu)."""
    if exclude_days:
        metrics = [m for m in metrics if m.day not in exclude_days]
    rest_hr = profile.resting_hr or rhr_baseline(metrics, as_of + timedelta(days=1)) or 60
    hrv = analyze_hrv(metrics, as_of)
    rhr = analyze_rhr(metrics, as_of)
    sleep = analyze_sleep(metrics, as_of, profile.sleep_target_h)
    load = analyze_load(activities, as_of, profile.max_hr, rest_hr)
    readiness = compute_readiness(hrv, rhr, sleep, load)
    if exclude_days and as_of in exclude_days:
        readiness.reasons.insert(0, "Työvuoron jälkeinen päivä: yön uni-, HRV- ja leposykearvoja ei käytetä (eivät vertailukelpoisia)")
    return Analysis(
        as_of=as_of, hrv=hrv, rhr=rhr, sleep=sleep, load=load, readiness=readiness, resting_hr_used=float(rest_hr),
    )


def hr_zones(max_hr: float, rest_hr: float) -> dict[str, tuple[int, int]]:
    """Karvosen menetelmällä (sykereservi) lasketut sykealueet Z1–Z5."""
    bounds = {"Z1": (0.50, 0.60), "Z2": (0.60, 0.70), "Z3": (0.70, 0.80), "Z4": (0.80, 0.90), "Z5": (0.90, 1.00)}
    hrr = max_hr - rest_hr
    return {z: (round(rest_hr + lo * hrr), round(rest_hr + hi * hrr)) for z, (lo, hi) in bounds.items()}
