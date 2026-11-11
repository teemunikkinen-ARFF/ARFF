"""Pyöräilykunnon kehitys + lihaskunnon ylläpito.

Periaatteet:
* Tehoalueet Coggan-malliin FTP:n prosentteina: Z2 56–75 %, tempo 76–90 %, sweet spot 88–94 %,
  kynnys 95–105 %, VO2max 106–120 %.
* 12 viikon sykli: peruskausi (sweet spot/tempo) → kehityskausi (kynnys, over-under) → huippukausi (VO2max + kynnys)
  → testiviikko (FTP-testi). Joka 4. viikko on kevyt (-30 % tunteja, vähemmän toistoja). Testin jälkeen uusi sykli
  alkaa päivitetyllä FTP:llä, joten watit seuraavat kehittymistäsi.
* Viikkotunnit kasvavat enintään 8 % viikossa, katto = 1,5 × nykyinen (tai --max-hours).
* Työvuorokalenterilla: ajot vapaille päiville, vuoropäivälle lyhyt valinnainen ajo (≤ 75 min) vain tarvittaessa.
* Voimaharjoittelu: vain ylä- ja keskivartalo (lääkärin kielto jalkaharjoittelulle salilla; jalkavoima tulee
  pyörältä). Sessiot A (veto), B (työntö), C (ylä-/keskivartalo), 40–45 min, 2 toiston vara.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from itertools import combinations, permutations

from .models import Goal, Session
from .shifts import day_type
from .planner import (
    DELOAD_FACTOR, PHASE_BASE, PHASE_BUILD, PHASE_DELOAD, PHASE_PEAK, WEEKLY_INCREASE, monday,
)

PHASE_TEST = "Testiviikko"
MACRO_WEEKS = 12
MIN_BIKE_MIN = 30
STRENGTH_MIN = 45  # voimasalin kesto (min)
STRENGTH_KINDS = ("pull", "push", "core")  # sali A, B, C viikon järjestyksessä

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


STRENGTH_TITLES = {
    "pull": "Voima A: veto + keskivartalo",
    "push": "Voima B: työntö + keskivartalo",
    "core": "Voima C: ylä- ja keskivartalo",
}
STRENGTH_BODY = {
    "pull": "leuanveto lisäpainolla 4 × 5, rintatuettu soutu 4 × 6, takaolkapäät 2 × 12, keskivartalo 2 sarjaa",
    "push": "penkkipunnerrus 4 × 5, pystypunnerrus istuen 3 × 6, ojentajat 2 × 10, ab wheel tai muu keskivartalo 2 sarjaa",
    "core": "ylävartalon volyymi RPE 7 (veto ja työntö 3 × 8–10), keskivartalo 3 sarjaa, liikkuvuus",
}


def strength_emphasis(title: str) -> str:
    """Päättelee session tyypin otsikosta (A = veto, B = työntö, C = ylä-/keskivartalo)."""
    for kind, t in STRENGTH_TITLES.items():
        if title.startswith(t.split(":")[0]):
            return kind
    return "core"


def describe_strength(minutes: int, emphasis: str, light: bool, on_shift: bool = False) -> tuple[str, str]:
    """Vain ylä- ja keskivartalo: lääkärin kielto jalkaharjoittelulle salilla. Jalkojen voima tulee pyörältä."""
    title = STRENGTH_TITLES[emphasis]
    light_txt = " Kevyt versio: 2 sarjaa/liike, ei lähelle epäonnistumista." if light else ""
    if on_shift:
        title += " (työvuorossa, valinnainen)"
        light_txt += " Tee vain jos vuoro sallii; jätä väliin, jos yö meni huonosti tai hälytyksiä tuli."
    return title, (f"{minutes} min: {STRENGTH_BODY[emphasis]}. Jätä 2 toistoa varaan, pysy aikarajassa. "
                   f"Ei jalkaliikkeitä (lääkärin kielto).{light_txt}")


# --- viikon rakentaminen ---------------------------------------------------------------

PRIORITY = {  # sijoitusjärjestys; viimeiset pudotetaan ensin, jos vapaita päiviä on liian vähän
    3: ["q1", "long", "q2"],
    4: ["q1", "long", "q2", "easy"],
    5: ["q1", "long", "q2", "easy", "easy2"],
}
REST_TEXT = {
    "shift": ("Työvuoro", "24 h vuoro, ei suunniteltua harjoitusta (hälytykset, katkonainen uni)."),
    "post": ("Vuoron jälkeinen päivä", "Palaudu: nuku tarvittaessa, kevyt kävely ok. Yön uni- ja HRV-arvoja ei käytetä valmiusarviossa."),
    "free": ("Lepo", "Lepo tai kevyt liikkuvuus/venyttely."),
    "": ("Lepo", "Lepo tai kevyt liikkuvuus/venyttely."),
}


def _session(day: date, kind: str, sport: str, minutes: int, phase: str, week: int, title: str, details: str,
             daytype: str = "") -> Session:
    return Session(day=day, kind=kind, distance_km=0.0, title=title, details=details, phase=phase,
                   week_index=week, sport=sport, duration_min=float(minutes), daytype=daytype)


def slot_kind(slot: str, week: Week) -> str:
    """Slotin sisältö tällä viikolla: long | quality | test | easy."""
    if slot == "long":
        return "long"
    if slot == "q1":
        return "test" if week.test else QUALITY_BY_PHASE[week.train_phase][0]
    if slot == "q2":
        return "easy" if (week.test or week.deload) else QUALITY_BY_PHASE[week.train_phase][1]
    return "easy"


SHIFT_RIDE_NOTE = " Tee vain jos vuoro sallii; jätä väliin, jos yö meni huonosti tai hälytyksiä tuli."


def _ride(slot: str, base_min: float, week: Week, day: date, goal: Goal, zones, daytype: str = "",
          on_shift: bool = False) -> Session:
    sess = _ride_core(slot, base_min, week, day, goal, zones, daytype)
    if on_shift:
        sess.title += " (työvuorossa, valinnainen)"
        sess.details += SHIFT_RIDE_NOTE
    return sess


def _ride_core(slot: str, base_min: float, week: Week, day: date, goal: Goal, zones, daytype: str = "") -> Session:
    ftp = goal.ftp
    kind = slot_kind(slot, week)
    if kind == "long":
        m = r5(min(270, max(60 if week.deload else 75, base_min)))
        title, det = describe_bike("long", m, week.phase, ftp, zones)
        return _session(day, "long", "bike", m, week.phase, week.index, title, det, daytype)
    if kind == "easy":
        m = r5(max(40, base_min))
        title, det = describe_bike("easy", m, week.phase, ftp, zones)
        return _session(day, "easy", "bike", m, week.phase, week.index, title, det, daytype)
    if kind == "test":
        m, step, light = max(r5(base_min), 90), 0, False
    else:
        step, light = week.step, week.deload
        m = max(r5(base_min), -(-quality_minutes(kind, step, light)[2] // 5) * 5)
    title, det = describe_quality(kind, m, step, light, ftp)
    return _session(day, kind, "bike", m, week.phase, week.index, title, det, daytype)


def _rest(day: date, week: Week, daytype: str = "") -> Session:
    title, det = REST_TEXT[daytype]
    return Session(day=day, kind="rest", distance_km=0.0, title=title, details=det, phase=week.phase,
                   week_index=week.index, sport="rest", daytype=daytype)


def build_week(week: Week, week_start: date, goal: Goal, zones: dict[str, tuple[int, int]]) -> list[Session]:
    """Kiinteä viikkomalli (ei työvuorokalenteria): ajot ja voima sidottu pitkän ajon viikonpäivään."""
    n = goal.days_per_week
    layout, shares = BIKE_LAYOUT[n], SHARES[n]
    long_day = goal.long_run_weekday
    total = week.hours * 60
    day_of = lambda off: week_start + timedelta(days=(long_day + off) % 7)  # noqa: E731

    slots: dict[int, Session] = {off: _ride(slot, total * shares[slot], week, day_of(off), goal, zones)
                                 for slot, off in layout.items()}
    # Voimaharjoitukset omille päivilleen (ohjelmassa yksi sessio/päivä), viikon järjestyksessä A, B, C.
    strength_offsets = sorted([o for o in STRENGTH_PREFS if o not in slots][: goal.strength_days], key=day_of)
    strength: dict[int, Session] = {}
    for i, off in enumerate(strength_offsets):
        mins = 40 if week.deload else STRENGTH_MIN
        title, det = describe_strength(mins, STRENGTH_KINDS[i % 3], week.deload)
        strength[off] = _session(day_of(off), "strength", "strength", mins, week.phase, week.index, title, det)

    out: list[Session] = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        off = (d.weekday() - long_day) % 7
        out.append(slots.get(off) or strength.get(off) or _rest(d, week))
    return out


def _is_hard(kind: str) -> bool:
    """Kaikki muut kuin peruskestävyysajo (pitkä ajo, testi ja kaikki laatuajot) ovat kovia."""
    return kind != "easy"


SHIFT_RIDE_MAX = 75  # vuoropäivän ajon enimmäiskesto (min)
SHIFT_RIDE_PENALTY = 4  # vuoropäivä vain, jos vapaat päivät eivät riitä tai johtaisivat huonompaan ratkaisuun


def shift_ride_ok(kind: str, week: Week) -> bool:
    """Vuoropäivälle sopii peruskestävyysajo tai lyhyt laatuajo; ei pitkää ajoa eikä FTP-testiä."""
    if kind == "easy":
        return True
    if kind in ("long", "test"):
        return False
    return quality_minutes(kind, week.step, week.deload)[2] <= SHIFT_RIDE_MAX


def assign_rides(slot_kinds: dict[str, str], free_days: list[date], shifts,
                 prev_ride: tuple[date, str] | None = None, shift_days: list[date] | None = None,
                 shift_ok: dict[str, bool] | None = None) -> dict[str, date] | None:
    """Sijoittaa ajot päiville. Rajoitteet: laatuajo ei peräkkäisenä päivänä toisen laatuajon kanssa eikä pitkän ajon
    jälkeen (laatu → pitkä sallitaan). Pehmeät: kova ajo ennen työvuoroa, kovat peräkkäin ja vuoropäivän käyttö
    (valinnainen, vain jos `shift_ok`). Palauttaa None, jos mahdotonta."""
    names = list(slot_kinds)
    kinds = dict(slot_kinds)
    if prev_ride:  # edellisen viikon viimeinen ajo (viikon rajan yli)
        kinds["_prev"] = prev_ride[1]
    shift_set = set(shift_days or [])
    shift_ok = shift_ok or {}
    best, best_key = None, None
    for combo in permutations(sorted(set(free_days) | shift_set), len(names)):
        placed = dict(zip(names, combo))
        if any(d in shift_set and not shift_ok.get(nm, False) for nm, d in placed.items()):
            continue
        ok, penalty = True, SHIFT_RIDE_PENALTY * sum(1 for d in placed.values() if d in shift_set)
        items = list(placed.items()) + ([("_prev", prev_ride[0])] if prev_ride else [])
        items.sort(key=lambda kv: kv[1])
        for (na, da), (nb, db) in zip(items, items[1:]):
            if (db - da).days == 1 and _is_hard(kinds[na]) and _is_hard(kinds[nb]):
                if kinds[na] == "long" or kinds[nb] != "long":
                    ok = False
                    break
                penalty += 1  # laatu → pitkä
        if not ok:
            continue
        for nm, d in placed.items():
            if _is_hard(slot_kinds[nm]) and (d + timedelta(days=1)) in shifts:
                penalty += 2  # kova ajo välittömästi ennen vuoroa
        key = (penalty, tuple(placed[nm].toordinal() for nm in names))
        if best_key is None or key < best_key:
            best, best_key = placed, key
    return best


def build_week_shifts(week: Week, week_start: date, goal: Goal, zones,
                      prev_ride: tuple[date, str] | None = None) -> list[Session]:
    """Viikko työvuorokalenterin mukaan: ajot ensisijaisesti vapaille päiville, vuoropäivä tarvittaessa (lyhyt,
    valinnainen), voima ensisijaisesti vuoropäiville joilla ei ole ajoa."""
    shifts = goal.shift_days
    days = [week_start + timedelta(days=i) for i in range(7)]
    dtype = {d: day_type(d, shifts) for d in days}
    free = [d for d in days if dtype[d] == "free"]
    shift_d = [d for d in days if dtype[d] == "shift"]
    n = goal.days_per_week
    total = week.hours * 60
    shares = SHARES[n]
    kind_of = {nm: slot_kind(nm, week) for nm in PRIORITY[n]}
    ok_on_shift = {nm: shift_ride_ok(k, week) for nm, k in kind_of.items()}

    placed: dict[str, date] = {}
    wanted = PRIORITY[n]
    for k in range(len(wanted), 0, -1):
        for names in combinations(wanted, k):  # etusijalla tärkeimmät slotit
            res = assign_rides({nm: kind_of[nm] for nm in names}, free, shifts, prev_ride, shift_d, ok_on_shift)
            if res:
                placed = res
                break
        if placed:
            break

    norm = sum(shares[nm] for nm in placed) or 1.0
    mins = {nm: total * shares[nm] / norm for nm in placed}
    deficit = 0.0
    for nm, d in placed.items():
        if dtype[d] == "shift" and mins[nm] > SHIFT_RIDE_MAX:
            deficit += mins[nm] - SHIFT_RIDE_MAX
            mins[nm] = SHIFT_RIDE_MAX
    if deficit and "long" in placed and dtype[placed["long"]] != "shift":
        mins["long"] += deficit  # lyhyt vuoropäivän ajo korvataan pitkällä ajolla

    by_day: dict[date, Session] = {}
    for nm, d in placed.items():
        by_day[d] = _ride(nm, mins[nm], week, d, goal, zones, dtype[d], on_shift=dtype[d] == "shift")
    if placed and len(placed) < n:
        first = min(by_day.values(), key=lambda x: x.day)
        first.adjusted = f"Ajoja mahtuu tällä viikolla vain {len(placed)}/{n} (työvuorot)"

    cands = [d for d in shift_d if d not in by_day] + [d for d in free if d not in by_day]
    for i, d in enumerate(sorted(cands[: goal.strength_days])):
        on_shift = dtype[d] == "shift"
        m = 40 if (week.deload or on_shift) else STRENGTH_MIN
        title, det = describe_strength(m, STRENGTH_KINDS[i % 3], week.deload, on_shift)
        by_day[d] = _session(d, "strength", "strength", m, week.phase, week.index, title, det, dtype[d])
    return [by_day.get(d) or _rest(d, week, dtype[d]) for d in days]


def build_cycling_plan(goal: Goal, zones: dict[str, tuple[int, int]], until: date | None = None) -> list[Session]:
    start, weeks = week_plan(goal, until)
    builder = build_week_shifts if goal.shift_days is not None else build_week
    out: list[Session] = []
    prev_ride = None
    for w in weeks:
        ws = start + timedelta(weeks=w.index)
        if builder is build_week_shifts:
            week = builder(w, ws, goal, zones, prev_ride)
            last = week[-1]
            prev_ride = (last.day, last.kind) if last.sport == "bike" else None
        else:
            week = builder(w, ws, goal, zones)
        out.extend(week)
    return out
