"""Pyöräilykunnon kehitys + lihaskunnon ylläpito.

Periaatteet:
* Tehoalueet Coggan-malliin FTP:n prosentteina: Z2 56–75 %, tempo 76–90 %, sweet spot 88–94 %,
  kynnys 95–105 %, VO2max 106–120 %.
* 12 viikon sykli: peruskausi (sweet spot/tempo) → kehityskausi (kynnys, over-under) → huippukausi (VO2max + kynnys)
  → testiviikko (FTP-testi). Joka 4. viikko on kevyt (-30 % tunteja, vähemmän toistoja). Testin jälkeen uusi sykli
  alkaa päivitetyllä FTP:llä, joten watit seuraavat kehittymistäsi.
* Viikkotunnit kasvavat enintään 8 % viikossa, katto = 1,5 × nykyinen (tai --max-hours).
* Voimaharjoittelu on ylläpitoa: kohtuullinen volyymi, 2 toiston vara. Alavartalopainotteinen sessio ei osu
  päivälle ennen laatuajoa tai pitkää ajoa (silloin ylävartalopainotteinen).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .models import Goal, Session
from .planner import (
    DELOAD_FACTOR, PHASE_BASE, PHASE_BUILD, PHASE_DELOAD, PHASE_PEAK, WEEKLY_INCREASE, monday,
)

PHASE_TEST = "Testiviikko"
MACRO_WEEKS = 12
MIN_BIKE_MIN = 30
STRENGTH_MIN = 60

# Päivät suhteessa pitkän ajon päivään (0 = pitkä ajo, 1 = seuraava päivä, ...).
BIKE_LAYOUT = {
    3: {"long": 0, "q1": 2, "q2": 4},
    4: {"long": 0, "q1": 2, "q2": 4, "easy": 5},
    5: {"long": 0, "q1": 2, "easy": 3, "q2": 4, "easy2": 5},
}
SHARES = {
    3: {"long": 0.40, "q1": 0.30, "q2": 0.30},
    4: {"long": 0.35, "q1": 0.25, "q2": 0.25, "easy": 0.15},
    5: {"long": 0.32, "q1": 0.22, "q2": 0.22, "easy": 0.12, "easy2": 0.12},
}
STRENGTH_PREFS = (5, 3, 1, 6, 4, 2)  # ensisijaiset päivät voimaharjoittelulle

QUALITY_BY_PHASE = {
    PHASE_BASE: ("sweetspot", "tempo"),
    PHASE_BUILD: ("threshold", "overunder"),
    PHASE_PEAK: ("vo2", "threshold"),
}
# Toistot × minuutit vaiheen viikoilla 1–3 (neljäs viikko on kevyt).
INTERVAL_SETS = {
    "sweetspot": [(2, 15), (3, 12), (3, 15)],
    "tempo": [(2, 20), (2, 25), (3, 20)],
    "threshold": [(3, 10), (3, 12), (2, 20)],
    "overunder": [(3, 9), (3, 12), (4, 9)],
    "vo2": [(5, 4), (6, 4), (5, 5)],
}
# %FTP-alue ja palautus (min)
INTENSITY = {
    "sweetspot": (88, 94, 5), "tempo": (76, 90, 5), "threshold": (95, 100, 5),
    "overunder": (95, 105, 5), "vo2": (110, 120, 4),
}
TITLES = {
    "sweetspot": "Sweet spot", "tempo": "Tempo", "threshold": "Kynnys", "overunder": "Over-under",
    "vo2": "VO2max-intervallit", "test": "FTP-testi", "easy": "Peruskestävyys", "long": "Pitkä ajo",
    "recovery": "Palauttava ajo",
}


@dataclass(frozen=True)
class Week:
    index: int
    phase: str  # näytettävä vaihe
    train_phase: str  # varsinainen harjoitusvaihe (kevyellä viikolla edellisen kauden sisältö)
    step: int  # viikko vaiheen sisällä 0–3
    hours: float
    deload: bool
    test: bool


def r5(x: float) -> int:
    return int(round(x / 5.0)) * 5


def watts(ftp: int | None, pct: float) -> int | None:
    return r5(ftp * pct / 100) if ftp else None


def power_text(ftp: int | None, lo: float, hi: float) -> str:
    if ftp:
        return f"{watts(ftp, lo)}–{watts(ftp, hi)} W ({lo:g}–{hi:g} % FTP)"
    return f"{lo:g}–{hi:g} % FTP:stä (aseta FTP: garmin-coach profile --ftp N)"


def week_plan(goal: Goal, until: date | None = None) -> tuple[date, list[Week]]:
    start = monday(goal.start_date)
    n = max(4, (monday(until) - start).days // 7 + 1) if until else MACRO_WEEKS
    cap = goal.max_weekly_hours or min(max(goal.weekly_hours * 1.5, goal.weekly_hours + 2), 12.0)
    h = goal.weekly_hours
    weeks: list[Week] = []
    for i in range(n):
        m = i % MACRO_WEEKS
        train = PHASE_BASE if m < 4 else PHASE_BUILD if m < 8 else PHASE_PEAK
        test = m == MACRO_WEEKS - 1
        deload = m % 4 == 3
        if not deload and i > 0:
            h = min(h * WEEKLY_INCREASE, cap)
        phase = PHASE_TEST if test else PHASE_DELOAD if deload else train
        weeks.append(Week(i, phase, train, m % 4, h * (DELOAD_FACTOR if deload else 1.0), deload, test))
    return start, weeks


# --- kuvaukset ----------------------------------------------------------------------------

def describe_bike(kind: str, minutes: int, phase: str, ftp: int | None, zones: dict[str, tuple[int, int]]) -> tuple[str, str]:
    """Rauhalliset ajot (easy, long, recovery). Laatuajot kuvataan `describe_quality`:lla."""
    title = TITLES[kind]
    hr = f" Syke yleensä alle {zones['Z2'][1]} bpm." if zones else ""
    if kind == "recovery":
        return title, f"{minutes} min hyvin kevyesti, alle {power_text(ftp, 0, 55)}, kadenssi vapaa."
    if kind == "easy":
        return title, f"{minutes} min tasaisesti Z2 {power_text(ftp, 56, 75)}.{hr}"
    if kind == "long":
        extra = ""
        if phase in (PHASE_BUILD, PHASE_PEAK) and minutes >= 120:
            extra = f" Tunnin kohdalla 2 × 10 min tempo {power_text(ftp, 76, 90)}, palautus 5 min."
        fuel = " Syö yli 90 min ajolla ≥ 60 g hiilihydraattia/h." if minutes >= 90 else ""
        return title, f"{minutes} min tasaisesti Z2 {power_text(ftp, 56, 75)}.{extra}{fuel}{hr}"
    raise ValueError(kind)


def quality_minutes(kind: str, step: int, light: bool) -> tuple[int, int, int]:
    reps, mins = INTERVAL_SETS[kind][min(step, 2)]
    if light:
        reps, mins = max(2, INTERVAL_SETS[kind][0][0] - 1), INTERVAL_SETS[kind][0][1]
    rest = INTENSITY[kind][2]
    return reps, mins, 15 + reps * mins + (reps - 1) * rest + 10


def describe_quality(kind: str, minutes: int, step: int, light: bool, ftp: int | None) -> tuple[str, str]:
    title = TITLES[kind]
    if kind == "test":
        return title, (
            f"{minutes} min: 20 min lämmittely, 3 × 1 min reippaasti (palautus 1 min), 5 min kevyesti, 5 min täysillä, "
            "10 min kevyesti, sitten 20 min kovin tasainen yritys (älä lähde liian kovaa). "
            "FTP ≈ 0,95 × 20 min keskiteho. Päivitä: garmin-coach profile --ftp <watit>."
        )
    reps, mins, _ = quality_minutes(kind, step, light)
    lo, hi, rest = INTENSITY[kind]
    if kind == "overunder":
        body = (f"{reps} × {mins} min vuorotellen 2 min @ {power_text(ftp, 95, 95)} ja 1 min @ {power_text(ftp, 105, 105)}")
    else:
        body = f"{reps} × {mins} min @ {power_text(ftp, lo, hi)}"
    tag = " (kevyt viikko: vähemmän toistoja)" if light else ""
    return title, f"15 min lämmittely, {body}, palautus {rest} min kevyesti, 10 min verryttely. Yht. {minutes} min.{tag}"


def describe_strength(minutes: int, emphasis: str, light: bool) -> tuple[str, str]:
    if emphasis == "lower":
        title = "Voima: alavartalo"
        body = ("kyykky tai jalkaprässi 3 × 5–6, romanialainen maastaveto 3 × 6, askelkyykky 2 × 8/jalka, "
                "pohkeet 2 × 12, keskivartalo 2 sarjaa")
    else:
        title = "Voima: ylävartalo"
        body = ("penkki- tai pystypunnerrus 3 × 5–6, soutu 3 × 6–8, leuanveto 3 × 6–8, olkapäät/ojentajat 2 × 10, "
                "keskivartalo 2 sarjaa. Jalat vain kevyesti, ei kuormitusta ennen laatu-/pitkää ajoa")
    light_txt = " Kevyt versio: 2 sarjaa/liike, ei lähelle epäonnistumista." if light else ""
    return title, f"{minutes} min: {body}. Jätä 2 toistoa varaan. Käytä omia liikkeitäsi, tämä on runko.{light_txt}"


# --- viikon rakentaminen ---------------------------------------------------------------

def _session(day: date, kind: str, sport: str, minutes: int, phase: str, week: int, title: str, details: str) -> Session:
    return Session(day=day, kind=kind, distance_km=0.0, title=title, details=details, phase=phase,
                   week_index=week, sport=sport, duration_min=float(minutes))


def build_week(week: Week, week_start: date, goal: Goal, zones: dict[str, tuple[int, int]]) -> list[Session]:
    ftp = goal.ftp
    n = goal.days_per_week
    layout, shares = BIKE_LAYOUT[n], SHARES[n]
    long_day = goal.long_run_weekday
    total = week.hours * 60
    qa = QUALITY_BY_PHASE[week.train_phase]
    day_of = lambda off: week_start + timedelta(days=(long_day + off) % 7)  # noqa: E731

    slots: dict[int, Session] = {}
    for slot, off in layout.items():
        base_min = total * shares[slot]
        if slot == "long":
            m = r5(min(270, max(60 if week.deload else 75, base_min)))
            title, det = describe_bike("long", m, week.phase, ftp, zones)
            s = _session(day_of(off), "long", "bike", m, week.phase, week.index, title, det)
        elif slot in ("q1", "q2"):
            kind = qa[0 if slot == "q1" else 1]
            if week.test:
                kind = "test" if slot == "q1" else "easy"
            elif week.deload and slot == "q2":
                kind = "easy"
            if kind == "easy":
                m = r5(max(40, base_min))
                title, det = describe_bike("easy", m, week.phase, ftp, zones)
                s = _session(day_of(off), "easy", "bike", m, week.phase, week.index, title, det)
            else:
                if kind == "test":
                    m = max(r5(base_min), 90)
                    step, light = 0, False
                else:
                    step, light = week.step, week.deload
                    m = max(r5(base_min), -(-quality_minutes(kind, step, light)[2] // 5) * 5)
                title, det = describe_quality(kind, m, step, light, ftp)
                s = _session(day_of(off), kind, "bike", m, week.phase, week.index, title, det)
        else:
            m = r5(max(40, base_min))
            title, det = describe_bike("easy", m, week.phase, ftp, zones)
            s = _session(day_of(off), "easy", "bike", m, week.phase, week.index, title, det)
        slots[off] = s

    # Voimaharjoitukset omille päivilleen (ohjelmassa yksi sessio/päivä). Jos seuraavana päivänä on laatu- tai
    # pitkä ajo, sessio on ylävartalopainotteinen.
    hard_offsets = {off for off, s in slots.items() if s.is_quality or s.kind == "long"}
    strength_offsets = [o for o in STRENGTH_PREFS if o not in slots][: goal.strength_days]
    strength: dict[int, Session] = {}
    for off in strength_offsets:
        emphasis = "upper" if ((off + 1) % 7) in hard_offsets else "lower"
        light = week.deload
        mins = STRENGTH_MIN if not light else 45
        title, det = describe_strength(mins, emphasis, light)
        strength[off] = _session(day_of(off), "strength", "strength", mins, week.phase, week.index, title, det)

    out: list[Session] = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        off = (d.weekday() - long_day) % 7
        if off in slots:
            out.append(slots[off])
        elif off in strength:
            out.append(strength[off])
        else:
            out.append(Session(day=d, kind="rest", distance_km=0.0, title="Lepo",
                               details="Lepo tai kevyt liikkuvuus/venyttely.", phase=week.phase,
                               week_index=week.index, sport="rest"))
    return out


def build_cycling_plan(goal: Goal, zones: dict[str, tuple[int, int]], until: date | None = None) -> list[Session]:
    start, weeks = week_plan(goal, until)
    out: list[Session] = []
    for w in weeks:
        out.extend(build_week(w, start + timedelta(weeks=w.index), goal, zones))
    return out
