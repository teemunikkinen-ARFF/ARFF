"""Tavoitteeseen perustuva perusohjelma (ennen datapohjaista mukautusta).

Periaatteet:
* Jaksotus: peruskausi → kehityskausi → huippukausi → kevennys → kilpailu.
* Viikkovolyymi nousee enintään 8 % viikossa, joka 4. viikko on kevyempi (-25 %).
* Pitkä lenkki 30–38 % viikkovolyymista (maraton suurin), katto tavoitematkan mukaan.
* Laatuharjoitukset eivät ole peräkkäisinä päivinä eivätkä pitkän lenkin vieressä.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .models import GOAL_DISTANCE_KM, GOAL_LABELS, SESSION_LABELS, Goal, Session

WEEKLY_INCREASE = 1.08
DELOAD_FACTOR = 0.75


@dataclass(frozen=True)
class GoalSpec:
    peak_km: float
    long_max_km: float
    taper: tuple[float, ...]  # kevennysviikkojen volyymi suhteessa huippuun; viimeinen = kilpailuviikko


GOAL_SPECS = {
    "5k": GoalSpec(40, 14, (0.6,)),
    "10k": GoalSpec(50, 18, (0.65,)),
    "half": GoalSpec(60, 22, (0.75, 0.5)),
    "marathon": GoalSpec(75, 32, (0.75, 0.6, 0.4)),
    "fitness": GoalSpec(35, 14, ()),
}
# Pitkän lenkin osuus viikkovolyymista: maratonilla pitkä lenkki on ohjelman tärkein harjoitus.
LONG_SHARE = {"marathon": 0.38, "half": 0.33}
DAYS_FACTOR = {3: 0.65, 4: 0.8, 5: 0.95, 6: 1.0}

# Harjoituspäivät viikonpäivinä, kun pitkä lenkki on sunnuntaina (6). Siirretään pitkän lenkin päivän mukaan.
DAY_PATTERNS = {3: (1, 3, 6), 4: (1, 2, 4, 6), 5: (0, 1, 3, 4, 6), 6: (0, 1, 2, 4, 5, 6)}

PHASE_BASE = "Peruskausi"
PHASE_BUILD = "Kehityskausi"
PHASE_PEAK = "Huippukausi"
PHASE_TAPER = "Kevennys"
PHASE_RACE = "Kilpailuviikko"
PHASE_DELOAD = "Kevyt viikko"

PHASE_QUALITY = {
    PHASE_BASE: ("strides",),
    PHASE_BUILD: ("intervals", "tempo"),
    PHASE_PEAK: ("intervals", "tempo"),
    PHASE_TAPER: ("sharpen",),
    PHASE_DELOAD: ("strides",),
}

# (ohje, varsinaisen työosuuden km). Session kokonaispituus = työ + lämmittely/verryttely + palautukset.
INTERVALS = {
    ("5k", PHASE_BUILD): ("6 × 800 m Z4–Z5, palautus 2 min hölkkä", 4.8),
    ("5k", PHASE_PEAK): ("5 × 1000 m 5 km:n kisavauhtia, palautus 2 min", 5.0),
    ("10k", PHASE_BUILD): ("5 × 1000 m Z4–Z5, palautus 90 s", 5.0),
    ("10k", PHASE_PEAK): ("3 × 2 km 10 km:n kisavauhtia, palautus 3 min", 6.0),
    ("half", PHASE_BUILD): ("4 × 1,5 km Z4, palautus 2 min", 6.0),
    ("half", PHASE_PEAK): ("3 × 3 km puolimaratonvauhtia, palautus 3 min", 9.0),
    ("marathon", PHASE_BUILD): ("3 × 3 km Z3–Z4, palautus 3 min", 9.0),
    ("marathon", PHASE_PEAK): ("2 × 5 km maratonvauhtia, palautus 3 min", 10.0),
}
FITNESS_INTERVALS = ("8 × 1 min reippaasti Z4, palautus 1 min kävellen/hölkäten", 2.0)
MAX_TEMPO_KM = 12.0


def interval_set(goal_kind: str, phase: str) -> tuple[str, float]:
    if goal_kind == "fitness":
        return FITNESS_INTERVALS
    return INTERVALS.get((goal_kind, phase)) or INTERVALS[(goal_kind, PHASE_BUILD)]


def r05(x: float) -> float:
    """Pyöristys 0,5 km tarkkuuteen."""
    return round(x * 2) / 2


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _circ(a: int, b: int) -> int:
    x = abs(a - b) % 7
    return min(x, 7 - x)


def training_days(days_per_week: int, long_day: int) -> list[int]:
    shift = long_day - 6
    return sorted((d + shift) % 7 for d in DAY_PATTERNS[days_per_week])


def pick_quality_days(train: list[int], long_day: int, n: int) -> list[int]:
    cands = sorted((d for d in train if d != long_day), key=lambda d: (_circ(d, long_day) <= 1, d))
    chosen: list[int] = []
    for d in cands:
        if len(chosen) < n and all(_circ(d, c) > 1 for c in chosen):
            chosen.append(d)
    for d in cands:  # jos ei muuten mahdu, sallitaan vierekkäisyys
        if len(chosen) < n and d not in chosen:
            chosen.append(d)
    return chosen


def describe(kind: str, km: float, phase: str, goal_kind: str, zones: dict[str, tuple[int, int]]) -> tuple[str, str]:
    """Palauttaa (otsikko, ohje) sessiolle."""
    z = lambda name: f"{name} {zones[name][0]}–{zones[name][1]} bpm"  # noqa: E731
    title = SESSION_LABELS[kind]
    if kind == "rest":
        return title, "Lepo tai kevyt liikkuvuus/venyttely."
    if kind == "recovery":
        return title, f"{km:g} km hyvin kevyesti, {z('Z1')}."
    if kind == "easy":
        return title, f"{km:g} km rauhallisesti, {z('Z2')}."
    if kind == "strides":
        return title, f"{km:g} km {z('Z2')}, lopuksi 6 × 20 s rentoja vetoja, kävelypalautus."
    if kind == "long":
        extra = ""
        if goal_kind == "marathon" and phase == PHASE_PEAK:
            extra = " Viimeiset 5 km maratonvauhtia."
        elif goal_kind == "half" and phase == PHASE_PEAK:
            extra = " Viimeiset 3 km puolimaratonvauhtia."
        return title, f"{km:g} km tasaisesti, {z('Z2')}.{extra}"
    if kind == "tempo":
        core = max(2.0, r05(km - 4))
        lo, hi = sum(zones["Z3"]) // 2, sum(zones["Z4"]) // 2
        return title, f"2 km lämmittely, {core:g} km kynnysvauhtia {lo}–{hi} bpm (Z3–Z4), loppuverryttely. Yht. {km:g} km."
    if kind == "intervals":
        body = interval_set(goal_kind, phase)[0]
        return title, f"2 km lämmittely, {body}, loppuverryttely. Yht. {km:g} km. Z4 = {zones['Z4'][0]}–{zones['Z4'][1]} bpm."
    if kind == "sharpen":
        return title, f"2 km lämmittely, 4 × 400 m kisavauhtia hieman reippaammin, täysi palautus, verryttely. Yht. {km:g} km."
    if kind == "race":
        return f"KILPAILU: {GOAL_LABELS[goal_kind]}", f"{km:g} km. Lämmittely 10–15 min, aloita maltillisesti."
    raise ValueError(kind)


def make_session(day: date, kind: str, km: float, phase: str, week: int, goal: Goal, zones) -> Session:
    km = 0.0 if kind == "rest" else km
    title, details = describe(kind, km, phase, goal.kind, zones)
    return Session(day=day, kind=kind, distance_km=km, title=title, details=details, phase=phase, week_index=week)


def week_plan(goal: Goal, until: date | None = None) -> tuple[date, list[tuple[str, float]]]:
    """Palauttaa (ensimmäinen maanantai, [(vaihe, viikkovolyymi), ...]).

    Kilpailuttomalle tavoitteelle viikkoja luodaan `until`-päivään asti (oletus 52 vk)."""
    spec = GOAL_SPECS[goal.kind]
    start = monday(goal.start_date)
    if goal.event_date:
        n_weeks = (monday(goal.event_date) - start).days // 7 + 1
    elif until:
        n_weeks = max(4, (monday(until) - start).days // 7 + 1)
    else:
        n_weeks = 52
    taper = list(spec.taper)
    while taper and n_weeks - len(taper) < 2:  # lyhyt valmistautumisaika -> lyhennetään kevennystä
        taper.pop(0)
    core = n_weeks - len(taper)

    if goal.event_date:
        n_base = round(core * 0.4)
        n_build = round(core * 0.4)
        phases = [PHASE_BASE] * n_base + [PHASE_BUILD] * n_build + [PHASE_PEAK] * (core - n_base - n_build)
    else:
        phases = [PHASE_BASE] * min(4, core) + [PHASE_BUILD] * max(0, core - 4)

    start_km = max(goal.weekly_km, 10.0)
    peak_target = max(start_km, spec.peak_km * DAYS_FACTOR[goal.days_per_week])
    weeks: list[tuple[str, float]] = []
    v = start_km
    for i in range(core):
        deload = i % 4 == 3 and i != core - 1
        if deload:
            weeks.append((PHASE_DELOAD, v * DELOAD_FACTOR))
            continue
        if i > 0:
            v = min(v * WEEKLY_INCREASE, peak_target)
        weeks.append((phases[i], v))
    for j, f in enumerate(taper):
        weeks.append((PHASE_RACE if j == len(taper) - 1 else PHASE_TAPER, v * f))
    if goal.event_date and not taper:
        weeks[-1] = (PHASE_RACE, weeks[-1][1])
    return start, weeks


def _race_week(week_start: date, w: int, goal: Goal, zones) -> list[Session]:
    sessions = []
    ev = goal.event_date
    train = set(training_days(goal.days_per_week, goal.long_run_weekday))
    sharpen_day = ev - timedelta(days=3)
    easy_km = 6.0 if goal.kind == "marathon" else 5.0
    for i in range(7):
        d = week_start + timedelta(days=i)
        if d == ev:
            sessions.append(make_session(d, "race", GOAL_DISTANCE_KM[goal.kind], PHASE_RACE, w, goal, zones))
        elif d > ev or d == ev - timedelta(days=1):
            sessions.append(make_session(d, "rest", 0, PHASE_RACE, w, goal, zones))
        elif d == sharpen_day:
            sessions.append(make_session(d, "sharpen", 6.0, PHASE_RACE, w, goal, zones))
        elif d.weekday() in train and d != sharpen_day + timedelta(days=1) and d != sharpen_day - timedelta(days=1):
            sessions.append(make_session(d, "easy", easy_km, PHASE_RACE, w, goal, zones))
        else:
            sessions.append(make_session(d, "rest", 0, PHASE_RACE, w, goal, zones))
    return sessions


def _normal_week(week_start: date, w: int, phase: str, volume: float, goal: Goal, zones) -> list[Session]:
    spec = GOAL_SPECS[goal.kind]
    long_day = goal.long_run_weekday
    train = training_days(goal.days_per_week, long_day)
    q_kinds = list(PHASE_QUALITY[phase])
    if goal.kind == "fitness" and phase == PHASE_BUILD:
        q_kinds = ["intervals"] if goal.days_per_week <= 4 else ["intervals", "tempo"]
    if goal.days_per_week == 3:
        q_kinds = q_kinds[:1]
    q_days = pick_quality_days(train, long_day, len(q_kinds))
    q_map = dict(zip(q_days, q_kinds))

    share = LONG_SHARE.get(goal.kind, 0.30) + (0.05 if goal.days_per_week == 3 else 0.0)
    long_km = min(spec.long_max_km, max(5.0, volume * share))
    work = interval_set(goal.kind, phase)[1]
    fixed = {
        "tempo": min(MAX_TEMPO_KM, max(5.0, volume * 0.18)),
        "intervals": max(work + 4.0, volume * 0.17),  # 4 km lämmittely + verryttely
        "sharpen": max(5.0, volume * 0.15),
    }
    q_total = sum(fixed[k] for k in q_map.values() if k in fixed)
    n_flex = sum(1 for d in train if d != long_day and q_map.get(d) not in fixed)
    flex_km = max(3.0, (volume - long_km - q_total) / n_flex) if n_flex else 0.0
    # Peruslenkki ei saa venyä pitkän lenkin mittaiseksi: kasvatetaan ensin pitkää lenkkiä (katon rajoissa),
    # ja jos se ei riitä, viikon volyymi jää tavoitetta pienemmäksi – se on turvallisempi virhe.
    if n_flex and flex_km > 0.8 * long_km:
        long_km = min(spec.long_max_km, max(long_km, (volume - q_total) / (1 + 0.8 * n_flex)))
        flex_km = min(max(3.0, (volume - long_km - q_total) / n_flex), 0.8 * long_km)

    sessions = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        wd = d.weekday()
        if wd not in train:
            kind, km = "rest", 0.0
        elif wd == long_day:
            kind, km = "long", long_km
        elif wd in q_map:
            kind = q_map[wd]
            km = fixed.get(kind, flex_km)
        else:
            kind, km = "easy", flex_km
        sessions.append(make_session(d, kind, r05(km), phase, w, goal, zones))
    return sessions


def build_reference_plan(goal: Goal, zones: dict[str, tuple[int, int]], until: date | None = None) -> list[Session]:
    """Koko perusohjelma tavoitteen alusta kilpailuun (tai `until`-päivään asti yleiskuntotavoitteella)."""
    goal.validate()
    if goal.kind == "cycling":
        from .cycling import build_cycling_plan

        return build_cycling_plan(goal, zones, until)
    start, weeks = week_plan(goal, until)
    sessions: list[Session] = []
    for w, (phase, vol) in enumerate(weeks):
        ws = start + timedelta(weeks=w)
        if until and ws > until:
            break
        if phase == PHASE_RACE:
            sessions.extend(_race_week(ws, w, goal, zones))
        else:
            sessions.extend(_normal_week(ws, w, phase, vol, goal, zones))
    return sessions


def weekly_volumes(sessions: list[Session]) -> dict[date, float]:
    out: dict[date, float] = {}
    for s in sessions:
        k = monday(s.day)
        out[k] = out.get(k, 0.0) + s.distance_km
    return out
